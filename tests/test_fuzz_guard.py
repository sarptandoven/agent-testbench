"""The guard hook against generated commands: --approve in disguise (quotes, backslashes, variables, subshells, env,
xargs, `bash -c`, Python) always becomes a question to the person, the same launches without it pass untouched, and
no command or edit makes the hook fail (a failing hook gives no decision, which would let the command through).
TESTBENCH_FUZZ_EXAMPLES=N runs N examples of each (tests/trust_helpers.py)."""
import json
import math
import re

import pytest
import yaml

pytest.importorskip("hypothesis")
from hypothesis import given, strategies as st  # noqa: E402

from trust_helpers import decided, fuzz, guard_module  # noqa: E402

PROGS = ["testbench", "agent-testbench", "python -m agent_testbench", "python3 -m agent_testbench",
         "python -m agent_testbench.cli", "/usr/local/bin/testbench", "~/.local/bin/testbench", "./.venv/bin/testbench",
         "uvx --from agent-testbench testbench", "env A=1 testbench", "nohup testbench", "time testbench",
         "exec testbench", "command testbench", "testbench --all", "nice -n 5 testbench", "timeout 600 testbench"]
LAUNCHES = [("run", "big"), ("watch", "big --debounce 2"), ("session start", ""), ("session start", "gpu --backend modal"),
            ("loop start", "big --metric m.json:x --budget-usd 9")]
FLAGS = ["--approve", "'--approve'", '"--approve"', '--app""rove', "--appr''ove", "--\\approve", "\\-\\-approve",
         "$'--approve'", '$"--approve"', "-\\-approve", '"--"approve', "--approve=1"]
VIA_VARIABLE = ["$F", "${F}", '"$F"', "$(echo --approve)", "`echo --approve`", "$(printf -- --approve)", "$(cat flags)"]
SET_VARIABLE = ["", "F=--approve; ", "export F=--approve && ", "F='--approve'\n", "read F < flags; "]
WRAPPERS = ["{c}", "({c})", "{{ {c}; }}", "cd /tmp && {c}", "true; {c}", "{c} &", "{c} 2>&1 | tee run.log",
            "if true; then {c}; fi", "for i in 1; do {c}; done", "echo start\n{c}", 'eval "{c}"', "bash -c '{c}'",
            'sh -c "{c}"', "bash -lc '{c}'", "sudo -E {c}", "{c} || echo failed", "set -e; {c}"]


def command(prog: str, launch: tuple, flag: str, first: bool) -> str:
    sub, rest = launch
    words = [prog, sub, *([flag] if first else []), rest, *([] if first else [flag])]
    return " ".join(w for w in words if w)


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "p"
    root.mkdir()
    (root / "testbench.yaml").write_text(yaml.safe_dump({"project": "p", "plans": {"big": {
        "backend": "modal", "gpu": "H100", "steps": [{"name": "a", "run": "true", "max_minutes": 60}]}}}))
    return root


@pytest.fixture
def G(machine_limits):
    return guard_module(machine_limits)


@fuzz(400)
@given(st.sampled_from(PROGS), st.sampled_from(LAUNCHES), st.sampled_from(FLAGS), st.booleans(),
       st.sampled_from(WRAPPERS), st.booleans())
def test_approve_in_disguise_always_asks(G, project, prog, launch, flag, first, wrapper, inside):
    c = wrapper.format(c=command(prog, launch, flag, first))
    assert decided(G, project if inside else project.parent, c) == "ask", c


@fuzz(300)
@given(st.sampled_from(PROGS), st.sampled_from(LAUNCHES), st.sampled_from(VIA_VARIABLE), st.sampled_from(SET_VARIABLE),
       st.sampled_from(WRAPPERS))
def test_approve_through_a_variable_or_subshell_asks(G, project, prog, launch, flag, setter, wrapper):
    c = setter + wrapper.format(c=command(prog, launch, flag, False))
    assert decided(G, project, c) == "ask", c


@fuzz(200)
@given(st.sampled_from(PROGS), st.sampled_from(LAUNCHES), st.sampled_from(FLAGS + ["--approve"]),
       st.sampled_from(["echo {f} | xargs {p} {l}", "printf '%s\\n' {f} | xargs -r {p} {l}", "xargs -a flags {p} {l}",
                        "echo big {f} | parallel {p} {l}", "find . -name flags -exec {p} {l} {{}} \\;"]))
def test_arguments_from_xargs_or_find_ask(G, project, prog, launch, flag, form):
    c = form.format(f=flag, p=prog, l=" ".join(launch).strip())
    assert decided(G, project, c) == "ask", c


@fuzz(100)
@given(st.sampled_from(["approve=True", "approve = True", "approve=1", "approved_by_person=True"]),
       st.sampled_from(["from agent_testbench.api import Testbench; Testbench('.').run('big', {a})",
                        "import agent_testbench.core as c; c.launch('.', c.load('.'), 'big', {a})",
                        "from agent_testbench import api; api.Testbench('.').loop_start('big', 'm:x', 9, {a})"]),
       st.sampled_from(['python -c "{s}"', "python3 - <<'EOF'\n{s}\nEOF", "uv run python -c \"{s}\""]))
def test_approve_from_python_asks(G, project, approve, script, form):
    c = form.format(s=script.format(a=approve))
    assert decided(G, project, c) == "ask", c


@fuzz(300)
@given(st.sampled_from(PROGS), st.sampled_from(LAUNCHES), st.booleans(),
       st.sampled_from([w for w in WRAPPERS if "$" not in w]))
def test_the_same_launches_without_approve_pass(G, project, prog, launch, inside, wrapper):
    c = wrapper.format(c=command(prog, launch, "", False))
    assert decided(G, project if inside else project.parent, c) is None, c
    est = wrapper.format(c=f"{prog} estimate big --approve")
    assert decided(G, project, est) is None, "an estimate as if approved spends nothing: " + est


ODD = st.sampled_from(["--approve", "'", '"', "\\", "$", "`", "(", ")", "{", "}", ";", "&", "|", "\n", "\x00", "\ud800",
                       "testbench", " run ", " big", "=", "$'", "\\\n", "xargs ", "TESTBENCH_USER_DIR=", "ledger.jsonl",
                       "> ", "limits.yaml", "rm ", "-exec "])


@fuzz(500)
@given(st.lists(st.one_of(ODD, st.text(max_size=6)), max_size=14).map("".join))
def test_no_command_makes_the_hook_fail(G, project, text):
    for cwd in (project, project.parent):
        got = G.decision({"cwd": str(cwd), "tool_name": "Bash", "tool_input": {"command": text}})
        assert got is None or got[0] in ("ask", "deny")
    if re.search(r"(^|[\s;&|])testbench run [^\n;&|]*--approve(\s|$)", text):
        assert decided(G, project, text) in ("ask", "deny"), text


budgets = st.one_of(st.floats(), st.integers(-10, 10 ** 6),
                    st.sampled_from(["lots", None, True, [], "5", math.nan, math.inf, -math.inf, 10 ** 400, 0.0, 1.0]))


def _number(v) -> bool:
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    except OverflowError:
        return False


@fuzz(300)
@given(st.fixed_dictionaries({k: budgets for k in ("max_run_usd", "ask_above_usd", "max_day_usd")}),
       st.fixed_dictionaries({k: budgets for k in ("max_run_usd", "ask_above_usd", "max_day_usd")}),
       st.sampled_from(["Write", "Edit"]))
def test_an_edit_that_raises_or_breaks_a_budget_asks(G, project, old, new, tool):
    yml = project / "testbench.yaml"
    base = yaml.safe_load(yml.read_text())
    before = yaml.safe_dump({**base, "budget": old}, allow_unicode=True)
    after = yaml.safe_dump({**base, "budget": new}, allow_unicode=True)
    yml.write_text(before)
    inp = ({"file_path": str(yml), "content": after} if tool == "Write" else
           {"file_path": str(yml), "old_string": before, "new_string": after})
    got = G.decision({"cwd": str(project), "tool_name": tool, "tool_input": inp})
    raised = [k for k in old if not (_number(old[k]) and _number(new[k])) and (old[k] != new[k] or not _number(new[k]))
              or _number(old[k]) and _number(new[k]) and new[k] > old[k]]
    assert (got is not None and got[0] == "ask") == bool(raised), (old, new, got)


@fuzz(150)
@given(st.dictionaries(st.sampled_from(["network", "run_args", "command", "image"]),
                       st.sampled_from(["none", "bridge", "host", ["-v", "/:/host"], ["podman"], None]), max_size=4),
       st.sampled_from(["docker", "local", "modal", "ssh"]))
def test_a_way_out_of_the_docker_sandbox_asks(G, project, docker, backend):
    yml = project / "testbench.yaml"
    boxed = {"project": "p", "docker": {"network": "none", "image": "python:3.11-slim"},
             "plans": {"box": {"backend": "docker", "steps": [{"name": "a", "run": "true"}]}}}
    yml.write_text(yaml.safe_dump(boxed))
    after = {**boxed, "docker": {**boxed["docker"], **docker},
             "plans": {"box": {**boxed["plans"]["box"], "backend": backend}}}
    got = G.decision({"cwd": str(project), "tool_name": "Write",
                      "tool_input": {"file_path": str(yml), "content": yaml.safe_dump(after)}})
    loosened = (after["docker"].get("network") != "none" or any(after["docker"].get(k) != boxed["docker"].get(k)
                                                               for k in ("run_args", "command")) or backend != "docker")
    assert (got is not None and got[0] == "ask") == loosened, (docker, backend, got)


@fuzz(200)
@given(st.sampled_from(["", "  ", '{"x": 1}', "a: [", "- 1", "budget: 5", "budget: {max_run_usd: .nan}", "\ud800",
                        json.dumps({"budget": {"max_run_usd": 1e400}})]),
       st.text(max_size=30), st.sampled_from(["Write", "Edit", "MultiEdit"]))
def test_no_edit_makes_the_hook_fail(G, project, before, text, tool):
    yml = project / "testbench.yaml"
    yml.write_bytes(before.encode("utf-8", "surrogatepass"))         # a file that is not UTF-8 too
    inp = {"Write": {"file_path": str(yml), "content": text},
           "Edit": {"file_path": str(yml), "old_string": before, "new_string": text},
           "MultiEdit": {"file_path": str(yml), "edits": [{"old_string": before, "new_string": text}]}}[tool]
    got = G.decision({"cwd": str(project), "tool_name": tool, "tool_input": inp})
    assert got is None or got[0] in ("ask", "deny")
