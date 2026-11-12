# testbench-metrics-example

A package that adds one metric reader to agent-testbench: `kvlog`, the last value of each `key=value` in a log - the
lines a training loop prints (`epoch=2 loss=0.25 acc=0.88`).

```
pip install ./examples/plugins/testbench-metrics-example
testbench numbers                        # lists kvlog, "from testbench-metrics-example"
testbench numbers kvlog train.log        # epoch 2, loss 0.25, acc 0.88, lines 2
testbench numbers kvlog train.log --opt keys=[loss] --json
```

How it is wired (`pyproject.toml`):

```toml
[project.entry-points."agent_testbench.metrics"]
kvlog = "testbench_metrics_example:KeyValueLog"
```

`KeyValueLog` subclasses `agent_testbench.metrics.Reader`: `files` says where it looks when no file is named,
`options` what it takes, `read()` returns its numbers by name (finite numbers, booleans, or `None` for a number it
could not work out, with a note saying why), and `self.path()` raises `FileNotFoundError` naming a missing file.
`sample()` gives a log it reads: `testbench plugin test` checks the reader against the contract with it (from Python:
`agent_testbench.plugins.check("metrics", KeyValueLog())`, `[]` when it keeps it). See
[docs/plugins-metrics.md](../../../docs/plugins-metrics.md).
