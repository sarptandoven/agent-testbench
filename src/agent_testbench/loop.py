"""testbench loop: an agent's improvement loop, judged by proof runs and held inside a budget.

    testbench loop start PLAN --metric outputs/metrics.json:accuracy --budget-usd 5 [--goal max] [--max-hours 8]
    (edit the code)
    testbench loop try "lower the learning rate to 3e-4"   # commit, run the plan, keep it or revert it
    testbench loop status                                  # every try: kept or reverted, its metric, its cost
    testbench loop stop                                    # the report; the kept changes stay on the loop's branch

The agent proposes, testbench judges. The loop works on a branch of its own. Each try commits the agent's edits in
the project folder and runs the plan on them, in a scratch copy so runs leave the folder alone. A try is kept when
its run passes and the metric beats the best so far by more than min_delta; otherwise the branch goes back to the
best commit (edits made meanwhile are put back on top). The metric is a number one of the plan's expectations
checks, so it reads the same on every backend: FILE:KEY of a json expectation, or check:SUBSTRING for any check whose
name contains SUBSTRING (a junit count, a number printed to the log). The person approves the loop's total budget
once; each run is still checked against max_run_usd and the day limits, and the loop refuses a try its budget has no
room for.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import subprocess
import time
from pathlib import Path

from . import budget, config as C, report as R, runs

ACTIVE = "running"
OWN = ":(exclude).testbench"       # testbench's own folder (runs, notes, this loop) is never tried or reverted
CHECK = "check"                    # --metric check:SUBSTRING


class LoopError(RuntimeError):
    pass


def loops_dir(root: Path) -> Path:
    return Path(root) / ".testbench" / "loops"


def _save(state: dict) -> dict:
    p = Path(state["dir"]) / "state.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(p)
    return state


def active(root: Path) -> dict | None:
    d = loops_dir(root)
    for p in sorted(d.glob("*/state.json")) if d.exists() else []:
        s = json.loads(p.read_text())
        if s.get("status") == ACTIVE:
            return s
    return None


def _current(root: Path) -> dict:
    s = active(root)
    if not s:
        raise LoopError("No loop is running here. Start one with `testbench loop start PLAN --metric FILE:KEY "
                        "--budget-usd N`.")
    return s


def _git(root: Path, *args, check: bool = True) -> str:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=300)
    if check and p.returncode != 0:
        raise LoopError(f"`git {' '.join(args[:3])}` failed: {(p.stderr or p.stdout).strip()[-600:]}")
    return p.stdout.strip()


def _identity(root: Path) -> list[str]:
    """Commit as the person's git identity; a loop on a machine without one still commits, under its own name."""
    if _git(root, "config", "user.email", check=False):
        return []
    return ["-c", "user.name=agent-testbench loop", "-c", "user.email=testbench-loop@localhost"]


def _own(root: Path) -> list[str]:
    """Pathspecs for what is testbench's own: the project's .testbench/, and this machine's folder (limits, ledger)
    if it happens to be inside the project."""
    out = [OWN]
    try:
        out.append(":(exclude)" + str(budget.user_dir().resolve().relative_to(Path(root).resolve())))
    except ValueError:
        pass
    return out


def _changes(root: Path, where: str = ".") -> list[str]:
    return [ln for ln in _git(root, "status", "--porcelain", "--", where, *_own(root)).splitlines() if ln.strip()]


def parse_metric(spec: str) -> tuple[str, str]:
    """FILE:KEY (a json expectation), or check:SUBSTRING (path CHECK: any check whose name contains SUBSTRING)."""
    spec = str(spec)
    if spec.startswith(CHECK + ":") and spec[len(CHECK) + 1:].strip():
        return CHECK, spec[len(CHECK) + 1:].strip()
    path, sep, key = spec.rpartition(":")
    path, key = path.strip(), key.strip()                # "check: " would match every check
    if not sep or not path or not key:
        raise LoopError(f"--metric is FILE:KEY, the file and key one of the plan's json expectations checks (e.g. "
                        f"outputs/metrics.json:accuracy), or check:SUBSTRING for the number of the check whose name "
                        f"contains SUBSTRING (e.g. check:val_loss); got {spec!r}")
    return path, key


def metric_value(report: dict, path: str, key: str) -> float | None:
    """The number the report's last check of `path: key` found (a json expectation); with path CHECK, the number the
    last check whose name contains `key` found."""
    value = None
    for s in report.get("steps", []):
        for c in s.get("expectations") or []:
            name = str(c.get("check", ""))
            if (key in name) if path == CHECK else name.startswith(f"{path}: {key} "):
                n = R.check_number(c)
                value = value if n is None else n
    return value


def _what(state: dict) -> str:
    return f"check containing {state['key']!r}" if state["path"] == CHECK else f"{state['key']} in {state['path']}"


def _missing(state: dict, rep: dict) -> str:
    """Why a passing run gave no metric; with check:SUBSTRING, which checks did find numbers."""
    if state["path"] != CHECK:
        return f"reported no {state['key']} in {state['path']}"
    found = [c["check"] for s in rep.get("steps", []) for c in s.get("expectations") or []
             if R.check_number(c) is not None]
    return f"no check whose name contains {state['key']!r} found a number" \
        + (f" (checks that did: {'; '.join(found[:8])})" if found else "")


def _better(state: dict, value: float | None) -> bool:
    best = state["best"].get("value")
    if value is None:
        return False
    if best is None:
        return True
    delta = float(state["min_delta"])
    return value > best + delta if state["goal"] == "max" else value < best - delta


def spent(state: dict) -> float:
    return round(sum(t.get("cost_usd") or 0 for t in state["tries"]), 4)


def _note(root: Path, text: str) -> None:
    notes = Path(root) / ".testbench" / "NOTES.md"
    notes.parent.mkdir(parents=True, exist_ok=True)
    with notes.open("a") as f:
        f.write(f"- {dt.datetime.now():%Y-%m-%d %H:%M}: {text}\n")


def _fmt(v) -> str:
    return "none" if v is None else f"{v:.4g}"


def start(root: Path, cfg: dict, plan: str, metric: str, budget_usd: float, goal: str = "max",
          max_hours: float = 8.0, min_delta: float = 0.0, approve: bool = False) -> dict:
    if plan not in cfg["plans"]:
        raise LoopError(f"No plan {plan!r}. Plans: {', '.join(cfg['plans'])}")
    if goal not in ("max", "min"):
        raise LoopError("--goal is max (a higher metric is better) or min (lower is better)")
    for flag, v, least in (("--budget-usd", budget_usd, 0), ("--max-hours", max_hours, 1e-9), ("--min-delta", min_delta, 0)):
        if not (isinstance(v, (int, float)) and math.isfinite(v) and v >= least):      # nan would pass every limit
            raise LoopError(f"{flag} is a number of {'at least 0' if least == 0 else 'more than 0'}; got {v!r}")
    path, key = parse_metric(metric)
    checked = any(key in ((s["expect"].get("json") or {}).get(path) or {}) for s in cfg["plans"][plan]["steps"])
    if path != CHECK and not checked:
        raise LoopError(f"plan {plan} checks no {key!r} in {path}, so its runs would not report the metric. Give a "
                        f"step an expectation such as `expect: {{json: {{{path}: {{{key}: \">= 0\"}}}}}}`.")
    if active(root):
        raise LoopError(f"loop {active(root)['id']} is still running here; stop it first with `testbench loop stop`")
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise LoopError("the loop keeps and reverts changes with git, and this folder is not in a git repository: "
                        "`git init`, commit the project, then start the loop")
    dirty = _changes(root, _git(root, "rev-parse", "--show-toplevel"))
    if dirty:
        raise LoopError("the repository has uncommitted changes, which a revert could lose. Commit or stash them, then "
                        "start the loop:\n  " + "\n  ".join(dirty[:10]))
    worst = budget.worst_case(cfg, plan)["worst_case_usd"]
    if worst > budget_usd:
        raise LoopError(f"--budget-usd ${budget_usd:.2f} has no room for one run of {plan} (worst case ${worst:.2f})")
    ok, message, _ = budget.check_launch(cfg, plan, approve=True)
    if not ok:
        raise LoopError(message)
    ask, where = budget.limits(cfg)["ask_above_usd"]
    if budget_usd > ask and not approve:
        raise LoopError(f"Needs approval: a loop on plan {plan} could spend up to ${budget_usd:.2f} in total (each run "
                        f"up to ${worst:.2f}), above {where}. Ask a person; if they agree, rerun with --approve.")
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    lid = f"{stamp}-{plan}"
    branch = f"testbench-loop/{lid}"
    original = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", branch)
    d = loops_dir(root) / lid
    d.mkdir(parents=True)
    state = _save({"id": lid, "dir": str(d), "status": ACTIVE, "plan": plan, "metric": metric, "path": path, "key": key,
                   "goal": goal, "min_delta": float(min_delta), "budget_usd": float(budget_usd), "approved": bool(approve),
                   "deadline": time.time() + float(max_hours) * 3600, "branch": branch, "original_branch": original,
                   "base": base, "best": {"commit": base, "value": None, "try": None}, "tries": [], "pending": None,
                   "started": time.time()})
    return _launch(root, cfg, state, "baseline (the code as it was)", base)


def _room(cfg: dict, state: dict) -> float:
    """The worst case of one more run, if the loop has room for it in money and time."""
    worst = budget.worst_case(cfg, state["plan"])["worst_case_usd"]
    if spent(state) + worst > state["budget_usd"]:
        raise LoopError(f"the loop's budget has no room for another run: ${spent(state):.2f} of "
                        f"${state['budget_usd']:.2f} spent, and a run could cost up to ${worst:.2f}. Stop the loop.")
    if time.time() > state["deadline"]:
        raise LoopError("the loop is past its --max-hours; stop it with `testbench loop stop`")
    return worst


def _launch(root: Path, cfg: dict, state: dict, description: str, commit: str) -> dict:
    worst = _room(cfg, state)
    run_cfg = copy.deepcopy(cfg)
    run_cfg["plans"][state["plan"]]["workdir"] = "copy"         # runs leave the project folder alone
    try:
        run_dir = runs.launch(root, run_cfg, state["plan"], approve=state["approved"])
    except runs.LaunchRefused as exc:
        raise LoopError(str(exc)) from None
    n = len(state["tries"])
    state["tries"].append({"n": n, "description": description, "commit": commit, "run": run_dir.name,
                           "status": "running", "worst_case_usd": worst, "started": time.time()})
    state["pending"] = n
    return _save(state)


def try_change(root: Path, cfg: dict, description: str) -> dict:
    state = judge(root, cfg, _current(root))
    if state["pending"] is not None:
        t = state["tries"][state["pending"]]
        raise LoopError(f"try {t['n']} is still running (run {t['run']}); wait for it (`testbench wait {t['run']}`, then "
                        "`testbench loop status`) before the next try")
    top = _git(root, "rev-parse", "--show-toplevel")
    inside = _changes(root)
    elsewhere = [ln for ln in _changes(root, top) if ln not in inside]
    if elsewhere:
        raise LoopError("there are changes outside the project folder, which the loop does not try:\n  "
                        + "\n  ".join(elsewhere[:10]))
    if not inside:
        raise LoopError("nothing changed since the best commit; edit the code, then `testbench loop try \"what changed\"`")
    _room(cfg, state)
    _git(root, "add", "-A", "--", ".", *_own(root))
    n = len(state["tries"])
    _git(root, *_identity(root), "commit", "-q", "-m", f"testbench loop try {n}: {description}")
    try:
        return _launch(root, cfg, state, description, _git(root, "rev-parse", "HEAD"))
    except LoopError:
        _git(root, "reset", "-q", "--soft", "HEAD~1")       # refused: the edits stay, uncommitted, as they were
        raise


def _revert(root: Path, state: dict, n: int) -> str:
    """Back to the best commit; edits made while the try ran are stashed and put back on top, and the lab notebook
    (.testbench/NOTES.md, which may be committed) keeps what was written to it."""
    notes = Path(root) / ".testbench" / "NOTES.md"
    kept_notes = notes.read_text() if notes.exists() else None
    stashed = bool(_changes(root))
    if stashed:
        _git(root, "stash", "push", "-q", "--include-untracked", "-m", f"testbench loop: edits made while try {n} ran",
             "--", ".", *_own(root))
    _git(root, "reset", "-q", "--hard", state["best"]["commit"])
    if kept_notes is not None:
        notes.write_text(kept_notes)
    if stashed and subprocess.run(["git", "stash", "pop", "-q"], cwd=root, capture_output=True).returncode != 0:
        return (" Edits made while it ran did not apply cleanly on the best commit; they are kept in `git stash list` "
                f"(\"edits made while try {n} ran\").")
    return " Edits made while it ran were put back on top." if stashed else ""


def judge(root: Path, cfg: dict, state: dict) -> dict:
    """Close the pending try once its run has finished: keep it or revert it."""
    if state["pending"] is None:
        return state
    t = state["tries"][state["pending"]]
    run_dir = runs.runs_dir(root) / t["run"]
    run_state = runs.read_state(run_dir)
    if run_state.get("status") not in runs.DONE:
        return state
    rep = R.load(run_dir) or {}
    value = metric_value(rep, state["path"], state["key"])
    best = state["best"]["value"]
    t.update(status=run_state["status"], value=value, cost_usd=run_state.get("cost_usd") or 0.0, finished=time.time())
    baseline = t["n"] == 0
    if run_state["status"] == "passed" and (baseline or _better(state, value)):
        t["kept"] = True
        state["best"] = {"commit": t["commit"], "value": value, "try": t["n"]}
        if baseline:
            t["verdict"] = f"BASELINE: {state['key']} {_fmt(value)}, run {t['run']} passed" + (
                f", but {_missing(state, rep)}; any passing try that reports {state['key']} will be kept."
                if value is None else ".")
        else:
            t["verdict"] = (f"KEPT try {t['n']}: {state['key']} {_fmt(value)} (best was {_fmt(best)}"
                            + (f", {value - best:+.4g}" if best is not None else "") + f"). Run {t['run']} passed. "
                            f"Best is now {t['commit'][:9]}.")
    else:
        t["kept"] = False
        if run_state["status"] != "passed":
            why = f"the run {run_state['status']} (`testbench report {t['run']}` says why)"
        elif value is None:
            why = f"the run passed but {_missing(state, rep)}"
        else:
            why = f"{state['key']} {_fmt(value)} is not better than the best {_fmt(best)}" \
                + (f" by more than {state['min_delta']:g}" if state["min_delta"] else "")
        if baseline:
            t["verdict"] = f"BASELINE: {why}; any passing try that reports {state['key']} will be kept."
        else:
            t["verdict"] = f"REVERTED try {t['n']}: {why}. The branch is back at {state['best']['commit'][:9]}." \
                + _revert(root, state, t["n"])
    state["pending"] = None
    _note(root, f"loop {state['id']} " + t["verdict"].split(". The branch")[0] + f" - {t['description']}")
    return _save(state)


def follow(root: Path, cfg: dict, state: dict, timeout: float | None, on_line=print) -> dict:
    """Wait for the pending try's run (up to timeout seconds; None: until it ends) and judge it."""
    if state["pending"] is None:
        return state
    t = state["tries"][state["pending"]]
    if timeout is None:
        p = cfg["plans"][state["plan"]]
        from . import backends
        timeout = (C.plan_minutes(p) + backends.get(p["backend"]).startup_minutes + 5) * 60
    runs.wait(runs.runs_dir(root) / t["run"], timeout, on_line=on_line)
    return judge(root, cfg, state)


def describe(state: dict) -> str:
    used, kept = spent(state), [t for t in state["tries"] if t.get("kept") and t["n"] > 0]
    left = (state["deadline"] - time.time()) / 3600
    lines = [f"loop {state['id']} ({state['status']}) on plan {state['plan']}: {state['goal']} {_what(state)}; "
             f"best {_fmt(state['best']['value'])} at {state['best']['commit'][:9]}; "
             f"{len(state['tries']) - 1 if state['tries'] else 0} tries, {len(kept)} kept; ${used:.3f} of "
             f"${state['budget_usd']:.2f} spent" + (f"; {left:.1f} h left" if state["status"] == ACTIVE else ""),
             f"branch {state['branch']} (from {state['original_branch']} at {state['base'][:9]})"]
    for t in state["tries"]:
        mark = "running" if t["status"] == "running" else ("kept" if t.get("kept") else "reverted")
        if t["n"] == 0:
            mark = "baseline"
        cost = f"${t['cost_usd']:.3f}" if t.get("cost_usd") is not None else f"up to ${t['worst_case_usd']:.2f}"
        lines.append(f"  {t['n']:>3}  {mark:<9} {state['key']}={_fmt(t.get('value'))}  {cost}  {t['run']}  {t['description']}")
    return "\n".join(lines)


def stop(root: Path, cfg: dict) -> tuple[dict, Path]:
    state = judge(root, cfg, _current(root))
    if state["pending"] is not None:
        t = state["tries"][state["pending"]]
        raise LoopError(f"try {t['n']} is still running (run {t['run']}): wait for it, or cancel it with "
                        f"`testbench cancel {t['run']}` and run `testbench loop status`, then stop")
    dirty = _changes(root)
    if dirty:
        raise LoopError("there are edits that were never tried; try them (`testbench loop try`) or discard them "
                        "(`git checkout -- .`), then stop:\n  " + "\n  ".join(dirty[:10]))
    state["status"], state["stopped"] = "stopped", time.time()
    if state["original_branch"] != "HEAD":
        _git(root, "checkout", "-q", state["original_branch"])
    path = Path(state["dir"]) / "report.md"
    path.write_text(render(state))
    _save(state)
    return state, path


def render(state: dict) -> str:
    tries = [t for t in state["tries"] if t["n"] > 0]
    kept = [t for t in tries if t.get("kept")]
    base = state["tries"][0].get("value") if state["tries"] else None
    hours = ((state.get("stopped") or time.time()) - state["started"]) / 3600
    out = [f"# Loop `{state['id']}` on plan `{state['plan']}`: {state['key']} {_fmt(base)} -> "
           f"{_fmt(state['best']['value'])} ({state['goal']})", "",
           f"{len(tries)} tries, {len(kept)} kept, ${spent(state):.3f} of ${state['budget_usd']:.2f}, {hours:.1f} h. "
           f"Branch `{state['branch']}` from `{state['original_branch']}` at `{state['base'][:9]}`.", ""]
    if kept:
        out += ["## Kept", ""]
        prev = base
        for t in kept:
            moved = f" ({t['value'] - prev:+.4g})" if prev is not None and t.get("value") is not None else ""
            out.append(f"- try {t['n']} `{t['commit'][:9]}`: {t['description']} - {state['key']} {_fmt(prev)} -> "
                       f"{_fmt(t.get('value'))}{moved}, run `{t['run']}`, ${t.get('cost_usd', 0):.3f}")
            prev = t.get("value")
        out.append("")
    reverted = [t for t in tries if not t.get("kept")]
    if reverted:
        out += ["## Tried and reverted", ""]
        for t in reverted:
            why = re.sub(r"^REVERTED try \d+: ", "", t.get("verdict", "")).split(" The branch")[0]
            out.append(f"- try {t['n']}: {t['description']} - {why}")
        out.append("")
    out += ["## Next", "",
            f"- review: `git log --oneline {state['base'][:9]}..{state['branch']}` and "
            f"`git diff {state['base'][:9]}..{state['branch']}`",
            f"- keep the result: `git merge {state['branch']}` (a person decides)",
            f"- the proof for the best result: run `{state['tries'][state['best']['try']]['run']}`"
            if state["best"]["try"] is not None else "- no run passed with the metric; nothing to merge"]
    return "\n".join(out) + "\n"
