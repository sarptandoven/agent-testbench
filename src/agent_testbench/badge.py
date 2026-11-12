"""testbench badge: a README badge of a plan's latest run, drawn here as a small SVG - no service, no network.

    testbench badge [PLAN] [-o testbench-PLAN.svg] [--link URL]

"testbench | 14 checks passed" on green, "testbench | 2 of 14 checks failed" on red, "testbench | no runs" on grey.
The words carry the result, so it reads without the colours; white text on each colour is 4.5:1 or more (WCAG AA,
tests/test_badge.py checks it). `testbench share` puts one next to the page it publishes.
"""
from __future__ import annotations

import html
from pathlib import Path

from . import report as R, runs
from .config import counted

LABEL = "testbench"
TEXT = "#ffffff"
COLORS = {"label": "#555555", "passed": "#1f7a3f", "failed": "#b3261e", "warn": "#8a5a00", "none": "#5f5f5a"}
HEIGHT = 20


def message(rep: dict | None) -> tuple[str, str]:
    """(the words on the right, its colour key) for a run's report; None: no finished run yet."""
    if not isinstance(rep, dict):
        return "no runs", "none"
    checks = [c for s in rep.get("steps") or [] if isinstance(s, dict)
              for c in s.get("expectations") or [] if isinstance(c, dict)]
    missed = sum(1 for c in checks if not c.get("ok"))
    status = str(rep.get("status"))
    if status == "passed":
        return (f"{counted(len(checks), 'check')} passed" if checks else "passed"), "passed"
    if status in ("failed", "error", "timeout"):
        if missed:
            return f"{missed} of {counted(len(checks), 'check')} failed", "failed"
        return {"timeout": "timed out"}.get(status, status), "failed"
    return (status if status in ("cancelled", "lost") else "not finished"), "warn"


def _width(text: str) -> float:
    """About how wide text is in 11px Verdana; textLength then makes it exactly that wide in any font."""
    narrow, wide = set("ijl.,:;'!|()[] "), set("mwMW@%")
    return sum(3.6 if ch in narrow else 9.8 if ch in wide else 7.6 if ch.isupper() or ch.isdigit() else 6.6
               for ch in text)


def svg(words: str, color: str) -> str:
    """The badge: LABEL on dark grey, then the words on the result's colour."""
    left, right = _width(LABEL) + 12, _width(words) + 12
    total = left + right
    alt = html.escape(f"{LABEL}: {words}")
    text = html.escape(words)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{total:.0f}" height="{HEIGHT}" role="img" '
            f'aria-label="{alt}"><title>{alt}</title>'
            f'<clipPath id="r"><rect width="{total:.0f}" height="{HEIGHT}" rx="3"/></clipPath><g clip-path="url(#r)">'
            f'<rect width="{left:.0f}" height="{HEIGHT}" fill="{COLORS["label"]}"/>'
            f'<rect x="{left:.0f}" width="{total - left:.0f}" height="{HEIGHT}" fill="{COLORS[color]}"/></g>'
            f'<g fill="{TEXT}" text-anchor="middle" font-family="Verdana,DejaVu Sans,Geneva,sans-serif" '
            f'font-size="11"><text x="{left / 2:.1f}" y="14" textLength="{left - 12:.0f}">{LABEL}</text>'
            f'<text x="{left + right / 2:.1f}" y="14" textLength="{right - 12:.0f}">{text}</text></g></svg>\n')


def for_report(rep: dict | None) -> tuple[str, str]:
    """(the SVG, its alt text) for a run's report."""
    words, color = message(rep)
    return svg(words, color), f"{LABEL}: {words}"


def latest(root: Path, plan: str | None) -> dict | None:
    """The report of the plan's latest finished run (any plan's when plan is None); None when there is none."""
    for run_dir in reversed(runs.all_runs(root)):
        rep = R.load(run_dir)
        if rep and rep.get("status") in runs.DONE and (plan is None or rep.get("plan") == plan):
            return rep
    return None


def markdown(alt: str, image: str, link: str | None = None) -> str:
    """The line to paste in a README."""
    shown = f"![{alt.replace(']', ')')}]({image})"
    return f"[{shown}]({link})" if link else shown


def file_name(plan: str | None) -> str:
    """testbench-PLAN.svg, with only letters, digits, - and _ from the plan's name."""
    safe = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in str(plan or "")).strip("-")
    return f"testbench-{safe}.svg" if safe else "testbench.svg"
