"""Run on a Kubernetes cluster: each plan (and each live session) is a Job with one pod, on a GPU node or a CPU one.

    kubernetes:
      context: null                # a context from your kubeconfig (null: kubectl's current one)
      namespace: null              # null: the context's namespace (or default)
      image: python:3.11-slim      # what the pod runs: Python 3.10 or newer (and pip, for setup: pip)
      setup: pip                   # pip: the pod installs testbench's basics and `pip:` when it starts; none: the image
      pip: [torch==2.5.1]          #   already has ipykernel, jupyter_client, nbformat, nbclient and pyyaml
      python: python3              # the image's Python
      image_pull_policy: IfNotPresent
      usd_per_hour: {cpu: 0.0, A100: 2.5}   # per GPU by name, and `cpu` for a pod without a GPU: the GPUs plans can
                                   #   ask for are its keys, so every launch is priced
      gpu_resource: nvidia.com/gpu # the resource a GPU is requested as
      gpu_node_selector: {}        # where each GPU is: {A100: {nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB}}
      node_selector: {}            # for every pod, e.g. {pool: research}
      tolerations: []              # e.g. [{key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}]
      service_account: null        # null: no service account token in the pod
      cpu: null                    # requests and limits for the pod (a plan's cpu: / memory_gb: win)
      memory_gb: null
      env: {}                      # plain environment variables for the pod
      env_secrets: {}              # {HF_TOKEN: hf-token/token}: a secret from a Kubernetes Secret (name/key)
      volumes: []                  # more pod volumes, e.g. [{name: data, persistentVolumeClaim: {claimName: imagenet}}]
      volume_mounts: []            # and where they go: [{name: data, mountPath: /data, readOnly: true}]
      command: [kubectl]           # how to run kubectl, e.g. [kubectl, --kubeconfig, ~/.kube/lab]

The project goes in without any shared storage: the pod starts a small standard-library script that waits for it,
the project and this package are streamed in with `kubectl exec -i` (a tar.gz on stdin), and the evidence comes back
the same way (a tar.gz on stdout) while it runs and at the end; the pod waits (bounded) until it is collected. The
hard stop is Kubernetes' own: the Job's activeDeadlineSeconds is the plan's priced worst case, with backoffLimit 0
and ttlSecondsAfterFinished, and kernelkit's watchdog in the pod stops the work a minute before so its evidence can
still be taken. Secrets come from Kubernetes Secrets by reference, or from this machine over kubectl exec's stdin;
their values never appear in the Job, on a command line, or in a log.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .. import config as C
from ..config import ConfigError
from . import GPU_FORMAT, Backend, split_gpu
from .docker_backend import (BASE_PIP, DONE, KERNEL, LIST, PROBE, PROJECT, READY, RUN, WATCH, WORK, _extract_run, _tail,
                             bundle)

START = f"{WORK}/start.json"             # what the pod runs once the project is in (and secrets from here); then deleted
COLLECTED = f"{WORK}/collected"          # written once the evidence has been taken: the pod may end
STOP_MARGIN = 60                         # seconds the watchdog stops the work before the Job's deadline
LINGER_SECONDS = 180                     # after a plan, the pod waits at most this long for its evidence to be taken
IDLE_SLACK = 10                          # the pod's watchdog waits this much longer than the supervisor here
TTL_SECONDS = 600                        # ttlSecondsAfterFinished: Kubernetes deletes a finished Job after this
CONTAINER = "main"
REMOTE_STOP = ("by Kubernetes itself: the Job's activeDeadlineSeconds ends the pod at the worst case, and its watchdog "
               "stops the work just before, even if testbench on this machine has stopped")
FATAL = {"ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName", "CreateContainerConfigError"}

# The pod's first process, standard library only (the package is not there yet): it waits for the project, takes
# what to run (and secrets sent from here) from start.json and deletes it, installs testbench's basics if asked,
# then becomes the in-pod side of this module.
BOOTSTRAP = r'''
import json, os, subprocess, sys, time
cfg = json.loads(sys.argv[1])
start, end = "/work/start.json", time.time() + cfg["wait"]
print("[testbench] waiting for the project to be copied in", flush=True)
while not os.path.exists(start):
    if time.time() > end:
        sys.exit("[testbench] nothing was copied into this pod in time; it ends here")
    time.sleep(0.5)
with open(start) as f:
    go = json.load(f)
os.remove(start)
began = time.time()
if cfg["pip"]:
    print("[testbench] installing " + " ".join(cfg["pip"]), flush=True)
    if subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *cfg["pip"]]).returncode:
        sys.exit("[testbench] pip could not install testbench's basics in the pod: it needs a package index (or use an "
                 "image that has them, with kubernetes.setup: none)")
left = go["minutes"] - (time.time() - began) / 60       # the time installing took is not the work's
if left <= 0:
    sys.exit("[testbench] installing took all the time the Job has left; raise the steps' max_minutes, or use an "
             "image that has the packages (kubernetes.setup: none)")
argv = [f"{left:.2f}" if a == "{minutes}" else a for a in go["argv"]]
os.execve(sys.executable, [sys.executable, "-m", "agent_testbench.backends.kubernetes_backend", *argv],
          {**os.environ, **go["env"]})
'''

UNPACK = ("import sys, tarfile\n"
          "with tarfile.open(fileobj=sys.stdin.buffer, mode='r|gz') as t:\n"
          "    t.extractall('/', **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))\n")

# stdin to a file, written whole before it appears (start.json is read the moment it exists)
WRITE = ("import os, sys\n"
         "p, mode = sys.argv[1], int(sys.argv[2], 8)\n"
         "os.makedirs(os.path.dirname(p), exist_ok=True)\n"
         "with os.fdopen(os.open(p + '.part', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), 'wb') as f:\n"
         "    f.write(sys.stdin.buffer.read())\n"
         "os.replace(p + '.part', p)\n")

READ = ("import os, shutil, sys\n"
        "if not os.path.isfile(sys.argv[1]):\n"
        "    sys.exit('no such file in the pod: ' + sys.argv[1])\n"
        "shutil.copyfileobj(open(sys.argv[1], 'rb'), sys.stdout.buffer)\n")

INTERRUPT = "import os, signal; os.kill(1, signal.SIGINT)"
MARK_COLLECTED = f"open({COLLECTED!r}, 'w').close()"


class KubeError(RuntimeError):
    pass


class Kubectl:
    """kubectl for one context and namespace. Every command is logged (with its exit code and the end of its
    stderr); what goes in on stdin (the project, secrets) never is."""

    def __init__(self, command: list[str], context: str | None, namespace: str | None, log: Path | None = None,
                 error: type[Exception] = KubeError):
        self.command, self.context, self.namespace, self.log, self.error = list(command), context, namespace, log, error
        self.base = [*self.command, *(["--context", context] if context else []),
                     *(["--namespace", namespace] if namespace else [])]
        self.where = f"context {context or '(current)'}, namespace {namespace or '(the context default)'}"

    def __call__(self, *args, data: bytes | None = None, stdin=None, stdout=None, timeout: float = 600,
                 check: bool = True, binary: bool = False) -> subprocess.CompletedProcess:
        argv = [*self.base, *map(str, args)]
        shown = shlex.join(a if len(a) <= 160 else a[:60].split("\n")[0] + " ..." for a in argv)   # scripts cut short
        began = time.time()
        try:
            p = subprocess.run(argv, input=data, stdin=None if data is not None else (stdin or subprocess.DEVNULL),
                               stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except FileNotFoundError:
            raise self.error(f"`{self.command[0]}` is not installed here: install kubectl (https://kubernetes.io/docs/"
                             "tasks/tools/), or set kubernetes.command to how you run it") from None
        except subprocess.TimeoutExpired:
            raise self.error(f"`{shown[:160]}` did not answer within {timeout:.0f} s ({self.where})") from None
        err = p.stderr.decode(errors="replace")
        if self.log:
            with self.log.open("a") as f:
                f.write(f"$ {shown}  ({time.time() - began:.1f} s, exit {p.returncode})\n"
                        + (err[-1500:] + ("" if err.endswith("\n") else "\n") if err.strip() else ""))
        if check and p.returncode != 0:
            raise self.error(explain(err, shown, p.returncode, self.where))
        out = p.stdout if (binary or p.stdout is None) else p.stdout.decode(errors="replace")
        return subprocess.CompletedProcess(argv, p.returncode, out, err)

    def exec(self, pod: str, *argv, data: bytes | None = None, stdin=None, **kw) -> subprocess.CompletedProcess:
        piped = ["-i"] if data is not None or stdin is not None else []
        return self("exec", *piped, pod, "-c", CONTAINER, "--", *argv, data=data, stdin=stdin, **kw)

    def objects(self, job: str) -> tuple[dict | None, list[dict]]:
        """The Job and its pods (newest first), in one request."""
        items = json.loads(self("get", "jobs,pods", "-l", f"agent-testbench/job={job}", "-o", "json",
                                timeout=120).stdout or "{}").get("items", [])
        pods = sorted((i for i in items if i.get("kind") == "Pod"),
                      key=lambda p: p["metadata"].get("creationTimestamp", ""), reverse=True)
        return next((i for i in items if i.get("kind") == "Job"), None), pods

    def delete(self, job: str) -> subprocess.CompletedProcess:
        """The Job, and with it its pod (kubectl's default background cascade)."""
        return self("delete", "job", job, "--ignore-not-found", "--wait=false", timeout=120)


def explain(err: str, shown: str, code: int, where: str) -> str:
    """kubectl's error, with what to do about the usual ones."""
    text = err.strip()[-800:] or "no reason given"
    low = text.lower()
    if any(s in low for s in ("unable to connect", "connection refused", "no configuration has been provided",
                              "current-context is not set", "no such host", "i/o timeout", "context was not found",
                              "does not exist")) and "namespaces" not in low:
        return (f"kubectl could not reach the cluster ({where}): {text}. Check that `kubectl get pods` works for that "
                "context (`testbench doctor` shows it), or set kubernetes.context")
    if "forbidden" in low:
        return (f"the cluster refused ({where}): {text}. Your account needs to create, get and delete Jobs, get pods "
                "and their logs, and create pods/exec in that namespace (`kubectl auth can-i create jobs` checks one)")
    if "namespaces" in low and "not found" in low:
        return f"{text} - create the namespace, or set kubernetes.namespace to one that exists"
    return f"`{shown[:160]}` exited {code}: {text}"


def k8s_name(*parts: str) -> str:
    """A name Kubernetes takes for a Job and its label: lowercase letters, digits and -, at most 63 characters (a
    short hash keeps apart two names cut to the same text)."""
    raw = "-".join(parts)
    text = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    return f"{text[:52].rstrip('-')}-{hashlib.sha256(raw.encode()).hexdigest()[:6]}"


def _label(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value))[:63].strip("-_.") or "x"


def plan_limit(plan: dict) -> float:
    """The plan's worst case in minutes, which is also its Job's deadline: its steps and the start-up allowance."""
    return sum(st["max_minutes"] for st in plan["steps"]) + backend.startup_minutes


def price_table(k: dict) -> dict:
    """kubernetes.usd_per_hour as {"cpu": rate, "A100": rate, ...}; a plain number is the price of a pod without GPU."""
    raw = k.get("usd_per_hour")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = {"cpu": raw}
    example = "{cpu: 0.0, A100: 2.5}"
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"`kubernetes.usd_per_hour` is what a pod costs per hour, by GPU and `cpu` for a pod without "
                          f"one, e.g. {example} (0 for your own cluster)")
    out = {}
    for name, rate in raw.items():
        if not C.number(rate):
            raise ConfigError(f"`kubernetes.usd_per_hour.{name}` is US dollars per hour (per GPU), e.g. 2.5")
        out["cpu" if str(name).lower() == "cpu" else str(name).upper()] = float(rate)
    return out


def gpu_rate(k: dict, gpu, where: str) -> float:
    """US dollars per hour for a pod with `gpu` (NAME or NAME:COUNT; none: the cpu price). Nothing unpriced runs."""
    table = price_table(k)
    name, count = split_gpu(gpu)
    gpus = [g for g in table if g != "cpu"]
    if not name:
        if "cpu" not in table:
            raise ConfigError(f"`kubernetes.usd_per_hour` has no price for a pod without a GPU ({where}); add cpu: "
                              "<dollars per hour>, e.g. {cpu: 0.0}")
        return table["cpu"]
    if name not in table:
        offers = f"it prices {', '.join(gpus)}" if gpus else "it prices no GPU"
        raise ConfigError(f"`{where}.gpu` is {gpu!r}, but kubernetes.usd_per_hour has no price for {name} ({offers}); add "
                          f"{name}: <dollars per hour per GPU> to it, so the worst case is known before anything runs")
    return table[name] * count


def _secret_ref(where: str, value) -> tuple[str, str]:
    """'secret-name/key' -> (name, key). The value is never repeated in a message: it might be the secret itself."""
    m = re.fullmatch(r"([a-z0-9]([-a-z0-9.]*[a-z0-9])?)/([-._A-Za-z0-9]+)", value) if isinstance(value, str) else None
    if not m:
        raise ConfigError(f"`{where}` must be SECRET/KEY: the name of a Kubernetes Secret in the namespace and a key in "
                          "it, e.g. hf-token/token (made with `kubectl create secret generic hf-token "
                          "--from-literal=token=...`); never the value itself")
    return m.group(1), m.group(3)


def _hardware(k: dict, hw: dict, where: str) -> tuple[dict, dict]:
    """(resources, node selector) for a pod with this hardware ({gpu, cpu?, memory_gb?})."""
    name, count = split_gpu(hw.get("gpu"))
    gpu_rate(k, hw.get("gpu"), where)                  # priced, or it does not start
    res: dict = {}
    cpu, mem = hw.get("cpu") or k.get("cpu"), hw.get("memory_gb") or k.get("memory_gb")
    if cpu:
        res["cpu"] = f"{float(cpu):g}"
    if mem:
        res["memory"] = f"{int(float(mem) * 1024)}Mi"
    if name:
        res[k["gpu_resource"]] = str(count)
    selector = {**(k.get("node_selector") or {}), **((k.get("gpu_node_selector") or {}).get(name) or {})}
    return ({"requests": dict(res), "limits": dict(res)} if res else {}), selector


def manifest(cfg: dict, name: str, kind: str, deadline: int, hw: dict, secret_names: list[str], where: str,
             env: dict | None = None, labels: dict | None = None) -> dict:
    """The Job for one run or session (`where` names it in messages). Its hard stop is Kubernetes' own:
    activeDeadlineSeconds from the worst case, no retries, deleted after it finishes. Secrets appear only as
    references to Kubernetes Secrets."""
    k = cfg["kubernetes"]
    resources, selector = _hardware(k, hw, where)
    plain = {"PYTHONPATH": f"{WORK}/pkg", "TESTBENCH_CACHE": f"{WORK}/cache", "PYTHONUNBUFFERED": "1",
             "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_ROOT_USER_ACTION": "ignore", "PIP_BREAK_SYSTEM_PACKAGES": "1",
             **{n: os.environ[n] for n in ("TESTBENCH_WATCHDOG_SECONDS",) if n in os.environ},
             **{str(a): str(b) for a, b in (k.get("env") or {}).items()}, **(env or {})}
    variables = [{"name": a, "value": b} for a, b in plain.items()]
    for var in secret_names:
        secret, key = _secret_ref(f"kubernetes.env_secrets.{var}", k["env_secrets"][var])
        variables.append({"name": var, "valueFrom": {"secretKeyRef": {"name": secret, "key": key}}})
    tags = {"app.kubernetes.io/managed-by": "agent-testbench", "agent-testbench/project": _label(cfg["project"]),
            "agent-testbench/kind": kind, "agent-testbench/job": name,
            **{f"agent-testbench/{a}": _label(b) for a, b in (labels or {}).items()}}
    pip = sorted(set(BASE_PIP + list(k.get("pip") or []))) if k["setup"] == "pip" else []
    boot = json.dumps({"wait": int(backend.startup_minutes * 60 + 60), "pip": pip})
    container = {"name": CONTAINER, "image": k["image"], "imagePullPolicy": k["image_pull_policy"],
                 "command": [k["python"], "-c", BOOTSTRAP, boot], "env": variables,
                 "volumeMounts": [{"name": "work", "mountPath": WORK}, *(k.get("volume_mounts") or [])]}
    if resources:
        container["resources"] = resources
    pod = {"restartPolicy": "Never", "terminationGracePeriodSeconds": 30, "enableServiceLinks": False,
           "containers": [container], "volumes": [{"name": "work", "emptyDir": {}}, *(k.get("volumes") or [])]}
    if k.get("service_account"):
        pod["serviceAccountName"] = k["service_account"]
    else:
        pod["automountServiceAccountToken"] = False
    if selector:
        pod["nodeSelector"] = selector
    if k.get("tolerations"):
        pod["tolerations"] = k["tolerations"]
    meta = {"name": name, "labels": tags,
            "annotations": {"agent-testbench/hard-stop": f"activeDeadlineSeconds {deadline}: the worst case testbench "
                                                         "priced before it started"}}
    return {"apiVersion": "batch/v1", "kind": "Job", "metadata": meta,
            "spec": {"activeDeadlineSeconds": int(deadline), "backoffLimit": 0, "ttlSecondsAfterFinished": TTL_SECONDS,
                     "template": {"metadata": {"labels": tags}, "spec": pod}}}


def split_secrets(k: dict, names: list[str]) -> tuple[list[str], dict]:
    """(names that come from Kubernetes Secrets, {name: value} sent from this machine over kubectl exec's stdin)."""
    refs = [n for n in names if n in (k.get("env_secrets") or {})]
    local = [n for n in names if n not in refs]
    missing = [n for n in local if n not in os.environ]
    if missing:
        raise KubeError(f"secrets not in this environment: {', '.join(missing)} - export them first, or map them to a "
                        "Kubernetes Secret in kubernetes.env_secrets (e.g. NAME: secret-name/key)")
    return refs, {n: os.environ[n] for n in local}


def _command(cmd) -> list[str]:
    return shlex.split(cmd) if isinstance(cmd, str) else list(cmd or ["kubectl"])


def pin(k: dict, log: Path | None = None, error: type[Exception] = KubeError) -> Kubectl:
    """kubectl for the context and namespace a run uses, fixed when it starts, so cancelling later finds the same Job
    even if the kubeconfig's current context changes meanwhile."""
    cmd = _command(k.get("command"))
    kube = Kubectl(cmd, None, None, log, error)
    ctx = k.get("context")
    if not ctx:
        p = kube("config", "current-context", check=False, timeout=60)
        ctx = p.stdout.strip() or None
        if p.returncode != 0 or not ctx:
            raise kube.error("kubectl has no current context: set kubernetes.context, or choose one with "
                             "`kubectl config use-context NAME`")
    ns = k.get("namespace")
    if not ns:
        p = kube("config", "view", "--minify", "--context", ctx, "-o", "jsonpath={..namespace}", check=False, timeout=60)
        ns = (p.stdout.strip().split() or ["default"])[0] if p.returncode == 0 else "default"
    return Kubectl(cmd, ctx, ns, log, error)


def _waiting(pod: dict) -> tuple[str, str] | None:
    """(reason, message) for a pod that has not started: unschedulable, pulling, a bad image or a missing Secret."""
    status = pod.get("status") or {}
    for c in status.get("containerStatuses") or []:
        w = (c.get("state") or {}).get("waiting")
        if w and w.get("reason") not in (None, "ContainerCreating", "PodInitializing"):
            return w["reason"], w.get("message") or w["reason"]
    for cond in status.get("conditions") or []:
        if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
            return cond.get("reason") or "Unschedulable", cond.get("message") or "no node can take it yet"
    return None


def _running(pod: dict) -> bool:
    return any("running" in (c.get("state") or {}) for c in (pod.get("status") or {}).get("containerStatuses") or [])


def _over(pod: dict | None) -> bool:
    return bool(pod) and (pod.get("status") or {}).get("phase") in ("Succeeded", "Failed")


def why_ended(job: dict | None, pod: dict | None) -> str:
    """Why the Job or its pod ended, in plain words ('' if nothing says)."""
    for cond in ((job or {}).get("status") or {}).get("conditions") or []:
        if cond.get("type") in ("Failed", "FailureTarget") and cond.get("status") == "True":
            if cond.get("reason") == "DeadlineExceeded":
                return (f"Kubernetes stopped the Job at its deadline (activeDeadlineSeconds "
                        f"{job['spec'].get('activeDeadlineSeconds')}: the worst case priced before it started)")
            return f"the Job failed: {cond.get('reason')}: {cond.get('message', '')}".rstrip(": ")
    status = (pod or {}).get("status") or {}
    if status.get("reason") == "Evicted":
        return f"the node evicted the pod: {status.get('message', '')}"
    for c in status.get("containerStatuses") or []:
        t = (c.get("state") or {}).get("terminated")
        if t and t.get("reason") == "OOMKilled":
            return "the pod ran out of memory (OOMKilled): raise kubernetes.memory_gb or the plan's memory_gb"
        if t and t.get("exitCode"):
            return f"the pod's process exited {t['exitCode']} ({t.get('reason', '')})"
    return ""


class Pod:
    """The Job's pod, found and started: what a run and a session share."""

    def __init__(self, cfg: dict, kube: Kubectl, job: str, created: float, deadline: int, say=print):
        self.cfg, self.kube, self.job, self.created, self.deadline, self.say = cfg, kube, job, created, deadline, say
        self.name: str | None = None
        self.python = cfg["kubernetes"]["python"]

    def wait(self) -> str:
        """The pod's name once its container runs; a plain error if it cannot start within the start-up allowance."""
        allowance = backend.startup_minutes * 60
        poll = float(os.environ.get("TESTBENCH_K8S_POLL_SECONDS", 2))
        last, misses = None, 0
        while True:
            try:
                job, pods = self.kube.objects(self.job)
                misses = 0
            except KubeError:
                misses += 1                                  # once is the API's hiccup; three times is an answer
                if misses >= 3:
                    raise
                time.sleep(poll)
                continue
            pod = pods[0] if pods else None
            if pod:
                self.name = pod["metadata"]["name"]
            if (job and why_ended(job, None)) or _over(pod):
                raise KubeError(f"the pod ended before the project was copied in: "
                                f"{why_ended(job, pod) or 'no reason given'}. Its log:\n{self.log_tail()}")
            if pod and _running(pod):
                return self.name
            problem = _waiting(pod) if pod else None
            if problem and problem[0] in FATAL:
                raise KubeError(self._fatal(*problem))
            if problem and problem != last:
                self.say(f"[testbench] the pod is waiting: {problem[1]}", flush=True)
                last = problem
            if time.time() - self.created > allowance:
                why = last[1] if last else f"the Job made no pod (`kubectl describe job {self.job}` says why)"
                raise KubeError(f"the pod did not start within {allowance / 60:g} min: {why}. Check that the cluster has "
                                "a free node for it (kubernetes.node_selector, tolerations, gpu_node_selector, and the "
                                "GPUs, CPU and memory it asks for)")
            time.sleep(poll)

    def _fatal(self, reason: str, message: str) -> str:
        k = self.cfg["kubernetes"]
        if reason == "CreateContainerConfigError":
            return (f"the pod could not be set up: {message}. Every Secret in kubernetes.env_secrets that a step uses "
                    f"must exist in namespace {self.kube.namespace} with that key (kubectl create secret generic NAME "
                    "--from-literal=KEY=...)")
        if reason == "ErrImageNeverPull":
            return f"with kubernetes.image_pull_policy: Never the image must already be on the node: {message}"
        return (f"the cluster could not pull the image {k['image']} ({reason}): {message}. Check kubernetes.image; for a "
                "private registry, give kubernetes.service_account an image pull secret")

    def start(self, root: Path | None, exclude: list[str], argv: list[str], env: dict, files: dict | None = None,
              pkg: bool = True) -> float:
        """Copy the project and this package in, then what to run (with secrets from here) over stdin. "{minutes}" in
        argv becomes the minutes the work may take: up to STOP_MARGIN before the Job's deadline (returned), less what
        installing in the pod takes, which the pod measures on its own clock."""
        with bundle(root, exclude, pkg=pkg, files=files) as archive:
            self.kube.exec(self.name, self.python, "-c", UNPACK, stdin=archive, timeout=1800)
        left = (self.created + self.deadline - STOP_MARGIN - time.time()) / 60
        if left <= 0:
            raise KubeError(f"the pod started too late: nothing can run before the Job's deadline "
                            f"({self.deadline} s after it was created)")
        self.put(START, json.dumps({"argv": argv, "minutes": left, "env": env}).encode(), mode="600", timeout=120)
        return left

    def put(self, path: str, data: bytes, mode: str = "644", timeout: float = 1800) -> None:
        self.kube.exec(self.name, self.python, "-c", WRITE, path, mode, data=data, timeout=timeout)

    def get(self, path: str) -> bytes:
        p = self.kube.exec(self.name, self.python, "-c", READ, path, check=False, binary=True, timeout=1800)
        if p.returncode != 0:
            raise self.kube.error(p.stderr.strip()[-500:] or f"could not read {path} in the pod")
        return p.stdout

    def log_tail(self, n: int = 2000) -> str:
        if not self.name:
            return "(no pod)"
        try:
            p = self.kube("logs", self.name, "-c", CONTAINER, "--tail", "200", check=False, timeout=120)
        except KubeError as exc:
            return str(exc)
        return (p.stdout or p.stderr)[-n:]


class KubernetesRunner:
    """A plan as a Job: the executor runs in its pod under the watchdog; progress comes back every few seconds and
    everything at the end, then the Job is deleted."""

    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta
        k = cfg["kubernetes"]
        self.kube = Kubectl(k["command"], k.get("context"), k.get("namespace"), self.run_dir / "kubectl.log")
        self.started = self.name = self.pod = None

    def run(self):
        from .. import report as R, runs
        k = self.cfg["kubernetes"]
        self.started = time.time()
        refs, local = split_secrets(k, sorted({n for st in self.plan["steps"] for n in st["secrets"]}))
        self.kube = kube = pin(k, self.kube.log)
        limit = plan_limit(self.plan)
        deadline = math.ceil(limit * 60)
        name = k8s_name("tb", self.cfg["project"], self.run_dir.name)
        runs.write_state(self.run_dir, kube_command=k["command"], kube_context=kube.context,
                         kube_namespace=kube.namespace, kube_job=name, remote_stop=REMOTE_STOP)
        job = manifest(self.cfg, name, "plan", deadline, self.plan, refs, f"plans.{self.meta.get('plan')}",
                       labels={"plan": self.meta.get("plan", ""), "run": self.run_dir.name})
        self.name = name                                     # from here on, cancel deletes it
        created = time.time()
        self.kube("create", "-f", "-", "-o", "name", data=json.dumps(job).encode(), timeout=120)
        print(f"[testbench] hard stop after {limit:g} min {REMOTE_STOP}", flush=True)
        print(f"[testbench] Job {name} created in {self.kube.where}; waiting for its pod", flush=True)
        self.pod = Pod(self.cfg, self.kube, name, created, deadline)
        self.pod.wait()
        py = k["python"]
        command = ["plan", "--max-minutes", "{minutes}", "--linger-seconds", str(LINGER_SECONDS), "--",
                   py, "-m", "agent_testbench.executor", "--plan", f"{WORK}/plan.json", "--project", PROJECT,
                   "--run-dir", RUN, "--workdir", PROJECT, "--meta", json.dumps(self.meta)]
        left = self.pod.start(self.root, self.cfg["exclude"], command, local,
                              files={f"{WORK}/plan.json": json.dumps(self.plan).encode()})
        print(f"[testbench] running the plan in pod {self.pod.name} from {k['image']} (watchdog at {left:.1f} min)",
              flush=True)
        give_up = created + deadline + LINGER_SECONDS + 300      # Kubernetes ends it long before this
        poll = float(os.environ.get("TESTBENCH_K8S_POLL_SECONDS", 2))
        while time.time() < give_up:
            done = self._mirror(full=False)
            if done:
                break
            if done is None:
                try:
                    job_obj, pods = self.kube.objects(name)
                except KubeError:                            # the API did not answer this time; the deadline holds
                    time.sleep(poll)
                    continue
                if job_obj is None or why_ended(job_obj, None) or _over(pods[0] if pods else None):
                    break
            time.sleep(poll)
        self._mirror(full=True)
        try:
            job_obj, pods = self.kube.objects(name)
            why = why_ended(job_obj, pods[0] if pods else None)
        except KubeError:
            why = ""
        self._close()
        rep = R.load(self.run_dir)
        stopped = self.run_dir / "watchdog" / "watchdog_stopped"
        if rep and rep.get("status") == "cancelled":         # interrupted in the pod: say by what
            if why:
                rep["stopped_because"] = why
            elif stopped.exists():
                rep["stopped_because"] = f"the pod's watchdog: {stopped.read_text().strip()}"
        if rep and rep.get("status") == "running":
            rep.update(status="error", finished=time.time(), stopped_because=(why or "the executor in the pod ended "
                       "before the plan finished") + "; see pod.log in the run")
        if not rep:
            rep = {**self.meta, "steps": [], "started": self.started, "status": "error",
                   "stopped_because": "the plan never started in the pod" + (f": {why}" if why else ""),
                   "launch_error": "The pod could not start the plan. Its log:\n\n```\n"
                                   f"{_tail(self.run_dir / 'pod.log')}\n```\n\nkubectl commands:\n\n```\n"
                                   f"{_tail(self.run_dir / 'kubectl.log')}\n```\n\nWith kubernetes.setup: none the "
                                   f"image needs {k['python']} with ipykernel, jupyter_client, nbformat, nbclient and "
                                   "pyyaml; with setup: pip, the pod needs a package index."}
        return rep, self.cost(rep)

    def _mirror(self, full: bool) -> bool | None:
        """Bring the run folder back: progress and logs while it runs, everything at the end. True once the work
        has ended; None if the pod could not be asked (not running, or gone)."""
        if not (self.pod and self.pod.name):
            return None
        with tempfile.TemporaryFile() as out:
            try:
                p = self.kube.exec(self.pod.name, self.cfg["kubernetes"]["python"], "-c", PROBE,
                                   "full" if full else "light", stdout=out, check=False, timeout=1800, binary=True)
            except KubeError:
                return None
            if p.returncode != 0:
                return None
            out.seek(0)
            try:
                return _extract_run(out, self.run_dir)
            except Exception:
                return None

    def _close(self) -> None:
        """Keep the pod's log, let the pod end, and delete the Job (its pod goes with it)."""
        if self.pod and self.pod.name:
            try:
                p = self.kube("logs", self.pod.name, "-c", CONTAINER, "--tail", "5000", check=False, timeout=120)
                if p.stdout:
                    (self.run_dir / "pod.log").write_text(p.stdout)
                self.kube.exec(self.pod.name, self.cfg["kubernetes"]["python"], "-c", MARK_COLLECTED, check=False,
                               timeout=60)
            except KubeError:
                pass
        if self.name:
            try:
                self.kube.delete(self.name)
            except KubeError as exc:
                print(f"[testbench] could not delete the Job {self.name}: {exc} (Kubernetes deletes it "
                      f"{TTL_SECONDS} s after it ends)", flush=True)

    def cancel(self) -> str:
        if not self.name:
            return ""
        if self.pod and self.pod.name:
            try:
                p = self.kube.exec(self.pod.name, self.cfg["kubernetes"]["python"], "-c", INTERRUPT, check=False,
                                   timeout=60)                    # the executor writes its report and ends
                if p.returncode == 0:
                    end = time.time() + 60
                    while time.time() < end and self._mirror(full=False) is False:
                        time.sleep(0.5)
                    self._mirror(full=True)
            except KubeError:
                pass
        self._close()
        return f"stopped and deleted the Job {self.name} ({self.kube.where})"

    def cost(self, rep: dict) -> float:
        """The time since it started, at most the Job's deadline (the cluster ends the pod there)."""
        minutes = min((time.time() - self.started) / 60, plan_limit(self.plan)) if self.started else 0.0
        return backend.rate_per_minute(self.cfg, self.plan) * minutes


class KubernetesSession:
    """A kernel in a Job's pod, in a copy of the project; the same launcher and client as everywhere else. The pod
    lives as long as its kernel, and never past the Job's deadline."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        from ..session_backends import SessionError
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        h = state.get("handles") or {}
        k = cfg.get("kubernetes") or {}
        self.job, self.pod_name = h.get("job"), h.get("pod")
        self.python = h.get("python") or k.get("python", "python3")
        self.kube = Kubectl(_command(h.get("command") or k.get("command")), h.get("context", k.get("context")),
                            h.get("namespace", k.get("namespace")), self.sdir / "kubectl.log", error=SessionError)

    def _exec(self, *argv, **kw):
        return self.kube.exec(self.pod_name, *argv, **kw)

    def start(self) -> dict:
        from ..session_backends import SessionError
        try:
            backend.validate(self.cfg)
            k, s = self.cfg["kubernetes"], self.state
            refs, local = split_secrets(k, list(s.get("secrets") or []))
        except (ConfigError, KubeError) as exc:
            raise SessionError(str(exc)) from None
        self.kube = kube = pin(k, self.kube.log, SessionError)
        deadline = math.ceil((s["max_minutes"] + backend.startup_minutes) * 60)
        name = k8s_name("tb", self.cfg["project"], s["name"], hashlib.sha256(
            f"{self.sdir.resolve()} {s.get('created')}".encode()).hexdigest()[:6])
        handles = {"job": name, "context": kube.context, "namespace": kube.namespace, "command": k["command"],
                   "python": self.python,
                   "workdir": PROJECT, "remote_stop": REMOTE_STOP}
        s["handles"] = handles
        hw = {"gpu": s.get("gpu"), "cpu": s.get("cpu"), "memory_gb": s.get("memory_gb")}
        try:
            job = manifest(self.cfg, name, "session", deadline, hw, refs, "sessions",
                           env={"TESTBENCH_SESSION": s["name"]}, labels={"session": s["name"]})
        except ConfigError as exc:
            raise SessionError(str(exc)) from None
        self.job = name                                      # from here on, stop deletes it
        created = time.time()
        self.kube("create", "-f", "-", "-o", "name", data=json.dumps(job).encode(), timeout=120)
        pod = Pod(self.cfg, self.kube, name, created, deadline, say=lambda *a, **kw: None)
        try:
            self.pod_name = handles["pod"] = pod.wait()
            command = ["session", "--max-minutes", "{minutes}", "--idle-minutes", f"{s['idle_minutes'] + IDLE_SLACK:g}",
                       "--", self.python, "-m", "agent_testbench.kernelkit.launcher", "--workdir", PROJECT,
                       "--state-dir", KERNEL, "--shim"]
            pod.start(self.root, self.cfg["exclude"], command, local)
        except KubeError as exc:
            raise SessionError(str(exc)) from None
        end = created + backend.startup_minutes * 60 + 120          # room to pip install the basics
        while time.time() < end:
            if self._exec(self.python, "-c", READY, check=False, timeout=60).returncode == 0:
                return handles
            _, pods = self.kube.objects(name)
            if not pods or _over(pods[0]):
                break
            time.sleep(1)
        raise SessionError(f"the kernel did not start in pod {self.pod_name}: {pod.log_tail(1500)}")

    def alive(self) -> bool:
        if not self.pod_name:
            return False
        try:
            return self._exec(self.python, "-c", READY, check=False, timeout=60).returncode == 0
        except RuntimeError:
            return False

    def _pod(self) -> Pod:
        p = Pod(self.cfg, self.kube, self.job, 0, 0)
        p.name, p.python = self.pod_name, self.python
        return p

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        from ..session_backends import _parse
        req = f"{WORK}/out/{prefix}.json"
        pod = self._pod()
        pod.put(req, json.dumps({"code": code, "timeout": timeout, "prefix": prefix}).encode(), mode="600")
        p = self._exec(self.python, "-m", "agent_testbench.kernelkit.client", "--connection", f"{KERNEL}/kernel.json",
                       "--request", req, "--out-dir", f"{WORK}/out/{prefix}", timeout=timeout + 180, check=False)
        res = _parse(p.stdout)
        local = []
        for path in res.get("images", []):
            dst = Path(out_dir) / Path(path).name
            dst.write_bytes(pod.get(path))
            local.append(str(dst))
        res["images"] = local
        return res

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{PROJECT}/{path}"

    def ls(self, path: str) -> list[str]:
        p = self._exec(self.python, "-c", LIST, self._abs(path or "."), check=False, timeout=60)
        if p.returncode != 0:
            raise self.kube.error(p.stderr.strip()[-500:] or f"could not list {path}")
        return [ln for ln in p.stdout.splitlines() if ln.strip()]

    def get(self, remote: str, local: Path) -> None:
        Path(local).write_bytes(self._pod().get(self._abs(remote)))

    def put(self, local: Path, remote: str) -> None:
        self._pod().put(self._abs(remote), Path(local).read_bytes())

    def _busy(self, script: str, timeout: float) -> subprocess.CompletedProcess:
        """A shell command in the project's copy, counted as activity by the pod's watchdog."""
        k = KERNEL
        return self._exec("sh", "-c", f"echo {time.time() + timeout + 120:.0f} > {k}/busy; cd {PROJECT} && "
                                      f"{{ {script}\n}} 2>&1; rc=$?; rm -f {k}/busy; : > {k}/last_used; exit $rc",
                          timeout=timeout + 60, check=False)

    def install(self, packages: list[str]) -> str:
        p = self._busy(f"{shlex.quote(self.python)} -m pip install {' '.join(shlex.quote(x) for x in packages)}", 1800)
        return p.stdout[-3000:] + ("" if p.returncode == 0 else f"\n(pip exited {p.returncode})")

    def sync(self) -> str:
        with bundle(self.root, self.cfg["exclude"], pkg=False) as archive:
            self._exec(self.python, "-c", UNPACK, stdin=archive, timeout=1800)
        self._exec("sh", "-c", f": > {KERNEL}/last_used", check=False, timeout=60)
        return f"project files copied to {PROJECT} in pod {self.pod_name} (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        return self._busy(command, timeout).stdout[-20000:]

    def stop(self) -> None:
        if not self.job:
            return
        try:
            self.kube.delete(self.job)
        except RuntimeError:
            pass


class KubernetesBackend(Backend):
    name = "kubernetes"
    startup_minutes = 5.0             # scheduling, pulling the image, installing the basics, copying the project
    config_defaults = {"kubernetes": {
        "context": None, "namespace": None, "image": "python:3.11-slim", "setup": "pip", "pip": [], "python": "python3",
        "image_pull_policy": "IfNotPresent", "usd_per_hour": {"cpu": 0.0}, "gpu_resource": "nvidia.com/gpu",
        "gpu_node_selector": {}, "node_selector": {}, "tolerations": [], "service_account": None, "cpu": None,
        "memory_gb": None, "env": {}, "env_secrets": {}, "volumes": [], "volume_mounts": [], "command": ["kubectl"]}}

    def check_hardware(self, where: str, gpu) -> None:
        """The GPUs on offer are kubernetes.usd_per_hour's keys; validate and rate_per_minute check them."""
        if gpu and not GPU_FORMAT.fullmatch(str(gpu)):
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write a GPU name, or NAME:COUNT for several (e.g. A100:2), "
                              "with no spaces")

    def validate(self, cfg: dict) -> None:
        k = cfg["kubernetes"]

        def text(key: str, example: str, required: bool = True, pattern: str | None = None) -> None:
            v = k.get(key)
            if v is None and not required:
                return
            if not isinstance(v, str) or not v.strip() or (pattern and not re.fullmatch(pattern, v)):
                raise ConfigError(f"`kubernetes.{key}` {example}")

        text("context", "is a context from your kubeconfig (`kubectl config get-contexts`), or leave it out for the "
                        "current one", required=False)
        text("namespace", "is a namespace name (lowercase letters, digits and -), or leave it out for the context's",
             required=False, pattern=r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
        text("image", "is the image the pod runs, e.g. python:3.11-slim (Python 3.10 or newer)")
        text("python", "is the image's Python command, e.g. python3")
        text("gpu_resource", "is the resource a GPU is requested as, e.g. nvidia.com/gpu or amd.com/gpu")
        text("service_account", "is a service account in the namespace, or leave it out", required=False,
             pattern=r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?")
        if k.get("setup") not in ("pip", "none"):
            raise ConfigError("`kubernetes.setup` is pip (the pod installs testbench's basics and kubernetes.pip when it "
                              "starts) or none (kubernetes.image already has ipykernel, jupyter_client, nbformat, "
                              "nbclient and pyyaml)")
        if k.get("image_pull_policy") not in ("IfNotPresent", "Always", "Never"):
            raise ConfigError("`kubernetes.image_pull_policy` is IfNotPresent, Always or Never")
        pip = k.get("pip") or []
        if not isinstance(pip, list) or not all(isinstance(x, str) for x in pip):
            raise ConfigError("`kubernetes.pip` is a list of packages the pod installs (pin them): [torch==2.5.1]")
        if pip and k["setup"] == "none":
            raise ConfigError("`kubernetes.pip` needs kubernetes.setup: pip (with setup: none, the image is used as it "
                              "is); put the packages in your image, or use setup: pip")
        cmd = k.get("command")
        if isinstance(cmd, str):
            k["command"] = cmd = shlex.split(cmd)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ConfigError("`kubernetes.command` is how to run kubectl, as a list: [kubectl] or [kubectl, "
                              "--kubeconfig, /path/to/config]")
        for key, example in (("node_selector", "{pool: gpu}"), ("env", "{HF_HOME: /work/cache/hf}")):
            v = k.get(key)
            if v is None:
                v = k[key] = {}
            if not isinstance(v, dict) or not all(isinstance(a, str) and isinstance(b, (str, int, float))
                                                  and not isinstance(b, bool) for a, b in v.items()):
                raise ConfigError(f"`kubernetes.{key}` is a mapping of names to values, e.g. {example}")
        gns = k.get("gpu_node_selector") or {}
        if not isinstance(gns, dict) or not all(isinstance(v, dict) for v in gns.values()):
            raise ConfigError("`kubernetes.gpu_node_selector` maps a GPU to the node labels that find it, e.g. "
                              "{A100: {nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB}}")
        k["gpu_node_selector"] = {str(g).upper(): {str(a): str(b) for a, b in v.items()} for g, v in gns.items()}
        tol = k.get("tolerations") or []
        allowed = {"key", "operator", "value", "effect", "tolerationSeconds"}
        if not isinstance(tol, list) or not all(isinstance(t, dict) and t and set(t) <= allowed for t in tol):
            raise ConfigError("`kubernetes.tolerations` is a list of Kubernetes tolerations, e.g. [{key: nvidia.com/gpu, "
                              "operator: Exists, effect: NoSchedule}] (keys: key, operator, value, effect, "
                              "tolerationSeconds)")
        k["tolerations"] = tol
        vols, mounts = k.get("volumes") or [], k.get("volume_mounts") or []
        if not isinstance(vols, list) or not all(isinstance(v, dict) and isinstance(v.get("name"), str)
                                                 and v["name"] != "work" and len(v) > 1 for v in vols):
            raise ConfigError("`kubernetes.volumes` is a list of Kubernetes volumes, each with a name (not work) and a "
                              "source, e.g. [{name: data, persistentVolumeClaim: {claimName: imagenet, readOnly: true}}]")
        names = {v["name"] for v in vols}
        for m in mounts if isinstance(mounts, list) else [None]:
            path = m.get("mountPath") if isinstance(m, dict) else None
            if not (isinstance(path, str) and path.startswith("/") and m.get("name") in names) or \
                    path == WORK or path.startswith(WORK + "/"):
                raise ConfigError("`kubernetes.volume_mounts` is a list of mounts of kubernetes.volumes, outside /work, "
                                  "e.g. [{name: data, mountPath: /data, readOnly: true}]")
        k["volumes"], k["volume_mounts"] = vols, mounts
        for key in ("cpu", "memory_gb"):
            v = k.get(key)
            if v is not None and not C.number(v, positive=True):
                raise ConfigError(f"`kubernetes.{key}` is a positive number (the pod's request and limit), or leave it "
                                  "out")
        secrets = k.get("env_secrets") or {}
        if not isinstance(secrets, dict):
            raise ConfigError("`kubernetes.env_secrets` maps an environment variable to a Kubernetes Secret and key, "
                              "e.g. {HF_TOKEN: hf-token/token}")
        for var, ref in secrets.items():
            if not isinstance(var, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", var):
                raise ConfigError("`kubernetes.env_secrets` keys are environment variable names, e.g. HF_TOKEN")
            _secret_ref(f"kubernetes.env_secrets.{var}", ref)
        k["env_secrets"] = secrets
        table = price_table(k)
        for g in k["gpu_node_selector"]:
            if g not in table:
                raise ConfigError(f"`kubernetes.gpu_node_selector.{g}` is for a GPU kubernetes.usd_per_hour has no price "
                                  f"for; add {g}: <dollars per hour> to it")
        for name, plan in (cfg.get("plans") or {}).items():
            if plan.get("backend") == "kubernetes":
                gpu_rate(k, plan.get("gpu"), f"plans.{name}")
                for key in ("cpu", "memory_gb"):
                    v = plan.get(key)
                    if v is not None and not C.number(v, positive=True):
                        raise ConfigError(f"`plans.{name}.{key}` is a positive number, or leave it out")
        s = cfg.get("sessions") or {}
        if s.get("backend") == "kubernetes":
            gpu_rate(k, s.get("gpu"), "sessions")

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        return gpu_rate(cfg["kubernetes"], hardware.get("gpu"), "session" if "steps" not in hardware else "plan") / 60

    def prices(self, cfg: dict) -> list[str]:
        k = cfg["kubernetes"]
        try:
            table = price_table(k)
        except ConfigError as exc:
            return [f"  {exc}"]
        where = k.get("context") or "kubectl's current context"
        return [f"  ${rate:.2f}/h {'for a pod without a GPU' if g == 'cpu' else 'per ' + g} on {where} "
                "(testbench.yaml kubernetes.usd_per_hour)" for g, rate in sorted(table.items(), key=lambda kv: kv[1])]

    def plan_runner(self):
        return KubernetesRunner

    def session_class(self):
        return KubernetesSession

    def cancel_remote(self, state: dict) -> str:
        job = state.get("kube_job")
        if not job:
            return ""
        kube = Kubectl(state.get("kube_command") or ["kubectl"], state.get("kube_context"), state.get("kube_namespace"))
        try:
            kube.delete(job)
        except KubeError as exc:
            return f"could not delete the Job {job}: {exc}"
        return f"deleted the Job {job} and its pod ({kube.where})"

    def doctor(self):
        what = "kubectl (kubernetes plans and sessions)"
        k = _project_section()
        cmd = _command(k.get("command"))
        exe = shutil.which(cmd[0])
        if not exe:
            return [(what, False, "install kubectl (https://kubernetes.io/docs/tasks/tools/) and a kubeconfig for your "
                                  "cluster")]
        out = [(what, True, exe)]
        try:
            kube = pin({**k, "command": cmd})
        except KubeError as exc:
            return out + [("kubernetes context", False, str(exc))]
        ctx, ns = kube.context, kube.namespace
        try:
            p = kube("version", "-o", "json", "--request-timeout=10s", check=False, timeout=30)
            server = (json.loads(p.stdout or "{}").get("serverVersion") or {}).get("gitVersion")
        except (KubeError, ValueError):
            server = None
        if not server:
            return out + [("kubernetes cluster", False, f"context {ctx} does not answer - check `kubectl --context {ctx} "
                                                       "get pods` works")]
        out.append(("kubernetes cluster", True, f"context {ctx}, namespace {ns}, server {server}"))
        for verb, resource in (("create", "jobs"), ("create", "pods/exec")):
            try:
                p = kube("auth", "can-i", verb, resource, "--request-timeout=10s", check=False, timeout=30)
                ok = p.stdout.strip() == "yes"
                detail = "yes" if ok else (p.stdout.strip() or p.stderr.strip()[-200:] or "no")
            except KubeError as exc:
                ok, detail = False, str(exc)
            out.append((f"kubectl auth can-i {verb} {resource} -n {ns}", ok, detail))
        return out


def _project_section() -> dict:
    """The kubernetes section of the project here, if there is one, for `testbench doctor` (read, not validated)."""
    try:
        import yaml
        from ..config import find_root, CONFIG_NAME
        raw = yaml.safe_load((find_root() / CONFIG_NAME).read_text()) or {}
        k = raw.get("kubernetes") if isinstance(raw, dict) else None
        return k if isinstance(k, dict) else {}
    except Exception:
        return {}


backend = KubernetesBackend()


# ---- inside the pod ------------------------------------------------------------------------------------------

def _reap(proc: subprocess.Popen, limit: float) -> int:
    """Wait for the work to end, reaping every orphan meanwhile: in the pod this process is PID 1, which nothing
    else reaps for, and the watchdog would take a zombie for a process still running."""
    end = time.time() + limit
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return proc.returncode if proc.returncode is not None else 1
        if pid == proc.pid:
            return os.waitstatus_to_exitcode(status)
        if pid == 0:
            if time.time() > end:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                end = time.time() + 60
            time.sleep(0.25)


def inside(argv=None) -> None:
    """The pod's main process once the project is in (it replaces the bootstrap, so it is PID 1):

        python -m agent_testbench.backends.kubernetes_backend plan|session --max-minutes M [--idle-minutes I] -- COMMAND...

    It starts the watchdog first, then the work as its child, and reaps orphans while it waits. A signal (kubectl
    exec sends SIGINT to cancel; Kubernetes sends SIGTERM at the deadline) goes on to the work: a plan's executor
    writes a cancelled report, a session's launcher shuts its kernel down. When a plan's work ends, `done` is
    written and the pod waits, at most --linger-seconds, for `collected`, then ends; a session's pod ends with its
    kernel.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    cut = argv.index("--") if "--" in argv else len(argv)
    ap = argparse.ArgumentParser(prog="python -m agent_testbench.backends.kubernetes_backend")
    ap.add_argument("mode", choices=["plan", "session"])
    ap.add_argument("--max-minutes", type=float, required=True)
    ap.add_argument("--idle-minutes", type=float)
    ap.add_argument("--linger-seconds", type=float, default=LINGER_SECONDS)
    a, command = ap.parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        ap.error("give the command to run after --")
    from ..kernelkit import watchdog
    try:
        if not os.path.lexists("/content"):                 # Colab notebooks expect to live in /content
            os.symlink(PROJECT, "/content")
    except OSError:
        pass
    state_dir = KERNEL if a.mode == "session" else WATCH
    watchdog.spawn(state_dir, max_minutes=a.max_minutes, idle_minutes=a.idle_minutes)
    proc = subprocess.Popen(command, cwd=PROJECT if os.path.isdir(PROJECT) else None, start_new_session=True)
    if a.mode == "plan":
        watchdog.note_interrupt(state_dir, proc.pid)
    forward = signal.SIGINT if a.mode == "plan" else signal.SIGTERM

    def on_signal(signum, frame):
        try:
            os.kill(proc.pid, forward)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    code = _reap(proc, a.max_minutes * 60 + 90)            # the watchdog stops it at max_minutes; this backs it up
    if a.mode == "session":
        sys.exit(0)
    Path(DONE).write_text(f"{code}\n")
    end = time.time() + a.linger_seconds
    while time.time() < end and not os.path.exists(COLLECTED):
        time.sleep(0.5)


if __name__ == "__main__":
    inside()
