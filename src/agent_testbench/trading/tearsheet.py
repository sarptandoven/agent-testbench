"""The tear sheet: one page a trader - or someone who has never written a backtest - can read, made by testbench from
the standard files (files.py), never by the strategy.

    python -m agent_testbench.trading.tearsheet FOLDER_OR_EQUITY_CSV [-o tearsheet.html] [--risk-free 0.04]
        [--calendar auto|stocks|crypto] [--periods-per-year N] [--symbol SPY] [--log | --linear]

It shows the headline numbers of metrics.compute (each with its definition on hover and in a list below), the growth
of the account against buy and hold, the drawdown below each running peak, the monthly returns as a year x month
table, the round trips (largest win and loss, holding times), and the caveats: metrics' notes and "Past results, on
past data. Not financial advice." The charts are inline SVG drawn here - no script, no library, no network - and the
page reads the same in light and dark (the monthly colours keep text on them at 4.5:1 or more, WCAG AA).

`testbench report --format html`, `testbench ui` and the live dashboard show a tear sheet for every step whose
artifacts hold an equity.csv in the standard format (known by its header: time and equity columns), with trades.csv
and prices.csv from the same folder when they are there; `testbench report RUN --tearsheet` writes the page alone and
report.md gets a short Strategy section (markdown()). A file trading.files refuses is a short note saying why, never
a traceback. Only the standard library.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import datetime as dt
import hashlib
import html
import math
import statistics
import sys
from collections import deque
from pathlib import Path

from . import files as F, metrics as M

DAY = 86400.0
NOT_ADVICE = "Past results, on past data. Not financial advice."
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")
BINS = (0.02, 0.05)                   # a month's return below 2% either way, 2% to 5%, 5% or more: three shades a side
# The monthly table's colours, by colour scheme: a diverging pair (blue up, red down, weakest first) around a neutral
# grey, with the text colour written on them. tests/test_tearsheet.py checks every one at 4.5:1 or more (WCAG AA).
COLORS = {"light": {"text": "#1d1d1b", "flat": "#f0efec", "up": ("#e0ecfb", "#a9cbf4", "#6da7ec"),
                    "down": ("#fbdcdb", "#f5b0ae", "#ee8380")},
          "dark": {"text": "#ecebe7", "flat": "#2c2c2a", "up": ("#1f2f45", "#1f4a7d", "#255fa6"),
                   "down": ("#45211f", "#792a28", "#a8312f")}}
INK = {"light": {"surface": "#ffffff", "line": "#e3e2de", "grid": "#ecebe7", "axis": "#b9b8b2", "muted": "#6b6b66",
                 "strategy": "#2a78d6", "benchmark": "#898781", "drawdown": "#d03b3b", "warn": "#8a5a00",
                 "caveat": "#fdf7e7"},
       "dark": {"surface": "#1e1e1c", "line": "#34332f", "grid": "#2c2c2a", "axis": "#4a4945", "muted": "#a3a29c",
                "strategy": "#3987e5", "benchmark": "#898781", "drawdown": "#e66767", "warn": "#e0b45c",
                "caveat": "#29251b"}}
WIDTH, LEFT, RIGHT, TOP, BOTTOM = 940, 60, 16, 12, 26       # the charts' viewBox width and margins
GROWTH_HEIGHT, DRAWDOWN_HEIGHT = 280, 150
STRIPS = 120                          # hover strips across a chart, each with the values of its last bar
MAX_SHEETS = 10                       # equity.csv files looked at in one folder

# Headline numbers: (key in metrics.compute, label, format, what it is in plain words).
HEADLINE = {
    "Returns": (
        ("total_return", "Total return", "pct", "How much the account grew from the first row to the last: last "
         "equity / first equity - 1."),
        ("cagr", "CAGR", "pct", "Compound annual growth rate: the yearly return that, compounded over the backtest's "
         "calendar time, gives the total return."),
        ("benchmark_return", "Buy and hold", "pct", "The total return of simply holding the benchmark over the same "
         "rows (equity.csv's benchmark column, or prices.csv's closes)."),
        ("excess_return", "Excess return", "pct", "Total return minus buy and hold: what the strategy added, or lost, "
         "against just holding."),
    ),
    "Risk": (
        ("volatility", "Volatility", "pct0", "How much the returns swing from bar to bar, as a yearly standard "
         "deviation."),
        ("sharpe", "Sharpe", "ratio", "The average return above the risk-free rate divided by how much returns swing "
         "(their standard deviation), scaled to a year. Above 1 is good; above 2 is rare and worth doubting."),
        ("sortino", "Sortino", "ratio", "Like Sharpe, but divided only by the downside: the swings below the "
         "risk-free rate."),
        ("max_drawdown", "Max drawdown", "pct", "The deepest fall of the account from a peak to a later low: the "
         "worst loss someone who started at the top would have seen."),
        ("max_drawdown_days", "Drawdown length", "days", "How long the deepest drawdown lasted: from its peak until "
         "the account was back at that peak, or to the end if it never got back."),
        ("calmar", "Calmar", "ratio", "CAGR divided by the size of the max drawdown: yearly growth for each unit of "
         "the worst fall."),
    ),
    "Trading": (
        ("trades", "Trades", "count", "Round trips: every fill that shrinks or closes a position, matched first in, "
         "first out against the fills that opened it."),
        ("win_rate", "Win rate", "pct0", "The share of round trips that made money after fees."),
        ("profit_factor", "Profit factor", "ratio", "Money made by the winning round trips divided by money lost by "
         "the losing ones. Above 1, the winners paid for the losers."),
        ("total_fees", "Fees", "money", "Every fee and commission in trades.csv, in the account's currency."),
        ("exposure", "Exposure", "pct0", "The share of bars that started with a position open: how much of the time "
         "money was at risk."),
        ("turnover_per_year", "Turnover", "turnover", "How many times a year the strategy traded its own size: the "
         "value of all fills / the average equity, per calendar year."),
    ),
}
TRADE_ROWS = {
    "Largest win": "The round trip that made the most, after fees, in the account's currency.",
    "Largest loss": "The round trip that lost the most, after fees, in the account's currency.",
    "Average win": "The mean profit of the round trips that made money.",
    "Average loss": "The mean loss of the round trips that lost money.",
    "Average trade": "The mean profit of all round trips, winners and losers together.",
    "Holding time": "How long a round trip held its position, from the fills it closed (first in, first out) to the "
                    "fill that closed them: the mean, the median and the longest.",
    "Still open": "Symbols with a position open after the last fill: their profit or loss is in the equity but in "
                  "no round trip.",
}


# ---- what a sheet is made of --------------------------------------------------------------------------------------

def standard(path) -> bool:
    """Whether a file is an equity.csv in the standard format, by its header: a time and an equity column."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.reader(f):
                if any(c.strip() for c in row):
                    return {"time", "equity"} <= {c.strip().lower() for c in row}
    except (OSError, UnicodeDecodeError, csv.Error):
        return False
    return False


def sheet(equity, trades=None, prices=None, *, shown: dict | None = None, risk_free: float = 0.0,
          periods_per_year: float | None = None, calendar: str = "auto", symbol: str | None = None,
          log: bool | None = None) -> dict:
    """Everything a tear sheet shows, from the files' paths: {"ok": True, "numbers": metrics.compute(...), ...}, or
    {"ok": False, "note": why} for a file or setting that is refused (never an exception). shown names the files in
    messages ({"equity": "steps/01-x/artifacts/equity.csv", ...}; their paths by default). log: the growth chart on a
    log scale (None: when the account or the benchmark grew or shrank tenfold)."""
    paths = {"equity": Path(equity), "trades": Path(trades) if trades else None,
             "prices": Path(prices) if prices else None}
    names = {k: (shown or {}).get(k) or (str(p) if p else "") for k, p in paths.items()}

    def plain(text: str) -> str:
        for k, p in paths.items():
            if p:
                text = text.replace(str(p), names[k])
        return text
    settings = {"risk_free": risk_free, "periods_per_year": periods_per_year, "calendar": calendar, "symbol": symbol}
    extra = []
    try:
        try:
            numbers = M.compute(*(str(p) if p else None for p in paths.values()), **settings)
        except F.TradingFileError as e:
            if not paths["prices"] or not str(e).startswith(str(paths["prices"])):
                raise
            extra.append(f"{names['prices']} is not used as the benchmark: {plain(str(e))}")
            paths["prices"] = None
            numbers = M.compute(*(str(p) if p else None for p in paths.values()), **settings)
        bars = F.read_equity(paths["equity"])
        fills = F.read_trades(paths["trades"]) if paths["trades"] else None
        closes = F.read_prices(paths["prices"]) if paths["prices"] else None
        bench = M._benchmark(bars, closes, symbol, fills, names, []) \
            if numbers["benchmark_return"] is not None else None
    except ValueError as e:                            # TradingFileError too: the message says what to change
        return {"ok": False, "note": plain(str(e))}
    except Exception as e:                             # a page is always made; the note names what went wrong
        return {"ok": False, "note": f"the tear sheet could not be made ({type(e).__name__}: {plain(str(e))})"}
    numbers["notes"] = [plain(n) for n in numbers["notes"]] + extra
    numbers["notes"].append(f"Sharpe and Sortino take the risk-free rate as {risk_free * 100:g}% a year.")
    times, values = [b.time for b in bars], [b.equity for b in bars]
    profits = [p for p, _ in M.round_trips(fills)[0]] if fills else []
    held = holding_times(fills) if fills else []
    files = [(names[k], _sha256(p)) for k, p in paths.items() if p]
    return {"ok": True, "numbers": numbers, "times": times, "values": values, "benchmark": bench,
            "monthly": monthly(times, values), "benchmark_monthly": monthly(times, bench) if bench else None,
            "profits": profits, "held": held if len(held) == len(profits) else [], "files": files,
            "has_trades": fills is not None, "log": log}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def monthly(times, values) -> dict:
    """Returns by calendar month and by year, in the times' own zone: a month's return is its last value over the
    last value of the month before (the first value, for the first month) - 1; a year's, its last value over the last
    value before it - 1. {"months": {(year, month): r}, "years": {year: r}}; a month with no row has no return (the
    next month's spans it)."""
    ends: dict = {}
    for t, v in zip(times, values):
        ends[(t.year, t.month)] = v                   # times increase, so the keys are in time order
    months, starts, last = {}, {}, {}
    before = values[0]
    for (y, m), v in ends.items():
        starts.setdefault(y, before)
        months[(y, m)] = v / before - 1
        last[y] = before = v
    return {"months": months, "years": {y: last[y] / starts[y] - 1 for y in starts}}


def holding_times(fills) -> list[float]:
    """Seconds each round trip held its position, one per round trip in metrics.round_trips' order: the quantity
    weighted mean over the lots it closed of (its fill's time - the lot's time). The same first in, first out
    matching as metrics.round_trips."""
    books: dict[str, deque] = {}
    out = []
    for f in fills:
        book = books.setdefault(f.symbol, deque())
        sign = 1 if f.side == "buy" else -1
        left, held, matched_qty, matched = f.quantity, 0.0, 0.0, False
        while left > M.TINY * f.quantity and book and book[0][2] != sign:
            lot = book[0]                             # [quantity left, time, sign, size when opened]
            m = min(left, lot[0])
            held += m * (f.time - lot[1]).total_seconds()
            matched_qty += m
            lot[0] -= m
            left -= m
            matched = True
            if lot[0] <= M.TINY * lot[3]:
                book.popleft()
        if matched:
            out.append(held / matched_qty if matched_qty else 0.0)
        if left > M.TINY * f.quantity:
            book.append([left, f.time, sign, left])
    return out


def find(folder) -> list[Path]:
    """The equity.csv files in the standard format in a folder and below it (at most MAX_SHEETS), or the file itself."""
    folder = Path(folder)
    if folder.is_file():
        return [folder]
    found = sorted(p for p in folder.rglob("*") if p.name.lower() == "equity.csv" and p.is_file())
    return [p for p in found if standard(p)][:MAX_SHEETS]


def _next_to(equity: Path, name: str) -> Path | None:
    return next((p for p in equity.parent.iterdir() if p.name.lower() == name and p.is_file()), None)


def for_folder(folder, **settings) -> list[tuple[str, dict]]:
    """(label, sheet) for each standard equity.csv in a folder, with trades.csv and prices.csv next to it."""
    folder = Path(folder)
    base = folder.parent if folder.is_file() else folder
    out = []
    for equity in find(folder):
        found = {"equity": equity, "trades": _next_to(equity, "trades.csv"), "prices": _next_to(equity, "prices.csv")}
        shown = {k: _rel(p, base) for k, p in found.items() if p}
        out.append((shown["equity"], sheet(found["equity"], found["trades"], found["prices"], shown=shown,
                                           **settings)))
    return out


def _rel(p: Path, base: Path) -> str:
    try:
        return p.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(p)


def for_report(report: dict, run_dir, **settings) -> list[tuple[str, dict]]:
    """(step name, sheet) for each step of a run whose artifacts hold an equity.csv in the standard format."""
    from ..report import inside
    run_dir = Path(run_dir)
    out = []
    for s in report.get("steps") if isinstance(report.get("steps"), list) else []:
        if not isinstance(s, dict) or not isinstance(s.get("artifacts"), list):
            continue
        for a in s["artifacts"]:
            if not isinstance(a, str) or "(not copied" in a or Path(a).name.lower() != "equity.csv":
                continue
            equity = run_dir / str(s.get("dir") or "") / "artifacts" / a
            if not inside(run_dir, equity) or not standard(equity):
                continue
            found = {"equity": equity}
            for role in ("trades", "prices"):
                p = _next_to(equity, f"{role}.csv")
                found[role] = p if p and inside(run_dir, p) else None
            shown = {k: _rel(p, run_dir) for k, p in found.items() if p}
            label = str(s.get("name")) + (f" ({Path(a).as_posix()})" if Path(a).parent != Path(".") else "")
            out.append((label, sheet(found["equity"], found["trades"], found["prices"], shown=shown, **settings)))
    return out


# ---- numbers as text ----------------------------------------------------------------------------------------------

def fmt(value, kind: str) -> str:
    """A number as the tear sheet shows it ("-" when it is not defined); a minus sign is "-" (pages draw it as one)."""
    if value is None or isinstance(value, bool):
        return "-" if value is None else ("yes" if value else "no")
    if kind == "pct":
        return _pct(value)
    if kind == "pct0":
        return f"{value * 100:.0f}%"
    if kind == "ratio":
        return f"{value:.2f}"
    if kind == "count":
        return f"{int(value):,}"
    if kind == "money":
        return f"{value:,.2f}"
    if kind == "signed_money":
        return f"{value:+,.2f}"
    if kind == "days":
        return "none" if value == 0 else duration(value * DAY)
    if kind == "turnover":
        return f"{value:.1f}x a year"
    return f"{value:g}"


def _pct(r: float, decimals: int | None = None) -> str:
    x = r * 100
    if decimals is None:
        decimals = 1 if abs(x) < 1000 else 0
    text = f"{x:+,.{decimals}f}%"
    return "0%" if float(text[:-1].replace(",", "")) == 0 and decimals == 0 else text


def duration(seconds: float) -> str:
    """A length of time in words: 45 days, 6.5 hours, 12 minutes."""
    for unit, size in (("day", DAY), ("hour", 3600.0), ("minute", 60.0)):
        if seconds >= 2 * size or (unit == "day" and seconds >= size):
            n = seconds / size
            return f"1 {unit}" if n == 1 else f"{n:.0f} {unit}s" if n >= 10 or n == round(n) else f"{n:.1f} {unit}s"
    return f"{seconds:.0f} seconds" if seconds != 1 else "1 second"


def _when(t: dt.datetime, intraday: bool) -> str:
    return t.strftime("%Y-%m-%d %H:%M") if intraday else t.strftime("%Y-%m-%d")


def _intraday(times) -> bool:
    return any((t.hour, t.minute, t.second, t.microsecond) != (0, 0, 0, 0) for t in times)


def _short_note(note: str) -> str:
    if note.startswith("periods per year:"):
        return note.split(";")[0]
    return note.split(": ")[0].rstrip(".")


# ---- markdown -----------------------------------------------------------------------------------------------------

def markdown(report: dict, run_dir) -> list[str]:
    """report.md's Strategy section: a few lines per step with a tear sheet."""
    lines = []
    for label, s in for_report(report, run_dir):
        lines += [f"## Strategy: {' '.join(label.splitlines())}", ""]
        if not s["ok"]:
            first = s["note"].splitlines()
            lines += [f"No tear sheet: {first[0]}" + (f" (and {len(first) - 1} more lines)" if len(first) > 1 else ""),
                      ""]
            continue
        n = s["numbers"]
        bench = (f" (buy and hold {fmt(n['benchmark_return'], 'pct')}, excess {fmt(n['excess_return'], 'pct')})"
                 if n["benchmark_return"] is not None else " (no benchmark)")
        dd = (f" over {fmt(n['max_drawdown_days'], 'days')}"
              f" ({'recovered' if n['max_drawdown_recovered'] else 'not recovered by the end'})"
              if n["max_drawdown"] else "")
        names = [f"`{s['files'][0][0]}`"] + [f"`{Path(f).name}`" for f, _ in s["files"][1:]]
        lines += [f"testbench's numbers from {', '.join(names[:-1]) + ' and ' if len(names) > 1 else ''}{names[-1]}, "
                  f"not the strategy's (`testbench report {report.get('run_id')} --tearsheet` draws them):",
                  f"- total return {fmt(n['total_return'], 'pct')}{bench}, CAGR {fmt(n['cagr'], 'pct')}",
                  f"- Sharpe {fmt(n['sharpe'], 'ratio')}, Sortino {fmt(n['sortino'], 'ratio')}, max drawdown "
                  f"{fmt(n['max_drawdown'], 'pct')}{dd}, Calmar {fmt(n['calmar'], 'ratio')}"]
        if s["has_trades"]:
            lines += [f"- {fmt(n['trades'], 'count')} trades, win rate {fmt(n['win_rate'], 'pct0')}, profit factor "
                      f"{fmt(n['profit_factor'], 'ratio')}, fees {fmt(n['total_fees'], 'money')}, exposure "
                      f"{fmt(n['exposure'], 'pct0')}, turnover {fmt(n['turnover_per_year'], 'turnover')}"]
        else:
            lines += [f"- no trades.csv next to it, so no trade numbers; exposure {fmt(n['exposure'], 'pct0')}"]
        lines += [f"- caveats: {'; '.join(_short_note(x) for x in n['notes'])}. {NOT_ADVICE}", ""]
    return lines


# ---- HTML ---------------------------------------------------------------------------------------------------------

def _e(text) -> str:
    return html.escape(str(text))


def _num(text: str) -> str:
    """A formatted number for a page: escaped, its minus signs drawn as minus signs ("-" alone, no number: a dash)."""
    return "\u2013" if text == "-" else _e(text).replace("-", "\u2212")


def _ascii(text: str) -> str:
    """Characters beyond ASCII as numeric references, which HTML and XML (the SVGs) both read, in any encoding."""
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


def css() -> str:
    """The tear sheet's own style, scoped to .ts, in light and dark; the monthly colours come from COLORS."""
    def tokens(mode: str) -> str:
        c, k = COLORS[mode], INK[mode]
        out = [f"--ts-{name}:{value}" for name, value in k.items()]
        out += [f"--ts-text:{c['text']}", f"--ts-flat:{c['flat']}"]
        out += [f"--ts-u{i}:{v}" for i, v in enumerate(c["up"], 1)]
        out += [f"--ts-d{i}:{v}" for i, v in enumerate(c["down"], 1)]
        return ";".join(out)
    return (f".ts{{{tokens('light')}}}"
            f"@media (prefers-color-scheme:dark){{:root:not([data-theme=light]) .ts{{{tokens('dark')}}}}}"
            f":root[data-theme=dark] .ts{{{tokens('dark')}}}"
            """
.ts{color:var(--ts-text)}.ts h3{font-size:14px;margin:22px 0 6px}.ts .ts-src{font-size:12px;color:var(--ts-muted);
overflow-wrap:anywhere}.ts-group{font-size:12px;color:var(--ts-muted);margin:14px 0 6px;text-transform:uppercase;
letter-spacing:.04em}.ts-tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:8px}
.ts-tile{background:var(--ts-surface);border:1px solid var(--ts-line);border-radius:8px;padding:8px 10px;cursor:help}
.ts-tile b{display:block;font-size:12px;color:var(--ts-muted);font-weight:500}.ts-tile span{display:block;
font-size:20px;font-weight:600;line-height:1.3}.ts-tile small{display:block;font-size:12px;color:var(--ts-muted)}
.ts-wide{overflow-x:auto}.ts-chart{display:block;width:100%;min-width:680px;height:auto;
background:var(--ts-surface);border:1px solid var(--ts-line);border-radius:8px}.ts-chart text{font-size:11px;
fill:var(--ts-muted);font-variant-numeric:tabular-nums}
.ts-grid line{stroke:var(--ts-grid);stroke-width:1}.ts-zero{stroke:var(--ts-axis);stroke-width:1}
.ts-line{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}
.ts-strategy{stroke:var(--ts-strategy)}.ts-benchmark{stroke:var(--ts-benchmark);stroke-width:1.5}
.ts-dd{stroke:var(--ts-drawdown);stroke-width:1.5}.ts-area{fill:var(--ts-drawdown);fill-opacity:.14;stroke:none}
.ts-dot{stroke:var(--ts-surface);stroke-width:2}.ts-dot.ts-strategy{fill:var(--ts-strategy)}
.ts-dot.ts-dd{fill:var(--ts-drawdown)}.ts-chart text.ts-label{fill:var(--ts-text);font-weight:600}
.ts-hits rect{fill:var(--ts-text);fill-opacity:0}.ts-hits rect:hover{fill-opacity:.07}
.ts-legend{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;margin:0 0 6px}.ts-key{display:inline-block;
width:18px;border-top:2px solid var(--ts-strategy);vertical-align:middle;margin-right:6px}
.ts-key.ts-benchmark{border-top:2px solid var(--ts-benchmark)}.ts-key.ts-dd{border-top-color:var(--ts-drawdown)}
.ts table.ts-months{width:auto;min-width:100%;border-collapse:separate;border-spacing:0;background:transparent}
.ts-months th,.ts-months td{border:1px solid var(--bg,var(--ts-surface));padding:4px 6px;text-align:right;
font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}.ts-months th{color:var(--ts-muted);font-weight:500;
background:transparent}.ts-months th:first-child{text-align:left}.ts-months td{color:var(--ts-text);cursor:help}
.ts-months td.ts-year{font-weight:700}.ts-months td.ts-none{background:transparent;cursor:auto}
.ts-z{background:var(--ts-flat)}.ts-u1{background:var(--ts-u1)}.ts-u2{background:var(--ts-u2)}
.ts-u3{background:var(--ts-u3)}.ts-d1{background:var(--ts-d1)}.ts-d2{background:var(--ts-d2)}
.ts-d3{background:var(--ts-d3)}.ts-scale{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:12px;
color:var(--ts-muted);margin:6px 0 0}.ts-scale i{display:inline-block;width:14px;height:14px;border-radius:3px;
vertical-align:-2px;margin-right:5px}.ts table.ts-trades{width:auto;min-width:min(100%,520px)}
.ts-trades th{font-weight:500;color:var(--ts-muted);cursor:help}.ts-trades td{font-variant-numeric:tabular-nums}
.ts-caveats{background:var(--ts-caveat);border:1px solid var(--ts-line);border-left:4px solid var(--ts-warn);
border-radius:8px;padding:8px 14px;margin:22px 0 0;font-size:14px}.ts-caveats h3{margin:4px 0}
.ts-caveats ul{margin:6px 0;padding-left:20px}.ts-advice{font-weight:700;margin:8px 0 4px}
.ts-defs{margin:14px 0 0;font-size:14px}.ts-defs summary{cursor:pointer;color:var(--ts-muted)}
.ts-defs dt{font-weight:600;margin-top:6px}.ts-defs dd{margin:0 0 0 0}.ts-note{border-left:4px solid var(--ts-warn);
padding:6px 12px;background:var(--ts-caveat);border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere}
""")


def fragment(s: dict, heading: str = "", level: int = 2, style: bool = True) -> str:
    """One tear sheet as HTML to put in a page (its style included unless style=False): what sheet() made."""
    h = f"h{level}"
    out = [f"<style>{css()}</style>"] if style else []
    out.append("<section class=ts>")
    if heading:
        out.append(f"<{h}>{_e(heading)}</{h}>")
    if not s["ok"]:
        out.append(f"<p class=ts-note>No tear sheet: {_e(s['note'])}</p></section>")
        return _ascii("".join(out))
    n, times = s["numbers"], s["times"]
    intraday = _intraday(times)
    files = s["files"]
    used = ", ".join(_e(Path(f).name) for f, _ in files[:-1]) + (" and " if len(files) > 1 else "") + \
        _e(Path(files[-1][0]).name)
    out.append(f"<p class=ts-src>{_e(_when(times[0], intraday))} to {_e(_when(times[-1], intraday))} \u00b7 "
               f"{n['bars']:,} bars, {n['periods_per_year']:,g} a year \u00b7 equity "
               f"{_num(fmt(n['start_equity'], 'money'))} to {_num(fmt(n['end_equity'], 'money'))}<br>"
               f"Computed by testbench from {used}, never by the strategy that is graded. sha256: "
               + ", ".join(f"<code title='{_e(f)}'>{_e(Path(f).name)} {sha[:12]}</code>" for f, sha in files) + "</p>")
    out.append(_tiles(n))
    out.append(_growth(s, intraday))
    out.append(_drawdown(s, intraday))
    out.append(_months(s))
    out.append(_trades(s))
    out.append("<aside class=ts-caveats><h3>Caveats</h3><ul>" + "".join(f"<li>{_e(x)}</li>" for x in n["notes"])
               + f"</ul><p class=ts-advice>{_e(NOT_ADVICE)}</p></aside>")
    out.append("<details class=ts-defs><summary>What each number means</summary><dl>"
               + "".join(f"<dt>{_e(label)}</dt><dd>{_e(what)}</dd>"
                         for group in HEADLINE.values() for _, label, _, what in group)
               + "".join(f"<dt>{_e(k)}</dt><dd>{_e(v)}</dd>" for k, v in TRADE_ROWS.items()) + "</dl></details>")
    out.append("</section>")
    return _ascii("".join(out))


def _tiles(n: dict) -> str:
    out = []
    for group, items in HEADLINE.items():
        tiles = []
        for key, label, kind, what in items:
            if key == "turnover_per_year" and n[key] is None and n["turnover"] is not None:
                key, kind = "turnover", "turnover_total"
            value = n[key]
            shown = f"{value:.1f}x in all" if kind == "turnover_total" else fmt(value, kind)
            title = what + ("" if value is not None else " Not defined here: the caveats say why.")
            tiles.append(f"<div class=ts-tile data-key={key} data-value='{'null' if value is None else repr(value)}' "
                         f"title='{_e(title)}'><b>{_e(label)}</b><span>{_num(shown)}</span>"
                         f"<small>{_num(_sub(key, n))}</small></div>")
        out.append(f"<div class=ts-group>{_e(group)}</div><div class=ts-tiles>{''.join(tiles)}</div>")
    return "".join(out)


def _sub(key: str, n: dict) -> str:
    """The small line under a tile's number."""
    if key == "total_return":
        return f"{fmt(n['start_equity'], 'money')} to {fmt(n['end_equity'], 'money')}"
    if key == "cagr":
        return f"over {n['years']:.1f} years" if n["years"] >= 1 else f"over {n['days']:.0f} days"
    if key == "benchmark_return" and n[key] is None:
        return "no benchmark"
    if key == "sharpe" and n["benchmark_sharpe"] is not None:
        return f"buy and hold {n['benchmark_sharpe']:.2f}"
    if key == "max_drawdown_days" and n["max_drawdown"]:
        return f"{n['max_drawdown_bars']:,} bars, " + ("recovered" if n["max_drawdown_recovered"] else "not recovered")
    if key == "trades" and n["fills"] is not None:
        return f"from {n['fills']:,} fills"
    if key == "total_fees" and n["costs_modeled"] is not None:
        return "costs modeled" if n["costs_modeled"] else "no costs charged"
    if key in ("trades", "win_rate", "profit_factor", "total_fees", "turnover_per_year", "turnover") \
            and n["fills"] is None:
        return "no trades.csv"
    return ""


# ---- charts -------------------------------------------------------------------------------------------------------

def _x(t: dt.datetime) -> float:
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def _nice(lo: float, hi: float, n: int = 5) -> tuple[float, float, list[float], float]:
    """Round axis bounds and ticks around lo..hi, about n steps of 1, 2, 2.5 or 5 times a power of ten."""
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw * (1 - 1e-9))
    a, b = math.floor(lo / step + 1e-9) * step, math.ceil(hi / step - 1e-9) * step
    return a, b, [a + i * step for i in range(round((b - a) / step) + 1)], step


def _tick_pct(v: float, step: float) -> str:
    d = 0 if step * 100 >= 0.999 else 1 if step * 100 >= 0.0999 else 2
    if abs(v) < step / 1000:
        return "0%"
    return f"{v * 100:+,.{d}f}%"


def _log_ticks(glo: float, ghi: float) -> list[float]:
    for mults in ((1, 2, 5), (1, 1.5, 2, 3, 5, 7)):
        ticks = sorted(m * 10.0 ** k for k in range(-6, 9) for m in mults if glo <= m * 10.0 ** k <= ghi)
        if len(ticks) >= 3:
            return ticks
    return [glo, ghi]


def _time_ticks(t0: dt.datetime, t1: dt.datetime) -> list[tuple[dt.datetime, str]]:
    """Calendar ticks between two times (about 4 to 8): years, months, days, hours or minutes."""
    span = (t1 - t0).total_seconds() / DAY
    out = []
    if span >= 60:
        months = next(m for m in (1, 2, 3, 6, 12, 24, 60, 120, 240, 600) if span / 30.44 / m <= 8)
        i = t0.year * 12 + t0.month - 1
        i += (-i) % months
        if dt.datetime(i // 12, i % 12 + 1, 1, tzinfo=t0.tzinfo) < t0:
            i += months
        while True:
            t = dt.datetime(i // 12, i % 12 + 1, 1, tzinfo=t0.tzinfo)
            if t > t1:
                break
            label = str(t.year) if months >= 12 else \
                f"{MONTHS[t.month - 1]} {t.year}" if t.month == 1 or not out else MONTHS[t.month - 1]
            out.append((t, label))
            i += months
        return out
    if span >= 2:
        days = next(d for d in (1, 2, 7, 14, 30) if span / d <= 8)
        t = dt.datetime(t0.year, t0.month, t0.day, tzinfo=t0.tzinfo)
        t += dt.timedelta(days=1) if t < t0 else dt.timedelta(0)
        while t <= t1:
            out.append((t, f"{MONTHS[t.month - 1]} {t.day}"))
            t += dt.timedelta(days=days)
        return out
    minutes = next((m for m in (1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720) if span * 1440 / m <= 8), 1440)
    t = dt.datetime(t0.year, t0.month, t0.day, t0.hour, t0.minute, tzinfo=t0.tzinfo)
    while t < t0 or (t.hour * 60 + t.minute) % minutes:
        t += dt.timedelta(minutes=1)
    while t <= t1:
        out.append((t, t.strftime("%H:%M") if out else f"{MONTHS[t.month - 1]} {t.day} {t:%H:%M}"))
        t += dt.timedelta(minutes=minutes)
    return out


class _Frame:
    """A chart's coordinates: times across, values (or their logarithms) up."""

    def __init__(self, times, lo: float, hi: float, height: int, log: bool = False):
        self.xs = [_x(t) for t in times]
        self.x0, self.x1 = self.xs[0], self.xs[-1] if self.xs[-1] > self.xs[0] else self.xs[0] + 1
        self.lo, self.hi, self.h, self.log = lo, hi, height, log

    def x(self, xv: float) -> float:
        return LEFT + (xv - self.x0) / (self.x1 - self.x0) * (WIDTH - LEFT - RIGHT)

    def y(self, v: float) -> float:
        v = math.log(v) if self.log else v
        return TOP + (self.hi - v) / (self.hi - self.lo) * (self.h - TOP - BOTTOM)

    def path(self, ys) -> str:
        """An SVG path through the points, thinned to the highest and lowest point of each pixel column pair."""
        idx = _thin(self.xs, ys, (WIDTH - LEFT - RIGHT) // 2)
        return "M" + " L".join(f"{self.x(self.xs[i]):.1f},{self.y(ys[i]):.1f}" for i in idx)


def _thin(xs, ys, buckets: int) -> list[int]:
    """Indexes to draw: all of them when few, else the first, lowest, highest and last of each of `buckets` slices of
    time, so a drawdown or a spike is never smoothed away."""
    n = len(xs)
    if n <= 4 * buckets:
        return list(range(n))
    x0, span = xs[0], (xs[-1] - xs[0]) or 1.0
    keep, start = set(), 0
    for b in range(buckets):
        end = bisect.bisect_right(xs, x0 + span * (b + 1) / buckets, lo=start) if b < buckets - 1 else n
        if end > start:
            part = range(start, end)
            keep.update((start, end - 1, min(part, key=ys.__getitem__), max(part, key=ys.__getitem__)))
        start = end
    return sorted(keep)


def _axes(f: _Frame, yticks: list[tuple[float, str]], times) -> list[str]:
    out = ["<g class=\"ts-grid\">"]
    for v, _ in yticks:
        out.append(f"<line x1=\"{LEFT}\" x2=\"{WIDTH - RIGHT}\" y1=\"{f.y(v):.1f}\" y2=\"{f.y(v):.1f}\"/>")
    out.append("</g><g>")
    for v, label in yticks:
        out.append(f"<text x=\"{LEFT - 8}\" y=\"{f.y(v) + 4:.1f}\" text-anchor=\"end\">{_num(label)}</text>")
    for t, label in _time_ticks(times[0], times[-1]):
        x = f.x(_x(t))
        if LEFT <= x <= WIDTH - RIGHT:
            out.append(f"<text x=\"{x:.1f}\" y=\"{f.h - 8}\" text-anchor=\"middle\">{_e(label)}</text>")
    out.append("</g>")
    return out


def _hits(f: _Frame, times, titles) -> str:
    """Strips across the chart, each showing (as its tooltip) the values of the last bar in it."""
    plot = WIDTH - LEFT - RIGHT
    k = min(len(times), STRIPS)
    px = [f.x(x) for x in f.xs]
    strips = []
    for j in range(k):
        right = LEFT + plot * (j + 1) / k
        i = max(0, bisect.bisect_right(px, right + 1e-6) - 1)
        if strips and strips[-1][1] == i:
            strips[-1][2] = right
        else:
            strips.append([LEFT + plot * j / k if not strips else strips[-1][2], i, right])
    return "<g class=\"ts-hits\">" + "".join(
        f"<rect x=\"{a:.1f}\" y=\"{TOP}\" width=\"{b - a:.1f}\" height=\"{f.h - TOP - BOTTOM}\"><title>"
        f"{titles(i)}</title></rect>" for a, i, b in strips) + "</g>"


def _drawdowns(values) -> list[float]:
    out, peak = [], values[0]
    for v in values:
        peak = max(peak, v)
        out.append(v / peak - 1)
    return out


def _titles(s: dict, intraday: bool):
    times, values, bench = s["times"], s["values"], s["benchmark"]
    dd = _drawdowns(values)

    def title(i: int) -> str:
        parts = [f"{_when(times[i], intraday)}: strategy {_num(_pct(values[i] / values[0] - 1))}"]
        if bench:
            parts.append(f"buy and hold {_num(_pct(bench[i] / bench[0] - 1))}")
        parts.append(f"drawdown {_num(_pct(dd[i]))}")
        return ", ".join(parts)
    return title


def _growth(s: dict, intraday: bool) -> str:
    times, values, bench = s["times"], s["values"], s["benchmark"]
    n = s["numbers"]
    growth = [v / values[0] for v in values]
    other = [b / bench[0] for b in bench] if bench else []
    every = growth + other
    glo, ghi = min(min(every), 1.0), max(max(every), 1.0)
    log = s["log"] if s["log"] is not None else ghi / glo >= 10
    if log:
        pad = (math.log(ghi) - math.log(glo)) * 0.05 or 0.05
        lo, hi = math.log(glo) - pad, math.log(ghi) + pad
        ticks = [(g, _tick_pct(g - 1, 1)) for g in _log_ticks(math.exp(lo), math.exp(hi))]
    else:
        a, b, vals, step = _nice(glo - 1, ghi - 1 if ghi > glo else glo - 1 + 0.01)
        lo, hi = a + 1, b + 1
        ticks = [(1 + v, _tick_pct(v, step)) for v in vals]
    f = _Frame(times, lo, hi, GROWTH_HEIGHT, log)
    svg = [f"<svg class=\"ts-chart\" viewBox=\"0 0 {WIDTH} {GROWTH_HEIGHT}\" role=\"img\" aria-label=\""
           f"Growth of the account from {_e(_when(times[0], intraday))} to {_e(_when(times[-1], intraday))}: "
           f"strategy {_num(_pct(n['total_return']))}"
           + (f", buy and hold {_num(_pct(n['benchmark_return']))}" if bench else "") + "\">"]
    svg += _axes(f, ticks, times)
    svg.append(f"<line class=\"ts-zero\" x1=\"{LEFT}\" x2=\"{WIDTH - RIGHT}\" y1=\"{f.y(1.0):.1f}\" "
               f"y2=\"{f.y(1.0):.1f}\"/>")
    if bench:
        svg.append(f"<path class=\"ts-line ts-benchmark\" d=\"{f.path(other)}\"/>")
    svg.append(f"<path class=\"ts-line ts-strategy\" d=\"{f.path(growth)}\"/>")
    svg.append(f"<circle class=\"ts-dot ts-strategy\" cx=\"{f.x(f.xs[-1]):.1f}\" cy=\"{f.y(growth[-1]):.1f}\" "
               "r=\"4\"/>")
    svg.append(_hits(f, times, _titles(s, intraday)))
    svg.append("</svg>")
    legend = (f"<div class=ts-legend><span><i class=ts-key></i>Strategy <b>{_num(_pct(n['total_return']))}</b></span>"
              + (f"<span><i class='ts-key ts-benchmark'></i>Buy and hold <b>{_num(_pct(n['benchmark_return']))}</b>"
                 "</span>" if bench else "<span class=ts-src>no benchmark to compare with</span>") + "</div>")
    return (f"<h3>Growth of the account{', against buy and hold' if bench else ''}"
            f"{' (log scale)' if log else ''}</h3>{legend}<div class=ts-wide>{''.join(svg)}</div>")


def _drawdown(s: dict, intraday: bool) -> str:
    times, values = s["times"], s["values"]
    dd = _drawdowns(values)
    worst = min(range(len(dd)), key=dd.__getitem__)
    a, _, vals, step = _nice(min(dd[worst], -0.001), 0.0, 4)
    f = _Frame(times, a, 0.0, DRAWDOWN_HEIGHT)
    svg = [f"<svg class=\"ts-chart\" viewBox=\"0 0 {WIDTH} {DRAWDOWN_HEIGHT}\" role=\"img\" aria-label=\"Drawdown "
           f"below the running peak; the deepest {_num(_pct(dd[worst]))} on {_e(_when(times[worst], intraday))}\">"]
    svg += _axes(f, [(v, _tick_pct(v, step)) for v in vals], times)
    line = f.path(dd)
    base = f"{f.y(0.0):.1f}"
    svg.append(f"<path class=\"ts-area\" d=\"{line} L{f.x(f.xs[-1]):.1f},{base} L{f.x(f.xs[0]):.1f},{base} Z\"/>")
    svg.append(f"<line class=\"ts-zero\" x1=\"{LEFT}\" x2=\"{WIDTH - RIGHT}\" y1=\"{base}\" y2=\"{base}\"/>")
    svg.append(f"<path class=\"ts-line ts-dd\" d=\"{line}\"/>")
    if dd[worst] < 0:
        x, y = f.x(f.xs[worst]), f.y(dd[worst])
        left = x > WIDTH - RIGHT - 90
        svg.append(f"<circle class=\"ts-dot ts-dd\" cx=\"{x:.1f}\" cy=\"{y:.1f}\" r=\"4\"/>"
                   f"<text class=\"ts-label\" x=\"{x - 9 if left else x + 9:.1f}\" y=\"{y + 4:.1f}\" "
                   f"text-anchor=\"{'end' if left else 'start'}\">{_num(_pct(dd[worst]))}</text>")
    svg.append(_hits(f, times, _titles(s, intraday)))
    svg.append("</svg>")
    return ("<h3>Drawdown: how far below its last peak the account was</h3>"
            "<div class=ts-legend><span><i class='ts-key ts-dd'></i>Drawdown, deepest "
            f"<b>{_num(_pct(dd[worst]))}</b></span></div><div class=ts-wide>" + "".join(svg) + "</div>")


# ---- tables -------------------------------------------------------------------------------------------------------

def shade(r: float) -> str:
    """The monthly table's class for a return: ts-z when it shows as 0.0%, ts-u1..3 up, ts-d1..3 down (BINS)."""
    if abs(r) < 0.0005:
        return "ts-z"
    level = 1 + sum(abs(r) >= b for b in BINS)
    return f"ts-{'u' if r > 0 else 'd'}{level}"


def _months(s: dict) -> str:
    m, bm = s["monthly"], s["benchmark_monthly"]
    years = sorted(m["years"])
    head = "<tr><td></td>" + "".join(f"<th scope=col>{x}</th>" for x in MONTHS) + "<th scope=col>Year</th>" \
        + ("<th scope=col title='The benchmark, bought and held over the same rows'>Buy and hold</th>" if bm else "") \
        + "</tr>"
    rows = []
    for y in years:
        cells = []
        for mo in range(1, 13):
            r = m["months"].get((y, mo))
            cells.append("<td class=ts-none></td>" if r is None else
                         f"<td class={shade(r)} title='{MONTH_NAMES[mo - 1]} {y}: {_num(_pct(r, 2))}'>"
                         f"{_num(_cell(r))}</td>")
        r = m["years"][y]
        cells.append(f"<td class='ts-year {shade(r)}' title='{y}: {_num(_pct(r, 2))}'>{_num(_cell(r))}</td>")
        if bm:
            b = bm["years"].get(y)
            cells.append("<td></td>" if b is None else f"<td title='Buy and hold, {y}: {_num(_pct(b, 2))}'>"
                         f"{_num(_pct(b))}</td>")
        rows.append(f"<tr><th scope=row>{y}</th>{''.join(cells)}</tr>")
    lo, hi = (f"{b * 100:g}%" for b in BINS)
    scale = [("ts-d3", f"-{hi} or less"), ("ts-d2", f"-{lo} to -{hi}"), ("ts-d1", f"0 to -{lo}"), ("ts-z", "0.0%"),
             ("ts-u1", f"0 to +{lo}"), ("ts-u2", f"+{lo} to +{hi}"), ("ts-u3", f"+{hi} or more")]
    return ("<h3>Monthly returns</h3><div class=scroll><table class=ts-months>" + head + "".join(rows)
            + "</table></div><div class=ts-scale>"
            + "".join(f"<span><i class={c}></i>{_num(label)}</span>" for c, label in scale)
            + "<span>Each month: its last equity over the last equity of the month before. Hover a cell for two "
              "decimals.</span></div>")


def _cell(r: float) -> str:
    """A return in the monthly table: one decimal, 0.0% without a sign when it rounds to nothing."""
    return "0.0%" if shade(r) == "ts-z" else _pct(r)


def _trades(s: dict) -> str:
    n = s["numbers"]
    if not s["has_trades"]:
        return ("<h3>Trades</h3><p class=ts-src>No trades.csv next to equity.csv, so there are no trade numbers. Write "
                "it with the backtest (docs/trading.md has the format) to see them.</p>")
    profits, held = s["profits"], s["held"]
    if not profits:
        return ("<h3>Trades</h3><p class=ts-src>" + ("The strategy never traded." if not n["fills"] else
                f"{n['fills']:,} fills and no round trip: no position was ever reduced or closed.") + "</p>")
    wins, losses = [p for p in profits if p > 0], [p for p in profits if p < 0]
    rows = [("Round trips", f"{len(profits):,}: {len(wins):,} won, {len(losses):,} lost"
             + (f", {len(profits) - len(wins) - len(losses):,} even" if len(wins) + len(losses) < len(profits) else ""),
             HEADLINE["Trading"][0][3]),
            ("Largest win", fmt(max(wins), "signed_money") if wins else "-", TRADE_ROWS["Largest win"]),
            ("Largest loss", fmt(min(losses), "signed_money") if losses else "-", TRADE_ROWS["Largest loss"]),
            ("Average win", fmt(statistics.fmean(wins), "signed_money") if wins else "-", TRADE_ROWS["Average win"]),
            ("Average loss", fmt(statistics.fmean(losses), "signed_money") if losses else "-",
             TRADE_ROWS["Average loss"]),
            ("Average trade", fmt(n["average_trade"], "signed_money"), TRADE_ROWS["Average trade"])]
    if held:
        rows.append(("Holding time", f"{duration(statistics.fmean(held))} on average, median "
                     f"{duration(statistics.median(held))}, longest {duration(max(held))}", TRADE_ROWS["Holding time"]))
    if n["open_positions"]:
        rows.append(("Still open", f"{n['open_positions']:,} symbol{'s' if n['open_positions'] > 1 else ''} at the end",
                     TRADE_ROWS["Still open"]))
    return ("<h3>Trades</h3><div class=scroll><table class=ts-trades>"
            + "".join(f"<tr><th scope=row title='{_e(what)}'>{_e(k)}</th><td>{_num(v)}</td></tr>"
                      for k, v, what in rows)
            + "</table></div>")


# ---- pages --------------------------------------------------------------------------------------------------------

def html_section(report: dict, run_dir) -> str:
    """The tear sheets of a run's steps, for its HTML report ("" when no step has one)."""
    sheets = for_report(report, run_dir)
    return "".join(fragment(s, f"Strategy: {label}", style=i == 0) for i, (label, s) in enumerate(sheets))


def page(sheets: list[tuple[str, dict]], title: str = "Tear sheet", intro: str = "") -> str:
    """A whole page of tear sheets: self-contained, no script, nothing fetched."""
    from .. import report as R
    body = [f"<h1>{_e(title)}</h1>"] + ([f"<div class=muted>{_e(intro)}</div>"] if intro else [])
    body += [fragment(s, label, style=i == 0) if len(sheets) > 1 or not intro else
             f"<h2 class=vh>{_e(label)}</h2>" + fragment(s, "", style=i == 0)     # the outline goes h1, h2, h3
             for i, (label, s) in enumerate(sheets)]
    return R.page(title, "\n".join(body))


def run_page(report: dict, run_dir) -> tuple[str, list[tuple[str, dict]]]:
    """(the tear sheet page of a run, its sheets): `testbench report RUN --tearsheet`."""
    sheets = for_report(report, run_dir)
    run = report.get("run_id")
    intro = f"Plan {report.get('plan')}, run {run}" + (f", step {sheets[0][0]}" if len(sheets) == 1 else "") + "."
    return page(sheets, f"Tear sheet: {report.get('plan')} {run}", intro), sheets


# ---- command line -------------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m agent_testbench.trading.tearsheet",
                                description="testbench's tear sheet of a backtest: one self-contained HTML page made "
                                            "from its standard files (docs/trading.md)")
    p.add_argument("source", help="a folder with equity.csv (and trades.csv, prices.csv next to it), or equity.csv")
    p.add_argument("-o", "--output", metavar="HTML", default="tearsheet.html", help="the page (default tearsheet.html)")
    p.add_argument("--risk-free", type=float, default=0.0, metavar="RATE",
                   help="yearly risk-free rate as a fraction, e.g. 0.04 (default 0)")
    p.add_argument("--calendar", choices=("auto", *M.CALENDARS), default="auto",
                   help="daily and intraday bars: stocks (252 days a year) or crypto (365); auto reads the weekends")
    p.add_argument("--periods-per-year", type=float, metavar="N", help="bars per year, overriding the guess")
    p.add_argument("--symbol", help="prices.csv's benchmark symbol, when it has several")
    scale = p.add_mutually_exclusive_group()
    scale.add_argument("--log", dest="log", action="store_true", default=None, help="the growth chart on a log scale")
    scale.add_argument("--linear", dest="log", action="store_false", default=None,
                       help="the growth chart on a linear scale")
    a = p.parse_args(argv)
    if not Path(a.source).exists():
        print(f"error: {a.source}: no such file or folder", file=sys.stderr)
        return 2
    sheets = for_folder(a.source, risk_free=a.risk_free, periods_per_year=a.periods_per_year, calendar=a.calendar,
                        symbol=a.symbol, log=a.log)
    if not sheets:
        print(f"error: {a.source} has no equity.csv with a header of time and equity (docs/trading.md has the format)",
              file=sys.stderr)
        return 2
    Path(a.output).parent.mkdir(parents=True, exist_ok=True)
    Path(a.output).write_text(page(sheets, "Tear sheet", f"From {a.source}."), encoding="utf-8")
    for label, s in sheets:
        if not s["ok"]:
            print(f"{label}: no tear sheet: {s['note']}", file=sys.stderr)
    print(f"wrote {a.output}")
    return 0 if any(s["ok"] for _, s in sheets) else 2


if __name__ == "__main__":
    sys.exit(main())
