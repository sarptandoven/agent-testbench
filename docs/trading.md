# Trading: a strategy never grades itself

A backtest that prints "Sharpe 3.1" has graded its own homework: the same code that made the trades chose how to
count them - which returns, which year, whether fees were in, what a trade is. An agent that tunes a strategy until
that number goes up will find every way the number can be wrong.

The trading pack turns that around. Whatever framework ran the backtest - vectorbt, backtrader, NautilusTrader,
LEAN, freqtrade or plain pandas - it writes a few standard files, and testbench computes the numbers from them, the
same way for every framework and every run:

```bash
python -m agent_testbench.trading.metrics out/equity.csv out/trades.csv --benchmark out/prices.csv
```

```text
{
  "total_return": 0.2773,
  "sharpe": 0.966,
  "max_drawdown": -0.0902,
  "trades": 9,
  "win_rate": 0.444,
  "costs_modeled": true,
  "benchmark_return": 0.968,
  "excess_return": -0.6907,
  ...
  "notes": ["periods per year: 252 - daily bars; no rows fall on a weekend, so a stock market's 252 trading days a year; ...",
            "only 9 round trips: win_rate, profit_factor and the average trade say little with fewer than 30"]
}
```

The files and the numbers need nothing but the Python standard library: no numpy, no pandas (pandas DataFrames are
accepted when pandas is there).

New to it? [Trading with agents safely](trading-safely.md) has the ladder a strategy climbs - backtest with costs,
look-ahead, walk-forward, the holdout once, paper - what each rung proves and what none can, and `testbench demo
trading` runs it in seconds.

## Checking a strategy

The backtest step writes the standard files, and its `strategy:` expectation reads them: testbench computes the
numbers, checks your rules on them, and adds guardrails a good-looking number needs before it means anything.

```yaml
# testbench.yaml
project: momentum
plans:
  backtest:
    backend: local
    steps:
      - name: backtest
        run: python backtest.py               # writes out/equity.csv, out/trades.csv and out/prices.csv
        max_minutes: 10
        expect:
          strategy:
            equity: out/equity.csv            # the default; trades.csv and prices.csv are read next to it
            options: {calendar: stocks}       # risk_free, periods_per_year, calendar, symbol: as in compute()
            sharpe: ">= 1"                    # a rule on any of the numbers below
            max_drawdown: ">= -0.25"
            beats: buy_and_hold               # the total return beats buy and hold over the same bars
            min_trades: 30                    # default 20; 0 turns this guardrail off
            costs: required                   # the default; `none` allows a backtest that charged nothing
```

`testbench run backtest` then reports, for a strategy that misses:

```
- expected strategy out/equity.csv: sharpe >= 1: 0.842 (wanted >= 1.0); daily bars, 252 a year; 3 years
- expected strategy out/equity.csv: beats buy and hold: -0.0412 (wanted > 0.0); excess_return: the total return 0.3127 against buy and hold's 0.3539 (closes in out/prices.csv); daily bars, 252 a year; 3 years
- expected strategy out/equity.csv: trades >= 30 (min_trades): 12 (wanted >= 30.0); round trips in out/trades.csv, from 24 fills; a number from this few trades means little: backtest over a longer span or more symbols, or set min_trades knowingly (0 turns this off)
- expected strategy out/equity.csv: costs modelled: false: every fee and slippage in out/trades.csv is 0: the backtest charged no trading costs, so its returns are better than an account would get. Charge fees or slippage in the backtest (trades.csv's fee and slippage columns), or write `costs: none` to allow zero costs - the report then says costs were not modelled
```

Each finding is named `strategy EQUITY: ...`, and a measured number comes first, so history, run-to-run
comparisons and `testbench loop --metric check:sharpe` read it back. What `strategy:` takes:

| key | | |
|---|---|---|
| `equity` | default `out/equity.csv` | the equity file; trades.csv and prices.csv are read from the same folder when they are there. `strategy: out/run/equity.csv` is short for `{equity: out/run/equity.csv}` |
| `options` | | the trading reader's: `risk_free`, `periods_per_year`, `calendar`, `symbol` ([below](#the-numbers)) |
| any key of [the numbers](#the-numbers) | | a rule, as every expectation writes one: `">= 1"`, `"~= 0.12 +- 0.02"`, `"between -0.3 and 0"`, `true` |
| `beats` | | `buy_and_hold`: `excess_return` > 0 against the benchmark column of equity.csv, or prices.csv's closes |
| `min_trades` | default 20 | the fewest round trips the numbers may come from; 0 turns it off |
| `costs` | default `required` | `required`: some fee or slippage in trades.csv is above 0. `none`: zero costs pass |

The guardrails, each a finding of its own:

- **Costs.** A backtest that charged no fee and no slippage on any fill fails `costs modelled` (and so does one with
  no trades.csv, where costs cannot be seen). `costs: none` lets it pass, and puts a line on the report that says
  so: `strategy out/equity.csv: costs not modelled (costs: none) - warning: every fee and slippage in
  out/trades.csv is 0: ...`.
- **Too few trades.** Fewer round trips than `min_trades` (20 unless set) fails `trades >= 20 (min_trades)`: a win
  rate or Sharpe from a dozen trades is mostly luck. No trades.csv fails it too, saying what to write.
- **A benchmark to beat.** `beats: buy_and_hold` with no benchmark column and no prices.csv fails, saying how to give
  one; it never passes for want of a benchmark.
- **Undefined numbers.** A rule on a number that is not defined (`null`: Sharpe from two bars, profit factor with no
  losing trade) fails with the note that says why: `sharpe is undefined (null): volatility, sharpe and sortino are
  null: they need at least 3 rows (2 returns)`.
- **Old or broken files.** A file last written before the step started is an earlier run's: it is refused, not
  read, and so is a file the reader refuses (with its message: the file, the line, the column). Then
  `strategy out/equity.csv: the standard files` fails with why, and every other finding fails as not checked.

The first finding, `the standard files`, passes once they are read and lists every note of the computation - how the
periods per year were chosen, too few bars, positions left open - so a passing report still shows the caveats.

`numbers:` ([Expectations](expectations.md#numbers)) reads the same numbers with rules and no guardrails, for a
step that checks a backtest's numbers alongside other readers':

```yaml
expect:
  numbers: {reader: trading, files: {equity: out/equity.csv}, rules: {sharpe: ">= 1", trades: ">= 30"}}
```

## Sealed data

Numbers testbench computes can still come from a strategy tuned on the very rows they grade. testbench.yaml's `data:`
names the price files the plans read and seals the latest rows (`holdout_from`): every plan but the one that opens
the holdout sees the file cut before them, each opening is counted (and `max_opens` refuses more), a
`walk_forward:` plan runs window by window with no row from the future, a `lookahead:` plan reruns the steps on the
data cut earlier and fails when a decision changes with rows after its day, `model_cutoff:` flags results on data a
language model the strategy asks may already know, and every report records the sha256 of the data its steps saw,
which `testbench verify` and `testbench diff` compare: [Sealed data](trading-data.md).

## The files

All three are UTF-8 CSV with a header line. Column names match whatever their case and surrounding spaces; other
columns are allowed and ignored; blank lines are skipped. The writers below produce them, or any tool that writes CSV
can.

### trades.csv - one row per fill

| column | | |
|---|---|---|
| `time` | required | when the fill happened ([times](#times)); rows in time order, equal times are fine |
| `symbol` | required | the instrument, as the backtest names it |
| `side` | required | `buy` or `sell`, in any case. Opening a short is a sell, covering it a buy |
| `quantity` | required | above 0: the size without a sign (shares, contracts, coins); the direction is in `side` |
| `price` | required | above 0: the fill price, slippage included |
| `fee` | required | 0 or more: the fill's commissions and fees in the account currency (0 when none were charged) |
| `order_id` | optional | text |
| `strategy` | optional | text |
| `slippage` | optional | 0 or more: what slippage cost on this fill, in the account currency. It is already in `price`, so it is never taken off again; it only records that slippage was modeled |

A partial fill is a row of its own. A file with only the header is a strategy that never traded.

### equity.csv - one row per bar

| column | | |
|---|---|---|
| `time` | required | the bar's time; strictly increasing, one row per bar (or per day) |
| `equity` | required | above 0: the account's whole value - cash plus open positions - at that time |
| `cash` | optional | any number (below 0 on margin) |
| `position_value` | optional | any number: the value of the open positions (below 0 when short, 0 when flat) |
| `benchmark` | optional | above 0: a buy-and-hold price or equity series at the same times |

### prices.csv - closes for a benchmark (optional)

| column | | |
|---|---|---|
| `time` | required | rows of several symbols may be mixed; each symbol's times increase |
| `symbol` | required | |
| `close` | required | above 0 |

### Numbers

Numbers are plain (`1234.5`, `-3`, `1e-3`), never empty, NaN or infinite. A numeric optional column, once there, has
a value on every row: leave the column out when some rows have none. Text columns may be empty.

### Times

Times are ISO 8601: `2024-01-02`, `2024-01-02T15:30:00`, `2024-01-02 15:30:00.250`, `2024-01-02T15:30:00Z`,
`2024-01-02T15:30:00+02:00`. Either every time in a file has a zone (`Z` or an offset; offsets may differ, as across
a change to summer time) or none has, and files read together follow the same convention, so their times compare. A
Unix timestamp is refused with the line that converts it.

### When a file is refused

A file that breaks a rule is refused with a message that names the file, the line and the column and says what to
write instead, up to ten problems at a time - the reader is often an agent fixing its strategy's output from the
message alone:

```
out/trades.csv: 3 problems
  line 3, column `side`: 'long' is not a side; write buy or sell (opening a short is a sell, covering it a buy)
  line 3, column `quantity`: -5 is negative; write the size without a sign and put the direction in side (a sale or a short is a sell)
  line 4, column `time`: '2024-01-02T09:00:00Z' is before line 3's '2024-01-02T11:00:00Z'; fills are in time order (sort the rows by time)
```

```
out/equity.csv, line 1 (the header): no `time` column. The column with no name may hold the times (pandas writes an unnamed index that way): name it, e.g. df.to_csv(path, index_label='time'). equity.csv needs time, equity (optional: cash, position_value, benchmark); found: equity
```

## The numbers

`compute()` and the command line return every one of these keys, always, in this order; a number that is not
defined is `null` (`None`), and a note says why. Checks refer to them by name (`strategy:`, `numbers:`), so they are
never renamed.

| key | what it is |
|---|---|
| `start_equity`, `end_equity` | equity on the first and the last row |
| `bars`, `days`, `years` | rows of equity.csv; calendar days from the first to the last; days / 365.25 |
| `periods_per_year` | bars per year, for the yearly numbers ([below](#periods-per-year)) |
| `risk_free` | the yearly risk-free rate used (a fraction; 0 unless given) |
| `total_return` | last equity / first - 1 |
| `cagr` | (last / first) ^ (1 / years) - 1, on calendar time |
| `volatility` | sample standard deviation of the bar returns x sqrt(periods per year) |
| `sharpe` | mean(r - rf) / stdev(r) x sqrt(periods per year); r = equity / previous equity - 1, rf = (1 + risk_free) ^ (1 / periods per year) - 1 |
| `sortino` | mean(r - rf) / downside deviation x sqrt(periods per year); downside deviation = sqrt(mean(min(0, r - rf)^2)) over every bar |
| `max_drawdown` | the deepest fall from a running peak: min(equity / peak - 1), between -1 and 0 |
| `max_drawdown_days`, `max_drawdown_bars` | how long that drawdown ran: from its peak to the first row back at the peak, or to the last row if it never got back (of two equal drawdowns, the first) |
| `max_drawdown_recovered` | whether the equity got back to that peak |
| `longest_drawdown_days` | the longest any drawdown ran, by the same rule |
| `calmar` | cagr / -max_drawdown |
| `fills` | rows of trades.csv |
| `trades` | round trips ([below](#round-trips)) |
| `win_rate` | share of round trips that made money, after fees |
| `profit_factor` | money made by winning round trips / money lost by losing ones |
| `average_trade` | mean profit of a round trip, after fees, in the account currency |
| `average_trade_return` | mean of each round trip's profit / the cost of the entries it closed |
| `total_fees` | sum of `fee` |
| `costs_modeled` | `true` when some fee or slippage is above 0 |
| `traded_value` | sum of quantity x price over the fills |
| `turnover`, `turnover_per_year` | traded value / mean equity, over the whole backtest and per calendar year |
| `exposure` | share of the bars that start with a position open: from `position_value` when equity.csv has it, else from the fills up to each row's time |
| `open_positions` | symbols with a position still open after the last fill |
| `benchmark_return`, `benchmark_sharpe` | the same numbers for buy and hold over the same rows ([below](#benchmark)) |
| `excess_return` | total_return - benchmark_return |
| `notes` | plain-words caveats: how periods per year were chosen; fewer than 30 round trips; fewer than 100 bars or less than a year; no fees or slippage at all; no benchmark; positions still open; fills outside the equity's times; why a number is null |

Without trades.csv the trade keys are `null`; without a benchmark the benchmark keys are.

### Periods per year

The yearly numbers (volatility, Sharpe, Sortino) scale by the square root of the bars in a year, and a year of daily
bars is 252 bars on a stock market but 365 on a crypto exchange, which never closes. Getting this wrong changes
Sharpe by a fifth (sqrt(365 / 252) = 1.20), so testbench decides it explicitly and says how in the first note:

| median spacing of the times | periods per year |
|---|---|
| under half a day (intraday) | stocks: bars a day (the median count per date) x 252; crypto: 365 days / the spacing |
| half a day to 4 days (daily) | stocks: 252; crypto: 365 |
| 4 to 10 days (weekly) | 52 |
| 10 to 20 days | 26 |
| 20 to 45 days (monthly) | 12 |
| 45 to 135 days (quarterly) | 4 |
| 135 to 270 days | 2 |
| more | 1 |

The calendar is crypto when more than a tenth of the rows fall on a Saturday or Sunday (in the times' own zone),
stocks otherwise. Override either: `calendar="stocks"` or `"crypto"` (`--calendar`), or the number itself,
`periods_per_year=365` (`--periods-per-year 365`). A 24-hour weekday market (FX) is read as 252 days of 24 hours a
day; give its number if that is not what you mean. CAGR does not depend on this: it uses calendar time.

### Round trips

Fills are matched first in, first out, per symbol, the way pyfolio's round trips are. Every fill that shrinks or
closes a position is one round trip, matched against the oldest open lots; its profit is net of its own fee and of
the matched lots' share of theirs. The part of a fill that flips the position (sell 15 when long 10) opens a new
lot. For example:

| fill | | round trip |
|---|---|---|
| buy 10 A at 100, fee 1 | opens a lot, fee 0.1 a unit | |
| sell 4 A at 110, fee 0.4 | | 4 x (110 - 100) - 4 x (0.1 + 0.1) = 39.2 |
| sell 6 A at 90, fee 0.6 | | 6 x (90 - 100) - 6 x 0.2 = -61.2 |

Two round trips, one won: win rate 0.5, profit factor 39.2 / 61.2 = 0.64. A position still open at the end is in
the equity (and in `open_positions`) but in no round trip.

### Benchmark

The benchmark is buy and hold over exactly the same rows as the equity: the `benchmark` column of equity.csv, or
prices.csv's closes for one symbol, taking at each equity row the last close at or before its time. With prices.csv
the symbol is the only one in the file, or the only one traded, or the one named (`symbol="SPY"`, `--symbol SPY`).
When prices.csv has no close at or before the first equity row, there is no benchmark, and a note says so.

## Tear sheet

One page a trader - or anyone who has never written a backtest - can read, made by testbench from the same standard
files and never by the strategy:

[![The tear sheet of a three-year moving-average crossover backtest. A header with the period (2022-01-03 to 2024-11-25, 756 daily bars, 252 a year), the equity from 100,000 to 153,562.54, and the sha256 of equity.csv, trades.csv and prices.csv it was computed from. Tiles: returns - total return +53.6%, CAGR +16.0%, buy and hold +52.1%, excess return +1.5%; risk - volatility 12%, Sharpe 1.25 (buy and hold 0.88), Sortino 1.97, max drawdown -13.4%, drawdown length 357 days (255 bars, recovered), Calmar 1.19; trading - 10 trades from 20 fills, win rate 50%, profit factor 4.12, fees 1,328.27, exposure 52%, turnover 6.6x a year. A chart of the account's growth in blue against buy and hold in grey, a drawdown chart in red with its deepest point marked -13.4%, and the monthly returns of 2022 to 2024 as a table of blue and red cells with each year's total and buy and hold's.](assets/tearsheet.png)](assets/tearsheet.png)

It shows:

- **the headline numbers** from [the table above](#the-numbers): total return, CAGR, buy and hold and the excess over
  it; volatility, Sharpe, Sortino, max drawdown and how long it lasted, Calmar; trades, win rate, profit factor, fees,
  exposure and turnover. Each says what it is when you hover it, and again in "What each number means" below;
- **the growth of the account against buy and hold**, both from 0% on one axis (on a log scale when either grew or
  shrank tenfold), and **the drawdown**: how far below its last peak the account was, its deepest point marked.
  Hovering a chart shows the date, both returns and the drawdown there;
- **the monthly returns** as a year by month table, each year's total and buy and hold's beside it. Gains are blue and
  losses red, in three shades a side (under 2%, 2% to 5%, 5% or more), and every cell also has its sign, so neither
  colour vision nor print loses it; the text on every colour keeps at least 4.5:1 contrast (WCAG AA), in light and
  dark;
- **the trades**: round trips won and lost, the largest win and loss, the average win, loss and trade, and how long
  positions were held (mean, median, longest);
- **the caveats**: the [notes](#the-numbers) - how the periods per year were chosen, too few trades or bars, no costs
  charged, no benchmark - and always "Past results, on past data. Not financial advice."

The page is self-contained: charts are inline SVG drawn by testbench, with no script, no font or library to fetch
and nothing that reaches the network, so it opens the same from a CI artifact, an email or a laptop offline.

**Where it appears.** A step that keeps its files among its `artifacts:` gets one - testbench finds an `equity.csv`
by its header (a `time` and an `equity` column) and reads `trades.csv` and `prices.csv` from the same folder:

```yaml
      - name: backtest
        run: python backtest.py                 # writes out/equity.csv, out/trades.csv, out/prices.csv
        artifacts: [out/*.csv]
```

- `testbench report --format html`, the pages `testbench ui` writes and a run's page on the live dashboard
  (`testbench ui --serve`) show it under the steps;
- `testbench report RUN --tearsheet [-o FILE]` writes the tear sheet alone (to `tearsheet.html` in the run folder
  unless `-o` says where), the page to send someone;
- report.md - what an agent reads - gets a short Strategy section:

```text
## Strategy: backtest (out/equity.csv)

testbench's numbers from `steps/01-backtest/artifacts/out/equity.csv`, `trades.csv` and `prices.csv`, not the strategy's (`testbench report 20261112-091352-backtest --tearsheet` draws them):
- total return +53.6% (buy and hold +52.1%, excess +1.5%), CAGR +16.0%
- Sharpe 1.25, Sortino 1.97, max drawdown -13.4% over 357 days (recovered), Calmar 1.19
- 10 trades, win rate 50%, profit factor 4.12, fees 1,328.27, exposure 52%, turnover 6.6x a year
- caveats: periods per year: 252 - daily bars; only 10 round trips; Sharpe and Sortino take the risk-free rate as 0% a year. Past results, on past data. Not financial advice.
```

A file the reader refuses gives a short note in its place, with the reader's message (``No tear sheet:
steps/01-backtest/artifacts/out/equity.csv, line 3, column `equity`: -5 is negative; ...``), never a traceback; the
rest of the report is unchanged. A prices.csv that cannot be the benchmark (several symbols and none named) is left
out, with a caveat saying why.

In a report the tear sheet uses the defaults: a risk-free rate of 0 and the calendar read from the times. For other
settings, or for a folder outside any run, make the page directly:

```
python -m agent_testbench.trading.tearsheet FOLDER_OR_EQUITY_CSV [-o tearsheet.html] [--risk-free RATE]
    [--calendar auto|stocks|crypto] [--periods-per-year N] [--symbol SYMBOL] [--log | --linear]
```

From Python, `tearsheet.sheet(equity, trades, prices, risk_free=0.04)` returns what the page shows (its `numbers` are
`compute()`'s) or `{"ok": False, "note": ...}`, and `tearsheet.page(tearsheet.for_folder("out"))` is the page as text
(`from agent_testbench.trading import tearsheet`).

## From Python

```python
from agent_testbench.trading.files import read_equity, read_trades, write_equity, write_trades, TradingFileError
from agent_testbench.trading.metrics import compute

m = compute("out/equity.csv", "out/trades.csv", "out/prices.csv", risk_free=0.04, calendar="stocks", symbol="SPY")
m["sharpe"], m["notes"]
```

`compute(equity, trades=None, prices=None, *, risk_free=0.0, periods_per_year=None, calendar="auto", symbol=None)`
takes for each file a path, a pandas DataFrame (a DatetimeIndex, or an index named `time`, is the time column when
there is none; a Series is read as equity) or a list of rows (dicts, or tuples in the column order above). It raises
`TradingFileError` (a `ValueError`) for a refused file and `ValueError` for a refused setting.

The writers - `write_trades(rows, path)`, `write_equity(rows, path)`, `write_prices(rows, path)` - take the same
rows, check them by the same rules (refusing with the row number) and write the standard file: times as ISO 8601,
numbers as plain decimals, the required columns first, then whichever optional columns the rows have. A value of
`None` counts as no value.

On the command line:

```
python -m agent_testbench.trading.metrics EQUITY [TRADES] [--benchmark PRICES] [--symbol SYMBOL]
    [--risk-free RATE] [--calendar auto|stocks|crypto] [--periods-per-year N] [--out metrics.json]
```

It prints the numbers as JSON (and writes them to `--out`); a refused file or setting prints `error: ` and the
message, and exits 2.

The same numbers come from the `trading` [metric reader](plugins.md#metrics), which `strategy:` and `numbers:` read
them with: `testbench numbers trading out/equity.csv out/trades.csv out/prices.csv --opt risk_free=0.04` prints
each one by name (`--json` for JSON).

## Writing the files from a framework

Each snippet records every fill and every bar's equity and writes them with the writers. The **plain pandas**,
**vectorbt** (1.2) and **backtrader** (1.9.78) snippets were run: on the same backtest testbench's numbers matched
the framework's own (below). The NautilusTrader, LEAN and freqtrade snippets were **not run**: they follow those
frameworks' documented APIs, so check their output with the reader before trusting it.

### Plain pandas

```python
import pandas as pd
from agent_testbench.trading.files import write_equity, write_trades

close = pd.read_csv("closes.csv", index_col="time", parse_dates=True)["close"]
long = close.rolling(10).mean() > close.rolling(30).mean()            # the signal, known at each close
units = long.shift(1, fill_value=False) * 100.0                       # hold 100 units from the next bar on
traded = units.diff().fillna(units)                                   # what changed hands at each bar's close
fee = traded.abs() * close * 0.001                                    # 0.1% a fill
fills = traded[traded != 0]
write_trades([{"time": t, "symbol": "SPY", "side": "buy" if q > 0 else "sell", "quantity": abs(q),
               "price": close[t], "fee": fee[t]} for t, q in fills.items()], "out/trades.csv")
cash = 10_000 - (traded * close + fee).cumsum()
write_equity(pd.DataFrame({"equity": cash + units * close, "position_value": units * close, "benchmark": close}),
             "out/equity.csv")
```

### vectorbt

```python
import pandas as pd
import vectorbt as vbt
from agent_testbench.trading.files import write_equity, write_trades

pf = vbt.Portfolio.from_signals(close, entries, exits, fees=0.001, slippage=0.0005, init_cash=10_000, freq="1D")
orders = pf.orders.records_readable           # Order Id, Column, Timestamp, Size, Price, Fees, Side
write_trades([{"time": o["Timestamp"], "symbol": "BTC", "side": o["Side"], "quantity": o["Size"],
               "price": o["Price"], "fee": o["Fees"], "order_id": o["Order Id"]} for _, o in orders.iterrows()],
             "out/trades.csv")
write_equity(pd.DataFrame({"equity": pf.value(), "benchmark": close}), "out/equity.csv")
```

On a 700-day crypto backtest, total return, max drawdown, closed trades, win rate, profit factor, fees paid and the
benchmark's return matched `pf.stats()` to the last digits. Sharpe, Sortino and Calmar differ in the third or fourth
digit, by convention: vectorbt adds a first return against the initial cash, and its Calmar counts years as bars / 365
where testbench uses calendar days / 365.25. vectorbt's drawdown duration starts one bar later, at the first bar
below the peak.

### backtrader

```python
import backtrader as bt
from agent_testbench.trading.files import write_equity, write_trades

class Record(bt.Analyzer):
    """Every fill and every bar's account value, for testbench's trades.csv and equity.csv."""

    def start(self):
        self.fills, self.bars = [], []

    def notify_order(self, order):
        if order.status == order.Completed:
            ex = order.executed
            self.fills.append({"time": bt.num2date(ex.dt), "symbol": order.data._name or "data0",
                               "side": "buy" if order.isbuy() else "sell", "quantity": abs(ex.size),
                               "price": ex.price, "fee": ex.comm, "order_id": order.ref})

    def next(self):
        broker = self.strategy.broker
        self.bars.append({"time": self.data.datetime.datetime(0), "equity": broker.getvalue(),
                          "cash": broker.getcash(), "benchmark": self.data.close[0]})

cerebro.addanalyzer(Record, _name="record")
strategy = cerebro.run()[0]
write_trades(strategy.analyzers.record.fills, "out/trades.csv")
write_equity(strategy.analyzers.record.bars, "out/equity.csv")
```

On a 600-bar SMA-cross backtest with commission, the closed trades, the winners, their net profit, the open trade
and the final value matched backtrader's `TradeAnalyzer` and broker exactly. An order filled in parts is one row here
(backtrader reports the average price); use `order.executed.exbits` for a row per part.

### NautilusTrader (not run)

In your `Strategy`, record each `OrderFilled` event and the account on each bar, and write the files when it stops:

```python
import pandas as pd
from nautilus_trader.model.enums import OrderSide
from agent_testbench.trading.files import write_equity, write_trades

    # in your Strategy subclass; set self.fills, self.bars = [], [] in __init__
    def on_order_filled(self, event):
        self.fills.append({"time": pd.Timestamp(event.ts_event, tz="UTC"), "symbol": str(event.instrument_id),
                           "side": "buy" if event.order_side == OrderSide.BUY else "sell",
                           "quantity": event.last_qty.as_double(), "price": event.last_px.as_double(),
                           "fee": event.commission.as_double(), "order_id": str(event.client_order_id)})

    def on_bar(self, bar):                       # one instrument's bars: one row per bar
        instrument = bar.bar_type.instrument_id
        account = self.portfolio.account(instrument.venue)
        unrealized = self.portfolio.unrealized_pnl(instrument)
        self.bars.append({"time": pd.Timestamp(bar.ts_event, tz="UTC"),
                          "equity": account.balance_total().as_double() + (unrealized.as_double() if unrealized else 0)})

    def on_stop(self):
        write_trades(self.fills, "out/trades.csv")
        write_equity(self.bars, "out/equity.csv")
```

The equity line is for a margin account in one currency; a commission charged in another currency (some crypto
venues take it in the base asset) has to be converted to the account currency first.

### LEAN (not run)

In your `QCAlgorithm` (Python), record fills from order events and the portfolio value on each slice, all in UTC:

```python
from agent_testbench.trading.files import write_equity, write_trades

    # in initialize(): self.fills, self.bars = [], []
    def on_order_event(self, e):
        if e.fill_quantity != 0:
            self.fills.append({"time": e.utc_time.strftime("%Y-%m-%dT%H:%M:%SZ"), "symbol": e.symbol.value,
                               "side": "buy" if e.fill_quantity > 0 else "sell", "quantity": abs(e.fill_quantity),
                               "price": e.fill_price, "fee": e.order_fee.value.amount, "order_id": e.order_id})

    def on_data(self, data):
        self.bars.append({"time": self.utc_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "equity": self.portfolio.total_portfolio_value})

    def on_end_of_algorithm(self):
        write_trades(self.fills, "trades.csv")
        write_equity(self.bars, "equity.csv")
```

agent-testbench has to be importable where LEAN runs the algorithm (with the LEAN CLI, that is LEAN's Docker image),
and the files land in the algorithm's working folder there. Where it is not, keep the rows and save them as CSV with
`self.object_store.save(...)` instead, then run the reader on the copies. LEAN reports fees in the quote currency
(`e.order_fee.value.currency`): convert any that is not the account's.

### freqtrade (not run)

freqtrade's backtest results hold one row per round trip (entry and exit), and a daily profit series. Turn each trade
into its two fills, and the daily profit into equity:

```python
from pathlib import Path
import pandas as pd
from freqtrade.data.btanalysis import load_backtest_data, load_backtest_stats
from agent_testbench.trading.files import write_equity, write_trades

results = Path("user_data/backtest_results")                  # the latest backtest in it
fills = []
for t in load_backtest_data(results).itertuples():
    entry, leave = ("sell", "buy") if t.is_short else ("buy", "sell")
    fills += [{"time": t.open_date, "symbol": t.pair, "side": entry, "quantity": t.amount, "price": t.open_rate,
               "fee": t.amount * t.open_rate * t.fee_open},
              {"time": t.close_date, "symbol": t.pair, "side": leave, "quantity": t.amount, "price": t.close_rate,
               "fee": t.amount * t.close_rate * t.fee_close}]
write_trades(sorted(fills, key=lambda f: f["time"]), "out/trades.csv")
stats = load_backtest_stats(results)["strategy"]["MyStrategy"]
daily = pd.DataFrame(stats["daily_profit"], columns=["time", "profit"])
write_equity(pd.DataFrame({"time": pd.to_datetime(daily["time"], utc=True),
                           "equity": stats["starting_balance"] + daily["profit"].cumsum()}), "out/equity.csv")
```

This equity counts only closed trades, day by day, so drawdowns inside an open trade do not show: the max drawdown
here is a floor, not the real one. freqtrade's fees are rates (`fee_open`, `fee_close`), turned into money above.
