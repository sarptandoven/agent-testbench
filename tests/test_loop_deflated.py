"""A loop over a trading strategy says whether its best try is better than luck: every try's returns are kept,
reverted tries too, and each report adds the deflated Sharpe of the best of all tries so far (and PBO once there are
enough). --min-deflated-sharpe keeps a lucky try from meeting --target."""
import asyncio
import json
import random
from pathlib import Path

import pytest

from conftest import cli, git, write_config

from agent_testbench import loop as L
from agent_testbench.trading import stats

STRATEGY = """\
import datetime as dt
import json
import os
import random

from settings import DRIFT, SCORE, SEED

rng = random.Random(SEED)
day, equity = dt.date(2024, 1, 1), 100.0
rows = [f"{day.isoformat()},{equity!r}"]
for _ in range(250):
    day += dt.timedelta(days=1)
    equity *= 1 + rng.gauss(DRIFT, 0.01)
    rows.append(f"{day.isoformat()},{equity!r}")
os.makedirs("out", exist_ok=True)
with open("out/equity.csv", "w") as f:
    f.write("time,equity\\n" + "\\n".join(rows) + "\\n")
json.dump({"score": SCORE}, open("metrics.json", "w"))
print("score", SCORE)
"""
START = ("loop", "start", "backtest", "--metric", "metrics.json:score", "--budget-usd", "0")


@pytest.fixture
def strat(tmp_path) -> Path:
    """A strategy in a git repository: its returns are seeded noise plus DRIFT a day, and its score (the loop's
    metric) is whatever settings.py says, so a test decides which try the loop keeps and how lucky it is."""
    root = tmp_path / "strat"
    root.mkdir()
    (root / "strategy.py").write_text(STRATEGY)
    (root / ".gitignore").write_text("metrics.json\nout/\n__pycache__/\n")
    write_config(root, {"project": "strat", "plans": {"backtest": {"steps": [
        {"name": "backtest", "run": "python strategy.py", "max_minutes": 1, "artifacts": ["out/equity.csv"],
         "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}})
    settings(root, 1, 1)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    return root


def settings(root: Path, score, seed, drift=0.0) -> None:
    (root / "settings.py").write_text(f"SCORE = {score}\nSEED = {seed}\nDRIFT = {drift}\n")


def state(root: Path) -> dict:
    return json.loads(next((root / ".testbench" / "loops").glob("*/state.json")).read_text())


def notify_to(root: Path, url: str, on: list[str]) -> None:
    cfg = (root / "testbench.yaml").read_text() + f"notify:\n  webhook: {url}\n  on: [{', '.join(on)}]\n"
    (root / "testbench.yaml").write_text(cfg)
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "notify")


def noise(seed: int, drift: float = 0.0, n: int = 250) -> list[float]:
    """The equity a strategy with no edge (drift 0) makes: 1% a day of seeded noise."""
    rng, equity = random.Random(seed), [100.0]
    for _ in range(n):
        equity.append(equity[-1] * (1 + rng.gauss(drift, 0.01)))
    return equity


def loop_state(tmp_path: Path, equities: list, best: int, key: str = "score") -> dict:
    """A loop's state built directly: one finished try per equity series (None: a try whose run kept no returns),
    each learnt from its own run folder the way a real loop learns it."""
    d = tmp_path / ".testbench" / "loops" / "x"
    d.mkdir(parents=True)
    s = {"id": "x", "dir": str(d), "plan": "backtest", "key": key, "goal": "max", "returns": "out/equity.csv",
         "returns_how": "given", "strategy": True, "best": {"commit": "c" * 40, "value": 1.0, "try": best},
         "tries": [], "started": 0, "target": None, "min_deflated_sharpe": None}
    for n, equity in enumerate(equities):
        run = tmp_path / "runs" / f"run{n}"
        kept = run / "steps" / "01-backtest" / "artifacts" / "out"
        kept.mkdir(parents=True)
        if equity is not None:
            (kept / "equity.csv").write_text("time,equity\n" + "".join(
                f"2024-01-{1 + i // 24:02d}T{i % 24:02d}:00:00,{v!r}\n" for i, v in enumerate(equity)))
        t = {"n": n, "status": "passed", "kept": n == best, "run": run.name, "description": f"try {n}"}
        L._learn(s, t, run, {"workdir": str(run / "work")})
        s["tries"].append(t)
    s["luck"] = L._luck(s)
    return s


def test_the_best_of_many_noise_tries_looks_great_and_is_called_luck(tmp_path):
    equities = [noise(seed) for seed in range(1, 21)]
    rets = [[b / a - 1 for a, b in zip(e, e[1:])] for e in equities]
    top = max(range(20), key=lambda i: stats.return_stats(rets[i]).sharpe)
    alone = stats.return_stats(rets[top])
    assert stats.probabilistic_sharpe_ratio(**alone._asdict()) > 0.95, "alone, the luckiest try looks significant"
    assert alone.sharpe * 252 ** 0.5 > 2, "an annualized Sharpe above 2, from noise"

    s = loop_state(tmp_path, equities, best=top)
    luck = s["luck"]
    assert luck["tries"] == 20 and luck["judged"] == top and not luck["significant"]
    line = L.luck_lines(s)[0]
    assert line == stats.deflate_tries(rets).verdict + " - the improvement may be luck", "trading.stats's own words"
    assert line.startswith("best of 20 tries: deflated Sharpe 0.") and "not significant at 0.95" in line
    assert L.luck_lines(s)[1].startswith("PBO ") and " over 20 tries: the best in-sample try " in L.luck_lines(s)[1]
    assert all(t["returns"]["n_obs"] == 250 and (Path(s["dir"]) / t["returns"]["series"]).is_file()
               for t in s["tries"])

    equities[7] = noise(99, drift=0.004)                       # a real edge: 0.4% a day on 1% of noise
    s = loop_state(tmp_path / "edge", equities, best=7)
    assert s["luck"]["significant"] and s["luck"]["judged"] == 7, s["luck"]
    assert L.luck_lines(s)[0].startswith("best of 20 tries: deflated Sharpe ")
    assert L.luck_lines(s)[0].endswith("; significant at 0.95"), L.luck_lines(s)[0]


def test_what_is_counted_and_when_pbo_is_left_out(tmp_path, monkeypatch):
    s = loop_state(tmp_path / "a", [noise(1), noise(2)], best=1)
    assert L.luck_lines(s) == [L.luck_lines(s)[0]] and "best of 2 tries" in L.luck_lines(s)[0], "no PBO under 4"
    assert "no PBO: PBO needs 4 tries with returns or more (2 so far)" in "\n".join(L._luck_notes(s))

    s = loop_state(tmp_path / "b", [noise(1), None, noise(3), noise(4), noise(5, n=200)], best=2)
    assert s["luck"]["tries"] == 4 and s["luck"]["missing"] == [1], "a try without returns is not counted"
    notes = "\n".join(L._luck_notes(s, final=True))
    assert "not counted, no returns: try 1 (out/equity.csv is not in run run1's evidence" in notes, notes
    assert "no PBO: PBO needs every try's returns over the same periods" in notes
    assert "only this loop's tries are counted" in notes and "`testbench history backtest`" in notes

    s = loop_state(tmp_path / "c", [noise(1), None, noise(3)], best=1)
    assert s["luck"]["judged"] in (0, 2) and s["luck"]["best_dsr"] is None
    assert ", the highest Sharpe: the loop's best, try 1, has no returns)" in L.luck_lines(s)[0], \
        "the best try kept no returns: the highest Sharpe stands in, and the line says so"

    monkeypatch.setattr(L, "MAX_SERIES", 100)
    s = loop_state(tmp_path / "d", [noise(i) for i in range(1, 6)], best=2)
    assert all(t["returns"]["series"] is None and t["returns"]["n_obs"] == 250 for t in s["tries"])
    assert "best of 5 tries" in L.luck_lines(s)[0] and len(L.luck_lines(s)) == 1, "a long series keeps its moments"
    assert "a series longer than 100 periods keeps only its Sharpe" in s["luck"]["pbo_why"]
    assert not (Path(s["dir"]) / "returns").exists()


def test_returns_files(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("0.01\n-0.02\n0.005\n")
    assert L.read_returns(p) == ([0.01, -0.02, 0.005], None, None)
    p.write_text("time,returns,other\n2024-01-01,0.01,x\n2024-01-02,0.02,y\n")
    assert L.read_returns(p) == ([0.01, 0.02], "2024-01-01", "2024-01-02")
    p.write_text("Time,Equity\n2024-01-01,100\n2024-01-02,110\n2024-01-03,99\n")
    values, first, last = L.read_returns(p)
    assert values == pytest.approx([0.1, -0.1]) and (first, last) == ("2024-01-01T00:00:00", "2024-01-03T00:00:00")
    for text, says in (("return\n0.01\nabc\n", "r.csv, line 3: 'abc' is not a return"),
                       ("a,b\n1,2\n", "has no equity column and 2 columns (a, b)"),
                       ("time,equity\n2024-01-02,100\n2024-01-01,101\n", "is not after line 2's"), ("", "is empty")):
        p.write_text(text)
        with pytest.raises(ValueError) as e:
            L.read_returns(p)
        assert says in str(e.value), str(e.value)


def test_a_loop_counts_every_try_and_says_so_everywhere(strat, webhook):
    notify_to(strat, webhook.url, ["loop_try", "loop_end"])
    r = cli(strat, *START)
    assert r.returncode == 0 and "BASELINE: score 1" in r.stdout, r.stdout + r.stderr
    assert "returns for the luck check: out/equity.csv, the one equity.csv the plan keeps as evidence" in r.stdout
    assert "deflated Sharpe" not in r.stdout, "one try: nothing to deflate"

    settings(strat, 2, 2)
    r = cli(strat, "loop", "try", "seed 2")
    assert r.returncode == 0 and "KEPT try 1" in r.stdout, r.stdout
    assert "\nbest of 2 tries: deflated Sharpe 0." in r.stdout and "the improvement may be luck" in r.stdout
    settings(strat, 1.5, 3)
    r = cli(strat, "loop", "try", "seed 3")
    assert r.returncode == 1 and "REVERTED try 2" in r.stdout, r.stdout
    assert "\nbest of 3 tries: deflated Sharpe 0." in r.stdout, "the reverted try is counted"
    settings(strat, 3, 4, drift=0.004)
    r = cli(strat, "loop", "try", "a real edge")
    assert r.returncode == 0 and "KEPT try 3" in r.stdout, r.stdout
    assert "\nbest of 4 tries: deflated Sharpe " in r.stdout and "; significant at 0.95\n" in r.stdout, r.stdout
    assert "\nPBO " in r.stdout and " over 4 tries: " in r.stdout

    s = state(strat)
    assert [t["returns"]["n_obs"] for t in s["tries"]] == [250] * 4 and not s["tries"][2]["kept"]
    assert s["returns"] == "out/equity.csv" and s["returns_how"] == "found" and s["luck"]["judged"] == 3
    assert all((Path(s["dir"]) / t["returns"]["series"]).is_file() for t in s["tries"])
    status = cli(strat, "loop", "status").stdout
    assert "\nluck: best of 4 tries: deflated Sharpe " in status and "\n  PBO " in status, status
    assert "returns: out/equity.csv in each try's run, the one equity.csv the plan keeps as evidence" in status
    notes = (strat / ".testbench" / "NOTES.md").read_text()
    assert "REVERTED try 2" in notes and "(best of 3 tries: deflated Sharpe" in notes
    from agent_testbench import dashboard
    assert "<p>best of 4 tries: deflated Sharpe " in dashboard._loop(strat, {}), "the live page shows it too"

    report = cli(strat, "loop", "stop").stdout
    assert "## Is the best try luck?\n\n- best of 4 tries: deflated Sharpe " in report, report
    assert "- only this loop's tries are counted" in report
    tries = webhook.events("loop_try")
    assert [e["try"] for e in tries] == [0, 1, 2, 3] and tries[0]["luck"] is None
    assert "Best of 3 tries: deflated Sharpe 0." in tries[2]["text"] and tries[2]["status"] == "reverted"
    assert tries[2]["deflated_sharpe_tries"] == 3 and tries[2]["deflated_sharpe"] < 0.95
    assert tries[3]["deflated_sharpe"] >= 0.95 and tries[3]["pbo"] is not None
    end = webhook.events("loop_end")[0]
    assert "Best of 4 tries: deflated Sharpe " in end["text"] and end["luck"].startswith("best of 4 tries")


def test_a_lucky_try_does_not_meet_the_target_with_min_deflated_sharpe(strat):
    r = cli(strat, *START, "--target", "5", "--min-deflated-sharpe", "0.95", "--returns", "./out/equity.csv")
    assert r.returncode == 0 and "returns for the luck check" not in r.stdout, r.stdout
    assert state(strat)["returns"] == "out/equity.csv" and state(strat)["min_deflated_sharpe"] == 0.95

    settings(strat, 6, 2)                                       # the score says done; the returns are noise
    r = cli(strat, "loop", "try", "lucky")
    assert r.returncode == 0 and "KEPT try 1: score 6" in r.stdout and "Target reached" not in r.stdout, r.stdout
    assert ("Best score 6 reaches the target (score >= 5), but the target counts only once the best try's deflated "
            "Sharpe is at least 0.95 (--min-deflated-sharpe): its deflated Sharpe is 0.") in r.stdout
    assert "over 2 tries, so the loop goes on." in r.stdout
    status = cli(strat, "loop", "status").stdout
    assert "stopping rules: target score >= 5 with a deflated Sharpe of at least 0.95\n  -> Best score 6 reaches" \
        in status, status

    settings(strat, 7, 3, drift=0.004)
    r = cli(strat, "loop", "try", "a real edge")
    assert r.returncode == 0 and "Target reached (score >= 5, deflated Sharpe " in r.stdout, r.stdout
    assert " >= 0.95): stop the loop" in r.stdout and "\nbest of 3 tries: deflated Sharpe " in r.stdout
    assert "; significant at 0.95\n" in r.stdout
    settings(strat, 8, 4)
    r = cli(strat, "loop", "try", "more")
    assert r.returncode == 2 and "the loop's target is reached" in r.stdout and "Best of 3 tries: deflated" in r.stdout
    git(strat, "checkout", "--", "settings.py")
    report = cli(strat, "loop", "stop").stdout
    assert "Stopping rules: target score >= 5 with a deflated Sharpe of at least 0.95. It had reached its target " \
           "when it was stopped; best of 3 tries: deflated Sharpe " in report, report


def test_a_target_met_by_luck_says_so_next_to_it(strat, webhook):
    notify_to(strat, webhook.url, ["loop_rule", "loop_end"])
    assert cli(strat, *START, "--target", "5").returncode == 0
    settings(strat, 6, 2)
    r = cli(strat, "loop", "try", "lucky")
    assert r.returncode == 0, r.stdout
    assert ("Target reached (score >= 5): stop the loop with `testbench loop stop` and report.\nbest of 2 tries: "
            "deflated Sharpe 0.") in r.stdout and "not significant at 0.95 - the improvement may be luck" in r.stdout
    settings(strat, 7, 3)
    r = cli(strat, "loop", "try", "again")
    assert r.returncode == 2 and "the target was score >= 5. Best of 2 tries: deflated Sharpe 0." in r.stdout, r.stdout
    git(strat, "checkout", "--", "settings.py")
    report = cli(strat, "loop", "stop").stdout
    assert "It had reached its target when it was stopped; best of 2 tries: deflated Sharpe 0." in report, report
    assert "not significant at 0.95 - the improvement may be luck" in report
    rule, end = webhook.bodies
    assert "Target reached (score >= 5). Best of 2 tries: deflated Sharpe 0." in rule["text"], rule["text"]
    assert rule["deflated_sharpe"] < 0.95 and "may be luck" in end["text"]


def test_a_loop_stopped_short_of_min_deflated_sharpe_did_not_meet_its_target(strat):
    assert cli(strat, *START, "--target", "5", "--min-deflated-sharpe", "0.9").returncode == 0
    settings(strat, 6, 2)
    assert cli(strat, "loop", "try", "lucky").returncode == 0
    settings(strat, 2, 3)
    r = cli(strat, "loop", "try", "worse")
    assert r.returncode == 1 and "The target (score >= 5) still does not count: its deflated Sharpe is 0." in r.stdout
    assert "over 3 tries (--min-deflated-sharpe 0.9)." in r.stdout, r.stdout
    report = cli(strat, "loop", "stop").stdout
    assert "Its best reached the target (score >= 5) but not --min-deflated-sharpe 0.9 (its deflated Sharpe is 0." \
        in report, report
    assert "so the target was not met" in report and "It had reached its target" not in report
    assert state(strat)["ended_by"] is None


def test_refusals(strat):
    for args, says in ((("--returns", "/etc/equity.csv"), "a relative path inside the project"),
                       (("--returns", "../x/equity.csv"), "a relative path inside the project"),
                       (("--min-deflated-sharpe", "0.95"), "give --target VALUE as well"),
                       (("--target", "1", "--min-deflated-sharpe", "1.5"), "from 0.5 up to (not including) 1"),
                       (("--target", "1", "--min-deflated-sharpe", "nan"), "is not a finite number")):
        r = cli(strat, *START, *args)
        assert r.returncode == 2 and says in r.stdout + r.stderr, (args, r.stdout, r.stderr)
    assert not (strat / ".testbench" / "loops").exists()

    r = cli(strat, *START, "--returns", "out/missing.csv")
    assert r.returncode == 0
    assert "no returns from this try for the luck check: out/missing.csv is not in run " in r.stdout, r.stdout
    assert "luck: not judged yet - no try has returns: out/missing.csv is not in run" in \
        cli(strat, "loop", "status").stdout


def test_a_strategy_whose_plan_keeps_no_returns_is_told_how(strat):
    write_config(strat, {"project": "strat", "plans": {"backtest": {"steps": [
        {"name": "backtest", "run": "python strategy.py   # writes out/equity.csv", "max_minutes": 1,
         "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}})
    git(strat, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "no evidence")
    r = cli(strat, *START)
    assert r.returncode == 0 and ("no returns from this try for the luck check: the plan keeps no equity.csv as "
                                  "evidence: add `artifacts: [out/equity.csv]`") in r.stdout, r.stdout
    assert "luck: not judged yet - no try has returns: the plan keeps no equity.csv" in \
        cli(strat, "loop", "status").stdout


def test_a_loop_that_is_not_a_strategy_says_nothing_about_luck(repo):
    assert cli(repo, *("loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0")).returncode == 0
    (repo / "settings.py").write_text("SCORE = 2\n")
    r = cli(repo, "loop", "try", "two")
    assert r.returncode == 0 and "luck" not in r.stdout and "deflated" not in r.stdout, r.stdout
    assert "luck" not in cli(repo, "loop", "status").stdout
    assert "luck" not in cli(repo, "loop", "stop").stdout


def test_the_mcp_loop_tools_carry_the_line(strat):
    from agent_testbench import mcp_server

    async def go():
        call = mcp_server.server.call_tool
        text = lambda out: json.dumps(out, default=lambda o: getattr(o, "text", str(o)))     # noqa: E731
        a = text(await call("loop_start", {"plan": "backtest", "metric": "metrics.json:score", "budget_usd": 0,
                                           "target": 9, "min_deflated_sharpe": 0.9, "returns": "out/equity.csv",
                                           "project_dir": str(strat)}))
        settings(strat, 2, 2)
        b = text(await call("loop_try", {"description": "seed 2", "project_dir": str(strat)}))
        c = text(await call("loop_status", {"project_dir": str(strat)}))
        d = text(await call("loop_stop", {"project_dir": str(strat)}))
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert "BASELINE: score 1" in a and state(strat)["min_deflated_sharpe"] == 0.9
    assert state(strat)["returns"] == "out/equity.csv" and state(strat)["returns_how"] == "given"
    for out in (b, c, d):
        assert "best of 2 tries: deflated Sharpe " in out and "at 0.9" in out, out
    assert "target score >= 9 with a deflated Sharpe of at least 0.9" in c
