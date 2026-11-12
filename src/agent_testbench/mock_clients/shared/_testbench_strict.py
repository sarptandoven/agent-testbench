"""StrictAPI, for mocks of APIs testbench has no mock for: what a mock plug-in's own code imports in the step
(docs/plugins.md). Standard library only, so it runs in whatever Python the step uses - from uvx or pipx that is the
project's, which may not have agent-testbench - and the folder it is in goes on every mocked step's PYTHONPATH;
agent_testbench.mocks hands out these same objects. Nothing here opens a network connection.

    TESTBENCH_MOCK_<NAME>   the step's options for the mock NAME, as JSON (step_options(name) reads them)
    TESTBENCH_MOCK_CALLS    where the call log goes; by default $TESTBENCH_STEP_DIR/mock_calls.jsonl, else
                            ./mock_calls.jsonl (the log the built-in mocks write, which `mock_calls:` rules count)
    TESTBENCH_MOCK_LOG      StrictAPI's own log, if set: the accepted calls as one JSON list
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG_NAME = "mock_calls.jsonl"


class UnexpectedCall(RuntimeError):
    """A call the mock refuses: one nobody planned for, or arguments the real service would refuse."""


class NotModelled(UnexpectedCall, AttributeError):
    """A method or name the mock does not have: refused with its name, never answered with a silent None."""


@dataclass
class Endpoint:
    required: dict = field(default_factory=dict)      # name -> type, or a tuple of allowed values
    optional: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)        # name -> (low, high), inclusive


def env_name(name: str) -> str:
    """The variable a mock's options reach the step in: TESTBENCH_MOCK_ANTHROPIC, TESTBENCH_MOCK_IB_ASYNC."""
    return "TESTBENCH_MOCK_" + re.sub(r"[^A-Za-z0-9]", "_", name).upper()


def step_options(name: str) -> dict:
    """In the step: the options testbench.yaml gives the mock `name` (its file options as absolute paths); {} when
    it has none, or outside testbench."""
    raw = os.environ.get(env_name(name), "").strip()
    try:
        opts = json.loads(raw) if raw else {}
    except ValueError:
        raise UnexpectedCall(f"mock {name}: {env_name(name)} is not JSON ({raw[:80]!r}); testbench sets it from the "
                             f"step's mocks: {{{name}: {{...}}}}") from None
    return opts if isinstance(opts, dict) else {}


def log_path() -> str:
    step = os.environ.get("TESTBENCH_STEP_DIR")
    return os.environ.get("TESTBENCH_MOCK_CALLS") or (os.path.join(step, LOG_NAME) if step else LOG_NAME)


def shrink(v, limit=2000):
    """Arguments as logged: long strings and lists are cut, so the log stays readable evidence."""
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + f"... ({len(v)} characters)"
    if isinstance(v, dict):
        return {str(k): shrink(x, limit) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [shrink(x, limit) for x in v[:100]] + ([f"... and {len(v) - 100} more"] if len(v) > 100 else [])
    if v is None or isinstance(v, (bool, int, float)):
        return v
    return repr(v)[:200]


class StrictAPI:
    """Accepts only the calls it has an Endpoint for, with arguments of the right names, types and ranges; refuses
    the rest with UnexpectedCall. sdk: the mock's name - with it, every call (refused ones too) is a line of the
    step's mock_calls.jsonl under that name, which `mock_calls:` rules count."""

    def __init__(self, endpoints: dict[str, Endpoint], log: str | None = None, needs_env: str | None = None,
                 sdk: str | None = None):
        self.endpoints = endpoints
        self.log = log or os.environ.get("TESTBENCH_MOCK_LOG")
        self.needs_env = needs_env
        self.sdk = sdk
        self.count = 0

    @property
    def options(self) -> dict:
        """The step's options for this mock (needs sdk)."""
        return step_options(self.sdk) if self.sdk else {}

    def check(self, endpoint: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        try:
            self._check(endpoint, arguments)
        except UnexpectedCall as exc:
            self._record(endpoint, arguments, exc)
            raise
        self._record(endpoint, arguments, None)
        if self.log:
            p = Path(self.log)
            calls = json.loads(p.read_text()) if p.exists() else []
            calls.append({"endpoint": endpoint, "arguments": arguments})
            p.write_text(json.dumps(calls, indent=1, default=str))
        return arguments

    def not_modelled(self, name: str, where: str = "") -> AttributeError:
        """For a client's __getattr__: the error to raise for a method the mock does not have, naming it, logged as
        a refused call. A private name (copy and pickle look for some) gets a plain AttributeError, not logged."""
        if name.startswith("_"):
            return AttributeError(name)
        exc = NotModelled(f"{self._who}: {where}{name} is not modelled; the mock answers "
                          f"{', '.join(sorted(self.endpoints))}. Anything else is refused, so that no test passes on a "
                          "call nothing checked.")
        exc.name = name
        self._record(f"{where}{name}", {}, exc)
        return exc

    @property
    def _who(self) -> str:
        return f"mock {self.sdk}" if self.sdk else "mock"

    def _check(self, endpoint: str, arguments: dict) -> None:
        who = self._who
        if endpoint not in self.endpoints:
            raise UnexpectedCall(f"{who}: refused a call to {endpoint!r}; only {sorted(self.endpoints)} are expected")
        if self.needs_env and not os.environ.get(self.needs_env):
            raise UnexpectedCall(f"{who}: {self.needs_env} is not set (the real client would refuse too)")
        spec = self.endpoints[endpoint]
        allowed = {**spec.required, **spec.optional}
        unknown = set(arguments) - set(allowed)
        if unknown:
            raise UnexpectedCall(f"{who}: {endpoint} does not accept {sorted(unknown)}")
        for name in spec.required:
            if name not in arguments:
                raise UnexpectedCall(f"{who}: {endpoint} requires {name!r}")
        for name, value in arguments.items():
            kind = allowed[name]
            if isinstance(kind, tuple) and value not in kind:
                raise UnexpectedCall(f"{who}: {endpoint} {name}={value!r} not in {kind}")
            if isinstance(kind, type) and not (isinstance(value, kind) and not (kind is int and isinstance(value, bool))):
                raise UnexpectedCall(f"{who}: {endpoint} {name} must be {kind.__name__}, got {type(value).__name__}")
            if name in spec.ranges:
                lo, hi = spec.ranges[name]
                if not lo <= value <= hi:
                    raise UnexpectedCall(f"{who}: {endpoint} {name}={value!r} outside {lo}..{hi}")

    def _record(self, endpoint: str, arguments: dict, exc: Exception | None) -> None:
        if not self.sdk:
            return
        self.count += 1
        rec = {"sdk": self.sdk, "endpoint": endpoint, "call": self.count, "t": round(time.time(), 3),
               "arguments": shrink(arguments), "error": None if exc is None else
               {"kind": "refused", "type": type(exc).__name__, "message": str(exc)}}
        with open(log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=repr) + "\n")
