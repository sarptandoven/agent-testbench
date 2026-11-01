# Why agent-testbench

## The problem

Coding agents are good at changing code and at describing what they changed. When the work needs a real machine
(a notebook that trains something, a GPU, a test suite, a web page), three things go wrong.

**Results nobody ran.** "Accuracy is now 0.97" can come from a cell run once in a kernel full of leftover state,
from a metrics file an earlier run wrote, from a run on different code than the code in the pull request, or from
nothing at all. The claim and the evidence come from the same place: the agent's own account of what happened.

**Spending with no ceiling.** An agent that can type `modal run` or `colab new` can start a GPU and leave it, or
try "one more idea" all night. A limit written in a prompt is a request. What bounds the cost has to sit outside
the agent: in code that refuses the launch before anything starts, and on the machine doing the work, which stops
at its limit even when the laptop that started it is asleep.

**Notebooks that worked on one machine.** A Colab notebook that waits for `files.upload()`, writes to
`/content/...`, installs whatever pandas is newest that day, or depends on cells run out of order works for the
person who made it and fails everywhere else: in CI, on a GPU box, for the next person, for an agent.

## What testbench proves, and how

A **plan** is a list of steps - a notebook, a shell command or a web page - each with expectations: what has to
be true for it to count as working. `testbench run <plan>` hands the plan to a background driver: a fresh process
and a fresh kernel, not the agent's live session. The report is written from what happened there.

| a passing report says | what backs it |
|---|---|
| every step ran to the end | the executor runs a notebook cell by cell in a new IPython kernel (a command in bash, a page in headless Chromium) and records the first error with its cell and line |
| the result is what you said "working" means | every expectation becomes checks that say what they found - `0.1711 (wanted >= 0.85)` - and a malformed one stops the file from loading ([expectations](expectations.md)) |
| on this code | `source` in report.json: the git commit at launch, whether there were uncommitted changes, and a sha256 of the diff and the untracked files |
| on that machine | `environment`, recorded on the machine that ran the plan (Docker, ssh, Modal and Colab too): Python, platform, a hash of the installed packages |
| with this evidence | the sha256 of every file the run kept, and one over all of them; `testbench verify` lists what changed, went missing or was added |
| for at most this much | the worst case - hardware price times the steps' `max_minutes`, plus start-up - checked before launch against the project's budget and this machine's `~/.testbench/limits.yaml`, and a hard stop on the machine doing the work ([guardrails](../README.md#guardrails)) |
| this change made it better (loops) | a try is kept only when its run passes and the metric beats the best so far; otherwise the branch goes back to the best commit ([loops](loops.md)) |

The format is published as a JSON Schema ([proof records](proof-records.md)). This is what `verify` says after a
number in a run's evidence was changed by hand, and after it was put back (run on 2026-11-01):

```
$ testbench verify 20261101-064131-smoke
20261101-064131-smoke: the evidence no longer matches its report:
  changed: steps/02-train-quick/artifacts/outputs/metrics.json
$ testbench verify 20261101-064131-smoke
20261101-064131-smoke: all 11 evidence files match their recorded sha256 (7de805b890bbbd64); ran on commit 3a9dbfffcaec with uncommitted changes
```

### What it does not prove

- **That the expectations are the right ones.** A plan proves what it checks and nothing more. `testbench init`
  writes the checks it can (every cell runs, the files a notebook writes appear, every test passes) and leaves a
  `FILL IN` line for the number only you know.
- **That a report was not forged.** Reports are not signed. `verify` shows that the evidence files still match the
  hashes in report.json; someone who can write to the run folder could rewrite both. For a record a reviewer can
  trust, run the plan where the agent does not control the machine: in CI, with [the GitHub Action](github-action.md).
- **That every file came from this run.** `junit:`, `image:`, and `metrics:` on a file refuse a file last written
  before the run started. `json:`, `files:` and `text:` on a file read what is there, so a result file left over
  from an earlier run can satisfy them if the code stopped writing it. Have the code write its outputs on every
  run, or remove them in a first step.
- **That a live session was clean.** Sessions are for exploring; state built up by hand can hide a bug. Only a plan
  run counts, and the `iterate` skill tells the agent so.
- **Isolation, unless you choose it.** `local` runs in your project folder with your permissions, and the Claude
  Code hook is a safety net, not a sandbox. For code nobody has reviewed, use `backend: docker` with
  `network: none` ([what a container isolates, and what it does not](docker.md#what-it-isolates-and-what-it-does-not)).
- **The exact bill.** Costs are time times a published price. `testbench spend --modal` reads Modal's own bill;
  Colab's unit rates vary by account.

## Compared with other ways to do it

Each of these does something better than testbench. Most of them also fit inside it or next to it.

### Doing it by hand

You run the notebook or the tests yourself and read what comes out.

- **Better at:** judgment. You notice a plot that looks wrong, a warning nobody wrote a check for, a number that
  passes but should not. Nothing to set up.
- **What testbench adds:** the same run, repeatable while you are away (an agent can rerun it after every change,
  all night), with the commit and the evidence recorded, a comparison with the previous run, and a cost bound you
  set once.
  It does not replace looking: the report lists the plots and screenshots to look at, and `testbench report
  --format html` puts them on one page.

### Plain CI

A workflow that runs `pytest` (or a notebook) on every push.

- **Better at:** being the shared gate. Required checks and branch protection, matrices across operating systems
  and versions, caching, secrets management, a history everyone in the repository already reads. It proves that a
  commit passes the tests you wrote, on a machine nobody's agent controls.
- **What testbench adds:** the same check before a push, in the agent's loop, on the hardware the code needs (a
  GPU on Modal or your own box over ssh, where hosted runners are CPU machines unless you pay for GPU runners or
  host your own); checks on numbers, plots, pages and GPU use rather than only a test exit code; a worst-case cost
  checked before a GPU run starts. The two combine: [the GitHub Action](github-action.md) runs a plan in CI and
  makes its report the job summary.

### papermill

Runs a Jupyter notebook headless with parameters and saves the executed copy.

- **Better at:** being a small, established library for exactly that: any Jupyter kernel (not only Python),
  notebooks read from and written to cloud storage, and a Python API that schedulers and pipelines already call.
- **What testbench adds:** a verdict. papermill executes; deciding whether the result is good is up to code you
  write. testbench checks the notebook's outputs against expectations, sets Colab form fields (`NAME = value
  #@param`) where papermill injects a parameters cell, answers `files.upload()` and `userdata.get()` with a
  headless `google.colab` stand-in, lints for what breaks on Colab, runs the notebook on other machines, and bounds
  the cost. It runs Python notebooks only (an IPython kernel).

### Calling Modal or Colab directly

`modal run`, a Modal function of your own, or a Colab runtime.

- **Better at:** everything else those platforms do. Modal: web endpoints, schedules, many containers at once,
  volumes, its own dashboard, logs and billing. Colab: the notebook in a browser, widgets, sharing with a person.
  testbench does not drive the Colab or Modal Notebooks web pages, and Colab VMs can be reclaimed after an hour or
  two, so long jobs belong on Modal or ssh.
- **What testbench adds:** a limit per project that the agent cannot raise. A token or a login lets whoever holds
  it start anything the account allows; testbench prices each launch first, refuses it over the budget, asks a
  person above a threshold, and makes Modal's own timeouts (or a watchdog on the Colab VM) the hard stop. It also
  keeps the evidence of each run in one place. In a project with the Claude Code plugin, the guard hook refuses
  raw `modal run` and `colab new` for exactly this reason.

### LLM-eval frameworks

Tools that score a model's or an agent's answers against a dataset, with graders such as exact match or another
model acting as judge.

- **Better at:** the question they ask: how good are these outputs, across many prompts, models or versions.
  testbench has no datasets, no graders of free text and no model-as-judge.
- **What testbench adds:** a different question - did the code this agent changed actually run, on real
  hardware, and produce this number? The two fit together: an eval harness can be a step (`run: python eval.py`)
  whose score file a `json:` or `metrics:` expectation checks, with the run's cost bounded and its evidence kept.

## Where to go next

- [Use cases](use-cases.md): seven walkthroughs with real output.
- [Getting started](getting-started.md): a first run on a real project, step by step.
- [FAQ](faq.md): cost, safety, privacy, platforms and agents.
