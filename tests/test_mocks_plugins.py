"""Mock SDKs as plug-ins (mocks.py, docs/plugins-mocks.md): the five built-in mocks are Mock objects in the "mocks"
registry and keep its contract; a package adds one through an entry point, a step lists it under `mocks:` like a
built-in, and its calls land in the same mock_calls log. The contract names what is wrong with a mock in plain words.
Every mock here runs with the network cut: one that reached for it would fail."""
import json
import os
import re
import shutil
import sys
import textwrap
import tomllib

import pytest

from agent_testbench import config as C, expect as X, mocks as M, plugins as P
from conftest import REPO, cli, write_config

EXAMPLE = REPO / "examples" / "plugins" / "testbench-mock-example"
GUARD = """import socket
def _no_network(*a, **k):
    raise AssertionError("a mock opened a network connection")
socket.socket.connect = _no_network
"""


def _step(**step):
    return {"project": "p", "plans": {"p": {"steps": [{"name": "s", "run": "true", **step}]}}}


@pytest.fixture
def weatherkit(monkeypatch):
    """The example plug-in's mock, registered by hand in this process."""
    monkeypatch.syspath_prepend(str(EXAMPLE))
    import testbench_mock_example
    P.register("mocks", "weatherkit", testbench_mock_example.mock)
    yield testbench_mock_example.mock
    P.unregister("mocks", "weatherkit")


@pytest.fixture
def installed(tmp_path) -> dict:
    """testbench-mock-example installed the way pip would - its package, and a dist-info with the entry points of its
    own pyproject.toml - in a folder on PYTHONPATH, where the CLI and the run's driver find it for themselves."""
    site = tmp_path / "site"
    shutil.copytree(EXAMPLE / "testbench_mock_example", site / "testbench_mock_example",
                    ignore=shutil.ignore_patterns("__pycache__"))
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    info = site / f"testbench_mock_example-{project['version']}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {project['version']}\n")
    (info / "entry_points.txt").write_text("".join(f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in eps.items())
                                                   for group, eps in project["entry-points"].items()))
    return {"PYTHONPATH": os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH")) if p)}


# ---- the built-ins are plug-ins ------------------------------------------------------------------------------

def test_the_built_in_mocks_are_plug_ins_and_nothing_a_step_sees_changes(tmp_path):
    assert P.kind("mocks") is M.KIND and list(M.KIND.builtin) == list(M.SDKS) == M.names()[:5]
    assert all(P.builtin("mocks", n) and isinstance(M.get(n), M.Mock) for n in M.SDKS)
    assert M.get("ib_async").imports == ("ib_async", "ib_insync") and M.get("ib_async").folders() == [
        M.CLIENTS / "ib_async"]
    assert M.client_paths(M.SDKS) == [str(M.CLIENTS / n) for n in M.SDKS] + [str(M.CLIENTS / "shared")]
    assert M.call_keys("anthropic") == X.MOCK_KEYS and M.call_keys("ccxt") == X.BROKER_KEYS
    env = M.client_env({"anthropic": {"replies": "r.yaml", "fail_on_call": 2}, "ib_async": {"prices": "p.csv"},
                        "openai": {}}, tmp_path)
    assert json.loads(env["TESTBENCH_MOCK_ANTHROPIC"]) == {"replies": str(tmp_path / "r.yaml"), "fail_on_call": 2}
    assert json.loads(env["TESTBENCH_MOCK_IB_ASYNC"]) == {"prices": str(tmp_path / "p.csv")}
    assert env["TESTBENCH_MOCK_OPENAI"] == "{}"
    with pytest.raises(C.ConfigError, match=r"`steps\[0\].expect.mock_calls` names 'gemini'; testbench has mock "
                                            "clients for anthropic, openai, alpaca, ccxt, ib_async"):
        M.call_keys("gemini", "steps[0].expect.mock_calls")


@pytest.mark.parametrize("name", M.SDKS)
def test_every_built_in_mock_keeps_the_contract(name):
    assert P.check("mocks", M.get(name)) == []


# ---- the contract ----------------------------------------------------------------------------------------------

LEAKY = """
import socket

class Client:
    def call(self):
        try:
            socket.create_connection(("api.leaky.example", 443), timeout=1)
        except OSError:
            pass

    def __getattr__(self, name):
        return lambda *a, **k: None
"""
NEEDS_TESTBENCH = """
from agent_testbench.mocks import StrictAPI
"""
GOOD = """
from _testbench_strict import Endpoint, StrictAPI
api = StrictAPI({"get": Endpoint()}, sdk="good")

class Client:
    def get(self):
        return api.check("get")

    def __getattr__(self, name):
        raise api.not_modelled(name, "Client.")
"""
QUIET = """
from _testbench_strict import Endpoint, StrictAPI
api = StrictAPI({"get": Endpoint()}, sdk="quiet")

class Client:
    def get(self):
        return api.check("get")

    def __getattr__(self, name):
        raise AttributeError("no such thing")
"""


def _sdk(tmp_path, **modules):
    for name, code in modules.items():
        (tmp_path / "sdk" / name).mkdir(parents=True)
        (tmp_path / "sdk" / name / "__init__.py").write_text(code)
    return tmp_path / "sdk"


def test_the_contract_says_what_is_wrong_with_a_mock(tmp_path):
    sdk = _sdk(tmp_path, leaky=LEAKY, needs=NEEDS_TESTBENCH, quiet=QUIET, good=GOOD)
    leaky = M.Mock("leaky", sdk, ["leaky", "leaky_extras"], call_keys=["calls", "refused", "charges"],
                   example="import leaky\nclient = leaky.Client()\nclient.call()\n")
    found = P.check("mocks", leaky)
    assert found[0] == (f"its path has no package 'leaky_extras': put leaky_extras/__init__.py (or leaky_extras.py) "
                        f"in {sdk}")
    assert found[1].startswith("its call_keys name charges, which testbench does not count")
    assert found[2] == "it reached for the network (to ('api.leaky.example', 443)); a mock never touches it"
    assert found[3].startswith("its example logged no call under 'leaky' in mock_calls.jsonl, so `mock_calls:` rules "
                               "would count nothing")
    assert found[4].startswith("calling client.testbench_unmodelled_call() returned None instead of raising an error")
    assert len(found) == 5

    needs = M.Mock("needs", sdk, ["needs"], example="import needs\n")
    assert P.check("mocks", needs) == [
        "its code imports agent_testbench in the step, whose Python may not have agent-testbench (from uvx or pipx it "
        "is the project's); import StrictAPI, Endpoint and step_options from _testbench_strict instead, the same "
        "objects, which every mocked step has on its PYTHONPATH"]
    quiet = M.Mock("quiet", sdk, ["quiet"], example="import quiet\nclient = quiet.Client()\nclient.get()\n")
    assert P.check("mocks", quiet) == ["calling client.testbench_unmodelled_call() raised AttributeError, but its "
                                       "message does not name the method: no such thing"]
    unbound = M.Mock("quiet", sdk, ["quiet"], example="import quiet\nquiet.Client().get()\n")
    assert P.check("mocks", unbound) == ["its example does not bind `client` (the object a step calls), so calling "
                                         "something it does not model cannot be checked"]
    failing = M.Mock("quiet", sdk, ["quiet"], example="import quiet\nclient = quiet.Client()\nclient.get(1)\n")
    assert P.check("mocks", failing)[0].startswith("its example failed: TypeError:")

    class Lax(M.Mock):
        def validate(self, where, opts, root):
            return opts

    class Rough(M.Mock):
        def validate(self, where, opts, root):
            raise KeyError(next(iter(opts), "nothing"))
    example = "import good\nclient = good.Client()\nclient.get()\n"
    assert P.check("mocks", M.Mock("good", sdk, ["good"], example=example)) == []
    lax = P.check("mocks", Lax("good", sdk, ["good"], example=example))
    assert lax == ["validate accepted an option it does not have (testbench_junk_option: 1); refuse unknown options "
                   "with a ConfigError naming the field, so a misspelt option fails before the step runs"]
    rough = P.check("mocks", Rough("good", sdk, ["good"], example=example))
    assert rough[0].startswith("validate raised KeyError for an unknown option, where a ConfigError naming the field")
    assert rough[1].startswith("validate refuses no options at all (KeyError: 'nothing')")

    assert P.check("mocks", M.Mock("x", "sdk", ["x"])) == [
        "its path sdk is relative; give the folder's full path, e.g. Path(__file__).parent / 'sdk'",
        "it has no example: a few lines a step would run that make one call it answers, with the client bound to "
        "`client`"]
    assert P.check("mocks", M.Mock("x", tmp_path / "nope", [], call_keys=["calls"])) == [
        f"its path {tmp_path / 'nope'} does not exist (or is not a folder)",
        "its imports are []; list the module names it stands in for, e.g. ['weatherkit']",
        "its call_keys ['calls'] leave out calls or refused, which every mock is counted by (a step with mocks: fails "
        "on a refused call unless it says otherwise)",
        "it has no example: a few lines a step would run that make one call it answers, with the client bound to "
        "`client`"]
    assert P.check("mocks", 42) == ["it cannot be made into a mock: TypeError: a mock is an agent_testbench.mocks.Mock "
                                    "(an instance, or a subclass), not int"]
    assert P.check("mocks", M.Mock("weather", sdk, ["quiet"]), "weatherkit")[0].startswith(
        "it cannot be made into a mock: PluginError: it calls itself 'weather' but is installed as 'weatherkit'")
    with pytest.raises(TypeError, match="Mock\\(\\) has no 'import'; it takes name, path, imports"):
        M.Mock("x", sdk, imports=["x"], **{"import": "x"})


# ---- StrictAPI writes the log the built-in mocks write -----------------------------------------------------------

def test_strict_api_logs_every_call_to_the_step_s_log_under_the_mock_s_name(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTBENCH_MOCK_CALLS", str(tmp_path / "mock_calls.jsonl"))
    monkeypatch.delenv("TESTBENCH_MOCK_LOG", raising=False)
    api = M.StrictAPI({"charge": M.Endpoint(required={"amount": int}, ranges={"amount": (1, 1000)})}, sdk="acme")
    assert api.check("charge", {"amount": 5}) == {"amount": 5}
    with pytest.raises(M.UnexpectedCall, match=r"mock acme: charge amount=5000 outside 1\.\.1000"):
        api.check("charge", {"amount": 5000})
    err = api.not_modelled("refund", "Client.")
    assert isinstance(err, AttributeError) and isinstance(err, M.UnexpectedCall) and err.name == "refund"
    assert str(err).startswith("mock acme: Client.refund is not modelled; the mock answers charge.")
    private = api.not_modelled("_ipython_display_")
    assert type(private) is AttributeError and str(private) == "_ipython_display_"
    lines = [json.loads(line) for line in (tmp_path / "mock_calls.jsonl").read_text().splitlines()]
    assert [(r["sdk"], r["endpoint"], r["call"], (r["error"] or {}).get("type")) for r in lines] == [
        ("acme", "charge", 1, None), ("acme", "charge", 2, "UnexpectedCall"),
        ("acme", "Client.refund", 3, "NotModelled")]
    checks = X.check({"mock_calls": {"acme": {"calls": 3, "refused": "<= 1"}}}, workdir=tmp_path,
                     result={"step_dir": str(tmp_path), "mocks": {"acme": {}}})
    assert [(c["check"], c["ok"]) for c in checks] == [("mock acme calls 3", True), ("mock acme refused <= 1", False)]
    assert "call 2: UnexpectedCall: mock acme: charge amount=5000 outside 1..1000" in checks[1]["detail"]

    M.StrictAPI({"charge": M.Endpoint()}).check("charge")              # without sdk: no line in the step's log
    assert len((tmp_path / "mock_calls.jsonl").read_text().splitlines()) == 3
    assert sys.modules["_testbench_strict"].StrictAPI is M.StrictAPI, "one module, whichever way it is imported"
    assert M.strict_core().__file__ == str(M.CLIENTS / "shared" / "_testbench_strict.py")


def test_a_mock_reads_its_options_from_the_step(monkeypatch):
    assert M.env_name("ib_async") == "TESTBENCH_MOCK_IB_ASYNC" and M.env_name("my-api") == "TESTBENCH_MOCK_MY_API"
    monkeypatch.setenv("TESTBENCH_MOCK_MY_API", '{"rates": "/abs/rates.json"}')
    assert M.step_options("my-api") == {"rates": "/abs/rates.json"} and M.step_options("other") == {}
    assert M.StrictAPI({}, sdk="my-api").options == {"rates": "/abs/rates.json"}
    monkeypatch.setenv("TESTBENCH_MOCK_MY_API", "{rates")
    with pytest.raises(M.UnexpectedCall, match="TESTBENCH_MOCK_MY_API is not JSON"):
        M.step_options("my-api")


# ---- a plug-in mock in testbench.yaml ----------------------------------------------------------------------------

def test_testbench_yaml_takes_a_plug_in_mock_and_its_own_checks(tmp_path, weatherkit):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "f.yaml").write_text("Paris: [{summary: drizzle, high_c: 14, low_c: 8}]\n")
    (tmp_path / "flat.yaml").write_text("Paris: drizzle\n")
    cfg = C.validate(_step(mocks={"weatherkit": {"forecasts": "tests/f.yaml"}, "openai": None}), tmp_path)
    assert cfg["plans"]["p"]["steps"][0]["mocks"] == {"weatherkit": {"forecasts": "tests/f.yaml"}, "openai": {}}
    assert C.validate(_step(mocks="weatherkit"), tmp_path)["plans"]["p"]["steps"][0]["mocks"] == {"weatherkit": {}}
    for mocks, why in [({"weatherkit": {"forecast": "f.yaml"}}, "`plans.p.steps[0].mocks.weatherkit` has no "
                        "'forecast' (did you mean 'forecasts'?); the options are forecasts"),
                       ({"weatherkit": {"forecasts": "nope.yaml"}}, "mocks.weatherkit.forecasts`: nope.yaml does not "
                        "exist"),
                       ({"weatherkit": {"forecasts": "flat.yaml"}}, "flat.yaml maps each city to its days"),
                       ({"weatherkit": ["Paris"]}, "mocks.weatherkit` is the mock's options, a mapping; the options "
                        "are forecasts"),
                       (["gemini"], "names 'gemini'; testbench has mock clients for anthropic, openai, alpaca, ccxt, "
                        "ib_async, and installed plug-ins add weatherkit. For another API")]:
        with pytest.raises(C.ConfigError, match=re.escape(why)):
            C.validate(_step(mocks=mocks), tmp_path)
    assert M.call_keys("weatherkit") == ("calls", "refused")
    sdk = str(EXAMPLE / "testbench_mock_example" / "sdk")
    assert M.client_paths(["weatherkit", "openai"]) == [sdk, str(M.CLIENTS / "openai"), str(M.CLIENTS / "shared")]
    env = M.client_env({"weatherkit": {"forecasts": "tests/f.yaml"}}, tmp_path)
    assert json.loads(env["TESTBENCH_MOCK_WEATHERKIT"]) == {"forecasts": str(tmp_path / "tests" / "f.yaml")}


class EP:
    def __init__(self, name, value, load):
        self.name, self.value, self._load = name, value, load

    def load(self):
        return self._load()


@pytest.fixture
def entry_points(monkeypatch):
    eps = []
    monkeypatch.setattr(P, "entry_points", lambda kind_name: eps if kind_name == "mocks" else [])
    P.reset("mocks")
    yield eps
    P.reset("mocks")


def test_a_mock_plug_in_that_did_not_load_or_takes_a_built_in_s_name_is_named(tmp_path, entry_points):
    def boom():
        raise ImportError("No module named 'shaky_sdk'")
    entry_points += [EP("shaky", "shaky:mock", boom), EP("openai", "evil:mock", lambda: M.Mock("openai"))]
    assert M.names() == list(M.SDKS) and M.get("openai") is M.OPENAI, "a built-in is never replaced"
    assert "tries to replace a mock that already exists" in P.broken("mocks")["openai"]
    with pytest.raises(C.ConfigError, match=re.escape("`plans.p.steps[0].mocks` names 'shaky', a mock that is "
                                                      "installed but did not load (shaky:mock did not load: ImportError: "
                                                      "No module named 'shaky_sdk'); `testbench plugins` shows it")):
        C.validate(_step(mocks=["shaky"]), tmp_path)


# ---- end to end: a plan whose step lists an installed mock plug-in ------------------------------------------------

APP = GUARD + """
import json, sys
import weatherkit
client = weatherkit.Client("any-key")
out = {}
for city in sys.argv[1:]:
    if city == "--radar":
        try:
            client.radar("Paris")
        except AttributeError as exc:            # swallowed: the step still fails on the refused call
            print("no radar:", exc)
        continue
    try:
        out[city] = client.forecast(city, days=2)["days"][0]["summary"]
    except weatherkit.WeatherKitError as exc:    # swallowed too
        print("no forecast:", exc)
json.dump(out, open("out.json", "w"))
print("forecasts", out)
"""


def test_a_plan_runs_against_an_installed_mock_plug_in(tmp_path, installed):
    root = tmp_path / "weather"
    (root / "tests").mkdir(parents=True)
    (root / "app.py").write_text(APP)
    (root / "tests" / "forecasts.yaml").write_text(textwrap.dedent("""\
        Paris:
          - {summary: drizzle, high_c: 14, low_c: 8}
          - {summary: sunny, high_c: 19, low_c: 9}
        London:
          - {summary: fog, high_c: 11, low_c: 6}
        """))
    own = {"forecasts": "tests/forecasts.yaml"}
    keep_going = {"continue_on_failure": True}
    write_config(root, {"project": "weather", "plans": {
        "weather": {"steps": [
            {"name": "forecast", "run": "python app.py Paris London", "mocks": {"weatherkit": own},
             "expect": {"json": {"out.json": {"Paris": "drizzle", "London": "fog"}}}},
            {"name": "shipped forecasts", "run": "python app.py Paris", "mocks": ["weatherkit"],
             "expect": {"json": {"out.json": {"Paris": "sunny"}}}}]},
        "strays": {"steps": [
            {"name": "unknown city", "run": "python app.py Paris Oslo", "mocks": {"weatherkit": own}, **keep_going},
            {"name": "radar", "run": "python app.py --radar", "mocks": ["weatherkit"], **keep_going}]}}})
    r = cli(root, "run", "weather", "--wait", "180", env=installed)
    assert r.returncode == 0, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    rep = json.loads((run / "report.json").read_text())
    assert [s["status"] for s in rep["steps"]] == ["passed", "passed"]
    checks = {(s["name"], c["check"]): c for s in rep["steps"] for c in s["expectations"]}
    assert checks[("forecast", "mock weatherkit refused 0")]["ok"], "every step that lists a mock refuses no call"
    step_dir = run / "steps" / "01-forecast"
    logged = [json.loads(line) for line in (step_dir / "mock_calls.jsonl").read_text().splitlines()]
    assert [(r["sdk"], r["endpoint"], r["arguments"], r["error"]) for r in logged] == [
        ("weatherkit", "forecast", {"city": "Paris", "days": 2}, None),
        ("weatherkit", "forecast", {"city": "London", "days": 2}, None)], "the log is the step's evidence"
    # `mock_calls:` rules on the plug-in, as the step's expectations count them (testbench.yaml takes them once
    # config._expect asks mocks.call_keys for a mock's rule keys)
    seen = {"step_dir": str(step_dir), "mocks": {"weatherkit": own}}
    rules = X.check({"mock_calls": {"weatherkit": {"calls": 2, "refused": 0}}}, workdir=root, result=seen)
    assert [c["ok"] for c in rules] == [True, True]
    wrong = X.check({"mock_calls": {"weatherkit": {"calls": "<= 1"}}}, workdir=root, result=seen)
    assert not wrong[0]["ok"] and wrong[0]["detail"].startswith("2 (wanted <= 1.0); forecast")

    r = cli(root, "run", "strays", "--wait", "180", env=installed)
    assert r.returncode == 1, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    rep = json.loads((run / "report.json").read_text())
    assert {s["name"]: s["status"] for s in rep["steps"]} == {"unknown city": "failed", "radar": "failed"}
    checks = {(s["name"], c["check"]): c for s in rep["steps"] for c in s["expectations"]}
    city = checks[("unknown city", "mock weatherkit refused 0")]
    assert not city["ok"] and ("call 2: UnexpectedCall: mock weatherkit: forecast city='Oslo' not in ('Paris', "
                               "'London')") in city["detail"]
    radar = checks[("radar", "mock weatherkit refused 0")]
    assert not radar["ok"] and "call 1: NotModelled: mock weatherkit: Client.radar is not modelled; the mock answers " \
                               "forecast" in radar["detail"]

    r = cli(root, "run", "weather", "--wait", "60")          # not installed here: the step says what to do
    assert r.returncode != 0 and "names 'weatherkit'; testbench has mock clients for anthropic" in r.stdout + r.stderr
    write_config(root, {"project": "weather", "plans": {"misspelt": {"steps": [
        {"name": "s", "run": "python app.py Paris", "mocks": {"weatherkit": {"forecast": "x"}}}]}}})
    r = cli(root, "run", "misspelt", "--wait", "60", env=installed)
    assert r.returncode != 0 and "`plans.misspelt.steps[0].mocks.weatherkit` has no 'forecast' (did you mean " \
                                 "'forecasts'?)" in r.stdout + r.stderr


def test_the_example_package_is_a_complete_mock_plug_in(weatherkit):
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["entry-points"]["agent_testbench.mocks"] == {"weatherkit": "testbench_mock_example:mock"}
    assert P.check("mocks", weatherkit) == []
    code = [EXAMPLE / "testbench_mock_example" / "__init__.py",
            EXAMPLE / "testbench_mock_example" / "sdk" / "weatherkit" / "__init__.py"]
    assert sum(len(p.read_text().splitlines()) for p in code) <= 90, "an example: small enough to read in a minute"
    assert (EXAMPLE / "README.md").is_file()
