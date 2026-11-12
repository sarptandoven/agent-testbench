"""The data seal: the data a project's plans read, named by role in testbench.yaml, with a sealed holdout, walk-forward
windows and a fingerprint of the bytes every step saw (docs/trading-data.md).

    data:
      prices:
        path: data/prices.csv      # a CSV file with a time column, or a folder of CSV files
        time: time                 # the time column (default: the first of time, date, timestamp, datetime)
        holdout_from: 2025-07-01   # rows at or after this time are sealed

It works in three places:
- config: `data:`, and a plan's `holdout:` and `walk_forward:`, are checked when testbench.yaml loads, without reading
  the data (a file may arrive later). A plan whose steps see a cut file runs in a copy of the project, never in it.
- launch, on this machine: each data file is read once (cached while it is unchanged) - its sha256, rows, first and
  last time, and the sealed part's sha256 - the holdout's earlier openings are counted (and refused past max_opens),
  and a walk-forward plan's windows are cut from the time range: plan.json holds a copy of the steps per window.
- the executor, where the plan runs: before the first step each sealed file in the run's copy is cut to the rows
  before holdout_from (and in each window to the rows before the window's test end), and what the steps see is
  hashed into the report and into data.json in each step's folder (evidence, so `testbench verify` covers it).

A row is cut by its own time, so files whose rows are not in time order (several symbols) are cut right. Times are
ISO 8601, as in the trading files. A cut without a zone is compared with each time as it is written (its wall
clock); with a zone, with zoned times as instants.
"""
from __future__ import annotations

import array
import bisect
import calendar
import contextlib
import copy
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from .config import ConfigError, counted, near, number

ROLE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,40}")
ROLE_KEYS = ("path", "time", "holdout_from")
TIME_NAMES = ("time", "date", "timestamp", "datetime")
WALK_KEYS = ("data", "train", "test", "step", "anchored", "start")
DURATION = re.compile(r"([1-9][0-9]{0,4})\s*(d|w|mo|y)")
UNITS = {"d": "day", "w": "week", "mo": "month", "y": "year"}
MAX_WINDOWS = 200
HOLDOUT_LOG = Path(".testbench") / "holdout.jsonl"
EXAMPLE = "{prices: {path: data/prices.csv, time: time, holdout_from: 2025-07-01}}"
WALK_EXAMPLE = "{data: prices, train: 24mo, test: 6mo, step: 6mo}"
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


class DataError(ConfigError):
    """Data a launch or a step cannot use as testbench.yaml's data: describes it; the message names the file, the
    line and what to do."""


def env_name(role: str) -> str:
    """The variable a step finds a role's file in: TESTBENCH_DATA_PRICES for prices."""
    return f"TESTBENCH_DATA_{role.upper()}"


# ---- testbench.yaml: data:, and a plan's holdout: and walk_forward: (nothing is read here) ------------------------

def check_data(raw, root: Path, exclude: list[str]) -> dict:
    """testbench.yaml's data: as {role: {path, time, holdout_from}}, {} when there is none."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"`data:` names the data the plans read, by role, e.g. {EXAMPLE}")
    from .backends import excluded
    out, names = {}, {}
    for role, spec in raw.items():
        where = f"data.{role}"
        if not isinstance(role, str) or not ROLE.fullmatch(role):
            raise ConfigError(f"`data` has the role {role!r}; a role is a short name of letters, digits and _, "
                              "starting with a letter (a step finds its file in TESTBENCH_DATA_<ROLE>), e.g. prices")
        if env_name(role) in names:
            raise ConfigError(f"`data.{role}` and `data.{names[env_name(role)]}` would both be {env_name(role)}; "
                              "rename one")
        names[env_name(role)] = role
        spec = {"path": spec} if isinstance(spec, str) else spec
        if not isinstance(spec, dict):
            raise ConfigError(f"`{where}` is the data's path, or {{path, time, holdout_from}}, e.g. {EXAMPLE}")
        for key in spec:
            if key not in ROLE_KEYS:
                raise ConfigError(f"`{where}` has no {key!r}{near(key, ROLE_KEYS)}; use {', '.join(ROLE_KEYS)}, e.g. "
                                  f"{EXAMPLE}")
        rel = _relative(where, spec.get("path"), root)
        if excluded(rel, exclude):
            raise ConfigError(f"`{where}.path`: {rel} is left out of copies of the project by `exclude:`, and the "
                              "plans that read it run in a copy; take the pattern that matches it out of `exclude:`")
        column = spec.get("time")
        if column is not None and (not isinstance(column, str) or not column.strip()):
            raise ConfigError(f"`{where}.time` is the name of the time column, e.g. time (left out: the first of "
                              f"{', '.join(TIME_NAMES)})")
        cut = spec.get("holdout_from")
        out[role] = {"path": rel, "time": column.strip() if column else None,
                     "holdout_from": None if cut is None else _when(f"{where}.holdout_from", cut)}
    return out


def _relative(where: str, path, root: Path) -> str:
    text = path.strip().replace("\\", "/") if isinstance(path, str) else ""
    p = PurePosixPath(text)
    if not text or p.is_absolute() or re.match(r"[A-Za-z]:", text) or ".." in p.parts or not p.parts \
            or p.parts[0] == ".testbench":
        raise ConfigError(f"`{where}.path` is {path!r}; write the data's path in the project, relative to {root}, "
                          "e.g. data/prices.csv (a plan runs in a copy of the project, which holds only its own files)")
    return p.as_posix()


def _when(where: str, value, example: str = "2025-07-01") -> str:
    """A time from testbench.yaml (YAML reads 2025-07-01 as a date) as ISO 8601 text."""
    if isinstance(value, dt.datetime):
        return _text(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return _text(_parse(value))
        except ValueError:
            pass
    raise ConfigError(f"`{where}` is {value!r}; write an ISO 8601 date or time, e.g. {example} or "
                      f"{example}T00:00:00Z")


def check_plan(where: str, plan: dict, data: dict) -> None:
    """A plan's holdout: and walk_forward:, written out in place; and its workdir: a copy whenever its steps see a cut
    file, so the cut never touches the project's own. Called before the backend's default workdir is filled in."""
    holdout = plan.get("holdout")
    sealed = [role for role, d in data.items() if d["holdout_from"]]
    if holdout in (None, False):
        plan.pop("holdout", None)
        holdout = None
    else:
        if holdout is True or holdout == {}:
            holdout = {"max_opens": None}
        if not isinstance(holdout, dict) or set(holdout) - {"max_opens"}:
            raise ConfigError(f"`{where}.holdout` is true (this plan opens the sealed rows), or {{max_opens: 1}} to "
                              "refuse an opening past the first")
        most = holdout.get("max_opens")
        if most is not None and not (number(most, positive=True) and float(most).is_integer()):
            raise ConfigError(f"`{where}.holdout.max_opens` is how many times the holdout may be opened, a whole "
                              "number of 1 or more, e.g. 1")
        if not sealed:
            raise ConfigError(f"`{where}.holdout`: no data is sealed, so there is no holdout to open. Seal the latest "
                              "rows with holdout_from, e.g. data: {prices: {path: data/prices.csv, holdout_from: "
                              "2025-07-01}}")
        plan["holdout"] = holdout = {"max_opens": None if most is None else int(most)}
    wf = plan.get("walk_forward")
    if wf is not None:
        plan["walk_forward"] = wf = _walk(where, wf, data)
        if holdout:
            raise ConfigError(f"`{where}`: a plan opens the holdout (holdout:) or walks forward through the rows "
                              "before it (walk_forward:), not both; make them two plans")
    cut = (bool(sealed) and not holdout) or wf is not None
    if cut:
        if plan.get("workdir") == "inplace":
            what = "walk-forward windows" if wf else "the sealed data"
            raise ConfigError(f"`{where}.workdir` is inplace, but this plan's steps see {what} cut to the rows they "
                              "may read, which is done only in a copy of the project, never in your files; remove "
                              "`workdir: inplace` (the plan runs in a copy)")
        plan["workdir"] = "copy"


def _walk(where: str, wf, data: dict) -> dict:
    at = f"{where}.walk_forward"
    if not isinstance(wf, dict):
        raise ConfigError(f"`{at}` is {{data, train, test, step}}: the steps run once per window, e.g. {WALK_EXAMPLE}")
    for key in wf:
        if key not in WALK_KEYS:
            raise ConfigError(f"`{at}` has no {key!r}{near(key, WALK_KEYS)}; use {', '.join(WALK_KEYS)}, e.g. "
                              f"{WALK_EXAMPLE}")
    if not data:
        raise ConfigError(f"`{at}` cuts its windows from a data file's time column; name the data first, under "
                          f"`data:`, e.g. {EXAMPLE}")
    role = wf.get("data")
    if role is None and len(data) == 1:
        role = next(iter(data))
    if role not in data:
        raise ConfigError(f"`{at}.data` names the data to cut windows from: one of {', '.join(data)}"
                          + (f"; there is no {role!r}{near(role, data)}" if role is not None else ""))
    out = {"data": role}
    for key in ("train", "test", "step"):
        value = wf.get(key, wf.get("test") if key == "step" else None)
        if not isinstance(value, str) or not DURATION.fullmatch(value.strip()):
            hint = (" (mo is months: m could be minutes)" if isinstance(value, str) and re.fullmatch(r"\d+\s*m", value)
                    else "")
            raise ConfigError(f"`{at}.{key}` is {value!r}{hint}; a length is a whole number and d (days), w (weeks), "
                              f"mo (months) or y (years), e.g. {WALK_EXAMPLE}" + (" (step: defaults to test)"
                                                                                 if key == "step" else ""))
        out[key] = value.replace(" ", "")
    anchored = wf.get("anchored", False)
    if not isinstance(anchored, bool):
        raise ConfigError(f"`{at}.anchored` is true (every train period starts at the first window's start and "
                          "grows) or false (the train period rolls forward, the default)")
    out["anchored"] = anchored
    out["start"] = None if wf.get("start") is None else _when(f"{at}.start", wf["start"], "2020-01-01")
    return out


def check_plans(plans: dict) -> None:
    """At most one plan opens the holdout, so its reports count every time the sealed rows were opened."""
    opening = [name for name, p in plans.items() if p.get("holdout")]
    if len(opening) > 1:
        raise ConfigError(f"`plans.{opening[1]}.holdout`: plan {opening[0]} already opens the holdout. One plan does, "
                          "so its reports can say how many times the sealed rows were opened; take holdout: out of "
                          "the other")


# ---- reading a data file: one pass, cut by each row's own time --------------------------------------------------

def _parse(text: str) -> dt.datetime:
    from .trading.files import _Bad, _time
    try:
        return _time(str(text))
    except _Bad as exc:
        raise ValueError(f"{text!r} {exc}" if str(exc).startswith("is ") else str(exc)) from None


def _text(t: dt.datetime | None) -> str | None:
    """ISO 8601: a date alone for a time at midnight without a zone."""
    if t is None:
        return None
    return t.date().isoformat() if t.tzinfo is None and t.time() == dt.time(0) else t.isoformat()


def _before(t: dt.datetime, cut: dt.datetime) -> bool:
    if (t.tzinfo is None) != (cut.tzinfo is None):            # one has a zone: compare the wall clocks as written
        return t.replace(tzinfo=None) < cut.replace(tzinfo=None)
    return t < cut


def _like(t: dt.datetime, sample: dt.datetime) -> dt.datetime:
    """t written the way sample is (with its zone, or without one)."""
    if (t.tzinfo is None) == (sample.tzinfo is None):
        return t
    return t.replace(tzinfo=sample.tzinfo) if t.tzinfo is None else t.replace(tzinfo=None)


def _seconds(t: dt.datetime) -> float:
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def _records(f, name: str):
    """(line number, bytes) of each row of a CSV file, one per line. A quoted value that runs over a line break is
    refused: a stray quote would make the rest of the file one row, sealed rows and all, filed under its first time."""
    for n, raw in enumerate(f, 1):
        if raw.count(b'"') % 2:
            raise DataError(f"{name} line {n} opens a quoted value it does not close; the seal reads one row per line "
                            "(a value that holds a line break, or a stray quote, would hide where rows end): close "
                            "the quote, or take the line break out of the value")
        yield n, raw


def _cells(text: str) -> list[str]:
    text = text.rstrip("\r\n")
    return next(csv.reader([text]), []) if '"' in text else text.split(",")


def _column(cells: list[str], want: str | None, name: str, role: str) -> int:
    cols = [c.strip().lower() for c in cells]
    found = ", ".join(c.strip() for c in cells if c.strip()) or "nothing"
    if want:
        if want.strip().lower() in cols:
            return cols.index(want.strip().lower())
        raise DataError(f"{name}, line 1 (the header): no `{want}` column, which data.{role}.time names as the time "
                        f"column; found: {found}")
    for n in TIME_NAMES:
        if n in cols:
            return cols.index(n)
    hint = (" The column with no name may hold the times (pandas writes an unnamed index that way): name it, e.g. "
            "df.to_csv(path, index_label='time')." if "" in cols else "")
    raise DataError(f"{name}, line 1 (the header): no time column (looked for {', '.join(TIME_NAMES)}; found: "
                    f"{found}).{hint} Name the column with data.{role}.time")


def _part() -> dict:
    return {"hash": hashlib.sha256(), "bytes": 0, "rows": 0, "first": None, "last": None}


def _row(part: dict, t: dt.datetime) -> None:
    part["rows"] += 1
    if part["first"] is None or t < part["first"]:
        part["first"] = t
    if part["last"] is None or t > part["last"]:
        part["last"] = t


def _done(part: dict) -> dict:
    return {"bytes": part["bytes"], "sha256": part["hash"].hexdigest(), "rows": part["rows"], "first": part["first"],
            "last": part["last"]}


def _pass(src: Path, name: str, role: str, want: str | None, cut: dt.datetime | None = None, out: Path | None = None,
          times: bool = False) -> dict:
    """One pass over a CSV file: {file, kept, sealed} - the whole file, the rows before cut (all of them without one)
    and the rest, each with bytes, sha256, rows and first and last time - and the time column's name. out: write the
    header and the kept rows there (the file a step sees; kept's sha256 is that file's). times: the kept rows' times
    as seconds, sorted, for counting rows per window."""
    parts = {"file": _part(), "kept": _part(), "sealed": _part()}
    secs = array.array("d") if times else None
    index = column = zone = None
    tmp = sink = None
    try:
        if out is not None:
            tmp = out.with_name(f".{out.name}.testbench-cut")
            sink = open(tmp, "wb")
        with open(src, "rb") as f:
            for line, raw in _records(f, name):
                parts["file"]["hash"].update(raw)
                parts["file"]["bytes"] += len(raw)
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise DataError(f"{name} line {line} is not UTF-8 text ({exc.reason}); save the file as UTF-8 "
                                    "CSV") from None
                blank = not text.strip(" \t\r\n,\ufeff")
                if index is None:                              # the header (and blank lines before it) go along
                    if not blank:
                        cells = _cells(text.lstrip("\ufeff"))
                        index = _column(cells, want, name, role)
                        column = cells[index].strip()
                    parts["kept"]["hash"].update(raw)
                    parts["kept"]["bytes"] += len(raw)
                    if sink:
                        sink.write(raw)
                    continue
                if blank:                                      # a blank line is no row
                    continue
                cells = _cells(text)
                if index >= len(cells):
                    raise DataError(f"{name} line {line}: no value in column `{column}` (the row has {len(cells)} "
                                    "values); every row needs its time, or the seal cannot tell whether it is sealed")
                try:
                    t = _parse(cells[index])
                except ValueError as exc:
                    raise DataError(f"{name} line {line}, column `{column}`: {exc}. Every row needs a time the seal "
                                    "can read: a row it cannot place could be a sealed one") from None
                aware = t.tzinfo is not None
                zone = zone or (aware, line, cells[index].strip())
                if aware != zone[0]:
                    raise DataError(f"{name} line {line}, column `{column}`: {cells[index].strip()!r} "
                                    f"{'has' if aware else 'has no'} time zone but line {zone[1]}'s {zone[2]!r} "
                                    f"{'has none' if aware else 'has one'}; write every time in the file the same way")
                _row(parts["file"], t)
                part = parts["kept"] if cut is None or _before(t, cut) else parts["sealed"]
                part["hash"].update(raw)
                part["bytes"] += len(raw)
                _row(part, t)
                if part is parts["kept"]:
                    if sink:
                        sink.write(raw)
                    if secs is not None:
                        secs.append(_seconds(t))
        if index is None:
            raise DataError(f"{name} is empty; its first line is the header, with a time column")
        if sink:
            sink.close()
            sink = None
            tmp.replace(out)
            tmp = None
    finally:
        if sink:
            sink.close()
        if tmp is not None and tmp.exists():
            tmp.unlink()
    return {**{k: _done(p) for k, p in parts.items()}, "column": column,
            "times": array.array("d", sorted(secs)) if secs is not None else None}


def _files(target: Path, label: str, role: str, cutting: bool = True, exclude=None) -> list[tuple[str, Path]]:
    """(name in the role, path) of a role's files: the file itself (name ""), or every .csv file in its folder (hidden
    files and those left out of copies aside). A folder that is cut holds nothing else: it could hold sealed rows."""
    from .backends import excluded
    if target.is_file():
        return [("", target)]
    if not target.is_dir():
        raise DataError(f"`data.{role}`: {label} does not exist; write it before launching, or fix "
                        f"`data.{role}.path` in testbench.yaml")
    out = []
    for p in sorted(target.rglob("*")):
        rel = p.relative_to(target).as_posix()
        if not p.is_file() or any(x.startswith(".") for x in rel.split("/")) or (
                exclude and excluded(f"{label}/{rel}", exclude)):
            continue
        if p.suffix.lower() != ".csv":
            if cutting:
                raise DataError(f"`data.{role}`: {label}/{rel} is not a .csv file. A data folder holds only CSV files, "
                                "each cut by its time column, so nothing in it gets past the seal: move it out of "
                                f"{label}/")
            continue
        out.append((rel, p))
    if not out:
        raise DataError(f"`data.{role}`: the folder {label}/ has no .csv files")
    return out


def _combined(label: str, parts: list[tuple[str, dict]]) -> dict:
    """One fingerprint for a role: a file's own, or a folder's (sha256 over each file's name and sha256)."""
    if len(parts) == 1 and parts[0][0] == "":
        return {"path": label, **parts[0][1]}
    firsts = [p["first"] for _, p in parts if p["first"] is not None]
    lasts = [p["last"] for _, p in parts if p["last"] is not None]
    try:
        first, last = (min(firsts) if firsts else None), (max(lasts) if lasts else None)
    except TypeError:                                  # files with and without zones: as written
        first = min(firsts, key=lambda t: t.replace(tzinfo=None))
        last = max(lasts, key=lambda t: t.replace(tzinfo=None))
    text = "".join(f"{rel}\0{p['sha256']}\n" for rel, p in parts)
    return {"path": label, "bytes": sum(p["bytes"] for _, p in parts),
            "sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
            "rows": sum(p["rows"] for _, p in parts), "first": first, "last": last, "files": len(parts)}


def _plain(fp: dict | None) -> dict | None:
    """A fingerprint with its times as text, for JSON."""
    return None if fp is None else {k: (_text(v) if isinstance(v, dt.datetime) else v) for k, v in fp.items()}


_SCANS: dict = {}


def scan(root: Path, role: str, spec: dict, exclude=None) -> dict:
    """A role's data on this machine, read once while it is unchanged (size and modification time): {file, kept,
    sealed} fingerprints (kept: the rows before holdout_from, all of them without one) and the kept rows' times."""
    target = Path(root) / spec["path"]
    files = _files(target, spec["path"], role, exclude=exclude)
    stats = tuple((rel, p.stat().st_size, p.stat().st_mtime_ns) for rel, p in files)
    key = (str(target.resolve()), role, spec.get("time"), spec.get("holdout_from"), stats)
    if key in _SCANS:
        return _SCANS[key]
    cut = _parse(spec["holdout_from"]) if spec.get("holdout_from") else None
    results = [(rel, _pass(p, f"{spec['path']}/{rel}" if rel else spec["path"], role, spec.get("time"), cut,
                           times=True)) for rel, p in files]
    times = array.array("d", sorted(t for _, r in results for t in r["times"]))
    found = {part: _combined(spec["path"], [(rel, r[part]) for rel, r in results])
             for part in ("file", "kept", "sealed")}
    found.update(column=results[0][1]["column"], times=times, cut=cut)
    if len(_SCANS) > 32:
        _SCANS.clear()
    _SCANS[key] = found
    return found


# ---- walk-forward windows ----------------------------------------------------------------------------------------

def _length(text: str) -> tuple[int, str]:
    m = DURATION.fullmatch(text)
    return int(m.group(1)), m.group(2)


def _shift(t: dt.datetime, length: str, times: int = 1) -> dt.datetime:
    n, unit = _length(length)
    n *= times
    if unit in ("d", "w"):
        return t + dt.timedelta(days=n * (7 if unit == "w" else 1))
    months = t.month - 1 + n * (12 if unit == "y" else 1)
    year, month = t.year + months // 12, months % 12 + 1
    return t.replace(year=year, month=month, day=min(t.day, calendar.monthrange(year, month)[1]))


def _floor(t: dt.datetime, months: bool) -> dt.datetime:
    t = t.replace(hour=0, minute=0, second=0, microsecond=0)
    return t.replace(day=1) if months else t


def cut_windows(wf: dict, found: dict, where: str = "walk_forward") -> list[dict]:
    """The windows of a walk-forward over a role's rows before the seal, oldest first. The first starts at `start:`,
    else at the first row's day (its month's first day when a length is in months or years); window k trains on
    [start + k step, + train) - from start itself when anchored - and tests on the `test` after it. Windows are kept
    while the test ends at or before the end of the rows: the day (month) after the last row, or holdout_from."""
    kept = found["kept"]
    if not kept["rows"]:
        raise DataError(f"`{where}`: {found['kept']['path']} has no rows before the seal to cut windows from")
    months = any(_length(wf[k])[1] in ("mo", "y") for k in ("train", "test", "step"))
    first, last = kept["first"], kept["last"]
    start = _like(_parse(wf["start"]), first) if wf.get("start") else _floor(first, months)
    end = _shift(_floor(last, months), "1mo" if months else "1d")
    if found.get("cut") is not None:
        end = min(end, _like(found["cut"], first))
    windows = []
    while True:
        base = _shift(start, wf["step"], len(windows))
        train_end = _shift(base, wf["train"])
        test_end = _shift(train_end, wf["test"])
        if test_end > end:
            break
        if len(windows) == MAX_WINDOWS:
            raise DataError(f"`{where}`: step {wf['step']} cuts more than {MAX_WINDOWS} windows from {kept['path']}; "
                            "a walk-forward runs every step once per window, so use a longer step (e.g. "
                            f"{_length(wf['test'])[0]}{_length(wf['test'])[1]}, the test's length)")
        windows.append((start if wf.get("anchored") else base, train_end, test_end))
    span = (f"{kept['path']} has rows from {_text(first)} to {_text(last)}"
            + (f" before the seal at {_text(found['cut'])}" if found.get("cut") is not None else ""))
    if len(windows) < 2:
        raise DataError(f"`{where}`: {span}, and train {wf['train']} then test {wf['test']}, stepping {wf['step']} "
                        f"from {_text(start)}, fits {counted(len(windows), 'window')} by {_text(end)}; a "
                        "walk-forward needs at least 2. Shorten train or test (e.g. train: "
                        f"{max(1, _length(wf['train'])[0] // 2)}{_length(wf['train'])[1]}), take a shorter step, or "
                        "use more data")
    times, out = found["times"], []
    count = lambda a, b: bisect.bisect_left(times, _seconds(b)) - bisect.bisect_left(times, _seconds(a))  # noqa: E731
    for k, (train_start, train_end, test_end) in enumerate(windows, 1):
        out.append({"k": k, "n": len(windows), "train_start": _text(train_start), "train_end": _text(train_end),
                    "test_start": _text(train_end), "test_end": _text(test_end),
                    "train_rows": count(train_start, train_end), "test_rows": count(train_end, test_end)})
    return out


def windows_of(cfg: dict, plan_name: str) -> list[dict]:
    """A walk-forward plan's windows, cut from its data on this machine ([] for any other plan); a DataError, with the
    fix, when the data cannot be read or fits fewer than 2."""
    wf = cfg["plans"][plan_name].get("walk_forward")
    if not wf:
        return []
    found = scan(Path(cfg["root"]), wf["data"], cfg["data"][wf["data"]], cfg.get("exclude"))
    return cut_windows(wf, found, f"plans.{plan_name}.walk_forward")


def window_count(cfg: dict, plan_name: str) -> int:
    """How many times a plan's steps run: once per walk-forward window, else once (budget.worst_case uses it)."""
    return len(windows_of(cfg, plan_name)) or 1


# ---- launch: the plan as it will run, and the record of its data -------------------------------------------------

def prepare(root: Path, cfg: dict, plan_name: str) -> dict | None:
    """What a launch of the plan needs from the data (None for a project without data:): {plan} - the plan.json to
    run, with each role's cut and, for a walk-forward, a copy of the steps per window - {data} - the record of every
    role at launch (report.json's `data`) - and {opening}, whether it opens the holdout. A DataError with the fix
    when a file cannot be read, or a cut leaves the steps nothing."""
    data = cfg.get("data") or {}
    if not data:
        return None
    plan = copy.deepcopy(cfg["plans"][plan_name])
    opening = bool(plan.get("holdout"))
    wf = plan.get("walk_forward")
    record, view = {}, {}
    for role, spec in data.items():
        found = scan(Path(root), role, spec, cfg.get("exclude"))
        cut = None if opening else spec["holdout_from"]
        if cut and not found["kept"]["rows"]:
            raise DataError(f"`data.{role}`: every row of {spec['path']} is at or after holdout_from {cut}, so plan "
                            f"{plan_name} would see none; move holdout_from later (the rows before it are the ones "
                            "plans tune on)")
        seen = found["kept"] if cut or not spec["holdout_from"] else found["file"]
        seen = {k: v for k, v in seen.items() if k != "path"}
        record[role] = {"path": spec["path"], "time": found["column"], "holdout_from": spec["holdout_from"],
                        "view": "windows" if wf else "sealed" if cut else "full",
                        **_plain(seen), "file": _plain({k: v for k, v in found["file"].items() if k != "path"}),
                        "sealed": _plain({k: v for k, v in found["sealed"].items() if k != "path"})
                        if spec["holdout_from"] else None}
        if spec["holdout_from"] and not found["sealed"]["rows"]:
            record[role]["note"] = (f"no row is at or after holdout_from {spec['holdout_from']} yet (the last is "
                                    f"{_text(found['file']['last'])}), so nothing is sealed")
        view[role] = {"path": spec["path"], "time": spec["time"], "cut": cut}
    plan["data"] = view
    if wf:
        windows = windows_of(cfg, plan_name)
        plan["walk_forward"] = {**wf, "windows": windows}
        steps = []
        for w in windows:
            env = {"TESTBENCH_WINDOW": f"{w['k']}/{w['n']}", "TESTBENCH_TRAIN_START": w["train_start"],
                   "TESTBENCH_TRAIN_END": w["train_end"], "TESTBENCH_TEST_START": w["test_start"],
                   "TESTBENCH_TEST_END": w["test_end"]}
            steps += [{**copy.deepcopy(s), "name": f"{s['name']} (window {w['k']}/{w['n']})",
                       "env": {**s["env"], **env}, "window": w["k"]} for s in plan["steps"]]
        plan["steps"] = steps
    return {"plan": plan, "data": record, "opening": opening}


# ---- the holdout's openings: .testbench/holdout.jsonl ------------------------------------------------------------

def openings(root: Path) -> list[dict]:
    """Every opening of the holdout recorded in the project; a line that cannot be read counts as an opening of
    everything (it cannot be told apart)."""
    p = Path(root) / HOLDOUT_LOG
    if not p.exists():
        return []
    out = []
    for line in p.read_text(errors="replace").splitlines():
        if line.strip():
            try:
                e = json.loads(line)
            except ValueError:
                e = None
            out.append(e if isinstance(e, dict) else {"unreadable": line[:200]})
    return out


def opened_before(root: Path, cfg: dict) -> list[dict]:
    """The earlier openings that saw rows still sealed now: those whose file reached into the current holdout period
    (its last time at or after holdout_from). Data that arrived after every opening, sealed by a later holdout_from,
    was never opened, so the count starts again."""
    sealed = {role: _parse(s["holdout_from"]) for role, s in cfg["data"].items() if s["holdout_from"]}
    out = []
    for e in openings(root):
        roles = e.get("data")
        if not isinstance(roles, dict):
            out.append(e)
            continue
        for role, cut in sealed.items():
            seen = roles.get(role)
            if not isinstance(seen, dict):
                continue
            try:
                last = _parse(seen.get("last"))
            except ValueError:
                last = None
            if last is None or not _before(last, cut):
                out.append(e)
                break
    return out


def holdout(root: Path, cfg: dict, plan_name: str) -> tuple[dict, str | None]:
    """The holdout record of a launch of the holdout plan ({opened_before, max_opens, first, last, roles}), and the
    refusal when holdout.max_opens has been reached (None to go ahead). Called under the launch lock."""
    plan = cfg["plans"][plan_name]
    before = opened_before(root, cfg)
    most = plan["holdout"].get("max_opens")
    roles = {role: s["holdout_from"] for role, s in cfg["data"].items() if s["holdout_from"]}
    brief = lambda e: {"run": e.get("run"), "time": e.get("time")} if e else None    # noqa: E731
    rec = {"opened_before": len(before), "max_opens": most, "first": brief(before[0] if before else None),
           "last": brief(before[-1] if before else None), "roles": roles}
    if most is None or len(before) < most:
        return rec, None
    what = " and ".join(f"{role} from {cut}" for role, cut in roles.items())
    seen = [(_parse(r["last"]), r["last"]) for e in before for r in (e.get("data") or {}).values()
            if isinstance(r, dict) and _readable(r.get("last"))]
    latest = max(seen, key=lambda x: x[0].replace(tzinfo=None))[1] if seen else None
    first = before[0]
    move = (f"wait for data newer than {latest} and move holdout_from past it (rows no opening has seen start the "
            "count again)" if latest else "seal rows no opening has seen, with a later holdout_from")
    return rec, (f"Refused: plan {plan_name} opens the holdout (the sealed rows: {what}), which was opened "
                 f"{counted(len(before), 'time')} already (first in run {first.get('run', '?')} at "
                 f"{first.get('time', '?')}), and plans.{plan_name}.holdout.max_opens is {most}. A holdout is an "
                 "honest test only while nothing was tuned on it. Nothing ran and nothing was spent. To test on "
                 f"untouched rows, {move}; or have a person raise holdout.max_opens in testbench.yaml (every report "
                 "still says how many times the holdout was opened).")


def _readable(text) -> bool:
    try:
        _parse(text)
        return True
    except (ValueError, TypeError):
        return False


def record_opening(root: Path, run_id: str, plan_name: str, source: dict | None, record: dict) -> None:
    """A line in .testbench/holdout.jsonl for a launch of the holdout plan: the run, when, on which code, and which
    data (the file's sha256 and last time, the sealed part's sha256)."""
    line = {"time": dt.datetime.now().isoformat(timespec="seconds"), "run": run_id, "plan": plan_name,
            "commit": (source or {}).get("commit"), "dirty": (source or {}).get("dirty"),
            "data": {role: {"path": r["path"], "holdout_from": r["holdout_from"], "sha256": r["file"]["sha256"],
                            "last": r["file"]["last"], "sealed_sha256": (r["sealed"] or {}).get("sha256"),
                            "sealed_rows": (r["sealed"] or {}).get("rows")}
                     for role, r in record.items() if r["holdout_from"]}}
    p = Path(root) / HOLDOUT_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(line) + "\n")


# ---- where the plan runs: the cut in the run's copy -------------------------------------------------------------

class Seal:
    """The data files in the run's copy of the project, cut to what the plan may see: the rows before holdout_from,
    and in a walk-forward window the rows before its test end. begin() before the first step, window(k) before each
    window's steps, close() at the end."""

    def __init__(self, plan: dict, workdir: Path):
        self.plan, self.workdir = plan, Path(workdir)
        self.roles = plan.get("data") or {}
        self.windows = {w["k"]: w for w in (plan.get("walk_forward") or {}).get("windows") or []}
        self.stash: Path | None = None
        self.current, self.seen, self.files = None, {}, {}

    def env(self) -> dict:
        return {env_name(role): str((self.workdir / r["path"]).resolve()) for role, r in self.roles.items()}

    def begin(self) -> dict:
        """Cut every sealed file in the copy; {role: fingerprint} of what the steps see."""
        cutting = bool(self.windows) or any(r.get("cut") for r in self.roles.values())
        if cutting and self.plan.get("workdir") != "copy":
            raise DataError("this plan's data would be cut in the project folder itself; the seal cuts files only in "
                            "a copy of the project (workdir: copy, which testbench.yaml sets for every plan that sees "
                            "cut data). Launch it with `testbench run`, which writes the plan.json that runs")
        if self.windows:
            self.stash = Path(tempfile.mkdtemp(prefix="testbench-data-"))
        out = {}
        for role, r in self.roles.items():
            cut = _parse(r["cut"]) if r.get("cut") else None
            parts = []
            self.files[role] = _files(self.workdir / r["path"], r["path"], role,
                                      cutting=cut is not None or bool(self.windows))
            for rel, p in self.files[role]:
                found = _pass(p, f"{r['path']}/{rel}" if rel else r["path"], role, r.get("time"), cut,
                              out=p if cut is not None else None)
                parts.append((rel, found["kept"]))
                if self.stash:                                 # the rows before the seal, which windows are cut from
                    keep = self.stash / role / (rel or "data.csv")
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, keep)
            out[role] = _plain(_combined(r["path"], parts))
        self.seen = out
        return out

    def window(self, k: int) -> dict:
        """Cut every role to the rows before window k's test end, from the rows before the seal; {role: fingerprint}."""
        if k == self.current:
            return self.seen
        cut = _parse(self.windows[k]["test_end"])
        out = {}
        for role, r in self.roles.items():
            parts = []
            for rel, p in self.files[role]:
                found = _pass(self.stash / role / (rel or "data.csv"), f"{r['path']}/{rel}" if rel else r["path"],
                              role, r.get("time"), cut, out=p)
                parts.append((rel, found["kept"]))
            out[role] = _plain(_combined(r["path"], parts))
        self.current, self.seen = k, out
        return out

    def close(self) -> None:
        if self.stash:
            shutil.rmtree(self.stash, ignore_errors=True)
            self.stash = None


def windows_report(plan: dict, steps: list[dict], seen: dict) -> list[dict]:
    """A line per window for the report: its dates and rows, what its data was, and whether every step of it
    passed (skipped when none ran)."""
    windows = (plan.get("walk_forward") or {}).get("windows") or []
    per = len(plan["steps"]) // max(1, len(windows))
    out = []
    for w in windows:
        ran = [s for s in steps if s.get("window") == w["k"]]
        bad = next((s["status"] for s in ran if s["status"] != "passed"), None)
        status = "skipped" if not ran else bad or ("passed" if len(ran) == per else "failed")
        out.append({**{k: v for k, v in w.items()}, "window": f"{w['k']}/{w['n']}", "status": status,
                    "steps": [s["name"] for s in ran], "data": seen.get(w["k"])})
    return out


def sealed_view(cfg: dict) -> dict:
    """What anything but the holdout plan sees of the project's data, as a plan.json's `data` (for cut_copies): a
    live session's copy of the project is sealed like a plan's."""
    return {"data": {role: {"path": s["path"], "time": s["time"], "cut": s["holdout_from"]}
                     for role, s in (cfg.get("data") or {}).items() if s["holdout_from"]}}


@contextlib.contextmanager
def cut_copies(root: Path, plan: dict):
    """The sealed files of a plan.json cut on this machine, in a temporary folder: {path in the project: cut file}.
    A backend that copies the project to its machine sends these instead, so the sealed rows never leave here."""
    roles = {role: r for role, r in (plan.get("data") or {}).items() if r.get("cut")}
    if not roles:
        yield {}
        return
    tmp = Path(tempfile.mkdtemp(prefix="testbench-sealed-"))
    try:
        out = {}
        for role, r in roles.items():
            for rel, p in _files(Path(root) / r["path"], r["path"], role):
                name = f"{r['path']}/{rel}" if rel else r["path"]
                dest = tmp / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                _pass(p, name, role, r.get("time"), _parse(r["cut"]), out=dest)
                out[name] = dest
        yield out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- after the run: the report's record, verify and diff ---------------------------------------------------------

def merge(launch: dict | None, seen: dict | None) -> dict | None:
    """report.json's `data`: the record made at launch, with what the steps saw as measured where they ran."""
    if not launch:
        return None
    out = copy.deepcopy(launch)
    for role, rec in out.items():
        s = (seen or {}).get(role) if isinstance(seen, dict) else None
        if not isinstance(s, dict):
            continue
        if s.get("sha256") != rec.get("sha256"):
            rec["note"] = ("the data the steps saw is not what was read at launch (sha256 "
                           f"{str(rec.get('sha256'))[:12]} then, {str(s.get('sha256'))[:12]} where the plan ran): the "
                           "file changed in between")
        rec.update({k: s.get(k) for k in ("bytes", "sha256", "rows", "first", "last")})
    return out


def file_sha256(root: Path, rec: dict) -> str | None:
    """The sha256 of a role's file (or folder) now, as the record's `file.sha256` was made; None when it is gone."""
    target = Path(root) / str(rec.get("path"))
    try:
        if target.is_file():
            return _sha_bytes(target)
        if target.is_dir():
            files = _files(target, str(rec.get("path")), "?", cutting=False)
            if len(files) == 1 and files[0][0] == "":
                return _sha_bytes(files[0][1])
            text = "".join(f"{rel}\0{_sha_bytes(p)}\n" for rel, p in files)
            return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    except (OSError, DataError):
        return None
    return None


def _sha_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(root: Path, report: dict) -> tuple[list[str], list[str]]:
    """(problems, notes) for a run's data now: a data file whose sha256 is not the one the run recorded is a problem
    (the run's numbers are about other data); one that is gone is a note (it cannot be compared)."""
    problems, notes = [], []
    data = report.get("data")
    for role, rec in (data.items() if isinstance(data, dict) else ()):
        if not isinstance(rec, dict) or not isinstance(rec.get("file"), dict):
            continue
        then, path = rec["file"].get("sha256"), rec.get("path")
        now = file_sha256(root, rec)
        if now is None:
            notes.append(f"{path} ({role}) is not here any more, so it cannot be compared with the run's (sha256 "
                         f"{str(then)[:16]})")
        elif now != then:
            problems.append(f"{path} changed since this run: the run's numbers are about other data (sha256 "
                            f"{str(then)[:16]} then, {now[:16]} now)")
        else:
            notes.append(f"{path} ({role}) is the data the run read (sha256 {now[:16]})")
    return problems, notes
