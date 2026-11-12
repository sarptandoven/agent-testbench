"""alpaca.trading.enums of the mock alpaca package: the values of alpaca-py 0.45.0."""
from enum import Enum

from .._mock import missing_names

__all__ = ["OrderSide", "TimeInForce", "OrderType", "OrderStatus", "OrderClass", "QueryOrderStatus", "AssetClass",
           "PositionSide", "AssetExchange", "AccountStatus", "PositionIntent"]


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"
    BUY_MINUS = "buy_minus"
    SELL_PLUS = "sell_plus"
    SELL_SHORT = "sell_short"
    SELL_SHORT_EXEMPT = "sell_short_exempt"
    UNDISCLOSED = "undisclosed"
    CROSS = "cross"
    CROSS_SHORT = "cross_short"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    OPG = "opg"
    CLS = "cls"
    IOC = "ioc"
    FOK = "fok"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TRAILING_STOP = "trailing_stop"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    DONE_FOR_DAY = "done_for_day"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REPLACED = "replaced"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    PENDING_REVIEW = "pending_review"
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    ACCEPTED_FOR_BIDDING = "accepted_for_bidding"
    STOPPED = "stopped"
    REJECTED = "rejected"
    SUSPENDED = "suspended"
    CALCULATED = "calculated"
    HELD = "held"


class OrderClass(str, Enum):
    SIMPLE = "simple"
    MLEG = "mleg"
    BRACKET = "bracket"
    OCO = "oco"
    OTO = "oto"
    EMPTY = ""


class QueryOrderStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    ALL = "all"


class AssetClass(str, Enum):
    US_EQUITY = "us_equity"
    US_OPTION = "us_option"
    CRYPTO = "crypto"
    CRYPTO_PERP = "crypto_perp"
    TREASURY = "treasury"
    CORPORATE = "corporate"
    GLOBAL_EQUITY = "global_equity"
    US_INDEX = "us_index"
    US_EQUITY_CHAIN = "us_equity_chain"
    IPO = "ipo"


class PositionSide(str, Enum):
    SHORT = "short"
    LONG = "long"


class AssetExchange(str, Enum):
    AMEX = "AMEX"
    ARCA = "ARCA"
    ASCX = "ASCX"
    BATS = "BATS"
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"
    NYSEARCA = "NYSEARCA"
    TXSE = "TXSE"
    FTXU = "FTXU"
    CBSE = "CBSE"
    GNSS = "GNSS"
    ERSX = "ERSX"
    OTC = "OTC"
    CRYPTO = "CRYPTO"
    EMPTY = ""


class AccountStatus(str, Enum):
    ACCOUNT_CLOSED = "ACCOUNT_CLOSED"
    ACCOUNT_CLOSED_PENDING = "ACCOUNT_CLOSED_PENDING"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED"
    ACTION_REQUIRED = "ACTION_REQUIRED"
    ACTIVE = "ACTIVE"
    AML_REVIEW = "AML_REVIEW"
    APPROVAL_PENDING = "APPROVAL_PENDING"
    APPROVED = "APPROVED"
    DISABLED = "DISABLED"
    DISABLE_PENDING = "DISABLE_PENDING"
    EDITED = "EDITED"
    INACTIVE = "INACTIVE"
    KYC_SUBMITTED = "KYC_SUBMITTED"
    LIMITED = "LIMITED"
    ONBOARDING = "ONBOARDING"
    PAPER_ONLY = "PAPER_ONLY"
    REAPPROVAL_PENDING = "REAPPROVAL_PENDING"
    REJECTED = "REJECTED"
    RESUBMITTED = "RESUBMITTED"
    SIGNED_UP = "SIGNED_UP"
    SUBMISSION_FAILED = "SUBMISSION_FAILED"
    SUBMITTED = "SUBMITTED"


class PositionIntent(str, Enum):
    BUY_TO_OPEN = "buy_to_open"
    BUY_TO_CLOSE = "buy_to_close"
    SELL_TO_OPEN = "sell_to_open"
    SELL_TO_CLOSE = "sell_to_close"


__getattr__ = missing_names("alpaca.trading.enums", __all__)
