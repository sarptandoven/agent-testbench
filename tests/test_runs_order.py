"""Runs started in the same second are ordered by when each was created, not by plan name, so `last` is the latest
(a holdout run named ...-holdout sorted after a later ...-backtest made `report last` show the wrong run)."""
import json
import time

from agent_testbench import runs


def test_runs_in_the_same_second_are_in_the_order_they_were_made(tmp_path):
    d = runs.runs_dir(tmp_path)
    now = time.time()
    for i, name in enumerate(["20261112-102924-zeta", "20261112-102924-alpha", "20261112-102923-mid"]):
        (d / name).mkdir(parents=True)
        (d / name / "state.json").write_text(json.dumps({"created": now + i if "mid" not in name else now - 5}))
    assert [p.name for p in runs.all_runs(tmp_path)] == ["20261112-102923-mid", "20261112-102924-zeta",
                                                         "20261112-102924-alpha"]
    assert runs.resolve(tmp_path, "last").name == "20261112-102924-alpha"
