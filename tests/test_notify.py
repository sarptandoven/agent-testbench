"""Notifications: testbench.yaml's `notify:` webhook hears when a run ends, a loop ends and a launch waits for a
person's approval. Delivery never breaks a run, and the webhook URL's secret path is never shown."""
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import REPO, Webhook, cli, git, score, write_config

PLAN = {"name": "score", "run": "python score.py", "max_minutes": 1,
        "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}


def configure(root: Path, notify: dict | None, **extra) -> None:
    write_config(root, {"project": "scoring", **({"notify": notify} if notify else {}), **extra,
                        "plans": extra.pop("plans", None) or {"score": {"steps": [PLAN]}}})


def never_shown(root: Path, *outputs) -> None:
    """The webhook's secret path is in no output and in nothing testbench wrote (scratch copies of the project,
    which hold testbench.yaml itself, aside)."""
    for out in outputs:
        assert Webhook.SECRET not in out and "SECRET" not in out, out
    for f in (root / ".testbench").rglob("*"):
        if f.is_file() and "work" not in f.relative_to(root).parts:
            assert "SECRET" not in f.read_text(errors="replace"), f


def test_run_end_for_passing_and_failing_runs(repo, webhook):
    configure(repo, {"webhook": webhook.url})
    r = cli(repo, "run", "score")
    assert r.returncode == 0
    [sent] = webhook.events("run_end")
    run = sent["run_id"]
    assert sent["path"] == Webhook.SECRET and sent["project"] == "scoring" and sent["plan"] == "score"
    assert sent["status"] == "passed" and sent["numbers"] == {"metrics.json: score >= 0": 1.0}
    assert sent["cost_usd"] == 0 and sent["report"] == f".testbench/runs/{run}/report.md"
    assert sent["text"].startswith(f"agent-testbench, scoring: run {run} of plan score on local PASSED after ")
    assert "metrics.json: score >= 0 (found 1)" in sent["text"], sent["text"]

    score(repo, -5)
    r = cli(repo, "run", "score")
    assert r.returncode == 1
    sent = webhook.events("run_end")[-1]
    assert sent["status"] == "failed" and "FAILED" in sent["text"] and "Stopped because step 1 (score)" in sent["text"]
    assert sent["failed"] and "exit code 0" in sent["failed"][0], sent
    log = (repo / ".testbench" / "notify.log").read_text()
    assert log.count("run_end to http://127.0.0.1:") == 2 and "accepted (HTTP 200)" in log
    never_shown(repo, r.stdout + r.stderr, log)


@pytest.mark.posix
def test_run_end_for_a_cancelled_run(repo, webhook):
    configure(repo, {"webhook": webhook.url}, plans={"slow": {"steps": [
        {"name": "wait", "run": "python -c \"import time; time.sleep(60)\"", "max_minutes": 2}]}})
    assert cli(repo, "run", "slow", "--detach").returncode == 3
    cancelled = cli(repo, "cancel")
    assert "cancelled" in cancelled.stdout
    [sent] = webhook.events("run_end")
    assert sent["status"] == "cancelled" and sent["plan"] == "slow" and "CANCELLED" in sent["text"], sent
    never_shown(repo, cancelled.stdout + cancelled.stderr)


def test_run_end_is_sent_once_when_a_cancel_gives_up_on_a_slow_driver(repo, webhook, monkeypatch):
    from agent_testbench import notify, runs
    configure(repo, {"webhook": webhook.url})
    assert cli(repo, "run", "score").returncode == 0
    run_dir = runs.resolve(repo, None)
    runs.write_state(run_dir, status="running", driver_pid=12345)          # as when `cancel` came in, not yet told
    (run_dir / notify.TOLD).unlink()
    webhook.bodies.clear()
    monkeypatch.setattr(runs, "alive", lambda pid: True)                    # a driver that takes over 60 s to stop
    monkeypatch.setattr(runs, "kill_group", lambda pid, hard=False: None)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    assert runs.cancel(run_dir).endswith("cancelled") and runs.read_state(run_dir)["status"] == "cancelled"
    notify.run_end(repo, run_dir)                                           # ... then the driver finishes after all
    assert [e["status"] for e in webhook.events("run_end")] == ["cancelled"], "one run, one run_end"
    assert json.loads((run_dir / notify.TOLD).read_text())["time"]


def test_approval_needed_for_runs_sessions_and_loops(repo, plugin, webhook):
    priced = {"backend": "fake", "gpu": "FAKE1"}
    configure(repo, {"webhook": webhook.url}, fake={"usd_per_hour": 3600},                    # $60 a minute
              budget={"max_run_usd": 100, "ask_above_usd": 0.5, "max_day_usd": 1000},
              plans={"score": {**priced, "steps": [{**PLAN, "max_minutes": 0.1}]},             # worst case $66
                     "big": {**priced, "steps": [{**PLAN, "max_minutes": 10}]}})              # $660: refused outright
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "priced")
    r = cli(repo, "run", "score", env=plugin)
    assert r.returncode == 2 and "Needs approval" in r.stdout, r.stdout
    [asked] = webhook.events("approval_needed")
    assert asked["what"] == "plan score" and asked["backend"] == "fake" and asked["gpu"] == "FAKE1"
    assert asked["worst_case_usd"] == 66.0 and asked["approve_with"] == "testbench run score --approve"
    assert asked["text"] == ("agent-testbench, scoring: needs your approval: plan score could cost up to $66.00 "
                             "(above budget.ask_above_usd $0.50). On fake FAKE1. The agent was told to wait for a "
                             "person's yes: tell it yes to let it go ahead (`testbench run score --approve`), "
                             "or no."), asked["text"]
    assert asked["status"] == "waiting for approval" and asked["message"].startswith("Needs approval: plan score")

    r = cli(repo, "session", "start", "--backend", "fake", "--gpu", "FAKE1", "--max-minutes", "0.5", env=plugin)
    assert r.returncode == 2 and "Needs approval" in r.stdout, r.stdout
    asked = webhook.events("approval_needed")[-1]
    assert asked["what"] == "session main" and asked["worst_case_usd"] == 90.0
    assert asked["approve_with"] == ("testbench session start main --backend fake --gpu FAKE1 --max-minutes 0.5 "
                                     "--approve")

    r = cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "67", "--patience", "3",
            env=plugin)
    assert r.returncode == 2 and "Needs approval: a loop on plan score" in r.stdout, r.stdout
    asked = webhook.events("approval_needed")[-1]
    assert asked["what"] == "a loop on plan score" and asked["worst_case_usd"] == 67.0 and asked["gpu"] == "FAKE1"
    assert asked["approve_with"] == ("testbench loop start score --metric metrics.json:score --budget-usd 67 "
                                     "--patience 3 --approve")

    for argv in (("run", "big"), ("loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "50")):
        r = cli(repo, *argv, env=plugin)
        assert r.returncode == 2 and "Needs approval" not in r.stdout, r.stdout
    assert len(webhook.events("approval_needed")) == 3, "a hard refusal is not a question for the person"
    assert not webhook.events("run_end"), "nothing ran"


def test_on_picks_the_events(repo, webhook):
    (repo / "testbench.yaml").write_text((repo / "testbench.yaml").read_text()
                                         + f"notify:\n  webhook: {webhook.url}\n  on: approval_needed\n")
    assert cli(repo, "run", "score").returncode == 0
    assert not webhook.bodies and not (repo / ".testbench" / "notify.log").exists()


def test_a_dead_or_slow_webhook_does_not_break_a_run(repo, monkeypatch):
    with socket.socket() as s:                    # a port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    configure(repo, {"webhook": f"http://127.0.0.1:{port}{Webhook.SECRET}"})
    began = time.time()
    r = cli(repo, "run", "score")
    assert r.returncode == 0 and "PASS" in r.stdout and time.time() - began < 60
    path = repo / ".testbench" / "notify.log"          # the driver notifies after the run is done; on Windows a
    for _ in range(80):                                # refused connection takes a couple of seconds
        if path.exists() and "run_end" in path.read_text():
            break
        time.sleep(0.25)
    log = path.read_text()
    assert f"run_end to http://127.0.0.1:{port} (notify.webhook in testbench.yaml): not accepted" in log, log
    t = cli(repo, "notify", "test")
    assert t.returncode == 1 and "not accepted" in t.stdout and ".testbench/notify.log" in t.stdout, t.stdout
    never_shown(repo, r.stdout + r.stderr, t.stdout + t.stderr, log)

    from agent_testbench import config as C, notify
    hook = Webhook()
    hook.hang = True                              # takes the request and never answers
    try:
        configure(repo, {"webhook": hook.url})
        monkeypatch.setattr(notify, "TIMEOUT", 0.5)
        began = time.time()
        ok, what = notify.send(repo, C.load(repo), "run_end", "slow", {})
        assert ok is False and "no answer within 0.5 s" in what and time.time() - began < 3, what
    finally:
        hook.server.shutdown()


def test_webhook_env_keeps_the_url_out_of_the_file(repo, webhook):
    configure(repo, {"webhook_env": "TB_HOOK"})
    assert "127.0.0.1" not in (repo / "testbench.yaml").read_text()
    r = cli(repo, "run", "score", env={"TB_HOOK": webhook.url})
    assert r.returncode == 0 and webhook.events("run_end")[0]["status"] == "passed"
    r = cli(repo, "run", "score")
    assert r.returncode == 0, "an unset variable does not stop the run"
    assert "TB_HOOK, which is not set in this environment" in (repo / ".testbench" / "notify.log").read_text()
    t = cli(repo, "notify", "test")
    assert t.returncode == 2 and "nothing was sent: notify.webhook_env names TB_HOOK, which is not set" in t.stdout
    t = cli(repo, "notify", "test", env={"TB_HOOK": "hooks.example.com/SECRET"})
    assert t.returncode == 2 and "$TB_HOOK does not hold an http:// or https:// URL" in t.stdout
    t = cli(repo, "notify", "test", env={"TB_HOOK": webhook.url})
    assert t.returncode == 0 and "($TB_HOOK): accepted (HTTP 200)" in t.stdout and "written in" not in t.stdout
    never_shown(repo, r.stdout, t.stdout)


def test_notify_test(repo, webhook):
    t = cli(repo, "notify", "test")
    assert t.returncode == 2 and "no `notify:` section" in t.stdout and "webhook_env:" in t.stdout
    configure(repo, {"webhook": webhook.url, "on": ["loop_end"]})
    t = cli(repo, "notify", "test")
    host = webhook.url.split("/services")[0]
    assert t.returncode == 0 and f"Test message to {host} (notify.webhook in testbench.yaml): accepted (HTTP 200)." \
        in t.stdout, t.stdout
    assert "webhook_env: NAME" in t.stdout, "the URL in the file is worth a word"
    [sent] = webhook.bodies
    assert sent["event"] == "test" and "Notifications for loop_end, loop_rule go here" in sent["text"]
    webhook.status = 404
    t = cli(repo, "notify", "test")
    assert t.returncode == 1 and "not accepted (HTTP 404 Not Found: no_service)" in t.stdout, t.stdout
    never_shown(repo, t.stdout + t.stderr)


@pytest.mark.parametrize("notify, says", [
    ("https://hooks.example.com/services/SECRET", "`notify:` is a mapping"),
    ({}, "`notify` needs one of `webhook:`"),
    ({"webhook": "https://a.example/SECRET", "webhook_env": "HOOK"}, "`notify` needs one of `webhook:`"),
    ({"webhook": "hooks.example.com/services/SECRET"}, "`notify.webhook` is a URL that takes a JSON POST"),
    ({"webhook_env": "https://hooks.example.com/services/SECRET"}, "the NAME of an environment variable"),
    ({"webhook": "https://a.example/SECRET", "on": ["run_start"]}, "there is no 'run_start'"),
    ({"webhook": "https://a.example/SECRET", "on": []}, "`notify.on` lists the events to send"),
    ({"webhook": "https://a.example/SECRET", "channel": "#ops"}, "`notify` has no 'channel'"),
])
def test_config_says_what_to_write(tmp_path, notify, says):
    from agent_testbench import config as C
    with pytest.raises(C.ConfigError) as exc:
        C.validate({"project": "p", "notify": notify, "plans": {}}, tmp_path)
    assert says in str(exc.value) and "SECRET" not in str(exc.value), str(exc.value)


def test_config_reads_a_bare_on_and_defaults_to_every_event(tmp_path):
    from agent_testbench import config as C
    (tmp_path / "testbench.yaml").write_text("project: p\nnotify:\n  webhook_env: HOOK\n  on: [loop_end]\nplans: {}\n")
    assert C.load(tmp_path)["notify"] == {"webhook": None, "webhook_env": "HOOK", "on": ["loop_end", "loop_rule"]}, \
        "a list written when a rule firing was sent as loop_end still hears it"
    cfg = C.validate({"project": "p", "notify": {"webhook": "https://a.example/x"}, "plans": {}}, tmp_path)
    assert cfg["notify"]["on"] == ["run_end", "loop_rule", "loop_end", "approval_needed"]
    cfg = C.validate({"project": "p", "notify": {"webhook": "https://a.example/x", "on": ["loop_rule"]}, "plans": {}},
                     tmp_path)
    assert cfg["notify"]["on"] == ["loop_rule"]
    assert C.validate({"project": "p", "plans": {}}, tmp_path)["notify"] is None


def guard(root: Path, tool: str, inp: dict) -> dict | None:
    p = subprocess.run([sys.executable, str(REPO / "hooks" / "guard.py")], capture_output=True, text=True, timeout=30,
                       input=json.dumps({"cwd": str(root), "tool_name": tool, "tool_input": inp}))
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout.strip() else None


def test_the_guard_lets_notify_edits_through_and_keeps_the_url_secret(repo):
    yaml_path = str(repo / "testbench.yaml")
    current = (repo / "testbench.yaml").read_text()
    notify = "notify:\n  webhook: https://hooks.example.com/services/T/B/X\n  on: [run_end, loop_end]\n"
    assert guard(repo, "Write", {"file_path": yaml_path, "content": current + notify}) is None
    assert guard(repo, "Edit", {"file_path": yaml_path, "old_string": "project: scoring",
                                "new_string": "project: scoring\nnotify: {webhook_env: HOOK, on: loop_end}"}) is None
    raised = guard(repo, "Write", {"file_path": yaml_path, "content": current + notify
                                   + "budget: {max_run_usd: 50}\n"})
    assert raised["permissionDecision"] == "ask" and "budget.max_run_usd raised" in raised["permissionDecisionReason"]
    for cmd in ("echo $SLACK_WEBHOOK_URL", "printenv TESTBENCH_WEBHOOK"):
        assert guard(repo, "Bash", {"command": cmd})["permissionDecision"] == "deny", cmd
    assert guard(repo, "Bash", {"command": "testbench notify test"}) is None
