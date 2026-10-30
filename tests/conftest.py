import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import nbformat
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def make_notebook(path: Path, cells: list[str]) -> Path:
    """Code cells from strings; a string starting with '# md:' becomes a markdown cell."""
    nb = nbformat.v4.new_notebook()
    for c in cells:
        if c.startswith("# md:"):
            nb.cells.append(nbformat.v4.new_markdown_cell(c[5:].strip()))
        else:
            nb.cells.append(nbformat.v4.new_code_cell(textwrap.dedent(c).strip()))
    nb.metadata["kernelspec"] = {"name": "python3", "display_name": "Python 3", "language": "python"}
    path.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(path))
    return path


def write_config(root: Path, cfg: dict) -> Path:
    (root / "testbench.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))
    return root / "testbench.yaml"


def cli(root: Path, *args, timeout=600, env=None) -> subprocess.CompletedProcess:
    """The CLI exactly as a person or agent runs it, from the project folder."""
    return subprocess.run([sys.executable, "-m", "agent_testbench", *args], cwd=root, capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})


@pytest.fixture(autouse=True)
def machine_limits(tmp_path, monkeypatch) -> Path:
    """Every test gets its own ~/.testbench (limits for all projects, the machine's ledger), never the real one."""
    d = tmp_path / "user-testbench"
    monkeypatch.setenv("TESTBENCH_USER_DIR", str(d))
    return d


@pytest.fixture
def tiny(tmp_path) -> Path:
    """A fresh copy of the tiny-model example."""
    dest = tmp_path / "tiny-model"
    shutil.copytree(EXAMPLES / "tiny-model", dest, ignore=shutil.ignore_patterns(".testbench", "outputs", "__pycache__"))
    return dest


@pytest.fixture
def plain(tmp_path) -> Path:
    """A small project with a dependency-free notebook (no scikit-learn needed)."""
    root = tmp_path / "plain"
    make_notebook(root / "nb.ipynb", [
        "# md: A plain notebook",
        'N = 3 #@param {type:"integer"}\nNAME = "ada" #@param {type:"string"}',
        "import json, pathlib\npathlib.Path('out').mkdir(exist_ok=True)\nprint('hello', NAME)",
        "json.dump({'n': N, 'squares': [i * i for i in range(N)]}, open('out/result.json', 'w'))\nprint('wrote', N)",
    ])
    write_config(root, {"project": "plain", "plans": {
        "quick": {"backend": "local", "steps": [
            {"name": "notebook", "notebook": "nb.ipynb", "max_minutes": 3, "artifacts": ["out/*"],
             "expect": {"json": {"out/result.json": {"n": ">= 3", "squares.-1": "== 4"}}}}]}}})
    return root


def has_sklearn() -> bool:
    try:
        import sklearn, matplotlib  # noqa: F401
        return True
    except ImportError:
        return False


def chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False


PLUGIN = '''
from agent_testbench.backends import Backend
from agent_testbench.backends.local import LocalBackend
from agent_testbench.session_backends import LocalSession


class FakeRunner(LocalBackend):
    def run(self):
        rep, _ = super().run()
        return rep, self.cost(rep)

    def cost(self, rep):
        return backend.rate_per_minute(self.cfg, self.plan) * rep.get("minutes", 0)


class Fake(Backend):
    """This machine, priced as if it were a rented GPU box."""
    gpus = ("FAKE1", "FAKE2")
    remote = False
    startup_minutes = 1.0
    config_defaults = {"fake": {"usd_per_hour": 6.0}}

    def validate(self, cfg):
        if cfg["fake"]["usd_per_hour"] <= 0:
            raise ValueError("fake.usd_per_hour must be positive")

    def rate_per_minute(self, cfg, hardware):
        return cfg["fake"]["usd_per_hour"] / 60

    def plan_runner(self):
        return FakeRunner

    def session_class(self):
        return LocalSession

    def doctor(self):
        return [("fake box reachable", True, "yes")]


backend = Fake()
'''


@pytest.fixture
def plugin(tmp_path) -> dict:
    """fakebench installed the way pip would: a module plus a dist-info with entry points, on PYTHONPATH."""
    site = tmp_path / "site"
    (site / "fakebench-0.1.dist-info").mkdir(parents=True)
    (site / "fakebench.py").write_text(PLUGIN)
    (site / "fakebench-0.1.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: fakebench\nVersion: 0.1\n")
    (site / "fakebench-0.1.dist-info" / "entry_points.txt").write_text(textwrap.dedent("""\
        [agent_testbench.backends]
        fake = fakebench:backend
        broken = fakebench:no_such_thing
        """))
    return {"PYTHONPATH": str(site)}
