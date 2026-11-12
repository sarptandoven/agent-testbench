"""testbench init: look at a project and write plans that already check something.

It finds notebooks, .ipynb or percent-format .py (a lint step, then the notebook with its parameters - Colab form
fields, papermill parameters - listed and the files it writes expected),
a pytest suite (every test must pass), test suites in other languages, judged by their own JUnit report (`npm test`
with jest, vitest, mocha, node --test or Playwright; a Go module through gotestsum; a Rust crate through
cargo-nextest), a web app (a static index.html, or an npm dev/start script: opened in headless Chromium, checked
for console errors, screenshotted) and training scripts (train.py and the like: a short run to fill in). What it could not know - the numbers that mean "working" - is left as a marked line to
fill in, and init says so.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

SKIP = {".git", ".testbench", ".venv", "venv", "env", "node_modules", ".ipynb_checkpoints", "__pycache__",
        "site-packages", ".tox", "build", "dist", ".mypy_cache", ".pytest_cache", ".claude"}
WRITTEN = re.compile(r"""["']([\w./-]+\.(?:json|png|jpg|jpeg|csv|pt|pth|pkl|npy|mp4|gif|html))["']""")
TRAINERS = ("train.py", "training.py", "finetune.py", "fine_tune.py", "main_train.py")


def _walk(root: Path, pattern: str, limit: int) -> list[Path]:
    out = []
    for p in sorted(root.rglob(pattern)):
        if not SKIP.intersection(p.relative_to(root).parts[:-1]) and p.is_file():
            out.append(p)
            if len(out) == limit:
                break
    return out


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "plan"


def _notebook(root: Path, nb: Path) -> tuple[str, str, str]:
    from . import notebook as NB
    rel = nb.relative_to(root).as_posix()
    try:
        parsed = NB.read(nb)
        cells, found = [c.source for c in parsed.cells if c.cell_type == "code"], NB.fields(parsed)
    except Exception:
        cells, found = [], []
    cells = [src for src in cells if isinstance(src, str)]           # a hand-made notebook may hold anything
    fields = {f["name"]: f["value"] for f in found}
    forms, papermill = (sum(f["kind"] == k for f in found) for k in ("form", "parameter"))
    made = sorted({m for src in cells for m in WRITTEN.findall(src) if not m.startswith(("http", "/"))})
    name = f"{_slug(nb.stem)}-local"
    step = _slug(nb.stem) if _slug(nb.stem) != "lint" else "notebook"       # lint.ipynb: two steps named lint
    lines = [f"  {name}:", "    backend: local",
             f"    description: {_plain(f'lint {rel}, then run it top to bottom on this machine')}",
             "    steps:", "      - name: lint", f"        run: {_plain(f'testbench lint {_q(rel)}')}", "        max_minutes: 1",
             f"      - name: {_plain(step)}", f"        notebook: {_q(rel)}", "        max_minutes: 20"]
    if fields:
        shown = ", ".join(f"{k}: {v}" for k, v in list(fields.items())[:6])
        what = "its form fields" if not papermill else "its papermill parameters" if not forms else "its parameters"
        lines.append(f"        # params: {{{shown}}}      # {what}; set smaller values for a quick check")
    if made:
        lines.append("        artifacts: [" + ", ".join(_q(f"**/{Path(m).name}") for m in made[:6]) + "]")
    lines.append("        expect:")
    lines.append("          no_errors: true                # every cell runs")
    if made:
        lines.append("          files: [" + ", ".join(_q(f"**/{Path(m).name}") for m in made[:6]) + "]   # what it writes")
    metrics = [m for m in made if m.endswith(".json")]
    where = _q(f"**/{Path(metrics[0]).name}") if metrics else "outputs/metrics.json"
    lines.append(f"          # json: {{{where}: {{accuracy: \">= 0.9\"}}}}   # FILL IN: the number that means it works")
    bits = ([f"{forms} form field(s)"] if forms else []) + ([f"{papermill} papermill parameter(s)"] if papermill else []) \
        + ([f"writes {', '.join(Path(m).name for m in made[:3])}"] if made else [])
    return name, "\n".join(lines) + "\n", f"notebook {rel}" + (f" ({', '.join(bits)})" if bits else "")


def _percent_notebooks(root: Path, limit: int) -> list[Path]:
    """Percent-format .py notebooks (`# %%` cells), except those paired with an .ipynb of the same name."""
    from .notebook import plain_python
    out = []
    for p in _walk(root, "*.py", 2000) if limit > 0 else []:
        if p.with_suffix(".ipynb").exists() or p.stat().st_size > 2_000_000 or plain_python(p):
            continue
        out.append(p)
        if len(out) == limit:
            break
    return out


def _q(text: str, always: bool = False) -> str:
    """text as a YAML value, in JSON's double quotes (which YAML reads) when it has to be quoted."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:                           # a file name that is not UTF-8: YAML reads \udcXX back
        return json.dumps(text)
    return json.dumps(text, ensure_ascii=False) if always or re.search(r"[\s:#{}\[\],*&!|>'\"%@`]", text) else text


def _plain(text: str) -> str:
    """text as a YAML value: as it is when YAML reads it back the same (a sentence, a command), else quoted."""
    try:
        if yaml.safe_load(f"k: {text}") == {"k": text}:
            return text
    except yaml.YAMLError:
        pass
    return _q(text, always=True)


def _text(f: Path) -> str:
    try:
        return f.read_text(errors="replace")
    except OSError:                                      # unreadable: as if it said nothing
        return ""


def _pytest(root: Path) -> tuple[str, str, str] | None:
    tests = _walk(root, "test_*.py", 1) or _walk(root, "*_test.py", 1)
    configured = any((root / f).is_file() and "pytest" in _text(root / f)
                     for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"))
    if not tests and not configured:
        return None
    text = ("  tests:\n    backend: local\n    description: the test suite; every test must pass\n    steps:\n"
            "      - name: pytest\n        run: python -m pytest -q\n        max_minutes: 15\n"
            "        expect:\n          exit_code: 0\n")
    return "tests", text, "a pytest suite" + (f" ({tests[0].relative_to(root).as_posix()}, ...)" if tests else "")


def _package(root: Path) -> dict:
    """package.json, or {} when there is none or it is not a JSON object (it may be anything)."""
    pkg = root / "package.json"
    try:
        data = json.loads(pkg.read_text()) if pkg.is_file() else {}
    except (OSError, ValueError, RecursionError):
        data = {}
    return data if isinstance(data, dict) else {}


def _scripts(data: dict) -> dict:
    scripts = data.get("scripts")
    return {k: v for k, v in scripts.items() if isinstance(v, str)} if isinstance(scripts, dict) else {}


# A JS test runner, found in what `npm test` runs or else among the packages: its plan's name, the runner's name, the
# command that writes JUnit to junit.xml, the step's env, its artifacts and the JUnit reporter it needs installed.
JS_TESTS = {
    "vitest": ("vitest-tests", "vitest", "npx --no -- vitest run --reporter=default --reporter=junit "
                                         "--outputFile.junit=junit.xml", "", "[junit.xml]", ""),
    "jest": ("jest-tests", "jest", "npx --no -- jest --ci --reporters=default --reporters=jest-junit",
             '{JEST_JUNIT_OUTPUT_FILE: junit.xml, JEST_JUNIT_REPORT_TEST_SUITE_ERRORS: "true"}', "[junit.xml]",
             "jest-junit@17.0.0"),
    "mocha": ("mocha-tests", "mocha", "npx --no -- mocha --reporter xunit --reporter-option output=junit.xml", "",
              "[junit.xml]", ""),
    "node": ("node-tests", "node --test", "node --test --test-reporter=spec --test-reporter-destination=stdout "
                                         "--test-reporter=junit --test-reporter-destination=junit.xml", "",
             "[junit.xml]", ""),
    "playwright": ("e2e", "Playwright", "npx --no -- playwright test --reporter=list,junit",
                   "{PLAYWRIGHT_JUNIT_OUTPUT_NAME: junit.xml}", '[junit.xml, "test-results/**"]', ""),
}
JS_SCRIPT = (("vitest", r"\bvitest\b"), ("jest", r"\bjest\b"), ("mocha", r"\bmocha\b"),
             ("node", r"\bnode\b[^&|;]*\s--test\b"), ("playwright", r"\bplaywright\s+test\b"))
JS_PACKAGES = (("vitest", "vitest"), ("jest", "jest"), ("mocha", "mocha"), ("playwright", "@playwright/test"))
JUNIT_LINE = '          junit: {path: %s, failures: 0, errors: 0, tests: ">= 1"}    # FILL IN: ">= N", the tests you have now\n'


def _js_kind(data: dict) -> tuple[str | None, str, set]:
    """The runner `npm test` runs (None: it runs one init does not know), the test script, and the packages."""
    test = _scripts(data).get("test", "").strip()
    if "no test specified" in test:                              # npm init's placeholder
        test = ""
    deps = {name for key in ("dependencies", "devDependencies") if isinstance(data.get(key), dict) for name in data[key]}
    kind = next((k for k, pattern in JS_SCRIPT if re.search(pattern, test)), None) \
        or next((k for k, package in JS_PACKAGES if package in deps), None)
    return kind, test, deps


def _js_tests(root: Path) -> tuple[str, str, str] | None:
    """`npm test` with a runner that writes JUnit (jest, vitest, mocha, node --test, Playwright), judged by its report."""
    kind, test, deps = _js_kind(_package(root))
    if not test:
        return None
    shown = " ".join(test.split())[:80]                           # in a comment, and in what init prints
    if kind is None:
        text = ("  js-tests:\n    backend: local\n    description: the project's npm test passes\n    steps:\n"
                f"      - name: npm test\n        run: npm test                     # it runs: {shown}\n"
                "        max_minutes: 15\n        expect:\n          exit_code: 0\n"
                "          # junit: {path: junit.xml, tests: \">= 1\"}   # FILL IN: have the runner write JUnit XML there\n")
        return "js-tests", text, f"an npm test script ({shown})"
    return _js_plan(kind, shown, deps)


def _playwright_tests(root: Path) -> tuple[str, str, str] | None:
    """Playwright tests next to another runner's unit tests (`npm test` runs jest, @playwright/test is installed)."""
    kind, test, deps = _js_kind(_package(root))
    if "@playwright/test" not in deps or kind == "playwright" and test:
        return None
    script = next((f"npm run {s}" for s, cmd in _scripts(_package(root)).items() if re.search(JS_SCRIPT[-1][1], cmd)),
                  "playwright test")
    return _js_plan("playwright", script, deps)


def _js_plan(kind: str, shown: str, deps: set) -> tuple[str, str, str]:
    name, runner, run, env, artifacts, needs = JS_TESTS[kind]
    reporter = needs.rsplit("@", 1)[0]
    missing = bool(needs) and reporter not in deps
    lines = [f"  {name}:", "    backend: local",
             f"    description: every test passes, counted from {runner}'s JUnit report", "    steps:",
             f"      - name: {_plain(runner):<29}# FILL IN: your arguments; the project runs: {shown}",
             f"        run: {run}"]
    lines += [f"        env: {env}"] if env else []
    lines += [f"        # needs {reporter}, the JUnit reporter: npm install --save-dev {needs}"] if missing else []
    lines += ["        max_minutes: 15", f"        artifacts: {artifacts}", "        expect:", "          exit_code: 0"]
    what = f"a {runner} suite ({shown})" + (f"; it needs {reporter} (npm install --save-dev {needs})" if missing else "")
    return name, "\n".join(lines) + "\n" + JUNIT_LINE % "junit.xml", what


def _go_tests(root: Path) -> tuple[str, str, str] | None:
    """A Go module with tests: gotestsum (by `go run`, pinned) runs go test and writes its JUnit report."""
    mod = root / "go.mod"
    if not mod.is_file() or not _walk(root, "*_test.go", 1):
        return None
    module = next(iter(re.findall(r"(?m)^module\s+(\S+)", _text(mod))), "")
    text = ("  go-tests:\n    backend: local\n    description: every package builds and every test passes, counted "
            "from gotestsum's JUnit report\n    steps:\n"
            "      - name: go test                      # FILL IN: your packages and flags, after the --\n"
            "        run: go run gotest.tools/gotestsum@v1.13.0 --junitfile junit.xml -- -count=1 ./...\n"
            "        max_minutes: 15\n        artifacts: [junit.xml]\n        expect:\n          exit_code: 0\n"
            + JUNIT_LINE % "junit.xml")
    return "go-tests", text, f"a Go module ({module})" if module else "a Go module"


def _cargo_tests(root: Path) -> tuple[str, str, str] | None:
    """A Rust crate or workspace: cargo-nextest runs the tests and writes JUnit for a profile with a junit path."""
    manifest = root / "Cargo.toml"
    if not manifest.is_file():
        return None
    cargo = _text(manifest)
    found = re.findall(r"""(?ms)^\[profile\.([\w-]+)\.junit\][^\[]*?^\s*path\s*=\s*["']([^"']+)["']""",
                       _text(root / ".config" / "nextest.toml"))
    profile, path = found[0] if found else ("ci", "junit.xml")
    report = _q(f"target/nextest/{profile}/{path}")
    setup = "" if found else (
        "        # needs cargo-nextest (cargo install cargo-nextest --locked) and these lines in .config/nextest.toml:\n"
        "        #   [profile.ci]\n        #   fail-fast = false\n        #   [profile.ci.junit]\n"
        "        #   path = \"junit.xml\"\n")
    text = ("  cargo-tests:\n    backend: local\n    description: every test passes, counted from cargo-nextest's JUnit "
            "report\n    steps:\n      - name: nextest\n"
            f"        run: cargo nextest run --profile {profile}       # FILL IN: your arguments (--workspace)\n"
            f"{setup}        max_minutes: 20\n        artifacts: [{report}]\n        expect:\n          exit_code: 0\n"
            + JUNIT_LINE % report)
    crate = next(iter(re.findall(r'(?ms)^\[package\][^\[]*?^name\s*=\s*"([^"]+)"', cargo)), "")
    what = "a Rust workspace" if re.search(r"(?m)^\[workspace\]", cargo) and not crate else \
        f"a Rust crate ({crate})" if crate else "a Rust crate"
    return "cargo-tests", text, what + ("" if not setup else "; it needs cargo-nextest and a junit profile in "
                                                             ".config/nextest.toml, as the plan says")


def _web(root: Path) -> tuple[str, str, str] | None:
    if (root / "package.json").is_file():
        scripts = _scripts(_package(root))
        script = next((s for s in ("dev", "start", "preview") if s in scripts), None)
        if script:
            text = ("  ui:\n    backend: local\n    description: open the app in a headless browser and check the page\n"
                    "    steps:\n      - name: page loads\n        web:\n"
                    f"          serve: npm run {script}                    # FILL IN: the port it serves on, below\n"
                    "          url: http://127.0.0.1:5173/\n          ready_timeout: 60\n"
                    "          actions:\n            - screenshot: home\n        max_minutes: 3\n"
                    "        expect:\n          web:\n            status: 200\n            no_console_errors: true\n"
                    "            # text: [\"Welcome\"]          # FILL IN: what the page should say\n")
            return "ui", text, f"a web app (npm run {script})"
    for folder in ("", "site", "public", "web", "static", "www"):
        if (root / folder / "index.html").is_file():
            d = folder or "."
            text = ("  ui:\n    backend: local\n    description: open the page in a headless browser and check it\n"
                    "    steps:\n      - name: page loads\n        web:\n"
                    f"          serve: python -m http.server 8971 --bind 127.0.0.1 -d {d}\n"
                    "          ready_timeout: 90             # its start can take ~30 s on macOS (host name lookup)\n"
                    "          url: http://127.0.0.1:8971/\n          actions:\n            - screenshot: home\n"
                    "        max_minutes: 2\n        expect:\n          web:\n            status: 200\n"
                    "            no_console_errors: true\n"
                    "            # text: [\"Welcome\"]          # FILL IN: what the page should say\n")
            return "ui", text, f"a static page ({(Path(folder) / 'index.html').as_posix()})"
    return None


def trainer_script(root: Path) -> str | None:
    """The project's training script (train.py and the like, at the top, in src/ or in scripts/), relative to root."""
    for name in TRAINERS:
        hits = [p for p in (root / name, root / "src" / name, root / "scripts" / name) if p.is_file()]
        if hits:
            return hits[0].relative_to(root).as_posix()
    return None


def _trainer(root: Path) -> tuple[str, str, str] | None:
    rel = trainer_script(root)
    if rel is None:
        return None
    text = ("  train-smoke:\n    backend: local\n    description: a short training run, to check it works end to end\n"
            "    steps:\n      - name: train briefly\n"
            f"        run: python {_q(rel)}            # FILL IN: flags for a tiny run (few steps, small data)\n"
            "        max_minutes: 10\n        expect:\n          exit_code: 0\n"
            "          # json: {outputs/metrics.json: {loss: \"< 1.0\"}}   # FILL IN: what it writes, and what is good\n")
    return "train-smoke", text, f"a training script ({rel})"


def detect(root: Path) -> list[tuple[str, str, str]]:
    """(plan name, plan YAML, what was found) for everything init recognised, in a sensible order."""
    root = Path(root)
    notebooks = _walk(root, "*.ipynb", 5)
    found = [_notebook(root, nb) for nb in notebooks + _percent_notebooks(root, 5 - len(notebooks))]
    for probe in (_pytest, _js_tests, _playwright_tests, _go_tests, _cargo_tests, _trainer, _web):
        hit = probe(root)
        if hit:
            found.append(hit)
    seen, out = set(), []
    for name, text, what in found:
        unique, n = name, 2
        while unique in seen:
            unique, n = f"{name}-{n}", n + 1
        seen.add(unique)
        out.append((unique, text.replace(f"  {name}:", f"  {unique}:", 1), what))
    return out
