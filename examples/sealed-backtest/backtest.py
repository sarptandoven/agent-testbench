"""A moving-average crossover, on the data testbench hands this step: TESTBENCH_DATA_PRICES holds only the rows the plan
may see - those before the sealed holdout, and in a walk-forward window those before the window's test end. It trades
from TESTBENCH_TEST_START (a walk-forward window's test period) or TRADE_FROM, with the rows before for its averages,
and writes out/equity.csv and out/trades.csv; the plan's metrics step grades them, not this script. out/signals.csv has
what it decided each day (1: hold, 0: stay out), which the lookahead plan compares across runs on data cut earlier."""
import csv
import os

from agent_testbench.trading.files import write_equity, write_trades

FAST, SLOW, FEE = 10, 40, 0.0005                     # days, days, 0.05% of each fill

rows = list(csv.DictReader(open(os.environ.get("TESTBENCH_DATA_PRICES", "data/prices.csv"))))
start = os.environ.get("TESTBENCH_TEST_START") or os.environ.get("TRADE_FROM") or ""
closes = [float(r["close"]) for r in rows]
cash, units, bars, fills, signals = 10_000.0, 0.0, [], [], ["time,symbol,signal"]
for i, row in enumerate(rows):
    if i < SLOW or row["time"] < start:
        continue
    fast, slow = sum(closes[i - FAST:i]) / FAST, sum(closes[i - SLOW:i]) / SLOW    # known before today's close
    signals.append(f"{row['time']},{row['symbol']},{int(fast > slow)}")
    price = closes[i]
    if (fast > slow) != (units > 0):                 # the signal changed: trade at today's close
        side = "buy" if fast > slow else "sell"
        quantity = cash / (price * (1 + FEE)) if side == "buy" else units
        fee = quantity * price * FEE
        cash += -quantity * price - fee if side == "buy" else quantity * price - fee
        units = quantity if side == "buy" else 0.0
        fills.append({"time": row["time"], "symbol": row["symbol"], "side": side, "quantity": quantity,
                      "price": price, "fee": fee})
    bars.append({"time": row["time"], "equity": cash + units * price, "position_value": units * price,
                 "benchmark": price})
write_trades(fills, "out/trades.csv")
write_equity(bars, "out/equity.csv")
with open("out/signals.csv", "w") as f:
    f.write("\n".join(signals) + "\n")
print(f"{len(bars)} bars from {bars[0]['time']} to {bars[-1]['time']}, {len(fills)} fills, "
      f"equity {bars[-1]['equity']:.2f}")
