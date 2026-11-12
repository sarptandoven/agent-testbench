"""alpaca.data.enums of the mock alpaca package."""
from enum import Enum

__all__ = ["DataFeed", "Adjustment", "CryptoFeed"]


class DataFeed(str, Enum):
    IEX = "iex"
    SIP = "sip"
    DELAYED_SIP = "delayed_sip"
    OTC = "otc"
    BOATS = "boats"
    OVERNIGHT = "overnight"


class Adjustment(str, Enum):
    RAW = "raw"
    SPLIT = "split"
    DIVIDEND = "dividend"
    ALL = "all"


class CryptoFeed(str, Enum):
    US = "us"
