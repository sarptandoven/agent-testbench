"""agent-testbench's MCP tools for LLM APIs called directly and for agent frameworks: tool definitions for the
Anthropic and OpenAI APIs, `call` to run the tool a model asked for, and the same tools as the OpenAI Agents SDK's,
LangChain's (and LangGraph's) and the Claude Agent SDK's own tool objects.

    from agent_testbench import tools
    defs = tools.anthropic_tools(project_dir="path/to/project")     # or tools.openai_tools(...)
    out = tools.call(block.name, block.input, project_dir="path/to/project")   # str, or text + image blocks

    tools.for_openai_agents(project_dir=...)      # [agents.FunctionTool]        Agent(tools=...)
    tools.for_langchain(project_dir=...)          # [StructuredTool]             create_agent / ToolNode(...)
    tools.for_claude_agent_sdk(project_dir=...)   # an in-process MCP server     ClaudeAgentOptions(mcp_servers=...)

The names, descriptions and argument schemas are read from the MCP server (mcp_server.py) when asked for, and a
call runs the MCP server's own tool function, so all of them always offer the same tools, behave the same and stay
inside the same budget: an argument that reaches the command line is a value, never an option. The framework tools
never offer the model approved_by_person and drop it if sent: there only a person approves a cost, when the
application asks them (ask=). Needs the mcp package: pip install "agent-testbench[mcp]".
"""
from __future__ import annotations

import asyncio
import base64
import copy
import inspect
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

FORMATS = ("anthropic", "openai", "openai-responses", "text", "openai-agents", "langchain", "mcp")
APPROVAL = "approved_by_person"              # the MCP tools' approval argument, which agent frameworks never offer
PERSON_ONLY = (f" In this tool set the model cannot approve a cost: {APPROVAL} is not offered, and is ignored if "
               "sent. A launch that needs approval is put to the person by the application, or says so.")
NO_MODEL_APPROVAL = ("\nHere only a person approves a cost, never the model: tell the person what it would cost. They "
                     "approve it when the application asks them, or run it themselves.")


def _server():
    try:
        from .mcp_server import server
    except ImportError as exc:
        raise ImportError(f"agent_testbench.tools reads its tools from the MCP server, which needs the mcp package "
                          f"({exc}): pip install \"agent-testbench[mcp]\"") from None
    return server


def _tools():
    return _server()._tool_manager.list_tools()            # the server's own table (mcp 1 and 2 both keep it here)


def definitions(project_dir: str | Path | None = None, only=None) -> list[dict]:
    """[{name, description, input_schema}] for every MCP tool (or those named in `only`). With project_dir, the
    tools work in that project and the model is not offered a project_dir argument."""
    out = []
    for t in _tools():
        if only is not None and t.name not in only:
            continue
        schema = copy.deepcopy(t.parameters)
        if project_dir is not None:
            schema.get("properties", {}).pop("project_dir", None)
            if "project_dir" in schema.get("required", []):
                schema["required"].remove("project_dir")
        out.append({"name": t.name, "description": t.description or "", "input_schema": schema})
    return out


def anthropic_tools(project_dir: str | Path | None = None, only=None) -> list[dict]:
    """Tool definitions for the Anthropic Messages API (`tools=`)."""
    return definitions(project_dir, only)


def openai_tools(project_dir: str | Path | None = None, only=None, api: str = "chat") -> list[dict]:
    """Function tools for OpenAI: api="chat" for Chat Completions, api="responses" for the Responses API. Not
    strict: the schemas have optional arguments with defaults."""
    if api not in ("chat", "responses"):
        raise ValueError('api is "chat" (Chat Completions) or "responses" (the Responses API)')
    defs = definitions(project_dir, only)
    if api == "responses":
        return [{"type": "function", "name": d["name"], "description": d["description"],
                 "parameters": d["input_schema"], "strict": False} for d in defs]
    return [{"type": "function", "function": {"name": d["name"], "description": d["description"],
                                              "parameters": d["input_schema"]}} for d in defs]


class _Context:
    """What the MCP tools ask of their context: the person to ask about a cost (ask), and progress (on_progress)."""

    def __init__(self, ask=None, on_progress=None):
        self.ask, self.on_progress = ask, on_progress
        self.client_capabilities = SimpleNamespace(elicitation=True) if ask else None

    async def elicit(self, message: str, schema=None):
        yes = await asyncio.to_thread(self.ask, message)
        if inspect.isawaitable(yes):
            yes = await yes
        return SimpleNamespace(action="accept" if yes else "decline", data=SimpleNamespace(approve=bool(yes)))

    async def report_progress(self, progress, total=None, message=None):
        if self.on_progress and message:
            self.on_progress(message)


async def acall(name: str, arguments: dict | str | None = None, *, project_dir: str | Path | None = None,
                format: str = "anthropic", ask=None, on_progress=None):
    """`call` for async code (an asyncio agent loop)."""
    return await _dispatch(name, arguments, project_dir, format, ask, on_progress, model_may_approve=True)


async def _dispatch(name: str, arguments, project_dir, format: str, ask, on_progress, model_may_approve: bool):
    """Every tool call, from call() and from each agent framework's tools: the MCP server's own tool function.
    model_may_approve=False (the frameworks): approved_by_person is dropped whatever the model sent, so only the
    person, through ask, says yes to a cost."""
    if format not in FORMATS:
        raise ValueError(f"format is one of {', '.join(FORMATS)}")
    if isinstance(arguments, str):                            # OpenAI sends the arguments as a JSON string
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError as exc:
            return f"testbench: the arguments for {name} are not valid JSON ({exc}); send a JSON object"
    if not isinstance(arguments or {}, dict):
        return f"testbench: the arguments for {name} must be a JSON object, not {type(arguments).__name__}"
    arguments = dict(arguments or {})
    if project_dir is not None:
        arguments["project_dir"] = str(project_dir)
    if ask is not None or not model_may_approve:              # the person is asked: the model's word is not needed
        arguments.pop(APPROVAL, None)
    server = _server()
    names = [t.name for t in server._tool_manager.list_tools()]
    if name not in names:
        return f"testbench: there is no tool {name!r}. Tools: {', '.join(names)}"
    try:
        out = await server._tool_manager.call_tool(name, arguments, context=_Context(ask, on_progress))
    except Exception as exc:                                  # bad arguments, mostly: the model reads it and retries
        return f"testbench: {exc}"
    if not model_may_approve:
        from .mcp_server import APPROVAL_HINT
        out = [x.replace(APPROVAL_HINT, NO_MODEL_APPROVAL) if isinstance(x, str) else x
               for x in (out if isinstance(out, (list, tuple)) else [out])]
    return _convert(out, format)


def _sync(coro):
    """Run a coroutine to its end from sync code, also inside a running event loop (Jupyter): on a thread of its own."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


def call(name: str, arguments: dict | str | None = None, *, project_dir: str | Path | None = None,
         format: str = "anthropic", ask=None, on_progress=None):
    """Run the tool a model asked for and return what the model should read: text (the CLI's output and exit code),
    or with images (plots, screenshots) a list of content blocks in the given format:

    - "anthropic": text and base64 image blocks, for a tool_result's content
    - "openai-responses": input_text and input_image items, for a function_call_output's output
    - "openai" (Chat Completions, whose tool messages take text only) and "text": text that names the image files
    - "openai-agents", "langchain", "mcp": the content blocks of the OpenAI Agents SDK (text, image with a data URL),
      LangChain (text, image with base64 and mime_type) and MCP (text, image with data and mimeType)

    project_dir runs it in that project whatever the model passed. ask(question) -> bool, if given, is how a person
    is asked about a cost above the approval threshold (like MCP elicitation), and then only the person approves:
    the model's approved_by_person is ignored. Without it the tool says what needs approval. on_progress(line) gets
    the progress lines of long runs. It works inside a running event loop too (Jupyter): the call then runs on a
    thread of its own."""
    return _sync(acall(name, arguments, project_dir=project_dir, format=format, ask=ask, on_progress=on_progress))


# Agent frameworks: the same tools as their own tool objects, each call through _dispatch with the model unable to
# approve (approved_by_person is not in the schema, is dropped if sent, and the description says so).

def agent_definitions(project_dir: str | Path | None = None, only=None) -> list[dict]:
    """definitions() as the agent frameworks offer them: the same names, descriptions and schemas, except that
    approved_by_person is never offered (and the descriptions of the tools that had it say so)."""
    out = []
    for d in definitions(project_dir, only):
        schema = d["input_schema"]
        if APPROVAL in schema.get("properties", {}):
            schema["properties"].pop(APPROVAL)
            if APPROVAL in schema.get("required", []):
                schema["required"].remove(APPROVAL)
            d["description"] += PERSON_ONLY
        out.append(d)
    return out


def _needs(module: str, what: str, pip: str):
    import importlib
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(f"agent_testbench.tools.{what} needs {pip.split()[-1]} ({exc}): {pip}") from None


def for_openai_agents(project_dir: str | Path | None = None, only=None, ask=None, on_progress=None) -> list:
    """The tools as the OpenAI Agents SDK's FunctionTools: Agent(name=..., tools=tools.for_openai_agents(...)).
    Each runs the MCP server's tool; images come back as the SDK's image outputs. Not strict: the schemas have
    optional arguments with defaults. ask(question) -> bool is how a person is asked about a cost (call())."""
    agents = _needs("agents", "for_openai_agents", "pip install openai-agents")
    if not hasattr(agents, "FunctionTool"):
        raise ImportError(f"agent_testbench.tools.for_openai_agents needs openai-agents, whose module is `agents`; the "
                          f"`agents` here ({getattr(agents, '__file__', '?')}) is another package: pip install "
                          "openai-agents")

    def invoke(name):
        async def on_invoke_tool(ctx, arguments: str):
            return await _dispatch(name, arguments, project_dir, "openai-agents", ask, on_progress, False)
        return on_invoke_tool
    return [agents.FunctionTool(name=d["name"], description=d["description"], params_json_schema=d["input_schema"],
                                on_invoke_tool=invoke(d["name"]), strict_json_schema=False)
            for d in agent_definitions(project_dir, only)]


def for_langchain(project_dir: str | Path | None = None, only=None, ask=None, on_progress=None) -> list:
    """The tools as LangChain StructuredTools, for LangChain agents and LangGraph (create_agent, ToolNode,
    model.bind_tools). Each takes the JSON schema of the MCP tool as args_schema and runs it, sync (invoke) or async
    (ainvoke); images come back as LangChain image content blocks. ask(question) -> bool as in call()."""
    lc_tools = _needs("langchain_core.tools", "for_langchain", "pip install langchain-core")

    def runners(name):
        async def arun(**arguments):
            return await _dispatch(name, arguments, project_dir, "langchain", ask, on_progress, False)

        def run(**arguments):
            return _sync(arun(**arguments))
        return run, arun
    out = []
    for d in agent_definitions(project_dir, only):
        run, arun = runners(d["name"])
        out.append(lc_tools.StructuredTool(name=d["name"], description=d["description"], args_schema=d["input_schema"],
                                           func=run, coroutine=arun))
    return out


def for_claude_agent_sdk(project_dir: str | Path | None = None, only=None, ask=None, on_progress=None,
                         name: str = "testbench"):
    """The tools as an in-process MCP server for the Claude Agent SDK:
    ClaudeAgentOptions(mcp_servers={"testbench": tools.for_claude_agent_sdk(...)}); Claude sees them as
    mcp__testbench__<tool>. The SDK does not pass MCP elicitation on, so a cost is put to the person through ask
    (question) -> bool, as in call(); without it the tool says what needs approval."""
    sdk = _needs("claude_agent_sdk", "for_claude_agent_sdk", "pip install claude-agent-sdk")
    from . import __version__

    def handler(tool_name):
        async def run(arguments):
            out = await _dispatch(tool_name, arguments, project_dir, "mcp", ask, on_progress, False)
            return {"content": out if isinstance(out, list) else [{"type": "text", "text": out}]}
        return run
    return sdk.create_sdk_mcp_server(name=name, version=__version__, tools=[
        sdk.tool(d["name"], d["description"], d["input_schema"])(handler(d["name"]))
        for d in agent_definitions(project_dir, only)])


def _image(img) -> tuple[bytes, str]:
    data = Path(img.path).read_bytes() if img.path else img.data
    return data, getattr(img, "_mime_type", None) or "image/png"


def _convert(out, format: str):
    from .mcp_server import Image
    items = out if isinstance(out, (list, tuple)) else [out]
    images = [x for x in items if isinstance(x, Image)]
    text = "\n".join(x if isinstance(x, str) else json.dumps(x) if isinstance(x, (dict, list)) else str(x)
                     for x in items if not isinstance(x, Image))
    if not images:
        return text
    if format in ("openai", "text"):
        named = [str(i.path) for i in images if i.path and str(i.path) not in text]
        return text + "".join(f"\nimage file: {p}" for p in named)
    blocks = [{"type": "input_text" if format == "openai-responses" else "text", "text": text}]
    for img in images:
        data, mime = _image(img)
        b64 = base64.b64encode(data).decode()
        blocks.append({
            "anthropic": {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
            "openai-responses": {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"},
            "openai-agents": {"type": "image", "image_url": f"data:{mime};base64,{b64}"},
            "langchain": {"type": "image", "base64": b64, "mime_type": mime},
            "mcp": {"type": "image", "data": b64, "mimeType": mime}}[format])
    return blocks
