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
- Backends: `backends/__init__.py` (the `Backend` interface and registry, plugins by entry point); each backend
  module (`backends/local.py`, `modal_backend.py`, `colab_backend.py`, `ssh_backend.py`,
  `docker_backend.py`) owns its GPUs, prices and
  plan runner.
- Proof: `provenance.py` (source, environment, evidence hashes; `schema/report-v1.schema.json`), `report.py`
  (md, html, junit, history), `ui.py` (static pages). Prices: `prices.py` and `pricing/catalog.json` (keep them equal
  to the backends' built-in tables; a test checks).
- Processes: `procs.py` (start, check and stop background work and lock files - the same on Linux, macOS and
  Windows; never `os.kill(pid, 0)` on Windows, it ends the process). `scaffold.py`: what `testbench init` writes.
- CI: `action.yml` (the GitHub Action) and `.github/workflows/action.yml` (its self-test).
- Loops: `loop.py` (`testbench loop`: tries on a git branch, kept or reverted by a plan run's metric).
- Exports: `export.py` (`testbench export`, and testbench.yaml's `export:` after every run from `runs.finalize`:
  MLflow, W&B, csv, jsonl). Trackers are imported only when used and never fail or hold up a run; their tests
  skip unless mlflow / wandb are installed (in a separate venv: `docs/export.md`).
- Sessions: `kernelkit/` (the launcher and client that run on the session's machine), `session_backends.py`
  (local, Modal Sandbox, Colab CLI), `sessions.py` (state, budget, supervisor).
- `claude plugin validate . && claude plugin validate .claude-plugin/plugin.json --strict` after plugin changes.
