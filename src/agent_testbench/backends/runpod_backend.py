"""Run on RunPod: each plan (and each live session) is a pod of its own, made through RunPod's REST API on the GPU
the plan names (or on a CPU pod), and terminated when it ends.

    runpod:
      image: python:3.11-slim      # what the pod runs: Python 3.10 or newer (and pip, for setup: pip)
      setup: pip                   # pip: the pod installs testbench's basics and `pip:` when it starts; none: the image
      pip: [torch==2.5.1]          #   already has ipykernel, jupyter_client, nbformat, nbclient and pyyaml
      python: python3              # the image's Python
      cloud: SECURE                # SECURE or COMMUNITY
      disk_gb: 20                  # the pod's container disk
      cpu_flavor: cpu3c            # a pod without a GPU: its CPU flavor and vCPUs (a power of two)
      vcpus: 2
      data_centers: []             # where it may run, e.g. [US-KS-2]; empty: anywhere
      env: {}                      # plain environment variables for the pod
      gpu_types: {}                # GPUs beyond the built-in ones: {RTXPRO6000: "NVIDIA RTX PRO 6000 ..."}; each needs
                                   #   a price under pricing: runpod_usd_per_hour
      allow_without_remote_stop: false
      api_url: https://api.runpod.io/v2
      proxy_url: https://{pod_id}-{port}.proxy.runpod.net

The API key is RUNPOD_API_KEY, read from this machine's environment by name. It goes only to RunPod's own hosts
(https) or to this machine (http://127.0.0.1, for tests); it is never logged, written to state or sent to the pod.

How the project gets in: the pod's start command is a standard-library script (BOOTSTRAP) that serves a small,
token-protected HTTP agent on port 8000, which RunPod's HTTPS proxy reaches at https://<pod>-8000.proxy.runpod.net.
Everything after that - the project as a tar.gz, starting the executor, bringing the evidence back - is a command
sent to that agent. This needs nothing of the image but Python: no sshd, no public IP, no key pair, and the same
on Secure and Community Cloud. The token is an HMAC of RUNPOD_API_KEY and a nonce, so a session's later commands
can recompute it and nothing secret is kept in state.json.

The hard stop is the pod's own: the bootstrap is the pod's first process, and before anything else it holds the
plan's deadline (the priced worst case, counted from when the pod was created). At the deadline, when nothing was
started within the start-up allowance, or once the work has ended and its evidence was taken (or after a short
wait for that), it has RunPod terminate the pod, with the API key RunPod gives every pod for itself
(RUNPOD_API_KEY in the pod, scoped to that pod): REST DELETE, then GraphQL podTerminate, then runpodctl. If none
works it ends every process testbench started and exits. kernelkit's watchdog in the pod stops the work a minute
before the deadline, so the executor writes its report and the evidence can still be taken. A pod that has no way
to terminate itself is refused at start (and terminated from here) unless runpod.allow_without_remote_stop is on.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .. import __version__, config as C, prices
from ..config import ConfigError
from . import GPU_FORMAT, Backend, project_files, split_gpu
from .docker_backend import BASE_PIP, LIST, PKG, PROBE, _extract_run, _tail
from .kubernetes_backend import READ, WRITE
from .ssh_backend import SPAWN

# US dollars per hour per GPU, on-demand. RunPod's pricing page (runpod.io/pricing) could not be reached when these
# were written (2026-11-12): they are its Secure Cloud rates as last known, each raised about 25-50% and rounded up,
# so an estimate stays above what RunPod charges (Community Cloud is cheaper still). The margin also covers the
# container disk. A fetched catalog can raise them; `pricing: runpod_usd_per_hour:` sets your own.
PRICES_TAKEN = "2026-11-12, from RunPod's Secure Cloud rates as last known, raised (runpod.io/pricing not reachable)"
USD_PER_HOUR = {"RTX3090": 0.60, "RTX4090": 0.90, "RTX5090": 1.20, "A4000": 0.45, "A5000": 0.50, "A6000": 0.95,
                "A40": 0.60, "L4": 0.60, "L40": 1.25, "L40S": 1.20, "RTX6000-ADA": 1.05, "A100-PCIE": 2.10,
                "A100-SXM": 2.40, "H100-PCIE": 3.00, "H100-SXM": 3.80, "H100-NVL": 3.60, "H200": 5.00, "B200": 7.50,
                "MI300X": 3.50}
CPU_USD_PER_VCPU_HOUR = 0.08
# What each name is called in RunPod's API (its gpu type ids; not checked against a live account).
GPU_IDS = {"RTX3090": "NVIDIA GeForce RTX 3090", "RTX4090": "NVIDIA GeForce RTX 4090",
           "RTX5090": "NVIDIA GeForce RTX 5090", "A4000": "NVIDIA RTX A4000", "A5000": "NVIDIA RTX A5000",
           "A6000": "NVIDIA RTX A6000", "A40": "NVIDIA A40", "L4": "NVIDIA L4", "L40": "NVIDIA L40",
           "L40S": "NVIDIA L40S", "RTX6000-ADA": "NVIDIA RTX 6000 Ada Generation",
           "A100-PCIE": "NVIDIA A100 80GB PCIe", "A100-SXM": "NVIDIA A100-SXM4-80GB", "H100-PCIE": "NVIDIA H100 PCIe",
           "H100-SXM": "NVIDIA H100 80GB HBM3", "H100-NVL": "NVIDIA H100 NVL", "H200": "NVIDIA H200",
           "B200": "NVIDIA B200", "MI300X": "AMD Instinct MI300X OAM"}

KEY_ENV = "RUNPOD_API_KEY"
PORT = 8000                              # the pod's agent; RunPod's proxy serves it at https://<pod>-8000.proxy...
WORK = "/work"                           # in the pod: the project, this package, the run, the kernel
STOP_MARGIN = 60                         # seconds the watchdog stops the work before the pod's deadline
LINGER_SECONDS = 120                     # after a plan, the pod waits at most this long for its evidence to be taken
IDLE_SLACK = 10                          # the pod's watchdog waits this much longer than the supervisor here
JOB_WAIT = 25                            # seconds one request to the agent waits (RunPod's proxy ends one at 100 s)
RUNPOD_HOSTS = ("runpod.io", "runpod.ai", "runpod.net")
REMOTE_STOP = ("by the pod itself: at the worst case its first process has RunPod terminate it (with the key RunPod "
               "gives each pod), and its watchdog stops the work just before, even if testbench on this machine has "
               "stopped")
NO_REMOTE_STOP = ("only from this machine: the pod could not terminate itself (no pod key, no runpodctl), and "
                  "runpod.allow_without_remote_stop is on; its watchdog still stops the work at the limit, but the pod "
                  "bills until testbench here (or you, at runpod.io/console/pods) terminates it")

# The pod's first process, standard library only. It holds the deadline from the pod's first second, serves the
# agent (in a child process) and reaps orphans (it is PID 1), and ends the pod: at the deadline, when nothing was
# started within the start-up allowance, or once the work is over and its evidence taken (or after `linger`).
BOOTSTRAP = r'''
import base64, hmac, json, os, re, shutil, signal, subprocess, sys, threading, time, urllib.error, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
cfg = json.loads(sys.argv[1])
TOKEN = os.environ.pop("TESTBENCH_POD_TOKEN", "")
WORK = os.environ.get("TESTBENCH_POD_WORK") or "/work"
POD, KEY = os.environ.get("RUNPOD_POD_ID", ""), os.environ.get("RUNPOD_API_KEY", "")
POD = POD if re.fullmatch(r"[A-Za-z0-9_-]+", POD) else ""
try:
    RETRY = min(max(float(os.environ.get("TESTBENCH_POD_RETRY_SECONDS") or 20), 0.1), 60)
except ValueError:
    RETRY = 20.0
RETRY = RETRY if RETRY == RETRY else 20.0
os.makedirs(WORK, exist_ok=True)
BOOTED = time.time()
END = min(cfg["deadline"], BOOTED + cfg["minutes"] * 60)      # the deadline counts from when the pod was made
SETUP = {"state": "installing" if cfg["pip"] else "ready"}
JOBS, LOCK = {}, threading.Lock()

def log(text):
    print("[testbench] " + text, flush=True)
    try:
        with open(os.path.join(WORK, "pod.log"), "a") as f:
            f.write(time.strftime("%H:%M:%S ") + text + "\n")
    except OSError:
        pass

def ways():
    out = []
    if POD and KEY:
        out += [("RunPod's REST API", rest), ("RunPod's GraphQL API", graphql)]
    if POD and shutil.which("runpodctl"):
        out.append(("runpodctl", ctl))
    return out

def ask(method, url, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method,
                                 headers={"Authorization": "Bearer " + KEY, "Content-Type": "application/json",
                                          "User-Agent": "agent-testbench-pod"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "DELETE":
            return b""
        raise RuntimeError("HTTP %d %s" % (e.code, e.read()[:300].decode(errors="replace")))

def rest():
    ask("DELETE", cfg["api"] + "/pods/" + POD)

def graphql():
    out = json.loads(ask("POST", cfg["graphql"], {"query": 'mutation { podTerminate(input: {podId: "%s"}) }' % POD})
                     or b"{}")
    if out.get("errors"):
        raise RuntimeError(str(out["errors"])[:300])

def ctl():
    for argv in (["runpodctl", "pod", "delete", POD], ["runpodctl", "remove", "pod", POD]):
        if subprocess.run(argv, capture_output=True, timeout=60).returncode == 0:
            return
    raise RuntimeError("runpodctl could not terminate it")

def ours():
    """Processes testbench started in the pod: the work folder on their command line, or as their folder."""
    found, mark = [], WORK.encode()
    for d in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if d.isdigit() and int(d) != os.getpid():
            try:
                cmd = open("/proc/%s/cmdline" % d, "rb").read()
                cwd = os.readlink("/proc/%s/cwd" % d).encode()
            except OSError:
                continue
            if mark + b"/" in cmd or mark + b"\0" in cmd or cwd == mark or cwd.startswith(mark + b"/"):
                found.append(int(d))
    return found

def end_work():
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in ours():
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.time() + 3
        while time.time() < end and ours():
            time.sleep(0.2)

def terminate(why, server):
    log("ending this pod: " + why)
    for _ in range(3):
        asked = False
        for name, way in ways():
            try:
                way()
                log("asked " + name + " to terminate this pod")
                asked = True
                break
            except Exception as exc:
                log("%s could not terminate this pod: %s: %s" % (name, type(exc).__name__, exc))
        if asked:
            time.sleep(90)                        # RunPod ends the pod here; if it has not, end the work, ask again
        end_work()
        if not asked:
            time.sleep(RETRY)
    log("RunPod did not terminate this pod: it stops here, with nothing running (terminate it at "
        "runpod.io/console/pods)")
    try:
        os.kill(server, signal.SIGKILL)
    except OSError:
        pass
    sys.exit(3)

def install():
    r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *cfg["pip"]], capture_output=True, text=True)
    SETUP["state"] = "ready" if r.returncode == 0 else "failed: " + (r.stderr or r.stdout)[-1500:]
    log("pip: " + SETUP["state"][:200])

def work(j, spec):
    try:
        p = subprocess.Popen(spec["argv"], cwd=spec.get("cwd") or WORK, env={**os.environ, **(spec.get("env") or {})},
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
        try:
            data = base64.b64decode(spec.get("stdin") or "")
            out, err = p.communicate(data, timeout=float(spec.get("timeout") or 600))
            code = p.returncode
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
            code, err = 124, err + b"\n[testbench] timed out in the pod"
    except OSError as exc:
        out, err, code = b"", str(exc).encode(), 127
    j.update(code=code, out=base64.b64encode(out).decode(), err=err.decode(errors="replace")[-20000:], at=time.time())
    j["done"].set()

def job(spec, start):
    with LOCK:
        for k in [k for k, v in JOBS.items() if v.get("at", 1e18) < time.time() - 600]:
            JOBS.pop(k)
        j = JOBS.get(spec.get("job"))
        if j is None:
            if not start:
                return {"done": True, "code": 125, "stdout": "",
                        "stderr": "the pod's agent no longer knows this command"}
            j = JOBS[spec["job"]] = {"done": threading.Event()}
            threading.Thread(target=work, args=(j, spec), daemon=True).start()
    j["done"].wait(min(float(spec.get("wait") or 20), 50))
    if not j["done"].is_set():
        return {"done": False}
    return {"done": True, "code": j["code"], "stdout": j["out"], "stderr": j["err"]}

class Agent(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def allowed(self):
        given = self.headers.get("Authorization", "").encode()
        return bool(TOKEN) and hmac.compare_digest(given, ("Bearer " + TOKEN).encode())

    def do_GET(self):
        if not self.allowed():
            return self.reply(401, {"error": "not allowed"})
        if self.path != "/status":
            return self.reply(404, {"error": "no such path"})
        self.reply(200, {"agent": 1, "setup": SETUP["state"], "selfstop": [n for n, _ in ways()], "work": WORK,
                         "python": sys.executable, "end": END, "now": time.time()})

    def do_POST(self):
        if not self.allowed():
            return self.reply(401, {"error": "not allowed"})
        if self.path not in ("/run", "/wait"):
            return self.reply(404, {"error": "no such path"})
        spec = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.reply(200, job(spec, self.path == "/run"))

def serve():
    if cfg["pip"]:
        threading.Thread(target=install, daemon=True).start()
    port = int(os.environ.get("TESTBENCH_POD_PORT") or cfg["port"])
    srv = ThreadingHTTPServer((os.environ.get("TESTBENCH_POD_BIND") or "0.0.0.0", port), Agent)
    srv.daemon_threads = True
    log("agent on port %d; the pod ends itself in %.1f min at the latest" % (port, (END - time.time()) / 60))
    srv.serve_forever()

def fork():
    pid = os.fork()
    if pid == 0:
        try:
            serve()
        finally:
            os._exit(1)
    return pid

log("pod started: ways it can end itself: " + (", ".join(n for n, _ in ways()) or "none"))
server = fork()

def stopped(signum, frame):                                   # PID 1 ignores a signal it has no handler for
    try:
        os.kill(server, signal.SIGKILL)
    except OSError:
        pass
    sys.exit(0)

signal.signal(signal.SIGTERM, stopped)
while True:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if pid == 0:
            break
        if pid == server:
            server = fork()
    now = time.time()
    done, taken = os.path.join(WORK, "done"), os.path.join(WORK, "collected")
    if now >= END:
        terminate("reached its time limit (%.1f min, the worst case priced before it started)" % cfg["minutes"], server)
    if not os.path.exists(os.path.join(WORK, "started")) and now > BOOTED + cfg["wait"]:
        terminate("nothing was started in it within %.1f min" % (cfg["wait"] / 60), server)
    if os.path.exists(taken):
        terminate("its work is over and the evidence was taken", server)
    if os.path.exists(done) and now > os.path.getmtime(done) + cfg["linger"]:
        terminate("its work is over and nobody took the evidence within %d s" % cfg["linger"], server)
    time.sleep(0.5)
'''

# Small standard-library scripts the agent runs in the pod.
UNPACK = ("import sys, tarfile\n"
          "with tarfile.open(fileobj=sys.stdin.buffer, mode='r:gz') as t:\n"
          "    t.extractall(sys.argv[1], **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))\n")
TOUCH = "import sys\nfor p in sys.argv[1:]:\n    open(p, 'a').close()\n"
EXISTS = "import os, sys\nsys.exit(0 if os.path.exists(sys.argv[1]) else 1)\n"
INTERRUPT = "import os, signal, sys\nos.kill(int(sys.argv[1]), signal.SIGINT)\n"


class RunPodError(RuntimeError):
    pass


# ---- talking to RunPod -----------------------------------------------------------------------------------------

def check_url(where: str, url, placeholder: str | None = None) -> str:
    """A URL RUNPOD_API_KEY (or a pod's token) may go to: RunPod's own hosts over https, or this machine over http
    (tests). Anything else is refused, so no edit of testbench.yaml can send the key somewhere else."""
    parts = urlsplit(url) if isinstance(url, str) else None
    host = (parts.hostname or "").lower() if parts else ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    ok = parts is not None and not parts.username and not parts.password and (
        (parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in RUNPOD_HOSTS))
        or (parts.scheme == "http" and loopback))
    if not ok or (placeholder and placeholder not in url):
        need = f", with {placeholder} in it" if placeholder else ""
        raise ConfigError(f"`runpod.{where}` is {url!r}: RUNPOD_API_KEY only goes to RunPod itself, over https (a "
                          f"host under {', '.join(RUNPOD_HOSTS)}), or to this machine over http (127.0.0.1, for "
                          f"tests){need}")
    return url.rstrip("/")


def api_key(error: type[Exception] = RunPodError) -> str:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise error(f"{KEY_ENV} is not set here: create an API key at runpod.io (Settings > API Keys) and export it as "
                    f"{KEY_ENV}; testbench reads it by name and never writes it down")
    return key


def pod_token(key: str, nonce: str) -> str:
    """What the pod's agent takes from this machine: from the API key and a nonce, so anyone holding the key (and the
    nonce, from state.json) can talk to the pod, and nothing secret has to be kept."""
    return hmac.new(key.encode(), f"agent-testbench pod {nonce}".encode(), hashlib.sha256).hexdigest()


def _explain(code: int, text: str, what: str) -> str:
    try:
        data = json.loads(text)
        msg = data.get("error") or data.get("message") or data.get("detail") or text
    except (ValueError, AttributeError):
        msg = text
    msg = (str(msg).strip() or "no reason given")[:600]
    if code in (401, 403):
        return (f"RunPod refused to {what} (HTTP {code}: {msg}): check that {KEY_ENV} is a current API key with read "
                "and write access (runpod.io, Settings > API Keys)")
    return f"RunPod could not {what} (HTTP {code}): {msg}"


class Api:
    """RunPod's REST API with the person's key. Each request is logged (method, path, status, time, and RunPod's
    message on an error); the key, the request and the pod's settings never are."""

    def __init__(self, url: str, key: str, log: Path | None = None, error: type[Exception] = RunPodError):
        self.url, self.key, self.log, self.error = check_url("api_url", url), key, log, error

    def call(self, method: str, path: str, body=None, what: str = "answer", timeout: float = 60):
        req = urllib.request.Request(self.url + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json",
                                              "Content-Type": "application/json",
                                              "User-Agent": f"agent-testbench/{__version__}"})
        began, code, text = time.time(), 0, ""
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code, text = r.status, r.read().decode(errors="replace")
        except urllib.error.HTTPError as e:
            code, text = e.code, e.read().decode(errors="replace")
        except (urllib.error.URLError, OSError) as e:
            self._note(f"{method} {path}  ({time.time() - began:.1f} s, no answer: {getattr(e, 'reason', e)})")
            raise self.error(f"RunPod's API ({self.url}) did not answer (asked to {what}): {getattr(e, 'reason', e)}"
                             ) from None
        self._note(f"{method} {path}  ({time.time() - began:.1f} s, HTTP {code})"
                   + (f"\n  {_explain(code, text, what)}" if code >= 400 else ""))
        return code, text

    def _note(self, line: str) -> None:
        if self.log:
            with self.log.open("a") as f:
                f.write(line.replace(self.key, "[key]") + "\n")

    def _json(self, text: str, what: str):
        try:
            return json.loads(text or "{}")
        except ValueError:
            raise self.error(f"RunPod's answer when asked to {what} is not JSON: {text[:200]!r}") from None

    def create(self, body: dict) -> dict:
        code, text = self.call("POST", "/pods", body, "create the pod", timeout=120)
        if code >= 300:
            raise self.error(_explain(code, text, "create the pod"))
        pod = self._json(text, "create the pod")
        if not isinstance(pod, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(pod.get("id") or "")):
            raise self.error(f"RunPod made a pod but did not say its id: {text[:300]}")
        return pod

    def get(self, pod_id: str) -> dict | None:
        """The pod, None once it is gone."""
        code, text = self.call("GET", f"/pods/{pod_id}", what="say how the pod is")
        if code == 404:
            return None
        if code >= 300:
            raise self.error(_explain(code, text, "say how the pod is"))
        pod = self._json(text, "say how the pod is")
        return pod if isinstance(pod, dict) else {}

    def named(self, name: str) -> list[str]:
        """Pods with this name (a create whose answer was lost made one we do not know the id of)."""
        code, text = self.call("GET", "/pods", what="list the pods")
        if code >= 300:
            return []
        data = self._json(text, "list the pods")
        pods = data.get("pods", []) if isinstance(data, dict) else data
        return [p["id"] for p in pods if isinstance(p, dict) and p.get("name") == name and p.get("id")]

    def terminate(self, pod_id: str, patience: float = 90) -> None:
        """Terminate the pod (gone already is fine), and wait until RunPod says it is gone, asking again meanwhile."""
        last, end = "", time.time() + patience
        while True:
            try:
                code, text = self.call("DELETE", f"/pods/{pod_id}", what="terminate the pod")
                if code < 300 or code == 404:
                    for _ in range(10):
                        pod = self.get(pod_id)
                        if pod is None or str(pod.get("status", "")).upper() == "TERMINATED":
                            return
                        last = f"it is still {pod.get('status')}"
                        time.sleep(1)
                else:
                    last = _explain(code, text, "terminate the pod")
                    if code in (401, 403):               # asking again will not help
                        break
            except RuntimeError as exc:
                last = str(exc)
            if time.time() > end:
                break
            time.sleep(2)
        raise self.error(f"could not terminate pod {pod_id}: {last}. It ends itself at its time limit; to end it now, "
                         "terminate it at runpod.io/console/pods")


class Agent:
    """The pod's agent, through RunPod's proxy: each command runs there as a job, waited on in requests of JOB_WAIT
    seconds (the proxy ends a request at 100 s). A job carries its own id, so a request whose answer was lost is
    asked again without running the command twice. Logged: what ran (its first words), its exit code and time."""

    def __init__(self, url: str, token: str, log: Path | None = None, error: type[Exception] = RunPodError):
        self.url, self.token, self.log, self.error = url.rstrip("/"), token, log, error

    def _request(self, path: str, body=None, timeout: float = JOB_WAIT + 30) -> dict | None:
        """The agent's answer; None if it did not answer (not up yet, or the proxy has nothing there)."""
        req = urllib.request.Request(self.url + path, method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.token}",
                                              "Content-Type": "application/json",
                                              "User-Agent": f"agent-testbench/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise self.error("the pod's agent refused testbench's token: is RUNPOD_API_KEY the key the pod was "
                                 "made with?") from None
            return None
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def status(self) -> dict | None:
        return self._request("/status", timeout=30)

    def run(self, argv: list[str], *, stdin: bytes | None = None, timeout: float = 600, cwd: str | None = None,
            env: dict | None = None, check: bool = True, tries: int = 6) -> subprocess.CompletedProcess:
        spec = {"job": uuid.uuid4().hex, "argv": [str(a) for a in argv], "timeout": timeout, "cwd": cwd,
                "env": env or {}, "stdin": base64.b64encode(stdin).decode() if stdin else "", "wait": JOB_WAIT}
        began, misses, path, res = time.time(), 0, "/run", None
        while True:
            res = self._request(path, spec)
            if res is None:
                misses += 1
                if misses >= tries or time.time() > began + timeout + 180:
                    self._note(argv, began, None)
                    raise self.error(f"the pod's agent did not answer (`{_shown(argv)}`): the pod may have ended")
                time.sleep(min(2 * misses, 10))
                continue
            misses = 0
            if res.get("done"):
                break
            path = "/wait"                           # still running there: wait again (the job knows its own id)
            spec = {"job": spec["job"], "wait": JOB_WAIT}
        code, err = int(res.get("code", 1)), str(res.get("stderr") or "")
        out = base64.b64decode(res.get("stdout") or "")
        self._note(argv, began, code, err if code else "")
        if check and code != 0:
            said = (err or out.decode(errors="replace"))[-600:]
            raise self.error(f"in the pod, `{_shown(argv)}` exited {code}: {said}")
        return subprocess.CompletedProcess(argv, code, out, err)

    def _note(self, argv, began: float, code, err: str = "") -> None:
        if self.log:
            with self.log.open("a") as f:
                f.write(f"pod: {_shown(argv)}  ({time.time() - began:.1f} s, "
                        + (f"exit {code})" if code is not None else "no answer)")
                        + (f"\n  {err.strip()[-800:]}" if err.strip() else "") + "\n")


def _shown(argv) -> str:
    """A command as the log shows it: scripts cut to their first line, never what goes in on stdin."""
    return shlex.join(a if len(a) <= 100 else a[:60].split("\n")[0] + " ..." for a in map(str, argv))[:300]


# ---- what a pod is made of ---------------------------------------------------------------------------------------

def gpu_table(cfg: dict) -> dict:
    """{NAME: RunPod's id} for every GPU a plan can name: the built-in ones and runpod.gpu_types."""
    return {**GPU_IDS, **{str(k).upper(): str(v) for k, v in (cfg["runpod"].get("gpu_types") or {}).items()}}


def _rates(cfg: dict) -> tuple[dict, float]:
    """({GPU: dollars per hour}, dollars per vCPU-hour): built in, raised by a fetched catalog, then pricing:."""
    own = cfg.get("pricing") or {}
    cat = prices.cached().get("runpod") or {}
    gpu = {**prices.raised(USD_PER_HOUR, cat.get("usd_per_hour")), **prices.own_table(own, "runpod_usd_per_hour")}
    cpu = prices.own_price(own, "runpod_cpu_usd_per_vcpu_hour",
                           prices.raised_value(CPU_USD_PER_VCPU_HOUR, cat.get("cpu_usd_per_vcpu_hour")))
    return gpu, cpu


def gpu_rate(cfg: dict, gpu, where: str) -> float:
    """US dollars per hour for a pod with `gpu` (NAME or NAME:COUNT), or a CPU pod. Nothing unpriced starts."""
    rates, cpu = _rates(cfg)
    name, count = split_gpu(gpu)
    if not name:
        return cpu * float(cfg["runpod"]["vcpus"])
    known = gpu_table(cfg)
    if name not in known:
        raise ConfigError(f"`{where}.gpu` is {gpu!r}; runpod offers {', '.join(known)}{C.near(name, known)} (or name "
                          "another of RunPod's GPUs under runpod.gpu_types, with its price under pricing: "
                          "runpod_usd_per_hour)")
    if name not in rates:
        raise ConfigError(f"`{where}.gpu` is {gpu!r}, which has no price: add {name}: <dollars per hour per GPU> under "
                          "pricing: runpod_usd_per_hour, so the worst case is known before anything runs")
    return rates[name] * count


def plan_limit(plan: dict) -> float:
    """The plan's worst case in minutes, which is also its pod's deadline: its steps and the start-up allowance."""
    return sum(st["max_minutes"] for st in plan["steps"]) + backend.startup_minutes


def pod_body(cfg: dict, name: str, hw: dict, minutes: float, created: float, token: str, env: dict) -> dict:
    """The request that makes one pod. Its start command is the bootstrap, with the deadline in it; secrets (env) go
    by name as the pod's environment variables, and the agent's token too (the bootstrap takes it out at once)."""
    r = cfg["runpod"]
    gpu, count = split_gpu(hw.get("gpu"))
    boot = {"deadline": created + minutes * 60, "minutes": minutes, "wait": backend.startup_minutes * 60,
            "linger": LINGER_SECONDS, "port": PORT, "api": check_url("api_url", r["api_url"]),
            "graphql": graphql_url(r["api_url"]),
            "pip": sorted(set(BASE_PIP + list(r.get("pip") or []))) if r["setup"] == "pip" else []}
    plain = {"PYTHONUNBUFFERED": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_ROOT_USER_ACTION": "ignore",
             "PIP_BREAK_SYSTEM_PACKAGES": "1", **{n: os.environ[n] for n in ("TESTBENCH_WATCHDOG_SECONDS",)
                                                  if n in os.environ},
             **{str(a): str(b) for a, b in (r.get("env") or {}).items()}}
    body = {"name": name, "image": r["image"], "cloud": r["cloud"], "disk": math.ceil(float(r["disk_gb"])),
            "ports": [f"{PORT}/http"], "entrypoint": [r["python"], "-c", BOOTSTRAP], "cmd": [json.dumps(boot)],
            "env": {**plain, **env, "TESTBENCH_POD_TOKEN": token}}
    if gpu:
        body["gpu"] = {"id": gpu_table(cfg)[gpu], "count": count}
    else:
        body["cpu"] = {"id": r["cpu_flavor"], "vcpuCount": int(r["vcpus"])}
    if r.get("data_centers"):
        body["dataCenterIds"] = list(r["data_centers"])
    return body


def _poll() -> float:
    """Seconds between checks: 3, or what TESTBENCH_RUNPOD_POLL_SECONDS says (tests), kept within 0.01-15."""
    from ..kernelkit.watchdog import _tick
    return _tick(os.environ.get("TESTBENCH_RUNPOD_POLL_SECONDS") or "3")


def graphql_url(api_url: str) -> str:
    """RunPod's GraphQL endpoint next to its REST API: https://api.runpod.io/v2 -> https://api.runpod.io/graphql."""
    p = urlsplit(api_url)
    return f"{p.scheme}://{p.netloc}/graphql"


def pod_name(*parts: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", "-".join(("tb",) + parts))[:60].strip("-")


def secret_values(names: list[str], error: type[Exception] = RunPodError) -> dict:
    missing = [n for n in names if n not in os.environ]
    if missing:
        raise error(f"secrets not in this environment: {', '.join(missing)} - export them first (they reach the pod as "
                    "its environment variables, by name)")
    return {n: os.environ[n] for n in names}


class Pod:
    """One pod, from its creation to its end: what a run and a session share."""

    def __init__(self, cfg: dict, api: Api, log: Path | None, say=print, error: type[Exception] = RunPodError):
        self.cfg, self.api, self.log, self.say, self.error = cfg, api, log, say, error
        self.id = self.agent = self.work = self.python = None
        self.created = self.minutes = None
        self.selfstop: list[str] = []

    def create(self, name: str, hw: dict, minutes: float, token: str, env: dict) -> str:
        created = time.time()
        body = pod_body(self.cfg, name, hw, minutes, created, token, env)
        try:
            pod = self.api.create(body)
        except RuntimeError as exc:
            if "did not answer" in str(exc):          # it may exist all the same: find it by its name and end it
                try:
                    for pid in self.api.named(name):
                        self.api.terminate(pid)
                except RuntimeError:
                    pass                               # it ends itself: nothing will start in it
            raise
        self.id, self.created, self.minutes = pod["id"], created, minutes      # billed from here
        url = self.cfg["runpod"]["proxy_url"].replace("{pod_id}", self.id).replace("{port}", str(PORT))
        self.agent = Agent(check_url("proxy_url", url), token, self.log, self.error)
        return self.id

    def wait(self) -> dict:
        """The agent's status once the pod runs and is set up; a plain error if it cannot within the start-up
        allowance (the pod is then terminated by whoever catches it)."""
        allowance = backend.startup_minutes * 60
        poll = _poll()
        last = None
        while True:
            pod = self.api.get(self.id)
            if pod is None:
                raise self.error(f"pod {self.id} is gone: RunPod removed it before it started (runpod.io/console/pods "
                                 "may say why)")
            status = str(pod.get("status") or pod.get("desiredStatus") or "").upper()
            if status in ("ERROR", "EXITED", "TERMINATED"):
                raise self.error(f"pod {self.id} is {status} before anything ran in it: RunPod could not start it "
                                 f"({pod.get('statusMessage') or pod.get('message') or 'no reason given'}); check "
                                 "runpod.image and runpod.python")
            if status != last:
                self.say(f"[testbench] pod {self.id}: {status.lower() or 'made'}", flush=True)
                last = status
            st = self.agent.status() if status == "RUNNING" else None
            if st:
                if str(st.get("setup", "")).startswith("failed"):
                    raise self.error(f"the pod could not install testbench's basics ({st['setup'][:800]}); it needs a "
                                     "package index, or an image that has them, with runpod.setup: none")
                if st.get("setup") == "ready":
                    self.work, self.python = st.get("work") or WORK, st.get("python") or self.cfg["runpod"]["python"]
                    self.selfstop = list(st.get("selfstop") or [])
                    return st
            if time.time() - self.created > allowance:
                raise self.error(f"pod {self.id} did not start within {allowance / 60:g} min (last seen "
                                 f"{status or 'made'}{', installing' if st else ''}): RunPod may have no machine of "
                                 "that kind free now; try another GPU, runpod.cloud or runpod.data_centers")
            time.sleep(poll)

    def check_selfstop(self) -> str:
        """What stops the pod on its own side, in words; refused (an error) when nothing can, unless allowed."""
        if self.selfstop:
            return REMOTE_STOP
        if self.cfg["runpod"].get("allow_without_remote_stop"):
            self.say("[testbench] warning: this pod cannot terminate itself; it bills until testbench here terminates "
                     "it (runpod.allow_without_remote_stop is on)", flush=True)
            return NO_REMOTE_STOP
        raise self.error("the pod cannot terminate itself: it has no RUNPOD_POD_ID and RUNPOD_API_KEY of its own "
                         "(RunPod gives them to every pod it runs) and no runpodctl, so nothing would end it if this "
                         "machine slept. It is terminated now. Use an image RunPod starts as usual, or set "
                         "runpod.allow_without_remote_stop: true to make testbench on this machine its only stop")

    def p(self, rel: str) -> str:
        return f"{self.work}/{rel}"

    def upload(self, root: Path | None, exclude: list[str], pkg: bool = True, files: dict | None = None) -> None:
        """The project (and this package) as one tar.gz, unpacked at the work folder; files by their path in it."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            if root is not None:
                for f in project_files(root, exclude):
                    t.add(f, arcname="project/" + f.relative_to(root).as_posix(), recursive=False)
            if pkg:
                t.add(PKG, arcname="pkg/agent_testbench", filter=lambda m: None if "__pycache__" in m.name else m)
            for rel, data in (files or {}).items():
                info = tarfile.TarInfo(rel)
                info.size, info.mode, info.mtime = len(data), 0o644, time.time()
                t.addfile(info, io.BytesIO(data))
        self.agent.run([self.python, "-c", UNPACK, self.work], stdin=buf.getvalue(), timeout=1800)

    def spawn(self, argv: list[str], log: str, env: dict | None = None) -> int:
        req = json.dumps({"argv": argv, "log": log, "cwd": self.p("project"), "env": env or {}}).encode()
        out = self.agent.run([self.python, "-c", SPAWN], stdin=req, timeout=120).stdout.decode(errors="replace")
        pid = next((ln.split()[1] for ln in out.splitlines() if ln.startswith("TESTBENCH_PID")), None)
        if not pid:
            raise self.error(f"could not start {argv[:4]} in the pod: {out[-400:]}")
        return int(pid)

    def inside(self, mode: str, minutes: float, command: list[str], idle: float | None = None,
               env: dict | None = None) -> int:
        """Start the work under the watchdog (this module's inside(), in the pod)."""
        argv = [self.python, "-m", "agent_testbench.backends.runpod_backend", mode, "--work", self.work,
                "--max-minutes", f"{minutes:.3f}", *(["--idle-minutes", f"{idle:g}"] if idle else []), "--", *command]
        return self.spawn(argv, self.p("inside.log"), {"PYTHONPATH": self.p("pkg"), "TESTBENCH_CACHE": self.p("cache"),
                                                       **(env or {})})

    def minutes_left(self) -> float:
        """What the work may take: up to STOP_MARGIN before the pod's deadline."""
        left = (self.created + self.minutes * 60 - STOP_MARGIN - time.time()) / 60
        if left <= 0:
            raise self.error(f"the pod started too late: nothing can run before its deadline ({self.minutes:g} min "
                             "after it was made)")
        return left

    def put(self, path: str, data: bytes, mode: str = "644") -> None:
        self.agent.run([self.python, "-c", WRITE, path, mode], stdin=data, timeout=1800)

    def get(self, path: str) -> bytes:
        p = self.agent.run([self.python, "-c", READ, path], timeout=1800, check=False)
        if p.returncode != 0:
            raise self.error(p.stderr.strip()[-500:] or f"could not read {path} in the pod")
        return p.stdout

    def mirror(self, dest: Path, full: bool) -> bool | None:
        """Bring the run folder back: progress and logs while it runs, everything at the end. True once the work has
        ended; None if the pod could not be asked."""
        try:
            p = self.agent.run([self.python, "-c", PROBE, "full" if full else "light", self.work], timeout=1800,
                               check=False, tries=6 if full else 2)
        except RuntimeError:
            return None
        if p.returncode != 0:
            return None
        try:
            return _extract_run(io.BytesIO(p.stdout), dest)
        except Exception:
            return None

    def end(self) -> None:
        """Terminate the pod (it is told first that its evidence was taken, so it ends itself too)."""
        if self.agent and self.work:
            try:
                self.agent.run([self.python, "-c", TOUCH, self.p("collected")], timeout=60, check=False)
            except RuntimeError:
                pass
        if self.id:
            self.api.terminate(self.id)

    def cost(self, cfg: dict, hw: dict) -> float:
        """From when the pod was made, at most its deadline (it ends itself there)."""
        if not self.created:
            return 0.0
        minutes = min((time.time() - self.created) / 60, self.minutes)
        return backend.rate_per_minute(cfg, hw) * minutes


class RunPodRunner:
    """A plan in a pod of its own: the executor runs there under the watchdog; progress comes back every few seconds
    and everything at the end, then the pod is terminated."""

    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta
        self.pod: Pod | None = None
        self.started = self.pid = None

    def run(self):
        from .. import report as R, runs
        r = self.cfg["runpod"]
        self.started = time.time()
        key = api_key()
        env = secret_values(sorted({n for st in self.plan["steps"] for n in st["secrets"]}))
        api = Api(r["api_url"], key, self.run_dir / "runpod.log")
        limit = plan_limit(self.plan)
        nonce = secrets.token_hex(8)
        self.pod = pod = Pod(self.cfg, api, self.run_dir / "runpod.log")
        name = pod_name(self.cfg["project"], self.run_dir.name)
        runs.write_state(self.run_dir, runpod_api=api.url, runpod_name=name, remote_stop=REMOTE_STOP)
        pod.create(name, self.plan, limit, pod_token(key, nonce), env)       # from here on, cancel terminates it
        runs.write_state(self.run_dir, runpod_pod=pod.id)
        print(f"[testbench] pod {pod.id} made ({self.plan.get('gpu') or 'CPU'}, {r['cloud']} cloud); hard stop after "
              f"{limit:g} min {REMOTE_STOP}", flush=True)
        pod.wait()
        stop = pod.check_selfstop()
        if stop != REMOTE_STOP:
            runs.write_state(self.run_dir, remote_stop=stop)
        pod.upload(self.root, self.cfg["exclude"], files={"plan.json": json.dumps(self.plan).encode()})
        left = pod.minutes_left()
        py, w = pod.python, pod.work
        self.pid = pod.inside("plan", left, [py, "-m", "agent_testbench.executor", "--plan", f"{w}/plan.json",
                                             "--project", f"{w}/project", "--run-dir", f"{w}/run", "--workdir",
                                             f"{w}/project", "--meta", json.dumps(self.meta)])
        print(f"[testbench] running the plan in pod {pod.id} from {r['image']} (watchdog at {left:.1f} min)",
              flush=True)
        give_up = pod.created + limit * 60 + LINGER_SECONDS + 300     # the pod ends itself long before this
        poll = _poll()
        misses = 0
        while time.time() < give_up:
            done = pod.mirror(self.run_dir, full=False)
            if done:
                break
            if done is None:
                misses += 1
                try:
                    gone = misses >= 3 and str((api.get(pod.id) or {}).get("status", "TERMINATED")).upper() in (
                        "TERMINATED", "EXITED", "ERROR")
                except RuntimeError:
                    gone = False                     # the API did not answer this time; the pod's deadline holds
                if gone:
                    break
            else:
                misses = 0
            time.sleep(poll)
        pod.mirror(self.run_dir, full=True)
        self._close()
        rep = R.load(self.run_dir)
        stopped = self.run_dir / "watchdog" / "watchdog_stopped"
        if rep and rep.get("status") == "cancelled" and stopped.exists():
            rep["stopped_because"] = f"the pod's watchdog: {stopped.read_text().strip()}"
        if rep and rep.get("status") == "running":
            rep.update(status="error", finished=time.time(), stopped_because="the executor in the pod ended before the "
                       "plan finished (the pod ended, or ran out of memory); see pod.log in the run")
        if not rep:
            rep = {**self.meta, "steps": [], "started": self.started, "status": "error",
                   "stopped_because": f"the plan never started in pod {pod.id}",
                   "launch_error": "The pod could not start the plan. Its log:\n\n```\n"
                                   f"{_tail(self.run_dir / 'pod.log')}\n```\n\nRunPod and pod commands:\n\n```\n"
                                   f"{_tail(self.run_dir / 'runpod.log')}\n```\n\nWith runpod.setup: none the "
                                   f"image needs {r['python']} with ipykernel, jupyter_client, nbformat, nbclient "
                                   "and pyyaml."}
        return rep, self.cost(rep)

    def _close(self) -> None:
        """Keep the pod's own log, then terminate it."""
        pod = self.pod
        if pod and pod.agent and pod.work:
            logs = []
            for name in ("pod.log", "inside.log"):
                try:
                    logs.append(pod.get(pod.p(name)))
                except RuntimeError:
                    pass
            if logs:
                (self.run_dir / "pod.log").write_bytes(b"".join(logs))
        if pod and pod.id:
            try:
                pod.end()
            except RuntimeError as exc:
                print(f"[testbench] {exc}", flush=True)

    def cancel(self) -> str:
        pod = self.pod
        if not (pod and pod.id):
            return ""
        if self.pid and pod.agent:
            try:                                     # the executor writes its report and ends
                pod.agent.run([pod.python, "-c", INTERRUPT, str(self.pid)], timeout=60, check=False)
                end = time.time() + 60
                while time.time() < end and pod.mirror(self.run_dir, full=False) is False:
                    time.sleep(0.5)
                pod.mirror(self.run_dir, full=True)
            except RuntimeError:
                pass
        self._close()
        return f"stopped the plan and terminated pod {pod.id}"

    def cost(self, rep: dict) -> float:
        return self.pod.cost(self.cfg, self.plan) if self.pod else 0.0


class RunPodSession:
    """A kernel in a pod of its own, in a copy of the project; the same launcher and client as everywhere else. The
    pod ends with its kernel (the watchdog's idle and time limits), and never past its deadline."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        from ..session_backends import SessionError
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        self.error = SessionError
        h = state.get("handles") or {}
        self.pod_id, self.work, self.python = h.get("pod"), h.get("work"), h.get("python")
        self._agent = None

    def _api(self) -> Api:
        h = self.state.get("handles") or {}
        try:
            return Api(h.get("api") or self.cfg["runpod"]["api_url"], api_key(self.error), self.sdir / "runpod.log",
                       self.error)
        except ConfigError as exc:
            raise self.error(str(exc)) from None

    def agent(self) -> Agent:
        if self._agent is None:
            h = self.state.get("handles") or {}
            try:
                url = check_url("proxy_url", h["agent"])
            except (ConfigError, KeyError):
                raise self.error("this session has no pod to talk to (its state.json names none)") from None
            self._agent = Agent(url, pod_token(api_key(self.error), h["nonce"]), self.sdir / "runpod.log", self.error)
        return self._agent

    def start(self) -> dict:
        s = self.state
        try:
            backend.validate(self.cfg)
            gpu_rate(self.cfg, s.get("gpu"), "sessions")
            key = api_key(self.error)
            env = secret_values(list(s.get("secrets") or []), self.error)
            api = Api(self.cfg["runpod"]["api_url"], key, self.sdir / "runpod.log", self.error)
        except ConfigError as exc:
            raise self.error(str(exc)) from None
        nonce = secrets.token_hex(8)
        pod = Pod(self.cfg, api, self.sdir / "runpod.log", say=lambda *a, **kw: None, error=self.error)
        handles = {"api": api.url, "nonce": nonce, "remote_stop": REMOTE_STOP}
        s["handles"] = handles
        name = pod_name(self.cfg["project"], "session", s["name"], nonce[:6])
        minutes = s["max_minutes"] + backend.startup_minutes
        self.pod_id = handles["pod"] = pod.create(name, {"gpu": s.get("gpu")}, minutes, pod_token(key, nonce),
                                                  {**env, "TESTBENCH_SESSION": s["name"]})
        handles["agent"] = pod.agent.url
        self._agent = pod.agent
        pod.wait()
        handles["remote_stop"] = pod.check_selfstop()
        self.work, self.python = handles["work"], handles["python"] = pod.work, pod.python
        handles["workdir"] = pod.p("project")
        pod.upload(self.root, self.cfg["exclude"])
        pod.inside("session", pod.minutes_left(), [pod.python, "-m", "agent_testbench.kernelkit.launcher", "--workdir",
                                                   pod.p("project"), "--state-dir", pod.p("kernel"), "--shim"],
                   idle=s["idle_minutes"] + IDLE_SLACK, env={"TESTBENCH_SESSION": s["name"]})
        end = time.time() + 180
        while time.time() < end:
            if self._ready():
                return handles
            time.sleep(1)
        log = self.agent().run([self.python, "-c", READ, pod.p("inside.log")], check=False).stdout
        raise self.error(f"the kernel did not start in pod {pod.id}: {log.decode(errors='replace')[-1500:]}")

    def _ready(self) -> bool:
        p = self.agent().run([self.python, "-c", EXISTS, f"{self.work}/kernel/ready"], timeout=60, check=False)
        return p.returncode == 0

    def alive(self) -> bool:
        if not self.pod_id:
            return False
        try:
            return bool(self.agent().status()) and self._ready()
        except RuntimeError:
            return False

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        from ..session_backends import _parse
        a, w = self.agent(), self.work
        req = f"{w}/out/{prefix}.json"
        a.run([self.python, "-c", WRITE, req, "600"], stdin=json.dumps({"code": code, "timeout": timeout,
                                                                         "prefix": prefix}).encode(), timeout=120)
        p = a.run([self.python, "-m", "agent_testbench.kernelkit.client", "--connection", f"{w}/kernel/kernel.json",
                   "--request", req, "--out-dir", f"{w}/out/{prefix}"], cwd=f"{w}/project",
                  env={"PYTHONPATH": f"{w}/pkg"}, timeout=timeout + 180, check=False)
        res = _parse(p.stdout.decode(errors="replace"))
        local = []
        for path in res.get("images", []):
            dst = Path(out_dir) / Path(path).name
            dst.write_bytes(self._get(path))
            local.append(str(dst))
        res["images"] = local
        return res

    def _get(self, path: str) -> bytes:
        p = self.agent().run([self.python, "-c", READ, path], timeout=1800, check=False)
        if p.returncode != 0:
            raise self.error(p.stderr.strip()[-500:] or f"could not read {path} in the pod")
        return p.stdout

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.work}/project/{path}"

    def ls(self, path: str) -> list[str]:
        p = self.agent().run([self.python, "-c", LIST, self._abs(path or ".")], timeout=60, check=False)
        if p.returncode != 0:
            raise self.error(p.stderr.strip()[-500:] or f"could not list {path}")
        return [ln for ln in p.stdout.decode(errors="replace").splitlines() if ln.strip()]

    def get(self, remote: str, local: Path) -> None:
        Path(local).write_bytes(self._get(self._abs(remote)))

    def put(self, local: Path, remote: str) -> None:
        self.agent().run([self.python, "-c", WRITE, self._abs(remote), "644"], stdin=Path(local).read_bytes(),
                         timeout=1800)

    def _busy(self, script: str, timeout: float) -> subprocess.CompletedProcess:
        """A shell command in the project's copy, counted as activity by the pod's watchdog."""
        k = f"{self.work}/kernel"
        line = (f"echo {time.time() + timeout + 120:.0f} > {k}/busy; cd {self.work}/project && {{ {script}\n}} 2>&1; "
                f"rc=$?; rm -f {k}/busy; : > {k}/last_used; exit $rc")
        return self.agent().run(["sh", "-c", line], timeout=timeout + 60, check=False)

    def install(self, packages: list[str]) -> str:
        p = self._busy(f"{shlex.quote(self.python)} -m pip install {' '.join(shlex.quote(x) for x in packages)}", 1800)
        said = p.stdout.decode(errors="replace")[-3000:]
        return said + ("" if p.returncode == 0 else f"\n(pip exited {p.returncode})")

    def sync(self) -> str:
        pod = Pod(self.cfg, None, None, error=self.error)
        pod.agent, pod.work, pod.python = self.agent(), self.work, self.python
        pod.upload(self.root, self.cfg["exclude"], pkg=False)
        self.agent().run([self.python, "-c", TOUCH, f"{self.work}/kernel/last_used"], timeout=60, check=False)
        return f"project files copied to {self.work}/project in pod {self.pod_id} (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        return self._busy(command, timeout).stdout.decode(errors="replace")[-20000:]

    def stop(self) -> None:
        if not self.pod_id:
            return
        try:
            self._api().terminate(self.pod_id)
        except RuntimeError:
            pass


class RunPodBackend(Backend):
    name = "runpod"
    startup_minutes = 10.0            # finding a machine, pulling the image, installing the basics, copying the project
    hard_stop = ("the pod terminates itself through RunPod's API (with the key RunPod gives each pod) at the priced "
                 "worst case or once its work ends; kernelkit's watchdog in the pod stops the work just before")
    config_defaults = {"runpod": {
        "image": "python:3.11-slim", "setup": "pip", "pip": [], "python": "python3", "cloud": "SECURE", "disk_gb": 20,
        "cpu_flavor": "cpu3c", "vcpus": 2, "data_centers": [], "env": {}, "gpu_types": {},
        "allow_without_remote_stop": False, "api_url": "https://api.runpod.io/v2",
        "proxy_url": "https://{pod_id}-{port}.proxy.runpod.net"}}

    @property
    def gpus(self) -> tuple[str, ...]:
        return tuple(GPU_IDS)

    def check_hardware(self, where: str, gpu) -> None:
        """The GPUs on offer are the built-in ones and runpod.gpu_types; validate and rate_per_minute check them."""
        if gpu and not GPU_FORMAT.fullmatch(str(gpu)):
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write a GPU name, or NAME:COUNT for several (e.g. "
                              "H100-SXM:2), with no spaces")

    def validate(self, cfg: dict) -> None:
        r = cfg["runpod"]

        def text(key: str, example: str, pattern: str = r".*\S.*") -> None:
            if not isinstance(r.get(key), str) or not re.fullmatch(pattern, r[key]):
                raise ConfigError(f"`runpod.{key}` {example}")

        text("image", "is the image the pod runs, e.g. python:3.11-slim or runpod/pytorch:<tag> (Python 3.10 or newer)")
        text("python", "is the image's Python command, e.g. python3")
        text("cpu_flavor", "is a RunPod CPU flavor for a pod without a GPU, e.g. cpu3c", r"[A-Za-z0-9_-]+")
        if r.get("setup") not in ("pip", "none"):
            raise ConfigError("`runpod.setup` is pip (the pod installs testbench's basics and runpod.pip when it "
                              "starts) or none (runpod.image already has ipykernel, jupyter_client, nbformat, nbclient "
                              "and pyyaml)")
        pip = r.get("pip") or []
        if not isinstance(pip, list) or not all(isinstance(x, str) for x in pip):
            raise ConfigError("`runpod.pip` is a list of packages the pod installs (pin them): [torch==2.5.1]")
        if pip and r["setup"] == "none":
            raise ConfigError("`runpod.pip` needs runpod.setup: pip (with setup: none, the image is used as it is)")
        r["pip"] = pip
        if r.get("cloud") not in ("SECURE", "COMMUNITY"):
            raise ConfigError("`runpod.cloud` is SECURE (RunPod's data centers) or COMMUNITY (cheaper hosts)")
        if not C.number(r.get("disk_gb"), positive=True):
            raise ConfigError("`runpod.disk_gb` is the pod's container disk in GB, e.g. 20")
        v = r.get("vcpus")
        if not (isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 512 and v & (v - 1) == 0):
            raise ConfigError(f"`runpod.vcpus` is {v!r}; a pod without a GPU gets this many vCPUs, a power of two "
                              "(2, 4, 8, ...)")
        dcs = r.get("data_centers") or []
        if not isinstance(dcs, list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z0-9-]+", x) for x in dcs):
            raise ConfigError("`runpod.data_centers` is a list of RunPod data center ids, e.g. [US-KS-2], or empty for "
                              "anywhere")
        r["data_centers"] = dcs
        env = r.get("env") or {}
        if not isinstance(env, dict) or not all(
                isinstance(a, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", a) and not a.startswith(("RUNPOD_",
                                                                                                       "TESTBENCH_POD"))
                and isinstance(b, (str, int, float)) and not isinstance(b, bool) for a, b in env.items()):
            raise ConfigError("`runpod.env` maps environment variable names to plain values, e.g. {HF_HOME: "
                              "/work/cache/hf} (not RUNPOD_* or TESTBENCH_POD*, and never a secret: name those in a "
                              "step's secrets:)")
        r["env"] = env
        types = r.get("gpu_types") or {}
        if not isinstance(types, dict) or not all(isinstance(a, str) and GPU_FORMAT.fullmatch(a) and ":" not in a
                                                  and isinstance(b, str) and b.strip() for a, b in types.items()):
            raise ConfigError("`runpod.gpu_types` names more of RunPod's GPUs: {SHORTNAME: \"RunPod's GPU id\"}, "
                              "e.g. {RTXPRO6000: \"NVIDIA RTX PRO 6000 Blackwell Server Edition\"}, each priced under "
                              "pricing: runpod_usd_per_hour")
        r["gpu_types"] = types
        if not isinstance(r.get("allow_without_remote_stop"), bool):
            raise ConfigError("`runpod.allow_without_remote_stop` is true or false (default false: a pod that cannot "
                              "terminate itself is refused)")
        r["api_url"] = check_url("api_url", r.get("api_url"))
        check_url("proxy_url", r.get("proxy_url"), "{pod_id}")
        _rates(cfg)                                  # pricing: overrides are numbers where one is meant
        for name in types:
            gpu_rate(cfg, name, f"runpod.gpu_types.{name}")
        for name, plan in (cfg.get("plans") or {}).items():
            if plan.get("backend") == "runpod":
                gpu_rate(cfg, plan.get("gpu"), f"plans.{name}")
        s = cfg.get("sessions") or {}
        if s.get("backend") == "runpod":
            gpu_rate(cfg, s.get("gpu"), "sessions")

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        return gpu_rate(cfg, hardware.get("gpu"), "session" if "steps" not in hardware else "plan") / 60

    def prices(self, cfg: dict) -> list[str]:
        try:
            rates, cpu = _rates(cfg)
        except ConfigError as exc:
            return [f"  {exc}"]
        fetched = ((prices.cached().get("runpod") or {}).get("usd_per_hour"))
        own = (cfg.get("pricing") or {}).get("runpod_usd_per_hour")
        known = gpu_table(cfg)
        lines = [f"  {g:<12} ${v:.2f}/h per GPU  ({prices.source(g, USD_PER_HOUR, fetched, own)})"
                 for g, v in sorted(rates.items(), key=lambda kv: kv[1]) if g in known]
        return lines + [f"  CPU pod      ${cpu:.2f}/h per vCPU; runpod.vcpus {cfg['runpod']['vcpus']} -> "
                        f"${cpu * float(cfg['runpod']['vcpus']):.2f}/h",
                        f"  (built-in prices: {PRICES_TAKEN})"]

    def plan_runner(self):
        return RunPodRunner

    def session_class(self):
        return RunPodSession

    def cancel_remote(self, state: dict) -> str:
        pod = state.get("runpod_pod")
        if not pod:
            return ""
        try:
            Api(state.get("runpod_api") or self.config_defaults["runpod"]["api_url"], api_key()).terminate(pod)
        except (RuntimeError, ConfigError) as exc:
            return f"could not terminate pod {pod}: {exc}"
        return f"terminated pod {pod}"

    def doctor(self):
        ok = bool(os.environ.get(KEY_ENV, "").strip())
        return [(f"{KEY_ENV} (runpod plans and sessions)", ok,
                 "set (not shown)" if ok else "create an API key at runpod.io (Settings > API Keys) and export it as "
                                              f"{KEY_ENV}")]


backend = RunPodBackend()


# ---- inside the pod ------------------------------------------------------------------------------------------

def inside(argv=None) -> None:
    """The work in the pod, started by the agent once the project is in:

        python -m agent_testbench.backends.runpod_backend plan|session --work W --max-minutes M
            [--idle-minutes I] -- COMMAND...

    It starts the watchdog first, then the work as its child, and marks the work folder: `started` (the pod no longer
    ends itself for want of work), then `done` when the work has ended - a plan's pod then waits a little for its
    evidence to be taken; a session's (`collected` too) ends at once. An interrupt (cancel) goes on to the work: a
    plan's executor writes a cancelled report, a session's launcher shuts its kernel down."""
    argv = list(sys.argv[1:] if argv is None else argv)
    cut = argv.index("--") if "--" in argv else len(argv)
    ap = argparse.ArgumentParser(prog="python -m agent_testbench.backends.runpod_backend")
    ap.add_argument("mode", choices=["plan", "session"])
    ap.add_argument("--work", required=True)
    ap.add_argument("--max-minutes", type=float, required=True)
    ap.add_argument("--idle-minutes", type=float)
    a, command = ap.parse_args(argv[:cut]), argv[cut + 1:]
    if not command:
        ap.error("give the command to run after --")
    from ..kernelkit import watchdog
    work = Path(a.work)
    (work / "started").touch()
    try:
        if work == Path(WORK) and not os.path.lexists("/content"):    # Colab notebooks expect to live in /content
            os.symlink(work / "project", "/content")
    except OSError:
        pass
    state_dir = work / ("kernel" if a.mode == "session" else "run/watchdog")
    watchdog.spawn(state_dir, max_minutes=a.max_minutes, idle_minutes=a.idle_minutes)
    proc = subprocess.Popen(command, cwd=work / "project", start_new_session=True)
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
    try:
        code = proc.wait(timeout=a.max_minutes * 60 + 90)     # the watchdog stops it at max_minutes; this backs it up
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        code = proc.wait()
    if a.mode == "session":
        (work / "collected").touch()
    (work / "done").write_text(f"{code}\n")


if __name__ == "__main__":
    inside()
