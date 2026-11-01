"""Other languages' test suites: the jest, vitest, go-test, cargo-test and playwright-ts recipes, `testbench init` on
JavaScript, Go and Rust projects, and `junit:` on the reports those runners really write (tests/data/junit/, made by
jest-junit 17, vitest 5, mocha 12, node 22's --test, Playwright 1.64, gotestsum 1.13, go-junit-report 2.1 and
cargo-nextest 0.9). Where a toolchain is on this machine, each recipe's local plan runs on a tiny project: it passes,
then fails naming the test that broke. The docker plans build the image their recipe describes and run offline only
with TESTBENCH_TEST_DOCKER_LANGS=1 (each pulls an official image and builds for minutes)."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

from agent_testbench import config as C, expect, recipes as RC, scaffold
from agent_testbench.backends.docker_backend import BASE_PIP
from agent_testbench.cli import INIT_TEMPLATE
from conftest import cli

DATA = Path(__file__).parent / "data" / "junit"
LANGUAGES = {"jest": ("jest-tests", "jest-sandbox"), "vitest": ("vitest-tests", "vitest-sandbox"),
             "go-test": ("go-tests", "go-sandbox"), "cargo-test": ("cargo-tests", "cargo-sandbox"),
             "playwright-ts": ("e2e", None)}
OFFICIAL = re.compile(r"(python|node|golang|rust):\d[\w.-]*")     # an official image, with a pinned version


def dockerfile(recipe: dict) -> list[str]:
    """The Dockerfile a recipe's header gives: the indented lines from its first `FROM` on, without their "#"."""
    head = recipe["header"]
    at = next((i for i, line in enumerate(head) if line.startswith("#     FROM ")), None)
    if at is None:
        return []
    out = []
    for line in head[at:]:
        if not line.startswith("#     "):
            break
        out.append(line[6:])
    return out


def junit_of(plan: dict) -> dict:
    return next(s["expect"]["junit"] for s in plan["steps"] if "junit" in (s.get("expect") or {}))


# ---- the recipes ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(LANGUAGES))
def test_each_language_recipe_loads_and_is_judged_by_its_runner_s_junit_report(tmp_path, name):
    r = RC.load(name)
    local, sandbox = LANGUAGES[name]
    assert list(r["plans"]) == [p for p in (local, sandbox) if p]
    text, _ = RC.compose(INIT_TEMPLATE.format(project="langs", plans=""), [r], fresh=True)
    cfg = C.validate(yaml.safe_load(text), tmp_path)
    assert "FILL IN" in text and r["header"][0] in text
    for plan_name in r["plans"]:
        plan, junit = cfg["plans"][plan_name], junit_of(cfg["plans"][plan_name])
        assert junit["failures"] == 0 and junit["errors"] == 0 and junit["tests"] == ">= 1", plan_name
        step = next(s for s in plan["steps"] if "junit" in s["expect"])
        assert junit["path"] in step["artifacts"], f"{plan_name}: the report comes back with the run"
        assert re.search(r'tests: ">= 1"\}\s+# FILL IN', text), "the test count is the person's to fill in"
    assert cfg["plans"][local]["backend"] == "local"
    if sandbox is None:
        assert "No docker plan" in "\n".join(r["header"]) and not dockerfile(r)
        return
    assert cfg["plans"][sandbox]["backend"] == "docker"
    assert cfg["docker"]["network"] == "none" and cfg["docker"]["setup"] == "none"
    assert cfg["docker"]["usd_per_hour"] == 0.0
    lines = dockerfile(r)
    local_steps = [s for s in cfg["plans"][local]["steps"] if "junit" in s["expect"]]
    sandbox_steps = [s for s in cfg["plans"][sandbox]["steps"] if "junit" in s["expect"]]
    pinned = "go run gotest.tools/gotestsum@v1.13.0"     # offline, the gotestsum the image installed at that version
    assert name != "go-test" or "RUN go install gotest.tools/gotestsum@v1.13.0" in lines
    assert [(s["run"].replace(pinned, "gotestsum"), s["env"]) for s in local_steps] == \
        [(s["run"], s["env"]) for s in sandbox_steps], "the container runs what the local plan does"
    froms = [ln.split()[1] for ln in lines if ln.startswith("FROM ")]
    assert froms[-1] == "python:3.13-trixie" and all(OFFICIAL.fullmatch(f) for f in froms), froms
    assert all("-trixie" in f for f in froms), "every image on the same Debian, so the binaries copied across run"
    pip = next(ln for ln in lines if ln.startswith("RUN pip install"))
    assert all(f'"{req}"' in pip for req in BASE_PIP), "what testbench runs in a container (docker_backend.BASE_PIP)"
    assert f"-t {r['data']['docker']['image']} ." in "\n".join(r["header"]), "the header builds the tag the plan uses"


def test_the_language_recipes_combine_with_each_other_and_the_rest(tmp_path):
    chosen = [RC.load(n) for n in ("go-test", "jest", "docker-sandbox", "pytest", "web-app", "playwright-ts")]
    text, notes = RC.compose(INIT_TEMPLATE.format(project="poly", plans=""), chosen, fresh=True)
    cfg = C.validate(yaml.safe_load(text), tmp_path)
    assert list(cfg["plans"]) == ["go-tests", "go-sandbox", "jest-tests", "jest-sandbox", "sandbox-tests", "sandbox-run",
                                  "tests", "ui", "ui-build", "e2e"]
    assert cfg["docker"]["image"] == "go-tests", "one docker: section per file: the first recipe's"
    assert any(n.startswith("Kept the `docker:` section") and "recipe jest's" in n for n in notes), notes


# ---- testbench init ---------------------------------------------------------------------------------------------

def _write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text if isinstance(text, str) else json.dumps(text, indent=2))
    return root


def _pkg(test=None, deps=None, **scripts):
    return {"name": "sum", "version": "1.0.0", "private": True, "scripts": {**({"test": test} if test else {}), **scripts},
            "devDependencies": deps or {}}


SUM_CJS = "function sum(a, b) { return a + b; }\nmodule.exports = { sum };\n"
SUM_ESM = "export function sum(a, b) { return a + b; }\n"
JEST_TEST = ('const { sum } = require("./sum");\n\ndescribe("sum", () => {\n'
             '  test("adds", () => { expect(sum(1, 2)).toBe(3); });\n'
             '  test("adds negatives", () => { expect(sum(-1, -1)).toBe(-2); });\n});\n')
VITEST_TEST = ('import { describe, test, expect } from "vitest";\nimport { sum } from "./sum.js";\n\ndescribe("sum", () => {\n'
               '  test("adds", () => { expect(sum(1, 2)).toBe(3); });\n'
               '  test("adds negatives", () => { expect(sum(-1, -1)).toBe(-2); });\n});\n')
NODE_TEST = ('const test = require("node:test");\nconst assert = require("node:assert");\nconst { sum } = require("../sum");\n\n'
             'test.describe("sum", () => {\n  test("adds", () => { assert.strictEqual(sum(1, 2), 3); });\n'
             '  test("adds negatives", () => { assert.strictEqual(sum(-1, -1), -2); });\n});\n')
GO_MOD = "module example.com/calc\n\ngo 1.22\n"
GO_CODE = "package calc\n\nfunc Add(a, b int) int { return a + b }\n"
GO_TEST = ('package calc\n\nimport "testing"\n\nfunc TestAdd(t *testing.T) {\n\tif got, want := Add(1, 2), 3; got != want {\n'
           '\t\tt.Fatalf("Add(1, 2) = %d, want %d", got, want)\n\t}\n}\n\nfunc TestNegatives(t *testing.T) {\n'
           '\tif Add(-1, -1) != -2 {\n\t\tt.Fatal("negatives")\n\t}\n}\n')
CARGO_TOML = '[package]\nname = "calc"\nversion = "0.1.0"\nedition = "2021"\n'
LIB_RS = ("pub fn add(a: i64, b: i64) -> i64 {\n    a + b\n}\n\n#[cfg(test)]\nmod tests {\n    use super::*;\n\n"
          "    #[test]\n    fn adds() {\n        assert_eq!(add(1, 2), 3);\n    }\n\n"
          "    #[test]\n    fn negatives() {\n        assert_eq!(add(-1, -1), -2);\n    }\n}\n")
NEXTEST = '[profile.ci]\nfail-fast = false\n\n[profile.ci.junit]\npath = "junit.xml"\n'

PROJECTS = {
    "jest": ({"package.json": _pkg("jest", {"jest": "30.5.2", "jest-junit": "17.0.0"}), "sum.js": SUM_CJS,
              "sum.test.js": JEST_TEST}, "jest-tests", "a jest suite (jest)"),
    "vitest": ({"package.json": {**_pkg("vitest", {"vitest": "5.0.5"}), "type": "module"}, "sum.js": SUM_ESM,
                "sum.test.js": VITEST_TEST}, "vitest-tests", "a vitest suite (vitest)"),
    "node": ({"package.json": _pkg("node --test"), "sum.js": SUM_CJS, "test/sum.test.js": NODE_TEST}, "node-tests",
             "a node --test suite (node --test)"),
    "mocha": ({"package.json": _pkg("mocha", {"mocha": "12.0.3"})}, "mocha-tests", "a mocha suite (mocha)"),
    "playwright": ({"package.json": _pkg("jest", {"jest": "30.5.2", "@playwright/test": "1.64.1"},
                                         **{"test:e2e": "playwright test"})}, "e2e", "a Playwright suite (npm run test:e2e)"),
    "go": ({"go.mod": GO_MOD, "calc.go": GO_CODE, "calc_test.go": GO_TEST}, "go-tests", "a Go module (example.com/calc)"),
    "rust": ({"Cargo.toml": CARGO_TOML, "src/lib.rs": LIB_RS, ".config/nextest.toml": NEXTEST}, "cargo-tests",
             "a Rust crate (calc)"),
}


@pytest.mark.parametrize("kind", list(PROJECTS))
def test_init_writes_a_junit_plan_for_each_language(tmp_path, kind):
    files, plan_name, what = PROJECTS[kind]
    root = _write(tmp_path / kind, files)
    r = cli(root, "init")
    assert r.returncode == 0, r.stdout + r.stderr
    assert f"  plan {plan_name}: {what}\n" in r.stdout, r.stdout
    cfg, text = C.load(root), (root / "testbench.yaml").read_text()
    junit = junit_of(cfg["plans"][plan_name])
    assert junit == {"path": "target/nextest/ci/junit.xml" if kind == "rust" else "junit.xml", "failures": 0,
                     "errors": 0, "tests": ">= 1"}
    assert re.search(r'tests: ">= 1"\}\s+# FILL IN: ">= N"', text) and "# FILL IN: your" in text
    recipe = {"jest": "jest", "vitest": "vitest", "go": "go-test", "rust": "cargo-test", "playwright": "playwright-ts"}
    if kind in recipe:                                  # init and the recipe run the runner the same way
        theirs = RC.load(recipe[kind])["plans"][LANGUAGES[recipe[kind]][0]]["steps"]
        mine = cfg["plans"][plan_name]["steps"]
        assert [(s["run"], s.get("env", {})) for s in mine] == [(s["run"], s.get("env", {})) for s in theirs]


def test_init_says_what_a_js_suite_still_needs_and_leaves_unknown_runners_to_npm_test(tmp_path):
    root = _write(tmp_path / "bare-jest", {"package.json": _pkg("jest --coverage", {"jest": "30.5.2"})})
    r = cli(root, "init")
    assert "plan jest-tests: a jest suite (jest --coverage); it needs jest-junit (npm install --save-dev " \
           "jest-junit@17.0.0)" in r.stdout
    assert "# needs jest-junit, the JUnit reporter: npm install --save-dev jest-junit@17.0.0" in \
        (root / "testbench.yaml").read_text()
    root = _write(tmp_path / "ava", {"package.json": _pkg("ava")})
    assert "plan js-tests: an npm test script (ava)" in cli(root, "init").stdout
    assert C.load(root)["plans"]["js-tests"]["steps"][0]["run"] == "npm test"
    for files in ({"package.json": _pkg('echo "Error: no test specified" && exit 1')},     # npm init's placeholder
                  {"go.mod": GO_MOD, "calc.go": GO_CODE}):                                 # a Go module with no tests
        root = _write(tmp_path / f"nothing-{len(files)}", files)
        assert scaffold.detect(root) == [], files
    root = _write(tmp_path / "bare-crate", {"Cargo.toml": CARGO_TOML, "src/lib.rs": LIB_RS})
    r = cli(root, "init")
    assert "plan cargo-tests: a Rust crate (calc); it needs cargo-nextest and a junit profile" in r.stdout
    assert '#   [profile.ci.junit]\n        #   path = "junit.xml"' in (root / "testbench.yaml").read_text()
    root = _write(tmp_path / "own-profile", {"Cargo.toml": '[workspace]\nmembers = ["a"]\n', ".config/nextest.toml":
                                             "[profile.default.junit]\npath = 'report.xml'\n"})
    found = scaffold.detect(root)
    assert found[0][2] == "a Rust workspace" and "cargo nextest run --profile default" in found[0][1]
    assert "path: target/nextest/default/report.xml" in found[0][1]


def test_init_keeps_python_detection_and_adds_the_other_languages_beside_it(tmp_path):
    root = _write(tmp_path / "poly", {"tests/test_math.py": "def test_add():\n    assert 1 + 1 == 2\n",
                                      "go.mod": GO_MOD, "calc.go": GO_CODE, "calc_test.go": GO_TEST,
                                      "package.json": _pkg("vitest", {"vitest": "5.0.5"}, dev="vite")})
    r = cli(root, "init")
    assert r.returncode == 0, r.stdout
    cfg = C.load(root)
    assert list(cfg["plans"]) == ["tests", "vitest-tests", "go-tests", "ui"]
    assert cfg["plans"]["tests"]["steps"][0]["run"] == "python -m pytest -q", "the pytest plan is as it was"
    assert "Next: fill in the lines marked FILL IN in testbench.yaml" in r.stdout and "`testbench run tests`" in r.stdout


def test_init_with_a_language_recipe_leaves_out_the_plan_it_covers(tmp_path):
    files, _, _ = PROJECTS["go"]
    root = _write(tmp_path / "gomod", files)
    r = cli(root, "init", "--recipe", "go-test")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "found a Go module (example.com/calc): the recipe's plans already cover it" in r.stdout
    assert list(C.load(root)["plans"]) == ["go-tests", "go-sandbox"]
    plans = yaml.safe_load("plans:\n" + scaffold._go_tests(root)[1])["plans"]
    assert RC.subjects(plans) == {("go-test", "")}
    assert RC.subjects({"p": {"steps": [{"run": "cargo nextest run"}, {"run": "npx --no -- vitest run"}]}}) == \
        {("cargo-test", ""), ("vitest", "")}


# ---- junit: real reports from each runner -----------------------------------------------------------------------

REPORTS = {     # file: tests, passed, failures, errors, skipped, and how the failing and erroring cases are named
    "jest-pass.xml": (3, 2, 0, 0, 1, [], []),
    "jest-fail.xml": (5, 1, 2, 1, 1, [
        "sum adds 1 + 2 to equal 3 (Error: expect(received).toBe(expected) // Object.is equality Expected: 4 Received: 3)",
        "Test suite failed to run > broken.test.js (Cannot find module './does-not-exist' from 'broken.test.js')"], [
        "Test suite failed to run > broken.test.js (Cannot find module './does-not-exist' from 'broken.test.js')"]),
    "vitest-pass.xml": (3, 2, 0, 0, 1, [], []),
    "vitest-fail.xml": (4, 1, 2, 0, 1, [
        "broken.test.js (Cannot find module './does-not-exist.js' imported from /tmp/tb-lang-tools/proj/vitest/broken.test.js)",
        "sum.test.js > sum > adds 1 + 2 to equal 3 (expected 3 to be 4 // Object.is equality)"], []),
    "mocha-xunit-pass.xml": (3, 2, 0, 0, 1, [], []),
    "mocha-xunit-fail.xml": (4, 1, 2, 0, 1, [                  # mocha's suite says errors="2" for its two failures
        'with a broken hook > "before all" hook for "never runs" (setup failed)',
        "sum > adds 1 + 2 to equal 3 (Expected values to be strictly equal: 3 !== 4)"], []),
    "node-test-pass.xml": (3, 2, 0, 0, 1, [], []),
    "node-test-fail.xml": (4, 1, 2, 0, 1, [
        "test/broken.test.js (test failed)", "sum > adds 1 + 2 to equal 3 (Expected values to be strictly equal:3 !== 4)"], []),
    "playwright-pass.xml": (3, 2, 0, 0, 1, [], []),
    "playwright-fail.xml": (4, 2, 1, 0, 1, [                   # its flaky test passed on a retry: it counts as passed
        'home.spec.ts > home › has title (expect(page).toHaveTitle(expected) failed Expected: "Other App" '
        'Received: "My App")'], []),
    "gotestsum-pass.xml": (5, 4, 0, 0, 1, [], []),
    "gotestsum-fail.xml": (8, 2, 5, 0, 1, [
        "example.com/calc.TestAdd (calc_test.go:7: 1 + 2 is not 3)",
        "example.com/calc.TestTable/neg (calc_test.go:19: Add(-1, 1) = 0, want 5)",
        "example.com/calc.TestTable (Failed)",                 # a parent test fails with its subtest
        "example.com/calc/broken > TestMain (FAIL example.com/calc/broken [build failed])",
        "example.com/calc/panics.TestPanics (panic: assignment to entry in nil map [recovered])"], []),
    "go-junit-report-pass.xml": (5, 4, 0, 0, 1, [], []),
    "go-junit-report-fail.xml": (8, 2, 4, 1, 1, [
        "example.com/calc.TestAdd (calc_test.go:7: 1 + 2 is not 3)", "example.com/calc.TestTable (Failed)",
        "example.com/calc.TestTable/neg (calc_test.go:19: Add(-1, 1) = 0, want 5)",
        "example.com/calc/panics.TestPanics (Failed)"], [
        "example.com/calc/broken.[build failed] (broken/broken.go:3:26: undefined: undefinedThing)"]),
    "nextest-pass.xml": (4, 4, 0, 0, 0, [], []),               # nextest leaves ignored tests out of its report
    "nextest-fail.xml": (5, 3, 2, 0, 0, [
        "calc.tests::adds (thread 'tests::adds' (29047) panicked at src/lib.rs:11:9: assertion `left == right` failed "
        "left: 3 right: 4)",
        "calc::panics.unwraps_none (thread 'unwraps_none' (29052) panicked at tests/panics.rs:4:7: called "
        "`Option::unwrap()` on a `None` value)"], []),
}


def test_every_report_in_the_data_folder_is_checked():
    assert sorted(p.name for p in DATA.glob("*.xml")) == sorted(REPORTS)


@pytest.mark.parametrize("name", list(REPORTS))
def test_junit_reads_each_runner_s_real_report(tmp_path, name):
    tests, passed, failures, errors, skipped, failing, erroring = REPORTS[name]
    got = expect.read_junit(DATA / name)
    assert (got["tests"], got["passed"], got["failures"], got["errors"], got["skipped"]) == \
        (tests, passed, failures, errors, skipped), got
    assert got["failing"] == failing and got["erroring"] == erroring
    shutil.copy(DATA / name, tmp_path / "junit.xml")            # and as a plan's check reads it, run_started now
    checks = {c["check"]: c for c in expect.check({"junit": {"path": "junit.xml", "tests": f">= {tests}"}},
                                                   workdir=tmp_path, result={"kind": None, "run_started": 0})}
    assert checks["junit.xml: failures 0"]["ok"] == (failures == 0) and checks["junit.xml: errors 0"]["ok"] == (errors == 0)
    if failing:
        assert f"failed: {failing[0]}" in checks["junit.xml: failures 0"]["detail"]


@pytest.mark.parametrize("command, how", [
    (None, "pytest: add --junitxml=junit.xml"),
    ("npx --no -- jest --ci", "jest: --reporters=default --reporters=jest-junit"),
    ("npx --no -- vitest run", "vitest: --reporter=default --reporter=junit --outputFile.junit=junit.xml"),
    ("go test ./...", "Go: `go run gotest.tools/gotestsum@v1.13.0 --junitfile junit.xml -- ./...`"),
    ("cargo nextest run --profile ci", "cargo-nextest writes it only for a profile with a junit path"),
    ("npx --no -- playwright test", "PLAYWRIGHT_JUNIT_OUTPUT_NAME=junit.xml"),
    ("node --test", "node --test: --test-reporter=junit --test-reporter-destination=junit.xml"),
    ("cargo test --workspace", "cargo test writes no JUnit: cargo-nextest does"),
    ("npm test", "npm test: run its test runner with a JUnit reporter instead"),
])
def test_a_missing_report_says_how_the_runner_writes_one(tmp_path, command, how):
    got = expect.check({"junit": "junit.xml"}, workdir=tmp_path, result={"kind": "run", "command": command,
                                                                         "exit_code": 0})
    detail = next(c["detail"] for c in got if c["check"] == "junit.xml: JUnit report")
    assert detail.startswith("file missing: the test command wrote no report there (") and how in detail, detail


# ---- the local plans, on tiny projects, where the toolchain is here -----------------------------------------------

def _ok(*cmd, cwd=None, timeout=300) -> bool:
    try:
        return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _npm_install(root: Path):
    if not shutil.which("npm"):
        pytest.skip("needs node and npm")
    if not _ok("npm", "install", "--no-audit", "--no-fund", "--prefer-offline", cwd=root, timeout=600):
        pytest.skip("npm could not install the packages (no network or registry here)")


def _gotestsum(root: Path):
    if not shutil.which("go"):
        pytest.skip("needs Go")
    if not _ok("go", "run", "gotest.tools/gotestsum@v1.13.0", "--version", cwd=root, timeout=600):
        pytest.skip("go could not fetch gotestsum (no network or module proxy here)")


def _nextest(root: Path):
    if not (shutil.which("cargo") and _ok("cargo", "nextest", "--version", cwd=root, timeout=120)):
        pytest.skip("needs cargo and cargo-nextest (cargo install cargo-nextest --locked)")


RUNS = {    # recipe: (project, how to get its tools, the plan, the edit that breaks a test, the failure it must name)
    "jest": ("jest", _npm_install, "jest-tests", ("sum.test.js", "toBe(3)", "toBe(4)"),
             "failed: sum adds (Error: expect(received).toBe(expected) // Object.is equality Expected: 4 Received: 3)"),
    "vitest": ("vitest", _npm_install, "vitest-tests", ("sum.test.js", "toBe(3)", "toBe(4)"),
               "failed: sum.test.js > sum > adds (expected 3 to be 4 // Object.is equality)"),
    "go-test": ("go", _gotestsum, "go-tests", ("calc_test.go", "2), 3;", "2), 4;"),
                "failed: example.com/calc.TestAdd (calc_test.go:7: Add(1, 2) = 3, want 4)"),
    "cargo-test": ("rust", _nextest, "cargo-tests", ("src/lib.rs", "add(1, 2), 3", "add(1, 2), 4"),
                   "failed: calc.tests::adds (thread 'tests::adds' "),
}


@pytest.mark.parametrize("name", list(RUNS))
def test_the_local_plan_passes_then_names_the_test_that_broke(tmp_path, name):
    kind, tools, plan, (rel, old, new), named = RUNS[name]
    root = _write(tmp_path / kind, PROJECTS[kind][0])
    tools(root)
    r = cli(root, "init", "--recipe", name)
    assert r.returncode == 0, r.stdout + r.stderr
    r = cli(root, "run", plan, "--wait", "900", timeout=1200)
    assert r.returncode == 0 and f"# PASS - plan `{plan}` on local" in r.stdout, f"{name}:\n{r.stdout}{r.stderr}"
    assert re.search(r"junit\.xml: tests >= 1 - 2 \(wanted >= 1\.0\)", r.stdout), r.stdout
    (root / rel).write_text((root / rel).read_text().replace(old, new))
    r = cli(root, "run", plan, "--wait", "900", timeout=1200)
    assert r.returncode == 1 and f"# FAIL - plan `{plan}` on local" in r.stdout, r.stdout
    assert "failures 0: 1 (wanted == 0) of 2 tests; " + named in r.stdout, r.stdout


def test_node_s_own_test_runner_through_init(tmp_path):
    if not shutil.which("node"):
        pytest.skip("needs node")
    root = _write(tmp_path / "node", PROJECTS["node"][0])
    assert cli(root, "init").returncode == 0
    r = cli(root, "run", "node-tests", "--wait", "300")
    if "--test-reporter" in r.stdout and "bad option" in r.stdout:
        pytest.skip("this node has no --test-reporter (node 20 or newer has)")
    assert r.returncode == 0 and "# PASS - plan `node-tests` on local" in r.stdout, r.stdout
    (root / "sum.js").write_text(SUM_CJS.replace("a + b", "a * b"))
    r = cli(root, "run", "node-tests", "--wait", "300")
    assert r.returncode == 1 and "failures 0: 2 (wanted == 0) of 2 tests; failed: " in r.stdout, r.stdout
    assert re.search(r"failed: (sum > )?adds", r.stdout), r.stdout    # node 22 names the suite too; others do not


# ---- the docker plans: the image the recipe describes, built and run offline (opt in) -----------------------------

DOCKER_PROJECTS = {"jest": "jest", "vitest": "vitest", "go-test": "go", "cargo-test": "rust"}
DEPENDENCIES = {      # a module and a crate from outside, which the image must hold for the offline run to build
    "go": {"go.mod": GO_MOD + "\nrequire github.com/google/go-cmp v0.6.0\n",
           "go.sum": "github.com/google/go-cmp v0.6.0 h1:ofyhxvXcZhMsU5ulbFiLKl/XBFqE1GSq7atu8tAmTRI=\n"
                     "github.com/google/go-cmp v0.6.0/go.mod h1:17dUlkBOakJ0+DkrSSNjCkIjxS6bF9zb3elmeNGIjoY=\n",
           "same_test.go": 'package calc\n\nimport (\n\t"testing"\n\n\t"github.com/google/go-cmp/cmp"\n)\n\n'
                           'func TestSame(t *testing.T) {\n\tif d := cmp.Diff([]int{Add(1, 1)}, []int{2}); d != "" {\n'
                           '\t\tt.Fatal(d)\n\t}\n}\n'},
    "rust": {"Cargo.toml": CARGO_TOML + '\n[dev-dependencies]\nitoa = "1"\n',      # and no Cargo.lock
             "tests/formats.rs": "#[test]\nfn formats() {\n    assert_eq!(itoa::Buffer::new().format(calc::add(40, 2)), "
                                 '"42");\n}\n'},
}


def _for_this_machine(lines: list[str], ca: str | None) -> list[str]:
    """The recipe's Dockerfile as this machine can build it: base images from a registry mirror (Docker Hub limits
    pulls), and a TLS-intercepting proxy's CA bundle trusted after the last FROM."""
    mirror = os.environ.get("TESTBENCH_TEST_DOCKER_MIRROR", "mirror.gcr.io/library/")
    out = [re.sub(r"^FROM (\S+)", lambda m: f"FROM {mirror}{m.group(1)}", ln) for ln in lines]
    if ca:
        last = max(i for i, ln in enumerate(out) if ln.startswith("FROM "))
        bundle = "/etc/ssl/certs/ca-certificates.crt"
        out[last + 1:last + 1] = ["COPY testbench-ca.crt /usr/local/share/ca-certificates/testbench-ca.crt",
                                  "RUN update-ca-certificates",
                                  f"ENV SSL_CERT_FILE={bundle} PIP_CERT={bundle} NODE_EXTRA_CA_CERTS={bundle} "
                                  f"CARGO_HTTP_CAINFO={bundle} CARGO_BUILD_JOBS=2"]
    return out


@pytest.mark.posix
@pytest.mark.serial                                      # Docker
@pytest.mark.parametrize("name", list(DOCKER_PROJECTS))
def test_the_sandbox_plan_runs_offline_in_the_image_its_recipe_describes(tmp_path, name):
    import test_docker as TD
    if os.environ.get("TESTBENCH_TEST_DOCKER_LANGS") != "1":
        pytest.skip("set TESTBENCH_TEST_DOCKER_LANGS=1 to build each language's image (minutes each) and run it")
    if not TD._docker_answers():
        pytest.skip("needs a Docker daemon that answers `docker info`")
    kind, recipe = DOCKER_PROJECTS[name], RC.load(name)
    root = _write(tmp_path / kind, {**PROJECTS[kind][0], **DEPENDENCIES.get(kind, {})})
    if kind in ("jest", "vitest"):
        _npm_install(root)                                   # its package-lock.json; node_modules stays out of copies
        shutil.rmtree(root / "node_modules")
    ca = os.environ.get("PIP_CERT") if os.environ.get("PIP_CERT") and Path(os.environ["PIP_CERT"]).is_file() else None
    text = "\n".join(_for_this_machine(dockerfile(recipe), ca)) + "\n"
    (root / "Dockerfile.testbench").write_text(text)
    (root / ".dockerignore").write_text("target\nnode_modules\n.testbench\n")
    if ca:
        shutil.copyfile(ca, root / "testbench-ca.crt")
    tag = f"testbench-test-{name}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    build = ["docker", "build", "-f", "Dockerfile.testbench", "-t", tag]
    if proxy and urlparse(proxy).hostname in ("127.0.0.1", "localhost", "::1"):
        build += ["--network", "host"]
    build += [x for n in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy") if os.environ.get(n)
              for x in ("--build-arg", n)]
    p = subprocess.run([*build, "."], cwd=root, capture_output=True, text=True, timeout=3600)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    assert cli(root, "init", "--recipe", name).returncode == 0
    yml = root / "testbench.yaml"
    yml.write_text(yml.read_text().replace(f"  image: {recipe['data']['docker']['image']} ", f"  image: {tag} ", 1)
                   + ("exclude: [.git, target]\n" if kind == "rust" else ""))
    plan = LANGUAGES[name][1]
    r = cli(root, "run", plan, "--wait", "1800", timeout=2400)
    assert r.returncode == 0 and f"# PASS - plan `{plan}` on docker" in r.stdout, f"{name}:\n{r.stdout}{r.stderr}"
    want = 3 if kind in DEPENDENCIES else 2
    assert re.search(rf"junit\.xml: tests >= 1 - {want} \(wanted >= 1\.0\)", r.stdout), r.stdout
    if kind in ("jest", "vitest"):                           # a lockfile the image was not built from: said so
        (root / "package-lock.json").write_text((root / "package-lock.json").read_text().replace('"name": "sum"',
                                                                                                  '"name": "sum2"', 1))
        r = cli(root, "run", plan, "--wait", "600", timeout=900)
        assert r.returncode == 1 and "package-lock.json changed since the image was built: rebuild it" in r.stdout, \
            r.stdout
