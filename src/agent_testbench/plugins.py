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
finds, loads and hands them out, says where each came from (`inventory`), and runs the conformance kit
(`conformance` for `testbench plugin test`, `assert_conforms` for a plug-in's own tests).
"""
from __future__ import annotations

import copy
import importlib
import os
import sys
import types
from dataclasses import dataclass, field
from typing import Any, Callable

GROUP = "agent_testbench.{kind}"
PREFIX = "agent_testbench."                      # every kind's entry point group starts with this

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
    origin: dict[str, dict] = field(default_factory=dict, repr=False)   # name -> {from, package} of an installed one


def kind(name: str) -> Kind:
    if name not in KINDS:
        raise PluginError(f"no plug-in kind {name!r}; the kinds are {', '.join(KINDS)}")
    try:
        module = importlib.import_module(KINDS[name])
    except ModuleNotFoundError as exc:
        if exc.name != KINDS[name]:
            raise
        raise PluginError(f"{KINDS[name]} is not in this version of testbench, so the {name} kind cannot take "
                          "plug-ins yet") from None
    found = getattr(module, "KIND", None)
    if not isinstance(found, Kind):
        raise PluginError(f"{KINDS[name]} does not define the {name} plug-in kind (a module-level KIND = "
                          "plugins.Kind(...)); that kind cannot take plug-ins yet")
    return found


def _load_ref(ref: str):
    module, _, attr = ref.partition(":")
    obj = importlib.import_module(module.strip())
    for part in attr.strip().split(".") if attr.strip() else ():
        obj = getattr(obj, part)
    return obj


def entry_points(kind_name: str) -> list:
    from importlib.metadata import entry_points as eps
    return list(eps(group=GROUP.format(kind=kind_name)))


def package_of(ep) -> str:
    """'fakebench 0.1': the installed package an entry point came from ('' when that is not known)."""
    dist = getattr(ep, "dist", None)
    try:
        return f"{dist.metadata['Name']} {dist.version}" if dist is not None else ""
    except Exception:
        return ""


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
            k.origin[ep.name] = {"from": ep.value, "package": package_of(ep)}
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
            k.loaded, k.broken, k.origin = None, {}, {}


def _spare(obj):
    """A copy to check, so checking never changes the object itself (prepare may give it a name)."""
    if isinstance(obj, (type, types.ModuleType)):
        return obj
    try:
        return copy.copy(obj)
    except Exception:
        return obj


def check(kind_name: str, obj, name: str = "") -> list[str]:
    """The kind's contract for one object: a list of problems in plain words ([] = it keeps the contract). Given
    `name` (its entry point's), an object that calls itself something else is a problem too. The object itself is
    not changed."""
    k = kind(kind_name)
    try:
        prepared = k.prepare(_spare(obj), name or getattr(obj, "name", "") or "plugin")
    except Exception as exc:
        return [f"it cannot be made into a {k.one}: {type(exc).__name__}: {exc}"]
    problems = []
    own = getattr(prepared, "name", None)
    if name and isinstance(own, str) and own and own != name:
        problems.append(f"it calls itself {own!r} but is installed as {name!r}: testbench.yaml would name it {name!r} "
                        f"and its messages {own!r}; make them the same")
    try:
        problems += list(k.contract(prepared))
    except Exception as exc:
        problems.append(f"checking its contract raised {type(exc).__name__}: {exc}")
    return problems


def assert_conforms(kind_name: str, obj, name: str = "") -> None:
    """For a plug-in's own test suite: an AssertionError listing every problem when `obj` does not keep its kind's
    contract (the same check as `testbench plugin test`).

        def test_rack_is_a_good_backend():
            plugins.assert_conforms("backends", testbench_rack.backend)
    """
    problems = check(kind_name, obj, name)
    if problems:
        who = name or getattr(obj, "name", "") or repr(obj)
        raise AssertionError(f"{who} does not keep the {kind_name} contract:\n" + "\n".join(f"- {p}" for p in problems))


# ---- what is installed, and the conformance kit (`testbench plugins`, `testbench plugin test`) --------------------

def inventory() -> list[dict]:
    """Every kind with what it has: its built-ins, the plug-ins installed for it and the package each came from, and
    the broken ones with why. A kind whose module does not define KIND yet is not open, and any plug-ins installed
    for it are listed as waiting. What `testbench plugins` and `testbench doctor` show."""
    rows = []
    for name, module in KINDS.items():
        row = {"kind": name, "group": GROUP.format(kind=name), "module": module, "open": True, "one": name,
               "what": "", "builtin": [], "installed": [], "broken": [], "waiting": []}
        rows.append(row)
        try:
            k = kind(name)
        except PluginError as exc:
            row.update(open=False, why=str(exc))
            try:
                row["waiting"] = [{"name": ep.name, "from": ep.value, "package": package_of(ep)}
                                  for ep in entry_points(name)]
            except Exception:
                pass
            continue
        except Exception as exc:                 # a bug in that kind's module: say so, and list the other kinds
            row.update(open=False, why=f"{module} did not import", error=f"{module} did not import: "
                                                                         f"{type(exc).__name__}: {exc}")
            continue
        row.update(one=k.one, what=k.what)
        try:
            found = registry(name)
        except Exception as exc:
            row["error"] = f"its built-ins did not load: {type(exc).__name__}: {exc}"
            continue
        row["builtin"] = [n for n in found if n in k.builtin]
        row["installed"] = [{"name": n, **k.origin.get(n, {"from": "", "package": ""})}
                            for n in found if n not in k.builtin]
        row["broken"] = [{"name": n, "why": why, "package": k.origin.get(n, {}).get("package", "")}
                         for n, why in k.broken.items()]
    return rows


def conformance(target: str = "", kind_name: str = "", installed: bool = False) -> list[dict]:
    """The conformance kit: one result per plug-in tested, {kind, name, source, package, problems} (problems [] =
    it keeps its kind's contract). `target` is module:attr (one object) or an installed package's name (every
    plug-in it declares); installed=True tests every installed plug-in instead. Input it cannot test - no such
    object or package, a package that declares no plug-ins, a kind it cannot tell - raises PluginError."""
    if kind_name and kind_name not in KINDS:
        raise PluginError(f"no plug-in kind {kind_name!r}; the kinds are {', '.join(KINDS)}")
    if installed:
        return [_test_entry_point(k, ep) for k in KINDS if not kind_name or k == kind_name for ep in entry_points(k)]
    if not target:
        raise PluginError("say what to test: module:attr (one object), an installed package's name, or --installed")
    if ":" in target:
        return _test_object(target, kind_name)
    from importlib.metadata import PackageNotFoundError, distribution
    try:
        dist = distribution(target)
    except PackageNotFoundError:
        raise PluginError(f"no installed package {target!r}; give the name pip installed it under (`pip show NAME`), "
                          "or module:attr for one object (e.g. testbench_rack:backend)") from None
    eps = [ep for ep in dist.entry_points if ep.group.startswith(PREFIX)
           and (not kind_name or ep.group == GROUP.format(kind=kind_name))]
    if not eps:
        group = GROUP.format(kind=kind_name or "backends")
        raise PluginError(f"{target} declares no {kind_name or 'plug-ins'}: it has no entry points in an "
                          f"{GROUP.format(kind=kind_name or 'KIND')} group (a package declares them in its "
                          f"pyproject.toml, under [project.entry-points.\"{group}\"])")
    return [_test_entry_point(ep.group[len(PREFIX):], ep) for ep in eps]


def _test_entry_point(kind_name: str, ep) -> dict:
    out = {"kind": kind_name, "name": ep.name, "source": ep.value, "package": package_of(ep), "problems": []}
    if kind_name not in KINDS:
        out["problems"].append(f"its entry point group {GROUP.format(kind=kind_name)} is not a kind of plug-in, so it "
                               f"is never used; the kinds are {', '.join(KINDS)}")
        return out
    try:
        k = kind(kind_name)
    except PluginError as exc:
        out["problems"].append(f"this version of testbench does not take {kind_name} plug-ins yet, so it is not used "
                               f"({exc})")
        return out
    except Exception as exc:
        out["problems"].append(f"the {kind_name} kind could not be loaded to test it: {type(exc).__name__}: {exc}")
        return out
    if ep.name in k.builtin:
        out["problems"].append(f"{ep.name} is a built-in {k.one}, and a plug-in cannot replace one: it would be "
                               "skipped. Give its entry point another name")
    try:
        obj = ep.load()
    except Exception as exc:
        out["problems"].append(f"it does not load: {type(exc).__name__}: {exc}")
        return out
    out["problems"] += check(kind_name, obj, ep.name)
    return out


def _load_here(ref: str):
    """module:attr as `python -m` would find it: installed, on PYTHONPATH, or in the current folder - where a plug-in
    being written sits before it is packaged (the `testbench` command, unlike python, does not look there)."""
    try:
        return _load_ref(ref)
    except ModuleNotFoundError as exc:
        here = os.getcwd()
        if exc.name != ref.partition(":")[0].strip().split(".")[0] or here in sys.path or "" in sys.path:
            raise
        sys.path.insert(0, here)
        try:
            return _load_ref(ref)
        except Exception:
            sys.path.remove(here)
            raise


def _test_object(ref: str, kind_name: str) -> list[dict]:
    try:
        obj = _load_here(ref)
    except Exception as exc:
        raise PluginError(f"cannot load {ref}: {type(exc).__name__}: {exc}. Give it as module:attr, with the module "
                          "importable here (installed, or its folder on PYTHONPATH)") from None
    name = getattr(obj, "name", "")
    return [{"kind": k, "name": name if isinstance(name, str) else "", "source": ref, "package": "",
             "problems": check(k, obj)} for k in ([kind_name] if kind_name else _kinds_of(ref, obj))]


def _kinds_of(ref: str, obj) -> list[str]:
    """The kind an object is: the one an installed entry point declares it under, else the only kind that takes it."""
    declared = []
    for k in KINDS:
        try:
            if any(ep.value.replace(" ", "") == ref.replace(" ", "") for ep in entry_points(k)):
                declared.append(k)
        except Exception:
            continue
    if declared:
        return declared
    takes = []
    for k in KINDS:
        try:
            kind(k).prepare(_spare(obj), getattr(obj, "name", "") or "plugin")
        except Exception:
            continue
        takes.append(k)
    if len(takes) == 1:
        return takes
    why = f"it could be any of {', '.join(takes)}" if takes else "no kind takes it as it is"
    raise PluginError(f"which kind of plug-in is {ref}? {why}; say which with --kind ({', '.join(KINDS)})")
