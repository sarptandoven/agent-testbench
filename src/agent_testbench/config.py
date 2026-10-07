"""testbench.yaml: what a project lets its testbench run, where, and for how much.

Every error names the field and says what to write instead, because the reader is often an agent that
will fix the file from the message alone.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

CONFIG_NAME = "testbench.yaml"
BACKENDS = ("local", "modal", "colab")
STEP_KINDS = ("notebook", "run", "web")
MODAL_GPUS = ("T4", "L4", "A10", "A10G", "L40S", "A100-40GB", "A100-80GB", "A100", "H100", "H200", "B200", "B300",
              "RTX-PRO-6000")
COLAB_GPUS = ("T4", "L4", "G4", "A100", "H100")

DEFAULTS = {
    "exclude": [".git", ".testbench", "__pycache__", "*.pyc", ".venv", "venv", "node_modules", ".ipynb_checkpoints",
                ".DS_Store"],
    "budget": {"max_run_usd": 2.0, "ask_above_usd": 0.5, "max_day_usd": 10.0},
    "pricing": {},
    "modal": {"python": "3.11", "pip": [], "requirements": None, "apt": [], "cpu": 2.0, "memory_gb": 8},
    "colab": {"pip": [], "requirements": None},
    "guard": {"ask": ["git push", "gh repo create", "gh pr merge", "gh release create"]},
    "sessions": {"backend": "local", "gpu": None, "idle_minutes": 10, "max_minutes": 60, "secrets": []},
}
STEP_DEFAULTS = {"max_minutes": 10, "params": {}, "env": {}, "secrets": [], "pythonpath": [], "artifacts": [],
                 "expect": {}, "uploads": [], "cells": None, "colab_shim": True, "continue_on_failure": False}


class ConfigError(ValueError):
    pass


def find_root(start: str | Path = ".") -> Path:
    """The nearest directory at or above `start` that holds testbench.yaml."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    raise ConfigError(f"No {CONFIG_NAME} here or in any parent of {here}. Run `testbench init` in the project folder.")


def load(root: str | Path) -> dict:
    root = Path(root)
    path = root / CONFIG_NAME
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    return validate(raw, root)


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate(raw: dict, root: Path) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must be a mapping with `project:` and `plans:`")
    cfg = _merge(DEFAULTS, raw)
    project = cfg.get("project")
    if not project or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", str(project)):
        raise ConfigError("`project:` must be a short name of letters, digits, - or _ (it names the Modal app and volume)")
    cfg["root"] = str(root)
    for key in ("max_run_usd", "ask_above_usd", "max_day_usd"):
        if not isinstance(cfg["budget"].get(key), (int, float)) or cfg["budget"][key] < 0:
            raise ConfigError(f"`budget.{key}` must be a number of US dollars, e.g. {DEFAULTS['budget'][key]}")
    _sessions(cfg["sessions"])
    plans = cfg.get("plans") or {}
    if not isinstance(plans, dict):
        raise ConfigError("`plans:` must map plan names to {backend, steps}; see `testbench init` for an example")
    cfg["plans"] = {name: _plan(name, plan, root) for name, plan in plans.items()}
    return cfg


def check_hardware(where: str, backend: str, gpu) -> None:
    if backend not in BACKENDS:
        raise ConfigError(f"`{where}.backend` is {backend!r}; use one of {', '.join(BACKENDS)}")
    if gpu:
        if backend == "local":
            raise ConfigError(f"`{where}.gpu`: local uses whatever GPU this machine has; drop the GPU, or choose the "
                              "modal or colab backend")
        allowed = MODAL_GPUS if backend == "modal" else COLAB_GPUS
        if str(gpu).split(":")[0].upper() not in {g.upper() for g in allowed}:
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; {backend} offers {', '.join(allowed)}")


def _sessions(s: dict) -> None:
    check_hardware("sessions", s.get("backend", "local"), s.get("gpu"))
    for key in ("idle_minutes", "max_minutes"):
        if not isinstance(s.get(key), (int, float)) or s[key] <= 0:
            raise ConfigError(f"`sessions.{key}` must be a positive number of minutes")


def _plan(name: str, plan: dict, root: Path) -> dict:
    where = f"plans.{name}"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", str(name)):
        raise ConfigError(f"`{where}`: plan names use letters, digits, - or _")
    if not isinstance(plan, dict):
        raise ConfigError(f"`{where}` must be a mapping with `backend:` and `steps:`")
    plan = {"backend": "local", "gpu": None, "workdir": None, "description": "", **plan}
    check_hardware(where, plan["backend"], plan.get("gpu"))
    plan["workdir"] = plan["workdir"] or ("inplace" if plan["backend"] == "local" else "copy")
    if plan["workdir"] not in ("inplace", "copy"):
        raise ConfigError(f"`{where}.workdir` must be inplace (run in the project folder) or copy (a scratch copy)")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigError(f"`{where}.steps` must be a list of steps")
    seen = set()
    plan["steps"] = []
    for i, step in enumerate(steps):
        s = _step(f"{where}.steps[{i}]", step, root)
        if s["name"] in seen:
            raise ConfigError(f"`{where}`: two steps are named {s['name']!r}; names must be unique within a plan")
        seen.add(s["name"])
        plan["steps"].append(s)
    return plan


def _step(where: str, step: dict, root: Path) -> dict:
    if not isinstance(step, dict) or not step.get("name"):
        raise ConfigError(f"`{where}` needs a `name:`")
    kinds = [k for k in STEP_KINDS if k in step]
    if len(kinds) != 1:
        raise ConfigError(f"`{where}` ({step['name']}) needs exactly one of `notebook:`, `run:` or `web:`")
    s = {**copy.deepcopy(STEP_DEFAULTS), **step, "kind": kinds[0]}
    if not isinstance(s["max_minutes"], (int, float)) or s["max_minutes"] <= 0:
        raise ConfigError(f"`{where}.max_minutes` must be a positive number; the step is stopped when it runs out")
    for key in ("params", "env", "expect"):
        if not isinstance(s[key], dict):
            raise ConfigError(f"`{where}.{key}` must be a mapping")
    for key in ("secrets", "pythonpath", "artifacts", "uploads"):
        if not isinstance(s[key], list):
            raise ConfigError(f"`{where}.{key}` must be a list")
    if s["kind"] == "notebook":
        nb = root / s["notebook"]
        if not nb.is_file():
            raise ConfigError(f"`{where}.notebook`: {s['notebook']} does not exist (paths are relative to {root})")
        if s["cells"] is not None:
            s["cells"] = parse_cells(s["cells"], where)
        for group in s["uploads"]:
            if not isinstance(group, list):
                raise ConfigError(f"`{where}.uploads` is a list of uploads, each a list of files: [[data.csv], [a.mp4, b.mp4]]")
    if s["kind"] == "web":
        w = s["web"]
        if not isinstance(w, dict) or not w.get("url"):
            raise ConfigError(f"`{where}.web` needs at least `url:` (and usually `serve:`, the command that starts the app)")
        s["web"] = {"serve": None, "ready_timeout": 30, "actions": [], "screenshot": True, "viewport": [1280, 800], **w}
    if s["kind"] == "run" and not isinstance(s["run"], str):
        raise ConfigError(f"`{where}.run` is a shell command string")
    for name in s["secrets"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name)):
            raise ConfigError(f"`{where}.secrets` lists secret NAMES only, never values")
    return s


def parse_cells(spec, where="cells") -> list[int]:
    """'1-3,5' or [1, 2] -> [1, 2, 3, 5]: code cells counted from 1, in notebook order."""
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, list):
        out = spec
    else:
        out = []
        for part in str(spec).split(","):
            part = part.strip()
            if re.fullmatch(r"\d+-\d+", part):
                a, b = map(int, part.split("-"))
                out += list(range(a, b + 1))
            elif part.isdigit():
                out.append(int(part))
            elif part:
                raise ConfigError(f"`{where}.cells`: {part!r} is not a cell number or range like 2-5")
    if not out or any(not isinstance(c, int) or c < 1 for c in out):
        raise ConfigError(f"`{where}.cells` counts code cells from 1, e.g. '1-3,5'")
    return sorted(set(out))


def plan_minutes(plan: dict) -> float:
    return float(sum(s["max_minutes"] for s in plan["steps"]))
