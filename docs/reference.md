# testbench.yaml reference

Paths are relative to the folder holding `testbench.yaml`. Every validation error names the field and says what
to write instead.

The smallest testbench.yaml is a plan with one command; everything else has a default:

```yaml
plans:
  tests: "pytest -q"
```

## Autocomplete in your editor

testbench.yaml has a JSON Schema ([`src/agent_testbench/schema/testbench.schema.json`](../src/agent_testbench/schema/testbench.schema.json);
`testbench schema` prints it). With it, an editor completes keys, shows what each one does on hover, and flags a
misspelt key or a wrong value as you type. `testbench init` writes this first line, which the YAML language server
(VS Code's YAML extension, and editors built on it) reads:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/src/agent_testbench/schema/testbench.schema.json
```

Or, for every testbench.yaml, in VS Code's settings.json (`testbench schema > testbench.schema.json` saves a copy
that works offline; put its path in place of the URL):

```json
"yaml.schemas": {
  "https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/src/agent_testbench/schema/testbench.schema.json": "testbench.yaml"
}
```

The [VS Code extension](vscode.md) does this for every testbench.yaml with the schema of the version you have
installed, kept so that it works offline (a `$schema` first line still wins).

Under `expect:` the shipped schema allows any plain name besides the built-ins, since a check can come from a
plug-in package ([Check plug-ins](plugins.md#checks)). testbench itself refuses a name no installed check has when the
file loads, and `testbench schema` adds the installed plug-ins' own descriptions.

## Top level

| field | default | |
|---|---|---|
| `project` | the folder's name | letters, digits, `-`, `_`; names the Modal app and volume (`testbench-<project>`). Left out, it is the folder's name with every other character written as `-`, as `testbench init` writes it |
| `budget.max_run_usd` | `2.0` | refuse a run whose worst case is higher |
| `budget.ask_above_usd` | `0.5` | above this, `testbench run` needs `--approve` (a person's yes) |
| `budget.max_day_usd` | `10.0` | refuse a run that would take today's committed total over this |
| | | each budget value is capped by this machine's `~/.testbench/limits.yaml`, if set (see below) |
| `exclude` | `.git`, `.testbench`, `__pycache__`, `*.pyc`, `.venv`, `venv`, `node_modules`, ... | files left out of every copy of the project (remote backends, `workdir: copy`); setting it replaces this list. `.testbench`, `.env` and `.env.*` are always left out |
| `modal.python` | `"3.11"` | Python of the Modal image |
| `modal.pip` | `[]` | packages for the Modal image (pin them) |
| `modal.requirements` | none | a requirements file for the Modal image |
| `modal.apt` | `[]` | Debian packages (e.g. `ffmpeg`) |
| `modal.run_commands` | `[]` | extra image build commands |
| `modal.cpu`, `modal.memory_gb` | `2`, `8` | defaults for every Modal plan (a plan can override) |
| `colab.pip`, `colab.requirements` | none | installed on the VM with `colab install` |
| `colab.auth` | none | `adc` or `oauth2`, passed to the Colab CLI |
| `colab.cli` | `colab` | the Colab CLI executable |
| `pricing.modal_gpu_per_s` | built in | `{H100: 0.001097, ...}` US dollars per second |
| `pricing.modal_cpu_core_per_s`, `pricing.modal_mem_gib_per_s` | built in | |
| `pricing.colab_units_per_hour` | built in (approximate) | `{CPU: 0.1, T4: 1.8, A100: 5.4, ...}`; check `colab usage` |
| `pricing.colab_usd_per_unit` | `0.10` | |
| `sessions.backend` | `local` | where `testbench session start` puts its kernel by default: `local`, `docker`, `ssh`, `kubernetes`, `modal`, `colab` or a plugin's |
| `sessions.gpu` | none | default GPU for sessions on that backend |
| `sessions.idle_minutes` | `10` | a session stops itself after this long without use |
| `sessions.max_minutes` | `60` | and after this long in any case; its worst-case cost is this many minutes of its hardware (Modal: up to 1440) |
| `sessions.secrets` | `[]` | secret NAMES passed into every session |
| `sessions.cpu`, `sessions.memory_gb` | from `modal:` | Modal sessions only |
| `guard.ask` | `git push`, `gh repo create`, `gh pr merge`, `gh release create`, `testbench share` | commands the Claude Code hook asks about first (not with `--dry-run`) |
| `data.<role>.path` | none | the data the plans read, by role: a CSV file (or a folder of CSV files) with a time column, relative to the project; steps find it in `TESTBENCH_DATA_<ROLE>`. `data: {prices: data/prices.csv}` is the path alone ([sealed data](trading-data.md)) |
| `data.<role>.time` | the first of `time`, `date`, `timestamp`, `datetime` | its time column (ISO 8601 times) |
| `data.<role>.holdout_from` | none | rows at or after this time are sealed: every plan but the one with `holdout:` sees the file cut before it, in a copy of the project |

## Machine limits

`~/.testbench/limits.yaml` holds limits for every project on this machine. `testbench init` writes it when it is
missing; after that only you change it.

```yaml
max_run_usd: 5.00        # refuse any run or session that could cost more, in any project
ask_above_usd: 1.00      # above this, a person approves (--approve), in any project
max_day_usd: 20.00       # everything today, all projects together
```

A project's `budget:` can lower these limits, never raise them; a refusal says which limit it hit and where that
limit is set. Every launch and finish is written to the project's `.testbench/ledger.jsonl` and to
`~/.testbench/ledger.jsonl`, so the machine's day limit counts every project, including a new or deleted one. A
file that is not valid refuses every launch until it is fixed. In Claude Code the guard hook asks you before an
agent's edit raises a budget in testbench.yaml, changes its `pricing:` or `guard:`, or touches limits.yaml, and
refuses edits to the ledgers and commands that point testbench at another limits folder. This keeps an agent from
raising its own limits; it is not a sandbox against an agent set on getting around it.

## The ssh backend

```yaml
ssh:
  host: gpu-box                  # required: a Host from ~/.ssh/config, or user@host; `ssh HOST true` must work with keys
  usd_per_hour: 0.0              # what the machine costs you per hour while it works (0: your own); prices the worst case
  python: python3                # the machine's Python, 3.10 or newer
  workdir: ~/.testbench-remote   # where projects, sessions and runs go there
  setup: venv                    # venv: <workdir>/<project>/venv with testbench's basics and `pip:` (built once, kept);
  pip: [torch==2.5.1]            #   none: use `python` as it is, with what it has
  command: [ssh]                 # how to reach it: [ssh, -p, "2222"], [ssh, -J, bastion] ... (also a string)
```

Plans and sessions on `backend: ssh` run on that machine in a copy of the project (and of this package); a plan's
`gpu:` is a label there, and the report shows the GPU actually used. Commands go over one reused ssh connection
in BatchMode, so a host that asks for a password fails with a message instead of hanging. Secrets go over ssh's
stdin, never onto a command line. The hard stop is on the machine: the watchdog starts first, with the machine's
own Python, and ends everything the session or run started at its time limit (and a session after it sits idle),
even if this machine is asleep. A finished or cancelled run's folder there is removed once its evidence is back.
testbench bounds the work, not the machine: a rented instance that bills while it is on stays on.

## Backends

A backend is where plans and sessions run. Built in: `local`, `docker`, `ssh`, `kubernetes`, `modal`, `colab`
([docker.md](docker.md), [kubernetes.md](kubernetes.md)); `testbench doctor` lists every
backend this machine has and whether each is set up. Other backends come from installed packages: a package
declares an entry point in the `agent_testbench.backends` group that names an instance of
`agent_testbench.backends.Backend`, and its name can then be used as `backend:` in plans and sessions.

```toml
# pyproject.toml of the package that provides the backend
[project.entry-points."agent_testbench.backends"]
rack = "testbench_rack:backend"
```

```python
# testbench_rack.py
from agent_testbench.backends import Backend, split_gpu

class Rack(Backend):
    name = "rack"                                # what `backend:` names (else its entry point's name)
    gpus = ("RTX4090",)                          # what `gpu:` may name (NAME or NAME:COUNT)
    startup_minutes = 2                          # added to every worst case
    hard_stop = "a watchdog on the box ends the work at its time limit"   # what stops it there, in words
    config_defaults = {"rack": {"usd_per_hour": 0.6}}     # its section of testbench.yaml

    def rate_per_minute(self, cfg, hardware):    # US dollars per minute, so budget.check can bound the cost
        return cfg["rack"]["usd_per_hour"] / 60 * split_gpu(hardware.get("gpu"))[1]

    def plan_runner(self):                       # cls(root=, cfg=, run_dir=, plan=, meta=): run() -> (report, usd),
        return RackRunner                        #   cancel() -> str, cost(report) -> float

    def session_class(self):                     # cls(root, cfg, sdir, state): start, alive, exec, ls, get, put,
        return RackSession                       #   install, sync, shell, stop (see backends.Session)

    def cancel_remote(self, state):              # stop remote work from a run's state.json when its driver is gone
        return ""

    def doctor(self):                            # (what, ok, detail) lines for `testbench doctor`
        return []

backend = Rack()
```

Optional: `validate(cfg)` checks the backend's own section (called only when a plan or the sessions use it),
`max_session_minutes`, and `remote = False` for a backend on this machine (plans then run in the project folder
and no start-up is priced in). Every remote run and session must have a hard stop on the remote side, so the
worst case holds even if this machine goes offline; `hard_stop` says what it is. A backend that fails to load is
listed by `testbench doctor` and `testbench plugins`, and only breaks the projects that use it; one that takes a
built-in's name is skipped. `testbench plugin test testbench-rack` checks a backend keeps the backend contract
([plugins.md](plugins.md#the-backend-contract)), and
[examples/plugins/testbench-backend-example](../examples/plugins/testbench-backend-example) is a whole package.

## Live sessions

A session is one IPython kernel kept running on a machine, so an agent can work the way a person works in a
notebook: run code or cells, look at the values and plots, fix, run again, without losing state.

| backend | machine | project files | stops |
|---|---|---|---|
| `local` | this machine, this Python environment | the project folder itself | supervisor: idle / max minutes |
| `modal` | a Modal Sandbox (what Modal Notebooks run on) with the GPU you pick; image from `modal:` | copied to `/work/project` (also `/content`); `/testbench` is the project's volume (`$TESTBENCH_CACHE` = `/testbench/cache`) | Modal itself (lifetime = max minutes, idle timeout = idle minutes; a running command counts as activity) and the supervisor |
| `colab` | a Colab VM through Google's Colab CLI | copied to `/content` | the VM's own watchdog (max minutes + 10, idle minutes + 10; it releases the VM) and the supervisor (idle / max minutes) |
| `ssh` | any machine `ssh` reaches (keys, no password) | copied to `<ssh.workdir>/<project>/sessions/NAME/project` | the machine's own watchdog (max minutes + 2, idle minutes + 10) and the supervisor (idle / max minutes) |
| `docker` | a fresh container on this machine ([docker.md](docker.md)) | copied in with `docker cp` | a watchdog inside the container (max minutes + 2, idle minutes + 10) and the supervisor |
| `kubernetes` | a Job's pod on your cluster ([kubernetes.md](kubernetes.md)) | copied in with `kubectl exec` | the Job's `activeDeadlineSeconds`, a watchdog in the pod, and the supervisor |

```
testbench session start [NAME] [--backend B] [--gpu G] [--idle-minutes N] [--max-minutes N] [--secret NAME] [--approve]
testbench session list | stop [NAME|--all] | sync [NAME] | install PKG... [-s NAME] | shell "CMD" [-s NAME]
testbench exec [-s NAME] "code" | -f file.py | --cells nb.ipynb:3-5 [--params JSON]  [--timeout SECONDS] [--json]
testbench files [-s NAME] ls [PATH] | get REMOTE [LOCAL] | put LOCAL REMOTE
```

`exec` prints what the code printed, its last value (`=> ...`), images saved under
`.testbench/sessions/NAME/outputs/`, and for an error the exception, the line that raised it and the end of the
traceback. Exit codes: 0 ok, 1 error, 3 interrupted at `--timeout` (the kernel and its variables survive), 2
refused or no such session. Notebook cells are numbered among code cells from 1, with form fields set by
`--params`. `sync` copies the project again after local edits; variables are kept. Every session command also takes
the session as `-s NAME` or `--session NAME`, and with no name picks the only running one. The kernel has the
headless `google.colab` stand-in, so Colab notebooks run as they are.

Each session's start is checked against the budget like a plan (worst case = `max_minutes` of its hardware), and
its measured cost is written to the ledger when it stops. Its folder, `.testbench/sessions/NAME/`, holds
`state.json`, `exec.log` (every command and its outcome) and `outputs/`.

**The hard stop on a Colab VM.** Right after the VM is assigned, before anything is installed, kernelkit's
watchdog (standard library only) is copied to it and started. At the time limit - a session's `max_minutes`, or a
plan's total `max_minutes`, plus the 10 start-up minutes the estimate allows for - and, for a session, after
`idle_minutes` + 10 without a command reaching the VM, it ends everything testbench started there and asks Colab
to release the VM (what `google.colab.runtime.unassign()` does: a request to the VM's runtime service). `session
start` says which stop it got: if the VM has no runtime service to ask (no `TBE_RUNTIME_ADDR`), the work is still
ended, and Colab takes the idle VM back on its own schedule. The supervisor on this machine stops the session
sooner, at `idle_minutes` / `max_minutes`, while this machine is awake.

## Plans

```yaml
plans:
  <name>:
    backend: local | docker | ssh | kubernetes | modal | colab   # default local; or a plugin's
    gpu: T4                            # modal: T4 L4 A10 L40S A100-40GB A100-80GB H100 H200 B200 B300 RTX-PRO-6000
                                       #        (append :N for N GPUs, e.g. H100:2); colab: T4 L4 G4 A100 H100
    cpu: 4                             # modal only; overrides modal.cpu
    memory_gb: 16                      # modal only
    workdir: inplace | copy            # local default inplace (the project folder); remote always a copy
    description: what this plan proves
    holdout: true | {max_opens: 1}     # this plan opens the sealed rows of data: (one plan may); counted, max refused
    walk_forward: {data: prices, train: 24mo, test: 6mo, step: 6mo, anchored: false, start: 2020-01-01}
    lookahead: {data: prices, output: out/signals.csv, cuts: 4}   # rerun on data cut earlier; compare decisions
    model_cutoff: 2024-10-01 | {date: 2024-10-01, require_after: true}   # the LLM's training cutoff (your statement)
    steps: [...]
```

A plan runs its steps in order and stops at the first one that does not pass, unless that step has
`continue_on_failure: true`.

With `data:` ([sealed data](trading-data.md)): a plan of a project with sealed data, and any walk-forward plan, runs
in a copy (`workdir: inplace` is refused), where each sealed file holds only the rows before `holdout_from`; the
plan with `holdout:` sees every row, and its reports say how many times they were opened before. `walk_forward:`
runs the steps once per window (lengths: a whole number and `d`, `w`, `mo` or `y`; `step` defaults to `test`), each
with only the rows before its test end and `TESTBENCH_WINDOW`, `TESTBENCH_TRAIN_START` / `_END`,
`TESTBENCH_TEST_START` / `_END`; its worst case is the steps' minutes times the windows, cut from the data at launch
(fewer than 2 windows is refused there, exit 2). `lookahead:` ([look-ahead](trading-data.md#look-ahead)) runs the
steps once, then `cuts` more times (default 4, at most 20) with the role's data cut at earlier times (never in the
first 20% of rows), each from a fresh copy of the project, and fails the plan when a decision in `output` (a CSV
file: time, symbol, decision columns) dated before a cut differs from the plan's own run; its worst case is the
steps' minutes times 1 + `cuts`, and it is refused at load with `holdout:` or `walk_forward:`. `model_cutoff:`
([model cutoff](trading-data.md#model-cutoff)) warns in every report whose test periods start on or before that day
(each walk-forward window is marked), and with `require_after: true` fails a run with no test period after it.

Short forms, for plans that run on this machine (`backend: local`) with the default `max_minutes`. Each is read
as the long form it stands for, so runs, estimates, reports and plan.json are the same as if it were written out:

```yaml
plans:
  tests: "pytest -q"                          # one command: one step, named after the plan (tests)
  checks: ["ruff check .", "pytest -q"]       # commands in order: steps named ruff, then pytest
```

In any plan, a step may be a plain command; it is a `run:` step named after its program (`pytest` for
`pytest -q`, `python -m pytest` or `uv run pytest`; `train` for `python train.py`), with `-2`, `-3` added when
a name is taken:

```yaml
plans:
  gpu-check:
    backend: modal
    gpu: T4
    steps:
      - pytest -q                                          # a step named pytest
      - {name: train, run: python train.py, max_minutes: 30}
```

A plan or step written in a shape testbench does not take gets an error naming the field and showing each of
these forms on its own line.

## Steps

Exactly one of `notebook:`, `run:` or `web:` - or a plain command, which is a `run:` step named after its
program (see [Plans](#plans)). `params`, `cells`, `uploads` and `colab_shim` are for notebook steps; on another
step they are refused, not ignored.

| field | default | |
|---|---|---|
| `name` | required (a plain command is named for you) | unique within the plan |
| `notebook` | | an `.ipynb` or a percent-format `.py`; runs cell by cell in its kernel ([notebooks.md](notebooks.md)) |
| `run` | | a shell command (bash) |
| `web` | | see below |
| `max_minutes` | `10` | the step is stopped when it runs out; also bounds the cost estimate |
| `params` | `{}` | notebook parameters to set: `{EPOCHS: 2}` replaces the value on `EPOCHS = ...  #@param`, or sets a papermill parameter |
| `cells` | all | code cells to run, counted from 1: `"1-3,5"` or `[1, 2]` |
| `uploads` | `[]` | answers to `files.upload()`, one list of files per call: `[[data.csv], [a.mp4, b.mp4]]` |
| `colab_shim` | `true` | install the headless `google.colab` stand-in in the kernel |
| `env` | `{}` | extra environment variables |
| `secrets` | `[]` | secret NAMES. Local: must be exported. Modal: Modal secrets of these names are attached |
| `pythonpath` | `[]` | folders put first on `PYTHONPATH` (e.g. `mocks`) |
| `mocks` | `{}` | mock SDKs for the step: LLMs `[openai, anthropic]`, brokers `[alpaca]`, `[ccxt]`, `[ib_async]` (also `import ib_insync`), or with options ([mocks.md](mocks.md), [brokers](mocks.md#brokers)) |
| `artifacts` | `[]` | globs copied into the run folder as evidence |
| `expect` | `{}` | see below |
| `continue_on_failure` | `false` | keep going to the next step even if this one fails |

Inside a step: the working folder is the project (or its copy); `TESTBENCH_RUN_DIR`, `TESTBENCH_STEP_DIR`,
`TESTBENCH_PROJECT_DIR` are set; on Modal `TESTBENCH_CACHE` is a folder on the project's volume that survives
between runs (put downloaded weights there); `python` and `testbench` are the environment running agent-testbench;
`MPLBACKEND=Agg` for child processes.

## Expectations

The common ones are below; every expectation - `junit:`, `metrics:`, `numbers:`, `strategy:`, `gpu:`, `image:`,
`deterministic:`, `duration:`, tolerances (`"~= 0.93 +- 0.01"`) and ranges (`"between 0.8 and 0.95"`) - with what its failure looks
like is in [expectations.md](expectations.md).

```yaml
expect:
  exit_code: 0                          # run steps: default 0
  no_errors: true                       # notebook steps: default true
  files: [outputs/model.pt, "plots/*.png"]
  json:
    outputs/metrics.json:
      accuracy: ">= 0.9"                # operators: >= <= > < == !=, or a bare value for equality
      "history.loss.-1": "< 0.3"        # dotted paths; -1 is the last list item
  text:
    stdout: ["saved checkpoint"]        # the step's output, or a file path
  media:                                # needs ffprobe
    out/render.mp4: {frames: ">= 240", width: 1920, height: 1080, fps: "24/1", audio: true, seconds: ">= 10"}
  web:
    status: 200
    title: Counter
    text: ["Count: 2"]
    no_console_errors: true             # default true; page errors count too
```

A step passes when it ran without error and every expectation holds.

## Web steps

```yaml
web:
  serve: python -m http.server 8971 --bind 127.0.0.1 -d site    # optional; stopped afterwards
  url: http://127.0.0.1:8971/
  ready_timeout: 30                     # seconds to wait for the URL to answer
  viewport: [1280, 800]
  actions:
    - click: "#increment"
    - fill: {selector: "#name", text: "Ada"}
    - press: Enter
    - wait_for: "text=Saved"
    - wait_ms: 500
    - goto: http://127.0.0.1:8971/other
    - screenshot: after-save
  screenshot: true                      # full page at the end
```

Recorded: HTTP status, title, page text, console errors, page errors, failed requests, action errors and the
screenshots. If something already answers on the URL before `serve` starts, the step stops instead of
testing the wrong program.

## Loops

```
testbench loop start PLAN --metric FILE:KEY --budget-usd N [--goal max|min] [--min-delta X] [--max-hours H] [--approve] [--wait S]
                     [--target V] [--patience N] [--max-tries N] [--returns PATH] [--min-deflated-sharpe P]
testbench loop try "what changed" [--wait S]      # exit 0 kept, 1 reverted, 3 still running, 2 refused
testbench loop status                             # judges a finished try; exit 3 while one is running
testbench loop stop
```

- **Start** needs a git repository with no uncommitted changes. It creates the branch `testbench-loop/<id>`, then
  runs the plan on the code as it is: the baseline. `--metric` names a number one of the plan's json expectations
  checks (`FILE:KEY`, as in `expect: {json: {FILE: {KEY: ...}}}`), so it reads the same on every backend.
- **A try** commits every change in the project folder (not `.testbench/`, nor a new `.env` or `.env.*` file,
  which stays where it is: secrets go by name) and runs the plan in a scratch copy of
  that commit. It is kept when the run passes and the metric beats the best by more than `--min-delta` (higher for
  `--goal max`, lower for `min`). Otherwise the branch is reset to the best commit; edits made while the run went
  are stashed and put back on top. A failing baseline is fine: then the first passing try that reports the metric
  is kept.
- **Money and time**: `--budget-usd` bounds all the loop's runs together (finished runs at their measured cost, a
  running one at its worst case); a try with no room left is refused before anything is committed. Above
  `ask_above_usd`, a person approves the total once with `--approve`. Each run is still checked against
  `max_run_usd`, the day limits and this machine's limits. No new tries after `--max-hours`.
- **Stop** writes `.testbench/loops/<id>/report.md` (each kept change with what it moved and its run, each reverted
  try and why) and checks out the branch the loop started from. The kept commits stay on the loop's branch.
- Every verdict also goes to the lab notebook, `.testbench/NOTES.md`.
- **A strategy**: with each try's returns (`--returns out/equity.csv`, or the one `equity.csv` the plan keeps as
  evidence), every report says whether the best try is better than luck - its deflated Sharpe ratio over all tries,
  reverted ones included, and PBO - and `--min-deflated-sharpe 0.95` makes `--target` count only once the best try
  clears it ([Is the best try luck?](loops.md#is-the-best-try-luck)).

## Proof records

`report.json` follows a published JSON Schema (`src/agent_testbench/schema/report-v1.schema.json`, `schema_version:
1`; later versions only add fields). Besides the steps, checks and costs, it records:

| field | |
|---|---|
| `source` | at launch: `commit`, `branch`, `dirty` (uncommitted changes or untracked files were part of the run), `changes_sha256` (the diff against the commit and the untracked files), `untracked_files`; null outside git. `.testbench/` is not counted |
| `environment` | where the plan ran (recorded on that machine, Modal, Colab and ssh too): `python`, `platform`, `machine`, `packages` and `packages_sha256` (of the sorted name==version list) |
| `evidence`, `evidence_sha256` | every file under `steps/` with its size and sha256, and one sha256 over all of them |
| `worst_case_usd`, `approved`, `remote_stop` | the bound checked before launch, whether a person approved it, and the hard stop on the remote side |
| `data` | with `data:` in testbench.yaml, each role: `path`, `view` (`sealed`, `full`, `windows`), the `bytes`, `sha256`, `rows`, `first` and `last` time of what the steps saw, the whole `file` at launch and the `sealed` rows' (each step folder's `data.json` holds what its data was, as evidence) |
| `holdout` | a run of the plan that opens the holdout: `opened_before` (earlier openings of these sealed rows), `max_opens`, the `first` and `last` of them |
| `windows` | a walk-forward plan: each window's dates, train and test rows, outcome and data (and `after_cutoff` with `model_cutoff:`) |
| `lookahead` | a plan with `lookahead:`: the data's `first` and `last`, each rerun's cut (`before`), `data_rows`, `status`, `rows_compared`, `differences` and `first_difference`, the `verdict` (`clean`, `look-ahead`, `crashed`, `not checked`), `ok` and the `finding` in words |
| `model_cutoff` | a plan with `model_cutoff:`: the `date`, `require_after`, the test `periods` and whether each is after it, the `level` (`ok`, `warning`, `failed`) and the `finding` in words |

```
testbench verify [RUN]                     # exit 0: every evidence file matches; 1: what changed, went missing or was added
                                           #   (and with data:, 1 when a data file is not what the run read)
testbench report [RUN] --format html|junit [-o FILE]
testbench report [RUN] --tearsheet [-o FILE]   # trading: the tear sheet page alone (tearsheet.html in the run folder)
testbench history PLAN [-n N] [--json]     # each run's outcome and every number its checks found, oldest first
testbench ui [--open]                      # .testbench/ui/index.html: plans, trend lines, runs, loops, sessions
testbench share [RUN] --to DEST [--dry-run]   # the run as a page others open and check (docs/share.md)
testbench verify --from URL_OR_FOLDER      # a shared run, with no project: files, manifest and recorded hashes
testbench repro PLAN [--times N] [--on B]  # run it again; is the evidence the same, and if not, why (docs/reproducibility.md)
testbench repro --compare A B [C...]       # runs that exist: ids, run folders, shared bundles
```

## Prices

`testbench prices` lists every price estimates use and where it comes from: the table built into this version, a
fetched catalog, or the project's `pricing:`. `testbench prices --update [--url URL]` fetches `pricing/catalog.json`
from the repository into `~/.testbench/prices.json`. A catalog can raise a price above the built-in one or add
hardware this version does not know (a new GPU becomes allowed, priced at no less than the dearest hardware this
version knows); it never lowers a price - that comes with a release - so a stale or tampered catalog only makes
estimates more cautious. The guard hook asks before `--update --url` or an edit of `~/.testbench/prices.json`. A project's `pricing:` overrides
both. Nothing is fetched unless asked.

## Run folder

`.testbench/runs/<YYYYmmdd-HHMMSS-plan>/`:

```
state.json        status, process, remote handles (Modal call id / Colab session), cost
plan.json         the plan as run
progress.jsonl    one line per event
report.json       machine-readable report (rewritten after each step; schema/report-v1.schema.json)
report.md         the report
report.html       the report as one page with the evidence images (`testbench report --format html`, `testbench ui`)
tearsheet.html    a trading step's tear sheet alone (`testbench report --tearsheet`); report.html has it too
driver.log        the background driver's own output
steps/NN-name/    log.txt, result.json, executed.ipynb, cellNN_imageK.png, downloads/, artifacts/, screenshots,
                  data.json (with data:, what the step's data files held)
steps/lookahead/  with lookahead:, the decisions each run wrote: full.csv, cut-1.csv, ...
work/             on local, the copy of the project a `workdir: copy` plan runs in (sealed files cut)
```

`.testbench/ledger.jsonl` records every launch (worst case) and finish (estimated cost);
`.testbench/holdout.jsonl` every opening of the sealed holdout (run, time, commit, the data's sha256 and last time);
`.testbench/NOTES.md` is the lab notebook (`testbench note`), the one file there worth committing.
`.testbench/repro/<id>/` holds each `testbench repro`: `repro.json` (the comparison as data) and `repro.md` (in
words); `testbench ui` lists them.
`.testbench/check.json` is what `testbench check` found in a folder with no testbench.yaml, the plans `init`
would write there; commands in that folder read their plans from it (only plans that run on this machine, with
the default budget) until a testbench.yaml takes over.

## Commands

`testbench` alone (or `testbench help`) lists the core: the dozen commands in the first three groups below, with
the ones to start with. `testbench help --all` lists every command, in the groups of the rest of the table.
`testbench help COMMAND` (or `testbench COMMAND --help`) shows a command's options and examples. Options are spelled
out in full: `--appr` is not `--approve`. A mistyped command gets the closest one as a whole command line (`testbench
rnu smoke`: did you mean `testbench run smoke`?), words in the wrong order get the right order (`testbench smoke
run`: the command comes first), a mistyped plan, session, recipe, client or example name gets the closest one, and
so does a file that is not there (`testbench lint trian.ipynb`: did you mean `testbench lint train.ipynb`?). A value
an option cannot take gets an example of that option. `--json` and `--format json` are the same on every command
with a JSON form (`report`, `diff`, `repro`, `history`, `estimate`, `exec`, `rescue`, `check`, `plans`, `runs`,
`spend`, `loop status`); every session command takes `-s NAME` (also `--session NAME`). The JSON forms of `plans`, `runs`,
`spend` and `loop status` are what editors read ([the VS Code extension](vscode.md)): `plans --json` says for each
plan whether it may launch now (`allowed`, `needs_approval` or `refused`, with testbench's message), `runs --json`
gives each run's folder, and `loop status --json` only reads - it never keeps or reverts a try whose run has ended,
which `loop status` does.

| command | what it does | exit codes besides 2 |
|---|---|---|
| **start** | | |
| `demo [NAME] [DIR]`, `demo --list` | copy an example project to a new folder and run its free plan | 0 the plan did what the example expects (the `test-suite` plan fails on purpose), 1 it did not, 3 still running |
| `check [--plan NAME]... [--json] [--junit FILE]` | run every plan that is free and needs no approval, and list each other plan with its worst case and `testbench run NAME`; with no testbench.yaml, run the plans `init` would write for what it finds, on this machine, keeping what it found in `.testbench/check.json` (so `report`, `runs`, `verify`, `ui` and `diff` work there); a plan whose tool is missing is skipped with the command that installs it; `--junit` also writes JUnit XML for CI (each run's steps as test cases, a refusal as an error, a paid plan or a missing tool as skipped) | 0 every plan that ran passed, 1 one failed, 2 nothing to check, 3 still running |
| `doctor` | what this machine can run; each `no` line says what to do | 0, whatever it found |
| **prove it with a plan** | | |
| `plans [--json]` | the plans, their hardware and worst case (and whether each may launch now) | 0 |
| `estimate PLAN [--approve] [--json]` | a plan's worst case and whether it may launch now | 0 it may (2: it may not) |
| `run PLAN [--wait SECONDS] [--detach] [--approve] [--junit FILE]` | run a plan and print its report (`--junit`: also as JUnit XML, or why it did not start) | 0 passed, 1 failed, 3 still running after `--wait` (and always with `--detach`) |
| `report [RUN] [--format md\|json\|html\|junit] [-o FILE]` | a run's report | 0, a failed run's report too |
| `report [RUN] --tearsheet [-o FILE]` | only the [tear sheet](trading.md#tear-sheet) of the steps that kept an `equity.csv` among their artifacts: one page, to the run folder's `tearsheet.html` or FILE | 0; 1 no step kept one, or every one was refused (the reason is printed) |
| `verify [RUN] [--attestation [--repo OWNER/REPO]]` | check the evidence against its recorded sha256 | 0 it matches, 1 it does not (2: it could not be checked) |
| `verify --from URL_OR_FOLDER` | check a run `share` published, with no project: every file against its manifest, each evidence file against the sha256 the run recorded | 0 it matches, 1 it does not (2: it could not be checked) |
| `ui [--open] [--serve [--port N]]` | every run as a page, or the live dashboard | 0 |
| **work live, improve in a loop** | | |
| `session start [NAME] [--backend B] [--gpu G] ...` | start a kernel, inside the budget | 0 |
| `session list` | sessions, their hardware, uptime and cost | 0 |
| `session stop [NAME\|--all]` | stop one | 0, also when none was running |
| `session sync`, `install PKG...`, `shell "CMD"` | copy the project again, pip install, run a shell command | 0; `install`: 1 when pip fails; `shell`: 0 once the command ran (its own exit code is not passed on) |
| `exec CODE \| -f FILE \| --cells NB[:CELLS]` | run code in the session's kernel | 0 ok, 1 the code raised, 3 interrupted at `--timeout` (the kernel and its variables are kept) |
| `loop start PLAN --metric FILE:KEY --budget-usd N` | start a loop and run the baseline | 0 the baseline passed, 1 it did not (the loop still started), 3 still running |
| `loop try "WHAT"` | commit the edits as a try and run the plan | 0 kept, 1 reverted, 3 still running |
| `loop status [--json]`, `loop stop` | the tries so far (`--json`: as data, read-only); the report | 0; `status`: 3 while a try runs |
| **set up** (the rest: `testbench help --all`) | | |
| `init [--recipe NAME] [--add] [--force]` | write testbench.yaml (a plan for each thing it finds) and AGENTS.md | 0 |
| `recipes [NAME]` | ready plans for a goal, with their worst case; or print one | 0 |
| `schema` | print the JSON Schema of testbench.yaml (for editor autocomplete; `init` points the file at it) | 0 |
| `connect CLIENT... [--user] [--dry-run]`, `connect --list` | register the MCP server in a client's config and write AGENTS.md | 0 |
| `ci [--for gitlab\|azure\|circleci\|shell] [--plan NAME]... [-o FILE] [--force]` | a CI job that installs this version, runs `testbench check` (or `testbench run` for each `--plan`), publishes JUnit and keeps the runs; no `--for`: the templates ([ci.md](ci.md)) | 0 (2: an unknown CI or plan, or `-o` names a file that is there) |
| `selftest [--keep]` | check the main promises hold on this machine, in a temporary folder (a minute or so, local and free) | 0 they all held, 1 one did not |
| `mcp [--http]` | serve the commands as MCP tools, with resources (plans, runs, reports, evidence, the loop, notes) and prompts ([clients.md](clients.md#resources-and-prompts)) | 0 when it stops |
| `plugins [--json]` | each kind of plug-in: its built-ins, what is installed and from which package, what is broken and why ([plugins.md](plugins.md)) | 0 |
| `plugin test TARGET\|--installed [--kind KIND]` | the conformance kit: check plug-ins keep their kind's contract; TARGET is `module:attr` or an installed package's name | 0 they all do, 1 one does not |
| **runs** | | |
| `wait [RUN] [--timeout SECONDS]` | follow a run (`--wait` is the same as `--timeout`) | 0 passed, 1 failed, 3 still running |
| `status [RUN]` | one line about a run | 0 passed, 1 failed, cancelled, error or lost, 3 still running |
| `runs [-n N] [--json]` | recent runs | 0 |
| `cancel [RUN]` | stop a run, here and on its remote machine | 0, also when it had already ended (the message says how to read it instead) |
| `watch PLAN [--max-runs N]` | run a free plan again on every change | 0 stopped, or the last of `--max-runs` passed; 1 it did not |
| **proof** | | |
| `share [RUN] --to DEST [--dry-run] [--yes] [--keep-paths]` | publish a run's proof as a static page others open and check, in DEST/RUN/: a folder, `git:BRANCH/FOLDER` (committed, never pushed) or `s3://BUCKET/PREFIX` (the aws CLI); lists every file first and needs a person's yes ([share.md](share.md)) | 0 published or listed, 1 it failed or the evidence changed |
| `badge [PLAN] [-o SVG] [--link URL]` | a README badge of the plan's latest run: checks passed, failed, or no runs | 0 |
| `diff [A B \| PLAN \| RUN] [--json]` | what changed between two runs | 0 |
| `repro PLAN [--times N] [--on BACKEND]... [--approve] [--wait SECONDS] [--json]` | run a plan N times (default 2; each run in a fresh copy of the project, priced on the backend it goes to, the total checked and approved once) and compare the runs' evidence file by file: identical, differs (how, and why: times, paths, ids, order, encoding, rounding, a seed, the platform) or missing ([reproducibility.md](reproducibility.md)) | 0 the outputs are the same and every run passed, 1 they are not or a run did not pass, 3 a run is still going after `--wait` |
| `repro --compare RUN RUN [RUN...] [--json]` | the same comparison for runs that exist: ids, run folders, or bundles `share` made (a folder or URL) | 0 the outputs are the same and every run passed, 1 they are not or a run did not pass |
| `history PLAN [-n N] [--json]` | a plan's runs over time | 0 |
| `export [RUN\|all] [--to mlflow\|wandb\|csv\|jsonl]` | a run in a tracker or a file | 0, 1 a target failed |
| `numbers [READER [PATH...]] [--opt KEY=VALUE] [--json]` | the named numbers a metric reader (json, csv, junit, trading or an installed one) finds in evidence files, to write rules with; no READER: the readers and the files each reads ([metric readers](plugins.md#metrics)) | 0 (2: a file is missing or refused) |
| **sessions and notifications** | | |
| `files ls\|get\|put PATH...` | files on the session's machine | 0 |
| `notify test` | one test message to the `notify:` webhook | 0 accepted, 1 refused by the service |
| **money** | | |
| `spend [-n N] [--modal] [--json]` | what runs and sessions have cost (`--json`: today against the day limits) | 0 |
| `prices [--update]` | the prices behind every estimate | 0 |
| **notebooks and the lab notebook** | | |
| `lint NOTEBOOK... [--strict]` | Colab and headless pitfalls | 0; with `--strict`, 1 when anything is found |
| `rescue NOTEBOOK [--fix OUT] [--no-run]` | why a notebook does not run, and what to do | 0 it ran, 1 it failed or ran out of time (`--no-run`: something blocks it) |
| `nb split\|build\|fields` | a notebook as plain cell files | 0 |
| `note TEXT` | a line in `.testbench/NOTES.md` | 0 |

## Exit codes

Every command uses the same four, and the GitHub Action and the MCP server read them:

| code | means |
|---|---|
| `0` | passed, or done |
| `1` | failed: the run, the check or the code did not pass |
| `2` | refused or bad input: nothing ran and nothing was spent; the message says what was refused, why, and the command (or the file and field to edit) that fixes it |
| `3` | still running: `testbench wait RUN` follows it (for a loop's try, then `testbench loop status`) |

A command line testbench cannot read (an unknown command or option, a missing argument, a value that is not a
number) is `2` too, with the closest command, or the usage and an example of the option, and where the examples
are.
