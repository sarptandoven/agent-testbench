"""Reports: one page that says what worked, what failed and why, what it cost, and what changed since last time.

report.md is written for whoever reads it next - usually the agent deciding what to try - so failures come
first, with the cell, the error and the line that raised it, and the comparison with the previous run of the
same plan says which steps a change fixed or broke.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

ICON = {"passed": "PASS", "failed": "FAIL", "timeout": "TIMEOUT", "error": "ERROR", "cancelled": "CANCELLED",
        "running": "RUNNING", "queued": "QUEUED"}


def load(run_dir: Path) -> dict | None:
    p = Path(run_dir) / "report.json"
    return json.loads(p.read_text()) if p.exists() else None


def _metrics(report: dict) -> dict:
    """Numbers checked by expectations, keyed by step and check: what a change moved."""
    out = {}
    for s in report.get("steps", []):
        for c in s.get("expectations", []) or []:
            detail = str(c.get("detail", ""))
            head = detail.split(" (wanted")[0].strip().strip("'")
            try:
                out[f"{s['name']} | {c['check']}"] = float(head)
            except ValueError:
                pass
    return out


def compare(prev: dict | None, cur: dict) -> dict:
    if not prev:
        return {}
    ps = {s["name"]: s["status"] for s in prev.get("steps", [])}
    cs = {s["name"]: s["status"] for s in cur.get("steps", [])}
    fixed = [n for n, st in cs.items() if st == "passed" and ps.get(n) not in (None, "passed")]
    broke = [n for n, st in cs.items() if st != "passed" and ps.get(n) == "passed"]
    still = [n for n, st in cs.items() if st != "passed" and ps.get(n) not in (None, "passed")]
    pm, cm = _metrics(prev), _metrics(cur)
    moved = {k: [pm[k], cm[k]] for k in cm if k in pm and pm[k] != cm[k]}
    return {"previous_run": prev.get("run_id"), "previous_status": prev.get("status"), "fixed": fixed, "broke": broke,
            "still_failing": still, "metrics_moved": moved}


def render(report: dict) -> str:
    r = report
    when = dt.datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M") if r.get("started") else "?"
    cost = r.get("cost_usd")
    lines = [f"# {ICON.get(r['status'], r['status'].upper())} - plan `{r.get('plan')}` on {r.get('backend')}"
             f"{' (' + r['gpu'] + ')' if r.get('gpu') else ''}",
             "",
             f"Run `{r.get('run_id')}`, started {when}, {r.get('minutes', '?')} min"
             + (f", about ${cost:.3f}" if isinstance(cost, (int, float)) else "")
             + (f". Stopped because {r['stopped_because']}." if r.get("stopped_because") else "."), ""]
    if r.get("launch_error"):
        lines += ["## Could not start", "", r["launch_error"], ""]
    failing = [s for s in r.get("steps", []) if s["status"] != "passed"]
    for s in failing:
        lines += [f"## {ICON[s['status']]}: {s['name']}", ""]
        e = s.get("error") or {}
        if e:
            where = f"cell {e['cell']}" if e.get("cell") else s["kind"]
            lines += [f"- **{where}**: `{e.get('ename')}`: {str(e.get('evalue', ''))[:600]}"]
            if e.get("line"):
                lines += [f"- raised at {e['line']}"]
            if e.get("source_head"):
                lines += ["- cell starts:", "", "```python", e["source_head"], "```"]
            if e.get("traceback_tail"):
                lines += ["", "<details><summary>traceback</summary>", "", "```", e["traceback_tail"][-2500:], "```",
                          "", "</details>"]
        bad = [c for c in s.get("expectations", []) or [] if not c["ok"]]
        for c in bad:
            lines += [f"- expected {c['check']}: {c['detail']}"]
        web = s.get("web") or {}
        for key in ("console_errors", "page_errors", "failed_requests", "action_errors"):
            for item in web.get(key, [])[:5]:
                lines += [f"- {key.replace('_', ' ')}: {item}"]
        if s.get("stdout_tail") and not e.get("traceback_tail"):
            lines += ["", "Last output:", "", "```", s["stdout_tail"][-1500:], "```"]
        lines += [f"- evidence: `{s.get('dir')}/` (log.txt, result.json"
                  + (", executed.ipynb" if s["kind"] == "notebook" else "") + ")", ""]
    d = r.get("delta") or {}
    if d:
        lines += [f"## Compared with the previous run `{d.get('previous_run')}` ({d.get('previous_status')})", ""]
        for label, key in (("Fixed", "fixed"), ("Broke", "broke"), ("Still failing", "still_failing")):
            if d.get(key):
                lines += [f"- {label}: {', '.join(d[key])}"]
        for k, (a, b) in (d.get("metrics_moved") or {}).items():
            lines += [f"- {k}: {a:g} -> {b:g}"]
        if not any(d.get(k) for k in ("fixed", "broke", "still_failing", "metrics_moved")):
            lines += ["- no change in step results or checked numbers"]
        lines += [""]
    lines += ["## Steps", "", "| | step | minutes | checks | GPU busy |", "|---|---|---|---|---|"]
    for s in r.get("steps", []):
        checks = s.get("expectations") or []
        ok = sum(1 for c in checks if c["ok"])
        gpu = s.get("gpu") or {}
        lines += [f"| {ICON[s['status']]} | {s['name']} | {s.get('minutes', 0):.2f} | {ok}/{len(checks)} | "
                  f"{str(gpu.get('busy_pct')) + '%' if gpu else '-'} |"]
    for name in r.get("skipped", []):
        lines += [f"| SKIPPED | {name} | | | |"]
    lines += [""]
    shown = []
    for s in r.get("steps", []):
        files = [f"{s['dir']}/{x}" for x in s.get("images", [])] + \
                [f"{s['dir']}/{x}" for x in (s.get('web') or {}).get("screenshots", [])] + \
                [f"{s['dir']}/artifacts/{x}" for x in s.get("artifacts", []) if "(not copied" not in x] + \
                [f"{s['dir']}/downloads/{x}" for x in s.get("downloads", [])]
        shown += files
    if shown:
        lines += ["## Evidence to look at", ""] + [f"- `{f}`" for f in shown[:40]] + [""]
    passing = [s for s in r.get("steps", []) if s["status"] == "passed"]
    if passing:
        lines += ["## Passed checks", ""]
        for s in passing:
            for c in s.get("expectations", []) or []:
                lines += [f"- {s['name']}: {c['check']} - {c['detail']}"]
        lines += [""]
    return "\n".join(lines).rstrip() + "\n"


def write(run_dir: Path, report: dict) -> None:
    (Path(run_dir) / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (Path(run_dir) / "report.md").write_text(render(report))
