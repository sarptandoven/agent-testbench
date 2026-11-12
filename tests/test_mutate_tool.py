"""tools/mutate.py, the hand-run mutation test of the money paths, still runs: it lists its mutants and kills one with
the test written for it. (The full run takes many minutes: `python tools/mutate.py`, or the manual mutation
workflow.)"""
import importlib.util

from trust_helpers import REPO


def load():
    spec = importlib.util.spec_from_file_location("mutate_tool", REPO / "tools" / "mutate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_mutation_tool_kills_a_mutant_with_its_test(capsys):
    M = load()
    found = {t: M.mutants(t) for t in M.TARGETS}
    assert len(found["budget"]) > 30 and len(found["guard"]) > 60
    assert {m["kind"] for m in found["budget"]} == {"compare", "number", "bool", "not", "boolop"}
    assert all(m["function"] in M.TARGETS["guard"]["functions"] for m in found["guard"]), "decision functions only"
    n = next(i for i, m in enumerate(found["budget"]) if m["function"] == "check" and m["code"].startswith(
        "return True"))                                          # the launch allowed: turned into a refusal
    assert "return (False, f'Within budget" in M.mutated(found["budget"][n])
    code = M.main(["--target", "budget", "--only", str(n), "--tests",
                   "tests/test_budget_rules.py::test_above_ask_above_usd_only_a_person_approves"])
    out = capsys.readouterr().out
    assert code == 0 and "1 mutants: 1 killed, 0 survived (mutation score 100.0%)" in out, out
