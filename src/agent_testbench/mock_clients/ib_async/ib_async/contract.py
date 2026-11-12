"""ib_async.contract of the mock ib_async package: Contract, Stock, Forex and Crypto as in ib_async 2.1.0."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, NamedTuple

__all__ = ["Contract", "Stock", "Forex", "Crypto", "TagValue"]


class TagValue(NamedTuple):
    tag: str
    value: str


@dataclass(repr=False, eq=True)
class Contract:
    secType: str = ""
    conId: int = 0
    symbol: str = ""
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = ""
    exchange: str = ""
    primaryExchange: str = ""
    currency: str = ""
    localSymbol: str = ""
    tradingClass: str = ""
    includeExpired: bool = False
    secIdType: str = ""
    secId: str = ""
    description: str = ""
    issuerId: str = ""
    comboLegsDescrip: str = ""
    comboLegs: list = field(default_factory=list)
    deltaNeutralContract: Any = None

    def __hash__(self):
        return hash((self.secType, self.conId, self.symbol, self.currency, self.localSymbol))

    def __repr__(self):
        shown = {f.name: getattr(self, f.name) for f in fields(self)
                 if f.name != "secType" and getattr(self, f.name) not in ("", 0, 0.0, False, None, [])}
        name = type(self).__name__
        if name == "Contract":
            shown = {"secType": self.secType, **shown}
        return f"{name}({', '.join(f'{k}={v!r}' for k, v in shown.items())})"


class Stock(Contract):
    def __init__(self, symbol: str = "", exchange: str = "", currency: str = "", **kwargs):
        Contract.__init__(self, secType="STK", symbol=symbol, exchange=exchange, currency=currency, **kwargs)


class Forex(Contract):
    def __init__(self, pair: str = "", exchange: str = "IDEALPRO", symbol: str = "", currency: str = "", **kwargs):
        if pair:
            assert len(pair) == 6
            symbol = symbol or pair[:3]
            currency = currency or pair[3:]
        Contract.__init__(self, secType="CASH", symbol=symbol, exchange=exchange, currency=currency, **kwargs)

    def pair(self) -> str:
        return self.symbol + self.currency


class Crypto(Contract):
    def __init__(self, symbol: str = "", exchange: str = "", currency: str = "", **kwargs):
        Contract.__init__(self, secType="CRYPTO", symbol=symbol, exchange=exchange, currency=currency, **kwargs)
