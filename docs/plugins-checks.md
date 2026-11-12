# Check plug-ins

Every kind of check under a step's `expect:` is a plug-in of kind `checks`, the thirteen built-ins of
[Expectations](expectations.md) included: they sit in the same registry a package adds to. So a check from a package
works exactly like a built-in. testbench refuses a malformed value when `testbench.yaml` loads, before anything runs.
Its findings go in `report.md` and `report.json` with the others, and its numbers reach history, run-to-run
comparisons and `testbench loop --metric check:...`.

## Write one

[`examples/plugins/testbench-check-example`](../examples/plugins/testbench-check-example)
is a whole plug-in in about 50 lines: `max_log_lines` fails a step that printed more lines than you allow.

```python
from agent_testbench.config import ConfigError
from agent_testbench.expect import Check


class MaxLogLines(Check):
    name = "max_log_lines"
    example = 200

    def validate(self, where, value):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(f"`{where}` is the most lines the step may print, a whole number such as 200")

    def check(self, value, *, workdir, result, add):
        lines = len(result.get("stdout", "").splitlines())
        add(f"log at most {value} lines", lines <= value, f"{lines} (wanted <= {value})")
```

```toml
# its pyproject.toml: the entry point's name is the key under expect:
[project.entry-points."agent_testbench.checks"]
max_log_lines = "testbench_check_example:MaxLogLines"
```

```yaml
expect:
  max_log_lines: 200
```

After `pip install`, `testbench plugins` lists it, and a step that printed 250 lines fails with
`expected log at most 200 lines: 250 (wanted <= 200)`.

## The interface

A check is an object, or a class that takes no arguments (subclassing `agent_testbench.expect.Check` is optional):

| attribute | what it is |
|---|---|
| `name` | the key under `expect:`; a plain YAML key such as `max_log_lines` (the entry point's name if it has none) |
| `validate(where, value)` | runs when `testbench.yaml` loads. Raise `agent_testbench.config.ConfigError` for a malformed value, with a message that starts with `` `{where}` `` (the field, e.g. `plans.train.steps[0].expect.max_log_lines`) and says what to write instead |
| `check(value, *, workdir, result, add)` | runs after the step. Call `add(name, ok, detail)` once per finding; `detail` says what was found, the measured number first (`250 (wanted <= 200)`) |
| `example` | a value `validate` accepts; the contract tries `check()` with it |
| `keys` | optional: the names its value takes, for messages and docs |
| `schema` | optional: the JSON Schema of its value, for editors (below) |
| `default` | optional: the value `check()` gets on a step that does not name it, so it checks every step. Without one, it runs only where named. `exit_code`, `no_errors`, `web` and `mock_calls` have one |

`check()` reads `workdir`, the folder the step ran in, and never writes. `result` is the step's outcome:
`kind` (`run`, `notebook` or `web`), `exit_code`, `stdout` (everything the step printed), `seconds`, `run_started`
(a file older than this is an old result, not this run's), `step_dir`, `command`, `error` (a notebook's failing
cell), `web`, `gpu` and `mocks`. A missing or unreadable file is a failing finding (`add(name, False, "file
missing")`), never an exception.

`agent_testbench.expect` has the helpers the built-ins use: `compare(actual, rule)` for the rules of
[Expectations](expectations.md#rules) (`">= 0.9"`, `"~= 0.93 +- 0.01"`, `"between 0.8 and 0.95"`), `validate_rule`
and `validate_table` for values that hold rules, and `dig(data, "a.b.-1")`.

## What testbench does with it

- **Order.** The built-ins' findings come first, in their usual order, then the plug-ins' in the order the step
  names them.
- **A check that raises** fails its step with a finding that says so (`the check plug-in 'x' failed: KeyError:
  ...`). It never stops the run or touches another step. A `validate()` that raises anything but a `ConfigError`
  is refused the same way, at load time.
- **A plug-in that does not load** is left out and reported (`testbench plugins`). A `testbench.yaml` that names it
  is refused at load with the reason (`the check 'x' is installed but did not load (...)`); projects that do not use
  it are not affected. A plug-in cannot replace a built-in: one that takes a built-in's name is skipped.
- **Where it runs.** A step's checks run where the step ran. For a remote backend that is the remote machine, which
  needs the package installed too. Without it, the step fails with `no check 'x' is installed where this step was
  checked`; a check is never skipped in silence.
- **An unknown name** is refused at load with the near miss, listing every installed check:
  `` `plans.p.steps[0].expect` has no 'max_log_line' check; expectations are exit_code, ..., max_log_lines (did you
  mean max_log_lines?) ``.

## The contract

`testbench plugin test` (and `agent_testbench.plugins.check("checks", obj)`, which it calls) checks a check before
anyone installs it, and lists each problem in plain words:

- its name is a plain YAML key (not `on`, `yes` or a name with a space) and not a built-in's;
- `validate` accepts its `example`, and refuses a list and a mapping with a key nobody uses with a `ConfigError`
  that names the field (`null` may be accepted, as "nothing to check");
- `check()` with the example, on an empty folder as a run, notebook and web step, records at least one finding,
  calls `add(name, ok, detail)` with text and raises nothing (a check with nothing to check says so:
  `add(name, True, "nothing to check")`);
- `check()` writes nothing outside the step's folder;
- its `schema`, if it has one, is valid JSON Schema (draft 2020-12) and its example matches it.

The built-ins pass the same contract (`tests/test_checks_plugins.py`).

## Editors: the JSON Schema

The shipped schema describes the built-ins and allows any other plain name under `expect:`, since it cannot know
what is installed. `testbench schema` prints it with each installed plug-in's `schema` added under `$defs.expect`,
so an editor that uses that output flags a bad `max_log_lines:` too. A schema may use the shipped schema's own
definitions, such as `{"$ref": "#/$defs/rule"}` for a rule.

## In tests

`agent_testbench.plugins.register("checks", "max_log_lines", MaxLogLines())` adds one by hand and
`plugins.unregister` or `plugins.reset("checks")` forgets it. `plugins.check("checks", MaxLogLines())` returns the
contract's problems (`[]` when there are none). For a test that runs the CLI, put the module and a `dist-info` folder
with its `entry_points.txt` on `PYTHONPATH`: that is how an installed package is found, by the CLI and by the run's
own process.
