"""testbench init looks at a project and writes plans that already check something."""
import json

import yaml

from agent_testbench import config as C
from conftest import cli, make_notebook


def test_init_recognises_notebooks_tests_scripts_and_apps(tmp_path):
    root = tmp_path / "mixed"
    make_notebook(root / "notebooks" / "my analysis.ipynb", [
        'ROWS = 100 #@param {type:"integer"}',
        "import json, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\njson.dump({'rows': ROWS}, open('out/summary.json', 'w'))",
    ])
    (root / "tests").mkdir()
    (root / "tests" / "test_math.py").write_text("def test_add():\n    assert 1 + 1 == 2\n")
    (root / "train.py").write_text("print('trained')\n")
    (root / "package.json").write_text(json.dumps({"scripts": {"dev": "vite"}}))
    r = cli(root, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plan my-analysis-local: notebook notebooks/my analysis.ipynb (1 form field(s), writes summary.json)" in r.stdout
    assert "plan tests: a pytest suite" in r.stdout and "plan train-smoke: a training script (train.py)" in r.stdout
    assert "plan ui: a web app (npm run dev)" in r.stdout and "FILL IN" in r.stdout
    cfg = C.load(root)                                           # valid YAML, valid plans, paths with spaces quoted
    nb = cfg["plans"]["my-analysis-local"]["steps"]
    assert nb[0]["run"] == 'testbench lint "notebooks/my analysis.ipynb"' and nb[1]["notebook"] == "notebooks/my analysis.ipynb"
    assert nb[1]["expect"]["files"] == ["**/summary.json"]
    assert cfg["plans"]["ui"]["steps"][0]["web"]["serve"] == "npm run dev"
    for plan in ("my-analysis-local", "tests", "train-smoke"):
        r = cli(root, "run", plan, "--wait", "180")
        assert r.returncode == 0, f"{plan}: {r.stdout}"


def test_a_static_page_gets_a_browser_check(tmp_path):
    (tmp_path / "public").mkdir()
    (tmp_path / "public" / "index.html").write_text("<title>Hi</title><p>hello</p>")
    assert "plan ui: a static page (public/index.html)" in cli(tmp_path, "init").stdout
    step = yaml.safe_load((tmp_path / "testbench.yaml").read_text())["plans"]["ui"]["steps"][0]
    assert step["web"]["serve"].endswith("-d public") and step["expect"]["web"]["no_console_errors"] is True


def test_demo_copies_a_working_project_and_runs_it(tmp_path):
    import pytest
    from conftest import has_sklearn
    if not has_sklearn():
        pytest.skip("the demo trains a model with scikit-learn and matplotlib")
    target = tmp_path / "try-it"
    r = cli(tmp_path, "demo", str(target))
    assert r.returncode == 0 and "# PASS - plan `smoke` on local" in r.stdout and "testbench loop start" in r.stdout, r.stdout
    assert (target / ".git").is_dir() and (target / "testbench.yaml").is_file()
    assert cli(tmp_path, "demo", str(target)).returncode == 2, "never into a folder that is not empty"


def test_the_fill_in_line_works_when_uncommented(tmp_path):
    """init only sees `metrics.json` in the code, not the folder it goes to: its json line uses a one-file pattern."""
    import shutil
    import pytest
    from conftest import EXAMPLES, has_sklearn
    if not has_sklearn():
        pytest.skip("the example trains a model with scikit-learn")
    root = tmp_path / "nb"
    shutil.copytree(EXAMPLES / "tiny-model", root, ignore=shutil.ignore_patterns(".testbench", "outputs", "testbench.yaml"))
    assert cli(root, "init").returncode == 0
    yml = root / "testbench.yaml"
    text = yml.read_text()
    assert '# json: {"**/metrics.json": {accuracy: ">= 0.9"}}' in text
    yml.write_text(text.replace('# json: {"**/metrics.json"', 'json: {"**/metrics.json"'))
    r = cli(root, "run", "tiny-model-local", "--wait", "300")
    assert r.returncode == 0 and "**/metrics.json: accuracy >= 0.9" in r.stdout, r.stdout


def test_a_notebook_that_prints_its_number_gets_a_fill_in_line_that_checks_it(tmp_path):
    """A notebook that prints `Test accuracy = 0.95` and writes no metrics file: its FILL IN line reads the printed
    number, and it works when uncommented. One that prints nothing named is offered both ways."""
    root = tmp_path / "nb"
    make_notebook(root / "iris.ipynb", ["acc = 0.95", 'print(f"Test accuracy = {acc}")'])
    make_notebook(root / "plain.ipynb", ["x = 1", "print(x)"])
    assert cli(root, "init").returncode == 0
    yml = root / "testbench.yaml"
    text = yml.read_text()
    line = '# metrics: {stdout: {"Test accuracy = *([0-9.eE+-]+)": ">= 0.9"}}   # FILL IN: the number it prints'
    assert line in text and '# json: {outputs/metrics.json' in text.split("plain-local:")[1], text
    assert '# metrics: {stdout: {"accuracy: *([0-9.eE+-]+)": ">= 0.9"}}   # or a number it prints' in text
    yml.write_text(text.replace('# metrics: {stdout: {"Test accuracy', 'metrics: {stdout: {"Test accuracy'))
    r = cli(root, "run", "iris-local", "--wait", "300")
    assert r.returncode == 0 and "stdout /Test accuracy = *([0-9.eE+-]+)/ >= 0.9 - 0.95" in r.stdout, r.stdout


def test_init_again_with_force_writes_the_agent_rules_once(tmp_path):
    """`testbench init --recipe vitest --force` after a plain init (to get the recipe's sandbox plan) left CLAUDE.md
    with its line twice."""
    (tmp_path / "CLAUDE.md").write_text("# my notes\n")
    assert cli(tmp_path, "init").returncode == 0
    assert cli(tmp_path, "init", "--recipe", "pytest", "--force").returncode == 0
    claude, agents = (tmp_path / "CLAUDE.md").read_text(), (tmp_path / "AGENTS.md").read_text()
    assert claude == "# my notes\n\nSee AGENTS.md for how to work in this project (agent-testbench).\n", claude
    assert agents.count("<!-- agent-testbench -->") == 1, agents


def test_a_json_pattern_must_name_one_file(tmp_path):
    from agent_testbench import expect
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "m.json").write_text('{"x": 1}')
    one = expect.check({"json": {"**/m.json": {"x": "== 1"}}}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})
    assert [c["ok"] for c in one if "m.json" in c["check"]] == [True]
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "m.json").write_text('{"x": 1}')
    two = expect.check({"json": {"**/m.json": {"x": "== 1"}}}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})
    assert "2 files match (a/m.json, b/m.json); the pattern must match exactly one" in [c["detail"] for c in two][-1]
    none = expect.check({"json": {"**/z.json": {"x": "== 1"}}}, workdir=tmp_path, result={"kind": "run", "exit_code": 0})
    assert "nothing matches" in none[-1]["detail"]
