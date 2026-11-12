"""agent-testbench as MCP tools, for Claude Desktop, Claude Code, Cursor, Codex or any MCP client.

    testbench mcp                       # stdio server; or: uvx --from "agent-testbench[mcp]" testbench mcp
    testbench mcp --http [--port 8765]  # streamable HTTP on 127.0.0.1, for a client that connects to a URL

Each tool is the CLI command of the same name, run in its own process in the given project folder. Tools run side
by side: a session can be stopped, or a run checked, while a long exec or a run is still going. Plots and other
images come back as images the client can show. A launch above the approval threshold asks the person directly
(MCP elicitation) when the client supports it; otherwise it says what needs approval, as the CLI does.

Resources (testbench://plans, testbench://runs, a run's report and evidence, the loop, the notes) and prompts
(prove-this-change, find-why-this-fails, tune-with-a-loop, check-a-strategy) are in mcp_resources.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

try:                                         # mcp >= 2
    from mcp.server.mcpserver import Context, Image, MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:                          # mcp 1.x
    from mcp.server.fastmcp import Context, FastMCP as _Server, Image
    from mcp.server.fastmcp.exceptions import ToolError

from . import __version__, config as C, mcp_resources, notify
from .cli import render_exec

INSTRUCTIONS = (
    "agent-testbench is a test bench you operate. Live sessions give you a kernel that stays up on this machine, a "
    "Modal GPU or a Colab VM: run code or notebook cells, inspect variables and the plots that come back as images, "
    "fix and rerun, move files - then stop the session. Plans run a project's notebooks, scripts or web UIs end to end "
    "and report what passed, what failed and why. Everything stays inside the budget in testbench.yaml and this "
    "machine's limits. Explore in a session, prove with a plan. Tools can run while others are still going (stop a "
    "session or check a run at any time). When a cost needs approval, the person may be asked directly; only set "
    "approved_by_person=true when the person explicitly agreed to that cost in this conversation. Resources read the "
    "project the server started in without running anything: testbench://plans, testbench://runs, "
    "testbench://runs/RUN_ID/report, its report.json and evidence/PATH, testbench://loops/current, testbench://notes."
)
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 2_000_000
APPROVAL_HINT = ("\nThrough this server the approval is approved_by_person=true on this tool: set it only after the "
                 "person said yes to this cost.")     # tools.py puts its own words in its place for agent frameworks
ASK_NOTICE_AFTER_S = 60.0     # a person asked directly who has not answered by then may be away: notify: tells them

try:                                         # the version clients see in initialize (mcp 2 takes it here)
    server = _Server(name="agent-testbench", instructions=INSTRUCTIONS, version=__version__)
except Exception:                            # mcp 1.x
    server = _Server(name="agent-testbench", instructions=INSTRUCTIONS)
if getattr(getattr(server, "_mcp_server", None), "version", None) in (None, ""):
    try:
        server._mcp_server.version = __version__
    except Exception:
        pass


async def _cli(project_dir: str, argv: list[str], on_line=None, env: dict | None = None) -> tuple[int, str]:
    """Run one CLI command in project_dir in its own process: (exit code, everything it printed). on_line, if given,
    gets each line as it is printed (progress for long runs); env, variables to add to its environment."""
    cwd = Path(project_dir).expanduser()
    if not cwd.is_dir():
        return 2, f"testbench: project_dir {cwd} is not a folder; pass the project's folder (it holds testbench.yaml)"
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "agent_testbench", *argv, cwd=str(cwd),
                                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, limit=2 ** 26,
                                                env={**os.environ, **env} if env else None)
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
    --approve only on their yes. The agent is never the one saying yes here. The approval_needed notification
    (notify:) waits ASK_NOTICE_AFTER_S for that answer: it goes out only if the person has not answered by then,
    or could not be asked."""
    if "--approve" in argv or not _can_ask(ctx):
        code, out = await _cli(project_dir, argv)
        if code == 2 and "Needs approval:" in out and "--approve" not in argv:     # the CLI's words, as this tool
            out += APPROVAL_HINT
        return code, out
    fd, name = tempfile.mkstemp(prefix="testbench-approval-", suffix=".json")
    os.close(fd)
    held = Path(name)
    try:
        code, out = await _cli(project_dir, argv, env={notify.HOLD_ENV: str(held)})
        if code != 2 or "Needs approval:" not in out:
            return code, out
        cost = out.split("Needs approval:", 1)[1].split("Ask a person", 1)[0].strip()
        told, ask = False, None
        try:
            ask = asyncio.ensure_future(ctx.elicit(message=f"agent-testbench: {cost} Approve spending up to that?",
                                                   schema=Approval))
            done, _ = await asyncio.wait({ask}, timeout=ASK_NOTICE_AFTER_S)
            if not done:                                 # nobody at the screen: tell them where they will see it
                told = True
                await asyncio.to_thread(notify.send_held, held, "The agent's client is asking you too.")
            answer = await ask
        except Exception as exc:
            if not told:
                await asyncio.to_thread(notify.send_held, held)
            return code, out + f"\n(The person could not be asked directly: {exc}.)"
        finally:
            if ask is not None and not ask.done():       # the tool call itself was cancelled
                ask.cancel()
    finally:
        held.unlink(missing_ok=True)
    data = getattr(answer, "data", None)
    if getattr(answer, "action", None) == "accept" and data is not None and data.approve:
        code, again = await _cli(project_dir, argv + ["--approve"])
        return code, f"The person approved this cost when asked.\n{again}"
    how = "dismissed the question" if getattr(answer, "action", None) == "cancel" else "said no"
    return code, out + f"\nThe person was asked directly and {how}: do not launch this again without a new yes."


def _progress(ctx):
    """A line handler that passes each line on to the client as MCP progress."""
    seen = 0

    async def progress(line: str):
        nonlocal seen
        seen += 1
        if ctx is not None and line:
            try:
                await ctx.report_progress(seen, None, line[:300])
            except Exception:
                pass
    return progress


class BadName(ToolError, ValueError):
    """A tool argument refused as a name. A ToolError, so the client is told why (mcp 2 hides any other error's text)."""


def _name(value: str, what: str) -> str:
    """A plan, run or session name as a tool argument. Never an option: `--approve` as a name must not approve."""
    if value and not re.fullmatch(r"[A-Za-z0-9][\w.:-]{0,80}", value):
        raise BadName(f"{what} {value!r} is not a name testbench uses (letters, digits, - _ . : and no leading -)")
    return value


async def _follow(ctx, project_dir: str, run: str, seconds: int, head: str = "") -> str:
    """Follow a run with `testbench wait`, passing its progress lines on to the client as they come."""
    code, out = await _cli(project_dir, ["wait", _name(run, "run"), "--timeout", str(max(0, seconds))],
                           on_line=_progress(ctx))
    if code in (0, 1):                           # it has ended: the runs list and its report are new
        await mcp_resources.updated(ctx, project_dir, run)
    return _text(code, (head + "\n" if head else "") + out)


@server.tool(description="List the project's plans: backend, GPU, steps, and the worst-case cost of each.")
async def plans(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["plans"]))


@server.tool(description="Check a project in one call, with or without a testbench.yaml - a good first call in a "
                         "project new to you. With a testbench.yaml: every plan that is free and needs no approval runs "
                         "now, and each other plan is listed with its worst case and the command a person runs for it; "
                         "nothing that costs money starts. With none: what `testbench init` would set up there - a test "
                         "suite (pytest, npm test, go test, cargo), notebooks, a web app - runs on this machine for "
                         "free, as ordinary runs with proof records (get_report, diff_runs and the rest work there "
                         "afterwards). Returns a line per plan, the report of each failure, the run ids and the next "
                         "step; a plan whose tool is missing here is skipped with the command that installs it. plans: "
                         "only these plan names. as_json=true returns the summary as data.")
async def check_project(plans: list[str] | None = None, project_dir: str = ".", as_json: bool = False,
                        ctx: Context = None) -> str:
    argv = ["check", *(f"--plan={_name(p, 'plan')}" for p in plans or []), *(["--json"] if as_json else [])]
    return _text(*await _cli(project_dir, argv, on_line=_progress(ctx)))


@server.tool(description="Recipes: proven starting points for testbench.yaml by goal (pytest, notebook-colab, "
                         "pytorch-train, hf-finetune, web-app, llm-eval, docker-sandbox, ssh-gpu-box, data-pipeline), "
                         "each a ladder of plans - free here first, a short check on the target hardware, then the full "
                         "run - with checks that fail when the result is wrong. With no name: what each is for, its "
                         "backends and each plan's worst case at the project's prices. With a name: the recipe itself, "
                         "YAML with FILL IN lines to adapt. Start from one when the person's goal matches it: "
                         "`testbench init --recipe NAME`, or `testbench init --add --recipe NAME` for an existing "
                         "testbench.yaml.")
async def recipes(name: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["recipes", *([_name(name, "recipe")] if name else [])]))


@server.tool(description="Run a plan. Waits up to wait_seconds (passing on progress) and returns the report if it "
                         "finished, otherwise the run id to pass to wait_run. Refused if over budget. Above the approval "
                         "threshold the person is asked directly when the client supports it; otherwise it needs "
                         "approved_by_person=true, which you may set only after the person said yes to that cost.")
async def run_plan(plan: str, project_dir: str = ".", approved_by_person: bool = False, wait_seconds: int = 120,
                   ctx: Context = None) -> str:
    code, out = await _with_approval(ctx, project_dir, ["run", _name(plan, "plan"), "--detach"] + (["--approve"] if approved_by_person else []))
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
    code, out = await _cli(project_dir, ["report", *([_name(run, "run")] if run else [])])
    if code != 0 or not images:
        return _text(code, out)
    _, raw = await _cli(project_dir, ["report", *([run] if run else []), "--json"])
    try:
        rep = json.loads(raw)
        run_dir = C.find_root(Path(project_dir).expanduser()) / ".testbench" / "runs" / rep["run_id"]
        if Path(rep["run_id"]).name != rep["run_id"] or rep["run_id"] in (".", ".."):
            raise ValueError("a run id is a folder name")
    except Exception:
        return _text(code, out)
    from .report import inside
    paths = [run_dir / s["dir"] / name for s in rep.get("steps", []) if s.get("dir")
             for name in s.get("images", []) + (s.get("web") or {}).get("screenshots", [])]
    shown = _images([p for p in paths if inside(run_dir, p)])      # only the run's files, whatever its report names
    return [_text(code, out), *shown] if shown else _text(code, out)


def _run_ref(value: str) -> str:
    """A run as a tool argument: a name, or a name with ~N (last~1: the run before the latest). Never an option."""
    base, tilde, back = value.partition("~")
    if tilde and (not base or not back.isdigit()):
        raise ValueError(f"run {value!r} is not a run testbench knows (a run id, a prefix, last, or last~N)")
    _name(base, "run")
    return value


@server.tool(description="What changed between two runs, in plain words: the verdict and each step, every checked "
                         "number (old -> new, by how much, and better or worse by the check's rule, e.g. '>= 0.9' "
                         "wants higher), checks that newly pass or fail, evidence files added, removed or changed "
                         "(sha256), cost and minutes, the code (commits, uncommitted changes, git diff --stat between "
                         "the commits) and where it ran (Python, platform, packages that changed version). a and b are "
                         "runs, older first: ids, prefixes, 'last' or 'last~N'. A plan name as a alone compares the "
                         "plan's last two finished runs; a run alone, it and the run of its plan before it; nothing, "
                         "the latest run and the one before it. as_json=true returns the comparison as data.")
async def diff_runs(a: str = "", b: str = "", project_dir: str = ".", as_json: bool = False) -> str:
    refs = [_run_ref(x) for x in (a, b) if x]
    return _text(*await _cli(project_dir, ["diff", *(["--json"] if as_json else []), "--", *refs]))


@server.tool(description="Export a run (default: the latest; 'all' for every finished run) where people track "
                         "experiments: to is mlflow, wandb, csv or jsonl (default: every target in testbench.yaml's "
                         "export: section). It carries the plan, status, cost, commit, params, every checked number "
                         "and the evidence; exporting a run again updates it instead of adding a duplicate.")
async def export_run(run: str = "", to: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["export", *([_name(run, "run")] if run else []), *([f"--to={to}"] if to else [])]))


@server.tool(description="Recent runs with status and cost.")
async def list_runs(project_dir: str = ".", n: int = 10) -> str:
    return _text(*await _cli(project_dir, ["runs", "-n", str(n)]))


@server.tool(description="Cancel a run (default: the latest), including its Modal call or Colab session.")
async def cancel_run(run: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["cancel", *([_name(run, "run")] if run else [])]))


@server.tool(description="What runs have cost today and in total, against the project's budget and this machine's limits.")
async def spend(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["spend"]))


@server.tool(description="Check a notebook (.ipynb or percent-format .py) for Colab and headless pitfalls (big outputs, "
                         "embedded video, secrets without fallback, unpinned installs, settable form fields and parameters).")
async def lint_notebook(notebook: str, project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["lint", "--", notebook]))


@server.tool(description="Find out why a notebook (.ipynb or percent-format .py) does not run, and what to do. Reads "
                         "it (cells run out of order, names used before the cell that makes them, missing packages with "
                         "their pip names, Colab-only parts, files it reads that are not there, input()), then runs it "
                         "top to bottom in a fresh kernel "
                         "on this machine within timeout_seconds and diagnoses the first failing cell. Returns the "
                         "diagnosis and a plan to paste into testbench.yaml. fix_to writes a copy with mechanical fixes "
                         "only (never the original); run=false only reads it.")
async def rescue_notebook(notebook: str, project_dir: str = ".", timeout_seconds: float = 120, fix_to: str = "",
                          run: bool = True, as_json: bool = False) -> str:
    argv = ["rescue", "--timeout", str(timeout_seconds)] + ([f"--fix={fix_to}"] if fix_to else []) \
        + ([] if run else ["--no-run"]) + (["--json"] if as_json else []) + ["--", notebook]
    return _text(*await _cli(project_dir, argv))


@server.tool(description="Add a line to the project's lab notebook (.testbench/NOTES.md): what was tried and learned.")
async def add_note(text: str, project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["note", "--", text]))


@server.tool(description="Start a live session: a kernel that stays up on this machine (backend local), a Modal "
                         "machine with an optional GPU (modal: T4, L4, A10, L40S, A100-40GB, A100-80GB, H100, ...), or a "
                         "Colab VM (colab: T4, L4, A100, H100). It stops itself after idle_minutes without use and at "
                         "max_minutes, which bounds its cost. Above the approval threshold the person is asked directly "
                         "when the client supports it; otherwise it needs approved_by_person=true, which you may set "
                         "only after the person agreed to that cost.")
async def session_start(name: str = "main", backend: str = "", gpu: str = "", idle_minutes: float = 0,
                        max_minutes: float = 0, project_dir: str = ".", approved_by_person: bool = False,
                        ctx: Context = None) -> str:
    argv = ["session", "start", _name(name, "session")] + ([f"--backend={backend}"] if backend else []) + ([f"--gpu={gpu}"] if gpu else []) \
        + (["--idle-minutes", str(idle_minutes)] if idle_minutes else []) + (["--max-minutes", str(max_minutes)] if max_minutes else []) \
        + (["--approve"] if approved_by_person else [])
    return _text(*await _with_approval(ctx, project_dir, argv))


@server.tool(description="Run Python in a live session's kernel - variables persist between calls, like notebook cells. "
                         "Pass code, or notebook (train.ipynb, or a percent-format train.py) with cells (e.g. '3-5') and "
                         "params (Colab form fields or papermill parameters, as a JSON object string). Returns what it printed, the last value, the images it displayed (as images), "
                         "and errors with the line that raised them; as_json=true returns the raw results instead. A call "
                         "longer than timeout_seconds is interrupted; the kernel survives. Other tools keep working "
                         "meanwhile (session_stop ends it).")
async def session_exec(code: str = "", notebook: str = "", cells: str = "", params: str = "", session: str = "",
                       timeout_seconds: float = 600, project_dir: str = ".", as_json: bool = False):
    argv = ["exec", "--json"] + (["-s", _name(session, "session")] if session else []) + ["--timeout", str(timeout_seconds)]
    if notebook:
        argv += [f"--cells={notebook}:{cells}" if cells else f"--cells={notebook}"] + ([f"--params={params}"] if params else [])
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
    argv = ["files", op] + (["-s", _name(session, "session")] if session else []) + ["--", path] \
        + ([second_path] if second_path else [])
    return _text(*await _cli(project_dir, argv))


@server.tool(description="Copy the project's files to a live session's machine again after editing them locally "
                         "(variables in the kernel are kept).")
async def session_sync(session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "sync", *([_name(session, "session")] if session else [])]))


@server.tool(description="pip install packages on a live session's machine (space-separated, pin versions).")
async def session_install(packages: str, session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "install", *(["-s", _name(session, "session")] if session else []), "--",
                                                 *packages.split()]))


@server.tool(description="Run a shell command on a live session's machine (nvidia-smi, ls, df, ...).")
async def session_shell(command: str, session: str = "", project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "shell", *(["-s", _name(session, "session")] if session else []), "--", command]))


@server.tool(description="Live sessions with their hardware, uptime, idle time and cost so far.")
async def session_list(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["session", "list"]))


@server.tool(description="Stop a live session (default: the only running one) and report what it cost; it works while "
                         "a command is still running in it. Stop sessions as soon as you are done with them.")
async def session_stop(session: str = "", project_dir: str = ".", all_sessions: bool = False) -> str:
    argv = ["session", "stop", *([_name(session, "session")] if session else []), *(["--all"] if all_sessions else [])]
    return _text(*await _cli(project_dir, argv))


@server.tool(description="Start an improvement loop on a plan, on a git branch of its own: the baseline runs now. Then "
                         "edit the code and call loop_try for each idea; testbench keeps a try only if the plan passes "
                         "and metric (FILE:KEY of a number a json expectation of the plan checks, e.g. "
                         "outputs/metrics.json:accuracy, or check:SUBSTRING for the number of the check whose name "
                         "contains it, e.g. check:val_loss) beats the best so far, else reverts it. budget_usd bounds the "
                         "whole loop. Stopping rules, for a loop left to run: target (done once the best reaches it; "
                         "null for none), patience (refuse a try after that many in a row were not kept) and "
                         "max_tries (0 for none). For a trading strategy, returns names each try's equity.csv (e.g. "
                         "out/equity.csv; default: the one the plan keeps as evidence), and every report says whether "
                         "the best try is better than luck (its deflated Sharpe over all tries, reverted ones too); "
                         "min_deflated_sharpe (e.g. 0.95) makes target count only once the best try also clears it. "
                         "Above the approval threshold the person is asked directly when the "
                         "client supports it; otherwise it needs approved_by_person=true, set only after the person "
                         "agreed.")
async def loop_start(plan: str, metric: str, budget_usd: float, goal: str = "max", max_hours: float = 8.0,
                     min_delta: float = 0.0, target: float | None = None, patience: int = 0, max_tries: int = 0,
                     returns: str = "", min_deflated_sharpe: float | None = None, project_dir: str = ".",
                     approved_by_person: bool = False, ctx: Context = None) -> str:
    argv = ["loop", "start", _name(plan, "plan"), f"--metric={metric}", "--budget-usd", str(budget_usd), f"--goal={goal}",
            "--max-hours", str(max_hours), "--min-delta", str(min_delta)] \
        + ([f"--target={target}"] if target is not None else []) \
        + (["--patience", str(patience)] if patience else []) + (["--max-tries", str(max_tries)] if max_tries else []) \
        + ([f"--returns={returns}"] if returns else []) \
        + ([f"--min-deflated-sharpe={min_deflated_sharpe}"] if min_deflated_sharpe is not None else []) \
        + (["--approve"] if approved_by_person else [])
    return _text(*await _with_approval(ctx, project_dir, argv))


@server.tool(description="Try the edits made since the loop's best commit: commit them, run the plan (passing on "
                         "progress), and keep the try (exit 0) or revert it (exit 1); describe what changed in a few "
                         "words. wait_seconds=0 follows the run until it ends; if it is still running (exit 3), "
                         "loop_status judges it later. A try is refused (exit 2) once a stopping rule fires: stop "
                         "the loop and report; after patience ran out, new_direction=true is for an idea different "
                         "in kind.")
async def loop_try(description: str, wait_seconds: int = 0, new_direction: bool = False, project_dir: str = ".",
                   ctx: Context = None) -> str:
    argv = ["loop", "try"] + (["--wait", str(wait_seconds)] if wait_seconds else []) \
        + (["--new-direction"] if new_direction else []) + ["--", description]
    return _text(*await _cli(project_dir, argv, on_line=_progress(ctx)))


@server.tool(description="The loop's tries: kept or reverted, the metric, the cost, the best so far and what is left; "
                         "for a strategy, whether the best try is better than luck.")
async def loop_status(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["loop", "status"]))


@server.tool(description="Stop the loop: write its report (each kept change and what it moved, each reverted try and "
                         "why) and go back to the branch it started from; the kept changes stay on the loop's branch "
                         "for the person to review and merge.")
async def loop_stop(project_dir: str = ".") -> str:
    return _text(*await _cli(project_dir, ["loop", "stop"]))


mcp_resources.register(server)              # testbench://plans, runs, reports, evidence, the loop, notes; the prompts


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
