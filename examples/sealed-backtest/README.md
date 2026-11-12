# sealed-backtest

A moving-average crossover on made-up daily closes (`data/prices.csv`, 2020 to September 2025; `make_prices.py`
writes it), with the last quarter sealed. A strategy an agent found is a claim; this project turns it into a proven
claim or a refused one ([docs/trading-data.md](../../docs/trading-data.md)).

- `backtest` runs the strategy on every row before the seal and grades it with testbench's own metrics (the step
  writes `out/equity.csv` and `out/trades.csv`; `agent_testbench.trading.metrics` computes the numbers). Its step
  never sees a row from July 2025 on: it runs in a copy of the project whose `data/prices.csv` stops at 2025-06-30.
- `walk-forward` runs the same steps once per window - 24 months of history, then the 6 months it trades - seven
  windows from 2022 to mid-2025, each with only the rows before its test end. It passes only if every window does.
- `lookahead` runs the backtest once, then four times more on the data cut at earlier dates, and compares what it
  decided each day (`out/signals.csv`): a decision that changes when later rows are cut away was made with
  tomorrow's data, and fails the plan with the first such day. This strategy only averages earlier closes, so it
  passes; make it read `closes[i + 1]` and it fails.
- `holdout` opens the sealed quarter: once. Its report says how many times the holdout was opened before, and
  `holdout: {max_opens: 1}` refuses a second opening at launch (exit 2, nothing spent).

```bash
testbench run backtest          # tune here, as often as you like
testbench run walk-forward      # does it hold up, window after window?
testbench run lookahead         # does it use tomorrow's data today?
testbench run holdout           # the last word, once
testbench verify                # the evidence, and whether data/prices.csv is still the data the run read
```

Every report has a Data section: the rows the steps saw, their sha256, and the sealed part's. Ask your agent to
improve the strategy's Sharpe on `backtest` without looking at the holdout, then to run `holdout` once and say
whether the improvement held.
