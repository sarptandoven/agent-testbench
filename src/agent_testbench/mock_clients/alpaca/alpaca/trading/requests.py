"""alpaca.trading.requests of the mock alpaca package: the order and query requests of alpaca-py 0.45.0, checked
when they are made as the SDK's pydantic models check them (the same messages), and stricter about one thing: a field
the model does not have is refused instead of dropped."""
import _testbench_mock as core

from .._mock import Request, missing_names
from ..common.enums import Sort
from .enums import OrderClass, OrderSide, OrderType, PositionIntent, QueryOrderStatus, TimeInForce

__all__ = ["OrderRequest", "MarketOrderRequest", "LimitOrderRequest", "StopOrderRequest", "StopLimitOrderRequest",
           "TakeProfitRequest", "StopLossRequest", "GetOrdersRequest", "GetOrderByIdRequest", "ClosePositionRequest",
           "CancelOrderResponse"]


class TakeProfitRequest(Request):
    _fields = {"limit_price": "float"}
    _need = ("limit_price",)


class StopLossRequest(Request):
    _fields = {"stop_price": "float", "limit_price": "float"}
    _need = ("stop_price",)


_ORDER = {"symbol": "str", "qty": "float", "notional": "float", "side": OrderSide, "type": OrderType,
          "time_in_force": TimeInForce, "order_class": OrderClass, "extended_hours": "bool", "client_order_id": "str",
          "legs": None, "take_profit": TakeProfitRequest, "stop_loss": StopLossRequest,
          "position_intent": PositionIntent}


class OrderRequest(Request):
    _fields = dict(_ORDER)
    _need = ("type", "time_in_force")

    def _check(self, v):
        if v["qty"] is None and v["notional"] is None:
            raise ValueError("At least one of qty or notional must be provided")
        if v["qty"] is not None and v["notional"] is not None:
            raise ValueError("Both qty and notional can not be set.")
        if v["order_class"] != OrderClass.MLEG:
            if v["symbol"] is None:
                raise ValueError("symbol is required for all order classes other than mleg.")
            if v["side"] is None:
                raise ValueError("side is required for all order classes other than mleg.")


class MarketOrderRequest(OrderRequest):
    _set = {"type": OrderType.MARKET}
    _order_type = "market"


class LimitOrderRequest(OrderRequest):
    _fields = {**_ORDER, "limit_price": "float"}
    _set = {"type": OrderType.LIMIT}
    _prices = ("limit_price",)
    _order_type = "limit"

    def _check(self, v):
        super()._check(v)
        if v["order_class"] != OrderClass.OCO and v["limit_price"] is None:
            raise ValueError("limit_price is required")


class StopOrderRequest(OrderRequest):
    _fields = {**_ORDER, "stop_price": "float"}
    _need = ("type", "time_in_force", "stop_price")
    _set = {"type": OrderType.STOP}
    _prices = ("stop_price",)
    _order_type = "stop"


class StopLimitOrderRequest(OrderRequest):
    _fields = {**_ORDER, "stop_price": "float", "limit_price": "float"}
    _need = ("type", "time_in_force", "stop_price", "limit_price")
    _set = {"type": OrderType.STOP_LIMIT}
    _prices = ("stop_price", "limit_price")
    _order_type = "stop_limit"


class GetOrdersRequest(Request):
    _fields = {"status": QueryOrderStatus, "limit": "int", "after": "datetime", "until": "datetime",
               "direction": Sort, "nested": "bool", "side": OrderSide, "symbols": "strs"}


class GetOrderByIdRequest(Request):
    _fields = {"nested": "bool"}


class ClosePositionRequest(Request):
    _fields = {"qty": "float", "percentage": "float"}

    def _check(self, v):
        if (v["qty"] is None) == (v["percentage"] is None):
            raise ValueError("qty or percentage must be given to the ClosePositionRequest, and not both")
        if v["percentage"] is not None and not 0 < v["percentage"] <= 100:
            raise ValueError("percentage is from 0 to 100")


class CancelOrderResponse(core.Obj):
    _required = ("id", "status")
    _optional = ("body",)


__getattr__ = missing_names("alpaca.trading.requests", __all__)
