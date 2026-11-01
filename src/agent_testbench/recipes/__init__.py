"""Recipes: testbench.yaml for a goal ("fine-tune a model", "check my web app") rather than for what a project holds.

`testbench init` writes plans for what it finds in a project; a recipe starts from what the person wants to prove,
with the ladder of plans that proves it: a free check on this machine first, a short one on the target hardware,
then the full run - each step with expectations that fail when the result is wrong, not just when it crashes, and
max_minutes that bound what it can cost.

A recipe is a YAML file in this folder: a header comment (what it proves, what to FILL IN, what it costs), the
sections its plans need (`sessions:`, `modal:`, `colab:`, `docker:`, `ssh:`), then `plans:` - never `budget:`,
`pricing:` or `guard:`, which stay the person's. Recipes are added to a testbench.yaml as text, so their comments come
along and nothing already in the file changes: a plan name that is taken is refused, and a section the file already
has is kept as it is (the person is told what the recipe's own version holds).
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import yaml

from .. import budget, config as C

HERE = Path(__file__).parent
SECTIONS = ("sessions", "modal", "colab", "docker", "ssh")
KEY = re.compile(r"([A-Za-z_][\w-]*):(?=\s|$)")          # a top-level key, at the start of a line
INTRO = """\
Recipes: testbench.yaml for a goal - a ladder of plans (free here first, a short check on the target hardware, then
the full run) whose checks fail when the result is wrong, each with its FILL IN lines.
  testbench recipes NAME                  print one
  testbench init --recipe NAME            start testbench.yaml from it (repeatable; init adds what it finds here)
  testbench init --add --recipe NAME      add its plans to an existing testbench.yaml, changing nothing in it"""


class RecipeError(ValueError):
    """A recipe cannot be used as asked; the message says what to do instead."""


def names() -> list[str]:
    return sorted(p.stem for p in HERE.glob("*.yaml"))


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
    """A recipe: name, path, text, summary (its first line), header (its leading comments), sections ({key: lines}),
    plan_lines (its plans, as indented in the file), plans and data (as YAML reads them). With root, the project's
    own notebook and training script replace the files the recipe names (`swapped`: {what it named: what it runs})."""
    path = HERE / f"{name}.yaml"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", str(name)) or not path.is_file():
        raise RecipeError(f"no recipe {name!r}; the recipes are {', '.join(names())} (`testbench recipes` says what each "
                          "is for)")
    text, swapped = path.read_text(encoding="utf-8"), {}
    for stand_in, own in (stand_ins(root) if root is not None else {}).items():
        pattern = re.compile(rf"(?<![\w./-]){re.escape(stand_in)}(?![\w-]|\.\w)")
        if pattern.search(text):
            text, swapped[stand_in] = pattern.sub(lambda m: own, text), own
    bl = blocks(text)
    keys = [k for k, _ in bl if k]
    if not keys or keys[-1] != "plans" or len(set(keys)) != len(keys) or set(keys[:-1]) - set(SECTIONS):
        raise RecipeError(f"recipe {name} ({path}) must hold sections among {', '.join(SECTIONS)}, each once, then "
                          f"`plans:`; it has {', '.join(keys) or 'none'}")
    first = next(i for i, (k, _) in enumerate(bl) if k)
    plans = bl[-1][1]
    at = next(i for i, line in enumerate(plans) if KEY.match(line))
    header = _trim([line for _, lines in bl[:first] for line in lines] + plans[:at])
    data = yaml.safe_load(text)
    summary = re.sub(rf"^#\s*Recipe\s+{re.escape(name)}:\s*", "", header[0]) if header else ""
    return {"name": name, "path": path, "text": text, "summary": summary, "header": header,
            "sections": {k: _trim(lines) for k, lines in bl if k and k != "plans"}, "plan_lines": _trim(plans[at + 1:]),
            "plans": data["plans"], "data": data, "swapped": swapped}


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
    for plan in plans.values():
        for step in (plan or {}).get("steps") or []:
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


def check(text: str, root: Path) -> dict:
    """The config `text` loads as; a RecipeError saying why when it would not load, so nothing broken is written."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RecipeError(f"the combined {C.CONFIG_NAME} would not be valid YAML ({exc}); nothing was written") from None
    for name, plan in (raw.get("plans") or {}).items():
        for step in (plan or {}).get("steps") or []:
            nb = step.get("notebook") if isinstance(step, dict) else None
            if isinstance(nb, str) and not (Path(root) / nb).is_file():
                raise RecipeError(f"plan {name} runs the notebook {nb}, which is not in this project, and there is no "
                                  "notebook here to use instead, so nothing was written. Put the notebook in the project "
                                  "(any folder), then run this again.")
    try:
        return C.validate(raw, Path(root))
    except C.ConfigError as exc:
        raise RecipeError(f"the combined {C.CONFIG_NAME} would not load, so nothing was written: {exc}") from None


def config(recipe: dict, base: dict | None = None) -> dict:
    """The recipe's plans as a loaded config, to price them: with the budget and prices of `base` (a project's loaded
    config) when given, else the defaults. Its notebooks need not exist: the recipe file stands in for each."""
    raw = copy.deepcopy(recipe["data"])
    raw["project"] = "recipe"
    for key in ("budget", "pricing"):
        if base and base.get(key):
            raw[key] = copy.deepcopy(base[key])
    for plan in raw["plans"].values():
        for step in plan.get("steps") or []:
            if "notebook" in step:
                step["notebook"] = str(recipe["path"])      # an absolute path that exists: pricing reads no notebook
    return C.validate(raw, recipe["path"].parent)


def describe(cfg: dict, plan_name: str) -> str:
    """A plan's hardware, time limit and worst case, and what the budget says about that worst case."""
    plan, est, lim = cfg["plans"][plan_name], budget.worst_case(cfg, plan_name), budget.limits(cfg)
    usd = est["worst_case_usd"]
    text = (f"{plan['backend']}" + (f" {plan['gpu']}" if plan.get("gpu") else "") + f", up to {est['max_minutes']:g} "
            "min, " + ("free" if not usd else f"worst case ${usd:.2f}" if usd >= 0.005 else "worst case under $0.01"))
    if usd > lim["max_run_usd"][0]:
        text += f" - over {lim['max_run_usd'][1]}: refused until a person raises it"
    elif usd > lim["ask_above_usd"][0]:
        text += f" - above {lim['ask_above_usd'][1]}: a person approves it"
    return text


def listing(base: dict | None = None) -> str:
    """Every recipe: what it is for, the backends it uses, and each plan's hardware, time limit and worst case."""
    where = f"project {base['project']}'s prices and budget" if base else "the built-in prices and the default budget"
    out = [INTRO, "", f"Worst cases at {where}.", ""]
    for name in names():
        r = load(name)
        cfg = config(r, base)
        used = dict.fromkeys(p["backend"] for p in cfg["plans"].values())
        out += [f"{name} ({', '.join(used)})", f"  {r['summary']}"]
        out += [f"    {plan:<19} {describe(cfg, plan)}" for plan in cfg["plans"]]
        out.append("")
    return "\n".join(out).rstrip()
