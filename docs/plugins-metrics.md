# Metric readers

A metric reader turns evidence files into named numbers: `sharpe` from a backtest's equity.csv, `loss.last` from a
training log, `failures` from a JUnit report. A check can then name a number instead of matching a regex, and the
numbers are testbench's own reading of the files, not what the code under test printed about itself.

```
$ testbench numbers trading out/equity.csv out/trades.csv
trading read out/equity.csv, out/trades.csv:
  start_equity            100000
  total_return            0.01
  sharpe                  1.2345
  max_drawdown            -0.0498
  trades                  41
  ...
notes:
  - periods per year: 252 - daily bars; ...
```

`testbench numbers` with no reader lists the readers, the files each looks for and its options. With no PATH a
reader looks in the current folder and one folder down; `--opt KEY=VALUE` sets an option (`--opt risk_free=0.04`,
`--opt columns=[loss,accuracy]`), and `--json` prints `{reader, files, values, notes}`. It needs no testbench.yaml.
Exit 0 when it read the files, 2 when a file is missing or refused (the message says which and what to do).

## The built-in readers

| reader | reads | names |
|---|---|---|
| `json` | a JSON file (default `metrics.json`) | every number, boolean and null by dotted path, as the `json:` expectation names them: `accuracy`, `eval.f1`, `losses.0`; a list longer than 10 shows only `.0` and `.-1`. Option `keys`: only these paths (`losses.-1` is the last item) |
| `csv` | a CSV file with a header (default `metrics.csv`) | `rows`, and for each numeric column `COLUMN.last`, `.mean`, `.max`, `.min`. Options `columns`, `stats`. A column with a nan or infinite value has null for what it spoils |
| `junit` | a JUnit XML report (default `junit.xml`, `report.xml`) | `tests`, `passed`, `failures`, `errors`, `skipped`, read as the `junit:` expectation reads them; the failing cases are in the notes |
| `trading` | `equity.csv`, and `trades.csv` and `prices.csv` next to it when they are there ([trading](trading.md)) | testbench's own backtest numbers, every key of `agent_testbench.trading.metrics.KEYS`: `sharpe`, `sortino`, `max_drawdown`, `cagr`, `trades`, `win_rate`, `exposure`, ... Options `risk_free`, `periods_per_year`, `calendar`, `symbol` |

Each value is a finite number, `true`/`false`, or `null` where the number is not defined (no trades file, a NaN in
the file); the notes say why. A null passes no rule.

## In a plan (planned)

Checks will name a reader, its files and rules on its numbers. This is the shape planned for the next release; it is
not wired into `expect:` yet:

```yaml
expect:
  numbers:
    reader: trading
    files: {equity: out/equity.csv, trades: out/trades.csv}
    options: {risk_free: 0.04}
    rules: {sharpe: ">= 1", max_drawdown: ">= -0.2", trades: ">= 30"}
```

Each rule is written as every other expectation's (`>= 1`, `~= 0.93 +- 0.01`, `between 0.8 and 0.95`). A missing
file fails every rule with the reader's message. Until then, `testbench numbers` shows what each name will hold, and
from Python:

```python
from agent_testbench import metrics
got = metrics.read("trading", "path/to/run/folder", {"files": {"equity": "out/equity.csv"}, "risk_free": 0.04})
got["values"]["sharpe"], got["notes"], got["files"]
```

`metrics.read(name, workdir, opts)` raises `FileNotFoundError` naming a missing file and `ValueError` (a
`metrics.MetricsError`) for a file, option or reader it refuses - nothing else.

## A reader in a package

[examples/plugins/testbench-metrics-example](https://github.com/sarptandoven/agent-testbench/tree/main/examples/plugins/testbench-metrics-example)
adds `kvlog`, the last value of each `key=value` in a log, in about 40 lines:

```toml
# pyproject.toml
[project.entry-points."agent_testbench.metrics"]
kvlog = "testbench_metrics_example:KeyValueLog"
```

```python
from agent_testbench.metrics import Reader, listed

class KeyValueLog(Reader):
    name = "kvlog"
    what = "the last value of each key=value in a log (loss=0.25), and how many lines had one"
    files = {"log": ("metrics.log", "train.log", "*/metrics.log")}    # role -> patterns tried in order
    options = {"keys": "only these keys (default: every key in the log)"}

    def read(self, workdir, opts):
        path = self.path(workdir, opts, "log")      # the named file, else the patterns; FileNotFoundError if none
        ...
        return {**numbers, "notes": notes, "files": {"log": self.shown(workdir, path)}}

    def sample(self):
        return {"metrics.log": "epoch=1 loss=0.91 acc=0.52\nepoch=2 loss=0.25 acc=0.88\n"}
```

A reader has:

- `name`: lowercase letters, digits, `-` and `_`. It cannot take a built-in's name (it is skipped and reported).
- `files`: each file it reads, by role, with the glob patterns it tries in order, relative to the folder it reads.
  The first role must be found unless `required` names others. `opts["files"]` names files by role; a reader given
  paths on the command line takes them in the order of its roles.
- `options`: what `read()` takes besides the files, with one line each. An option it does not list is refused
  before it reads anything, with the closest name.
- `read(workdir, opts)`: `{name: value}`, each a finite `int` or `float`, a `bool`, or `None`, plus the reserved
  `notes` (plain-words caveats) and `files` (`{role: the path it read}`). A missing file raises
  `FileNotFoundError` naming it (`self.path()` does this); a file it refuses raises `ValueError` saying what to change.
- `sample()`: `{relative path: text}` that `read()` reads with its default options.

## The contract

`testbench plugin test` checks a reader against `metrics.contract`; from Python, `plugins.check("metrics", reader)`
returns the problems in plain words (`[]` when it keeps it):

1. Its name is plain and `files` maps roles to patterns.
2. With its file missing (an empty folder, and a file it was told to read that is not there) `read()` raises
   `FileNotFoundError` whose message names the file - never another exception, and never a result.
3. On its `sample()` files it returns finite numbers, booleans or `None` by name (not NaN, not numpy types, not
   text), `notes` as strings and `files` as a mapping.
4. Reading the same files twice gives the same names in the same order: checks name them.

The four built-in readers keep it (tests/test_metrics_readers.py checks each).
