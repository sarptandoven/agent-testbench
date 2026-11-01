"""Stopping rules for loops left to run on their own: --target, --patience, --max-tries. Once one fires, the next try
is refused with what to do instead, and the person's webhook hears about it."""
import json
from pathlib import Path

from conftest import cli, git, score, write_config

START = ("loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0")


def state(root: Path) -> dict:
    return json.loads(next((root / ".testbench" / "loops").glob("*/state.json")).read_text())


def test_target_reached_ends_the_loop(repo, webhook):
    cfg = (repo / "testbench.yaml").read_text() + f"notify:\n  webhook: {webhook.url}\n  on: [loop_end]\n"
    (repo / "testbench.yaml").write_text(cfg)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "notify")
    r = cli(repo, *START, "--target", "3")
    assert r.returncode == 0 and "BASELINE: score 1" in r.stdout and "Target" not in r.stdout, r.stdout
    assert "stopping rules: target score >= 3" in cli(repo, "loop", "status").stdout

    score(repo, 2)
    r = cli(repo, "loop", "try", "two")
    assert r.returncode == 0 and "KEPT try 1" in r.stdout and "Target" not in r.stdout, r.stdout
    score(repo, 3)
    r = cli(repo, "loop", "try", "three")
    assert r.returncode == 0 and "KEPT try 2: score 3" in r.stdout, r.stdout
    assert "Target reached (score >= 3): stop the loop with `testbench loop stop` and report." in r.stdout

    score(repo, 4)
    r = cli(repo, "loop", "try", "four")
    assert r.returncode == 2 and "the loop's target is reached: best score 3 (try 2, commit" in r.stdout, r.stdout
    assert "No new try is made; stop the loop with `testbench loop stop` and report the result." in r.stdout
    assert git(repo, "status", "--porcelain", "--", "settings.py"), "a refused try commits nothing"
    status = cli(repo, "loop", "status").stdout
    assert "-> Target reached" in status and "2 tries, 2 kept" in status

    git(repo, "checkout", "--", "settings.py")
    r = cli(repo, "loop", "stop")
    assert r.returncode == 0 and "Stopping rules: target score >= 3. It had reached its target when it was stopped." \
        in r.stdout, r.stdout
    assert [(e["event"], e["status"], e["open"]) for e in webhook.bodies] == [
        ("loop_rule", "target_reached", True), ("loop_end", "stopped", False)], "a rule fired, then the loop ended"
    rule, end = webhook.bodies
    assert rule["best"] == 3 and rule["tries"] == 2 and rule["best_run"] == state(repo)["tries"][2]["run"]
    assert "reached its target: score 1 -> 3 (max) in 2 tries, 2 kept" in rule["text"]
    assert end["report"].endswith("report.md") and "It had reached its target." in end["text"]
    assert end["ended_by"] == "target_reached" and rule["ended_by"] is None
    assert not webhook.events("run_end"), "on: [loop_end] sends nothing else (it brings loop_rule along)"


def test_each_try_s_verdict_is_sent_when_asked_for(repo, webhook):
    cfg = (repo / "testbench.yaml").read_text() + f"notify:\n  webhook: {webhook.url}\n  on: [run_end, loop_try]\n"
    (repo / "testbench.yaml").write_text(cfg)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "notify")
    assert cli(repo, *START).returncode == 0
    score(repo, 2)
    assert cli(repo, "loop", "try", "two").returncode == 0
    score(repo, 0)
    assert cli(repo, "loop", "try", "zero").returncode == 1
    tries = webhook.events("loop_try")
    assert [(e["try"], e["status"], e["value"]) for e in tries] == [(0, "baseline", 1), (1, "kept", 2),
                                                                    (2, "reverted", 0)], tries
    assert "KEPT try 1: score 2" in tries[1]["text"] and tries[1]["description"] == "two"
    assert "REVERTED try 2" in tries[2]["text"] and tries[2]["run_status"] == "passed" and tries[2]["best"] == 2
    assert not webhook.events("run_end"), "a loop's runs send no run_end: a run's PASSED cannot say kept or reverted"
    assert cli(repo, "run", "score").returncode == 0
    assert len(webhook.events("run_end")) == 1, "a run outside the loop still does"


def test_a_baseline_at_the_target_refuses_the_first_try(repo):
    r = cli(repo, *START, "--goal", "min", "--target", "1")
    assert r.returncode == 0 and "Target reached (score <= 1)" in r.stdout, r.stdout
    score(repo, 0)
    r = cli(repo, "loop", "try", "zero")
    assert r.returncode == 2 and "best score 1 (the baseline, commit" in r.stdout, r.stdout


def test_patience_refuses_more_of_the_same(repo, webhook):
    write_config(repo, {"project": "scoring", "notify": {"webhook_env": "TB_HOOK"}, "plans": {"score": {"steps": [
        {"name": "score", "run": "python score.py", "max_minutes": 1,
         "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}})
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "notify")
    env = {"TB_HOOK": webhook.url}
    assert cli(repo, *START, "--patience", "2", env=env).returncode == 0
    score(repo, 0)
    r = cli(repo, "loop", "try", "zero", env=env)
    assert r.returncode == 1 and "REVERTED try 1" in r.stdout and "in a row" not in r.stdout, r.stdout
    score(repo, 5)
    assert cli(repo, "loop", "try", "five", env=env).returncode == 0, "a kept try starts the count again"
    for value in (2, 3):
        score(repo, value)
        r = cli(repo, "loop", "try", f"down to {value}", env=env)
        assert r.returncode == 1
    assert "That is 2 tries in a row not kept (--patience 2): stop the loop" in r.stdout, r.stdout
    assert "--new-direction" in r.stdout

    score(repo, 4)
    r = cli(repo, "loop", "try", "down to 4", env=env)
    assert r.returncode == 2 and "2 tries in a row were not kept (--patience 2):" in r.stdout, r.stdout
    assert "try 3: down to 2 - score 2 is not better than the best 5" in r.stdout
    assert "try 4: down to 3 - score 3 is not better than the best 5" in r.stdout
    assert "Best score so far: 5 (try 2, commit" in r.stdout and "testbench loop stop" in r.stdout
    assert "testbench loop try --new-direction" in r.stdout
    assert "patience 2 (2 in a row not kept now)" in cli(repo, "loop", "status", env=env).stdout

    score(repo, 9)
    r = cli(repo, "loop", "try", "a different model", "--new-direction", env=env)
    assert r.returncode == 0 and "KEPT try 5: score 9" in r.stdout, r.stdout
    score(repo, 1)
    r = cli(repo, "loop", "try", "anything", "--new-direction", env=env)
    assert r.returncode == 2 and "--new-direction is for the try after --patience refused one" in r.stdout
    assert "[new direction] a different model" in cli(repo, "loop", "status", env=env).stdout
    rules = webhook.events("loop_rule")
    assert [e["status"] for e in rules] == ["out_of_patience"] and rules[0]["open"], rules
    assert "ran out of patience" in rules[0]["text"] and "try something different in kind" in rules[0]["text"]
    assert not webhook.events("loop_end"), "the loop goes on after patience runs out: it has not ended"
    assert not webhook.events("run_end") and not webhook.events("loop_try"), \
        "a loop's runs are told by the loop, and each try's verdict only when asked for"


def test_max_tries(repo):
    r = cli(repo, *START, "--max-tries", "1", "--patience", "5")
    assert r.returncode == 0
    score(repo, 2)
    r = cli(repo, "loop", "try", "two")
    assert r.returncode == 0 and "That was the last try the loop may make (--max-tries 1)" in r.stdout, r.stdout
    score(repo, 3)
    r = cli(repo, "loop", "try", "three")
    assert r.returncode == 2 and "the loop has made its 1 try (--max-tries 1); best score 2 (try 1" in r.stdout
    git(repo, "checkout", "--", "settings.py")
    report = cli(repo, "loop", "stop").stdout
    assert "at most 1 tries (1 made)" in report and "It had made all its tries" in report, report


def test_bad_rules_and_loops_without_them(repo):
    r = cli(repo, *START, "--patience", "0")
    assert r.returncode == 2 and "--patience is a whole number, 1 or more" in r.stdout, r.stdout
    assert not (repo / ".testbench" / "loops").exists() or not list((repo / ".testbench" / "loops").iterdir())
    assert cli(repo, *START).returncode == 0
    for value in (0, 0.5, 0.25):
        score(repo, value)
        assert cli(repo, "loop", "try", f"to {value}").returncode == 1
    status = cli(repo, "loop", "status").stdout
    assert "stopping rules" not in status and state(repo)["patience"] is None
    r = cli(repo, "loop", "try", "x", "--new-direction")
    assert r.returncode == 2 and "this loop has no --patience" in r.stdout
    assert "Stopping rules" not in cli(repo, "loop", "stop").stdout


def test_stopping_rules_over_mcp(repo):
    import asyncio
    from agent_testbench import mcp_server

    async def go():
        call = mcp_server.server.call_tool
        text = lambda out: json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
        a = text(await call("loop_start", {"plan": "score", "metric": "metrics.json:score", "budget_usd": 0,
                                           "target": 2, "max_tries": 3, "project_dir": str(repo)}))
        score(repo, 2)
        b = text(await call("loop_try", {"description": "two", "project_dir": str(repo)}))
        score(repo, 3)
        c = text(await call("loop_try", {"description": "three", "project_dir": str(repo)}))
        return a, b, c

    a, b, c = asyncio.run(go())
    assert "BASELINE" in a and "Target reached (score >= 2)" in b and "target is reached" in c and "[exit 2]" in c
    assert state(repo)["target"] == 2 and state(repo)["max_tries"] == 3
