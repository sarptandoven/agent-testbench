"""alpaca.data.historical.stock of the mock alpaca package: StockHistoricalDataClient, bars from the step's prices
(never after the mock's current bar)."""
import _testbench_broker as B

from ..._mock import BROKER, error, raw
from ..models import Bar, BarSet
from ..requests import CryptoBarsRequest, StockBarsRequest

__all__ = ["StockHistoricalDataClient"]
_UNIT = {"Min": 60, "Hour": 3600, "Day": 86400, "Week": 604800, "Month": 2592000}


def _credentials(name, api_key, secret_key, oauth_token, required=True):
    args = {"api_key": "<given>" if api_key else None, "secret_key": "<given>" if secret_key else None,
            "oauth_token": "<given>" if oauth_token else None}
    why = ("You must supply a method of authentication" if required and not oauth_token and not api_key else
           "Either an oauth_token or an api_key may be supplied, but not both" if oauth_token and (
               api_key or secret_key) else
           "A corresponding secret_key must be supplied with the api_key" if api_key and not secret_key else None)
    if why:
        raise BROKER.log_refusal(name, args, ValueError(why))


class _DataClient:
    _has = ""

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise BROKER.not_modelled(f"{type(self).__name__}.", name, self._has)

    def _bars(self, endpoint, request_params, crypto):
        if not isinstance(request_params, (StockBarsRequest, CryptoBarsRequest)):
            raise BROKER.log_refusal(endpoint, {"request_params": repr(request_params)[:200]}, TypeError(
                f"{endpoint}() takes a {'CryptoBarsRequest' if crypto else 'StockBarsRequest'} from "
                f"alpaca.data.requests, got {type(request_params).__name__}"))
        r = request_params

        def answer():
            book = BROKER.book
            symbols = [r.symbol_or_symbols] if isinstance(r.symbol_or_symbols, str) else r.symbol_or_symbols
            tf = r.timeframe
            data = {}
            for s in symbols:
                if not book.has(s):
                    raise B.SetupError(f"mock alpaca: {book.missing(s)}. Add its bars to the prices file, or the "
                                       "symbol to price:.")
                note = B.spacing_note(book, s, tf.amount * _UNIT[tf.unit.value], tf.value)
                if note:
                    BROKER.note(note)
                bars = book.history(s, r.start, r.end)
                if r.sort is not None and r.sort.value == "desc":
                    bars = bars[::-1]
                if r.limit is not None:
                    bars = bars[:r.limit]
                if bars:
                    data[s] = [Bar(symbol=s, timestamp=b.time, open=b.open, high=b.high, low=b.low, close=b.close,
                                   volume=b.volume, trade_count=None, vwap=None) for b in bars]
            if self._raw:
                return {s: [{"t": B.iso(b.timestamp), "o": b.open, "h": b.high, "l": b.low, "c": b.close,
                             "v": b.volume} for b in bars] for s, bars in data.items()}
            return BarSet(data=data)
        return BROKER.call(endpoint, {"request_params": raw(r.to_request_fields())}, answer, error=error)


class StockHistoricalDataClient(_DataClient):
    _has = "get_stock_bars"

    def __init__(self, api_key=None, secret_key=None, oauth_token=None, use_basic_auth=False, raw_data=False,
                 url_override=None, sandbox=False):
        _credentials("StockHistoricalDataClient", api_key, secret_key, oauth_token)
        self._raw = raw_data

    def get_stock_bars(self, request_params):
        return self._bars("get_stock_bars", request_params, crypto=False)
