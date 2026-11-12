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
import math
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
    try:
        with open(state_dir / "watchdog.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {text}\n")
    except OSError:                  # the work may have broken the log; stopping it matters more than the log
        pass


def _commands():
    """(pid, command line) of every process: from /proc on Linux (arguments end in NUL), else from ps (macOS, BSD;
    arguments end in a space; ww: never cut to the terminal width)."""
    proc = Path("/proc")
    if proc.is_dir():
        for d in proc.iterdir():
            if d.name.isdigit():
                try:
                    yield int(d.name), (d / "cmdline").read_bytes()
                except OSError:
                    continue
        return
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,command="], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return
    for line in out.splitlines():
        pid, _, cmd = line.strip().partition(b" ")
        if pid.isdigit():
            yield int(pid), cmd + b" "


def _ours(state_dir: Path) -> list[int]:
    """Processes of this session: its state folder is on their command line (the launcher's --state-dir, the
    kernel's connection file, each client's --connection). Other sessions on the same machine are left alone, and
    so are watchdogs."""
    me = os.getpid()
    marks = {str(Path(state_dir)).encode(), str(Path(state_dir).resolve()).encode()}   # /tmp is /private/tmp on macOS
    return [pid for pid, cmd in _commands() if pid != me and b"agent_testbench.kernelkit.watchdog" not in cmd
            and any(m + end in cmd for m in marks for end in (b"/", b"\x00", b" "))]


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


def started_at(pid: int) -> str | None:
    """When a process started (Linux), to tell it from a later process that got the same pid."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def note_interrupt(state_dir, pid: int) -> None:
    """What a plan writes so the watchdog can interrupt it: its pid and, where the system says, when it started."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    (Path(state_dir) / "interrupt.pid").write_text(f"{pid} {started_at(pid) or ''}".strip())


def _noted(state_dir: Path) -> int | None:
    """The pid in interrupt.pid - if it is still the process that wrote it, not a later one that reused the pid."""
    try:
        pid, *start = (state_dir / "interrupt.pid").read_text().split()
        pid = int(pid)
    except (OSError, ValueError):
        return None
    now = started_at(pid)
    return None if start and now is not None and now != start[0] else pid


def end_processes(state_dir: Path, kill_group: int | None, interrupt_pid: int | None) -> None:
    interrupted = [p for p in {interrupt_pid, _noted(state_dir)} if p]
    for pid in interrupted:
        try:
            os.kill(pid, signal.SIGINT)             # a running plan stops and writes its report
        except (ProcessLookupError, PermissionError):
            pass
    end = time.time() + 30
    while time.time() < end and any(_alive(p) for p in interrupted):
        time.sleep(0.25)
    targets = ([kill_group] if kill_group else []) + [p for p in interrupted if _alive(p)] + _ours(state_dir)
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
    if not (math.isfinite(max_minutes) and max_minutes >= 0):
        _log(state_dir, f"max_minutes {max_minutes!r} is not a number of minutes: stopping at once")
        max_minutes = 0.0
    if idle_minutes is not None and not (math.isfinite(idle_minutes) and idle_minutes > 0):
        idle_minutes = None
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
    try:
        (state_dir / "watchdog_stopped").write_text(reason + "\n")
    except OSError:
        pass
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
