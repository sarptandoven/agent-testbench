"""agent-testbench from Python: plans, runs, live sessions and spending, for scripts, notebooks and agents of your
own - the same code paths as the CLI, so the guardrails are the same.

    from agent_testbench import Testbench, NeedsApproval
    tb = Testbench("path/to/project")
    run = tb.run("smoke")                     # follows it to the end; wait=0 returns at once
    print(run.status, run.report_md)
    with tb.session() as s:                   # a live kernel; stopped on leaving the block
        print(s.exec("import sys; sys.version").text)

Every launch goes through the budget check the CLI uses: over a limit raises Refused, above the approval threshold
NeedsApproval. approve=True means a person said yes to that cost, exactly like `--approve`: it never gets past
max_run_usd, a day's budget or this machine's limits, and nothing here can raise a limit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from . import budget, config as C, notebook as NB, provenance, report as R, runs as RUNS


class TestbenchError(Exception):
    """Something testbench could not do; the message says what and what to do instead."""
    __test__ = False


class Refused(TestbenchError):
    """A launch over a limit (max_run_usd, a day's budget, this machine's limits). `reason` says which."""

    def __init__(self, reason: str, estimate: dict | None = None):
        super().__init__(reason)
        self.reason, self.estimate = reason, estimate or {}
        self.what = self.estimate.get("what")
        self.hardware = " ".join(x for x in (self.estimate.get("backend"), self.estimate.get("gpu")) if x) or None
        self.worst_case_usd = self.estimate.get("worst_case_usd")


class NeedsApproval(Refused):
    """A launch above the approval threshold: ask a person about `worst_case_usd` for `what` on `hardware`; if they
    agree, launch again with approve=True."""


def _launch_error(message: str, estimate) -> TestbenchError:
    if message.startswith("Needs approval:"):
        return NeedsApproval(message, estimate())
    if message.startswith("Refused:"):
        return Refused(message, estimate())
    return TestbenchError(message)


class _plain:
    """Turns testbench's own errors into TestbenchError with the same message."""

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        from .session_backends import SessionError
        if kind is not None and issubclass(kind, (C.ConfigError, SessionError, FileNotFoundError, NB.NotebookError)):
            raise TestbenchError(str(exc)) from None
        return False


class Testbench:
    """A project: the folder holding testbench.yaml (or any folder below it)."""
    __test__ = False

    def __init__(self, root: str | Path = "."):
        with _plain():
            self.root = C.find_root(root)

    def __repr__(self):
        return f"Testbench({str(self.root)!r})"

    def _cfg(self) -> dict:
        with _plain():
            return C.load(self.root)                 # read each time, as the CLI does: limits come from disk

    def plans(self) -> dict:
        """{name: {backend, gpu, description, steps, max_minutes, worst_case_usd}}."""
        cfg = self._cfg()
        out = {}
        for name, plan in cfg["plans"].items():
            est = budget.worst_case(cfg, name)
            out[name] = {"backend": plan["backend"], "gpu": plan.get("gpu"), "description": plan.get("description", ""),
                         "steps": [s["name"] for s in plan["steps"]], "max_minutes": est["max_minutes"],
                         "worst_case_usd": est["worst_case_usd"]}
        return out

    def estimate(self, plan: str, approve: bool = False) -> dict:
        """The worst case of a plan and whether it may launch now: what `testbench estimate --json` prints."""
        cfg = self._cfg()
        if plan not in cfg["plans"]:
            raise TestbenchError(C.no_plan(cfg, plan))
        with _plain():
            ok, message, est = budget.check_launch(cfg, plan, approve)
        return {**est, "allowed": ok, "message": message}

    def run(self, plan: str, approve: bool = False, wait: float | None = None, on_line=None) -> "Run":
        """Launch a plan inside the budget and follow it: wait=None until it ends, wait=0 not at all, else that many
        seconds. on_line gets each progress line. approve=True only after a person agreed to the cost."""
        cfg = self._cfg()
        try:
            run_dir = RUNS.launch(self.root, cfg, plan, approve=approve)
        except (RUNS.LaunchRefused, C.ConfigError) as exc:
            raise _launch_error(str(exc), lambda: budget.worst_case(cfg, plan)) from None
        run = Run(self, run_dir)
        if wait is None or wait > 0:
            run.wait(wait, on_line=on_line)
        return run

    def check(self, plans: list[str] | None = None, on_line=None) -> dict:
        """`testbench check` here: run every plan that is free and needs no approval (or only `plans`); the others are
        listed with their worst case, never launched. Returns its summary: each plan's status, run id and why it did
        not run, the run ids, `next` and `exit_code`. on_line gets each plan's line as it is settled.
        api.check(folder) does the same in a folder with no testbench.yaml."""
        return check(self.root, plans, on_line)

    def runs(self, n: int = 10) -> list["Run"]:
        """The latest n runs, oldest first."""
        return [Run(self, p) for p in RUNS.all_runs(self.root)[-n:]]

    def get_run(self, run_id: str | None = None) -> "Run":
        """A run by id or unique prefix; None: the latest."""
        with _plain():
            return Run(self, RUNS.resolve(self.root, run_id))

    def verify(self, run_id: str | None = None) -> dict:
        """Whether a run's evidence files still match the sha256 its report recorded: {run, ok, problems, message,
        source}."""
        run = self.get_run(run_id)
        rep = run.report
        if not rep or "evidence" not in rep:
            raise TestbenchError(f"{run.id} has no recorded evidence to check (it ran before agent-testbench recorded "
                                 "hashes, or never finished)")
        problems, data = provenance.verify(run.dir, rep), []
        if not problems and isinstance(rep.get("data"), dict):      # and the data it read is still the same
            from . import dataseal
            problems = data = dataseal.verify(self.root, rep)[0]
        src = rep.get("source") or {}
        where = (f"commit {src['commit'][:12]}" + (" with uncommitted changes" if src.get("dirty") else "")) if src \
            else "code outside git"
        message = (f"{run.id}: " + "; ".join(data)) if data else \
            (f"{run.id}: the evidence no longer matches its report: " + "; ".join(problems)) if problems else \
            (f"{run.id}: all {len(rep['evidence'])} evidence files match their recorded sha256 "
             f"({rep['evidence_sha256'][:16]}); ran on {where}")
        return {"run": run.id, "ok": not problems, "problems": problems, "message": message, "source": src or None}

    def spend(self) -> dict:
        """What runs and sessions have cost, against the project's limits (capped by this machine's)."""
        cfg = self._cfg()
        with _plain():
            lim, machine = budget.limits(cfg), budget.user_limits()
        finished = [e for e in budget.entries(self.root) if e["event"] == "finish"]
        return {"today_usd": budget.spent_today(self.root), "all_time_usd": round(sum(e["usd"] for e in finished), 4),
                "finished": len(finished), "limits": {k: v for k, (v, _) in lim.items()},
                "limits_set_in": {k: where for k, (_, where) in lim.items()},
                "machine_today_usd": budget.spent_today_everywhere(), "machine_limits": machine,
                "recent": finished[-10:]}

    def session(self, name: str = "main", backend: str | None = None, gpu: str | None = None,
                idle_minutes: float | None = None, max_minutes: float | None = None, secrets: list[str] | None = None,
                approve: bool = False) -> "Session":
        """Start a live session inside the budget (worst case: max_minutes on its hardware). It stops itself after
        idle_minutes unused and at max_minutes; use it in a `with` block to stop it on leaving."""
        from . import sessions as S
        from .session_backends import SessionError
        cfg = self._cfg()

        def estimate():
            longest = float(max_minutes or cfg["sessions"]["max_minutes"])
            return budget.estimate(cfg, S.hardware(cfg, backend, gpu), longest, f"session {name}")
        try:
            S.start(self.root, cfg, name, backend=backend, gpu=gpu, idle_minutes=idle_minutes, max_minutes=max_minutes,
                    secrets=secrets, approve=approve)
        except (SessionError, C.ConfigError) as exc:
            raise _launch_error(str(exc), estimate) from None
        return Session(self, name)

    def sessions(self) -> list["Session"]:
        """Every session of the project, running or stopped (to pick up one started elsewhere)."""
        from . import sessions as S
        return [Session(self, d.name) for d in S.all_sessions(self.root)]


def check(root: str | Path = ".", plans: list[str] | None = None, on_line=None) -> dict:
    """`testbench check` in any folder: with a testbench.yaml at or above it, as Testbench.check; with none, the plans
    `testbench init` would write for what it finds there, run on this machine for free (afterwards Testbench(root)
    works there too). Returns the summary `testbench check --json` prints; nothing is raised for a failed plan."""
    from . import check as CK

    def show(event, entry):
        if on_line is not None and event == "end":
            on_line(CK.line(entry))
    with _plain():
        if not Path(root).expanduser().is_dir():
            raise TestbenchError(f"{root} is not a folder; give check() the project's folder")
        return CK.run(Path(root).expanduser(), plans, show)


class Run:
    """A run of a plan. Its state is read from disk each time, so it also follows runs started elsewhere."""

    def __init__(self, tb: Testbench, run_dir: Path):
        self._tb, self.dir, self.id = tb, Path(run_dir), Path(run_dir).name

    def __repr__(self):
        return f"<Run {self.id} {self.status}>"

    @property
    def state(self) -> dict:
        return RUNS.read_state(self.dir)

    @property
    def status(self) -> str:
        """queued, running, passed, failed, cancelled or error."""
        return self.state.get("status")

    @property
    def done(self) -> bool:
        return self.status in RUNS.DONE

    @property
    def passed(self) -> bool:
        return self.status == "passed"

    @property
    def cost_usd(self) -> float | None:
        return self.state.get("cost_usd")

    def wait(self, timeout: float | None = None, on_line=None) -> "Run":
        """Follow the run until it ends or timeout seconds pass (None: until it ends)."""
        RUNS.wait(self.dir, math.inf if timeout is None else timeout, on_line=on_line or (lambda line: None))
        return self

    @property
    def report(self) -> dict | None:
        """The report (see schema/report-v1.schema.json) once the run has ended, else None."""
        return R.load(self.dir)

    @property
    def report_md(self) -> str:
        """The report as Markdown, failures first; one status line while it runs."""
        md = self.dir / "report.md"
        return md.read_text() if md.exists() else RUNS.status_line(self.dir)

    @property
    def images(self) -> list[Path]:
        """Evidence images (plots, screenshots) of the finished run."""
        rep = self.report or {}
        return [self.dir / s["dir"] / name for s in rep.get("steps", []) if s.get("dir")
                for name in s.get("images", []) + (s.get("web") or {}).get("screenshots", [])]

    def cancel(self) -> str:
        """Stop the run, here and on the remote machine."""
        return RUNS.cancel(self.dir)

    def verify(self) -> dict:
        return self._tb.verify(self.id)


@dataclass
class ExecResult:
    """What one piece of code did in a session: `text` as an agent reads it, `images` it displayed (saved files)."""
    raw: dict
    text: str
    images: list[Path] = field(default_factory=list)

    @property
    def status(self) -> str:
        return self.raw["status"]                    # ok, error or timeout

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def result(self) -> str | None:
        return self.raw.get("result")                 # the last expression's value, as text

    @property
    def stdout(self) -> str:
        return self.raw.get("stdout", "")

    @property
    def error(self) -> dict | None:
        return self.raw.get("error")

    def __str__(self):
        return self.text


class Session:
    """A live session: a kernel that keeps its variables between calls. One call at a time per session."""

    def __init__(self, tb: Testbench, name: str):
        from . import sessions as S
        self._tb, self.name, self.dir = tb, name, S.sessions_dir(tb.root) / name

    def __repr__(self):
        return f"<Session {self.name} {self.status}>"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    def _call(self, fn, *args):
        cfg = self._tb._cfg()
        with _plain():
            return fn(self._tb.root, cfg, self.dir, *args)

    @property
    def state(self) -> dict:
        from . import sessions as S
        with _plain():
            return S.read(self.dir)

    @property
    def status(self) -> str:
        return self.state.get("status")              # starting, ready, stopped or error

    def describe(self) -> str:
        from . import sessions as S
        return S.describe(self.dir)

    def exec(self, code: str | None = None, *, file: str | Path | None = None, timeout: float = 600) -> ExecResult:
        """Run code (or a .py file) in the kernel: what it printed, the last value, images, the error and its line.
        Past timeout seconds the code is interrupted; the kernel and its variables stay."""
        from . import sessions as S
        if (code is None) == (file is None):
            raise TestbenchError("pass code or file= (one of them)")
        label = None
        if file is not None:
            with _plain():
                label, code = str(file), (self._tb.root / file).read_text()
        return _result(self._call(S.exec_code, code, timeout, label))

    def cells(self, notebook: str | Path, cells: str | None = None, params: dict | None = None,
              timeout: float = 600) -> list[ExecResult]:
        """Run a notebook's code cells (e.g. cells="1-3"; numbered from 1) with form fields set; stops at the first
        error."""
        from . import sessions as S
        return [_result(r) for r in self._call(S.exec_cells, self._tb.root / notebook, cells, params or {}, timeout)]

    def ls(self, path: str = ".") -> list[str]:
        from . import sessions as S
        out = self._call(S.files, "ls", path)
        return out.splitlines() if out else []

    def get(self, remote: str, local: str | Path | None = None) -> str:
        """Copy a file from the session's machine (default: into the session's outputs folder)."""
        from . import sessions as S
        return self._call(S.files, "get", remote, *([str(local)] if local else []))

    def put(self, local: str | Path, remote: str) -> str:
        from . import sessions as S
        return self._call(S.files, "put", str(local), remote)

    def install(self, *packages: str) -> str:
        """pip install on the session's machine (pin versions)."""
        from . import sessions as S
        return self._call(S.install, list(packages))

    def shell(self, command: str, timeout: float = 120) -> str:
        from . import sessions as S
        return self._call(S.shell, command, timeout)

    def sync(self) -> str:
        """Copy the project's files to the session's machine again (the kernel keeps its variables)."""
        from . import sessions as S
        return self._call(S.sync)

    def stop(self) -> dict:
        """Stop it and record what it cost: {status, cost_usd, minutes, stop_reason}."""
        from . import sessions as S
        s = self._call(S.stop)
        began = s.get("started") or s.get("created") or 0
        return {"status": s.get("status"), "cost_usd": s.get("cost_usd", 0.0), "stop_reason": s.get("stop_reason"),
                "minutes": round((s["stopped"] - began) / 60, 2) if s.get("stopped") and began else None}


def _result(raw: dict) -> ExecResult:
    from .cli import render_exec
    return ExecResult(raw=raw, text=render_exec(raw), images=[Path(p) for p in raw.get("images", [])])
