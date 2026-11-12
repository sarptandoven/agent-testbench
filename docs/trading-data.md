# Sealed data: a holdout, walk-forward windows, fingerprints

A strategy an agent found is a claim. An agent that tunes a strategy against a price history until the numbers go up
has, by the end, tuned it to that history: every try that did worse was thrown away, and what is left fits the past
it was chosen on. The only honest test is data the tuning never saw - and an agent with a folder of CSV files sees
all of it.

testbench keeps that test honest. testbench.yaml names the data the plans read; the latest rows can be sealed, so
that every plan but one runs on a copy of the data without them; the plan that opens them is counted every time it
does; a walk-forward plan replays the strategy window by window with no row from the future; and every run records
exactly which bytes its steps saw.

```yaml
data:
  prices:
    path: data/prices.csv      # a CSV file with a time column, or a folder of CSV files
    time: time                 # the time column (default: the first of time, date, timestamp, datetime)
    holdout_from: 2025-07-01   # rows at or after this time are sealed

plans:
  backtest:                    # sees the rows before 2025-07-01 only
    steps:
      - {name: backtest, run: python backtest.py}
      - name: metrics
        run: python -m agent_testbench.trading.metrics out/equity.csv out/trades.csv --out out/metrics.json
        expect: {json: {out/metrics.json: {sharpe: ">= 1", trades: ">= 30"}}}
  walk-forward:                # the same steps once per window: 24 months of history, then 6 months of test
    walk_forward: {data: prices, train: 24mo, test: 6mo, step: 6mo}
    steps: [...]
  holdout:                     # opens the sealed rows: counted, and refused after the first opening
    holdout: {max_opens: 1}
    steps: [...]
```

[examples/sealed-backtest](../examples/sealed-backtest) is a project set up this way.

## data:

Each entry is a role - a name of letters, digits and `_`, such as `prices` or `features` - and its data:

| key | | |
|---|---|---|
| `path` | required | a CSV file, or a folder of CSV files, relative to the project. Written alone (`prices: data/prices.csv`), it is the path |
| `time` | | the time column; left out, the first of `time`, `date`, `timestamp`, `datetime` (any case) |
| `holdout_from` | | a date or time (ISO 8601): the rows at or after it are sealed |

A step finds each role's file (or folder) in `TESTBENCH_DATA_<ROLE>` (`TESTBENCH_DATA_PRICES`): the copy it may read.

The file is UTF-8 CSV with a header line and one row per line; other columns are anything. The times are ISO 8601,
as in [the trading files](trading.md#times) (`2025-07-01`, `2025-07-01T15:30:00`, `2025-07-01T15:30:00Z`), all with a
zone or all without. Every row is cut by its own time, so rows need not be in order (several symbols mixed are fine).
A `holdout_from` without a zone is compared with each time as it is written (its wall clock); with a zone, with zoned
times as instants.

A folder holds only `.csv` files (hidden files, and files `exclude:` leaves out of copies, aside): each is cut by
the same rule, and anything else in it would get past the seal, so a launch refuses it. Its fingerprint is a sha256
over each file's name and sha256.

testbench.yaml is checked when it loads, without reading the data - a file can arrive later. The data is read at
launch, once, in a single pass (and again only when it changes): a row whose time cannot be read, a quote that does
not close on its line, a missing time column or a missing file refuses the launch, exit 2, with the file, the line
and what to write. A row the seal cannot place could be a sealed one, so nothing runs until every row reads.

## The sealed holdout

Every plan but the one with `holdout:` sees each sealed file cut to the rows before `holdout_from`. It runs in a copy
of the project (`workdir: copy` is set for every plan of a project with sealed data; `workdir: inplace` there is
refused at load), and in that copy the file holds the header and the earlier rows only - the bytes of the original
lines, so a reader sees the same file, shorter. Your own file is never written.

One plan may have `holdout: true` (or `holdout: {max_opens: N}`): it sees every row, and each of its runs is an
opening. Openings are recorded in `.testbench/holdout.jsonl` - the run, when, the commit, and the data's sha256 and
last time - and every report of the holdout plan says how many times the sealed rows were opened before it:

```
Holdout (prices from 2025-07-01): this run is the first to open it, so results on it are untouched.
```

```
**Warning**: the holdout (prices from 2025-07-01) was opened 3 times before this run: results on it are no longer
untouched (first opened in run `20261103-101500-holdout`).
```

That is a warning, not a failure. With `max_opens`, an opening past it is refused at launch - exit 2, no run, nothing
in the ledger - with the fix:

```
Refused: plan holdout opens the holdout (the sealed rows: prices from 2025-07-01), which was opened 1 time already
(first in run 20261103-101500-holdout at 2026-11-03T10:15:00), and plans.holdout.holdout.max_opens is 1. A holdout is
an honest test only while nothing was tuned on it. Nothing ran and nothing was spent. To test on untouched rows, wait
for data newer than 2025-09-30 and move holdout_from past it (rows no opening has seen start the count again); or
have a person raise holdout.max_opens in testbench.yaml (every report still says how many times the holdout was
opened).
```

An opening counts against the current holdout when the file it opened reached into the sealed period: its last time
was at or after today's `holdout_from`. Moving `holdout_from` over rows that were already opened changes nothing; new
data, sealed by a later `holdout_from`, was never opened, so its count starts at 0.

A plan opens the holdout or walks forward, not both. Only one plan may open it, so its count covers every opening.

## Walk-forward windows

```yaml
walk_forward: {data: prices, train: 24mo, test: 6mo, step: 6mo}
```

| key | | |
|---|---|---|
| `data` | the only role | the role whose times the windows are cut from |
| `train`, `test` | required | lengths: a whole number and `d`, `w`, `mo` or `y` (`m` is refused: minutes or months?) |
| `step` | `test` | how far each window moves on from the one before |
| `anchored` | `false` | `true`: every train period starts at the first window's start and grows; `false`: it rolls forward |
| `start` | the data's | when the first train period starts |

The windows are cut from the rows before the seal (all rows without one), oldest first:

- The first train period starts at `start:`, else at the first row's day - its month's first day when a length is in
  `mo` or `y` (data from 2020-01-02 starts 2020-01-01).
- Window k trains on [start + k·step, + train) - from start itself when anchored - and tests on the `test` right
  after it. Periods are half-open: a train period holds the rows at or after its start and before its end.
- Windows are kept while the test ends at or before the end of the rows: `holdout_from`, or the day (month) after the
  last row. Data through 2025-06-30, 24mo / 6mo / 6mo: seven windows, the last testing 2025-01-01 to 2025-07-01.

The plan's steps run once per window, in order. In window k each data file holds only the rows before the window's
test end - no future - and every step gets:

| variable | |
|---|---|
| `TESTBENCH_WINDOW` | `k/n`, e.g. `3/7` |
| `TESTBENCH_TRAIN_START`, `TESTBENCH_TRAIN_END` | the train period (dates, or times with the data's zone) |
| `TESTBENCH_TEST_START`, `TESTBENCH_TEST_END` | the test period; TEST_START is TRAIN_END |

Each window is checked like a step: a step's `expect:` applies in every window. The steps are named
`backtest (window 3/7)` in the report, which has a line per window - its dates, rows and outcome - and the plan
passes only when every window does. A failing window stops the plan like any failing step (the rest are listed as
skipped); give the step `continue_on_failure: true` to run every window anyway.

The windows are cut at launch: the worst case is the steps' `max_minutes` times the windows (`testbench plans`
shows `2 step(s) x 7 walk-forward windows, up to 21 min`), `budget.check` prices that, and plan.json holds a copy of
the steps per window, so every backend's hard stop allows for all of them. Fewer than 2 windows, or more than 200, is
refused at launch with the fix (`testbench plans` says so too), not at load: telling needs the data's time range,
which loading testbench.yaml does not read.

## Fingerprints

Every run of a project with `data:` records each role in report.json (`data`), in report.md and in the HTML report:

| field | |
|---|---|
| `path`, `time`, `holdout_from` | as configured; `time` is the column found |
| `view` | `sealed` (the rows before the seal), `full` (the holdout plan, or no seal) or `windows` (cut per window from the rows before the seal) |
| `bytes`, `sha256`, `rows`, `first`, `last` | what the steps saw, measured where they ran |
| `file` | the whole file at launch: `bytes`, `sha256`, `rows`, `first`, `last` |
| `sealed` | the sealed rows at launch (`rows`, `sha256`, `first`, `last`); null without a seal |
| `note` | when the data changed between launch and the step, or nothing is sealed yet |

Each step's folder has `data.json` - what its data files held when it started (for a window, that window's cut) - so
the fingerprints are evidence too: `testbench verify` covers them like any other file. A walk-forward report's
`windows` have each window's dates, rows, outcome and data.

`testbench verify` also hashes each data file as it is now. When it is not what the run read, verify exits 1:

```
20261103-101500-backtest: data/prices.csv changed since this run: the run's numbers are about other data (sha256
3c1f04b29a8e7d55 then, 9a02bb1c0e4f6d18 now)
```

A file that is gone is a note, not a failure. `testbench diff` shows a data change between two runs (and when the
same file was read through another view, such as a backtest run against the holdout run):

```
## Data

- prices (`data/prices.csv`): the file changed between the runs (sha256 3c1f04b29a8e -> 9a02bb1c0e4f; 1500 -> 1544
  rows, last 2025-09-30 -> 2025-11-28), so their numbers are about different data
```

## What the seal enforces, backend by backend

The cut is real on every backend: the file a step is given holds no sealed row. Whether a step that goes looking can
find the sealed rows elsewhere depends on where it runs.

| backend | the sealed rows |
|---|---|
| `docker` | cut on this machine before the project is copied in: they never enter the container (nothing of this machine is mounted). A [live session](reference.md#live-sessions) on docker gets the same cut copy. With `docker.network: none` the step cannot fetch them from anywhere else either. This is the backend that enforces the seal - unless `docker.run_args` mounts a folder that holds them |
| `local` | the run's copy (`.testbench/runs/<id>/work`) is cut, but the step runs as you on this machine: it can still open the project's own file by its absolute path (`TESTBENCH_PROJECT_DIR` names the folder). The seal stops accidents - a script that loads `data/prices.csv`, a notebook that reads everything - and the fingerprint records what the step was given, not what else it opened. A live local session sees the project as it is |
| `ssh`, `kubernetes`, `colab` | the whole project is copied there, and the executor cuts the file in the run's copy before the first step |
| `modal` | the project is part of the container image (at `/project`, whole); the run's copy at `/work/project` is cut before the first step, but `/project` still holds every row: like local, a guard against accidents |

For a claim to hold against an agent set on getting around it, run the plans it tunes on docker with
`network: none`, and keep the holdout plan's launch (and `max_opens`) with a person.
