"""testbench loop: an agent's improvement loop, judged by proof runs and held inside a budget.

    testbench loop start PLAN --metric outputs/metrics.json:accuracy --budget-usd 5 [--goal max] [--max-hours 8]
                         [--target 0.95] [--patience 4] [--max-tries 20]       # stopping rules, for overnight
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

Stopping rules tell an agent working alone when it is done: once the best reaches --target, after --max-tries tries,
or after --patience tries in a row that were not kept, the next try is refused with a message that says why and
what to do (for patience: stop, or try something different in kind with --new-direction). When a rule fires and
when the loop stops, testbench.yaml's `notify:` webhook hears about it (loop_end).
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import budget, config as C, notify, report as R, runs

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
    if it happens to be inside the project. One the repository ignores already is left out: git refuses to add or
    stash a path it ignores, even one a pathspec only excludes."""
    out = [OWN]
    try:
        out.append(":(exclude)" + str(budget.user_dir().resolve().relative_to(Path(root).resolve())))
    except ValueError:
        pass
    paths = [p.split(")", 1)[1] for p in out]
    ignored = set(_git(root, "check-ignore", "--", *paths, check=False).splitlines())
    return [p for p, path in zip(out, paths) if path not in ignored] + _env_files(root)


def _env_files(root: Path) -> list[str]:
    """Pathspecs that leave out the untracked .env and .env.* files the repository does not ignore: they hold
    secrets, which never go into a try's commit (as they never go into a copy of the project), nor into a stash."""
    found = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "-z", "--", ":(glob)**/.env",
                            ":(glob)**/.env.*"], cwd=root, capture_output=True, text=True, timeout=300).stdout
    return [":(exclude,literal)" + p for p in found.split("\0") if p]


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


def _desc(t: dict) -> str:
    return ("[new direction] " if t.get("new_direction") else "") + t["description"]


def _why(t: dict) -> str:
    """Why a try was reverted, from its verdict."""
    return re.sub(r"^REVERTED try \d+: ", "", t.get("verdict", "")).split(" The branch")[0]


def _streak(state: dict) -> list[dict]:
    """The latest finished tries that were not kept, in a row: back to the last kept one, or to a --new-direction
    try (which starts the count again)."""
    out = []
    for t in reversed(state["tries"][1:]):
        if t["status"] == "running" or t.get("kept"):
            break
        out.append(t)
        if t.get("new_direction"):
            break
    return out[::-1]


def _best_at(state: dict) -> str:
    b = state["best"]
    if b["try"] is None:
        return f"at {b['commit'][:9]}, no passing run yet"
    return (f"{'the baseline' if b['try'] == 0 else 'try ' + str(b['try'])}, commit {b['commit'][:9]}, "
            f"run {state['tries'][b['try']]['run']}")


STOP = "stop the loop with `testbench loop stop` and report"


def _halt(state: dict) -> tuple[str, str, str] | None:
    """The stopping rule that refuses another try, if one does: (status, what a verdict says, what a refusal says)."""
    key, best, goal = state["key"], state["best"]["value"], state["goal"]
    target = state.get("target")
    if target is not None and best is not None and (best >= target if goal == "max" else best <= target):
        rule = f"{key} {'>=' if goal == 'max' else '<='} {target:g}"
        return ("target_reached", f"Target reached ({rule}): {STOP}.",
                f"the loop's target is reached: best {key} {_fmt(best)} ({_best_at(state)}), and the target was "
                f"{rule}. No new try is made; {STOP} the result.")
    most = state.get("max_tries")
    made = [t for t in state["tries"][1:] if t["status"] != "running"]
    if most and len(made) >= most:
        tries = f"{most} {'try' if most == 1 else 'tries'}"
        return ("max_tries_made", f"That was the last try the loop may make (--max-tries {most}): {STOP}.",
                f"the loop has made its {tries} (--max-tries {most}); best {key} {_fmt(best)} ({_best_at(state)}). "
                f"{STOP[0].upper() + STOP[1:]}.")
    patience, streak = state.get("patience"), _streak(state)
    if patience and len(streak) >= patience:
        tried = "\n".join(f"  try {t['n']}: {t['description']} - {_why(t)}" for t in streak)
        return ("out_of_patience",
                f"That is {len(streak)} tries in a row not kept (--patience {patience}): {STOP}, or try something "
                "different in kind with `testbench loop try --new-direction \"...\"`.",
                f"{len(streak)} tries in a row were not kept (--patience {patience}):\n{tried}\nBest {key} so far: "
                f"{_fmt(best)} ({_best_at(state)}). More of the same is unlikely to help. Either {STOP}, or try an "
                "idea different in kind from these with `testbench loop try --new-direction \"what is different\"` "
                "(the count starts again from it).")
    return None


def _rules(state: dict, when: str = "now") -> str:
    """The loop's stopping rules and where it stands on each, or "" for a loop without any."""
    out = []
    if state.get("target") is not None:
        out.append(f"target {state['key']} {'>=' if state['goal'] == 'max' else '<='} {state['target']:g}")
    if state.get("patience"):
        out.append(f"patience {state['patience']} ({len(_streak(state))} in a row not kept {when})")
    if state.get("max_tries"):
        out.append(f"at most {state['max_tries']} tries "
                   f"({len([t for t in state['tries'][1:] if t['status'] != 'running'])} made)")
    return "; ".join(out)


ENDED = {"stopped": "stopped", "target_reached": "reached its target", "out_of_patience": "ran out of patience",
         "max_tries_made": "made all its tries", "no_budget_left": "has no budget left for another try",
         "out_of_time": "is past its --max-hours"}
HALTED = {"target_reached": "reached its target", "out_of_patience": "run out of patience",
          "max_tries_made": "made all its tries"}


def _tell(root: Path, cfg: dict, state: dict, status: str, why: str, report: Path | None = None) -> None:
    """loop_end, once for each reason the loop ended or a rule fired (the person may be asleep; one message is
    enough)."""
    mark = f"{status}@{len(state['tries']) - 1}"
    if mark in state.setdefault("notified", []):
        return
    state["notified"].append(mark)
    _save(state)
    tries = state["tries"][1:]
    base = state["tries"][0].get("value") if state["tries"] else None
    best = state["best"]
    kept = len([t for t in tries if t.get("kept")])
    then = {"stopped": "", "out_of_patience": " The agent was told to stop the loop, or to try something different "
            "in kind."}.get(status, " The agent was told to stop the loop and report.")
    text = (f"loop {state['id']} on plan {state['plan']} {ENDED[status]}: {state['key']} {_fmt(base)} -> "
            f"{_fmt(best['value'])} ({state['goal']}) in {len(tries)} tries, {kept} kept, ${spent(state):.3f} of "
            f"${state['budget_usd']:.2f}." + (f" {why}" if why else "") + then
            + (f" Best: {_best_at(state)}." if best["try"] is not None else "")
            + (f" Report: {notify.rel(report, root)}" if report else ""))
    notify.send(root, cfg, "loop_end", text, {
        "loop": state["id"], "plan": state["plan"], "status": status, "why": why, "metric": state["metric"],
        "goal": state["goal"], "baseline": base, "best": best["value"], "best_commit": best["commit"],
        "best_run": state["tries"][best["try"]]["run"] if best["try"] is not None else None, "tries": len(tries),
        "kept": kept, "cost_usd": spent(state), "budget_usd": state["budget_usd"], "branch": state["branch"],
        "report": notify.rel(report, root) if report else None})


def start(root: Path, cfg: dict, plan: str, metric: str, budget_usd: float, goal: str = "max",
          max_hours: float = 8.0, min_delta: float = 0.0, approve: bool = False, target: float | None = None,
          patience: int | None = None, max_tries: int | None = None) -> dict:
    if plan not in cfg["plans"]:
        raise LoopError(f"No plan {plan!r}. Plans: {', '.join(cfg['plans'])}")
    if goal not in ("max", "min"):
        raise LoopError("--goal is max (a higher metric is better) or min (lower is better)")
    for flag, n, what in (("--patience", patience, "tries in a row may be reverted before the loop refuses another"),
                          ("--max-tries", max_tries, "tries the loop may make, the baseline not counted")):
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            raise LoopError(f"{flag} is a whole number, 1 or more: how many {what}")
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
        message = (f"Needs approval: a loop on plan {plan} could spend up to ${budget_usd:.2f} in total (each run up "
                   f"to ${worst:.2f}), above {where}. Ask a person; if they agree, rerun with --approve.")
        given = (("max-hours", max_hours, 8.0), ("min-delta", min_delta, 0.0), ("target", target, None),
                 ("patience", patience, None), ("max-tries", max_tries, None))
        how = " ".join([f"testbench loop start {plan} --metric {shlex.quote(metric)} --budget-usd {budget_usd:g}"]
                       + ([f"--goal {goal}"] if goal != "max" else [])
                       + [f"--{k} {v:g}" for k, v, usual in given if v != usual] + ["--approve"])
        p = cfg["plans"][plan]
        notify.approval_needed(root, cfg, message, {"what": f"a loop on plan {plan}", "plan": plan,
                                                    "backend": p["backend"], "gpu": p.get("gpu"),
                                                    "worst_case_usd": float(budget_usd),
                                                    "max_minutes": float(max_hours) * 60}, how)
        raise LoopError(message)
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
                   "started": time.time(), "target": None if target is None else float(target), "patience": patience,
                   "max_tries": max_tries, "notified": []})
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


def _launch(root: Path, cfg: dict, state: dict, description: str, commit: str, new_direction: bool = False) -> dict:
    worst = _room(cfg, state)
    run_cfg = copy.deepcopy(cfg)
    run_cfg["plans"][state["plan"]]["workdir"] = "copy"         # runs leave the project folder alone
    n = len(state["tries"])
    try:
        run_dir = runs.launch(root, run_cfg, state["plan"], approve=state["approved"],
                              origin={"loop": state["id"], "try": n})
    except runs.LaunchRefused as exc:
        raise LoopError(str(exc)) from None
    state["tries"].append({"n": n, "description": description, "commit": commit, "run": run_dir.name,
                           "status": "running", "worst_case_usd": worst, "started": time.time(),
                           **({"new_direction": True} if new_direction else {})})
    state["pending"] = n
    return _save(state)


def try_change(root: Path, cfg: dict, description: str, new_direction: bool = False) -> dict:
    state = judge(root, cfg, _current(root))
    if state["pending"] is not None:
        t = state["tries"][state["pending"]]
        raise LoopError(f"try {t['n']} is still running (run {t['run']}); wait for it (`testbench wait {t['run']}`, then "
                        "`testbench loop status`) before the next try")
    halt = _halt(state)
    if halt and not (new_direction and halt[0] == "out_of_patience"):
        left = _changes(root)
        raise LoopError(halt[2] + (" The edits made for this try are still there, uncommitted: undo them "
                                   "(`git checkout -- .`, or edit them back) before `testbench loop stop`."
                                   if left else ""))
    if new_direction and not halt:
        raise LoopError("--new-direction is for the try after --patience refused one, and this try is not refused"
                        + (f" ({len(_streak(state))} of {state['patience']} tries in a row not kept so far)"
                           if state.get("patience") else " (this loop has no --patience)") + "; try it without")
    top = _git(root, "rev-parse", "--show-toplevel")
    inside = _changes(root)
    elsewhere = [ln for ln in _changes(root, top) if ln not in inside]
    if elsewhere:
        raise LoopError("there are changes outside the project folder, which the loop does not try:\n  "
                        + "\n  ".join(elsewhere[:10]))
    if not inside:
        raise LoopError("nothing changed since the best commit; edit the code, then `testbench loop try \"what changed\"`")
    try:
        _room(cfg, state)
    except LoopError as exc:
        _tell(root, cfg, state, "no_budget_left" if "no room" in str(exc) else "out_of_time", str(exc))
        raise
    _git(root, "add", "-A", "--", ".", *_own(root))
    n = len(state["tries"])
    _git(root, *_identity(root), "commit", "-q", "-m", f"testbench loop try {n}: {description}")
    try:
        return _launch(root, cfg, state, description, _git(root, "rev-parse", "HEAD"), new_direction)
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
    halt = _halt(state)
    if halt:
        t["verdict"] += " " + halt[1]
    _note(root, f"loop {state['id']} " + t["verdict"].split(". The branch")[0] + f" - {t['description']}")
    _save(state)
    if halt:
        _tell(root, cfg, state, halt[0], halt[1].split(":")[0] + ".")
    return state


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
    if _rules(state):
        halt = _halt(state) if state["status"] == ACTIVE else None
        lines.append(f"stopping rules: {_rules(state)}" + (f"\n  -> {halt[1]}" if halt else ""))
    for t in state["tries"]:
        mark = "running" if t["status"] == "running" else ("kept" if t.get("kept") else "reverted")
        if t["n"] == 0:
            mark = "baseline"
        cost = f"${t['cost_usd']:.3f}" if t.get("cost_usd") is not None else f"up to ${t['worst_case_usd']:.2f}"
        lines.append(f"  {t['n']:>3}  {mark:<9} {state['key']}={_fmt(t.get('value'))}  {cost}  {t['run']}  "
                     + _desc(t))
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
    halt = _halt(state)
    state["ended_by"] = halt[0] if halt else None
    path = Path(state["dir"]) / "report.md"
    path.write_text(render(state))
    _save(state)
    _tell(root, cfg, state, "stopped", f"It had {HALTED[halt[0]]}." if halt else "", path)
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
    if _rules(state):
        ended = state.get("ended_by")
        out[-1:] = [f"Stopping rules: {_rules(state, 'at the end')}."
                    + (f" It had {HALTED[ended]} when it was stopped." if ended else ""), ""]
    if kept:
        out += ["## Kept", ""]
        prev = base
        for t in kept:
            moved = f" ({t['value'] - prev:+.4g})" if prev is not None and t.get("value") is not None else ""
            out.append(f"- try {t['n']} `{t['commit'][:9]}`: {_desc(t)} - {state['key']} {_fmt(prev)} -> "
                       f"{_fmt(t.get('value'))}{moved}, run `{t['run']}`, ${t.get('cost_usd', 0):.3f}")
            prev = t.get("value")
        out.append("")
    reverted = [t for t in tries if not t.get("kept")]
    if reverted:
        out += ["## Tried and reverted", ""]
        for t in reverted:
            out.append(f"- try {t['n']}: {_desc(t)} - {_why(t)}")
        out.append("")
    out += ["## Next", "",
            f"- review: `git log --oneline {state['base'][:9]}..{state['branch']}` and "
            f"`git diff {state['base'][:9]}..{state['branch']}`",
            f"- keep the result: `git merge {state['branch']}` (a person decides)",
            f"- the proof for the best result: run `{state['tries'][state['best']['try']]['run']}`"
            if state["best"]["try"] is not None else "- no run passed with the metric; nothing to merge"]
    return "\n".join(out) + "\n"
