"""The data seal (dataseal.py, docs/trading-data.md): `data:` names the data the plans read; every plan but the one that
opens the holdout sees each sealed file cut before holdout_from, in a copy of the project; openings of the holdout are
counted, and max_opens refuses one past the limit at launch; a walk-forward plan runs its steps once per window with
only the rows before the window's test end; and every run records what its steps saw, which `testbench verify` and
`testbench diff` compare."""
import datetime as dt
import hashlib
import json
import tarfile
from pathlib import Path

import pytest
import yaml

from agent_testbench import budget, config as C, dataseal as DS, executor, runs
from conftest import cli

HOLDOUT = "2025-07-01"
SEEN = '''\
import csv, json, os, sys
p = os.environ["TESTBENCH_DATA_PRICES"]
rows = list(csv.DictReader(open(p)))
env = {k: os.environ.get(k) for k in ("TESTBENCH_WINDOW", "TESTBENCH_TRAIN_START", "TESTBENCH_TRAIN_END",
                                      "TESTBENCH_TEST_START", "TESTBENCH_TEST_END")}
seen = {"path": p, "rows": len(rows), "first": min(r["time"] for r in rows), "last": max(r["time"] for r in rows),
        "symbols": sorted({r["symbol"] for r in rows}), **env}
json.dump(seen, open("seen.json", "w"))
print(json.dumps(seen))
fail_from = os.environ.get("FAIL_FROM")
sys.exit(1 if fail_from and env["TESTBENCH_TEST_START"] and env["TESTBENCH_TEST_START"] >= fail_from else 0)
'''


def weekdays(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d < end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def write_prices(path: Path, start=dt.date(2020, 1, 1), end=dt.date(2025, 10, 1), symbol="SPY") -> list[str]:
    lines = ["time,symbol,close"] + [f"{d.isoformat()},{symbol},{100 + i * 0.1:.2f}"
                                    for i, d in enumerate(weekdays(start, end))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return lines


def project(tmp_path: Path, plans: dict, data=None, **more) -> Path:
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    write_prices(root / "data" / "prices.csv")
    (root / "seen.py").write_text(SEEN)
    data = {"prices": {"path": "data/prices.csv", "holdout_from": HOLDOUT}} if data is None else data
    (root / "testbench.yaml").write_text(yaml.safe_dump({"project": "seal", "data": data, "plans": plans, **more},
                                                        sort_keys=False))
    return root


STEP = {"name": "seen", "run": "python seen.py", "max_minutes": 2, "artifacts": ["seen.json"]}


def launch(root: Path, plan: str) -> tuple[Path, dict]:
    run_dir = runs.launch(root, C.load(root), plan, foreground=True)
    return run_dir, json.loads((run_dir / "report.json").read_text())


def seen(run_dir: Path, step: int = 1) -> dict:
    return json.loads(next((run_dir / "steps").glob(f"{step:02d}-*/artifacts/seen.json")).read_text())


def sha(lines: list[str]) -> str:
    return hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()


# ---- the seal ----------------------------------------------------------------------------------------------------

def test_a_plan_sees_only_the_rows_before_the_seal_in_a_copy_and_the_holdout_plan_sees_all(tmp_path):
    root = project(tmp_path, {"dev": {"steps": [STEP]}, "final": {"holdout": True, "steps": [STEP]}})
    lines = (root / "data" / "prices.csv").read_text().splitlines()
    before = [ln for ln in lines[1:] if ln < HOLDOUT]
    assert C.load(root)["plans"]["dev"]["workdir"] == "copy", "a plan that sees cut data runs in a copy"

    run_dir, rep = launch(root, "dev")
    got = seen(run_dir)
    assert rep["status"] == "passed" and got["last"] == "2025-06-30" and got["rows"] == len(before)
    assert Path(got["path"]).is_relative_to(run_dir / "work"), "TESTBENCH_DATA_PRICES points at the run's copy"
    assert (root / "data" / "prices.csv").read_text().splitlines() == lines, "the project's own file is untouched"

    d = rep["data"]["prices"]                                 # what the step saw, and the file it was cut from
    assert d["view"] == "sealed" and d["holdout_from"] == HOLDOUT and d["time"] == "time"
    assert (d["rows"], d["first"], d["last"]) == (len(before), "2020-01-01", "2025-06-30")
    assert d["sha256"] == sha([lines[0], *before]) == hashlib.sha256(Path(got["path"]).read_bytes()).hexdigest()
    assert d["file"]["sha256"] == hashlib.sha256((root / "data" / "prices.csv").read_bytes()).hexdigest()
    assert d["file"]["rows"] == len(lines) - 1 and d["sealed"]["rows"] == len(lines) - 1 - len(before)
    assert d["sealed"]["sha256"] == sha([ln for ln in lines[1:] if ln >= HOLDOUT]) and d["sealed"]["first"] == HOLDOUT
    step_data = json.loads(next((run_dir / "steps").glob("01-*/data.json")).read_text())
    assert step_data["prices"]["sha256"] == d["sha256"], "each step folder holds what its data was, as evidence"
    md = (run_dir / "report.md").read_text()
    assert "## Data" in md and f"the rows before {HOLDOUT} (the rest is sealed): {len(before)} rows" in md
    assert "Holdout" not in md

    run_dir, rep = launch(root, "final")
    got = seen(run_dir)
    assert got["last"] == "2025-09-30" and got["rows"] == len(lines) - 1
    assert rep["data"]["prices"]["view"] == "full" and rep["data"]["prices"]["sha256"] == d["file"]["sha256"]
    assert C.load(root)["plans"]["final"]["workdir"] == "inplace", "nothing is cut for the holdout plan"
    pytest.importorskip("jsonschema").validate(rep, json.loads((Path(C.__file__).parent / "schema" /
                                                                "report-v1.schema.json").read_text()))


def test_shorthand_plans_and_a_folder_of_csv_files_are_sealed_too(tmp_path):
    root = project(tmp_path, {"quick": "python seen.py"},
                   data={"prices": {"path": "data/prices", "holdout_from": HOLDOUT}})
    for symbol in ("SPY", "QQQ"):
        write_prices(root / "data" / "prices" / f"{symbol}.csv", symbol=symbol)
    (root / "seen.py").write_text(SEEN.replace('rows = list(csv.DictReader(open(p)))',
                                               'rows = [r for f in sorted(os.listdir(p)) '
                                               'for r in csv.DictReader(open(os.path.join(p, f)))]'))
    run_dir, rep = launch(root, "quick")
    out = json.loads(next((run_dir / "steps").glob("01-*/log.txt")).read_text().strip().splitlines()[-1])
    assert rep["status"] == "passed" and out["last"] == "2025-06-30" and out["symbols"] == ["QQQ", "SPY"]
    d = rep["data"]["prices"]
    assert d["files"] == 2 and d["view"] == "sealed" and d["file"]["files"] == 2 and d["sealed"]["rows"] > 0

    (root / "data" / "prices" / "notes.txt").write_text("sealed rows could hide here\n")
    r = cli(root, "run", "quick", "--foreground")
    assert r.returncode == 2 and "notes.txt is not a .csv file" in r.stdout and "move it out" in r.stdout


def test_what_testbench_yaml_refuses_says_how_to_fix_it(tmp_path):
    def refused(plans: dict, data=None) -> str:
        root = tmp_path / f"p{len(list(tmp_path.iterdir()))}"
        root.mkdir()
        (root / "testbench.yaml").write_text(yaml.safe_dump({"data": data or {"prices": {
            "path": "data/prices.csv", "holdout_from": HOLDOUT}}, "plans": plans}))
        with pytest.raises(C.ConfigError) as e:
            C.load(root)
        return str(e.value)

    assert "remove `workdir: inplace`" in refused({"p": {"workdir": "inplace", "steps": ["x"]}})
    assert "mo is months" in refused({"p": {"walk_forward": {"train": "2m", "test": "1mo"}, "steps": ["x"]}})
    assert "make them two plans" in refused({"p": {"holdout": True, "walk_forward": {"train": "1y", "test": "1mo"},
                                                   "steps": ["x"]}})
    assert "plan a already opens the holdout" in refused({"a": {"holdout": True, "steps": ["x"]},
                                                          "b": {"holdout": True, "steps": ["x"]}})
    assert "no data is sealed" in refused({"p": {"holdout": True, "steps": ["x"]}}, data={"prices": "data/p.csv"})
    assert "one of fills, prices" in refused({"p": {"walk_forward": {"train": "1y", "test": "1mo"}, "steps": ["x"]}},
                                             data={"prices": "p.csv", "fills": "f.csv"})
    assert "write the data's path in the project, relative to" in refused({}, data={"prices": "../p.csv"})
    root = tmp_path / "excluded"
    root.mkdir()
    (root / "testbench.yaml").write_text("exclude: [data]\ndata: {prices: data/p.csv}\nplans: {p: x}\n")
    with pytest.raises(C.ConfigError, match="left out of copies of the project by `exclude:`"):
        C.load(root)


def test_a_row_whose_time_cannot_be_read_refuses_the_launch(tmp_path):
    root = project(tmp_path, {"dev": {"steps": [STEP]}})
    with (root / "data" / "prices.csv").open("a") as f:
        f.write("next tuesday,SPY,1.0\n")
    r = cli(root, "run", "dev", "--foreground")
    assert r.returncode == 2
    assert "data/prices.csv line 1502, column `time`: 'next tuesday' is not an ISO 8601" in r.stdout
    assert "a row it cannot place could be a sealed one" in r.stdout and not (root / ".testbench" / "runs").exists()
    (tmp_path / "q.csv").write_text('time,note\n2024-01-02,"a stray quote\n2025-08-01,a sealed row"\n')
    with pytest.raises(DS.DataError, match="line 2 opens a quoted value it does not close"):
        DS.scan(tmp_path, "notes", {"path": "q.csv", "time": None, "holdout_from": HOLDOUT})


def test_the_executor_never_cuts_the_project_folder_itself(tmp_path):
    root = project(tmp_path, {"dev": {"steps": [STEP]}})
    before = (root / "data" / "prices.csv").read_bytes()
    plan = {**C.load(root)["plans"]["dev"], "workdir": "inplace",
            "data": {"prices": {"path": "data/prices.csv", "time": None, "cut": HOLDOUT}}}
    rep = executor.execute_plan(plan, project_dir=root, run_dir=tmp_path / "run")
    assert rep["status"] == "error" and "the seal cuts files only in a copy of the project" in rep["stopped_because"]
    assert rep["steps"] == [] and (root / "data" / "prices.csv").read_bytes() == before


def test_the_docker_bundle_carries_the_cut_file_not_the_sealed_rows(tmp_path):
    from agent_testbench.backends.docker_backend import PROJECT, DockerSession, bundle
    root = project(tmp_path, {"dev": {"steps": [STEP]}, "final": {"holdout": True, "steps": [STEP]}})
    cfg = C.load(root)
    plan = DS.prepare(root, cfg, "dev")["plan"]
    with DS.cut_copies(root, plan) as sealed:
        assert set(sealed) == {"data/prices.csv"}
        with bundle(root, cfg["exclude"], pkg=False, replace=sealed) as archive, tarfile.open(fileobj=archive) as t:
            text = t.extractfile(f"{PROJECT[1:]}/data/prices.csv").read().decode()
            assert t.extractfile(f"{PROJECT[1:]}/seen.py").read().decode() == SEEN
    assert text.splitlines()[-1].startswith("2025-06-30") and HOLDOUT not in text and "2025-09" not in text
    with DockerSession(root, cfg, tmp_path, {})._sealed() as sealed:      # a live session's copy is sealed too
        assert Path(sealed["data/prices.csv"]).read_text().splitlines()[-1].startswith("2025-06-30")
    with DS.cut_copies(root, DS.prepare(root, cfg, "final")["plan"]) as sealed:
        assert sealed == {}, "the holdout plan's container gets the whole file"


def test_zoned_times_and_a_cut_without_a_zone_compare_as_written(tmp_path):
    f = tmp_path / "p.csv"
    f.write_text("date,close\n2025-06-30T23:00:00-04:00,1\n2025-07-01T01:00:00+02:00,2\n2025-06-30T20:00:00Z,3\n")
    found = DS.scan(tmp_path, "prices", {"path": "p.csv", "time": None, "holdout_from": HOLDOUT})
    assert (found["kept"]["rows"], found["sealed"]["rows"], found["column"]) == (2, 1, "date")
    assert found["sealed"]["first"].isoformat() == "2025-07-01T01:00:00+02:00", "July 1st on its own clock"
    cut = DS.scan(tmp_path, "prices", {"path": "p.csv", "time": "date", "holdout_from": "2025-07-01T00:00:00Z"})
    assert cut["sealed"]["rows"] == 1 and cut["sealed"]["first"].isoformat() == "2025-06-30T23:00:00-04:00", \
        "with a zone: instants (03:00 UTC on July 1st)"


# ---- the holdout's openings ----------------------------------------------------------------------------------------

def test_openings_are_counted_and_every_holdout_report_says_how_many(tmp_path):
    root = project(tmp_path, {"dev": {"steps": [STEP]}, "final": {"holdout": True, "steps": [STEP]}})
    launch(root, "dev")
    reports = [launch(root, "final")[1] for _ in range(3)]
    assert [r["holdout"]["opened_before"] for r in reports] == [0, 1, 2]
    first_run = reports[0]["run_id"]
    assert reports[2]["holdout"]["first"]["run"] == first_run and reports[2]["holdout"]["roles"] == {"prices": HOLDOUT}
    md = (runs.runs_dir(root) / reports[2]["run_id"] / "report.md").read_text()
    assert (f"**Warning**: the holdout (prices from {HOLDOUT}) was opened 2 times before this run: results on it are "
            f"no longer untouched (first opened in run `{first_run}`).") in md
    first = (runs.runs_dir(root) / first_run / "report.md").read_text()
    assert "this run is the first to open it, so results on it are untouched" in first
    log = [json.loads(x) for x in (root / ".testbench" / "holdout.jsonl").read_text().splitlines()]
    assert [e["run"] for e in log] == [r["run_id"] for r in reports]
    assert log[0]["data"]["prices"]["last"] == "2025-09-30" and log[0]["data"]["prices"]["sealed_rows"] == 66
    assert "Holdout" not in (runs.all_runs(root)[0] / "report.md").read_text(), "dev runs open nothing"


def test_max_opens_refuses_the_second_opening_at_launch_with_nothing_started_or_spent(tmp_path):
    root = project(tmp_path, {"final": {"holdout": {"max_opens": 1}, "steps": [STEP]}})
    assert cli(root, "run", "final", "--foreground").returncode == 0
    ledger = (root / ".testbench" / "ledger.jsonl").read_text()
    opened = (root / ".testbench" / "holdout.jsonl").read_text()
    r = cli(root, "run", "final", "--foreground")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Refused: plan final opens the holdout (the sealed rows: prices from 2025-07-01), which was opened 1 time " \
           "already" in r.stdout and "max_opens is 1" in r.stdout and "Nothing ran and nothing was spent" in r.stdout
    assert "move holdout_from past it" in r.stdout and "have a person raise holdout.max_opens" in r.stdout
    assert len(runs.all_runs(root)) == 1, "no run was started"
    assert (root / ".testbench" / "ledger.jsonl").read_text() == ledger, "no launch in the ledger"
    assert (root / ".testbench" / "holdout.jsonl").read_text() == opened

    cfg = yaml.safe_load((root / "testbench.yaml").read_text())   # moving the seal over rows already opened: still
    cfg["data"]["prices"]["holdout_from"] = "2025-08-01"         # refused (the opened file reached past it)
    (root / "testbench.yaml").write_text(yaml.safe_dump(cfg))
    assert cli(root, "run", "final", "--foreground").returncode == 2
    write_prices(root / "data" / "prices.csv", end=dt.date(2026, 1, 1))     # new rows no opening has seen, sealed
    cfg["data"]["prices"]["holdout_from"] = "2025-10-01"
    (root / "testbench.yaml").write_text(yaml.safe_dump(cfg))
    r = cli(root, "run", "final", "--foreground")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads((runs.all_runs(root)[-1] / "report.json").read_text())["holdout"]["opened_before"] == 0


# ---- walk-forward ------------------------------------------------------------------------------------------------

WF = {"walk_forward": {"data": "prices", "train": "24mo", "test": "6mo", "step": "6mo"}, "steps": [
    {**STEP, "expect": {"json": {"seen.json": {"rows": ">= 100"}}}}]}


def test_walk_forward_windows_have_the_right_dates_rows_and_variables_and_no_future(tmp_path):
    root = project(tmp_path, {"wf": WF, "anchored": {**WF, "walk_forward": {**WF["walk_forward"], "anchored": True}}})
    run_dir, rep = launch(root, "wf")
    assert rep["status"] == "passed" and len(rep["windows"]) == 7 and len(rep["steps"]) == 7
    days = weekdays(dt.date(2020, 1, 1), dt.date(2025, 10, 1))
    for k, w in enumerate(rep["windows"], 1):
        start = dt.date(2020 + (k - 1) // 2, 1 + 6 * ((k - 1) % 2), 1)
        train_end = start.replace(year=start.year + 2)
        test_end = train_end.replace(year=train_end.year + (train_end.month == 7), month=7 if train_end.month == 1
                                     else 1)
        assert (w["train_start"], w["train_end"], w["test_start"], w["test_end"]) == (
            start.isoformat(), train_end.isoformat(), train_end.isoformat(), test_end.isoformat())
        assert w["train_rows"] == sum(start <= d < train_end for d in days)
        assert w["test_rows"] == sum(train_end <= d < test_end for d in days)
        assert w["status"] == "passed" and w["window"] == f"{k}/7" and w["steps"] == [f"seen (window {k}/7)"]
        got = seen(run_dir, k)
        assert got["TESTBENCH_WINDOW"] == f"{k}/7" and got["TESTBENCH_TRAIN_START"] == w["train_start"]
        assert (got["TESTBENCH_TRAIN_END"], got["TESTBENCH_TEST_START"], got["TESTBENCH_TEST_END"]) == (
            w["train_end"], w["test_start"], w["test_end"])
        rows = [d for d in days if d < test_end]
        assert (got["rows"], got["last"]) == (len(rows), rows[-1].isoformat()), "the file ends before the test end"
        assert w["data"]["prices"]["rows"] == len(rows)
    assert rep["windows"][-1]["test_end"] == HOLDOUT, "windows stay within the rows before the seal"
    assert rep["data"]["prices"]["view"] == "windows"
    md = (run_dir / "report.md").read_text()
    assert "## Walk-forward: 7 windows, 7 passed" in md and "| PASS | 1/7 | 2020-01-01 to 2022-01-01 | 2022-01-01 to " \
                                                            "2022-07-01 |" in md
    _, rep = launch(root, "anchored")
    assert {w["train_start"] for w in rep["windows"]} == {"2020-01-01"} and rep["windows"][-1]["train_end"] == \
        "2025-01-01"


def test_a_failing_window_fails_the_plan(tmp_path):
    root = project(tmp_path, {"wf": {**WF, "steps": [{**WF["steps"][0], "env": {"FAIL_FROM": "2023-07-01"}}]},
                              "every": {**WF, "steps": [{**WF["steps"][0], "env": {"FAIL_FROM": "2023-07-01"},
                                                         "continue_on_failure": True}]}})
    run_dir, rep = launch(root, "wf")
    assert rep["status"] == "failed" and [w["status"] for w in rep["windows"]] == ["passed"] * 3 + ["failed"] + \
        ["skipped"] * 3
    assert rep["skipped"] == [f"seen (window {k}/7)" for k in (5, 6, 7)]
    assert "## Walk-forward: 7 windows, 3 passed" in (run_dir / "report.md").read_text()
    _, rep = launch(root, "every")
    assert rep["status"] == "failed" and [w["status"] for w in rep["windows"]] == ["passed"] * 3 + ["failed"] * 4


def test_the_worst_case_and_the_hard_stop_count_every_window(tmp_path):
    root = project(tmp_path, {"wf": {**WF, "backend": "docker"}, "once": {**WF, "backend": "docker",
                                                                          "walk_forward": None}},
                   docker={"usd_per_hour": 6.0})
    cfg = C.load(root)
    once, wf = budget.worst_case(cfg, "once"), budget.worst_case(cfg, "wf")
    assert wf["windows"] == 7 and wf["max_minutes"] == 7 * once["max_minutes"] == 14
    assert wf["worst_case_usd"] > once["worst_case_usd"] and "(7 walk-forward windows)" in wf["what"]
    plan = DS.prepare(root, cfg, "wf")["plan"]
    assert C.plan_minutes(plan) == 14, "plan.json's steps: what a backend's hard stop sums"
    from agent_testbench.backends.docker_backend import backend, plan_limit
    assert plan_limit(plan) == 14 + backend.startup_minutes
    cfg["budget"]["max_run_usd"] = 1.0
    ok, message, _ = budget.check_launch(cfg, "wf", approve=True)
    assert not ok and "plan wf (7 walk-forward windows) could cost up to" in message and "fewer windows" in message


def test_a_walk_forward_whose_windows_do_not_fit_is_refused_at_launch(tmp_path):
    root = project(tmp_path, {"wf": {**WF, "walk_forward": {"train": "5y", "test": "1y"}}})
    r = cli(root, "run", "wf", "--foreground")
    assert r.returncode == 2 and not (root / ".testbench" / "runs").exists()
    assert ("`plans.wf.walk_forward`: data/prices.csv has rows from 2020-01-01 to 2025-06-30 before the seal at "
            "2025-07-01, and train 5y then test 1y, stepping 1y from 2020-01-01, fits 0 windows") in r.stdout
    assert "needs at least 2. Shorten train or test (e.g. train: 2y)" in r.stdout
    r = cli(root, "plans")
    assert r.returncode == 0 and "wf: local, 1 step(s), worst case not known: `plans.wf.walk_forward`" in r.stdout
    root = project(tmp_path / "many", {"wf": {**WF, "walk_forward": {"train": "1w", "test": "1d"}}})
    with pytest.raises(C.ConfigError, match="more than 200 windows"):
        budget.worst_case(C.load(root), "wf")


# ---- verify and diff ---------------------------------------------------------------------------------------------

def test_verify_says_when_the_data_changed_since_the_run_and_diff_shows_it(tmp_path):
    root = project(tmp_path, {"dev": {"steps": [STEP]}, "final": {"holdout": True, "steps": [STEP]}})
    run_dir, rep = launch(root, "dev")
    r = cli(root, "verify")
    assert r.returncode == 0 and "data/prices.csv (prices) is the data the run read" in r.stdout, r.stdout + r.stderr

    lines = write_prices(root / "data" / "prices.csv", end=dt.date(2025, 12, 1))   # rows added after the seal
    r = cli(root, "verify", run_dir.name)
    assert r.returncode == 1 and ("data/prices.csv changed since this run: the run's numbers are about other data "
                                  f"(sha256 {rep['data']['prices']['file']['sha256'][:16]} then") in r.stdout

    launch(root, "dev")
    r = cli(root, "diff")
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("- prices (`data/prices.csv`): the file changed between the runs (sha256 "
            f"{rep['data']['prices']['file']['sha256'][:12]} -> ") in r.stdout
    assert f"-> {len(lines) - 1} rows" in r.stdout
    d = json.loads(cli(root, "diff", "--json").stdout)
    assert d["data"]["changed"] and d["data"]["roles"][0]["change"] == "file changed"
    assert d["data"]["roles"][0]["a"]["sha256"] == d["data"]["roles"][0]["b"]["sha256"], "the same rows before it"

    launch(root, "final")
    r = cli(root, "diff", "last~1", "last")
    assert "the same file, but the steps saw other rows: the rows before the seal in A" in r.stdout
    assert "the whole file in B" in r.stdout

    (root / "data" / "prices.csv").unlink()
    r = cli(root, "verify")
    assert r.returncode == 0 and "data/prices.csv (prices) is not here any more, so it cannot be compared" in r.stdout
    data_json = next((runs.all_runs(root)[-1] / "steps").glob("01-*/data.json"))
    data_json.write_text(data_json.read_text().replace('"rows"', '"rowz"'))
    r = cli(root, "verify")
    assert r.returncode == 1 and "changed: steps/01-" in r.stdout, "what the step saw is evidence"


def test_a_project_without_data_is_as_before(tmp_path):
    (tmp_path / "testbench.yaml").write_text("plans:\n  tests: python -c pass\n")
    cfg = C.load(tmp_path)
    assert cfg["data"] == {} and cfg["plans"]["tests"]["workdir"] == "inplace"
    assert "holdout" not in cfg["plans"]["tests"] and "walk_forward" not in cfg["plans"]["tests"]
    run_dir, rep = launch(tmp_path, "tests")
    assert rep["status"] == "passed" and "data" not in rep and "holdout" not in rep and "windows" not in rep
    assert not list((run_dir / "steps").glob("*/data.json"))
