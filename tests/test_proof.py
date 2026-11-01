"""Proof records: a report names the code it ran (commit and a hash of uncommitted changes), the machine it ran on,
and the hash of every evidence file; it follows the published schema; and it renders as HTML, JUnit and history."""
import json
import platform
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from agent_testbench import provenance
from conftest import cli, make_notebook, write_config


def git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proof"
    make_notebook(root / "nb.ipynb", [
        'N = 3 #@param {type:"integer"}',
        "import base64, json, pathlib\nfrom IPython.display import Image, display\npathlib.Path('out').mkdir(exist_ok=True)\n"
        "display(Image(data=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==')))",
        "json.dump({'n': N, 'loss': 1 / N}, open('out/m.json', 'w'))",
    ])
    write_config(root, {"project": "proof", "plans": {"p": {"steps": [
        {"name": "nb <main>", "notebook": "nb.ipynb", "max_minutes": 3, "artifacts": ["out/*"],
         "expect": {"json": {"out/m.json": {"n": ">= 3", "loss": "< 0.5"}}}}]}}})
    (root / ".gitignore").write_text("out/\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    return root


def latest(root: Path) -> tuple[Path, dict]:
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    return run, json.loads((run / "report.json").read_text())


def test_a_report_names_its_code_machine_and_evidence(proj):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(provenance.SCHEMA.read_text())
    assert cli(proj, "run", "p", "--wait", "120").returncode == 0
    run, rep = latest(proj)
    jsonschema.validate(rep, schema)
    assert rep["schema_version"] == 1 and rep["source"]["commit"] == git(proj, "rev-parse", "HEAD")
    assert rep["source"]["dirty"] is False and rep["source"]["changes_sha256"] is None
    assert rep["environment"]["python"] == platform.python_version() and rep["environment"]["packages"] > 10
    paths = {e["path"] for e in rep["evidence"]}
    assert "steps/01-nb-main/log.txt" in paths and "steps/01-nb-main/executed.ipynb" in paths
    assert any(p.endswith(".png") for p in paths) and len(rep["evidence_sha256"]) == 64

    nb = proj / "nb.ipynb"                                           # an uncommitted change is part of the proof
    nb.write_text(nb.read_text().replace("N = 3", "N = 4"))
    cli(proj, "run", "p", "--wait", "120")
    first = latest(proj)[1]["source"]
    cli(proj, "run", "p", "--wait", "120")
    second = latest(proj)[1]["source"]
    assert first["dirty"] and first["changes_sha256"] and first["changes_sha256"] == second["changes_sha256"]
    nb.write_text(nb.read_text().replace("N = 4", "N = 5"))
    cli(proj, "run", "p", "--wait", "120")
    assert latest(proj)[1]["source"]["changes_sha256"] != first["changes_sha256"], "other changes, another hash"
    (proj / "extra.py").write_text("x = 1\n")
    cli(proj, "run", "p", "--wait", "120")
    assert latest(proj)[1]["source"]["untracked_files"] == 1


def test_verify_finds_evidence_changed_after_the_run(proj):
    cli(proj, "run", "p", "--wait", "120")
    run, rep = latest(proj)
    r = cli(proj, "verify")
    assert r.returncode == 0 and f"all {len(rep['evidence'])} evidence files match" in r.stdout
    (run / "steps" / "01-nb-main" / "log.txt").write_text("all good, trust me\n")
    (run / "steps" / "01-nb-main" / "artifacts" / "extra.png").write_bytes(b"x")
    r = cli(proj, "verify")
    assert r.returncode == 1 and "changed: steps/01-nb-main/log.txt" in r.stdout and "added after the run" in r.stdout


def test_html_junit_and_history(proj):
    cli(proj, "run", "p", "--wait", "120")
    nb = proj / "nb.ipynb"
    nb.write_text(nb.read_text().replace("N = 3", "N = 1"))               # loss 1.0: the check fails
    assert cli(proj, "run", "p", "--wait", "120").returncode == 1
    run, rep = latest(proj)

    r = cli(proj, "report", "--format", "html")
    assert "wrote" in r.stdout and (run / "report.html").exists()
    page = (run / "report.html").read_text()
    assert "FAIL" in page and "nb &lt;main&gt;" in page and "loss &lt; 0.5" in page
    assert page.count("<main>") == 1, "the step's name is escaped, never markup"
    assert "data:image/png;base64," in page and rep["source"]["commit"][:12] in page

    xml = cli(proj, "report", "--format", "junit").stdout
    suite = ET.fromstring(xml).find("testsuite")
    assert suite.get("failures") == "1" and suite.find("testcase").get("name") == "nb <main>"
    failure = suite.find("testcase/failure")
    assert "n >= 3" in failure.get("message") and "loss < 0.5: 1" in failure.text, "every missed check is listed"
    out = proj / "junit.xml"
    cli(proj, "report", "--format", "junit", "-o", str(out))
    assert ET.parse(out).getroot().tag == "testsuites"

    h = cli(proj, "history", "p").stdout
    assert "2 run(s), 1 passed" in h and "out/m.json: loss < 0.5: 0.333333 -> 1" in h
    data = json.loads(cli(proj, "history", "p", "--json").stdout)
    assert [r["status"] for r in data["runs"]] == ["passed", "failed"]
