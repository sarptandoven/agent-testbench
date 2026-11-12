"""The runpod backend: plans and live sessions in pods of their own, through RunPod's REST API.

Nothing here reaches RunPod. A fake RunPod (a small HTTP server on 127.0.0.1, per test) answers the REST API
(/v2/pods), the GraphQL podTerminate a pod may send, and RunPod's HTTPS proxy. "Creating a pod" starts the pod's
real start command (the bootstrap) on this machine, with the environment RunPod gives a pod (RUNPOD_POD_ID, a
pod-scoped RUNPOD_API_KEY) and a folder of its own as /work; terminating it ends every process found with that
folder. So a plan really runs end to end, and the tests see who terminated each pod, and with which key."""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_testbench import budget, config as C, runs
from agent_testbench.backends import runpod_backend as R
from conftest import cli, make_notebook, write_config

SECRET = f"s3cret-{uuid.uuid4().hex[:12]}"           # unique, so finding it anywhere means it leaked from this test


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pids_in(folder: str) -> list[int]:
    """Processes with the folder on their command line or as their working folder: a fake pod's own."""
    mark, out = folder.encode(), []
    for d in Path("/proc").iterdir():
        if d.name.isdigit() and int(d.name) != os.getpid():
            try:
                cmd = (d / "cmdline").read_bytes()
                cwd = os.readlink(d / "cwd").encode()
            except OSError:
                continue
            if mark in cmd or cwd == mark or cwd.startswith(mark + b"/"):
                out.append(int(d.name))
    return out


class FakeRunPod:
    """RunPod's REST API, its GraphQL podTerminate and its HTTP proxy, for one test. Every call is kept in `calls`
    (method, path, who - "user" for the person's key, "pod:<id>" for a pod's own - and the request)."""

    def __init__(self, d: Path):
        self.d = d
        d.mkdir(parents=True, exist_ok=True)
        self.key = f"rpa_fake_{uuid.uuid4().hex}"
        self.pods: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.scenario: dict = {}
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                fake.handle(self, "GET")

            def do_POST(self):
                fake.handle(self, "POST")

            def do_DELETE(self):
                fake.handle(self, "DELETE")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{port}/v2"
        self.proxy = f"http://127.0.0.1:{port}/proxy/{{pod_id}}-{{port}}"

    # ---- what the tests look at --------------------------------------------------------------------------------
    def created(self) -> list[dict]:
        return [c["body"] for c in self.calls if c["method"] == "POST" and c["path"] == "/v2/pods" and c.get("made")]

    def api_calls(self) -> list[dict]:
        return [c for c in self.calls if not c["path"].startswith("/proxy/")]

    def wait_terminated(self, pod_id: str, timeout: float = 60) -> dict:
        end = time.time() + timeout
        while time.time() < end and not self.pods[pod_id].get("terminated"):
            time.sleep(0.1)
        return self.pods[pod_id]

    def running(self, pod_id: str) -> list[int]:
        return _pids_in(str(self.pods[pod_id]["dir"]))

    def gone(self, pod_id: str, timeout: float = 20) -> bool:
        end = time.time() + timeout
        while time.time() < end and self.running(pod_id):
            time.sleep(0.2)
        return not self.running(pod_id)

    # ---- the fake ----------------------------------------------------------------------------------------------
    def who(self, req) -> str | None:
        auth = req.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else None
        if token == self.key:
            return "user"
        return next((f"pod:{i}" for i, p in self.pods.items() if token and token == p["key"]), None)

    def handle(self, req, method: str) -> None:
        body = req.rfile.read(int(req.headers.get("Content-Length") or 0))
        path = req.path
        if path.startswith("/proxy/"):
            return self.proxy_to(req, method, path, body)
        who = self.who(req)
        call = {"method": method, "path": path, "who": who, "body": json.loads(body) if body else None}
        with self.lock:
            self.calls.append(call)
        if who is None:
            return self.reply(req, 401, {"error": "unauthorized"})
        m = re.fullmatch(r"/v2/pods/([A-Za-z0-9]+)", path)
        if method == "POST" and path == "/v2/pods":
            return self.create(req, call)
        if method == "GET" and path == "/v2/pods":
            return self.reply(req, 200, {"pods": [self.view(p) for p in self.pods.values() if not p.get("terminated")]})
        if m and method in ("GET", "DELETE"):
            pod = self.pods.get(m.group(1))
            if pod is None or pod.get("terminated") or (who != "user" and who != f"pod:{pod['id']}"):
                return self.reply(req, 404, {"error": "pod not found"})
            if method == "GET":
                return self.reply(req, 200, self.view(pod))
            return self.terminate(req, pod, who)
        if method == "POST" and path == "/graphql":
            found = re.search(r'podTerminate\(input: \{podId: "([A-Za-z0-9]+)"\}\)', call["body"]["query"])
            pod = self.pods.get(found.group(1)) if found else None
            if pod is None or pod.get("terminated") or (who != "user" and who != f"pod:{pod['id']}"):
                return self.reply(req, 200, {"errors": [{"message": "pod not found"}]})
            return self.terminate(req, pod, who, graphql=True)
        self.reply(req, 404, {"error": f"no route {method} {path}"})

    def reply(self, req, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        req.send_response(code)
        req.send_header("Content-Type", "application/json")
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def view(self, pod: dict) -> dict:
        status = pod["status"]
        if status == "RUNNING" and pod.get("proc") and pod["proc"].poll() is not None:
            status = "EXITED"
        elif status != "RUNNING" and pod["polls"] >= len(pod["before"]):
            status = pod["status"] = "RUNNING" if pod.get("proc") else status
        pod["polls"] += 1
        return {"id": pod["id"], "name": pod["body"]["name"], "status": status, "image": pod["body"]["image"],
                "cost": 1.0, "gpu": pod["body"].get("gpu"), "cpu": pod["body"].get("cpu")}

    def create(self, req, call: dict) -> None:
        sc, body = self.scenario, call["body"]
        if sc.get("lost"):                               # made, but the answer never arrives
            pod_id = f"lost{uuid.uuid4().hex[:8]}"
            self.pods[pod_id] = {"id": pod_id, "body": body, "dir": self.d / pod_id, "key": "-", "polls": 0,
                                 "before": [], "status": "STARTING", "proc": None}
            call["made"] = True
            req.close_connection = True
            return
        if sc.get("create"):
            code, message = sc["create"]
            return self.reply(req, code, {"error": message})
        assert body["name"] and body["image"] and ("gpu" in body) != ("cpu" in body), body
        pod_id = f"fake{len(self.pods) + 1}{uuid.uuid4().hex[:8]}"
        pdir = self.d / pod_id
        pdir.mkdir()
        pod = {"id": pod_id, "body": body, "dir": pdir, "key": f"podkey-{uuid.uuid4().hex}", "polls": 0,
               "before": sc.get("before", ["PROVISIONING", "STARTING"]), "status": "PROVISIONING", "proc": None}
        call["made"] = True
        self.pods[pod_id] = pod
        start = sc.get("start", "RUNNING")
        if start == "RUNNING":
            pod["port"] = _free_port()
            env = {"PATH": os.environ["PATH"], "HOME": str(pdir), "LANG": "C.UTF-8", **body.get("env", {}),
                   "RUNPOD_POD_ID": pod_id, "TESTBENCH_POD_WORK": str(pdir / "work"),
                   "TESTBENCH_POD_PORT": str(pod["port"]), "TESTBENCH_POD_BIND": "127.0.0.1",
                   "TESTBENCH_POD_RETRY_SECONDS": "0.5"}
            if not sc.get("no_pod_key"):
                env["RUNPOD_API_KEY"] = pod["key"]
            log = open(pdir / "container.log", "ab")
            pod["proc"] = subprocess.Popen(body.get("entrypoint", []) + body.get("cmd", []), env=env, cwd=pdir,
                                           stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                           start_new_session=True)
        else:
            pod["before"], pod["status"] = [], start                # ERROR, or STARTING for ever
        self.reply(req, 201, self.view(pod))

    def terminate(self, req, pod: dict, who: str, graphql: bool = False) -> None:
        if who.startswith("pod:") and self.scenario.get("pod_key_refused"):
            return self.reply(req, 403 if not graphql else 200,
                              {"error": "forbidden"} if not graphql else {"errors": [{"message": "forbidden"}]})
        pod.update(terminated=True, terminated_by=who, terminated_how="graphql" if graphql else "rest")
        self.reply(req, 200, {"data": {"podTerminate": None}} if graphql else {})
        threading.Thread(target=self.kill, args=(pod,), daemon=True).start()

    def kill(self, pod: dict) -> None:
        if pod.get("proc"):
            try:
                os.killpg(pod["proc"].pid, signal.SIGKILL)
            except OSError:
                pass
        for _ in range(3):
            for pid in _pids_in(str(pod["dir"])):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.2)
        if pod.get("proc"):
            pod["proc"].wait()

    def proxy_to(self, req, method: str, path: str, body: bytes) -> None:
        m = re.fullmatch(r"/proxy/([A-Za-z0-9]+)-(\d+)(/.*)", path)
        pod = self.pods.get(m.group(1)) if m else None
        with self.lock:
            self.calls.append({"method": method, "path": path, "who": "proxy",
                               "auth": req.headers.get("Authorization", "")})
        if pod is None or pod.get("terminated") or not pod.get("port"):
            return self.reply(req, 404, {"error": "no such pod"})
        out = urllib.request.Request(f"http://127.0.0.1:{pod['port']}{m.group(3)}", data=body or None, method=method,
                                     headers={k: v for k, v in req.headers.items() if k in ("Authorization",
                                                                                           "Content-Type")})
        try:
            with urllib.request.urlopen(out, timeout=90) as r:
                code, data = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, data = e.code, e.read()
        except (urllib.error.URLError, OSError):
            return self.reply(req, 502, {"error": "bad gateway"})            # what RunPod's proxy says till it is up
        req.send_response(code)
        req.send_header("Content-Length", str(len(data)))
        req.end_headers()
        req.wfile.write(data)

    def close(self) -> None:
        for pod in self.pods.values():
            if not pod.get("terminated"):
                pod["terminated"] = True
            self.kill(pod)
        self.server.shutdown()


# A fake pod is this machine's processes, found (and ended) by their folder in /proc; and the bootstrap forks.
pods = pytest.mark.skipif(not Path("/proc").is_dir(), reason="the fake's pods are found by their folder in /proc (Linux)")


@pytest.fixture
def fake(tmp_path, monkeypatch) -> FakeRunPod:
    monkeypatch.setenv("TESTBENCH_RUNPOD_POLL_SECONDS", "0.1")
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.2")
    f = FakeRunPod(tmp_path / "runpod")
    monkeypatch.setenv("RUNPOD_API_KEY", f.key)
    yield f
    f.close()


def _section(fake: FakeRunPod, **more) -> dict:
    return {"api_url": fake.url, "proxy_url": fake.proxy, "python": sys.executable, "setup": "none", **more}


def _project(tmp_path: Path, fake: FakeRunPod, **section) -> Path:
    root = tmp_path / "proj"
    make_notebook(root / "nb.ipynb", [
        'N = 3 #@param {type:"integer"}',
        "import json, os, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\n"
        "print('token', os.environ.get('TB_TOKEN'))",
        "json.dump({'n': N}, open('out/result.json', 'w'))",
    ])
    write_config(root, {"project": "proj", "runpod": _section(fake, **section),
                        "budget": {"max_run_usd": 5, "ask_above_usd": 5, "max_day_usd": 20},
                        "plans": {
                            "train": {"backend": "runpod", "gpu": "H100-SXM", "steps": [
                                {"name": "where", "run": "pwd; echo on-the-pod", "max_minutes": 1,
                                 "expect": {"text": {"stdout": ["on-the-pod"]}}},
                                {"name": "notebook", "notebook": "nb.ipynb", "params": {"N": 7}, "max_minutes": 3,
                                 "secrets": ["TB_TOKEN"], "artifacts": ["out/*"],
                                 "expect": {"json": {"out/result.json": {"n": "== 7"}}}}]},
                            "cpu": {"backend": "runpod", "steps": [{"name": "a", "run": "python -c 'print(6 * 7)'",
                                                                    "max_minutes": 1}]},
                            "slow": {"backend": "runpod", "steps": [{"name": "nap", "run": "echo napping; sleep 120",
                                                                     "max_minutes": 5}]},
                            "stuck": {"backend": "runpod", "steps": [{"name": "nap", "run": "sleep 300",
                                                                      "max_minutes": 1}]},
                            "big": {"backend": "runpod", "gpu": "H200:8", "steps": [{"name": "a", "run": "true",
                                                                                      "max_minutes": 60}]}}})
    return root


def _run(root: Path, plan: str) -> tuple[Path, dict]:
    run = runs.launch(root, C.load(root), plan, foreground=True)
    return run, json.loads((run / "report.json").read_text())


def _everything(root: Path) -> list[tuple[Path, bytes]]:
    return [(p, p.read_bytes()) for p in (root / ".testbench").rglob("*") if p.is_file()]


@pods
def test_a_plan_runs_in_a_pod_of_its_own_and_the_pod_is_terminated(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)
    monkeypatch.setenv("TB_TOKEN", SECRET)
    cfg = C.load(root)
    est = budget.worst_case(cfg, "train")
    assert est["worst_case_usd"] == round(R.USD_PER_HOUR["H100-SXM"] / 60 * (4 + 10), 4), \
        "one H100 SXM for 4 min of steps + 10 start-up, at the built-in price"
    began = time.time()
    run, rep = _run(root, "train")
    assert rep["status"] == "passed", rep
    assert (run / "steps" / "02-notebook" / "artifacts" / "out" / "result.json").exists(), "the evidence came back"
    assert "token [secret]" in (run / "steps" / "02-notebook" / "log.txt").read_text(), \
        "the secret reached the pod (the notebook printed it), and the value printed is not kept"
    assert "/work/project" in (run / "steps" / "01-where" / "log.txt").read_text(), "it ran in the pod's copy"

    body = fake.created()[0]
    assert body["gpu"] == {"id": "NVIDIA H100 80GB HBM3", "count": 1} and "cpu" not in body
    assert body["image"] == "python:3.11-slim" and body["cloud"] == "SECURE" and body["disk"] == 20
    assert body["ports"] == ["8000/http"], "the agent is reached through RunPod's HTTP proxy, no ssh or public IP"
    assert body["entrypoint"] == [sys.executable, "-c", R.BOOTSTRAP]
    boot = json.loads(body["cmd"][0])
    assert boot["minutes"] == R.plan_limit(cfg["plans"]["train"]) == 14, "the pod's deadline is the priced worst case"
    assert began <= boot["deadline"] - 14 * 60 <= time.time(), "counted from when the pod was made"
    assert body["env"]["TB_TOKEN"] == SECRET, "a secret goes by name, as the pod's environment"
    assert fake.key not in json.dumps(body), "the person's API key never goes to the pod"

    pod_id = next(iter(fake.pods))
    pod = fake.wait_terminated(pod_id, 5)
    assert pod["terminated"] and fake.gone(pod_id), "the pod is terminated and nothing of it runs on"
    deletes = [c for c in fake.api_calls() if c["method"] == "DELETE"]
    assert deletes and all(c["who"] in ("user", f"pod:{pod_id}") for c in deletes)
    proxied = [c for c in fake.calls if c["who"] == "proxy"]
    assert proxied and not any(fake.key in c["auth"] for c in proxied), "the pod's agent gets a token, never the key"
    state = runs.read_state(run)
    assert state["runpod_pod"] == pod_id and "by the pod itself" in state["remote_stop"]
    assert "by the pod itself" in rep["remote_stop"]
    assert rep["cost_usd"] > 0 and rep["cost_usd"] <= est["worst_case_usd"]
    leaks = [str(p) for p, data in _everything(root) if fake.key.encode() in data or SECRET.encode() in data]
    assert not leaks, "the API key and the secret are in no log, state, report or evidence"
    assert "POST /pods" in (run / "runpod.log").read_text() and (run / "pod.log").exists()


def test_the_budget_refuses_before_any_call_to_runpod(tmp_path, fake):
    root = _project(tmp_path, fake)
    with pytest.raises(runs.LaunchRefused, match="max_run_usd"):
        runs.launch(root, C.load(root), "big")
    r = cli(root, "run", "big", env={"RUNPOD_API_KEY": fake.key})
    assert r.returncode == 2 and "max_run_usd" in r.stdout, r.stdout + r.stderr
    assert fake.calls == [], "nothing reached RunPod: no pod was made"


@pods
def test_cancel_terminates_the_pod(tmp_path, fake):
    root = _project(tmp_path, fake)
    run = runs.launch(root, C.load(root), "slow", foreground=False)        # a driver in the background
    log = run / "steps" / "01-nap" / "log.txt"
    end = time.time() + 90
    while time.time() < end and not (log.exists() and "napping" in log.read_text()):
        time.sleep(0.1)
    assert log.exists() and "napping" in log.read_text(), "the step never ran in the pod"
    assert "cancelled" in runs.cancel(run)
    state = runs.read_state(run)
    assert state["status"] == "cancelled"
    pod = fake.pods[state["runpod_pod"]]
    assert pod["terminated"] and pod["terminated_by"] in ("user", f"pod:{pod['id']}")
    assert fake.gone(pod["id"]), "the step's process in the pod is gone with it"
    rep = json.loads((run / "report.json").read_text())
    assert rep["status"] == "cancelled" and "terminated pod" in rep["stopped_because"]
    assert "napping" in log.read_text(), "what the step wrote came back"

    # the driver is gone: cancel works from state.json alone
    made = R.Api(fake.url, fake.key).create({"name": "left", "image": "x", "cpu": {"id": "cpu3c", "vcpuCount": 2},
                                             "entrypoint": [sys.executable, "-c", "import time; time.sleep(300)"],
                                             "cmd": []})
    assert R.backend.cancel_remote({"runpod_pod": made["id"], "runpod_api": fake.url}) == f"terminated pod {made['id']}"
    assert fake.pods[made["id"]]["terminated_by"] == "user" and fake.gone(made["id"])
    assert R.backend.cancel_remote({}) == ""
    before = len(fake.calls)
    msg = R.backend.cancel_remote({"runpod_pod": "x", "runpod_api": "https://example.com/v2"})
    assert "only goes to RunPod itself" in msg and len(fake.calls) == before, "a state.json cannot send the key away"


def _pod(fake: FakeRunPod, root: Path, minutes: float) -> R.Pod:
    """A pod made the way a run makes one, then left alone: this machine stops talking to it."""
    cfg = C.load(root)
    pod = R.Pod(cfg, R.Api(fake.url, fake.key), None, say=lambda *a, **kw: None)
    pod.create("tb-proj-alone", {"gpu": None}, minutes, R.pod_token(fake.key, "n0nce"), {})
    return pod


@pods
def test_the_pod_terminates_itself_at_its_deadline_with_its_own_key(tmp_path, fake):
    """This machine starts the work and then says nothing more (asleep, gone): the pod ends itself at its deadline."""
    root = _project(tmp_path, fake)
    pod = _pod(fake, root, minutes=0.2)
    pod.wait()
    assert pod.selfstop == ["RunPod's REST API", "RunPod's GraphQL API"]
    pod.upload(root, [])
    pod.inside("plan", 5, ["sleep", "300"])                 # its watchdog would wait 5 min: the deadline comes first
    state = fake.wait_terminated(pod.id, 60)
    assert state.get("terminated_by") == f"pod:{pod.id}", "the pod asked RunPod to terminate it, with its own key"
    assert state["terminated_how"] == "rest" and fake.gone(pod.id), "and the work is gone with it"
    assert "reached its time limit" in (state["dir"] / "work" / "pod.log").read_text()


@pods
def test_a_pod_nothing_is_started_in_ends_itself(tmp_path, fake, monkeypatch):
    """A pod whose run never got going (this machine went away after making it) does not wait for its deadline."""
    monkeypatch.setattr(R.backend, "startup_minutes", 0.05)
    root = _project(tmp_path, fake)
    pod = _pod(fake, root, minutes=10)
    state = fake.wait_terminated(pod.id, 30)
    assert state.get("terminated_by") == f"pod:{pod.id}" and fake.gone(pod.id)
    assert "nothing was started in it" in (state["dir"] / "work" / "pod.log").read_text()


@pods
def test_a_pod_runpod_will_not_terminate_still_stops_everything_and_exits(tmp_path, fake):
    """If RunPod refuses the pod's own key, every way is tried, then the work is ended and the pod's first process
    exits (the container stops), so at least nothing runs on."""
    fake.scenario["pod_key_refused"] = True
    root = _project(tmp_path, fake)
    pod = _pod(fake, root, minutes=0.15)
    pod.wait()
    pod.upload(root, [])
    pod.inside("plan", 5, ["sleep", "300"])
    proc = fake.pods[pod.id]["proc"]
    proc.wait(timeout=60)
    assert proc.returncode == 3 and not fake.pods[pod.id].get("terminated")
    assert fake.gone(pod.id), "nothing testbench started runs on"
    log = (fake.pods[pod.id]["dir"] / "work" / "pod.log").read_text()
    assert "RunPod's REST API could not terminate this pod: RuntimeError: HTTP 403" in log
    assert "RunPod's GraphQL API could not terminate this pod" in log and "RunPod did not terminate this pod" in log
    assert R.Api(fake.url, fake.key).get(pod.id)["status"] == "EXITED"
    R.Api(fake.url, fake.key).terminate(pod.id)


@pods
def test_the_watchdog_in_the_pod_stops_the_work_before_the_deadline(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(R.backend, "startup_minutes", 0.15)
    monkeypatch.setattr(R, "STOP_MARGIN", 58)               # the plan's 1.15 min, less 58 s: it stops ~11 s in
    root = _project(tmp_path, fake)
    began = time.time()
    run, rep = _run(root, "stuck")
    assert rep["status"] == "cancelled" and rep["stopped_because"].startswith("the pod's watchdog: reached max_minutes")
    assert time.time() - began < 45, "it did not wait for the step's own limit"
    pod_id = runs.read_state(run)["runpod_pod"]
    assert fake.wait_terminated(pod_id, 10)["terminated"] and fake.gone(pod_id)


@pods
def test_the_agent_answers_only_with_its_token(tmp_path, fake):
    root = _project(tmp_path, fake)
    pod = _pod(fake, root, minutes=5)
    pod.wait()
    url = pod.agent.url + "/status"
    for auth in (None, "Bearer wrong", f"Bearer {fake.key}", f"Bearer {fake.pods[pod.id]['key']}"):
        req = urllib.request.Request(url, headers={"Authorization": auth} if auth else {})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=30)
        assert e.value.code == 401, auth
    assert pod.agent.status()["agent"] == 1
    assert "TESTBENCH_POD_TOKEN" not in pod.agent.run(["env"]).stdout.decode(), "the code in the pod never sees it"
    with pytest.raises(R.RunPodError, match="refused testbench's token"):
        R.Agent(pod.agent.url, "not-the-token").status()
    R.Api(fake.url, fake.key).terminate(pod.id)
    assert fake.gone(pod.id)


@pods
def test_failures_say_what_to_do_and_a_failed_create_costs_nothing(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)

    def error(**scenario) -> dict:
        fake.scenario = scenario
        fake.calls.clear()
        run, rep = _run(root, "cpu")
        assert rep["status"] == "error", rep
        for p in fake.pods.values():
            assert p.get("terminated") or p.get("proc") is None, "a pod that was made is terminated"
        return rep

    rep = error(create=(500, "There are no longer any instances available with the requested specifications."))
    assert "RunPod could not create the pod (HTTP 500): There are no longer any instances" in rep["stopped_because"]
    assert rep["cost_usd"] == 0 and not fake.pods, "nothing was made, so nothing was spent"
    rep = error(lost=True)
    assert "did not answer (asked to create the pod)" in rep["stopped_because"]
    lost = next(p for p in fake.pods.values() if p["id"].startswith("lost"))
    assert lost["terminated_by"] == "user", "a pod whose making was not confirmed is found by its name and terminated"
    rep = error(create=(401, "Unauthorized"))
    assert "RunPod refused to create the pod (HTTP 401" in rep["stopped_because"] and "RUNPOD_API_KEY" in \
        rep["stopped_because"]
    rep = error(start="ERROR")
    assert "is ERROR before anything ran in it" in rep["stopped_because"]
    with monkeypatch.context() as m:
        m.setattr(R.backend, "startup_minutes", 0.02)
        rep = error(start="STARTING")
    assert "did not start within" in rep["stopped_because"] and "runpod.data_centers" in rep["stopped_because"]
    rep = error(no_pod_key=True)
    assert "the pod cannot terminate itself" in rep["stopped_because"] and \
        "runpod.allow_without_remote_stop: true" in rep["stopped_because"]
    assert all(p["terminated_by"] == "user" for p in fake.pods.values() if p.get("proc")), "terminated from here"

    write_config(root, {**C.yaml.safe_load((root / "testbench.yaml").read_text()),
                        "runpod": _section(fake, allow_without_remote_stop=True)})
    fake.scenario = {"no_pod_key": True}
    run, rep = _run(root, "cpu")
    assert rep["status"] == "passed" and "only from this machine" in rep["remote_stop"], "allowed by the person"

    fake.scenario, n = {}, len(fake.calls)
    monkeypatch.delenv("RUNPOD_API_KEY")
    run, rep = _run(root, "cpu")
    assert "RUNPOD_API_KEY is not set here" in rep["stopped_because"] and len(fake.calls) == n
    monkeypatch.setenv("RUNPOD_API_KEY", fake.key)
    cfg = C.yaml.safe_load((root / "testbench.yaml").read_text())
    cfg["plans"]["cpu"]["steps"][0]["secrets"] = ["NOT_SET_ANYWHERE"]
    write_config(root, cfg)
    run, rep = _run(root, "cpu")
    assert "secrets not in this environment: NOT_SET_ANYWHERE" in rep["stopped_because"] and len(fake.calls) == n


def test_the_runpod_section_is_checked(tmp_path, fake):
    def load(gpu=None, **section):
        write_config(tmp_path, {"project": "p", "runpod": _section(fake, **section),
                                "plans": {"p": {"backend": "runpod", "gpu": gpu,
                                                "steps": [{"name": "a", "run": "true"}]}}})
        return C.load(tmp_path)

    cfg = load("RTX4090:2")
    assert budget.worst_case(cfg, "p")["usd_per_minute"] == R.USD_PER_HOUR["RTX4090"] * 2 / 60
    assert budget.worst_case(load(), "p")["usd_per_minute"] == R.CPU_USD_PER_VCPU_HOUR * 2 / 60, "a CPU pod, 2 vCPUs"
    for url in ("https://example.com/v2", "http://api.runpod.io/v2", "https://api.runpod.io.example.com/v2",
                "http://10.0.0.1/v2", "https://user:pw@api.runpod.io/v2", "ftp://127.0.0.1/v2"):
        with pytest.raises(C.ConfigError, match="only goes to RunPod itself"):
            load(api_url=url)
    assert load(api_url="https://api.runpod.io/v2/")["runpod"]["api_url"] == "https://api.runpod.io/v2"
    with pytest.raises(C.ConfigError, match=r"with \{pod_id\} in it"):
        load(proxy_url="https://proxy.runpod.net")
    with pytest.raises(C.ConfigError, match=r"runpod offers .*H100-SXM.* \(did you mean H100-SXM\?\)"):
        load("H100-SMX")
    with pytest.raises(C.ConfigError, match="has no price: add RTXPRO6000"):
        load("RTXPRO6000", gpu_types={"RTXPRO6000": "NVIDIA RTX PRO 6000 Blackwell Server Edition"})
    write_config(tmp_path, {"project": "p", "runpod": _section(fake, gpu_types={"rtxpro6000": "NVIDIA RTX PRO 6000"}),
                            "pricing": {"runpod_usd_per_hour": {"RTXPRO6000": 2.5}},
                            "plans": {"p": {"backend": "runpod", "gpu": "RTXPRO6000",
                                            "steps": [{"name": "a", "run": "x"}]}}})
    cfg = C.load(tmp_path)
    assert budget.worst_case(cfg, "p")["usd_per_minute"] == 2.5 / 60
    assert R.pod_body(cfg, "n", cfg["plans"]["p"], 5, 0, "t", {})["gpu"] == {"id": "NVIDIA RTX PRO 6000", "count": 1}
    for bad, match in (({"cloud": "ALL"}, "SECURE"), ({"vcpus": 3}, "power of two"), ({"setup": "conda"}, "setup"),
                       ({"env": {"RUNPOD_API_KEY": "x"}}, "not RUNPOD_"), ({"disk_gb": 0}, "disk_gb"),
                       ({"setup": "none", "pip": ["torch"]}, "needs runpod.setup: pip"),
                       ({"allow_without_remote_stop": "yes"}, "true or false"),
                       ({"data_centers": "US-KS-2"}, "data_centers")):
        with pytest.raises(C.ConfigError, match=match):
            load(**bad)


def test_it_keeps_the_backend_contract_and_says_its_prices(tmp_path, fake):
    r = cli(tmp_path, "plugin", "test", "agent_testbench.backends.runpod_backend:backend", "--kind", "backends")
    assert r.returncode == 0 and "keeps its kind's contract" in r.stdout, r.stdout + r.stderr
    r = cli(tmp_path, "prices")
    assert "runpod\n" in r.stdout and "H100-SXM     $3.80/h per GPU  (built in)" in r.stdout, r.stdout
    assert "2026-11-12" in r.stdout
    r = cli(tmp_path, "doctor", env={"RUNPOD_API_KEY": fake.key})
    assert "RUNPOD_API_KEY (runpod plans and sessions)" in r.stdout and fake.key not in r.stdout


@pods
def test_a_live_session_in_a_pod(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)
    env = {"TB_TOKEN": SECRET, "RUNPOD_API_KEY": fake.key}
    r = cli(root, "session", "start", "pod", "--backend", "runpod", "--max-minutes", "5", "--secret", "TB_TOKEN",
            env=env)
    assert r.returncode == 0 and "ready on runpod" in r.stdout and "Hard stop by the pod itself" in r.stdout, \
        r.stdout + r.stderr
    pod_id = json.loads((root / ".testbench" / "sessions" / "pod" / "state.json").read_text())["handles"]["pod"]
    assert fake.created()[0]["cpu"] == {"id": "cpu3c", "vcpuCount": 2}
    cli(root, "exec", "x = 21", env=env)
    assert "=> 42" in cli(root, "exec", "x * 2", env=env).stdout, "variables persist"
    assert f"=> '{SECRET}'" in cli(root, "exec", "import os; os.environ['TB_TOKEN']", env=env).stdout
    out = cli(root, "exec", "--cells", "nb.ipynb", "--params", '{"N": 5}', env=env).stdout
    assert "token" in out
    assert "out/" in cli(root, "files", "ls", env=env).stdout
    got = root / "got.json"
    cli(root, "files", "get", "out/result.json", str(got), env=env)
    assert json.loads(got.read_text()) == {"n": 5}
    (root / "local.txt").write_text("from here")
    cli(root, "files", "put", str(root / "local.txt"), "sent/local.txt", env=env)
    assert "from here" in cli(root, "session", "shell", "cat sent/local.txt", env=env).stdout
    (root / "new_module.py").write_text("VALUE = 'synced'\n")
    assert "copied" in cli(root, "session", "sync", env=env).stdout
    assert "=> 'synced'" in cli(root, "exec", "import new_module; new_module.VALUE", env=env).stdout
    state = (root / ".testbench" / "sessions" / "pod" / "state.json").read_text()
    assert fake.key not in state and SECRET not in state, "state.json holds a nonce, never the key or the token"
    assert "stopped after" in cli(root, "session", "stop", env=env).stdout
    assert fake.wait_terminated(pod_id, 10)["terminated"] and fake.gone(pod_id)
