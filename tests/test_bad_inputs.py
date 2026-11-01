"""Messy projects and hostile inputs: every bad testbench.yaml gets a plain message that says what to write, never a
traceback, and no check passes when it should fail. One example per bug found by tests/test_fuzz.py and by hand."""
import json
import math
import os
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import nbformat
import pytest
import yaml

from agent_testbench import budget, config as C, expect, loop as LP, prices, provenance, report as R, scaffold, ui
from agent_testbench.backends import colab_backend, modal_backend
from conftest import cli

STEP = "plans:\n  p:\n    steps:\n      - name: a\n        run: 'true'\n"


def load(tmp_path: Path, text: str) -> dict:
    (tmp_path / "testbench.yaml").write_bytes(text.encode() if isinstance(text, str) else text)
    return C.load(tmp_path)


# ---- testbench.yaml: a ConfigError that says what to write, never a traceback -----------------------------------

@pytest.mark.parametrize("text, message", [
    ("project: p\nbudget: [1]\n" + STEP, "`budget:` is a mapping of settings"),
    ("project: p\nsessions: local\n" + STEP, "`sessions:` is a mapping of settings"),
    ("project: p\nmodal: abc\n" + STEP, "`modal:` is a mapping of settings"),
    ("project: p\ndocker: [1]\nplans: {p: {backend: docker, steps: [{name: a, run: x}]}}\n", "`docker:` is a mapping"),
    ("project: p\nsessions: {backend: [local]}\n" + STEP, "`sessions.backend` is ['local']; use one of"),
    ("project: p\nplans: {p: {backend: {a: 1}, steps: [{name: a, run: x}]}}\n", "`plans.p.backend` is {'a': 1}"),
    ("project: p\nplans: {p: {steps: [{name: [a], run: x}]}}\n", "`plans.p.steps[0].name` is a short text"),
    ("project: p\nplans: {p: {steps: [{name: a, notebook: 5}]}}\n", "`plans.p.steps[0].notebook` is the notebook's path"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expectt: {files: [a]}}]}}\n", "has no 'expectt' (did you mean 'expect'?)"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, env: {1: a}}]}}\n", "maps variable NAMES to values"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, artifacts: [{a: 1}]}]}}\n", "`plans.p.steps[0].artifacts` must be a list of paths"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, continue_on_failure: 'no'}]}}\n", "is true or false"),
    ("project: p\nplans: {p: {steps: [{name: a, web: {url: 'http://x', viewport: wide}}]}}\n", "a width and height"),
    ("project: p\nexclude: .git\n" + STEP, "`exclude:` is a list of file names or patterns"),
    ("project: p\nguard: {ask: git push}\n" + STEP, "`guard.ask` is a list of commands"),
    ("project: p\nplans: {5: {steps: [{name: a, run: x}]}}\n", "quote it, \"5\":"),
    ("project: p\nplans: {yes: {steps: [{name: a, run: x}]}}\n", "plan names use letters"),
    # NaN, infinity and true slip past `< 0` and `<= 0`: a NaN budget or time limit would refuse nothing
    ("project: p\nbudget: {max_run_usd: .nan}\n" + STEP, "`budget.max_run_usd` must be a number of US dollars"),
    ("project: p\nbudget: {max_day_usd: .inf}\n" + STEP, "`budget.max_day_usd` must be a number"),
    ("project: p\nsessions: {max_minutes: .nan}\n" + STEP, "`sessions.max_minutes` must be a positive number"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, max_minutes: .nan}]}}\n", "max_minutes` must be a positive number"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, max_minutes: .inf}]}}\n", "max_minutes` must be a positive number"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, max_minutes: true}]}}\n", "max_minutes` must be a positive number"),
    ("project: p\npricing: {modal_gpu_per_s: {T4: .nan}}\n" + STEP, "`pricing.modal_gpu_per_s.T4` is nan; a price is a number"),
    ("project: p\npricing: {modal_cpu_core_per_s: cheap}\n" + STEP, "`pricing.modal_cpu_core_per_s` is 'cheap'"),
    ("project: p\npricing: [1]\n" + STEP, "`pricing:` is a mapping of settings"),
    ("project: p\npricing: {modal_cpu_core_per_s: {}}\nplans: {p: {backend: modal, steps: [{name: a, run: x}]}}\n",
     "`pricing.modal_cpu_core_per_s` is {}; write one price"),
    ("project: p\npricing: {colab_units_per_hour: 5}\nplans: {p: {backend: colab, steps: [{name: a, run: x}]}}\n",
     "`pricing.colab_units_per_hour` is 5; write names and prices"),
    ("project: p\nmodal: {cpu: -100}\nplans: {p: {backend: modal, steps: [{name: a, run: x}]}}\n", "`modal.cpu` is -100"),
    ("project: p\nplans: {p: {backend: modal, memory_gb: .nan, steps: [{name: a, run: x}]}}\n", "`plans.p.memory_gb` is nan"),
    ("project: p\nmodal: {pip: numpy}\nplans: {p: {backend: modal, steps: [{name: a, run: x}]}}\n", "`modal.pip` is a list"),
    ("project: p\nplans: {p: {backend: modal, gpu: 'T4:0', steps: [{name: a, run: x}]}}\n", "e.g. H100 or H100:2"),
    ("project: p\nplans: {p: {backend: modal, gpu: 'T4:²', steps: [{name: a, run: x}]}}\n", "e.g. H100 or H100:2"),
    ("project: p\nssh: {host: h, usd_per_hour: .nan}\nplans: {p: {backend: ssh, steps: [{name: a, run: x}]}}\n", "`ssh.usd_per_hour`"),
    ("project: p\nssh: {host: h, command: \"ssh -p '22\"}\nplans: {p: {backend: ssh, steps: [{name: a, run: x}]}}\n",
     "cannot be split into arguments (No closing quotation)"),
    ("project: p\ndocker: {usd_per_hour: .inf}\nplans: {p: {backend: docker, steps: [{name: a, run: x}]}}\n", "`docker.usd_per_hour`"),
    ("project: p\ndocker: {run_args: \"-v '/x\"}\nplans: {p: {backend: docker, steps: [{name: a, run: x}]}}\n", "cannot be split"),
    # a plan travels as JSON, which has no dates, sets or loops
    ("project: p\nplans: {p: {steps: [{name: a, run: x, params: {START: 2024-01-01}}]}}\n",
     "`plans.p.steps[0].params.START` is datetime.date(2024, 1, 1), which a plan cannot carry; write it in quotes"),
    ("project: p\nplans: &a {p: {steps: [{name: a, run: x, params: *a}]}}\n", "contains itself"),
    # YAML itself: duplicate keys drop the first silently; deep nesting and huge integers raise past yaml.YAMLError
    ("project: p\n" + STEP + "  p:\n    steps: [{name: b, run: x}]\n", "line 7: 'p' is given twice in the same mapping (also on line 3)"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {a.json: {x: 1}}, json: {b.json: {y: 1}}}}]}}\n",
     "'json' is given twice"),
    ("project: p\nplans: " + "[" * 3000 + "]" * 3000 + "\n", "nests too deeply to read"),
    ("project: p\nbudget: {max_run_usd: " + "9" * 5000 + "}\n", "is not valid YAML: Exceeds the limit"),
    (b"project: p\nplans: {}\n\xff\xfe", "cannot be read"),
    # expectations keyed by something that is not a path, or that can never fail
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {5: {a: 1}}}}]}}\n", "has 5 where a file's path goes"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {text: {7: hi}}}]}}\n", "where a file's path (or stdout) goes"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {metrics: {stdout: {5: 1}}}}]}}\n", "where a regular expression goes"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {text: {stdout: ''}}}]}}\n", "an empty text is found in anything"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {text: {log.txt: []}}}]}}\n", "a list of texts"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {}}}}]}}\n", "is empty, so nothing would be"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {metrics: {stdout: {}}}}]}}\n", "is empty, so nothing would be"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {files: ['']}}]}}\n", "is a list of paths or globs"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {web: {text: [5]}}}]}}\n", "is a list of texts the page must show"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {web: {title: ''}}}]}}\n", "text the page's title contains"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {junit: {}}}]}}\n", "needs `path:`"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {junit: ''}}]}}\n", "needs `path:`"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: '=> 0.9'}}}}]}}\n", "=> is not a comparison"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: '≥ 0.9'}}}}]}}\n", "≥ is not a comparison"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: '>='}}}}]}}\n", "has no value after its comparison"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: '>= nan'}}}}]}}\n", "never holds"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: '~= 1 +- inf'}}}}]}}\n", "must be real numbers"),
    ("project: p\nplans: {p: {steps: [{name: a, run: x, expect: {json: {m.json: {a: 'between nan and 1'}}}}]}}\n", "real numbers"),
])
def test_bad_config_says_what_to_write(tmp_path, text, message):
    with pytest.raises(C.ConfigError) as got:
        load(tmp_path, text)
    assert message in str(got.value)


def test_bad_config_from_the_cli_is_exit_2_and_no_traceback(tmp_path):
    (tmp_path / "testbench.yaml").write_text("project: p\nbudget: lots\n" + STEP)
    r = cli(tmp_path, "plans")
    assert r.returncode == 2 and "`budget:` is a mapping" in r.stderr and "Traceback" not in r.stderr
    (tmp_path / "testbench.yaml").write_text("project: p\npricing: {modal_mem_gib_per_s: {a: 1}}\n" + STEP)
    r = cli(tmp_path, "prices")                          # every backend's prices, used or not
    assert r.returncode == 2 and "`pricing.modal_mem_gib_per_s`" in r.stderr and "Traceback" not in r.stderr


def test_what_still_loads(tmp_path):
    cfg = load(tmp_path, "project: p\nbudget:\npricing:\nsessions:\n" + STEP)       # sections with every line commented out
    assert cfg["budget"]["max_run_usd"] == 2.0 and cfg["sessions"]["backend"] == "local"
    cfg = load(tmp_path, "project: p\nbase: &b {run: x, max_minutes: 1}\nplans:\n  p:\n    steps:\n"
                         "      - {<<: *b, name: a, max_minutes: 2}\n      - {<<: *b, name: b}\n")
    assert [s["max_minutes"] for s in cfg["plans"]["p"]["steps"]] == [2, 1], "a merge key may be overridden"
    cfg = load(tmp_path, "project: p\nplans: {p: {steps: [{name: 2024, run: x}]}}\n")
    assert cfg["plans"]["p"]["steps"][0]["name"] == "2024", "a number as a step name is its text"
    cfg = load(tmp_path, "project: p\nplans: {p: {steps: [{name: a, web: {url: 'http://x', actions: }}]}}\n")
    assert cfg["plans"]["p"]["steps"][0]["web"]["actions"] == []


def test_an_unpriceable_worst_case_is_refused(tmp_path):
    cfg = load(tmp_path, "project: p\n" + STEP)
    for cost in (math.nan, -1.0, math.inf):
        est = {"what": "plan p", "worst_case_usd": cost, "max_minutes": 10.0, "usd_per_minute": 0.0}
        ok, message, _ = budget.check(cfg, est, approve=True, how_to_lower="Lower max_minutes")
        assert not ok and "Refused" in message, cost


# ---- expectations: nothing passes that should fail --------------------------------------------------------------

@pytest.mark.parametrize("actual, rule, ok", [
    (True, ">= 0.9", False), (True, "> 0", False), (1, True, False), (True, 1, False), (0, False, False),
    (True, "== true", True), (False, "false", True), (True, True, True), (False, "!= 0", True),
    (math.nan, "!= 0", False), (math.nan, "!= nan", False), (0.5, "!= nan", True), (math.nan, "between -inf and inf", False),
    (math.inf, ">= 0.9", False), (-math.inf, "< 0.3", False), ("inf", ">= 0.9", False), (math.inf, "~= 1 +- 1", False),
    (5, "between 0 and inf", True), (10 ** 400, ">= 1", True), (10 ** 400, "~= 1 +- 1", False),
    (1.1e-12, "~= 1e-13 +- 1e-14", False), (1e15 + 500, "~= 1000000000000000 +- 0", False),
    (0.94, "~= 0.93 +- 0.01", True), (0.1 + 0.2, "~= 0.3 +- 0", True), (105, "~= 100 +- 5%", True),
    (1, "=> 1", False), (1, "= 1", False), (1, ">=", False), (math.nan, math.nan, False),
])
def test_compare_does_not_pass_what_should_fail(actual, rule, ok):
    assert expect.compare(actual, rule)[0] is ok


def test_json_and_text_from_an_earlier_run_do_not_count(tmp_path):
    (tmp_path / "m.json").write_text('{"acc": 0.99}')
    (tmp_path / "train.log").write_text("finished\n")
    old = time.time() - 3600
    for f in ("m.json", "train.log"):
        os.utime(tmp_path / f, (old, old))
    got = expect.check({"json": {"m.json": {"acc": ">= 0.9"}}, "text": {"train.log": "finished"}}, workdir=tmp_path,
                       result={"kind": None, "run_started": time.time()})
    assert [c["ok"] for c in got] == [False, False] and all("before this run started" in c["detail"] for c in got)
    got = expect.check({"json": {"m.json": {"acc": ">= 0.9"}}, "text": {"train.log": "finished"}}, workdir=tmp_path,
                       result={"kind": None, "run_started": old})
    assert all(c["ok"] for c in got)


def test_json_that_cannot_be_read_fails_the_check(tmp_path):
    (tmp_path / "bin.json").write_bytes(b"\xff\xfe{")
    (tmp_path / "big.json").write_text('{"n": ' + "9" * 5000 + "}")
    (tmp_path / "deep.json").write_text("[" * 100000 + "]" * 100000)
    (tmp_path / "huge.json").write_text('{"n": ' + "9" * 400 + ', "nan": NaN, "inf": Infinity}')
    (tmp_path / "dir.json").mkdir()
    exp = {"json": {"bin.json": {"n": ">= 1"}, "big.json": {"n": ">= 1"}, "deep.json": {"0": ">= 1"}, "dir.json": {"n": ">= 1"},
                    "huge.json": {"n": "~= 1 +- 1", "nan": "!= 0", "inf": "> 0"}}}
    got = {c["check"]: c for c in expect.check(exp, workdir=tmp_path, result={"kind": None})}
    assert not any(c["ok"] for c in got.values())
    assert "not valid JSON" in got["bin.json"]["detail"] and "cannot be read" in got["dir.json"]["detail"]
    assert "is not a finite number" in got["huge.json: nan != 0"]["detail"]


def test_a_json_pattern_through_a_symlink_loop_still_names_one_file(tmp_path):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "metrics.json").write_text('{"a": 1}')
    (tmp_path / "loop").symlink_to(tmp_path)
    got = expect.check({"json": {"**/metrics.json": {"a": "== 1"}}}, workdir=tmp_path, result={"kind": None})
    assert got[0]["ok"], got


@pytest.mark.parametrize("xml, failures", [
    ('<testsuites><testsuite tests="9" failures="9"/><testsuite><testcase name="a"/></testsuite></testsuites>', 9),
    ('<testsuite tests="1" failures="1"><testcase name="a"/><failure message="setup broke"/></testsuite>', 1),
    ('<testsuite tests="2" failures="1"><testcase name="a"><failure/></testcase><testcase name="b"/></testsuite>', 1),
])
def test_junit_failures_the_suite_totals_report_count(tmp_path, xml, failures):
    (tmp_path / "r.xml").write_text(xml)
    assert expect.read_junit(tmp_path / "r.xml")["failures"] == failures
    got = expect.check({"junit": "r.xml"}, workdir=tmp_path, result={"kind": None})
    assert not next(c for c in got if c["check"] == "r.xml: failures 0")["ok"]


# ---- reports, pages and proof -------------------------------------------------------------------------------

def _report(name="a|b\nc", text="\x1b[31mred\x1b[0m \x00 \ud800 <script>x</script>"):
    return {"run_id": "r1", "plan": "p", "backend": "local", "status": "failed", "started": 1700000000, "minutes": 1.0,
            "steps": [{"name": name, "kind": "run", "status": "failed", "minutes": 0.1, "dir": "steps/00-a",
                       "error": {"ename": "E", "evalue": text}, "stdout_tail": text,
                       "expectations": [{"check": "c\n# PASS", "ok": False, "detail": text}]},
                      {"name": "ok", "kind": "run", "status": "weird-new-status", "minutes": 0.1, "dir": "steps/01-ok"}],
            "skipped": ["s|t"], "evidence": [{"path": "steps/x", "bytes": "<b>", "sha256": "<script>alert(1)</script>"}],
            "delta": {"previous_run": "r0", "previous_status": "passed", "metrics_moved": {"k": ["a", "b"]}}}


def test_junit_export_is_valid_xml_with_colored_output_and_bad_unicode():
    text = R.render_junit(_report())
    root = ET.fromstring(text.encode("utf-8"))
    assert root.find(".//system-out").text.startswith("[31mred[0m")


def test_markdown_and_html_survive_hostile_names(tmp_path):
    md = R.render(_report())
    md.encode("utf-8")
    rows = [ln for ln in md.splitlines() if ln.startswith("| ")]
    assert all(ln.replace("\\|", "").count("|") == 6 for ln in rows if "SKIPPED" not in ln), rows
    assert "\n# PASS" not in md and "WEIRD-NEW-STATUS" in md
    page = R.render_html(_report(), tmp_path)
    page.encode("utf-8")
    assert "<script>" not in page and "&lt;b&gt;" in page


def test_check_number_is_never_nan_or_inf():
    assert [R.check_number({"detail": d}) for d in ("nan (wanted >= 0)", "inf (wanted", "1e999", "0.5 (wanted")] == \
        [None, None, None, 0.5]
    assert LP.metric_value({"steps": [{"expectations": [{"detail": "1 (wanted"}]}]}, LP.CHECK, "x") is None


def test_ui_leaves_out_reports_it_cannot_read(tmp_path):
    runs = tmp_path / ".testbench" / "runs"
    for name, text in (("a", '{"run_id": "a", "status": "pas'), ("b", "[1]"), ("c", b"\xff\xfe"),
                       ("d", '{"run_id": 5, "status": "passed", "plan": ["p"], "started": "x"}'),
                       ("e", '{"run_id": "e", "status": "passed", "plan": "p", "started": 1e300, "steps": []}'),
                       ("f", json.dumps({"run_id": "f", "status": "passed", "plan": "p", "started": 1700000000, "steps": []}))):
        (runs / name).mkdir(parents=True)
        (runs / name / "state.json").write_text("{}")
        (runs / name / "report.json").write_bytes(text if isinstance(text, bytes) else text.encode())
    (tmp_path / ".testbench" / "loops" / "x").mkdir(parents=True)
    (tmp_path / ".testbench" / "loops" / "x" / "state.json").write_text("{}")
    assert R.load(runs / "a") is None and R.load(runs / "b") is None and R.load(runs / "c") is None
    index = ui.build(tmp_path, {"project": "p", "plans": {"p": {}}}).read_text()
    assert "f/report.html" in index and "5 run(s) whose report.json cannot be read: a, b, c, d, e" in index
    assert "its state.json cannot be read" in index


def test_verify_catches_an_edited_or_broken_evidence_list(tmp_path):
    (tmp_path / "steps").mkdir()
    (tmp_path / "steps" / "log.txt").write_text("hi")
    files, total = provenance.evidence(tmp_path)
    assert provenance.verify(tmp_path, {"evidence": files, "evidence_sha256": total}) == []
    (tmp_path / "steps" / "log.txt").write_text("forged")
    forged = [{**files[0], "sha256": provenance._sha256_file(tmp_path / "steps" / "log.txt")}]
    assert "does not add up to its evidence_sha256" in provenance.verify(tmp_path, {"evidence": forged,
                                                                                    "evidence_sha256": total})[0]
    for broken in ("abc", [5], [{"path": ["x"], "sha256": "a"}], {"a": 1}):
        assert "not a list of {path, sha256}" in provenance.verify(tmp_path, {"evidence": broken})[0]


@pytest.mark.posix
def test_evidence_with_a_file_name_that_is_not_utf8(tmp_path):
    (tmp_path / "steps").mkdir()
    with open(os.path.join(os.fsencode(tmp_path / "steps"), b"caf\xe9.png"), "wb") as f:
        f.write(b"x")
    files, total = provenance.evidence(tmp_path)
    rep = json.loads(json.dumps({"evidence": files, "evidence_sha256": total}))      # as report.json keeps it
    assert provenance.verify(tmp_path, rep) == []
    rep.update(status="passed", steps=[{"name": "a", "kind": "run", "status": "passed", "dir": "steps",
                                        "artifacts": [files[0]["path"][6:]]}])
    R.render(rep).encode("utf-8")


# ---- testbench init on odd projects, prices, loops ------------------------------------------------------------------

def test_detect_on_odd_projects_writes_yaml_that_loads(tmp_path):
    (tmp_path / "package.json").write_text("[1, 2]")
    assert scaffold.detect(tmp_path) == []
    (tmp_path / "package.json").write_text('{"scripts": "dev"}')
    assert scaffold.detect(tmp_path) == []
    (tmp_path / "package.json").unlink()
    (tmp_path / "loop").symlink_to(tmp_path)
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell("x = 1 #@param")])
    for name in ("weird name: #1 {x}.ipynb", "😀 émoji.ipynb", "true.ipynb", "a'b\"c.ipynb"):
        nbformat.write(nb, str(tmp_path / name))
    (tmp_path / "int.ipynb").write_text(json.dumps({"cells": [{"cell_type": "code", "source": 5, "metadata": {},
                                                    "outputs": [], "execution_count": None}], "nbformat": 4,
                                         "nbformat_minor": 5, "metadata": {}}))
    found = scaffold.detect(tmp_path)
    raw = yaml.safe_load("project: p\nplans:\n" + "".join(text for _, text, _ in found))
    cfg = C.validate(raw, tmp_path)
    notebooks = {s["notebook"] for p in cfg["plans"].values() for s in p["steps"] if s["kind"] == "notebook"}
    assert {"weird name: #1 {x}.ipynb", "😀 émoji.ipynb", "true.ipynb", "a'b\"c.ipynb", "int.ipynb"} <= notebooks


def test_detect_skips_files_it_cannot_read(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    (tmp_path / "package.json").write_text('{"scripts": {"dev": "vite"}}')
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name in ("pyproject.toml", "package.json"):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read_text)
    assert scaffold.detect(tmp_path) == []


def test_a_hostile_price_catalog_never_lowers_a_price_or_adds_a_free_gpu(tmp_path):
    cfg = load(tmp_path, "project: p\n" + STEP)
    for catalog in ({"modal": [1], "colab": "x"}, {"modal": {"gpu_per_s": 5}, "colab": {"units_per_hour": "abc"}},
                    {"modal": {"gpu_per_s": {"T4": -1, "H100": "lots", "X9": "lots", "Y9": 1e400}},
                     "colab": {"units_per_hour": {"NEW": None, "T4": math.nan}}}):
        prices.cache_path().parent.mkdir(parents=True, exist_ok=True)
        prices.cache_path().write_text(json.dumps(catalog))
        assert modal_backend.backend.gpus == modal_backend.backend.built_in_gpus
        assert colab_backend.backend.gpus == colab_backend.backend.built_in_gpus
        for gpu, per_s in modal_backend.GPU_PER_S.items():
            assert modal_backend.backend.rate_per_minute(cfg, {"backend": "modal", "gpu": gpu}) >= per_s * 60
        assert modal_backend.backend.prices(cfg) and colab_backend.backend.prices(cfg)
    prices.cache_path().write_text(json.dumps({"modal": {"gpu_per_s": {"h100x": 0.01, "free1": 0}},
                                               "colab": {"units_per_hour": {"tpu9": 3}}}))
    assert modal_backend.backend.gpus[-1:] == ("H100X",) and colab_backend.backend.gpus[-1:] == ("TPU9",)
    assert modal_backend.backend.rate_per_minute(cfg, {"backend": "modal", "gpu": "h100x"}) >= 0.6, "not priced at $0"
    assert colab_backend.backend.rate_per_minute(cfg, {"backend": "colab", "gpu": "tpu9"}) > 0


def test_loop_metric_and_numbers_are_checked(tmp_path):
    for spec in ("check: ", "file: ", ":acc", "check:"):
        with pytest.raises(LP.LoopError):
            LP.parse_metric(spec)
    assert LP.parse_metric(" m.json : acc ") == ("m.json", "acc")
    cfg = load(tmp_path, "project: p\n" + STEP)
    for kwargs in ({"budget_usd": math.nan}, {"budget_usd": 1, "max_hours": math.nan}, {"budget_usd": 1, "min_delta": math.inf}):
        with pytest.raises(LP.LoopError, match="is a number of"):
            LP.start(tmp_path, cfg, "p", "check:x", **kwargs)
