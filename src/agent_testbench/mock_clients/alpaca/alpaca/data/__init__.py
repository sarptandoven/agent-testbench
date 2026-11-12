"""alpaca.data of the mock alpaca package."""
from .._mock import missing_names
from .enums import Adjustment, CryptoFeed, DataFeed
from .historical import CryptoHistoricalDataClient, StockHistoricalDataClient
from .models import Bar, BarSet
from .requests import CryptoBarsRequest, StockBarsRequest
from .timeframe import TimeFrame, TimeFrameUnit

__all__ = ["Adjustment", "CryptoFeed", "DataFeed", "CryptoHistoricalDataClient", "StockHistoricalDataClient", "Bar",
           "BarSet", "CryptoBarsRequest", "StockBarsRequest", "TimeFrame", "TimeFrameUnit"]


__getattr__ = missing_names("alpaca.data", __all__)
