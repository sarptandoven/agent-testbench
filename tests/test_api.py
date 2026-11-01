"""The Python API and the LLM tool definitions, as an agent of your own uses them: a plan proved end to end, the
same refusals and approvals as the CLI, live sessions, the tool dispatcher with images, the tool definitions in the
Anthropic and OpenAI formats (valid JSON Schema, the MCP server's own tools), and the example agent loop run
against a stand-in client."""
import asyncio
import base64
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_testbench import api, backends, tools
from conftest import REPO, write_config


@pytest.fixture
def priced(tmp_path, plugin, monkeypatch) -> Path:
    """A project on the fake plugin backend ($6/h): a 4-minute plan's worst case is $0.50, above ask_above_usd."""
    site = plugin["PYTHONPATH"]
    monkeypatch.syspath_prepend(site)                      # this process prices it ...
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(p for p in (site, os.environ.get("PYTHONPATH")) if p))
    monkeypatch.setattr(backends, "_loaded", None)         # ... and the driver it starts runs it
    root = tmp_path / "priced"
    root.mkdir()
    write_config(root, {"project": "priced", "budget": {"max_run_usd": 5, "ask_above_usd": 0.1, "max_day_usd": 10},
                        "plans": {"on-fake": {"backend": "fake", "gpu": "FAKE1", "steps": [
                            {"name": "hello", "run": "echo hi", "max_minutes": 4}]}}})
    return root


def test_a_plan_end_to_end(plain):
    (plain / "sub").mkdir()
    tb = api.Testbench(plain / "sub")                      # any folder of the project finds its testbench.yaml
    assert tb.root == plain.resolve()
    assert tb.plans()["quick"]["backend"] == "local" and tb.plans()["quick"]["worst_case_usd"] == 0
    assert tb.estimate("quick")["allowed"]
    run = tb.run("quick", wait=0)
    assert run.wait(180).passed, run.report_md
    assert run.report["status"] == "passed" and "# PASS - plan `quick`" in run.report_md and run.cost_usd == 0
    assert [r.id for r in tb.runs()] == [run.id] and tb.get_run().id == run.id
    check = tb.verify(run.id)
    assert check["ok"] and "evidence files match" in check["message"], check
    next((run.dir / "steps").rglob("log.txt")).write_text("edited")
    check = run.verify()
    assert not check["ok"] and any("changed" in p for p in check["problems"]), check
    spent = tb.spend()
    assert spent["finished"] == 1 and spent["all_time_usd"] == 0 and spent["limits"]["max_run_usd"] == 2.0
    with pytest.raises(api.TestbenchError, match="No plan 'nope'"):
        tb.run("nope")


def test_refused_and_approval_the_same_as_the_cli(priced, machine_limits):
    tb = api.Testbench(priced)
    with pytest.raises(api.NeedsApproval) as asked:
        tb.run("on-fake")
    e = asked.value
    assert e.worst_case_usd == 0.5 and e.hardware == "fake FAKE1" and e.what == "plan on-fake"
    assert "Needs approval" in str(e) and not tb.runs(), "nothing launched"
    assert tb.run("on-fake", approve=True, wait=120).passed            # a person said yes
    machine_limits.mkdir(parents=True, exist_ok=True)
    (machine_limits / "limits.yaml").write_text("max_run_usd: 0.30\n")
    with pytest.raises(api.Refused) as refused:                         # approve=True never gets past a limit
        tb.run("on-fake", approve=True)
    assert not isinstance(refused.value, api.NeedsApproval) and "this machine's limit" in refused.value.reason
    assert tb.estimate("on-fake", approve=True)["allowed"] is False
    with pytest.raises(api.Refused, match="Refused"):
        tb.session("box", backend="fake", gpu="FAKE2", max_minutes=30, approve=True)
    assert len(tb.runs()) == 1 and tb.spend()["limits"]["max_run_usd"] == 0.3


def test_a_live_session(plain):
    pytest.importorskip("matplotlib")
    tb = api.Testbench(plain)
    with tb.session("work", idle_minutes=5, max_minutes=10) as s:
        assert s.status == "ready" and "work  ready on local" in s.describe()
        assert s.exec("x = 6").ok and s.exec("x * 7").result == "42"
        out = s.exec("import matplotlib.pyplot as plt\nplt.plot([3, 1, 2])\nplt.show()\n'plotted'")
        assert out.ok and out.images and all(p.is_file() for p in out.images) and "=> 'plotted'" in out.text
        bad = s.exec("1 / 0")
        assert not bad.ok and bad.error["ename"] == "ZeroDivisionError" and "ZeroDivisionError" in str(bad)
        cells = s.cells("nb.ipynb", "1-2", {"NAME": "grace"})
        assert [c.ok for c in cells] == [True, True] and "hello grace" in cells[1].stdout
        assert "nb.ipynb" in s.ls(".")
        with pytest.raises(api.TestbenchError, match="already running"):
            tb.session("work")
    assert s.status == "stopped" and [x.name for x in tb.sessions()] == ["work"]
    with pytest.raises(api.TestbenchError, match="is stopped"):
        s.exec("x")


def test_the_tool_dispatcher(plain):
    pytest.importorskip("matplotlib")
    assert "quick: local" in tools.call("plans", {}, project_dir=plain)
    assert "quick: local" in tools.call("plans", json.dumps({"project_dir": "/elsewhere"}), project_dir=plain)
    assert "no tool 'nope'" in tools.call("nope", {}) and "run_plan" in tools.call("nope", {})
    assert "plan" in tools.call("run_plan", {}, project_dir=plain) and "Field required" in tools.call("run_plan", {})

    async def inside_a_loop():                    # Jupyter runs cells inside an event loop
        return tools.call("session_start", {"name": "t"}, project_dir=plain)
    assert "ready on local" in asyncio.run(inside_a_loop())
    try:
        code = {"code": "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.show()\n'plotted'", "session": "t"}
        blocks = tools.call("session_exec", code, project_dir=plain)
        assert blocks[0]["type"] == "text" and "=> 'plotted'" in blocks[0]["text"]
        img = blocks[1]
        assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
        assert base64.b64decode(img["source"]["data"]).startswith(b"\x89PNG")
        items = tools.call("session_exec", code, project_dir=plain, format="openai-responses")
        assert items[0]["type"] == "input_text" and items[1]["image_url"].startswith("data:image/png;base64,")
        text = tools.call("session_exec", code, project_dir=plain, format="openai")
        assert isinstance(text, str) and "image: " in text and ".png" in text
    finally:
        assert "stopped after" in asyncio.run(tools.acall("session_stop", {"session": "t"}, project_dir=plain))


def test_a_person_approves_through_ask(priced):
    asked = []
    out = tools.call("run_plan", {"plan": "on-fake", "approved_by_person": True}, project_dir=priced,
                     ask=lambda q: asked.append(q) or False)
    assert len(asked) == 1 and "$0.50" in asked[0], "the model's approved_by_person is not the person's yes"
    assert "asked directly and said no" in out and "PASS" not in out
    lines = []
    out = tools.call("run_plan", {"plan": "on-fake"}, project_dir=priced, ask=lambda q: True, on_progress=lines.append)
    assert "approved this cost when asked" in out and "# PASS - plan `on-fake`" in out, out
    assert any("step 1" in line for line in lines), lines
    out = tools.call("run_plan", {"plan": "on-fake"}, project_dir=priced)
    assert "Needs approval" in out and "[exit 2]" in out


def test_tool_definitions_are_the_mcp_servers(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from agent_testbench.mcp_server import server
    listed = asyncio.run(server.list_tools())
    defs = tools.anthropic_tools()
    assert [d["name"] for d in defs] == [t.name for t in listed]
    assert [d["description"] for d in defs] == [t.description for t in listed]
    assert {"plans", "run_plan", "session_exec", "session_stop", "spend"} <= {d["name"] for d in defs}
    for d, t in zip(defs, listed):
        assert d["input_schema"] == (getattr(t, "input_schema", None) or t.inputSchema)
        jsonschema.Draft202012Validator.check_schema(d["input_schema"])
        assert set(d) == {"name", "description", "input_schema"} and d["input_schema"]["type"] == "object"
    bound = tools.anthropic_tools(project_dir=tmp_path)
    assert all("project_dir" not in d["input_schema"]["properties"] for d in bound)
    run_plan = next(d for d in bound if d["name"] == "run_plan")["input_schema"]
    jsonschema.validate({"plan": "smoke", "wait_seconds": 30}, run_plan)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"wait_seconds": 30}, run_plan)
    chat = tools.openai_tools(only=["plans", "run_plan"])
    assert [c["type"] for c in chat] == ["function"] * 2 and chat[1]["function"]["name"] == "run_plan"
    assert chat[1]["function"]["parameters"] == next(d for d in defs if d["name"] == "run_plan")["input_schema"]
    resp = tools.openai_tools(api="responses")
    assert resp[0] == {"type": "function", "name": "plans", "description": defs[0]["description"],
                       "parameters": defs[0]["input_schema"], "strict": False}


def test_a_tool_added_to_the_mcp_server_shows_up():
    from agent_testbench import mcp_server

    async def recipes(project_dir: str = ".") -> str:
        return "a recipe"
    mcp_server.server.add_tool(recipes, name="recipes_demo", description="Recipes for plans.")
    try:
        assert {"name": "recipes_demo", "description": "Recipes for plans."}.items() \
            <= next(d for d in tools.anthropic_tools() if d["name"] == "recipes_demo").items()
        assert tools.call("recipes_demo", {}) == "a recipe"
    finally:
        remove = getattr(mcp_server.server, "remove_tool", None)
        remove("recipes_demo") if remove else mcp_server.server._tool_manager._tools.pop("recipes_demo")


def test_importing_the_package_stays_light():
    code = ("import sys, agent_testbench as t; assert 'agent_testbench.api' not in sys.modules; "
            "assert 'agent_testbench.mcp_server' not in sys.modules; print(t.Testbench.__module__)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "agent_testbench.api", out.stderr


class FakeClient:
    """Stands in for anthropic.Anthropic(): answers with the scripted turns and keeps every request."""

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})     # as it was when sent
        stop, blocks = self.turns.pop(0)
        return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(**b) for b in blocks])


def test_the_example_agent_loop(plain):
    spec = importlib.util.spec_from_file_location("agent_loop", REPO / "examples" / "agent-loop" / "agent_loop.py")
    loop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loop)
    client = FakeClient([
        ("tool_use", [{"type": "text", "text": "Listing the plans."},
                      {"type": "tool_use", "id": "t1", "name": "plans", "input": {}}]),
        ("tool_use", [{"type": "tool_use", "id": "t2", "name": "run_plan", "input": {"plan": "quick"}}]),
        ("end_turn", [{"type": "text", "text": "The quick plan passed."}]),
    ])
    said = []
    final = loop.work(client, str(plain), "quick", ask=lambda q: False, say=said.append)
    assert final == "The quick plan passed." and len(client.requests) == 3
    first = client.requests[0]
    assert first["model"] == "claude-opus-5-5" and "Make the quick plan pass" in first["messages"][0]["content"]
    assert all("project_dir" not in t["input_schema"]["properties"] for t in first["tools"])
    results = client.requests[2]["messages"][-1]["content"]           # what the model read after run_plan
    assert results[0]["tool_use_id"] == "t2" and "# PASS - plan `quick`" in results[0]["content"]
    assert "quick: local" in client.requests[1]["messages"][-1]["content"][0]["content"]
    assert "-> run_plan {'plan': 'quick'}" in said
