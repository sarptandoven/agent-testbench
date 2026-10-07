"""End to end through the CLI, the way an agent uses it: run, fail, fix, compare, cancel, guard, MCP."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from conftest import REPO, has_sklearn, cli, make_notebook, write_config


def latest_run(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir())[-1]


def test_pass_fail_fix_compare(plain):
    r = cli(plain, "run", "quick", "--wait", "120")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# PASS - plan `quick` on local" in r.stdout and "out/result.json: squares.-1 == 4" in r.stdout
    first = latest_run(plain)
    assert (first / "steps" / "01-notebook" / "artifacts" / "out" / "result.json").exists()
    assert json.loads((first / "report.json").read_text())["status"] == "passed"

    nb = plain / "nb.ipynb"                                   # break it: a bug in the last cell
    nb.write_text(nb.read_text().replace("for i in range(N)", "for i in range(N) if undefined_name"))
    r = cli(plain, "run", "quick", "--wait", "120")
    assert r.returncode == 1
    assert "## FAIL: notebook" in r.stdout and "NameError" in r.stdout and "cell 3" in r.stdout
    assert "Broke: notebook" in r.stdout, "the comparison must say which step the change broke"

    nb.write_text(nb.read_text().replace(" if undefined_name", ""))
    r = cli(plain, "run", "quick", "--wait", "120")
    assert r.returncode == 0 and "Fixed: notebook" in r.stdout

    runs = cli(plain, "runs").stdout.strip().splitlines()
    assert len(runs) == 3 and "passed" in runs[-1] and "failed" in runs[1]
    assert cli(plain, "report", "--json").stdout.strip().startswith("{")
    spend = cli(plain, "spend").stdout
    assert "3 finished run(s)" in spend


def test_params_are_overridden_per_plan(plain):
    cfg = yaml.safe_load((plain / "testbench.yaml").read_text())
    cfg["plans"]["quick"]["steps"][0]["params"] = {"N": 5}
    cfg["plans"]["quick"]["steps"][0]["expect"] = {"json": {"out/result.json": {"n": "== 5", "squares.-1": "== 16"}}}
    write_config(plain, cfg)
    r = cli(plain, "run", "quick", "--wait", "120")
    assert r.returncode == 0, r.stdout
    cfg["plans"]["quick"]["steps"][0]["params"] = {"M": 5}
    write_config(plain, cfg)
    r = cli(plain, "run", "quick", "--wait", "120")
    assert r.returncode == 1 and "ParamError" in r.stdout and "N, NAME" in r.stdout, "a typo in params must fail loudly"


def test_cancel_stops_the_work(tmp_path):
    root = tmp_path / "slow"
    root.mkdir()
    write_config(root, {"project": "slow", "plans": {"long": {"steps": [
        {"name": "sleep", "run": "echo started; sleep 120; echo never", "max_minutes": 5}]}}})
    r = cli(root, "run", "long", "--detach")
    assert r.returncode == 3
    run = latest_run(root)
    for _ in range(50):
        if (run / "steps").exists() and "started" in (next(run.glob("steps/*/log.txt")).read_text() if list(run.glob("steps/*/log.txt")) else ""):
            break
        time.sleep(0.2)
    began = time.time()
    r = cli(root, "cancel")
    assert "cancelled" in r.stdout and time.time() - began < 30
    state = json.loads((run / "state.json").read_text())
    assert state["status"] == "cancelled"
    out = subprocess.run(["pgrep", "-f", "sleep 120"], capture_output=True, text=True).stdout.strip()
    assert not out, "the step's process must be gone after cancel"
    assert "CANCELLED" in (run / "report.md").read_text()


def test_timeouts_are_enforced(tmp_path):
    root = tmp_path / "t"
    root.mkdir()
    write_config(root, {"project": "t", "plans": {"p": {"steps": [
        {"name": "too slow", "run": "sleep 60", "max_minutes": 0.05}, {"name": "never runs", "run": "true"}]}}})
    began = time.time()
    r = cli(root, "run", "p", "--wait", "60")
    assert r.returncode == 1 and time.time() - began < 40
    assert "TIMEOUT: too slow" in r.stdout and "| SKIPPED | never runs |" in r.stdout


def test_refused_and_approval(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    write_config(root, {"project": "b", "budget": {"max_run_usd": 1.0, "ask_above_usd": 0.05, "max_day_usd": 5},
                        "plans": {"gpu": {"backend": "modal", "gpu": "A100-80GB", "steps": [{"name": "x", "run": "true", "max_minutes": 5}]},
                                  "huge": {"backend": "modal", "gpu": "H100", "steps": [{"name": "x", "run": "true", "max_minutes": 600}]}}})
    r = cli(root, "run", "gpu")
    assert r.returncode == 2 and "Needs approval" in r.stdout and "--approve" in r.stdout
    r = cli(root, "run", "huge", "--approve")
    assert r.returncode == 2 and "Refused" in r.stdout and "max_run_usd" in r.stdout
    assert not (root / ".testbench" / "runs").exists(), "a refused run must leave nothing running"
    est = json.loads(cli(root, "estimate", "gpu", "--json").stdout)
    assert est["allowed"] is False and est["gpu"] == "A100-80GB"


def test_init_on_a_fresh_project(tmp_path):
    make_notebook(tmp_path / "proj" / "train.ipynb", ["EPOCHS = 1 #@param", "print(EPOCHS)"])
    root = tmp_path / "proj"
    r = cli(root, "init")
    assert r.returncode == 0, r.stderr
    cfg = yaml.safe_load((root / "testbench.yaml").read_text())
    assert cfg["project"] == "proj" and "train-local" in cfg["plans"]
    assert "<!-- agent-testbench -->" in (root / "AGENTS.md").read_text() and "AGENTS.md" in (root / "CLAUDE.md").read_text()
    assert cli(root, "init").returncode == 2, "init must not overwrite an existing testbench.yaml"
    r = cli(root, "run", "train-local", "--wait", "120")
    assert r.returncode == 0, r.stdout


@pytest.mark.skipif(not has_sklearn(), reason="the example needs scikit-learn and matplotlib")
def test_example_smoke_plan(tiny):
    r = cli(tiny, "run", "smoke", "--wait", "300")
    assert r.returncode == 0, r.stdout + r.stderr
    rep = json.loads((latest_run(tiny) / "report.json").read_text())
    train = rep["steps"][1]
    assert train["images"] == ["cell04_image1.png"] and train["downloads"] == ["metrics.json"]
    acc = json.loads((tiny / "outputs" / "metrics.json").read_text())["accuracy"]
    assert acc >= 0.85


# ---- guard hook ----------------------------------------------------------------------------------------------

def guard(cwd: Path, command: str = None, tool: str = "Bash", tool_input: dict | None = None) -> dict | None:
    payload = {"session_id": "t", "cwd": str(cwd), "hook_event_name": "PreToolUse", "tool_name": tool,
               "tool_input": tool_input if tool_input is not None else {"command": command}}
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


def test_guard_decisions(plain, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert guard(outside, "modal deploy app.py") is None, "outside a testbench project the guard stays silent"
    assert guard(plain, "python -m pytest -q") is None
    assert guard(plain, "testbench run quick") is None
    for cmd in ("modal deploy app.py", "modal run --detach train.py", "python -m modal run x.py", "cd x && colab new --gpu A100"):
        d = guard(plain, cmd)
        assert d and d["permissionDecision"] == "deny" and "testbench run" in d["permissionDecisionReason"], cmd
    for cmd in ("cat .env", "cat config/.env.local", "printenv", "echo $OPENAI_API_KEY", "env",
                "modal secret create fal FAL_KEY=abc", "cat ~/.modal.toml"):
        d = guard(plain, cmd)
        assert d and d["permissionDecision"] == "deny" and "secret" in d["permissionDecisionReason"], cmd
    d = guard(plain, "testbench run quick --approve")
    assert d["permissionDecision"] == "ask"
    for cmd in ("git push origin main", "git add . && git push", "gh repo create x --public"):
        assert guard(plain, cmd)["permissionDecision"] == "ask", cmd
    assert guard(plain, "git status") is None
    d = guard(plain, tool="mcp__plugin_agent-testbench__run_plan", tool_input={"plan": "quick", "approved_by_person": True})
    assert d["permissionDecision"] == "ask"
    assert guard(plain, tool="mcp__x__run_plan", tool_input={"plan": "quick"}) is None


def test_guard_never_blocks_on_its_own_failure(plain):
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], input="not json", capture_output=True,
                       text=True, timeout=30)
    assert p.returncode == 0 and not p.stdout.strip()


# ---- MCP -----------------------------------------------------------------------------------------------------

def test_mcp_tools(plain):
    from agent_testbench import mcp_server

    async def go():
        tools = await mcp_server.server.list_tools()
        names = {t.name for t in tools}
        assert {"plans", "run_plan", "wait_run", "get_report", "list_runs", "cancel_run", "spend", "lint_notebook",
                "add_note"} <= names
        out = await mcp_server.server.call_tool("run_plan", {"plan": "quick", "project_dir": str(plain), "wait_seconds": 120})
        return out

    out = asyncio.run(go())
    text = json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
    assert "PASS - plan `quick`" in text and "[exit 0]" in text


def test_mcp_server_starts_over_stdio(plain):
    """The server answers an MCP initialize over stdio, as a client like Claude Desktop starts it."""
    proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, cwd=plain)
    try:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
        proc.stdin.write(json.dumps(init) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        reply = json.loads(line)
        assert reply["id"] == 1 and reply["result"]["serverInfo"]["name"] == "agent-testbench"
    finally:
        proc.kill()
