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
    plan = cfg["plans"][plan_name]
    return {**estimate(cfg, plan, C.plan_minutes(plan), f"plan {plan_name}"), "plan": plan_name}


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
            if not isinstance(raw[key], (int, float)) or isinstance(raw[key], bool) or raw[key] < 0:
                raise C.ConfigError(f"`{key}` in {p} must be a number of US dollars; nothing can launch until a "
                                    "person fixes it")
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
        if key in machine and machine[key] < own:
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
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def entries(root: Path) -> list[dict]:
    return _read(ledger_path(root))


def _committed_today(rows: list[dict]) -> float:
    """Finished runs at their measured cost, unfinished ones at their worst case."""
    today = dt.date.today().isoformat()
    runs: dict[tuple, float] = {}
    for e in rows:
        if not e["time"].startswith(today):
            continue
        key = (e.get("root"), e["run"])
        if e["event"] == "launch":
            runs[key] = e["worst_case_usd"]
        elif e["event"] == "finish":
            runs[key] = e["usd"]
    return round(sum(runs.values()), 4)


def spent_today(root: Path) -> float:
    """Dollars committed today in this project."""
    return _committed_today(entries(root))


def spent_today_everywhere() -> float:
    """Dollars committed today in every project on this machine."""
    return _committed_today(_read(user_ledger_path()))


def check(cfg: dict, est: dict, approve: bool, how_to_lower: str) -> tuple[bool, str, dict]:
    """(allowed, message, estimate). Over max_run_usd or a day's budget (the project's, or the machine's for all
    projects together): refused. Over ask_above_usd: needs approve=True, which an agent may only pass after a
    person said yes. Each limit is the project's, capped by the machine's limits."""
    lim = limits(cfg)
    cost, what = est["worst_case_usd"], est["what"]
    most, where = lim["max_run_usd"]
    if cost > most:
        return False, (f"Refused: {what} could cost up to ${cost:.2f} ({est['max_minutes']:.0f} min at "
                       f"${est['usd_per_minute']:.4f}/min), over {where}. "
                       f"{how_to_lower}, use a cheaper GPU, or have a person raise the budget."), est
    today, day = spent_today(Path(cfg["root"])), float(cfg["budget"]["max_day_usd"])
    if today + cost > day:
        return False, (f"Refused: ${today:.2f} already committed today; {what} could add ${cost:.2f}, over "
                       f"budget.max_day_usd ${day:.2f}."), est
    machine_day = user_limits().get("max_day_usd")
    everywhere = spent_today_everywhere() if machine_day is not None else 0.0
    if machine_day is not None and everywhere + cost > machine_day:
        return False, (f"Refused: ${everywhere:.2f} already committed today across every project on this machine; "
                       f"{what} could add ${cost:.2f}, over max_day_usd ${machine_day:.2f} in {limits_path()} (this "
                       "machine's limit, which a project cannot raise)."), est
    ask, where = lim["ask_above_usd"]
    if cost > ask and not approve:
        return False, (f"Needs approval: {what} could cost up to ${cost:.2f} (above {where}). Ask a person; if "
                       "they agree, rerun with --approve."), est
    machine = f"; ${everywhere:.2f} of ${machine_day:.2f} on this machine" if machine_day is not None else ""
    return True, f"Within budget: up to ${cost:.2f} (today so far ${today:.2f} of ${day:.2f}{machine}).", est


def check_launch(cfg: dict, plan_name: str, approve: bool) -> tuple[bool, str, dict]:
    return check(cfg, worst_case(cfg, plan_name), approve, "Lower the steps' max_minutes")
