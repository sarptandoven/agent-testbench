# Sealed data: a holdout, walk-forward windows, fingerprints

A strategy an agent found is a claim. An agent that tunes a strategy against a price history until the numbers go up
has, by the end, tuned it to that history: every try that did worse was thrown away, and what is left fits the past
it was chosen on. The only honest test is data the tuning never saw - and an agent with a folder of CSV files sees
all of it.

testbench keeps that test honest. testbench.yaml names the data the plans read; the latest rows can be sealed, so
that every plan but one runs on a copy of the data without them; the plan that opens them is counted every time it
does; a walk-forward plan replays the strategy window by window with no row from the future; a look-ahead plan
catches a strategy that uses tomorrow's rows today; a model cutoff flags results a language model may already know;
and every run records exactly which bytes its steps saw.

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
  lookahead:                   # the steps again on data cut earlier: a decision that changes used tomorrow's rows
    lookahead: {data: prices, output: out/signals.csv, cuts: 4}
    steps: [...]
  holdout:                     # opens the sealed rows: counted, and refused after the first opening
    holdout: {max_opens: 1}
    steps: [...]
```

[examples/trading](../examples/trading) is a project set up this way (`testbench demo trading`).

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

## Look-ahead

A strategy that uses tomorrow's data today - a signal on the next day's close, an average centred on today, a
threshold computed over the whole history, a `shift(-1)` - backtests beautifully and trades badly. Its decisions give
it away: a decision made with only the rows up to its own day does not change when the later rows are cut off.

```yaml
plans:
  lookahead:
    lookahead:
      data: prices                 # the data: role to cut (default: the only one)
      output: out/signals.csv      # where the steps write what the strategy decided
      cuts: 4                      # how many reruns on data cut earlier (default 4, at most 20)
    steps:
      - {name: backtest, run: python backtest.py}
```

testbench runs the steps once on the role's data as the plan sees it (the rows before the seal), keeps `output`, then
reruns the steps `cuts` more times, each with the data cut at an earlier time, and compares every decision dated
strictly before each cut with the plan's own run. Any difference fails the plan, with the first one in words:

```
look-ahead: AAPL at 2024-03-04 was `1` with data up to 2025-06-30 but was `0` with data before 2024-03-05 - the
strategy used rows after 2024-03-04 to decide it
```

A row in one run and not the other is a difference too (`... but had no row with data before ...`); with several
decision columns the finding names the column (`AAPL at 2024-03-04 (weight) was ...`).

**The output.** A UTF-8 CSV file with a header: a time column (the first of `time`, `date`, `timestamp`,
`datetime`, ISO 8601 as in the data), a `symbol` column when there are several instruments, and one or more decision
columns - every other column is one (a signal, a target weight, an order size). Rows are matched by time and symbol
(and order, when a time and symbol repeat). Every run must write it, even with no decision (the header alone).
Values compare as text, except numbers: two numbers within a relative 1e-9 of each other (or 1e-12 of 0) are the
same decision, so a last-digit difference from adding up in another order is not look-ahead; `nan` equals `nan`.

**The cuts.** They are times of rows of the role's data, spread evenly over the times past the first 20% of the
rows, the last of them at the last time (a rerun cut there lacks only the last rows, so a strategy that peeks only
near the end is caught too); with 4 cuts, near 40%, 60%, 80% and 100% of the rows. No cut ever leaves fewer than 20% of the rows, and the same data always gives the same cuts: nothing
random. Data with fewer distinct times past the first 20% than `cuts` is refused at launch, with the fix. A strategy
that peeks a single row ahead shows it only on the last row before a cut; more cuts catch it more surely.

**Each rerun starts afresh.** Before it, the run's copy of the project is put back as it was before the first step,
`output` is deleted and the role's files are cut, so nothing a run wrote - a cached model, a file of results computed
on every row - reaches the next. The steps are not told which run they are in. Each run's `output` is kept as
evidence in `steps/lookahead/` (`full.csv`, `cut-1.csv`, ...), and each step's `data.json` holds its cut.

**What is not look-ahead.** A rerun's steps are checked only for running (their exit code; for a notebook, no cell
error) and keep no `artifacts:`: the plan's own run is the one graded, and a rerun on less data may well miss a
number the full run makes. A rerun that crashes, or writes no `output`, stops the plan and is reported as a crash,
not as look-ahead:

```
look-ahead not checked: the rerun with data before 2023-03-14 (cut 1/4) crashed - step `backtest (look-ahead cut 1/4)`
failed: exited 3 (its log: steps/02-backtest-look-ahead-cut-1-4/log.txt). That is a crash, not look-ahead: make the
steps run on less data (this rerun had 362 rows), then run the plan again
```

When the plan's own run writes no `output`, no rerun runs: `look-ahead not checked: the plan's own run wrote no
out/signals.csv, the file lookahead.output names, so there is nothing to compare. ...`

**Cost.** The steps run 1 + `cuts` times: the worst case counts them all (`testbench plans` shows `1 step(s) x 5
runs (look-ahead: the plan's run and 4 reruns on data cut earlier)`), `budget.check` prices that, and plan.json holds
a copy of the steps per rerun (`backtest (look-ahead cut 2/4)`), so every backend's hard stop allows for all of them.

**With the seal.** A look-ahead plan runs in a copy of the project (`workdir: inplace` is refused). It does not open
the holdout - `lookahead:` with `holdout:` is refused at load, as it would open the sealed rows 1 + `cuts` times - and
it does not walk forward: `lookahead:` with `walk_forward:` is refused too (every window would run 1 + `cuts` times,
and the strategy's code is the same in every window). Put `lookahead:` on a plan of its own with the same steps.

The report has a Look-ahead section - each rerun's cut, its rows, the rows compared and the differences, and the
verdict - and report.json a `lookahead` object (`verdict`: `clean`, `look-ahead`, `crashed` or `not checked`; `ok`;
`finding`; `runs`, each with `before`, `data_rows`, `status`, `rows_compared`, `differences`, `first_difference`).

## Model cutoff

A strategy that asks a language model about the market is graded on history the model may have read about: a model
trained on news up to October 2024 knows how 2023 ended. Its backtest on 2023 says nothing about its judgement.

```yaml
plans:
  backtest:
    model_cutoff: 2024-10-01       # the training cutoff of the model the strategy asks
    steps: [...]
  strict:
    model_cutoff: {date: 2024-10-01, require_after: true}
    walk_forward: {data: prices, train: 12mo, test: 3mo}
    steps: [...]
```

`model_cutoff` is your statement (or your agent's) of the last day the model may know - testbench cannot know which
model a step asks or what it was trained on, and it is not set for you when a step lists `mocks:`. A date means that
whole day; a time, that instant. The plan needs `data:`, whose dates it is compared with.

Every report of the plan compares the cutoff with its test periods: each walk-forward window's test period, the
sealed rows for the plan that opens the holdout, else every row the steps saw. A period counts as evidence when it
starts after the cutoff. When one does not, the report has a warning, not a failure:

```
**Warning**: results on data up to 2024-06-30 may use what the model already knew (model_cutoff 2024-10-01); only
windows after the cutoff, or a forward paper record, count as evidence.
```

Data that runs past the cutoff is still mixed (`results on data from 2020-01-01 to 2025-06-30 include days up to
model_cutoff 2024-10-01, which the model may already know; ...`). Walk-forward windows are marked one by one
(`after_cutoff` in report.json; the report lists the windows that count):

```
**Warning**: results on windows 1/7 to 5/7 (test periods from 2022-01-01 to 2024-07-01) may use what the model
already knew (model_cutoff 2024-03-15); only windows after the cutoff (windows 6/7 to 7/7 here), or a forward paper
record, count as evidence.
```

With `require_after: true`, a run with no test period after the cutoff fails (`... - and model_cutoff.require_after is
true, so with no test period here starting after the cutoff this run fails. ...`). When every test period is after
it, the report says so: `model cutoff 2024-10-01: the results here are on data from 2025-01-02 on, after it, which
the model cannot have been trained on`. report.json has a `model_cutoff` object (`date`, `require_after`, `level`:
`ok`, `warning` or `failed`, `ok`, `finding`, `periods`).

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
