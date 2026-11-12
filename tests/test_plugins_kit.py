"""The plug-in tooling every kind shares: the conformance kit (`testbench plugin test`, plugins.assert_conforms), the
backend contract it checks, `testbench plugins` and the plug-ins `testbench doctor` reports - and the example
backend package (examples/plugins/testbench-backend-example), installed the way pip would put it on the path."""
import json
import os
import sys
import textwrap
import types
from pathlib import Path

import pytest

from agent_testbench import backends as B, cli as CLI, plugins as P
from agent_testbench.backends.local import LocalBackend
from conftest import EXAMPLES, cli, write_config

EXAMPLE = EXAMPLES / "plugins" / "testbench-backend-example"
DIST = "testbench-backend-example"


def tb(capsys, *args) -> tuple[int, str]:
    with pytest.raises(SystemExit) as ex:
        CLI.main(list(args))
    out = capsys.readouterr()
    return ex.value.code or 0, out.out + out.err


@pytest.fixture(autouse=True)
def fresh_registry():
    """Each test loads the registries again, and leaves no plug-in it installed behind for the next test."""
    P.reset()
    yield
    P.reset()


def install(site: Path, dist: str, version: str, entry_points: str) -> Path:
    """A dist-info as pip writes one: importlib.metadata finds the package, and its entry points, on the path."""
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    (info / "entry_points.txt").write_text(textwrap.dedent(entry_points))
    return site


@pytest.fixture
def nice(tmp_path, monkeypatch) -> dict:
    """The example package installed: its dist-info (with the entry point its pyproject.toml declares) and its
    module on the path, in this process and in the PYTHONPATH of the CLI's processes."""
    site = install(tmp_path / "site", DIST, "0.1.0", """\
        [agent_testbench.backends]
        nice = testbench_nice:backend
        """)
    paths = [str(site), str(EXAMPLE)]
    for p in reversed(paths):
        monkeypatch.syspath_prepend(p)
    monkeypatch.delitem(sys.modules, "testbench_nice", raising=False)
    yield {"PYTHONPATH": os.pathsep.join(paths + [p for p in [os.environ.get("PYTHONPATH")] if p])}
    sys.modules.pop("testbench_nice", None)


class Remote(B.Backend):
    """A remote backend that keeps every rule: priced through its own setting, stopped on the remote side."""
    name = "rack"
    gpus = ("RTX4090",)
    startup_minutes = 2.0
    hard_stop = "a watchdog on the machine ends the work at its time limit"
    config_defaults = {"rack": {"usd_per_hour": 0.0}}

    def rate_per_minute(self, cfg, hardware):
        return cfg["rack"]["usd_per_hour"] / 60 * B.split_gpu(hardware.get("gpu"))[1]

    def plan_runner(self):
        return LocalBackend


# ---- the backend contract ----------------------------------------------------------------------------------------

def test_every_built_in_backend_keeps_the_contract():
    for name in B.BUILTIN:
        assert P.check("backends", B.get(name), name) == [], name
        P.assert_conforms("backends", B.get(name))
    assert P.check("backends", Remote()) == []
    assert B.names() == list(B.BUILTIN), "checking a backend leaves the registry as it was"


def test_the_contract_names_each_rule_a_backend_breaks():
    class Sloppy(B.Backend):
        name = "sloppy"
        gpus = ("A 100", "T4")
        startup_minutes = -1
        max_session_minutes = float("nan")              # NaN compares false with every limit
        config_defaults = {"budget": {"max_run_usd": 1000}, "ssh": {}, "sloppy": "fast"}

        def rate_per_minute(self, cfg, hardware):
            return float("inf")

        def plan_runner(self):
            return object

        def session_class(self):
            return type("Half", (), {"start": lambda self: {}, "stop": lambda self: None})

    problems = P.check("backends", Sloppy())
    text = "\n".join(problems)
    for says in ("config_defaults is", "startup_minutes is -1", "max_session_minutes is nan",
                 "gpus lists ['A 100']", "costs inf at worst",
                 "plan_runner() returns object, which has no run, cancel, cost",
                 "session_class() returns Half, which has no alive, exec, ls, get, put, install, sync, shell"):
        assert says in text, f"{says!r} not in:\n{text}"
    Sloppy.config_defaults = {"budget": {"max_run_usd": 1000}, "ssh": {}}
    text = "\n".join(P.check("backends", Sloppy()))
    assert "`budget:`, which is testbench.yaml's own" in text, "a plug-in may not raise the budget's defaults"
    assert "`ssh:`, which is the ssh backend's section" in text
    with pytest.raises(AssertionError, match=r"(?s)sloppy does not keep the backends contract:\n- config_defaults"):
        P.assert_conforms("backends", Sloppy())

    class Nameless(Remote):
        name = ""
    blank = Nameless()
    assert P.check("backends", blank, "rack2") == [] and blank.name == "", "checking never changes the object"
    assert "is installed as 'other'" in P.check("backends", Remote(), "other")[0]
    assert P.check("backends", type("Bad", (B.Backend,), {"name": "bad box"})())[0].startswith(
        "its name 'bad box' cannot be written as `backend:`")
    assert P.check("backends", 42) == ["it cannot be made into a backend: TypeError: 42 is not an "
                                       "agent_testbench.backends.Backend"]
    assert P.check("backends", B.Backend)[-1] == ("it runs neither plans nor sessions: plan_runner() or "
                                                  "session_class() returns a class")


def test_a_remote_backend_prices_its_machine_and_stops_itself():
    class Unpriced(Remote):
        config_defaults = {}

        def rate_per_minute(self, cfg, hardware):
            return 0.0
    [problem] = P.check("backends", Unpriced())
    assert "costs $0 at worst, and it has no price setting of its own" in problem
    assert "rack: {usd_per_hour: 0}" in problem

    class Ignores(Remote):
        def rate_per_minute(self, cfg, hardware):
            return 0.0
    [problem] = P.check("backends", Ignores())
    assert "without a GPU or with gpu: RTX4090 costs $0 at worst, even with rack.usd_per_hour set to 1" in problem

    class Unstopped(Remote):
        hard_stop = ""
    [problem] = P.check("backends", Unstopped())
    assert "say what does it in hard_stop" in problem
    Unstopped.max_session_minutes = 120
    assert P.check("backends", Unstopped()) == []

    class Instant(Remote):
        startup_minutes = 0
    assert "startup_minutes is 0" in P.check("backends", Instant())[0], "a remote machine always takes some start-up"

    class Raises(Remote):
        def rate_per_minute(self, cfg, hardware):
            return cfg["rack_prices"]["usd"]
    assert P.check("backends", Raises()) == ["pricing a one-step plan on it raised KeyError: 'rack_prices' (does it "
                                             "read a setting its config_defaults do not have?)"]


# ---- the kit: `testbench plugin test` ----------------------------------------------------------------------------

def test_plugin_test_finds_a_module_in_the_current_folder_before_it_is_packaged(tmp_path, monkeypatch, capsys):
    """docs/plugins.md: `testbench plugin test mod:obj --kind ...` tests one object "before it is packaged" - in the
    folder being written, as `python -m` would find it, though the testbench command does not put the folder on the
    path itself."""
    (tmp_path / "tb_here_runtime.py").write_text(textwrap.dedent("""\
        from agent_testbench.config import ConfigError
        from agent_testbench.expect import Check
        class MaxRuntimeSeconds(Check):
            name = "max_runtime_seconds"
            example = 60
            def validate(self, where, value):
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise ConfigError(f"`{where}` is the most seconds the step may take, such as 60")
            def check(self, value, *, workdir, result, add):
                add(f"runtime at most {value} s", True, "1 (wanted <= 60)")
        """))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", ".", str(tmp_path))])
    try:
        code, out = tb(capsys, "plugin", "test", "tb_here_runtime:MaxRuntimeSeconds", "--kind", "checks")
        assert code == 0 and out.startswith("ok   checks max_runtime_seconds (tb_here_runtime:MaxRuntimeSeconds)"), out
        code, out = tb(capsys, "plugin", "test", "tb_not_here_at_all:X", "--kind", "checks")
        assert code == 2 and "cannot load tb_not_here_at_all:X: ModuleNotFoundError" in out, out
        assert os.getcwd() in sys.path, "the current folder is added, as python -m adds it"
    finally:
        sys.modules.pop("tb_here_runtime", None)


def test_plugin_test_on_one_object(tmp_path, monkeypatch, capsys):
    (tmp_path / "rackmod.py").write_text(textwrap.dedent("""\
        from agent_testbench.backends import Backend
        from agent_testbench.backends.local import LocalBackend
        class Rack(Backend):
            name = "rack"
        class Here(Rack):
            remote = False
            def plan_runner(self):
                return LocalBackend
        good, bad = Here(), Rack()
        """))
    monkeypatch.syspath_prepend(str(tmp_path))
    code, out = tb(capsys, "plugin", "test", "rackmod:good", "--kind", "backends")
    assert code == 0 and out == "ok   backends rack (rackmod:good): keeps its kind's contract\n", out
    code, out = tb(capsys, "plugin", "test", "rackmod:bad", "--kind", "backends")
    assert code == 1 and out.startswith("FAIL backends rack (rackmod:bad): 3 problems\n     - "), out
    assert "say what does it in hard_stop" in out and "costs $0 at worst" in out and "runs neither plans" in out
    code, out = tb(capsys, "plugin", "test", "rackmod:bad", "--kind", "backends", "--json")
    got = json.loads(out)
    assert code == 1 and got["ok"] is False and len(got["results"][0]["problems"]) == 3

    for args, says in [(["rackmod:nope", "--kind", "backends"], "cannot load rackmod:nope: AttributeError"),
                       (["no_such_module_here:x"], "cannot load no_such_module_here:x: ModuleNotFoundError"),
                       (["rackmod:good", "--kind", "widgets"], "no plug-in kind 'widgets'; the kinds are backends"),
                       (["no-such-package-at-all"], "no installed package 'no-such-package-at-all'"),
                       (["agent-testbench"], "agent-testbench declares no plug-ins"),
                       (["rackmod:good", "--installed"], "test rackmod:good or --installed, not both"),
                       ([], "say what to test: module:attr")]:
        code, out = tb(capsys, "plugin", "test", *args)
        assert code == 2 and says in out, f"plugin test {args}: exit {code}\n{out}"


def test_without_kind_the_kit_tests_an_object_as_the_only_kind_that_takes_it(monkeypatch):
    monkeypatch.setattr(P, "KINDS", {"backends": P.KINDS["backends"], "widgets": "tb_fake_widgets"})
    widgets = types.ModuleType("tb_fake_widgets")
    widgets.KIND = P.Kind("widgets", "widget", "spin", {}, prepare=lambda obj, name: obj.spin and obj)
    monkeypatch.setitem(sys.modules, "tb_fake_widgets", widgets)
    assert P.conformance("agent_testbench.backends.local:backend")[0]["kind"] == "backends"
    with pytest.raises(P.PluginError, match=r"which kind of plug-in is json:dumps\? no kind takes it as it is; say "
                                            r"which with --kind \(backends, widgets\)"):
        P.conformance("json:dumps")
    widgets.KIND.prepare = lambda obj, name: obj
    with pytest.raises(P.PluginError, match="it could be any of backends, widgets; say which with --kind"):
        P.conformance("agent_testbench.backends.local:backend")


def test_entry_points_that_take_a_built_in_name_or_another_name_or_kind(monkeypatch):
    class EP:
        def __init__(self, name, value, obj, group="agent_testbench.backends"):
            self.name, self.value, self.group, self.obj = name, value, group, obj

        def load(self):
            return self.obj
    eps = [EP("local", "evil:backend", Remote()), EP("rack2", "racks:backend", Remote())]
    monkeypatch.setattr(P, "entry_points", lambda kind: eps if kind == "backends" else [])
    found = {r["name"]: r["problems"] for r in P.conformance(installed=True)}
    assert found["local"][0] == ("local is a built-in backend, and a plug-in cannot replace one: it would be skipped. "
                                 "Give its entry point another name")
    assert "it calls itself 'rack' but is installed as 'rack2'" in found["rack2"][0]
    assert "is not a kind of plug-in" in P._test_entry_point("widgets", EP("w", "w:w", 1))["problems"][0]
    assert B.names() == list(B.BUILTIN) + ["rack2"] and type(B.get("local")).__name__ == "Local", "the built-in stays"


def test_kinds_not_open_yet_are_named(tmp_path, monkeypatch):
    monkeypatch.setitem(P.KINDS, "widgets", "agent_testbench.no_such_module_yet")
    with pytest.raises(P.PluginError, match="agent_testbench.no_such_module_yet is not in this version of testbench"):
        P.kind("widgets")
    (tmp_path / "tb_fake_raises.py").write_text("raise RuntimeError('half-written kind')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setitem(P.KINDS, "gadgets", "tb_fake_raises")
    rows = {r["kind"]: r for r in P.inventory()}
    row = rows["widgets"]
    assert row["open"] is False and row["builtin"] == [] and "cannot take plug-ins yet" in row["why"]
    assert rows["gadgets"]["error"] == "tb_fake_raises did not import: RuntimeError: half-written kind", \
        "a kind whose module is broken is reported; the others are still listed"
    assert rows["backends"]["builtin"] == list(B.BUILTIN)
    assert "does not take widgets plug-ins yet" in P._test_entry_point(
        "widgets", types.SimpleNamespace(name="w", value="w:w"))["problems"][0]


# ---- `testbench plugins` and `testbench doctor` ------------------------------------------------------------------

def test_plugins_lists_every_kind_with_its_builtins_installed_and_broken(tmp_path, plugin):
    r = cli(tmp_path, "plugins", env=plugin)
    assert r.returncode == 0 and "Traceback" not in r.stderr, r.stderr
    lines = r.stdout.splitlines()
    for kind in P.KINDS:
        assert any(line.startswith(f"{kind:<10} ") for line in lines), f"{kind} is not listed:\n{r.stdout}"
    assert "backends   where plans and sessions run\n  built in   local, modal, colab, ssh, docker, kubernetes\n" \
           "  installed  fake (fakebench:backend, fakebench 0.1)\n  broken     broken: fakebench:no_such_thing did " \
           "not load: AttributeError" in r.stdout, r.stdout
    assert "testbench plugin test PACKAGE" in r.stdout

    r = cli(tmp_path, "plugins", "--json", env=plugin)
    rows = {row["kind"]: row for row in json.loads(r.stdout)["kinds"]}
    assert r.returncode == 0 and list(rows) == list(P.KINDS)
    backends = rows["backends"]
    assert backends["open"] and backends["builtin"] == list(B.BUILTIN)
    assert backends["installed"] == [{"name": "fake", "from": "fakebench:backend", "package": "fakebench 0.1"}]
    assert backends["broken"][0]["name"] == "broken" and backends["broken"][0]["package"] == "fakebench 0.1"
    assert all(isinstance(row["open"], bool) for row in rows.values())

    r = cli(tmp_path, "plugin", "test", "--installed", env=plugin)
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ok   backends fake (fakebench:backend, fakebench 0.1): keeps its kind's contract" in r.stdout
    assert "FAIL backends broken (fakebench:no_such_thing, fakebench 0.1): 1 problem\n     - it does not load: " \
           "AttributeError" in r.stdout
    assert "2 plug-ins tested: 1 does not keep its kind's contract" in r.stdout


def test_doctor_reports_broken_plugins_of_every_kind(tmp_path, plugin, monkeypatch, capsys):
    r = cli(tmp_path, "doctor", env=plugin)
    assert "ok   plug-ins: fake (backends) (`testbench plugins` lists them)" in r.stdout, r.stdout
    assert "no   backend broken: fakebench:no_such_thing did not load: AttributeError" in r.stdout
    assert "`testbench plugin test fakebench` says more" in r.stdout

    # any kind: one that is open (its module defines KIND) and one that is not open yet
    mod = types.ModuleType("tb_fake_greeters")
    mod.KIND = P.Kind("greeters", "greeter", "say hello", {})
    monkeypatch.setitem(sys.modules, "tb_fake_greeters", mod)
    monkeypatch.setitem(sys.modules, "tb_fake_widgets", types.ModuleType("tb_fake_widgets"))
    monkeypatch.setitem(P.KINDS, "greeters", "tb_fake_greeters")
    monkeypatch.setitem(P.KINDS, "widgets", "tb_fake_widgets")

    def boom():
        raise ImportError("no module named greetings_extra")
    eps = {"greeters": [types.SimpleNamespace(name="shaky", value="shaky:it", load=boom)],
           "widgets": [types.SimpleNamespace(name="spinner", value="spin:it", load=boom)]}
    monkeypatch.setattr(P, "entry_points", lambda kind: eps.get(kind, []))
    checks = {name: (ok, detail) for name, ok, detail in CLI._plugin_checks()}
    assert checks["greeter shaky"] == (False, "shaky:it did not load: ImportError: no module named greetings_extra")
    assert checks["widgets plug-in spinner"][0] is False
    assert "does not take widgets plug-ins yet, so it is not used" in checks["widgets plug-in spinner"][1]
    code, out = tb(capsys, "plugins")
    assert code == 0 and "greeters   say hello\n  broken     shaky: shaky:it did not load" in out, out
    assert "widgets    not yet open to plug-ins\n  waiting    spinner (spin:it): not used until" in out, out


# ---- the example package -----------------------------------------------------------------------------------------

def test_the_example_backend_package_keeps_its_contract(nice, tmp_path, capsys):
    [result] = P.conformance(DIST)
    assert result == {"kind": "backends", "name": "nice", "source": "testbench_nice:backend",
                      "package": f"{DIST} 0.1.0", "problems": []}
    assert P.conformance("testbench_nice:backend")[0]["kind"] == "backends", "the kind its entry point declares"
    assert P.get("backends", "nice").name == "nice" and "nice" in B.names()
    sys.path.insert(0, str(EXAMPLE))
    try:
        import test_nice                           # the package's own test: plugins.assert_conforms
        test_nice.test_nice_keeps_the_backend_contract()
    finally:
        sys.path.remove(str(EXAMPLE))
        sys.modules.pop("test_nice", None)

    r = cli(tmp_path, "plugin", "test", DIST, env=nice)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == f"ok   backends nice (testbench_nice:backend, {DIST} 0.1.0): keeps its kind's contract\n"
    r = cli(tmp_path, "plugins", env=nice)
    assert f"  installed  nice (testbench_nice:backend, {DIST} 0.1.0)" in r.stdout, r.stdout


@pytest.mark.posix
def test_a_plan_runs_on_the_example_backend_under_nice(nice, tmp_path):
    write_config(tmp_path, {"project": "nice", "plans": {"low": {"backend": "nice", "steps": [
        {"name": "priority", "run": f"\"{sys.executable}\" -c \"import os; print('niceness', os.nice(0))\"",
         "max_minutes": 2,
         "expect": {"text": {"stdout": ["niceness"]}}}]}}})
    before = os.nice(0)
    r = cli(tmp_path, "plans", env=nice)
    assert r.returncode == 0 and "low: nice" in r.stdout and "worst case $0.00" in r.stdout, r.stdout + r.stderr
    r = cli(tmp_path, "run", "low", "--wait", "120", env=nice)
    assert r.returncode == 0 and "# PASS - plan `low` on nice" in r.stdout, r.stdout + r.stderr
    log = next((tmp_path / ".testbench" / "runs").glob("*/steps/*/log.txt")).read_text()
    assert f"niceness {min(before + 10, 19)}" in log, log

    write_config(tmp_path, {"project": "nice", "nice": {"niceness": 25},
                            "plans": {"low": {"backend": "nice", "steps": ["true"]}}})
    r = cli(tmp_path, "plans", env=nice)
    assert r.returncode == 2 and "`nice.niceness` is 25; write a whole number from 1 to 19" in r.stderr, r.stderr
