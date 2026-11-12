"""ccxt.base.exchange of the mock ccxt package: the Exchange every mock exchange is, answering the unified API of
ccxt 4.5.92 (its method names in snake_case and camelCase, its dict structures) from the step's prices.

An account call (balance, orders, trades) on a client without set_sandbox_mode(True) is a live account: refused unless
the step's mocks: options say allow_live: true. Market data (markets, tickers, candles) touches no account and is
answered either way."""
from __future__ import annotations

import math
import re

import _testbench_broker as B

from . import errors as E

BROKER = B.Broker("ccxt")
PUBLIC = ("load_markets, fetch_markets, fetch_ticker, fetch_tickers, fetch_ohlcv, fetch_time, market, "
          "amount_to_precision, price_to_precision, milliseconds, seconds, iso8601, parse8601")
PRIVATE = ("fetch_balance, create_order (market and limit; create_market_order, create_limit_order and their _buy_ and "
           "_sell_ forms), cancel_order, cancel_all_orders, fetch_order, fetch_orders, fetch_open_orders, "
           "fetch_closed_orders, fetch_my_trades")
_CAPS = ("loadMarkets", "fetchMarkets", "fetchTicker", "fetchTickers", "fetchOHLCV", "fetchTime", "fetchBalance",
         "createOrder", "createMarketOrder", "createLimitOrder", "createMarketBuyOrder", "createMarketSellOrder",
         "createLimitBuyOrder", "createLimitSellOrder", "cancelOrder", "cancelAllOrders", "fetchOrder", "fetchOrders",
         "fetchOpenOrders", "fetchClosedOrders", "fetchMyTrades")
_PARAMS = ("clientOrderId", "timeInForce", "postOnly")
_CONFIG = ("apiKey", "secret", "password", "uid", "enableRateLimit", "rateLimit", "timeout", "verbose", "options",
           "headers", "proxies", "httpProxy", "httpsProxy", "socksProxy", "userAgent", "newUpdates", "walletAddress",
           "privateKey", "token", "login", "twofa", "accountId", "hostname", "precisionMode", "defaultType")


class _Has(dict):
    """exchange.has: True for what the mock answers, False for the rest (the mock does not have it)."""

    def __missing__(self, key):
        return False


class Exchange:
    """A mock exchange: the unified ccxt API on an account kept here, one per exchange id."""
    id = None
    name = None
    sandbox_url = True                    # does the real exchange have a sandbox (set_sandbox_mode(True))?
    currency = "USDT"                     # the cash currency of the mock account unless the options say otherwise
    credentials = ("apiKey", "secret")
    symbol_needed = ()                    # methods the real exchange refuses without a symbol
    timeframes = {}

    def __init__(self, config=None):
        config = {} if config is None else config
        if not isinstance(config, dict):
            raise TypeError(f"ccxt.{self.id}() takes a config dict such as {{'apiKey': ..., 'secret': ...}}, got "
                            f"{type(config).__name__}")
        self.apiKey = self.secret = self.password = self.uid = ""
        self.enableRateLimit, self.rateLimit, self.timeout, self.verbose = True, 50, 10000, False
        self.options = {}
        for key, value in config.items():
            if key == "options" and isinstance(value, dict):
                self.options.update(value)
            else:
                setattr(self, key, value)
        self.markets = self.markets_by_id = self.symbols = None
        self.isSandboxModeEnabled = False
        self.has = _Has({cap: True for cap in _CAPS}, sandbox=self.sandbox_url, spot=True)

    def __getattr__(self, name):
        if name.startswith("_") or name in _CONFIG:
            raise AttributeError(name)
        raise BROKER.not_modelled(f"{self.id}.", name, f"{PUBLIC}; and on an account {PRIVATE}")

    def __repr__(self):
        return f"ccxt.{self.id}()"

    # ---- plumbing ----------------------------------------------------------------------------------------------

    def _error(self, exc: B.Refused) -> Exception:
        if exc.kind == "live":
            return B.LiveAccountRefused(str(exc))
        cls = {"argument": exc.info.get("cls", E.BadRequest), "invalid": E.InvalidOrder, "funds": E.InsufficientFunds,
               "qty": E.InsufficientFunds, "not_found": E.OrderNotFound, "symbol": E.BadSymbol,
               "state": E.OrderNotFound, "auth": E.AuthenticationError, "unsupported": E.NotSupported,
               "immediate": E.OrderImmediatelyFillable}.get(exc.kind, E.ExchangeError)
        return cls(f"{self.id} {exc}")

    def _account(self) -> B.Account:
        cur = BROKER.opts.get("currency", self.currency)
        a = BROKER.account(self.id, cur, _split, lambda asset: f"{asset}/{cur}")
        BROKER.use(a)
        return a

    def _public(self, endpoint, args, answer):
        return BROKER.call(endpoint, args, answer, error=self._error)

    def _private(self, endpoint, args, answer):
        live = not self.isSandboxModeEnabled

        def guarded():
            what = f"ccxt.{self.id}() without set_sandbox_mode(True)"
            if not self.sandbox_url:
                what += f"; {self.id} has no sandbox in ccxt, so every account call of this code reaches a real account"
            BROKER.guard_live(live, what)
            for key in self.credentials:
                if not getattr(self, key, None):
                    raise B.Refused("auth", f'requires "{key}" credential')
            return answer()
        return BROKER.call(endpoint, args, guarded, error=self._error, live=live)

    def _need_symbol(self, method, symbol):
        if symbol is None and method in self.symbol_needed:
            raise B.Refused("argument", f"{method}() requires a symbol argument", cls=E.ArgumentsRequired)

    def _params(self, params, allowed=()):
        if not isinstance(params, dict):
            raise B.Refused("argument", f"params must be a dict, got {type(params).__name__}")
        extra = [k for k in params if k not in allowed]
        if extra:
            hint = " (stop and trigger orders are not modelled)" if any("rice" in k or "stop" in k.lower()
                                                                        for k in extra) else ""
            raise B.Refused("unsupported", f"the mock does not model params {', '.join(map(repr, extra))}{hint}; "
                                           f"it takes {', '.join(allowed) or 'none here'}")

    def _symbol(self, symbol) -> str:
        if not isinstance(symbol, str) or "/" not in symbol:
            raise B.Refused("symbol", f"does not have market symbol {symbol!r}: ccxt's symbols are unified, "
                                      "BASE/QUOTE such as BTC/USDT")
        if not BROKER.book.has(symbol):
            raise B.SetupError(f"mock ccxt: {BROKER.book.missing(symbol)}. Add its bars to the prices file, or the "
                               "symbol to price:.")
        if self.markets is None:
            self._load()
        return symbol

    def _market(self, symbol) -> dict:
        base, quote = symbol.split("/", 1)
        fee = float(BROKER.opts.get("fee_bps", 0)) / 10000
        return {"id": f"{base}{quote}", "symbol": symbol, "base": base, "quote": quote, "settle": None,
                "baseId": base, "quoteId": quote, "settleId": None, "type": "spot", "spot": True, "margin": False,
                "swap": False, "future": False, "option": False, "index": None, "active": True, "contract": False,
                "linear": None, "inverse": None, "subType": None, "taker": fee, "maker": fee, "contractSize": None,
                "expiry": None, "expiryDatetime": None, "strike": None, "optionType": None,
                "precision": {"amount": 1e-08, "price": 1e-08}, "limits": {
                    "leverage": {"min": None, "max": None}, "amount": {"min": 1e-08, "max": None},
                    "price": {"min": 1e-08, "max": None}, "cost": {"min": None, "max": None}},
                "marginModes": {"cross": None, "isolated": None}, "created": None, "lowercaseId": None, "info": {}}

    def _load(self) -> dict:
        self.markets = {s: self._market(s) for s in BROKER.book.symbols() if "/" in s}
        self.markets_by_id = {m["id"]: [m] for m in self.markets.values()}
        self.symbols = sorted(self.markets)
        return self.markets

    def _ticker(self, symbol) -> dict:
        book = BROKER.book
        bar, prev = book.bar(symbol), book.previous(symbol)
        if bar is None:
            raise B.Refused("symbol", book.missing(symbol))
        t = B.ms(bar.time)
        change = None if prev is None else bar.close - prev
        return {"symbol": symbol, "timestamp": t, "datetime": B.iso(bar.time).replace("Z", ".000Z"),
                "high": bar.high, "low": bar.low, "bid": bar.close, "bidVolume": None, "ask": bar.close,
                "askVolume": None, "vwap": None, "open": bar.open, "close": bar.close, "last": bar.close,
                "previousClose": prev, "change": change, "percentage": None if not prev else change / prev * 100,
                "average": None if prev is None else (bar.close + prev) / 2, "baseVolume": bar.volume,
                "quoteVolume": bar.volume * bar.close, "indexPrice": None, "markPrice": None, "info": {}}

    def _order(self, o: dict) -> dict:
        def stamp(t):
            return None if t is None else B.ms(t)
        filled = o["filled"]
        fee = {"cost": o["fee"], "currency": o["quote"]} if o["status"] == "filled" else None
        return {"id": o["id"], "clientOrderId": o["client_id"], "timestamp": stamp(o["created"]),
                "datetime": self.iso8601(stamp(o["created"])), "lastTradeTimestamp": stamp(o["filled_at"]),
                "lastUpdateTimestamp": stamp(o["filled_at"] or o["canceled_at"]),
                "status": {"open": "open", "filled": "closed", "canceled": "canceled"}[o["status"]],
                "symbol": o["symbol"], "type": o["type"], "timeInForce": str(o["tif"]).upper(),
                "postOnly": bool(o["info"].get("postOnly")), "reduceOnly": None, "side": o["side"],
                "price": o["limit"] if o["limit"] is not None else o["price"], "triggerPrice": None,
                "stopPrice": None, "takeProfitPrice": None, "stopLossPrice": None, "average": o["price"],
                "amount": o["qty"], "filled": filled, "remaining": B.num(o["qty"] - filled),
                "cost": B.num(filled * (o["price"] or 0)), "trades": [], "fee": fee, "fees": [fee] if fee else [],
                "info": {"mock": "testbench", **({"why": o["why"]} if o["why"] else {})}}

    def _trade(self, f: dict) -> dict:
        o = f["order_obj"]
        fee = {"cost": f["fee"], "currency": f["currency"]}
        return {"info": {}, "id": f["id"], "timestamp": B.ms(f["time"]), "datetime": self.iso8601(B.ms(f["time"])),
                "symbol": f["symbol"], "order": f["order"], "type": o["type"], "side": f["side"],
                "takerOrMaker": "taker" if o["limit"] is None else "maker", "price": f["price"], "amount": f["qty"],
                "cost": f["value"], "fee": fee, "fees": [fee]}

    @staticmethod
    def _since_limit(items, since, limit, stamp):
        if since is not None:
            items = [x for x in items if stamp(x) >= since]
            return items[:limit] if limit else items
        return items[-limit:] if limit else items

    # ---- time and precision ----------------------------------------------------------------------------------

    @staticmethod
    def milliseconds() -> int:
        """The mock's clock (its current bar), so that since= worked out from it lines up with the prices file."""
        return B.ms(BROKER.book.now())

    @staticmethod
    def seconds() -> int:
        return Exchange.milliseconds() // 1000

    @staticmethod
    def iso8601(timestamp=None):
        if timestamp is None or isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return None
        t = B.parse_time(float(timestamp) / 1000)
        return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(timestamp) % 1000:03d}Z"

    @staticmethod
    def parse8601(timestamp=None):
        try:
            return B.ms(B.parse_time(timestamp))
        except (ValueError, TypeError):
            return None

    def _precise(self, symbol, value, what):
        self._symbol(symbol)
        x = B.number(value, what)
        s = f"{math.floor(x * 1e8 + 1e-6) / 1e8:.8f}".rstrip("0").rstrip(".")
        return s or "0"

    def amount_to_precision(self, symbol, amount):
        return self._call_quietly(lambda: self._precise(symbol, amount, "amount"))

    def price_to_precision(self, symbol, price):
        return self._call_quietly(lambda: self._precise(symbol, price, "price"))

    def _call_quietly(self, fn):
        try:
            return fn()
        except B.Refused as exc:
            raise self._error(exc) from None

    def sleep(self, milliseconds):
        """Does not wait: the mock's clock moves with requests (advance_every), not with time."""
        return None

    def close(self, clean_instance_data=False):
        return None

    def set_sandbox_mode(self, enabled):
        def answer():
            if enabled and not self.sandbox_url:
                raise B.Refused("unsupported", "does not have a sandbox URL")
            self.isSandboxModeEnabled = bool(enabled)
        return BROKER.call("set_sandbox_mode", {"enabled": enabled, "exchange": self.id}, answer, error=self._error,
                           request=False)

    # ---- market data ---------------------------------------------------------------------------------------

    def load_markets(self, reload=False, params={}):
        if self.markets is not None and not reload:
            return self.markets
        return self._public("load_markets", {"exchange": self.id}, self._load)

    def fetch_markets(self, params={}):
        return self._public("fetch_markets", {"exchange": self.id}, lambda: list(self._load().values()))

    def market(self, symbol):
        if self.markets is None:
            raise E.ExchangeError(f"{self.id} markets not loaded")
        if symbol not in self.markets:
            raise E.BadSymbol(f"{self.id} does not have market symbol {symbol}")
        return self.markets[symbol]

    def fetch_time(self, params={}):
        return self._public("fetch_time", {"exchange": self.id}, lambda: B.ms(BROKER.book.now()))

    def fetch_ticker(self, symbol, params={}):
        def answer():
            self._params(params)
            return self._ticker(self._symbol(symbol))
        return self._public("fetch_ticker", {"exchange": self.id, "symbol": symbol}, answer)

    def fetch_tickers(self, symbols=None, params={}):
        def answer():
            self._params(params)
            names = symbols if symbols is not None else [s for s in BROKER.book.symbols() if "/" in s]
            return {s: self._ticker(self._symbol(s)) for s in names}
        return self._public("fetch_tickers", {"exchange": self.id, "symbols": symbols}, answer)

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None, params={}):
        def answer():
            self._params(params)
            self._symbol(symbol)
            if timeframe not in self.timeframes:
                raise B.Refused("argument", f"does not have a {timeframe!r} timeframe; it has "
                                            f"{', '.join(self.timeframes)}")
            for name, v in (("since", since), ("limit", limit)):
                if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and v >= 0):
                    raise B.Refused("argument", f"fetchOHLCV() {name} must be a whole number" +
                                    (" of milliseconds since 1970" if name == "since" else "") + f", got {v!r}")
            m = re.fullmatch(r"(\d+)([smhdwMy])", timeframe)
            note = B.spacing_note(BROKER.book, symbol, B.bar_seconds(int(m.group(1)), m.group(2)), timeframe)
            if note:
                BROKER.note(note)
            rows = [[B.ms(b.time), b.open, b.high, b.low, b.close, b.volume] for b in BROKER.book.history(symbol)]
            return self._since_limit(rows, since, limit if limit is not None else 500, lambda r: r[0])
        return self._public("fetch_ohlcv", {"exchange": self.id, "symbol": symbol, "timeframe": timeframe,
                                            "since": since, "limit": limit}, answer)

    # ---- the account ---------------------------------------------------------------------------------------

    def fetch_balance(self, params={}):
        def answer():
            self._params(params)
            a = self._account()
            out = {"info": {"mock": "testbench"}, "free": {}, "used": {}, "total": {}}
            for cur in sorted(c for c, q in a.balance.items() if abs(q) > B.EPS or c == a.currency):
                total, used = B.num(a.held(cur)), B.num(a.reserved(cur))
                out[cur] = {"free": B.num(total - used), "used": used, "total": total}
                for k in ("free", "used", "total"):
                    out[k][cur] = out[cur][k]
            now = B.ms(BROKER.book.now())
            out.update(timestamp=now, datetime=self.iso8601(now))
            return out
        return self._private("fetch_balance", {"exchange": self.id}, answer)

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        def answer():
            self._params(params, _PARAMS)
            self._symbol(symbol)
            if type not in ("market", "limit"):
                raise B.Refused("unsupported", f"createOrder() type {type!r} is not modelled: the mock takes 'market' "
                                               "and 'limit'")
            if side not in ("buy", "sell"):
                raise B.Refused("invalid", f"createOrder() side must be 'buy' or 'sell', got {side!r}")
            qty = B.number(amount, "createOrder() amount")
            limit = None
            if type == "limit":
                if price is None:
                    raise B.Refused("argument", "createOrder() requires a price argument for a limit order",
                                    cls=E.ArgumentsRequired)
                limit = B.number(price, "createOrder() price")
            elif price is not None:
                BROKER.note(f"a price ({price}) given with a market order is not used: market orders fill at the "
                            "current price")
            tif = str(params.get("timeInForce", "GTC")).upper()
            if tif not in ("GTC", "IOC", "FOK", "PO"):
                raise B.Refused("invalid", f"timeInForce {tif!r} is not one of GTC, IOC, FOK, PO")
            post = bool(params.get("postOnly")) or tif == "PO"
            now = BROKER.book.price(symbol)
            if post and limit is not None and now is not None and (limit >= now if side == "buy" else limit <= now):
                raise B.Refused("immediate", f"a postOnly order at {limit} would fill at once (the price is {now})")
            a = self._account()
            client = params.get("clientOrderId")
            if client is not None and any(o["client_id"] == client for o in a.orders):
                raise B.Refused("invalid", f"clientOrderId {client!r} is already used", cls=E.DuplicateOrderId)
            o = a.place(symbol=symbol, side=side, qty=qty, type=type, limit=limit, tif=tif, client_id=client,
                        info={"postOnly": post})
            return self._order(o)
        return self._private("create_order", {"exchange": self.id, "symbol": symbol, "type": type, "side": side,
                                              "amount": amount, "price": price, "params": params}, answer)

    def create_market_order(self, symbol, side, amount, price=None, params={}):
        return self.create_order(symbol, "market", side, amount, price, params)

    def create_limit_order(self, symbol, side, amount, price, params={}):
        return self.create_order(symbol, "limit", side, amount, price, params)

    def create_market_buy_order(self, symbol, amount, params={}):
        return self.create_order(symbol, "market", "buy", amount, None, params)

    def create_market_sell_order(self, symbol, amount, params={}):
        return self.create_order(symbol, "market", "sell", amount, None, params)

    def create_limit_buy_order(self, symbol, amount, price, params={}):
        return self.create_order(symbol, "limit", "buy", amount, price, params)

    def create_limit_sell_order(self, symbol, amount, price, params={}):
        return self.create_order(symbol, "limit", "sell", amount, price, params)

    def _find(self, a, id, symbol):
        o = next((o for o in a.orders if o["id"] == str(id)), None)
        if o is None or (symbol is not None and o["symbol"] != symbol):
            raise B.Refused("not_found", f"order {id} not found" + (f" for {symbol}" if symbol else ""))
        return o

    def cancel_order(self, id, symbol=None, params={}):
        def answer():
            self._params(params)
            self._need_symbol("cancelOrder", symbol)
            a = self._account()
            o = self._find(a, id, symbol)
            a.cancel(o)
            return self._order(o)
        return self._private("cancel_order", {"exchange": self.id, "id": id, "symbol": symbol}, answer)

    def cancel_all_orders(self, symbol=None, params={}):
        def answer():
            self._params(params)
            self._need_symbol("cancelAllOrders", symbol)
            a = self._account()
            out = []
            for o in a.open_orders():
                if symbol is None or o["symbol"] == symbol:
                    a.cancel(o)
                    out.append(self._order(o))
            return out
        return self._private("cancel_all_orders", {"exchange": self.id, "symbol": symbol}, answer)

    def fetch_order(self, id, symbol=None, params={}):
        def answer():
            self._params(params)
            self._need_symbol("fetchOrder", symbol)
            return self._order(self._find(self._account(), id, symbol))
        return self._private("fetch_order", {"exchange": self.id, "id": id, "symbol": symbol}, answer)

    def _orders(self, endpoint, method, keep, symbol, since, limit, params):
        def answer():
            self._params(params)
            self._need_symbol(method, symbol)
            if symbol is not None:
                self._symbol(symbol)
            items = [o for o in self._account().orders if keep(o) and (symbol is None or o["symbol"] == symbol)]
            return [self._order(o) for o in self._since_limit(items, since, limit, lambda o: B.ms(o["created"]))]
        return self._private(endpoint, {"exchange": self.id, "symbol": symbol, "since": since, "limit": limit}, answer)

    def fetch_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._orders("fetch_orders", "fetchOrders", lambda o: True, symbol, since, limit, params)

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._orders("fetch_open_orders", "fetchOpenOrders", lambda o: o["status"] == "open", symbol, since,
                            limit, params)

    def fetch_closed_orders(self, symbol=None, since=None, limit=None, params={}):
        return self._orders("fetch_closed_orders", "fetchClosedOrders", lambda o: o["status"] != "open", symbol,
                            since, limit, params)

    def fetch_my_trades(self, symbol=None, since=None, limit=None, params={}):
        def answer():
            self._params(params)
            self._need_symbol("fetchMyTrades", symbol)
            if symbol is not None:
                self._symbol(symbol)
            fills = [f for f in self._account().fills if symbol is None or f["symbol"] == symbol]
            return [self._trade(f) for f in self._since_limit(fills, since, limit, lambda f: B.ms(f["time"]))]
        return self._private("fetch_my_trades", {"exchange": self.id, "symbol": symbol, "since": since,
                                                 "limit": limit}, answer)


def _split(symbol: str):
    base, quote = symbol.split("/", 1)
    return base, quote


# ccxt's camelCase names for the same methods (exchange.fetchOHLCV, exchange.createOrder, ...)
for _name in [n for n in vars(Exchange) if not n.startswith("_") and "_" in n and callable(getattr(Exchange, n))]:
    _camel = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), _name).replace("Ohlcv", "OHLCV")
    setattr(Exchange, _camel, vars(Exchange)[_name])
