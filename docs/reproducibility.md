# Reproducibility

A passing run says the code did what the checks asked, once. `testbench repro` asks the next question: does the same
plan leave the same evidence again - here, on another machine, or for a colleague? Where the outputs are
deterministic their sha256 match; where they are not, the report says which files differ, how, and most often why.

```bash
testbench repro train                          # run plan train twice here, then compare the two runs
testbench repro train --times 3                # three runs
testbench repro train --on local --on docker   # once on this machine, once in a container
testbench repro --compare last~1 last          # runs that exist: ids, run folders, shared bundles
```

## testbench repro PLAN

Each run is a normal launch, so each has its report, its evidence and its line in the ledger, and each works in a
fresh copy of the project (as remote runs and loop tries do), so no run can see what another left in the folder. The
runs go one after another, and the command follows each for at most `--wait` seconds (default 540).

`--on BACKEND` sends the runs to that backend instead of the plan's own; given more than once, the runs take turns:
`--on local --on docker` runs the plan once on each, which is how you see whether a container, an ssh box or a GPU
machine gives the same files as yours. The plan has to be able to run there as written (a GPU the backend offers, its
section of testbench.yaml); if it cannot, nothing starts and the message says why.

**Money.** Every run is priced on the backend it goes to, and the total is checked once, before anything starts:
each run must fit `max_run_usd`, all of them together the day limits (the project's and this machine's), and above
`ask_above_usd` a person approves the total with `--approve` - one question for the whole repro, never one per run,
which would hide the total behind small ones. A plan whose one run is allowed can still need approval for a repro:

```
$ testbench repro paid
Needs approval: a repro of plan paid (2 runs) could cost up to $0.40 (above budget.ask_above_usd $0.30). Ask a person; only they approve it, with `testbench repro paid --approve`.
```

In Claude Code the guard hook asks the person before an agent runs `testbench repro ... --approve`. A plan that opens
the sealed holdout is refused (a repro would open it again for every run); compare its runs that exist instead.

Exit codes: 0 the outputs are the same (byte for byte, or apart from times, paths, ids, order or encoding) and every
run passed, 1 they are not, or a run did not pass (a failing run proves nothing, however reproducible), 2 refused or bad input (nothing ran), 3 a run is still going after `--wait` (the repro stops there and starts
no more runs; the message gives the `--compare` command for when it has ended).

## What it says

A real one: a plan whose step writes five files, three of them a little differently every run.

```
$ testbench repro train
[testbench] repro of plan train: 2 runs, one after another, each in a fresh copy of the project. Within budget: up to $0.00 (today so far $0.00 of $10.00).
[testbench] run 1 of 2: 20261112-105811-train on local
...

# Reproducibility: plan `train`, 2 runs - NOT REPRODUCED

NOT REPRODUCED: of 5 outputs, 2 identical, 3 differ.

- run 1: `20261112-105811-train` (passed, local)
- run 2: `20261112-105813-train` (passed, local)

## Where they ran

- the same in every run: backend local, python 3.13.17, implementation CPython, platform Linux-6.18.44-fc-v378-x86_64-with-glibc2.39, machine x86_64, the same packages

## Code and data

- the same code in every run: commit 224f448a6c23, no uncommitted changes

## Outputs: 2 identical, 3 differ, 0 missing

- DIFFERS `steps/01-make/artifacts/out/noise.json`: 1 key differs, numbers by up to 0.0108%: accuracy: 0.9301 -> 0.9302 (0.0108%) - numbers that differ by small relative amounts: a random seed not fixed
- DIFFERS `steps/01-make/artifacts/out/order.txt`: the same 5 lines, in another order (line 1 is the first that moved) - the same content in another order
- DIFFERS `steps/01-make/artifacts/out/stamp.txt`: line 1: 'made at 2026-11-12T10:00:01' -> 'made at 2026-11-12T10:00:02'; 1 of 2 lines differs - times or durations in the content
- identical in every run (2): `steps/01-make/artifacts/out/model.json`, `steps/01-make/artifacts/out/table.csv`

## testbench's records of each step: 2 identical, 0 differ, 0 missing

Logs and result.json hold times and the step's own account; they do not decide the verdict.

- identical in every run (2): `steps/01-make/log.txt`, `steps/01-make/result.json`

## What to do

- numbers that differ by small relative amounts: a random seed not fixed: fix every seed the code uses (random.seed, numpy.random.seed or default_rng(seed), torch.manual_seed, PYTHONHASHSEED) and pass it to anything that samples
- the same content in another order: sort before writing (sorted(), df.sort_values(), json.dump(..., sort_keys=True)): set and dict order, os.listdir and parallel workers change it
- times or durations in the content: keep times and durations out of files that must be the same (print them, or write them to a file of their own)

Kept in .testbench/repro/20261112-105811-train/ (repro.json, repro.md); `testbench ui` lists it.
```

It names the environments first (backend, GPU, Python, platform, machine, and the packages that changed version),
then the code (commit and uncommitted changes) and the data (with [`data:`](trading-data.md)): a difference between
runs on different code or data is no news, so the report says so before anything else.

**Outputs decide the verdict.** A step's outputs are what it made: its artifacts, the images a notebook showed, the
executed notebook, downloads. testbench's own records of a step - `log.txt`, `result.json`, `data.json` - are compared
too and shown apart, since they hold times by nature. A plan that keeps no files (no `artifacts:`) has its logs
compared as its outputs. The verdict is one of:

| verdict | when |
|---|---|
| IDENTICAL | every output has the same sha256 in every run |
| THE SAME apart from ... | the outputs that differ do so only in times, paths, ids, order or encoding |
| NOT REPRODUCED | an output differs in another way, or is missing in some runs |

With more than two runs, every run is compared with the first, and a file that differs says in which runs and how
many versions of it there are.

## How a difference is read

Every evidence file is compared by the sha256 each run recorded. A file that differs is read to say how:

| file | what is said |
|---|---|
| text | the first line that differs (both versions) and how many lines do |
| JSON, notebooks (`.json`, `.ipynb`) | which keys differ, both values, and for numbers by how much (relative difference) |
| CSV, TSV | which rows and which columns differ, and numbers by how much |
| images | the mean pixel difference, as [`image:`](expectations.md#image) measures it (needs Pillow) |
| anything else | the first byte that differs, and both sizes |

and why, when it can tell:

| reason | what it means | the rule |
|---|---|---|
| times or durations | dates, clock times, seconds since 1970, `1.2s`, `12 it/s`, `took 3.4` - or a number under a key or column named for a time (`elapsed_s`, `started_at`, `minutes`) | the same once those are blanked out |
| absolute paths | `/home/ada/proj/...`, `C:\Users\...`: the folder it ran in | the same once paths are blanked out |
| ids | memory addresses (`0x7f...`), uuids, process ids | the same once those are blanked out |
| order | the same lines, rows, list items or JSON keys, in another order | the same when sorted |
| encoding | the same content written differently: line endings, spacing, `1.0` and `1.00`, CSV quoting, the same pixels in another PNG | the same once read |
| rounding | numbers differ in their last digits (at most 1 part in a million) | the same but for the numbers, each within 1e-6 |
| a random seed not fixed | numbers differ by small relative amounts (up to 10%) | the same but for the numbers, each within 10% |
| the platform | as above, but the runs' environments differ, so the machine is the likely cause | rounding or a seed's amounts, on different environments |
| results not the same | numbers differ by more than 10% | the same but for the numbers |
| content | anything else | - |

Times, paths, ids, order and encoding leave the results the same; the others do not. A file can have several
reasons (a log line with a drifting loss and a duration has two); the report names each, the most telling first, and
says what to do about each one it found.

## Comparing runs that exist: --compare

```bash
testbench repro --compare last~1 last
testbench repro --compare 20261112-0916 20261112-0930 20261112-1002     # the first is the one the others are compared with
testbench repro --compare ./public/20261112-094032-train last           # a run a colleague shared, and yours
testbench repro --compare https://ada.github.io/proj/runs/20261112-094032-train/ last
```

Each run can be a run of this project (an id, a prefix, `last`, `last~N`), a run folder anywhere, or a bundle
[`testbench share`](share.md) made - its folder or its URL - so the same plan run by a colleague, in CI or on
another machine can be set beside yours. Only bytes a run recorded are read: an evidence file whose sha256 is no
longer the one in its report, or that was left out when a run was shared, is compared by its recorded sha256 only
and says so. Run `testbench verify --from` on a bundle first to check the bundle itself. Folders and bundles need no
project; the comparison is kept in `.testbench/repro/` when there is one.

`testbench diff A B` points here when the two runs are of the same plan on the same code and data, yet their
outputs differ.

## deterministic: files that must not change

`testbench repro` looks when you ask. The [`deterministic:`](expectations.md#deterministic) expectation checks on
every run that files which must be byte for byte the same are: each is compared with the same file in the latest
passing run of the plan on the same code (commit and uncommitted changes) and, with `data:`, the same data.

```yaml
expect:
  deterministic: [out/model.json, "out/*.csv"]
```

The first such run passes and says there was nothing to compare with yet; the files are kept as evidence (as if they
were in `artifacts:`), so it becomes the next run's reference. A run that differs fails, naming the file, the first
difference and the reason, as above.

## repro.json

`.testbench/repro/<id>/repro.json` (and `repro.md`, the report above): `verdict` (`identical`, `same`, `different`,
`nothing`), `reproduced`, `plans`, `runs` (each with `run_id`, `plan`, `status`, `backend`, `gpu`, `where`,
`evidence_sha256`, `commit`, and `problems` when its evidence no longer matches its report), `environment`
(`differences`, `packages`, `same_as_first`), `same_code`, `code`, `data_differs_in`, `outputs` and `records` (counts),
`reasons`, and `files`: each evidence file with its `group` (`output` or `record`), `status` (`identical`, `differs`,
`missing`), the `sha256` and `bytes` in each run, and for one that differs its `reasons`, a one-line `summary` and the
`pairs` (each run against the first: `kind`, `detail` with the keys, rows, cells or lines). A repro that launched runs
also has `launched` (`times`, `on`, `approved`, `worst_case_usd`). `--json` prints the same.

## Why it matters

A result anyone can rerun is harder to fake and easier to trust. The evidence hashes in a report prove the files were
not changed after the run ([proof records](proof-records.md)); a repro shows the run was not a one-off - the same plan,
on the same code, gives the same files - and when it does not, which outputs to treat as noise and which as results.
