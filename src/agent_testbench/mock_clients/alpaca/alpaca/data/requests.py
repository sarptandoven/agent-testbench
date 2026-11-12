"""alpaca.data.requests of the mock alpaca package: the bars requests of alpaca-py 0.45.0."""
from .._mock import Request, missing_names
from ..common.enums import Sort
from .enums import Adjustment, DataFeed
from .timeframe import TimeFrame

__all__ = ["StockBarsRequest", "CryptoBarsRequest"]

_BARS = {"symbol_or_symbols": "symbols", "start": "datetime", "end": "datetime", "limit": "int", "currency": "str",
         "sort": Sort, "timeframe": TimeFrame}


class StockBarsRequest(Request):
    _fields = {**_BARS, "adjustment": Adjustment, "feed": DataFeed, "asof": "str"}
    _need = ("symbol_or_symbols", "timeframe")


class CryptoBarsRequest(Request):
    _fields = dict(_BARS)
    _need = ("symbol_or_symbols", "timeframe")


__getattr__ = missing_names("alpaca.data.requests", __all__)
