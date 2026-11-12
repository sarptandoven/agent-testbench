"""budget.py rule by rule, at the edges: each limit holds exactly at its value and refuses a cent over it, the
machine's limits cap the project's (and only lower them), the ledgers count what they should and refuse what they
cannot read, and every refusal says how to get under the limit. tools/mutate.py checks that these tests fail when any
of these rules is changed (a comparison turned, a number moved by one, a `not` removed)."""
import datetime as dt
import json
from pathlib import Path

import pytest

from agent_testbench import budget, config as C

NOW = dt.datetime.now().isoformat(timespec="seconds")
YESTERDAY = (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")


def cfg(root: Path, run=2.0, ask=0.5, day=10.0) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    return {"root": str(root), "budget": {"max_run_usd": run, "ask_above_usd": ask, "max_day_usd": day}}


def est(cost, minutes=10.0, rate=None, what="plan x") -> dict:
    return {"what": what, "worst_case_usd": cost, "max_minutes": minutes,
            "usd_per_minute": cost / minutes if rate is None else rate}


def check(c, e, approve=False):
    return budget.check(c, e, approve, "lower it ({fit} min in all would fit)", "testbench run x", "testbench estimate x")


def machine(user_dir: Path, text: str) -> None:
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "limits.yaml").write_text(text)


def ledger(p: Path, *rows) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))


# ---- the worst case ----------------------------------------------------------------------------------------------

def test_a_worst_case_is_rounded_to_a_hundredth_of_a_cent(monkeypatch):
    monkeypatch.setattr(budget, "rate_per_minute", lambda c, hw: 0.00123456)
    got = budget.estimate({}, {"backend": "local"}, 10, "x")
    assert got["worst_case_usd"] == 0.0123 and got["max_minutes"] == 10.0 and got["usd_per_minute"] == 0.00123456
    assert budget.estimate({}, {"backend": "modal"}, 10, "x")["worst_case_usd"] == round(0.00123456 * 15, 4), \
        "a remote machine also pays for starting up"


# ---- the machine's limits ------------------------------------------------------------------------------------------

def test_the_machine_folder_is_testbench_user_dir_else_the_home_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("TESTBENCH_USER_DIR", str(tmp_path / "u"))
    assert budget.user_dir() == tmp_path / "u" and budget.limits_path() == tmp_path / "u" / "limits.yaml"
    monkeypatch.delenv("TESTBENCH_USER_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    assert budget.user_dir() == tmp_path / "home" / ".testbench"


def test_machine_limits_read(machine_limits):
    assert budget.user_limits() == {}, "no file: no machine limits"
    machine(machine_limits, "")
    assert budget.user_limits() == {}, "an empty file sets nothing"
    machine(machine_limits, "max_run_usd: 3\nmax_day_usd: 7.5\nother: 1\n")
    assert budget.user_limits() == {"max_run_usd": 3.0, "max_day_usd": 7.5}
    for bad in ("max_run_usd: -1", "ask_above_usd: .nan", "max_day_usd: lots", "- 1", "max_run_usd: ["):
        machine(machine_limits, bad)
        with pytest.raises(C.ConfigError, match="nothing can launch"):
            budget.user_limits()


def test_limits_are_written_once_into_any_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTBENCH_USER_DIR", str(tmp_path / "a" / "b"))
    assert budget.write_limits_if_missing() == tmp_path / "a" / "b" / "limits.yaml", "made with its parents"
    assert budget.write_limits_if_missing() is None
    monkeypatch.setenv("TESTBENCH_USER_DIR", str(tmp_path / "a"))       # the folder there already: still written
    assert budget.write_limits_if_missing() == tmp_path / "a" / "limits.yaml"


@pytest.mark.parametrize("key", budget.LIMIT_KEYS)
def test_the_machine_caps_a_limit_and_never_raises_it(tmp_path, machine_limits, key):
    c = cfg(tmp_path / "p", run=4.0, ask=4.0, day=4.0)
    assert budget.limits(c)[key] == (4.0, f"budget.{key} $4.00"), "no machine limits: the project's"
    machine(machine_limits, f"{key}: 3\n")
    value, where = budget.limits(c)[key]
    assert value == 3.0 and "this machine's limit" in where and str(budget.limits_path()) in where
    machine(machine_limits, f"{key}: 4\n")
    assert budget.limits(c)[key] == (4.0, f"budget.{key} $4.00"), "equal: the project's own, named as such"
    machine(machine_limits, f"{key}: 9\n")
    assert budget.limits(c)[key] == (4.0, f"budget.{key} $4.00"), "a higher machine limit does not raise it"
    other = [k for k in budget.LIMIT_KEYS if k != key][0]
    assert budget.limits(c)[other] == (4.0, f"budget.{other} $4.00"), "only the limit the machine sets"


# ---- ledgers ----------------------------------------------------------------------------------------------------

def test_record_writes_both_ledgers_into_new_and_existing_folders(tmp_path):
    root = tmp_path / "deep" / "p"
    budget.record(root, {"event": "launch", "run": "r1", "worst_case_usd": 0.25})
    budget.record(root, {"event": "finish", "run": "r1", "usd": 0.125})
    rows = budget.entries(root)
    assert [r["event"] for r in rows] == ["launch", "finish"] and rows[0]["time"][:10] == NOW[:10]
    mine = [json.loads(ln) for ln in budget.user_ledger_path().read_text().splitlines()]
    assert len(mine) == 2 and mine[0]["root"] == str(root.resolve()) and "root" not in rows[0]


def test_a_ledger_counts_finished_runs_at_cost_and_running_ones_at_their_worst(tmp_path):
    root = tmp_path / "p"
    assert budget.spent_today(root) == 0 and budget.entries(root) == []
    ledger(budget.ledger_path(root),
           {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": 1.0},
           "",                                                               # a blank line is skipped
           {"time": NOW, "event": "finish", "run": "a", "usd": 0.25},
           {"time": NOW, "event": "launch", "run": "b", "worst_case_usd": 0.5},
           {"time": YESTERDAY, "event": "launch", "run": "c", "worst_case_usd": 100.0},
           {"time": NOW, "event": "note", "usd": 7.0},                       # not a run: costs nothing
           {"time": NOW, "event": ["launch"], "run": "x", "worst_case_usd": 9.0},   # nor an event that is no name
           {"time": NOW, "event": "launch", "run": "d", "worst_case_usd": 0.00004},
           {"time": NOW, "event": "launch", "run": "e", "worst_case_usd": 0.00004})
    assert budget.spent_today(root) == 0.7501, "0.25 finished + 0.5 running + 0.00008, to a hundredth of a cent"


@pytest.mark.parametrize("row", [
    "not json", "[1, 2]", "null",
    {"event": "launch", "run": "a", "worst_case_usd": 1.0},                       # no time
    {"time": 5, "event": "launch", "run": "a", "worst_case_usd": 1.0},
    {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": float("nan")},
    {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": -1},
    {"time": NOW, "event": "launch", "run": "a"},                                  # no worst case
    {"time": NOW, "event": "launch", "worst_case_usd": 1.0},                       # no run
    {"time": NOW, "event": "finish", "run": 3, "usd": 1.0},
    {"time": NOW, "event": "finish", "run": "a", "usd": "1"},
    {"time": NOW, "event": "finish", "run": "a"},
    {"time": NOW, "event": "finish", "run": "a", "worst_case_usd": 1.0},          # a finish costs its usd
    {"time": NOW, "event": "note", "usd": float("inf")}])
def test_a_ledger_line_testbench_did_not_write_refuses_everything(tmp_path, row):
    root = tmp_path / "p"
    ledger(budget.ledger_path(root), {"time": NOW, "event": "launch", "run": "ok", "worst_case_usd": 0.0}, row)
    with pytest.raises(C.ConfigError, match=r"line 2 is not a ledger entry testbench wrote.*nothing can launch"):
        budget.spent_today(root)


def test_a_launch_counts_its_worst_case_not_a_usd_it_carries(tmp_path):
    root = tmp_path / "p"
    ledger(budget.ledger_path(root), {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": 2.0, "usd": 0.0})
    assert budget.spent_today(root) == 2.0


# ---- the check, limit by limit -----------------------------------------------------------------------------------

def test_at_max_run_usd_is_allowed_and_a_cent_over_is_refused(tmp_path):
    c = cfg(tmp_path / "p", run=2.0, ask=5.0)
    assert check(c, est(2.0))[0]
    ok, msg, _ = check(c, est(2.01), approve=True)
    assert not ok and msg.startswith("Refused: plan x could cost up to $2.01 (10 min at $0.2010/min), over "
                                     "budget.max_run_usd $2.00."), msg


def test_a_refusal_over_max_run_says_how_many_minutes_would_fit(tmp_path, machine_limits):
    c = cfg(tmp_path / "p", run=1.5, ask=5.0)
    ok, msg, _ = check(c, est(3.0, minutes=3.0, rate=1.0))                 # 3 - 1.5 = 1.5 min fit: one whole minute
    assert not ok and msg.endswith(
        "Lower it (1 min in all would fit), use a cheaper GPU or backend, or have a person raise "
        "budget.max_run_usd in testbench.yaml. `testbench estimate x` checks it again."), msg
    ok, msg, _ = check(c, est(3.0, minutes=2.0, rate=1.0))                 # 0.5 min: no whole minute fits
    assert "would fit" not in msg and msg.endswith(
        "Use a cheaper GPU or backend, or have a person raise budget.max_run_usd in testbench.yaml. `testbench "
        "estimate x` checks it again."), msg
    for rate in (0.0, 5e-324):                                             # nothing fits, or no finite answer
        ok, msg, _ = check(c, est(3.0, minutes=0.0, rate=rate))
        assert not ok and "would fit" not in msg and "max_run_usd" in msg, msg
    machine(machine_limits, "max_run_usd: 1\n")
    ok, msg, _ = check(c, est(3.0, minutes=3.0, rate=1.0))
    assert f"have a person raise max_run_usd in {budget.limits_path()}" in msg, msg


def test_the_project_day_limit_holds_to_the_cent(tmp_path):
    c = cfg(tmp_path / "p", run=5.0, ask=5.0, day=3.0)
    ledger(budget.ledger_path(Path(c["root"])), {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": 1.0})
    assert check(c, est(2.0))[0], "1 + 2 is the day limit exactly"
    ok, msg, _ = check(c, est(2.01), approve=True)
    assert not ok and msg.startswith("Refused: $1.00 already committed today; plan x could add $2.01, over "
                                     "budget.max_day_usd $3.00."), msg


def test_the_machine_day_limit_counts_every_project_to_the_cent(tmp_path, machine_limits):
    c = cfg(tmp_path / "p", run=5.0, ask=5.0, day=100.0)
    machine(machine_limits, "max_day_usd: 3\n")
    ledger(budget.user_ledger_path(), {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": 1.0,
                                       "root": str(tmp_path / "elsewhere")})
    ok, msg, _ = check(c, est(2.0))
    assert ok and msg == ("Within budget: up to $2.00 (today so far $0.00 of $100.00; $1.00 of $3.00 on this "
                          "machine)."), msg
    ok, msg, _ = check(c, est(2.01), approve=True)
    assert not ok and msg.startswith("Refused: $1.00 already committed today across every project on this machine; "
                                     "plan x could add $2.01, over max_day_usd $3.00 in"), msg


def test_without_machine_limits_only_the_project_counts(tmp_path, machine_limits):
    c = cfg(tmp_path / "p", run=5.0, ask=5.0, day=3.0)
    ledger(budget.user_ledger_path(), {"time": NOW, "event": "launch", "run": "a", "worst_case_usd": 50.0,
                                       "root": str(tmp_path / "elsewhere")})
    ok, msg, _ = check(c, est(2.0))
    assert ok and msg == "Within budget: up to $2.00 (today so far $0.00 of $3.00).", msg


def test_above_ask_above_usd_only_a_person_approves(tmp_path):
    c = cfg(tmp_path / "p", run=5.0, ask=1.0)
    assert check(c, est(1.0))[0], "at the threshold: no approval needed"
    assert check(c, est(0.5), approve=True)[0] and check(c, est(0.5))[0]
    ok, msg, _ = check(c, est(1.01))
    assert not ok and msg == ("Needs approval: plan x could cost up to $1.01 (above budget.ask_above_usd $1.00). Ask a "
                              "person; only they approve it, with `testbench run x --approve`."), msg
    assert check(c, est(1.01), approve=True)[0]
    assert "with --approve." in budget.check(c, est(1.01), False, "lower it")[1], "no command: the flag"


def test_anything_that_is_not_a_number_is_refused_unpriced(tmp_path):
    c = cfg(tmp_path / "p", run=5.0, ask=5.0)
    for bad in ({"worst_case_usd": float("nan")}, {"max_minutes": float("inf")}, {"usd_per_minute": -1.0},
                {"worst_case_usd": True}, {"usd_per_minute": None}):
        ok, msg, _ = check(c, {**est(1.0), **bad}, approve=True)
        assert not ok and msg.startswith("Refused: the worst case of plan x cannot be priced"), (bad, msg)
    for key in budget.LIMIT_KEYS:
        broken = cfg(tmp_path / "p", run=5.0, ask=5.0)
        broken["budget"][key] = float("nan")
        assert not check(broken, est(0.0), approve=True)[0], key


def test_a_free_plan_launches_whatever_the_limits(tmp_path, machine_limits):
    machine(machine_limits, "max_run_usd: 0\nask_above_usd: 0\nmax_day_usd: 0\n")
    ok, msg, _ = check(cfg(tmp_path / "p", run=0.0, ask=0.0, day=0.0), est(0.0, rate=0.0))
    assert ok, msg
