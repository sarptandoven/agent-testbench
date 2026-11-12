"""The trading on-ramp: `init --recipe trading-backtest` in a folder with no backtest writes a starter one that runs
the recipe's ladder as it is (standard files, fees, no look-ahead), never over a file; and `python -m
agent_testbench.trading.files --check FOLDER` says what a backtest's own CSV files need to read as the standard ones."""
import csv
import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

from agent_testbench.trading import files as F
from conftest import EXAMPLES, cli, write_config

STARTER = "Wrote backtest.py, a starter for `python backtest.py` (the plans run it, and there was no backtest.py here)"


def project(tmp_path):
    root = tmp_path / "strategy"
    (root / "data").mkdir(parents=True)
    shutil.copy(EXAMPLES / "trading" / "data" / "prices.csv", root / "data" / "prices.csv")
    return root


def starter_module(path):
    spec = importlib.util.spec_from_file_location("starter_backtest", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_init_writes_a_starter_backtest_that_climbs_the_ladder(tmp_path):
    root = project(tmp_path)
    r = cli(root, "init", "--recipe", "trading-backtest")
    assert r.returncode == 0 and STARTER in r.stdout, r.stdout + r.stderr
    assert "put your strategy in decide()" in r.stdout
    out = subprocess.run([sys.executable, "backtest.py"], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "TESTBENCH_DATA_PRICES": "data/prices.csv"})
    assert out.returncode == 0, out.stderr
    fills = F.read_trades(root / "out" / "trades.csv")
    bars = F.read_equity(root / "out" / "equity.csv")
    assert fills and all(f.fee > 0 for f in fills), "fees are charged on every fill"
    assert all(b.benchmark for b in bars), "with buy and hold's price, so `beats: buy_and_hold` can be checked"
    code, lines = F.check_folder(root / "out")
    assert code == 0 and lines[0].startswith(f"{root / 'out'}: every file reads"), lines
    # the recipe's own rungs, as written: no decision changes when later rows are cut away, and the numbers are read
    look = cli(root, "run", "lookahead")
    assert look.returncode == 0 and "## Look-ahead: none found" in look.stdout, look.stdout + look.stderr
    back = cli(root, "run", "backtest")
    assert "Strategy: backtest (out/equity.csv)" in back.stdout and "fees" in back.stdout, back.stdout


def test_the_starter_decides_from_past_rows_only(tmp_path):
    root = project(tmp_path)
    assert cli(root, "init", "--recipe", "trading-backtest").returncode == 0
    mod = starter_module(root / "backtest.py")
    symbol, rows = mod.read_prices(root / "data" / "prices.csv")
    _, _, full = mod.backtest(rows)
    _, _, cut = mod.backtest(rows[:400])
    assert cut == full[:len(cut)], "a decision never changes when the rows after it are gone"
    _, _, late = mod.backtest(rows, "2024-01-01")
    assert late[0][0] >= "2024-01-01" and late == [s for s in full if s[0] >= "2024-01-01"], \
        "trading from TESTBENCH_TEST_START or TRADE_FROM, warmed up on the rows before"


def test_init_never_writes_over_a_backtest(tmp_path):
    root = project(tmp_path)
    (root / "backtest.py").write_text("print('mine')\n")
    r = cli(root, "init", "--recipe", "trading-backtest")
    assert r.returncode == 0 and "Wrote backtest.py" not in r.stdout, r.stdout
    assert (root / "backtest.py").read_text() == "print('mine')\n"
    other = tmp_path / "other"
    other.mkdir()
    write_config(other, {"project": "other", "plans": {"smoke": {"steps": [{"name": "a", "run": "true",
                                                                            "max_minutes": 1}]}}})
    added = cli(other, "init", "--add", "--recipe", "trading-backtest")
    assert added.returncode == 0 and STARTER in added.stdout, added.stdout
    again = cli(tmp_path / "other", "init", "--recipe", "pytest", "--force")
    assert "backtest.py" not in again.stdout, "a recipe whose commands name no starter writes none"


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows(rows)


def test_check_says_what_a_backtests_own_files_need(tmp_path):
    out = tmp_path / "out"
    write(out / "portfolio.csv", [["", "Value", "returns"], ["01/02/2024", "10000", "0"], ["01/03/2024", "10100", ".01"]])
    write(out / "orders.csv", [["Date", "Ticker", "Size", "Price", "Commission"], ["2024-01-02", "SPY", "10", "470", "1"],
                               ["2024-01-03", "SPY", "-10", "480", "1"]])
    write(out / "fills.csv", [["time", "symbol", "side", "quantity", "price"], ["2024-01-02", "SPY", "long", "1", "9"]])
    write(out / "signals.csv", [["timestamp", "ticker", "gap"], ["1704153600", "SPY", "0.01"]])
    write(out / "notes.csv", [["a", "b"], ["1", "2"]])
    code, lines = F.check_folder(out)
    text = "\n".join(lines)
    assert code == 1 and lines[0].startswith(f"{out}: ") and "changes to reach testbench's standard format" in text
    for said in ("FIX  portfolio.csv, which looks like the equity curve:", "- name it equity.csv",
                 "- name the unnamed first column `time` (pandas writes its index without a name: "
                 "df.to_csv(path, index_label='time'))", "- rename column `Value` to `equity`",
                 "- the unnamed first column: '01/02/2024' is not an ISO 8601 time; write year-month-day first",
                 "FIX  orders.csv, which looks like the fills:", "- name it trades.csv",
                 "- rename column `Date` to `time`", "- rename column `Ticker` to `symbol`",
                 "- rename column `Size` to `quantity`", "- rename column `Commission` to `fee`",
                 "- add `side`: buy where `Size` is above 0 and sell where it is below",
                 "FIX  fills.csv, which looks like the fills:", "- add `fee`: each fill's commissions and fees",
                 "- `side` holds 'long' as buy: write buy or sell on every row",
                 "FIX  signals.csv, which looks like the decisions:", "- rename column `timestamp` to `time`",
                 "- `timestamp`: '1704153600' looks like a Unix timestamp in seconds; write ISO 8601 instead",
                 "--   notes.csv: not one of the standard files"):
        assert said in text, f"{said!r} not in:\n{text}"
    assert "ADD" not in text, "each standard file has a candidate: nothing to add"
    # once renamed and rewritten, the folder reads; a missing file is named with what it holds
    shutil.rmtree(out)
    write(out / "equity.csv", [["time", "equity"], ["2024-01-02", "10000"], ["2024-01-03", "10100"]])
    write(out / "trades.csv", [["time", "symbol", "side", "quantity", "price", "fee"],
                               ["2024-01-02", "SPY", "buy", "10", "470", "1"]])
    code, lines = F.check_folder(out)
    assert code == 1 and any(line.startswith("ADD  signals.csv: no file here looks like the decisions; write "
                                             "signals.csv - time, symbol, then a column per decision") for line in lines)
    assert "ok   equity.csv: reads as testbench's equity.csv: 2 rows, 2024-01-02 to 2024-01-03; it has no benchmark" \
        in "\n".join(lines), lines
    write(out / "signals.csv", [["time", "symbol", "signal"], ["2024-01-02", "SPY", "0.5"]])
    write(out / "trades.csv", [["time", "symbol", "side", "quantity", "price", "fee"],
                               ["2024-01-02", "SPY", "buy", "-10", "470", "1"]])
    code, lines = F.check_folder(out)
    assert code == 1 and "-10 is negative; write the size without a sign" in "\n".join(lines), \
        "a file with the right columns is read by the standard rules, line by line"


def test_check_from_the_command_line(tmp_path):
    def check(folder):
        return subprocess.run([sys.executable, "-m", "agent_testbench.trading.files", "--check", str(folder)],
                              capture_output=True, text=True)
    r = check(tmp_path / "nope")
    assert r.returncode == 2 and "is not there: give the folder the backtest writes its CSV files to" in r.stdout
    (tmp_path / "empty").mkdir()
    r = check(tmp_path / "empty")
    assert r.returncode == 2 and "holds no CSV files: run the backtest first" in r.stdout
    out = tmp_path / "run" / "out"
    write(out / "equity.csv", [["time", "equity", "benchmark"], ["2024-01-02", "10000", "5"]])
    write(out / "trades.csv", [["time", "symbol", "side", "quantity", "price", "fee"]])
    write(out / "signals.csv", [["time", "symbol", "signal"], ["2024-01-02", "SPY", "0.5"]])
    r = check(tmp_path / "run")                    # one folder down, as the backtest's out/
    assert r.returncode == 1 and "it has 1 row; the numbers need at least 2 (a start and an end)" in r.stdout, r.stdout
    write(out / "equity.csv", [["time", "equity", "benchmark"], ["2024-01-02", "10000", "5"],
                               ["2024-01-03", "10100", "5.1"]])
    r = check(tmp_path / "run")
    assert r.returncode == 0 and "every file reads in testbench's standard format" in r.stdout, r.stdout
    assert "ok   out/trades.csv: reads as testbench's trades.csv: 0 rows" in r.stdout, r.stdout
    with pytest.raises(F.TradingFileError, match=r"is a folder; give the path of the CSV file in it \(`python -m "
                                                 r"agent_testbench.trading.files --check"):
        F.read_equity(out)
    shown = cli(tmp_path, "numbers", "trading", "run/out/equity.csv", "--json")
    assert shown.returncode == 0 and json.loads(shown.stdout)["values"]["total_return"] == pytest.approx(0.01), \
        "what the check says reads, the numbers read"


def test_the_check_example_in_docs_trading_is_what_it_prints(tmp_path, monkeypatch):
    from conftest import REPO
    text = (REPO / "docs" / "trading.md").read_text(encoding="utf-8")
    shown = text.split("$ python -m agent_testbench.trading.files --check out\n", 1)[1].split("```", 1)[0]
    write(tmp_path / "out" / "portfolio.csv", [["Date", "Value"], ["01/02/2024", "10000"], ["01/03/2024", "10100"]])
    write(tmp_path / "out" / "orders.csv", [["Date", "Symbol", "Size", "Price", "Commission"],
                                            ["2024-01-02", "SPY", "10", "470", "1"],
                                            ["2024-01-03", "SPY", "-10", "480", "1"]])
    monkeypatch.chdir(tmp_path)
    code, lines = F.check_folder("out")
    assert code == 1 and "\n".join(lines) + "\n" == shown
