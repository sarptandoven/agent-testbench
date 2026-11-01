"""Split the bill: a page with a form, and a JSON API that does the sums. Standard library only.

    python app.py 8974          # then open http://127.0.0.1:8974/
"""
import json
import socketserver
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = Path(__file__).parent / "static"
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css"}


class Server(ThreadingHTTPServer):
    def server_bind(self):                 # no host name lookup: it can hold the start up ~30 s on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def split(total: str, people: str, tip_pct: str) -> dict:
    """Each person's share of the total plus the tip, to the cent."""
    try:
        total, people, tip_pct = Decimal(total), int(people), Decimal(tip_pct or "0")
        if not (total.is_finite() and tip_pct.is_finite()):
            raise ValueError("not a finite number")
    except (InvalidOperation, ValueError):
        return {"error": "Enter the total and the tip as numbers, and the number of people as a whole number."}
    if total < 0 or tip_pct < 0 or people < 1:
        return {"error": "The total and the tip can't be negative, and the bill needs at least one person."}
    if total > 1_000_000 or tip_pct > 100 or people > 1000:
        return {"error": "That is more than this page splits: up to $1,000,000, a 100% tip and 1000 people."}
    with_tip = total * (1 + tip_pct / 100)
    each = (with_tip / people).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return {"each": f"{each:.2f}", "total": f"{with_tip.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, kind: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = STATIC / (self.path.split("?")[0].lstrip("/") or "index.html")
        if path.resolve().parent != STATIC.resolve() or not path.is_file():
            return self._send(404, b"not found", "text/plain")
        self._send(200, path.read_bytes(), TYPES.get(path.suffix, "application/octet-stream"))

    def do_POST(self):
        if self.path != "/api/split":
            return self._send(404, b"not found", "text/plain")
        form = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        result = split(str(form.get("total", "")), str(form.get("people", "")), str(form.get("tip", "")))
        self._send(200, json.dumps(result).encode(), "application/json")   # errors are answers, shown in the page

    def log_message(self, fmt, *args):
        print(f"{self.command} {self.path} -> {args[1] if len(args) > 1 else ''}", flush=True)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8974
    print(f"serving http://127.0.0.1:{port}/", flush=True)
    Server(("127.0.0.1", port), Handler).serve_forever()
