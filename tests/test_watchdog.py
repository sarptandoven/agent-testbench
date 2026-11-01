"""The hard stop that lives on a session's machine: it ends the kernel at max_minutes or when idle, waits out a
running command, and asks Colab to release the VM - with nobody on this side to do it."""
import http.server
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_testbench.backends import colab_backend
from agent_testbench.kernelkit import watchdog
from agent_testbench.kernelkit.client import execute

FAST = {**os.environ, "TESTBENCH_WATCHDOG_SECONDS": "0.2"}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:                                            # a zombie (exited, not yet collected) counts as gone
        return "Z" not in subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return True


@pytest.fixture
def kernel(tmp_path):
    """A launcher and its kernel, started the way a session machine starts them (detached)."""
    state = tmp_path / "k"
    proc = subprocess.Popen([sys.executable, "-m", "agent_testbench.kernelkit.launcher", "--workdir", str(tmp_path),
                             "--state-dir", str(state)], start_new_session=True, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)
    for _ in range(240):
        if (state / "ready").exists():
            break
        time.sleep(0.25)
    assert (state / "ready").exists(), "the kernel did not start"
    yield state, proc
    if proc.poll() is None:
        os.killpg(proc.pid, 9)


def _stopped(state: Path, seconds: float) -> str:
    end = time.time() + seconds
    while time.time() < end:
        if (state / "watchdog_stopped").exists():
            return (state / "watchdog_stopped").read_text()
        time.sleep(0.2)
    return ""


def test_stops_the_kernel_at_max_minutes(kernel):
    state, launcher = kernel
    kernel_pid = int((state / "kernel.pid").read_text())
    watchdog.spawn(state, max_minutes=0.05, kill_group=launcher.pid, env=FAST)
    assert "reached max_minutes" in _stopped(state, 30)
    end = time.time() + 20
    while time.time() < end and (_alive(launcher.pid) or _alive(kernel_pid)):
        time.sleep(0.2)
    assert not _alive(launcher.pid) and not _alive(kernel_pid), "the launcher and its kernel must be gone"


def test_idle_waits_out_a_running_command(kernel):
    state, launcher = kernel
    watchdog.spawn(state, max_minutes=5, idle_minutes=0.05, env=FAST)        # 3 s idle; no --kill-group: found by
    res = execute(str(state / "kernel.json"), "import time; time.sleep(6); 'done'", timeout=60, out_dir=str(state))
    assert res["status"] == "ok" and res["result"] == "'done'", "a command longer than idle_minutes must finish"
    assert not (state / "watchdog_stopped").exists()
    assert "no command for" in _stopped(state, 30)
    end = time.time() + 20
    while time.time() < end and _alive(launcher.pid):
        time.sleep(0.2)
    assert not _alive(launcher.pid), "the session's processes are found by their state folder and ended"


def test_asks_colab_to_release_the_vm(tmp_path, monkeypatch):
    calls = []

    class RuntimeService(http.server.BaseHTTPRequestHandler):       # stands in for Colab's VM-local service
        def do_POST(self):
            calls.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), RuntimeService)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("TBE_RUNTIME_ADDR", f"127.0.0.1:{server.server_port}")
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.1")
    assert watchdog.can_release("colab")
    reason = watchdog.watch(tmp_path / "plan", max_minutes=0.002, release="colab")
    server.shutdown()
    assert "max_minutes" in reason and calls == ["/unassign"]
    assert "accepted" in (tmp_path / "plan" / "watchdog.log").read_text()


def test_without_the_runtime_service_it_still_ends_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("TBE_RUNTIME_ADDR", raising=False)
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.1")
    assert not watchdog.can_release("colab")
    (tmp_path / "plan").mkdir()
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    # collected as soon as it ends, as its parent on Colab would: a zombie still looks alive, and the watchdog would
    # wait out its 30 s for an interrupted plan and its 10 s after SIGTERM
    threading.Thread(target=victim.wait, daemon=True).start()
    (tmp_path / "plan" / "interrupt.pid").write_text(str(victim.pid))       # what a plan on Colab writes
    watchdog.watch(tmp_path / "plan", max_minutes=0.002, release="colab")
    assert victim.wait(timeout=10) != 0, "the plan's process is interrupted"
    assert "cannot release itself" in (tmp_path / "plan" / "watchdog.log").read_text()


def test_colab_starts_the_watchdog_before_anything_else(tmp_path):
    calls = []

    class Done:
        stdout = "TESTBENCH_WATCHDOG releases-vm\n"

    def fake_colab(*argv, timeout=0):
        calls.append(argv)
        if argv[0] == "exec":
            script = Path(argv[argv.index("-f") + 1]).read_text()
            compile(script, "watchdog_start", "exec")
            assert "MAX_MINUTES = 70.0" in script and "IDLE_MINUTES = 20" in script
        return Done()

    stop = colab_backend.start_watchdog(fake_colab, "s1", "/tmp/testbench/kernel", 70, 20)
    assert stop == "releases-vm" and "releases itself" in colab_backend.REMOTE_STOP[stop]
    assert [c[0] for c in calls] == ["upload", "exec"] and calls[0][3] == str(colab_backend.WATCHDOG)
    imports = [ln for ln in colab_backend.WATCHDOG.read_text().splitlines() if ln.startswith(("import ", "from "))]
    assert all(ln.split()[1].split(".")[0] in sys.stdlib_module_names for ln in imports), \
        f"the watchdog runs on the VM before the package is there, so standard library only: {imports}"


def test_a_reused_pid_is_left_alone(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    watchdog.note_interrupt(tmp_path, proc.pid)
    assert watchdog._noted(tmp_path) == proc.pid
    if watchdog.started_at(proc.pid) is not None:                 # Linux: a different start time is another process
        (tmp_path / "interrupt.pid").write_text(f"{proc.pid} 1")
        assert watchdog._noted(tmp_path) is None
    proc.kill()


def test_finds_the_sessions_processes_without_proc(tmp_path, monkeypatch):
    """macOS and BSD have no /proc: the watchdog asks ps instead, and still leaves other sessions alone."""
    ours, other = tmp_path / "k", tmp_path / "k2"
    sleep = "import time; time.sleep(30)"
    mine = subprocess.Popen([sys.executable, "-c", sleep, "--state-dir", str(ours)])
    theirs = subprocess.Popen([sys.executable, "-c", sleep, "--state-dir", str(other)])
    real = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: False if str(self) == "/proc" else real(self))
    try:
        time.sleep(0.3)
        found = watchdog._ours(ours)
        assert mine.pid in found and theirs.pid not in found
    finally:
        mine.kill()
        theirs.kill()
