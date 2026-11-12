# trading

A trend follower - hold while the close is above its 20-day average, stay out while it is below - on made-up daily
closes (`data/prices.csv`, 2020 to September 2025; `make_prices.py` writes it), with the last quarter sealed and a
look-ahead bug planted in `strategy.py`. Its backtest looks superb until testbench checks it. Free, offline, seconds.

```bash
testbench demo trading          # copies this folder and runs `lookahead`: it fails, on purpose
```

testbench is not a broker, a signal service or investment advice, and it never picks a strategy. It checks the claims
a backtest makes, rung by rung, and computes every number itself from the files `strategy.py` writes
(`out/equity.csv`, `out/trades.csv`): the strategy never grades itself.
[docs/trading-safely.md](../../docs/trading-safely.md) says what each rung proves and what none of them can.

## The bug

```
**FAIL** - look-ahead: DEMO at 2022-03-10 (signal) was `-0.026018` with data up to 2025-06-30 but was `-0.038945`
with data before 2022-03-11 - the strategy used rows after 2022-03-10 to decide it
```

The backtest in the same run passes everything - Sharpe 3.85, a 97% win rate, +1,496% against buy and hold's +154% -
which is the tell. Line 31 of `strategy.py` hands `decide()` the closes up to tomorrow's:

```python
        signal, position = decide(closes[: i + 2])       # the closes up to today's
```

`closes[: i + 2]` stops after row `i + 1`, tomorrow; the closes up to today's are `closes[: i + 1]`. In pandas the same
mistake is a `shift(-1)`, a `rolling(..., center=True)`, or a threshold computed over the whole history. Change that
one line, and the honest strategy is still better than buy and hold here (Sharpe 1.38, +173% against +154%) - on these
made-up prices, whose trends were built in for it to find. On real prices, after costs, that is much harder.

## The ladder

Each rung is a plan; a rung that fails is the answer, not something to tune away.

| rung | command | it proves | it does not prove |
|---|---|---|---|
| backtest | `testbench run backtest` | on every row before the seal, with fees charged: Sharpe at least 1, a drawdown no deeper than 25%, 30+ round trips, a better total return than buy and hold | that it was not tuned to this history |
| look-ahead | `testbench run lookahead` | no decision changes when the rows after it are cut away: 4 reruns on data cut earlier, each decision in `out/signals.csv` compared | anything about returns |
| walk-forward | `testbench run walk-forward` | every half-year holds up on its own (no loss over 10%, no drawdown deeper than 15%), with only the rows before each window's end | that the next half-year will |
| holdout | `testbench run holdout` | the same strategy on the sealed quarter, which no other plan ever saw, by rules written before it opens. Once: a second opening is refused (exit 2, nothing runs) | much: one quarter is a small sample; it is evidence of no gross overfitting |
| paper | `testbench run paper` | a rehearsal: `paper.py` makes the same decisions day by day as orders on a mock Alpaca paper account (`mocks: alpaca`), through the quarter before the seal; at most 20 orders, none over $10,000, no live account, and its record read like a backtest's | an edge: these are past days. A real paper record is days that have not happened yet, on Alpaca's paper API (`testbench recipes alpaca-paper`) |

Every report says which rows its steps saw (sha256 of the data, and of the sealed part), and `testbench verify` checks
that the data is still the data the run read. `testbench report RUN --tearsheet` draws a run's numbers as one page.

## With an agent

Ask: "the lookahead plan fails: find the look-ahead in strategy.py, fix it, and take the strategy up the ladder -
paper only, never live money". The MCP prompt `check-a-strategy` gives an agent the same rules. If it tries
variants, use `testbench loop start backtest --metric check:sharpe --budget-usd 0`: every report then says whether
the best try is better than luck (the deflated Sharpe over every try, reverted ones included), and the holdout stays
a person's call.
