"""Where code runs. A backend is one object that says what it offers - GPUs, prices, the start-up a remote machine
costs, its section of testbench.yaml - and hands out the two classes that do the work: a plan runner (takes a
launched run, returns its report and cost) and a session (one kernel kept up on its machine).

Built in: local, modal, colab, ssh, docker, kubernetes. Others come from installed packages that declare an entry point:

    # pyproject.toml of a package such as testbench-ssh
    [project.entry-points."agent_testbench.backends"]
    ssh = "testbench_ssh:backend"            # an instance (or subclass) of agent_testbench.backends.Backend

Two rules every backend keeps: price its hardware, so `budget.check` bounds the worst case before anything
starts; and give every remote run and session a hard stop on the remote side, so the bound holds even if this
machine goes to sleep.
"""
from __future__ import annotations

import fnmatch
import importlib
import re
import shutil
from pathlib import Path
from typing import Protocol

from ..config import ConfigError

ENTRY_POINTS = "agent_testbench.backends"
GPU_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.!+-]*(:[1-9][0-9]{0,2})?")
BUILTIN = {"local": "agent_testbench.backends.local:backend",
           "modal": "agent_testbench.backends.modal_backend:backend",
           "colab": "agent_testbench.backends.colab_backend:backend",
           "ssh": "agent_testbench.backends.ssh_backend:backend",
           "docker": "agent_testbench.backends.docker_backend:backend",
           "kubernetes": "agent_testbench.backends.kubernetes_backend:backend"}


class PlanRunner(Protocol):
    """Runs one launched plan. Built as cls(root=, cfg=, run_dir=, plan=, meta=) in the run's driver process."""

    def run(self) -> tuple[dict, float]: ...      # (report, US dollars); report.json is in run_dir when it returns
    def cancel(self) -> str: ...                  # stop the remote work now; say what was stopped
    def cost(self, report: dict) -> float: ...    # what the run cost so far, for a run cut short


class Session(Protocol):
    """One kernel on the backend's machine, built as cls(root, cfg, sdir, state). Runs kernelkit's launcher there
    and kernelkit's client for each exec, so `exec` returns the same evidence on every backend."""

    def start(self) -> dict: ...                  # handles saved in state.json (ids to find the machine again)
    def alive(self) -> bool: ...
    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict: ...
    def ls(self, path: str) -> list[str]: ...
    def get(self, remote: str, local: Path) -> None: ...
    def put(self, local: Path, remote: str) -> None: ...
    def install(self, packages: list[str]) -> str: ...
    def sync(self) -> str: ...
    def shell(self, command: str, timeout: float) -> str: ...
    def stop(self) -> None: ...


class Backend:
    name = ""
    gpus: tuple[str, ...] = ()            # GPUs it offers by name (a plan may ask for NAME:COUNT); empty: no choice
    remote = True                         # another machine: plans run in a copy, start-up is priced in
    startup_minutes = 5.0                 # added to every worst case: starting, installing, uploading
    max_session_minutes: float | None = None
    config_defaults: dict = {}            # its own section(s) of testbench.yaml, e.g. {"modal": {"cpu": 2.0}}

    def check_hardware(self, where: str, gpu) -> None:
        if not gpu:
            return
        if not GPU_FORMAT.fullmatch(str(gpu)):
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write a GPU name, or NAME:COUNT for several (e.g. H100:2), "
                              "with no spaces")
        if not self.gpus:
            raise ConfigError(f"`{where}.gpu`: {self.name} has no choice of GPU; drop the GPU, or choose "
                              f"{_gpu_backends()}")
        _, sep, count = str(gpu).partition(":")
        if sep and not (count.isascii() and count.isdigit() and int(count) > 0):     # T4:0 would be priced as no GPU
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write the GPU, or the GPU and how many, e.g. H100 or H100:2")
        if split_gpu(gpu)[0] not in {g.upper() for g in self.gpus}:
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; {self.name} offers {', '.join(self.gpus)}")

    def validate(self, cfg: dict) -> None:
        """Check this backend's own section of testbench.yaml; called only when a plan or the sessions use it."""

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        """US dollars per minute for hardware ({backend, gpu, cpu?, memory_gb?}) at its worst."""
        return 0.0

    def plan_runner(self) -> type[PlanRunner]:
        raise ConfigError(f"the {self.name} backend cannot run plans; use it for sessions, or choose another backend")

    def session_class(self) -> type[Session]:
        raise ConfigError(f"the {self.name} backend has no live sessions; use it for plans, or choose another backend")

    def cancel_remote(self, state: dict) -> str:
        """Cancel a run's remote work from the handles in its state.json, when its driver process is gone."""
        return ""

    def doctor(self) -> list[tuple[str, bool, str]]:
        """(what, ok, detail) lines for `testbench doctor`: is this machine set up to use the backend?"""
        return []

    def prices(self, cfg: dict) -> list[str]:
        """Lines for `testbench prices`: what this backend charges for, and where each price comes from."""
        return []


def split_gpu(gpu) -> tuple[str, int]:
    """'H100:2' -> ('H100', 2); None -> ('', 1)."""
    if not gpu:
        return "", 1
    name, _, count = str(gpu).partition(":")
    return name.upper(), int(count) if count.isascii() and count.isdigit() else 1


def tool_check(what: str, exe: str, how: str) -> tuple[str, bool, str]:
    found = shutil.which(exe)
    return what, bool(found), found or how


_loaded: dict[str, Backend] | None = None
_broken: dict[str, str] = {}


def _instance(obj, name: str) -> Backend:
    b = obj() if isinstance(obj, type) else obj
    if not isinstance(b, Backend):
        raise TypeError(f"{obj!r} is not an agent_testbench.backends.Backend")
    b.name = b.name or name
    return b


def registry() -> dict[str, Backend]:
    """Every backend that loaded: the built-ins, then installed plugins. A plugin that fails to load is left out
    and reported by `testbench doctor`, so it only breaks the projects that use it."""
    global _loaded
    if _loaded is None:
        found = {}
        for name, ref in BUILTIN.items():
            module, _, attr = ref.partition(":")
            found[name] = _instance(getattr(importlib.import_module(module), attr), name)
        from importlib.metadata import entry_points
        for ep in entry_points(group=ENTRY_POINTS):
            if ep.name in found:
                _broken[ep.name] = f"{ep.value} tries to replace a backend that already exists; it was skipped"
                continue
            try:
                found[ep.name] = _instance(ep.load(), ep.name)
            except Exception as exc:
                _broken[ep.name] = f"{ep.value} did not load: {type(exc).__name__}: {exc}"
        _loaded = found
    return _loaded


def register(backend: Backend) -> Backend:
    """Add a backend by hand (a test, or a notebook that defines one inline)."""
    registry()[backend.name] = backend
    return backend


def broken() -> dict[str, str]:
    registry()
    return dict(_broken)


def names() -> list[str]:
    return list(registry())


def get(name: str) -> Backend:
    found = registry()
    if name in found:
        return found[name]
    if name in _broken:
        raise ConfigError(f"backend {name!r} is installed but did not load ({_broken[name]}); `testbench doctor` "
                          "shows it - reinstall or fix that package")
    raise ConfigError(f"no backend {name!r}; use one of {', '.join(found)}, or install the package that provides it")


def _gpu_backends() -> str:
    offering = [n for n, b in registry().items() if b.gpus]
    return (" or ".join(offering) + " backend") if offering else "a backend that offers GPUs"


def excluded(rel: str, patterns: list[str]) -> bool:
    parts = Path(rel).parts
    return any(fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts) for p in patterns)


def copy_project(root: Path, dest: Path, exclude: list[str]) -> Path:
    """A scratch copy of the project, without the excluded files (and never .testbench itself)."""
    root, dest = Path(root), Path(dest)
    patterns = list(exclude) + [".testbench"]

    def ignore(directory, names):
        rel_dir = Path(directory).relative_to(root)
        return [n for n in names if excluded(str(rel_dir / n) if str(rel_dir) != "." else n, patterns)]

    shutil.copytree(root, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)
    return dest


def project_files(root: Path, exclude: list[str]) -> list[Path]:
    root = Path(root)
    patterns = list(exclude) + [".testbench"]
    out = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_file() and not excluded(rel, patterns):
            out.append(p)
    return out
