"""testbench loop: the agent edits, testbench commits the try, proves it with a run, and keeps it only if the run
passes and the metric beats the best; otherwise the branch goes back. All inside the loop's budget."""
import json
import subprocess

from conftest import SCORER, cli, git, score, write_config

def test_keep_what_improves_revert_what_does_not(repo):
    original = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0")
    assert r.returncode == 0 and "BASELINE: score 1, run" in r.stdout, r.stdout
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").startswith("testbench-loop/")

    score(repo, 3)
    r = cli(repo, "loop", "try", "raise the score to 3")
    assert r.returncode == 0 and "KEPT try 1: score 3 (best was 1, +2)" in r.stdout, r.stdout
    best = git(repo, "rev-parse", "HEAD")

    score(repo, 2)
    r = cli(repo, "loop", "try", "lower it to 2")
    assert r.returncode == 1 and "REVERTED try 2: score 2 is not better than the best 3" in r.stdout, r.stdout
    assert (repo / "settings.py").read_text() == "SCORE = 3\n" and git(repo, "rev-parse", "HEAD") == best

    score(repo, -5)
    r = cli(repo, "loop", "try", "a bug")
    assert r.returncode == 1 and "REVERTED try 3: the run failed" in r.stdout
    assert (repo / "settings.py").read_text() == "SCORE = 3\n"

    r = cli(repo, "loop", "try", "nothing")
    assert r.returncode == 2 and "nothing changed" in r.stdout

    status = cli(repo, "loop", "status").stdout
    assert "best 3" in status and "3 tries, 1 kept" in status and "reverted" in status

    r = cli(repo, "loop", "stop")
    assert r.returncode == 0 and "score 1 -> 3 (max)" in r.stdout and "## Kept" in r.stdout
    assert "try 1 " in r.stdout and "raise the score to 3 - score 1 -> 3 (+2)" in r.stdout
    assert "lower it to 2 - score 2 is not better" in r.stdout
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == original, "back where the loop started"
    branch = next(ln.strip() for ln in git(repo, "branch").splitlines() if "testbench-loop/" in ln)
    assert git(repo, "log", "--oneline", f"{original}..{branch}").count("\n") == 0, "one kept commit on the branch"
    notes = (repo / ".testbench" / "NOTES.md").read_text()
    assert "KEPT try 1" in notes and "REVERTED try 2" in notes, "the lab notebook keeps every verdict"


def test_edits_made_while_a_try_runs_survive_its_revert(repo):
    cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0")
    (repo / "score.py").write_text(SCORER.replace('print("score", SCORE)', 'import time; time.sleep(4); print("score", SCORE)'))
    score(repo, 0)
    r = cli(repo, "loop", "try", "slow and worse", "--wait", "0")
    assert r.returncode == 3 and "still running" in r.stdout
    assert cli(repo, "loop", "try", "too soon").returncode == 2
    (repo / "README.md").write_text("next idea\n")                     # the agent keeps working meanwhile
    run = json.loads(next((repo / ".testbench" / "loops").glob("*/state.json")).read_text())["tries"][1]["run"]
    cli(repo, "wait", run, "--timeout", "60")
    r = cli(repo, "loop", "status")
    assert r.returncode == 0 and "reverted" in r.stdout
    assert (repo / "settings.py").read_text() == "SCORE = 1\n" and (repo / "README.md").read_text() == "next idea\n"


def test_refusals_say_what_to_do(repo, plugin):
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:loss", "--budget-usd", "1")
    assert r.returncode == 2 and "checks no 'loss' in metrics.json" in r.stdout
    (repo / "settings.py").write_text("SCORE = 9\n")
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "1")
    assert r.returncode == 2 and "uncommitted changes" in r.stdout
    git(repo, "checkout", "--", "settings.py")
    write_config(repo, {"project": "scoring", "fake": {"usd_per_hour": 3600},          # $60 a minute
                        "budget": {"max_run_usd": 100, "ask_above_usd": 0.5, "max_day_usd": 1000},
                        "plans": {"score": {"backend": "fake", "gpu": "FAKE1", "steps": [
                            {"name": "score", "run": "python score.py && sleep 1", "max_minutes": 0.1,
                             "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]}}})    # worst case $66 a run
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "priced")
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "50", env=plugin)
    assert r.returncode == 2 and "no room for one run" in r.stdout and "$66.00" in r.stdout, r.stdout
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "67", env=plugin)
    assert r.returncode == 2 and "Needs approval: a loop on plan score could spend up to $67.00" in r.stdout
    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "67", "--approve", env=plugin)
    assert r.returncode == 0 and "BASELINE" in r.stdout, r.stdout          # measured: a second or more, over $1
    score(repo, 2)
    r = cli(repo, "loop", "try", "better", env=plugin)
    assert r.returncode == 2 and "no room for another run" in r.stdout, r.stdout
    assert git(repo, "status", "--porcelain", "--", "settings.py"), "a refused try leaves the edits uncommitted"
    assert "try" not in git(repo, "log", "-1", "--format=%s"), "and commits nothing"


def test_loop_over_mcp_and_the_guard(repo):
    import asyncio
    import sys
    from agent_testbench import mcp_server
    from conftest import REPO

    async def go():
        call = mcp_server.server.call_tool
        text = lambda out: json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
        a = text(await call("loop_start", {"plan": "score", "metric": "metrics.json:score", "budget_usd": 0,
                                           "project_dir": str(repo)}))
        score(repo, 4)
        b = text(await call("loop_try", {"description": "four", "project_dir": str(repo)}))
        c = text(await call("loop_status", {"project_dir": str(repo)}))
        d = text(await call("loop_stop", {"project_dir": str(repo)}))
        return a, b, c, d

    a, b, c, d = asyncio.run(go())
    assert "BASELINE: score 1" in a and "KEPT try 1: score 4" in b and "1 tries, 1 kept" in c and "score 1 -> 4" in d
    for tool, inp in (("Bash", {"command": "testbench loop start score --metric m.json:x --budget-usd 9 --approve"}),
                      ("mcp__x__loop_start", {"plan": "score", "budget_usd": 9, "approved_by_person": True})):
        p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], capture_output=True, text=True,
                           input=json.dumps({"cwd": str(repo), "tool_name": tool, "tool_input": inp}), timeout=30)
        decision = json.loads(p.stdout)["hookSpecificOutput"]
        assert decision["permissionDecision"] == "ask" and "loop" in decision["permissionDecisionReason"]
