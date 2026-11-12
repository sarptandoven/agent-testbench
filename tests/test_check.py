"""testbench check: a first proven result with no testbench.yaml, in well under a minute - and, in a project that has
one, every free plan run while nothing that costs money starts."""
import asyncio
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from agent_testbench import api, check as CK, config as C, executor
from conftest import cli, git, make_notebook, write_config

RUN_ID = re.compile(r"\b\d{8}-\d{6}-[\w-]+")


def pytest_repo(root: Path, failing: bool = False) -> Path:
    """A small git repository with a pytest suite and no testbench.yaml."""
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
        + ("\n\ndef test_add_negative():\n    assert add(-2, -2) == 4, 'negatives'\n" if failing else ""))
    (root / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['.']\n")
    if shutil.which("git"):
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    return root


def ledger(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_check_proves_a_pytest_repo_with_no_config_in_under_a_minute(tmp_path, machine_limits):
    root = pytest_repo(tmp_path / "calc")
    began = time.time()
    r = cli(root, "check", timeout=120)
    took = time.time() - began
    assert r.returncode == 0, r.stdout + r.stderr
    assert took < 60, f"testbench check took {took:.0f} s"
    assert re.search(r"(?m)^tests: a pytest suite .* PASS \(\d+\.\d s\)$", r.stdout), r.stdout
    run_id = RUN_ID.search(r.stdout).group(0)
    assert f"`testbench verify {run_id}`" in r.stdout and "1 plan: 1 passed, 0 failed" in r.stdout
    assert r.stdout.rstrip().splitlines()[-1].startswith("Next: `testbench init"), r.stdout
    for name in ("testbench.yaml", "AGENTS.md", "CLAUDE.md"):
        assert not (root / name).exists(), f"check wrote {name}"
    assert not (machine_limits / "limits.yaml").exists(), "check wrote ~/.testbench/limits.yaml"
    record = json.loads((root / C.CHECKED).read_text())
    assert record["by"] == "testbench check" and list(record["found"]) == ["tests"]
    assert [e["run"] for e in ledger(root / ".testbench" / "ledger.jsonl") if e["event"] == "launch"] == [run_id]
    # an ordinary run with a full proof record: the commands that read runs work here, with no testbench.yaml
    v = cli(root, "verify", run_id)
    assert v.returncode == 0 and "evidence files match their recorded sha256" in v.stdout, v.stdout
    assert "ran on commit" in v.stdout or not shutil.which("git"), v.stdout
    rep = cli(root, "report")
    assert rep.returncode == 0 and "# PASS - plan `tests`" in rep.stdout, rep.stdout + rep.stderr
    assert run_id in cli(root, "runs").stdout
    assert cli(root, "ui").returncode == 0 and (root / ".testbench" / "ui" / "index.html").is_file()
    assert "first finished run of plan tests" in cli(root, "diff").stderr + cli(root, "diff").stdout
    # init keeps the plan's name, so the history goes on from the run check made
    assert cli(root, "init").returncode == 0 and (root / "testbench.yaml").is_file()
    h = cli(root, "history", "tests", "--json")
    assert h.returncode == 0 and [row["run_id"] for row in json.loads(h.stdout)["runs"]] == [run_id], h.stdout


def test_a_failing_test_fails_the_check_and_its_report_names_it(tmp_path):
    root = pytest_repo(tmp_path / "calc", failing=True)
    r = cli(root, "check", timeout=120)
    assert r.returncode == 1, r.stdout + r.stderr
    assert re.search(r"(?m)^tests: a pytest suite .* FAIL \(", r.stdout), r.stdout
    assert "# FAIL - plan `tests`" in r.stdout and "test_add_negative" in r.stdout, "the report, as `run` prints it"
    run_id = RUN_ID.search(r.stdout).group(0)
    assert f"Next: fix what the report of tests names (`testbench report {run_id}`" in r.stdout
    assert "1 plan: 0 passed, 1 failed" in r.stdout


def test_an_empty_folder_has_nothing_to_check_and_is_told_what_to_do(tmp_path):
    r = cli(tmp_path, "check")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "Nothing to check in" in r.stdout and "a test suite (pytest" in r.stdout
    assert "Next: `testbench init`" in r.stdout and "testbench recipes" in r.stdout
    assert not (tmp_path / ".testbench").exists(), "nothing ran, so nothing is written"
    j = json.loads(cli(tmp_path, "check", "--json").stdout)
    assert j["exit_code"] == 2 and j["plans"] == [] and j["message"].startswith("Nothing to check")


def test_with_a_config_free_plans_run_and_paid_ones_are_only_priced(tmp_path, machine_limits):
    root = tmp_path / "proj"
    root.mkdir()
    write_config(root, {"project": "proj", "plans": {
        "quick": {"backend": "local", "steps": [{"name": "hello", "run": "python -c \"print('hi')\"",
                                                 "max_minutes": 1}]},
        "gpu-check": {"backend": "modal", "gpu": "T4", "description": "the notebook on a T4",
                      "steps": [{"name": "train", "run": "python train.py", "max_minutes": 20}]},
        "full": {"backend": "modal", "gpu": "H100", "steps": [{"name": "train", "run": "python train.py",
                                                               "max_minutes": 600}]}}})
    r = cli(root, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    assert re.search(r"(?m)^quick: local: hello \.\.\. PASS", r.stdout), r.stdout
    assert re.search(r"(?m)^gpu-check: the notebook on a T4 \.\.\. not run: it can cost up to \$\d+\.\d\d on modal T4; "
                     r"a person runs it with `testbench run gpu-check`$", r.stdout), r.stdout
    assert re.search(r"(?m)^full: .* not run: it can cost up to \$\d+\.\d\d on modal H100, over the budget; a person "
                     r"runs it with `testbench run full`$", r.stdout), r.stdout
    assert "3 plans: 1 passed, 0 failed, 2 not run" in r.stdout and "Next: `testbench ui --open`" in r.stdout
    launched = [e["plan"] for e in ledger(root / ".testbench" / "ledger.jsonl") if e["event"] == "launch"]
    assert launched == ["quick"], "a plan that costs money is priced, never launched"
    assert [e["plan"] for e in ledger(machine_limits / "ledger.jsonl") if e["event"] == "launch"] == ["quick"]
    assert not (root / C.CHECKED).exists(), "with a testbench.yaml, check keeps no record of its own"
    # --plan picks plans by name, by the same rules: a paid plan alone runs nothing (exit 2)
    j = json.loads(cli(root, "check", "--plan", "gpu-check", "--json").stdout)
    assert j["exit_code"] == 2 and [(e["plan"], e["status"], e["command"]) for e in j["plans"]] == \
        [("gpu-check", "not run", "testbench run gpu-check")]
    assert j["plans"][0]["worst_case_usd"] > 0 and j["runs"] == []
    assert "testbench estimate gpu-check" in j["next"]
    bad = cli(root, "check", "--plan", "quik")
    assert bad.returncode == 2 and "No plan 'quik' (did you mean quick?)" in bad.stdout
    assert [e["plan"] for e in ledger(root / ".testbench" / "ledger.jsonl") if e["event"] == "launch"] == ["quick"]


def test_a_plan_whose_tool_is_missing_is_skipped_with_the_command_that_installs_it(tmp_path):
    root = pytest_repo(tmp_path / "mixed")
    (root / "Cargo.toml").write_text('[package]\nname = "mixed"\nversion = "0.1.0"\n')
    bare = tmp_path / "bin"                              # a PATH with no cargo or cargo-nextest on it
    bare.mkdir()
    env = {"PATH": str(bare), "CARGO_HOME": str(tmp_path / "no-cargo")}
    r = cli(root, "check", env=env, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr       # the pytest plan ran and passed; the other one is skipped
    assert re.search(r"(?m)^tests: .* PASS", r.stdout), r.stdout
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("cargo-tests:"))
    assert " skipped: " in line and "Install: `" in line, line
    j = json.loads(cli(root, "check", "--json", env=env, timeout=120).stdout)
    cargo = next(e for e in j["plans"] if e["plan"] == "cargo-tests")
    assert cargo["status"] == "skipped" and cargo["run"] is None and cargo["install"], cargo
    assert any(w in cargo["install"].lower() for w in ("cargo", "rustup", "nextest")), cargo
    assert not any("cargo-tests" in p.name for p in (root / ".testbench" / "runs").iterdir())


def test_a_notebook_whose_packages_are_missing_is_skipped_with_the_command_that_installs_them(tmp_path):
    root = tmp_path / "nbproj"
    make_notebook(root / "explore.ipynb", ["import json", "import surely_not_installed_pkg_7f3", "print('hi')"])
    r = cli(root, "check", timeout=120)
    assert r.returncode == 2, r.stdout + r.stderr       # nothing could run
    line = next(ln for ln in r.stdout.splitlines() if ln.startswith("explore-local:"))
    assert "skipped: explore.ipynb imports surely_not_installed_pkg_7f3, which the Python its kernel runs" in line
    assert "is a guess" in line and "pip install surely-not-installed-pkg-7f3`" in line, line
    assert "Next: install what explore-local needs:" in r.stdout
    assert not (root / ".testbench").exists(), "nothing ran, so nothing is written"


def test_a_web_plan_needs_chromium_and_npm_plans_need_their_packages(tmp_path, monkeypatch):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"scripts": {"test": "vitest run", "dev": "vite"}, '
                                       '"devDependencies": {"vitest": "^2", "jest-junit": "^17"}}')
    plan = {"backend": "local", "steps": [{"kind": "run", "run": "npx --no -- vitest run --reporter=junit"}]}
    if shutil.which("npx", path=executor.step_path(root)):
        assert CK.needs(plan, root) == ("the project's npm packages are not installed (no node_modules folder)",
                                        "npm install")
        (root / "node_modules" / ".bin").mkdir(parents=True)
        assert CK.needs(plan, root) is None
        jest = {"backend": "local", "steps": [{"kind": "run", "run": "npx --no -- jest --ci --reporters=jest-junit"}]}
        assert CK.needs(jest, root)[1] == "npm install --save-dev jest-junit@17.0.0"
    web = {"backend": "local", "steps": [{"kind": "web", "web": {"serve": None, "url": "http://127.0.0.1:1/"}}]}
    monkeypatch.setattr("agent_testbench.web.chromium_check", lambda: (False, "Chromium is not installed (x is "
                        "missing). Install it with: python -m playwright install chromium"))
    if CK.importlib.util.find_spec("playwright") is not None:
        assert CK.needs(web, root) == ("Chromium is not installed (x is missing)",
                                       "python -m playwright install chromium")


def test_without_config_or_record_commands_name_check_and_init(tmp_path):
    for args in (["runs"], ["report"], ["plans"]):
        r = cli(tmp_path, *args)
        assert r.returncode == 2 and "No testbench.yaml here or in any parent of" in r.stderr, r.stderr
        assert "`testbench check`" in r.stderr and "`testbench init`" in r.stderr, r.stderr


def test_the_record_check_keeps_cannot_set_a_budget_or_a_paid_machine(tmp_path):
    root = pytest_repo(tmp_path / "calc")
    assert cli(root, "check").returncode == 0
    sub = root / "tests"
    assert C.find_root(sub) == root, "a folder below one check ran in belongs to it, as below a testbench.yaml"
    below = json.loads(cli(sub, "check", "--json").stdout)
    assert below["root"] == str(root) and below["passed"] == 1 and not (sub / ".testbench").exists(), \
        "check from a folder below checks the project, as every other command there sees it"
    record = json.loads((root / C.CHECKED).read_text())
    cfg = C.load(root)
    assert cfg["budget"] == C.DEFAULTS["budget"] and list(cfg["plans"]) == ["tests"]
    record.update(budget={"max_run_usd": 1000}, plans=record["plans"] + "  gpu:\n    backend: modal\n    gpu: H100\n"
                  "    steps:\n      - name: x\n        run: python x.py\n")
    (root / C.CHECKED).write_text(json.dumps(record))
    r = cli(root, "plans")
    assert r.returncode == 2 and "plan gpu (modal) is not on this machine" in r.stderr, r.stderr
    (root / C.CHECKED).write_text("{not json")
    r = cli(root, "report")
    assert r.returncode == 2 and "is not the record `testbench check` writes" in r.stderr and \
        "run `testbench check` there again" in r.stderr, r.stderr
    assert cli(root, "check").returncode == 0, "check looks anew and writes the record again"
    assert C.load(root)["budget"] == C.DEFAULTS["budget"]


def test_check_refuses_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    pytest_repo(tmp_path / "x")
    s = CK.run(tmp_path)
    assert s["exit_code"] == 2 and "is your home folder" in s["message"] and not (tmp_path / ".testbench").exists()


def test_check_from_python_and_as_an_mcp_tool(tmp_path):
    root = pytest_repo(tmp_path / "calc")
    with pytest.raises(api.TestbenchError, match="No testbench.yaml"):
        api.Testbench(root)
    lines = []
    s = api.check(root, on_line=lines.append)
    assert s["exit_code"] == 0 and s["found_by_check"] and len(s["runs"]) == 1, s
    assert lines and lines[0].startswith("tests: a pytest suite") and "PASS" in lines[0]
    tb = api.Testbench(root)                              # the folder is a project now
    assert tb.verify(s["runs"][0])["ok"] and tb.check(plans=["tests"])["passed"] == 1
    with pytest.raises(api.TestbenchError, match="is not a folder"):
        api.check(tmp_path / "nope")
    pytest.importorskip("mcp")
    from agent_testbench import mcp_server, tools
    assert "check_project" in {d["name"] for d in tools.anthropic_tools()}

    async def go():
        out = await mcp_server.server.call_tool("check_project", {"project_dir": str(root), "plans": ["tests"]})
        return json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
    text = asyncio.run(go())
    assert "PASS" in text and "[exit 0]" in text, text
    with pytest.raises(Exception, match="not a name testbench uses"):
        asyncio.run(mcp_server.check_project(plans=["--approve"], project_dir=str(root)))


def test_steps_use_the_projects_python_when_testbench_runs_from_a_tool_environment(tmp_path, monkeypatch):
    own = str(Path(sys.executable).parent)
    other = str(tmp_path / "usr-bin")
    environ = {"PATH": os.pathsep.join([own, other])}
    assert executor.step_path(tmp_path, environ) == os.pathsep.join([own, own, other]), "a plain install: unchanged"
    tool = tmp_path / "uv" / "tools" / "agent-testbench"
    tool.mkdir(parents=True)
    (tool / "uv-receipt.toml").write_text("[tool]\n")
    monkeypatch.setattr(sys, "prefix", str(tool))
    assert executor.tool_environment() == "uv tool" and CK.tb() == "testbench"
    assert CK._pip(sys.executable, ["pytest"]) == "uv tool install --force --with pytest agent-testbench"
    assert executor.step_path(tmp_path, environ) == os.pathsep.join([other, own]), \
        "`python` is the one on PATH; testbench's folder last, where `testbench` is still found"
    venv = tmp_path / ".venv"
    (venv / ("Scripts" if os.name == "nt" else "bin")).mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n")
    first = executor.step_path(tmp_path, environ).split(os.pathsep)[0]
    assert first == str(venv / ("Scripts" if os.name == "nt" else "bin")), "the project's own venv comes first"
    activated = {**environ, "VIRTUAL_ENV": str(tmp_path / "elsewhere")}       # its bin folder is first on PATH then
    assert executor.step_path(tmp_path, activated) == os.pathsep.join([other, own]), "an activated virtualenv wins"
    cache = tmp_path / "cache" / "archive-v0" / "Ab12"
    cache.mkdir(parents=True)
    monkeypatch.setattr(sys, "prefix", str(cache))
    assert executor.tool_environment() == "uvx", "an environment in uv's cache"
    assert CK.tb() == "uvx agent-testbench", "uvx puts no `testbench` on PATH: what check prints says so"
    assert CK._pip(sys.executable, ["scikit-learn", "matplotlib"]) == \
        "uvx --with scikit-learn --with matplotlib agent-testbench check"
