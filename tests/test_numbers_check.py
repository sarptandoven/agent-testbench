"""The `numbers:` expectation: rules on the numbers a metric reader (metrics.py) reads from a step's files - json, csv,
junit, trading or an installed reader. Real plans through the CLI whose steps write the files: a rule passes or fails
on testbench's own reading; a number the reader did not give, or gave as null, fails with the reader's note on why;
a missing, refused or stale file fails every rule; a malformed block is refused when testbench.yaml loads, naming
the near miss; and the schema says the same."""
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

from agent_testbench import config as C, expect as X, metrics as M, plugins as P, report as R, watch as W
from agent_testbench.trading import metrics as TM
from conftest import REPO, cli, write_config

BACKTEST = REPO / "tests" / "data" / "trading" / "backtest.py"
EXAMPLE = REPO / "examples" / "plugins" / "testbench-metrics-example"
SCHEMA = json.loads(C.SCHEMA.read_text(encoding="utf-8"))
WHERE = "plans.p.steps[0].expect.numbers"


def project(root: Path, plans: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy(BACKTEST, root / "backtest.py")
    write_config(root, {"project": "numbers", "plans": plans})
    return root


def step(name: str, run: str, numbers) -> dict:
    return {"name": name, "run": run, "max_minutes": 2, "continue_on_failure": True, "expect": {"numbers": numbers}}


def findings(root: Path) -> dict[str, dict[str, dict]]:
    """The last run's checks by step and by check name: {step: {check: {ok, detail}}}."""
    run = sorted((root / ".testbench" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)[-1]
    report = json.loads((run / "report.json").read_text())
    return {s["name"]: {c["check"]: c for c in s["expectations"]} for s in report["steps"]}


def old(*paths: Path) -> None:
    """Files from an earlier run: last written an hour ago."""
    then = time.time() - 3600
    for p in paths:
        os.utime(p, (then, then))


def load(root: Path, numbers) -> dict:
    write_config(root, {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true",
                                                                    "expect": {"numbers": numbers}}]}}})
    return C.load(root)


# ---- through the CLI ---------------------------------------------------------------------------------------------

def test_rules_on_a_reader_s_numbers_pass_and_fail_through_a_real_plan(tmp_path):
    root = tmp_path / "proj"
    write_json = ("python -c \"import json, os; os.makedirs('out', exist_ok=True); json.dump({'accuracy': 0.93, "
                  "'losses': [0.9, 0.4]}, open('out/m.json', 'w')); open('out/log.csv', 'w').write("
                  "'epoch,loss\\n1,0.5\\n2,0.25\\n')\"")
    trading = {"reader": "trading", "files": {"equity": "out/a/equity.csv"}, "options": {"calendar": "stocks"},
               "rules": {"sharpe": ">= 1", "max_drawdown": ">= -0.2", "trades": ">= 30"}}
    project(root, {"p": {"steps": [
        step("good", "python backtest.py out/a", trading),
        step("weak", "python backtest.py out/b --drift 0.0003 --crash 0.3 --trips 12",
             {**trading, "files": {"equity": "out/b/equity.csv"}}),
        step("undefined", "python backtest.py out/c --bars 2 --no-trades --bench none",
             {"reader": "trading", "files": "out/c/equity.csv", "rules": {"sharpe": ">= 1", "win_rate": ">= 0.5"}}),
        step("several", write_json, [
            {"reader": "json", "files": "out/m.json", "rules": {"accuracy": ">= 0.9", "losses.-1": "< 0.5",
                                                                "f1": ">= 0.5"}},
            {"reader": "csv", "files": ["out/log.csv"], "rules": {"loss.min": "< 0.3", "lr.last": "< 1"}}]),
        step("missing", "python -c pass", {"reader": "json", "files": "out/none.json", "rules": {"a": 1, "b": 2}}),
        step("refused", "python -c \"open('bad.json', 'w').write('{oops')\"",
             {"reader": "json", "files": "bad.json", "rules": {"a": 1}}),
        step("found", "python backtest.py found --trips 3", {"reader": "trading", "rules": {"fills": 6}}),
    ]}})
    r = cli(root, "run", "p", "--wait", "300")
    assert r.returncode == 1 and "# FAIL - plan `p`" in r.stdout, r.stdout + r.stderr
    got = findings(root)

    good = got["good"]
    assert [(name, c["ok"]) for name, c in good.items()] == [
        ("exit code 0", True), ("trading out/a/equity.csv: sharpe >= 1", True),
        ("trading out/a/equity.csv: max_drawdown >= -0.2", True), ("trading out/a/equity.csv: trades >= 30", True)]
    want = TM.compute(root / "out/a/equity.csv", root / "out/a/trades.csv", calendar="stocks")
    assert good["trading out/a/equity.csv: sharpe >= 1"]["detail"] == f"{want['sharpe']:.6g} (wanted >= 1.0)", \
        "the number first, at 6 significant digits, as history and loops read it back"
    assert R.check_number(good["trading out/a/equity.csv: trades >= 30"]) == 40

    weak = got["weak"]
    assert not any(c["ok"] for name, c in weak.items() if name != "exit code 0")
    assert re.fullmatch(r"-0\.3\d+ \(wanted >= -0\.2\)",
                        weak["trading out/b/equity.csv: max_drawdown >= -0.2"]["detail"])
    assert weak["trading out/b/equity.csv: trades >= 30"]["detail"] == "12 (wanted >= 30.0)"

    undefined = got["undefined"]
    assert undefined["trading out/c/equity.csv: sharpe >= 1"] == {
        "check": "trading out/c/equity.csv: sharpe >= 1", "ok": False,
        "detail": "sharpe is undefined (null): volatility, sharpe and sortino are null: they need at least 3 rows "
                  "(2 returns); the backtest covers 1 days: the yearly numbers (cagr, volatility, sharpe, sortino, "
                  "calmar) stretch less than a year into one"}, \
        "a rule never passes for want of a number; the reader's notes on that number say why"
    assert undefined["trading out/c/equity.csv: win_rate >= 0.5"]["detail"].startswith(
        "win_rate is undefined (null): no trades file: fills, trades, win_rate, ")

    several = got["several"]
    assert [(name, c["ok"]) for name, c in several.items()] == [
        ("exit code 0", True), ("json out/m.json: accuracy >= 0.9", True), ("json out/m.json: losses.-1 < 0.5", True),
        ("json out/m.json: f1 >= 0.5", False), ("csv out/log.csv: loss.min < 0.3", True),
        ("csv out/log.csv: lr.last < 1", False)]
    assert several["json out/m.json: losses.-1 < 0.5"]["detail"] == "0.4 (wanted < 0.5)", \
        "json reads the dotted paths the rules name, -1 the last item, as the json: expectation does"
    assert several["json out/m.json: f1 >= 0.5"]["detail"] == "f1 is undefined (null): no 'f1' in out/m.json"
    assert several["csv out/log.csv: lr.last < 1"]["detail"] == (
        "reader csv gave no number 'lr.last' (did you mean loss.last?); it gave rows, epoch.last, epoch.mean, "
        "epoch.max, epoch.min, loss.last, loss.mean, loss.max, loss.min")

    missing = got["missing"]
    for name in ("json out/none.json: a 1", "json out/none.json: b 2"):
        assert not missing[name]["ok"] and missing[name]["detail"].startswith("out/none.json: no such file in "), \
            missing[name]
    assert got["refused"]["json bad.json: a 1"]["detail"].startswith("bad.json is not valid JSON")

    found = got["found"]["trading: fills 6"]
    assert found["ok"] and found["detail"] == "6 (wanted == 6); read found/equity.csv, found/trades.csv", \
        "no file named: the reader looked for its own, and the detail says which it read"


def test_a_file_from_an_earlier_run_is_refused_not_read(tmp_path):
    root = project(tmp_path / "proj", {"p": {"steps": [
        step("nothing written", "python -c pass",
             {"reader": "trading", "files": "out/old/equity.csv", "rules": {"sharpe": ">= 0"}}),
        step("trades left over", "python backtest.py out/mixed --no-trades",
             {"reader": "trading", "files": "out/mixed/equity.csv", "rules": {"sharpe": ">= 0", "trades": ">= 0"}}),
        step("fresh", "python backtest.py out/fresh", {"reader": "trading", "files": "out/fresh/equity.csv",
                                                        "rules": {"sharpe": ">= 0"}})]}})
    for folder in ("old", "mixed"):                         # what an earlier run left behind
        subprocess.run([sys.executable, str(BACKTEST), str(root / "out" / folder)], check=True, capture_output=True)
    old(*(root / "out" / f / n for f in ("old", "mixed") for n in ("equity.csv", "trades.csv")))

    r = cli(root, "run", "p", "--wait", "300")
    assert r.returncode == 1, r.stdout + r.stderr
    got = findings(root)
    stale = "the file was last written before this run started, so it is an old result, not this run's"
    assert got["nothing written"]["trading out/old/equity.csv: sharpe >= 0"] == {
        "check": "trading out/old/equity.csv: sharpe >= 0", "ok": False, "detail": f"out/old/equity.csv: {stale}"}
    mixed = got["trades left over"]
    assert [c["detail"] for name, c in mixed.items() if name != "exit code 0"] == \
        [f"out/mixed/trades.csv: {stale}"] * 2, "equity.csv is this run's, but the trades.csv next to it is not: refused"
    assert got["fresh"]["trading out/fresh/equity.csv: sharpe >= 0"]["ok"]


def install_kvlog(tmp_path: Path) -> dict:
    """The example metric reader installed as pip would: its module and a dist-info with its entry point."""
    site = tmp_path / "site"
    info = site / "testbench_metrics_example-0.1.0.dist-info"
    info.mkdir(parents=True)
    shutil.copy(EXAMPLE / "testbench_metrics_example.py", site)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: testbench-metrics-example\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text("[agent_testbench.metrics]\nkvlog = testbench_metrics_example:KeyValueLog\n")
    return {"PYTHONPATH": os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH")) if p)}


def test_an_installed_reader_works_in_numbers_where_it_is_installed(tmp_path):
    env = install_kvlog(tmp_path)
    root = project(tmp_path / "proj", {"p": {"steps": [step(
        "train",
        "python -c \"open('train.log', 'w').write('epoch=1 loss=0.9 acc=0.5\\nepoch=2 loss=0.25 acc=nan\\n')\"",
        {"reader": "kvlog", "files": "train.log", "options": {"keys": ["loss", "acc"]},
         "rules": {"loss": "< 0.3", "acc": ">= 0.8"}})]}})
    r = cli(root, "run", "p", "--wait", "120", env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    got = findings(root)["train"]
    assert got["kvlog train.log: loss < 0.3"] == {"check": "kvlog train.log: loss < 0.3", "ok": True,
                                                  "detail": "0.25 (wanted < 0.3)"}
    assert got["kvlog train.log: acc >= 0.8"]["detail"] == ("acc is undefined (null): acc ended at nan: not a finite "
                                                            "number, so it is null")
    assert "expected kvlog train.log: acc >= 0.8: acc is undefined (null)" in r.stdout

    r = cli(root, "plans")                                   # the same project where the reader is not installed
    assert r.returncode == 2, r.stdout + r.stderr
    assert "`plans.p.steps[0].expect.numbers.reader`: no reader 'kvlog'; the readers are json, csv, junit, trading" \
        in r.stderr


# ---- refused when testbench.yaml loads ---------------------------------------------------------------------------

TRADING = {"reader": "trading", "files": "out/equity.csv", "rules": {"sharpe": ">= 1"}}


@pytest.mark.parametrize("numbers, message", [
    ({**TRADING, "reader": "tradng"}, f"`{WHERE}.reader`: no reader 'tradng' (did you mean trading?); the readers are "
                                      "json, csv, junit, trading"),
    ({"files": "x.json", "rules": {"a": 1}}, f"`{WHERE}` needs `reader:`, one of json, csv, junit, trading"),
    ({**TRADING, "rule": {"a": 1}},
     f"`{WHERE}` has no 'rule' (did you mean rules?); use reader, files, options, rules"),
    ({"reader": "json"}, f"`{WHERE}` needs `rules:`, a rule on each number by name"),
    ({**TRADING, "rules": {}}, f"`{WHERE}.rules` is empty, so nothing would be checked"),
    ({**TRADING, "rules": {"sharpe": "=> 1"}}, f"`{WHERE}.rules.sharpe`: '=> 1': => is not a comparison"),
    ({**TRADING, "rules": {"sharpe": "== high"}}, f"`{WHERE}.rules.sharpe`: '== high' compares with text, but a "
                                                  "reader's numbers are numbers or true/false"),
    ({**TRADING, "rules": {"sharpe": None}}, "compares with nothing"),
    ({**TRADING, "rules": {"sharp": ">= 1"}}, f"`{WHERE}.rules` names 'sharp' (did you mean sharpe?), a number reader "
                                              "trading does not give; it gives start_equity, end_equity, "),
    ({"reader": "junit", "rules": {"failed": 0}}, "names 'failed' (did you mean failures?), a number reader junit"),
    ({**TRADING, "options": {"risk_fre": 0.04}}, f"`{WHERE}`: reader trading has no option 'risk_fre' (did you mean "
                                                 "risk_free?); its options are risk_free, periods_per_year, calendar"),
    ({**TRADING, "options": {"calendar": "stockz"}}, f"`{WHERE}`: reader trading: calendar is 'stockz'; use auto, "
                                                     "stocks (252 trading days a year) or crypto (365)"),
    ({**TRADING, "options": {"files": "x"}}, "files go under `files:`, next to `options:`"),
    ({**TRADING, "options": [1]}, f"`{WHERE}.options` is a mapping of the reader's options"),
    ({**TRADING, "files": {"fills": "x.csv"}},
     "reader trading has no file 'fills'; its files are equity, trades, prices"),
    ({**TRADING, "files": ["a", "b", "c", "d"]}, "reader trading reads 3 file(s) (equity, trades, prices); got 4"),
    ({**TRADING, "files": [""]},
     f"`{WHERE}.files` is a path, a list of paths in the reader's order, or {{role: path}}"),
    ([], f"`{WHERE}` is empty, so nothing would be checked"),
    ("trading", f"`{WHERE}` is {X.NUMBERS_EXAMPLE}, or a list of those"),
    (None, f"`{WHERE}` is {X.NUMBERS_EXAMPLE}, or a list of those"),
    ([TRADING, {"reader": "json"}], f"`{WHERE}[1]` needs `rules:`"),
])
def test_a_malformed_block_is_refused_at_load_with_the_near_miss(tmp_path, numbers, message):
    with pytest.raises(C.ConfigError) as info:
        load(tmp_path, numbers)
    assert message in str(info.value)


def test_every_reader_s_rules_load_and_a_reader_without_keys_takes_any_name(tmp_path):
    cfg = load(tmp_path, [TRADING, {"reader": "json", "rules": {"anything.at.all": "~= 1 +- 0.1"}},
                          {"reader": "csv", "files": "a.csv", "rules": {"loss.last": "between 0 and 1"}},
                          {"reader": "junit", "files": ["report.xml"], "rules": {"failures": 0, "tests": ">= 1"}},
                          {**TRADING, "rules": {"costs_modeled": True, "max_drawdown_recovered": "== true"},
                           "options": {"risk_free": 0.04, "periods_per_year": 252, "symbol": "SPY"}}])
    assert len(cfg["plans"]["p"]["steps"][0]["expect"]["numbers"]) == 5


# ---- the pieces --------------------------------------------------------------------------------------------------

def test_the_check_keeps_the_contract_and_is_registered_in_order():
    chk = P.get("checks", "numbers")
    assert isinstance(chk, X.NumbersCheck) and P.builtin("checks", "numbers") and P.check("checks", chk) == []
    assert X.KINDS.index("numbers") == X.KINDS.index("metrics") + 1 and "numbers" in X.RUN_ORDER


def test_a_float_is_shown_short_unless_that_would_hide_why_it_failed():
    assert X.compare_shown(1.2345678912, ">= 1") == (True, "1.23457 (wanted >= 1.0)")
    assert X.compare_shown(0.99999999, ">= 1") == (False, "0.99999999 (wanted >= 1.0)"), "1 would look like a pass"
    assert X.compare_shown(40, ">= 30") == (True, "40 (wanted >= 30.0)")
    assert X.number_rule_error(">= 1") is None and X.number_rule_error("== true") is None
    assert "compares with text" in X.number_rule_error("done") and "compares with text" in X.number_rule_error("> true")
    notes = ["no trades file: fills, trades, win_rate are null", "every fee in out/trades.csv is 0",
             "loss in log.csv has a nan value", "losses has 15 items: only losses.0 and losses.-1 are shown"]
    assert X.notes_about("trades", notes) == notes[:1], "the number's name as a word, not inside a file's name"
    assert X.notes_about("loss.last", notes) == [notes[2]] and X.notes_about("losses.-1", notes) == [notes[3]]


def test_a_reader_s_keys_are_held_to_what_it_reads():
    class Says(M.Reader):
        name, files, keys = "says", {"file": ("a.txt",)}, ("loss", "accuracy")

        def read(self, workdir, opts):
            self.path(workdir, opts, "file")
            return {"loss": 0.5}

        def sample(self):
            return {"a.txt": "x"}

    assert "keys names accuracy, but read() on its sample() files does not give it" in "\n".join(
        P.check("metrics", Says()))
    assert P.check("metrics", P.get("metrics", "trading")) == [] and M.keys("trading") == TM.KEYS
    assert M.keys("junit") == ("tests", "passed", "failures", "errors", "skipped") and M.keys("json") is None


def test_the_files_numbers_reads_are_what_watch_leaves_out():
    plan = {"steps": [{"name": "a", "run": "x", "expect": {"numbers": [TRADING, {"reader": "json", "rules": {"a": 1}},
                                                                       {"reader": "csv", "files": {"file": "l.csv"},
                                                                        "rules": {"a": 1}}]}}]}
    assert W.outputs(plan) == ["out/equity.csv", "l.csv"]


# ---- the schema --------------------------------------------------------------------------------------------------

def test_the_schema_takes_what_testbench_takes_and_names_the_trading_and_junit_numbers(tmp_path):
    validator = jsonschema.Draft202012Validator(SCHEMA)
    plan = lambda numbers: {"plans": {"p": {"steps": [{"name": "a", "run": "x", "expect": {"numbers": numbers}}]}}}
    for good in (TRADING, [TRADING, {"reader": "csv", "rules": {"loss.last": "< 0.3"}}], X.NumbersCheck.example,
                 {"reader": "trading", "files": {"equity": "e.csv", "trades": "t.csv"},
                  "options": {"calendar": "crypto"}, "rules": {"excess_return": "> 0"}}):
        assert not list(validator.iter_errors(plan(good))), good
        load(tmp_path, good)
    assert not list(validator.iter_errors(plan({"reader": "kvlog", "rules": {"loss": "< 0.3"}}))), \
        "a reader a package adds: the schema cannot know it, testbench checks it is installed"
    for bad in ({**TRADING, "rules": {"sharp": 1}}, {**TRADING, "options": {"risk_fre": 1}}, {"reader": "json"},
                {**TRADING, "rule": {}}, [], {"reader": "junit", "rules": {"failed": 0}},
                {**TRADING, "files": {"fills": "x"}}, {**TRADING, "options": {"calendar": "stockz"}}):
        assert list(validator.iter_errors(plan(bad))), bad
        with pytest.raises(C.ConfigError):
            load(tmp_path, bad)
    defs = SCHEMA["$defs"]
    assert tuple(defs["numbers"]["properties"]) == X.NUMBERS_KEYS
    assert set(defs["trading_options"]["properties"]) == set(P.get("metrics", "trading").options)
    assert tuple(defs["trading_number"]["enum"]) == TM.KEYS
    junit = defs["numbers"]["allOf"][1]["then"]["properties"]["rules"]["propertyNames"]["enum"]
    assert tuple(junit) == M.keys("junit")
