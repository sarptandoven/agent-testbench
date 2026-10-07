# Working on agent-testbench itself

- Code is in `src/agent_testbench/`; the Claude Code plugin is `.claude-plugin/`, `skills/`, `hooks/`.
- Run `python -m pytest -q` before and after a change. Web tests need `playwright install chromium`.
  The default suite never touches Modal or Colab. `tests/test_live.py` does (opt in with TESTBENCH_TEST_MODAL=1 /
  TESTBENCH_TEST_COLAB=1) and costs money - ask first.
- After changing `examples/tiny-model/cells/`, rebuild: `cd examples/tiny-model && testbench nb build cells tiny_model.ipynb`
  (a test checks the notebook matches its cells).
- Messages are for whoever reads them next (often an agent): say what was found and what to do, in plain words.
- Keep guardrails in code: anything that spends money must go through `budget.check` (runs and sessions), and
  every remote run or session must have a hard stop on the remote side.
- Sessions: `kernelkit/` (the launcher and client that run on the session's machine), `session_backends.py`
  (local, Modal Sandbox, Colab CLI), `sessions.py` (state, budget, supervisor).
- `claude plugin validate . && claude plugin validate .claude-plugin/plugin.json --strict` after plugin changes.
