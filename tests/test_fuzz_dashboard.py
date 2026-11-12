"""The dashboard asked for any path: dot-dot, percent-encoded slashes and dots, NUL, links from a run's evidence to
its state.json, to a session's kernel key or out of the project. It serves its pages, the static pages, reports and
step evidence from .testbench and nothing else, and no page shows a step's env values, a kernel key, the ledger or a
file outside the project. TESTBENCH_FUZZ_EXAMPLES=N runs N examples (tests/trust_helpers.py)."""
import json
import urllib.parse

import pytest

pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st  # noqa: E402

from agent_testbench import dashboard as D  # noqa: E402
from conftest import cli, write_config  # noqa: E402
from trust_helpers import fuzz  # noqa: E402

MARKS = ("PLAN-ENV-MARK", "KERNEL-MARK", "LEDGER-MARK", "OUTSIDE-MARK")


@pytest.fixture(scope="module")
def watched(tmp_path_factory):
    """A project with a finished run whose plan.json holds a marked env value, a session folder with a marked kernel
    key, a marked ledger line and a marked file outside .testbench; and links from the run's evidence to state.json,
    plan.json, the sessions folder and out of the project."""
    base = tmp_path_factory.mktemp("dash")
    root = base / "p"
    root.mkdir()
    write_config(root, {"project": "p", "plans": {"p": {"steps": [
        {"name": "a", "run": "python -c \"print('hello')\"", "env": {"HINT": "PLAN-ENV-MARK"}, "max_minutes": 1}]}}})
    r = cli(root, "run", "p", "--wait", "120", env={"TESTBENCH_USER_DIR": str(base / "user")})   # never ~/.testbench
    assert r.returncode == 0, r.stdout + r.stderr
    tb = root / ".testbench"
    [run] = (tb / "runs").iterdir()
    (tb / "sessions" / "main").mkdir(parents=True)
    (tb / "sessions" / "main" / "connection.json").write_text('{"key": "KERNEL-MARK"}')
    with (tb / "ledger.jsonl").open("a") as f:
        f.write(json.dumps({"time": "2020-01-01T00:00:00", "event": "launch", "run": "LEDGER-MARK",
                            "worst_case_usd": 0}) + "\n")
    (root / "outside.txt").write_text("OUTSIDE-MARK")
    step = next(run.glob("steps/*"))
    for name, target in (("state-link.txt", run / "state.json"), ("plan-link.txt", run / "plan.json"),
                         ("out-link.txt", root / "outside.txt"), ("dir-link", tb / "sessions")):
        try:
            (step / name).symlink_to(target)
        except OSError:
            pass                                       # Windows without the right to make links
    return D.Dashboard(root), run.name, step.name


PARTS = ["", ".", "..", "...", "runs", "RUN", "steps", "STEP", "log.txt", "state.json", "plan.json", "report.json",
         "report.md", "report.html", "state-link.txt", "plan-link.txt", "out-link.txt", "dir-link", "connection.json",
         "sessions", "main", "ledger.jsonl", "ui", "index.html", "loops", "live", "overview", ".testbench", "outside.txt",
         "%2e%2e", "%2E%2E%2F", "..%2f", "%2F", "%5c", "..\\", "C:", "\x00", "%00", "~", "dashboard.js", "p", "x" * 300]


def target(parts, run, step, encode):
    words = [p.replace("RUN", run).replace("STEP", step) for p in parts]
    if encode:
        words = [urllib.parse.quote(w, safe="") for w in words]
    return "/" + "/".join(words)


@fuzz(400)
@given(st.lists(st.sampled_from(PARTS), min_size=1, max_size=6), st.booleans(),
       st.sampled_from(["", "?step=x&offset=5", "?offset=-1&step=../../state.json"]))
def test_the_dashboard_serves_only_its_pages_reports_and_evidence(watched, parts, encode, query):
    dash, run, step = watched
    try:
        code, body, kind, _ = D.route(dash, target(parts, run, step, encode) + query)
    except Exception as exc:                                     # the server answers 500 with this, nothing more
        code, body = 500, f"{type(exc).__name__}: {exc}"
    if hasattr(body, "read_bytes"):
        real = body.resolve()
        rel = real.relative_to(dash.tb.resolve())                # raises if outside .testbench
        assert rel.parts[0] in ("ui", "runs", "loops") and real.name not in ("state.json", "plan.json"), rel
        assert "sessions" not in rel.parts and not real.name.startswith("ledger"), rel
        text = real.read_bytes().decode("utf-8", "replace")
    else:
        text = body if isinstance(body, str) else bytes(body).decode("utf-8", "replace")
    assert not [m for m in MARKS if m in text], (parts, code)


@pytest.mark.parametrize("host, answered", [
    ("127.0.0.1", True), ("localhost", True), ("LOCALHOST", True), ("127.0.0.1:8770", True), ("localhost:8770", True),
    ("", False), ("evil.example", False), ("127.0.0.1.evil.example", False), ("127.0.0.1:1", False),
    ("localhost.", False), ("[::1]", False), ("0.0.0.0", False), ("127.0.0.2", False), ("evil.example:8770", False),
    ("localhost:8770@evil.example", False), ("2130706433", False), ("127.0.0.1:8770.evil.example", False)])
def test_only_this_machine_s_names_are_answered(host, answered):
    """A page elsewhere that points its name at 127.0.0.1 (DNS rebinding) still sends its own name as the Host."""
    assert D._local(host, 8770) is answered
