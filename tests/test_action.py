"""The GitHub Action (action.yml).

Its inputs and outputs are the ones docs/github-action.md lists; its scripts get values only through env: (so an
input can never become shell code); and its steps, run here one after another the way a runner runs a composite
action (bash -e -o pipefail, a GITHUB_OUTPUT file per step, if: conditions, outputs passed between steps), turn a
passing, failing, refused or over-long run into the right status, summary, JUnit file, artifact and pass/fail.
GitHub's own steps are stood in for: setup-python (the Python running these tests is on PATH) and upload-artifact.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import REPO, write_config

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


def run_action(workspace: Path, tmp: Path, env: dict | None = None, **inputs) -> dict:
    ctx = {"inputs": {k: str(inputs.get(k.replace("-", "_"), spec.get("default", "")))
                      for k, spec in ACTION["inputs"].items()},
           "github": {"action_path": str(REPO)}, "steps": {}}
    temp = tmp / "runner temp"
    temp.mkdir(exist_ok=True)
    base = {**os.environ, **(env or {}), "GITHUB_WORKSPACE": str(workspace), "RUNNER_TEMP": str(temp),
            "PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}"}
    failed, summary, uploads, log = False, "", [], []
    for n, step in enumerate(STEPS):
        if not _condition(step.get("if", ""), ctx, failed):
            continue
        if "uses" in step:
            given = {k: _render(v, ctx) for k, v in (step.get("with") or {}).items()}
            if step["uses"].startswith("actions/upload-artifact@"):
                assert Path(given["path"]).is_dir(), given
                uploads.append(given)
                ctx["steps"][step["id"]] = {"artifact-url": f"https://github.invalid/artifacts/{given['name']}"}
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
        summary += summ.read_text()
        failed = failed or r.returncode != 0
    return {"outputs": {k: _render(spec["value"], ctx) for k, spec in ACTION["outputs"].items()}, "failed": failed,
            "summary": summary, "uploads": uploads, "log": "\n".join(log)}


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
