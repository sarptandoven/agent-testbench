"""testbench selftest: a minute or so to see that the main promises hold on this machine.

It builds a small project in a temporary folder and, with this machine's limits and ledger swapped for empty ones
in that folder (your ~/.testbench is never touched), checks one thing per line: a plan passes, a broken change
fails and says why, evidence is fingerprinted, costs are refused or asked about before anything starts, a budget
that is not a number is refused, a live kernel answers, a mock LLM SDK stands in for the real one, a strategy that
decides with tomorrow's data is caught (and passes once fixed), a loop keeps a better try and reverts a worse one, an
HTML report is written, and the MCP server answers. Everything runs here
for free; nothing goes to Modal, Colab or any network. Exit 0 if no check failed (some may be skipped, with why).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import __version__

SCORE_PY = """\
import json, pathlib
from settings import SCORE
pathlib.Path("out").mkdir(exist_ok=True)
json.dump({"score": SCORE}, open("out/metrics.json", "w"))
print("score", SCORE)
"""
CONFIG = """\
project: selftest
budget: {max_run_usd: 1.0, ask_above_usd: 0.25, max_day_usd: 2.0}
plans:
  smoke:
    steps:
      - name: score
        run: python score.py
        max_minutes: 1
        expect:
          json: {out/metrics.json: {score: ">= 2"}}
  mid-gpu:                       # never run: only priced
    backend: modal
    gpu: A100-40GB
    steps: [{name: train, run: "true", max_minutes: 10}]
  big-gpu:                       # never run: only priced
    backend: modal
    gpu: H100:8
    steps: [{name: train, run: "true", max_minutes: 60}]
"""
STRATEGY_PY = """\
import csv, os, pathlib
rows = list(csv.DictReader(open(os.environ["TESTBENCH_DATA_PRICES"])))
closes = [float(r["close"]) for r in rows]
pathlib.Path("out").mkdir(exist_ok=True)
with open("out/signals.csv", "w") as f:
    f.write("time,symbol,signal\\n")
    for i, r in enumerate(rows):
        if i >= 5:
            seen = closes[: i + 2]          # meant to be the closes up to today's: this is up to tomorrow's
            f.write(f"{r['time']},{r['symbol']},{seen[-1] / (sum(seen[-5:]) / 5) - 1:.6f}\\n")
"""
STRATEGY_CONFIG = """\
project: selftest-strategy
data:
  prices: data/prices.csv
plans:
  lookahead:
    lookahead: {data: prices, output: out/signals.csv, cuts: 2}
    steps: [{name: strategy, run: python strategy.py, max_minutes: 1}]
"""


class Check:
    def __init__(self, root: Path, env: dict):
        self.root, self.env = root, env

    def tb(self, *args, cwd: Path | None = None, timeout: float = 240, stdin: str | None = None):
        return subprocess.run([sys.executable, "-m", "agent_testbench", *args], cwd=cwd or self.root, env=self.env,
                              capture_output=True, text=True, timeout=timeout, input=stdin)

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, env=self.env, capture_output=True, text=True, timeout=60)

    def score(self, n: int) -> None:
        (self.root / "settings.py").write_text(f"SCORE = {n}\n")

    def last_run(self) -> Path:
        return sorted((self.root / ".testbench" / "runs").iterdir())[-1]


def _said(r) -> str:
    return (r.stdout + r.stderr).strip()


def check_pass(c: Check):
    r = c.tb("run", "smoke")
    return r.returncode == 0 and "PASS" in r.stdout, _said(r)


def check_fail(c: Check):
    c.score(1)
    try:
        r = c.tb("run", "smoke")
    finally:
        c.score(3)
    return r.returncode == 1 and "FAIL" in r.stdout and "score >= 2" in r.stdout, _said(r)


def check_verify(c: Check):
    run = c.last_run()
    good = c.tb("verify", run.name)
    log = next(run.glob("steps/*/log.txt"))
    log.write_text(log.read_text() + "edited afterwards\n")
    bad = c.tb("verify", run.name)
    ok = good.returncode == 0 and bad.returncode != 0 and "log.txt" in bad.stdout + bad.stderr
    return ok, f"untouched: {_said(good)}\nedited: {_said(bad)}"


def check_costs(c: Check):
    big, mid = c.tb("estimate", "big-gpu"), c.tb("estimate", "mid-gpu")
    ok = big.returncode == 2 and "Refused" in big.stdout and mid.returncode == 2 and "Needs approval" in mid.stdout
    return ok, f"8 x H100 for an hour: {_said(big)}\nan A100 for 15 min: {_said(mid)}"


def check_nan(c: Check):
    yml = c.root / "testbench.yaml"
    text = yml.read_text()
    yml.write_text(text.replace("max_run_usd: 1.0", "max_run_usd: .nan"))
    try:
        r = c.tb("estimate", "big-gpu")
    finally:
        yml.write_text(text)
    return r.returncode == 2 and "must be a number" in _said(r), _said(r)


def check_session(c: Check):
    start = c.tb("session", "start", "--max-minutes", "3")
    if start.returncode != 0:
        return False, _said(start)
    try:
        r = c.tb("exec", "21 * 2")
    finally:
        stop = c.tb("session", "stop")
    return r.returncode == 0 and "=> 42" in r.stdout and stop.returncode == 0, f"{_said(r)}\n{_said(stop)}"


def check_mocks(c: Check):
    app = c.root.parent / "llm-app"
    r = c.tb("demo", "llm-app", str(app), cwd=c.root.parent, timeout=300)
    return r.returncode == 0 and "PASS" in r.stdout, _said(r)


def check_lookahead(c: Check):
    root = c.root.parent / "strategy"
    (root / "data").mkdir(parents=True)
    days = [d for d in (dt.date(2024, 1, 1) + dt.timedelta(n) for n in range(140)) if d.weekday() < 5]
    rows = "".join(f"{d:%Y-%m-%d},TEST,{100 + 8 * math.sin(i / 4) + 0.2 * i:.2f}\n" for i, d in enumerate(days))
    (root / "data" / "prices.csv").write_text("time,symbol,close\n" + rows)
    (root / "strategy.py").write_text(STRATEGY_PY)
    (root / "testbench.yaml").write_text(STRATEGY_CONFIG)
    caught = c.tb("run", "lookahead", cwd=root)
    (root / "strategy.py").write_text(STRATEGY_PY.replace("closes[: i + 2]", "closes[: i + 1]"))     # the fix
    fixed = c.tb("run", "lookahead", cwd=root)
    ok = (caught.returncode == 1 and "the strategy used rows after" in caught.stdout
          and fixed.returncode == 0 and "Look-ahead: none found" in fixed.stdout)
    return ok, f"with tomorrow's close: {_said(caught)[-1500:]}\nfixed: {_said(fixed)[-1500:]}"


def check_loop(c: Check):
    if not shutil.which("git"):
        return None, "git is not installed here"
    for args in (("init", "-q"), ("add", "-A"), ("-c", "user.name=selftest", "-c", "user.email=selftest@localhost",
                                                  "commit", "-qm", "start")):
        g = c.git(*args)
        if g.returncode != 0:
            return False, f"git {' '.join(args)}: {g.stderr}"
    env = {**c.env, "GIT_AUTHOR_NAME": "selftest", "GIT_COMMITTER_NAME": "selftest",
           "GIT_AUTHOR_EMAIL": "selftest@localhost", "GIT_COMMITTER_EMAIL": "selftest@localhost"}
    c2 = Check(c.root, env)
    start = c2.tb("loop", "start", "smoke", "--metric", "out/metrics.json:score", "--budget-usd", "0")
    c2.score(5)
    up = c2.tb("loop", "try", "raise the score to 5")
    c2.score(4)
    down = c2.tb("loop", "try", "lower it to 4")
    stop = c2.tb("loop", "stop")
    ok = (start.returncode == 0 and "KEPT" in up.stdout and "REVERTED" in down.stdout and stop.returncode == 0
          and "score 3 -> 5" in stop.stdout)          # the kept change stays on the loop's branch for a person
    return ok, "\n".join(_said(r) for r in (start, up, down, stop))


def check_html(c: Check):
    out = c.root / "report.html"
    r = c.tb("report", "--format", "html", "-o", str(out))
    ok = r.returncode in (0, 1) and out.is_file() and "<html" in out.read_text(errors="replace")[:2000].lower()
    return ok, _said(r)


def check_mcp(c: Check):
    import importlib.util
    if importlib.util.find_spec("mcp") is None:
        return None, 'the MCP package is not installed (pip install "agent-testbench[mcp]")'
    proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "mcp"], cwd=c.root, env=c.env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "selftest", "version": "1"}}}
        proc.stdin.write(json.dumps(hello) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
    finally:
        proc.kill()
        proc.wait(10)
    try:
        info = json.loads(line)["result"]["serverInfo"]
    except (ValueError, KeyError, TypeError):
        return False, f"no initialize answer: {line[:300]!r}"
    return info.get("name") == "agent-testbench", f"{info.get('name')} {info.get('version')}"


CHECKS = [
    ("a plan runs on this machine and passes", check_pass),
    ("a broken change fails, and the report says which check and why", check_fail),
    ("evidence is fingerprinted: verify passes, then catches an edited log", check_verify),
    ("costs are checked before anything starts: refused / needs approval", check_costs),
    ("a budget that is not a number (NaN) is refused", check_nan),
    ("a live kernel keeps running and answers", check_session),
    ("an LLM app runs against the mock SDK, with no key and no network", check_mocks),
    ("a strategy that decides with tomorrow's data is caught", check_lookahead),
    ("a loop keeps a better try and reverts a worse one", check_loop),
    ("an HTML report is written", check_html),
    ("the MCP server answers", check_mcp),
]


def main(keep: bool = False, say=print) -> int:
    began = time.time()
    tmp = Path(tempfile.mkdtemp(prefix="testbench-selftest-"))
    root = tmp / "project"
    root.mkdir()
    (root / "score.py").write_text(SCORE_PY)
    (root / "testbench.yaml").write_text(CONFIG)
    (root / ".gitignore").write_text("out/\n__pycache__/\nreport.html\n")
    env = {**os.environ, "TESTBENCH_USER_DIR": str(tmp / "user-testbench"), "PYTHONUTF8": "1"}
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"):
        env.pop(name, None)                                  # nothing here may reach a paid service
    c = Check(root, env)
    c.score(3)
    say(f"agent-testbench {__version__} self-test, in {tmp}")
    say("(local and free; this machine's limits and ledger are swapped for empty ones there)\n")
    counts = {"ok": 0, "FAILED": 0, "skipped": 0}
    for what, fn in CHECKS:
        t0 = time.time()
        try:
            ok, detail = fn(c)
        except Exception as exc:                             # a check that crashes is a failed check
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        mark = "skipped" if ok is None else "ok" if ok else "FAILED"
        counts[mark] += 1
        say(f"  {mark:<8} {what}  ({time.time() - t0:.0f} s)" + (f": {detail}" if ok is None else ""))
        if ok is False:
            say("\n".join("           " + ln for ln in str(detail).splitlines()[-25:]))
    say(f"\n{len(CHECKS)} checks: {counts['ok']} ok, {counts['FAILED']} failed, {counts['skipped']} skipped, "
        f"in {time.time() - began:.0f} s.")
    if counts["FAILED"] or keep:
        say(f"The project and its runs are kept in {tmp} (`testbench report` there shows each run).")
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return 1 if counts["FAILED"] else 0
