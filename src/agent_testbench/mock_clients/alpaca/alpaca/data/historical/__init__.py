"""alpaca.data.historical of the mock alpaca package."""
from ..._mock import missing_names
from .crypto import CryptoHistoricalDataClient
from .stock import StockHistoricalDataClient

__all__ = ["CryptoHistoricalDataClient", "StockHistoricalDataClient"]


__getattr__ = missing_names("alpaca.data.historical", __all__)
