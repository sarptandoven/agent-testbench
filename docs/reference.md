# testbench.yaml reference

Paths are relative to the folder holding `testbench.yaml`. Every validation error names the field and says what
to write instead.

## Top level

| field | default | |
|---|---|---|
| `project` | required | letters, digits, `-`, `_`; names the Modal app and volume (`testbench-<project>`) |
| `budget.max_run_usd` | `2.0` | refuse a run whose worst case is higher |
| `budget.ask_above_usd` | `0.5` | above this, `testbench run` needs `--approve` (a person's yes) |
| `budget.max_day_usd` | `10.0` | refuse a run that would take today's committed total over this |
| | | each budget value is capped by this machine's `~/.testbench/limits.yaml`, if set (see below) |
| `exclude` | `.git`, `.testbench`, `__pycache__`, `*.pyc`, `.venv`, `venv`, `node_modules`, ... | files never shipped to Modal or Colab |
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
| `sessions.backend` | `local` | where `testbench session start` puts its kernel by default: `local`, `modal` or `colab` |
| `sessions.gpu` | none | default GPU for sessions on that backend |
| `sessions.idle_minutes` | `10` | a session stops itself after this long without use |
| `sessions.max_minutes` | `60` | and after this long in any case; its worst-case cost is this many minutes of its hardware (Modal: up to 1440) |
| `sessions.secrets` | `[]` | secret NAMES passed into every session |
| `sessions.cpu`, `sessions.memory_gb` | from `modal:` | Modal sessions only |
| `guard.ask` | `git push`, `gh repo create`, `gh pr merge`, `gh release create` | commands the Claude Code hook asks about first |

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

A backend is where plans and sessions run. Built in: `local`, `modal`, `colab`; `testbench doctor` lists every
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
    gpus = ("RTX4090",)                          # what `gpu:` may name (NAME or NAME:COUNT)
    startup_minutes = 2                          # added to every worst case
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
worst case holds even if this machine goes offline. A backend that fails to load is listed by `testbench doctor`
and only breaks the projects that use it.

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
`--params`. `sync` copies the project again after local edits; variables are kept. The kernel has the headless
`google.colab` stand-in, so Colab notebooks run as they are.

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
    backend: local | modal | colab     # default local
    gpu: T4                            # modal: T4 L4 A10 L40S A100-40GB A100-80GB H100 H200 B200 B300 RTX-PRO-6000
                                       #        (append :N for N GPUs, e.g. H100:2); colab: T4 L4 G4 A100 H100
    cpu: 4                             # modal only; overrides modal.cpu
    memory_gb: 16                      # modal only
    workdir: inplace | copy            # local default inplace (the project folder); remote always a copy
    description: what this plan proves
    steps: [...]
```

A plan runs its steps in order and stops at the first one that does not pass, unless that step has
`continue_on_failure: true`.

## Steps

Exactly one of `notebook:`, `run:` or `web:`.

| field | default | |
|---|---|---|
| `name` | required | unique within the plan |
| `notebook` | | an `.ipynb`; runs cell by cell in an IPython kernel |
| `run` | | a shell command (bash) |
| `web` | | see below |
| `max_minutes` | `10` | the step is stopped when it runs out; also bounds the cost estimate |
| `params` | `{}` | notebook form fields to set: `{EPOCHS: 2}` replaces the value on `EPOCHS = ...  #@param` |
| `cells` | all | code cells to run, counted from 1: `"1-3,5"` or `[1, 2]` |
| `uploads` | `[]` | answers to `files.upload()`, one list of files per call: `[[data.csv], [a.mp4, b.mp4]]` |
| `colab_shim` | `true` | install the headless `google.colab` stand-in in the kernel |
| `env` | `{}` | extra environment variables |
| `secrets` | `[]` | secret NAMES. Local: must be exported. Modal: Modal secrets of these names are attached |
| `pythonpath` | `[]` | folders put first on `PYTHONPATH` (e.g. `mocks`) |
| `artifacts` | `[]` | globs copied into the run folder as evidence |
| `expect` | `{}` | see below |
| `continue_on_failure` | `false` | keep going to the next step even if this one fails |

Inside a step: the working folder is the project (or its copy); `TESTBENCH_RUN_DIR`, `TESTBENCH_STEP_DIR`,
`TESTBENCH_PROJECT_DIR` are set; on Modal `TESTBENCH_CACHE` is a folder on the project's volume that survives
between runs (put downloaded weights there); `python` and `testbench` are the environment running agent-testbench;
`MPLBACKEND=Agg` for child processes.

## Expectations

The common ones are below; every expectation - `junit:`, `metrics:`, `gpu:`, `image:`, `duration:`, tolerances
(`"~= 0.93 +- 0.01"`) and ranges (`"between 0.8 and 0.95"`) - with what its failure looks like is in
[expectations.md](expectations.md).

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
testbench loop try "what changed" [--wait S]      # exit 0 kept, 1 reverted, 3 still running, 2 refused
testbench loop status                             # judges a finished try; exit 3 while one is running
testbench loop stop
```

- **Start** needs a git repository with no uncommitted changes. It creates the branch `testbench-loop/<id>`, then
  runs the plan on the code as it is: the baseline. `--metric` names a number one of the plan's json expectations
  checks (`FILE:KEY`, as in `expect: {json: {FILE: {KEY: ...}}}`), so it reads the same on every backend.
- **A try** commits every change in the project folder (not `.testbench/`) and runs the plan in a scratch copy of
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

## Proof records

`report.json` follows a published JSON Schema (`src/agent_testbench/schema/report-v1.schema.json`, `schema_version:
1`; later versions only add fields). Besides the steps, checks and costs, it records:

| field | |
|---|---|
| `source` | at launch: `commit`, `branch`, `dirty` (uncommitted changes or untracked files were part of the run), `changes_sha256` (the diff against the commit and the untracked files), `untracked_files`; null outside git. `.testbench/` is not counted |
| `environment` | where the plan ran (recorded on that machine, Modal, Colab and ssh too): `python`, `platform`, `machine`, `packages` and `packages_sha256` (of the sorted name==version list) |
| `evidence`, `evidence_sha256` | every file under `steps/` with its size and sha256, and one sha256 over all of them |
| `worst_case_usd`, `approved`, `remote_stop` | the bound checked before launch, whether a person approved it, and the hard stop on the remote side |

```
testbench verify [RUN]                     # exit 0: every evidence file matches; 1: what changed, went missing or was added
testbench report [RUN] --format html|junit [-o FILE]
testbench history PLAN [-n N] [--json]     # each run's outcome and every number its checks found, oldest first
testbench ui [--open]                      # .testbench/ui/index.html: plans, trend lines, runs, loops, sessions
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
driver.log        the background driver's own output
steps/NN-name/    log.txt, result.json, executed.ipynb, cellNN_imageK.png, downloads/, artifacts/, screenshots
```

`.testbench/ledger.jsonl` records every launch (worst case) and finish (estimated cost);
`.testbench/NOTES.md` is the lab notebook (`testbench note`), the one file there worth committing.
