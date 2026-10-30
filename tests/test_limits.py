"""Limits the agent cannot raise: this machine's limits cap every project, the machine's ledger counts every
project, and the guard asks before an edit raises a budget and refuses edits to ledgers."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from agent_testbench import budget, config as C
from conftest import REPO, cli, write_config

PLANS = {"cheap": {"backend": "modal", "steps": [{"name": "a", "run": "true", "max_minutes": 1}]},
         "mid": {"backend": "modal", "gpu": "T4", "steps": [{"name": "a", "run": "true", "max_minutes": 30}]},
         "big": {"backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]}}


def project(root: Path, **budget_values) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    write_config(root, {"project": root.name, "budget": {"max_run_usd": 50, "ask_above_usd": 40, "max_day_usd": 100,
                                                         **budget_values}, "plans": PLANS})
    return C.load(root)


def test_machine_limits_cap_a_generous_project(tmp_path, machine_limits):
    cfg = project(tmp_path / "p")
    assert budget.check_launch(cfg, "big", False)[0], "without machine limits the project's own budget applies"
    machine_limits.mkdir()
    (machine_limits / "limits.yaml").write_text("max_run_usd: 3.0\nask_above_usd: 0.2\n")
    ok, msg, _ = budget.check_launch(cfg, "big", True)
    assert not ok and "max_run_usd $3.00 in" in msg and "a project cannot raise" in msg
    ok, msg, _ = budget.check_launch(cfg, "mid", False)
    assert not ok and "Needs approval" in msg and "ask_above_usd $0.20 in" in msg
    assert budget.check_launch(cfg, "mid", True)[0]
    lower = project(tmp_path / "q", max_run_usd=0.1)
    ok, msg, _ = budget.check_launch(lower, "mid", True)
    assert not ok and "budget.max_run_usd $0.10" in msg, "a project can still lower its own limits"


def test_the_machine_day_limit_counts_every_project(tmp_path, machine_limits):
    a, b = project(tmp_path / "a"), project(tmp_path / "b")
    machine_limits.mkdir()
    (machine_limits / "limits.yaml").write_text("max_day_usd: 1.0\n")
    budget.record(Path(a["root"]), {"event": "launch", "run": "r1", "worst_case_usd": 0.5})
    budget.record(Path(a["root"]), {"event": "finish", "run": "r1", "usd": 0.9})
    budget.record(Path(b["root"]), {"event": "launch", "run": "r1", "worst_case_usd": 0.05})   # same id, other project
    assert budget.spent_today(Path(b["root"])) == 0.05 and budget.spent_today_everywhere() == 0.95
    ok, msg, _ = budget.check_launch(b, "mid", True)
    assert not ok and "across every project on this machine" in msg and "max_day_usd $1.00" in msg
    (Path(a["root"]) / ".testbench" / "ledger.jsonl").unlink()      # a project's ledger gone: the machine's remains
    assert not budget.check_launch(b, "mid", True)[0]


@pytest.mark.parametrize("text", ["max_run_usd: [", "max_run_usd: lots", "- 1\n- 2\n"])
def test_a_broken_limits_file_refuses_everything(tmp_path, machine_limits, text):
    cfg = project(tmp_path / "p")
    machine_limits.mkdir()
    (machine_limits / "limits.yaml").write_text(text)
    with pytest.raises(C.ConfigError, match="nothing can launch"):
        budget.check_launch(cfg, "cheap", False)


def test_init_writes_machine_limits_once(tmp_path, machine_limits):
    root = tmp_path / "fresh"
    root.mkdir()
    r = cli(root, "init")
    assert r.returncode == 0 and "limits for every project on this machine" in r.stdout
    assert yaml.safe_load((machine_limits / "limits.yaml").read_text())["max_run_usd"] == 5.0
    (machine_limits / "limits.yaml").write_text("max_run_usd: 0.01\n")
    other = tmp_path / "other"
    other.mkdir()
    assert "limits for every project" not in cli(other, "init").stdout
    assert (machine_limits / "limits.yaml").read_text() == "max_run_usd: 0.01\n", "never overwritten"
    r = cli(other, "plans")
    assert "capped by this machine: max_run_usd $0.01" in r.stdout


def guard(payload: dict) -> str | None:
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=30)
    return json.loads(p.stdout)["hookSpecificOutput"]["permissionDecision"] if p.stdout.strip() else None


def test_guard_asks_only_when_an_edit_raises_the_limits(tmp_path):
    root = tmp_path / "p"
    project(root, max_run_usd=2.0)
    yml = root / "testbench.yaml"
    text = yml.read_text()
    edit = lambda old, new: guard({"cwd": str(root), "tool_name": "Edit",
                                   "tool_input": {"file_path": str(yml), "old_string": old, "new_string": new}})
    assert edit("max_run_usd: 2.0", "max_run_usd: 20.0") == "ask"
    assert edit("max_run_usd: 2.0", "max_run_usd: 1.0") is None, "lowering a budget needs no question"
    assert edit("max_minutes: 30", "max_minutes: 20") is None, "editing plans needs no question"
    assert guard({"cwd": str(root), "tool_name": "Write", "tool_input": {
        "file_path": "testbench.yaml", "content": text + "pricing:\n  modal_gpu_per_s: {H100: 0.0000001}\n"}}) == "ask"
    assert guard({"cwd": str(root), "tool_name": "MultiEdit", "tool_input": {"file_path": str(yml), "edits": [
        {"old_string": "max_day_usd: 100", "new_string": "max_day_usd: 1000"}]}}) == "ask"


def test_guard_protects_machine_limits_and_ledgers(tmp_path, machine_limits):
    root = tmp_path / "p"
    project(root)
    assert guard({"cwd": str(root), "tool_name": "Edit", "tool_input": {
        "file_path": str(machine_limits / "limits.yaml"), "old_string": "5.00", "new_string": "500"}}) == "ask"
    assert guard({"cwd": str(root), "tool_name": "Write", "tool_input": {
        "file_path": str(root / ".testbench" / "ledger.jsonl"), "content": ""}}) == "deny"
    bash = lambda cmd: guard({"cwd": str(root), "tool_name": "Bash", "tool_input": {"command": cmd}})
    assert bash("rm .testbench/ledger.jsonl") == "deny"
    assert bash(f"echo '' > {machine_limits}/ledger.jsonl") == "deny"
    assert bash("cat .testbench/ledger.jsonl") is None, "reading the ledger is fine"
    assert bash("TESTBENCH_USER_DIR=/tmp/none testbench run big") == "deny"
    assert bash("HOME=/tmp testbench session start --backend modal") == "deny"
    assert bash("sed -i 's/50/500/' testbench.yaml") == "ask"
    assert bash("testbench plans") is None
