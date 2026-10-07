"""Static checks for notebooks that are meant to run on Colab and be tested headless.

Each rule comes from a failure seen in real runs; the message says what goes wrong and what to do.
"""
from __future__ import annotations

import re
from pathlib import Path

import nbformat

from .notebook import PARAM

RULES = {
    "L001": "large outputs saved in the notebook",
    "L002": "video embedded into the page",
    "L003": "secret read without a fallback",
    "L004": "files.upload() needs a person",
    "L005": "unpinned pip install",
    "L006": "child Python processes inherit Colab's inline matplotlib backend",
    "L007": "form field that cannot be set",
    "L008": "hard-coded /content path",
}


def lint(path: str | Path) -> list[dict]:
    nb = nbformat.read(str(path), as_version=4)
    out = []

    def add(rule, cell, msg):
        out.append({"rule": rule, "title": RULES[rule], "cell": cell, "message": msg})

    code = [(i, c) for i, c in enumerate(nb.cells, 1) if c.cell_type == "code"]
    all_src = "\n".join(c.source for _, c in code)
    for n, (i, c) in enumerate(code, 1):
        src = c.source
        size = len(nbformat.writes(nbformat.v4.new_notebook(cells=[c])))
        if size > 1_000_000:
            add("L001", n, f"code cell {n} carries {size / 1e6:.1f} MB of saved output. Colab saves the whole notebook "
                           "on every change; big outputs make saving fail and the page hang. Clear outputs before sharing.")
        if re.search(r"Video\([^)]*embed\s*=\s*True", src) or "b64encode(" in src and "<video" in src:
            add("L002", n, "a video is embedded into the page as base64. Over a few MB this freezes Colab's tab and "
                           "drops the connection. Show a small preview (ffmpeg -vf scale=640:-2) and link the full file.")
        if re.search(r"userdata\.get\(", src) and not re.search(r"try:|SecretNotFoundError|except", src):
            add("L003", n, "userdata.get() with no fallback stops 'Run all' with an access prompt when the secret is "
                           "missing. Wrap it in try/except and say which secret is needed.")
        if re.search(r"files\.upload\(\)", src):
            add("L004", n, "files.upload() waits for a person to pick files. Fine on Colab; for tests give the step "
                           "`uploads:` in testbench.yaml, or let a path field skip the upload.")
        for m in re.finditer(r"^\s*[!%]\s*pip3?\s+install\s+(.+)$", src, re.M):
            pkgs = [p for p in m.group(1).split() if not p.startswith("-")]
            loose = [p for p in pkgs if not re.search(r"[=<>~@]", p) and not p.startswith(("git+", "http", ".", "/"))]
            if loose:
                add("L005", n, f"`pip install {' '.join(loose)}` takes whatever version is newest that day, so the "
                               f"notebook can break without a change. Pin versions (pkg==x.y.z).")
        runs_python = re.search(r"subprocess\.\w+\(\s*\[[^\]]*(sys\.executable|['\"]python3?['\"])(?![^\]]*['\"]pip['\"])", src) \
            or re.search(r"^\s*!\s*python3?\s+(?!-m\s+pip)", src, re.M)
        if runs_python and "MPLBACKEND" not in all_src:
            add("L006", n, "this cell starts Python processes. Colab sets MPLBACKEND to its inline backend, and a child "
                           "process that imports matplotlib then crashes. Pass env with MPLBACKEND='Agg'.")
        for line in src.splitlines():
            m = PARAM.match(line)
            if "@param" in line and not line.lstrip().startswith(("#", "@")) and not (m and m.group("value").strip()):
                add("L007", n, f"`{line.strip()[:80]}` looks like a form field but is not `NAME = value  #@param ...`, so "
                               "nothing can set it from outside.")
        if re.search(r"""['"]/content/(?!drive)""", src):
            add("L008", n, "a path under /content only exists on Colab. Build paths from a ROOT variable (or "
                           "Path.cwd()) so the notebook also runs locally and on Modal.")
    names = [m.group("name") for _, c in code for m in PARAM.finditer(c.source)]
    for name in sorted({x for x in names if names.count(x) > 1}):
        add("L007", None, f"form field {name} is defined on {names.count(name)} lines; a plan cannot tell which to set.")
    return out


def render(path: str | Path, findings: list[dict]) -> str:
    if not findings:
        return f"{path}: no problems found"
    lines = [f"{path}: {len(findings)} finding(s)"]
    for f in findings:
        where = f"cell {f['cell']}" if f["cell"] else "notebook"
        lines.append(f"  {f['rule']} {where}: {f['message']}")
    return "\n".join(lines)
