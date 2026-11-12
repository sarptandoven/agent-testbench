"""alpaca.trading of the mock alpaca package."""
from .._mock import missing_names
from .client import TradingClient
from .enums import (AccountStatus, AssetClass, AssetExchange, OrderClass, OrderSide, OrderStatus, OrderType,
                    PositionIntent, PositionSide, QueryOrderStatus, TimeInForce)
from .models import Clock, ClosePositionResponse, Order, Position, TradeAccount
from .requests import (CancelOrderResponse, ClosePositionRequest, GetOrderByIdRequest, GetOrdersRequest,
                       LimitOrderRequest, MarketOrderRequest, OrderRequest, StopLimitOrderRequest, StopLossRequest,
                       StopOrderRequest, TakeProfitRequest)

__all__ = ["TradingClient", "AccountStatus", "AssetClass", "AssetExchange", "OrderClass", "OrderSide", "OrderStatus",
           "OrderType", "PositionIntent", "PositionSide", "QueryOrderStatus", "TimeInForce", "Clock",
           "ClosePositionResponse", "Order", "Position", "TradeAccount", "CancelOrderResponse", "ClosePositionRequest",
           "GetOrderByIdRequest", "GetOrdersRequest", "LimitOrderRequest", "MarketOrderRequest", "OrderRequest",
           "StopLimitOrderRequest", "StopLossRequest", "StopOrderRequest", "TakeProfitRequest"]


__getattr__ = missing_names("alpaca.trading", __all__)
