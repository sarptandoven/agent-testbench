"""Plug-ins: the parts of testbench that another package can add to, each behind one small interface.

    # pyproject.toml of a package, e.g. testbench-trading
    [project.entry-points."agent_testbench.checks"]
    strategy = "testbench_trading.checks:strategy"

Kinds (`testbench plugins` lists what is installed for each):

    backends   where code runs                       agent_testbench.backends.Backend
    checks     what a step's `expect:` can check      agent_testbench.expect.Check
    mocks      stand-in SDKs a step's `mocks:` loads  agent_testbench.mocks.Mock
    sinks      where `export:` sends a run            agent_testbench.export.Sink
    notifiers  where `notify:` tells people           agent_testbench.notify.Notifier
    recipes    ready plan ladders (`init --recipe`)   agent_testbench.recipes.Recipe
    metrics    numbers read from a step's files      agent_testbench.metrics.Reader

Built-ins sit in the same table as plug-ins, so a plug-in can do anything a built-in does. A plug-in cannot replace
a built-in of the same name (it is skipped and reported), and every kind keeps its guardrails: a backend prices its
hardware and stops itself on the remote side; a sink or notifier never raises into a run or holds it up; a mock never
touches the network. `testbench plugin test` checks a plug-in against its kind's contract before anyone installs it.

This module is part of the core: it imports nothing but the standard library at module level. Each kind's module
(the right-hand column above) says what its objects look like (KIND) and how to check one (contract); this module
only finds, loads and hands them out.
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

GROUP = "agent_testbench.{kind}"

# kind -> the module that defines it (module-level KIND = plugins.Kind(...)). Imported only when that kind is used.
KINDS = {
    "backends": "agent_testbench.backends",
    "checks": "agent_testbench.expect",
    "mocks": "agent_testbench.mocks",
    "sinks": "agent_testbench.export",
    "notifiers": "agent_testbench.notify",
    "recipes": "agent_testbench.recipes",
    "metrics": "agent_testbench.metrics",
}


class PluginError(ValueError):
    """A plug-in that is missing, broken or does not keep its kind's contract. The message says which and what to do."""


@dataclass
class Kind:
    name: str                                   # "checks"
    one: str                                    # "check": a single one, for messages
    what: str                                   # one line for `testbench plugins`
    builtin: dict[str, str]                     # name -> "module:attr" of the built-ins, loaded lazily
    prepare: Callable[[Any, str], Any] = lambda obj, name: obj      # loaded object -> what the registry holds
    contract: Callable[[Any], list[str]] = lambda obj: []           # problems with one object ([] = keeps it)
    loaded: dict[str, Any] | None = field(default=None, repr=False)
    broken: dict[str, str] = field(default_factory=dict, repr=False)


def kind(name: str) -> Kind:
    if name not in KINDS:
        raise PluginError(f"no plug-in kind {name!r}; the kinds are {', '.join(KINDS)}")
    found = getattr(importlib.import_module(KINDS[name]), "KIND", None)
    if not isinstance(found, Kind):
        raise PluginError(f"{KINDS[name]} does not define the {name} plug-in kind (a module-level KIND = "
                          "plugins.Kind(...)); that kind cannot take plug-ins yet")
    return found


def _load_ref(ref: str):
    module, _, attr = ref.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split(".") if attr else ():
        obj = getattr(obj, part)
    return obj


def entry_points(kind_name: str) -> list:
    from importlib.metadata import entry_points as eps
    return list(eps(group=GROUP.format(kind=kind_name)))


def registry(kind_name: str) -> dict[str, Any]:
    """Every object of this kind that loaded: the built-ins, then installed plug-ins. A plug-in that fails to load,
    or tries to take a built-in's name, is left out and reported (`broken`, `testbench plugins`, `testbench doctor`),
    so it only breaks the projects that use it."""
    k = kind(kind_name)
    if k.loaded is None:
        found = {}
        for name, ref in k.builtin.items():
            found[name] = k.prepare(_load_ref(ref), name)
        for ep in entry_points(kind_name):
            if ep.name in found:
                k.broken[ep.name] = f"{ep.value} tries to replace a {k.one} that already exists; it was skipped"
                continue
            try:
                found[ep.name] = k.prepare(ep.load(), ep.name)
            except Exception as exc:
                k.broken[ep.name] = f"{ep.value} did not load: {type(exc).__name__}: {exc}"
        k.loaded = found
    return k.loaded


def register(kind_name: str, name: str, obj) -> Any:
    """Add one by hand (a test, or a notebook that defines one inline)."""
    k = kind(kind_name)
    found = registry(kind_name)
    found[name] = k.prepare(obj, name)
    return found[name]


def unregister(kind_name: str, name: str) -> None:
    registry(kind_name).pop(name, None)


def broken(kind_name: str) -> dict[str, str]:
    registry(kind_name)
    return dict(kind(kind_name).broken)


def names(kind_name: str) -> list[str]:
    return list(registry(kind_name))


def get(kind_name: str, name: str):
    found = registry(kind_name)
    if name in found:
        return found[name]
    k = kind(kind_name)
    if name in k.broken:
        raise PluginError(f"{k.one} {name!r} is installed but did not load ({k.broken[name]}); `testbench plugins` "
                          "shows it - reinstall or fix that package")
    raise PluginError(f"no {k.one} {name!r}; the {k.name} here are {', '.join(found) or 'none'}. A package can add one "
                      f"(entry point group {GROUP.format(kind=kind_name)}); `testbench plugins` lists what is installed")


def builtin(kind_name: str, name: str) -> bool:
    return name in kind(kind_name).builtin


def reset(kind_name: str | None = None) -> None:
    """Forget what was loaded (tests, or after installing a package in the same process). Kinds whose module was
    never imported have nothing to forget and are not imported for this."""
    for n in [kind_name] if kind_name else list(KINDS):
        k = getattr(sys.modules.get(KINDS[n]), "KIND", None)
        if isinstance(k, Kind):
            k.loaded, k.broken = None, {}


def check(kind_name: str, obj, name: str = "") -> list[str]:
    """The kind's contract for one object: a list of problems in plain words ([] = it keeps the contract)."""
    k = kind(kind_name)
    try:
        prepared = k.prepare(obj, name or getattr(obj, "name", "") or "plugin")
    except Exception as exc:
        return [f"it cannot be made into a {k.one}: {type(exc).__name__}: {exc}"]
    return list(k.contract(prepared))
