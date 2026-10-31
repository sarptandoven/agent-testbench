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
