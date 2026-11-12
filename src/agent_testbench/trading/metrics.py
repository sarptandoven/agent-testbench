"""testbench's own numbers for a backtest, computed from the standard files (files.py), never taken from the strategy.

    python -m agent_testbench.trading.metrics equity.csv [trades.csv] [--benchmark prices.csv] [--symbol SPY]
        [--risk-free 0.04] [--calendar auto|stocks|crypto] [--periods-per-year N] [--out metrics.json]

prints the numbers as JSON (and writes them to --out); a refused file or setting prints why and exits 2.
compute() returns the same dict: every key in KEYS, always, as a float, int or bool, or None where the number is not
defined (no trades file, no benchmark, no volatility), then `notes`, plain-words caveats (too few trades or bars, no
costs, no benchmark, how the periods per year were chosen). Checks will name these keys (`sharpe: ">= 1"`), so a key
is never renamed or dropped. Only the standard library: no numpy, no pandas.

Definitions (docs/trading.md has them with worked examples):
- r_i = equity_i / equity_(i-1) - 1, one return per bar after the first; rf = (1 + risk_free) ** (1 / ppy) - 1, the
  yearly risk-free rate per bar; ppy = periods per year (periods_per_year() says how it is chosen).
- total_return = last / first - 1. cagr = (last / first) ** (1 / years) - 1, years = calendar days / 365.25.
- volatility = stdev(r) * sqrt(ppy), the sample standard deviation (n - 1).
- sharpe = mean(r - rf) / stdev(r) * sqrt(ppy). sortino = mean(r - rf) / sqrt(mean(min(0, r - rf) ** 2)) * sqrt(ppy),
  the downside deviation taken over every bar.
- max_drawdown = min(equity / running peak - 1), in [-1, 0]. Its drawdown runs from the peak before it to the first
  row back at that peak (or the last row if none is): max_drawdown_days (calendar), max_drawdown_bars,
  max_drawdown_recovered; longest_drawdown_days is the longest any drawdown ran. Of equal drawdowns (within TIE) the
  first counts, so rounding never picks one. calmar = cagr / -max_drawdown.
- Round trips, FIFO per symbol (as pyfolio's): every fill that shrinks or closes a position makes one round trip
  against the oldest open lots; its profit is net of its own fee and the matched lots' share of theirs. The part of a
  fill that flips the position opens a new lot. trades = round trips; win_rate = share with profit > 0;
  profit_factor = profits / losses; average_trade = mean profit (account currency); average_trade_return = mean of
  profit / the matched entries' cost.
- traded_value = sum(quantity * price); turnover = traded_value / mean equity (over the whole backtest);
  turnover_per_year = turnover / years. costs_modeled = some fee or slippage is above 0.
- exposure = share of the bars (row to next row) that start with a position open: from position_value when equity.csv
  has it, else from the fills up to each row's time.
- Benchmark: equity.csv's benchmark column, or prices.csv's closes for one symbol at each equity row (the last close
  at or before it). benchmark_return, benchmark_sharpe as above; excess_return = total_return - benchmark_return.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, deque
from pathlib import Path

from ..config import number
from . import files as F

DAY = 86400.0
YEAR_DAYS = 365.25                    # a calendar year, for cagr and turnover_per_year
CALENDARS = {"stocks": 252, "crypto": 365}
WEEKEND_SHARE = 0.1                   # daily rows on Saturdays and Sundays above this share: a market open every day
SPACING = ((0.5, None, "intraday"), (4, None, "daily"), (10, 52, "weekly"), (20, 26, "two-weekly"),
           (45, 12, "monthly"), (135, 4, "quarterly"), (270, 2, "half-yearly"), (math.inf, 1, "yearly"))
FEW_TRADES = 30
FEW_BARS = 100
TINY = 1e-9                           # a position, or a standard deviation of returns, this small counts as zero
TIE = 1e-12                           # drawdowns this close are equal; equity this close below its peak is back at it
KEYS = ("start_equity", "end_equity", "bars", "days", "years", "periods_per_year", "risk_free",
        "total_return", "cagr", "volatility", "sharpe", "sortino",
        "max_drawdown", "max_drawdown_days", "max_drawdown_bars", "max_drawdown_recovered", "longest_drawdown_days",
        "calmar",
        "fills", "trades", "win_rate", "profit_factor", "average_trade", "average_trade_return", "total_fees",
        "costs_modeled", "traded_value", "turnover", "turnover_per_year", "exposure", "open_positions",
        "benchmark_return", "benchmark_sharpe", "excess_return")
TRADE_KEYS = ("fills", "trades", "win_rate", "profit_factor", "average_trade", "average_trade_return", "total_fees",
              "costs_modeled", "traded_value", "turnover", "turnover_per_year", "open_positions")


def compute(equity, trades=None, prices=None, *, risk_free: float = 0.0, periods_per_year: float | None = None,
            calendar: str = "auto", symbol: str | None = None) -> dict:
    """The numbers for a backtest. equity, trades and prices are what files.read_* take (a path, a DataFrame, rows);
    trades and prices are optional. risk_free is the yearly rate as a fraction (0.04 for 4%). periods_per_year
    overrides the one read from the times; calendar ("auto", "stocks": 252 days, "crypto": 365) decides daily and
    intraday bars only. symbol picks prices.csv's benchmark when it has several. Raises files.TradingFileError for a
    refused file and ValueError for a refused setting, with a message that says what to change."""
    _check_settings(risk_free, periods_per_year, calendar)
    names = {k: F.name_of(src, k) for k, src in (("equity", equity), ("trades", trades), ("prices", prices))}
    bars = F.read_equity(equity, names["equity"])
    fills = None if trades is None else F.read_trades(trades, names["trades"])
    closes = None if prices is None else F.read_prices(prices, names["prices"])
    F.check_same_zones((names["equity"], bars), (names["trades"], fills or []), (names["prices"], closes or []))
    if len(bars) < 2:
        raise F.TradingFileError(f"{names['equity']} has 1 row; the numbers need at least 2 (a start and an end), "
                                 "one row per bar")
    times, values = [b.time for b in bars], [b.equity for b in bars]
    ppy, how = _periods(times, calendar, periods_per_year)
    notes = [how]
    out: dict = dict.fromkeys(KEYS)
    days = (times[-1] - times[0]).total_seconds() / DAY
    years = days / YEAR_DAYS
    out.update(start_equity=values[0], end_equity=values[-1], bars=len(bars), days=days, years=years,
               periods_per_year=ppy, risk_free=float(risk_free))
    try:
        rf = math.exp(math.log1p(risk_free) / ppy) - 1             # (1 + risk_free) ** (1 / ppy) - 1
    except OverflowError:
        rf = math.inf
    out.update(_performance(values, ppy, rf, notes, ""))
    out["cagr"] = _cagr(values[0], values[-1], years)
    if out["cagr"] is None:
        notes.append(f"cagr is null: {days:.3g} days is too short to turn into a yearly rate")
    out.update(_drawdowns(times, values))
    if out["max_drawdown"] < 0 and out["cagr"] is not None:
        out["calmar"] = out["cagr"] / -out["max_drawdown"]
    elif out["max_drawdown"] == 0:
        notes.append("calmar is null: the equity never fell below an earlier peak, so there is no drawdown to "
                     "divide by")
    if len(bars) < FEW_BARS:
        notes.append(f"only {len(bars)} bars: ratios from fewer than {FEW_BARS} bars swing a lot from one sample to "
                     "the next")
    if years < 1:
        notes.append(f"the backtest covers {days:.3g} days: the yearly numbers (cagr, volatility, sharpe, sortino, "
                     "calmar) stretch less than a year into one")
    if fills is None:
        notes.append(f"no trades file: {', '.join(TRADE_KEYS)} are null")
    else:
        out.update(_trading(fills, bars, years, names, notes))
    out["exposure"] = _exposure(bars, fills)
    if out["exposure"] is None:
        notes.append("exposure is null: equity.csv has no position_value column and there is no trades file")
    bench = _benchmark(bars, closes, symbol, fills, names, notes)
    if bench is not None:
        perf = _performance(bench, ppy, rf, notes, "benchmark_")
        out.update(benchmark_return=perf["benchmark_total_return"], benchmark_sharpe=perf["benchmark_sharpe"])
        out["excess_return"] = out["total_return"] - out["benchmark_return"]
    overflowed = [k for k, v in out.items() if isinstance(v, float) and not math.isfinite(v)]
    if overflowed:
        notes.append(f"{', '.join(overflowed)} {'is' if len(overflowed) == 1 else 'are'} null: out of the range of a "
                     "number (check the files for a value that is far off)")
    out = {k: (None if k in overflowed else v) for k, v in out.items()}
    out["notes"] = notes
    return out


def _check_settings(risk_free, periods_per_year, calendar) -> None:
    if isinstance(risk_free, bool) or not isinstance(risk_free, (int, float)) or not math.isfinite(risk_free) \
            or risk_free <= -1:
        raise ValueError(f"risk_free is {risk_free!r}; give the yearly risk-free rate as a fraction, e.g. 0.04 for "
                         "4% (0 when left out)")
    if periods_per_year is not None and not number(periods_per_year, positive=True):
        raise ValueError(f"periods_per_year is {periods_per_year!r}; give a number above 0, e.g. 252 for daily bars "
                         "of a stock market, 365 for daily bars of crypto, 52 for weekly bars")
    if calendar not in ("auto", *CALENDARS):
        raise ValueError(f"calendar is {calendar!r}; use auto, stocks (252 trading days a year) or crypto (365)")


# ---- periods per year --------------------------------------------------------------------------------------------

def periods_per_year(times, calendar: str = "auto") -> tuple[float, str]:
    """(bars per year, a note saying how that was chosen) for bars at these times (at least two, increasing).

    From the median spacing: weekly bars 52 a year, two-weekly 26, monthly 12, quarterly 4, half-yearly 2, yearly 1.
    Daily bars: 252 on a stock market's calendar, 365 on one that trades every day (crypto). Intraday bars: on the
    stocks calendar, the bars of a day (the median count per date) times 252; on the crypto calendar, a 365-day year
    over the median spacing. calendar="auto" picks crypto when more than WEEKEND_SHARE of the rows fall on a Saturday
    or Sunday (in the times' own zone), stocks otherwise."""
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    spacing = statistics.median(gaps) / DAY
    _, per_year, kind = next(s for s in SPACING if spacing < s[0])
    if per_year:
        return float(per_year), f"periods per year: {per_year} - {kind} bars (a median of {spacing:.3g} days apart)"
    weekend = sum(t.weekday() >= 5 for t in times) / len(times)
    if calendar == "auto":
        calendar = "crypto" if weekend > WEEKEND_SHARE else "stocks"
        why = (f"{weekend:.0%} of the rows fall on a weekend, so a market that trades every day (crypto); for a stock "
               "market pass calendar='stocks' (--calendar stocks)" if calendar == "crypto" else
               f"{'no' if weekend == 0 else 'almost no'} rows fall on a weekend, so a stock market's 252 trading days "
               "a year; for a market that trades every day (crypto) pass calendar='crypto' (--calendar crypto)")
    else:
        why = f"the {calendar} calendar, as given"
    days = CALENDARS[calendar]
    if kind == "daily":
        return float(days), f"periods per year: {days} - daily bars; {why}"
    if calendar == "crypto":
        ppy = days / spacing
        return ppy, (f"periods per year: {ppy:.6g} - intraday bars {spacing * 24 * 60:.3g} minutes apart (the "
                     f"median), around the clock, {days} days a year; {why}")
    per_date = Counter(t.date() for t in times)
    per_day = statistics.median(per_date.values())
    ppy = days * per_day
    few = (f"; counted over only {len(per_date)} date{'s' if len(per_date) > 1 else ''}, so pass "
           "periods_per_year (--periods-per-year) to be sure" if len(per_date) < 3 else "")
    return float(ppy), (f"periods per year: {ppy:.6g} - intraday bars, {per_day:g} a day (the median per date) times "
                        f"{days} trading days; {why}{few}")


def _periods(times, calendar: str, given) -> tuple[float, str]:
    if given is not None:
        return float(given), f"periods per year: {given:g}, as given"
    return periods_per_year(times, calendar)


# ---- returns, risk, drawdowns ------------------------------------------------------------------------------------

def returns(values) -> list[float]:
    """One simple return per value after the first: v_i / v_(i-1) - 1."""
    return [b / a - 1.0 for a, b in zip(values, values[1:])]


def _sum(xs) -> float:
    """math.fsum, or a plain sum where fsum gives up (an intermediate overflow, infinities of both signs): absurd
    values become an infinity or NaN, which compute() reports as null with a note, never a traceback."""
    xs = list(xs)
    try:
        return math.fsum(xs)
    except (OverflowError, ValueError):
        return sum(xs)


def _mean(xs) -> float:
    return _sum(x / len(xs) for x in xs)


def _stdev(xs) -> float:
    m = _mean(xs)
    return math.sqrt(_sum((x - m) * (x - m) for x in xs) / (len(xs) - 1))


def sharpe(rets, ppy: float, rf: float = 0.0) -> float | None:
    """mean(r - rf) / stdev(r) * sqrt(ppy); None with fewer than two returns or returns that never vary."""
    if len(rets) < 2:
        return None
    sd = _stdev(rets)
    return None if sd <= TINY else (_mean(rets) - rf) / sd * math.sqrt(ppy)


def sortino(rets, ppy: float, rf: float = 0.0) -> float | None:
    """mean(r - rf) / downside deviation * sqrt(ppy), the downside deviation sqrt(mean(min(0, r - rf)^2)) over every
    return; None with fewer than two returns or none below rf."""
    if len(rets) < 2:
        return None
    down = math.sqrt(_sum(min(0.0, r - rf) * min(0.0, r - rf) for r in rets) / len(rets))
    return None if down <= TINY else (_mean(rets) - rf) / down * math.sqrt(ppy)


def _performance(values, ppy: float, rf: float, notes: list, prefix: str) -> dict:
    rets = returns(values)
    what = "the benchmark's" if prefix else "the equity's"
    out = {f"{prefix}total_return": values[-1] / values[0] - 1, f"{prefix}sharpe": sharpe(rets, ppy, rf)}
    if not prefix:
        out.update(volatility=_stdev(rets) * math.sqrt(ppy) if len(rets) > 1 else None, sortino=sortino(rets, ppy, rf))
    if len(rets) < 2:
        if not prefix:
            notes.append("volatility, sharpe and sortino are null: they need at least 3 rows (2 returns)")
    elif out[f"{prefix}sharpe"] is None:
        notes.append(f"{prefix}sharpe is null: {what} returns never varied from bar to bar, so there is no "
                     "volatility to divide by")
    if not prefix and len(rets) > 1 and out["sortino"] is None:
        notes.append("sortino is null: no bar returned less than the risk-free rate, so there is no downside to "
                     "divide by")
    return out


def _cagr(first: float, last: float, years: float) -> float | None:
    try:
        return math.exp(math.log(last / first) / years) - 1
    except (OverflowError, ZeroDivisionError, ValueError):            # ValueError: last / first underflowed to 0
        return None


def _drawdowns(times, values) -> dict:
    """Max drawdown, and how long its drawdown ran: from the peak before it to the first row back at that peak (the
    last row when the equity never got back). Rounding never decides: a value within TIE of the peak is back at it,
    and of drawdowns within TIE of each other the first is the max drawdown."""
    peak_i, worst, worst_peak = 0, 0.0, None
    spells = []                                       # (peak row, end row, recovered)
    under = False
    for i, v in enumerate(values):
        if v >= values[peak_i] * (1 - TIE):
            if under:
                spells.append((peak_i, i, True))
                under = False
                peak_i = i
            elif v >= values[peak_i]:
                peak_i = i
        else:
            under = True
            dd = v / values[peak_i] - 1
            if dd < worst:
                if worst_peak is None or dd < worst - TIE:
                    worst_peak = peak_i
                worst = dd
    if under:
        spells.append((peak_i, len(values) - 1, False))
    span = {p: (e, ok) for p, e, ok in spells}

    def days(p, e):
        return (times[e] - times[p]).total_seconds() / DAY
    end, recovered = span[worst_peak] if worst_peak is not None else (0, True)
    start = worst_peak or 0
    return {"max_drawdown": worst, "max_drawdown_days": days(start, end), "max_drawdown_bars": end - start,
            "max_drawdown_recovered": recovered,
            "longest_drawdown_days": max((days(p, e) for p, e, _ in spells), default=0.0)}


# ---- trades ------------------------------------------------------------------------------------------------------

def round_trips(fills) -> tuple[list[tuple[float, float]], list[str]]:
    """FIFO per symbol: (profit net of fees, cost of the entries closed) for every fill that shrinks or closes a
    position, and the symbols still open at the end. A fill matches the oldest open lots of the other side first; its
    fee and each lot's fee count pro rata to the quantity matched; what is left of it opens a lot of its own."""
    books: dict[str, deque] = {}
    trips = []
    for f in fills:
        book = books.setdefault(f.symbol, deque())
        sign = 1 if f.side == "buy" else -1
        left, fee_each = f.quantity, f.fee / f.quantity
        profit = cost = 0.0
        matched = False
        while left > TINY * f.quantity and book and book[0][3] != sign:
            lot = book[0]                             # [quantity left, price, fee per unit, sign, size when opened]
            m = min(left, lot[0])
            profit += lot[3] * m * (f.price - lot[1]) - m * (lot[2] + fee_each)
            cost += m * lot[1]
            lot[0] -= m
            left -= m
            matched = True
            if lot[0] <= TINY * lot[4]:
                book.popleft()
        if matched:
            trips.append((profit, cost))
        if left > TINY * f.quantity:
            book.append([left, f.price, fee_each, sign, left])
    return trips, sorted(s for s, b in books.items() if b)


def _trading(fills, bars, years: float, names: dict, notes: list) -> dict:
    trips, still_open = round_trips(fills)
    profits = [p for p, _ in trips]
    wins = [p for p in profits if p > 0]
    losses = [-p for p in profits if p < 0]
    traded = _sum(f.quantity * f.price for f in fills)
    turnover = traded / _mean([b.equity for b in bars])
    out = {"fills": len(fills), "trades": len(trips), "total_fees": _sum(f.fee for f in fills),
           "costs_modeled": any(f.fee > 0 or (f.slippage or 0) > 0 for f in fills) if fills else None,
           "traded_value": traded, "turnover": turnover,
           "turnover_per_year": turnover / years if years > 0 else None, "open_positions": len(still_open),
           "win_rate": len(wins) / len(trips) if trips else None,
           "profit_factor": _sum(wins) / _sum(losses) if losses else None,
           "average_trade": _mean(profits) if trips else None,
           "average_trade_return": _mean([p / c if c else math.nan for p, c in trips]) if trips else None}
    if not fills:
        notes.append(f"{names['trades']} has no fills: the strategy never traded")
        return out
    if len(trips) < FEW_TRADES:
        notes.append(f"only {len(trips)} round trip{'s' if len(trips) != 1 else ''}: win_rate, profit_factor and "
                     f"the average trade say little with fewer than {FEW_TRADES}")
    if not out["costs_modeled"]:
        notes.append(f"every fee and slippage in {names['trades']} is 0: the backtest charged no trading costs, so "
                     "its returns are better than an account would get")
    if trips and not losses:
        notes.append(f"profit_factor is null: none of the {len(trips)} round trips lost money")
    if still_open:
        shown = ", ".join(still_open[:5]) + (f" and {len(still_open) - 5} more" if len(still_open) > 5 else "")
        notes.append(f"{len(still_open)} symbol{'s' if len(still_open) > 1 else ''} still had a position open at the "
                     f"end ({shown}): its profit or loss is in the equity but in no round trip")
    outside = sum(f.time < bars[0].time or f.time > bars[-1].time for f in fills)
    if outside:
        notes.append(f"{outside} fill{'s are' if outside > 1 else ' is'} before {names['equity']}'s first row or "
                     "after its last: counted in the trade numbers, but exposure only covers the equity's times")
    return out


def _exposure(bars, fills) -> float | None:
    if bars[0].position_value is not None:
        held = [abs(b.position_value) > TINY * b.equity for b in bars]
    elif fills is not None:
        held, position, size, open_now, j = [], {}, {}, set(), 0
        for b in bars:
            while j < len(fills) and fills[j].time <= b.time:
                f = fills[j]
                position[f.symbol] = position.get(f.symbol, 0.0) + f.signed_quantity
                size[f.symbol] = max(size.get(f.symbol, 0.0), f.quantity)
                (open_now.add if abs(position[f.symbol]) > TINY * size[f.symbol] else open_now.discard)(f.symbol)
                j += 1
            held.append(bool(open_now))
    else:
        return None
    return sum(held[:-1]) / (len(bars) - 1)


# ---- benchmark ---------------------------------------------------------------------------------------------------

def _benchmark(bars, closes, symbol, fills, names: dict, notes: list) -> list[float] | None:
    has_column = bars[0].benchmark is not None
    if closes is None:
        if has_column:
            return [b.benchmark for b in bars]
        notes.append("no benchmark: add a benchmark column to equity.csv, or pass prices.csv (--benchmark), to "
                     "compare with buy and hold over the same period")
        return None
    if has_column:
        notes.append(f"{names['equity']}'s benchmark column is not used: {names['prices']} was given")
    symbols = sorted({c.symbol for c in closes})
    traded = {f.symbol for f in fills or []}
    if symbol is None:
        if len(symbols) == 1:
            symbol = symbols[0]
        elif len(traded) == 1 and next(iter(traded)) in symbols:
            symbol = next(iter(traded))               # the one symbol traded: buy and hold of the same thing
        else:
            raise F.TradingFileError(f"{names['prices']} has closes for {len(symbols)} symbols "
                                     f"({', '.join(symbols[:10])}{', ...' if len(symbols) > 10 else ''}); say which is "
                                     "the benchmark: symbol='SPY' (--symbol SPY)")
    elif symbol not in symbols:
        raise F.TradingFileError(f"{names['prices']} has no closes for {symbol!r}; it has "
                                 f"{', '.join(symbols[:10])}{', ...' if len(symbols) > 10 else ''}")
    series = [c for c in closes if c.symbol == symbol]
    out, j = [], -1
    for b in bars:
        while j + 1 < len(series) and series[j + 1].time <= b.time:
            j += 1
        out.append(series[j].close if j >= 0 else None)
    if out[0] is None:
        notes.append(f"no benchmark: {names['prices']}'s first close for {symbol} ({series[0].time.isoformat()}) is "
                     f"after {names['equity']}'s first row ({bars[0].time.isoformat()}); the benchmark needs a close "
                     "at or before it to cover the same period")
        return None
    if series[-1].time < bars[-1].time:
        notes.append(f"{names['prices']}'s last close for {symbol} ({series[-1].time.isoformat()}) is before "
                     f"{names['equity']}'s last row ({bars[-1].time.isoformat()}): the benchmark stays at that close "
                     "to the end")
    return out


# ---- command line ------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m agent_testbench.trading.metrics",
                                description="testbench's own numbers for a backtest, from its standard files "
                                            "(docs/trading.md), printed as JSON")
    p.add_argument("equity", help="equity.csv: time, equity (optional: cash, position_value, benchmark)")
    p.add_argument("trades", nargs="?", help="trades.csv: time, symbol, side, quantity, price, fee")
    p.add_argument("--benchmark", metavar="PRICES_CSV", help="prices.csv (time, symbol, close) for buy and hold")
    p.add_argument("--symbol", help="the benchmark's symbol when prices.csv has several")
    p.add_argument("--risk-free", type=float, default=0.0, metavar="RATE",
                   help="yearly risk-free rate as a fraction, e.g. 0.04 (default 0)")
    p.add_argument("--calendar", choices=("auto", *CALENDARS), default="auto",
                   help="daily and intraday bars: stocks (252 days a year) or crypto (365); auto reads the weekends")
    p.add_argument("--periods-per-year", type=float, metavar="N", help="bars per year, overriding the guess")
    p.add_argument("--out", metavar="JSON", help="also write the numbers to this file")
    a = p.parse_args(argv)
    try:
        result = compute(a.equity, a.trades, a.benchmark, risk_free=a.risk_free, periods_per_year=a.periods_per_year,
                         calendar=a.calendar, symbol=a.symbol)
    except ValueError as e:                           # TradingFileError too: the message says what to change
        print(f"error: {e}", file=sys.stderr)
        return 2
    text = json.dumps(result, indent=2, allow_nan=False)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
