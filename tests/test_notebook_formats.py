"""What a notebook step accepts besides a Colab .ipynb: papermill `parameters` cells, percent-format .py notebooks
(`# %%`), and kernels other than python3 (a second Python kernelspec, and the refusal for one not installed)."""
import json
import os
import sys
import textwrap
import time
import types
from pathlib import Path

import nbformat
import pytest

from agent_testbench import config as C, lint as L, notebook as NB
from agent_testbench.executor import SHIM
from conftest import cli, make_notebook, write_config

PERCENT = textwrap.dedent('''\
    # ---
    # jupyter:
    #   kernelspec:
    #     display_name: Python 3
    #     language: python
    #     name: python3
    # ---

    # %% [markdown]
    # # Squares
    #
    # A percent-format notebook.

    # %% tags=["parameters"]
    ROWS = 3
    LABEL: str = "ada"

    # %%
    # !echo from-the-shell
    import json, pathlib
    doc = """
    # %% inside a string is not a cell
    """
    pathlib.Path("out").mkdir(exist_ok=True)

    # %% Write the result {"tags": ["writes"]}
    json.dump({"rows": ROWS, "label": LABEL, "squares": [i * i for i in range(ROWS)]}, open("out/result.json", "w"))
    print("wrote", ROWS, LABEL)
    ''')


def papermill_notebook(path: Path, extra: list[str] = ()) -> Path:
    nb = make_notebook(path, ['N = 2 #@param {type:"integer"}', "ROWS = 3\nLABEL = 'ada'  # who", *extra,
                              "print('rows', ROWS, 'label', LABEL, 'n', N)"])
    doc = nbformat.read(str(nb), 4)
    doc.cells[1].metadata["tags"] = ["parameters"]
    nbformat.write(doc, str(nb))
    return nb


def run(nb_path: Path, tmp_path: Path, **kw):
    env = {**os.environ, "TESTBENCH_UPLOAD_QUEUE": str(tmp_path / "queue.json"),
           "TESTBENCH_DOWNLOADS_DIR": str(tmp_path / "downloads"), **kw.pop("env", {})}
    return NB.run(nb_path, params=kw.pop("params", {}), cells=kw.pop("cells", None), deadline=time.time() + 120,
                  env=env, workdir=nb_path.parent, out_dir=tmp_path / "out", live_log=tmp_path / "log.txt", shim=SHIM)


def executed(tmp_path: Path):
    return nbformat.read(str(tmp_path / "out" / "executed.ipynb"), 4)


# ---- papermill parameters ---------------------------------------------------------------------------------

def test_papermill_parameters_are_injected_after_the_parameters_cell(tmp_path):
    png = "from IPython.display import Image, display\nimport base64\ndisplay(Image(data=base64.b64decode(" \
          "'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')))"
    nb = papermill_notebook(tmp_path / "w" / "nb.ipynb", [png])
    r = run(nb, tmp_path, params={"ROWS": 7, "LABEL": "grace", "N": 5})
    assert r["status"] == "passed", r["error"]
    assert r["code_cells"] == 4 and [c["cell"] for c in r["cells"]] == [1, 2, 3, 4], "numbering is the file's"
    assert "rows 7 label grace n 5" in r["cells"][3]["stdout_tail"]
    assert r["images"] == ["cell03_image1.png"], "the injected cell does not shift later cells' numbers"
    cells = executed(tmp_path).cells
    assert NB.cell_tags(cells[1]) == ["parameters"] and cells[1].source.startswith("ROWS = 3"), "defaults kept"
    assert NB.cell_tags(cells[2]) == ["injected-parameters"]
    assert cells[2].source == "# Parameters\nROWS = 7\nLABEL = 'grace'\n", "form fields are set in place, not injected"
    assert "N = 5 #@param" in cells[0].source


def test_an_unknown_param_names_the_parameters_the_notebook_has(tmp_path):
    nb = papermill_notebook(tmp_path / "w" / "nb.ipynb")
    with pytest.raises(NB.ParamError) as e:
        run(nb, tmp_path, params={"ROW": 1})
    assert "no parameter 'ROW'" in str(e.value) and "form fields (NAME = value  #@param) are: N" in str(e.value)
    assert "papermill parameters (set in the code cell tagged `parameters`) are: ROWS, LABEL" in str(e.value)
    plain = make_notebook(tmp_path / "p.ipynb", ["x = 1"])
    with pytest.raises(NB.ParamError, match="are: none; .* none \\(no code cell is tagged `parameters`\\)"):
        NB.prepare(NB.read(plain), {"x": 2})


def test_cells_that_skip_where_a_param_is_set_are_refused(tmp_path):
    nb = papermill_notebook(tmp_path / "w" / "nb.ipynb")
    with pytest.raises(NB.ParamError) as e:
        run(nb, tmp_path, params={"ROWS": 7, "N": 5}, cells=[3])
    msg = str(e.value)
    assert "ROWS in code cell 2 (the cell tagged `parameters`)" in msg and "N in code cell 1 (a Colab form field)" in msg
    assert "the cells asked for (3) do not include those cells" in msg and "Add cells 1, 2 to the cells to run" in msg
    r = run(nb, tmp_path, params={"ROWS": 7, "N": 5}, cells=[1, 2, 3])
    assert r["status"] == "passed" and "rows 7 label ada n 5" in r["cells"][-1]["stdout_tail"], "with their cells, run"
    with pytest.raises(NB.ParamError, match="LABEL in code cell 2 .* do not include that cell, .* Add cell 2 "):
        NB.check_cells_set_params(NB.read(nb), {"LABEL": "x"}, [1, 3])


def test_a_cell_left_by_a_papermill_run_is_replaced(tmp_path):
    nb = papermill_notebook(tmp_path / "w" / "nb.ipynb")
    doc = nbformat.read(str(nb), 4)
    stale = nbformat.v4.new_code_cell("# Parameters\nROWS = 100\n", metadata={"tags": ["injected-parameters"]})
    doc.cells.insert(2, stale)
    nbformat.write(doc, str(nb))
    r = run(nb, tmp_path, params={"ROWS": 4})
    assert r["status"] == "passed" and "rows 4 label ada" in r["cells"][-1]["stdout_tail"]
    assert [c.source for c in executed(tmp_path).cells].count("# Parameters\nROWS = 4\n") == 1
    assert r["code_cells"] == 4, "the stale cell is a cell of the file; it is replaced, not added to"


def test_session_cells_run_the_injected_values_with_the_parameters_cell(tmp_path):
    nb = NB.read(papermill_notebook(tmp_path / "nb.ipynb"))
    code = NB.cell_sources(nb, {"LABEL": "x", "N": 9})
    assert len(code) == 3 and code[1].endswith("# Parameters\nLABEL = 'x'\n") and "N = 9 #@param" in code[0]


def test_fields_lists_both_kinds(tmp_path):
    nb = papermill_notebook(tmp_path / "nb.ipynb")
    out = cli(tmp_path, "nb", "fields", "nb.ipynb").stdout
    assert "cell 1: N = 2  (Colab form field)" in out and "cell 2: ROWS = 3  (papermill parameter)" in out
    assert "cell 2: LABEL = 'ada'  (papermill parameter)" in out
    (tmp_path / "squares.py").write_text(PERCENT)
    out = cli(tmp_path, "nb", "fields", "squares.py").stdout
    assert "cell 1: ROWS = 3  (papermill parameter)" in out and "cell 1: LABEL = \"ada\"" in out
    make_notebook(tmp_path / "none.ipynb", ["x = 1"])
    assert "nothing params can set" in cli(tmp_path, "nb", "fields", "none.ipynb").stdout
    assert nb.exists()


# ---- percent-format .py notebooks -------------------------------------------------------------------------

def test_the_built_in_reader_follows_the_percent_format():
    nb = NB.from_percent(PERCENT)
    assert nb.metadata["kernelspec"]["name"] == "python3", "the jupytext header gives the metadata"
    assert [c.cell_type for c in nb.cells] == ["markdown", "code", "code", "code"]
    md, params, setup, write = nb.cells
    assert md.source == "# Squares\n\nA percent-format notebook."
    assert params.metadata == {"tags": ["parameters"]} and params.source == 'ROWS = 3\nLABEL: str = "ada"'
    assert setup.source.startswith("!echo from-the-shell\n"), "commented magics are uncommented, as jupytext writes them"
    assert "# %% inside a string" in setup.source, "a marker inside a string is not a cell"
    assert write.metadata == {"tags": ["writes"], "title": "Write the result"}
    raw = NB.from_percent("x = 1\n\n#%%   [raw]\n# keep\n\n# %% [md] tags=[\"a\", \"b\"]\n# hi\n#\n# # % not magic\n")
    assert [(c.cell_type, c.source) for c in raw.cells] == [("code", "x = 1"), ("raw", "keep"),
                                                             ("markdown", "hi\n\n# % not magic")]
    assert raw.cells[2].metadata == {"tags": ["a", "b"]}
    code = NB.from_percent("# %%\nif True:\n    # %time y = 2\n    # # %escaped comment\n# a comment\nout = 1\n").cells[0]
    assert code.source == "if True:\n    %time y = 2\n    # %escaped comment\n# a comment\nout = 1"


def test_jupytext_reads_the_file_when_it_is_installed(tmp_path, monkeypatch):
    calls = []
    fake = types.ModuleType("jupytext")
    fake.reads = lambda text, fmt: calls.append(fmt) or NB.from_percent(text)
    monkeypatch.setitem(sys.modules, "jupytext", fake)
    (tmp_path / "squares.py").write_text(PERCENT)
    assert len(NB.read(tmp_path / "squares.py").cells) == 4 and calls == ["py:percent"]


def test_a_percent_notebook_runs_as_a_plan_step_and_init_finds_it(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "squares.py").write_text(PERCENT)
    (root / "helpers.py").write_text("def double(x):\n    return 2 * x\n")
    r = cli(root, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "plan squares-local: notebook squares.py (2 papermill parameter(s), writes result.json)" in r.stdout
    assert "helpers" not in r.stdout, "a plain .py is not a notebook"
    assert "# params: {ROWS: 3, LABEL: \"ada\"}      # its papermill parameters" in (root / "testbench.yaml").read_text()
    write_config(root, {"project": "proj", "plans": {"quick": {"steps": [
        {"name": "lint", "run": "testbench lint squares.py --strict", "max_minutes": 1},
        {"name": "squares", "notebook": "squares.py", "params": {"ROWS": 5}, "max_minutes": 3, "artifacts": ["out/*"],
         "expect": {"json": {"out/result.json": {"rows": "== 5", "squares.-1": "== 16"}}}}]}}})
    r = cli(root, "run", "quick", "--wait", "120")
    assert r.returncode == 0, r.stdout + r.stderr
    step = sorted((root / ".testbench" / "runs").iterdir())[-1] / "steps" / "02-squares"
    nb = nbformat.read(str(step / "executed.ipynb"), 4)
    assert NB.cell_tags(nb.cells[2]) == ["injected-parameters"] and "ROWS = 5" in nb.cells[2].source
    assert any("from-the-shell" in o.get("text", "") for c in nb.cells for o in c.get("outputs", [])), \
        "the executed notebook is the evidence, outputs and all"


def test_a_plain_py_is_refused_with_what_to_use_instead(tmp_path):
    (tmp_path / "script.py").write_text("print('hi')\n")
    write_config(tmp_path, {"project": "p", "plans": {"p": {"steps": [{"name": "s", "notebook": "script.py"}]}}})
    with pytest.raises(C.ConfigError, match=r"plain Python file.*Use a run: step instead \(run: python script.py\)"):
        C.load(tmp_path)
    with pytest.raises(NB.NotebookError, match="no `# %%` cell markers"):
        NB.read(tmp_path / "script.py")
    r = cli(tmp_path, "lint", "script.py")
    assert r.returncode == 2 and "run: python script.py" in r.stderr


def test_lint_reads_percent_notebooks(tmp_path):
    (tmp_path / "nb.py").write_text("# %% tags=[\"parameters\"]\nA = 1\n\n# %%\n# !pip install numpy\n\n"
                                    "# %% tags=[\"parameters\"]\nB = 2\n")
    found = {(f["rule"], f["cell"]) for f in L.lint(tmp_path / "nb.py")}
    assert ("L005", 2) in found, "the commented !pip is read as the magic it is"
    assert ("L009", 3) in found, "two cells tagged parameters"
    (tmp_path / "empty.py").write_text("# %% tags=[\"parameters\"]\n# nothing here\n\n# %%\nprint(1)\n")
    assert [f["rule"] for f in L.lint(tmp_path / "empty.py")] == ["L009"]


# ---- kernels ----------------------------------------------------------------------------------------------

def kernelspec(jupyter_path: Path, name: str, language: str, env: dict) -> None:
    """A kernelspec that starts this Python's ipykernel, under whatever name and language a test needs."""
    d = jupyter_path / "kernels" / name
    d.mkdir(parents=True)
    (d / "kernel.json").write_text(json.dumps({"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
                                               "display_name": name, "language": language, "env": env}))


def with_kernel(path: Path, cells: list[str], name: str, language: str | None) -> Path:
    nb = make_notebook(path, cells)
    doc = nbformat.read(str(nb), 4)
    doc.metadata["kernelspec"] = {"name": name, "display_name": name, **({"language": language} if language else {})}
    nbformat.write(doc, str(nb))
    return nb


def test_a_second_python_kernel_is_used_when_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jp"))
    kernelspec(tmp_path / "jp", "other-py", "python", {"TB_WHICH_KERNEL": "other-py"})
    nb = with_kernel(tmp_path / "w" / "nb.ipynb", ['N = 1 #@param', "import os, google.colab\n"
                                                   "print('kernel', os.environ.get('TB_WHICH_KERNEL'), N)"], "other-py", None)
    r = run(nb, tmp_path, params={"N": 3})
    assert r["status"] == "passed" and r["kernel"] == "other-py", r["error"]
    assert "kernel other-py 3" in r["cells"][1]["stdout_tail"], "the notebook's kernel, with the shim and form fields"


def test_a_python_kernel_that_is_not_installed_falls_back_to_python3(tmp_path):
    nb = with_kernel(tmp_path / "w" / "nb.ipynb", ["print('ran')"], "conda-env-gone-py", "python")
    r = run(nb, tmp_path)
    assert r["status"] == "passed" and r["kernel"] == "python3"
    assert "names kernel 'conda-env-gone-py', which is not installed here; it runs on python3" in (tmp_path / "log.txt").read_text()


def test_a_kernel_that_is_not_installed_is_named_with_how_to_install_it(tmp_path):
    nb = with_kernel(tmp_path / "w" / "nb.ipynb", ["x <- 1"], "fakelang-0.3", "fakelang")
    with pytest.raises(NB.NotebookError) as e:
        run(nb, tmp_path)
    msg = str(e.value)
    assert "'fakelang-0.3' kernel" in msg and "not installed" in msg and "`jupyter kernelspec list` should show" in msg
    assert "python3" in msg, "it says which kernels are installed"
    from jupyter_client.kernelspec import KernelSpecManager
    if "ir" not in KernelSpecManager().find_kernel_specs():
        with pytest.raises(NB.NotebookError, match=r"'ir' kernel .*IRkernel::installspec\(\)"):
            NB.kernel(NB.read(with_kernel(tmp_path / "r.ipynb", ["x <- 1"], "ir", None)))
    root = tmp_path / "proj"                                      # through a plan: an error that says the same
    with_kernel(root / "nb.ipynb", ["x <- 1"], "fakelang-0.3", "fakelang")
    write_config(root, {"project": "p", "plans": {"p": {"steps": [{"name": "nb", "notebook": "nb.ipynb", "max_minutes": 2}]}}})
    r = cli(root, "run", "p", "--wait", "60")
    assert r.returncode == 1 and "NotebookError" in r.stdout and "jupyter kernelspec list" in r.stdout, r.stdout


def test_other_languages_get_no_shim_and_refuse_params(tmp_path, monkeypatch):
    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path / "jp"))
    kernelspec(tmp_path / "jp", "fake-r", "R", {})                 # an R kernel, as far as testbench can tell
    nb = with_kernel(tmp_path / "w" / "nb.ipynb", ["import sys\nprint('shim', 'google.colab' in sys.modules)"], "fake-r", "R")
    with pytest.raises(NB.ParamError, match=r"runs on the fake-r kernel \(r\).*Sys.getenv"):
        run(nb, tmp_path, params={"EPOCHS": 2})
    r = run(nb, tmp_path)
    assert r["status"] == "passed" and r["kernel"] == "fake-r" and "shim False" in r["cells"][0]["stdout_tail"]


# ---- remote ------------------------------------------------------------------------------------------------

SSH = '''\
import subprocess, sys
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-p", "-i", "-l", "-F", "-J", "-S") else 1
sys.exit(subprocess.run(" ".join(args[i + 1:]), shell=True).returncode)
'''


@pytest.mark.posix
def test_a_percent_notebook_with_parameters_runs_on_a_remote_backend(tmp_path):
    """The ssh backend with a stand-in for ssh (as in test_ssh.py): the .py goes to the machine as it is, and the
    executor there - this package, shipped with the run - reads it, injects the parameters and runs it."""
    shim = tmp_path / "fake_ssh.py"
    shim.write_text(SSH)
    root = tmp_path / "proj"
    root.mkdir()
    (root / "squares.py").write_text(PERCENT)
    write_config(root, {"project": "proj", "budget": {"max_run_usd": 1, "ask_above_usd": 1, "max_day_usd": 5},
                        "ssh": {"host": "box", "command": [sys.executable, str(shim)], "workdir": str(tmp_path / "remote"),
                                "python": sys.executable, "setup": "none", "usd_per_hour": 1.2},
                        "plans": {"remote": {"backend": "ssh", "steps": [
                            {"name": "squares", "notebook": "squares.py", "params": {"ROWS": 4}, "max_minutes": 3,
                             "artifacts": ["out/*"], "expect": {"json": {"out/result.json": {"squares.-1": "== 9"}}}}]}}})
    r = cli(root, "run", "remote", "--wait", "180", env={"TESTBENCH_SSH_POLL_SECONDS": "0.5"})
    assert r.returncode == 0 and "# PASS - plan `remote` on ssh" in r.stdout, r.stdout + r.stderr
    step = sorted((root / ".testbench" / "runs").iterdir())[-1] / "steps" / "01-squares"
    assert "ROWS = 4" in (step / "executed.ipynb").read_text()


def test_a_percent_notebook_in_a_live_session(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "squares.py").write_text(PERCENT)
    write_config(root, {"project": "proj", "plans": {"p": {"steps": [{"name": "x", "run": "true"}]}}})
    assert cli(root, "session", "start").returncode == 0
    try:
        r = cli(root, "exec", "--cells", "squares.py", "--params", '{"LABEL": "lin"}')
        assert r.returncode == 0 and "wrote 3 lin" in r.stdout, r.stdout + r.stderr
    finally:
        cli(root, "session", "stop", "--all")
