"""Metric readers: evidence files as named numbers. The four built-ins (json, csv, junit, trading) keep the reader
contract and read what the json: and junit: expectations and the trading pack read; a missing file is a
FileNotFoundError naming it and a refused one a ValueError saying what to change, nothing else; a value is a finite
number, a bool or None. A package adds a reader by an entry point, and `testbench numbers` shows what any of them
finds."""
import json
import re
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent_testbench import core, expect, metrics as M, plugins as P
from agent_testbench.trading import metrics as TM
from conftest import REPO, cli

EXAMPLE = REPO / "examples" / "plugins" / "testbench-metrics-example"
EQUITY = "time,equity\n" + "".join(f"2024-01-{d:02d},{v}\n" for d, v in
                                   zip((2, 3, 4, 5, 8, 9, 10), (100000, 100400, 99900, 101200, 101000, 100500, 102000)))
TRADES = "time,symbol,side,quantity,price,fee\n2024-01-02,SPY,buy,10,470,1\n2024-01-08,SPY,sell,10,480,1\n"


def write(folder: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    return folder


def install(site: Path, dist: str, entries: dict[str, str], group="agent_testbench.metrics") -> Path:
    """A package as pip leaves it: its files in a folder on the path, and a .dist-info with its entry points."""
    info = site / f"{dist.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text(f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items()))
    return site


def example_site(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    shutil.copy(EXAMPLE / "testbench_metrics_example.py", site)
    return install(site, "testbench-metrics-example", {"kvlog": "testbench_metrics_example:KeyValueLog"})


@pytest.fixture
def fresh_registry():
    P.reset("metrics")
    yield
    P.reset("metrics")


# ---- the built-ins and the contract ------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["json", "csv", "junit", "trading"])
def test_every_built_in_reader_keeps_the_contract(name):
    reader = P.get("metrics", name)
    assert P.builtin("metrics", name) and reader.name == name and isinstance(reader, M.Reader)
    assert P.check("metrics", reader) == [], f"reader {name} breaks the reader contract"


def test_metrics_is_core_and_reads_json_without_the_trading_pack():
    assert "agent_testbench.metrics" in core.CORE_MODULES
    code = ("import sys, tempfile, pathlib; from agent_testbench import metrics as M\n"
            "d = pathlib.Path(tempfile.mkdtemp()); (d / 'metrics.json').write_text('{\"a\": 1}')\n"
            "print(M.read('json', d)['values'], 'agent_testbench.trading' in sys.modules, 'agent_testbench.cli' in "
            "sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and out.stdout.split() == ["{'a':", "1}", "False", "False"], out.stdout + out.stderr


@pytest.mark.parametrize("name, given", [("json", "out/metrics.json"), ("csv", "log.csv"), ("junit", "junit.xml"),
                                         ("trading", "out/equity.csv")])
def test_a_missing_file_is_a_file_not_found_error_that_names_it(tmp_path, name, given):
    with pytest.raises(FileNotFoundError, match=f"no such file in {re.escape(str(tmp_path))}"):
        M.read(name, tmp_path, {"files": given})
    with pytest.raises(FileNotFoundError, match=given.replace(".", r"\.")):
        M.read(name, tmp_path, {"files": given})
    label = {"trading": "equity file"}.get(name, "file")
    with pytest.raises(FileNotFoundError, match=f"no {label} in .*{name} looked for"):
        M.read(name, tmp_path)
    (tmp_path / "out" / "x").mkdir(parents=True)
    with pytest.raises(M.MetricsError, match=f"out/x is a folder; name the {label} in it"):
        M.read(name, tmp_path, {"files": "out/x"})


def test_the_contract_names_what_a_reader_gets_wrong():
    class Sloppy(M.Reader):                          # KeyError for a missing file, NaN and text for numbers
        name = "Sloppy Reader"
        files = {"log": "x.log"}

        def read(self, workdir, opts):
            if not (Path(workdir) / "x.log").exists():
                raise KeyError("x.log")
            return {"loss": math.nan, "model": "tiny", "steps": 3}

        def sample(self):
            return {"x.log": "loss=nan\n"}

    problems = P.check("metrics", Sloppy())
    assert problems[0].startswith("its name is 'Sloppy Reader'; a reader's name is lowercase letters")
    assert "when the folder is empty, read() raises KeyError ('x.log'); it must raise FileNotFoundError naming the " \
           "file" in problems
    assert any(p.startswith("loss is nan (float); a value is a finite int or float") for p in problems), problems
    assert any(p.startswith("model is 'tiny' (str)") for p in problems), problems

    class Shifty(M.Reader):                          # names that change between reads, a message without the file
        name = "shifty"
        files = {"file": ("a.txt",)}
        calls = 0

        def read(self, workdir, opts):
            if not (Path(workdir) / "a.txt").exists():
                raise FileNotFoundError("nothing to read")
            Shifty.calls += 1
            return {f"n{Shifty.calls}": 1}

        def sample(self):
            return {"a.txt": "1"}

    problems = P.check("metrics", Shifty())
    assert any("raises FileNotFoundError, but its message does not name the file: nothing to read" in p
               for p in problems), problems
    assert problems[-1] == "reading the same files twice gave different names; names are stable, as checks name them"

    class Quiet(M.Reader):
        name = "quiet"

        def read(self, workdir, opts):
            return {}

    problems = P.check("metrics", Quiet())
    assert "when the folder is empty, read() returns; it must raise FileNotFoundError naming the file" in problems
    assert problems[-1].startswith("sample() gives no files, so its numbers cannot be checked")
    assert P.check("metrics", object())[0].startswith("it cannot be made into a reader: MetricsError: a reader has "
                                                      "read(workdir, opts)")


# ---- the json reader ---------------------------------------------------------------------------------------------

def test_json_reads_every_number_by_the_path_the_json_expectation_uses(tmp_path):
    data = {"accuracy": 0.93, "eval": {"f1": 0.8, "ok": True, "note": "fine", "skip": None},
            "losses": [1.0 - i / 20 for i in range(15)], "pair": [3, 4]}
    write(tmp_path, {"outputs/metrics.json": json.dumps(data)})
    got = M.read("json", tmp_path)
    assert got["files"] == {"file": "outputs/metrics.json"}, "found one folder down"
    assert got["values"] == {"accuracy": 0.93, "eval.f1": 0.8, "eval.ok": True, "eval.skip": None, "losses.0": 1.0,
                             "losses.-1": 0.30000000000000004, "pair.0": 3, "pair.1": 4}
    assert any("losses has 15 items: only losses.0 and losses.-1 are shown" in n for n in got["notes"])
    assert "1 text value(s) in outputs/metrics.json left out: they are not numbers" in got["notes"]
    for key, value in got["values"].items():
        assert value == expect.dig(data, key), f"{key} is what the json: expectation reads"
    got = M.read("json", tmp_path, {"keys": ["losses.-1", "eval.f1", "eval.note", "nope.x"]})
    assert got["values"] == {"losses.-1": data["losses"][-1], "eval.f1": 0.8, "eval.note": None, "nope.x": None}
    assert "eval.note in outputs/metrics.json is str, not a number" in got["notes"]
    assert "no 'nope.x' in outputs/metrics.json" in got["notes"]
    assert M.read("json", tmp_path, {"keys": "accuracy"})["values"] == {"accuracy": 0.93}, "one key as a word"


def test_json_nan_is_null_bad_json_is_refused_and_two_matches_are_named(tmp_path):
    write(tmp_path, {"metrics.json": '{"loss": NaN, "big": Infinity, "n": 2}'})
    got = M.read("json", tmp_path)
    assert got["values"] == {"loss": None, "big": None, "n": 2}
    assert "loss is nan: not a finite number, so it is null" in got["notes"]
    write(tmp_path, {"bad.json": "{oops"})
    with pytest.raises(M.MetricsError, match="bad.json is not valid JSON"):
        M.read("json", tmp_path, {"files": "bad.json"})
    (tmp_path / "metrics.json").unlink()
    write(tmp_path, {"a/metrics.json": "{}", "b/metrics.json": "{}"})
    with pytest.raises(M.MetricsError, match=r"2 files match \*/metrics.json in .* \(a/metrics.json, b/metrics.json\); "
                                             "name the file to read"):
        M.read("json", tmp_path)
    write(tmp_path, {"list.json": "[1, 2.5]"})
    assert M.read("json", tmp_path, {"files": "list.json"})["values"] == {"0": 1, "1": 2.5}


# ---- the csv reader ----------------------------------------------------------------------------------------------

def test_csv_reads_each_numeric_column_s_last_mean_max_and_min(tmp_path):
    write(tmp_path, {"metrics.csv": "epoch,loss,phase,lr\n1,0.9,warm,0.1\n2,0.4,train,\n\n3,0.5,train,0.01\n"})
    got = M.read("csv", tmp_path)
    assert got["values"] == {"rows": 3, "epoch.last": 3, "epoch.mean": 2.0, "epoch.max": 3, "epoch.min": 1,
                             "loss.last": 0.5, "loss.mean": pytest.approx(0.6), "loss.max": 0.9, "loss.min": 0.4,
                             "lr.last": 0.01, "lr.mean": pytest.approx(0.055), "lr.max": 0.1, "lr.min": 0.01}
    assert got["notes"] == ["left out text columns: phase"]
    got = M.read("csv", tmp_path, {"columns": ["loss", "phase"], "stats": "min"})
    assert got["values"] == {"rows": 3, "loss.min": 0.4, "phase.min": None}
    assert got["notes"] == ["phase in metrics.csv holds text, not numbers: its values are null"]
    with pytest.raises(M.MetricsError, match="metrics.csv has no column 'acc'; the columns are epoch, loss, phase, lr"):
        M.read("csv", tmp_path, {"columns": "acc"})
    with pytest.raises(M.MetricsError, match="has no stat 'median'; the stats are last, mean, max, min"):
        M.read("csv", tmp_path, {"stats": ["median"]})
    write(tmp_path, {"nan.csv": "loss\n0.5\nnan\n", "empty.csv": "\n"})
    got = M.read("csv", tmp_path, {"files": "nan.csv"})
    assert got["values"] == {"rows": 2, "loss.last": None, "loss.mean": None, "loss.max": None, "loss.min": None}
    assert "loss in nan.csv has a nan or infinite value: its last value, mean, max and min are null" in got["notes"]
    with pytest.raises(M.MetricsError, match="empty.csv is empty; its first line is the header"):
        M.read("csv", tmp_path, {"files": "empty.csv"})


# ---- the junit reader --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("report", ["jest-fail.xml", "vitest-pass.xml", "mocha-xunit-fail.xml", "nextest-fail.xml"])
def test_junit_reads_what_the_junit_expectation_reads(tmp_path, report):
    shutil.copy(REPO / "tests" / "data" / "junit" / report, tmp_path / "junit.xml")
    got, want = M.read("junit", tmp_path), expect.read_junit(tmp_path / "junit.xml")
    assert got["values"] == {k: want[k] for k in ("tests", "passed", "failures", "errors", "skipped")}
    assert [n for n in got["notes"] if n.startswith("failed: ")] == [f"failed: {c}" for c in want["failing"][:5]]


def test_junit_refuses_a_file_that_is_not_a_report(tmp_path):
    write(tmp_path, {"junit.xml": "<html><body>no</body></html>", "broken.xml": "<testsuite"})
    with pytest.raises(M.MetricsError, match="junit.xml is not a JUnit XML report .*pytest --junitxml"):
        M.read("junit", tmp_path)
    with pytest.raises(M.MetricsError, match="broken.xml is not a JUnit XML report"):
        M.read("junit", tmp_path, {"files": "broken.xml"})


# ---- the trading reader ------------------------------------------------------------------------------------------

def test_trading_gives_the_trading_pack_s_own_numbers_by_name(tmp_path):
    write(tmp_path, {"out/equity.csv": EQUITY, "out/trades.csv": TRADES})
    got = M.read("trading", tmp_path, {"risk_free": 0.04})
    want = TM.compute(tmp_path / "out/equity.csv", tmp_path / "out/trades.csv", risk_free=0.04)
    assert got["files"] == {"equity": "out/equity.csv", "trades": "out/trades.csv"}, "trades.csv next to equity.csv"
    assert got["values"] == {k: want[k] for k in TM.KEYS} and list(got["values"]) == list(TM.KEYS)
    assert got["notes"] == want["notes"] and got["values"]["risk_free"] == 0.04
    alone = M.read("trading", tmp_path, {"files": {"equity": "out/equity.csv"}, "calendar": "crypto"})
    assert alone["files"]["trades"] == "out/trades.csv", "found next to the equity file it was given"
    assert alone["values"]["periods_per_year"] == 365
    write(tmp_path, {"elsewhere/equity.csv": EQUITY})
    only = M.read("trading", tmp_path, {"files": ["elsewhere/equity.csv"]})
    assert only["files"] == {"equity": "elsewhere/equity.csv"} and only["values"]["trades"] is None
    with pytest.raises(M.MetricsError, match=r"reader trading has no option 'risk_fre' \(did you mean risk_free\?\)"):
        M.read("trading", tmp_path, {"risk_fre": 0.04})
    write(tmp_path, {"bad/equity.csv": "time,equity\n2024-01-02,100\n2024-01-03,-5\n"})
    with pytest.raises(ValueError, match="bad/equity.csv"):
        M.read("trading", tmp_path, {"files": "bad/equity.csv"})
    with pytest.raises(M.MetricsError, match="reader trading reads 3 file.* got 4"):
        M.read("trading", tmp_path, {"files": ["a", "b", "c", "d"]})
    with pytest.raises(M.MetricsError, match="no file 'fills'; its files are equity, trades, prices"):
        M.read("trading", tmp_path, {"files": {"fills": "x.csv"}})


# ---- metrics.read keeps every reader to the contract -------------------------------------------------------------

def test_read_turns_a_reader_s_slips_into_plain_errors_and_nulls(tmp_path, fresh_registry):
    class Buggy(M.Reader):
        name = "buggy"
        options = {"mode": "what to return"}

        def read(self, workdir, opts):
            if opts.get("mode") == "crash":
                return {}["boom"]
            if opts.get("mode") == "text":
                return {"model": "tiny"}
            return {"loss": math.inf, "n": True, "notes": ["it ran"], "files": {"log": "x.log"}}

    P.register("metrics", "buggy", Buggy)
    got = M.read("buggy", tmp_path)
    assert got == {"reader": "buggy", "files": {"log": "x.log"}, "values": {"loss": None, "n": True},
                   "notes": ["it ran", "loss is inf: not a finite number, so it counts as none and passes no rule"]}
    with pytest.raises(M.MetricsError, match="reader buggy could not read .*: KeyError: 'boom'"):
        M.read("buggy", tmp_path, {"mode": "crash"})
    with pytest.raises(M.MetricsError, match="reader buggy gave model = 'tiny', which is not a number"):
        M.read("buggy", tmp_path, {"mode": "text"})
    with pytest.raises(M.MetricsError, match=r"no reader 'bugy' \(did you mean buggy\?\); the readers are json, csv"):
        M.read("bugy", tmp_path)


# ---- installed readers -------------------------------------------------------------------------------------------

def test_an_installed_reader_is_found_by_its_entry_point_and_keeps_the_contract(tmp_path, monkeypatch,
                                                                               fresh_registry):
    site = example_site(tmp_path)
    (site / "tb_clash.py").write_text("from agent_testbench.metrics import JsonReader\nclass Mine(JsonReader):\n"
                                      "    pass\n")
    install(site, "tb-clash", {"json": "tb_clash:Mine", "gone": "tb_clash:Missing"})
    monkeypatch.syspath_prepend(str(site))
    try:
        assert M.names() == ["json", "csv", "junit", "trading", "kvlog"]
        reader = P.get("metrics", "kvlog")
        assert type(reader).__name__ == "KeyValueLog" and P.check("metrics", reader) == []
        assert P.get("metrics", "json").__class__ is M.JsonReader, "a built-in is never replaced"
        write(tmp_path / "proj", {"train.log": "epoch=1 loss=0.91 acc=0.52\nsaved\nepoch=2 loss=0.25 acc=nan\n"})
        got = M.read("kvlog", tmp_path / "proj")
        assert got["values"] == {"epoch": 2, "loss": 0.25, "acc": None, "lines": 2}
        assert got["files"] == {"log": "train.log"} and "acc ended at nan: not a finite number, so it is null" \
            in got["notes"]
        listing = M.listing()
        assert "\nkvlog     the last value of each key=value" in listing
        assert "\n            from testbench-metrics-example\n" in listing
        assert "Installed but not loaded" in listing and "\n  gone from tb-clash: tb_clash:Missing did not load: " \
            "AttributeError" in listing
        with pytest.raises(M.MetricsError, match="reader 'gone' is installed but did not load"):
            M.read("gone", tmp_path)
    finally:
        for mod in ("testbench_metrics_example", "tb_clash"):
            sys.modules.pop(mod, None)


# ---- testbench numbers -------------------------------------------------------------------------------------------

def test_numbers_prints_what_a_reader_finds(tmp_path):
    write(tmp_path, {"out/equity.csv": EQUITY, "out/trades.csv": TRADES})
    r = cli(tmp_path, "numbers")
    assert r.returncode == 0 and r.stdout.startswith("Readers turn evidence files into named numbers"), r.stdout
    for line in ("\njson      a JSON file's numbers", "\ntrading   testbench's own backtest numbers",
                 "\n            equity: equity.csv, */equity.csv\n", "options: risk_free, periods_per_year"):
        assert line in r.stdout, line
    r = cli(tmp_path, "numbers", "trading", "out/equity.csv", "out/trades.csv", "--opt", "risk_free=0.04")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.startswith("trading read out/equity.csv, out/trades.csv:\n  start_equity ")
    assert "\n  trades                  1\n" in r.stdout and "\nnotes:\n  - periods per year: 252" in r.stdout
    r = cli(tmp_path, "numbers", "trading", "--json")
    got = json.loads(r.stdout)
    assert r.returncode == 0 and got["reader"] == "trading" and got["files"]["trades"] == "out/trades.csv"
    assert got["values"]["risk_free"] == 0 and set(got["values"]) == set(TM.KEYS)
    r = cli(tmp_path, "numbers", "trading", "missing/equity.csv")
    assert r.returncode == 2 and r.stdout.startswith("testbench: missing/equity.csv: no such file in "), r.stdout
    r = cli(tmp_path, "numbers", "trading", "--opt", "risk_free")
    assert r.returncode == 2 and "--opt is KEY=VALUE, e.g. --opt risk_free=0.04" in r.stdout
    r = cli(tmp_path, "numbers", "nope")
    assert r.returncode == 2 and "no reader 'nope'; the readers are json, csv, junit, trading" in r.stdout
    write(tmp_path, {"log.csv": "loss,accuracy\n0.5,0.7\n0.25,0.9\n"})
    r = cli(tmp_path, "numbers", "csv", "log.csv", "--opt", "columns=[loss]", "--opt", "stats=[last, min]", "--json")
    assert json.loads(r.stdout)["values"] == {"rows": 2, "loss.last": 0.25, "loss.min": 0.25}


def test_numbers_uses_an_installed_reader(tmp_path):
    env = {"PYTHONPATH": os.pathsep.join(p for p in (str(example_site(tmp_path)), os.environ.get("PYTHONPATH", ""))
                                         if p)}
    proj = write(tmp_path / "proj", {"logs/run1.log": "loss=0.5 acc=0.7\nloss=0.2 acc=0.9\n"})
    r = cli(proj, "numbers", env=env)
    assert r.returncode == 0 and "\nkvlog     the last value of each key=value" in r.stdout, r.stdout + r.stderr
    assert "\n            from testbench-metrics-example\n" in r.stdout
    r = cli(proj, "numbers", "kvlog", "logs/run1.log", "--opt", "keys=[loss]", "--json", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(r.stdout) == {"reader": "kvlog", "files": {"log": "logs/run1.log"},
                                    "values": {"loss": 0.2, "lines": 2}, "notes": []}
    r = cli(proj, "numbers", "kvlog", env=env)
    assert r.returncode == 2 and "no log file in " in r.stdout and "kvlog looked for metrics.log, train.log" in r.stdout
