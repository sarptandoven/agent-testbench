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
