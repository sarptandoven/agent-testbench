"""What a run proves, and about what: the code it ran, where it ran, and the evidence it left.

A report (schema/report-v1.schema.json) carries:
  source       the project's git commit at launch, whether it had uncommitted changes, and a hash of them
               (the diff and the untracked files), so "this passed" names exactly which code passed;
  environment  Python, platform, the installed packages' versions and a hash of them, where the plan ran (Modal,
               Colab, ssh too);
  evidence     every file the run kept (logs, executed notebooks, images, artifacts) with its size and sha256, and
               one sha256 over all of them, so evidence that was changed afterwards no longer matches.

A run made by the GitHub Action with `attest: true` is also signed: a GitHub artifact attestation of report.json
(and of each evidence file) says which repository's workflow made it, at which commit. verify_attestation checks it
with `gh attestation verify`; together with verify() that covers the evidence too, since report.json records the
sha256 of every evidence file.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
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


def check_list(report: dict) -> str:
    """'' when the report's evidence list is whole - entries of {path, sha256} that add up to its evidence_sha256 -
    else what is wrong with it. `testbench verify --from` checks a published bundle with it, away from the run."""
    entries = report.get("evidence")
    if not isinstance(entries, list) or not all(isinstance(e, dict) and isinstance(e.get("path"), str)
                                                and isinstance(e.get("sha256"), str) for e in entries):
        return "the report's evidence list is not a list of {path, sha256}: report.json was cut short or edited"
    if _total(entries) != report.get("evidence_sha256"):
        return ("the report's evidence list does not add up to its evidence_sha256: report.json was edited after "
                "the run")
    return ""


def verify(run_dir: Path, report: dict) -> list[str]:
    """What no longer matches the evidence the report recorded: changed, missing or added files, or a list of hashes
    edited to match changed files (it no longer adds up to the evidence_sha256 the run recorded and printed)."""
    wrong = check_list(report)
    if wrong:
        return [wrong]
    entries = report["evidence"]
    recorded = {e["path"]: e["sha256"] for e in entries}
    try:
        now = {e["path"]: e["sha256"] for e in evidence(run_dir)[0]}
    except OSError as exc:
        return [f"the evidence cannot be read: {exc}"]
    problems = [f"changed: {p}" for p in recorded if p in now and now[p] != recorded[p]]
    problems += [f"missing: {p}" for p in recorded if p not in now]
    problems += [f"added after the run: {p}" for p in now if p not in recorded]
    return problems


# ---- attestations: a report signed by a GitHub workflow (the action's `attest: true`) -------------------------------

BUNDLE = "attestation.sigstore.json"          # the action keeps the signed bundle here, next to report.json


def github_repo(root: Path) -> str:
    """OWNER/REPO of the GitHub repository a project is in: GITHUB_REPOSITORY inside a workflow, else the origin
    remote; "" when neither says."""
    if re.fullmatch(r"[\w.-]+/[\w.-]+", os.environ.get("GITHUB_REPOSITORY") or ""):
        return os.environ["GITHUB_REPOSITORY"]
    try:
        out = _git(Path(root), "remote", "get-url", "origin").stdout.decode(errors="replace").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", out)
    return f"{m[1]}/{m[2]}" if m else ""


def verify_attestation(run_dir: Path, report: dict, repo: str, gh: str | None = None) -> tuple[int, str]:
    """Whether report.json carries a GitHub artifact attestation made by a workflow of repo, at the commit the report
    names, checked with `gh attestation verify` (offline from the bundle in the run folder when the action kept one).
    Returns (0, what it shows), (1, what does not check out) or (2, why it could not be checked)."""
    run_dir = Path(run_dir)
    gh = gh or shutil.which("gh")
    if not gh:
        return 2, ("the attestation was not checked: it takes `gh`, the GitHub CLI (https://cli.github.com), which is "
                   f"not on PATH. With it: gh attestation verify {run_dir / 'report.json'} "
                   f"--repo {repo or 'OWNER/REPO'}")
    if not repo:
        return 2, ("the attestation was not checked: say which GitHub repository's workflow should have made it, with "
                   "--repo OWNER/REPO")
    cmd = [gh, "attestation", "verify", str(run_dir / "report.json"), "--repo", repo, "--format", "json"]
    bundle = run_dir / BUNDLE
    if bundle.is_file():
        cmd += ["--bundle", str(bundle)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, f"the attestation was not checked: gh attestation verify did not finish ({exc})"
    if r.returncode != 0:
        said = "\n  ".join((r.stderr.strip() or r.stdout.strip() or f"exit code {r.returncode}").splitlines()[-6:])
        if r.returncode == 4 or re.search(r"gh auth login|not logged in|HTTP 401|GH_TOKEN", said):
            return 2, ("the attestation was not checked: gh is not signed in to GitHub (run `gh auth login`, or set "
                       f"GH_TOKEN). gh said:\n  {said}")
        return 1, (f"report.json has no attestation from a workflow of {repo} that checks out"
                   + (f" (checked against {BUNDLE})" if bundle.is_file() else "") + f". gh said:\n  {said}")
    try:
        results = json.loads(r.stdout or "[]")
    except ValueError:
        results = []
    certs = [((x.get("verificationResult") or {}).get("signature") or {}).get("certificate") or {}
             for x in results if isinstance(x, dict)] if isinstance(results, list) else []
    certs = [c for c in certs if isinstance(c, dict)]
    commit = str((report.get("source") or {}).get("commit") or "")
    digests = [str(c["sourceRepositoryDigest"]) for c in certs if c.get("sourceRepositoryDigest")]
    if commit and digests and commit not in digests:
        return 1, (f"report.json is attested by a workflow of {repo} at commit {digests[0][:12]}, but the report says "
                   f"it ran on commit {commit[:12]}: the workflow ran other code than it was started on, so the "
                   "attestation does not vouch for this report's commit")
    cert = next((c for c in certs if c.get("sourceRepositoryDigest") == commit), certs[0] if certs else {})
    signer = str(cert.get("buildConfigURI") or cert.get("subjectAlternativeName") or f"a workflow of {repo}")
    signer = re.sub(r"^https://github\.com/", "", signer)
    run = str(cert.get("runInvocationURI") or "")
    if not commit:
        at = ", for code outside git (no commit to compare)"
    elif digests:
        at = f", at commit {commit[:12]}, the commit the report names"
    else:
        at = f"; gh did not say at which commit, so the report's commit {commit[:12]} was not compared"
    text = (f"report.json is attested: signed by {signer}" + (f" in {run}" if run else "") + at
            + (f"; checked against {BUNDLE}" if bundle.is_file() else "") + ".")
    if cert.get("runnerEnvironment") == "self-hosted":
        text += " It ran on a self-hosted runner: whoever runs that machine could have changed the run."
    return 0, text
