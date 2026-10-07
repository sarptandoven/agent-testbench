"""Where a live session's kernel runs: this machine, a Modal Sandbox, or a Colab VM.

Each backend starts the same kernel launcher (kernelkit.launcher) next to the project's files and runs code
through the same client (kernelkit.client), so `exec` returns the same evidence everywhere. Remote machines get
a hard stop on their own side: a Modal Sandbox has a lifetime timeout and an idle timeout (a running command
counts as activity); a Colab session is stopped by the session's supervisor.
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from .backends import project_files

PKG = Path(__file__).resolve().parent
BASE_PIP = ["nbformat>=5.9", "nbclient>=0.10", "ipykernel>=6.29", "jupyter_client>=8", "pyyaml>=6"]
MARK = "TESTBENCH_RESULT "


class SessionError(RuntimeError):
    pass


def bundle_bytes(root: Path, exclude: list[str], with_pkg: bool) -> bytes:
    """The project (and, for remote machines, this package) as one tar.gz: project/... and pkg/agent_testbench/..."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for f in project_files(root, exclude):
            t.add(f, arcname="project/" + str(f.relative_to(root)))
        if with_pkg:
            t.add(PKG, arcname="pkg/agent_testbench", filter=lambda m: None if "__pycache__" in m.name else m)
    return buf.getvalue()


def _parse(stdout: str) -> dict:
    for line in reversed(stdout.splitlines()):
        if line.startswith(MARK):
            return json.loads(line[len(MARK):])
    raise SessionError("the session's kernel client gave no result; last output:\n" + "\n".join(stdout.splitlines()[-15:]))


# ---- this machine --------------------------------------------------------------------------------------------

class LocalSession:
    """A kernel on this machine, in the project folder, with this Python environment."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        self.kdir = self.sdir / "kernel"

    def start(self) -> dict:
        from .runs import spawn
        env = {**os.environ, "TESTBENCH_SESSION": self.state["name"]}
        missing = [s for s in self.state.get("secrets", []) if s not in env]
        if missing:
            raise SessionError(f"secrets not in this environment: {', '.join(missing)} - export them first")
        proc = spawn([sys.executable, "-m", "agent_testbench.kernelkit.launcher", "--workdir", str(self.root),
                      "--state-dir", str(self.kdir), "--shim"], cwd=self.root, env=env, log_path=self.sdir / "kernel.log")
        for _ in range(240):
            if (self.kdir / "ready").exists():
                return {"launcher_pid": proc.pid, "workdir": str(self.root)}
            if proc.poll() is not None:
                break
            time.sleep(0.25)
        raise SessionError("the kernel did not start; see kernel.log in the session folder")

    def alive(self) -> bool:
        from .runs import alive
        return alive(self.state.get("handles", {}).get("launcher_pid")) and (self.kdir / "ready").exists()

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        from .kernelkit.client import execute
        return execute(str(self.kdir / "kernel.json"), code, timeout, str(out_dir), prefix)

    def _path(self, remote: str) -> Path:
        p = Path(remote)
        return p if p.is_absolute() else self.root / p

    def ls(self, path: str) -> list[str]:
        p = self._path(path or ".")
        return sorted(x.name + ("/" if x.is_dir() else "") for x in p.iterdir()) if p.is_dir() else [p.name]

    def get(self, remote: str, local: Path) -> None:
        shutil.copyfile(self._path(remote), local)

    def put(self, local: Path, remote: str) -> None:
        dst = self._path(remote)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dst)

    def install(self, packages: list[str]) -> str:
        p = subprocess.run([sys.executable, "-m", "pip", "install", *packages], capture_output=True, text=True, timeout=1800)
        return (p.stdout + p.stderr)[-3000:] + ("" if p.returncode == 0 else f"\n(pip exited {p.returncode})")

    def sync(self) -> str:
        return "local sessions run in the project folder itself; nothing to sync"

    def shell(self, command: str, timeout: float) -> str:
        p = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, text=True, timeout=timeout)
        return (p.stdout + p.stderr)[-20000:]

    def stop(self) -> None:
        from .runs import alive, kill_group
        pid = self.state.get("handles", {}).get("launcher_pid")
        if not pid:
            return
        kill_group(pid)                                  # the launcher shuts its kernel down on SIGTERM
        for _ in range(40):
            if not alive(pid):
                return
            time.sleep(0.25)
        kill_group(pid, signal.SIGKILL)


# ---- Modal Sandbox -------------------------------------------------------------------------------------------

class ModalSession:
    """A kernel in a Modal Sandbox (the machinery behind Modal Notebooks), on the GPU you choose. The project is
    in /work/project (also /content); /testbench is the project's volume, kept between sessions and runs."""

    WORK = "/work/project"
    KDIR = "/tmp/testbench/kernel"

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        self._sb = None

    def _app_name(self) -> str:
        return f"testbench-{self.cfg['project']}"

    def _image(self):
        import modal
        m = self.cfg["modal"]
        image = modal.Image.debian_slim(python_version=str(m["python"]))
        if m.get("apt"):
            image = image.apt_install(*m["apt"])
        image = image.pip_install(*BASE_PIP, *(m.get("pip") or []))
        if m.get("requirements"):
            image = image.pip_install_from_requirements(str(self.root / m["requirements"]))
        for cmd in m.get("run_commands") or []:
            image = image.run_commands(cmd)
        return image.add_local_python_source("agent_testbench")

    def sandbox(self):
        import modal
        if self._sb is None:
            self._sb = modal.Sandbox.from_id(self.state["handles"]["sandbox_id"])
        return self._sb

    def _run(self, *args, timeout: float = 600) -> tuple[int, str]:
        p = self.sandbox().exec(*args, timeout=int(timeout))
        out = p.stdout.read()
        err = p.stderr.read()
        code = p.wait()
        return code, out + err

    def start(self) -> dict:
        import modal
        s = self.state
        app = modal.App.lookup(self._app_name(), create_if_missing=True)
        volume = modal.Volume.from_name(self._app_name(), create_if_missing=True)
        m = self.cfg["modal"]
        self._sb = modal.Sandbox.create(
            "python", "-m", "agent_testbench.kernelkit.launcher", "--workdir", self.WORK, "--state-dir", self.KDIR, "--shim",
            app=app, image=self._image(), gpu=s.get("gpu"), cpu=float(s.get("cpu") or m["cpu"]),
            memory=int(float(s.get("memory_gb") or m["memory_gb"]) * 1024), timeout=int(s["max_minutes"] * 60),
            idle_timeout=int(s["idle_minutes"] * 60), volumes={"/testbench": volume}, workdir=self.WORK,
            secrets=[modal.Secret.from_name(n) for n in s.get("secrets", [])],
            env={"TESTBENCH_CACHE": "/testbench/cache", "TESTBENCH_SESSION": s["name"]},
            tags={"testbench_project": self.cfg["project"], "testbench_session": s["name"]})
        handles = {"sandbox_id": self._sb.object_id, "workdir": self.WORK, "app": self._app_name()}
        self.state["handles"] = handles
        self._upload_project()
        for _ in range(240):
            code, _ = self._run("test", "-f", f"{self.KDIR}/ready", timeout=30)
            if code == 0:
                return handles
            if self._sb.poll() is not None:
                break
            time.sleep(1)
        raise SessionError("the kernel did not start in the Modal Sandbox")

    def _upload_project(self) -> None:
        data = bundle_bytes(self.root, self.cfg["exclude"], with_pkg=False)
        self.sandbox().filesystem.write_bytes(data, "/tmp/testbench_bundle.tar.gz")
        script = ("import tarfile, os; os.makedirs('/work/project', exist_ok=True); t = tarfile.open('/tmp/testbench_bundle.tar.gz');"
                  "ms = [m for m in t.getmembers() if m.name.startswith('project/')];"
                  "[setattr(m, 'name', m.name[len('project/'):]) for m in ms];"
                  "t.extractall('/work/project', members=[m for m in ms if m.name]);"
                  "os.path.exists('/content') or os.symlink('/work/project', '/content');"
                  "os.makedirs('/testbench/cache', exist_ok=True)")
        code, out = self._run("python", "-c", script, timeout=600)
        if code != 0:
            raise SessionError(f"could not unpack the project in the Sandbox: {out[-800:]}")

    def alive(self) -> bool:
        try:
            return self.sandbox().poll() is None
        except Exception:
            return False

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        sb = self.sandbox()
        req = f"/tmp/testbench/{prefix}.json"
        remote_out = f"/tmp/testbench/out/{prefix}"
        sb.filesystem.write_text(json.dumps({"code": code, "timeout": timeout, "prefix": prefix}), req)
        rc, out = self._run("python", "-m", "agent_testbench.kernelkit.client", "--connection", f"{self.KDIR}/kernel.json",
                            "--request", req, "--out-dir", remote_out, timeout=timeout + 120)
        res = _parse(out)
        local = []
        for remote in res.get("images", []):
            dst = Path(out_dir) / Path(remote).name
            sb.filesystem.copy_to_local(remote, dst)
            local.append(str(dst))
        res["images"] = local
        return res

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.WORK}/{path}"

    def ls(self, path: str) -> list[str]:
        entries = self.sandbox().filesystem.list_files(self._abs(path or "."))
        return sorted(e.name + ("/" if e.is_dir() else "") for e in entries)

    def get(self, remote: str, local: Path) -> None:
        self.sandbox().filesystem.copy_to_local(self._abs(remote), local)

    def put(self, local: Path, remote: str) -> None:
        target = self._abs(remote)
        self.sandbox().filesystem.make_directory(str(Path(target).parent), create_parents=True)
        self.sandbox().filesystem.copy_from_local(local, target)

    def install(self, packages: list[str]) -> str:
        code, out = self._run("python", "-m", "pip", "install", *packages, timeout=1800)
        return out[-3000:] + ("" if code == 0 else f"\n(pip exited {code})")

    def sync(self) -> str:
        self._upload_project()
        return f"project files copied to {self.WORK} (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        return self._run("bash", "-lc", command, timeout=timeout)[1][-20000:]

    def stop(self) -> None:
        try:
            self.sandbox().terminate()
        except Exception:
            pass


# ---- Colab VM ------------------------------------------------------------------------------------------------

COLAB_BOOT = r'''
import glob, os, subprocess, sys, tarfile, time
safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
parts = sorted(glob.glob("/content/.testbench_bundle.part*"))
with open("/tmp/testbench_bundle.tar.gz", "wb") as out:
    for p in parts:
        with open(p, "rb") as f:
            out.write(f.read())
        os.remove(p)
with tarfile.open("/tmp/testbench_bundle.tar.gz") as t:
    for m in t.getmembers():
        if m.name.startswith("project/") and m.name != "project/":
            m.name = m.name[len("project/"):]
            t.extract(m, "/content", **safe)
        elif m.name.startswith("pkg/") and START:
            m.name = m.name[len("pkg/"):]
            t.extract(m, "/content/.testbench_pkg", **safe)
os.makedirs("/content/.testbench_cache", exist_ok=True)
if START:
    env = {**os.environ, "PYTHONPATH": "/content/.testbench_pkg" + os.pathsep + os.environ.get("PYTHONPATH", ""),
           "TESTBENCH_CACHE": "/content/.testbench_cache", "TESTBENCH_SESSION": SESSION, **SECRETS}
    subprocess.Popen([sys.executable, "-m", "agent_testbench.kernelkit.launcher", "--workdir", "/content",
                      "--state-dir", "/tmp/testbench/kernel", "--shim"], env=env, start_new_session=True,
                     stdout=open("/tmp/testbench_kernel.log", "a"), stderr=subprocess.STDOUT)
    for _ in range(240):
        if os.path.exists("/tmp/testbench/kernel/ready"):
            print("TESTBENCH_READY", flush=True)
            break
        time.sleep(0.5)
    else:
        print("TESTBENCH_NOT_READY", open("/tmp/testbench_kernel.log").read()[-2000:], flush=True)
else:
    print("TESTBENCH_SYNCED", flush=True)
'''

# On Colab, `exec` starts the client in the background on the VM and returns at once; short polls then read the
# result. A long cell never depends on one long-lived Colab CLI connection (which can stall), and a poll that
# stalls is simply retried.
COLAB_EXEC_START = r'''
import json, os, subprocess, sys
env = {**os.environ, "PYTHONPATH": "/content/.testbench_pkg" + os.pathsep + os.environ.get("PYTHONPATH", "")}
out = "/tmp/testbench/out/" + PREFIX
os.makedirs(out, exist_ok=True)
open("/tmp/testbench/" + PREFIX + ".json", "w").write(json.dumps(REQUEST))
subprocess.Popen([sys.executable, "-m", "agent_testbench.kernelkit.client", "--connection", "/tmp/testbench/kernel/kernel.json",
                  "--request", "/tmp/testbench/" + PREFIX + ".json", "--out-dir", out, "--result-file", out + "/result.txt"],
                 env=env, start_new_session=True, stdout=open(out + "/client.log", "w"), stderr=subprocess.STDOUT)
print("TESTBENCH_STARTED", flush=True)
'''

COLAB_EXEC_POLL = r'''
import os
out = "/tmp/testbench/out/" + PREFIX
if os.path.exists(out + "/result.txt"):
    print(open(out + "/result.txt").read(), flush=True)
else:
    log = open(out + "/client.log").read()[-3000:] if os.path.exists(out + "/client.log") else ""
    print("TESTBENCH_RUNNING " + str(len(log)), flush=True)
    if "Traceback" in log:
        print("TESTBENCH_CLIENT_FAILED\n" + log, flush=True)
'''


class ColabSession:
    """A kernel on a Colab VM driven through Google's Colab CLI. The project is in /content, as on Colab."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        c = cfg.get("colab") or {}
        self.args = [c.get("cli", "colab")] + (["--auth", c["auth"]] if c.get("auth") else [])
        self.session = state.get("handles", {}).get("colab_session") or ("testbench-" + state["name"] + "-"
                                                                          + time.strftime("%H%M%S"))[:60]

    def _colab(self, *argv, timeout: float = 900, check: bool = True) -> subprocess.CompletedProcess:
        began = time.time()
        try:
            p = subprocess.run([*self.args, *argv], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            with (self.sdir / "colab.log").open("a") as log:
                log.write(f"$ colab {argv[0]} ... no answer after {timeout:.0f} s\n")
            raise SessionError(f"`colab {argv[0]}` did not answer within {timeout:.0f} s (the Colab connection may have "
                               "stalled); the session is still there - try again, or `testbench session list`") from None
        with (self.sdir / "colab.log").open("a") as log:
            log.write(f"$ colab {argv[0]} ... ({time.time() - began:.1f} s, exit {p.returncode})\n"
                      f"{p.stdout[-3000:]}{p.stderr[-3000:]}\n")
        if check and p.returncode != 0:
            raise SessionError(f"`colab {argv[0]}` failed (exit {p.returncode}): "
                               f"{((p.stderr or p.stdout).strip().splitlines() or [''])[-1]} - see colab.log")
        return p

    def _script(self, body: str, **values) -> Path:
        path = Path(tempfile.mkdtemp()) / "testbench_colab.py"
        path.write_text("".join(f"{k} = {v!r}\n" for k, v in values.items()) + body)
        return path

    def _send_bundle(self, with_pkg: bool) -> None:
        data = bundle_bytes(self.root, self.cfg["exclude"], with_pkg=with_pkg)
        chunk = 45 * 1024 * 1024
        tmp = Path(tempfile.mkdtemp())
        for i in range(0, max(1, len(data)), chunk):
            part = tmp / f"part{i // chunk:03d}"
            part.write_bytes(data[i:i + chunk])
            self._colab("upload", "-s", self.session, str(part), f"/content/.testbench_bundle.part{i // chunk:03d}")

    def start(self) -> dict:
        if not shutil.which(self.args[0]):
            raise SessionError("The Colab CLI is not installed: `uv tool install google-colab-cli`, then log in once.")
        gpu = (self.state.get("gpu") or "").upper()
        self._colab("new", "-s", self.session, *(["--gpu", gpu] if gpu else []))
        handles = {"colab_session": self.session, "workdir": "/content", "cli": self.args}
        self.state["handles"] = handles
        pkgs = BASE_PIP + list((self.cfg.get("colab") or {}).get("pip") or [])
        self._colab("install", "-s", self.session, *pkgs, timeout=1800)
        self._send_bundle(with_pkg=True)
        secrets = {}
        for name in self.state.get("secrets", []):
            if name not in os.environ:
                raise SessionError(f"secret {name} is not in this environment; export it first")
            secrets[name] = os.environ[name]
        boot = self._script(COLAB_BOOT, START=True, SESSION=self.state["name"], SECRETS=secrets)
        p = self._colab("exec", "-s", self.session, "-f", str(boot), "--timeout", "300", timeout=600)
        boot.unlink()
        if "TESTBENCH_READY" not in p.stdout:
            raise SessionError("the kernel did not start on the Colab VM: " + p.stdout[-1500:])
        return handles

    def alive(self) -> bool:
        try:
            p = self._colab("status", "-s", self.session, timeout=120, check=False)
            return p.returncode == 0
        except Exception:
            return False

    def _try(self, *argv, timeout: float, attempts: int = 3) -> subprocess.CompletedProcess | None:
        for _ in range(attempts):
            try:
                return self._colab(*argv, timeout=timeout, check=False)
            except SessionError:
                time.sleep(2)
        return None

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        start = self._script(COLAB_EXEC_START, PREFIX=prefix, REQUEST={"code": code, "timeout": timeout, "prefix": prefix})
        p = self._try("exec", "-s", self.session, "-f", str(start), "--timeout", "120", timeout=180)
        if p is None or "TESTBENCH_STARTED" not in p.stdout:
            raise SessionError("could not start the code on the Colab VM" + (f": {p.stdout[-800:]}" if p else ""))
        poll = self._script(COLAB_EXEC_POLL, PREFIX=prefix)
        deadline = time.time() + timeout + 180          # the client itself interrupts the kernel at `timeout`
        wait = 0.5
        res = None
        while time.time() < deadline:
            p = self._try("exec", "-s", self.session, "-f", str(poll), "--timeout", "20", timeout=30, attempts=1)
            if p is not None and MARK in p.stdout:
                res = _parse(p.stdout)
                break
            if p is not None and "TESTBENCH_CLIENT_FAILED" in p.stdout:
                raise SessionError("the session's kernel client failed on the Colab VM:\n" + p.stdout[-2000:])
            time.sleep(wait)
            wait = min(wait * 1.5, 5)
        if res is None:
            return {"status": "timeout", "stdout": "", "stderr": "", "result": None, "displays": [], "images": [],
                    "seconds": round(timeout + 180, 1), "execution_count": None,
                    "error": {"ename": "SessionTimeout", "evalue": "no result came back from the Colab VM in time"}}
        local = []
        for remote in res.get("images", []):
            dst = Path(out_dir) / Path(remote).name
            self._colab("download", "-s", self.session, remote, str(dst))
            local.append(str(dst))
        res["images"] = local
        return res

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"/content/{path}"

    def ls(self, path: str) -> list[str]:
        p = self._colab("ls", "-s", self.session, self._abs(path or "."))
        return [ln for ln in p.stdout.splitlines() if ln.strip()]

    def get(self, remote: str, local: Path) -> None:
        self._colab("download", "-s", self.session, self._abs(remote), str(local), timeout=1800)

    def put(self, local: Path, remote: str) -> None:
        self._colab("upload", "-s", self.session, str(local), self._abs(remote), timeout=1800)

    def install(self, packages: list[str]) -> str:
        p = self._colab("install", "-s", self.session, *packages, timeout=1800, check=False)
        return (p.stdout + p.stderr)[-3000:]

    def sync(self) -> str:
        self._send_bundle(with_pkg=False)
        boot = self._script(COLAB_BOOT, START=False, SESSION=self.state["name"], SECRETS={})
        self._colab("exec", "-s", self.session, "-f", str(boot), "--timeout", "300", timeout=600)
        return "project files copied to /content (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        code = f"import subprocess\nr = subprocess.run({command!r}, shell=True, capture_output=True, text=True)\nprint(r.stdout + r.stderr)"
        script = self._script(code)
        return self._colab("exec", "-s", self.session, "-f", str(script), "--timeout", str(int(timeout)),
                           timeout=timeout + 120, check=False).stdout[-20000:]

    def stop(self) -> None:
        try:
            self._colab("stop", "-s", self.session, timeout=180, check=False)
        except Exception:
            pass


def backend_for(name: str):
    return {"local": LocalSession, "modal": ModalSession, "colab": ColabSession}[name]
