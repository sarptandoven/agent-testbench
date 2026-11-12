"""Expectations: what a step must have produced, checked after it runs.

Each check returns {"check", "ok", "detail"} where detail states what was found, so a failure reads as a
finding ("accuracy 0.71, wanted >= 0.9") rather than a bare FAIL. A check that measured a number puts it first
("0.71 (wanted >= 0.9)"): that is how reports, history and loops read the number back.

    expect:
      exit_code: 0
      no_errors: true                       # notebooks: every cell ran without an error (on by default)
      files: [outputs/model.pt, "plots/*.png"]
      json:
        outputs/metrics.json: {accuracy: ">= 0.9", "losses.-1": "< 0.5"}
      text:
        stdout: ["epoch 10"]                 # or a file path
      media:
        out/video.mp4: {frames: ">= 120", width: 1920, audio: true}
      web:                                   # web steps
        status: 200
        text: ["Count: 2"]
        no_console_errors: true
      junit: {path: report.xml, tests: ">= 10"}       # failures: 0 and errors: 0 unless said otherwise
      metrics:                               # numbers printed: the regex's first group, in its last match
        stdout: {"val_loss=([0-9.eE+-]+)": "< 0.3"}
      numbers:                               # numbers a metric reader reads from files (metrics.py); or a list
        reader: trading
        files: {equity: out/equity.csv}
        rules: {sharpe: ">= 1", max_drawdown: ">= -0.2"}
      strategy:                              # the trading pack's check, with its guardrails (trading/checks.py)
        equity: out/equity.csv
        sharpe: ">= 1"
        beats: buy_and_hold
      gpu: {busy_pct: ">= 50", max_mem_mib: "<= 15000"}
      image:
        outputs/plot.png: {like: golden/plot.png, max_diff: 0.02}
      duration: {minutes: "<= 5"}
      mock_calls:                            # calls the step made to the mock clients (mocks: on the step)
        anthropic: {calls: "<= 5", output_tokens: "<= 2000"}       # refused: 0 unless said otherwise
        alpaca: {orders: "<= 20", orders_without_limit: 0}         # the brokers: orders, fills, fees, live, ...

Any rule can also be a tolerance ("~= 0.93 +- 0.01", "~= 100 +- 5%") or an inclusive range ("between 0.8 and 0.95").

Every kind of check is a plug-in of kind "checks" (agent_testbench.plugins), the fifteen above included, so a package
can add one that works exactly like a built-in (docs/plugins.md#checks). `strategy:` lives in the trading pack, a layer:
the registry names it ("agent_testbench.trading.checks:StrategyCheck") and imports it when checks are first used.

    # pyproject.toml of a package
    [project.entry-points."agent_testbench.checks"]
    max_log_lines = "testbench_check_example:MaxLogLines"

A check is an object (or a class that takes no arguments; subclassing Check is optional) with:

    name        the key under `expect:`, e.g. max_log_lines; a plain YAML key (the entry point's name if it has none)
    validate(where: str, value) -> None
                refuse a malformed value when testbench.yaml loads, before anything runs: raise config.ConfigError
                with a message that starts with `where` (the field, e.g. `plans.train.steps[0].expect.max_log_lines`)
                and says what to write instead. validate_table() and validate_rule() below do the common shapes
    check(value, *, workdir: Path, result: dict, add) -> None
                after the step ran: add(name, ok, detail) once per finding, where detail says what was found, the
                measured number first ("0.71 (wanted >= 0.9)"; compare() writes that). workdir is the folder the step
                ran in: read from it, never write. result is the step's outcome: kind (run, notebook or web),
                exit_code, stdout (everything it printed), seconds, run_started (a file older than this is an old
                result), step_dir, command, error, web, gpu, mocks. A missing or unreadable file is a failing
                finding, never an exception
    example     a value validate accepts: the contract (`testbench plugin test`) tries check() with it
    keys        optional: the names its value takes, for messages and the docs, e.g. ("minutes", "seconds")
    schema      optional: the JSON Schema of its value; `testbench schema` adds it under $defs.expect (it may use
                testbench.schema.json's own $defs, e.g. {"$ref": "#/$defs/rule"})
    default     optional: the value check() gets for a step that does not name it, so it applies to every step;
                without one it runs only where named (exit_code, no_errors, web and mock_calls have one)

The built-ins' findings come first (RUN_ORDER), then the plug-ins' in the order the step names them. A plug-in check
that raises fails its step with a finding that says so; it never stops the run or another step. One that cannot run
where the step is checked (not installed there, or broken) fails the step too: a check is never skipped in silence.
"""
from __future__ import annotations

import copy
import glob
import importlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from . import plugins
from .config import ConfigError, counted, near

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, "!=": lambda a, b: a != b, "==": lambda a, b: a == b,
       ">": lambda a, b: a > b, "<": lambda a, b: a < b}
RULE = re.compile(r"^\s*(>=|<=|!=|==|>|<)\s*(.+?)\s*$")
NEAR = re.compile(r"^\s*~=\s*(\S+?)\s*(?:\+-|\+/-|±)\s*(\S+?)\s*(%?)\s*$")
BETWEEN = re.compile(r"^\s*between\s+(\S+)\s+and\s+(\S+)\s*$", re.I)
KINDS = ("exit_code", "no_errors", "files", "json", "text", "media", "web", "junit", "metrics", "numbers", "strategy",
         "gpu", "image", "duration", "mock_calls")       # the built-ins, in the order testbench.yaml is validated
RUN_ORDER = ("exit_code", "no_errors", "files", "json", "text", "media", "web", "junit", "metrics", "numbers",
             "strategy", "image", "gpu", "duration", "mock_calls")    # ... and the order their findings come in
STRATEGY = "agent_testbench.trading.checks:StrategyCheck"           # a layer's built-in: imported by name, when used
NUMBERS_KEYS = ("reader", "files", "options", "rules")
NUMBERS_EXAMPLE = '{reader: trading, files: out/equity.csv, rules: {sharpe: ">= 1"}}'
LLM_SDKS = ("anthropic", "openai")
BROKER_SDKS = ("alpaca", "ccxt", "ib_async")
MOCK_SDKS = LLM_SDKS + BROKER_SDKS
MOCK_KEYS = ("calls", "refused", "injected", "input_tokens", "output_tokens", "tool_calls")
BROKER_KEYS = ("calls", "refused", "orders", "fills", "cancels", "orders_without_limit", "max_order_value", "fees",
               "live")
MOCK_LOG = "mock_calls.jsonl"
MEDIA_KEYS = ("frames", "width", "height", "fps", "seconds", "audio")
WEB_KEYS = ("status", "title", "text", "no_console_errors")
JUNIT_KEYS = ("tests", "passed", "failures", "errors", "skipped")
GPU_KEYS = ("busy_pct", "mean_util_pct", "max_mem_mib")
DURATION_KEYS = ("minutes", "seconds")
NO_GPU = "no GPU readings (nvidia-smi not found or the step was too short)"


def rule_error(rule) -> str | None:
    """Why a rule cannot be checked, or None. Config validation uses it, so a bad rule fails at load time."""
    if isinstance(rule, (dict, list, tuple, set)):
        return (f"a rule is one value such as \">= 0.9\", \"~= 0.93 +- 0.01\" or \"between 0.8 and 0.95\" "
                f"(or a bare value meaning equality); got {rule!r}")
    if isinstance(rule, float) and math.isnan(rule):
        return "a rule of .nan never holds, since nothing equals NaN; write the value to compare with"
    if not isinstance(rule, str):
        return None
    typo = re.match(r"\s*(=>|=<|===|=(?!=)|≥|≤|≠)", rule)
    if typo:
        return f"{rule!r}: {typo.group(1)} is not a comparison; use >=, <=, ==, !=, > or <, e.g. \">= 0.9\""
    op = RULE.match(rule)
    if re.match(r"\s*[<>=!]", rule) and (not op or op.group(2).strip()[:1] in ("", "<", ">", "=")):
        return f"{rule!r} has no value after its comparison; write one, e.g. \">= 0.9\""
    if op and op.group(1) != "!=" and isinstance(_num(op.group(2)), float) and math.isnan(_num(op.group(2))):
        return f"{rule!r} never holds, since nothing equals or orders against NaN (\"!= nan\" asks for a number)"
    if not re.match(r"\s*(~=|between\b)", rule, re.I):
        return None
    m = NEAR.match(rule) or BETWEEN.match(rule)
    try:
        a, b = (float(x) for x in m.groups()[:2]) if m else (None, None)
    except ValueError:
        a = None
    if a is None:
        return (f"{rule!r} is neither a tolerance such as \"~= 0.93 +- 0.01\" (or \"~= 100 +- 5%\") nor a range "
                "such as \"between 0.8 and 0.95\"")
    if math.isnan(a) or math.isnan(b) or m.re is NEAR and not (math.isfinite(a) and math.isfinite(b)):
        return f"{rule!r}: its numbers must be real numbers" + (" (a range may end at inf)" if m.re is BETWEEN else "")
    if m.re is BETWEEN and a > b:
        return f"{rule!r}: write the low end first, \"between {m.group(2)} and {m.group(1)}\""
    if m.re is NEAR and b < 0:
        return f"{rule!r}: the tolerance after +- must not be negative"
    return None


def compare(actual, rule) -> tuple[bool, str]:
    """rule is '>= 0.9', '== done', '~= 0.93 +- 0.01', '~= 100 +- 5%', 'between 0.8 and 0.95' (inclusive), or a
    bare value meaning equality."""
    bad = rule_error(rule)
    if bad:
        return False, f"{actual!r}, but the rule cannot be checked: {bad}"
    near = NEAR.match(rule) if isinstance(rule, str) else None
    span = BETWEEN.match(rule) if isinstance(rule, str) else None
    m = RULE.match(rule) if isinstance(rule, str) and not (near or span) else None
    op, target = (m.group(1), _num(m.group(2))) if m else ("==", rule)
    a = _num(actual)
    if isinstance(a, float) and not math.isfinite(a) and (near or span or isinstance(actual, float) or _real(target)):
        return False, f"{actual!r} is not a finite number, so it does not satisfy {rule}"      # a NaN loss passes nothing
    if near or span:
        want = f"{near.group(1)} ± {near.group(2)}{near.group(3)}" if near else \
            f"between {span.group(1)} and {span.group(2)}"
        if not _real(a):
            return False, f"{actual!r} is not a number, so it cannot be compared with {want}"
        x = _float(a)
        if span:
            return float(span.group(1)) <= x <= float(span.group(2)), f"{actual!r} (wanted {want})"
        center, tol = float(near.group(1)), float(near.group(2))
        tol = abs(center) * tol / 100 if near.group(3) else tol
        slack = 4 * math.ulp(max(abs(x), abs(center), tol))      # rounding only (0.94 is within 0.93 +- 0.01), at any scale
        ok = math.isfinite(x) and abs(x - center) <= tol + slack
        return ok, f"{actual!r} (wanted {want}; differs by {x - center:+.4g})"
    if isinstance(a, bool) and isinstance(target, str) and target.lower() in ("true", "false"):
        target = target.lower() == "true"                                                # "== true" for a JSON true
    if isinstance(a, bool) != isinstance(target, bool):                    # true is not 1, and not a number to order
        if op in ("==", "!="):
            return op == "!=", f"{actual!r} (wanted {op} {target!r})"
        return False, f"{actual!r} cannot be compared with {op} {target!r}"
    try:
        ok = OPS[op](a, target)
    except TypeError:
        return False, f"{actual!r} cannot be compared with {op} {target!r}"
    return bool(ok), f"{actual!r} (wanted {op} {target!r})"


def number_rule_error(rule) -> str | None:
    """Why a rule cannot hold for a reader's number (a finite number or true/false), or None: a rule against text or
    null could never pass, so the numbers: and strategy: checks refuse it when testbench.yaml loads."""
    bad = rule_error(rule)
    if bad or isinstance(rule, (bool, int, float)):
        return bad
    if isinstance(rule, str):
        if NEAR.match(rule) or BETWEEN.match(rule):
            return None
        m = RULE.match(rule)
        target = _num(m.group(2) if m else rule)
        if _real(target) or (str(target).lower() in ("true", "false") and (not m or m.group(1) in ("==", "!="))):
            return None
    return (f"{rule!r} compares with {'nothing' if rule is None else 'text'}, but a reader's numbers are numbers or "
            "true/false: write the number to compare with, e.g. \">= 1\" or \"between 0.8 and 0.95\"")


def compare_shown(actual, rule) -> tuple[bool, str]:
    """compare(), with a float shown to 6 significant digits (1.2345678912 as 1.23457) unless that would change the
    outcome, which is always the exact value's."""
    ok, detail = compare(actual, rule)
    if isinstance(actual, float) and math.isfinite(actual):
        short_ok, short = compare(float(f"{actual:.6g}"), rule)
        if short_ok == ok:
            return ok, short
    return ok, detail


def _real(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _float(x) -> float:
    try:
        return float(x)
    except OverflowError:                                # an integer too big for a float
        return math.inf if x > 0 else -math.inf


def _num(x):
    if isinstance(x, (int, float, bool)) or x is None:
        return x
    try:
        return float(x)
    except (TypeError, ValueError):
        return str(x).strip().strip("'\"")


def _number(text):
    """A number as printed ('12', '3.5e-4', '0.25.'), else the text itself."""
    if text is None:
        return None
    t = text.strip().rstrip(".,;:")
    try:
        return int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)
    except ValueError:
        return text


def dig(data, path: str):
    """'a.b.0' and 'losses.-1' walk dicts and lists."""
    cur = data
    for part in str(path).split("."):
        if isinstance(cur, list) and re.fullmatch(r"-?\d+", part):
            cur = cur[int(part)]
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(path)
    return cur


def _stale(f: Path, since) -> str | None:
    """A file last written before the run started is an old result, not this run's."""
    if since and f.stat().st_mtime < since - 2:
        return "the file was last written before this run started, so it is an old result, not this run's"
    return None


def _read(source: str, workdir: Path, result: dict) -> tuple[str | None, str | None]:
    """The text of stdout (everything the step printed) or of a file; or None and why not."""
    if source == "stdout":
        return result.get("stdout", ""), None
    f = workdir / source
    if not f.is_file():
        return None, "file missing"
    try:
        stale = _stale(f, result.get("run_started"))
        return (None, stale) if stale else (f.read_text(errors="replace"), None)
    except OSError as exc:
        return None, f"cannot be read: {exc}"


# ---- checking a step, and validating its expect:, through the "checks" registry -------------------------------------

def check(expect: dict, *, workdir: Path, result: dict) -> list[dict]:
    """Every finding for a step's `expect:`: each check it names (and each one with a default), through the "checks"
    registry. result: the step's outcome (kind, exit_code, stdout, notebook error, web observations, gpu window,
    seconds it ran, run_started, step_dir, mocks, the command it ran)."""
    out = []
    add = lambda name, ok, detail: out.append({"check": name, "ok": bool(ok), "detail": detail})
    found = plugins.registry("checks")
    named = [n for n in expect if n not in RUN_ORDER]
    for name in [*RUN_ORDER, *named, *(n for n in found if n not in RUN_ORDER and n not in named)]:
        chk = found.get(name)
        if chk is None:
            if name in expect:                           # validated where testbench.yaml loaded, but not here
                add(str(name), False, _not_here(name))
            continue
        if name in expect:
            value = expect[name]
        elif hasattr(chk, "default"):
            value = chk.default
        else:
            continue
        if plugins.builtin("checks", name):
            chk.check(value, workdir=workdir, result=result, add=add)
            continue
        try:                                             # a plug-in's bug fails its step, never the run
            chk.check(copy.deepcopy(value), workdir=workdir, result=result,
                      add=lambda name, ok, detail="": add(str(name), ok, str(detail)))
        except Exception as exc:
            add(str(name), False, f"the check plug-in {name!r} failed: {type(exc).__name__}: {str(exc)[:300]}. That is "
                                  "a bug in the plug-in, not in the step; `testbench plugin test` shows it - fix or "
                                  "reinstall that package")
    return out


def validate(where: str, expect: dict) -> None:
    """Refuse a malformed `expect:` when testbench.yaml loads (config._expect), before anything runs: a name no check
    has, or one whose plug-in did not load; then each check's own validate(), in the registry's order."""
    names = plugins.names("checks")
    for key in expect:
        if key not in names:
            raise ConfigError(_unknown(where, key, names))
    present = [(name, plugins.get("checks", name)) for name in names if name in expect]
    for name, chk in present:                            # a path that is not text first, as it always was
        if getattr(chk, "keyed_by", None):
            _path_keys(f"{where}.{name}", expect[name], chk.keyed_by)
    for name, chk in present:
        if plugins.builtin("checks", name):
            chk.validate(f"{where}.{name}", expect[name])
            continue
        try:
            chk.validate(f"{where}.{name}", copy.deepcopy(expect[name]))
        except ConfigError:
            raise
        except Exception as exc:
            raise ConfigError(f"`{where}.{name}`: the check plug-in {name!r} failed while reading this value "
                              f"({type(exc).__name__}: {str(exc)[:300]}). That is a bug in the plug-in; `testbench "
                              "plugin test` shows it - fix or reinstall that package") from None


def _unknown(where: str, key, names: list[str]) -> str:
    broken = plugins.broken("checks")
    if key in broken:
        return (f"`{where}.{key}`: the check {key!r} is installed but did not load ({broken[key]}); `testbench "
                "plugins` shows it - reinstall or fix that package")
    start = [n for n in names if isinstance(key, str) and len(key) > 2 and (n.startswith(key) or key.startswith(n))]
    hint = f" (did you mean {start[0]}?)" if len(start) == 1 else near(key, names) or (
        "; a check from a plug-in package works once that package is installed here (`testbench plugins` lists them)")
    return f"`{where}` has no {key!r} check; expectations are {', '.join(names)}{hint}"


def _not_here(name) -> str:
    broken = plugins.broken("checks").get(name)
    if broken:
        return f"the check {name!r} is installed where this step was checked but did not load ({broken})"
    return (f"no check {name!r} is installed where this step was checked, so it was not checked: install the package "
            "that provides it there too (a remote backend's machine needs it as well as this one)")


def validate_rule(where: str, rule) -> None:
    """Refuse a rule that cannot be checked (`where` names the field)."""
    bad = rule_error(rule)
    if bad:
        raise ConfigError(f"`{where}`: {bad}")


def validate_table(where: str, value, example: str, keys=None, some=False) -> dict:
    """A mapping; with keys, each of its values is a rule and only those keys are allowed; with some, not empty."""
    if not isinstance(value, dict):
        raise ConfigError(f"`{where}` must be a mapping, e.g. {example}")
    if some and not value:                               # {metrics.json: {}} would check nothing, even a missing file
        raise ConfigError(f"`{where}` is empty, so nothing would be checked; e.g. {example}")
    for k, rule in value.items() if keys else ():
        if k not in keys:
            raise ConfigError(f"`{where}` has no {k!r}; use {', '.join(keys)}, e.g. {example}")
        validate_rule(f"{where}.{k}", rule)
    return value


def _path_keys(where: str, value, keyed_by: str) -> None:
    for k in value if isinstance(value, dict) else ():
        if not isinstance(k, str) or not k.strip():
            raise ConfigError(f"`{where}` has {k!r} where {keyed_by} goes; write the path in quotes")


# ---- Check, and the built-in checks: each one a Check in the same registry as plug-ins -----------------------------

class Check:
    """One kind of `expect:` check (the module's docstring has the interface). Subclassing it is optional: any object
    with name, validate, check and example will do."""
    name: str = ""
    keys: tuple[str, ...] | None = None
    schema: dict | None = None
    keyed_by: str | None = None          # built-ins keyed by a file: today's first message for a key that is no path

    def __repr__(self) -> str:
        return f"<check {self.name}>"


class ExitCodeCheck(Check):
    name, example, default = "exit_code", 0, None          # None: run steps must exit 0

    def validate(self, where, value):
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"`{where}` is the code the command must exit with, a whole number such as 0")

    def check(self, value, *, workdir, result, add):
        if result.get("kind") == "run" or value is not None:
            want = 0 if value is None else value
            add(f"exit code {want}", result.get("exit_code") == want, f"exited {result.get('exit_code')}")


class NoErrorsCheck(Check):
    name, example, default = "no_errors", True, True

    def validate(self, where, value):
        if not isinstance(value, bool):
            raise ConfigError(f"`{where}` is true or false")

    def check(self, value, *, workdir, result, add):
        if result.get("kind") == "notebook" and value:
            err = result.get("error")
            add("every cell ran without an error", not err, "all cells ran" if not err else
                f"cell {err.get('cell')}: {err.get('ename')}: {str(err.get('evalue'))[:300]}")


class FilesCheck(Check):
    name, example = "files", ["outputs/model.pt", "plots/*.png"]

    def validate(self, where, value):
        if not isinstance(value, list) or not all(isinstance(f, str) and f.strip() for f in value):
            raise ConfigError(f"`{where}` is a list of paths or globs, e.g. [outputs/model.pt, \"plots/*.png\"]")

    def check(self, value, *, workdir, result, add):
        since = result.get("run_started")
        for pattern in value:
            hits = sorted(glob.glob(str(workdir / pattern), recursive=True))
            fresh = [h for h in hits if not _stale(Path(h), since)]
            add(f"file {pattern}", fresh, f"{len(fresh)} found" if fresh else
                f"{len(hits)} found, all last written before this run started: old results, not this run's" if hits
                else "missing")


class JsonCheck(Check):
    name, keyed_by = "json", "a file's path"
    example = {"outputs/metrics.json": {"accuracy": ">= 0.9", "losses.-1": "< 0.5"}}

    def validate(self, where, value):
        _path_keys(where, value, self.keyed_by)
        for path, rules in validate_table(where, value or {}, '{metrics.json: {accuracy: ">= 0.9"}}').items():
            for key, rule in validate_table(f"{where}.{path}", rules, '{accuracy: ">= 0.9", "losses.-1": "< 0.5"}',
                                            some=True).items():
                validate_rule(f"{where}.{path}.{key}", rule)

    def check(self, value, *, workdir, result, add):
        for path, rules in (value or {}).items():
            f = workdir / path
            if any(c in str(path) for c in "*?["):           # a pattern, e.g. **/metrics.json: it must name one file
                hits = sorted({os.path.realpath(h): h for h in glob.glob(str(workdir / path), recursive=True)}.values())
                if len(hits) != 1:
                    found = ", ".join(Path(h).relative_to(workdir).as_posix() for h in hits[:5])
                    for key in rules:
                        add(f"{path}: {key}", False, f"{len(hits)} files match ({found}); the pattern must match "
                                                     "exactly one" if hits else "file missing (nothing matches)")
                    continue
                f = Path(hits[0])
            try:
                stale = _stale(f, result.get("run_started"))
                data = None if stale else json.loads(f.read_text())
            except FileNotFoundError:
                stale = "file missing"
            except OSError as exc:
                add(f"{path}", False, f"cannot be read: {exc}")
                continue
            except (ValueError, RecursionError) as exc:      # bad JSON, not UTF-8, a number too long to read
                add(f"{path}", False, f"not valid JSON: {exc}")
                continue
            if stale:
                for key in rules:
                    add(f"{path}: {key}", False, stale)
                continue
            for key, rule in rules.items():
                try:
                    ok, detail = compare(dig(data, key), rule)
                except (KeyError, IndexError):
                    ok, detail = False, f"no {key!r} in the file"
                add(f"{path}: {key} {rule}", ok, detail)


class TextCheck(Check):
    name, keyed_by, example = "text", "a file's path (or stdout)", {"stdout": ["epoch 10"]}

    def validate(self, where, value):
        _path_keys(where, value, self.keyed_by)
        for source, needles in validate_table(where, value or {}, '{stdout: ["epoch 10"]}').items():
            needles = [needles] if isinstance(needles, str) else needles
            if not isinstance(needles, list) or not needles or not all(isinstance(n, str) and n for n in needles):
                raise ConfigError(f"`{where}.{source}` is the text to find, or a list of texts, e.g. [\"epoch 10\"] "
                                  "(an empty text is found in anything)")

    def check(self, value, *, workdir, result, add):
        for source, needles in (value or {}).items():
            text, why = _read(source, workdir, result)       # a log left by an earlier run does not count
            for needle in ([needles] if isinstance(needles, str) else needles):
                if text is None:
                    add(f"{source} contains {needle!r}", False, why)
                else:
                    add(f"{source} contains {needle!r}", needle in text, "found" if needle in text else "not found")


class MediaCheck(Check):
    name, keyed_by, keys = "media", "a file's path", MEDIA_KEYS
    example = {"out/video.mp4": {"frames": ">= 120", "width": 1920, "audio": True}}

    def validate(self, where, value):
        _path_keys(where, value, self.keyed_by)
        for path, rules in validate_table(where, value or {}, '{out/video.mp4: {frames: ">= 120"}}').items():
            validate_table(f"{where}.{path}", rules, '{frames: ">= 120", width: 1920, audio: true}', MEDIA_KEYS,
                           some=True)

    def check(self, value, *, workdir, result, add):
        since = result.get("run_started")
        for path, rules in (value or {}).items():
            stale = (workdir / path).is_file() and _stale(workdir / path, since)
            info = {"error": stale} if stale else probe_media(workdir / path)
            for key, rule in rules.items():
                if "error" in info:
                    add(f"{path}: {key}", False, info["error"])
                    continue
                ok, detail = compare(info.get(key), rule)
                add(f"{path}: {key} {rule}", ok, detail)


class WebCheck(Check):
    name, keys, default = "web", WEB_KEYS, None             # every web step: no console errors unless said otherwise
    example = {"status": 200, "text": ["Count: 2"], "no_console_errors": True}

    def validate(self, where, value):
        web = validate_table(where, value or {}, '{status: 200, text: ["Count: 2"]}')
        for k in web:
            if k not in WEB_KEYS:
                raise ConfigError(f"`{where}` has no {k!r}; use {', '.join(WEB_KEYS)}")
        validate_rule(f"{where}.status", web.get("status"))
        if not isinstance(web.get("text", []), list) or not all(isinstance(t, str) and t for t in web.get("text", [])):
            raise ConfigError(f"`{where}.text` is a list of texts the page must show, e.g. [\"Count: 2\"]")
        if "title" in web and not (isinstance(web["title"], str) and web["title"]):
            raise ConfigError(f"`{where}.title` is text the page's title contains, e.g. Counter")
        if not isinstance(web.get("no_console_errors", True), bool):
            raise ConfigError(f"`{where}.no_console_errors` is true or false")

    def check(self, value, *, workdir, result, add):
        if result.get("kind") != "web":
            return
        web, rules = result.get("web") or {}, value or {}
        if "status" in rules:
            ok, detail = compare(web.get("status"), rules["status"])
            add(f"page status {rules['status']}", ok, detail)
        for needle in rules.get("text", []):
            add(f"page shows {needle!r}", needle in web.get("text", ""), "found" if needle in web.get("text", "") else
                "not found")
        if rules.get("title"):
            add(f"title contains {rules['title']!r}", rules["title"] in web.get("title", ""), repr(web.get("title")))
        if rules.get("no_console_errors", True):
            errs = web.get("console_errors", []) + web.get("page_errors", [])
            add("no console or page errors", not errs, "none" if not errs else "; ".join(errs)[:500])
        if web.get("action_errors"):
            add("every action worked", False, "; ".join(web["action_errors"])[:500])


class JunitCheck(Check):
    name, keys, example = "junit", ("path", *JUNIT_KEYS), {"path": "report.xml", "tests": ">= 10"}

    def validate(self, where, value):
        example = '{path: report.xml, failures: 0, tests: ">= 10"}'
        if value is not None and not isinstance(value, (str, dict, list)):
            raise ConfigError(f"`{where}` is the report's path or {example}")
        for i, spec in enumerate(junit_specs(value) or ([None] if isinstance(value, (str, dict)) else [])):
            at = where + (f"[{i}]" if isinstance(value, list) else "")
            if not isinstance(spec, dict) or not isinstance(spec.get("path"), str) or not spec["path"].strip():
                raise ConfigError(f"`{at}` needs `path:`, the JUnit XML file the test command writes, e.g. {example} "
                                  "(pytest: --junitxml=report.xml)")
            validate_table(at, {k: v for k, v in spec.items() if k != "path"}, example, JUNIT_KEYS)

    def check(self, value, *, workdir, result, add):
        for spec in junit_specs(value):
            _junit(spec, workdir, result, add)


class MetricsCheck(Check):
    name, keyed_by = "metrics", "a file's path (or stdout)"
    example = {"stdout": {"val_loss=([0-9.eE+-]+)": "< 0.3"}}

    def validate(self, where, value):
        _path_keys(where, value, self.keyed_by)
        example = '{"val_loss=([0-9.eE+-]+)": "< 0.3"}'
        for source, patterns in validate_table(where, value or {}, f"{{stdout: {example}}}").items():
            for pattern, rule in validate_table(f"{where}.{source}", patterns, example, some=True).items():
                if not isinstance(pattern, str):
                    raise ConfigError(f"`{where}.{source}` has {pattern!r} where a regular expression goes; write it "
                                      f"in quotes, e.g. {example}")
                try:
                    re.compile(pattern)
                except re.error as exc:
                    raise ConfigError(f"`{where}.{source}`: {pattern!r} is not a regular expression ({exc}); its "
                                      "first group is the number, e.g. \"val_loss=([0-9.eE+-]+)\"") from None
                validate_rule(f"{where}.{source}.{pattern}", rule)

    def check(self, value, *, workdir, result, add):
        for source, patterns in (value or {}).items():
            text, why = _read(source, workdir, result)
            for pattern, rule in patterns.items():
                name = f"{source} /{pattern}/ {rule}"
                hits = list(re.finditer(pattern, text, re.M)) if text is not None else []
                if text is None or not hits:
                    add(name, False, why or f"no match in {source} ({counted(len(text.splitlines()), 'line')})")
                    continue
                last = hits[-1]
                ok, detail = compare(_number(last.group(1) if last.re.groups else last.group(0)), rule)
                add(name, ok, f"{detail}; {'the only match' if len(hits) == 1 else f'the last of {len(hits)} matches'}"
                              f": {last.group(0).strip()[:80]!r}")


class NumbersCheck(Check):
    name, keys = "numbers", NUMBERS_KEYS
    example = {"reader": "json", "files": "outputs/metrics.json", "rules": {"accuracy": ">= 0.9"}}

    def validate(self, where, value):
        if isinstance(value, list) and not value:
            raise ConfigError(f"`{where}` is empty, so nothing would be checked; e.g. {NUMBERS_EXAMPLE}")
        for i, block in enumerate(value if isinstance(value, list) else [value]):
            _numbers_block(where + (f"[{i}]" if isinstance(value, list) else ""), block)

    def check(self, value, *, workdir, result, add):
        from . import metrics as M
        for block in value if isinstance(value, list) else [value]:
            reader, files, rules = str(block.get("reader")), block.get("files"), block.get("rules") or {}
            options = dict(block.get("options") or {})
            try:                                         # a reader that takes `keys` reads the names the rules use
                if "keys" in (getattr(M.get(reader), "options", None) or {}) and "keys" not in options:
                    options["keys"] = list(rules)
            except ValueError:                           # no such reader here: read_numbers says so
                pass
            got, why = read_numbers(reader, workdir, result, files, options)
            number_findings(_numbers_label(reader, files), got, why, rules, add, shown=not files)


class GpuCheck(Check):
    name, keys, example = "gpu", GPU_KEYS, {"busy_pct": ">= 50", "max_mem_mib": "<= 15000"}

    def validate(self, where, value):
        validate_table(where, value or {}, '{busy_pct: ">= 50", max_mem_mib: "<= 15000"}', GPU_KEYS)

    def check(self, value, *, workdir, result, add):
        gpu = result.get("gpu")
        for key, rule in (value or {}).items():
            if not gpu:
                add(f"gpu {key} {rule}", False, NO_GPU)
                continue
            ok, detail = compare(gpu.get(key), rule)
            add(f"gpu {key} {rule}", ok, f"{detail} on {gpu.get('gpu') or 'the GPU'}, {gpu.get('readings', '?')} "
                                         "reading(s)")


class ImageCheck(Check):
    name, keyed_by, keys = "image", "a file's path", ("like", "max_diff")
    example = {"outputs/plot.png": {"like": "golden/plot.png", "max_diff": 0.02}}

    def validate(self, where, value):
        _path_keys(where, value, self.keyed_by)
        example = "{like: golden/plot.png, max_diff: 0.02}"
        for path, spec in validate_table(where, value or {}, f"{{outputs/plot.png: {example}}}").items():
            at = f"{where}.{path}"
            if not isinstance(spec, dict) or not isinstance(spec.get("like"), str) or set(spec) - {"like", "max_diff"}:
                raise ConfigError(f"`{at}` is {example}: the known-good image and the largest mean pixel difference "
                                  "(0..1)")
            most = spec.get("max_diff", 0.02)
            if not isinstance(most, (int, float)) or isinstance(most, bool) or not 0 <= most <= 1:
                raise ConfigError(f"`{at}.max_diff` is a number from 0 (identical) to 1, e.g. 0.02")

    def check(self, value, *, workdir, result, add):
        for path, spec in (value or {}).items():
            most = spec.get("max_diff", 0.02)
            diff, note = image_diff(workdir / path, workdir / str(spec.get("like")), result.get("run_started"))
            if diff is None:
                add(f"{path} looks like {spec.get('like')}", False, note)
            else:
                ok, detail = compare(round(diff, 4), f"<= {most}")
                add(f"{path} looks like {spec.get('like')}", ok,
                    f"{detail}, the mean difference per pixel and channel (0 identical, 1 opposite){note}")


class DurationCheck(Check):
    name, keys, example = "duration", DURATION_KEYS, {"minutes": "<= 5"}

    def validate(self, where, value):
        validate_table(where, value or {}, '{minutes: "<= 5"}', DURATION_KEYS)

    def check(self, value, *, workdir, result, add):
        secs = result.get("seconds")
        for key, rule in (value or {}).items():
            ok, detail = compare(None if secs is None else round(secs / 60, 3) if key == "minutes" else round(secs, 2),
                                 rule)
            add(f"duration {key} {rule}", ok, detail)


class MockCallsCheck(Check):
    name, keys, default = "mock_calls", MOCK_KEYS, None     # a step with mocks: refuses no call unless said otherwise
    example = {"anthropic": {"calls": "<= 5", "output_tokens": "<= 2000"}}

    def validate(self, where, value):
        example = '{calls: "<= 5", output_tokens: "<= 2000"}'
        for sdk, rules in validate_table(where, value or {}, f"{{anthropic: {example}}}").items():
            from . import mocks as M                            # a layer: only when a step names a mock
            keys = M.call_keys(sdk, where)                      # built-in or plug-in; an unknown name says which exist
            broker = sdk in BROKER_SDKS                         # the mock brokers count orders, not tokens
            validate_table(f"{where}.{sdk}", rules, '{orders: "<= 20", orders_without_limit: 0}' if broker else example,
                           keys)

    def check(self, value, *, workdir, result, add):
        spec, mocked = value or {}, result.get("mocks") or {}
        for sdk in [*spec, *(s for s in mocked if s not in spec)]:
            _mock_calls(sdk, {**(spec.get(sdk) or {}), "refused": (spec.get(sdk) or {}).get("refused", 0)}, workdir,
                        result, add)


BUILTIN = {c.name: c for c in (ExitCodeCheck, NoErrorsCheck, FilesCheck, JsonCheck, TextCheck, MediaCheck, WebCheck,
                               JunitCheck, MetricsCheck, NumbersCheck, GpuCheck, ImageCheck, DurationCheck,
                               MockCallsCheck)}     # this module's; strategy is the trading pack's (STRATEGY)
REFS = {name: STRATEGY if name == "strategy" else f"{__name__}:{BUILTIN[name].__name__}" for name in KINDS}


def _builtin_class(name: str) -> type:
    module, _, attr = REFS[name].partition(":")
    return getattr(importlib.import_module(module), attr)


def _prepare(obj, name: str):
    """What the registry holds for an entry point: the check itself, or one made from its class."""
    chk = obj() if isinstance(obj, type) else obj
    missing = [m for m in ("validate", "check") if not callable(getattr(chk, m, None))]
    if missing:
        raise TypeError(f"{obj!r} is not a check: it has no {' and no '.join(m + '()' for m in missing)} "
                        "(agent_testbench.expect.Check)")
    if not getattr(chk, "name", None):
        chk.name = name
    return chk


# ---- the contract: what `testbench plugin test` checks for a check -----------------------------------------------

WRONG = (("nothing (null)", None, False), ("a list", [{"no_such_key": 1}], True),
         ("a mapping with a key nobody uses", {"no_such_key_at_all": [1]}, True))
RESULTS = {"run": {"exit_code": 0, "command": "python train.py"}, "notebook": {"error": None},
           "web": {"web": {"status": 200, "title": "", "text": "", "console_errors": [], "page_errors": []}}}


def contract(chk) -> list[str]:
    """Problems with one check, in plain words ([] = it keeps the contract): a name that is a plain YAML key; validate
    accepts its example and refuses an obviously wrong value with a ConfigError naming the field; check() on an empty
    folder records a finding, raises nothing and writes nothing outside the folder; a schema, if given, is one."""
    problems, name = [], getattr(chk, "name", None)
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", name) or \
            yaml.safe_load(f"{name}: 1") != {name: 1}:
        problems.append(f"its name {name!r} is not a plain YAML key: use letters, digits and _ (starting with a letter, "
                        "and not a word YAML reads as true, false or null, such as on, off, yes or no), e.g. "
                        "max_log_lines")
        name = "your_check"
    elif name in REFS and not isinstance(chk, _builtin_class(name)):
        problems.append(f"{name!r} is a built-in check's name, and a plug-in cannot replace a built-in; pick another")
    missing = [m for m in ("validate", "check") if not callable(getattr(chk, m, None))]
    if missing:
        return problems + [f"it has no {m}() (agent_testbench.expect.Check says what it takes)" for m in missing]
    where = f"plans.p.steps[0].expect.{name}"
    example = getattr(chk, "example", None)
    if example is None:
        problems.append("it has no `example`: a value its validate() accepts, which this test tries check() with (and "
                        "docs can show), e.g. example = 200")
    else:
        try:
            chk.validate(where, copy.deepcopy(example))
        except Exception as exc:
            problems.append(f"validate() refuses its own example {example!r}: {type(exc).__name__}: {exc}")
            example = None
    for label, wrong, refuse in WRONG:
        try:
            chk.validate(where, copy.deepcopy(wrong))
        except ConfigError as exc:
            if where not in str(exc):
                problems.append(f"validate() refused {label}, but its message does not name the field (start it with "
                                f"`{where}`, the `where` it was given): {exc}")
            continue
        except Exception as exc:
            problems.append(f"validate() raised {type(exc).__name__} for {label} ({exc}); refuse a wrong value with "
                            "agent_testbench.config.ConfigError, naming the field and what to write instead")
            continue
        if refuse:
            problems.append(f"validate() accepted {label} ({wrong!r}); refuse it with a ConfigError that names the field "
                            "and says what to write, so a mistake shows when testbench.yaml loads, not after a run")
    if example is not None:
        problems += _try_check(chk, example)
    problems += _schema_problems(chk, example)
    return problems


def _try_check(chk, example) -> list[str]:
    """check() with the example on an empty folder, as each kind of step: it records a finding (for at least one kind),
    raises nothing and writes nothing outside the folder."""
    problems, findings, here = [], 0, os.getcwd()
    for kind, extra in RESULTS.items():
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            workdir, cwd = base / "workdir", base / "cwd"
            workdir.mkdir()
            cwd.mkdir()
            before = _files(base, workdir)
            got = []
            result = {"kind": kind, "stdout": "", "seconds": 1.0, "run_started": time.time() - 1, **copy.deepcopy(extra)}
            try:
                os.chdir(cwd)                            # a relative path written by mistake lands here, where we look
                chk.check(copy.deepcopy(example), workdir=workdir, result=result,
                          add=lambda *a, **kw: got.append((a, kw)))
            except Exception as exc:
                problems.append(f"check() raised {type(exc).__name__} ({exc}) for a {kind} step on an empty folder; a "
                                "missing or unreadable file is a failing finding - add(name, False, \"file missing\") - "
                                "never an exception")
            finally:
                os.chdir(here)
            wrote = sorted(p for p, stamp in _files(base, workdir).items() if before.get(p) != stamp)
            if wrote:
                problems.append(f"check() wrote {', '.join(wrote[:3])} outside the step's folder; a check only reads")
            for args, kw in got:
                a = dict(zip(("name", "ok", "detail"), args), **kw)
                if set(a) != {"name", "ok", "detail"} or not isinstance(a["name"], str) or not a["name"] or \
                        not isinstance(a["detail"], str):
                    problems.append(f"check() called add() with {args!r} {kw or ''}; call add(name, ok, detail): name "
                                    "and detail are text, ok is true or false")
                    break
            findings += len(got)
    if not findings:
        problems.append("check() recorded no finding for its example on an empty folder (as a run, notebook or web "
                        "step); a missing file is a failing finding, and a check with nothing to check says so with "
                        "add(name, True, \"nothing to check\")")
    return problems


def _files(base: Path, skip: Path) -> dict:
    return {p.relative_to(base).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in base.rglob("*")
            if p.is_file() and skip not in p.parents}


def _schema_problems(chk, example) -> list[str]:
    schema = getattr(chk, "schema", None)
    if schema is None:
        return []
    if not isinstance(schema, (dict, bool)):
        return [f"its schema is {type(schema).__name__}; give a JSON Schema (a dict) for its value, or none"]
    try:
        import jsonschema
    except ImportError:                                  # an optional package: the shape is all that can be checked
        return []
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        return [f"its schema is not valid JSON Schema (draft 2020-12): {exc.message}"]
    if example is None:
        return []
    from .config import SCHEMA
    defs = json.loads(SCHEMA.read_text(encoding="utf-8"))["$defs"]
    try:
        bad = next(jsonschema.Draft202012Validator({"$defs": defs, **schema} if isinstance(schema, dict) else schema)
                   .iter_errors(copy.deepcopy(example)), None)
    except Exception as exc:                             # e.g. a $ref to nothing in testbench.schema.json
        return [f"its schema cannot be used: {type(exc).__name__}: {exc}"]
    return [f"its own example does not match its schema: {bad.message}"] if bad else []


def schema_text() -> str:
    """testbench.yaml's JSON Schema (`testbench schema`), with each installed check plug-in's `schema` added under
    $defs.expect; exactly the shipped file when no plug-in gives one."""
    from .config import SCHEMA
    text = SCHEMA.read_text(encoding="utf-8")
    extra = {name: chk.schema for name, chk in plugins.registry("checks").items()
             if not plugins.builtin("checks", name) and isinstance(getattr(chk, "schema", None), (dict, bool))}
    if not extra:
        return text
    data = json.loads(text)
    data["$defs"]["expect"]["properties"].update(extra)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


KIND = plugins.Kind("checks", "check", "what a step's `expect:` can check", dict(REFS), prepare=_prepare,
                    contract=contract)


# ---- numbers from metric readers (metrics.py): numbers:, and the trading pack's strategy: -------------------------

def _numbers_block(where: str, block) -> None:
    from . import metrics as M
    if not isinstance(block, dict):
        raise ConfigError(f"`{where}` is {NUMBERS_EXAMPLE}, or a list of those: a metric reader, its files and rules "
                          "on the numbers it reads")
    for k in block:
        if k not in NUMBERS_KEYS:
            raise ConfigError(f"`{where}` has no {k!r}{near(k, NUMBERS_KEYS)}; use {', '.join(NUMBERS_KEYS)}, e.g. "
                              f"{NUMBERS_EXAMPLE}")
    reader, files, options = block.get("reader"), block.get("files"), block.get("options")
    if not isinstance(reader, str) or not reader.strip():
        raise ConfigError(f"`{where}` needs `reader:`, one of {', '.join(M.names())} (`testbench numbers` lists them "
                          f"and the files each reads), e.g. {NUMBERS_EXAMPLE}")
    try:
        M.get(reader)
    except ValueError as exc:
        raise ConfigError(f"`{where}.reader`: {exc}") from None
    if files is not None and not _paths(files):
        raise ConfigError(f"`{where}.files` is a path, a list of paths in the reader's order, or {{role: path}} such "
                          "as {equity: out/equity.csv}; leave it out and the reader looks for its own files")
    if options is not None and (not isinstance(options, dict) or "files" in options):
        raise ConfigError(f"`{where}.options` is a mapping of the reader's options, e.g. {{risk_free: 0.04}}"
                          + ("; files go under `files:`, next to `options:`" if isinstance(options, dict) else ""))
    try:
        M.options(reader, {**(options or {}), "files": files})
    except ValueError as exc:
        raise ConfigError(f"`{where}`: {exc}") from None
    if "rules" not in block:
        raise ConfigError(f"`{where}` needs `rules:`, a rule on each number by name, e.g. {{sharpe: \">= 1\"}} "
                          "(`testbench numbers READER PATH` prints the names and their numbers)")
    rules = validate_table(f"{where}.rules", block["rules"], '{sharpe: ">= 1", trades: ">= 30"}', some=True)
    known = M.keys(reader)
    for key, rule in rules.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigError(f"`{where}.rules` has {key!r} where the name of a number goes; write it in quotes")
        if known is not None and key not in known:
            raise ConfigError(f"`{where}.rules` names {key!r}{near(key, known)}, a number reader {reader} does not "
                              f"give; it gives {', '.join(known)}")
        bad = number_rule_error(rule)
        if bad:
            raise ConfigError(f"`{where}.rules.{key}`: {bad}")


def _paths(files) -> bool:
    """files: a path, a non-empty list of paths, or a non-empty {role: path}."""
    path = lambda p: isinstance(p, str) and bool(p.strip())
    if isinstance(files, list):
        return bool(files) and all(map(path, files))
    if isinstance(files, dict):
        return bool(files) and all(path(r) and path(p) for r, p in files.items())
    return path(files)


def _numbers_label(reader: str, files) -> str:
    """How findings name what was read: the reader and the first file named, as testbench.yaml gives them, so the
    name is the same in every run (history and loops find a number by it)."""
    if isinstance(files, dict):
        files = list(files.values())
    first = files if isinstance(files, str) else files[0] if isinstance(files, list) and files else ""
    return f"{reader} {first}".strip()


def read_numbers(reader: str, workdir: Path, result: dict, files=None, options=None) -> tuple[dict | None, str]:
    """What a metric reader finds in the step's folder ({reader, files, values, notes}, as metrics.read gives it),
    or None and why not: a file missing or refused, or last written before the run started (an old result, not this
    run's). Never raises: a check records what went wrong as a finding."""
    from . import metrics as M
    since = result.get("run_started")
    try:
        ready = M.options(reader, {**(options or {}), "files": files}, values=False)
        why = _stale_among(workdir, ready["files"].values(), since)        # the files named: before reading them
        got = None if why else M.read(reader, workdir, ready)
    except (FileNotFoundError, ValueError) as exc:
        return None, str(exc)
    except Exception as exc:                                               # never expected: M.read wraps a reader's
        return None, f"reader {reader} failed: {type(exc).__name__}: {exc}"
    why = why or _stale_among(workdir, got["files"].values(), since)        # and those it found by their patterns
    return (None, why) if why else (got, "")


def _stale_among(workdir: Path, paths, since) -> str:
    for p in paths:
        f = Path(workdir) / p
        try:
            why = f.is_file() and _stale(f, since)
        except OSError:
            continue
        if why:
            return f"{p}: {why}"
    return ""


def notes_about(key: str, notes: list[str]) -> list[str]:
    """The reader's notes that name a number (or, for a dotted name such as loss.last, its first part), as a word of
    its own: trades in "no trades file: fills, trades, ..." but not in a file's name such as out/trades.csv."""
    words = {key, key.split(".")[0]}
    return [n for n in notes if any(re.search(rf"(?<![\w.]){re.escape(w)}(?!\w|\.\w)", n) for w in words)]


def number_findings(label: str, got: dict | None, why: str, rules: dict, add, about=notes_about,
                    context: str = "", shown: bool = False, remarks=None) -> None:
    """One finding per rule on a reader's numbers, named `{label}: {key} {rule}`. A number the reader did not give,
    or gave as None (not defined), fails with the reader's notes on it (about(key, notes) picks them): a rule never
    passes for want of a number. When the files could not be read (got None), every rule fails with why, in full on
    the first. After a measured number come context, the files read when shown (testbench.yaml named none), and the
    notes remarks(key, notes) picks, if given."""
    for i, (key, rule) in enumerate(rules.items()):
        name = f"{label}: {key} {rule}"
        if got is None:
            add(name, False, why if i == 0 or "\n" not in why else
                f"{why.splitlines()[0]} (the whole message is on the first of these checks)")
            continue
        values, notes = got["values"], got["notes"]
        if key not in values:
            names = list(values)
            add(name, False, f"reader {got['reader']} gave no number {key!r}{near(key, names)}; it gave "
                             f"{', '.join(names[:30]) or 'none'}{' and more' if len(names) > 30 else ''}")
        elif values[key] is None:
            add(name, False, f"{key} is undefined (null): " + ("; ".join(about(key, notes) or notes) or
                                                              f"reader {got['reader']} gave no reason"))
        else:
            ok, detail = compare_shown(values[key], rule)
            files = f"; read {', '.join(got['files'].values())}" if shown and got["files"] else ""
            add(name, ok, detail + (f"; {context}" if context else "") + files
                + "".join(f"; {n}" for n in (remarks(key, notes) if remarks else ())))


# ---- calls to the mock clients (docs/mocks.md) ---------------------------------------------------------------

def _refusals(recs: list, kinds: list, key: str) -> str:
    hits = [r for r, k in zip(recs, kinds) if k == key]
    return "; ".join(f"call {r.get('call')}: {r['error'].get('type')}: {r['error'].get('message')}"[:300]
                     for r in hits[:2]) + (f"; and {len(hits) - 2} more" if len(hits) > 2 else "")


def _mock_calls(sdk: str, rules: dict, workdir: Path, result: dict, add) -> None:
    step_dir = result.get("step_dir")
    path = Path(step_dir) / MOCK_LOG if step_dir else workdir / MOCK_LOG
    recs = []
    for line in path.read_text(errors="replace").splitlines() if path.is_file() else []:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("sdk") == sdk:
            recs.append(rec)
    kinds = [(r.get("error") or {}).get("kind") for r in recs]
    if sdk in BROKER_SDKS:
        _broker_calls(sdk, rules, recs, kinds, result, add)
        return
    found = {"calls": len(recs), "refused": kinds.count("refused"), "injected": kinds.count("injected"),
             "tool_calls": sum(len((r.get("reply") or {}).get("tool_calls") or []) for r in recs)}
    for key in ("input_tokens", "output_tokens"):
        found[key] = sum(int((r.get("usage") or {}).get(key) or 0) for r in recs)
    if not recs:
        context = "no calls were logged" + (f" (is `mocks: [{sdk}]` on this step?)" if sdk not in
                                            (result.get("mocks") or {}) else "")
    else:
        counts = {}
        for r in recs:
            label = " ".join(str(x) for x in (r.get("endpoint"), r.get("model")) if x)     # a plug-in has no model
            label += "" if r.get("known_model", True) else " (not a model the mock knows)"
            counts[label] = counts.get(label, 0) + 1
        context = "; ".join(f"{label} x{n}" for label, n in counts.items())
    for key, rule in rules.items():
        ok, detail = compare(found[key], rule)
        extra = context
        if key in ("refused", "injected") and found[key]:
            extra = _refusals(recs, kinds, key)
            if key == "injected":
                hits = [r for r, k in zip(recs, kinds) if k == key]
                extra += f" ({sum(bool(r['error'].get('retried')) for r in hits)} retried by the SDK itself)"
        elif key in ("input_tokens", "output_tokens"):
            extra = f"the mock's count (words x 1.3); {context}"
        add(f"mock {sdk} {key} {rule}", ok, f"{detail}; {extra}")


def _amount(v) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) else 0.0


def _order_text(rec: dict, o: dict) -> str:
    price = f" at {o['limit']}" if o.get("limit") is not None else f" stop {o['stop']}" if o.get("stop") is not None \
        else ""
    return (f"call {rec.get('call')}: {o.get('type')} {o.get('side')} {_amount(o.get('qty')):g} {o.get('symbol')}"
            f"{price}, value {_amount(o.get('value')):,.2f}")


def _broker_calls(sdk: str, rules: dict, recs: list, kinds: list, result: dict, add) -> None:
    """The mock brokers' log: every order placed, every fill and cancel, and whether a live account was asked for."""
    orders = [(r, o) for r in recs for o in r.get("orders") or [] if isinstance(o, dict)]
    fills = [f for r in recs for f in r.get("fills") or [] if isinstance(f, dict)]
    found = {"calls": len(recs), "refused": kinds.count("refused"), "orders": len(orders), "fills": len(fills),
             "cancels": sum(len(r.get("cancels") or []) for r in recs),
             "orders_without_limit": sum(o.get("limit") is None for _, o in orders),
             "max_order_value": round(max((_amount(o.get("value")) for _, o in orders), default=0.0), 6),
             "fees": round(sum(_amount(f.get("fee")) for f in fills), 6),
             "live": sum(bool(r.get("live")) for r in recs)}
    if recs:
        counts = {}
        for r in recs:
            label = f"{r.get('endpoint')}" + (" (live)" if r.get("live") else "")
            counts[label] = counts.get(label, 0) + 1
        context = "; ".join(f"{label} x{n}" for label, n in counts.items())
    else:
        context = "no calls were logged" + (f" (is `mocks: [{sdk}]` on this step?)" if sdk not in
                                            (result.get("mocks") or {}) else "")
    for key, rule in rules.items():
        ok, detail = compare(found[key], rule)
        extra = context
        if key == "refused" and found[key]:
            extra = _refusals(recs, kinds, key)
        elif key == "orders_without_limit" and found[key]:
            hits = [_order_text(r, o) for r, o in orders if o.get("limit") is None]
            extra = "; ".join(hits[:3]) + (f"; and {len(hits) - 3} more" if len(hits) > 3 else "")
        elif key == "max_order_value" and orders:
            extra = "the largest: " + _order_text(*max(orders, key=lambda ro: _amount(ro[1].get("value"))))
        elif key == "live" and found[key]:
            hits = [r for r in recs if r.get("live")]
            extra = "; ".join(f"call {r.get('call')}: {r.get('endpoint')}" + (
                f" (refused: {r['error'].get('type')})" if (r.get("error") or {}).get("kind") == "refused" else
                " (answered: allow_live is on)") for r in hits[:2]) + (f"; and {len(hits) - 2} more"
                                                                      if len(hits) > 2 else "")
        elif key == "fees":
            extra = f"{len(fills)} fill(s); {context}"
        add(f"mock {sdk} {key} {rule}", ok, f"{detail}; {extra}")


# ---- JUnit XML: a test suite's own report -------------------------------------------------------------------

def junit_specs(spec) -> list[dict]:
    """junit: report.xml | {path: report.xml, failures: 0, ...} | a list of those."""
    items = spec if isinstance(spec, list) else [spec] if spec else []
    return [{"path": x} if isinstance(x, str) else x for x in items]


# How each runner writes JUnit XML, for a step whose command ran one and wrote no report where it should.
JUNIT_HOW = (
    (r"\bcargo\b.*\bnextest\b", "cargo-nextest writes it only for a profile with a junit path: `[profile.ci.junit]` "
                                "`path = \"junit.xml\"` in .config/nextest.toml, then `cargo nextest run --profile ci` "
                                "writes target/nextest/ci/junit.xml"),
    (r"\bgotestsum\b", "gotestsum: --junitfile {path}"),
    (r"\bgo\s+test\b", "Go: `go run gotest.tools/gotestsum@v1.13.0 --junitfile {path} -- ./...` in place of go test"),
    (r"\bvitest\b", "vitest: --reporter=default --reporter=junit --outputFile.junit={path}"),
    (r"\bplaywright\b", "Playwright: --reporter=list,junit with PLAYWRIGHT_JUNIT_OUTPUT_NAME={path} in the step's env"),
    (r"\bjest\b", "jest: --reporters=default --reporters=jest-junit, with jest-junit installed; it writes junit.xml in "
                  "the working directory, or JEST_JUNIT_OUTPUT_FILE"),
    (r"\bmocha\b", "mocha: --reporter xunit --reporter-option output={path}"),
    (r"\bnode\b.*--test\b", "node --test: --test-reporter=junit --test-reporter-destination={path}"),
    (r"\bcargo\s+test\b", "cargo test writes no JUnit: cargo-nextest does (`cargo nextest run --profile ci`, with "
                          "`[profile.ci.junit]` `path = \"junit.xml\"` in .config/nextest.toml)"),
    (r"\bnpm\s+(?:run\s+)?test\b", "npm test: run its test runner with a JUnit reporter instead - jest with jest-junit, "
                                   "vitest --reporter=junit, mocha --reporter xunit"),
)


def _junit_how(command: str | None, path: str) -> str:
    how = next((h for pattern, h in JUNIT_HOW if command and re.search(pattern, command)), "pytest: add --junitxml={path}")
    return how.replace("{path}", path)


def _junit(spec: dict, workdir: Path, result: dict, add) -> None:
    path = str(spec.get("path"))
    f = workdir / path
    if not f.is_file():
        add(f"{path}: JUnit report", False, f"file missing: the test command wrote no report there "
                                            f"({_junit_how(result.get('command'), path)})")
        return
    try:
        got = _stale(f, result.get("run_started")) or read_junit(f)
    except Exception as exc:                                     # ParseError, or not a JUnit root
        got = f"not a JUnit XML report: {exc}"
    if isinstance(got, str):
        add(f"{path}: JUnit report", False, got)
        return
    for key, rule in {"failures": 0, "errors": 0, **{k: v for k, v in spec.items() if k != "path"}}.items():
        ok, detail = compare(got.get(key), rule)
        detail += "" if key == "tests" else f" of {counted(got['tests'], 'test')}"
        cases = {"failures": got["failing"], "errors": got["erroring"]}.get(key)
        if cases:
            detail += (f"; {'failed' if key == 'failures' else 'errored'}: " + "; ".join(cases[:3])
                       + (f"; and {len(cases) - 3} more" if len(cases) > 3 else ""))
        add(f"{path}: {key} {rule}", ok, detail)


def read_junit(path: Path) -> dict:
    """tests, passed, failures, errors, skipped and the failing cases of a JUnit XML report (pytest --junitxml,
    jest-junit, vitest, mocha's xunit, node --test, Playwright, gotestsum, go-junit-report, cargo-nextest, ...):
    <testsuites> or a bare <testsuite>. tests/data/junit/ holds a real report from each of those."""
    import xml.etree.ElementTree as ET
    root = ET.parse(path).getroot()
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError(f"the root element is <{root.tag}>, not <testsuites> or <testsuite>")
    out = {"tests": 0, "passed": 0, "failures": 0, "errors": 0, "skipped": 0, "failing": [], "erroring": []}
    parent = {child: el for el in root.iter() for child in el}
    cases = list(root.iter("testcase"))
    suites = [s for s in root.iter("testsuite") if s.find("testsuite") is None] or [root]
    for s in suites:
        if s.find(".//testcase") is None:                        # only totals: the suite's own counts
            got = {key: _int(s.get(key)) for key in ("tests", "failures", "errors", "skipped")}
            for key, n in got.items():
                out[key] += n
            out["passed"] += max(0, got["tests"] - got["failures"] - got["errors"] - got["skipped"])
    out["tests"] += len(cases)
    for case in cases:
        tags = {c.tag for c in case}
        out["skipped"] += "skipped" in tags
        out["passed"] += not tags & {"failure", "error", "skipped"}
        for tag, key, names in (("failure", "failures", "failing"), ("error", "errors", "erroring")):
            el = case.find(tag)
            if el is not None:
                out[key] += 1
                out[names].append(f"{_case_name(case, _suite_names(case, parent))} ({_case_why(el)})")
    more = {"failures": 0, "errors": 0}                          # e.g. a suite that failed before its cases ran
    for s in suites:
        listed = s.findall(".//testcase")
        if not listed:                                           # its totals are counted above
            continue
        said = {key: _int(s.get(key)) for key in more}
        got = {"failures": sum(c.find("failure") is not None for c in listed),
               "errors": sum(c.find("error") is not None for c in listed)}
        extra = sum(said.values()) - sum(got.values())           # the two together: mocha's xunit counts its
        if extra > 0:                                            # failures as errors, and is right about the total
            failures = min(extra, max(0, said["failures"] - got["failures"]))
            more["failures"] += failures
            more["errors"] += extra - failures
    for key, names in (("failures", "failing"), ("errors", "erroring")):
        if more[key]:
            out[names].append(f"{more[key]} more in the suites' totals than among their test cases")
            out[key] += more[key]
    return out


def _int(x) -> int:
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return 0


def _suite_names(case, parent: dict) -> list[str]:
    """The names of the <testsuite>s around a test case, outermost first."""
    out, el = [], parent.get(case)
    while el is not None:
        if el.tag == "testsuite" and el.get("name"):
            out.insert(0, el.get("name"))
        el = parent.get(el)
    return out


JS_FILE = re.compile(r"\.[cm]?[jt]sx?$")                          # vitest's and Playwright's classname: the test file


def _case_name(case, suites: list[str] | None = None) -> str:
    """pytest's tests.test_math.test_add, Go's example.com/calc.TestAdd, Rust's calc.tests::adds; a file or a describe
    title before a test's title the way JS runners show it (sum.test.js > sum > adds). A case with no classname (or
    node --test's constant "test") is named after its suites, such as the package of gotestsum's TestMain."""
    cls, name = case.get("classname") or "", case.get("name") or "?"
    if cls in ("", "test"):
        return " > ".join([*(suites or []), name])
    if name == cls or name.startswith(cls + " "):
        return name
    words = re.search(r"\s", cls) or re.search(r"\s", name.split("[", 1)[0])
    js = JS_FILE.search(cls) or JS_FILE.search(case.get("file") or "")      # node 24 names the file on each case
    return f"{cls} > {name}" if words or js else f"{cls}.{name}"


GENERIC_WHY = {"failed", "failure", "error", "build error"}         # gotestsum's and go-junit-report's messages
RUNNER_LINE = re.compile(r"(?:=== (?:RUN|PAUSE|CONT|NAME)\b|--- (?:FAIL|PASS|SKIP):|●\s)")    # Go's and jest's own lines
MORE_LINE = re.compile(r"(?:left|right|Expected|Received):")        # assert_eq!'s and expect()'s values, which follow


def _case_why(el) -> str:
    """Why a case failed, in a line: the message, unless it says nothing (Go's "Failed"), else the first line of the
    text that is not the runner's own; with the lines that finish it (after a colon) and the values it compared."""
    message = " ".join((el.get("message") or "").split())
    text = (el.text or "").strip()
    if text.startswith("{"):                                       # jest-junit: a suite's error as a JSON object
        try:
            data = json.loads(text)
            text = str(data.get("message") or data.get("_originalMessage") or text) if isinstance(data, dict) else text
        except (ValueError, RecursionError):                       # not JSON after all: the text as it is
            pass
    lines = [" ".join(ln.split()) for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not RUNNER_LINE.match(ln)]
    head = message if message.lower() not in GENERIC_WHY else ""
    at = next((i for i, ln in enumerate(lines) if head and head in ln), None) if head else (0 if lines else None)
    if at is not None:
        if not head or lines[at].startswith(head):
            head = lines[at]                                       # nextest: the message is its first line, cut short
        for ln in lines[at + 1:]:
            if len(head) >= 200 or not (head.endswith(":") or MORE_LINE.match(ln)):
                break
            head += " " + ln
    return (head or message or el.get("type") or "no message")[:200]


# ---- images and media -----------------------------------------------------------------------------------------

def image_diff(path: Path, golden: Path, since=None) -> tuple[float | None, str]:
    """Mean absolute difference per pixel and channel, 0..1, with both images in RGB at the golden's size; or None
    and why not."""
    try:
        from PIL import Image, ImageChops, ImageStat
    except ImportError:
        return None, "Pillow is not installed where the step ran, so image checks cannot run: pip install pillow"
    if not path.is_file():
        return None, "file missing"
    if not golden.is_file():
        return None, f"the known-good image {golden.name} is missing (paths are relative to the project folder)"
    stale = _stale(path, since)
    if stale:
        return None, stale
    try:
        with Image.open(path) as fa, Image.open(golden) as fb:
            a, b = fa.convert("RGB"), fb.convert("RGB")
    except Exception as exc:
        return None, f"cannot read the images: {exc}"
    note = ""
    if a.size != b.size:
        note = f", after resizing it from {a.width}x{a.height} to the golden's {b.width}x{b.height}"
        a = a.resize(b.size)
    means = ImageStat.Stat(ImageChops.difference(a, b)).mean
    return sum(means) / len(means) / 255, note


def probe_media(path: Path) -> dict:
    """frames, width, height, fps, seconds and audio for a video or image, via ffprobe."""
    if not path.is_file():
        return {"error": "file missing"}
    if not shutil.which("ffprobe"):
        return {"error": "ffprobe is not installed, so media checks cannot run"}
    cmd = ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
           "stream=codec_type,width,height,r_frame_rate,nb_read_frames:format=duration", "-of", "json", str(path)]
    try:
        data = json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=300).stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        return {"error": f"ffprobe failed: {exc}"}
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not video:
        return {"error": "no video or image stream"}
    return {"frames": int(video.get("nb_read_frames") or 0), "width": video.get("width"), "height": video.get("height"),
            "fps": video.get("r_frame_rate"), "seconds": float(data.get("format", {}).get("duration") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in streams)}
