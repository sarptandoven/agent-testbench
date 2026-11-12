"""budget.check and check_launch with any numbers in: a launch allowed is always priced, inside max_run_usd and the day
limits (the project's and this machine's), and approved when above ask_above_usd; one inside all of them is never
refused; and nothing in a ledger, a limits file or a price makes it crash (a ConfigError refuses everything instead).
TESTBENCH_FUZZ_EXAMPLES=N runs N examples of each (tests/trust_helpers.py)."""
import datetime as dt
import json
import math

import pytest
import yaml

pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st  # noqa: E402

from agent_testbench import budget, config as C  # noqa: E402
from trust_helpers import fuzz  # noqa: E402

KEYS = ("max_run_usd", "ask_above_usd", "max_day_usd")
money = st.one_of(st.floats(0, 100, allow_nan=False), st.sampled_from([0, 0.5, 1, 2, 5, 10, 20]),
                  st.integers(0, 1000))
odd = st.sampled_from([math.nan, math.inf, -math.inf, -1.0, -0.0, 1e308, 10 ** 400, True, None, "1", [1]])
anything = st.one_of(money, odd)


def number(v) -> bool:
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    except OverflowError:
        return False


def rows(history, roots: dict) -> list[dict]:
    """Ledger entries for runs launched here or in another project, today or yesterday, finished or still going."""
    out = []
    for i, (where, finished, worst, spent, yesterday) in enumerate(history):
        when = (dt.datetime.now() - dt.timedelta(days=yesterday)).isoformat(timespec="seconds")
        out.append({"time": when, "event": "launch", "run": f"r{i}", "worst_case_usd": worst, "root": roots[where]})
        if finished:
            out.append({"time": when, "event": "finish", "run": f"r{i}", "usd": spent, "root": roots[where]})
    return out


def committed(entries, here=None) -> float:
    """What the ledger says was committed today, worked out here independently of budget.py."""
    today, runs = dt.date.today().isoformat(), {}
    for e in entries:
        if e["time"].startswith(today) and (here is None or e.get("root") == here):
            runs[(e.get("root"), e["run"])] = e["worst_case_usd"] if e["event"] == "launch" else e["usd"]
    return round(sum(runs.values()), 4)


runs_today = st.lists(st.tuples(st.sampled_from(["here", "elsewhere"]), st.booleans(), money, money, st.booleans()),
                      max_size=5)
machine = st.one_of(st.none(), st.fixed_dictionaries({}, optional={k: money for k in KEYS}))


@fuzz(400)
@given(st.fixed_dictionaries({k: money for k in KEYS}), machine, runs_today,
       st.one_of(money, odd), st.one_of(money, odd), st.one_of(money, odd), st.booleans())
def test_an_allowed_launch_is_inside_every_limit_and_one_inside_them_is_allowed(
        tmp_path, machine_limits, own, mine, history, cost, minutes, rate, approve):
    root = tmp_path / "p"
    root.mkdir(exist_ok=True)
    machine_limits.mkdir(exist_ok=True)
    (machine_limits / "limits.yaml").unlink(missing_ok=True)
    if mine is not None:
        (machine_limits / "limits.yaml").write_text(yaml.safe_dump(mine))
    entries = rows(history, {"here": str(root.resolve()), "elsewhere": str(tmp_path / "other")})
    budget.ledger_path(root).parent.mkdir(exist_ok=True)
    budget.ledger_path(root).write_text("".join(json.dumps({k: v for k, v in e.items() if k != "root"}) + "\n"
                                                for e in entries if e["root"] == str(root.resolve())))
    budget.user_ledger_path().write_text("".join(json.dumps(e) + "\n" for e in entries))
    cfg = {"root": str(root), "budget": dict(own)}
    est = {"what": "plan x", "worst_case_usd": cost, "max_minutes": minutes, "usd_per_minute": rate}
    ok, msg, _ = budget.check(cfg, est, approve, "lower it ({fit} min would fit)", "testbench run x",
                              "testbench estimate x")
    assert isinstance(ok, bool) and isinstance(msg, str) and msg

    mine = mine or {}
    cap = {k: min(float(own[k]), float(mine[k])) if k in mine else float(own[k]) for k in KEYS}
    spent_here, spent_all = committed(entries, str(root.resolve())), committed(entries)
    priced = number(cost) and number(minutes) and number(rate)
    inside = (priced and cost <= cap["max_run_usd"] and spent_here + cost <= float(own["max_day_usd"])
              and ("max_day_usd" not in mine or spent_all + cost <= float(mine["max_day_usd"])))
    assert ok == bool(inside and (approve or cost <= cap["ask_above_usd"])), (ok, msg, own, mine, cost, approve)
    if not ok:
        assert msg.startswith(("Refused:", "Needs approval:")), msg
        assert msg.startswith("Needs approval:") == bool(inside), msg
    if inside and not approve and cost > cap["ask_above_usd"]:
        assert "testbench run x --approve" in msg, "a refusal says how a person approves it"


@fuzz(300)
@given(st.dictionaries(st.sampled_from([*KEYS, "max_runs", 3]), anything, max_size=4)
       | st.sampled_from(["", "[1, 2]", "max_run_usd: [", "\t", "max_run_usd: .nan", "max_day_usd: 1e999", "x"]))
def test_any_limits_file_is_limits_or_a_refusal(tmp_path, machine_limits, doc):
    machine_limits.mkdir(exist_ok=True)
    text = doc if isinstance(doc, str) else yaml.safe_dump(doc)
    (machine_limits / "limits.yaml").write_text(text)
    try:
        got = budget.user_limits()
    except C.ConfigError as exc:
        assert "nothing can launch" in str(exc)
        return
    assert set(got) <= set(KEYS) and all(number(v) for v in got.values()), got


line = st.one_of(st.text(max_size=20), st.builds(json.dumps, st.dictionaries(
    st.sampled_from(["time", "event", "run", "usd", "worst_case_usd", "root"]),
    st.one_of(anything, st.sampled_from(["launch", "finish", "note", dt.datetime.now().isoformat()])), max_size=6)))


@fuzz(400)
@given(st.lists(line, max_size=5))
def test_any_ledger_is_counted_or_refuses_everything(tmp_path, machine_limits, lines):
    root = tmp_path / "p"
    budget.ledger_path(root).parent.mkdir(parents=True, exist_ok=True)
    budget.ledger_path(root).write_text("\n".join(lines) + "\n")
    try:
        spent = budget.spent_today(root)
    except C.ConfigError as exc:
        assert "nothing can launch" in str(exc)
        return
    assert math.isfinite(spent) or spent == math.inf and spent >= 0, spent


@fuzz(300)
@given(st.sampled_from(["local", "modal", "colab"]), st.sampled_from([None, "T4", "H100", "A100", "H100:8"]),
       st.lists(st.one_of(st.floats(0.001, 1e6), st.sampled_from([1e300, 1e308, 10 ** 300])), min_size=1, max_size=3),
       st.one_of(st.none(), st.floats(0, 1e3), st.sampled_from([1e308, 0.0])),
       st.fixed_dictionaries({k: money for k in KEYS}), st.booleans())
def test_any_plan_that_loads_is_priced_and_held_to_its_limits(tmp_path, backend, gpu, minutes, price, own, approve):
    plan = {"backend": backend, **({"gpu": gpu} if gpu else {}),
            "steps": [{"name": f"s{i}", "run": "true", "max_minutes": m} for i, m in enumerate(minutes)]}
    raw = {"project": "p", "budget": own, "plans": {"x": plan},
           **({"pricing": {"modal_gpu_per_s": {str(gpu).split(":")[0]: price}}} if price is not None and gpu else {})}
    try:
        cfg = C.validate(raw, tmp_path)
    except C.ConfigError:
        return
    ok, msg, est = budget.check_launch(cfg, "x", approve)
    cost = est["worst_case_usd"]
    if ok:
        assert number(cost) and cost <= float(own["max_run_usd"]) and cost <= float(own["max_day_usd"]), (cost, own)
        assert approve or cost <= float(own["ask_above_usd"])
    if backend == "local" and C.number(C.plan_minutes(cfg["plans"]["x"])):
        assert cost == 0 and ok, "this machine is free, and needs no approval"
    if not number(cost):
        assert not ok and "cannot be priced" in msg
