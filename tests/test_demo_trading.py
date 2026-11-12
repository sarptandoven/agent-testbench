"""`testbench demo trading` (examples/trading): the trading pack's gate. The demo's free plan fails on the planted
look-ahead, with the finding and the line to fix; fixed (one line), the strategy passes the backtest (costs, 30+ round
trips, better than buy and hold, with a tear sheet), the look-ahead check and the walk-forward, opens the holdout once
and is refused a second opening, and rehearses the paper rung on a mock Alpaca account with no live call. All through
the CLI, as a person or an agent runs it: free, offline, seconds."""
import re
import shutil
import subprocess
import sys

from agent_testbench import runs
from conftest import EXAMPLES, cli

BUG, FIX = "decide(closes[: i + 2])", "decide(closes[: i + 1])"


def bug_line() -> int:
    lines = (EXAMPLES / "trading" / "strategy.py").read_text().splitlines()
    found = [n for n, line in enumerate(lines, 1) if BUG in line]
    assert len(found) == 1, "strategy.py has the planted line once: update this test with it"
    return found[0]


def test_the_demo_fails_on_the_planted_look_ahead_and_says_which_line_to_fix(tmp_path):
    r = cli(tmp_path, "demo", "trading")
    out = r.stdout
    assert r.returncode == 0, "the plan's failure is the demo working: " + out + r.stderr
    assert "Copied the trading example to" in out and "Running its free plan, `lookahead`" in out
    assert "# FAIL - plan `lookahead` on local" in out and "## Look-ahead: FOUND" in out, out
    finding = re.search(r"look-ahead: DEMO at (\S+) \(signal\) was `[-0-9.]+` with data up to 2025-06-30 but was "
                        r"`[-0-9.]+` with data before (\S+) - the strategy used rows after \1 to decide it", out)
    assert finding, out
    assert "strategy out/equity.csv: sharpe >= 1 - 3.8" in out, "the backtest itself looks superb: that is the tell"
    assert ("the strategy decides\nwith tomorrow's close. Line {} of strategy.py hands decide() `closes[: i + 2]`"
            .format(bug_line())) in out, out
    assert f"Line {bug_line()} of `strategy.py`" in (EXAMPLES / "trading" / "README.md").read_text(), "README's line"
    assert "then in " in out and "testbench run lookahead" in out and "testbench run paper" in out
    target = tmp_path / "trading"
    status = subprocess.run(["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True).stdout
    assert status.split() in ([], ["??", ".testbench/"]), f"the run wrote only .testbench/: {status}"
    r = cli(tmp_path, "demo", "--list")
    assert "  trading " in r.stdout and "look-ahead" in r.stdout


def test_fixed_it_climbs_the_ladder_backtest_lookahead_walk_forward_holdout_once_and_paper(tmp_path):
    root = tmp_path / "trading"
    shutil.copytree(EXAMPLES / "trading", root, ignore=shutil.ignore_patterns(".testbench", "out", "__pycache__"))
    shipped = (root / "data" / "prices.csv").read_bytes()
    subprocess.run([sys.executable, "make_prices.py"], cwd=root, check=True, timeout=60)
    assert (root / "data" / "prices.csv").read_bytes() == shipped, "make_prices.py writes the file that ships"
    strategy = root / "strategy.py"
    text = strategy.read_text()
    strategy.write_text(text.replace(BUG, FIX))          # the one-line fix the demo names
    assert sum(a != b for a, b in zip(text.splitlines(), strategy.read_text().splitlines())) == 1

    def run(plan):
        r = cli(root, "run", plan, "--foreground")
        assert r.returncode == 0 and f"# PASS - plan `{plan}` on local" in r.stdout, r.stdout + r.stderr
        return r.stdout, runs.all_runs(root)[-1]

    out, run_dir = run("backtest")
    for line in ("strategy out/equity.csv: beats buy and hold - ", "strategy out/equity.csv: trades >= 30 (min_trades)",
                 "strategy out/equity.csv: costs modelled - true", "strategy out/equity.csv: sharpe >= 1 - 1.38"):
        assert line in out, line
    assert "## Strategy: backtest (out/equity.csv)" in out and "(buy and hold +154.4%" in out, out
    r = cli(root, "report", run_dir.name, "--tearsheet")
    page = (run_dir / "tearsheet.html").read_text()
    assert r.returncode == 0 and "the tear sheet" in r.stdout and "<svg" in page and "Not financial advice" in page

    out, run_dir = run("lookahead")
    assert "## Look-ahead: none found (4 reruns, " in out and "**FAIL**" not in out

    out, run_dir = run("walk-forward")
    assert "## Walk-forward: 7 windows, 7 passed" in out

    out, run_dir = run("holdout")
    assert "this run is the first to open it" in out and "the whole file, the sealed rows from" in out
    r = cli(root, "run", "holdout", "--foreground")
    assert r.returncode == 2 and "plans.holdout.holdout.max_opens is 1" in r.stdout, "a second opening is refused"
    assert "Nothing ran and nothing was spent" in r.stdout

    out, run_dir = run("paper")
    for line in ("mock alpaca live 0 - 0", "mock alpaca refused 0 - 0", "mock alpaca orders <= 20 - 11",
                 "## Strategy: paper account (out/paper/equity.csv)"):
        assert line in out, line
    calls = (next(run_dir.glob("steps/01-*")) / "mock_calls.jsonl").read_text()
    assert '"live": true' not in calls and '"endpoint": "submit_order"' in calls

    r = cli(root, "verify")
    assert r.returncode == 0 and "data/prices.csv (prices) is the data the run read" in r.stdout, r.stdout


def test_the_bug_back_in_fails_the_look_ahead_check_and_nothing_else_needs_changing(tmp_path):
    """The look-ahead is what catches it: the planted backtest passes every number it is given."""
    root = tmp_path / "trading"
    shutil.copytree(EXAMPLES / "trading", root, ignore=shutil.ignore_patterns(".testbench", "out", "__pycache__"))
    r = cli(root, "run", "backtest", "--foreground")
    assert r.returncode == 0 and "# PASS - plan `backtest`" in r.stdout and "win rate 97%" in r.stdout, r.stdout
    r = cli(root, "run", "lookahead", "--foreground")
    assert r.returncode == 1 and "look-ahead: DEMO at " in r.stdout
    assert re.search(r"\| FAIL \| 4/4 \| 2025-06-30 \| ", r.stdout), "every cut catches it, the last one too"
