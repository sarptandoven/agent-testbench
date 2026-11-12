# Proof records

<!-- "What a record holds" and "The run folder" are docs/reference.md's "Proof records" and "Run folder": edit them
     there. The rest is this page's own. The screenshots are made by docs/assets/make_screenshots.py from the runs
     of docs/tour.md. -->

Each report names what it proves: the git commit it ran (and a hash of any uncommitted changes), the Python,
platform and packages where it ran, and the sha256 of every evidence file, in a format published as a
[JSON Schema](../src/agent_testbench/schema/report-v1.schema.json). `testbench verify` says whether the evidence
still matches, and `testbench diff A B` says what changed between two runs - each checked number old -> new and
whether that is better by its rule, checks that flipped, evidence that changed, the commits, packages that changed
version.

The same record, in the forms people read:

- `testbench report --format html` writes one page with the plots inline, and `--format junit` feeds CI test
  reports.
- `testbench history <plan>` shows how the checked numbers moved, and `testbench ui` writes static pages for every
  run with a trend line per number; `testbench ui --serve` shows them live at http://127.0.0.1:8770/ - what is
  running, its log as it grows, sessions, the loop, and today's spend against your limits - read-only and for this
  machine only ([live dashboard](dashboard.md)).
- `testbench watch smoke` reruns a free plan whenever you save, one run at a time ([watch](watch.md)).
- `testbench export --to mlflow|wandb|csv|jsonl` (or `export:` in testbench.yaml, after every run) puts runs where
  you already track experiments, with the evidence attached ([exports](export.md)).
- `testbench share --to git:gh-pages/runs` publishes a run as a page anyone can open, with a README badge, and
  `testbench verify --from URL` checks it there - no project needed ([share a run](share.md)).

`testbench ui` for the demo project after the runs of [the tour](tour.md) (a screenshot):

[![The testbench ui index for the tiny-model project: 6 runs, 5 passed, $0.000; a plans table where smoke's latest run passed, 83% of its 6 runs passed, with a trend line for accuracy that dips once and ends at 0.98, and a flat one for epochs; three plans never run; a runs table, newest first, each with its plan, where it ran, minutes, cost and commit, the one FAIL marked as run on uncommitted changes; and the loop: stopped, best accuracy 0.98, 2 tries, 1 kept.](assets/ui-index.png)](assets/ui-index.png)

## What a record holds

--8<-- "docs/reference.md#proof-records"

The end of a run's page, `report.html` in its folder, with its list of evidence files opened (a screenshot of the
loop's best run in the tour):

[![The end of a run's page: the 11 evidence files with their sizes and sha256 (logs, result.json, the executed notebook, the plots, metrics.json), then the provenance - the commit on its branch, not dirty, no untracked files; Python 3.13.17 CPython on Linux x86_64, the interpreter's path, 182 packages and their sha256; and the sha256 over all the evidence.](assets/ui-run.png)](assets/ui-run.png)

## The run folder

--8<-- "docs/reference.md#run-folder"

## Checking a run away from its project: verify --from

`testbench verify` checks a run in its project. A run published with [`testbench share`](share.md) - a folder, a
branch for GitHub Pages, a bucket - is checked from its address or a downloaded copy, with no project at all:

```
$ testbench verify --from https://ada.github.io/proj/runs/20261112-094032-p/
https://ada.github.io/proj/runs/20261112-094032-p/: run 20261112-094032-p (plan p, passed): all 8 files the manifest lists match it; 5 of the run's 8 evidence files were published, each matching the sha256 the run recorded, and the run's list adds up to its evidence_sha256 66f75f520455b8e8 (3 left out when it was shared: the manifest says why).
This shows the files are the ones published and agree with the report. Someone who rewrote report.json together with the evidence would pass it too, so compare that evidence_sha256 with the one the run printed when it ended (in its report.md, the CI log, or the commit that published it).
```

It checks every file against the bundle's `testbench-manifest.json` (sha256 and size), the report's list of evidence
hashes against its `evidence_sha256`, and each published evidence file against the sha256 the run recorded. Exit 0
when it all holds, 1 naming each file that does not (`changed: ...`, `missing: ...`, `not in the manifest: ...`), 2
when it could not be checked. Evidence left out when the run was shared (a file holding a secret's value or this
machine's paths) is counted, not checked: the manifest says why each was left out.

## Comparing two runs: testbench diff

Every report already ends with what changed since the previous run of its plan: steps fixed or broken, checked
numbers that moved. `testbench diff` is that comparison in full, for any two runs:

```bash
testbench diff                  # the latest run against the run of its plan before it
testbench diff smoke            # a plan: its last two finished runs
testbench diff last~1 last      # any two runs, older first: ids, prefixes, last, last~N (N runs before the latest)
testbench diff A B --json       # the same as data
```

A real one, after a commit that set a score from 3 to 1:

````
$ testbench diff q
# BROKE: plan `q` went from PASS to FAIL

A is run `20261101-082845-q` (passed, 2026-11-01 08:28); B is run `20261101-082848-q` (failed, 2026-11-01 08:28).

## Steps

- Broke: score (passed -> failed)

## Checked numbers

- score | out/m.json: score >= 2: 3 -> 1 (-2; worse: the rule wants higher)
- score | out/m.json: loss < 0.5: 0.25 -> 0.5 (+0.25; worse: the rule wants lower)
- score | out/m.json: near ~= 1 +- 0.5: 1 -> 0.333333 (-0.6667; worse: the rule wants closer to 1)

## Checks

- Now failing: score: out/m.json: score >= 2 - 1 (wanted >= 2.0)
- Now failing: score: out/m.json: loss < 0.5 - 0.5 (wanted < 0.5)
- Now failing: score: out/m.json: near ~= 1 +- 0.5 - 0.3333333333333333 (wanted 1 ± 0.5; differs by -0.6667)

## Evidence

By sha256: 3 changed, 0 added, 0 removed, 0 the same.
- changed: `steps/01-score/artifacts/out/m.json`
- changed: `steps/01-score/log.txt`
- changed: `steps/01-score/result.json`

## Cost and time

- cost: $0.000 -> $0.000
- minutes: 0 -> 0

## Code

A ran commit 14577bb2ad49 with no uncommitted changes; B ran commit 082b4ca7582f with no uncommitted changes; B is 1 commit(s) after A.

```
 settings.py | 2 +-
 1 file changed, 1 insertion(+), 1 deletion(-)
```

## Where it ran

- the same: local, CPython 3.13.17 on Linux-6.18.44-fc-v185-x86_64-with-glibc2.39 (x86_64), 179 packages at the same versions
````

What each part reads from the two reports:

| part | what it says |
|---|---|
| verdict | FIXED, BROKE, STILL PASSING or STILL FAILING |
| Steps | each step's status in A and in B; steps only in one of them (the plan changed, or a step was skipped) |
| Checked numbers | every number a check found, A -> B, the difference, and better or worse when the rule has a direction: `>=` and `>` want higher, `<=` and `<` lower, `~= X +- T` closer to X; `==` and `between` say none |
| Checks | checks that now fail or now pass, and checks only one run made |
| Evidence | files added, removed or changed, by the sha256 each report recorded (logs change on every run) |
| Cost and time | the estimated cost and the minutes of each |
| Code | the commit and uncommitted changes (their hash) of each; when both commits are in the repository, how far apart they are and `git diff --stat` between them, for the project's folder. Uncommitted changes are hashed, not kept, so only their being different can be told |
| Data | with [`data:`](trading-data.md), each role: whether the file changed between the runs (sha256, rows, last time) or the steps saw other rows of the same file (a backtest against the holdout's run); left out without `data:` |
| Where it ran | backend, GPU, Python, platform, machine, and the packages that changed version (runs from before testbench recorded versions say only whether the package set differs) |

`--json` gives the same as data: `verdict`, `fixed`, `broke`, `still_failing` (those three are the report's own
`delta`), `steps`, `numbers` (each with `a`, `b`, `delta`, `wants`, `change`), `checks`, `evidence`, `cost_usd`,
`minutes`, `code` and `environment`. MCP clients have it as the `diff_runs` tool, and from Python it is
`agent_testbench.diff.compare(report_a, report_b, root)`. `testbench watch` prints its one-line summary after each
run ([watch.md](watch.md)).

When the two runs are of the same plan on the same code and data and their outputs still differ, the Evidence part
says so and names the command that reads each difference: `testbench repro --compare A B` - which files differ, how,
and why (times, paths, order, a random seed, the platform). [Reproducibility](reproducibility.md) has it, and
`testbench repro PLAN`, which runs a plan again to see whether it gives the same evidence.
