"""Run a plan's steps where the plan runs (this machine, a Modal container, a Colab VM) and record evidence.

Each step gets a folder under the run: log.txt (everything it printed, live), the executed notebook,
images it displayed, artifacts it made, and result.json. progress.jsonl gets one line per event, and
report.json is rewritten after every step, so a run that is cut off still says how far it got.

    python -m agent_testbench.executor --plan plan.json --project DIR --run-dir DIR
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, expect, notebook

SHIM = Path(__file__).parent / "colab_shim"
ARTIFACT_FILE_LIMIT = 200 * 1024 * 1024


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "step"


class GpuSampler:
    """Samples nvidia-smi every 10 s; tells how much of a step the GPU actually worked."""

    def __init__(self):
        self.samples: list[tuple[float, float]] = []
        self.name = None
        self._stop = threading.Event()
        self._thread = None
        if shutil.which("nvidia-smi"):
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def sample(self):
        if self._thread is None:
            return
        try:
            q = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=20).stdout
            line = q.strip().splitlines()[0].split(",")
            self.name = line[0].strip()
            self.samples.append((float(line[1]), float(line[2])))
        except Exception:
            pass

    def _loop(self):
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(10)

    def window(self, start: int) -> dict | None:
        s = self.samples[start:]
        if not s:
            return None
        return {"gpu": self.name, "busy_pct": round(100 * sum(1 for u, _ in s if u > 10) / len(s)),
                "mean_util_pct": round(sum(u for u, _ in s) / len(s)), "max_mem_mib": max(m for _, m in s)}

    def stop(self):
        self._stop.set()


def _event(run_dir: Path, **fields) -> None:
    with (run_dir / "progress.jsonl").open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 1), **fields}) + "\n")


def _copy_artifacts(patterns: list[str], workdir: Path, dest: Path) -> list[str]:
    kept = []
    for pattern in patterns:
        for hit in sorted(glob.glob(str(workdir / pattern), recursive=True)):
            src = Path(hit)
            if not src.is_file():
                continue
            rel = src.relative_to(workdir) if src.is_relative_to(workdir) else Path(src.name)
            if src.stat().st_size > ARTIFACT_FILE_LIMIT:
                kept.append(f"{rel} (not copied: {src.stat().st_size / 1e6:.0f} MB)")
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            kept.append(str(rel))
    return kept


def _run_command(cmd: str, *, workdir: Path, env: dict, deadline: float, log_path: Path) -> dict:
    with log_path.open("a") as log:
        proc = subprocess.Popen(cmd, shell=True, cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, executable="/bin/bash" if Path("/bin/bash").exists() else None)
        try:
            code = proc.wait(timeout=max(1, deadline - time.time()))
            return {"exit_code": code, "timed_out": False}
        except BaseException as exc:                              # timeout, or the run was cancelled
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=10)
                    break
                except Exception:
                    continue
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            return {"exit_code": None, "timed_out": True}


def run_step(step: dict, *, project: Path, workdir: Path, step_dir: Path, base_env: dict) -> dict:
    step_dir.mkdir(parents=True, exist_ok=True)
    log_path = step_dir / "log.txt"
    log_path.touch()
    began = time.time()
    deadline = began + float(step["max_minutes"]) * 60
    env = dict(base_env)
    env.update({k: str(v) for k, v in step["env"].items()})
    paths = [str((workdir / p).resolve()) for p in step["pythonpath"]]
    if step["kind"] == "notebook" and step.get("colab_shim", True):
        paths.insert(0, str(SHIM))
    env["PYTHONPATH"] = os.pathsep.join(paths + [p for p in [env.get("PYTHONPATH")] if p])
    env["TESTBENCH_DOWNLOADS_DIR"] = str(step_dir / "downloads")
    env["TESTBENCH_STEP_DIR"] = str(step_dir)
    env.setdefault("MPLBACKEND", "Agg")                         # child processes must not inherit an inline backend
    result: dict = {"name": step["name"], "kind": step["kind"], "status": "error", "error": None}
    missing = [s for s in step["secrets"] if s not in env]
    if missing:
        result["error"] = {"ename": "MissingSecret", "evalue": f"secrets not in the environment: {', '.join(missing)} "
                           "(locally: export them; on Modal: create a Modal secret with that name)"}
    else:
        try:
            if step["kind"] == "notebook":
                queue = step_dir / "upload_queue.json"
                queue.write_text(json.dumps([[str((workdir / f).resolve()) for f in group] for group in step["uploads"]]))
                env["TESTBENCH_UPLOAD_QUEUE"] = str(queue)
                env.pop("MPLBACKEND", None)                          # the kernel's own plots render inline
                nb = notebook.run(workdir / step["notebook"], params=step["params"], cells=step["cells"],
                                  deadline=deadline, env=env, workdir=workdir, out_dir=step_dir, live_log=log_path,
                                  shim=SHIM if step.get("colab_shim", True) else None)
                result.update(cells=nb["cells"], images=nb["images"], code_cells=nb["code_cells"], error=nb["error"])
                result["status"] = {"passed": "passed", "timeout": "timeout"}.get(nb["status"], "failed")
                left = json.loads(queue.read_text())
                if left:
                    result["note"] = f"{len(left)} queued upload(s) were never asked for"
            elif step["kind"] == "run":
                r = _run_command(step["run"], workdir=workdir, env=env, deadline=deadline, log_path=log_path)
                result.update(exit_code=r["exit_code"], status="timeout" if r["timed_out"] else "passed")
                if r["timed_out"]:
                    result["error"] = {"ename": "StepTimeout", "evalue": f"stopped after max_minutes={step['max_minutes']}"}
            elif step["kind"] == "web":
                from . import web
                w = web.run(step["web"], workdir=workdir, out_dir=step_dir, env=env, deadline=deadline, log_path=log_path)
                result["web"] = {k: v for k, v in w.items() if k != "text"}
                result["web"]["text_head"] = w.get("text", "")[:1500]
                result["status"] = "passed"
                if w.get("setup_error"):
                    result.update(status="error", error={"ename": "WebSetupError", "evalue": w["setup_error"]})
                result["_web_full"] = w
        except notebook.ParamError as exc:
            result.update(status="error", error={"ename": "ParamError", "evalue": str(exc)})
        except Exception as exc:                                   # the harness itself failed: say so plainly
            result.update(status="error", error={"ename": type(exc).__name__, "evalue": str(exc)[:2000]})
    text = log_path.read_text(errors="replace")
    result["stdout_tail"] = notebook._tail(text, 40, 5000)
    if result["status"] in ("passed", "failed") or step["kind"] == "run":
        checks = expect.check(step["expect"], workdir=workdir,
                              result={**result, "stdout": text, "web": result.pop("_web_full", None) or result.get("web")})
        result["expectations"] = checks
        if result["status"] == "passed" and not all(c["ok"] for c in checks):
            result["status"] = "failed"
    result.pop("_web_full", None)
    downloads = step_dir / "downloads"
    result["downloads"] = sorted(p.name for p in downloads.iterdir() if p.name != "downloads.json") if downloads.exists() else []
    result["artifacts"] = _copy_artifacts(step["artifacts"], workdir, step_dir / "artifacts")
    result["minutes"] = round((time.time() - began) / 60, 2)
    (step_dir / "result.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def execute_plan(plan: dict, *, project_dir: str | Path, run_dir: str | Path, workdir: str | Path | None = None,
                 meta: dict | None = None, on_progress=None) -> dict:
    """Run every step in order; stop at the first failure unless the step says continue_on_failure."""
    project, run_dir = Path(project_dir).resolve(), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(workdir).resolve() if workdir else project
    report = {"testbench_version": __version__, **(meta or {}), "status": "running", "started": time.time(),
              "steps": [], "workdir": str(workdir)}
    sampler = GpuSampler()
    base_env = {**os.environ, "TESTBENCH_RUN_DIR": str(run_dir), "TESTBENCH_PROJECT_DIR": str(project)}
    # steps see the Python that runs testbench first, so `python` and `testbench` in a step mean this environment
    base_env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), base_env.get("PATH", "")])
    stop_reason = None

    def save():
        (run_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
        if on_progress:
            on_progress()

    save()
    try:
        for i, step in enumerate(plan["steps"], 1):
            _event(run_dir, event="step_start", step=step["name"], index=i)
            save()
            mark = len(sampler.samples)
            result = run_step(step, project=project, workdir=workdir, step_dir=run_dir / "steps" / f"{i:02d}-{slug(step['name'])}",
                              base_env=base_env)
            sampler.sample()                                       # short steps still get one reading
            result["gpu"] = sampler.window(mark)
            result["dir"] = f"steps/{i:02d}-{slug(step['name'])}"
            report["steps"].append(result)
            _event(run_dir, event="step_end", step=step["name"], index=i, status=result["status"], minutes=result["minutes"])
            save()
            if result["status"] != "passed" and not step.get("continue_on_failure"):
                stop_reason = f"step {i} ({step['name']}) {result['status']}"
                break
    except KeyboardInterrupt:
        stop_reason = "interrupted"
        report["status"] = "cancelled"
    finally:
        sampler.stop()
        if report["status"] != "cancelled":
            ran = report["steps"]
            report["status"] = "passed" if ran and len(ran) == len(plan["steps"]) and all(s["status"] == "passed" for s in ran) \
                else "failed"
        report["stopped_because"] = stop_reason
        report["finished"] = time.time()
        report["minutes"] = round((report["finished"] - report["started"]) / 60, 2)
        report["skipped"] = [s["name"] for s in plan["steps"][len(report["steps"]):]]
        _event(run_dir, event="run_end", status=report["status"])
        save()
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True, help="plan JSON (one entry of testbench.yaml's plans, validated)")
    ap.add_argument("--project", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--workdir")
    ap.add_argument("--meta", default="{}")
    a = ap.parse_args(argv)
    report = execute_plan(json.loads(Path(a.plan).read_text()), project_dir=a.project, run_dir=a.run_dir,
                          workdir=a.workdir, meta=json.loads(a.meta))
    print(json.dumps({"status": report["status"], "minutes": report["minutes"]}))


if __name__ == "__main__":
    main()
