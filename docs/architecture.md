# How it fits together

agent-testbench is a small core with layers around it. The core is what it takes to prove a plan on this machine;
everything else either reaches the core from outside (a way in), plugs a kind of machine, check or output into it
(a plug-in), or builds on runs (a layer).

```text
ways in     the CLI; the MCP server (tools, resources, prompts) and the clients `testbench connect` sets up; the
   |        Claude Code and Cursor plugins; the Python API and its tools for the Anthropic and OpenAI APIs and
   |        agent frameworks; the VS Code extension; the GitHub Action and `testbench ci` jobs; the dashboard
   v
core        load testbench.yaml, price a plan and check the budget, launch a run and drive it on this
   |        machine, check its expectations, write and verify its proof record
   v
plug-ins    seven kinds, each one table of built-ins and installed packages: backends (machines), checks
            (expect:), mocks (stand-in SDKs), sinks (export:), notifiers (notify:), recipes, metric readers
```

**The core** is these modules of `src/agent_testbench/`: `config` (testbench.yaml), `budget` and `prices` (worst
cases, limits, ledgers), `backends/__init__` (the `Backend` interface and registry) and `backends/local` (this
machine), `runs` and `driver` (launching, following and closing out a run), `executor` (a plan's steps and their
evidence), `expect` (the checks), `metrics` (evidence files read as named numbers), `provenance` and `report` (the
proof record), `procs` (background processes and lock files on every system), `plugins` (finds, loads and hands out
plug-ins of every kind), and `core` itself. `agent_testbench.core` is its public face: thin functions to load a
project, list and estimate plans, launch a run, wait for it, read its report and verify its evidence - the same code
the CLI runs, so the same budget checks and the same records.

```python
from agent_testbench import core

root = core.find_root("path/to/project")
cfg = core.load(root)
core.estimate(cfg, "smoke")                    # {worst_case_usd, ..., allowed, message}
run_dir = core.launch(root, cfg, "smoke")      # over a limit: core.LaunchRefused, which says why
core.wait(run_dir)                             # its state.json once it ends
core.report(run_dir)["status"]                 # 'passed'
core.verify(run_dir)                           # []: every evidence file still matches its recorded sha256
```

## Ways in

Every way in launches through the core, so a run started from an editor, a CI job or an agent framework is priced,
limited and recorded exactly like one typed at the command line.

- **The CLI** (`cli`): the commands, grouped in `testbench help --all`.
- **The MCP server** (`mcp_server`, `mcp_resources`): the commands as tools, the project's plans, runs, reports and
  notes as resources, and the common jobs as prompts. `connect` writes its entry into the config of Claude Code,
  Cursor, VS Code, Codex or Gemini CLI, and the working rules into AGENTS.md ([other coding agents](clients.md)).
- **The Claude Code and Cursor plugins** (`.claude-plugin/`, `.cursor-plugin/`, `skills/`, `hooks/`): the
  `iterate` and `setup` skills, and in Claude Code the guard hook.
- **Python** (`api`, `tools`): `Testbench` for plans, runs and sessions, and the MCP server's own tool table as
  tool definitions for the Anthropic and OpenAI APIs and as the OpenAI Agents SDK's, LangChain's and the Claude Agent
  SDK's tools ([Python API](python-api.md)).
- **The VS Code extension** (`editors/vscode/`): reads `plans --json`, `runs --json`, `spend --json` and
  `loop status --json`, and runs plans through the CLI ([VS Code](vscode.md)).
- **CI**: the GitHub Action (`action.yml`, `github_action`) runs a plan and turns the run into a job summary, a
  check, a pull request comment and an attestation; `testbench ci` (`ci/`) writes a job for GitLab, Azure
  Pipelines, CircleCI or any shell that runs `testbench check` ([GitHub Action](github-action.md), [CI](ci.md)).
- **Pages**: `ui` (static pages for every run) and `dashboard` (`testbench ui --serve`: live, read-only, this
  machine only).

## Plug-ins

A plug-in is a package that adds to one of seven kinds: a backend, a check, a mock, a sink, a notifier, a recipe or
a metric reader. Each kind is one table that holds the built-ins and the installed plug-ins alike, behind one small
interface, so a plug-in can do anything a built-in does; `testbench plugin test` checks it keeps its kind's contract
([Plug-ins](plugins.md)). (The Claude Code and Cursor plugins above are something else: how those editors install
testbench's skills and hook.)

The built-in machines other than this one are layers that plug in the same way: `backends/modal_backend`,
`colab_backend`, `ssh_backend`, `docker_backend`, `kubernetes_backend` and `runpod_backend`, with live sessions in `sessions`,
`session_backends` and `kernelkit/` (the launcher and watchdog that run on the session's machine).

## Layers

**Everything else is a layer**: kinds of step (`notebook`, `web`, `mocks`), what happens after a run (`notify`,
`export`), a run's proof for others (`share`, `badge`), work built on runs (`check`, `loop`, `watch`, `diff`,
`rescue`) and setting a project up (`scaffold`, `recipes`, `lint`, `selftest`). `core.CORE_MODULES` and
`core.LAYER_MODULES` are the two lists in code.

**The trading pack** is plug-ins of those kinds plus one layer, not a product of its own:

- a metric reader, `trading` (`trading/metrics.py`), computes a strategy's numbers - Sharpe, drawdown, trades -
  from the standard files a backtest writes (`trading/files.py`), so a strategy never grades itself;
- a check, `strategy:` (`trading/checks.py`), puts rules on those numbers with the pack's guardrails;
- mocks, `alpaca`, `ccxt` and `ib_async`, answer broker SDKs from a prices file and refuse a live account;
- recipes - `trading-backtest`, `vectorbt-strategy`, `freqtrade-strategy`, `alpaca-paper` - are the ladder from
  backtest to paper;
- the loop's overfitting statistics (`trading/stats.py`: deflated Sharpe ratio, PBO) and the tear sheet
  (`trading/tearsheet.py`, `testbench report --tearsheet`);
- the layer is the data seal (`dataseal`): `data:` names the data plans read, with a sealed holdout, walk-forward
  windows, `lookahead:` and `model_cutoff:`. Config, launch and the executor reach it only for a testbench.yaml
  with `data:` ([sealed data](trading-data.md)).

## The rule

A core module never imports a layer, or a package beyond the standard library and PyYAML, at module level. A layer
may import the core; the core reaches a layer only inside the function that needs it - a notebook step loads the
notebook machinery (nbclient, a kernel), a web step Playwright, a finished run its notifications and exports. So
`import agent_testbench.core` loads only the standard library and PyYAML (120 to 160 modules, depending on the
Python version), and a plan of plain `run:` steps is priced, run, checked and verified without nbformat, nbclient,
the mock clients or any optional package. `tests/test_core_boundary.py` checks all of it.

The CLI keeps to the same idea: `testbench --version` and `testbench --help` load no PyYAML, no notebook machinery
and no core module, and each command loads only what it uses (`tests/test_cli_ux.py` checks the first part).

This is the base for plug-ins: a plug-in builds on the core and never needs the CLI or the MCP server loaded.
