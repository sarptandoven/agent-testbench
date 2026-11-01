"""What the mock `anthropic` and `openai` packages share. agent-testbench puts their folders first on a step's
PYTHONPATH (`mocks: [anthropic, openai]` in testbench.yaml; docs/mocks.md). Standard library only, so it runs in
whatever Python the step uses; nothing here opens a network connection.

    TESTBENCH_MOCK_ANTHROPIC / TESTBENCH_MOCK_OPENAI   JSON options: {"replies": "/abs/replies.yaml",
                                                       "fail_on_call": [3], "fail_with": "rate_limit"}
    TESTBENCH_MOCK_CALLS                               where the call log goes; by default
                                                       $TESTBENCH_STEP_DIR/mock_calls.jsonl, else ./mock_calls.jsonl
"""
from __future__ import annotations

import copy
import difflib
import hashlib
import importlib.abc
import importlib.machinery
import json
import math
import os
import random
import re
import sys
import time
import types
from functools import lru_cache

LOG_NAME = "mock_calls.jsonl"
OPTION_KEYS = ("replies", "fail_on_call", "fail_with")
FAILURES = ("rate_limit", "overloaded", "server", "timeout", "connection", "auth", "permission", "not_found",
            "bad_request")
RETRYABLE = {"rate_limit", "overloaded", "server", "timeout", "connection"}      # what the SDKs retry themselves
RULE_KEYS = ("match", "text", "json", "tool", "input", "error")


class SetupError(RuntimeError):
    """The mock's own settings or replies file are wrong (not something the real API would say)."""


class Refusal(Exception):
    """A request the real API (or the SDK, when argument=True) would refuse."""

    def __init__(self, message, param=None, kind="bad_request"):
        super().__init__(message)
        self.param, self.kind = param, kind


class Injected(Exception):
    def __init__(self, kind, why):
        super().__init__(why)
        self.kind = kind


class NotGiven:
    def __bool__(self):
        return False

    def __repr__(self):
        return "NOT_GIVEN"


class Omit:
    def __bool__(self):
        return False

    def __repr__(self):
        return "omit"


NOT_GIVEN = not_given = NotGiven()
omit = Omit()


class Obj:
    """A response object: attribute access like the SDKs' models (pydantic models), with their to_dict() / to_json()
    and pydantic's model_dump() / model_dump_json() / model_validate() / model_validate_json() / model_copy().

    A model class says what model_validate needs: _required (the fields the real model requires), _optional (the
    ones it fills with None when they are not given) and _nested ({field: class}, or {field: {"type" value: class}}
    for a list of blocks of several kinds), which turn nested mappings into objects as the real models do; others
    stay mappings, as a tool call's `input` does. Types are not checked beyond that."""
    _required: tuple = ()
    _optional: tuple = ()
    _nested: dict = {}

    def __init__(self, **fields):
        self.__dict__.update(fields)
        for k in self._optional:                         # None where the real model has a default
            self.__dict__.setdefault(k, None)

    def to_dict(self, *, mode="python", use_api_names=True, exclude_unset=True, exclude_defaults=False,
                exclude_none=False, warnings=True):
        return plain(self, exclude_none)

    def to_json(self, *, indent=2, use_api_names=True, exclude_unset=True, exclude_defaults=False, exclude_none=False,
                warnings=True):
        return json.dumps(plain(self, exclude_none), indent=indent)

    def model_dump(self, *, mode="python", include=None, exclude=None, context=None, by_alias=None,
                   exclude_unset=False, exclude_defaults=False, exclude_none=False, exclude_computed_fields=False,
                   round_trip=False, warnings=True, fallback=None, serialize_as_any=False,
                   polymorphic_serialization=None):
        return _pick(plain(self, exclude_none), include, exclude)

    def model_dump_json(self, *, indent=None, ensure_ascii=False, include=None, exclude=None, context=None,
                        by_alias=None, exclude_unset=False, exclude_defaults=False, exclude_none=False,
                        exclude_computed_fields=False, round_trip=False, warnings=True, fallback=None,
                        serialize_as_any=False, polymorphic_serialization=None):
        return json.dumps(_pick(plain(self, exclude_none), include, exclude), indent=indent, ensure_ascii=ensure_ascii)

    @classmethod
    def model_validate(cls, obj, *, strict=None, extra=None, from_attributes=None, context=None, by_alias=None,
                       by_name=None):
        if isinstance(obj, cls):
            return obj
        data = plain(obj) if isinstance(obj, Obj) else obj
        if not isinstance(data, dict):
            raise validation_error(cls.__name__, [{"type": "model_type", "loc": (), "input": obj,
                                                   "ctx": {"class_name": cls.__name__}}])
        missing = [k for k in cls._required if k not in data]
        if missing:
            raise validation_error(cls.__name__, [{"type": "missing", "loc": (k,), "input": data} for k in missing])
        return cls(**{k: _nest(cls._nested[k], v) if k in cls._nested else v for k, v in data.items()})

    @classmethod
    def model_validate_json(cls, json_data, *, strict=None, extra=None, context=None, by_alias=None, by_name=None):
        try:
            data = json.loads(json_data)
        except ValueError as exc:
            raise validation_error(cls.__name__, [{"type": "json_invalid", "loc": (), "input": json_data,
                                                   "ctx": {"error": str(exc)}}]) from None
        return cls.model_validate(data)

    @classmethod
    def model_construct(cls, _fields_set=None, **values):
        return cls(**values)

    construct = model_construct

    def model_copy(self, *, update=None, deep=False):
        new = type(self).__new__(type(self))
        new.__dict__.update(copy.deepcopy(vars(self)) if deep else vars(self))
        new.__dict__.update(update or {})
        return new

    def __eq__(self, other):
        return type(other) is type(self) and plain(other) == plain(self)

    __hash__ = None

    def __repr__(self):
        return f"{type(self).__name__}({', '.join(f'{k}={v!r}' for k, v in vars(self).items() if k[0] != '_')})"


def _nest(kind, v):
    """A nested mapping (or each one in a list) as the object the real model makes of it."""
    if isinstance(v, (list, tuple)):
        return [_nest(kind, x) for x in v]
    if not isinstance(v, (dict, Obj)):
        return v
    if isinstance(kind, dict):                           # blocks of several kinds, told apart by their "type"
        t = v.get("type") if isinstance(v, dict) else getattr(v, "type", None)
        kind = kind.get(t, Obj)
    return kind.model_validate(v)


def _pick(d, include, exclude):
    """model_dump's include= and exclude=: a set of field names, or a mapping of names to True or to the same for
    that field (for a list field, for each of its items; "__all__" too)."""
    def spec(s):
        return {k: True for k in s} if isinstance(s, (set, frozenset, list, tuple)) else s

    def apply(v, inc, exc):
        if isinstance(v, list):
            return [apply(x, (inc or {}).get("__all__", inc) if isinstance(inc, dict) else inc,
                          (exc or {}).get("__all__", exc) if isinstance(exc, dict) else exc) for x in v]
        if not isinstance(v, dict):
            return v
        inc, exc = spec(inc), spec(exc)
        out = {}
        for k, x in v.items():
            if inc is not None and inc is not True and k not in inc:
                continue
            if isinstance(exc, dict) and exc.get(k) is True:
                continue
            sub_inc = inc.get(k) if isinstance(inc, dict) else None
            sub_exc = exc.get(k) if isinstance(exc, dict) else None
            out[k] = apply(x, None if sub_inc is True else sub_inc, None if sub_exc is True else sub_exc)
        return out
    return apply(d, include, exclude)


def validation_error(title: str, errors: list) -> Exception:
    """pydantic's own ValidationError where pydantic is installed (code that catches it works as with the real
    SDK), else a ValueError that reads the same."""
    try:
        from pydantic_core import ValidationError
        return ValidationError.from_exception_data(title, errors)
    except Exception:
        what = {"missing": "Field required", "model_type": f"Input should be a valid dictionary or instance of {title}",
                "json_invalid": "Invalid JSON"}
        lines = [(".".join(map(str, e["loc"])) + "\n  " if e["loc"] else "  ") + what.get(e["type"], e["type"])
                 + f" [type={e['type']}]" for e in errors]
        return MockValidationError(f"{len(errors)} validation error{'s' * (len(errors) > 1)} for {title}\n"
                                   + "\n".join(lines))


class MockValidationError(ValueError):
    """model_validate's error where pydantic is not installed."""


class _Absent(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """After the mock package's own files: a request-type module (`*_param`, `*_params`) is made on the fly, every
    name in it a dict, as the real TypedDicts are; any other module the mock lacks is refused saying so, instead of
    a bare "No module named"."""

    def __init__(self, sdk: str, has: str):
        self.sdk, self.has = sdk, has
        self.testbench_mock_sdk = sdk

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(self.sdk + ".") or not getattr(sys.modules.get(self.sdk), "TESTBENCH_MOCK", False):
            return None                                  # not the mock's, or the real package is the one loaded
        if re.search(r"_params?$", fullname):
            return importlib.machinery.ModuleSpec(fullname, self)
        raise ModuleNotFoundError(f"No module named {fullname!r}: the mock {self.sdk} package (agent-testbench) does "
                                  f"not have it. It has {self.has}; every *_param module, its names plain dicts. "
                                  "docs/mocks.md says what the mocks cover.", name=fullname)

    def create_module(self, spec):
        mod = types.ModuleType(spec.name, f"Request types of the mock {self.sdk}: plain dicts.")

        def __getattr__(name):
            if name.startswith("_"):
                raise AttributeError(name)
            return dict
        mod.__getattr__ = __getattr__
        return mod

    def exec_module(self, module):
        pass


def absent_modules(sdk: str, has: str) -> None:
    """Install _Absent for `sdk` once, last on sys.meta_path (after the finders that find the mock's files)."""
    if not any(getattr(f, "testbench_mock_sdk", None) == sdk for f in sys.meta_path):     # also one from a reload
        sys.meta_path.append(_Absent(sdk, has))


def plain(v, drop_none=False):
    """Response objects and tuples as plain dicts and lists (what a request holding them sends)."""
    if isinstance(v, Obj):
        v = {k: x for k, x in vars(v).items() if k[0] != "_"}
    if isinstance(v, dict):
        return {k: plain(x, drop_none) for k, x in v.items() if not (drop_none and x is None)}
    if isinstance(v, (list, tuple)):
        return [plain(x, drop_none) for x in v]
    return v


def given(kwargs: dict) -> dict:
    return {k: v for k, v in kwargs.items() if not isinstance(v, (NotGiven, Omit))}


# ---- strict checks --------------------------------------------------------------------------------------------

def kind(v) -> str:
    names = {dict: "a mapping", list: "a list", tuple: "a list", str: "a string", int: "an integer", float: "a number",
             bool: "a boolean", type(None): "null"}
    return names.get(type(v), "a mapping" if isinstance(v, Obj) else type(v).__name__)


def is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def refuse(message, param=None):
    raise Refusal(message, param)


def need(ok, at, what, value):
    if not ok:
        refuse(f"{at} must be {what}, got {kind(value)} {shrink(value, 80)!r}", at)


def number(at, v, lo=None, hi=None, integer=False):
    need(is_int(v) if integer else is_num(v), at, "an integer" if integer else "a number", v)
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        span = f"from {lo} to {hi}" if lo is not None and hi is not None else f"at least {lo}" if lo is not None \
            else f"at most {hi}"
        refuse(f"{at} must be {span}, got {v}", at)


def choice(at, v, options):
    if v not in options:
        refuse(f"{at} is {v!r}; it takes {', '.join(map(repr, options))}", at)


def keys(at, d: dict, allowed, hints=None):
    extra = [k for k in d if k not in allowed]
    if extra:
        hint = next((hints[k] for k in extra if hints and k in hints), "")
        refuse(f"{at} has {', '.join(map(repr, extra))}, which the API does not accept{hint}; it takes "
               f"{', '.join(allowed)}", f"{at}.{extra[0]}")


def as_list(at, v, empty_ok=False) -> list:
    if isinstance(v, (str, bytes, dict)) or not hasattr(v, "__iter__"):
        refuse(f"{at} must be a list, got {kind(v)}", at)
    v = list(v)
    if not v and not empty_ok:
        refuse(f"{at} is an empty list", at)
    return v


def arguments(endpoint, kwargs, allowed, required, hints=None):
    """Keyword arguments as the SDK's method takes them: an unknown or a missing one is the TypeError the real
    method raises (the mock's error is also the SDK's BadRequestError)."""
    for name in kwargs:
        if name not in allowed:
            hint = (hints or {}).get(name) or next((f" (did you mean {m!r}?)" for m in difflib.get_close_matches(
                name, allowed, 1, 0.75)), "")
            raise Refusal(f"{endpoint}() got an unexpected keyword argument {name!r}{hint}; it takes "
                          f"{', '.join(sorted(allowed))}", name, "argument")
    missing = [r for r in required if r not in kwargs]
    if missing:
        raise Refusal(f"{endpoint}() is missing {', '.join(missing)}; it requires {', '.join(required)}", missing[0],
                      "argument")


# ---- options, replies, answers --------------------------------------------------------------------------------

def options_error(opts) -> str | None:
    """Why mock options (the step's `mocks: {openai: {...}}`) are wrong, or None. testbench.yaml is checked with it too."""
    if not isinstance(opts, dict):
        return "options are a mapping such as {replies: tests/replies.yaml, fail_on_call: 3}"
    extra = [k for k in opts if k not in OPTION_KEYS]
    if extra:
        return f"has no {extra[0]!r}; the options are {', '.join(OPTION_KEYS)}"
    if "replies" in opts and not isinstance(opts["replies"], str):
        return "replies is the path of a replies file (YAML or JSON), e.g. tests/replies.yaml"
    fail = opts.get("fail_on_call")
    if fail is not None and not ((is_int(fail) and fail >= 1) or (
            isinstance(fail, list) and fail and all(is_int(n) and n >= 1 for n in fail))):
        return "fail_on_call is a call number such as 3, or a list such as [3, 4]; calls count from 1"
    if "fail_with" in opts:
        if opts["fail_with"] not in FAILURES:
            return f"fail_with is {opts['fail_with']!r}; use one of {', '.join(FAILURES)}"
        if fail is None:
            return "fail_with says how the calls in fail_on_call fail; set fail_on_call too"
    return None


def parse_replies(data) -> list[dict]:
    """A replies file: a list of rules {match, text | json | tool (+ input) | error}, or a mapping of pattern ->
    reply text (or -> {text | json | tool + input | error}). Raises ValueError saying what to write instead."""
    if isinstance(data, dict):
        data = [{"match": k, **(v if isinstance(v, dict) else {"text": v})} for k, v in data.items()]
    if not isinstance(data, list):
        raise ValueError('a replies file is a list of rules such as [{match: "weather", text: "Sunny."}] '
                         'or a mapping such as {"weather": "Sunny."}')
    rules = []
    for i, r in enumerate(data, 1):
        if not isinstance(r, dict) or not isinstance(r.get("match"), str):
            raise ValueError(f"rule {i} needs match:, a regular expression searched in the last user message")
        at = f"rule {i} ({r['match']!r})"
        extra = [k for k in r if k not in RULE_KEYS]
        if extra:
            raise ValueError(f"{at} has {extra[0]!r}; a rule takes {', '.join(RULE_KEYS)}")
        try:
            pattern = re.compile(r["match"])
        except re.error as exc:
            raise ValueError(f"{at}: not a regular expression ({exc})") from None
        if not any(k in r for k in ("text", "json", "tool", "error")):
            raise ValueError(f"{at} needs text:, json:, tool: (with input:) or error:")
        if "text" in r and not isinstance(r["text"], str):
            raise ValueError(f"{at}: text: is the reply text, a string (quote it in YAML)")
        if "text" in r and "json" in r:
            raise ValueError(f"{at}: give text: or json:, not both (json: is a value written out as the reply text)")
        if "tool" in r and not (isinstance(r["tool"], str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", r["tool"])):
            raise ValueError(f"{at}: tool: is the name of the tool the model calls, e.g. get_weather")
        if "input" in r and ("tool" not in r or not isinstance(r["input"], dict)):
            raise ValueError(f"{at}: input: is a mapping of the tool call's arguments, next to tool:")
        if "error" in r and (r["error"] not in FAILURES or len(r) > 2):
            raise ValueError(f"{at}: error: stands alone and is one of {', '.join(FAILURES)}")
        rules.append({**r, "pattern": pattern, "number": i})
    return rules


def load_replies(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    try:
        data = json.loads(text)
    except ValueError:
        try:
            import yaml
        except ImportError:
            raise SetupError(f"the replies file {path} is YAML, but PyYAML is not installed where the step runs: "
                             "pip install pyyaml, or write the file as JSON") from None
        data = yaml.safe_load(text)
    try:
        return parse_replies(data)
    except ValueError as exc:
        raise SetupError(f"the replies file {path}: {exc}") from None


def skeleton(schema, root=None, depth=0):
    """The smallest value of a JSON schema's shape: what a structured-output reply holds unless the replies say."""
    root = root if root is not None else schema
    if not isinstance(schema, dict) or depth > 20:
        return None
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        node = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return skeleton(node, root, depth + 1)
    for key in ("const", "default"):
        if key in schema:
            return schema[key]
    if schema.get("enum"):
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            return skeleton(schema[key][0], root, depth + 1)
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), "null")
    if t == "object" or "properties" in schema:
        return {k: skeleton(v, root, depth + 1) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return [skeleton(schema.get("items") or {}, root, depth + 1) for _ in range(schema.get("minItems") or 0)]
    return {"string": "mock", "integer": int(schema.get("minimum", 0)), "number": float(schema.get("minimum", 0)),
            "boolean": False}.get(t)


def default_text(last: str) -> str:
    snippet = " ".join(last.split())[:60]
    return f"mock reply to: {snippet}" if snippet else "mock reply"


def tokens(text) -> int:
    """The mock's token count: words x 1.3, rounded up. Real tokenizers differ; this one only has to be stable."""
    return math.ceil(len(str(text or "").split()) * 1.3)


def cut(text: str, max_tokens=None, stops=()):
    """The reply ended as the model would: at the first stop sequence, or after max_tokens tokens.
    -> (text, "end_turn" | "stop_sequence" | "max_tokens", the stop sequence)"""
    hits = sorted((text.find(s), s) for s in stops or () if s and s in text)
    if hits:
        return text[:hits[0][0]], "stop_sequence", hits[0][1]
    if max_tokens is not None and tokens(text) > max_tokens:
        return " ".join(text.split()[:int(max_tokens / 1.3)]), "max_tokens", None
    return text, "end_turn", None


def pieces(text: str) -> list[str]:
    """Streamed text: a word (with the space after it) per chunk."""
    return re.findall(r"\s*\S+\s*", text) or ([text] if text else [])


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=repr, sort_keys=True).encode()).hexdigest()[:20]


@lru_cache(maxsize=8192)
def _word_vector(word: str, dims: int) -> tuple:
    r = random.Random(int.from_bytes(hashlib.sha256(word.encode()).digest()[:8], "big"))
    return tuple(r.gauss(0, 1) for _ in range(dims))


def embed(text: str, dims: int) -> list[float]:
    """A unit vector made from the text's words: the same text always gets the same vector, and texts that share
    words point the same way (a stand-in for similarity, not a semantic model)."""
    v = [0.0] * dims
    for word in re.findall(r"\w+", text.lower()) or [text]:
        for i, x in enumerate(_word_vector(word, dims)):
            v[i] += x
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / norm, 6) for x in v]


class Mock:
    """One SDK's mock in this process: its options, its replies and its call counter."""

    def __init__(self, sdk: str):
        self.sdk, self.count, self._opts, self._rules = sdk, 0, None, None

    @property
    def opts(self) -> dict:
        if self._opts is None:
            name = f"TESTBENCH_MOCK_{self.sdk.upper()}"
            raw = os.environ.get(name, "").strip() or "{}"
            try:
                opts = json.loads(raw)
            except ValueError:
                raise SetupError(f"mock {self.sdk}: {name} is not JSON ({raw[:80]!r}); it holds options such as "
                                 '{"replies": "tests/replies.yaml", "fail_on_call": 3}') from None
            bad = options_error(opts)
            if bad:
                raise SetupError(f"mock {self.sdk}: {name} {bad}")
            self._opts = opts
        return self._opts

    def rules(self) -> list[dict]:
        if self._rules is None:
            path = self.opts.get("replies")
            self._rules = load_replies(path) if path else []
        return self._rules

    def failure(self, n: int) -> str | None:
        fail = self.opts.get("fail_on_call")
        hit = n in fail if isinstance(fail, list) else n == fail
        return self.opts.get("fail_with", "rate_limit") if hit else None

    def reply(self, last: str, *, tools: dict, forced=None, structured=None) -> dict:
        """What the model answers: {"text": str | None, "tool_calls": [{"name", "input"}], "rule": n | None}.
        tools: the tools this request lets the model call, name -> input JSON schema. forced: a tool name, or
        "any", when the request makes the model call one. structured: a JSON schema the reply must fit, or True
        for any JSON object."""
        for rule in self.rules():
            m = rule["pattern"].search(last)
            if not m:
                continue
            if "error" in rule:
                raise Injected(rule["error"], f"replies rule {rule['number']} ({rule['match']!r}) answers with "
                                              f"error {rule['error']}")
            text = _expand(m, rule["text"]) if "text" in rule else json.dumps(rule["json"]) if "json" in rule else None
            out = {"text": text, "tool_calls": [], "rule": rule["number"]}
            if "tool" in rule:
                name = rule["tool"]
                if name not in tools:
                    raise SetupError(f"mock {self.sdk}: replies rule {rule['number']} ({rule['match']!r}) calls the "
                                     f"tool {name!r}, but this request offers " + (
                                         f"only {', '.join(tools)}" if tools else "no tools") +
                                     ". Pass that tool in tools=, or fix the replies file.")
                args = {k: _expand(m, v) if isinstance(v, str) else v for k, v in (rule.get("input") or {}).items()}
                missing = [k for k in (tools[name] or {}).get("required") or [] if k not in args]
                if missing:
                    raise SetupError(f"mock {self.sdk}: replies rule {rule['number']} calls {name!r} without "
                                     f"{', '.join(missing)}, which the tool's schema requires; add them under input:")
                out["tool_calls"] = [{"name": name, "input": args}]
            return out
        if forced and tools:
            name = forced if forced in tools else next(iter(tools))
            return {"text": None, "tool_calls": [{"name": name, "input": skeleton(tools[name]) or {}}], "rule": None}
        if structured is True:
            return {"text": json.dumps({"reply": default_text(last)}), "tool_calls": [], "rule": None}
        text = json.dumps(skeleton(structured)) if isinstance(structured, dict) else default_text(last)
        return {"text": text, "tool_calls": [], "rule": None}


def _expand(m, template: str) -> str:
    try:
        return m.expand(template)                       # \1 and \g<name> take the matched groups
    except (re.error, IndexError):
        return template


# ---- one call, logged -----------------------------------------------------------------------------------------

def log_path() -> str:
    step = os.environ.get("TESTBENCH_STEP_DIR")
    return os.environ.get("TESTBENCH_MOCK_CALLS") or (os.path.join(step, LOG_NAME) if step else LOG_NAME)


def shrink(v, limit=2000):
    """A request or reply as logged: long strings and lists are cut, so the log stays readable evidence."""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"... ({len(v)} characters)"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    if isinstance(v, (Obj, dict)):
        return {str(k): shrink(x, limit) for k, x in plain(v).items()}
    if isinstance(v, (list, tuple)):
        return [shrink(x, limit) for x in v[:100]] + ([f"... and {len(v) - 100} more"] if len(v) > 100 else [])
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return repr(v)[:200]


def write(rec: dict) -> None:
    with open(log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, default=repr) + "\n")


def call(mock: Mock, endpoint: str, kwargs: dict, *, retries: int, check, answer, error):
    """One SDK method call, as the real client makes it: check the request (a refusal is not retried), fail it when
    the options say so (retried as the SDK retries: up to `retries` more attempts, each a call of its own), else
    answer it. Every attempt is one line of the log. check(kwargs) -> request; answer(request, n) -> (result, info);
    error(kind, message, param, n) -> the SDK's exception."""
    attempt = 0
    while True:
        mock.count += 1
        n = mock.count
        rec = {"sdk": mock.sdk, "endpoint": endpoint, "call": n, "t": round(time.time(), 3),
               "model": kwargs.get("model") if isinstance(kwargs.get("model"), str) else None,
               "arguments": shrink({k: v for k, v in kwargs.items() if k not in ("extra_headers", "timeout")}),
               "error": None}
        try:
            req = check(kwargs)
            rec.update(known_model=req.get("known_model"), notes=req.get("notes") or [])
            failure = mock.failure(n)
            if failure:
                raise Injected(failure, f"fail_on_call: call {n} fails with {failure}")
            result, info = answer(req, n)
        except Refusal as exc:
            err = error(exc.kind, str(exc), exc.param, n)
            rec["error"] = {"kind": "refused", "type": type(err).__name__, "message": str(exc), "param": exc.param}
            write(rec)
            raise err from None
        except SetupError as exc:
            rec["error"] = {"kind": "refused", "type": "SetupError", "message": str(exc)}
            write(rec)
            raise
        except Injected as exc:
            err = error(exc.kind, f"mock {mock.sdk}: {exc}", None, n)
            again = exc.kind in RETRYABLE and attempt < retries
            rec["error"] = {"kind": "injected", "type": type(err).__name__, "failure": exc.kind, "message": str(exc),
                            "retried": again}
            write(rec)
            if again:
                attempt += 1
                continue
            raise err from None
        rec.update(info)
        write(rec)
        return result


class FakeRequest:
    def __init__(self, url: str):
        self.method, self.url, self.headers, self.content = "POST", url, {}, b""

    def __repr__(self):
        return f"<Request('POST', '{self.url}')>"


class Headers(dict):
    """Response headers: looked up without regard to case, like httpx's."""

    def __init__(self, items=()):
        super().__init__((str(k).lower(), v) for k, v in dict(items).items())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __getitem__(self, key):
        return super().__getitem__(str(key).lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())


class FakeResponse:
    def __init__(self, status: int, body, request: FakeRequest, headers: dict):
        self.status_code, self.request, self.headers, self._body = status, request, Headers(headers), body
        self.text = json.dumps(body)
        self.is_success = False

    def json(self):
        return self._body

    def __repr__(self):
        return f"<Response [{self.status_code}]>"


class Stream:
    """What create(stream=True) returns: an iterator of events that is also a context manager."""

    def __init__(self, events, response=None):
        self._it, self.response = iter(events), response

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._it)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._it = iter(())


class AsyncStream:
    def __init__(self, events, response=None):
        self._it, self.response = iter(events), response

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        self._it = iter(())
