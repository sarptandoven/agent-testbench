"""Processes and locks on this machine, the same on Linux, macOS and Windows.

testbench starts work in the background (a run's driver, a session's kernel and supervisor, a step's command, a
web app), checks whether it is still there, and stops it with everything it started. POSIX does that with
sessions and process groups; Windows with process groups, console break events and `taskkill /T`. On Windows,
`os.kill(pid, 0)` is not a liveness check - it terminates the process - so nothing here uses it there.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path

WINDOWS = os.name == "nt"


def write_text_atomic(path, text: str, encoding: str = "utf-8") -> None:
    """Replace a small file (state.json) so a reader sees the old text or the new, never half: through a temp file of
    this process and thread's own, then os.replace. On Windows a file another process has open cannot be replaced
    for that moment, so it tries again briefly."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text, encoding=encoding)
    for attempt in range(200):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not WINDOWS or attempt == 199:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def read_text_retry(path) -> str:
    """Read a small file another process may be replacing at this moment (on Windows that is a PermissionError)."""
    for attempt in range(200):
        try:
            return Path(path).read_text(encoding="utf-8")
        except PermissionError:
            if not WINDOWS or attempt == 199:
                raise
            time.sleep(0.05)
    return Path(path).read_text(encoding="utf-8")


def group_kwargs() -> dict:
    """Popen arguments that start a process in a group of its own, so stop_group() reaches what it starts."""
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if WINDOWS else {"start_new_session": True}


def spawn(argv: list[str], *, cwd, log_path: Path, env: dict | None = None) -> subprocess.Popen:
    """Start a background process in its own group. A thread waits on it, so a long-lived caller (the MCP server)
    collects it when it exits instead of keeping a zombie that still looks alive."""
    log = open(log_path, "a")
    proc = subprocess.Popen(argv, cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env,
                            **group_kwargs())
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def alive(pid: int | None) -> bool:
    if not pid:
        return False
    if WINDOWS:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel32.CloseHandle(handle)
        return bool(ok) and code.value == 259                            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):          # gone (EPERM: an exited group on macOS)
        return False


def stop_group(pid: int | None, hard: bool = False) -> None:
    """Ask a process group started by spawn()/group_kwargs() to stop (SIGTERM; on Windows a console break), or end
    it at once with everything it started (hard)."""
    if not pid:
        return
    if WINDOWS:
        if not hard:
            try:
                os.kill(pid, signal.CTRL_BREAK_EVENT)
                return
            except OSError:                                # not on this console: there is only the hard way
                pass
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def stop_and_wait(pid: int | None, grace: float = 10, proc: subprocess.Popen | None = None) -> None:
    """Ask, wait up to `grace` seconds, then end it hard. proc: the Popen, when it is this process's child; it is
    waited on, so it is collected as soon as it exits (until then its pid still looks alive, for all of `grace`)."""
    if not pid:
        return
    stop_group(pid)
    end = time.time() + grace
    if proc is not None:
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    while time.time() < end and alive(pid):
        time.sleep(0.25)
    if alive(pid):
        stop_group(pid, hard=True)


def on_stop(handler) -> None:
    """Call handler when asked to stop: SIGTERM, and on Windows also the console break stop_group() sends."""
    signal.signal(signal.SIGTERM, handler)
    if WINDOWS:
        signal.signal(signal.SIGBREAK, handler)


@contextmanager
def exclusive(path: Path):
    """Hold an exclusive lock on a file while the block runs (other processes wait for it)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as f:
        if WINDOWS:
            import msvcrt
            f.seek(0)
            while True:
                try:
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)    # retries for about 10 s, then raises
                    break
                except OSError:
                    continue
            try:
                yield
            finally:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def shell(command: str) -> dict:
    """Popen arguments for a step's `run:` command: bash where there is one (on Windows, Git Bash's, not WSL's in
    System32, which is another machine), else the system shell."""
    if not WINDOWS:
        return {"args": command, "shell": True, "executable": "/bin/bash" if Path("/bin/bash").exists() else None}
    import shutil
    bash = shutil.which("bash")
    if bash and "system32" not in bash.lower():
        return {"args": [bash, "-c", command], "shell": False}
    return {"args": command, "shell": True}
