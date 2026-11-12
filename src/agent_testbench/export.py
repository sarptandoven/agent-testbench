"""Exports: a finished run in the experiment tracker people already use (MLflow, Weights & Biases) or in a table
(csv, jsonl), with the way back to its proof.

    testbench export [RUN|latest|all] [--to mlflow|wandb|csv|jsonl|a plug-in sink]
    export: {mlflow: {tracking_uri: ..., experiment: ...}, wandb: {project: ..., mode: offline}, csv: runs.csv}

A run becomes one record: plan, status, backend and GPU, cost, minutes, commit and dirty flag, the steps' params,
every number its checks found (the numbers `testbench history` shows, named step/check without the rule), the run
id and the report's path; trackers also get the report and the evidence files. Exporting a run again updates it:
csv and jsonl replace its row, MLflow finds it by tag, W&B uses the run id as its own. After every run
(runs.finalize -> after_run) csv and jsonl are written at once and trackers in a background process, so a slow or
unreachable tracker never holds a run up; every outcome goes to the run's export.log, which `testbench report`
shows. Secrets (WANDB_API_KEY, MLFLOW_TRACKING_TOKEN, ...) stay in the environment, where the trackers read them;
nothing here prints or writes them.

Each place a run can go is a sink (the `sinks` plug-in kind: KIND at the end; Sink says what one is). The four
above are the built-ins. A package adds more with an entry point in the group agent_testbench.sinks, and `export:`
and `--to` then take its name like a built-in's (docs/plugins.md#sinks).
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import importlib
import io
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import yaml

from . import config as C, plugins as P, procs, report as R

FIELDS = ("run_id", "project", "plan", "status", "backend", "gpu", "started", "finished", "minutes", "cost_usd",
          "worst_case_usd", "commit", "branch", "dirty", "steps", "steps_passed", "checks", "checks_passed",
          "evidence_files", "evidence_sha256", "stopped_because", "testbench_version", "python", "report")
QUIET = {"mlflow": {"MLFLOW_HTTP_REQUEST_MAX_RETRIES": "2", "MLFLOW_HTTP_REQUEST_TIMEOUT": "30",   # down: fails fast
                    "MLFLOW_DISABLE_TELEMETRY": "true", "MLFLOW_DISABLE_AGENT_HINT": "1", "MLFLOW_LOGGING_LEVEL": "WARNING"},
         "wandb": {"WANDB_SILENT": "true", "WANDB_ERROR_REPORTING": "false"}}
BACKGROUND_LIMIT = 900                                # seconds a background export may take before it gives up
FOREGROUND_LIMIT = 60                                 # seconds the quick sinks may take at the end of a run, in all
LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) (\w+): (ok|failed|started) - (.*)$")
RULE_AT_END = re.compile(r"\s+(?:(?:>=|<=|!=|==|>|<)\s*\S+|~=\s*\S+\s*(?:\+-|\+/-|±)\s*\S+|between\s+\S+\s+and\s+\S+"
                         r"|[-+]?\d[\d.eE+-]*)$", re.I)
SECRET_WORDS = re.compile(r"token|key|secret|password|passwd|credential", re.I)     # setting names never in the file
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ExportError(Exception):
    code = 1


class Missing(ExportError):
    code = 2


class Sink:
    """Where `export:` sends a finished run (the `sinks` plug-in kind). A plug-in sink is any object with these;
    subclassing this class is optional, and brings the settings check described at the end.

        name        its key under `export:` and its `--to` name ("csv"); the entry point's name when left empty
        background  True for anything that may touch the network or take long: after a run it goes to a background
                    process (at most BACKGROUND_LIMIT seconds), so it never holds the run up. False for quick local
                    work, done at the end of the run (FOREGROUND_LIMIT seconds for all of them, then the run goes on)
        requires    the module that must import for it to work ("mlflow"), or "". Imported only when the sink is
                    used; a missing one fails that export with the pip command that installs it, never the run
        package     what to pip install when `requires` does not import (default: `requires`)
        title       how messages name it ("MLflow"; default: its name)
        example     settings that validate, for `testbench plugin test` (relative paths land in a scratch folder)

        validate(where, opts) -> dict
            The settings as written under `export.NAME` (None or true for none, a string for a short form) -> the
            settings send() gets. Anything it cannot use raises config.ConfigError naming the field (`where` is
            "export.NAME") and saying what to write instead. Secrets never go in testbench.yaml: a setting named
            like a token, key or password is refused, unless it ends in _env and holds an environment variable's
            NAME.
        send(root, run_dir, record, opts) -> str
            Export one finished run (`record`: what record() makes - FIELDS, and params and metrics) and return
            what was done, in one line ("row 3 of .testbench/runs.csv"). A plain failure raises ExportError (Missing
            for something to install) with a message that says what to do. Whatever it raises fails that export
            only, in export.log; nothing a sink does can fail a run. Never print or return a secret (the
            environment's are blanked out of messages too, but a sink must not lean on that).

    The settings check this class brings: `options` names the settings it takes; each is a non-empty string,
    except `flags` ({name: what true means}: true or false) and `lists` (lists of words). `short` is the setting a
    plain string stands for (`csv: runs.csv`) and `short_hint` what that string is ("the file's path"); `hints`
    ({setting: an example value}) go in the messages, and `secrets` names the variables to set when someone writes
    a secret in the file. check(where, opts) adds rules of its own; or override validate() altogether.
    """
    name = ""
    background = False
    requires = ""
    package = ""
    title = ""
    example: dict = {}
    options: tuple = ()
    flags: dict = {}
    lists: tuple = ()
    short = ""
    short_hint = ""
    hints: dict = {}
    secrets = "it"
    quiet: dict = {}                                  # environment defaults set before `requires` is imported

    def __init__(self, name: str = "", send=None, **settings):
        for k, v in settings.items():
            if k.startswith("_") or not hasattr(Sink, k) or callable(getattr(Sink, k)):
                raise TypeError(f"a Sink has no setting {k!r}")
            setattr(self, k, v)
        if name:
            self.name = name
        if send is not None:
            self.send = send

    def __repr__(self):
        return f"<sink {self.name or '?'}>"

    def validate(self, where: str, opts) -> dict:
        opts = {self.short: opts} if isinstance(opts, str) and self.short else {} if opts in (None, True) else opts
        if not isinstance(opts, dict):
            raise C.ConfigError(f"`{where}` is a mapping of settings ({', '.join(self.options)})"
                                + (f", or {self.short_hint}, e.g. `{self.name}: {self.hints.get(self.short)}`"
                                   if self.short else ""))
        for k, v in opts.items():
            if SECRET_WORDS.search(str(k)) and not (k in self.options and str(k).endswith("_env")):
                raise C.ConfigError(f"`{where}.{k}`: secrets never go in testbench.yaml. Set {self.secrets} in the "
                                    "environment, where the tracker reads it; testbench never prints or writes it")
            if k not in self.options:
                raise C.ConfigError(f"`{where}` has no {k!r}; use {', '.join(self.options)}")
            if k in self.flags and not isinstance(v, bool):
                raise C.ConfigError(f"`{where}.{k}` is true ({self.flags[k]}) or false")
            if k in self.lists and (not isinstance(v, list) or not all(isinstance(t, str) for t in v)):
                raise C.ConfigError(f"`{where}.{k}` is a list of words, e.g. {self.hints.get(k, '[a, b]')}")
            if k not in self.flags and k not in self.lists and (not isinstance(v, str) or not v.strip()):
                raise C.ConfigError(f"`{where}.{k}` is a string, e.g. {self.hints.get(k, '...')}")
            if str(k).endswith("_env") and not VARIABLE.fullmatch(v):
                raise C.ConfigError(f"`{where}.{k}` is the NAME of an environment variable that holds the value, "
                                    f"e.g. {self.hints.get(k, 'MY_TOKEN')} - not the value itself")
        self.check(where, opts)
        return opts

    def check(self, where: str, opts: dict) -> None:
        """Rules of the sink's own, after the ones validate() applies to every setting."""

    def send(self, root: Path, run_dir: Path, record: dict, opts: dict) -> str:
        raise ExportError(f"the {self.name or '?'} sink has no send(); its package is unfinished")


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
    procs.write_text_atomic(path, text)


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


# ---- the built-in sinks -----------------------------------------------------------------------------------------

ARTIFACTS = {"artifacts": "upload the report and the evidence files, the default"}


class _MLflow(Sink):
    def check(self, where: str, opts: dict) -> None:
        if re.match(r"\w[\w+.-]*://[^/@\s]+@", str(opts.get("tracking_uri", ""))):
            raise C.ConfigError(f"`{where}.tracking_uri` holds credentials; write the URL without them and set "
                                f"{self.secrets} in the environment")


class _WandB(Sink):
    def check(self, where: str, opts: dict) -> None:
        if opts.get("mode") not in (None, "online", "offline", "disabled"):
            raise C.ConfigError(f"`{where}.mode` is online (to the W&B server), offline (kept on this machine for "
                                "`wandb sync`) or disabled")
        if set(str(opts.get("project", ""))) & set("/\\#?%:"):
            raise C.ConfigError(f"`{where}.project` is a W&B project name, without / \\ # ? % or :")


MLFLOW = _MLflow("mlflow", to_mlflow, background=True, requires="mlflow", title="MLflow", quiet=QUIET["mlflow"],
                 options=C.EXPORT_KEYS["mlflow"], flags=ARTIFACTS,
                 example={"tracking_uri": "sqlite:///.testbench/mlflow.db"},
                 hints={"tracking_uri": "http://127.0.0.1:5000 or sqlite:///.testbench/mlflow.db",
                           "experiment": "my-project"},
                 secrets="MLFLOW_TRACKING_TOKEN (or MLFLOW_TRACKING_USERNAME and MLFLOW_TRACKING_PASSWORD)")
WANDB = _WandB("wandb", to_wandb, background=True, requires="wandb", title="Weights & Biases (wandb)",
               quiet=QUIET["wandb"], options=C.EXPORT_KEYS["wandb"], flags=ARTIFACTS, lists=("tags",),
               example={"mode": "offline"}, secrets="WANDB_API_KEY",
               hints={"project": "my-project", "entity": "my-team", "mode": "offline", "dir": ".testbench",
                         "tags": "[baseline, nightly]"})
CSV = Sink("csv", to_csv, options=C.EXPORT_KEYS["csv"], short="path", short_hint="the file's path",
           hints={"path": ".testbench/runs.csv"})
JSONL = Sink("jsonl", to_jsonl, options=C.EXPORT_KEYS["jsonl"], short="path", short_hint="the file's path",
             hints={"path": ".testbench/runs.jsonl"})


def __getattr__(attr: str):
    """TARGETS, EXPORTERS, LOCAL and PACKAGES as callers knew them before sinks were plug-ins, read from the
    registry now (so they include installed plug-in sinks)."""
    found = P.registry("sinks") if attr in ("TARGETS", "EXPORTERS", "LOCAL", "PACKAGES") else None
    if attr == "TARGETS":
        return tuple(found)
    if attr == "EXPORTERS":
        return {n: s.send for n, s in found.items()}
    if attr == "LOCAL":
        return tuple(n for n, s in found.items() if getattr(s, "background", False) is not True)
    if attr == "PACKAGES":
        return {n: getattr(s, "title", "") or n for n, s in found.items() if getattr(s, "requires", "")}
    raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")


def sink(target: str):
    """The sink of that name (a built-in or an installed plug-in); plugins.PluginError says why there is none."""
    return P.get("sinks", target)


def background(target: str) -> bool:
    """Whether the sink runs in the background process after a run. One that does not load fails at once instead,
    in export.log."""
    try:
        return getattr(sink(target), "background", False) is True
    except Exception:
        return False


def known(target: str) -> bool:
    """A sink installed here, loaded or not (`testbench export --to NAME` can try it again)."""
    try:
        return target in P.names("sinks") or target in P.broken("sinks")
    except Exception:
        return False


# ---- running exports, and what they left in export.log ----------------------------------------------------------

def require(target: str) -> None:
    """Trackers are imported only when used; a missing one says how to install it into this Python. Each is set up
    to send nothing but the run (no usage telemetry or error reports) and to say little, unless the environment
    already says otherwise."""
    try:
        found = sink(target)
    except P.PluginError:
        return                                   # not a sink here: check_export says so, and export() again
    _require(found, target)


def _require(found, target: str) -> None:
    for k, v in (getattr(found, "quiet", None) or {}).items():
        os.environ.setdefault(k, v)
    module = getattr(found, "requires", "") or ""
    if module:
        try:
            importlib.import_module(module)
        except ImportError:
            package = getattr(found, "package", "") or module
            how = (f"pip install 'agent-testbench[{target}]' (or pip install {package})"
                   if P.builtin("sinks", target) else f"pip install {package}")
            raise Missing(f"{getattr(found, 'title', '') or target} is not installed in this Python "
                          f"({sys.executable}): {how}") from None


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
            found = sink(target)
            _require(found, target)
            msg = found.send(root, run_dir, rec, opts or {})
            ok, msg, code = True, "done" if msg is None else str(msg), 0
        except (Exception, SystemExit) as exc:                 # a plug-in's sys.exit() fails its export, not the run
            ok, code = False, exc.code if isinstance(exc, ExportError) else 1
            plain = isinstance(exc, (ExportError, P.PluginError))          # a message that says what to do
            msg = str(exc) if plain else f"{type(exc).__name__}: {str(exc)[:600]}"
        msg = " ".join(redact(msg).split())
        log(run_dir, target, "ok" if ok else "failed", msg)
        out.append((target, ok, msg, code))
    return out


def after_run(root: Path, run_dir: Path) -> None:
    """At the end of every run: export it where testbench.yaml's `export:` says. Never raises, never waits on a
    tracker: csv and jsonl now, MLflow and W&B in a background process (at most BACKGROUND_LIMIT seconds); a plug-in
    sink goes where its `background` says."""
    try:
        if not (Path(root) / C.CONFIG_NAME).is_file():
            return                                       # a folder `testbench check` ran in: no export: section
        raw = yaml.safe_load((Path(root) / C.CONFIG_NAME).read_text()) or {}
        if not isinstance(raw, dict) or not raw.get("export"):
            return                                       # no exports: nothing to load, nothing to log
        cfg = C.load(root)
        targets = cfg["export"]
        local = {t: o for t, o in targets.items() if not background(t)}
        if local:
            _at_the_end(root, run_dir, local, cfg["project"])
        remote = [t for t in targets if background(t)]
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


def _at_the_end(root: Path, run_dir: Path, targets: dict, project: str) -> None:
    """The quick sinks: the built-ins one after the other (csv, jsonl), each plug-in in a thread of its own, all in
    FOREGROUND_LIMIT seconds. One that hangs (a plug-in that should have said background = True) holds up no other
    sink, costs the end of the run that long at worst, and export.log says so."""
    done: set = set()

    def go(group: list) -> None:
        for t in group:
            export(root, run_dir, {t: targets[t]}, project)
            done.add(t)

    groups = [[t for t in targets if P.builtin("sinks", t)], *([t] for t in targets if not P.builtin("sinks", t))]
    workers = [threading.Thread(target=go, args=(group,), daemon=True) for group in groups if group]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + FOREGROUND_LIMIT
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    for t in targets:
        if t not in done:
            log(run_dir, t, "failed", f"no outcome after {FOREGROUND_LIMIT:g} s at the end of the run, so the run "
                "went on without it. A sink that can be slow should say background = True (its package's job), so it "
                "runs in a background process; `testbench export` tries it again")


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
            again = f"`testbench export {Path(run_dir).name}" + (f" --to {target}`" if known(target) else "`")
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


# ---- the sinks plug-in kind: what the registry holds, and the contract `testbench plugin test` checks ------------

NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
CHECK_LIMIT = 120                # seconds the contract waits for one send()
QUICK = 10                       # seconds a sink with background = False may take in the contract


def _prepare(obj, name: str):
    found = obj() if isinstance(obj, type) else obj
    if not callable(getattr(found, "send", None)):
        raise P.PluginError(f"{obj!r} is not a sink: a sink has send(root, run_dir, record, opts) (export.Sink)")
    if not getattr(found, "name", ""):
        found.name = name
    return found


def _scratch_run(root: Path) -> Path:
    """A finished run's folder, as runs.finalize leaves it, for a sink to export in the contract."""
    run_dir = Path(root) / ".testbench" / "runs" / "20260101-000000-check"
    (run_dir / "steps" / "01-check").mkdir(parents=True)
    (run_dir / "steps" / "01-check" / "log.txt").write_text("val_loss=0.25\n")
    plan = {"backend": "local", "steps": [{"name": "check", "params": {"N": 3}}]}
    (run_dir / "plan.json").write_text(json.dumps(plan))
    rep = {"run_id": run_dir.name, "plan": "check", "backend": "local", "gpu": None, "status": "passed",
           "started": 1767225600.0, "finished": 1767225660.0, "minutes": 1.0, "cost_usd": 0.0, "testbench_version": "0",
           "schema_version": 1, "source": None, "steps": [{"name": "check", "kind": "run", "status": "passed",
                                                           "dir": "steps/01-check", "minutes": 1.0, "expectations": [
               {"check": "stdout /val_loss=([0-9.]+)/ < 0.3", "ok": True, "detail": "0.25 (wanted < 0.3)"}]}]}
    from . import provenance
    rep["evidence"], rep["evidence_sha256"] = provenance.evidence(run_dir)
    R.write(run_dir, rep)
    return run_dir


class _NoNetwork:
    """While a sink is checked: connections to other machines are refused (and noted), loopback ones go through."""
    LOCAL = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")

    def __init__(self):
        import socket
        import urllib.parse
        self.socket, self.reached = socket, []
        self.saved = (socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo)
        self.proxies = set()                         # a proxy on this machine is the way out all the same
        for k, v in os.environ.items():
            if k.lower().endswith("_proxy") and k.lower() != "no_proxy" and "://" in v:
                try:
                    p = urllib.parse.urlsplit(v)
                    self.proxies.add((p.hostname, p.port or (443 if p.scheme == "https" else 80)))
                except ValueError:
                    pass

    def _local(self, host, port=None) -> bool:
        host = host.decode() if isinstance(host, bytes) else str(host)
        return (host, port) not in self.proxies and (host in self.LOCAL or host.startswith("127.")
                                                     or host.startswith("::ffff:127."))

    def __enter__(self):
        connect, connect_ex, getaddrinfo = self.saved
        guard = self

        def refuse(address) -> bool:
            if isinstance(address, tuple) and address and not guard._local(*address[:2]):
                guard.reached.append(f"{address[0]}:{address[1]}" if len(address) > 1 else str(address[0]))
                return True
            return False

        def checked_connect(sock, address):
            if refuse(address):
                raise ConnectionRefusedError("the sink check lets nothing leave this machine")
            return connect(sock, address)

        def checked_connect_ex(sock, address):
            return 111 if refuse(address) else connect_ex(sock, address)

        def checked_getaddrinfo(host, *args, **kwargs):
            if host is not None and refuse((host, args[0] if args else "")):
                raise self.socket.gaierror("the sink check lets nothing leave this machine")
            return getaddrinfo(host, *args, **kwargs)

        self.socket.socket.connect, self.socket.socket.connect_ex = checked_connect, checked_connect_ex
        self.socket.getaddrinfo = checked_getaddrinfo
        return self

    def __exit__(self, *exc):
        self.socket.socket.connect, self.socket.socket.connect_ex, self.socket.getaddrinfo = self.saved


def _contract(found) -> list[str]:
    """What is wrong with a sink, in plain words ([] = it keeps the contract): a name, settings checked (junk
    refused with a ConfigError naming the field), send() into a scratch folder either working or raising ExportError,
    no secret shown (one in the environment and one slipped into its settings), and background = True if it reaches
    for another machine."""
    out = []
    name = getattr(found, "name", "")
    if not isinstance(name, str) or not NAME.fullmatch(name):
        out.append(f"its name is {name!r}: a sink's name is a short lowercase word (its key under `export:` and its "
                   "--to name), e.g. sheets")
    if not isinstance(getattr(found, "background", False), bool):
        out.append("background is True (it may touch the network or take long, so it runs in a background process) "
                   "or False (quick local work at the end of a run)")
    lacking = [f"it has no {how} (export.Sink says what each does)" for attr, how in (
        ("validate", "validate(where, opts) -> dict"), ("send", "send(root, run_dir, record, opts) -> str"))
        if not callable(getattr(found, attr, None))]
    if lacking:
        return out + lacking
    where = f"export.{name}"
    for junk in (42, ["a", "list"], {"no_such_setting": 1}):
        try:
            found.validate(where, copy.deepcopy(junk))
            out.append(f"validate() took {junk!r} as its settings; refuse what it cannot use with config.ConfigError, "
                       f"naming `{where}` and saying what to write instead")
        except C.ConfigError as exc:
            if where not in str(exc):
                out.append(f"validate() refused {junk!r} without naming the field ({exc}); name `{where}` (or the "
                           "setting under it), so the reader knows which line of testbench.yaml to fix")
        except Exception as exc:
            out.append(f"validate() raised {type(exc).__name__} for {junk!r} ({exc}); refuse bad settings with "
                       "config.ConfigError, whose message says what to write instead")
    example = getattr(found, "example", None) or {}
    try:
        opts = found.validate(where, copy.deepcopy(example))
    except Exception as exc:
        return out + [f"validate() refused its own example settings {example!r} ({type(exc).__name__}: {exc}); "
                      "`example` holds settings that validate, so its send() can be tried"]
    if not isinstance(opts, dict):
        return out + [f"validate() returned {type(opts).__name__}; it returns the settings send() gets, as a dict"]
    return out + _try_send(found, name, opts)


def _try_send(found, name: str, opts: dict) -> list[str]:
    import contextlib
    import secrets
    import tempfile
    out, shh = [], {"env": f"tb-check-{secrets.token_hex(8)}", "opts": f"tb-check-{secrets.token_hex(8)}"}
    variable, before = "TESTBENCH_CHECK_TOKEN", os.environ.get("TESTBENCH_CHECK_TOKEN")
    printed, got = io.StringIO(), []
    with tempfile.TemporaryDirectory(prefix="testbench-sink-check-") as tmp:
        root = Path(tmp)
        run_dir = _scratch_run(root)
        rec = record(root, run_dir, R.load(run_dir), "sinkcheck")

        def go():
            try:
                _require(found, name)
                got.append(("ok", found.send(root, run_dir, rec, {**opts, "token": shh["opts"]})))
            except ExportError as exc:
                got.append(("failed", str(exc)))
            except BaseException as exc:
                got.append(("raised", exc))

        began = time.monotonic()
        os.environ[variable] = shh["env"]
        try:
            with _NoNetwork() as net, contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
                worker = threading.Thread(target=go, daemon=True)
                worker.start()
                worker.join(CHECK_LIMIT)
        finally:
            if before is None:
                os.environ.pop(variable, None)
            else:
                os.environ[variable] = before
        took = time.monotonic() - began
        logged = (run_dir / "export.log").read_text(errors="replace") if (run_dir / "export.log").is_file() else ""
    if not got:
        return [f"send() did not finish within {CHECK_LIMIT} s; a sink gives up on what does not answer and raises "
                "ExportError saying so"]
    state, said = got[0]
    if state == "raised":
        why = str(said)[:300].replace(shh["env"], "<a secret>").replace(shh["opts"], "<a secret>")
        out.append(f"send() raised {type(said).__name__} ({why}); a plain failure raises export.ExportError "
                   "(Missing for something to install) with a message that says what to do")
    elif state == "ok" and not isinstance(said, str):
        out.append(f"send() returned {type(said).__name__}; it returns what it did as one line of text, e.g. "
                   "'row 3 of .testbench/runs.csv'")
    shown = f"{said}\n{printed.getvalue()}\n{logged}"
    leaked = [where for where, value in shh.items() if value in shown]
    if leaked:
        out.append("a secret came out in what send() returned, printed or wrote to export.log (one put in "
                   + " and one in ".join({"env": f"the environment as {variable}", "opts": "its settings"}[w]
                                         for w in leaked)
                   + "); say what was done without echoing settings or the environment")
    if net.reached and getattr(found, "background", False) is not True:
        out.append(f"it reached for {net.reached[0]} with background = False; anything that may touch the network "
                   "says background = True, so it runs in a background process and a slow service never holds a run up")
    elif getattr(found, "background", False) is not True and took > QUICK:
        out.append(f"it took {took:.0f} s with background = False; the end of a run waits for such sinks, so they "
                   f"take well under {QUICK} s (local files) - or say background = True")
    return out


KIND = P.Kind("sinks", "sink", "where `export:` sends every finished run (`testbench export --to NAME`)",
              {"mlflow": "agent_testbench.export:MLFLOW", "wandb": "agent_testbench.export:WANDB",
               "csv": "agent_testbench.export:CSV", "jsonl": "agent_testbench.export:JSONL"},
              prepare=_prepare, contract=_contract)


if __name__ == "__main__":
    main()
