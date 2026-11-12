"""ib_async.objects of the mock ib_async package: the account, fill and bar objects of ib_async 2.1.0."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, NamedTuple

from .contract import Contract

__all__ = ["AccountValue", "Position", "PortfolioItem", "Fill", "Execution", "CommissionReport", "BarData",
           "BarDataList", "TradeLogEntry"]
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AccountValue(NamedTuple):
    account: str
    tag: str
    value: str
    currency: str
    modelCode: str


class Position(NamedTuple):
    account: str
    contract: Contract
    position: float
    avgCost: float


class PortfolioItem(NamedTuple):
    contract: Contract
    position: float
    marketPrice: float
    marketValue: float
    averageCost: float
    unrealizedPNL: float
    realizedPNL: float
    account: str


@dataclass
class Execution:
    execId: str = ""
    time: datetime = _EPOCH
    acctNumber: str = ""
    exchange: str = ""
    side: str = ""
    shares: float = 0.0
    price: float = 0.0
    permId: int = 0
    clientId: int = 0
    orderId: int = 0
    liquidation: int = 0
    cumQty: float = 0.0
    avgPrice: float = 0.0
    orderRef: str = ""
    evRule: str = ""
    evMultiplier: float = 0.0
    modelCode: str = ""
    lastLiquidity: int = 0
    pendingPriceRevision: bool = False


@dataclass
class CommissionReport:
    execId: str = ""
    commission: float = 0.0
    currency: str = ""
    realizedPNL: float = 0.0
    yield_: float = 0.0
    yieldRedemptionDate: int = 0


class Fill(NamedTuple):
    contract: Contract
    execution: Execution
    commissionReport: CommissionReport
    time: datetime


@dataclass
class BarData:
    date: Any = _EPOCH
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: float = 0
    average: float = 0.0
    barCount: int = 0


@dataclass
class TradeLogEntry:
    time: datetime
    status: str = ""
    message: str = ""
    errorCode: int = 0


class BarDataList(list):
    """The bars reqHistoricalData answers, with the request's settings as attributes."""
    reqId: int
    contract: Contract
    endDateTime: Any
    durationStr: str
    barSizeSetting: str
    whatToShow: str
    useRTH: bool
    formatDate: int
    keepUpToDate: bool
    chartOptions: list

    def __eq__(self, other):
        return self is other

    __hash__ = object.__hash__

