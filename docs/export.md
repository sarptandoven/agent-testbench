# Exports: MLflow, W&B, csv, jsonl

ML people already track experiments in MLflow or Weights & Biases. `testbench export` puts a finished run there,
next to the experiments it belongs with - its numbers, params and evidence, and the way back to the proof - or
in a csv or jsonl file, one row per run, for a spreadsheet, pandas or anything else.

```bash
testbench export --to csv                                      # the latest run -> .testbench/runs.csv
testbench export 20261101-064113-smoke --to mlflow --tracking-uri http://127.0.0.1:5000
testbench export all --to wandb --project my-project           # every finished run, oldest first
testbench export                                               # the latest run, to every target in testbench.yaml
```

To export every run when it finishes, add an `export:` section to testbench.yaml:

```yaml
export:
  mlflow: {tracking_uri: http://127.0.0.1:5000, experiment: my-project}
  wandb: {project: my-project, mode: offline}
  csv: .testbench/runs.csv
  jsonl: .testbench/runs.jsonl
```

MLflow and W&B are not dependencies of agent-testbench: install the one you use into the Python that runs
testbench, `pip install "agent-testbench[mlflow]"` or `pip install "agent-testbench[wandb]"` (or plain
`pip install mlflow` / `wandb`; `mlflow-skinny` is enough for an MLflow server over HTTP). Without it, an export
says which command installs it.

Somewhere else? A package can add a sink of its own - a sheet, an internal dashboard, a table in your wiki - and
then `export:` and `--to` take its name like the four above ([Plug-in sinks](#plug-in-sinks)).

## What a run becomes

| | MLflow | W&B | csv / jsonl |
|---|---|---|---|
| plan, status, backend, GPU | tags `testbench.*`, params `backend`, `gpu`; run status FINISHED, FAILED or KILLED | config, tags, group (the plan); exit code 0 or 1 | columns / keys |
| cost, minutes, steps and checks passed, `passed` (1 or 0) | metrics | summary | columns / keys |
| every number the checks found | metrics | summary | `metrics.<name>` |
| the steps' params (notebook form fields) | params `<step>/<NAME>` | config | `params.<name>` |
| commit, branch, uncommitted changes | `mlflow.source.git.commit`, `mlflow.source.git.branch`, `testbench.dirty` | config `commit`, `branch`, `dirty`; the run's git commit | `commit`, `branch`, `dirty` |
| run id, report path | run name, `testbench.run_id`, `testbench.report`, the run's description | run id and name, config `report`, notes | `run_id`, `report` |
| report and evidence | artifacts: `report.json`, `report.md`, `steps/...` | files `report.json`, `report.md`; artifact `testbench-<run id>` (type `testbench-evidence`) with every evidence file | - |

The numbers are the ones `testbench history` shows: each check that measured a number (`0.93 (wanted >= 0.9)`).
Their names are the step and the check without its rule, so tightening a threshold keeps the series:
`train/outputs/metrics.json accuracy`, `train/stdout val_loss` (a `metrics:` check, named by its pattern's leading
word), `train/gpu busy_pct`, `train/duration minutes`. Characters a tracker refuses become `_`.

The report path (`.testbench/runs/<run-id>/report.json`, relative to the project) and the run id get you back to
the proof: `testbench report <run-id>`, and `testbench verify <run-id>` checks the evidence still matches its
hashes. The evidence sha256 is in every target (`testbench.evidence_sha256`, config `evidence_sha256`).

## Exporting again

A run exported again is updated, not duplicated, wherever the tracker allows it:

- **csv and jsonl** replace the run's row, and keep rows in run order. Columns grow as new numbers appear.
- **MLflow** finds the run by its tags `testbench.run_id` and `testbench.project` in the experiment, adds what is
  missing (metrics and params already there are kept as they are, so no metric gets a second point) and uploads
  the evidence again only if it changed.
- **W&B** uses the testbench run id as the W&B run id (`resume: allow`), so an export again goes to the same run.
  Offline, each export writes its own `offline-run-*-<run-id>` folder; synced, they are one run.

## Settings

| `export.mlflow` | default | |
|---|---|---|
| `tracking_uri` | `MLFLOW_TRACKING_URI` | `http://...` for a server; `sqlite:///.testbench/mlflow.db` or a folder for a local store (relative paths are in the project) |
| `experiment` | the project's name | created when missing |
| `artifacts` | `true` | `false`: no report or evidence files |

| `export.wandb` | default | |
|---|---|---|
| `project` | the project's name | |
| `entity` | your default | a team or user |
| `mode` | `WANDB_MODE`, else W&B's default | `online`, `offline` (kept on this machine; `wandb sync` uploads it) or `disabled` |
| `dir` | `.testbench` | W&B's folder goes in it (`.testbench/wandb/`) |
| `tags` | `[]` | added to the plan, the status and `agent-testbench` |
| `artifacts` | `true` | `false`: no evidence artifact |

`csv` and `jsonl` take the file's path (relative to the project), or `{path: ...}`; `true` means
`.testbench/runs.csv` / `.testbench/runs.jsonl`. `false` leaves a target out. On the command line, `--to` picks one
target (a built-in or an installed plug-in sink) and `--tracking-uri`, `--experiment`, `--project`, `--entity`,
`--mode`, `--path` and `--no-artifacts` override its settings. The MCP tool is `export_run` (`run`, `to`).

## After every run

With an `export:` section, the end of every run exports it: csv and jsonl at once (milliseconds), MLflow and W&B
in a background process, so a slow or unreachable tracker never holds a run up (it gives up after 15 minutes).
A plug-in sink goes where it says it belongs: in the background with the trackers, or at the end of the run,
each in a thread of its own and written off after 60 seconds, so even a sink that hangs costs the run no more.
An export never fails a run. Every outcome goes to `export.log` in the run folder, and `testbench report` ends
with them:

```
## Exports

- csv: row 12 of .testbench/runs.csv
- mlflow: FAILED (2026-11-01 09:14:02): MlflowException: API request to http://127.0.0.1:5000/api/2.0/mlflow/experiments/get-by-name failed ... Connection refused. Fix that, then `testbench export 20261101-091355-smoke --to mlflow`
```

## Secrets

Credentials stay in the environment, where the trackers read them: `MLFLOW_TRACKING_TOKEN` (or
`MLFLOW_TRACKING_USERNAME` and `MLFLOW_TRACKING_PASSWORD`), `WANDB_API_KEY`, and your cloud's variables for MLflow
artifact stores. testbench.yaml refuses them (`api_key:`, `token:`, a password in `tracking_uri`) and says which
variable to set instead. Messages and export.log never show a secret's value: the value of any variable named like
`*TOKEN*`, `*KEY*`, `*SECRET*` or `*PASSWORD*`, and a password in a URL, are replaced before anything is printed or
written.

For exports, testbench turns off MLflow's usage telemetry (`MLFLOW_DISABLE_TELEMETRY`) and W&B's error reports
(`WANDB_ERROR_REPORTING`), so nothing but the run is sent; set those variables yourself to choose otherwise.

## Plug-in sinks

The four targets above are the built-in sinks. A package adds another with an entry point in the group
`agent_testbench.sinks`; once it is installed in the Python that runs testbench, its name works everywhere a
built-in's does:

```yaml
export:
  csv: .testbench/runs.csv
  markdown: docs/runs.md            # examples/plugins/testbench-sink-example: a markdown table row per run
```

```bash
testbench export all --to markdown
```

The sink checks its own settings, so a mistake is refused when testbench.yaml loads, naming the field
(`export.markdown.path`); a setting that would hold a secret is refused for every sink (give an environment
variable's name instead, in a setting ending in `_env`). A plug-in sink keeps the promises above: its outcome is a
line in `export.log` and `testbench report`, and nothing it does fails or holds up a run - even when its package
breaks after an upgrade, testbench.yaml still loads and only that export fails, saying the sink did not load.
[Plug-in sinks](plugins.md#sinks) shows how to write one and the contract `testbench plugin test` checks.

## How it is tested

`tests/test_export.py`: csv and jsonl always, against real runs. MLflow and W&B tests run where those packages are
installed and skip elsewhere; nothing leaves the machine. MLflow: a local sqlite store, read back with the MLflow
client (metrics, params, tags, artifacts, one run after exporting twice). W&B: offline mode with the server
address pointed at a closed local port, the offline run folder read back (run, config, summary, files, the
evidence artifact). To run them, keep the trackers out of your main environment:

```bash
python -m venv /tmp/export-venv && /tmp/export-venv/bin/pip install -e ".[dev]" mlflow wandb
/tmp/export-venv/bin/python -m pytest -q tests/test_export.py
```
