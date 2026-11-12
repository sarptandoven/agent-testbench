"""Where code runs. A backend is one object that says what it offers - GPUs, prices, the start-up a remote machine
costs, its section of testbench.yaml - and hands out the two classes that do the work: a plan runner (takes a
launched run, returns its report and cost) and a session (one kernel kept up on its machine).

Built in: local, modal, colab, ssh, docker, kubernetes, runpod. Others come from installed packages that declare an entry point:

    # pyproject.toml of a package such as testbench-ssh
    [project.entry-points."agent_testbench.backends"]
    ssh = "testbench_ssh:backend"            # an instance (or subclass) of agent_testbench.backends.Backend

Two rules every backend keeps: price its hardware, so `budget.check` bounds the worst case before anything
starts; and give every remote run and session a hard stop on the remote side, so the bound holds even if this
machine goes to sleep. `contract` checks them, and the rest of what a backend must be (`testbench plugin test`).

The registry is plugins.py's, kind "backends" (KIND below); registry(), get() and the others are its own calls,
with the ConfigError messages testbench.yaml's checks give.
"""
from __future__ import annotations

import copy
import fnmatch
import re
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .. import plugins
from ..config import ConfigError

ENTRY_POINTS = plugins.GROUP.format(kind="backends")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,40}")          # what `backend:` in testbench.yaml can name
GPU_FORMAT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.!+-]*(:[1-9][0-9]{0,2})?")
BUILTIN = {"local": "agent_testbench.backends.local:backend",
           "modal": "agent_testbench.backends.modal_backend:backend",
           "colab": "agent_testbench.backends.colab_backend:backend",
           "ssh": "agent_testbench.backends.ssh_backend:backend",
           "docker": "agent_testbench.backends.docker_backend:backend",
           "kubernetes": "agent_testbench.backends.kubernetes_backend:backend",
           "runpod": "agent_testbench.backends.runpod_backend:backend"}


class PlanRunner(Protocol):
    """Runs one launched plan. Built as cls(root=, cfg=, run_dir=, plan=, meta=) in the run's driver process."""

    def run(self) -> tuple[dict, float]: ...      # (report, US dollars); report.json is in run_dir when it returns
    def cancel(self) -> str: ...                  # stop the remote work now; say what was stopped
    def cost(self, report: dict) -> float: ...    # what the run cost so far, for a run cut short


class Session(Protocol):
    """One kernel on the backend's machine, built as cls(root, cfg, sdir, state). Runs kernelkit's launcher there
    and kernelkit's client for each exec, so `exec` returns the same evidence on every backend."""

    def start(self) -> dict: ...                  # handles saved in state.json (ids to find the machine again)
    def alive(self) -> bool: ...
    def exec(self, code: str, timeout: float, prefix: str, out_dir: Path) -> dict: ...
    def ls(self, path: str) -> list[str]: ...
    def get(self, remote: str, local: Path) -> None: ...
    def put(self, local: Path, remote: str) -> None: ...
    def install(self, packages: list[str]) -> str: ...
    def sync(self) -> str: ...
    def shell(self, command: str, timeout: float) -> str: ...
    def stop(self) -> None: ...


class Backend:
    name = ""
    gpus: tuple[str, ...] = ()            # GPUs it offers by name (a plan may ask for NAME:COUNT); empty: no choice
    remote = True                         # another machine: plans run in a copy, start-up is priced in
    startup_minutes = 5.0                 # added to every worst case: starting, installing, uploading
    max_session_minutes: float | None = None
    hard_stop = ""                        # remote: what ends its work at the time limit on the remote side, in words
    config_defaults: dict = {}            # its own section(s) of testbench.yaml, e.g. {"modal": {"cpu": 2.0}}

    def check_hardware(self, where: str, gpu) -> None:
        if not gpu:
            return
        if not GPU_FORMAT.fullmatch(str(gpu)):
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write a GPU name, or NAME:COUNT for several, with no "
                              "spaces: e.g. H100 or H100:2")
        if not self.gpus:
            raise ConfigError(f"`{where}.gpu`: {self.name} has no choice of GPU; drop the GPU, or choose "
                              f"{_gpu_backends()}")
        _, sep, count = str(gpu).partition(":")
        if sep and not (count.isascii() and count.isdigit() and int(count) > 0):     # T4:0 would be priced as no GPU
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; write the GPU, or the GPU and how many, e.g. H100 or H100:2")
        if split_gpu(gpu)[0] not in {g.upper() for g in self.gpus}:
            raise ConfigError(f"`{where}.gpu` is {gpu!r}; {self.name} offers {', '.join(self.gpus)}")

    def validate(self, cfg: dict) -> None:
        """Check this backend's own section of testbench.yaml; called only when a plan or the sessions use it."""

    def rate_per_minute(self, cfg: dict, hardware: dict) -> float:
        """US dollars per minute for hardware ({backend, gpu, cpu?, memory_gb?}) at its worst."""
        return 0.0

    def plan_runner(self) -> type[PlanRunner]:
        raise ConfigError(f"the {self.name} backend cannot run plans; use it for sessions, or choose another backend")

    def session_class(self) -> type[Session]:
        raise ConfigError(f"the {self.name} backend has no live sessions; use it for plans, or choose another backend")

    def cancel_remote(self, state: dict) -> str:
        """Cancel a run's remote work from the handles in its state.json, when its driver process is gone."""
        return ""

    def doctor(self) -> list[tuple[str, bool, str]]:
        """(what, ok, detail) lines for `testbench doctor`: is this machine set up to use the backend?"""
        return []

    def preflight(self, cfg: dict) -> str:
        """Why work cannot start on this backend here and now, and the fix, in plain words ('' when it can): asked
        just before a run or session is started (once the budget allows it), before its folder or a ledger line is
        written, so a launch that could only fail is refused instead (exit 2, nothing recorded). Quick: seconds."""
        return ""

    def prices(self, cfg: dict) -> list[str]:
        """Lines for `testbench prices`: what this backend charges for, and where each price comes from."""
        return []


def split_gpu(gpu) -> tuple[str, int]:
    """'H100:2' -> ('H100', 2); None -> ('', 1)."""
    if not gpu:
        return "", 1
    name, _, count = str(gpu).partition(":")
    return name.upper(), int(count) if count.isascii() and count.isdigit() else 1


def tool_check(what: str, exe: str, how: str) -> tuple[str, bool, str]:
    found = shutil.which(exe)
    return what, bool(found), found or how


def _instance(obj, name: str) -> Backend:
    b = obj() if isinstance(obj, type) else obj
    if not isinstance(b, Backend):
        raise TypeError(f"{obj!r} is not an agent_testbench.backends.Backend")
    b.name = b.name or name
    return b


# ---- the contract: what `testbench plugin test` and plugins.assert_conforms("backends", obj) check ---------------

OWN_SECTIONS = ("project", "plans", "notify", "export", "data", "root")     # testbench.yaml's own, besides DEFAULTS'
PRICE_KEY = re.compile(r"usd|price", re.I)                         # a backend's own price settings, by their name
TRIAL_MINUTES = 60                                                 # the one step of the plan it is priced with


def _methods(protocol) -> list[str]:
    return [n for n, v in vars(protocol).items() if callable(v) and not n.startswith("_")]


def contract(b: Backend) -> list[str]:
    """The rules a backend keeps, as problems in plain words ([] = it keeps them all):

    - its name can be written as `backend:` in testbench.yaml;
    - config_defaults maps sections of its own (not testbench.yaml's, not another backend's) to their settings;
    - it prices its CPU time and every GPU it offers: the real budget code gives a one-step plan on it a finite
      worst case of 0 or more dollars, and more than 0 on a remote backend (at its defaults, or once its own price
      settings - a number under a key with usd or price in its name - are set);
    - a remote backend takes start-up minutes above 0 (added to every worst case), and says what stops its work on
      the remote side (hard_stop) or how long a session may run there (max_session_minutes);
    - plan_runner() is a class with run, cancel and cost, and session_class() one with the Session methods (either
      may raise ConfigError: it does not do that), and it does at least one of the two."""
    from .. import config as C
    problems = []
    if not isinstance(b.name, str) or not NAME.fullmatch(b.name):
        return [f"its name {b.name!r} cannot be written as `backend:` in testbench.yaml: name it with letters, "
                "digits, - and _ (e.g. name = \"rack\")"]
    if not isinstance(b.remote, bool):
        problems.append(f"remote is {b.remote!r}: True for another machine (plans run in a copy, start-up is priced), "
                        "False for this one")
    sections = b.config_defaults
    if not isinstance(sections, dict) or not all(isinstance(k, str) and isinstance(v, dict)
                                                 for k, v in sections.items()):
        problems.append(f"config_defaults is {sections!r}: it maps each section of testbench.yaml the backend reads "
                        f"to its settings, e.g. {{\"{b.name}\": {{\"usd_per_hour\": 0.6}}}}")
        sections = {}
    others = {s: n for n, o in registry().items() if n != b.name and o is not b
              and isinstance(o.config_defaults, dict) for s in o.config_defaults}
    for s in sections:
        if s in C.DEFAULTS or s in OWN_SECTIONS:
            problems.append(f"config_defaults has `{s}:`, which is testbench.yaml's own; a backend's settings go in a "
                            f"section of its own (`{b.name}:`)")
        elif s in others:
            problems.append(f"config_defaults has `{s}:`, which is the {others[s]} backend's section; use one of its "
                            f"own (`{b.name}:`)")
    remote = b.remote is True
    if not C.number(b.startup_minutes, positive=remote):
        problems.append(f"startup_minutes is {b.startup_minutes!r}; it is added to every worst case: "
                        + ("the minutes a remote machine takes to start, install and upload, above 0" if remote
                           else "a number of minutes, 0 or more"))
    if b.max_session_minutes is not None and not C.number(b.max_session_minutes, positive=True):
        problems.append(f"max_session_minutes is {b.max_session_minutes!r}; it is the longest a session may run on "
                        "it, in minutes above 0, or None")
    if not isinstance(b.hard_stop, str):
        problems.append(f"hard_stop is {b.hard_stop!r}; it says in words what stops remote work at its time limit")
    elif remote and b.max_session_minutes is None and not b.hard_stop.strip():
        problems.append("a remote backend stops its own work on the remote side, so the worst case holds even if "
                        "this machine goes offline: say what does it in hard_stop (e.g. hard_stop = \"a watchdog on "
                        "the machine ends the run at its time limit\"), or set max_session_minutes")
    problems += _prices(b, sections, remote)
    runner = _class_of(b, "plan_runner", PlanRunner, problems)
    session = _class_of(b, "session_class", Session, problems)
    if runner is None and session is None:
        problems.append("it runs neither plans nor sessions: plan_runner() or session_class() returns a class")
    return problems


def _class_of(b: Backend, what: str, protocol, problems: list):
    """The class b.<what>() hands out; None when it says it does not do that (ConfigError), False when it is wrong."""
    try:
        cls = getattr(b, what)()
    except ConfigError:
        return None
    except Exception as exc:
        problems.append(f"{what}() raised {type(exc).__name__}: {exc}; it returns a class, or raises ConfigError "
                        "when the backend does not do that")
        return False
    if not isinstance(cls, type):
        problems.append(f"{what}() returns {cls!r}, which is not a class")
        return False
    missing = [m for m in _methods(protocol) if not callable(getattr(cls, m, None))]
    if missing:
        problems.append(f"{what}() returns {cls.__name__}, which has no {', '.join(missing)} (see "
                        f"agent_testbench.backends.{protocol.__name__})")
    return cls


def _prices(b: Backend, sections: dict, remote: bool) -> list[str]:
    """Price a one-step plan on b - without a GPU and with each GPU it offers - through config's plan reader and
    budget.worst_case, on a throwaway config: its defaults, then (remote, at $0) its own price settings at 1."""
    from .. import budget, config as C
    try:
        gpus = list(b.gpus)
    except Exception as exc:
        return [f"its gpus cannot be read: {type(exc).__name__}: {exc}"]
    problems = []
    named = [g for g in gpus if isinstance(g, str) and GPU_FORMAT.fullmatch(g) and ":" not in g]
    if len(named) < len(gpus):
        problems.append(f"gpus lists {[g for g in gpus if g not in named]!r}: each is a GPU name a plan's `gpu:` can "
                        "give (no count, no spaces)")

    def worst(own: dict) -> list[tuple[str, float]]:
        cfg = C._merge(copy.deepcopy(C.DEFAULTS), copy.deepcopy(own))
        cfg["plans"], out = {}, []
        for i, gpu in enumerate([None, *named]):
            plan = {"backend": b.name, "gpu": gpu, "steps": [{"name": "one", "run": "true",
                                                               "max_minutes": TRIAL_MINUTES}]}
            cfg["plans"][f"p{i}"] = C._plan(f"p{i}", plan, Path.cwd())
            usd = budget.worst_case(cfg, f"p{i}")["worst_case_usd"]
            out.append((f"with gpu: {gpu}" if gpu else "without a GPU", usd))
        return out

    with _trying(b):
        try:
            found = worst(sections)
        except Exception as exc:
            return problems + [f"pricing a one-step plan on it raised {type(exc).__name__}: {exc} (does it read a "
                               "setting its config_defaults do not have?)"]
        wrong = [f"a one-step plan on it {what} costs {usd!r} at worst; a price is a number of dollars, 0 or more, or "
                 "budget.check cannot bound it" for what, usd in found
                 if not C.number(usd)]
        free = [what for what, usd in found if usd == 0]
        if wrong or not remote or not free:
            return problems + wrong
        raised, keys = _prices_set(sections)
        try:
            again = dict(worst(raised)) if keys else {}
        except Exception as exc:
            again, problems = {}, problems + [f"pricing a plan with {', '.join(keys)} set raised "
                                              f"{type(exc).__name__}: {exc}"]
        still = [what for what in free if not (again.get(what) or 0) > 0]
        if still:
            how = f"even with {', '.join(keys)} set to 1" if keys else "and it has no price setting of its own"
            problems.append(f"a one-step plan on it {' or '.join(still)} costs $0 at worst, {how}: a remote backend "
                            "prices its machine, so budget.check bounds what a run can cost (a free one, such as "
                            f"your own machine, takes a price setting that can be 0, e.g. `{b.name}: "
                            "{usd_per_hour: 0}`)")
    return problems


def _prices_set(sections: dict) -> tuple[dict, list[str]]:
    """Its sections with every price setting - a number under a key with usd or price in its name - at 1."""
    keys = []

    def walk(x, path: str, priced: bool):
        if isinstance(x, dict):
            return {k: walk(v, f"{path}.{k}", priced or bool(PRICE_KEY.search(str(k)))) for k, v in x.items()}
        if priced and isinstance(x, (int, float)) and not isinstance(x, bool):
            keys.append(path)
            return 1.0
        return x
    return {s: walk(v, s, False) for s, v in sections.items()}, keys


@contextmanager
def _trying(b: Backend):
    """b in the registry under its name while the real budget code prices it; what was there is put back."""
    found = registry()
    before = found.get(b.name)
    found[b.name] = b
    try:
        yield
    finally:
        if before is None:
            found.pop(b.name, None)
        else:
            found[b.name] = before


KIND = plugins.Kind("backends", "backend", "where plans and sessions run", BUILTIN, prepare=_instance,
                    contract=contract)


def registry() -> dict[str, Backend]:
    """Every backend that loaded: the built-ins, then installed plugins. A plugin that fails to load is left out
    and reported by `testbench doctor` and `testbench plugins`, so it only breaks the projects that use it."""
    return plugins.registry("backends")


def register(backend: Backend) -> Backend:
    """Add a backend by hand (a test, or a notebook that defines one inline)."""
    return plugins.register("backends", backend.name, backend)


def broken() -> dict[str, str]:
    return plugins.broken("backends")


def names() -> list[str]:
    return plugins.names("backends")


def get(name: str) -> Backend:
    found = registry()
    if name in found:
        return found[name]
    why = broken().get(name)
    if why:
        raise ConfigError(f"backend {name!r} is installed but did not load ({why}); `testbench doctor` "
                          "shows it - reinstall or fix that package")
    raise ConfigError(f"no backend {name!r}; use one of {', '.join(found)}, or install the package that provides it")


def _gpu_backends() -> str:
    offering = [n for n, b in registry().items() if b.gpus]
    return (" or ".join(offering) + " backend") if offering else "a backend that offers GPUs"


def excluded(rel: str, patterns: list[str]) -> bool:
    parts = Path(rel).parts
    return any(fnmatch.fnmatch(rel, p) or any(fnmatch.fnmatch(part, p) for part in parts) for p in patterns)


def copy_project(root: Path, dest: Path, exclude: list[str]) -> Path:
    """A scratch copy of the project, without the excluded files (and never .testbench itself)."""
    root, dest = Path(root), Path(dest)
    patterns = list(exclude) + [".testbench"]

    def ignore(directory, names):
        rel_dir = Path(directory).relative_to(root)
        return [n for n in names if excluded(str(rel_dir / n) if str(rel_dir) != "." else n, patterns)]

    shutil.copytree(root, dest, ignore=ignore, dirs_exist_ok=True, symlinks=True)
    return dest


def project_files(root: Path, exclude: list[str]) -> list[Path]:
    root = Path(root)
    patterns = list(exclude) + [".testbench"]
    out = []
    for p in sorted(root.rglob("*")):
        rel = str(p.relative_to(root))
        if p.is_file() and not excluded(rel, patterns):
            out.append(p)
    return out
