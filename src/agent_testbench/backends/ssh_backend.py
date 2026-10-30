"""Run on any machine you can reach with ssh: a GPU box under your desk, a university server, a rented instance.

    ssh:
      host: gpu-box                # a Host from ~/.ssh/config, or user@host
      usd_per_hour: 0.0            # what the machine costs you while it works (0: your own); prices the worst case
      python: python3              # its Python (3.10 or newer)
      workdir: ~/.testbench-remote # where projects, sessions and runs go there
      setup: venv                  # venv: a virtualenv per project there, with testbench's basics and `pip:`; none:
      pip: [torch==2.5.1]          #   use `python` as it is
      command: [ssh]               # how to reach it (e.g. [ssh, -p, "2222"]); BatchMode, so keys, never passwords

Nothing is installed on the machine beyond that virtualenv. The project and this package are copied there for
each session and run. The hard stop is on the machine: kernelkit's watchdog (standard library only) starts first,
with the machine's own Python, and ends the work at the time limit - and a session after it sits idle - even if
this machine is asleep. Secrets go over ssh's stdin, never onto a command line. testbench bounds the work, not
the machine: a rented instance billed while it is on stays on.
"""
from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from ..config import ConfigError
from . import Backend, tool_check

PKG = Path(__file__).resolve().parents[1]
WATCHDOG = PKG / "kernelkit" / "watchdog.py"
BASE_PIP = ["nbformat>=5.9", "nbclient>=0.10", "ipykernel>=6.29", "jupyter_client>=8", "pyyaml>=6"]
MARK = "TESTBENCH_RESULT "
IDLE_SLACK = 10           # the machine's watchdog waits this much longer than the supervisor here (it is the backstop)


def _control() -> list[str]:
    """Reuse one ssh connection for the many short commands a session makes (a private folder: socket paths are short)."""
    d = Path(tempfile.gettempdir()) / f"testbench-ssh-{os.getuid()}"
    d.mkdir(mode=0o700, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={d}/%C", "-o", "ControlPersist=120"]


# Started with the machine's Python (no package needed): runs argv detached, in its own session, with extra
# environment (secrets) read from stdin, and prints its pid.
SPAWN = (
    "import json, os, subprocess, sys\n"
    "r = json.load(sys.stdin)\n"
    "os.makedirs(os.path.dirname(r['log']), exist_ok=True)\n"
    "p = subprocess.Popen(r['argv'], cwd=r.get('cwd'), env={**os.environ, **r.get('env', {})}, start_new_session=True,\n"
    "                     stdin=subprocess.DEVNULL, stdout=open(r['log'], 'a'), stderr=subprocess.STDOUT)\n"
    "print('TESTBENCH_PID', p.pid, flush=True)\n"
)


class Remote:
    """Commands on the machine over ssh, each through `bash -lc` so the machine's login PATH applies."""

    def __init__(self, command: list[str], host: str, log: Path | None = None):
        self.command, self.host, self.log = list(command), host, log

    def run(self, script: str, *, data: bytes | None = None, timeout: float = 600, check: bool = True,
            binary: bool = False):
        argv = [*self.command, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=30",
                *_control(), self.host, "bash -lc " + shlex.quote(script)]
        began = time.time()
        try:
            p = subprocess.run(argv, input=data, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ssh {self.host}: no answer within {timeout:.0f} s") from None
        err = p.stderr.decode(errors="replace")
        if self.log:
            with self.log.open("a") as f:
                f.write(f"$ ssh {self.host} {script.splitlines()[0][:160]} ... ({time.time() - began:.1f} s, "
                        f"exit {p.returncode})\n{err[-1500:]}")
        if p.returncode == 255 and (check or "Could not resolve" in err or "Connection" in err or "Permission denied" in err):
            raise RuntimeError(f"ssh could not reach {self.host}: {err.strip()[-400:] or 'no reason given'} - check "
                               "`ssh " + self.host + " true` works without a password (keys, ~/.ssh/config)")
        if check and p.returncode != 0:
            raise RuntimeError(f"on {self.host}: `{script.splitlines()[0][:120]}` exited {p.returncode}: "
                               f"{(err or p.stdout.decode(errors='replace')).strip()[-600:]}")
        return p if binary else subprocess.CompletedProcess(p.args, p.returncode, p.stdout.decode(errors="replace"), err)

    def spawn(self, python: str, argv: list[str], log: str, cwd: str | None = None, env: dict | None = None) -> int:
        req = json.dumps({"argv": argv, "log": log, "cwd": cwd, "env": env or {}}).encode()
        out = self.run(f"{shlex.quote(python)} -c {shlex.quote(SPAWN)}", data=req, timeout=120).stdout
        pid = next((ln.split()[1] for ln in out.splitlines() if ln.startswith("TESTBENCH_PID")), None)
        if not pid:
            raise RuntimeError(f"could not start {argv[:3]} on {self.host}: {out[-400:]}")
        return int(pid)

    def kill_group(self, pid: int | None, wait: float = 10) -> None:
        if pid:
            self.run(f"kill -TERM -{pid} 2>/dev/null; for i in $(seq {int(wait * 4)}); do kill -0 {pid} 2>/dev/null "
                     f"|| exit 0; sleep 0.25; done; kill -KILL -{pid} 2>/dev/null; true", timeout=wait + 60, check=False)

    def put_bytes(self, data: bytes, path: str) -> None:
        q = shlex.quote(path)
        self.run(f"mkdir -p \"$(dirname {q})\" && cat > {q}", data=data, timeout=1800)

    def get_bytes(self, path: str) -> bytes:
        return self.run(f"cat {shlex.quote(path)}", timeout=1800, binary=True).stdout

    def untar(self, data: bytes, into: str) -> None:
        self.run(f"mkdir -p {shlex.quote(into)} && tar xzf - -C {shlex.quote(into)}", data=data, timeout=1800)


def _setup(cfg: dict, remote: Remote, base: str) -> str:
    """The machine's Python for this project: a virtualenv with testbench's basics (once), or `python` as it is."""
    s = cfg["ssh"]
    if s["setup"] == "none":
        return s["python"]
    venv, reqs = f"{base}/venv", sorted(BASE_PIP + list(s.get("pip") or []))
    want = " ".join(reqs)
    q = shlex.quote
    remote.run(f"test -x {q(venv)}/bin/python || {q(s['python'])} -m venv {q(venv)}; "
               f"if [ \"$(cat {q(venv)}/.testbench-reqs 2>/dev/null)\" != {q(want)} ]; then "
               f"{q(venv)}/bin/python -m pip install -q {' '.join(q(r) for r in reqs)} && printf %s {q(want)} > "
               f"{q(venv)}/.testbench-reqs; fi", timeout=3600)
    return f"{venv}/bin/python"


def _base(cfg: dict, remote: Remote) -> str:
    """The project's folder on the machine, as an absolute path."""
    s = cfg["ssh"]
    out = remote.run(f"{shlex.quote(s['python'])} -c 'import os, sys; print(os.path.expanduser(sys.argv[1]))' "
                     f"{shlex.quote(s['workdir'])}", timeout=120).stdout.strip().splitlines()
    if not out:
        raise RuntimeError(f"{s['python']} on {s['host']} did not answer; set ssh.python to its Python 3")
    return f"{out[-1].rstrip('/')}/{cfg['project']}"


def _bundle(root: Path, exclude: list[str], with_pkg: bool) -> bytes:
    from ..session_backends import bundle_bytes
    return bundle_bytes(root, exclude, with_pkg)


def _secrets(names: list[str]) -> dict:
    missing = [n for n in names if n not in os.environ]
    if missing:
        raise RuntimeError(f"secrets not in this environment: {', '.join(missing)} - export them first")
    return {n: os.environ[n] for n in names}


def _start_watchdog(remote: Remote, python: str, base: str, state_dir: str, max_minutes: float,
                    idle_minutes: float | None = None) -> int:
    remote.put_bytes(WATCHDOG.read_bytes(), f"{base}/watchdog.py")
    argv = [python, f"{base}/watchdog.py", "--state-dir", state_dir, "--max-minutes", str(max_minutes)]
    return remote.spawn(python, argv + (["--idle-minutes", str(idle_minutes)] if idle_minutes else []),
                        f"{state_dir}/watchdog.log")


REMOTE_STOP = "on the machine itself: its watchdog ends the work at the time limit, even if this machine is asleep"


class SSHRunner:
    """A plan on the machine: the executor runs there, detached; progress comes back every few seconds."""

    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta
        self.remote = Remote(cfg["ssh"]["command"], cfg["ssh"]["host"], self.run_dir / "ssh.log")
        self.started = self.pid = self.dir = self.dog = None

    def run(self):
        from .. import report as R, runs
        s = self.cfg["ssh"]
        base = _base(self.cfg, self.remote)
        d = self.dir = f"{base}/runs/{self.run_dir.name}"
        self.started = time.time()
        limit = sum(st["max_minutes"] for st in self.plan["steps"]) + backend.startup_minutes
        dog = self.dog = _start_watchdog(self.remote, s["python"], d, f"{d}/watchdog", limit)
        runs.write_state(self.run_dir, ssh_command=s["command"], ssh_host=s["host"], ssh_dir=d, ssh_watchdog=dog,
                         remote_stop=REMOTE_STOP)
        print(f"[testbench] hard stop after {limit:g} min {REMOTE_STOP}", flush=True)
        python = _setup(self.cfg, self.remote, base)
        self.remote.untar(_bundle(self.root, self.cfg["exclude"], with_pkg=True), d)
        self.remote.put_bytes(json.dumps(self.plan).encode(), f"{d}/plan.json")
        secrets = _secrets(sorted({n for st in self.plan["steps"] for n in st["secrets"]}))
        self.pid = self.remote.spawn(python, [python, "-m", "agent_testbench.executor", "--plan", f"{d}/plan.json",
                                              "--project", f"{d}/project", "--run-dir", f"{d}/run", "--workdir",
                                              f"{d}/project", "--meta", json.dumps(self.meta)],
                                     f"{d}/executor.log", cwd=f"{d}/project",
                                     env={"PYTHONPATH": f"{d}/pkg", "TESTBENCH_CACHE": f"{base}/cache", **secrets})
        self.remote.run(f"PYTHONPATH={shlex.quote(d + '/pkg')} {shlex.quote(python)} -c 'import sys; from "
                        "agent_testbench.kernelkit.watchdog import note_interrupt; note_interrupt(sys.argv[1], "
                        f"int(sys.argv[2]))' {shlex.quote(d + '/watchdog')} {self.pid}", timeout=60)
        runs.write_state(self.run_dir, ssh_pid=self.pid)
        print(f"[testbench] running the plan on {s['host']} (up to {limit:g} min)", flush=True)
        while self.remote.run(f"kill -0 {self.pid} 2>/dev/null", check=False, timeout=60).returncode == 0:
            self._mirror(light=True)
            time.sleep(float(os.environ.get("TESTBENCH_SSH_POLL_SECONDS", 5)))
        self._mirror(light=False)
        self._clean_up()
        rep = R.load(self.run_dir)
        if not rep:
            log = self.remote.log.read_text(errors="replace")[-2000:] if self.remote.log.exists() else ""
            rep = {**self.meta, "steps": [], "started": self.started, "status": "error",
                   "stopped_because": f"the plan never started on {s['host']}",
                   "launch_error": f"The machine could not start the plan. ssh log:\n\n```\n{log}\n```"}
        return rep, self.cost(rep)

    def _mirror(self, light: bool) -> None:
        """Bring the run folder back: progress and logs while it runs, everything at the end."""
        q = shlex.quote(f"{self.dir}/run")
        pick = ("ls -d progress.jsonl report.json steps/*/log.txt steps/*/result.json 2>/dev/null | tar czf - -T -"
                if light else "tar czf - .")
        p = self.remote.run(f"cd {q} 2>/dev/null && {pick}", check=False, binary=True, timeout=1800)
        if p.returncode != 0 or not p.stdout:
            return
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as t:
            t.extractall(self.run_dir, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))

    def _clean_up(self) -> None:
        """The run is over: its watchdog goes, and so does its folder on the machine (the evidence is here now)."""
        self.remote.kill_group(self.dog, wait=1)
        if self.dir:
            self.remote.run(f"rm -rf {shlex.quote(self.dir)}", check=False, timeout=300)

    def cancel(self) -> str:
        if not self.dir:
            return ""
        if self.pid:
            self.remote.run(f"kill -INT {self.pid} 2>/dev/null; for i in $(seq 120); do kill -0 {self.pid} 2>/dev/null "
                            "|| exit 0; sleep 0.25; done", check=False, timeout=90)    # it writes its report, then exits
            self._mirror(light=False)
            self.remote.kill_group(self.pid)
        self._clean_up()
        return f"stopped the plan on {self.cfg['ssh']['host']}"

    def cost(self, rep: dict) -> float:
        minutes = (time.time() - self.started) / 60 if self.started else 0.0
        return backend.rate_per_minute(self.cfg, self.plan) * minutes


class SSHSession:
    """A kernel on the machine, in a copy of the project; the same launcher and client as everywhere else."""

    def __init__(self, root: Path, cfg: dict, sdir: Path, state: dict):
        self.root, self.cfg, self.sdir, self.state = Path(root), cfg, Path(sdir), state
        self.remote = Remote(cfg["ssh"]["command"], cfg["ssh"]["host"], self.sdir / "ssh.log")
        h = state.get("handles") or {}
        self.dir, self.python = h.get("dir"), h.get("python")

    def _p(self, rel: str) -> str:
        return shlex.quote(f"{self.dir}/{rel}")

    def start(self) -> dict:
        s = self.cfg["ssh"]
        base = _base(self.cfg, self.remote)
        self.dir = f"{base}/sessions/{self.state['name']}"
        self.remote.run(f"rm -rf {shlex.quote(self.dir)} && mkdir -p {shlex.quote(self.dir)}", timeout=120)
        handles = {"host": s["host"], "dir": self.dir, "workdir": f"{self.dir}/project", "remote_stop": REMOTE_STOP}
        self.state["handles"] = handles
        handles["watchdog_pid"] = _start_watchdog(self.remote, s["python"], self.dir, f"{self.dir}/kernel",
                                                  self.state["max_minutes"] + backend.startup_minutes,
                                                  self.state["idle_minutes"] + IDLE_SLACK)
        self.python = handles["python"] = _setup(self.cfg, self.remote, base)
        self.remote.untar(_bundle(self.root, self.cfg["exclude"], with_pkg=True), self.dir)
        env = {"PYTHONPATH": f"{self.dir}/pkg", "TESTBENCH_SESSION": self.state["name"],
               "TESTBENCH_CACHE": f"{base}/cache", **_secrets(self.state.get("secrets", []))}
        handles["launcher_pid"] = self.remote.spawn(
            self.python, [self.python, "-m", "agent_testbench.kernelkit.launcher", "--workdir", f"{self.dir}/project",
                          "--state-dir", f"{self.dir}/kernel", "--shim"], f"{self.dir}/kernel.log",
            cwd=f"{self.dir}/project", env=env)
        for _ in range(240):
            if self.remote.run(f"test -f {self._p('kernel/ready')}", check=False, timeout=60).returncode == 0:
                return handles
            time.sleep(0.5)
        log = self.remote.run(f"tail -c 1500 {self._p('kernel.log')}", check=False).stdout
        raise RuntimeError(f"the kernel did not start on {s['host']}: {log}")

    def alive(self) -> bool:
        pid = (self.state.get("handles") or {}).get("launcher_pid")
        try:
            return bool(pid) and self.remote.run(f"kill -0 {pid} && test -f {self._p('kernel/ready')}", check=False,
                                                 timeout=60).returncode == 0
        except RuntimeError:
            return False

    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict:
        from ..session_backends import _parse
        req = f"{self.dir}/out/{prefix}.json"
        self.remote.put_bytes(json.dumps({"code": code, "timeout": timeout, "prefix": prefix}).encode(), req)
        p = self.remote.run(f"cd {self._p('project')} && PYTHONPATH={self._p('pkg')} {shlex.quote(self.python)} -m "
                            f"agent_testbench.kernelkit.client --connection {self._p('kernel/kernel.json')} --request "
                            f"{shlex.quote(req)} --out-dir {self._p('out/' + prefix)}", timeout=timeout + 180, check=False)
        res = _parse(p.stdout)
        local = []
        for remote_path in res.get("images", []):
            dst = Path(out_dir) / Path(remote_path).name
            dst.write_bytes(self.remote.get_bytes(remote_path))
            local.append(str(dst))
        res["images"] = local
        return res

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.dir}/project/{path}"

    def ls(self, path: str) -> list[str]:
        out = self.remote.run(f"ls -1p {shlex.quote(self._abs(path or '.'))}").stdout
        return [ln for ln in out.splitlines() if ln.strip()]

    def get(self, remote: str, local: Path) -> None:
        Path(local).write_bytes(self.remote.get_bytes(self._abs(remote)))

    def put(self, local: Path, remote: str) -> None:
        self.remote.put_bytes(Path(local).read_bytes(), self._abs(remote))

    def install(self, packages: list[str]) -> str:
        p = self.remote.run(f"{shlex.quote(self.python)} -m pip install {' '.join(shlex.quote(x) for x in packages)} 2>&1",
                            timeout=1800, check=False)
        return p.stdout[-3000:] + ("" if p.returncode == 0 else f"\n(pip exited {p.returncode})")

    def sync(self) -> str:
        self.remote.untar(_bundle(self.root, self.cfg["exclude"], with_pkg=False), self.dir)
        self.remote.run(f"date +%s > {self._p('kernel/last_used')}", check=False)
        return f"project files copied to {self.dir}/project on {self.cfg['ssh']['host']} (variables in the kernel are kept)"

    def shell(self, command: str, timeout: float) -> str:
        k = self._p("kernel")
        p = self.remote.run(f"echo $(( $(date +%s) + {int(timeout) + 120} )) > {k}/busy; cd {self._p('project')} && "
                            f"{{ {command}\n}} 2>&1; rc=$?; rm -f {k}/busy; date +%s > {k}/last_used; exit $rc",
                            timeout=timeout + 60, check=False)
        return p.stdout[-20000:]

    def stop(self) -> None:
        h = self.state.get("handles") or {}
        if not self.dir:
            return
        try:
            self.remote.kill_group(h.get("launcher_pid"))                 # the launcher shuts its kernel down
            self.remote.kill_group(h.get("watchdog_pid"), wait=1)
            self.remote.run(f"rm -rf {shlex.quote(self.dir)}", check=False, timeout=300)
        except RuntimeError:
            pass


class SSH(Backend):
    name = "ssh"
    startup_minutes = 2.0            # copying the project and checking the virtualenv
    config_defaults = {"ssh": {"host": None, "usd_per_hour": 0.0, "python": "python3", "workdir": "~/.testbench-remote",
                               "setup": "venv", "pip": [], "command": ["ssh"]}}

    def check_hardware(self, where: str, gpu) -> None:
        """The machine has the GPU it has; `gpu:` on an ssh plan is a label (the report shows what it really used)."""

    def validate(self, cfg: dict) -> None:
        s = cfg["ssh"]
        if not s.get("host"):
            raise ConfigError("`ssh.host` is needed for the ssh backend: a Host from ~/.ssh/config, or user@host, that "
                              "`ssh HOST true` reaches without a password")
        if not isinstance(s.get("usd_per_hour"), (int, float)) or s["usd_per_hour"] < 0:
            raise ConfigError("`ssh.usd_per_hour` is what the machine costs you per hour while it works (0 for your own)")
        if s.get("setup") not in ("venv", "none"):
            raise ConfigError("`ssh.setup` is venv (a virtualenv per project on the machine) or none (use ssh.python)")
        cmd = s.get("command")
        if isinstance(cmd, str):
            s["command"] = cmd = shlex.split(cmd)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            raise ConfigError("`ssh.command` is how to reach the machine, as a list: [ssh] or [ssh, -p, \"2222\"]")
        if not isinstance(s.get("pip") or [], list):
            raise ConfigError("`ssh.pip` is a list of packages for the machine's virtualenv (pin them)")

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        return float(cfg["ssh"]["usd_per_hour"]) / 60

    def prices(self, cfg: dict) -> list[str]:
        return [f"  ${float(cfg['ssh']['usd_per_hour']):.2f}/h for {cfg['ssh'].get('host') or '(no ssh.host set)'} "
                "(testbench.yaml ssh.usd_per_hour)"]

    def plan_runner(self):
        return SSHRunner

    def session_class(self):
        return SSHSession

    def cancel_remote(self, state: dict) -> str:
        if not state.get("ssh_pid"):
            return ""
        remote = Remote(state["ssh_command"], state["ssh_host"])
        try:
            remote.run(f"kill -INT {state['ssh_pid']} 2>/dev/null", check=False, timeout=60)
            remote.kill_group(state["ssh_pid"])
            remote.kill_group(state.get("ssh_watchdog"), wait=1)
            if state.get("ssh_dir"):
                remote.run(f"rm -rf {shlex.quote(state['ssh_dir'])}", check=False, timeout=300)
            return f"stopped the plan on {state['ssh_host']}"
        except RuntimeError as exc:
            return f"could not reach {state['ssh_host']} to stop the plan: {exc}"

    def doctor(self):
        return [tool_check("ssh client (ssh plans and sessions)", "ssh", "install OpenSSH")]


backend = SSH()
