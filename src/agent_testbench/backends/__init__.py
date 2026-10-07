"""Where a plan runs. Each backend takes a launched run and returns (report, cost in US dollars)."""
from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path


def get_backend(name: str):
    if name == "local":
        from .local import LocalBackend
        return LocalBackend
    if name == "modal":
        from .modal_backend import ModalBackend
        return ModalBackend
    if name == "colab":
        from .colab_backend import ColabBackend
        return ColabBackend
    raise ValueError(f"unknown backend {name!r}")


def cancel_remote(state: dict) -> str:
    """Cancel remote work for a run whose driver is gone, from the handles saved in state.json."""
    if state.get("modal_call_id"):
        from .modal_backend import cancel_call
        return cancel_call(state["modal_call_id"])
    if state.get("colab_session"):
        from .colab_backend import stop_session
        return stop_session(state["colab_session"], state.get("colab_cli_args", []))
    return ""


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
