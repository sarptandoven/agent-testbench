"""Live runs and sessions on the real services, opt in: they cost money or compute units.

    TESTBENCH_TEST_MODAL=1 python -m pytest tests/test_live.py -k modal   # Modal: about $0.05 (needs `modal setup`)
    TESTBENCH_TEST_COLAB=1 python -m pytest tests/test_live.py -k colab   # Colab: CPU runtimes for a few minutes
                                                                          # (needs the Colab CLI and a login; set
                                                                          # TESTBENCH_COLAB_AUTH=adc if you use ADC)
    TESTBENCH_TEST_SSH_HOST=user@host python -m pytest tests/test_live.py -k ssh   # a real ssh host (free unless it is
                                                                          # billed); TESTBENCH_TEST_SSH_COMMAND for flags
"""
import json
import os
import shutil

import pytest

from conftest import EXAMPLES, cli


@pytest.fixture
def example(tmp_path):
    dest = tmp_path / "tiny-model"
    shutil.copytree(EXAMPLES / "tiny-model", dest, ignore=shutil.ignore_patterns(".testbench", "outputs"))
    return dest


def _report(root):
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    return run, json.loads((run / "report.json").read_text())


@pytest.mark.modal
@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_MODAL") != "1", reason="set TESTBENCH_TEST_MODAL=1 to run on Modal")
def test_modal_cpu_and_gpu(example):
    r = cli(example, "run", "full-modal", "--wait", "900", timeout=1000)
    assert r.returncode == 0, r.stdout + r.stderr
    run, rep = _report(example)
    assert rep["steps"][0]["status"] == "passed" and (run / "steps" / "01-train-full" / "artifacts" / "outputs" / "metrics.json").exists()
    r = cli(example, "run", "gpu-check", "--wait", "900", timeout=1000)
    assert r.returncode == 0 and "Tesla T4" in (_report(example)[0] / "steps" / "01-see-the-gpu" / "log.txt").read_text()


@pytest.mark.modal
@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_MODAL") != "1", reason="set TESTBENCH_TEST_MODAL=1 to run on Modal")
def test_modal_cancel_stops_the_container(example):
    import time
    import modal
    cfg = (example / "testbench.yaml").read_text().replace("run: nvidia-smi --query-gpu=name,memory.total --format=csv",
                                                         "run: sleep 600")
    (example / "testbench.yaml").write_text(cfg)
    r = cli(example, "run", "gpu-check", "--detach")
    assert r.returncode == 3
    run = sorted((example / ".testbench" / "runs").iterdir())[-1]
    call_id = None
    for _ in range(120):
        call_id = json.loads((run / "state.json").read_text()).get("modal_call_id")
        if call_id:
            break
        time.sleep(2)
    assert call_id, "the run never reached Modal"
    time.sleep(20)
    assert "cancelled" in cli(example, "cancel").stdout
    with pytest.raises(Exception):
        modal.FunctionCall.from_id(call_id).get(timeout=60)
    assert json.loads((run / "state.json").read_text())["status"] == "cancelled"


@pytest.mark.colab
@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_COLAB") != "1", reason="set TESTBENCH_TEST_COLAB=1 to run on Colab")
def test_colab_cpu(example):
    if os.environ.get("TESTBENCH_COLAB_AUTH"):
        (example / "testbench.yaml").write_text((example / "testbench.yaml").read_text()
                                                + f"\ncolab:\n  auth: {os.environ['TESTBENCH_COLAB_AUTH']}\n")
    r = cli(example, "run", "colab-cpu", "--wait", "1500", timeout=1600)
    assert r.returncode == 0, r.stdout + r.stderr
    run, rep = _report(example)
    assert (run / "steps" / "01-train-on-colab" / "artifacts" / "outputs" / "metrics.json").exists()
    assert "stopped" in (run / "driver.log").read_text(), "the Colab session must be released"


def _session_roundtrip(root, name, backend, *extra):
    r = cli(root, "session", "start", name, "--backend", backend, "--idle-minutes", "3", "--max-minutes", "10", *extra,
            timeout=900)
    assert r.returncode == 0, r.stdout + r.stderr
    try:
        r = cli(root, "exec", "-s", name, "--cells", "tiny_model.ipynb:1-4", "--params", '{"EPOCHS": 30}', timeout=900)
        assert r.returncode == 0 and "accuracy" in r.stdout and "image: " in r.stdout, r.stdout
        r = cli(root, "exec", "-s", name, "model.n_iter_", timeout=300)
        assert "=> 30" in r.stdout, "the kernel must keep its variables between calls"
        (root / "note.txt").write_text("synced")
        assert "copied" in cli(root, "session", "sync", name, timeout=600).stdout
        assert "=> 'synced'" in cli(root, "exec", "-s", name, "open('note.txt').read()", timeout=300).stdout
        assert "metrics.json" in cli(root, "files", "-s", name, "ls", "outputs", timeout=300).stdout
    finally:
        r = cli(root, "session", "stop", name, timeout=300)
    assert "stopped after" in r.stdout


@pytest.mark.modal
@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_MODAL") != "1",
                    reason="set TESTBENCH_TEST_MODAL=1 to run on Modal")
def test_modal_live_session(example):
    _session_roundtrip(example, "live", "modal")
    r = cli(example, "session", "start", "gpu", "--backend", "modal", "--gpu", "T4", "--max-minutes", "5", timeout=900)
    assert r.returncode == 0, r.stdout
    try:
        assert "Tesla T4" in cli(example, "session", "shell", "-s", "gpu", "nvidia-smi -L", timeout=300).stdout
    finally:
        cli(example, "session", "stop", "gpu", timeout=300)


@pytest.mark.colab
@pytest.mark.skipif(os.environ.get("TESTBENCH_TEST_COLAB") != "1",
                    reason="set TESTBENCH_TEST_COLAB=1 to run on Colab")
def test_colab_live_session(example):
    if os.environ.get("TESTBENCH_COLAB_AUTH"):
        (example / "testbench.yaml").write_text((example / "testbench.yaml").read_text()
                                                + f"\ncolab:\n  auth: {os.environ['TESTBENCH_COLAB_AUTH']}\n")
    _session_roundtrip(example, "live", "colab")


@pytest.mark.skipif(not os.environ.get("TESTBENCH_TEST_SSH_HOST"), reason="set TESTBENCH_TEST_SSH_HOST to run on a real ssh host")
def test_ssh_plan_and_session(example):
    """TESTBENCH_TEST_SSH_HOST=user@host (reached with keys); TESTBENCH_TEST_SSH_COMMAND='ssh -p 2222 -i key' if needed.
    Free unless the host is billed; builds a virtualenv there the first time (a minute or two)."""
    import yaml
    cfg = yaml.safe_load((example / "testbench.yaml").read_text())
    cfg["ssh"] = {"host": os.environ["TESTBENCH_TEST_SSH_HOST"], "command": os.environ.get("TESTBENCH_TEST_SSH_COMMAND", "ssh"),
                  "pip": ["scikit-learn==1.5.2", "matplotlib==3.9.2"]}
    cfg["plans"]["on-ssh"] = {"backend": "ssh", "steps": [{"name": "train", "notebook": "tiny_model.ipynb", "max_minutes": 10,
                                                           "expect": {"json": {"outputs/metrics.json": {"accuracy": ">= 0.9"}}}}]}
    (example / "testbench.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    r = cli(example, "run", "on-ssh", "--wait", "900", timeout=1000)
    assert r.returncode == 0 and "PASS - plan `on-ssh` on ssh" in r.stdout, r.stdout + r.stderr
    r = cli(example, "session", "start", "box", "--backend", "ssh", "--max-minutes", "10")
    assert r.returncode == 0 and "ready on ssh" in r.stdout, r.stdout
    try:
        out = cli(example, "exec", "--cells", "tiny_model.ipynb:1-4").stdout
        assert "accuracy" in out and "image:" in out
    finally:
        assert "stopped after" in cli(example, "session", "stop").stdout
