"""The mock ib_async package's own parts: its broker (prices, the account, the call log) and a small Event like
eventkit's (connect with +=, disconnect with -=, emit)."""
from __future__ import annotations

import _testbench_broker as B

BROKER = B.Broker("ib_async")
LIVE_PORTS = {7496: "TWS", 4001: "IB Gateway"}
PAPER_PORTS = {7497: "TWS", 4002: "IB Gateway"}


class Event:
    """eventkit's Event, as far as trading code uses it: `ev += handler`, `ev -= handler`, `ev.emit(*args)`."""

    def __init__(self, name: str = ""):
        self.name, self._slots = name, []

    def connect(self, c, error=None, done=None, keep_ref=False):
        self._slots.append(c)
        return self

    def disconnect(self, c, error=None, done=None):
        if c in self._slots:
            self._slots.remove(c)
        return self

    __iadd__ = connect
    __isub__ = disconnect

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)

    def clear(self):
        self._slots.clear()

    def __len__(self):
        return len(self._slots)

    def __repr__(self):
        return f"Event<{self.name}, {self._slots}>"


def currency() -> str:
    return BROKER.opts.get("currency", "USD")


def account() -> B.Account:
    cur = currency()
    a = BROKER.account("ib", cur, lambda symbol: (symbol, cur), lambda asset: asset)
    BROKER.use(a)
    return a
