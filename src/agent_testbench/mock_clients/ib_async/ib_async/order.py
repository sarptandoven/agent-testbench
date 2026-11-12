"""ib_async.order of the mock ib_async package: Order (every keyword of ib_async 2.1.0's Order, with its defaults),
MarketOrder, LimitOrder, StopOrder, StopLimitOrder, OrderStatus and Trade."""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from typing import ClassVar

from ._mock import Event
from .contract import Contract

__all__ = ["Order", "MarketOrder", "LimitOrder", "StopOrder", "StopLimitOrder", "OrderStatus", "Trade"]
UNSET_DOUBLE = sys.float_info.max
UNSET_INTEGER = 2 ** 31 - 1
_FIELDS = {
    "orderId": 0, "clientId": 0, "permId": 0, "action": "", "totalQuantity": 0.0, "orderType": "",
    "lmtPrice": UNSET_DOUBLE, "auxPrice": UNSET_DOUBLE, "tif": "", "activeStartTime": "", "activeStopTime": "",
    "ocaGroup": "", "ocaType": 0, "orderRef": "", "transmit": True, "parentId": 0, "blockOrder": False,
    "sweepToFill": False, "displaySize": 0, "triggerMethod": 0, "outsideRth": False, "hidden": False,
    "goodAfterTime": "", "goodTillDate": "", "rule80A": "", "allOrNone": False, "minQty": UNSET_INTEGER,
    "percentOffset": UNSET_DOUBLE, "overridePercentageConstraints": False, "trailStopPrice": UNSET_DOUBLE,
    "trailingPercent": UNSET_DOUBLE, "faGroup": "", "faProfile": "", "faMethod": "", "faPercentage": "",
    "designatedLocation": "", "openClose": "O", "origin": 0, "shortSaleSlot": 0, "exemptCode": -1,
    "discretionaryAmt": 0.0, "eTradeOnly": False, "firmQuoteOnly": False, "nbboPriceCap": UNSET_DOUBLE,
    "optOutSmartRouting": False, "auctionStrategy": 0, "startingPrice": UNSET_DOUBLE, "stockRefPrice": UNSET_DOUBLE,
    "delta": UNSET_DOUBLE, "stockRangeLower": UNSET_DOUBLE, "stockRangeUpper": UNSET_DOUBLE, "randomizePrice": False,
    "randomizeSize": False, "volatility": UNSET_DOUBLE, "volatilityType": UNSET_INTEGER, "deltaNeutralOrderType": "",
    "deltaNeutralAuxPrice": UNSET_DOUBLE, "deltaNeutralConId": 0, "deltaNeutralSettlingFirm": "",
    "deltaNeutralClearingAccount": "", "deltaNeutralClearingIntent": "", "deltaNeutralOpenClose": "",
    "deltaNeutralShortSale": False, "deltaNeutralShortSaleSlot": 0, "deltaNeutralDesignatedLocation": "",
    "continuousUpdate": False, "referencePriceType": UNSET_INTEGER, "basisPoints": UNSET_DOUBLE,
    "basisPointsType": UNSET_INTEGER, "scaleInitLevelSize": UNSET_INTEGER, "scaleSubsLevelSize": UNSET_INTEGER,
    "scalePriceIncrement": UNSET_DOUBLE, "scalePriceAdjustValue": UNSET_DOUBLE,
    "scalePriceAdjustInterval": UNSET_INTEGER, "scaleProfitOffset": UNSET_DOUBLE, "scaleAutoReset": False,
    "scaleInitPosition": UNSET_INTEGER, "scaleInitFillQty": UNSET_INTEGER, "scaleRandomPercent": False,
    "scaleTable": "", "hedgeType": "", "hedgeParam": "", "account": "", "settlingFirm": "", "clearingAccount": "",
    "clearingIntent": "", "algoStrategy": "", "algoParams": [], "smartComboRoutingParams": [], "algoId": "",
    "whatIf": False, "notHeld": False, "solicited": False, "modelCode": "", "orderComboLegs": [],
    "orderMiscOptions": [], "referenceContractId": 0, "peggedChangeAmount": 0.0, "isPeggedChangeAmountDecrease": False,
    "referenceChangeAmount": 0.0, "referenceExchangeId": "", "adjustedOrderType": "", "triggerPrice": UNSET_DOUBLE,
    "adjustedStopPrice": UNSET_DOUBLE, "adjustedStopLimitPrice": UNSET_DOUBLE, "adjustedTrailingAmount": UNSET_DOUBLE,
    "adjustableTrailingUnit": 0, "lmtPriceOffset": UNSET_DOUBLE, "conditions": [], "conditionsCancelOrder": False,
    "conditionsIgnoreRth": False, "extOperator": "", "softDollarTier": None, "cashQty": UNSET_DOUBLE,
    "mifid2DecisionMaker": "", "mifid2DecisionAlgo": "", "mifid2ExecutionTrader": "", "mifid2ExecutionAlgo": "",
    "dontUseAutoPriceForHedge": False, "isOmsContainer": False, "discretionaryUpToLimitPrice": False,
    "autoCancelDate": "", "filledQuantity": UNSET_DOUBLE, "refFuturesConId": 0, "autoCancelParent": False,
    "shareholder": "", "imbalanceOnly": False, "routeMarketableToBbo": False, "parentPermId": 0,
    "usePriceMgmtAlgo": False, "duration": UNSET_INTEGER, "postToAts": UNSET_INTEGER, "advancedErrorOverride": "",
    "manualOrderTime": "", "minTradeQty": UNSET_INTEGER, "minCompeteSize": UNSET_INTEGER,
    "competeAgainstBestOffset": UNSET_DOUBLE, "midOffsetAtWhole": UNSET_DOUBLE, "midOffsetAtHalf": UNSET_DOUBLE}


class Order:
    """An order: any keyword of the real Order (an unknown one is the TypeError the real dataclass raises)."""

    def __init__(self, **kwargs):
        for k in kwargs:
            if k not in _FIELDS:
                raise TypeError(f"{type(self).__name__}.__init__() got an unexpected keyword argument {k!r}")
        for k, default in _FIELDS.items():
            setattr(self, k, copy.copy(default))
        for k, v in kwargs.items():
            setattr(self, k, v)

    def __repr__(self):
        shown = {k: getattr(self, k) for k, d in _FIELDS.items() if getattr(self, k) != d}
        return f"{type(self).__name__}({', '.join(f'{k}={v!r}' for k, v in shown.items())})"

    __eq__ = object.__eq__
    __hash__ = object.__hash__


class MarketOrder(Order):
    def __init__(self, action: str, totalQuantity: float, **kwargs):
        Order.__init__(self, orderType="MKT", action=action, totalQuantity=totalQuantity, **kwargs)


class LimitOrder(Order):
    def __init__(self, action: str, totalQuantity: float, lmtPrice: float, **kwargs):
        Order.__init__(self, orderType="LMT", action=action, totalQuantity=totalQuantity, lmtPrice=lmtPrice, **kwargs)


class StopOrder(Order):
    def __init__(self, action: str, totalQuantity: float, stopPrice: float, **kwargs):
        Order.__init__(self, orderType="STP", action=action, totalQuantity=totalQuantity, auxPrice=stopPrice, **kwargs)


class StopLimitOrder(Order):
    def __init__(self, action: str, totalQuantity: float, lmtPrice: float, stopPrice: float, **kwargs):
        Order.__init__(self, orderType="STP LMT", action=action, totalQuantity=totalQuantity, lmtPrice=lmtPrice,
                       auxPrice=stopPrice, **kwargs)


@dataclass
class OrderStatus:
    orderId: int = 0
    status: str = ""
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0
    permId: int = 0
    parentId: int = 0
    lastFillPrice: float = 0.0
    clientId: int = 0
    whyHeld: str = ""
    mktCapPrice: float = 0.0

    @property
    def total(self) -> float:
        return self.filled + self.remaining

    PendingSubmit: ClassVar[str] = "PendingSubmit"
    PendingCancel: ClassVar[str] = "PendingCancel"
    PreSubmitted: ClassVar[str] = "PreSubmitted"
    Submitted: ClassVar[str] = "Submitted"
    ApiPending: ClassVar[str] = "ApiPending"
    ApiCancelled: ClassVar[str] = "ApiCancelled"
    ApiUpdate: ClassVar[str] = "ApiUpdate"
    Cancelled: ClassVar[str] = "Cancelled"
    Filled: ClassVar[str] = "Filled"
    Inactive: ClassVar[str] = "Inactive"
    ValidationError: ClassVar[str] = "ValidationError"
    DoneStates: ClassVar[frozenset] = frozenset(["Filled", "Cancelled", "ApiCancelled", "Inactive"])
    ActiveStates: ClassVar[frozenset] = frozenset(["PendingSubmit", "ApiPending", "PreSubmitted", "Submitted",
                                                   "ValidationError", "ApiUpdate"])
    WaitingStates: ClassVar[frozenset] = frozenset(["PendingSubmit", "ApiPending", "PreSubmitted"])
    WorkingStates: ClassVar[frozenset] = frozenset(["Submitted", "ValidationError", "ApiUpdate"])


@dataclass
class Trade:
    """An order, its status and its fills, kept up to date by the mock as the order fills or is cancelled."""
    contract: Contract = field(default_factory=Contract)
    order: Order = field(default_factory=Order)
    orderStatus: OrderStatus = field(default_factory=OrderStatus)
    fills: list = field(default_factory=list)
    log: list = field(default_factory=list)
    advancedError: str = ""
    events: ClassVar = ("statusEvent", "modifyEvent", "fillEvent", "commissionReportEvent", "filledEvent",
                        "cancelEvent", "cancelledEvent")

    def __post_init__(self):
        for name in self.events:
            setattr(self, name, Event(name))

    def isWaiting(self) -> bool:
        return self.orderStatus.status in OrderStatus.WaitingStates

    def isWorking(self) -> bool:
        return self.orderStatus.status in OrderStatus.WorkingStates

    def isActive(self) -> bool:
        return self.orderStatus.status in OrderStatus.ActiveStates

    def isDone(self) -> bool:
        return self.orderStatus.status in OrderStatus.DoneStates

    def filled(self) -> float:
        return sum(f.execution.shares for f in self.fills)

    def remaining(self) -> float:
        return float(self.order.totalQuantity) - self.filled()

