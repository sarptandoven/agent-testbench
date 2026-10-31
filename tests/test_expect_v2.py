"""Expectations v2: tolerances and ranges in any rule, JUnit reports, numbers printed to the log, GPU use, images
compared with a known-good one, and the step's run time. Every check says what it found, so a failure is a finding."""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_testbench import config as C, executor, expect, loop as LP, report as R
from conftest import cli, write_config


def checks(exp: dict, workdir: Path, **result) -> dict:
    return {c["check"]: c for c in expect.check(exp, workdir=workdir, result={"kind": None, **result})}


# ---- tolerances and ranges ---------------------------------------------------------------------------------

@pytest.mark.parametrize("actual, rule, ok", [
    (0.935, "~= 0.93 +- 0.01", True), (0.94, "~= 0.93 +- 0.01", True), (0.92, "~= 0.93 ± 0.01", True),
    (0.95, "~= 0.93 +- 0.01", False), (0.9, "~=0.93+-0.01", False), (104, "~= 100 +- 5%", True),
    (106, "~= 100 +- 5%", False), ("0.931", "~= 0.93 +/- 0.01", True), (0.8, "between 0.8 and 0.95", True),
    (0.95, "between 0.8 and 0.95", True), (0.97, "Between 0.8 and 0.95", False), (-0.5, "between -1 and 0", True),
    (0.95, ">= 0.9", True), (0.85, ">= 0.9", False), ("done", "done", True), (3, "!= 4", True), (2, 2, True),
    (True, True, True), ("24/1", "24/1", True), (5, "< 5", False),
])
def test_rules(actual, rule, ok):
    assert expect.compare(actual, rule)[0] is ok


def test_rule_details_say_what_was_found():
    assert expect.compare(0.71, ">= 0.9") == (False, "0.71 (wanted >= 0.9)")         # unchanged
    ok, detail = expect.compare(0.95, "~= 0.93 +- 0.01")
    assert not ok and detail == "0.95 (wanted 0.93 ± 0.01; differs by +0.02)"
    assert expect.compare(0.97, "between 0.8 and 0.95") == (False, "0.97 (wanted between 0.8 and 0.95)")
    ok, detail = expect.compare("n/a", "~= 1 +- 0.1")
    assert not ok and detail == "'n/a' is not a number, so it cannot be compared with 1 ± 0.1"
    assert "not a number" in expect.compare(None, "between 0 and 1")[1]
    ok, detail = expect.compare(1, "~= abc +- 1")
    assert not ok and "cannot be checked" in detail and "~= 0.93 +- 0.01" in detail


def test_tolerances_in_json_media_and_web(tmp_path, monkeypatch):
    (tmp_path / "m.json").write_text(json.dumps({"acc": 0.935, "loss": [0.4, 0.25]}))
    got = checks({"json": {"m.json": {"acc": "~= 0.93 +- 0.01", "loss.-1": "between 0.1 and 0.2"}}}, tmp_path)
    assert got["m.json: acc ~= 0.93 +- 0.01"]["ok"]
    assert got["m.json: loss.-1 between 0.1 and 0.2"] == {"check": "m.json: loss.-1 between 0.1 and 0.2", "ok": False,
                                                          "detail": "0.25 (wanted between 0.1 and 0.2)"}
    (tmp_path / "v.mp4").write_bytes(b"x")
    monkeypatch.setattr(expect, "probe_media", lambda p: {"frames": 48, "seconds": 2.04, "width": 320})
    got = checks({"media": {"v.mp4": {"seconds": "~= 2 +- 0.1", "frames": "between 50 and 60"}}}, tmp_path)
    assert got["v.mp4: seconds ~= 2 +- 0.1"]["ok"] and not got["v.mp4: frames between 50 and 60"]["ok"]
    web = {"status": 204, "text": "", "title": ""}
    got = checks({"web": {"status": "between 200 and 299"}}, tmp_path, kind="web", web=web)
    assert got["page status between 200 and 299"]["ok"]
    assert not checks({"web": {"status": "between 200 and 202"}}, tmp_path, kind="web", web=web)[
        "page status between 200 and 202"]["ok"]


# ---- config: malformed expectations fail at load time ------------------------------------------------------

@pytest.mark.parametrize("exp, message", [
    ({"json": {"m.json": {"acc": "~= 0.9"}}}, "expect.json.m.json.acc`: '~= 0.9' is neither a tolerance"),
    ({"json": {"m.json": {"acc": "between 0.95 and 0.8"}}}, "write the low end first, \"between 0.8 and 0.95\""),
    ({"json": {"m.json": {"acc": "~= 0.9 +- -0.1"}}}, "must not be negative"),
    ({"json": {"m.json": {"acc": {">=": 0.9}}}}, "a rule is one value such as"),
    ({"json": {"m.json": ">= 0.9"}}, "expect.json.m.json` must be a mapping"),
    ({"media": {"v.mp4": {"duration": ">= 1"}}}, "has no 'duration'; use frames, width"),
    ({"web": {"status": "~= 200"}}, "expect.web.status`"),
    ({"web": {"text": "Count"}}, "expect.web.text` is a list"),
    ({"junit": {"failures": 0}}, "expect.junit` needs `path:`"),
    ({"junit": [{"path": "a.xml"}, {"tests": 3}]}, "expect.junit[1]` needs `path:`"),
    ({"junit": {"path": "r.xml", "failed": 0}}, "has no 'failed'; use tests, passed, failures, errors, skipped"),
    ({"junit": {"path": "r.xml", "tests": "between 10"}}, "expect.junit.tests`"),
    ({"metrics": {"stdout": {"loss=([0-9.]+": "< 1"}}}, "is not a regular expression"),
    ({"metrics": {"stdout": "loss=([0-9.]+)"}}, "expect.metrics.stdout` must be a mapping"),
    ({"gpu": {"util": ">= 50"}}, "has no 'util'; use busy_pct, mean_util_pct, max_mem_mib"),
    ({"image": {"p.png": {"max_diff": 0.1}}}, "is {like: golden/plot.png, max_diff: 0.02}"),
    ({"image": {"p.png": "g.png"}}, "is {like: golden/plot.png"),
    ({"image": {"p.png": {"like": "g.png", "max_diff": 5}}}, "max_diff` is a number from 0 (identical) to 1"),
    ({"duration": {"hours": "<= 1"}}, "has no 'hours'; use minutes, seconds"),
    ({"jsn": {}}, "has no 'jsn' check; expectations are exit_code"),
    ({"files": "out.txt"}, "expect.files` is a list of paths"),
    ({"exit_code": "0"}, "a whole number such as 0"),
    ({"no_errors": "yes"}, "is true or false"),
])
def test_malformed_expectations_fail_at_load(tmp_path, exp, message):
    write_config(tmp_path, {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true", "expect": exp}]}}})
    with pytest.raises(C.ConfigError, match=re.escape(message)) as info:
        C.load(tmp_path)
    assert "plans.p.steps[0].expect" in str(info.value)


def test_every_new_expectation_loads(tmp_path):
    exp = {"exit_code": 1, "junit": [{"path": "a.xml", "tests": ">= 10", "skipped": "<= 2"}, "b.xml"],
           "metrics": {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.3"}, "log.txt": {r"acc (\d+)": "~= 90 +- 5%"}},
           "gpu": {"busy_pct": ">= 50", "max_mem_mib": "<= 15000", "mean_util_pct": "between 30 and 100"},
           "image": {"outputs/plot.png": {"like": "golden/plot.png", "max_diff": 0.02}, "b.png": {"like": "g.png"}},
           "duration": {"minutes": "<= 5", "seconds": "> 1"}, "web": {"status": "between 200 and 299"}}
    write_config(tmp_path, {"project": "p", "plans": {"p": {"steps": [{"name": "a", "run": "true", "expect": exp}]}}})
    assert C.load(tmp_path)["plans"]["p"]["steps"][0]["expect"] == exp


# ---- junit ---------------------------------------------------------------------------------------------------

PYTEST_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites name="pytest tests"><testsuite name="pytest" errors="1" failures="1" skipped="1" tests="4" time="0.1">
<testcase classname="tests.test_math" name="test_add" time="0.001"/>
<testcase classname="tests.test_math" name="test_sub" time="0.001"><failure message="assert 1 == 2">def test_sub():
&gt;       assert 1 == 2
E       assert 1 == 2</failure></testcase>
<testcase classname="tests.test_math" name="test_io" time="0.001"><error message="failed on setup with &quot;FileNotFoundError: data.csv&quot;">trace</error></testcase>
<testcase classname="tests.test_math" name="test_gpu" time="0"><skipped type="pytest.skip" message="no GPU">skip</skipped></testcase>
</testsuite></testsuites>"""
GO_XML = """<testsuite name="example.com/pkg" tests="2" failures="1" errors="0">
<testcase classname="pkg" name="TestA" time="0.00"/>
<testcase classname="pkg" name="TestB" time="0.00"><failure message="Failed" type="">pkg_test.go:12: got 3, want 4</failure></testcase>
</testsuite>"""
JEST_XML = """<testsuites name="jest tests" tests="2" failures="1" errors="0"><testsuite name="math" tests="2" failures="1">
<testcase classname="math adds" name="math adds" time="0.002"/>
<testcase classname="math subtracts" name="math subtracts" time="0.001"><failure>
Error: expect(received).toBe(expected)

Expected: 1
Received: 2</failure></testcase></testsuite></testsuites>"""
TOTALS_XML = """<testsuites><testsuite name="a" tests="5" failures="1" errors="0" skipped="1"/>
<testsuite name="b" tests="3" failures="0" errors="0" skipped="0"/></testsuites>"""


def test_junit_counts_and_names_the_failing_cases(tmp_path):
    (tmp_path / "report.xml").write_text(PYTEST_XML)
    got = checks({"junit": {"path": "report.xml", "tests": ">= 10", "skipped": "<= 2", "passed": 1}}, tmp_path)
    assert list(got) == ["report.xml: failures 0", "report.xml: errors 0", "report.xml: tests >= 10",
                         "report.xml: skipped <= 2", "report.xml: passed 1"], "failures: 0 and errors: 0 by default"
    assert got["report.xml: failures 0"] == {
        "check": "report.xml: failures 0", "ok": False,
        "detail": "1 (wanted == 0) of 4 tests; failed: tests.test_math.test_sub (assert 1 == 2)"}
    assert got["report.xml: errors 0"]["detail"] == ("1 (wanted == 0) of 4 tests; errored: tests.test_math.test_io "
                                                     "(failed on setup with \"FileNotFoundError: data.csv\")")
    assert got["report.xml: tests >= 10"]["detail"] == "4 (wanted >= 10.0)" and not got["report.xml: tests >= 10"]["ok"]
    assert got["report.xml: skipped <= 2"]["ok"] and got["report.xml: passed 1"]["ok"]
    got = checks({"junit": {"path": "report.xml", "failures": "<= 1", "errors": "<= 1"}}, tmp_path)
    assert all(c["ok"] for c in got.values()) and "test_sub" in got["report.xml: failures <= 1"]["detail"]


def test_junit_formats(tmp_path):
    (tmp_path / "go.xml").write_text(GO_XML)
    (tmp_path / "jest.xml").write_text(JEST_XML)
    (tmp_path / "totals.xml").write_text(TOTALS_XML)
    got = checks({"junit": ["go.xml", {"path": "jest.xml"}, {"path": "totals.xml", "failures": 1, "tests": 8,
                                                              "skipped": 1, "passed": 6}]}, tmp_path)
    assert got["go.xml: failures 0"]["detail"] == "1 (wanted == 0) of 2 tests; failed: pkg.TestB (Failed)"
    assert got["jest.xml: failures 0"]["detail"] == ("1 (wanted == 0) of 2 tests; failed: math subtracts "
                                                     "(Error: expect(received).toBe(expected))")
    assert got["go.xml: errors 0"]["ok"] and got["jest.xml: errors 0"]["ok"]
    assert all(got[f"totals.xml: {k}"]["ok"] for k in ("failures 1", "tests 8", "skipped 1", "passed 6", "errors 0"))
    cases = "".join(f'<testcase classname="t" name="test_{i}"><failure message="boom {i}"/></testcase>' for i in range(5))
    (tmp_path / "many.xml").write_text(f"<testsuite>{cases}</testsuite>")
    detail = checks({"junit": "many.xml"}, tmp_path)["many.xml: failures 0"]["detail"]
    assert detail == ("5 (wanted == 0) of 5 tests; failed: t.test_0 (boom 0); t.test_1 (boom 1); t.test_2 (boom 2); "
                      "and 2 more")


def test_junit_missing_broken_or_old(tmp_path):
    got = checks({"junit": {"path": "out/report.xml"}}, tmp_path)
    assert got == {"out/report.xml: JUnit report": {
        "check": "out/report.xml: JUnit report", "ok": False,
        "detail": "file missing: the test command wrote no report there (pytest: add --junitxml=out/report.xml)"}}
    (tmp_path / "bad.xml").write_text("<testsuite><testcase")
    assert "not a JUnit XML report" in checks({"junit": "bad.xml"}, tmp_path)["bad.xml: JUnit report"]["detail"]
    (tmp_path / "page.xml").write_text("<html><body/></html>")
    assert "the root element is <html>" in checks({"junit": "page.xml"}, tmp_path)["page.xml: JUnit report"]["detail"]
    (tmp_path / "old.xml").write_text(GO_XML)
    os.utime(tmp_path / "old.xml", (time.time() - 3600, time.time() - 3600))
    got = checks({"junit": "old.xml"}, tmp_path, run_started=time.time())
    assert not got["old.xml: JUnit report"]["ok"] and "written before this run started" in got["old.xml: JUnit report"]["detail"]
    assert "old.xml: failures 0" in checks({"junit": "old.xml"}, tmp_path), "no run_started: no age check"


# ---- numbers printed to the log or a file --------------------------------------------------------------------

LOG = "epoch 1 val_loss=0.52\nepoch 2 val_loss=0.31\nepoch 3 val_loss=0.25\n12 passed in 0.3s\n"


def test_regex_metrics(tmp_path):
    got = checks({"metrics": {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.3", r"(\d+) passed": ">= 12",
                                         "acc=([0-9.]+)": ">= 0.9", r"\d+(?= passed)": 12}}}, tmp_path, stdout=LOG)
    assert got["stdout /val_loss=([0-9.eE+-]+)/ < 0.3"] == {
        "check": "stdout /val_loss=([0-9.eE+-]+)/ < 0.3", "ok": True,
        "detail": "0.25 (wanted < 0.3); the last of 3 matches: 'val_loss=0.25'"}
    assert got[r"stdout /(\d+) passed/ >= 12"]["detail"] == "12 (wanted >= 12.0); the only match: '12 passed'"
    assert got["stdout /acc=([0-9.]+)/ >= 0.9"] == {"check": "stdout /acc=([0-9.]+)/ >= 0.9", "ok": False,
                                                     "detail": "no match in stdout (4 lines)"}
    assert got[r"stdout /\d+(?= passed)/ 12"]["ok"], "without a group, the whole match is the number"
    got = checks({"metrics": {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.2"}}}, tmp_path, stdout=LOG)
    assert not got["stdout /val_loss=([0-9.eE+-]+)/ < 0.2"]["ok"]
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "train.log").write_text("step 100 loss 1.5e-3.\n")
    got = checks({"metrics": {"logs/train.log": {"loss ([0-9.eE+-]+)": "~= 0.0015 +- 0.0001"}, "nope.log": {"x(1)": 1}}},
                 tmp_path)
    assert got["logs/train.log /loss ([0-9.eE+-]+)/ ~= 0.0015 +- 0.0001"]["ok"]
    assert got["nope.log /x(1)/ 1"] == {"check": "nope.log /x(1)/ 1", "ok": False, "detail": "file missing"}


# ---- gpu ---------------------------------------------------------------------------------------------------

WINDOW = {"gpu": "NVIDIA A10G", "busy_pct": 20, "mean_util_pct": 15, "max_mem_mib": 9000.0, "readings": 6}


def test_gpu_checks(tmp_path):
    exp = {"gpu": {"busy_pct": ">= 50", "max_mem_mib": "<= 15000", "mean_util_pct": "between 10 and 100"}}
    got = checks(exp, tmp_path, gpu=WINDOW)
    assert got["gpu busy_pct >= 50"] == {"check": "gpu busy_pct >= 50", "ok": False,
                                         "detail": "20 (wanted >= 50.0) on NVIDIA A10G, 6 reading(s)"}
    assert got["gpu max_mem_mib <= 15000"]["ok"] and got["gpu mean_util_pct between 10 and 100"]["ok"]
    got = checks(exp, tmp_path, gpu=None)
    assert [c["detail"] for c in got.values()] == [expect.NO_GPU] * 3 and not any(c["ok"] for c in got.values())
    assert expect.NO_GPU == "no GPU readings (nvidia-smi not found or the step was too short)"


def test_gpu_window_reaches_the_checks(tmp_path, monkeypatch):
    with monkeypatch.context() as m:                        # no sampling thread, even on a machine with a GPU
        m.setattr(executor.shutil, "which", lambda name: None)
        sampler = executor.GpuSampler()
    sampler.samples = [(80.0, 1000.0), (5.0, 1200.0)]
    assert sampler.window(0) == {"gpu": sampler.name, "busy_pct": 50, "mean_util_pct": 42, "max_mem_mib": 1200.0,
                                 "readings": 2}
    step = C._step("s", {"name": "train", "run": "echo hi", "expect": {"gpu": {"busy_pct": ">= 10"},
                                                                       "duration": {"seconds": "< 60"}}}, tmp_path)
    r = executor.run_step(step, project=tmp_path, workdir=tmp_path, step_dir=tmp_path / "s1", base_env=dict(os.environ),
                          gpu=lambda: WINDOW)
    assert r["gpu"] == WINDOW and r["status"] == "passed", r["expectations"]
    assert json.loads((tmp_path / "s1" / "result.json").read_text())["gpu"] == WINDOW
    r = executor.run_step(step, project=tmp_path, workdir=tmp_path, step_dir=tmp_path / "s2", base_env=dict(os.environ))
    assert r["status"] == "failed" and {"check": "gpu busy_pct >= 10", "ok": False, "detail": expect.NO_GPU} in r["expectations"]


# ---- images ------------------------------------------------------------------------------------------------

def test_image_like_a_known_good_one(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    (tmp_path / "golden").mkdir()
    Image.new("RGB", (40, 30), (255, 255, 255)).save(tmp_path / "golden" / "plot.png")
    Image.new("RGB", (80, 60), (255, 255, 255)).save(tmp_path / "big.png")
    half = Image.new("RGB", (40, 30), (255, 255, 255))
    half.paste((0, 0, 0), (0, 0, 20, 30))
    half.save(tmp_path / "half.png")
    Image.new("RGBA", (40, 30), (255, 255, 255, 255)).save(tmp_path / "rgba.png")
    got = checks({"image": {"big.png": {"like": "golden/plot.png"}, "half.png": {"like": "golden/plot.png", "max_diff": 0.6},
                            "rgba.png": {"like": "golden/plot.png", "max_diff": 0}}}, tmp_path)
    assert got["big.png looks like golden/plot.png"] == {
        "check": "big.png looks like golden/plot.png", "ok": True,
        "detail": "0.0 (wanted <= 0.02), the mean difference per pixel and channel (0 identical, 1 opposite), "
                  "after resizing it from 80x60 to the golden's 40x30"}
    assert got["half.png looks like golden/plot.png"]["detail"].startswith("0.5 (wanted <= 0.6)")
    assert got["rgba.png looks like golden/plot.png"]["ok"]
    got = checks({"image": {"half.png": {"like": "golden/plot.png"}}}, tmp_path)
    assert not got["half.png looks like golden/plot.png"]["ok"]
    got = checks({"image": {"none.png": {"like": "golden/plot.png"}, "half.png": {"like": "golden/none.png"}}}, tmp_path)
    assert got["none.png looks like golden/plot.png"]["detail"] == "file missing"
    assert "known-good image none.png is missing" in got["half.png looks like golden/none.png"]["detail"]


def test_image_without_pillow(tmp_path, monkeypatch):
    (tmp_path / "a.png").write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "PIL", None)
    got = checks({"image": {"a.png": {"like": "a.png"}}}, tmp_path)
    assert not got["a.png looks like a.png"]["ok"] and "pip install pillow" in got["a.png looks like a.png"]["detail"]


# ---- duration, and the numbers reports and loops read back ---------------------------------------------------

def test_duration(tmp_path):
    got = checks({"duration": {"minutes": "<= 1", "seconds": "between 60 and 120"}}, tmp_path, seconds=90.04)
    assert got["duration minutes <= 1"] == {"check": "duration minutes <= 1", "ok": False, "detail": "1.501 (wanted <= 1.0)"}
    assert got["duration seconds between 60 and 120"] == {"check": "duration seconds between 60 and 120", "ok": True,
                                                          "detail": "90.04 (wanted between 60 and 120)"}
    assert not checks({"duration": {"seconds": "< 0.05"}}, tmp_path, seconds=0.1)["duration seconds < 0.05"]["ok"]


def test_new_numbers_reach_metrics_and_the_loop(tmp_path):
    (tmp_path / "report.xml").write_text(PYTEST_XML)
    (tmp_path / "m.json").write_text(json.dumps({"acc": 0.935}))
    exp = {"junit": {"path": "report.xml", "tests": ">= 2"}, "metrics": {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.3"}},
           "gpu": {"busy_pct": ">= 10"}, "duration": {"minutes": "<= 5"}, "json": {"m.json": {"acc": "~= 0.93 +- 0.01"}}}
    found = expect.check(exp, workdir=tmp_path, result={"kind": "run", "exit_code": 0, "stdout": LOG, "gpu": WINDOW,
                                                         "seconds": 30})
    rep = {"steps": [{"name": "s", "status": "failed", "expectations": found}]}
    assert R._metrics(rep) == {"s | m.json: acc ~= 0.93 +- 0.01": 0.935, "s | report.xml: failures 0": 1.0,
                 "s | report.xml: errors 0": 1.0, "s | report.xml: tests >= 2": 4.0,
                 "s | stdout /val_loss=([0-9.eE+-]+)/ < 0.3": 0.25, "s | gpu busy_pct >= 10": 20.0,
                 "s | duration minutes <= 5": 0.5}
    assert LP.parse_metric("check:val_loss") == ("check", "val_loss")
    assert LP.parse_metric("check:report.xml: tests") == ("check", "report.xml: tests")
    assert LP.parse_metric("outputs/m.json:acc") == ("outputs/m.json", "acc")
    with pytest.raises(LP.LoopError, match="check:SUBSTRING"):
        LP.parse_metric("check:")
    assert LP.metric_value(rep, "check", "val_loss") == 0.25
    assert LP.metric_value(rep, "check", "tests") == 4.0
    assert LP.metric_value(rep, "check", "no such check") is None
    assert LP.metric_value(rep, "m.json", "acc") == 0.935, "FILE:KEY still works"


# ---- end to end ----------------------------------------------------------------------------------------------

TINY_TESTS = """\
def test_ok():
    assert 1 + 1 == 2


def test_broken():
    assert 1 + 1 == 3, "math is broken"
"""
TRAIN = """\
import json
for i, loss in enumerate([0.52, 0.31, 0.25]):
    print(f"epoch {i} val_loss={loss}")
json.dump({"accuracy": 0.934}, open("metrics.json", "w"))
"""


def test_plan_with_junit_regex_metric_and_tolerance(tmp_path):
    root = tmp_path / "suite"
    (root / "tests_gen").mkdir(parents=True)
    (root / "tests_gen" / "test_tiny.py").write_text(TINY_TESTS)
    (root / "train.py").write_text(TRAIN)
    write_config(root, {"project": "suite", "plans": {"check": {"steps": [
        {"name": "tests", "run": "python -m pytest -q -p no:cacheprovider --junitxml=report.xml tests_gen",
         "max_minutes": 2, "continue_on_failure": True, "expect": {"exit_code": 1, "junit": {"path": "report.xml", "tests": 2}}},
        {"name": "train", "run": "python train.py", "max_minutes": 2,
         "expect": {"json": {"metrics.json": {"accuracy": "~= 0.93 +- 0.01"}}, "duration": {"minutes": "<= 2"},
                    "metrics": {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.3"}}}}]}}})
    r = cli(root, "run", "check", "--wait", "120")
    assert r.returncode == 1, r.stdout + r.stderr
    out = r.stdout
    assert "## FAIL: tests" in out and "expected report.xml: failures 0: 1 (wanted == 0) of 2 tests; failed: " in out
    assert re.search(r"failed: (tests_gen\.)?test_tiny\.test_broken \(AssertionError: math is broken", out), out
    assert "- train: metrics.json: accuracy ~= 0.93 +- 0.01 - 0.934 (wanted 0.93 ± 0.01; differs by +0.004)" in out
    assert "- train: stdout /val_loss=([0-9.eE+-]+)/ < 0.3 - 0.25 (wanted < 0.3); the last of 3 matches" in out
    assert "| PASS | train |" in out and "| FAIL | tests |" in out
    (root / "train.py").write_text(TRAIN.replace("0.25", "0.21"))
    r = cli(root, "run", "check", "--wait", "120")
    assert "train | stdout /val_loss=([0-9.eE+-]+)/ < 0.3: 0.25 -> 0.21" in r.stdout, r.stdout
    h = cli(root, "history", "check").stdout
    assert "tests | report.xml: failures 0: 1 -> 1" in h and "0.25 -> 0.21" in h, h


def git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def test_loop_on_a_number_printed_to_the_log(tmp_path):
    root = tmp_path / "scoring"
    root.mkdir()
    (root / "score.py").write_text("from settings import SCORE\nprint('score', SCORE)\n")
    (root / "settings.py").write_text("SCORE = 1\n")
    (root / ".gitignore").write_text("__pycache__/\n")
    write_config(root, {"project": "scoring", "plans": {"score": {"steps": [
        {"name": "score", "run": "python score.py", "max_minutes": 1,
         "expect": {"metrics": {"stdout": {r"score (\d+)": ">= 0"}}}}]}}})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    r = cli(root, "loop", "start", "score", "--metric", "check:score (", "--budget-usd", "0")
    assert r.returncode == 0 and "BASELINE: score ( 1, run" in r.stdout, r.stdout
    (root / "settings.py").write_text("SCORE = 3\n")
    r = cli(root, "loop", "try", "raise the score")
    assert r.returncode == 0 and "KEPT try 1: score ( 3 (best was 1, +2)" in r.stdout, r.stdout
    assert "max check containing 'score ('" in cli(root, "loop", "status").stdout
    assert cli(root, "loop", "stop").returncode == 0
    r = cli(root, "loop", "start", "score", "--metric", "check:accuracy", "--budget-usd", "0")
    assert ("BASELINE: accuracy none, run " in r.stdout and " passed, but no check whose name contains 'accuracy' found "
            "a number (checks that did: stdout /score (\\d+)/ >= 0)" in r.stdout), r.stdout
