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

## Live sessions

A session is one IPython kernel kept running on a machine, so an agent can work the way a person works in a
notebook: run code or cells, look at the values and plots, fix, run again, without losing state.

| backend | machine | project files | stops |
|---|---|---|---|
| `local` | this machine, this Python environment | the project folder itself | supervisor: idle / max minutes |
| `modal` | a Modal Sandbox (what Modal Notebooks run on) with the GPU you pick; image from `modal:` | copied to `/work/project` (also `/content`); `/testbench` is the project's volume (`$TESTBENCH_CACHE` = `/testbench/cache`) | Modal itself (lifetime = max minutes, idle timeout = idle minutes; a running command counts as activity) and the supervisor |
| `colab` | a Colab VM through Google's Colab CLI | copied to `/content` | supervisor: idle / max minutes |

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

## Run folder

`.testbench/runs/<YYYYmmdd-HHMMSS-plan>/`:

```
state.json        status, process, remote handles (Modal call id / Colab session), cost
plan.json         the plan as run
progress.jsonl    one line per event
report.json       machine-readable report (rewritten after each step)
report.md         the report
driver.log        the background driver's own output
steps/NN-name/    log.txt, result.json, executed.ipynb, cellNN_imageK.png, downloads/, artifacts/, screenshots
```

`.testbench/ledger.jsonl` records every launch (worst case) and finish (estimated cost);
`.testbench/NOTES.md` is the lab notebook (`testbench note`), the one file there worth committing.
