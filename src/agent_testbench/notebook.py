"""Notebooks: Colab form fields, cells as plain files, and cell-by-cell execution that keeps the evidence.

Execution runs the real .ipynb in a real IPython kernel (so `!pip`, `%cd` and display() behave as in Colab),
one code cell at a time against the step's deadline. For each cell it records the time, the text it printed,
the images it displayed and, if it failed, the error and the lines that raised it.
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

import nbformat

PARAM = re.compile(r"^(?P<lhs>\s*(?P<name>[A-Za-z_]\w*)\s*=\s*)(?P<value>.*?)(?P<tail>\s*#\s*@param.*)$", re.M)
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class ParamError(ValueError):
    pass


def form_fields(source: str) -> dict:
    """{name: current value text} for every `NAME = value  #@param ...` line."""
    return {m.group("name"): m.group("value").strip() for m in PARAM.finditer(source)}


def set_params(cells_source: list[str], params: dict) -> list[str]:
    """Set form fields the way the Colab form does: each value replaces the one on its #@param line.

    A field must appear on exactly one #@param line in the notebook, so a typo fails loudly instead of
    silently running with the default.
    """
    out = list(cells_source)
    for name, value in params.items():
        hits = [(i, m) for i, src in enumerate(out) for m in PARAM.finditer(src) if m.group("name") == name]
        if len(hits) != 1:
            known = sorted({n for src in out for n in form_fields(src)})
            raise ParamError(f"form field {name!r} found on {len(hits)} #@param lines; the notebook's fields are: "
                             f"{', '.join(known) or 'none'}")
        i, m = hits[0]
        out[i] = out[i][:m.start()] + m.group("lhs") + repr(value) + m.group("tail") + out[i][m.end():]
    return out


# ---- cells as files ---------------------------------------------------------------------------------------

def split(nb_path: str | Path, out_dir: str | Path) -> list[Path]:
    """Write each cell as NN_slug.py or NN_slug.md, which an agent can edit like any source file."""
    nb = nbformat.read(str(nb_path), as_version=4)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("[0-9][0-9]_*.py")) + list(out.glob("[0-9][0-9]_*.md")):
        old.unlink()
    written = []
    for i, cell in enumerate(nb.cells, 1):
        first = next((ln for ln in cell.source.splitlines() if ln.strip()), "")
        title = re.search(r"#\s*@title\s+(.+)", cell.source)
        words = title.group(1) if title else first.lstrip("#").strip()
        slug = re.sub(r"[^a-z0-9]+", "_", words.lower()).strip("_")[:40] or cell.cell_type
        path = out / f"{i:02d}_{slug}.{'py' if cell.cell_type == 'code' else 'md'}"
        path.write_text(cell.source + ("\n" if cell.source and not cell.source.endswith("\n") else ""))
        written.append(path)
    meta = {k: v for k, v in nb.metadata.items() if k in ("kernelspec", "language_info", "colab", "accelerator")}
    (out / "notebook.json").write_text(json.dumps({"metadata": meta}, indent=1) + "\n")
    return written


def build(cells_dir: str | Path, nb_path: str | Path) -> Path:
    """Assemble NN_*.py / NN_*.md files, in order, into a notebook with no outputs."""
    d = Path(cells_dir)
    files = sorted(list(d.glob("[0-9][0-9]_*.py")) + list(d.glob("[0-9][0-9]_*.md")), key=lambda p: p.name)
    if not files:
        raise FileNotFoundError(f"no NN_name.py / NN_name.md cell files in {d}")
    nb = nbformat.v4.new_notebook()
    meta = json.loads((d / "notebook.json").read_text())["metadata"] if (d / "notebook.json").exists() else {}
    nb.metadata.update(meta or {"kernelspec": {"name": "python3", "display_name": "Python 3", "language": "python"},
                                "language_info": {"name": "python"}})
    for f in files:
        text = f.read_text().rstrip("\n")
        cell = nbformat.v4.new_code_cell(text) if f.suffix == ".py" else nbformat.v4.new_markdown_cell(text)
        cell["id"] = re.sub(r"[^A-Za-z0-9_-]", "-", f.stem)[:64]      # stable ids keep rebuilt notebooks' diffs small
        nb.cells.append(cell)
    Path(nb_path).parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(nb, str(nb_path))
    return Path(nb_path)


# ---- execution --------------------------------------------------------------------------------------------

def _clean(text: str) -> str:
    return ANSI.sub("", text)


def _tail(text: str, lines: int = 40, chars: int = 6000) -> str:
    text = "\n".join(text.splitlines()[-lines:])
    return text[-chars:]


SHIM_BOOT = """
import importlib.util as _u, os as _o, sys as _s, types as _t
_d = {shim!r}
try:
    import google as _g
except ImportError:
    _g = _t.ModuleType("google"); _g.__path__ = []; _s.modules["google"] = _g
def _load(name, path, package=False):
    spec = _u.spec_from_file_location(name, path, submodule_search_locations=[_o.path.dirname(path)] if package else None)
    mod = _u.module_from_spec(spec); _s.modules[name] = mod; spec.loader.exec_module(mod); return mod
_c = _load("google.colab", _o.path.join(_d, "google", "colab", "__init__.py"), package=True)
for _n in ("files", "userdata", "drive", "output", "patches"):
    setattr(_c, _n, _load("google.colab." + _n, _o.path.join(_d, "google", "colab", _n + ".py")))
_g.colab = _c
del _u, _o, _s, _t, _d, _g, _c, _n, _load
"""


def run(nb_path: str | Path, *, params: dict, cells: list[int] | None, deadline: float, env: dict,
        workdir: str | Path, out_dir: str | Path, live_log: Path | None = None, shim: str | Path | None = None) -> dict:
    """Execute a notebook's code cells in one kernel; stop at the first error or when the deadline passes.

    Returns {"status": "passed"|"failed"|"timeout", "cells": [...], "error": {...}|None, "images": [...]}.
    """
    from nbclient import NotebookClient
    from nbclient.exceptions import CellExecutionError, CellTimeoutError, DeadKernelError

    nb = nbformat.read(str(nb_path), as_version=4)
    code_idx = [i for i, c in enumerate(nb.cells) if c.cell_type == "code"]
    sources = set_params([nb.cells[i].source for i in code_idx], params)
    for i, src in zip(code_idx, sources):
        nb.cells[i].source = src
    numbers = cells or list(range(1, len(code_idx) + 1))
    bad = [n for n in numbers if n > len(code_idx)]
    if bad:
        raise ParamError(f"cells {bad} asked for, but the notebook has {len(code_idx)} code cells")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log = open(live_log, "a", buffering=1) if live_log else None

    class Client(NotebookClient):
        def process_message(self, msg, cell, cell_index):       # stream printed text as it happens
            content = msg.get("content", {})
            if log and msg.get("msg_type") == "stream":
                log.write(_clean(content.get("text", "")))
            elif log and msg.get("msg_type") == "error":
                log.write(_clean("\n".join(content.get("traceback", []))) + "\n")
            return super().process_message(msg, cell, cell_index)

    client = Client(nb, kernel_name="python3", allow_errors=False, timeout=None,
                    resources={"metadata": {"path": str(workdir)}})
    result = {"status": "passed", "cells": [], "error": None, "images": [], "code_cells": len(code_idx)}
    try:
        with client.setup_kernel(env=env):              # env is the kernel's whole environment
            if shim:
                # The stand-in goes straight into sys.modules, so it wins even where a real google.colab is installed
                # (a Colab VM driven headless has no browser to answer files.upload()).
                # nbclient stores an executed cell back at its index, so the setup cell gets a slot of its own
                # past the end, removed again before anything is saved.
                boot = nbformat.v4.new_code_cell(SHIM_BOOT.format(shim=str(shim)))
                nb.cells.append(boot)
                client.timeout = 60
                try:
                    client.execute_cell(boot, len(nb.cells) - 1, store_history=False)
                finally:
                    nb.cells.pop()
            for n in numbers:
                index = code_idx[n - 1]
                cell = nb.cells[index]
                remaining = deadline - time.time()
                if remaining <= 0:
                    result.update(status="timeout", error={"cell": n, "ename": "StepTimeout",
                                                           "evalue": "the step's max_minutes ran out before this cell"})
                    break
                client.timeout = int(max(1, remaining))
                if log:
                    log.write(f"\n[testbench] --- cell {n} ---\n")
                began = time.time()
                rec = {"cell": n, "seconds": 0.0, "status": "ok", "source_head": _head(cell.source)}
                try:
                    client.execute_cell(cell, index)
                except CellTimeoutError:
                    rec["status"] = "timeout"
                    result.update(status="timeout", error={"cell": n, "ename": "StepTimeout",
                                                           "evalue": "the step's max_minutes ran out in this cell",
                                                           "source_head": rec["source_head"]})
                except (CellExecutionError, DeadKernelError) as exc:
                    rec["status"] = "error"
                    result.update(status="failed", error=_error(cell, n, exc))
                rec["seconds"] = round(time.time() - began, 2)
                rec["stdout_tail"] = _tail(_cell_text(cell), 12, 2000)
                result["cells"].append(rec)
                if rec["status"] != "ok":
                    break
    except DeadKernelError as exc:
        result.update(status="failed", error={"ename": "DeadKernelError", "evalue": str(exc)[-500:]})
    finally:
        if log:
            log.close()
    result["images"] = _save_images(nb, out_dir)
    _trim_outputs(nb)
    nbformat.write(nb, str(out_dir / "executed.ipynb"))
    return result


def _head(source: str, lines: int = 3) -> str:
    return "\n".join(source.splitlines()[:lines])[:300]


def _cell_text(cell) -> str:
    parts = []
    for o in cell.get("outputs", []):
        if o.get("output_type") == "stream":
            parts.append(o.get("text", ""))
        elif o.get("output_type") in ("execute_result", "display_data"):
            parts.append(o.get("data", {}).get("text/plain", ""))
    return _clean("".join(parts))


def _error(cell, n: int, exc) -> dict:
    err = next((o for o in cell.get("outputs", []) if o.get("output_type") == "error"), None)
    if err:
        tb = _clean("\n".join(err.get("traceback", [])))
        return {"cell": n, "ename": err.get("ename"), "evalue": _clean(str(err.get("evalue")))[:2000],
                "traceback_tail": _tail(tb, 25, 4000), "source_head": _head(cell.source),
                "line": _failing_line(tb)}
    return {"cell": n, "ename": type(exc).__name__, "evalue": _clean(str(exc))[-2000:], "source_head": _head(cell.source)}


def _failing_line(tb: str) -> str | None:
    """Where it failed: the cell's own line (IPython's first '---> N' mark) and, if the error came from deeper
    code, the file and line that raised it (the last frame)."""
    marks = re.findall(r"^-+>\s*(\d+)\s(.*)$", tb, re.M)
    if not marks:
        return None
    where = f"cell line {marks[0][0]}: {marks[0][1].strip()}"
    frames = re.findall(r"^File\s+(.+?):(\d+),\s+in\s+([\w.<>]+)", tb, re.M)
    if len(marks) > 1 and frames:
        path, line, fn = frames[-1]
        where += f" (raised in {Path(path).name}:{line}, {fn})"
    return where


def _save_images(nb, out_dir: Path) -> list[str]:
    saved = []
    code_no = 0
    for cell in nb.cells:
        if cell.cell_type != "code":
            continue
        code_no += 1
        k = 0
        for o in cell.get("outputs", []):
            data = o.get("data", {}) if o.get("output_type") in ("display_data", "execute_result") else {}
            for mime, ext in (("image/png", "png"), ("image/jpeg", "jpg")):
                if mime in data:
                    k += 1
                    path = out_dir / f"cell{code_no:02d}_image{k}.{ext}"
                    payload = data[mime]
                    path.write_bytes(base64.b64decode(payload if isinstance(payload, str) else "".join(payload)))
                    saved.append(path.name)
                    break
    return saved


def _trim_outputs(nb, limit: int = 200_000) -> None:
    """Keep the executed notebook small: drop embedded media over `limit` bytes (images are saved separately)."""
    for cell in nb.cells:
        for o in cell.get("outputs", []):
            for mime, payload in list(o.get("data", {}).items()):
                size = len(payload) if isinstance(payload, str) else sum(len(x) for x in payload)
                if size > limit and mime != "text/plain":
                    del o["data"][mime]
                    o["data"]["text/plain"] = f"[testbench: {mime} output of {size} bytes removed; images are saved beside this file]"
