"""Live sessions on this machine with real kernels: state that persists, errors, timeouts, images, notebook cells,
files, auto-stop, budgets, the guard, and the MCP tools. (Modal and Colab sessions: tests/test_live.py.)"""
import asyncio
import json
import os
import subprocess
import sys
import time

import pytest
import yaml

from conftest import REPO, cli, write_config


def state(root, name="main"):
    return json.loads((root / ".testbench" / "sessions" / name / "state.json").read_text())


@pytest.fixture
def started(plain):
    r = cli(plain, "session", "start")
    assert r.returncode == 0, r.stdout + r.stderr
    yield plain
    cli(plain, "session", "stop", "--all")


def test_state_persists_like_notebook_cells(started):
    root = started
    assert cli(root, "exec", "total = 40").returncode == 0
    r = cli(root, "exec", "total += 2\nprint('total is', total)\ntotal * 10")
    assert r.returncode == 0 and "total is 42" in r.stdout and "=> 420" in r.stdout
    r = cli(root, "exec", "--json", "import os\nos.getcwd()")
    res = json.loads(r.stdout)[0]
    assert res["status"] == "ok" and str(root) in res["result"], "a local session runs in the project folder"


def test_errors_name_the_line_and_keep_the_kernel(started):
    root = started
    cli(root, "exec", "keep = 'me'")
    r = cli(root, "exec", "x = 1\ny = {}['missing']\nprint('never')")
    assert r.returncode == 1 and "ERROR KeyError" in r.stdout and "line 2" in r.stdout
    r = cli(root, "exec", "keep")
    assert r.returncode == 0 and "=> 'me'" in r.stdout


def test_timeout_interrupts_and_keeps_state(started):
    root = started
    cli(root, "exec", "before = 7")
    began = time.time()
    r = cli(root, "exec", "--timeout", "2", "import time\ntime.sleep(60)")
    assert r.returncode == 3 and "TIMEOUT" in r.stdout and time.time() - began < 30
    assert "=> 7" in cli(root, "exec", "before").stdout


def test_images_and_notebook_cells_with_params(started):
    root = started
    r = cli(root, "exec", "from IPython.display import Image, display\nimport base64\n"
            "display(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')))")
    img = [ln.split("image: ")[1] for ln in r.stdout.splitlines() if ln.startswith("image: ")]
    assert len(img) == 1 and os.path.getsize(img[0]) > 0
    r = cli(root, "exec", "--cells", "nb.ipynb:1-3", "--params", '{"N": 4}')
    assert r.returncode == 0, r.stdout
    assert "[nb.ipynb cell 1 | ok" in r.stdout and "hello ada" in r.stdout and "wrote 4" in r.stdout
    assert "=> [0, 1, 4, 9]" in cli(root, "exec", "json.load(open('out/result.json'))['squares']").stdout
    r = cli(root, "exec", "--cells", "nb.ipynb:9")
    assert r.returncode == 2 and "has 3 code cells" in r.stdout


def test_files_and_shell(started, tmp_path):
    root = started
    src = tmp_path / "data.txt"
    src.write_text("payload")
    assert "copied" in cli(root, "files", "put", str(src), "incoming/data.txt").stdout
    assert "=> 'payload'" in cli(root, "exec", "open('incoming/data.txt').read()").stdout
    assert "data.txt" in cli(root, "files", "ls", "incoming").stdout
    out = tmp_path / "back.txt"
    cli(root, "files", "get", "incoming/data.txt", str(out))
    assert out.read_text() == "payload"
    assert "hello-from-shell" in cli(root, "session", "shell", "echo hello-from-shell").stdout


def test_stop_ends_the_kernel_and_records_cost(plain):
    cli(plain, "session", "start", "work")
    pid = state(plain, "work")["handles"]["launcher_pid"]
    r = cli(plain, "session", "stop", "work")
    assert "stopped after" in r.stdout
    s = state(plain, "work")
    assert s["status"] == "stopped" and s["cost_usd"] == 0
    time.sleep(1)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    r = cli(plain, "exec", "-s", "work", "1")
    assert r.returncode == 2 and "is stopped" in r.stdout
    ledger = [json.loads(x) for x in (plain / ".testbench" / "ledger.jsonl").read_text().splitlines()]
    assert [e["event"] for e in ledger] == ["launch", "finish"]


@pytest.mark.parametrize("flags, why", [(["--idle-minutes", "0.03"], "idle for"), (["--max-minutes", "0.05"], "max_minutes")])
def test_sessions_stop_themselves(plain, flags, why):
    env = {"TESTBENCH_SUPERVISE_SECONDS": "1"}
    r = cli(plain, "session", "start", *flags, env=env)
    assert r.returncode == 0, r.stdout
    for _ in range(40):
        if state(plain)["status"] == "stopped":
            break
        time.sleep(0.5)
    s = state(plain)
    assert s["status"] == "stopped" and why in s["stop_reason"], s


def test_one_name_one_running_session(started):
    r = cli(started, "session", "start")
    assert r.returncode == 2 and "already running" in r.stdout
    assert cli(started, "session", "start", "bad name").returncode == 2
    cli(started, "session", "start", "second")
    r = cli(started, "exec", "1")
    assert r.returncode == 2 and "name one with -s" in r.stdout
    assert cli(started, "exec", "-s", "second", "1").returncode == 0


def test_budget_applies_to_sessions(tmp_path):
    root = tmp_path / "b"
    root.mkdir()
    write_config(root, {"project": "b", "budget": {"max_run_usd": 1.0, "ask_above_usd": 0.05, "max_day_usd": 5}})
    r = cli(root, "session", "start", "--backend", "modal", "--gpu", "H100", "--max-minutes", "120")
    assert r.returncode == 2 and "Refused" in r.stdout and "max_run_usd" in r.stdout
    r = cli(root, "session", "start", "--backend", "modal", "--gpu", "T4", "--max-minutes", "30")
    assert r.returncode == 2 and "Needs approval" in r.stdout
    r = cli(root, "session", "start", "--backend", "local", "--gpu", "T4")
    assert r.returncode == 2 and "gpu" in r.stdout
    assert not (root / ".testbench" / "sessions").exists() or not any((root / ".testbench" / "sessions").iterdir())


def test_guard_asks_before_an_approved_session(plain):
    payload = {"cwd": str(plain), "tool_name": "Bash",
               "tool_input": {"command": "testbench session start gpu --backend modal --gpu A100-80GB --approve"}}
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    assert json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_mcp_session_tools(plain):
    from agent_testbench import mcp_server

    async def go():
        call = mcp_server.server.call_tool
        text = lambda out: json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
        a = text(await call("session_start", {"project_dir": str(plain)}))
        b = text(await call("session_exec", {"code": "v = 21 * 2\nv", "project_dir": str(plain)}))
        c = text(await call("session_exec", {"notebook": "nb.ipynb", "cells": "1-2", "params": '{"NAME": "grace"}',
                                             "project_dir": str(plain)}))
        d = text(await call("session_stop", {"project_dir": str(plain)}))
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert "ready on local" in a and "=> 42" in b and "hello grace" in c and "stopped after" in d
