"""Strict stand-ins for paid APIs, so tests exercise everything around a paid call without making it.

A mock is a small module with the real client's name placed in a folder that a step lists under
`pythonpath:`. It should accept only the calls you expect, check their arguments against the service's
published schema, return a reply of the real shape, and refuse anything else - so a test fails loudly the
moment code tries a paid call nobody planned for.

    # mocks/acme_client.py
    from agent_testbench.mocks import Endpoint, StrictAPI
    api = StrictAPI({"acme/text-to-video": Endpoint(required={"prompt": str},
                                                     optional={"duration": int, "resolution": ("720p", "1080p")},
                                                     ranges={"duration": (5, 15)})})

    def subscribe(endpoint, arguments):
        api.check(endpoint, arguments)                 # raises on an unknown endpoint or a bad argument
        return {"video": {"url": make_test_video(arguments["duration"])}}

Every accepted call is appended to $TESTBENCH_MOCK_LOG (a JSON list), so expectations can count them.

For the two most used LLM SDKs there is nothing to write: `mocks: [openai, anthropic]` on a step puts the mock
`openai` and `anthropic` packages of mock_clients/ first on its PYTHONPATH (docs/mocks.md). The same goes for the
broker SDKs trading code uses, `mocks: [alpaca]`, `[ccxt]` or `[ib_async]` (which also answers `import ib_insync`):
paper accounts answered from a prices file, and a live account refused (docs/mocks.md#brokers).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .expect import BROKER_SDKS, MOCK_SDKS

SDKS = MOCK_SDKS
BROKERS = BROKER_SDKS
CLIENTS = Path(__file__).parent / "mock_clients"
_core = _broker = None


class UnexpectedCall(RuntimeError):
    pass


@dataclass
class Endpoint:
    required: dict = field(default_factory=dict)      # name -> type, or a tuple of allowed values
    optional: dict = field(default_factory=dict)
    ranges: dict = field(default_factory=dict)        # name -> (low, high), inclusive


class StrictAPI:
    def __init__(self, endpoints: dict[str, Endpoint], log: str | None = None, needs_env: str | None = None):
        self.endpoints = endpoints
        self.log = log or os.environ.get("TESTBENCH_MOCK_LOG")
        self.needs_env = needs_env

    def check(self, endpoint: str, arguments: dict | None = None) -> dict:
        arguments = arguments or {}
        if endpoint not in self.endpoints:
            raise UnexpectedCall(f"mock: refused a call to {endpoint!r}; only {sorted(self.endpoints)} are expected")
        if self.needs_env and not os.environ.get(self.needs_env):
            raise UnexpectedCall(f"mock: {self.needs_env} is not set (the real client would refuse too)")
        spec = self.endpoints[endpoint]
        allowed = {**spec.required, **spec.optional}
        unknown = set(arguments) - set(allowed)
        if unknown:
            raise UnexpectedCall(f"mock: {endpoint} does not accept {sorted(unknown)}")
        for name in spec.required:
            if name not in arguments:
                raise UnexpectedCall(f"mock: {endpoint} requires {name!r}")
        for name, value in arguments.items():
            kind = allowed[name]
            if isinstance(kind, tuple) and value not in kind:
                raise UnexpectedCall(f"mock: {endpoint} {name}={value!r} not in {kind}")
            if isinstance(kind, type) and not (isinstance(value, kind) and not (kind is int and isinstance(value, bool))):
                raise UnexpectedCall(f"mock: {endpoint} {name} must be {kind.__name__}, got {type(value).__name__}")
            if name in spec.ranges:
                lo, hi = spec.ranges[name]
                if not lo <= value <= hi:
                    raise UnexpectedCall(f"mock: {endpoint} {name}={value!r} outside {lo}..{hi}")
        if self.log:
            p = Path(self.log)
            calls = json.loads(p.read_text()) if p.exists() else []
            calls.append({"endpoint": endpoint, "arguments": arguments})
            p.write_text(json.dumps(calls, indent=1, default=str))
        return arguments


# ---- the drop-in mock clients (docs/mocks.md) ------------------------------------------------------------------

def client_core():
    """The mock clients' shared module, loaded from its file (it is not one of this package's modules), so
    testbench.yaml is checked by the same rules the mocks apply."""
    global _core
    if _core is None:
        spec = importlib.util.spec_from_file_location("_testbench_mock", CLIENTS / "shared" / "_testbench_mock.py")
        _core = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_core)
    return _core


def broker_core():
    """The mock brokers' shared module, loaded from its file like client_core() (it imports that one, which is
    lent to it under its own name only while it loads)."""
    global _broker
    if _broker is None:
        saved = sys.modules.get("_testbench_mock")
        sys.modules["_testbench_mock"] = client_core()
        try:
            path = CLIENTS / "shared" / "_testbench_broker.py"
            spec = importlib.util.spec_from_file_location("_testbench_broker", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            if saved is None:
                sys.modules.pop("_testbench_mock", None)
            else:
                sys.modules["_testbench_mock"] = saved
        _broker = module
    return _broker


def client_paths(names) -> list[str]:
    """Put first on PYTHONPATH, these make `import openai` / `import anthropic` / `import alpaca` load the mocks."""
    return [str(CLIENTS / n) for n in names] + [str(CLIENTS / "shared")]


def client_env(mocks: dict, workdir: Path) -> dict:
    """What the mocks read: their options as JSON, with the replies and prices files' paths made absolute."""
    env = {}
    for name, opts in mocks.items():
        opts = dict(opts or {})
        for key in ("replies", "prices"):
            if opts.get(key):
                opts[key] = str((Path(workdir) / opts[key]).resolve())
        env[f"TESTBENCH_MOCK_{name.upper()}"] = json.dumps(opts)
    return env
