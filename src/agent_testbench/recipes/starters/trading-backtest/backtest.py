"""A starter backtest, written by `testbench init --recipe trading-backtest`: put your strategy in decide().

It holds one symbol while its close is above the average of its last 20 closes and stays in cash while it is below.
It decides at each close from the closes up to that one - never a later row, so the lookahead plan finds nothing -
and fills at that close, paying FEE of every fill's value in fees and slippage.

It reads the prices testbench hands the step: TESTBENCH_DATA_PRICES, a CSV file with time (ISO dates or times),
symbol and close, holding only the rows the plan may see (those before the sealed holdout; in a walk-forward window,
those before the window's end). It trades from TESTBENCH_TEST_START (a walk-forward window's test period) or TRADE_FROM
(the holdout plan) when set, with the rows before to warm up. SYMBOL picks the symbol when the file has several.

It writes testbench's standard files (docs/trading.md#the-files), and testbench computes every number from them - this
script grades nothing itself:
  out/equity.csv   time, equity, cash, position_value, benchmark (buy and hold of the same symbol)
  out/trades.csv   time, symbol, side, quantity, price, fee: one row per fill
  out/signals.csv  time, symbol, signal, position: what it decided at each close (the lookahead plan compares them)
`python -m agent_testbench.trading.files --check out` says whether the files read. Standard library only.
"""
import csv
import os
from pathlib import Path

N = 20                  # closes in the average
FEE = 0.0005            # fees and slippage: this share of every fill's value
CASH = 10_000.0         # the account at the start


def decide(closes):
    """The decision at a close, from the closes up to and including it: the signal - how far the last close is above
    the average of the last N - and the position to hold until the next close, 1 (all in) or 0 (out). Write the
    signal's number to signals.csv, not only the position: a number changes on every peek ahead."""
    signal = closes[-1] / (sum(closes[-N:]) / N) - 1
    return signal, int(signal > 0)


def backtest(rows, start=""):
    """Equity per close, fills and decisions, trading from `start` (an ISO time; '' for every row)."""
    closes, cash, units = [], CASH, 0.0
    bars, fills, signals = [], [], []
    for row in rows:
        price = float(row["close"])
        closes.append(price)                            # the closes up to this row's, and no further
        if len(closes) < N or row["time"] < start:
            continue
        signal, position = decide(closes)
        if position != (units > 0):
            quantity = cash / (price * (1 + FEE)) if position else units
            fee = quantity * price * FEE
            cash += -quantity * price - fee if position else quantity * price - fee
            units = quantity if position else 0.0
            fills.append([row["time"], row["symbol"], "buy" if position else "sell", quantity, price, fee])
        bars.append([row["time"], cash + units * price, cash, units * price, price])
        signals.append([row["time"], row["symbol"], round(signal, 6), position])
    return bars, fills, signals


def read_prices(path, symbol=""):
    """The rows of one symbol (the file's first, unless `symbol`), in time order."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f)]
    missing = {"time", "symbol", "close"} - set(rows[0] if rows else {})
    if not rows or missing:
        raise SystemExit(f"{path}: {'no rows' if not rows else 'no ' + ', '.join(sorted(missing)) + ' column'}; the "
                         "prices file has a time column (ISO dates or times), symbol and close")
    symbol = symbol or rows[0]["symbol"]
    mine = sorted((r for r in rows if r["symbol"] == symbol), key=lambda r: r["time"])
    if not mine:
        raise SystemExit(f"{path} has no rows of {symbol}; its symbols: {', '.join(sorted({r['symbol'] for r in rows}))}")
    return symbol, mine


def write(path, header, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header, *rows])


if __name__ == "__main__":
    prices = os.environ.get("TESTBENCH_DATA_PRICES") or "data/prices.csv"
    symbol, rows = read_prices(prices, os.environ.get("SYMBOL", ""))
    bars, fills, signals = backtest(rows, os.environ.get("TESTBENCH_TEST_START") or os.environ.get("TRADE_FROM") or "")
    if not bars:
        raise SystemExit(f"{prices}: {len(rows)} closes of {symbol}, and nothing to trade: it needs {N} to start, and "
                         "rows on or after the day it trades from")
    Path("out").mkdir(exist_ok=True)
    write("out/equity.csv", ["time", "equity", "cash", "position_value", "benchmark"], bars)
    write("out/trades.csv", ["time", "symbol", "side", "quantity", "price", "fee"], fills)
    write("out/signals.csv", ["time", "symbol", "signal", "position"], signals)
    print(f"{symbol}: {len(bars)} closes from {bars[0][0]} to {bars[-1][0]}, {len(fills)} fills, equity "
          f"{bars[-1][1]:,.2f} (out/equity.csv, out/trades.csv, out/signals.csv; testbench computes the numbers)")
