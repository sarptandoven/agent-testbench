"""Writes data/prices.csv: made-up daily closes for one symbol, every weekday from 2020 to September 2025 (a random walk
whose drift may change at the start of a month). Run it again to get the same file."""
import datetime as dt
import random
from pathlib import Path

rng, close, drift, month = random.Random(7), 100.0, 0.0006, None
day, rows = dt.date(2020, 1, 1), ["time,symbol,close"]
while day < dt.date(2025, 10, 1):
    if day.weekday() < 5:
        if day.month != month and rng.random() < 0.4:
            drift = rng.choice([0.0012, 0.0004, -0.0008])
        month = day.month
        close *= 1 + drift + rng.gauss(0, 0.011)
        rows.append(f"{day.isoformat()},DEMO,{close:.2f}")
    day += dt.timedelta(days=1)
Path(__file__).with_name("data").joinpath("prices.csv").write_text("\n".join(rows) + "\n")
