"""Run on a Colab VM through Google's Colab CLI (`uv tool install google-colab-cli`).

Per run: allocate a session (CPU or the plan's GPU), install the pinned basics, upload the project as one
bundle (in chunks - uploads over ~100 MB fail), execute the plan with a timeout equal to its budget, bring
the run folder back, and always stop the session, so compute units stop with it. The project lands in
/content, where Colab notebooks expect their files.

Colab notes: VMs can be reclaimed after a couple of hours even while busy, so keep Colab plans short and
use Modal for long jobs; set `colab: {auth: adc}` in testbench.yaml if you log in with Application Default
Credentials.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from .. import budget
from . import project_files

CHUNK = 45 * 1024 * 1024
PKG = Path(__file__).resolve().parents[1]          # the agent_testbench package, shipped with the project

BOOTSTRAP = r'''
import glob, json, os, subprocess, sys, tarfile
parts = sorted(glob.glob("/content/.testbench_bundle.part*"))
with open("/content/.testbench_bundle.tar.gz", "wb") as out:
    for p in parts:
        with open(p, "rb") as f:
            out.write(f.read())
        os.remove(p)
safe = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
with tarfile.open("/content/.testbench_bundle.tar.gz") as t:
    for m in t.getmembers():
        if m.name.startswith("project/"):
            m.name = m.name[len("project/"):]
            if m.name:
                t.extract(m, "/content", **safe)
        elif m.name.startswith("pkg/"):
            m.name = m.name[len("pkg/"):]
            t.extract(m, "/content/.testbench_pkg", **safe)
sys.path.insert(0, "/content/.testbench_pkg")
os.environ["TESTBENCH_CACHE"] = "/content/.testbench_cache"
os.makedirs("/content/.testbench_cache", exist_ok=True)
from agent_testbench.executor import execute_plan
run_dir = "/content/.testbench_run/" + RUN_ID
report = execute_plan(PLAN, project_dir="/content", run_dir=run_dir, workdir="/content", meta=META)
print("TESTBENCH_DONE " + json.dumps({"status": report["status"], "minutes": report.get("minutes")}), flush=True)
'''

PACK = r'''
import os, tarfile
d = "/content/.testbench_run/" + RUN_ID
with tarfile.open("/content/.testbench_result.tar.gz", "w:gz") as t:
    if os.path.isdir(d):
        t.add(d, arcname=".")
print("TESTBENCH_PACKED", os.path.getsize("/content/.testbench_result.tar.gz"), flush=True)
'''


def cli_args(cfg: dict) -> list[str]:
    c = cfg.get("colab") or {}
    return [c.get("cli", "colab")] + (["--auth", c["auth"]] if c.get("auth") else [])


def stop_session(session: str, args: list[str]) -> str:
    try:
        subprocess.run([*(args or ["colab"]), "stop", "-s", session], capture_output=True, text=True, timeout=120)
        return f"Colab session {session} stopped"
    except Exception as exc:
        return f"could not stop Colab session {session}: {exc} - stop it with `colab stop -s {session}`"


class ColabBackend:
    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta
        self.args = cli_args(cfg)
        self.session = ("testbench-" + run_dir.name)[:60]
        self.started = None

    def _colab(self, *argv, timeout: float = 900, check: bool = True, input: str | None = None):
        cmd = [*self.args, *argv]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input)
        with (self.run_dir / "colab.log").open("a") as log:
            log.write(f"$ {' '.join(cmd[:4])} ...\n{proc.stdout[-4000:]}{proc.stderr[-4000:]}\n")
        if check and proc.returncode != 0:
            raise RuntimeError(f"`colab {argv[0]}` failed (exit {proc.returncode}): "
                               f"{(proc.stderr or proc.stdout).strip().splitlines()[-1:] or ''}. See colab.log in the run folder.")
        return proc

    def _bundle(self) -> Path:
        path = Path(tempfile.mkdtemp()) / "bundle.tar.gz"
        with tarfile.open(path, "w:gz") as t:
            for f in project_files(self.root, self.cfg["exclude"]):
                t.add(f, arcname="project/" + str(f.relative_to(self.root)))
            t.add(PKG, arcname="pkg/agent_testbench", filter=lambda m: None if "__pycache__" in m.name else m)
        return path

    def run(self):
        from .. import runs, report as R
        if not shutil.which(self.args[0]):
            raise RuntimeError("The Colab CLI is not installed: `uv tool install google-colab-cli`, then log in once "
                               "with `colab new` (or set colab.auth: adc in testbench.yaml).")
        gpu = (self.plan.get("gpu") or "").upper()
        runs.write_state(self.run_dir, colab_session=self.session, colab_cli_args=self.args)
        print(f"[testbench] allocating Colab session {self.session} ({gpu or 'CPU'})", flush=True)
        self._colab("new", "-s", self.session, *(["--gpu", gpu] if gpu else []), timeout=900)
        self.started = time.time()
        try:
            pkgs = ["nbformat>=5.9", "nbclient>=0.10", "ipykernel>=6.29", "pyyaml>=6", *self.cfg["colab"].get("pip", [])]
            self._colab("install", "-s", self.session, *pkgs, timeout=1200)
            if self.cfg["colab"].get("requirements"):
                self._colab("install", "-s", self.session, "-r", str(self.root / self.cfg["colab"]["requirements"]), timeout=1800)
            bundle = self._bundle()
            data = bundle.read_bytes()
            for i in range(0, max(1, len(data)), CHUNK):
                part = bundle.with_name(f"part{i // CHUNK:03d}")
                part.write_bytes(data[i:i + CHUNK])
                self._colab("upload", "-s", self.session, str(part), f"/content/.testbench_bundle.part{i // CHUNK:03d}", timeout=900)
            minutes = sum(s["max_minutes"] for s in self.plan["steps"])
            script = (f"RUN_ID = {self.run_dir.name!r}\nPLAN = json.loads({json.dumps(self.plan)!r})\n"
                      f"META = json.loads({json.dumps(self.meta)!r})\n")
            boot = Path(tempfile.mkdtemp()) / "testbench_boot.py"
            boot.write_text("import json\n" + script + BOOTSTRAP)
            print(f"[testbench] running the plan on Colab (up to {minutes:.0f} min)", flush=True)
            proc = self._colab("exec", "-s", self.session, "-f", str(boot), "--timeout", str(int(minutes * 60 + 120)),
                               timeout=minutes * 60 + 600, check=False)
            (self.run_dir / "colab_exec.log").write_text(proc.stdout + proc.stderr)
            pack = Path(tempfile.mkdtemp()) / "testbench_pack.py"
            pack.write_text(f"RUN_ID = {self.run_dir.name!r}\n" + PACK)
            self._colab("exec", "-s", self.session, "-f", str(pack), "--timeout", "300", timeout=600)
            local = Path(tempfile.mkdtemp()) / "result.tar.gz"
            self._colab("download", "-s", self.session, "/content/.testbench_result.tar.gz", str(local), timeout=1800)
            with tarfile.open(local) as t:
                t.extractall(self.run_dir, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        finally:
            print(f"[testbench] {stop_session(self.session, self.args)}", flush=True)
        rep = R.load(self.run_dir)
        if not rep:
            log = (self.run_dir / "colab_exec.log")
            tail = "\n".join(log.read_text(errors="replace").strip().splitlines()[-20:]) if log.exists() else ""
            rep = {**self.meta, "steps": [], "started": self.started, "status": "error",
                   "stopped_because": "the plan never started on the Colab VM",
                   "launch_error": "The VM could not start the plan. Its last output:\n\n```\n" + tail + "\n```"}
        return rep, self.cost(rep)

    def cancel(self) -> str:
        return stop_session(self.session, self.args)

    def cost(self, rep: dict) -> float:
        minutes = (time.time() - self.started) / 60 if self.started else 0.0
        return budget.rate_per_minute(self.cfg, self.plan) * minutes
