"""The core of agent-testbench: what it takes to prove a plan on this machine, and nothing more.

    ways in    the CLI, the MCP server, the Python API and its tool definitions, editors (connect, the VS Code
       |       extension), the dashboard, the GitHub Action and `testbench ci` jobs
       v
    core       load testbench.yaml, price a plan and check the budget, launch a run and drive it here, check its
       |       expectations, write and verify its proof record
       v
    plug-ins   seven kinds: backends (Modal, Colab, ssh, Docker, Kubernetes, entry-point backends), checks, mocks,
               export sinks, notifiers, recipes, metric readers; the trading pack is plug-ins of these kinds

docs/architecture.md is this picture in words.

The rule: a core module never imports a layer, or a package other than the standard library and PyYAML, at module
level. A layer may import the core; the core reaches a layer only inside the function that needs it (a notebook step
loads the notebook machinery, a finished run its notifications and exports), so `import agent_testbench.core` loads
no optional package and nothing a plain `run:` step does not need. tests/test_core_boundary.py checks it.

The functions below are the core's public face, for plug-ins and the Python API: thin calls into the modules that do
the work, so the budget checks and the records are the CLI's own.

    from agent_testbench import core
    root = core.find_root("path/to/project")
    cfg = core.load(root)
    core.estimate(cfg, "smoke")                    # {worst_case_usd, ..., allowed, message}
    run_dir = core.launch(root, cfg, "smoke")      # over a limit: core.LaunchRefused, which says why
    core.wait(run_dir)                             # its state.json once it ends
    core.report(run_dir)["status"]                 # 'passed'
    core.verify(run_dir)                           # []: every evidence file still matches its recorded sha256
"""
from __future__ import annotations

import math
from pathlib import Path

from . import budget, config as C, provenance, report as R, runs

CORE_MODULES = (
    "agent_testbench",                     # the version; the Python API loads on first use
    "agent_testbench.core",                # this module
    "agent_testbench.config",              # testbench.yaml: read, checked, defaults filled in
    "agent_testbench.budget",              # worst cases, limits and ledgers: whether a launch may go
    "agent_testbench.prices",              # the price catalog backends read
    "agent_testbench.backends",            # the Backend interface and its registry
    "agent_testbench.backends.local",      # this machine
    "agent_testbench.runs",                # launch, find, follow, cancel and close out runs
    "agent_testbench.driver",              # the background process behind a run
    "agent_testbench.executor",            # a plan's steps where the plan runs, and their evidence
    "agent_testbench.expect",              # expectations: the checks on a step's result
    "agent_testbench.metrics",             # metric readers: evidence files as named numbers, for checks to name
    "agent_testbench.provenance",          # source, environment and evidence hashes; verify
    "agent_testbench.report",              # the report: json, md, html, junit, history
    "agent_testbench.procs",               # background processes and lock files, the same on every system
    "agent_testbench.plugins",             # finds, loads and hands out plug-ins of every kind
)

# Every other module of the package is a layer. A package name stands for its modules too.
LAYER_MODULES = (
    # ways in
    "agent_testbench.__main__", "agent_testbench.cli", "agent_testbench.mcp_server", "agent_testbench.api",
    "agent_testbench.tools", "agent_testbench.connect", "agent_testbench.dashboard", "agent_testbench.ui",
    "agent_testbench.github_action", "agent_testbench.ci",
    # machines: the remote backends, and live sessions on any backend
    "agent_testbench.backends.modal_backend", "agent_testbench.backends.colab_backend",
    "agent_testbench.backends.ssh_backend", "agent_testbench.backends.docker_backend",
    "agent_testbench.backends.kubernetes_backend", "agent_testbench.sessions", "agent_testbench.session_backends",
    "agent_testbench.kernelkit",
    # kinds of step, and the mock LLM clients a step can run against
    "agent_testbench.notebook", "agent_testbench.web", "agent_testbench.mocks",
    # after a run: notifications and exports
    "agent_testbench.notify", "agent_testbench.export",
    # a run's proof for others: a page to publish (and check), a README badge
    "agent_testbench.share", "agent_testbench.badge",
    # work built on runs
    "agent_testbench.loop", "agent_testbench.watch", "agent_testbench.diff", "agent_testbench.rescue",
    # setting a project up
    "agent_testbench.scaffold", "agent_testbench.recipes", "agent_testbench.lint", "agent_testbench.selftest",
    # the trading pack: standard files, testbench's own metrics, overfitting statistics
    "agent_testbench.trading",
    # the data seal: sealed holdout rows, walk-forward windows, data fingerprints
    "agent_testbench.dataseal",
)

# The only package outside the standard library a core module imports at module level (testbench.yaml, limits.yaml).
CORE_REQUIRES = ("yaml",)

ConfigError = C.ConfigError            # testbench.yaml (or this machine's limits) is wrong; the message says how
LaunchRefused = runs.LaunchRefused     # over a limit ("Refused: ...") or to be approved ("Needs approval: ...")
DONE = runs.DONE                       # the statuses of a run that has ended


def find_root(start: str | Path = ".") -> Path:
    """The project folder: the one holding testbench.yaml, at `start` or above it."""
    return C.find_root(start)


def load(root: str | Path) -> dict:
    """testbench.yaml, checked and with every default filled in. Read it again before each launch, as the CLI does:
    limits come from disk."""
    return C.load(root)


def plans(cfg: dict) -> dict[str, dict]:
    """Each plan's worst case: {name: {what, backend, gpu, usd_per_minute, max_minutes, worst_case_usd, plan}}. The
    plans themselves (steps, expectations) are cfg["plans"]."""
    return {name: budget.worst_case(cfg, name) for name in cfg["plans"]}


def estimate(cfg: dict, plan: str, approve: bool = False) -> dict:
    """A plan's worst case and whether it may launch now: {..., allowed, message}, as `testbench estimate --json`.
    approve=True only after a person agreed to the cost; it never gets past a limit."""
    if plan not in cfg["plans"]:
        raise ConfigError(C.no_plan(cfg, plan))
    ok, message, est = budget.check_launch(cfg, plan, approve)
    return {**est, "allowed": ok, "message": message}


def launch(root: str | Path, cfg: dict, plan: str, approve: bool = False, foreground: bool = False) -> Path:
    """Start a run of a plan inside the budget and return its folder. It runs in a background driver process that
    outlives this one; foreground=True runs it here and returns when it ends."""
    return runs.launch(Path(root), cfg, plan, approve=approve, foreground=foreground)


def find_run(root: str | Path, run: str | None = None) -> Path:
    """A run's folder from its id, a unique prefix of it, 'last' or 'last~N'; None: the latest run."""
    return runs.resolve(Path(root), run)


def wait(run_dir: str | Path, timeout: float | None = None, on_line=None) -> dict:
    """Follow a run until it ends or `timeout` seconds pass (None: until it ends); on_line gets each progress and log
    line. Returns its state.json: status is one of DONE once it ended."""
    return runs.wait(Path(run_dir), math.inf if timeout is None else timeout, on_line=on_line or (lambda line: None))


def cancel(run_dir: str | Path) -> str:
    """Stop a run, here and on any remote machine; says what happened."""
    return runs.cancel(Path(run_dir))


def report(run_dir: str | Path) -> dict | None:
    """The run's report (schema/report-v1.schema.json) once it ended, else None."""
    return R.load(Path(run_dir))


def verify(run_dir: str | Path) -> list[str]:
    """What no longer matches the evidence the run's report recorded: [] when every file still matches."""
    rep = R.load(Path(run_dir))
    if not rep or "evidence" not in rep:
        return [f"{Path(run_dir).name} has no recorded evidence to check: it has not ended, or it ran before "
                "agent-testbench recorded hashes"]
    return provenance.verify(Path(run_dir), rep)
