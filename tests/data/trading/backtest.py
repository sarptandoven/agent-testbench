"""A deterministic backtest for the trading pack's checks (tests/test_numbers_check.py, tests/test_strategy_check.py):
it writes equity.csv, trades.csv and, when asked, prices.csv into OUT with the numbers a test wants, and nothing else.
Standard library only, no randomness: the same arguments give the same files, byte for byte.

    python backtest.py OUT [--bars 504] [--trips 40] [--fee 1] [--slippage S] [--drift 0.001] [--crash 0]
                           [--bench column|prices|none] [--bench-drift 0.0003] [--no-trades] [--crypto]

The equity's daily return is drift + 0.01 * sin(1.7 * i), so drift 0.001 gives a Sharpe near 2.2 and 0.0003 near 0.7
(on 252 bars a year); --crash 0.3 takes 30% off at the middle bar (a max drawdown a little past -0.3). --trips round
trips of 10 SPY each, evenly spaced, each fill paying --fee (and recording --slippage, when given)."""
import argparse
import datetime as dt
import math
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("out")
p.add_argument("--bars", type=int, default=504)
p.add_argument("--trips", type=int, default=40)
p.add_argument("--fee", type=float, default=1.0)
p.add_argument("--slippage", type=float)
p.add_argument("--drift", type=float, default=0.001)
p.add_argument("--crash", type=float, default=0.0)
p.add_argument("--bench", choices=("column", "prices", "none"), default="column")
p.add_argument("--bench-drift", type=float, default=0.0003)
p.add_argument("--no-trades", action="store_true")
p.add_argument("--crypto", action="store_true", help="a bar every day, weekends too")
a = p.parse_args()

out = Path(a.out)
out.mkdir(parents=True, exist_ok=True)
day, times = dt.date(2022, 1, 3), []                     # a Monday
while len(times) < a.bars:
    if a.crypto or day.weekday() < 5:
        times.append(day)
    day += dt.timedelta(days=1)

equity, price, rows, closes = 100000.0, 100.0, [], []
for i, t in enumerate(times):
    if i:
        equity *= 1 + a.drift + 0.01 * math.sin(1.7 * i)
        price *= 1 + a.bench_drift + 0.008 * math.sin(2.3 * i)
    if a.crash and i == a.bars // 2:
        equity *= 1 - a.crash
    rows.append((t, equity, price))
    closes.append(price)

header = "time,equity" + (",benchmark" if a.bench == "column" else "")
lines = [f"{t},{e:.6f}" + (f",{c:.6f}" if a.bench == "column" else "") for t, e, c in rows]
(out / "equity.csv").write_text(header + "\n" + "\n".join(lines) + "\n")
if a.bench == "prices":
    (out / "prices.csv").write_text("time,symbol,close\n" + "".join(f"{t},SPY,{c:.6f}\n" for t, _, c in rows))
if not a.no_trades:
    fills, gap = [], max(2, (a.bars - 1) // max(a.trips, 1))
    for k in range(a.trips):
        enter, leave = k * gap, min(k * gap + gap // 2, a.bars - 1)
        if leave >= a.bars or enter >= leave:
            break
        for side, j in (("buy", enter), ("sell", leave)):
            fills.append(f"{times[j]},SPY,{side},10,{closes[j]:.6f},{a.fee:g}"
                         + ("" if a.slippage is None else f",{a.slippage:g}"))
    head = "time,symbol,side,quantity,price,fee" + ("" if a.slippage is None else ",slippage")
    (out / "trades.csv").write_text(head + "\n" + "".join(f + "\n" for f in fills))
print(f"wrote {out}/equity.csv ({a.bars} bars)" + ("" if a.no_trades else f", trades.csv ({a.trips} round trips)"))
