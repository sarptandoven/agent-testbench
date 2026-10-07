"""Runs: launching a plan in the background, finding runs, cancelling them, and closing them out.

A run lives in .testbench/runs/<run-id>/ with state.json (status, process, remote handles), plan.json,
progress.jsonl, report.json and report.md, and a folder per step. `testbench run` starts a detached driver
process, so a run outlives the shell (or agent tool call) that started it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import budget, config as C, report as R

DONE = ("passed", "failed", "cancelled", "error")


class LaunchRefused(Exception):
    pass


def runs_dir(root: Path) -> Path:
    return Path(root) / ".testbench" / "runs"


def read_state(run_dir: Path) -> dict:
    return json.loads((Path(run_dir) / "state.json").read_text())


def write_state(run_dir: Path, **changes) -> dict:
    p = Path(run_dir) / "state.json"
    state = json.loads(p.read_text()) if p.exists() else {}
    state.update(changes)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)
    return state


def all_runs(root: Path) -> list[Path]:
    d = runs_dir(root)
    return sorted((p for p in d.iterdir() if (p / "state.json").exists()), key=lambda p: p.name) if d.exists() else []


def resolve(root: Path, run: str | None) -> Path:
    """A run id, a unique prefix, 'last', or None (the latest run)."""
    runs = all_runs(root)
    if not runs:
        raise FileNotFoundError("No runs yet. Start one with `testbench run <plan>`.")
    if run in (None, "", "last", "latest"):
        return runs[-1]
    hits = [p for p in runs if p.name == run] or [p for p in runs if p.name.startswith(run)]
    if len(hits) != 1:
        raise FileNotFoundError(f"run {run!r} matches {len(hits)} runs; `testbench runs` lists them")
    return hits[0]


def previous_report(root: Path, run_dir: Path, plan: str) -> dict | None:
    for p in reversed(all_runs(root)):
        if p.name >= run_dir.name:
            continue
        rep = R.load(p)
        if rep and rep.get("plan") == plan and rep.get("status") in DONE:
            return rep
    return None


def launch(root: Path, cfg: dict, plan_name: str, approve: bool = False, foreground: bool = False) -> Path:
    if plan_name not in cfg["plans"]:
        raise LaunchRefused(f"No plan {plan_name!r}. Plans: {', '.join(cfg['plans'])}")
    ok, message, est = budget.check_launch(cfg, plan_name, approve)
    if not ok:
        raise LaunchRefused(message)
    plan = cfg["plans"][plan_name]
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = runs_dir(root) / f"{stamp}-{plan_name}"
    n = 1
    while run_dir.exists():
        n += 1
        run_dir = runs_dir(root) / f"{stamp}-{plan_name}-{n}"
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=1))
    write_state(run_dir, run_id=run_dir.name, plan=plan_name, backend=plan["backend"], gpu=plan.get("gpu"),
                status="queued", created=time.time(), worst_case_usd=est["worst_case_usd"],
                usd_per_minute=est["usd_per_minute"], approved=bool(approve), budget_message=message)
    budget.record(root, {"event": "launch", "run": run_dir.name, "plan": plan_name, "backend": plan["backend"],
                         "worst_case_usd": est["worst_case_usd"]})
    _gitignore(root)
    if foreground:
        from . import driver
        driver.drive(run_dir)
        return run_dir
    proc = spawn([sys.executable, "-m", "agent_testbench.driver", str(run_dir)], cwd=root, log_path=run_dir / "driver.log")
    write_state(run_dir, driver_pid=proc.pid)
    return run_dir


def _gitignore(root: Path) -> None:
    gi = Path(root) / ".testbench" / ".gitignore"
    if not gi.exists():
        gi.write_text("# runs, ledger and generated files are local evidence; NOTES.md is worth committing\n*\n!.gitignore\n!NOTES.md\n")


def spawn(argv: list[str], *, cwd, log_path: Path, env: dict | None = None) -> subprocess.Popen:
    """Start a background process in its own session. A thread waits on it, so a long-lived caller (the MCP server)
    collects it when it exits instead of keeping a zombie that still looks alive."""
    import threading
    log = open(log_path, "a")
    proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=env)
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def kill_group(pid: int | None, sig=signal.SIGTERM) -> None:
    if not pid:
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):          # already gone (EPERM: an exited group on macOS)
        pass


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def status_line(run_dir: Path) -> str:
    s = read_state(run_dir)
    st = s.get("status")
    if st in ("queued", "running") and not alive(s.get("driver_pid")) and not s.get("foreground"):
        st = "lost (driver exited without finishing; see driver.log)"
    events = progress(run_dir)
    step = next((e for e in reversed(events) if e.get("event") == "step_start"), None)
    where = f", step {step['index']}: {step['step']}" if step and st in ("queued", "running") else ""
    cost = s.get("cost_usd")
    return (f"{run_dir.name}  {st}{where}  backend={s.get('backend')}{'/' + s['gpu'] if s.get('gpu') else ''}"
            + (f"  ~${cost:.3f}" if isinstance(cost, (int, float)) else f"  (worst case ${s.get('worst_case_usd', 0):.2f})"))


def progress(run_dir: Path) -> list[dict]:
    p = Path(run_dir) / "progress.jsonl"
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def live_log(run_dir: Path) -> tuple[str, Path | None]:
    """The log of the step running now (or the last one)."""
    steps = sorted((Path(run_dir) / "steps").glob("*/log.txt")) if (Path(run_dir) / "steps").exists() else []
    if not steps:
        return "", None
    return steps[-1].read_text(errors="replace"), steps[-1]


def wait(run_dir: Path, timeout: float, on_line=print) -> dict:
    """Follow a run until it finishes or `timeout` seconds pass, printing progress and new log lines."""
    end = time.time() + timeout
    seen_events, seen_log = 0, {}
    while True:
        events = progress(run_dir)
        for e in events[seen_events:]:
            if e.get("event") == "step_start":
                on_line(f"[testbench] step {e['index']} started: {e['step']}")
            elif e.get("event") == "step_end":
                on_line(f"[testbench] step {e['index']} {e['status']} after {e['minutes']} min: {e['step']}")
        seen_events = len(events)
        text, path = live_log(run_dir)
        if path is not None:
            before = seen_log.get(path, 0)
            new = text[before:]
            if new.strip():
                for line in new.rstrip().splitlines()[-30:]:
                    on_line("  " + line[:300])
            seen_log[path] = len(text)
        state = read_state(run_dir)
        if state.get("status") in DONE:
            return state
        if state.get("status") in ("queued", "running") and not alive(state.get("driver_pid")) and not state.get("foreground"):
            time.sleep(1)
            state = read_state(run_dir)
            if state.get("status") not in DONE:
                write_state(run_dir, status="error", error="the driver process exited without finishing; see driver.log")
                return read_state(run_dir)
        if time.time() >= end:
            return state
        time.sleep(2 if state.get("backend") == "local" else 5)


def cancel(run_dir: Path) -> str:
    s = read_state(run_dir)
    if s.get("status") in DONE:
        return f"{run_dir.name} already {s['status']}"
    pid = s.get("driver_pid")
    if alive(pid):
        kill_group(pid)                                # the driver cancels remote work on its way out
        for _ in range(60):
            if read_state(run_dir).get("status") in DONE:
                break
            time.sleep(1)
    s = read_state(run_dir)
    if s.get("status") not in DONE:                  # the driver is gone: cancel remote work from here
        from .backends import cancel_remote
        msg = cancel_remote(s)
        write_state(run_dir, status="cancelled", error="cancelled by request" + (f"; {msg}" if msg else ""))
    return f"{run_dir.name} cancelled"


def finalize(root: Path, run_dir: Path, rep: dict, cost_usd: float) -> dict:
    state = read_state(run_dir)
    rep.update(run_id=run_dir.name, plan=state["plan"], backend=state["backend"], gpu=state.get("gpu"),
               cost_usd=round(cost_usd, 4))
    rep["delta"] = R.compare(previous_report(root, run_dir, state["plan"]), rep)
    R.write(run_dir, rep)
    write_state(run_dir, status=rep["status"], finished=time.time(), cost_usd=round(cost_usd, 4),
                minutes=rep.get("minutes"))
    budget.record(root, {"event": "finish", "run": run_dir.name, "plan": state["plan"], "backend": state["backend"],
                         "status": rep["status"], "minutes": rep.get("minutes"), "usd": round(cost_usd, 4)})
    return rep
