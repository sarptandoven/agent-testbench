"""A proof record names what each step really ran on: the Python its `python` resolved to (the project's own from
uvx, uv tool or pipx), the kernel's Python for a notebook, node, go or cargo when the command used them - and from
a tool environment a notebook's kernel runs on the project's virtualenv when that has ipykernel, and the record's
`environment` (Python, packages) is that Python's, not testbench's own."""
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import nbformat
import pytest

from agent_testbench import check as CK, config as C, diff as D, executor as E, notebook as NB, provenance as P, \
    report as R
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


def site_packages(python: Path) -> Path:
    site = Path(subprocess.run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                               capture_output=True, text=True, check=True).stdout.strip())
    site.mkdir(parents=True, exist_ok=True)
    return site


def make_venv(where: Path, ipykernel: bool) -> Path:
    """A virtualenv with no pip (quick, offline); with ipykernel, the one testbench runs with is put on its path."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(where)], check=True, capture_output=True)
    python = where / BIN / ("python.exe" if os.name == "nt" else "python")
    if ipykernel:                                        # the folders this Python finds ipykernel and its needs in
        found = [p for p in sys.path if p and Path(p).is_dir() and Path(p).name in ("site-packages", "dist-packages")]
        (site_packages(python) / "testbench-test.pth").write_text("\n".join(found) + "\n")
        assert subprocess.run([str(python), "-c", "import ipykernel"], capture_output=True).returncode == 0
    return python


def install_fake(python: Path, name: str, version: str) -> None:
    """A package installed for that Python as importlib.metadata sees one (its dist-info folder; nothing to import)."""
    site = site_packages(python)
    for old in site.glob(f"{name}-*.dist-info"):
        shutil.rmtree(old)
    (site / f"{name}-{version}.dist-info").mkdir()
    (site / f"{name}-{version}.dist-info" / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")


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


def python_plan(root: Path, run: str = "python -c pass") -> dict:
    write_config(root, {"project": "p", "plans": {"smoke": {"steps": [{"name": "py", "run": run, "max_minutes": 1}]}}})
    return C.load(root)["plans"]["smoke"]


def execute(root: Path, plan: dict, run_id: str) -> dict:
    meta = {"schema_version": 1, "run_id": run_id, "plan": "smoke", "backend": "local"}     # as runs.finalize has it
    return E.execute_plan(plan, project_dir=root, run_dir=root.parent / run_id, meta=meta)


def test_from_a_tool_environment_the_record_is_the_steps_python_and_its_packages(tmp_path, from_uv_tool):
    root = tmp_path / "proj"
    root.mkdir()
    python = make_venv(root / ".venv", ipykernel=False)
    install_fake(python, "Steps-Only", "1.2.3")
    assert E._norm(E.steps_python(E.step_path(root))) == E._norm(python)     # Windows: which() says python.EXE
    rep = execute(root, python_plan(root), "a")
    assert rep["status"] == "passed", rep
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)
    env = rep["environment"]
    assert Path(env["executable"]).parent.resolve() == python.parent.resolve(), "the project's venv, not uv's tool"
    assert E._norm(rep["steps"][0]["runtime"]["python"]["path"]) == E._norm(python), "the Python the step ran on"
    assert env["python"] == version_of(str(python))
    assert env["package_versions"] == {"steps-only": "1.2.3"}, "the venv's packages, not testbench's own"
    assert env["packages"] == 1 and env["packages_sha256"] == hashlib.sha256(b"steps-only==1.2.3").hexdigest()
    assert env["testbench"] == {"tool": "uv tool", "python": platform.python_version(), "executable": sys.executable}
    assert "note" not in env
    described = REPORT_SCHEMA["properties"]["environment"]["properties"]
    assert set(env) <= set(described) and set(env["testbench"]) <= set(described["testbench"]["properties"])

    install_fake(python, "Steps-Only", "1.3.0")             # the project upgrades it: testbench diff says so
    d = D.compare(rep, execute(root, python_plan(root), "b"))
    assert d["environment"]["packages"]["changed"] == {"steps-only": ["1.2.3", "1.3.0"]}
    assert "steps-only 1.2.3 -> 1.3.0" in D.render(d), D.render(d)


def test_installed_like_any_package_the_record_is_testbenchs_own_python_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    root = tmp_path / "proj"
    root.mkdir()
    install_fake(make_venv(root / ".venv", ipykernel=False), "steps-only", "1.2.3")
    assert E.tool_environment() is None, "these tests run from an ordinary install of testbench"
    assert E.steps_python(E.step_path(root)) is None, "no tool environment: the record stays about testbench's Python"
    env = execute(root, python_plan(root), "a")["environment"]
    from importlib.metadata import distributions         # what environment() recorded before it asked other Pythons
    found = [((d.metadata["Name"] or "").lower(), d.version) for d in distributions() if d.metadata["Name"]]
    pkgs = sorted({f"{name}=={version}" for name, version in found})
    assert env == {"python": platform.python_version(), "implementation": platform.python_implementation(),
                   "platform": platform.platform(), "machine": platform.machine(), "executable": sys.executable,
                   "packages": len(pkgs), "packages_sha256": hashlib.sha256("\n".join(pkgs).encode()).hexdigest(),
                   "package_versions": dict(sorted(found))}, "the same fields with the same values as ever"
    assert P._ask(sys.executable, None) == (env, ""), "asked with PROBE, a Python records what it records in-process"


@pytest.mark.posix
def test_a_steps_python_that_cannot_say_leaves_testbenchs_own_record_and_a_note(tmp_path, from_uv_tool):
    root = tmp_path / "proj"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")
    broken = root / ".venv" / "bin" / "python"
    broken.write_text("#!/bin/sh\necho 'this interpreter is broken' >&2\nexit 3\n")
    broken.chmod(0o755)
    rep = execute(root, python_plan(root, "echo hi"), "a")
    pytest.importorskip("jsonschema").validate(rep, REPORT_SCHEMA)
    env = rep["environment"]
    assert env["executable"] == sys.executable and "testbench" not in env, "testbench's own record"
    assert env["note"] == (f"the Python the steps' `python` resolves to ({broken}) could not say what it has installed "
                           "(it exited with code 3: this interpreter is broken): the Python and packages recorded "
                           "here are testbench's own (uv tool), not the steps'")
    assert "note" in REPORT_SCHEMA["properties"]["environment"]["properties"]
    assert env["note"] in R.render(rep), "report.md says so too"


def test_from_a_tool_environment_check_finds_a_notebooks_imports_in_the_projects_venv(tmp_path, from_uv_tool):
    root = tmp_path / "proj"
    root.mkdir()
    python = make_venv(root / ".venv", ipykernel=True)
    (site_packages(python) / "venv_only_mod.py").write_text("VALUE = 7\n")
    step = notebook_step(root, ["import venv_only_mod\nprint('VALUE', venv_only_mod.VALUE)"])
    assert importlib.util.find_spec("venv_only_mod") is None, "testbench's own Python lacks it"
    assert CK._notebook_needs(root / "nb.ipynb", root) is None, "its kernel runs on the venv, which has it: not skipped"
    result, log = run_notebook(root, step)
    assert result["status"] == "passed" and "VALUE 7" in log, log
    make_notebook(root / "lacks.ipynb", ["import venv_lacks_mod"])
    what, how = CK._notebook_needs(root / "lacks.ipynb", root)
    assert f"({python})" in what and how.startswith(f"{python} -m pip install ") and "agent-testbench" not in how, \
        f"the venv's Python lacks it: installed there, not with uvx --with ({what}: {how})"


def tb(capsys, *argv) -> tuple[int, str]:
    """testbench in this process (so the simulated tool environment holds), as a person types it."""
    from agent_testbench import cli as CLI
    try:
        code = CLI.main(list(argv))
    except SystemExit as done:
        code = done.code
    return code or 0, capsys.readouterr().out


def test_from_a_tool_environment_a_local_session_runs_on_the_projects_venv(tmp_path, from_uv_tool, monkeypatch,
                                                                           capsys):
    root = tmp_path / "proj"
    root.mkdir()
    python = make_venv(root / ".venv", ipykernel=True)
    (site_packages(python) / "venv_only_mod.py").write_text("VALUE = 7\n")
    write_config(root, {"project": "p", "plans": {"smoke": "python -c pass"}})
    monkeypatch.chdir(root)
    code, out = tb(capsys, "session", "start", "uvs")
    try:
        assert code == 0, out
        assert f"[testbench] the kernel runs on the project's Python: {python}." in out, "the start says which Python"
        h = json.loads((root / ".testbench" / "sessions" / "uvs" / "state.json").read_text())["handles"]
        assert h["python"] == str(python) and h["tool"] == "uv tool"
        code, out = tb(capsys, "exec", "-s", "uvs", "import sys, venv_only_mod\nprint('IN', sys.prefix, venv_only_mod.VALUE)")
        assert code == 0 and "IN " in out and " 7" in out, out
        assert Path(out.split("IN ", 1)[1].split()[0]).resolve() == (root / ".venv").resolve(), "the kernel is the venv's"
        code, out = tb(capsys, "session", "shell", "-s", "uvs", 'python -c "import sys; print(sys.prefix)"')
        assert code == 0 and Path(out.strip().splitlines()[-1]).resolve() == (root / ".venv").resolve(), \
            "`python` in session shell is the project's, as in a run: step"
        with monkeypatch.context() as m:                    # what session install would run (no network here)
            seen = []
            m.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
            code, out = tb(capsys, "session", "install", "-s", "uvs", "tiny-pkg")
        assert code == 0 and seen == [[str(python), "-m", "pip", "install", "tiny-pkg"]], "into the kernel's Python"
    finally:
        tb(capsys, "session", "stop", "--all")


def test_session_install_never_puts_packages_in_a_tool_environments_own_python(tmp_path):
    from agent_testbench import session_backends as SB
    root = tmp_path / "proj"
    root.mkdir()
    state = {"name": "s", "handles": {"python": sys.executable, "tool": "uvx", "kernel_note": "the project has none"}}
    with pytest.raises(SB.SessionError) as refused:
        SB.LocalSession(root, {}, tmp_path / "s", state).install(["tiny-pkg"])
    said = str(refused.value)
    venv = str(Path(".venv") / BIN / ("python.exe" if os.name == "nt" else "python"))
    assert said.startswith(f"not installed: the session's kernel runs on testbench's own Python (uvx: {sys.executable})")
    assert f"`python -m venv .venv`, then `{CK._pip(venv, ['ipykernel', 'tiny-pkg'])}`" in said, said
    assert "`testbench session stop s`, `testbench session start s`" in said
    python = make_venv(root / ".venv", ipykernel=False)
    (python.parent.parent / "pyvenv.cfg").write_text("home = /usr\nuv = 0.11.0\n")       # a virtualenv uv made: no pip
    assert CK.pip_argv(str(python), ["x"]) == ["uv", "pip", "install", "--python", str(python), "x"]
    with pytest.raises(SB.SessionError, match=r"uv pip install --python"):
        SB.LocalSession(root, {}, tmp_path / "s", state).install(["tiny-pkg"])


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
