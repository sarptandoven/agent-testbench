# Contributing

Bug reports and pull requests are welcome.

```bash
git clone https://github.com/sarptandoven/agent-testbench && cd agent-testbench
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]" scikit-learn==1.5.2 matplotlib==3.9.2
playwright install chromium
python -m pytest -q                  # everything that needs no account (about 3 minutes)
```

- `tests/test_live.py` runs on Modal and Colab and costs money; it only runs with `TESTBENCH_TEST_MODAL=1` /
  `TESTBENCH_TEST_COLAB=1`. Say in your pull request whether you ran it.
- Anything that can spend money goes through `budget.check`, and every remote run or session needs a hard stop
  on the remote side - please keep it that way.
- Messages and reports are read by agents as much as by people: say what was found and what to do next.
- After changing `examples/tiny-model/cells/`, rebuild the notebook: `testbench nb build cells tiny_model.ipynb`.
- Plugin changes: `claude plugin validate . --strict`.

[AGENTS.md](AGENTS.md) has the same notes for coding agents working on this repository.
