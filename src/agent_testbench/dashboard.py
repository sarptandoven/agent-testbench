"""testbench ui --serve: the project live at http://127.0.0.1:8770/, for the person watching an agent work.

    testbench ui --serve [--port 8770] [--open]

The overview has today's spend against the project's and this machine's limits, the runs going now (their step,
their time against their limits, what they have cost so far), the live sessions, the loop (tries, best, stopping
rules) and the recent runs. A run's page follows its progress and its step's log as they grow, then shows its
report with the images. The static pages `testbench ui` writes are served too, at /ui/index.html. Pages ask small
endpoints for news every POLL seconds; only the standard library (http.server, a thread per request).

Read-only on purpose: nothing here launches, approves, cancels or spends - an agent can reach the port with curl,
and an approval is a person's. It listens on 127.0.0.1 only and answers only requests whose Host header is
127.0.0.1 or localhost, so a web page cannot reach it by DNS rebinding. Files come from the project's .testbench
folder only, and only the static pages, reports and step evidence: never state.json, plan.json (step env values),
ledgers, testbench's own logs or session folders (kernel keys). A webhook is shown as its scheme and host.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import budget, config as C, loop as L, notify, report as R, runs, sessions as S, ui

e = html.escape
PORT = 8770
HOSTS = ("127.0.0.1", "localhost")
POLL = 1.5                    # seconds between a page's updates
LOG_TAIL = 64 * 1024          # how much of a log a page starts with
LOG_CHUNK = 512 * 1024        # the most of a log one update sends
RECENT, SCAN = 15, 300        # runs listed; runs looked at for the ones going now (the latest)
GOING = ("queued", "running")
HTML, PLAIN, JSON = "text/html; charset=utf-8", "text/plain; charset=utf-8", "application/json"
REPORTS = ("report.html", "report.md", "report.json", "tearsheet.html")      # a run's report files it serves
IMAGES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
TEXTS = {".txt", ".log", ".md", ".json", ".jsonl", ".csv", ".tsv", ".py", ".ipynb", ".yaml", ".yml", ".xml", ".svg",
         ".out", ".err"}
PAGE_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
FILE_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'none'"
READ_ONLY = ("Read-only: nothing on these pages launches, approves, cancels or spends. A person approves on the command "
             "line (<code>--approve</code>); <code>testbench cancel RUN</code> stops a run, "
             "<code>testbench session stop</code> a session.")
NOT_SERVED = ("Not here. This dashboard serves its pages (/ and /runs/RUN/), the static pages (/ui/index.html), "
              "reports and step evidence from the project's .testbench folder - never state, plans, ledgers or "
              "session folders.\n")
CSS = """
nav{display:flex;gap:16px;flex-wrap:wrap;align-items:baseline;font-size:14px;margin-bottom:16px}
.note{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--card);font-size:13px;
color:var(--muted);margin:24px 0 0}.small{font-size:12px;color:var(--muted);overflow-wrap:anywhere}meter{width:110px;height:10px;
vertical-align:middle;margin-left:6px}.log{max-height:65vh;overflow:auto}.lost,.LOST{color:var(--fail);font-weight:600}
.queued{color:var(--warn);font-weight:600}.warn{color:var(--warn)}#conn{color:var(--fail);font-size:13px}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--pass);margin-right:8px;
vertical-align:middle;animation:beat 1.6s ease-in-out infinite}@keyframes beat{50%{opacity:.2}}
@media (prefers-reduced-motion:reduce){.dot{animation:none}}
"""
SCRIPT = """\
"use strict";
(function () {
  var live = document.getElementById("live");
  if (!live) return;
  var run = live.dataset.kind === "run", every = Number(live.dataset.every) || 1500;
  var log = document.getElementById("log"), title = document.getElementById("logtitle");
  var conn = document.getElementById("conn"), step = "", offset = -1;
  function again() { setTimeout(tick, every); }
  function tick() {
    var url = live.dataset.src + (run ? "?step=" + encodeURIComponent(step) + "&offset=" + offset : "");
    fetch(url, {cache: "no-store"}).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return run ? r.json() : r.text();
    }).then(function (d) {
      conn.textContent = "";
      if (!run) { live.innerHTML = d; return again(); }
      live.innerHTML = d.head;
      if (d.step !== step) { log.textContent = ""; step = d.step; }
      title.textContent = d.label;
      if (d.text) {
        var end = log.scrollTop + log.clientHeight >= log.scrollHeight - 40;
        log.appendChild(document.createTextNode(d.text));
        if (log.textContent.length > 600000) log.textContent = log.textContent.slice(-400000);
        if (end) log.scrollTop = log.scrollHeight;
      }
      offset = d.offset;
      if (d.done) { location.reload(); return; }
      again();
    }).catch(function (err) {
      conn.textContent = "No answer from the dashboard (" + err.message + "); trying again. If it was stopped, " +
        "`testbench ui --serve` starts it again.";
      again();
    });
  }
  if (run) tick(); else again();
})();
"""


def _n(v) -> float | None:
    return float(v) if C.number(v) else None


def _g(v) -> str:
    return f"{v:g}" if C.number(v) else "?"


def _money(v) -> str:
    return f"${v:.3f}" if C.number(v) else "?"


def _q(name: str) -> str:
    """A name as one path segment of a link, ready for an attribute."""
    return e(urllib.parse.quote(str(name), safe=""))


def _hw(s: dict) -> str:
    return str(s.get("backend") or "?") + (f" {s['gpu']}" if s.get("gpu") else "")


def _when(t) -> str:
    try:
        return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if C.number(t) else ""
    except (ValueError, OverflowError, OSError):
        return ""


def _meter(value, most) -> str:
    if not (C.number(value) and C.number(most, positive=True)):
        return ""
    return (f"<meter min=0 max={most:g} low={most * 0.7:g} high={most * 0.9:g} optimum=0 "
            f"value={min(value, most):g}></meter>")


def _json(obj) -> str:
    """JSON with <, > and & as escapes, so no text in it can read as markup anywhere."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _read(p: Path) -> dict:
    try:
        s = json.loads(Path(p).read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    return s if isinstance(s, dict) else {}


def _status(s: dict) -> str:
    """A run's status; one whose driver is gone without finishing it is lost (a run in the foreground has none)."""
    st, pid = str(s.get("status") or "?"), s.get("driver_pid")
    if st in GOING and isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2 ** 31 and not runs.alive(pid):
        return "lost"
    return st


def _whole(b: bytes) -> int:
    """How much of `b` ends on a whole UTF-8 character (a log read while it is written can stop inside one)."""
    for back in range(1, min(4, len(b)) + 1):
        c = b[-back]
        if c < 0x80:
            return len(b)
        if c >= 0xC0:
            return len(b) if back >= (2 if c < 0xE0 else 3 if c < 0xF0 else 4) else len(b) - back
    return len(b)


class Dashboard:
    """What the pages are made from: the project folder and its testbench.yaml, read again when it changes (one
    that stops loading leaves the last good one in use, and the overview says so)."""

    def __init__(self, root: Path, cfg: dict | None = None):
        self.root = Path(root).resolve()
        self.tb = self.root / ".testbench"
        self.cfg, self.cfg_error, self._mtime, self._built = cfg, None, None, 0.0
        self._lock, self._build_lock = threading.Lock(), threading.Lock()
        if cfg is None:
            self.cfg = C.load(self.root)
        self._mtime = self._stamp()

    def _stamp(self):
        try:
            return (self.root / C.CONFIG_NAME).stat().st_mtime_ns
        except OSError:
            return None

    def config(self) -> dict:
        with self._lock:
            stamp = self._stamp()
            if stamp != self._mtime:
                self._mtime = stamp
                try:
                    self.cfg, self.cfg_error = C.load(self.root), None
                except C.ConfigError as exc:
                    self.cfg_error = str(exc)
            return self.cfg

    def static_index(self) -> None:
        """`testbench ui`'s pages, rebuilt at most every few seconds."""
        with self._build_lock:
            if time.time() - self._built > 5:
                ui.build(self.root, self.config())
                self._built = time.time()

    def run(self, name: str) -> Path | None:
        return next((p for p in runs.all_runs(self.root) if p.name == name), None)

    def file(self, seg: list[str]) -> Path | None:
        """A file the dashboard may serve, inside .testbench: a static page, a report or a step's evidence. The
        path is checked as asked and again as it resolves, so a link cannot lead to state.json or out of the
        folder."""
        def allowed(parts) -> bool:
            if any(not p or p.startswith(".") or any(c in p for c in "/\\:\0") for p in parts):
                return False
            n = len(parts)
            return n >= 2 and ((parts[0] == "ui" and n == 2)
                               or (parts[0] == "runs" and n == 3 and parts[2] in REPORTS)
                               or (parts[0] == "runs" and n >= 4 and parts[2] == "steps")
                               or (parts[0] == "loops" and n == 3 and parts[2] in ("report.html", "report.md")))
        if not allowed(seg):
            return None
        try:
            top = self.tb.resolve(strict=True)
            real = self.tb.joinpath(*seg).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            return None
        if not real.is_relative_to(top) or not real.is_file() or not allowed(list(real.relative_to(top).parts)):
            return None
        return real


# ---- the overview ------------------------------------------------------------------------------------------------

def _spend(root: Path, cfg: dict) -> str:
    lim = budget.limits(cfg)
    today, day = budget.spent_today(root), float(cfg["budget"]["max_day_usd"])
    cards = [("this project today", f"{_money(today)} of ${day:.2f}{_meter(today, day)}", "budget.max_day_usd")]
    machine_day = budget.user_limits().get("max_day_usd")
    if machine_day is None:
        cards.append(("this machine today, every project", "no day limit", f"{budget.limits_path()} sets none"))
    else:
        everywhere = budget.spent_today_everywhere()
        cards.append(("this machine today, every project", f"{_money(everywhere)} of ${machine_day:.2f}"
                      f"{_meter(everywhere, machine_day)}", f"max_day_usd in {budget.limits_path()}"))
    (most, most_where), (ask, ask_where) = lim["max_run_usd"], lim["ask_above_usd"]
    cards.append(("each run or session", f"refused above ${most:.2f}; a person approves above ${ask:.2f}",
                  f"{most_where}; {ask_where}"))
    if not cfg.get("notify"):
        cards.append(("notifications", "none", "notify: in testbench.yaml sends a message when a run or loop ends"))
    elif list(notify.chosen(cfg["notify"])) == ["webhook"]:
        url, source = notify.target(cfg)
        cards.append(("notifications", e(f"to {notify.where(url)}") if url else "not set up here",
                      f"{source}; on {', '.join(cfg['notify']['on'])}" if url else source))
    else:                                            # the desktop or plug-in notifiers: where each sends, or why not
        found = notify.places(cfg)
        ready = [f"to {shown}" for _, shown, missing in found if not missing]
        cards.append(("notifications", e("; ".join(ready)) if ready else "not set up here",
                      "; ".join([missing for *_, missing in found if missing] + [f"on {', '.join(cfg['notify']['on'])}"])))
    return ("<h2>Spend today</h2><div class=facts>"
            + "".join(f"<div><b>{e(k)}</b>{v}<br><span class=small>{e(w)}</span></div>" for k, v, w in cards)
            + "</div><div class=small>Committed: what finished runs and sessions cost, and the worst case of those "
              "still going (estimates). A launch over a limit is refused before it starts.</div>")


def _now(run_dir: Path, s: dict) -> dict:
    """A run as it is now: its status, its steps (names, kinds and limits from plan.json - never its env values),
    the step going, its time against its limits, and what it has cost so far (an estimate)."""
    now = time.time()
    plan = _read(run_dir / "plan.json")
    steps = [{"name": str(x.get("name")), "kind": str(x.get("kind") or ""), "limit": _n(x.get("max_minutes"))}
             for x in plan.get("steps") or [] if isinstance(x, dict)] if isinstance(plan.get("steps"), list) else []
    for ev in runs.progress(run_dir):
        i = ev.get("index") if isinstance(ev, dict) else None
        if not isinstance(i, int) or isinstance(i, bool) or not 0 < i <= 1000:
            continue
        while len(steps) < i:
            steps.append({"name": str(ev.get("step")), "kind": "", "limit": None})
        if ev.get("event") == "step_start":
            steps[i - 1].update(status="running", began=_n(ev.get("t")))
        elif ev.get("event") == "step_end":
            steps[i - 1].update(status=str(ev.get("status")), minutes=_n(ev.get("minutes")))
    status = _status(s)
    began = _n(s.get("started")) or _n(s.get("created"))
    going = status in GOING
    minutes = (now - began) / 60 if going and began else _n(s.get("minutes"))
    current = next(((i, x) for i, x in reversed(list(enumerate(steps, 1))) if x.get("status") == "running"), None) \
        if status == "running" else None
    if current and current[1].get("began"):
        current[1]["minutes"] = (now - current[1]["began"]) / 60
    limit = sum(x["limit"] for x in steps if x["limit"] is not None) if steps else None
    rate, worst = _n(s.get("usd_per_minute")) or 0.0, _n(s.get("worst_case_usd"))
    if going and not rate and not worst:
        cost = "free"
    elif going:
        cost = f"about {_money(rate * (minutes or 0))} so far" + (f" (worst case ${worst:.2f})" if worst is not None else "")
    else:
        cost = _money(s.get("cost_usd"))
    return {"status": status, "steps": steps, "current": current, "minutes": minutes, "limit": limit, "cost": cost}


def _step_cell(n: dict) -> str:
    if n["status"] == "queued":
        return "starting"
    if not n["current"]:
        return "-" if n["status"] != "lost" else "its driver exited without finishing"
    i, x = n["current"]
    return (f"step {i} of {len(n['steps'])}: <strong>{e(x['name'])}</strong><br>{_g_min(x.get('minutes'))} of "
            f"{_g(x.get('limit'))} min{_meter(x.get('minutes'), x.get('limit'))}")


def _g_min(v) -> str:
    return f"{v:.1f}" if C.number(v) else "?"


def _time_cell(n: dict) -> str:
    return f"{_g_min(n['minutes'])} of {_g(n['limit'])} min{_meter(n['minutes'], n['limit'])}"


def _running(root: Path, cfg: dict) -> str:
    going = [(p, s) for p, s in ((p, _read(p / "state.json")) for p in runs.all_runs(root)[-SCAN:])
             if s.get("status") in GOING]
    if not going:
        return "<h2>Running now</h2><p class=muted>Nothing is running.</p>"
    rows = []
    for run_dir, s in reversed(going):
        n = _now(run_dir, s)
        origin = s.get("origin") if isinstance(s.get("origin"), dict) else {}
        rows.append(f"<tr><td class='{e(n['status'])}'>{e(R.ICON.get(n['status'], n['status'].upper()))}</td>"
                    f"<td><a href='/runs/{_q(run_dir.name)}/'>{e(run_dir.name)}</a></td>"
                    f"<td>{e(str(s.get('plan')))}"
                    + (f"<br><span class=small>loop try {e(str(origin.get('try')))}</span>" if origin.get("loop") else "")
                    + f"</td><td>{e(_hw(s))}</td><td>{_step_cell(n)}</td><td>{_time_cell(n)}</td>"
                    f"<td>{e(n['cost'])}</td></tr>")
    return ("<h2>Running now</h2><div class=scroll><table><tr><th></th><th>run</th><th>plan</th><th>where</th>"
            "<th>step</th><th>time</th><th>cost</th></tr>" + "".join(rows) + "</table></div>")


def _sessions(root: Path, cfg: dict) -> str:
    found = [(p.name, s) for p in S.all_sessions(root) if (s := _read(p / "state.json"))]
    if not found:
        return "<h2>Sessions</h2><p class=muted>No sessions.</p>"
    active = [x for x in found if x[1].get("status") in S.ACTIVE]
    ended = sorted((x for x in found if x not in active), key=lambda x: _n(x[1].get("stopped")) or 0)[-5:]
    now, rows = time.time(), []
    for name, s in active + ended[::-1]:
        st = str(s.get("status"))
        if st in S.ACTIVE:
            up = (now - (_n(s.get("started")) or _n(s.get("created")) or now)) / 60
            idle = (now - (_n(s.get("last_used")) or now)) / 60
            busy = now < (_n(s.get("busy_until")) or 0)
            most = _n(s.get("max_minutes"))
            when = (f"up {up:.1f} of {_g(most)} min{_meter(up, most)}<br>"
                    + ("busy: a command is running" if busy else
                       f"idle {idle:.1f} min (stops after {e(_g(s.get('idle_minutes')))})"))
            cost = f"about {_money((_n(s.get('usd_per_minute')) or 0) * up)} so far" \
                   + (f" (worst case ${s['worst_case_usd']:.2f})" if C.number(s.get("worst_case_usd")) else "")
        else:
            when, cost = e(str(s.get("stop_reason") or "")), _money(s.get("cost_usd"))
        rows.append(f"<tr><td class='{'running' if st in S.ACTIVE else 'muted'}'>{e(st)}</td><td><b>{e(name)}</b></td>"
                    f"<td>{e(_hw(s))}</td><td>{when}</td><td>{e(cost)}</td><td>{e(_g(s.get('exec_count', 0)))}</td></tr>")
    return ("<h2>Sessions</h2><div class=scroll><table><tr><th></th><th>session</th><th>where</th><th>time</th>"
            "<th>cost</th><th>execs</th></tr>" + "".join(rows) + "</table></div><div class=small>A session stops "
            "itself after its idle minutes unused or at its max minutes; remote machines are also stopped on their "
            "own side.</div>")


def _loop(root: Path, cfg: dict) -> str:
    d = L.loops_dir(root)
    found = [(p.parent.name, s) for p in (sorted(d.glob("*/state.json")) if d.is_dir() else []) if (s := _read(p))]
    if not found:
        return "<h2>Loop</h2><p class=muted>No loop here.</p>"
    lid, s = next((x for x in reversed(found) if x[1].get("status") == L.ACTIVE), found[-1])
    tries, best, now = s.get("tries") or [], s.get("best") or {}, time.time()
    active = s.get("status") == L.ACTIVE
    left = (_n(s.get("deadline")) or now) - now
    rules = L._rules(s)
    cards = [("best", e(L._fmt(best.get("value"))), L._best_at(s) if tries else "no try yet"),
             ("tries", f"{max(0, len(tries) - 1)}, {sum(1 for t in tries[1:] if t.get('kept'))} kept", "the baseline not counted"),
             ("spent", f"{_money(L.spent(s))} of ${s['budget_usd']:.2f}{_meter(L.spent(s), s['budget_usd'])}",
              "every run of the loop together"),
             ("time", f"{max(0.0, left) / 3600:.1f} h left" if active else e(f"stopped {_when(s.get('stopped'))}"),
              "--max-hours" if active else (L.ENDED.get(s.get("ended_by"), "") if s.get("ended_by") else "")),
             ("stopping rules", e(rules) if rules else "none", "" if rules else "--target, --patience, --max-tries")]
    out = [f"<h2>Loop <span class=small>{e(lid)}</span></h2>",
           f"<p><span class='{'running' if active else 'muted'}'>{e(str(s.get('status')))}</span> on plan "
           f"<code>{e(str(s.get('plan')))}</code>: {e(str(s.get('goal')))} {e(L._what(s))}, branch "
           f"<code>{e(str(s.get('branch')))}</code>"
           + (f" &middot; <a href='/loops/{_q(lid)}/report.md'>report</a>" if (d / lid / "report.md").is_file() else "")
           + "</p><div class=facts>"
           + "".join(f"<div><b>{e(k)}</b>{v}<br><span class=small>{e(w)}</span></div>" for k, v, w in cards) + "</div>"]
    halt = L._halt(s) if active else None
    if halt:
        out.append(f"<p class=warn>{e(halt[1])}</p>")
    pending = s.get("pending")
    if active and isinstance(pending, int) and 0 <= pending < len(tries):
        run_state = _read(runs.runs_dir(root) / str(tries[pending].get("run")) / "state.json")
        if run_state.get("status") in runs.DONE:
            out.append(f"<p class=muted>Try {pending}'s run has ended ({e(str(run_state['status']))}); "
                       "<code>testbench loop status</code> keeps or reverts it.</p>")
    rows = []
    for t in [t for t in tries if isinstance(t, dict)][::-1][:12]:
        mark = "baseline" if t.get("n") == 0 else "running" if t.get("status") == "running" else \
            "kept" if t.get("kept") else "reverted"
        css = {"kept": "passed", "running": "running", "reverted": "muted"}.get(mark, "")
        value = t.get("value")
        shown = L._fmt(value) if value is None or (isinstance(value, (int, float)) and not isinstance(value, bool)) else "?"
        cost = _money(t.get("cost_usd")) if t.get("cost_usd") is not None else f"up to {_money(t.get('worst_case_usd'))}"
        rows.append(f"<tr><td class='{css}'>{e(mark)}</td><td>{e(_g(t.get('n')))}</td><td>{e(shown)}</td>"
                    f"<td>{e(cost)}</td><td><a href='/runs/{_q(t.get('run'))}/'>{e(str(t.get('run')))}</a></td>"
                    f"<td>{e(L._desc(t) if isinstance(t.get('description'), str) else '')}</td></tr>")
    out.append(f"<div class=scroll><table><tr><th></th><th>try</th><th>{e(str(s.get('key')))}</th><th>cost</th>"
               "<th>run</th><th>what changed</th></tr>" + "".join(rows) + "</table></div>")
    return "".join(out)


def _recent(root: Path, cfg: dict) -> str:
    found = runs.all_runs(root)
    if not found:
        return "<h2>Recent runs</h2><p class=muted>No runs yet.</p>"
    rows = []
    for run_dir in reversed(found[-RECENT:]):
        s = _read(run_dir / "state.json")
        st = _status(s)
        cost = _money(s.get("cost_usd")) if C.number(s.get("cost_usd")) else \
            (f"up to ${s['worst_case_usd']:.2f}" if C.number(s.get("worst_case_usd")) else "")
        mins = _n(s.get("minutes"))
        rows.append(f"<tr><td class='{e(st)}'>{e(R.ICON.get(st, st.upper()))}</td>"
                    f"<td><a href='/runs/{_q(run_dir.name)}/'>{e(run_dir.name)}</a></td><td>{e(str(s.get('plan')))}</td>"
                    f"<td>{e(_hw(s))}</td><td>{e(_when(s.get('started') or s.get('created')))}</td>"
                    f"<td>{'' if mins is None else f'{mins:g}'}</td><td>{e(cost)}</td></tr>")
    return ("<h2>Recent runs</h2><div class=scroll><table><tr><th></th><th>run</th><th>plan</th><th>where</th>"
            "<th>started</th><th>minutes</th><th>cost</th></tr>" + "".join(rows) + "</table></div>"
            f"<p class=small>{len(found)} run(s) in all; <a href='/ui/index.html'>every plan and run</a> has the "
            "trend of each checked number.</p>")


SECTIONS = (("Spend today", _spend), ("Running now", _running), ("Sessions", _sessions), ("Loop", _loop),
            ("Recent runs", _recent))


def overview(d: Dashboard) -> str:
    cfg = d.config()
    out = [f"<h1><span class=dot></span>{e(cfg['project'])}</h1>",
           f"<div class=muted>{e(str(d.root))} &middot; updated {dt.datetime.now():%H:%M:%S}</div>"]
    if d.cfg_error:
        out.append(f"<p class=failed>testbench.yaml cannot be read now, so this uses it as it was: {e(d.cfg_error)}</p>")
    for name, part in SECTIONS:
        try:
            out.append(part(d.root, cfg))
        except Exception as exc:                       # one unreadable file must not take the page down
            out.append(f"<h2>{e(name)}</h2><p class=failed>Cannot be shown: {e(type(exc).__name__)}: {e(str(exc))}</p>")
    return "\n".join(out)


# ---- a run -------------------------------------------------------------------------------------------------------

def _run_head(run_dir: Path, s: dict, n: dict) -> str:
    st, name = n["status"], run_dir.name
    src = s.get("source") if isinstance(s.get("source"), dict) else {}
    facts = [("where", e(_hw(s))), ("started", e(_when(s.get("started") or s.get("created")))),
             ("time", _time_cell(n)), ("step", _step_cell(n)), ("cost", e(n["cost"])),
             ("code", e(str(src.get("commit") or "")[:12] + (" + uncommitted changes" if src.get("dirty") else ""))
              if src else "not in git")]
    rows = []
    for i, x in enumerate(n["steps"], 1):
        stx = x.get("status") or ("skipped" if st not in GOING else "waiting")
        rows.append(f"<tr><td class='{e(stx)}'>{e(R.ICON.get(stx, stx.upper()))}</td><td>{i}</td><td>{e(x['name'])}</td>"
                    f"<td>{e(x['kind'])}</td><td>{_g_min(x.get('minutes')) if x.get('status') else ''}</td>"
                    f"<td>{e(_g(x['limit']))}</td></tr>")
    stop = f" &middot; to stop it: <code>testbench cancel {e(name)}</code>" if st in GOING else ""
    lost = ("<p class=failed>Its driver process exited without finishing it. <code>testbench wait "
            f"{e(name)}</code> closes it out as an error; driver.log in the run folder says why.</p>") if st == "lost" else ""
    return (f"<h1><span class='badge {e(st)}'>{e(R.ICON.get(st, st.upper()))}</span> plan "
            f"<code>{e(str(s.get('plan')))}</code></h1><div class=muted>run <code>{e(name)}</code>{stop} &middot; "
            f"updated {dt.datetime.now():%H:%M:%S}</div>{lost}<div class=facts>"
            + "".join(f"<div><b>{e(k)}</b>{v}</div>" for k, v in facts) + "</div>"
            + ("<h2>Steps</h2><div class=scroll><table><tr><th></th><th>#</th><th>step</th><th>kind</th><th>minutes</th>"
               "<th>limit</th></tr>" + "".join(rows) + "</table></div>" if rows else ""))


def _finished(s: dict) -> bool:
    return _status(s) not in GOING


def run_live(run_dir: Path, step: str = "", offset: int = -1) -> dict:
    """What a run's page adds now: its header, and the log of the step going (or the last one) from `offset`."""
    s = _read(run_dir / "state.json")
    n = _now(run_dir, s)
    out = {"head": _run_head(run_dir, s, n), "done": _finished(s), "step": "", "label": "Output", "offset": 0,
           "text": ""}
    logs = sorted((run_dir / "steps").glob("*/log.txt")) if (run_dir / "steps").is_dir() else []
    if not logs:
        return out
    log = logs[-1]
    try:
        size = log.stat().st_size
        if log.parent.name != step or not 0 <= offset <= size:
            offset = -1
        text = ""
        with open(log, "rb") as f:
            if offset < 0:
                offset = max(0, size - LOG_TAIL)
                if offset:
                    f.seek(offset)
                    offset += len(f.readline(4096))              # start on a whole line
                    text = f"[the first {offset} bytes are not shown here; steps/{log.parent.name}/log.txt has them]\n"
            f.seek(offset)
            chunk = f.read(LOG_CHUNK)
    except OSError:
        return out
    chunk = chunk[:_whole(chunk)]
    index, steps = log.parent.name.split("-", 1)[0], n["steps"]
    label = f"Output of step {int(index)}: {steps[int(index) - 1]['name']}" \
        if index.isdigit() and 0 < int(index) <= len(steps) else f"Output of {log.parent.name}"
    out.update(step=log.parent.name, label=label, offset=offset + len(chunk),
               text=text + chunk.decode("utf-8", "replace"))
    return out


def run_page(run_dir: Path) -> tuple[str, str]:
    s = _read(run_dir / "state.json")
    title = f"{R.ICON.get(_status(s), _status(s).upper())} {s.get('plan')} {run_dir.name}"
    if not _finished(s):
        return title, (f"<div id=live data-kind=run data-src='/live/runs/{_q(run_dir.name)}' "
                       f"data-every={int(POLL * 1000)}>{_run_head(run_dir, s, _now(run_dir, s))}</div>"
                       "<h2 id=logtitle>Output</h2><pre id=log class=log></pre>")
    rep = R.load(run_dir)
    if ui._readable(rep):
        try:
            return title, (f"<p class=muted>Run <code>{e(run_dir.name)}</code> has ended; this is its report "
                           "(report.html in the run folder is the same).</p>" + R.html_body(rep, run_dir))
        except (ValueError, TypeError, KeyError, AttributeError, IndexError, OSError, OverflowError):
            pass
    tail = run_live(run_dir)
    return title, (tail["head"] + "<p class=muted>Its report.json cannot be read (cut short, or never written); "
                   f"here is the end of its last step's log.</p><h2>{e(tail['label'])}</h2>"
                   f"<pre class=log>{e(tail['text'])}</pre>")


# ---- the server --------------------------------------------------------------------------------------------------

def page(title: str, body: str) -> str:
    nav = ("<nav><a href='/'>overview</a><a href='/ui/index.html'>every plan and run</a>"
           "<span class=small>testbench ui --serve &middot; this machine only &middot; read-only</span></nav>")
    return R.page(title, nav + body + f"<p class=note>{READ_ONLY}</p><p id=conn></p>",
                  f"<style>{CSS}</style><script src='/dashboard.js' defer></script>")


def _local(host: str, port: int) -> bool:
    h = host.strip().lower()
    return h in HOSTS or h in {f"{x}:{port}" for x in HOSTS}


def route(d: Dashboard, target: str) -> tuple[int, str | bytes | Path, str, dict]:
    """(status, body, content type, headers) for a GET of `target`."""
    raw, _, query = target.partition("?")
    if not raw.startswith("/"):
        return 400, "Ask for a path on this server, such as /.\n", PLAIN, {}
    seg = [urllib.parse.unquote(p, errors="replace") for p in raw[1:].split("/")]
    if seg == [""]:
        cfg = d.config()
        return 200, page(f"testbench: {cfg['project']}", f"<div id=live data-kind=overview data-src='/live/overview' "
                                                         f"data-every={int(POLL * 1000)}>{overview(d)}</div>"), HTML, {}
    if seg == ["dashboard.js"]:
        return 200, SCRIPT, "text/javascript; charset=utf-8", {}
    if seg == ["live", "overview"]:
        return 200, overview(d), HTML, {}
    if len(seg) == 3 and seg[:2] == ["live", "runs"] and (run_dir := d.run(seg[2])):
        q = urllib.parse.parse_qs(query)
        try:
            offset = int(q.get("offset", ["-1"])[0])
        except ValueError:
            offset = -1
        return 200, _json(run_live(run_dir, q.get("step", [""])[0], offset)), JSON, {}
    if len(seg) in (2, 3) and seg[0] == "runs" and seg[2:] in ([], [""]) and (run_dir := d.run(seg[1])):
        if len(seg) == 2:
            return 302, "", PLAIN, {"Location": f"/runs/{urllib.parse.quote(seg[1], safe='')}/"}
        return (200, page(*run_page(run_dir)), HTML, {})
    if seg in (["ui"], ["ui", ""]):
        return 302, "", PLAIN, {"Location": "/ui/index.html"}
    if seg == ["ui", "index.html"]:
        d.static_index()
    f = d.file(seg)
    if f is None:
        return 404, NOT_SERVED, PLAIN, {}
    suffix, extra = f.suffix.lower(), {"Content-Security-Policy": FILE_CSP}
    if suffix in IMAGES:
        kind = IMAGES[suffix]
    elif suffix in (".html", ".htm"):
        kind = HTML
    elif suffix in TEXTS:
        kind = PLAIN
    else:
        kind = "application/octet-stream"
        extra["Content-Disposition"] = f"attachment; filename=\"{re.sub(r'[^A-Za-z0-9._-]', '_', f.name)}\""
    return 200, f, kind, extra


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "testbench-dashboard", ""
    timeout = 30

    def log_message(self, format, *args):              # pages poll every few seconds; nothing to say about it
        pass

    def do_GET(self):
        self._answer(True)

    def do_HEAD(self):
        self._answer(False)

    def _read_only(self):
        self._send(405, "This dashboard is read-only: it launches, approves, cancels and spends nothing. Use "
                        "testbench on the command line.\n", PLAIN, {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _read_only

    def _answer(self, body: bool):
        if not _local(self.headers.get("Host", ""), self.server.server_address[1]):
            return self._send(403, "Refused: this dashboard answers requests for 127.0.0.1 or localhost only (the "
                                   "Host header said otherwise). Open http://127.0.0.1:"
                                   f"{self.server.server_address[1]}/ on this machine.\n", PLAIN, body=body)
        try:
            code, payload, kind, extra = route(self.server.dash, self.path)
        except Exception as exc:
            code, payload, kind, extra = 500, f"The dashboard could not make this page: {type(exc).__name__}: {exc}\n", \
                PLAIN, {}
        self._send(code, payload, kind, extra, body)

    def _send(self, code: int, payload, kind: str, extra: dict | None = None, body: bool = True):
        try:
            size = payload.stat().st_size if isinstance(payload, Path) else None
            if size is None:
                payload = payload if isinstance(payload, bytes) else payload.encode("utf-8", "replace")
                size = len(payload)
            self.send_response(code)
            for k, v in {"Content-Type": kind, "Content-Length": str(size), "Cache-Control": "no-store",
                         "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Cross-Origin-Resource-Policy": "same-origin", "Content-Security-Policy": PAGE_CSP,
                         **(extra or {})}.items():
                self.send_header(k, v)
            self.end_headers()
            if not body:
                return
            if isinstance(payload, Path):                 # a log may grow meanwhile: send what was announced
                with open(payload, "rb") as f:
                    while size > 0 and (chunk := f.read(min(1 << 20, size))):
                        self.wfile.write(chunk)
                        size -= len(chunk)
            else:
                self.wfile.write(payload)
        except OSError:
            pass                                          # the browser went away, or the file did


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"             # on Windows it would let a second server share a port in use

    def server_bind(self):                            # HTTPServer's looks up this machine's name, which can be slow
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def make_server(root: Path, cfg: dict | None = None, port: int = PORT) -> Server:
    """The dashboard's server on 127.0.0.1:port (0: a free port), not yet serving."""
    dash = Dashboard(root, cfg)
    try:
        server = Server(("127.0.0.1", port), Handler)
    except (OSError, OverflowError) as exc:
        raise C.ConfigError(f"the dashboard cannot listen on 127.0.0.1:{port} ({getattr(exc, 'strerror', None) or exc}); "
                            "another program, perhaps another dashboard, may have that port. Pick another with "
                            "--port N.") from None
    server.dash = dash
    return server


def serve(root: Path, cfg: dict | None = None, port: int = PORT, open_browser: bool = False) -> int:
    server = make_server(root, cfg, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Dashboard for {server.dash.cfg['project']} at {url} - live, read-only, for this machine only. "
          "Ctrl-C stops it.", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("dashboard stopped", flush=True)
    finally:
        server.server_close()
    return 0
