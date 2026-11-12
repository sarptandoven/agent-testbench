"""The standard files of the trading pack: what any backtest writes, so that testbench computes the numbers, not the
strategy that is being graded. docs/trading.md has the same definitions and snippets for each framework.

trades.csv - one row per fill (a partial fill is a row of its own), in time order (equal times are fine):
    time      required  ISO 8601, see below
    symbol    required  the instrument, as the backtest names it
    side      required  buy or sell, in any case; opening a short is a sell, covering it a buy
    quantity  required  above 0: the size without a sign (shares, contracts, coins); the direction is in side
    price     required  above 0: the fill price, slippage included
    fee       required  0 or more: the fill's commissions and fees in the account currency (0 when none were charged)
    order_id  optional  text
    strategy  optional  text
    slippage  optional  0 or more: what slippage cost on this fill, in the account currency. It is already in price,
                        so it is only recorded (it tells testbench that slippage was modeled), never taken off again

equity.csv - one row per bar (or per day), times strictly increasing:
    time            required
    equity          required  above 0: the account's whole value (cash plus open positions) at that time
    cash            optional  any real number (below 0 on margin)
    position_value  optional  any real number: the value of the open positions (below 0 when short, 0 when flat)
    benchmark       optional  above 0: a buy-and-hold price or equity series at the same times

prices.csv - optional, closes for a benchmark; rows of several symbols may be mixed, each symbol's times increasing:
    time, symbol, close (above 0)

Rules for all three:
- The first line is the header. Column names match whatever their case and surrounding spaces; other columns are
  allowed and ignored. Blank lines are skipped. The files are UTF-8 (a byte order mark is fine), comma-separated.
- Numbers are plain (1234.5, -3, 1e-3): never empty, NaN or infinite. A numeric optional column, once there, has a
  value on every row (leave the column out when some rows have none); text columns may be empty.
- Times are ISO 8601: 2024-01-02, 2024-01-02T15:30:00, 2024-01-02 15:30:00.250, 2024-01-02T15:30:00Z,
  2024-01-02T15:30:00+02:00. Either every time in a file has a zone (Z or an offset) or none has, and files read
  together (equity.csv with trades.csv and prices.csv) follow the same convention, so their times compare.

A file that breaks a rule is refused with a TradingFileError whose message names the file, the line and the column
and says what to write instead: its reader is often an agent fixing its strategy's output from the message alone.
Up to MAX_PROBLEMS problems are listed at once.

Readers take a path, a pandas DataFrame (its DatetimeIndex, or an index named time, is the time column when there is
no time column; a Series is read as equity), rows as dicts or as tuples in column order, or the records a reader
returned (checked again). pandas is never imported here: a DataFrame is recognised by its type's module. Writers
(write_trades, write_equity, write_prices) take the same rows, check them by the same rules and write the standard
file; a key or value of None counts as no value.
"""
from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import math
import re
from pathlib import Path

MAX_PROBLEMS = 10
FILE = {"trades": "trades.csv", "equity": "equity.csv", "prices": "prices.csv"}
REQUIRED = {"trades": ("time", "symbol", "side", "quantity", "price", "fee"),
            "equity": ("time", "equity"),
            "prices": ("time", "symbol", "close")}
OPTIONAL = {"trades": ("order_id", "strategy", "slippage"),
            "equity": ("cash", "position_value", "benchmark"),
            "prices": ()}
TEXT = ("symbol", "order_id", "strategy")          # empty is fine except for symbol
SIDES = ("buy", "sell")
# column: (rule, why the rule) for the numeric columns; rule is ">0", ">=0" or "any"
NUMBERS = {
    "quantity": (">0", "write the size without a sign and put the direction in side (a sale or a short is a sell)"),
    "price": (">0", "a fill price is above 0"),
    "fee": (">=0", "a fee is a cost: 0 or more, in the account currency (0 when the backtest charged none)"),
    "slippage": (">=0", "slippage is a cost: 0 or more, in the account currency"),
    "equity": (">0", "equity is the account's whole value; at 0 or below the account is wiped out, so end the file "
                     "at the last bar above 0"),
    "cash": ("any", ""),
    "position_value": ("any", ""),
    "benchmark": (">0", "a benchmark is a price or an equity value, above 0"),
    "close": (">0", "a close price is above 0"),
}
TIME = re.compile(r"(\d{4})-(\d\d)-(\d\d)(?:[Tt ](\d\d):(\d\d)(?::(\d\d)(?:[.,](\d{1,9}))?)?"
                  r" ?([Zz]|[+-]\d\d(?::?\d\d)?)?)?")
EPOCH = {range(9, 11): ("seconds", "s", ""), range(12, 14): ("milliseconds", "ms", " / 1000"),
         range(15, 17): ("microseconds", "us", " / 1e6"), range(18, 20): ("nanoseconds", "ns", " / 1e9")}
ISO_EXAMPLE = "2024-01-02, 2024-01-02T15:30:00 or 2024-01-02T15:30:00Z"


class TradingFileError(ValueError):
    """A trading file (or rows) testbench refuses. str() is the whole message; .problems has one line per problem."""

    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or [message]


@dataclasses.dataclass(frozen=True, slots=True)
class Fill:
    """One row of trades.csv."""
    time: dt.datetime
    symbol: str
    side: str                     # "buy" or "sell"
    quantity: float
    price: float
    fee: float
    order_id: str = ""
    strategy: str = ""
    slippage: float | None = None

    @property
    def signed_quantity(self) -> float:
        return self.quantity if self.side == "buy" else -self.quantity


@dataclasses.dataclass(frozen=True, slots=True)
class Bar:
    """One row of equity.csv."""
    time: dt.datetime
    equity: float
    cash: float | None = None
    position_value: float | None = None
    benchmark: float | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class Close:
    """One row of prices.csv."""
    time: dt.datetime
    symbol: str
    close: float


class _Bad(ValueError):
    """One value refused; the message says why, without the location."""


# ---- reading -----------------------------------------------------------------------------------------------------

def read_trades(src, name: str | None = None) -> list[Fill]:
    """trades.csv (or a DataFrame, rows, or Fills) as Fills in file order. A file with only the header is no fills."""
    return _read("trades", src, name)


def read_equity(src, name: str | None = None) -> list[Bar]:
    """equity.csv (or a DataFrame, a Series of equity, rows, or Bars) as Bars; at least one row."""
    return _read("equity", src, name)


def read_prices(src, name: str | None = None) -> list[Close]:
    """prices.csv (or a DataFrame, rows, or Closes) as Closes in file order; at least one row."""
    return _read("prices", src, name)


def name_of(src, kind: str) -> str:
    """What messages call a source: a file by its path, anything else by what it is."""
    if isinstance(src, (str, Path)):
        return str(src)
    if _is_pandas(src):
        return f"the {type(src).__name__} given for {FILE[kind]}"
    return f"the rows given for {FILE[kind]}"


def _read(kind: str, src, name: str | None) -> list:
    name = name or name_of(src, kind)
    if isinstance(src, (str, Path)):
        header_at, header, rows = _csv_rows(Path(src), name, kind)
    elif _is_pandas(src):
        header_at, header, rows = _frame_rows(src, kind, name)
    else:
        try:
            items = list(src)
        except TypeError:
            raise TypeError(f"{FILE[kind]} is read from a path, a pandas DataFrame or a list of rows, "
                            f"not {type(src).__name__}") from None
        header_at, header, rows = _item_rows(items, kind)
    return _parse(kind, name, header_at, header, rows)


def _is_pandas(obj) -> bool:
    return type(obj).__module__.split(".")[0] == "pandas"


def _csv_rows(path: Path, name: str, kind: str):
    if path.is_dir():                                  # open() says PermissionError for a folder on Windows
        raise TradingFileError(f"{name} is a folder; give the path of the CSV file in it (`python -m "
                               f"agent_testbench.trading.files --check {name}` says what each CSV file there needs)")
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            lines = [(reader.line_num, row) for row in reader]
    except FileNotFoundError:
        raise TradingFileError(f"{name}: no such file. Write it with the backtest first (docs/trading.md has "
                               "snippets for each framework)") from None
    except UnicodeDecodeError as e:
        raise TradingFileError(f"{name} is not UTF-8 text ({e.reason} at byte {e.start}); save it as UTF-8 CSV") \
            from None
    except csv.Error as e:
        raise TradingFileError(f"{name} is not readable as CSV ({e}); write plain comma-separated values") from None
    lines = [(n, row) for n, row in lines if any(c.strip() for c in row)]
    if not lines:
        raise TradingFileError(f"{name} is empty; its first line is the header: {','.join(REQUIRED[kind])}")
    (at, header), body = lines[0], lines[1:]
    return f"line {at} (the header)", header, [(f"line {n}", row) for n, row in body]


def _frame_rows(frame, kind: str, name: str):
    if type(frame).__name__ == "Series":
        if kind != "equity":
            raise TradingFileError(f"{name}: a Series can only be equity (its index the times); give {FILE[kind]} "
                                   "as a DataFrame with columns " + ", ".join(REQUIRED[kind]))
        frame = frame.to_frame("equity")
    columns = [str(c) for c in frame.columns]
    values = [list(row) for row in frame.itertuples(index=False, name=None)]
    index = frame.index
    named = str(getattr(index, "name", None) or "").strip().lower()
    if "time" not in (c.strip().lower() for c in columns) and (
            str(getattr(index, "dtype", "")).startswith("datetime64") or named == "time"):
        columns = ["time"] + columns
        values = [[t] + row for t, row in zip(index, values)]
    rows = [(f"row {i + 1} (iloc {i})", [_cell(v) for v in row]) for i, row in enumerate(values)]
    return "the columns", columns, rows


def _item_rows(items: list, kind: str):
    order = REQUIRED[kind] + OPTIONAL[kind]
    header: list[str] = [] if items else list(REQUIRED[kind])     # no rows: a header alone (no fills is fine)
    dicts = []
    for i, item in enumerate(items):
        if dataclasses.is_dataclass(item) and not isinstance(item, type):
            item = {f.name: getattr(item, f.name) for f in dataclasses.fields(item)}
        if isinstance(item, dict):
            d = {str(k).strip().lower(): v for k, v in item.items()}
        elif isinstance(item, (list, tuple)):
            if len(item) > len(order):
                raise TradingFileError(f"row {i + 1} of the rows given for {FILE[kind]} has {len(item)} values; "
                                       f"a row as a tuple has at most {len(order)}, in this order: {', '.join(order)}")
            d = dict(zip(order, item))
        else:
            raise TypeError(f"row {i + 1} of the rows given for {FILE[kind]} is a {type(item).__name__}; "
                            f"give each row as a dict or a tuple ({', '.join(REQUIRED[kind])})")
        d = {k: v for k, v in d.items() if v is not None}          # None: no value, as an empty cell would be
        header += [k for k in d if k not in header]
        dicts.append(d)
    rows = [(f"row {i + 1}", [_cell(d.get(k)) for k in header]) for i, d in enumerate(dicts)]
    return "the rows' keys", header, rows


def _cell(v) -> str:
    """A value from a DataFrame or a dict as the text a CSV file would hold."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(v)                                  # refused where a number is wanted, as in a file
    if isinstance(v, float):                           # numpy.float64 too: repr of the float, not "np.float64(...)"
        return repr(float(v))
    if hasattr(v, "isoformat"):                        # datetime, date, pandas Timestamp (NaT says "NaT")
        try:
            return v.isoformat()
        except (TypeError, ValueError):
            return str(v)
    return str(v)


def _parse(kind: str, name: str, header_at: str, header: list[str], rows: list) -> list:
    cols = [str(h).strip().lower() for h in header]
    dup = sorted({c for c in cols if c and cols.count(c) > 1})
    if dup:
        raise TradingFileError(f"{name}, {header_at}: column {_q(dup[0])} appears more than once; keep one")
    missing = [c for c in REQUIRED[kind] if c not in cols]
    if missing:
        hint = " (write 0 on rows without a fee)" if "fee" in missing else ""
        timeish = [c for c in cols if c in ("date", "datetime", "timestamp", "day", "dt", "ts", "date_time", "bar")]
        if "time" in missing and "" in cols:
            hint += (". The column with no name may hold the times (pandas writes an unnamed index that way): "
                     "name it, e.g. df.to_csv(path, index_label='time')")
        elif "time" in missing and timeish:
            hint += (f". If {_q(timeish[0])} holds the times, name that column time, with ISO 8601 times in it "
                     "(2024-01-02 or 2024-01-02T15:30:00; a bar number or a Unix timestamp is not a time)")
        raise TradingFileError(
            f"{name}, {header_at}: no {', '.join(_q(c) for c in missing)} column{'s' if len(missing) > 1 else ''}"
            f"{hint}. {FILE[kind]} needs {', '.join(REQUIRED[kind])} (optional: {', '.join(OPTIONAL[kind]) or 'none'});"
            f" found: {', '.join(c for c in cols if c) or 'nothing'}")
    used = [(c, cols.index(c)) for c in REQUIRED[kind] + OPTIONAL[kind] if c in cols]
    problems: list[str] = []
    out = []
    zone_of = None                                     # (has a zone, where the first time was, its text)
    last: dict = {}                                    # ordering key -> (time, where, text)
    for where, cells in rows:
        if len(cells) > len(cols) and not any(c.strip() for c in cells[len(cols):]):
            cells = cells[:len(cols)]                  # trailing empty cells: a trailing comma
        if len(cells) != len(cols):
            problems.append(f"{where}: {len(cells)} value{'s' if len(cells) != 1 else ''} but the header has "
                            f"{len(cols)} columns; every row has one value per column (quote a value with a comma)")
            continue
        values, ok = {}, True
        for col, i in used:
            try:
                values[col] = _value(col, cells[i])
            except _Bad as e:
                problems.append(f"{where}, column {_q(col)}: {e}")
                ok = False
        if "time" in values:
            t, text = values["time"], cells[cols.index("time")].strip()
            aware = t.tzinfo is not None
            zone_of = zone_of or (aware, where, text)
            if aware != zone_of[0]:
                problems.append(f"{where}, column `time`: {text!r} {'has' if aware else 'has no'} time zone but "
                                f"{zone_of[1]}'s {zone_of[2]!r} {'has none' if aware else 'has one'}; write every "
                                "time in the file the same way, all with a zone (Z or +02:00) or all without")
                ok = False
            else:
                key = values.get("symbol") if kind == "prices" else None
                if key in last:
                    before, at, was = last[key]
                    wrong = t < before if kind == "trades" else t <= before
                    if wrong:
                        problems.append(f"{where}, column `time`: {text!r} {_order_problem(kind, key, at, was)}")
                        ok = False
                last[key] = (t, where, text)
        if ok:
            out.append(_record(kind, values))
    if problems:
        shown = problems[:MAX_PROBLEMS]
        if len(problems) == 1:
            message = f"{name}, {problems[0]}"
        else:
            more = f"\n  ... and {len(problems) - len(shown)} more" if len(problems) > len(shown) else ""
            message = f"{name}: {len(problems)} problems\n" + "\n".join(f"  {p}" for p in shown) + more
        raise TradingFileError(message, problems)
    if not out and kind != "trades":
        raise TradingFileError(f"{name} has no rows after the header; write one row per "
                               f"{'bar' if kind == 'equity' else 'close'}")
    return out


def _q(col: str) -> str:
    return f"`{col}`"


def _order_problem(kind: str, symbol, at: str, was: str) -> str:
    if kind == "trades":
        return f"is before {at}'s {was!r}; fills are in time order (sort the rows by time)"
    if kind == "prices":
        return (f"is not after {at}'s {was!r} for {symbol}; each symbol has one close per time, times increasing "
                "(sort by symbol and time, and drop repeated rows)")
    return (f"is not after {at}'s {was!r}; equity.csv has one row per bar with times increasing (a repeated time "
            "usually means two rows for one bar: keep the last)")


def _record(kind: str, v: dict):
    if kind == "trades":
        return Fill(v["time"], v["symbol"], v["side"], v["quantity"], v["price"], v["fee"], v.get("order_id", ""),
                    v.get("strategy", ""), v.get("slippage"))
    if kind == "equity":
        return Bar(v["time"], v["equity"], v.get("cash"), v.get("position_value"), v.get("benchmark"))
    return Close(v["time"], v["symbol"], v["close"])


def _value(col: str, text: str):
    if col == "time":
        return _time(text)
    if col == "side":
        side = text.strip().lower()
        if side not in SIDES:
            raise _Bad(f"{text!r} is not a side; write buy or sell (opening a short is a sell, covering it a buy)"
                       if text.strip() else "is empty; write buy or sell")
        return side
    if col in TEXT:
        if col == "symbol" and not text.strip():
            raise _Bad("is empty; write the instrument's symbol")
        return text.strip()
    return _number(col, text)


def _number(col: str, text: str) -> float:
    rule, why = NUMBERS[col]
    s = text.strip()
    if not s:
        raise _Bad("is empty; write a number" + (" (0 when there was no fee)" if col == "fee" else ""))
    try:
        x = float(s)
    except ValueError:
        hint = (" (no thousands separators; a dot for decimals)" if "," in s
                else " (no currency sign or unit)" if re.search(r"\d", s) else "")
        raise _Bad(f"{s!r} is not a number{hint}") from None
    if math.isnan(x):
        raise _Bad("is NaN (not a number); every value is a real number. A NaN usually comes from a calculation on "
                   "missing data, such as the first bars of a rolling window: drop those rows or fill them")
    if math.isinf(x):
        raise _Bad(f"{s!r} is infinite; every value is a real number (an infinity usually comes from a division by 0)")
    if x < 0 and rule != "any":
        raise _Bad(f"{s} is negative; {why}")
    if x == 0 and rule == ">0":
        raise _Bad(f"{s} is not above 0; {why}")
    return x


def _time(text: str) -> dt.datetime:
    """An ISO 8601 date or date and time, with or without a zone; refused (with what to write) otherwise."""
    s = text.strip()
    m = TIME.fullmatch(s)
    if not m:
        if not s:
            raise _Bad(f"is empty; write an ISO 8601 time, e.g. {ISO_EXAMPLE}")
        digits = re.fullmatch(r"(\d+)(?:\.\d*)?", s)
        for lengths, (unit, short, div) in EPOCH.items():
            if digits and len(digits.group(1)) in lengths:
                raise _Bad(f"{s!r} looks like a Unix timestamp in {unit}; write ISO 8601 instead, e.g. "
                           f"datetime.fromtimestamp(t{div}, timezone.utc).isoformat() or "
                           f"pandas.to_datetime(t, unit='{short}', utc=True)")
        raise _Bad(f"{s!r} is not an ISO 8601 time; write year-month-day first, e.g. {ISO_EXAMPLE}")
    y, mo, d, h, mi, sec, frac, zone = m.groups()
    try:
        tz = None
        if zone:
            if zone in ("Z", "z"):
                tz = dt.timezone.utc
            else:
                digits = zone[1:].replace(":", "")
                minutes = int(digits[:2]) * 60 + int(digits[2:] or 0)
                tz = dt.timezone(dt.timedelta(minutes=minutes if zone[0] == "+" else -minutes))
        return dt.datetime(int(y), int(mo), int(d), int(h or 0), int(mi or 0), int(sec or 0),
                           int((frac or "0").ljust(6, "0")[:6]), tzinfo=tz)
    except ValueError as e:
        raise _Bad(f"{s!r} is not a real date and time ({e}); write e.g. {ISO_EXAMPLE}") from None


def check_same_zones(*sources: tuple[str, list]) -> None:
    """Files read together write their times the same way (all with a zone or all without), so their times compare.
    sources: (name, records) pairs; empty record lists are skipped."""
    seen = [(name, rows[0].time) for name, rows in sources if rows]
    for name, t in seen[1:]:
        if (t.tzinfo is not None) != (seen[0][1].tzinfo is not None):
            first, ft = seen[0]
            raise TradingFileError(
                f"{name} writes times {'with' if t.tzinfo else 'without'} a time zone ({t.isoformat()}) but "
                f"{first} {'with' if ft.tzinfo else 'without'} ({ft.isoformat()}); write the times of all the files "
                "the same way, all with a zone (Z or +02:00) or all without, so they can be compared")


# ---- writing -----------------------------------------------------------------------------------------------------

def write_trades(rows, path) -> Path:
    """Fills (dicts, tuples in column order, a DataFrame or Fills) checked by trades.csv's rules and written to path."""
    return _write("trades", rows, path)


def write_equity(rows, path) -> Path:
    """Bars (dicts, (time, equity) tuples, a DataFrame, a Series of equity or Bars), checked and written to path."""
    return _write("equity", rows, path)


def write_prices(rows, path) -> Path:
    """Closes (dicts, (time, symbol, close) tuples, a DataFrame or Closes), checked and written to path."""
    return _write("prices", rows, path)


def _write(kind: str, rows, path) -> Path:
    if isinstance(rows, (str, Path)):
        raise TypeError(f"write_{kind} takes the rows to write and then the path, not a path as the rows")
    path = Path(path)
    records = _read(kind, rows, f"the rows for {path}")
    cols = list(REQUIRED[kind]) + [c for c in OPTIONAL[kind]
                                   if any(getattr(r, c) not in (None, "") for r in records)]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(cols)
        for r in records:
            w.writerow([_text(getattr(r, c)) for c in cols])
    return path


def _text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, dt.datetime):
        return v.isoformat()
    if isinstance(v, float):
        return repr(v)
    return str(v)


# ---- a backtest's own files: what to rename or add to reach the standard format ----------------------------------
#
#     python -m agent_testbench.trading.files --check out
#
# reads every CSV file in a folder the way a person would - which one is the equity curve, which the fills, which the
# decisions - and says, file by file, what to rename and what to add; a file that already reads is said to. Exit 0
# when every file reads and none is missing, 1 when something needs changing, 2 when there is nothing to read.

SIGNALS = "signals.csv"
SYNONYMS = {       # names backtests give the standard columns, lowercase
    "time": ("date", "datetime", "timestamp", "day", "dt", "ts", "date_time", "bar_time", "time_stamp", "index",
             "period", "bar"),
    "symbol": ("ticker", "asset", "instrument", "code", "pair", "coin", "sym", "security", "contract", "stock"),
    "side": ("direction", "action", "buy_sell", "order_side", "trade_side", "trade_type", "type"),
    "quantity": ("qty", "size", "shares", "amount", "units", "volume", "contracts", "filled_qty", "position_size",
                 "lots", "filled"),
    "price": ("fill_price", "exec_price", "execution_price", "avg_price", "average_price", "px", "trade_price",
              "filled_price", "avg_fill_price"),
    "fee": ("fees", "commission", "commissions", "cost", "costs", "transaction_cost", "transaction_costs",
            "fee_paid", "trading_fee", "trading_fees"),
    "equity": ("value", "portfolio_value", "total_value", "balance", "nav", "account_value", "net_liquidation",
               "capital", "portfolio", "total", "equity_curve", "net_worth", "account"),
    "benchmark": ("buy_and_hold", "buy_hold", "bh", "hold", "benchmark_equity", "benchmark_value"),
    "close": ("adj_close", "adjclose", "adj close", "close_price", "last", "price"),
    "cash": ("free_cash", "cash_balance"),
    "position_value": ("positions_value", "holdings", "holdings_value", "market_value", "exposure"),
}
BY_NAME = (("equity", ("equity", "portfolio", "balance", "nav", "value", "account")),
           ("trades", ("trade", "fill", "order", "transaction", "execution")),
           ("signals", ("signal", "decision", "position", "prediction", "forecast")),
           ("prices", ("price", "ohlc", "bar", "quote", "candle", "close")))
WHAT = {"equity": "the equity curve", "trades": "the fills", "signals": "the decisions", "prices": "prices"}
ADD = {
    "time": "add `time`: when each row happened, as ISO 8601 (2024-01-02, or 2024-01-02T15:30:00)",
    "symbol": "add `symbol`: the instrument of each row (the same value on every row is fine for one instrument)",
    "side": "add `side`: buy or sell for each fill (opening a short is a sell, covering it a buy)",
    "quantity": "add `quantity`: each fill's size, without a sign (shares, contracts, coins)",
    "price": "add `price`: each fill's price, slippage included",
    "fee": ("add `fee`: each fill's commissions and fees in the account currency, 0 when none were charged (a "
            "`strategy:` check with costs: required fails a backtest that charged nothing)"),
    "equity": "add `equity`: the account's whole value (cash plus open positions) at each bar",
    "close": "add `close`: each row's close price",
}
SIDE_WORDS = {"long": "buy", "short": "sell", "b": "buy", "s": "sell", "1": "buy", "-1": "sell", "bought": "buy",
              "sold": "sell", "+1": "buy", "entry": "?", "exit": "?", "open": "?", "close": "?"}


def check_folder(folder) -> tuple[int, list[str]]:
    """(exit code, lines): each CSV file in folder (or one folder down when it holds none) - which standard file it
    looks like, and what to rename or add for it to read as one - then the standard files that are missing."""
    folder = Path(folder)
    if not folder.is_dir():
        what = "a file, not a folder" if folder.exists() else "not there"
        return 2, [f"{folder} is {what}: give the folder the backtest writes its CSV files to (e.g. out)"]
    files = sorted(folder.glob("*.csv")) or sorted(folder.glob("*/*.csv"))
    if not files:
        return 2, [f"{folder} holds no CSV files: run the backtest first, then check the folder it writes to (the "
                   "standard files are equity.csv, trades.csv and signals.csv; docs/trading.md#the-files)"]
    found: dict[str, list] = {}
    lines, changes = [], 0
    for f in files:
        kind, todo, ok = _check_file(f, folder)
        found.setdefault(kind, []).append(f)
        rel = f.relative_to(folder).as_posix()
        if ok:
            lines.append(f"ok   {rel}: {ok}")
        elif todo:
            changes += len(todo)
            lines.append(f"FIX  {rel}" + (f", which looks like {WHAT[kind]}:" if kind else ":"))
            lines += [f"     - {t}" for t in todo]
        else:
            lines.append(f"--   {rel}: not one of the standard files, by its name or its columns (left as it is)")
    for kind, name, why in (("equity", "equity.csv", "time, equity at each bar, and benchmark (buy and hold) for "
                                                     "`beats: buy_and_hold`: what `strategy:` grades"),
                            ("trades", "trades.csv", "time, symbol, side, quantity, price, fee - one row per fill, "
                                                     "next to equity.csv"),
                            ("signals", SIGNALS, "time, symbol, then a column per decision - what the lookahead plan "
                                                 "compares across reruns; write the signal's number, not only the "
                                                 "position it gives")):
        if kind not in found:
            changes += 1
            lines.append(f"ADD  {name}: no file here looks like {WHAT[kind]}; write {name} - {why}")
    if not changes:
        head = (f"{folder}: every file reads in testbench's standard format. `testbench numbers trading "
                f"{(folder / 'equity.csv').as_posix()}` shows the numbers testbench computes from them.")
    else:
        head = (f"{folder}: {changes} change{'s' * (changes != 1)} to reach testbench's standard format "
                "(docs/trading.md#the-files; agent_testbench.trading.files has writers that check each row):")
    return (0 if not changes else 1), [head] + lines


def _check_file(path: Path, folder: Path) -> tuple[str | None, list[str], str]:
    """(the standard file it looks like, what to change, '' or what it reads as when nothing needs changing)."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.reader(f) if any(c.strip() for c in r)]
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        return None, [f"it cannot be read as UTF-8 CSV ({type(exc).__name__}: {str(exc)[:200]}); save it as UTF-8, "
                      "comma-separated"], ""
    if not rows:
        return None, ["it is empty; its first line is the header"], ""
    header, body = rows[0], rows[1:51]                    # the first 50 rows tell how the file is written
    cols = [c.strip().lower() for c in header]
    kind = _kind(path.stem.lower(), cols)
    if kind is None:
        return None, [], ""
    todo = []
    standard = SIGNALS if kind == "signals" else FILE[kind]
    if path.name.lower() != standard and kind != "prices":
        todo.append(f"name it {standard}" + (" (testbench reads trades.csv next to equity.csv)" if kind != "signals"
                                             else " (or name it in the lookahead plan's output:)"))
    todo += _signal_advice(header, body) if kind == "signals" else _column_advice(kind, header, body)
    if todo:
        return kind, todo, ""
    if kind == "signals":
        return kind, [], (f"{WHAT[kind]}: time, symbol and {len(cols) - 2} decision column"
                          f"{'s' * (len(cols) != 3)} ({', '.join(cols[2:][:5])}), {len(rows) - 1} rows")
    try:
        records = _read(kind, path, path.relative_to(folder).as_posix())
    except TradingFileError as exc:
        return kind, [str(p) for p in exc.problems[:MAX_PROBLEMS]], ""
    if kind == "equity" and len(records) < 2:
        return kind, [f"it has {len(records)} row; the numbers need at least 2 (a start and an end), one row per bar"], ""
    day = lambda t: t.date().isoformat() if t.tzinfo is None and t.time() == dt.time() else t.isoformat()  # noqa: E731
    span = f", {day(records[0].time)} to {day(records[-1].time)}" if records else ""
    hint = ("; it has no benchmark column, so `beats: buy_and_hold` reads closes from prices.csv next to it"
            if kind == "equity" and "benchmark" not in cols else "")
    return kind, [], f"reads as testbench's {FILE[kind]}: {len(records)} row{'s' * (len(records) != 1)}{span}{hint}"


def _kind(stem: str, cols: list[str]) -> str | None:
    for kind, words in BY_NAME:
        if any(w in stem for w in words):
            return kind
    has = lambda key: key in cols or any(s in cols for s in SYNONYMS[key])          # noqa: E731
    if has("side") or (has("quantity") and has("price")):
        return "trades"
    if "equity" in cols or has("equity"):
        return "equity"
    if "close" in cols or "open" in cols:
        return "prices"
    return None


def _renames(kind: str, cols: list[str], wanted: tuple = ()) -> dict:
    """{standard column: the column of this file that holds it}, for the standard columns it lacks."""
    out, known = {}, wanted or REQUIRED[kind] + OPTIONAL[kind]
    for need in known:
        if need in cols:
            continue
        alt = next((c for c in cols if c in SYNONYMS.get(need, ()) and c not in out.values() and c not in known),
                   None)
        if need == "time" and alt is None and "" in cols:
            alt = ""                                       # pandas' unnamed index
        if alt is not None:
            out[need] = alt
    return out


def _column(cols: list[str], body: list, name: str) -> list[str]:
    i = cols.index(name)
    return [r[i].strip() for r in body if i < len(r) and r[i].strip()]


def _shown(header: list[str], col: str) -> str:
    """A column as the file writes it (`Date`, not date), or the unnamed one."""
    if col == "":
        return "the unnamed first column"
    cols = [c.strip().lower() for c in header]
    return f"`{header[cols.index(col)].strip()}`"


def _common_advice(header: list[str], body: list, wanted: tuple, kind: str) -> tuple[list[str], dict]:
    """The renames, the standard columns to add, and the first time that is not ISO 8601; with the renames found."""
    cols = [c.strip().lower() for c in header]
    renames = _renames(kind, cols, wanted)
    todo = [f"name the unnamed first column `{need}` (pandas writes its index without a name: "
            f"df.to_csv(path, index_label='{need}'))" if alt == "" else f"rename column {_shown(header, alt)} to "
            f"`{need}`" for need, alt in renames.items()]
    time_col = "time" if "time" in cols else renames.get("time")
    if time_col is not None:                              # the times as written: ISO 8601, or what to write
        for v in _column(cols, body, time_col)[:20]:
            try:
                _time(v)
            except _Bad as exc:
                todo.append(f"{_shown(header, time_col)}: {exc}")
                break
    return todo, renames


def _column_advice(kind: str, header: list[str], body: list) -> list[str]:
    cols = [c.strip().lower() for c in header]
    todo, renames = _common_advice(header, body, REQUIRED[kind] + OPTIONAL[kind], kind)
    signed = False
    if kind == "trades" and "side" not in cols and "side" not in renames:
        qty = "quantity" if "quantity" in cols else renames.get("quantity")
        signed = bool(qty) and any(v.startswith("-") for v in _column(cols, body, qty))
        if signed:
            todo.append(f"add `side`: buy where {_shown(header, qty)} is above 0 and sell where it is below, then "
                        "write the quantity without its sign")
    for need in REQUIRED[kind]:
        if need not in cols and need not in renames and not (need == "side" and signed):
            todo.append(ADD[need] + (". It has `returns`: equity is the starting cash times the running product "
                                     "of (1 + returns)" if need == "equity" and "returns" in cols else ""))
    side_col = "side" if "side" in cols else renames.get("side")
    if kind == "trades" and side_col:
        odd = sorted({v for v in _column(cols, body, side_col) if v.lower() not in SIDES})
        if odd:
            to = {v: SIDE_WORDS.get(v.lower()) for v in odd[:4]}
            words = ", ".join(f"{v!r} as {w}" if w and w != "?" else repr(v) for v, w in to.items())
            todo.append(f"{_shown(header, side_col)} holds {words}: write buy or sell on every row (opening a short "
                        "is a sell, covering it a buy)")
    return todo


def _signal_advice(header: list[str], body: list) -> list[str]:
    cols = [c.strip().lower() for c in header]
    todo, renames = _common_advice(header, body, ("time", "symbol"), "signals")
    todo += [ADD[need] for need in ("time", "symbol") if need not in cols and need not in renames]
    if not [c for c in cols if c not in ("time", "symbol") and c not in renames.values()]:
        todo.append("add a column per decision after time and symbol: the signal's number (not only the position "
                    "it gives - a number changes on every peek ahead, a 0 or 1 only when it flips)")
    return todo


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m agent_testbench.trading.files",
                                 description="Say what a backtest's own CSV files need to read as testbench's standard "
                                             "trading files (equity.csv, trades.csv, signals.csv): what to rename and "
                                             "what to add. Exit 0 every file reads, 1 something to change, 2 nothing "
                                             "to read.")
    ap.add_argument("--check", required=True, metavar="FOLDER", help="the folder the backtest writes its files to")
    a = ap.parse_args(argv)
    code, lines = check_folder(a.check)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
