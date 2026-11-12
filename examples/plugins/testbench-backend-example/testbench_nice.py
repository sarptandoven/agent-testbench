"""A backend plug-in for agent-testbench, as small as a useful one gets: `backend: nice` runs a plan on this machine
the way `local` does, under `nice -n 10`, so a long test suite or training run leaves the machine usable.

    # testbench.yaml
    nice: {niceness: 10}          # optional: 1 (a little lower) to 19 (only when nothing else wants the CPU)
    plans:
      tests: {backend: nice, steps: ["pytest -q"]}

It is free and not remote: plans run in the project folder and no start-up is priced in, so it needs no hard stop
of its own (a remote backend would need one; `testbench plugin test` checks).
"""
from __future__ import annotations

import os

from agent_testbench.backends import Backend
from agent_testbench.backends.local import LocalBackend
from agent_testbench.config import ConfigError


class NiceRunner(LocalBackend):
    """local's plan runner in a driver process that first lowers its own priority; every step it starts inherits
    it. Built as NiceRunner(root=, cfg=, run_dir=, plan=, meta=); run() returns (report, dollars)."""

    def run(self):
        if hasattr(os, "nice"):               # not on Windows: there the plan runs at normal priority
            os.nice(self.cfg["nice"]["niceness"])
        return super().run()


class Nice(Backend):
    name = "nice"
    remote = False                            # this machine: plans run in place, no start-up to price
    startup_minutes = 0.0
    config_defaults = {"nice": {"niceness": 10}}     # its own section of testbench.yaml

    def validate(self, cfg):
        n = cfg["nice"].get("niceness")
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 19:
            raise ConfigError(f"`nice.niceness` is {n!r}; write a whole number from 1 to 19, e.g. 10")

    def rate_per_minute(self, cfg, hardware):
        return 0.0                            # free: budget.check sees $0

    def plan_runner(self):
        return NiceRunner

    def prices(self, cfg):
        return [f"  free (this machine, under nice -n {cfg['nice']['niceness']})"]

    def doctor(self):                         # (what, ok, detail) lines for `testbench doctor`
        ok = hasattr(os, "nice")
        return [("nice (the nice backend)", ok, "lowers plans' priority" if ok else "not on Windows: normal priority")]


backend = Nice()
