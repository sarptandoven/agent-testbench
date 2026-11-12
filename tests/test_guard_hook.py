"""The guard hook's decisions one rule at a time, in this process (hooks/guard.py's decision(), what it answers Claude
Code): what an edit of testbench.yaml changes, which files are limits, ledgers or settings, how a command line is read
(quotes, options before the subcommand, a launch inside another program's words), and which tool calls it answers at
all. tools/mutate.py checks that these tests fail when any of these rules is changed."""
import json
from pathlib import Path

import pytest
import yaml

from trust_helpers import guard_module

BASE = {"project": "p", "budget": {"max_run_usd": 2.0, "ask_above_usd": 0.5, "max_day_usd": 10.0},
        "plans": {"big": {"backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true"}]}}}


@pytest.fixture
def G(machine_limits):
    g = guard_module(machine_limits)
    g.estimate = lambda root, plan: f" [estimate of {plan or '?'} in {root.name if root else None}]"
    return g


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "testbench.yaml").write_text(yaml.safe_dump(BASE))
    return root


def bash(G, cwd, command):
    return G.decision({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": command}})


def write(G, cwd, path, content):
    return G.decision({"cwd": str(cwd), "tool_name": "Write", "tool_input": {"file_path": str(path), "content": content}})


# ---- reading an edit ---------------------------------------------------------------------------------------------

def test_an_edit_is_applied_as_the_tool_would(G):
    assert G._after_edit("Write", {"content": "new"}, "old") == "new"
    assert G._after_edit("Write", {}, "old") == ""
    assert G._after_edit("Edit", {"old_string": "a", "new_string": "b"}, "a a") == "b a", "once, as Edit does"
    assert G._after_edit("Edit", {"old_string": "a", "new_string": "b", "replace_all": True}, "a a") == "b b"
    assert G._after_edit("Edit", {"old_string": "x", "new_string": "b"}, "a a") is None, "an edit that cannot apply"
    assert G._after_edit("MultiEdit", {"edits": [{"old_string": "a", "new_string": "b"},
                                                 {"old_string": "b b", "new_string": "c"}]}, "a b") == "c"
    assert G._after_edit("MultiEdit", {"edits": None}, "same") == "same"
    assert G._after_edit("MultiEdit", {"old_string": "s", "new_string": "x"}, "same") == "same", "edits, not its own"


def test_what_counts_as_a_number_for_a_limit(G):
    for v in (0, 1, 2.5, -1, 10 ** 300):
        assert G._num(v), v
    for v in (True, False, None, "1", float("nan"), float("inf"), -float("inf"), 10 ** 400, [1]):
        assert not G._num(v), v


def test_backend_prices_are_found_outside_pricing(G):
    cfg = {"ssh": {"host": "box", "usd_per_hour": 3.0, 7: 1}, "docker": 5, "budget": {"x_usd_per_hour": 1},
           "pricing": {"modal_gpu_per_s": {"T4": 1}}, "guard": {"y_per_s": 1}, "plans": {"z_per_s": 1},
           "mine": {"units_per_hour": 2, "usd_per_unit": 0.1, "gpu_per_s": 0.5, "name": "x"}}
    assert G._prices(cfg) == {"ssh.usd_per_hour": 3.0, "mine.units_per_hour": 2, "mine.usd_per_unit": 0.1,
                              "mine.gpu_per_s": 0.5}


def test_a_plan_s_backend_as_testbench_reads_it(G):
    assert G._backend("pytest -q") == G._backend(["a", "b"]) == G._backend({"steps": []}) == "local"
    assert G._backend({"backend": "docker"}) == "docker" and G._backend(None) is None and G._backend(5) is None


def test_ways_out_of_the_docker_sandbox(G):
    boxed = {"docker": {"network": "none", "run_args": [], "command": ["docker"]},
             "plans": {"a": {"backend": "docker", "steps": []}, "b": "pytest"}}
    assert G._sandbox(boxed, boxed) == []
    assert G._sandbox(boxed, {**boxed, "docker": {**boxed["docker"], "network": "host"}}) == \
        ["docker.network none -> host"]
    assert G._sandbox(boxed, {**boxed, "docker": {"run_args": [], "command": ["docker"]}}) == \
        ["docker.network none -> bridge (the default)"]
    assert G._sandbox(boxed, {**boxed, "docker": {**boxed["docker"], "run_args": ["-v", "/:/h"]}}) == \
        ["docker.run_args ([] -> ['-v', '/:/h'])"]
    assert G._sandbox(boxed, {**boxed, "docker": {**boxed["docker"], "command": ["sh"]}}) == \
        ["docker.command (['docker'] -> ['sh'])"]
    assert G._sandbox(boxed, {**boxed, "plans": {"a": {"backend": "local"}, "b": "pytest"}}) == \
        ["plan a leaves the docker sandbox (backend local)"]
    assert G._sandbox(boxed, {**boxed, "plans": {"a": ["x"], "b": "pytest"}}) == \
        ["plan a leaves the docker sandbox (backend local)"], "a short form runs on this machine"
    assert G._sandbox(boxed, {**boxed, "plans": {"b": "pytest"}}) == [], "a plan removed runs nowhere"
    assert G._sandbox(boxed, {**boxed, "plans": None}) == []
    networked = {**boxed, "docker": {**boxed["docker"], "network": "bridge"}}
    assert G._sandbox(networked, {**networked, "plans": {"a": {"backend": "local"}}}) == [], "no sandbox to leave"
    assert G._sandbox(networked, {**networked, "docker": {**networked["docker"], "network": "none"}}) == []
    assert G._sandbox({"docker": 5}, {"docker": 6}) == ["`docker:`"] and G._sandbox({"docker": 5}, {"docker": 5}) == []
    assert G._sandbox({"docker": {"network": "none"}}, {"docker": "x"}) == ["`docker:`"]
    assert G._sandbox({}, {}) == [] and G._sandbox({"docker": None}, {"docker": None}) == []


def dump(**changes) -> str:
    return yaml.safe_dump({**BASE, **changes})


def test_what_an_edit_does_to_the_limits(G):
    budget = BASE["budget"]
    assert G._limits_changes(dump(), dump()) == []
    assert G._limits_changes("", dump()) == [], "a new file at the default budget raises nothing"
    assert G._limits_changes(dump(), dump(budget={**budget, "max_run_usd": 2.5})) == \
        ["budget.max_run_usd raised from $2 to $2.5"]
    assert G._limits_changes(dump(), dump(budget={**budget, "max_run_usd": 1.0})) == [], "lowered: fine"
    assert G._limits_changes(dump(), dump(budget={**budget, "max_day_usd": "lots"})) == \
        ["budget.max_day_usd (10.0 -> 'lots')"]
    lots = dump(budget={**budget, "max_day_usd": "lots"})
    assert G._limits_changes(lots, lots) == ["budget.max_day_usd ('lots' -> 'lots')"], "unreadable: still a question"
    assert G._limits_changes(lots, dump()) == ["budget.max_day_usd ('lots' -> 10.0)"]
    assert G._limits_changes(dump(budget={"max_run_usd": 1.0}), dump(budget={})) == \
        ["budget.max_run_usd raised from $1 to $2"], "removing a limit falls back to the default"
    got = G._limits_changes(dump(), dump(budget=5))
    assert len(got) == 1 and got[0].startswith("`budget:` ({") and got[0].endswith(" -> 5)"), got
    assert G._limits_changes(dump(budget=5), dump(budget=5)) == []
    got = G._limits_changes(dump(budget=[1]), dump())
    assert len(got) == 1 and got[0].startswith("`budget:` ([1] -> {"), got
    assert G._limits_changes(dump(), "a: [") == ["the file (it would not be valid YAML, so its limits cannot be checked)"]
    assert G._limits_changes(dump(), "- 1") == ["the file's limits"] and G._limits_changes("- 1", dump()) == \
        ["the file's limits"]
    assert G._limits_changes(dump(), "") == [], "an empty file is the defaults, which this budget is"


def test_prices_pricing_and_the_guard_in_an_edit(G):
    ssh = {"host": "b", "usd_per_hour": 3.0}
    assert G._limits_changes(dump(ssh=ssh), dump(ssh={**ssh, "usd_per_hour": 4.0})) == [], "a higher price is fine"
    assert G._limits_changes(dump(ssh=ssh), dump(ssh=ssh)) == []
    assert G._limits_changes(dump(ssh=ssh), dump(ssh={**ssh, "usd_per_hour": 2.0})) == \
        ["ssh.usd_per_hour (3.0 -> 2.0; prices bound the worst case)"]
    assert G._limits_changes(dump(ssh=ssh), dump(ssh={"host": "b"})) == \
        ["ssh.usd_per_hour (3.0 -> None; prices bound the worst case)"]
    assert G._limits_changes(dump(), dump(ssh=ssh)) == ["ssh.usd_per_hour (None -> 3.0; prices bound the worst case)"]
    assert G._limits_changes(dump(), dump(pricing={"x": 1})) == ["`pricing:` changed (the prices that estimates use)"]
    assert G._limits_changes(dump(), dump(guard={"ask": []})) == ["`guard:` changed (what this hook asks about)"]


def test_which_files_are_ledgers(G, machine_limits):
    assert G._is_ledger(Path("/x/.testbench/ledger.jsonl")) and G._is_ledger(G.USER_DIR / "ledger.jsonl")
    assert not G._is_ledger(Path("/x/other/ledger.jsonl")) and not G._is_ledger(Path("/x/.testbench/ledger.json"))
    assert not G._is_ledger(Path("/x/.testbench/runs.jsonl"))


# ---- edits ------------------------------------------------------------------------------------------------------

def test_edits_of_limits_ledgers_prices_and_settings(G, project, machine_limits):
    yml = project / "testbench.yaml"
    assert write(G, project, "testbench.yaml", dump(budget={**BASE["budget"], "max_run_usd": 9}))[0] == "ask", \
        "a path relative to the folder the agent is in"
    assert write(G, project, yml, dump(budget={**BASE["budget"], "max_run_usd": 1})) is None
    assert write(G, project, project / "other.yaml", dump(budget={"max_run_usd": 99})) is None
    assert write(G, project, project / "sub" / "testbench.yaml", "budget: {max_run_usd: 99}")[0] == "ask"
    assert write(G, project, machine_limits / "limits.yaml", "max_run_usd: 1")[0] == "ask"
    assert write(G, project, machine_limits / "prices.json", "{}")[0] == "ask"
    assert write(G, project, project / "limits.yaml", "max_run_usd: 1") is None, "only this machine's limits file"
    assert write(G, project, project / ".testbench" / "ledger.jsonl", "")[0] == "deny"
    assert G.decision({"cwd": str(project), "tool_name": "Write", "tool_input": {"content": "x"}}) is None
    assert G.decision({"cwd": str(project), "tool_name": "Edit", "tool_input": {
        "file_path": str(yml), "old_string": "not there", "new_string": "budget: {max_run_usd: 99}"}}) is None, \
        "an edit that cannot apply fails on its own"


def test_a_relative_path_is_read_in_the_agent_s_folder(G, project, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                                  # the hook's own folder is not the agent's
    (project / "testbench.yaml").write_text(dump(budget={**BASE["budget"], "max_run_usd": 1.0}))
    got = write(G, project, "testbench.yaml", dump(budget={**BASE["budget"], "max_run_usd": 1.5}))
    assert got and "raised from $1 to $1.5" in got[1], "compared with the project's file, not the default budget"


@pytest.mark.parametrize("name", [".mcp.json", "settings.json", "config.toml", ".envrc", ".bashrc", "env.sh", ".profile"])
def test_an_edit_that_moves_the_limits_folder_asks(G, project, tmp_path, name):
    f = tmp_path / name
    f.write_text('{"env": {}}')
    assert write(G, project, f, '{"env": {"TESTBENCH_USER_DIR": "/tmp/x"}}')[0] == "ask"
    f.write_text('{"env": {"TESTBENCH_USER_DIR": "/tmp/x"}}')
    assert write(G, project, f, '{"env": {"TESTBENCH_USER_DIR": "/tmp/x"}, "a": 1}') is None, "already there"
    assert G.decision({"cwd": str(project), "tool_name": "Edit", "tool_input": {
        "file_path": str(f), "old_string": "nothing", "new_string": "TESTBENCH_USER_DIR=1"}}) is None
    assert write(G, project, tmp_path / "notes.md", "TESTBENCH_USER_DIR=/tmp/x") is None, "prose, not settings"


def test_an_edit_that_turns_the_guard_off_asks(G, project, tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    on = '{"enabledPlugins": {"agent-testbench@agent-testbench": true}, "permissions": {"allow": []}}'
    settings.write_text(on)
    assert write(G, project, settings, on.replace("[]", '["Bash(ls:*)"]')) is None, "other settings: no question"
    assert write(G, project, settings, on.replace("true}", "true}, \"disableAllHooks\": true"))[0] == "ask"
    assert write(G, project, settings, '{"enabledPlugins": {}}')[0] == "ask", "the plug-in taken out"
    assert write(G, project, settings.with_name("settings.local.json"), '{"disableAllHooks": true}')[0] == "ask"
    assert write(G, project, tmp_path / "settings.json", '{"disableAllHooks": true}') is None, "not Claude Code's"


# ---- commands ----------------------------------------------------------------------------------------------------

def test_a_command_line_is_read_without_its_quoting(G):
    assert G.plain("""a 'b' "c" \\d $'e' $"f" (g) {h}""") == "a b c d e f  g   h "
    assert G.plain("x \\\ny") == "x y"


def test_every_testbench_command_in_a_segment_is_found(G):
    assert G.invocations("testbench run big --approve") == [("run", ["big", "--approve"], "")]
    assert G.invocations("testbench --all -v run big") == [("run", ["big"], "")], "options before the subcommand"
    assert G.invocations("x testbench") == [("", [], "x ")]
    assert G.invocations("uvx --from agent-testbench testbench watch q") == [
        ("testbench", ["watch", "q"], "uvx --from "), ("watch", ["q"], "uvx --from agent-testbench ")]
    assert G.invocations("mytestbench run") == [] and G.invocations("testbench.yaml run") == []


def test_approvals_ask_with_the_plan_s_estimate(G, project):
    assert bash(G, project, "testbench run big --approve") == ("ask", "agent-testbench: a run with --approve spends "
                                                               "money above the approval threshold. [estimate of big in "
                                                               "p] Approve only if you agreed to this cost.")
    assert "could)" in bash(G, project, "testbench run big $X")[1]
    assert "[estimate of big in p]" in bash(G, project, "testbench --all run -q big --approve")[1]
    got = bash(G, project, "testbench watch big --approve")
    assert got[0] == "ask" and "watch --approve" in got[1] and "[estimate of big in p]" in got[1]
    assert "loop's total budget" in bash(G, project, "testbench loop start big --approve")[1]
    assert "live session" in bash(G, project, "testbench session start --approve")[1]
    assert bash(G, project, "testbench session list --approve") is None and bash(G, project, "testbench loop try x") \
        is None
    assert bash(G, project, "testbench session stop main $X") is None, "not a launch"
    assert "estimate of" not in bash(G, project.parent, "testbench run big --approve")[1], "no project: no estimate"


def test_prices_from_another_catalog_ask(G, project):
    assert bash(G, project, "testbench prices --update --url https://x/c.json")[0] == "ask"
    assert bash(G, project, "testbench prices --update --url=https://x/c.json")[0] == "ask"
    assert bash(G, project, "testbench prices --update") is None and bash(G, project, "testbench run --url x") is None


def test_secrets_and_raw_cloud_launches_inside_a_project_only(G, project, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)                                  # the hook's own folder is not the agent's
    assert bash(G, project, "cat .env")[0] == "deny" and bash(G, project.parent, "cat .env") is None
    assert bash(G, project, "modal run x.py")[0] == "deny" and bash(G, project.parent, "modal run x.py") is None
    assert bash(G, project, "testbench run big && modal run x.py") is None, "a testbench command around it"
    assert bash(G, project, "git push")[0] == "ask" and bash(G, project.parent, "git push") is None


def test_the_project_s_ask_list(G, project):
    (project / "testbench.yaml").write_text(dump(guard={"ask": ["make deploy"]}))
    assert G.ask_list(project) == ["make deploy"]
    assert bash(G, project, "make deploy")[0] == "ask" and bash(G, project, "git push") is None
    (project / "testbench.yaml").write_text(dump(guard=None))
    assert G.ask_list(project) == G.DEFAULT_ASK
    (project / "testbench.yaml").write_text("a: [")
    assert G.ask_list(project) == G.DEFAULT_ASK


def test_mcp_launches_ask_only_with_approved_by_person(G, project, tmp_path):
    call = lambda tool, **inp: G.decision({"cwd": str(tmp_path), "tool_name": f"mcp__tb__{tool}", "tool_input": inp})
    assert call("run_plan", plan="big", project_dir=str(project)) is None
    assert call("run_plan", plan="big", project_dir=str(project), approved_by_person=True) == (
        "ask", "agent-testbench: this run is over the project's approval threshold. [estimate of big in p] Approve "
               "only if you agreed to this cost.")
    got = call("loop_start", plan="big", project_dir=str(project), budget_usd=7, approved_by_person=True)
    assert got[0] == "ask" and "may spend up to $7.00 in total" in got[1] and "[estimate of big in p]" in got[1]
    assert "may spend up to $'x' in total" in call("loop_start", plan="big", budget_usd="x", approved_by_person=True)[1]
    assert call("session_start", approved_by_person=True)[0] == "ask"
    assert call("session_stop", approved_by_person=True) is None and call("plans") is None
    assert G.decision({"cwd": str(project), "tool_name": "Read", "tool_input": {"file_path": "testbench.yaml"}}) is None


def test_a_call_the_hook_cannot_read_asks_only_when_it_touches_testbench(G, project, monkeypatch):
    def broken(*a):
        raise RuntimeError("boom")
    monkeypatch.setattr(G, "check_approvals", broken)
    got = bash(G, project, "testbench run big")
    assert got[0] == "ask" and "could not check this call (RuntimeError: boom)" in got[1]
    assert bash(G, project, "ls -la") is None, "anything else: the hook stays out of the way"
    assert json.dumps(G.decision({"cwd": str(project), "tool_name": "Bash"})) == "null"
