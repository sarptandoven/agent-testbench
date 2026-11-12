"""A proof record names what each step really ran on: the Python its `python` resolved to (the project's own from
uvx, uv tool or pipx), the kernel's Python for a notebook, node, go or cargo when the command used them - and from
a tool environment a notebook's kernel runs on the project's virtualenv when that has ipykernel."""
import json
import os
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import nbformat
import pytest

from agent_testbench import config as C, diff as D, executor as E, notebook as NB, report as R
from conftest import cli, make_notebook, write_config

REPORT_SCHEMA = json.loads((Path(C.__file__).parent / "schema" / "report-v1.schema.json").read_text())
BIN = "Scripts" if os.name == "nt" else "bin"


def fake_tools(folder: Path) -> Path:
    """node, go and cargo that only say their versions (node's from FAKE_NODE, so a test can change it)."""
    folder.mkdir(parents=True, exist_ok=True)
    for name, said in (("node", 'echo "${FAKE_NODE:-v99.1.0}"'), ("go", "echo go version go1.99.2 linux/amd64"),
                       ("cargo", "echo 'cargo 1.99.0 (abc123 2026-01-01)'")):
        (folder / name).write_text(f"#!/bin/sh\n{said}\n")
        (folder / name).chmod(0o755)
    os.symlink(sys.executable, folder / "python3.99")         # a Python a step names by its version
    return folder


def run_report(root: Path, plan: str, env: dict) -> dict:
    r = cli(root, "run", plan, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    return json.loads((run / "report.json").read_text())


def version_of(python: str) -> str:
    return subprocess.run([python, "-c", "import platform; print(platform.python_version())"], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.mark.posix
def test_the_report_names_the_interpreters_each_step_ran_on(tmp_path):
    tools = fake_tools(tmp_path / "tools")
    root = tmp_path / "p"
    root.mkdir()
    first = {"PATH": os.pathsep.join([str(tools), os.environ.get("PATH", "")])}     # the fakes before any real one
    write_config(root, {"project": "p", "plans": {"smoke": {"steps": [
        {"name": "py", "run": "python -c \"print('hi')\"", "max_minutes": 1},
        {"name": "named", "run": "python3.99 -c pass", "max_minutes": 1, "env": first},
        {"name": "web-ish", "run": "cd . && npm --version >/dev/null 2>&1; node x.js && go version && cargo --version",
         "max_minutes": 1, "env": first}]}}})
    rep = run_report(root, "smoke", {})
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)
    py, named, web = (s["runtime"] for s in rep["steps"])
    path = shutil.which("python", path=E.step_path(root))
    assert py == {"python": {"path": path, "version": version_of(path)}}, "the Python the step's `python` resolved to"
    assert named["python"] == {"path": str(tools / "python3.99"), "version": version_of(sys.executable)}, \
        "a command that names its Python records that one"
    assert web["node"] == {"path": str(tools / "node"), "version": "v99.1.0"}, "npm and node: node's version"
    assert web["go"] == {"path": str(tools / "go"), "version": "1.99.2"}
    assert web["cargo"] == {"path": str(tools / "cargo"), "version": "1.99.0"}
    assert "node" not in py and "go" not in named, "only what the command uses"
    run = root / ".testbench" / "runs" / rep["run_id"]
    md = (run / "report.md").read_text()
    assert f"The steps ran on py: Python {py['python']['version']} (`{path}`); named: Python" in md, md
    assert "web-ish: Python" in md and "node v99.1.0, go 1.99.2, cargo 1.99.0." in md, md
    page = R.render_html(rep, run)
    assert "<b>steps ran on</b>" in page and "node v99.1.0" in page

    # the same plan on another node: testbench diff names the step and the change
    again = run_report(root, "smoke", {"FAKE_NODE": "v99.2.0"})
    d = D.compare(rep, again, root)
    assert [s["step"] for s in d["environment"]["steps"]] == ["web-ish"]
    shown = D.render(d)
    assert "- step web-ish ran on another interpreter: " in shown and "node v99.1.0" in shown and \
        "node v99.2.0" in shown, shown
    assert "web-ish ran on" in D.summary(d)
    r = cli(root, "diff")
    assert r.returncode == 0 and "ran on another interpreter" in r.stdout and "v99.2.0" in r.stdout, r.stdout


def test_ran_on_line_groups_steps_and_reads_old_reports():
    one = {"python": {"path": "/p/.venv/bin/python", "version": "3.12.1"}}
    rep = {"steps": [{"name": "a", "runtime": one}, {"name": "b", "runtime": {**one, "node": {"path": "/n",
                                                                                              "version": "v20.1.0"}}}]}
    assert R.ran_on_line(rep) == "The steps ran on Python 3.12.1 (/p/.venv/bin/python); b: also node v20.1.0."
    rep["steps"][1]["runtime"]["python"] = {"path": "/usr/bin/python3", "version": None}
    assert R.ran_on_line(rep) == ("The steps ran on a: Python 3.12.1 (/p/.venv/bin/python); b: Python (it did not say "
                                  "its version) (/usr/bin/python3), node v20.1.0.")
    assert R.ran_on_line({"steps": [{"name": "nb", "runtime": {"kernel": "ir"}}]}) == "The step ran on kernel ir."
    assert R.ran_on_line({"steps": [{"name": "old"}]}) == "", "a report from before steps recorded it: no line"
    assert D.compare({"steps": [{"name": "old", "status": "passed"}]},
                     {"steps": [{"name": "old", "status": "passed", "runtime": one}]})["environment"]["steps"] == []


def make_venv(where: Path, ipykernel: bool) -> Path:
    """A virtualenv with no pip (quick, offline); with ipykernel, the one testbench runs with is put on its path."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(where)], check=True, capture_output=True)
    python = where / BIN / ("python.exe" if os.name == "nt" else "python")
    if ipykernel:                                        # the folders this Python finds ipykernel and its needs in
        site = subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                              capture_output=True, text=True, check=True).stdout.strip()
        Path(site).mkdir(parents=True, exist_ok=True)
        found = [p for p in sys.path if p and Path(p).is_dir() and Path(p).name in ("site-packages", "dist-packages")]
        (Path(site) / "testbench-test.pth").write_text("\n".join(found) + "\n")
        assert subprocess.run([str(python), "-c", "import ipykernel"], capture_output=True).returncode == 0
    return python


@pytest.fixture
def from_uv_tool(tmp_path, monkeypatch):
    """testbench as if installed with `uv tool install`: an environment of its own."""
    tool = tmp_path / "uv" / "tools" / "agent-testbench"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(tool))
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert E.tool_environment() == "uv tool"


def notebook_step(root: Path, cells: list[str]) -> dict:
    make_notebook(root / "nb.ipynb", cells)
    return {**C.STEP_DEFAULTS, "name": "nb", "kind": "notebook", "notebook": "nb.ipynb", "max_minutes": 2,
            "cells": None, "expect": {}}


def run_notebook(root: Path, step: dict) -> tuple[dict, str]:
    env = {**os.environ, "PATH": E.step_path(root)}
    result = E.run_step(step, project=root, workdir=root, step_dir=root / "out", base_env=env)
    return result, (root / "out" / "log.txt").read_text()


def test_from_a_tool_environment_a_notebook_runs_on_the_projects_venv(tmp_path, from_uv_tool):
    root = tmp_path / "proj"
    root.mkdir()
    python = make_venv(root / ".venv", ipykernel=True)
    assert E.kernel_python(root) == (str(python), ""), "the project's venv has ipykernel: the kernel runs on it"
    result, log = run_notebook(root, notebook_step(root, ["import sys\nprint('PREFIX', sys.prefix)"]))
    assert result["status"] == "passed", log
    assert f"PREFIX {root / '.venv'}" in log or f"PREFIX {(root / '.venv').resolve()}" in log, \
        "the code ran in the project's virtualenv"
    assert f"[testbench] the kernel runs on the project's Python: {python}" in log, "the log says which Python"
    assert result["runtime"]["kernel"] == "python3" and result["runtime"]["python"]["path"] == str(python)
    assert result["runtime"]["python"]["version"] == version_of(str(python))
    # a module the project's venv lacks: the hint installs it there, not into testbench's own environment
    failed = {**result, "status": "failed", "error": {"ename": "ModuleNotFoundError", "evalue": "No module named 'xgb'"}}
    hint = R.missing_module(failed, {"backend": "local", "plan": "p"})
    assert hint.startswith(f"`xgb` is not installed for the kernel's Python ({python}): `{python} -m pip install xgb`")


def test_a_venv_without_ipykernel_keeps_testbenchs_python_and_says_why(tmp_path, from_uv_tool):
    root = tmp_path / "proj"
    root.mkdir()
    python = make_venv(root / ".venv", ipykernel=False)
    chosen, why = E.kernel_python(root)
    assert chosen is None and "has no ipykernel" in why and f"`{python} -m pip install ipykernel`" in why, why
    result, log = run_notebook(root, notebook_step(root, ["import sys\nprint('PREFIX', sys.prefix)"]))
    assert result["status"] == "passed", log
    assert f"[testbench] {why}" in log, "the log says which Python ran it, and how to run it on the project's"
    assert result["runtime"]["python"]["path"] == NB.kernel_executable("python3", os.environ)
    failed = {**result, "status": "failed", "error": {"ename": "ModuleNotFoundError", "evalue": "No module named 'xgb'"},
              "runtime": {"kernel": "python3", "python": {"path": sys.executable, "version": "3"}}}
    assert "`uv tool install --force --with xgb agent-testbench` adds it" in \
        R.missing_module(failed, {"backend": "local", "plan": "p"}), "the hint for testbench's own Python stays"


def test_which_python_a_notebook_kernel_runs_on(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    assert E.kernel_python(root) == (None, ""), "installed like any package: the kernel testbench always starts"
    tool = tmp_path / "uv" / "tools" / "agent-testbench"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(tool))
    monkeypatch.setattr(E, "_has_ipykernel", lambda python, environ: True)       # the selection, not the check
    chosen, why = E.kernel_python(root, {})
    assert chosen is None and "the project has no virtualenv (.venv or venv)" in why and "uv tool" in why
    venv = root / "venv"
    (venv / BIN).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    python = venv / BIN / ("python.exe" if os.name == "nt" else "python")
    assert E.kernel_python(root, {})[0] is None, "a venv without a Python is no kernel"
    python.write_text("")
    assert E.kernel_python(root, {}) == (str(python), ""), "venv/ as well as .venv/"
    other = tmp_path / "activated"
    (other / BIN).mkdir(parents=True)
    (other / BIN / python.name).write_text("")
    assert E.kernel_python(root, {"VIRTUAL_ENV": str(other)})[0] == str(other / BIN / python.name), \
        "an activated virtualenv wins, as it does for `python` in a run: step"
    assert E.kernel_python(root, {"VIRTUAL_ENV": str(tool)})[0] == str(python), "testbench's own is not the project's"
    monkeypatch.setattr(E, "_has_ipykernel", lambda python, environ: False)
    assert E.kernel_python(root, {})[0] is None


def test_reading_a_notebook_without_cell_ids_says_nothing(tmp_path):
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1")])
    data = json.loads(nbformat.writes(nb))
    for cell in data["cells"]:
        cell.pop("id")                                     # as older editors save nbformat 4.5 notebooks
    (tmp_path / "old.ipynb").write_text(json.dumps(data))
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        assert NB.read(tmp_path / "old.ipynb").cells[0].source == "x = 1"
        assert not seen, [str(w.message) for w in seen]
        nbformat.read(str(tmp_path / "old.ipynb"), as_version=4)
    assert [type(w.message).__name__ for w in seen] == ["MissingIDFieldWarning"], \
        "silenced only around testbench's own read: nbformat itself still warns"
