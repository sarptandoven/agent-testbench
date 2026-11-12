"""testbench ci: the CI jobs for GitLab, Azure Pipelines, CircleCI and a plain shell script - each valid YAML (or
shell), with the JUnit report the CI shows, this version pinned and no approval anywhere - and the shell job run in a
project, where it exits as `testbench check` does and fails on a refusal. Then the JUnit files `check --junit` and
`run --junit` write, and the `testbench ci` command. TESTBENCH_TEST_CI_SCHEMAS=1 also checks the YAML jobs against
the published schemas of GitLab, CircleCI and Azure Pipelines (fetched, so it is opt-in)."""
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from agent_testbench import __version__, backends, ci
from conftest import cli, write_config

PIN = f"agent-testbench=={__version__}"
JUNIT = ".testbench/junit/*.xml"


def job_script(kind: str, doc) -> str:
    """The shell that runs testbench in a YAML job."""
    if kind == "gitlab":
        return doc["testbench"]["script"][-1]
    if kind == "azure":
        return next(s["bash"] for s in doc["steps"] if s.get("displayName") == "testbench")
    return next(s["run"]["command"] for s in doc["jobs"]["testbench"]["steps"]
                if isinstance(s, dict) and isinstance(s.get("run"), dict) and s["run"].get("name") == "testbench")


@pytest.mark.parametrize("kind", ["gitlab", "azure", "circleci", "shell"])
@pytest.mark.parametrize("plans", [(), ("smoke", "train-gpu")])
def test_each_template_is_valid_publishes_junit_pins_the_version_and_never_approves(kind, plans):
    text = ci.render(kind, plans)
    assert "{{" not in text and "}}" not in text.replace("${{", ""), "every placeholder filled in"
    assert "--approve" not in text, "a CI job never approves a cost"
    assert "never approves a cost" in text, "the template says so, for whoever edits it"
    assert PIN in text, "the version that wrote it is the version it installs"
    assert text == ci.render(kind, list(plans)), "the same job each time"
    job = ci._read("job.sh")
    assert all(line.strip() in text for line in job.splitlines() if line.strip()), "job.sh is what it runs"
    if kind == "shell":
        assert text.startswith("#!/bin/sh\n") and f': "${{TESTBENCH_PLANS={" ".join(plans)}}}"' in text
        script = text
    else:
        doc = yaml.safe_load(text)
        script = job_script(kind, doc)
        if kind == "gitlab":
            job = doc["testbench"]
            assert job["variables"]["TESTBENCH_PLANS"] == " ".join(plans)
            assert job["script"][0] == f'python -m pip install "{PIN}"'
            assert job["artifacts"]["when"] == "always" and job["artifacts"]["reports"]["junit"] == JUNIT
            assert ".testbench/runs/" in job["artifacts"]["paths"]
        elif kind == "azure":
            assert doc["variables"]["TESTBENCH_PLANS"] == " ".join(plans)
            tests = next(s for s in doc["steps"] if s.get("task") == "PublishTestResults@2")
            assert tests["condition"] == "succeededOrFailed()"
            assert tests["inputs"]["testResultsFormat"] == "JUnit" and tests["inputs"]["testResultsFiles"] == JUNIT
            runs = next(s for s in doc["steps"] if s.get("task") == "PublishPipelineArtifact@1")
            assert runs["condition"] == "succeededOrFailed()" and runs["inputs"]["targetPath"] == ".testbench/runs"
        else:
            job = doc["jobs"]["testbench"]
            assert doc["version"] == 2.1 and doc["workflows"]["testbench"]["jobs"] == ["testbench"]
            assert job["environment"]["TESTBENCH_PLANS"] == " ".join(plans)
            steps = {next(iter(s)): s[next(iter(s))] for s in job["steps"] if isinstance(s, dict)}
            assert steps["store_test_results"] == {"path": ".testbench/junit", "when": "always"}
            assert steps["store_artifacts"] == {"path": ".testbench/runs", "destination": "testbench-runs",
                                                "when": "always"}
    assert "testbench check --junit .testbench/junit/check.xml" in script
    assert '--junit "$junit" -- "$plan"' in script, "a plan's name is a value after --, never an option"
    if shutil.which("sh"):
        r = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"{kind}: not valid shell: {r.stderr}"


def test_a_name_that_is_not_a_plan_name_is_refused():
    for bad in ("--approve", "a b", "x;rm -rf /", "", "-x"):
        with pytest.raises(ValueError, match="is not a plan name"):
            ci.render("gitlab", [bad])
    with pytest.raises(ValueError, match="no CI template 'jenkins'; there are gitlab, azure, circleci, shell"):
        ci.render("jenkins")


@pytest.fixture
def shell_job(tmp_path):
    """The shell template, and a run of it with the testbench of this checkout on PATH (TESTBENCH_INSTALL=none)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "testbench").write_text(f'#!/bin/sh\nexec "{sys.executable}" -m agent_testbench "$@"\n')
    (bin_dir / "testbench").chmod(0o755)
    script = tmp_path / "testbench-ci.sh"
    script.write_text(ci.render("shell"))

    def run(root: Path, env=None) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "TESTBENCH_INSTALL": "none",
               **(env or {})}
        return subprocess.run(["sh", str(script)], cwd=root, capture_output=True, text=True, timeout=300, env=env)
    return run


def suites(path: Path) -> list[ET.Element]:
    root = ET.parse(path).getroot()
    assert root.tag == "testsuites"
    return root.findall("testsuite")


@pytest.mark.posix
@pytest.mark.parametrize("case", ["passes", "fails", "nothing to check"])
def test_the_shell_job_exits_as_testbench_check_does(tmp_path, shell_job, case):
    root = tmp_path / "project"
    root.mkdir()
    if case != "nothing to check":
        write_config(root, {"plans": {"ok": "echo hi", **({"broken": "exit 1"} if case == "fails" else {})}})
    twin = tmp_path / "twin"
    shutil.copytree(root, twin)
    expected = cli(twin, "check").returncode
    r = shell_job(root)
    assert r.returncode == expected == {"passes": 0, "fails": 1, "nothing to check": 2}[case], r.stdout + r.stderr
    verdict = r.stdout.rstrip().splitlines()[-1]
    assert verdict.startswith({"passes": "testbench: passed.", "fails": "testbench: FAILED: a plan did not pass.",
                               "nothing to check": "testbench: REFUSED (exit 2): nothing ran"}[case]), r.stdout
    found = suites(root / ".testbench" / "junit" / "check.xml")
    assert (root / ".testbench" / "runs").is_dir(), "the folder the CI keeps is there, even when nothing ran"
    if case == "nothing to check":
        error = found[0].find("testcase/error")
        assert error is not None and error.get("message").startswith("nothing ran: Nothing to check in"), found
        assert "never approves a cost" in r.stdout
        return
    by_name = {s.get("name"): s for s in found}
    assert by_name["ok on local"].get("failures") == "0" and by_name["ok on local"].find("testcase") is not None
    if case == "fails":
        assert by_name["broken on local"].find("testcase/failure").get("message") == "expected exit code 0: exited 1"


@pytest.fixture
def priced(tmp_path, plugin, monkeypatch) -> tuple[Path, dict]:
    """A project with a free plan and one on the fake plugin backend ($6/h, 4 minutes: $0.50, above ask_above_usd)."""
    monkeypatch.setattr(backends.KIND, "loaded", None)
    root = tmp_path / "priced"
    root.mkdir()
    write_config(root, {"project": "priced", "budget": {"max_run_usd": 5, "ask_above_usd": 0.1, "max_day_usd": 10},
                        "plans": {"free": "echo free",
                                  "on-fake": {"backend": "fake", "gpu": "FAKE1", "steps": [
                                      {"name": "hello", "run": "echo hi", "max_minutes": 4}]}}})
    return root, plugin


@pytest.mark.posix
def test_named_plans_run_and_a_cost_that_needs_approval_fails_the_job(priced, shell_job):
    root, env = priced
    r = shell_job(root, {**env, "TESTBENCH_PLANS": "free on-fake --approve"})
    out = r.stdout + r.stderr
    assert r.returncode == 2, out
    assert "# PASS - plan `free`" in out, "the free plan ran"
    assert "Needs approval: plan on-fake" in out and "No plan '--approve'" in out, out
    assert "testbench: REFUSED (exit 2)" in out and "never approves a cost" in out
    launches = [json.loads(x) for x in (root / ".testbench" / "ledger.jsonl").read_text().splitlines()
                if json.loads(x)["event"] == "launch"]
    assert [x["plan"] for x in launches] == ["free"], "nothing was launched on the job's word"
    junit = root / ".testbench" / "junit"
    assert sorted(p.name for p in junit.iterdir()) == ["free.xml", "on-fake.xml", "plan-3.xml"]
    assert suites(junit / "free.xml")[0].get("failures") == "0"
    for name in ("on-fake.xml", "plan-3.xml"):
        error = suites(junit / name)[0].find("testcase/error")
        assert error.get("type") == "refused" and error.get("message").startswith("refused, nothing ran: "), name
    # testbench check in the same project runs the free plan and only lists the one that costs money
    r = shell_job(root, env)
    assert r.returncode == 0, r.stdout + r.stderr
    check = {s.get("name"): s for s in suites(junit / "check.xml")}
    skipped = check["on-fake"].find("testcase/skipped")
    assert check["free on local"].get("failures") == "0" and "can cost up to $0.50" in skipped.get("message")


def test_the_junit_says_why_a_plan_did_not_run(tmp_path):
    xml = ci.junit([
        {"plan": "gpu", "status": "not run", "why": "it can cost up to $1.20 on modal A10G; a person runs it"},
        {"plan": "web", "status": "skipped", "why": "it needs Chromium", "install": "playwright install chromium"},
        {"plan": "big", "status": "refused", "why": "Refused: over max_run_usd \x1b[31m"}])
    root = ET.fromstring(xml)
    assert (root.get("tests"), root.get("errors"), root.get("skipped"), root.get("failures")) == ("3", "1", "2", "0")
    gpu, web, big = root.findall("testsuite")
    assert gpu.find("testcase/skipped").get("message").startswith("it can cost up to $1.20")
    assert web.find("testcase/skipped").get("message") == "it needs Chromium. Install: playwright install chromium"
    assert big.find("testcase/error").get("message") == "refused, nothing ran: Refused: over max_run_usd [31m"
    nothing = ET.fromstring(ci.junit([], "No plan 'x'", title="x"))
    assert nothing.get("errors") == "1" and nothing.find("testsuite/testcase").get("name") == "launch"


def test_run_and_check_write_junit_however_they_end(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"plans": {"ok": "echo hi"}})
    r = cli(root, "run", "ok", "--junit", "out/ok.xml")
    assert r.returncode == 0 and suites(root / "out" / "ok.xml")[0].get("name") == "ok on local", r.stdout
    r = cli(root, "run", "--junit", "out/nope.xml", "--", "nope")
    assert r.returncode == 2 and "No plan 'nope'" in r.stdout
    assert suites(root / "out" / "nope.xml")[0].find("testcase/error").get("type") == "refused"
    r = cli(root, "check", "--plan", "ok", "--json", "--junit", "out/check.xml")
    assert r.returncode == 0 and json.loads(r.stdout)["passed"] == 1, "--json output stays JSON"
    assert suites(root / "out" / "check.xml")[0].get("tests") == "1"
    (root / "testbench.yaml").write_text("plans: [")                      # does not load: still a JUnit error
    r = cli(root, "run", "ok", "--junit", "out/broken.xml")
    assert r.returncode == 2 and "testbench.yaml" in r.stderr
    assert suites(root / "out" / "broken.xml")[0].find("testcase/error").get("message").startswith("nothing ran: ")
    r = cli(root, "check", "--junit", "out/broken-check.xml")
    assert r.returncode == 2 and (root / "out" / "broken-check.xml").is_file()


def test_the_ci_command_lists_prints_and_writes(tmp_path):
    r = cli(tmp_path, "ci")
    assert r.returncode == 0, r.stderr
    for kind, (_, writes, name, junit) in ci.TEMPLATES.items():
        assert f"  {kind:<9} {writes:<21} {name}; JUnit: {junit}" in r.stdout
    r = cli(tmp_path, "ci", "--for", "azure")
    assert r.returncode == 0 and r.stdout == ci.render("azure")
    r = cli(tmp_path, "ci", "--for", "circleci", "-o", ".circleci/config.yml", "--plan", "smoke")
    written = tmp_path / ".circleci" / "config.yml"
    assert r.returncode == 0 and written.read_text() == ci.render("circleci", ["smoke"]), r.stderr
    assert "`testbench run` for smoke" in r.stdout and "Tests tab" in r.stdout
    r = cli(tmp_path, "ci", "--for", "gitlab", "-o", ".circleci/config.yml")
    assert r.returncode == 2 and "is there already; nothing was written" in r.stderr
    assert written.read_text() == ci.render("circleci", ["smoke"])
    r = cli(tmp_path, "ci", "--for", "shell", "-o", ".circleci/config.yml", "--force")
    assert r.returncode == 0 and written.read_text() == ci.render("shell") and os.access(written, os.X_OK)
    r = cli(tmp_path, "ci", "--for", "gitlab", "--plan", "--approve")
    assert r.returncode == 2 and "Traceback" not in r.stderr
    write_config(tmp_path, {"plans": {"smoke": "echo hi"}})
    r = cli(tmp_path, "ci", "--for", "gitlab", "--plan", "smok")
    assert r.returncode == 2 and "No plan 'smok'" in r.stderr and "smoke" in r.stderr, r.stderr


SCHEMAS = {   # the published JSON Schemas of each CI's configuration file
    "gitlab": "https://gitlab.com/gitlab-org/gitlab/-/raw/master/app/assets/javascripts/editor/schema/ci.json",
    "circleci": "https://raw.githubusercontent.com/CircleCI-Public/circleci-yaml-language-server/refs/heads/main/"
                "schema.json",
    "azure": "https://raw.githubusercontent.com/microsoft/azure-pipelines-vscode/main/service-schema.json",
}


@pytest.mark.skipif(not os.environ.get("TESTBENCH_TEST_CI_SCHEMAS"),
                    reason="fetches the CI schemas from gitlab.com and GitHub: TESTBENCH_TEST_CI_SCHEMAS=1")
@pytest.mark.parametrize("kind", list(SCHEMAS))
def test_the_yaml_jobs_match_the_published_schemas(kind):
    import urllib.request
    jsonschema = pytest.importorskip("jsonschema")
    with urllib.request.urlopen(SCHEMAS[kind], timeout=60) as r:
        schema = json.loads(r.read().decode("utf-8-sig"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    for plans in ((), ("smoke", "train-gpu")):
        errors = [f"{list(e.path)}: {e.message}" for e in validator.iter_errors(yaml.safe_load(ci.render(kind, plans)))]
        assert not errors, errors
