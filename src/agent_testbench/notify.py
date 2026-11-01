"""Notifications: a JSON POST to a webhook when a run ends, a loop ends, or a launch waits for a person's approval,
so whoever left an agent working overnight hears about it without looking.

    notify:
      webhook_env: TESTBENCH_WEBHOOK                  # the URL in an environment variable (or webhook: https://...)
      on: [run_end, loop_end, approval_needed]         # which events (default: these three; watch_run is opt-in)

The body is Slack-compatible ({"text": "..."}) and carries the same facts as fields (event, project, plan or loop,
status, numbers, cost, run, report), so any service that takes a JSON POST can use it. Delivery never breaks or
holds up the work: one attempt, bounded by TIMEOUT seconds in all, no retries, and every attempt is written to
.testbench/notify.log instead of raised. Everything is sent from this machine, also for remote runs (their driver
closes them out here). The URL's path is a secret (for Slack it is the key), so only its scheme and host are ever
shown, here and in messages.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config as C

EVENTS = C.NOTIFY_EVENTS
TIMEOUT = 5.0                     # seconds for one delivery, all of it (connecting, sending, the answer)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
HOLD_ENV = "TESTBENCH_HOLD_APPROVAL_NOTICE"    # an empty file: approval_needed goes there, for the MCP server to send
                                               # only if the person it asks directly does not answer soon


def log_path(root: Path) -> Path:
    return Path(root) / ".testbench" / "notify.log"


def where(url: str) -> str:
    """The part of a webhook URL that is safe to show: scheme and host (and port), never the path or a password."""
    try:
        p = urllib.parse.urlsplit(str(url))
        host = p.hostname or "?"
        port = f":{p.port}" if p.port else ""
    except ValueError:
        return "an unreadable URL"
    return f"{p.scheme or '?'}://{'[' + host + ']' if ':' in host else host}{port}"


def _scrub(text: str, url: str) -> str:
    """`text` with the URL, its path and its query taken out."""
    p = urllib.parse.urlsplit(url)
    for secret in sorted({url, url.split("://", 1)[-1], p.path + ("?" + p.query if p.query else ""), p.query,
                          p.password or ""}, key=len, reverse=True):
        if len(secret) > 1:
            text = text.replace(secret, "/...")
    return text


def target(cfg: dict) -> tuple[str | None, str]:
    """(the URL, or None when it cannot be had; where it comes from or why it cannot be had)."""
    n = cfg.get("notify") or {}
    if n.get("webhook_env"):
        url = os.environ.get(n["webhook_env"], "").strip()
        if not url:
            return None, (f"notify.webhook_env names {n['webhook_env']}, which is not set in this environment; set "
                          "it where testbench runs (the shell, the MCP server's environment, the CI secrets)")
        if not re.match(r"https?://", url):
            return None, f"${n['webhook_env']} does not hold an http:// or https:// URL"
        return url, f"${n['webhook_env']}"
    return n.get("webhook"), "notify.webhook in testbench.yaml"


def _post(url: str, body: dict, timeout: float) -> tuple[bool, str]:
    """One POST, never longer than `timeout` seconds in all (a thread, so a slow name lookup cannot hold it up)."""
    out: list = []

    def go():
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
            "Content-Type": "application/json", "User-Agent": f"agent-testbench/{__version__}"})
        local = urllib.parse.urlsplit(url).hostname in LOOPBACK         # a proxy cannot reach this machine's ports
        opener = urllib.request.build_opener(*([urllib.request.ProxyHandler({})] if local else []))
        try:
            with opener.open(req, timeout=timeout) as r:
                out.append((200 <= r.status < 300, f"HTTP {r.status}"))
        except urllib.error.HTTPError as exc:
            said = exc.read(200).decode(errors="replace").strip() if exc.fp else ""
            out.append((False, f"HTTP {exc.code} {exc.reason}" + (f": {said}" if said else "")))
        except Exception as exc:
            reason = getattr(exc, "reason", None) or exc
            if isinstance(reason, TimeoutError):           # urllib's own timeout, a moment before ours
                out.append((False, f"no answer within {timeout:g} s"))
            else:
                out.append((False, f"{type(exc).__name__}: {reason}"))

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout)
    if not out:
        return False, f"no answer within {timeout:g} s"
    ok, detail = out[0]
    return ok, _scrub(" ".join(str(detail).split())[:300], url)


def _log(root: Path, line: str) -> None:
    try:
        p = log_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def send(root: Path, cfg: dict | None, event: str, text: str, fields: dict,
         force: bool = False) -> tuple[bool | None, str]:
    """Post one event if testbench.yaml asks for it (force: whatever `on:` says). Never raises. (None, why) when
    nothing was sent because nothing is set up; (True/False, what happened) otherwise, also in notify.log."""
    url = None
    try:
        if cfg is None:
            cfg = C.load(root)
        n = cfg.get("notify")
        if not n or (not force and event not in n["on"]):
            return None, "notifications are not set up for this event"
        url, source = target(cfg)
        if not url:
            _log(root, f"{event}: nothing sent - {source}")
            return False, f"not sent: {source}"
        body = {"text": f"agent-testbench, {cfg['project']}: {text}", "event": event, "project": cfg["project"],
                "machine": socket.gethostname(), "time": dt.datetime.now().isoformat(timespec="seconds"), **fields}
        ok, detail = _post(url, body, TIMEOUT)
        what = f"to {where(url)} ({source}): " + ("accepted" if ok else "not accepted") + f" ({detail})"
        _log(root, f"{event} {what}")
        return ok, what
    except Exception as exc:                             # a notification must never break what it reports on
        why = f"not sent ({type(exc).__name__}: {exc})"[:300]
        why = _scrub(why, url) if url else why
        _log(root, f"{event}: {why}")
        return False, why


def rel(path: Path, root: Path) -> str:
    """A path as the person finds it from the project folder."""
    try:
        return Path(path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return str(path)


def _money(v) -> str:
    return f"${v:.3f}" if isinstance(v, (int, float)) else "$?"


def run_end(root: Path, run_dir: Path, rep: dict | None = None, cfg: dict | None = None) -> None:
    """A run finished (passed, failed, cancelled, error): what it proved, what it cost, where its report is. A run
    `testbench watch` started is told only as watch_run, and only when its result differs from the run before it:
    a watch runs on every save, and a message for each would be noise."""
    from . import report as R, runs
    try:
        state = runs.read_state(run_dir)
        rep = rep or R.load(run_dir) or {}
        status = state.get("status") or rep.get("status")
        event, origin = "run_end", state.get("origin") or {}
        if origin.get("watch"):
            before = (rep.get("delta") or {}).get("previous_status")
            if status == "cancelled" or before is None or (before == "passed") == (status == "passed"):
                return
            event = "watch_run"
        numbers = {c["check"]: n for s in rep.get("steps", []) for c in s.get("expectations") or []
                   if (n := R.check_number(c)) is not None}
        failed = [f"{s['name']}: {c['check']} - {c['detail']}" for s in rep.get("steps", []) if s["status"] != "passed"
                  for c in s.get("expectations") or [] if not c["ok"]]
        errors = [f"{s['name']}: {s['error'].get('ename')}: {str(s['error'].get('evalue', ''))[:200]}"
                  for s in rep.get("steps", []) if s.get("error")]
        why = rep.get("stopped_because") or state.get("error")
        hw = (state.get("backend") or "?") + (f" {state['gpu']}" if state.get("gpu") else "")
        cost = rep.get("cost_usd", state.get("cost_usd"))
        report = run_dir / "report.md"
        text = (f"run {run_dir.name} of plan {state.get('plan')} on {hw} {str(status).upper()}"
                + ((f" (loop {origin['loop']}, " + ("the baseline)" if origin.get("try") == 0
                                                      else f"try {origin.get('try')})")) if origin.get("loop") else "")
                + (f" (testbench watch; the run before it {(rep.get('delta') or {}).get('previous_status')})"
                   if event == "watch_run" else "")
                + f" after {rep.get('minutes', state.get('minutes', '?'))} min, about {_money(cost)}."
                + (f" Stopped because {why}." if why else "")
                + "".join(f" {x}." for x in (errors + failed)[:3])
                + (" Checked: " + "; ".join(f"{k} (found {v:g})" for k, v in list(numbers.items())[:6]) + "."
                   if numbers else "")
                + (f" Report: {rel(report, root)}" if report.exists() else ""))
        send(root, cfg, event, text, {
            "plan": state.get("plan"), "status": status, "run_id": run_dir.name, "backend": state.get("backend"),
            "gpu": state.get("gpu"), "minutes": rep.get("minutes", state.get("minutes")), "cost_usd": cost,
            "numbers": numbers, "failed": (errors + failed)[:10], "stopped_because": why,
            "loop": origin.get("loop"), "loop_try": origin.get("try"),
            **({"watch": origin["watch"], "previous_status": (rep.get("delta") or {}).get("previous_status")}
               if event == "watch_run" else {}),
            "report": rel(report, root) if report.exists() else None})
    except Exception as exc:
        _log(root, f"run_end: nothing sent - {type(exc).__name__}: {exc}"[:400])


def approval_needed(root: Path, cfg: dict, message: str, est: dict, how: str) -> None:
    """A launch was refused until a person approves it: what, on what hardware, the worst case, how to say yes.
    Other refusals (over a hard limit) are not a question for the person, so they are not sent. When the MCP server
    is about to ask the person directly (HOLD_ENV), it is held for the server, which sends it only if that question
    goes unanswered for a while (or cannot be asked): a person who answered at once needs no message."""
    if not str(message).startswith("Needs approval"):
        return
    hw = (est.get("backend") or "?") + (f" {est['gpu']}" if est.get("gpu") else "")
    worst = est.get("worst_case_usd")
    said = str(message).split(":", 1)[1].split(" Ask a person", 1)[0].strip()
    text = (f"needs your approval: {said} On {hw}. The agent was told to wait for a person's yes: tell it yes to let "
            f"it go ahead (`{how}`), or no.")
    fields = {"what": est.get("what"), "plan": est.get("plan"), "backend": est.get("backend"), "gpu": est.get("gpu"),
              "status": "waiting for approval", "worst_case_usd": worst, "max_minutes": est.get("max_minutes"),
              "message": message, "approve_with": how}
    held = os.environ.get(HOLD_ENV)
    if held and Path(held).is_file() and not Path(held).stat().st_size:
        try:                                             # the MCP server asks the person first; it sends this if
            Path(held).write_text(json.dumps({"root": str(root), "text": text, "fields": fields}))  # nobody answers
            return
        except OSError:
            pass
    send(root, cfg, "approval_needed", text, fields)


def send_held(held: Path, note: str = "") -> tuple[bool | None, str]:
    """Send the approval_needed a launch left in `held` (see HOLD_ENV), with `note` after its text. Never raises."""
    try:
        data = json.loads(Path(held).read_text() or "null")
        root, text, fields = Path(data["root"]), str(data["text"]), dict(data["fields"])
    except (OSError, ValueError, TypeError, KeyError):
        return None, "no approval was needed"
    return send(root, None, "approval_needed", text + (f" {note}" if note else ""), fields)
