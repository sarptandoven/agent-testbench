"""The background process behind one run: hands the plan to its backend, then closes the run out.

    python -m agent_testbench.driver .testbench/runs/<run-id>

SIGTERM (from `testbench cancel`) stops the work, cancels anything remote, and still writes the report.
"""
from __future__ import annotations

import json
import signal
import sys
import time
import traceback
from pathlib import Path

from . import config as C, runs


class Cancelled(Exception):
    pass


def _on_term(signum, frame):
    raise KeyboardInterrupt


def drive(run_dir: Path) -> dict:
    run_dir = Path(run_dir).resolve()
    root = run_dir.parents[2]
    state = runs.read_state(run_dir)
    plan = json.loads((run_dir / "plan.json").read_text())
    cfg = C.load(root)
    meta = {"run_id": run_dir.name, "plan": state["plan"], "backend": state["backend"], "gpu": state.get("gpu")}
    runs.write_state(run_dir, status="running", started=time.time())
    from .backends import get_backend
    backend = get_backend(state["backend"])(root=root, cfg=cfg, run_dir=run_dir, plan=plan, meta=meta)
    try:
        rep, cost = backend.run()
    except KeyboardInterrupt:
        msg = backend.cancel()
        rep = _partial(run_dir, meta, "cancelled", f"cancelled by request{'; ' + msg if msg else ''}")
        cost = backend.cost(rep)
    except Exception as exc:
        rep = _partial(run_dir, meta, "error", f"{type(exc).__name__}: {exc}")
        rep["launch_error"] = f"{type(exc).__name__}: {exc}\n\n```\n{traceback.format_exc()[-3000:]}\n```"
        try:
            backend.cancel()
        except Exception:
            pass
        cost = backend.cost(rep)
    return runs.finalize(root, run_dir, rep, cost)


def _partial(run_dir: Path, meta: dict, status: str, why: str) -> dict:
    from . import report as R
    rep = R.load(run_dir) or {**meta, "steps": [], "started": runs.read_state(run_dir).get("started", time.time())}
    rep["status"] = status
    rep["stopped_because"] = why
    rep.setdefault("finished", time.time())
    rep["minutes"] = round((rep["finished"] - rep.get("started", rep["finished"])) / 60, 2)
    rep.setdefault("skipped", [])
    return rep


def main():
    signal.signal(signal.SIGTERM, _on_term)
    rep = drive(Path(sys.argv[1]))
    print(f"[testbench] run finished: {rep['status']}", flush=True)


if __name__ == "__main__":
    main()
