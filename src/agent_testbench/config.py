"""testbench.yaml: what a project lets its testbench run, where, and for how much.

Every error names the field and says what to write instead, because the reader is often an agent that
will fix the file from the message alone.
"""
from __future__ import annotations

import copy
import difflib
import math
import re
from pathlib import Path

import yaml

CONFIG_NAME = "testbench.yaml"
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
STEP_DEFAULTS = {"max_minutes": 10, "params": {}, "env": {}, "secrets": [], "pythonpath": [], "artifacts": [],
                 "expect": {}, "uploads": [], "cells": None, "colab_shim": True, "continue_on_failure": False,
                 "mocks": {}}


class ConfigError(ValueError):
    pass


def find_root(start: str | Path = ".") -> Path:
    """The nearest directory at or above `start` that holds testbench.yaml."""
    here = Path(start).resolve()
    for d in (here, *here.parents):
        if (d / CONFIG_NAME).is_file():
            return d
    raise ConfigError(f"No {CONFIG_NAME} here or in any parent of {here}. Run `testbench init` in the project folder.")


def load(root: str | Path) -> dict:
    root = Path(root)
    path = root / CONFIG_NAME
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
    return validate({} if raw is None else raw, root)


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
    project = cfg.get("project")
    if not project or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", str(project)):
        raise ConfigError("`project:` must be a short name of letters, digits, - or _ (it names the Modal app and volume)")
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
    plans = cfg.get("plans") or {}
    if not isinstance(plans, dict):
        raise ConfigError("`plans:` must map plan names to {backend, steps}; see `testbench init` for an example")
    cfg["plans"] = {name: _plan(name, plan, root) for name, plan in plans.items()}
    for name in sorted({p["backend"] for p in cfg["plans"].values()} | {cfg["sessions"]["backend"]}):
        B.get(name).validate(cfg)
    return cfg


def number(v, positive: bool = False) -> bool:
    """A real number of dollars or minutes: not a bool, not NaN or infinity, not negative (not zero if positive).
    Every limit, price and duration goes through this, because NaN compares false with everything and would let a
    launch past every limit."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            and (v > 0 if positive else v >= 0))


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


NOTIFY_EVENTS = ("run_end", "loop_end", "approval_needed", "watch_run")
NOTIFY_DEFAULT = NOTIFY_EVENTS[:3]            # watch_run (a watched run whose result changed) only when asked for
NOTIFY_EXAMPLE = "{webhook_env: TESTBENCH_WEBHOOK, on: [run_end, loop_end, approval_needed]}"


def _notify(n) -> dict | None:
    """Where notifications go. The values are never repeated in a message: a webhook URL's path is its secret."""
    if n is None:
        return None
    if not isinstance(n, dict):
        raise ConfigError(f"`notify:` is a mapping, e.g. {NOTIFY_EXAMPLE}")
    if True in n:                                  # YAML 1.1 reads a bare `on:` key as true
        n = {("on" if k is True else k): v for k, v in n.items()}
    for k in n:
        if k not in ("webhook", "webhook_env", "on"):
            raise ConfigError(f"`notify` has no {str(k)!r}; use webhook (or webhook_env) and on, e.g. {NOTIFY_EXAMPLE}")
    url, env = n.get("webhook"), n.get("webhook_env")
    if bool(url) == bool(env):
        raise ConfigError("`notify` needs one of `webhook:` (the URL to POST to) or `webhook_env:` (the name of an "
                          "environment variable that holds it, which keeps the URL's secret out of the file), e.g. "
                          + NOTIFY_EXAMPLE)
    if url and (not isinstance(url, str) or not re.match(r"https?://[^/\s]+", url.strip())):
        raise ConfigError("`notify.webhook` is a URL that takes a JSON POST, starting with https:// (e.g. a Slack "
                          "incoming webhook); or put the URL in an environment variable and name it with "
                          "`notify.webhook_env: NAME`")
    if env and (not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env)):
        raise ConfigError("`notify.webhook_env` is the NAME of an environment variable that holds the webhook URL, "
                          "e.g. TESTBENCH_WEBHOOK - not the URL itself (export TESTBENCH_WEBHOOK=https://... where "
                          "testbench runs)")
    on = n.get("on", list(NOTIFY_DEFAULT))
    on = [on] if isinstance(on, str) else on
    if not isinstance(on, list) or not on or any(e not in NOTIFY_EVENTS for e in on):
        bad = [e for e in on if e not in NOTIFY_EVENTS] if isinstance(on, list) else []
        raise ConfigError(f"`notify.on` lists the events to send, from {', '.join(NOTIFY_EVENTS)}"
                          + (f"; there is no {bad[0]!r}" if bad else "")
                          + f" (default: {', '.join(NOTIFY_DEFAULT)}), e.g. {NOTIFY_EXAMPLE}")
    return {"webhook": url.strip() if url else None, "webhook_env": env or None, "on": list(dict.fromkeys(on))}


EXPORT_KEYS = {"mlflow": ("tracking_uri", "experiment", "artifacts"),
               "wandb": ("project", "entity", "mode", "dir", "tags", "artifacts"), "csv": ("path",), "jsonl": ("path",)}
EXPORT_SECRETS = {"mlflow": "MLFLOW_TRACKING_TOKEN (or MLFLOW_TRACKING_USERNAME and MLFLOW_TRACKING_PASSWORD)",
                  "wandb": "WANDB_API_KEY"}
EXPORT_EXAMPLES = {"tracking_uri": "http://127.0.0.1:5000 or sqlite:///.testbench/mlflow.db", "experiment": "my-project",
                   "project": "my-project", "entity": "my-team", "mode": "offline", "dir": ".testbench"}


def check_export(e) -> dict:
    """export: where every finished run goes too (docs/export.md). Returns {target: options}; false leaves one out."""
    example = "{csv: .testbench/runs.csv, mlflow: {tracking_uri: http://127.0.0.1:5000, experiment: my-project}}"
    if e is None:
        return {}
    if not isinstance(e, dict):
        raise ConfigError(f"`export` maps trackers to their settings, e.g. {example}")
    out = {}
    for target, opts in e.items():
        where = f"export.{target}"
        if target not in EXPORT_KEYS:
            raise ConfigError(f"`export` has no {target!r}; use {', '.join(EXPORT_KEYS)}, e.g. {example}")
        if opts is False:
            continue
        opts = {"path": opts} if isinstance(opts, str) and target in ("csv", "jsonl") else \
            {} if opts in (None, True) else opts
        if not isinstance(opts, dict):
            raise ConfigError(f"`{where}` is a mapping of settings ({', '.join(EXPORT_KEYS[target])})"
                              + (f", or the file's path, e.g. `{target}: .testbench/runs.{target}`" if target in ("csv", "jsonl") else ""))
        for k, v in opts.items():
            if re.search(r"token|key|secret|password|passwd|credential", str(k), re.I):
                raise ConfigError(f"`{where}.{k}`: secrets never go in testbench.yaml. Set "
                                  f"{EXPORT_SECRETS.get(target, 'it')} in the environment, where the tracker reads it; "
                                  "testbench never prints or writes it")
            if k not in EXPORT_KEYS[target]:
                raise ConfigError(f"`{where}` has no {k!r}; use {', '.join(EXPORT_KEYS[target])}")
            if k == "artifacts" and not isinstance(v, bool):
                raise ConfigError(f"`{where}.artifacts` is true (upload the report and the evidence files, the "
                                  "default) or false")
            if k == "tags" and (not isinstance(v, list) or not all(isinstance(t, str) for t in v)):
                raise ConfigError(f"`{where}.tags` is a list of words, e.g. [baseline, nightly]")
            if k not in ("artifacts", "tags") and (not isinstance(v, str) or not v.strip()):
                raise ConfigError(f"`{where}.{k}` is a string, e.g. {EXPORT_EXAMPLES.get(k, f'.testbench/runs.{target}')}")
        if re.match(r"\w[\w+.-]*://[^/@\s]+@", str(opts.get("tracking_uri", ""))):
            raise ConfigError(f"`{where}.tracking_uri` holds credentials; write the URL without them and set "
                              f"{EXPORT_SECRETS['mlflow']} in the environment")
        if opts.get("mode") not in (None, "online", "offline", "disabled"):
            raise ConfigError(f"`{where}.mode` is online (to the W&B server), offline (kept on this machine for "
                              "`wandb sync`) or disabled")
        if target == "wandb" and set(str(opts.get("project", ""))) & set("/\\#?%:"):
            raise ConfigError(f"`{where}.project` is a W&B project name, without / \\ # ? % or :")
        out[target] = opts
    return out


def _plan(name: str, plan: dict, root: Path) -> dict:
    where = f"plans.{name}"
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}", name):
        raise ConfigError(f"`{where}`: plan names use letters, digits, - or _" + ("" if isinstance(name, str) else
                          f" (quote it, \"{name}\":, so YAML does not read it as a number, a date or true/false)"))
    if not isinstance(plan, dict):
        raise ConfigError(f"`{where}` must be a mapping with `backend:` and `steps:`")
    plan = {"backend": "local", "gpu": None, "workdir": None, "description": "", **plan}
    check_hardware(where, plan["backend"], plan.get("gpu"))
    from . import backends as B
    plan["workdir"] = plan["workdir"] or ("copy" if B.get(plan["backend"]).remote else "inplace")
    if plan["workdir"] not in ("inplace", "copy"):
        raise ConfigError(f"`{where}.workdir` must be inplace (run in the project folder) or copy (a scratch copy)")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ConfigError(f"`{where}.steps` must be a list of steps")
    seen = set()
    plan["steps"] = []
    for i, step in enumerate(steps):
        s = _step(f"{where}.steps[{i}]", step, root)
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
    if not isinstance(step, dict) or not step.get("name"):
        raise ConfigError(f"`{where}` needs a `name:`")
    if not isinstance(step["name"], (str, int)) or isinstance(step["name"], bool):
        raise ConfigError(f"`{where}.name` is a short text naming the step, e.g. train")
    step = {**step, "name": str(step["name"])}
    kinds = [k for k in STEP_KINDS if k in step]
    if len(kinds) != 1:
        raise ConfigError(f"`{where}` ({step['name']}) needs exactly one of `notebook:`, `run:` or `web:`")
    known = ["name", *STEP_KINDS, *STEP_DEFAULTS]
    for key in step:
        if key not in known:                             # a misspelt `expect:` would leave the step with no checks
            near = difflib.get_close_matches(str(key), known, n=1)
            raise ConfigError(f"`{where}` ({step['name']}) has no {key!r}"
                              + (f" (did you mean {near[0]!r}?)" if near else "") + f"; a step takes {', '.join(known)}")
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
        s["web"] = {"serve": None, "ready_timeout": 30, "actions": [], "screenshot": True, "viewport": [1280, 800], **w}
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
    """mocks: [openai, anthropic], or with options: {anthropic: {replies: tests/replies.yaml, fail_on_call: 3}}."""
    from . import mocks as M
    example = "[openai, anthropic], or with options {anthropic: {replies: tests/replies.yaml, fail_on_call: 3}}"
    if value in (None, [], {}):
        return {}
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list):
        if not all(isinstance(v, str) for v in value):
            raise ConfigError(f"`{where}` lists the mock clients by name, e.g. {example}")
        value = {v: {} for v in value}
    if not isinstance(value, dict):
        raise ConfigError(f"`{where}` lists the mock clients, e.g. {example}")
    core, out = M.client_core(), {}
    for name, opts in value.items():
        if name not in M.SDKS:
            raise ConfigError(f"`{where}` names {name!r}; testbench has mock clients for {', '.join(M.SDKS)}. For "
                              "another API, write a strict mock with agent_testbench.mocks.StrictAPI and list its "
                              "folder under pythonpath:")
        opts = opts or {}
        bad = core.options_error(opts)
        if bad:
            raise ConfigError(f"`{where}.{name}`: {bad}")
        if "replies" in opts:
            f = root / opts["replies"]
            if not f.is_file():
                raise ConfigError(f"`{where}.{name}.replies`: {opts['replies']} does not exist (paths are relative to "
                                  f"{root})")
            try:
                core.parse_replies(yaml.safe_load(f.read_text(encoding="utf-8")))
            except (yaml.YAMLError, ValueError) as exc:
                raise ConfigError(f"`{where}.{name}.replies` ({opts['replies']}): {exc}") from None
        out[name] = opts
    return out


def _rule(where: str, rule) -> None:
    from .expect import rule_error
    bad = rule_error(rule)
    if bad:
        raise ConfigError(f"`{where}`: {bad}")


def _table(where: str, value, example: str, keys=None, some=False) -> dict:
    """A mapping; with keys, each of its values is a rule and only those keys are allowed; with some, not empty."""
    if not isinstance(value, dict):
        raise ConfigError(f"`{where}` must be a mapping, e.g. {example}")
    if some and not value:                               # {metrics.json: {}} would check nothing, even a missing file
        raise ConfigError(f"`{where}` is empty, so nothing would be checked; e.g. {example}")
    for k, rule in value.items() if keys else ():
        if k not in keys:
            raise ConfigError(f"`{where}` has no {k!r}; use {', '.join(keys)}, e.g. {example}")
        _rule(f"{where}.{k}", rule)
    return value


def _expect(where: str, e: dict) -> None:
    """Malformed expectations fail at load time, before anything runs (docs/expectations.md has every one)."""
    from . import expect as X
    for key in e:
        if key not in X.KINDS:
            raise ConfigError(f"`{where}` has no {key!r} check; expectations are {', '.join(X.KINDS)}")
    for kind in ("json", "text", "media", "metrics", "image"):          # keyed by a file (or stdout)
        for k in e[kind] if isinstance(e.get(kind), dict) else ():
            if not isinstance(k, str) or not k.strip():
                raise ConfigError(f"`{where}.{kind}` has {k!r} where a file's path" + (" (or stdout)" if kind in
                                  ("text", "metrics") else "") + " goes; write the path in quotes")
    if "exit_code" in e and (not isinstance(e["exit_code"], int) or isinstance(e["exit_code"], bool)):
        raise ConfigError(f"`{where}.exit_code` is the code the command must exit with, a whole number such as 0")
    if "no_errors" in e and not isinstance(e["no_errors"], bool):
        raise ConfigError(f"`{where}.no_errors` is true or false")
    if not isinstance(e.get("files", []), list) or not all(isinstance(f, str) and f.strip() for f in e.get("files", [])):
        raise ConfigError(f"`{where}.files` is a list of paths or globs, e.g. [outputs/model.pt, \"plots/*.png\"]")
    for path, rules in _table(f"{where}.json", e.get("json") or {}, '{metrics.json: {accuracy: ">= 0.9"}}').items():
        for key, rule in _table(f"{where}.json.{path}", rules, '{accuracy: ">= 0.9", "losses.-1": "< 0.5"}', some=True).items():
            _rule(f"{where}.json.{path}.{key}", rule)
    for source, needles in _table(f"{where}.text", e.get("text") or {}, '{stdout: ["epoch 10"]}').items():
        needles = [needles] if isinstance(needles, str) else needles
        if not isinstance(needles, list) or not needles or not all(isinstance(n, str) and n for n in needles):
            raise ConfigError(f"`{where}.text.{source}` is the text to find, or a list of texts, e.g. [\"epoch 10\"] "
                              "(an empty text is found in anything)")
    for path, rules in _table(f"{where}.media", e.get("media") or {}, '{out/video.mp4: {frames: ">= 120"}}').items():
        _table(f"{where}.media.{path}", rules, '{frames: ">= 120", width: 1920, audio: true}', X.MEDIA_KEYS, some=True)
    web = _table(f"{where}.web", e.get("web") or {}, '{status: 200, text: ["Count: 2"]}')
    for k in web:
        if k not in X.WEB_KEYS:
            raise ConfigError(f"`{where}.web` has no {k!r}; use {', '.join(X.WEB_KEYS)}")
    _rule(f"{where}.web.status", web.get("status"))
    if not isinstance(web.get("text", []), list) or not all(isinstance(t, str) and t for t in web.get("text", [])):
        raise ConfigError(f"`{where}.web.text` is a list of texts the page must show, e.g. [\"Count: 2\"]")
    if "title" in web and not (isinstance(web["title"], str) and web["title"]):
        raise ConfigError(f"`{where}.web.title` is text the page's title contains, e.g. Counter")
    if not isinstance(web.get("no_console_errors", True), bool):
        raise ConfigError(f"`{where}.web.no_console_errors` is true or false")
    junit, example = e.get("junit"), '{path: report.xml, failures: 0, tests: ">= 10"}'
    if junit is not None and not isinstance(junit, (str, dict, list)):
        raise ConfigError(f"`{where}.junit` is the report's path or {example}")
    for i, spec in enumerate(X.junit_specs(junit) or ([None] if isinstance(junit, (str, dict)) else [])):
        at = f"{where}.junit" + (f"[{i}]" if isinstance(junit, list) else "")
        if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"].strip():
            raise ConfigError(f"`{at}` needs `path:`, the JUnit XML file the test command writes, e.g. {example} "
                              "(pytest: --junitxml=report.xml)")
        _table(at, {k: v for k, v in spec.items() if k != "path"}, example, X.JUNIT_KEYS)
    example = '{"val_loss=([0-9.eE+-]+)": "< 0.3"}'
    for source, patterns in _table(f"{where}.metrics", e.get("metrics") or {}, f"{{stdout: {example}}}").items():
        for pattern, rule in _table(f"{where}.metrics.{source}", patterns, example, some=True).items():
            if not isinstance(pattern, str):
                raise ConfigError(f"`{where}.metrics.{source}` has {pattern!r} where a regular expression goes; write it "
                                  f"in quotes, e.g. {example}")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"`{where}.metrics.{source}`: {pattern!r} is not a regular expression ({exc}); its "
                                  "first group is the number, e.g. \"val_loss=([0-9.eE+-]+)\"") from None
            _rule(f"{where}.metrics.{source}.{pattern}", rule)
    _table(f"{where}.gpu", e.get("gpu") or {}, '{busy_pct: ">= 50", max_mem_mib: "<= 15000"}', X.GPU_KEYS)
    example = "{like: golden/plot.png, max_diff: 0.02}"
    for path, spec in _table(f"{where}.image", e.get("image") or {}, f"{{outputs/plot.png: {example}}}").items():
        at = f"{where}.image.{path}"
        if not isinstance(spec, dict) or not isinstance(spec.get("like"), str) or set(spec) - {"like", "max_diff"}:
            raise ConfigError(f"`{at}` is {example}: the known-good image and the largest mean pixel difference (0..1)")
        most = spec.get("max_diff", 0.02)
        if not isinstance(most, (int, float)) or isinstance(most, bool) or not 0 <= most <= 1:
            raise ConfigError(f"`{at}.max_diff` is a number from 0 (identical) to 1, e.g. 0.02")
    _table(f"{where}.duration", e.get("duration") or {}, '{minutes: "<= 5"}', X.DURATION_KEYS)
    example = '{calls: "<= 5", output_tokens: "<= 2000"}'
    for sdk, rules in _table(f"{where}.mock_calls", e.get("mock_calls") or {}, f"{{anthropic: {example}}}").items():
        if sdk not in X.MOCK_SDKS:
            raise ConfigError(f"`{where}.mock_calls` names {sdk!r}; the mock clients are {', '.join(X.MOCK_SDKS)}")
        _table(f"{where}.mock_calls.{sdk}", rules, example, X.MOCK_KEYS)


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
