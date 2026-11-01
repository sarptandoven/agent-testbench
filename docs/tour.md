# A five-minute tour

From `pip install` to a fix that a run proves, on your own machine and for free: a small notebook project, a bug
planted in it, the run that catches it, the fix, and the pages testbench writes. Every command below was run on a
Linux machine on 2026-11-01 with agent-testbench 0.2.0, and every output is that command's, shortened where a line
says `...`. The pictures are screenshots of the pages those same runs wrote; click one to see it full size.
[`docs/assets/make_screenshots.py`](https://github.com/sarptandoven/agent-testbench/blob/main/docs/assets/make_screenshots.py)
runs all of it again.

You type the commands here. With the Claude Code plugin or the MCP server, you ask in plain words and your agent
runs the same commands.

## 1. Install

```bash
pip install "agent-testbench[all]"         # or: uv tool install "agent-testbench[all]"
```

```
$ testbench --version
agent-testbench 0.2.0
```

(The pip line was not run for this page: the runs used 0.2.0 from a checkout of the repository.)

## 2. A project that works

`testbench demo` copies a small project into `testbench-demo/` - a Colab-style notebook that trains a digit
classifier with scikit-learn, kept as cell files in `cells/`, in a git repository - and runs its free plan:

```
$ testbench demo
Copied the tiny-model example to .../testbench-demo: a Colab-style notebook that trains a small network, and plans for this machine, Modal and Colab.

Running its free plan, `smoke` (lint, then a quick train on this machine):

[testbench] run 20261101-081917-smoke started (Within budget: up to $0.00 (today so far $0.00 of $2.00; $0.00 of $20.00 on this machine).)
...
# PASS - plan `smoke` on local

Run `20261101-081917-smoke`, started 2026-11-01 08:19, 0.05 min, about $0.000.
Code `07c166fcacac`; Python 3.13.17 on x86_64; evidence sha256 `9abdc9c42f7b5f39` (`testbench verify` checks it).
...
- train quick: outputs/metrics.json: accuracy >= 0.85 - 0.9756 (wanted >= 0.85)
...
```

The plan is in `testbench-demo/testbench.yaml`. "Working" means: the notebook lints clean, runs top to bottom in a
fresh kernel, writes its metrics and its plot, and reaches 0.85 accuracy.

```yaml
  smoke:
    backend: local
    description: lint, then a quick train on this machine (seconds, free)
    steps:
      - name: lint
        run: testbench lint tiny_model.ipynb --strict
        max_minutes: 1
      - name: train quick
        notebook: tiny_model.ipynb
        params: {EPOCHS: 15}
        max_minutes: 5
        artifacts: [outputs/*]
        expect:
          files: [outputs/metrics.json, outputs/confusion.png]
          json:
            outputs/metrics.json: {accuracy: ">= 0.85", epochs: "<= 15"}
```

## 3. Plant a bug

The kind an agent can make without noticing, because nothing crashes: in `testbench-demo/cells/02_settings.py`,
change `LEARNING_RATE = 0.01` to `LEARNING_RATE = 1.0`. Then rebuild the notebook from its cells:

```
$ cd testbench-demo
$ testbench nb build cells tiny_model.ipynb
built tiny_model.ipynb
```

## 4. The run that catches it

```
$ testbench run smoke
[testbench] run 20261101-081922-smoke started (Within budget: up to $0.00 (today so far $0.00 of $2.00; $0.00 of $20.00 on this machine).)
...
# FAIL - plan `smoke` on local

Run `20261101-081922-smoke`, started 2026-11-01 08:19, 0.06 min, about $0.000. Stopped because step 2 (train quick) failed.
Code `07c166fcacac` with uncommitted changes; Python 3.13.17 on x86_64; evidence sha256 `43f6494a597dea32` (`testbench verify` checks it).

## FAIL: train quick

- expected outputs/metrics.json: accuracy >= 0.85: 0.1711 (wanted >= 0.85)
...
## Compared with the previous run `20261101-081917-smoke` (passed)

- Broke: train quick
- train quick | outputs/metrics.json: accuracy >= 0.85: 0.9756 -> 0.1711
...
```

??? note "The whole output of that command, not shortened (a picture of the real output)"

    [![The full terminal output of testbench run smoke with the planted bug, as captured from the command: progress lines for both steps and every cell, the cell 3 metrics with accuracy 0.1711 and learning_rate 1.0, then the report - FAIL, plan smoke on local; stopped because step 2 (train quick) failed; code 07c166fcacac with uncommitted changes; expected accuracy >= 0.85: 0.1711; the last output; compared with the previous run: Broke: train quick, accuracy 0.9756 -> 0.1711; the steps table; the evidence to look at; the passed check.](assets/terminal-fail.png)](assets/terminal-fail.png)

The report leads with what failed and what it found instead, says the code had uncommitted changes, and says what
changed since the last run. The same report as a page:

```
$ testbench report --format html
wrote .../testbench-demo/.testbench/runs/20261101-081922-smoke/report.html (one self-contained page: open it in a browser)
```

[![The HTML report of the failing run, first screen: a red FAIL badge for plan smoke, stopped because step 2 (train quick) failed; facts - local, $0.000, worst case $0.00, code 07c166fcacac + uncommitted changes, Python 3.13.17, 11 evidence files; FAIL: train quick, expected outputs/metrics.json: accuracy >= 0.85: 0.1711 (wanted >= 0.85); compared with the previous run (passed): Broke: train quick, accuracy 0.9756 -> 0.1711; steps: lint PASS, train quick FAIL with 4 of 5 checks.](assets/report-fail.png)](assets/report-fail.png)

## 5. Fix it, and prove the fix

Set `LEARNING_RATE = 0.01` again, rebuild, and run the same plan:

```
$ testbench nb build cells tiny_model.ipynb
built tiny_model.ipynb
$ testbench run smoke
...
# PASS - plan `smoke` on local

Run `20261101-081926-smoke`, started 2026-11-01 08:19, 0.05 min, about $0.000.
Code `07c166fcacac`; Python 3.13.17 on x86_64; evidence sha256 `ef9577f86b99ebae` (`testbench verify` checks it).

## Compared with the previous run `20261101-081922-smoke` (failed)

- Fixed: train quick
- train quick | outputs/metrics.json: accuracy >= 0.85: 0.1711 -> 0.9756
...
$ testbench verify
20261101-081926-smoke: all 11 evidence files match their recorded sha256 (ef9577f86b99ebae); ran on commit 07c166fcacac
$ testbench report --format html
wrote .../testbench-demo/.testbench/runs/20261101-081926-smoke/report.html (one self-contained page: open it in a browser)
```

That is the proved fix: a run testbench started itself, in a fresh kernel, on commit `07c166fcacac` with no
uncommitted changes; every check passed; its 11 evidence files still match their recorded sha256. Its page has the
plot inline:

[![The HTML report of the run after the fix: a green PASS badge for plan smoke; facts - local, $0.000, code 07c166fcacac, Python 3.13.17, 11 evidence files; compared with the previous run (failed): Fixed: train quick, accuracy 0.1711 -> 0.9756; steps: lint and train quick PASS; checks: all six ok, accuracy 0.9756 (wanted >= 0.85); evidence to look at: the notebook's confusion matrix plot, titled accuracy 0.976, with nearly every test digit on the diagonal.](assets/report-pass.png)](assets/report-pass.png)

## 6. Let a loop try ideas

Optional, and still free. A loop works on a branch of its own: each try is committed, proved by a run of the plan,
and kept only if the run passes and the number beats the best so far. Before each try, `cells/02_settings.py` was
edited (first `HIDDEN = 16`; then `HIDDEN = 128` and `LEARNING_RATE = 0.02`) and the notebook rebuilt with
`testbench nb build cells tiny_model.ipynb`:

```
$ testbench loop start smoke --metric outputs/metrics.json:accuracy --budget-usd 0
[testbench] loop 20261101-081931-smoke started on branch testbench-loop/20261101-081931-smoke; running the baseline (run 20261101-081931-smoke)
...
BASELINE: accuracy 0.9756, run 20261101-081931-smoke passed.
$ testbench loop try 'hidden units 64 -> 16'
...
REVERTED try 1: accuracy 0.9578 is not better than the best 0.9756. The branch is back at 07c166fca.
$ testbench loop try 'hidden units 64 -> 128, learning rate 0.01 -> 0.02'
...
KEPT try 2: accuracy 0.98 (best was 0.9756, +0.0044). Run 20261101-081940-smoke passed. Best is now cb99b4e1a.
$ testbench loop stop
# Loop `20261101-081931-smoke` on plan `smoke`: accuracy 0.9756 -> 0.98 (max)

2 tries, 1 kept, $0.000 of $0.00, 0.0 h. Branch `testbench-loop/20261101-081931-smoke` from `master` at `07c166fca`.
...
- the proof for the best result: run `20261101-081940-smoke`

(report: .testbench/loops/20261101-081931-smoke/report.md; back on master)
```

Merging the branch is yours to decide. More in [loops](loops.md).

## 7. Every run as a page

```
$ testbench history smoke
plan smoke: 6 run(s), 5 passed; latest 20261101-081940-smoke passed
...
checked numbers, oldest to newest (- where a run did not reach the check):
  train quick | outputs/metrics.json: accuracy >= 0.85: 0.9756 -> 0.1711 -> 0.9756 -> 0.9756 -> 0.9578 -> 0.98
...
$ testbench ui
wrote .../testbench-demo/.testbench/ui/index.html (static pages: open it in a browser; each run's page is report.html in its folder)
```

Static pages, no server: open the file in a browser.

[![The testbench ui index for the tiny-model project: 6 runs, 5 passed, $0.000; a plans table where smoke's latest run passed, 83% of its 6 runs passed, with a trend line for accuracy that dips once (the planted bug) and ends at 0.98, and a flat one for epochs; full-modal, gpu-check and colab-cpu never run; a runs table, newest first, with the one FAIL on code 07c166fca+ (uncommitted changes); the loop, stopped, best accuracy 0.98, 2 tries, 1 kept.](assets/ui-index.png)](assets/ui-index.png)

Each run links to its page. The loop's best run, `20261101-081940-smoke`, scrolled to the end, with its list of
evidence files opened - what `testbench verify` checks, and where the run ran:

[![The end of a run's page: the 11 evidence files with their sizes and sha256 (logs, result.json, the executed notebook, the plots, metrics.json), then the provenance - the commit on branch testbench-loop/20261101-081931-smoke, not dirty, no untracked files; Python 3.13.17 CPython on Linux x86_64, the interpreter's path, 182 packages and their sha256; and the sha256 over all the evidence.](assets/ui-run.png)](assets/ui-run.png)

## Next

- Your own project: `testbench init` looks at it and writes plans that already check something -
  [getting started](getting-started.md).
- Hand it to your agent: the Claude Code plugin, or the MCP server in any client - [quick start](index.md#quick-start).
- The same plan on a GPU, in a Docker sandbox or over ssh: [backends](backends.md). In every pull request:
  [the GitHub Action](github-action.md).
- What a report proves, and what it does not: [proof records](proof-records.md), [why agent-testbench](why.md).
