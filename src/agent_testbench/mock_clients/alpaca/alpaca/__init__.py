"""Mock `alpaca` package (alpaca-py) from agent-testbench (docs/mocks.md#brokers): trading and historical bars
answered here from the step's prices, with a paper account whose cash, positions and orders add up. No network, no
key needed beyond the SDK's own check that one is given. A live account (TradingClient(paper=False)) is refused unless
the step's mocks: options say allow_live: true. Every call is one line of mock_calls.jsonl.

Follows alpaca-py 0.45.0 (TradingClient, StockHistoricalDataClient, CryptoHistoricalDataClient and their request
models, enums and replies).
"""
import _testbench_mock as _core

__version__ = "0.45.0+testbench.mock"
TESTBENCH_MOCK = True
__all__ = []

_core.absent_modules("alpaca", "alpaca.common (exceptions, enums), alpaca.trading (client, requests, enums, models) "
                     "and alpaca.data (historical with stock and crypto, requests, timeframe, models with bars, enums)")
