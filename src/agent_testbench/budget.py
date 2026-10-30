"""Money: what a plan can cost at worst, what runs have cost, and whether a launch is allowed.

The worst case of a plan is its hardware's rate times the sum of its steps' max_minutes. Remote steps are
also stopped on the remote side at that limit (a Modal function timeout, a Colab exec timeout), so the
estimate is a bound, not a hope - it holds even if this machine goes offline mid-run.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

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


def ledger_path(root: Path) -> Path:
    return Path(root) / ".testbench" / "ledger.jsonl"


def record(root: Path, entry: dict) -> None:
    p = ledger_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"), **entry}) + "\n")


def entries(root: Path) -> list[dict]:
    p = ledger_path(root)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def spent_today(root: Path) -> float:
    """Dollars committed today: finished runs at their measured cost, unfinished ones at their worst case."""
    today = dt.date.today().isoformat()
    runs: dict[str, float] = {}
    for e in entries(root):
        if not e["time"].startswith(today):
            continue
        if e["event"] == "launch":
            runs[e["run"]] = e["worst_case_usd"]
        elif e["event"] == "finish":
            runs[e["run"]] = e["usd"]
    return round(sum(runs.values()), 4)


def check(cfg: dict, est: dict, approve: bool, how_to_lower: str) -> tuple[bool, str, dict]:
    """(allowed, message, estimate). Over max_run_usd or the day's budget: refused. Over ask_above_usd: needs
    approve=True, which an agent may only pass after a person said yes."""
    b = cfg["budget"]
    cost, what = est["worst_case_usd"], est["what"]
    if cost > b["max_run_usd"]:
        return False, (f"Refused: {what} could cost up to ${cost:.2f} ({est['max_minutes']:.0f} min at "
                       f"${est['usd_per_minute']:.4f}/min), over budget.max_run_usd ${b['max_run_usd']:.2f}. "
                       f"{how_to_lower}, use a cheaper GPU, or have a person raise the budget."), est
    today = spent_today(Path(cfg["root"]))
    if today + cost > b["max_day_usd"]:
        return False, (f"Refused: ${today:.2f} already committed today; {what} could add ${cost:.2f}, over "
                       f"budget.max_day_usd ${b['max_day_usd']:.2f}."), est
    if cost > b["ask_above_usd"] and not approve:
        return False, (f"Needs approval: {what} could cost up to ${cost:.2f} (above budget.ask_above_usd "
                       f"${b['ask_above_usd']:.2f}). Ask a person; if they agree, rerun with --approve."), est
    return True, f"Within budget: up to ${cost:.2f} (today so far ${today:.2f} of ${b['max_day_usd']:.2f}).", est


def check_launch(cfg: dict, plan_name: str, approve: bool) -> tuple[bool, str, dict]:
    return check(cfg, worst_case(cfg, plan_name), approve, "Lower the steps' max_minutes")
