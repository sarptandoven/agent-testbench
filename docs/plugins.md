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

Every kind of check under a step's `expect:` is a plug-in of kind `checks`, the fifteen built-ins of
[Expectations](expectations.md) included: they sit in the same registry a package adds to (`strategy:` is the
trading pack's, registered by name and imported when checks are first used). So a check from a package
works exactly like a built-in. testbench refuses a malformed value when `testbench.yaml` loads, before anything runs.
Its findings go in `report.md` and `report.json` with the others, and its numbers reach history, run-to-run
comparisons and `testbench loop --metric check:...`.

### Write one

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

### The interface

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

### What testbench does with it

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

### The contract

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

### Editors: the JSON Schema

The shipped schema describes the built-ins and allows any other plain name under `expect:`, since it cannot know
what is installed. `testbench schema` prints it with each installed plug-in's `schema` added under `$defs.expect`,
so an editor that uses that output flags a bad `max_log_lines:` too. A schema may use the shipped schema's own
definitions, such as `{"$ref": "#/$defs/rule"}` for a rule.

### In tests

`agent_testbench.plugins.register("checks", "max_log_lines", MaxLogLines())` adds one by hand and
`plugins.unregister` or `plugins.reset("checks")` forgets it. `plugins.check("checks", MaxLogLines())` returns the
contract's problems (`[]` when there are none). For a test that runs the CLI, put the module and a `dist-info` folder
with its `entry_points.txt` on `PYTHONPATH`: that is how an installed package is found, by the CLI and by the run's
own process.

## Mocks

A stand-in SDK a step's `mocks:` puts first on PYTHONPATH, never touching the network. (Coming: this section says
how to write one.)

## Sinks

A sink is a place `export:` sends every finished run: a tracker, a table, a dashboard of your own. The four that
ship - `mlflow`, `wandb`, `csv`, `jsonl` ([Exports](export.md)) - are sinks in the same registry as the ones a
package adds, so a plug-in sink does everything a built-in does: its name goes under `export:` in testbench.yaml,
`testbench export --to NAME` and the MCP tool `export_run` take it, its outcome is a line in the run's `export.log`
and in `testbench report`.

```yaml
# testbench.yaml
export:
  csv: .testbench/runs.csv
  markdown: docs/runs.md          # a plug-in sink, by the name its package gives it
```

### Make one

A sink is an object with a name, a check of its settings and a `send()`. Subclassing
`agent_testbench.export.Sink` is optional; it brings the settings check below. This is the whole of
[examples/plugins/testbench-sink-example](../examples/plugins/testbench-sink-example/),
which writes one markdown table row per run:

```python
from pathlib import Path
from agent_testbench.export import ExportError, Sink


class MarkdownSink(Sink):
    name = "markdown"
    background = False                   # a local file, written in milliseconds at the end of the run
    options = ("path",)                  # the settings it takes: Sink.validate checks them
    short, short_hint = "path", "the file's path"     # `markdown: docs/runs.md` means {path: docs/runs.md}
    hints = {"path": ".testbench/runs.md"}            # examples in its messages
    example = {"path": "runs.md"}                      # settings `testbench plugin test` tries it with

    def send(self, root, run_dir, record, opts):
        path = Path(root) / (opts.get("path") or ".testbench/runs.md")
        ...                                            # write the row (a run exported again replaces its row)
        return f"a row in {opts.get('path') or '.testbench/runs.md'}"
```

and its `pyproject.toml` declares it under the entry point group `agent_testbench.sinks`; the entry point's name
is the sink's name in testbench.yaml:

```toml
[project.entry-points."agent_testbench.sinks"]
markdown = "testbench_sink_example:MarkdownSink"
```

`pip install` the package into the Python that runs testbench, and `testbench plugins` lists it.

### The interface

| | what it is |
|---|---|
| `name` | its key under `export:` and its `--to` name: a short lowercase word. Left empty, the entry point's name |
| `background` | `True` for anything that may touch the network or take long: after a run it goes to a background process (at most 15 minutes), so a slow or unreachable service never holds the run up. `False` for quick local work, done at the end of the run |
| `requires`, `package` | the module that must import for it to work (`"mlflow"`) and what to `pip install` when it does not (default: the module). Imported only when the sink is used; a missing one fails that export with the pip command, never the run |
| `title` | how messages name it (`"MLflow"`; default: its name) |
| `example` | settings that validate, for `testbench plugin test` (relative paths land in a scratch folder) |
| `validate(where, opts) -> dict` | the settings as written under `export.NAME` (`None` or `true` for none, a string for a short form) -> the settings `send()` gets. Anything it cannot use raises `agent_testbench.config.ConfigError` naming the field (`where` is `"export.NAME"`) and saying what to write instead |
| `send(root, run_dir, record, opts) -> str` | export one finished run and say what was done, in one line (`"row 3 of .testbench/runs.csv"`). A plain failure raises `agent_testbench.export.ExportError` (or `Missing`, exit code 2, for something to install) with a message that says what to do |

`record` is one dict per run - the same record csv and jsonl write: `run_id`, `project`, `plan`, `status`,
`backend`, `gpu`, `started`, `finished`, `minutes`, `cost_usd`, `worst_case_usd`, `commit`, `branch`, `dirty`,
`steps`, `steps_passed`, `checks`, `checks_passed`, `evidence_files`, `evidence_sha256`, `stopped_because`,
`testbench_version`, `python`, `report` (the report's path, relative to the project), `params` (the steps' params)
and `metrics` (every number the checks found, named step/check without the rule). `run_dir` holds `report.json`,
`report.md` and the evidence under `steps/`, for a sink that uploads files.

#### Settings, checked for you

A subclass of `Sink` declares its settings and gets the same check, and the same messages, as the built-ins:
`options` names the settings; each is a non-empty string, except `flags` (`{name: what true means}`: true or false)
and `lists` (lists of words). `short` is the setting a plain string stands for, `short_hint` what that string is,
`hints` examples for the messages, `secrets` the variables to name when someone writes a secret in the file.
`check(where, opts)` adds rules of its own; or override `validate()` altogether.

Secrets never go in testbench.yaml. A setting named like a token, key, secret or password is refused - by
`Sink.validate`, and again by testbench after any sink's own check - unless it ends in `_env` and holds the NAME
of an environment variable (`token_env: SHEETS_TOKEN`); the sink reads the value from the environment. Nothing a
sink returns or raises is shown before the value of every variable named like `*TOKEN*`, `*KEY*`, `*SECRET*` or
`*PASSWORD*`, and any password in a URL, is blanked out - but a sink should not lean on that: say what was done,
not what was sent.

### What testbench promises around a sink

- **Nothing a sink does fails a run.** Whatever `send()` raises - `ExportError`, any other exception, even
  `SystemExit` - fails that export only, as a line in `export.log` (`sheets: failed - ...`) and in
  `testbench report`, with the command that tries it again (`testbench export RUN --to sheets`).
- **Nothing a sink does holds a run up.** Background sinks run in a process of their own after the run, given up
  on after 15 minutes. The quick ones (`background = False`) run at the end of the run: the built-ins one after
  the other, each plug-in in a thread of its own, all within 60 seconds; one still going then is written off in
  `export.log` and the run goes on.
- **A package that breaks does not stop the project.** A sink whose package is installed but does not load (an
  import error after an upgrade) keeps testbench.yaml loading: its exports fail in `export.log`, saying it did not
  load, and `testbench plugins` shows why. A name nothing provides is refused when testbench.yaml loads, with the
  names that exist. A `validate()` that crashes (rather than raising `ConfigError`) is named as a bug in its package,
  with the line that leaves it out (`sheets: false`).
- A plug-in cannot take a built-in's name: it is skipped and reported.

### The contract

`testbench plugin test` checks a sink before anyone installs it (it calls `plugins.check("sinks", obj)`, which is
`export.KIND.contract`); every built-in passes it, and a test runs it on each. The problems come back in plain
words, one per line:

- it has a name `export:` can use, `background` is `True` or `False`, and it has `validate()` and `send()`;
- `validate()` refuses junk (`42`, a list, an unknown setting) with a `ConfigError` that names `export.NAME`;
- `validate(example)` gives the settings back as a dict;
- `send()` into a scratch folder, with a minimal finished run, either returns one line of text or raises
  `ExportError` - no other exception - within two minutes;
- no secret comes out: a fake one is put in the environment (`TESTBENCH_CHECK_TOKEN`) and another slipped into
  its settings, and neither may appear in what `send()` returns, prints or writes to `export.log`;
- connections to other machines are refused during the check, and a sink that reaches for one must say
  `background = True`; one with `background = False` must also finish in well under 10 seconds.

### Test yours

Install it editable (`pip install -e .`), add it to a project's testbench.yaml and run a plan, or export a run
you already have: `testbench export --to NAME` prints its line, `cat .testbench/runs/<run>/export.log` shows
every attempt. testbench's own tests install the example the way pip does - the module and a `*.dist-info` with
`entry_points.txt` in a folder on `PYTHONPATH` - and run plans through the CLI
(`tests/test_sinks_notifiers_plugins.py`). In-process, `plugins.register("sinks", "name", obj)` adds one by hand.

Code written before sinks were plug-ins keeps working: `export.TARGETS`, `EXPORTERS`, `LOCAL` and `PACKAGES` are
read from the registry, so they include installed plug-ins.

## Notifiers

A notifier is a place `notify:` tells people that a run ended, a loop's rule fired or ended it, or a launch waits
for a person's approval ([Notifications](notify.md)). Two ship: the **webhook** (a JSON POST, Slack-compatible)
and the **desktop** (a notification on this machine's screen). A package adds more, and they sit in the same
registry: every notifier `notify:` names gets every event `on:` lists, all at once.

```yaml
# testbench.yaml
notify:
  webhook_env: TESTBENCH_WEBHOOK                # the webhook, as always: webhook: or webhook_env:
  desktop: true                                 # the desktop notifier: no settings
  events: {path: ~/testbench-events.jsonl}      # a plug-in notifier, by its name, with its settings
  on: [run_end, loop_rule, loop_end, approval_needed]
```

`webhook:` / `webhook_env:` and `on:` are `notify:`'s own keys; every other key is a notifier's name, set to
`true` (or left empty) for one without settings, to its settings, or to `false` to leave it out. A section names
at least one notifier.

### Make one

A notifier is an object with a name, a check of its settings, `send()` and `where()`. Subclassing
`agent_testbench.notify.Notifier` is optional (it gives a notifier without settings, `NAME: true`). This is the
heart of
[examples/plugins/testbench-notifier-example](../examples/plugins/testbench-notifier-example/),
which writes each event as a JSON line to a file, or POSTs it to a service on this machine:

```python
import json, urllib.request
from agent_testbench.config import ConfigError
from agent_testbench.notify import Notifier, where


class EventsNotifier(Notifier):
    name = "events"
    example = {"url": "{url}"}           # `testbench plugin test` makes {url} an address that does not answer

    def validate(self, field, opts):      # field is "notify.events"
        if not isinstance(opts, dict) or len(opts) != 1 or set(opts) - {"path", "url"}:
            raise ConfigError(f"`{field}` takes path (a file) or url (an http:// address on this machine)")
        ...
        return opts

    def send(self, event, text, fields, opts, timeout):
        try:
            ...                           # one attempt, within `timeout` seconds
            return True, "written"
        except Exception as exc:          # never raise: say what went wrong, without the URL
            return False, f"{type(exc).__name__}: {getattr(exc, 'reason', None) or exc}"

    def where(self, opts):
        return opts["path"] if "path" in opts else where(opts["url"])     # a URL: scheme and host only
```

```toml
[project.entry-points."agent_testbench.notifiers"]
events = "testbench_notifier_example:EventsNotifier"
```

### The interface

| | what it is |
|---|---|
| `name` | its key under `notify:`: a short lowercase word, not `on` or `webhook_env` (notify:'s own keys). Left empty, the entry point's name |
| `example` | settings that validate, for `testbench plugin test`; a value `"{url}"` there becomes the address of a closed port, then of one that never answers |
| `validate(where, opts) -> dict` | the settings as written under `notify.NAME` (`None` or `true` for none) -> the settings the others get. Anything it cannot use raises `agent_testbench.config.ConfigError` naming the field (`where` is `"notify.NAME"`) and saying what to write instead |
| `send(event, text, fields, opts, timeout) -> (ok, detail)` | tell people about one event: `text` is the message (`"agent-testbench, my-project: run ... PASSED ..."`), `fields` the same facts as data (`event`, `project`, `machine`, `time` and the event's own - the table in [notify.md](notify.md#events)). One attempt, at most `timeout` seconds, and it never raises: `(True, "HTTP 200")` when the message was taken, `(False, what went wrong)` otherwise |
| `where(opts) -> str` | where messages go, as `notify.log` and `testbench notify test` show it. For a URL only its scheme and host (`notify.where(url)` does that): never its path, query or a password, which are often the secret |
| `missing(opts) -> str` (optional) | what this machine lacks to send now - a variable that is not set, a command that is not installed - or `""`. When it says something, nothing is tried and `notify.log` says that instead |

Secrets stay out of testbench.yaml: take an environment variable's NAME, in a setting ending in `_env`, as
`webhook_env` does. A setting named like a token, key, secret or password is refused unless it ends in `_env`
and holds a variable's name - whatever the notifier's own check allows.

### What testbench promises around a notifier

The guarantees of the webhook hold for every notifier:

- **Delivery never raises and never holds anything up.** Each notifier's `send()` runs in a thread of its own,
  all of them at once, given 5 seconds (`timeout`); one still going a second after that is left behind and
  written down as `no answer within 5 s`. Whatever `send()` raises becomes `not accepted (ExceptionName: ...)`.
- **Every attempt is a line in `.testbench/notify.log`**, delivered or not: `run_end to <where()>: accepted
  (<detail>)`, `... not accepted (...)`, or `run_end: nothing sent - <missing()>`.
- **The same events, from the same places**: `run_end` once per run (from `runs.finalize`, never for a loop's
  tries), `loop_rule` / `loop_end` / `loop_try` from the loop, `approval_needed` when a launch is refused with
  "Needs approval" - each sent from this machine, also for remote runs.
- **Secrets stay hidden**: before a line is written, any URL in the notifier's settings (or in a variable an
  `_env` setting names) is cut back to its scheme and host, and such a variable's value is blanked out.
- **A package that breaks does not stop the project**: a notifier whose package is installed but does not load
  keeps testbench.yaml loading; each event says in `notify.log` that it did not load. A name nothing provides is
  refused when testbench.yaml loads, with the names that exist.

`testbench notify test` sends one test message to every notifier: exit 0 when all took it, 1 when one did not
(each part of the answer says what happened), 2 when none could be tried here.

### The desktop notifier

`desktop: true` shows a notification on the screen of the machine testbench runs on - for someone working at it,
with no account and no URL. It uses `notify-send` on Linux (from libnotify: `apt install libnotify-bin`, `dnf
install libnotify`), `osascript` on macOS, and a PowerShell toast on Windows (`msg` when that fails). The text is
passed as a value (in PowerShell's case through the environment), never as part of a command. Where none is found
- a server, CI - `notify.log` says `nothing sent - no desktop notification on this machine: ...` and nothing else
happens; a command that fails (no display) is a `not accepted (exit 1: ...)` line, never a failed run.

### The contract

`testbench plugin test` checks a notifier before anyone installs it (`plugins.check("notifiers", obj)`, which is
`notify.KIND.contract`); the webhook and the desktop pass it, and a test runs it on each:

- it has a name `notify:` can use, and `validate()`, `where()` and `send()`;
- `validate()` refuses junk (`42`, a list, an unknown setting) with a `ConfigError` that names `notify.NAME`;
- with its `example` settings, against a closed port on this machine and then against one that takes the
  connection and never answers (each URL with a secret path and query), `send()` returns `(ok, detail)` within
  its timeout plus one second, never raises, never says `True` for an address nothing answers, and shows the URL's
  path or query neither in its answer nor in `where()` or `missing()`.

### Test yours

Install it editable, add it to `notify:` and run `testbench notify test`; `.testbench/notify.log` has every
attempt. testbench's own tests install the example the way pip does (a `*.dist-info` with `entry_points.txt` on
`PYTHONPATH`), run a plan through the CLI and read the file it wrote, or the local webhook stand-in it posted to
(`tests/test_sinks_notifiers_plugins.py`). In-process, `plugins.register("notifiers", "name", obj)` adds one by
hand.

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

### In a plan

A step's `numbers:` expectation ([Expectations](expectations.md#numbers)) names a reader, its files and options, and
rules on the numbers it reads; a list of such blocks reads with several readers. A reader a package adds works
there like a built-in, once the package is installed where the step is checked:

```yaml
expect:
  numbers:
    reader: trading                                          # json | csv | junit | trading | an installed reader
    files: {equity: out/equity.csv, trades: out/trades.csv}  # or a path, or a list; default: the reader's own
    options: {risk_free: 0.04}                               # the reader's options
    rules: {sharpe: ">= 1", max_drawdown: ">= -0.2", trades: ">= 30"}
```

Each rule is written as every other expectation's (`>= 1`, `~= 0.93 +- 0.01`, `between 0.8 and 0.95`). A missing,
refused or stale file (last written before the step started) fails every rule with the reader's message; a number
the reader did not give, or gave as `null`, fails its rule with the reader's note on why. When `testbench.yaml`
loads, the reader, its options (by name, and by value through `check_options`), its file roles and the rules are
checked, and for a reader with `keys` the names too. A reader whose options include `keys` gets the rules' names
there unless the block sets it, so it reads exactly those. The trading pack's `strategy:`
([trading](trading.md#checking-a-strategy)) reads with `trading` and adds guardrails.

`testbench numbers` shows what each name holds, and from Python:

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
- `keys` (optional): the names `read()` always gives, when they do not depend on the file (`trading`'s and
  `junit`'s do not). A `numbers:` rule on another name is then refused when `testbench.yaml` loads, with the near
  miss.
- `check_options(opts)` (optional): raise `ValueError` for an option's value it refuses, saying what to write, so a
  `numbers:` block with `calendar: stockz` is refused at load rather than after the step.

### The contract

`testbench plugin test` checks a reader against `metrics.contract`; from Python, `plugins.check("metrics", reader)`
returns the problems in plain words (`[]` when it keeps it):

1. Its name is plain and `files` maps roles to patterns.
2. With its file missing (an empty folder, and a file it was told to read that is not there) `read()` raises
   `FileNotFoundError` whose message names the file - never another exception, and never a result.
3. On its `sample()` files it returns finite numbers, booleans or `None` by name (not NaN, not numpy types, not
   text), `notes` as strings and `files` as a mapping.
4. Reading the same files twice gives the same names in the same order: checks name them.
5. With `keys`, every name it lists is among those `read()` gives on its sample.

The four built-in readers keep it (tests/test_metrics_readers.py checks each).
