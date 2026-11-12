"""ib_async.ib of the mock ib_async package: IB, connected to the mock's account instead of TWS or IB Gateway.

As with the real IB, a request IB refuses is reported on errorEvent and in the trade's log, not raised (unless
IB.RaiseRequestErrors is set): an order IB would reject comes back as a Trade with status Inactive. The mock logs it as
refused all the same, so `refused: 0` (on by default for a mocked SDK) still fails the step. A live port (7496 TWS,
4001 Gateway) is a live account: refused unless the step's mocks: options say allow_live: true."""
from __future__ import annotations

import enum
import re
import zlib
from datetime import date, datetime, timedelta, timezone

import _testbench_broker as B

from ._mock import BROKER, LIVE_PORTS, PAPER_PORTS, Event, currency
from ._mock import account as _account
from .contract import Contract, Stock
from .objects import (AccountValue, BarData, BarDataList, CommissionReport, Execution, Fill, PortfolioItem, Position,
                      TradeLogEntry)
from .order import UNSET_DOUBLE, Order, OrderStatus, Trade
from .ticker import Ticker

__all__ = ["IB", "RequestError", "StartupFetch", "StartupFetchALL", "StartupFetchNONE"]
HAS = ("connect, disconnect, isConnected, managedAccounts, accountValues, accountSummary, reqAccountSummary, "
       "positions, reqPositions, portfolio, placeOrder (MKT, LMT, STP, STP LMT), cancelOrder, reqGlobalCancel, "
       "trades, openTrades, orders, openOrders, fills, executions, reqOpenOrders, reqAllOpenOrders, reqExecutions, "
       "reqHistoricalData, reqMktData, reqTickers, cancelMktData, qualifyContracts, reqCurrentTime, sleep, "
       "waitOnUpdate, and the events " + ", ".join(("errorEvent", "newOrderEvent", "orderStatusEvent",
                                                     "execDetailsEvent", "commissionReportEvent", "cancelOrderEvent",
                                                     "connectedEvent", "disconnectedEvent")))
BAR_SIZES = {"1 secs": 1, "5 secs": 5, "10 secs": 10, "15 secs": 15, "30 secs": 30, "1 min": 60, "2 mins": 120,
             "3 mins": 180, "5 mins": 300, "10 mins": 600, "15 mins": 900, "20 mins": 1200, "30 mins": 1800,
             "1 hour": 3600, "2 hours": 7200, "3 hours": 10800, "4 hours": 14400, "8 hours": 28800, "1 day": 86400,
             "1 week": 604800, "1 month": 2592000}
WHAT = ("TRADES", "MIDPOINT", "BID", "ASK", "BID_ASK", "ADJUSTED_LAST", "HISTORICAL_VOLATILITY",
        "OPTION_IMPLIED_VOLATILITY", "REBATE_RATE", "FEE_RATE", "YIELD_BID", "YIELD_ASK", "YIELD_BID_ASK",
        "YIELD_LAST", "SCHEDULE", "AGGTRADES")
ORDER_TYPES = {"MKT": "market", "LMT": "limit", "STP": "stop", "STP LMT": "stop_limit"}
TIFS = ("", "DAY", "GTC", "IOC", "FOK", "OPG", "GTD")
_ALL: list = []                      # every Trade of this process (the account is one per process)
_BY_ID: dict = {}                    # the mock's order id -> (IB, Trade)
_CONTRACTS: dict = {}                # a held symbol -> the contract it was bought with
_NEXT = [0]


class RequestError(Exception):
    def __init__(self, reqId: int, code: int, message: str):
        super().__init__(f"API error: {code}: {message}")
        self.reqId, self.code, self.message = reqId, code, message


class StartupFetch(enum.Flag):
    POSITIONS = 1
    ORDERS_OPEN = 2
    ORDERS_COMPLETE = 4
    ACCOUNT_UPDATES = 8
    SUB_ACCOUNT_UPDATES = 16
    EXECUTIONS = 32


StartupFetchNONE = StartupFetch(0)
StartupFetchALL = StartupFetch(63)


def _error(exc: B.Refused) -> Exception:
    if exc.kind == "live":
        return B.LiveAccountRefused(str(exc))
    if exc.kind == "connection":
        return ConnectionRefusedError(str(exc))
    if exc.kind == "argument":
        return exc.info.get("cls", ValueError)(str(exc))
    return RequestError(-1, exc.info.get("code", 0), str(exc))


def _symbol(contract: Contract) -> str:
    """The symbol of the prices a contract is answered from: localSymbol, symbol, symbol+currency (EURUSD),
    symbol/currency (BTC/USD) or symbol.currency, the first the prices have."""
    book = BROKER.book
    names = [contract.localSymbol, contract.symbol, contract.symbol + contract.currency,
             f"{contract.symbol}/{contract.currency}", f"{contract.symbol}.{contract.currency}"]
    for name in names:
        if name and book.has(name):
            return name
    raise B.SetupError(f"mock ib_async: {book.missing(contract.symbol or contract.localSymbol or repr(contract))}. "
                       "Add its bars to the prices file, or the symbol to price:.")


def _args(x):
    """A contract or order as the log shows it: only what is set."""
    if isinstance(x, Contract):
        return {k: v for k, v in (("secType", x.secType), ("symbol", x.symbol), ("localSymbol", x.localSymbol),
                                  ("exchange", x.exchange), ("currency", x.currency)) if v}
    if isinstance(x, Order):
        return {k: v for k, v in (("action", x.action), ("totalQuantity", x.totalQuantity),
                                  ("orderType", x.orderType), ("lmtPrice", x.lmtPrice), ("auxPrice", x.auxPrice),
                                  ("tif", x.tif), ("orderId", x.orderId)) if v not in ("", 0, None, UNSET_DOUBLE)}
    return repr(x)[:200]


def _ib_time(v, now: datetime) -> datetime:
    """endDateTime: '' or None (now), a datetime or date, or IB's 'yyyymmdd hh:mm:ss [zone]' text."""
    if v in ("", None):
        return now
    if isinstance(v, (datetime, date)):
        return B.parse_time(v)
    m = re.fullmatch(r"\s*(\d{8})(?:[ -](\d{2}):(\d{2}):(\d{2}))?(?:\s+(\S+))?\s*", str(v))
    if not m:
        raise ValueError(f"endDateTime {v!r} is not '' or a time like '20240105 16:00:00' (or a datetime)")
    d = datetime.strptime(m.group(1), "%Y%m%d")
    t = d.replace(hour=int(m.group(2) or 0), minute=int(m.group(3) or 0), second=int(m.group(4) or 0))
    zone = timezone.utc
    if m.group(5) and m.group(5) not in ("UTC", "GMT"):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(m.group(5))
        except Exception:
            raise ValueError(f"endDateTime {v!r}: {m.group(5)!r} is not a time zone name") from None
    return t.replace(tzinfo=zone).astimezone(timezone.utc)


def _on_fill(acct, o, f):
    entry = _BY_ID.get(o["id"])
    if entry is None:
        return
    ib, trade = entry
    buy = o["side"] == "buy"
    ex = Execution(execId=f"0000e0d5.{int(f['id']):08x}.01.01", time=f["time"], acctNumber=ib._account,
                   exchange=trade.contract.exchange or "SMART", side="BOT" if buy else "SLD", shares=f["qty"],
                   price=f["price"], permId=trade.order.permId, clientId=trade.order.clientId,
                   orderId=trade.order.orderId, cumQty=f["qty"], avgPrice=f["price"])
    report = CommissionReport(execId=ex.execId, commission=f["fee"], currency=f["currency"],
                              realizedPNL=f["realized"], yield_=0.0, yieldRedemptionDate=0)
    fill = Fill(trade.contract, ex, report, f["time"])
    trade.fills.append(fill)
    st = trade.orderStatus
    st.status, st.filled, st.remaining, st.avgFillPrice, st.lastFillPrice = "Filled", f["qty"], 0.0, f["price"], \
        f["price"]
    trade.log.append(TradeLogEntry(f["time"], "Filled", f"Fill {f['qty']:g}@{f['price']:g}"))
    if buy:
        _CONTRACTS.setdefault(o["asset"], trade.contract)
    ib.execDetailsEvent.emit(trade, fill)
    trade.fillEvent.emit(trade, fill)
    ib.commissionReportEvent.emit(trade, fill, report)
    trade.commissionReportEvent.emit(trade, fill, report)
    ib.orderStatusEvent.emit(trade)
    trade.statusEvent.emit(trade)
    trade.filledEvent.emit(trade)


def _on_cancel(acct, o):
    entry = _BY_ID.get(o["id"])
    if entry is None:
        return
    ib, trade = entry
    trade.orderStatus.status = "Cancelled"
    trade.log.append(TradeLogEntry(BROKER.book.now(), "Cancelled", o["why"] or ""))
    ib.orderStatusEvent.emit(trade)
    trade.statusEvent.emit(trade)
    trade.cancelledEvent.emit(trade)


BROKER.on_fill, BROKER.on_cancel = _on_fill, _on_cancel


class IB:
    """ib_async.IB against the mock's account. connect() on 7497 (TWS) or 4002 (Gateway) is paper; 7496 and 4001 are
    live and refused unless the step's mocks: options say allow_live: true."""
    RequestTimeout = 0.0
    RaiseRequestErrors = False
    MaxSyncedSubAccounts = 50
    TimezoneTWS = None
    events = ("connectedEvent", "disconnectedEvent", "updateEvent", "pendingTickersEvent", "newOrderEvent",
              "orderModifyEvent", "cancelOrderEvent", "openOrderEvent", "orderStatusEvent", "execDetailsEvent",
              "commissionReportEvent", "updatePortfolioEvent", "positionEvent", "accountValueEvent",
              "accountSummaryEvent", "errorEvent", "timeoutEvent")

    def __init__(self):
        for name in self.events:
            setattr(self, name, Event(name))
        self._connected = self._live = self._readonly = False
        self._port, self._client_id, self._account = None, 0, ""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise BROKER.not_modelled("IB.", name, HAS)

    def __repr__(self):
        return f"<IB {'connected to the mock on port ' + str(self._port) if self._connected else 'not connected'}>"

    # ---- plumbing ----------------------------------------------------------------------------------------------

    def _call(self, endpoint, args, answer, request=True, connected=True):
        def run():
            if connected and not self._connected:
                raise B.Refused("argument", "Not connected", cls=ConnectionError)
            return answer()
        return BROKER.call(endpoint, args, run, error=_error, live=self._live, request=request)

    def _request_error(self, code, message, empty, req_id=-1, contract=None):
        """How IB answers a request it refuses: an error event (raised only with RaiseRequestErrors)."""
        self.errorEvent.emit(req_id, code, message, contract)
        if self.RaiseRequestErrors:
            raise B.Refused("request", message, code=code)
        BROKER.refused_quietly("RequestError", f"error {code}: {message}")
        return empty

    # ---- connection ----------------------------------------------------------------------------------------

    def connect(self, host="127.0.0.1", port=7497, clientId=1, timeout=4, readonly=False, account="",
                raiseSyncErrors=False, fetchFields=StartupFetchALL):
        live = port in LIVE_PORTS
        name = account

        def answer():
            if port not in LIVE_PORTS and port not in PAPER_PORTS:
                raise B.Refused("connection", f"mock ib_async: nothing the mock knows listens on port {port}: TWS "
                                              "takes 7497 (paper) or 7496 (live), IB Gateway 4002 (paper) or 4001 "
                                              "(live)")
            BROKER.guard_live(live, f"IB().connect(port={port}), the live port of {LIVE_PORTS.get(port)}")
            self._connected, self._live, self._port, self._readonly, self._client_id = True, live, port, readonly, \
                clientId
            self._account = name or ("U" if live else "DU") + "1234567"
            self.connectedEvent.emit()
            return self
        return BROKER.call("connect", {"host": host, "port": port, "clientId": clientId, "readonly": readonly,
                                       "account": account}, answer, error=_error, live=live, request=False)

    def disconnect(self):
        def answer():
            was, self._connected = self._connected, False
            if was:
                self.disconnectedEvent.emit()
        self._call("disconnect", {}, answer, request=False, connected=False)

    def isConnected(self) -> bool:
        return self._connected

    def managedAccounts(self) -> list:
        return self._call("managedAccounts", {}, lambda: [self._account], request=False)

    @staticmethod
    def sleep(secs: float = 0.02) -> bool:
        """Does not wait: the mock's clock moves with requests (advance_every), not with time."""
        return True

    def waitOnUpdate(self, timeout: float = 0) -> bool:
        return True

    def reqCurrentTime(self) -> datetime:
        return self._call("reqCurrentTime", {}, BROKER.book.now)

    # ---- the account ---------------------------------------------------------------------------------------

    def _values(self, summary: bool) -> list:
        a = _account()
        cur = a.currency
        cash, equity, free = a.held(cur), a.equity(), a.free(cur)
        unrealized = sum((a.value(x) or 0.0) - a.cost.get(x, 0.0) for x in a.holdings())
        rows = [("NetLiquidation", equity), ("TotalCashValue", cash), ("SettledCash", cash), ("AvailableFunds", free),
                ("BuyingPower", free), ("ExcessLiquidity", free), ("EquityWithLoanValue", equity),
                ("GrossPositionValue", equity - cash), ("UnrealizedPnL", unrealized), ("RealizedPnL", a.realized)]
        first = ("AccountType", "INDIVIDUAL") if summary else ("AccountCode", self._account)
        out = [AccountValue(self._account, first[0], first[1], "", "")]
        out += [AccountValue(self._account, tag, f"{v:.2f}", cur, "") for tag, v in rows]
        if not summary:
            out += [AccountValue(self._account, "CashBalance", f"{cash:.2f}", c, "") for c in (cur, "BASE")]
        return out

    def accountValues(self, account: str = "") -> list:
        return self._call("accountValues", {"account": account}, lambda: self._values(False), request=False)

    def accountSummary(self, account: str = "") -> list:
        return self._call("accountSummary", {"account": account}, lambda: self._values(True), request=False)

    def reqAccountSummary(self):
        def answer():
            self._values(True)
        self._call("reqAccountSummary", {}, answer)

    def _contract(self, asset: str) -> Contract:
        c = _CONTRACTS.get(asset)
        if c is None:
            c = _CONTRACTS[asset] = Stock(asset, "SMART", currency(), conId=zlib.crc32(asset.encode()) % 10 ** 8)
        return c

    def _positions(self) -> list:
        a = _account()
        return [Position(self._account, self._contract(x), a.held(x), a.cost.get(x, 0.0) / a.held(x))
                for x in a.holdings()]

    def positions(self, account: str = "") -> list:
        return self._call("positions", {"account": account}, self._positions, request=False)

    def reqPositions(self) -> list:
        return self._call("reqPositions", {}, self._positions)

    def portfolio(self, account: str = "") -> list:
        def answer():
            a = _account()
            out = []
            for x in a.holdings():
                qty, cost = a.held(x), a.cost.get(x, 0.0)
                price = BROKER.book.price(x) or 0.0
                out.append(PortfolioItem(self._contract(x), qty, price, qty * price, cost / qty, qty * price - cost,
                                         0.0, self._account))
            return out
        return self._call("portfolio", {"account": account}, answer, request=False)

    # ---- orders --------------------------------------------------------------------------------------------

    @staticmethod
    def _problem(order: Order):
        """Why IB would refuse an order before it works, as (error code, message), or None."""
        if order.action not in ("BUY", "SELL"):
            return 321, f"Error validating request: action {order.action!r} must be 'BUY' or 'SELL'"
        q = order.totalQuantity
        if isinstance(q, bool) or not isinstance(q, (int, float)) or not q > 0:
            return 321, f"Error validating request: totalQuantity must be a number above 0, got {q!r}"
        if order.orderType not in ORDER_TYPES:
            return 321, (f"Error validating request: orderType {order.orderType!r} is not modelled by the mock; it "
                         "takes MKT, LMT, STP and STP LMT")
        if order.orderType in ("LMT", "STP LMT") and order.lmtPrice in (None, UNSET_DOUBLE):
            return 321, "Error validating request: a limit order needs lmtPrice"
        if order.orderType in ("STP", "STP LMT") and order.auxPrice in (None, UNSET_DOUBLE):
            return 321, "Error validating request: a stop order needs auxPrice (the stop price)"
        if str(order.tif).upper() not in TIFS:
            return 321, f"Error validating request: tif {order.tif!r} is not one of DAY, GTC, IOC, FOK, OPG, GTD"
        if order.whatIf:
            return 321, "whatIf orders are not modelled by the mock"
        return None

    def placeOrder(self, contract: Contract, order: Order) -> Trade:
        if not isinstance(contract, Contract) or not isinstance(order, Order):
            raise BROKER.log_refusal("placeOrder", {"contract": _args(contract), "order": _args(order)}, TypeError(
                "placeOrder(contract, order) takes a Contract (Stock, Forex, Crypto, ...) and an Order (MarketOrder, "
                f"LimitOrder, StopOrder, ...), got {type(contract).__name__} and {type(order).__name__}"), self._live)

        def answer():
            if any(t.order is order or (order.orderId and t.order.orderId == order.orderId) for t in _ALL):
                raise B.Refused("argument", "placeOrder() with an order already placed modifies it, which the mock "
                                            "does not model: cancelOrder() it and place a new one",
                                cls=NotImplementedError)
            _NEXT[0] += 1
            order_id = order.orderId or _NEXT[0]
            order.orderId, order.clientId = order_id, self._client_id
            order.permId = order.permId or 1000000 + order_id
            now = BROKER.book.now()
            trade = Trade(contract, order, OrderStatus(orderId=order_id, status="PendingSubmit",
                                                       remaining=float(order.totalQuantity or 0),
                                                       permId=order.permId, clientId=order.clientId),
                          [], [TradeLogEntry(now, "PendingSubmit", "")])
            _ALL.append(trade)
            self.newOrderEvent.emit(trade)
            why = self._problem(order)
            if why is None and self._readonly:
                why = (321, "Error validating request:-'bD' : cause - The API interface is currently in Read-Only "
                            "mode.")
            if why is None:
                symbol = _symbol(contract)
                key = f"ib-{order_id}"
                _BY_ID[key] = (self, trade)
                lmt = order.lmtPrice if order.orderType in ("LMT", "STP LMT") else None
                aux = order.auxPrice if order.orderType in ("STP", "STP LMT") else None
                try:
                    o = _account().place(symbol=symbol, side=order.action.lower(), qty=float(order.totalQuantity),
                                        type=ORDER_TYPES[order.orderType], limit=lmt, stop=aux,
                                        tif=order.tif or "DAY", make_id=lambda n: key)
                except B.Refused as exc:
                    why = (201, f"Order rejected - reason: {exc}")
                else:
                    if o["status"] == "open":
                        status = "PreSubmitted" if o["stop"] is not None and not o["triggered"] else "Submitted"
                        trade.orderStatus.status = status
                        trade.log.append(TradeLogEntry(now, status, ""))
                        self.orderStatusEvent.emit(trade)
                        trade.statusEvent.emit(trade)
            if why is not None:
                code, message = why
                trade.orderStatus.status = "Inactive"
                trade.log.append(TradeLogEntry(now, "Inactive", message, code))
                self.orderStatusEvent.emit(trade)
                trade.statusEvent.emit(trade)
                return self._request_error(code, message, trade, order_id, contract)
            return trade
        return self._call("placeOrder", {"contract": _args(contract), "order": _args(order)}, answer)

    def cancelOrder(self, order: Order, manualCancelOrderTime: str = ""):
        def answer():
            trade = next((t for t in _ALL if t.order is order or (order.orderId and t.order.orderId == order.orderId)),
                         None)
            if trade is None:
                return self._request_error(10147, f"OrderId {order.orderId} that needs to be cancelled is not found.",
                                           None, order.orderId)
            if trade.isDone():
                return self._request_error(10148, f"OrderId {order.orderId} that needs to be cancelled cannot be "
                                                  f"cancelled, state: {trade.orderStatus.status}.", trade,
                                           order.orderId)
            trade.cancelEvent.emit(trade)
            self.cancelOrderEvent.emit(trade)
            a = _account()
            a.cancel(a.find(f"ib-{trade.order.orderId}"))
            return trade
        return self._call("cancelOrder", {"order": _args(order)}, answer)

    def reqGlobalCancel(self):
        def answer():
            for t in [t for t in _ALL if not t.isDone()]:
                a = _account()
                a.cancel(a.find(f"ib-{t.order.orderId}"))
        self._call("reqGlobalCancel", {}, answer)

    def trades(self) -> list:
        return self._call("trades", {}, lambda: list(_ALL), request=False, connected=False)

    def openTrades(self) -> list:
        return self._call("openTrades", {}, lambda: [t for t in _ALL if not t.isDone()], request=False,
                          connected=False)

    def orders(self) -> list:
        return self._call("orders", {}, lambda: [t.order for t in _ALL], request=False, connected=False)

    def openOrders(self) -> list:
        return self._call("openOrders", {}, lambda: [t.order for t in _ALL if not t.isDone()], request=False,
                          connected=False)

    def fills(self) -> list:
        return self._call("fills", {}, lambda: [f for t in _ALL for f in t.fills], request=False, connected=False)

    def executions(self) -> list:
        return self._call("executions", {}, lambda: [f.execution for t in _ALL for f in t.fills], request=False,
                          connected=False)

    def reqOpenOrders(self) -> list:
        return self._call("reqOpenOrders", {}, lambda: [t for t in _ALL if not t.isDone()])

    def reqAllOpenOrders(self) -> list:
        return self._call("reqAllOpenOrders", {}, lambda: [t for t in _ALL if not t.isDone()])

    def reqExecutions(self, execFilter=None) -> list:
        if execFilter is not None:
            raise BROKER.not_modelled("IB.reqExecutions(execFilter=...): ", "execFilter", "reqExecutions() without "
                                                                                           "a filter")
        return self._call("reqExecutions", {}, lambda: [f for t in _ALL for f in t.fills])

    # ---- market data ---------------------------------------------------------------------------------------

    def qualifyContracts(self, *contracts) -> list:
        def answer():
            for c in contracts:
                symbol = _symbol(c)
                c.conId = c.conId or zlib.crc32(symbol.encode()) % 10 ** 8
                c.exchange = c.exchange or "SMART"
                c.currency = c.currency or currency()
                c.localSymbol = c.localSymbol or c.symbol
            return list(contracts)
        return self._call("qualifyContracts", {"contracts": [_args(c) for c in contracts]}, answer)

    def _ticker(self, contract) -> Ticker:
        symbol = _symbol(contract)
        bar, prev = BROKER.book.bar(symbol), BROKER.book.previous(symbol)
        return Ticker(contract=contract, time=bar.time, marketDataType=1, minTick=0.01, bid=bar.close, bidSize=100.0,
                      ask=bar.close, askSize=100.0, last=bar.close, lastSize=100.0, volume=bar.volume, open=bar.open,
                      high=bar.high, low=bar.low, close=prev if prev is not None else bar.close)

    def reqMktData(self, contract, genericTickList="", snapshot=False, regulatorySnapshot=False, mktDataOptions=[]):
        return self._call("reqMktData", {"contract": _args(contract), "snapshot": snapshot},
                          lambda: self._ticker(contract))

    def reqTickers(self, *contracts, regulatorySnapshot=False) -> list:
        return self._call("reqTickers", {"contracts": [_args(c) for c in contracts]},
                          lambda: [self._ticker(c) for c in contracts])

    def cancelMktData(self, contract) -> bool:
        return self._call("cancelMktData", {"contract": _args(contract)}, lambda: True, request=False)

    def reqHistoricalData(self, contract, endDateTime, durationStr, barSizeSetting, whatToShow, useRTH,
                          formatDate=1, keepUpToDate=False, chartOptions=[], timeout=60) -> BarDataList:
        def answer():
            out = BarDataList()
            out.reqId, out.contract, out.endDateTime, out.durationStr = -1, contract, endDateTime, durationStr
            out.barSizeSetting, out.whatToShow, out.useRTH, out.formatDate = barSizeSetting, whatToShow, useRTH, \
                formatDate
            out.keepUpToDate, out.chartOptions = keepUpToDate, chartOptions
            if barSizeSetting not in BAR_SIZES:
                return self._request_error(321, f"Error validating request: barSizeSetting {barSizeSetting!r} is not "
                                                f"one of {', '.join(BAR_SIZES)}", out, contract=contract)
            m = re.fullmatch(r"(\d+) ([SDWMY])", str(durationStr))
            if not m:
                return self._request_error(321, f"Error validating request: durationStr {durationStr!r} is not an "
                                                "amount and a unit, e.g. '30 D', '1 Y' or '3600 S'", out,
                                           contract=contract)
            if whatToShow not in WHAT:
                return self._request_error(321, f"Error validating request: whatToShow {whatToShow!r} is not one of "
                                                f"{', '.join(WHAT)}", out, contract=contract)
            if keepUpToDate:
                return self._request_error(0, "keepUpToDate=True (bars that keep updating) is not modelled by the "
                                              "mock", out, contract=contract)
            symbol = _symbol(contract)
            try:
                end = _ib_time(endDateTime, BROKER.book.now())
            except ValueError as exc:
                return self._request_error(321, f"Error validating request: {exc}", out, contract=contract)
            span = timedelta(seconds=int(m.group(1)) * {"S": 1, "D": 86400, "W": 604800, "M": 2592000,
                                                        "Y": 31536000}[m.group(2)])
            note = B.spacing_note(BROKER.book, symbol, BAR_SIZES[barSizeSetting], repr(barSizeSetting))
            if note:
                BROKER.note(note)
            daily = BAR_SIZES[barSizeSetting] >= 86400 and formatDate == 1
            for b in BROKER.book.history(symbol, end=end):
                if b.time > end - span:
                    out.append(BarData(date=b.time.date() if daily else b.time, open=b.open, high=b.high, low=b.low,
                                       close=b.close, volume=b.volume, average=b.close, barCount=0))
            return out
        return self._call("reqHistoricalData", {"contract": _args(contract), "endDateTime": str(endDateTime),
                                                "durationStr": durationStr, "barSizeSetting": barSizeSetting,
                                                "whatToShow": whatToShow, "useRTH": useRTH}, answer)
