"""The MCP tools as agent frameworks' own tools - the OpenAI Agents SDK, LangChain / LangGraph and the Claude Agent
SDK - from the MCP server's tool table and through the same call path as tools.call: the definitions are the
server's (less approved_by_person, which a framework never offers), a call round-trips through each framework's tool
object (stand-ins always; the real package when it is installed), an argument that looks like an option stays a
value, and no model's word approves a cost: only the person, through ask."""
import asyncio
import base64
import dataclasses
import json
import os
import sys
import types
from pathlib import Path

import pytest

from agent_testbench import __version__, backends, mcp_server, tools
from conftest import PNG_1PX, write_config

PERSON_ONLY = tools.PERSON_ONLY


@pytest.fixture
def quick(tmp_path) -> Path:
    root = tmp_path / "quick"
    root.mkdir()
    write_config(root, {"plans": {"quick": "echo hi"}})
    return root


def test_the_definitions_are_the_mcp_servers_less_the_approval_argument(tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    listed = asyncio.run(mcp_server.server.list_tools())
    defs = tools.agent_definitions()
    assert [d["name"] for d in defs] == [t.name for t in listed]
    took_it = set()
    for d, t in zip(defs, listed):
        schema = json.loads(json.dumps(getattr(t, "input_schema", None) or t.inputSchema))
        if tools.APPROVAL in schema.get("properties", {}):
            took_it.add(t.name)
            schema["properties"].pop(tools.APPROVAL)                 # never required: it defaults to false
            assert d["description"] == t.description + PERSON_ONLY
        else:
            assert d["description"] == t.description
        assert d["input_schema"] == schema, t.name
        assert tools.APPROVAL not in json.dumps(d["input_schema"]), "never offered to the model"
        jsonschema.Draft202012Validator.check_schema(d["input_schema"])
    assert {"run_plan", "session_start", "loop_start"} <= took_it
    pinned = tools.agent_definitions(project_dir=tmp_path, only=["run_plan", "plans"])
    assert [d["name"] for d in pinned] == ["plans", "run_plan"]
    assert all("project_dir" not in d["input_schema"]["properties"] for d in pinned)
    assert tools.anthropic_tools(only=["run_plan"])[0]["input_schema"]["properties"][tools.APPROVAL], \
        "the Anthropic and OpenAI definitions are as before: the MCP server's own"


# Stand-ins with each framework's shape: what the real packages take and how they call a tool.

@dataclasses.dataclass
class FakeFunctionTool:                       # agents.FunctionTool
    name: str
    description: str
    params_json_schema: dict
    on_invoke_tool: object
    strict_json_schema: bool = True


class FakeStructuredTool:                     # langchain_core.tools.StructuredTool
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_sdks(monkeypatch):
    agents = types.SimpleNamespace(FunctionTool=FakeFunctionTool)
    lc_tools = types.ModuleType("langchain_core.tools")
    lc_tools.StructuredTool = FakeStructuredTool
    lc = types.ModuleType("langchain_core")
    lc.tools = lc_tools

    def tool(name, description, input_schema):
        return lambda handler: types.SimpleNamespace(name=name, description=description, input_schema=input_schema,
                                                     handler=handler)
    sdk = types.SimpleNamespace(tool=tool, create_sdk_mcp_server=lambda name, version, tools: {
        "type": "sdk", "name": name, "version": version, "instance": tools})
    for name, module in (("agents", agents), ("langchain_core", lc), ("langchain_core.tools", lc_tools),
                         ("claude_agent_sdk", sdk)):
        monkeypatch.setitem(sys.modules, name, module)


def framework_tools(kind: str, **kw) -> dict:
    """{name: a function that calls the tool as its framework does, with a dict of arguments}."""
    if kind == "openai-agents":
        return {t.name: (lambda args, t=t: asyncio.run(t.on_invoke_tool(None, json.dumps(args))))
                for t in tools.for_openai_agents(**kw)}
    if kind == "langchain-sync":
        return {t.name: (lambda args, t=t: t.func(**args)) for t in tools.for_langchain(**kw)}
    if kind == "langchain-async":
        return {t.name: (lambda args, t=t: asyncio.run(t.coroutine(**args))) for t in tools.for_langchain(**kw)}
    server = tools.for_claude_agent_sdk(**kw)
    return {t.name: (lambda args, t=t: asyncio.run(t.handler(args))) for t in server["instance"]}


KINDS = ["openai-agents", "langchain-sync", "langchain-async", "claude-agent-sdk"]


def text_of(out) -> str:
    if isinstance(out, dict):                                   # the Claude Agent SDK's {"content": [...]}
        out = out["content"]
    return out if isinstance(out, str) else "\n".join(b.get("text", "") for b in out)


@pytest.mark.parametrize("kind", KINDS)
def test_a_call_round_trips_through_each_frameworks_tool_object(quick, monkeypatch, kind):
    fake_sdks(monkeypatch)
    defs = {d["name"]: d for d in tools.agent_definitions(project_dir=quick)}
    if kind == "openai-agents":
        made = tools.for_openai_agents(project_dir=quick)
        assert [(t.name, t.description, t.params_json_schema, t.strict_json_schema) for t in made] == \
            [(d["name"], d["description"], d["input_schema"], False) for d in defs.values()]
    elif kind.startswith("langchain"):
        made = tools.for_langchain(project_dir=quick)
        assert [(t.name, t.description, t.args_schema) for t in made] == \
            [(d["name"], d["description"], d["input_schema"]) for d in defs.values()]
    else:
        made = tools.for_claude_agent_sdk(project_dir=quick, name="tb")
        assert made["name"] == "tb" and made["version"] == __version__
        assert [(t.name, t.description, t.input_schema) for t in made["instance"]] == \
            [(d["name"], d["description"], d["input_schema"]) for d in defs.values()]
    call = framework_tools(kind, project_dir=quick, only=["plans", "run_plan"])
    assert sorted(call) == ["plans", "run_plan"]
    assert "quick: local" in text_of(call["plans"]({"project_dir": "/elsewhere"})), "project_dir is the app's"
    out = text_of(call["run_plan"]({"plan": "quick", "wait_seconds": 60}))
    assert "# PASS - plan `quick`" in out and "[exit 0]" in out, out
    assert "Field required" in text_of(call["run_plan"]({})), "bad arguments come back for the model to read"


class Spy:
    """Stands in for the CLI behind the MCP tools: keeps each command line; a launch needs approval until --approve."""

    def __init__(self):
        self.argvs = []

    async def __call__(self, project_dir, argv, on_line=None, env=None):
        self.argvs.append(list(argv))
        if argv[0] in ("run", "session", "loop") and "--approve" not in argv:
            return 2, "Needs approval: plan smoke could cost up to $0.50 (above $0.10). Ask a person; only"
        return 0, "[testbench] run 20261112-000000-smoke started"


@pytest.mark.parametrize("kind", KINDS)
def test_an_option_stays_a_value_and_the_model_cannot_approve(tmp_path, monkeypatch, kind):
    fake_sdks(monkeypatch)
    spy = Spy()
    monkeypatch.setattr(mcp_server, "_cli", spy)
    call = framework_tools(kind, project_dir=tmp_path)
    out = text_of(call["run_plan"]({"plan": "--approve"}))
    assert "is not a name testbench uses" in out and spy.argvs == [], "refused before any command line"
    out = text_of(call["run_plan"]({"plan": "smoke", "approved_by_person": True, "wait_seconds": 0}))
    assert spy.argvs == [["run", "smoke", "--detach"]], "the model's approved_by_person is dropped"
    assert "Needs approval" in out and tools.NO_MODEL_APPROVAL.strip() in out
    assert mcp_server.APPROVAL_HINT.strip() not in out, "no advice to set an argument the model does not have"
    spy.argvs.clear()
    call["session_start"]({"backend": "--approve", "gpu": "T4 --approve", "approved_by_person": True})
    call["loop_start"]({"plan": "smoke", "metric": "--approve", "budget_usd": 1, "approved_by_person": True})
    assert spy.argvs[0][:3] == ["session", "start", "main"] and "--backend=--approve" in spy.argvs[0]
    assert "--gpu=T4 --approve" in spy.argvs[0] and "--metric=--approve" in spy.argvs[1]
    assert all("--approve" not in argv for argv in spy.argvs), spy.argvs
    # only the person, asked by the application, says yes
    asked = []
    spy.argvs.clear()
    call = framework_tools(kind, project_dir=tmp_path, ask=lambda q: asked.append(q) or False)
    out = text_of(call["run_plan"]({"plan": "smoke", "approved_by_person": True}))
    assert len(asked) == 1 and "$0.50" in asked[0] and "said no" in out
    assert all("--approve" not in argv for argv in spy.argvs), spy.argvs
    call = framework_tools(kind, project_dir=tmp_path, ask=lambda q: True)
    out = text_of(call["run_plan"]({"plan": "smoke", "wait_seconds": 0}))
    assert spy.argvs[-1] == ["run", "smoke", "--detach", "--approve"] and "approved this cost when asked" in out


def test_a_cost_that_needs_approval_is_refused_end_to_end(tmp_path, plugin, monkeypatch):
    for p in reversed(plugin["PYTHONPATH"].split(os.pathsep)):
        monkeypatch.syspath_prepend(p)
    monkeypatch.setenv("PYTHONPATH", plugin["PYTHONPATH"])
    monkeypatch.setattr(backends.KIND, "loaded", None)
    root = tmp_path / "priced"
    root.mkdir()
    write_config(root, {"budget": {"max_run_usd": 5, "ask_above_usd": 0.1}, "plans": {"on-fake": {
        "backend": "fake", "gpu": "FAKE1", "steps": [{"name": "hello", "run": "echo hi", "max_minutes": 4}]}}})
    fake_sdks(monkeypatch)
    call = framework_tools("openai-agents", project_dir=root)
    out = text_of(call["run_plan"]({"plan": "on-fake", "approved_by_person": True}))
    assert "Needs approval" in out and "[exit 2]" in out and tools.NO_MODEL_APPROVAL.strip() in out, out
    ledger = root / ".testbench" / "ledger.jsonl"
    assert not ledger.exists() or '"launch"' not in ledger.read_text(), "nothing launched on the model's word"


def test_images_come_back_in_each_frameworks_blocks(tmp_path):
    png = tmp_path / "plot.png"
    png.write_bytes(PNG_1PX)
    out = ["made a plot", mcp_server.Image(path=png)]
    b64 = base64.b64encode(PNG_1PX).decode()
    assert tools._convert(out, "openai-agents") == [{"type": "text", "text": "made a plot"},
                                                    {"type": "image", "image_url": f"data:image/png;base64,{b64}"}]
    assert tools._convert(out, "langchain")[1] == {"type": "image", "base64": b64, "mime_type": "image/png"}
    assert tools._convert(out, "mcp")[1] == {"type": "image", "data": b64, "mimeType": "image/png"}
    assert tools._convert(["just text"], "mcp") == "just text"


def test_a_missing_framework_says_what_to_install(monkeypatch):
    for name, fn, pip in (("agents", tools.for_openai_agents, "pip install openai-agents"),
                          ("langchain_core.tools", tools.for_langchain, "pip install langchain-core"),
                          ("claude_agent_sdk", tools.for_claude_agent_sdk, "pip install claude-agent-sdk")):
        monkeypatch.setitem(sys.modules, name, None)                     # import of it fails
        with pytest.raises(ImportError, match=pip):
            fn()
    monkeypatch.setitem(sys.modules, "agents", types.ModuleType("agents"))    # a package of the same name
    with pytest.raises(ImportError, match="is another package: pip install openai-agents"):
        tools.for_openai_agents()


# The real packages, when installed (pip install openai-agents langchain-core claude-agent-sdk).

def test_the_real_claude_agent_sdk_serves_the_tools(quick):
    sdk = pytest.importorskip("claude_agent_sdk")
    import mcp
    config = tools.for_claude_agent_sdk(project_dir=quick, only=["plans", "run_plan"])
    assert config["type"] == "sdk" and config["name"] == "testbench"
    defs = tools.agent_definitions(project_dir=quick, only=["plans", "run_plan"])
    assert isinstance(sdk.ClaudeAgentOptions(mcp_servers={"testbench": config}).mcp_servers["testbench"], dict)

    async def session():
        if hasattr(mcp, "Client"):                                   # mcp 2
            async with mcp.Client(config["instance"]) as client:
                return await talk(client)
        from mcp.shared.memory import create_connected_server_and_client_session
        async with create_connected_server_and_client_session(config["instance"]) as client:
            return await talk(client)

    async def talk(client):
        listed = (await client.list_tools()).tools
        assert [(t.name, t.description, getattr(t, "input_schema", None) or t.inputSchema) for t in listed] == \
            [(d["name"], d["description"], d["input_schema"]) for d in defs]
        return [await client.call_tool("run_plan", args) for args in (
            {"plan": "quick", "approved_by_person": True, "wait_seconds": 60}, {"plan": "--approve"}, {})]
    ran, injected, missing = asyncio.run(session())
    assert "# PASS - plan `quick`" in ran.content[0].text, ran
    assert "is not a name testbench uses" in injected.content[0].text
    assert getattr(missing, "is_error", getattr(missing, "isError", None)) is True, "the SDK checks the schema too"


def test_the_real_langchain_tools(quick):
    pytest.importorskip("langchain_core")
    from langchain_core.messages import ToolMessage
    from langchain_core.utils.function_calling import convert_to_openai_tool
    made = {t.name: t for t in tools.for_langchain(project_dir=quick, only=["plans", "run_plan", "session_start"])}
    schema = next(d for d in tools.agent_definitions(project_dir=quick) if d["name"] == "run_plan")["input_schema"]
    assert convert_to_openai_tool(made["run_plan"])["function"]["parameters"]["required"] == schema["required"]
    msg = made["run_plan"].invoke({"name": "run_plan", "args": {"plan": "quick", "approved_by_person": True},
                                   "id": "c1", "type": "tool_call"})
    assert isinstance(msg, ToolMessage) and msg.tool_call_id == "c1" and "# PASS - plan `quick`" in msg.content
    assert "is not a name" in asyncio.run(made["run_plan"].ainvoke({"plan": "--approve"}))
    assert "quick: local" in made["plans"].invoke({})


def test_the_real_openai_agents_runner_calls_the_tools(quick):
    agents = pytest.importorskip("agents")
    if not hasattr(agents, "Runner"):
        pytest.skip("the agents package here is not the OpenAI Agents SDK")
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
    agents.set_tracing_disabled(True)

    class Scripted(Model):                        # asks for run_plan, then answers with the tool's output
        def __init__(self):
            self.inputs = []

        async def get_response(self, *args, **kwargs):
            self.inputs.append(kwargs.get("input", args[1] if len(args) > 1 else None))
            if len(self.inputs) == 1:
                return ModelResponse(output=[ResponseFunctionToolCall(
                    type="function_call", call_id="c1", name="run_plan", id="fc1", status="completed",
                    arguments=json.dumps({"plan": "quick", "approved_by_person": True}))], usage=Usage(),
                    response_id=None)
            text = ResponseOutputText(type="output_text", text="proved", annotations=[])
            return ModelResponse(output=[ResponseOutputMessage(type="message", id="m1", role="assistant",
                                                               status="completed", content=[text])],
                                 usage=Usage(), response_id=None)

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    model = Scripted()
    agent = agents.Agent(name="prover", model=model, tools=tools.for_openai_agents(project_dir=quick,
                                                                                   only=["run_plan"]))
    result = asyncio.run(agents.Runner.run(agent, "prove the quick plan"))
    assert result.final_output == "proved"
    output = next(i for i in model.inputs[1] if isinstance(i, dict) and i.get("type") == "function_call_output")
    assert "# PASS - plan `quick`" in str(output["output"])
