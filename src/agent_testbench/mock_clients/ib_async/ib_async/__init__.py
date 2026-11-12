"""Mock `ib_async` package from agent-testbench (docs/mocks.md#brokers): IB() connected to an account kept here
instead of TWS or IB Gateway, answered from the step's prices. No network. connect() on a live port (7496 TWS,
4001 Gateway) is refused unless the step's mocks: options say allow_live: true. Every call is one line of
mock_calls.jsonl. `import ib_insync` loads the same mock.

Follows ib_async 2.1.0 (IB's account, order and market data methods, contracts, orders, Trade and its events).
"""
import _testbench_mock as _core

from . import util
from ._mock import BROKER as _BROKER
from ._mock import Event
from .contract import Contract, Crypto, Forex, Stock, TagValue
from .ib import IB, RequestError, StartupFetch, StartupFetchALL, StartupFetchNONE
from .objects import (AccountValue, BarData, BarDataList, CommissionReport, Execution, Fill, PortfolioItem, Position,
                      TradeLogEntry)
from .order import LimitOrder, MarketOrder, Order, OrderStatus, StopLimitOrder, StopOrder, Trade
from .ticker import Ticker

__version__ = "2.1.0+testbench.mock"
__version_info__ = (2, 1, 0)
TESTBENCH_MOCK = True
__all__ = ["Event", "util", "IB", "RequestError", "StartupFetch", "StartupFetchALL", "StartupFetchNONE", "Contract",
           "Stock", "Forex", "Crypto", "TagValue", "Order", "MarketOrder", "LimitOrder", "StopOrder", "StopLimitOrder",
           "OrderStatus", "Trade", "AccountValue", "BarData", "BarDataList", "CommissionReport", "Execution", "Fill",
           "PortfolioItem", "Position", "TradeLogEntry", "Ticker", "__version__", "__version_info__"]

_core.absent_modules("ib_async", "ib_async.ib, .contract, .order, .objects, .ticker and .util")


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)
    raise _BROKER.not_modelled("ib_async.", name, ", ".join(n for n in __all__ if not n.startswith("_")))
