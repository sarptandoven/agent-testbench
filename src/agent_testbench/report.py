"""Reports: one page that says what worked, what failed and why, what it cost, and what changed since last time.

report.md is written for whoever reads it next - usually the agent deciding what to try - so failures come
first, with the cell, the error and the line that raised it, and the comparison with the previous run of the
same plan says which steps a change fixed or broke.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import re
from pathlib import Path

from .config import counted

ICON = {"passed": "PASS", "failed": "FAIL", "timeout": "TIMEOUT", "error": "ERROR", "cancelled": "CANCELLED",
        "running": "RUNNING", "queued": "QUEUED"}
NOT_XML = re.compile(f"[^\t\n\r -{chr(0xD7FF)}{chr(0xE000)}-{chr(0xFFFD)}{chr(0x10000)}-{chr(0x10FFFF)}]")  # control chars, surrogates


def _icon(status) -> str:
    return ICON.get(status, str(status).upper())


def _line(text) -> str:
    """On one line: a line break in a name or detail would start a heading or a list item of its own."""
    return " ".join(str(text).splitlines())


def _cell(text) -> str:
    """One markdown table cell: a | in a step's name would shift the columns."""
    return _line(text).replace("|", "\\|")


def _fenced(text, lang: str = "") -> list[str]:
    """text as a code block whose fence is longer than any run of backticks in it, so output that prints ``` (a log
    of markdown, a notebook's own code block) cannot end the block and turn the rest into markdown - report.md is also
    the job summary of the GitHub Action."""
    fence = "`" * max(3, 1 + max(map(len, re.findall(r"`+", str(text))), default=0))
    return [fence + lang, str(text), fence]


def _g(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else str(v)


def _encodable(text: str) -> str:
    """Text that can be written as UTF-8 (a file name that is not UTF-8 reaches the report as a lone surrogate)."""
    return text.encode("utf-8", "replace").decode("utf-8")


def inside(run_dir: Path, path: Path) -> bool:
    """path is a file in the run folder. A report names its evidence (a step's dir, images, artifacts), and a report
    from a container or another machine can name anything: an absolute path, .. or a link must not lead out."""
    try:
        return Path(path).resolve().is_relative_to(Path(run_dir).resolve()) and Path(path).is_file()
    except (OSError, RuntimeError, ValueError, TypeError):
        return False


def load(run_dir: Path) -> dict | None:
    """The run's report, or None when there is none or it cannot be read (cut short, or not a report)."""
    p = Path(run_dir) / "report.json"
    try:
        rep = json.loads(p.read_text())
    except (OSError, ValueError, RecursionError):
        return None
    return rep if isinstance(rep, dict) else None


def check_number(c: dict) -> float | None:
    """The number a check found, or None: a measuring check's detail starts with it ("0.93 (wanted >= 0.9)")."""
    head = str(c.get("detail", "")).split(" (wanted")[0].strip().strip("'\"")
    try:
        n = float(head)
    except ValueError:
        return None
    return n if math.isfinite(n) else None                       # nan is no number to track or to loop on


def checked(report: dict) -> dict:
    """Every check that found a number, keyed by step and check ("step | check"): {key: (number, step, check)}."""
    out = {}
    for s in report.get("steps") or []:
        for c in (s.get("expectations") or []) if isinstance(s, dict) else []:
            if isinstance(c, dict) and (n := check_number(c)) is not None:
                out[f"{s.get('name')} | {c.get('check')}"] = (n, s, c)
    return out


def _metrics(report: dict) -> dict:
    """Numbers checked by expectations, keyed by step and check: what a change moved."""
    return {k: n for k, (n, _, _) in checked(report).items()}


def compare(prev: dict | None, cur: dict) -> dict:
    """What changed since the previous run of the plan: steps fixed, broken and still failing, and the checked
    numbers that moved. Every report keeps it as `delta`; `testbench diff` (diff.py) builds on it."""
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


def render(report: dict, run_dir: Path | None = None) -> str:
    """report.md. With the run folder, a step whose artifacts hold an equity.csv also gets a Strategy section."""
    r = report
    when = dt.datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M") if r.get("started") else "?"
    cost = r.get("cost_usd")
    lines = [f"# {ICON.get(r['status'], r['status'].upper())} - plan `{r.get('plan')}` on {r.get('backend')}"
             f"{' (' + r['gpu'] + ')' if r.get('gpu') else ''}",
             "",
             f"Run `{r.get('run_id')}`, started {when}, {r.get('minutes', '?')} min"
             + (f", about ${cost:.3f}" if isinstance(cost, (int, float)) else "")
             + (f". Stopped because {r['stopped_because']}." if r.get("stopped_because") else ".")]
    src, env = r.get("source") or {}, r.get("environment") or {}
    if src or env or r.get("evidence_sha256"):
        lines += ["Code " + (f"`{src['commit'][:12]}`" + (" with uncommitted changes" if src.get("dirty") else "")
                             if src else "outside git")
                  + (f"; Python {env['python']} on {env.get('machine', '?')}" if env.get("python") else "")
                  + (f"; evidence sha256 `{r['evidence_sha256'][:16]}` (`testbench verify` checks it)"
                     if r.get("evidence_sha256") else "") + "."]
    lines += [""]
    if holdout_line(r):
        lines += [holdout_line(r), ""]
    for finding in plan_findings(r):                     # look-ahead, model_cutoff
        lines += [finding, ""]
    if r.get("launch_error"):
        lines += ["## Could not start", "", r["launch_error"], ""]
    failing = [s for s in r.get("steps", []) if s["status"] != "passed"]
    for s in failing:
        lines += [f"## {_icon(s['status'])}: {_line(s['name'])}", ""]
        e = s.get("error") or {}
        if e:
            where = f"cell {e['cell']}" if e.get("cell") else s["kind"]
            lines += [f"- **{where}**: `{e.get('ename')}`: {str(e.get('evalue', ''))[:600]}"]
            if e.get("line"):
                lines += [f"- raised at {e['line']}"]
            if e.get("source_head"):
                lines += ["- cell starts:", "", *_fenced(e["source_head"], "python")]
            if e.get("traceback_tail"):
                lines += ["", "<details><summary>traceback</summary>", "", *_fenced(e["traceback_tail"][-2500:]),
                          "", "</details>"]
        bad = [c for c in s.get("expectations", []) or [] if not c["ok"]]
        for c in bad:
            lines += [f"- expected {_line(c['check'])}: {_line(c['detail'])}"]
        web = s.get("web") or {}
        for key in ("console_errors", "page_errors", "failed_requests", "action_errors"):
            for item in web.get(key, [])[:5]:
                lines += [f"- {key.replace('_', ' ')}: {item}"]
        if s.get("stdout_tail") and not e.get("traceback_tail"):
            lines += ["", "Last output:", "", *_fenced(s["stdout_tail"][-1500:])]
        mocked = any(str(c.get("check", "")).startswith("mock ") for c in s.get("expectations") or [])
        lines += [f"- evidence: `{s.get('dir')}/` (log.txt, result.json"
                  + (", executed.ipynb" if s["kind"] == "notebook" else "")
                  + (", mock_calls.jsonl" if mocked else "") + ")", ""]
    d = r.get("delta") or {}
    if d:
        lines += [f"## Compared with the previous run `{d.get('previous_run')}` ({d.get('previous_status')})", ""]
        for label, key in (("Fixed", "fixed"), ("Broke", "broke"), ("Still failing", "still_failing")):
            if d.get(key):
                lines += [f"- {label}: {', '.join(d[key])}"]
        for k, (a, b) in (d.get("metrics_moved") or {}).items():
            lines += [f"- {k}: {_g(a)} -> {_g(b)}"]
        if not any(d.get(k) for k in ("fixed", "broke", "still_failing", "metrics_moved")):
            lines += ["- no change in step results or checked numbers"]
        lines += [""]
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    if data:
        lines += ["## Data", ""] + [f"- {_line(role)}: {data_line(d)}" for role, d in data.items()
                                    if isinstance(d, dict)] + [""]
    windows = [w for w in r.get("windows") or [] if isinstance(w, dict)]
    if windows:
        passed = sum(w.get("status") == "passed" for w in windows)
        lines += [f"## Walk-forward: {counted(len(windows), 'window')}, {passed} passed", "",
                  "| | window | train | test | rows (train / test) |", "|---|---|---|---|---|"]
        lines += [f"| {_icon(w.get('status'))} | {_cell(w.get('window'))} | {_cell(w.get('train_start'))} to "
                  f"{_cell(w.get('train_end'))} | {_cell(w.get('test_start'))} to {_cell(w.get('test_end'))} | "
                  f"{_cell(w.get('train_rows'))} / {_cell(w.get('test_rows'))} |" for w in windows]
        lines += ["", "The plan passes only when every window does; window k's data holds only the rows before its "
                      "test end.", ""]
        lines += [counting_line(r), ""] if counting_line(r) else []
    lines += lookahead_section(r)
    lines += ["## Steps", "", "| | step | minutes | checks | GPU busy |", "|---|---|---|---|---|"]
    for s in r.get("steps", []):
        checks = s.get("expectations") or []
        ok = sum(1 for c in checks if c["ok"])
        gpu = s.get("gpu") or {}
        lines += [f"| {_icon(s['status'])} | {_cell(s['name'])} | {s.get('minutes', 0):.2f} | {ok}/{len(checks)} | "
                  f"{str(gpu.get('busy_pct')) + '%' if gpu else '-'} |"]
    for name in r.get("skipped", []):
        lines += [f"| SKIPPED | {_cell(name)} | | | |"]
    lines += [""]
    lines += _strategy(r, run_dir, "md")
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
                lines += [f"- {_line(s['name'])}: {_line(c['check'])} - {_line(c['detail'])}"]
        lines += [""]
    return _encodable("\n".join(lines).rstrip() + "\n")


def _short(sha) -> str:
    return f"`{str(sha)[:12]}`" if sha else "-"


def data_line(d: dict) -> str:
    """One role of report.json's `data` in words: what the steps saw, the sealed part, the whole file."""
    cut, file, sealed = d.get("holdout_from"), d.get("file") or {}, d.get("sealed") or {}
    saw = {"sealed": f"the rows before {cut} (the rest is sealed)",
           "full": "the whole file" + (f", the sealed rows from {cut} included" if cut else ""),
           "windows": "cut per walk-forward window from these rows" + (f", before the seal at {cut}" if cut else "")
           }.get(d.get("view"), "the file")
    text = (f"`{_line(d.get('path'))}`, {saw}: {d.get('rows')} rows, {d.get('first')} to {d.get('last')}, sha256 "
            f"{_short(d.get('sha256'))}")
    if cut:
        text += f"; sealed: {sealed.get('rows', 0)} rows from {cut}, sha256 {_short(sealed.get('sha256'))}"
    if file and file.get("sha256") != d.get("sha256"):
        text += f"; the whole file: {file.get('rows')} rows, sha256 {_short(file.get('sha256'))}"
    return _line(text + (f". {d['note']}" if d.get("note") else ""))


def holdout_line(r: dict) -> str:
    """For a run of the holdout plan: how many times the sealed rows were opened before it, as a warning."""
    h = r.get("holdout")
    if not isinstance(h, dict):
        return ""
    roles = h.get("roles") if isinstance(h.get("roles"), dict) else {}
    what = ", ".join(f"{role} from {cut}" for role, cut in roles.items()) or "the sealed rows"
    n, first = h.get("opened_before"), h.get("first") if isinstance(h.get("first"), dict) else {}
    if not n:
        return f"Holdout ({_line(what)}): this run is the first to open it, so results on it are untouched."
    return _line(f"**Warning**: the holdout ({what}) was opened {counted(n, 'time')} before this run: results on it "
                 f"are no longer untouched (first opened in run `{first.get('run')}`)" + (
                     f"; holdout.max_opens is {h['max_opens']}" if h.get("max_opens") else "") + ".")


LOOK_STATUS = {"same": "PASS", "different": "FAIL", "crashed": "CRASHED", "skipped": "SKIPPED",
               "incomplete": "CANCELLED"}
LOOK_CLASS = {"same": "passed", "different": "failed", "crashed": "failed"}
LOOK_TITLE = {"clean": "none found", "look-ahead": "FOUND", "crashed": "not checked, a rerun crashed",
              "not checked": "not checked"}


def plan_findings(r: dict) -> list[str]:
    """What a plan-level check found, for the top of a report (markdown): a look-ahead the reruns found, or why they
    could not tell; and how the results stand against the model's training cutoff."""
    out = []
    la, mc = r.get("lookahead"), r.get("model_cutoff")
    if isinstance(la, dict) and la.get("finding") and not la.get("ok"):
        out.append(_line(f"**FAIL** - {la['finding']}"))
    if isinstance(mc, dict) and mc.get("finding"):
        lead = {"failed": "**FAIL** - ", "warning": "**Warning**: "}.get(mc.get("level"), "")
        out.append(_line(f"{lead}{mc['finding']}."))
    return out


def counting_line(r: dict) -> str:
    """With model_cutoff, which walk-forward windows test after it: the ones that count as evidence."""
    windows = [w for w in r.get("windows") or [] if isinstance(w, dict) and "after_cutoff" in w]
    mc = r.get("model_cutoff") if isinstance(r.get("model_cutoff"), dict) else {}
    if not windows:
        return ""
    after = [str(w.get("window")) for w in windows if w.get("after_cutoff")]
    return _line(f"Windows that test after model_cutoff {mc.get('date')}, the ones that count as evidence: "
                 + (", ".join(after) if after else "none") + ".")


def lookahead_section(r: dict) -> list[str]:
    """The Look-ahead section (markdown): each rerun's cut, the rows compared, and the verdict."""
    la = r.get("lookahead")
    if not isinstance(la, dict):
        return []
    runs = [x for x in la.get("runs") or [] if isinstance(x, dict)] if isinstance(la.get("runs"), list) else []
    title = LOOK_TITLE.get(la.get("verdict"), str(la.get("verdict")))
    if la.get("verdict") == "clean":
        title += f" ({counted(len(runs), 'rerun')}, {la.get('rows_compared')} rows compared)"
    tol = la.get("tolerance") if isinstance(la.get("tolerance"), dict) else {}
    rel = tol.get("relative") if isinstance(tol.get("relative"), (int, float)) else 1e-9
    out = [f"## Look-ahead: {_line(title)}", "",
           _line(f"`{la.get('output')}` from the plan's own run ({la.get('data')} up to {la.get('last')}) against "
                 f"{counted(len(runs), 'rerun')} on the data cut earlier, each compared on the rows dated before its "
                 f"cut; numbers within a relative {rel:g} of each other are the same decision."), ""]
    if runs:
        out += ["| | rerun | data before | data rows | rows compared | differences |", "|---|---|---|---|---|---|"]
        out += [f"| {LOOK_STATUS.get(x.get('status'), _cell(x.get('status')))} | {x.get('k')}/{x.get('n')} | "
                f"{_cell(x.get('before'))} | {_cell(x.get('data_rows'))} | {_cell(x.get('rows_compared'))} | "
                f"{_cell(x.get('differences'))} |" for x in runs]
        out += [""]
    if la.get("finding"):
        out += [_line(la["finding"]), ""]
    return out


def write(run_dir: Path, report: dict) -> None:
    (Path(run_dir) / "report.json").write_text(json.dumps(report, indent=1, default=str))
    (Path(run_dir) / "report.md").write_text(render(report, run_dir))


def _strategy(report: dict, run_dir: Path | None, form: str):
    """The tear sheets (trading/tearsheet.py) of the steps whose artifacts hold an equity.csv: report.md's Strategy
    lines (form "md") or the HTML report's section. The trading pack is imported only for a run that has one, and
    the report is made whatever happens in it."""
    empty = [] if form == "md" else ""
    steps = report.get("steps") if isinstance(report.get("steps"), list) else []
    steps = [s for s in steps if isinstance(s, dict) and isinstance(s.get("artifacts"), list)]
    if run_dir is None or not any(isinstance(a, str) and Path(a).name.lower() == "equity.csv"
                                  for s in steps for a in s["artifacts"]):
        return empty
    try:
        from .trading import tearsheet
        return tearsheet.markdown(report, run_dir) if form == "md" else tearsheet.html_section(report, run_dir)
    except Exception as exc:                          # never in the way of the report itself
        import html
        note = f"The tear sheet could not be made ({type(exc).__name__}: {exc})."
        return [note, ""] if form == "md" else f"<p class=muted>{html.escape(note)}</p>"


# ---- other formats: HTML (a page to look at), JUnit (for CI), history (a plan over time) -------------------------

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1b;--muted:#6b6b66;--line:#e3e2de;--card:#fff;--pass:#1f7a3f;--fail:#b3261e;--warn:#8a5a00;
--code:#f3f2ef;--link:#2457a6}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe7;--muted:#a3a29c;--line:#34332f;--card:#1e1e1c;
--pass:#5cc27f;--fail:#f08378;--warn:#e0b45c;--code:#262624;--link:#8fb3f0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,
"Segoe UI",sans-serif}main{max-width:980px;margin:0 auto;padding:24px 16px 64px}h1{font-size:22px;margin:0 0 4px}
h2{font-size:16px;margin:28px 0 8px}a{color:var(--link)}.muted{color:var(--muted)}.facts{display:grid;
grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px 16px;margin:16px 0}.facts div{background:var(--card);
border:1px solid var(--line);border-radius:8px;padding:8px 10px}.facts b{display:block;font-size:12px;color:var(--muted);
font-weight:500}table{border-collapse:collapse;width:100%;background:var(--card)}td,th{border:1px solid var(--line);
padding:6px 8px;text-align:left;vertical-align:top}th{font-weight:600;font-size:13px}pre{background:var(--code);
padding:10px;border-radius:6px;overflow-x:auto;font-size:13px;white-space:pre-wrap;word-break:break-word}
code{font-size:13px}.passed,.PASS{color:var(--pass);font-weight:600}.failed,.error,.timeout,.FAIL,.ERROR,.TIMEOUT
{color:var(--fail);font-weight:600}.cancelled,.CANCELLED,.running{color:var(--warn);font-weight:600}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:13px}
figure{margin:12px 0}figure img{max-width:100%;border:1px solid var(--line);border-radius:6px;background:#fff}
figcaption{font-size:12px;color:var(--muted)}.scroll{overflow-x:auto}
"""
IMAGE_LIMIT = 1_500_000
IMAGES_TOTAL = 12_000_000


def page(title: str, body: str, head: str = "") -> str:
    import html
    return _encodable(f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width="
                      f"device-width,initial-scale=1'><title>{html.escape(title)}</title><style>{CSS}</style>{head}"
                      f"</head><body><main>{body}</main></body></html>\n")


def render_html(report: dict, run_dir: Path) -> str:
    """The report as one self-contained page: what failed and why, the evidence images inline, what it ran on."""
    r = report
    return page(f"{ICON.get(r['status'], r['status'])} {r.get('plan')} {r.get('run_id')}", html_body(report, run_dir))


def html_body(report: dict, run_dir: Path) -> str:
    """What render_html's page holds (the live dashboard shows it under its own header)."""
    import base64
    import html
    e = html.escape
    r, run_dir = report, Path(run_dir)
    when = dt.datetime.fromtimestamp(r["started"]).strftime("%Y-%m-%d %H:%M") if r.get("started") else "?"
    hw = r.get("backend", "?") + (f" ({r['gpu']})" if r.get("gpu") else "")
    src, env = r.get("source") or {}, r.get("environment") or {}
    facts = [("plan", r.get("plan")), ("where", hw), ("started", when), ("minutes", r.get("minutes")),
             ("cost", f"${r['cost_usd']:.3f}" if isinstance(r.get("cost_usd"), (int, float)) else "?"),
             ("worst case", f"${r['worst_case_usd']:.2f}" if isinstance(r.get("worst_case_usd"), (int, float)) else "-"),
             ("code", (src.get("commit", "")[:12] + (" + uncommitted changes" if src.get("dirty") else "")) if src else "not in git"),
             ("python", f"{env.get('python', '?')} on {env.get('machine', '?')}" if env else "-"),
             ("evidence", f"{counted(len(r['evidence']), 'file')}, sha256 {r['evidence_sha256'][:12]}" if r.get("evidence_sha256")
              else "not recorded (an older run)")]
    out = [f"<h1><span class='badge {e(r['status'])}'>{e(ICON.get(r['status'], r['status'].upper()))}</span> plan "
           f"<code>{e(str(r.get('plan')))}</code></h1>",
           f"<div class=muted>run <code>{e(str(r.get('run_id')))}</code>"
           + (f" &middot; stopped because {e(r['stopped_because'])}" if r.get("stopped_because") else "") + "</div>",
           "<div class=facts>" + "".join(f"<div><b>{e(k)}</b>{e(str(v))}</div>" for k, v in facts) + "</div>"]
    if holdout_line(r):
        out.append(f"<p class={'cancelled' if (r.get('holdout') or {}).get('opened_before') else 'muted'}>"
                   f"{e(holdout_line(r).replace('**', ''))}</p>")
    for finding in plan_findings(r):                     # look-ahead, model_cutoff
        tone = "failed" if finding.startswith("**FAIL**") else "cancelled" if finding.startswith("**") else "muted"
        out.append(f"<p class={tone}>{e(finding.replace('**', ''))}</p>")
    if r.get("launch_error"):
        out += ["<h2>Could not start</h2>", f"<pre>{e(r['launch_error'])}</pre>"]
    for s in [s for s in r.get("steps", []) if s["status"] != "passed"]:
        out.append(f"<h2><span class='{e(s['status'])}'>{e(_icon(s['status']))}</span>: {e(s['name'])}</h2>")
        err = s.get("error") or {}
        if err:
            where = f"cell {err['cell']}" if err.get("cell") else s["kind"]
            out.append(f"<p><b>{e(where)}</b>: <code>{e(str(err.get('ename')))}</code>: {e(str(err.get('evalue', ''))[:1500])}</p>")
            if err.get("line"):
                out.append(f"<p>raised at {e(err['line'])}</p>")
            if err.get("source_head"):
                out.append(f"<pre>{e(err['source_head'])}</pre>")
            if err.get("traceback_tail"):
                out.append(f"<details><summary>traceback</summary><pre>{e(err['traceback_tail'][-4000:])}</pre></details>")
        bad = [c for c in s.get("expectations") or [] if not c["ok"]]
        if bad:
            out.append("<ul>" + "".join(f"<li>expected {e(c['check'])}: <b>{e(c['detail'])}</b></li>" for c in bad) + "</ul>")
        if s.get("stdout_tail") and not err.get("traceback_tail"):
            out.append(f"<details><summary>last output</summary><pre>{e(s['stdout_tail'][-3000:])}</pre></details>")
    d = r.get("delta") or {}
    if d:
        items = [e(f"{label}: {', '.join(d[key])}") for label, key in (("Fixed", "fixed"), ("Broke", "broke"),
                                                                       ("Still failing", "still_failing")) if d.get(key)]
        items += [f"{e(k)}: {e(_g(a))} &rarr; {e(_g(b))}" for k, (a, b) in (d.get("metrics_moved") or {}).items()]
        out += [f"<h2>Compared with <code>{e(str(d.get('previous_run')))}</code> ({e(str(d.get('previous_status')))})</h2>",
                "<ul>" + "".join(f"<li>{x}</li>" for x in items) + "</ul>"
                if items else "<p class=muted>no change in step results or checked numbers</p>"]
    data = r.get("data") if isinstance(r.get("data"), dict) else {}
    if data:
        out += ["<h2>Data</h2><ul>" + "".join(f"<li>{e(str(role))}: {e(data_line(d).replace('`', ''))}</li>"
                                              for role, d in data.items() if isinstance(d, dict)) + "</ul>"]
    windows = [w for w in r.get("windows") or [] if isinstance(w, dict)]
    if windows:
        out += [f"<h2>Walk-forward: {e(counted(len(windows), 'window'))}, "
                f"{sum(w.get('status') == 'passed' for w in windows)} passed</h2><div class=scroll><table><tr><th></th>"
                "<th>window</th><th>train</th><th>test</th><th>rows (train / test)</th></tr>"
                + "".join(f"<tr><td class='{e(str(w.get('status')))}'>{e(_icon(w.get('status')))}</td>"
                          f"<td>{e(str(w.get('window')))}</td><td>{e(str(w.get('train_start')))} to "
                          f"{e(str(w.get('train_end')))}</td><td>{e(str(w.get('test_start')))} to "
                          f"{e(str(w.get('test_end')))}</td><td>{e(str(w.get('train_rows')))} / "
                          f"{e(str(w.get('test_rows')))}</td></tr>" for w in windows) + "</table></div>"]
        out += [f"<p>{e(counting_line(r))}</p>"] if counting_line(r) else []
    la = r.get("lookahead") if isinstance(r.get("lookahead"), dict) else None
    if la:                                                # markdown's section, as a table
        text = lookahead_section(r)
        runs = [x for x in la.get("runs") or [] if isinstance(x, dict)] if isinstance(la.get("runs"), list) else []
        out += [f"<h2>{e(text[0][3:])}</h2>", f"<p>{e(text[2].replace('`', ''))}</p>"]
        if runs:
            out += ["<div class=scroll><table><tr><th></th><th>rerun</th><th>data before</th><th>data rows</th>"
                    "<th>rows compared</th><th>differences</th></tr>"
                    + "".join(f"<tr><td class='{LOOK_CLASS.get(x.get('status'), 'muted')}'>"
                              f"{e(LOOK_STATUS.get(x.get('status'), str(x.get('status'))))}</td>"
                              f"<td>{e(str(x.get('k')))}/{e(str(x.get('n')))}</td><td>{e(str(x.get('before')))}</td>"
                              f"<td>{e(str(x.get('data_rows')))}</td><td>{e(str(x.get('rows_compared')))}</td>"
                              f"<td>{e(str(x.get('differences')))}</td></tr>" for x in runs) + "</table></div>"]
        if la.get("finding"):
            out.append(f"<p class={'passed' if la.get('ok') else 'failed'}>{e(_line(la['finding']))}</p>")
    rows = []
    for s in r.get("steps", []):
        checks = s.get("expectations") or []
        gpu = s.get("gpu") or {}
        rows.append(f"<tr><td class='{e(s['status'])}'>{e(_icon(s['status']))}</td><td>{e(s['name'])}</td>"
                    f"<td>{s.get('minutes', 0):.2f}</td><td>{sum(c['ok'] for c in checks)}/{len(checks)}</td>"
                    f"<td>{e(str(gpu.get('busy_pct')) + '%' if gpu else '-')}</td></tr>")
    rows += [f"<tr><td class=muted>SKIPPED</td><td>{e(n)}</td><td></td><td></td><td></td></tr>" for n in r.get("skipped", [])]
    out += ["<h2>Steps</h2><div class=scroll><table><tr><th></th><th>step</th><th>minutes</th><th>checks</th>"
            "<th>GPU busy</th></tr>" + "".join(rows) + "</table></div>"]
    checks = [(s["name"], c) for s in r.get("steps", []) for c in s.get("expectations") or []]
    if checks:
        out += ["<h2>Checks</h2><div class=scroll><table><tr><th></th><th>step</th><th>check</th><th>found</th></tr>"
                + "".join(f"<tr><td class='{'passed' if c['ok'] else 'failed'}'>{'ok' if c['ok'] else 'missed'}</td>"
                          f"<td>{e(n)}</td><td>{e(c['check'])}</td><td>{e(c['detail'])}</td></tr>" for n, c in checks)
                + "</table></div>"]
    out.append(_strategy(r, run_dir, "html"))
    import hashlib
    shown, seen, budget_left = [], set(), IMAGES_TOTAL
    for s in r.get("steps", []):
        names = list(s.get("images", [])) + list((s.get("web") or {}).get("screenshots", []))
        names += [f"artifacts/{a}" for a in s.get("artifacts", []) if "(not copied" not in a]
        for name in names:
            f = run_dir / s.get("dir", "") / name
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(f.suffix.lower())
            if not mime or not inside(run_dir, f) or f.stat().st_size > min(IMAGE_LIMIT, budget_left):
                continue
            raw = f.read_bytes()
            if hashlib.sha256(raw).digest() in seen:                 # a plot shown in a cell and saved as a file
                continue
            seen.add(hashlib.sha256(raw).digest())
            budget_left -= len(raw)
            data = base64.b64encode(raw).decode()
            shown.append(f"<figure><img alt='{e(name)}' src='data:{mime};base64,{data}'><figcaption>{e(s['name'])}: "
                         f"{e(name)}</figcaption></figure>")
    if shown:
        out += ["<h2>Evidence to look at</h2>"] + shown
    files = r.get("evidence") or []
    if files:
        out += [f"<details><summary>{len(files)} evidence files (sizes and sha256)</summary><div class=scroll><table>"
                "<tr><th>file</th><th>bytes</th><th>sha256</th></tr>"
                + "".join(f"<tr><td><a href='{e(x['path'])}'>{e(x['path'])}</a></td><td>{e(str(x['bytes']))}</td>"
                          f"<td><code>{e(x['sha256'][:16])}</code></td></tr>" for x in files) + "</table></div></details>"]
    if src or env:
        env = {k: v for k, v in env.items() if k != "package_versions"}      # for testbench diff; too long for here
        out += ["<h2>Provenance</h2><pre>" + e(json.dumps({"source": src, "environment": env,
                                                           "evidence_sha256": r.get("evidence_sha256")}, indent=1)) + "</pre>"]
    return "\n".join(out)


def render_junit(report: dict) -> str:
    """The run as JUnit XML: a test suite for the plan, a test case per step; for CI test reports."""
    import xml.etree.ElementTree as ET
    r = report
    steps = r.get("steps", [])
    fails = sum(1 for s in steps if s["status"] == "failed")
    errors = sum(1 for s in steps if s["status"] in ("error", "timeout"))
    stamp = dt.datetime.fromtimestamp(r["started"]).isoformat(timespec="seconds") if r.get("started") else ""
    root = ET.Element("testsuites", name="agent-testbench", tests=str(len(steps) + len(r.get("skipped", []))),
                      failures=str(fails), errors=str(errors), time=f"{(r.get('minutes') or 0) * 60:.1f}")
    suite = ET.SubElement(root, "testsuite", name=f"{r.get('plan')} on {r.get('backend')}", timestamp=stamp,
                          tests=root.get("tests"), failures=str(fails), errors=str(errors),
                          skipped=str(len(r.get("skipped", []))), time=root.get("time"))
    props = ET.SubElement(suite, "properties")
    src = r.get("source") or {}
    for k, v in (("run_id", r.get("run_id")), ("status", r.get("status")), ("cost_usd", r.get("cost_usd")),
                 ("commit", src.get("commit")), ("dirty", src.get("dirty")), ("evidence_sha256", r.get("evidence_sha256"))):
        if v is not None:
            ET.SubElement(props, "property", name=k, value=str(v))
    for s in steps:
        case = ET.SubElement(suite, "testcase", classname=str(r.get("plan")), name=s["name"],
                             time=f"{(s.get('minutes') or 0) * 60:.1f}")
        err = s.get("error") or {}
        bad = [f"expected {c['check']}: {c['detail']}" for c in s.get("expectations") or [] if not c["ok"]]
        detail = "\n".join(([f"{err.get('ename')}: {err.get('evalue')}" + (f" (at {err['line']})" if err.get("line") else "")]
                            if err else []) + bad + ([err["traceback_tail"]] if err.get("traceback_tail") else []))
        if s["status"] == "failed":
            ET.SubElement(case, "failure", message=(bad or [str(err.get("evalue", "failed"))])[0][:500],
                          type=str(err.get("ename") or "ExpectationFailed")).text = detail
        elif s["status"] in ("error", "timeout"):
            ET.SubElement(case, "error", message=str(err.get("evalue") or s["status"])[:500],
                          type=str(err.get("ename") or s["status"])).text = detail
        if s.get("stdout_tail"):
            ET.SubElement(case, "system-out").text = s["stdout_tail"]
    for name in r.get("skipped", []):
        ET.SubElement(ET.SubElement(suite, "testcase", classname=str(r.get("plan")), name=name), "skipped",
                      message="an earlier step did not pass")
    for key, name in (("lookahead", "look-ahead"), ("model_cutoff", "model cutoff")):    # the plan's own checks
        found = r.get(key)
        if isinstance(found, dict) and found.get("finding"):
            case = ET.SubElement(suite, "testcase", classname=str(r.get("plan")), name=name, time="0")
            if not found.get("ok", True):
                ET.SubElement(case, "failure", message=str(found["finding"])[:500],
                              type="LookAhead" if key == "lookahead" else "ModelCutoff").text = str(found["finding"])
            else:
                ET.SubElement(case, "system-out").text = str(found["finding"])
            for el in (root, suite):
                el.set("tests", str(int(el.get("tests")) + 1))
                el.set("failures", str(int(el.get("failures")) + (not found.get("ok", True))))
    for el in root.iter():                     # colored output (ESC) and other control characters are not XML
        el.text = el.text and NOT_XML.sub("", el.text)
        for k, v in el.attrib.items():
            el.set(k, NOT_XML.sub("", v))
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def history(reports: list[dict]) -> dict:
    """A plan over time, oldest first: each run's outcome and every number its checks found."""
    rows, series = [], {}
    for rep in reports:
        m = _metrics(rep)
        src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
        minutes = rep.get("minutes") if isinstance(rep.get("minutes"), (int, float)) else None    # an unreadable one
        rows.append({"run_id": rep.get("run_id"), "started": rep.get("started"), "status": rep.get("status"),
                     "minutes": minutes, "cost_usd": rep.get("cost_usd"), "backend": rep.get("backend"),
                     "commit": str(src.get("commit") or "")[:9] + ("+" if src.get("dirty") else ""), "metrics": m})
        for k in m:
            series.setdefault(k, [])
    for k in series:
        series[k] = [row["metrics"].get(k) for row in rows]
    return {"runs": rows, "metrics": series}


def render_history(plan: str, h: dict) -> str:
    rows = h["runs"]
    if not rows:
        return f"plan {plan}: no finished runs yet"
    passed = sum(1 for r in rows if r["status"] == "passed")
    lines = [f"plan {plan}: {len(rows)} run(s), {passed} passed; latest {rows[-1]['run_id']} {rows[-1]['status']}", ""]
    for r in rows:
        cost = f"${r['cost_usd']:.3f}" if isinstance(r.get("cost_usd"), (int, float)) else "?"
        lines.append(f"  {r['run_id']}  {r['status']:<9} {r.get('backend') or '':<6} {r.get('minutes') or 0:>6.2f} min  "
                     f"{cost:>8}  {r['commit'] or '-'}")
    if h["metrics"]:
        lines += ["", "checked numbers, oldest to newest (- where a run did not reach the check):"]
        for k, vals in h["metrics"].items():
            lines.append(f"  {k}: " + " -> ".join("-" if v is None else f"{v:g}" for v in vals))
    return "\n".join(lines)
