"""Exports: a finished run in MLflow, W&B, a csv or a jsonl file, with its checked numbers, params and evidence and
the way back to its proof; no duplicate when a run is exported again; a missing or unreachable tracker never fails
or holds up a run. csv and jsonl always run. MLflow and W&B run where those packages are installed (a separate venv,
see docs/export.md), against a local sqlite store and W&B's offline mode: nothing leaves this machine."""
import asyncio
import csv
import json
import os
import struct
import subprocess
import time
from pathlib import Path

import pytest
import yaml

from agent_testbench import config as C, export as X, provenance, report as R
from conftest import REPO, cli, write_config

SECRET = "sekrit-value-0123456789"


@pytest.fixture(autouse=True)
def nothing_leaves(monkeypatch):
    """The trackers' own usage telemetry and error reports stay off here too (export turns them off by default)."""
    for k, v in {"MLFLOW_DISABLE_TELEMETRY": "true", "DO_NOT_TRACK": "true", "MLFLOW_DISABLE_AGENT_HINT": "1",
                 "WANDB_ERROR_REPORTING": "false"}.items():
        monkeypatch.setenv(k, v)


def exporting(root: Path, export: dict, params: dict | None = None) -> Path:
    cfg = yaml.safe_load((root / "testbench.yaml").read_text())
    cfg["export"] = export
    if params:
        cfg["plans"]["quick"]["steps"][0]["params"] = params
    write_config(root, cfg)
    return root


def latest(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir())[-1]


def outcome(run_dir: Path, target: str, seconds: float = 180) -> str:
    """The first finished outcome export.log records for target (background exports take a moment)."""
    end = time.time() + seconds
    while time.time() < end:
        log = (run_dir / "export.log").read_text() if (run_dir / "export.log").exists() else ""
        hits = [ln for ln in log.splitlines() if X.LINE.match(ln) and f" {target}: " in ln and " started - " not in ln]
        if hits:
            return hits[0]
        time.sleep(0.5)
    raise AssertionError(f"no {target} outcome in export.log after {seconds} s:\n{log}")


def fake_run(root: Path, run_id: str = "20261101-120000-quick", status: str = "passed", n: float = 3.0) -> Path:
    """A finished run's folder as finalize leaves it, without running anything."""
    d = root / ".testbench" / "runs" / run_id
    (d / "steps" / "01-notebook").mkdir(parents=True)
    (d / "steps" / "01-notebook" / "log.txt").write_text("hello\n")
    (d / "plan.json").write_text(json.dumps({"backend": "local", "steps": [{"name": "notebook", "params": {"N": 3}}]}))
    rep = {"run_id": run_id, "plan": "quick", "backend": "local", "gpu": None, "status": status, "started": 1793500000.0,
           "finished": 1793500060.0, "minutes": 1.0, "cost_usd": 0.0, "testbench_version": "0", "schema_version": 1,
           "source": None, "steps": [{"name": "notebook", "kind": "notebook", "status": status, "dir": "steps/01-notebook",
                                      "minutes": 1.0, "expectations": [
                                          {"check": "out/result.json: n >= 3", "ok": n >= 3, "detail": f"{n} (wanted >= 3.0)"},
                                          {"check": "every cell ran without an error", "ok": True, "detail": "all cells ran"}]}]}
    rep["evidence"], rep["evidence_sha256"] = provenance.evidence(d)
    R.write(d, rep)
    (d / "state.json").write_text(json.dumps({"run_id": run_id, "plan": "quick", "backend": "local", "status": status}))
    return d


def test_metric_names_leave_out_the_rule():
    cases = {("train", "outputs/metrics.json: accuracy >= 0.9"): "train/outputs/metrics.json accuracy",
             ("nb <main>", "out/m.json: loss < 0.5"): "nb _main_/out/m.json loss",
             ("train", "stdout /val_loss=([0-9.eE+-]+)/ < 0.3"): "train/stdout val_loss",
             ("train", "logs/x.log /^([0-9.]+) acc/ >= 1"): "train/logs/x.log _0-9._ acc",
             ("gpu", "gpu busy_pct >= 50"): "gpu/gpu busy_pct",
             ("t", "out/a.json: acc ~= 0.93 +- 0.01"): "t/out/a.json acc",
             ("t", "out/a.json: acc between 0.8 and 0.95"): "t/out/a.json acc",
             ("t", "report.xml: failures 0"): "t/report.xml failures",
             ("t", "../x.json: losses.-1 == 3"): "t/_/x.json losses.-1",
             ("t", "out/plot.png looks like golden/plot.png"): "t/out/plot.png looks like golden/plot.png"}
    for (step, check), want in cases.items():
        assert X.metric_name(step, check) == want, (step, check)
    try:
        from mlflow.utils.validation import _validate_metric_name, _validate_param_name
    except ImportError:
        return
    for want in cases.values():                                  # names MLflow takes as they are
        _validate_metric_name(want)
        _validate_param_name(want)


@pytest.mark.parametrize("section, says", [
    ({"neptune": {}}, "use mlflow, wandb, csv, jsonl"),
    ({"wandb": {"api_key": "x"}}, "Set WANDB_API_KEY in the environment"),
    ({"mlflow": {"token": "x"}}, "MLFLOW_TRACKING_TOKEN"),
    ({"mlflow": {"tracking_uri": "https://me:pw@mlflow.example.com"}}, "holds credentials"),
    ({"wandb": {"mode": "cloud"}}, "online (to the W&B server), offline"),
    ({"wandb": {"project": "a/b"}}, "without /"),
    ({"csv": 3}, "or the file's path, e.g. `csv: .testbench/runs.csv`"),
    ({"mlflow": {"artifacts": "yes"}}, "is true (upload the report"),
    ({"mlflow": {"experiments": "x"}}, "use tracking_uri, experiment, artifacts"),
    ({"wandb": {"tags": "nightly"}}, "a list of words"),
    ("mlflow", "maps trackers to their settings"),
])
def test_the_export_section_says_what_to_write(tmp_path, section, says):
    with pytest.raises(C.ConfigError) as err:
        C.validate({"project": "p", "export": section, "plans": {"q": {"steps": [{"name": "s", "run": "true"}]}}}, tmp_path)
    assert says in str(err.value), str(err.value)


def test_the_export_section_short_forms():
    assert C.check_export({"csv": ".testbench/runs.csv", "jsonl": None, "mlflow": False, "wandb": {"mode": "offline"}}) == \
        {"csv": {"path": ".testbench/runs.csv"}, "jsonl": {}, "wandb": {"mode": "offline"}}
    assert C.check_export(None) == {}


def test_every_run_lands_in_csv_and_jsonl_once(plain):
    exporting(plain, {"csv": ".testbench/runs.csv", "jsonl": "results/runs.jsonl"}, params={"N": 3, "NAME": "bob"})
    assert cli(plain, "run", "quick", "--wait", "180").returncode == 0
    run = latest(plain)
    rows = list(csv.DictReader((plain / ".testbench" / "runs.csv").open()))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["run_id"] == run.name and row["status"] == "passed" and row["plan"] == "quick" and row["backend"] == "local"
    assert float(row["metrics.notebook/out/result.json n"]) == 3 and float(row["metrics.notebook/out/result.json squares.-1"]) == 4
    assert row["params.notebook/N"] == "3" and row["params.notebook/NAME"] == "bob" and row["checks_passed"] == row["checks"]
    assert row["report"] == f".testbench/runs/{run.name}/report.json" and (plain / row["report"]).is_file()
    assert len(row["evidence_sha256"]) == 64 and float(row["cost_usd"]) == 0
    line = json.loads((plain / "results" / "runs.jsonl").read_text())
    assert line["run_id"] == run.name and line["metrics"]["notebook/out/result.json n"] == 3 and line["params"]["notebook/NAME"] == "bob"

    again = cli(plain, "export", "--to", "csv")                           # exported again: the row is replaced
    assert again.returncode == 0 and f"{run.name} csv: row 1 of" in again.stdout, again.stdout + again.stderr
    assert len(list(csv.DictReader((plain / ".testbench" / "runs.csv").open()))) == 1
    assert cli(plain, "export", "latest", "--to", "jsonl", "--path", "results/runs.jsonl").returncode == 0
    assert len((plain / "results" / "runs.jsonl").read_text().splitlines()) == 1

    assert cli(plain, "run", "quick", "--wait", "180").returncode == 0      # a second run: a second row, in order
    rows = list(csv.DictReader((plain / ".testbench" / "runs.csv").open()))
    assert [r["run_id"] for r in rows] == sorted(p.name for p in (plain / ".testbench" / "runs").iterdir())
    r = cli(plain, "export", "all", "--to", "csv", "--path", "all.csv")
    assert r.returncode == 0 and len(list(csv.DictReader((plain / "all.csv").open()))) == 2
    report = cli(plain, "report").stdout
    assert "## Exports" in report and "- csv: row 2 of" in report and "- jsonl: line 2 of" in report


def test_a_failing_export_never_fails_or_holds_up_the_run(plain):
    (plain / "blocked").write_text("a file where the csv's folder would be")
    exporting(plain, {"csv": "blocked/runs.csv", "mlflow": {"tracking_uri": "http://127.0.0.1:9"}})
    r = cli(plain, "run", "quick", "--wait", "180", env={"MLFLOW_TRACKING_TOKEN": SECRET, "WANDB_API_KEY": SECRET})
    run = latest(plain)
    assert r.returncode == 0 and json.loads((run / "state.json").read_text())["status"] == "passed", r.stdout
    assert " csv: failed - " in outcome(run, "csv")
    assert " mlflow: failed - " in outcome(run, "mlflow")                # not installed, or nothing listens there
    report = cli(plain, "report").stdout
    assert "- csv: FAILED" in report and f"`testbench export {run.name} --to csv`" in report
    assert "- mlflow: FAILED" in report
    assert SECRET not in (run / "export.log").read_text() + report

    start = time.time()                                                   # the end of a run only starts the export
    X.after_run(plain, run)
    assert time.time() - start < 5


def test_a_missing_tracker_says_how_to_install_it(plain, tmp_path):
    fake_run(plain)
    stub = tmp_path / "stub"
    for name in ("mlflow", "wandb"):
        (stub / name).mkdir(parents=True)
        (stub / name / "__init__.py").write_text(f"raise ImportError('No module named {name}')\n")
    env = {"PYTHONPATH": os.pathsep.join([str(stub), str(REPO / "src")])}
    r = cli(plain, "export", "--to", "mlflow", "--tracking-uri", "sqlite:///x.db", env=env)
    assert r.returncode == 2 and "pip install 'agent-testbench[mlflow]'" in r.stdout, r.stdout + r.stderr
    r = cli(plain, "export", "--to", "wandb", "--mode", "offline", env=env)
    assert r.returncode == 2 and "pip install 'agent-testbench[wandb]'" in r.stdout
    r = cli(plain, "export")
    assert r.returncode == 2 and "Nothing to export to" in r.stdout


def test_without_an_export_section_a_run_is_left_alone(plain):
    d = fake_run(plain)
    X.after_run(plain, d)
    assert not (d / "export.log").exists() and "Exports" not in cli(plain, "report").stdout


def test_an_unfinished_run_is_not_exported(plain):
    d = fake_run(plain, status="passed")
    (d / "report.json").write_text(json.dumps({**json.loads((d / "report.json").read_text()), "status": "running"}))
    r = cli(plain, "export", "--to", "jsonl")
    assert r.returncode == 1 and "has not finished" in r.stdout and not (plain / ".testbench" / "runs.jsonl").exists()


def test_mcp_export_run(plain):
    from agent_testbench import mcp_server
    fake_run(plain)

    async def go():
        names = {t.name for t in await mcp_server.server.list_tools()}
        out = await mcp_server.server.call_tool("export_run", {"project_dir": str(plain), "to": "jsonl"})
        return names, out

    names, out = asyncio.run(go())
    text = json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
    assert "export_run" in names and "jsonl: line 1 of" in text and "[exit 0]" in text, text


# ---- MLflow, against a local sqlite store ---------------------------------------------------------------------

def git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def test_mlflow_gets_numbers_params_tags_and_evidence_once(plain):
    pytest.importorskip("mlflow")
    from mlflow.tracking import MlflowClient
    exporting(plain, {"mlflow": {"tracking_uri": "sqlite:///.testbench/mlflow.db", "experiment": "plain-exp"}},
              params={"N": 3, "NAME": "bob"})
    (plain / ".gitignore").write_text("out/\n")
    git(plain, "init", "-q")
    git(plain, "add", "-A")
    git(plain, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    assert cli(plain, "run", "quick", "--wait", "180").returncode == 0
    run = latest(plain)
    assert " mlflow: ok - MLflow run " in outcome(run, "mlflow")         # exported in the background

    client = MlflowClient(tracking_uri="sqlite:///" + (plain / ".testbench" / "mlflow.db").as_posix())
    eid = client.get_experiment_by_name("plain-exp").experiment_id
    found = client.search_runs([eid], filter_string=f"tags.`testbench.run_id` = '{run.name}'")
    assert len(found) == 1
    r = found[0]
    m, p, t = r.data.metrics, r.data.params, r.data.tags
    assert m["notebook/out/result.json n"] == 3 and m["notebook/out/result.json squares.-1"] == 4
    assert m["passed"] == 1 and m["cost_usd"] == 0 and m["checks_passed"] == 3
    assert p["notebook/N"] == "3" and p["notebook/NAME"] == "bob" and p["backend"] == "local"
    assert t["testbench.status"] == "passed" and t["testbench.plan"] == "quick" and t["mlflow.runName"] == run.name
    assert t["testbench.report"] == f".testbench/runs/{run.name}/report.json" and t["testbench.dirty"] == "False"
    assert t["mlflow.source.git.commit"] == git(plain, "rev-parse", "HEAD") and "testbench verify" in t["mlflow.note.content"]
    assert r.info.status == "FINISHED" and r.info.run_name == run.name
    top = {a.path for a in client.list_artifacts(r.info.run_id)}
    assert {"report.json", "report.md", "steps"} <= top
    logged = {a.path for a in client.list_artifacts(r.info.run_id, "steps/01-notebook")}
    assert "steps/01-notebook/log.txt" in logged and "steps/01-notebook/executed.ipynb" in logged
    got = json.loads(Path(client.download_artifacts(r.info.run_id, "report.json", str(plain / "dl"))).read_text())
    assert got["evidence_sha256"] == R.load(run)["evidence_sha256"]

    again = cli(plain, "export", "--to", "mlflow", env={"MLFLOW_TRACKING_TOKEN": SECRET})
    assert again.returncode == 0 and "(updated)" in again.stdout, again.stdout + again.stderr
    assert len(client.search_runs([eid], filter_string=f"tags.`testbench.run_id` = '{run.name}'")) == 1
    assert len(client.get_metric_history(r.info.run_id, "notebook/out/result.json n")) == 1
    assert SECRET not in again.stdout + again.stderr + (run / "export.log").read_text()

    failed = fake_run(plain, run_id="20261101-120000-quick", status="failed", n=2.0)
    assert cli(plain, "export", failed.name, "--to", "mlflow").returncode == 0
    f = client.search_runs([eid], filter_string=f"tags.`testbench.run_id` = '{failed.name}'")[0]
    assert f.info.status == "FAILED" and f.data.metrics["passed"] == 0 and f.data.metrics["notebook/out/result.json n"] == 2


# ---- W&B, offline: the run folder is read back, nothing is sent -----------------------------------------------

def wandb_records(path: Path) -> list:
    """The records of an offline run's .wandb file (a LevelDB-style log of protobuf Records)."""
    from wandb.proto import wandb_internal_pb2 as pb
    raw, pos, buf, out = path.read_bytes(), 7, b"", []
    while pos + 7 <= len(raw):
        if 32768 - pos % 32768 < 7:                             # a block's tail too short for a header is padding
            pos += 32768 - pos % 32768
            continue
        _, n, kind = struct.unpack("<IHB", raw[pos:pos + 7])
        data, pos = raw[pos + 7:pos + 7 + n], pos + 7 + n
        if kind == 0:
            break
        buf = data if kind in (1, 2) else buf + data
        if kind in (1, 4):
            rec = pb.Record()
            rec.ParseFromString(buf)
            out.append(rec)
    return out


@pytest.fixture
def offline(tmp_path, monkeypatch) -> dict:
    env = {"WANDB_MODE": "offline", "WANDB_BASE_URL": "http://127.0.0.1:9", "WANDB_API_KEY": SECRET,
           "WANDB_CONFIG_DIR": str(tmp_path / "wb" / "config"), "WANDB_CACHE_DIR": str(tmp_path / "wb" / "cache"),
           "WANDB_DATA_DIR": str(tmp_path / "wb" / "data"), "WANDB_SILENT": "true"}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return env


def test_wandb_offline_run_carries_the_record(plain, offline):
    pytest.importorskip("wandb")
    exporting(plain, {"wandb": {"project": "plain-wb", "mode": "offline", "tags": ["nightly"]}}, params={"N": 3, "NAME": "bob"})
    assert cli(plain, "run", "quick", "--wait", "180", env=offline).returncode == 0
    run = latest(plain)
    assert f" wandb: ok - W&B run {run.name} in project 'plain-wb', offline in " in outcome(run, "wandb")
    folders = sorted((plain / ".testbench" / "wandb").glob(f"offline-run-*-{run.name}"))
    assert len(folders) == 1
    raw = (folders[0] / f"run-{run.name}.wandb").read_bytes()
    recs = {}
    for rec in wandb_records(folders[0] / f"run-{run.name}.wandb"):
        recs.setdefault(rec.WhichOneof("record_type"), []).append(rec)
    r = recs["run"][0].run
    config = {u.key: json.loads(u.value_json) for c in [r.config] + [x.config for x in recs.get("config", [])]
              for u in c.update}
    assert r.run_id == run.name and r.project == "plain-wb" and r.display_name == run.name and r.run_group == "quick"
    assert set(r.tags) == {"nightly", "quick", "passed", "agent-testbench"} and "testbench verify" in r.notes
    assert config["notebook/N"] == 3 and config["notebook/NAME"] == "bob" and config["plan"] == "quick"
    assert config["report"] == f".testbench/runs/{run.name}/report.json"
    summary = {u.key: json.loads(u.value_json) for s in recs["summary"] for u in s.summary.update}
    assert summary["notebook/out/result.json n"] == 3 and summary["notebook/out/result.json squares.-1"] == 4
    assert summary["status"] == "passed" and summary["passed"] == 1
    art = recs["artifact"][0].artifact
    paths = {c.path for c in art.manifest.contents}
    assert art.name == f"testbench-{run.name}" and art.type == "testbench-evidence"
    assert {"report.json", "report.md", "steps/01-notebook/log.txt", "steps/01-notebook/executed.ipynb"} <= paths
    assert "report.json" in {f.path for rec in recs["files"] for f in rec.files.files}
    assert (folders[0] / "files" / "report.json").exists()
    assert recs["exit"][0].exit.exit_code == 0

    again = cli(plain, "export", "--to", "wandb", env=offline)          # the same W&B run id: one run once synced
    assert again.returncode == 0, again.stdout + again.stderr
    assert all(f.name.endswith(f"-{run.name}") for f in (plain / ".testbench" / "wandb").glob("offline-run-*"))
    for f in (plain / ".testbench" / "wandb").rglob("*"):
        if f.is_file() and f.suffix in (".wandb", ".log", ".json", ".yaml", ".txt"):
            assert SECRET.encode() not in f.read_bytes(), f
    assert SECRET not in again.stdout + again.stderr + (run / "export.log").read_text() and SECRET.encode() not in raw
