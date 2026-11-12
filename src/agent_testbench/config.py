"""testbench.yaml: what a project lets its testbench run, where, and for how much.

Every error names the field and says what to write instead, because the reader is often an agent that
will fix the file from the message alone.
"""
from __future__ import annotations

import copy
import difflib
import json
import math
import re
import shlex
from pathlib import Path

import yaml

CONFIG_NAME = "testbench.yaml"
SCHEMA = Path(__file__).parent / "schema" / "testbench.schema.json"      # for editors: `testbench schema` prints it
SCHEMA_URL = ("https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/src/agent_testbench/schema/"
              "testbench.schema.json")
STEP_KINDS = ("notebook", "run", "web")

NEVER_COPIED = [".testbench", ".env", ".env.*"]    # secrets go by name (`secrets:`), never in a copy of the project
DEFAULTS = {
    "exclude": [".git", ".testbench", "__pycache__", "*.pyc", ".venv", "venv", "node_modules", ".ipynb_checkpoints",
                ".DS_Store"],
    "budget": {"max_run_usd": 2.0, "ask_above_usd": 0.5, "max_day_usd": 10.0},
    "pricing": {},
    "guard": {"ask": ["git push", "gh repo create", "gh pr merge", "gh release create"]},
    "sessions": {"backend": "local", "gpu": None, "idle_minutes": 10, "max_minutes": 60, "secrets": []},
}
PLAN_DEFAULTS = {"backend": "local", "gpu": None, "workdir": None, "description": ""}
PLAN_DATA_KEYS = ("holdout", "walk_forward", "lookahead", "model_cutoff")   # the data seal's (dataseal.py) keys
STEP_DEFAULTS = {"max_minutes": 10, "params": {}, "env": {}, "secrets": [], "pythonpath": [], "artifacts": [],
                 "expect": {}, "uploads": [], "cells": None, "colab_shim": True, "continue_on_failure": False,
                 "mocks": {}}
WEB_DEFAULTS = {"serve": None, "ready_timeout": 30, "actions": [], "screenshot": True, "viewport": [1280, 800]}
PLAN_FORMS = """\
  tests: "pytest -q"                                one command, run on this machine
  checks: ["ruff check .", "pytest -q"]             commands run in order, on this machine
  train: {backend: modal, gpu: T4, steps: [...]}    the long form: backend, gpu, description, steps"""
STEP_FORMS = """\
  - "pytest -q"                                     a command; the step is named after its program
  - {name: train, run: python train.py, max_minutes: 30}
  - {name: analysis, notebook: notebooks/analysis.ipynb}
  - {name: app, web: {url: "http://127.0.0.1:8000/", serve: python app.py}}"""
NOTEBOOK_ONLY = {"params": "it sets a notebook's Colab form fields and papermill parameters",
                 "cells": "it picks the notebook's code cells to run", "uploads": "it answers files.upload() in a notebook",
                 "colab_shim": "it puts the google.colab stand-in in the notebook's kernel"}


class ConfigError(ValueError):
    pass


def near(word, names, cutoff: float = 0.6) -> str:
    """' (did you mean X?)' when word is a near miss for one of names, else ''."""
    hit = difflib.get_close_matches(str(word), [str(n) for n in names], n=1, cutoff=cutoff)
    return f" (did you mean {hit[0]}?)" if hit else ""


def no_plan(cfg: dict, name) -> str:
    """What to say when testbench.yaml has no plan of that name: the closest one, and every plan."""
    if not cfg["plans"]:
        return (f"No plan {name!r}: {CONFIG_NAME} has no plans yet. Add one under `plans:` (`testbench recipes` has "
                "ready ones, `testbench init --add --recipe NAME` adds them)")
    return (f"No plan {name!r}{near(name, cfg['plans'])}. Plans: {', '.join(cfg['plans'])}. `testbench plans` shows "
            "each with its hardware and worst case")


def find_root(start: str | Path = ".") -> Path:
    """The nearest directory at or above `start` that holds testbench.yaml, or the record `testbench check` keeps of
    what it found and ran in a folder without one (CHECKED)."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file() or (d / CHECKED).is_file():
            return d
    raise ConfigError(f"No {CONFIG_NAME} here or in any parent of {here}. Run `testbench check` in the project folder "
                      f"to run its tests, notebooks or app as they are, or `testbench init` to write {CONFIG_NAME} with "
                      "a plan for each.")


CHECKED = Path(".testbench") / "check.json"     # what `testbench check` found and ran where there is no testbench.yaml


def checked(root: str | Path, record: dict | None = None) -> dict:
    """The config of a folder with no testbench.yaml where `testbench check` ran: the plans `testbench init` would write
    there (from .testbench/check.json, or `record` before check writes it) with every default, budget included. Only
    `project` and `plans` are read from the record, and every plan must run on this machine: a record cannot set a
    budget, a price or a machine that costs money."""
    root, p = Path(root), Path(root) / CHECKED
    again = f"run `testbench check` there again to look anew, or `testbench init` to write {CONFIG_NAME}"
    try:
        rec = json.loads(p.read_text()) if record is None else record
        plans = _parse("plans:\n" + rec["plans"])["plans"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError, yaml.YAMLError) as exc:
        raise ConfigError(f"{p} is not the record `testbench check` writes ({type(exc).__name__}: {exc}); {again}") \
            from None
    if not isinstance(plans, dict) or not plans:
        raise ConfigError(f"{p} lists no plans; {again}")
    try:
        cfg = validate({"project": rec.get("project"), "plans": plans}, root)
    except ConfigError as exc:
        raise ConfigError(f"{p}: {exc}; {again}") from None
    away = [f"{name} ({plan['backend']})" for name, plan in cfg["plans"].items() if plan["backend"] != "local"]
    if away:
        raise ConfigError(f"{p}: plan {away[0]} is not on this machine, and `testbench check` runs plans only here; "
                          f"{again}")
    return cfg


def load(root: str | Path) -> dict:
    root = Path(root)
    path = root / CONFIG_NAME
    if not path.is_file() and (root / CHECKED).is_file():
        return checked(root)                     # a folder `testbench check` ran in, with no testbench.yaml
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path} cannot be read ({exc}); it must be a UTF-8 text file") from None
    try:
        raw = _parse(text)
    except ConfigError as exc:
        raise ConfigError(f"{path} {exc}") from None
    except RecursionError:
        raise ConfigError(f"{path} nests too deeply to read; testbench.yaml needs only a few levels") from None
    except (yaml.YAMLError, ValueError) as exc:                  # ValueError: an integer too long to read
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    try:
        return validate({} if raw is None else raw, root)
    except ConfigError as exc:                                   # the field and what to write, and in which file
        raise ConfigError(f"{path}: {exc}") from None


def _parse(text: str):
    """yaml.safe_load, refusing a key given twice in one mapping: YAML keeps only the last, so the first (a plan, a
    budget, a set of checks) would be dropped without a word."""
    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        todo, done = [node], set()
        while todo:
            n = todo.pop()
            if n is None or id(n) in done:
                continue
            done.add(id(n))
            if isinstance(n, yaml.MappingNode):
                seen = {}
                for k, v in n.value:
                    if isinstance(k, yaml.ScalarNode) and k.tag != "tag:yaml.org,2002:merge":
                        if (k.tag, k.value) in seen:
                            raise ConfigError(f"line {k.start_mark.line + 1}: {k.value!r} is given twice in the same "
                                              f"mapping (also on line {seen[k.tag, k.value]}); YAML keeps only the last, "
                                              "so the first would be ignored. Keep one, or merge them")
                        seen[k.tag, k.value] = k.start_mark.line + 1
                    todo += [k, v]
            elif isinstance(n, yaml.SequenceNode):
                todo += n.value
        return None if node is None else loader.construct_document(node)
    finally:
        loader.dispose()


def _finite(v) -> bool:
    """A real number: not true/false, .nan or .inf (which slip past every limit), nor too big for a float."""
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    except OverflowError:
        return False


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def validate(raw: dict, root: Path) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError(f"{CONFIG_NAME} must be a mapping with `project:` and `plans:`")
    from . import backends as B
    defaults = copy.deepcopy(DEFAULTS)
    for b in B.registry().values():                     # each backend brings its own section (modal:, colab:, ...)
        defaults = _merge(defaults, b.config_defaults)
    cfg = _merge(defaults, raw)
    sections = (k for b in B.registry().values() for k in b.config_defaults)     # modal:, ssh:, docker:, ...
    for key in ("budget", "sessions", "pricing", "guard", *sections):
        if cfg.get(key) is None:                         # `budget:` with every line under it commented out
            cfg[key] = copy.deepcopy(defaults[key])
        elif not isinstance(cfg[key], dict):
            raise ConfigError(f"`{key}:` is a mapping of settings (`{key}:`, then indented `name: value` lines); got "
                              f"{cfg[key]!r}")
    if cfg.get("project") is None:                       # left out: the folder's name, as `testbench init` writes it
        cfg["project"] = project_name(root)
    project = cfg["project"]
    if not project or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", str(project)):
        raise ConfigError("`project:` must be a short name of letters, digits, - or _ (it names the Modal app and "
                          "volume); or leave it out to use the folder's name")
    cfg["root"] = str(root)
    if not isinstance(cfg.get("exclude"), list) or not all(isinstance(x, str) for x in cfg["exclude"]):
        raise ConfigError("`exclude:` is a list of file names or patterns left out of copies of the project, e.g. "
                          "[.git, data/*.bin]")
    cfg["exclude"] = list(dict.fromkeys(cfg["exclude"] + NEVER_COPIED))
    for key in ("max_run_usd", "ask_above_usd", "max_day_usd"):
        if not number(cfg["budget"].get(key)):
            raise ConfigError(f"`budget.{key}` must be a number of US dollars, e.g. {DEFAULTS['budget'][key]}")
    _sessions(cfg["sessions"])
    for key, value in cfg["pricing"].items():
        for name, price in value.items() if isinstance(value, dict) else [(None, value)]:
            if not number(price):
                raise ConfigError(f"`pricing.{key}{'' if name is None else '.' + str(name)}` is {price!r}; a price is a "
                                  "number of 0 or more, e.g. modal_gpu_per_s: {T4: 0.000164}")
    if not isinstance(cfg["guard"].get("ask"), list) or not all(isinstance(x, str) for x in cfg["guard"]["ask"]):
        raise ConfigError("`guard.ask` is a list of commands the hook asks about first, e.g. [git push, gh pr merge]")
    cfg["notify"] = _notify(cfg.get("notify"))
    cfg["export"] = check_export(cfg.get("export"))
    data, cfg["data"] = cfg.get("data"), {}
    if data is not None:                                 # data: by role, a sealed holdout, walk-forward windows
        from . import dataseal
        cfg["data"] = dataseal.check_data(data, root, cfg["exclude"])
    plans = cfg.get("plans") or {}
    if not isinstance(plans, dict):
        raise ConfigError(f"`plans:` maps plan names to plans; it is {_brief(plans)}. Under `plans:`, write each plan "
                          f"one of these ways:\n{PLAN_FORMS}")
    cfg["plans"] = {name: _plan(name, plan, root, cfg["data"]) for name, plan in plans.items()}
    if sum(bool(p.get("holdout")) for p in cfg["plans"].values()) > 1:
        from . import dataseal
        dataseal.check_plans(cfg["plans"])
    for name in sorted({p["backend"] for p in cfg["plans"].values()} | {cfg["sessions"]["backend"]}):
        B.get(name).validate(cfg)
    return cfg


def counted(n, one: str, many: str | None = None) -> str:
    """A count with its noun, singular for exactly one: counted(1, "try", "tries") is "1 try", counted(3, "file") is
    "3 files"."""
    return (f"{n:g}" if isinstance(n, float) else str(n)) + " " + (one if n == 1 else many or one + "s")


def number(v, positive: bool = False) -> bool:
    """A real number of dollars or minutes: not a bool, not NaN or infinity, not negative (not zero if positive).
    Every limit, price and duration goes through this, because NaN compares false with everything and would let a
    launch past every limit."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and (v > 0 if positive else v >= 0))


def project_name(folder) -> str:
    """A project name made from a folder's name: letters, digits, - and _ (anything else becomes -), at most 40.
    `testbench init` writes it, and a testbench.yaml without `project:` uses it."""
    name = "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in Path(folder).resolve().name)
    return name[:40].strip("-_") or "project"


def _brief(x) -> str:
    """A value as a message shows it: short, and `empty` for a key with nothing after it."""
    text = "empty" if x is None else repr(x)
    return text if len(text) <= 60 else text[:57] + "..."


def check_hardware(where: str, backend: str, gpu) -> None:
    from . import backends as B
    if not isinstance(backend, str) or backend not in B.names():
        why = B.broken().get(backend) if isinstance(backend, str) else None
        hint = f" ({why})" if why else ", or install the package that provides it"
        raise ConfigError(f"`{where}.backend` is {backend!r}; use one of {', '.join(B.names())}{hint}")
    B.get(backend).check_hardware(where, gpu)


def _sessions(s: dict) -> None:
    check_hardware("sessions", s.get("backend", "local"), s.get("gpu"))
    for key in ("idle_minutes", "max_minutes"):
        if not number(s.get(key), positive=True):
            raise ConfigError(f"`sessions.{key}` must be a positive number of minutes")
    if not isinstance(s.get("secrets") or [], list):
        raise ConfigError("`sessions.secrets` is a list of secret NAMES, e.g. [HF_TOKEN]")


NOTIFY_EVENTS = ("run_end", "loop_rule", "loop_end", "approval_needed", "watch_run", "loop_try")
NOTIFY_DEFAULT = NOTIFY_EVENTS[:4]            # watch_run and loop_try (one message per run) only when asked for
NOTIFY_EXAMPLE = "{webhook_env: TESTBENCH_WEBHOOK, on: [run_end, loop_rule, loop_end, approval_needed]}"


def own_file(root: Path, path: Path) -> Path:
    """path, a file testbench writes in the project (testbench.yaml, AGENTS.md, CLAUDE.md, .testbench/NOTES.md),
    refused when it really is outside it: a link, or in a linked folder. A repository can ship AGENTS.md as a link
    to ../.bashrc, and writing it would change that file instead."""
    real = Path(path).resolve()
    if not real.is_relative_to(Path(root).resolve()):
        raise ConfigError(f"{Path(path).relative_to(root)} is a link to {real}, outside the project, so testbench does "
                          "not write it (that would change the file it leads to). Replace the link with a file of the "
                          "project's own, or remove it, then run this again")
    return Path(path)


NOTIFY_OWN = ("webhook", "webhook_env", "on")     # notify:'s own keys; every other key names a notifier


def _notify(n) -> dict | None:
    """Where notifications go: the webhook (`webhook:` or `webhook_env:`, as always) and any other notifier by its
    name (`desktop: true`, or an installed plug-in's, with its settings); `on:` picks the events for all of them.
    The values are never repeated in a message: a webhook URL's path is its secret. Returns webhook, webhook_env and
    on, plus {name: settings} for each other notifier (notify.chosen reads it)."""
    if n is None:
        return None
    if not isinstance(n, dict):
        raise ConfigError(f"`notify:` is a mapping, e.g. {NOTIFY_EXAMPLE}")
    if True in n:                                  # YAML 1.1 reads a bare `on:` key as true
        n = {("on" if k is True else k): v for k, v in n.items()}
    from . import plugins as P
    names, broken = P.names("notifiers"), P.broken("notifiers")
    for k in n:
        if k not in NOTIFY_OWN and k not in names and k not in broken:
            others = [x for x in names if x != "webhook"]
            raise ConfigError(f"`notify` has no {str(k)!r}; use webhook (or webhook_env) and on, e.g. {NOTIFY_EXAMPLE}"
                              + (f"; another notifier goes by its name: {', '.join(others)}" if others else ""))
    url, env = n.get("webhook"), n.get("webhook_env")
    others = {k: v for k, v in n.items() if k not in NOTIFY_OWN and v is not False}
    if not url and not env and not others:
        raise ConfigError("`notify` needs one of `webhook:` (the URL to POST to) or `webhook_env:` (the name of an "
                          "environment variable that holds it, which keeps the URL's secret out of the file), e.g. "
                          + NOTIFY_EXAMPLE + "; or another notifier by its name, e.g. `desktop: true` (a notification "
                          "on this machine's screen)")
    hook = _settings("notifiers", "webhook", "notify", {"webhook": url, "webhook_env": env}) if url or env else \
        {"webhook": None, "webhook_env": None}
    on = n.get("on", list(NOTIFY_DEFAULT))
    on = [on] if isinstance(on, str) else on
    if not isinstance(on, list) or not on or any(e not in NOTIFY_EVENTS for e in on):
        bad = [e for e in on if e not in NOTIFY_EVENTS] if isinstance(on, list) else []
        raise ConfigError(f"`notify.on` lists the events to send, from {', '.join(NOTIFY_EVENTS)}"
                          + (f"; there is no {bad[0]!r}" if bad else "")
                          + f" (default: {', '.join(NOTIFY_DEFAULT)}), e.g. {NOTIFY_EXAMPLE}")
    if "loop_end" in on:                           # a rule firing was sent as loop_end before loop_rule was its own
        on = [*on, "loop_rule"]
    out = {"webhook": hook["webhook"], "webhook_env": hook["webhook_env"], "on": list(dict.fromkeys(on))}
    for name, opts in others.items():              # installed but not loading: kept, and told in notify.log
        out[name] = _settings("notifiers", name, f"notify.{name}", opts) if name in names else \
            opts if isinstance(opts, dict) else {}
        _no_secrets(f"notify.{name}", out[name])
    return out


EXPORT_KEYS = {"mlflow": ("tracking_uri", "experiment", "artifacts"),     # the built-in sinks' settings (export.py)
               "wandb": ("project", "entity", "mode", "dir", "tags", "artifacts"), "csv": ("path",), "jsonl": ("path",)}


def check_export(e) -> dict:
    """export: where every finished run goes too (docs/export.md). Returns {sink: its settings}, each checked by the
    sink itself (export.Sink.validate); false leaves one out. A plug-in sink that is installed but does not load is
    kept as written, so its exports fail in export.log and it never stops a run or this file from loading."""
    example = "{csv: .testbench/runs.csv, mlflow: {tracking_uri: http://127.0.0.1:5000, experiment: my-project}}"
    if e is None:
        return {}
    if not isinstance(e, dict):
        raise ConfigError(f"`export` maps trackers to their settings, e.g. {example}")
    from . import plugins as P
    names, broken = P.names("sinks"), P.broken("sinks")
    out = {}
    for target, opts in e.items():
        where = f"export.{target}"
        if target not in names and target not in broken:
            raise ConfigError(f"`export` has no {target!r}; use {', '.join(names)}, e.g. {example}")
        if opts is False:
            continue
        out[target] = _settings("sinks", target, where, opts) if target in names else \
            opts if isinstance(opts, dict) else {}
        _no_secrets(where, out[target])
    return out


def _settings(kind: str, name: str, where: str, opts) -> dict:
    """A sink's or notifier's settings as it checks them itself (its validate()). One that cannot check them, rather
    than refusing them with a ConfigError, has a bug: it is named, with the line that leaves it out."""
    from . import plugins as P
    try:
        got = P.get(kind, name).validate(where, opts)
    except ConfigError:
        raise
    except Exception as exc:
        got = exc
    if not isinstance(got, dict):
        what = f"failed ({type(got).__name__}: {got})" if isinstance(got, Exception) else \
            f"gave back {type(got).__name__}, not its settings"
        raise ConfigError(f"`{where}`: the {name} {P.kind(kind).one} could not check its settings - its validate() "
                          f"{what}. That is a bug in the package that provides it; leave it out (`{name}: false`) "
                          "until it is fixed") from None
    return got


def _no_secrets(where: str, opts: dict) -> None:
    """Secrets stay out of testbench.yaml, whatever a plug-in's own check allows: a setting named like a token, key
    or password is refused, unless it ends in _env and holds an environment variable's NAME."""
    for k, v in opts.items():
        if re.search(r"token|key|secret|password|passwd|credential", str(k), re.I) and not (
                str(k).endswith("_env") and isinstance(v, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v)):
            raise ConfigError(f"`{where}.{k}`: secrets never go in testbench.yaml. Put it in an environment variable "
                              "where testbench runs and give that variable's NAME instead (a setting ending in _env, "
                              "if this one takes it); testbench never prints or writes it")


# ---- short forms: written out in full before anything else reads a plan ------------------------------------------

LAUNCHERS = ("npx", "bunx", "pnpx", "exec", "time", "nice", "nohup")      # `npx vitest run` runs vitest
RUNS_WITH = ("uv", "poetry", "pipenv", "pdm", "hatch", "rye")              # `uv run pytest` runs pytest
SETUP_WORDS = ("cd", "pushd", "source", ".", "export", "set")              # `cd app && npm test` runs npm


def step_name(command: str) -> str:
    """The name a step written as a plain command gets: its program's. pytest for `pytest -q`, `python -m pytest`
    and `uv run pytest`; train for `python train.py`; npm for `cd web && npm test`; step when there is none."""
    for part in re.split(r"&&|\|\||[;|&\n]", str(command)):
        try:
            words = shlex.split(part)
        except ValueError:                               # an unclosed quote: the words as they stand
            words = part.split()
        while words:
            prog = re.split(r"[/\\]", words[0])[-1]
            if re.fullmatch(r"[A-Za-z_]\w*=.*", words[0]) or prog in LAUNCHERS or words[0].startswith("-"):
                words = words[1:]                        # FOO=1 pytest, npx --yes vitest
            elif prog in RUNS_WITH and words[1:2] == ["run"]:
                words = words[2:]
            else:
                break
        if not words or words[0] in SETUP_WORDS:
            continue
        prog, rest = re.split(r"[/\\]", words[0])[-1], words[1:]
        if re.fullmatch(r"python[\d.]*|py", prog):       # the module or script it runs
            if "-m" in rest[:-1]:
                prog = rest[rest.index("-m") + 1].split(".")[0]
            else:
                prog = next((re.split(r"[/\\]", w)[-1][:-3] for w in rest if w.endswith(".py")), prog)
        prog = re.sub(r"(?<=.)\.\w+$", "", prog)        # check.sh: check
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", prog).strip("-_")[:40]
        if name:
            return name
    return "step"


def _named(steps: list) -> list:
    """Steps with each plain command written as a run: step named after its program, unique in the plan (pytest,
    pytest-2), never taking a name a step written in full already has."""
    taken = {str(s["name"]) for s in steps if isinstance(s, dict) and s.get("name") not in (None, "")}
    out = []
    for s in steps:
        if isinstance(s, str):
            base = name = step_name(s)
            n = 1
            while name in taken:
                n += 1
                name = f"{base}-{n}"
            taken.add(name)
            s = {"name": name, "run": s}
        out.append(s)
    return out


def long_form(name, plan):
    """A plan as it is written in full. `tests: "pytest -q"` is one step on this machine, named after the plan;
    `checks: ["ruff check .", "pytest -q"]` is those steps in order on this machine; in any plan, a step written as a
    plain command is a run: step named after its program. Anything else comes back as it is, for validation to say
    what is wrong with it. Never raises: the guard's and the recipes' readers use it on files that may not load."""
    if isinstance(plan, str):
        return {"backend": "local", "steps": [{"name": str(name), "run": plan}]}
    if isinstance(plan, list):
        return {"backend": "local", "steps": _named(plan)}
    if isinstance(plan, dict) and isinstance(plan.get("steps"), list):
        return {**plan, "steps": _named(plan["steps"])}
    return plan


def _plan(name: str, plan: dict, root: Path, data: dict | None = None) -> dict:
    where = f"plans.{name}"
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", name):
        raise ConfigError(f"`{where}`: plan names use letters, digits, - or _" + ("" if isinstance(name, str) else
                          f" (quote it, \"{name}\":, so YAML does not read it as a number, a date or true/false)"))
    written = plan.get("steps") if isinstance(plan, dict) else plan
    at = where if isinstance(plan, list) else f"{where}.steps"          # where its steps are, as written
    if isinstance(plan, str) and not plan.strip() or isinstance(plan, list) and not plan:
        raise ConfigError(f"`{where}` is {_brief(plan)}, which runs nothing; a plan is written one of these ways:\n"
                          + PLAN_FORMS)
    for i, step in enumerate(written if isinstance(written, list) else []):
        if isinstance(step, str) and not step.strip():
            raise ConfigError(f"`{at}[{i}]` is an empty command; a step is written one of these ways:\n{STEP_FORMS}")
    plan = long_form(name, plan)
    if not isinstance(plan, dict):
        raise ConfigError(f"`{where}` is {_brief(plan)}; a plan is written one of these ways:\n{PLAN_FORMS}")
    plan = {**PLAN_DEFAULTS, **plan}
    check_hardware(where, plan["backend"], plan.get("gpu"))
    if data or any(k in plan for k in PLAN_DATA_KEYS):
        from . import dataseal                           # holdout:, walk_forward:, lookahead:, model_cutoff:, a copy
        dataseal.check_plan(where, plan, data or {})
    from . import backends as B
    plan["workdir"] = plan["workdir"] or ("copy" if B.get(plan["backend"]).remote else "inplace")
    if plan["workdir"] not in ("inplace", "copy"):
        raise ConfigError(f"`{where}.workdir` must be inplace (run in the project folder) or copy (a scratch copy)")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigError(f"`{where}.steps` must be a list of steps"
                          + (f", not {_brief(steps)}" if "steps" in plan else "")
                          + f", each written one of these ways:\n{STEP_FORMS}")
    seen = set()
    plan["steps"] = []
    for i, step in enumerate(steps):
        s = _step(f"{at}[{i}]", step, root)
        if s["name"] in seen:
            raise ConfigError(f"`{where}`: two steps are named {s['name']!r}; names must be unique within a plan")
        seen.add(s["name"])
        plan["steps"].append(s)
    _plain_data(where, plan, ())
    return plan


def _plain_data(where: str, x, inside: tuple) -> None:
    """A run carries its plan as JSON: a YAML date (START: 2024-01-01), set or binary would stop it at launch."""
    if isinstance(x, (dict, list)):
        if id(x) in inside:
            raise ConfigError(f"`{where}` contains itself (a YAML alias inside its own anchor); write the value out")
        for k, v in x.items() if isinstance(x, dict) else enumerate(x):
            _plain_data(f"{where}.{k}" if isinstance(x, dict) else f"{where}[{k}]", v, inside + (id(x),))
    elif not isinstance(x, (str, int, float, bool, type(None))):
        raise ConfigError(f"`{where}` is {x!r}, which a plan cannot carry; write it in quotes, e.g. \"{x}\"")


def _step(where: str, step: dict, root: Path) -> dict:
    if not isinstance(step, dict):
        raise ConfigError(f"`{where}` is {_brief(step)}; a step is written one of these ways:\n{STEP_FORMS}")
    if not step.get("name"):
        raise ConfigError(f"`{where}` needs a `name:` (only a step written as a plain command is named for you); a "
                          f"step is written one of these ways:\n{STEP_FORMS}")
    if not isinstance(step["name"], (str, int)) or isinstance(step["name"], bool):
        raise ConfigError(f"`{where}.name` is a short text naming the step, e.g. train")
    step = {**step, "name": str(step["name"])}
    kinds = [k for k in STEP_KINDS if k in step]
    if len(kinds) != 1:
        raise ConfigError(f"`{where}` ({step['name']}) needs exactly one of `notebook:`, `run:` or `web:`; a step "
                          f"is written one of these ways:\n{STEP_FORMS}")
    known = ["name", *STEP_KINDS, *STEP_DEFAULTS]
    for key in step:
        if key not in known:                             # a misspelt `expect:` would leave the step with no checks
            near = difflib.get_close_matches(str(key), known, n=1)
            raise ConfigError(f"`{where}` ({step['name']}) has no {key!r}"
                              + (f" (did you mean {near[0]!r}?)" if near else "") + f"; a step takes {', '.join(known)}")
    if kinds[0] != "notebook":                           # set on another step, these would be ignored without a word
        for key, what in NOTEBOOK_ONLY.items():
            if step.get(key, STEP_DEFAULTS[key]) not in (STEP_DEFAULTS[key], None):
                raise ConfigError(f"`{where}` ({step['name']}) is a {kinds[0]}: step, and `{key}:` applies to notebook: "
                                  f"steps only ({what}), so it would be ignored. Remove it"
                                  + (", and give the command its values on its command line or in `env:`, e.g. "
                                     "env: {EPOCHS: \"2\"}" if key == "params" else ""))
    s = {**copy.deepcopy(STEP_DEFAULTS), **step, "kind": kinds[0]}
    if not number(s["max_minutes"], positive=True):
        raise ConfigError(f"`{where}.max_minutes` must be a positive number; the step is stopped when it runs out")
    for key in ("params", "env", "expect"):
        if not isinstance(s[key], dict):
            raise ConfigError(f"`{where}.{key}` must be a mapping")
    _expect(f"{where}.expect", s["expect"])
    s["mocks"] = _mocks(f"{where}.mocks", s["mocks"], root)
    for key in ("secrets", "pythonpath", "artifacts", "uploads"):
        paths = key in ("pythonpath", "artifacts")
        if not isinstance(s[key], list) or paths and not all(isinstance(x, str) for x in s[key]):
            raise ConfigError(f"`{where}.{key}` must be a list" + (" of paths" if paths else ""))
    if not all(isinstance(k, str) for k in s["env"]):
        raise ConfigError(f"`{where}.env` maps variable NAMES to values, e.g. {{SEED: \"1\"}}")
    for key in ("colab_shim", "continue_on_failure"):
        if not isinstance(s[key], bool):
            raise ConfigError(f"`{where}.{key}` is true or false")
    if s["kind"] == "notebook":
        if not isinstance(s["notebook"], str):
            raise ConfigError(f"`{where}.notebook` is the notebook's path, e.g. notebooks/train.ipynb")
        nb = root / s["notebook"]
        if not nb.is_file():
            raise ConfigError(f"`{where}.notebook`: {s['notebook']} does not exist (paths are relative to {root})")
        if nb.suffix.lower() == ".py":
            from .notebook import plain_python
            if plain_python(nb):
                raise ConfigError(f"`{where}.notebook`: {s['notebook']} is a plain Python file, not a notebook (no "
                                  f"`# %%` cell markers). Use a run: step instead (run: python {s['notebook']}), or "
                                  "mark its cells with `# %%` to run it as a notebook")
        if s["cells"] is not None:
            s["cells"] = parse_cells(s["cells"], where)
        for group in s["uploads"]:
            if not isinstance(group, list):
                raise ConfigError(f"`{where}.uploads` is a list of uploads, each a list of files: [[data.csv], [a.mp4, b.mp4]]")
    if s["kind"] == "web":
        w = s["web"]
        if not isinstance(w, dict) or not w.get("url"):
            raise ConfigError(f"`{where}.web` needs at least `url:` (and usually `serve:`, the command that starts the app)")
        s["web"] = {**copy.deepcopy(WEB_DEFAULTS), **w}
        s["web"]["actions"] = [] if s["web"]["actions"] is None else s["web"]["actions"]
        vp, wait = s["web"]["viewport"], s["web"]["ready_timeout"]
        if not isinstance(w["url"], str) or not isinstance(s["web"]["actions"], list) or not _finite(wait) or wait <= 0 \
                or not (isinstance(vp, list) and len(vp) == 2 and all(_finite(x) and x > 0 for x in vp)):
            raise ConfigError(f"`{where}.web` is {{url: \"http://...\", actions: [...], viewport: [1280, 800], "
                              "ready_timeout: 30}: a url, a list of actions, a width and height, and seconds to wait")
    if s["kind"] == "run" and not isinstance(s["run"], str):
        raise ConfigError(f"`{where}.run` is a shell command string")
    for name in s["secrets"]:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name)):
            raise ConfigError(f"`{where}.secrets` lists secret NAMES only, never values")
    return s


def _mocks(where: str, value, root: Path) -> dict:
    """mocks: [openai, anthropic], or with options: {anthropic: {replies: tests/replies.yaml, fail_on_call: 3}}. Each
    mock, built in or a plug-in's, checks its own options (mocks.Mock.validate)."""
    example = "[openai, anthropic], or with options {anthropic: {replies: tests/replies.yaml, fail_on_call: 3}}"
    if value in (None, [], {}):
        return {}
    from . import mocks as M                                 # a layer: only for steps that use mocks
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f"`{where}` lists the mock clients by name, e.g. {example}")
        value = {v: {} for v in value}
    if not isinstance(value, dict):
        raise ConfigError(f"`{where}` lists the mock clients, e.g. {example}")
    return {name: M.find(name, where).validate(f"{where}.{name}", opts, root) for name, opts in value.items()}


def _expect(where: str, e: dict) -> None:
    """Malformed expectations fail at load time, before anything runs (docs/expectations.md has every one). Each kind
    of check validates its own value (expect.Check.validate), the built-ins and installed plug-ins alike."""
    from . import expect as X
    X.validate(where, e)


def parse_cells(spec, where="cells") -> list[int]:
    """'1-3,5' or [1, 2] -> [1, 2, 3, 5]: code cells counted from 1, in notebook order."""
    if isinstance(spec, int):
        return [spec]
    if isinstance(spec, list):
        out = spec
    else:
        out = []
        for part in str(spec).split(","):
            part = part.strip()
            if re.fullmatch(r"\d+-\d+", part):
                a, b = map(int, part.split("-"))
                out += list(range(a, b + 1))
            elif part.isdigit():
                out.append(int(part))
            elif part:
                raise ConfigError(f"`{where}.cells`: {part!r} is not a cell number or range like 2-5")
    if not out or any(not isinstance(c, int) or c < 1 for c in out):
        raise ConfigError(f"`{where}.cells` counts code cells from 1, e.g. '1-3,5'")
    return sorted(set(out))


def plan_minutes(plan: dict) -> float:
    return float(sum(float(s["max_minutes"]) for s in plan["steps"]))     # huge whole numbers add up to inf, not a crash
