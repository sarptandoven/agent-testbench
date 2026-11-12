# agent-testbench

[![tests](https://github.com/sarptandoven/agent-testbench/actions/workflows/ci.yml/badge.svg)](https://github.com/sarptandoven/agent-testbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-testbench.svg)](https://pypi.org/project/agent-testbench/)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/sarptandoven/agent-testbench/blob/main/LICENSE)

**A test bench your coding agent operates.** Claude Code, Codex, Cursor or any agent gets a live kernel on real
hardware - your machine, a Docker sandbox, any box you reach with ssh, a Modal GPU or a Colab VM - to run code
and notebook cells, look at the values and plots, fix and rerun. Then it proves the result with a clean, checked
run whose report names the code it ran and fingerprints its evidence. Every run is priced before it starts and
stopped on the machine itself at its limit, inside a budget the agent cannot raise.

```
$ testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0
BASELINE: accuracy 0.86, run 20261031-054550-smoke passed.

$ testbench loop try "hidden units 8 -> 64"        # the agent edited the code; testbench commits, runs, judges
KEPT try 2: accuracy 0.9689 (best was 0.8689, +0.1). Run 20261031-054609-smoke passed. Best is now 509b79c25.

$ testbench loop try "learning rate 0.05 -> 0.1"
REVERTED try 3: accuracy 0.9689 is not better than the best 0.9689. The branch is back at 509b79c25.
```

That is a real run: Claude Code, given only this plugin, took a model from 0.86 to 0.9756 accuracy in six tries,
four kept and two reverted by testbench, in 78 seconds - [docs/demo-loop.md](docs/demo-loop.md).

<img src="https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/docs/assets/report-pass.png" width="640"
alt="The HTML report of a fixed run: PASS, accuracy 0.1711 to 0.9756 since the failing run before it, every check that
was made, the code and Python it ran, the evidence hash, and the confusion matrix plot inline">

*`testbench report --format html` for the demo's fixed run - a real report, unedited ([the tour](docs/tour.md)).*

## Quick start

```bash
pip install "agent-testbench[all]"         # or: uv tool install "agent-testbench[all]"
testbench selftest                         # ten checks of the main promises on this machine, in under a minute
testbench demo                             # a small project, run for free here - see it work in a minute
testbench demo llm-app                     # or web-app, test-suite: your kind of project (testbench demo --list)
cd your-project && testbench init          # looks at the project and writes plans that already check something
testbench run <plan>                       # the report: what passed, what failed and why, what it cost
```

`testbench init` finds notebooks, a pytest suite, training scripts and web apps, and writes a plan for each -
lint, run, the files they write, every test passing, a page with no console errors - with a `FILL IN` line for
the number only you know ("accuracy >= 0.9"). Starting from a goal instead? `testbench recipes` lists ready
ladders of plans - pytest, a Colab notebook, PyTorch training, a LoRA fine-tune, a web app, an LLM eval, a Docker
sandbox, your own GPU box, a data pipeline - with each plan's worst case; `testbench init --recipe pytorch-train`
writes one, and `--add` adds one to the testbench.yaml you have ([docs/recipes.md](docs/recipes.md)). Not only
Python: `init` turns `npm test` (jest, vitest, mocha, node --test, Playwright), a Go module or a Rust crate into a
plan judged by the runner's own JUnit report, and the jest, vitest, go-test and cargo-test recipes also run the
suite in a Docker container with no network.

**Claude Code** - the plugin adds the skills and a guard hook for spending and secrets:

```bash
claude plugin marketplace add sarptandoven/agent-testbench
claude plugin install agent-testbench@agent-testbench
```

Then ask in plain words: *"get training working and prove it with the smoke plan"*, or *"use testbench loop to
raise accuracy, budget $2"*.

**Cursor, VS Code, Codex, Gemini CLI**: `testbench connect cursor` (or `vscode`, `codex`, `gemini`, `claude`;
`--user` for every project, `--list` to see what is set up) registers the MCP server in the client's own config
and writes the working rules into AGENTS.md; [docs/clients.md](docs/clients.md) says which guardrails hold
outside Claude Code.

**Any other MCP client** (nothing to install with [uv](https://docs.astral.sh/uv/)):

```json
{ "mcpServers": { "agent-testbench": { "command": "uvx", "args": ["--from", "agent-testbench[all]", "testbench", "mcp"] } } }
```

**In CI**: `uses: sarptandoven/agent-testbench@v1` with `plan: smoke`; `comment: true` puts the verdict and the
numbers' change since the base branch on the pull request, and `attest: true` signs the report so
`testbench verify --attestation` shows which workflow and commit made it ([docs/github-action.md](docs/github-action.md)).

**Your own agent or a notebook**: `from agent_testbench import Testbench` runs plans and live sessions from Python,
and `agent_testbench.tools` gives the same tools to the Anthropic or OpenAI API (`tools.anthropic_tools()`,
`tools.call(name, input)`), inside the same budget, approvals and machine limits
([docs/python-api.md](docs/python-api.md), [examples/agent-loop](examples/agent-loop)).

**Any other agent** reads the working rules `testbench init` writes into `AGENTS.md`.

## What your agent can do with it

- **Work live** in a kernel that stays up: run code, `.py` files or a notebook's cells (Colab form fields set), get
  back what it printed, the last value, plots as images, and errors with the line that raised them. Install
  packages, run shell commands, move files, push local edits without losing variables.
- **Prove it** with a plan: notebooks, scripts or web UIs run end to end, checked against expectations - metric
  thresholds and tolerances, the project's own test suite, numbers in the log, files, video frames, a page's text,
  GPU use, a plot that should look like a known-good one. The report leads with what failed and says what changed
  since the last run.
- **Rescue a notebook** that no longer runs: `testbench rescue nb.ipynb` reads it and runs it top to bottom, then
  says which cell fails and why - a missing package and its pip name, cells that only worked in another order, a
  Colab-only path, a file downloaded by hand, a GPU this machine lacks - with a plan that proves it once fixed.
- **Test LLM apps for free**: `mocks: [openai, anthropic]` on a step swaps in strict mock SDKs - no key, no network,
  the same answer every time, and the SDK's own `BadRequestError` for a request the real API would refuse.
- **Improve it** with a loop: each idea is committed, proved by a run, and kept only if the number got better.
- **Stay inside a budget**: worst case priced before every launch, approvals above a threshold, machine-wide limits
  a project cannot raise, and a hard stop on the machine doing the work.
- **Show it**: reports as HTML pages with the evidence inline, JUnit for CI, history and trend lines, and proof
  records anyone can check with `testbench verify`.

## Where it runs

| backend | the machine | hard stop | |
|---|---|---|---|
| `local` | this one, in the project folder | this machine's supervisor | free |
| `docker` | a fresh container here; `network: none` for a sandbox, `gpus: all` for NVIDIA GPUs | a watchdog inside the container | [docs/docker.md](docs/docker.md) |
| `ssh` | any machine `ssh` reaches: a GPU box, a university server, a rented instance | a watchdog on that machine | [reference](docs/reference.md#the-ssh-backend) |
| `kubernetes` | a Job on your cluster's GPU or CPU nodes, through kubectl | the Job's `activeDeadlineSeconds`, and a watchdog in the pod | [docs/kubernetes.md](docs/kubernetes.md) |
| `modal` | a [Modal](https://modal.com) Sandbox or function with any GPU (T4 ... B200) | Modal's own timeouts | your Modal account |
| `colab` | a [Colab](https://colab.research.google.com) VM through Google's Colab CLI | a watchdog on the VM, which releases it | your compute units |
| plugins | anything a package registers (`agent_testbench.backends` entry point) | the backend's own (`testbench plugin test` checks it has one) | [docs/plugins.md](docs/plugins.md) |

Each is set per plan (`backend:`) or per session (`--backend`), so the same notebook can be proved for free
locally, then on the target GPU for a few cents. Linux, macOS and Windows (Colab's CLI has no Windows build).

## Live sessions

```
$ testbench session start gpu --backend modal --gpu T4 --idle-minutes 3 --max-minutes 8
gpu  ready on modal T4  up 0.0 min, idle 0.0 min (stops after 3 idle / 8 total)  ~$0.000 so far
Within budget: up to $0.16 (today so far $0.01 of $2.00).

$ testbench exec -f check_gpu.py
[check_gpu.py | ok | 3.3 s]
20 matmuls of 4096x4096 on Tesla T4: 0.67 s
=> torch.Size([4096, 4096])

$ testbench exec "float(b.mean()), torch.cuda.max_memory_allocated() // 2**20"      # variables persist
[code | ok | 0.2 s]
=> (1023.1211547851562, 200)

$ testbench session stop
gpu stopped after 1.4 min, about $0.017
```

Also: `exec --cells nb.ipynb:1-4 --params '{"EPOCHS": 1}'`, `files ls|get|put`, `session sync|install|shell|list`.
A call past `--timeout` is interrupted and the kernel keeps its variables; a session stops itself when idle.

## Plans and expectations

```yaml
plans:
  smoke:                               # cheapest proof first: here, free
    backend: local
    steps:
      - name: tests
        run: python -m pytest -q --junitxml=junit.xml
        expect:
          junit: {path: junit.xml, failures: 0, tests: ">= 10"}       # names each failing test
      - name: train quick
        notebook: train.ipynb
        params: {EPOCHS: 1}                                           # Colab form fields
        expect:
          json: {outputs/metrics.json: {accuracy: "~= 0.93 +- 0.02"}}
          metrics: {stdout: {"val_loss=([0-9.]+)": "< 0.3"}}          # a number printed to the log
          image: {outputs/confusion.png: {like: golden/confusion.png, max_diff: 0.05}}
  gpu-check:                           # a few minutes on the target GPU before a long run
    backend: modal
    gpu: A10
    steps:
      - name: train short
        notebook: train.ipynb
        max_minutes: 10                # also the hard stop, and what the worst case is priced on
        expect:
          gpu: {busy_pct: ">= 50"}     # an idle GPU fails the step
```

Steps are `notebook:` (an `.ipynb` or a percent-format `.py`; `params:` sets Colab form fields and papermill
parameters - [docs/notebooks.md](docs/notebooks.md)), `run:` (a shell command) or `web:` (a page driven in headless
Chromium: click, fill, wait, screenshot). `mocks: [openai, anthropic]` gives a step mock LLM SDKs, and
`mock_calls: {anthropic: {calls: "<= 5"}}` bounds what the code asked for ([docs/mocks.md](docs/mocks.md)). Every expectation and what its failure looks like: [docs/expectations.md](docs/expectations.md). Every
field of testbench.yaml: [docs/reference.md](docs/reference.md).

```
$ testbench run smoke                  # after a bad edit to the data cell (abridged)
# FAIL - plan `smoke` on local

Run `20261031-064120-smoke`, started 2026-10-31 06:41, 0.05 min, about $0.000. Stopped because step 2 (train quick) failed.

## FAIL: train quick

- **cell 2**: `AxisError`: axis 2 is out of bounds for array of dimension 2
- raised at cell line 10: X_train_mean = X.mean(axis=2) (raised in _methods.py:83, _count_reduce_items)

## Compared with the previous run `20261031-064113-smoke` (passed)

- Broke: train quick
```

## Proof records

Each report names what it proves: the git commit it ran (and a hash of any uncommitted changes), the Python,
platform and packages where it ran, and the sha256 of every evidence file, in a format published as a
[JSON Schema](src/agent_testbench/schema/report-v1.schema.json). `testbench verify` says whether the evidence still
matches, and `testbench diff A B` says what changed between two runs - each checked number old -> new and whether
that is better by its rule, checks that flipped, evidence that changed, the commits, packages that changed version.
`testbench watch smoke` reruns a free plan whenever you save, one run at a time. `report --format html` writes one page with the plots inline, `--format junit` feeds CI test reports,
`testbench history <plan>` shows how the checked numbers moved, and `testbench ui` writes static pages for every
run with a trend line per number; `testbench ui --serve` shows them live at http://127.0.0.1:8770/ - what is
running, its log as it grows, sessions, the loop, and today's spend against your limits - read-only and for this
machine only ([docs/dashboard.md](docs/dashboard.md)). `testbench export --to mlflow|wandb|csv|jsonl` (or `export:` in testbench.yaml,
after every run) puts runs where you already track experiments, with the evidence attached
([docs/export.md](docs/export.md)).

## Loops

```bash
testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 5 --max-hours 8
testbench loop try "lower the learning rate to 3e-4"     # commit, run, keep or revert
testbench loop status | stop
```

The loop works on a branch of its own. A try is kept only when its run passes and the metric beats the best so
far; otherwise the branch goes back to the best commit, keeping edits made meanwhile. `--metric` is a number a json
check reads (`FILE:KEY`) or any check's number (`check:val_loss`). One budget covers every run of the loop, approved
once; each run is still checked against the per-run and daily limits. The report lists each kept change, what it
moved and the run that proves it; merging is yours to decide.

Left to run, a loop stops itself: `--target 0.95` (done), `--patience 4` (four tries in a row not kept) or
`--max-tries 20` refuse the next try and say what to do instead. With `notify: {webhook_env: TESTBENCH_WEBHOOK}`
in testbench.yaml you hear when runs and loops end, or when a launch waits for your yes - a Slack webhook or any
JSON POST ([docs/loops.md](docs/loops.md), [docs/notify.md](docs/notify.md)).

## Guardrails

- **Money**: worst case = hardware price x time limit (+ start-up), checked before every run, session and loop.
  Over `max_run_usd` or a day's limit: refused. Over `ask_above_usd`: a person approves - in Claude Code the hook
  asks you, in MCP clients the server asks you directly (elicitation). `~/.testbench/limits.yaml` caps every project
  on the machine; a project can lower those limits, never raise them, and they hold whatever was approved. Prices
  come from a built-in table that a fetched catalog can raise, never lower (`testbench prices`). Anything that is
  not a real number - a NaN budget, an infinite price, a broken ledger line - refuses the launch.
- **Hard stops** on the machine doing the work: Modal's timeouts, or kernelkit's watchdog on Colab VMs, ssh machines
  and in containers - so the bound holds with your laptop asleep. Ssh and docker runs are also stopped from this
  machine shortly after their limit, in case the work there stopped its watchdog.
- **Secrets** by name only, never on a command line; strict mocks stand in for paid APIs in tests.
- **The guard hook** (Claude Code) refuses raw cloud launches and commands that would print secrets, asks before
  an edit raises a budget, lowers a price or loosens a docker sandbox, turns every `--approve` into a question to
  you, and refuses edits to the ledgers. It is a safety net, not a sandbox (a script the agent writes can still
  pass `--approve`; the machine's limits still hold) - for running code nobody reviewed, use `backend: docker`
  with `network: none`.

## MCP and agents

Tools: `session_start`, `session_exec`, `session_files`, `session_sync`, `session_install`, `session_shell`,
`session_list`, `session_stop`, `plans`, `run_plan`, `wait_run`, `get_report`, `list_runs`, `cancel_run`, `spend`,
`lint_notebook`, `rescue_notebook`, `recipes`, `export_run`, `diff_runs`, `add_note`, `loop_start`, `loop_try`, `loop_status`,
`loop_stop`. They run side by side (stop a
session while a long cell runs), return plots and screenshots as images, and pass on progress. `testbench mcp --http`
serves streamable HTTP on 127.0.0.1. In Claude Code: `claude mcp add agent-testbench -- testbench mcp`, or the
plugin, which adds the `iterate` and `setup` skills.

## Commands

`testbench --help` shows the core, a dozen commands:

| | |
|---|---|
| `testbench demo` / `check` / `doctor` | start: an example project, a first run of yours, what this machine can run |
| `testbench plans` / `estimate` / `run` / `report [--format html\|junit]` / `verify [--attestation]` / `ui [--serve]` | prove it with a plan: its worst case, the run, its report and evidence, every run as a page |
| `testbench session start/list/stop/...` / `exec` / `loop start/try/status/stop` | work live in a kernel, improve in a loop |

The rest, for setup, money, notebooks and more (`testbench help --all` lists every command in groups):

| | |
|---|---|
| `testbench init [--recipe]` / `recipes` / `schema` / `connect` / `selftest` / `mcp [--http]` / `plugins` / `plugin test` | set up a project and your agents, editor autocomplete for testbench.yaml, check the promises hold here, serve MCP, list plug-ins and check one keeps its contract |
| `testbench wait` / `status` / `runs` / `cancel` / `watch` | follow, list and stop runs; run a free plan again on every change |
| `testbench diff` / `history` / `export` | what changed between runs, the checked numbers over time, MLflow / W&B / csv / jsonl |
| `testbench files` / `notify test` | files on a session's machine; a test message to the `notify:` webhook |
| `testbench spend [--modal]` / `prices [--update]` | money |
| `testbench lint` / `rescue` / `nb split/build/fields` / `note` | notebooks and the lab notebook |

`testbench help COMMAND` shows a command's options and examples; exit codes are in
[docs/reference.md](docs/reference.md#exit-codes) (0 passed, 1 failed, 2 refused or bad input, 3 still running). A
refusal (exit 2) says what was refused, why, and the command or the testbench.yaml field that fixes it.

## Documentation

- [docs/reference.md](docs/reference.md) - every field of testbench.yaml, sessions, plans, steps, loops, proof
  records, prices, the ssh backend, machine limits, writing a backend plugin
- [docs/plugins.md](docs/plugins.md) - plug-ins of every kind: how a package adds one, and the kit that checks it
- [docs/expectations.md](docs/expectations.md) - every expectation, with what its failure looks like
- [docs/recipes.md](docs/recipes.md), [docs/notebooks.md](docs/notebooks.md), [docs/rescue.md](docs/rescue.md),
  [docs/mocks.md](docs/mocks.md) - starting points, what notebook steps accept, rescuing notebooks, mock LLM SDKs
- [docs/docker.md](docs/docker.md), [docs/kubernetes.md](docs/kubernetes.md) - the docker and kubernetes backends
- [docs/loops.md](docs/loops.md), [docs/notify.md](docs/notify.md), [docs/export.md](docs/export.md) - loops,
  notifications, exports to MLflow and W&B
- [docs/clients.md](docs/clients.md), [docs/python-api.md](docs/python-api.md) - other coding agents, the Python API
  and LLM tool definitions
- [docs/tour.md](docs/tour.md) - a five-minute tour with real screenshots, from install to a proved fix
- [docs/examples.md](docs/examples.md) - the examples: an LLM app on a mock SDK, a web form in headless Chromium, a
  failing test suite for your agent to fix, a Colab-style notebook
- [docs/why.md](docs/why.md), [docs/use-cases.md](docs/use-cases.md), [docs/faq.md](docs/faq.md),
  [docs/troubleshooting.md](docs/troubleshooting.md)
- [docs/github-action.md](docs/github-action.md) - the GitHub Action: inputs, outputs, what reviewers see
- [docs/demo-loop.md](docs/demo-loop.md), [docs/demo-sandbox.md](docs/demo-sandbox.md) and
  [docs/demo.md](docs/demo.md) - real agent runs, with what they did
- [examples/](examples) - a Colab-style notebook with plans for every backend, and a web UI check

## Install notes

- **Modal**: `modal setup` once (your account; [pricing](https://modal.com/pricing)).
- **Colab**: `uv tool install google-colab-cli`, then log in once (`colab new`). Uses your compute units.
- **ssh**: `ssh HOST true` must work with keys; `ssh: {host: HOST, usd_per_hour: 0}` in testbench.yaml.
- **Docker**: a running Docker (or podman); `docker: {network: none}` for no network.
- **Kubernetes**: kubectl with a context for your cluster; `kubernetes: {usd_per_hour: {cpu: 0, A100: 2.5}}`.
- **Trackers**: `pip install "agent-testbench[mlflow]"` or `[wandb]` for exports.
- **Web steps**: `playwright install chromium`.

`testbench doctor` shows what this machine can run.

## How it is tested

`python -m pytest` runs everything that needs no account - notebooks in real kernels, expectations, budgets and
limits, the guard hook, the MCP server over stdio and HTTP, loops on real git repositories, the ssh backend through
a stand-in for ssh, Docker in real containers, web steps in Chromium - on Linux and macOS with Python 3.10 and
3.12, and on Windows. `tests/test_live.py` runs the same things on Modal, Colab or a real ssh host and is opt-in.
The demos in `docs/` are real agent runs.

## Limits

- Costs are estimates (time x price); `testbench spend --modal` reads Modal's actual bill; Colab unit rates vary by
  account (`colab usage`).
- It runs `.ipynb` files and live kernels; it does not drive the Colab or Modal Notebooks web pages.
- Colab VMs can be reclaimed after an hour or two even while busy; use Modal or ssh for long jobs.
- On Windows, interrupting a cell that is blocked inside a C call (`time.sleep`, a long native call) waits until
  that call returns; Python code is interrupted at once.

## License

MIT

<!-- mcp-name: io.github.sarptandoven/agent-testbench -->
