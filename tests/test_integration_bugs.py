"""Bugs found where the 0.2.0 features meet each other and ordinary projects: each test failed before its fix."""
import json

from conftest import cli, git, make_notebook, score
from agent_testbench import rescue as RS


def test_a_loop_works_in_a_repository_that_ignores_the_testbench_folder(repo):
    # Many projects list .testbench/ in their .gitignore. git refuses to add or stash a path it ignores even to leave
    # it out (`:(exclude).testbench`), so every try failed with "The following paths are ignored".
    (repo / ".gitignore").write_text("metrics.json\n__pycache__/\n.testbench/\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore testbench's folder")
    assert cli(repo, "loop", "start", "score", "--metric", "metrics.json:score", "--budget-usd", "0").returncode == 0
    score(repo, 3)
    r = cli(repo, "loop", "try", "raise the score to 3")
    assert r.returncode == 0 and "KEPT try 1" in r.stdout, r.stdout + r.stderr

    (repo / "score.py").write_text((repo / "score.py").read_text().replace('print("score", SCORE)',
                                                                          'import time; time.sleep(3); print("score", SCORE)'))
    score(repo, 2)
    r = cli(repo, "loop", "try", "slower and worse", "--wait", "0")
    assert r.returncode == 3, r.stdout + r.stderr
    (repo / "README.md").write_text("next idea\n")                     # edited while the try runs: stashed, put back
    run = json.loads(next((repo / ".testbench" / "loops").glob("*/state.json")).read_text())["tries"][2]["run"]
    cli(repo, "wait", run, "--timeout", "60")
    r = cli(repo, "loop", "status")
    assert r.returncode == 0 and "reverted" in r.stdout, r.stdout + r.stderr
    assert (repo / "settings.py").read_text() == "SCORE = 3\n" and (repo / "README.md").read_text() == "next idea\n"


def test_rescue_does_not_report_files_a_notebook_makes_or_reads_from_an_archive_as_missing(tmp_path):
    # ZipFile(path, "w") and h5py.File(path, "w") make the file; z.open(name) reads a member of the archive; with
    # path.open("w") the path is the file and "w" its mode. Each was reported as "reads X, which is not there" (blocks,
    # X being the mode "w" for the last) on a notebook that runs top to bottom.
    nb = make_notebook(tmp_path / "nb.ipynb", [
        "import zipfile, h5py",
        "with zipfile.ZipFile('arch.zip', 'w') as z:\n    z.writestr('inner.csv', 'a,b')",
        "with zipfile.ZipFile('arch.zip') as z:\n    with z.open('inner.csv') as f:\n        print(f.read())",
        "with h5py.File('weights.h5', 'w') as h:\n    h['x'] = [1]\nprint(h5py.File('weights.h5')['x'])",
        "from pathlib import Path\nOUT = Path('out')\nOUT.mkdir(exist_ok=True)\nm = OUT / 'm.json'\nwith m.open('w') as f:\n"
        "    f.write('{}')\n(OUT / 'cfg.txt').write_text('x')",
        "print(m.open().read(), (OUT / 'cfg.txt').open().read())",
        "print(open('notes.txt').read())",
    ])
    info = RS.read(nb)
    files = [f["message"] for f in info["findings"] if f["kind"] == "files"]
    assert len(files) == 1 and "notes.txt" in files[0], files     # a file nothing makes is still reported
    assert {"arch.zip", "weights.h5", "out/m.json", "out/cfg.txt"} <= set(info["writes"]), info["writes"]


def test_the_python_api_turns_a_notebook_it_cannot_run_into_a_testbench_error(plain):
    # docs/python-api.md: anything else is a TestbenchError. A plain .py given as a notebook raised notebook.NotebookError.
    from agent_testbench import Testbench
    from agent_testbench.api import Session, TestbenchError
    (plain / "script.py").write_text("print('no cell markers')\n")
    tb = Testbench(plain)
    try:
        Session(tb, "main").cells("script.py")
    except TestbenchError as exc:
        assert "plain Python file" in str(exc)
    else:
        raise AssertionError("no error")
