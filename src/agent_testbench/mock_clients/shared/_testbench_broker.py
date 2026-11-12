"""What the mock broker SDKs share (`alpaca`, `ccxt`, `ib_async`; docs/mocks.md#brokers): the prices they answer
from, one account per broker with its cash, positions and orders, deterministic fills, the live-account guardrail and
the call log (the same mock_calls.jsonl as the LLM mocks). Standard library only, so it runs in whatever Python the
step uses; nothing here opens a network connection.

    TESTBENCH_MOCK_ALPACA / TESTBENCH_MOCK_CCXT / TESTBENCH_MOCK_IB_ASYNC
        JSON options: {"prices": "/abs/prices.csv", "cash": 100000, "fee_usd": 1, "slippage_bps": 5, ...}
    TESTBENCH_MOCK_CALLS   where the call log goes; by default $TESTBENCH_STEP_DIR/mock_calls.jsonl, else ./

Fills follow one rule for every broker: a market order fills at once at the current bar's close moved against the
trader by slippage_bps; a limit order fills when the close crosses its limit (at the close with slippage, never past
the limit); a stop order becomes a market order once the close reaches its stop. Fees are fee_usd per filled order
plus fee_bps of its value, paid in the cash currency. Cash never goes below zero and nothing is sold short.
"""
from __future__ import annotations

import bisect
import csv
import difflib
import json
import math
import os
import re
import statistics
import time
from datetime import date, datetime, timedelta, timezone

import _testbench_mock as core

SDKS = ("alpaca", "ccxt", "ib_async")
OPTION_KEYS = ("prices", "price", "now", "advance_every", "cash", "currency", "positions", "fee_usd", "fee_bps",
               "slippage_bps", "allow_live")
DOCS = "docs/mocks.md#brokers"
LIVE_RULE = "trading code under test must use paper/sandbox, see docs/mocks.md#brokers"
EPS = 1e-9


class SetupError(core.SetupError):
    """The mock's own settings or prices file are wrong (not something the broker would say)."""


class Refused(Exception):
    """What the broker (or its SDK) would refuse. kind picks the SDK's error: argument (the SDK's own TypeError or
    ValueError), invalid, funds, qty, not_found, symbol, state (an order that cannot be cancelled), auth, live,
    unsupported."""

    def __init__(self, kind, message, **info):
        super().__init__(message)
        self.kind, self.info = kind, info


class LiveAccountRefused(RuntimeError):
    """Trading code under test asked for a live account (the step's mocks: options did not say allow_live: true)."""


class NotModelled(AttributeError):
    """A method or name of the SDK that the mock does not answer: refused, so a test never passes on a call nothing
    checked."""


# ---- options and the prices file ------------------------------------------------------------------------------

def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def options_error(opts) -> str | None:
    """Why broker mock options (the step's `mocks: {alpaca: {...}}`) are wrong, or None. testbench.yaml is checked
    with it too."""
    if not isinstance(opts, dict):
        return "options are a mapping such as {prices: data/prices.csv, cash: 100000, fee_usd: 1, slippage_bps: 5}"
    extra = [k for k in opts if k not in OPTION_KEYS]
    if extra:
        close = difflib.get_close_matches(str(extra[0]), OPTION_KEYS, 1, 0.7)
        return (f"has no {extra[0]!r}" + (f" (did you mean {close[0]!r}?)" if close else "") +
                f"; the options are {', '.join(OPTION_KEYS)}")
    if "prices" in opts and not (isinstance(opts["prices"], str) and opts["prices"].strip()):
        return "prices is the path of a CSV file with the columns time,symbol,close (open, high, low, volume optional)"
    price = opts.get("price")
    if price is not None and not (isinstance(price, dict) and price and all(
            isinstance(k, str) and _finite(v) and v > 0 for k, v in price.items())):
        return "price is a mapping of symbol to a price above 0, e.g. {AAPL: 190.5, BTC/USDT: 65000}"
    if opts.get("now") is not None:
        try:
            parse_time(opts["now"])
        except ValueError as exc:
            return f"now: {exc}"
    adv = opts.get("advance_every")
    if adv is not None and not (core.is_int(adv) and adv >= 0):
        return ("advance_every is a whole number of requests, e.g. 1 (each request to the broker moves the clock one "
                "bar)")
    for key, what in (("cash", "the starting cash, e.g. 100000"), ("fee_usd", "a fixed fee per filled order, e.g. 1"),
                      ("fee_bps", "a fee in basis points of each fill's value, e.g. 10 for 0.1%"),
                      ("slippage_bps", "how far market fills move against the trader, in basis points, e.g. 5")):
        if key in opts and not (_finite(opts[key]) and 0 <= opts[key] < (1e15 if key == "cash" else 10000)):
            return f"{key} is {what}: a number from 0" + ("" if key == "cash" else " to under 10000")
    if "currency" in opts and not (isinstance(opts["currency"], str) and re.fullmatch(r"[A-Z0-9]{2,10}",
                                                                                    opts["currency"])):
        return "currency is the cash currency's code, e.g. USD or USDT"
    pos = opts.get("positions")
    if pos is not None and not (isinstance(pos, dict) and all(isinstance(k, str) and _finite(v) and v > 0
                                                              for k, v in pos.items())):
        return ("positions is a mapping of symbol (for ccxt, currency) to the quantity held at the start, e.g. "
                "{AAPL: 10}")
    if "allow_live" in opts and not isinstance(opts["allow_live"], bool):
        return "allow_live is true or false"
    return None


def setup_error(opts, base: str) -> str | None:
    """options_error, and the prices file read as the mock will read it (paths relative to base)."""
    bad = options_error(opts)
    if bad or not opts.get("prices"):
        return bad
    path = os.path.join(base, opts["prices"])
    if not os.path.isfile(path):
        return f"prices: {opts['prices']} does not exist (paths are relative to {base})"
    try:
        bars = parse_prices(path)
    except ValueError as exc:
        return f"prices ({opts['prices']}): {exc}"
    if opts.get("now") is not None:
        first = min(b[0].time for b in bars.values())
        if parse_time(opts["now"]) < first:
            return f"now ({iso(parse_time(opts['now']))}) is before the first bar of {opts['prices']} ({iso(first)})"
    return None


def parse_time(v) -> datetime:
    """A time of the prices file or of now:, as an aware UTC datetime: 2024-01-05, 2024-01-05 15:30,
    2024-01-05T15:30:00Z, with an offset, or seconds / milliseconds since 1970. A time without a zone is UTC."""
    if isinstance(v, datetime):
        d = v
    elif isinstance(v, date):
        d = datetime(v.year, v.month, v.day)
    elif _finite(v):
        d = datetime.fromtimestamp(v / 1000 if v > 1e11 else v, timezone.utc)
    elif isinstance(v, str) and re.fullmatch(r"\s*\d{9,13}(\.\d+)?\s*", v):
        return parse_time(float(v))
    elif isinstance(v, str):
        try:
            d = datetime.fromisoformat(re.sub(r"[zZ]$", "+00:00", v.strip()))
        except ValueError:
            raise ValueError(f"{v!r} is not a time; write it like 2024-01-05, 2024-01-05 15:30 or "
                             "2024-01-05T15:30:00Z") from None
    else:
        raise ValueError(f"{v!r} is not a time; write it like 2024-01-05 or 2024-01-05T15:30:00Z")
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def ms(t: datetime) -> int:
    return int(round(t.timestamp() * 1000))


class Bar:
    __slots__ = ("time", "open", "high", "low", "close", "volume")

    def __init__(self, t, o, h, low, c, v):
        self.time, self.open, self.high, self.low, self.close, self.volume = t, o, h, low, c, v


def _price(text: str, column: str, zero_ok=False) -> float:
    try:
        x = float(text)
    except ValueError:
        raise ValueError(f"{column} is {text!r}, not a number") from None
    if not math.isfinite(x) or x < 0 or (x == 0 and not zero_ok):
        raise ValueError(f"{column} is {text}; it must be a number above 0" + (" (or 0)" if zero_ok else ""))
    return x


def parse_prices(path: str) -> dict:
    """The prices file: symbol -> its bars, oldest first. Raises ValueError saying what to write instead."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = csv.reader(f)
        header = next(rows, None)
        if not header:
            raise ValueError("is empty; its first line names the columns: time,symbol,close")
        cols = [h.strip().lower() for h in header]
        missing = [c for c in ("time", "symbol", "close") if c not in cols]
        if missing:
            raise ValueError(f"has no {', '.join(missing)} column; the columns are time,symbol,close (open, high, "
                             f"low, volume optional), it has {', '.join(cols)}")
        at = {c: cols.index(c) for c in ("time", "symbol", "open", "high", "low", "close", "volume") if c in cols}
        found: dict = {}
        for n, row in enumerate(rows, 2):
            if not any(x.strip() for x in row):
                continue
            try:
                cell = {c: row[i].strip() if i < len(row) else "" for c, i in at.items()}
                t = parse_time(cell["time"])
                symbol = cell["symbol"]
                if not symbol:
                    raise ValueError("the symbol is empty")
                c = _price(cell["close"], "close")
                o, h, low = (_price(cell[k], k) if cell.get(k) else c for k in ("open", "high", "low"))
                v = _price(cell["volume"], "volume", zero_ok=True) if cell.get("volume") else 0.0
            except ValueError as exc:
                raise ValueError(f"line {n}: {exc}") from None
            bars = found.setdefault(symbol, {})
            if t in bars:
                raise ValueError(f"line {n}: {symbol} has a second bar at {iso(t)}")
            bars[t] = Bar(t, o, max(h, o, c), min(low, o, c), c, v)
    if not found:
        raise ValueError("has no rows after its header line")
    return {s: [b[t] for t in sorted(b)] for s, b in found.items()}


class Book:
    """The prices, and the mock's clock: one tick per bar time in the file. now: picks the bar at or before it (by
    default the last); with advance_every, the clock moves one tick every that many requests."""

    def __init__(self, opts: dict):
        try:
            self.bars = parse_prices(opts["prices"]) if opts.get("prices") else {}
        except OSError as exc:
            raise SetupError(f"prices: cannot read {opts['prices']} ({exc.strerror})") from None
        except ValueError as exc:
            raise SetupError(f"prices ({opts['prices']}): {exc}") from None
        self.fixed = {k: float(v) for k, v in (opts.get("price") or {}).items()}
        self.times = {s: [b.time for b in bars] for s, bars in self.bars.items()}
        self.ticks = sorted({b.time for bars in self.bars.values() for b in bars})
        start = parse_time(opts["now"]) if opts.get("now") is not None else None
        if self.ticks:
            if start is not None and start < self.ticks[0]:
                raise SetupError(f"now ({iso(start)}) is before the first bar of the prices file "
                                 f"({iso(self.ticks[0])})")
            self.i = len(self.ticks) - 1 if start is None else bisect.bisect_right(self.ticks, start) - 1
            self.still = None
        else:
            self.i = 0
            self.still = start or datetime.now(timezone.utc).replace(microsecond=0)

    def now(self) -> datetime:
        return self.still if self.still is not None else self.ticks[self.i]

    def advance(self) -> bool:
        if self.still is None and self.i < len(self.ticks) - 1:
            self.i += 1
            return True
        return False

    def symbols(self) -> list:
        return sorted(set(self.bars) | set(self.fixed))

    def has(self, symbol) -> bool:
        return symbol in self.fixed or symbol in self.bars

    def _at(self, symbol):
        return bisect.bisect_right(self.times[symbol], self.now()) - 1

    def bar(self, symbol) -> Bar | None:
        """The current bar of a symbol: its last bar at or before now (a price: symbol has one at every time)."""
        if symbol in self.fixed:
            p = self.fixed[symbol]
            return Bar(self.now(), p, p, p, p, 0.0)
        if symbol not in self.bars:
            return None
        j = self._at(symbol)
        return self.bars[symbol][j] if j >= 0 else None

    def price(self, symbol) -> float | None:
        b = self.bar(symbol)
        return b.close if b else None

    def previous(self, symbol) -> float | None:
        if symbol in self.fixed:
            return self.fixed[symbol]
        j = self._at(symbol) if symbol in self.bars else -1
        return self.bars[symbol][j - 1].close if j >= 1 else None

    def history(self, symbol, start=None, end=None) -> list:
        """The bars of a symbol up to now (never after: no look-ahead), between start and end when given."""
        if symbol in self.fixed:
            bars = [self.bar(symbol)]
        else:
            bars = self.bars.get(symbol, [])[:self._at(symbol) + 1] if symbol in self.bars else []
        return [b for b in bars if (start is None or b.time >= start) and (end is None or b.time <= end)]

    def spacing(self, symbol) -> float | None:
        """The usual seconds between a symbol's bars (None with fewer than two)."""
        t = self.times.get(symbol) or []
        gaps = [(b - a).total_seconds() for a, b in zip(t, t[1:])]
        return statistics.median(gaps) if gaps else None

    def missing(self, symbol) -> str:
        """Why there is no price for a symbol, in words."""
        if symbol in self.bars:
            return (f"no price for {symbol} at {iso(self.now())}: its first bar in the prices file is "
                    f"{iso(self.bars[symbol][0].time)}")
        have = self.symbols()
        close = difflib.get_close_matches(symbol, have, 1, 0.6)
        return (f"no price for {symbol}: the mock's prices (prices: and price: in the step's mocks: options) have " +
                (", ".join(have[:20]) + (" ..." if len(have) > 20 else "") if have else "no symbols at all") +
                (f" (did you mean {close[0]}?)" if close else ""))


def bar_seconds(amount: int, unit: str) -> float:
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}[unit]


def spacing_note(book: Book, symbol: str, asked: float, asked_text: str) -> str | None:
    have = book.spacing(symbol)
    if have and asked and abs(have - asked) > 0.01 * asked:
        return (f"{asked_text} bars were asked for {symbol}; the prices file's bars are {_span(have)} apart and are "
                "answered as they are (the mock does not resample)")
    return None


def _span(seconds: float) -> str:
    for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            n = int(seconds // size)
            return f"{n} {unit}{'s' * (n != 1)}"
    return f"{seconds:g} seconds"


# ---- one account ----------------------------------------------------------------------------------------------

def num(x, digits=8) -> float:
    return float(round(x, digits))


class Account:
    """Cash and holdings of one account (balance: currency or asset -> amount), the cost of each holding, and every
    order, oldest first. asset_of(symbol) -> (asset, quote currency); symbol_of(asset) -> the symbol it is priced by."""

    def __init__(self, broker, name, currency, asset_of, symbol_of):
        self.broker, self.name, self.currency = broker, name, currency
        self.asset_of, self.symbol_of = asset_of, symbol_of
        opts, book = broker.opts, broker.book
        self.balance = {currency: float(opts.get("cash", 100000))}
        self.cost: dict = {}
        self.realized = 0.0
        self.start_equity = None
        for name, qty in (opts.get("positions") or {}).items():
            asset = self.asset_of(name)[0] if "/" in name else name          # BTC/USD is held as alpaca's BTCUSD
            self.balance[asset] = self.balance.get(asset, 0.0) + float(qty)
            p = book.price(self.symbol_of(asset))
            self.cost[asset] = float(qty) * (p or 0.0)
        self.orders: list = []
        self.fills: list = []
        self.start_equity = self.equity()

    # what is held and what is free
    def held(self, asset) -> float:
        return self.balance.get(asset, 0.0)

    def open_orders(self) -> list:
        return [o for o in self.orders if o["status"] == "open"]

    def reserved(self, asset) -> float:
        out = 0.0
        for o in self.open_orders():
            if o["side"] == "buy" and o["quote"] == asset:
                out += o["reserve"]
            elif o["side"] == "sell" and o["asset"] == asset:
                out += o["qty"]
        return out

    def free(self, asset) -> float:
        return self.held(asset) - self.reserved(asset)

    def value(self, asset) -> float | None:
        """What a holding is worth now in the cash currency (None when there is no price for it)."""
        if asset == self.currency:
            return self.held(asset)
        p = self.broker.book.price(self.symbol_of(asset))
        return None if p is None else self.held(asset) * p

    def equity(self) -> float:
        return sum(v for v in (self.value(a) for a in self.balance) if v is not None)

    def holdings(self) -> list:
        return [a for a, q in self.balance.items() if a != self.currency and abs(q) > EPS]

    # orders
    def place(self, *, symbol, side, qty=None, notional=None, type="market", limit=None, stop=None, tif="day",
              client_id=None, info=None, make_id=str) -> dict:
        book, opts = self.broker.book, self.broker.opts
        price = book.price(symbol)
        if price is None:
            raise SetupError(f"mock {self.broker.sdk}: {book.missing(symbol)}. Add its bars to the prices file, or "
                             "the symbol to price:.")
        asset, quote = self.asset_of(symbol)
        slip = float(opts.get("slippage_bps", 0)) / 10000
        if notional is not None:                         # a cash amount: what it buys (sells) at the fill price
            qty = num(notional / (price * (1 + slip if side == "buy" else 1 - slip)), 9)
        if not (_finite(qty) and qty > 0):
            raise Refused("invalid", f"the quantity must be a number above 0, got {qty!r}")
        for name, v in (("limit price", limit), ("stop price", stop)):
            if v is not None and not (_finite(v) and v > 0):
                raise Refused("invalid", f"the {name} must be a number above 0, got {v!r}")
        ref = limit if limit is not None else max(stop, price) if (stop is not None and side == "buy") else \
            stop if stop is not None else price
        reserve = 0.0
        if side == "buy":
            gross = qty * ref * (1 + slip if limit is None else 1)
            reserve = gross + self.fee(gross)
            free = self.free(quote)
            if reserve > free + EPS:
                raise Refused("funds", f"this order needs {reserve:,.2f} {quote} and {free:,.2f} {quote} is free "
                                       f"(cash {self.held(quote):,.2f}, {self.reserved(quote):,.2f} held for open "
                                       "orders)",
                              need=reserve, free=free, currency=quote)
        else:
            free = self.free(asset)
            if qty > free + EPS:
                raise Refused("qty", f"this order sells {qty:g} {asset} and {max(free, 0):g} is free to sell "
                                     f"(held {self.held(asset):g}, {self.reserved(asset):g} in open orders); the mock "
                                     "does not sell short", requested=qty, available=max(free, 0.0), asset=asset)
        self.broker.seq += 1
        order = {"n": self.broker.seq, "id": make_id(self.broker.seq), "symbol": symbol, "asset": asset,
                 "quote": quote, "side": side, "type": type, "qty": qty, "notional": notional, "limit": limit,
                 "stop": stop, "tif": tif, "client_id": client_id, "status": "open", "filled": 0.0, "price": None,
                 "fee": 0.0, "created": book.now(), "filled_at": None, "canceled_at": None, "triggered": False,
                 "reserve": reserve, "value": num(qty * ref, 6), "info": info or {}, "why": None}
        self.orders.append(order)
        self.broker.event("orders", {"id": order["id"], "symbol": symbol, "side": side, "type": type,
                                     "qty": num(qty), "limit": limit, "stop": stop, "value": order["value"]})
        self.try_fill(order)
        if order["status"] == "open" and str(tif).lower() in ("ioc", "fok"):
            self.cancel(order, why="expired: not fillable at once (time in force " + str(tif).upper() + ")")
        return order

    def fee(self, value: float) -> float:
        opts = self.broker.opts
        return float(opts.get("fee_usd", 0)) + value * float(opts.get("fee_bps", 0)) / 10000

    def try_fill(self, order) -> None:
        price = self.broker.book.price(order["symbol"])
        if price is None or order["status"] != "open":
            return
        buy = order["side"] == "buy"
        if order["stop"] is not None and not order["triggered"]:
            if not (price >= order["stop"] if buy else price <= order["stop"]):
                return
            order["triggered"] = True
        slip = float(self.broker.opts.get("slippage_bps", 0)) / 10000
        moved = price * (1 + slip) if buy else price * (1 - slip)
        if order["limit"] is not None:
            if (buy and price > order["limit"] + EPS) or (not buy and price < order["limit"] - EPS):
                return
            moved = min(order["limit"], moved) if buy else max(order["limit"], moved)
        self._fill(order, num(moved))

    def _fill(self, order, price) -> None:
        qty, asset, quote = order["qty"], order["asset"], order["quote"]
        value = qty * price
        fee = num(self.fee(value), 8)
        realized = 0.0
        if order["side"] == "buy":
            free = self.held(quote) - (self.reserved(quote) - order["reserve"])
            if value + fee > free + EPS:
                self.cancel(order, why=f"rejected when it filled: {value + fee:,.2f} {quote} needed, {free:,.2f} free")
                return
            self.balance[quote] = self.held(quote) - value - fee
            self.balance[asset] = self.held(asset) + qty
            self.cost[asset] = self.cost.get(asset, 0.0) + value
        else:
            held = self.held(asset)
            avg = self.cost.get(asset, 0.0) / held if held > EPS else 0.0
            realized = (price - avg) * qty - fee
            self.realized += realized
            self.cost[asset] = self.cost.get(asset, 0.0) - avg * qty
            self.balance[asset] = held - qty
            self.balance[quote] = self.held(quote) + value - fee
            if abs(self.balance[asset]) <= EPS * max(1.0, qty):
                del self.balance[asset]
                self.cost.pop(asset, None)
        order.update(status="filled", filled=qty, price=price, fee=fee, filled_at=self.broker.book.now())
        self.broker.seq += 1
        fill = {"id": str(self.broker.seq), "order": order["id"], "symbol": order["symbol"], "side": order["side"],
                "qty": num(qty), "price": price, "fee": fee, "value": num(value, 6), "time": order["filled_at"],
                "currency": quote, "realized": num(realized, 6), "order_obj": order}
        self.fills.append(fill)
        self.broker.event("fills", {k: v for k, v in fill.items() if k not in ("time", "order_obj")})
        self.broker.filled(self, order, fill)

    def cancel(self, order, why=None) -> None:
        if order["status"] != "open":
            raise Refused("state", f"order {order['id']} is {order['status']}, so it cannot be cancelled",
                          status=order["status"])
        order.update(status="canceled", canceled_at=self.broker.book.now(), why=why)
        self.broker.event("cancels", {"id": order["id"], "symbol": order["symbol"], **({"why": why} if why else {})})
        self.broker.cancelled(self, order)

    def find(self, order_id) -> dict:
        for o in self.orders:
            if o["id"] == str(order_id).lower() or (o["client_id"] is not None and o["client_id"] == order_id):
                return o
        raise Refused("not_found", f"no order {order_id} in this account")

    def check_resting(self) -> None:
        for o in self.open_orders():
            self.try_fill(o)


# ---- one SDK in this process ----------------------------------------------------------------------------------

class Broker:
    """One broker SDK's mock in this process: its options, prices, accounts and call counter. A call is logged as
    one line of mock_calls.jsonl, answered or refused; a request (a call that would reach the broker) may move the
    clock, which fills resting orders the new prices cross."""

    def __init__(self, sdk: str):
        self.sdk, self.count, self.requests, self.seq = sdk, 0, 0, 0
        self._opts = self._book = None
        self.accounts: dict = {}
        self._ev = None
        self.on_fill = self.on_cancel = None             # the SDK's hooks: on_fill(account, order, fill)

    @property
    def opts(self) -> dict:
        if self._opts is None:
            name = f"TESTBENCH_MOCK_{self.sdk.upper()}"
            raw = os.environ.get(name, "").strip() or "{}"
            try:
                opts = json.loads(raw)
            except ValueError:
                raise SetupError(f"mock {self.sdk}: {name} is not JSON ({raw[:80]!r}); it holds options such as "
                                 '{"prices": "data/prices.csv", "cash": 100000}') from None
            bad = options_error(opts)
            if bad:
                raise SetupError(f"mock {self.sdk}: {name} {bad}")
            self._opts = opts
        return self._opts

    @property
    def book(self) -> Book:
        if self._book is None:
            try:
                self._book = Book(self.opts)
            except SetupError as exc:
                raise SetupError(f"mock {self.sdk}: {exc}") from None
        return self._book

    def account(self, name, currency, asset_of, symbol_of) -> Account:
        if name not in self.accounts:
            self.accounts[name] = Account(self, name, currency, asset_of, symbol_of)
        return self.accounts[name]

    # what happened during the call being answered
    def event(self, kind, item) -> None:
        if self._ev is not None:
            self._ev.setdefault(kind, []).append(item)

    def note(self, text) -> None:
        self.event("notes", text)

    def use(self, account) -> None:
        if self._ev is not None:
            self._ev["account"] = account

    def refused_quietly(self, type_name, message) -> None:
        """The broker refused, but the SDK reports it without raising (ib_async's order rejections)."""
        if self._ev is not None:
            self._ev["error"] = {"kind": "refused", "type": type_name, "message": message}

    def filled(self, account, order, fill) -> None:
        if self.on_fill:
            self.on_fill(account, order, fill)

    def cancelled(self, account, order) -> None:
        if self.on_cancel:
            self.on_cancel(account, order)

    def guard_live(self, live: bool, what: str) -> None:
        """A live account is refused unless the step's mocks: options say allow_live: true."""
        if live and not self.opts.get("allow_live"):
            raise Refused("live", f"mock {self.sdk}: this test asked for a live account ({what}); {LIVE_RULE}. Code "
                                  "that is only exercised offline can be let through with allow_live: true in the "
                                  "step's mocks: options (nothing reaches a broker either way).")

    def _tick(self) -> None:
        adv = self.opts.get("advance_every") or 0
        if adv and self.requests > 1 and (self.requests - 1) % adv == 0 and self.book.advance():
            for account in self.accounts.values():
                account.check_resting()

    def call(self, endpoint, arguments, answer, *, error, live=False, request=True):
        """One call of the SDK: answer() -> result, or a Refused that error(refused) turns into the SDK's exception.
        request=False for what never reaches the broker (making a client, switching on a sandbox)."""
        self.count += 1
        rec = {"sdk": self.sdk, "endpoint": endpoint, "call": self.count, "t": round(time.time(), 3),
               "live": bool(live), "arguments": core.shrink(arguments), "error": None}
        outer, self._ev = self._ev, {}                   # a call made from an event handler inside another call
        ev = self._ev
        try:
            if request:
                self.requests += 1
                self._tick()
            result = answer()
        except Refused as exc:
            err = error(exc)
            rec["error"] = {"kind": "refused", "type": type(err).__name__, "message": str(exc)}
            self._write(rec, ev)
            raise err from None
        except core.SetupError as exc:
            rec["error"] = {"kind": "refused", "type": "SetupError", "message": str(exc)}
            self._write(rec, ev)
            raise
        except Exception as exc:                         # the code's own (an ib_async event handler), or the mock's
            rec["error"] = {"kind": "error", "type": type(exc).__name__, "message": str(exc)[:2000]}
            self._write(rec, ev)
            raise
        finally:
            self._ev = outer
        if ev.get("error"):
            rec["error"] = ev["error"]
        self._write(rec, ev)
        return result

    def log_refusal(self, endpoint, arguments, exc: BaseException, live=False) -> BaseException:
        """A refusal outside call(): the SDK's own checks (a request model, a client's arguments, a name the mock
        does not model). Logged like a call, without moving the clock; returns exc to raise."""
        self.count += 1
        self._write({"sdk": self.sdk, "endpoint": endpoint, "call": self.count, "t": round(time.time(), 3),
                     "live": bool(live), "arguments": core.shrink(arguments),
                     "error": {"kind": "refused", "type": type(exc).__name__, "message": str(exc)}}, {})
        return exc

    def not_modelled(self, where: str, name: str, has: str) -> NotModelled:
        msg = (f"mock {self.sdk}: {where}{name} is not modelled; the mock answers {has} ({DOCS}). Anything else is "
               "refused, so that no test passes on a call nothing checked.")
        return self.log_refusal(f"{where}{name}", {}, NotModelled(msg, name=name))

    def _write(self, rec, ev) -> None:
        for key in ("orders", "fills", "cancels", "notes"):
            if ev.get(key):
                rec[key] = ev[key]
        try:
            rec["time"] = iso(self.book.now())
        except core.SetupError:
            pass
        account = ev.get("account")
        if account is not None:
            rec.update(account=account.name, cash=num(account.held(account.currency), 6),
                       equity=num(account.equity(), 6))
        core.write(rec)


def invalid(title: str, message: str, data=None) -> Exception:
    """pydantic's ValidationError for a request model, where pydantic is installed (code that catches it works as
    with the real SDK), else a ValueError that reads the same."""
    try:
        from pydantic_core import ValidationError
        return ValidationError.from_exception_data(title, [{"type": "value_error", "loc": (), "input": data or {},
                                                            "ctx": {"error": ValueError(message)}}])
    except Exception:
        return core.MockValidationError(f"1 validation error for {title}\n  Value error, {message} [type=value_error]")


def did_you_mean(name, options) -> str:
    close = difflib.get_close_matches(str(name), list(options), 1, 0.75)
    return f" (did you mean {close[0]!r}?)" if close else ""


def number(v, at: str) -> float:
    """A quantity or price as the SDKs take it: an int, a float, a Decimal or a numeric string."""
    if isinstance(v, bool) or v is None:
        raise Refused("argument", f"{at} must be a number, got {core.kind(v)} {v!r}")
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise Refused("argument", f"{at} must be a number, got {core.kind(v)} {v!r}") from None
    if not math.isfinite(x):
        raise Refused("argument", f"{at} must be a finite number, got {v!r}")
    return x


def fmt(x) -> str | None:
    """A number as the brokers' JSON writes it: no float noise, no trailing zeros."""
    if x is None:
        return None
    s = f"{float(x):.9f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def market_hours(now: datetime):
    """The next regular-session open and close (09:30 and 16:00 New York time) after now, for get_clock."""
    try:
        from zoneinfo import ZoneInfo
        ny = ZoneInfo("America/New_York")
    except Exception:
        ny = timezone(timedelta(hours=-5))
    local = now.astimezone(ny)
    day = local.date()

    def at(d, h, m):
        return datetime(d.year, d.month, d.day, h, m, tzinfo=ny).astimezone(timezone.utc)

    def next_weekday(d):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        return d
    close = at(day, 16, 0) if day.weekday() < 5 and local < at(day, 16, 0) else at(next_weekday(day), 16, 0)
    opening = at(day, 9, 30) if day.weekday() < 5 and local < at(day, 9, 30) else at(next_weekday(day), 9, 30)
    return opening, close
