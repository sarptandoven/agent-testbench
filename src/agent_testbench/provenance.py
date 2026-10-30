"""What a run proves, and about what: the code it ran, where it ran, and the evidence it left.

A report (schema/report-v1.schema.json) carries:
  source       the project's git commit at launch, whether it had uncommitted changes, and a hash of them
               (the diff and the untracked files), so "this passed" names exactly which code passed;
  environment  Python, platform and a hash of the installed packages where the plan ran (Modal, Colab, ssh too);
  evidence     every file the run kept (logs, executed notebooks, images, artifacts) with its size and sha256, and
               one sha256 over all of them, so evidence that was changed afterwards no longer matches.
"""
from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
SCHEMA = Path(__file__).parent / "schema" / "report-v1.schema.json"
OWN = ":(exclude).testbench"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(root: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=120)


def source(root: Path) -> dict | None:
    """The code a run is launched on; None outside a git repository."""
    root = Path(root)
    try:
        head = _git(root, "rev-parse", "HEAD")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if head.returncode != 0:
        return None
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.decode().strip()
    diff = _git(root, "diff", "HEAD", "--binary", "--", ".", OWN).stdout
    untracked = [p for p in _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", ".", OWN)
                 .stdout.decode(errors="replace").split("\0") if p]
    h = hashlib.sha256(diff)
    top = Path(_git(root, "rev-parse", "--show-toplevel").stdout.decode().strip())
    for rel in sorted(untracked):
        f = root / rel
        if f.is_file():
            h.update(f"\0{(f.resolve().relative_to(top.resolve()))}\0{_sha256_file(f)}".encode())
    dirty = bool(diff) or bool(untracked)
    return {"commit": head.stdout.decode().strip(), "branch": branch, "dirty": dirty,
            "changes_sha256": h.hexdigest() if dirty else None, "untracked_files": len(untracked)}


def environment() -> dict:
    """Where the plan's code ran: called by the executor on that machine."""
    from importlib.metadata import distributions
    pkgs = sorted({f"{(d.metadata['Name'] or '').lower()}=={d.version}" for d in distributions() if d.metadata["Name"]})
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "executable": sys.executable,
            "packages": len(pkgs), "packages_sha256": hashlib.sha256("\n".join(pkgs).encode()).hexdigest()}


def evidence(run_dir: Path) -> tuple[list[dict], str]:
    """Every file a run kept under steps/, with sizes and hashes, and one hash over all of them."""
    run_dir = Path(run_dir)
    files = sorted(p for p in (run_dir / "steps").rglob("*") if p.is_file()) if (run_dir / "steps").exists() else []
    out = [{"path": str(p.relative_to(run_dir)), "bytes": p.stat().st_size, "sha256": _sha256_file(p)} for p in files]
    total = hashlib.sha256("".join(f"{e['path']}\0{e['sha256']}\n" for e in out).encode()).hexdigest()
    return out, total


def verify(run_dir: Path, report: dict) -> list[str]:
    """What no longer matches the evidence the report recorded: changed, missing or added files."""
    recorded = {e["path"]: e["sha256"] for e in report.get("evidence") or []}
    now = {e["path"]: e["sha256"] for e in evidence(run_dir)[0]}
    problems = [f"changed: {p}" for p in recorded if p in now and now[p] != recorded[p]]
    problems += [f"missing: {p}" for p in recorded if p not in now]
    problems += [f"added after the run: {p}" for p in now if p not in recorded]
    return problems
