"""Notebooks: reading them, their parameters, cells as plain files, and cell-by-cell execution that keeps the evidence.

A notebook is an .ipynb, or a percent-format .py (`# %%` cells, as jupytext, VS Code and Spyder write them).
Its parameters are Colab form fields (`NAME = value  #@param`) and papermill's: a code cell tagged `parameters`,
after which a cell tagged `injected-parameters` sets the values. Execution runs the notebook in a real kernel -
the one its kernelspec names when that is installed, else python3 for a Python notebook (an IPython kernel, so
`!pip`, `%cd` and display() behave as in Colab) - one code cell at a time against the step's deadline. For each
cell it records the time, the text it printed, the images it displayed and, if it failed, the error and the
lines that raised it. docs/notebooks.md has the details.
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
CELL_MARK = re.compile(r"^[ \t]*#[ \t]*%%(%*)(?:[ \t]+(.*?))?[ \t]*$")      # `# %%`, `# %% title [markdown] tags=[..]`
MAGIC = re.compile(r"^\s*(?:# ?)+(?:%{1,3}[A-Za-z]|\s*!\s*[A-Za-z.~$\\/{}]|\s*[A-Za-z_]\w*\s*=\s*(?:%{1,3}|!)[A-Za-z])")
INSTALL = {"ir": "R, then IRkernel - in R: install.packages('IRkernel'); IRkernel::installspec()",
           "julia": 'Julia, then IJulia - in Julia: using Pkg; Pkg.add("IJulia")',
           "bash": "bash_kernel: pip install bash_kernel && python -m bash_kernel.install",
           "deno": "Deno, then: deno jupyter --install"}


class NotebookError(ValueError):
    """The notebook cannot run as asked (its format, its kernel or its params); the message says what to do."""


class ParamError(NotebookError):
    pass


# ---- reading ----------------------------------------------------------------------------------------------

def plain_python(path: str | Path) -> str | None:
    """Why a .py cannot be a notebook (it has no `# %%` cell markers), or None when it can."""
    p = Path(path)
    if p.suffix.lower() != ".py" or not p.is_file():
        return None
    if any(CELL_MARK.match(line) for line in p.read_text(encoding="utf-8", errors="replace").splitlines()):
        return None
    return (f"{p.as_posix()} is a plain Python file, not a notebook: it has no `# %%` cell markers. Run it with a "
            f"`run:` step (run: python {p.as_posix()}), or mark its cells with `# %%` (the percent format of "
            "jupytext, VS Code and Spyder) to run it as a notebook")


def read(path: str | Path):
    """An .ipynb, or a percent-format .py read as a notebook: by jupytext when it is installed, else by
    from_percent."""
    p = Path(path)
    if p.suffix.lower() != ".py":
        return nbformat.read(str(p), as_version=4)
    problem = plain_python(p)
    if problem:
        raise NotebookError(problem)
    text = p.read_text(encoding="utf-8")
    try:
        import jupytext
    except ImportError:
        return from_percent(text)
    return jupytext.reads(text, fmt="py:percent")


def from_percent(text: str):
    """The built-in reader of percent-format scripts. `# %%` starts a code cell, `# %% [markdown]` (or [md]) a
    markdown cell whose lines are comments, `# %% [raw]` a raw cell; after the marker may come a title and
    metadata as key=JSON (`# %% tags=["parameters"]`) or as a JSON object. Commented magics (`# %pip ...`,
    `# !ls`, `# x = !ls`) are uncommented, as jupytext writes them. The jupytext header (`# ---` ... `# ---`)
    gives the notebook's metadata, such as its kernelspec. Not read: other formats (light, `# In[ ]:` markers),
    cells written as triple-quoted strings, jupytext's per-cell options such as active=."""
    lines = text.splitlines()
    i = 0
    while i < len(lines) and (lines[i].startswith("#!") or re.match(r"#.*coding[:=]", lines[i])):
        i += 1
    meta = {}
    if i < len(lines) and lines[i].rstrip() == "# ---":
        end = next((j for j in range(i + 1, len(lines)) if lines[j].rstrip() == "# ---"), None)
        if end is not None:
            import yaml
            try:
                header = yaml.safe_load("\n".join(_uncomment(ln) for ln in lines[i + 1:end])) or {}
            except yaml.YAMLError:
                header = {}
            meta = dict(header.get("jupyter") or {}) if isinstance(header, dict) else {}
            i = end + 1
    chunks, options, body, quote = [], None, [], None
    for line in lines[i:]:
        mark = None if quote else CELL_MARK.match(line)
        if mark:
            chunks.append((options, body))
            options, body = mark.group(2) or "", []
            continue
        body.append(line)
        quote = _open_string(line, quote)
    chunks.append((options, body))
    nb = nbformat.v4.new_notebook(metadata=meta)
    for options, body in chunks:
        while body and not body[0].strip():
            body.pop(0)
        while body and not body[-1].strip():
            body.pop()
        if options is None and not body:
            continue
        kind, cell_meta = _cell_options(options or "")
        if kind == "code":
            source, quote = [], None
            for line in body:
                source.append(_uncomment(line, indent=True) if not quote and MAGIC.match(line) else line)
                quote = _open_string(line, quote)
            cell = nbformat.v4.new_code_cell("\n".join(source), metadata=cell_meta)
        else:
            make = nbformat.v4.new_markdown_cell if kind == "markdown" else nbformat.v4.new_raw_cell
            cell = make("\n".join(_uncomment(ln) for ln in body), metadata=cell_meta)
        cell["id"] = f"cell-{len(nb.cells) + 1}"
        nb.cells.append(cell)
    return nb


def _uncomment(line: str, indent: bool = False) -> str:
    """The line without one leading `# ` (or `#`); with indent, the one after its indentation."""
    body = line.lstrip() if indent else line
    lead = line[:len(line) - len(body)]
    for prefix in ("# ", "#"):
        if body.startswith(prefix):
            return lead + body[len(prefix):]
    return line


def _open_string(line: str, quote: str | None) -> str | None:
    """The triple quote still open at the end of `line`, given the one open at its start (so a `# %%` inside a
    docstring is not a cell marker)."""
    i = 0
    while i < len(line):
        if quote:
            j = line.find(quote, i)
            if j < 0:
                return quote
            i, quote = j + 3, None
        elif line.startswith(('"""', "'''"), i):
            quote, i = line[i:i + 3], i + 3
        elif line[i] == "#":
            return None
        elif line[i] in "\"'":
            j = i + 1
            while j < len(line) and line[j] != line[i]:
                j += 2 if line[j] == "\\" else 1
            i = j + 1
        else:
            i += 1
    return quote


def _cell_options(text: str) -> tuple[str, dict]:
    """('code' | 'markdown' | 'raw', metadata) from what follows `# %%`."""
    text, meta = text.strip(), {}
    brace, key = text.find("{"), re.search(r"(?:^|\s)[A-Za-z_][\w.-]*\s*=", text)
    if brace >= 0 and (not key or brace < key.start()):
        try:
            meta, text = json.loads(text[brace:]), text[:brace].strip()
        except ValueError:
            pass
    elif key:
        text, rest = text[:key.start()].strip(), text[key.start():].strip()
        decoder = json.JSONDecoder()
        while (m := re.match(r"([A-Za-z_][\w.-]*)\s*=\s*", rest)):
            rest = rest[m.end():]
            try:
                value, end = decoder.raw_decode(rest)
            except ValueError:
                value, _, rest = rest.partition(" ")
                end = 0
            meta[m.group(1)], rest = value, rest[end:].strip()
    kind = "code"
    for marker, cell_type in (("[markdown]", "markdown"), ("[md]", "markdown"), ("[raw]", "raw")):
        if marker in text:
            kind, text = cell_type, text.replace(marker, "").strip()
            break
    if text:
        meta["title"] = text
    return kind, meta


# ---- kernels ----------------------------------------------------------------------------------------------

def language(nb) -> str:
    """The notebook's language, from its metadata; python when it does not say."""
    spec = nb.metadata.get("kernelspec") or {}
    lang = spec.get("language") or (nb.metadata.get("language_info") or {}).get("name")
    if not lang:
        name = str(spec.get("name") or "python3").lower()
        lang = "r" if name == "ir" else "julia" if name.startswith("julia") else "python"
    return str(lang).lower()


def kernel(nb) -> tuple[str, str, str | None]:
    """(kernel to start, its language, a note when that is not the kernel the notebook names).

    The kernel the notebook's kernelspec names, when it is installed. A Python notebook whose kernel is not
    installed (a virtualenv's kernel on its author's machine) runs on python3, as every notebook used to; one in
    another language is refused, with how to install its kernel."""
    from jupyter_client.kernelspec import KernelSpecManager
    spec = nb.metadata.get("kernelspec") or {}
    name = str(spec.get("name") or "python3").lower()
    ksm = KernelSpecManager()
    installed = ksm.find_kernel_specs()
    if name in installed:
        return name, (ksm.get_kernel_spec(name).language or spec.get("language") or "python").lower(), None
    lang = language(nb)
    if lang == "python":
        note = None if name == "python3" else (f"the notebook names kernel {name!r}, which is not installed here; it "
                                                 "runs on python3 (the Python that runs testbench)")
        return "python3", lang, note
    how = next((h for k, h in INSTALL.items() if re.match(rf"{k}(?:[-_.\d]|$)", name)),
               "the package that provides that kernel (see its documentation)")
    raise NotebookError(f"the notebook runs on the {name!r} kernel ({spec.get('display_name') or lang}), which is not "
                        f"installed on this machine. Install {how}. Then `jupyter kernelspec list` should show "
                        f"{name}. Kernels installed here: {', '.join(sorted(installed)) or 'none'}. (For a remote "
                        "backend, install it in the machine's image or setup.)")


# ---- parameters -------------------------------------------------------------------------------------------

def form_fields(source: str) -> dict:
    """{name: current value text} for every `NAME = value  #@param ...` line."""
    return {m.group("name"): m.group("value").strip() for m in PARAM.finditer(source)}


def cell_tags(cell) -> list:
    return list((cell.get("metadata") or {}).get("tags") or [])


def parameter_names(source: str) -> dict:
    """{name: value text} for the top-level assignments in a papermill `parameters` cell."""
    import ast
    clean = "\n".join("" if re.match(r"\s*[%!]", ln) else ln for ln in source.splitlines())
    try:
        tree = ast.parse(clean)
    except SyntaxError:
        return {m.group(1): m.group(2).strip() for m in
                re.finditer(r"^([A-Za-z_]\w*)\s*(?::[^=\n]+)?=(?!=)\s*(.*?)\s*(?:#.*)?$", clean, re.M)}
    out = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else \
            [node.target] if isinstance(node, ast.AnnAssign) and node.value is not None else []
        for t in targets:
            if isinstance(t, ast.Name):
                out[t.id] = (ast.get_source_segment(clean, node.value) or "").strip()
    return out


def fields(nb) -> list[dict]:
    """What `params:` can set, in notebook order: {cell, name, value, kind}, kind "form" (a Colab form field) or
    "parameter" (assigned in the first code cell tagged `parameters`, papermill's convention); code cells are
    numbered from 1."""
    code = [c for c in nb.cells if c.cell_type == "code"]
    out = [{"cell": n, "name": k, "value": v, "kind": "form"}
           for n, c in enumerate(code, 1) for k, v in form_fields(c.source).items()]
    forms = {f["name"] for f in out}
    tagged = next(((n, c) for n, c in enumerate(code, 1) if "parameters" in cell_tags(c)), None)
    if tagged:
        out += [{"cell": tagged[0], "name": k, "value": v, "kind": "parameter"}
                for k, v in parameter_names(tagged[1].source).items() if k not in forms]
    return sorted(out, key=lambda f: f["cell"])


def check_cells_set_params(nb, params: dict, numbers) -> None:
    """A ParamError when the code cells to run (numbered from 1) leave out a cell that one of `params` is set in:
    its value would never reach the code. Call it before prepare(), which adds a cell."""
    where = {}
    for f in fields(nb):
        where.setdefault(f["name"], f)
    left = {k: where[k] for k in params if k in where and where[k]["cell"] not in set(numbers)}
    if not left:
        return
    what = "; ".join(f"{k} in code cell {f['cell']} ("
                     + ("a Colab form field)" if f["kind"] == "form" else "the cell tagged `parameters`)")
                     for k, f in left.items())
    cells = sorted({f["cell"] for f in left.values()})
    raise ParamError(f"params: {what} - the cells asked for ({', '.join(map(str, numbers))}) do not include "
                     f"{'that cell' if len(cells) == 1 else 'those cells'}, so the value would never be used. Add "
                     f"cell{'s' * (len(cells) > 1)} {', '.join(map(str, cells))} to the cells to run, or drop "
                     f"{', '.join(left)} from params")


def set_params(cells_source: list[str], params: dict, parameters=()) -> list[str]:
    """Set form fields the way the Colab form does: each value replaces the one on its #@param line.

    A field must appear on exactly one #@param line in the notebook, so a typo fails loudly instead of
    silently running with the default. `parameters`: the papermill parameters, named in that error.
    """
    out = list(cells_source)
    for name, value in params.items():
        hits = [(i, m) for i, src in enumerate(out) for m in PARAM.finditer(src) if m.group("name") == name]
        if len(hits) > 1:
            raise ParamError(f"form field {name!r} is on {len(hits)} #@param lines; keep one, so params can tell "
                             "which to set")
        if not hits:
            known = sorted({n for src in out for n in form_fields(src)})
            raise ParamError(f"params: the notebook has no parameter {name!r}. Its Colab form fields (NAME = value  "
                             f"#@param) are: {', '.join(known) or 'none'}; its papermill parameters (set in the code "
                             f"cell tagged `parameters`) are: {', '.join(parameters) or 'none'}"
                             + ("" if parameters else " (no code cell is tagged `parameters`)"))
        i, m = hits[0]
        out[i] = out[i][:m.start()] + m.group("lhs") + repr(value) + m.group("tail") + out[i][m.end():]
    return out


def prepare(nb, params: dict, lang: str | None = None):
    """Set params in the notebook, in place: Colab form fields on their #@param lines; the others as papermill
    does, in a cell tagged `injected-parameters` right after the first cell tagged `parameters` (a cell already
    tagged `injected-parameters`, left by a papermill run, is replaced instead).

    Returns the indexes of the code cells, numbered from 1 as in the file, and the cell it added (not numbered:
    it runs right after the parameters cell), if any."""
    code_idx = [i for i, c in enumerate(nb.cells) if c.cell_type == "code"]
    if not params:
        return code_idx, None
    lang = lang or language(nb)
    if lang != "python":
        name = (nb.metadata.get("kernelspec") or {}).get("name") or lang
        raise ParamError(f"params: this notebook runs on the {name} kernel ({lang}); testbench sets params only in "
                         "Python notebooks (Colab form fields, papermill parameters). Pass the values as environment "
                         "variables instead - `env: {EPOCHS: \"2\"}` in the step, read with Sys.getenv(\"EPOCHS\") in "
                         "R or ENV[\"EPOCHS\"] in Julia - and drop `params:`")
    sources = [nb.cells[i].source for i in code_idx]
    forms = {k for src in sources for k in form_fields(src)}
    tagged = next((i for i in code_idx if "parameters" in cell_tags(nb.cells[i])), None)
    names = parameter_names(nb.cells[tagged].source) if tagged is not None else {}
    paper = {k: v for k, v in params.items() if k in names and k not in forms}
    sources = set_params(sources, {k: v for k, v in params.items() if k not in paper}, parameters=list(names))
    for i, src in zip(code_idx, sources):
        nb.cells[i].source = src
    if not paper:
        return code_idx, None
    text = "# Parameters\n" + "".join(f"{k} = {v!r}\n" for k, v in paper.items())
    old = next((i for i in code_idx if "injected-parameters" in cell_tags(nb.cells[i])), None)
    if old is not None:
        nb.cells[old].source = text
        return code_idx, None
    cell = nbformat.v4.new_code_cell(text, metadata={"tags": ["injected-parameters"]})
    nb.cells.insert(tagged + 1, cell)
    return [i for i, c in enumerate(nb.cells) if c.cell_type == "code" and c is not cell], cell


def cell_sources(nb, params: dict) -> list[str]:
    """The code cells' sources, cell n at [n - 1], with params set; papermill's injected values run as the end of
    the parameters cell."""
    code_idx, added = prepare(nb, params)
    out = [nb.cells[i].source for i in code_idx]
    if added is not None:
        n = next(k for k, i in enumerate(code_idx) if nb.cells[i + 1] is added)
        out[n] = out[n].rstrip("\n") + "\n\n" + added.source
    return out


# ---- cells as files ---------------------------------------------------------------------------------------

def split(nb_path: str | Path, out_dir: str | Path) -> list[Path]:
    """Write each cell as NN_slug.py or NN_slug.md, which an agent can edit like any source file."""
    nb = read(nb_path)
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

    nb = read(nb_path)
    kernel_name, lang, note = kernel(nb)
    if cells and params and lang == "python":
        check_cells_set_params(nb, params, cells)
    code_idx, added = prepare(nb, params, lang)
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

    if log and (note or kernel_name != "python3"):
        log.write(f"[testbench] {note or f'kernel {kernel_name} ({lang})'}\n")
    client = Client(nb, kernel_name=kernel_name, allow_errors=False, timeout=None,
                    resources={"metadata": {"path": str(workdir)}})
    result = {"status": "passed", "cells": [], "error": None, "images": [], "code_cells": len(code_idx),
              "kernel": kernel_name}
    try:
        with client.setup_kernel(env=env):              # env is the kernel's whole environment
            if shim and lang == "python":
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
                current = cell
                try:
                    client.execute_cell(cell, index)
                    if added is not None and index + 1 < len(nb.cells) and nb.cells[index + 1] is added:
                        current = added                          # papermill's injected values, right after
                        client.execute_cell(added, index + 1)
                except CellTimeoutError:
                    rec["status"] = "timeout"
                    result.update(status="timeout", error={"cell": n, "ename": "StepTimeout",
                                                           "evalue": "the step's max_minutes ran out in this cell",
                                                           "source_head": rec["source_head"]})
                except (CellExecutionError, DeadKernelError) as exc:
                    rec["status"] = "error"
                    result.update(status="failed", error=_error(current, n, exc))
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
    result["images"] = _save_images(nb, out_dir, skip=added)
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


def _save_images(nb, out_dir: Path, skip=None) -> list[str]:
    saved = []
    code_no = 0
    for cell in nb.cells:
        if cell.cell_type != "code" or cell is skip:
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
