"""testbench share: one run's proof as a static page anyone can open - and check.

    testbench share [RUN] --to DEST [--dry-run] [--yes] [--keep-paths] [--max-file-mb 25]
    testbench verify --from URL_OR_FOLDER

A share is a folder (a "bundle"): index.html (the run's report as one page, with its tear sheet), report.json,
badge.svg, the evidence files the page links, and testbench-manifest.json (the sha256 of every file in it, and the
run's evidence_sha256). It goes to DEST/RUN/, where DEST is

    ./public                 a folder on this machine
    git:BRANCH[/FOLDER]      a branch of the project's git repository (gh-pages for GitHub Pages), committed with git
                             plumbing and a temporary index: the working tree, the index and the current branch are
                             never touched, and nothing is pushed - the person pushes
    s3://BUCKET[/PREFIX]     with the aws CLI and its own sign-in; testbench never sees the credentials

Never published: state.json, plan.json, ledgers, limits, NOTES.md, .env files and keys, and any file holding the value
of a secret (`secrets:`), of a step's env: or of an environment variable named like a key or a token. The page and
report.json are scrubbed of this machine's paths (the project folder, the home folder, the user's name). An evidence
file is published byte for byte or not at all - its sha256 is the proof - so one holding such a path is left out
unless --keep-paths. Every file is listed with its size and why it is there before anything is published, and
publishing takes a person's yes: --yes, or y typed in a terminal (the guard hook asks before an agent runs it).

`testbench verify --from` checks a bundle, a folder or its URL, with no project: every file against the manifest,
each published evidence file against the sha256 the run recorded, and that list against its evidence_sha256.
"""
from __future__ import annotations

import collections
import datetime as dt
import fnmatch
import getpass
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, badge as B, provenance, report as R, runs
from .config import counted

MANIFEST = "testbench-manifest.json"
FORMAT = 1
DOCS = "https://sarptandoven.github.io/agent-testbench/share/"
MAX_FILE_MB = 25.0
NEVER = (".env", ".env.*", "*.env", "state.json", "plan.json", "ledger.jsonl", "NOTES.md", "limits.yaml", "prices.json",
         ".netrc", ".modal.toml", ".npmrc", ".pypirc", ".git-credentials", "id_rsa*", "id_dsa*", "id_ecdsa*",
         "id_ed25519*", "*.pem", "*.key", "*.p12", "*.pfx", "credentials", "credentials.json", "kubeconfig")
NEVER_DIRS = (".git", ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".testbench")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|WEBHOOK|CREDENTIAL|AUTH", re.I)
MIN_SECRET, MIN_ENV = 4, 8              # shorter values ("1", "cpu") are in every log; they are no one's secret
VERIFY_LIMIT = 2 << 30                  # the most `verify --from` downloads for one bundle
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")
SAFE_PATH = re.compile(r"(?:[A-Za-z0-9_.@+=,~ ()\[\]-]+/)*[A-Za-z0-9_.@+=,~ ()\[\]-]+")


class ShareError(Exception):
    """Nothing was published: why, and what to do. code 2: refused or bad input; 1: it failed."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def never(path: str) -> str:
    """Why a file is never published, by its name or a folder on its way ('' when it may be)."""
    parts = path.split("/")
    for d in parts[:-1]:
        if d in NEVER_DIRS:
            return f"in a {d} folder"
    hit = next((p for p in NEVER if fnmatch.fnmatch(parts[-1].lower(), p.lower())), None)
    return f"{parts[-1]} is never published (state, plans, ledgers, limits, notes, .env files and keys)" if hit else ""


def safe_path(path) -> bool:
    """A file's place in a bundle: relative, /-separated, no .. or hidden part, nothing a shell or URL reads twice."""
    return (isinstance(path, str) and bool(SAFE_PATH.fullmatch(path)) and len(path) <= 400
            and not any(p.startswith(".") or p.strip() != p for p in path.split("/")))


# ---- what must not be published: this machine's paths and secret values ----------------------------------------

def _home() -> Path | None:
    try:
        home = Path.home()
    except (RuntimeError, KeyError, OSError):
        return None
    return home if len(str(home)) > 3 else None             # "/" or "C:\" names no one


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:                                       # no user name to be had (a container with no passwd entry)
        return ""


def _spellings(text: str) -> set[str]:
    """How a path or value is written in a page, a log or JSON."""
    out = {text, text.replace("\\", "/"), json.dumps(text)[1:-1], html.escape(text)}
    return {s for s in out if len(s) >= 2}


class Scrub:
    """This machine's paths and the values that must stay private: found in files, replaced in what testbench renders.

    rules: (pattern, label, replacement, original shown in the listing or None for a secret, kind "path" or "value")."""

    def __init__(self, root: Path, values: dict[str, str]):
        self.rules = []
        project = Path(root).resolve()
        places = [("the project folder", "<project>", project, {str(project), str(Path(root).absolute())})]
        home = _home()
        if home:
            places.append(("the home folder", "~", home, {str(home), str(home.resolve())}))
        for label, as_, shown, paths in places:
            for p in sorted({s for x in paths for s in _spellings(x)}, key=len, reverse=True):
                self._add(rf"(?<![\w.-]){re.escape(p)}(?![\w.-])", label, as_, str(shown), "path")
        user = _user()
        if len(user) >= 2:                                   # another user's home: /home/NAME, C:\Users\NAME
            after = r"(?:(?<=/home/)|(?<=/Users/)|(?<=\\Users\\)|(?<=\\\\Users\\\\))"
            self._add(after + re.escape(user) + r"(?=[/\\]|$)", "the user name", "<user>", user, "path")
        for name, value in sorted(values.items(), key=lambda kv: -len(kv[1])):
            bare = re.sub(r"^(secret |env |\$)", "", name)
            for v in sorted(_spellings(value), key=len, reverse=True):
                self._add(re.escape(v), f"the value of {name}", f"<{bare}>", None, "value")

    def _add(self, pattern: str, label: str, as_: str, shown, kind: str) -> None:
        flags = re.I if kind == "path" and os.name == "nt" else 0          # C:\Users and c:\users are one folder
        self.rules.append((re.compile(pattern, flags), re.compile(pattern.encode("utf-8", "surrogatepass"), flags),
                           label, as_, shown, kind))

    def text(self, text: str, counts: collections.Counter | None = None) -> str:
        """text with each path and value replaced (secrets first, then the longest paths), counted in counts."""
        for rx, _, label, as_, shown, kind in sorted(self.rules, key=lambda r: r[5] != "value"):
            text, n = rx.subn(lambda m: as_, text)
            if n and counts is not None:
                counts[(label, as_, shown)] += n
        return text

    def found(self, data: bytes) -> dict[str, collections.Counter]:
        """What a file holds: {"path": {label: times}, "value": {label: times}}, each counted once, as text() would
        replace it (the project folder inside the home folder is the project folder)."""
        out = {"path": collections.Counter(), "value": collections.Counter()}
        for _, rx, label, as_, _, kind in sorted(self.rules, key=lambda r: r[5] != "value"):
            data, n = rx.subn(lambda m: as_.encode(), data)
            if n:
                out[kind][label] += n
        return out

    def json(self, obj, counts: collections.Counter, keep=()):
        """A report with every string scrubbed except the keys in keep (its evidence list: the proof as recorded)."""
        if isinstance(obj, dict):
            return {self.text(k, counts) if isinstance(k, str) else k: v if k in keep else self.json(v, counts)
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.json(v, counts) for v in obj]
        return self.text(obj, counts) if isinstance(obj, str) else obj


def private_values(root: Path, cfg: dict, run_dir: Path) -> dict[str, str]:
    """{name: value} of what is never published: secrets named in testbench.yaml or the run's plan (their values from
    this environment), the steps' env: values, and environment variables named like a key, token or password."""
    names, values = set((cfg.get("sessions") or {}).get("secrets") or []), {}
    for plan in (cfg.get("plans") or {}).values():
        for step in (plan.get("steps") or []) if isinstance(plan, dict) else []:
            names |= set(step.get("secrets") or []) if isinstance(step, dict) else set()
    try:
        plan = json.loads((Path(run_dir) / "plan.json").read_text())
    except (OSError, ValueError):
        plan = {}
    for step in plan.get("steps") or [] if isinstance(plan, dict) else []:
        if not isinstance(step, dict):
            continue
        names |= {str(n) for n in step.get("secrets") or []}
        for k, v in (step.get("env") or {}).items() if isinstance(step.get("env"), dict) else ():
            if len(str(v)) >= MIN_ENV:
                values[f"env {k}"] = str(v)
    for name in names:
        if len(os.environ.get(name, "")) >= MIN_SECRET:
            values[f"secret {name}"] = os.environ[name]
    for name, v in os.environ.items():
        if SECRET_NAME.search(name) and len(v) >= MIN_ENV and v not in values.values():
            values[f"${name}"] = v
    return values


def unchecked_secrets(run_dir: Path, rep: dict) -> dict[str, list[str]]:
    """{a step's folder: the secrets it names that are not set here}. Their values cannot be looked for (a Modal
    secret, one set in another shell), so the step's text evidence is left out and its output kept off the page."""
    try:
        plan = json.loads((Path(run_dir) / "plan.json").read_text())
    except (OSError, ValueError):
        plan = {}
    named = {str(s.get("name")): [str(n) for n in s.get("secrets") or []
                                  if len(os.environ.get(str(n), "")) < MIN_SECRET]
             for s in (plan.get("steps") or [] if isinstance(plan, dict) else []) if isinstance(s, dict)}
    return {str(s.get("dir")): named[str(s.get("name"))] for s in rep.get("steps") or []
            if isinstance(s, dict) and named.get(str(s.get("name")))}


def _without_output(rep: dict, unchecked: dict) -> dict:
    """The report with what the steps of unchecked secrets printed taken out (it could hold their values)."""
    if not unchecked:
        return rep
    out = json.loads(json.dumps(rep, default=str))
    for s in out.get("steps") or []:
        names = unchecked.get(str(s.get("dir"))) if isinstance(s, dict) else None
        if not names:
            continue
        note = f"(not published: the step uses {', '.join(names)}, which was not set where it was shared)"
        if s.get("stdout_tail"):
            s["stdout_tail"] = note
        if isinstance(s.get("error"), dict):
            s["error"] = {k: v for k, v in s["error"].items() if k in ("ename", "cell", "line", "source_head")}
            s["error"]["evalue"] = note
        if isinstance(s.get("web"), dict):
            s["web"] = {k: v for k, v in s["web"].items() if k == "screenshots"}
    return out


# ---- the bundle ----------------------------------------------------------------------------------------------------

def _why(path: str, rep: dict) -> str:
    """Why an evidence file is in the bundle, in a few words."""
    parts = path.split("/")
    step = next((s for s in rep.get("steps") or [] if isinstance(s, dict) and s.get("dir") == "/".join(parts[:2])), {})
    inner = "/".join(parts[2:])
    images = set(step.get("images") or []) | set((step.get("web") or {}).get("screenshots") or [])
    if inner in images:
        return "an image the page shows" if inner in (step.get("images") or []) else "a screenshot of the page"
    if inner.startswith("artifacts/"):
        kept = inner[len("artifacts/"):]
        read = any(kept in str(c.get("check", "")) for c in step.get("expectations") or [] if isinstance(c, dict))
        if read:
            return "a file the step kept, which its checks read"
        return "an image the step kept (the page shows it)" if kept.lower().endswith(IMAGES) else \
            "a file the step kept (artifacts:)"
    return {"log.txt": "the step's output", "result.json": "the step's result as testbench recorded it",
            "executed.ipynb": "the notebook as it ran, with its outputs", "data.json": "what the step's data files held",
            "mock_calls.jsonl": "every call the code made to the mock SDK",
            "upload_queue.json": "the files the notebook was given to upload"}.get(
        inner, "a file the notebook offered for download" if inner.startswith("downloads/") else "a file the step left")


def _header(rep: dict, alt: str, badge_svg: str) -> str:
    e = html.escape
    w = re.search(r'width="(\d+)"', badge_svg)
    return (f"<p class=muted><img src='badge.svg' alt='{e(alt)}' width={w.group(1) if w else 120} height=20 "
            f"style='vertical-align:middle'> One run of plan <code>{e(str(rep.get('plan')))}</code>, published with "
            f"<a href='{DOCS}'>testbench share</a>. Every file here is listed with its sha256 in "
            f"<a href='{MANIFEST}'>{MANIFEST}</a>, and each evidence file matches the sha256 the run recorded when it "
            "ended. To check it yourself: <code>pip install agent-testbench</code>, then <code>testbench verify "
            "--from URL</code>, with this page's address as URL.</p>")


def build(root: Path, cfg: dict, run_dir: Path, out: Path, keep_paths: bool = False,
          max_file_mb: float = MAX_FILE_MB) -> dict:
    """Write the bundle of a finished run into the empty folder out; what it holds, what was left out and why, and
    what was scrubbed. ShareError when the run cannot be shared (not finished, or its evidence changed)."""
    run_dir, out = Path(run_dir), Path(out)
    rep = R.load(run_dir)
    if rep is None or rep.get("status") not in runs.DONE:
        state = runs.status_line(run_dir)
        raise ShareError(f"{run_dir.name} has not finished, so there is no proof to share yet: {state}. "
                         f"`testbench wait {run_dir.name}` follows it to the end.")
    if not isinstance(rep.get("evidence"), list) or not rep.get("evidence_sha256"):
        raise ShareError(f"{run_dir.name} has no recorded evidence hashes (it ran before testbench recorded them), so "
                         f"a bundle of it could not be checked. Run the plan again: `testbench run {rep.get('plan')}`")
    problems = provenance.verify(run_dir, rep)
    if problems:
        raise ShareError(f"{run_dir.name}'s evidence no longer matches its report, so it proves nothing to share:\n  "
                         + "\n  ".join(problems) + f"\n`testbench verify {run_dir.name}` says the same.", code=1)
    scrub = Scrub(root, private_values(root, cfg, run_dir))
    unchecked = unchecked_secrets(run_dir, rep)
    limit = int(max_file_mb * 1024 * 1024)
    files, left, published = [], [], set()
    for entry in rep["evidence"]:
        path, src = entry["path"], run_dir / entry["path"]
        why_not = never(path) or ("" if safe_path(path) and R.inside(run_dir, src) else
                                  "its name cannot be published as it is (a hidden part, or characters a URL reads)")
        secret = unchecked.get("/".join(path.split("/")[:2]))
        if not why_not and secret and not path.lower().endswith(IMAGES[:-1]):
            why_not = (f"its step uses {', '.join(secret)}, which is not set here, so the file could not be searched "
                       "for its value (set it, then share again)")
        if not why_not and src.stat().st_size > limit:
            why_not = f"larger than {max_file_mb:g} MB (--max-file-mb)"
        data = b""
        if not why_not:
            data = src.read_bytes()
            if _sha256(data) != entry["sha256"]:
                raise ShareError(f"{path} changed while it was read; nothing was published. Try again.", code=1)
            hits = scrub.found(data)
            if hits["value"]:
                why_not = "holds " + ", ".join(sorted(hits["value"])) + " (never published)"
            elif hits["path"] and not keep_paths:
                why_not = "holds " + ", ".join(sorted(hits["path"])) + " (--keep-paths publishes it as it is)"
        if why_not:
            left.append({"path": path, "bytes": src.stat().st_size if src.is_file() else 0, "why": why_not})
            continue
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_bytes(data)
        files.append({"path": path, "bytes": len(data), "sha256": entry["sha256"], "why": _why(path, rep)
                      + (" (it holds this machine's paths: --keep-paths)" if keep_paths and scrub.found(data)["path"]
                         else "")})
        published.add(path)
    counts = collections.Counter()
    words, color = B.message(rep)
    badge_svg, alt = B.svg(words, color), f"{B.LABEL}: {words}"
    title = f"{R.ICON.get(rep['status'], rep['status'])} {rep.get('plan')} {rep.get('run_id')}"
    raw = (run_dir / "report.json").read_bytes()
    clean = scrub.json(_without_output(rep, unchecked), counts, keep=("evidence", "evidence_sha256"))
    scrubbed = clean != rep
    try:
        body = R.html_body(clean, run_dir, published)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
        raise ShareError(f"{run_dir.name}'s report.json cannot be made into a page ({type(exc).__name__}: {exc}): it "
                         "was cut short or edited by hand. `testbench verify` checks its evidence.", code=1) from None
    page = scrub.text(R.page(title, _header(rep, alt, badge_svg) + body), counts)
    report_bytes = (json.dumps(clean, indent=1, default=str) + "\n").encode() if scrubbed else raw
    own = [("index.html", page.encode("utf-8"), "the report as one page: what ran, the checks and what they found, "
                                                 "the evidence"),
           ("report.json", report_bytes, "the report as data, with the sha256 the run recorded for each evidence file"
            + (" (scrubbed)" if scrubbed else "")),
           ("badge.svg", badge_svg.encode(), f"a README badge: {alt}")]
    for name, data, _ in own:
        (out / name).write_bytes(data)
    files = [{"path": name, "bytes": len(data), "sha256": _sha256(data), "why": why} for name, data, why in own] + files
    src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
    manifest = {"testbench_share": FORMAT, "made_by": f"agent-testbench {__version__}",
                "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "run": {"id": rep.get("run_id"), "plan": rep.get("plan"), "status": rep.get("status"),
                        "started": rep.get("started"), "commit": src.get("commit"), "badge": words},
                "evidence_sha256": rep["evidence_sha256"], "evidence_files": len(rep["evidence"]),
                "files": files, "left_out": left,
                "scrubbed": [{"what": label, "as": as_, "times": n} for (label, as_, _), n in counts.items()],
                "report_json": "scrubbed" if scrubbed else "as recorded",
                "how_to_check": "testbench verify --from URL_OR_FOLDER (pip install agent-testbench)"}
    text = json.dumps(manifest, indent=1) + "\n"
    (out / MANIFEST).write_text(text, encoding="utf-8")
    return {"run": run_dir.name, "report": rep, "dir": out, "files": files, "left_out": left, "scrubbed": counts,
            "manifest_bytes": len(text.encode()), "badge": (alt, words), "keep_paths": keep_paths}


def listing(b: dict, where: str) -> str:
    """Every file, its size and why; what was left out and why; what was scrubbed. Printed before anything goes out."""
    rows = b["files"] + [{"path": MANIFEST, "bytes": b["manifest_bytes"],
                          "why": "the sha256 of every file here, for `testbench verify --from`"}]
    width = min(56, max(len(r["path"]) for r in rows + b["left_out"]))
    line = lambda r: f"  {r['path']:<{width}}  {size(r['bytes']):>9}  {r['why']}"      # noqa: E731
    rep = b["report"]
    out = [f"Run {b['run']} (plan {rep.get('plan')}: {b['badge'][1]}) to {where}:", ""] + [line(r) for r in rows]
    out += ["", f"{counted(len(rows), 'file')}, {size(sum(r['bytes'] for r in rows))} in all; "
                f"{len(b['files']) - 3} of the run's {counted(len(rep['evidence']), 'evidence file')}."]
    if b["left_out"]:
        out += ["", "Left out:"] + [line(r) for r in b["left_out"]]
        if any("--keep-paths" in r["why"] for r in b["left_out"]):
            out += ["An evidence file is published byte for byte or not at all (its sha256 is the proof), so one that "
                    "holds this machine's paths is left out."]
    out += ["", "Never published: state.json, plan.json, the ledgers, limits, NOTES.md, .env files and keys, or any "
                "file holding a secret's value, a step's env: value or a token in this environment."]
    if b["scrubbed"]:
        out += ["", "Scrubbed from index.html and report.json:"]
        out += [f"  {label}" + (f" {shown}" if shown else "") + f" -> {as_} ({counted(n, 'time')})"
                for (label, as_, shown), n in b["scrubbed"].items()]
    return "\n".join(out)


# ---- where it goes -------------------------------------------------------------------------------------------------

def destination(dest: str, root: Path):
    """The place DEST names: Folder, GitBranch or S3."""
    if dest.startswith("git:"):
        return GitBranch(dest[4:], root)
    if dest.lower().startswith("s3://"):
        return S3(dest)
    if re.match(r"^[a-z][a-z0-9+.-]*://", dest, re.I):
        raise ShareError(f"--to {dest}: testbench share publishes to a folder (--to ./public), a branch of this git "
                         "repository (--to git:gh-pages/runs) or S3 (--to s3://bucket/prefix). For anywhere else, share "
                         "to a folder and upload that folder.")
    return Folder(dest, root)


class Folder:
    def __init__(self, dest: str, root: Path):
        self.base, self.root = Path(dest).expanduser(), Path(root)

    def describe(self, run: str) -> str:
        return f"{self.base / run}{os.sep} (a folder on this machine)"

    def check(self, run: str) -> str:
        target = self.base / run
        if target.resolve().is_relative_to((self.root / ".testbench").resolve()):
            return (f"{target} is inside .testbench, where testbench keeps its runs and their evidence; pick a folder "
                    "outside it, such as ./public")
        if self.base.exists() and not self.base.is_dir():
            return f"{self.base} is a file, not a folder"
        if target.exists() and not target.is_dir():
            return f"{target} is a file, not a folder"
        if target.is_dir():
            old = _manifest_paths(target / MANIFEST)
            there = {p.relative_to(target).as_posix() for p in target.rglob("*")
                     if p.is_file() and not any(x.startswith(".") for x in p.relative_to(target).parts)}
            if old is None and there:
                return (f"{target} holds files that are not a testbench share; nothing there is replaced. Pick another "
                        "--to, or empty that folder.")
            if old is not None and there - old - {MANIFEST}:
                return (f"{target} holds files its {MANIFEST} does not list "
                        f"({', '.join(sorted(there - old - {MANIFEST})[:3])}); nothing there is replaced. Remove "
                        "them, or pick another --to.")
        return ""

    def publish(self, bundle: Path, run: str) -> tuple[list[str], str | None]:
        target = self.base / run
        old = _manifest_paths(target / MANIFEST) if target.is_dir() else None
        for p in sorted(old or (), reverse=True):                    # a share of this run made before: replaced
            if (target / p).resolve().is_relative_to(target.resolve()):
                (target / p).unlink(missing_ok=True)
        if old is not None:
            (target / MANIFEST).unlink(missing_ok=True)
            for d in sorted((d for d in target.rglob("*") if d.is_dir()), reverse=True):
                if not any(d.iterdir()):
                    d.rmdir()
        shutil.copytree(bundle, target, dirs_exist_ok=True)
        page = (target / "index.html").resolve()
        return [f"Published {run} to {target}{os.sep}.", f"Open {page.as_uri()} - or put that folder on any static web "
                "host (GitHub Pages, Netlify, an S3 bucket): it is plain files.",
                f"Check it: testbench verify --from {target}"], None


def _manifest_paths(p: Path) -> set | None:
    try:
        m = json.loads(p.read_text(encoding="utf-8"))
        return {f["path"] for f in m["files"] if safe_path(f.get("path"))}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class S3:
    def __init__(self, dest: str):
        m = re.fullmatch(r"s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])(?:/(.*))?", dest, re.I)
        if not m:
            raise ShareError(f"--to {dest}: an S3 place is s3://BUCKET or s3://BUCKET/PREFIX, e.g. s3://my-bucket/runs")
        self.bucket, self.prefix = m.group(1), (m.group(2) or "").strip("/")
        if self.prefix and not safe_path(self.prefix):
            raise ShareError(f"--to {dest}: the prefix {self.prefix!r} has a hidden part, .. or characters a URL "
                             "reads twice; use letters, digits, - _ . and /")
        self.aws = shutil.which("aws")

    def key(self, run: str) -> str:
        return f"{self.prefix}/{run}" if self.prefix else run

    def describe(self, run: str) -> str:
        return f"s3://{self.bucket}/{self.key(run)}/ (with the aws CLI, as it is signed in)"

    def check(self, run: str) -> str:
        if not self.aws:
            return ("the aws CLI is not on PATH, and testbench uploads to S3 only through it (it never handles the "
                    "credentials itself). Install it (https://aws.amazon.com/cli/) and sign in with `aws configure` "
                    "or `aws sso login`; or share to a folder and upload that.")
        return ""

    def publish(self, bundle: Path, run: str) -> tuple[list[str], str | None]:
        target = f"s3://{self.bucket}/{self.key(run)}/"
        try:
            r = subprocess.run([self.aws, "s3", "cp", str(bundle), target, "--recursive", "--only-show-errors"],
                               capture_output=True, text=True, timeout=3600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ShareError(f"aws s3 cp did not finish ({exc}); some files may be there already: "
                             f"`aws s3 ls {target}` lists them.", code=1) from None
        if r.returncode != 0:
            said = "\n  ".join((r.stderr.strip() or r.stdout.strip() or f"exit code {r.returncode}").splitlines()[-6:])
            raise ShareError(f"aws s3 cp could not upload to {target} (exit code {r.returncode}). aws said:\n  {said}\n"
                             "`aws sts get-caller-identity` says who aws is signed in as.", code=1)
        url = f"https://{self.bucket}.s3.amazonaws.com/{self.key(run)}/"
        return [f"Published {run} to {target} with aws s3 cp.",
                f"On AWS the page is {url}index.html once the bucket allows public reads (another S3-compatible "
                "service has its own address).", f"Check it: testbench verify --from {url}"], url


class GitBranch:
    """A folder of a branch of the project's git repository. Committed with plumbing (hash-object, a temporary index,
    write-tree, commit-tree, update-ref): no checkout, so the working tree, the index and HEAD stay as they are."""

    def __init__(self, spec: str, root: Path):
        self.root = Path(root)
        top = self._git("rev-parse", "--show-toplevel", cwd=self.root, check=False)
        if top.returncode != 0:
            raise ShareError(f"--to git:{spec}: {self.root} is not in a git repository, so there is no branch to commit "
                             "to. Share to a folder instead (--to ./public), or `git init` first.")
        self.top = Path(top.stdout.strip())
        parts = [p for p in spec.split("/") if p]
        if not parts:
            raise ShareError("--to git: names a branch, and a folder in it if you like: --to git:gh-pages/runs")
        cut = next((k for k in range(len(parts), 0, -1)
                    if self._git("rev-parse", "--verify", "-q", f"refs/heads/{'/'.join(parts[:k])}",
                                 check=False).returncode == 0), 1)
        self.branch, self.folder = "/".join(parts[:cut]), "/".join(parts[cut:])
        if self.branch.startswith("-") or \
                self._git("check-ref-format", "--branch", self.branch, check=False).returncode != 0:
            raise ShareError(f"--to git:{spec}: {self.branch!r} cannot be a branch name")
        if self.folder and not safe_path(self.folder):
            raise ShareError(f"--to git:{spec}: the folder {self.folder!r} has a hidden part, .. or characters a URL "
                             "reads twice; use letters, digits, - _ . and /")

    def _git(self, *args, cwd=None, env=None, input=None, check=True):
        try:
            r = subprocess.run(["git", *args], cwd=cwd or getattr(self, "top", self.root), capture_output=True,
                               text=True, timeout=300, env=env, input=input)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ShareError(f"git {args[0]} did not run ({exc}); is git installed?") from None
        if check and r.returncode != 0:
            raise ShareError(f"git {' '.join(args[:2])} failed: {r.stderr.strip() or r.stdout.strip()}. Nothing was "
                             f"committed; branch {getattr(self, 'branch', '')} is as it was.", code=1)
        return r

    def path(self, run: str) -> str:
        return f"{self.folder}/{run}" if self.folder else run

    def describe(self, run: str) -> str:
        return f"branch {self.branch}, folder {self.path(run)}/ of {self.top} (committed there, not pushed)"

    def check(self, run: str) -> str:
        listed = self._git("worktree", "list", "--porcelain").stdout
        for block in listed.strip().split("\n\n"):
            lines = dict(line.split(" ", 1) for line in block.splitlines() if " " in line)
            if lines.get("branch") == f"refs/heads/{self.branch}":
                return (f"branch {self.branch} is checked out in {lines.get('worktree')}, and testbench share commits "
                        "only to a branch nobody has checked out (so no working tree changes under anyone). Share to "
                        "another branch, or switch that worktree to another one.")
        return ""

    def publish(self, bundle: Path, run: str) -> tuple[list[str], str | None]:
        problem = self.check(run)
        if problem:
            raise ShareError(problem)
        ref = f"refs/heads/{self.branch}"
        found = self._git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        parent = found.stdout.strip() if found.returncode == 0 else ""
        where = self.path(run)
        files = sorted(p for p in bundle.rglob("*") if p.is_file())
        with tempfile.TemporaryDirectory(prefix="testbench-share-") as tmp:
            env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if parent:
                self._git("read-tree", parent, env=env)
                old = self._git("ls-files", "-z", "--", where, env=env).stdout.split("\0")
                gone = "".join(f"0 {'0' * 40}\t{p}\n" for p in old if p)
                if gone:
                    self._git("update-index", "--index-info", env=env, input=gone)
            blobs = self._git("hash-object", "-w", "--no-filters", "--stdin-paths",
                              input="".join(f"{p}\n" for p in files)).stdout.split()
            entries = [f"100644 {sha}\t{where}/{p.relative_to(bundle).as_posix()}\n" for p, sha in zip(files, blobs)]
            if not parent:                                  # a new branch: GitHub Pages serves the files as they are
                empty = self._git("hash-object", "-w", "--stdin", input="").stdout.strip()
                entries.append(f"100644 {empty}\t.nojekyll\n")
            self._git("update-index", "--add", "--index-info", env=env, input="".join(entries))
            tree = self._git("write-tree", env=env).stdout.strip()
        if parent and tree == self._git("rev-parse", f"{parent}^{{tree}}").stdout.strip():
            return [f"Branch {self.branch} already holds this bundle at {where}/; nothing new to commit."] \
                + self._after(run), self._url(run)
        rep = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
        words = json.loads((bundle / MANIFEST).read_text(encoding="utf-8"))["run"]["badge"]
        message = (f"testbench share: run {run} ({rep.get('plan')}: {words})\n\n"
                   f"evidence_sha256 {rep.get('evidence_sha256')}\n"
                   f"Check it: testbench verify --from {where}/\n")
        env = dict(os.environ)
        if any(self._git("var", f"GIT_{who}_IDENT", check=False).returncode != 0 for who in ("AUTHOR", "COMMITTER")):
            env.update(GIT_AUTHOR_NAME="testbench share", GIT_AUTHOR_EMAIL="testbench-share@localhost",
                       GIT_COMMITTER_NAME="testbench share", GIT_COMMITTER_EMAIL="testbench-share@localhost")
        commit = self._git("commit-tree", tree, *(["-p", parent] if parent else []), "-F", "-", env=env,
                           input=message).stdout.strip()
        self._git("update-ref", "-m", f"testbench share {run}", ref, commit, parent)
        return [f"Committed {run} to branch {self.branch}, folder {where}/ (commit {commit[:12]}); your working tree, "
                "index and current branch are as they were."] + self._after(run), self._url(run)

    def _remote(self) -> str:
        names = self._git("remote", check=False).stdout.split()
        return "origin" if "origin" in names else (names[0] if names else "")

    def _url(self, run: str) -> str | None:
        remote = self._remote()
        out = self._git("remote", "get-url", remote, check=False).stdout.strip() if remote else ""
        m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", out)
        if not m:
            return None
        owner, repo = m.group(1), m.group(2)
        user_site = repo.lower() == f"{owner.lower()}.github.io"           # OWNER.github.io serves from its root
        site = f"https://{owner.lower()}.github.io/" + ("" if user_site else f"{repo}/")
        return site + self.path(run) + "/"

    def _after(self, run: str) -> list[str]:
        remote = self._remote()
        push = f"git push {remote} {self.branch}" if remote else \
            f"git remote add origin URL && git push origin {self.branch}"
        url = self._url(run)
        if url:
            return [f"Nothing was pushed. To publish it, a person runs: {push}",
                    f"On GitHub Pages (Settings > Pages: deploy from branch {self.branch}) the page will be {url}",
                    f"Check it once it is served: testbench verify --from {url}"]
        return [f"Nothing was pushed. To publish it, a person runs: {push}",
                f"Check it from a checkout of branch {self.branch}: testbench verify --from {self.path(run)}/"]


# ---- share ---------------------------------------------------------------------------------------------------------

def share(root: Path, cfg: dict, run: str | None, dest: str, *, dry_run: bool = False, yes: bool = False,
          keep_paths: bool = False, max_file_mb: float = MAX_FILE_MB, ask=input, tty=None, say=print) -> int:
    """`testbench share`: build the bundle, list it, and publish it when a person said yes. The exit code."""
    run_dir = runs.resolve(root, run)
    place = destination(dest, root)
    with tempfile.TemporaryDirectory(prefix="testbench-share-") as tmp:
        bundle = build(root, cfg, run_dir, Path(tmp), keep_paths, max_file_mb)
        say(listing(bundle, place.describe(run_dir.name)))
        problem = place.check(run_dir.name)
        if dry_run:
            say("\nNothing was published (--dry-run)." + (f" Publishing there would be refused: {problem}" if problem
                                                          else f" To publish it, a person runs: testbench share "
                                                               f"{run_dir.name} --to {dest}"))
            return 0
        if problem:
            raise ShareError(problem)
        if not yes:
            interactive = sys.stdin.isatty() if tty is None else tty
            if not interactive:
                raise ShareError("Nothing was published: publishing puts these files where others can read them, so it "
                                 "takes a person's yes. Run it in a terminal and answer y, or add --yes if a person "
                                 "read this list and agreed (an agent never adds it on its own).")
            if ask(f"\nPublish these files to {place.describe(run_dir.name)}? Anyone who can read it sees them. "
                   "[y/N] ").strip().lower() not in ("y", "yes"):
                say("Nothing was published.")
                return 2
        lines, url = place.publish(Path(tmp), run_dir.name)
    for line in lines:
        say(line)
    if url:
        say(f"README badge: {B.markdown(bundle['badge'][0], url + 'badge.svg', url)}")
    return 0


# ---- verify --from -------------------------------------------------------------------------------------------------

class _Folder:
    def __init__(self, source: str):
        base = Path(source).expanduser()
        if not base.exists():
            raise ShareError(f"{source} does not exist: give the folder of a bundle (the one with index.html and "
                             f"{MANIFEST}), or its URL")
        self.base = base.parent if base.is_file() else base
        self.name = str(self.base)

    def read(self, path: str, most: int) -> bytes | None:
        p = self.base / path
        if not p.is_file():
            return None
        with open(p, "rb") as f:
            return f.read(most + 1)

    def everything(self) -> set | None:
        """Every file in the folder but hidden ones (.DS_Store, .nojekyll): a file the manifest does not list."""
        return {p.relative_to(self.base).as_posix() for p in self.base.rglob("*")
                if p.is_file() and not any(x.startswith(".") for x in p.relative_to(self.base).parts)}

    def missing_manifest(self) -> str:
        inside = sorted(p.parent.name for p in self.base.glob(f"*/{MANIFEST}"))
        if inside:
            return (f"{self.base} holds shared runs in folders of their own ({', '.join(inside[:4])}); name one: "
                    f"testbench verify --from {self.base / inside[-1]}")
        return (f"{self.base} has no {MANIFEST}, so it is not a bundle `testbench share` made (or not all of one was "
                "downloaded)")


class _Url:
    def __init__(self, source: str):
        parts = urllib.parse.urlsplit(source)
        path = parts.path if parts.path.endswith("/") else (
            parts.path.rsplit("/", 1)[0] + "/" if "." in parts.path.rsplit("/", 1)[-1] else parts.path + "/")
        self.base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        self.name = self.base

    def read(self, path: str, most: int) -> bytes | None:
        url = self.base + urllib.parse.quote(path, safe="/")
        req = urllib.request.Request(url, headers={"User-Agent": f"agent-testbench/{__version__} (verify)"})
        local = urllib.parse.urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1")    # no proxy reaches it
        opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))
        try:
            with opener.open(req, timeout=60) as r:
                return r.read(most + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404, 410):
                return None
            raise ShareError(f"{url} answered HTTP {exc.code}; try again later") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ShareError(f"{url} could not be read ({getattr(exc, 'reason', exc)}); is the address right, and "
                             "is it reachable from here?") from None

    def everything(self) -> None:
        return None                                       # a web server lists nothing

    def missing_manifest(self) -> str:
        return (f"{self.base}{MANIFEST} is not there, so {self.base} is not a bundle `testbench share` made (or it was "
                "published without its manifest). Give the address of the folder that holds index.html")


def _listed(m) -> list | None:
    """The manifest's files when each is {path, bytes, sha256} with a path inside the bundle; else None."""
    files = m.get("files") if isinstance(m, dict) else None
    ok = isinstance(files, list) and all(
        isinstance(f, dict) and safe_path(f.get("path")) and f["path"] != MANIFEST and isinstance(f.get("bytes"), int)
        and not isinstance(f["bytes"], bool) and f["bytes"] >= 0 and re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256")))
        for f in files)
    return files if ok and len({f["path"] for f in files}) == len(files) else None


def verify_from(source: str) -> tuple[int, list[str]]:
    """`testbench verify --from`: (exit code, lines). 0 it checks out, 1 something does not, 2 it could not be
    checked. Every file against the manifest; report.json's evidence list against its evidence_sha256; each published
    evidence file against the sha256 the run recorded."""
    try:
        reader = _Url(source) if re.match(r"https?://", source, re.I) else _Folder(source)
        raw = reader.read(MANIFEST, 4 << 20)
        if raw is None:
            return 2, [reader.missing_manifest()]
        try:
            m = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return 1, [f"{reader.name}: {MANIFEST} is not JSON: it was cut short or edited"]
        if isinstance(m, dict) and isinstance(m.get("testbench_share"), int) and m["testbench_share"] > FORMAT:
            return 2, [f"{reader.name} was shared by a newer testbench (bundle format {m['testbench_share']}); this one "
                       f"reads format {FORMAT}: pip install -U agent-testbench"]
        files = _listed(m)
        if files is None:
            return 1, [f"{reader.name}: {MANIFEST} does not list its files as path, bytes and sha256, each once and "
                       "inside the bundle: it was edited, or is not testbench's"]
        if sum(f["bytes"] for f in files) > VERIFY_LIMIT:
            return 2, [f"{reader.name}: {MANIFEST} lists more than {size(VERIFY_LIMIT)} of files; download the bundle "
                       "and verify the folder"]
        problems, got = [], {}
        for f in files:
            data = reader.read(f["path"], f["bytes"])
            if data is None:
                problems.append(f"missing: {f['path']}")
            elif len(data) != f["bytes"] or _sha256(data) != f["sha256"]:
                problems.append(f"changed: {f['path']} (its sha256 is not the one in {MANIFEST})")
            else:
                got[f["path"]] = data
        listed = {f["path"] for f in files}
        problems += [f"not in the manifest: {p}" for p in sorted((reader.everything() or set()) - listed - {MANIFEST})]
    except ShareError as exc:
        return 2, [str(exc)]
    rep = None
    if "report.json" in got:
        try:
            rep = json.loads(got["report.json"].decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            problems.append("report.json is not JSON")
    elif "report.json" not in listed:
        problems.append("report.json is not in the manifest, so there is no record of the run to check against")
    wrong = (provenance.check_list(rep) if isinstance(rep, dict) else "report.json is not a report") \
        if rep is not None else ""
    if wrong:
        problems.append(wrong)
    run = "the run"
    if isinstance(rep, dict) and not wrong:
        run = f"run {rep.get('run_id')} (plan {rep.get('plan')}, {rep.get('status')})"
        if m.get("evidence_sha256") != rep["evidence_sha256"]:
            problems.append("the manifest's evidence_sha256 is not report.json's: one of them was edited")
        sums = {e["path"]: e["sha256"] for e in rep["evidence"]}
        evidence = sorted(p for p in listed if p.startswith("steps/"))
        for p in evidence:
            if p not in sums:
                problems.append(f"{p} is not evidence the run recorded")
            elif p in got and _sha256(got[p]) != sums[p]:
                problems.append(f"changed: {p} (its sha256 is not the one the run recorded)")
    if problems or rep is None:
        return 1, [f"{reader.name}: {run} does not check out:"] + [f"  {p}" for p in problems]
    total, left = rep["evidence_sha256"], len(sums) - len(evidence)
    return 0, [f"{reader.name}: {run}: all {counted(len(files), 'file')} the manifest lists match it; "
               f"{len(evidence)} of the run's {counted(len(sums), 'evidence file')} were published, each matching the "
               f"sha256 the run recorded, and the run's list adds up to its evidence_sha256 {total[:16]}"
               + (f" ({left} left out when it was shared: the manifest says why)." if left else "."),
               "This shows the files are the ones published and agree with the report. Someone who rewrote report.json "
               "together with the evidence would pass it too, so compare that evidence_sha256 with the one the run "
               "printed when it ended (in its report.md, the CI log, or the commit that published it)."]


