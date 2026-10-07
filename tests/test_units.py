"""Fast checks of each piece on its own: config, form fields, cell files, expectations, budget, lint, mocks."""
import json
import shutil
import subprocess
import time
from pathlib import Path

import nbformat
import pytest

from agent_testbench import budget, config as C, expect, lint as L, notebook as NB
from agent_testbench.backends import modal_backend
from agent_testbench.mocks import Endpoint, StrictAPI, UnexpectedCall
from conftest import EXAMPLES, make_notebook, write_config


# ---- config ------------------------------------------------------------------------------------------------

def test_examples_load():
    cfg = C.load(EXAMPLES / "tiny-model")
    assert set(cfg["plans"]) == {"smoke", "full-modal", "gpu-check", "colab-cpu"}
    assert cfg["plans"]["smoke"]["workdir"] == "inplace" and cfg["plans"]["full-modal"]["workdir"] == "copy"
    assert C.load(EXAMPLES / "web-ui")["plans"]["ui-check"]["steps"][0]["kind"] == "web"


@pytest.mark.parametrize("change, message", [
    ({"project": "has space"}, "project"),
    ({"plans": {"p": {"backend": "aws", "steps": [{"name": "a", "run": "true"}]}}}, "backend"),
    ({"plans": {"p": {"backend": "local", "gpu": "T4", "steps": [{"name": "a", "run": "true"}]}}}, "gpu"),
    ({"plans": {"p": {"backend": "modal", "gpu": "TPU", "steps": [{"name": "a", "run": "true"}]}}}, "offers"),
    ({"plans": {"p": {"steps": [{"name": "a", "run": "x", "notebook": "nb.ipynb"}]}}}, "exactly one"),
    ({"plans": {"p": {"steps": [{"name": "a", "notebook": "missing.ipynb"}]}}}, "does not exist"),
    ({"plans": {"p": {"steps": [{"name": "a", "run": "x"}, {"name": "a", "run": "y"}]}}}, "two steps"),
    ({"plans": {"p": {"steps": [{"name": "a", "run": "x", "secrets": ["KEY=abc"]}]}}}, "NAMES only"),
    ({"plans": {"p": {"steps": [{"name": "a", "run": "x", "max_minutes": 0}]}}}, "max_minutes"),
    ({"budget": {"max_run_usd": "lots"}}, "budget.max_run_usd"),
])
def test_config_errors_say_what_to_fix(tmp_path, change, message):
    cfg = {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true"}]}}, **change}
    write_config(tmp_path, cfg)
    with pytest.raises(C.ConfigError, match=message):
        C.load(tmp_path)


def test_parse_cells():
    assert C.parse_cells("1-3,5") == [1, 2, 3, 5]
    assert C.parse_cells([2, 1, 2]) == [1, 2]
    with pytest.raises(C.ConfigError):
        C.parse_cells("0")


def test_find_root_walks_up(tmp_path):
    write_config(tmp_path, {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true"}]}}})
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert C.find_root(tmp_path / "a" / "b") == tmp_path.resolve()


# ---- notebooks -----------------------------------------------------------------------------------------------

def test_set_params_like_the_colab_form():
    src = ['EPOCHS = 5 #@param {type:"integer"}\nLR = 0.1  # @param {type:"number"}', "print(EPOCHS)"]
    out = NB.set_params(src, {"EPOCHS": 40, "LR": 0.001})
    assert "EPOCHS = 40 #@param" in out[0] and "LR = 0.001  # @param" in out[0] and out[1] == src[1]
    with pytest.raises(NB.ParamError, match="EPOCHS, LR"):
        NB.set_params(src, {"EPOCH": 1})
    with pytest.raises(NB.ParamError, match="2 #@param lines"):
        NB.set_params(src + ['EPOCHS = 1 #@param'], {"EPOCHS": 2})


def test_split_and_build_round_trip(tmp_path):
    nb_path = EXAMPLES / "tiny-model" / "tiny_model.ipynb"
    files = NB.split(nb_path, tmp_path / "cells")
    assert [f.name for f in files][:2] == ["01_tiny_model_handwritten_digits.md", "02_settings.py"]
    rebuilt = NB.build(tmp_path / "cells", tmp_path / "rebuilt.ipynb")
    a, b = nbformat.read(str(nb_path), 4), nbformat.read(str(rebuilt), 4)
    assert [(c.cell_type, c.source) for c in a.cells] == [(c.cell_type, c.source) for c in b.cells]
    again = NB.build(tmp_path / "cells", tmp_path / "again.ipynb")
    assert again.read_text() == rebuilt.read_text(), "building twice must give the same file (stable cell ids)"


def test_example_notebook_is_built_from_its_cells(tmp_path):
    built = NB.build(EXAMPLES / "tiny-model" / "cells", tmp_path / "x.ipynb")
    assert built.read_text() == (EXAMPLES / "tiny-model" / "tiny_model.ipynb").read_text(), \
        "examples/tiny-model/tiny_model.ipynb is stale: run `testbench nb build cells tiny_model.ipynb` there"


def _run(nb_path, tmp_path, minutes=2, **kw):
    import os
    env = {**os.environ, "TESTBENCH_UPLOAD_QUEUE": str(tmp_path / "queue.json"),
           "TESTBENCH_DOWNLOADS_DIR": str(tmp_path / "downloads"), **kw.pop("env", {})}
    from agent_testbench.executor import SHIM
    return NB.run(nb_path, params=kw.pop("params", {}), cells=kw.pop("cells", None), deadline=time.time() + minutes * 60,
                  env=env, workdir=nb_path.parent, out_dir=tmp_path / "out", live_log=tmp_path / "log.txt", shim=SHIM)


def test_run_records_cells_images_and_magics(tmp_path):
    nb = make_notebook(tmp_path / "w" / "nb.ipynb", [
        'N = 2 #@param',
        "print('n is', N)",
        "!echo from-the-shell",
        "from IPython.display import Image, display\nimport base64\n"
        "display(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')))",
    ])
    r = _run(nb, tmp_path, params={"N": 7})
    assert r["status"] == "passed" and [c["status"] for c in r["cells"]] == ["ok"] * 4
    assert "n is 7" in r["cells"][1]["stdout_tail"] and "from-the-shell" in (tmp_path / "log.txt").read_text()
    assert r["images"] == ["cell04_image1.png"] and (tmp_path / "out" / "cell04_image1.png").stat().st_size > 0
    assert (tmp_path / "out" / "executed.ipynb").exists()


def test_run_reports_the_failing_cell_and_line(tmp_path):
    nb = make_notebook(tmp_path / "w" / "nb.ipynb", ["x = 1", "y = 2\nz = {}['missing']\nprint('never')", "print('after')"])
    r = _run(nb, tmp_path)
    assert r["status"] == "failed" and len(r["cells"]) == 2, "execution stops at the first error, as Run all does"
    e = r["error"]
    assert e["cell"] == 2 and e["ename"] == "KeyError" and "line 2" in e["line"] and "missing" in e["line"]


def test_run_stops_at_the_deadline(tmp_path):
    nb = make_notebook(tmp_path / "w" / "nb.ipynb", ["import time\ntime.sleep(30)", "print('never')"])
    began = time.time()
    r = _run(nb, tmp_path, minutes=0.05)
    assert r["status"] == "timeout" and time.time() - began < 25


def test_colab_shim_upload_download_secrets_drive(tmp_path):
    work = tmp_path / "w"
    data = work / "data.csv"
    nb = make_notebook(work / "nb.ipynb", [
        "from google.colab import files, userdata, drive\nimport google.colab\nassert google.colab.TESTBENCH_SHIM",
        "got = files.upload()\nprint(sorted(got))",
        "files.download('data.csv')",
        "print('secret length', len(userdata.get('MY_TOKEN')))",
        "try:\n    userdata.get('NOT_SET')\nexcept userdata.SecretNotFoundError as e:\n    print('missing ok')",
        f"drive.mount({str(tmp_path / 'drive')!r})",
    ])
    data.write_text("a,b\n1,2\n")
    (tmp_path / "queue.json").write_text(json.dumps([[str(data)]]))
    r = _run(nb, tmp_path, env={"MY_TOKEN": "abc123"})
    assert r["status"] == "passed", r["error"]
    log = (tmp_path / "log.txt").read_text()
    assert "['data.csv']" in log and "secret length 6" in log and "missing ok" in log and "abc123" not in log
    assert (tmp_path / "downloads" / "data.csv").exists() and (tmp_path / "drive" / "MyDrive").is_dir()
    assert json.loads((tmp_path / "queue.json").read_text()) == []


def test_upload_without_a_queue_says_how_to_fix(tmp_path):
    nb = make_notebook(tmp_path / "w" / "nb.ipynb", ["from google.colab import files\nfiles.upload()"])
    r = _run(nb, tmp_path)
    assert r["status"] == "failed" and "uploads:" in r["error"]["evalue"]


# ---- expectations ------------------------------------------------------------------------------------------

def test_compare_and_dig():
    assert expect.compare(0.95, ">= 0.9")[0] and not expect.compare(0.85, ">= 0.9")[0]
    assert expect.compare("done", "done")[0] and expect.compare(3, "!= 4")[0]
    assert expect.dig({"a": {"b": [1, 2, 3]}}, "a.b.-1") == 3
    ok, detail = expect.compare(0.71, ">= 0.9")
    assert not ok and "0.71" in detail and ">= 0.9" in detail


def test_check_files_json_text(tmp_path):
    (tmp_path / "m.json").write_text(json.dumps({"acc": 0.93, "loss": [0.9, 0.2]}))
    (tmp_path / "plots").mkdir()
    (tmp_path / "plots" / "a.png").write_bytes(b"x")
    checks = expect.check({"files": ["plots/*.png", "nope.txt"], "json": {"m.json": {"acc": ">= 0.9", "loss.-1": "< 0.1"}},
                           "text": {"stdout": ["epoch 3"]}}, workdir=tmp_path, result={"kind": "run", "exit_code": 0,
                                                                                      "stdout": "epoch 1\nepoch 3\n"})
    got = {c["check"]: c["ok"] for c in checks}
    assert got == {"exit code 0": True, "file plots/*.png": True, "file nope.txt": False, "m.json: acc >= 0.9": True,
                   "m.json: loss.-1 < 0.1": False, "stdout contains 'epoch 3'": True}


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_media_checks(tmp_path):
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=24:duration=2",
                    "-pix_fmt", "yuv420p", str(tmp_path / "v.mp4")], check=True)
    checks = expect.check({"media": {"v.mp4": {"frames": "== 48", "width": 320, "audio": False}}},
                          workdir=tmp_path, result={"kind": "notebook"} | {"error": None})
    assert all(c["ok"] for c in checks), checks


# ---- budget ------------------------------------------------------------------------------------------------

def test_prices_and_worst_case(tmp_path):
    cfg = C.load(EXAMPLES / "tiny-model")
    gpu = budget.worst_case(cfg, "gpu-check")
    per_min = (0.000164 + 2 * 0.0000131 + 4 * 0.00000222) * 60
    assert abs(gpu["usd_per_minute"] - per_min) < 1e-9 and gpu["max_minutes"] == 7
    assert abs(gpu["worst_case_usd"] - round(per_min * 12, 4)) < 1e-9          # 7 min of steps + 5 to start up
    assert budget.worst_case(cfg, "smoke")["worst_case_usd"] == 0


def test_launch_rules(tmp_path):
    cfg = {"project": "p", "budget": {"max_run_usd": 1.0, "ask_above_usd": 0.1, "max_day_usd": 1.2},
           "plans": {"cheap": {"backend": "modal", "steps": [{"name": "a", "run": "true", "max_minutes": 1}]},
                     "mid": {"backend": "modal", "gpu": "T4", "steps": [{"name": "a", "run": "true", "max_minutes": 30}]},
                     "big": {"backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]}}}
    write_config(tmp_path, cfg)
    cfg = C.load(tmp_path)
    assert budget.check_launch(cfg, "cheap", False)[0]
    ok, msg, _ = budget.check_launch(cfg, "mid", False)
    assert not ok and "Needs approval" in msg and "--approve" in msg
    assert budget.check_launch(cfg, "mid", True)[0]
    ok, msg, _ = budget.check_launch(cfg, "big", True)
    assert not ok and "max_run_usd" in msg, "approval cannot exceed the per-run cap"
    budget.record(tmp_path, {"event": "launch", "run": "r1", "worst_case_usd": 1.0})
    budget.record(tmp_path, {"event": "finish", "run": "r1", "usd": 0.9})
    assert budget.spent_today(tmp_path) == 0.9
    ok, msg, _ = budget.check_launch(cfg, "mid", True)
    assert not ok and "max_day_usd" in msg


# ---- lint ----------------------------------------------------------------------------------------------------

def test_lint_finds_the_known_pitfalls(tmp_path):
    nb = make_notebook(tmp_path / "bad.ipynb", [
        "!pip install torch transformers==4.40.0",
        "from google.colab import userdata\nkey = userdata.get('HF_TOKEN')",
        "from google.colab import files\nfiles.upload()",
        "from IPython.display import Video\nVideo('out.mp4', embed=True)",
        "import subprocess, sys\nsubprocess.run([sys.executable, 'render.py'])",
        "import subprocess, sys\nsubprocess.run([sys.executable, '-m', 'pip', 'install', 'x==1'])",
        "open('/content/data.csv')",
        "LR = #@param {type:'number'}",
    ])
    found = L.lint(nb)
    rules = {f["rule"] for f in found}
    assert {"L002", "L003", "L004", "L005", "L006", "L007", "L008"} <= rules
    assert [f["cell"] for f in found if f["rule"] == "L006"] == [5], "pip through subprocess is not a python script"
    assert not L.lint(EXAMPLES / "tiny-model" / "tiny_model.ipynb")


# ---- mocks -------------------------------------------------------------------------------------------------

def test_strict_api(tmp_path, monkeypatch):
    log = tmp_path / "calls.json"
    api = StrictAPI({"acme/t2v": Endpoint(required={"prompt": str}, optional={"duration": int, "res": ("720p", "1080p")},
                                          ranges={"duration": (5, 15)})}, log=str(log), needs_env="ACME_KEY")
    with pytest.raises(UnexpectedCall, match="ACME_KEY"):
        api.check("acme/t2v", {"prompt": "x"})
    monkeypatch.setenv("ACME_KEY", "placeholder")
    api.check("acme/t2v", {"prompt": "a road", "duration": 10, "res": "1080p"})
    for bad, why in [({"prompt": "x", "duration": 20}, "outside"), ({"prompt": "x", "res": "4k"}, "not in"),
                     ({"duration": 5}, "requires"), ({"prompt": "x", "fps": 30}, "does not accept"),
                     ({"prompt": "x", "duration": True}, "must be int")]:
        with pytest.raises(UnexpectedCall, match=why):
            api.check("acme/t2v", bad)
    with pytest.raises(UnexpectedCall, match="refused"):
        api.check("acme/other", {})
    assert len(json.loads(log.read_text())) == 1


# ---- generated Modal app -------------------------------------------------------------------------------------

def test_generated_modal_app():
    import ast
    cfg = C.load(EXAMPLES / "tiny-model")
    src = modal_backend.generate(cfg)
    ast.parse(src)
    assert 'modal.App(\'testbench-tiny-model\')' in src
    assert "def plan_full_modal(" in src and "def plan_gpu_check(" in src and "def plan_smoke(" not in src
    assert "gpu='T4'" in src and "gpu=None" in src and "retries=0" in src and "scaledown_window=2" in src
    assert "timeout=780" in src and "timeout=600" in src            # (10 + 3) and (7 + 3) minutes
    lines = [ln.strip() for ln in src.splitlines()]
    i = lines.index('.add_local_python_source("agent_testbench")')
    assert lines[i + 1].startswith(".add_local_dir(") and lines[i + 2] == ")", "local files must be the last image steps"
    assert "'scikit-learn==1.5.2'" in src and "cpu=2.0" in src and "memory=4096" in src
