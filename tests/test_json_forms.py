"""The --json forms editors and scripts read (the VS Code extension in editors/vscode): `plans`, `runs`, `spend` and
`loop status`. Each prints JSON only, and `loop status --json` only reads: it never keeps or reverts a try."""
import json
import time

from conftest import SCORER, cli, git, score, write_config


def _json(root, *args, code=0):
    r = cli(root, *args, "--json")
    assert r.returncode == code, f"testbench {' '.join(args)} --json: exit {r.returncode}\n{r.stdout}{r.stderr}"
    return json.loads(r.stdout)


def test_plans_json_says_whether_each_plan_may_launch_now(tmp_path):
    write_config(tmp_path, {"project": "priced", "budget": {"max_run_usd": 2.0, "ask_above_usd": 0.5,
                                                            "max_day_usd": 10.0},
                            "plans": {"free": "python -c 'print(1)'",
                                      "cheap": {"backend": "modal", "gpu": "T4", "description": "a short check",
                                                "steps": [{"name": "t", "run": "python t.py", "max_minutes": 20}]},
                                      "asks": {"backend": "modal", "gpu": "T4",
                                               "steps": [{"name": "t", "run": "python t.py", "max_minutes": 60}]},
                                      "huge": {"backend": "modal", "gpu": "H100",
                                               "steps": [{"name": "t", "run": "python t.py", "max_minutes": 600}]}}})
    j = _json(tmp_path, "plans")
    assert j["project"] == "priced" and j["spent_today_usd"] == 0
    assert j["limits"]["ask_above_usd"] == {"usd": 0.5, "where": "budget.ask_above_usd $0.50"}
    plans = {p["name"]: p for p in j["plans"]}
    assert list(plans) == ["free", "cheap", "asks", "huge"], "in the order testbench.yaml lists them"
    assert plans["free"] | {"message": ""} == {"name": "free", "backend": "local", "gpu": None, "description": None,
                                               "steps": ["free"], "max_minutes": 10.0, "worst_case_usd": 0.0,
                                               "launch": "allowed", "message": ""}
    assert plans["cheap"]["launch"] == "allowed" and 0 < plans["cheap"]["worst_case_usd"] <= 0.5
    assert plans["cheap"]["description"] == "a short check" and plans["cheap"]["gpu"] == "T4"
    assert plans["asks"]["launch"] == "needs_approval" and 0.5 < plans["asks"]["worst_case_usd"] <= 2.0
    assert plans["asks"]["message"].startswith("Needs approval: plan asks could cost up to $")
    assert "`testbench run asks --approve`" in plans["asks"]["message"]
    assert plans["huge"]["launch"] == "refused" and plans["huge"]["message"].startswith("Refused: plan huge could cost")
    assert _json(tmp_path, "plans") == json.loads(cli(tmp_path, "plans", "--format", "json").stdout)


def test_plans_json_counts_the_machine_limits(tmp_path, machine_limits):
    machine_limits.mkdir(parents=True, exist_ok=True)
    (machine_limits / "limits.yaml").write_text("ask_above_usd: 0.10\n")
    write_config(tmp_path, {"project": "capped", "plans": {
        "cheap": {"backend": "modal", "gpu": "T4", "steps": [{"name": "t", "run": "python t.py", "max_minutes": 20}]}}})
    j = _json(tmp_path, "plans")
    assert j["limits"]["ask_above_usd"]["usd"] == 0.10 and "this machine's limit" in j["limits"]["ask_above_usd"]["where"]
    assert j["plans"][0]["launch"] == "needs_approval", "the machine's lower threshold applies"


def test_runs_json_lists_runs_oldest_first_with_their_folders(tmp_path):
    write_config(tmp_path, {"project": "r", "plans": {"ok": "python -c 'print(1)'", "bad": "python -c 'exit(3)'"}})
    assert cli(tmp_path, "run", "ok").returncode == 0
    assert cli(tmp_path, "run", "bad").returncode == 1
    lost = tmp_path / ".testbench" / "runs" / "20000101-000000-ok"     # a run whose driver went away long ago
    lost.mkdir()
    (lost / "state.json").write_text(json.dumps({"run_id": lost.name, "plan": "ok", "status": "running",
                                                 "backend": "local", "created": 1.0, "worst_case_usd": 0.0}))
    runs = _json(tmp_path, "runs")["runs"]
    assert [(r["plan"], r["status"]) for r in runs] == [("ok", "lost"), ("ok", "passed"), ("bad", "failed")]
    old, ok, bad = runs
    assert ok["dir"] == str((tmp_path / ".testbench" / "runs" / ok["run_id"]).resolve()) and ok["has_report"]
    assert ok["cost_usd"] == 0.0 and ok["approved"] is False and ok["backend"] == "local" and ok["step"] is None
    assert ok["line"].startswith(f"{ok['run_id']}  passed") and not old["has_report"]
    assert [r["run_id"] for r in _json(tmp_path, "runs", "-n", "1")["runs"]] == [bad["run_id"]]


def test_spend_json_is_today_against_the_day_limits(tmp_path, machine_limits):
    machine_limits.mkdir(parents=True, exist_ok=True)
    (machine_limits / "limits.yaml").write_text("max_day_usd: 20\n")
    write_config(tmp_path, {"project": "s", "budget": {"max_day_usd": 4}, "plans": {"ok": "python -c 'print(1)'"}})
    cli(tmp_path, "run", "ok")
    today = time.strftime("%Y-%m-%dT%H")
    (machine_limits / "ledger.jsonl").open("a").write(json.dumps(       # another project, still running
        {"time": f"{today}:00:00", "event": "launch", "run": "x", "worst_case_usd": 1.25, "root": "/elsewhere"}) + "\n")
    j = _json(tmp_path, "spend")
    assert j["project"] == {"today_usd": 0.0, "day_limit_usd": 4.0, "where": "budget.max_day_usd in testbench.yaml"}
    assert j["machine"]["today_usd"] == 1.25 and j["machine"]["day_limit_usd"] == 20.0
    assert j["machine"]["where"].endswith("limits.yaml") and j["finished"] == 1 and j["total_usd"] == 0.0
    assert [e["status"] for e in j["latest"]] == ["passed"] and set(j["latest"][0]) == {
        "time", "run", "backend", "status", "minutes", "usd"}
    r = cli(tmp_path, "spend", "--json", "--modal")
    assert r.returncode == 2 and "`testbench spend --modal` without --json" in r.stderr


def test_loop_status_json_only_reads(repo):
    assert _json(repo, "loop", "status") == {"loop": None}
    assert cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0").returncode == 0
    (repo / "score.py").write_text(SCORER.replace('print("score", SCORE)', 'import time; time.sleep(3); print("score", SCORE)'))
    score(repo, 0)
    assert cli(repo, "loop", "try", "slower and worse", "--wait", "0").returncode == 3
    j = _json(repo, "loop", "status", code=3)["loop"]
    assert j["status"] == "running" and j["pending"] == 1 and [t["mark"] for t in j["tries"]] == ["baseline", "running"]
    assert j["best"]["value"] == 1 and j["key"] == "score" and j["goal"] == "max" and j["spent_usd"] == 0
    assert 0 < j["hours_left"] <= 8 and j["luck"] == [] and j["luck_why"] is None
    assert cli(repo, "wait", j["tries"][1]["run"], "--timeout", "60").returncode == 0
    state = next((repo / ".testbench" / "loops").glob("*/state.json"))
    before, head = state.read_text(), git(repo, "rev-parse", "HEAD")
    j = _json(repo, "loop", "status", code=3)["loop"]
    assert j["pending_run_ended"] and j["tries"][1]["mark"] == "running", "a try whose run ended is not judged here"
    assert state.read_text() == before and git(repo, "rev-parse", "HEAD") == head
    assert (repo / "settings.py").read_text() == "SCORE = 0\n", "nor reverted"
    assert cli(repo, "loop", "status").returncode == 0                 # the text form judges it
    j = _json(repo, "loop", "status")["loop"]
    assert [t["mark"] for t in j["tries"]] == ["baseline", "reverted"] and j["pending"] is None
    assert j["tries"][1]["verdict"].startswith("REVERTED try 1: score 0 is not better than the best 1")
    assert cli(repo, "loop", "stop").returncode == 0
    j = _json(repo, "loop", "status")["loop"]
    assert j["status"] == "stopped" and j["hours_left"] is None, "the latest loop, once none is running"


def test_json_forms_never_print_nan(tmp_path):
    write_config(tmp_path, {"project": "n", "plans": {"ok": "python -c 'print(1)'"}})
    cli(tmp_path, "run", "ok")
    run = next((tmp_path / ".testbench" / "runs").iterdir())
    state = json.loads((run / "state.json").read_text())
    (run / "state.json").write_text(json.dumps({**state, "cost_usd": float("nan")}))
    r = cli(tmp_path, "runs", "--json")
    assert "NaN" not in r.stdout and json.loads(r.stdout)["runs"][0]["cost_usd"] is None
