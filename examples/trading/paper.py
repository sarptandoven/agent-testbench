"""The paper rung: strategy.py's daily decision as orders on an Alpaca paper account.

Each trading day it reads DEMO's daily closes up to today's from the broker, decides as the backtest does (decide() in
strategy.py), and buys or sells at once so the account holds what the strategy decided: all in, in whole shares, or
out. It writes the paper record in testbench's standard files: out/paper/equity.csv (the account at each day's end)
and out/paper/trades.csv (every fill, with the fee the account was charged).

    python paper.py              one decision, today: run it once a day after the close against Alpaca's paper API,
                                 with ALPACA_API_KEY and ALPACA_SECRET_KEY set (paper=True, always)
    python paper.py --days 65    65 days in a row, waiting for each new bar: a rehearsal against testbench's mock
                                 broker, whose clock moves one bar every few requests

In this project's `paper` plan it runs against testbench's mock alpaca package (`mocks: alpaca`): a paper account kept
by testbench, prices from data/prices.csv, nothing sent anywhere, and a live account refused."""
import argparse
import csv
import os
import time
from datetime import timedelta
from pathlib import Path

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from strategy import N, decide

SYMBOL = "DEMO"
key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
trading = TradingClient(key, secret, paper=True)                 # a paper account: never paper=False
market = StockHistoricalDataClient(key, secret)


def next_day(after):
    """The broker's time once its date is past `after`'s (at once for the first day); None when it does not move on."""
    for _ in range(20):
        now = trading.get_clock().timestamp
        if after is None or now.date() > after.date():
            return now
    return None


def held():
    return next((float(p.qty) for p in trading.get_all_positions() if p.symbol == SYMBOL), 0.0)


def trade(position, close, cash):
    """Buy or sell at once so the account holds `position` (1: all in, 0: out); the fill, or None."""
    shares = held()
    if position and not shares:
        order = MarketOrderRequest(symbol=SYMBOL, qty=int(cash * 0.99 / close), side=OrderSide.BUY,
                                   time_in_force=TimeInForce.DAY)
    elif shares and not position:
        order = MarketOrderRequest(symbol=SYMBOL, qty=shares, side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
    else:
        return None
    if not order.qty:
        return None
    placed = trading.submit_order(order)
    for _ in range(30):                     # the mock fills a market order at once; Alpaca in seconds, when open
        if placed.filled_avg_price is not None:
            return {"side": order.side.value, "quantity": float(placed.filled_qty),
                    "price": float(placed.filled_avg_price)}
        time.sleep(1)
        placed = trading.get_order_by_id(placed.id)
    print(f"order {placed.id} is not filled yet (is the market closed?): Alpaca fills it when it opens")
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="strategy.py's daily decision as orders on an Alpaca paper account")
    parser.add_argument("--days", type=int, default=1, help="trading days in a row (default 1: today)")
    days = parser.parse_args().days
    print(f"paper account {trading.get_account().account_number}")        # Alpaca numbers paper accounts PA...
    Path("out/paper").mkdir(parents=True, exist_ok=True)
    equity, fills, now = [], [], None
    for _ in range(days):
        now = next_day(now)
        if now is None:
            print("The broker's clock did not move on to a new day: against a real account, run paper.py once a day")
            break
        bars = market.get_stock_bars(StockBarsRequest(symbol_or_symbols=SYMBOL, timeframe=TimeFrame.Day,
                                                      start=now - timedelta(days=3 * N), end=now))[SYMBOL]
        closes = [float(b.close) for b in bars]
        signal, position = decide(closes)
        before = float(trading.get_account().cash)
        fill = trade(position, closes[-1], before)
        account = trading.get_account()
        cash, total = float(account.cash), float(account.equity)
        day = bars[-1].timestamp.date().isoformat()
        if fill:
            paid = fill["quantity"] * fill["price"]
            fee = before - cash - paid if fill["side"] == "buy" else paid - (cash - before)
            fills.append([day, SYMBOL, fill["side"], fill["quantity"], fill["price"], round(max(fee, 0.0), 6)])
        equity.append([day, total, cash, total - cash, closes[-1]])
        print(f"{day}: signal {signal:+.4f}, hold {position}" + (f"; {fill['side']} {fill['quantity']:g} at "
                                                                 f"{fill['price']:.2f}" if fill else "")
              + f"; equity {total:,.2f}")
    for name, header, rows in (("equity", ["time", "equity", "cash", "position_value", "benchmark"], equity),
                               ("trades", ["time", "symbol", "side", "quantity", "price", "fee"], fills)):
        with open(f"out/paper/{name}.csv", "w", newline="") as f:
            csv.writer(f).writerows([header, *rows])
