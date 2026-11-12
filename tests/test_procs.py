"""Processes and locks the same on Linux, macOS and Windows: liveness without killing, stopping a group, a lock
that serializes processes, and interrupting a kernel through its launcher (the way that also works on Windows)."""
import subprocess
import sys
import threading
import time

from agent_testbench import procs
from agent_testbench.kernelkit.client import execute

# Python code keeps running until interrupted; a C call such as time.sleep is not woken by an interrupt on
# Windows (CPython), so tests interrupt this instead
BUSY = "import time\nend = time.time() + 60\nwhile time.time() < end:\n    pass"


def test_alive_and_stop_group(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **procs.group_kwargs())
    assert procs.alive(proc.pid) and procs.alive(proc.pid), "checking twice must not end it"
    threading.Thread(target=proc.wait, daemon=True).start()     # collected when it ends, as spawn() does: a zombie
    procs.stop_and_wait(proc.pid, grace=5)                       # would look alive for all of the grace
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
    log = open(tmp_path / "launcher.log", "w")
    launcher = subprocess.Popen([sys.executable, "-m", "agent_testbench.kernelkit.launcher", "--workdir", str(tmp_path),
                                 "--state-dir", str(state)], stdout=log, stderr=subprocess.STDOUT, **procs.group_kwargs())
    try:
        for _ in range(240):
            if (state / "ready").exists():
                break
            time.sleep(0.25)
        out = {}
        t = threading.Thread(target=lambda: out.update(execute(str(state / "kernel.json"), BUSY,
                                                               timeout=120, out_dir=str(tmp_path))))
        t.start()
        time.sleep(3)
        (state / "interrupt").write_text("now")
        t.join(timeout=30)
        why = (f"result {out}; the launcher {'took' if not (state / 'interrupt').exists() else 'never took'} the "
               f"request; its log: {(tmp_path / 'launcher.log').read_text()[-1500:]}")
        assert out.get("error", {}).get("ename") == "KeyboardInterrupt", why
        after = execute(str(state / "kernel.json"), "1 + 1", timeout=30, out_dir=str(tmp_path))
        assert after["result"] == "2", "the kernel survives the interrupt"
    finally:
        procs.stop_and_wait(launcher.pid, proc=launcher)      # this process's child: waited on, not its grace


def test_state_files_survive_writers_and_readers_at_once(tmp_path):
    """state.json is written by a run's driver, the CLI and the MCP server at once: each write goes through a temp file
    of its own, and a reader never sees half a file (on Windows a busy file is tried again for a moment)."""
    import json
    import threading
    from agent_testbench import runs
    errors = []

    def writer(n):
        try:
            for i in range(40):
                runs.write_state(tmp_path, **{f"w{n}": i})
                json.loads(procs.read_text_retry(tmp_path / "state.json"))
        except Exception as exc:                       # noqa: BLE001 - any error fails the test below
            errors.append(exc)
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"


def test_two_processes_changing_one_run_state_lose_nothing(tmp_path):
    """The CLI writes a run's driver pid while the driver writes its status: each change is kept (an unlocked
    read-change-write lost one, and a run whose pid was lost read as one whose driver had died)."""
    from agent_testbench import runs
    runs.write_state(tmp_path, status="queued", created=time.time())
    code = ("import sys\nfrom agent_testbench import runs\n"
            "for i in range(150):\n    runs.write_state(sys.argv[1], **{sys.argv[2] + str(i): i})\n")
    procs_ = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path), who]) for who in ("cli", "driver")]
    assert all(p.wait(120) == 0 for p in procs_)
    s = runs.read_state(tmp_path)
    assert all(f"{who}{i}" in s for who in ("cli", "driver") for i in range(150)), "every change from both is there"
    assert not runs.driver_gone({**s, "status": "running"}), "no pid yet, just created: starting, not lost"
    assert runs.driver_gone({**s, "status": "running", "created": time.time() - 120}), "no pid for two minutes: lost"
