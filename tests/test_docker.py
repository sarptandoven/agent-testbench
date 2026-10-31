"""The docker backend: plans and live sessions in a fresh container on this machine, against a real Docker daemon
(skipped where `docker info` does not answer). The image comes from mirror.gcr.io, Docker Hub's mirror, which is not
rate-limited (TESTBENCH_TEST_DOCKER_IMAGE picks another); testbench's derived image is built once per test session."""
import base64
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pytest

from agent_testbench import config as C, runs, sessions as S
from agent_testbench.backends import docker_backend as D
from conftest import PNG_1PX, cli, make_notebook, write_config


def _docker_answers() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


needs_docker = pytest.mark.skipif(not _docker_answers(), reason="needs a Docker daemon that answers `docker info`")
IMAGE = os.environ.get("TESTBENCH_TEST_DOCKER_IMAGE", "mirror.gcr.io/library/python:3.11-slim")
SECRET = f"s3cret-{uuid.uuid4().hex[:12]}"           # unique, so finding it anywhere means it leaked from this test


def _build_settings() -> dict:
    """What building the image needs on this machine. Behind a proxy on this machine's own loopback, the build has to
    share this machine's network to reach it; a CA bundle in PIP_CERT (a TLS-intercepting proxy) goes into the build.
    On a machine with plain internet access, such as GitHub's runners, neither applies."""
    out = {}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy and urlparse(proxy).hostname in ("127.0.0.1", "localhost", "::1"):
        out["build_args"] = ["--network", "host"]
    if os.environ.get("PIP_CERT") and Path(os.environ["PIP_CERT"]).is_file():
        out["ca_bundle"] = os.environ["PIP_CERT"]
    return out


@pytest.fixture(scope="session")
def section(tmp_path_factory) -> dict:
    """The docker section every test uses, with its image built once (later builds find it by its tag)."""
    s = {"image": IMAGE, **_build_settings()}
    tmp = tmp_path_factory.mktemp("docker-image")
    cfg = {"project": "warm", "root": str(tmp), "plans": {}, "sessions": {"backend": "local"},
           "docker": {**D.backend.config_defaults["docker"], **s}}
    D.backend.validate(cfg)
    D.ensure_image(cfg, D.Docker(["docker"], tmp / "build.log"))
    return s


def _container(name: str) -> dict | None:
    p = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    return json.loads(p.stdout)[0] if p.returncode == 0 else None


def _gone(name: str, seconds: float = 30) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if _container(name) is None:
            return True
        time.sleep(0.25)
    return False


def _latest_run(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir())[-1]


def _wait_for_step(run: Path, seconds: float = 180) -> dict:
    """The run's state once its first step has started in the container (progress comes back as it runs)."""
    end = time.time() + seconds
    while time.time() < end:
        if any(e.get("event") == "step_start" for e in runs.progress(run)):
            return runs.read_state(run)
        time.sleep(0.25)
    raise AssertionError(f"no step started; driver.log:\n{(run / 'driver.log').read_text()[-2000:]}")


@pytest.fixture
def project(tmp_path, section) -> Path:
    root = tmp_path / "proj"
    make_notebook(root / "nb.ipynb", [
        'N = 3 #@param {type:"integer"}',
        "import json, os, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\nprint('token', os.environ.get('TB_TOKEN'))",
        "json.dump({'n': N}, open('out/result.json', 'w'))",
    ])
    write_config(root, {"project": "proj", "docker": {**section, "usd_per_hour": 0.6},
                        "budget": {"max_run_usd": 1, "ask_above_usd": 1, "max_day_usd": 5},
                        "plans": {"boxed": {"backend": "docker", "steps": [
                            {"name": "where", "run": "pwd; test -f /.dockerenv && echo in-a-container", "max_minutes": 1,
                             "expect": {"text": {"stdout": ["/work/project", "in-a-container"]}}},
                            {"name": "notebook", "notebook": "nb.ipynb", "params": {"N": 7}, "max_minutes": 3,
                             "secrets": ["TB_TOKEN"], "artifacts": ["out/*"],
                             "expect": {"json": {"out/result.json": {"n": "== 7"}}}}]},
                            "slow": {"backend": "docker", "steps": [{"name": "nap", "run": "echo napping; sleep 120",
                                                                      "max_minutes": 5}]},
                            "killed": {"backend": "docker", "steps": [{"name": "kill", "max_minutes": 1,  # as OOM would:
                                       "run": "echo ending the executor; kill -9 $PPID; sleep 30"}]}}})
    return root


@needs_docker
def test_a_plan_runs_in_a_fresh_container_and_its_evidence_comes_back(project):
    r = cli(project, "plans")
    assert "boxed: docker, 2 step(s), up to 4 min, worst case $0.06" in r.stdout, r.stdout   # 4 + 2 start-up min, $0.6/h
    r = cli(project, "run", "boxed", "--wait", "300", env={"TB_TOKEN": SECRET})
    assert r.returncode == 0 and "# PASS - plan `boxed` on docker" in r.stdout, r.stdout + r.stderr
    run = _latest_run(project)
    assert (run / "steps" / "02-notebook" / "artifacts" / "out" / "result.json").exists(), "evidence came back"
    assert list((run / "steps" / "02-notebook").glob("*.ipynb")), "the executed notebook too"
    assert f"token {SECRET}" in (run / "steps" / "02-notebook" / "log.txt").read_text(), "secrets reach the container"
    assert not (project / "out").exists(), "the plan ran in the container's copy, not in the project"
    assert all(p.stat().st_uid == os.getuid() for p in run.rglob("*")), "what comes back belongs to whoever ran it"
    state = runs.read_state(run)
    assert "inside the container" in state["remote_stop"]
    assert "watching: stop at 6 min" in (run / "watchdog" / "watchdog.log").read_text(), "the hard stop was armed"
    commands = (run / "docker.log").read_text()
    assert "-e TB_TOKEN" in commands and SECRET not in commands, "secrets go to docker by name, never by value"
    assert json.loads((run / "report.json").read_text())["environment"]["python"].startswith("3.11"), "the image's Python"
    assert _gone(state["docker_container"], 5), "the container is removed"


@needs_docker
def test_a_plan_whose_executor_dies_in_the_container_is_an_error(project):
    r = cli(project, "run", "killed", "--wait", "300")
    run = _latest_run(project)
    rep = json.loads((run / "report.json").read_text())
    assert r.returncode != 0 and rep["status"] == "error", r.stdout
    assert "ended before the plan finished" in rep["stopped_because"]
    assert "ending the executor" in (run / "steps" / "01-kill" / "log.txt").read_text(), "what it did so far came back"
    assert _gone(runs.read_state(run)["docker_container"], 5)


@needs_docker
def test_network_none_is_a_sandbox_without_a_network(tmp_path, section):
    root = tmp_path / "sandbox"
    root.mkdir()
    reach = "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 443), timeout=5); print('reached it')\""
    nics = "python3 -c \"import os; print('interfaces:', sorted(os.listdir('/sys/class/net')))\""
    write_config(root, {"project": "sandbox", "docker": {**section, "network": "none"}, "plans": {"offline": {
        "backend": "docker", "steps": [
            {"name": "reach out", "run": reach, "max_minutes": 1, "continue_on_failure": True,
             "expect": {"exit_code": 1, "text": {"stdout": ["Network is unreachable"]}}},
            {"name": "interfaces", "run": nics, "max_minutes": 1, "expect": {"text": {"stdout": ["interfaces: ['lo']"]}}}]}}})
    r = cli(root, "run", "offline", "--wait", "300")
    assert r.returncode == 0 and "# PASS - plan `offline` on docker" in r.stdout, r.stdout + r.stderr
    assert "reached it" not in (_latest_run(root) / "steps" / "01-reach-out" / "log.txt").read_text()


@needs_docker
def test_a_live_session_in_a_container(project):
    env = {"TB_TOKEN": SECRET}
    r = cli(project, "session", "start", "box", "--backend", "docker", "--max-minutes", "5", "--secret", "TB_TOKEN", env=env)
    assert r.returncode == 0 and "ready on docker" in r.stdout and "Hard stop inside the container" in r.stdout, \
        r.stdout + r.stderr
    sdir = project / ".testbench" / "sessions" / "box"
    name = S.read(sdir)["handles"]["container"]
    box = _container(name)
    assert box and box["State"]["Running"]
    cli(project, "exec", "x = 21")
    assert "=> 42" in cli(project, "exec", "x * 2").stdout, "variables persist"
    assert f"=> '{SECRET}'" in cli(project, "exec", "import os; os.environ['TB_TOKEN']").stdout, "the secret is there"
    shown = json.dumps([box["Path"], box["Args"], box["Config"]["Cmd"], box["Config"].get("Entrypoint")])
    assert SECRET not in shown, "and not on the container's command line"
    commands = (sdir / "docker.log").read_text()
    assert "-e TB_TOKEN" in commands and SECRET not in commands, "nor on any docker command testbench ran"
    if Path("/proc").is_dir():
        leaked = []
        for d in Path("/proc").iterdir():
            try:
                if d.name.isdigit() and SECRET.encode() in (d / "cmdline").read_bytes():
                    leaked.append(d.name)
            except OSError:
                pass
        assert not leaked, "nor on any command line on this machine"

    png = base64.b64encode(PNG_1PX).decode()
    out = cli(project, "exec", f"from IPython.display import Image, display; import base64\n"
                               f"display(Image(data=base64.b64decode('{png}')))").stdout
    images = [ln.split("image: ", 1)[1] for ln in out.splitlines() if ln.startswith("image: ")]
    assert images and Path(images[0]).read_bytes() == PNG_1PX, out
    assert Path(images[0]).is_relative_to(sdir / "outputs"), "an image displayed comes back as a file here"

    out = cli(project, "exec", "--cells", "nb.ipynb", "--params", '{"N": 5}').stdout
    assert f"token {SECRET}" in out
    assert "out/" in cli(project, "files", "ls").stdout
    got = project / "got.json"
    cli(project, "files", "get", "out/result.json", str(got))
    assert json.loads(got.read_text()) == {"n": 5}
    (project / "local.txt").write_text("from here")
    cli(project, "files", "put", str(project / "local.txt"), "sent/local.txt")
    out = cli(project, "session", "shell", "cat sent/local.txt; pwd").stdout
    assert "from here" in out and "/work/project" in out
    (project / "new_module.py").write_text("VALUE = 'synced'\n")
    assert "copied" in cli(project, "session", "sync").stdout
    assert "=> 'synced'" in cli(project, "exec", "import new_module; new_module.VALUE").stdout
    assert "=> 21" in cli(project, "exec", "x").stdout, "a sync keeps the kernel's variables"
    assert "stopped after" in cli(project, "session", "stop").stdout
    assert _gone(name, 10), "stop removes the container"


@needs_docker
def test_cancel_stops_the_container(project):
    r = cli(project, "run", "slow", "--detach")
    assert r.returncode == 3, r.stdout + r.stderr
    run = _latest_run(project)
    name = _wait_for_step(run)["docker_container"]
    assert _container(name)["State"]["Running"]
    assert "cancelled" in cli(project, "cancel").stdout
    assert _gone(name, 10), "cancel removes the container"
    assert runs.read_state(run)["status"] == "cancelled"
    assert "napping" in (run / "steps" / "01-nap" / "log.txt").read_text(), "and what it did so far came back"

    r = cli(project, "run", "slow", "--detach")                  # with its driver gone, cancel works from state.json
    run = _latest_run(project)
    state = _wait_for_step(run)
    os.killpg(state["driver_pid"], signal.SIGKILL)
    time.sleep(0.5)
    assert _container(state["docker_container"])["State"]["Running"], "the container outlives the driver"
    # what `testbench cancel` does once the driver is gone (called directly: where the killed driver is not reaped
    # at once, cancel first waits a minute for it)
    assert D.backend.cancel_remote(runs.read_state(run)) == f"removed the container {state['docker_container']}"
    assert _gone(state["docker_container"], 10)


@needs_docker
def test_the_hard_stop_fires_inside_the_container(project, monkeypatch):
    """A tiny limit and a quick watchdog tick keep this short: nothing on this side stops the work."""
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.2")        # passed to the container's watchdog by name
    monkeypatch.setattr(D, "plan_limit", lambda plan: 0.1)         # 6 s, far inside the step's own 5 minutes
    run = runs.launch(project, C.load(project), "slow", foreground=True)
    rep = json.loads((run / "report.json").read_text())
    assert rep["status"] == "cancelled" and "napping" in (run / "steps" / "01-nap" / "log.txt").read_text()
    assert "reached max_minutes (0.1) on the machine itself" in (run / "watchdog" / "watchdog_stopped").read_text()
    assert _gone(runs.read_state(run)["docker_container"], 10)

    monkeypatch.setattr(D.backend, "startup_minutes", 0.0)         # a session's stop: max_minutes, no start-up allowance
    s = S.start(project, C.load(project), "brief", backend="docker", max_minutes=0.1)
    os.killpg(s["supervisor_pid"], signal.SIGKILL)                 # the supervisor here would stop it; it is gone now
    assert _container(s["handles"]["container"])["State"]["Running"]
    assert _gone(s["handles"]["container"], 60), "the container's own watchdog ends the session, and the container goes"


def test_the_docker_section_is_checked(tmp_path):
    def load(docker: dict, **plan):
        write_config(tmp_path, {"project": "p", "docker": docker, "plans": {"p": {"backend": "docker", **plan,
                                                                                   "steps": [{"name": "a", "run": "true"}]}}})
        return C.load(tmp_path)

    cfg = load({"run_args": "-v /data:/data:ro", "command": "podman", "memory_gb": 4})
    assert cfg["docker"]["run_args"] == ["-v", "/data:/data:ro"] and cfg["docker"]["command"] == ["podman"]
    assert cfg["plans"]["p"]["workdir"] == "copy", "plans run in a copy"
    for docker, plan, message in (
            ({"network": None}, {}, "write bridge .*, none \\(no network at all: a sandbox\\)"),
            ({"setup": "venv"}, {}, "`docker.setup` is build"),
            ({"usd_per_hour": -1}, {}, "docker.usd_per_hour"),
            ({"gpus": True}, {}, "what `docker run --gpus` takes"),
            ({"memory_gb": "4g"}, {}, "`docker.memory_gb` is a positive number"),
            ({"setup": "none", "pip": ["torch"]}, {}, "`docker.pip` needs docker.setup: build"),
            ({"ca_bundle": "missing.pem"}, {}, "missing.pem does not exist"),
            ({}, {"gpu": "A100"}, "`plans.p.gpu` asks for a GPU, but containers get no GPU unless docker.gpus is set")):
        with pytest.raises(C.ConfigError, match=message):
            load(docker, **plan)
    assert load({"gpus": "all"}, gpu="A100")["docker"]["gpus"] == "all", "with docker.gpus, gpu: is a label"
    r = cli(tmp_path, "prices")
    assert "$0.00/h for containers from python:3.11-slim (testbench.yaml docker.usd_per_hour)" in r.stdout, r.stdout


def test_the_image_and_the_container_follow_the_config(tmp_path, monkeypatch):
    base = {"project": "p", "root": str(tmp_path), "docker": dict(D.backend.config_defaults["docker"])}
    other = {**base, "docker": {**base["docker"], "pip": ["numpy==2.1.0"]}}
    assert D.image_tag(base) == D.image_tag(json.loads(json.dumps(base))), "the same config, the same image"
    assert D.image_tag(base) != D.image_tag(other), "another package, another image"
    assert " numpy==2.1.0 " in D.dockerfile(other) and " 'nbclient>=0.10' " in D.dockerfile(other), "quoted for the shell"

    monkeypatch.setenv("TB_TOKEN", SECRET)
    cfg = {"project": "p", "docker": {**base["docker"], "gpus": "all", "cpu": 2, "memory_gb": 1.5, "network": "none",
                                      "run_args": ["-v", "/data:/data:ro"]}}
    args = D.create_args(cfg, "testbench-p-r", "img", ["python3", "-V"], {"run": "r"}, ["TB_TOKEN"])
    shown = " ".join(args)
    assert "--rm --init --name testbench-p-r --network none" in shown, "removed when it ends, under docker's init"
    assert "--gpus all" in shown and "--cpus 2" in shown and "--memory 1536m" in shown
    assert args[args.index("TB_TOKEN") - 1] == "-e" and SECRET not in shown, "a secret goes by name"
    assert args[-5:] == ["-v", "/data:/data:ro", "img", "python3", "-V"], "run_args, then the image and its command"
