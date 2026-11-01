"""The GitHub Action's pull request comment and attestation subjects. action.yml runs this file by its path.

It runs on the runner with whatever Python the action uses, from the action's own folder, so it works with any
`install:` (an older agent-testbench from PyPI, or none at all). That is why it uses the standard library only and
imports nothing from the package.

  python github_action.py comment            post the plan's comment on the pull request, or edit it if it exists
  python github_action.py attest-subjects    write the checksums file that actions/attest-build-provenance signs

Every value comes from the environment: the step's env: and the runner's GITHUB_* variables. The token is read
from TESTBENCH_GITHUB_TOKEN and sent only in the Authorization header of requests to GITHUB_API_URL. It is never
printed or written to a file, and never sent to the storage host an artifact download is redirected to. When a
comment cannot be posted (a fork's read-only token, a missing permission, GitHub down), the job summary gets a
notice and the job does not fail.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

UA = "agent-testbench-action"
VERDICT = {"passed": "PASS", "failed": "FAIL", "error": "ERROR", "cancelled": "CANCELLED", "refused": "REFUSED"}
MAX_ROWS = 30                    # numbers in the comment's table; the job summary has the rest
MAX_BODY = 60_000                # GitHub takes up to 65536 characters in a comment
ARTIFACT_LIMIT = 100 * 2**20     # the largest base-branch artifact downloaded for a comparison
SUBJECT_LIMIT = 1024             # what one attestation can hold (actions/attest)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def env(key: str) -> str:
    return os.environ.get(key) or ""


def _counted(n: int, one: str) -> str:
    """config.counted, here since this file imports nothing from the package: "1 file", "3 files"."""
    return f"{n} {one}{'' if n == 1 else 's'}"


# ---- what the runner says about this job ---------------------------------------------------------------------------

def pull_request() -> dict | None:
    """The pull request this job runs for, or None (a push, a schedule, a manual run). read_only says why GitHub
    gave the job a token that cannot write (a fork, Dependabot)."""
    event = env("GITHUB_EVENT_NAME")
    if event not in ("pull_request", "pull_request_target"):
        return None
    try:
        payload = json.loads(Path(env("GITHUB_EVENT_PATH")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pr = payload.get("pull_request") if isinstance(payload, dict) else None
    if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
        return None
    head = ((pr.get("head") or {}).get("repo") or {}).get("full_name") or ""
    base = ((pr.get("base") or {}).get("repo") or {}).get("full_name") or env("GITHUB_REPOSITORY")
    read_only = ""
    if event == "pull_request" and head != base:      # pull_request_target runs in the base repository: it can write
        read_only = (f"this pull request comes from the fork {head}" if head else
                     "this pull request comes from a fork that was deleted")
        read_only += ", and GitHub gives workflows on pull requests from forks a read-only token and no id-token"
    elif event == "pull_request" and env("GITHUB_ACTOR") == "dependabot[bot]":
        read_only = "GitHub gives workflows on Dependabot's pull requests a read-only token and no id-token"
    return {"number": pr["number"], "base_ref": (pr.get("base") or {}).get("ref") or env("GITHUB_BASE_REF"),
            "head_repo": head, "read_only": read_only}


def output(**values) -> None:
    path = env("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for key, value in values.items():
            end = f"EOF_{uuid.uuid4().hex}"
            f.write(f"{key.replace('_', '-')}<<{end}\n{value}\n{end}\n")


def note(text: str) -> None:
    """A line for whoever reads the job summary (and the summary file the action outputs)."""
    for key in ("GITHUB_STEP_SUMMARY", "SUMMARY_FILE"):
        if env(key):
            with open(env(key), "a", encoding="utf-8") as f:
                f.write(f"\n> {text}\n")


def annotate(level: str, text: str) -> None:
    text = text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::{level} title=agent-testbench::{text}", flush=True)


# ---- the GitHub REST API, with the standard library ----------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                 # the caller decides: an artifact's storage host must not get the token


class GitHub:
    def __init__(self, token: str, api: str = "", timeout: float = 30):
        self.api = (api or env("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
        self.timeout = timeout
        self._token = token
        self._open = urllib.request.build_opener(_NoRedirect).open

    def _request(self, method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            return self._open(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                return e
            message = ""
            try:
                message = str(json.loads(e.read() or b"{}").get("message") or "")
            except (ValueError, AttributeError, OSError):
                pass
            raise ApiError(e.code, message[:300] or str(e.reason)) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(0, f"could not reach {self.api}: {getattr(e, 'reason', e)}") from None

    def call(self, method: str, path: str, body=None):
        with self._request(method, path, body) as r:
            raw = r.read()
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            raise ApiError(0, f"{method} {path.split('?')[0]} did not answer JSON") from None

    def download(self, path: str, dest: Path, limit: int) -> None:
        """Save what path answers to dest. GitHub answers an artifact with a redirect to its storage host, which
        is fetched without the token."""
        r = self._request("GET", path)
        try:
            if r.getcode() in (301, 302, 303, 307, 308):
                where = r.headers.get("Location") or ""
                if not where.startswith(("https://", "http://")):
                    raise ApiError(0, "the artifact download was redirected nowhere")
                r.close()
                try:
                    r = urllib.request.urlopen(urllib.request.Request(where, headers={"User-Agent": UA}),
                                               timeout=self.timeout)
                except (urllib.error.URLError, OSError, ValueError) as e:      # not GitHub's answer: status 0
                    raise ApiError(0, f"could not download the artifact: {getattr(e, 'reason', e)}") from None
            got = 0
            with open(dest, "wb") as f:
                for chunk in iter(lambda: r.read(1 << 20), b""):
                    got += len(chunk)
                    if got > limit:
                        raise ApiError(0, f"the artifact is over {limit // 2**20} MB")
                    f.write(chunk)
        finally:
            r.close()


# ---- what the comment says -----------------------------------------------------------------------------------------

def code(text, limit: int = 200, table: bool = False) -> str:
    """Text as an inline code span on one line, so nothing in it (a step name from the pull request's
    testbench.yaml) becomes markdown, a link, HTML or an @mention. In a table, | is escaped."""
    s = " ".join(str(text).split())
    if len(s) > limit:
        s = s[:limit - 3] + "..."
    if not s:
        return "(empty)"
    if table:
        s = s.replace("|", "\\|")
    ticks = max((len(m) for m in re.findall(r"`+", s)), default=0)
    pad = " " if ticks else ""
    return f"{'`' * (ticks + 1)}{pad}{s}{pad}{'`' * (ticks + 1)}"


def url(u: str) -> str:
    return u if re.fullmatch(r"https?://[^\s()<>\[\]\"']+", u or "") else ""


def check_number(c: dict) -> float | None:
    """The number a check found (its detail starts with it: "0.93 (wanted >= 0.9)"), as report.check_number."""
    head = str(c.get("detail", "")).split(" (wanted")[0].strip().strip("'\"")
    try:
        n = float(head)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def numbers(rep: dict) -> list[tuple[str, str, float, bool]]:
    """(step, check, number, passed) for every check that found a number, in the report's order."""
    out = []
    for s in rep.get("steps") or []:
        if not isinstance(s, dict):
            continue
        for c in s.get("expectations") or []:
            if isinstance(c, dict) and (n := check_number(c)) is not None:
                out.append((str(s.get("name")), str(c.get("check")), n, bool(c.get("ok"))))
    return out


def _g(v: float) -> str:
    return f"{v:g}"


def _change(before: float | None, now: float) -> str:
    if before is None:
        return "new"
    return "same" if now == before else f"{now - before:+.4g}"


def _statuses(rep: dict) -> dict:
    return {str(s.get("name")): s.get("status") for s in rep.get("steps") or [] if isinstance(s, dict)}


def failures(rep: dict, cap: int = 12) -> list[str]:
    out = []
    if rep.get("launch_error"):
        out.append(f"- could not start: {code(rep['launch_error'], 400)}")
    for s in rep.get("steps") or []:
        if not isinstance(s, dict) or s.get("status") == "passed":
            continue
        name = code(s.get("name"), 100)
        err = s.get("error") if isinstance(s.get("error"), dict) else {}
        bad = [c for c in s.get("expectations") or [] if isinstance(c, dict) and not c.get("ok")]
        if err:
            out.append(f"- {name}: {code(err.get('ename') or 'error', 80)}: {code(err.get('evalue') or '', 300)}"
                       + (f", raised at {code(err['line'], 200)}" if err.get("line") else ""))
        for c in bad[:3]:
            out.append(f"- {name}: expected {code(c.get('check'))}, found {code(c.get('detail'))}")
        if len(bad) > 3:
            out.append(f"- {name}: and {len(bad) - 3} more missed checks")
        if not err and not bad:
            out.append(f"- {name}: {re.sub(r'[^a-z]', '', str(s.get('status')))}")
    skipped = [x for x in rep.get("skipped") or [] if isinstance(x, str)]
    if skipped:
        out.append("- not run, because an earlier step did not pass: " + ", ".join(code(x, 80) for x in skipped[:10])
                   + (f" and {len(skipped) - 10} more" if len(skipped) > 10 else ""))
    if len(out) > cap:
        out = out[:cap] + [f"- and {len(out) - cap} more in the job summary"]
    return out


def _ran(rep: dict, plan: str, status: str) -> str:
    cost, worst = rep.get("cost_usd"), rep.get("worst_case_usd")
    where = str(rep.get("backend") or "?") + (f" ({rep['gpu']})" if rep.get("gpu") else "")
    checks = [c for s in rep.get("steps") or [] if isinstance(s, dict) for c in s.get("expectations") or []
              if isinstance(c, dict)]
    src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
    minutes = rep.get("minutes")
    return (f"Plan {code(plan, 80)} **{status}** on {code(where, 80)}"
            + (f" in {minutes} min" if isinstance(minutes, (int, float)) else "")
            + (f", about ${cost:.3f}" if isinstance(cost, (int, float)) else "")
            + (f" (worst case ${worst:.2f})" if isinstance(worst, (int, float)) else "")
            + (f"; {sum(1 for c in checks if c.get('ok'))} of {_counted(len(checks), 'check')} passed" if checks else "")
            + f". Run {code(rep.get('run_id'), 80)} on "
            + (f"commit `{str(src.get('commit'))[:12]}`" + (" with uncommitted changes" if src.get("dirty") else "")
               if src.get("commit") else "code outside git") + ".")


def _numbers_section(rep: dict, against: dict | None) -> list[str]:
    now = numbers(rep)
    before_rep = (against or {}).get("report")
    out = []
    if before_rep:
        before = {(s, c): v for s, c, v, _ in numbers(before_rep)}
        if now:
            out += [f"**Numbers checked**, compared with {against['label']}:", "",
                    "| | step | check | this run | before | change |", "|---|---|---|---|---|---|"]
            for step, check, value, ok in now[:MAX_ROWS]:
                b = before.get((step, check))
                out.append(f"| {'ok' if ok else '**missed**'} | {code(step, 80, True)} | {code(check, 120, True)} | "
                           f"{_g(value)} | {'-' if b is None else _g(b)} | {_change(b, value)} |")
        prev, cur = _statuses(before_rep), _statuses(rep)
        fixed = [n for n, st in cur.items() if st == "passed" and prev.get(n) not in (None, "passed")]
        broke = [n for n, st in cur.items() if st != "passed" and prev.get(n) == "passed"]
        moved = "; ".join(f"{label} " + ", ".join(code(n, 80) for n in names)
                          for label, names in (("fixed", fixed), ("broke", broke)) if names)
        if moved:
            out += ([""] if now else []) + [f"Steps since {against['label'] if not now else 'then'}: {moved}."]
    elif now:
        why = (against or {}).get("note") or "no earlier run to compare with"
        out += [f"**Numbers checked** ({why}):", "", "| | step | check | this run |", "|---|---|---|---|"]
        out += [f"| {'ok' if ok else '**missed**'} | {code(step, 80, True)} | {code(check, 120, True)} | {_g(value)} |"
                for step, check, value, ok in now[:MAX_ROWS]]
    if len(now) > MAX_ROWS:
        out.append(f"\nAnd {len(now) - MAX_ROWS} more numbers in the job summary.")
    return out + [""] if out else []


def render(plan: str, status: str, rep: dict | None, *, marker: str, message: str = "", against: dict | None = None,
           links: dict | None = None, footer: str = "") -> str:
    """The comment: the verdict, what did not pass, the numbers checked and how they moved, the cost, and links."""
    status = status or "error"
    lines = [marker, f"### {VERDICT.get(status, status.upper())} - plan {code(plan, 80)}", ""]
    if rep:
        lines += [_ran(rep, plan, status), ""]
        bad = failures(rep) if status != "passed" else []
        if bad:
            lines += ["**What did not pass**", ""] + bad + [""]
        elif status != "passed" and message:
            lines += [f"The action says: {code(message, 600)}", ""]
        lines += _numbers_section(rep, against)
    else:
        lead = ("The run was refused before it started" if status == "refused" else "The plan did not run")
        lines += [f"{lead}, so nothing was checked: {code(message or 'the action stopped before testbench ran', 600)}",
                  ""]
    links = links or {}
    parts = []
    if url(links.get("summary", "")):
        parts.append(f"[job summary]({links['summary']}) (the whole report)")
    if url(links.get("artifact", "")):
        name = code(links.get("artifact_name") or "evidence", 100)
        parts.append(f"[evidence]({links['artifact']}) (artifact {name})")
    if url(links.get("attestation", "")):
        parts.append(f"[attestation]({links['attestation']}) (report.json and the evidence, signed by this workflow)")
    if parts:
        lines += ["Links: " + "; ".join(parts) + ".", ""]
    if footer:
        lines += [f"<sub>{footer}</sub>"]
    body = "\n".join(lines).rstrip() + "\n"
    if len(body) > MAX_BODY:
        body = body[:MAX_BODY].rsplit("\n", 1)[0] + "\n\n(cut short; the job summary has the whole report)\n"
    return body


def marker_for(workflow: str, artifact: str) -> str:
    """The hidden first line that finds this plan's comment again: one per workflow and evidence artifact name, so
    one per plan (or per matrix entry, which needs an artifact name of its own anyway)."""
    key = re.sub(r"-{2,}", "-", re.sub(r"[^\w .,:/()+=-]", "-", f"{workflow or 'workflow'}/{artifact or 'testbench'}"))
    return f"<!-- agent-testbench comment: {key[:200]} -->"


# ---- what the comment compares with --------------------------------------------------------------------------------

def _report_from_zip(path: Path) -> dict | None:
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if n.rstrip("/").split("/")[-1] == "report.json"),
                           key=lambda n: (n.count("/"), n))
            if not names or z.getinfo(names[0]).file_size > 50 * 2**20:
                return None
            rep = json.loads(z.read(names[0]))
    except (OSError, ValueError, zipfile.BadZipFile, KeyError, RecursionError):
        return None
    return rep if isinstance(rep, dict) else None


def base_branch_report(gh: GitHub, repo: str, base_ref: str, artifact: str) -> dict:
    """The report of this plan in the latest finished run of this workflow on the base branch, read from that run's
    evidence artifact: {"report", "label"}, or {"note"} saying why there is none."""
    path = env("GITHUB_WORKFLOW_REF").split("@", 1)[0]
    workflow = path.rsplit("/", 1)[-1]
    if not base_ref or not workflow.endswith((".yml", ".yaml")):
        return {"note": "no base branch to compare with"}
    q = urllib.parse.urlencode({"branch": base_ref, "status": "completed", "per_page": 20})
    try:
        runs = (gh.call("GET", f"/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs?{q}") or {})
        looked = 0
        for run in runs.get("workflow_runs") or []:
            if (str(run.get("event", "")).startswith("pull_request") or str(run.get("id")) == env("GITHUB_RUN_ID")
                    or (run.get("head_repository") or {}).get("full_name", repo) != repo):
                continue
            looked += 1
            if looked > 5:
                break
            q2 = urllib.parse.urlencode({"name": artifact, "per_page": 10})
            arts = gh.call("GET", f"/repos/{repo}/actions/runs/{run['id']}/artifacts?{q2}") or {}
            art = next((a for a in arts.get("artifacts") or [] if a.get("name") == artifact and not a.get("expired")),
                       None)
            if not art:
                continue                  # that run did not get as far as this plan; an older one may have
            if (art.get("size_in_bytes") or 0) > ARTIFACT_LIMIT:
                return {"note": f"the latest run on {code(base_ref)} kept {art['size_in_bytes'] // 2**20} MB of "
                                f"evidence, too much to download for a comparison"}
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp) / "evidence.zip"
                gh.download(f"/repos/{repo}/actions/artifacts/{art['id']}/zip", dest, ARTIFACT_LIMIT)
                rep = _report_from_zip(dest)
            if rep:
                src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
                sha = str(src.get("commit") or run.get("head_sha") or "")[:12]
                link = url(str(run.get("html_url") or ""))
                return {"report": rep, "label": f"the latest run on {code(base_ref)}"
                        + (f" ([workflow run]({link})" if link else " (") + f", commit `{sha}`)"}
    except ApiError as e:
        if e.status in (401, 403, 404):
            return {"note": f"to compare with the latest run on {code(base_ref)}, give the job `actions: read`"}
        return {"note": f"could not read the latest run on {code(base_ref)}: {code(str(e), 200)}"}
    return {"note": f"no finished run of this workflow on {code(base_ref)} kept an artifact named {code(artifact)}"}


def previous_in_job(run_dir: Path, rep: dict) -> dict | None:
    """The previous run of the same plan on this runner (a plan run twice in one job), as the report's delta has it."""
    prev = str((rep.get("delta") or {}).get("previous_run") or "")
    if not prev or Path(prev).name != prev:
        return None
    try:
        before = json.loads((run_dir.parent / prev / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(before, dict):
        return None
    return {"report": before, "label": f"the previous run of this plan in this job ({code(prev, 80)})"}


def summary_link(gh: GitHub | None, repo: str) -> str:
    """The job's section of the run's summary page (the run's page when the job cannot be found)."""
    server = (env("GITHUB_SERVER_URL") or "https://github.com").rstrip("/")
    run, attempt = env("GITHUB_RUN_ID"), env("GITHUB_RUN_ATTEMPT") or "1"
    if not (repo and run.isdigit() and attempt.isdigit()):
        return ""
    page = f"{server}/{repo}/actions/runs/{run}/attempts/{attempt}"
    runner = env("RUNNER_NAME")
    if gh and runner:
        try:
            jobs = (gh.call("GET", f"/repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100") or {})
            mine = [j for j in jobs.get("jobs") or [] if j.get("runner_name") == runner
                    and j.get("status") == "in_progress" and str(j.get("id", "")).isdigit()]
            if len(mine) == 1:
                return f"{page}#summary-{mine[0]['id']}"
        except ApiError:
            pass
    return page


# ---- the commands --------------------------------------------------------------------------------------------------

def upsert(gh: GitHub, repo: str, number: int, marker: str, body: str) -> tuple[str, str]:
    """Edit the comment that starts with marker, or post one. Only a comment written by a bot (the token's own,
    github-actions[bot] for GITHUB_TOKEN) or by the token's own user counts, so nobody can plant the marker in a
    comment of theirs and have the report land there."""
    me = ""
    try:
        me = str((gh.call("GET", "/user") or {}).get("login") or "")     # a personal token; GITHUB_TOKEN has none
    except ApiError:
        pass
    for page in range(1, 51):
        items = gh.call("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100&page={page}") or []
        for c in items:
            user = c.get("user") or {}
            first = (str(c.get("body") or "").splitlines() or [""])[0].strip()
            if first == marker and (user.get("type") == "Bot" or (me and user.get("login") == me)):
                done = gh.call("PATCH", f"/repos/{repo}/issues/comments/{c['id']}", {"body": body}) or {}
                return str(done.get("html_url") or ""), "updated"
        if len(items) < 100:
            break
    done = gh.call("POST", f"/repos/{repo}/issues/{number}/comments", {"body": body}) or {}
    return str(done.get("html_url") or ""), "posted"


def cmd_comment() -> int:
    pr = pull_request()
    if not pr and env("GITHUB_EVENT_NAME").startswith("pull_request"):
        why = f"No pull request comment: the event payload ({env('GITHUB_EVENT_PATH') or 'none'}) has no pull request."
        annotate("warning", why)
        note(why)
        return 0
    if not pr:
        print(f"comment: true: this is a {env('GITHUB_EVENT_NAME') or 'local'} event, not a pull request, so there is "
              "no pull request to comment on")
        return 0
    if pr["read_only"]:
        why = f"No pull request comment: {pr['read_only']}. The report is in this summary."
        annotate("notice", why)
        note(why)
        return 0
    repo, token = env("GITHUB_REPOSITORY"), env("TESTBENCH_GITHUB_TOKEN")
    if not token or not repo:
        why = "No pull request comment: no token (the github-token input is empty)."
        annotate("warning", why)
        note(why)
        return 0
    gh = GitHub(token)
    plan, status = env("PLAN"), env("STATUS")
    run_dir = Path(env("RUN_DIR")) if env("RUN_DIR") else None
    rep = None
    if run_dir:
        try:
            rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            rep = None
    rep = rep if isinstance(rep, dict) else None
    artifact = env("ARTIFACT_NAME") or f"testbench-{plan}"
    against = None
    if rep:
        against = base_branch_report(gh, repo, pr["base_ref"], artifact)
        if not against.get("report"):
            against = previous_in_job(run_dir, rep) or against
    message = env("MESSAGE").replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")
    run_id = (rep or {}).get("run_id") or env("RUN_ID")
    sha = env("GITHUB_SHA")[:12]
    now = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
    footer = (f"agent-testbench edits this comment on every push. Updated {now}"
              f" UTC{f' for commit `{sha}`' if re.fullmatch(r'[0-9a-f]+', sha) else ''}.")
    if rep and run_id:
        rid = re.sub(r"[^\w.-]", "-", str(run_id))[:120]
        footer += (f" To check it: unzip the evidence into `.testbench/runs/{rid}/` in the project and run "
                   f"`testbench verify {rid}" + (" --attestation" if url(env("ATTESTATION_URL")) else "") + "`.")
    links = {"summary": summary_link(gh, repo), "artifact": env("ARTIFACT_URL"), "artifact_name": artifact,
             "attestation": env("ATTESTATION_URL")}
    body = render(plan, status, rep, marker=marker_for(env("GITHUB_WORKFLOW"), artifact), message=message,
                  against=against, links=links, footer=footer)
    try:
        where, how = upsert(gh, repo, pr["number"], marker_for(env("GITHUB_WORKFLOW"), artifact), body)
    except ApiError as e:
        if e.status in (401, 403, 404):
            why = (f"No pull request comment: GitHub refused it ({e}). The job needs `permissions: pull-requests: "
                   "write` (and `github-token` a token that may write to this repository's pull requests).")
        else:
            why = f"No pull request comment: {e}. The report is in this summary."
        annotate("warning", why)
        note(why)
        return 0
    output(comment_url=where)
    print(f"{how} the comment on pull request #{pr['number']}: {where}")
    return 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def cmd_attest_subjects() -> int:
    """The checksums file to sign: report.json and every evidence file, after checking the evidence still matches
    the hashes report.json recorded (a report whose evidence does not match is not signed)."""
    output(ready="false")
    run_dir = Path(env("RUN_DIR")) if env("RUN_DIR") else None
    if not run_dir or not (run_dir / "report.json").is_file():
        print("attest: true: no report.json (the run did not start), so there is nothing to attest")
        return 0
    pr = pull_request()
    if pr and pr["read_only"]:
        why = f"No attestation: {pr['read_only']}, which signing needs."
        annotate("notice", why)
        note(why)
        return 0
    if not env("ACTIONS_ID_TOKEN_REQUEST_URL"):
        why = ("No attestation: attest: true needs the job's permissions to include `id-token: write` (to sign) and "
               "`attestations: write` (to store it); this job has no id-token.")
        annotate("error", why)
        note(why)
        return 1
    try:
        rep = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        rep = None
    if not isinstance(rep, dict):
        annotate("error", "No attestation: report.json cannot be read")
        return 1
    steps = run_dir / "steps"
    files = sorted(p for p in steps.rglob("*") if p.is_file()) if steps.is_dir() else []
    now = {p.relative_to(run_dir).as_posix(): _sha256(p) for p in files}
    recorded = rep.get("evidence")
    if isinstance(recorded, list):
        then = {str(e.get("path")): str(e.get("sha256")) for e in recorded if isinstance(e, dict)}
        wrong = sorted(p for p in set(now) | set(then) if now.get(p) != then.get(p))
        if wrong:
            why = (f"No attestation: the evidence no longer matches report.json ({_counted(len(wrong), 'file')}, first "
                   f"{wrong[0]}), so it is not signed. `testbench verify {rep.get('run_id')}` lists them.")
            annotate("error", why)
            note(why)
            return 1
    run_id = re.sub(r"[^\w.-]", "-", str(rep.get("run_id") or run_dir.name))
    subjects = [(_sha256(run_dir / "report.json"), f"{run_id}/report.json")]
    evidence = [(sha, f"{run_id}/" + CONTROL.sub("?", path)) for path, sha in now.items()]  # one line per name
    if len(evidence) + 1 > SUBJECT_LIMIT:
        note(f"The attestation signs report.json only: the run kept {len(evidence)} evidence files, more than one "
             f"attestation holds ({SUBJECT_LIMIT}). report.json records their sha256, which `testbench verify` checks.")
    else:
        subjects += evidence
    work = Path(env("WORK") or tempfile.mkdtemp())
    out = work / "attestation-subjects.txt"
    out.write_text("".join(f"{sha}  {name}\n" for sha, name in subjects), encoding="utf-8")
    output(ready="true", checksums=str(out), subjects=str(len(subjects)))
    print(f"attesting {_counted(len(subjects), 'file')}: report.json and {_counted(len(subjects) - 1, 'evidence file')} "
          f"of run {run_id}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    commands = {"comment": cmd_comment, "attest-subjects": cmd_attest_subjects}
    if len(args) != 1 or args[0] not in commands:
        print(f"usage: github_action.py {{{','.join(commands)}}}", file=sys.stderr)
        return 2
    if args[0] == "comment":
        try:
            return cmd_comment()
        except Exception as e:                  # never fail the job for a comment
            why = f"No pull request comment: {type(e).__name__}: {e}"
            annotate("warning", why)
            note(why)
            return 0
    return commands[args[0]]()


if __name__ == "__main__":
    sys.exit(main())
