"""Two holes the threat-model review found outside its own files, closed: results from a Colab VM unpack only inside
the run folder (also on Pythons without tarfile's data filter), and the local session supervisor's interval cannot be
broken or stretched from the environment a session started in."""
import io
import tarfile

import pytest

from agent_testbench.backends import colab_backend as CB
from agent_testbench.kernelkit import watchdog as W


def _tar(tmp_path, members) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, kind in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = b"ok\n"
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            else:
                info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                t.addfile(info)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


@pytest.mark.parametrize("with_filter", [True, False])
def test_results_from_the_vm_unpack_only_inside_the_run_folder(tmp_path, monkeypatch, with_filter):
    if not with_filter:
        monkeypatch.delattr(tarfile, "data_filter", raising=False)      # a Python before 3.10.12
    elif not hasattr(tarfile, "data_filter"):
        pytest.skip("this Python has no tarfile data filter")
    run = tmp_path / "run"
    run.mkdir()
    CB.extract_inside(_tar(tmp_path, [("steps/01-a/log.txt", "file")]), run)
    assert (run / "steps" / "01-a" / "log.txt").read_text() == "ok\n"
    for bad in ([("../outside.txt", "file")], [("steps/link", "symlink")]):
        with pytest.raises(Exception):
            CB.extract_inside(_tar(tmp_path, bad), run)
    assert not (tmp_path / "outside.txt").exists() and not (run / "steps" / "link").exists()


@pytest.mark.parametrize("text, seconds", [(None, 15.0), ("nan", 15.0), ("-3", 0.01), ("1e9", 15.0), ("soon", 15.0),
                                           ("0.2", 0.2)])
def test_the_supervisor_interval_is_clamped(text, seconds):
    assert W._tick(text) == seconds
