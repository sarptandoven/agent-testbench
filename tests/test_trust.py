"""Claims in docs/threat-model.md that no other test proved, each proved here: a secret the code prints is kept
nowhere, no abbreviation of --approve approves, the remote hard stop does not depend on the environment a run
started from, an MCP argument never becomes an option, and a number too big for a float is no limit. (The dashboard's
paths are in test_fuzz_dashboard.py, the guard's in test_guard_hook.py and test_fuzz_guard.py.)"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

import pytest

from agent_testbench import config as C
from agent_testbench.cli import build_parser
from conftest import cli, write_config

TOKEN = f"tb-trust-{uuid.uuid4().hex}"
LEAK = ("python -c \"import os, sys; print('the token is', os.environ['TB_TRUST_TOKEN']); "
        "open('out.txt', 'w').write(os.environ['TB_TRUST_TOKEN']); sys.exit(3)\"")


def test_a_secret_the_code_prints_is_kept_nowhere(tmp_path, webhook):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "notify": {"webhook": webhook.url}, "export": {"jsonl": ".testbench/runs.jsonl"},
                        "plans": {"leak": {"steps": [{"name": "leak", "run": LEAK, "secrets": ["TB_TRUST_TOKEN"],
                                                      "artifacts": ["out.txt"], "max_minutes": 2}]}}})
    r = cli(root, "run", "leak", "--wait", "120", env={"TB_TRUST_TOKEN": TOKEN})
    assert r.returncode == 1, r.stdout + r.stderr
    [run] = (root / ".testbench" / "runs").iterdir()
    logs = [p.read_text() for p in run.glob("steps/*/log.txt")]
    assert any("the token is [secret]" in t for t in logs), logs
    kept = [p for p in (root / ".testbench").rglob("*") if p.is_file()]
    assert any(p.name == "out.txt" for p in kept) and (root / ".testbench" / "runs.jsonl").is_file()
    for p in kept:
        assert TOKEN.encode() not in p.read_bytes(), f"{p} holds the secret's value"
    assert webhook.events("run_end") and TOKEN not in json.dumps(webhook.bodies)
    assert TOKEN not in cli(root, "report").stdout
    assert cli(root, "verify").returncode == 0, "the evidence hashes are of what was kept: the redacted files"


def test_a_short_or_unknown_secret_is_left_alone(tmp_path, monkeypatch):
    from agent_testbench import runs
    run = tmp_path / "run"
    (run / "steps" / "00-a").mkdir(parents=True)
    (run / "plan.json").write_text(json.dumps({"steps": [{"secrets": ["TB_SHORT", "TB_UNSET", "TB_LONG"]}]}))
    (run / "steps" / "00-a" / "log.txt").write_text("yes admin tok-123456 tok-1234567")
    monkeypatch.setenv("TB_SHORT", "admin")
    monkeypatch.setenv("TB_LONG", "tok-1234567")
    monkeypatch.delenv("TB_UNSET", raising=False)
    rep = runs.redact(run, {"steps": [{"stdout_tail": "admin tok-1234567", "error": {"evalue": ["tok-1234567"]}}]})
    assert rep == {"steps": [{"stdout_tail": "admin [secret]", "error": {"evalue": ["[secret]"]}}]}
    assert (run / "steps" / "00-a" / "log.txt").read_text() == "yes admin tok-123456 [secret]", \
        "values shorter than 6 characters would blank out ordinary words; other text stays as it was"


# ---- approvals -----------------------------------------------------------------------------------------------------

LAUNCHES = [["run", "x"], ["watch", "x"], ["session", "start"], ["loop", "start", "x", "--metric", "m.json:k",
                                                                  "--budget-usd", "1"], ["estimate", "x"]]


@pytest.mark.parametrize("launch", LAUNCHES, ids=lambda x: " ".join(x[:2]))
def test_no_abbreviation_of_approve_approves(launch, capsys):
    parser = build_parser()
    assert parser.parse_args([*launch, "--approve"]).approve
    for n in range(3, len("--approve")):                    # "--" alone ends the options
        with pytest.raises(SystemExit):
            parser.parse_args([*launch, "--approve"[:n]])
    for disguise in ("--approve=yes", "--APPROVE", "\u2014approve", "--approve\u200b", "-approve", "+approve"):
        try:
            assert not parser.parse_args([*launch, disguise]).approve, disguise
        except SystemExit:
            pass                                         # refused: nothing runs


def test_mcp_session_files_takes_its_operation_as_a_value(tmp_path, monkeypatch):
    pytest.importorskip("mcp")
    from agent_testbench import mcp_server as M
    seen = []

    async def fake_cli(project_dir, argv, on_line=None, env=None):
        seen.append(argv)
        return 2, "refused"
    monkeypatch.setattr(M, "_cli", fake_cli)
    for op in ("--approve", "-h", "--session=x", "ls"):
        asyncio.run(M.session_files(op, path="-x", second_path="--y", project_dir=str(tmp_path)))
    for op, argv in zip(("--approve", "-h", "--session=x", "ls"), seen):
        assert argv == ["files", "--", op, "-x", "--y"], "the operation and paths come after --: values, not options"


# ---- the hard stop -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("tick", ["nan", "-1", "1e9", "inf", "soon"])
def test_the_hard_stop_holds_whatever_the_watchdog_tick_says(tmp_path, monkeypatch, tick):
    """TESTBENCH_WATCHDOG_SECONDS goes from the environment a run started in into its container or pod; it can make
    the watchdog check more often, never stop it or put the stop off."""
    from agent_testbench.kernelkit import watchdog as W
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", tick)
    began = time.time()
    assert "max_minutes" in W.watch(tmp_path, 0.005)
    assert time.time() - began < 10, "stopped at max_minutes (0.3 s), not after the tick it was given"


# ---- numbers -------------------------------------------------------------------------------------------------------

def test_a_number_too_big_for_a_float_is_no_limit(tmp_path):
    assert not C.number(10 ** 400) and not C.number(-(10 ** 400)) and C.number(10 ** 300)
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"a": "true"}})
    (root / "testbench.yaml").write_text((root / "testbench.yaml").read_text()
                                         + "budget: {max_run_usd: 1" + "0" * 400 + "}\n")
    r = cli(root, "plans")
    assert r.returncode == 2 and "`budget.max_run_usd` must be a number" in r.stderr and "Traceback" not in r.stderr
    assert os.environ.get("TESTBENCH_USER_DIR"), "never the real ~/.testbench"


@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), 10 ** 400, -1, "5"])
def test_a_loop_s_budget_from_python_is_a_number_of_dollars(tmp_path, bad):
    from agent_testbench import loop as LP
    for args in ({"budget_usd": bad}, {"budget_usd": 1.0, "max_hours": bad}, {"budget_usd": 1.0, "min_delta": bad}):
        with pytest.raises(LP.LoopError, match="is a number of"):
            LP.start(tmp_path, {"plans": {"p": {}}}, "p", "m.json:k", **args)


# ---- the threat model ----------------------------------------------------------------------------------------------

def test_every_test_the_threat_model_names_exists():
    """docs/threat-model.md proves each claim with a test it names (file::test); a test renamed or removed must take
    its claim with it."""
    import ast
    import re
    from trust_helpers import REPO
    text = (REPO / "docs" / "threat-model.md").read_text()
    named = set(re.findall(r"`(test_\w+\.py)::(test_\w+)`", text))
    assert len(named) > 60, "the page names its proof"
    missing = []
    for file, test in sorted(named):
        path = REPO / "tests" / file
        tree = ast.parse(path.read_text()) if path.is_file() else ast.Module(body=[], type_ignores=[])
        if test not in {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}:
            missing.append(f"{file}::{test}")
    assert not missing, f"docs/threat-model.md names tests that do not exist: {missing}"
    nav = (REPO / "mkdocs.yml").read_text()
    assert "threat-model.md" in nav


# ---- the MCP server over HTTP --------------------------------------------------------------------------------------

def test_the_mcp_http_server_answers_only_this_machine_s_names(tmp_path):
    """A web page (one a web step opens, or any the person visits) that points its own name at 127.0.0.1 sends that
    name as the Host, and its own origin: the server refuses both, so the page cannot call run_plan."""
    pytest.importorskip("mcp")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "mcp", "--http", "--port", str(port)],
                            cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}).encode()

    def post(**headers) -> int:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=body, headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status
        except urllib.error.HTTPError as exc:
            return exc.code
    try:
        for _ in range(150):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        assert post() == 200
        assert post(Host=f"evil.example:{port}") in (400, 403, 421), "a name that is not this machine's"
        assert post(Origin="http://evil.example") in (400, 403, 421), "a page from elsewhere"
    finally:
        proc.kill()
        proc.wait(timeout=30)
