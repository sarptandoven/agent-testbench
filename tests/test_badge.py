"""testbench badge: a README badge of a plan's latest run, drawn here as SVG - "testbench | 14 checks passed" on
green, "failed" on red, "no runs" on grey - with the words carrying the result and white text readable on each."""
import sys
import xml.etree.ElementTree as ET

from agent_testbench import badge as B
from conftest import cli, write_config
from test_tearsheet import contrast

NS = "{http://www.w3.org/2000/svg}"


def report(status, checks=(), **more):
    return {"status": status, "steps": [{"name": "s", "expectations": [{"check": f"c{i}", "ok": ok}
                                                                        for i, ok in enumerate(checks)]}], **more}


def test_the_words_say_the_result():
    assert B.message(report("passed", [True] * 14)) == ("14 checks passed", "passed")
    assert B.message(report("passed", [True])) == ("1 check passed", "passed")
    assert B.message(report("passed")) == ("passed", "passed")
    assert B.message(report("failed", [True, False, False])) == ("2 of 3 checks failed", "failed")
    assert B.message(report("error")) == ("error", "failed")
    assert B.message(report("timeout")) == ("timed out", "failed")
    assert B.message(report("cancelled", [True])) == ("cancelled", "warn")
    assert B.message(None) == ("no runs", "none")
    assert B.message({"status": "failed", "steps": "not a list"}) == ("failed", "failed"), "an odd report"


def test_the_svg_is_well_formed_labelled_and_readable():
    for words, color in (("14 checks passed", "passed"), ("2 of 14 checks failed", "failed"), ("no runs", "none"),
                         ("cancelled", "warn"), ("<script>&", "failed")):
        svg = ET.fromstring(B.svg(words, color))
        assert svg.tag == f"{NS}svg" and svg.get("role") == "img"
        assert svg.get("aria-label") == f"testbench: {words}" == svg.find(f"{NS}title").text
        texts = [t.text for t in svg.iter(f"{NS}text")]
        assert texts == ["testbench", words], "the words are text, so they read without the colours"
        fills = [r.get("fill") for r in svg.iter(f"{NS}rect") if r.get("fill")]
        assert fills == [B.COLORS["label"], B.COLORS[color]]
        assert float(svg.get("width")) > 9 * len(words), "wide enough for its words"
    for name, fill in B.COLORS.items():
        assert contrast(B.TEXT, fill) >= 4.5, f"white on {name} {fill} is {contrast(B.TEXT, fill):.2f}:1"
    assert B.markdown("testbench: 3 checks passed", "badge.svg", "https://x.example/r/") == \
        "[![testbench: 3 checks passed](badge.svg)](https://x.example/r/)"
    assert B.file_name("train gpu/2") == "testbench-train-gpu-2.svg" and B.file_name(None) == "testbench.svg"


def test_the_command_writes_the_latest_runs_badge(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    py = f'"{sys.executable}"'
    write_config(root, {"project": "proj", "plans": {
        "ok": {"steps": [{"name": "a", "run": f"{py} -c \"print(1)\"", "max_minutes": 1}]},
        "bad": {"steps": [{"name": "b", "run": f"{py} -c \"import sys; sys.exit(3)\"", "max_minutes": 1}]}}})
    r = cli(root, "badge", "ok")
    assert r.returncode == 0 and "testbench: no runs" in r.stdout and "no finished run yet" in r.stdout
    assert "testbench: no runs" in (root / "testbench-ok.svg").read_text()
    assert cli(root, "run", "ok", "--wait", "120").returncode == 0
    assert cli(root, "run", "bad", "--wait", "120").returncode == 1
    r = cli(root, "badge", "ok", "-o", "docs/badge.svg", "--link", "https://example.org/runs/")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "testbench: 1 check passed" in (root / "docs" / "badge.svg").read_text()
    assert "[![testbench: 1 check passed](docs/badge.svg)](https://example.org/runs/)" in r.stdout
    r = cli(root, "badge")
    assert r.returncode == 0 and "testbench: 1 of 1 check failed" in (root / "testbench-bad.svg").read_text(), \
        "no plan: the latest run's"
    r = cli(root, "badge", "okk")
    assert r.returncode == 2 and "did you mean ok?" in r.stdout
