"""Money: what a plan can cost at worst, what runs have cost, and whether a launch is allowed.

The worst case of a plan is its hardware's rate times the sum of its steps' max_minutes. Remote steps are
also stopped on the remote side at that limit (a Modal function timeout, a Colab exec timeout), so the
estimate is a bound, not a hope - it holds even if this machine goes offline mid-run.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import config as C

# Modal, US dollars per second (modal.com/pricing, checked 2026-10). Override any of them under `pricing:`.
MODAL_GPU_PER_S = {"B300": 0.001972, "B200": 0.001736, "H200": 0.001261, "H100": 0.001097, "RTX-PRO-6000": 0.000842,
                   "A100-80GB": 0.000694, "A100-40GB": 0.000583, "A100": 0.000583, "L40S": 0.000542, "A10": 0.000306,
                   "A10G": 0.000306, "L4": 0.000222, "T4": 0.000164}
MODAL_CPU_CORE_PER_S = 0.0000131
MODAL_MEM_GIB_PER_S = 0.00000222
# Colab bills compute units. These per-hour rates are approximate and vary by account and region; `colab usage`
# shows your actual rate - put it under `pricing: colab_units_per_hour:`. Pay-as-you-go is ~$10 per 100 units.
COLAB_UNITS_PER_HOUR = {"CPU": 0.1, "T4": 1.8, "L4": 2.0, "G4": 4.0, "A100": 5.4, "H100": 9.0}
COLAB_USD_PER_UNIT = 0.10


def rate_per_minute(cfg: dict, plan: dict) -> float:
    """US dollars per minute for the plan's hardware; 0 for local plans."""
    prices = cfg.get("pricing") or {}
    backend = plan["backend"]
    if backend == "local":
        return 0.0
    gpu = (plan.get("gpu") or "").split(":")[0].upper()
    count = int(plan["gpu"].split(":")[1]) if plan.get("gpu") and ":" in plan["gpu"] else 1
    if backend == "modal":
        gpu_rates = {**MODAL_GPU_PER_S, **(prices.get("modal_gpu_per_s") or {})}
        cpu = float(plan.get("cpu") or cfg["modal"]["cpu"])
        mem = float(plan.get("memory_gb") or cfg["modal"]["memory_gb"])
        per_s = (gpu_rates.get(gpu, 0.0) * count if gpu else 0.0) \
            + cpu * float(prices.get("modal_cpu_core_per_s", MODAL_CPU_CORE_PER_S)) \
            + mem * float(prices.get("modal_mem_gib_per_s", MODAL_MEM_GIB_PER_S))
        return per_s * 60
    units = {**COLAB_UNITS_PER_HOUR, **(prices.get("colab_units_per_hour") or {})}
    return units.get(gpu or "CPU", 0.0) * float(prices.get("colab_usd_per_unit", COLAB_USD_PER_UNIT)) / 60


def estimate(cfg: dict, hardware: dict, minutes: float, label: str) -> dict:
    """Worst case for `minutes` on `hardware` ({backend, gpu, cpu?, memory_gb?}); a remote machine also pays for
    starting up and installing, so a few minutes are added."""
    rate = rate_per_minute(cfg, hardware)
    overhead = 0 if hardware["backend"] == "local" else 5
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
