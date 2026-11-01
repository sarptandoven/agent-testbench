"""testbench rescue: broken notebooks of each kind, read without running and run in a real kernel, with the
diagnosis an agent acts on, the suggested plan, the fixed copy, --json and the MCP tool."""
import asyncio
import json
import re
from pathlib import Path

import nbformat
import pytest
import yaml

from conftest import cli, make_notebook, write_config
from agent_testbench import config as C, rescue as RS


def _counts(path: Path, counts: list) -> Path:
    nb = nbformat.read(str(path), as_version=4)
    for cell, k in zip([c for c in nb.cells if c.cell_type == "code"], counts):
        cell["execution_count"] = k
    nbformat.write(nb, str(path))
    return path


def _found(info, kind, cell=None):
    return [f for f in info["findings"] if f["kind"] == kind and (cell is None or f["cell"] == cell)]


@pytest.fixture(autouse=True)
def no_gpu(monkeypatch):
    monkeypatch.setattr(RS, "has_gpu", lambda: False)


# ---- reading without running ----------------------------------------------------------------------------------

def test_reading_finds_cells_out_of_order_and_names_nothing_makes(tmp_path):
    nb = _counts(make_notebook(tmp_path / "nb.ipynb", [
        "import numpy as np",
        "print(model)",
        "model = np.zeros(2)",
        "df = pd.DataFrame()",
        "def f():\n    return made_when_called\nsq = [i * i for i in range(3)]\nfiles = !ls",
        "%%capture cap\nprint(sq, files)",
        "try:\n    maybe\nexcept NameError:\n    maybe = 0\nprint(cap, maybe, _, display)",
    ]), [1, 7, 3, None, 4, 5, 6])
    info = RS.read(nb)
    order = _found(info, "order")
    assert len(order) == 1 and order[0]["cell"] == 2 and "`model`" in order[0]["message"] and "cell 3" in order[0]["message"]
    assert "Move cell 3" in order[0]["fix"] and "never moves cells" in order[0]["fix"]
    assert info["undefined"] == ["pd"], "function bodies, comprehensions, !ls, %%capture and try/except NameError are fine"
    assert "import pandas as pd" in _found(info, "undefined")[0]["fix"]
    hidden = " ".join(f["message"] for f in _found(info, "hidden_state"))
    assert "out of order" in hidden and "cell 3 ran (count 3) before cell 2 (count 7)" in hidden
    assert "cell(s) 4 have no saved count" in hidden
    gaps = _counts(make_notebook(tmp_path / "gaps.ipynb", ["a = 1", "b = 2", "c = 3"]), [1, 2, 7])
    assert "4 execution(s) are not in the saved notebook" in _found(RS.read(gaps), "hidden_state")[0]["message"]


def test_reading_names_packages_and_their_pip_names(tmp_path):
    assert [RS.pip_name(m) for m in ("sklearn.linear_model", "cv2", "PIL.Image", "yaml", "bs4", "google.cloud.storage")] == [
        ("scikit-learn", True), ("opencv-python", True), ("pillow", True), ("pyyaml", True), ("beautifulsoup4", True),
        ("google-cloud-storage", True)]
    assert RS.pip_name("numpy") == ("numpy", True) and RS.pip_name("some_thing") == ("some-thing", False)
    (tmp_path / "helpers.py").write_text("X = 1\n")
    nb = make_notebook(tmp_path / "nb.ipynb", [
        "!pip install -q 'zz-early-rescue>=1' requests==2.32.3",
        "import yaml, json, os.path\nimport zz_missing_rescue_pkg\nimport zz_early_rescue\nimport helpers",
        "%%writefile mymod.py\nX = 1",
        "import mymod\ntry:\n    import zz_optional_rescue\nexcept ImportError:\n    zz_optional_rescue = None",
        "import zz_late_rescue",
        "%pip install zz-late-rescue",
    ])
    info = RS.read(nb)
    pkgs = {p["import"]: p for p in info["packages"]}
    assert set(pkgs) == {"yaml", "zz_missing_rescue_pkg", "zz_early_rescue", "zz_optional_rescue", "zz_late_rescue"}, \
        "the standard library and the notebook's own modules are not packages"
    assert pkgs["yaml"]["installed"] and pkgs["yaml"]["pip"] == "pyyaml" and pkgs["yaml"]["version"]
    assert not pkgs["zz_missing_rescue_pkg"]["installed"] and not pkgs["zz_missing_rescue_pkg"]["sure"]
    by = {f["message"].split("`")[1]: f for f in _found(info, "packages") if f["message"].count("`") >= 2}
    assert by["zz_missing_rescue_pkg"]["severity"] == "blocks" and "pip install zz-missing-rescue-pkg" in by["zz_missing_rescue_pkg"]["fix"]
    assert by["zz_early_rescue"]["severity"] == "info" and "cell 1 installs it" in by["zz_early_rescue"]["message"]
    assert by["zz_optional_rescue"]["severity"] == "info" and "try/except" in by["zz_optional_rescue"]["message"]
    assert by["zz_late_rescue"]["severity"] == "blocks" and "only after that" in by["zz_late_rescue"]["message"]
    assert any("`!pip install`" in f["message"] and "%pip" in f["fix"] for f in _found(info, "packages"))


def test_reading_colab_parts_files_paths_input_and_shell(tmp_path):
    (tmp_path / "present.txt").write_text("here\n")
    nb = make_notebook(tmp_path / "nb.ipynb", [
        "from google.colab import drive, files, auth\ndrive.mount('/content/drive')",
        "up = files.upload()",
        "import pandas as pd\ndf = pd.read_csv('/content/drive/MyDrive/data.csv')\nDATA = 'data'\n"
        "raw = open(DATA + '/raw.txt').read()\nok = open('present.txt').read()\nfeats = '/home/alice/feats.npy'",
        "open('out.txt', 'w').write('x')\nprint(open('out.txt').read())",
        "name = input('who?')",
        "!apt-get install -y ffmpeg\n!cd somewhere\n!wget -q https://example.com/d.zip\n!unzip -q d.zip",
        "rows = open('extracted/rows.csv').read()\nz = open('d.zip', 'rb')",
        "%tensorflow_version 2.x",
        "print 'py2'",
    ])
    info = RS.read(nb)
    parts = {c["part"]: c["covered"] for c in info["colab"]}
    assert parts == {"google.colab.drive": True, "google.colab.files": True, "google.colab.auth": False}
    colab = {(f["cell"], f["severity"]): f for f in _found(info, "colab")}
    assert set(colab) == {(1, "blocks"), (1, "info"), (2, "warn"), (3, "blocks"), (8, "blocks")}
    assert "google.colab.auth" in colab[1, "blocks"]["message"] and "uploads:" in colab[2, "warn"]["fix"]
    assert "MyDrive an empty local folder, so /content/drive/MyDrive/data.csv is missing" in colab[1, "info"]["message"]
    assert "reads /content/drive/MyDrive/data.csv" in colab[3, "blocks"]["message"]
    assert "%tensorflow_version" in colab[8, "blocks"]["message"]
    files = {f["cell"]: f for f in _found(info, "files")}
    assert set(files) == {3, 7}, "present.txt is there, out.txt and d.zip are made by the notebook"
    assert "data/raw.txt" in files[3]["message"] and files[3]["severity"] == "warn" and "cell 2" in files[3]["fix"]
    assert "extracted/rows.csv" in files[7]["message"] and "cell 6" in files[7]["fix"], "an unzip may have made it"
    assert _found(info, "paths")[0]["severity"] == "warn" and "/home/alice/feats.npy" in _found(info, "paths")[0]["message"]
    assert _found(info, "input", 5)[0]["severity"] == "blocks" and "form field" in _found(info, "input")[0]["fix"]
    shell = " ".join(f["message"] for f in _found(info, "shell", 6))
    assert "apt-get" in shell and "`!cd` runs in a shell of its own" in shell and "downloads at run time" in shell
    assert "Python 2" in _found(info, "syntax", 9)[0]["message"]


def test_reading_secrets_and_gpu_shape_the_plan(tmp_path, monkeypatch):
    monkeypatch.delenv("RESCUE_TOKEN", raising=False)
    monkeypatch.delenv("RESCUE_API_KEY", raising=False)
    monkeypatch.setenv("HOME_LIKE", "/x")
    nb = make_notebook(tmp_path / "train.ipynb", [
        'EPOCHS = 50 #@param {type:"integer"}\nN_SAMPLES = 10000 #@param\nBATCH_SIZE = 64 #@param\nLR = 0.01 #@param\n'
        'NAME = "ada" #@param',
        "from google.colab import userdata\ntok = userdata.get('RESCUE_TOKEN')",
        "import os\nh = os.environ['HOME_LIKE']\nk = os.environ['RESCUE_API_KEY']",
        "import torch\nx = torch.zeros(2).cuda()\ntorch.save(x, 'outputs/model.pt')",
    ])
    d = RS.rescue(nb, run=False)
    secrets = {re.search(r"reads the (?:secret|environment variable) (\w+)", f["message"]).group(1): f
               for f in _found(d, "secrets")}
    assert set(secrets) == {"RESCUE_TOKEN", "RESCUE_API_KEY"} and all(f["severity"] == "blocks" for f in secrets.values())
    assert _found(d, "gpu")[0]["severity"] == "blocks" and "no NVIDIA GPU" in _found(d, "gpu")[0]["message"]
    plan = yaml.safe_load("plans:\n" + d["plan"]["yaml"])["plans"][d["plan"]["name"]]
    assert plan["backend"] == "modal" and plan["gpu"] == "T4", "a GPU notebook on a machine without one: a GPU plan"
    step = plan["steps"][0]
    assert step["params"] == {"EPOCHS": 1, "N_SAMPLES": 100, "BATCH_SIZE": 8, "LR": 0.01, "NAME": "ada"}
    assert step["secrets"] == ["RESCUE_API_KEY", "RESCUE_TOKEN"] and step["expect"]["files"] == ["**/model.pt"]
    C.validate({"project": "p", "plans": {d["plan"]["name"]: plan}}, tmp_path)
    assert (Path(d["dir"]) / "plan.yaml").exists() and (tmp_path / ".testbench" / ".gitignore").exists()


# ---- the run ----------------------------------------------------------------------------------------------------

RUNS = {
    "missing_package": (["x = 1", "import zz_not_installed_rescue"], 2, "pip install zz-not-installed-rescue"),
    "order": (["a = 1", "print(total)", "total = a + 1"], 2, "Move cell 3"),
    "missing_file": (["import json", "json.load(open('data/settings.json'))"], 2, "data/settings.json"),
    "input": (["name = input('who?')"], 1, "form field"),
    "upload": (["from google.colab import files\nfiles.upload()"], 1, "uploads: [["),
    "secret": (["from google.colab import userdata\nuserdata.get('RESCUE_TEST_TOKEN')"], 1, "secrets: [RESCUE_TEST_TOKEN]"),
    "gpu": (["raise RuntimeError('Found no NVIDIA driver on your system')"], 1, "backend: modal"),
    "magic": (["%tensorflow_version 2.x"], 1, "delete the line"),
    "syntax": (["print 'hello'"], 1, "Python 2"),
    "api_changed": (["import json\njson.loadz('{}')"], 1, "`loadz`"),
}


@pytest.mark.parametrize("kind", list(RUNS))
def test_the_run_stops_at_the_first_failing_cell_and_says_what_to_do(tmp_path, kind):
    cells, cell, words = RUNS[kind]
    d = RS.rescue(make_notebook(tmp_path / "nb.ipynb", cells), timeout=90)
    f = d["failure"]
    assert d["status"] == "failed" and f["cell"] == cell and f["kind"] == kind, f
    assert words in f["fix"], f["fix"]
    if kind == "missing_file":
        assert str(tmp_path / "data" / "settings.json") in f["fix"], "where the notebook expects it"
    if kind == "gpu":
        assert yaml.safe_load(d["plan"]["yaml"])[d["plan"]["name"]]["backend"] == "modal"
    out = Path(d["dir"])
    assert {"executed.ipynb", "log.txt", "kernel.log", "diagnosis.md", "diagnosis.json", "plan.yaml"} <= {p.name for p in out.iterdir()}
    assert json.loads((out / "diagnosis.json").read_text())["failure"]["kind"] == kind


def test_a_cell_that_runs_too_long_is_named(tmp_path):
    d = RS.rescue(make_notebook(tmp_path / "nb.ipynb", ["import time", "time.sleep(60)", "print('never')"]), timeout=6)
    assert d["status"] == "timeout" and d["failure"]["cell"] == 2 and d["failure"]["kind"] == "timeout"
    assert "--timeout 600" in d["failure"]["fix"] and d["seconds"] < 40


def test_a_notebook_that_runs_gets_a_plan_that_proves_it(tmp_path):
    root = tmp_path / "proj"
    make_notebook(root / "nb.ipynb", [
        'EPOCHS = 30 #@param {type:"integer"}\nNAME = "ada" #@param',
        "import json, pathlib\npathlib.Path('out').mkdir(exist_ok=True)",
        "json.dump({'epochs': EPOCHS}, open('out/metrics.json', 'w'))\nprint('done', EPOCHS)",
    ])
    write_config(root, {"project": "proj", "plans": {"other": {"steps": [{"name": "x", "run": "true"}]}}})
    d = RS.rescue(root / "nb.ipynb", timeout=90)
    assert d["status"] == "passed" and d["failure"] is None and d["cells_ran"] == 3
    assert Path(d["dir"]).parent == root / ".testbench" / "rescue", "kept with the project that holds testbench.yaml"
    cfg = yaml.safe_load((root / "testbench.yaml").read_text())
    cfg["plans"].update(yaml.safe_load(d["plan"]["yaml"]))
    write_config(root, cfg)
    step = C.load(root)["plans"][d["plan"]["name"]]["steps"][0]
    assert step["params"] == {"EPOCHS": 1, "NAME": "ada"} and step["expect"]["files"] == ["**/metrics.json"]
    p = cli(root, "run", d["plan"]["name"], "--wait", "300")
    assert p.returncode == 0 and "PASS" in p.stdout, p.stdout + p.stderr
    assert json.loads((root / "out" / "metrics.json").read_text()) == {"epochs": 1}


# ---- --fix, --json, the CLI and MCP -------------------------------------------------------------------------------

def test_fix_writes_a_copy_with_mechanical_fixes_only(tmp_path, monkeypatch):
    missing = {"yaml", "cv2"}
    monkeypatch.setattr(RS, "_probe", lambda names, wd: {n: {"installed": n not in missing, "version": None} for n in names})
    nb = make_notebook(tmp_path / "nb.ipynb", [
        "# md: A title stays first",
        "!pip install -q requests==2.32.3",
        "import yaml\nimport cv2\nimport zz_unsure_rescue",
        "print(model)",
        "model = pd.DataFrame()",
    ])
    before = nb.read_bytes()
    d = RS.rescue(nb, run=False, fix=tmp_path / "fixed.ipynb")
    assert nb.read_bytes() == before, "the original is never written"
    fixed = nbformat.read(str(tmp_path / "fixed.ipynb"), as_version=4)
    assert fixed.cells[0].cell_type == "markdown"
    setup = fixed.cells[1].source
    assert "%pip install -q pyyaml opencv-python" in setup and "zz" not in setup and "import pandas as pd" in setup
    assert [c.source for c in fixed.cells[2:]] == ["%pip install -q requests==2.32.3", "import yaml\nimport cv2\nimport zz_unsure_rescue",
                                                    "print(model)", "model = pd.DataFrame()"], "cells keep their order"
    changes = " ".join(d["fix"]["changes"])
    assert "`%pip install`" in changes and "pyyaml, opencv-python" in changes and "numbered one higher" in changes
    assert d["fix"]["left"] == 1, "moving cell 4 is left for the person"
    p = cli(tmp_path, "rescue", "nb.ipynb", "--no-run", "--fix", "nb.ipynb")
    assert p.returncode == 2 and "never writes the original" in p.stdout


PERCENT = '''\
# ---
# jupyter:
#   kernelspec: {display_name: Python 3, language: python, name: python3}
# ---

# %% [markdown]
# # An analysis
# !pip install stays-in-markdown

# %%
# !pip install -q requests==2.32.3
import yaml

# %% Use it   {"tags": ["main"]}
print("rows", len(pd.DataFrame()))  # pandas, never imported
'''


def test_a_percent_format_py_notebook_is_rescued_and_fixed_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(RS, "_probe", lambda names, wd: {n: {"installed": n != "yaml", "version": None} for n in names})
    nb = tmp_path / "analysis.py"
    nb.write_text(PERCENT)
    d = RS.rescue(nb, run=False, fix=tmp_path / "fixed.py")
    assert d["code_cells"] == 2 and _found(d, "packages") and "notebook: fixed.py" in d["plan"]["yaml"]
    fixed = (tmp_path / "fixed.py").read_text()
    assert fixed.startswith(PERCENT.split("# %%\n")[0]), "the header and the markdown cell are as they were"
    assert "# !pip install stays-in-markdown" in fixed and "# %pip install -q requests==2.32.3" in fixed
    assert "# %%\n# added by testbench rescue" in fixed and "# %pip install -q pyyaml\nimport pandas as pd\n" in fixed
    assert '# %% Use it   {"tags": ["main"]}\nprint("rows"' in fixed, "titles and tags stay"
    from agent_testbench import notebook as NB
    cells = [c.source for c in NB.read(tmp_path / "fixed.py").cells if c.cell_type == "code"]
    assert cells[1:] == ["%pip install -q requests==2.32.3\nimport yaml", "print(\"rows\", len(pd.DataFrame()))  # pandas, never imported"]
    RS.rescue(nb, run=False, fix=tmp_path / "fixed.ipynb")
    assert [c.source for c in nbformat.read(str(tmp_path / "fixed.ipynb"), as_version=4).cells if c.cell_type == "code"] == cells

    p = cli(tmp_path, "rescue", "analysis.py", "--timeout", "90")
    assert p.returncode == 1 and "# Rescue: `analysis.py` fails at cell 2 of 2" in p.stdout, p.stdout
    (tmp_path / "script.py").write_text("print('hi')\n")
    p = cli(tmp_path, "rescue", "script.py")
    assert p.returncode == 2 and "is a plain Python file, not a notebook" in p.stdout and "`# %%`" in p.stdout
    make_notebook(tmp_path / "nb.ipynb", ["x = 1"])
    p = cli(tmp_path, "rescue", "nb.ipynb", "--no-run", "--fix", "nb-fixed.py")
    assert p.returncode == 2 and "give a path that ends in .ipynb" in p.stdout, p.stdout


def test_cli_json_markdown_and_exit_codes(tmp_path):
    make_notebook(tmp_path / "nb.ipynb", ["import json", "print(not_made_anywhere)"])
    p = cli(tmp_path, "rescue", "nb.ipynb", "--json", "--timeout", "90")
    d = json.loads(p.stdout)                                   # nothing but JSON: the kernel's stderr went to kernel.log
    assert p.returncode == 1 and d["status"] == "failed" and d["failure"]["kind"] == "undefined"
    assert d["failure"]["cell"] == 2 and (tmp_path / d["dir"] / "executed.ipynb").exists()
    p = cli(tmp_path, "rescue", "nb.ipynb", "--no-run")
    assert p.returncode == 1 and "# Rescue: `nb.ipynb` read without running it" in p.stdout and "not_made_anywhere" in p.stdout
    make_notebook(tmp_path / "ok.ipynb", ["x = 1"])
    p = cli(tmp_path, "rescue", "ok.ipynb")
    assert p.returncode == 0 and "runs top to bottom here" in p.stdout and "WARNING" not in p.stdout + p.stderr
    assert cli(tmp_path, "rescue", "missing.ipynb").returncode == 2


def test_mcp_tool(tmp_path):
    from agent_testbench import mcp_server
    make_notebook(tmp_path / "nb.ipynb", ["df = pd.DataFrame()"])

    async def go():
        names = {t.name for t in await mcp_server.server.list_tools()}
        out = await mcp_server.server.call_tool("rescue_notebook", {"notebook": "nb.ipynb", "project_dir": str(tmp_path),
                                                                    "run": False})
        return names, out

    names, out = asyncio.run(go())
    text = json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
    assert "rescue_notebook" in names and "# Rescue" in text and "import pandas as pd" in text and "[exit 1]" in text
