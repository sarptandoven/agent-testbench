"""ib_async.ticker of the mock ib_async package: Ticker, filled from the current bar (last = its close, bid = ask =
last, close = the previous bar's close, as IB's close is the previous session's)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contract import Contract

__all__ = ["Ticker"]
nan = float("nan")


@dataclass
class Ticker:
    contract: Contract | None = None
    time: datetime | None = None
    timestamp: float | None = None
    marketDataType: int = 1
    minTick: float = nan
    bid: float = nan
    bidSize: float = nan
    bidExchange: str = ""
    ask: float = nan
    askSize: float = nan
    askExchange: str = ""
    last: float = nan
    lastSize: float = nan
    lastExchange: str = ""
    lastTimestamp: Any = None
    prevBid: float = nan
    prevBidSize: float = nan
    prevAsk: float = nan
    prevAskSize: float = nan
    prevLast: float = nan
    prevLastSize: float = nan
    volume: float = nan
    open: float = nan
    high: float = nan
    low: float = nan
    close: float = nan
    vwap: float = nan

    def hasBidAsk(self) -> bool:
        return self.bid != -1 and not math.isnan(self.bid) and self.ask != -1 and not math.isnan(self.ask)

    def midpoint(self) -> float:
        return (self.bid + self.ask) * 0.5 if self.hasBidAsk() else nan

    def marketPrice(self) -> float:
        if self.hasBidAsk() and self.bid <= self.last <= self.ask:
            return self.last
        return self.midpoint() if self.hasBidAsk() else self.last

    __hash__ = object.__hash__
