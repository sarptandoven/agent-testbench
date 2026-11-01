"""Live sessions: a kernel that stays up on a machine (this one, a Modal GPU, a Colab VM) so an agent can work in
it the way a person works in a notebook - run code or cells, look at what came back, fix, run again - without
restarting or losing state.

A session lives in .testbench/sessions/<name>/ (state.json, exec.log, outputs/). It is started inside the budget
(worst case = max_minutes x hardware rate), watched by a supervisor process that stops it after idle_minutes
without use or at max_minutes, and remote machines are also stopped on their own side (Modal Sandbox timeout and
idle timeout). Stopping records what it cost.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from . import backends as B, budget, config as C, notebook as NB, procs
from .session_backends import SessionError

ACTIVE = ("starting", "ready")


def sessions_dir(root: Path) -> Path:
    return Path(root) / ".testbench" / "sessions"


def read(sdir: Path) -> dict:
    return json.loads(procs.read_text_retry(Path(sdir) / "state.json"))


def write(sdir: Path, **changes) -> dict:
    p = Path(sdir) / "state.json"
    state = json.loads(procs.read_text_retry(p)) if p.exists() else {}
    state.update(changes)
    procs.write_text_atomic(p, json.dumps(state, indent=1))
    return state


def all_sessions(root: Path) -> list[Path]:
    d = sessions_dir(root)
    return sorted(p for p in d.iterdir() if (p / "state.json").exists()) if d.exists() else []


def resolve(root: Path, name: str | None) -> Path:
    """A session by name; with no name, the only active one."""
    if name:
        sdir = sessions_dir(root) / name
        if not (sdir / "state.json").exists():
            raise SessionError(f"No session named {name!r}{C.near(name, [s.name for s in all_sessions(root)])}. "
                               "`testbench session list` shows them.")
        return sdir
    active = [s for s in all_sessions(root) if read(s).get("status") in ACTIVE]
    if len(active) == 1:
        return active[0]
    if not active:
        raise SessionError("No session is running. Start one with `testbench session start`.")
    raise SessionError(f"{len(active)} sessions are running ({', '.join(s.name for s in active)}); name one with -s")


def _backend(root: Path, cfg: dict, sdir: Path):
    state = read(sdir)
    return B.get(state["backend"]).session_class()(root, cfg, sdir, state)


def hardware(cfg: dict, backend: str | None, gpu: str | None) -> dict:
    s = cfg["sessions"]
    b = backend or s["backend"]
    g = gpu if gpu is not None else (s.get("gpu") if b == s["backend"] else None)
    C.check_hardware("session", b, g or None)
    return {"backend": b, "gpu": g or None, "cpu": s.get("cpu"), "memory_gb": s.get("memory_gb")}


def start(root: Path, cfg: dict, name: str = "main", backend: str | None = None, gpu: str | None = None,
          idle_minutes: float | None = None, max_minutes: float | None = None, secrets: list[str] | None = None,
          approve: bool = False) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,30}", name):
        raise SessionError("session names use letters, digits, - or _ (up to 31 characters)")
    hw = hardware(cfg, backend, gpu)
    idle = idle_minutes or cfg["sessions"]["idle_minutes"]
    longest = max_minutes or cfg["sessions"]["max_minutes"]
    for what, v in (("idle_minutes", idle), ("max_minutes", longest)):
        if not C.number(v, positive=True):
            raise SessionError(f"{what} must be a positive number of minutes, not {v!r}")
    idle, longest = float(idle), float(longest)
    most = B.get(hw["backend"]).max_session_minutes
    if most and longest > most:
        raise SessionError(f"a {hw['backend']} session can last at most {C.counted(most / 60, 'hour')} "
                           f"(max_minutes <= {most:g})")
    sdir = sessions_dir(root) / name
    if (sdir / "state.json").exists():
        old = read(sdir)
        if old.get("status") in ACTIVE:
            raise SessionError(f"session {name!r} is already running on {old['backend']}; stop it first or use another name")
        archive = sessions_dir(root) / ".old" / f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        sdir.rename(archive)
    with budget.launching():
        ok, message, est = budget.check(cfg, budget.estimate(cfg, hw, longest, f"session {name}"), approve,
                                        "Lower max_minutes")
        if ok:
            (sdir / "outputs").mkdir(parents=True)
            now = time.time()
            state = write(sdir, name=name, backend=hw["backend"], gpu=hw["gpu"], cpu=hw["cpu"],
                          memory_gb=hw["memory_gb"], status="starting", created=now, idle_minutes=idle,
                          max_minutes=longest, usd_per_minute=est["usd_per_minute"],
                          worst_case_usd=est["worst_case_usd"],
                          secrets=list(secrets or cfg["sessions"].get("secrets") or []), exec_count=0, handles={},
                          budget_message=message)
            budget.record(root, {"event": "launch", "run": f"session:{name}:{int(now)}", "plan": f"session {name}",
                                 "backend": hw["backend"], "worst_case_usd": est["worst_case_usd"]})
    if not ok:                                         # told outside the launch lock, so no launch waits on it
        from . import notify
        how = " ".join(["testbench session start", name] + (["--backend", hw["backend"]] if backend else [])
                       + (["--gpu", hw["gpu"]] if hw["gpu"] else []) + ["--max-minutes", f"{longest:g}", "--approve"])
        notify.approval_needed(root, cfg, message, est, how)
        raise SessionError(message)
    write(sdir, ledger_id=f"session:{name}:{int(now)}")
    be = B.get(hw["backend"]).session_class()(root, cfg, sdir, state)
    try:
        handles = be.start()
    except BaseException as exc:
        try:
            be.stop()
        except Exception:
            pass
        _finish(root, sdir, "error", f"could not start: {exc}")
        raise SessionError(str(exc)) from None
    now = time.time()
    write(sdir, status="ready", handles=handles, started=now, last_used=now, busy_until=0)
    from .runs import spawn
    sup = spawn([sys.executable, "-m", "agent_testbench.sessions", "supervise", str(sdir)], cwd=root,
                log_path=sdir / "supervisor.log")
    return write(sdir, supervisor_pid=sup.pid)


def _finish(root: Path, sdir: Path, status: str, reason: str) -> dict:
    s = read(sdir)
    if s.get("status") in ("stopped", "error") and s.get("stopped"):
        return s
    end = time.time()
    began = s.get("started") or s.get("created") or end
    usd = round(s.get("usd_per_minute", 0) * (end - began) / 60, 4)
    s = write(sdir, status=status, stopped=end, stop_reason=reason, cost_usd=usd)
    budget.record(root, {"event": "finish", "run": s.get("ledger_id", f"session:{s['name']}"), "plan": f"session {s['name']}",
                         "backend": s["backend"], "status": status, "minutes": round((end - began) / 60, 2), "usd": usd})
    return s


def stop(root: Path, cfg: dict, sdir: Path, reason: str = "stopped by request") -> dict:
    s = read(sdir)
    if s.get("status") not in ACTIVE:
        return s
    _backend(root, cfg, sdir).stop()
    pid = s.get("supervisor_pid")
    s = _finish(root, sdir, "stopped", reason)
    if pid and pid != os.getpid():
        from .runs import kill_group
        kill_group(pid)
    return s


@contextmanager
def _in_use(sdir: Path, timeout: float):
    """One command at a time per session; the supervisor sees the session as busy until it ends."""
    with procs.exclusive(sdir / ".lock"):
        write(sdir, busy_until=time.time() + timeout + 300, last_used=time.time())
        try:
            yield
        finally:
            write(sdir, busy_until=0, last_used=time.time())


def _ready(root: Path, cfg: dict, sdir: Path) -> dict:
    s = read(sdir)
    if s.get("status") not in ACTIVE:
        raise SessionError(f"session {s['name']} is {s.get('status')} ({s.get('stop_reason', '')}); start a new one")
    return s


def exec_code(root: Path, cfg: dict, sdir: Path, code: str, timeout: float = 600, label: str | None = None) -> dict:
    s = _ready(root, cfg, sdir)
    n = s.get("exec_count", 0) + 1
    prefix = f"exec{n:04d}"
    with _in_use(sdir, timeout):
        be = _backend(root, cfg, sdir)
        try:
            res = be.exec(code, timeout, prefix, sdir / "outputs")
        except Exception as exc:
            if not be.alive():
                _finish(root, sdir, "stopped", "the machine went away (stopped by its provider or timed out)")
                raise SessionError(f"session {s['name']} is gone: its machine stopped. Start a new session.") from None
            raise
    write(sdir, exec_count=n)
    res.update(n=n, label=label or "code")
    with (sdir / "exec.log").open("a") as log:
        log.write(json.dumps({"n": n, "label": res["label"], "status": res["status"], "seconds": res["seconds"],
                              "code": code[:2000], "error": res.get("error")}) + "\n")
    return res


def exec_cells(root: Path, cfg: dict, sdir: Path, nb_path: Path, spec, params: dict, timeout: float) -> list[dict]:
    """Run a notebook's code cells (numbered from 1) in the session, with params set; stop at the first error."""
    nb = NB.read(nb_path)
    if spec and params and NB.language(nb) == "python":
        NB.check_cells_set_params(nb, params, C.parse_cells(spec))
    code = NB.cell_sources(nb, params or {})
    numbers = C.parse_cells(spec) if spec else list(range(1, len(code) + 1))
    bad = [x for x in numbers if x > len(code)]
    if bad:
        raise SessionError(f"cells {bad} asked for, but {nb_path.name} has {C.counted(len(code), 'code cell')}")
    out = []
    for n in numbers:
        res = exec_code(root, cfg, sdir, code[n - 1], timeout, label=f"{nb_path.name} cell {n}")
        res["cell"] = n
        out.append(res)
        if res["status"] != "ok":
            break
    return out


def files(root: Path, cfg: dict, sdir: Path, op: str, *paths: str) -> str:
    _ready(root, cfg, sdir)
    be = _backend(root, cfg, sdir)
    with _in_use(sdir, 1800):
        if op == "ls":
            return "\n".join(be.ls(paths[0] if paths else "."))
        if op == "get":
            remote = paths[0]
            local = Path(paths[1]) if len(paths) > 1 else sdir / "outputs" / Path(remote).name
            local.parent.mkdir(parents=True, exist_ok=True)
            be.get(remote, local)
            return f"copied {remote} to {local} ({local.stat().st_size / 1e6:.2f} MB)"
        if op == "put":
            be.put(Path(paths[0]), paths[1])
            return f"copied {paths[0]} to {paths[1]} on the session's machine"
    raise SessionError("files: use ls [PATH], get REMOTE [LOCAL] or put LOCAL REMOTE")


def install(root: Path, cfg: dict, sdir: Path, packages: list[str]) -> str:
    _ready(root, cfg, sdir)
    with _in_use(sdir, 1800):
        return _backend(root, cfg, sdir).install(packages)


def sync(root: Path, cfg: dict, sdir: Path) -> str:
    _ready(root, cfg, sdir)
    with _in_use(sdir, 1800):
        return _backend(root, cfg, sdir).sync()


def shell(root: Path, cfg: dict, sdir: Path, command: str, timeout: float = 120) -> str:
    _ready(root, cfg, sdir)
    with _in_use(sdir, timeout):
        return _backend(root, cfg, sdir).shell(command, timeout)


def describe(sdir: Path) -> str:
    s = read(sdir)
    now = time.time()
    hw = s["backend"] + (f" {s['gpu']}" if s.get("gpu") else "")
    if s.get("status") in ACTIVE:
        up = (now - (s.get("started") or s["created"])) / 60
        idle = (now - s.get("last_used", now)) / 60
        cost = s.get("usd_per_minute", 0) * up
        return (f"{s['name']}  {s['status']} on {hw}  up {up:.1f} min, idle {idle:.1f} min (stops after "
                f"{s['idle_minutes']:g} idle / {s['max_minutes']:g} total)  ~${cost:.3f} so far  {s.get('exec_count', 0)} exec(s)")
    return (f"{s['name']}  {s.get('status')} on {hw}  ({s.get('stop_reason', '')})  ~${s.get('cost_usd', 0):.3f}  "
            f"{s.get('exec_count', 0)} exec(s)")


def supervise(sdir: Path) -> None:
    """Stop the session after idle_minutes without use, at max_minutes, or when its machine disappears."""
    sdir = Path(sdir).resolve()
    root = sdir.parents[2]
    cfg = C.load(root)
    checks = 0
    while True:
        s = read(sdir)
        if s.get("status") not in ACTIVE:
            return
        now = time.time()
        busy = now < s.get("busy_until", 0)
        idle = (now - s.get("last_used", now)) / 60
        age = (now - (s.get("started") or s["created"])) / 60
        reason = None
        if age >= s["max_minutes"]:
            reason = f"reached max_minutes ({s['max_minutes']:g})"
        elif not busy and idle >= s["idle_minutes"]:
            reason = f"idle for {s['idle_minutes']:g} min"
        elif checks % 8 == 7 and not busy and not _backend(root, cfg, sdir).alive():
            _finish(root, sdir, "stopped", "the machine went away (stopped by its provider or timed out)")
            return
        if reason:
            stop(root, cfg, sdir, reason)
            print(f"[testbench] session {s['name']} stopped: {reason}", flush=True)
            return
        checks += 1
        time.sleep(float(os.environ.get("TESTBENCH_SUPERVISE_SECONDS", 15)))


if __name__ == "__main__" and len(sys.argv) == 3 and sys.argv[1] == "supervise":
    procs.on_stop(lambda *_: sys.exit(0))
    supervise(Path(sys.argv[2]))
