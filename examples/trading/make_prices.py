"""Writes data/prices.csv: made-up daily closes for one symbol, DEMO, every weekday from 2020 to September 2025.
A random walk whose trend may turn at the start of a month - up, down or flat until it turns again - so a
strategy that follows trends has something to find (real prices are harder). Run it again to get the same
file."""
import datetime as dt
import random
from pathlib import Path

rng, close, drift, month = random.Random(6), 100.0, 0.0010, None
day, rows = dt.date(2020, 1, 1), ["time,symbol,close"]
while day < dt.date(2025, 10, 1):
    if day.weekday() < 5:
        if day.month != month and rng.random() < 0.35:
            drift = rng.choices([0.0010, -0.0015, 0.0], [2, 1, 1])[0]
        month = day.month
        close *= 1 + drift + rng.gauss(0, 0.011)
        rows.append(f"{day.isoformat()},DEMO,{close:.2f}")
    day += dt.timedelta(days=1)
Path(__file__).with_name("data").joinpath("prices.csv").write_text("\n".join(rows) + "\n")
