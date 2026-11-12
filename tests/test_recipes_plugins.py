"""Recipes as plug-ins: the 14 built-in recipes are the registry's built-ins and keep the recipe contract; a package
adds one by an entry point (its package folder's NAME.yaml, a Path, YAML text, a mapping or a Recipe), and `testbench
recipes`, `init --recipe` and `init --add --recipe` use it like a built-in, naming its package. A recipe that sets
budget:, pricing: or guard: - however it hides it - never loads, and the contract says what else is wrong in plain
words."""
import os
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from agent_testbench import config as C, plugins as P, recipes as RC
from conftest import REPO, cli

EXAMPLE = REPO / "examples" / "plugins" / "testbench-recipe-example"
GOOD = """\
# Recipe {name}: a tool answers --help.
#
# Proves: the tool installs and starts.
# FILL IN: the tool's command.
# Costs: nothing; it runs on this machine.

plans:
  {plan}:
    backend: local
    description: the tool starts
    steps:
      - name: help
        run: mytool --help                    # FILL IN: your tool's command
        max_minutes: 2
        expect: {{text: {{stdout: ["usage:"]}}}}
"""


def good(name="tool-help", plan="tool-help") -> str:
    return GOOD.format(name=name, plan=plan)


def install(site: Path, dist: str, entries: dict[str, str], group="agent_testbench.recipes") -> Path:
    """A package as pip leaves it: its files in a folder on the path, and a .dist-info with its entry points."""
    info = site / f"{dist.replace('-', '_')}-0.1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text(f"[{group}]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items()))
    return site


def example_site(tmp_path: Path) -> Path:
    """examples/plugins/testbench-recipe-example installed, plus a package whose recipe sets budget: and one that
    tries to take the name of a built-in recipe."""
    site = tmp_path / "site"
    shutil.copytree(EXAMPLE / "testbench_recipe_example", site / "testbench_recipe_example")
    install(site, "testbench-recipe-example", {"cli-smoke": "testbench_recipe_example"})
    (site / "tb_sneaky_recipe").mkdir()
    (site / "tb_sneaky_recipe" / "__init__.py").write_text("")
    (site / "tb_sneaky_recipe" / "sneaky.yaml").write_text(good("sneaky", "sneaky") + "budget:\n  max_run_usd: 500\n")
    (site / "tb_sneaky_recipe" / "pytest.yaml").write_text(good("pytest", "mine"))
    install(site, "tb-sneaky", {"sneaky": "tb_sneaky_recipe", "pytest": "tb_sneaky_recipe"})
    return site


def with_site(site: Path) -> dict:
    return {"PYTHONPATH": os.pathsep.join(p for p in (str(site), os.environ.get("PYTHONPATH", "")) if p)}


@pytest.fixture
def fresh_registry():
    P.reset("recipes")
    yield
    P.reset("recipes")


@pytest.fixture
def installed(tmp_path, monkeypatch, fresh_registry):
    """The example site on sys.path of this process, found by importlib.metadata as a real install is."""
    site = example_site(tmp_path)
    monkeypatch.syspath_prepend(str(site))
    yield site
    for mod in ("testbench_recipe_example", "tb_sneaky_recipe"):
        sys.modules.pop(mod, None)


# ---- the built-ins -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(p.stem for p in RC.HERE.glob("*.yaml")))
def test_every_built_in_recipe_is_a_registry_built_in_that_keeps_the_contract(name):
    assert P.builtin("recipes", name) and name in P.names("recipes")
    r = RC.get(name)
    assert isinstance(r, RC.Recipe) and r.path == RC.HERE / f"{name}.yaml" and r.origin == "agent-testbench"
    assert r.text == r.path.read_text(encoding="utf-8") and r.summary == RC.load(name)["summary"]
    assert P.check("recipes", r) == [], f"recipe {name} breaks the recipe contract"


def test_a_package_folder_path_text_mapping_or_recipe_is_a_recipe(tmp_path, fresh_registry):
    (tmp_path / "x.yaml").write_text(good())
    forms = {"a path": tmp_path / "x.yaml", "a path as text": str(tmp_path / "x.yaml"), "YAML text": good(),
             "a Recipe": RC.Recipe("", good()), "a package": RC}
    for what, obj in forms.items():
        name = "pytest" if obj is RC else "tool-help"
        r = RC._prepare(obj, name)
        assert isinstance(r, RC.Recipe) and r.name == name, what
        assert P.check("recipes", r, name) == [], what
    assert RC._prepare(tmp_path / "x.yaml", "tool-help").path == tmp_path / "x.yaml"
    with pytest.raises(RC.RecipeError, match="has no nope.yaml in .*named like the entry point"):
        RC._prepare(RC, "nope")
    with pytest.raises(RC.RecipeError, match="is not a file; a recipe given as a path is a YAML file"):
        RC._prepare(tmp_path / "missing.yaml", "tool-help")
    with pytest.raises(RC.RecipeError, match="a recipe is a YAML file .*; got int"):
        RC._prepare(42, "x")
    with pytest.raises(RC.RecipeError, match="lowercase letters, digits and -.*'Tool_Help'"):
        RC._prepare(good(), "Tool_Help")


def test_a_mapping_is_written_as_yaml_with_its_header_and_fill_in_marks():
    mapping = {                                      # the one docs/plugins-recipes.md shows
        "summary": "a command-line tool answers --help",
        "proves": "the tool installs and starts",
        "fill_in": "the tool's command",
        "costs": "nothing; it runs on this machine",
        "fill_in_at": {"plans.cli.steps.0.run": "your tool's command"},
        "plans": {"cli": {"backend": "local", "description": "the tool starts", "steps": [
            {"name": "help", "run": "mytool --help", "max_minutes": 2, "expect": {"text": {"stdout": ["usage:"]}}}]}},
    }
    r = RC._prepare(mapping, "cli-help")
    assert r.text.startswith("# Recipe cli-help: a command-line tool answers --help\n#\n# Proves: the tool")
    assert "      - name: help\n        run: mytool --help    # FILL IN: your tool's command\n" in r.text, r.text
    assert yaml.safe_load(r.text) == {"plans": mapping["plans"]}
    assert P.check("recipes", r) == []
    sections = {**mapping, "docker": {"image": "python:3.12-slim"}, "plans": mapping["plans"]}
    assert list(yaml.safe_load(RC.from_mapping(sections, "cli-help"))) == ["docker", "plans"], "plans: comes last"
    with pytest.raises(RC.RecipeError, match="needs `summary`: one line"):
        RC.from_mapping({"plans": mapping["plans"]}, "cli-help")
    with pytest.raises(RC.RecipeError, match="fill_in_at names plans.cli.steps.3.run, not in its sections or plans"):
        RC.from_mapping({**mapping, "fill_in_at": {"plans.cli.steps.3.run": "x"}}, "cli-help")
    with pytest.raises(RC.RecipeError, match="sets `pricing:`"):
        RC._prepare({**mapping, "pricing": {"modal_gpu_per_s": {"T4": 0}}}, "cli-help")


# ---- what a recipe may never do ----------------------------------------------------------------------------------

@pytest.mark.parametrize("text, says", [
    (good() + "budget:\n  max_run_usd: 500\n", "sets `budget:`, which stays the person's"),
    ("pricing: {modal_gpu_per_s: {H100: 0}}\nguard: {ask: []}\n" + good(),
     "sets `pricing:` and `guard:`, which stay the person's"),
    (good() + '"budget": {max_run_usd: 500}\n', "under `plans:` starts at the beginning of its line"),
    ('"budget": {max_run_usd: 500}\n' + good(), "YAML reads its top-level keys as budget, plans, but the lines"),
    ("<<: {budget: {max_run_usd: 500}}\n" + good(), "YAML reads its top-level keys as"),
    (good().replace("        max_minutes: 2", "max_minutes: 2"), "must hold sections among"),
    (good() + "  more:\n    backend: local\n- stray\n", "`- stray` under `plans:` starts at the beginning of its line"),
    ("# Recipe x: y\nplans: {}\n", "`plans:` maps plan names to plans, and a recipe has at least one"),
    ("modal: 3\n" + good(), "`modal:` is a mapping of settings"),
    (good() + "  bad: [unclosed\n", "is not valid YAML"),
])
def test_a_recipe_that_sets_the_person_s_limits_or_cannot_be_added_as_text_never_loads(text, says):
    with pytest.raises(RC.RecipeError) as refused:
        RC._prepare(text, "tool-help")
    assert says in str(refused.value), str(refused.value)
    assert any(says in p for p in P.check("recipes", RC.Recipe("tool-help", text))), "the contract says the same"


def test_the_contract_says_what_else_is_wrong(tmp_path):
    plain = good().replace("# FILL IN: your tool's command", "")
    plain = plain.replace("# Proves: the tool installs and starts.\n", "")
    problems = P.check("recipes", RC.Recipe("tool-help", plain))
    assert "its header comment does not say what it proves: add a `# Proves: ...` paragraph" in problems
    assert any(p.startswith("no line is marked `# FILL IN`") for p in problems), problems
    assert P.check("recipes", RC.Recipe("tool-help", plain.replace("# FILL IN: the tool's command.",
                                                                    "# FILL IN: nothing.")))[1:] == [], \
        "`FILL IN: nothing` is an answer"
    assert P.check("recipes", RC.Recipe("other", good()))[0].startswith("its first line is `# Recipe other: ")
    pricey = good().replace("backend: local", "backend: modal\n    gpu: H100").replace("max_minutes: 2",
                                                                                     "max_minutes: 600")
    assert any(p.startswith("plan tool-help's worst case $") and "over the default budget.max_run_usd $2.00" in p
               for p in P.check("recipes", RC.Recipe("tool-help", pricey))), "refused as it ships"
    unknown = good().replace("backend: local", "backend: rack")
    assert "its plans do not load under the header `testbench init` writes" in \
        P.check("recipes", RC.Recipe("tool-help", unknown))[0]
    outside = good().replace("run: mytool --help", "notebook: ../../elsewhere.ipynb")
    assert any("runs the notebook ../../elsewhere.ipynb, outside the project folder" in p
               for p in P.check("recipes", RC.Recipe("tool-help", outside)))
    notebook = good().replace("run: mytool --help", "notebook: nb/analysis.ipynb")
    assert P.check("recipes", RC.Recipe("tool-help", notebook)) == [], "a notebook it names stands for the project's"


# ---- installed packages, in this process -------------------------------------------------------------------------

def test_an_installed_recipe_is_found_named_and_used_like_a_built_in(installed, tmp_path):
    builtin = sorted(p.stem for p in RC.HERE.glob("*.yaml"))
    assert RC.names() == builtin + ["cli-smoke"], "built-ins first, then the installed ones"
    r = RC.get("cli-smoke")
    assert r.origin == "testbench-recipe-example" and r.path.name == "cli-smoke.yaml"
    assert P.check("recipes", r) == [], "the example keeps the recipe contract"
    assert P.get("recipes", "pytest").path == RC.HERE / "pytest.yaml", "a built-in is never replaced"
    bad = RC.broken()
    assert bad["pytest"].startswith("from tb-sneaky: tb_sneaky_recipe tries to replace a recipe that already exists")
    assert "sets `budget:`, which stays the person's" in bad["sneaky"] and bad["sneaky"].startswith("from tb-sneaky:")
    with pytest.raises(RC.RecipeError, match="recipe 'sneaky' is installed but did not load .*sets `budget:`"):
        RC.load("sneaky")
    with pytest.raises(RC.RecipeError, match="no recipe 'cli-smok' \\(did you mean cli-smoke\\?\\)"):
        RC.load("cli-smok")
    loaded = RC.load("cli-smoke", tmp_path)
    assert loaded["origin"] == "testbench-recipe-example" and list(loaded["plans"]) == ["cli-smoke"]
    text, _ = RC.compose("project: p\nplans:\n  tests: pytest -q\n", [loaded])
    assert list(RC.check(text, tmp_path)["plans"]) == ["tests", "cli-smoke"]
    listing = RC.listing()
    assert "\ncli-smoke (local) - from testbench-recipe-example\n" in listing
    assert "Recipes from installed packages name their package; the others come with agent-testbench." in listing
    assert "Installed but not loaded" in listing and "\n  sneaky from tb-sneaky: " in listing


def test_a_hand_registered_recipe_says_where_it_came_from(fresh_registry):
    P.register("recipes", "tool-help", good())
    assert RC.get("tool-help").origin == "this process (plugins.register)"
    assert "tool-help (local) - from this process (plugins.register)" in RC.listing()


# ---- installed packages, through the CLI -------------------------------------------------------------------------

def test_the_cli_lists_prints_and_writes_an_installed_recipe(tmp_path):
    env = with_site(example_site(tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    r = cli(proj, "recipes", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "\ncli-smoke (local) - from testbench-recipe-example\n  a command-line tool starts" in r.stdout
    assert "\npytest (local)\n" in r.stdout, "built-ins name no package"
    assert "\n  sneaky from tb-sneaky: " in r.stdout and "sets `budget:`" in r.stdout
    r = cli(proj, "recipes", "cli-smoke", env=env)
    assert r.returncode == 0 and r.stdout == (EXAMPLE / "testbench_recipe_example" / "cli-smoke.yaml").read_text()
    r = cli(proj, "init", "--recipe", "sneaky", env=env)
    assert r.returncode == 2 and "recipe 'sneaky' is installed but did not load" in r.stdout, r.stdout
    assert not (proj / "testbench.yaml").exists(), "nothing was written"

    r = cli(proj, "init", "--recipe", "cli-smoke", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plan cli-smoke: local, up to 7 min, free (recipe cli-smoke, from testbench-recipe-example)" in r.stdout
    text = (proj / "testbench.yaml").read_text()
    assert "# Recipe cli-smoke: " in text and "# FILL IN: your tool's command" in text
    cfg = C.load(proj)
    assert list(cfg["plans"]) == ["cli-smoke"] and cfg["budget"]["max_run_usd"] == 2.0

    other = tmp_path / "other"
    other.mkdir()
    (other / "testbench.yaml").write_text("project: other\nplans:\n  tests: python -m pytest -q\n")
    r = cli(other, "init", "--add", "--recipe", "cli-smoke", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (other / "testbench.yaml").read_text().startswith("project: other\nplans:\n  tests: python -m pytest -q\n")
    assert list(C.load(other)["plans"]) == ["tests", "cli-smoke"]


def test_an_installed_recipe_s_plan_runs_and_passes_on_a_project_made_to_it(tmp_path):
    env = with_site(example_site(tmp_path))
    proj = tmp_path / "proj"
    (proj / "mytool").mkdir(parents=True)
    (proj / "samples").mkdir()
    (proj / "samples" / "input.csv").write_text("a,b\n1,2\n")
    (proj / "mytool" / "__main__.py").write_text(
        "import argparse, pathlib, shutil\n"
        "p = argparse.ArgumentParser(prog='mytool')\np.add_argument('src')\np.add_argument('--out')\n"
        "a = p.parse_args()\npathlib.Path(a.out).parent.mkdir(parents=True, exist_ok=True)\n"
        "shutil.copy(a.src, a.out)\nprint('done')\n")
    assert cli(proj, "init", "--recipe", "cli-smoke", env=env).returncode == 0
    r = cli(proj, "run", "cli-smoke", "--wait", "240", env=env)
    assert r.returncode == 0 and "# PASS - plan `cli-smoke` on local" in r.stdout, r.stdout + r.stderr
