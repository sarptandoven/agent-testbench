"""Shared by the trust tests (test_trust.py, test_budget_rules.py, test_guard_hook.py, test_fuzz_*.py): the guard hook
loaded in this process, and hypothesis settings that run a few seconds by default and many more by hand.

    TESTBENCH_FUZZ_EXAMPLES=20000 python -m pytest -q tests/test_fuzz_guard.py     # every fuzz test, 20000 examples
"""
import importlib.util
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def fuzz(n: int):
    """Hypothesis settings: n examples, the same every run (derandomized, no example database), unless
    TESTBENCH_FUZZ_EXAMPLES says how many to try, which also draws them at random for a bug hunt."""
    from hypothesis import HealthCheck, settings
    many = os.environ.get("TESTBENCH_FUZZ_EXAMPLES")
    return settings(max_examples=int(many) if many else n, derandomize=not many, database=None, deadline=None,
                    suppress_health_check=list(HealthCheck))


def guard_module(user_dir: Path | None = None):
    """hooks/guard.py as a module, with ~/.testbench at user_dir and no `testbench estimate` behind its questions
    (a subprocess per call). Its decision(payload) is what the hook answers Claude Code: (decision, reason) or None."""
    spec = importlib.util.spec_from_file_location("testbench_guard_under_test", REPO / "hooks" / "guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if user_dir is not None:
        mod.USER_DIR = Path(user_dir).resolve()
    mod.estimate = lambda root, plan: ""
    return mod


def decided(mod, cwd: Path, command: str):
    """The guard's decision ('ask', 'deny' or None) on a Bash command run in cwd."""
    got = mod.decision({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})
    return got[0] if got else None
