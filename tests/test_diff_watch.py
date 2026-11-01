"""testbench diff (what changed between two runs, from report.py's comparison) and testbench watch (a free plan run
again on every change of the project's files, one run at a time, stopped cleanly)."""
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_testbench import diff as D, procs
from conftest import cli, git, write_config
from test_limits import guard

SCORER = """\
import json, pathlib
from settings import SCORE
pathlib.Path("out").mkdir(exist_ok=True)
json.dump({"score": SCORE, "loss": 1 / (SCORE + 1)}, open("out/m.json", "w"))
if SCORE < 2:
    pathlib.Path("out/low.txt").write_text("low\\n")
else:
    pathlib.Path("out/low.txt").unlink(missing_ok=True)
print("score", SCORE)
"""
PLAN = {"backend": "local", "steps": [{"name": "score", "run": "python score.py", "max_minutes": 2,
                                       "artifacts": ["out/*"], "env": {"PYTHONDONTWRITEBYTECODE": "1"},
                                       "expect": {"json": {"out/m.json": {"score": ">= 2", "loss": "< 0.5"}}}}]}


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "score.py").write_text(SCORER)
    (root / "settings.py").write_text("SCORE = 3\n")
    (root / ".gitignore").write_text("out/\n__pycache__/\n")
    write_config(root, {"project": "proj", "plans": {"q": PLAN}})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    return root


def commit(root: Path, score_line: str) -> None:
    (root / "settings.py").write_text(score_line)
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", score_line.strip())


# ---- diff ----------------------------------------------------------------------------------------------------

def test_diff_of_two_real_runs(proj):
    assert cli(proj, "run", "q", "--wait", "120").returncode == 0
    commit(proj, "SCORE = 1\n")                              # a commit that breaks it
    assert cli(proj, "run", "q", "--wait", "120").returncode == 1
    r = cli(proj, "diff", "q")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert out.startswith("# BROKE: plan `q` went from PASS to FAIL"), out
    assert "- Broke: score (passed -> failed)" in out
    assert "score | out/m.json: score >= 2: 3 -> 1 (-2; worse: the rule wants higher)" in out
    assert "score | out/m.json: loss < 0.5: 0.25 -> 0.5 (+0.25; worse: the rule wants lower)" in out
    assert "- Now failing: score: out/m.json: score >= 2 - 1 (wanted >= 2.0)" in out
    assert "- changed: `steps/01-score/artifacts/out/m.json`" in out, "the artifact's sha256 changed"
    assert "- added: `steps/01-score/artifacts/out/low.txt`" in out
    assert "B is 1 commit(s) after A" in out and "settings.py | 2 +-" in out
    assert "packages at the same versions" in out
    assert cli(proj, "diff", "last~1", "last").stdout == out == cli(proj, "diff").stdout, "the same two runs"

    d = json.loads(cli(proj, "diff", "q", "--json").stdout)
    assert d["verdict"] == "broke" and d["broke"] == ["score"] and d["a"]["status"] == "passed"
    score = next(n for n in d["numbers"] if n["check"] == "out/m.json: score >= 2")
    assert (score["a"], score["b"], score["delta"], score["wants"], score["change"]) == (3, 1, -2, "higher", "worse")
    assert [c["check"] for c in d["checks"]["now_failing"]] == ["out/m.json: score >= 2", "out/m.json: loss < 0.5"]
    assert "steps/01-score/artifacts/out/m.json" in d["evidence"]["changed"]
    assert d["code"]["stat"] and "settings.py" in d["code"]["stat"] and not d["code"]["same"]
    assert d["environment"]["packages"]["known"] and not d["environment"]["changes"]

    (proj / "settings.py").write_text("SCORE = 5\n")         # fixed, uncommitted
    assert cli(proj, "run", "q", "--wait", "120").returncode == 0
    out = cli(proj, "diff").stdout
    assert out.startswith("# FIXED: plan `q` went from FAIL to PASS") and "- Now passing: score" in out
    assert "the same commit" in out and "A had no uncommitted changes, B had uncommitted changes (sha256 " in out
    assert "- removed: `steps/01-score/artifacts/out/low.txt`" in out
    first = sorted(p.name for p in (proj / ".testbench" / "runs").iterdir())[0]
    assert "STILL PASSING" in cli(proj, "diff", first, "last").stdout

    for args, says in ((["nope"], "is not a plan with finished runs either (plans: q)"),
                       (["last~9"], "is before the first run: there are 3 runs"),
                       (["a", "b", "c"], "compares two runs")):
        r = cli(proj, "diff", *args)
        assert r.returncode == 2 and says in r.stdout, r.stdout

    from agent_testbench import mcp_server as M

    async def go():
        call = M.server.call_tool
        text = lambda out: json.dumps(out, default=lambda o: getattr(o, "text", str(o)))
        return (text(await call("diff_runs", {"a": "last~2", "b": "last~1", "project_dir": str(proj)})),
                text(await call("diff_runs", {"a": "q", "project_dir": str(proj), "as_json": True})))
    words, data = asyncio.run(go())
    assert "BROKE: plan `q`" in words and "[exit 0]" in words
    assert '\\"verdict\\": \\"fixed\\"' in data
    with pytest.raises(ValueError):
        M._run_ref("--approve")
    with pytest.raises(ValueError):
        M._run_ref("last~-1")
    assert M._run_ref("last~2") == "last~2"


def report(steps, status="passed", **extra) -> dict:
    return {"run_id": extra.pop("run_id", "r"), "plan": "p", "status": status, "started": 0, "steps": steps, **extra}


def test_diff_reads_rules_packages_and_older_reports():
    check = lambda name, detail, ok=True: {"check": name, "ok": ok, "detail": detail}
    a = report([{"name": "s", "status": "passed", "expectations": [
        check("m: a ~= 1 +- 0.5", "1.4 (wanted 1 ± 0.5; differs by +0.4)"), check("m: b between 0 and 9", "3 (wanted between 0 and 9)"),
        check("m: c <= 5", "4 (wanted <= 5.0)"), check("m: d >= 1", "7 (wanted >= 1.0)")]}],
        environment={"python": "3.12.1", "packages_sha256": "x", "package_versions": {"numpy": "2.1.0", "old": "1"}})
    b = report([{"name": "s", "status": "passed", "expectations": [
        check("m: a ~= 1 +- 0.5", "1.1 (wanted 1 ± 0.5; differs by +0.1)"), check("m: b between 0 and 9", "5 (wanted between 0 and 9)"),
        check("m: c <= 5", "4 (wanted <= 5.0)")]}],
        environment={"python": "3.13.0", "packages_sha256": "y", "package_versions": {"numpy": "2.2.6", "new": "3"}})
    d = D.compare(a, b)
    by = {n["check"]: n for n in d["numbers"]}
    assert by["m: a ~= 1 +- 0.5"]["change"] == "better" and by["m: a ~= 1 +- 0.5"]["wants"] == "closer to 1"
    assert by["m: b between 0 and 9"]["change"] == "moved" and by["m: b between 0 and 9"]["wants"] is None
    assert by["m: c <= 5"]["change"] == "same" and by["m: d >= 1"]["change"] == "gone"
    assert d["checks"]["gone"][0]["check"] == "m: d >= 1"
    out = D.render(d)
    assert "- python: 3.12.1 -> 3.13.0" in out and "numpy 2.1.0 -> 2.2.6; added new 3; removed old 1" in out
    assert "the rule says no direction" in out and "not in B (A found 7)" in out
    assert "Not compared: a run from before testbench recorded evidence hashes." in out
    for env in (a["environment"], b["environment"]):         # reports from before package versions were recorded
        del env["package_versions"]
    assert "which ones changed is not known" in D.render(D.compare(a, b))
    with pytest.raises(D.DiffError, match="not a report testbench wrote"):
        D.compare(a, {"steps": "nope"})


# ---- watch ---------------------------------------------------------------------------------------------------

class Watching:
    """`testbench watch` in its own process, its output collected line by line."""

    def __init__(self, root: Path, *args, env=None):
        self.proc = subprocess.Popen([sys.executable, "-m", "agent_testbench", "watch", *args], cwd=root,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     env={**os.environ, **(env or {})}, **procs.group_kwargs())
        self.lines: list[str] = []
        threading.Thread(target=lambda: [self.lines.append(ln.rstrip()) for ln in self.proc.stdout], daemon=True).start()

    def count(self, text: str) -> int:
        return sum(text in ln for ln in self.lines)

    def wait(self, text: str, n: int = 1, timeout: float = 60) -> None:
        end = time.time() + timeout
        while self.count(text) < n:
            assert time.time() < end and self.proc.poll() is None or self.count(text) >= n, \
                f"no {n} x {text!r}:\n" + "\n".join(self.lines)
            time.sleep(0.05)

    def interrupt(self, terminate=False) -> int:
        """Ctrl-C (a console break on Windows), or SIGTERM."""
        if os.name == "nt":
            self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.proc.send_signal(signal.SIGTERM if terminate else signal.SIGINT)
        return self.proc.wait(60)


def drivers_left(root: Path) -> list[str]:
    """Runs not closed, or whose driver is still there after a few seconds (it exits just after closing its run)."""
    from agent_testbench import runs
    end = time.time() + 15
    while True:
        left = [p.name for p in runs.all_runs(root) if runs.read_state(p).get("status") not in runs.DONE
                or procs.alive(runs.read_state(p).get("driver_pid"))]
        if not left or time.time() > end:
            return left
        time.sleep(0.2)


def test_watch_reruns_on_change_queues_one_run_and_stops_clean(proj, webhook):
    cfg = {"project": "proj", "notify": {"webhook": webhook.url, "on": ["run_end", "watch_run"]}, "plans": {"q": PLAN}}
    write_config(proj, cfg)
    w = Watching(proj, "q", "--debounce", "0.3", "--interval", "0.1")
    w.wait("] PASS run")                                     # the first run, at once
    assert "watching 4 files" in w.lines[0] and "what the plan writes (out/*, out/m.json)" in w.lines[0], w.lines[0]
    os.utime(proj / "score.py")                              # a touch is a change
    w.wait("(score.py changed)")
    w.wait("] PASS run", 2)
    (proj / "settings.py").write_text("import time; time.sleep(3)\nSCORE = 1\n")
    w.wait("(settings.py changed)")
    time.sleep(0.6)
    (proj / "settings.py").write_text("SCORE = 4\n")         # two changes while the run goes ...
    time.sleep(0.4)
    os.utime(proj / "score.py")
    w.wait("] FAIL run")
    w.wait("(score.py, settings.py changed)")                # ... make one more run, with both in it
    w.wait("] PASS run", 3)
    time.sleep(1.5)
    assert w.count("started (") == 4, "\n".join(w.lines)
    assert w.count("one more run when it ends") == 1
    assert "since " in w.lines[-1] and "fixed score; out/m.json: score >= 2: 1 -> 4 (better)" in w.lines[-1]

    (proj / "settings.py").write_text("import time; time.sleep(60)\nSCORE = 3\n")
    w.wait("started (", 5)
    time.sleep(0.5)
    assert w.interrupt() == 0
    assert "cancelling run" in "\n".join(w.lines) and "stopped after 5 run(s): 3 passed, 2 did not" in w.lines[-1]
    from agent_testbench import runs
    last = runs.all_runs(proj)[-1]
    assert runs.read_state(last)["status"] == "cancelled" and not drivers_left(proj)
    assert [e["status"] for e in webhook.events("watch_run")] == ["failed", "passed"], "only when the result changed"
    assert not webhook.events("run_end"), "a watch's runs send no run_end"


def test_a_terminated_watch_cancels_its_run(proj):
    (proj / "settings.py").write_text("import time; time.sleep(60)\nSCORE = 3\n")
    w = Watching(proj, "q", "--debounce", "0.3")
    w.wait("started (the first run")
    time.sleep(0.5)
    assert w.interrupt(terminate=True) == 0
    assert not drivers_left(proj) and "stopped after 1 run(s): 0 passed, 1 did not; the last was cancelled" in w.lines[-1]


def test_watch_leaves_out_caches_ignored_files_and_what_the_plan_writes(proj):
    from agent_testbench import config as C, watch as W
    (proj / ".gitignore").write_text("out/\n__pycache__/\n!keep.txt\n*\n**/data/**\n/build/\n")
    assert W.gitignore(proj) == ["out", "__pycache__", "data", "build"], "an allow-list's * is left out"
    for f in ("data/big.bin", ".pytest_cache/v", "notes.py.swp", "out/m.json", "sub/a.txt"):
        (proj / f).parent.mkdir(parents=True, exist_ok=True)
        (proj / f).write_text("x")
    write_config(proj, {"project": "proj", "plans": {"q": {**PLAN, "steps": [{**PLAN["steps"][0], "expect": {
        "junit": "reports/junit.xml", "json": {"m.json": {"a": 1}}, "text": {"stdout": ["x"], "log.txt": ["y"]}}}]}}})
    cfg = C.load(proj)
    assert W.outputs(cfg["plans"]["q"]) == ["out/*", "m.json", "log.txt", "reports/junit.xml"]
    assert sorted(W.snapshot(proj, W.left_out(proj, cfg, "q"))) == [".gitignore", "score.py", "settings.py", "sub/a.txt",
                                                                     "testbench.yaml"]
    assert W.git_ignored(proj, {"sub/a.txt", "score.py"}) == {"sub/a.txt"}, "git's own rules; tracked files count"

    said = []
    w = W.Watch(proj, cfg, "q", False, 0.1, 0.1, None, said.append)
    w.last_during = {"out.txt"}                              # a run rewrote it, and only it changed: three times
    assert [w.echoing({"out.txt"}) for _ in range(3)] == [False, False, True] and "not running again" in said[-1]
    assert not w.echoing({"out.txt", "score.py"}), "any other change runs"


def test_watch_needs_a_free_plan_or_a_persons_yes(proj, plugin):
    paid = {"backend": "fake", "steps": [{"name": "a", "run": "python -c pass", "max_minutes": 1}]}
    write_config(proj, {"project": "proj", "budget": {"max_day_usd": 0.1}, "plans": {"q": PLAN, "paid": paid}})
    r = cli(proj, "watch", "paid", env=plugin)
    assert r.returncode == 2 and r.stdout.startswith("Refused: plan paid can cost up to $0.20 a run (fake, 1 min)")
    assert "Only plans that cost nothing are watched without approval" in r.stdout and "--approve" in r.stdout
    r = cli(proj, "watch", "paid", "--approve", "--max-runs", "1", env=plugin)    # approved, and still in the limits
    assert r.returncode == 2 and "over budget.max_day_usd $0.10" in r.stdout and "Stopped: a run was refused" in r.stdout
    assert not (proj / ".testbench" / "runs").exists() or not list((proj / ".testbench" / "runs").iterdir())
    assert cli(proj, "watch", "q", "--max-runs", "1", env=plugin).returncode == 0, "a free plan needs no approval"

    bash = lambda cmd: guard({"cwd": str(proj), "tool_name": "Bash", "tool_input": {"command": cmd}})
    assert bash("testbench watch paid --approve") == "ask"
    assert bash("testbench watch --approve paid --debounce 2") == "ask"
    assert bash("F=--approve; testbench watch paid $F") == "ask"
    assert bash("testbench watch q") is None and bash("testbench diff q --json") is None
