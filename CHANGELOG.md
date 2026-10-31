# Changelog

## 0.2.0 - not yet released

- `backend: docker`: plans and live sessions in a fresh container on this machine. The project is copied in, nothing
  is mounted, and evidence comes back owned by you; `network: none` gives a sandbox with no network, `gpus:` passes
  NVIDIA GPUs through. The hard stop is kernelkit's watchdog inside the container. See docs/docker.md.
- Expectations for real projects: `junit:` reads a test suite's JUnit XML (pytest, jest, go, ...) and names the
  failing tests; `metrics:` checks numbers printed to the log or a file; `gpu:` fails a step that left the GPU idle;
  `image:` compares a plot with a known-good one; `duration:` bounds a step's run time. Any rule can be a tolerance
  (`"~= 0.93 +- 0.01"`, `"+- 5%"`) or a range (`"between 0.8 and 0.95"`). Malformed expectations fail when
  testbench.yaml loads. `testbench loop --metric check:SUBSTRING` uses any check's number. See docs/expectations.md.
- GitHub Action (`uses: sarptandoven/agent-testbench@v1`): runs a plan in CI and shows its report as the job summary,
  plus junit.xml, the evidence as an artifact and a pass/fail check; a refused run fails with the reason. Budgets
  apply; `approve: true` is a standing yes. See docs/github-action.md.
- Windows: the CLI, local plans and sessions, loops, reports and the MCP server run on Windows (CI covers it). One
  module (procs.py) starts, checks and stops background work on every system; the kernel launcher interrupts its
  kernel on request, which works on Windows too; the CLI runs in Python's UTF-8 mode there.
- `testbench demo [DIR]`: copies the tiny-model example (now in the wheel) to a new git repository, runs its free
  plan, and shows what to try next; it says which packages to install if scikit-learn or matplotlib are missing.
- `testbench init` looks at the project and writes plans that already check something: notebooks (lint, run, the
  files they write), a pytest suite, training scripts and web apps, with a FILL IN line for the number only you know.
- The watchdog finds a session's processes with `ps` where there is no /proc (macOS, BSD).
- docs/demo-loop.md: a real Claude Code run with only this plugin took a model from 0.86 to 0.9756 accuracy with
  `testbench loop` in 6 tries.
- docs/demo-sandbox.md: a real Claude Code run fixed a failing test suite proved in a Docker sandbox with no
  network, judged by the suite's JUnit report (32 s, $0.10 in tokens).
- Proof records: each report.json now names the git commit it ran (and a hash of uncommitted changes), the
  machine and packages where it ran, and the sha256 of every evidence file; it follows a published JSON Schema
  (schema/report-v1.schema.json). `testbench verify` checks the evidence still matches.
- `testbench report --format html|junit`, `testbench history <plan>`, and `testbench ui` (static pages for every run,
  with a trend line per checked number).
- `testbench prices [--update]`: every price in use and where it comes from; a fetched catalog can raise prices or
  add hardware, never lower a price.
- `backend: ssh`: plans and live sessions on any machine you reach with ssh (a GPU box, a university server, a
  rented instance), in a copy of the project and a virtualenv there, priced by `ssh.usd_per_hour`, with the
  watchdog as the hard stop on the machine itself. Secrets go over stdin. Checked against a real sshd.
- The watchdog interrupts a plan, then ends it if it has not stopped 30 s later, and never signals a process that
  only reused a plan's pid.
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
