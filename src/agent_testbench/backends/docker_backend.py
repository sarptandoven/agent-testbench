"""Run in a Docker container on this machine: a fresh container per run, optionally with no network at all, the
project as a copy, and (with NVIDIA's Container Toolkit) the machine's GPUs.

    docker:
      image: python:3.11-slim     # what containers start from (Python 3.10 or newer, with pip for setup: build)
      setup: build                # build: an image from `image` with testbench's basics and `pip:`, built once per
      pip: [torch==2.5.1]         #   config (tagged by a hash); none: use `image` as it is (it must already have
                                  #   ipykernel, jupyter_client, nbformat, nbclient and pyyaml)
      network: bridge             # bridge | none (no network: a sandbox) | host | a network from `docker network create`
      gpus: null                  # what `docker run --gpus` takes: all, 2, "device=0,1"
      cpu: null                   # --cpus
      memory_gb: null             # --memory
      usd_per_hour: 0.0           # what the machine costs while it works (0: your own); prices the worst case
      python: python3             # the image's Python
      run_args: []                # more `docker create` arguments, e.g. [-v, /data:/data:ro]
      build_args: []              # more `docker build` arguments, e.g. [--network, host]
      ca_bundle: null             # a CA bundle (PEM) for pip behind a TLS-intercepting proxy: used to build the image
                                  #   and set as PIP_CERT in containers
      command: [docker]           # the container CLI (podman works too)

Nothing from this machine is mounted: the project and this package are copied into each container with `docker cp`
and the evidence is copied back the same way, so the container sees a copy of the project and none of this machine's
folders, and what comes back is owned by you (not by the container's root). Sealed data (testbench.yaml's data: with
holdout_from) is cut on this machine before it is copied, so the sealed rows never enter a container but the holdout
plan's (dataseal.py). The hard stop is inside the container:
it starts kernelkit's watchdog before the work, and the watchdog ends the work at the time limit (a session also after
it sits idle). The container runs with --rm under docker's init and ends with its work, so it removes itself even if
testbench here has stopped. Secrets are passed by name (-e NAME): docker reads their values from this environment, so
they never appear on a command line.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .. import config as C
from ..config import ConfigError
from . import Backend, project_files

PKG = Path(__file__).resolve().parents[1]
BASE_PIP = ["nbformat>=5.9", "nbclient>=0.10", "ipykernel>=6.29", "jupyter_client>=8", "pyyaml>=6"]
WORK = "/work"                           # in the container: everything testbench puts there
PROJECT = f"{WORK}/project"              # the copy of the project, and the working directory
RUN = f"{WORK}/run"                      # the run folder the executor writes; copied back to the local run folder
KERNEL = f"{WORK}/kernel"                # a session's kernel state (connection file, ready, busy, last_used)
WATCH = f"{RUN}/watchdog"                # a plan's watchdog state; its log comes back with the evidence
DONE = f"{WORK}/done"                    # written when a plan's work has ended
IDLE_SLACK = 10                          # the container's watchdog waits this much longer than the supervisor here
LINGER_SECONDS = 120                     # after a plan, the container waits (idle) this long for its evidence to be taken
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy")
IMAGE_VERSION = 1                        # part of the derived image's tag: bump when the Dockerfile changes
REMOTE_STOP = ("inside the container: its watchdog ends the work at the time limit and the container removes itself, "
               "even if testbench on this machine has stopped")
SAFE = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}

# Run in the container with its Python (standard library only): a tar.gz on stdout with the run folder - only the
# files that show progress, or all of it - and `done` once the work has ended. Light files are read whole first,
# so a file rewritten while it is read never breaks the archive.
PROBE = r'''
import glob, io, os, sys, tarfile
full = sys.argv[1] == "full"
os.chdir("/work")
if full:
    names = [os.path.join(d, f) for d, _, fs in os.walk("run") for f in fs]
else:
    names = ["run/progress.jsonl", "run/report.json"] + glob.glob("run/steps/*/log.txt") + \
            glob.glob("run/steps/*/result.json") + glob.glob("run/watchdog/*")
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as t:
    for n in names + ["done"]:
        if not os.path.isfile(n):
            continue
        if full:
            t.add(n)
            continue
        try:
            data = open(n, "rb").read()
        except OSError:
            continue
        info = tarfile.TarInfo(n)
        info.size, info.mode, info.mtime = len(data), 0o644, os.path.getmtime(n)
        t.addfile(info, io.BytesIO(data))
'''

LIST = r'''
import os, sys
p = sys.argv[1]
if os.path.isdir(p):
    print("\n".join(sorted(n + ("/" if os.path.isdir(os.path.join(p, n)) else "") for n in os.listdir(p))))
elif os.path.exists(p):
    print(os.path.basename(p))
else:
    sys.exit("no such file or folder in the container: " + p)
'''

READY = "import os, sys; sys.exit(0 if os.path.exists('/work/kernel/ready') else 1)"


class DockerError(RuntimeError):
    pass


class Docker:
    """The container CLI. Every command it runs is logged (with its exit code and the end of its stderr); secrets
    only ever appear in them by name."""

    def __init__(self, command: list[str], log: Path | None = None, error: type[Exception] = DockerError):
        self.command, self.log, self.error = list(command), log, error

    def __call__(self, *args, data: bytes | None = None, stdin=None, stdout=None, timeout: float = 600,
                 check: bool = True, binary: bool = False) -> subprocess.CompletedProcess:
        argv = [*self.command, *map(str, args)]
        shown = shlex.join(a if len(a) <= 160 else a[:60].split("\n")[0] + " ..." for a in argv)   # scripts cut short
        began = time.time()
        try:
            p = subprocess.run(argv, input=data, stdin=None if data is not None else (stdin or subprocess.DEVNULL),
                               stdout=stdout or subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        except FileNotFoundError:
            raise self.error(f"`{self.command[0]}` is not installed here: install Docker, or set docker.command to the "
                             "container CLI you use (e.g. [podman])") from None
        except subprocess.TimeoutExpired:
            raise self.error(f"`{shown}` did not answer within {timeout:.0f} s") from None
        err = p.stderr.decode(errors="replace")
        if self.log:
            with self.log.open("a") as f:
                f.write(f"$ {shown}  ({time.time() - began:.1f} s, exit {p.returncode})\n"
                        + (err[-1500:] + ("" if err.endswith("\n") else "\n") if err.strip() else ""))
        if check and p.returncode != 0:
            raise self.error(f"`{shown[:160]}` exited {p.returncode}: {err.strip()[-800:] or 'no reason given'}")
        out = p.stdout if (binary or p.stdout is None) else p.stdout.decode(errors="replace")
        return subprocess.CompletedProcess(argv, p.returncode, out, err)

    def status(self, name: str) -> str:
        """created, running, exited, ... or gone."""
        try:
            p = self("inspect", "--format", "{{.State.Status}}", name, check=False, timeout=60)
        except RuntimeError:
            return "gone"
        return p.stdout.strip() if p.returncode == 0 else "gone"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dockerfile(cfg: dict) -> str:
    d = cfg["docker"]
    reqs = " ".join(shlex.quote(r) for r in sorted(set(BASE_PIP + list(d.get("pip") or []))))
    ca = bool(d.get("ca_bundle"))
    return "\n".join(filter(None, [
        "# agent-testbench: docker.image with what the executor and the kernel need, and docker.pip",
        f"FROM {d['image']}",
        "USER root",
        "ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore PIP_BREAK_SYSTEM_PACKAGES=1",
        "COPY testbench-ca.crt /tmp/testbench-ca.crt" if ca else "",
        f"RUN {'PIP_CERT=/tmp/testbench-ca.crt ' if ca else ''}{shlex.quote(d['python'])} -m pip install --no-cache-dir "
        f"{reqs}{' && rm -f /tmp/testbench-ca.crt' if ca else ''}",
        "LABEL agent-testbench=1",
    ])) + "\n"


def image_tag(cfg: dict) -> str:
    """The derived image's tag: a hash of everything that goes into it, so a change of config builds a new one."""
    d = cfg["docker"]
    ca = Path(d["ca_bundle"]).read_bytes() if d.get("ca_bundle") else b""
    key = json.dumps({"v": IMAGE_VERSION, "dockerfile": dockerfile(cfg), "ca": _sha256(ca)}, sort_keys=True)
    return f"testbench-env:{_sha256(key.encode())[:16]}"


def ensure_image(cfg: dict, docker: Docker) -> str:
    """The image containers start from: docker.image itself (setup: none), or the derived image, built once."""
    d = cfg["docker"]
    if d["setup"] == "none":
        return d["image"]
    tag = image_tag(cfg)
    if docker("image", "inspect", tag, check=False, timeout=120).returncode == 0:
        return tag
    print(f"[testbench] building the container image {tag} from {d['image']} (once for this config; it can take a "
          "few minutes)", flush=True)
    with tempfile.TemporaryDirectory() as ctx:
        Path(ctx, "Dockerfile").write_text(dockerfile(cfg))
        if d.get("ca_bundle"):
            shutil.copyfile(d["ca_bundle"], Path(ctx, "testbench-ca.crt"))
        proxies = [x for n in PROXY_VARS if os.environ.get(n) for x in ("--build-arg", n)]   # values from the environment
        p = docker("build", "-t", tag, *proxies, *d.get("build_args", []), ctx, check=False, timeout=3600)
    if docker.log:
        with docker.log.open("a") as f:
            f.write(p.stdout[-20000:] + p.stderr[-20000:] + "\n")
    if p.returncode != 0:
        raise docker.error(
            f"could not build the container image from {d['image']} (exit {p.returncode}):\n"
            f"{(p.stderr or p.stdout).strip()[-1500:]}\nIf pip could not reach its index, the build needs this "
            "machine's network or proxy: set docker.build_args (e.g. [--network, host]) and, behind a TLS-intercepting "
            "proxy, docker.ca_bundle. Or use an image that already has the basics, with docker.setup: none.")
    return tag


def missing_secrets(names: list[str]) -> list[str]:
    return [n for n in names if n not in os.environ]


def create_args(cfg: dict, name: str, image: str, command: list[str], labels: dict, secrets: list[str],
                env: dict | None = None) -> list[str]:
    """`docker create` for one run or session: removed when it ends, under docker's init (it reaps processes and
    passes signals on), limited as configured. Secrets go by name only."""
    d = cfg["docker"]
    args = ["create", "--rm", "--init", "--name", name, "--network", d["network"], "--workdir", PROJECT,
            "--label", "agent-testbench=1", "--label", f"agent-testbench.project={cfg['project']}"]
    for key, value in labels.items():
        args += ["--label", f"agent-testbench.{key}={value}"]
    if d.get("gpus") is not None:
        args += ["--gpus", str(d["gpus"])]
    if d.get("cpu"):
        args += ["--cpus", f"{float(d['cpu']):g}"]
    if d.get("memory_gb"):
        args += ["--memory", f"{int(float(d['memory_gb']) * 1024)}m"]
    plain = {"PYTHONPATH": f"{WORK}/pkg", "TESTBENCH_CACHE": f"{WORK}/cache", "PYTHONUNBUFFERED": "1", **(env or {})}
    if d.get("ca_bundle"):
        plain["PIP_CERT"] = f"{WORK}/ca.crt"
    for key, value in plain.items():
        args += ["-e", f"{key}={value}"]
    for key in [n for n in ("TESTBENCH_WATCHDOG_SECONDS",) if n in os.environ] + list(secrets):
        args += ["-e", key]                                  # docker takes the value from its environment
    return args + list(d.get("run_args") or []) + [image, *command]


def bundle(root: Path | None, exclude: list[str], *, pkg: bool, files: dict[str, bytes] | None = None,
           replace: dict[str, Path] | None = None):
    """A tar.gz to unpack at the container's / : the project at /work/project, this package at /work/pkg, and
    `files` by their path in the container. replace: project files sent as another local file instead (the sealed
    data, cut on this machine). Kept in a temporary file, not in memory."""
    out = tempfile.TemporaryFile()
    replace = replace or {}
    with tarfile.open(fileobj=out, mode="w:gz") as t:
        if root is not None:
            for f in project_files(root, exclude):
                rel = f.relative_to(root).as_posix()
                t.add(replace.get(rel, f), arcname=f"{PROJECT[1:]}/{rel}", recursive=False)
        if pkg:
            t.add(PKG, arcname=f"{WORK[1:]}/pkg/agent_testbench", filter=lambda m: None if "__pycache__" in m.name else m)
        for path, data in (files or {}).items():
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size, info.mode, info.mtime = len(data), 0o644, time.time()
            t.addfile(info, io.BytesIO(data))
    out.seek(0)
    return out


def upload(docker: Docker, name: str, archive) -> None:
    with archive:
        docker("cp", "-", f"{name}:/", stdin=archive, timeout=1800)


def download(docker: Docker, name: str, path: str) -> bytes:
    """One file from the container."""
    with tempfile.TemporaryFile() as out:
        docker("cp", f"{name}:{path}", "-", stdout=out, timeout=1800, binary=True)
        out.seek(0)
        with tarfile.open(fileobj=out, mode="r:*") as t:
            first = t.next()
            if first is not None and first.isfile():
                return t.extractfile(first).read()
    raise docker.error(f"{path} in the container is a folder, not a file; get one file at a time")


def _extract_run(archive, dest: Path) -> bool:
    """Unpack a PROBE archive into the local run folder; True if the work in the container has ended."""
    done, members = False, []
    with tarfile.open(fileobj=archive, mode="r:*") as t:
        for m in t.getmembers():
            if m.name == "done":
                done = True
            elif m.isfile() and m.name.startswith("run/"):
                m.name = m.name[len("run/"):]
                if m.name and not m.name.startswith("/") and ".." not in Path(m.name).parts:
                    members.append(m)
        t.extractall(dest, members=members, **SAFE)
    return done


def plan_limit(plan: dict) -> float:
    """The plan's hard stop in the container, in minutes: its steps' max_minutes and the start-up allowance."""
    return sum(st["max_minutes"] for st in plan["steps"]) + backend.startup_minutes


def _ended(status: str, started: subprocess.Popen) -> bool:
    """The container is gone or stopped, or `docker start` gave up before it ran (it then stays created)."""
    return status not in ("created", "running") or (status == "created" and started.poll() is not None)


def _tail(path: Path, n: int = 2000) -> str:
    return path.read_text(errors="replace")[-n:] if path.exists() else ""


class DockerRunner:
    """A plan in a fresh container: the executor runs there under the watchdog; progress comes back every few
    seconds and everything at the end, then the container is removed."""

    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta
        self.docker = Docker(cfg["docker"]["command"], self.run_dir / "docker.log")
        self.started = self.name = None

    def run(self):
        from .. import report as R, runs
        d = self.cfg["docker"]
        self.started = time.time()
        secrets = sorted({n for st in self.plan["steps"] for n in st["secrets"]})
        if missing_secrets(secrets):
            raise DockerError(f"secrets not in this environment: {', '.join(missing_secrets(secrets))} - export them "
                              "first (they reach the container by name, never by value)")
        limit = plan_limit(self.plan)
        name = f"testbench-{self.cfg['project']}-{self.run_dir.name}"
        runs.write_state(self.run_dir, docker_command=d["command"], docker_container=name, remote_stop=REMOTE_STOP)
        image = ensure_image(self.cfg, self.docker)
        py = d["python"]
        command = [py, "-m", "agent_testbench.backends.docker_backend", "plan", "--max-minutes", f"{limit:g}", "--",
                   py, "-m", "agent_testbench.executor", "--plan", f"{WORK}/plan.json", "--project", PROJECT,
                   "--run-dir", RUN, "--workdir", PROJECT, "--meta", json.dumps(self.meta)]
        self.name = name                                     # from here on, cancel removes it
        self.docker(*create_args(self.cfg, name, image, command, {"run": self.run_dir.name}, secrets), timeout=1800)
        files = {f"{WORK}/plan.json": json.dumps(self.plan).encode()}
        if d.get("ca_bundle"):
            files[f"{WORK}/ca.crt"] = Path(d["ca_bundle"]).read_bytes()
        from ..dataseal import cut_copies
        with cut_copies(self.root, self.plan) as sealed:     # the sealed rows never leave this machine
            upload(self.docker, name, bundle(self.root, self.cfg["exclude"], pkg=True, files=files, replace=sealed))
        started = runs.spawn([*d["command"], "start", "--attach", name], cwd=self.run_dir,
                             log_path=self.run_dir / "container.log")
        print(f"[testbench] hard stop after {limit:g} min {REMOTE_STOP}", flush=True)
        print(f"[testbench] running the plan in container {name} from {image} (network: {d['network']})", flush=True)
        give_up = time.time() + limit * 60 + LINGER_SECONDS + 600     # it ends itself long before this
        poll = float(os.environ.get("TESTBENCH_DOCKER_POLL_SECONDS", 2))
        while time.time() < give_up:
            done = self._mirror(full=False)
            if done:
                break
            if done is None and _ended(self.docker.status(name), started):
                break
            time.sleep(poll)
        self._mirror(full=True)
        self._remove()
        rep = R.load(self.run_dir)
        if rep and rep.get("status") == "running":
            rep.update(status="error", finished=time.time(), stopped_because="the container's executor ended before the "
                       "plan finished (killed - out of memory under docker.memory_gb?); see container.log in the run")
        if not rep:
            rep = {**self.meta, "steps": [], "started": self.started, "status": "error",
                   "stopped_because": "the plan never started in the container",
                   "launch_error": "The container could not start the plan. Its output:\n\n```\n"
                                   f"{_tail(self.run_dir / 'container.log')}\n```\n\nDocker commands:\n\n```\n"
                                   f"{_tail(self.run_dir / 'docker.log')}\n```\n\nWith docker.setup: none the image "
                                   f"needs {d['python']} with ipykernel, jupyter_client, nbformat, nbclient and pyyaml."}
        return rep, self.cost(rep)

    def _mirror(self, full: bool) -> bool | None:
        """Bring the run folder back: progress and logs while it runs, everything at the end. True once the work
        has ended; None if the container could not be asked (not started yet, or gone)."""
        if not self.name:
            return None
        with tempfile.TemporaryFile() as out:
            try:
                p = self.docker("exec", self.name, self.cfg["docker"]["python"], "-c", PROBE, "full" if full else "light",
                                stdout=out, check=False, timeout=1800, binary=True)
            except DockerError:
                return None
            if p.returncode != 0:
                return None
            out.seek(0)
            try:
                return _extract_run(out, self.run_dir)
            except (tarfile.TarError, OSError, EOFError):
                return None

    def _remove(self) -> None:
        if self.name:
            try:
                self.docker("rm", "--force", self.name, check=False, timeout=120)
            except DockerError:
                pass

    def cancel(self) -> str:
        if not self.name:
            return ""
        if self.docker.status(self.name) == "running":
            self.docker("kill", "--signal", "INT", self.name, check=False, timeout=60)   # init -> the executor:
            end = time.time() + 60                                                    # it writes its report and ends
            while time.time() < end and self._mirror(full=False) is False:
                time.sleep(0.5)
            self._mirror(full=True)
        self._remove()
        return f"stopped and removed the container {self.name}"

    def cost(self, rep: dict) -> float:
        minutes = (time.time() - self.started) / 60 if self.started else 0.0
        return backend.rate_per_minute(self.cfg, self.plan) * minutes


class DockerSession:
    """A kernel in a long-running container, in a copy of the project; the same launcher and client as everywhere
    else. The container lives as long as its kernel: its watchdog ends the kernel at max_minutes or when idle."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        from ..session_backends import SessionError
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        h = state.get("handles") or {}
        self.name = h.get("container")
        self.docker = Docker(h.get("command") or cfg["docker"]["command"], self.sdir / "docker.log", error=SessionError)
        self.python = h.get("python") or cfg["docker"]["python"]

    def start(self) -> dict:
        from ..session_backends import SessionError
        try:
            backend.validate(self.cfg)
        except ConfigError as exc:
            raise SessionError(str(exc)) from None
        d, s = self.cfg["docker"], self.state
        if s.get("gpu") and d.get("gpus") is None:
            raise SessionError(f"the session asks for GPU {s['gpu']}, but containers get no GPU unless docker.gpus is "
                               "set (e.g. all; needs NVIDIA's Container Toolkit)")
        secrets = list(s.get("secrets") or [])
        if missing_secrets(secrets):
            raise SessionError(f"secrets not in this environment: {', '.join(missing_secrets(secrets))} - export them "
                               "first (they reach the container by name, never by value)")
        image = ensure_image(self.cfg, self.docker)
        key = f"{self.sdir.resolve()} {s.get('created')}".encode()
        name = f"testbench-{self.cfg['project']}-{s['name']}-{_sha256(key)[:6]}"
        handles = {"container": name, "image": image, "command": d["command"], "python": self.python,
                   "workdir": PROJECT, "remote_stop": REMOTE_STOP}
        s["handles"] = handles
        command = [self.python, "-m", "agent_testbench.backends.docker_backend", "session",
                   "--max-minutes", f"{s['max_minutes'] + backend.startup_minutes:g}",
                   "--idle-minutes", f"{s['idle_minutes'] + IDLE_SLACK:g}", "--",
                   self.python, "-m", "agent_testbench.kernelkit.launcher", "--workdir", PROJECT, "--state-dir", KERNEL,
                   "--shim"]
        self.name = name                                     # from here on, stop removes it
        self.docker(*create_args(self.cfg, name, image, command, {"session": s["name"]}, secrets,
                                 env={"TESTBENCH_SESSION": s["name"]}), timeout=1800)
        files = {f"{WORK}/ca.crt": Path(d["ca_bundle"]).read_bytes()} if d.get("ca_bundle") else {}
        with self._sealed() as sealed:
            upload(self.docker, name, bundle(self.root, self.cfg["exclude"], pkg=True, files=files, replace=sealed))
        from ..runs import spawn
        started = spawn([*self.docker.command, "start", "--attach", name], cwd=self.sdir,
                        log_path=self.sdir / "kernel.log")
        for _ in range(480):
            if self.docker("exec", name, self.python, "-c", READY, check=False, timeout=60).returncode == 0:
                return handles
            if _ended(self.docker.status(name), started):
                break
            time.sleep(0.25)
        raise SessionError(f"the kernel did not start in container {name}: {_tail(self.sdir / 'kernel.log', 1500)}")

    def alive(self) -> bool:
        if not self.name:
            return False
        try:
            return self.docker("exec", self.name, self.python, "-c", READY, check=False, timeout=60).returncode == 0
        except RuntimeError:
            return False

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        from ..session_backends import _parse
        req = f"{WORK}/out/{prefix}.json"
        upload(self.docker, self.name, bundle(None, [], pkg=False, files={
            req: json.dumps({"code": code, "timeout": timeout, "prefix": prefix}).encode()}))
        p = self.docker("exec", self.name, self.python, "-m", "agent_testbench.kernelkit.client", "--connection",
                        f"{KERNEL}/kernel.json", "--request", req, "--out-dir", f"{WORK}/out/{prefix}",
                        timeout=timeout + 180, check=False)
        res = _parse(p.stdout)
        local = []
        for path in res.get("images", []):
            dst = Path(out_dir) / Path(path).name
            dst.write_bytes(download(self.docker, self.name, path))
            local.append(str(dst))
        res["images"] = local
        return res

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{PROJECT}/{path}"

    def ls(self, path: str) -> list[str]:
        p = self.docker("exec", self.name, self.python, "-c", LIST, self._abs(path or "."), check=False, timeout=60)
        if p.returncode != 0:
            raise self.docker.error(p.stderr.strip()[-500:] or f"could not list {path}")
        return [ln for ln in p.stdout.splitlines() if ln.strip()]

    def get(self, remote: str, local: Path) -> None:
        Path(local).write_bytes(download(self.docker, self.name, self._abs(remote)))

    def put(self, local: Path, remote: str) -> None:
        upload(self.docker, self.name, bundle(None, [], pkg=False, files={self._abs(remote): Path(local).read_bytes()}))

    def _busy(self, script: str, timeout: float) -> subprocess.CompletedProcess:
        """A shell command in the project's copy, counted as activity by the container's watchdog."""
        k = KERNEL
        return self.docker("exec", self.name, "sh", "-c",
                           f"echo {time.time() + timeout + 120:.0f} > {k}/busy; cd {PROJECT} && {{ {script}\n}} 2>&1; "
                           f"rc=$?; rm -f {k}/busy; : > {k}/last_used; exit $rc", timeout=timeout + 60, check=False)

    def install(self, packages: list[str]) -> str:
        p = self._busy(f"{shlex.quote(self.python)} -m pip install {' '.join(shlex.quote(x) for x in packages)}", 1800)
        return p.stdout[-3000:] + ("" if p.returncode == 0 else f"\n(pip exited {p.returncode})")

    def _sealed(self):
        """The project's sealed data (testbench.yaml's data:) cut on this machine: a session sees what plans see."""
        from ..dataseal import cut_copies, sealed_view
        return cut_copies(self.root, sealed_view(self.cfg))

    def sync(self) -> str:
        with self._sealed() as sealed:
            upload(self.docker, self.name, bundle(self.root, self.cfg["exclude"], pkg=False, replace=sealed))
        self.docker("exec", self.name, "sh", "-c", f": > {KERNEL}/last_used", check=False, timeout=60)
        return f"project files copied to {PROJECT} in container {self.name} (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        return self._busy(command, timeout).stdout[-20000:]

    def stop(self) -> None:
        if not self.name:
            return
        try:
            self.docker("rm", "--force", self.name, check=False, timeout=120)
        except RuntimeError:
            pass


class DockerBackend(Backend):
    name = "docker"
    startup_minutes = 2.0             # creating the container and copying the project (a first image build is extra)
    hard_stop = "kernelkit's watchdog in the container ends the work at the time limit; the container removes itself"
    config_defaults = {"docker": {"image": "python:3.11-slim", "setup": "build", "pip": [], "network": "bridge",
                                  "gpus": None, "cpu": None, "memory_gb": None, "usd_per_hour": 0.0, "python": "python3",
                                  "run_args": [], "build_args": [], "ca_bundle": None, "command": ["docker"]}}

    def check_hardware(self, where: str, gpu) -> None:
        """Containers get the GPUs docker.gpus gives them; `gpu:` on a docker plan is a label (checked in validate)."""

    def validate(self, cfg: dict) -> None:
        d = cfg["docker"]
        if not isinstance(d.get("image"), str) or not d["image"].strip():
            raise ConfigError("`docker.image` is the image containers start from, e.g. python:3.11-slim (Python 3.10 "
                              "or newer)")
        if d.get("setup") not in ("build", "none"):
            raise ConfigError("`docker.setup` is build (testbench builds an image from docker.image with its basics and "
                              "docker.pip, once) or none (docker.image already has ipykernel, jupyter_client, nbformat, "
                              "nbclient and pyyaml)")
        net = d.get("network")
        if not isinstance(net, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", net):
            raise ConfigError(f"`docker.network` is {net!r}; write bridge (Docker's default network), none (no network "
                              "at all: a sandbox), host (this machine's network) or the name of a network made with "
                              "`docker network create`")
        gpus = d.get("gpus")
        if not (gpus is None or (isinstance(gpus, int) and not isinstance(gpus, bool) and gpus > 0)
                or (isinstance(gpus, str) and gpus.strip())):
            raise ConfigError("`docker.gpus` is what `docker run --gpus` takes: all, a count, or \"device=0,1\" (needs "
                              "NVIDIA's Container Toolkit); leave it out for no GPU")
        for key, flag in (("cpu", "--cpus"), ("memory_gb", "--memory")):
            v = d.get(key)
            if v is not None and not C.number(v, positive=True):
                raise ConfigError(f"`docker.{key}` is a positive number (docker's {flag}), or leave it out for no limit")
        v = d.get("usd_per_hour")
        if not C.number(v):
            raise ConfigError("`docker.usd_per_hour` is what the machine costs you per hour while it works (0 for your "
                              "own)")
        if not isinstance(d.get("python"), str) or not d["python"].strip():
            raise ConfigError("`docker.python` is the image's Python command, e.g. python3")
        for key, example in (("command", "[docker] or [podman]"), ("run_args", "[-v, /data:/data:ro]"),
                             ("build_args", "[--network, host]")):
            v = d.get(key)
            if v is None:
                v = d[key] = []
            try:
                v = d[key] = shlex.split(v) if isinstance(v, str) else v
            except ValueError as exc:                    # an unclosed quote
                raise ConfigError(f"`docker.{key}` {v!r} cannot be split into arguments ({exc}); write it as a list, "
                                  f"e.g. {example}") from None
            if not isinstance(v, list) or not all(isinstance(x, (str, int, float)) for x in v):
                raise ConfigError(f"`docker.{key}` is a list of arguments, e.g. {example}")
            d[key] = [str(x) for x in v]
        if not d["command"]:
            raise ConfigError("`docker.command` is the container CLI, as a list: [docker] or [podman]")
        pip = d.get("pip") or []
        if not isinstance(pip, list) or not all(isinstance(x, str) for x in pip):
            raise ConfigError("`docker.pip` is a list of packages for the container image (pin them): [torch==2.5.1]")
        if pip and d["setup"] == "none":
            raise ConfigError("`docker.pip` needs docker.setup: build (with setup: none, docker.image is used as it is); "
                              "put the packages in your image, or use setup: build")
        if d.get("ca_bundle"):
            ca = Path(str(d["ca_bundle"])).expanduser()
            ca = ca if ca.is_absolute() else Path(cfg["root"]) / ca
            if not ca.is_file():
                raise ConfigError(f"`docker.ca_bundle`: {ca} does not exist; it is the CA bundle (PEM) pip needs behind a "
                                  "TLS-intercepting proxy - point it at the file, or leave it out")
            d["ca_bundle"] = str(ca)
        if gpus is None:
            wants = [f"plans.{n}" for n, p in cfg.get("plans", {}).items() if p["backend"] == "docker" and p.get("gpu")]
            s = cfg.get("sessions") or {}
            wants += ["sessions"] if s.get("backend") == "docker" and s.get("gpu") else []
            if wants:
                raise ConfigError(f"`{wants[0]}.gpu` asks for a GPU, but containers get no GPU unless docker.gpus is set "
                                  "(e.g. gpus: all; needs NVIDIA's Container Toolkit) - set it, or drop the gpu")

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        return float(cfg["docker"]["usd_per_hour"]) / 60

    def prices(self, cfg: dict) -> list[str]:
        d = cfg["docker"]
        return [f"  ${float(d['usd_per_hour']):.2f}/h for containers from {d['image']} (testbench.yaml docker.usd_per_hour)"]

    def plan_runner(self):
        return DockerRunner

    def session_class(self):
        return DockerSession

    def cancel_remote(self, state: dict) -> str:
        name = state.get("docker_container")
        if not name:
            return ""
        try:
            p = Docker(state.get("docker_command") or ["docker"])("rm", "--force", name, check=False, timeout=120)
        except DockerError as exc:
            return f"could not remove the container {name}: {exc}"
        gone = p.returncode == 0 or "No such container" in p.stderr or "no such container" in p.stderr
        return f"removed the container {name}" if gone else f"could not remove the container {name}: {p.stderr.strip()}"

    def doctor(self):
        what = "docker CLI (docker plans and sessions)"
        exe = shutil.which("docker")
        if not exe:
            return [(what, False, "install Docker (or set docker.command, e.g. to podman)")]
        try:
            p = subprocess.run([exe, "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=20)
            ok, detail = p.returncode == 0 and bool(p.stdout.strip()), p.stdout.strip() or p.stderr.strip()[-300:]
        except subprocess.TimeoutExpired:
            ok, detail = False, "no answer within 20 s"
        return [(what, True, exe),
                ("docker daemon", ok, f"server {detail}" if ok else f"{detail or 'no answer'} - start Docker")]


backend = DockerBackend()


# ---- inside the container ------------------------------------------------------------------------------------

def inside(argv=None) -> None:
    """The container's main process, under docker's init:

        python -m agent_testbench.backends.docker_backend plan|session --max-minutes M [--idle-minutes I] -- COMMAND...

    It starts the watchdog first, then the work. A session's launcher replaces this process, so the container lives
    as long as the kernel (the watchdog finds the launcher by the kernel folder on its command line). A plan's
    executor runs as its child, noted for the watchdog to interrupt; this process never has the watchdog's folder on
    its command line, so the watchdog leaves it to finish up: an interrupt (`docker kill --signal INT`) passes to the
    executor, which writes a cancelled report; when it ends, `done` is written and the container waits, idle, for
    its evidence to be taken, then ends.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    cut = argv.index("--") if "--" in argv else len(argv)
    ap = argparse.ArgumentParser(prog="python -m agent_testbench.backends.docker_backend")
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
    if a.mode == "session":
        os.execvp(command[0], command)
    proc = subprocess.Popen(command, cwd=PROJECT if os.path.isdir(PROJECT) else None, start_new_session=True)
    watchdog.note_interrupt(state_dir, proc.pid)

    def interrupt(signum, frame):
        try:
            os.kill(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        code = proc.wait(timeout=a.max_minutes * 60 + 90)     # the watchdog stops it at max_minutes; this backs it up
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        code = proc.wait()
    Path(DONE).write_text(f"{code}\n")
    time.sleep(a.linger_seconds)                           # testbench takes the evidence and removes the container


if __name__ == "__main__":
    inside()
