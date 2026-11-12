"""The mock alpaca package's own parts: its broker (prices, the account, the call log), request models that check
their fields as alpaca-py's pydantic models do, the SDK's errors, and replies as raw JSON for raw_data=True."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime
from enum import Enum

import _testbench_broker as B
import _testbench_mock as core

BROKER = B.Broker("alpaca")
TRADING = ("get_account, get_all_positions, get_open_position, submit_order, get_orders, get_order_by_id, "
           "cancel_orders, cancel_order_by_id, close_all_positions, close_position and get_clock")
PRICE_FIELDS = ("limit_price", "stop_price", "trail_price", "trail_percent")


def currency() -> str:
    return BROKER.opts.get("currency", "USD")


def asset_of(symbol: str):
    """A symbol's position key and quote currency: AAPL -> (AAPL, USD); BTC/USD -> (BTCUSD, USD), as alpaca names
    crypto positions."""
    cur = currency()
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        if quote != cur:
            raise B.Refused("invalid", f"the mock's alpaca account holds {cur}; {symbol} is quoted in {quote}, "
                                       "which it does not model")
        return base + quote, cur
    return symbol, cur


def symbol_of(asset: str) -> str:
    """The symbol a position is priced by: BTCUSD -> BTC/USD when the prices have that."""
    if BROKER.book.has(asset):
        return asset
    return next((s for s in BROKER.book.symbols() if "/" in s and s.replace("/", "") == asset), asset)


def account() -> B.Account:
    a = BROKER.account("alpaca", currency(), asset_of, symbol_of)
    BROKER.use(a)
    return a


def uid(kind: str, key) -> uuid.UUID:
    """The same id for the same thing in every run (the real API's are random)."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"https://testbench.mock/alpaca/{kind}/{key}")


def new_order_id(n: int) -> str:
    return str(uid("order", n))


def missing_names(module: str, names) -> object:
    """A module's __getattr__: a name the mock does not have (TrailingStopOrderRequest, OptionHistoricalDataClient)
    is refused and logged, saying what the module has."""
    def __getattr__(name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise BROKER.not_modelled(f"{module}.", name, ", ".join(names))
    return __getattr__


# ---- errors ---------------------------------------------------------------------------------------------------

class _Response:
    def __init__(self, status: int, body: dict):
        self.status_code, self._body, self.text = status, body, json.dumps(body)
        self.headers = core.Headers({"content-type": "application/json"})
        self.request = core.FakeRequest("https://paper-api.alpaca.markets/v2")
        self.ok = False

    def json(self):
        return self._body


class HTTPError(Exception):
    """What alpaca-py wraps: the HTTP error of the requests library, with .response and .request."""

    def __init__(self, response: _Response):
        super().__init__(f"{response.status_code} Client Error")
        self.response, self.request = response, response.request


_CODES = {"funds": (403, 40310000), "qty": (403, 40310000), "not_found": (404, 40410000), "invalid": (422, 42210000),
          "symbol": (422, 42210000), "state": (422, 42210000), "unsupported": (422, 42210000), "auth": (401, 40110000)}


def error(exc: B.Refused) -> Exception:
    from .common.exceptions import APIError
    if exc.kind == "live":
        return B.LiveAccountRefused(str(exc))
    if exc.kind == "argument":
        return exc.info.get("cls", ValueError)(str(exc))
    status, code = _CODES.get(exc.kind, (422, 42210000))
    message = str(exc)
    if exc.kind == "funds":
        message = f"insufficient buying power ({exc})"
    elif exc.kind == "qty":
        message = (f"insufficient qty available for order (requested: {B.fmt(exc.info['requested'])}, available: "
                   f"{B.fmt(exc.info['available'])})")
    body = {"code": code, "message": message}
    return APIError(json.dumps(body), HTTPError(_Response(status, body)))


# ---- request models -------------------------------------------------------------------------------------------

def _coerce(name: str, kind, v):
    if isinstance(kind, type) and issubclass(kind, Enum):
        if isinstance(v, kind):
            return v
        try:
            return kind(v.value if isinstance(v, Enum) else v)
        except ValueError:
            raise ValueError(f"{name}: Input should be " + ", ".join(repr(m.value) for m in kind) +
                             f", got {v!r}") from None
    if kind == "float":
        try:
            return B.number(v, name)
        except B.Refused as exc:
            raise ValueError(str(exc)) from None
    if kind == "int":
        if not core.is_int(v):
            raise ValueError(f"{name} must be an integer, got {core.kind(v)} {v!r}")
        return v
    if kind == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"{name} must be true or false, got {v!r}")
        return v
    if kind == "str":
        if not isinstance(v, str):
            raise ValueError(f"{name} must be a string, got {core.kind(v)} {v!r}")
        return v
    if kind == "datetime":
        try:
            return B.parse_time(v)
        except ValueError as exc:
            raise ValueError(f"{name}: {exc}") from None
    if kind == "symbols":
        if isinstance(v, str) and v:
            return v
        if isinstance(v, (list, tuple)) and v and all(isinstance(s, str) and s for s in v):
            return list(v)
        raise ValueError(f"{name} is a symbol such as 'AAPL' or a list of them, got {v!r}")
    if kind == "strs":
        if isinstance(v, (list, tuple)) and all(isinstance(s, str) for s in v):
            return list(v)
        raise ValueError(f"{name} is a list of strings, got {v!r}")
    if isinstance(kind, type):
        if isinstance(v, kind):
            return v
        if isinstance(v, dict) and issubclass(kind, Request):
            return kind(**v)
        raise ValueError(f"{name} must be a {kind.__name__}, got {core.kind(v)} {v!r}")
    return v


class Request(core.Obj):
    """A request model: fields checked when it is made, as alpaca-py's pydantic models check them, raising pydantic's
    ValidationError (a ValueError). A field the model does not have is refused too: the real SDK drops it without a
    word, so a misspelt one (limit_prce=) would silently do nothing."""
    _fields: dict = {}                       # name -> what it takes: "float", "str", ..., an Enum or a class
    _need: tuple = ()
    _set: dict = {}                          # what the class sets itself (an order request's type)
    _prices: tuple = ()                      # which of PRICE_FIELDS an order request takes
    _order_type = None

    def __init__(self, **data):
        title = type(self).__name__
        values = {}
        try:
            if self._order_type is not None:
                for k in PRICE_FIELDS:
                    if data.get(k) is not None and k not in self._prices:
                        raise ValueError(f"{k} is not supported for {self._order_type} orders.")
            unknown = [k for k in data if k not in self._fields]
            if unknown:
                raise ValueError(f"{title} has no field {unknown[0]!r}{B.did_you_mean(unknown[0], self._fields)}; "
                                 "alpaca-py would drop it without a word, so it would do nothing. Its fields are "
                                 f"{', '.join(self._fields)}")
            data = {**data, **self._set}
            for name, kind in self._fields.items():
                v = data.get(name)
                values[name] = None if v is None else _coerce(name, kind, v)
            missing = [n for n in self._need if values.get(n) is None]
            if missing:
                raise ValueError(f"{missing[0]} is required")
            self._check(values)
        except ValueError as exc:
            raise BROKER.log_refusal(title, raw(data), B.invalid(title, str(exc), data)) from None
        super().__init__(**values)

    def _check(self, values: dict) -> None:
        pass

    def to_request_fields(self) -> dict:
        d = {k: v for k, v in vars(self).items() if k[0] != "_" and v is not None}
        if "symbol_or_symbols" in d:
            s = d.pop("symbol_or_symbols")
            d["symbols"] = ",".join(s) if isinstance(s, list) else s
        return d


# ---- raw replies (raw_data=True) ------------------------------------------------------------------------------

def raw(v):
    """A reply as the API's JSON has it: enums as their values, times as text, ids as strings."""
    if isinstance(v, core.Obj):
        v = {k: x for k, x in vars(v).items() if k[0] != "_"}
    if isinstance(v, dict):
        return {str(k): raw(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [raw(x) for x in v]
    if isinstance(v, Enum):
        return v.value
    if isinstance(v, datetime):
        return B.iso(v)
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if hasattr(v, "value") and type(v).__name__ == "TimeFrame":
        return v.value
    return v
