"""Look-ahead and model cutoffs (dataseal.py, docs/trading-data.md): a plan with `lookahead:` runs its steps once on
the data it may see, then again on that data cut at earlier times, and fails when a decision dated before a cut changed
with the rows after it; a crashing rerun is a crash, not look-ahead. `model_cutoff:` warns when results are on data
the model the strategy asks may already know (and fails with require_after when no test period is after it)."""
import datetime as dt
import json
import math
import random
import re
from pathlib import Path

import pytest
import yaml

from agent_testbench import budget, config as C, dataseal as DS, report as R, runs
from conftest import cli

HOLDOUT = "2023-10-02"
LAST = "2023-09-29"                                       # the last row before the seal: what the plans see
SYMBOLS = ("AAPL", "MSFT")
STRATEGY = '''\
import csv, json, os, sys
from collections import defaultdict
rows = list(csv.DictReader(open(os.environ["TESTBENCH_DATA_PRICES"])))
mode = os.environ.get("MODE", "clean")
if mode == "crash" and max(r["time"] for r in rows) < os.environ["CRASH_BEFORE"]:
    sys.exit(3)
if mode == "cache":                                       # a statistic of every row, kept for the next run
    if not os.path.exists("cache.json"):
        json.dump({"mean": sum(float(r["close"]) for r in rows) / len(rows)}, open("cache.json", "w"))
    mean = json.load(open("cache.json"))["mean"]
by = defaultdict(list)
for r in rows:
    by[r["symbol"]].append((r["time"], float(r["close"])))
out = ["time,symbol,signal" + (",weight" if mode == "float" else "")]
for symbol, series in sorted(by.items()):
    series.sort()
    closes = [c for _, c in series]
    for i, (t, c) in enumerate(series):
        if i < 20:
            continue
        fast, slow = sum(closes[i - 4:i + 1]) / 5, sum(closes[i - 19:i + 1]) / 20     # today's close and before
        signal = 1 if fast > slow else 0
        if mode == "peek" or mode == "late" and t >= os.environ["PEEK_FROM"]:
            if i + 1 == len(series):
                if mode == "late":
                    continue                              # no tomorrow, no decision: df.shift(-1).dropna()
                signal = 0
            else:
                signal = 1 if closes[i + 1] > c else 0    # tomorrow's close
        if mode == "cache":
            signal = 1 if c > mean else 0
        weight = f",{fast / slow + float(os.environ.get('JITTER', '0')) * len(rows)!r}" if mode == "float" else ""
        out.append(f"{t},{symbol},{signal}{weight}")
os.makedirs("out", exist_ok=True)
if mode != "silent":
    open("out/signals.csv", "w").write("\\n".join(out) + "\\n")
print(len(rows), "rows,", len(out) - 1, "decisions")
'''
LOOK = {"lookahead": {"data": "prices", "output": "out/signals.csv"},
        "steps": [{"name": "strategy", "run": "python strategy.py", "max_minutes": 2}]}


def weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    return [start + dt.timedelta(days=i) for i in range((end - start).days) if (start + dt.timedelta(days=i))
            .weekday() < 5]


def write_prices(path: Path, start=dt.date(2022, 1, 3), end=dt.date(2024, 1, 1)) -> dict:
    """Two symbols' closes, a seeded random walk each, rows of both mixed by day; {symbol: [(day, close)]}."""
    rng, closes, series, lines = random.Random(11), {s: 100.0 for s in SYMBOLS}, {s: [] for s in SYMBOLS}, \
        ["time,symbol,close"]
    for d in weekdays(start, end):
        for s in SYMBOLS:
            closes[s] = round(closes[s] * (1 + rng.gauss(0, 0.012)), 2)
            series[s].append((d.isoformat(), closes[s]))
            lines.append(f"{d.isoformat()},{s},{closes[s]:.2f}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return series


def project(tmp_path: Path, plans: dict, name="proj", seal=True, **more) -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    write_prices(root / "data" / "prices.csv")
    (root / "strategy.py").write_text(STRATEGY)
    data = {"prices": {"path": "data/prices.csv", **({"holdout_from": HOLDOUT} if seal else {})}}
    (root / "testbench.yaml").write_text(yaml.safe_dump({"project": "look", "data": data, "plans": plans, **more},
                                                        sort_keys=False))
    return root


def plan(**env) -> dict:
    return {**LOOK, "steps": [{**LOOK["steps"][0], "env": {k: str(v) for k, v in env.items()}}]}


def launch(root: Path, name: str) -> tuple[Path, dict]:
    run_dir = runs.launch(root, C.load(root), name, foreground=True)
    return run_dir, json.loads((run_dir / "report.json").read_text())


REPORT_SCHEMA = json.loads((Path(C.__file__).parent / "schema" / "report-v1.schema.json").read_text())


# ---- a clean strategy passes; the cuts are where they should be -------------------------------------------------

def test_a_clean_strategy_passes_and_the_cuts_are_deterministic_and_past_the_first_fifth(tmp_path):
    root = project(tmp_path, {"clean": LOOK})
    lines = (root / "data" / "prices.csv").read_text()
    assert C.load(root)["plans"]["clean"]["workdir"] == "copy", "reruns cut the data: only in a copy"
    run_dir, rep = launch(root, "clean")
    la = rep["lookahead"]
    assert rep["status"] == "passed" and la["verdict"] == "clean" and la["ok"], la
    assert (la["data"], la["output"], la["cuts"], la["last"]) == ("prices", "out/signals.csv", 4, LAST), \
        "the plan's own run sees the rows before the seal"
    kept = [ln for ln in lines.splitlines()[1:] if ln < HOLDOUT]
    times = sorted({ln.split(",")[0] for ln in kept})
    cuts = [r["before"] for r in la["runs"]]
    assert [r["status"] for r in la["runs"]] == ["same"] * 4 and cuts == sorted(cuts) and cuts[-1] == LAST
    for r in la["runs"]:
        assert r["data_rows"] == sum(ln < r["before"] for ln in kept) >= math.ceil(0.2 * len(kept))
        assert r["before"] in times and r["differences"] == 0 and r["rows_compared"] > 0
    assert la["rows_compared"] == sum(r["rows_compared"] for r in la["runs"])
    assert la["finding"] == (f"look-ahead: none found - 4 reruns with the data cut at {', '.join(cuts)} decided "
                             f"every one of the {la['rows_compared']} rows dated before its cut as the plan's own "
                             "run did")
    assert [s["name"] for s in rep["steps"]] == ["strategy"] + [f"strategy (look-ahead cut {k}/4)"
                                                                for k in (1, 2, 3, 4)]
    assert [s.get("lookahead") for s in rep["steps"]] == [None, 1, 2, 3, 4]
    step = json.loads(next((run_dir / "steps").glob("03-*/data.json")).read_text())["prices"]
    assert (step["rows"], step["last"]) == (la["runs"][1]["data_rows"], times[times.index(cuts[1]) - 1]), \
        "each rerun's data.json is its cut"
    assert (root / "data" / "prices.csv").read_text() == lines, "the project's own file is untouched"
    evidence = {e["path"] for e in rep["evidence"]}
    assert {"steps/lookahead/full.csv", "steps/lookahead/cut-4.csv"} <= evidence, "the decisions are evidence"
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)

    md = (run_dir / "report.md").read_text()
    assert f"## Look-ahead: none found (4 reruns, {la['rows_compared']} rows compared)" in md
    assert f"| PASS | 1/4 | {cuts[0]} | {la['runs'][0]['data_rows']} |" in md and "**FAIL**" not in md
    html = R.render_html(rep, run_dir)
    assert "<h2>Look-ahead: none found" in html and "rows compared</th>" in html

    _, again = launch(root, "clean")
    assert [r["before"] for r in again["lookahead"]["runs"]] == cuts, "the same data gives the same cuts"

    root = project(tmp_path, {"clean": {**LOOK, "lookahead": {"output": "out/signals.csv", "cuts": 2}}},
                   name="unsealed", seal=False)
    assert C.load(root)["plans"]["clean"]["workdir"] == "copy", "no seal, but the reruns cut: a copy all the same"
    _, rep = launch(root, "clean")
    assert rep["lookahead"]["verdict"] == "clean" and rep["lookahead"]["last"] == "2023-12-29", "every row, unsealed"
    assert rep["lookahead"]["runs"][-1]["before"] == "2023-12-29" and len(rep["steps"]) == 3


# ---- look-ahead is caught -----------------------------------------------------------------------------------------

def test_a_strategy_that_uses_tomorrows_close_fails_with_the_first_difference(tmp_path):
    root = project(tmp_path, {"peek": plan(MODE="peek")})
    series = write_prices(root / "data" / "prices.csv")
    run_dir, rep = launch(root, "peek")
    la = rep["lookahead"]
    assert rep["status"] == "failed" and la["verdict"] == "look-ahead" and not la["ok"]
    assert all(s["status"] == "passed" for s in rep["steps"]) and len(rep["steps"]) == 5, "every rerun ran"
    expected = []                     # each cut's last row: tomorrow was up in the full run, unknown (0) in the rerun
    for k, r in enumerate(la["runs"], 1):
        for s in SYMBOLS:
            days = [(t, c) for t, c in series[s] if t < HOLDOUT]
            j = max(i for i, (t, _) in enumerate(days) if t < r["before"])
            if j >= 20 and days[j + 1][1] > days[j][1]:
                expected.append((days[j][0], s, k))
    day, symbol, k = min(expected)
    cut = la["runs"][k - 1]["before"]
    assert la["finding"] == (f"look-ahead: {symbol} at {day} was `1` with data up to {LAST} but was `0` with data "
                             f"before {cut} - the strategy used rows after {day} to decide it")
    first = la["runs"][k - 1]["first_difference"]
    assert (first["time"], first["symbol"], first["full"], first["cut"]) == (day, symbol, "1", "0")
    md = (run_dir / "report.md").read_text()
    assert md.index(f"**FAIL** - {la['finding']}") < md.index("## Data"), "the finding comes first"
    assert "## Look-ahead: FOUND" in md and "| FAIL |" in md
    assert "<h2>Look-ahead: FOUND</h2>" in R.render_html(rep, run_dir)
    junit = R.render_junit(rep)
    assert 'name="look-ahead"' in junit and 'type="LookAhead"' in junit and 'failures="1"' in junit
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)


def test_a_strategy_that_peeks_only_near_the_end_is_still_caught(tmp_path):
    root = project(tmp_path, {"late": plan(MODE="late", PEEK_FROM="2023-09-20")})
    _, rep = launch(root, "late")
    la = rep["lookahead"]
    assert [r["status"] for r in la["runs"]] == ["same", "same", "same", "different"], "only the last cut sees it"
    assert la["runs"][-1]["before"] == LAST and la["verdict"] == "look-ahead" and rep["status"] == "failed"
    assert re.fullmatch(r"look-ahead: AAPL at 2023-09-28 was `[01]` with data up to 2023-09-29 but had no row with "
                        r"data before 2023-09-29 - the strategy used rows after 2023-09-28 to decide it", la["finding"])


def test_a_rerun_starts_from_the_project_as_it_was_so_a_cache_cannot_hide_the_future(tmp_path):
    """The plan's own run caches a statistic of every row; were cache.json still there, every rerun would reuse it
    and decide just the same."""
    root = project(tmp_path, {"cache": plan(MODE="cache")})
    _, rep = launch(root, "cache")
    la = rep["lookahead"]
    assert la["verdict"] == "look-ahead" and la["runs"][0]["status"] == "different", la["finding"]
    first = la["runs"][0]["first_difference"]
    assert first["time"] < la["runs"][0]["before"] and la["finding"].endswith(
        f"the strategy used rows after {first['time']} to decide it")


@pytest.mark.posix
def test_putting_the_copy_back_removes_links_and_never_what_they_point_at(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("not the project's\n")
    root = project(tmp_path, {"clean": {**LOOK, "steps": [{**LOOK["steps"][0], "run": "python strategy.py && "
                                                            "mkdir -p made && ln -sfn ../ext made/link"}]}})
    (root / "ext").symlink_to(outside, target_is_directory=True)
    run_dir, rep = launch(root, "clean")
    assert rep["status"] == "passed" and rep["lookahead"]["verdict"] == "clean", rep.get("stopped_because")
    assert (outside / "keep.txt").read_text() == "not the project's\n" and (run_dir / "work" / "ext").is_symlink()


def test_numbers_compare_within_a_tiny_tolerance_and_a_finding_names_the_column(tmp_path):
    root = project(tmp_path, {"noise": plan(MODE="float", JITTER="1e-15"), "drift": plan(MODE="float", JITTER="1e-7")})
    _, rep = launch(root, "noise")
    assert rep["lookahead"]["verdict"] == "clean", "a last-digit difference is the same decision"
    _, rep = launch(root, "drift")
    assert rep["lookahead"]["verdict"] == "look-ahead"
    assert re.match(r"look-ahead: AAPL at 2022-01-31 \(weight\) was `[0-9.]+` with data up to 2023-09-29 but was "
                    r"`[0-9.]+` with data before ", rep["lookahead"]["finding"])
    assert DS.same_decision("0.1", "0.1000000000001") and DS.same_decision("nan", "NaN")
    assert not DS.same_decision("0.1", "0.1001") and not DS.same_decision("buy", "sell")
    assert not DS.same_decision("1", None) and DS.same_decision(None, None)


# ---- what is not look-ahead ----------------------------------------------------------------------------------------

def test_a_rerun_that_crashes_is_reported_as_a_crash_not_as_look_ahead(tmp_path):
    root = project(tmp_path, {"crash": plan(MODE="crash", CRASH_BEFORE="2023-01-01"),
                              "silent": plan(MODE="silent")})
    run_dir, rep = launch(root, "crash")
    la = rep["lookahead"]
    first = la["runs"][0]
    assert rep["status"] == "failed" and la["verdict"] == "crashed" and first["status"] == "crashed"
    assert [r["status"] for r in la["runs"][1:]] == ["skipped"] * 3
    assert rep["skipped"] == [f"strategy (look-ahead cut {k}/4)" for k in (2, 3, 4)]
    assert la["finding"] == (f"look-ahead not checked: the rerun with data before {first['before']} (cut 1/4) crashed "
                             "- step `strategy (look-ahead cut 1/4)` failed: exited 3 (its log: "
                             "steps/02-strategy-look-ahead-cut-1-4/log.txt). That is a crash, not look-ahead: make the "
                             f"steps run on less data (this rerun had {first['data_rows']} rows), then run the plan "
                             "again")
    md = (run_dir / "report.md").read_text()
    assert "## Look-ahead: not checked, a rerun crashed" in md and "| CRASHED | 1/4 |" in md
    assert "used rows after" not in md

    _, rep = launch(root, "silent")
    assert rep["status"] == "failed" and rep["lookahead"]["verdict"] == "not checked"
    assert rep["lookahead"]["finding"].startswith("look-ahead not checked: the plan's own run wrote no "
                                                  "out/signals.csv, the file lookahead.output names")
    assert rep["stopped_because"] == rep["lookahead"]["finding"] and len(rep["steps"]) == 1 and \
        len(rep["skipped"]) == 4, "no rerun runs when there is nothing to compare"


def test_a_cut_rerun_compares_only_rows_dated_before_its_cut_and_missing_rows_differ():
    rows = lambda pairs: {"columns": ["signal"], "rows": {  # noqa: E731
        (DS._seconds(DS._parse(t)), s, 0): {"t": DS._parse(t), "time": t, "symbol": s, "values": {"signal": v}}
        for t, s, v in pairs}}
    full = rows([("2024-01-02", "AAPL", "1"), ("2024-01-03", "AAPL", "0"), ("2024-01-04", "AAPL", "1")])
    cut = rows([("2024-01-02", "AAPL", "1")])
    n, diffs = DS.compare_decisions(full, cut, DS._parse("2024-01-04"))
    assert n == 2 and len(diffs) == 1 and diffs[0]["time"] == "2024-01-03" and diffs[0]["missing"] == "cut"
    n, diffs = DS.compare_decisions(full, rows([("2024-01-02", "AAPL", "1")]), DS._parse("2024-01-03"))
    assert (n, diffs) == (1, []), "a row dated at or after the cut is not compared"


# ---- cost: the worst case and the hard stop count every rerun ------------------------------------------------------

def test_the_worst_case_and_the_hard_stop_count_one_plus_cuts_runs(tmp_path):
    graded = {**LOOK["steps"][0], "artifacts": ["out/signals.csv"], "expect": {"exit_code": 0, "files": ["out/x"]}}
    root = project(tmp_path, {"la": {**LOOK, "backend": "docker", "steps": [graded]},
                              "six": {**LOOK, "backend": "docker", "lookahead": {**LOOK["lookahead"], "cuts": 6}},
                              "once": {**LOOK, "backend": "docker", "lookahead": None}}, docker={"usd_per_hour": 6.0})
    cfg = C.load(root)
    once, la, six = (budget.worst_case(cfg, p) for p in ("once", "la", "six"))
    assert (la["runs"], six["runs"]) == (5, 7) and "runs" not in once
    assert la["max_minutes"] == 5 * once["max_minutes"] == 10 and six["max_minutes"] == 14
    assert la["worst_case_usd"] > once["worst_case_usd"] and "(its run and 4 look-ahead reruns)" in la["what"]
    plan_json = DS.prepare(root, cfg, "la")["plan"]
    assert C.plan_minutes(plan_json) == 10, "plan.json holds a copy of the steps per rerun: what a hard stop sums"
    assert plan_json["steps"][0] == {**cfg["plans"]["la"]["steps"][0]}, "the plan's own run, as written"
    assert [(s["expect"], s["artifacts"], s["lookahead"]) for s in plan_json["steps"][1:]] == \
        [({"exit_code": 0}, [], k) for k in (1, 2, 3, 4)], "a rerun is checked for running only, and keeps no artifacts"
    from agent_testbench.backends.docker_backend import backend, plan_limit
    assert plan_limit(plan_json) == 10 + backend.startup_minutes
    cfg["budget"]["max_run_usd"] = 0.5
    ok, message, _ = budget.check_launch(cfg, "la", approve=True)
    assert not ok and "plan la (its run and 4 look-ahead reruns) could cost up to" in message
    assert "counted 5 times: the plan's run and each look-ahead rerun, or fewer lookahead.cuts" in message
    r = cli(root, "plans")
    assert "la: docker, 1 step(s) x 5 runs (look-ahead: the plan's run and 4 reruns on data cut earlier), up to 10 " \
           "min" in r.stdout, r.stdout


def test_too_little_data_for_the_cuts_is_refused_at_launch(tmp_path):
    root = project(tmp_path, {"many": {**LOOK, "lookahead": {**LOOK["lookahead"], "cuts": 20}}})
    write_prices(root / "data" / "prices.csv", start=dt.date(2023, 9, 18))       # 10 days before the seal
    r = cli(root, "run", "many", "--foreground")
    assert r.returncode == 2 and not (root / ".testbench" / "runs").exists(), r.stdout + r.stderr
    assert ("`plans.many.lookahead`: data/prices.csv has 20 rows from 2023-09-18 to 2023-09-29 before the seal at "
            "2023-10-02; a look-ahead rerun keeps at least the first 20% of them") in r.stdout
    assert "which leaves 8 times to cut at, fewer than cuts: 20. Lower plans.many.lookahead.cuts to 8" in r.stdout


# ---- what testbench.yaml refuses -----------------------------------------------------------------------------------

@pytest.mark.parametrize("plans, data, message", [
    ({"p": {**LOOK, "holdout": True}}, True, "lookahead: runs the steps 5 times, and this plan opens the holdout "
                                             "(holdout:), so it would open the sealed rows 5 times"),
    ({"p": {**LOOK, "walk_forward": {"train": "6mo", "test": "3mo"}}}, True,
     "not both: every window would run 5 times"),
    ({"p": {**LOOK, "lookahead": {"output": "out/s.csv", "cuts": 0}}}, True, "a whole number from 1 to 20"),
    ({"p": {**LOOK, "lookahead": {"output": "out/s.csv", "cuts": 21}}}, True, "a whole number from 1 to 20"),
    ({"p": {**LOOK, "lookahead": {"output": "out/s.csv", "cuts": True}}}, True, "a whole number from 1 to 20"),
    ({"p": {**LOOK, "lookahead": {"output": "out/s.csv", "cuts": "4"}}}, True, "a whole number from 1 to 20"),
    ({"p": {**LOOK, "lookahead": {"cuts": 4}}}, True, "name the file the steps write the strategy's decisions to"),
    ({"p": {**LOOK, "lookahead": {"output": "/tmp/s.csv"}}}, True, "name the file the steps write"),
    ({"p": {**LOOK, "lookahead": {"output": "../s.csv"}}}, True, "name the file the steps write"),
    ({"p": {**LOOK, "lookahead": {"output": "s.csv", "cut": 3}}}, True, "has no 'cut' (did you mean cuts?)"),
    ({"p": {**LOOK, "lookahead": {"output": "s.csv", "data": "price"}}}, True,
     "names the data to cut: one of prices; there is no 'price' (did you mean prices?)"),
    ({"p": {**LOOK, "lookahead": "out/s.csv"}}, True, "is {data, output, cuts}"),
    ({"p": LOOK}, False, "cuts a data file by its time column; name the data first"),
    ({"p": {**LOOK, "workdir": "inplace"}}, True, "this plan's steps see the data of each look-ahead rerun cut"),
    ({"p": {**LOOK, "lookahead": None, "model_cutoff": "soon"}}, True, "write an ISO 8601 date or time"),
    ({"p": {**LOOK, "lookahead": None, "model_cutoff": {"require_after": True}}}, True,
     "`plans.p.model_cutoff.date` is the training cutoff of the model the strategy asks"),
    ({"p": {**LOOK, "lookahead": None, "model_cutoff": {"date": "2024-10-01", "require_after": "yes"}}}, True,
     "`plans.p.model_cutoff.require_after` is true"),
    ({"p": {**LOOK, "lookahead": None, "model_cutoff": {"date": "2024-10-01", "after": True}}}, True,
     "has no 'after'"),
    ({"p": {**LOOK, "lookahead": None, "model_cutoff": "2024-10-01"}}, False, "testbench knows only from `data:`"),
])
def test_what_testbench_yaml_refuses_says_how_to_fix_it(tmp_path, plans, data, message):
    raw = {"project": "x", "plans": plans}
    if data:
        raw["data"] = {"prices": {"path": "data/prices.csv", "holdout_from": HOLDOUT}}
    (tmp_path / "testbench.yaml").write_text(yaml.safe_dump(raw, sort_keys=False))
    with pytest.raises(C.ConfigError) as exc:
        C.load(tmp_path)
    assert message in str(exc.value)


def test_the_json_schema_covers_lookahead_and_model_cutoff():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(C.SCHEMA.read_text(encoding="utf-8"))
    plan_full = schema["$defs"]["plan_full"]["properties"]
    assert set(DS.LOOKAHEAD_KEYS) == set(plan_full["lookahead"]["properties"]), "lookahead's keys"
    assert set(DS.CUTOFF_KEYS) == set(plan_full["model_cutoff"]["anyOf"][1]["properties"]), "model_cutoff's keys"
    assert {"lookahead", "model_cutoff"} <= set(C.PLAN_DATA_KEYS)

    class Editor(yaml.SafeLoader):                         # YAML as an editor reads it: dates are text
        pass
    Editor.yaml_implicit_resolvers = {k: [(tag, r) for tag, r in v if tag != "tag:yaml.org,2002:timestamp"]
                                      for k, v in yaml.SafeLoader.yaml_implicit_resolvers.items()}
    validator = jsonschema.Draft202012Validator(schema)
    head = "data: {prices: data/prices.csv}\nplans:\n  p: {steps: [x], "
    good = ["lookahead: {data: prices, output: out/signals.csv, cuts: 4}", "lookahead: {output: o.csv}",
            "model_cutoff: 2024-10-01", "model_cutoff: {date: 2024-10-01, require_after: true}",
            "lookahead: {output: o.csv}, model_cutoff: 2024-10-01T00:00:00Z"]
    bad = ["lookahead: {cuts: 4}", "lookahead: {output: o.csv, cuts: 0}", "lookahead: {output: o.csv, cuts: 21}",
           "lookahead: {output: /o.csv}", "lookahead: {output: o.csv, every: 2}", "model_cutoff: soon",
           "model_cutoff: {require_after: true}", "model_cutoff: {date: 2024-10-01, require_after: 'yes'}"]
    for text in good + bad:
        errors = list(validator.iter_errors(yaml.load(head + text + "}\n", Loader=Editor)))
        assert bool(errors) == (text in bad), (text, [e.message for e in errors])


# ---- model_cutoff ------------------------------------------------------------------------------------------------

def cutoff_project(tmp_path: Path, plans: dict) -> Path:
    """Weekdays from 2020 to September 2025, sealed from 2025-07-01 (the data seal tests' layout)."""
    root = tmp_path / "cutoff"
    root.mkdir()
    lines = ["time,symbol,close"] + [f"{d.isoformat()},SPY,{100 + i * 0.1:.2f}"
                                    for i, d in enumerate(weekdays(dt.date(2020, 1, 1), dt.date(2025, 10, 1)))]
    (root / "data").mkdir()
    (root / "data" / "prices.csv").write_text("\n".join(lines) + "\n")
    step = [{"name": "backtest", "run": "python -c \"print('ok')\"", "max_minutes": 1}]
    (root / "testbench.yaml").write_text(yaml.safe_dump({
        "project": "cutoff", "data": {"prices": {"path": "data/prices.csv", "holdout_from": "2025-07-01"}},
        "plans": {name: {**p, "steps": step} for name, p in plans.items()}}, sort_keys=False))
    return root


WF = {"data": "prices", "train": "24mo", "test": "6mo"}
TAIL = "only windows after the cutoff, or a forward paper record, count as evidence"


def test_model_cutoff_warns_on_results_the_model_may_already_know(tmp_path):
    root = cutoff_project(tmp_path, {"before": {"model_cutoff": "2025-09-01"},
                                     "mixed": {"model_cutoff": "2023-01-01"},
                                     "after": {"model_cutoff": "2019-06-30"},
                                     "final": {"holdout": True, "model_cutoff": "2025-03-01"}})
    run_dir, rep = launch(root, "before")
    mc = rep["model_cutoff"]
    assert rep["status"] == "passed" and mc["level"] == "warning" and mc["ok"], "a warning, not a failure"
    assert mc["finding"] == ("results on data up to 2025-06-30 may use what the model already knew (model_cutoff "
                             f"2025-09-01); {TAIL}")
    assert f"**Warning**: {mc['finding']}." in (run_dir / "report.md").read_text()
    assert f"Warning: {mc['finding']}." in R.render_html(rep, run_dir)
    assert 'name="model cutoff"' in R.render_junit(rep) and "<failure" not in R.render_junit(rep)
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)

    _, rep = launch(root, "mixed")
    assert rep["model_cutoff"]["finding"] == ("results on data from 2020-01-01 to 2025-06-30 include days up to "
                                              "model_cutoff 2023-01-01, which the model may already know; " + TAIL)
    _, rep = launch(root, "after")
    assert rep["model_cutoff"]["level"] == "ok" and rep["model_cutoff"]["finding"] == (
        "model cutoff 2019-06-30: the results here are on data from 2020-01-01 on, after it, which the model cannot "
        "have been trained on")
    _, rep = launch(root, "final")
    assert rep["model_cutoff"]["level"] == "ok" and rep["model_cutoff"]["periods"] == [
        {"what": "the holdout", "start": "2025-07-01", "end": "2025-09-30", "after_cutoff": True}], \
        "the holdout plan's test period is the sealed rows"


def test_model_cutoff_marks_the_walk_forward_windows_that_count(tmp_path):
    root = cutoff_project(tmp_path, {"wf": {"walk_forward": WF, "model_cutoff": "2024-03-15"},
                                     "on": {"walk_forward": WF, "model_cutoff": "2024-07-01"}})
    run_dir, rep = launch(root, "wf")
    assert rep["status"] == "passed" and [w["after_cutoff"] for w in rep["windows"]] == [False] * 5 + [True] * 2
    assert rep["model_cutoff"]["finding"] == (
        "results on windows 1/7 to 5/7 (test periods from 2022-01-01 to 2024-07-01) may use what the model already "
        "knew (model_cutoff 2024-03-15); only windows after the cutoff (windows 6/7 to 7/7 here), or a forward paper "
        "record, count as evidence")
    assert ("Windows that test after model_cutoff 2024-03-15, the ones that count as evidence: 6/7, 7/7."
            in (run_dir / "report.md").read_text())
    _, rep = launch(root, "on")
    assert [w["after_cutoff"] for w in rep["windows"]] == [False] * 6 + [True], \
        "a window that starts on the cutoff day does not count: the model may know that day"
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)


def test_require_after_fails_a_run_with_no_test_period_after_the_cutoff(tmp_path):
    strict = lambda date: {"date": date, "require_after": True}                  # noqa: E731
    root = cutoff_project(tmp_path, {"strict": {"model_cutoff": strict("2025-09-01")},
                                     "wf": {"walk_forward": WF, "model_cutoff": strict("2025-06-01")},
                                     "some": {"walk_forward": WF, "model_cutoff": strict("2024-12-01")}})
    r = cli(root, "run", "strict", "--foreground")
    rep = json.loads((runs.all_runs(root)[-1] / "report.json").read_text())
    assert r.returncode == 1 and rep["status"] == "failed" and rep["model_cutoff"]["level"] == "failed"
    assert all(s["status"] == "passed" for s in rep["steps"]), "every step passed: the cutoff fails it"
    assert rep["model_cutoff"]["finding"] == (
        "results on data up to 2025-06-30 may use what the model already knew (model_cutoff 2025-09-01); " + TAIL
        + " - and model_cutoff.require_after is true, so with no test period here starting after the cutoff this run "
        "fails. Test on rows after it (newer data, or walk-forward windows that start after it), or keep a forward "
        "paper record")
    assert f"**FAIL** - {rep['model_cutoff']['finding']}." in r.stdout
    assert '<failure message="results on data up to 2025-06-30' in R.render_junit(rep)
    _, rep = launch(root, "wf")
    assert rep["status"] == "failed" and "(none here)" in rep["model_cutoff"]["finding"]
    _, rep = launch(root, "some")
    assert rep["status"] == "passed" and rep["model_cutoff"]["level"] == "warning", "a window after it counts"
