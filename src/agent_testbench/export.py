"""Exports: a finished run in the experiment tracker people already use (MLflow, Weights & Biases) or in a table
(csv, jsonl), with the way back to its proof.

    testbench export [RUN|latest|all] [--to mlflow|wandb|csv|jsonl]
    export: {mlflow: {tracking_uri: ..., experiment: ...}, wandb: {project: ..., mode: offline}, csv: runs.csv}

A run becomes one record: plan, status, backend and GPU, cost, minutes, commit and dirty flag, the steps' params,
every number its checks found (the numbers `testbench history` shows, named step/check without the rule), the run
id and the report's path; trackers also get the report and the evidence files. Exporting a run again updates it:
csv and jsonl replace its row, MLflow finds it by tag, W&B uses the run id as its own. After every run
(runs.finalize -> after_run) csv and jsonl are written at once and trackers in a background process, so a slow or
unreachable tracker never holds a run up; every outcome goes to the run's export.log, which `testbench report`
shows. Secrets (WANDB_API_KEY, MLFLOW_TRACKING_TOKEN, ...) stay in the environment, where the trackers read them;
nothing here prints or writes them.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import os
import re
import sys
import threading
from pathlib import Path

import yaml

from . import config as C, procs, report as R

TARGETS = ("mlflow", "wandb", "csv", "jsonl")
LOCAL = ("csv", "jsonl")                              # written at the end of a run; trackers go to the background
FIELDS = ("run_id", "project", "plan", "status", "backend", "gpu", "started", "finished", "minutes", "cost_usd",
          "worst_case_usd", "commit", "branch", "dirty", "steps", "steps_passed", "checks", "checks_passed",
          "evidence_files", "evidence_sha256", "stopped_because", "testbench_version", "python", "report")
PACKAGES = {"mlflow": "MLflow", "wandb": "Weights & Biases (wandb)"}
QUIET = {"mlflow": {"MLFLOW_HTTP_REQUEST_MAX_RETRIES": "2", "MLFLOW_HTTP_REQUEST_TIMEOUT": "30",   # down: fails fast
                    "MLFLOW_DISABLE_TELEMETRY": "true", "MLFLOW_DISABLE_AGENT_HINT": "1", "MLFLOW_LOGGING_LEVEL": "WARNING"},
         "wandb": {"WANDB_SILENT": "true", "WANDB_ERROR_REPORTING": "false"}}
BACKGROUND_LIMIT = 900                                # seconds a background export may take before it gives up
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+): (ok|failed|started) - (.*)$")
RULE_AT_END = re.compile(r"\s+(?:(?:>=|<=|!=|==|>|<)\s*\S+|~=\s*\S+\s*(?:\+-|\+/-|±)\s*\S+|between\s+\S+\s+and\s+\S+"
                         r"|[-+]?\d[\d.eE+-]*)$", re.I)


class ExportError(Exception):
    code = 1


class Missing(ExportError):
    code = 2


# ---- one record per run --------------------------------------------------------------------------------------

def name(*parts: str) -> str:
    """A metric or param name every tracker takes (MLflow's rules are the strictest): letters, digits, _ - . space
    and /, no empty, . or .. segments, at most 250 characters."""
    s = re.sub(r":\s+", " ", "/".join(parts))
    s = re.sub(r"[^\w./ -]+", "_", s)
    segs = [p.strip() for p in s.split("/")]
    return "/".join("_" if p in (".", "..") else p for p in segs if p)[:250] or "_"


def metric_name(step: str, check: str) -> str:
    """'train', 'out/metrics.json: accuracy >= 0.9' -> 'train/out/metrics.json accuracy': the rule is left out, so
    tightening a threshold keeps the series; a metrics check is named by its pattern's leading word."""
    what = RULE_AT_END.sub("", check)
    m = re.fullmatch(r"(\S+) /(.*)/", what)            # "stdout /val_loss=([0-9.]+)/"
    if m:
        lead = re.match(r"[\w. -]*", m.group(2).lstrip("^")).group(0).strip(" =")
        what = f"{m.group(1)} {lead or m.group(2)}"
    return name(step, what)


def metrics(rep: dict) -> dict:
    out = {}
    for s in rep.get("steps") or []:
        for c in s.get("expectations") or []:
            if (n := R.check_number(c)) is not None:
                key = metric_name(s["name"], c["check"])
                out[key if key not in out else f"{key}_{len(out)}"] = n
    return out


def params(run_dir: Path) -> dict:
    """The steps' params (notebook form fields and the like): what was varied."""
    try:
        plan = json.loads((Path(run_dir) / "plan.json").read_text())
    except (OSError, ValueError):
        return {}
    return {name(s["name"], str(k)): v for s in plan.get("steps", []) for k, v in (s.get("params") or {}).items()}


def _iso(t) -> str | None:
    return dt.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if isinstance(t, (int, float)) else None


def record(root: Path, run_dir: Path, rep: dict, project: str) -> dict:
    src, env = rep.get("source") or {}, rep.get("environment") or {}
    steps = rep.get("steps") or []
    checks = [c for s in steps for c in s.get("expectations") or []]
    return {"run_id": rep.get("run_id") or Path(run_dir).name, "project": project, "plan": rep.get("plan"),
            "status": rep.get("status"), "backend": rep.get("backend"), "gpu": rep.get("gpu"),
            "started": _iso(rep.get("started")), "finished": _iso(rep.get("finished")), "minutes": rep.get("minutes"),
            "cost_usd": rep.get("cost_usd"), "worst_case_usd": rep.get("worst_case_usd"), "commit": src.get("commit"),
            "branch": src.get("branch"), "dirty": src.get("dirty"), "steps": len(steps),
            "steps_passed": sum(s.get("status") == "passed" for s in steps), "checks": len(checks),
            "checks_passed": sum(bool(c.get("ok")) for c in checks), "evidence_files": len(rep.get("evidence") or []),
            "evidence_sha256": rep.get("evidence_sha256"), "stopped_because": rep.get("stopped_because"),
            "testbench_version": rep.get("testbench_version"), "python": env.get("python"),
            "report": _short(root, Path(run_dir) / "report.json"), "params": params(run_dir), "metrics": metrics(rep),
            "_times": (rep.get("started"), rep.get("finished"))}


def _facts(rec: dict) -> dict:
    """The run's own numbers, for trackers: next to the checked ones (whose names all have a /)."""
    out = {k: rec[k] for k in ("cost_usd", "minutes", "steps_passed", "checks_passed")
           if isinstance(rec.get(k), (int, float))}
    return {**out, "passed": 1.0 if rec["status"] == "passed" else 0.0}


def _ms(t) -> int | None:
    return int(t * 1000) if isinstance(t, (int, float)) else None


def _path(root: Path, p) -> Path:
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_absolute() else Path(root) / p


def _short(root: Path, p) -> str:
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(p)


def _note(rec: dict) -> str:
    return (f"agent-testbench run {rec['run_id']} of plan {rec['plan']}: {rec['status']}. The proof is "
            f"{rec['report']} in project {rec['project']}; `testbench verify {rec['run_id']}` checks its evidence.")


# ---- csv and jsonl: one row per run, a run exported again replaces its row --------------------------------------

def _flat(rec: dict) -> dict:
    return {**{k: rec.get(k) for k in FIELDS}, **{f"params.{k}": v for k, v in rec["params"].items()},
            **{f"metrics.{k}": v for k, v in rec["metrics"].items()}}


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def to_csv(root: Path, run_dir: Path, rec: dict, opts: dict) -> str:
    path = _path(root, opts.get("path") or ".testbench/runs.csv")
    with procs.exclusive(Path(root) / ".testbench" / "export.lock"):
        rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8")))) if path.is_file() else []
        rows = [{k: v for k, v in r.items() if k is not None} for r in rows if r.get("run_id") != rec["run_id"]]
        rows = sorted(rows + [_flat(rec)], key=lambda r: r.get("run_id") or "")
        cols = list(FIELDS) + sorted({k for r in rows for k in r} - set(FIELDS))
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        _replace(path, out.getvalue())
    return f"row {[r.get('run_id') for r in rows].index(rec['run_id']) + 1} of {_short(root, path)}"


def _run_id(line: str) -> str:
    try:
        d = json.loads(line)
    except ValueError:
        return line                                      # a line that is not ours: kept as it is
    return str(d.get("run_id", line)) if isinstance(d, dict) else line


def to_jsonl(root: Path, run_dir: Path, rec: dict, opts: dict) -> str:
    path = _path(root, opts.get("path") or ".testbench/runs.jsonl")
    line = json.dumps({k: v for k, v in rec.items() if not k.startswith("_")}, default=str)
    with procs.exclusive(Path(root) / ".testbench" / "export.lock"):
        lines = [ln for ln in (path.read_text(encoding="utf-8").splitlines() if path.is_file() else []) if ln.strip()]
        keep = {_run_id(ln): ln for ln in lines + [line]}
        order = sorted(keep)
        _replace(path, "\n".join(keep[k] for k in order) + "\n")
    return f"line {order.index(rec['run_id']) + 1} of {_short(root, path)}"


# ---- MLflow ---------------------------------------------------------------------------------------------------

def _tracking_uri(root: Path, uri: str) -> str:
    """A local store relative to the project (sqlite:///.testbench/mlflow.db, file:mlruns, mlruns) is in it."""
    if uri.startswith("sqlite:///") and not uri.startswith("sqlite:////"):
        return "sqlite:///" + _path(root, uri[10:]).resolve().as_posix()
    if uri.startswith("file:") and not uri.startswith("file:/"):
        return _path(root, uri[5:]).resolve().as_uri()
    if "://" not in uri and not uri.startswith("file:"):        # a plain folder: MLflow's file store
        return _path(root, uri).resolve().as_uri()
    return uri


def to_mlflow(root: Path, run_dir: Path, rec: dict, opts: dict) -> str:
    require("mlflow")
    from mlflow.entities import Metric, Param, RunTag
    from mlflow.tracking import MlflowClient
    uri = opts.get("tracking_uri") or os.environ.get("MLFLOW_TRACKING_URI")
    if not uri:
        raise ExportError("no MLflow tracking URI: set `export.mlflow.tracking_uri` in testbench.yaml (e.g. "
                          "http://127.0.0.1:5000 or sqlite:///.testbench/mlflow.db), MLFLOW_TRACKING_URI, or --tracking-uri")
    uri = _tracking_uri(root, uri)
    client = MlflowClient(tracking_uri=uri)
    experiment = opts.get("experiment") or rec["project"]
    exp = client.get_experiment_by_name(experiment)
    eid = exp.experiment_id if exp else client.create_experiment(experiment)
    started, finished = rec["_times"]
    tags = {"mlflow.runName": rec["run_id"], "mlflow.note.content": _note(rec), "mlflow.source.name": "agent-testbench",
            "mlflow.source.git.commit": rec["commit"], "mlflow.source.git.branch": rec["branch"],
            **{f"testbench.{k}": rec[k] for k in ("run_id", "project", "plan", "status", "backend", "gpu", "report",
                                                   "evidence_sha256", "dirty", "stopped_because")}}
    tags = {k: str(v)[:5000] for k, v in tags.items() if v is not None}
    q = f"tags.`testbench.run_id` = '{rec['run_id']}' and tags.`testbench.project` = '{rec['project']}'"
    found = client.search_runs([eid], filter_string=q, max_results=1)
    run = found[0] if found else client.create_run(eid, start_time=_ms(started), tags=tags, run_name=rec["run_id"])
    rid, have = run.info.run_id, run.data
    when = _ms(finished) or _ms(started) or 0
    ms = [Metric(k, float(v), when, 0) for k, v in {**_facts(rec), **rec["metrics"]}.items() if k not in have.metrics]
    ps = {"backend": rec["backend"], "gpu": rec["gpu"], "python": rec["python"], **rec["params"]}
    ps = [Param(k, str(v)[:6000]) for k, v in ps.items() if v is not None and k not in have.params]
    for i in range(0, max(len(ms), len(ps), 1), 100):                     # log_batch takes up to 100 params
        client.log_batch(rid, metrics=ms[i:i + 100], params=ps[i:i + 100],
                         tags=[RunTag(k, v) for k, v in tags.items()] if i == 0 else [])
    if opts.get("artifacts", True) and have.tags.get("testbench.artifacts") != rec["evidence_sha256"]:
        for f in ("report.json", "report.md"):
            if (Path(run_dir) / f).is_file():
                client.log_artifact(rid, str(Path(run_dir) / f))
        if (Path(run_dir) / "steps").is_dir():
            client.log_artifacts(rid, str(Path(run_dir) / "steps"), "steps")
        client.set_tag(rid, "testbench.artifacts", rec["evidence_sha256"] or "")
    status = {"passed": "FINISHED", "cancelled": "KILLED"}.get(rec["status"], "FAILED")
    client.set_terminated(rid, status, end_time=_ms(finished))
    link = f"{uri.rstrip('/')}/#/experiments/{eid}/runs/{rid}" if uri.startswith("http") else uri
    return f"MLflow run {rid} in experiment {experiment!r}{' (updated)' if found else ''}: {link}"


# ---- Weights & Biases -----------------------------------------------------------------------------------------

def to_wandb(root: Path, run_dir: Path, rec: dict, opts: dict) -> str:
    require("wandb")
    import wandb
    folder = _path(root, opts.get("dir") or ".testbench")
    folder.mkdir(parents=True, exist_ok=True)
    want = {"silent": True, "console": "off", "save_code": False, "x_disable_stats": True, "x_save_requirements": False}
    if rec["commit"]:
        want["git_commit"] = rec["commit"]
    try:
        settings = wandb.Settings(**want)
    except Exception:                                    # an older wandb without some of these
        settings = wandb.Settings(silent=True)
    project = opts.get("project") or rec["project"]
    tags = [str(t)[:64] for t in [*(opts.get("tags") or []), rec["plan"], rec["status"], "agent-testbench"] if t]
    run = wandb.init(project=project, entity=opts.get("entity"), id=rec["run_id"], name=rec["run_id"], resume="allow",
                     mode=opts.get("mode"), dir=str(folder), group=rec["plan"], job_type="testbench",
                     tags=list(dict.fromkeys(tags)), notes=_note(rec), settings=settings)
    try:
        run.config.update({**{k: rec[k] for k in ("run_id", "plan", "backend", "gpu", "commit", "branch", "dirty",
                                                  "report", "evidence_sha256", "python", "testbench_version")},
                           **rec["params"]}, allow_val_change=True)
        run.summary.update({**_facts(rec), "status": rec["status"], **rec["metrics"]})
        for f in ("report.json", "report.md"):
            if (Path(run_dir) / f).is_file():
                run.save(str(Path(run_dir) / f), base_path=str(run_dir), policy="now")
        if opts.get("artifacts", True):
            art = wandb.Artifact(f"testbench-{rec['run_id']}", type="testbench-evidence",
                                 metadata={k: rec[k] for k in ("run_id", "plan", "status", "commit", "evidence_sha256")})
            files = ["report.json", "report.md"] + [e["path"] for e in (R.load(run_dir) or {}).get("evidence") or []]
            for rel in files:
                if (Path(run_dir) / rel).is_file():
                    art.add_file(str(Path(run_dir) / rel), name=rel, policy="immutable")
            run.log_artifact(art)
        url = None if getattr(run, "offline", True) else run.url
        where = _short(root, Path(run.dir).parent)
    finally:
        run.finish(exit_code=0 if rec["status"] == "passed" else 1)
    if url:
        return f"W&B run {rec['run_id']} in project {project!r}: {url}"
    if (opts.get("mode") or os.environ.get("WANDB_MODE")) == "disabled":
        return "W&B is disabled (mode: disabled), so nothing was kept"
    return f"W&B run {rec['run_id']} in project {project!r}, offline in {where}; `wandb sync {where}` uploads it"


EXPORTERS = {"mlflow": to_mlflow, "wandb": to_wandb, "csv": to_csv, "jsonl": to_jsonl}


# ---- running exports, and what they left in export.log ----------------------------------------------------------

def require(target: str) -> None:
    """Trackers are imported only when used; a missing one says how to install it into this Python. Each is set up
    to send nothing but the run (no usage telemetry or error reports) and to say little, unless the environment
    already says otherwise."""
    for k, v in QUIET.get(target, {}).items():
        os.environ.setdefault(k, v)
    if target in PACKAGES:
        try:
            __import__(target)
        except ImportError:
            raise Missing(f"{PACKAGES[target]} is not installed in this Python ({sys.executable}): "
                          f"pip install 'agent-testbench[{target}]' (or pip install {target})") from None


def redact(text: str) -> str:
    """No secret from the environment, and no password in a URL, in anything printed or written."""
    for k, v in os.environ.items():
        if v and len(v) >= 6 and re.search(r"TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL", k, re.I):
            text = text.replace(v, f"<{k}>")
    return re.sub(r"(\w+://)[^/@\s]+@", r"\1***@", text)


_FINISHED: set = set()


def log(run_dir: Path, target: str, state: str, message: str) -> None:
    line = " ".join(redact(message).split())[:1500]
    with open(Path(run_dir) / "export.log", "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {target}: {state} - {line}\n")
    if state != "started":
        _FINISHED.add(target)


def export(root: Path, run_dir: Path, targets: dict, project: str) -> list[tuple[str, bool, str, int]]:
    """Export one finished run to each target ({name: options}); never raises. (target, ok, message, exit code)."""
    from .runs import DONE
    out, why = [], f"{Path(run_dir).name} has not finished; export it when it ends (`testbench wait {Path(run_dir).name}`)"
    try:
        rep = R.load(run_dir)
        rec = record(root, run_dir, rep, project) if rep and rep.get("status") in DONE else None
    except Exception as exc:
        rec, why = None, f"could not read {Path(run_dir).name}'s report.json: {type(exc).__name__}: {exc}"
    for target, opts in targets.items():
        try:
            if rec is None:
                raise ExportError(why)
            ok, msg, code = True, EXPORTERS[target](root, run_dir, rec, opts or {}), 0
        except Exception as exc:
            ok, code = False, getattr(exc, "code", 1)
            msg = str(exc) if isinstance(exc, ExportError) else f"{type(exc).__name__}: {str(exc)[:600]}"
        msg = " ".join(redact(msg).split())
        log(run_dir, target, "ok" if ok else "failed", msg)
        out.append((target, ok, msg, code))
    return out


def after_run(root: Path, run_dir: Path) -> None:
    """At the end of every run: export it where testbench.yaml's `export:` says. Never raises, never waits on a
    tracker: csv and jsonl now, MLflow and W&B in a background process (at most BACKGROUND_LIMIT seconds)."""
    try:
        raw = yaml.safe_load((Path(root) / C.CONFIG_NAME).read_text()) or {}
        if not isinstance(raw, dict) or not raw.get("export"):
            return                                       # no exports: nothing to load, nothing to log
        cfg = C.load(root)
        targets = cfg["export"]
        local = {t: o for t, o in targets.items() if t in LOCAL}
        if local:
            export(root, run_dir, local, cfg["project"])
        remote = [t for t in targets if t not in LOCAL]
        for t in remote:
            log(run_dir, t, "started", "in the background")
        if remote:
            procs.spawn([sys.executable, "-m", "agent_testbench.export", str(Path(run_dir).resolve()), *remote],
                        cwd=root, log_path=Path(run_dir) / "export.log")
    except Exception as exc:
        try:
            log(run_dir, "export", "failed", f"{type(exc).__name__}: {exc}")
        except Exception:
            pass


def summary(run_dir: Path) -> str:
    """The latest outcome per target, for `testbench report`; empty when the run was never exported."""
    p = Path(run_dir) / "export.log"
    latest = {}
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []:
        if m := LINE.match(line):
            latest[m.group(2)] = m.groups()
    out = []
    for target, (when, _, state, msg) in latest.items():
        if state == "ok":
            out.append(f"- {target}: {msg}")
        elif state == "started":
            out.append(f"- {target}: started {when} in the background, no outcome yet (export.log in the run folder)")
        else:
            again = f"`testbench export {Path(run_dir).name}" + (f" --to {target}`" if target in TARGETS else "`")
            out.append(f"- {target}: FAILED ({when}): {msg}. Fix that, then {again}")
    return "\n".join(["## Exports", "", *out]) if out else ""


def _give_up(run_dir: Path, targets: list[str]) -> None:
    for t in targets:
        if t not in _FINISHED:
            log(run_dir, t, "failed", f"gave up after {BACKGROUND_LIMIT // 60} minutes without an answer from the tracker")
    os._exit(1)


def main(argv=None) -> None:
    """python -m agent_testbench.export RUN_DIR TARGET...: the background half of after_run."""
    argv = sys.argv[1:] if argv is None else argv
    run_dir, targets = Path(argv[0]).resolve(), argv[1:]
    timer = threading.Timer(BACKGROUND_LIMIT, _give_up, (run_dir, targets))
    timer.daemon = True
    timer.start()
    try:
        root = run_dir.parents[2]
        cfg = C.load(root)
        export(root, run_dir, {t: cfg["export"].get(t) or {} for t in targets}, cfg["project"])
    except Exception as exc:
        for t in targets:
            if t not in _FINISHED:
                log(run_dir, t, "failed", f"{type(exc).__name__}: {exc}")
    timer.cancel()
    if "wandb" in sys.modules:
        try:
            sys.modules["wandb"].teardown()
        except Exception:
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)                  # trackers can leave threads behind; the outcomes are written


if __name__ == "__main__":
    main()
