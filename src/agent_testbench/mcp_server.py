"""agent-testbench as MCP tools, for Claude Desktop, Claude Code, Cursor, Codex or any MCP client.

    testbench mcp                       # stdio server; or: uvx --from "agent-testbench[mcp]" testbench mcp
    testbench mcp --http [--port 8765]  # streamable HTTP on 127.0.0.1, for a client that connects to a URL

Each tool is the CLI command of the same name, run in its own process in the given project folder. Tools run side
by side: a session can be stopped, or a run checked, while a long exec or a run is still going. Plots and other
images come back as images the client can show. A launch above the approval threshold asks the person directly
(MCP elicitation) when the client supports it; otherwise it says what needs approval, as the CLI does.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, Field

try:                                         # mcp >= 2
    from mcp.server.mcpserver import Context, Image, MCPServer as _Server
except ImportError:                          # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _Server, Image

from . import config as C
from .cli import render_exec

INSTRUCTIONS = (
    "agent-testbench is a test bench you operate. Live sessions give you a kernel that stays up on this machine, a "
    "Modal GPU or a Colab VM: run code or notebook cells, inspect variables and the plots that come back as images, "
    "fix and rerun, move files - then stop the session. Plans run a project's notebooks, scripts or web UIs end to end "
    "and report what passed, what failed and why. Everything stays inside the budget in testbench.yaml and this "
    "machine's limits. Explore in a session, prove with a plan. Tools can run while others are still going (stop a "
    "session or check a run at any time). When a cost needs approval, the person may be asked directly; only set "
    "approved_by_person=true when the person explicitly agreed to that cost in this conversation."
)
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 2_000_000

server = _Server(name="agent-testbench", instructions=INSTRUCTIONS)


async def _cli(project_dir: str, argv: list[str], on_line=None) -> tuple[int, str]:
    """Run one CLI command in project_dir in its own process: (exit code, everything it printed). on_line, if given,
    gets each line as it is printed (progress for long runs)."""
    cwd = Path(project_dir).expanduser()
    if not cwd.is_dir():
        return 2, f"testbench: project_dir {cwd} is not a folder; pass the project's folder (it holds testbench.yaml)"
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_testbench", *argv, cwd=str(cwd),
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2 ** 26)
    lines = []
    try:
        while line := await proc.stdout.readline():
            lines.append(line.decode(errors="replace"))
            if on_line:
                await on_line(lines[-1].rstrip())
        code = await proc.wait()
    except asyncio.CancelledError:           # the client gave up on the call: stop following (a run keeps going)
        proc.terminate()
        raise
    return code, "".join(lines).strip()


def _text(code: int, out: str) -> str:
    return out + f"\n[exit {code}]"


def _images(paths: list) -> list:
    out = []
    for p in paths:
        p = Path(p)
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and p.is_file() and p.stat().st_size <= MAX_IMAGE_BYTES:
            out.append(Image(path=p))
        if len(out) == MAX_IMAGES:
            break
    return out


class Approval(BaseModel):
    approve: bool = Field(description="Yes: allow spending up to this amount")


def _can_ask(ctx) -> bool:
    if ctx is None:
        return False
    for read in (lambda: ctx.client_capabilities, lambda: ctx.session.client_params.capabilities):
        try:
            caps = read()
        except Exception:
            continue
        if caps is not None:
            return getattr(caps, "elicitation", None) is not None
    return False


async def _with_approval(ctx, project_dir: str, argv: list[str]) -> tuple[int, str]:
    """Run a launch; if it needs a person's approval, ask the person directly when the client can, and launch with
    --approve only on their yes. The agent is never the one saying yes here."""
    code, out = await _cli(project_dir, argv)
    if code != 2 or "Needs approval:" not in out or "--approve" in argv or not _can_ask(ctx):
        return code, out
    cost = out.split("Needs approval:", 1)[1].split("Ask a person", 1)[0].strip()
    try:
        answer = await ctx.elicit(message=f"agent-testbench: {cost} Approve spending up to that?", schema=Approval)
    except Exception as exc:
        return code, out + f"\n(The person could not be asked directly: {exc}.)"
    data = getattr(answer, "data", None)
    if getattr(answer, "action", None) == "accept" and data is not None and data.approve:
        code, again = await _cli(project_dir, argv + ["--approve"])
        return code, f"The person approved this cost when asked.\n{again}"
    how = "dismissed the question" if getattr(answer, "action", None) == "cancel" else "said no"
    return code, out + f"\nThe person was asked directly and {how}: do not launch this again without a new yes."


async def _follow(ctx, project_dir: str, run: str, seconds: int, head: str = "") -> str:
    """Follow a run with `testbench wait`, passing its progress lines on to the client as they come."""
    seen = 0

    async def progress(line: str):
        nonlocal seen
        seen += 1
        if ctx is not None and line:
            try:
                await ctx.report_progress(seen, None, line[:300])
            except Exception:
                pass

    code, out = await _cli(project_dir, ["wait", run, "--timeout", str(max(0, seconds))], on_line=progress)
    return _text(code, (head + "\n" if head else "") + out)


@server.tool(description="List the project's plans: backend, GPU, steps, and the worst-case cost of each.")
async def plans(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["plans"]))


@server.tool(description="Run a plan. Waits up to wait_seconds (passing on progress) and returns the report if it "
                         "finished, otherwise the run id to pass to wait_run. Refused if over budget. Above the approval "
                         "threshold the person is asked directly when the client supports it; otherwise it needs "
                         "approved_by_person=true, which you may set only after the person said yes to that cost.")
async def run_plan(plan: str, project_dir: str = ".", approved_by_person: bool = False, wait_seconds: int = 120,
                   ctx: Context = None) -> str:
    code, out = await _with_approval(ctx, project_dir, ["run", plan, "--detach"] + (["--approve"] if approved_by_person else []))
    started = re.search(r"\[testbench\] run (\S+) started", out)
    if code != 3 or not started:
        return _text(code, out)
    head = "\n".join(ln for ln in out.splitlines() if not ln.startswith("Follow it with"))
    return await _follow(ctx, project_dir, started.group(1), wait_seconds, head)


@server.tool(description="Follow a run (default: the latest) for up to timeout_seconds, passing on progress; returns "
                         "new progress and the report once it ends.")
async def wait_run(run: str = "", project_dir: str = ".", timeout_seconds: int = 300, ctx: Context = None) -> str:
    return await _follow(ctx, project_dir, run or "last", timeout_seconds)


@server.tool(description="The report of a run (default: the latest): failures with cell, error and line, missed "
                         "expectations, evidence files, cost, and changes since the previous run of the same plan. With "
                         "images=true (the default) the first evidence images (plots, screenshots) come back too - look "
                         "at them before calling a visual result good.")
async def get_report(run: str = "", project_dir: str = ".", images: bool = True):
    code, out = await _cli(project_dir, ["report", *([run] if run else [])])
    if code != 0 or not images:
        return _text(code, out)
    _, raw = await _cli(project_dir, ["report", *([run] if run else []), "--json"])
    try:
        rep = json.loads(raw)
        run_dir = C.find_root(Path(project_dir).expanduser()) / ".testbench" / "runs" / rep["run_id"]
    except Exception:
        return _text(code, out)
    paths = [run_dir / s["dir"] / name for s in rep.get("steps", []) if s.get("dir")
             for name in s.get("images", []) + (s.get("web") or {}).get("screenshots", [])]
    shown = _images(paths)
    return [_text(code, out), *shown] if shown else _text(code, out)


@server.tool(description="Recent runs with status and cost.")
async def list_runs(project_dir: str = ".", n: int = 10) -> str:
    return _text(*await _cli(project_dir, ["runs", "-n", str(n)]))


@server.tool(description="Cancel a run (default: the latest), including its Modal call or Colab session.")
async def cancel_run(run: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["cancel", *([run] if run else [])]))


@server.tool(description="What runs have cost today and in total, against the project's budget and this machine's limits.")
async def spend(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["spend"]))


@server.tool(description="Check a notebook for Colab and headless pitfalls (big outputs, embedded video, secrets "
                         "without fallback, unpinned installs, settable form fields).")
async def lint_notebook(notebook: str, project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["lint", notebook]))


@server.tool(description="Add a line to the project's lab notebook (.testbench/NOTES.md): what was tried and learned.")
async def add_note(text: str, project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["note", text]))


@server.tool(description="Start a live session: a kernel that stays up on this machine (backend local), a Modal "
                         "machine with an optional GPU (modal: T4, L4, A10, L40S, A100-40GB, A100-80GB, H100, ...), or a "
                         "Colab VM (colab: T4, L4, A100, H100). It stops itself after idle_minutes without use and at "
                         "max_minutes, which bounds its cost. Above the approval threshold the person is asked directly "
                         "when the client supports it; otherwise it needs approved_by_person=true, which you may set "
                         "only after the person agreed to that cost.")
async def session_start(name: str = "main", backend: str = "", gpu: str = "", idle_minutes: float = 0,
                        max_minutes: float = 0, project_dir: str = ".", approved_by_person: bool = False,
                        ctx: Context = None) -> str:
    argv = ["session", "start", name] + (["--backend", backend] if backend else []) + (["--gpu", gpu] if gpu else []) \
        + (["--idle-minutes", str(idle_minutes)] if idle_minutes else []) + (["--max-minutes", str(max_minutes)] if max_minutes else []) \
        + (["--approve"] if approved_by_person else [])
    return _text(*await _with_approval(ctx, project_dir, argv))


@server.tool(description="Run Python in a live session's kernel - variables persist between calls, like notebook cells. "
                         "Pass code, or notebook (e.g. train.ipynb) with cells (e.g. '3-5') and params (form fields as a JSON "
                         "object string). Returns what it printed, the last value, the images it displayed (as images), "
                         "and errors with the line that raised them; as_json=true returns the raw results instead. A call "
                         "longer than timeout_seconds is interrupted; the kernel survives. Other tools keep working "
                         "meanwhile (session_stop ends it).")
async def session_exec(code: str = "", notebook: str = "", cells: str = "", params: str = "", session: str = "",
                       timeout_seconds: float = 600, project_dir: str = ".", as_json: bool = False):
    argv = ["exec", "--json"] + (["-s", session] if session else []) + ["--timeout", str(timeout_seconds)]
    if notebook:
        argv += ["--cells", f"{notebook}:{cells}" if cells else notebook] + (["--params", params] if params else [])
    else:
        argv += ["--", code]
    status, out = await _cli(project_dir, argv)
    try:
        results = json.loads(out)
    except json.JSONDecodeError:
        return _text(status, out)                # refused, no session, a bad request: the message says which
    if as_json:
        return _text(status, out)
    shown = _images([p for r in results for p in r.get("images", [])])
    text = _text(status, "\n\n".join(render_exec(r) for r in results))
    return [text, *shown] if shown else text


@server.tool(description="Files on a live session's machine: op 'ls' (path), 'get' (remote path, optional local path; "
                         "default saves into the session's outputs folder) or 'put' (local path, remote path).")
async def session_files(op: str, path: str = ".", second_path: str = "", session: str = "", project_dir: str = ".") -> str:
    argv = ["files", op, path] + ([second_path] if second_path else []) + (["-s", session] if session else [])
    return _text(*await _cli(project_dir, argv))


@server.tool(description="Copy the project's files to a live session's machine again after editing them locally "
                         "(variables in the kernel are kept).")
async def session_sync(session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "sync", *([session] if session else [])]))


@server.tool(description="pip install packages on a live session's machine (space-separated, pin versions).")
async def session_install(packages: str, session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "install", *packages.split(), *(["-s", session] if session else [])]))


@server.tool(description="Run a shell command on a live session's machine (nvidia-smi, ls, df, ...).")
async def session_shell(command: str, session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "shell", command, *(["-s", session] if session else [])]))


@server.tool(description="Live sessions with their hardware, uptime, idle time and cost so far.")
async def session_list(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "list"]))


@server.tool(description="Stop a live session (default: the only running one) and report what it cost; it works while "
                         "a command is still running in it. Stop sessions as soon as you are done with them.")
async def session_stop(session: str = "", project_dir: str = ".", all_sessions: bool = False) -> str:
    argv = ["session", "stop", *([session] if session else []), *(["--all"] if all_sessions else [])]
    return _text(*await _cli(project_dir, argv))


def main(http: bool = False, host: str = "127.0.0.1", port: int = 8765):
    if not http:
        server.run("stdio")
        return
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[testbench] serving MCP on {host}:{port} with no authentication: anyone who can reach this port can "
              "run code and spend within your limits. Put it behind authentication, or serve on 127.0.0.1.",
              file=sys.stderr, flush=True)
    print(f"[testbench] MCP over streamable HTTP at http://{host}:{port}/mcp", file=sys.stderr, flush=True)
    try:
        server.run("streamable-http", host=host, port=port)            # mcp >= 2
    except TypeError:                                                   # mcp 1.x keeps them in settings
        server.settings.host, server.settings.port = host, port
        server.run("streamable-http")


if __name__ == "__main__":
    main()
