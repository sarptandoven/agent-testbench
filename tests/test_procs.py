"""Processes and locks the same on Linux, macOS and Windows: liveness without killing, stopping a group, a lock
that serializes processes, and interrupting a kernel through its launcher (the way that also works on Windows)."""
import subprocess
import sys
import threading
import time

from agent_testbench import procs
from agent_testbench.kernelkit.client import execute


def test_alive_and_stop_group(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **procs.group_kwargs())
    assert procs.alive(proc.pid) and procs.alive(proc.pid), "checking twice must not end it"
    procs.stop_and_wait(proc.pid, grace=5)
    proc.wait(timeout=20)
    assert not procs.alive(proc.pid) and not procs.alive(None)


def test_exclusive_serializes_processes(tmp_path):
    lock, log = tmp_path / "x.lock", tmp_path / "log.txt"
    code = ("import time\nfrom agent_testbench.procs import exclusive\n"
            f"with exclusive({str(lock)!r}):\n"
            f"    open({str(log)!r}, 'a').write('in\\n'); time.sleep(1); open({str(log)!r}, 'a').write('out\\n')\n")
    ps = [subprocess.Popen([sys.executable, "-c", code]) for _ in range(3)]
    for p in ps:
        p.wait(timeout=60)
    assert log.read_text().split() == ["in", "out"] * 3, "never two inside at once"


def test_the_launcher_interrupts_its_kernel_on_request(tmp_path):
    state = tmp_path / "k"
    launcher = subprocess.Popen([sys.executable, "-m", "agent_testbench.kernelkit.launcher", "--workdir", str(tmp_path),
                                 "--state-dir", str(state)], stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                **procs.group_kwargs())
    try:
        for _ in range(240):
            if (state / "ready").exists():
                break
            time.sleep(0.25)
        out = {}
        t = threading.Thread(target=lambda: out.update(execute(str(state / "kernel.json"), "import time; time.sleep(60)",
                                                               timeout=120, out_dir=str(tmp_path))))
        t.start()
        time.sleep(3)
        (state / "interrupt").write_text("now")
        t.join(timeout=30)
        assert out.get("error", {}).get("ename") == "KeyboardInterrupt", out
        after = execute(str(state / "kernel.json"), "1 + 1", timeout=30, out_dir=str(tmp_path))
        assert after["result"] == "2", "the kernel survives the interrupt"
    finally:
        procs.stop_and_wait(launcher.pid)
