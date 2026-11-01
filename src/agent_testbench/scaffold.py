"""testbench init: look at a project and write plans that already check something.

It finds notebooks, .ipynb or percent-format .py (a lint step, then the notebook with its parameters - Colab form
fields, papermill parameters - listed and the files it writes expected),
a pytest suite (every test must pass), a web app (a static index.html, or an npm dev/start script: opened in
headless Chromium, checked for console errors, screenshotted) and training scripts (train.py and the like: a
short run to fill in). What it could not know - the numbers that mean "working" - is left as a marked line to
fill in, and init says so.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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
    fields = {f["name"]: f["value"] for f in found}
    forms, papermill = (sum(f["kind"] == k for f in found) for k in ("form", "parameter"))
    made = sorted({m for src in cells for m in WRITTEN.findall(src) if not m.startswith(("http", "/"))})
    name = f"{_slug(nb.stem)}-local"
    lines = [f"  {name}:", "    backend: local", f"    description: lint {rel}, then run it top to bottom on this machine",
             "    steps:", "      - name: lint", f"        run: testbench lint {_q(rel)}", "        max_minutes: 1",
             f"      - name: {_slug(nb.stem)}", f"        notebook: {_q(rel)}", "        max_minutes: 20"]
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


def _q(text: str) -> str:
    return json.dumps(text) if re.search(r"[\s:#{}\[\],*&!|>'\"%@`]", text) else text


def _pytest(root: Path) -> tuple[str, str, str] | None:
    tests = _walk(root, "test_*.py", 1) or _walk(root, "*_test.py", 1)
    configured = any((root / f).is_file() and "pytest" in (root / f).read_text(errors="replace")
                     for f in ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini"))
    if not tests and not configured:
        return None
    text = ("  tests:\n    backend: local\n    description: the test suite; every test must pass\n    steps:\n"
            "      - name: pytest\n        run: python -m pytest -q\n        max_minutes: 15\n"
            "        expect:\n          exit_code: 0\n")
    return "tests", text, "a pytest suite" + (f" ({tests[0].relative_to(root).as_posix()}, ...)" if tests else "")


def _web(root: Path) -> tuple[str, str, str] | None:
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
        except ValueError:
            scripts = {}
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
                    "          url: http://127.0.0.1:8971/\n          actions:\n            - screenshot: home\n"
                    "        max_minutes: 2\n        expect:\n          web:\n            status: 200\n"
                    "            no_console_errors: true\n"
                    "            # text: [\"Welcome\"]          # FILL IN: what the page should say\n")
            return "ui", text, f"a static page ({(Path(folder) / 'index.html').as_posix()})"
    return None


def _trainer(root: Path) -> tuple[str, str, str] | None:
    for name in TRAINERS:
        hits = [p for p in (root / name, root / "src" / name, root / "scripts" / name) if p.is_file()]
        if hits:
            rel = hits[0].relative_to(root).as_posix()
            text = ("  train-smoke:\n    backend: local\n    description: a short training run, to check it works end to end\n"
                    "    steps:\n      - name: train briefly\n"
                    f"        run: python {_q(rel)}            # FILL IN: flags for a tiny run (few steps, small data)\n"
                    "        max_minutes: 10\n        expect:\n          exit_code: 0\n"
                    "          # json: {outputs/metrics.json: {loss: \"< 1.0\"}}   # FILL IN: what it writes, and what is good\n")
            return "train-smoke", text, f"a training script ({rel})"
    return None


def detect(root: Path) -> list[tuple[str, str, str]]:
    """(plan name, plan YAML, what was found) for everything init recognised, in a sensible order."""
    root = Path(root)
    notebooks = _walk(root, "*.ipynb", 5)
    found = [_notebook(root, nb) for nb in notebooks + _percent_notebooks(root, 5 - len(notebooks))]
    for probe in (_pytest, _trainer, _web):
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
