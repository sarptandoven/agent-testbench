# agent-testbench

[![tests](https://github.com/sarptandoven/agent-testbench/actions/workflows/ci.yml/badge.svg)](https://github.com/sarptandoven/agent-testbench/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agent-testbench.svg)](https://pypi.org/project/agent-testbench/)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/sarptandoven/agent-testbench/blob/main/LICENSE)

**A test bench your coding agent operates.** Claude, GPT/Codex or any agent can start a live kernel on a Modal GPU,
a Colab VM or your machine, run code and notebook cells in it, inspect variables and plots, install packages,
move files, fix and rerun - and then prove the result with a clean, checked run. Everything stays inside a
budget you set.

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

## What your agent can do with it

- **Work live** in a kernel that stays up - on this machine, in a [Modal](https://modal.com) Sandbox with any
  GPU (the machinery behind Modal Notebooks), or on a [Colab](https://colab.research.google.com) VM through
  Google's Colab CLI. Run code, `.py` files, or a notebook's cells with its Colab form fields set; get back what
  it printed, the last value, plots as image files, and errors with the line that raised them. Install
  packages, run shell commands (`nvidia-smi`), list, upload and download files, and push local edits with
  `sync` without losing variables.
- **Prove with plans**: run notebooks (Colab ones included), scripts or web UIs end to end on any of the same
  backends, checked against expectations - metric thresholds, files, video frame counts, text on a page - with
  a report that names the failing cell and line, shows the evidence, and says what changed since the last run.
- **Stay inside a budget**: every session and run is priced before it starts (worst case = its time limit on
  its hardware), refused above your cap, sent to you for approval above your threshold, and stopped by the
  provider at its limit even if your laptop goes to sleep. Sessions stop themselves when idle.
- **Stay safe**: secrets are referred to by name only, paid APIs get strict mocks in tests, and the Claude Code
  hook blocks raw cloud launches and commands that would print secrets.

## Install

```bash
pip install "agent-testbench[all]"     # or: uv tool install "agent-testbench[all]"
```

The latest from GitHub: `pip install "agent-testbench[all] @ git+https://github.com/sarptandoven/agent-testbench"`.

- **Modal**: `modal setup` once (your account, your card; [pricing](https://modal.com/pricing)).
- **Colab**: `uv tool install google-colab-cli`, then log in once (`colab new` opens the sign-in; or set
  `colab: {auth: adc}` if you use gcloud Application Default Credentials). Uses your Colab compute units.
- **Web UI checks**: `playwright install chromium`.

`testbench doctor` shows what this machine can do. Then, in your project:

```bash
testbench init          # writes testbench.yaml (budget, session defaults, a plan per notebook) and AGENTS.md
```

## Connect your agent

**Claude Code** - the plugin adds the skills and the guard hook:

```bash
claude plugin marketplace add sarptandoven/agent-testbench
claude plugin install agent-testbench@agent-testbench
```

Then ask in plain words ("get training working on a T4 and prove it with the smoke plan"), or call the skills:
`/agent-testbench:iterate` and `/agent-testbench:setup`.

**Claude Desktop, Cursor, Codex and other MCP clients**:

```json
{ "mcpServers": { "agent-testbench": { "command": "testbench", "args": ["mcp"] } } }
```

Tools: `session_start`, `session_exec`, `session_files`, `session_sync`, `session_install`, `session_shell`,
`session_list`, `session_stop`, `plans`, `run_plan`, `wait_run`, `get_report`, `list_runs`, `cancel_run`,
`spend`, `lint_notebook`, `add_note`. In Claude Code: `claude mcp add agent-testbench -- testbench mcp`.

**Any other agent** reads the rules `testbench init` writes into `AGENTS.md`.

## Live sessions

```bash
testbench session start                                     # default from testbench.yaml (local unless set)
testbench session start big --backend modal --gpu A100-80GB --idle-minutes 10 --max-minutes 90
testbench session start nb --backend colab --gpu T4

testbench exec "x = load_data(); x.shape"                   # code
testbench exec -f debug/check.py                            # a local file
testbench exec --cells train.ipynb:1-4 --params '{"EPOCHS": 1}'   # notebook cells, form fields set
testbench files get outputs/sample.png                      # ls / get / put on the session's machine
testbench session sync                                      # copy local edits again; variables stay
testbench session install "transformers==4.46.3"
testbench session shell "nvidia-smi"
testbench session list                                      # uptime, idle time, cost so far
testbench session stop                                      # or let it stop itself
```

| backend | where the kernel runs | stops |
|---|---|---|
| `local` | this machine, in the project folder | after `idle_minutes` unused or `max_minutes` |
| `modal` | a Modal Sandbox with your GPU (T4 ... H100, B200), project in `/work/project` and `/content`, a persistent volume at `/testbench` | Modal enforces both limits itself (a running command counts as activity), so it stops even if your laptop is off |
| `colab` | a Colab VM (CPU, T4, L4, A100, H100 by subscription), project in `/content` | after `idle_minutes` unused or `max_minutes` (Colab may also reclaim VMs after an hour or two) |

A call that runs past `--timeout` is interrupted; the kernel and its variables survive. One command runs at a
time per session.

**GPU libraries on Modal**: the default image is Debian slim with the NVIDIA driver but no CUDA toolkit.
PyTorch works as it is (its wheels bring their own CUDA libraries - checked on a T4). Libraries that expect the
CUDA toolkit on the machine, such as CuPy, fail there with errors like
`libcurand.so.10: cannot open shared object file` (also checked). Give them the CUDA libraries they need as
described in their install guide (for CuPy: docs.cupy.dev, "Installation"), and confirm it in a short GPU
session (`testbench session start --backend modal --gpu T4 --max-minutes 5`) before a long run.

Notebooks see a headless `google.colab` (uploads from files you name, downloads kept,
secrets from the environment, Drive as a local folder), so Colab notebooks run as they are.

## Plans

```yaml
project: my-model                 # names the Modal app and volume

budget:
  max_run_usd: 2.00               # refuse any run or session that could cost more
  ask_above_usd: 0.50             # above this a person approves (--approve)
  max_day_usd: 10.00              # everything today together

sessions:
  backend: modal
  gpu: A10
  idle_minutes: 10
  max_minutes: 60

modal:                            # image for Modal plans and sessions
  python: "3.11"
  pip: [torch==2.5.1, scikit-learn==1.5.2]
  cpu: 4
  memory_gb: 16

plans:
  smoke:                          # cheapest proof first
    backend: local
    steps:
      - name: lint
        run: testbench lint train.ipynb --strict
        max_minutes: 1
      - name: train tiny
        notebook: train.ipynb
        params: {EPOCHS: 1, SUBSET: 200}     # Colab form fields (NAME = value  #@param)
        max_minutes: 5
        expect:
          json: {outputs/metrics.json: {loss: "< 2.0"}}

  gpu-check:                      # a few minutes on the target GPU before a long run
    backend: modal
    gpu: A10
    steps:
      - name: train short
        notebook: train.ipynb
        params: {EPOCHS: 2}
        max_minutes: 10           # also the hard stop on Modal's side
        artifacts: [outputs/*.png]
        expect:
          json: {outputs/metrics.json: {accuracy: ">= 0.8"}}
```

```bash
testbench plans                   # every plan, its hardware and worst-case cost
testbench run smoke               # runs it and prints the report (follows up to 9 min; then `testbench wait`)
testbench report                  # the latest report
```

A report leads with what failed: the cell, the error, the line that raised it, each expectation that missed and
what was found instead, then what changed since the previous run of the plan ("Fixed: train", "Broke: eval",
"accuracy 0.71 -> 0.93"), the evidence files to look at, and GPU use per step (an idle GPU shows as 0%).
Steps are `notebook:`, `run:` (a shell command) or `web:` (a page driven in headless Chromium: click, fill,
press, wait for, screenshot). The full reference is in [docs/reference.md](https://github.com/sarptandoven/agent-testbench/blob/main/docs/reference.md).

## Guardrails

- **Budget**: worst case = hardware price x time limit (+ start-up). Over `max_run_usd` or the day's cap:
  refused. Over `ask_above_usd`: needs `--approve`, which the agent is told never to pass itself - and in Claude
  Code the hook turns it into a question to you. Every finished run and session is written to a ledger;
  `testbench spend --modal` also reads Modal's own bill.
- **Hard stops**: plans and sessions on Modal carry their limits in Modal itself (function and Sandbox
  timeouts, no retries, 2-second scale-down). Colab sessions and runs are stopped by their supervisor and always
  released, even when a run fails.
- **Secrets**: by name only; values come from Modal secrets or your environment. The hook refuses commands that
  would print them.
- **Paid APIs**: test with strict mocks (`agent_testbench.mocks.StrictAPI`): a module with the real client's
  name that checks arguments against the published schema and refuses anything unexpected.
- **Outside world**: the hook asks before `git push`, `gh repo create` and anything in `guard.ask`.

The hook is a safety net for agents, not a security boundary; the budget checks and remote time limits are
enforced in code either way.

## Notebooks

- `testbench nb split nb.ipynb cells/` and `testbench nb build cells/ nb.ipynb` - edit cells as plain files.
- `testbench nb fields nb.ipynb` - the form fields a plan or `--params` can set.
- `testbench lint nb.ipynb` - big saved outputs, embedded video, secrets without a fallback, `files.upload()`,
  unpinned installs, the inline matplotlib backend leaking into child processes, broken form fields, hard-coded
  `/content` paths.

## Commands

| | |
|---|---|
| `testbench session start/list/stop/sync/install/shell` | live kernels |
| `testbench exec` / `testbench files` | work in a session |
| `testbench plans` / `run` / `wait` / `report` / `runs` / `cancel` | plans |
| `testbench estimate <plan>` / `spend [--modal]` | money |
| `testbench lint` / `nb split/build/fields` | notebooks |
| `testbench note "..."` | the project's lab notebook (`.testbench/NOTES.md`) |
| `testbench init` / `doctor` / `mcp` | set up, check, serve MCP |

## Examples

- [`examples/tiny-model`](https://github.com/sarptandoven/agent-testbench/tree/main/examples/tiny-model): a Colab-style notebook that trains a small network, with plans
  for local, Modal CPU, Modal T4 and Colab.
  [Open in Colab](https://colab.research.google.com/github/sarptandoven/agent-testbench/blob/main/examples/tiny-model/tiny_model.ipynb)
- [`examples/web-ui`](https://github.com/sarptandoven/agent-testbench/tree/main/examples/web-ui): a page checked in a headless browser - clicks, text, console errors, screenshots.

## How it is tested

`python -m pytest` runs everything that needs no account: config, notebooks executed in real kernels (errors,
timeouts, images, magics, the Colab stand-in), expectations, budgets, lint, mocks, the generated Modal app, full
CLI runs (pass, fail, fix, compare, cancel, timeouts, refusals), live sessions on this machine (persistent
state, interrupts, notebook cells, files, auto-stop, budgets), the guard hook, the MCP server over stdio, and web
steps in headless Chromium. It passes on Python 3.11 and 3.12.

`tests/test_live.py` runs the same things on the real services and is opt-in because it costs money:
`TESTBENCH_TEST_MODAL=1` (plans on CPU and a T4, cancelling a GPU run mid-flight, a live session, a T4 session)
and `TESTBENCH_TEST_COLAB=1` (a plan and a live session on a Colab CPU VM).

[docs/demo.md](https://github.com/sarptandoven/agent-testbench/blob/main/docs/demo.md) shows Claude Code, given only this plugin and a project with a planted bug, finding
and fixing it and proving the fix with a run.

## Limits

- Costs are estimates (time x published price); `testbench spend --modal` reads Modal's actual bill. Colab
  unit rates vary by account - put yours (`colab usage`) under `pricing: colab_units_per_hour:`.
- It runs `.ipynb` files and live kernels; it does not drive the Colab or Modal Notebooks web pages themselves
  (Modal Notebooks have no API; for steering a Colab tab in your browser see Google's
  [colab-mcp](https://github.com/googlecolab/colab-mcp)).
- Colab VMs can be reclaimed after an hour or two even while busy; use Modal for long jobs.
- macOS and Linux. Windows is untested (the Colab CLI does not support it either).

## License

MIT
