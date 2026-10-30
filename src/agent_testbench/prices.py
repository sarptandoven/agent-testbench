"""Prices for worst-case estimates: the table built into this version, raised - never lowered - by a fetched catalog.

    testbench prices                 # every price in use, and where it comes from
    testbench prices --update        # fetch pricing/catalog.json from the repository into ~/.testbench/prices.json

Providers change prices between releases. A price that went up makes every estimate too low, which is the
dangerous direction, so a fetched catalog may raise a price above the built-in one or add a price for hardware
this version does not know; it cannot lower one (that comes with a release). A tampered or stale catalog can
therefore only make estimates more cautious. A project's `pricing:` overrides both, and the guard hook asks the
person before an agent changes it. Nothing is fetched unless asked.
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/sarptandoven/agent-testbench/main/pricing/catalog.json"


class PriceError(RuntimeError):
    pass


def cache_path() -> Path:
    from .budget import user_dir
    return user_dir() / "prices.json"


def cached() -> dict:
    """The fetched catalog, {} if there is none (or it cannot be read: then the built-in prices apply)."""
    try:
        data = json.loads(cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _price(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def raised(built_in: dict, fetched) -> dict:
    """built_in with each fetched price that is higher, and fetched prices for names it does not have."""
    out = dict(built_in)
    for name, value in (fetched or {}).items() if isinstance(fetched, dict) else ():
        if _price(value) and value > out.get(name, -1):
            out[name] = float(value)
    return out


def raised_value(built_in: float, fetched) -> float:
    return max(built_in, float(fetched)) if _price(fetched) else built_in


def source(name: str, built_in: dict, fetched, overrides: dict | None) -> str:
    if name in (overrides or {}):
        return "testbench.yaml pricing:"
    if isinstance(fetched, dict) and _price(fetched.get(name)) and fetched[name] > built_in.get(name, -1):
        return f"catalog {cached().get('version', '?')}"
    return "built in"


def update(url: str = URL) -> list[str]:
    """Fetch the catalog and keep it; returns what it raises or adds compared with the built-in tables."""
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:
        raise PriceError(f"could not fetch the price catalog from {url}: {exc}; the prices in use are unchanged") from None
    if not isinstance(data, dict) or not isinstance(data.get("version"), str):
        raise PriceError(f"{url} is not a price catalog (no version); the prices in use are unchanged")
    for section in ("modal", "colab"):
        if section in data and not isinstance(data[section], dict):
            raise PriceError(f"{url}: `{section}` is not a mapping; the prices in use are unchanged")
    data.update(fetched=dt.datetime.now().isoformat(timespec="seconds"), url=url)
    p = cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=1))
    from .backends.colab_backend import UNITS_PER_HOUR
    from .backends.modal_backend import GPU_PER_S
    changes = []
    for label, built, fetched in (("Modal GPU $/s", GPU_PER_S, (data.get("modal") or {}).get("gpu_per_s")),
                                  ("Colab units/h", UNITS_PER_HOUR, (data.get("colab") or {}).get("units_per_hour"))):
        for name, value in raised(built, fetched).items():
            if name not in built:
                changes.append(f"{label} {name}: {value:g} (new)")
            elif value != built[name]:
                changes.append(f"{label} {name}: {built[name]:g} -> {value:g}")
    return changes
