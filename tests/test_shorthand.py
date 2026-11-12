"""Short testbench.yaml: `tests: "pytest -q"`, a list of commands, steps written as plain commands and `project:` left
out all load to the very same plans as the long form, and every reader of the file (the guard hook, recipes, init
--add) understands them."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_testbench import config as C, recipes as RC
from conftest import REPO, cli

LONG = """\
project: p
plans:
  tests:
    backend: local
    steps:
      - name: tests
        run: python -c "print('ok')"
"""
SHORT = """\
project: p
plans:
  tests: python -c "print('ok')"
"""


def load(root: Path, text: str) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    (root / "testbench.yaml").write_text(text)
    return C.load(root)


def test_one_command_is_the_long_form_with_one_step_named_after_the_plan(tmp_path):
    short, long = load(tmp_path / "a", SHORT)["plans"], load(tmp_path / "b", LONG)["plans"]
    assert short == long and json.dumps(short) == json.dumps(long), "the same dicts, keys in the same order"
    step = short["tests"]["steps"][0]
    assert short["tests"]["backend"] == "local" and short["tests"]["workdir"] == "inplace"
    assert step["name"] == "tests" and step["kind"] == "run" and step["max_minutes"] == C.STEP_DEFAULTS["max_minutes"]


def test_a_list_of_commands_runs_in_order_named_after_their_programs(tmp_path):
    cfg = load(tmp_path, 'project: p\nplans:\n  checks: ["ruff check .", "pytest -q", "python -m pytest -x", '
                         '"uv run pytest tests/unit"]\n')
    plan = cfg["plans"]["checks"]
    assert [s["name"] for s in plan["steps"]] == ["ruff", "pytest", "pytest-2", "pytest-3"]
    assert [s["run"] for s in plan["steps"]] == ["ruff check .", "pytest -q", "python -m pytest -x",
                                                 "uv run pytest tests/unit"]
    assert plan["backend"] == "local" and all(s["max_minutes"] == 10 for s in plan["steps"])


def test_a_step_may_be_a_plain_command_in_the_long_form_too(tmp_path):
    cfg = load(tmp_path, "project: p\nplans:\n  gpu:\n    backend: modal\n    gpu: T4\n    steps:\n"
                         "      - pytest -q\n      - {name: train, run: python train.py, max_minutes: 30}\n"
                         "      - python train.py --eval\n      - {name: pytest, run: pytest -x}\n")
    steps = cfg["plans"]["gpu"]["steps"]
    assert [(s["name"], s["run"]) for s in steps] == [("pytest-2", "pytest -q"), ("train", "python train.py"),
                                                      ("train-2", "python train.py --eval"), ("pytest", "pytest -x")]
    assert cfg["plans"]["gpu"]["backend"] == "modal" and cfg["plans"]["gpu"]["workdir"] == "copy"
    assert steps[1]["max_minutes"] == 30, "a step written in full is as it was"


@pytest.mark.parametrize("command, name", [
    ("pytest -q", "pytest"), ("python -m pytest -q", "pytest"), ("uv run pytest", "pytest"), ("ruff check .", "ruff"),
    ("python train.py --epochs 1", "train"), ("python3 src/eval.py", "eval"), ("cd web && npm test", "npm"),
    ("FOO=1 pytest", "pytest"), ("npx --yes vitest run", "vitest"), ("./scripts/check.sh", "check"),
    ("go test ./...", "go"), ("echo 'unclosed", "echo"), ("", "step"),
])
def test_a_command_s_step_is_named_after_its_program(command, name):
    assert C.step_name(command) == name


def test_project_may_be_left_out_and_is_the_folder_s_name(tmp_path):
    assert load(tmp_path / "my-app", "plans:\n  tests: pytest -q\n")["project"] == "my-app"
    assert load(tmp_path / "My App (v2)", "project:\nplans: {}\n")["project"] == "My-App--v2"
    assert load(tmp_path / "_café.d", "")["project"] == "caf--d", "what a name cannot hold becomes -"
    assert load(tmp_path / "x", "project: mine\n")["project"] == "mine", "a project: written is kept"
    with pytest.raises(C.ConfigError, match="or leave it out to use the folder's name"):
        load(tmp_path / "y", "project: -bad\n")


def test_init_names_the_project_the_same_way(tmp_path):
    root = tmp_path / "My App (v2)"
    root.mkdir()
    assert cli(root, "init").returncode == 0
    assert "\nproject: My-App--v2\n" in (root / "testbench.yaml").read_text()
    assert C.load(root)["project"] == C.project_name(root) == "My-App--v2"


@pytest.mark.parametrize("text, field, forms", [
    ("plans: 5\n", "`plans:`", C.PLAN_FORMS),
    ("plans:\n  tests: 42\n", "`plans.tests` is 42", C.PLAN_FORMS),
    ("plans:\n  tests:\n", "`plans.tests` is empty", C.PLAN_FORMS),
    ("plans:\n  tests: ''\n", "`plans.tests` is '', which runs nothing", C.PLAN_FORMS),
    ("plans:\n  tests: []\n", "`plans.tests` is [], which runs nothing", C.PLAN_FORMS),
    ("plans:\n  tests: [pytest, 5]\n", "`plans.tests[1]` is 5", C.STEP_FORMS),
    ("plans:\n  tests: [pytest, '  ']\n", "`plans.tests[1]` is an empty command", C.STEP_FORMS),
    ("plans:\n  tests: {backend: local}\n", "`plans.tests.steps` must be a list of steps", C.STEP_FORMS),
    ("plans:\n  tests: {steps: pytest}\n", "`plans.tests.steps` must be a list of steps, not 'pytest'", C.STEP_FORMS),
    ("plans:\n  tests: {steps: [{run: pytest}]}\n", "`plans.tests.steps[0]` needs a `name:`", C.STEP_FORMS),
    ("plans:\n  tests: {steps: [{name: a}]}\n", "needs exactly one of `notebook:`, `run:` or `web:`", C.STEP_FORMS),
])
def test_a_wrong_shape_names_the_field_and_shows_every_form(tmp_path, text, field, forms):
    with pytest.raises(C.ConfigError) as got:
        load(tmp_path, text)
    message = str(got.value)
    assert field in message, message
    assert all(line in message for line in forms.splitlines()), "every accepted form, one line each"
    assert all(len(line) <= 120 for line in message.splitlines()[1:])


def test_a_short_plan_runs_and_writes_the_same_plan_json(tmp_path):
    runs = {}
    for name, text in (("short", SHORT), ("long", LONG)):
        root = tmp_path / name
        load(root, text)
        r = cli(root, "run", "tests", "--wait", "120")
        assert r.returncode == 0 and "PASS - plan `tests`" in r.stdout, r.stdout + r.stderr
        runs[name] = next((root / ".testbench" / "runs").glob("*/plan.json")).read_text()
    assert runs["short"] == runs["long"]
    root = tmp_path / "list"
    load(root, "plans:\n  checks:\n    - python -c \"print('first')\"\n    - python -c \"raise SystemExit(3)\"\n"
               "    - python -c \"print('never')\"\n")
    r = cli(root, "run", "checks", "--wait", "120")
    assert r.returncode == 1 and "python-2" in r.stdout and "python-3" in r.stdout, r.stdout
    rep = json.loads(next((root / ".testbench" / "runs").glob("*/report.json")).read_text())
    assert [s["status"] for s in rep["steps"]] == ["passed", "failed"] and rep["skipped"] == ["python-3"]


# ---- every reader of testbench.yaml ------------------------------------------------------------------------------

def guard(root: Path, tool: str, inp: dict) -> str | None:
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], capture_output=True, text=True, timeout=30,
                       input=json.dumps({"cwd": str(root), "tool_name": tool, "tool_input": inp}))
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None


def test_the_guard_reads_a_short_plan_as_a_local_one(tmp_path):
    yml = tmp_path / "testbench.yaml"
    boxed = ("project: p\ndocker: {network: none}\nplans:\n  tests:\n    backend: docker\n"
             "    steps: [{name: t, run: x}]\n")
    yml.write_text(boxed)
    write = lambda text: guard(tmp_path, "Write", {"file_path": str(yml), "content": text})
    out_long = boxed.replace("backend: docker", "backend: local")
    out_short = "project: p\ndocker: {network: none}\nplans:\n  tests: pytest -q\n"
    out_list = "project: p\ndocker: {network: none}\nplans:\n  tests: [pytest -q, ruff check .]\n"
    assert write(out_long) == write(out_short) == write(out_list) == "ask", "each leaves the docker sandbox"
    assert write(boxed.replace("steps: [{name: t, run: x}]", "steps: [pytest -q]")) is None, "a step's form: no matter"

    to_modal = "project: p\nplans:\n  tests:\n    backend: modal\n    gpu: T4\n    steps: [pytest -q]\n"
    decisions = []
    for local in ("project: p\nplans:\n  tests: pytest -q\n", "project: p\nplans:\n  tests: [pytest -q]\n",
                  "project: p\nplans:\n  tests:\n    backend: local\n    steps: [{name: tests, run: pytest -q}]\n"):
        yml.write_text(local)
        decisions.append(write(to_modal))
    assert decisions[0] == decisions[1] == decisions[2], "a short plan moving to modal is judged as the long form is"
    yml.write_text(out_short)
    assert write(out_short.replace("plans:", "budget: {max_run_usd: 50}\nplans:")) == "ask", "a budget raised"
    yml.write_text(out_short + "guard: {ask: [make deploy]}\n")
    assert guard(tmp_path, "Bash", {"command": "make deploy"}) == "ask", "guard.ask beside a short plan"


def test_init_add_extends_a_file_written_in_short_form(tmp_path):
    yml = tmp_path / "testbench.yaml"
    before = "project: mine\nplans:\n  tests: python -c \"pass\"\n  checks:\n    - ruff check .\n    - pytest -q\n"
    yml.write_text(before)
    r = cli(tmp_path, "init", "--add", "--recipe", "data-pipeline")
    assert r.returncode == 0, r.stdout + r.stderr
    after = yml.read_text()
    assert after.startswith(before.rstrip("\n")), "nothing already there changes"
    assert list(C.load(tmp_path)["plans"]) == ["tests", "checks", "pipeline-sample", "pipeline-full"]
    assert "\n  pipeline-sample:\n    backend: local\n" in after, "at the file's own indentation"
    r = cli(tmp_path, "init", "--add", "--recipe", "pytest")
    assert r.returncode == 2 and "already has one, so nothing was written" in r.stdout and yml.read_text() == after


def test_recipes_read_short_plans(tmp_path):
    assert RC.subjects({"tests": "pytest -q", "eval": ["python eval.py", {"name": "nb", "notebook": "a.ipynb"}],
                        "bad": 5}) == {("pytest", ""), ("script", "eval.py"), ("notebook", "a.ipynb")}
    cfg = RC.check("project: p\nplans:\n  tests: pytest -q\n  more: [ruff check ., {name: nb, run: x}]\n", tmp_path)
    assert [s["name"] for s in cfg["plans"]["more"]["steps"]] == ["ruff", "nb"]
    with pytest.raises(RC.RecipeError, match="runs the notebook missing.ipynb"):
        RC.check("plans:\n  nb: [{name: nb, notebook: missing.ipynb}]\n", tmp_path)
    with pytest.raises(RC.RecipeError, match="`plans.x` is 5"):
        RC.check("plans:\n  x: 5\n", tmp_path)


def test_long_form_never_raises_on_what_does_not_load():
    for plan in (None, 5, "", [], [5, None], {"steps": 5}, {"steps": [None, "x"]}, {"backend": "local"}):
        C.long_form("p", plan)
    assert C.long_form("p", {"steps": "x"}) == {"steps": "x"}, "left as it is, for validation to say what is wrong"


def test_the_forms_an_error_shows_are_forms_that_load(tmp_path):
    (tmp_path / "notebooks").mkdir()
    (tmp_path / "notebooks" / "analysis.ipynb").write_text('{"cells": [], "metadata": {}, "nbformat": 4, '
                                                           '"nbformat_minor": 5}')
    for line in C.PLAN_FORMS.splitlines():
        plan = line.strip().split("  ")[0]
        assert load(tmp_path, f"plans:\n  {plan}\n")["plans"], line
    for line in C.STEP_FORMS.splitlines():
        step = line.strip().split("  ")[0]
        assert load(tmp_path, f"plans:\n  p:\n    steps:\n    {step}\n")["plans"]["p"]["steps"], line
