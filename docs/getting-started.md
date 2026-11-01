# Getting started

--8<-- "README.md#quick-start"

## A first run, step by step

What the [quick start](../README.md#quick-start) commands do on a real project: a copy of
[examples/tiny-model](../examples/tiny-model) (a notebook that trains a small digit classifier) with its
testbench.yaml removed, in a folder named `myproject`. Everything below runs on this machine and costs nothing.
The output is from a real run on 2026-10-31, shortened where a line says `...`.

### 1. See what this machine can run

```
$ testbench doctor
ok   python >= 3.10: 3.13.17
ok   backends: local, modal, colab, ssh, docker
ok   modal (Modal plans and sessions): installed
no   Modal login: run `modal setup`
no   colab CLI (Colab plans and sessions): uv tool install google-colab-cli
ok   docker daemon: server 29.9.2
...
```

Each `no` line says what to run to fix it. You only need the backends you will use; `local` always works. The
[install notes](../README.md#install-notes) say what each backend needs.

### 2. Let testbench look at the project

```
$ cd myproject && testbench init
Wrote testbench.yaml:
  plan tiny-model-local: notebook tiny_model.ipynb (4 form field(s), writes confusion.png, metrics.json)
and the working rules in AGENTS.md (CLAUDE.md points to it).
Wrote ~/.testbench/limits.yaml: limits for every project on this machine, which a project's budget can lower but never raise (edit it yourself to raise them).
Next: fill in the lines marked FILL IN in testbench.yaml - the numbers that mean "working" - then `testbench run tiny-model-local`.
```

`testbench.yaml` now holds the budget, where live sessions start by default, and a plan for the notebook:

```yaml
plans:
  tiny-model-local:
    backend: local
    description: lint tiny_model.ipynb, then run it top to bottom on this machine
    steps:
      - name: lint
        run: testbench lint tiny_model.ipynb
        max_minutes: 1
      - name: tiny-model
        notebook: tiny_model.ipynb
        max_minutes: 20
        # params: {HIDDEN: 64, EPOCHS: 60, LEARNING_RATE: 0.01, SEED: 0}      # its form fields; set smaller values for a quick check
        artifacts: ["**/confusion.png", "**/metrics.json"]
        expect:
          no_errors: true                # every cell runs
          files: ["**/confusion.png", "**/metrics.json"]   # what it writes
          # json: {metrics.json: {accuracy: ">= 0.9"}}   # FILL IN: the number that means it works
```

It already checks that every cell runs and that the files the notebook writes appear. AGENTS.md tells any agent
working in the project how to use testbench, and `~/.testbench/limits.yaml` caps every project on the machine
([machine limits](reference.md#machine-limits)).

### 3. Say what "working" means

Only you know which number makes the notebook work. Replace the FILL IN line with the check you want. json paths
are relative to the project folder, and this notebook writes its metrics to `outputs/metrics.json`:

```yaml
          json: {outputs/metrics.json: {accuracy: ">= 0.9"}}
```

[Expectations](expectations.md) lists every kind of check: tolerances, the project's own test suite, numbers
printed to the log, GPU use, plots compared with a known-good one.

### 4. Prove it

```
$ testbench run tiny-model-local
[testbench] run 20261031-065523-tiny-model-local started (Within budget: up to $0.00 (today so far $0.00 of $10.00; $0.00 of $20.00 on this machine).)
[testbench] step 1 started: lint
[testbench] step 1 passed after 0.01 min: lint
[testbench] step 2 started: tiny-model
[testbench] step 2 passed after 0.05 min: tiny-model

# PASS - plan `tiny-model-local` on local

Run `20261031-065523-tiny-model-local`, started 2026-10-31 06:55, 0.06 min, about $0.000.
Code outside git; Python 3.13.17 on x86_64; evidence sha256 `c9e3b3ce5acafee6` (`testbench verify` checks it).

## Steps

| | step | minutes | checks | GPU busy |
|---|---|---|---|---|
| PASS | lint | 0.01 | 1/1 | - |
| PASS | tiny-model | 0.05 | 4/4 | - |
...
## Passed checks
...
- tiny-model: outputs/metrics.json: accuracy >= 0.9 - 0.9733 (wanted >= 0.9)
```

The worst case was priced before the run started ($0.00 here: it ran on this machine). A failing run leads with
what failed and why, and from the second run on the report says what changed since the last one. The run's folder,
`.testbench/runs/<run id>/`, holds the report, the executed notebook, the plots and the files the notebook wrote.

??? example "What a failing run prints: a picture of the real output, not shortened, for the demo project's `smoke` plan with a bad learning rate planted in it"

    [![The full terminal output of testbench run smoke with the planted bug: progress lines for both steps and every cell, the cell 3 metrics with accuracy 0.1711 and learning_rate 1.0, then the report - FAIL, plan smoke on local; stopped because step 2 (train quick) failed; code with uncommitted changes; expected accuracy >= 0.85: 0.1711 (wanted >= 0.85); the last output; compared with the previous run: Broke: train quick, accuracy 0.9756 -> 0.1711; the steps table; the evidence to look at; the passed check.](assets/terminal-fail.png)](assets/terminal-fail.png)

    The runs behind it, and the fix, are in [the five-minute tour](tour.md).

Then:

```
$ testbench verify
20261031-065523-tiny-model-local: all 11 evidence files match their recorded sha256 (c9e3b3ce5acafee6); ran on code outside git
$ testbench report --format html          # one page with the plots inline
$ testbench history tiny-model-local      # each run, and how the checked numbers moved
```

The page `report --format html` writes, here a screenshot for the demo project's `smoke` plan, run again after a
fix ([the tour](tour.md)):

[![The HTML report of a passing run of the smoke plan: a green PASS badge; facts - local, $0.000, worst case $0.00, the commit, Python 3.13.17, 11 evidence files; compared with the previous run (failed): Fixed: train quick, accuracy 0.1711 -> 0.9756; steps: lint and train quick PASS; checks: all six ok, accuracy 0.9756 (wanted >= 0.85); evidence to look at: the notebook's confusion matrix plot, titled accuracy 0.976.](assets/report-pass.png)](assets/report-pass.png)

In a git repository the report also names the commit it ran; see [proof records](proof-records.md).

### 5. Hand it to your agent

Install the Claude Code plugin or add the MCP server (the commands are in the [quick start](../README.md#quick-start)), then ask in
plain words: *"get training working and prove it with the tiny-model-local plan"*. The agent works in live
sessions, proves the result with a plan run, and stays inside the budget in testbench.yaml and
`~/.testbench/limits.yaml`.

From here:

- Work live in a kernel: [live sessions](../README.md#live-sessions).
- Run the same notebook on a GPU, in a container or on another machine: [backends](backends.md).
- Let the agent raise a number, keeping only what helps: [loops](loops.md).
- Check every pull request: [the GitHub Action](github-action.md).
- Every field of testbench.yaml: [reference](reference.md).
