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
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__, expect     # core modules only: notebook, mocks and web load for the steps that use them

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
                "mean_util_pct": round(sum(u for u, _ in s) / len(s)), "max_mem_mib": max(m for _, m in s),
                "readings": len(s)}

    def reading(self, start: int) -> dict | None:
        """The window since `start`, sampled once more first, so short steps still get one reading."""
        self.sample()
        return self.window(start)

    def stop(self):
        self._stop.set()


def _event(run_dir: Path, **fields) -> None:
    with (run_dir / "progress.jsonl").open("a") as f:
        f.write(json.dumps({"t": round(time.time(), 1), **fields}) + "\n")


def _copy_artifacts(patterns: list[str], workdir: Path, dest: Path) -> list[str]:
    kept = []
    for pattern in patterns:
        for hit in sorted(glob.glob(str(workdir / pattern), recursive=True)):
            src = Path(os.path.abspath(hit))           # "../x" is outside the workdir: kept by its name, in dest
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
    from .procs import group_kwargs, shell, stop_and_wait
    with log_path.open("a") as log:
        proc = subprocess.Popen(cwd=workdir, env=env, stdout=log, stderr=subprocess.STDOUT, **shell(cmd), **group_kwargs())
        try:
            code = proc.wait(timeout=max(1, deadline - time.time()))
            return {"exit_code": code, "timed_out": False}
        except BaseException as exc:                              # timeout, or the run was cancelled
            stop_and_wait(proc.pid, grace=10, proc=proc)
            if not isinstance(exc, subprocess.TimeoutExpired):
                raise
            return {"exit_code": None, "timed_out": True}


def run_step(step: dict, *, project: Path, workdir: Path, step_dir: Path, base_env: dict, gpu=None,
             run_started: float | None = None, versions: dict | None = None) -> dict:
    """gpu: called once the step's work is done, returns its GPU window (gpu: expectations check it). versions: the
    interpreter versions already asked for in this run (step_runtime), so each is asked once."""
    versions = {} if versions is None else versions
    step_dir.mkdir(parents=True, exist_ok=True)
    log_path = step_dir / "log.txt"
    log_path.touch()
    began = time.time()
    deadline = began + float(step["max_minutes"]) * 60
    env = dict(base_env)
    env.update({k: str(v) for k, v in step["env"].items()})
    paths = [str((workdir / p).resolve()) for p in step["pythonpath"]]
    mocked = step.get("mocks") or {}
    if mocked:                                                  # the mock packages (built in or plug-ins) win the import
        from . import mocks
        paths = mocks.client_paths(mocked) + paths
        env.update(mocks.client_env(mocked, workdir))
        env.pop("TESTBENCH_MOCK_CALLS", None)                    # the log goes to the step's folder, where it is read
    if step["kind"] == "notebook" and step.get("colab_shim", True):
        paths.insert(0, str(SHIM))
    env["PYTHONPATH"] = os.pathsep.join(paths + [p for p in [env.get("PYTHONPATH")] if p])
    env["TESTBENCH_DOWNLOADS_DIR"] = str(step_dir / "downloads")
    env["TESTBENCH_STEP_DIR"] = str(step_dir)
    env.setdefault("MPLBACKEND", "Agg")                         # child processes must not inherit an inline backend
    result: dict = {"name": step["name"], "kind": step["kind"], "status": "error", "error": None}
    param_error: tuple = ()                                     # notebook.ParamError, once a notebook step loads it
    missing = [s for s in step["secrets"] if s not in env]
    if missing:
        result["error"] = {"ename": "MissingSecret", "evalue": f"secrets not in the environment: {', '.join(missing)} "
                           "(locally: export them; on Modal: create a Modal secret with that name)"}
    else:
        try:
            if step["kind"] == "notebook":
                from . import notebook                               # nbclient and a kernel: notebook steps only
                param_error = (notebook.ParamError,)
                queue = step_dir / "upload_queue.json"
                queue.write_text(json.dumps([[str((workdir / f).resolve()) for f in group] for group in step["uploads"]]))
                env["TESTBENCH_UPLOAD_QUEUE"] = str(queue)
                env.pop("MPLBACKEND", None)                          # the kernel's own plots render inline
                python, why = kernel_python(project, env)            # uvx & co: the project's venv, if it can
                nb = notebook.run(workdir / step["notebook"], params=step["params"], cells=step["cells"],
                                  deadline=deadline, env=env, workdir=workdir, out_dir=step_dir, live_log=log_path,
                                  shim=SHIM if step.get("colab_shim", True) else None, python=python, python_note=why)
                result.update(cells=nb["cells"], images=nb["images"], code_cells=nb["code_cells"], error=nb["error"])
                result["runtime"] = {"kernel": nb["kernel"]}
                if nb.get("python"):
                    result["runtime"]["python"] = python_runtime(nb["python"], env, versions)
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
        except param_error as exc:
            result.update(status="error", error={"ename": "ParamError", "evalue": str(exc)})
        except Exception as exc:                                   # the harness itself failed: say so plainly
            result.update(status="error", error={"ename": type(exc).__name__, "evalue": str(exc)[:2000]})
        if step["kind"] in ("run", "web"):                         # what its command ran on, for the proof record
            result["runtime"] = step_runtime(step, env, workdir, versions)
    text = log_path.read_text(errors="replace")
    result["stdout_tail"] = "\n".join(text.splitlines()[-40:])[-5000:]        # the last 40 lines, at most 5000 chars
    result["gpu"] = gpu() if gpu else None
    if result["status"] in ("passed", "failed") or step["kind"] == "run":
        seen = {**result, "stdout": text, "web": result.pop("_web_full", None) or result.get("web"),
                "seconds": time.time() - began, "run_started": run_started, "step_dir": str(step_dir),
                "mocks": mocked, "command": step.get("run")}
        checks = expect.check(step["expect"], workdir=workdir, result=seen)
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


def tool_environment() -> str | None:
    """The kind of environment made only for testbench that it runs from - uvx, uv tool or pipx - which holds none of
    the project's packages (not pytest either); None when it is installed like any other package."""
    prefix = Path(sys.prefix)
    if sys.prefix == sys.base_prefix:
        return None
    if (prefix / "uv-receipt.toml").is_file():
        return "uv tool"
    if (prefix / "pipx_metadata.json").is_file():
        return "pipx"
    return "uvx" if re.fullmatch(r"archive-v\d+", prefix.parent.name) else None     # uvx: an environment in uv's cache


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(p))


BIN = "Scripts" if os.name == "nt" else "bin"


def project_venv(project: Path, environ: dict | None = None) -> tuple[Path | None, bool]:
    """(the project's own virtualenv, whether it is the activated one): an activated VIRTUAL_ENV that is not the
    environment running testbench, else the project's .venv or venv; (None, False) when it has none."""
    environ = os.environ if environ is None else environ
    active = environ.get("VIRTUAL_ENV")
    if active and _norm(active) != _norm(sys.prefix):
        return Path(active), True
    found = next((Path(project) / d for d in (".venv", "venv") if (Path(project) / d / "pyvenv.cfg").is_file()), None)
    return found, False


def step_path(project: Path, environ: dict | None = None) -> str:
    """The PATH a step gets. The folder of the Python that runs testbench comes first, so `python` and `testbench` in a
    step mean the environment testbench is installed in - unless that is a tool environment of its own (uvx, uv tool,
    pipx): then `python` is the project's (an activated virtualenv, else its .venv or venv, else the one on PATH) and
    testbench's folder goes last, where `testbench` is still found."""
    environ = os.environ if environ is None else environ
    own, path = str(Path(sys.executable).parent), environ.get("PATH", "")
    if not tool_environment():
        return os.pathsep.join([own, path])
    rest = [p for p in path.split(os.pathsep) if p and _norm(p) != _norm(own)]     # uvx put it first
    venv, active = project_venv(project, environ)
    first = [str(venv / BIN)] if venv and not active else []                       # an activated one is first already
    return os.pathsep.join(first + rest + [own])


def steps_python(path: str) -> str | None:
    """The Python a step's `python` resolves to on step_path's PATH, when testbench runs from a tool environment of its
    own (uvx, uv tool, pipx) and that is another environment's Python (the project's): the one the proof record's
    `environment` then describes (provenance.environment). None when the steps' `python` is testbench's own."""
    if not tool_environment():
        return None
    found = shutil.which("python", path=path)
    if not found or _norm(Path(found).parent) == _norm(Path(sys.executable).parent):   # bin/python next to python3
        return None
    return found


def kernel_python(project: Path, environ: dict | None = None) -> tuple[str | None, str]:
    """For a Python notebook on this machine: (the Python its kernel should run, a note for the step's log). None is
    the kernel testbench always starts (its own Python's). From uvx, uv tool or pipx that Python holds none of the
    project's packages, so the kernel runs on the project's virtualenv instead (step_path's), when that has
    ipykernel - checked by running its Python, never by importing the project's code."""
    tool = tool_environment()
    if not tool:
        return None, ""
    environ = os.environ if environ is None else environ
    venv, _ = project_venv(project, environ)
    python = venv / BIN / ("python.exe" if os.name == "nt" else "python") if venv else None
    own = f"testbench's own Python ({tool}: {sys.executable}), which has none of the project's packages"
    if python is None or not python.is_file():
        return None, (f"the kernel runs on {own}: the project has no virtualenv (.venv or venv) to run it on"
                      + (f" ({venv} has no {python.name})" if python else ""))
    if not _has_ipykernel(str(python), environ):
        from .check import _pip
        return None, (f"the kernel runs on {own}: the project's virtualenv ({venv}) has no ipykernel. "
                      f"`{_pip(str(python), ['ipykernel'])}` puts it there, and notebooks then run on the project's "
                      "Python")
    return str(python), ""


def _has_ipykernel(python: str, environ: dict) -> bool:
    env = {k: v for k, v in environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")}
    try:
        return subprocess.run([python, "-c", "import ipykernel"], capture_output=True, env=env, timeout=60,
                              cwd=str(Path(python).parent)).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# ---- what a step ran on: the interpreters its command resolves to, found as the step finds them -------------------

PYTHON_WORD = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?", re.I)
TOOLS = {"node": "node", "npm": "node", "npx": "node", "pnpm": "node", "yarn": "node", "go": "go", "cargo": "cargo"}
VERSION = {"python": ("-c", "import platform; print(platform.python_version())"), "node": ("--version",),
           "go": ("version",), "cargo": ("--version",)}


def commands(line: str) -> list[list[str]]:
    """The words of each simple command in a shell line (leading VAR=value assignments dropped); what does not
    split cleanly is left out."""
    import shlex
    out = []
    for part in re.split(r"&&|\|\||[;|\n]", line or ""):
        try:
            words = shlex.split(part, comments=True)
        except ValueError:
            continue
        while words and re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]):
            words = words[1:]
        if words:
            out.append(words)
    return out


def _resolve(word: str, path: str, workdir: Path) -> str | None:
    if os.sep in word or (os.altsep and os.altsep in word):        # .venv/bin/python: from the step's folder
        p = Path(word) if os.path.isabs(word) else workdir / word
        return str(p) if p.is_file() and os.access(p, os.X_OK) else None
    return shutil.which(word, path=path)


def _version(tool: str, exe: str, env: dict, cache: dict) -> str | None:
    key = (tool, _norm(exe))
    if key not in cache and tool == "python" and key[1] == _norm(sys.executable):
        import platform
        cache[key] = platform.python_version()                  # the Python running this: no need to ask it
    if key not in cache:
        try:
            p = subprocess.run([exe, *VERSION[tool]], capture_output=True, text=True, env=env, timeout=30,
                               stdin=subprocess.DEVNULL)
            said = (p.stdout.strip() or p.stderr.strip()).splitlines()
            text = said[0].strip() if p.returncode == 0 and said else None
        except (OSError, subprocess.TimeoutExpired):
            text = None
        if text and tool == "go":                           # go version go1.22.1 linux/amd64
            text = (re.search(r"go(\d[\w.]*)", text) or [None, text])[1]
        elif text and tool == "cargo":                      # cargo 1.79.0 (ffa9cf99a 2024-06-03)
            text = (re.search(r"(\d+\.\d+[\w.-]*)", text) or [None, text])[1]
        cache[key] = text[:80] if text else None
    return cache[key]


def step_runtime(step: dict, env: dict, workdir: Path, cache: dict | None = None) -> dict:
    """What a run or web step's command runs on, found the way the step finds it (its PATH, its folder): the Python
    `python` resolves to - or the one the command names (python3.11, .venv/bin/python) - and node, go or cargo when
    the command uses them: {tool: {path, version}}. Only versions are asked for; the project's code is never run."""
    cache = {} if cache is None else cache
    line = step.get("run") if step.get("kind") == "run" else (step.get("web") or {}).get("serve") or ""
    firsts = [cmd[0] for cmd in commands(line or "")]           # each simple command's program
    named = next((w for w in firsts if PYTHON_WORD.fullmatch(Path(w).name)), "python")
    wanted = {"python": named}
    for w in firsts:                                             # npm, npx, pnpm and yarn run on node
        tool = TOOLS.get(Path(w).name)
        if tool and tool not in wanted:
            wanted[tool] = tool
    out = {}
    for tool, word in wanted.items():
        exe = _resolve(word, env.get("PATH", ""), workdir)
        if exe:
            out[tool] = {"path": exe, "version": _version(tool, exe, env, cache)}
    return out


def python_runtime(python: str, env: dict, cache: dict | None = None) -> dict:
    """{path, version} of a Python by its path (a notebook's kernel)."""
    return {"path": python, "version": _version("python", python, env, {} if cache is None else cache)}


def execute_plan(plan: dict, *, project_dir: str | Path, run_dir: str | Path, workdir: str | Path | None = None,
                 meta: dict | None = None, on_progress=None) -> dict:
    """Run every step in order; stop at the first failure unless the step says continue_on_failure."""
    project, run_dir = Path(project_dir).resolve(), Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(workdir).resolve() if workdir else project
    from .provenance import environment
    base_env = {**os.environ, "TESTBENCH_RUN_DIR": str(run_dir), "TESTBENCH_PROJECT_DIR": str(project),
                "PATH": step_path(project)}
    report = {"testbench_version": __version__, **(meta or {}), "status": "running", "started": time.time(),
              "steps": [], "workdir": str(workdir),                 # about the Python the steps' `python` is
              "environment": environment(steps_python(base_env["PATH"]), base_env)}
    sampler = GpuSampler()
    stop_reason = None
    seal, seen, steps = None, {}, plan["steps"]                 # data: cut in the run's copy (dataseal.py)
    versions: dict = {}                                         # each interpreter's version, asked once a run
    look = None                                                 # lookahead: the reruns on data cut earlier

    def save():
        (run_dir / "report.json").write_text(json.dumps(report, indent=1, default=str))
        if on_progress:
            on_progress()

    save()
    try:
        if plan.get("data"):
            from . import dataseal                              # a layer: only for plans of a project with data:
            seal = dataseal.Seal(plan, workdir)
            try:
                report["data"] = seal.begin()
                base_env.update(seal.env())
                if plan.get("lookahead"):
                    reruns = dataseal.Lookahead(plan, workdir, run_dir)
                    try:
                        reruns.begin(seal.seen)
                    except BaseException:
                        reruns.close()
                        raise
                    look = reruns
            except (dataseal.DataError, OSError) as exc:
                report["status"], stop_reason, steps = "error", f"its data could not be prepared: {exc}", []
        for i, step in enumerate(steps, 1):
            if look:                                            # the next rerun: the copy put back, the data cut
                try:
                    why = look.switch(step.get("lookahead") or 0)
                except (OSError, ValueError) as exc:            # DataError is a ValueError
                    report["status"], why = "error", f"a look-ahead rerun could not be prepared: {exc}"
                if why:
                    stop_reason = why
                    break
            _event(run_dir, event="step_start", step=step["name"], index=i)
            save()
            mark = len(sampler.samples)
            step_dir = run_dir / "steps" / f"{i:02d}-{slug(step['name'])}"
            if seal:                                            # what the step's data files hold, as evidence
                seen[step.get("window")] = data = (seal.window(step["window"]) if step.get("window")
                                                   else look.seen if look else seal.seen)
                step_dir.mkdir(parents=True, exist_ok=True)
                (step_dir / "data.json").write_text(json.dumps(data, indent=1))
            result = run_step(step, project=project, workdir=workdir, step_dir=step_dir,
                              base_env=base_env, gpu=lambda: sampler.reading(mark), run_started=report["started"],
                              versions=versions)
            result["dir"] = f"steps/{i:02d}-{slug(step['name'])}"
            for key in ("window", "lookahead"):
                if step.get(key):
                    result[key] = step[key]
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
        if seal:
            seal.close()
            if seal.windows:                                    # a line per walk-forward window
                report["windows"] = dataseal.windows_report(plan, report["steps"], seen)
        if look:                                                # the decisions compared, and the verdict
            try:
                report["lookahead"] = look.finish(report["steps"])
            except Exception as exc:                            # never lose the report over it: say so
                report["lookahead"] = {"verdict": "not checked", "ok": False, "finding": "look-ahead not checked: "
                                       f"comparing the decisions failed: {type(exc).__name__}: {str(exc)[:500]}"}
            finally:
                look.close()
        if plan.get("model_cutoff"):                            # results on data the model may already know
            from . import dataseal
            try:
                report["model_cutoff"] = dataseal.cutoff_record(plan, report)
            except (ValueError, TypeError, KeyError) as exc:          # unknown is a failure only with require_after
                strict = bool((plan["model_cutoff"] or {}).get("require_after"))
                report["model_cutoff"] = {"level": "failed" if strict else "warning", "ok": not strict,
                                          "finding": "model_cutoff could not be compared with this run's data: "
                                                     f"{type(exc).__name__}: {exc}"}
        if report["status"] not in ("cancelled", "error"):
            ran = report["steps"]
            report["status"] = "passed" if ran and len(ran) == len(plan["steps"]) and all(s["status"] == "passed" for s in ran) \
                else "failed"
            if any(isinstance(report.get(k), dict) and not report[k].get("ok", True)
                   for k in ("lookahead", "model_cutoff")):
                report["status"] = "failed"
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
