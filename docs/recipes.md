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
[src/agent_testbench/recipes/](../src/agent_testbench/recipes), installed with the package. In a project with a
test suite and `src/train.py`:

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
  section the recipe needs (`modal:`, `colab:`, `docker:`, `ssh:`, `sessions:`) is added only if the file has none.
  If it has one, yours is kept and init prints the recipe's version, so you can merge what its plans need (the
  packages, above all). A plan name that is already taken is refused, and nothing is written.
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

The plan names differ between recipes, so any of them combine, except that pytorch-train and ssh-gpu-box are the
same ladder on different machines.

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

## How the recipes are tested

`tests/test_recipes.py` loads every recipe as a config under init's header and checks that the costs its header
states are its worst cases; combines all of them in one file; adds them to existing files without changing a line;
and runs every plan that runs on this machine - `tests`, `notebook-smoke`, `smoke`, `box-smoke`, `finetune-smoke`,
`eval-quick`, `eval-full`, `pipeline-sample`, `ui`, and the docker sandbox's two plans - on a tiny project made to
each recipe's contract, plus an improvement loop on `eval-quick`'s score. The Modal, Colab and ssh plans are checked
as configuration only.
