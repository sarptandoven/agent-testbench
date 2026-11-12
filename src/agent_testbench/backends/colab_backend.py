"""Run on a Colab VM through Google's Colab CLI (`uv tool install google-colab-cli`).

Per run: allocate a session (CPU or the plan's GPU), install the pinned basics, upload the project as one
bundle (in chunks - uploads over ~100 MB fail), execute the plan with a timeout equal to its budget, bring
the run folder back, and always stop the session, so compute units stop with it. The project lands in
/content, where Colab notebooks expect their files.

The hard stop is on the VM itself: right after the VM is assigned, kernelkit's stand-alone watchdog is copied
there and started. At the plan's time limit (its max_minutes plus the start-up the estimate allows) it interrupts
the plan and asks Colab to release the VM, the way google.colab.runtime.unassign() does - so the bound holds
even if this machine is asleep. (`colab exec --timeout` alone is not one: the CLI enforces it on this side.)

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

from .. import prices
from . import Backend, project_files, split_gpu, tool_check

# Colab bills compute units. These per-hour rates are approximate and vary by account and region; `colab usage`
# shows your actual rate - put it under `pricing: colab_units_per_hour:`. Pay-as-you-go is ~$10 per 100 units.
UNITS_PER_HOUR = {"CPU": 0.1, "T4": 1.8, "L4": 2.0, "G4": 4.0, "A100": 5.4, "H100": 9.0}
USD_PER_UNIT = 0.10
CHUNK = 45 * 1024 * 1024
PKG = Path(__file__).resolve().parents[1]          # the agent_testbench package, shipped with the project
WATCHDOG = PKG / "kernelkit" / "watchdog.py"       # standard library only: it runs before anything is installed

WATCHDOG_START = r'''
import os, subprocess, sys
os.makedirs(STATE, exist_ok=True)
argv = [sys.executable, "/content/.testbench_watchdog.py", "--state-dir", STATE, "--max-minutes", str(MAX_MINUTES),
        "--release", "colab"] + (["--idle-minutes", str(IDLE_MINUTES)] if IDLE_MINUTES else [])
subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL, stdout=open(STATE + "/watchdog.log", "a"),
                 stderr=subprocess.STDOUT)
print("TESTBENCH_WATCHDOG " + ("releases-vm" if os.environ.get("TBE_RUNTIME_ADDR") else "ends-kernel"), flush=True)
'''

REMOTE_STOP = {
    "releases-vm": "on the VM itself: it releases itself at the time limit, even if this machine is asleep",
    "ends-kernel": ("on the VM itself: everything testbench started there is ended at the time limit, even if this "
                    "machine is asleep; the VM could not be told to release itself (no TBE_RUNTIME_ADDR there), so "
                    "Colab takes it back once it sits idle (Colab does not publish how long that takes)"),
}


def start_watchdog(colab, session: str, state_dir: str, max_minutes: float, idle_minutes: float | None = None) -> str:
    """Copy the watchdog to a freshly assigned VM and start it there; returns 'releases-vm' or 'ends-kernel'.
    `colab` is a backend's own CLI caller (colab(*argv, timeout=...))."""
    colab("upload", "-s", session, str(WATCHDOG), "/content/.testbench_watchdog.py", timeout=300)
    script = Path(tempfile.mkdtemp()) / "testbench_watchdog_start.py"
    script.write_text(f"STATE = {state_dir!r}\nMAX_MINUTES = {float(max_minutes)!r}\n"
                      f"IDLE_MINUTES = {idle_minutes!r}\n" + WATCHDOG_START)
    p = colab("exec", "-s", session, "-f", str(script), "--timeout", "120", timeout=300)
    if "TESTBENCH_WATCHDOG" not in p.stdout:
        raise RuntimeError("could not start the hard stop on the Colab VM, so it was not used: " + p.stdout[-800:])
    return "releases-vm" if "TESTBENCH_WATCHDOG releases-vm" in p.stdout else "ends-kernel"

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
from agent_testbench.kernelkit.watchdog import note_interrupt
note_interrupt("/tmp/testbench/plan", os.getpid())                       # the VM's watchdog interrupts this at the limit
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


def extract_inside(tar, dest) -> None:
    """Unpack the run's results from the VM into dest and nowhere else. Pythons with tarfile's data filter use it;
    older ones (before 3.10.12) get the same rule here: plain files and folders only, every path inside dest."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    root = Path(dest).resolve()
    for m in tar.getmembers():
        target = (root / m.name).resolve()
        if not (m.isfile() or m.isdir()) or (target != root and root not in target.parents):
            raise RuntimeError(f"the results from the Colab VM hold {m.name!r}, which would land outside the run "
                               "folder or is not a plain file; nothing was unpacked from it")
    tar.extractall(root)

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
            limit = sum(s["max_minutes"] for s in self.plan["steps"]) + backend.startup_minutes
            stop = start_watchdog(self._colab, self.session, "/tmp/testbench/plan", limit)
            runs.write_state(self.run_dir, remote_stop=REMOTE_STOP[stop])
            print(f"[testbench] hard stop after {limit:g} min {REMOTE_STOP[stop]}", flush=True)
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
            local = Path(tempfile.mkdtemp()) / "result.tar.gz"
            try:
                self._colab("exec", "-s", self.session, "-f", str(pack), "--timeout", "300", timeout=600)
                self._colab("download", "-s", self.session, "/content/.testbench_result.tar.gz", str(local), timeout=1800)
            except RuntimeError as exc:
                raise RuntimeError(f"could not bring the run back from the Colab VM ({exc}). If the plan ran to its "
                                   "time limit, the VM's own hard stop may have released it already.") from None
            with tarfile.open(local) as t:
                extract_inside(t, self.run_dir)
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
        return backend.rate_per_minute(self.cfg, self.plan) * minutes


class Colab(Backend):
    """Plans and sessions on a Colab VM through Google's Colab CLI; billed in your Colab compute units."""
    name = "colab"
    built_in_gpus = ("T4", "L4", "G4", "A100", "H100")
    startup_minutes = 10.0     # assigning, installing and uploading, plus bringing the evidence back before the
                               # VM's own hard stop (at the time limit plus this) - so the estimate bounds it
    hard_stop = "kernelkit's watchdog on the VM interrupts the work and releases the VM at the time limit"
    config_defaults = {"colab": {"pip": [], "requirements": None}}

    @property
    def gpus(self) -> tuple[str, ...]:
        fetched = prices.raised({}, (prices.cached().get("colab") or {}).get("units_per_hour"))   # priced ones only
        return self.built_in_gpus + tuple(g for g in fetched if g not in self.built_in_gpus and g != "CPU")

    def validate(self, cfg: dict) -> None:
        self._rates(cfg)                                 # pricing: overrides are numbers where one is meant

    def _rates(self, cfg: dict) -> tuple[dict, float]:
        own = cfg.get("pricing") or {}
        cat = prices.cached().get("colab") or {}
        units = {**prices.raised(UNITS_PER_HOUR, cat.get("units_per_hour")), **prices.own_table(own, "colab_units_per_hour")}
        return units, prices.own_price(own, "colab_usd_per_unit", prices.raised_value(USD_PER_UNIT, cat.get("usd_per_unit")))

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        units, usd = self._rates(cfg)
        gpu, _ = split_gpu(hardware.get("gpu"))
        return units.get(gpu or "CPU", 0.0) * usd / 60

    def prices(self, cfg: dict) -> list[str]:
        units, usd = self._rates(cfg)
        fetched = (prices.cached().get("colab") or {}).get("units_per_hour")
        own = (cfg.get("pricing") or {}).get("colab_units_per_hour")
        return [f"  {g:<6} {u:g} units/h  ${u * usd:.2f}/h  ({prices.source(g, UNITS_PER_HOUR, fetched, own)})"
                for g, u in sorted(units.items(), key=lambda kv: kv[1])] + \
            [f"  ${usd:.2f} per compute unit; approximate - `colab usage` shows your account's rates"]

    def plan_runner(self):
        return ColabBackend

    def session_class(self):
        from ..session_backends import ColabSession
        return ColabSession

    def cancel_remote(self, state: dict) -> str:
        return stop_session(state["colab_session"], state.get("colab_cli_args", [])) if state.get("colab_session") else ""

    def doctor(self):
        return [tool_check("colab CLI (Colab plans and sessions)", "colab", "uv tool install google-colab-cli")]


backend = Colab()
