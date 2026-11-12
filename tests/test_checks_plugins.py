"""Every `expect:` check is a plug-in of kind "checks" (expect.py's docstring, docs/plugins.md#checks), the built-ins
included: each built-in keeps the contract a plug-in is held to; a plug-in installed the way pip installs one is
validated when testbench.yaml loads, runs through the normal run path and lands in the report next to the built-ins;
an unknown name lists the plug-ins too; and a broken plug-in only breaks what uses it."""
import json
import os
import re
import tomllib
from pathlib import Path

import jsonschema
import pytest

from agent_testbench import config as C, expect as X, plugins as P
from conftest import EXAMPLES, cli, write_config

EXAMPLE = EXAMPLES / "plugins" / "testbench-check-example"
WHERE = "plans.p.steps[0].expect"


class EP:
    def __init__(self, name, value, load):
        self.name, self.value, self._load = name, value, load

    def load(self):
        return self._load()


@pytest.fixture
def checks(monkeypatch):
    """The checks registry with made-up installed entry points (append to the list), forgotten afterwards."""
    eps, real = [], P.entry_points
    monkeypatch.setattr(P, "entry_points", lambda kind: eps if kind == "checks" else real(kind))
    P.reset("checks")
    yield eps
    P.reset("checks")


@pytest.fixture
def example(monkeypatch):
    """The example plug-in's module, imported from its folder."""
    monkeypatch.syspath_prepend(str(EXAMPLE))
    import testbench_check_example
    return testbench_check_example


class Words(X.Check):
    """A check that keeps the contract; the ones below each get one thing wrong."""
    name, example = "max_words", 3

    def validate(self, where, value):
        if not isinstance(value, int) or isinstance(value, bool):
            raise C.ConfigError(f"`{where}` is the most words the step may print, a whole number such as 3")

    def check(self, value, *, workdir, result, add):
        ok, detail = X.compare(len(result.get("stdout", "").split()), f"<= {value}")
        add(f"at most {value} words", ok, detail)


def load(root: Path, expect: dict, *more):
    plans = {"p": {"steps": [{"name": "a", "run": "true", "expect": expect}]}}
    for i, e in enumerate(more):
        plans[f"p{i + 2}"] = {"steps": [{"name": "a", "run": "true", "expect": e}]}
    write_config(root, {"project": "p", "plans": plans})
    return C.load(root)


# ---- the built-ins are plug-ins ------------------------------------------------------------------------------------

def test_every_built_in_is_a_registered_check_that_keeps_the_contract(checks):
    assert list(X.KIND.builtin) == list(X.KINDS) == P.names("checks")
    assert set(X.RUN_ORDER) == set(X.KINDS)
    here = os.getcwd()
    for name in X.KINDS:
        chk = P.get("checks", name)
        assert P.builtin("checks", name) and chk.name == name and isinstance(chk, X.Check)
        assert P.check("checks", chk) == [], name
    assert os.getcwd() == here, "the contract puts the working folder back"


def test_the_contract_says_what_a_check_gets_wrong(checks):
    def problems(**changes) -> str:
        return "\n".join(P.check("checks", type("Bad", (Words,), changes)()))

    def leaks(self, value, *, workdir, result, add):
        Path("left-behind.txt").write_text("x")                    # relative: lands in the working folder
        add("x", True, "y")

    def raises(self, value, *, workdir, result, add):
        raise FileNotFoundError(workdir / "out.txt")

    def shrugs(self, where, value):
        raise ValueError("no")

    assert P.check("checks", Words()) == []
    assert "'on' is not a plain YAML key" in problems(name="on")
    assert "'two words' is not a plain YAML key" in problems(name="two words")
    assert "'json' is a built-in check's name" in problems(name="json")
    assert "it has no `example`" in problems(example=None)
    assert "validate() refuses its own example 'x'" in problems(example="x")
    assert "validate() accepted a list" in problems(validate=lambda self, where, value: None)
    assert "validate() raised ValueError for nothing (null)" in problems(validate=shrugs)
    assert "does not name the field" in problems(validate=lambda self, where, value: (_ for _ in ()).throw(
        C.ConfigError("wrong")))
    assert "check() raised FileNotFoundError" in problems(check=raises)
    assert "check() wrote cwd/left-behind.txt outside the step's folder" in problems(check=leaks)
    assert "recorded no finding" in problems(check=lambda self, value, **kw: None)
    assert "call add(name, ok, detail)" in problems(check=lambda self, value, *, add, **kw: add(1, True))
    assert "not valid JSON Schema" in problems(schema={"type": "nope"})
    assert "its own example does not match its schema" in problems(schema={"type": "string"})
    assert problems(schema={"$ref": "#/$defs/rule"}) == "", "testbench.schema.json's own $defs are there to use"
    assert P.check("checks", object())[0].startswith("it cannot be made into a check: TypeError")


# ---- a plug-in, installed the way pip installs one, end to end --------------------------------------------------------

def installed(tmp_path: Path) -> dict:
    """The example plug-in installed as pip would: its module, plus a dist-info whose entry_points.txt says what its
    pyproject.toml says. On PYTHONPATH, both the CLI and the driver it starts find it by its entry point."""
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text())["project"]
    dist = tmp_path / "site" / "testbench_check_example-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {project['name']}\nVersion: {project['version']}\n")
    (dist / "entry_points.txt").write_text("[agent_testbench.checks]\n" + "".join(
        f"{k} = {v}\n" for k, v in project["entry-points"]["agent_testbench.checks"].items()))
    paths = [str(tmp_path / "site"), str(EXAMPLE), os.environ.get("PYTHONPATH")]
    return {"PYTHONPATH": os.pathsep.join(p for p in paths if p)}


def test_the_example_plugin_runs_through_a_plan_and_its_findings_land_in_the_report(tmp_path):
    env = installed(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "talk.py").write_text("import sys\nfor i in range(int(sys.argv[1])):\n    print('line', i)\n"
                                  "open('talk.log', 'w').write('one\\ntwo\\n')\n")
    write_config(root, {"project": "proj", "plans": {
        "quiet": {"steps": [{"name": "quiet", "run": "python talk.py 3", "max_minutes": 1,
                             "expect": {"max_log_lines": 5, "text": {"stdout": ["line 2"]}}}]},
        "chatty": {"steps": [{"name": "chatty", "run": "python talk.py 12", "max_minutes": 1,
                              "expect": {"max_log_lines": 5, "exit_code": 0}, "continue_on_failure": True},
                             {"name": "its log", "run": "python talk.py 0", "max_minutes": 1,
                              "expect": {"max_log_lines": {"lines": 1, "file": "talk.log"}}}]}}})
    r = cli(root, "run", "quiet", "--wait", "120", env=env)
    assert r.returncode == 0 and "# PASS - plan `quiet`" in r.stdout, r.stdout + r.stderr
    r = cli(root, "run", "chatty", "--wait", "120", env=env)
    assert r.returncode == 1 and "# FAIL - plan `chatty`" in r.stdout, r.stdout + r.stderr
    assert "expected log at most 5 lines: 12 (wanted <= 5)" in r.stdout, r.stdout
    run = sorted((root / ".testbench" / "runs").iterdir(), key=lambda p: p.stat().st_mtime)[-1]
    steps = json.loads((run / "report.json").read_text())["steps"]
    found = [[(c["check"], c["ok"], c["detail"]) for c in s["expectations"]] for s in steps]
    assert found[0] == [("exit code 0", True, "exited 0"), ("log at most 5 lines", False, "12 (wanted <= 5)")], \
        "the built-ins first, then the plug-in's, in the report's own format"
    assert found[1][1] == ("talk.log at most 1 lines", False, "2 (wanted <= 1)")

    schema = json.loads(cli(root, "schema", env=env).stdout)
    assert schema["$defs"]["expect"]["properties"]["max_log_lines"]["anyOf"][0] == {"type": "integer", "minimum": 0}
    bad = {"plans": {"p": {"steps": [{"name": "a", "run": "x", "expect": {"max_log_lines": "lots"}}]}}}
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(bad)), "with the plug-in's schema, editors see it"

    r = cli(root, "plans")                                       # the same project where the plug-in is not installed
    assert r.returncode == 2 and "has no 'max_log_lines' check" in r.stderr, r.stdout + r.stderr
    assert "a check from a plug-in package works once that package is installed here" in r.stderr


def test_the_example_plugin_keeps_the_contract_and_refuses_bad_values_at_load(tmp_path, checks, example):
    checks.append(EP("max_log_lines", "testbench_check_example:MaxLogLines", lambda: example.MaxLogLines))
    assert P.check("checks", P.get("checks", "max_log_lines")) == []
    assert load(tmp_path, {"max_log_lines": {"lines": 9, "file": "x.log"}})
    for value, message in ((-1, "is the most lines the step may print"), ({"lines": 2, "fiel": "x"}, "e.g. 200"),
                           ({"lines": 2, "file": ""}, "max_log_lines.file` is the log's path")):
        with pytest.raises(C.ConfigError, match=re.escape(message)) as info:
            load(tmp_path, {"max_log_lines": value})
        assert f"{WHERE}.max_log_lines" in str(info.value)


# ---- names, order, defaults ------------------------------------------------------------------------------------------

def test_an_unknown_check_lists_the_plugins_too_with_the_near_miss(tmp_path, checks, example):
    P.register("checks", "max_log_lines", example.MaxLogLines())
    names = ", ".join([*X.KINDS, "max_log_lines"])
    with pytest.raises(C.ConfigError, match=re.escape(f"has no 'max_log_line' check; expectations are {names} (did "
                                                      "you mean max_log_lines?)")):
        load(tmp_path, {"max_log_line": 5})
    with pytest.raises(C.ConfigError, match=re.escape("has no 'zzz' check; expectations are exit_code")) as info:
        load(tmp_path, {"zzz": 5})
    assert "max_log_lines; a check from a plug-in package works once that package is installed here" in str(info.value)


def test_plugin_findings_follow_the_built_ins_and_one_with_a_default_checks_every_step(tmp_path, checks):
    class Always(Words):
        name, default = "max_words_everywhere", 2

    P.register("checks", "max_words", Words())
    P.register("checks", "max_words_everywhere", Always())
    load(tmp_path, {"max_words": 4, "exit_code": 0})
    got = X.check({"max_words": 4, "exit_code": 0, "files": ["nope"]}, workdir=tmp_path,
                  result={"kind": "run", "exit_code": 0, "stdout": "one two three"})
    assert [(c["check"], c["ok"]) for c in got] == [("exit code 0", True), ("file nope", False),
                                                    ("at most 4 words", True), ("at most 2 words", False)]
    assert [c["check"] for c in X.check({}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})] == \
        ["exit code 0", "at most 2 words"], "a default applies to steps that do not name it"


# ---- a broken plug-in --------------------------------------------------------------------------------------------------

def test_a_broken_check_plugin_is_reported_and_breaks_only_what_uses_it(tmp_path, checks):
    def boom():
        raise ImportError("no module named shaky_checks")

    class Crashes(Words):
        name = "crashes"

        def check(self, value, *, workdir, result, add):
            add(name="before the crash", ok=True, detail="fine")      # by keyword, as a built-in may be called
            raise KeyError("words")

    class Fragile(Words):
        name = "fragile"

        def validate(self, where, value):
            return value["lines"]                                # a bug: TypeError for a number

    checks += [EP("shaky", "shaky_checks:Shaky", boom), EP("json", "evil:Json", lambda: Words()),
               EP("crashes", "x:Crashes", lambda: Crashes), EP("fragile", "x:Fragile", lambda: Fragile)]
    cfg = load(tmp_path, {"json": {"m.json": {"acc": ">= 0.9"}}, "exit_code": 0})
    assert cfg["plans"]["p"]["steps"][0]["expect"]["exit_code"] == 0, "a project that does not use it is unaffected"
    assert isinstance(P.get("checks", "json"), X.JsonCheck), "a plug-in never replaces a built-in"
    broken = P.broken("checks")
    assert "shaky_checks:Shaky did not load: ImportError: no module named shaky_checks" in broken["shaky"]
    assert "tries to replace a check that already exists" in broken["json"]

    with pytest.raises(C.ConfigError, match=re.escape("`plans.p2.steps[0].expect.shaky`: the check 'shaky' is "
                                                      "installed but did not load (shaky_checks:Shaky did not load")):
        load(tmp_path, {"exit_code": 0}, {"shaky": 1})
    with pytest.raises(C.ConfigError, match=re.escape(f"`{WHERE}.fragile`: the check plug-in 'fragile' failed while "
                                                      "reading this value (TypeError")):
        load(tmp_path, {"fragile": 3})

    got = X.check({"crashes": 1, "exit_code": 0}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})
    assert [(c["check"], c["ok"]) for c in got] == [("exit code 0", True), ("before the crash", True),
                                                    ("crashes", False)]
    assert "the check plug-in 'crashes' failed: KeyError: 'words'. That is a bug in the plug-in" in got[2]["detail"]

    got = X.check({"shaky": 1, "gone": 2}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})
    assert [(c["check"], c["ok"]) for c in got] == [("exit code 0", True), ("shaky", False), ("gone", False)]
    assert "installed where this step was checked but did not load" in got[1]["detail"]
    assert "no check 'gone' is installed where this step was checked, so it was not checked" in got[2]["detail"], \
        "a check that cannot run here fails its step; it is never skipped in silence"


# ---- the JSON Schema ---------------------------------------------------------------------------------------------------

def test_the_schema_allows_plugin_checks_and_testbench_schema_adds_their_own(checks, example):
    schema = json.loads(C.SCHEMA.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    step = lambda expect: {"plans": {"p": {"steps": [{"name": "a", "run": "x", "expect": expect}]}}}
    assert not list(validator.iter_errors(step({"max_log_lines": 200, "exit_code": 0})))
    assert list(validator.iter_errors(step({"max log lines": 200}))), "not a plain name: no check can have it"
    assert list(validator.iter_errors(step({"exit_code": "0"}))), "the built-ins keep their own definitions"
    assert X.schema_text() == C.SCHEMA.read_text(encoding="utf-8"), "no plug-in with a schema: the shipped file"
    P.register("checks", "max_log_lines", example.MaxLogLines())
    P.register("checks", "max_words", Words())                   # no schema of its own: allowed as any value
    merged = json.loads(X.schema_text())
    assert merged["$defs"]["expect"]["properties"]["max_log_lines"] == example.MaxLogLines.schema
    assert "max_words" not in merged["$defs"]["expect"]["properties"]
    jsonschema.Draft202012Validator.check_schema(merged)
    assert list(jsonschema.Draft202012Validator(merged).iter_errors(step({"max_log_lines": -1})))
