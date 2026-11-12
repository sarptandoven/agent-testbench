# Plug-ins

A plug-in is a part of testbench that another package adds: a machine to run on, a check `expect:` can make, a
place `export:` sends a run, and so on. Each kind sits behind one small interface around the core
([Architecture](architecture.md)), and built-ins and plug-ins share one table per kind, so a plug-in can do anything
a built-in does. `testbench plugins` lists every kind, its built-ins, what is installed and from which package, and
what is broken and why.

## Kinds

| kind | entry point group | what one adds | interface |
|---|---|---|---|
| backends | `agent_testbench.backends` | a machine plans and sessions run on (`backend:`) | `agent_testbench.backends.Backend` |
| checks | `agent_testbench.checks` | a check a step's `expect:` can make | `agent_testbench.expect.Check` |
| mocks | `agent_testbench.mocks` | a stand-in SDK a step's `mocks:` loads | `agent_testbench.mocks.Mock` |
| sinks | `agent_testbench.sinks` | a place `export:` sends a run | `agent_testbench.export.Sink` |
| notifiers | `agent_testbench.notifiers` | a place `notify:` tells people | `agent_testbench.notify.Notifier` |
| recipes | `agent_testbench.recipes` | a ready plan ladder (`testbench init --recipe`) | `agent_testbench.recipes.Recipe` |
| metrics | `agent_testbench.metrics` | numbers read from a step's files | `agent_testbench.metrics.Reader` |

`agent_testbench.plugins.KINDS` is this table in code. A kind takes plug-ins once its module defines `KIND`
(`plugins.Kind(...)`: its built-ins, how a loaded object becomes one, and its contract); until then `testbench
plugins` lists it as "not yet open to plug-ins", and a plug-in installed for it is listed as waiting.

## How a package adds one

A package declares an entry point in the kind's group, in its `pyproject.toml`. The entry point's name is the name
testbench uses (in testbench.yaml, on the command line); its value is the object, as `module:attr`:

```toml
[project.entry-points."agent_testbench.backends"]
nice = "testbench_nice:backend"
```

Installed next to agent-testbench (`pip install .` in the package's folder, or `pip install -e .` while you work
on it), it is found the next time testbench loads that kind. Nothing is imported until a kind is used, so a
plug-in costs nothing to the commands that do not need it.

Three rules hold for every kind:

- **A built-in cannot be replaced.** A plug-in that takes a built-in's name (`local`, `modal`, ...) is skipped and
  reported; give it a name of its own.
- **A broken plug-in breaks only what uses it.** One that does not load is left out and reported - by `testbench
  plugins`, by `testbench doctor` (a `no` line with why), and by the message a plan that names it gets - and every
  other plan, kind and command goes on working.
- **Every kind keeps its guardrails.** A backend prices its hardware and stops itself on the remote side; a sink
  or notifier never raises into a run or holds it up; a mock never touches the network. Each kind's contract
  checks its part.

## Testing a plug-in: the conformance kit

`testbench plugin test` loads plug-ins and checks each against its kind's contract, one line per plug-in and one
per problem, in plain words:

```bash
testbench plugin test testbench-backend-example        # every plug-in an installed package declares
testbench plugin test testbench_nice:backend --kind backends    # one object, before it is packaged
testbench plugin test --installed                      # every installed plug-in, of every kind
```

```text
ok   backends nice (testbench_nice:backend, testbench-backend-example 0.1.0): keeps its kind's contract
FAIL backends rack (testbench_rack:backend): 2 problems
     - a remote backend stops its own work on the remote side, ... say what does it in hard_stop ...
     - a one-step plan on it without a GPU costs $0 at worst, and it has no price setting of its own: ...
```

Exit codes: 0 every plug-in keeps its contract, 1 one does not, 2 nothing could be tested (no such object or
package, a package that declares no plug-ins, a kind it cannot tell: `--kind` says which). `--json` gives the same
as JSON. A `module:attr` without `--kind` is tested as the kind an installed entry point declares it under, or the
only kind that takes it. The kit also reports an entry point that takes a built-in's name, one whose object calls
itself by another name, and one in a group that is not a kind.

In the plug-in's own tests, the same check (standard library only):

```python
from agent_testbench.plugins import assert_conforms
import testbench_nice

def test_nice_keeps_the_backend_contract():
    assert_conforms("backends", testbench_nice.backend, "nice")    # AssertionError listing every problem
```

[examples/plugins/testbench-backend-example](../examples/plugins/testbench-backend-example) is a whole plug-in
package: a backend in one file, its `pyproject.toml` with the entry point, and its own test.

## Backends

A backend is a machine for the core to run plans and sessions on. The interface, from the reference:

<!-- From docs/reference.md, "Backends". Edit it there. -->
--8<-- "docs/reference.md#backends"

### The backend contract

What `testbench plugin test` checks for a backend (`agent_testbench.backends.contract`):

- its name can be written as `backend:` in testbench.yaml (letters, digits, `-` and `_`), and is the name its
  entry point gives it;
- `config_defaults` maps sections of its own to their settings: not one of testbench.yaml's own (`budget:`,
  `sessions:`, `pricing:`, ...), so a plug-in cannot change a budget, and not another backend's;
- it prices its CPU time and every GPU it offers: the real budget code (config's plan reader and
  `budget.worst_case`, on a throwaway config, no network) gives a one-step plan on it, without a GPU and with each
  of its GPUs, a finite worst case of $0 or more;
- a remote backend costs more than $0 there, at its defaults or once its own price settings (a number under a key
  with `usd` or `price` in its name, e.g. `usd_per_hour: 0` for a machine you own) are set;
- a remote backend takes start-up minutes above 0 (added to every worst case), and says what stops its work on the
  remote side at the time limit (`hard_stop`) or how long a session may run there (`max_session_minutes`);
- `plan_runner()` is a class with `run`, `cancel` and `cost`, `session_class()` one with the `Session` methods
  (either may raise `ConfigError`: the backend does not do that), and it does at least one of the two.

All six built-in backends keep it (a test checks).

## Checks

What a step's `expect:` can check, from another package. (Coming: this section says how to write one.)

## Mocks

A stand-in SDK a step's `mocks:` puts first on PYTHONPATH, never touching the network. (Coming: this section says
how to write one.)

## Sinks

A place `export:` sends a run after it ends, never failing or holding up the run. (Coming: this section says how to
write one.)

## Notifiers

A place `notify:` tells people a run or loop ended, never failing or holding up the run. (Coming: this section says
how to write one.)

## Recipes

A [recipe](recipes.md) is testbench.yaml for a goal: a ladder of plans with their checks and `FILL IN` lines. The 14
that ship are YAML files in `src/agent_testbench/recipes/`; a package adds more, and they work everywhere the built-in
ones do: `testbench recipes` lists them (with the package each comes from), `testbench recipes NAME` prints one,
`testbench init --recipe NAME` and `testbench init --add --recipe NAME` write them, and so does the MCP `recipes` tool.

### A package that ships one

[examples/plugins/testbench-recipe-example](https://github.com/sarptandoven/agent-testbench/tree/main/examples/plugins/testbench-recipe-example)
is the whole of it: a YAML file in the package, and one entry point that names the package.

```toml
# pyproject.toml
[project.entry-points."agent_testbench.recipes"]
cli-smoke = "testbench_recipe_example"      # reads cli-smoke.yaml from the package's folder
```

```
$ pip install ./examples/plugins/testbench-recipe-example
$ testbench recipes
...
cli-smoke (local) - from testbench-recipe-example
  a command-line tool starts, and turns a small sample input into the output it should.
    cli-smoke           local, up to 7 min, free
$ testbench init --recipe cli-smoke
Wrote testbench.yaml from recipe cli-smoke:
  plan cli-smoke: local, up to 7 min, free (recipe cli-smoke, from testbench-recipe-example)
```

An entry point can name any of these, all read the same way:

| the entry point names | read as |
|---|---|
| a package or module (`"my_pkg"`) | the file `NAME.yaml` in its folder, NAME being the entry point's name |
| a `pathlib.Path` (`"my_pkg:RECIPE"`, `RECIPE = Path(__file__).with_name("x.yaml")`) | that YAML file |
| a string with a newline in it | the YAML text itself |
| a mapping | `recipes.from_mapping` (below) |
| an `agent_testbench.recipes.Recipe` | `Recipe(name, text, path)`, as is |

### What a recipe holds

The same as a built-in one, and the same rules hold for it, checked in code when it loads:

- **A header comment.** Its first line is `# Recipe NAME: what it is for` (the line `testbench recipes` shows), then
  `Proves:` (what passing shows), `FILL IN:` (what only the person knows) and `Costs:` (what it can cost).
- **Sections, then plans.** Any of `sessions:`, `modal:`, `colab:`, `docker:`, `ssh:` (each once), then `plans:`
  with at least one plan; each top-level key unquoted at the start of its line and everything under it indented, so
  the recipe can be added to a testbench.yaml as text, comments and all.
- **Never `budget:`, `pricing:` or `guard:`.** They stay the person's. A recipe that sets one does not load:
  `testbench recipes` lists it under "Installed but not loaded" with the reason, and nothing from it is ever written.
- **`# FILL IN` on each value only the person knows**: a command's arguments, a path, the number that means
  working. A recipe with nothing to fill in says `FILL IN: nothing` in its header.

A plan name a recipe shares with the testbench.yaml it is added to (or with another recipe in the same `init`) is
refused with what to do; a built-in recipe's name cannot be taken by a plug-in (it is skipped and reported).

### The contract

`testbench plugin test` checks a recipe against `recipes.contract` before anyone installs it; from Python,
`plugins.check("recipes", recipe)` returns the problems in plain words (`[]` when it keeps the contract):

1. It reads by the rules above (`recipes.parse`).
2. Its header has the first line, `Proves:`, `FILL IN:` and `Costs:`, and its lines to fill in are marked.
3. Its plans load through config under the header `testbench init` writes, as `init --recipe` checks them (the
   notebooks it names stand in for the project's own).
4. Every plan's worst case is a number of dollars within the default `budget.max_run_usd` ($2.00), so nothing in it
   is refused as it ships.

The 14 built-in recipes keep it (tests/test_recipes_plugins.py checks each).

### A recipe made in Python

`recipes.from_mapping` writes a recipe's YAML text from a mapping, for a package that builds recipes in code. A
mapping holds no comments, so the header and the `FILL IN` marks are keys of their own:

```python
RECIPE = {
    "summary": "a command-line tool answers --help",
    "proves": "the tool installs and starts",
    "fill_in": "the tool's command",
    "costs": "nothing; it runs on this machine",
    "fill_in_at": {"plans.cli.steps.0.run": "your tool's command"},     # a `# FILL IN:` comment on that line
    "plans": {"cli": {"backend": "local", "description": "the tool starts", "steps": [
        {"name": "help", "run": "mytool --help", "max_minutes": 2, "expect": {"text": {"stdout": ["usage:"]}}}]}},
}
```

```toml
[project.entry-points."agent_testbench.recipes"]
cli-help = "my_pkg:RECIPE"
```

## Metrics

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

### The built-in readers

| reader | reads | names |
|---|---|---|
| `json` | a JSON file (default `metrics.json`) | every number, boolean and null by dotted path, as the `json:` expectation names them: `accuracy`, `eval.f1`, `losses.0`; a list longer than 10 shows only `.0` and `.-1`. Option `keys`: only these paths (`losses.-1` is the last item) |
| `csv` | a CSV file with a header (default `metrics.csv`) | `rows`, and for each numeric column `COLUMN.last`, `.mean`, `.max`, `.min`. Options `columns`, `stats`. A column with a nan or infinite value has null for what it spoils |
| `junit` | a JUnit XML report (default `junit.xml`, `report.xml`) | `tests`, `passed`, `failures`, `errors`, `skipped`, read as the `junit:` expectation reads them; the failing cases are in the notes |
| `trading` | `equity.csv`, and `trades.csv` and `prices.csv` next to it when they are there ([trading](trading.md)) | testbench's own backtest numbers, every key of `agent_testbench.trading.metrics.KEYS`: `sharpe`, `sortino`, `max_drawdown`, `cagr`, `trades`, `win_rate`, `exposure`, ... Options `risk_free`, `periods_per_year`, `calendar`, `symbol` |

Each value is a finite number, `true`/`false`, or `null` where the number is not defined (no trades file, a NaN in
the file); the notes say why. A null passes no rule.

### In a plan (planned)

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

### A reader in a package

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

### The contract

`testbench plugin test` checks a reader against `metrics.contract`; from Python, `plugins.check("metrics", reader)`
returns the problems in plain words (`[]` when it keeps it):

1. Its name is plain and `files` maps roles to patterns.
2. With its file missing (an empty folder, and a file it was told to read that is not there) `read()` raises
   `FileNotFoundError` whose message names the file - never another exception, and never a result.
3. On its `sample()` files it returns finite numbers, booleans or `None` by name (not NaN, not numpy types, not
   text), `notes` as strings and `files` as a mapping.
4. Reading the same files twice gives the same names in the same order: checks name them.

The four built-in readers keep it (tests/test_metrics_readers.py checks each).
