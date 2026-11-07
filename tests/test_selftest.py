"""`testbench selftest`: ten checks of the main promises in a temporary folder, local and free - and it never
writes to this machine's real limits folder or ledger (it swaps in empty ones of its own)."""
from conftest import cli


def test_selftest_passes_and_leaves_the_machine_s_limits_alone(tmp_path, machine_limits):
    r = cli(tmp_path, "selftest", timeout=600)
    assert r.returncode == 0 and "10 checks: 10 ok, 0 failed" in r.stdout or (
        r.returncode == 0 and "0 failed" in r.stdout and "skipped" in r.stdout), r.stdout + r.stderr
    assert not (machine_limits / "ledger.jsonl").exists(), "its runs are not in this machine's ledger"
    assert "self-test, in " in r.stdout and "kept in" not in r.stdout, "the temporary folder is removed when all pass"
