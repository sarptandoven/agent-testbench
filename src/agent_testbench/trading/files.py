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
        raise TradingFileError(f"{name} is a folder; give the path of the CSV file in it")
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
        if "time" in missing and "" in cols:
            hint += (". The column with no name may hold the times (pandas writes an unnamed index that way): "
                     "name it, e.g. df.to_csv(path, index_label='time')")
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
