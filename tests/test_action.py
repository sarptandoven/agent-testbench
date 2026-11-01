"""The GitHub Action (action.yml).

Its inputs and outputs are the ones docs/github-action.md lists; its scripts get values only through env: (so an
input can never become shell code); and its steps, run here one after another the way a runner runs a composite
action (bash -e -o pipefail, a GITHUB_OUTPUT file per step, if: conditions, outputs passed between steps), turn a
passing, failing, refused or over-long run into the right status, summary, JUnit file, artifact and pass/fail.
GitHub's own steps are stood in for: setup-python (the Python running these tests is on PATH), upload-artifact and
attest-build-provenance. The pull request comment talks to a fake GitHub API on 127.0.0.1, never the real one.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from conftest import REPO, cli, write_config
from agent_testbench import github_action as GA

ACTION = yaml.safe_load((REPO / "action.yml").read_text())
STEPS = ACTION["runs"]["steps"]
DOCS = REPO / "docs" / "github-action.md"
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
BIN = Path(sys.executable).parent


def test_inputs_and_outputs_match_the_docs():
    text = DOCS.read_text()

    def table(heading):
        section = text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]
        return {m[1]: m[2].strip() for m in re.finditer(r"^\| `([\w-]+)` \| ([^|]*)\|", section, re.M)}

    inputs = table("Inputs")
    assert set(inputs) == set(ACTION["inputs"])
    for name, spec in ACTION["inputs"].items():
        want = "required" if spec.get("required") else (f"`{spec['default']}`" if spec.get("default") else "empty")
        assert inputs[name] == want, f"docs give input {name} the default {inputs[name]}; action.yml has {want}"
    assert set(table("Outputs")) == set(ACTION["outputs"])
    for kind in ("inputs", "outputs"):
        for name, spec in ACTION[kind].items():
            assert spec.get("description"), f"{kind[:-1]} {name} has no description"


def test_scripts_take_values_only_through_env():
    ids = {s["id"] for s in STEPS if "id" in s}
    for step in STEPS:
        assert "${{" not in step.get("run", ""), f"step {step['name']!r}: pass values through env:, not in the script"
        assert "uses" in step or step.get("shell") == "bash", f"step {step['name']!r} needs shell: bash"
        fields = [step.get("if", ""), *(step.get("env") or {}).values(), *(step.get("with") or {}).values()]
        for expr in EXPR.findall(" ".join(map(str, fields)) + " ".join(o["value"] for o in ACTION["outputs"].values())):
            for name in re.findall(r"\binputs\.([\w-]+)", expr):
                assert name in ACTION["inputs"], f"{expr!r} uses an input action.yml does not declare"
            for sid in re.findall(r"\bsteps\.([\w-]+)\.", expr):
                assert sid in ids, f"{expr!r} names a step that does not exist"


# ---- the steps, run the way a runner runs them --------------------------------------------------------------------

def _lookup(path: str, ctx: dict) -> str:
    parts = path.strip().split(".")
    if parts[0] == "steps" and parts[2] == "outcome":         # steps.<id>.outcome
        return ctx["outcomes"].get(parts[1], "")
    if parts[0] == "steps":                                   # steps.<id>.outputs.<name>
        return ctx["steps"].get(parts[1], {}).get(parts[3], "")
    return ctx[parts[0]].get(parts[1], "")


def _render(value, ctx: dict) -> str:
    return EXPR.sub(lambda m: _lookup(m[1], ctx), str(value))


def _condition(cond: str, ctx: dict, failed: bool) -> bool:
    """The if: forms action.yml uses: status functions and `x == 'v'` / `x != 'v'`, joined by &&. Without a status
    function GitHub adds success(). Nothing is cancelled here."""
    m = EXPR.fullmatch(cond.strip())
    expr = (m[1] if m else cond).strip()
    ok = True if re.search(r"cancelled\(\)|always\(\)|failure\(\)", expr) else not failed
    for term in filter(None, (t.strip() for t in expr.split("&&"))):
        if term == "cancelled()":
            ok = False
        elif term != "!cancelled()":
            t = re.fullmatch(r"([\w.-]+) (==|!=) '(.*)'", term)
            assert t, f"this stand-in for the runner does not understand if: {cond}"
            same = _lookup(t[1], ctx).lower() == t[3].lower()          # GitHub compares strings ignoring case
            ok = ok and (same if t[2] == "==" else not same)
    return ok


def _outputs(text: str) -> dict:
    out, lines = {}, iter(text.splitlines())
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, end = line.split("<<", 1)
            value = []
            for more in lines:
                if more == end:
                    break
                value.append(more)
            out[key] = "\n".join(value)
        elif "=" in line:
            key, value = line.split("=", 1)
            out[key] = value
    return out


def run_action(workspace: Path, tmp: Path, env: dict | None = None, attest_fails: bool = False, **inputs) -> dict:
    """The action's steps, as a runner runs them. env: what the runner would set (GITHUB_EVENT_NAME, ...); the
    GITHUB_*, ACTIONS_* and RUNNER_* variables of the process running the tests (in CI, the real ones) are left out."""
    ctx = {"inputs": {k: str(inputs.get(k.replace("-", "_"), spec.get("default", "")))
                      for k, spec in ACTION["inputs"].items()},
           "github": {"action_path": str(REPO)}, "steps": {}, "outcomes": {}}
    temp = tmp / "runner temp"
    temp.mkdir(exist_ok=True)
    own = {k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "ACTIONS_", "RUNNER_"))}
    base = {**own, **(env or {}), "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temp),
            "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"}
    failed, summary, uploads, attestations, log = False, "", [], [], []
    for n, step in enumerate(STEPS):
        if not _condition(step.get("if", ""), ctx, failed):
            if "id" in step:
                ctx["outcomes"][step["id"]] = "skipped"
            continue
        if "uses" in step:
            given = {k: _render(v, ctx) for k, v in (step.get("with") or {}).items()}
            if step["uses"].startswith("actions/upload-artifact@"):
                assert Path(given["path"]).is_dir(), given
                uploads.append({**given, "files": sorted(p.relative_to(given["path"]).as_posix()
                                                         for p in Path(given["path"]).rglob("*") if p.is_file())})
                ctx["steps"][step["id"]] = {"artifact-url": f"https://github.invalid/artifacts/{given['name']}"}
            elif step["uses"].startswith("actions/attest-build-provenance@"):
                assert re.fullmatch(r"actions/attest-build-provenance@[0-9a-f]{40}", step["uses"]), "pin a commit"
                subjects = Path(given["subject-checksums"]).read_text()
                attestations.append({**given, "subjects": subjects})
                if attest_fails:                      # as without attestations: write - the action fails its step
                    log.append(f"--- {step['name']}: Error: Failed to persist attestation: Resource not accessible")
                    ctx["outcomes"][step["id"]], failed = "failure", True
                    continue
                bundle = tmp / "attestation.json"
                bundle.write_text(json.dumps({"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}))
                ctx["steps"][step["id"]] = {"bundle-path": str(bundle), "attestation-id": "77",
                                            "attestation-url": "https://github.com/octo/proj/attestations/77"}
            if "id" in step:
                ctx["outcomes"][step["id"]] = "success"
            continue
        out, summ, script = tmp / f"step{n}.out", tmp / f"step{n}.summary", tmp / f"step{n}.sh"
        out.write_text("")
        summ.write_text("")
        script.write_text(step["run"])
        step_env = {**base, **{k: _render(v, ctx) for k, v in (step.get("env") or {}).items()},
                    "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summ)}
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], cwd=workspace,
                           env=step_env, capture_output=True, text=True, timeout=600)
        log.append(f"--- {step['name']}: exit {r.returncode}\n{r.stdout}{r.stderr}")
        if "id" in step:
            ctx["steps"][step["id"]] = _outputs(out.read_text())
            ctx["outcomes"][step["id"]] = "success" if r.returncode == 0 else "failure"
        summary += summ.read_text()
        failed = failed or r.returncode != 0
    return {"outputs": {k: _render(spec["value"], ctx) for k, spec in ACTION["outputs"].items()}, "failed": failed,
            "summary": summary, "uploads": uploads, "attestations": attestations, "log": "\n".join(log),
            "ran": [line.split(":")[0][4:] for line in log]}


@pytest.fixture
def workspace(tmp_path) -> Path:
    """A checked-out repository (in a folder with a space in its name) with a project in 'my project/'."""
    if not (BIN / "testbench").exists():
        pytest.skip(f"no testbench command next to {sys.executable} (pip install -e . puts it there)")
    ws = tmp_path / "work space"
    (ws / "my project").mkdir(parents=True)
    (ws / "my project" / "train.py").write_text(
        "import json, sys\njson.dump({'accuracy': float(sys.argv[1])}, open('metrics.json', 'w'))\nprint('trained')\n")
    return ws


def plans(ws: Path, budget: dict | None = None, fake: bool = False) -> Path:
    """Local plans that pass, fail and outlast a short wait; with the fake backend (local, priced like a rented GPU at
    $6/h) one that needs approval and one over max_run_usd."""
    def step(name, run, minutes=1, **expect):
        return {"name": name, "run": run, "max_minutes": minutes, **({"expect": expect} if expect else {})}
    accurate = {"json": {"metrics.json": {"accuracy": ">= 0.9"}}}
    cfg = {"project": "action-test", "plans": {
        "good": {"backend": "local", "steps": [step("train", "python train.py 0.97", **accurate)]},
        "bad": {"backend": "local", "steps": [step("train", "python train.py 0.5", **accurate),
                                              step("evaluate", "echo never runs")]},
        "slow": {"backend": "local", "steps": [step("sleep", "python -c 'import time; time.sleep(120)'", 3)]}}}
    if fake:
        cfg["plans"].update({"rented": {"backend": "fake", "gpu": "FAKE1", "steps": [step("hello", "echo hi", 4)]},
                             "huge": {"backend": "fake", "gpu": "FAKE1", "steps": [step("hello", "echo hi", 60)]}})
    if budget:
        cfg["budget"] = budget
    return write_config(ws / "my project", cfg)


LOCAL = {"install": "none", "setup_python": "false", "project_dir": "my project"}


def test_a_passing_plan_gives_the_summary_junit_html_artifact_and_a_pass(workspace, tmp_path):
    plans(workspace)
    git = ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "project"]):
        subprocess.run(git + args, cwd=workspace, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True).stdout.strip()

    r = run_action(workspace, tmp_path, plan="good", **LOCAL)
    out = r["outputs"]
    assert not r["failed"] and out["status"] == "passed", r["log"]
    run_dir = workspace / "my project" / ".testbench" / "runs" / out["run-id"]
    assert out["run-dir"] == str(run_dir) and out["cost-usd"] == "0.0000"
    for key, name in (("report-path", "report.md"), ("html-path", "report.html"), ("junit-path", "junit.xml")):
        assert out[key] == str(run_dir / name) and Path(out[key]).stat().st_size > 0
    assert 'failures="0"' in Path(out["junit-path"]).read_text()
    assert [(u["name"], u["path"]) for u in r["uploads"]] == [("testbench-good", str(run_dir))]
    head = f"**agent-testbench**: plan `good` **passed**, about $0.000, run `{out['run-id']}`, commit `{sha[:12]}`."
    assert r["summary"].startswith(head + "\n\n# PASS - plan `good` on local"), r["summary"]
    assert "Evidence: [testbench-good](https://github.invalid/artifacts/testbench-good)" in r["summary"]
    assert Path(out["summary-path"]).read_text() == r["summary"]
    # comment and attest are off by default: no step of theirs runs, nothing is signed, nothing extra is uploaded
    assert not [s for s in r["ran"] if "comment" in s or "attest" in s] and not r["attestations"], r["ran"]
    assert out["comment-url"] == out["attestation-url"] == "" and "attestation.sigstore.json" not in r["uploads"][0]["files"]


def test_a_failing_plan_fails_the_step_unless_fail_on_failure_is_false(workspace, tmp_path):
    plans(workspace)
    r = run_action(workspace, tmp_path, plan="bad", fail_on_failure="false", artifact_name="evidence: bad", **LOCAL)
    out = r["outputs"]
    assert not r["failed"] and out["status"] == "failed", r["log"]
    assert "::warning title=agent-testbench::plan bad failed: train: expected metrics.json: accuracy >= 0.9" in r["log"]
    assert "<failure" in Path(out["junit-path"]).read_text() and 'skipped="1"' in Path(out["junit-path"]).read_text()
    assert "plan `bad` **failed**" in r["summary"] and "expected metrics.json: accuracy >= 0.9: 0.5" in r["summary"]
    assert "code outside git" in r["summary"] and r["uploads"][0]["name"] == "evidence- bad"

    r = run_action(workspace, tmp_path, plan="bad", **LOCAL)
    assert r["failed"] and r["outputs"]["status"] == "failed" and r["uploads"], r["log"]
    assert "::error title=agent-testbench::plan bad failed: train" in r["log"]


def test_a_refused_plan_fails_with_the_reason_and_approve_is_a_standing_yes(workspace, tmp_path, plugin):
    plans(workspace, {"max_run_usd": 2.00, "ask_above_usd": 0.10, "max_day_usd": 5.00}, fake=True)
    r = run_action(workspace, tmp_path, env=plugin, plan="rented", fail_on_failure="false", **LOCAL)
    out = r["outputs"]
    assert r["failed"] and out["status"] == "refused" and out["run-id"] == "" and not r["uploads"], r["log"]
    assert "plan `rented` **refused**: the run did not start" in r["summary"]
    assert "Needs approval: plan rented could cost up to $0.50" in r["summary"] and "`approve: true`" in r["summary"]
    assert "::error title=agent-testbench::plan rented was refused: Needs approval" in r["log"]
    assert out["cost-usd"] == "0.0000" and out["junit-path"] == "" and Path(out["summary-path"]).exists()

    r = run_action(workspace, tmp_path, env=plugin, plan="rented", approve="true", **LOCAL)
    assert not r["failed"] and r["outputs"]["status"] == "passed", r["log"]
    assert json.loads(Path(r["outputs"]["run-dir"], "report.json").read_text())["approved"] is True

    r = run_action(workspace, tmp_path, env=plugin, plan="huge", approve="true", **LOCAL)
    assert r["failed"] and r["outputs"]["status"] == "refused", r["log"]
    assert "Refused: plan huge could cost up to $6.10" in r["summary"] and "does not lift this limit" in r["summary"]


def test_a_run_that_outlasts_wait_minutes_is_cancelled(workspace, tmp_path):
    plans(workspace)
    r = run_action(workspace, tmp_path, plan="slow", wait_minutes="0.1", **LOCAL)
    out = r["outputs"]
    assert r["failed"] and out["status"] == "cancelled", r["log"]
    assert "Still running after 0.1 min (`wait-minutes`), so the action cancelled it" in r["summary"]
    assert "# CANCELLED - plan `slow`" in r["summary"] and r["uploads"]
    state = json.loads(Path(out["run-dir"], "state.json").read_text())
    assert state["status"] == "cancelled"


def test_bad_inputs_stop_the_action_and_the_summary_says_so(workspace, tmp_path):
    r = run_action(workspace, tmp_path, plan="good", approve="yes", project_dir="nowhere", wait_minutes="soon",
                   install="none", setup_python="false")
    assert r["failed"] and r["outputs"]["status"] == "error" and not r["uploads"]
    assert "input approve must be true or false, not 'yes'" in r["log"]
    assert "wait-minutes must be a number of minutes, such as 30 or 1.5, not 'soon'" in r["log"]
    assert "project-dir 'nowhere' is not a folder" in r["log"]
    assert "did not run: the action stopped before testbench ran" in r["summary"]


# ---- comment: true - the pull request comment, against a fake GitHub API -------------------------------------------

TOKEN = "ghs_fakeTOKENthatMustNeverBePrinted0123456789"
BOT = {"login": "github-actions[bot]", "type": "Bot"}


class FakeGitHub:
    """api.github.com for one repository (octo/proj) and one pull request (#7): its comments, the workflow's runs on
    main with their evidence artifacts (downloads redirect to a storage host, as GitHub's do), and the run's jobs.
    forbid: "write" (a read-only token) and/or "actions" (no actions: read)."""

    def __init__(self):
        self.comments: list[dict] = []
        self.requests: list[tuple[str, str, str | None]] = []       # method, path, Authorization
        self.forbid: set[str] = set()
        self.base_report: dict | None = None
        self.zip = b""
        server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server, self.url = server, f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def bodies(self) -> list[str]:
        return [c["body"] for c in self.comments]

    def _handler(self):
        gh = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, data=None, headers=None):
                raw = data if isinstance(data, bytes) else json.dumps(data if data is not None else {}).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _route(self, method):
                parts = urlsplit(self.path)
                path, q = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
                gh.requests.append((method, self.path, self.headers.get("Authorization")))
                size = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(size)) if size else None
                denied = {"message": "Resource not accessible by integration"}
                if path == "/user":
                    return self._send(403, denied)
                if path.startswith("/storage/"):                   # the artifact's storage host
                    return self._send(200, gh.zip, {"Content-Type": "application/zip"})
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"message": "Bad credentials"})
                if path == "/repos/octo/proj/issues/7/comments" and method == "GET":
                    page, per = int(q.get("page", 1)), int(q.get("per_page", 30))
                    return self._send(200, gh.comments[(page - 1) * per:page * per])
                if path == "/repos/octo/proj/issues/7/comments" and method == "POST":
                    if "write" in gh.forbid:
                        return self._send(403, denied)
                    c = {"id": 1000 + len(gh.comments), "body": body["body"], "user": BOT}
                    c["html_url"] = f"https://github.com/octo/proj/pull/7#issuecomment-{c['id']}"
                    gh.comments.append(c)
                    return self._send(201, c)
                m = re.fullmatch(r"/repos/octo/proj/issues/comments/(\d+)", path)
                if m and method == "PATCH":
                    c = next(c for c in gh.comments if c["id"] == int(m[1]))
                    assert c["user"] == BOT, "only the action's own comment is edited"
                    c["body"] = body["body"]
                    return self._send(200, c)
                if path.startswith("/repos/octo/proj/actions/") and "actions" in gh.forbid:
                    return self._send(403, denied)
                if path == "/repos/octo/proj/actions/workflows/action.yml/runs":
                    assert q["branch"] == "main" and q["status"] == "completed", q
                    head = {"full_name": "octo/proj"}
                    return self._send(200, {"workflow_runs": [
                        {"id": 900, "event": "pull_request", "head_repository": head},     # another PR's: not main's
                        {"id": 901, "event": "push", "head_sha": "b" * 40, "head_repository": head,
                         "html_url": "https://github.com/octo/proj/actions/runs/901"}]})
                if path == "/repos/octo/proj/actions/runs/901/artifacts":
                    arts = [{"id": 55, "name": q["name"], "expired": False, "size_in_bytes": len(gh.zip)}]
                    return self._send(200, {"artifacts": arts if gh.base_report else []})
                if path == "/repos/octo/proj/actions/artifacts/55/zip":
                    return self._send(302, b"", {"Location": f"{gh.url}/storage/55.zip?sig=signed"})
                if path == "/repos/octo/proj/actions/runs/123/attempts/1/jobs":
                    return self._send(200, {"jobs": [
                        {"id": 4242, "runner_name": "runner 1", "status": "in_progress"},
                        {"id": 4243, "runner_name": "runner 2", "status": "in_progress"}]})
                return self._send(404, {"message": f"Not Found: {method} {path}"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PATCH(self):
                self._route("PATCH")

        return Handler

    def set_base(self, report: dict):
        """main's latest run kept this report in its evidence artifact."""
        self.base_report = report
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("report.json", json.dumps(report))
            z.writestr("steps/01-train/artifacts/report.json", "{}")      # a file of the run's own: not the report
        self.zip = buf.getvalue()


@pytest.fixture
def github():
    gh = FakeGitHub()
    yield gh
    gh.server.shutdown()
    gh.server.server_close()


def runner_env(gh: FakeGitHub, tmp: Path, event: str = "pull_request", head: str = "octo/proj", **more) -> dict:
    """What a runner sets for a job on pull request #7 of octo/proj (into main), pointed at the fake API."""
    payload = tmp / f"event-{event}.json"
    payload.write_text(json.dumps({"pull_request": {"number": 7, "head": {"repo": {"full_name": head}},
                                                    "base": {"ref": "main", "repo": {"full_name": "octo/proj"}}}}
                                  if event.startswith("pull_request") else {"ref": "refs/heads/main"}))
    return {"GITHUB_API_URL": gh.url, "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "octo/proj",
            "GITHUB_EVENT_NAME": event, "GITHUB_EVENT_PATH": str(payload), "GITHUB_WORKFLOW": "testbench",
            "GITHUB_WORKFLOW_REF": "octo/proj/.github/workflows/action.yml@refs/pull/7/merge",
            "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_SHA": "a" * 40, "GITHUB_ACTOR": "octocat",
            "GITHUB_BASE_REF": "main", "RUNNER_NAME": "runner 1", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
            **more}


def _report(status="passed", accuracy=0.97, loss=0.2, steps=None, **more) -> dict:
    checks = [{"check": "exit code 0", "ok": True, "detail": "exited 0"},
              {"check": "metrics.json: accuracy >= 0.9", "ok": accuracy >= 0.9, "detail": f"{accuracy} (wanted >= 0.9)"},
              {"check": "metrics.json: loss < 1", "ok": loss < 1, "detail": f"{loss} (wanted < 1.0)"}]
    return {"run_id": "20261101-120000-good", "plan": "good", "backend": "local", "status": status, "minutes": 0.05,
            "cost_usd": 0.0, "worst_case_usd": 0.0, "source": {"commit": "c" * 40, "dirty": False},
            "steps": steps or [{"name": "train", "kind": "run", "status": "passed" if accuracy >= 0.9 else "failed",
                                "expectations": checks}], **more}


def test_the_comment_says_the_verdict_what_failed_and_how_the_numbers_moved():
    marker = GA.marker_for("testbench", "testbench-good")
    links = {"summary": "https://github.com/octo/proj/actions/runs/123/attempts/1#summary-4242",
             "artifact": "https://github.com/octo/proj/actions/runs/123/artifacts/9", "artifact_name": "testbench-good"}
    body = GA.render("good", "passed", _report(), marker=marker, links=links, footer="edited on every push")
    assert body.startswith(marker + "\n### PASS - plan `good`\n"), body
    assert "Plan `good` **passed** on `local` in 0.05 min, about $0.000 (worst case $0.00); 3 of 3 checks passed. " \
           f"Run `20261101-120000-good` on commit `{'c' * 12}`." in body
    assert "What did not pass" not in body
    assert "**Numbers checked** (no earlier run to compare with):" in body
    assert "| ok | `train` | `metrics.json: accuracy >= 0.9` | 0.97 |" in body and "exit code 0" not in body
    assert f"[job summary]({links['summary']})" in body and f"[evidence]({links['artifact']})" in body
    assert body.rstrip().endswith("<sub>edited on every push</sub>")

    before = _report(accuracy=0.95, loss=0.2)
    bad = _report("failed", accuracy=0.5, loss=0.2, skipped=["evaluate"])
    body = GA.render("good", "failed", bad, marker=marker,
                     against={"report": before, "label": "the latest run on `main` (commit `bbbbbbbbbbbb`)"})
    assert "### FAIL - plan `good`" in body and "2 of 3 checks passed" in body
    assert ("**What did not pass**\n\n- `train`: expected `metrics.json: accuracy >= 0.9`, found `0.5 (wanted >= 0.9)`\n"
            "- not run, because an earlier step did not pass: `evaluate`") in body
    assert "**Numbers checked**, compared with the latest run on `main` (commit `bbbbbbbbbbbb`):" in body
    assert "| **missed** | `train` | `metrics.json: accuracy >= 0.9` | 0.5 | 0.95 | -0.45 |" in body
    assert "| ok | `train` | `metrics.json: loss < 1` | 0.2 | 0.2 | same |" in body
    assert "Steps since then: broke `train`." in body

    fixed = GA.render("good", "passed", _report(accuracy=0.99), marker=marker, against={"report": bad, "label": "x"})
    assert "| 0.99 | 0.5 | +0.49 |" in fixed and "Steps since then: fixed `train`." in fixed
    new = _report(steps=[{"name": "eval", "kind": "run", "status": "passed",
                          "expectations": [{"check": "f1 >= 0.5", "ok": True, "detail": "0.8 (wanted >= 0.5)"}]}])
    assert "| `eval` | `f1 >= 0.5` | 0.8 | - | new |" in GA.render("good", "passed", new, marker=marker,
                                                                  against={"report": before, "label": "x"})

    body = GA.render("pricey", "refused", None, marker=marker, message="Needs approval: plan pricey could cost up to $1")
    assert "### REFUSED - plan `pricey`" in body and "refused before it started, so nothing was checked" in body
    assert "`Needs approval: plan pricey could cost up to $1`" in body


def test_nothing_from_the_pull_request_becomes_markdown_mentions_or_html_in_the_comment():
    sly = "@everyone <img src=x> [click](https://evil.example) | `tick`\n# heading"
    rep = _report("failed", steps=[{"name": sly, "kind": "run", "status": "failed", "expectations": [
        {"check": sly, "ok": False, "detail": "7 (wanted >= 9)"}]}])
    body = GA.render(sly, "failed", rep, marker=GA.marker_for("w", "a"))
    for line in body.splitlines()[1:]:
        outside = re.sub(r"(`+) .*? \1|`[^`]*`", "", line)            # what is not inside a code span
        assert "@everyone" not in outside and "<img" not in outside and "](" not in outside, line
    assert "\n# heading" not in body
    row = next(x for x in body.splitlines() if x.startswith("| **missed**"))
    assert row.count(" | ") == 3 and "\\|" in row, "a | in a name is escaped, so the table keeps its columns"
    assert GA.marker_for("w --> <b> @x", "a") == "<!-- agent-testbench comment: w - -b- -x/a -->"


def test_a_comment_is_posted_once_then_edited_and_compared_with_main(workspace, tmp_path, github):
    plans(workspace)
    github.comments += [{"id": n, "body": f"looks good {n}", "user": {"login": "ada", "type": "User"}}
                        for n in range(1, 121)]                    # 120 comments: the action's lands on page 2
    marker = GA.marker_for("testbench", "testbench-good")
    github.comments.append({"id": 500, "body": marker + "\nplanted by a person", "user": {"login": "mallory",
                                                                                        "type": "User"}})
    github.set_base(_report(accuracy=0.95, loss=0.25))
    env = runner_env(github, tmp_path)

    r = run_action(workspace, tmp_path, env=env, plan="good", comment="true", github_token=TOKEN, **LOCAL)
    assert not r["failed"] and r["outputs"]["status"] == "passed", r["log"]
    posted = [c for c in github.comments if c["user"] == BOT]
    assert len(posted) == 1 and r["outputs"]["comment-url"] == posted[0]["html_url"], r["log"]
    assert github.comments[120]["body"].endswith("planted by a person"), "a person's comment is never edited"
    body = posted[0]["body"]
    assert body.startswith(marker + "\n### PASS - plan `good`\n"), body
    assert ("**Numbers checked**, compared with the latest run on `main` "
            "([workflow run](https://github.com/octo/proj/actions/runs/901), commit `cccccccccccc`):") in body
    assert "| ok | `train` | `metrics.json: accuracy >= 0.9` | 0.97 | 0.95 | +0.02 |" in body
    assert "[job summary](https://github.com/octo/proj/actions/runs/123/attempts/1#summary-4242)" in body
    assert "[evidence](https://github.invalid/artifacts/testbench-good) (artifact `testbench-good`)" in body
    assert f"for commit `{'a' * 12}`" in body and "testbench verify 2026" in body
    downloads = [(p, auth) for m, p, auth in github.requests if p.startswith("/storage/")]
    assert downloads == [("/storage/55.zip?sig=signed", None)], "the storage host never gets the token"

    first = posted[0]["id"]
    r = run_action(workspace, tmp_path, env=env, plan="bad", comment="true", fail_on_failure="false",
                   artifact_name="testbench-good", github_token=TOKEN, **LOCAL)
    assert not r["failed"] and r["outputs"]["status"] == "failed", r["log"]
    posted = [c for c in github.comments if c["user"] == BOT]
    assert [c["id"] for c in posted] == [first], "the second push edits the same comment"
    assert posted[0]["body"].startswith(marker + "\n### FAIL - plan `bad`") and "What did not pass" in posted[0]["body"]
    methods = [m for m, *_ in github.requests]
    assert methods.count("POST") == 1 and methods.count("PATCH") == 1

    everything = r["log"] + r["summary"] + "".join(github.bodies())
    everything += "".join(p.read_text(errors="replace") for p in tmp_path.rglob("*") if p.is_file()
                          and p.suffix in ("", ".out", ".summary", ".md", ".txt", ".json", ".log", ".sh"))
    assert TOKEN not in everything, "the token is never printed or written"


def test_without_actions_read_the_comment_compares_with_the_previous_run_in_the_job(workspace, tmp_path, github):
    plans(workspace)
    github.forbid.add("actions")
    env = runner_env(github, tmp_path)
    run_action(workspace, tmp_path, env=env, plan="good", comment="true", github_token=TOKEN, **LOCAL)
    body = github.bodies()[0]
    assert "**Numbers checked** (to compare with the latest run on `main`, give the job `actions: read`):" in body
    assert "[job summary](https://github.com/octo/proj/actions/runs/123/attempts/1)" in body, "the run's page"
    train = workspace / "my project" / "train.py"
    train.write_text(train.read_text().replace("float(sys.argv[1])", "float(sys.argv[1]) - 0.01"))
    r = run_action(workspace, tmp_path, env=env, plan="good", comment="true", github_token=TOKEN, **LOCAL)
    body = github.bodies()[0]
    assert len(github.comments) == 1 and r["outputs"]["comment-url"], r["log"]
    assert re.search(r"compared with the previous run of this plan in this job \(`\d{8}-\d{6}-good`\):", body), body
    assert "| ok | `train` | `metrics.json: accuracy >= 0.9` | 0.96 | 0.97 | -0.01 |" in body


@pytest.mark.parametrize("case", ["fork", "dependabot", "read-only token", "push"])
def test_a_comment_that_cannot_be_posted_is_a_notice_never_a_failure(workspace, tmp_path, github, case):
    plans(workspace)
    more = {"fork": {"head": "mallory/proj"}, "dependabot": {"GITHUB_ACTOR": "dependabot[bot]"},
            "push": {"event": "push"}}.get(case, {})
    if case == "read-only token":
        github.forbid.add("write")
    r = run_action(workspace, tmp_path, env=runner_env(github, tmp_path, **more), plan="good", comment="true",
                   github_token=TOKEN, **LOCAL)
    assert not r["failed"] and r["outputs"]["status"] == "passed" and r["outputs"]["comment-url"] == "", r["log"]
    assert not github.comments
    if case == "push":
        assert not github.requests and "this is a push event, not a pull request" in r["log"]
        return
    if case in ("fork", "dependabot"):
        assert not github.requests, "a read-only token is not even tried"
        assert ("this pull request comes from the fork mallory/proj" if case == "fork" else "Dependabot") in r["summary"]
        assert "read-only token" in r["summary"] and "::notice title=agent-testbench::No pull request comment" in r["log"]
    else:
        assert ("No pull request comment: GitHub refused it (HTTP 403: Resource not accessible by integration)"
                in r["summary"]) and "`permissions: pull-requests: write`" in r["summary"]
    assert Path(r["outputs"]["summary-path"]).read_text().count("No pull request comment") == 1
    assert TOKEN not in r["log"] + r["summary"]


def test_a_refused_run_is_commented_too_and_github_being_down_fails_nothing(workspace, tmp_path, github, plugin):
    plans(workspace, {"max_run_usd": 2.00, "ask_above_usd": 0.10, "max_day_usd": 5.00}, fake=True)
    env = runner_env(github, tmp_path)
    r = run_action(workspace, tmp_path, env={**env, **plugin}, plan="rented", comment="true", github_token=TOKEN,
                   **LOCAL)
    assert r["failed"] and r["outputs"]["status"] == "refused", r["log"]
    assert "### REFUSED - plan `rented`" in github.bodies()[0] and "Needs approval" in github.bodies()[0]

    down = runner_env(github, tmp_path, GITHUB_API_URL="http://127.0.0.1:9")      # nothing listens there
    r = run_action(workspace, tmp_path, env={**down, **plugin}, plan="good", comment="true", github_token=TOKEN,
                   **LOCAL)
    assert not r["failed"] and r["outputs"]["status"] == "passed", r["log"]
    assert "No pull request comment: could not reach http://127.0.0.1:9" in r["summary"]


# ---- attest: true - the attestation of report.json and the evidence -----------------------------------------------

def test_attest_signs_the_report_and_every_evidence_file_and_keeps_the_bundle(workspace, tmp_path, github):
    plans(workspace)
    env = runner_env(github, tmp_path, event="push", ACTIONS_ID_TOKEN_REQUEST_URL="https://token.invalid/oidc")
    r = run_action(workspace, tmp_path, env=env, plan="good", attest="true", **LOCAL)
    out = r["outputs"]
    assert not r["failed"] and out["status"] == "passed", r["log"]
    run_dir = Path(out["run-dir"])
    rep = json.loads((run_dir / "report.json").read_text())
    [att] = r["attestations"]
    assert att["create-storage-record"] == "false" and att["github-token"] == "${{ github.token }}"
    lines = att["subjects"].splitlines()
    want = [f"{hashlib.sha256((run_dir / 'report.json').read_bytes()).hexdigest()}  {rep['run_id']}/report.json"]
    want += [f"{e['sha256']}  {rep['run_id']}/{e['path']}" for e in rep["evidence"]]
    assert lines == want and len(lines) > 2, att["subjects"]
    assert out["attestation-url"] == "https://github.com/octo/proj/attestations/77"
    assert "attestation.sigstore.json" in r["uploads"][0]["files"], "the bundle is uploaded with the evidence"
    assert (f"Attestation: [report.json and {len(lines) - 1} evidence files](https://github.com/octo/proj/attestations/77)"
            f", signed by this workflow at commit `{'a' * 12}`") in r["summary"]
    assert f"`testbench verify {rep['run_id']} --attestation`" in r["summary"]
    assert not github.requests, "the attestation itself goes through actions/attest-build-provenance"


def test_attest_says_what_is_missing_and_skips_forks(workspace, tmp_path, github):
    plans(workspace)
    r = run_action(workspace, tmp_path, env=runner_env(github, tmp_path, event="push"), plan="good", attest="true",
                   **LOCAL)
    assert r["failed"] and r["outputs"]["status"] == "passed" and not r["attestations"], r["log"]
    assert "attest: true needs the job's permissions to include `id-token: write`" in r["summary"]
    assert "::error title=agent-testbench::No attestation" in r["log"] and r["uploads"], "the evidence is still uploaded"

    oidc = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.invalid/oidc"}
    r = run_action(workspace, tmp_path, env=runner_env(github, tmp_path, head="mallory/proj", **oidc), plan="good",
                   attest="true", **LOCAL)
    assert not r["failed"] and not r["attestations"] and r["outputs"]["attestation-url"] == "", r["log"]
    assert "No attestation: this pull request comes from the fork mallory/proj" in r["summary"]

    r = run_action(workspace, tmp_path, env=runner_env(github, tmp_path, event="push", **oidc), plan="good",
                   attest="true", attest_fails=True, **LOCAL)
    assert r["failed"] and r["outputs"]["attestation-url"] == "" and r["uploads"], r["log"]
    assert "Attestation: none was made" in r["summary"] and "attestations: write" in r["summary"]
    assert "::error title=agent-testbench::attest is true but no attestation was made" in r["log"]

    r = run_action(workspace, tmp_path, env=runner_env(github, tmp_path, event="push", **oidc), plan="nope",
                   attest="true", **LOCAL)
    assert r["outputs"]["status"] == "refused" and not r["attestations"], "no report, nothing to attest"


def test_attest_refuses_evidence_that_no_longer_matches_its_report(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "20261101-120000-good"
    (run_dir / "steps" / "01-train").mkdir(parents=True)
    log = run_dir / "steps" / "01-train" / "log.txt"
    log.write_bytes(b"trained\n")                         # bytes: no CRLF on Windows
    evidence = [{"path": "steps/01-train/log.txt", "bytes": 8, "sha256": hashlib.sha256(b"trained\n").hexdigest()}]
    (run_dir / "report.json").write_text(json.dumps({"run_id": run_dir.name, "evidence": evidence}))
    for k, v in {"RUN_DIR": str(run_dir), "WORK": str(tmp_path), "GITHUB_OUTPUT": str(tmp_path / "out"),
                 "GITHUB_EVENT_NAME": "push", "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.invalid"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("SUMMARY_FILE", raising=False)
    assert GA.main(["attest-subjects"]) == 0
    assert "\ntrue\n" in (tmp_path / "out").read_text() and (tmp_path / "attestation-subjects.txt").exists()
    log.write_bytes(b"trained, trust me\n")
    (tmp_path / "out").write_text("")
    assert GA.main(["attest-subjects"]) == 1
    said = capsys.readouterr().out
    assert "No attestation: the evidence no longer matches report.json (1 files, first steps/01-train/log.txt)" in said
    assert "\ntrue\n" not in (tmp_path / "out").read_text()


# ---- testbench verify --attestation ---------------------------------------------------------------------------------

FAKE_GH = """\
import json, os, sys
open(os.environ["FAKE_GH_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\\n")
mode = os.environ.get("FAKE_GH", "ok")
if mode == "auth":
    print("To get started with GitHub CLI, please run:  gh auth login", file=sys.stderr)
    sys.exit(4)
if mode == "fail":
    print("Error: verifying with issuer \\"sigstore.dev\\": no matching attestations found", file=sys.stderr)
    sys.exit(1)
cert = {"sourceRepositoryDigest": os.environ["FAKE_GH_DIGEST"], "runnerEnvironment": "github-hosted",
        "buildConfigURI": "https://github.com/octo/proj/.github/workflows/action.yml@refs/heads/main",
        "runInvocationURI": "https://github.com/octo/proj/actions/runs/123/attempts/1"}
print(json.dumps([{"attestation": {}, "verificationResult": {"signature": {"certificate": cert}}}]))
"""


@pytest.mark.posix
def test_verify_attestation_runs_gh_and_checks_the_commit(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    write_config(proj, {"project": "att", "plans": {"p": {"backend": "local", "steps": [
        {"name": "hello", "run": "echo hello", "max_minutes": 1}]}}})
    git = ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "p"],
                 ["remote", "add", "origin", "git@github.com:octo/proj.git"]):
        subprocess.run(git + args, cwd=proj, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj, capture_output=True, text=True).stdout.strip()
    assert cli(proj, "run", "p", "--wait", "120").returncode == 0
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(f"#!{sys.executable}\n{FAKE_GH}")
    (bin_dir / "gh").chmod(0o755)
    calls = tmp_path / "gh-calls.jsonl"
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_LOG": str(calls), "FAKE_GH_DIGEST": sha,
           "GITHUB_REPOSITORY": ""}

    r = cli(proj, "verify", "--attestation", env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "evidence files match" in r.stdout
    assert (f"report.json is attested: signed by octo/proj/.github/workflows/action.yml@refs/heads/main in "
            f"https://github.com/octo/proj/actions/runs/123/attempts/1, at commit {sha[:12]}, the commit the report "
            "names.") in r.stdout
    run_dir = next((proj / ".testbench" / "runs").iterdir())
    assert json.loads(calls.read_text().splitlines()[0]) == [
        "attestation", "verify", str(run_dir / "report.json"), "--repo", "octo/proj", "--format", "json"]

    (run_dir / "attestation.sigstore.json").write_text("{}")             # the bundle the action keeps
    r = cli(proj, "verify", "--attestation", "--repo", "someone/else", env={**env, "FAKE_GH_DIGEST": "d" * 40})
    assert r.returncode == 1 and f"at commit dddddddddddd, but the report says it ran on commit {sha[:12]}" in r.stdout
    assert json.loads(calls.read_text().splitlines()[1])[4:] == [
        "someone/else", "--format", "json", "--bundle", str(run_dir / "attestation.sigstore.json")]

    r = cli(proj, "verify", "--attestation", env={**env, "FAKE_GH": "fail"})
    assert r.returncode == 1 and "report.json has no attestation from a workflow of octo/proj that checks out" in r.stdout
    assert "no matching attestations found" in r.stdout
    r = cli(proj, "verify", "--attestation", env={**env, "FAKE_GH": "auth"})
    assert r.returncode == 2 and "gh is not signed in to GitHub (run `gh auth login`, or set GH_TOKEN)" in r.stdout

    r = cli(proj, "verify", "--attestation", env={**env, "PATH": str(tmp_path / "empty")})
    assert r.returncode == 2 and "it takes `gh`, the GitHub CLI (https://cli.github.com), which is not on PATH" in r.stdout
    subprocess.run(["git", "remote", "remove", "origin"], cwd=proj, check=True)
    r = cli(proj, "verify", "--attestation", env=env)
    assert r.returncode == 2 and "with --repo OWNER/REPO" in r.stdout
    assert cli(proj, "verify", env=env).returncode == 0, "without --attestation, gh is not needed"
    assert len(calls.read_text().splitlines()) == 4
