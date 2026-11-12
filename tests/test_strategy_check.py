"""The trading pack's `strategy:` expectation (trading/checks.py): testbench's own numbers for a backtest, read from
the standard files a step wrote, with rules on them and guardrails built in - costs must be modelled (or `costs:
none` says on the report that they were not), too few round trips fail, `beats: buy_and_hold` needs a benchmark.
Real plans through the CLI whose steps write deterministic equity and trades files (tests/data/trading/backtest.py);
a malformed value is refused at load with the near miss; the trading pack stays a layer the core loads by name."""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import jsonschema
import pytest
import yaml

from agent_testbench import config as C, expect as X, plugins as P, report as R, watch as W
from agent_testbench.trading import checks as TC, metrics as TM
from conftest import REPO, cli, write_config

BACKTEST = REPO / "tests" / "data" / "trading" / "backtest.py"
SCHEMA = json.loads(C.SCHEMA.read_text(encoding="utf-8"))
WHERE = "plans.p.steps[0].expect.strategy"
STALE = "the file was last written before this run started, so it is an old result, not this run's"


def project(root: Path, steps: list[dict]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(BACKTEST, root / "backtest.py")
    write_config(root, {"project": "strategy", "plans": {"p": {"steps": steps}}})
    return root


def step(name: str, args: str, strategy) -> dict:
    run = f"python backtest.py {args}" if args else "python -c pass"
    return {"name": name, "run": run, "max_minutes": 2, "continue_on_failure": True, "expect": {"strategy": strategy}}


def last_run(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)[-1]


def findings(root: Path) -> dict[str, list[tuple[str, bool, str]]]:
    """The last run's checks by step, as (name, ok, detail), without exit code 0."""
    report = json.loads((last_run(root) / "report.json").read_text())
    return {s["name"]: [(c["check"], c["ok"], c["detail"]) for c in s["expectations"] if c["check"] != "exit code 0"]
            for s in report["steps"]}


def load(root: Path, strategy) -> dict:
    write_config(root, {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true",
                                                                    "expect": {"strategy": strategy}}]}}})
    return C.load(root)


# ---- through the CLI ---------------------------------------------------------------------------------------------

def test_a_strategy_s_rules_and_guardrails_through_a_real_plan(tmp_path):
    rules = {"sharpe": ">= 1", "max_drawdown": ">= -0.25", "beats": "buy_and_hold", "min_trades": 30}
    root = project(tmp_path / "proj", [
        step("good", "out/good", {"equity": "out/good/equity.csv", **rules}),
        step("weak", "out/weak --drift 0.0003 --crash 0.3 --bench prices", {"equity": "out/weak/equity.csv", **rules}),
        step("free", "out/free --fee 0", "out/free/equity.csv"),
        step("slippage", "out/slip --fee 0 --slippage 0.5", "out/slip/equity.csv"),
        step("few", "out/few --trips 12", {"equity": "out/few/equity.csv", "trades": ">= 10"}),
        step("no benchmark", "out/nob --bench none", {"equity": "out/nob/equity.csv", "beats": "buy_and_hold"}),
        step("no trades", "out/nt --no-trades", {"equity": "out/nt/equity.csv", "win_rate": ">= 0.5"}),
        step("defaults", "out", {"sharpe": "~= 2.26 +- 0.01"}),
        step("crypto", "out/crypto --crypto", {"equity": "out/crypto/equity.csv", "options": {"calendar": "crypto"},
                                               "volatility": "< 1"}),
    ])
    r = cli(root, "run", "p", "--wait", "300")
    assert r.returncode == 1 and "# FAIL - plan `p`" in r.stdout, r.stdout + r.stderr
    got = findings(root)

    good = got["good"]
    want = TM.compute(root / "out/good/equity.csv", root / "out/good/trades.csv")
    assert [(name, ok) for name, ok, _ in good] == [
        ("strategy out/good/equity.csv: the standard files", True), ("strategy out/good/equity.csv: sharpe >= 1", True),
        ("strategy out/good/equity.csv: max_drawdown >= -0.25", True),
        ("strategy out/good/equity.csv: beats buy and hold", True),
        ("strategy out/good/equity.csv: trades >= 30 (min_trades)", True),
        ("strategy out/good/equity.csv: costs modelled", True)]
    files, sharpe, drawdown, beats, trades, costs = (detail for _, _, detail in good)
    assert files.startswith("read out/good/equity.csv, out/good/trades.csv: 504 bars; daily bars, 252 a year; 1.92 "
                            "years; 40 round trips; notes: periods per year: 252 - daily bars; no rows fall on a "
                            "weekend"), "the notes of trading.metrics, the periods-per-year choice first"
    assert sharpe == f"{want['sharpe']:.6g} (wanted >= 1.0); daily bars, 252 a year; 1.92 years"
    assert R.check_number({"detail": sharpe}) == pytest.approx(want["sharpe"], rel=1e-5), \
        "history and loops read it back"
    assert drawdown.startswith(f"{want['max_drawdown']:.6g} (wanted >= -0.25)")
    assert beats == (f"{want['excess_return']:.6g} (wanted > 0.0); excess_return: the total return "
                     f"{want['total_return']:.4g} against buy and hold's {want['benchmark_return']:.4g} (the "
                     "benchmark column of out/good/equity.csv); daily bars, 252 a year; 1.92 years")
    assert trades == "40 (wanted >= 30.0); round trips in out/good/trades.csv, from 80 fills"
    assert costs == "true: 80 fills paid 80.00 in fees"

    weak = {name.split(": ", 1)[1]: (ok, detail) for name, ok, detail in got["weak"]}
    assert not weak["sharpe >= 1"][0] and weak["sharpe >= 1"][1].startswith("-0.314")
    assert not weak["max_drawdown >= -0.25"][0] and weak["max_drawdown >= -0.25"][1].startswith("-0.307")
    assert not weak["beats buy and hold"][0]
    assert re.fullmatch(r"-0\.35\d+ \(wanted > 0\.0\); excess_return: the total return -0\.1934 against buy and "
                        r"hold's 0\.1574 \(closes in out/weak/prices\.csv\); daily bars, 252 a year; 1\.92 years",
                        weak["beats buy and hold"][1]), weak["beats buy and hold"][1]
    assert weak["costs modelled"][0] and weak["trades >= 30 (min_trades)"][0]

    free = dict((name, (ok, detail)) for name, ok, detail in got["free"])
    assert free["strategy out/free/equity.csv: costs modelled"] == (False, (
        "false: every fee and slippage in out/free/trades.csv is 0: the backtest charged no trading costs, so its "
        "returns are better than an account would get. Charge fees or slippage in the backtest (trades.csv's fee and "
        "slippage columns), or write `costs: none` to allow zero costs - the report then says costs were not "
        "modelled"))
    assert free["strategy out/free/equity.csv: trades >= 20 (min_trades)"][0], "min_trades is 20 unless set"
    assert got["slippage"][-1] == ("strategy out/slip/equity.csv: costs modelled", True,
                                   "true: 80 fills paid 0.00 in fees, and slippage was recorded")

    few = dict((name, (ok, detail)) for name, ok, detail in got["few"])
    assert few["strategy out/few/equity.csv: trades >= 10"] == (True, "12 (wanted >= 10.0); daily bars, 252 a year; "
                                                                      "1.92 years")
    assert few["strategy out/few/equity.csv: trades >= 20 (min_trades)"] == (False, (
        "12 (wanted >= 20.0); round trips in out/few/trades.csv, from 24 fills; a number from this few trades means "
        "little: backtest over a longer span or more symbols, or set min_trades knowingly (0 turns this off)")), \
        "a rule the strategy passes does not lift the guardrail"

    assert ("strategy out/nob/equity.csv: beats buy and hold", False, (
        "no benchmark to beat: add a benchmark column to out/nob/equity.csv (a buy-and-hold price or equity at the "
        "same times), or write prices.csv next to it (time, symbol, close; options: {symbol: SPY} picks one when it "
        "has several)")) in got["no benchmark"]

    no_trades = dict((name, (ok, detail)) for name, ok, detail in got["no trades"])
    assert no_trades["strategy out/nt/equity.csv: win_rate >= 0.5"] == (False, (
        "win_rate is undefined (null): no trades file: fills, trades, win_rate, profit_factor, average_trade, "
        "average_trade_return, total_fees, costs_modeled, traded_value, turnover, turnover_per_year, open_positions "
        "are null"))
    assert no_trades["strategy out/nt/equity.csv: trades >= 20 (min_trades)"] == (False, (
        "no trades.csv next to out/nt/equity.csv: testbench counts the round trips from it (one row per fill: time, "
        "symbol, side, quantity, price, fee; docs/trading.md#tradescsv---one-row-per-fill)"))
    assert no_trades["strategy out/nt/equity.csv: costs modelled"] == (False, (
        "no trades.csv next to out/nt/equity.csv, so testbench cannot see whether fees or slippage were charged. "
        "Charge fees or slippage in the backtest (trades.csv's fee and slippage columns), or write `costs: none` to "
        "allow zero costs - the report then says costs were not modelled"))

    assert [(name, ok) for name, ok, _ in got["defaults"]] == [
        ("strategy out/equity.csv: the standard files", True),
        ("strategy out/equity.csv: sharpe ~= 2.26 +- 0.01", True),
        ("strategy out/equity.csv: trades >= 20 (min_trades)", True), ("strategy out/equity.csv: costs modelled", True)]
    assert got["crypto"][1][2].endswith("; daily bars, 365 a year; 1.38 years"), got["crypto"][1]


def test_costs_none_passes_and_says_so_on_the_report_and_undefined_numbers_fail(tmp_path):
    root = project(tmp_path / "proj", [
        step("no costs", "out --fee 0", {"sharpe": ">= 1", "costs": "none"}),
        step("two bars", "short --bars 2 --no-trades --bench none",
             {"equity": "short/equity.csv", "sharpe": ">= 1", "max_drawdown": ">= -0.1", "min_trades": 0,
              "costs": "none"})])
    r = cli(root, "run", "p", "--wait", "300")
    assert r.returncode == 1, r.stdout + r.stderr
    got = findings(root)
    assert got["no costs"][-1] == ("strategy out/equity.csv: costs not modelled (costs: none)", True, (
        "warning: every fee and slippage in out/trades.csv is 0: the backtest charged no trading costs, so its returns "
        "are better than an account would get; `costs: none` allows it"))
    report = (last_run(root) / "report.md").read_text()
    assert "- no costs: strategy out/equity.csv: costs not modelled (costs: none) - warning: every fee and slippage" \
        in report, "a passing step's warning is a line of the report"

    short = dict((name, (ok, detail)) for name, ok, detail in got["two bars"])
    assert short["strategy short/equity.csv: sharpe >= 1"] == (False, (
        "sharpe is undefined (null): volatility, sharpe and sortino are null: they need at least 3 rows (2 returns); "
        "the backtest covers 1 days: the yearly numbers (cagr, volatility, sharpe, sortino, calmar) stretch less than "
        "a year into one")), "never passes for want of a number"
    assert short["strategy short/equity.csv: max_drawdown >= -0.1"] == (True, "0.0 (wanted >= -0.1); daily bars, 252 a "
                                                                              "year; 24 hours")
    assert short["strategy short/equity.csv: costs not modelled (costs: none)"] == (True, (
        "warning: no trades.csv next to short/equity.csv, so testbench cannot see whether fees or slippage were "
        "charged; `costs: none` allows it"))
    assert not any("min_trades" in name for name in short), "min_trades: 0 turns the guardrail off"


def test_files_from_an_earlier_run_missing_or_refused_fail_every_finding(tmp_path):
    root = project(tmp_path / "proj", [
        step("nothing written", "", {"equity": "out/old/equity.csv", "sharpe": ">= 0", "beats": "buy_and_hold"}),
        step("trades left over", "out/mixed --no-trades", "out/mixed/equity.csv"),
        step("missing", "", "out/none/equity.csv"),
        {**step("refused", "", "bad/equity.csv"), "run": "python write_bad.py"}])
    for folder in ("old", "mixed"):                         # what an earlier run left behind
        subprocess.run([sys.executable, str(BACKTEST), str(root / "out" / folder)], check=True, capture_output=True)
    then = time.time() - 3600
    for f in ("old/equity.csv", "old/trades.csv", "mixed/equity.csv", "mixed/trades.csv"):
        os.utime(root / "out" / f, (then, then))
    (root / "write_bad.py").write_text("import os\nos.makedirs('bad', exist_ok=True)\nopen('bad/equity.csv', 'w')."
                                       "write('time,equity\\n2024-01-02,100\\n2024-01-03,-5\\n')\n")
    r = cli(root, "run", "p", "--wait", "300")
    assert r.returncode == 1, r.stdout + r.stderr
    got = findings(root)
    later = "not checked: the standard files could not be read (the finding above says why)"
    assert got["nothing written"] == [
        ("strategy out/old/equity.csv: the standard files", False, f"out/old/equity.csv: {STALE}"),
        ("strategy out/old/equity.csv: sharpe >= 0", False, later),
        ("strategy out/old/equity.csv: beats buy and hold", False, later),
        ("strategy out/old/equity.csv: trades >= 20 (min_trades)", False, later),
        ("strategy out/old/equity.csv: costs modelled", False, later)]
    assert got["trades left over"][0] == ("strategy out/mixed/equity.csv: the standard files", False,
                                          f"out/mixed/trades.csv: {STALE}"), \
        "this run's equity.csv, but the trades.csv next to it is an earlier run's: refused, not read"
    missing = got["missing"][0]
    assert not missing[1] and missing[2].startswith("out/none/equity.csv: no such file in "), missing
    refused = got["refused"][0]
    assert not refused[1] and refused[2].startswith("bad/equity.csv") and "line 3" in refused[2], \
        "the reader's message names the file as testbench.yaml does, not by its absolute path"


def test_the_complete_example_in_docs_trading_passes_for_a_good_strategy(tmp_path):
    text = (REPO / "docs" / "trading.md").read_text(encoding="utf-8")
    section = text.split("## Checking a strategy", 1)[1]
    example = re.search(r"```yaml\n(.*?)```", section, re.S).group(1)
    assert problems(example) == [], "editors see nothing wrong with it"
    cfg = yaml.safe_load(example)
    for plan in cfg["plans"].values():
        for s in plan["steps"]:
            s["run"] = "python backtest.py out --bench prices"
    root = tmp_path / "proj"
    root.mkdir()
    shutil.copy(BACKTEST, root / "backtest.py")
    write_config(root, cfg)
    plan = next(iter(cfg["plans"]))
    r = cli(root, "run", plan, "--wait", "300")
    assert r.returncode == 0 and f"# PASS - plan `{plan}`" in r.stdout, r.stdout + r.stderr


# ---- refused when testbench.yaml loads ---------------------------------------------------------------------------

@pytest.mark.parametrize("strategy, message", [
    ({"sharp": ">= 1"}, f"`{WHERE}` has no 'sharp' (did you mean sharpe?); it takes equity, options, beats, "
                        "min_trades, costs and a rule on any of testbench's numbers for a backtest: start_equity, "
                        "end_equity"),
    ({"sharpe": "high"}, f"`{WHERE}.sharpe`: 'high' compares with text, but a reader's numbers are numbers"),
    ({"sharpe": "=> 1"}, f"`{WHERE}.sharpe`: '=> 1': => is not a comparison"),
    ({"trades": "out/trades.csv"}, "`trades:` here is a rule on testbench's number trades; trades.csv and prices.csv "
                                   "are read from the folder equity.csv is in"),
    ({"beats": "SPY"}, f"`{WHERE}.beats` is buy_and_hold: the strategy's total return must beat buy and hold"),
    ({"min_trades": -1}, f"`{WHERE}.min_trades` is the fewest round trips the numbers may come from"),
    ({"min_trades": "20"}, f"`{WHERE}.min_trades` is the fewest round trips"),
    ({"min_trades": True}, f"`{WHERE}.min_trades` is the fewest round trips"),
    ({"costs": "optional"}, f"`{WHERE}.costs` is required (the default: some fee or slippage must be charged) or none"),
    ({"costs": False}, f"`{WHERE}.costs` is required"),
    ({"equity": ""}, f"`{WHERE}.equity` is the path of the equity.csv the backtest writes"),
    ({"options": {"calender": "crypto"}}, f"`{WHERE}.options`: reader trading has no option 'calender' (did you mean "
                                          "calendar?)"),
    ({"options": {"risk_free": "4%"}}, f"`{WHERE}.options`: reader trading: risk_free is '4%'; give the yearly "
                                       "risk-free rate as a fraction"),
    ({"options": {"periods_per_year": 0}}, "periods_per_year is 0; give a number above 0"),
    ({"options": {"symbol": ""}}, "symbol is ''; give prices.csv's benchmark symbol, e.g. SPY"),
    ({"options": {"files": "x"}}, f"`{WHERE}.options` is a mapping of the trading reader's options"),
    ([{"sharpe": ">= 1"}], f"`{WHERE}` is a mapping such as {TC.EXAMPLE}, or the path of the equity.csv"),
    (5, f"`{WHERE}` is a mapping such as"),
])
def test_a_malformed_strategy_is_refused_at_load_with_the_near_miss(tmp_path, strategy, message):
    with pytest.raises(C.ConfigError) as info:
        load(tmp_path, strategy)
    assert message in str(info.value)


@pytest.mark.parametrize("strategy", [None, {}, "out/run/equity.csv", TC.StrategyCheck.example,
                                      {"costs": "none", "min_trades": 0, "costs_modeled": False, "exposure": "<= 0.5",
                                       "max_drawdown_recovered": True, "options": {"risk_free": 0.04, "symbol": "SPY",
                                                                                   "calendar": "auto"}}])
def test_what_strategy_takes(tmp_path, strategy):
    load(tmp_path, strategy)
    assert problems(yaml.safe_dump({"plans": {"p": {"steps": [{"name": "a", "run": "x",
                                                               "expect": {"strategy": strategy}}]}}})) == []


# ---- the check itself, the boundary and the schema ---------------------------------------------------------------

def test_strategy_is_a_built_in_that_keeps_the_contract_and_no_plug_in_can_take_its_name():
    chk = P.get("checks", "strategy")
    assert type(chk) is TC.StrategyCheck and isinstance(chk, X.Check) and P.builtin("checks", "strategy")
    assert P.check("checks", chk) == []
    assert X.KIND.builtin["strategy"] == X.STRATEGY == "agent_testbench.trading.checks:StrategyCheck"
    assert chk.keys == (*TC.SETTINGS, *TM.KEYS)

    class Mine(X.Check):
        name, example = "strategy", 1

        def validate(self, where, value):
            if not isinstance(value, int):
                raise C.ConfigError(f"`{where}` is a number")

        def check(self, value, *, workdir, result, add):
            add("mine", True, "fine")
    assert "'strategy' is a built-in check's name, and a plug-in cannot replace a built-in" in "\n".join(
        P.check("checks", Mine()))


BOUNDARY = """
import json, sys, time, pathlib, tempfile
from agent_testbench import expect
before = 'agent_testbench.trading' in sys.modules
d = pathlib.Path(tempfile.mkdtemp())
(d / 'm.json').write_text('{"a": 1}')
got = expect.check({'json': {'m.json': {'a': 1}}, 'numbers': {'reader': 'json', 'files': 'm.json', 'rules': {'a': 1}}},
                   workdir=d, result={'kind': 'run', 'exit_code': 0, 'run_started': time.time() - 5})
print(json.dumps({'before': before, 'ok': all(c['ok'] for c in got),
                  'loaded': sorted(m for m in sys.modules if m.startswith('agent_testbench.trading'))}))
"""


def test_a_plan_without_strategy_never_loads_the_trading_pack_s_numbers():
    out = subprocess.run([sys.executable, "-c", BOUNDARY], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    assert got == {"before": False, "ok": True,
                   "loaded": ["agent_testbench.trading", "agent_testbench.trading.checks"]}, \
        "the registry imports the check by name when checks are first used; trading.metrics only when it checks one"


def test_the_trading_folders_a_strategy_reads_are_what_watch_leaves_out():
    plan = lambda strategy: {"steps": [{"name": "a", "run": "x", "expect": {"strategy": strategy}}]}
    assert W.outputs(plan(None)) == ["out/equity.csv", "out/trades.csv"]
    assert W.outputs(plan("bt/equity.csv")) == ["bt/equity.csv", "bt/trades.csv"]
    assert W.outputs(plan({"equity": "equity.csv"})) == ["equity.csv", "trades.csv"]


VALIDATOR = jsonschema.Draft202012Validator(SCHEMA)


def problems(text: str) -> list[str]:
    return [e.message for e in VALIDATOR.iter_errors(yaml.safe_load(text))]


def test_the_schema_knows_every_key_strategy_takes_and_refuses_what_it_refuses(tmp_path):
    defs = SCHEMA["$defs"]
    assert set(defs["strategy"]["properties"]) == {*TC.SETTINGS, *TM.KEYS}, "a key strategy takes, and no other"
    assert tuple(defs["strategy"]["properties"]["beats"]["enum"]) == TC.BEATS
    assert tuple(defs["strategy"]["properties"]["costs"]["enum"]) == TC.COSTS
    assert set(defs["trading_options"]["properties"]) == set(P.get("metrics", "trading").options)
    assert defs["trading_options"]["properties"]["calendar"]["enum"] == ["auto", *TM.CALENDARS]
    for bad in ({"sharp": 1}, {"beats": "SPY"}, {"costs": "maybe"}, {"min_trades": -1}, {"options": {"calender": "x"}},
                {"options": {"calendar": "weekly"}}, {"equity": ""}, [{"sharpe": 1}]):
        text = yaml.safe_dump({"plans": {"p": {"steps": [{"name": "a", "run": "x", "expect": {"strategy": bad}}]}}})
        assert problems(text), bad
        with pytest.raises(C.ConfigError):
            load(tmp_path, bad)
