"""Start one IPython kernel and keep it until told to stop.

    python -m agent_testbench.kernelkit.launcher --workdir DIR --state-dir DIR [--shim] [--python PYTHON]

Writes STATE_DIR/kernel.json (the connection file), kernel.pid and ready. Exits when the kernel dies or on
SIGTERM (on Windows also a console break), shutting the kernel down. A file STATE_DIR/interrupt asks it to
interrupt the kernel - the way that works on every system, Windows included. With --shim, `google.colab` in the
kernel is the headless stand-in. With --python, the kernel runs on that Python (a project's virtualenv with ipykernel,
when testbench runs from uvx, uv tool or pipx) instead of the python3 kernel of the Python running this.
"""
import argparse
import os
import signal
import sys
import time
from pathlib import Path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--state-dir", required=True)
    ap.add_argument("--shim", action="store_true")
    ap.add_argument("--python")
    a = ap.parse_args(argv)
    state, workdir = Path(a.state_dir), Path(a.workdir)
    state.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    for name in ("ready", "kernel.pid", "kernel.json"):
        (state / name).unlink(missing_ok=True)
    from jupyter_client import KernelManager
    if a.python:
        from agent_testbench.notebook import only_spec, project_kernel
        km = KernelManager(kernel_name="python3", connection_file=str(state / "kernel.json"),
                           kernel_spec_manager=only_spec(project_kernel(a.python)))
    else:
        km = KernelManager(kernel_name="python3", connection_file=str(state / "kernel.json"))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    env.pop("MPLBACKEND", None)                         # the kernel's own plots render inline
    km.start_kernel(cwd=str(workdir), env=env)
    kc = km.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=120)
    if a.shim:
        from agent_testbench.executor import SHIM
        from agent_testbench.notebook import SHIM_BOOT
        kc.execute_interactive(SHIM_BOOT.format(shim=str(SHIM)), store_history=False, timeout=60)
    kc.stop_channels()
    pid = getattr(km.provisioner, "pid", None) or getattr(getattr(km.provisioner, "process", None), "pid", None)
    (state / "kernel.pid").write_text(str(pid or ""))
    (state / "ready").write_text(str(time.time()))
    print(f"[testbench] kernel ready in {workdir}", flush=True)

    from agent_testbench.procs import on_stop
    stopping = []
    on_stop(lambda *_: stopping.append(1))
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    request = state / "interrupt"
    try:
        while not stopping and km.is_alive():
            if request.exists():
                request.unlink(missing_ok=True)
                km.interrupt_kernel()
            time.sleep(0.2)
    finally:
        try:
            km.shutdown_kernel(now=True)
        except Exception:
            pass
        (state / "ready").unlink(missing_ok=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
