"""Money: what a plan can cost at worst, what runs have cost, and whether a launch is allowed.

The worst case of a plan is its hardware's rate times the sum of its steps' max_minutes. Remote steps are
also stopped on the remote side at that limit (a Modal function timeout, a watchdog on the Colab VM), so the
estimate is a bound, not a hope - it holds even if this machine goes offline mid-run.

The limits are the project's `budget:`, capped by this machine's ~/.testbench/limits.yaml, which a project can
lower but never raise. Every launch and finish goes to the project's ledger and to this machine's, so the
machine's day limit counts every project together.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from contextlib import contextmanager
from pathlib import Path

import yaml

from . import backends as B, config as C


def rate_per_minute(cfg: dict, hardware: dict) -> float:
    """US dollars per minute for the hardware ({backend, gpu, cpu?, memory_gb?}) of a plan or session; each backend
    prices its own (Modal's and Colab's published rates, overridable under `pricing:`), and local is free."""
    return B.get(hardware["backend"]).rate_per_minute(cfg, hardware)


def estimate(cfg: dict, hardware: dict, minutes: float, label: str) -> dict:
    """Worst case for `minutes` on `hardware` ({backend, gpu, cpu?, memory_gb?}); a remote machine also pays for
    starting up and installing, so a few minutes are added."""
    rate = rate_per_minute(cfg, hardware)
    overhead = B.get(hardware["backend"]).startup_minutes
    return {"what": label, "backend": hardware["backend"], "gpu": hardware.get("gpu"), "usd_per_minute": rate,
            "max_minutes": float(minutes), "worst_case_usd": round(rate * (float(minutes) + overhead), 4)}


def worst_case(cfg: dict, plan_name: str) -> dict:
    """A walk-forward plan runs its steps once per window, so its minutes are its steps' times the windows its data
    cuts (read from the data; a DataError, a ConfigError, says why when they cannot be cut). A look-ahead plan runs
    them once, then once per cut: 1 + lookahead.cuts times."""
    plan = cfg["plans"][plan_name]
    if plan.get("lookahead"):
        cuts = int(plan["lookahead"]["cuts"])
        return {**estimate(cfg, plan, C.plan_minutes(plan) * (1 + cuts),
                           f"plan {plan_name} (its run and {cuts} look-ahead reruns)"),
                "plan": plan_name, "runs": 1 + cuts}
    if not plan.get("walk_forward"):
        return {**estimate(cfg, plan, C.plan_minutes(plan), f"plan {plan_name}"), "plan": plan_name}
    from . import dataseal
    n = dataseal.window_count(cfg, plan_name)
    return {**estimate(cfg, plan, C.plan_minutes(plan) * n, f"plan {plan_name} ({n} walk-forward windows)"),
            "plan": plan_name, "windows": n}


LIMIT_KEYS = ("max_run_usd", "ask_above_usd", "max_day_usd")
LIMITS_TEMPLATE = """\
# agent-testbench: limits for every project on this machine. A project's testbench.yaml can lower them, never
# raise them, and the Claude Code hook asks you before an agent edits this file. Raise them here yourself.
max_run_usd: 5.00        # refuse any run or session that could cost more, in any project
ask_above_usd: 1.00      # above this, a person approves (--approve), in any project
max_day_usd: 20.00       # everything today, all projects together
"""


def user_dir() -> Path:
    """This person's testbench folder: the limits for every project on this machine, and the ledger of all of
    them (TESTBENCH_USER_DIR, for tests; the hook refuses commands that set it)."""
    return Path(os.environ.get("TESTBENCH_USER_DIR") or Path.home() / ".testbench")


def limits_path() -> Path:
    return user_dir() / "limits.yaml"


def user_limits() -> dict:
    """The machine's limits, {} when there are none. A broken file refuses every launch rather than allowing it."""
    p = limits_path()
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise C.ConfigError(f"{p} is not valid YAML ({exc}); nothing can launch until a person fixes it") from None
    if not isinstance(raw, dict):
        raise C.ConfigError(f"{p} must map {', '.join(LIMIT_KEYS)} to US dollars; nothing can launch until it does")
    out = {}
    for key in LIMIT_KEYS:
        if key in raw:
            if not C.number(raw[key]):
                usual = {"max_run_usd": "5.00", "ask_above_usd": "1.00", "max_day_usd": "20.00"}[key]   # the template's
                raise C.ConfigError(f"`{key}` in {p} must be a number of US dollars; nothing can launch until a "
                                    f"person fixes it (e.g. `{key}: {usual}`)")
            out[key] = float(raw[key])
    return out


def write_limits_if_missing() -> Path | None:
    p = limits_path()
    if p.exists():
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LIMITS_TEMPLATE)
    return p


def limits(cfg: dict) -> dict:
    """{key: (dollars, where it is set)}: the project's budget, capped by the machine's limits."""
    machine = user_limits()
    out = {}
    for key in LIMIT_KEYS:
        own = float(cfg["budget"][key])
        if key in machine and not own <= machine[key]:              # NaN in the project: the machine's limit
            out[key] = (machine[key], f"{key} ${machine[key]:.2f} in {limits_path()} (this machine's limit, which "
                                      "a project cannot raise)")
        else:
            out[key] = (own, f"budget.{key} ${own:.2f}")
    return out


@contextmanager
def launching():
    """Held from a launch's check to its ledger entry, machine-wide: two launches at once (two agents, two MCP
    calls) cannot both pass a day limit that only has room for one."""
    from .procs import exclusive
    with exclusive(user_dir() / ".launch.lock"):
        yield


def ledger_path(root: Path) -> Path:
    return Path(root) / ".testbench" / "ledger.jsonl"


def user_ledger_path() -> Path:
    return user_dir() / "ledger.jsonl"


def record(root: Path, entry: dict) -> None:
    """Write an entry to the project's ledger and to this machine's, which holds every project's."""
    line = {"time": dt.datetime.now().isoformat(timespec="seconds"), **entry}
    for p, extra in ((ledger_path(root), {}), (user_ledger_path(), {"root": str(Path(root).resolve())})):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps({**line, **extra}) + "\n")


def _read(p: Path) -> list[dict]:
    """A ledger's entries. One that cannot be read refuses every launch: what was spent is unknown."""
    if not p.exists():
        return []
    rows = []
    for n, line in enumerate(p.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            row = None
        event = row.get("event") if isinstance(row, dict) else None
        key = {"launch": "worst_case_usd", "finish": "usd"}[event] if event in ("launch", "finish") else None
        cost = row.get(key or "usd", 0) if isinstance(row, dict) else None       # key: what _committed_today adds up
        if not isinstance(row, dict) or not isinstance(row.get("time"), str) or not C.number(cost) or key and (
                key not in row or not isinstance(row.get("run"), str)):
            raise C.ConfigError(f"{p} line {n} is not a ledger entry testbench wrote ({line[:80]!r}); what was spent "
                                "cannot be counted, so nothing can launch until a person fixes or removes that line")
        rows.append(row)
    return rows


def entries(root: Path) -> list[dict]:
    return _read(ledger_path(root))


def _committed_today(rows: list[dict]) -> float:
    """Finished runs at their measured cost, unfinished ones at their worst case."""
    today = dt.date.today().isoformat()
    runs: dict[tuple, float] = {}
    for e in rows:
        if e.get("event") not in ("launch", "finish") or not e["time"].startswith(today):
            continue                                 # another kind of entry costs nothing (and may have no run)
        runs[(e.get("root"), e["run"])] = e["worst_case_usd" if e["event"] == "launch" else "usd"]
    return round(sum(runs.values()), 4)


def spent_today(root: Path) -> float:
    """Dollars committed today in this project."""
    return _committed_today(entries(root))


def spent_today_everywhere() -> float:
    """Dollars committed today in every project on this machine."""
    return _committed_today(_read(user_ledger_path()))


def _raise(key: str, where: str) -> str:
    """Who raises a limit, and in which file: the project's budget, or this machine's limits."""
    return (f"have a person raise {key} in {limits_path()}" if "this machine's limit" in where
            else f"have a person raise budget.{key} in {C.CONFIG_NAME}")


def check(cfg: dict, est: dict, approve: bool, how_to_lower: str, command: str = "",
          again: str = "", together: bool = False) -> tuple[bool, str, dict]:
    """(allowed, message, estimate). Over max_run_usd or a day's budget (the project's, or the machine's for all
    projects together): refused. Over ask_above_usd: needs approve=True, which an agent may only pass after a
    person said yes. Each limit is the project's, capped by the machine's limits.

    Every refusal says how to get under the limit: how_to_lower may hold {fit}, the minutes that would fit under
    max_run_usd; command is the launch without --approve (`testbench run smoke`), which a person approves with;
    again is the command that checks again without launching (`testbench estimate smoke`). together: est is several
    runs at once, each already held to max_run_usd (check_together), so only the day limits and the approval apply."""
    lim = limits(cfg)
    cost, what = est["worst_case_usd"], est["what"]
    numbers = (cost, est.get("max_minutes"), est.get("usd_per_minute"), *(v for v, _ in lim.values()))
    if not all(C.number(v) for v in numbers):            # NaN passes every comparison below; so would a bad rate
        return False, (f"Refused: the worst case of {what} cannot be priced: ${cost!r} for {est.get('max_minutes')!r} "
                       f"min at ${est.get('usd_per_minute')!r} a minute, where each must be a number of dollars or "
                       "minutes, 0 or more (and so must the budget's limits). `testbench prices` shows the prices in "
                       "use; `testbench plans` the minutes."), est
    most, where = lim["max_run_usd"]
    if cost > most and not together:
        rate = est["usd_per_minute"]
        fit = est["max_minutes"] + (most - cost) / rate if rate > 0 else 0.0
        fit = int(fit) if C.number(fit) else 0                                     # whole minutes, start-up paid
        fixes = [how_to_lower.format(fit=fit)] * (fit >= 1) + ["use a cheaper GPU or backend",
                                                               _raise("max_run_usd", where)]
        fix = ", ".join(fixes[:-1]) + ", or " + fixes[-1]
        return False, (f"Refused: {what} could cost up to ${cost:.2f} ({est['max_minutes']:.0f} min at "
                       f"${rate:.4f}/min), over {where}. {fix[:1].upper() + fix[1:]}."
                       + (f" `{again}` checks it again." if again else "")), est
    today, day = spent_today(Path(cfg["root"])), float(cfg["budget"]["max_day_usd"])
    if today + cost > day:
        return False, (f"Refused: ${today:.2f} already committed today; {what} could add ${cost:.2f}, over "
                       f"budget.max_day_usd ${day:.2f}. Wait for running work to finish (runs still going count at "
                       f"their worst case) or until tomorrow, launch something cheaper, or have a person raise "
                       f"budget.max_day_usd in {C.CONFIG_NAME}. `testbench spend` shows what today's runs cost."), est
    machine_day = user_limits().get("max_day_usd")
    everywhere = spent_today_everywhere() if machine_day is not None else 0.0
    if machine_day is not None and everywhere + cost > machine_day:
        return False, (f"Refused: ${everywhere:.2f} already committed today across every project on this machine; "
                       f"{what} could add ${cost:.2f}, over max_day_usd ${machine_day:.2f} in {limits_path()} (this "
                       "machine's limit, which a project cannot raise). Wait for running work to finish or until "
                       "tomorrow, launch something cheaper, or have a person raise max_day_usd in that file. "
                       "`testbench spend` shows today's total on this machine."), est
    ask, where = lim["ask_above_usd"]
    if cost > ask and not approve:
        how = f"`{command} --approve`" if command else "--approve"
        return False, (f"Needs approval: {what} could cost up to ${cost:.2f} (above {where}). Ask a person; only "
                       f"they approve it, with {how}."), est
    machine = f"; ${everywhere:.2f} of ${machine_day:.2f} on this machine" if machine_day is not None else ""
    return True, f"Within budget: up to ${cost:.2f} (today so far ${today:.2f} of ${day:.2f}{machine}).", est


def _how_to_lower(plan_name: str, est: dict) -> str:
    windows = (", counted once per window, or cut fewer windows (a longer walk_forward.step)" if est.get("windows")
               else f", counted {est['runs']} times: the plan's run and each look-ahead rerun, or fewer lookahead.cuts"
               if est.get("runs") else "")
    return (f"lower the max_minutes of plan {plan_name}'s steps in {C.CONFIG_NAME} ({{fit}} min in all would "
            f"fit{windows})")


def check_launch(cfg: dict, plan_name: str, approve: bool) -> tuple[bool, str, dict]:
    est = worst_case(cfg, plan_name)
    return check(cfg, est, approve, _how_to_lower(plan_name, est), f"testbench run {plan_name}",
                 f"testbench estimate {plan_name}")


def check_together(cfg: dict, ests: list[dict], approve: bool, what: str, command: str,
                   again: str = "") -> tuple[bool, str, dict]:
    """Several runs launched as one (`testbench repro`: a plan run N times, maybe on other backends). Each must fit
    max_run_usd on its own; all of them together must fit the day limits; above ask_above_usd their total needs a
    person's yes, asked once for the whole - never run by run, which would hide the total behind N small questions.
    ests: each run's worst case (worst_case() of the plan as that run launches it). Returns (allowed, message, the
    total's estimate)."""
    total = {"what": what, "plan": ests[0].get("plan") if ests else None, "runs": len(ests),
             "backend": ", ".join(dict.fromkeys(str(e.get("backend")) for e in ests)),
             "gpu": ", ".join(dict.fromkeys(str(e["gpu"]) for e in ests if e.get("gpu"))) or None,
             "max_minutes": sum(float(e["max_minutes"]) for e in ests) if ests else 0.0,
             "usd_per_minute": max((e["usd_per_minute"] for e in ests), default=0.0),
             "worst_case_usd": round(sum(e["worst_case_usd"] for e in ests), 4) if ests else 0.0}
    if not ests:
        return False, f"Refused: {what} has no runs to launch.", total
    for est in ests:                                     # max_run_usd, NaN and unpriced hardware: run by run
        ok, message, _ = check(cfg, est, True, _how_to_lower(str(est.get("plan")), est), command, again)
        if not ok:
            return False, message, total
    return check(cfg, total, approve, "", command, again, together=True)
