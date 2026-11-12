"""The trading pack's check: `strategy:` under a step's expect: testbench's own numbers for a backtest, read from the
standard files it wrote (files.py) by the trading metric reader, with rules on them and guardrails built in. The
strategy never grades itself, and a good number from a backtest that charged nothing or hardly traded is no proof.

    expect:
      strategy:
        equity: out/equity.csv             # the default; trades.csv and prices.csv are read next to it when there
        options: {calendar: stocks}        # the trading reader's: risk_free, periods_per_year, calendar, symbol
        sharpe: ">= 1"                     # a rule on any of trading.metrics.KEYS
        max_drawdown: ">= -0.25"
        beats: buy_and_hold                # excess_return > 0, against the benchmark column or prices.csv
        min_trades: 30                     # default 20; 0 turns this guardrail off
        costs: required                    # the default: some fee or slippage was charged; `none` allows zero costs

`strategy: out/run/equity.csv` is short for {equity: out/run/equity.csv}. Its findings, each named
`strategy EQUITY: ...`:

    the standard files     the files read, the bars and span, and every note of trading.metrics; it passes once they
                           are read, and fails with why not (missing, refused, or last written before the step
                           started), and then every finding below fails as not checked
    KEY RULE               each rule: the number first, then the bars, periods a year and span it was measured over,
                           and the notes that name it; a number that is not defined (null) fails with the note on why
    beats buy and hold     excess_return > 0; with no benchmark it fails, saying how to give one
    trades >= N            the round trips, at least min_trades (20 unless set): a number from few trades means little
    costs modelled         some fee or slippage above 0 in trades.csv; with `costs: none` zero costs pass, as a
                           "costs not modelled (costs: none)" line that says so on the report

This module is a layer: the core registers it by name (expect.STRATEGY) and imports it when checks are first used. It
imports trading.metrics only when it validates or checks a strategy, so a plan without one never loads the pack.
"""
from __future__ import annotations

import re

from ..config import ConfigError, counted, near
from ..expect import Check, compare_shown, notes_about, number_findings, number_rule_error, read_numbers

DEFAULT_EQUITY = "out/equity.csv"
MIN_TRADES = 20
SETTINGS = ("equity", "options", "beats", "min_trades", "costs")        # what strategy: takes besides its rules
BEATS = ("buy_and_hold",)
COSTS = ("required", "none")
EXAMPLE = '{equity: out/equity.csv, sharpe: ">= 1", max_drawdown: ">= -0.25", beats: buy_and_hold}'
BENCHMARK_KEYS = ("benchmark_return", "benchmark_sharpe", "excess_return")
FILES = "the standard files"
BAR_KIND = re.compile(r"^periods per year: \S+ - ([^;(]+)")          # trading.metrics' first note: "daily bars"


def numbers() -> tuple[str, ...]:
    """The names of testbench's numbers for a backtest, each of which strategy: takes a rule on."""
    from . import metrics as TM
    return TM.KEYS


class StrategyCheck(Check):
    name = "strategy"
    example = {"equity": DEFAULT_EQUITY, "sharpe": ">= 1", "max_drawdown": ">= -0.25", "beats": "buy_and_hold",
               "min_trades": 30, "costs": "required"}

    @property
    def keys(self) -> tuple[str, ...]:
        return (*SETTINGS, *numbers())

    def validate(self, where, value):
        spec, known = _spec(where, value), numbers()
        for key, v in spec.items():
            at = f"{where}.{key}"
            if key == "equity":
                if not isinstance(v, str) or not v.strip():
                    raise ConfigError(f"`{at}` is the path of the equity.csv the backtest writes, e.g. "
                                      f"{DEFAULT_EQUITY} (trades.csv and prices.csv are read next to it)")
            elif key == "options":
                if not isinstance(v, dict) or "files" in v:
                    raise ConfigError(f"`{at}` is a mapping of the trading reader's options, e.g. {{calendar: "
                                      "crypto, risk_free: 0.04}; the files are equity.csv and the ones next to it")
            elif key == "beats":
                if v not in BEATS:
                    raise ConfigError(f"`{at}` is buy_and_hold: the strategy's total return must beat buy and hold "
                                      "over the same bars - the benchmark column of equity.csv, or prices.csv next "
                                      "to it (options: {symbol: SPY} picks one of its symbols)")
            elif key == "min_trades":
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    raise ConfigError(f"`{at}` is the fewest round trips the numbers may come from, a whole number "
                                      f"such as 30 (default {MIN_TRADES}; 0 turns this off)")
            elif key == "costs":
                if v not in COSTS:
                    raise ConfigError(f"`{at}` is required (the default: some fee or slippage must be charged) or "
                                      "none (zero costs are allowed, and the report says costs were not modelled)")
            elif key in known:
                bad = number_rule_error(v)
                if bad:
                    path = isinstance(v, str) and v.strip().lower().endswith(".csv")
                    raise ConfigError(f"`{at}`: {bad}" + (f". `{key}:` here is a rule on testbench's number {key}; "
                                                          "trades.csv and prices.csv are read from the folder "
                                                          "equity.csv is in" if path else ""))
            else:
                raise ConfigError(f"`{where}` has no {key!r}{near(key, [*SETTINGS, *known])}; it takes "
                                  f"{', '.join(SETTINGS)} and a rule on any of testbench's numbers for a backtest: "
                                  f"{', '.join(known)} (docs/trading.md#the-numbers)")
        from .. import metrics as M
        try:
            M.options("trading", {**(spec.get("options") or {}), "files": {"equity": spec.get("equity")}})
        except ValueError as exc:
            raise ConfigError(f"`{where}.options`: {exc}") from None

    def check(self, value, *, workdir, result, add):
        spec = _spec("strategy", value)
        equity = spec.get("equity") or DEFAULT_EQUITY
        label = f"strategy {equity}"
        rules = {k: v for k, v in spec.items() if k not in SETTINGS}
        min_trades = spec.get("min_trades", MIN_TRADES)
        guards = (["beats buy and hold"] if spec.get("beats") else []) + \
            ([f"trades >= {min_trades} (min_trades)"] if min_trades else []) + ["costs modelled"]
        got, why = read_numbers("trading", workdir, result, {"equity": equity}, spec.get("options"))
        if got is None:
            add(f"{label}: {FILES}", False, why)
            later = f"not checked: {FILES} could not be read (the finding above says why)"
            number_findings(label, None, later, rules, add)
            for guard in guards:
                add(f"{label}: {guard}", False, later)
            return
        values, notes, files = got["values"], got["notes"], got["files"]
        context = _context(values, notes)
        trips = values.get("trades")
        add(f"{label}: {FILES}", True,
            f"read {', '.join(files.values())}: {counted(values.get('bars'), 'bar')}; {context}"
            + (f"; {counted(trips, 'round trip')}" if trips is not None else "; no trades.csv next to it")
            + (f"; notes: {'; '.join(notes)}" if notes else ""))
        number_findings(label, got, "", rules, add, about=_about, context=context,
                        remarks=lambda key, said: [n for n in _about(key, said) if not BAR_KIND.match(n)])
        if spec.get("beats"):
            _beats(f"{label}: beats buy and hold", values, notes, files, equity, context, add)
        if min_trades:
            _min_trades(f"{label}: trades >= {min_trades} (min_trades)", min_trades, values, files, equity, add)
        _costs(label, spec.get("costs", "required") == "none", values, files, equity, add)


def _spec(where: str, value) -> dict:
    """strategy:'s value as a mapping: null is every default, a path is the equity file's."""
    if value is None:
        return {}
    if isinstance(value, str) and value.strip():
        return {"equity": value}
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise ConfigError(f"`{where}` has {key!r} where a name goes, e.g. {EXAMPLE}")
        return dict(value)
    raise ConfigError(f"`{where}` is a mapping such as {EXAMPLE}, or the path of the equity.csv the backtest writes")


def _span(days) -> str:
    if not isinstance(days, (int, float)):
        return "an unknown span"
    if days >= 365.25:
        return counted(round(days / 365.25, 2), "year")
    return counted(round(days), "day") if days >= 2 else counted(round(days * 24, 1), "hour")


def _context(values: dict, notes: list[str]) -> str:
    """What a yearly number was measured over, in a few words: "daily bars, 252 a year; 3 years"."""
    ppy = values.get("periods_per_year")
    kind = next((m.group(1).strip() for m in map(BAR_KIND.match, notes) if m), None)
    per_year = f"{ppy:g}" if isinstance(ppy, (int, float)) else "?"
    head = f"{kind}, {per_year} a year" if kind else f"{per_year} bars a year (periods_per_year, as given)"
    return f"{head}; {_span(values.get('days'))}"


def _about(key: str, notes: list[str]) -> list[str]:
    """The notes on one number: those that name it, and for the benchmark's numbers those on the benchmark."""
    said = notes_about(key, notes)
    if key in BENCHMARK_KEYS:
        said += [n for n in notes if "benchmark" in n and n not in said]
    return said


def _beats(name: str, values: dict, notes: list[str], files: dict, equity: str, context: str, add) -> None:
    excess = values.get("excess_return")
    if excess is None:
        said = [n for n in notes if "benchmark" in n] if "prices" in files else []
        add(name, False, "no benchmark to beat: " + ("; ".join(said) if said else
                                                     f"add a benchmark column to {equity} (a buy-and-hold price or "
                                                     "equity at the same times), or write prices.csv next to it "
                                                     "(time, symbol, close; options: {symbol: SPY} picks one when it "
                                                     "has several)"))
        return
    ok, detail = compare_shown(excess, "> 0")
    source = f"closes in {files['prices']}" if "prices" in files else f"the benchmark column of {files['equity']}"
    add(name, ok, f"{detail}; excess_return: the total return {_g(values.get('total_return'))} against buy and "
                  f"hold's {_g(values.get('benchmark_return'))} ({source}); {context}")


def _g(x) -> str:
    return f"{x:.4g}" if isinstance(x, (int, float)) and not isinstance(x, bool) else "null"


def _min_trades(name: str, least: int, values: dict, files: dict, equity: str, add) -> None:
    trips = values.get("trades")
    if trips is None:
        add(name, False, f"no trades.csv next to {equity}: testbench counts the round trips from it (one row per "
                         "fill: time, symbol, side, quantity, price, fee; "
                         "docs/trading.md#tradescsv---one-row-per-fill)")
        return
    ok, detail = compare_shown(trips, f">= {least}")
    add(name, ok, f"{detail}; round trips in {files['trades']}, from {counted(values.get('fills'), 'fill')}"
                  + ("" if ok else "; a number from this few trades means little: backtest over a longer span or more "
                                   "symbols, or set min_trades knowingly (0 turns this off)"))


def _costs(label: str, allowed: bool, values: dict, files: dict, equity: str, add) -> None:
    modelled, trades = values.get("costs_modeled"), files.get("trades")
    if modelled:
        fees = values.get("total_fees") or 0
        add(f"{label}: costs modelled", True, f"true: {counted(values.get('fills'), 'fill')} paid {fees:,.2f} in fees"
                                              + ("" if fees else ", and slippage was recorded"))
        return
    if trades is None:
        what = f"no trades.csv next to {equity}, so testbench cannot see whether fees or slippage were charged"
    elif modelled is None:
        what = f"{trades} has no fills: the strategy never traded, so no costs were charged or seen"
    else:
        what = (f"every fee and slippage in {trades} is 0: the backtest charged no trading costs, so its returns are "
                "better than an account would get")
    if allowed:
        add(f"{label}: costs not modelled (costs: none)", True, f"warning: {what}; `costs: none` allows it")
    else:
        add(f"{label}: costs modelled", False, ("false: " if modelled is False else "") + what
            + ". Charge fees or slippage in the backtest (trades.csv's fee and slippage columns), or write "
              "`costs: none` to allow zero costs - the report then says costs were not modelled")
