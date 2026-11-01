"""A web step's readiness probe asks this machine's own ports directly: a proxy in the environment (or macOS's
system proxy settings, which urllib reads) cannot reach them, and going through one made every web step on such a
machine wait out ready_timeout and then fail with "nothing answered". And `testbench doctor` launches Chromium
rather than trusting an installed playwright package, which says nothing about the browser build it needs."""
import http.server
import sys
import threading

from agent_testbench import web
from conftest import cli


def test_a_local_server_is_seen_through_a_proxy_that_is_not_there(monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")          # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        assert web._wait_up(f"http://127.0.0.1:{port}/", 5, None) == 200
        assert web._wait_up(f"http://localhost:{port}/", 5, None) == 200
    finally:
        srv.shutdown()


def test_nothing_listening_is_none():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert web._wait_up(f"http://127.0.0.1:{port}/", 1, None) is None


def test_a_chromium_build_that_is_missing_is_named_with_the_command_that_installs_it(tmp_path):
    browsers = tmp_path / "pw-browsers"
    for name in ("chromium-1243", "chromium_headless_shell-1243", "ffmpeg-1011"):
        (browsers / name).mkdir(parents=True)
    wanted = browsers / "chromium_headless_shell-1248" / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    said = web.explain_launch_error(f"BrowserType.launch: Executable doesn't exist at {wanted}\n"
                                    "Looks like Playwright was just installed or updated.")
    assert "needs Chromium build 1248 (chromium_headless_shell-1248), which is not installed" in said, said
    assert f"{browsers} has build 1243, for another Playwright version" in said
    assert said.endswith(f"Install it with: {sys.executable} -m playwright install chromium")
    said = web.explain_launch_error("Host system is missing dependencies to run browsers. libnss3")
    assert "lacks system libraries" in said and "-m playwright install-deps chromium" in said


def test_doctor_says_whether_chromium_launches(tmp_path):
    ok, detail = web.chromium_check()
    out = cli(tmp_path, "doctor").stdout
    line = next((ln for ln in out.splitlines() if "chromium (web steps)" in ln), None)
    if "playwright (web steps): installed" not in out:
        assert line is None, "no Chromium check without Playwright"
    elif ok:
        assert line.startswith("ok   chromium (web steps): Chromium ") and "launches headless" in line, out
    else:
        assert line.startswith("no   chromium (web steps): ") and "-m playwright install" in line, out
