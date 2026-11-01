"""What a run proves, and about what: the code it ran, where it ran, and the evidence it left.

A report (schema/report-v1.schema.json) carries:
  source       the project's git commit at launch, whether it had uncommitted changes, and a hash of them
               (the diff and the untracked files), so "this passed" names exactly which code passed;
  environment  Python, platform, the installed packages' versions and a hash of them, where the plan ran (Modal,
               Colab, ssh too);
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
    found = [((d.metadata["Name"] or "").lower(), d.version) for d in distributions() if d.metadata["Name"]]
    pkgs = sorted({f"{name}=={version}" for name, version in found})
    return {"python": platform.python_version(), "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(), "executable": sys.executable,
            "packages": len(pkgs), "packages_sha256": hashlib.sha256("\n".join(pkgs).encode()).hexdigest(),
            "package_versions": dict(sorted(found))}         # so `testbench diff` can name the packages that changed


def evidence(run_dir: Path) -> tuple[list[dict], str]:
    """Every file a run kept under steps/, with sizes and hashes, and one hash over all of them."""
    run_dir = Path(run_dir)
    files = sorted(p for p in (run_dir / "steps").rglob("*") if p.is_file()) if (run_dir / "steps").exists() else []
    out = [{"path": p.relative_to(run_dir).as_posix(), "bytes": p.stat().st_size, "sha256": _sha256_file(p)}
           for p in files]                                   # / on every system, so a report checks out anywhere
    return out, _total(out)


def _total(entries: list[dict]) -> str:
    text = "".join(f"{e['path']}\0{e['sha256']}\n" for e in entries)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()     # a file name that is not UTF-8


def verify(run_dir: Path, report: dict) -> list[str]:
    """What no longer matches the evidence the report recorded: changed, missing or added files, or a list of hashes
    edited to match changed files (it no longer adds up to the evidence_sha256 the run recorded and printed)."""
    entries = report.get("evidence")
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("path"), str)
                                                and isinstance(e.get("sha256"), str) for e in entries):
        return ["the report's evidence list is not a list of {path, sha256}: report.json was cut short or edited"]
    if _total(entries) != report.get("evidence_sha256"):
        return ["the report's evidence list does not add up to its evidence_sha256: report.json was edited after "
                "the run"]
    recorded = {e["path"]: e["sha256"] for e in entries}
    try:
        now = {e["path"]: e["sha256"] for e in evidence(run_dir)[0]}
    except OSError as exc:
        return [f"the evidence cannot be read: {exc}"]
    problems = [f"changed: {p}" for p in recorded if p in now and now[p] != recorded[p]]
    problems += [f"missing: {p}" for p in recorded if p not in now]
    problems += [f"added after the run: {p}" for p in now if p not in recorded]
    return problems
