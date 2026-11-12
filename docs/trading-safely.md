# Trading with agents safely

testbench is not a broker, a signal service or investment advice. It never picks a strategy and never says one will
make money. It checks the claims a backtest makes, and agent-testbench 0.3 does backtests and paper trading only:
nothing in it sends a live order.

An agent that writes or tunes a trading strategy is fast and tireless, which is the danger: it will find every way a
backtest can flatter a strategy - no fees, tomorrow's prices, a lucky stretch, the hundredth variant of an idea - and
report the number that went up. The ladder below makes each of those fail, in code, before anyone trusts the result.

```bash
testbench demo trading       # a strategy with a planted look-ahead bug: caught, fixed, then up the ladder (seconds)
testbench init --recipe trading-backtest      # the ladder for your backtest (or vectorbt-strategy, freqtrade-strategy)
testbench init --add --recipe alpaca-paper    # the paper rung: a rehearsal on a mock broker, then Alpaca's paper API
```

## The ladder

Each rung is a plan. Climb in order; a rung that fails is the answer, not something to tune away.

| rung | what it proves | what it does not prove |
|---|---|---|
| **1. Backtest with costs** (`strategy:`) | testbench's own numbers, from the trades and equity the backtest wrote, clear the bar you set: fees or slippage charged, enough round trips to mean something, better than buy and hold | that the strategy was not tuned to this very history |
| **2. Look-ahead** (`lookahead:`) | no decision changes when the rows after it are cut away: the strategy did not use tomorrow's data today | anything about returns |
| **3. Walk-forward** (`walk_forward:`) | every window holds up on its own, each seeing only the rows before its end - not just the whole history on average | that the next window will |
| **4. Holdout, once** (`holdout: {max_opens: 1}`) | the strategy on sealed rows that no other plan ever saw, by rules written before they were opened | much, from one stretch; and nothing at all once it has been opened and tuned against |
| **5. Paper** (`mocks: alpaca`, then Alpaca's paper API) | the order code works against a broker's API - the right orders, sizes within bounds, no live account - and, day by day, a record on days that had not happened when the strategy was written | that the edge will last; paper fills are kinder than real ones |

[Trading](trading.md) has the files and the numbers, [Sealed data](trading-data.md) the seal, the look-ahead check
and walk-forward, [Mock clients](mocks.md#brokers) the brokers, [Recipes](recipes.md#trading-strategies) the
ladders to start from.

## What testbench enforces in code

- **The numbers are testbench's.** The backtest writes its trades and equity in [standard files](trading.md#the-files);
  testbench computes Sharpe, drawdown, round trips and the benchmark from them, the same way for every framework. A
  file from an earlier run is refused, not read.
- **Costs.** `strategy:` fails a backtest whose fills paid no fee and no slippage, unless you write `costs: none` -
  and then the report says so. Fewer than 20 round trips fail too (`min_trades`), and `beats: buy_and_hold` never
  passes for want of a benchmark.
- **The seal.** Every plan but the holdout's sees the data cut before `holdout_from`. Each opening is counted in
  `.testbench/holdout.jsonl`, every report says how many came before it, and `max_opens` refuses one more at launch.
- **Look-ahead.** The steps rerun on the data cut at earlier times; a decision dated before a cut that changes fails
  the plan, with the first one in words.
- **Luck.** A loop over a strategy keeps every try's returns, reverted tries included, and each report gives the
  best try's [deflated Sharpe ratio](trading-stats.md#deflated-sharpe-ratio-dsr) over all of them: "best of 12
  tries: deflated Sharpe 0.41 ...; not significant at 0.95 - the improvement may be luck". `--min-deflated-sharpe`
  makes a loop's target count only when the best try clears it.
- **Brokers.** The mock `alpaca`, `ccxt` and `ib_async` packages fill orders on a paper account from a prices file
  and refuse a live one (`TradingClient(paper=False)`, an exchange without sandbox mode, IB's live ports), and that
  refusal fails the step even when the code catches it. `mock_calls:` bounds the orders, their size and `live: 0`.
- **Money.** Every run is priced before it starts; a person approves above the threshold, and the machine's limits
  hold whatever was approved. Keys go by name (`secrets:`), never into testbench.yaml.
- **Fingerprints.** Every report records the sha256 of the data its steps saw; `testbench verify` says when the data
  changed since.

## What it cannot

- **A strategy set on cheating, on the local backend.** The run's copy of the data is cut, but a step runs as you and
  can still open the project's own file by its absolute path. The seal stops accidents there, not intent. For an
  agent's code you do not trust, run the plans on `docker` with `network: none`, and keep the holdout plan's launch
  and its `max_opens` with a person.
- **Tries it never saw.** The luck line counts the loop's tries. Variants run by hand, in another loop or in another
  tool make the true count higher and the deflated Sharpe lower than it says.
- **Regime change.** Every rung looks at the past. A market that changes its behaviour - rates, liquidity, who else
  trades the same idea - can undo a strategy that passed every one.
- **The future.** A paper record shows days as they arrive; no rung proves an edge in advance. Real fills, slippage
  in a fast market, borrowing costs, outages and your own mistakes are not in a backtest.
- **Your data.** Bad prices, survivorship bias (a list of today's stocks back-tested over the past) and a model that
  already read about the period (`model_cutoff:` only flags the dates you give it) are yours to get right.

## Working with an agent

- **Give it the ladder, not a target return.** The MCP prompt `check-a-strategy` (a slash command in clients that show
  prompts) tells an agent to climb rung by rung, report each run id, and never weaken a check to pass. The `iterate`
  skill says the same in Claude Code.
- **Let it tune on the lower rungs only.** A loop on the backtest (`testbench loop start backtest --metric
  check:sharpe --budget-usd 0`) is fine; the holdout is opened once, when a person says so.
- **Ask for the luck line.** A best try the deflated Sharpe calls luck is not an edge, however good its backtest.
- **Never let it approve money.** `--approve` is a person's answer; the Claude Code hook turns an agent's `--approve`
  into a question for you. Live trading is not something testbench offers, and an agent should never point a bot at a
  live account: paper keys only, by name.
- **Read the caveats it reports.** Few trades, a short history, one market regime, positions still open: the report
  lists them, and "Past results, on past data. Not financial advice." is on every tear sheet for a reason.
