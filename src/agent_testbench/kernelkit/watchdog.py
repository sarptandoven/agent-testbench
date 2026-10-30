"""A hard stop that lives on the session's machine, so it holds while the machine that started it is asleep or gone.

    python -m agent_testbench.kernelkit.watchdog --state-dir DIR --max-minutes M [--idle-minutes I]
        [--kill-group PID] [--interrupt-pid PID] [--release colab]

Activity is what kernelkit's client leaves next to the kernel's connection file: `busy` (until when a command may
run) while code runs, and `last_used` when it ends. Idle time counts only once the kernel has been ready, so a slow
start is not mistaken for idleness. At max_minutes, or after idle_minutes without a command, the watchdog writes
`watchdog_stopped` with the reason, ends the testbench processes on the machine (the launcher's process group and
anything started with the state folder on its command line: the kernel, its clients), interrupts --interrupt-pid
and the pid in `interrupt.pid` (a plan running in Colab's own kernel writes it), and with --release colab asks
Colab to release the VM - the request google.colab.runtime.unassign() makes, to the runtime service named by
$TBE_RUNTIME_ADDR. If the release fails, nothing testbench started is left running, so Colab reclaims the idle VM.

Standard library only, so it can be copied to a machine on its own and started before anything is installed.
"""
import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


def mark_busy(state_dir, until: float) -> None:
    try:
        (Path(state_dir) / "busy").write_text(str(until))
    except OSError:
        pass


def mark_used(state_dir) -> None:
    try:
        (Path(state_dir) / "busy").unlink(missing_ok=True)
        (Path(state_dir) / "last_used").write_text(str(time.time()))
    except OSError:
        pass


def can_release(release: str | None) -> bool:
    """Whether this machine can release itself, so a session can say which kind of hard stop it has."""
    return release == "colab" and bool(os.environ.get("TBE_RUNTIME_ADDR"))


def spawn(state_dir, *, max_minutes: float, idle_minutes: float | None = None, kill_group: int | None = None,
          interrupt_pid: int | None = None, release: str | None = None, env: dict | None = None) -> int:
    """Start the watchdog detached from the caller (its own session), so it outlives the command that started it."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    argv = [sys.executable, "-m", "agent_testbench.kernelkit.watchdog", "--state-dir", str(state_dir),
            "--max-minutes", str(max_minutes)]
    for flag, value in (("--idle-minutes", idle_minutes), ("--kill-group", kill_group),
                        ("--interrupt-pid", interrupt_pid), ("--release", release)):
        if value is not None:
            argv += [flag, str(value)]
    log = open(Path(state_dir) / "watchdog.log", "a")
    return subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True).pid


def _log(state_dir: Path, text: str) -> None:
    with open(state_dir / "watchdog.log", "a") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {text}\n")


def _ours(state_dir: Path) -> list[int]:
    """Processes of this session: its state folder is on their command line (the launcher's --state-dir, the
    kernel's connection file, each client's --connection). Other sessions on the same machine are left alone."""
    me, out = os.getpid(), []
    mark = str(Path(state_dir).resolve()).encode()
    proc = Path("/proc")
    if not proc.is_dir():                           # not Linux: the launcher's process group is all there is
        return out
    for d in proc.iterdir():
        if not d.name.isdigit() or int(d.name) == me:
            continue
        try:
            cmd = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if b"agent_testbench.kernelkit.watchdog" not in cmd and (mark + b"/" in cmd or mark + b"\x00" in cmd):
            out.append(int(d.name))
    return out


def _signal(pid: int, sig) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)                     # a group leader: the process and whatever it started
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def end_processes(state_dir: Path, kill_group: int | None, interrupt_pid: int | None) -> None:
    written = state_dir / "interrupt.pid"
    noted = written.read_text().strip() if written.exists() else ""
    for pid in {interrupt_pid, int(noted) if noted.isdigit() else None}:
        if pid:
            try:
                os.kill(pid, signal.SIGINT)         # a plan running in Colab's kernel stops and writes its report
            except (ProcessLookupError, PermissionError):
                pass
    if interrupt_pid or written.exists():
        time.sleep(5)
    targets = ([kill_group] if kill_group else []) + _ours(state_dir)
    for pid in targets:
        _signal(pid, signal.SIGTERM)                # the launcher shuts its kernel down on SIGTERM
    end = time.time() + 10
    while time.time() < end and any(_alive(p) for p in targets):
        time.sleep(0.25)
    for pid in targets + _ours(state_dir):
        if _alive(pid):
            _signal(pid, signal.SIGKILL)


def release_colab(state_dir: Path) -> bool:
    addr = os.environ.get("TBE_RUNTIME_ADDR")
    if not addr:
        _log(state_dir, "no TBE_RUNTIME_ADDR here, so the VM cannot release itself; Colab reclaims it once idle")
        return False
    for wait in (5, 15, 30, None):
        try:
            req = urllib.request.Request(f"http://{addr}/unassign", data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status == 200:
                    _log(state_dir, "asked Colab to release this VM: accepted")
                    return True
                _log(state_dir, f"release refused: HTTP {r.status}")
        except Exception as exc:
            _log(state_dir, f"release failed: {type(exc).__name__}: {exc}")
        if wait:
            time.sleep(wait)
    return False


def watch(state_dir, max_minutes: float, idle_minutes: float | None = None, kill_group: int | None = None,
          interrupt_pid: int | None = None, release: str | None = None) -> str:
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    began = time.time()
    tick = float(os.environ.get("TESTBENCH_WATCHDOG_SECONDS", 15))
    _log(state_dir, f"watching: stop at {max_minutes:g} min" + (f" or {idle_minutes:g} idle" if idle_minutes else ""))
    was_ready = False
    while True:
        now = time.time()
        reason = None
        if not was_ready and (state_dir / "ready").exists():
            was_ready, began_idle = True, now
        if (now - began) / 60 >= max_minutes:
            reason = f"reached max_minutes ({max_minutes:g}) on the machine itself"
        elif idle_minutes and was_ready:
            busy, used = state_dir / "busy", state_dir / "last_used"
            try:
                running = busy.exists() and float(busy.read_text() or 0) > now
            except (OSError, ValueError):
                running = False
            last = max(began_idle, used.stat().st_mtime if used.exists() else began_idle)
            if not running and (now - last) / 60 >= idle_minutes:
                reason = f"no command for {idle_minutes:g} min, checked on the machine itself"
        if reason:
            break
        time.sleep(tick)
    (state_dir / "watchdog_stopped").write_text(reason + "\n")
    _log(state_dir, f"stopping: {reason}")
    end_processes(state_dir, kill_group, interrupt_pid)
    if release == "colab":
        release_colab(state_dir)
    return reason


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--max-minutes", type=float, required=True)
    ap.add_argument("--idle-minutes", type=float)
    ap.add_argument("--kill-group", type=int)
    ap.add_argument("--interrupt-pid", type=int)
    ap.add_argument("--release", choices=["colab"])
    a = ap.parse_args(argv)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)     # the command that started it may end; the watchdog stays
    watch(a.state_dir, a.max_minutes, a.idle_minutes, a.kill_group, a.interrupt_pid, a.release)


if __name__ == "__main__":
    main()
