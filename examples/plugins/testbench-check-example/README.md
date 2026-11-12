# testbench-check-example

A check plug-in in one file: `max_log_lines` fails a step that printed more lines than you allow (or whose log file
is longer). Copy it to start your own; [docs/plugins-checks.md](../../../docs/plugins-checks.md) has the interface.

```bash
pip install -e examples/plugins/testbench-check-example
testbench plugins           # lists it under checks; `testbench plugin test` checks it keeps the contract
```

```yaml
expect:
  max_log_lines: 200                                  # everything the step printed
  max_log_lines: {lines: 200, file: logs/train.log}   # or a log file the step wrote
```

A step that printed 250 lines fails with `log at most 200 lines - 250 (wanted <= 200)`. Install it wherever plans
run: a remote backend checks its steps on the remote machine.
