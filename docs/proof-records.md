# Proof records

<!-- From README.md, "Proof records", and docs/reference.md, "Proof records" and "Run folder". Edit them there.
     "Comparing two runs" is this page's own. -->
--8<-- "README.md#proof-records"

## What a record holds

--8<-- "docs/reference.md#proof-records"

## The run folder

--8<-- "docs/reference.md#run-folder"

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
| Where it ran | backend, GPU, Python, platform, machine, and the packages that changed version (runs from before testbench recorded versions say only whether the package set differs) |

`--json` gives the same as data: `verdict`, `fixed`, `broke`, `still_failing` (those three are the report's own
`delta`), `steps`, `numbers` (each with `a`, `b`, `delta`, `wants`, `change`), `checks`, `evidence`, `cost_usd`,
`minutes`, `code` and `environment`. MCP clients have it as the `diff_runs` tool, and from Python it is
`agent_testbench.diff.compare(report_a, report_b, root)`. `testbench watch` prints its one-line summary after each
run ([watch.md](watch.md)).
