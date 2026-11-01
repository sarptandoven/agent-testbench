"""The MCP server the way clients use it: tools side by side over stdio (stop a session while a long exec runs),
plots back as images, a cost above the threshold asked of the person directly, and streamable HTTP."""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

import pytest

from conftest import write_config


def content(out) -> list:
    """A tool call's content blocks, called in-process: a result object (mcp 2), a list, or (content, structured)."""
    out = getattr(out, "content", out)
    return list(out[0]) if isinstance(out, tuple) else list(out)


class Stdio:
    """A bare MCP client over stdio: send requests without waiting, note when each reply arrives."""

    def __init__(self, cwd, env=None):
        self.proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "mcp"], cwd=cwd, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env={**os.environ, **(env or {})})
        self.replies, self.arrived = {}, {}
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        self.wait(0, 60)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        for line in self.proc.stdout:
            msg = json.loads(line)
            if "id" in msg and ("result" in msg or "error" in msg):
                self.replies[msg["id"]], self.arrived[msg["id"]] = msg, time.time()

    def send(self, msg):
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def call(self, n, tool, args):
        self.send({"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": tool, "arguments": args}})

    def wait(self, n, seconds) -> dict:
        end = time.time() + seconds
        while n not in self.replies:
            assert time.time() < end, f"no reply to request {n} within {seconds} s"
            time.sleep(0.1)
        return self.replies[n]

    def text(self, n, seconds=60) -> str:
        reply = self.wait(n, seconds)
        return "\n".join(c.get("text", "") for c in reply.get("result", {}).get("content", [])) or json.dumps(reply)


def test_a_session_can_be_stopped_while_a_long_exec_runs(plain):
    c = Stdio(plain)
    try:
        c.call(1, "session_start", {"project_dir": str(plain)})
        assert "ready on local" in c.text(1, 120)
        began = time.time()
        c.call(2, "session_exec", {"code": "import time; time.sleep(60); 'too slow'", "project_dir": str(plain)})
        time.sleep(3)
        c.call(3, "session_list", {"project_dir": str(plain)})
        assert "main  ready on local" in c.text(3, 30) and 2 not in c.replies, "other tools answer during an exec"
        c.call(4, "session_stop", {"project_dir": str(plain)})
        assert "stopped after" in c.text(4, 30)
        assert "KernelGone" in c.text(2, 30) and time.time() - began < 40, "the exec ends when its session stops"
    finally:
        c.proc.kill()


def test_plots_come_back_as_images(plain):
    pytest.importorskip("matplotlib")
    from agent_testbench import mcp_server

    async def go():
        call = mcp_server.server.call_tool
        await call("session_start", {"project_dir": str(plain)})
        try:
            return await call("session_exec", {"code": "import matplotlib.pyplot as plt\nplt.plot([3, 1, 2])\nplt.show()\n"
                                                       "'plotted'", "project_dir": str(plain)})
        finally:
            await call("session_stop", {"project_dir": str(plain)})

    blocks = content(asyncio.run(go()))
    kinds = [getattr(c, "type", None) for c in blocks]
    assert kinds[0] == "text" and "image" in kinds, kinds
    assert "=> 'plotted'" in blocks[0].text and "image: " in blocks[0].text


def _priced(tmp_path):
    root = tmp_path / "priced"
    root.mkdir()
    write_config(root, {"project": "priced", "budget": {"max_run_usd": 5, "ask_above_usd": 0.1, "max_day_usd": 10},
                        "plans": {"on-fake": {"backend": "fake", "gpu": "FAKE1", "steps": [
                            {"name": "hello", "run": "echo hi", "max_minutes": 4}]}}})
    return root


def test_a_cost_above_the_threshold_is_asked_of_the_person(tmp_path, plugin):
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from mcp.types import ElicitResult
    root = _priced(tmp_path)

    async def run(answer: bool):
        asked = []

        async def person(context, params):                 # the client's dialog: the person answers, not the agent
            asked.append(params.message)
            return ElicitResult(action="accept", content={"approve": answer})

        server = StdioServerParameters(command=sys.executable, args=["-m", "agent_testbench", "mcp"], cwd=str(root),
                                       env={**os.environ, **plugin})
        async with stdio_client(server) as (read, write):
            async with ClientSession(read, write, elicitation_callback=person) as s:
                await s.initialize()
                out = await s.call_tool("run_plan", {"plan": "on-fake", "project_dir": str(root), "wait_seconds": 60})
        return asked, "\n".join(getattr(c, "text", "") for c in out.content)

    asked, text = asyncio.run(run(True))
    assert len(asked) == 1 and "$0.50" in asked[0] and "Approve spending" in asked[0]
    assert "approved this cost when asked" in text and "# PASS - plan `on-fake`" in text, text
    asked, text = asyncio.run(run(False))
    assert "asked directly and said no" in text and "PASS" not in text


class Person:
    """An MCP context whose client can ask the person: they answer `answer` after `delay` seconds (or it raises)."""

    def __init__(self, answer, delay=0.0):
        import types
        self.client_capabilities = types.SimpleNamespace(elicitation=object())
        self.answer, self.delay, self.asked = answer, delay, []

    async def elicit(self, message, schema):
        import types
        self.asked.append(message)
        await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        return types.SimpleNamespace(action="accept", data=schema(approve=self.answer))


def test_a_person_asked_directly_is_notified_only_if_they_do_not_answer(tmp_path, plugin, monkeypatch, webhook):
    from agent_testbench import backends, mcp_server
    root = _priced(tmp_path)
    (root / "testbench.yaml").write_text((root / "testbench.yaml").read_text() + f"notify: {{webhook: '{webhook.url}'}}\n")
    for p in reversed(plugin["PYTHONPATH"].split(os.pathsep)):
        monkeypatch.syspath_prepend(p)                     # the server reads testbench.yaml too, as the CLI does
    monkeypatch.setattr(backends, "_loaded", None)
    monkeypatch.setenv("PYTHONPATH", plugin["PYTHONPATH"])
    monkeypatch.setattr(mcp_server, "ASK_NOTICE_AFTER_S", 1.0)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))      # where the held notice waits
    launch = ["run", "on-fake", "--detach"]

    code, out = asyncio.run(mcp_server._with_approval(Person(False), str(root), launch))
    assert code == 2 and "asked directly and said no" in out, out
    assert not webhook.events(), "the person answered at once: there is nothing to tell them"

    person = Person(False, delay=4)
    code, out = asyncio.run(mcp_server._with_approval(person, str(root), launch))
    assert "said no" in out and len(person.asked) == 1
    [sent] = webhook.events("approval_needed")
    assert sent["text"].endswith("or no. The agent's client is asking you too.") and sent["plan"] == "on-fake", sent

    code, out = asyncio.run(mcp_server._with_approval(Person(RuntimeError("no dialog here")), str(root), launch))
    assert "could not be asked directly: no dialog here" in out
    assert len(webhook.events("approval_needed")) == 2 and webhook.events()[-1]["text"].endswith("or no.")
    assert not list(tmp_path.glob("testbench-approval-*")) and not webhook.events("run_end"), "nothing ran"


def test_without_elicitation_it_says_what_needs_approval(tmp_path, plugin, monkeypatch):
    from agent_testbench import mcp_server
    root = _priced(tmp_path)
    monkeypatch.setenv("PYTHONPATH", plugin["PYTHONPATH"])
    out = asyncio.run(mcp_server.server.call_tool("run_plan", {"plan": "on-fake", "project_dir": str(root)}))
    text = " ".join(getattr(c, "text", "") for c in content(out))
    assert "Needs approval" in text and "--approve" in text and "[exit 2]" in text


def _post(url, body, session=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session:
        headers |= {"mcp-session-id": session, "mcp-protocol-version": "2025-06-18"}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        raw, sid = r.read().decode(), r.headers.get("mcp-session-id")
    if not raw.strip():
        return None, sid
    data = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    return json.loads(data[-1] if data else raw), sid


def test_mcp_over_http(plain):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "mcp", "--http", "--port", str(port)], cwd=plain,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        reply, sid = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}})
        assert reply["result"]["serverInfo"]["name"] == "agent-testbench"
        _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
        reply, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "plans", "arguments": {"project_dir": str(plain)}}}, sid)
        assert "quick: local" in reply["result"]["content"][0]["text"]
    finally:
        proc.kill()
