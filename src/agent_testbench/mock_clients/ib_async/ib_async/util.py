"""ib_async.util of the mock ib_async package: df, startLoop, logToConsole, isNan, sleep (which does not wait)."""
from __future__ import annotations

import logging
import sys
from dataclasses import fields, is_dataclass

__all__ = ["df", "startLoop", "patchAsyncio", "logToConsole", "isNan", "sleep", "UNSET_DOUBLE", "UNSET_INTEGER"]
UNSET_DOUBLE = sys.float_info.max
UNSET_INTEGER = 2 ** 31 - 1


def df(objs, labels=None):
    """A pandas DataFrame of a list of ib_async objects (bars, fills, positions, ...), as util.df makes it."""
    try:
        import pandas as pd
    except ImportError:
        raise ImportError("util.df needs pandas, which is not installed where the step runs (pip install pandas)") \
            from None
    if not objs:
        return None
    objs = list(objs)
    obj = objs[0]
    if is_dataclass(obj):
        frame = pd.DataFrame.from_records(tuple(getattr(o, f.name) for f in fields(o)) for o in objs)
        frame.columns = [f.name for f in fields(obj)]
    else:
        frame = pd.DataFrame.from_records(objs)
        if isinstance(obj, tuple) and getattr(obj, "_fields", None):
            frame.columns = obj._fields
    if labels:
        frame = frame.drop([c for c in frame if c not in labels], axis=1)
    return frame


def isNan(x: float) -> bool:
    return x != x


def startLoop():
    """Nothing to do: the mock answers at once, with no event loop."""


def patchAsyncio():
    """Nothing to do: the mock answers at once, with no event loop."""


def logToConsole(level=logging.INFO):
    logging.getLogger("ib_async").setLevel(level)


def sleep(secs: float = 0.02) -> bool:
    """Does not wait: the mock's clock moves with requests (advance_every), not with time."""
    return True
