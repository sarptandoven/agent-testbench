"""The kubernetes backend: plans and live sessions as Jobs on a cluster.

Unit tests run against a stand-in for kubectl that records every command and what went in on its stdin, and answers
from a scenario (the pod's status, what the pod sends back): the Job's manifest, where secrets go, cancelling and the
failure paths. With TESTBENCH_TEST_KIND=1 the tests at the end run real plans and sessions on a kind cluster
(TESTBENCH_TEST_KIND_CLUSTER, default `testbench`; `kind` and `kubectl` on PATH; see docs/kubernetes.md)."""
import base64
import io
import json
import math
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

import pytest

from agent_testbench import budget, config as C, executor, runs, sessions as S
from agent_testbench.backends import kubernetes_backend as K
from conftest import PNG_1PX, cli, make_notebook, write_config

SECRET = f"s3cret-{uuid.uuid4().hex[:12]}"           # unique, so finding it anywhere means it leaked from this test

FAKE = r'''
"""Stands in for kubectl: records argv and stdin, answers from scenario.json next to it."""
import base64, json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
sc = json.load(open(os.path.join(here, "scenario.json")))
state_path = os.path.join(here, "state.json")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"get": 0, "deleted": False}
args = sys.argv[1:]
flags, rest, i = {}, [], 0
while i < len(args):
    if args[i] in ("--context", "--namespace") and not rest:
        flags[args[i]] = args[i + 1]
        i += 2
    else:
        rest.append(args[i])
        i += 1
data = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": args, "flags": flags, "rest": rest, "stdin": base64.b64encode(data).decode()}) + "\n")
verb = rest[0] if rest else ""

def done(code=0, out=b"", err=""):
    json.dump(state, open(state_path, "w"))
    sys.stdout.buffer.write(out if isinstance(out, bytes) else out.encode())
    sys.stderr.write(err)
    sys.exit(code)

def pick(key):
    seq = sc.get(key) or [None]
    return seq[min(state["get"], len(seq) - 1)]

if verb == "config" and rest[1] == "current-context":
    ctx = sc.get("context")
    done(0, ctx + "\n") if ctx else done(1, b"", "error: current-context is not set\n")
if verb == "config":
    done(0, sc.get("namespace", ""))
if verb == "create":
    c = sc.get("create") or {}
    done(c.get("rc", 0), "job.batch/x created\n", c.get("stderr", ""))
if verb == "get":
    job = rest[rest.index("-l") + 1].split("=", 1)[1]
    items = []
    if not state["deleted"]:
        j, p = pick("jobs"), pick("pods")
        items.append({"kind": "Job", "metadata": {"name": job}, "spec": {"activeDeadlineSeconds": 420}, "status": j or {}})
        if p is not None:
            items.append({"kind": "Pod", "metadata": {"name": job + "-abcde", "creationTimestamp": "2026-11-01T00:00:00Z"},
                          "status": p})
    state["get"] += 1
    done(0, json.dumps({"kind": "List", "items": items}))
if verb == "delete":
    state["deleted"] = True
    done(0, 'job.batch "x" deleted\n')
if verb == "logs":
    done(0, sc.get("logs", "[testbench] waiting for the project to be copied in\n"))
if verb == "version":
    done(0, json.dumps({"serverVersion": {"gitVersion": "v1.34.0"}}))
if verb == "auth":
    done(0, sc.get("can_i", "yes") + "\n")
if verb == "exec":
    script = rest[rest.index("--") + 3] if "-c" in rest else ""
    if "w|gz" in script:
        probe = sc.get("probe_done") if state.get("interrupted") else sc.get("probe")
        if probe and not state["deleted"]:
            done(0, base64.b64decode(probe))
        done(1, b"", "error: container not running\n")
    if "os.kill(1" in script:
        state["interrupted"] = True
        done(0)
    if "/work/kernel/ready" in script:
        done(0 if sc.get("ready", True) else 1)
    done(0)
done(1, b"", "fake kubectl: unknown command " + " ".join(rest) + "\n")
'''

RUNNING = {"phase": "Running", "containerStatuses": [{"name": "main", "state": {"running": {}}}]}


class Fake:
    """The stand-in's folder: its scenario, and the commands it was given."""

    def __init__(self, d: Path):
        self.d = d
        d.mkdir(parents=True, exist_ok=True)
        (d / "kubectl.py").write_text(FAKE)
        self.command = [sys.executable, str(d / "kubectl.py")]

    def scenario(self, **sc) -> None:
        (self.d / "scenario.json").write_text(json.dumps({"context": "fake-ctx", "namespace": "", "jobs": [{}],
                                                          "pods": [RUNNING], **sc}))
        for name in ("state.json", "calls.jsonl"):
            (self.d / name).unlink(missing_ok=True)

    def calls(self) -> list[dict]:
        p = self.d / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def stdin(self, call: dict) -> bytes:
        return base64.b64decode(call["stdin"])

    def created(self) -> dict:
        return json.loads(self.stdin(next(c for c in self.calls() if c["rest"][:1] == ["create"])))

    def execs(self, marker: str) -> list[dict]:
        return [c for c in self.calls() if c["rest"][:1] == ["exec"] and any(marker in a for a in c["rest"])]


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Fake:
    monkeypatch.setenv("TESTBENCH_K8S_POLL_SECONDS", "0.05")
    f = Fake(tmp_path / "fake-kubectl")
    f.scenario()
    return f


def _probe(tmp_path: Path, plan: dict, done: bool = True) -> str:
    """What the pod sends back: a real executor's run folder, as the pod's probe packs it."""
    root, run = tmp_path / "pod-project", tmp_path / "pod-run"
    root.mkdir(exist_ok=True)
    executor.execute_plan(plan, project_dir=root, run_dir=run)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for p in sorted(run.rglob("*")):
            if p.is_file():
                t.add(p, arcname="run/" + p.relative_to(run).as_posix())
        if done:
            info = tarfile.TarInfo("done")
            info.size = 2
            t.addfile(info, io.BytesIO(b"0\n"))
    return base64.b64encode(buf.getvalue()).decode()


def _project(tmp_path: Path, fake: Fake, **section) -> Path:
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    (root / "train.py").write_text("print('training')\n")
    k = {"command": fake.command, "usd_per_hour": {"cpu": 0.6, "A100": 2.5}, "cpu": 4, "memory_gb": 16,
         "node_selector": {"pool": "research"}, "gpu_node_selector": {"a100": {"nvidia.com/gpu.product": "A100-SXM4"}},
         "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
         "env_secrets": {"HF_TOKEN": "hf-token/token"}, "env": {"HF_HOME": "/work/cache/hf"},
         "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "imagenet", "readOnly": True}}],
         "volume_mounts": [{"name": "data", "mountPath": "/data", "readOnly": True}], **section}
    write_config(root, {"project": "proj", "kubernetes": k,
                        "budget": {"max_run_usd": 5, "ask_above_usd": 5, "max_day_usd": 10},
                        "plans": {"train": {"backend": "kubernetes", "gpu": "A100:2", "steps": [
                                      {"name": "train", "run": "python train.py", "max_minutes": 10,
                                       "secrets": ["HF_TOKEN", "TB_TOKEN"]}]},
                                  "cpu": {"backend": "kubernetes", "steps": [{"name": "a", "run": "python -c pass",
                                                                              "max_minutes": 1}]}}})
    return root


def _run(root: Path, plan: str) -> tuple[Path, dict]:
    run = runs.launch(root, C.load(root), plan, foreground=True)
    return run, json.loads((run / "report.json").read_text())


def test_a_plan_is_a_job_with_kubernetes_own_hard_stop(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)
    monkeypatch.setenv("TB_TOKEN", SECRET)
    cfg = C.load(root)
    fake.scenario(probe=_probe(tmp_path, {"steps": [dict(cfg["plans"]["cpu"]["steps"][0])]}))
    est = budget.worst_case(cfg, "train")
    assert est["worst_case_usd"] == round(2 * 2.5 / 60 * 15, 4), "two A100s for 10 min + 5 start-up, at $2.50/h each"
    run, rep = _run(root, "train")
    assert rep["status"] == "passed" and (run / "steps" / "01-a" / "log.txt").exists(), "the pod's evidence came back"

    job = fake.created()
    spec, pod = job["spec"], job["spec"]["template"]["spec"]
    assert spec["activeDeadlineSeconds"] == math.ceil(K.plan_limit(cfg["plans"]["train"]) * 60) == 15 * 60, \
        "the deadline is the priced worst case"
    assert spec["backoffLimit"] == 0 and spec["ttlSecondsAfterFinished"] == K.TTL_SECONDS
    assert pod["restartPolicy"] == "Never" and pod["automountServiceAccountToken"] is False
    box = pod["containers"][0]
    assert box["resources"]["limits"] == {"cpu": "4", "memory": "16384Mi", "nvidia.com/gpu": "2"}
    assert box["resources"]["requests"] == box["resources"]["limits"]
    assert pod["nodeSelector"] == {"pool": "research", "nvidia.com/gpu.product": "A100-SXM4"}, "where an A100 is"
    assert pod["tolerations"] == [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
    env = {e["name"]: e for e in box["env"]}
    assert env["HF_TOKEN"]["valueFrom"] == {"secretKeyRef": {"name": "hf-token", "key": "token"}}, "by reference"
    assert env["HF_HOME"]["value"] == "/work/cache/hf" and "TB_TOKEN" not in env
    assert box["volumeMounts"] == [{"name": "work", "mountPath": "/work"},
                                   {"name": "data", "mountPath": "/data", "readOnly": True}], "no shared storage assumed"
    assert pod["volumes"][0] == {"name": "work", "emptyDir": {}} and pod["volumes"][1]["name"] == "data"
    assert job["metadata"]["labels"]["agent-testbench/project"] == "proj"
    assert len(job["metadata"]["name"]) <= 63

    calls = fake.calls()
    start = fake.execs("/work/start.json")
    assert len(start) == 1 and json.loads(fake.stdin(start[0]))["env"] == {"TB_TOKEN": SECRET}, \
        "a secret from this machine goes over kubectl exec's stdin"
    leaks = [c["rest"][:3] for c in calls if SECRET in json.dumps(c["argv"]) or
             (c != start[0] and SECRET.encode() in fake.stdin(c))]
    assert not leaks, "and nowhere else: not on a command line, not in the manifest"
    assert not [p for p in run.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()], "nor in the run folder"

    upload = fake.execs("r|gz")[0]
    with tarfile.open(fileobj=io.BytesIO(fake.stdin(upload))) as t:
        names = t.getnames()
    assert "work/project/train.py" in names and "work/plan.json" in names, "the project goes in on stdin"
    assert any(n.startswith("work/pkg/agent_testbench/") for n in names), "and this package with it"
    assert all(c["flags"] == {"--context": "fake-ctx", "--namespace": "default"} for c in calls
               if c["rest"][0] not in ("config",)), "every command pinned to the context it started with"
    assert fake.execs("/work/collected"), "the pod is told its evidence was taken"
    assert calls[-1]["rest"][:2] == ["delete", "job"], "and the Job is deleted"
    state = runs.read_state(run)
    assert state["kube_job"] == job["metadata"]["name"] and "Kubernetes itself" in state["remote_stop"]
    assert SECRET not in (run / "kubectl.log").read_text()


def test_cancel_deletes_the_job(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)
    cfg = C.load(root)
    plan = {"steps": [dict(cfg["plans"]["cpu"]["steps"][0])]}
    fake.scenario(probe=_probe(tmp_path, plan, done=False), probe_done=_probe(tmp_path, plan))
    run = runs.launch(root, cfg, "cpu", foreground=False)       # a driver in the background
    for _ in range(400):
        if fake.execs("w|gz"):
            break
        time.sleep(0.05)
    assert "cancelled" in runs.cancel(run)
    assert runs.read_state(run)["status"] == "cancelled"
    assert fake.execs("os.kill(1, signal.SIGINT)"), "the executor in the pod is interrupted, so it writes its report"
    assert any(c["rest"][:3] == ["delete", "job", runs.read_state(run)["kube_job"]] for c in fake.calls())

    fake.scenario()                                             # the driver is gone: cancel works from state.json
    msg = K.backend.cancel_remote({"kube_job": "tb-x", "kube_context": "lab", "kube_namespace": "ml",
                                   "kube_command": fake.command})
    assert msg == "deleted the Job tb-x and its pod (context lab, namespace ml)"
    assert fake.calls()[-1]["argv"] == ["--context", "lab", "--namespace", "ml", "delete", "job", "tb-x",
                                        "--ignore-not-found", "--wait=false"]
    assert K.backend.cancel_remote({}) == ""


def test_failures_say_what_to_do(tmp_path, fake, monkeypatch):
    root = _project(tmp_path, fake)

    def error(**scenario) -> dict:
        fake.scenario(**scenario)
        run, rep = _run(root, "cpu")
        assert rep["status"] == "error", rep
        assert any(c["rest"][:2] == ["delete", "job"] for c in fake.calls()) or not any(
            c["rest"][:1] == ["create"] for c in fake.calls()), "a Job that was made is deleted"
        return rep

    pull = {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {
        "reason": "ImagePullBackOff", "message": 'Back-off pulling image "nope:1"'}}}]}
    assert "could not pull the image python:3.11-slim (ImagePullBackOff)" in error(pods=[pull])["stopped_because"]
    config = {"phase": "Pending", "containerStatuses": [{"state": {"waiting": {
        "reason": "CreateContainerConfigError", "message": 'secret "hf-token" not found'}}}]}
    assert "kubectl create secret generic" in error(pods=[config])["stopped_because"]
    assert "Forbidden" in error(create={"rc": 1, "stderr": "Error from server (Forbidden): jobs.batch is forbidden"}
                                )["stopped_because"]
    assert "auth can-i create jobs" in error(create={"rc": 1, "stderr": "Error from server (Forbidden): no"})[
        "stopped_because"]
    assert "kubectl has no current context: set kubernetes.context" in error(context="")["stopped_because"]
    deadline = {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded"}]}
    rep = error(jobs=[{}, {}, deadline], pods=[RUNNING, RUNNING, None])           # it ran, then the cluster ended it
    assert "Kubernetes stopped the Job at its deadline (activeDeadlineSeconds 420" in rep["stopped_because"]
    oom = {"phase": "Failed", "containerStatuses": [{"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}
    assert "ran out of memory (OOMKilled)" in error(pods=[RUNNING, RUNNING, oom])["stopped_because"]

    with monkeypatch.context() as m:
        m.setattr(K.backend, "startup_minutes", 0.002)
        wait = {"phase": "Pending", "conditions": [{"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                                    "message": "0/3 nodes are available: 3 Insufficient nvidia.com/gpu."}]}
        rep = error(pods=[wait])
    assert "did not start within" in rep["stopped_because"] and "3 Insufficient nvidia.com/gpu" in rep["stopped_because"]

    monkeypatch.delenv("TB_TOKEN", raising=False)
    fake.scenario()
    run, rep = _run(root, "train")
    assert "secrets not in this environment: TB_TOKEN" in rep["stopped_because"] and "kubernetes.env_secrets" in \
        rep["stopped_because"]
    assert not any(c["rest"][:1] == ["create"] for c in fake.calls()), "nothing is made before the secrets are there"

    write_config(root, {"project": "proj", "kubernetes": {"command": ["no-such-kubectl-here"]},
                        "plans": {"cpu": {"backend": "kubernetes", "steps": [{"name": "a", "run": "true"}]}}})
    run, rep = _run(root, "cpu")
    assert "`no-such-kubectl-here` is not installed here: install kubectl" in rep["stopped_because"]


def test_the_kubernetes_section_is_checked(tmp_path, fake):
    def load(k: dict, gpu=None, sessions=None):
        write_config(tmp_path, {"project": "p", "kubernetes": k, "sessions": sessions or {"backend": "local"},
                                "plans": {"p": {"backend": "kubernetes", "gpu": gpu,
                                                "steps": [{"name": "a", "run": "true"}]}}})
        return C.load(tmp_path)

    cfg = load({"usd_per_hour": 1.5, "command": "kubectl --kubeconfig /k"})
    assert K.price_table(cfg["kubernetes"]) == {"cpu": 1.5}
    assert cfg["kubernetes"]["command"] == ["kubectl", "--kubeconfig", "/k"]
    assert cfg["plans"]["p"]["workdir"] == "copy", "plans run in a copy"
    assert load({"usd_per_hour": {"cpu": 0, "h100": 4}}, gpu="H100:8")["kubernetes"]["usd_per_hour"]["h100"] == 4
    for k, gpu, message in (
            ({}, "A100", "`plans.p.gpu` is 'A100', but kubernetes.usd_per_hour has no price for A100 "
                         "\\(it prices no GPU\\); add A100: <dollars per hour per GPU>"),
            ({"usd_per_hour": {"cpu": 0, "L4": 0.8}}, "H100", "it prices L4"),
            ({"usd_per_hour": {"cpu": -1}}, None, "`kubernetes.usd_per_hour.cpu` is US dollars per hour"),
            ({"usd_per_hour": "cheap"}, None, "`kubernetes.usd_per_hour` is what a pod costs per hour"),
            ({"env_secrets": {"HF_TOKEN": "hf_abcdef123"}}, None, "must be SECRET/KEY"),
            ({"env_secrets": {"bad-name": "a/b"}}, None, "keys are environment variable names"),
            ({"setup": "conda"}, None, "`kubernetes.setup` is pip"),
            ({"setup": "none", "pip": ["torch"]}, None, "`kubernetes.pip` needs kubernetes.setup: pip"),
            ({"namespace": "Research_Lab"}, None, "`kubernetes.namespace` is a namespace name"),
            ({"image_pull_policy": "always"}, None, "IfNotPresent, Always or Never"),
            ({"tolerations": [{"key": "x", "effct": "NoSchedule"}]}, None, "a list of Kubernetes tolerations"),
            ({"tolerations": {"key": "x"}}, None, "a list of Kubernetes tolerations"),
            ({"node_selector": ["pool=gpu"]}, None, "`kubernetes.node_selector` is a mapping"),
            ({"gpu_node_selector": {"A100": {"a": "b"}}}, None, "`kubernetes.gpu_node_selector.A100` is for a GPU"),
            ({"memory_gb": "16Gi"}, None, "`kubernetes.memory_gb` is a positive number"),
            ({"volumes": [{"name": "work", "emptyDir": {}}]}, None, "`kubernetes.volumes` is a list"),
            ({"volumes": [{"name": "d", "emptyDir": {}}], "volume_mounts": [{"name": "d", "mountPath": "/work/d"}]},
             None, "outside /work"),
            ({"volume_mounts": [{"name": "nope", "mountPath": "/data"}]}, None, "mounts of kubernetes.volumes"),
            ({"image": ""}, None, "`kubernetes.image` is the image the pod runs")):
        with pytest.raises(C.ConfigError, match=message):
            load(k, gpu)
    try:
        load({"env_secrets": {"HF_TOKEN": "hf_abcdef123"}})
    except C.ConfigError as exc:
        assert "hf_abcdef123" not in str(exc), "a value that might be the secret itself is never repeated"
    with pytest.raises(C.ConfigError, match="`sessions.gpu` is 'T4'"):
        load({}, None, {"backend": "kubernetes", "gpu": "T4"})
    with pytest.raises(C.ConfigError, match="no price for T4"):          # --gpu at start: refused by the budget
        budget.estimate(load({}), {"backend": "kubernetes", "gpu": "T4"}, 10, "session x")

    load({"usd_per_hour": {"cpu": 0.2, "A100": 2.5}, "context": "lab"})
    r = cli(tmp_path, "prices")
    assert "$0.20/h for a pod without a GPU on lab (testbench.yaml kubernetes.usd_per_hour)" in r.stdout, r.stdout
    assert "$2.50/h per A100 on lab" in r.stdout


def test_doctor_checks_kubectl_the_cluster_and_permissions(tmp_path, fake):
    write_config(tmp_path, {"project": "p", "kubernetes": {"command": fake.command, "namespace": "ml"},
                            "plans": {"a": {"steps": [{"name": "a", "run": "true"}]}}})
    out = cli(tmp_path, "doctor").stdout
    assert "ok   kubernetes cluster: context fake-ctx, namespace ml, server v1.34.0" in out, out
    assert "ok   kubectl auth can-i create jobs -n ml: yes" in out and "can-i create pods/exec -n ml: yes" in out
    fake.scenario(can_i="no")
    assert "no   kubectl auth can-i create jobs -n ml: no" in cli(tmp_path, "doctor").stdout
    fake.scenario(context="")
    assert "no   kubernetes context: kubectl has no current context" in cli(tmp_path, "doctor").stdout
    write_config(tmp_path, {"project": "p", "kubernetes": {"command": ["no-such-kubectl-here"]},
                            "plans": {"a": {"steps": [{"name": "a", "run": "true"}]}}})
    assert "no   kubectl (kubernetes plans and sessions): install kubectl" in cli(tmp_path, "doctor").stdout


def test_names_fit_kubernetes():
    a = K.k8s_name("tb", "My_Project", "20261101-120000-a-very-long-plan-name-that-goes-on-and-on-and-on-2")
    b = K.k8s_name("tb", "My_Project", "20261101-120000-a-very-long-plan-name-that-goes-on-and-on-and-on-3")
    assert len(a) <= 63 and a != b and a == a.lower() and a[0].isalnum() and a[-1].isalnum()
    assert K._label("proj-") == "proj" and K._label("a b/c") == "a-b-c"


# ---- on a real cluster: kind ----------------------------------------------------------------------------------

CLUSTER = os.environ.get("TESTBENCH_TEST_KIND_CLUSTER", "testbench")
_kind = pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_KIND") != "1",
                           reason="a real cluster: set TESTBENCH_TEST_KIND=1 with a kind cluster running")
CONTEXT = f"kind-{CLUSTER}"


def needs_kind(test):
    """A test on the kind cluster: they share it (and the image loaded into it), so one at a time (serial)."""
    return pytest.mark.serial(_kind(test))


def kubectl(*args, check=True, data=None) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", "--context", CONTEXT, *args], capture_output=True, text=True, check=check,
                          input=data, timeout=120)


@pytest.fixture(scope="session")
def kind_image(tmp_path_factory) -> str:
    """An image with testbench's basics on the cluster's node: TESTBENCH_TEST_KIND_IMAGE, or the docker backend's
    derived image built here and loaded with `kind load` (pods on kind may have no package index to install from)."""
    if os.environ.get("TESTBENCH_TEST_KIND_IMAGE"):
        return os.environ["TESTBENCH_TEST_KIND_IMAGE"]
    from agent_testbench.backends import docker_backend as D
    from test_docker import IMAGE, _build_settings
    tmp = tmp_path_factory.mktemp("kind-image")
    cfg = {"project": "warm", "root": str(tmp), "plans": {}, "sessions": {"backend": "local"},
           "docker": {**D.backend.config_defaults["docker"], "image": IMAGE, **_build_settings()}}
    D.backend.validate(cfg)
    tag = D.ensure_image(cfg, D.Docker(["docker"], tmp / "build.log"))
    subprocess.run(["kind", "load", "docker-image", tag, "--name", CLUSTER], check=True, capture_output=True, timeout=600)
    return tag


@pytest.fixture
def cluster(kind_image, tmp_path) -> dict:
    """The kubernetes section for the kind cluster, and a Secret in it for the duration of a test."""
    ns = f"tb-{uuid.uuid4().hex[:8]}"
    kubectl("create", "namespace", ns)
    kubectl("create", "secret", "generic", "tb-secret", f"--from-literal=token={SECRET}", "-n", ns)
    yield {"context": CONTEXT, "namespace": ns, "image": kind_image, "setup": "none", "image_pull_policy": "IfNotPresent",
           "usd_per_hour": {"cpu": 0.6}, "env_secrets": {"KUBE_TOKEN": "tb-secret/token"}}
    kubectl("delete", "namespace", ns, "--wait=false", check=False)


def _jobs(ns: str) -> list[str]:
    return kubectl("get", "jobs", "-n", ns, "-o", "name").stdout.split()


def _gone(ns: str, seconds: float = 60) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not _jobs(ns) and not kubectl("get", "pods", "-n", ns, "-o", "name").stdout.split():
            return True
        time.sleep(1)
    return False


def _latest_run(root: Path) -> Path:
    return sorted((root / ".testbench" / "runs").iterdir())[-1]


def _wait_for_step(run: Path, seconds: float = 180) -> dict:
    end = time.time() + seconds
    while time.time() < end:
        if any(e.get("event") == "step_start" for e in runs.progress(run)):
            return runs.read_state(run)
        time.sleep(0.25)
    raise AssertionError(f"no step started; driver.log:\n{(run / 'driver.log').read_text()[-2000:]}")


HASH = "python -c \"import hashlib, os; print('{0}', hashlib.sha256(os.environ['{0}'].encode()).hexdigest()[:12])\""


@pytest.fixture
def kproject(tmp_path, cluster) -> Path:
    root = tmp_path / "kproj"
    make_notebook(root / "nb.ipynb", [
        'N = 3 #@param {type:"integer"}',
        "import json, pathlib\npathlib.Path('out').mkdir(exist_ok=True)",
        "json.dump({'n': N}, open('out/result.json', 'w'))",
    ])
    write_config(root, {"project": "kproj", "kubernetes": cluster,
                        "budget": {"max_run_usd": 1, "ask_above_usd": 1, "max_day_usd": 5},
                        "plans": {"job": {"backend": "kubernetes", "steps": [
                            {"name": "where", "run": "pwd; " + HASH.format("TB_TOKEN") + "; " + HASH.format("KUBE_TOKEN"),
                             "max_minutes": 1, "secrets": ["TB_TOKEN", "KUBE_TOKEN"],
                             "expect": {"text": {"stdout": ["/work/project"]}}},
                            {"name": "notebook", "notebook": "nb.ipynb", "params": {"N": 7}, "max_minutes": 3,
                             "artifacts": ["out/*"], "expect": {"json": {"out/result.json": {"n": "== 7"}}}}]},
                            "slow": {"backend": "kubernetes", "steps": [{"name": "nap", "run": "echo napping; sleep 300",
                                                                          "max_minutes": 10}]}}})
    return root


def _digest(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@needs_kind
def test_kind_a_plan_runs_as_a_job_and_its_evidence_comes_back(kproject, cluster):
    r = cli(kproject, "run", "job", "--wait", "300", env={"TB_TOKEN": SECRET})
    assert r.returncode == 0 and "# PASS - plan `job` on kubernetes" in r.stdout, r.stdout + r.stderr
    run = _latest_run(kproject)
    assert (run / "steps" / "02-notebook" / "artifacts" / "out" / "result.json").exists(), "evidence came back"
    log = (run / "steps" / "01-where" / "log.txt").read_text()
    assert f"TB_TOKEN {_digest(SECRET)}" in log, "a secret from this machine reached the pod"
    assert f"KUBE_TOKEN {_digest(SECRET)}" in log, "and one from a Kubernetes Secret"
    assert not [p for p in run.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()], \
        "no secret value in the run folder: not in kubectl.log, pod.log, the report"
    assert not (kproject / "out").exists(), "the plan ran in the pod's copy"
    assert "watching: stop at" in (run / "watchdog" / "watchdog.log").read_text(), "the watchdog was armed"
    assert _gone(cluster["namespace"]), "the Job and its pod are deleted"


@needs_kind
def test_kind_kubernetes_ends_the_job_at_its_deadline(kproject, cluster, monkeypatch):
    """The watchdog is put past the deadline, so only Kubernetes can stop the work: nothing on this side does."""
    monkeypatch.setattr(K, "plan_limit", lambda plan: 0.75)          # activeDeadlineSeconds 45
    monkeypatch.setattr(K, "STOP_MARGIN", -600)
    began = time.time()
    run = runs.launch(kproject, C.load(kproject), "slow", foreground=True)
    rep = json.loads((run / "report.json").read_text())
    assert rep["status"] in ("cancelled", "error") and time.time() - began < 120, rep   # cancelled: it wrote its report
    assert "Kubernetes stopped the Job at its deadline (activeDeadlineSeconds 45" in rep["stopped_because"], rep
    assert "napping" in (run / "steps" / "01-nap" / "log.txt").read_text(), "what it did so far came back"
    assert _gone(cluster["namespace"])


@needs_kind
def test_kind_the_watchdog_stops_the_work_before_the_deadline(kproject, cluster, monkeypatch):
    monkeypatch.setenv("TESTBENCH_WATCHDOG_SECONDS", "0.2")          # in the Job's environment, so the pod's watchdog
    monkeypatch.setattr(K, "plan_limit", lambda plan: 1.5)           # deadline 90 s; the watchdog 75 s before it
    monkeypatch.setattr(K, "STOP_MARGIN", 75)
    run = runs.launch(kproject, C.load(kproject), "slow", foreground=True)
    rep = json.loads((run / "report.json").read_text())
    assert rep["status"] == "cancelled" and "the pod's watchdog: reached max_minutes" in rep["stopped_because"], rep
    assert "napping" in (run / "steps" / "01-nap" / "log.txt").read_text()
    assert _gone(cluster["namespace"])


@needs_kind
def test_kind_cancel_deletes_the_job(kproject, cluster):
    ns = cluster["namespace"]
    r = cli(kproject, "run", "slow", "--detach")
    assert r.returncode == 3, r.stdout + r.stderr
    run = _latest_run(kproject)
    _wait_for_step(run)
    assert _jobs(ns)
    assert "cancelled" in cli(kproject, "cancel").stdout
    rep = json.loads((run / "report.json").read_text())
    assert rep["status"] == "cancelled" and "napping" in (run / "steps" / "01-nap" / "log.txt").read_text()
    assert _gone(ns, 30), "cancel deletes the Job and its pod"

    r = cli(kproject, "run", "slow", "--detach")                 # with its driver gone, cancel works from state.json
    run = _latest_run(kproject)
    state = _wait_for_step(run)
    os.killpg(state["driver_pid"], signal.SIGKILL)
    time.sleep(1)
    assert _jobs(ns) == [f"job.batch/{state['kube_job']}"], "the Job outlives the driver"
    assert K.backend.cancel_remote(runs.read_state(run)).startswith(f"deleted the Job {state['kube_job']}")
    assert _gone(ns, 60)


@needs_kind
def test_kind_a_live_session_in_a_pod(kproject, cluster):
    ns = cluster["namespace"]
    r = cli(kproject, "session", "start", "box", "--backend", "kubernetes", "--max-minutes", "5",
            "--secret", "KUBE_TOKEN", "--secret", "TB_TOKEN", env={"TB_TOKEN": SECRET})
    assert r.returncode == 0 and "ready on kubernetes" in r.stdout and "Kubernetes itself" in r.stdout, r.stdout + r.stderr
    sdir = kproject / ".testbench" / "sessions" / "box"
    job = S.read(sdir)["handles"]["job"]
    spec = json.loads(kubectl("get", "job", job, "-n", ns, "-o", "json").stdout)
    assert spec["spec"]["activeDeadlineSeconds"] == (5 + K.backend.startup_minutes) * 60
    assert SECRET not in json.dumps(spec), "the Job has no secret value, only a reference"
    cli(kproject, "exec", "x = 21")
    assert "=> 42" in cli(kproject, "exec", "x * 2").stdout, "variables persist"
    out = cli(kproject, "exec", "import os; os.environ['KUBE_TOKEN'] == os.environ['TB_TOKEN'] != ''").stdout
    assert "=> True" in out, "both secrets are there"
    png = base64.b64encode(PNG_1PX).decode()
    out = cli(kproject, "exec", f"from IPython.display import Image, display; import base64\n"
                                f"display(Image(data=base64.b64decode('{png}')))").stdout
    images = [ln.split("image: ", 1)[1] for ln in out.splitlines() if ln.startswith("image: ")]
    assert images and Path(images[0]).read_bytes() == PNG_1PX, out
    out = cli(kproject, "exec", "--cells", "nb.ipynb", "--params", '{"N": 5}').stdout
    assert "out/" in cli(kproject, "files", "ls").stdout, out
    got = kproject / "got.json"
    cli(kproject, "files", "get", "out/result.json", str(got))
    assert json.loads(got.read_text()) == {"n": 5}
    (kproject / "local.txt").write_text("from here")
    cli(kproject, "files", "put", str(kproject / "local.txt"), "sent/local.txt")
    out = cli(kproject, "session", "shell", "cat sent/local.txt; pwd").stdout
    assert "from here" in out and "/work/project" in out
    (kproject / "new_module.py").write_text("VALUE = 'synced'\n")
    assert "copied" in cli(kproject, "session", "sync").stdout
    assert "=> 'synced'" in cli(kproject, "exec", "import new_module; new_module.VALUE").stdout
    assert "=> 21" in cli(kproject, "exec", "x").stdout, "a sync keeps the kernel's variables"
    assert not [p for p in sdir.rglob("*") if p.is_file() and SECRET.encode() in p.read_bytes()]
    assert "stopped after" in cli(kproject, "session", "stop").stdout
    assert _gone(ns, 60), "stop deletes the Job and its pod"


@needs_kind
def test_kind_setup_pip_installs_the_basics_in_a_plain_image(tmp_path, cluster):
    """setup: pip on an image with nothing but Python. TESTBENCH_TEST_KIND_PLAIN_IMAGE names it (default
    python:3.11-slim) and TESTBENCH_TEST_KIND_PIP_ENV adds pip settings (JSON) for pods without a package index."""
    root = tmp_path / "plain"
    make_notebook(root / "nb.ipynb", ["import ipykernel, yaml\nprint('kernel', ipykernel.__version__)"])
    k = {**cluster, "image": os.environ.get("TESTBENCH_TEST_KIND_PLAIN_IMAGE", "python:3.11-slim"), "setup": "pip",
         "env": json.loads(os.environ.get("TESTBENCH_TEST_KIND_PIP_ENV") or "{}")}
    write_config(root, {"project": "plain", "kubernetes": k, "plans": {"p": {"backend": "kubernetes", "steps": [
        {"name": "nb", "notebook": "nb.ipynb", "max_minutes": 3, "expect": {"text": {"stdout": ["kernel"]}}}]}}})
    r = cli(root, "run", "p", "--wait", "600")
    assert r.returncode == 0 and "# PASS - plan `p` on kubernetes" in r.stdout, r.stdout + r.stderr
    assert "[testbench] installing ipykernel" in (_latest_run(root) / "pod.log").read_text()
    assert _gone(cluster["namespace"])


@needs_kind
def test_kind_failures_say_what_to_do(kproject, cluster):
    root = kproject
    write_config(root, {"project": "kproj", "kubernetes": {**cluster, "env_secrets": {"KUBE_TOKEN": "missing/token"}},
                        "plans": {"a": {"backend": "kubernetes", "steps": [
                            {"name": "a", "run": "true", "max_minutes": 1, "secrets": ["KUBE_TOKEN"]}]}}})
    r = cli(root, "run", "a", "--wait", "300")
    assert r.returncode != 0 and 'secret "missing" not found' in r.stdout and "kubectl create secret generic" in r.stdout
    write_config(root, {"project": "kproj", "kubernetes": {**cluster, "image": "localhost:1/no-such-image:1"},
                        "plans": {"a": {"backend": "kubernetes",
                                        "steps": [{"name": "a", "run": "true", "max_minutes": 1}]}}})
    r = cli(root, "run", "a", "--wait", "300")
    assert r.returncode != 0 and "could not pull the image localhost:1/no-such-image:1" in r.stdout, r.stdout
    assert _gone(cluster["namespace"], 60)


@needs_kind
def test_kind_doctor_sees_the_cluster(kproject, cluster):
    out = cli(kproject, "doctor").stdout
    assert f"ok   kubernetes cluster: context {CONTEXT}, namespace {cluster['namespace']}, server v" in out, out
    assert f"ok   kubectl auth can-i create jobs -n {cluster['namespace']}: yes" in out

