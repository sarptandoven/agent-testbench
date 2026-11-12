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

Mock SDKs are plug-ins (kind "mocks", docs/plugins.md). Each is one Mock object; the five above are built in, and a
package adds another (for `stripe`, or an internal API) with an entry point, usually built on StrictAPI:

    # pyproject.toml
    [project.entry-points."agent_testbench.mocks"]
    weatherkit = "testbench_mock_example:mock"

A Mock says:

    name                 what a step's `mocks:` lists, e.g. "weatherkit" (the entry point's name)
    path                 the folder, or a list of folders, whose packages go first on the step's PYTHONPATH
    imports              the module names it stands in for: ["anthropic"], ["ib_async", "ib_insync"]
    validate(where, opts, root) -> dict
                         the step's options for it, normalized, or a ConfigError naming the field (`where` is
                         where they sit in testbench.yaml; root is the project folder). The default takes the
                         options options_schema lists and checks that each file option exists
    files                options that name a file in the project: the step gets them as absolute paths
    call_keys            the `mock_calls:` rule keys its calls are counted by (CLIENT_KEYS for the LLM clients and
                         plug-ins, BROKER_KEYS for the brokers); always calls and refused
    options_schema, calls_schema
                         JSON Schema fragments for its options and its `mock_calls:` rules, as they would sit in
                         testbench.schema.json (they may $ref its #/$defs/rule)
    example              a few lines a step would run that make one call it answers, with the client bound to
                         `client`; the contract runs them

In the step, a mock reads its options with step_options(name) (TESTBENCH_MOCK_<NAME>, JSON) and logs every call,
answered or refused, as one line of the step's mock_calls.jsonl under its name; StrictAPI(..., sdk=name) does both,
and its not_modelled() refuses a method the mock does not have with an error naming it. The step's Python may not
have agent-testbench (from uvx or pipx it is the project's), so a mock's own code imports these from
_testbench_strict, a standard-library module in the shared folder every mocked step has on its PYTHONPATH; this
module hands out the same objects.

contract() checks a Mock the way `testbench plugin test` does: its path holds a package for each import; importing it
and running its example with the network cut (and no agent_testbench to import) opens no connection; validate refuses
junk with a ConfigError naming the field; the example's call is logged; and calling something it does not model
raises an error naming the method.
"""
from __future__ import annotations

import difflib
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import plugins
from .config import ConfigError

LLM_SDKS = ("anthropic", "openai")
BROKERS = ("alpaca", "ccxt", "ib_async")
SDKS = LLM_SDKS + BROKERS                                   # the built-in mocks' names; names() adds plug-ins
CLIENT_KEYS = ("calls", "refused", "injected", "input_tokens", "output_tokens", "tool_calls")
BROKER_KEYS = ("calls", "refused", "orders", "fills", "cancels", "orders_without_limit", "max_order_value", "fees",
               "live")
CLIENTS = Path(__file__).parent / "mock_clients"
SHARED = CLIENTS / "shared"
_core = _broker = None


def strict_core():
    """StrictAPI's module (mock_clients/shared/_testbench_strict.py): standard library only, so a plug-in mock's own
    code imports it in any step's Python. Loaded once under its own name, so a process that imports it both ways
    (agent_testbench.mocks and _testbench_strict) gets the same classes."""
    found = sys.modules.get("_testbench_strict")
    if found is None:
        spec = importlib.util.spec_from_file_location("_testbench_strict", SHARED / "_testbench_strict.py")
        found = sys.modules["_testbench_strict"] = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(found)
        except BaseException:
            sys.modules.pop("_testbench_strict", None)
            raise
    return found


_strict = strict_core()
UnexpectedCall, NotModelled, Endpoint, StrictAPI = _strict.UnexpectedCall, _strict.NotModelled, _strict.Endpoint, \
    _strict.StrictAPI
env_name, step_options = _strict.env_name, _strict.step_options


# ---- mock SDKs: the built-ins and plug-ins (docs/plugins.md) ----------------------------------------------------

def _rules(keys) -> dict:
    return {"type": ["object", "null"], "additionalProperties": False,
            "properties": {k: {"description": f'{k}: a rule, e.g. "<= 5"', "$ref": "#/$defs/rule"} for k in keys}}


class Mock:
    """One mock SDK a step's `mocks:` can name (the module docstring says what each attribute is). Build one with
    keywords, Mock(name=..., path=..., imports=[...], example=...), or subclass it to check options of your own."""
    name = ""
    path: str | Path | list = ()
    imports: tuple = ()
    files: tuple = ()
    call_keys: tuple = ("calls", "refused")
    options_schema: dict = {"type": ["object", "null"], "additionalProperties": False, "properties": {}}
    calls_schema: dict | None = None                    # None: rules on each of call_keys
    example = ""

    def __init__(self, name: str = "", path=None, imports=None, **attrs):
        for key, value in {"name": name, "path": path, "imports": imports, **attrs}.items():
            if key not in ("name", "path", "imports", "files", "call_keys", "options_schema", "calls_schema",
                           "example"):
                raise TypeError(f"Mock() has no {key!r}; it takes name, path, imports, files, call_keys, "
                                "options_schema, calls_schema and example")
            if value is not None and value != "":
                setattr(self, key, tuple(value) if key in ("imports", "files", "call_keys") else value)
        if self.calls_schema is None:
            self.calls_schema = _rules(self.call_keys)

    def __repr__(self):
        return f"{type(self).__name__}({self.name!r}, imports={list(self.imports)})"

    def folders(self) -> list[Path]:
        """The folders put first on the step's PYTHONPATH."""
        paths = self.path if isinstance(self.path, (list, tuple)) else [self.path]
        return [Path(p) for p in paths if p not in (None, "")]

    def validate(self, where: str, opts, root: Path) -> dict:
        known = list((self.options_schema or {}).get("properties") or {})
        takes = f"; the options are {', '.join(known)}" if known else "; it takes no options, so list it by name"
        if opts is None:
            return {}
        if not isinstance(opts, dict):
            raise ConfigError(f"`{where}` is the mock's options, a mapping{takes}")
        for key in opts:
            if key not in known:
                near = difflib.get_close_matches(str(key), known, n=1)
                raise ConfigError(f"`{where}` has no {key!r}" + (f" (did you mean {near[0]!r}?)" if near else "") + takes)
        for key in self.files:
            value = opts.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(f"`{where}.{key}` is a file's path, relative to the project folder")
            if not (Path(root) / value).is_file():
                raise ConfigError(f"`{where}.{key}`: {value} does not exist (paths are relative to {root})")
        return dict(opts)

    def env(self, opts: dict, workdir: Path) -> dict:
        """What the step's process gets: the options as JSON, with file options made absolute."""
        opts = dict(opts or {})
        for key in self.files:
            if opts.get(key):
                opts[key] = str((Path(workdir) / opts[key]).resolve())
        return {env_name(self.name): json.dumps(opts, default=str)}


class LLMClient(Mock):
    """anthropic and openai: replies from a file, failures on purpose (docs/mocks.md)."""
    files = ("replies",)
    call_keys = CLIENT_KEYS
    options_schema = {"$ref": "#/$defs/mock_options"}
    calls_schema = {"$ref": "#/$defs/client_calls"}

    def validate(self, where: str, opts, root: Path) -> dict:
        import yaml
        core, opts = client_core(), opts or {}
        bad = core.options_error(opts)
        if bad:
            raise ConfigError(f"`{where}`: {bad}")
        if "replies" in opts:
            f = Path(root) / opts["replies"]
            if not f.is_file():
                raise ConfigError(f"`{where}.replies`: {opts['replies']} does not exist (paths are relative to {root})")
            try:
                core.parse_replies(yaml.safe_load(f.read_text(encoding="utf-8")))
            except (yaml.YAMLError, ValueError) as exc:
                raise ConfigError(f"`{where}.replies` ({opts['replies']}): {exc}") from None
        return opts


class Broker(Mock):
    """alpaca, ccxt, ib_async: prices, cash, fees and the live-account rule (docs/mocks.md#brokers)."""
    files = ("prices",)
    call_keys = BROKER_KEYS
    options_schema = {"$ref": "#/$defs/broker_options"}
    calls_schema = {"$ref": "#/$defs/broker_calls"}

    def validate(self, where: str, opts, root: Path) -> dict:
        opts = opts or {}
        now = opts.get("now") if isinstance(opts, dict) else None
        if now is not None and not isinstance(now, (str, int, float)):
            opts = {**opts, "now": str(now)}                 # YAML reads `now: 2024-01-05` as a date
        bad = broker_core().setup_error(opts, str(root))
        if bad:
            raise ConfigError(f"`{where}`: {bad}")
        return opts


_HELLO = '[{"role": "user", "content": "Hello"}]'
ANTHROPIC = LLMClient("anthropic", CLIENTS / "anthropic", ["anthropic"], example=(
    "import anthropic\nclient = anthropic.Anthropic()\n"
    f'client.messages.create(model="claude-opus-5-5", max_tokens=64, messages={_HELLO})\n'))
OPENAI = LLMClient("openai", CLIENTS / "openai", ["openai"], example=(
    f'import openai\nclient = openai.OpenAI()\nclient.chat.completions.create(model="gpt-4o-mini", messages={_HELLO})\n'))
ALPACA = Broker("alpaca", CLIENTS / "alpaca", ["alpaca"], example=(
    'from alpaca.trading.client import TradingClient\nclient = TradingClient("key", "secret", paper=True)\n'
    "client.get_account()\n"))
CCXT = Broker("ccxt", CLIENTS / "ccxt", ["ccxt"], example=(
    'import ccxt\nclient = ccxt.binance({"apiKey": "key", "secret": "secret"})\nclient.set_sandbox_mode(True)\n'
    "client.fetch_balance()\n"))
IB_ASYNC = Broker("ib_async", CLIENTS / "ib_async", ["ib_async", "ib_insync"], example=(
    'from ib_async import IB\nclient = IB()\nclient.connect("127.0.0.1", 7497, clientId=1)\nclient.accountSummary()\n'))


def _prepare(obj, name: str) -> Mock:
    if isinstance(obj, type) and issubclass(obj, Mock):
        obj = obj()
    if not isinstance(obj, Mock):
        raise TypeError(f"a mock is an agent_testbench.mocks.Mock (an instance, or a subclass), not {type(obj).__name__}")
    if not obj.name:
        obj.name = name
    elif name and obj.name != name:
        raise plugins.PluginError(f"it calls itself {obj.name!r} but is installed as {name!r}; the two must match (a "
                                  "step's mocks: lists the installed name, and its calls are logged under its own)")
    return obj


def names() -> list[str]:
    """Every mock a step can name: the built-ins, then installed plug-ins."""
    return plugins.names("mocks")


def get(name: str) -> Mock:
    return plugins.get("mocks", name)


def find(name, where: str) -> Mock:
    """The mock a step's `mocks:` (or `mock_calls:`) names, or a ConfigError naming `where` and the mocks there are."""
    if isinstance(name, str) and name in plugins.registry("mocks"):
        return get(name)
    bad = plugins.broken("mocks").get(name) if isinstance(name, str) else None
    if bad:
        raise ConfigError(f"`{where}` names {name!r}, a mock that is installed but did not load ({bad}); `testbench "
                          "plugins` shows it - reinstall or fix that package")
    extra = [n for n in names() if n not in SDKS]
    hint = " (the mock ib_async also answers `import ib_insync`: list ib_async)" if name == "ib_insync" else ""
    raise ConfigError(f"`{where}` names {name!r}; testbench has mock clients for {', '.join(SDKS)}"
                      + (f", and installed plug-ins add {', '.join(extra)}" if extra else "") + f"{hint}. For another "
                      "API, write a strict mock with agent_testbench.mocks.StrictAPI and list its folder under "
                      "pythonpath:, or install a package that adds one (entry point group agent_testbench.mocks; "
                      "docs/plugins.md)")


def call_keys(name, where: str = "mock_calls") -> tuple[str, ...]:
    """The `mock_calls:` rule keys the mock `name` is counted by (CLIENT_KEYS, BROKER_KEYS or a plug-in's own); an
    unknown name is a ConfigError naming `where` and the mocks there are."""
    return tuple(find(name, where).call_keys)


def client_paths(names) -> list[str]:
    """Put first on PYTHONPATH, these make `import openai` / `import anthropic` / `import alpaca` (or a plug-in's
    imports) load the mocks: each mock's folders, then the folder of the shared cores."""
    out = []
    for n in names:
        for p in get(n).folders():
            if str(p) not in out:
                out.append(str(p))
    return out + [str(SHARED)]


def client_env(mocks: dict, workdir: Path) -> dict:
    """What the mocks read: their options as JSON, with file options (replies, prices, ...) made absolute."""
    env = {}
    for name, opts in mocks.items():
        env.update(get(name).env(opts, workdir))
    return env


# ---- the contract: what `testbench plugin test` checks for a mock -----------------------------------------------

UNMODELLED = "testbench_unmodelled_call"
JUNK = "testbench_junk_option"
PROBE = r'''
import importlib, json, socket, sys
out, imports, example, method = sys.argv[1], json.loads(sys.argv[2]), sys.argv[3], sys.argv[4]
seen = {"connections": [], "testbench": False, "imports": {}, "example": None, "unmodelled": None}

def save():
    with open(out, "w", encoding="utf-8") as f:
        json.dump(seen, f)

def cut(*args, **kwargs):
    method = bool(args) and isinstance(args[0], socket.socket)
    seen["connections"].append(repr(args[-1] if method else args[0] if len(args) == 1 else args[:2])[:200])
    save()
    raise OSError("the network is cut while testbench checks this mock")

for attr in ("connect", "connect_ex", "sendto"):
    setattr(socket.socket, attr, cut)
socket.create_connection = socket.getaddrinfo = socket.gethostbyname = socket.gethostbyname_ex = cut

class NoTestbench:                  # as in a step whose Python is the project's (uvx, pipx): no agent_testbench
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "agent_testbench":
            seen["testbench"] = True
            raise ModuleNotFoundError(f"No module named {name!r} (the step's Python may not have agent-testbench)",
                                      name=name)

sys.meta_path.insert(0, NoTestbench())
for name in imports:
    try:
        seen["imports"][name] = getattr(importlib.import_module(name), "__file__", None) or ""
    except BaseException as exc:
        seen["imports"][name] = {"error": f"{type(exc).__name__}: {exc}"[:500]}
save()
space = {"__name__": "__main__"}
try:
    exec(compile(example, "<the mock's example>", "exec"), space)
    seen["example"] = "ok"
except BaseException as exc:
    seen["example"] = f"{type(exc).__name__}: {exc}"[:500]
if "client" in space:
    try:
        got = getattr(space["client"], method)()
        seen["unmodelled"] = {"raised": False, "got": repr(got)[:200]}
    except BaseException as exc:
        seen["unmodelled"] = {"raised": True, "type": type(exc).__name__, "message": str(exc)[:500]}
save()
'''


def contract(mock: Mock) -> list[str]:
    """What is wrong with a mock, in plain words ([] = it keeps the contract). Runs its example in a subprocess."""
    problems = []
    name = mock.name
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name):
        problems.append(f"its name {name!r} is not one a step's mocks: can list (a letter, then letters, digits, "
                        "_ . or -)")
    folders = mock.folders()
    if not folders:
        problems.append("it has no path: the folder whose packages go first on the step's PYTHONPATH")
    for f in folders:
        if not f.is_absolute():
            problems.append(f"its path {f} is relative; give the folder's full path, e.g. Path(__file__).parent / "
                            "'sdk'")
        elif not f.is_dir():
            problems.append(f"its path {f} does not exist (or is not a folder)")
    found = [str(f) for f in folders if f.is_absolute() and f.is_dir()]
    imports = list(mock.imports or ())
    if not imports or not all(isinstance(m, str) and m for m in imports):
        problems.append(f"its imports are {imports!r}; list the module names it stands in for, e.g. ['weatherkit']")
        imports = [m for m in imports if isinstance(m, str) and m]
    top = {m: m.split(".")[0] for m in imports}                     # google.cloud.storage: a google/ folder
    present = [m for m in imports if found and importlib.machinery.PathFinder.find_spec(top[m], found) is not None]
    for module in imports if found else ():
        if module not in present:
            problems.append(f"its path has no package {top[module]!r}: put {top[module]}/__init__.py (or "
                            f"{top[module]}.py) in {' or '.join(found)}")
    keys = tuple(mock.call_keys or ())
    counted = BROKER_KEYS if name in BROKERS else CLIENT_KEYS
    if not {"calls", "refused"} <= set(keys):
        problems.append(f"its call_keys {list(keys)} leave out calls or refused, which every mock is counted by (a "
                        "step with mocks: fails on a refused call unless it says otherwise)")
    if set(keys) - set(counted):
        problems.append(f"its call_keys name {', '.join(sorted(set(keys) - set(counted)))}, which testbench does not "
                        f"count; a mock's calls are counted from its log lines as {', '.join(counted)}")
    problems += _schemas(mock, keys)
    bad, opts = _options(mock)
    problems += bad
    if not isinstance(mock.example, str) or not mock.example.strip():
        problems.append("it has no example: a few lines a step would run that make one call it answers, with the "
                        "client bound to `client`")
    elif present and len(found) == len(folders) and opts is not None:
        problems += _run_example(mock, present, opts)
    return problems


def _schemas(mock: Mock, keys: tuple) -> list[str]:
    out = []
    for attr in ("options_schema", "calls_schema"):
        frag = getattr(mock, attr)
        if not isinstance(frag, dict):
            out.append(f"its {attr} is {type(frag).__name__}; it is a JSON Schema fragment (a mapping)")
            continue
        try:
            import jsonschema
            jsonschema.Draft202012Validator.check_schema(frag)
        except ImportError:
            pass
        except Exception as exc:
            out.append(f"its {attr} is not a valid JSON Schema: {str(exc).splitlines()[0]}")
    rules = (mock.calls_schema or {}).get("properties") if isinstance(mock.calls_schema, dict) else None
    if rules is not None and set(rules) != set(keys):
        out.append(f"its calls_schema has rules for {sorted(rules)} but its call_keys are {sorted(keys)}; make them "
                   "the same")
    return out


def _options(mock: Mock) -> tuple[list[str], dict | None]:
    """validate's problems, and the options it makes of none (None if it refuses them)."""
    out, opts = [], None
    with tempfile.TemporaryDirectory() as tmp:
        where = f"steps[0].mocks.{mock.name}"
        try:
            mock.validate(where, {JUNK: 1}, Path(tmp))
            out.append(f"validate accepted an option it does not have ({JUNK}: 1); refuse unknown options with a "
                       "ConfigError naming the field, so a misspelt option fails before the step runs")
        except ConfigError as exc:
            if JUNK not in str(exc):
                out.append(f"validate refused an unknown option, but its message does not name the field {JUNK!r}: "
                           f"{exc}")
        except Exception as exc:
            out.append(f"validate raised {type(exc).__name__} for an unknown option, where a ConfigError naming the "
                       f"field belongs: {exc}")
        try:
            opts = mock.validate(where, {}, Path(tmp))
            if not isinstance(opts, dict):
                out.append(f"validate returned {type(opts).__name__} for no options; it returns the options, a mapping")
                opts = None
        except Exception as exc:
            out.append(f"validate refuses no options at all ({type(exc).__name__}: {exc}); a mock works with none "
                       "(the contract runs its example with none)")
    return out, opts


def _run_example(mock: Mock, imports: list[str], opts: dict) -> list[str]:
    """Import the mock and run its example as a step would, the network cut; then call a method it cannot have."""
    out = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        probe, seen_file, log = tmp / "_testbench_probe.py", tmp / "seen.json", tmp / "mock_calls.jsonl"
        probe.write_text(PROBE, encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("TESTBENCH_MOCK")}
        env.update(mock.env(opts, tmp))
        env.update(TESTBENCH_MOCK_CALLS=str(log), TESTBENCH_STEP_DIR=str(tmp), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONPATH=os.pathsep.join([str(f) for f in mock.folders()] + [str(SHARED)]
                                              + [p for p in [os.environ.get("PYTHONPATH")] if p]))
        try:
            r = subprocess.run([sys.executable, str(probe), str(seen_file), json.dumps(imports), mock.example,
                                UNMODELLED], cwd=tmp, env=env, capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return ["importing it and running its example did not finish in 2 minutes"]
        try:
            seen = json.loads(seen_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return [f"importing it in a fresh Python failed before anything ran: {(r.stderr or r.stdout)[-500:]}"]
        if seen["testbench"]:
            out.append("its code imports agent_testbench in the step, whose Python may not have agent-testbench (from "
                       "uvx or pipx it is the project's); import StrictAPI, Endpoint and step_options from "
                       "_testbench_strict instead, the same objects, which every mocked step has on its PYTHONPATH")
            return out
        if seen["connections"]:
            out.append(f"it reached for the network (to {'; '.join(seen['connections'][:3])}); a mock never touches "
                       "it")
        for module, file in seen["imports"].items():
            if isinstance(file, dict):
                out.append(f"`import {module}` failed with its path first on PYTHONPATH: {file['error']}")
            elif file and not any(Path(file).resolve().is_relative_to(f.resolve()) for f in mock.folders()):
                out.append(f"`import {module}` loaded {file}, not the mock in its path")
        if seen["example"] != "ok":
            out.append(f"its example failed: {seen['example']}")
            return out
        logged = []
        for line in log.read_text(encoding="utf-8").splitlines() if log.is_file() else []:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and rec.get("sdk") == mock.name:
                logged.append(rec)
        if not logged:
            out.append(f"its example logged no call under {mock.name!r} in mock_calls.jsonl, so `mock_calls:` rules "
                       f"would count nothing; log every call there (StrictAPI(..., sdk={mock.name!r}) does)")
        u = seen["unmodelled"]
        if u is None:
            out.append("its example does not bind `client` (the object a step calls), so calling something it does "
                       "not model cannot be checked")
        elif not u["raised"]:
            out.append(f"calling client.{UNMODELLED}() returned {u['got']} instead of raising an error that names it; "
                       "a mock refuses what it does not model (raise StrictAPI.not_modelled(name) in __getattr__)")
        elif UNMODELLED not in u["message"]:
            out.append(f"calling client.{UNMODELLED}() raised {u['type']}, but its message does not name the method: "
                       f"{u['message'][:200]}")
    return out


KIND = plugins.Kind("mocks", "mock", "stand-in SDKs a step's `mocks:` loads; they never touch the network",
                    {n: f"agent_testbench.mocks:{n.upper()}" for n in SDKS}, prepare=_prepare, contract=contract)


# ---- the shared cores of the built-in mocks (docs/mocks.md) -------------------------------------------------------

def client_core():
    """The mock clients' shared module, loaded from its file (it is not one of this package's modules), so
    testbench.yaml is checked by the same rules the mocks apply."""
    global _core
    if _core is None:
        spec = importlib.util.spec_from_file_location("_testbench_mock", SHARED / "_testbench_mock.py")
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
            path = SHARED / "_testbench_broker.py"
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
