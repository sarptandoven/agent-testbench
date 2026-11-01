"""testbench ui --serve: a live, read-only dashboard on 127.0.0.1. The overview and a run's page follow a real run
while it goes, then show its report; the server refuses other Host headers and every path outside what it serves,
escapes every name, and never shows a secret."""
import base64
import html
import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

import pytest

from agent_testbench import dashboard
from conftest import PNG_1PX, Webhook, cli, write_config

HOSTILE = "<img src=x onerror=alert(1)>"
ENV_VALUE = "env-value-never-shown-7f3a"
KERNEL_KEY = "kernel-key-never-shown-91c2"
MARKER = "outside-the-testbench-folder-5d1e"
HOOK = "https://hooks.example.com" + Webhook.SECRET
SECRETS = (ENV_VALUE, KERNEL_KEY, MARKER, "xoxSECRETKEY", "T0SECRET")
STEP = """\
import base64, pathlib, time
print("first line from the step", flush=True)
for _ in range(600):
    if pathlib.Path("go").exists():
        break
    time.sleep(0.05)
pathlib.Path("out").mkdir(exist_ok=True)
pathlib.Path("out/plot.png").write_bytes(base64.b64decode({png!r}))
print("last line from the step", flush=True)
"""


def get(port, path, host=None, method="GET"):
    """(status, content type, body); host "" sends no Host header at all."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        if host != "":
            c.putheader("Host", host or f"127.0.0.1:{port}")
        c.endheaders()
        r = c.getresponse()
        return r.status, r.getheader("Content-Type") or "", r.read().decode("utf-8", "replace")
    finally:
        c.close()


def poll(port, path, *wanted, timeout=40):
    end, body = time.time() + timeout, ""
    while time.time() < end:
        status, _, body = get(port, path)
        if status == 200 and all(w in body for w in wanted):
            return body
        time.sleep(0.2)
    pytest.fail(f"{path} never showed {wanted}; its last answer:\n{body[-3000:]}")


def links(page_path, body):
    """Every same-server link and image on a page, as paths."""
    out = []
    for ref in re.findall(r"""(?:href|src)=['"]([^'"]+)['"]""", body):
        ref = html.unescape(ref)
        if not ref.startswith(("data:", "http:", "https:", "#")):
            out.append(urllib.parse.urljoin(page_path, ref))
    return out


def clean(path, kind, body):
    for secret in SECRETS:
        assert secret not in body, f"{path} shows {secret!r}"
    if "html" in kind or "json" in kind:
        assert HOSTILE not in body, f"{path} has a name as markup"
        assert "<form" not in body and "<button" not in body, f"{path} has something to click that acts"


@pytest.fixture
def serve():
    servers = []

    def start(root):
        server = dashboard.make_server(root, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server.server_address[1]
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


@pytest.fixture
def project(tmp_path, webhook, monkeypatch):
    root = tmp_path / "dash"
    root.mkdir()
    (root / "slow.py").write_text(STEP.format(png=base64.b64encode(PNG_1PX).decode()))
    (root / "outside.txt").write_text(MARKER)
    write_config(root, {"project": "dash", "notify": {"webhook_env": "TB_TEST_HOOK"}, "plans": {"slow": {"steps": [
        {"name": HOSTILE, "run": "python slow.py", "max_minutes": 2, "env": {"API_KEY": ENV_VALUE},
         "artifacts": ["out/*.png"]}]}}})
    monkeypatch.setenv("TB_TEST_HOOK", webhook.url)
    return root


def test_the_dashboard_follows_a_run_live_then_shows_its_report(project, webhook):
    # -X utf8: on Windows the CLI would otherwise start itself again in UTF-8 mode, and stopping it would leave that one
    server = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "agent_testbench", "ui", "--serve", "--port", "0"],
                              cwd=project, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        said = server.stdout.readline()
        assert "read-only" in said, said
        port = int(re.search(r"http://127\.0\.0\.1:(\d+)/", said).group(1))
        r = cli(project, "run", "slow", "--detach")
        assert r.returncode == 3 and "started" in r.stdout, r.stdout + r.stderr     # 3: still running
        run_id = re.search(r"run (\S+) started", r.stdout).group(1)
        seen = {}
        overview = poll(port, "/live/overview", "RUNNING", html.escape(HOSTILE), f"/runs/{run_id}/", "step 1 of 1")
        assert f"to http://127.0.0.1:{webhook.server.server_address[1]}" in overview, "the webhook as scheme and host"
        seen["/live/overview"] = ("text/html", overview)
        status, kind, page = get(port, f"/runs/{run_id}/")
        assert status == 200 and "id=live" in page and f"testbench cancel {run_id}" in page and "Read-only" in page
        seen[f"/runs/{run_id}/ (running)"] = (kind, page)

        first = poll(port, f"/live/runs/{run_id}?step=&offset=-1", "first line from the step")
        live = json.loads(first)
        assert not live["done"] and live["label"] == f"Output of step 1: {HOSTILE}" and "RUNNING" in live["head"]
        seen["live json"] = ("application/json", first)
        (project / "go").touch()
        then = poll(port, f"/live/runs/{run_id}?step={live['step']}&offset={live['offset']}",
                    "last line from the step", '"done": true')
        assert "first line" not in json.loads(then)["text"], "a page gets only what is new"
        seen["live json, done"] = ("application/json", then)

        status, kind, page = get(port, f"/runs/{run_id}/")
        assert status == 200 and "id=live" not in page and "has ended" in page and "PASS" in page
        assert "data:image/png;base64," in page, "the report, with its images"
        seen[f"/runs/{run_id}/"] = (kind, page)
        for path in ["/", "/ui/index.html"] + links(f"/runs/{run_id}/", page):
            status, kind, body = get(port, path)
            assert status == 200, f"{path}: {status} {body[:200]}"
            seen[path] = (kind, body)
        assert run_id in seen["/ui/index.html"][1], "the static pages too"
        assert any(f"/runs/{run_id}/steps/" in p and p.endswith("plot.png") for p in seen), "the evidence files"
        for path in links("/ui/index.html", seen["/ui/index.html"][1]):
            seen[path] = get(port, path)[1:]
        for path in (f"/runs/{run_id}/state.json", f"/runs/{run_id}/plan.json", f"/runs/{run_id}/driver.log",
                     "/ledger.jsonl", "/notify.log", "/testbench.yaml", "/runs/../testbench.yaml"):
            status, kind, body = get(port, path)
            assert status == 404, f"{path} was served"
            seen[path] = (kind, body)
        for path, (kind, body) in seen.items():
            clean(path, kind, body)
        if os.name != "nt":                            # Ctrl-C stops it cleanly
            server.send_signal(signal.SIGINT)
            assert server.wait(15) == 0 and "dashboard stopped" in server.stdout.read()
    finally:
        if server.poll() is None:
            server.terminate()
            server.wait(15)
        server.stdout.close()


def _fake_run(root, name, plan=HOSTILE, status="failed", **state):
    d = root / ".testbench" / "runs" / name
    (d / "steps" / "01-x").mkdir(parents=True)
    (d / "steps" / "01-x" / "log.txt").write_text(f"said {HOSTILE}\n")
    (d / "state.json").write_text(json.dumps({"run_id": name, "plan": plan, "backend": "local", "status": status,
                                              "created": time.time(), "started": time.time(), "env": ENV_VALUE,
                                              "worst_case_usd": 0.5, "usd_per_minute": 0.01, **state}))
    (d / "plan.json").write_text(json.dumps({"steps": [{"name": HOSTILE, "kind": "run", "max_minutes": 5,
                                                        "env": {"API_KEY": ENV_VALUE}}]}))
    if status in ("passed", "failed"):
        (d / "report.json").write_text(json.dumps({
            "run_id": name, "plan": plan, "status": status, "backend": "local", "started": time.time(), "minutes": 0.1,
            "cost_usd": 0.0, "skipped": [], "steps": [{
                "name": HOSTILE, "kind": "run", "status": status, "minutes": 0.1, "dir": "steps/01-x",
                "stdout_tail": HOSTILE, "expectations": [{"check": HOSTILE, "ok": False, "detail": HOSTILE}]}]}))
    else:
        (d / "progress.jsonl").write_text(json.dumps({"t": time.time(), "event": "step_start", "step": HOSTILE,
                                                      "index": 1}) + "\n")
    return d


@pytest.fixture
def hostile(tmp_path):
    """A project whose folders hold hostile names, secrets in state files, a loop, sessions and a stale run."""
    root = tmp_path / "hostile"
    root.mkdir()
    (root / "outside.txt").write_text(MARKER)
    write_config(root, {"project": "hostile", "notify": {"webhook": HOOK}, "plans": {"p": {"steps": [
        {"name": HOSTILE, "run": "true", "max_minutes": 1}]}}})
    _fake_run(root, "20260101-000000-p")
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    _fake_run(root, "20260101-000001-p", status="running", driver_pid=gone.pid)
    now = time.time()
    for name, status in (("main", "ready"), ("old", "stopped")):
        d = root / ".testbench" / "sessions" / name
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({
            "name": name, "backend": "local", "gpu": HOSTILE, "status": status, "created": now - 120,
            "started": now - 120, "last_used": now - 30, "idle_minutes": 10, "max_minutes": 60, "usd_per_minute": 0.01,
            "worst_case_usd": 0.6, "exec_count": 3, "handles": {"launcher_pid": 1, "key": KERNEL_KEY},
            "stop_reason": HOSTILE, "stopped": now, "cost_usd": 0.02}))
    d = root / ".testbench" / "loops" / "20260101-000000-p"
    d.mkdir(parents=True)
    tries = [{"n": 0, "description": "baseline (the code as it was)", "commit": "a" * 40, "run": "20260101-000000-p",
              "status": "failed", "kept": False, "value": None, "cost_usd": 0.0, "worst_case_usd": 0.5},
             {"n": 1, "description": HOSTILE, "commit": "b" * 40, "run": "20260101-000001-p", "status": "running",
              "worst_case_usd": 0.5}]
    (d / "state.json").write_text(json.dumps({
        "id": d.name, "dir": str(d), "status": "running", "plan": "p", "metric": f"check:{HOSTILE}", "path": "check",
        "key": HOSTILE, "goal": "max", "min_delta": 0.0, "budget_usd": 5.0, "deadline": now + 3600, "branch": "b",
        "original_branch": "main", "base": "a" * 40, "best": {"commit": "a" * 40, "value": None, "try": None},
        "tries": tries, "pending": 1, "started": now, "target": 0.9, "patience": 3, "max_tries": 5}))
    return root


def test_names_are_escaped_and_secrets_never_shown(hostile, serve):
    port = serve(hostile)
    seen = {}
    for path in ("/", "/live/overview", "/runs/20260101-000000-p/", "/runs/20260101-000001-p/", "/ui/index.html",
                 "/live/runs/20260101-000001-p?step=&offset=-1"):
        status, kind, body = get(port, path)
        assert status == 200, f"{path}: {status} {body[:300]}"
        seen[path] = (kind, body)
    overview = seen["/live/overview"][1]
    assert html.escape(HOSTILE) in overview and "to https://hooks.example.com" in overview
    assert "LOST" in overview, "a run whose driver is gone"
    assert "ready" in overview and "about $0.0" in overview, "the live session, and what it has cost so far"
    assert "patience 3" in overview and "at most 5 tries" in overview, "the loop's stopping rules"
    assert "FAIL" in seen["/runs/20260101-000000-p/"][1] and "has ended" in seen["/runs/20260101-000000-p/"][1]
    live = json.loads(seen["/live/runs/20260101-000001-p?step=&offset=-1"][1])
    assert live["done"] and live["text"] == f"said {HOSTILE}\n"
    for path in links("/runs/20260101-000000-p/", seen["/runs/20260101-000000-p/"][1]):
        seen[path] = get(port, path)[1:]
    for path, (kind, body) in seen.items():
        clean(path, kind, body)


def test_only_requests_for_127_0_0_1_or_localhost_are_answered(hostile, serve):
    port = serve(hostile)
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.nip.io", f"localhost.evil.example:{port}",
                 f"127.0.0.1:{port}@evil.example", "0.0.0.0", f"[::1]:{port}", ""):
        for path in ("/", "/live/overview", "/ui/index.html", "/runs/20260101-000000-p/report.json"):
            status, _, body = get(port, path, host=host)
            assert status == 403 and "Refused" in body and "hostile" not in body, (host, path, status)
    for host in ("127.0.0.1", f"127.0.0.1:{port}", f"localhost:{port}", f"LOCALHOST:{port}"):
        assert get(port, "/", host=host)[0] == 200, host
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        status, _, body = get(port, "/", method=method)
        assert status == 405 and "read-only" in body, method


def test_nothing_outside_the_pages_reports_and_evidence_is_served(hostile, serve):
    run = hostile / ".testbench" / "runs" / "20260101-000000-p"
    try:
        os.symlink(hostile / "outside.txt", run / "steps" / "01-x" / "out.txt")
        os.symlink(run / "state.json", run / "steps" / "01-x" / "state.txt")
    except (OSError, NotImplementedError):
        pass                                           # no symlinks here (Windows without the right): the rest holds
    port = serve(hostile)
    r = "/runs/20260101-000000-p"
    for path in ("/../outside.txt", "/runs/../../outside.txt", "/runs/%2e%2e/%2e%2e/outside.txt",
                 f"{r}/steps/..%2f..%2f..%2f..%2foutside.txt", f"{r}/steps/01-x/..%5c..%5cstate.json",
                 f"{r}/steps/01-x/../../state.json", "/ui/..%2f..%2foutside.txt", "//outside.txt", "/outside.txt",
                 f"{r}/state.json", f"{r}/plan.json", f"{r}/progress.jsonl", "/ledger.jsonl", "/notify.log",
                 "/sessions/main/state.json", "/loops/20260101-000000-p/state.json", f"{r}/steps/01-x/out.txt",
                 f"{r}/steps/01-x/state.txt", f"{r}/steps/01-x/log.txt%00.png", "/C:/Windows/win.ini",
                 "/runs/C:%5cWindows%5cwin.ini/report.json", f"{r}/steps/.x", "http://evil.example/outside.txt",
                 "/runs/20260101-000000-p/report.json/../state.json", "/live/runs/..%2f..%2foutside.txt"):
        status, _, body = get(port, path)
        assert status in (400, 404), f"{path} was served: {body[:200]}"
        for secret in SECRETS:
            assert secret not in body, path
    status, kind, body = get(port, f"{r}/steps/01-x/log.txt")
    assert status == 200 and kind.startswith("text/plain") and HOSTILE in body, "evidence is served, as plain text"


def test_a_log_is_followed_from_where_the_page_left_off(tmp_path):
    run = _fake_run(tmp_path, "20260101-000000-p", status="running")
    log = run / "steps" / "01-x" / "log.txt"
    log.write_bytes("one\n".encode() + "dé".encode()[:2])           # cut inside the é
    a = dashboard.run_live(run, "", -1)
    assert a["text"] == "one\nd" and a["offset"] == 5
    with log.open("ab") as f:
        f.write("é".encode()[1:] + b"\ntwo\n")
    b = dashboard.run_live(run, a["step"], a["offset"])
    assert b["text"] == "é\ntwo\n" and b["offset"] == log.stat().st_size
    assert dashboard.run_live(run, "another-step", 3)["text"].startswith("one"), "a new step starts again"


def test_the_command_line(project):
    r = cli(project, "ui", "--port", "8771")
    assert r.returncode == 2 and "--port goes with --serve" in r.stderr
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    try:
        r = cli(project, "ui", "--serve", "--port", str(taken.getsockname()[1]), timeout=60)
        assert r.returncode == 2 and "Pick another with --port N" in r.stderr, r.stdout + r.stderr
    finally:
        taken.close()
