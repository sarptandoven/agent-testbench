"""The examples gallery: each example's free plan gives the outcome its README promises, a planted mistake is named in
the report, and `testbench demo NAME` sets each one up as a new git repository."""
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from agent_testbench import cli as CLI, config as C
from conftest import EXAMPLES, REPO, chromium_ready, cli, own_port

needs_chromium = pytest.mark.skipif(not chromium_ready(), reason="needs `playwright install chromium`")


def _docker_answers() -> bool:
    try:
        return bool(shutil.which("docker")) and subprocess.run(["docker", "info"], capture_output=True,
                                                               timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


def copy(tmp_path: Path, name: str) -> Path:
    dest = tmp_path / name
    shutil.copytree(EXAMPLES / name, dest, ignore=shutil.ignore_patterns(".testbench", "outputs", "__pycache__",
                                                                         ".pytest_cache"))
    return dest


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} is no longer in {path.name}: update this test"
    path.write_text(text.replace(old, new, 1))


def test_every_demo_is_an_example_shipped_in_the_wheel():
    pyproject = (REPO / "pyproject.toml").read_text()
    for name, demo in CLI.DEMOS.items():
        cfg = C.load(EXAMPLES / name)
        assert demo["plan"] in cfg["plans"] and cfg["plans"][demo["plan"]]["backend"] == "local", name
        assert f'"examples/{name}" = "agent_testbench/examples/{name}"' in pyproject, f"{name} is not in the wheel"
        assert f'"examples/{name}"' in pyproject.split("[tool.hatch.build.targets.sdist]")[1], f"{name} not in sdist"


def test_llm_app_proves_its_tool_loop_against_the_mock(tmp_path):
    root = copy(tmp_path, "llm-app")
    r = cli(root, "run", "mocked", "--wait", "120")
    assert r.returncode == 0 and "# PASS - plan `mocked` on local" in r.stdout, r.stdout + r.stderr
    for line in ("stdout contains 'Order A1002 has shipped with UPS and should arrive on 2026-11-04.' - found",
                 "mock anthropic tool_calls 4 - 4", "mock anthropic calls 9 - 9", "mock anthropic refused 0 - 0",
                 "survives a rate limit: mock anthropic injected 1 - 1"):
        assert line in r.stdout, line
    assert not (root / "mock_calls.jsonl").exists(), "the mock's log belongs in the step's folder"


def test_llm_app_names_the_400_a_broken_tool_loop_would_get(tmp_path):
    root = copy(tmp_path, "llm-app")
    edit(root / "support_bot.py", '"tool_use_id": block.id,', '"tool_use_id": "toolu_wrong",')
    r = cli(root, "run", "mocked", "--wait", "120")
    assert r.returncode == 1 and "# FAIL - plan `mocked`" in r.stdout
    assert "expected mock anthropic refused 0: 1" in r.stdout and "tool_use ids were found without tool_result" in r.stdout


@pytest.mark.web
@needs_chromium
def test_web_app_fills_the_form_and_checks_the_answer(tmp_path):
    root = copy(tmp_path, "web-app")
    own_port(root / "testbench.yaml", 8974)
    r = cli(root, "run", "ui", "--wait", "180")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "page shows 'Each person pays $19.17 (of $57.50 with the tip).' - found" in r.stdout
    assert "page shows 'the bill needs at least one person' - found" in r.stdout
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    assert {p.name for p in (run / "steps" / "01-split-a-bill").glob("*.png")} == {"empty-form.png", "split.png",
                                                                                   "page.png"}


@pytest.mark.web
@needs_chromium
def test_web_app_catches_a_rounding_bug(tmp_path):
    root = copy(tmp_path, "web-app")
    own_port(root / "testbench.yaml", 8974)
    edit(root / "app.py", 'each = (with_tip / people).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
         'each = (with_tip / people).quantize(Decimal("0.01"), rounding="ROUND_DOWN")')
    r = cli(root, "run", "ui", "--wait", "180")
    assert r.returncode == 1
    assert "expected page shows 'Each person pays $19.17 (of $57.50 with the tip).': not found" in r.stdout


def test_test_suite_fails_on_its_planted_bug_until_it_is_fixed(tmp_path):
    root = copy(tmp_path, "test-suite")
    r = cli(root, "run", "tests", "--wait", "120")
    assert r.returncode == 1 and "# FAIL - plan `tests` on local" in r.stdout
    assert ("expected outputs/junit.xml: failures 0: 1 (wanted == 0) of 5 tests; failed: "
            "tests.test_textstats.test_top_words_are_the_most_common") in r.stdout, r.stdout
    edit(root / "textstats" / "__init__.py", "key=lambda kv: (kv[1], kv[0])", "key=lambda kv: (-kv[1], kv[0])")
    r = cli(root, "run", "tests", "--wait", "120")
    assert r.returncode == 0 and "outputs/junit.xml: failures 0 - 0 (wanted == 0) of 5 tests" in r.stdout, r.stdout


@pytest.mark.posix
@pytest.mark.serial                                      # Docker
@pytest.mark.skipif(not _docker_answers(), reason="needs a Docker daemon that answers `docker info`")
def test_test_suite_sandbox_runs_the_suite_offline_in_docker(tmp_path):
    root = copy(tmp_path, "test-suite")
    cfg = yaml.safe_load((root / "testbench.yaml").read_text())
    cfg["docker"]["image"] = os.environ.get("TESTBENCH_TEST_DOCKER_IMAGE", "mirror.gcr.io/library/python:3.12-slim")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")       # what building needs on this machine
    if proxy and urlparse(proxy).hostname in ("127.0.0.1", "localhost", "::1"):
        cfg["docker"]["build_args"] = ["--network", "host"]
    if os.environ.get("PIP_CERT") and Path(os.environ["PIP_CERT"]).is_file():
        cfg["docker"]["ca_bundle"] = os.environ["PIP_CERT"]
    (root / "testbench.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    r = cli(root, "run", "sandbox", "--wait", "600")
    assert r.returncode == 1 and "# FAIL - plan `sandbox` on docker" in r.stdout, r.stdout + r.stderr
    assert "offline: stdout contains 'Network is unreachable' - found" in r.stdout
    assert "failed: tests.test_textstats.test_top_words_are_the_most_common" in r.stdout


def test_demo_lists_the_examples(tmp_path):
    r = cli(tmp_path, "demo", "--list")
    assert r.returncode == 0 and "testbench demo NAME [DIR]" in r.stdout
    assert all(f"  {name} " in r.stdout for name in CLI.DEMOS), r.stdout


def test_demo_llm_app_end_to_end(tmp_path):
    r = cli(tmp_path, "demo", "llm-app")
    target = tmp_path / "llm-app"
    assert r.returncode == 0 and "Copied the llm-app example to" in r.stdout, r.stdout + r.stderr
    assert "# PASS - plan `mocked` on local" in r.stdout and "testbench run mocked" in r.stdout
    log = subprocess.run(["git", "log", "--oneline"], cwd=target, capture_output=True, text=True).stdout
    status = subprocess.run(["git", "status", "--porcelain"], cwd=target, capture_output=True, text=True).stdout
    assert "llm-app demo" in log and status.split() in ([], ["??", ".testbench/"]), f"the run's files: {status}"
    assert cli(tmp_path, "demo", "llm-app").returncode == 2, "never into a folder that is not empty"


def test_demo_test_suite_fails_on_purpose_and_says_what_to_do(tmp_path):
    r = cli(tmp_path, "demo", "test-suite", "fix-me")
    assert r.returncode == 0, "the plan's failure is the demo working"
    assert "# FAIL - plan `tests` on local" in r.stdout and "The plan fails: that is the point" in r.stdout
    assert "ask" in r.stdout and str(tmp_path / "fix-me") in r.stdout


def test_demo_keeps_its_old_arguments(tmp_path):
    r = cli(tmp_path, "demo", "old-style", "--no-run")       # `testbench demo DIR` is still tiny-model
    assert r.returncode == 0 and "Copied the tiny-model example" in r.stdout
    assert (tmp_path / "old-style" / "tiny_model.ipynb").is_file()
    r = cli(tmp_path, "demo", "nope", "somewhere")
    assert r.returncode == 2 and "there is no nope example" in r.stdout and not (tmp_path / "somewhere").exists()


def test_demo_says_what_a_missing_package_needs(tmp_path, monkeypatch, capsys):
    import importlib.util as iu
    real = iu.find_spec
    monkeypatch.setattr(iu, "find_spec", lambda mod, *a: None if mod == "playwright" else real(mod, *a))
    monkeypatch.chdir(tmp_path)
    assert CLI.cmd_demo(CLI.build_parser().parse_args(["demo", "web-app"])) == 2
    out = capsys.readouterr().out
    assert 'pip install "agent-testbench[web]" && playwright install chromium' in out
    assert "`testbench demo web-app` again" in out and not (tmp_path / "web-app").exists()
