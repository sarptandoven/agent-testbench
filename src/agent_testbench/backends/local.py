"""Run on this machine: free, immediate, and the first rung of every ladder."""
from __future__ import annotations

from pathlib import Path

from .. import executor
from ..config import ConfigError
from . import Backend, _gpu_backends, copy_project


class LocalBackend:
    def __init__(self, root: Path, cfg: dict, run_dir: Path, plan: dict, meta: dict):
        self.root, self.cfg, self.run_dir, self.plan, self.meta = Path(root), cfg, Path(run_dir), plan, meta

    def run(self):
        workdir = self.root
        if self.plan.get("workdir") == "copy":
            workdir = copy_project(self.root, self.run_dir / "work", self.cfg["exclude"])
        rep = executor.execute_plan(self.plan, project_dir=self.root, run_dir=self.run_dir, workdir=workdir, meta=self.meta)
        return rep, 0.0

    def cancel(self) -> str:
        return ""

    def cost(self, rep: dict) -> float:
        return 0.0


class Local(Backend):
    name = "local"
    remote = False
    startup_minutes = 0.0

    def check_hardware(self, where: str, gpu) -> None:
        if gpu:
            raise ConfigError(f"`{where}.gpu`: local uses whatever GPU this machine has; drop the GPU, or choose the "
                              f"{_gpu_backends()}")

    def plan_runner(self):
        return LocalBackend

    def session_class(self):
        from ..session_backends import LocalSession
        return LocalSession


backend = Local()
