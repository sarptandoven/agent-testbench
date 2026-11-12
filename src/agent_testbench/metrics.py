"""Metric readers: evidence files turned into named numbers, so a check can name a number instead of a regex.

    from agent_testbench import metrics
    metrics.read("trading", Path("run/steps/1"), {"files": {"equity": "out/equity.csv"}, "risk_free": 0.04})
    # {"reader": "trading", "files": {"equity": "out/equity.csv", "trades": "out/trades.csv"},
    #  "values": {"sharpe": 1.21, "max_drawdown": -0.12, ...}, "notes": ["periods per year: 252 ...", ...]}

A reader has a `name`, `files` (the files it reads, by role, each with the glob patterns it tries in order, relative to
the folder it reads: {"equity": ("equity.csv", "*/equity.csv")}), `options` (what read() takes besides the files, with
one line each) and read(workdir, opts) -> {name: value} - each value a finite int or float, a bool, or None where the
number is not defined - plus, under the reserved names `notes` and `files`, plain-words caveats and the files it read.
opts["files"] names files by role (paths relative to workdir); a role it leaves out is looked for by its patterns.
A missing file raises FileNotFoundError naming it; a file or option it refuses raises ValueError saying what to change;
nothing else. sample() gives files read() works on: `testbench plugin test` and contract() read them.

Built in:
    json     a JSON file's numbers by dotted path (accuracy, losses.-1), as the json: expectation reads them
    csv      each numeric column's last value, mean, max and min (loss.last, loss.min), and rows
    junit    tests, passed, failures, errors and skipped from a JUnit XML report
    trading  testbench's own backtest numbers (agent_testbench.trading.metrics: sharpe, max_drawdown, trades, ...)
             from equity.csv, and trades.csv and prices.csv next to it when they are there

Readers are a plug-in kind (plugins.py): a package adds one with an entry point in the group agent_testbench.metrics
that names a Reader subclass or instance. `testbench numbers READER [PATH...]` prints what a reader finds, to write
rules with; docs/plugins-metrics.md has an example package.

Planned for checks (not wired yet: expect.py is where checks live): a step's expect: names a reader, its files and
rules on the numbers it reads, each rule as every other expectation writes one:

    expect:
      numbers:
        reader: trading
        files: {equity: out/equity.csv, trades: out/trades.csv}
        options: {risk_free: 0.04}
        rules: {sharpe: ">= 1", max_drawdown: ">= -0.2", trades: ">= 30"}

A missing file fails every rule with the reader's message; a number that is None fails its rule.

This module is part of the core: only the standard library and plugins at module level. The json and junit readers
use expect's dig and read_junit, and the trading reader the trading pack, each imported when it reads.
"""
from __future__ import annotations

import csv
import glob
import json
import math
import numbers as _numbers
import re
import tempfile
from pathlib import Path

from . import plugins

RESERVED = ("notes", "files")             # a reader's result: these two are not numbers
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
LIST_ITEMS = 10                           # json: a longer list shows only its first and last item
MAX_VALUES = 500                          # json: at most this many numbers from one file


class MetricsError(ValueError):
    """A reader, file or option refused; the message says what to change."""


class Reader:
    """Subclass it: set name, what, files and options, and write read() and sample()."""
    name = ""
    what = ""                                         # one line, for `testbench numbers` and `testbench plugins`
    files: dict = {}                                  # role -> glob patterns tried in order, relative to the folder
    required: tuple = ()                              # the roles that must be found (default: the first)
    options: dict = {}                                # option -> one line on what it does

    def read(self, workdir: Path, opts: dict) -> dict:
        raise NotImplementedError

    def sample(self) -> dict[str, str]:
        """{relative path: text}: files read() reads with its default options."""
        return {}

    def path(self, workdir: Path, opts: dict, role: str) -> Path | None:
        """The file for one role: opts["files"][role] when given (relative to workdir), else the only file the first
        matching pattern of the role finds. None for an optional role found nowhere; a FileNotFoundError naming the
        file (or the patterns tried) for a required one, or one that was named."""
        workdir = Path(workdir)
        given = ((opts or {}).get("files") or {}).get(role)
        label = "file" if role == "file" else f"{role} file"
        if given:
            f = workdir / given
            if f.is_dir():
                raise MetricsError(f"{given} is a folder; name the {label} in it")
            if not f.is_file():
                raise FileNotFoundError(f"{given}: no such file{'' if Path(given).is_absolute() else f' in {workdir}'} "
                                        f"(the {label} {self.name} was told to read)")
            return f
        patterns = self.files.get(role) or ()
        for pattern in (patterns,) if isinstance(patterns, str) else patterns:
            hits = [h for h in sorted(glob.glob(pattern, root_dir=workdir)) if (workdir / h).is_file()]
            if len(hits) > 1:
                raise MetricsError(f"{len(hits)} files match {pattern} in {workdir} ({', '.join(hits[:5])}); name the "
                                   f"{label} to read")
            if hits:
                return workdir / hits[0]
        if role in (self.required or tuple(self.files)[:1]):
            tried = ", ".join((patterns,) if isinstance(patterns, str) else patterns) or "nothing (no patterns)"
            raise FileNotFoundError(f"no {label} in {workdir}: {self.name} looked for {tried}. Name it (a PATH to "
                                    "`testbench numbers`, or files: {" + f"{role}: PATH" + "})")
        return None

    @staticmethod
    def shown(workdir: Path, path: Path) -> str:
        """A path as messages show it: relative to workdir when it is inside it."""
        try:
            return Path(path).relative_to(workdir).as_posix()
        except ValueError:
            return str(path)


def listed(value) -> list[str]:
    """An option that names one thing or several (`keys: accuracy`, `keys: [accuracy, loss]`), as a list."""
    if value is None or value == "":
        return []
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]


def _number(text: str):
    t = text.strip()
    return int(t) if re.fullmatch(r"[+-]?\d+", t) else float(t)


# ---- the built-in readers ----------------------------------------------------------------------------------------

class JsonReader(Reader):
    name = "json"
    what = "a JSON file's numbers by dotted path (accuracy, losses.-1)"
    files = {"file": ("metrics.json", "*/metrics.json")}
    options = {"keys": "dotted paths to read, as the json: expectation names them (default: every number)"}

    def read(self, workdir, opts):
        f = self.path(workdir, opts, "file")
        shown = self.shown(workdir, f)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, RecursionError) as exc:           # bad JSON, not UTF-8
            raise MetricsError(f"{shown} is not valid JSON ({exc}); write it with json.dump") from None
        out, notes = {}, []
        keys = listed(opts.get("keys"))
        if keys:
            from .expect import dig
            for key in keys:
                try:
                    value = dig(data, key)
                except (KeyError, IndexError):
                    out[key] = None
                    notes.append(f"no {key!r} in {shown}")
                    continue
                out[key] = _finite(value, key, notes) if _plain(value) else None
                if not _plain(value):
                    notes.append(f"{key} in {shown} is {type(value).__name__}, not a number")
        else:
            skipped = _flatten(data, "", out, notes)
            if skipped:
                notes.append(f"{skipped} text value(s) in {shown} left out: they are not numbers")
            if len(out) > MAX_VALUES:
                notes.append(f"{shown} holds {len(out)} numbers; the first {MAX_VALUES} are shown: name the ones you "
                             "want in keys")
                out = dict(list(out.items())[:MAX_VALUES])
        return {**out, "notes": notes, "files": {"file": shown}}

    def sample(self):
        return {"metrics.json": '{"accuracy": 0.93, "losses": [0.9, 0.5, 0.31], "done": true, "model": "tiny"}\n'}


def _plain(value) -> bool:
    return value is None or isinstance(value, (bool, int, float))


def _finite(value, name: str, notes: list):
    """value, or None (with a note) for NaN and infinity: JSON from Python can hold them, and a rule must not pass."""
    if isinstance(value, float) and not math.isfinite(value):
        notes.append(f"{name} is {value}: not a finite number, so it is null")
        return None
    return value


def _flatten(data, prefix: str, out: dict, notes: list) -> int:
    """Every number, bool and null in data by dotted path into out; returns how many text values were left out."""
    at = (lambda key: f"{prefix}.{key}") if prefix else str
    if _plain(data):
        out[prefix or "value"] = _finite(data, prefix or "value", notes)
        return 0
    if isinstance(data, dict):
        return sum(_flatten(v, at(k), out, notes) for k, v in data.items())
    if isinstance(data, list):
        if len(data) > LIST_ITEMS:
            notes.append(f"{prefix or 'the file'} has {len(data)} items: only {at(0)} and {at(-1)} are shown; name "
                         f"others in keys (e.g. {at(len(data) // 2)})")
            return _flatten(data[0], at(0), out, notes) + _flatten(data[-1], at(-1), out, notes)
        return sum(_flatten(v, at(i), out, notes) for i, v in enumerate(data))
    return 1


class CsvReader(Reader):
    name = "csv"
    what = "each numeric column's last value, mean, max and min (loss.last, loss.min), and rows"
    files = {"file": ("metrics.csv", "*/metrics.csv")}
    options = {"columns": "the columns to read (default: every numeric one)",
               "stats": "which of last, mean, max, min (default: all four)"}
    STATS = ("last", "mean", "max", "min")

    def read(self, workdir, opts):
        f = self.path(workdir, opts, "file")
        shown = self.shown(workdir, f)
        try:
            with open(f, newline="", encoding="utf-8-sig") as fh:
                rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
        except UnicodeDecodeError as exc:
            raise MetricsError(f"{shown} is not UTF-8 text ({exc.reason}); save it as UTF-8 CSV") from None
        except csv.Error as exc:
            raise MetricsError(f"{shown} is not readable as CSV ({exc}); write plain comma-separated values") from None
        if not rows:
            raise MetricsError(f"{shown} is empty; its first line is the header, the column names")
        header, body = [h.strip() for h in rows[0]], rows[1:]
        wanted, stats = listed(opts.get("columns")), listed(opts.get("stats")) or list(self.STATS)
        for name, known, what in [(c, header, "column") for c in wanted] + [(s, self.STATS, "stat") for s in stats]:
            if name not in known:
                raise MetricsError(f"{shown} has no {what} {name!r}; the {what}s are {', '.join(known)}")
        out, notes, text = {"rows": len(body)}, [], []
        for column in wanted or header:
            i = header.index(column)
            cells = [r[i].strip() for r in body if i < len(r) and r[i].strip()]
            try:
                xs = [_number(c) for c in cells]
            except ValueError:
                if wanted:
                    notes.append(f"{column} in {shown} holds text, not numbers: its values are null")
                    out.update({f"{column}.{s}": None for s in stats})
                else:
                    text.append(column)
                continue
            finite = all(math.isfinite(x) for x in xs)
            if not xs:
                notes.append(f"{column} in {shown} has no values: its numbers are null")
            elif not finite:
                notes.append(f"{column} in {shown} has a nan or infinite value: its "
                             f"{'last value, ' if not math.isfinite(xs[-1]) else ''}mean, max and min are null")
            stat = {"last": lambda: xs[-1], "mean": lambda: math.fsum(xs) / len(xs), "max": lambda: max(xs),
                    "min": lambda: min(xs)}
            for s in stats:
                ok = xs and (math.isfinite(xs[-1]) if s == "last" else finite)
                out[f"{column}.{s}"] = stat[s]() if ok else None
        if text:
            notes.append(f"left out text columns: {', '.join(text)}")
        return {**out, "notes": notes, "files": {"file": shown}}

    def sample(self):
        return {"metrics.csv": "epoch,loss,accuracy,phase\n1,0.9,0.52,warmup\n2,0.41,0.81,train\n3,0.25,0.88,train\n"}


class JunitReader(Reader):
    name = "junit"
    what = "tests, passed, failures, errors and skipped from a JUnit XML report"
    files = {"file": ("junit.xml", "*/junit.xml", "report.xml", "*/report.xml")}
    KEYS = ("tests", "passed", "failures", "errors", "skipped")

    def read(self, workdir, opts):
        import xml.etree.ElementTree as ET
        from .expect import read_junit
        f = self.path(workdir, opts, "file")
        shown = self.shown(workdir, f)
        try:
            got = read_junit(f)
        except (ET.ParseError, ValueError) as exc:
            raise MetricsError(f"{shown} is not a JUnit XML report ({exc}); write it with your runner's JUnit "
                               "reporter (pytest --junitxml=junit.xml)") from None
        notes = [f"failed: {case}" for case in got["failing"][:5]] + \
            [f"errored: {case}" for case in got["erroring"][:5]]
        return {**{k: got[k] for k in self.KEYS}, "notes": notes, "files": {"file": shown}}

    def sample(self):
        return {"junit.xml": '<testsuite name="s" tests="3"><testcase classname="t" name="a"/><testcase classname="t" '
                             'name="b"><failure message="1 != 2"/></testcase><testcase classname="t" name="c">'
                             '<skipped/></testcase></testsuite>\n'}


class TradingReader(Reader):
    name = "trading"
    what = "testbench's own backtest numbers (sharpe, max_drawdown, trades, ...)"
    files = {"equity": ("equity.csv", "*/equity.csv"), "trades": ("trades.csv",), "prices": ("prices.csv",)}
    required = ("equity",)
    options = {"risk_free": "the yearly risk-free rate as a fraction, e.g. 0.04 (default 0)",
               "periods_per_year": "bars per year, overriding the one read from the times",
               "calendar": "auto, stocks (252 days a year) or crypto (365), for daily and intraday bars",
               "symbol": "prices.csv's benchmark symbol, when it has several"}

    def read(self, workdir, opts):
        from .trading import metrics as TM
        workdir = Path(workdir)
        equity = self.path(workdir, opts, "equity")
        given = opts.get("files") or {}
        found = {"equity": equity}
        for role in ("trades", "prices"):                    # named, else next to equity.csv when it is there
            found[role] = self.path(workdir, opts, role) if given.get(role) else self.path(equity.parent, {}, role)
        settings = {k: opts[k] for k in self.options if opts.get(k) is not None}
        others = [str(found[role]) if found[role] else None for role in ("trades", "prices")]
        got = TM.compute(str(equity), *others, **settings)    # a refused file or setting: ValueError saying what to do
        return {**got, "files": {role: self.shown(workdir, f) for role, f in found.items() if f}}

    def sample(self):
        return {"equity.csv": "time,equity\n2024-01-02,100000\n2024-01-03,100400\n2024-01-04,99900\n"
                              "2024-01-05,101200\n2024-01-08,101000\n",
                "trades.csv": "time,symbol,side,quantity,price,fee\n2024-01-02,SPY,buy,10,470,1\n"
                              "2024-01-08,SPY,sell,10,480,1\n"}


# ---- the plug-in kind --------------------------------------------------------------------------------------------

def _prepare(obj, name: str):
    reader = obj() if isinstance(obj, type) else obj
    if not callable(getattr(reader, "read", None)):
        raise MetricsError(f"a reader has read(workdir, opts); {type(reader).__name__} has none (subclass "
                           "agent_testbench.metrics.Reader)")
    if name and getattr(reader, "name", "") != name:
        reader.name = name                               # the name it is looked up by, as messages should say
    return reader


def contract(reader) -> list[str]:
    """What `testbench plugin test` checks a reader for, in plain words ([] = it keeps the contract): a plain name;
    files by role; read() with its file missing raises FileNotFoundError naming the file, never another exception;
    on its sample() files it returns finite numbers, bools or None by name, and the same names when it reads them
    again."""
    out = []
    name, files = getattr(reader, "name", None), getattr(reader, "files", None)
    if not isinstance(name, str) or not NAME.fullmatch(name):
        out.append(f"its name is {name!r}; a reader's name is lowercase letters, digits, - and _, like trading")
    if not isinstance(files, dict) or not all(isinstance(r, str) and (isinstance(p, str) or (
            isinstance(p, (list, tuple)) and all(isinstance(x, str) for x in p))) for r, p in files.items()):
        out.append("files maps each file it reads (a role, e.g. equity) to the glob patterns it tries, e.g. "
                   "{'equity': ('equity.csv', '*/equity.csv')}")
        files = {}
    role = next(iter(files), "file")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "empty").mkdir()
        told = {"files": {role: "nowhere/missing-file.csv"}}
        for opts, missing in (({"files": {}}, None), (told, "missing-file.csv")):
            what = f"the {role} file it was told to read is missing" if missing else "the folder is empty"
            try:
                reader.read(root / "empty", opts)
            except FileNotFoundError as exc:
                if missing and missing not in str(exc):
                    out.append(f"when {what}, read() raises FileNotFoundError, but its message does not name the file: "
                               f"{exc}")
            except Exception as exc:
                out.append(f"when {what}, read() raises {type(exc).__name__} ({exc}); it must raise FileNotFoundError "
                           "naming the file")
            else:
                out.append(f"when {what}, read() returns; it must raise FileNotFoundError naming the file")
        sample = reader.sample() if callable(getattr(reader, "sample", None)) else None
        if not isinstance(sample, dict) or not sample:
            return out + ["sample() gives no files, so its numbers cannot be checked: return {relative path: text} "
                          "that read() reads with its default options"]
        for rel, text in sample.items():
            f = (root / "sample" / rel).resolve()
            if not f.is_relative_to((root / "sample").resolve()):
                return out + [f"sample() names {rel}, outside the folder it is read from"]
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text, encoding="utf-8")
        reads = []
        for _ in range(2):
            try:
                reads.append(reader.read(root / "sample", {"files": {}}))
            except Exception as exc:
                return out + [f"read() on its own sample() files raises {type(exc).__name__}: {exc}"]
    first = reads[0]
    if not isinstance(first, dict):
        return out + [f"read() returns {type(first).__name__}; it returns {{name: number}}"]
    for key, value in first.items():
        if key == "notes":
            if not isinstance(value, list) or not all(isinstance(n, str) for n in value):
                out.append("notes is a list of plain-words strings")
        elif key == "files":
            if not isinstance(value, dict):
                out.append("files is {role: the path it read}")
        elif not isinstance(key, str) or not key.strip():
            out.append(f"{key!r} is not a name; a number's name is a non-empty string")
        elif not (value is None or isinstance(value, bool) or type(value) is int or
                  (type(value) is float and math.isfinite(value))):
            out.append(f"{key} is {value!r} ({type(value).__name__}); a value is a finite int or float, a bool, or "
                       "None where the number is not defined")
    if not any(k not in RESERVED for k in first):
        out.append("read() on its sample() files found no numbers")
    if isinstance(reads[1], dict) and [k for k in first if k not in RESERVED] != \
            [k for k in reads[1] if k not in RESERVED]:
        out.append("reading the same files twice gave different names; names are stable, as checks name them")
    return out


KIND = plugins.Kind(
    "metrics", "reader", "numbers read from a step's files (`testbench numbers`)",
    {"json": f"{__name__}:JsonReader", "csv": f"{__name__}:CsvReader", "junit": f"{__name__}:JunitReader",
     "trading": f"{__name__}:TradingReader"},
    prepare=_prepare, contract=contract)


# ---- reading -----------------------------------------------------------------------------------------------------

def names() -> list[str]:
    return plugins.names("metrics")


def get(name: str):
    """A reader by name, built in or installed; a MetricsError that names the readers there are when there is none."""
    known = plugins.names("metrics")
    if name in known:
        return plugins.get("metrics", name)
    if name in plugins.broken("metrics"):
        try:
            plugins.get("metrics", name)
        except plugins.PluginError as exc:
            raise MetricsError(str(exc)) from None
    from .config import near
    raise MetricsError(f"no reader {name!r}{near(str(name), known)}; the readers are {', '.join(known)} (`testbench "
                       "numbers` lists them and the files each reads)")


def roles(reader) -> list[str]:
    return list(getattr(reader, "files", None) or {}) or ["file"]


def read(name: str, workdir, opts: dict | None = None) -> dict:
    """What reader `name` finds in workdir: {reader, files: {role: path}, values: {name: number}, notes: [...]}.
    opts: files - a path, a list of paths in the reader's order of roles, or {role: path} - and the reader's options.
    A missing file raises FileNotFoundError naming it; a refused file, option or reader raises ValueError
    (MetricsError) saying what to change; nothing else. A value that is not a finite number becomes None, with a
    note, so it passes no rule."""
    reader = get(name)
    opts = dict(opts or {})
    opts["files"] = _files(reader, opts.get("files"))
    known = getattr(reader, "options", None)
    if isinstance(known, dict):
        from .config import near
        for key in opts:
            if key != "files" and key not in known:
                raise MetricsError(f"reader {name} has no option {key!r}{near(key, list(known))}; "
                                   + (f"its options are {', '.join(known)}" if known else "it takes none"))
    workdir = Path(workdir)
    try:
        got = reader.read(workdir, opts)
    except (FileNotFoundError, ValueError):
        raise
    except Exception as exc:                             # a reader's own bug, or a file it could not open
        raise MetricsError(f"reader {name} could not read {workdir}: {type(exc).__name__}: {exc}") from None
    return _clean(name, got)


def _files(reader, given) -> dict:
    if not given:
        return {}
    have = roles(reader)
    if isinstance(given, (str, Path)):
        given = [given]
    if isinstance(given, (list, tuple)):
        if len(given) > len(have):
            raise MetricsError(f"reader {reader.name} reads {len(have)} file(s) ({', '.join(have)}); got {len(given)}: "
                               f"{', '.join(map(str, given))}")
        return {role: str(path) for role, path in zip(have, given)}
    if isinstance(given, dict):
        unknown = [r for r in given if r not in have]
        if unknown:
            raise MetricsError(f"reader {reader.name} has no file {unknown[0]!r}; its files are {', '.join(have)}")
        return {str(role): str(path) for role, path in given.items() if path}
    raise MetricsError(f"files is a path, a list of paths or {{role: path}}; got {given!r}")


def _clean(name: str, got) -> dict:
    if not isinstance(got, dict):
        raise MetricsError(f"reader {name} returned {type(got).__name__}, not {{name: number}}")
    notes = [str(n) for n in got.get("notes") or []]
    values = {}
    for key, value in got.items():
        if key in RESERVED:
            continue
        if not isinstance(key, str) or not key.strip():
            raise MetricsError(f"reader {name} gave a number without a name ({key!r})")
        if value is None or isinstance(value, bool):
            values[key] = value
        elif isinstance(value, _numbers.Integral):
            values[key] = int(value)
        elif isinstance(value, _numbers.Real):
            x = float(value)
            values[key] = x if math.isfinite(x) else None
            if not math.isfinite(x):
                notes.append(f"{key} is {x}: not a finite number, so it counts as none and passes no rule")
        else:
            raise MetricsError(f"reader {name} gave {key} = {value!r}, which is not a number, a bool or null")
    files = got.get("files") if isinstance(got.get("files"), dict) else {}
    return {"reader": name, "files": {str(k): str(v) for k, v in files.items()}, "values": values, "notes": notes}


# ---- what `testbench numbers` prints -----------------------------------------------------------------------------

def as_text(value) -> str:
    """A value as `testbench numbers` prints it: JSON's words for null and the booleans, 6 significant digits."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value) if isinstance(value, int) else f"{value:.6g}"


def render(got: dict) -> str:
    """A reading as text: the files read, each name and number, the notes."""
    files = ", ".join(got["files"].values()) or "its files"
    lines = [f"{got['reader']} read {files}:"]
    width = min(max((len(k) for k in got["values"]), default=0), 40)
    lines += [f"  {k:<{width}}  {as_text(v)}" for k, v in got["values"].items()] or ["  no numbers"]
    if got["notes"]:
        lines += ["notes:"] + [f"  - {n}" for n in got["notes"]]
    return "\n".join(lines)


def listing() -> str:
    """Every reader: what it reads, the files it looks for, its options and, for an installed one, its package."""
    lines = ["Readers turn evidence files into named numbers: `testbench numbers READER [PATH...]` prints them.", ""]
    for name in names():
        reader = plugins.get("metrics", name)
        lines.append(f"{name:<9} {getattr(reader, 'what', '') or ''}")
        if not plugins.builtin("metrics", name):
            lines.append(f"{'':<12}from {_package(name)}")
        for role, patterns in (getattr(reader, "files", None) or {}).items():
            patterns = (patterns,) if isinstance(patterns, str) else patterns
            lines.append(f"{'':<12}{role}: {', '.join(patterns)}")
        if getattr(reader, "options", None):
            lines.append(f"{'':<12}options: {', '.join(reader.options)}")
    bad = plugins.broken("metrics")
    if bad:
        lines += ["", "Installed but not loaded (fix or reinstall the package; `testbench plugins` lists every "
                      "plug-in):"]
        lines += [f"  {name} from {_package(name)}: {why}" for name, why in bad.items()]
    return "\n".join(lines)


def _package(name: str) -> str:
    for ep in plugins.entry_points("metrics"):
        if ep.name == name:
            return getattr(getattr(ep, "dist", None), "name", None) or ep.value.split(":")[0].split(".")[0]
    return "this process (plugins.register)"
