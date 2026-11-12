# Changelog

## 0.3.0 - not yet released

Phase 3: one command to start, a small core with everything else as plug-ins, and a trading pack that will not call
a strategy good until it survives costs, look-ahead, an untouched holdout and the number of tries it took.

**Start in a minute**
- `testbench check` (and `uvx agent-testbench check`): a first proven result with no config. It runs the plans `init`
  would write for what it finds, here and for free, as ordinary runs with proof records; with a testbench.yaml it
  runs every free plan and lists the paid ones with their worst case. A plan whose tool is missing is skipped with
  the command that installs it. Also the MCP tool `check_project` and `Testbench.check()`. From uvx, uv tool or
  pipx a step's `python` is the project's, not testbench's own.
- testbench.yaml can be short: `plans: {tests: "pytest -q"}` is a whole plan, a list of commands runs them in order,
  any step can be a plain command, and `project:` defaults to the folder's name. A JSON Schema (`testbench schema`)
  gives editors autocomplete; `init` writes the line that points VS Code's YAML extension at it.
- `testbench --help` shows a dozen core commands in three groups (`help --all` lists every one), and every refusal
  (exit 2) says what was refused, why, and the exact command or testbench.yaml field that fixes it
  (tests/test_refusals.py).
- `agent_testbench.core`: a small core (load, estimate, launch, wait, report, verify) that imports no layer and no
  optional package; importing it loads 121 modules instead of 269 (tests/test_core_boundary.py, docs/architecture.md).

**Plug-ins for everything**
- One registry (`plugins.py`) for seven kinds: backends, checks, mocks, export sinks, notifiers, recipes and metric
  readers. Built-ins sit in the same table; a broken plug-in is reported and breaks only what uses it.
  `testbench plugins` lists them; `testbench plugin test PACKAGE|module:attr|--installed` (and
  `plugins.assert_conforms`) checks one against its kind's contract; every built-in keeps its contract.
- Every `expect:` check is a plug-in, as are the mock SDKs (`StrictAPI` now runs where the step's Python lacks
  agent-testbench), export sinks, notifiers (new: `desktop`, a notification on this machine's screen), recipes (a
  recipe that sets budget:, pricing: or guard: never loads) and metric readers (`json`, `csv`, `junit`, `trading`;
  `testbench numbers READER [PATH]`). An example package for each kind is in `examples/plugins/`.

**Trading**
- Standard files (`trades.csv`, `equity.csv`, `prices.csv`) and testbench's own numbers from them
  (`agent_testbench.trading.metrics`): returns, CAGR, Sharpe, Sortino, drawdowns, Calmar, FIFO round trips, fees,
  turnover, exposure, a buy-and-hold benchmark. Checked against vectorbt and backtrader's own numbers.
- `strategy:` and `numbers:` expectations: rules on testbench's numbers, never the strategy's. Costs must be modelled,
  at least `min_trades` round trips are needed, and `beats: buy_and_hold` needs a benchmark.
- The data seal: `data:` names a strategy's data; `holdout_from` seals the latest rows from every plan but the one
  marked `holdout:` (each opening counted, `max_opens: 1` refuses a second); `walk_forward:` runs the steps once per
  window with only the rows before its test end; every run records the sha256, rows and time range its steps saw,
  and `verify` / `diff` catch a changed data file.
- `lookahead:` reruns a plan on data cut at earlier times and fails it when a past decision changes ("AAPL at
  2024-03-04 was `1` with data up to ... but `0` with data before ..."). `model_cutoff:` warns about results a model
  may have seen in training. The worst case and every hard stop count every rerun and window.
- Overfitting statistics from the papers, standard library only (`trading.stats`): probabilistic and deflated Sharpe
  ratios, minimum track record and backtest length, PBO by CSCV, Benjamini-Hochberg. `testbench loop` over a
  strategy reports "best of N tries: deflated Sharpe ... (probability the true Sharpe is above 0)" and PBO in every
  try, status, report and notification; `--min-deflated-sharpe` keeps a lucky try from meeting the target.
- A tear sheet made by testbench from the standard files: headline numbers with plain definitions, growth against
  buy and hold, drawdowns, monthly returns, trades, caveats - in HTML reports, `testbench ui`, the dashboard and
  `testbench report --tearsheet`.
- Strict mock brokers: `mocks: [alpaca]`, `[ccxt]` or `[ib_async]` fill orders from a prices file with fees and
  slippage, refuse a live account unless `allow_live: true`, and count orders, fills and fees for `mock_calls:`.
- `testbench demo trading`: a strategy with a planted look-ahead bug, caught in seconds and offline; with the
  one-line fix it passes a backtest with costs, look-ahead, walk-forward, the holdout once and a paper rehearsal on
  a mock Alpaca account (examples/trading). Recipes `trading-backtest`, `vectorbt-strategy`, `freqtrade-strategy`
  and `alpaca-paper` (a free rehearsal on the mock broker, then Alpaca's paper API, keys by name).
- docs/trading-safely.md, "Trading with agents safely": testbench is not a broker, a signal service or investment
  advice; 0.3 does backtests and paper trading only - no live money. An 11th selftest check: a strategy that
  decides with tomorrow's data is caught.

**For everyone**
- A VS Code extension (`editors/vscode`): plans, runs and the open loop, the HTML report, today's spend in the status
  bar, offline autocomplete; a plan that needs approval runs only after a person clicks "Approve and run" in a
  dialog showing its worst case. `plans`, `runs`, `spend` and `loop status` have `--json` forms.
- `testbench share RUN --to DEST` publishes a run's proof as a static page (a folder, a branch to push, S3): it lists
  every file first, needs a person's yes, never publishes state, keys or secret values, and scrubs this machine's
  paths. `testbench verify --from URL_OR_FOLDER` checks a published copy anywhere. `testbench badge` writes a README
  badge.
- Accessibility: scoped table headers, alt text naming the step and file, a visible focus ring, tested contrast in
  light and dark, and a dashboard that keeps keyboard focus across updates.

**Integrations**
- MCP resources (`testbench://plans`, `runs`, a run's report, report.json and evidence, the loop, the notes, the
  schema) under the dashboard's rules, and prompts (`prove-this-change`, `find-why-this-fails`, `tune-with-a-loop`,
  `check-a-strategy`).
- `testbench ci --for gitlab|azure|circleci|shell` writes a CI job (pinned version, JUnit the CI shows, runs kept,
  every refusal a failure, never an approval); `check` and `run` take `--junit FILE`.
- `tools.for_openai_agents()`, `for_langchain()` and `for_claude_agent_sdk()`: the MCP tools as each framework's own
  tool objects through one call path; the model is never offered an approval flag.

**Polish**
- `testbench --version` and `--help` start about three times faster (about 60 ms); the CLI loads config,
  budget, reports and notebooks only for commands that use them (a test keeps it so).
- One set of command groups everywhere: `help --all`, `--help` (its top three groups), README's Commands table and
  docs/reference.md use the same groups and lines, and tests fail when they drift. The README is reorganized for a
  newcomer, and the documentation site has nine sections.
- A newcomer walkthrough (a Python, JS, notebook, trading and plug-in author persona) fixed what they stalled on:
  no pointer to FILL IN lines when there are none, a missing module named with the command that installs it
  (for uvx too), a stopped Docker daemon said plainly, `plugin test` finding a module in the current folder,
  a printed score checked for notebooks that write no JSON, a pandas-free trading snippet, and more.

**Trust**
- docs/threat-model.md: who can do what, what each cannot, how, and the test that proves it.
- The guard hook reads commands with their quoting taken off: `--approve` in quotes, with backslashes, in a variable,
  through xargs, `bash -c`, a subshell, an option before the subcommand, or Python's `approve=True` asks the person;
  so do edits that move the limits (TESTBENCH_USER_DIR) or turn hooks off, and `testbench share`.
- MCP: when the client can ask the person, the person is always asked - `approved_by_person=true` no longer skips
  that question; the HTTP server checks Host and Origin with mcp 1.x too; every argument stays a value.
- A secret's value printed by the code becomes `[secret]` in the report and evidence before they are hashed.
- Watchdog and supervisor intervals cannot be stretched or broken from the environment; results from a Colab VM
  unpack only inside the run folder on every Python.
- Fuzz tests (configs, short forms, budgets, the guard, MCP arguments, the dashboard; `TESTBENCH_FUZZ_EXAMPLES=N`)
  and mutation testing of budget.py and the guard hook (`tools/mutate.py`, 98.4% of mutants killed).

## 0.2.0 - 2026-11-12

- `testbench selftest`: ten checks of the main promises in a temporary folder, local and free, in under a minute:
  a plan passes, a broken change fails with the reason, evidence is fingerprinted, costs are refused or asked about
  before anything starts, a NaN budget is refused, a live kernel answers, the mock LLM SDK, a loop keeps and
  reverts, an HTML report, the MCP server. It uses empty limits and ledger of its own. docs/verify.md: checking a
  release yourself in five minutes.
- A run's state is changed under a lock: the CLI writing the driver's pid and the driver writing its status at the
  same moment could lose one of the two, and a run whose pid was lost read as a dead driver and was closed as an
  error (seen as loop tries reverted with "the run error" on Windows CI). A run with no pid yet in its first minute
  is starting, not lost. Sessions' state is locked the same way.
- Release readiness: the PyPI page's links point at GitHub (the README's relative links are rewritten at build
  time), local builds leave out `.env` and caches, `release.yml` publishes only from a published GitHub release whose
  tag, versions and dated changelog agree (actions pinned to commit SHAs), and a test keeps the version equal in
  every file. `testbench mcp` without the `mcp` extra says what to install; the MCP server reports its version; the
  demo installs scikit-learn and matplotlib versions that have wheels for every Python, 3.14 included.
- The test suite runs in parallel (`python -m pytest -q -n 4`; pytest-xdist is in the dev extras): about 3.5 minutes
  instead of 13 here. `testbench cancel` no longer waits a minute for a run whose driver ended before it could
  close it.
- Secrets and paths: `connect --dry-run` hides other servers' values and writes configs privately; reports, the
  dashboard and `get_report` read only files inside the run; `artifacts:` with `..` stay in artifacts/; the PR
  comment escapes the report's commit and status; `rescue --fix` refuses any name of the original; loop tries never
  commit an untracked `.env`; report.md code blocks cannot be closed by output; `init`, `note` and `connect` refuse
  project files that link out of the project.
- Rough edges: `init --add` no longer adds a recipe's `sessions:` (it prints it); `params:`/`cells:`/`uploads:`/
  `colab_shim:` on a step that is not a notebook, and `cells:` that skip where a param is set, are refused instead
  of ignored. Notifications: `loop_rule` (a rule fired, the loop is still open) apart from `loop_end` (stopped); an
  opt-in `loop_try` replaces each loop run's `run_end`; `run_end` is sent once per run; `approval_needed` waits a
  minute for a person the MCP client is asking directly. `rescue` takes percent-format `.py` notebooks. `doctor`
  launches Chromium and names a missing build and the install command. The mock SDKs have
  `model_dump(exclude_none=)`, `model_validate` and the deep type modules. "1 try", not "1 tries".
- The command line explains itself: `testbench` / `testbench help` list the commands in groups with the ones to
  start with; every `--help` ends with examples; a mistyped command, plan, run, session, recipe, client or example
  name gets the closest one, and words in the wrong order get the right order. `status` exits 0/1/3 like `wait`;
  `session stop` with nothing running exits 0; a failed `session install` exits 1. `--format json` works wherever
  `--json` does, `wait --wait`, and `-s/--session` on every session command. Exit codes: docs/reference.md.
- Not only Python: recipes jest, vitest, go-test, cargo-test and playwright-ts, each with a local plan judged by the
  runner's JUnit report and (all but Playwright) an offline docker plan. `testbench init` detects npm test runners,
  Go modules and Rust crates. `junit:` reads real reports from nine runners correctly (mocha's failures, Go's
  failing line, gotestsum's build failures, JS test names) and says how to write a missing one.
- Fixes where the 0.2.0 features meet: `loop try` works in repositories that gitignore `.testbench/`; rescue no
  longer calls files a notebook writes (`path.open("w")`, `ZipFile(p, "w")`, archive members) or its own code
  (namespace folders, `sys.path.append`) missing, and sets papermill parameters small; `init --recipe` uses a
  percent-format `.py` notebook; MCP name refusals say why under mcp 2; the Python API raises `TestbenchError` for
  a notebook it cannot run; a loop's approval command is quoted.
- `testbench diff A B` (`--json`, MCP `diff_runs`) says what changed between two runs: each checked number old ->
  new and whether that is better by its rule, checks that flipped, evidence that changed, the commits with
  `git diff --stat`, packages that changed version. `testbench watch PLAN` reruns a free plan whenever project
  files change, one run at a time with at most one queued, each run through the budget (`--approve` asks, through
  the guard hook). Reports record package versions; `last~N` names runs; cancelling a local run takes about 1 s
  instead of 10.
- GitHub Action: `comment: true` posts one pull request comment per plan (verdict, failures, the checked numbers and
  their change since the base branch, cost, links), edited on later pushes and skipped on forks; `attest: true`
  signs report.json and the evidence with GitHub artifact attestations; `testbench verify RUN --attestation`
  checks them with `gh`. The repository's own self-test signs only when run by hand, since a public repository's
  attestations go to Sigstore's public log for good.
- `testbench demo [NAME] [DIR]` and `demo --list`: three more free examples beside tiny-model, each about a minute:
  `llm-app` (an Anthropic SDK support bot whose tool-use loop is proved against the mock SDK, no API key),
  `web-app` (a form filled in and checked in headless Chromium, with screenshots) and `test-suite` (a pytest suite
  with a planted bug, judged by `junit:` here and in a Docker sandbox with no network, for your agent to fix).
  All ship in the wheel; gallery: docs/examples.md.
- `testbench ui --serve [--port 8770] [--open]`: a live, read-only dashboard on 127.0.0.1 - today's spend against
  the limits, runs going now with their growing log, live sessions, the loop and its stopping rules, then each
  run's report. Standard library only; it refuses other Host headers, serves only reports and step evidence, and
  cannot launch, approve, cancel or spend. See docs/dashboard.md.
- docs/tour.md: a five-minute tour with real screenshots; the README shows a real HTML report.
- Recipes: `testbench recipes` lists starting points by goal (pytest, notebook-colab, pytorch-train, hf-finetune,
  web-app, llm-eval, docker-sandbox, ssh-gpu-box, data-pipeline), each a ladder of plans with real checks and each
  plan's worst case. `testbench init --recipe NAME` writes one (with the project's own notebook or training
  script); `--add` appends its plans to an existing testbench.yaml without changing a line. See docs/recipes.md.
- Mock LLM clients: `mocks: [openai, anthropic]` on a step swaps in strict, deterministic stand-ins for both Python
  SDKs (no key, no network). They refuse what the API would refuse with the SDK's own error, answer from a replies
  file (text, JSON, tool calls), fail chosen calls, and log every call to the step's `mock_calls.jsonl`;
  `expect: {mock_calls: {anthropic: {calls: "<= 5"}}}` checks the calls, and a refused call fails the step.
  See docs/mocks.md.
- `testbench rescue NOTEBOOK`: why a notebook does not run and what to do. It reads it (cells run out of order,
  names used before the cell that makes them, missing packages with their pip names, Colab-only parts, missing
  files), runs it top to bottom in a fresh kernel, names the first failing cell and why, and suggests a plan that
  proves it once fixed; `--fix OUT.ipynb` (mechanical fixes only), `--json`, `--no-run`. See docs/rescue.md.
- Notebook steps take papermill parameters (a cell tagged `parameters`), percent-format `.py` notebooks (`# %%`;
  jupytext when installed, a built-in reader otherwise), and the kernel the notebook names (a missing non-Python
  kernel is named with how to install it). See docs/notebooks.md.
- Loops left overnight: `testbench loop start --target/--patience/--max-tries` refuse further tries once a rule
  fires, saying what to do (`--new-direction` after patience). A `notify:` webhook (`webhook_env: NAME` keeps the
  URL out of the file) hears `run_end`, `loop_end` and `approval_needed`, never slowing or breaking a run;
  `testbench notify test`. See docs/loops.md and docs/notify.md.
- `testbench export RUN|latest|all --to mlflow|wandb|csv|jsonl`, and `export:` in testbench.yaml for every finished
  run: status, cost, commit, params and every checked number as metrics, the report and evidence as artifacts.
  Exporting again updates the run. Trackers are optional (`agent-testbench[mlflow]`, `[wandb]`) and export in the
  background. See docs/export.md.
- `backend: kubernetes`: plans and live sessions as Jobs on your cluster, GPUs priced by name in
  `kubernetes.usd_per_hour` (nothing unpriced launches). The project and evidence go over `kubectl exec`, with no
  shared storage; the hard stop is the Job's own `activeDeadlineSeconds`, plus a watchdog in the pod. Checked on a
  real kind cluster. See docs/kubernetes.md.
- `testbench connect CLIENT [--user] [--dry-run]` and `--list`: set agent-testbench up in Claude Code, Cursor, VS Code
  (Copilot), Codex or Gemini CLI - the MCP server merged into the client's own config (backed up, idempotent) and
  the working rules in a delimited AGENTS.md section; it says which guardrails hold outside Claude Code.
- Python API (`agent_testbench.Testbench`: runs, sessions, spend, verify) and `agent_testbench.tools`: the MCP tools
  as Anthropic and OpenAI tool definitions with a `call()` dispatcher, inside the same budget and approvals; an
  example agent loop in examples/agent-loop. See docs/python-api.md.
- Proof: `files:`, `json:`, `text:` and `media:` checks no longer pass on a file last written before the run
  started (an old result); `junit:`, `metrics:` and `image:` already refused one.
- `.env` and `.env.*` files never go into a copy of the project (Modal, Colab, ssh, docker, kubernetes, local
  copies), whatever `exclude:` says; secrets go by name (`secrets:`).
- The documentation site has a landing page, a fair comparison with the alternatives (docs/why.md), use cases, an
  FAQ and a troubleshooting page built from the real messages in the code.
- Guardrails hardened after an adversarial review. A budget, price or time limit that is not a real number (NaN,
  infinity) is refused when testbench.yaml, limits.yaml or a command line is read, and the budget check refuses
  any estimate or limit that is not one - before, a NaN budget in testbench.yaml let any launch past every limit,
  the machine's included. A ledger line testbench did not write refuses launches instead of being miscounted.
  GPUs are `NAME` or `NAME:COUNT` exactly (`"H100: 8"` was priced as one GPU). Hardware that only a fetched
  catalog knows is priced at no less than the dearest built-in hardware. Options cannot be abbreviated (`--appr`
  was `--approve`), and MCP tool arguments can no longer become options (a session named `--approve`). The guard
  hook now asks about `--approve` anywhere in a testbench command and outside projects, MCP approvals outside
  projects, lowered ssh/docker prices, a loosened docker sandbox, `prices --update --url` and edits of
  ~/.testbench/prices.json. Ssh plans are stopped from this machine shortly after their limit if the work there
  stopped its watchdog, and the watchdog no longer crashes when the work breaks its folder.
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
- Documentation site (MkDocs Material) built from README.md and docs/, deployed to GitHub Pages by
  .github/workflows/docs.yml; server.json for the official MCP Registry; a Cursor plugin manifest; the skills'
  frontmatter fits the Agent Skills spec; docs/publishing.md lists how each channel is published.
- A `json:` expectation's file can be a pattern (`"**/metrics.json"`) that must match exactly one file; the json line
  `testbench init` suggests uses one, so it works when uncommented.
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
