"""testbench watch PLAN: run a plan now, then again each time the project's files change.

    testbench watch smoke                  # Ctrl-C stops it
    testbench watch gpu-check --approve    # a plan that costs money: only with a person's yes

Money: a plan is watched without --approve only when its worst case is $0 (backend local, or docker, ssh or
kubernetes at usd_per_hour 0), because watching runs it again on every save. With --approve each run is still a
normal launch - priced by `budget.check` and held to max_run_usd and the project's and this machine's day limits -
and watching stops at the first run refused.

Noticing a change: every --interval seconds it reads the modification time and size of the project's files (no
file-system events: nothing to install, and the same on Linux, macOS and Windows), and starts a run once nothing has
changed for --debounce seconds. Left out: testbench.yaml's `exclude:`, testbench's own folder, caches and editor
files (IGNORE), the patterns in .gitignore (and in a git repository whatever git ignores), and the files the plan
itself writes and checks - its artifacts and the files its expectations read - so a run does not set off the next.
If a run still rewrites the files that started it, run after run, watch says so and stops rerunning on them.

One run at a time: a change while a run is going queues one more run, never more (it covers every change made
meanwhile). Each run's line says PASS or FAIL and what changed since the run before it (testbench diff's summary).

Notifications: runs of a watch send no run_end, since a message for every save is noise; `notify.on: [watch_run]`
sends one when a watched run's result differs from the run before it (a step broke, or was fixed).

Ctrl-C (or SIGTERM, or a console break on Windows) cancels the run that is going, if any, and stops.
"""
from __future__ import annotations

import datetime as dt
import os
import signal
import subprocess
import time
from pathlib import Path

from . import budget, config as C, diff as D, expect, procs, report as R, runs
from .backends import excluded

IGNORE = [".pytest_cache", ".mypy_cache", ".ruff_cache", ".hypothesis", ".tox", ".nox", ".coverage", "*.egg-info",
          ".idea", ".vscode", "*.swp", "*.swo", "*.swx", "*~", ".#*", "#*#", "4913", "*.tmp"]
ECHOES = 3              # runs in a row that each rewrote exactly the files that started them: the plan writes them


def free(cfg: dict, plan: str) -> tuple[bool, dict]:
    """Whether the plan's worst case is $0 (a price that is not a number is not free)."""
    est = budget.worst_case(cfg, plan)
    return C.number(est["worst_case_usd"]) and est["worst_case_usd"] == 0, est


def refusal(plan: str, est: dict) -> str:
    hw = est["backend"] + (f" {est['gpu']}" if est.get("gpu") else "")
    cost = est["worst_case_usd"]
    return (f"Refused: plan {plan} can cost up to " + (f"${cost:.2f}" if C.number(cost) else repr(cost))
            + f" a run ({hw}, {est['max_minutes']:.0f} min), and watching runs it again on every change of the "
            "project's files. Only plans that cost nothing are watched without approval (backend local, or docker, "
            "ssh or kubernetes at usd_per_hour 0). Ask a person; with their yes, `testbench watch "
            f"{plan} --approve` checks every run against max_run_usd and the day limits before it starts. Or watch a "
            f"free plan, and run this one once: `testbench run {plan}`.")


def outputs(plan: dict) -> list[str]:
    """What the plan's steps write and check in the project folder: their artifacts and the files their
    expectations read (not image goldens, which are inputs)."""
    out = []
    for s in plan["steps"]:
        e = s.get("expect") or {}
        out += list(s.get("artifacts") or []) + list(e.get("files") or [])
        out += [k for key in ("json", "media", "image") for k in (e.get(key) or {})]
        out += [k for key in ("text", "metrics") for k in (e.get(key) or {}) if k not in ("stdout", "stderr")]
        out += [str(j.get("path")) for j in expect.junit_specs(e.get("junit")) if isinstance(j, dict) and j.get("path")]
    return list(dict.fromkeys(str(p).replace("\\", "/").removeprefix("./") for p in out))


def gitignore(root: Path) -> list[str]:
    """The project's .gitignore as simple patterns. Its ! lines are not followed (git itself is asked about changed
    files in a repository), and a pattern for everything (an allow-list .gitignore) is left out."""
    try:
        text = (Path(root) / ".gitignore").read_text(errors="replace")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "!")):
            continue
        line = line.strip("/")
        while line.startswith("**/"):
            line = line[3:]
        line = line.removesuffix("/**")
        if line.strip("*/"):
            out.append(line)
    return out


def left_out(root: Path, cfg: dict, plan: str) -> list[str]:
    root = Path(root)
    pats = list(cfg["exclude"]) + [".testbench", ".git"] + IGNORE + gitignore(root) + outputs(cfg["plans"][plan])
    try:                                              # this machine's testbench folder (the ledger), if inside
        pats.append(budget.user_dir().resolve().relative_to(root.resolve()).as_posix())
    except ValueError:
        pass
    return list(dict.fromkeys(p for p in pats if p and p != "."))


def snapshot(root: Path, patterns: list[str]) -> dict[str, tuple[int, int]]:
    """{path relative to root, with /: (modification time in ns, size)} for every file not left out."""
    out = {}
    root = str(root)
    for here, dirs, files in os.walk(root):
        rel = os.path.relpath(here, root).replace(os.sep, "/")
        rel = "" if rel == "." else rel + "/"
        dirs[:] = [d for d in dirs if not excluded(rel + d, patterns)]
        for f in files:
            if excluded(rel + f, patterns):
                continue
            try:
                st = os.stat(os.path.join(here, f))
            except OSError:                           # gone meanwhile, or a broken link
                continue
            out[rel + f] = (st.st_mtime_ns, st.st_size)
    return out


def _in_git(root: Path) -> bool:
    try:
        return subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root, capture_output=True,
                              text=True, timeout=30).stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_ignored(root: Path, paths: set[str]) -> set[str]:
    """Which of these paths git ignores (every .gitignore, .git/info/exclude, the global one); none if git fails."""
    try:
        p = subprocess.run(["git", "check-ignore", "-z", "--stdin"], cwd=root, capture_output=True, timeout=30,
                           input="".join(f"{x}\0" for x in sorted(paths)).encode("utf-8", "surrogateescape"))
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if p.returncode not in (0, 1):
        return set()
    return {x for x in p.stdout.decode("utf-8", "surrogateescape").split("\0") if x}


def _names(paths, limit: int = 3) -> str:
    paths = sorted(paths)
    return ", ".join(paths[:limit]) + (f" and {len(paths) - limit} more" if len(paths) > limit else "")


class Stop(Exception):
    def __init__(self, code: int):
        self.code = code


def _interrupt(signum, frame):
    raise KeyboardInterrupt


class Watch:
    def __init__(self, root: Path, cfg: dict, plan: str, approve: bool, interval: float, debounce: float,
                 max_runs: int | None, say):
        self.root, self.cfg, self.plan, self.approve = Path(root), cfg, plan, approve
        self.interval, self.debounce, self.max_runs, self.say = interval, debounce, max_runs, say
        self.id = dt.datetime.now().strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"
        self.git = _in_git(self.root)
        self.patterns = left_out(self.root, cfg, plan)
        self.snap = snapshot(self.root, self.patterns)
        self.pending: set[str] = set()          # changed since the last run started
        self.changed_at = 0.0
        self.current: Path | None = None        # the run going now
        self.during: set[str] = set()           # what changed while it went
        self.last_during: set[str] = set()
        self.echo = 0
        self.statuses: list[str] = []

    # -- files ----------------------------------------------------------------------------------------------

    def look(self) -> None:
        new = snapshot(self.root, self.patterns)
        changed = {p for p in new.keys() | self.snap.keys() if new.get(p) != self.snap.get(p)}
        self.snap = new
        if changed and self.git:
            changed -= git_ignored(self.root, changed)
        if not changed:
            return
        if self.current is not None and not self.pending:
            self.say(f"[watch] {_names(changed)} changed while run {self.current.name} is going: one more run when it "
                     "ends")
        self.pending |= changed
        self.changed_at = time.monotonic()
        if self.current is not None:
            self.during |= changed

    # -- runs -----------------------------------------------------------------------------------------------

    def start(self, changed: list[str]) -> None:
        try:
            cfg = C.load(self.root)                           # testbench.yaml may have changed: the plan as it is now
        except C.ConfigError as exc:
            self.say(f"[watch] no run: {exc} Waiting for the next change.")
            return
        if self.plan not in cfg["plans"]:
            self.say(f"[watch] no run: there is no plan {self.plan} in testbench.yaml any more (plans: "
                     f"{', '.join(cfg['plans'])}). Waiting for the next change.")
            return
        ok, est = free(cfg, self.plan)
        if not ok and not self.approve:
            self.say("[watch] " + refusal(self.plan, est))
            raise Stop(2)
        self.cfg, patterns = cfg, left_out(self.root, cfg, self.plan)
        if patterns != self.patterns:                         # the plan writes other files now: look again without them
            self.patterns, self.snap = patterns, snapshot(self.root, patterns)
        try:
            run_dir = runs.launch(self.root, cfg, self.plan, approve=self.approve,
                                  origin={"watch": self.id, "changed": changed[:20]})
        except runs.LaunchRefused as exc:
            self.say(f"[watch] {exc}\n[watch] Stopped: a run was refused, so watching would only be refused again.")
            raise Stop(2)
        self.current, self.during = run_dir, set()
        why = f"{_names(changed)} changed" if changed else "the first run of this watch"
        self.say(f"[watch] run {run_dir.name} started ({why})")

    def finish(self) -> None:
        self.look()                                           # what the run wrote up to its end was written during it
        run_dir, self.current = self.current, None
        self.last_during = self.during
        rep = R.load(run_dir) or {}
        status = str(runs.read_state(run_dir).get("status") or rep.get("status"))
        self.statuses.append(status)
        prev = runs.previous_report(self.root, run_dir, self.plan)
        try:
            since = (f"since {prev.get('run_id')}: " + D.summary(D.compare(prev, rep)) if prev and rep.get("steps")
                     is not None else "the first run of the plan" if rep else "it left no report")
        except Exception as exc:                              # a line that cannot be worked out never stops the watch
            since = f"not compared with the run before it ({type(exc).__name__})"
        cost = rep.get("cost_usd")
        self.say(f"[watch] {R._icon(status)} run {run_dir.name} in {rep.get('minutes', '?')} min"
                 + (f", ${cost:.3f}" if isinstance(cost, (int, float)) else "") + f"; {since}"
                 + ("" if status == "passed" else f". `testbench report {run_dir.name}` says why"))

    def echoing(self, changed: set[str]) -> bool:
        """A run started only by files the run before it rewrote, ECHOES times in a row: the plan writes them."""
        self.echo = self.echo + 1 if changed and changed == self.last_during else 0
        if self.echo < ECHOES:
            return False
        self.echo = 0
        self.say(f"[watch] not running again: only {_names(changed)} changed, and that changed during each of the "
                 f"last {ECHOES} runs - the plan itself seems to write it. Add it to `exclude:` in testbench.yaml (or "
                 "to .gitignore) so watch leaves it out; if you are editing it, save again to run.")
        return True

    def loop(self) -> int:
        writes = outputs(self.cfg["plans"][self.plan])
        self.say(f"[watch] watching {C.counted(len(self.snap), 'file')} in {self.root} for plan {self.plan}: a run starts "
                 f"{self.debounce:g} s after the last change, one at a time. Left out: exclude:, .gitignore, caches"
                 + (f", and what the plan writes ({_names(writes, 4)})" if writes else "") + ". Ctrl-C stops.")
        self.start([])
        while True:
            began = time.monotonic()
            self.look()
            if self.current is not None and runs.settle(self.current).get("status") in runs.DONE:
                self.finish()
                if self.max_runs and len(self.statuses) >= self.max_runs:
                    return 0 if self.statuses[-1] == "passed" else 1
            if self.current is None and self.pending and time.monotonic() - self.changed_at >= self.debounce:
                changed, self.pending = self.pending, set()
                if not self.echoing(changed):
                    self.start(sorted(changed))
            time.sleep(max(self.interval, 2 * (time.monotonic() - began)))   # a big project is not scanned back to back

    def stop(self) -> None:
        if self.current is not None and runs.read_state(self.current).get("status") not in runs.DONE:
            self.say(f"[watch] stopping: cancelling run {self.current.name}")
            self.say("[watch] " + runs.cancel(self.current))
            self.statuses.append(str(runs.read_state(self.current).get("status")))
        passed = self.statuses.count("passed")
        self.say(f"[watch] stopped after {len(self.statuses)} run(s): {passed} passed, {len(self.statuses) - passed} "
                 "did not" + (f"; the last was {self.statuses[-1]}" if self.statuses else "") + ".")


def watch(root: Path, cfg: dict, plan: str, *, approve: bool = False, interval: float = 0.5, debounce: float = 1.0,
          max_runs: int | None = None, say=None) -> int:
    """Run `plan` now and after every change, until Ctrl-C (or max_runs runs). 0 when stopped (or the last run passed),
    1 when the last of max_runs runs did not pass, 2 when refused."""
    say = say or (lambda line: print(line, flush=True))
    if plan not in cfg["plans"]:
        say(f"No plan {plan!r}. Plans: {', '.join(cfg['plans'])}")
        return 2
    for name, v in (("--interval", interval), ("--debounce", debounce)):
        if not C.number(v, positive=True):
            say(f"{name} is a number of seconds, more than 0; got {v!r}")
            return 2
    ok, est = free(cfg, plan)
    if not ok and not approve:
        say(refusal(plan, est))
        return 2
    w = Watch(root, cfg, plan, approve, float(interval), float(debounce), max_runs, say)
    _signals(signal.default_int_handler, _interrupt)  # SIGTERM, and a console break on Windows, stop it like Ctrl-C
    code = 0
    try:
        code = w.loop()
    except KeyboardInterrupt:
        code = 0
    except Stop as s:
        code = s.code
    finally:
        _signals(signal.SIG_IGN, signal.SIG_IGN)      # a second Ctrl-C must not leave the run's driver going
        try:
            w.stop()
        finally:
            _signals(signal.default_int_handler, signal.SIG_DFL)
    return code


def _signals(on_int, on_stop) -> None:
    try:
        signal.signal(signal.SIGINT, on_int)
        procs.on_stop(on_stop)
    except ValueError:                               # not the main thread (watch called from Python): no handlers
        pass
