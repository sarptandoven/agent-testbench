# Recipes

`testbench init` writes plans for what it finds in a project. A recipe starts from what you want to prove instead -
"fine-tune a model", "test my LLM app", "check my web app" - with a ladder of plans that proves it: a free check on
this machine first, a short check on the target hardware, then the full run. Every step has expectations that fail
when the result is wrong, not only when the code crashes, and a `max_minutes` that bounds what it can cost. Lines
marked `FILL IN` hold what only you know: your script's flags, the number that means "working".

```
testbench recipes                          # every recipe: what it is for, its backends, each plan's worst case
testbench recipes pytorch-train            # one recipe, as it ships (YAML with its comments)
testbench init --recipe pytorch-train      # a new testbench.yaml from it; repeat --recipe to combine recipes
testbench init --add --recipe llm-eval     # its plans added to the testbench.yaml you already have
```

The MCP server has the same as the `recipes` tool (no name: the list; a name: that recipe). The recipes are files in
[src/agent_testbench/recipes/](../src/agent_testbench/recipes), installed with the package; other packages can add
more ([recipe plug-ins](plugins.md#recipes)), and `testbench recipes` names the package each of those comes from.
In a project with a test suite and `src/train.py`:

```
$ testbench init --recipe pytorch-train
Wrote testbench.yaml from recipe pytorch-train:
  plan smoke: local, up to 10 min, free (recipe pytorch-train)
  plan gpu-check: modal T4, up to 10 min, worst case $0.23 (recipe pytorch-train)
  plan full: modal A10, up to 60 min, worst case $1.54 - above budget.ask_above_usd $0.50: a person approves it (recipe pytorch-train)
    runs src/train.py, found here, where the recipe says train.py
  plan tests: a pytest suite (tests/test_a.py, ...), found here
  found a training script (src/train.py): the recipe's plans already cover it, so it gets no plan of its own
and the working rules in AGENTS.md (CLAUDE.md points to it).
Next: fill in the lines marked FILL IN in testbench.yaml - the commands, paths and numbers that mean "working" - then `testbench run smoke`, the cheapest plan first.
```

## How a recipe is added

- **As text, comments and all.** The recipe's header (what it proves, what to fill in, what it costs) goes above
  its plans in testbench.yaml, and every `FILL IN` comment stays next to its line.
- **With your files.** Where a recipe names `notebook.ipynb`, `train.py` or `finetune.py` and the project has no file
  of that name, init puts in the project's own: the first notebook it finds, the training script it finds (`train.py`
  and the like, at the top, in `src/` or in `scripts/`). It says which it used.
- **With what init finds.** `init --recipe` also writes init's usual plans for what it detects in the project, except
  those the recipe's plans already cover (the same notebook or script, a pytest suite, a web page), and says which it
  left out.
- **Without changing anything.** `--add` only adds: the recipe's plans go after yours, indented like yours, and each
  section the recipe's plans need (`modal:`, `colab:`, `docker:`, `ssh:`) is added only if the file has none.
  If it has one, yours is kept and init prints the recipe's version, so you can merge what its plans need (the
  packages, above all). A recipe's `sessions:` (docker-sandbox, ssh-gpu-box) is never added: it would move
  `testbench session start`'s kernels off this machine, so init prints it for you to add or not. A plan name that is
  already taken is refused, and nothing is written.
- **Only if it loads.** The combined file is checked as testbench.yaml is when it loads, before it is written. A
  recipe that runs a notebook the project does not have is refused with what to do.
- **Inside your budget.** A recipe never sets `budget:`, `pricing:` or `guard:`. Every recipe plan fits the default
  `max_run_usd` ($2.00); the full runs on Modal are above the default `ask_above_usd` ($0.50), so a person approves
  each before it starts. `testbench recipes` judges each worst case by the project's own prices and budget when run
  in a project.

## The recipes

Worst cases at the built-in prices (`testbench prices`); start-up minutes are priced in.

| recipe | plans | where | worst case |
|---|---|---|---|
| [pytest](#pytest) | `tests` | local | free |
| [notebook-colab](#notebook-colab) | `notebook-smoke`, `notebook-colab` | local, Colab CPU | under $0.01 |
| [pytorch-train](#pytorch-train) | `smoke`, `gpu-check`, `full` | local, Modal T4, Modal A10 | free, $0.23, $1.54 |
| [hf-finetune](#hf-finetune) | `finetune-smoke`, `finetune-gpu-check`, `finetune-full` | local, Modal T4, Modal A10 | free, $0.26, $1.67 |
| [web-app](#web-app) | `ui`, `ui-build` | local | free |
| [llm-eval](#llm-eval) | `eval-quick`, `eval-full` | local | free (model calls are billed by your provider) |
| [docker-sandbox](#docker-sandbox) | `sandbox-tests`, `sandbox-run` | docker, no network | free |
| [ssh-gpu-box](#ssh-gpu-box) | `box-smoke`, `box-check`, `box-full` | local, your GPU machine | free at `usd_per_hour: 0` |
| [data-pipeline](#data-pipeline) | `pipeline-sample`, `pipeline-full` | local | free |
| [jest](#jest) | `jest-tests`, `jest-sandbox` | local, docker, no network | free |
| [vitest](#vitest) | `vitest-tests`, `vitest-sandbox` | local, docker, no network | free |
| [go-test](#go-test) | `go-tests`, `go-sandbox` | local, docker, no network | free |
| [cargo-test](#cargo-test) | `cargo-tests`, `cargo-sandbox` | local, docker, no network | free |
| [playwright-ts](#playwright-ts) | `e2e` | local | free |
| [trading-backtest](#trading-backtest) | `backtest`, `lookahead`, `walk-forward`, `holdout` | local | free |
| [vectorbt-strategy](#vectorbt-strategy) | `vbt-backtest`, `vbt-lookahead`, `vbt-walk-forward`, `vbt-holdout` | local | free |
| [freqtrade-strategy](#freqtrade-strategy) | `ft-backtest`, `ft-lookahead`, `ft-walk-forward`, `ft-holdout` | local | free |
| [alpaca-paper](#alpaca-paper) | `paper-rehearsal`, `paper` | local (the mock broker), local (Alpaca's paper API) | free |

The plan names differ between recipes, so any of them combine, except that pytorch-train and ssh-gpu-box are the
same ladder on different machines, and trading-backtest, vectorbt-strategy and freqtrade-strategy the same ladder for
different frameworks: each has a plan that opens the holdout, and one plan per project may, so `init --add` refuses
a second one. A testbench.yaml has one `docker:` section: combine docker-sandbox or two of the
language recipes and the first one's image is kept (init prints the other's), so the second's sandbox plan needs
an image with both.

### pytest

**Proves** that the project's own tests pass, counted from pytest's JUnit report: the report must be written by this
run (one left over from an earlier run fails), no test failed or errored (the report names the first failing tests
and their messages), and at least as many tests ran as you have - so a suite that silently collects nothing fails
instead of passing. A `duration` check catches a suite that suddenly takes much longer.

**Adapt it**: set `tests: ">= N"` to the number of tests you have now, and add your pytest arguments. `python` in a
step is the Python running agent-testbench; use `.venv/bin/python -m pytest` if the project lives in another
environment.

**Costs** nothing.

### notebook-colab

**Proves**, in `notebook-smoke`, that the notebook has none of the pitfalls that break Colab or a headless run
(`testbench lint --strict`: unpinned installs, hard-coded `/content` paths, secrets read with no fallback, form fields
nothing can set), and that it runs top to bottom here with small values in its form fields, writing the files and
reaching the numbers it should. `notebook-colab` runs the same notebook, unchanged, on a Colab VM to the full result.

**Adapt it**: the form fields to set small (`testbench nb fields NOTEBOOK` lists them), the files the notebook writes,
the numbers a quick and a full run must reach, and `colab.pip` for packages Colab lacks. Add `gpu: T4` to
`notebook-colab` for a GPU VM.

**Costs**: `notebook-smoke` nothing; `notebook-colab` a CPU VM at about 0.1 compute units an hour - under a cent at
worst; a T4 is about 1.8 units an hour. Colab can reclaim a VM after an hour or two; long runs belong on Modal or
your own machine.

### pytorch-train

**Proves**, in `smoke`, that the training script runs end to end on this machine's CPU and learns: the last
`loss=` it prints is below where an untrained model starts (ln 10 = 2.30 for ten classes; the `\b` in the pattern
keeps `val_loss=` from counting), its accuracy is above chance, and it saves a checkpoint. `gpu-check` proves on a
Modal T4 that torch's CUDA build works with the machine's driver, that the GPU is busy (`gpu: {busy_pct: ">= 50"}`,
not idle while the CPU or the data loader does the work) and that the loss still goes down. `full` is the real run
on an A10, with an accuracy range: near 1.0 on a hard task usually means test data leaked into training.

**Adapt it**: your script's flags for a tiny, a short and a full run; the starting loss and the accuracy; the packages
`train.py` imports in `modal.pip` (torch 2.14.1 is pinned; PyPI's torch 2.14 wheels bundle CUDA 13, which needs a
recent driver, and `gpu-check` is where a mismatch shows); and `full`'s `max_minutes`, which is also its hard stop.
`--data-dir "${TESTBENCH_CACHE:-.cache}/data"` downloads a dataset once onto the project's Modal volume.

**Costs**: `smoke` nothing; `gpu-check` up to $0.23 (10 minutes of a T4 with 4 CPU cores and 16 GB, plus Modal's
start-up); `full` up to $1.54 (60 minutes of an A10), which a person approves.

### hf-finetune

**Proves**, in `finetune-smoke`, that the training loop, the evaluation and saving the LoRA adapter work, with
`hf-internal-testing/tiny-random-LlamaForCausalLM` (a random Llama small enough for a CPU) for 20 steps: the eval and
train losses are numbers (NaN, infinity or a missing number fails) and PEFT's `adapter_config.json` and
`adapter_model.safetensors` are saved. `finetune-gpu-check` trains the real base model on a Modal T4 for a few
minutes with the GPU busy and the eval loss below the base model's own. `finetune-full` lands on the eval loss of a
run you trust, within a tolerance (`"~= 1.35 +- 0.05"`).

**Adapt it**: `finetune.py`'s flags, the base model (`Qwen/Qwen2.5-0.5B` in the example), the eval losses, and the
packages; the image pins torch 2.14.1, transformers 5.20.2, peft 0.21.2, accelerate 1.15.0 and datasets 5.1.0, whose
stated requirements agree (install the same versions locally, so the free smoke plan is where a conflict shows). On
Modal `HF_HOME` is on the project's volume, so the base model downloads once; gated models need
`secrets: [HF_TOKEN]`.

**Costs**: `finetune-smoke` nothing; `finetune-gpu-check` up to $0.26 (a T4 with 32 GB of memory for loading the
model); `finetune-full` up to $1.67 (60 minutes of an A10), which a person approves.

### web-app

**Proves**, in `ui`, that the dev server starts, the page answers 200, has the right title and text, and logs no
console or page errors; then that a click-through of the main flow (fill, click, wait for the result) works, with
screenshots to look at. `ui-build` proves that a clean install from the lockfile (`npm ci`) and the production build
succeed and write the build's output.

**Adapt it**: the dev command and port - give the host, so the server answers on the address the check opens (Vite:
`npm run dev -- --host 127.0.0.1 --port 5173 --strictPort`; Next.js: `npm run dev -- -H 127.0.0.1 -p 3000`) - the
title and text, your flow's selectors and the text it ends on, and the build's output folder (`dist/`, `.next/`,
`build/`). Needs `playwright install chromium`.

**Costs** nothing. Look at the screenshots before calling the page right: a check sees text, not layout.

### llm-eval

**Proves**, in `eval-quick`, that on a small fixed sample (the same cases and seed every run) the score the eval
prints clears your bar (`metrics: {stdout: {"\\bscore=([0-9.]+)": ">= 0.70"}}`) and every case was scored (`n: 50`
in its JSON). `eval-full` runs the whole set and wants the score within a tolerance of a run you trust
(`"~= 0.80 +- 0.03"`): a drop is a regression, and a jump is worth a look too.

**Adapt it**: the eval command, the bar, the known-good score and how much it moves between runs, and the NAMES of
the API keys under `secrets:` (exported in your shell; never their values in the file). It is ready for a loop:
`testbench loop start eval-quick --metric outputs/eval-quick.json:score --budget-usd 0` keeps a prompt or model
change only when the score goes up (`--metric check:score` reads the printed score instead).

**Costs** nothing on testbench's side; the model calls are billed by your provider and are not in testbench's budget,
so keep the quick sample small. The app's own tests should use the mock SDKs (`mocks: [openai, anthropic]` on their
step, [docs/mocks.md](mocks.md)), not the real API.

### docker-sandbox

**Proves**, in `sandbox-tests`, that the tests pass in a fresh container from a pinned image with `network: none` -
nothing the code does can download, upload or reach a service, and nothing it writes lands on this machine.
`sandbox-run` first proves the container really is offline (a connection fails with "Network is unreachable", not
for another reason), then runs the code and checks what it writes. `sessions: {backend: docker}` sandboxes live
sessions too.

**Adapt it**: put every package the code needs, pinned, in `docker.pip` - testbench installs them when it builds the
image, with the network on, and the containers that run the code have none - or build an image yourself and use it
with `setup: none`. Then the test arguments, the number of tests, and the command and what it writes. A container is
not a virtual machine; [docker.md](docker.md) says what it isolates and what it does not.

**Costs** nothing (`usd_per_hour: 0`); the first run builds the image, a minute or two.

### ssh-gpu-box

**Proves**, in `box-smoke`, that training runs end to end here on the CPU; in `box-check`, that testbench reaches the
box, builds its virtualenv there, that torch sees the box's GPU and a short run keeps it busy; in `box-full`, that
the full run reaches the result you need. `sessions: {backend: ssh}` puts live kernels on the box.

**Adapt it**: `ssh.host` (`ssh HOST true` must work with keys), the box's Python, `ssh.pip`, the GPU's name as torch
prints it, the script's flags and the numbers. The watchdog on the box is the hard stop at each plan's `max_minutes`,
even if this machine sleeps; testbench bounds the work, not the machine, so for a rented instance set `usd_per_hour`
to its rate and the budget counts it.

**Costs** nothing at `usd_per_hour: 0`.

### data-pipeline

**Proves**, in `pipeline-sample`, that on a small sample committed next to the code the pipeline writes every output,
reads every input row, keeps as many as it should (`rows_in`, `rows_out`, `dropped_pct` in a JSON summary it writes)
and finishes in seconds, so a change that drops rows or breaks a join fails at once. `pipeline-full` checks the full
data's counts against a range (`"between 900000 and 1100000"`) and its run time.

**Adapt it**: the command and outputs, the sample's exact row count, the share of rows a run may drop, and the full
data's path and range. The summary file is the cheapest way to make a pipeline checkable. For data that lives
elsewhere, run the same plan on `backend: ssh`, or `backend: docker` with the data mounted read-only.

**Costs** nothing.

### Test suites in other languages

The jest, vitest, go-test, cargo-test and playwright-ts recipes are the [pytest](#pytest) recipe for other runners:
a local plan judged by the runner's own JUnit report - written by this run, no failures or errors (the report names
the first failing tests and why), and at least as many tests as you have (`tests: ">= N"`, to fill in) - then, but
for Playwright, the same suite in a fresh container with `network: none`. [Other
languages](expectations.md#other-languages) has each runner's command and what its report holds. `testbench init`
writes the same local plan when it finds a `test` script in package.json, a go.mod with tests or a Cargo.toml.

**How the packages get into a container with no network.** testbench's executor runs inside the container, so
the image needs Python 3.10 or newer with ipykernel, jupyter_client, nbformat, nbclient and pyyaml; the official
node, golang and rust images have no pip, and the docker backend's own `setup: build` only adds pip packages to an
image. So each recipe's header holds a Dockerfile.testbench that starts from the official Python image, copies the
toolchain from the official image for the language (pinned: `node:24-trixie-slim`, `golang:1.26-trixie`,
`rust:1.97-slim-trixie`, all on the same Debian as `python:3.13-trixie`), installs testbench's basics and the test
runner, and fetches the dependencies while the network is on: `npm ci` into `/work/project/node_modules` (where the
project copy lands; this machine's node_modules is never copied in), `go mod download` with `GOPROXY=off` after
it, `cargo fetch` with `CARGO_NET_OFFLINE=true`. You build it once - `docker build -f Dockerfile.testbench -t
go-tests .` - and the plan uses it as it is (`setup: none`). When the lockfile changes, rebuild it: Go and cargo
then fail naming the module they cannot fetch, and the node plans' first step compares package-lock.json with the
one the image was built from and says to rebuild.

### jest

**Proves**, in `jest-tests`, that jest's suite passes, counted by jest-junit, including a test file that fails to
load (`JEST_JUNIT_REPORT_TEST_SUITE_ERRORS`, or it would be missing from the report); `jest-sandbox` runs it offline
in the image. **Adapt it**: `tests: ">= N"`, your jest arguments, the image tag; install `jest-junit@17.0.0`.
**Costs** nothing.

### vitest

**Proves**, in `vitest-tests`, that `vitest run` passes, counted by vitest's built-in junit reporter (a file that
fails to import is a failed test named after it); `vitest-sandbox` runs it offline in the image, whose node_modules
holds the Linux builds of vite's and esbuild's native parts. **Adapt it**: `tests: ">= N"`, your vitest arguments
(`--project`, `--config`), the image tag. **Costs** nothing.

### go-test

**Proves**, in `go-tests`, that every package builds and every test passes (`-count=1`: run now, not a cached
pass), counted by gotestsum: `go run gotest.tools/gotestsum@v1.13.0` pins it with no install. gotestsum because
`go test` writes no JUnit and gotestsum is the converter Go CI uses: a package that does not build fails as
`<package> > TestMain`, a panic as the test that panicked. `go-sandbox` runs it offline in the image, with the
gotestsum the image installed (offline, `go run ...@version` fails: it asks the module proxy whether the module is
deprecated). **Adapt it**:
`tests: ">= N"` (gotestsum's `DONE N tests`; subtests and their parents count), packages and flags, the image tag.
**Costs** nothing.

### cargo-test

**Proves**, in `cargo-tests`, that every test binary's tests pass, counted by cargo-nextest
(`target/nextest/ci/junit.xml`): a failure is named with its binary, the panic and the values compared.
`cargo-sandbox` compiles and runs them offline in the image. **Adapt it**: add the `[profile.ci.junit]` lines to
.config/nextest.toml (the header has them), `cargo install cargo-nextest --locked`, set `tests: ">= N"` and your
arguments (`--workspace`), and `exclude: [.git, target]` so the build folder is not copied into containers. Doc
tests need a `cargo test --doc` step of their own. Without nextest, `cargo test` can be checked by its exit code
only. **Costs** nothing; building nextest into the image takes a few minutes, once.

### playwright-ts

**Proves**, in `e2e`, that the project's own Playwright tests pass as its playwright.config.ts runs them (projects,
webServer), counted by Playwright's junit reporter, with traces and screenshots of failures (`test-results/`) in the
run. **Adapt it**: `tests: ">= N"` (`npx playwright test --list`), your arguments; `--retries=0` if a pass on retry
should fail. It has no docker plan: the browsers need Playwright's own image (`mcr.microsoft.com/playwright`), and
the header says what to add to it. For a page with no Playwright tests of its own, use [web-app](#web-app).
**Costs** nothing.

### Trading strategies

testbench is not a broker, a signal service or investment advice, and it never picks a strategy: these recipes check
the claims a backtest makes, rung by rung, from files the backtest writes, and 0.3 stops at paper trading ([Trading
with agents safely](trading-safely.md)). Each strategy recipe brings a `data:` section that names the prices and seals
their latest rows, and four plans:

- the **backtest**, on every row before the seal, graded by `strategy:` - testbench's own numbers from the standard
  files ([Trading](trading.md#the-files)), with fees or slippage charged, 30+ round trips and `beats: buy_and_hold`;
- **look-ahead**: the backtest again on the data cut earlier, 4 times; a decision in `out/signals.csv` that changes
  when the rows after it are cut away fails the plan;
- **walk-forward**: the steps once per window (24 months of history, 6 to trade), each with only the rows before its
  end, and every window must hold up;
- the **holdout**: once, on the sealed rows no other plan saw, by rules written before it opens; a second opening is
  refused ([Sealed data](trading-data.md)).

`testbench recipes` gives a walk-forward plan's time per window: its windows come from the data, which a recipe does
not ship (`testbench plans` counts them once it is there).

#### trading-backtest

**Proves** the ladder above for any backtest that writes the standard files: plain pandas, backtrader, LEAN,
NautilusTrader ([Writing the files from a framework](trading.md#writing-the-files-from-a-framework) has a snippet for
each). [examples/trading](../examples/trading) is a project made to it (`testbench demo trading`). **Adapt it**: the
prices file and `holdout_from`, the backtest command, the bar (`sharpe`, `max_drawdown`), the windows, and what a
window and the holdout must at least do. The backtest reads `TESTBENCH_DATA_PRICES`, trades from
`TESTBENCH_TEST_START` or `TRADE_FROM` when set, and writes `out/signals.csv` with the signal's number, not only
the position it gives: a number changes on every peek ahead, a 0 or 1 only when it flips. In a folder with no
`backtest.py`, `init` writes a starter one (plain Python, a trend follower with fees and no look-ahead, writing the
three files) and says so; it never writes over a file. A backtest of your own that writes its own CSV files:
`python -m agent_testbench.trading.files --check out` says what to rename or add in each. **Costs** nothing.

#### vectorbt-strategy

**Proves** the same ladder for a `vbt.Portfolio`, with testbench's numbers in place of `pf.stats()`. The header shows
the lines that write the files from `pf.orders` and `pf.value()`, and the signals from the indicator, the entries and
the exits. **Adapt it**: as trading-backtest, and install vectorbt where the step's `python` is. **Costs** nothing.

#### freqtrade-strategy

**Proves** the same ladder for a freqtrade `IStrategy`. freqtrade reads its own data folder, which the seal does not
cut, so the step's script first writes the cut candles there from `TESTBENCH_DATA_PRICES` (keep your candles in
`data/candles.csv`), runs `freqtrade backtesting` with `--timerange` from `TESTBENCH_TEST_START` or `TRADE_FROM`, and
writes the standard files from its results; the signals leave out exits forced at the end of the data, which a cut
run forces where the full run did not. The freqtrade snippet was not run against freqtrade: check its files with
`testbench numbers trading` first. **Costs** nothing.

#### alpaca-paper

**Proves**, in `paper-rehearsal`, that a bot on alpaca-py reads the bars, decides and places its orders day by day on
testbench's mock Alpaca paper account (`mocks: alpaca`, its clock walking through a prices file): no more orders and
none larger than you allow, nothing refused, no live account, and its record graded like a backtest - free, offline,
seconds, and proof of the order code, not of an edge. `paper` runs the same bot once a day against Alpaca's paper
API, with the keys by name (`secrets: [ALPACA_API_KEY, ALPACA_SECRET_KEY]`), and checks that the account it reached
is a paper one (Alpaca numbers them PA...). Live trading is not part of 0.3: no recipe, plan or mock sends a live
order, and the mock refuses `paper=False`. [examples/trading/paper.py](../examples/trading/paper.py) is a bot made to
it. **Adapt it**: the bot's command, the rehearsal's prices, first day and length, the order bounds, the key names.
`init` refuses it until the rehearsal's prices file is in the project. **Costs** nothing; `paper` needs the network
to reach Alpaca and a paper account.

## How the recipes are tested

`tests/test_recipes.py` loads every recipe as a config under init's header and checks that the costs its header
states are its worst cases; combines them in one file (one strategy recipe of the three); adds them to existing files
without changing a line; and runs every plan that runs on this machine - `tests`, `notebook-smoke`, `smoke`,
`box-smoke`, `finetune-smoke`, `eval-quick`, `eval-full`, `pipeline-sample`, `ui`, the docker sandbox's two plans,
trading-backtest's four and alpaca-paper's `paper-rehearsal` - on a tiny project made to each recipe's contract (for
the trading ones, examples/trading's prices and strategy), plus an improvement loop on `eval-quick`'s score. The
Modal, Colab and ssh plans, the vectorbt and freqtrade ladders and the `paper` plan against Alpaca are checked as
configuration only.

`tests/test_recipes_languages.py` checks that each language recipe's Dockerfile starts from pinned official images
and installs what testbench needs in a container, and that init's plan for each language runs its runner the way
the recipe does; reads a real report from each runner; and, where the toolchain is on the machine, runs `jest-tests`,
`vitest-tests`, `go-tests` and `cargo-tests` on a tiny project - passing, then failing with the broken test named.
With `TESTBENCH_TEST_DOCKER_LANGS=1` it builds each recipe's image and runs its sandbox plan offline.
