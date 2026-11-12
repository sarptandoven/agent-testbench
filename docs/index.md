<!-- The home page. Its first screen is written here, for someone arriving from a search or a link; everything
after it is README.md, put in section by section by docs/hooks.py at build time, so edit those sections in
README.md, not here. A new section of README.md needs its own two lines below to appear on the site; keep each
heading as README.md has it, because links to README.md#anchor land on this page (HOME in docs/hooks.py). -->

# agent-testbench

**Your coding agent says it works. A checked run shows whether it does.**

agent-testbench is a command-line tool, an MCP server and a Claude Code plugin. It gives a coding agent real
machines to run code on - this one, a Docker sandbox, any machine you reach with ssh, a Modal GPU or a Colab VM -
and a way to prove the result: a **plan** runs the notebook, script, test suite or web page end to end, checks it
against expectations you wrote, and writes a report that names the commit it ran, where it ran, and the sha256 of
every evidence file. Every run is priced before it starts and stopped on the machine at its time limit, inside a
budget the agent cannot raise.

**It is for** people who let Claude Code, Codex, Cursor or another agent change notebooks, training code, test
suites or web apps, and want "it works" to come with a run they can check.

```
$ testbench run smoke              # LEARNING_RATE = 1.0 now; nothing crashes
# FAIL - plan `smoke` on local
Run `20261101-064131-smoke`, ... Stopped because step 2 (train quick) failed.
Code `3a9dbfffcaec` with uncommitted changes; Python 3.13.17 on x86_64; ...
## FAIL: train quick
- expected outputs/metrics.json: accuracy >= 0.85: 0.1711 (wanted >= 0.85)
...
$ testbench run smoke              # after the fix
# PASS - plan `smoke` on local
...
## Compared with the previous run `20261101-064131-smoke` (failed)
- Fixed: train quick
- train quick | outputs/metrics.json: accuracy >= 0.85: 0.1711 -> 0.9756
...
$ testbench verify
20261101-064151-smoke: all 11 evidence files match their recorded sha256 (0ed53480dc2a5893); ran on commit 3a9dbfffcaec
```

That is the project `testbench demo` sets up, with a bad learning rate planted in it, run on a Linux machine on
2026-11-01 (abridged where a line says `...`). `testbench report --format html` makes a report a page; this is the
failing run's, from the same steps run again for this screenshot ([the five-minute tour](tour.md) has the rest):

[![Screenshot of the HTML report of the failing run: a red FAIL badge for plan smoke, stopped because step 2 (train quick) failed; facts - local, $0.000, worst case $0.00, the commit plus uncommitted changes, Python 3.13.17, 11 evidence files; FAIL: train quick, expected outputs/metrics.json: accuracy >= 0.85: 0.1711 (wanted >= 0.85); compared with the previous run (passed): Broke: train quick, accuracy 0.9756 -> 0.1711; steps: lint PASS, train quick FAIL with 4 of 5 checks.](assets/report-fail.png)](assets/report-fail.png)

A passing report says:

- **it ran, start to finish**: in a fresh process and kernel started by testbench, not in the agent's own session,
  with every cell, command or page action recorded;
- **it did what "working" means**: each expectation you wrote says what it found (`0.1711 (wanted >= 0.85)`);
- **on this code, on that machine**: the git commit (and a hash of any uncommitted changes), and the Python,
  platform and packages where it ran;
- **with this evidence**: the sha256 of every log, executed notebook, plot and file it kept, which
  `testbench verify` checks later;
- **for at most this much**: the worst case was checked against the budget before it started.

What it does not prove, and how it compares with CI, papermill or calling Modal yourself: [why](why.md).

<div class="grid cards" markdown>

-   **Try it in a minute**

    `pip install "agent-testbench[all]"`, then `testbench demo`: a small notebook project, run for free here.
    [Getting started](getting-started.md), or [the tour](tour.md) with screenshots

-   **See it used**

    Seven walkthroughs with real output: a failing suite in a sandbox, a model improved by a loop, a Colab
    notebook, untrusted code, a web UI, your own GPU box, CI. [Use cases](use-cases.md)

-   **Know what it is for**

    The problem, what a report proves and how, and a fair comparison with the other ways to do it.
    [Why agent-testbench](why.md)

-   **Ask before you install**

    Cost, safety, what an agent can and cannot do, what leaves your machine, platforms, agents.
    [FAQ](faq.md) and [Troubleshooting](troubleshooting.md)

</div>

## Quick start

--8<-- "README.md#quick-start"

## What you can do

--8<-- "README.md#what-you-can-do"

## Guardrails

--8<-- "README.md#guardrails"

## Install

--8<-- "README.md#install"

## Commands

--8<-- "README.md#commands"

## Documentation

--8<-- "README.md#documentation"

## How it is tested

--8<-- "README.md#how-it-is-tested"

## Limits

--8<-- "README.md#limits"

## License

--8<-- "README.md#license"
