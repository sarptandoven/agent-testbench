"""Recipes: testbench.yaml for a goal ("fine-tune a model", "check my web app") rather than for what a project holds.

`testbench init` writes plans for what it finds in a project; a recipe starts from what the person wants to prove,
with the ladder of plans that proves it: a free check on this machine first, a short one on the target hardware,
then the full run - each step with expectations that fail when the result is wrong, not just when it crashes, and
max_minutes that bound what it can cost.

A recipe is a YAML file in this folder: a header comment (what it proves, what to FILL IN, what it costs), the
sections its plans need (`sessions:`, `modal:`, `colab:`, `docker:`, `ssh:`, `data:`), then `plans:` - never `budget:`,
`pricing:` or `guard:`, which stay the person's. Recipes are added to a testbench.yaml as text, so their comments come
along and nothing already in the file changes: a plan name that is taken is refused, and a section the file already
has is kept as it is (the person is told what the recipe's own version holds).

Recipes are a plug-in kind (plugins.py, docs/plugins.md#recipes). The files here are the built-in ones; a package adds
more with an entry point in the group agent_testbench.recipes that names its package (NAME.yaml in the package's
folder), a Path to a YAML file, YAML text, a mapping (from_mapping) or a Recipe. Every recipe, built in or installed,
is read by the same rules when it loads - one that sets budget:, pricing: or guard: never loads - and contract() is
what `testbench plugin test` checks a new one against.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
import tempfile
import textwrap
import types
from collections.abc import Mapping
from pathlib import Path

import yaml

from .. import budget, config as C, plugins

HERE = Path(__file__).parent
SECTIONS = ("sessions", "modal", "colab", "docker", "ssh", "data")
NEVER = ("budget", "pricing", "guard")                   # the person's own: a recipe never sets them
KEY = re.compile(r"([A-Za-z_][\w-]*):(?=\s|$)")          # a top-level key, at the start of a line
NAME = re.compile(r"[a-z0-9][a-z0-9-]*")
ORIGIN = "agent-testbench"                               # the package the built-in recipes come with
HEADER_KEYS = ("summary", "proves", "fill_in", "costs", "fill_in_at")      # a mapping's own keys (from_mapping)
EMPTY_NOTEBOOK = json.dumps({"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5})
INTRO = """\
Recipes: testbench.yaml for a goal - a ladder of plans (free here first, a short check on the target hardware, then
the full run) whose checks fail when the result is wrong, each with its FILL IN lines.
  testbench recipes NAME                  print one
  testbench init --recipe NAME            start testbench.yaml from it (repeatable; init adds what it finds here)
  testbench init --add --recipe NAME      add its plans to an existing testbench.yaml, changing nothing in it"""


class RecipeError(ValueError):
    """A recipe cannot be used as asked; the message says what to do instead."""


@dataclasses.dataclass
class Recipe:
    """A recipe as it ships, before any project: its name, its YAML text (the header comment, the sections, the plans
    and every FILL IN comment), the file it was read from (None for text made in Python) and the package it comes
    from (filled in when it is looked up). load() reads one for a project."""
    name: str = ""
    text: str = ""
    path: Path | None = None
    origin: str = ""

    @property
    def summary(self) -> str:
        return parse(self)["summary"]


def _prepare(obj, name: str) -> Recipe:
    """What an entry point (or the built-in table) gives, as a Recipe: a package or module (NAME.yaml in its folder), a
    path to a YAML file, YAML text, a mapping (from_mapping) or a Recipe. It is read by the rules every recipe is read
    by, so one that could not be added to a testbench.yaml, or sets budget:, pricing: or guard:, never loads."""
    if isinstance(obj, Recipe):
        r = dataclasses.replace(obj, name=name or obj.name)          # the name it is looked up by
    elif isinstance(obj, types.ModuleType):
        file = getattr(obj, "__file__", None)
        folder = Path(file).parent if file else Path(next(iter(obj.__path__)))
        path = folder / f"{name}.yaml"
        if not path.is_file():
            raise RecipeError(f"package {obj.__name__} has no {path.name} in {folder}; an entry point that names a "
                              "package loads the YAML file named like the entry point from the package's folder")
        r = Recipe(name, path.read_text(encoding="utf-8"), path)
    elif isinstance(obj, Mapping):
        r = Recipe(name, from_mapping(obj, name))
    elif isinstance(obj, str) and "\n" in obj:
        r = Recipe(name, obj)
    elif isinstance(obj, (str, Path)) or callable(getattr(obj, "read_text", None)):      # a path, or a Traversable
        path = Path(obj) if isinstance(obj, (str, Path)) else None
        if path is not None and not path.is_file():
            raise RecipeError(f"{path} is not a file; a recipe given as a path is a YAML file")
        r = Recipe(name, (path or obj).read_text(encoding="utf-8"), path)
    else:
        raise RecipeError(f"a recipe is a YAML file (a path), YAML text, a mapping or a Recipe; got "
                          f"{type(obj).__name__}")
    if not isinstance(r.name, str) or not NAME.fullmatch(r.name):
        raise RecipeError(f"a recipe's name is lowercase letters, digits and -, like pytorch-train; got {r.name!r}")
    parse(r)
    return r


def _contract(recipe: Recipe) -> list[str]:
    return contract(recipe)


KIND = plugins.Kind(
    "recipes", "recipe", "ready plan ladders for a goal (`testbench recipes`, `init --recipe NAME`)",
    {p.stem: __name__ for p in sorted(HERE.glob("*.yaml"))},       # each file here, found as a plug-in package's are
    prepare=_prepare, contract=_contract)


def names() -> list[str]:
    """Every recipe: the built-in ones, then the ones installed packages add."""
    found = plugins.names("recipes")
    return sorted(n for n in found if plugins.builtin("recipes", n)) + \
        sorted(n for n in found if not plugins.builtin("recipes", n))


def _installed_by(name: str) -> str | None:
    for ep in plugins.entry_points("recipes"):
        if ep.name == name:
            return getattr(getattr(ep, "dist", None), "name", None) or ep.value.split(":")[0].split(".")[0]
    return None


def origin(name: str) -> str:
    """The package a recipe comes from: agent-testbench for the built-in ones, else the package that installed it."""
    if plugins.builtin("recipes", name):
        return ORIGIN
    return _installed_by(name) or "this process (plugins.register)"


def get(name: str) -> Recipe:
    """A recipe by name, built in or installed; a RecipeError that names the recipes there are when there is none."""
    known = plugins.names("recipes")
    if isinstance(name, str) and name not in known and name in plugins.broken("recipes"):
        try:
            plugins.get("recipes", name)
        except plugins.PluginError as exc:
            raise RecipeError(str(exc)) from None
    if not isinstance(name, str) or not NAME.fullmatch(name) or name not in known:
        raise RecipeError(f"no recipe {name!r}{C.near(name, names())}; the recipes are {', '.join(names())} "
                          "(`testbench recipes` says what each is for)")
    r = plugins.get("recipes", name)
    if not r.origin:
        r.origin = origin(name)
    return r


def broken() -> dict[str, str]:
    """Installed recipes that did not load: {name: the package, and why}."""
    return {name: f"from {_installed_by(name) or 'an unknown package'}: {why}"
            for name, why in plugins.broken("recipes").items()}


def blocks(text: str) -> list[list]:
    """The top-level sections of a YAML file with their comments: [key, lines] for `key:`, everything under it and the
    comment lines right above it; [None, lines] for comments that stand alone (a header, a section commented out)."""
    out: list[list] = [[None, []]]
    for line in text.splitlines():
        m = KEY.match(line)
        if not m:
            out[-1][1].append(line)
            continue
        lines = out[-1][1]
        cut = len(lines)
        while cut and (not lines[cut - 1].strip() or lines[cut - 1].startswith("#")):
            cut -= 1                                     # the blank and comment lines that end the block before
        tail = lines[cut:]
        del lines[cut:]
        own = len(tail)
        while own and tail[own - 1].startswith("#"):
            own -= 1                                     # the comment lines right above the key are its own
        if any(t.startswith("#") for t in tail[:own]):
            out.append([None, tail[:own]])
        else:
            lines += tail[:own]
        out.append([m.group(1), tail[own:] + [line]])
    return [b for b in out if b[1]]


def _text(bl: list[list]) -> str:
    return "\n".join(line for _, lines in bl for line in lines).rstrip("\n") + "\n"


def _trim(lines: list[str]) -> list[str]:
    """Without the blank lines at either end."""
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def _find(bl: list[list], key: str) -> int | None:
    return next((i for i, (k, _) in enumerate(bl) if k == key), None)


def stand_ins(root: Path) -> dict[str, str]:
    """The project's own files for the files recipes name, where the project has no file of that name: its first
    notebook (an .ipynb, else a percent-format .py) for notebook.ipynb, its training script for train.py and
    finetune.py (quoted for YAML when needed)."""
    from ..scaffold import _percent_notebooks, _q, _walk, trainer_script
    root = Path(root)
    notebook = next((p.relative_to(root).as_posix() for p in _walk(root, "*.ipynb", 1) or _percent_notebooks(root, 1)),
                    None)
    trainer = trainer_script(root)
    own = {"notebook.ipynb": notebook, "train.py": trainer, "finetune.py": trainer}
    return {name: _q(path) for name, path in own.items() if path and path != name and not (root / name).is_file()}


def load(name: str, root: Path | None = None) -> dict:
    """A recipe by name, built in or installed, read for a project (parse)."""
    return parse(get(name), root)


def parse(recipe: Recipe, root: Path | None = None) -> dict:
    """A recipe read: name, path, origin, text, summary (its first line), header (its leading comments), sections
    ({key: lines}), plan_lines (its plans, as indented in the file), plans and data (as YAML reads them). With root,
    the project's own notebook and training script replace the files the recipe names (`swapped`: {what it named:
    what it runs}). The rules every recipe is read by, so that it can be added to a testbench.yaml as text: sections
    among SECTIONS, each once, then `plans:` with at least one plan; every top-level key at the start of its line and
    everything under it indented; never budget:, pricing: or guard:."""
    name, text, swapped = recipe.name, recipe.text, {}
    where = f"recipe {name}" + (f" ({recipe.path})" if recipe.path else "")
    for stand_in, own in (stand_ins(root) if root is not None else {}).items():
        pattern = re.compile(rf"(?<![\w./-]){re.escape(stand_in)}(?![\w-]|\.\w)")
        if pattern.search(text):
            text, swapped[stand_in] = pattern.sub(lambda m: own, text), own
    bl = blocks(text)
    keys = [k for k, _ in bl if k]
    banned = [f"`{k}:`" for k in keys if k in NEVER]
    if banned:
        raise RecipeError(f"{where} sets {' and '.join(banned)}, which stay{'s' * (len(banned) == 1)} the person's: a "
                          "recipe never sets budget:, pricing: or guard: (the project's own limits and prices decide "
                          f"what its plans may cost). Take {'them' if len(banned) > 1 else 'it'} out of the recipe")
    if not keys or keys[-1] != "plans" or len(set(keys)) != len(keys) or set(keys[:-1]) - set(SECTIONS):
        raise RecipeError(f"{where} must hold sections among {', '.join(SECTIONS)}, each once, then `plans:`; it has "
                          f"{', '.join(keys) or 'none'}")
    for key, lines in bl:
        stray = [ln for ln in lines if ln.strip() and not ln[0].isspace() and not ln.startswith("#") and
                 not KEY.match(ln)] if key else []
        if stray:
            raise RecipeError(f"{where}: `{stray[0][:60]}` under `{key}:` starts at the beginning of its line; indent "
                              "everything under a section, so it stays there when the recipe is added to a file")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RecipeError(f"{where} is not valid YAML: {exc}") from None
    if not isinstance(data, dict) or [str(k) for k in data] != keys:
        raise RecipeError(f"{where}: YAML reads its top-level keys as {', '.join(map(str, data or {})) or 'none'}, but "
                          f"the lines that start with a key say {', '.join(keys)}; write each top-level key unquoted "
                          "at the start of its own line (`plans:`), so the recipe can be added to a file as text")
    if not isinstance(data["plans"], dict) or not data["plans"]:
        raise RecipeError(f"{where}: `plans:` maps plan names to plans, and a recipe has at least one")
    for key in keys[:-1]:
        if data[key] is not None and not isinstance(data[key], dict):
            raise RecipeError(f"{where}: `{key}:` is a mapping of settings, as in testbench.yaml; got {data[key]!r}")
    first = next(i for i, (k, _) in enumerate(bl) if k)
    plans = bl[-1][1]
    at = next(i for i, line in enumerate(plans) if KEY.match(line))
    header = _trim([line for _, lines in bl[:first] for line in lines] + plans[:at])
    summary = re.sub(rf"^#\s*(?:Recipe\s+{re.escape(name)}:\s*)?", "", header[0]) if header else ""
    return {"name": name, "path": recipe.path, "origin": recipe.origin, "text": text, "summary": summary,
            "header": header, "sections": {k: _trim(lines) for k, lines in bl if k and k != "plans"},
            "plan_lines": _trim(plans[at + 1:]), "plans": data["plans"], "data": data, "swapped": swapped}


def from_mapping(data: Mapping, name: str) -> str:
    """A recipe's YAML text from a mapping: `summary` (one line: what it is for), `proves`, `fill_in` and `costs` (its
    header comment), `fill_in_at` ({dotted path: what to fill in}, each written as a `# FILL IN:` comment at the end of
    its line, since a mapping holds no comments), then its sections and `plans:` as testbench.yaml has them - plain
    mappings, lists, strings, numbers and booleans:

        {"summary": "a command-line tool answers --help", "proves": "...", "fill_in": "...", "costs": "nothing",
         "fill_in_at": {"plans.cli.steps.0.run": "your tool's command"},
         "plans": {"cli": {"backend": "local", "steps": [{"name": "help", "run": "mytool --help", ...}]}}}"""
    rest = dict(data)
    head = {k: rest.pop(k) for k in HEADER_KEYS if k in rest}
    summary = head.get("summary")
    if not isinstance(summary, str) or not summary.strip() or "\n" in summary:
        raise RecipeError(f"recipe {name}, given as a mapping, needs `summary`: one line saying what it is for")
    hints = dict(head.get("fill_in_at") or {})
    lines = [f"# Recipe {name}: {summary.strip()}", "#"]
    for key, label in (("proves", "Proves"), ("fill_in", "FILL IN"), ("costs", "Costs")):
        if head.get(key):
            lines += textwrap.wrap(f"{label}: {head[key]}", 116, initial_indent="# ", subsequent_indent="#   ")
    body = {k: v for k, v in rest.items() if k != "plans"} | ({"plans": rest["plans"]} if "plans" in rest else {})
    out: list[str] = []
    try:
        _emit(body, "", 0, hints, out)
    except yaml.YAMLError as exc:
        raise RecipeError(f"recipe {name} holds a value YAML cannot write ({exc}); use plain mappings, lists, strings, "
                          "numbers and booleans") from None
    if hints:
        raise RecipeError(f"recipe {name}'s fill_in_at names {', '.join(map(str, hints))}, not in its sections or "
                          "plans; give dotted paths such as plans.NAME.steps.0.run")
    text = "\n".join(lines + [""] + out) + "\n"
    if yaml.safe_load(text) != body:
        raise RecipeError(f"recipe {name} does not read back the same once written as YAML; give it as YAML text")
    return text


def _flow(value) -> str:
    """One value on one line of YAML."""
    if isinstance(value, str) and any(c in value for c in "\n\r\t"):
        return json.dumps(value, ensure_ascii=False)              # a JSON string is a YAML double-quoted one
    text = yaml.safe_dump(value, default_flow_style=True, width=1 << 30, allow_unicode=True, sort_keys=False)
    text = text.rstrip("\n")
    return text[:-4] if text.endswith("\n...") else text


def _block(value) -> bool:
    return (isinstance(value, Mapping) and bool(value)) or \
        (isinstance(value, list) and any(isinstance(x, (Mapping, list)) for x in value))


def _emit(value, path: str, indent: int, hints: dict, out: list[str]) -> None:
    """value as block-style YAML lines, 2 spaces a level, `# FILL IN: hint` on the lines hints names by dotted path."""
    pad = " " * indent

    def mark(p: str) -> str:
        return f"    # FILL IN: {hints.pop(p)}" if p in hints else ""

    if isinstance(value, Mapping):
        for k, v in value.items():
            p = f"{path}.{k}" if path else str(k)
            if _block(v):
                out.append(f"{pad}{_flow(k)}:{mark(p)}")
                _emit(v, p, indent + 2, hints, out)
            else:
                out.append(f"{pad}{_flow(k)}: {_flow(v)}{mark(p)}")
        return
    for i, v in enumerate(value):
        p = f"{path}.{i}"
        if isinstance(v, Mapping) and v:
            at = len(out)
            _emit(v, p, indent + 2, hints, out)
            out[at] = f"{pad}- {out[at][indent + 2:]}"
        elif _block(v):
            out.append(f"{pad}-{mark(p)}")
            _emit(v, p, indent + 2, hints, out)
        else:
            out.append(f"{pad}- {_flow(v)}{mark(p)}")


def _shift(line: str, by: int) -> str:
    if not line.strip():
        return ""
    return " " * by + line if by >= 0 else line[min(-by, len(line) - len(line.lstrip(" "))):]


def _insert(bl: list[list], at: int | None, key: str, lines: list[str]) -> None:
    at = len(bl) if at is None else at
    gap = [""] if at and bl[at - 1][1] and bl[at - 1][1][-1].strip() else []
    bl.insert(at, [key, gap + lines + [""]])


def _add_plans(bl: list[list], recipe: dict) -> None:
    """The recipe's plans, under its header, after the plans already in the file (indented as those are)."""
    at = _find(bl, "plans")
    if at is None:
        _insert(bl, None, "plans", ["plans:"])
        at = len(bl) - 1
    lines = bl[at][1]
    k = next(i for i, line in enumerate(lines) if KEY.match(line))
    if lines[k].split(":", 1)[1].split("#", 1)[0].strip():
        raise RecipeError(f"`plans:` in {C.CONFIG_NAME} is written on one line (`{lines[k].strip()}`); write it as a "
                          "block, one plan per indented entry, then add the recipe")
    body = [line for line in lines[k + 1:] if line.strip() and not line.lstrip().startswith("#")]
    indent = len(body[0]) - len(body[0].lstrip(" ")) if body else 2
    end = len(lines)
    while end > k + 1 and not lines[end - 1].strip():
        end -= 1
    new = [_shift(line, indent) for line in recipe["header"]] + [_shift(line, indent - 2) for line in recipe["plan_lines"]]
    bl[at][1] = lines[:end] + ([""] if end > k + 1 else []) + new + lines[end:]


def compose(text: str, recipes: list[dict], fresh: bool = False) -> tuple[str, list[str]]:
    """`text` (a testbench.yaml) with each recipe's plans after the plans already there, under the recipe's header, and
    each section a recipe needs added where the file has none. Nothing in the file changes - except in one init has
    just written (fresh), where a recipe's `sessions:` replaces the default and a commented-out example of a section
    the recipe sets is dropped. An existing file never gets a recipe's `sessions:` (it would move `session start`'s
    kernels without a word): a note says what to add. A plan name that is taken is refused. Returns (the new text, notes for the person)."""
    try:
        taken = dict.fromkeys(map(str, (yaml.safe_load(text) or {}).get("plans") or {}), C.CONFIG_NAME)
    except (yaml.YAMLError, AttributeError) as exc:
        raise RecipeError(f"{C.CONFIG_NAME} is not a valid YAML mapping ({exc}); fix it, then add the recipe") from None
    bl, replaceable, notes = blocks(text), {"sessions"} if fresh else set(), []
    for r in recipes:
        for plan in r["plans"]:
            if plan in taken:
                fix = (f"Rename that plan in {C.CONFIG_NAME}, or copy what you need from `testbench recipes {r['name']}` "
                       "by hand." if taken[plan] == C.CONFIG_NAME else
                       f"Use one of the two, and copy what you need from the other (`testbench recipes {r['name']}`).")
                raise RecipeError(f"recipe {r['name']} has a plan named {plan}, and {taken[plan]} already has one, so "
                                  f"nothing was written. {fix}")
        for key, lines in r["sections"].items():
            at = _find(bl, key)
            if at is not None and key in replaceable:
                old = bl[at][1]
                bl[at] = [key, lines + [""] * (len(old) - len(_trim(old)))]       # the same blank lines after it
                replaceable.discard(key)
                notes.append(f"`sessions:` is recipe {r['name']}'s: `testbench session start` with no --backend starts "
                             f"a kernel on {(r['data'].get('sessions') or {}).get('backend', 'local')}.")
            elif at is not None:
                why = ("where `testbench session start` puts its kernel; recipe {} would put it here" if key == "sessions"
                       else "recipe {}'s plans were written for this one - make sure yours has what they need")
                notes.append(f"Kept the `{key}:` section already in {C.CONFIG_NAME} ({why.format(r['name'])}):\n"
                             + "\n".join(_shift(line, 4) for line in lines))
            elif key == "sessions" and not fresh:
                backend = (r["data"].get("sessions") or {}).get("backend", "local")      # the plans do not need it
                notes.append(f"Left out recipe {r['name']}'s `sessions:` section: {C.CONFIG_NAME} has none, so "
                             "`testbench session start` with no --backend still starts a kernel on local. For one on "
                             f"{backend}, run `testbench session start --backend {backend}`, or add this to "
                             f"{C.CONFIG_NAME} to make it the default:\n" + "\n".join(_shift(line, 4) for line in lines))
            else:
                if fresh:
                    bl = [b for b in bl if not (b[0] is None and re.match(rf"#\s*{key}:", (_trim(b[1]) or [""])[0]))]
                _insert(bl, _find(bl, "plans"), key, lines)
        _add_plans(bl, r)
        taken.update(dict.fromkeys(r["plans"], f"recipe {r['name']}"))
    return _text(bl), notes


RUNNERS = (("jest", r"\bjest\b"), ("vitest", r"\bvitest\b"), ("mocha", r"\bmocha\b"),    # other languages' suites
           ("node-test", r"\bnode\b.*\s--test\b"), ("playwright", r"\bplaywright\s+test\b"),
           ("go-test", r"\bgotestsum\b|\bgo\s+test\b"), ("cargo-test", r"\bcargo\s+(?:nextest|test)\b"))


def subjects(plans: dict) -> set[tuple[str, str]]:
    """What plans exercise - each notebook and Python script, a pytest suite or another test runner's suite, a web
    page - so init can leave out a plan it detected when a recipe's plans already cover the same thing."""
    out = set()
    for name, plan in plans.items():
        plan = C.long_form(name, plan)                   # `tests: "pytest -q"` is a plan too
        for step in (plan.get("steps") if isinstance(plan, dict) else None) or []:
            if not isinstance(step, dict):
                continue
            if step.get("notebook"):
                out.add(("notebook", str(step["notebook"])))
            if step.get("web"):
                out.add(("web", ""))
            run = str(step.get("run") or "")
            out |= {("script", f) for f in re.findall(r"[\w./-]+\.py\b", run)}
            if re.search(r"\bpytest\b", run):
                out.add(("pytest", ""))
            out |= {(kind, "") for kind, pattern in RUNNERS if re.search(pattern, run)}
    return out


MOCK_STAND_INS = {"prices": "time,symbol,close\n1970-01-02,STAND-IN,1\n", "replies": "[]\n"}   # files mocks: read
MOCK_FILES = {"prices": "a CSV of time,symbol,close (docs/mocks.md#prices-and-the-clock)",
              "replies": "the mock's replies (docs/mocks.md#replies)"}


def mock_files(plans) -> list[tuple[str, str, str, dict]]:
    """The files the plans' mocks read when testbench.yaml loads - a broker's prices, an LLM client's replies:
    (plan, mock, option, the options) for each file option a step's `mocks:` gives."""
    from .. import mocks as M
    out = []
    for name, plan in (plans if isinstance(plans, dict) else {}).items():
        plan = C.long_form(name, plan)
        for step in (plan.get("steps") if isinstance(plan, dict) else None) or []:
            given = step.get("mocks") if isinstance(step, dict) else None
            for sdk, opts in (given.items() if isinstance(given, dict) else ()):
                try:
                    keys = M.get(sdk).files
                except Exception:                        # a mock testbench has none of: validate says so
                    continue
                out += [(str(name), sdk, key, opts) for key in keys if isinstance(opts, dict)
                        and isinstance(opts.get(key), str)]
    return out


def check(text: str, root: Path) -> dict:
    """The config `text` loads as; a RecipeError saying why when it would not load, so nothing broken is written."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RecipeError(f"the combined {C.CONFIG_NAME} would not be valid YAML ({exc}); nothing was written") from None
    plans = raw.get("plans") if isinstance(raw, dict) else None
    for name, plan in (plans if isinstance(plans, dict) else {}).items():
        plan = C.long_form(name, plan)                   # a short form has no notebook, but its steps may be mappings
        for step in (plan.get("steps") if isinstance(plan, dict) else None) or []:
            nb = step.get("notebook") if isinstance(step, dict) else None
            if isinstance(nb, str) and not (Path(root) / nb).is_file():
                raise RecipeError(f"plan {name} runs the notebook {nb}, which is not in this project, and there is no "
                                  "notebook here to use instead, so nothing was written. Put the notebook in the project "
                                  "(any folder), then run this again.")
    for name, sdk, key, opts in mock_files(plans):
        if not (Path(root) / opts[key]).is_file():
            raise RecipeError(f"plan {name}'s mock {sdk} reads {opts[key]} (its `{key}:`), which is not in this "
                              f"project, so nothing was written. Put {MOCK_FILES.get(key, 'the file')} there, then run "
                              "this again.")
    try:
        return C.validate(raw, Path(root))
    except C.ConfigError as exc:
        raise RecipeError(f"the combined {C.CONFIG_NAME} would not load, so nothing was written: {exc}") from None


def config(recipe: dict, base: dict | None = None) -> dict:
    """The recipe's plans as a loaded config, to price them: with the budget and prices of `base` (a project's loaded
    config) when given, else the defaults. Its notebooks and the files its mocks read need not exist: a recipe file
    stands in for each notebook, a minimal file for each mock's."""
    raw = copy.deepcopy(recipe["data"])
    raw["project"] = "recipe"
    for key in ("budget", "pricing"):
        if base and base.get(key):
            raw[key] = copy.deepcopy(base[key])
    path = recipe.get("path")
    path = path if isinstance(path, Path) and path.is_file() and path.suffix.lower() != ".py" else \
        min(HERE.glob("*.yaml"))                         # a recipe made in Python has no file: a built-in one stands in
    for name in raw["plans"]:
        plan = raw["plans"][name] = C.long_form(name, raw["plans"][name])
        for step in (plan.get("steps") if isinstance(plan, dict) else None) or []:
            if isinstance(step, dict) and "notebook" in step:
                step["notebook"] = str(path)            # an absolute path that exists: pricing reads no notebook
    with tempfile.TemporaryDirectory() as tmp:
        for i, (_, _, key, opts) in enumerate(mock_files(raw["plans"])):
            stand_in = Path(tmp) / f"{i}-{key}"
            stand_in.write_text(MOCK_STAND_INS.get(key, ""), encoding="utf-8")
            opts[key] = str(stand_in)                    # absolute: read when the config loads, never to price it
        return C.validate(raw, path.parent)


def worst_case(cfg: dict, plan_name: str) -> dict:
    """budget.worst_case - or, for a walk-forward plan whose data cannot be cut yet (a recipe ships without the data),
    the bound a launch allows: its steps in at most dataseal.MAX_WINDOWS windows (`max_windows`; the data decides how
    many, and its launch is priced by those)."""
    try:
        return budget.worst_case(cfg, plan_name)
    except C.ConfigError:
        plan = cfg["plans"][plan_name]
        if not plan.get("walk_forward"):
            raise
    from .. import dataseal
    n = dataseal.MAX_WINDOWS
    return {**budget.estimate(cfg, plan, C.plan_minutes(plan) * n, f"plan {plan_name} (at most {n} walk-forward "
                              "windows)"), "plan": plan_name, "max_windows": n}


def describe(cfg: dict, plan_name: str) -> str:
    """A plan's hardware, time limit and worst case, and what the budget says about that worst case."""
    plan, est, lim = cfg["plans"][plan_name], worst_case(cfg, plan_name), budget.limits(cfg)
    usd = est["worst_case_usd"]
    minutes = (f"up to {C.plan_minutes(plan):g} min a window (the data decides how many; at most {est['max_windows']})"
               if est.get("max_windows") else f"up to {est['max_minutes']:g} min")
    text = (f"{plan['backend']}" + (f" {plan['gpu']}" if plan.get("gpu") else "") + f", {minutes}, "
            + ("free" if not usd else f"worst case ${usd:.2f}" if usd >= 0.005 else "worst case under $0.01"))
    if usd > lim["max_run_usd"][0]:
        text += f" - over {lim['max_run_usd'][1]}: refused until a person raises it"
    elif usd > lim["ask_above_usd"][0]:
        text += f" - above {lim['ask_above_usd'][1]}: a person approves it"
    return text


def listing(base: dict | None = None) -> str:
    """Every recipe: what it is for, the backends it uses, and each plan's hardware, time limit and worst case."""
    where = f"project {base['project']}'s prices and budget" if base else "the built-in prices and the default budget"
    out = [INTRO, "", f"Worst cases at {where}."]
    bad = broken()
    if bad or any(not plugins.builtin("recipes", n) for n in names()):
        out.append(f"Recipes from installed packages name their package; the others come with {ORIGIN}.")
    out.append("")
    for name in names():
        r = load(name)
        tag = "" if r["origin"] == ORIGIN else f" - from {r['origin']}"
        try:
            cfg = config(r, base)
        except (C.ConfigError, ValueError) as exc:       # e.g. a plug-in recipe for a backend not installed here
            out += [f"{name}{tag}", f"  {r['summary']}", f"    cannot be priced here: {exc}", ""]
            continue
        used = dict.fromkeys(p["backend"] for p in cfg["plans"].values())
        out += [f"{name} ({', '.join(used)}){tag}", f"  {r['summary']}"]
        out += [f"    {plan:<19} {describe(cfg, plan)}" for plan in cfg["plans"]]
        out.append("")
    if bad:
        out.append("Installed but not loaded (fix or reinstall the package; `testbench plugins` lists every plug-in):")
        out += [f"  {name} {why}" for name, why in bad.items()]
    return "\n".join(out).rstrip()


def contract(recipe: Recipe) -> list[str]:
    """What `testbench plugin test` checks a recipe for, in plain words ([] = it keeps the contract): it reads by the
    rules every recipe is read by (parse: sections, then plans; never budget:, pricing: or guard:); its header says
    what it is for, what it proves, what to FILL IN and what it costs, and the lines to fill in are marked; its plans
    load through config under the header `testbench init` writes, as `init --recipe` checks them; and every plan's
    worst case is a number of dollars within the default budget, so nothing in it is refused as it ships."""
    from ..cli import INIT_TEMPLATE
    try:
        r = parse(recipe)
    except RecipeError as exc:
        return [str(exc)]
    out, head = [], "\n".join(r["header"])
    if not r["header"] or not r["header"][0].startswith(f"# Recipe {recipe.name}: ") or not r["summary"].strip():
        out.append(f"its first line is `# Recipe {recipe.name}: <what it is for>`, the line `testbench recipes` shows")
    for part, what in (("Proves:", "what it proves"), ("FILL IN:", "what the person fills in"),
                       ("Costs:", "what it costs")):
        if part not in head:
            out.append(f"its header comment does not say {what}: add a `# {part} ...` paragraph")
    body = r["plan_lines"] + [line for lines in r["sections"].values() for line in lines]
    nothing = re.search(r"FILL IN:\s*nothing", head, re.I)
    if not any(re.search(r"#.*\bFILL IN\b", line) for line in body) and not nothing:
        out.append("no line is marked `# FILL IN`: mark each value only the person knows (a command's arguments, the "
                   "number that means working), or say `FILL IN: nothing` in the header")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        for name, plan in r["plans"].items():            # the notebooks it names stand for the project's own
            plan = C.long_form(name, plan)
            for step in (plan.get("steps") if isinstance(plan, dict) else None) or []:
                nb = step.get("notebook") if isinstance(step, dict) else None
                if not isinstance(nb, str):
                    continue
                f = (root / nb).resolve()
                if not f.is_relative_to(root):
                    return out + [f"plan {name} runs the notebook {nb}, outside the project folder; name it by a path "
                                  "inside the project"]
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text("# %%\n" if f.suffix.lower() == ".py" else EMPTY_NOTEBOOK, encoding="utf-8")
        for name, sdk, key, opts in mock_files(r["plans"]):      # and the files its mocks read stand for the project's
            f = (root / opts[key]).resolve()
            if not f.is_relative_to(root):
                return out + [f"plan {name}'s mock {sdk} reads {opts[key]}, outside the project folder; name it by a "
                              "path inside the project"]
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(MOCK_STAND_INS.get(key, ""), encoding="utf-8")
        try:
            text, _ = compose(INIT_TEMPLATE.format(project="recipe-check", plans=""), [r], fresh=True)
            cfg = check(text, root)
        except RecipeError as exc:
            return out + [f"its plans do not load under the header `testbench init` writes: {exc}"]
    most = cfg["budget"]["max_run_usd"]
    for plan in cfg["plans"]:
        try:
            usd = worst_case(cfg, plan)["worst_case_usd"]
        except Exception as exc:                         # a plug-in's backend that cannot price it
            out.append(f"plan {plan}'s worst case cannot be worked out: {type(exc).__name__}: {exc}")
            continue
        if not C.number(usd):
            out.append(f"plan {plan}'s worst case is {usd!r}, not a number of dollars")
        elif usd > most:
            out.append(f"plan {plan}'s worst case ${usd:.2f} is over the default budget.max_run_usd ${most:.2f}, so it "
                       "would be refused as it ships: lower its max_minutes or pick smaller hardware")
    return out
