"""The MCP server's resources and prompts: what an agent reads without running a command, and the common jobs started
the right way.

    testbench://plans                           each plan: hardware, steps, worst case, whether a person approves it
    testbench://runs                            recent runs, one line each
    testbench://runs/{run_id}/report            a run's report.md (a run id, a prefix of one, last or last~N)
    testbench://runs/{run_id}/report.json       its proof record
    testbench://runs/{run_id}/evidence/{+path}  a file in its steps/ folder: text as text, images as images
    testbench://loops/current                   the open loop: tries, best, stopping rules, the luck line
    testbench://notes                           the lab notebook, .testbench/NOTES.md
    testbench://config/schema                   testbench.yaml's JSON Schema

Resources read the project the server was started in (the nearest folder at or above it with a testbench.yaml), and
only what the dashboard serves: what testbench.yaml declares, run reports, step evidence, the loop and the notes -
never state.json, plan.json (step env values), ledgers, the machine's limits file, secrets or any file outside the
project's .testbench. A run id or evidence path from the client is checked as asked and again as it resolves, so
`..`, an absolute path or a link cannot lead out. A read launches, approves, cancels, writes and spends nothing.

Prompts are short instructions a client offers as slash commands: prove-this-change, find-why-this-fails,
tune-with-a-loop and check-a-strategy. They say what cli.AGENTS_SECTION and skills/iterate say, in MCP tool names.
"""
from __future__ import annotations

import functools
import inspect
import json
import re
from pathlib import Path
from typing import Annotated

import anyio
from pydantic import Field

from . import budget, config as C, loop as L, runs

try:                                         # mcp >= 2
    from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError
except ImportError:                          # mcp 1.x
    from mcp.server.fastmcp.exceptions import ResourceError
    ResourceNotFoundError = ResourceError

RUNS, PLANS, LOOP, NOTES, SCHEMA = ("testbench://runs", "testbench://plans", "testbench://loops/current",
                                    "testbench://notes", "testbench://config/schema")
REPORT, REPORT_JSON = "testbench://runs/{run_id}/report", "testbench://runs/{run_id}/report.json"
EVIDENCE = "testbench://runs/{run_id}/evidence/{+path}"
RECENT = 20                     # runs testbench://runs lists
MAX_TEXT = 1_000_000            # bytes of a text file a read returns (the end of a longer one: a log's end matters)
MAX_JSON = 8_000_000            # a report.json is returned whole or not at all
MAX_BLOB = 2_000_000            # bytes of an image or other binary file, as the tools return images
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXTS = {".md": "text/markdown", ".json": "application/json", ".ipynb": "application/json", ".csv": "text/csv",
         ".jsonl": "text/plain", ".txt": "text/plain", ".log": "text/plain", ".tsv": "text/plain", ".py": "text/plain",
         ".yaml": "text/plain", ".yml": "text/plain", ".xml": "text/plain", ".out": "text/plain", ".err": "text/plain",
         ".svg": "text/plain", ".html": "text/plain", ".htm": "text/plain"}      # markup as text: nothing renders it
RUN_REF = re.compile(r"(?:last|latest)~\d{1,9}|[A-Za-z0-9][\w.:-]{0,80}")
ELSEWHERE = ("The MCP server reads the project it was started in (the client's working folder for the server); start "
             "it in the project folder, or use the tools, which take project_dir.")


class Refused(ResourceNotFoundError):
    """A read refused, with what to ask for instead. The client is shown the message (a protocol error)."""


# ---- where things are ------------------------------------------------------------------------------------------

def root() -> Path:
    """The project: the nearest folder at or above the server's working folder with a testbench.yaml (or the record
    `testbench check` keeps where there is none)."""
    try:
        return C.find_root(Path.cwd())
    except C.ConfigError as exc:
        raise Refused(f"{exc} {ELSEWHERE}") from None


def _config(top: Path) -> dict:
    try:
        return C.load(top)
    except C.ConfigError as exc:
        raise Refused(str(exc)) from None


def _shown(top: Path, f: Path) -> str:
    """A path as the agent can open it: relative to the project when it is inside."""
    try:
        return Path(f).relative_to(top.resolve()).as_posix()
    except ValueError:
        return str(f)


def run_dir(top: Path, run_id: str) -> Path:
    """The run a client names: an id, a unique prefix of one, last or last~N, as `runs.resolve` reads them - and
    only a folder directly in .testbench/runs once links are followed."""
    if not isinstance(run_id, str) or not RUN_REF.fullmatch(run_id):
        raise Refused(f"{run_id!r} is not a run testbench knows: a run is named by its id (as testbench://runs lists "
                      "it), a prefix of one, last or last~N")
    try:
        found = runs.resolve(top, run_id)
    except FileNotFoundError as exc:
        raise Refused(str(exc)) from None
    try:
        home, real = runs.runs_dir(top).resolve(strict=True), found.resolve(strict=True)
    except (OSError, RuntimeError):
        raise Refused(f"run {found.name} cannot be read") from None
    if real.parent != home:
        raise Refused(f"run {found.name} is a link out of .testbench/runs; only runs kept there are read")
    return real


def _inside(base: Path, parts: list[str], what: str, exact: bool = True) -> Path | None:
    """The file at base/parts, None if there is none. Refused unless it stays inside base once links are followed,
    with no hidden part; exact: and is that very file, not a link to another (a report.md linked to state.json)."""
    try:
        top = base.resolve(strict=True)
        real = top.joinpath(*parts).resolve(strict=True)
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, ValueError):
        raise Refused(f"{what} cannot be read") from None
    if not real.is_relative_to(top) or any(p.startswith(".") for p in real.relative_to(top).parts) \
            or (exact and real != top.joinpath(*parts)):
        raise Refused(f"{what} is a link to a file it may not lead to; it is not read")
    if not real.is_file():
        raise Refused(f"{what} is not a file")
    return real


def evidence_parts(path: str) -> list[str]:
    """An evidence path from the client, as the folders and name under the run's steps/ folder. Refused unless it is
    plainly a relative path down into it: no .., no absolute path, no drive, no hidden or empty part."""
    def no(why: str):
        raise Refused(f"evidence path {path!r} is refused: {why}. An evidence path is a file in the run's steps/ "
                      "folder, as report.json's evidence list names it (steps/01-train/log.txt; the steps/ may be "
                      "left out)")
    if not isinstance(path, str) or not path:
        no("it is empty")
    if len(path) > 1000 or "\0" in path or "\\" in path:
        no("it is too long, or has a NUL or a backslash")
    if path.startswith(("/", "~")) or re.match(r"[A-Za-z]:", path):
        no("it is absolute")
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        no("it has an empty, . or .. part")
    if any(p.startswith(".") or ":" in p for p in parts):
        no("a hidden name (starting with .) or a : is not read")
    if parts[0] == "steps":
        parts = parts[1:]
    if not parts:
        no("it names the steps/ folder itself, not a file in it")
    return parts


# ---- what is read --------------------------------------------------------------------------------------------

def _text(f: Path, shown: str) -> str:
    """A text file, or the end of a long one (from a whole line) with a line that says so."""
    size, most = f.stat().st_size, MAX_TEXT
    with open(f, "rb") as h:
        if size > most:
            h.seek(size - most)
            h.readline()
        data = h.read(most)
    text = data.decode("utf-8", "replace")
    if size > most:
        text = (f"[the first {size - len(data)} of its {size} bytes are not shown here, only the end; {shown} has "
                "them all]\n" + text)
    return text


def plans() -> str:
    """Each plan with its hardware, steps and worst case, and what launching it takes: what `testbench plans`
    says, as JSON. Never a step's env values."""
    top = root()
    cfg = _config(top)
    lim = budget.limits(cfg)
    most, ask = lim["max_run_usd"][0], lim["ask_above_usd"][0]
    out = []
    for name, plan in cfg["plans"].items():
        est = budget.worst_case(cfg, name)
        worst = est["worst_case_usd"]
        launch = ("refused: its worst case is over max_run_usd" if not C.number(worst) or worst > most else
                  "a person approves it first (over ask_above_usd)" if worst > ask else
                  "free: runs without asking" if worst == 0 else "runs without asking")
        steps = [{"name": s["name"], "kind": s.get("kind"),
                  "what": s.get("notebook") or s.get("run") or (s.get("web") or {}).get("url"),
                  "max_minutes": s.get("max_minutes")} for s in plan["steps"]]
        out.append({"plan": name, "description": plan.get("description"), "backend": plan["backend"],
                    "gpu": plan.get("gpu"), "steps": steps, "max_minutes": est["max_minutes"],
                    "worst_case_usd": round(worst, 4) if C.number(worst) else None, "launch": launch})
    return json.dumps({"project": cfg["project"], "folder": str(top), "max_run_usd": most, "ask_above_usd": ask,
                       "max_day_usd": float(cfg["budget"]["max_day_usd"]), "plans": out,
                       "next": "run_plan runs one; a person approves any plan whose launch says so (never set "
                               "approved_by_person yourself)"}, indent=1)


def recent() -> str:
    """The latest runs, one line each as `testbench runs` prints them, newest last."""
    top = root()
    found = runs.all_runs(top)
    if not found:
        return f"No runs yet in {top}. run_plan starts one; testbench://plans lists the plans."
    lines = []
    for p in found[-RECENT:]:
        try:
            lines.append(runs.status_line(p))
        except Exception:                           # one unreadable state must not hide the others
            lines.append(f"{p.name}  (its state cannot be read)")
    return (f"{C.counted(len(found), 'run')} in {top}; the latest {len(lines)}, newest last. A run's report: "
            f"testbench://runs/RUN_ID/report (RUN_ID may be last or last~N).\n" + "\n".join(lines) + "\n")


def report(run_id: str) -> str:
    """A run's report.md; while it is still going, its status line."""
    top = root()
    d = run_dir(top, run_id)
    f = _inside(d, ["report.md"], f"run {d.name}'s report.md")
    if f is None:
        try:
            line = runs.status_line(d)
        except Exception:
            line = f"{d.name}  (its state cannot be read)"
        return (line + f"\nNo report yet: it is written when the run ends. wait_run follows run "
                f"{d.name}; testbench://runs/{d.name}/report.json has the steps done so far.\n")
    return _text(f, _shown(top, f))


def report_json(run_id: str) -> str:
    """A run's report.json, the proof record (schema/report-v1.schema.json)."""
    top = root()
    d = run_dir(top, run_id)
    f = _inside(d, ["report.json"], f"run {d.name}'s report.json")
    if f is None:
        raise Refused(f"run {d.name} has no report.json yet: it is written after its first step. "
                      f"testbench://runs/{d.name}/report says where it is.")
    if f.stat().st_size > MAX_JSON:
        raise Refused(f"run {d.name}'s report.json is {f.stat().st_size} bytes, more than a read returns; "
                      f"{_shown(top, f)} has it")
    return f.read_text(encoding="utf-8", errors="replace")


def evidence(run_id: str, path: str) -> tuple[str | bytes, str]:
    """(content, mime type) of an evidence file: text as text (the end of a long one), an image as image bytes."""
    top = root()
    parts = evidence_parts(path)
    d = run_dir(top, run_id)
    what = f"evidence {'/'.join(parts)} of run {d.name}"
    steps = d / "steps"
    if steps.is_symlink():
        raise Refused(f"run {d.name}'s steps/ folder is a link; its evidence is not read")
    if not steps.is_dir():
        raise Refused(f"run {d.name} has kept no evidence yet (no steps/ folder)")
    f = _inside(steps, parts, what, exact=False)          # a link from one evidence file to another is fine
    if f is None:
        raise Refused(f"run {d.name} has no evidence file steps/{'/'.join(parts)}. Its report.json lists every one "
                      f"(testbench://runs/{d.name}/report.json, under evidence).")
    shown, suffix, size = _shown(top, f), f.suffix.lower(), f.stat().st_size
    try:
        if suffix in IMAGES or suffix not in TEXTS and not _looks_like_text(f):
            if size > MAX_BLOB:
                raise Refused(f"{what} is {size} bytes, more than the {MAX_BLOB} a read returns as "
                              f"{'an image' if suffix in IMAGES else 'a file'}; open {shown} instead")
            return f.read_bytes(), IMAGES.get(suffix, "application/octet-stream")
        return _text(f, shown), TEXTS.get(suffix, "text/plain")
    except OSError as exc:
        raise Refused(f"{what} cannot be read: {exc.strerror or exc}") from None


def _looks_like_text(f: Path) -> bool:
    with open(f, "rb") as h:
        head = h.read(8192)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return exc.start >= len(head) - 3            # cut inside the last character only
    return True


def loop_now() -> str:
    """The open loop as `testbench loop status` says it, without judging a try (that is loop_status's job: it may
    revert code); else the last loop, stopped."""
    top = root()
    found, d = [], L.loops_dir(top)
    for p in sorted(d.glob("*/state.json")) if d.is_dir() and not d.is_symlink() else []:
        f = None if p.parent.is_symlink() else _inside(p.parent, ["state.json"], f"loop {p.parent.name}'s state")
        try:
            s = json.loads(f.read_text()) if f else None
        except (OSError, ValueError, RecursionError):
            s = None
        if isinstance(s, dict):
            found.append(s)
    if not found:
        return ("No loop here. loop_start starts one on a plan, judged by a number (the tune-with-a-loop prompt says "
                "how).\n")
    s = next((x for x in reversed(found) if x.get("status") == L.ACTIVE), found[-1])
    try:
        text = L.describe(s)
    except Exception as exc:                        # a state from another version, or cut short
        raise Refused(f"loop {s.get('id')}'s state cannot be read ({type(exc).__name__}: {exc})") from None
    if s.get("status") != L.ACTIVE:
        return (f"No loop is open. The last one, {s.get('id')}, has stopped; its report is in "
                f".testbench/loops/{s.get('id')}/report.md.\n{text}\n")
    pending, tries = s.get("pending"), s.get("tries") or []
    if isinstance(pending, int) and 0 <= pending < len(tries):
        t = tries[pending]
        try:
            state = runs.read_state(run_dir(top, str(t.get("run"))))
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("status") in runs.DONE:
            text += (f"\nTry {t.get('n')}'s run has ended ({state['status']}); loop_status keeps or reverts it "
                     "(this read does not).")
    return text + "\n"


def notes() -> str:
    top = root()
    f = _inside(top / ".testbench", ["NOTES.md"], ".testbench/NOTES.md")
    if f is None:
        return "No notes yet. add_note adds a line: what was tried and what was learned.\n"
    return _text(f, ".testbench/NOTES.md")


def schema() -> str:
    from .expect import schema_text                      # the shipped file, plus installed check plug-ins' schemas
    return schema_text()


# ---- prompts -------------------------------------------------------------------------------------------------

def _error(message: str):
    """A protocol error carrying message (a prompt's own errors are reported as "Error rendering prompt")."""
    try:
        from mcp.shared.exceptions import MCPError                       # mcp >= 2
        return MCPError(code=-32602, message=message)
    except ImportError:
        from mcp.shared.exceptions import McpError                       # mcp 1.x
        from mcp.types import ErrorData
        return McpError(ErrorData(code=-32602, message=message))


def _quiet_root() -> Path | None:
    try:
        return C.find_root(Path.cwd())
    except C.ConfigError:
        return None


def _plan_arg(plan: str) -> str:
    """A plan name given to a prompt: one testbench.yaml has, when the project can be read."""
    if not plan:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9][\w.:-]{0,80}", plan):
        raise _error(f"plan {plan!r} is not a plan name testbench uses (letters, digits, - _ . : and no leading -)")
    top = _quiet_root()
    try:
        cfg = C.load(top) if top else None
    except C.ConfigError:
        cfg = None
    if cfg and plan not in cfg["plans"]:
        raise _error(C.no_plan(cfg, plan))
    return plan


def _run_arg(run: str) -> str:
    """A run given to a prompt, as the id it resolves to when the project's runs can be read."""
    if not run:
        return ""
    if not RUN_REF.fullmatch(run):
        raise _error(f"run {run!r} is not a run testbench knows (a run id, a prefix of one, last or last~N)")
    top = _quiet_root()
    if top is None:
        return run
    try:
        return runs.resolve(top, run).name
    except FileNotFoundError as exc:
        raise _error(str(exc)) from None


Plan = Annotated[str, Field(description="a plan in testbench.yaml (testbench://plans lists them)")]
Run = Annotated[str, Field(description="a run id, a prefix of one, last or last~N (testbench://runs lists them)")]
Metric = Annotated[str, Field(description="FILE:KEY of a number a json expectation of the plan checks (e.g. "
                                          "outputs/metrics.json:accuracy), or check:SUBSTRING (e.g. check:val_loss)")]
MONEY = ("Money is the person's decision: never set approved_by_person=true yourself. When a launch needs approval, "
         "tell me what it is, on what hardware and its worst case, and wait for my yes. Stop every session you start "
         "as soon as you are done with it.")


def prove_this_change(plan: Plan = "") -> str:
    plan = _plan_arg(plan)
    which = (f"Use plan `{plan}`." if plan else
             "Pick the plan that exercises this change, the cheapest one that does (a local plan first).")
    return f"""Prove the change I just made with agent-testbench - a passing run, not "it should work".

1. Know the ground: read testbench://plans (each plan, its hardware, its worst case) and testbench://runs. {which}
2. If you do not know yet that the change works, explore first in a live session: session_start (local is free),
   session_exec to run the code or the notebook's cells, look at the values and the images, fix, run again;
   session_stop when done. A session is not proof: state built up by hand can hide a bug a clean run exposes.
3. Run the plan with run_plan and read the report it returns (get_report, or testbench://runs/RUN_ID/report): the
   failing cell and line, the missed expectations, what changed since the previous run. Look at the evidence
   images yourself before you call a visual result good.
4. If it fails, fix one thing at a time and run it again; record what you learned with add_note. Never weaken an
   expectation to make a run pass; if one was wrong, say why you changed it.
5. {MONEY}
6. Finish with the answer: the run id whose passing report proves it, what it cost (runs and sessions), what
   changed, and what is still unproven.
"""


def _latest_failed() -> str:
    """The latest run that failed or broke, "" when there is none (or no project here)."""
    top = _quiet_root()
    for p in reversed(runs.all_runs(top)) if top else []:
        try:
            if runs.read_state(p).get("status") in ("failed", "error"):
                return p.name
        except Exception:
            continue
    return ""


def find_why_this_fails(run: Run = "") -> str:
    run = _run_arg(run)
    latest = "" if run else _latest_failed()
    rid = run or latest or "RUN_ID"
    which = f"run `{run}`" if run else f"the latest failed run, `{latest}`," if latest else "the latest failed run"
    where = (f"testbench://runs/{rid}/report" if run or latest else
             "the report of the latest run that failed (testbench://runs lists them; then "
             "testbench://runs/RUN_ID/report)")
    return f"""Find out why {which} fails, fix it, and prove the fix.

1. Read {where}. Before changing anything, say in a sentence or two which step failed, which check failed
   (what it expected, what it found) and the cell and line that raised.
2. Look at its evidence: the failing step's log and images (testbench://runs/{rid}/evidence/STEP/log.txt and the
   other files testbench://runs/{rid}/report.json lists under evidence; images come back as images).
3. Reproduce it cheaply in a live session: session_start (local is free; a GPU only if the failure needs one, with
   a short max_minutes), then session_exec the failing code or cells until you see the same error. A notebook that
   does not run at all: rescue_notebook says which cell fails, why, and what to do.
4. Fix one thing at a time and check it in the session; session_stop when done.
5. Run the same plan again with run_plan and read the new report; diff_runs shows what changed against the
   failed run. Never weaken an expectation to make a run pass.
6. {MONEY}
7. Report: the cause in plain words, the fix, and the run id that now passes and what it cost - or what still
   fails, and why.
"""


def _metric_arg(metric: str) -> str:
    if not isinstance(metric, str) or not metric or len(metric) > 200 or not metric.isprintable() or "`" in metric:
        raise _error(f"metric {metric!r} is not a metric: FILE:KEY (e.g. outputs/metrics.json:accuracy) or "
                     "check:SUBSTRING (e.g. check:val_loss)")
    try:
        L.parse_metric(metric)
    except L.LoopError as exc:
        raise _error(str(exc).replace("--metric is", "metric is")) from None
    return metric


def tune_with_a_loop(plan: Plan, metric: Metric) -> str:
    plan, metric = _plan_arg(plan), _metric_arg(metric)
    if not plan:
        raise _error("plan is needed: the plan whose runs judge each try (testbench://plans lists them)")
    return f"""Improve plan `{plan}` on `{metric}` with an agent-testbench loop: one change per try, judged by a run.

1. Read testbench://plans for the plan's worst case per run, and testbench://loops/current: if a loop is open
   already, carry on with it instead of starting another.
2. Ask me for the loop's budget (budget_usd: what all its runs together may cost) and its stopping rules (target,
   patience, max_tries) unless I gave them. Then loop_start with plan="{plan}", metric="{metric}", that budget and
   goal max or min. The baseline runs first.
3. One change per try: edit the code, then loop_try with a few plain words on what changed. testbench keeps the try
   only if the plan passes and `{metric}` beats the best so far; otherwise it reverts the code. Read loop_status (or
   testbench://loops/current) before the next idea.
4. Never edit the plan's expectations during a loop to make tries pass.
5. Stop on the rules: a try refused because a rule fired (target reached, patience, max_tries) or the budget or
   time ran out is the loop's answer - undo the untried edits and loop_stop. After patience ran out, go on only
   with an idea different in kind (new_direction=true), not another variation of the ones that failed.
6. For a trading strategy every try counts, reverted ones too: report the luck line (the deflated Sharpe of the best
   try over all tries, and PBO once there are enough) with the result, and never present a best try it calls luck
   as an edge.
7. {MONEY}
8. Report from loop_stop's report: each kept change and what it moved, the run that proves the best one, what the
   loop cost. The kept changes stay on the loop's branch for me to review and merge.
"""


def check_a_strategy(plan: Plan = "") -> str:
    plan = _plan_arg(plan)
    which = f"Start from plan `{plan}`." if plan else "testbench://plans lists the plans; start from the backtest."
    return f"""Check whether this trading strategy holds up, rung by rung, with agent-testbench. {which}
Paper and simulation only: never trade live money and never point the code at a live account.

1. Backtest with costs: fees and slippage modeled (costs_modeled true in `python -m agent_testbench.trading.metrics
   out/equity.csv out/trades.csv`), 30 round trips or more, and a benchmark. A backtest without costs proves nothing.
2. Walk-forward: tune on one window, judge on the next, rolling forward over the history. It counts only if it holds
   out of sample in most windows, not just over the whole history.
3. Holdout, once: keep the last stretch of data out of every try. Run on it once, at the end; if it fails, that is
   the answer - never tune against it and run it again.
4. Paper: run the strategy against a paper account (mocks: alpaca, ccxt or ib_async on the step, with a prices
   file), with mock_calls bounding the orders and live: 0.
5. Count every try: if variants were tried (a loop, or by hand), report the deflated Sharpe of the best over all of
   them - the luck line in testbench://loops/current or loop_status. A best try it calls luck is not an edge.
6. {MONEY}
7. Never weaken an expectation to make a rung pass; a rung that fails is a finding.
8. Report plainly: each rung passed or failed with its run id, the numbers (Sharpe, max drawdown, trades, costs),
   the luck line, and the caveats - few trades, a short history, one market regime, positions still open. A
   backtest is not a forecast.
"""


PROMPTS = (
    ("prove-this-change", prove_this_change,
     "Prove a change with a plan run: explore in a session if needed, run the plan, read the report, fix one thing at "
     "a time, and finish with the run id that proves it and what it cost. plan: the plan to run (optional)."),
    ("find-why-this-fails", find_why_this_fails,
     "Read a failed run's report (by default the latest failed run's), name the failing check and line, reproduce it "
     "in a session, fix it and run the plan again. run: a run id, last or last~N (optional)."),
    ("tune-with-a-loop", tune_with_a_loop,
     "Improve a number with a loop: a budget, one change per try, stop on the rules, the luck line for a strategy. "
     "plan: the plan; metric: FILE:KEY or check:SUBSTRING, e.g. outputs/metrics.json:accuracy."),
    ("check-a-strategy", check_a_strategy,
     "The trading ladder: backtest with costs, walk-forward, holdout once, paper; the deflated Sharpe over every try "
     "and plain caveats. Never live money. plan: the backtest plan (optional)."),
)


# ---- the server ------------------------------------------------------------------------------------------------

def register(server) -> None:
    """Add the resources, prompts and argument completions to the MCP server."""
    own_checks = {}
    if "security" in inspect.signature(server.resource).parameters:
        from mcp.server.mcpserver.resources.templates import ResourceSecurity
        # mcp 2 refuses a template value with .. or an absolute path as an unknown resource; run_dir and
        # evidence_parts refuse those and more, and say why
        own_checks = {"security": ResourceSecurity(exempt_params={"run_id", "path"})}

    def add(uri, fn, name, description, mime, **extra):
        server.resource(uri, name=name, description=description, mime_type=mime, **extra)(fn)

    add(PLANS, plans, "plans", "Each plan: its backend and GPU, steps, worst-case cost and whether launching it needs "
        "a person's yes; the project's max_run_usd and ask_above_usd. Read before run_plan.", "application/json")
    add(RUNS, recent, "runs", f"The latest {RECENT} runs, one line each (id, status, step, backend, cost), newest "
        "last.", "text/plain")
    add(LOOP, loop_now, "loop", "The open loop: each try kept or reverted with its metric and cost, the best so far, "
        "the stopping rules and, for a strategy, whether the best try is better than luck. Reading it judges nothing.",
        "text/plain")
    add(NOTES, notes, "notes", "The lab notebook, .testbench/NOTES.md: what was tried and learned.", "text/markdown")
    add(SCHEMA, schema, "config-schema", "testbench.yaml's JSON Schema: every key, what it takes and its default.",
        "application/json")

    add(REPORT, report, "report", "A run's report: failures with cell, error and line, missed expectations, "
        "evidence, cost, changes since the previous run. run_id: an id, a prefix of one, last or last~N.",
        "text/markdown", **own_checks)
    add(REPORT_JSON, report_json, "report.json", "A run's proof record (report-v1 JSON): steps, every check and the "
        "number it found, cost, the code and machine it ran on, every evidence file with its sha256.",
        "application/json", **own_checks)
    _add_evidence(server, own_checks)

    for name, fn, description in PROMPTS:
        server.prompt(name=name, description=description)(fn)
    _add_completions(server)


def _add_evidence(server, own_checks: dict) -> None:
    """The evidence template returns text as text and images as images, so its type is decided per file: a template
    of mcp 2 whose read says the type (mcp 1.x has no {+path}, and goes without)."""
    try:
        from mcp.server.mcpserver.resources import BinaryResource, ResourceTemplate, TextResource
    except ImportError:
        return

    class Evidence(ResourceTemplate):
        async def create_resource(self, uri, params, context):
            content, mime = await anyio.to_thread.run_sync(functools.partial(self.fn, **params))
            if isinstance(content, str):
                return TextResource(uri=uri, name=self.name, description=self.description, mime_type=mime, text=content)
            return BinaryResource(uri=uri, name=self.name, description=self.description, mime_type=mime, data=content)

    def read(run_id: str, path: str):
        return evidence(run_id, path)

    try:
        template = Evidence.from_function(read, uri_template=EVIDENCE, name="evidence", mime_type="text/plain",
                                          description="A file a run kept as evidence, path in its steps/ folder as "
                                                      "report.json lists it (01-train/log.txt): text as text (the end "
                                                      "of a long log), images as images. Nothing outside the run's "
                                                      "steps/ folder is read.", **own_checks)
        server._resource_manager._templates[template.uri_template] = template   # as add_template does, this class
    except (TypeError, ValueError, AttributeError):     # an mcp whose templates work otherwise: the server still starts
        return


def _add_completions(server) -> None:
    """Completions for prompt and template arguments: plan names, recent run ids, a run's evidence paths."""
    if not hasattr(server, "completion"):
        return
    try:
        from mcp.types import Completion
    except ImportError:
        return

    @server.completion()
    async def complete(ref, argument, context):
        known = dict(getattr(context, "arguments", None) or {})
        values = await anyio.to_thread.run_sync(functools.partial(suggest, argument.name, argument.value or "", known))
        return Completion(values=values[:100], total=len(values), has_more=len(values) > 100)


def suggest(name: str, typed: str, known: dict) -> list[str]:
    """What a client may offer for an argument: plan names, run ids (newest first), or the evidence files of the run
    already chosen. Read-only, like the resources; [] when there is nothing to offer."""
    top = _quiet_root()
    if top is None:
        return []
    try:
        if name == "plan":
            return [p for p in C.load(top)["plans"] if p.startswith(typed)]
        if name in ("run", "run_id"):
            return [p.name for p in reversed(runs.all_runs(top)) if p.name.startswith(typed)]
        if name == "path" and known.get("run_id"):
            steps = run_dir(top, str(known["run_id"])) / "steps"
            if steps.is_symlink():
                return []
            found = sorted(rel for f in steps.rglob("*") if f.is_file() and not f.is_symlink()
                           and not (rel := f.relative_to(steps).as_posix()).startswith(".") and "/." not in rel)
            return [p for p in found if p.startswith(typed.removeprefix("steps/"))]
    except (C.ConfigError, ResourceError, OSError):
        return []
    return []


async def updated(ctx, project_dir: str, run: str) -> None:
    """Tell clients listening for changes (mcp 2's subscriptions/listen) that a run has ended: the runs list and its
    report are new. Nothing for older clients, nothing for another project, and never an error."""
    try:
        if ctx is None or C.find_root(Path(project_dir).expanduser()) != C.find_root(Path.cwd()):
            return
        uris = [RUNS] + ([f"testbench://runs/{run}/report", f"testbench://runs/{run}/report.json"]
                         if run and run not in ("last", "latest") else [])
        for uri in uris:
            await ctx.notify_resource_updated(uri)
    except Exception:
        pass
