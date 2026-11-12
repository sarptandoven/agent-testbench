"""testbench repro: does the same plan leave the same evidence again - here, on another machine, for someone else?

    testbench repro PLAN [--times N] [--on BACKEND]... [--approve]   # run it N times (default 2), then compare
    testbench repro --compare RUN RUN [RUN...]                       # runs that exist: ids, run folders, shared bundles

Each run of a repro is a normal launch (runs.launch) in a fresh copy of the project, as remote runs and loop tries
are, so no run sees what another left behind; they go one after another. The budget sees every one of them: their
total is checked up front, and above ask_above_usd a person approves the total once (budget.check_together) - never
run by run, which would hide the total behind small questions. --on BACKEND sends the runs there instead of the
plan's own backend; given more than once, the runs take turns (--on local --on docker: one run on each).

Then every evidence file is compared across the runs by the sha256 each run recorded: identical, differs, or missing
in some. A file that differs is read to say how:

    text      the first line that differs, and how many lines do
    JSON      which keys differ, and numbers by how much (relative difference)
    CSV       which rows and columns
    images    the mean pixel difference (expect.image_diff)
    other     the first byte that differs

and why, when it can tell (REASONS): times or durations in the content, absolute paths, ids made fresh each run, the
same content in another order or encoded differently, numbers that differ in their last digits or by small relative
amounts (a random seed not fixed), the platform (the runs' environments differ: Python, OS, machine, packages), or
results that are simply not the same. The runs' environments, code and data are compared first.

A step's outputs (artifacts, images, executed notebooks, downloads) decide the verdict: IDENTICAL, the SAME apart from
times, paths, ids, order or encoding, or DIFFERENT. testbench's own records of a step (log.txt, result.json,
data.json) are compared too, and shown apart. Each comparison is kept in .testbench/repro/<id>/ (repro.json and
repro.md), which `testbench ui` lists.

The `deterministic:` expectation reads a difference the same way, for files that must be byte for byte what the
latest passing run of the plan on the same code (and data) made: runs.launch gives each such step that run's hashes
(with_reference), and check_deterministic compares the step's files with them where the step runs.
"""
from __future__ import annotations

import copy
import csv
import datetime as dt
import difflib
import glob
import hashlib
import io
import json
import math
import os
import platform
import re
import sys
import tempfile
from pathlib import Path

from . import budget, config as C, report as R, runs
from .config import counted

FORMAT = 1
SHOWN = 6                              # files, keys, cells or rows named before "and N more"
READ_LIMIT = 32 * 1024 * 1024          # a file larger than this is compared by its sha256 only
LINE_DIFF_LIMIT = 5000                 # lines difflib aligns; past that, lines are compared position by position
ROUNDING = 1e-6                        # a relative difference up to this is floating-point rounding
SMALL = 0.1                            # ... up to this, a random seed not fixed (or the platform); past it, results

REASONS = {          # each reason, in plain words: why a file is not the same in two runs
    "timestamps": "times or durations in the content",
    "paths": "absolute paths in the content (the folder it ran in)",
    "ids": "ids made fresh each run (memory addresses, uuids, process ids)",
    "ordering": "the same content in another order",
    "encoding": "the same content, encoded differently (spacing, number format, image metadata or compression)",
    "rounding": "numbers that differ in their last digits: floating-point rounding (the order of a sum, threads)",
    "random": "numbers that differ by small relative amounts: a random seed not fixed",
    "platform": "the platform: the runs' environments differ, and so do the numbers or pixels",
    "values": "results that are not the same: numbers differ by more than a little",
    "content": "content that is not the same",
    "missing": "made in some runs and not in others",
    "unread": "not read: too large, or its content is not here (left out of a shared bundle)",
}
BENIGN = ("timestamps", "paths", "ids", "ordering", "encoding")     # the same results, written down differently
SEVERITY = ("missing", "content", "values", "unread", "platform", "random", "rounding", "ordering", "ids", "paths",
            "timestamps", "encoding")
FIX = {
    "timestamps": "keep times and durations out of files that must be the same (print them, or write them to a file "
                  "of their own)",
    "paths": "write paths relative to the project folder (os.path.relpath), not where it happened to run",
    "ids": "leave object reprs, uuids and process ids out of outputs, or make them from a fixed seed",
    "ordering": "sort before writing (sorted(), df.sort_values(), json.dump(..., sort_keys=True)): set and dict order, "
                "os.listdir and parallel workers change it",
    "encoding": "write it the same way every time (a fixed format; for images, compare them with `image:` instead, "
                "or save without metadata: savefig(..., metadata={'Software': None}))",
    "rounding": "fix the order of floating-point work (one thread for BLAS: OMP_NUM_THREADS=1, "
                "torch.use_deterministic_algorithms(True)), or check those numbers with a tolerance (\"~= x +- t\")",
    "random": "fix every seed the code uses (random.seed, numpy.random.seed or default_rng(seed), torch.manual_seed, "
              "PYTHONHASHSEED) and pass it to anything that samples",
    "platform": "pin the versions (a lock file) and run on one platform, or check these numbers with a tolerance "
                "(\"~= x +- t\") rather than byte for byte",
    "values": "find what the code reads that changes between runs: a seed, the clock, the network, a file outside "
              "the project, data that moved",
    "content": "look at the two versions side by side (each run's folder has its copy under steps/)",
    "missing": "a step made the file in one run and not in another: read the step's log.txt in the run without it",
    "unread": "compare the two copies yourself, or keep a smaller file as evidence",
}
VERDICT = {"identical": "IDENTICAL", "same": "THE SAME", "different": "NOT REPRODUCED", "nothing": "NOTHING TO COMPARE"}
RECORDS = ("log.txt", "result.json", "data.json", "upload_queue.json", "mock_calls.jsonl", "downloads/downloads.json")
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff")
TIME_WORDS = {"time", "times", "timestamp", "timestamps", "date", "datetime", "stamp", "clock", "elapsed", "duration",
              "took", "started", "finished", "created", "updated", "modified", "seconds", "secs", "sec", "minutes",
              "mins", "ms", "at", "when", "runtime", "walltime", "wall", "now", "today"}

PATHS = re.compile(r"(?<![\w.:/\\~-])(?:[A-Za-z]:[\\/]|~/|/)(?:[\w.@+~-]+[\\/])+[\w.@+~-]*")
IDS = re.compile(r"\b0x[0-9a-fA-F]{6,}\b|\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                 r"[0-9a-fA-F]{12}\b|(?i:\bpid\W{0,3}\d+)")
TIMES = re.compile("|".join([
    r"\b\d{4}-\d{2}-\d{2}(?:[T _]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",     # ISO dates, times
    r"\b\d{8}[-_T]\d{6}\b",                                                              # 20261112-101500
    r"\b\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\b",                                         # 10:15:00, 00:01
    r"\b1[5-9]\d{8}(?:\.\d+)?\b|\b1[5-9]\d{11}\b",                                       # seconds or ms since 1970
    r"\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\s?(?:ns|us|µs|ms|s|secs?|seconds?|mins?|minutes?|h|hrs?|hours?)\b",
    r"\b\d+(?:\.\d+)?\s?[A-Za-z]+/s\b",                                                  # 12.3it/s, 40 MB/s
    r"(?i:\b(?:took|elapsed|time|duration|runtime|wall(?:time)?|seconds|secs|minutes)\b)[\s:=]{0,3}[-+]?\d+(?:\.\d+)?",
]))
MASKS = {"paths": PATHS, "ids": IDS, "timestamps": TIMES}          # applied in this order
NUMBER = re.compile(r"(?<![\w.])[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?(?!\w)")


class ReproError(ValueError):
    """Nothing to compare, or nothing launched: why, and what to do. code 2: refused or bad input."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


# ---- reading a difference -------------------------------------------------------------------------------------

def _rel(a: float, b: float) -> float:
    top = max(abs(a), abs(b))
    return 0.0 if top == 0 else abs(a - b) / top


def rel_text(r: float) -> str:
    """A relative difference in words: 0.27%, or 3e-07 relative when it is tiny."""
    return f"{r * 100:.3g}%" if r >= 1e-4 else f"{r:.2g} relative"


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def time_key(key: str) -> bool:
    """A key or column named for a time or a duration: started_at, elapsed_s, trainTime, minutes."""
    return any(w.lower() in TIME_WORDS for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", str(key)))


def _number_reason(r: float, same_env: bool, key: str = "", a=None) -> str:
    if time_key(key) or (_num(a) and 1.5e9 <= abs(a) < 2e9 and r < 1e-3):         # or seconds since 1970
        return "timestamps"
    reason = "rounding" if r <= ROUNDING else "random" if r <= SMALL else "values"
    return "platform" if not same_env and reason in ("rounding", "random") else reason


def _mask(text: str, classes) -> str:
    for name, rx in MASKS.items():
        if name in classes:
            text = rx.sub(f"<{name}>", text)
    return text


def _same(a: str, b: str, any_order: bool) -> bool:
    return a == b or any_order and sorted(a.splitlines()) == sorted(b.splitlines())


def _numbers(text: str) -> tuple[str, list[float]]:
    found = []

    def keep(m):
        try:
            found.append(float(m.group(0)))
        except ValueError:
            return m.group(0)
        return "#"
    return NUMBER.sub(keep, text), found


def _drift(a: str, b: str) -> list[tuple[float, float, float]] | None:
    """The numbers that differ, when the two texts are the same but for their numbers; else None."""
    sa, na = _numbers(a)
    sb, nb = _numbers(b)
    if sa != sb or len(na) != len(nb):
        return None
    return [(x, y, _rel(x, y)) for x, y in zip(na, nb) if x != y and math.isfinite(x) and math.isfinite(y)]


def classify(a: str, b: str, same_env: bool = True) -> tuple[list[str], dict]:
    """Why two texts differ: (reasons, most telling first; what was measured). [] when they are the same."""
    if a == b:
        return [], {}
    la, lb = a.splitlines(), b.splitlines()
    if la == lb:                                         # line endings, or a newline at the end
        return ["encoding"], {}
    if sorted(la) == sorted(lb):
        return ["ordering"], {}
    every = tuple(MASKS)
    rest = lambda c: [x for x in every if x != c]                                            # noqa: E731
    ma, mb = _mask(a, every), _mask(b, every)
    for any_order in (False, True):
        if _same(ma, mb, any_order):                     # the same once times, paths and ids are blanked out: which
            used = [c for c in every if not _same(_mask(a, rest(c)), _mask(b, rest(c)), any_order)]
            used = used or [c for c in every if _mask(a, [c]) != a or _mask(b, [c]) != b][:1]
            return used + ["ordering"] * any_order, {}
    shuffled = False
    drift = _drift(ma, mb)
    if drift is None:                                    # the same lines but for their numbers, in another order?
        drift, shuffled = _drift(_by_skeleton(ma), _by_skeleton(mb)), True
    if drift is None:
        return ["content"], {}
    if not drift:                                        # the same numbers, written another way (1.0 and 1.00)
        return ["encoding"] + ["ordering"] * shuffled, {}
    worst = max(r for _, _, r in drift)
    arrange = _by_skeleton if shuffled else (lambda text: text)

    def needed(c: str) -> bool:                          # leaving c's matches in makes more of the text differ
        without = _drift(arrange(_mask(a, rest(c))), arrange(_mask(b, rest(c))))
        return without is None or len(without) > len(drift)
    return [_number_reason(worst, same_env)] + [c for c in every if needed(c)] + ["ordering"] * shuffled, {
        "numbers": len(drift), "max_relative": worst, "examples": [[x, y, r] for x, y, r in drift[:SHOWN]]}


def _by_skeleton(text: str) -> str:
    """The lines sorted by what they say apart from their numbers, so lines that moved line up again."""
    return "\n".join(sorted(text.splitlines(), key=lambda line: _numbers(line)[0]))


def _short(x, n: int = 80) -> str:
    text = x if isinstance(x, str) else json.dumps(x, default=str)
    text = " ".join(text.split("\n"))
    return text if len(text) <= n else text[:n - 3] + "..."


def _lines(la: list[str], lb: list[str]) -> dict:
    """The first line that differs (1-based, in A) and how many lines differ."""
    if len(la) + len(lb) <= LINE_DIFF_LIMIT:
        ops = [op for op in difflib.SequenceMatcher(None, la, lb, autojunk=False).get_opcodes() if op[0] != "equal"]
        if not ops:
            return {"line": None, "lines_differ": 0}
        _, i1, i2, j1, j2 = ops[0]
        n = sum(max(x2 - x1, y2 - y1) for _, x1, x2, y1, y2 in ops)
        return {"line": i1 + 1, "a": la[i1] if i1 < len(la) else None, "b": lb[j1] if j1 < len(lb) else None,
                "lines_differ": n}
    wrong = [i for i, (x, y) in enumerate(zip(la, lb)) if x != y]
    n = len(wrong) + abs(len(la) - len(lb))
    i = wrong[0] if wrong else min(len(la), len(lb))
    return {"line": i + 1, "a": la[i] if i < len(la) else None, "b": lb[i] if i < len(lb) else None,
            "lines_differ": n}


def _n(x) -> str:
    return f"{x:.12g}" if isinstance(x, float) else str(x)


def _numbers_said(m: dict) -> str:
    if not m.get("numbers"):
        return ""
    ex = "; ".join(f"{_n(x)} -> {_n(y)}" for x, y, _ in m["examples"][:3])
    return (f"{counted(m['numbers'], 'number')} {'differs' if m['numbers'] == 1 else 'differ'}, by up to "
            f"{rel_text(m['max_relative'])}" + (f" ({ex}{', ...' if m['numbers'] > 3 else ''})" if ex else ""))


def _text(a: str, b: str, same_env: bool) -> dict:
    la, lb = a.splitlines(), b.splitlines()
    reasons, measured = classify(a, b, same_env)
    at = _lines(la, lb)
    said = []
    if reasons == ["ordering"]:
        said.append(f"the same {counted(len(la), 'line')}, in another order (line {at['line']} is the first that "
                    "moved)")
    elif at["line"] is not None:
        shown = lambda x: "(no line)" if x is None else repr(_short(x))                     # noqa: E731
        said.append(f"line {at['line']}: {shown(at['a'])} -> {shown(at['b'])}")
        n = at["lines_differ"]
        said.append(f"{n} of {counted(max(len(la), len(lb)), 'line')} {'differs' if n == 1 else 'differ'}")
    elif reasons:
        said.append("the same lines; only line endings or the end of the file differ")
    if measured.get("numbers"):
        said.append(_numbers_said(measured))
    return {"kind": "text", "reasons": reasons or ["encoding"], "summary": "; ".join(said),
            "detail": {**at, **measured}}


def _canon(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def _json_walk(a, b, key: str, out: list, same_env: bool, limit: int = 500) -> None:
    if len(out) >= limit:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in [*a, *(k for k in b if k not in a)]:
            sub = f"{key}.{k}" if key else str(k)
            if k not in b or k not in a:
                out.append({"key": sub, "a": _short(a[k]) if k in a else None, "b": _short(b[k]) if k in b else None,
                            "only_in": "A" if k in a else "B", "reasons": ["content"]})
            else:
                _json_walk(a[k], b[k], sub, out, same_env, limit)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) == len(b) and a != b and sorted(map(_canon, a)) == sorted(map(_canon, b)):
            out.append({"key": key or "(the top list)", "a": f"{len(a)} items", "b": "the same items, in another order",
                        "reasons": ["ordering"]})
        elif len(a) == len(b):
            for i, (x, y) in enumerate(zip(a, b)):
                _json_walk(x, y, f"{key}.{i}" if key else str(i), out, same_env, limit)
        else:
            out.append({"key": key or "(the top list)", "a": f"{len(a)} items", "b": f"{len(b)} items",
                        "reasons": ["content"]})
        return
    if _num(a) and _num(b):
        if a != b:
            r = _rel(float(a), float(b))
            out.append({"key": key, "a": a, "b": b, "relative": r,
                        "reasons": [_number_reason(r, same_env, key.rsplit(".", 1)[-1], a)]})
        return
    if isinstance(a, str) and isinstance(b, str):
        if a != b:
            reasons, measured = classify(a, b, same_env)
            out.append({"key": key, "a": _short(a), "b": _short(b), "reasons": reasons or ["content"],
                        **({"relative": measured["max_relative"]} if measured.get("numbers") else {})})
        return
    if type(a) is not type(b) or a != b:
        out.append({"key": key, "a": _short(a), "b": _short(b), "reasons": ["content"]})


def _key_line(d: dict) -> str:
    if d.get("only_in"):
        return f"{d['key']} only in {d['only_in']}"
    rel = f" ({rel_text(d['relative'])})" if isinstance(d.get("relative"), float) else ""
    a, b = (_n(d[k]) if _num(d[k]) else _short(d[k]) for k in ("a", "b"))
    return f"{d['key']}: {a} -> {b}{rel}"


def _json(a, b, same_env: bool) -> dict:
    diffs: list = []
    _json_walk(a, b, "", diffs, same_env)
    if not diffs:                                        # the same data: keys in another order, or other spacing
        ordered = json.dumps(a, default=str) != json.dumps(b, default=str)
        return {"kind": "json", "reasons": ["ordering" if ordered else "encoding"],
                "summary": "the same data: " + ("keys in another order" if ordered else "written differently "
                                                "(spacing or number format)"), "detail": {"keys_differ": 0}}
    reasons = _merge([d["reasons"] for d in diffs])
    rels = [d["relative"] for d in diffs if isinstance(d.get("relative"), float) and "timestamps" not in d["reasons"]]
    shown = "; ".join(_key_line(d) for d in diffs[:SHOWN]) + (f"; and {len(diffs) - SHOWN} more" if len(diffs) > SHOWN
                                                               else "")
    return {"kind": "json", "reasons": reasons,
            "summary": f"{counted(len(diffs), 'key')} {'differs' if len(diffs) == 1 else 'differ'}"
                       + (f", numbers by up to {rel_text(max(rels))}" if rels else "") + f": {shown}",
            "detail": {"keys_differ": len(diffs), "keys": diffs[:50],
                       **({"max_relative": max(rels)} if rels else {})}}


def _is_number(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except (TypeError, ValueError):
        return False


def _csv(a: str, b: str, delimiter: str, same_env: bool) -> dict:
    ra = list(csv.reader(io.StringIO(a), delimiter=delimiter))
    rb = list(csv.reader(io.StringIO(b), delimiter=delimiter))
    if ra == rb:
        return {"kind": "csv", "reasons": ["encoding"], "summary": "the same rows and cells, written differently "
                                                                    "(quoting or line endings)", "detail": {}}
    header = ra[0] if ra and not all(_is_number(c) for c in ra[0] if c.strip()) else None
    if header is not None and rb and rb[0] != header:
        reasons = ["ordering"] if sorted(header) == sorted(rb[0]) else ["content"]
        return {"kind": "csv", "reasons": reasons,
                "summary": f"the columns differ: {_short(', '.join(header))} -> {_short(', '.join(rb[0]))}",
                "detail": {"columns_a": header, "columns_b": rb[0]}}
    body_a, body_b = (ra[1:], rb[1:]) if header is not None else (ra, rb)
    if sorted(map(tuple, body_a)) == sorted(map(tuple, body_b)):
        return {"kind": "csv", "reasons": ["ordering"], "detail": {"rows": len(body_a)},
                "summary": f"the same {counted(len(body_a), 'row')}, in another order"}
    if len(body_a) != len(body_b):
        return {"kind": "csv", "reasons": ["content"], "summary": f"{counted(len(body_a), 'row')} -> {len(body_b)}",
                "detail": {"rows_a": len(body_a), "rows_b": len(body_b)}}
    cells, rows, columns = [], [], []
    for i, (x, y) in enumerate(zip(body_a, body_b), 1):
        if x == y:
            continue
        rows.append(i)
        for j in range(max(len(x), len(y))):
            u, v = (x[j] if j < len(x) else None), (y[j] if j < len(y) else None)
            if u == v:
                continue
            col = header[j] if header is not None and j < len(header) else f"column {j + 1}"
            if col not in columns:
                columns.append(col)
            cell = {"row": i, "column": col, "a": u, "b": v}
            if u is not None and v is not None and _is_number(u) and _is_number(v):
                r = _rel(float(u), float(v))
                cell.update(relative=r, reasons=[_number_reason(r, same_env, col, float(u))])
            elif u is not None and v is not None:
                cell["reasons"] = classify(u, v, same_env)[0] or ["content"]
            else:
                cell["reasons"] = ["content"]
            cells.append(cell)
    rels = [c["relative"] for c in cells if "relative" in c]
    first = cells[0] if cells else {}
    said = (f"{counted(len(rows), 'row')} of {len(body_a)} differ (row {', '.join(map(str, rows[:SHOWN]))}"
            f"{', ...' if len(rows) > SHOWN else ''}), in {', '.join(columns[:SHOWN])}"
            f"{' and more' if len(columns) > SHOWN else ''}")
    if rels:
        said += f"; numbers by up to {rel_text(max(rels))}"
    if first:
        said += f"; row {first['row']} {first['column']}: {_short(first['a'])} -> {_short(first['b'])}"
    return {"kind": "csv", "reasons": _merge([c["reasons"] for c in cells]) or ["content"], "summary": said,
            "detail": {"rows_differ": len(rows), "rows": rows[:50], "columns": columns, "cells": cells[:50],
                       **({"max_relative": max(rels)} if rels else {})}}


def _image(a: bytes, b: bytes, suffix: str, same_env: bool) -> dict:
    try:
        import PIL                                                                  # noqa: F401
    except ImportError:
        return {"kind": "image", "reasons": ["unread"], "summary": "the pictures differ by sha256; Pillow is not "
                "installed here to compare their pixels (pip install pillow)", "detail": {}}
    from .expect import image_diff
    with tempfile.TemporaryDirectory(prefix="testbench-repro-") as tmp:
        pa, pb = Path(tmp) / f"a{suffix}", Path(tmp) / f"b{suffix}"
        pa.write_bytes(a)
        pb.write_bytes(b)
        diff, note = image_diff(pb, pa)
    if diff is None:
        return {"kind": "image", "reasons": ["content"], "summary": f"the pictures could not be compared: {note}",
                "detail": {}}
    if diff == 0:
        reasons, said = ["encoding"], "the same pixels: only the file's encoding or metadata differ"
    elif diff <= 0.02:
        reasons = ["platform" if not same_env else "random"]
        said = f"nearly the same picture: mean pixel difference {diff:.4f} (0 identical, 1 opposite){note}"
    else:
        reasons = ["content"]
        said = f"the pictures differ: mean pixel difference {diff:.4f} (0 identical, 1 opposite){note}"
    return {"kind": "image", "reasons": reasons, "summary": said, "detail": {"mean_pixel_difference": round(diff, 6)}}


def _binary(a: bytes, b: bytes) -> dict:
    at = next((i for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)))
    return {"kind": "binary", "reasons": ["content"],
            "summary": f"the first difference is at byte {at} ({len(a)} and {len(b)} bytes)",
            "detail": {"first_byte": at, "bytes": [len(a), len(b)]}}


def _decode(data: bytes) -> str | None:
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def explain(a: bytes, b: bytes, path: str, same_env: bool = True) -> dict:
    """How two versions of one file differ, and why when it can tell: {kind, reason (the most telling of reasons),
    reasons, summary (one line), detail}. same_env: both were made on the same platform (Python, OS, machine,
    packages), so numbers that drift are a seed, not the machine."""
    suffix = Path(path).suffix.lower()
    if a == b:
        out = {"kind": "same", "reasons": [], "summary": "identical", "detail": {}}
    elif suffix in IMAGES:
        out = _image(a, b, suffix, same_env)
    else:
        ta, tb = _decode(a), _decode(b)
        out = None
        if ta is None or tb is None:
            out = _binary(a, b)
        elif suffix in (".json", ".ipynb", ".geojson"):
            try:
                out = _json(json.loads(ta), json.loads(tb), same_env)
            except (ValueError, RecursionError):
                out = None
        elif suffix in (".csv", ".tsv"):
            try:
                out = _csv(ta, tb, "\t" if suffix == ".tsv" else ",", same_env)
            except csv.Error:
                out = None
        out = out or _text(ta, tb, same_env)
    reasons = _merge([out["reasons"]])
    return {**out, "reasons": reasons, "reason": reasons[0] if reasons else None}


def _merge(groups) -> list[str]:
    """Every reason in groups, the most telling first."""
    found = {r for g in groups for r in g}
    return sorted(found, key=lambda r: SEVERITY.index(r) if r in SEVERITY else 0)


# ---- the runs to compare: a run here, a run folder, a shared bundle ------------------------------------------------

class Side:
    """One run to compare: its report, where it is, and its evidence files' bytes when they are here and are what the
    run recorded (a file changed since, or left out of a shared bundle, reads as None)."""

    def __init__(self, report: dict, where: str, reader, problems: list[str]):
        self.report, self.where, self._reader, self.problems = report, where, reader, problems
        self.label = str(report.get("run_id") or where)
        self.recorded = {e["path"]: e["sha256"] for e in report["evidence"]}
        self.sizes = {e["path"]: e.get("bytes") for e in report["evidence"]}
        self._cache: dict[str, bytes | None] = {}

    def read(self, path: str) -> bytes | None:
        if path in self._cache:
            return self._cache[path]
        size = self.sizes.get(path)
        if path not in self.recorded or isinstance(size, int) and size > READ_LIMIT:
            return None
        try:
            data = self._reader(path)
        except OSError:
            data = None
        if data is not None and hashlib.sha256(data).hexdigest() != self.recorded[path]:
            data = None
        if data is not None and len(data) <= 4 * 1024 * 1024:
            self._cache[path] = data
        return data


def _evidence_list(rep, name: str) -> None:
    from .provenance import check_list
    if not isinstance(rep, dict):
        raise ReproError(f"{name}: report.json is not a report testbench wrote")
    if rep.get("status") not in runs.DONE:
        raise ReproError(f"{name} has not finished ({rep.get('status')}): wait for it (`testbench wait "
                         f"{rep.get('run_id') or name}`), then compare", code=3)
    wrong = check_list(rep)
    if wrong:
        raise ReproError(f"{name}: {wrong}" if rep.get("evidence") is not None else
                         f"{name} has no recorded evidence hashes (it ran before testbench recorded them), so there is "
                         "nothing to compare by; run the plan again", code=1 if rep.get("evidence") is not None else 2)


def _folder_reader(base: Path):
    def read(path: str) -> bytes | None:
        p = base / path
        return p.read_bytes() if R.inside(base, p) else None
    return read


def load(root: Path | None, ref: str) -> Side:
    """A run to compare: a run id here (a prefix, last, last~N), a run folder, or a bundle `testbench share` made (its
    folder or its URL)."""
    from . import share
    if re.match(r"https?://", ref, re.I):
        reader = share._Url(ref)
        try:
            raw = reader.read("report.json", 64 << 20)
        except share.ShareError as exc:
            raise ReproError(str(exc)) from None
        if raw is None:
            raise ReproError(f"{reader.name}report.json is not there, so {ref} is not a run `testbench share` "
                             "published; give the address of the folder that holds index.html")
        try:
            rep = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise ReproError(f"{reader.name}report.json is not JSON: it was cut short or edited", code=1) from None
        _evidence_list(rep, ref)

        def fetch(path: str) -> bytes | None:
            try:
                return reader.read(path, READ_LIMIT)
            except share.ShareError:
                return None
        return Side(rep, reader.name, fetch, [])
    folder = Path(ref).expanduser()
    if folder.is_dir() and ((folder / share.MANIFEST).is_file() or (folder / "report.json").is_file()):
        rep = R.load(folder)
        if rep is None:
            raise ReproError(f"{folder}/report.json cannot be read: it was cut short or is not a report", code=1)
        _evidence_list(rep, str(folder))
        problems = []
        if not (folder / share.MANIFEST).is_file():
            from .provenance import verify
            problems = verify(folder, rep)
        return Side(rep, str(folder), _folder_reader(folder), problems)
    if root is None:
        raise ReproError(f"{ref!r} is not a run folder or a bundle `testbench share` made (a folder with "
                         "report.json and its evidence, or its URL), and a run id is looked up in a project: run this "
                         "in the project folder")
    try:
        run_dir = runs.resolve(root, ref)
    except FileNotFoundError as exc:
        raise ReproError(str(exc)) from None
    rep = R.load(run_dir)
    if rep is None:
        raise ReproError(f"run {run_dir.name} has no report to compare (it never started, or is still starting): "
                         f"`testbench status {run_dir.name}`", code=3 if runs.read_state(run_dir).get("status")
                         not in runs.DONE else 2)
    _evidence_list(rep, f"run {run_dir.name}")
    from .provenance import verify
    return Side(rep, "this project", _folder_reader(run_dir), verify(run_dir, rep))


# ---- comparing runs -----------------------------------------------------------------------------------------------

ENV_KEYS = ("backend", "gpu", "python", "implementation", "platform", "machine")


def _env_of(rep: dict, key: str):
    if key in ("backend", "gpu"):
        return rep.get(key)
    env = rep.get("environment")
    return env.get(key) if isinstance(env, dict) else None


def _environments(reps: list[dict]) -> dict:
    """What differs between where the runs ran: [{what, values: one per run}], the packages that changed version
    (against the first run), and for each run whether it ran where the first did."""
    rows = [{"what": k, "values": [_env_of(r, k) for r in reps]} for k in ENV_KEYS]
    rows = [r for r in rows if len({json.dumps(v) for v in r["values"]}) > 1]
    envs = [r.get("environment") if isinstance(r.get("environment"), dict) else {} for r in reps]
    packages, unknown = [], []
    first = envs[0].get("package_versions")
    for k, env in enumerate(envs[1:], 1):
        mine = env.get("package_versions")
        if isinstance(first, dict) and isinstance(mine, dict):
            for name in sorted(first.keys() | mine.keys()):
                if first.get(name) != mine.get(name):
                    packages.append({"name": name, "run": k + 1, "a": first.get(name), "b": mine.get(name)})
        elif envs[0].get("packages_sha256") and env.get("packages_sha256") and \
                envs[0]["packages_sha256"] != env["packages_sha256"]:
            unknown.append(k + 1)
    same_as_first = [True] + [not any(json.dumps(r["values"][k]) != json.dumps(r["values"][0]) for r in rows)
                              and not any(p["run"] == k + 1 for p in packages) and k + 1 not in unknown
                              for k in range(1, len(reps))]
    return {"same": not rows and not packages and not unknown, "recorded": all(envs), "differences": rows,
            "packages": packages, "packages_unknown": unknown, "same_as_first": same_as_first,
            "first": {k: _env_of(reps[0], k) for k in ENV_KEYS}}


def _code_and_data(reps: list[dict]) -> dict:
    from .diff import _code, _data
    code = [_code(reps[0], r, None) for r in reps[1:]]
    data = [_data(reps[0], r) for r in reps[1:]]
    first = reps[0].get("source") if isinstance(reps[0].get("source"), dict) else None
    same_code = all(c["same"] for c in code)
    if same_code:
        said = ("the same code in every run: commit " + str(first.get("commit"))[:12] + (
            ", with the same uncommitted changes" if first.get("dirty") else ", no uncommitted changes")
            if first else "every run ran code outside git, so whether it was the same code is not recorded")
    else:
        said = "; ".join(f"run {k + 2}: {c['summary']}" for k, c in enumerate(code) if not c["same"])
    data_changed = [k + 2 for k, d in enumerate(data) if d["changed"]]
    return {"same_code": same_code and bool(first), "code": said, "data_recorded": any(d["recorded"] for d in data),
            "data_differs_in": data_changed}


def group_of(path: str) -> str:
    """'record' for testbench's own account of a step (its log, result.json, data.json ...), 'output' for what the
    step made (artifacts, images, the executed notebook, downloads)."""
    parts = path.split("/")
    inner = "/".join(parts[2:]) if len(parts) > 2 and parts[0] == "steps" else path
    return "record" if inner in RECORDS else "output"


def compare(sides: list[Side]) -> dict:
    """Every evidence file of the runs, compared by sha256 across them, and the ones that differ read to say how:
    the data of repro.json (render() puts it in words). The first run is the one the others are compared with."""
    if len(sides) < 2:
        raise ReproError("a comparison needs at least two runs")
    reps = [s.report for s in sides]
    env = _environments(reps)
    cd = _code_and_data(reps)
    paths = sorted({p for s in sides for p in s.recorded})
    files, explained = [], {}
    for path in paths:
        hashes = [s.recorded.get(path) for s in sides]
        sizes = [s.sizes.get(path) for s in sides]
        entry = {"path": path, "group": group_of(path), "sha256": hashes, "bytes": sizes,
                 "versions": len({h for h in hashes if h})}
        if None in hashes:
            entry.update(status="missing", reasons=["missing"], reason="missing",
                         only_in=[k + 1 for k, h in enumerate(hashes) if h], summary="only in run " + ", ".join(
                             str(k + 1) for k, h in enumerate(hashes) if h) + f" of {len(sides)}")
        elif len(set(hashes)) == 1:
            entry.update(status="identical", reasons=[], reason=None)
        else:
            pairs = []
            for k in range(1, len(sides)):
                if hashes[k] == hashes[0]:
                    continue
                key = (hashes[0], hashes[k], env["same_as_first"][k])
                if key not in explained:
                    a, b = sides[0].read(path), sides[k].read(path)
                    if a is None or b is None:
                        gone = [s.label for s, x in ((sides[0], a), (sides[k], b)) if x is None]
                        explained[key] = {"kind": None, "reasons": ["unread"], "reason": "unread", "detail": {},
                                          "summary": f"by sha256 ({hashes[0][:12]} -> {hashes[k][:12]}); the content "
                                                     f"of run {' and '.join(gone)}'s copy is not here (too large, left "
                                                     "out when it was shared, or changed since the run)"}
                    else:
                        explained[key] = explain(a, b, path, env["same_as_first"][k])
                pairs.append({"run": k + 1, "run_id": sides[k].label, **explained[key]})
            reasons = _merge([p["reasons"] for p in pairs])
            entry.update(status="differs", reasons=reasons, reason=reasons[0] if reasons else "content",
                         differs_in=[p["run"] for p in pairs], summary=pairs[0]["summary"], kind=pairs[0]["kind"],
                         pairs=pairs)
        files.append(entry)
    outputs = [f for f in files if f["group"] == "output"]
    logs_as_outputs = False
    if not outputs:                                      # a plan that keeps no files: what it printed is its output
        for f in files:
            if f["path"].endswith("/log.txt"):
                f["group"], logs_as_outputs = "output", True
        outputs = [f for f in files if f["group"] == "output"]
    bad = [f for f in outputs if f["status"] == "missing" or f["status"] == "differs"
           and not set(f["reasons"]) <= set(BENIGN)]
    verdict = ("nothing" if not outputs else "identical" if all(f["status"] == "identical" for f in outputs)
               else "different" if bad else "same")
    count = lambda fs, st: sum(f["status"] == st for f in fs)                                  # noqa: E731
    records = [f for f in files if f["group"] == "record"]
    return {"repro": FORMAT, "verdict": verdict, "reproduced": verdict in ("identical", "same"),
            "plans": list(dict.fromkeys(str(r.get("plan")) for r in reps)),
            "runs": [{"run": k + 1, "run_id": s.label, "plan": s.report.get("plan"), "status": s.report.get("status"),
                      "backend": s.report.get("backend"), "gpu": s.report.get("gpu"), "where": s.where,
                      "started": s.report.get("started"), "evidence_sha256": s.report.get("evidence_sha256"),
                      "commit": (s.report.get("source") or {}).get("commit") if isinstance(s.report.get("source"), dict)
                      else None, "problems": s.problems} for k, s in enumerate(sides)],
            "environment": env, **cd, "logs_as_outputs": logs_as_outputs,
            "outputs": {"identical": count(outputs, "identical"), "differs": count(outputs, "differs"),
                        "missing": count(outputs, "missing"), "unexplained": len(bad)},
            "records": {"identical": count(records, "identical"), "differs": count(records, "differs"),
                        "missing": count(records, "missing")},
            "reasons": _merge([f["reasons"] for f in outputs]), "files": files}


# ---- in words ---------------------------------------------------------------------------------------------------

def _some(items: list[str], limit: int = SHOWN) -> str:
    return ", ".join(items[:limit]) + (f" and {len(items) - limit} more" if len(items) > limit else "")


def headline(d: dict) -> str:
    """One line: the verdict, and why when the outputs are not identical."""
    o = d["outputs"]
    if d["verdict"] == "identical":
        return f"IDENTICAL: every output the same, byte for byte ({counted(o['identical'], 'file')})"
    if d["verdict"] == "nothing":
        return "NOTHING TO COMPARE: the runs kept no outputs and no logs"
    if d["verdict"] == "same":
        return ("THE SAME apart from " + " and ".join(d["reasons"]) + f": {o['identical']} of "
                f"{o['identical'] + o['differs']} outputs byte for byte, the rest differ only in "
                + "; ".join(REASONS[r] for r in d["reasons"]))
    parts = [f"{o['differs']} {'differs' if o['differs'] == 1 else 'differ'}" if o["differs"] else "",
             f"{o['missing']} missing in some runs" if o["missing"] else ""]
    return (f"NOT REPRODUCED: of {counted(o['identical'] + o['differs'] + o['missing'], 'output')}, "
            f"{o['identical']} identical, " + ", ".join(p for p in parts if p))


def exit_code(d: dict) -> int:
    """0 the outputs are the same and every run passed; 1 otherwise (a run that failed proves nothing, however
    reproducible)."""
    return 0 if d["reproduced"] and all(r["status"] == "passed" for r in d["runs"]) else 1


def _run_line(r: dict) -> str:
    where = "" if r["where"] == "this project" else f", from {r['where']}"
    gpu = f" {r['gpu']}" if r["gpu"] else ""
    return R._line(f"run {r['run']}: `{r['run_id']}` ({r['status']}, {r['backend'] or '?'}{gpu}{where})")


def _file_line(f: dict, n: int) -> str:
    label = {"differs": "DIFFERS", "missing": "MISSING", "identical": "SAME"}[f["status"]]
    which = ""
    if f["status"] == "differs" and n > 2:
        which = f" (in run {', '.join(map(str, f['differs_in']))} of {n}; {f['versions']} versions)"
    why = "; ".join(REASONS[r] for r in f["reasons"] if r != "missing")
    tail = f" - {why}" if why else ""
    return f"- {label} `{R._line(f['path'])}`{which}: {R._line(f.get('summary') or '')}{tail}"


def _files_section(files: list[dict], n: int) -> list[str]:
    lines = [_file_line(f, n) for f in files if f["status"] != "identical"]
    same = [f"`{R._line(f['path'])}`" for f in files if f["status"] == "identical"]
    if same:
        lines.append(f"- identical in every run ({len(same)}): {_some(same)}")
    return lines or ["- none"]


def render(d: dict) -> str:
    """The comparison as markdown: the verdict, where the runs ran, their code and data, each output, testbench's own
    records, and what to do about each reason found."""
    n = len(d["runs"])
    plans = [R._line(p) for p in d["plans"]]                # a shared report's plan is anyone's text: one line
    plan = f"plan `{plans[0]}`" if len(plans) == 1 else "plans " + ", ".join(f"`{p}`" for p in plans)
    lines = [f"# Reproducibility: {plan}, {n} runs - {headline(d).split(':')[0]}", "", headline(d) + ".", "",
             *[f"- {_run_line(r)}" for r in d["runs"]], ""]
    notes = []
    if len(d["plans"]) > 1:
        notes.append("these are runs of different plans, so their steps and files may not line up")
    failed = [str(r["run"]) for r in d["runs"] if r["status"] != "passed"]
    if failed:
        notes.append(f"run {', '.join(failed)} did not pass, so it may have stopped before making every file")
    for r in d["runs"]:
        if r["problems"]:
            notes.append(f"run {r['run']}'s evidence no longer matches what it recorded ({_some(r['problems'], 3)}): "
                         "its hashes are the recorded ones, and the content of a changed file is not read")
    if d.get("logs_as_outputs"):
        notes.append("the plan keeps no files of its own (`artifacts:`), so what its steps printed (log.txt) is "
                     "compared as its output")
    if notes:
        lines += [f"Note: {x}." for x in notes] + [""]
    env = d["environment"]
    lines += ["## Where they ran", ""]
    if env["same"]:
        lines.append("- the same in every run: " + R._line(", ".join(
            f"{k} {v}" for k, v in env["first"].items() if v is not None)) + ", the same packages"
            if env["recorded"] else "- not recorded for every run (cut short, or an older testbench)")
    for row in env["differences"]:
        lines.append(f"- {row['what']}: " + R._line(" / ".join(f"{v if v is not None else '-'}" for v in row["values"]))
                     + " (run " + " / ".join(str(k + 1) for k in range(n)) + ")")
    if env["packages"]:
        moved = [R._line(f"{p['name']} {p['a'] or '-'} -> {p['b'] or '-'}") + (f" (run {p['run']})" if n > 2 else "")
                 for p in env["packages"]]
        lines.append(f"- packages ({len(moved)} differ from run 1's): {_some(moved, 12)}")
    if env["packages_unknown"]:
        lines.append(f"- the installed packages of run {', '.join(map(str, env['packages_unknown']))} differ from run "
                     "1's (their versions were not recorded)")
    lines += ["", "## Code and data", "", f"- {R._line(d['code'])}"]
    if d["data_differs_in"]:
        lines.append(f"- the data differs in run {', '.join(map(str, d['data_differs_in']))}: their numbers are about "
                     "other data (`testbench diff` names the files)")
    elif d["data_recorded"]:
        lines.append("- the same data in every run")
    files = d["files"]
    o, rec = d["outputs"], d["records"]
    outputs = [f for f in files if f["group"] == "output"]
    lines += ["", f"## Outputs: {o['identical']} identical, {o['differs']} differ, {o['missing']} missing", "",
              *_files_section(outputs, n)]
    records = [f for f in files if f["group"] == "record"]
    if records:
        lines += ["", f"## testbench's records of each step: {rec['identical']} identical, {rec['differs']} differ, "
                      f"{rec['missing']} missing", "",
                  "Logs and result.json hold times and the step's own account; they do not decide the verdict.", "",
                  *_files_section(records, n)]
    found = d["reasons"]                                # what to do about the outputs; logs hold times by nature
    if found:
        lines += ["", "## What to do", ""]
        lines += [f"- {REASONS[r]}: {FIX[r]}" for r in found if r in FIX]
        if not env["same"]:
            lines.append("- the runs ran in different environments (above): a difference may come from there; run "
                         "the repro on one machine (`testbench repro PLAN`) to tell the two apart")
    if d.get("kept"):
        lines += ["", f"Kept in {d['kept']} (repro.json, repro.md); `testbench ui` lists it."]
    return R._encodable("\n".join(lines).rstrip() + "\n")


def save(root: Path, d: dict, rid: str | None = None) -> Path:
    """repro.json and repro.md in .testbench/repro/<id>/ (rid: a folder _new_folder made, else a new one); the
    folder."""
    if rid is None:                                      # a plan's name from a shared report is anyone's text
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", d["plans"][0])[:40].strip("-") if len(d["plans"]) == 1 else ""
        folder = _new_folder(root, f"{dt.datetime.now():%Y%m%d-%H%M%S}-{name or 'compare'}")
    else:
        folder = Path(root) / ".testbench" / "repro" / rid
        folder.mkdir(parents=True, exist_ok=True)
    d["id"], d["kept"] = folder.name, f".testbench/repro/{folder.name}/"
    d["made"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    (folder / "repro.json").write_text(json.dumps(d, indent=1, default=str) + "\n", encoding="utf-8")
    (folder / "repro.md").write_text(render(d), encoding="utf-8")
    (folder / "pending").unlink(missing_ok=True)
    runs._gitignore(root)
    return folder


def all_repros(root: Path) -> list[dict]:
    """Every repro kept in the project, oldest first: its repro.json, or {id, error} when it cannot be read."""
    base = Path(root) / ".testbench" / "repro"
    out = []
    for p in sorted(base.glob("*/repro.json")) if base.is_dir() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not isinstance(d.get("verdict"), str):
                raise ValueError(p)
            out.append(d)
        except (OSError, ValueError, RecursionError):
            out.append({"id": p.parent.name, "error": "its repro.json cannot be read"})
    return out


def compare_runs(root: Path | None, refs: list[str], keep: bool = True) -> dict:
    """`testbench repro --compare`: the runs named, compared; kept in the project when there is one."""
    sides = [load(root, ref) for ref in refs]
    seen = {}
    for s, ref in zip(sides, refs):
        key = (s.report.get("run_id"), s.report.get("evidence_sha256"))
        if key in seen:
            raise ReproError(f"{ref} and {seen[key]} are the same run ({s.label}); name two different runs")
        seen[key] = ref
    d = compare(sides)
    if keep and root is not None:
        save(root, d)
    return d


# ---- a repro: the plan run N times, inside the budget -----------------------------------------------------------

def variant(cfg: dict, plan_name: str, backend: str | None) -> dict:
    """The config a repro's run launches with: the plan on `backend` (None: its own), in a fresh copy of the project."""
    from . import backends as B
    run_cfg = copy.deepcopy(cfg)
    plan = run_cfg["plans"][plan_name]
    if backend and backend != plan["backend"]:
        if backend not in B.names():
            raise ReproError(f"--on {backend}: no such backend{C.near(backend, B.names())}; use one of "
                             f"{', '.join(B.names())}")
        try:
            C.check_hardware(f"plans.{plan_name}", backend, plan.get("gpu"))
            B.get(backend).validate(run_cfg)
            B.get(backend).plan_runner()
        except (C.ConfigError, ValueError) as exc:
            raise ReproError(f"--on {backend}: plan {plan_name} cannot run there as it is: {exc}") from None
        plan["backend"] = backend
    plan["workdir"] = "copy"                               # no run sees what another left in the project folder
    return run_cfg


def _command(plan: str, times: int | None, on: list[str]) -> str:
    return " ".join(["testbench repro", plan] + ([f"--times {times}"] if times else []) + [f"--on {b}" for b in on])


def repro(root: Path, cfg: dict, plan: str, times: int | None = None, on=(), approve: bool = False,
          wait: float = 540, as_json: bool = False, say=print) -> int:
    """`testbench repro PLAN`: price every run, check their total once (a person approves it above ask_above_usd),
    launch them one after another, follow each, then compare them. The exit code is exit_code()'s, or 3 when a run is
    still going after `wait` seconds; a ReproError (2) when nothing was launched."""
    on = list(on or [])
    if plan not in cfg["plans"]:
        raise ReproError(C.no_plan(cfg, plan))
    if times is not None and times < 2:
        raise ReproError("a repro compares runs, so it takes at least two: --times 2 or more")
    n = times or max(2, len(on))
    if len(on) > n:
        raise ReproError(f"--times {n} is fewer runs than the {len(on)} backends --on names; give --times {len(on)} "
                         "or more")
    if cfg["plans"][plan].get("holdout"):
        raise ReproError(f"plan {plan} opens the sealed holdout, and a repro would open it {n} more times, each "
                         "counted against its max_opens. Reproduce a plan that tunes on the rows before the seal, or "
                         "compare runs that exist: `testbench repro --compare A B`")
    schedule = [on[k % len(on)] if on else None for k in range(n)]
    configs = {b: variant(cfg, plan, b) for b in dict.fromkeys(schedule)}
    try:
        ests = [budget.worst_case(configs[b], plan) for b in schedule]
    except C.ConfigError as exc:
        raise ReproError(str(exc)) from None
    command = _command(plan, times, on)
    ok, message, total = budget.check_together(cfg, ests, approve, f"a repro of plan {plan} ({n} runs)", command,
                                               f"testbench estimate {plan}")
    if not ok:                                           # one question for the whole repro, with its total
        from . import notify
        notify.approval_needed(root, cfg, message, total, f"{command} --approve")
        raise ReproError(message)
    tell = (lambda line: print(line, file=sys.stderr)) if as_json else say      # --json: only the JSON on stdout
    tell(f"[testbench] repro of plan {plan}: {n} runs" + (f" on {', '.join(dict.fromkeys(on))}" if on else "")
         + f", one after another, each in a fresh copy of the project. {message}")
    folder = _new_folder(root, f"{dt.datetime.now():%Y%m%d-%H%M%S}-{plan}")
    done: list[Path] = []
    for i, b in enumerate(schedule, 1):
        try:
            run_dir = runs.launch(root, configs[b], plan, approve=approve, origin={"repro": folder.name, "run": i})
        except runs.LaunchRefused as exc:                # something else took the day's room since the check
            tell(f"[testbench] run {i} of {n} was refused: {exc}")
            if len(done) < 2:
                _abandon(folder)
                raise ReproError(f"the repro stopped at run {i} of {n}: it was refused (above), and nothing more was "
                                 "launched" + (f"; run {done[0].name} finished" if done else "")) from None
            break
        tell(f"[testbench] run {i} of {n}: {run_dir.name} on {configs[b]['plans'][plan]['backend']}")
        state = runs.wait(run_dir, wait, on_line=tell)
        if state.get("status") not in runs.DONE:
            tell(f"[testbench] run {run_dir.name} is still going after {wait:.0f} s; the repro stops following it"
                 + (f", and runs {i + 1} to {n} were not started" if i < n else "") + ". When it has ended (`testbench "
                 f"wait {run_dir.name}`), compare: `testbench repro --compare "
                 f"{' '.join([p.name for p in done] + [run_dir.name])}`"
                 + (" and another run of the plan" if not done else ""))
            _abandon(folder)
            return 3
        done.append(run_dir)
    d = compare([load(root, p.name) for p in done])
    d["launched"] = {"times": n, "on": on, "approved": bool(approve), "worst_case_usd": total["worst_case_usd"]}
    save(root, d, folder.name)
    say(json.dumps(d, indent=1, default=str) if as_json else "\n" + render(d).rstrip())
    return exit_code(d)


def _new_folder(root: Path, rid: str) -> Path:
    """.testbench/repro/<rid>/, made now (rid-2, rid-3 ... when taken), marked pending until save() fills it."""
    base = Path(root) / ".testbench" / "repro"
    folder, k = base / rid, 1
    while True:
        try:
            folder.mkdir(parents=True)
            break
        except FileExistsError:
            k += 1
            folder = base / f"{rid}-{k}"
    (folder / "pending").write_text("")
    return folder


def _abandon(folder: Path) -> None:
    try:
        (folder / "pending").unlink(missing_ok=True)
        folder.rmdir()
    except OSError:
        pass


# ---- the deterministic: expectation ----------------------------------------------------------------------------

def glob_match(rel: str, pattern: str) -> bool:
    """rel (a / path) matches a glob as glob.glob(recursive=True) reads it: * and ? within a folder, ** across."""
    p = pattern.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    out, i = "", 0
    while i < len(p):
        if p.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif p.startswith("**", i):
            out, i = out + ".*", i + 2
        elif p[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif p[i] == "?":
            out, i = out + "[^/]", i + 1
        elif p[i] == "[" and "]" in p[i + 1:]:
            j = p.index("]", i + 1)
            body = p[i + 1:j]
            out, i = out + "[" + ("^" + body[1:] if body.startswith("!") else body) + "]", j + 1
        else:
            out, i = out + re.escape(p[i]), i + 1
    return re.fullmatch(out, rel) is not None


def _same_code(a: dict, b: dict) -> bool:
    return (a.get("commit") == b.get("commit") and bool(a.get("dirty")) == bool(b.get("dirty"))
            and (not a.get("dirty") or a.get("changes_sha256") == b.get("changes_sha256")))


def _fingerprint(data) -> dict:
    return {str(role): d.get("sha256") for role, d in (data or {}).items() if isinstance(d, dict)} \
        if isinstance(data, dict) else {}


def reference_run(root: Path, plan_name: str, source: dict | None, data: dict | None) -> tuple[Path | None, str]:
    """The latest passing run of the plan on the same code (commit and uncommitted changes) and data, or why none."""
    if not source:
        return None, ("the project is not in a git repository, so which earlier run ran the same code cannot be told "
                      "(commit it to compare runs)")
    want = _fingerprint(data)
    for p in reversed(runs.all_runs(root)):
        rep = R.load(p)
        if not rep or rep.get("plan") != plan_name or rep.get("status") != "passed":
            continue
        src = rep.get("source") if isinstance(rep.get("source"), dict) else {}
        if _same_code(src, source) and _fingerprint(rep.get("data")) == want and isinstance(rep.get("evidence"), list):
            return p, ""
    return None, (f"no earlier passing run of plan {plan_name} on this code (commit {str(source.get('commit'))[:12]}"
                  + (", with these uncommitted changes" if source.get("dirty") else "")
                  + (", and this data" if want else "") + ")")


def with_reference(root: Path, plan_name: str, plan: dict, source: dict | None, data: dict | None = None) -> dict:
    """The plan as it will run, each `deterministic:` step given what to compare with - the sha256 of the files it
    names in reference_run's evidence, that run's id and folder, and where it ran - or why there is none. The files
    are kept as the step's artifacts too, so this run is the next one's reference."""
    plan = copy.deepcopy(plan)
    steps = [s for s in plan["steps"] if isinstance(s.get("expect"), dict)
             and isinstance(s["expect"].get("deterministic"), list)]
    if not steps:
        return plan
    found, why = reference_run(root, plan_name, source, data)
    rep = R.load(found) if found else None
    for s in steps:
        patterns = s["expect"]["deterministic"]
        kept = list(s.get("artifacts") or [])           # kept as evidence too, unless an artifacts: glob has them
        s["artifacts"] = kept + [p for p in dict.fromkeys(patterns) if not any(glob_match(p, k) for k in kept)]
        against, none = None, why
        prev = next((x for x in (rep or {}).get("steps") or [] if isinstance(x, dict) and x.get("name") == s["name"]),
                    None)
        if rep and prev and re.fullmatch(r"steps/[\w.-]+", str(prev.get("dir"))) and ".." not in prev["dir"]:
            prefix = prev["dir"].rstrip("/") + "/artifacts/"
            files = {e["path"][len(prefix):]: {"sha256": e["sha256"], "bytes": e.get("bytes")}
                     for e in rep["evidence"] if isinstance(e, dict) and str(e.get("path", "")).startswith(prefix)
                     and any(glob_match(e["path"][len(prefix):], p) for p in patterns)}
            env = rep.get("environment") if isinstance(rep.get("environment"), dict) else {}
            against = {"run": rep.get("run_id"), "dir": str((found / prev["dir"] / "artifacts").resolve()),
                       "files": files, "backend": rep.get("backend"),
                       "environment": {k: env.get(k) for k in ("python", "platform", "machine")}}
        elif rep:
            none = f"run {rep.get('run_id')}, the latest passing run on this code, has no step {s['name']!r}"
        s["expect"]["deterministic"] = {"files": patterns, "against": against, "none": none}
    return plan


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _here() -> dict:
    return {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine()}


def check_deterministic(value, *, workdir: Path, result: dict, add) -> None:
    """The `deterministic:` check (expect.DeterministicCheck): each pattern's files against the reference run's."""
    from .expect import _stale
    spec = value if isinstance(value, dict) else {"files": value, "against": None,
                                                  "none": "this run was launched without a run to compare with"}
    against = spec.get("against") if isinstance(spec.get("against"), dict) else None
    workdir = Path(workdir)
    here = _here()
    then = (against or {}).get("environment") or {}
    same_env = not then or all(then.get(k) in (None, v) for k, v in here.items())
    moved = "" if same_env else (f"; run {against['run']} ran on Python {then.get('python')} ({then.get('platform')}), "
                                 f"this one on Python {here['python']} ({here['platform']})")
    this = Path(str(result.get("step_dir") or "")).parent.parent.name                   # the run's folder, here
    this = this if re.fullmatch(r"\d{8}-\d{6}-[\w.-]+", this) else "THIS_RUN"
    for pattern in spec.get("files") or []:
        name = f"deterministic {pattern}"
        hits = [Path(h) for h in sorted(glob.glob(str(workdir / pattern), recursive=True)) if Path(h).is_file()]
        fresh = [h for h in hits if not _stale(h, result.get("run_started"))]
        if not fresh:
            add(name, False, "missing" if not hits else f"{len(hits)} found, all last written before this run "
                                                        "started: old results, not this run's")
            continue
        rels = [Path(os.path.relpath(h, workdir)).as_posix() for h in fresh]
        if against is None:
            add(name, True, f"{counted(len(rels), 'file')}, nothing to compare with yet: "
                            f"{spec.get('none') or 'no earlier run'}. Kept as evidence, so the next run on this code "
                            "is compared with this one")
            continue
        run = against.get("run")
        ref = {rel: v for rel, v in (against.get("files") or {}).items() if glob_match(rel, pattern)}
        wrong, same, new = [], [], []
        for h, rel in zip(fresh, rels):
            if rel not in ref:
                new.append(rel)
                continue
            digest = _sha256(h)
            if digest == ref[rel].get("sha256"):
                same.append(rel)
                continue
            old, before, now = Path(str(against.get("dir") or "")) / rel, None, None
            try:
                if old.is_file() and old.stat().st_size <= READ_LIMIT and h.stat().st_size <= READ_LIMIT:
                    before, now = old.read_bytes(), h.read_bytes()
            except OSError:
                pass
            if before is None or hashlib.sha256(before).hexdigest() != ref[rel].get("sha256"):
                wrong.append(f"{rel} differs from run {run}'s (sha256 {str(ref[rel].get('sha256'))[:12]} -> "
                             f"{digest[:12]}; run {run}'s copy is not here to read: `testbench repro --compare {run} "
                             f"{this}` reads both)")
                continue
            ex = explain(before, now, rel, same_env)
            wrong.append(f"{rel} differs from run {run}'s: {ex['summary']} - {REASONS.get(ex['reason'], '')}")
        wrong += [f"{rel}: run {run} made it, this run did not" for rel in sorted(set(ref) - set(rels))]
        if wrong:
            add(name, False, "; ".join(wrong[:3]) + (f"; and {len(wrong) - 3} more" if len(wrong) > 3 else "") + moved)
        else:
            add(name, True, (f"{counted(len(same), 'file')} identical to run {run}'s" if same else
                             f"run {run} kept none of these files, so there was nothing to compare")
                + (f"; {_some(new, 3)} not in run {run}'s evidence, so not compared" if new and same else ""))


def main_compare(refs: list[str], as_json: bool = False, say=print) -> int:
    """`testbench repro --compare`: the comparison printed (or its JSON); exit_code()'s."""
    try:
        root = C.find_root()
    except C.ConfigError:
        root = None                                       # folders and bundles need no project
    d = compare_runs(root, refs)
    say(json.dumps(d, indent=1, default=str) if as_json
        else render(d).rstrip() + ("" if root is not None else "\n\n(Not kept: there is no project here.)"))
    return exit_code(d)

