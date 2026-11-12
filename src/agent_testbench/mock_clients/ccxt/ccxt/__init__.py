"""Mock `ccxt` package from agent-testbench (docs/mocks.md#brokers): ccxt.binance(), ccxt.kraken(), ... answered
here from the step's prices, with an account per exchange whose balances and orders add up. No network. An account
call on a client without set_sandbox_mode(True) is a live account: refused unless the step's mocks: options say
allow_live: true (kraken, coinbase, kucoin and bitstamp have no sandbox in ccxt). Every call is one line of
mock_calls.jsonl.

Follows ccxt 4.5.92 (its unified API for spot markets: method names, arguments, dict structures, errors).
"""
import _testbench_mock as _core

from .base import errors
from .base.errors import *  # noqa: F401,F403 - ccxt.InsufficientFunds and the rest, as the real package has them
from .base.errors import __all__ as _errors
from .base.exchange import Exchange

__version__ = "4.5.92+testbench.mock"
TESTBENCH_MOCK = True

_BINANCE_NEEDS = ("cancelOrder", "fetchOrder", "fetchOrders", "cancelAllOrders", "fetchClosedOrders", "fetchMyTrades")
_M, _H, _D = ["1m", "5m", "15m", "30m"], ["1h", "2h", "4h", "6h", "12h"], ["1d", "1w"]


def _exchange(id, name, *, sandbox, currency, timeframes, credentials=("apiKey", "secret"), needs=()):
    cls = type(id, (Exchange,), {"id": id, "name": name, "sandbox_url": sandbox, "currency": currency,
                                 "credentials": credentials, "symbol_needed": needs,
                                 "timeframes": {t: t for t in timeframes}, "__module__": __name__})
    globals()[id] = cls
    return id


exchanges = [
    _exchange("binance", "Binance", sandbox=True, currency="USDT", needs=_BINANCE_NEEDS,
              timeframes=["1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w",
                          "1M"]),
    _exchange("binanceus", "Binance US", sandbox=True, currency="USD", needs=_BINANCE_NEEDS,
              timeframes=["1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w",
                          "1M"]),
    _exchange("bybit", "Bybit", sandbox=True, currency="USDT", needs=("cancelOrder",),
              timeframes=["1m", "3m", *_M[1:], *_H, "1d", "1w", "1M"]),
    _exchange("okx", "OKX", sandbox=True, currency="USDT", needs=("cancelOrder", "fetchOrder"),
              credentials=("apiKey", "secret", "password"),
              timeframes=["1m", "3m", *_M[1:], *_H, "1d", "1w", "1M", "3M"]),
    _exchange("kucoin", "KuCoin", sandbox=False, currency="USDT", credentials=("apiKey", "secret", "password"),
              timeframes=["1m", "3m", *_M[1:], "1h", "2h", "4h", "6h", "8h", "12h", "1d", "1w", "1M"]),
    _exchange("coinbase", "Coinbase Advanced", sandbox=False, currency="USD",
              timeframes=[*_M, "1h", "2h", "6h", "1d"]),
    _exchange("coinbaseexchange", "Coinbase Exchange", sandbox=True, currency="USD", needs=("fetchMyTrades",),
              credentials=("apiKey", "secret", "password"), timeframes=["1m", "5m", "15m", "1h", "6h", "1d"]),
    _exchange("kraken", "Kraken", sandbox=False, currency="USD", timeframes=[*_M, "1h", "4h", "1d", "1w", "2w"]),
    _exchange("krakenfutures", "Kraken Futures", sandbox=True, currency="USD",
              timeframes=[*_M, "1h", "4h", "12h", "1d", "1w"]),
    _exchange("bitstamp", "Bitstamp", sandbox=False, currency="USD",
              timeframes=["1m", "3m", *_M[1:], *_H, "1d", "1w"]),
    _exchange("gemini", "Gemini", sandbox=True, currency="USD", needs=("fetchMyTrades",),
              timeframes=[*_M, "1h", "6h", "1d"]),
]
__all__ = ["exchanges", "Exchange", "errors", *exchanges, *_errors]

_core.absent_modules("ccxt", "ccxt.base (errors, exchange); exchanges " + ", ".join(exchanges) + " (no async_support, "
                     "no pro)")


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)
    from .base.exchange import BROKER
    raise BROKER.not_modelled("ccxt.", name, "the exchanges " + ", ".join(exchanges) + ", Exchange and the errors")
