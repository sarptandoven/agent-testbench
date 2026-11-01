"""Holes found by an adversarial review of the guardrails, each closed and kept closed: numbers that are not numbers
(NaN compares false with every limit), arguments that turn into options, prices from somewhere else, and the edits
and commands the guard hook did not see."""
import asyncio
from pathlib import Path

import pytest

from agent_testbench import budget, config as C, prices
from conftest import cli, write_config
from test_limits import guard, project


def _plan(root: Path, **budget_values) -> None:
    write_config(root, {"project": "p", "budget": {"max_run_usd": 2, "ask_above_usd": 0.5, "max_day_usd": 10,
                                                    **budget_values},
                        "plans": {"big": {"backend": "modal", "gpu": "H100:8", "steps": [
                            {"name": "a", "run": "true", "max_minutes": 23}]}}})


@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf", "true"])
def test_a_budget_that_is_not_a_number_is_refused(tmp_path, text):
    root = tmp_path / "p"
    root.mkdir()
    _plan(root)
    yml = root / "testbench.yaml"
    yml.write_text(yml.read_text().replace("max_run_usd: 2", f"max_run_usd: {text}"))
    r = cli(root, "estimate", "big")
    assert r.returncode == 2 and "`budget.max_run_usd` must be a number" in r.stderr, r.stdout + r.stderr


def test_minutes_and_prices_that_are_not_numbers_are_refused(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"a": {"steps": [{"name": "s", "run": "true", "max_minutes": 1}]}}})
    yml = root / "testbench.yaml"
    base = yml.read_text()
    yml.write_text(base.replace("max_minutes: 1", "max_minutes: .nan"))
    assert "`plans.a.steps[0].max_minutes` must be a positive number" in cli(root, "plans").stderr
    yml.write_text(base + "ssh: {host: box, usd_per_hour: .nan}\n")
    yml.write_text(yml.read_text().replace("steps:", "backend: ssh\n    steps:"))
    assert "`ssh.usd_per_hour`" in cli(root, "plans").stderr
    yml.write_text(base)
    for flag in ("nan", "inf", "-5", "0"):
        r = cli(root, "session", "start", "--max-minutes", flag)
        assert r.returncode == 2 and "--max-minutes" in r.stderr, (flag, r.stderr)


def test_the_check_fails_closed_on_numbers_that_are_not_numbers(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    _plan(root)
    cfg = C.load(root)
    est = budget.worst_case(cfg, "big")
    for bad in (float("nan"), float("inf"), -1.0):
        ok, msg, _ = budget.check(cfg, {**est, "worst_case_usd": bad}, True, "Lower it")
        assert not ok and "not a number of dollars" in msg, msg
    cfg["budget"]["ask_above_usd"] = float("nan")             # past validation somehow: still refused
    ok, msg, _ = budget.check(cfg, {**est, "worst_case_usd": 0.1}, False, "Lower it")
    assert not ok


def test_machine_limits_and_ledgers_that_are_not_numbers_refuse_everything(tmp_path, machine_limits):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"a": {"steps": [{"name": "s", "run": "true", "max_minutes": 1}]}}})
    machine_limits.mkdir(parents=True, exist_ok=True)
    (machine_limits / "limits.yaml").write_text("max_run_usd: .nan\n")
    r = cli(root, "run", "a")
    assert r.returncode == 2 and "must be a number of US dollars; nothing can launch" in r.stdout + r.stderr
    (machine_limits / "limits.yaml").write_text("max_run_usd: 5\nmax_day_usd: 20\n")
    (machine_limits / "ledger.jsonl").write_text('{"time": "2026-01-01T00:00:00", "event": "finish", "run": "x", '
                                                 '"usd": NaN}\n')
    r = cli(root, "run", "a")
    assert r.returncode == 2 and "line 1 is not a ledger entry testbench wrote" in r.stdout + r.stderr
    (machine_limits / "ledger.jsonl").write_text("not json\n")
    assert "line 1 is not a ledger entry" in cli(root, "run", "a").stderr


def test_a_gpu_is_a_name_or_name_and_count(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    for gpu, ok in (("H100:8", True), ("H100", True), ("H100: 8", False), ("H100:8x", False), ("H100:0", False)):
        write_config(root, {"project": "p", "plans": {"a": {"backend": "modal", "gpu": gpu, "steps": [
            {"name": "s", "run": "true"}]}}})
        r = cli(root, "plans")
        assert (r.returncode == 0) == ok, (gpu, r.stdout + r.stderr)
        if not ok:
            assert "NAME:COUNT" in r.stderr


def test_options_are_spelled_out(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"a": {"steps": [{"name": "s", "run": "true"}]}}})
    r = cli(root, "run", "a", "--appr")
    assert r.returncode == 2 and "unrecognized arguments: --appr" in r.stderr, "an abbreviation must not approve"


def test_a_catalog_cannot_add_cheap_hardware():
    built = {"T4": 0.0001, "H100": 0.001}
    out = prices.raised(built, {"H100!": 0.0, "X9": 0.005, "T4": float("nan"), "H100": float("inf")})
    assert out["H100!"] == 0.001 and out["X9"] == 0.005, "a new name costs at least the dearest known hardware"
    assert out["T4"] == 0.0001 and out["H100"] == 0.001, "NaN and infinity change nothing"


def test_mcp_names_never_become_options(tmp_path, monkeypatch):
    from agent_testbench import mcp_server as M
    with pytest.raises(ValueError, match="not a name testbench uses"):
        M._name("--approve", "session")
    assert M._name("20261031-064601-tests", "run") and M._name("", "run") == ""
    seen = []

    async def fake_cli(project_dir, argv, on_line=None):
        seen.append(argv)
        return 0, "ok"
    monkeypatch.setattr(M, "_cli", fake_cli)
    asyncio.run(M.add_note("--approve", project_dir=str(tmp_path)))
    asyncio.run(M.loop_try("-x", project_dir=str(tmp_path)))
    asyncio.run(M.session_shell("--approve", project_dir=str(tmp_path)))
    for argv in seen:
        assert argv[argv.index("--") + 1].startswith("-"), argv
    with pytest.raises(ValueError):
        asyncio.run(M.session_start(name="--approve", project_dir=str(tmp_path)))
    asyncio.run(M.session_start(name="box", gpu="--approve", project_dir=str(tmp_path)))
    assert "--gpu=--approve" in seen[-1], "an option's value stays its value"


def test_the_hook_sees_numbers_prices_and_sandboxes(tmp_path):
    root = tmp_path / "p"
    project(root, max_run_usd=2.0)
    yml = root / "testbench.yaml"
    text = yml.read_text()
    write = lambda content: guard({"cwd": str(root), "tool_name": "Write",
                                   "tool_input": {"file_path": str(yml), "content": content}})
    assert write(text.replace("max_run_usd: 2.0", "max_run_usd: .nan")) == "ask"
    with_ssh = text + "ssh: {host: box, usd_per_hour: 3.0}\n"
    yml.write_text(with_ssh)
    assert write(with_ssh.replace("usd_per_hour: 3.0", "usd_per_hour: 0")) == "ask"
    assert write(with_ssh.replace("usd_per_hour: 3.0", "usd_per_hour: 4.0")) is None, "a higher price is fine"
    assert write(text) == "ask", "removing a price falls back to a lower default"
    boxed = text.replace("backend: modal", "backend: docker", 1) + "docker: {network: none}\n"
    yml.write_text(boxed)
    assert write(boxed.replace("network: none", "network: bridge")) == "ask"
    assert write(boxed.replace("docker: {network: none}", "docker: {network: none, run_args: [-v, '/:/host']}")) == "ask"
    assert write(boxed.replace("backend: docker", "backend: local")) == "ask", "a plan leaving the sandbox"
    assert write(boxed.replace("backend: docker", "backend: modal")) == "ask"
    assert write(boxed.replace("max_run_usd: 2.0", "max_run_usd: 1.0")) is None


def test_the_hook_asks_about_approvals_anywhere(tmp_path, machine_limits):
    root = tmp_path / "p"
    project(root)
    elsewhere = tmp_path / "no-project"
    elsewhere.mkdir()
    bash = lambda cmd, cwd=root: guard({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": cmd}})
    assert bash("testbench run --approve big") == "ask", "the flag before the plan"
    assert bash("testbench run big --approve") == "ask"
    assert bash(f"cd {root} && testbench run big --approve", cwd=elsewhere) == "ask", "outside a project too"
    assert bash("F=--approve; testbench run big $F") == "ask", "a flag in a variable"
    assert bash("python -m agent_testbench session start --approve --backend modal") == "ask"
    assert bash("testbench loop start big --metric m:k --budget-usd 9 --approve") == "ask"
    assert bash("testbench prices --update --url file:///tmp/cheap.json") == "ask"
    assert bash("testbench prices --update") is None
    assert bash("testbench run big") is None and bash("testbench estimate big --approve") is None
    mcp = {"tool_name": "mcp__testbench__run_plan", "tool_input": {"plan": "big", "approved_by_person": True,
                                                                   "project_dir": str(root)}}
    assert guard({**mcp, "cwd": str(elsewhere)}) == "ask", "an MCP approval outside a project asks too"
    assert guard({"cwd": str(root), "tool_name": "Write", "tool_input": {
        "file_path": str(machine_limits / "prices.json"), "content": "{}"}}) == "ask"
    assert bash(f"echo '{{}}' > {machine_limits}/prices.json") == "ask"


def test_a_watchdog_with_no_real_limit_stops_at_once(tmp_path, monkeypatch):
    from agent_testbench.kernelkit import watchdog as W
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.05")
    (tmp_path / "watchdog_stopped").mkdir()                    # what the work could do to break it
    assert "max_minutes" in W.watch(tmp_path, float("nan"))
    assert "max_minutes" in W.watch(tmp_path, -1.0, idle_minutes=float("nan"))
