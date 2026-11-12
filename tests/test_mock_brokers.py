"""The drop-in mock broker SDKs (mocks: [alpaca], [ccxt], [ib_async] on a step) and their mock_calls expectation:
orders fill at the step's prices with its fees, the account adds up, a live account is refused, and anything the mock
does not model says so. The network is cut in every test, so a mock that reached for it would fail."""
import importlib
import inspect
import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from agent_testbench import config as C, expect, mocks as M
from conftest import cli, write_config

PATHS = M.client_paths(M.BROKERS)
NAMES = ("alpaca", "ccxt", "ib_async", "ib_insync", "_testbench_mock", "_testbench_broker")
PRICES = """time,symbol,close
2024-01-02,AAPL,100
2024-01-03,AAPL,102
2024-01-04,AAPL,98
2024-01-05,AAPL,105
2024-01-02,BTC/USD,40000
2024-01-03,BTC/USD,41000
2024-01-04,BTC/USD,39000
2024-01-05,BTC/USD,42000
2024-01-02,ETH/USDT,2000
2024-01-03,ETH/USDT,2100
2024-01-04,ETH/USDT,1900
2024-01-05,ETH/USDT,2050
"""
LIVE_RULE = "this test asked for a live account"


def _no_network(*a, **k):
    raise AssertionError("a mock broker opened a network connection")


def _forget():
    for k in [k for k in sys.modules if k.split(".")[0] in NAMES]:
        del sys.modules[k]


@pytest.fixture
def broker(tmp_path, monkeypatch):
    """load("alpaca", **options) makes the next import of that SDK a fresh mock, logging to tmp_path."""
    saved = {k: v for k, v in sys.modules.items() if k.split(".")[0] in NAMES}
    for p in reversed(PATHS):
        monkeypatch.syspath_prepend(p)
    monkeypatch.setenv("TESTBENCH_MOCK_CALLS", str(tmp_path / "mock_calls.jsonl"))
    monkeypatch.setattr(socket.socket, "connect", _no_network)
    (tmp_path / "prices.csv").write_text(PRICES)

    def load(name, **opts):
        monkeypatch.setenv(f"TESTBENCH_MOCK_{name.upper()}", json.dumps({"prices": str(tmp_path / "prices.csv"),
                                                                         **opts}))
        _forget()
        return importlib.import_module(name)
    yield load
    _forget()
    sys.modules.update(saved)


def logged(tmp_path):
    f = tmp_path / "mock_calls.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []


def refusals(tmp_path):
    return [r for r in logged(tmp_path) if (r["error"] or {}).get("kind") == "refused"]


# ---- alpaca -------------------------------------------------------------------------------------------------

def test_alpaca_trades_at_the_step_prices_with_fees_and_the_account_adds_up(broker, tmp_path):
    broker("alpaca", now="2024-01-03", cash=10000, fee_usd=1, slippage_bps=10)
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    t = TradingClient("key", "secret", paper=True)
    o = t.submit_order(MarketOrderRequest(symbol="AAPL", qty=10, side=OrderSide.BUY, time_in_force=TimeInForce.DAY))
    assert (o.status, o.filled_qty, o.filled_avg_price, o.symbol) == (OrderStatus.FILLED, "10", "102.102", "AAPL")
    a = t.get_account()
    assert (a.cash, a.equity, a.buying_power) == ("8977.98", "9997.98", "8977.98"), "10000 - 10 x 102.102 - 1"
    lim = t.submit_order(LimitOrderRequest(symbol="AAPL", qty=5, side="buy", time_in_force="gtc", limit_price=99))
    assert lim.status == OrderStatus.NEW and lim.limit_price == "99"
    assert t.get_account().buying_power == "8481.98", "5 x 99 + its fee are held for the open order"
    pos = t.get_open_position("AAPL")
    assert (pos.qty, pos.avg_entry_price, pos.market_value, pos.unrealized_pl) == ("10", "102.102", "1020", "-1.02")
    assert [x.id for x in t.get_orders()] == [lim.id]
    t.cancel_order_by_id(lim.id)
    assert t.get_orders() == [] and t.get_account().buying_power == "8977.98"
    crypto = t.submit_order(MarketOrderRequest(symbol="BTC/USD", notional=1026.025, side="buy", time_in_force="gtc"))
    assert (crypto.filled_qty, crypto.filled_avg_price) == ("0.025", "41041"), "a notional buys at the fill price"
    assert t.get_open_position("BTC/USD").symbol == t.get_open_position("BTCUSD").symbol == "BTCUSD"
    closed = t.close_all_positions(cancel_orders=True)
    assert [(c.symbol, c.status, c.body.filled_avg_price) for c in closed] == [("AAPL", 200, "101.898"),
                                                                                ("BTCUSD", 200, "40959")]
    assert t.get_all_positions() == [] and t.get_account().cash == "9991.91", "4 fills, 4 fees of 1"
    raw = TradingClient("key", "secret", raw_data=True).get_account()
    assert raw["cash"] == "9991.91" and raw["status"] == "ACTIVE", "raw_data=True answers the API's JSON"
    recs = logged(tmp_path)
    assert [r["endpoint"] for r in recs[:3]] == ["TradingClient", "submit_order", "get_account"]
    fill = recs[1]["fills"][0]
    assert (fill["qty"], fill["price"], fill["fee"], recs[1]["cash"]) == (10.0, 102.102, 1.0, 8977.98)
    assert not any(r["live"] for r in recs) and refusals(tmp_path) == []


def test_alpaca_bars_stop_at_the_current_bar(broker, tmp_path):
    broker("alpaca", now="2024-01-04")
    from alpaca.data.historical import CryptoHistoricalDataClient, StockHistoricalDataClient
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    stocks = StockHistoricalDataClient("key", "secret")
    bars = stocks.get_stock_bars(StockBarsRequest(symbol_or_symbols=["AAPL"], timeframe=TimeFrame.Day))
    assert [b.close for b in bars["AAPL"]] == [100, 102, 98], "no bar after now: no look-ahead"
    assert bars["AAPL"][0].timestamp == datetime(2024, 1, 2, tzinfo=timezone.utc) and "AAPL" in bars
    some = stocks.get_stock_bars(StockBarsRequest(symbol_or_symbols="AAPL", timeframe=TimeFrame.Day,
                                                  start=datetime(2024, 1, 3), limit=1))
    assert [b.close for b in some["AAPL"]] == [102], "as the API: limit keeps the earliest bars from start"
    btc = CryptoHistoricalDataClient().get_crypto_bars(CryptoBarsRequest(
        symbol_or_symbols="BTC/USD", timeframe=TimeFrame(1, TimeFrameUnit.Hour)))
    assert len(btc["BTC/USD"]) == 3
    assert "the prices file's bars are 1 day apart" in logged(tmp_path)[-1]["notes"][0]
    with pytest.raises(KeyError, match="No key MSFT was found"):
        btc["MSFT"]


def test_alpaca_refuses_what_alpaca_refuses(broker, tmp_path):
    broker("alpaca", cash=1000)
    from alpaca.common.exceptions import APIError
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
    for make, why in [(lambda: LimitOrderRequest(symbol="AAPL", qty=1, side="buy", time_in_force="day"),
                       "limit_price is required"),
                      (lambda: MarketOrderRequest(symbol="AAPL", qty=1, side="buy", time_in_force="day", limit_price=3),
                       "limit_price is not supported for market orders."),
                      (lambda: MarketOrderRequest(symbol="AAPL", side="buy", time_in_force="day"),
                       "At least one of qty or notional must be provided"),
                      (lambda: MarketOrderRequest(symbol="AAPL", qty=1, side="BUY", time_in_force="day"),
                       "side: Input should be 'buy', 'sell'"),
                      (lambda: MarketOrderRequest(symbol="AAPL", qty=1, side="buy", time_in_forc="day"),
                       "has no field 'time_in_forc' (did you mean 'time_in_force'?); alpaca-py would drop it")]:
        with pytest.raises(ValueError, match=re.escape(why)):
            make()
    with pytest.raises(ValueError, match="You must supply a method of authentication"):
        TradingClient()
    with pytest.raises(ValueError, match="You must supply a method of authentication"):
        StockHistoricalDataClient()
    t = TradingClient("key", "secret")
    for order, status, code, why in [
            (MarketOrderRequest(symbol="AAPL", qty=100, side="buy", time_in_force="day"), 403, 40310000,
             "insufficient buying power (this order needs 10,500.00 USD and 1,000.00 USD is free"),
            (MarketOrderRequest(symbol="AAPL", qty=1, side="sell", time_in_force="day"), 403, 40310000,
             "insufficient qty available for order (requested: 1, available: 0)"),
            (MarketOrderRequest(symbol="BTC/USD", qty=0.01, side="buy", time_in_force="day"), 422, 42210000,
             "crypto orders take gtc or ioc, not day"),
            (MarketOrderRequest(symbol="AAPL", qty=0.5, side="buy", time_in_force="gtc"), 422, 42210000,
             "fractional orders must be DAY orders")]:
        with pytest.raises(APIError) as info:
            t.submit_order(order)
        assert (info.value.status_code, info.value.code) == (status, code) and why in info.value.message
    with pytest.raises(APIError, match="position does not exist"):
        t.get_open_position("AAPL")
    done = t.submit_order(MarketOrderRequest(symbol="AAPL", qty=1, side="buy", time_in_force="day"))
    with pytest.raises(APIError) as info:
        t.cancel_order_by_id(done.id)
    assert info.value.message == 'order is already in "filled" state' and json.loads(str(info.value))["code"] == 42210000
    with pytest.raises(TypeError, match="submit_order\\(\\) takes an order request"):
        t.submit_order({"symbol": "AAPL", "qty": 1})
    with pytest.raises(core_setup_error(), match="no price for TSLA: the mock's prices"):
        t.submit_order(MarketOrderRequest(symbol="TSLA", qty=1, side="buy", time_in_force="day"))
    kinds = [(r["endpoint"], r["error"]["type"]) for r in refusals(tmp_path)]
    assert ("LimitOrderRequest", "ValidationError" if _has_pydantic() else "MockValidationError") in kinds
    assert ("submit_order", "APIError") in kinds and ("TradingClient", "ValueError") in kinds
    assert len(kinds) == 15, "every refusal is a line of the log, so a swallowed one still fails the step"


def core_setup_error():
    return importlib.import_module("_testbench_mock").SetupError


def _has_pydantic():
    try:
        import pydantic_core  # noqa: F401
        return True
    except ImportError:
        return False


def test_resting_orders_fill_when_the_clock_reaches_a_crossing_price(broker, tmp_path):
    broker("alpaca", now="2024-01-02", advance_every=1, positions={"AAPL": 5})      # AAPL 100, 102, 98, 105
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, StopOrderRequest
    t = TradingClient("key", "secret")                                  # making a client is not a request
    buy = t.submit_order(LimitOrderRequest(symbol="AAPL", qty=1, side="buy", time_in_force="gtc", limit_price=99))
    stop = t.submit_order(StopOrderRequest(symbol="AAPL", qty=5, side="sell", time_in_force="gtc", stop_price=99))
    assert (buy.status.value, stop.status.value) == ("new", "new")      # 100, then 102: neither crosses
    filled = t.get_orders(GetOrdersRequest(status="closed", direction="asc"))   # the clock is at 2024-01-04: 98
    assert [(o.side.value, o.filled_avg_price) for o in filled] == [("buy", "98"), ("sell", "98")]
    assert t.get_open_position("AAPL").qty == "1" and t.get_account().cash == "100392"
    rec = logged(tmp_path)[3]
    assert rec["time"] == "2024-01-04T00:00:00Z" and len(rec["fills"]) == 2
    assert t.get_clock().timestamp == datetime(2024, 1, 5, tzinfo=timezone.utc)
    assert t.get_clock().timestamp == datetime(2024, 1, 5, tzinfo=timezone.utc), "the clock stops at the last bar"


# ---- ccxt ---------------------------------------------------------------------------------------------------

def test_ccxt_exchange_on_a_sandbox_account(broker, tmp_path):
    ccxt = broker("ccxt", cash=10000, fee_bps=10)
    ex = ccxt.binance({"apiKey": "key", "secret": "secret", "enableRateLimit": True})
    ex.set_sandbox_mode(True)
    markets = ex.load_markets()
    assert sorted(markets) == ["BTC/USD", "ETH/USDT"] and ex.market("ETH/USDT")["quote"] == "USDT"
    assert ex.has["fetchOHLCV"] and not ex.has["fetchPositions"]
    ticker = ex.fetch_ticker("ETH/USDT")
    assert (ticker["last"], ticker["previousClose"], ticker["datetime"]) == (2050, 1900, "2024-01-05T00:00:00.000Z")
    assert [c[4] for c in ex.fetch_ohlcv("ETH/USDT", "1d", limit=2)] == [1900, 2050]
    since = ex.parse8601("2024-01-03T00:00:00Z")
    assert [c[4] for c in ex.fetchOHLCV("ETH/USDT", "1d", since=since, limit=2)] == [2100, 1900]
    o = ex.create_order("ETH/USDT", "market", "buy", 2)
    assert (o["status"], o["average"], o["filled"], o["fee"]) == ("closed", 2050, 2, {"cost": 4.1, "currency": "USDT"})
    lim = ex.create_limit_buy_order("ETH/USDT", 1, 2000)
    assert (lim["status"], lim["price"], lim["remaining"]) == ("open", 2000, 1)
    bal = ex.fetch_balance()
    assert bal["USDT"] == {"free": pytest.approx(3893.9), "used": 2002.0, "total": pytest.approx(5895.9)}
    assert bal["ETH"] == {"free": 2.0, "used": 0.0, "total": 2.0} and bal["total"]["ETH"] == 2.0
    with pytest.raises(ccxt.InsufficientFunds, match="binance this order needs 20,520.50 USDT"):
        ex.create_order("ETH/USDT", "market", "buy", 10)
    with pytest.raises(ccxt.ArgumentsRequired, match="binance cancelOrder\\(\\) requires a symbol argument"):
        ex.cancel_order(lim["id"])
    assert ex.cancel_order(lim["id"], "ETH/USDT")["status"] == "canceled" and ex.fetch_open_orders("ETH/USDT") == []
    sold = ex.createOrder("ETH/USDT", "limit", "sell", 2, 2000)
    assert (sold["status"], sold["average"]) == ("closed", 2050), "a sell limit below the price fills at the price"
    assert ex.fetch_balance()["total"] == {"USDT": pytest.approx(9991.8)}
    trades = ex.fetch_my_trades("ETH/USDT")
    assert [(x["side"], x["amount"], x["price"], x["fee"]["cost"]) for x in trades] == [("buy", 2, 2050, 4.1),
                                                                                       ("sell", 2, 2050, 4.1)]
    with pytest.raises(ccxt.ArgumentsRequired):
        ex.create_order("ETH/USDT", "limit", "buy", 1)
    with pytest.raises(ccxt.BadRequest, match="does not have a '7m' timeframe"):
        ex.fetch_ohlcv("ETH/USDT", "7m")
    with pytest.raises(ccxt.BadSymbol, match="unified, BASE/QUOTE"):
        ex.fetch_ticker("ETHUSDT")
    with pytest.raises(ccxt.NotSupported, match="kraken does not have a sandbox URL"):
        ccxt.kraken().set_sandbox_mode(True)
    keyless = ccxt.okx()
    keyless.set_sandbox_mode(True)
    with pytest.raises(ccxt.AuthenticationError, match='okx requires "apiKey" credential'):
        keyless.fetch_balance()
    assert [r["live"] for r in logged(tmp_path)] == [False] * len(logged(tmp_path))
    assert len(refusals(tmp_path)) == 7


# ---- ib_async -----------------------------------------------------------------------------------------------

def test_ib_async_places_orders_on_the_paper_account(broker, tmp_path):
    ib_async = broker("ib_async", now="2024-01-03", cash=10000, fee_usd=1)
    from ib_async import IB, LimitOrder, MarketOrder, Stock, StopOrder, util
    import ib_insync
    assert ib_insync.IB is IB and ib_insync.util is util, "import ib_insync loads the same mock"
    ib = IB()
    errors = []
    ib.errorEvent += lambda req_id, code, message, contract: errors.append((code, message))
    assert ib.connect("127.0.0.1", 7497, clientId=1) is ib and ib.managedAccounts() == ["DU1234567"]
    aapl = Stock("AAPL", "SMART", "USD")
    assert ib.qualifyContracts(aapl)[0].conId > 0
    bars = ib.reqHistoricalData(aapl, endDateTime="", durationStr="2 D", barSizeSetting="1 day", whatToShow="TRADES",
                                useRTH=True)
    assert [(str(b.date), b.close) for b in bars] == [("2024-01-02", 100), ("2024-01-03", 102)]
    older = ib.reqHistoricalData(aapl, "20240102 00:00:00", "1 D", "1 day", "TRADES", True)
    assert [b.close for b in older] == [100]
    held = []
    ib.execDetailsEvent += lambda trade, fill: held.append([p.position for p in ib.positions()])   # a call in a call
    t = ib.placeOrder(aapl, MarketOrder("BUY", 10))
    assert held == [[10]] and [len(r.get(k, [])) for r in logged(tmp_path) if r["endpoint"] == "placeOrder"
                             for k in ("orders", "fills")] == [1, 1], "the handler's call leaves placeOrder's line whole"
    assert (t.orderStatus.status, t.orderStatus.avgFillPrice, t.filled(), t.isDone()) == ("Filled", 102, 10, True)
    assert t.fills[0].execution.side == "BOT" and t.fills[0].commissionReport.commission == 1
    lim = ib.placeOrder(aapl, LimitOrder("BUY", 5, 100.0))
    stop = ib.placeOrder(aapl, StopOrder("SELL", 10, 99.0))
    assert (lim.orderStatus.status, stop.orderStatus.status) == ("Submitted", "PreSubmitted")
    assert [o.orderType for o in ib.openOrders()] == ["LMT", "STP"]
    values = {v.tag: v.value for v in ib.accountValues() if v.currency == "USD"}
    assert (values["NetLiquidation"], values["TotalCashValue"], values["AvailableFunds"]) == ("9999.00", "8979.00",
                                                                                           "8478.00")
    assert [(p.contract.symbol, p.position, p.avgCost) for p in ib.positions()] == [("AAPL", 10, 102)]
    assert ib.reqMktData(aapl).marketPrice() == 102 and ib.reqTickers(aapl)[0].close == 100
    big = ib.placeOrder(aapl, MarketOrder("BUY", 1000))
    assert big.orderStatus.status == "Inactive" and big.log[-1].errorCode == 201
    assert errors[-1][0] == 201 and "Order rejected - reason: this order needs 102,001.00 USD" in errors[-1][1]
    assert ib.cancelOrder(lim.order).orderStatus.status == "Cancelled"
    ib.reqGlobalCancel()
    assert stop.orderStatus.status == "Cancelled" and ib.openTrades() == []
    IB.RaiseRequestErrors = True
    try:
        with pytest.raises(ib_async.RequestError, match="action 'buy' must be 'BUY' or 'SELL'"):
            ib.placeOrder(aapl, MarketOrder("buy", 1))
    finally:
        IB.RaiseRequestErrors = False
    sold = ib.placeOrder(aapl, MarketOrder("SELL", 10))
    assert sold.fills[0].commissionReport.realizedPNL == -1, "sold at the price it was bought at, less the fee"
    ib.disconnect()
    with pytest.raises(ConnectionError, match="Not connected"):
        ib.placeOrder(aapl, MarketOrder("BUY", 1))
    assert [r["error"]["type"] for r in refusals(tmp_path)] == ["RequestError", "RequestError", "ConnectionError"]


# ---- the live-account rule and what is not modelled --------------------------------------------------------

LIVE = {"alpaca": ("from alpaca.trading.client import TradingClient\n"
                   "TradingClient('key', 'secret', paper=False).get_account()", "TradingClient(paper=False)", 2),
        "ccxt": ("import ccxt\nccxt.binance({'apiKey': 'key', 'secret': 'secret'}).fetch_balance()",
                 "ccxt.binance() without set_sandbox_mode(True)", 1),
        "ib_async": ("from ib_async import IB\nIB().connect('127.0.0.1', 4001).positions()",
                     "IB().connect(port=4001), the live port of IB Gateway", 2)}


@pytest.mark.parametrize("sdk", sorted(LIVE))
def test_a_live_account_is_refused_unless_allowed(broker, tmp_path, sdk):
    code, what, calls = LIVE[sdk]
    broker(sdk)
    with pytest.raises(RuntimeError) as info:
        exec(code, {})
    assert type(info.value).__name__ == "LiveAccountRefused"
    assert f"mock {sdk}: {LIVE_RULE} ({what}); trading code under test must use paper/sandbox, see " \
           "docs/mocks.md#brokers" in str(info.value) and "allow_live: true" in str(info.value)
    rec = logged(tmp_path)[-1]
    assert (rec["live"], rec["error"]["kind"], rec["error"]["type"]) == (True, "refused", "LiveAccountRefused")
    (tmp_path / "mock_calls.jsonl").unlink()
    broker(sdk, allow_live=True)                    # code only exercised offline: let through, and still recorded
    exec(code, {})
    assert [r["live"] for r in logged(tmp_path)] == [True] * calls and refusals(tmp_path) == []


def test_ccxt_market_data_needs_no_sandbox_but_an_account_does(broker, tmp_path):
    ccxt = broker("ccxt")
    ex = ccxt.kraken()
    assert ex.fetch_ticker("BTC/USD")["last"] == 42000, "no account is involved"
    with pytest.raises(RuntimeError, match="kraken has no sandbox in ccxt, so every account call of this code "
                                           "reaches a real account"):
        ex.create_order("BTC/USD", "market", "buy", 1)
    assert [(r["endpoint"], r["live"]) for r in logged(tmp_path)] == [("fetch_ticker", False), ("create_order", True)]


NOT_MODELLED = {"alpaca": ("from alpaca.trading.client import TradingClient\n"
                           "TradingClient('key', 'secret').get_portfolio_history()",
                           "TradingClient.get_portfolio_history is not modelled; the mock answers get_account"),
                "ccxt": ("import ccxt\nccxt.binance().fetch_positions()",
                         "binance.fetch_positions is not modelled; the mock answers load_markets"),
                "ib_async": ("from ib_async import IB\nIB().reqScannerData(None)",
                             "IB.reqScannerData is not modelled; the mock answers connect")}


@pytest.mark.parametrize("sdk", sorted(NOT_MODELLED))
def test_what_the_mock_does_not_model_says_so(broker, tmp_path, sdk):
    code, why = NOT_MODELLED[sdk]
    broker(sdk)
    with pytest.raises(AttributeError, match=re.escape(why)) as info:
        exec(code, {})
    assert type(info.value).__name__ == "NotModelled" and "docs/mocks.md#brokers" in str(info.value)
    assert refusals(tmp_path)[-1]["error"]["type"] == "NotModelled", "logged, so swallowing it still fails the step"
    with pytest.raises(ModuleNotFoundError, match="the mock"):
        importlib.import_module({"alpaca": "alpaca.trading.stream", "ccxt": "ccxt.pro", "ib_async": "ib_async.flexreport"
                                 }[sdk])
    module, name = {"alpaca": ("alpaca.trading.requests", "TrailingStopOrderRequest"), "ccxt": ("ccxt", "bitget"),
                    "ib_async": ("ib_async", "Future")}[sdk]
    with pytest.raises(ImportError):
        exec(f"from {module} import {name}", {})
    assert f"{module}.{name} is not modelled" in refusals(tmp_path)[-1]["error"]["message"]


# ---- testbench.yaml and the expectation -------------------------------------------------------------------

def _step(**step):
    return {"project": "p", "plans": {"p": {"steps": [{"name": "s", "run": "true", **step}]}}}


def test_config_takes_broker_mocks_and_says_what_to_write(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "prices.csv").write_text(PRICES)
    (tmp_path / "bad.csv").write_text("time,symbol,close\n2024-01-02,AAPL,abc\n")
    (tmp_path / "cols.csv").write_text("date,ticker,price\n")
    opts = {"prices": "data/prices.csv", "cash": 100000, "fee_usd": 1, "slippage_bps": 5}
    cfg = C.validate(_step(mocks={"alpaca": opts, "ccxt": None}), tmp_path)
    assert cfg["plans"]["p"]["steps"][0]["mocks"] == {"alpaca": opts, "ccxt": {}}
    yaml_date = C.validate(_step(mocks={"ib_async": {"prices": "data/prices.csv", "now": datetime(2024, 1, 3).date()}}),
                           tmp_path)
    assert yaml_date["plans"]["p"]["steps"][0]["mocks"]["ib_async"]["now"] == "2024-01-03"
    for mocks, why in [(["ib_insync"], "(the mock ib_async also answers `import ib_insync`: list ib_async)"),
                       (["robinhood"], "testbench has mock clients for anthropic, openai, alpaca, ccxt, ib_async"),
                       ({"alpaca": {"prices": "nope.csv"}}, "prices: nope.csv does not exist"),
                       ({"alpaca": {"prices": "bad.csv"}}, "prices (bad.csv): line 2: close is 'abc', not a number"),
                       ({"alpaca": {"prices": "cols.csv"}}, "has no time, symbol, close column"),
                       ({"alpaca": {"slipage_bps": 5}}, "has no 'slipage_bps' (did you mean 'slippage_bps'?)"),
                       ({"alpaca": {"fee_usd": -1}}, "fee_usd is a fixed fee per filled order"),
                       ({"ccxt": {"fee_bps": float("nan")}}, "fee_bps is a fee in basis points"),
                       ({"ccxt": {"allow_live": "yes"}}, "allow_live is true or false"),
                       ({"ccxt": {"price": {"BTC/USDT": 0}}}, "price is a mapping of symbol to a price above 0"),
                       ({"ib_async": {"now": "soon"}}, "now: 'soon' is not a time"),
                       ({"ib_async": {"prices": "data/prices.csv", "now": "2023-12-01"}},
                        "now (2023-12-01T00:00:00Z) is before the first bar of data/prices.csv"),
                       ({"ib_async": {"advance_every": 1.5}}, "advance_every is a whole number of requests")]:
        with pytest.raises(C.ConfigError, match=f"mocks.*{re.escape(why)}"):
            C.validate(_step(mocks=mocks), tmp_path)
    ok = C.validate(_step(expect={"mock_calls": {"alpaca": {"orders": "<= 20", "orders_without_limit": 0,
                                                            "max_order_value": "<= 5000", "live": 0}}}), tmp_path)
    assert ok["plans"]["p"]["steps"][0]["expect"]["mock_calls"]["alpaca"]["orders"] == "<= 20"
    for spec, why in [({"alpaca": {"tool_calls": 1}}, "has no 'tool_calls'; use calls, refused, orders"),
                      ({"openai": {"orders": 1}}, "has no 'orders'; use calls, refused, injected")]:
        with pytest.raises(C.ConfigError, match=f"mock_calls.*{re.escape(why)}"):
            C.validate(_step(expect={"mock_calls": spec}), tmp_path)


def test_broker_expectation_reads_the_log(tmp_path):
    order = {"symbol": "AAPL", "side": "buy", "qty": 10, "stop": None}
    lines = [{"sdk": "alpaca", "endpoint": "TradingClient", "call": 1, "live": False, "error": None},
             {"sdk": "alpaca", "endpoint": "submit_order", "call": 2, "live": False, "error": None,
              "orders": [{**order, "id": "a", "type": "market", "limit": None, "value": 1020.0}],
              "fills": [{"order": "a", "qty": 10, "price": 102.1, "fee": 1.0}]},
             {"sdk": "alpaca", "endpoint": "submit_order", "call": 3, "live": False, "error": None,
              "orders": [{**order, "id": "b", "type": "limit", "limit": 99.0, "value": 990.0}]},
             {"sdk": "alpaca", "endpoint": "cancel_orders", "call": 4, "live": False, "error": None,
              "cancels": [{"id": "b"}]},
             {"sdk": "ccxt", "endpoint": "fetch_balance", "call": 1, "live": True,
              "error": {"kind": "refused", "type": "LiveAccountRefused", "message": "this test asked for a live ..."}}]
    (tmp_path / "mock_calls.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines) + "not json\n")
    rules = {"alpaca": {"orders": "<= 20", "fills": 1, "cancels": 1, "orders_without_limit": 0,
                        "max_order_value": "<= 1000", "fees": "< 2", "live": 0},
             "ccxt": {"orders": 0, "live": 0}}
    found = {c["check"]: c for c in expect.check({"mock_calls": rules}, workdir=tmp_path, result={
        "kind": "run", "exit_code": 0, "step_dir": str(tmp_path), "mocks": {"alpaca": {}, "ccxt": {}}})}
    assert found["mock alpaca orders <= 20"]["ok"] and found["mock alpaca fills 1"]["ok"]
    assert found["mock alpaca cancels 1"]["ok"] and found["mock alpaca fees < 2"]["ok"]
    assert "submit_order x2; cancel_orders x1" in found["mock alpaca orders <= 20"]["detail"]
    unpriced = found["mock alpaca orders_without_limit 0"]
    assert not unpriced["ok"] and unpriced["detail"] == "1 (wanted == 0); call 2: market buy 10 AAPL, value 1,020.00"
    biggest = found["mock alpaca max_order_value <= 1000"]
    assert not biggest["ok"] and biggest["detail"].startswith("1020.0 (wanted <= 1000.0); the largest: call 2")
    assert found["mock alpaca refused 0"]["ok"] and found["mock alpaca live 0"]["ok"]
    live = found["mock ccxt live 0"]
    assert not live["ok"] and "call 1: fetch_balance (refused: LiveAccountRefused)" in live["detail"]
    assert not found["mock ccxt refused 0"]["ok"] and found["mock ccxt orders 0"]["ok"]
    none = expect.check({"mock_calls": {"ib_async": {"orders": 0}}}, workdir=tmp_path / "empty", result={})
    assert none[0]["ok"] and "no calls were logged (is `mocks: [ib_async]` on this step?)" in none[0]["detail"]


# ---- end to end: trading scripts run as plan steps ----------------------------------------------------------

GUARD = """import socket
def _no_network(*a, **k):
    raise AssertionError("a mock broker opened a network connection")
socket.socket.connect = _no_network
"""
ALPACA_BOT = GUARD + """
import json
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
bars = StockHistoricalDataClient("key", "secret").get_stock_bars(
    StockBarsRequest(symbol_or_symbols="AAPL", timeframe=TimeFrame.Day))["AAPL"]
closes = [b.close for b in bars]
trading = TradingClient("key", "secret", paper=True)
if closes[-1] > sum(closes) / len(closes):                  # above its average: buy
    trading.submit_order(MarketOrderRequest(symbol="AAPL", qty=10, side="buy", time_in_force="day"))
trading.submit_order(LimitOrderRequest(symbol="AAPL", qty=5, side="buy", time_in_force="gtc", limit_price=95))
account = trading.get_account()
json.dump({"cash": float(account.cash), "equity": float(account.equity),
           "positions": len(trading.get_all_positions())}, open("alpaca.json", "w"))
"""
CCXT_BOT = GUARD + """
import ccxt
exchange = ccxt.binance({"apiKey": "key", "secret": "secret"})
exchange.set_sandbox_mode(True)
exchange.load_markets()
last = exchange.fetch_ohlcv("ETH/USDT", "1d", limit=3)[-1][4]
exchange.create_order("ETH/USDT", "limit", "buy", 1, last * 0.9)
exchange.create_order("ETH/USDT", "market", "buy", 0.5)
print("balance", exchange.fetch_balance()["total"])
"""
IB_BOT = GUARD + """
import sys
from ib_insync import IB, LimitOrder, MarketOrder, Stock
ib = IB()
ib.connect("127.0.0.1", 7497, clientId=7)
aapl = Stock("AAPL", "SMART", "USD")
ib.qualifyContracts(aapl)
bars = ib.reqHistoricalData(aapl, "", "5 D", "1 day", "TRADES", True)
order = MarketOrder("BUY", 10) if "--market" in sys.argv else LimitOrder("BUY", 10, bars[-1].close)
trade = ib.placeOrder(aapl, order)
ib.sleep(1)
print("status", trade.orderStatus.status, trade.orderStatus.avgFillPrice)
ib.disconnect()
"""
LIVE_BOT = GUARD + """
from alpaca.trading.client import TradingClient
print("cash", TradingClient("key", "secret", paper=False).get_account().cash)
"""
SWALLOWS = GUARD + """
from alpaca.trading.client import TradingClient
try:
    TradingClient("key", "secret").get_portfolio_history()
except AttributeError as e:
    print("ignored:", type(e).__name__)
"""


def test_plans_trade_on_each_broker_and_the_guardrails_fail_steps(tmp_path):
    root = tmp_path / "bot"
    (root / "data").mkdir(parents=True)
    (root / "data" / "prices.csv").write_text(PRICES)
    for name, code in (("alpaca_bot.py", ALPACA_BOT), ("ccxt_bot.py", CCXT_BOT), ("ib_bot.py", IB_BOT),
                       ("live_bot.py", LIVE_BOT), ("swallows.py", SWALLOWS)):
        (root / name).write_text(code)
    prices = {"prices": "data/prices.csv", "fee_usd": 1, "slippage_bps": 5}
    keep_going = {"continue_on_failure": True}
    write_config(root, {"project": "bot", "plans": {
        "paper": {"steps": [
            {"name": "alpaca", "run": "python alpaca_bot.py", "mocks": {"alpaca": prices},
             "expect": {"json": {"alpaca.json": {"positions": 1, "cash": "~= 98948.475 +- 0.001"}},
                        "mock_calls": {"alpaca": {"orders": 2, "fills": 1, "fees": 1, "orders_without_limit": 1,
                                                  "max_order_value": "<= 1100", "live": 0}}}},
            {"name": "ccxt", "run": "python ccxt_bot.py", "mocks": {"ccxt": {**prices, "cash": 5000}},
             "expect": {"text": {"stdout": ["balance {'ETH': 0.5, 'USDT': 3973.4875}"]},
                        "mock_calls": {"ccxt": {"orders": "<= 20", "fills": 1, "live": 0}}}},
            {"name": "ib", "run": "python ib_bot.py", "mocks": {"ib_async": prices},
             "expect": {"text": {"stdout": ["status Filled 105.0"]},
                        "mock_calls": {"ib_async": {"orders": 1, "orders_without_limit": 0}}}}]},
        "guarded": {"steps": [
            {"name": "live", "run": "python live_bot.py", "mocks": {"alpaca": prices}, **keep_going,
             "expect": {"mock_calls": {"alpaca": {"live": 0}}}},
            {"name": "live allowed", "run": "python live_bot.py", "mocks": {"alpaca": {**prices, "allow_live": True}},
             **keep_going, "expect": {"mock_calls": {"alpaca": {"live": 2}}}},
            {"name": "no orders wanted", "run": "python ccxt_bot.py", "mocks": {"ccxt": prices}, **keep_going,
             "expect": {"mock_calls": {"ccxt": {"orders": 0}}}},
            {"name": "limits only", "run": "python ib_bot.py --market", "mocks": {"ib_async": prices}, **keep_going,
             "expect": {"mock_calls": {"ib_async": {"orders_without_limit": 0}}}},
            {"name": "swallowed", "run": "python swallows.py", "mocks": ["alpaca"], **keep_going}]}}})
    r = cli(root, "run", "paper", "--wait", "180")
    assert r.returncode == 0, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    rep = json.loads((run / "report.json").read_text())
    assert [s["status"] for s in rep["steps"]] == ["passed"] * 3
    for n, step in enumerate(("alpaca", "ccxt", "ib"), 1):
        assert (run / "steps" / f"0{n}-{step}" / "mock_calls.jsonl").is_file(), "the log is the step's evidence"

    r = cli(root, "run", "guarded", "--wait", "180")
    assert r.returncode == 1, r.stdout + r.stderr
    run = sorted((root / ".testbench" / "runs").iterdir())[-1]
    rep = json.loads((run / "report.json").read_text())
    status = {s["name"]: s["status"] for s in rep["steps"]}
    assert status == {"live": "failed", "live allowed": "passed", "no orders wanted": "failed",
                      "limits only": "failed", "swallowed": "failed"}
    checks = {(s["name"], c["check"]): c for s in rep["steps"] for c in s["expectations"]}
    live = checks[("live", "mock alpaca refused 0")]
    assert not live["ok"] and f"LiveAccountRefused: mock alpaca: {LIVE_RULE} (TradingClient(paper=False))" in \
        live["detail"]
    assert not checks[("live", "mock alpaca live 0")]["ok"]
    assert checks[("live allowed", "mock alpaca live 2")]["ok"], "a live client let through is still on record"
    assert checks[("no orders wanted", "mock ccxt orders 0")]["detail"].startswith("2 (wanted == 0)")
    assert "call 4: market buy 10 AAPL" in checks[("limits only", "mock ib_async orders_without_limit 0")]["detail"]
    swallowed = checks[("swallowed", "mock alpaca refused 0")]
    assert not swallowed["ok"] and "NotModelled: mock alpaca: TradingClient.get_portfolio_history is not " \
                                   "modelled" in swallowed["detail"]
    assert f"{LIVE_RULE} (TradingClient(paper=False))" in r.stdout


# ---- the mocks must not drift into names the real SDKs lack ------------------------------------------------

MOCK_NAMES = '''
import importlib, inspect, json
mods = {"alpaca": ["alpaca.common", "alpaca.common.exceptions", "alpaca.common.enums", "alpaca.trading",
                   "alpaca.trading.client", "alpaca.trading.requests", "alpaca.trading.enums",
                   "alpaca.trading.models", "alpaca.data", "alpaca.data.historical", "alpaca.data.requests",
                   "alpaca.data.timeframe", "alpaca.data.models", "alpaca.data.enums"],
        "ccxt": ["ccxt", "ccxt.base.errors"], "ib_async": ["ib_async", "ib_async.util"]}[SDK]
out = {m: [n for n in importlib.import_module(m).__all__ if not n.startswith("_")] for m in mods}
if SDK == "alpaca":
    from alpaca.trading.client import TradingClient
    out["methods"] = {n: list(inspect.signature(getattr(TradingClient, n)).parameters) for n in vars(TradingClient)
                      if not n.startswith("_")}
elif SDK == "ccxt":
    import ccxt
    out["methods"] = {n: list(inspect.signature(getattr(ccxt.Exchange, n)).parameters) for n in vars(ccxt.Exchange)
                      if not n.startswith("_") and callable(getattr(ccxt.Exchange, n)) and n not in ("has",)}
else:
    import ib_async
    out["methods"] = {n: list(inspect.signature(getattr(ib_async.IB, n)).parameters) for n in vars(ib_async.IB)
                      if not n.startswith("_") and callable(getattr(ib_async.IB, n))}
print(json.dumps(out))
'''
REAL = {"alpaca": ("alpaca.trading.client", "TradingClient"), "ccxt": ("ccxt", "Exchange"),
        "ib_async": ("ib_async", "IB")}


@pytest.mark.parametrize("sdk", sorted(REAL))
def test_mock_names_exist_in_the_real_sdk(sdk, tmp_path):
    module, cls = REAL[sdk]
    real = pytest.importorskip(module, reason=f"the real {sdk} package is not installed here")
    if getattr(sys.modules[module.split(".")[0]], "TESTBENCH_MOCK", False):
        pytest.skip("the mock is on this interpreter's path")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(PATHS)}
    out = subprocess.run([sys.executable, "-c", MOCK_NAMES.replace("SDK", repr(sdk))], capture_output=True, text=True,
                         env=env, cwd=tmp_path)
    mock = json.loads(out.stdout)
    for name, names in mock.items():
        if name != "methods":
            target = importlib.import_module(name)
            assert [n for n in names if not hasattr(target, n)] == [], f"{name} names the real SDK lacks"
    owner = getattr(real, cls)
    made = real.binance() if sdk == "ccxt" else None        # ccxt puts its camelCase names on a class it makes
    for name, params in mock["methods"].items():
        if made is not None and name != name.lower():
            assert hasattr(made, name), f"ccxt.binance().{name} is not in the real SDK"
            continue
        assert hasattr(owner, name), f"{cls}.{name} is not in the real SDK"
        assert params == list(inspect.signature(getattr(owner, name)).parameters), f"{cls}.{name}'s parameters"


def test_the_mock_packages_are_plain_files_testbench_ships():
    for sdk in M.BROKERS:
        assert (M.CLIENTS / sdk / sdk / "__init__.py").is_file()
    assert (M.CLIENTS / "ib_async" / "ib_insync" / "__init__.py").is_file()
    assert M.broker_core().options_error({"prices": "p.csv", "cash": 1}) is None
    assert "_testbench_mock" not in sys.modules or getattr(sys.modules["_testbench_mock"], "__file__", "").endswith(
        "_testbench_mock.py"), "loading the broker core for testbench.yaml leaves no stand-in behind"
