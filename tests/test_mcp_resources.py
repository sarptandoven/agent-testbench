"""The MCP server's resources and prompts: an agent reads plans, runs, reports, evidence, the loop and the notes
without running a command, and nothing it asks for leads out of the project's .testbench, launches or writes."""
import asyncio
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import PNG_1PX, cli, git, write_config

pytest.importorskip("mcp")

from agent_testbench import mcp_resources as M, mcp_server  # noqa: E402

STRATEGY = """\
import base64, datetime as dt, json, os, random
from settings import DRIFT, SCORE, SEED

rng, day, equity = random.Random(SEED), dt.date(2024, 1, 1), 100.0
rows = [f"{day.isoformat()},{equity!r}"]
for _ in range(250):
    day += dt.timedelta(days=1)
    equity *= 1 + rng.gauss(DRIFT, 0.01)
    rows.append(f"{day.isoformat()},{equity!r}")
os.makedirs("out", exist_ok=True)
open("out/equity.csv", "w").write("time,equity\\n" + "\\n".join(rows) + "\\n")
open("out/plot.png", "wb").write(base64.b64decode(%r))
json.dump({"score": SCORE}, open("metrics.json", "w"))
print("score", SCORE)
"""
LOOP = ("loop", "start", "backtest", "--metric", "metrics.json:score", "--budget-usd", "0")


def settings(root: Path, score, seed) -> None:
    (root / "settings.py").write_text(f"SCORE = {score}\nSEED = {seed}\nDRIFT = 0.0\n")


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    """A strategy in git with three finished runs: one that failed, and a loop's baseline and kept try (so the open
    loop has a luck line), plus a line in the notes."""
    base = tmp_path_factory.mktemp("resources")
    root, env = base / "strat", {"TESTBENCH_USER_DIR": str(base / "user-testbench")}
    root.mkdir()
    (root / "strategy.py").write_text(STRATEGY % base64.b64encode(PNG_1PX).decode())
    (root / ".gitignore").write_text("metrics.json\nout/\n__pycache__/\n")
    write_config(root, {"project": "strat", "plans": {
        "backtest": {"description": "a year of daily bars", "steps": [
            {"name": "backtest", "run": "python strategy.py", "max_minutes": 1, "env": {"SECRETLESS": "env-value-42"},
             "artifacts": ["out/equity.csv", "out/plot.png"],
             "expect": {"json": {"metrics.json": {"score": ">= 0"}}}}]},
        "broken": {"steps": [{"name": "boom", "run": "python -c \"raise SystemExit('boom: no data')\"",
                              "max_minutes": 1}]}}})
    settings(root, 1, 1)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "start")
    assert cli(root, "run", "broken", env=env).returncode == 1
    r = cli(root, *LOOP, env=env)
    assert r.returncode == 0 and "BASELINE: score 1" in r.stdout, r.stdout + r.stderr
    settings(root, 2, 2)
    r = cli(root, "loop", "try", "seed 2", env=env)
    assert r.returncode == 0 and "KEPT try 1" in r.stdout, r.stdout + r.stderr
    assert cli(root, "note", "--", "seed 2 beats seed 1; the deflated Sharpe calls it luck", env=env).returncode == 0
    return root


def runs_of(root: Path) -> list[str]:
    return sorted(p.name for p in (root / ".testbench" / "runs").iterdir())


def read(uri: str):
    """(content, mime type) of one read through the server, in-process."""
    [c] = list(asyncio.run(mcp_server.server.read_resource(uri)))
    return c.content, c.mime_type


def refused(uri: str) -> str:
    with pytest.raises(Exception) as e:
        read(uri)
    assert type(e.value).__name__ in ("Refused", "ResourceNotFoundError"), repr(e.value)
    return str(e.value)


def prompt(name: str, args: dict | None = None) -> str:
    out = asyncio.run(mcp_server.server.get_prompt(name, args or {}))
    [msg] = out.messages
    return msg.content.text


def test_the_server_lists_its_resources_templates_and_prompts_over_stdio(project, machine_limits):
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from mcp.types import BlobResourceContents, PromptReference, ResourceTemplateReference
    failed, base, kept = runs_of(project)

    async def go():
        server = StdioServerParameters(command=sys.executable, args=["-m", "agent_testbench", "mcp"], cwd=str(project),
                                       env={**os.environ, "TESTBENCH_USER_DIR": str(machine_limits)})
        async with stdio_client(server) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                listed = [str(x.uri) for x in (await s.list_resources()).resources]
                templates = [t.uri_template for t in (await s.list_resource_templates()).resource_templates]
                prompts = {p.name: p for p in (await s.list_prompts()).prompts}
                report = (await s.read_resource(f"testbench://runs/{failed}/report")).contents[0]
                image = (await s.read_resource("testbench://runs/last/evidence/steps/01-backtest/artifacts/out/"
                                               "plot.png")).contents[0]
                said = (await s.get_prompt("find-why-this-fails", {"run": failed[:-3]})).messages[0].content.text
                runs_offered = (await s.complete(ResourceTemplateReference(type="ref/resource", uri=M.EVIDENCE),
                                                 {"name": "run_id", "value": ""})).completion.values
                paths = (await s.complete(ResourceTemplateReference(type="ref/resource", uri=M.EVIDENCE),
                                          {"name": "path", "value": "01-backtest/art"},
                                          {"run_id": kept})).completion.values
                plans = (await s.complete(PromptReference(type="ref/prompt", name="tune-with-a-loop"),
                                          {"name": "plan", "value": "b"})).completion.values
                try:
                    await s.read_resource(f"testbench://runs/{kept}/evidence/..%2Fstate.json")
                    no = None
                except Exception as exc:
                    no = str(exc)
        return listed, templates, prompts, report, image, said, runs_offered, paths, plans, no

    listed, templates, prompts, report, image, said, runs_offered, paths, plans, no = asyncio.run(go())
    assert listed == [M.PLANS, M.RUNS, M.LOOP, M.NOTES, M.SCHEMA]
    assert templates == [M.REPORT, M.REPORT_JSON, M.EVIDENCE]
    assert list(prompts) == ["prove-this-change", "find-why-this-fails", "tune-with-a-loop", "check-a-strategy"]
    assert [(a.name, a.required) for a in prompts["tune-with-a-loop"].arguments] == [("plan", True), ("metric", True)]
    assert [(a.name, a.required) for a in prompts["prove-this-change"].arguments] == [("plan", False)]
    assert report.mime_type == "text/markdown" and "# FAIL - plan `broken`" in report.text
    assert "boom: no data" in report.text
    assert isinstance(image, BlobResourceContents) and image.mime_type == "image/png", "an image comes back as an image"
    assert base64.b64decode(image.blob) == PNG_1PX
    assert f"run `{failed}`" in said and f"testbench://runs/{failed}/report" in said, "a prefix: the run it names"
    assert runs_offered == [kept, base, failed], "newest first"
    assert paths == ["01-backtest/artifacts/out/equity.csv", "01-backtest/artifacts/out/plot.png"]
    assert plans == ["backtest", "broken"]
    assert no and "evidence path '../state.json' is refused: it has an empty, . or .. part" in no, no


def test_each_resource_reads_what_the_project_has(project, monkeypatch):
    monkeypatch.chdir(project)
    failed, base, kept = runs_of(project)
    plans, mime = read(M.PLANS)
    plans = json.loads(plans)
    assert mime == "application/json" and plans["project"] == "strat"
    backtest = plans["plans"][0]
    assert backtest["plan"] == "backtest" and backtest["backend"] == "local" and backtest["worst_case_usd"] == 0
    assert backtest["launch"] == "free: runs without asking" and backtest["steps"][0]["what"] == "python strategy.py"
    assert "env-value-42" not in json.dumps(plans) and "SECRETLESS" not in json.dumps(plans), "never a step's env"

    text, _ = read(M.RUNS)
    lines = text.splitlines()
    assert lines[0].startswith("3 runs in ") and [ln.split()[0] for ln in lines[1:]] == [failed, base, kept]
    assert " failed " in lines[1] and " passed " in lines[3]

    text, mime = read("testbench://runs/last~2/report")
    assert mime == "text/markdown" and text.startswith("# FAIL - plan `broken`") and "boom: no data" in text
    text, mime = read(f"testbench://runs/{kept[:15]}/report.json")
    rep = json.loads(text)
    assert mime == "application/json" and rep["run_id"] == kept and rep["status"] == "passed"
    assert {e["path"] for e in rep["evidence"]} >= {"steps/01-backtest/artifacts/out/plot.png"}

    log, mime = read(f"testbench://runs/{kept}/evidence/01-backtest/log.txt")
    assert mime == "text/plain" and log.strip() == "score 2"
    csv, mime = read(f"testbench://runs/{kept}/evidence/steps/01-backtest/artifacts/out/equity.csv")
    assert mime == "text/csv" and csv.replace("\r\n", "\n").startswith("time,equity\n2024-01-01,100.0\n")
    png, mime = read(f"testbench://runs/{kept}/evidence/01-backtest/artifacts/out/plot.png")
    assert mime == "image/png" and png == PNG_1PX

    loop, _ = read(M.LOOP)
    assert "(running) on plan backtest: max score in metrics.json; best 2 at" in loop, loop
    assert "\nluck: best of 2 tries: deflated Sharpe " in loop and "baseline" in loop and "kept" in loop
    notes, mime = read(M.NOTES)
    assert mime == "text/markdown" and "seed 2 beats seed 1; the deflated Sharpe calls it luck" in notes
    schema, _ = read(M.SCHEMA)
    assert json.loads(schema)["$defs"]["expect"]


def tree(*folders: Path) -> dict:
    """Every file and folder under folders: its size, time and sha256, to see that nothing changed."""
    out = {}
    for top in folders:
        for p in sorted(top.rglob("*")):
            st = p.lstat()
            out[str(p)] = (st.st_size, st.st_mtime_ns,
                           hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() and not p.is_symlink() else None)
    return out


def test_reading_everything_launches_writes_and_judges_nothing(project, monkeypatch, machine_limits):
    from agent_testbench import budget, loop, runs
    monkeypatch.chdir(project)
    for module, name in ((runs, "launch"), (runs, "write_state"), (runs, "cancel"), (budget, "record"),
                         (loop, "judge"), (loop, "_save"), (loop, "_revert"), (loop, "_git")):
        monkeypatch.setattr(module, name, lambda *a, **k: pytest.fail(f"a read called {name}"))
    before = tree(project, machine_limits.parent)
    uris = [M.PLANS, M.RUNS, M.LOOP, M.NOTES, M.SCHEMA]
    for run in runs_of(project):
        rep = json.loads(read(f"testbench://runs/{run}/report.json")[0])
        uris += [f"testbench://runs/{run}/report"] + [f"testbench://runs/{run}/evidence/{e['path']}"
                                                         for e in rep["evidence"]]
    for uri in uris:
        read(uri)
    refused("testbench://runs/last/evidence/../state.json")
    for name, fn, _ in M.PROMPTS:
        prompt(name, {"plan": "backtest", "metric": "metrics.json:score"} if name == "tune-with-a-loop" else {})
    for arg, known in (("plan", {}), ("run", {}), ("run_id", {}), ("path", {"run_id": "last"})):
        assert M.suggest(arg, "", known)
    assert len(uris) > 12 and tree(project, machine_limits.parent) == before


@pytest.mark.parametrize("uri, says", [
    ("testbench://runs/last/evidence/../state.json", "has an empty, . or .. part"),
    ("testbench://runs/last/evidence/01-backtest/..%2F..%2Fplan.json", "has an empty, . or .. part"),
    ("testbench://runs/last/evidence/%2Fetc%2Fpasswd", "it is absolute"),
    ("testbench://runs/last/evidence//etc/passwd", "it is absolute"),
    ("testbench://runs/last/evidence/~/.ssh/id_rsa", "it is absolute"),
    ("testbench://runs/last/evidence/C:/Windows/win.ini", "it is absolute"),
    ("testbench://runs/last/evidence/01-backtest%5C..%5C..%5Cstate.json", "has a NUL or a backslash"),
    ("testbench://runs/last/evidence/01-backtest/%00log.txt", "has a NUL or a backslash"),
    ("testbench://runs/last/evidence/01-backtest/.hidden", "a hidden name"),
    ("testbench://runs/last/evidence/steps", "names the steps/ folder itself"),
    ("testbench://runs/last/evidence/01-backtest/nothing.txt", "has no evidence file steps/01-backtest/nothing.txt"),
    ("testbench://runs/last/evidence/01-backtest", "is not a file"),
    ("testbench://runs/..%2F..%2Fsecrets/report", "'../../secrets' is not a run testbench knows"),
    ("testbench://runs/--approve/report", "'--approve' is not a run testbench knows"),
    ("testbench://runs/19990101/report", "No run '19990101'"),
    ("testbench://runs/backtest/report.json", "backtest is a plan, not a run; its latest run is"),
    ("testbench://runs/last~9/report", "last~9 is before the first run"),
    ("testbench://runs/last/state.json", "Unknown resource"),
    ("testbench://runs/last/plan.json", "Unknown resource"),
    ("testbench://ledger", "Unknown resource"),
])
def test_what_a_read_refuses_and_says_why(project, monkeypatch, uri, says):
    monkeypatch.chdir(project)
    message = refused(uri)
    assert says in message, message
    assert "driver_pid" not in message and "SECRETLESS" not in message


@pytest.mark.posix
def test_links_never_lead_out_of_the_evidence_folder(project, tmp_path, monkeypatch):
    root = tmp_path / "linked"
    shutil.copytree(project, root, symlinks=True)
    monkeypatch.chdir(root)
    last = root / ".testbench" / "runs" / runs_of(root)[-1]
    step = last / "steps" / "01-backtest"
    outside = tmp_path / "outside.txt"
    outside.write_text("a file outside the project")
    (step / "state.txt").symlink_to(last / "state.json")
    (step / "away.txt").symlink_to(outside)
    (step / "same.csv").symlink_to(step / "artifacts" / "out" / "equity.csv")
    for name in ("state.txt", "away.txt"):
        message = refused(f"testbench://runs/last/evidence/01-backtest/{name}")
        assert "is a link to a file it may not lead to; it is not read" in message, message
    assert read("testbench://runs/last/evidence/01-backtest/same.csv")[0].startswith("time,equity"), \
        "a link from one evidence file to another stays inside"

    (last / "report.md").unlink()
    (last / "report.md").symlink_to(last / "state.json")
    assert "report.md is a link to a file it may not lead to" in refused("testbench://runs/last/report")
    notes = root / ".testbench" / "NOTES.md"
    notes.unlink()
    notes.symlink_to(outside)
    assert ".testbench/NOTES.md is a link to a file it may not lead to" in refused(M.NOTES)

    first = root / ".testbench" / "runs" / runs_of(root)[0]
    shutil.rmtree(first / "steps")
    (first / "steps").symlink_to(first)
    assert "steps/ folder is a link; its evidence is not read" in \
        refused(f"testbench://runs/{first.name}/evidence/state.json")

    elsewhere = tmp_path / "elsewhere-run"
    shutil.copytree(first, elsewhere, symlinks=True)
    (root / ".testbench" / "runs" / "29990101-000000-backtest").symlink_to(elsewhere)
    assert "is a link out of .testbench/runs; only runs kept there are read" in refused(
        "testbench://runs/29990101-000000-backtest/report")


def test_long_and_large_files(project, monkeypatch):
    monkeypatch.chdir(project)
    monkeypatch.setattr(M, "MAX_TEXT", 40)
    text, _ = read("testbench://runs/last/evidence/01-backtest/artifacts/out/equity.csv")
    first, rest = text.split("\n", 1)
    assert first.startswith("[the first ") and "bytes are not shown here, only the end; .testbench/runs/" in first
    assert rest.endswith("\n") and len(rest) <= 40 and rest.count(",") >= 1, "the end, from a whole line"
    monkeypatch.setattr(M, "MAX_BLOB", 10)
    message = refused("testbench://runs/last/evidence/01-backtest/artifacts/out/plot.png")
    assert f"is {len(PNG_1PX)} bytes, more than the 10 a read returns as an image; open .testbench/runs/" in message


def test_prompts_render_with_and_without_arguments(project, monkeypatch, tmp_path):
    monkeypatch.chdir(project)
    failed = runs_of(project)[0]
    for name, fn, description in M.PROMPTS:
        args = {"plan": "backtest", "metric": "metrics.json:score"} if name == "tune-with-a-loop" else {}
        text = prompt(name, args)
        assert "never set approved_by_person=true yourself" in text, name
        assert len(text) < 3000 and description, "short, plain and described"
    proof = prompt("prove-this-change", {"plan": "backtest"})
    assert "Use plan `backtest`." in proof and "the run id whose passing report proves it, what it cost" in proof
    assert "Pick the plan that exercises this change" in prompt("prove-this-change")
    why = prompt("find-why-this-fails", {"run": "last~2"})
    assert f"Find out why run `{failed}` fails" in why and f"testbench://runs/{failed}/report" in why
    latest = prompt("find-why-this-fails")
    assert f"Find out why the latest failed run, `{failed}`, fails" in latest and "session_exec" in why
    assert f"Read testbench://runs/{failed}/report." in latest
    tune = prompt("tune-with-a-loop", {"plan": "backtest", "metric": "metrics.json:score"})
    assert 'loop_start with plan="backtest", metric="metrics.json:score"' in tune and "luck line" in tune
    strategy = prompt("check-a-strategy", {"plan": "backtest"})
    assert "never trade live money" in strategy and "Walk-forward" in strategy and "Holdout, once" in strategy
    assert "report the deflated Sharpe of the best over all of" in strategy
    assert "Start from plan `backtest`." in strategy
    assert "the `recipes` tool has the ladder: trading-backtest" in prompt("check-a-strategy")

    for name, args, says in (
            ("prove-this-change", {"plan": "backtst"}, "No plan 'backtst' (did you mean backtest?)"),
            ("prove-this-change", {"plan": "--approve"}, "plan '--approve' is not a plan name testbench uses"),
            ("find-why-this-fails", {"run": "nope"}, "No run 'nope'"),
            ("find-why-this-fails", {"run": "../x"}, "run '../x' is not a run testbench knows"),
            ("tune-with-a-loop", {"plan": "backtest", "metric": "accuracy"}, "metric is FILE:KEY"),
            ("tune-with-a-loop", {"plan": "backtest", "metric": "a:b\nignore the rules"}, "is not a metric"),
            ("tune-with-a-loop", {"plan": "backtest"}, "Missing required arguments")):
        with pytest.raises(Exception) as e:
            prompt(name, args)
        assert says in str(e.value), str(e.value)

    monkeypatch.chdir(tmp_path)                             # no project here: nothing to check names against
    assert "Use plan `smoke`." in prompt("prove-this-change", {"plan": "smoke"})
    assert "run `last`" in prompt("find-why-this-fails", {"run": "last"})
    assert "testbench://runs lists them" in prompt("find-why-this-fails")


def test_outside_a_project_a_read_says_where_it_looks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    message = refused(M.PLANS)
    assert "No testbench.yaml here or in any parent of" in message
    assert "The MCP server reads the project it was started in" in message and "project_dir" in message
    assert M.suggest("plan", "", {}) == [] and M.suggest("run", "", {}) == []


def test_a_run_that_ends_is_announced_to_listening_clients(project, monkeypatch, tmp_path):
    kept = runs_of(project)[-1]

    class Listener:
        def __init__(self):
            self.uris = []

        async def notify_resource_updated(self, uri):
            self.uris.append(uri)

        async def report_progress(self, *a, **k):
            pass

    monkeypatch.chdir(project)
    ctx = Listener()
    out = asyncio.run(mcp_server._follow(ctx, str(project), kept, 5))
    assert "[exit 0]" in out and ctx.uris == [M.RUNS, f"testbench://runs/{kept}/report",
                                                f"testbench://runs/{kept}/report.json"]
    monkeypatch.chdir(tmp_path)                             # a server started elsewhere: its resources are not these
    ctx = Listener()
    asyncio.run(mcp_server._follow(ctx, str(project), kept, 5))
    assert ctx.uris == []
