"""Prices for worst-case estimates: the table built into this version, raised - never lowered - by a fetched catalog.

    testbench prices                 # every price in use, and where it comes from
    testbench prices --update        # fetch pricing/catalog.json from the repository into ~/.testbench/prices.json

Providers change prices between releases. A price that went up makes every estimate too low, which is the
dangerous direction, so a fetched catalog may raise a price above the built-in one or add a price for hardware
this version does not know - at no less than the dearest hardware it does know, so a catalog cannot make
costly hardware cheap under a new name; it cannot lower a price (that comes with a release). A tampered or stale
catalog can therefore only make estimates more cautious. A project's `pricing:` overrides both, and the guard hook asks the
person before an agent changes it. Nothing is fetched unless asked.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

URL = "https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/pricing/catalog.json"


SECTIONS = ("modal", "colab", "runpod")              # the catalog's sections of prices, one per backend


class PriceError(RuntimeError):
    pass


def cache_path() -> Path:
    from .budget import user_dir
    return user_dir() / "prices.json"


def cached() -> dict:
    """The fetched catalog, {} if there is none (or it cannot be read: then the built-in prices apply)."""
    try:
        data = json.loads(cache_path().read_text())
    except (OSError, ValueError, RecursionError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if k not in SECTIONS or isinstance(v, dict)}


def _price(v) -> bool:
    try:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    except OverflowError:                                # an integer too big for a float
        return False


def raised(built_in: dict, fetched) -> dict:
    """built_in with each fetched price that is higher, and fetched prices for names it does not have - above 0, and
    never below the dearest built-in price, since nothing says what the new name really is (hardware a catalog adds
    cheaply would make every estimate for it too low). Names in capitals, as a plan's `gpu:` is looked up."""
    out = dict(built_in)
    floor = max(built_in.values(), default=0.0)
    for name, value in (fetched or {}).items() if isinstance(fetched, dict) else ():
        name = str(name).upper()
        if not _price(value):
            continue
        if name not in built_in:
            if value > 0:
                out[name] = max(float(value), floor)
        elif value > out[name]:
            out[name] = float(value)
    return out


def upper(table) -> dict:
    """A project's own prices by name in capitals (its `pricing:` may say t4 for T4)."""
    return {str(k).upper(): v for k, v in table.items()} if isinstance(table, dict) else {}


def own_table(pricing: dict, key: str) -> dict:
    """The project's `pricing.KEY` table ({T4: 0.0002}), by name in capitals."""
    table = pricing.get(key) or {}
    if not isinstance(table, dict) or not all(_price(v) for v in table.values()):
        from .config import ConfigError
        raise ConfigError(f"`pricing.{key}` is {table!r}; write names and prices of 0 or more, e.g. {{T4: 0.000164}}")
    return upper(table)


def own_price(pricing: dict, key: str, default: float) -> float:
    """The project's `pricing.KEY`, one price, or default."""
    if key not in pricing:
        return default
    if not _price(pricing[key]):
        from .config import ConfigError
        raise ConfigError(f"`pricing.{key}` is {pricing[key]!r}; write one price of 0 or more, e.g. 0.0000131")
    return float(pricing[key])


def raised_value(built_in: float, fetched) -> float:
    return max(built_in, float(fetched)) if _price(fetched) else built_in


def source(name: str, built_in: dict, fetched, overrides: dict | None) -> str:
    if name in upper(overrides):
        return "testbench.yaml pricing:"
    got = raised({}, fetched)
    if name not in built_in and name in got:
        return f"catalog {cached().get('version', '?')}, not below the dearest built-in price"
    if got.get(name, 0) > built_in.get(name, 0):
        return f"catalog {cached().get('version', '?')}"
    return "built in"


def update(url: str = URL) -> list[str]:
    """Fetch the catalog and keep it; returns what it raises or adds compared with the built-in tables."""
    import urllib.request                         # here: every estimate reads prices, only this fetches them
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        raise PriceError(f"could not fetch the price catalog from {url}: {exc}; the prices in use are unchanged") from None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise PriceError(f"{url} is not a price catalog (no version); the prices in use are unchanged")
    for section in SECTIONS:
        if section in data and not isinstance(data[section], dict):
            raise PriceError(f"{url}: `{section}` is not a mapping; the prices in use are unchanged")
    data.update(fetched=dt.datetime.now().isoformat(timespec="seconds"), url=url)
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    from .backends.colab_backend import UNITS_PER_HOUR
    from .backends.modal_backend import GPU_PER_S
    from .backends.runpod_backend import USD_PER_HOUR
    changes = []
    for label, built, fetched in (("Modal GPU $/s", GPU_PER_S, (data.get("modal") or {}).get("gpu_per_s")),
                                  ("Colab units/h", UNITS_PER_HOUR, (data.get("colab") or {}).get("units_per_hour")),
                                  ("RunPod GPU $/h", USD_PER_HOUR, (data.get("runpod") or {}).get("usd_per_hour"))):
        for name, value in raised(built, fetched).items():
            if name not in built:
                changes.append(f"{label} {name}: {value:g} (new)")
            elif value != built[name]:
                changes.append(f"{label} {name}: {built[name]:g} -> {value:g}")
    return changes
