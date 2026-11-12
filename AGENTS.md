# Working on agent-testbench itself

- Code is in `src/agent_testbench/`; the Claude Code plugin is `.claude-plugin/`, `skills/`, `hooks/`.
- Run `python -m pytest -q` before and after a change. Web tests need `playwright install chromium`.
  The default suite never touches Modal or Colab. `tests/test_live.py` does (opt in with TESTBENCH_TEST_MODAL=1 /
  TESTBENCH_TEST_COLAB=1) and costs money - ask first.
- `python -m pytest -q -n 4` runs it in parallel (pytest-xdist; CI does). So tests run side by side:
  each needs its own ports (port 0, or conftest's `own_port`), folders (`tmp_path`) and names, finds processes only
  by something no other test has (its own folder), and waits on a condition, not a fixed sleep. A test that uses
  something the whole machine shares (Docker, a kind cluster) is marked `@pytest.mark.serial`.
- After changing `examples/tiny-model/cells/`, rebuild: `cd examples/tiny-model && testbench nb build cells tiny_model.ipynb`
  (a test checks the notebook matches its cells).
- Messages are for whoever reads them next (often an agent): say what was found and what to do, in plain words.
- Keep guardrails in code: anything that spends money must go through `budget.check` (runs and sessions), and
  every remote run or session must have a hard stop on the remote side. Every budget, price and time limit goes
  through `config.number` (NaN compares false with every limit), and an MCP tool argument that ends up on the
  command line is a value, never an option (`mcp_server._name`, `--opt=value`, or after `--`).
- Core and layers (`core.py`, the base for plug-ins; `docs/architecture.md`): the core is `config`, `budget`,
  `prices`, `backends/__init__` and `backends/local`, `runs`, `driver`, `executor`, `expect`, `provenance`, `report`,
  `procs` and `core` itself - load a config, price and check a launch, run a plan on this machine, check it, write and
  verify its proof record. Everything else is a layer: the ways in (`cli`, `mcp_server`, `api`, `tools`, `connect`,
  `dashboard`, `ui`, `github_action`), the remote backends, `sessions`, `session_backends`, `kernelkit/`,
  `notebook`, `web`, `mocks`, `notify`, `export`, `loop`, `watch`, `diff`, `rescue`, `scaffold`, `recipes/`, `lint`,
  `selftest`. A core module never imports a layer or an optional package at module level (only inside the function
  that needs it, e.g. `executor` loads `notebook` for a notebook step); `tests/test_core_boundary.py` checks it.
  The lists are `core.CORE_MODULES` / `core.LAYER_MODULES`; a new module is a layer unless added to the core's.
- Backends: `backends/__init__.py` (the `Backend` interface and registry, plugins by entry point); each backend
  module (`backends/local.py`, `modal_backend.py`, `colab_backend.py`, `ssh_backend.py`, `docker_backend.py`,
  `kubernetes_backend.py`) owns its GPUs, prices and plan runner.
- Proof: `provenance.py` (source, environment, evidence hashes; `schema/report-v1.schema.json`), `report.py`
  (md, html, junit, history), `ui.py` (static pages), `dashboard.py` (`testbench ui --serve`: live and read-only on
  127.0.0.1, Host header checked, serves only reports and step evidence from .testbench - never state.json or
  plan.json; nothing on it may launch, approve, cancel or spend). Prices: `prices.py` and `pricing/catalog.json` (keep them equal
  to the backends' built-in tables; a test checks).
- Processes: `procs.py` (start, check and stop background work and lock files - the same on Linux, macOS and
  Windows; never `os.kill(pid, 0)` on Windows, it ends the process). `scaffold.py`: what `testbench init` writes.
- CI: `action.yml` (the GitHub Action) and `.github/workflows/action.yml` (its self-test).
- Loops: `loop.py` (`testbench loop`: tries on a git branch, kept or reverted by a plan run's metric; stopping
  rules --target, --patience, --max-tries).
- Notifications: `notify.py` (testbench.yaml `notify:`; run_end from `runs.finalize`, once per run and not for a
  loop's tries; loop_rule / loop_end / loop_try from `loop.py`; approval_needed from launches refused with "Needs
  approval"). Delivery never raises or blocks for long, and only a webhook URL's scheme and host are ever shown.
  Tests post to a local server (the `webhook` fixture), never a real one.
- Exports: `export.py` (`testbench export`, and testbench.yaml's `export:` after every run from `runs.finalize`:
  MLflow, W&B, csv, jsonl). Trackers are imported only when used and never fail or hold up a run; their tests
  skip unless mlflow / wandb are installed (in a separate venv: `docs/export.md`).
- Setup: `recipes/` (the YAML ladders `testbench recipes` and `init --recipe` use; recipes never set budget:,
  pricing: or guard:), `connect.py` (`testbench connect`: other clients' MCP configs, merged as text).
- testbench.yaml: `config.py` reads it; its short forms (`tests: "pytest -q"`, a list of commands, a step written as
  a command) become the long form there (`config.long_form`), so nothing downstream sees them.
  `schema/testbench.schema.json` is its JSON Schema for editors (`testbench schema`): a new key needs an entry
  there too (tests/test_schema.py fails otherwise).
- First runs: `check.py` (`testbench check`: every free plan, or with no testbench.yaml the plans `scaffold.detect`
  finds, run here; the record `.testbench/check.json` is the folder's config then, through `config.checked`, which
  reads only local plans from it - never a budget). `executor.step_path`: from uvx, uv tool or pipx, steps get the
  project's `python`.
- Plans for LLM apps: `mocks.py` and `mock_clients/` (the mock `openai` / `anthropic` packages a step's `mocks:` puts
  first on PYTHONPATH; they never touch the network). The broker mocks `alpaca` / `ccxt` / `ib_async` (+ `ib_insync`)
  share `mock_clients/shared/_testbench_broker.py` (prices, the paper account, fills, the live-account refusal).
  Notebooks: `notebook.py` (forms, papermill parameters, percent-format .py, kernels).
- From Python: `api.py` (`Testbench`, on the same launch paths as the CLI) and `tools.py` (the MCP tools as
  Anthropic / OpenAI tool definitions, read from the server's own tool table).
- Rescue: `rescue.py` (`testbench rescue`: reads a notebook, runs it through `executor.run_step`, diagnoses the first
  failing cell; the import-to-pip-name table is there).
- Sessions: `kernelkit/` (the launcher and client that run on the session's machine), `session_backends.py`
  (local, Modal Sandbox, Colab CLI), `sessions.py` (state, budget, supervisor).
- `claude plugin validate . && claude plugin validate .claude-plugin/plugin.json --strict` after plugin changes.
