"""testbench ui: the project's runs as static pages - no server, nothing to install, open the file in a browser.

    testbench ui [--open]      # writes .testbench/ui/index.html and a report.html per run

The index has every plan (its latest result, pass rate and a trend line per checked number), every run, the loops
and the sessions. Each run's page is report.html in its folder, self-contained, with the evidence images inline.
"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

from . import budget, report as R, runs
from .config import counted

e = html.escape


def sparkline(values: list, width: int = 160, height: int = 34) -> str:
    """An inline SVG trend line; runs that did not reach the check leave a gap."""
    points = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(points) < 2:
        return "<span class=muted>-</span>" if not points else f"{points[0][1]:g}"
    lo, hi = min(v for _, v in points), max(v for _, v in points)
    span, step = (hi - lo) or 1.0, (width - 8) / max(1, len(values) - 1)
    xy = [(4 + i * step, height - 4 - (v - lo) / span * (height - 8)) for i, v in points]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    title = " -> ".join("-" if v is None else f"{v:g}" for v in values)
    return (f"<svg width={width} height={height} role=img aria-label='{e(title)}'><title>{e(title)}</title>"
            f"<polyline fill=none stroke=currentColor stroke-width=1.5 points='{path}'/>"
            f"<circle cx={xy[-1][0]:.1f} cy={xy[-1][1]:.1f} r=2.5 fill=currentColor /></svg> "
            f"<span class=muted>{points[0][1]:g} &rarr; {points[-1][1]:g}</span>")


def _readable(rep) -> bool:
    """A report whose fields have the types the pages use (one cut short or edited by hand may not)."""
    return isinstance(rep, dict) and all(isinstance(rep.get(k), str) for k in ("run_id", "status")) \
        and all(isinstance(rep.get(k), (int, float, type(None))) for k in ("started", "minutes", "cost_usd")) \
        and isinstance(rep.get("steps", []), list) and all(isinstance(s, dict) for s in rep.get("steps", [])) \
        and isinstance(rep.get("source") or {}, dict) and all(isinstance(rep.get(k) or "", str) for k in ("gpu", "plan"))


def _when(t) -> str:
    try:
        return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else ""
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def build(root: Path, cfg: dict) -> Path:
    root = Path(root)
    out_dir = root / ".testbench" / "ui"
    out_dir.mkdir(parents=True, exist_ok=True)
    reports, unreadable = [], []
    for run_dir in runs.all_runs(root):
        rep = R.load(run_dir)
        try:
            if not _readable(rep):
                raise ValueError(rep)
            page = run_dir / "report.html"
            if not page.exists() or page.stat().st_mtime < (run_dir / "report.json").stat().st_mtime:
                page.write_text(R.render_html(rep, run_dir))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, OverflowError):
            if (run_dir / "report.json").exists():       # cut short or edited by hand; a running run has none yet
                unreadable.append(run_dir.name)
            continue
        reports.append(rep)
    done = [r for r in reports if r.get("status") in runs.DONE]
    spent = sum(e_["usd"] for e_ in budget.entries(root) if e_["event"] == "finish")
    body = [f"<h1>{e(cfg['project'])}</h1>",
            f"<div class=muted>{counted(len(reports), 'run')}, {sum(r['status'] == 'passed' for r in done)} passed; "
            f"${budget.spent_today(root):.3f} committed today, ${spent:.3f} in all (estimates). Built "
            f"{dt.datetime.now():%Y-%m-%d %H:%M}; rebuild with <code>testbench ui</code>.</div>"]
    if unreadable:
        body.append(f"<p class=muted>Left out: {len(unreadable)} run(s) whose report.json cannot be read: "
                    f"{e(', '.join(unreadable[:20]))}.</p>")
    body.append("<h2>Plans</h2><div class=scroll><table><tr><th>plan</th><th>latest</th><th>runs</th><th>passed</th>"
                "<th>checked numbers over time</th></tr>")
    for name in list(cfg["plans"]) + sorted({r.get("plan") for r in reports} - set(cfg["plans"]), key=str):
        mine = [r for r in done if r.get("plan") == name]
        h = R.history(mine)
        latest = mine[-1] if mine else None
        trends = "<br>".join(f"<code>{e(k)}</code><br>{sparkline(v)}" for k, v in h["metrics"].items()) or "-"
        status = (f"<a class='{e(latest['status'])}' href='../runs/{e(latest['run_id'])}/report.html'>"
                  f"{e(R.ICON.get(latest['status'], latest['status']))}</a> <span class=muted>{_when(latest.get('started'))}</span>"
                  if latest else "<span class=muted>never run</span>")
        body.append(f"<tr><td><b>{e(str(name))}</b></td><td>{status}</td><td>{len(mine)}</td>"
                    f"<td>{(100 * sum(r['status'] == 'passed' for r in mine) // len(mine)) if mine else 0}%</td>"
                    f"<td>{trends}</td></tr>")
    body.append("</table></div><h2>Runs</h2><div class=scroll><table><tr><th></th><th>run</th><th>plan</th>"
                "<th>where</th><th>minutes</th><th>cost</th><th>code</th></tr>")
    for r in reversed(reports):
        src = r.get("source") or {}
        cost = f"${r['cost_usd']:.3f}" if isinstance(r.get("cost_usd"), (int, float)) else ""
        body.append(f"<tr><td class='{e(r['status'])}'>{e(R.ICON.get(r['status'], r['status']))}</td>"
                    f"<td><a href='../runs/{e(r['run_id'])}/report.html'>{e(r['run_id'])}</a></td>"
                    f"<td>{e(str(r.get('plan')))}</td><td>{e(str(r.get('backend')))}{' ' + e(r['gpu']) if r.get('gpu') else ''}</td>"
                    f"<td>{e(str(r.get('minutes') or ''))}</td><td>{cost}</td>"
                    f"<td><code>{e(str(src.get('commit') or '')[:9])}{'+' if src.get('dirty') else ''}</code></td></tr>")
    body.append("</table></div>")
    loops = sorted((root / ".testbench" / "loops").glob("*/state.json")) if (root / ".testbench" / "loops").exists() else []
    if loops:
        body.append("<h2>Loops</h2><ul>")
        for p in loops:
            try:
                s = json.loads(p.read_text())
                kept = sum(1 for t in s["tries"] if t.get("kept") and t["n"] > 0)
                rep = p.parent / "report.md"
                link = ""
                if rep.exists():
                    (p.parent / "report.html").write_text(R.page(f"loop {s['id']}", f"<pre>{e(rep.read_text())}</pre>"))
                    link = f" &middot; <a href='../loops/{e(s['id'])}/report.html'>report</a>"
                body.append(f"<li><b>{e(s['id'])}</b> ({e(s['status'])}): {e(s['key'])} best "
                            f"{'-' if s['best']['value'] is None else format(s['best']['value'], 'g')}, "
                            f"{counted(len(s['tries']) - 1, 'try', 'tries')}, {kept} kept{link}</li>")
            except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError):
                body.append(f"<li><b>{e(p.parent.name)}</b>: its state.json cannot be read</li>")
        body.append("</ul>")
    sessions = (root / ".testbench" / "sessions")
    found = sorted(d for d in sessions.iterdir() if (d / "state.json").exists()) if sessions.exists() else []
    if found:
        from .sessions import describe
        body.append("<h2>Sessions</h2><pre>" + e("\n".join(describe(d) for d in found)) + "</pre>")
    index = out_dir / "index.html"
    index.write_text(R.page(f"testbench: {cfg['project']}", "\n".join(body)))
    return index
