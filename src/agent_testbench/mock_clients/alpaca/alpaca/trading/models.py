"""alpaca.trading.models of the mock alpaca package: the replies, with the fields of alpaca-py 0.45.0's models
(numbers as strings, as the API sends them)."""
import _testbench_mock as core

__all__ = ["TradeAccount", "Position", "Order", "Clock", "ClosePositionResponse"]


class TradeAccount(core.Obj):
    _required = ("id", "account_number", "status")
    _optional = ("crypto_status", "crypto_tier", "currency", "buying_power", "effective_buying_power",
                 "regt_buying_power", "daytrading_buying_power", "non_marginable_buying_power", "cash", "accrued_fees",
                 "pending_transfer_out", "pending_transfer_in", "portfolio_value", "pattern_day_trader",
                 "trading_blocked", "transfers_blocked", "account_blocked", "created_at", "trade_suspended_by_user",
                 "multiplier", "shorting_enabled", "equity", "last_equity", "long_market_value", "short_market_value",
                 "position_market_value", "initial_margin", "maintenance_margin", "last_maintenance_margin", "sma",
                 "daytrade_count", "options_buying_power", "options_approved_level", "options_trading_level",
                 "balance_asof", "intraday_adjustments", "pending_reg_taf_fees")


class Position(core.Obj):
    _required = ("asset_id", "symbol", "exchange", "asset_class", "avg_entry_price", "qty", "side", "cost_basis")
    _optional = ("asset_marginable", "market_value", "unrealized_pl", "unrealized_plpc", "unrealized_intraday_pl",
                 "unrealized_intraday_plpc", "current_price", "lastday_price", "change_today", "swap_rate",
                 "avg_entry_swap_rate", "prev_swap_rate", "usd", "qty_available")


class Order(core.Obj):
    _required = ("id", "client_order_id", "created_at", "updated_at", "submitted_at", "order_class", "time_in_force",
                 "status", "extended_hours")
    _optional = ("filled_at", "expired_at", "expires_at", "canceled_at", "failed_at", "replaced_at", "replaced_by",
                 "replaces", "asset_id", "symbol", "asset_class", "notional", "qty", "filled_qty", "filled_avg_price",
                 "order_type", "type", "side", "limit_price", "stop_price", "legs", "trail_percent", "trail_price",
                 "hwm", "position_intent", "ratio_qty", "group_id", "group_role")


class Clock(core.Obj):
    _required = ("timestamp", "is_open", "next_open", "next_close")


class ClosePositionResponse(core.Obj):
    _required = ("body",)
    _optional = ("order_id", "status", "symbol")
