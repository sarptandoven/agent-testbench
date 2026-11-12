# Export sinks

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

## Make one

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

## The interface

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

### Settings, checked for you

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

## What testbench promises around a sink

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

## The contract

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

## Test yours

Install it editable (`pip install -e .`), add it to a project's testbench.yaml and run a plan, or export a run
you already have: `testbench export --to NAME` prints its line, `cat .testbench/runs/<run>/export.log` shows
every attempt. testbench's own tests install the example the way pip does - the module and a `*.dist-info` with
`entry_points.txt` in a folder on `PYTHONPATH` - and run plans through the CLI
(`tests/test_sinks_notifiers_plugins.py`). In-process, `plugins.register("sinks", "name", obj)` adds one by hand.

Code written before sinks were plug-ins keeps working: `export.TARGETS`, `EXPORTERS`, `LOCAL` and `PACKAGES` are
read from the registry, so they include installed plug-ins.
