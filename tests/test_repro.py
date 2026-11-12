"""testbench repro (a plan run N times inside the budget, its runs' evidence compared file by file, each difference
read for how and why) and the deterministic: expectation (files byte for byte what the last passing run of the plan on
the same code made)."""
import json
from pathlib import Path

import pytest

from agent_testbench import budget, config as C, expect as X, plugins as P, repro as RP
from conftest import cli, git, write_config
from trust_helpers import guard_module

MAKER = """\
import json, os, pathlib, sys
out = pathlib.Path("out")
out.mkdir(exist_ok=True)
extra = pathlib.Path("extra.txt").read_text().strip() if pathlib.Path("extra.txt").exists() else "3"
json.dump({"weights": [0.5, 0.25], "n": int(extra)}, open("out/model.json", "w"), indent=1)
(out / "table.csv").write_text("a,b\\n1,2\\n3,4\\n")
if "--noisy" in sys.argv:                  # what changes from run to run: a counter kept in the project, not the copy
    counter = pathlib.Path(os.environ["TESTBENCH_PROJECT_DIR"]) / "count.txt"
    k = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(k))
    (out / "stamp.txt").write_text(f"made at 2026-11-12T10:00:{k:02d}\\nrows 2\\n")
    json.dump({"accuracy": 0.93 + k * 1e-4, "rows": 2}, open("out/noise.json", "w"))
    words = ["alpha", "beta", "gamma", "delta", "eps"]
    (out / "order.txt").write_text("\\n".join(words[k % 5:] + words[:k % 5]) + "\\n")
print("made it")
"""


def plan(*args, **expect) -> dict:
    return {"steps": [{"name": "make", "run": " ".join(["python make.py", *args]), "max_minutes": 1,
                       "artifacts": ["out/*"], **({"expect": expect} if expect else {})}]}


@pytest.fixture
def proj(tmp_path) -> Path:
    """A project in git: `same` makes the same files every run; `noisy` adds a time, a drifting number and lines in
    another order, from a counter in the project (git-ignored, so the code stays the same)."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "make.py").write_text(MAKER)
    (root / ".gitignore").write_text("out/\ncount.txt\nextra.txt\n")
    write_config(root, {"project": "proj", "plans": {
        "same": plan(), "noisy": plan("--noisy"),
        "det": plan(deterministic=["out/model.json", "out/*.csv"])}})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "start")
    return root


def kept(root: Path) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((root / ".testbench" / "repro").glob("*/repro.json"))]


def launches(root: Path) -> list[dict]:
    p = root / ".testbench" / "ledger.jsonl"
    return [e for e in map(json.loads, p.read_text().splitlines()) if e["event"] == "launch"] if p.exists() else []


# ---- a repro, end to end -------------------------------------------------------------------------------------------

def test_a_deterministic_plan_reproduces(proj):
    r = cli(proj, "repro", "same")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "# Reproducibility: plan `same`, 2 runs - IDENTICAL" in r.stdout
    assert "IDENTICAL: every output the same, byte for byte (2 files)" in r.stdout
    assert "- the same code in every run: commit " in r.stdout and "- the same in every run: backend local" in r.stdout
    assert "identical in every run (2): `steps/01-make/artifacts/out/model.json`, " in r.stdout
    [d] = kept(proj)
    assert d["verdict"] == "identical" and d["reproduced"] and d["outputs"] == {
        "identical": 2, "differs": 0, "missing": 0, "unexplained": 0}
    assert d["launched"] == {"times": 2, "on": [], "approved": False, "worst_case_usd": 0.0}
    ids = [x["run_id"] for x in d["runs"]]
    assert [e["run"] for e in launches(proj)] == ids, "the budget saw each run"
    for run in ids:                                      # each a normal launch, in a fresh copy of the project
        state = json.loads((proj / ".testbench" / "runs" / run / "state.json").read_text())
        assert state["origin"] == {"repro": d["id"], "run": ids.index(run) + 1}
        assert json.loads((proj / ".testbench" / "runs" / run / "plan.json").read_text())["workdir"] == "copy"
    assert (proj / ".testbench" / "repro" / d["id"] / "repro.md").read_text().startswith("# Reproducibility")
    assert cli(proj, "ui").returncode == 0
    index = (proj / ".testbench" / "ui" / "index.html").read_text()
    assert "<h2>Reproducibility</h2>" in index and f"../repro/{d['id']}/report.html" in index and "IDENTICAL" in index
    assert (proj / ".testbench" / "repro" / d["id"] / "report.html").is_file()
    assert "testbench repro" not in cli(proj, "diff", "same").stdout, "the same outputs: nothing to explain"


def test_times_drifting_numbers_and_order_are_named(proj):
    r = cli(proj, "repro", "noisy", "--json")
    assert r.returncode == 1, r.stdout + r.stderr
    d = json.loads(r.stdout)                             # --json: only the JSON on stdout
    assert "[testbench] run 1 of 2" in r.stderr
    files = {f["path"].split("/")[-1]: f for f in d["files"]}
    assert files["model.json"]["status"] == files["table.csv"]["status"] == "identical"
    assert files["stamp.txt"]["reasons"] == ["timestamps"]
    assert "line 1: 'made at 2026-11-12T10:00:01' -> 'made at 2026-11-12T10:00:02'" in files["stamp.txt"]["summary"]
    assert files["noise.json"]["reasons"] == ["random"]
    assert files["noise.json"]["summary"].startswith("1 key differs, numbers by up to 0.0108%: accuracy: 0.9301 -> ")
    assert files["order.txt"]["reasons"] == ["ordering"]
    assert files["log.txt"]["group"] == "record" and files["log.txt"]["status"] == "identical"
    assert d["verdict"] == "different" and d["reasons"] == ["random", "ordering", "timestamps"]
    assert d["outputs"] == {"identical": 2, "differs": 3, "missing": 0, "unexplained": 1}
    md = (proj / ".testbench" / "repro" / d["id"] / "repro.md").read_text()
    assert "NOT REPRODUCED: of 5 outputs, 2 identical, 3 differ" in md
    assert "- numbers that differ by small relative amounts: a random seed not fixed: fix every seed" in md
    assert "- the same content in another order: sort before writing" in md
    # the same runs again: diff points to repro, which says the same
    a, b = d["runs"][0]["run_id"], d["runs"][1]["run_id"]
    assert f"yet 3 of their outputs differ: `testbench repro --compare {a} {b}`" in cli(proj, "diff", a, b).stdout
    again = cli(proj, "repro", "--compare", a, b)
    assert again.returncode == 1 and "NOT REPRODUCED: of 5 outputs, 2 identical, 3 differ" in again.stdout
    assert len(kept(proj)) == 2


def test_compare_runs_that_exist_and_a_shared_bundle(proj, tmp_path):
    for extra in ("3", "4"):                             # the same code, other input: the second run's n differs
        (proj / "extra.txt").write_text(extra)
        assert cli(proj, "run", "same", "--wait", "120").returncode == 0
    r = cli(proj, "repro", "--compare", "last~1", "last")
    assert r.returncode == 1, r.stdout + r.stderr
    assert ("- DIFFERS `steps/01-make/artifacts/out/model.json`: 1 key differs, numbers by up to 25%: n: 3 -> 4 (25%) "
            "- results that are not the same") in r.stdout
    runs = sorted(p.name for p in (proj / ".testbench" / "runs").iterdir())
    shared = cli(proj, "share", runs[0], "--to", str(tmp_path / "public"), "--yes")
    assert shared.returncode == 0, shared.stdout + shared.stderr
    bundle = tmp_path / "public" / runs[0]
    elsewhere = tmp_path / "elsewhere"                   # no project here: folders and bundles need none
    elsewhere.mkdir()
    r = cli(elsewhere, "repro", "--compare", str(bundle), str(proj / ".testbench" / "runs" / runs[1]))
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"- run 1: `{runs[0]}` (passed, local, from {bundle})" in r.stdout
    assert "n: 3 -> 4 (25%)" in r.stdout and "(Not kept: there is no project here.)" in r.stdout
    r = cli(proj, "repro", "--compare", str(bundle), runs[0])  # the bundle is the run it was made from
    assert r.returncode == 2 and "are the same run" in r.stdout
    (bundle / "steps" / "01-make" / "artifacts" / "out" / "model.json").write_text("{}")   # a bundle edited since
    r = cli(proj, "repro", "--compare", str(bundle), runs[1], "--json")
    pair = next(f for f in json.loads(r.stdout)["files"] if f["path"].endswith("model.json"))
    assert pair["reasons"] == ["unread"] and "is not here" in pair["summary"], "only bytes the run recorded are read"
    bad = cli(proj, "repro", "--compare", "last")
    assert bad.returncode == 2 and "at least two runs" in bad.stdout
    assert cli(proj, "repro", "same", "--compare", "a", "b").returncode == 2
    assert cli(proj, "repro").returncode == 2


# ---- the budget: every run counted, the total asked about once ---------------------------------------------------

def test_the_budget_sees_every_run_and_asks_once_for_the_total(tmp_path, plugin):
    root = tmp_path / "paid"
    root.mkdir()
    write_config(root, {"project": "paid", "budget": {"max_run_usd": 1.0, "ask_above_usd": 0.3, "max_day_usd": 5.0},
                        "plans": {"paid": {"backend": "fake", "steps": [
                            {"name": "a", "run": "python -c \"print('hi')\"", "max_minutes": 1}]},
                                  "here": {"steps": [{"name": "a", "run": "python -c \"print('hi')\"",
                                                      "max_minutes": 1}]}}})
    one = cli(root, "estimate", "paid", "--json", env=plugin)
    assert one.returncode == 0 and json.loads(one.stdout)["worst_case_usd"] == 0.2, "one run may launch"
    r = cli(root, "repro", "paid", env=plugin)
    assert r.returncode == 2 and r.stdout.startswith(
        "Needs approval: a repro of plan paid (2 runs) could cost up to $0.40 (above budget.ask_above_usd $0.30). Ask "
        "a person; only they approve it, with `testbench repro paid --approve`."), r.stdout
    assert not (root / ".testbench" / "runs").exists() and launches(root) == [], "nothing launched"
    r = cli(root, "repro", "here", "--on", "fake", env=plugin)         # another backend: priced as that one
    assert r.returncode == 2 and "a repro of plan here (2 runs) could cost up to $0.40" in r.stdout
    r = cli(root, "repro", "paid", "--times", "30", "--approve", env=plugin)
    assert r.returncode == 2 and "could add $6.00, over budget.max_day_usd $5.00" in r.stdout, "approved, still capped"
    assert cli(root, "repro", "paid", "--times", "1", env=plugin).returncode == 2
    r = cli(root, "repro", "here", "--on", "nowhere", env=plugin)
    assert r.returncode == 2 and "--on nowhere: no such backend" in r.stdout
    assert launches(root) == []
    r = cli(root, "repro", "paid", "--approve", env=plugin)            # a person said yes to the total
    assert r.returncode == 0, r.stdout + r.stderr
    assert [e["worst_case_usd"] for e in launches(root)] == [0.2, 0.2]
    states = [json.loads(p.read_text()) for p in sorted((root / ".testbench" / "runs").glob("*/state.json"))]
    assert [s["approved"] for s in states] == [True, True] and {s["backend"] for s in states} == {"fake"}


def test_check_together_holds_each_run_to_max_run_and_the_total_to_the_rest(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "budget": {"max_run_usd": 0.25, "ask_above_usd": 0.3, "max_day_usd": 1.0},
                        "plans": {"p": {"steps": [{"name": "a", "run": "true", "max_minutes": 1}]}}})
    cfg = C.load(root)
    est = lambda usd: {"what": "plan p", "plan": "p", "backend": "x", "max_minutes": 1.0, "usd_per_minute": usd,  # noqa
                       "worst_case_usd": usd}
    ok, msg, total = budget.check_together(cfg, [est(0.2), est(0.2)], False, "a repro of plan p (2 runs)", "tb r")
    assert not ok and msg.startswith("Needs approval: a repro of plan p (2 runs) could cost up to $0.40") \
        and total["worst_case_usd"] == 0.4 and total["runs"] == 2
    ok, msg, _ = budget.check_together(cfg, [est(0.2), est(0.2)], True, "a repro of plan p (2 runs)", "tb r")
    assert ok, "a total above max_run_usd is fine: each run is held to it, not the total"
    ok, msg, _ = budget.check_together(cfg, [est(0.2), est(0.26)], True, "a repro of plan p (2 runs)", "tb r")
    assert not ok and "over budget.max_run_usd $0.25" in msg
    ok, msg, _ = budget.check_together(cfg, [est(0.2)] * 6, True, "a repro of plan p (6 runs)", "tb r")
    assert not ok and "over budget.max_day_usd $1.00" in msg
    ok, msg, _ = budget.check_together(cfg, [est(0.2), est(float("nan"))], True, "a repro", "tb r")
    assert not ok and "cannot be priced" in msg
    assert budget.check_together(cfg, [], True, "a repro", "tb r")[0] is False


def test_the_guard_asks_before_an_approved_repro(machine_limits, tmp_path):
    g = guard_module(machine_limits)
    root = tmp_path / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"big": {"backend": "modal", "gpu": "H100", "steps": [
        {"name": "a", "run": "true"}]}}})
    bash = lambda command: g.decision({"cwd": str(root), "tool_name": "Bash",                     # noqa: E731
                                       "tool_input": {"command": command}})
    asked = bash("testbench repro big --approve")
    assert asked[0] == "ask" and "repro --approve` runs a plan several times" in asked[1]
    assert bash("testbench repro big --times 3 --on docker") is None, "without --approve testbench asks for the total"
    assert bash("testbench repro --compare last~1 last") is None


# ---- reading a difference ----------------------------------------------------------------------------------------

def test_json_numbers_drift_with_their_relative_difference():
    a = json.dumps({"accuracy": 0.9312, "loss": 0.2101, "history": {"loss": [0.5, 0.25]}, "elapsed_s": 1.2}).encode()
    b = json.dumps({"accuracy": 0.9287, "loss": 0.2154, "history": {"loss": [0.5, 0.2501]}, "elapsed_s": 1.9}).encode()
    e = RP.explain(a, b, "out/metrics.json")
    assert e["kind"] == "json" and e["reason"] == "random" and e["reasons"] == ["random", "timestamps"]
    keys = {k["key"]: k for k in e["detail"]["keys"]}
    assert keys["accuracy"]["relative"] == pytest.approx(0.0025 / 0.9312)
    assert keys["history.loss.1"]["relative"] == pytest.approx(0.0001 / 0.2501)
    assert keys["elapsed_s"]["reasons"] == ["timestamps"], "a duration's key says what it is"
    assert e["summary"].startswith("4 keys differ, numbers by up to 2.46%: accuracy: 0.9312 -> 0.9287 (0.268%); ")
    big = RP.explain(b'{"n": 3}', b'{"n": 4}', "m.json")
    assert big["reasons"] == ["values"] and "n: 3 -> 4 (25%)" in big["summary"]
    tiny = RP.explain(b'{"x": 1.0000000001}', b'{"x": 1.0000000002}', "m.json")
    assert tiny["reasons"] == ["rounding"]
    assert RP.explain(b'{"x": 1.0000000001}', b'{"x": 1.0000000002}', "m.json", same_env=False)["reasons"] == \
        ["platform"], "on another platform, drifting digits are the platform's"
    keys_moved = RP.explain(b'{"a": 1, "b": 2}', b'{"b": 2, "a": 1}', "m.json")
    assert keys_moved["reasons"] == ["ordering"] and keys_moved["summary"] == "the same data: keys in another order"
    spaced = RP.explain(b'{"a": 1}', b'{"a":1}\n', "m.json")
    assert spaced["reasons"] == ["encoding"]
    gone = RP.explain(b'{"a": 1, "b": 2}', b'{"a": 1}', "m.json")
    assert gone["reasons"] == ["content"] and "b only in A" in gone["summary"]


def test_text_differences_and_their_reasons():
    def why(a: str, b: str, path="out/log.txt", **kw):
        return RP.explain(a.encode(), b.encode(), path, **kw)
    assert why("a\nb\nc\n", "c\na\nb\n")["reasons"] == ["ordering"]
    assert why("a\nb\nc\n", "c\na\nb\n")["summary"] == \
        "the same 3 lines, in another order (line 1 is the first that moved)"
    assert why("started 2026-11-12 10:00:01\nok\n", "started 2026-11-12 10:00:09\nok\n")["reasons"] == ["timestamps"]
    assert why("epoch 3 took 1.25s\n", "epoch 3 took 1.31s\n")["reasons"] == ["timestamps"]
    assert why("saved /home/ada/proj/out/m.pt\n", "saved /tmp/xyz/proj/out/m.pt\n")["reasons"] == ["paths"]
    assert why("<Model object at 0x7f3a2b1c0d90>\n", "<Model object at 0x7f99aa1c0d00>\n")["reasons"] == ["ids"]
    drift = why("loss 0.2311 at step 10\n", "loss 0.2316 at step 10\n")
    assert drift["reasons"] == ["random"] and "1 number differs, by up to 0.216% (0.2311 -> 0.2316)" in drift["summary"]
    assert why("x\r\ny\r\n", "x\ny\n")["reasons"] == ["encoding"]
    hello = why("hello\nsame\n", "world\nsame\n")
    assert hello["reasons"] == ["content"] and hello["summary"] == "line 1: 'hello' -> 'world'; 1 of 2 lines differs"
    both = why("b 0.51 done in 2.0 s\na\n", "a\nb 0.52 done in 3.1 s\n")
    assert both["reasons"][0] == "random" and "timestamps" in both["reasons"]
    assert RP.explain(b"\x00\x01\x02\x03", b"\x00\x01\x09\x03", "w.bin")["summary"] == \
        "the first difference is at byte 2 (4 and 4 bytes)"


def test_csv_rows_columns_and_order():
    a = b"step,loss,when\n1,0.50,2026-01-01T00:00:01\n2,0.40,2026-01-01T00:00:02\n3,0.30,2026-01-01T00:00:03\n"
    b = b"step,loss,when\n1,0.50,2026-01-01T00:00:05\n2,0.40,2026-01-01T00:00:06\n3,0.31,2026-01-01T00:00:07\n"
    e = RP.explain(a, b, "out/log.csv")
    assert e["kind"] == "csv" and e["reasons"] == ["random", "timestamps"]
    assert e["detail"]["rows"] == [1, 2, 3] and e["detail"]["columns"] == ["when", "loss"]
    assert e["summary"].startswith("3 rows of 3 differ (row 1, 2, 3), in when, loss; numbers by up to 3.23%")
    shuffled = RP.explain(b"a,b\n1,2\n3,4\n", b"a,b\n3,4\n1,2\n", "o.csv")
    assert shuffled["reasons"] == ["ordering"] and shuffled["summary"] == "the same 2 rows, in another order"
    assert RP.explain(b"a,b\n1,2\n", b"b,a\n2,1\n", "o.csv")["reasons"] == ["ordering"]
    assert RP.explain(b"a,b\n1,2\n", b"a,b\n1,2\n3,4\n", "o.csv")["summary"] == "1 row -> 2"


def test_images_by_their_pixels(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    import io

    def png(color, **info):
        from PIL import PngImagePlugin
        meta = PngImagePlugin.PngInfo()
        for k, v in info.items():
            meta.add_text(k, v)
        buf = io.BytesIO()
        Image.new("RGB", (20, 20), color).save(buf, "PNG", pnginfo=meta)
        return buf.getvalue()
    same = RP.explain(png((10, 20, 30), Software="a 1.0"), png((10, 20, 30), Software="a 2.0"), "p.png")
    assert same["reasons"] == ["encoding"] and same["summary"].startswith("the same pixels")
    near = RP.explain(png((10, 20, 30)), png((11, 20, 30)), "p.png")
    assert near["reasons"] == ["random"] and "nearly the same picture" in near["summary"]
    assert RP.explain(png((10, 20, 30)), png((11, 20, 30)), "p.png", same_env=False)["reasons"] == ["platform"]
    far = RP.explain(png((0, 0, 0)), png((255, 255, 255)), "p.png")
    assert far["reasons"] == ["content"] and "mean pixel difference 1.0000" in far["summary"]


def test_environments_and_what_decides_the_verdict():
    def rep(**env):
        return {"backend": "local", "gpu": None,
                "environment": {"python": "3.12.1", "platform": "Linux", "machine": "x86_64", **env}}
    e = RP._environments([rep(), rep(python="3.11.9", package_versions={"numpy": "2.1"})])
    assert e["same"] is False and e["differences"] == [{"what": "python", "values": ["3.12.1", "3.11.9"]}]
    assert e["same_as_first"] == [True, False]
    assert RP.group_of("steps/01-x/log.txt") == RP.group_of("steps/01-x/result.json") == "record"
    assert RP.group_of("steps/01-x/artifacts/out/m.json") == RP.group_of("steps/01-x/executed.ipynb") == "output"
    assert RP.glob_match("out/a.csv", "out/*.csv") and not RP.glob_match("out/x/a.csv", "out/*.csv")
    assert RP.glob_match("out/x/a.csv", "out/**/*.csv") and RP.glob_match("out/a.csv", "out/**/*.csv")
    assert RP.glob_match("out/model.json", "./out/model.json")


# ---- deterministic: ----------------------------------------------------------------------------------------------

def test_deterministic_passes_first_then_compares_with_the_last_passing_run(proj):
    r = cli(proj, "run", "det", "--wait", "120")
    assert r.returncode == 0, r.stdout
    assert ("- make: deterministic out/model.json - 1 file, nothing to compare with yet: no earlier passing run of "
            "plan det on this code (commit ") in r.stdout and "Kept as evidence" in r.stdout
    first = sorted((proj / ".testbench" / "runs").iterdir())[-1]
    assert (first / "steps" / "01-make" / "artifacts" / "out" / "model.json").is_file()
    r = cli(proj, "run", "det", "--wait", "120")
    assert r.returncode == 0 and f"deterministic out/model.json - 1 file identical to run {first.name}'s" in r.stdout
    second = sorted((proj / ".testbench" / "runs").iterdir())[-1]
    plan = json.loads((second / "plan.json").read_text())
    against = plan["steps"][0]["expect"]["deterministic"]["against"]
    assert against["run"] == first.name and set(against["files"]) == {"out/model.json", "out/table.csv"}
    assert plan["steps"][0]["artifacts"] == ["out/*"], "kept by artifacts: already, so not named twice"
    (proj / "extra.txt").write_text("7")                 # git-ignored: the same code, but the output changes
    r = cli(proj, "run", "det", "--wait", "120")
    assert r.returncode == 1, r.stdout
    assert (f"- expected deterministic out/model.json: out/model.json differs from run {second.name}'s: 1 key differs, "
            "numbers by up to 57.1%: n: 3 -> 7 (57.1%) - results that are not the same") in r.stdout
    (proj / "extra.txt").unlink()                       # the failed run is no reference: the second still is
    r = cli(proj, "run", "det", "--wait", "120")
    assert r.returncode == 0 and f"deterministic out/model.json - 1 file identical to run {second.name}'s" in r.stdout
    (proj / "make.py").write_text(MAKER + "# a change\n")    # other code: nothing to compare with yet
    r = cli(proj, "run", "det", "--wait", "120")
    assert r.returncode == 0 and "with these uncommitted changes)" in r.stdout


def test_deterministic_check_on_its_own(tmp_path):
    chk = P.get("checks", "deterministic")
    got = []
    add = lambda name, ok, detail: got.append((name, ok, detail))                       # noqa: E731
    work, before = tmp_path / "work", tmp_path / "before"
    (work / "out").mkdir(parents=True)
    (before / "out").mkdir(parents=True)
    (work / "out" / "a.txt").write_text("one\ntwo\n")
    (before / "out" / "a.txt").write_text("one\nthree\n")
    import hashlib
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()                          # noqa: E731
    against = {"run": "20260101-000000-p", "dir": str(before), "environment": {},
               "files": {"out/a.txt": {"sha256": sha(before / "out" / "a.txt")},
                         "out/gone.txt": {"sha256": "0" * 64}}}
    chk.check({"files": ["out/*.txt"], "against": against, "none": ""}, workdir=work, result={}, add=add)
    [(name, ok, detail)] = got
    assert name == "deterministic out/*.txt" and not ok
    assert detail.startswith("out/a.txt differs from run 20260101-000000-p's: line 2: 'three' -> 'two'; 1 of 2 lines "
                             "differs - content that is not the same; out/gone.txt: run 20260101-000000-p made it")
    got.clear()
    against["files"] = {"out/a.txt": {"sha256": sha(work / "out" / "a.txt")}}
    chk.check({"files": ["out/a.txt", "out/missing.bin"], "against": against}, workdir=work, result={}, add=add)
    assert got == [("deterministic out/a.txt", True, "1 file identical to run 20260101-000000-p's"),
                   ("deterministic out/missing.bin", False, "missing")]
    got.clear()
    against["dir"] = str(tmp_path / "elsewhere")         # the earlier run's copy is not on this machine
    against["files"] = {"out/a.txt": {"sha256": "1" * 64}}
    chk.check({"files": ["out/a.txt"], "against": against}, workdir=work, result={}, add=add)
    assert not got[0][1] and "run 20260101-000000-p's copy is not here to read: `testbench repro --compare" in got[0][2]
    got.clear()
    chk.check(["out/a.txt"], workdir=work, result={}, add=add)       # a plan.json from before: nothing to compare
    assert got[0][1] and "nothing to compare with yet" in got[0][2]
    for bad in (None, [], "out/a.txt", ["/etc/passwd"], ["../x.json"], ["out/../../x"], ["C:\\x"], [""], [3]):
        with pytest.raises(C.ConfigError, match=r"^`w`"):
            chk.validate("w", bad)
    chk.validate("w", ["out/model.json", "out/**/*.csv", "a..b.txt"])


def test_deterministic_is_a_built_in_in_the_registry_schema_and_docs():
    assert "deterministic" in X.KINDS and "deterministic" in X.RUN_ORDER and "deterministic" in P.names("checks")
    assert P.check("checks", P.get("checks", "deterministic")) == [], "it keeps the contract"
    schema = json.loads(C.SCHEMA.read_text())["$defs"]["expect"]["properties"]["deterministic"]
    assert schema["type"] == "array"
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(schema)
    assert v.is_valid(["out/model.json", "out/*.csv"]) and v.is_valid(["a..b.txt"])
    assert not any(v.is_valid(x) for x in ([], ["/abs"], ["../up"], ["out/../../up"], ["~/x"], ["C:/x"]))
    docs = Path(__file__).resolve().parents[1] / "docs"
    assert "## deterministic" in (docs / "expectations.md").read_text()
    assert "reproducibility.md" in (docs.parent / "mkdocs.yml").read_text()
