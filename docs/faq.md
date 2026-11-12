# FAQ

## Cost

### What does agent-testbench cost?

The package is free (MIT). What costs money is the hardware a plan or session runs on, and the coding agent's own
tokens, which testbench does not see or bill.

| backend | what testbench counts | where the price comes from |
|---|---|---|
| `local` | nothing | free |
| `docker`, `ssh` | `usd_per_hour` while the work runs | what you write in testbench.yaml (0 for your own machine) |
| `modal` | GPU, CPU cores and memory per second | Modal's published rates, built in |
| `colab` | compute units per hour, times `colab_usd_per_unit` | approximate rates, built in; `colab usage` shows your account's |

`testbench prices` lists every price and where it comes from; a project's `pricing:` overrides them. Costs in
reports are estimates (time times price); `testbench spend --modal` reads Modal's actual bill for the project.

### How is spending bounded?

Before every run, session and loop, testbench prices the worst case: the hardware's rate times the time limit
(the steps' `max_minutes`, or a session's `--max-minutes`), plus start-up minutes for a remote machine. Then:

- over `budget.max_run_usd`, or over what is left of `max_day_usd` today: **refused**, whatever anyone approves;
- over `ask_above_usd`: **refused unless a person approves** (`--approve`);
- `~/.testbench/limits.yaml` caps every project on the machine; a project can lower those limits, never raise them;
- the machine doing the work stops at the limit on its own: Modal's timeouts, or kernelkit's watchdog on a Colab VM,
  an ssh machine or inside a container.

Real messages, from a plan on a machine priced at $3/h:

```
$ testbench run long
Refused: plan long could cost up to $3.10 (60 min at $0.0500/min), over budget.max_run_usd $2.00. Lower the max_minutes of plan long's steps in testbench.yaml (38 min in all would fit), use a cheaper GPU or backend, or have a person raise budget.max_run_usd in testbench.yaml. `testbench estimate long` checks it again.
$ testbench run check
Needs approval: plan check could cost up to $0.60 (above budget.ask_above_usd $0.50). Ask a person; only they approve it, with `testbench run check --approve`.
```

A loop takes one `--budget-usd` for all its runs together, approved once if it is above the threshold; each run
is still checked against the per-run and daily limits.

### What happens if my laptop goes to sleep mid-run?

Remote work stops at its limit anyway, because the stop is on the remote side: Modal ends the function or Sandbox
at its timeout, and the watchdog on a Colab VM, an ssh machine or in a container ends what testbench started there
(and asks Colab to release the VM). A local run's driver is a background process on your machine, so it sleeps
with it. testbench bounds the work, not the machine: a rented instance you reach with ssh keeps billing while it
is on.

## Safety and control

### What can an agent do with it, and what can it not?

**It can** start and stop live sessions and run code, files and notebook cells in them; install packages, run
shell commands and move files on a session's machine; run plans, read reports, verify evidence and see history;
run loops inside the budget you gave; and edit the code, the notebooks and the plans, like any coding agent.

**It cannot, whatever it types** (testbench refuses in code, on every client):

- launch a run, session or loop whose worst case is over `max_run_usd`, or over what is left of a day's limit
  (the project's, or this machine's for all projects together);
- get past `~/.testbench/limits.yaml` by editing a project's `budget:` - a project can only lower those limits;
- keep a remote run or session going past its time limit (the stop is on the machine doing the work);
- decide a loop's verdict: testbench keeps a try only if its run passes and the metric beats the best.

**It is told not to, and in Claude Code the plugin's guard hook enforces it:**

- pass `--approve` (or `approved_by_person=true`) itself - the hook turns it into a question to you;
- raise `budget:` values or change `pricing:` or `guard:` in testbench.yaml - the hook asks you first (lowering a
  budget or editing plans does not);
- edit `~/.testbench/limits.yaml` (the hook asks) or a ledger (refused), or point testbench at another limits
  folder (refused);
- run `modal run`, `colab new` and the like directly in a project (refused);
- print secrets: `printenv`, `cat .env`, `echo $..._KEY` (refused);
- `git push`, `gh pr merge` and whatever else `guard.ask` lists (the hook asks).

The rules come from the `iterate` skill, or the `AGENTS.md` section `testbench init` writes. Outside Claude Code
they are instructions, not enforcement: testbench cannot tell who typed `--approve` on a command line. MCP clients
that support elicitation ask you directly when a launch needs approval; otherwise the tool says what needs
approval, and `approved_by_person=true` is a flag the agent is told to set only after you said yes. Code that a
session or plan runs can do anything your user can on that machine, including read a secret you passed into it.

What the hook decides, for a few commands (from feeding it the JSON Claude Code sends):

```
--- Bash: modal run train.py
deny: agent-testbench guard: in this project, cloud runs go through `testbench run <plan>`, which enforces the budget in testbench.yaml and keeps the evidence. Add or edit a plan instead; if a raw command is really needed, ask the person to run it.
--- Bash: testbench run smoke --approve
ask: agent-testbench: a run with --approve spends money above the approval threshold. Plan smoke on local can cost up to $0.00 (6 min). Approve only if you agreed to this cost.
--- Edit: raise budget.max_run_usd 0.50 -> 5.00
ask: agent-testbench: this edit of testbench.yaml changes its limits: budget.max_run_usd raised from $0.5 to $5. Approve only if you want to allow that spending.
--- Edit: lower budget.max_run_usd 0.50 -> 0.25
(no decision: allowed as usual)
```

### Is it a sandbox?

Only when you choose one. `local` runs in your project folder with your permissions, and so does anything an
agent runs there. The guard hook keeps an agent from changing its own limits; it is a safety net, not a sandbox.
For code nobody has reviewed, use `backend: docker` with `docker: {network: none}`: a fresh container per run, a
copy of the project, no network, optional CPU and memory limits, deleted afterwards. A container shares this
machine's kernel; for code you have reason to think is hostile, use a VM or gVisor
([what a container isolates, and what it does not](docker.md#what-it-isolates-and-what-it-does-not)).

## Privacy

### What leaves my machine?

Only what you send to a backend you chose, and the price catalog if you ask for it. testbench has no telemetry.

| | what goes there |
|---|---|
| `local` | nothing |
| `docker` | a copy of the project (minus `exclude:`) and of the testbench package, into a container on the Docker daemon you use (this machine, unless `DOCKER_HOST` points elsewhere) |
| `ssh` | a copy of the project (minus `exclude:`) and of the package, to the host, over ssh |
| `modal` | the project (minus `exclude:`) and the package, in your Modal account: added to a plan's image, or uploaded to a session's Sandbox; run folders on the project's Modal volume; Modal secrets you name are attached |
| `colab` | the project (minus `exclude:`), copied to the VM through Google's Colab CLI |
| `testbench prices --update` | one request for `pricing/catalog.json` from this repository on GitHub; nothing is fetched unless you run it |
| `testbench mcp --http` | listens on 127.0.0.1 unless you pass `--host` |

Two things to check:

- **`exclude:` replaces the default list** (`.git`, `.testbench`, `__pycache__`, `*.pyc`, `.venv`, `venv`,
  `node_modules`, `.ipynb_checkpoints`, `.DS_Store`); it does not add to it. `.env` is not in the defaults. If the
  project folder holds a `.env`, credentials or data that must not leave, write the whole list:
  `exclude: [.git, .testbench, __pycache__, "*.pyc", .venv, venv, node_modules, .ipynb_checkpoints, .DS_Store, .env]`.
- **Secrets go by name.** `secrets: [NAME]` on a step or `--secret NAME` on a session passes the value from your
  environment (or, on Modal, the Modal secret of that name) without putting it on a command line: docker reads it
  from testbench's environment, ssh sends it over stdin. Run folders keep logs as written, so code that prints a
  secret puts it in the evidence; never print one.

## Platforms and agents

### Does it work on Windows and macOS?

Yes: Linux, macOS and Windows, Python 3.10 or newer. The test suite runs on Linux and macOS with Python 3.10 and
3.12, and on Windows with 3.12. Differences:

- Colab's CLI has no Windows build, so the `colab` backend is not available there.
- On Windows, interrupting a cell blocked inside a C call (`time.sleep`, a long native call) waits until the call
  returns; Python code is interrupted at once.
- `run:` steps use bash: on Windows, Git Bash's (not WSL's), or the system shell when there is no bash.
- The GitHub Action's own tests run on Linux and macOS runners; its steps are bash, but it has not been run on a
  Windows runner yet.
- The `docker` backend needs a running Docker (Docker Desktop on macOS and Windows); the tests run it in real
  containers where a daemon answers.

### Which coding agents does it work with?

- **Claude Code**: the plugin adds the `iterate` and `setup` skills and the guard hook
  (`claude plugin marketplace add sarptandoven/agent-testbench`, then
  `claude plugin install agent-testbench@agent-testbench`). Or only the MCP server:
  `claude mcp add agent-testbench -- testbench mcp`.
- **Cursor, Codex, Claude Desktop or any MCP client**: `testbench mcp` serves the commands as tools over stdio (or
  `--http`). The config is in the [quick start](../README.md#quick-start).
- **Any other agent that can run commands**: the CLI, and the working rules `testbench init` writes into
  `AGENTS.md` (with `CLAUDE.md` pointing to it).

The demos on this site were run with Claude Code. Other agents use the same CLI and MCP tools, but no run with them
is shown here.

### Do I need git?

Not for runs: outside a repository the report says "code outside git". Loops need a git repository with no
uncommitted changes, because they commit each try and reset to the best one. In a repository, the report names
the commit and hashes any uncommitted changes.

### Which notebooks and languages?

Notebooks run in a Python (IPython) kernel, cell by cell, with Colab form fields and a headless `google.colab`
stand-in. `run:` steps are any shell command, so a test suite in any language counts if it writes a JUnit report
(`junit:` reads pytest, jest-junit, go-junit-report, Maven Surefire and most others). `web:` steps drive any page
that answers on a URL.

### Does it drive the Colab or Modal Notebooks web pages?

No. It runs `.ipynb` files and live kernels: on Colab through Google's Colab CLI, on Modal in a Sandbox (what
Modal Notebooks run on). Colab VMs can be reclaimed after an hour or two even while busy; use Modal or ssh for
long jobs.

## More

- Something went wrong: [troubleshooting](troubleshooting.md).
- Every field of testbench.yaml: [reference](reference.md).
