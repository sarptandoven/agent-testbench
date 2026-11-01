"""Runs: launching a plan in the background, finding runs, cancelling them, and closing them out.

A run lives in .testbench/runs/<run-id>/ with state.json (status, process, remote handles), plan.json,
progress.jsonl, report.json and report.md, and a folder per step. `testbench run` starts a detached driver
process, so a run outlives the shell (or agent tool call) that started it.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import budget, config as C, notify, procs, provenance, report as R

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
    """A run id, a unique prefix, 'last', 'last~N' (the Nth run before the latest), or None (the latest run)."""
    runs = all_runs(root)
    if not runs:
        raise FileNotFoundError("No runs yet. Start one with `testbench run <plan>`.")
    if run in (None, "", "last", "latest"):
        return runs[-1]
    back = re.fullmatch(r"(?:last|latest)~(\d{1,9})", str(run))
    if back:
        if int(back.group(1)) >= len(runs):
            raise FileNotFoundError(f"{run} is before the first run: there "
                                    f"{'is' if len(runs) == 1 else 'are'} {C.counted(len(runs), 'run')} "
                                    "(`testbench runs` lists them)")
        return runs[-1 - int(back.group(1))]
    hits = [p for p in runs if p.name == run] or [p for p in runs if p.name.startswith(run)]
    if len(hits) > 1:
        raise FileNotFoundError(f"run {run!r} matches {len(hits)} runs ({', '.join(p.name for p in hits[-3:])}"
                                f"{', ...' if len(hits) > 3 else ''}); give more of its id. `testbench runs` lists them")
    if not hits:
        of_plan = [p for p in runs if p.name.split("-", 2)[-1] == run]
        raise FileNotFoundError(
            f"{run} is a plan, not a run; its latest run is {of_plan[-1].name} (a prefix of the id is enough). "
            "`testbench runs` lists them" if of_plan else
            f"No run {run!r}. Run ids look like {runs[-1].name} (the latest; a prefix is enough, or last, last~1). "
            "`testbench runs` lists them")
    return hits[0]


def previous_report(root: Path, run_dir: Path, plan: str) -> dict | None:
    for p in reversed(all_runs(root)):
        if p.name >= run_dir.name:
            continue
        rep = R.load(p)
        if rep and rep.get("plan") == plan and rep.get("status") in DONE:
            return rep
    return None


def launch(root: Path, cfg: dict, plan_name: str, approve: bool = False, foreground: bool = False,
           origin: dict | None = None) -> Path:
    """Start a run of a plan inside the budget. origin: what started it, kept in its state (a loop: {loop, try})."""
    if plan_name not in cfg["plans"]:
        raise LaunchRefused(C.no_plan(cfg, plan_name))
    plan = cfg["plans"][plan_name]
    source = provenance.source(root)                   # the code this run proves, before anything changes it
    with budget.launching():
        ok, message, est = budget.check_launch(cfg, plan_name, approve)
        if ok:
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
                        usd_per_minute=est["usd_per_minute"], approved=bool(approve), budget_message=message,
                        source=source, **({"origin": origin} if origin else {}))
            budget.record(root, {"event": "launch", "run": run_dir.name, "plan": plan_name, "backend": plan["backend"],
                                 "worst_case_usd": est["worst_case_usd"]})
    if not ok:                                         # told outside the launch lock, so no launch waits on it
        notify.approval_needed(root, cfg, message, est, f"testbench run {plan_name} --approve")
        raise LaunchRefused(message)
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


spawn, alive = procs.spawn, procs.alive          # background processes, the same on Linux, macOS and Windows


def kill_group(pid: int | None, hard: bool = False) -> None:
    procs.stop_group(pid, hard=hard)


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
        state = settle(run_dir)
        if state.get("status") in DONE:
            return state
        if time.time() >= end:
            return state
        time.sleep(2 if state.get("backend") == "local" else 5)


def settle(run_dir: Path) -> dict:
    """The run's state now. A run whose driver exited without finishing it is closed here, as an error (and told)."""
    state = read_state(run_dir)
    if state.get("status") in ("queued", "running") and not alive(state.get("driver_pid")) and not state.get("foreground"):
        time.sleep(1)
        state = read_state(run_dir)
        if state.get("status") not in DONE:
            write_state(run_dir, status="error", error="the driver process exited without finishing; see driver.log")
            notify.run_end(Path(run_dir).resolve().parents[2], run_dir)
            return read_state(run_dir)
    return state


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
        from . import backends
        msg = backends.get(s["backend"]).cancel_remote(s)
        write_state(run_dir, status="cancelled", error="cancelled by request" + (f"; {msg}" if msg else ""))
        notify.run_end(Path(run_dir).resolve().parents[2], run_dir)
    return f"{run_dir.name} cancelled"


def finalize(root: Path, run_dir: Path, rep: dict, cost_usd: float, cfg: dict | None = None) -> dict:
    """Close a run out: its report, its state, the ledgers, then the run_end notification (after the state says it
    finished, so nothing waiting on the run waits on the notification)."""
    state = read_state(run_dir)
    rep.update(run_id=run_dir.name, plan=state["plan"], backend=state["backend"], gpu=state.get("gpu"),
               cost_usd=round(cost_usd, 4), schema_version=provenance.SCHEMA_VERSION, source=state.get("source"),
               worst_case_usd=state.get("worst_case_usd"), approved=bool(state.get("approved")),
               remote_stop=state.get("remote_stop"))
    rep["evidence"], rep["evidence_sha256"] = provenance.evidence(run_dir)
    rep["delta"] = R.compare(previous_report(root, run_dir, state["plan"]), rep)
    R.write(run_dir, rep)
    write_state(run_dir, status=rep["status"], finished=time.time(), cost_usd=round(cost_usd, 4),
                minutes=rep.get("minutes"))
    budget.record(root, {"event": "finish", "run": run_dir.name, "plan": state["plan"], "backend": state["backend"],
                         "status": rep["status"], "minutes": rep.get("minutes"), "usd": round(cost_usd, 4)})
    notify.run_end(root, run_dir, rep, cfg)
    from .export import after_run
    after_run(root, run_dir)                         # testbench.yaml's export:, never raises or waits on a tracker
    return rep
