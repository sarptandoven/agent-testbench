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
what to do (for patience: stop, or try something different in kind with --new-direction). When a rule fires or a
try is refused for want of budget or time, testbench.yaml's `notify:` webhook hears about it (loop_rule: the loop is
still open), and again when the loop stops (loop_end); each try's verdict, when asked for (loop_try).

A loop over a trading strategy also says whether its best try is better than luck. Each try's per-period returns
(--returns PATH, an equity.csv or a one-column returns CSV in the run's evidence or work copy; without it, the one
equity.csv the plan keeps as evidence) are kept in the loop's folder, reverted tries included, and every report adds
the deflated Sharpe ratio of the best of all tries so far (trading.stats.deflate_tries) and, with 4 or more tries
over the same periods, the probability of backtest overfitting. --min-deflated-sharpe makes --target count only
when the best try also clears that deflated Sharpe.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import json
import math
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import budget, config as C, notify, procs, report as R, runs
from .trading import files as F, stats

ACTIVE = "running"
OWN = ":(exclude).testbench"       # testbench's own folder (runs, notes, this loop) is never tried or reverted
CHECK = "check"                    # --metric check:SUBSTRING


class LoopError(RuntimeError):
    pass


def loops_dir(root: Path) -> Path:
    return Path(root) / ".testbench" / "loops"


def _save(state: dict) -> dict:
    procs.write_text_atomic(Path(state["dir"]) / "state.json", json.dumps(state, indent=1))
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
    if not notes.resolve().is_relative_to(Path(root).resolve()):     # a link out of the project (C.own_file)
        return
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

# ---- is the best try luck? -------------------------------------------------------------------------------------------

MAX_SERIES = 20_000     # returns kept per try; a longer series keeps only its Sharpe, length, skewness and kurtosis
CONFIDENCE = 0.95       # the deflated Sharpe that calls the best try significant, when --min-deflated-sharpe is not set
PBO_TRIES = 4           # PBO over fewer tries than this says nothing
PBO_BLOCK = 4           # PBO cuts the periods into blocks of at least this many returns (16 blocks at most)
EQUITY = "equity.csv"


def _p(p: float) -> str:
    """A probability as trading.stats words it in a verdict."""
    if p >= 0.99995:
        return "> 0.9999"
    if p < 0.00005:
        return "< 0.0001"
    return f"{p:.2f}" if 0.01 <= p <= 0.99 else f"{p:.4f}"


def read_returns(path: Path) -> tuple[list[float], str | None, str | None]:
    """Per-period returns from an equity.csv in the standard format (r = equity / previous equity - 1) or from a CSV
    of returns (its one column, or the column named return or returns; a header line is optional), with the first and
    last time when the file has times. Raises ValueError (TradingFileError) saying what to write instead."""
    path = Path(path)
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = [(reader.line_num, row) for row in reader if any(c.strip() for c in row)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise F.TradingFileError(f"{path.name} cannot be read as CSV ({exc})") from None
    if not rows:
        raise F.TradingFileError(f"{path.name} is empty: give equity.csv (time, equity) or one return per line")
    head = [c.strip().lower() for c in rows[0][1]]
    if "equity" in head:
        bars = F.read_equity(path, path.name)
        if len(bars) < 3:
            raise F.TradingFileError(f"{path.name} has {C.counted(len(bars), 'row')}: a Sharpe ratio needs at least "
                                     "2 returns, so 3 rows of equity")
        return ([b.equity / a.equity - 1 for a, b in zip(bars, bars[1:])], bars[0].time.isoformat(),
                bars[-1].time.isoformat())
    names = [n for n in ("returns", "return") if n in head]
    col = 0 if len(head) == 1 else head.index(names[0]) if names else None
    if col is None:
        raise F.TradingFileError(f"{path.name} has no equity column and {len(head)} columns ({', '.join(head)}): give "
                                 "equity.csv (time, equity) or a CSV of per-period returns, one column (or a column "
                                 "named return)")

    def number(text: str) -> float | None:
        try:
            x = float(text)
        except ValueError:
            return None
        return x if math.isfinite(x) else None

    body = rows[1:] if number(rows[0][1][col].strip()) is None else rows          # a header line, or none
    values = []
    for line, row in body:
        x = number(row[col].strip()) if col < len(row) else None
        if x is None:
            raise F.TradingFileError(f"{path.name}, line {line}: {(row[col] if col < len(row) else '')!r} is not a "
                                     "return; write one finite number per line, as a fraction (0.01 for 1%)")
        values.append(x)
    at = head.index("time") if "time" in head and len(head) > 1 else None
    times = [row[at].strip() if at < len(row) else "" for _, row in body] if at is not None else []
    return values, (times[0] or None if times else None), (times[-1] or None if times else None)


def _strategy(cfg: dict, plan: str) -> bool:
    """Whether a plan looks like a backtest: a step names an equity.csv or the trading pack."""
    text = json.dumps([[s.get("run"), s.get("artifacts")] for s in cfg["plans"][plan]["steps"]], default=str)
    return EQUITY in text or "agent_testbench.trading" in text


def _returns_file(state: dict, run_dir: Path, rep: dict) -> tuple[Path | None, str | None]:
    """The file with a try's returns, or (None, why not; None when there is nothing to say). --returns PATH is looked
    for in the run's evidence (the artifacts of each step, the last step first), the run's folder, then the run's
    work copy; without it, the one equity.csv the plan keeps as evidence, which is then used for every try."""
    run_dir = Path(run_dir).resolve()
    kept = sorted((run_dir / "steps").glob("*/artifacts"), reverse=True)
    spec = state.get("returns")
    if not spec:
        found = sorted({p.relative_to(a).as_posix() for a in kept for p in a.rglob(EQUITY) if p.is_file()})
        if len(found) > 1:
            return None, (f"the plan keeps {len(found)} {EQUITY} files as evidence ({', '.join(found[:4])}): name "
                          "the one to judge with --returns PATH when starting the loop")
        if not found and not state.get("strategy"):
            return None, None                                  # not a strategy, as far as the plan shows
        if not found:
            return None, (f"the plan keeps no {EQUITY} as evidence: add `artifacts: [out/{EQUITY}]` (its path) to "
                          "the step that writes it, or start the loop with --returns PATH")
        state.update(returns=found[0], returns_how="found")
        spec = found[0]
    work = Path(rep.get("workdir") or run_dir / "work").resolve()
    places = kept + [run_dir] + ([work] if work.is_relative_to(run_dir) else [])
    for place in places:
        p = (place / spec).resolve()
        if p.is_relative_to(run_dir) and p.is_file():
            return p, None
    return None, (f"{spec} is not in run {run_dir.name}'s evidence (steps/*/artifacts/) nor its work copy: keep it as "
                  f"evidence with `artifacts: [{spec}]` on the step that writes it")


def _learn(state: dict, t: dict, run_dir: Path, rep: dict) -> None:
    """Keep a finished try's per-period returns in the loop's folder (only their Sharpe, length, skewness and kurtosis
    when there are more than MAX_SERIES), or why it has none."""
    pinned = state.get("returns")
    path, why = _returns_file(state, run_dir, rep)
    if path is None:
        if why:
            t["returns_missing"] = why if t["status"] == "passed" else f"the run {t['status']}, and {why}"
        return
    if not pinned:
        t["returns_found"] = True
    source = path.relative_to(Path(run_dir).resolve()).as_posix()
    try:
        values, first, last = read_returns(path)
        s = stats.return_stats(values)
    except ValueError as exc:                                  # TradingFileError, StatsError
        t["returns_missing"] = f"{source}: {exc}"
        return
    series = None
    if len(values) <= MAX_SERIES:
        series = f"returns/{t['n']}.json"
        (Path(state["dir"]) / "returns").mkdir(parents=True, exist_ok=True)
        procs.write_text_atomic(Path(state["dir"]) / series, json.dumps(values))
    t["returns"] = {"source": source, "sharpe": s.sharpe, "n_obs": s.n_obs, "skewness": s.skewness,
                    "kurtosis": s.kurtosis, "first": first, "last": last, "series": series}


def _luck(state: dict) -> dict:
    """Whether the loop's best try is better than luck, over every finished try with returns (the baseline and the
    reverted tries too): its deflated Sharpe ratio, against what the best of that many skill-less tries reaches by
    luck, and PBO when it means something. The loop's best is the try it kept for its metric; when that one has no
    returns, the try with the highest Sharpe stands in. Kept in the state after each try, so every report reads the
    same numbers without reading files again."""
    c = state.get("min_deflated_sharpe") or CONFIDENCE
    done = [t for t in state["tries"] if t.get("status") != "running"]
    have = [t for t in done if isinstance(t.get("returns"), dict)]
    out = {"tries": len(have), "of": len(done), "missing": [t["n"] for t in done if t not in have],
           "confidence": c, "line": None, "dsr": None, "significant": None, "judged": None, "best_dsr": None,
           "pbo": None, "pbo_line": None, "pbo_why": None, "why": None}
    if not have:
        why = next((t["returns_missing"] for t in reversed(done) if t.get("returns_missing")), None)
        out["why"] = f"no try has returns: {why}" if why else "no try has returns yet"
        return out
    sharpes = [t["returns"]["sharpe"] for t in have]
    top = have[max(range(len(have)), key=sharpes.__getitem__)]
    best = next((t for t in have if t["n"] == state["best"]["try"]), None)
    judged = best or top
    j = judged["returns"]
    try:
        v = stats.deflate_tries(sharpes, n_obs=top["returns"]["n_obs"], skewness=top["returns"]["skewness"],
                                kurtosis=top["returns"]["kurtosis"], confidence=c)
        d = v.dsr if judged is top else stats.deflated_sharpe_ratio(
            j["sharpe"], j["n_obs"], n_trials=len(have), trials_sharpe_variance=v.trials_sharpe_variance or 0.0,
            skewness=j["skewness"], kurtosis=j["kurtosis"])
    except stats.StatsError as exc:
        out["why"] = f"the deflated Sharpe cannot be worked out: {exc}"
        return out
    out.update(dsr=d, significant=d >= c, judged=judged["n"], best_dsr=d if best else None)
    if len(have) < 2:
        out["why"] = "only 1 try has returns so far; the deflated Sharpe needs 2 or more"
        return out
    line = v.verdict if judged is top else (                   # the same words as trading.stats.deflate_tries
        f"best of {len(have)} tries: deflated Sharpe {_p(d)} (probability the true Sharpe is above 0); "
        f"{'significant' if d >= c else 'not significant'} at {c:g}")
    if d < c:
        line += " - the improvement may be luck" if judged["n"] else " - it may be luck"
    if best is None and state["best"]["try"] is not None:
        line += (f" ({_who(top['n'])}, the highest Sharpe: the loop's best, {_who(state['best']['try'])}, has no "
                 "returns)")
    elif judged is not top:
        line += f"; {_who(top['n'])} had the highest Sharpe (deflated {_p(v.dsr)})"
    out["line"] = line
    out.update(_pbo(state, have))
    return out


def _pbo(state: dict, have: list[dict]) -> dict:
    """PBO by CSCV over the tries x periods table, when every try kept its returns over the same periods."""
    n, rs = len(have), [t["returns"] for t in have]
    if n < PBO_TRIES:
        return {"pbo_why": f"PBO needs {PBO_TRIES} tries with returns or more ({n} so far)"}
    if not all(r.get("series") for r in rs):
        return {"pbo_why": f"PBO needs every try's returns, and a series longer than {MAX_SERIES} periods keeps "
                           "only its Sharpe"}
    if len({r["n_obs"] for r in rs}) > 1 or len({(r.get("first"), r.get("last")) for r in rs}) > 1:
        return {"pbo_why": "PBO needs every try's returns over the same periods, and these differ in length or dates"}
    splits = min(stats.MAX_SPLITS, rs[0]["n_obs"] // PBO_BLOCK // 2 * 2)
    if splits < 2:
        return {"pbo_why": f"PBO needs {2 * PBO_BLOCK} returns per try or more"}
    try:
        cols = [json.loads((Path(state["dir"]) / r["series"]).read_text()) for r in rs]
        res = stats.probability_of_backtest_overfitting(list(zip(*cols)), n_splits=splits)
    except (OSError, ValueError, TypeError) as exc:            # StatsError is a ValueError
        return {"pbo_why": f"PBO cannot be worked out: {exc}"}
    how = ("the best in-sample try is usually not the best out of sample" if res.pbo > 0.5 else
           "the best in-sample try usually stays above the median try out of sample" if res.pbo < 0.5 else
           "the best in-sample try falls below the median try out of sample as often as not")
    return {"pbo": res.pbo, "pbo_line": f"PBO {res.pbo:.2f} over {n} tries: {how}"}


def _luck_of(state: dict) -> dict:
    """What _luck found after the latest try ({} before the first, or in a loop started before testbench had it)."""
    luck = state.get("luck")
    return luck if isinstance(luck, dict) else {}


def luck_lines(state: dict) -> list[str]:
    """The deflated Sharpe line and the PBO line, as each report shows them (none for fewer than 2 tries with
    returns)."""
    luck = _luck_of(state)
    return [x for x in (luck.get("line"), luck.get("pbo_line")) if isinstance(x, str) and x]


def _sentence(line: str) -> str:
    return line[0].upper() + line[1:] + "."


def _sentences(state: dict) -> str:
    """luck_lines as sentences, for a notification or a paragraph; "" when there are none."""
    return " ".join(map(_sentence, luck_lines(state)))


def _who(n: int) -> str:
    return "the baseline" if n == 0 else f"try {n}"


def _luck_fields(state: dict) -> dict:
    luck = _luck_of(state)
    return {"luck": luck.get("line"), "deflated_sharpe": luck.get("dsr") if luck.get("line") else None,
            "deflated_sharpe_tries": luck.get("tries") if luck.get("line") else None, "pbo": luck.get("pbo")}


def _target_rule(state: dict) -> str:
    return f"{state['key']} {'>=' if state['goal'] == 'max' else '<='} {state['target']:g}"


def _clears(state: dict) -> tuple[bool, str]:
    """Whether the loop's best try clears --min-deflated-sharpe (always, when it is not set); if not, why."""
    least = state.get("min_deflated_sharpe")
    if least is None:
        return True, ""
    luck = _luck_of(state)
    d = luck.get("best_dsr")
    if d is None:
        return False, (f"the best try ({_who(state['best']['try'])}) has no returns to judge it by"
                       if luck.get("tries") else luck.get("why") or "no try has returns yet")
    if d >= least:
        return True, ""
    return False, f"its deflated Sharpe is {_p(d)} over {C.counted(luck['tries'], 'try', 'tries')}"


def _short(state: dict, again: bool = False) -> str:
    """What a verdict says when the best reaches --target but not --min-deflated-sharpe: the target does not count
    (again: briefly, for the tries after the one that reached it)."""
    best, target = state["best"]["value"], state.get("target")
    if target is None or best is None or not (best >= target if state["goal"] == "max" else best <= target):
        return ""
    ok, why = _clears(state)
    if ok:
        return ""
    least = state["min_deflated_sharpe"]
    if again:
        return f"The target ({_target_rule(state)}) still does not count: {why} (--min-deflated-sharpe {least:g})."
    return (f"Best {state['key']} {_fmt(best)} reaches the target ({_target_rule(state)}), but the target counts only "
            f"once the best try's deflated Sharpe is at least {least:g} (--min-deflated-sharpe): {why}, so the loop "
            f"goes on. A clearly better strategy raises it, while more tries of the same kind lower it; or {STOP}.")


def _other_runs(state: dict) -> int:
    """Runs of the loop's plan since it started that were not its tries: the deflated Sharpe does not count them."""
    root, n = Path(state["dir"]).parents[2], 0
    for p in runs.all_runs(root):
        try:
            s = runs.read_state(p)
        except (OSError, ValueError):
            continue
        origin = s.get("origin") if isinstance(s.get("origin"), dict) else {}
        if s.get("plan") == state["plan"] and C.number(s.get("created")) and s["created"] >= state["started"] \
                and origin.get("loop") != state["id"]:
            n += 1
    return n


def _luck_notes(state: dict, final: bool = False) -> list[str]:
    """Where the returns came from, the tries without any, and what is not counted: for status and the report."""
    luck, out = _luck_of(state), []
    if state.get("returns"):
        how = (f", the one {EQUITY} the plan keeps as evidence" if state.get("returns_how") == "found" else "")
        out.append(f"returns: {state['returns']} in each try's run{how}; per period, no risk-free rate taken off")
    missing = [t for t in state["tries"] if t["n"] in (luck.get("missing") or [])]
    if missing and luck.get("tries"):
        out.append("not counted, no returns: "
                   + "; ".join(f"{_who(t['n'])} ({t.get('returns_missing') or 'none kept'})" for t in missing[:6])
                   + (f"; and {len(missing) - 6} more" if len(missing) > 6 else ""))
    if luck.get("line") and luck.get("pbo_why"):
        out.append(f"no PBO: {luck['pbo_why']}")
    if luck.get("tries"):
        others = _other_runs(state)
        if others or final:
            out.append("only this loop's tries are counted (the baseline too, kept or reverted): "
                       + (f"{C.counted(others, 'other run')} of plan {state['plan']} since it started "
                          f"{'is' if others == 1 else 'are'} not" if others else
                          f"runs of plan {state['plan']} outside it are not")
                       + f" (`testbench history {state['plan']}`); if they tried variants, the true number of tries "
                         "is higher and the deflated Sharpe lower")
    return out


def _halt(state: dict) -> tuple[str, str, str] | None:
    """The stopping rule that refuses another try, if one does: (status, what a verdict says, what a refusal says)."""
    key, best, goal = state["key"], state["best"]["value"], state["goal"]
    target = state.get("target")
    if target is not None and best is not None and (best >= target if goal == "max" else best <= target) \
            and _clears(state)[0]:
        rule, least = _target_rule(state), state.get("min_deflated_sharpe")
        if least is not None:
            rule += f", deflated Sharpe {_p(_luck_of(state)['best_dsr'])} >= {least:g}"
        luck = luck_lines(state)
        return ("target_reached", f"Target reached ({rule}): {STOP}.",
                f"the loop's target is reached: best {key} {_fmt(best)} ({_best_at(state)}), and the target was "
                f"{rule}." + (f" {_sentence(luck[0])}" if luck else "") + f" No new try is made; {STOP} the result.")
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
                f"That is {C.counted(len(streak), 'try', 'tries')} in a row not kept (--patience {patience}): "
                f"{STOP}, or try something different in kind with `testbench loop try --new-direction \"...\"`.",
                f"{C.counted(len(streak), 'try', 'tries')} in a row "
                f"{'was' if len(streak) == 1 else 'were'} not kept (--patience {patience}):\n{tried}\nBest {key} so far: "
                f"{_fmt(best)} ({_best_at(state)}). More of the same is unlikely to help. Either {STOP}, or try an "
                "idea different in kind from these with `testbench loop try --new-direction \"what is different\"` "
                "(the count starts again from it).")
    return None


def _rules(state: dict, when: str = "now") -> str:
    """The loop's stopping rules and where it stands on each, or "" for a loop without any."""
    out = []
    if state.get("target") is not None:
        least = state.get("min_deflated_sharpe")
        out.append(f"target {_target_rule(state)}"
                   + (f" with a deflated Sharpe of at least {least:g}" if least is not None else ""))
    if state.get("patience"):
        out.append(f"patience {state['patience']} ({len(_streak(state))} in a row not kept {when})")
    if state.get("max_tries"):
        out.append(f"at most {C.counted(state['max_tries'], 'try', 'tries')} "
                   f"({len([t for t in state['tries'][1:] if t['status'] != 'running'])} made)")
    return "; ".join(out)


ENDED = {"stopped": "stopped", "target_reached": "reached its target", "out_of_patience": "ran out of patience",
         "max_tries_made": "made all its tries", "no_budget_left": "has no budget left for another try",
         "out_of_time": "is past its --max-hours"}
HALTED = {"target_reached": "reached its target", "out_of_patience": "run out of patience",
          "max_tries_made": "made all its tries"}


def _tell(root: Path, cfg: dict, state: dict, status: str, why: str, report: Path | None = None) -> None:
    """loop_rule once for each rule that fired or reason a try was refused (the loop is still open: the agent was
    told what to do; the person may be asleep, and one message is enough), loop_end when the loop is stopped."""
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
            f"{_fmt(best['value'])} ({state['goal']}) in {C.counted(len(tries), 'try', 'tries')}, {kept} kept, "
            f"${spent(state):.3f} of ${state['budget_usd']:.2f}." + (f" {why}" if why else "")
            + (f" {_sentences(state)}" if luck_lines(state) else "") + then
            + (f" Best: {_best_at(state)}." if best["try"] is not None else "")
            + (f" Report: {notify.rel(report, root)}" if report else ""))
    notify.send(root, cfg, "loop_end" if status == "stopped" else "loop_rule", text, {
        "loop": state["id"], "plan": state["plan"], "status": status, "why": why, "open": status != "stopped",
        "ended_by": state.get("ended_by") if status == "stopped" else None, "metric": state["metric"],
        "goal": state["goal"], "baseline": base, "best": best["value"], "best_commit": best["commit"],
        "best_run": state["tries"][best["try"]]["run"] if best["try"] is not None else None, "tries": len(tries),
        "kept": kept, "cost_usd": spent(state), "budget_usd": state["budget_usd"], "branch": state["branch"],
        "report": notify.rel(report, root) if report else None, **_luck_fields(state)})


def start(root: Path, cfg: dict, plan: str, metric: str, budget_usd: float, goal: str = "max",
          max_hours: float = 8.0, min_delta: float = 0.0, approve: bool = False, target: float | None = None,
          patience: int | None = None, max_tries: int | None = None, returns: str | None = None,
          min_deflated_sharpe: float | None = None) -> dict:
    if plan not in cfg["plans"]:
        raise LoopError(C.no_plan(cfg, plan))
    if goal not in ("max", "min"):
        raise LoopError("--goal is max (a higher metric is better) or min (lower is better)")
    for flag, n, what in (("--patience", patience, "tries in a row may be reverted before the loop refuses another"),
                          ("--max-tries", max_tries, "tries the loop may make, the baseline not counted")):
        if n is not None and (isinstance(n, bool) or not isinstance(n, int) or n < 1):
            raise LoopError(f"{flag} is a whole number, 1 or more: how many {what}")
    for flag, v, least in (("--budget-usd", budget_usd, 0), ("--max-hours", max_hours, 1e-9), ("--min-delta", min_delta, 0)):
        if not (C.number(v) and v >= least):         # nan would pass every limit; true is not 1 dollar here
            raise LoopError(f"{flag} is a number of {'at least 0' if least == 0 else 'more than 0'}; got {v!r}")
    path, key = parse_metric(metric)
    if returns is not None:
        if not isinstance(returns, str) or not returns.strip() or "\0" in returns:
            raise LoopError("--returns is the path of each try's equity.csv (or of a CSV of its returns) in its run, "
                            f"e.g. out/{EQUITY}")
        parts = Path(returns.strip()).as_posix().split("/")
        if Path(returns.strip()).is_absolute() or ".." in parts or re.match(r"^[A-Za-z]:", returns.strip()) \
                or returns.strip().startswith(("/", "\\")):              # rooted: not inside the project on Windows
            raise LoopError(f"--returns {returns!r} is looked for inside each try's run (its evidence or its work "
                            f"copy), so it is a relative path inside the project, e.g. out/{EQUITY}")
        returns = "/".join(x for x in parts if x not in ("", "."))
    if min_deflated_sharpe is not None:
        if not C.number(min_deflated_sharpe) or not 0.5 <= min_deflated_sharpe < 1:
            raise LoopError("--min-deflated-sharpe is the deflated Sharpe ratio (the probability that the best try's "
                            "true Sharpe is above 0, once every try is counted) the best try must reach for --target "
                            f"to count: from 0.5 up to (not including) 1, such as 0.95; got {min_deflated_sharpe!r}")
        if target is None:
            raise LoopError("--min-deflated-sharpe makes --target count only once the best try's deflated Sharpe "
                            "clears it, so give --target VALUE as well (without either, the loop still reports the "
                            "deflated Sharpe of its best try, at 0.95)")

    def again(metric=metric, budget_usd=budget_usd) -> str:
        """This loop start as a person types it (without --approve), with what a refusal says to change."""
        given = (("max-hours", max_hours, 8.0), ("min-delta", min_delta, 0.0), ("target", target, None),
                 ("patience", patience, None), ("max-tries", max_tries, None),
                 ("min-deflated-sharpe", min_deflated_sharpe, None))
        return " ".join([f"testbench loop start {plan} --metric {shlex.quote(metric)} --budget-usd {budget_usd:g}"]
                        + ([f"--goal {goal}"] if goal != "max" else [])
                        + [f"--{k} {v:g}" for k, v, usual in given if v != usual]
                        + ([f"--returns {shlex.quote(returns)}"] if returns else []))

    checks = [f"{p}:{k}" for s in cfg["plans"][plan]["steps"] for p, keys in (s["expect"].get("json") or {}).items()
              if isinstance(keys, dict) for k in keys]
    if path != CHECK and f"{path}:{key}" not in checks:
        raise LoopError(f"plan {plan} checks no {key!r} in {path}, so its runs would not report the metric. "
                        + (f"The numbers it checks: {', '.join(checks[:8])}; e.g. `{again(checks[0])}`. Or give"
                           if checks else "Give")
                        + f" a step of plan {plan} in {C.CONFIG_NAME} an expectation such as "
                        f"`expect: {{json: {{{path}: {{{key}: \">= 0\"}}}}}}`, then `{again()}`.")
    if active(root):
        raise LoopError(f"loop {active(root)['id']} is still running here; stop it first with `testbench loop stop`")
    if _git(root, "rev-parse", "--is-inside-work-tree", check=False) != "true":
        raise LoopError("the loop keeps and reverts changes with git, and this folder is not in a git repository: "
                        "`git init`, commit the project (`git add -A && git commit -m \"before the loop\"`), then "
                        f"start the loop: `{again()}`")
    dirty = _changes(root, _git(root, "rev-parse", "--show-toplevel"))
    if dirty:
        raise LoopError("the repository has uncommitted changes, which a revert could lose. Commit or stash them "
                        f"(`git stash -u`), then start the loop: `{again()}`. The changes:\n  " + "\n  ".join(dirty[:10]))
    worst = budget.worst_case(cfg, plan)["worst_case_usd"]
    if worst > budget_usd:
        raise LoopError(f"--budget-usd ${budget_usd:.2f} has no room for one run of {plan} (worst case ${worst:.2f}); "
                        f"give the loop at least that: `{again(budget_usd=math.ceil(worst * 100) / 100)}`")
    ok, message, _ = budget.check_launch(cfg, plan, approve=True)
    if not ok:
        raise LoopError(message)
    ask, where = budget.limits(cfg)["ask_above_usd"]
    if budget_usd > ask and not approve:
        how = f"{again()} --approve"
        message = (f"Needs approval: a loop on plan {plan} could spend up to ${budget_usd:.2f} in total (each run up "
                   f"to ${worst:.2f}), above {where}. Ask a person; only they approve it, with `{how}`.")
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
                   "max_tries": max_tries, "notified": [], "returns": returns or None,
                   "returns_how": "given" if returns else None, "strategy": bool(returns) or _strategy(cfg, plan),
                   "min_deflated_sharpe": None if min_deflated_sharpe is None else float(min_deflated_sharpe),
                   "luck": None})
    return _launch(root, cfg, state, "baseline (the code as it was)", base)


def _room(cfg: dict, state: dict) -> float:
    """The worst case of one more run, if the loop has room for it in money and time."""
    worst = budget.worst_case(cfg, state["plan"])["worst_case_usd"]
    if spent(state) + worst > state["budget_usd"]:
        raise LoopError(f"the loop's budget has no room for another run: ${spent(state):.2f} of "
                        f"${state['budget_usd']:.2f} spent, and a run could cost up to ${worst:.2f}. Stop the loop with "
                        "`testbench loop stop` (its report keeps the best try); going on takes a new loop, with a "
                        "budget a person agrees to.")
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
                        + (f" ({len(_streak(state))} of {C.counted(state['patience'], 'try', 'tries')} in a row "
                           "not kept so far)"
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
    kept_notes = notes.read_text() if notes.exists() and notes.resolve().is_relative_to(Path(root).resolve()) else None
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
    try:
        _learn(state, t, run_dir, rep)
    except Exception as exc:                           # the luck check never keeps a try from being judged
        t["returns_missing"] = f"its returns could not be kept ({type(exc).__name__}: {exc})"[:300]
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
    try:
        state["luck"] = _luck(state)                   # over every try so far, this one too, kept or reverted
    except Exception as exc:                           # nor the verdict from being given
        state["luck"] = {"why": f"the deflated Sharpe could not be worked out ({type(exc).__name__}: {exc})"[:300]}
    t["luck"] = luck_lines(state)
    halt = _halt(state)
    if halt:
        t["verdict"] += " " + halt[1]
    elif _short(state):
        t["verdict"] += " " + _short(state, again=not t["kept"])
    _note(root, f"loop {state['id']} " + t["verdict"].split(". The branch")[0] + f" - {t['description']}"
          + (f" ({t['luck'][0]})" if t["luck"] else ""))
    _save(state)
    notify.send(root, cfg, "loop_try", f"loop {state['id']} on plan {state['plan']}: {t['verdict']} ({_desc(t)})"
                + (f" {_sentences(state)}" if t["luck"] else ""), {
        "loop": state["id"], "plan": state["plan"], "try": t["n"], "description": t["description"],
        "status": "baseline" if baseline else "kept" if t["kept"] else "reverted", "run_id": t["run"],
        "run_status": t["status"], "metric": state["metric"], "value": value, "best": state["best"]["value"],
        "best_commit": state["best"]["commit"], "cost_usd": t["cost_usd"],
        "new_direction": bool(t.get("new_direction")), **_luck_fields(state)})
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
             f"{C.counted(len(state['tries']) - 1 if state['tries'] else 0, 'try', 'tries')}, {len(kept)} kept; "
             f"${used:.3f} of ${state['budget_usd']:.2f} spent" + (f"; {left:.1f} h left" if state["status"] == ACTIVE else ""),
             f"branch {state['branch']} (from {state['original_branch']} at {state['base'][:9]})"]
    if _rules(state):
        halt = _halt(state) if state["status"] == ACTIVE else None
        short = _short(state) if state["status"] == ACTIVE and not halt else ""
        lines.append(f"stopping rules: {_rules(state)}"
                     + (f"\n  -> {halt[1]}" if halt else f"\n  -> {short}" if short else ""))
    if luck_lines(state):
        lines.append("luck: " + "\n  ".join(luck_lines(state)))
    elif state.get("strategy") or state.get("returns"):
        lines.append(f"luck: not judged yet - {_luck_of(state).get('why') or 'no try has finished'}")
    lines += [f"  {note}" for note in _luck_notes(state)]
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
    _tell(root, cfg, state, "stopped", f"It had {HALTED[halt[0]]}." if halt else _unmet(state), path)
    return state, path


def _unmet(state: dict) -> str:
    """What the report says when the best reached --target but not --min-deflated-sharpe; "" otherwise."""
    if not _short(state):
        return ""
    return (f"Its best reached the target ({_target_rule(state)}) but not --min-deflated-sharpe "
            f"{state['min_deflated_sharpe']:g} ({_clears(state)[1]}), so the target was not met.")


def render(state: dict) -> str:
    tries = [t for t in state["tries"] if t["n"] > 0]
    kept = [t for t in tries if t.get("kept")]
    base = state["tries"][0].get("value") if state["tries"] else None
    hours = ((state.get("stopped") or time.time()) - state["started"]) / 3600
    out = [f"# Loop `{state['id']}` on plan `{state['plan']}`: {state['key']} {_fmt(base)} -> "
           f"{_fmt(state['best']['value'])} ({state['goal']})", "",
           f"{C.counted(len(tries), 'try', 'tries')}, {len(kept)} kept, ${spent(state):.3f} of "
           f"${state['budget_usd']:.2f}, {hours:.1f} h. "
           f"Branch `{state['branch']}` from `{state['original_branch']}` at `{state['base'][:9]}`.", ""]
    luck = luck_lines(state)
    if _rules(state):
        ended = state.get("ended_by")
        target = ended == "target_reached" and luck
        out[-1:] = [f"Stopping rules: {_rules(state, 'at the end')}."
                    + (f" It had {HALTED[ended]} when it was stopped" + (f"; {luck[0]}." if target else ".")
                       if ended else "") + (f" {_unmet(state)}" if _unmet(state) else ""), ""]
    if luck or state.get("strategy") or state.get("returns"):
        out += ["## Is the best try luck?", ""]
        out += [f"- {x}" for x in luck] or [f"- not judged: {_luck_of(state).get('why') or 'no try finished'}"]
        out += [f"- {note}" for note in _luck_notes(state, final=True)] + [""]
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
