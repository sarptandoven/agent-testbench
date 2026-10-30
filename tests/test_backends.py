"""Backends as plugins: an installed package adds a backend through an entry point, and testbench prices it, runs
plans and sessions on it, and reports a broken one instead of failing for everyone."""
import textwrap
from pathlib import Path

import pytest

from agent_testbench import backends, budget, config as C
from conftest import cli, write_config

PLUGIN = '''
from agent_testbench.backends import Backend
from agent_testbench.backends.local import LocalBackend
from agent_testbench.session_backends import LocalSession


class FakeRunner(LocalBackend):
    def cost(self, rep):
        return backend.rate_per_minute(self.cfg, self.plan) * rep.get("minutes", 0)


class Fake(Backend):
    """This machine, priced as if it were a rented GPU box."""
    gpus = ("FAKE1", "FAKE2")
    remote = False
    startup_minutes = 1.0
    config_defaults = {"fake": {"usd_per_hour": 6.0}}

    def validate(self, cfg):
        if cfg["fake"]["usd_per_hour"] <= 0:
            raise ValueError("fake.usd_per_hour must be positive")

    def rate_per_minute(self, cfg, hardware):
        return cfg["fake"]["usd_per_hour"] / 60

    def plan_runner(self):
        return FakeRunner

    def session_class(self):
        return LocalSession

    def doctor(self):
        return [("fake box reachable", True, "yes")]


backend = Fake()
'''


@pytest.fixture
def plugin(tmp_path) -> dict:
    """fakebench installed the way pip would: a module plus a dist-info with entry points, on PYTHONPATH."""
    site = tmp_path / "site"
    (site / "fakebench-0.1.dist-info").mkdir(parents=True)
    (site / "fakebench.py").write_text(PLUGIN)
    (site / "fakebench-0.1.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: fakebench\nVersion: 0.1\n")
    (site / "fakebench-0.1.dist-info" / "entry_points.txt").write_text(textwrap.dedent("""\
        [agent_testbench.backends]
        fake = fakebench:backend
        broken = fakebench:no_such_thing
        """))
    return {"PYTHONPATH": str(site)}


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    write_config(root, {"project": "proj", "budget": {"max_run_usd": 5, "ask_above_usd": 5, "max_day_usd": 10},
                        "plans": {"on-fake": {"backend": "fake", "gpu": "FAKE1", "steps": [
                            {"name": "hello", "run": "echo hi from fake", "max_minutes": 4,
                             "expect": {"text": {"stdout": ["hi from fake"]}}}]}}})
    return root


def test_a_plugin_backend_runs_plans_and_sessions(tmp_path, plugin):
    root = _project(tmp_path)
    r = cli(root, "plans", env=plugin)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "on-fake: fake FAKE1" in r.stdout and "worst case $0.50" in r.stdout     # (4 + 1 start-up) min at $6/h

    r = cli(root, "run", "on-fake", "--wait", "120", env=plugin)
    assert r.returncode == 0 and "# PASS - plan `on-fake` on fake (FAKE1)" in r.stdout, r.stdout + r.stderr

    r = cli(root, "session", "start", "box", "--backend", "fake", "--gpu", "FAKE2", "--max-minutes", "3", env=plugin)
    assert r.returncode == 0 and "ready on fake FAKE2" in r.stdout, r.stdout + r.stderr
    assert "=> 4" in cli(root, "exec", "2 + 2", env=plugin).stdout
    assert "stopped after" in cli(root, "session", "stop", env=plugin).stdout

    r = cli(root, "session", "start", "--backend", "fake", "--gpu", "H100", env=plugin)
    assert r.returncode == 2 and "fake offers FAKE1, FAKE2" in r.stdout


def test_a_broken_plugin_is_reported_not_fatal(tmp_path, plugin):
    root = _project(tmp_path)
    r = cli(root, "doctor", env=plugin)
    assert "backends: local, modal, colab, fake" in r.stdout and "fake box reachable" in r.stdout
    assert "backend broken: fakebench:no_such_thing did not load" in r.stdout
    write_config(root, {"project": "proj", "plans": {"p": {"backend": "broken", "steps": [{"name": "a", "run": "true"}]}}})
    r = cli(root, "plans", env=plugin)
    assert r.returncode == 2 and "'broken'" in r.stderr and "did not load" in r.stderr


def test_without_the_plugin_the_backend_is_unknown(tmp_path):
    root = _project(tmp_path)
    r = cli(root, "plans")
    assert r.returncode == 2 and "install the package that provides it" in r.stderr


def test_registered_backend_prices_and_validates(tmp_path):
    class Rack(backends.Backend):
        name = "rack"
        gpus = ("RTX4090",)
        config_defaults = {"rack": {"usd_per_hour": 0.6}}

        def rate_per_minute(self, cfg, hardware):
            return cfg["rack"]["usd_per_hour"] / 60 * backends.split_gpu(hardware.get("gpu"))[1]

    backends.register(Rack())
    write_config(tmp_path, {"project": "r", "rack": {"usd_per_hour": 1.2},
                            "plans": {"two": {"backend": "rack", "gpu": "rtx4090:2",
                                              "steps": [{"name": "a", "run": "true", "max_minutes": 10}]}}})
    cfg = C.load(tmp_path)
    assert cfg["plans"]["two"]["workdir"] == "copy"                    # remote by default: plans run in a copy
    est = budget.worst_case(cfg, "two")
    assert abs(est["worst_case_usd"] - round(2 * 1.2 / 60 * 15, 4)) < 1e-9   # 10 min + 5 start-up, two GPUs
    write_config(tmp_path, {"project": "r", "plans": {"x": {"backend": "rack", "gpu": "A100",
                                                            "steps": [{"name": "a", "run": "true"}]}}})
    with pytest.raises(C.ConfigError, match="rack offers RTX4090"):
        C.load(tmp_path)
