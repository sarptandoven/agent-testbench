"""The ssh backend: plans and live sessions on a machine reached by ssh. A stand-in for `ssh` drops its options and
the host and runs the command here, so every remote command the backend builds runs for real (set
TESTBENCH_TEST_SSH_HOST to also run against a real host; see test_live.py)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from agent_testbench import config as C
from conftest import cli, make_notebook, write_config

SHIM = '''\
"""Stands in for ssh: skips ssh's options and the host, runs the command on this machine."""
import subprocess, sys
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-p", "-i", "-l", "-F", "-J", "-S") else 1
sys.exit(subprocess.run(" ".join(args[i + 1:]), shell=True).returncode)
'''


@pytest.fixture
def box(tmp_path) -> dict:
    """The ssh section for a 'machine' that is this one, with its own folder as the remote workdir."""
    shim = tmp_path / "fake_ssh.py"
    shim.write_text(SHIM)
    return {"host": "box", "command": [sys.executable, str(shim)], "workdir": str(tmp_path / "remote"),
            "python": sys.executable, "setup": "none", "usd_per_hour": 1.2}


@pytest.fixture
def project(tmp_path, box) -> Path:
    root = tmp_path / "proj"
    make_notebook(root / "nb.ipynb", [
        'N = 3 #@param {type:"integer"}',
        "import json, os, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\nprint('token', os.environ.get('TB_TOKEN'))",
        "json.dump({'n': N}, open('out/result.json', 'w'))",
    ])
    write_config(root, {"project": "proj", "ssh": box, "budget": {"max_run_usd": 1, "ask_above_usd": 1, "max_day_usd": 5},
                        "plans": {"remote": {"backend": "ssh", "steps": [
                            {"name": "where", "run": "pwd; echo on-the-box", "max_minutes": 1,
                             "expect": {"text": {"stdout": ["on-the-box"]}}},
                            {"name": "notebook", "notebook": "nb.ipynb", "params": {"N": 7}, "max_minutes": 3,
                             "secrets": ["TB_TOKEN"], "artifacts": ["out/*"],
                             "expect": {"json": {"out/result.json": {"n": "== 7"}}}}]},
                            "slow": {"backend": "ssh", "steps": [{"name": "nap", "run": "echo napping; sleep 120",
                                                                    "max_minutes": 5}]}}})
    return root


def test_a_plan_runs_on_the_machine_and_comes_back(project, box):
    r = cli(project, "plans")
    assert "remote: ssh, 2 step(s), up to 4 min, worst case $0.12" in r.stdout, r.stdout     # 4 + 2 start-up min at $1.2/h
    r = cli(project, "run", "remote", "--wait", "180", env={"TB_TOKEN": "s3cret", "TESTBENCH_SSH_POLL_SECONDS": "0.5"})
    assert r.returncode == 0 and "# PASS - plan `remote` on ssh" in r.stdout, r.stdout + r.stderr
    run = sorted((project / ".testbench" / "runs").iterdir())[-1]
    assert (run / "steps" / "02-notebook" / "artifacts" / "out" / "result.json").exists(), "evidence came back"
    assert "token s3cret" in (run / "steps" / "02-notebook" / "log.txt").read_text(), "secrets reach the machine"
    assert "/remote/proj/runs/" in (run / "steps" / "01-where" / "log.txt").read_text(), "it ran in the machine's copy"
    assert "on the machine itself" in json.loads((run / "state.json").read_text())["remote_stop"]
    assert not (Path(box["workdir"]) / "proj" / "runs" / run.name).exists(), "the run's folder there is removed"


def test_cancel_stops_the_work_on_the_machine(project, box):
    r = cli(project, "run", "slow", "--detach", env={"TESTBENCH_SSH_POLL_SECONDS": "0.5"})
    assert r.returncode == 3
    run = sorted((project / ".testbench" / "runs").iterdir())[-1]
    for _ in range(120):
        if json.loads((run / "state.json").read_text()).get("ssh_pid"):
            break
        time.sleep(0.25)
    pid = json.loads((run / "state.json").read_text())["ssh_pid"]
    log = run / "steps" / "01-nap" / "log.txt"                 # the step is running there (its log comes back)
    for _ in range(120):
        if log.exists() and "napping" in log.read_text():
            break
        time.sleep(0.25)
    assert "cancelled" in cli(project, "cancel").stdout
    for _ in range(40):
        try:
            os.kill(pid, 0)
            time.sleep(0.25)
        except ProcessLookupError:
            break
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    state = json.loads((run / "state.json").read_text())
    assert state["status"] == "cancelled"
    remote = Path(box["workdir"]) / "proj" / "runs" / run.name
    assert not remote.exists(), "a cancelled run's folder there is removed too"
    # found by its folder there, which only this test's run has; another test's run can have the same name
    watchdogs = []
    for d in Path("/proc").iterdir() if Path("/proc").is_dir() else ():
        try:
            if d.name.isdigit() and state["ssh_dir"].encode() in (d / "cmdline").read_bytes():
                watchdogs.append(d.name)
        except OSError:                  # a process that ended while it was looked at
            pass
    assert not watchdogs, "and its watchdog is gone"


def test_a_live_session_on_the_machine(project, box):
    env = {"TB_TOKEN": "s3cret"}
    r = cli(project, "session", "start", "box", "--backend", "ssh", "--max-minutes", "5", "--secret", "TB_TOKEN", env=env)
    assert r.returncode == 0 and "ready on ssh" in r.stdout and "Hard stop on the machine itself" in r.stdout, r.stdout
    remote = Path(box["workdir"]) / "proj" / "sessions" / "box"
    assert (remote / "kernel" / "watchdog.log").exists(), "the watchdog starts with the session"
    cli(project, "exec", "x = 21")
    assert "=> 42" in cli(project, "exec", "x * 2").stdout, "variables persist"
    assert "=> 's3cret'" in cli(project, "exec", "import os; os.environ['TB_TOKEN']").stdout
    out = cli(project, "exec", "--cells", "nb.ipynb", "--params", '{"N": 5}').stdout
    assert "token s3cret" in out
    assert "out/" in cli(project, "files", "ls").stdout
    got = project / "got.json"
    cli(project, "files", "get", "out/result.json", str(got))
    assert json.loads(got.read_text()) == {"n": 5}
    (project / "local.txt").write_text("from here")
    cli(project, "files", "put", str(project / "local.txt"), "sent/local.txt")
    assert "from here" in cli(project, "session", "shell", "cat sent/local.txt").stdout
    (project / "new_module.py").write_text("VALUE = 'synced'\n")
    assert "copied" in cli(project, "session", "sync").stdout
    assert "=> 'synced'" in cli(project, "exec", "import new_module; new_module.VALUE").stdout
    assert "stopped after" in cli(project, "session", "stop").stdout
    assert not remote.exists(), "the session's folder there is removed"


def test_the_ssh_section_is_checked(tmp_path):
    write_config(tmp_path, {"project": "p", "plans": {"p": {"backend": "ssh", "steps": [{"name": "a", "run": "true"}]}}})
    with pytest.raises(C.ConfigError, match="ssh.host"):
        C.load(tmp_path)
    write_config(tmp_path, {"project": "p", "ssh": {"host": "h", "command": "ssh -p 2222", "usd_per_hour": 0.5},
                            "plans": {"p": {"backend": "ssh", "steps": [{"name": "a", "run": "true"}]}}})
    assert C.load(tmp_path)["ssh"]["command"] == ["ssh", "-p", "2222"]


def test_a_plan_past_its_limit_is_stopped_from_here(project, box):
    """If the work on the machine stopped its watchdog, this machine stops the plan shortly after the limit (made
    short here: the grace after the limit can be lowered, never raised)."""
    cfg = C.yaml.safe_load((project / "testbench.yaml").read_text())
    cfg["plans"]["stuck"] = {"backend": "ssh", "steps": [{"name": "nap", "run": "sleep 50", "max_minutes": 1}]}
    write_config(project, cfg)
    began = time.time()
    r = cli(project, "run", "stuck", "--wait", "120",
            env={"TESTBENCH_SSH_POLL_SECONDS": "0.5", "TESTBENCH_SSH_GRACE_SECONDS": "-170"})
    assert r.returncode != 0 and "stopped from this machine" in r.stdout, r.stdout + r.stderr
    assert time.time() - began < 45, "it did not wait for the step to end on its own"
