# How it fits together

agent-testbench is a small core with layers around it. The core is what it takes to prove a plan on this machine;
everything else either reaches the core from outside (a way in) or plugs a kind of machine, step or output into it.

```text
ways in     the CLI, the MCP server, the Python API and its tool definitions, other editors and agents
   |        (testbench connect), the live dashboard, the GitHub Action
   v
core        load testbench.yaml, price a plan and check the budget, launch a run and drive it on this
   |        machine, check its expectations, write and verify its proof record
   v
plug-ins    machines (Modal, Colab, ssh, Docker, Kubernetes, and backends from other packages), kinds of
            step (notebooks, web pages), mock LLM clients, export sinks (MLflow, W&B, csv), notifiers,
            recipes
```

**The core** is these modules of `src/agent_testbench/`: `config` (testbench.yaml), `budget` and `prices` (worst
cases, limits, ledgers), `backends/__init__` (the `Backend` interface and registry) and `backends/local` (this
machine), `runs` and `driver` (launching, following and closing out a run), `executor` (a plan's steps and their
evidence), `expect` (the checks), `metrics` (evidence files read as named numbers, [metric readers](plugins-metrics.md)),
`provenance` and `report` (the proof record), `procs` (background processes and
lock files on every system), and `core` itself. `agent_testbench.core` is its public face: thin functions to load a
project, list and estimate plans, launch a run, wait for it, read its report and verify its evidence - the same code
the CLI runs, so the same budget checks and the same records.

```python
from agent_testbench import core

root = core.find_root("path/to/project")
cfg = core.load(root)
core.estimate(cfg, "smoke")                    # {worst_case_usd, ..., allowed, message}
run_dir = core.launch(root, cfg, "smoke")      # over a limit: core.LaunchRefused, which says why
core.wait(run_dir)                             # its state.json once it ends
core.report(run_dir)["status"]                 # 'passed'
core.verify(run_dir)                           # []: every evidence file still matches its recorded sha256
```

**Everything else is a layer**: the ways in (`cli`, `mcp_server`, `api`, `tools`, `connect`, `dashboard`, `ui`,
`github_action`), the remote backends and live sessions (`backends/modal_backend`, `colab_backend`, `ssh_backend`,
`docker_backend`, `kubernetes_backend`, `sessions`, `session_backends`, `kernelkit/`), kinds of step (`notebook`,
`web`, `mocks`), what happens after a run (`notify`, `export`), work built on runs (`loop`, `watch`, `diff`,
`rescue`) and setting a project up (`scaffold`, `recipes`, `lint`, `selftest`). `core.CORE_MODULES` and
`core.LAYER_MODULES` are the two lists in code.

**The rule**: a core module never imports a layer, or a package beyond the standard library and PyYAML, at module
level. A layer may import the core; the core reaches a layer only inside the function that needs it - a notebook
step loads the notebook machinery (nbclient, a kernel), a web step Playwright, a finished run its notifications and
exports. So `import agent_testbench.core` loads only the standard library and PyYAML (120 to 160 modules, depending
on the Python version), and a plan of plain `run:` steps is priced, run, checked and verified without nbformat,
nbclient, the mock clients or any optional package.
`tests/test_core_boundary.py` checks all of it.

This is the base for plug-ins: a plug-in builds on the core and never needs the CLI or the MCP server loaded.
[Backend plugins](plugins.md) shows the one kind of plug-in that is open today, a machine from another package.
