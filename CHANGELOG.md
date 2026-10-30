# Changelog

## Unreleased

- Backends are plugins: each one (local, Modal, Colab, or a package with an `agent_testbench.backends` entry point)
  owns its GPUs, prices, config section, remote cancel and `doctor` checks. `testbench doctor` lists the
  backends and any that failed to load. No change for existing projects. See docs/reference.md, "Backends".
- Colab sessions and plans get a hard stop on the VM itself: a watchdog started before anything is installed ends
  the work and releases the VM at the time limit (and after a session sits idle), even if this machine is asleep.
  Before, only the supervisor on this machine stopped a Colab session. Colab estimates now allow 10 start-up
  minutes instead of 5, so they still bound the cost. `session start` says which hard stop the session got.
- Limits an agent cannot raise: `~/.testbench/limits.yaml` (written by `testbench init` when missing) caps every
  project on the machine, and a machine-wide ledger makes its day limit count all projects together. The Claude
  Code hook now also watches file edits: it asks before an edit raises a budget or changes `pricing:`/`guard:` in
  testbench.yaml or touches limits.yaml, and refuses edits to ledgers. `testbench plans` and `spend` show the
  limits in force.
- MCP server: each tool runs in its own process, so tools run side by side (stop a session or check a run while a
  long exec or run is going; an exec ends at once when its session stops, instead of waiting out its timeout).
  `session_exec` and `get_report` return images; `run_plan`/`wait_run` pass on progress; a launch above the
  approval threshold asks the person directly through MCP elicitation where the client supports it;
  `testbench mcp --http` serves streamable HTTP. Launches take a machine-wide lock around the budget check, so
  launches at the same moment cannot overspend a day limit together.
- `testbench loop start/try/status/stop` (and MCP `loop_start`, `loop_try`, `loop_status`, `loop_stop`): an improvement
  loop the agent drives and testbench judges. Each try is committed on the loop's own branch and proved by a plan
  run; it is kept only if the run passes and the metric beats the best, otherwise reverted. One budget bounds the
  whole loop, approved once; the report lists each kept change, what it moved and the run that proves it.

## 0.1.2 - 2026-10-07

- Installs on Intel Macs: the `mcp`/`all` extras keep `cryptography` below 49 there (49+ ships no Intel-Mac wheels).
- README: a quick start for Claude Code, MCP clients (no install with `uvx`) and the command line.

## 0.1.1 - 2026-10-07

- Corrected the package summary and the MCP server's instructions.

## 0.1.0 - 2026-10-07

First release.

- Live sessions: a kernel that stays up on this machine, a Modal Sandbox with any GPU, or a Colab VM - run code,
  files or notebook cells, inspect, install, move files, sync; stops itself when idle and at a time limit.
- Plans: notebooks, scripts and web UIs run end to end on local, Modal or Colab, checked against expectations,
  with reports that compare against the previous run.
- Budgets and guardrails, a Claude Code plugin (skills + guard hook), an MCP server, examples and tests.
