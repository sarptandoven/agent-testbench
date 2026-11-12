"""Notifications: a JSON POST to a webhook when a run ends, a loop's stopping rule fires or the loop ends, or a
launch waits for a person's approval, so whoever left an agent working overnight hears about it without looking.

    notify:
      webhook_env: TESTBENCH_WEBHOOK                  # the URL in an environment variable (or webhook: https://...)
      desktop: true                                   # and/or a notification on this machine's screen
      on: [run_end, loop_rule, loop_end, approval_needed]    # the default; watch_run and loop_try are opt-in

The body is Slack-compatible ({"text": "..."}) and carries the same facts as fields (event, project, plan or loop,
status, numbers, cost, run, report), so any service that takes a JSON POST can use it. Delivery never breaks or
holds up the work: one attempt, bounded by TIMEOUT seconds in all, no retries, and every attempt is written to
.testbench/notify.log instead of raised. Everything is sent from this machine, also for remote runs (their driver
closes them out here). The URL's path is a secret (for Slack it is the key), so only its scheme and host are ever
shown, here and in messages.

Each place a message goes is a notifier (the `notifiers` plug-in kind: KIND at the end; Notifier says what one is).
The built-ins are the webhook (notify:'s own `webhook:` / `webhook_env:`) and the desktop; any other key of
`notify:` names an installed plug-in's, with its settings (docs/plugins.md#notifiers). Every notifier named gets
every event `on:` lists, all of them at once, each within the same TIMEOUT.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config as C, plugins as P

EVENTS = C.NOTIFY_EVENTS
TIMEOUT = 5.0                     # seconds for one delivery, all of it (connecting, sending, the answer)
GRACE = 1.0                       # and this much more before a notifier that overruns is left behind
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TOLD = "run_end.told"                         # in a run's folder once run_end (or watch_run) was dealt with
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


def _hide(text: str, opts) -> str:
    """`text` without any URL a notifier's settings hold or name (past its scheme and host), or the value of a
    variable they name."""
    for k, v in opts.items() if isinstance(opts, dict) else ():
        if not isinstance(v, str) or not v:
            continue
        for value in [v] + ([os.environ.get(v, "")] if str(k).endswith("_env") else []):
            try:
                if "://" in value:
                    text = _scrub(text, value)
                elif value is not v and len(value) >= 6:
                    text = text.replace(value, f"<${v}>")
            except ValueError:                           # a URL urllib cannot split
                text = text.replace(value, "<a URL>")
    return text


# ---- notifiers: what one is, and the two built-ins -----------------------------------------------------------------

class Notifier:
    """Where `notify:` tells people (the `notifiers` plug-in kind). A plug-in notifier is any object with these;
    subclassing this class is optional (it gives a notifier without settings: `NAME: true`).

        name      its key under `notify:` ("desktop"); the entry point's name when left empty. Not `on` or
                  `webhook_env`, which notify: uses itself
        example   settings that validate, for `testbench plugin test`; a value "{url}" there becomes the address of a
                  closed port, then of one that never answers, to see that send() keeps to its timeout and that
                  where() hides the URL's path and query

        validate(where, opts) -> dict
            The settings as written under `notify.NAME` (None or true for none) -> the settings the others get.
            Anything it cannot use raises config.ConfigError naming the field (`where` is "notify.NAME") and saying
            what to write instead. Secrets stay out of testbench.yaml: take an environment variable's NAME (a
            setting ending in _env), as `webhook_env` does.
        send(event, text, fields, opts, timeout) -> (ok, detail)
            Tell people about one event. `text` is the message ("agent-testbench, my-project: run ... PASSED ..."),
            `fields` the same facts as data (event, project, machine, time and the event's own, docs/notify.md).
            One attempt, at most `timeout` seconds in all, and it never raises: (True, "HTTP 200") when it was
            taken, else (False, what went wrong in a few words). It runs in a thread of its own, all notifiers at
            once, and one still going after the timeout and GRACE is left behind as "no answer".
        where(opts) -> str
            Where messages go, as notify.log and `testbench notify test` may show it: for a URL only its scheme and
            host (notify.where(url) does that), never its path, query or a password.
        missing(opts) -> str      optional
            What this machine lacks to send now (a variable that is not set, a command that is not installed), or
            "". When it says something, nothing is sent and notify.log says that instead.
    """
    name = ""
    example: dict = {}

    def validate(self, where: str, opts) -> dict:
        if opts in (None, True) or opts == {}:
            return {}
        raise C.ConfigError(f"`{where}` takes no settings: write `{self.name or where.rpartition('.')[2]}: true`, "
                            "or false to leave it out")

    def send(self, event: str, text: str, fields: dict, opts: dict, timeout: float) -> tuple[bool, str]:
        return False, f"the {self.name or '?'} notifier has no send(); its package is unfinished"

    def where(self, opts: dict) -> str:
        return self.name

    def missing(self, opts: dict) -> str:
        return ""

    def __repr__(self):
        return f"<notifier {self.name or '?'}>"


class Webhook(Notifier):
    """A JSON POST (Slack-compatible) to a URL. Its settings are notify:'s own `webhook:` (the URL) or
    `webhook_env:` (the NAME of a variable that holds it), as they were before notifiers were plug-ins."""
    name = "webhook"
    example = {"webhook": "{url}"}

    def validate(self, where: str, opts) -> dict:
        if not isinstance(opts, dict):
            raise C.ConfigError(f"`{where}` is a webhook's settings: webhook (the URL to POST to) or webhook_env (the "
                                f"NAME of an environment variable that holds it), e.g. {C.NOTIFY_EXAMPLE}")
        for k in opts:
            if k not in ("webhook", "webhook_env"):
                raise C.ConfigError(f"`{where}` has no {str(k)!r}; use webhook (or webhook_env), e.g. "
                                    + C.NOTIFY_EXAMPLE)
        url, env = opts.get("webhook"), opts.get("webhook_env")
        if bool(url) == bool(env):
            raise C.ConfigError(f"`{where}` needs one of `webhook:` (the URL to POST to) or `webhook_env:` (the name of "
                                "an environment variable that holds it, which keeps the URL's secret out of the file), "
                                "e.g. " + C.NOTIFY_EXAMPLE)
        if url and (not isinstance(url, str) or not re.match(r"https?://[^/\s]+", url.strip())):
            raise C.ConfigError(f"`{where}.webhook` is a URL that takes a JSON POST, starting with https:// (e.g. a "
                                "Slack incoming webhook); or put the URL in an environment variable and name it with "
                                f"`{where}.webhook_env: NAME`")
        if env and (not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env)):
            raise C.ConfigError(f"`{where}.webhook_env` is the NAME of an environment variable that holds the webhook "
                                "URL, e.g. TESTBENCH_WEBHOOK - not the URL itself (export TESTBENCH_WEBHOOK=https://... "
                                "where testbench runs)")
        return {"webhook": url.strip() if url else None, "webhook_env": env or None}

    @staticmethod
    def resolve(opts: dict) -> tuple[str | None, str]:
        """(the URL, where it comes from), or (None, why it cannot be had here)."""
        env = opts.get("webhook_env")
        if env:
            url = os.environ.get(env, "").strip()
            if not url:
                return None, (f"notify.webhook_env names {env}, which is not set in this environment; set it where "
                              "testbench runs (the shell, the MCP server's environment, the CI secrets)")
            if not re.match(r"https?://", url):
                return None, f"${env} does not hold an http:// or https:// URL"
            return url, f"${env}"
        return opts.get("webhook"), "notify.webhook in testbench.yaml"

    def send(self, event: str, text: str, fields: dict, opts: dict, timeout: float) -> tuple[bool, str]:
        url, said = self.resolve(opts)
        if not url:
            return False, said
        return _post(url, {"text": text, **fields}, timeout)

    def where(self, opts: dict) -> str:
        url, said = self.resolve(opts)
        return f"{where(url)} ({said})" if url else f"a webhook (${opts['webhook_env']})" if opts.get("webhook_env") \
            else "a webhook"

    def missing(self, opts: dict) -> str:
        url, said = self.resolve(opts)
        return "" if url else said


class Desktop(Notifier):
    """A notification on this machine's screen, for whoever works at it: notify-send on Linux (libnotify), osascript
    on macOS, a PowerShell toast on Windows (else msg). No account, no URL, no settings: `desktop: true`. Where none
    of them is found (a server, CI), notify.log says so and nothing else happens."""
    name = "desktop"
    TITLE = "agent-testbench"
    TOAST = ("$ErrorActionPreference='Stop';"            # the text comes in the environment, never in the script
             "[void][Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
             "ContentType=WindowsRuntime];"
             "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
             "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);$t=$x.GetElementsByTagName('text');"
             "[void]$t.Item(0).AppendChild($x.CreateTextNode($env:TESTBENCH_NOTE_TITLE));"
             "[void]$t.Item(1).AppendChild($x.CreateTextNode($env:TESTBENCH_NOTE_TEXT));"
             "$app='{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe';"
             "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show("
             "[Windows.UI.Notifications.ToastNotification]::new($x))")

    @staticmethod
    def tools() -> list[tuple[str, str]]:
        """(command, its path) for each way to show one here, best first."""
        names = ("osascript",) if sys.platform == "darwin" else ("powershell", "pwsh", "msg") if os.name == "nt" \
            else ("notify-send",)
        return [(n, path) for n in names if (path := shutil.which(n))]

    def command(self, tool: str, path: str, title: str, body: str) -> tuple[list[str], dict | None]:
        """The command line (the text only ever as a value) and, for PowerShell, the environment carrying the text."""
        if tool == "notify-send":
            return [path, "--", title, body], None
        if tool == "osascript":
            return [path, "-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", title, body], None
        if tool == "msg":
            return [path, "*", "/TIME:60", f"{title}: {body}"], None
        return [path, "-NoProfile", "-NonInteractive", "-Command", self.TOAST], \
            {**os.environ, "TESTBENCH_NOTE_TITLE": title, "TESTBENCH_NOTE_TEXT": body}

    def send(self, event: str, text: str, fields: dict, opts: dict, timeout: float) -> tuple[bool, str]:
        found = self.tools()
        if not found:
            return False, self.missing(opts)
        body, deadline, said = text.removeprefix(f"{self.TITLE}, ")[:600], time.monotonic() + timeout, "nothing ran"
        for tool, path in found:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            argv, env = self.command(tool, path, self.TITLE, body)
            label = "" if tool == found[0][0] else f"{tool} "
            try:
                p = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=left, env=env,
                                   stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except subprocess.TimeoutExpired:
                said = f"{label}no answer within {timeout:g} s"
                continue
            except OSError as exc:
                said = f"{tool} did not start: {exc}"
                continue
            if p.returncode == 0:
                return True, f"{label}exit 0"
            out = " ".join((p.stderr or p.stdout or "").split())[:200]
            said = f"{label}exit {p.returncode}" + (f": {out}" if out else "")
        return False, said

    def where(self, opts: dict) -> str:
        found = self.tools()
        return f"this machine's desktop ({found[0][0]})" if found else "this machine's desktop"

    def missing(self, opts: dict) -> str:
        if self.tools():
            return ""
        how = ("osascript (part of macOS) is not on PATH" if sys.platform == "darwin" else
               "neither PowerShell nor msg is on PATH" if os.name == "nt" else
               "notify-send is not installed (it comes with libnotify: apt install libnotify-bin, dnf install libnotify)")
        return (f"no desktop notification on this machine: {how}. Leave desktop out of notify: where nobody watches "
                "a screen (servers, CI)")


WEBHOOK, DESKTOP = Webhook(), Desktop()


# ---- delivery: every notifier testbench.yaml names, at once, never raising or holding anything up ----------------

def chosen(n: dict | None) -> dict:
    """{notifier: its settings} for a checked `notify:` section (config._notify): the webhook first when it has a
    URL or a variable, then every other notifier it names."""
    if not n:
        return {}
    hook = {"webhook": {"webhook": n.get("webhook"), "webhook_env": n.get("webhook_env")}} \
        if n.get("webhook") or n.get("webhook_env") else {}
    return {**hook, **{k: v for k, v in n.items() if k not in C.NOTIFY_OWN}}


def target(cfg: dict) -> tuple[str | None, str]:
    """The webhook's (URL, where it comes from), or (None, why it cannot be had here)."""
    return Webhook.resolve(cfg.get("notify") or {})


def _missing(nt, opts) -> str:
    check = getattr(nt, "missing", None)
    return " ".join(str(check(opts) or "").split())[:400] if callable(check) else ""


def _shown(nt, opts, name: str) -> str:
    try:
        said = " ".join(str(nt.where(opts)).split())[:200]
    except Exception:
        said = ""
    return _hide(said, opts) or name


def places(cfg: dict) -> list[tuple[str, str, str]]:
    """(notifier, where it sends as it may be shown, what this machine lacks to send now or "") for each notifier
    testbench.yaml names: for `testbench notify test` and the dashboard."""
    out = []
    for name, opts in chosen(cfg.get("notify")).items():
        try:
            nt = P.get("notifiers", name)
            out.append((name, _shown(nt, opts, name), _hide(_missing(nt, opts), opts)))
        except Exception as exc:
            out.append((name, name, str(exc) if isinstance(exc, P.PluginError) else f"{type(exc).__name__}: {exc}"))
    return out


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
    """Tell every notifier testbench.yaml names about one event, if `on:` lists it (force: whatever `on:` says).
    Never raises. (None, why) when nothing was sent because nothing is set up; (True/False, what happened)
    otherwise - True when every notifier took it - and each notifier's outcome is a line in notify.log."""
    notifiers: dict = {}
    try:
        if cfg is None:
            cfg = C.load(root)
        n = cfg.get("notify")
        if not n or (not force and event not in n["on"]):
            return None, "notifications are not set up for this event"
        notifiers = chosen(n)
        if not notifiers:
            return None, "no notifier is set up"
        text = f"agent-testbench, {cfg['project']}: {text}"
        fields = {"event": event, "project": cfg["project"], "machine": socket.gethostname(),
                  "time": dt.datetime.now().isoformat(timespec="seconds"), **fields}
        told = _deliver(root, event, text, fields, notifiers)
        return all(ok for ok, _ in told), "; ".join(what for _, what in told)
    except Exception as exc:                             # a notification must never break what it reports on
        why = f"not sent ({type(exc).__name__}: {exc})"[:300]
        for opts in notifiers.values():
            why = _hide(why, opts)
        _log(root, f"{event}: {why}")
        return False, why


def _deliver(root: Path, event: str, text: str, fields: dict, notifiers: dict) -> list[tuple[bool, str]]:
    """Every notifier at once, each in a thread of its own; all of them answered, or left behind, within TIMEOUT
    and GRACE. One that lacks something here (missing()) or does not load is not tried, and notify.log says why."""
    timeout, jobs = TIMEOUT, []
    for name, opts in notifiers.items():
        try:
            nt = P.get("notifiers", name)
            why = _missing(nt, opts)
        except Exception as exc:
            nt, why = None, (str(exc) if isinstance(exc, P.PluginError)
                             else f"the {name} notifier: {type(exc).__name__}: {exc}")
        box: list = []
        worker = None if why else threading.Thread(target=_attempt, args=(nt, event, text, fields, opts, timeout, box),
                                                   daemon=True)
        if worker:
            worker.start()
        jobs.append((name, opts, nt, _hide(why, opts), worker, box))
    deadline, told = time.monotonic() + timeout + GRACE, []
    for name, opts, nt, why, worker, box in jobs:
        if why:
            _log(root, f"{event}: nothing sent - {why}")
            told.append((False, f"not sent: {why}"))
            continue
        worker.join(max(0.0, deadline - time.monotonic()))
        ok, detail = box[0] if box else (False, f"no answer within {timeout:g} s")
        what = f"to {_shown(nt, opts, name)}: " + ("accepted" if ok else "not accepted") + f" ({_hide(detail, opts)})"
        _log(root, f"{event} {what}")
        told.append((ok, what))
    return told


def _attempt(nt, event: str, text: str, fields: dict, opts: dict, timeout: float, box: list) -> None:
    """One notifier's send(), whatever it does: (ok, detail) in box, never an exception."""
    try:
        got = nt.send(event, text, dict(fields), opts, timeout)
        if isinstance(got, tuple) and len(got) == 2:
            box.append((bool(got[0]), " ".join(str(got[1]).split())[:300]))
        else:
            box.append((False, f"its send() gave back {type(got).__name__}, not (ok, detail)"))
    except BaseException as exc:
        box.append((False, f"{type(exc).__name__}: {' '.join(str(exc).split())[:200]}"))


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
    a watch runs on every save, and a message for each would be noise. A loop's runs are told by the loop, as
    loop_try once each is judged kept or reverted (a run's own PASSED cannot say which).

    Once per run, whoever closes it: a cancel that gave up waiting for the driver, then the driver finishing after
    all, both call this. The first to create the run's TOLD file (atomically: O_EXCL) is the one that tells."""
    from . import report as R, runs
    try:
        try:
            fd = os.open(Path(run_dir) / TOLD, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            return
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"time": dt.datetime.now().isoformat(timespec="seconds"), "pid": os.getpid()}) + "\n")
        state = runs.read_state(run_dir)
        rep = rep or R.load(run_dir) or {}
        status = state.get("status") or rep.get("status")
        event, origin = "run_end", state.get("origin") or {}
        if origin.get("loop"):
            return
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


# ---- the notifiers plug-in kind: what the registry holds, and the contract `testbench plugin test` checks --------

NAME = re.compile(r"[a-z][a-z0-9_-]{0,39}")
RESERVED = ("on", "webhook_env")                 # notify:'s own keys besides webhook, so no notifier's names
CHECK_TIMEOUT = 1.5                              # the timeout the contract gives send()


def _prepare(obj, name: str):
    found = obj() if isinstance(obj, type) else obj
    if not callable(getattr(found, "send", None)):
        raise P.PluginError(f"{obj!r} is not a notifier: a notifier has send(event, text, fields, opts, timeout) "
                            "(notify.Notifier)")
    if not getattr(found, "name", ""):
        found.name = name
    return found


def _fill(value, url: str):
    """The example settings with "{url}" made into `url`."""
    if isinstance(value, str):
        return value.replace("{url}", url)
    if isinstance(value, dict):
        return {k: _fill(v, url) for k, v in value.items()}
    return [_fill(v, url) for v in value] if isinstance(value, list) else value


def _contract(nt) -> list[str]:
    """What is wrong with a notifier, in plain words ([] = it keeps the contract): a name notify: can use, settings
    checked (junk refused with a ConfigError naming the field), and, against an address on this machine that refuses
    and then one that never answers, a send() that returns (ok, detail) within its timeout and a second, never
    raises, and shows neither in its answer nor in where() the URL's path or query."""
    import copy
    import secrets
    out = []
    name = getattr(nt, "name", "")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in RESERVED:
        out.append(f"its name is {name!r}: a notifier's name is a short lowercase word, its key under `notify:` (not "
                   f"{' or '.join(RESERVED)}, which notify: uses itself), e.g. teams")
    lacking = [f"it has no {how} (notify.Notifier says what each does)" for attr, how in (
        ("validate", "validate(where, opts) -> dict"), ("where", "where(opts) -> str"),
        ("send", "send(event, text, fields, opts, timeout) -> (ok, detail)")) if not callable(getattr(nt, attr, None))]
    if lacking:
        return out + lacking
    field = f"notify.{name}"
    for junk in (42, ["a", "list"], {"no_such_setting": 1}):
        try:
            nt.validate(field, copy.deepcopy(junk))
            out.append(f"validate() took {junk!r} as its settings; refuse what it cannot use with config.ConfigError, "
                       f"naming `{field}` and saying what to write instead")
        except C.ConfigError as exc:
            if field not in str(exc):
                out.append(f"validate() refused {junk!r} without naming the field ({exc}); name `{field}` (or the "
                           "setting under it), so the reader knows which line of testbench.yaml to fix")
        except Exception as exc:
            out.append(f"validate() raised {type(exc).__name__} for {junk!r} ({exc}); refuse bad settings with "
                       "config.ConfigError, whose message says what to write instead")
    hidden = (f"tb-check-{secrets.token_hex(6)}", f"tb-check-{secrets.token_hex(6)}")
    example = getattr(nt, "example", None) or {}
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        refusing = closed.getsockname()[1]
    silent = socket.socket()                    # listens and never answers: the kernel takes the connection, no more
    try:
        silent.bind(("127.0.0.1", 0))
        silent.listen(8)
        cases = [(f"http://127.0.0.1:{port}/{hidden[0]}/hook?key={hidden[1]}", against) for port, against in (
            (refusing, " against a closed port on this machine"),
            (silent.getsockname()[1], " against a port that takes the connection and never answers"))] \
            if "{url}" in json.dumps(example) else [("", "")]
        for n, (url, against) in enumerate(cases):
            try:
                opts = nt.validate(field, _fill(copy.deepcopy(example), url))
            except Exception as exc:
                return out + [f"validate() refused its own example settings {example!r} ({type(exc).__name__}: "
                              f"{exc}); `example` holds settings that validate, so its send() can be tried"]
            if not isinstance(opts, dict):
                return out + [f"validate() returned {type(opts).__name__}; it returns the settings send() gets, as a "
                              "dict"]
            out += _try_notify(nt, opts, bool(url), against, hidden, first=n == 0)
    finally:
        silent.close()
    return out


def _try_notify(nt, opts: dict, unreachable: bool, against: str, hidden: tuple, first: bool) -> list[str]:
    out = []
    try:
        shown = nt.where(opts)
        if not isinstance(shown, str) or not shown.strip():
            out.append(f"where() returned {shown!r}; it says where messages go, as text notify.log may show")
        elif any(h in shown for h in hidden):
            out.append(f"where() shows the URL's path or query ({shown}); show only its scheme and host, as "
                       "notify.where(url) does")
    except Exception as exc:
        out.append(f"where() raised {type(exc).__name__} ({exc}); it says where messages go, as text")
    try:
        lacks = _missing(nt, opts)
        if any(h in lacks for h in hidden):
            out.append("missing() shows the URL's path or query; name what is missing without the URL")
    except Exception as exc:
        out.append(f"missing() raised {type(exc).__name__} ({exc}); it returns what is missing here, or \"\"")
    out = out if first else []
    box: list = []

    def go():
        fields = {"event": "test", "project": "notifiercheck", "machine": socket.gethostname(),
                  "time": dt.datetime.now().isoformat(timespec="seconds"), "status": "test"}
        try:
            box.append(("returned", nt.send("test", "agent-testbench, notifiercheck: a test message from the notifier "
                                            "check", fields, opts, CHECK_TIMEOUT)))
        except BaseException as exc:
            box.append(("raised", exc))

    worker = threading.Thread(target=go, daemon=True)
    worker.start()
    worker.join(CHECK_TIMEOUT + GRACE)
    if not box:
        return out + [f"send(){against} was still going {CHECK_TIMEOUT + GRACE:g} s after it was called with timeout "
                      f"{CHECK_TIMEOUT:g}; one attempt within the timeout it is given, then (False, \"no answer within "
                      "... s\")"]
    state, got = box[0]
    if state == "raised":
        return out + [f"send(){against} raised {type(got).__name__} ({str(got)[:200]}); it catches everything and "
                      "returns (False, what went wrong)"]
    if not (isinstance(got, tuple) and len(got) == 2 and isinstance(got[0], bool) and isinstance(got[1], str)):
        return out + [f"send(){against} returned {got!r}; it returns (ok, detail): True or False, and a few words on "
                      "what happened"]
    if any(h in got[1] for h in hidden):
        out.append(f"send(){against} showed the URL's path or query in what it returned ({got[1][:200]}); show only "
                   "its scheme and host, as notify.where(url) does")
    if unreachable and got[0]:
        out.append(f"send() said True{against}; True means the message was taken, and nothing could take it there")
    return out


KIND = P.Kind("notifiers", "notifier", "where `notify:` tells people that a run or loop ended or a launch waits",
              {"webhook": "agent_testbench.notify:WEBHOOK", "desktop": "agent_testbench.notify:DESKTOP"},
              prepare=_prepare, contract=_contract)
