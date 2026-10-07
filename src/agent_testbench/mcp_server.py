"""agent-testbench as MCP tools, for Claude Desktop, Claude Code, Cursor, Codex or any MCP client.

    testbench mcp            # stdio server; or: uvx --from "agent-testbench[mcp]" testbench mcp

Each tool is the CLI command of the same name, run in the given project folder.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import threading
from pathlib import Path

try:                                         # mcp >= 2
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:                          # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import cli

INSTRUCTIONS = (
    "agent-testbench runs a project's plans (notebooks, scripts, web UIs) locally, on Modal or on Colab within the budget "
    "in its testbench.yaml, and reports what passed, what failed and why. Work like an testbench: list plans, run the "
    "cheapest plan that can show a change works, read the report, fix one thing, rerun. Only set "
    "approved_by_person=true when the person explicitly agreed to that run's cost in this conversation."
)

server = _Server(name="agent-testbench", instructions=INSTRUCTIONS)
_lock = threading.Lock()


def _call(project_dir: str, argv: list[str]) -> str:
    """Run one CLI command in project_dir and return everything it printed."""
    with _lock:
        old = os.getcwd()
        out = io.StringIO()
        try:
            os.chdir(Path(project_dir).expanduser())
            a = cli.build_parser().parse_args(argv)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                try:
                    code = a.fn(a)
                except Exception as exc:
                    print(f"testbench: {exc}")
                    code = 2
        finally:
            os.chdir(old)
    return out.getvalue().strip() + f"\n[exit {code}]"


@server.tool(description="List the project's plans: backend, GPU, steps, and the worst-case cost of each.")
def plans(project_dir: str = ".") -> str:
    return _call(project_dir, ["plans"])


@server.tool(description="Run a plan. Waits up to wait_seconds and returns the report if it finished, otherwise the "
                         "run id to pass to wait_run. Refused if over budget; runs above the approval threshold need "
                         "approved_by_person=true, which you may set only after the person said yes to that cost.")
def run_plan(plan: str, project_dir: str = ".", approved_by_person: bool = False, wait_seconds: int = 120) -> str:
    argv = ["run", plan, "--wait", str(max(0, wait_seconds))] + (["--approve"] if approved_by_person else [])
    return _call(project_dir, argv)


@server.tool(description="Follow a run (default: the latest) for up to timeout_seconds; returns new progress and the "
                         "report once it ends.")
def wait_run(run: str = "", project_dir: str = ".", timeout_seconds: int = 300) -> str:
    return _call(project_dir, ["wait", *([run] if run else []), "--timeout", str(timeout_seconds)])


@server.tool(description="The report of a run (default: the latest): failures with cell, error and line, missed "
                         "expectations, evidence files, cost, and changes since the previous run of the same plan.")
def get_report(run: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["report", *([run] if run else [])])


@server.tool(description="Recent runs with status and cost.")
def list_runs(project_dir: str = ".", n: int = 10) -> str:
    return _call(project_dir, ["runs", "-n", str(n)])


@server.tool(description="Cancel a run (default: the latest), including its Modal call or Colab session.")
def cancel_run(run: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["cancel", *([run] if run else [])])


@server.tool(description="What runs have cost today and in total, against the project's budget.")
def spend(project_dir: str = ".") -> str:
    return _call(project_dir, ["spend"])


@server.tool(description="Check a notebook for Colab and headless pitfalls (big outputs, embedded video, secrets "
                         "without fallback, unpinned installs, settable form fields).")
def lint_notebook(notebook: str, project_dir: str = ".") -> str:
    return _call(project_dir, ["lint", notebook])


@server.tool(description="Add a line to the project's lab notebook (.testbench/NOTES.md): what was tried and learned.")
def add_note(text: str, project_dir: str = ".") -> str:
    return _call(project_dir, ["note", text])


@server.tool(description="Start a live session: a kernel that stays up on this machine (backend local), a Modal "
                         "machine with an optional GPU (modal: T4, L4, A10, L40S, A100-40GB, A100-80GB, H100, ...), or a "
                         "Colab VM (colab: T4, L4, A100, H100). It stops itself after idle_minutes without use and at "
                         "max_minutes, which bounds its cost. Over the approval threshold it needs approved_by_person=true, "
                         "which you may set only after the person agreed to that cost.")
def session_start(name: str = "main", backend: str = "", gpu: str = "", idle_minutes: float = 0, max_minutes: float = 0,
                  project_dir: str = ".", approved_by_person: bool = False) -> str:
    argv = ["session", "start", name] + (["--backend", backend] if backend else []) + (["--gpu", gpu] if gpu else []) \
        + (["--idle-minutes", str(idle_minutes)] if idle_minutes else []) + (["--max-minutes", str(max_minutes)] if max_minutes else []) \
        + (["--approve"] if approved_by_person else [])
    return _call(project_dir, argv)


@server.tool(description="Run Python in a live session's kernel - variables persist between calls, like notebook cells. "
                         "Pass code, or notebook (e.g. train.ipynb) with cells (e.g. '3-5') and params (form fields as a JSON "
                         "object string). Returns what it printed, the last value, saved image files, and errors with the "
                         "line that raised them. A call longer than timeout_seconds is interrupted; the kernel survives.")
def session_exec(code: str = "", notebook: str = "", cells: str = "", params: str = "", session: str = "",
                 timeout_seconds: float = 600, project_dir: str = ".") -> str:
    argv = ["exec"] + (["-s", session] if session else []) + ["--timeout", str(timeout_seconds)]
    if notebook:
        argv += ["--cells", f"{notebook}:{cells}" if cells else notebook] + (["--params", params] if params else [])
    else:
        argv += ["--", code]
    return _call(project_dir, argv)


@server.tool(description="Files on a live session's machine: op 'ls' (path), 'get' (remote path, optional local path; "
                         "default saves into the session's outputs folder) or 'put' (local path, remote path).")
def session_files(op: str, path: str = ".", second_path: str = "", session: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["files", op, path] + ([second_path] if second_path else []) + (["-s", session] if session else []))


@server.tool(description="Copy the project's files to a live session's machine again after editing them locally "
                         "(variables in the kernel are kept).")
def session_sync(session: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["session", "sync", *([session] if session else [])])


@server.tool(description="pip install packages on a live session's machine (space-separated, pin versions).")
def session_install(packages: str, session: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["session", "install", *packages.split(), *(["-s", session] if session else [])])


@server.tool(description="Run a shell command on a live session's machine (nvidia-smi, ls, df, ...).")
def session_shell(command: str, session: str = "", project_dir: str = ".") -> str:
    return _call(project_dir, ["session", "shell", command, *(["-s", session] if session else [])])


@server.tool(description="Live sessions with their hardware, uptime, idle time and cost so far.")
def session_list(project_dir: str = ".") -> str:
    return _call(project_dir, ["session", "list"])


@server.tool(description="Stop a live session (default: the only running one) and report what it cost. Stop sessions "
                         "as soon as you are done with them.")
def session_stop(session: str = "", project_dir: str = ".", all_sessions: bool = False) -> str:
    return _call(project_dir, ["session", "stop", *([session] if session else []), *(["--all"] if all_sessions else [])])


def main():
    server.run("stdio")


if __name__ == "__main__":
    main()
