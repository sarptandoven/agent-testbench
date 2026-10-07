"""Run code in a session's kernel and collect the evidence.

    python -m agent_testbench.kernelkit.client --connection kernel.json --request req.json --out-dir DIR

Prints one line `TESTBENCH_RESULT {json}`: status (ok | error | timeout), stdout, stderr, the value of the last
expression, the files of images it displayed, and for an error its name, message, the cell line that raised it
and the traceback. A cell that runs past its timeout is interrupted, so the kernel and its state survive.
"""
import argparse
import base64
import json
import os
import queue
import re
import signal
import time
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
CAP = 200_000


def _keep_tail(text: str) -> str:
    return text if len(text) <= CAP else "[... earlier output cut ...]\n" + text[-CAP:]


def _failing_line(tb: str):
    marks = re.findall(r"^-+>\s*(\d+)\s(.*)$", tb, re.M)
    if not marks:
        return None
    where = f"line {marks[0][0]}: {marks[0][1].strip()}"
    frames = re.findall(r"^File\s+(.+?):(\d+),\s+in\s+([\w.<>]+)", tb, re.M)
    if len(marks) > 1 and frames:
        path, line, fn = frames[-1]
        where += f" (raised in {Path(path).name}:{line}, {fn})"
    return where


def execute(connection_file: str, code: str, timeout: float = 600, out_dir: str = ".", prefix: str = "exec") -> dict:
    from jupyter_client import BlockingKernelClient
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    kc = BlockingKernelClient(connection_file=connection_file)
    kc.load_connection_file()
    kc.start_channels()
    began = time.time()
    res = {"status": "ok", "stdout": "", "stderr": "", "result": None, "displays": [], "images": [], "error": None,
           "execution_count": None, "seconds": 0.0}
    try:
        kc.wait_for_ready(timeout=60)
        msg_id = kc.execute(code, store_history=True, allow_stdin=False, stop_on_error=True)
        deadline = began + timeout
        interrupted = False
        while True:
            left = deadline - time.time()
            if left <= 0 and not interrupted:
                _interrupt(connection_file, kc)
                interrupted = True
                res["status"] = "timeout"
                deadline = time.time() + 30          # give the interrupt time to land
                continue
            if left <= 0 and interrupted:
                break
            try:
                msg = kc.get_iopub_msg(timeout=min(max(left, 0.1), 5))
            except queue.Empty:
                continue
            if msg.get("parent_header", {}).get("msg_id") != msg_id:
                continue
            kind, c = msg["msg_type"], msg["content"]
            if kind == "stream":
                res[c.get("name", "stdout") if c.get("name") in ("stdout", "stderr") else "stdout"] += ANSI.sub("", c.get("text", ""))
            elif kind in ("execute_result", "display_data"):
                data = c.get("data", {})
                saved = False
                for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg")):
                    if mime in data:
                        payload = data[mime]
                        path = out_dir / f"{prefix}-img{len(res['images']) + 1}.{ext}"
                        path.write_bytes(base64.b64decode(payload if isinstance(payload, str) else "".join(payload)))
                        res["images"].append(str(path))
                        saved = True
                        break
                text = data.get("text/plain")
                if kind == "execute_result":
                    res["execution_count"] = c.get("execution_count")
                    res["result"] = ANSI.sub("", text if isinstance(text, str) else "".join(text or ""))[:20000]
                elif text and not saved:
                    res["displays"].append(ANSI.sub("", text if isinstance(text, str) else "".join(text))[:5000])
            elif kind == "error":
                tb = ANSI.sub("", "\n".join(c.get("traceback", [])))
                if res["status"] != "timeout":
                    res["status"] = "error"
                res["error"] = {"ename": c.get("ename"), "evalue": ANSI.sub("", str(c.get("evalue")))[:4000],
                                "line": _failing_line(tb), "traceback_tail": "\n".join(tb.splitlines()[-25:])[-5000:]}
            elif kind == "status" and c.get("execution_state") == "idle":
                break
    finally:
        kc.stop_channels()
    res["stdout"], res["stderr"] = _keep_tail(res["stdout"]), _keep_tail(res["stderr"])
    res["seconds"] = round(time.time() - began, 2)
    return res


def _interrupt(connection_file: str, kc) -> None:
    pid_file = Path(connection_file).with_name("kernel.pid")
    pid = pid_file.read_text().strip() if pid_file.exists() else ""
    if pid.isdigit():
        try:
            os.kill(int(pid), signal.SIGINT)
            return
        except ProcessLookupError:
            pass
    try:                                              # kernels that take interrupts as messages
        kc.shell_channel.send(kc.session.msg("interrupt_request", {}))
    except Exception:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--connection", required=True)
    ap.add_argument("--request", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args(argv)
    req = json.loads(Path(a.request).read_text())
    res = execute(a.connection, req["code"], float(req.get("timeout", 600)), a.out_dir, req.get("prefix", "exec"))
    print("TESTBENCH_RESULT " + json.dumps(res), flush=True)


if __name__ == "__main__":
    main()
