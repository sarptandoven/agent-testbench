"""A web step's readiness probe asks this machine's own ports directly: a proxy in the environment (or macOS's
system proxy settings, which urllib reads) cannot reach them, and going through one made every web step on such a
machine wait out ready_timeout and then fail with "nothing answered"."""
import http.server
import threading

from agent_testbench import web


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
