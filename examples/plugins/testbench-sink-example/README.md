# testbench-sink-example

An export sink for agent-testbench in one file: every finished run becomes a row of a markdown table, so a file
in your repository (or its wiki) lists the runs with their status, checks, cost and a link to the proof.

```bash
pip install -e examples/plugins/testbench-sink-example     # into the Python that runs testbench
```

```yaml
# testbench.yaml
export:
  markdown: docs/runs.md        # or {path: docs/runs.md}; `markdown: true` means .testbench/runs.md
```

```
| run | plan | status | checks | cost | report |
|---|---|---|---|---|---|
| 20261112-091502-tests | tests | PASSED | 3/3 | $0.000 | [report](.testbench/runs/20261112-091502-tests/report.json) |
```

Every run adds its row when it ends; `testbench export --to markdown` (or `testbench export all --to markdown`)
writes earlier ones, and a run exported again replaces its row. `testbench plugin test` checks the sink against
the contract every sink keeps.

How it is made (see [docs/plugins.md#sinks](../../../docs/plugins.md#sinks)):

- `pyproject.toml` declares the entry point: group `agent_testbench.sinks`, name `markdown` (the key under
  `export:` and the `--to` name), value `testbench_sink_example:MarkdownSink`.
- `MarkdownSink` subclasses `agent_testbench.export.Sink`, so declaring `options` gets its settings checked;
  `background = False` because it only writes a local file (anything that talks to a server says `True` and runs
  in a background process after the run).
- `send()` returns what it did in one line, which goes to the run's `export.log` and `testbench report`; a
  failure raises `ExportError` with a message that says what to do. Nothing a sink does can fail a run.
