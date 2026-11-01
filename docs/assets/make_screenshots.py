#!/usr/bin/env python3
"""Make the screenshots in docs/assets/ again, from real runs in a new folder.

    PYTHONPATH=src python docs/assets/make_screenshots.py          # in a checkout: writes docs/assets/*.png
    python docs/assets/make_screenshots.py --keep /tmp/shots       # keep the projects and transcript.txt there
    python docs/assets/make_screenshots.py --out /tmp/shots/png    # write the images somewhere else

What it runs, with the Python that runs this script (so the agent_testbench it imports is the one shown), each
command through `testbench` as a person would type it:

1. `testbench demo`: the tiny-model example, a git repository whose `smoke` plan passes;
2. a planted bug - `LEARNING_RATE = 1.0` in cells/02_settings.py, then `testbench nb build cells tiny_model.ipynb`
   and `testbench run smoke`, which fails (accuracy 0.17), and `testbench report --format html`;
3. the fix, the same run again, which passes, `testbench verify` and `testbench report --format html`;
4. a short loop (`testbench loop start`, two `loop try`, `loop stop`) so the checked number moves, then
   `testbench ui`;
5. a copy of examples/web-ui (on a free port): `testbench run ui-check` passes, then fails once site/app.js calls
   a function that does not exist.

Then it opens the pages in headless Chromium (Playwright; 1280x800 viewport, device scale 1, light colour scheme)
and saves, each a PNG under 150 KB (Pillow quantises one that is larger):

- report-fail.png    the HTML report of the failing run, first screen
- report-pass.png    the HTML report of the run after the fix, down to its plot
- terminal-fail.png  the real terminal output of the failing `testbench run smoke`, as a 100-column terminal shows it
- ui-index.png       `testbench ui`: plans with trend lines, runs, the loop
- ui-run.png         a run's page opened from the index: its evidence files and provenance
- web-report.png     the failing web run's report: its checks and the screenshot of the page

Nothing in a page is edited; a screenshot only chooses which part of the page to show (the main column, from one
heading to another). Every command and its output is written to transcript.txt in the work folder, so a page
that shows these images (docs/tour.md) can quote the same runs; keep it with --keep.

Needs: Linux or macOS, git, agent-testbench with scikit-learn and matplotlib (what `testbench demo` needs),
Playwright for Python with a Chromium it can start (`playwright install chromium`; if the browser that is
installed is older than your Playwright wants, use the Playwright version that matches it), and Pillow.
It costs nothing - every plan runs on this machine - and takes a minute or two. It is not run in CI: every run
gives new run ids, times and hashes. Importing this file runs nothing.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent                       # docs/assets
REPO = HERE.parents[1]
VIEWPORT = {"width": 1280, "height": 800}
COLUMN = {"x": 140, "width": 1000}       # a report page's main column (980 px, centred) and a little of its margin
LIMIT = 150_000                          # bytes per image
RUN_ID = re.compile(r"^Run `([^`]+)`", re.MULTILINE)


class Bench:
    """Runs `testbench ...` in a folder, the way a person would type it, and keeps a transcript."""

    def __init__(self, work: Path):
        import agent_testbench
        self.work, self.log = work, []
        bin_dir = work / "bin"
        bin_dir.mkdir()
        self.shim = bin_dir / "testbench"                    # the same Python and package for every step's commands
        self.shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m agent_testbench "$@"\n')
        self.shim.chmod(0o755)
        package_root = str(Path(agent_testbench.__file__).resolve().parents[1])
        path = [package_root] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PYTHONPATH": os.pathsep.join(str(Path(p).resolve()) for p in path),
                    "PYTHONUNBUFFERED": "1"}                 # output in the order a terminal shows it

    def __call__(self, cwd: Path, *args: str, exit_code: int = 0) -> str:
        shown = shlex.join(["testbench", *args])
        p = subprocess.run([str(self.shim), *args], cwd=cwd, env=self.env, text=True, check=False,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.log.append(f"### in {cwd.relative_to(self.work)}, exit {p.returncode}\n$ {shown}\n{p.stdout}")
        if p.returncode != exit_code:
            raise SystemExit(f"`{shown}` in {cwd} exited {p.returncode}, expected {exit_code}:\n{p.stdout[-4000:]}")
        return p.stdout


def edit(path: Path, old: str, new: str, every: bool = False) -> None:
    text = path.read_text()
    if old not in text:
        raise SystemExit(f"{path} has no {old!r}: the example changed; update make_screenshots.py to match it")
    path.write_text(text.replace(old, new, -1 if every else 1))


def run_id(output: str) -> str:
    m = RUN_ID.search(output)
    if not m:
        raise SystemExit(f"no run id in this output:\n{output[-2000:]}")
    return m.group(1)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_runs(tb: Bench) -> dict:
    """The runs the screenshots show; returns what the pictures need (run ids, the failing run's output)."""
    work = tb.work
    tb(work, "--version")
    tb(work, "demo")
    demo = work / "testbench-demo"
    settings = demo / "cells" / "02_settings.py"

    edit(settings, "LEARNING_RATE = 0.01 ", "LEARNING_RATE = 1.0 ")          # the planted bug: nothing crashes
    tb(demo, "nb", "build", "cells", "tiny_model.ipynb")
    failed = tb(demo, "run", "smoke", exit_code=1)
    tb(demo, "report", "--format", "html")                                     # the latest run: the failed one
    edit(settings, "LEARNING_RATE = 1.0 ", "LEARNING_RATE = 0.01 ")          # the fix
    tb(demo, "nb", "build", "cells", "tiny_model.ipynb")
    passed = tb(demo, "run", "smoke")
    tb(demo, "verify")
    tb(demo, "report", "--format", "html")

    tb(demo, "loop", "start", "smoke", "--metric", "outputs/metrics.json:accuracy", "--budget-usd", "0")
    edit(settings, "HIDDEN = 64 ", "HIDDEN = 16 ")
    tb(demo, "nb", "build", "cells", "tiny_model.ipynb")
    tb(demo, "loop", "try", "hidden units 64 -> 16", exit_code=1)              # not better: reverted
    edit(settings, "HIDDEN = 64 ", "HIDDEN = 128 ")
    edit(settings, "LEARNING_RATE = 0.01 ", "LEARNING_RATE = 0.02 ")
    tb(demo, "nb", "build", "cells", "tiny_model.ipynb")
    kept = tb(demo, "loop", "try", "hidden units 64 -> 128, learning rate 0.01 -> 0.02")
    tb(demo, "loop", "stop")
    tb(demo, "history", "smoke")
    tb(demo, "ui")
    best = re.search(r"Run (\S+) passed", kept)

    web = work / "web-ui"
    shutil.copytree(REPO / "examples" / "web-ui", web)
    edit(web / "testbench.yaml", "8971", str(free_port()), every=True)        # its serve: and url:
    tb(web, "run", "ui-check")
    edit(web / "site" / "app.js", "count += 1;", "count += 1; updateTotal();")  # a function that does not exist
    web_failed = tb(web, "run", "ui-check", exit_code=1)
    tb(web, "report", "--format", "html")
    return {"demo": demo, "web": web, "failed": run_id(failed), "passed": run_id(passed),
            "best": best.group(1) if best else run_id(passed), "failed_output": failed,
            "web_failed": run_id(web_failed)}


TERMINAL = """<!doctype html><html><head><meta charset=utf-8><style>
body{{margin:0;background:#fbfbfa;font:14px/1.45 "JetBrains Mono","DejaVu Sans Mono",Menlo,Consolas,monospace;
font-variant-ligatures:none;font-feature-settings:"liga" 0,"calt" 0}}   /* >= stays >=, -> stays -> */
.term{{display:inline-block;margin:16px;border:1px solid #34332f;border-radius:8px;overflow:hidden;background:#1e1e1c}}
.bar{{background:#2a2a28;color:#b8b7b1;font:12px/1.4 system-ui,sans-serif;padding:7px 14px}}
pre{{margin:0;padding:12px 16px 16px;color:#ecebe7;font:inherit;width:100ch;white-space:pre-wrap;
overflow-wrap:anywhere}}
</style></head><body><div class=term><div class=bar>{label}</div><pre>{text}</pre></div></body></html>
"""


def shoot(runs: dict, out: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright
    made = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1, color_scheme="light")

        def top(selector: str, nth: int = 0) -> float:      # in page coordinates, whatever the scroll
            return page.locator(selector).nth(nth).evaluate("e => e.getBoundingClientRect().top + window.scrollY")

        def bottom(selector: str, nth: int = 0) -> float:
            return page.locator(selector).nth(nth).evaluate("e => e.getBoundingClientRect().bottom + window.scrollY")

        def save(name: str, y0: float, y1: float) -> None:
            path = out / name
            page.screenshot(path=str(path), full_page=True, clip={**COLUMN, "y": max(0, y0), "height": y1 - max(0, y0)})
            made.append(path)

        def report(folder: Path, run: str) -> None:
            page.goto((folder / ".testbench" / "runs" / run / "report.html").as_uri())

        report(runs["demo"], runs["failed"])
        save("report-fail.png", 0, bottom("div.scroll") + 24)          # down to its steps table
        report(runs["demo"], runs["passed"])
        save("report-pass.png", 0, bottom("figcaption") + 8)

        page.goto((runs["demo"] / ".testbench" / "ui" / "index.html").as_uri())
        save("ui-index.png", 0, bottom("main > :last-child") + 24)
        page.get_by_role("link", name=runs["best"]).click()        # the loop's best run, as a reader would open it
        page.get_by_text("evidence files (sizes and sha256)").click()
        save("ui-run.png", top("details") - 6, bottom("main > pre") + 24)

        report(runs["web"], runs["web_failed"])
        checks = page.locator("h2", has_text="Checks")
        save("web-report.png", checks.evaluate("e => e.getBoundingClientRect().top + window.scrollY") - 16,
             bottom("figcaption") + 8)

        started = re.search(r"started (\d{4}-\d\d-\d\d \d\d:\d\d)", runs["failed_output"])
        label = (f"Real output of <b>testbench run smoke</b> with the planted bug, "
                 f"{started.group(1) if started else ''} - captured from the command, not edited")
        page.set_content(TERMINAL.format(label=label, text=html.escape("$ testbench run smoke\n"
                                                                        + runs["failed_output"].rstrip("\n"))))
        path = out / "terminal-fail.png"
        page.locator(".term").screenshot(path=str(path))
        made.append(path)
        browser.close()
    return made


def optimise(path: Path) -> None:
    """The screenshot as it is if it fits LIMIT, else a palette PNG with fewer colours until it does (max-coverage,
    no dithering: small coloured text such as FAIL keeps its colour, and text stays sharp)."""
    from PIL import Image
    image = Image.open(path).convert("RGB")
    image.save(path, optimize=True)
    for colours in (256, 128, 64, 32):
        if path.stat().st_size <= LIMIT:
            return
        image.quantize(colors=colours, method=Image.Quantize.MAXCOVERAGE, dither=Image.Dither.NONE).save(
            path, optimize=True)
    if path.stat().st_size > LIMIT:
        print(f"note: {path.name} is {path.stat().st_size // 1000} KB even with 32 colours")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--keep", metavar="DIR", help="work in DIR (new or empty) and keep it, with transcript.txt")
    ap.add_argument("--out", metavar="DIR", default=str(HERE), help="where the images go (default: docs/assets)")
    a = ap.parse_args(argv)
    missing = [name for name, mod in (("git", None), ("playwright", "playwright"), ("Pillow", "PIL"),
                                      ("agent-testbench", "agent_testbench"))
               if (shutil.which("git") is None if mod is None else not _importable(mod))]
    if missing:
        print(f"make_screenshots.py needs {', '.join(missing)}; see the top of this file")
        return 2
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if a.keep:
        work = Path(a.keep).resolve()
        if work.exists() and any(work.iterdir()):
            print(f"{work} is not empty; give --keep a new folder")
            return 2
        work.mkdir(parents=True, exist_ok=True)
    else:
        work = Path(tempfile.mkdtemp(prefix="testbench-shots-"))
    try:
        tb = Bench(work)
        try:
            runs = make_runs(tb)
        finally:
            (work / "transcript.txt").write_text("\n".join(tb.log))
        for path in shoot(runs, out):
            optimise(path)
            print(f"{path}  {path.stat().st_size // 1000} KB")
    finally:
        if not a.keep:
            shutil.rmtree(work, ignore_errors=True)
    if a.keep:
        print(f"the projects and every command's output: {work} (transcript.txt)")
    return 0


def _importable(module: str) -> bool:
    import importlib.util
    return importlib.util.find_spec(module) is not None


if __name__ == "__main__":
    sys.exit(main())
