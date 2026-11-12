"""A trend follower: hold DEMO while its close is above the average of its last 20 closes, stay out while it is below.
It decides at each day's close and trades at that close, paying 0.05% of every fill in fees and slippage.

It reads the data testbench hands this step: TESTBENCH_DATA_PRICES holds only the rows the plan may see - those before
the sealed holdout, and in a walk-forward window those before the window's test end. It trades from
TESTBENCH_TEST_START (a walk-forward window's test period) or TRADE_FROM (the holdout plan), with the rows before for
its average, and writes testbench's standard files: out/equity.csv and out/trades.csv, which testbench grades - this
script never computes its own Sharpe ratio - and out/signals.csv, what it decided each day, which the lookahead plan
compares across runs on data cut earlier. paper.py places the same decisions as orders. Standard library only."""
import csv
import os
from pathlib import Path

N, FEE = 20, 0.0005          # the average's days; fees and slippage, a share of what each fill trades
CASH = 10_000.0


def decide(closes):
    """The decision at a close, from the closes up to it: how far the last close is above the average of the last N
    (the signal), and the position to hold until the next close - 1, all in, or 0, out."""
    signal = closes[-1] / (sum(closes[-N:]) / N) - 1
    return signal, int(signal > 0)


def backtest(rows, start=""):
    closes = [float(r["close"]) for r in rows]
    cash, units, bars, fills, signals = CASH, 0.0, [], [], []
    for i, row in enumerate(rows):
        if i < N or row["time"] < start:
            continue
        signal, position = decide(closes[: i + 2])       # the closes up to today's
        price = closes[i]                                # today's close: where a trade fills
        if position != (units > 0):
            quantity = cash / (price * (1 + FEE)) if position else units
            fee = quantity * price * FEE
            cash += -quantity * price - fee if position else quantity * price - fee
            units = quantity if position else 0.0
            fills.append([row["time"], row["symbol"], "buy" if position else "sell", quantity, price, fee])
        bars.append([row["time"], cash + units * price, cash, units * price, price])
        signals.append([row["time"], row["symbol"], round(signal, 6), position])
    return bars, fills, signals


def write(path, header, rows):
    with open(path, "w", newline="") as f:
        csv.writer(f).writerows([header, *rows])


if __name__ == "__main__":
    rows = list(csv.DictReader(open(os.environ.get("TESTBENCH_DATA_PRICES", "data/prices.csv"))))
    bars, fills, signals = backtest(rows, os.environ.get("TESTBENCH_TEST_START") or os.environ.get("TRADE_FROM") or "")
    Path("out").mkdir(exist_ok=True)
    write("out/equity.csv", ["time", "equity", "cash", "position_value", "benchmark"], bars)
    write("out/trades.csv", ["time", "symbol", "side", "quantity", "price", "fee"], fills)
    write("out/signals.csv", ["time", "symbol", "signal", "position"], signals)
    print(f"{len(bars)} days from {bars[0][0]} to {bars[-1][0]}, {len(fills)} fills, equity {bars[-1][1]:,.2f} "
          "(out/equity.csv, out/trades.csv; testbench computes the numbers)")
