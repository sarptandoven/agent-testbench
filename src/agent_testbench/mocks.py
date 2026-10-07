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
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


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
