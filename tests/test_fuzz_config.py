"""testbench.yaml under fire: the real example configs and recipes mutated a value, a key or a line at a time, any YAML
document, and the plan shorthand forms. Loading one never raises anything but a ConfigError that names a field or
the file, and a config that loads always has finite budgets, session limits and step minutes, and a worst case that
is priced or refused. TESTBENCH_FUZZ_EXAMPLES=N runs N examples of each (tests/trust_helpers.py)."""
import copy
import math
import re
import tempfile
from pathlib import Path

import pytest
import yaml

pytest.importorskip("hypothesis")
from hypothesis import assume, given, strategies as st  # noqa: E402

from agent_testbench import budget, config as C  # noqa: E402
from trust_helpers import REPO, fuzz  # noqa: E402

EXAMPLES = sorted((REPO / "examples").glob("*/testbench.yaml"))
RECIPES = sorted((REPO / "src" / "agent_testbench" / "recipes").glob("*.yaml"))
SOURCES = [(p, p.read_text()) for p in EXAMPLES + RECIPES]
ODD = [None, True, False, 0, -1, 0.0, -0.0, 1e308, 10 ** 400, math.nan, math.inf, -math.inf, "", " ", "nan", ".inf",
       "--approve", "-1", "x" * 300, "\x00", "‮", "😀", "a\nb", [], {}, [None], {"": None}, {1: 2}, [[[]]],
       "2024-01-01", "${HOME}", "../../etc/passwd", "/", "~"]
odd = st.sampled_from(ODD)
values = st.recursive(st.one_of(odd, st.integers(-5, 10 ** 6), st.floats(), st.text(max_size=6)),
                      lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=6), inner, max_size=3),
                      max_leaves=5)


def paths(node, at=()):
    yield at
    items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
    for k, v in items:
        yield from paths(v, at + (k,))


def put(raw, path, value, how):
    for k in path[:-1]:
        raw = raw[k]
    if how == "delete" and isinstance(raw, dict):
        del raw[path[-1]]
    elif how == "rename" and isinstance(raw, dict):
        raw[f"{path[-1]}x"] = raw.pop(path[-1])
    else:
        raw[path[-1]] = value


def holds(cfg: dict) -> None:
    """What a config that loaded always is: limits that are numbers, and plans that are priced or refused."""
    for key in budget.LIMIT_KEYS:
        assert C.number(cfg["budget"][key]), (key, cfg["budget"][key])
    for key in ("idle_minutes", "max_minutes"):
        assert C.number(cfg["sessions"][key], positive=True), (key, cfg["sessions"][key])
    for name, plan in cfg["plans"].items():
        assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", name)
        names = [s["name"] for s in plan["steps"]]
        assert plan["steps"] and len(set(names)) == len(names), names
        for s in plan["steps"]:
            assert C.number(s["max_minutes"], positive=True) and s["kind"] in C.STEP_KINDS, s
        try:
            ok, msg, est = budget.check_launch(cfg, name, approve=True)
        except C.ConfigError as exc:                 # dataseal.DataError: a plan whose data: file is not there (a
            assert "data." in str(exc), exc          # walk-forward is priced from its windows) - refused at launch
            says_where(exc)
            continue
        assert ok or msg.startswith(("Refused:", "Needs approval:")), msg
        assert not ok or (C.number(est["worst_case_usd"]) and est["worst_case_usd"] <= cfg["budget"]["max_run_usd"])


def says_where(exc: C.ConfigError) -> None:
    text = str(exc)
    assert text and ("`" in text or "testbench.yaml" in text or "line " in text), text
    assert "Traceback" not in text


@fuzz(300)
@given(st.sampled_from(SOURCES), st.data())
def test_a_real_config_with_values_changed_loads_or_says_which_field(machine_limits, source, data):
    path, text = source
    raw = yaml.safe_load(text)
    for _ in range(data.draw(st.integers(1, 3))):
        if not [p for p in paths(raw) if p]:
            break
        where = data.draw(st.sampled_from([p for p in paths(raw) if p]))
        try:                                              # a copy: ODD's lists and dicts are shared between draws, and
            put(raw, where, copy.deepcopy(data.draw(values)),     # a later change inside one would make a cycle
                data.draw(st.sampled_from(["set", "set", "delete", "rename"])))
        except (KeyError, IndexError, TypeError):
            pass                                          # an earlier change took the parent away
    try:
        cfg = C.validate(copy.deepcopy(raw), path.parent)
    except C.ConfigError as exc:
        says_where(exc)
        return
    holds(cfg)


LINES = ["budget:", "  max_run_usd: .nan", "  max_run_usd: 1e999", "  ask_above_usd: -1", "  max_day_usd: 9" + "9" * 400,
         "plans:", "  x: pytest -q", "  x:", "    steps:", "      - true", "      - {name: a, run: b, max_minutes: .inf}",
         "<<: *a", "a: &a {b: 1}", "!!python/object:os.system x", "- ", "? [a]", "\t", "{", "'", '"', "%YAML 1.1",
         "---", "...", "&a [*a]", "plans: &p {x: *p}", "sessions: {max_minutes: 0}", "pricing: {modal_gpu_per_s: {T4: -1}}"]


@fuzz(300)
@given(st.sampled_from(SOURCES), st.lists(st.tuples(st.integers(0, 200), st.sampled_from(LINES + ["DUP", "DROP"])),
                                          min_size=1, max_size=3))
def test_a_real_config_with_lines_changed_loads_or_says_what_is_wrong(machine_limits, source, edits):
    _, text = source
    lines = text.splitlines()
    for at, what in edits:
        at = at % (len(lines) + 1)
        if what == "DUP" and at < len(lines):
            lines.insert(at, lines[at])                  # a key given twice
        elif what == "DROP" and at < len(lines):
            del lines[at]
        elif what not in ("DUP", "DROP"):
            lines.insert(at, what)
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "testbench.yaml").write_text("\n".join(lines) + "\n")
        try:
            cfg = C.load(d)
        except C.ConfigError as exc:
            says_where(exc)
            return
        holds(cfg)


documents = st.recursive(st.one_of(odd, st.text(max_size=5), st.integers(), st.floats()),
                         lambda inner: st.lists(inner, max_size=3) | st.dictionaries(
                             st.sampled_from(["project", "plans", "budget", "sessions", "pricing", "guard", "exclude",
                                              "steps", "run", "name", "max_minutes", "backend", "gpu", "docker", "ssh",
                                              "x", "max_run_usd", "ask_above_usd", "max_day_usd"]), inner, max_size=4),
                         max_leaves=12)


@fuzz(400)
@given(documents)
def test_any_document_loads_or_says_which_field(machine_limits, doc):
    with tempfile.TemporaryDirectory() as d:
        try:
            text = yaml.safe_dump(doc, allow_unicode=True)
        except (yaml.YAMLError, ValueError):
            assume(False)
        (Path(d) / "testbench.yaml").write_text(text, errors="surrogatepass")
        try:
            cfg = C.load(d)
        except C.ConfigError as exc:
            says_where(exc)
            return
        holds(cfg)


# ---- the plan shorthand forms ----------------------------------------------------------------------------------

PROGRAMS = ["pytest -q", "python -m pytest", "python train.py --epochs 2", "uv run pytest", "npx vitest run",
            "cd web && npm test", "FOO=1 make check", "./check.sh", "go test ./...", "cargo test", "bash -c 'x'", "",
            "   ", "-x", "--approve", "&&", "'unclosed", "python -m", "😀 run", "a" * 80, "nohup time nice pytest"]
commands = st.one_of(st.sampled_from(PROGRAMS), st.text(max_size=20))
steps = st.one_of(commands, st.fixed_dictionaries({"name": st.one_of(st.sampled_from(["pytest", "a", "", None]),
                                                                     st.text(max_size=5)), "run": commands}))
plans_ = st.one_of(commands, st.lists(steps, max_size=4),
                   st.fixed_dictionaries({"steps": st.lists(steps, max_size=4)},
                                         optional={"backend": st.sampled_from(["local", "modal", "docker", "x"])}),
                   values)


@fuzz(400)
@given(plans_, st.sampled_from(["tests", "x", "checks", "a-b"]))
def test_every_shorthand_is_the_long_form_or_says_which_form(machine_limits, plan, name):
    long = C.long_form(name, copy.deepcopy(plan))           # never raises: the guard and recipes use it on any file
    if isinstance(plan, (str, list)):
        assert long["backend"] == "local", "a short form runs on this machine"
    with tempfile.TemporaryDirectory() as d:
        try:
            cfg = C.validate({"project": "p", "plans": {name: plan}}, Path(d))
        except C.ConfigError as exc:
            says_where(exc)
            return
    holds(cfg)
    got = cfg["plans"][name]
    if isinstance(plan, str):
        assert [s["name"] for s in got["steps"]] == [name] and got["steps"][0]["run"] == plan
    for s, written in zip(got["steps"], plan if isinstance(plan, list) else plan.get("steps", [])
                          if isinstance(plan, dict) else []):
        if isinstance(written, str):
            assert s["run"] == written and re.fullmatch(r"[A-Za-z0-9_-]{1,40}(-\d+)?", s["name"]), s


@fuzz(400)
@given(commands)
def test_a_step_is_named_after_its_program(command):
    name = C.step_name(command)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) and not name.startswith(("-", "_")), name
    words = [w for w in re.split(r"[ \t\n]+", command) if w]          # the shell splits on these only
    if re.fullmatch(r"[a-z][a-z0-9]{0,10}", words[0] if words else "") and words[0] not in (
            C.LAUNCHERS + C.RUNS_WITH + C.SETUP_WORDS) and not re.fullmatch(r"python[\d.]*|py", words[0]):
        assert name == words[0], (command, name)
