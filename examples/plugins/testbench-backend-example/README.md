# testbench-backend-example

A backend plug-in for agent-testbench in one file: `backend: nice` runs plans on this machine, like `local`, under
`nice -n 10` (`nice: {niceness: N}` in testbench.yaml changes it). Free, and not remote.

```bash
pip install -e examples/plugins/testbench-backend-example
testbench plugins                                # installed  nice (testbench_nice:backend, testbench-backend-...)
testbench plugin test testbench-backend-example  # ok   backends nice (...): keeps its kind's contract
python -m pytest examples/plugins/testbench-backend-example   # its own test: plugins.assert_conforms
```

The parts: `pyproject.toml` declares the entry point (`nice = "testbench_nice:backend"` in the
`agent_testbench.backends` group), and `testbench_nice.py` is the backend - its prices, its section of
testbench.yaml, and the plan runner. [docs/plugins.md](../../../docs/plugins.md) says how to write one of each kind.
