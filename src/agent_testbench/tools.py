"""agent-testbench's MCP tools for LLM APIs called directly: tool definitions for the Anthropic and OpenAI APIs, and
`call` to run the tool a model asked for.

    from agent_testbench import tools
    defs = tools.anthropic_tools(project_dir="path/to/project")     # or tools.openai_tools(...)
    out = tools.call(block.name, block.input, project_dir="path/to/project")   # str, or text + image blocks

The names, descriptions and argument schemas are read from the MCP server (mcp_server.py) when asked for, and a
call runs the MCP server's own tool function, so both always offer the same tools, behave the same and stay inside
the same budget. Needs the mcp package: pip install "agent-testbench[mcp]".
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

FORMATS = ("anthropic", "openai", "openai-responses", "text")


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
    if ask is not None:                                       # the person is asked, so the model's word is not needed
        arguments.pop("approved_by_person", None)
    server = _server()
    names = [t.name for t in server._tool_manager.list_tools()]
    if name not in names:
        return f"testbench: there is no tool {name!r}. Tools: {', '.join(names)}"
    try:
        out = await server._tool_manager.call_tool(name, arguments, context=_Context(ask, on_progress))
    except Exception as exc:                                  # bad arguments, mostly: the model reads it and retries
        return f"testbench: {exc}"
    return _convert(out, format)


def call(name: str, arguments: dict | str | None = None, *, project_dir: str | Path | None = None,
         format: str = "anthropic", ask=None, on_progress=None):
    """Run the tool a model asked for and return what the model should read: text (the CLI's output and exit code),
    or with images (plots, screenshots) a list of content blocks in the given format:

    - "anthropic": text and base64 image blocks, for a tool_result's content
    - "openai-responses": input_text and input_image items, for a function_call_output's output
    - "openai" (Chat Completions, whose tool messages take text only) and "text": text that names the image files

    project_dir runs it in that project whatever the model passed. ask(question) -> bool, if given, is how a person
    is asked about a cost above the approval threshold (like MCP elicitation), and then only the person approves:
    the model's approved_by_person is ignored. Without it the tool says what needs approval. on_progress(line) gets
    the progress lines of long runs. It works inside a running event loop too (Jupyter): the call then runs on a
    thread of its own."""
    coro = acall(name, arguments, project_dir=project_dir, format=format, ask=ask, on_progress=on_progress)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


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
    blocks = [{"type": "text", "text": text}] if format == "anthropic" else [{"type": "input_text", "text": text}]
    for img in images:
        data, mime = _image(img)
        b64 = base64.b64encode(data).decode()
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
                      if format == "anthropic" else {"type": "input_image", "image_url": f"data:{mime};base64,{b64}"})
    return blocks
