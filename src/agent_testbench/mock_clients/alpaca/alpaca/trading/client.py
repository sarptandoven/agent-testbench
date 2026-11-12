"""alpaca.trading.client of the mock alpaca package: TradingClient on a paper account kept here."""
from __future__ import annotations

from datetime import datetime, timezone

import _testbench_broker as B

from .._mock import BROKER, TRADING, account, error, new_order_id, raw, symbol_of, uid
from .enums import (AccountStatus, AssetClass, AssetExchange, OrderClass, OrderSide, OrderStatus, OrderType,
                    PositionSide, TimeInForce)
from .models import Clock, ClosePositionResponse, Order, Position, TradeAccount
from .requests import CancelOrderResponse, ClosePositionRequest, GetOrdersRequest, OrderRequest

__all__ = ["TradingClient"]
_STATUS = {"open": OrderStatus.NEW, "filled": OrderStatus.FILLED, "canceled": OrderStatus.CANCELED}
_CREATED = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)


def _order(o: dict) -> Order:
    crypto = "/" in o["symbol"]
    status = OrderStatus.EXPIRED if (o["why"] or "").startswith("expired") else _STATUS[o["status"]]
    otype = OrderType(o["type"])
    return Order(
        id=uid("order", o["n"]), client_order_id=o["client_id"] or str(uid("client-order", o["n"])),
        created_at=o["created"], updated_at=o["filled_at"] or o["canceled_at"] or o["created"],
        submitted_at=o["created"], filled_at=o["filled_at"], expired_at=None, expires_at=None,
        canceled_at=o["canceled_at"], failed_at=None, replaced_at=None, replaced_by=None, replaces=None,
        asset_id=uid("asset", o["symbol"]), symbol=o["symbol"],
        asset_class=AssetClass.CRYPTO if crypto else AssetClass.US_EQUITY,
        notional=B.fmt(o["notional"]), qty=None if o["notional"] is not None and o["status"] != "filled" else
        B.fmt(o["qty"]), filled_qty=B.fmt(o["filled"]), filled_avg_price=B.fmt(o["price"]),
        order_class=OrderClass.SIMPLE, order_type=otype, type=otype, side=OrderSide(o["side"]),
        time_in_force=TimeInForce(o["tif"]), limit_price=B.fmt(o["limit"]), stop_price=B.fmt(o["stop"]),
        status=status, extended_hours=bool(o["info"].get("extended_hours")), legs=None, trail_percent=None,
        trail_price=None, hwm=None, position_intent=o["info"].get("position_intent"), ratio_qty=None, group_id=None,
        group_role=None)


def _position(a: B.Account, asset: str) -> Position:
    symbol = symbol_of(asset)
    qty, cost = a.held(asset), a.cost.get(asset, 0.0)
    price = BROKER.book.price(symbol) or 0.0
    last = BROKER.book.previous(symbol) or price
    value = qty * price
    crypto = "/" in symbol
    return Position(
        asset_id=uid("asset", symbol), symbol=asset, exchange=AssetExchange.CRYPTO if crypto else AssetExchange.EMPTY,
        asset_class=AssetClass.CRYPTO if crypto else AssetClass.US_EQUITY, asset_marginable=False,
        avg_entry_price=B.fmt(cost / qty if qty else 0), qty=B.fmt(qty), side=PositionSide.LONG,
        market_value=B.fmt(value), cost_basis=B.fmt(cost), unrealized_pl=B.fmt(value - cost),
        unrealized_plpc=B.fmt((value - cost) / cost if cost else 0), unrealized_intraday_pl=B.fmt((price - last) * qty),
        unrealized_intraday_plpc=B.fmt((price - last) / last if last else 0), current_price=B.fmt(price),
        lastday_price=B.fmt(last), change_today=B.fmt((price - last) / last if last else 0), swap_rate=None,
        avg_entry_swap_rate=None, prev_swap_rate=None, usd=None, qty_available=B.fmt(a.free(asset)))


def _asset(a: B.Account, symbol_or_asset_id) -> str:
    """The position key of what get_open_position / close_position name: AAPL, BTC/USD, BTCUSD or an asset id."""
    key = str(symbol_or_asset_id)
    for asset in a.holdings():
        if key in (asset, symbol_of(asset)) or key == str(uid("asset", symbol_of(asset))):
            return asset
    raise B.Refused("not_found", "position does not exist")


class TradingClient:
    """alpaca.trading.client.TradingClient against the mock's paper account. paper=False is a live account: refused
    unless the step's mocks: options say allow_live: true."""

    def __init__(self, api_key=None, secret_key=None, oauth_token=None, paper=True, raw_data=False,
                 url_override=None):
        args = {"api_key": "<given>" if api_key else None, "secret_key": "<given>" if secret_key else None,
                "oauth_token": "<given>" if oauth_token else None, "paper": paper, "raw_data": raw_data,
                "url_override": url_override}
        why = ("You must supply a method of authentication" if not oauth_token and not api_key else
               "Either an oauth_token or an api_key may be supplied, but not both" if oauth_token and (
                   api_key or secret_key) else
               "A corresponding secret_key must be supplied with the api_key" if not oauth_token and not (
                   api_key and secret_key) else None)
        if why:
            raise BROKER.log_refusal("TradingClient", args, ValueError(why))
        self._live, self._raw = not paper, raw_data
        BROKER.call("TradingClient", args, lambda: BROKER.guard_live(self._live, "TradingClient(paper=False)"),
                    error=error, live=self._live, request=False)

    def _call(self, endpoint, args, answer):
        result = BROKER.call(endpoint, args, answer, error=error, live=self._live)
        return raw(result) if self._raw else result

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise BROKER.not_modelled("TradingClient.", name, TRADING)

    # the account
    def get_account(self):
        def answer():
            a = account()
            cur = a.currency
            cash, equity, free = a.held(cur), a.equity(), a.free(cur)
            return TradeAccount(
                id=uid("account", "paper" if not self._live else "live"),
                account_number=("PA" if not self._live else "") + "3TESTBENCH", status=AccountStatus.ACTIVE,
                crypto_status=AccountStatus.ACTIVE, crypto_tier=None, currency=cur, buying_power=B.fmt(free),
                effective_buying_power=B.fmt(free), regt_buying_power=B.fmt(free), daytrading_buying_power="0",
                non_marginable_buying_power=B.fmt(free), cash=B.fmt(cash), accrued_fees="0",
                pending_transfer_out=None, pending_transfer_in=None, portfolio_value=B.fmt(equity),
                pattern_day_trader=False, trading_blocked=False, transfers_blocked=False, account_blocked=False,
                created_at=_CREATED, trade_suspended_by_user=False, multiplier="1", shorting_enabled=False,
                equity=B.fmt(equity), last_equity=B.fmt(a.start_equity), long_market_value=B.fmt(equity - cash),
                short_market_value="0", position_market_value=B.fmt(equity - cash), initial_margin="0",
                maintenance_margin="0", last_maintenance_margin="0", sma="0", daytrade_count=0,
                options_buying_power=B.fmt(free), options_approved_level=0, options_trading_level=0,
                balance_asof=BROKER.book.now().date().isoformat(), intraday_adjustments="0",
                pending_reg_taf_fees="0")
        return self._call("get_account", {}, answer)

    def get_all_positions(self):
        def answer():
            a = account()
            return [_position(a, asset) for asset in a.holdings()]
        return self._call("get_all_positions", {}, answer)

    def get_open_position(self, symbol_or_asset_id):
        def answer():
            a = account()
            return _position(a, _asset(a, symbol_or_asset_id))
        return self._call("get_open_position", {"symbol_or_asset_id": str(symbol_or_asset_id)}, answer)

    def get_clock(self):
        def answer():
            now = BROKER.book.now()
            opening, close = B.market_hours(now)
            return Clock(timestamp=now, is_open=True, next_open=opening, next_close=close)
        return self._call("get_clock", {}, answer)

    # orders
    def submit_order(self, order_data):
        if not isinstance(order_data, OrderRequest):
            raise BROKER.log_refusal("submit_order", {"order_data": repr(order_data)[:200]}, TypeError(
                "submit_order() takes an order request (MarketOrderRequest, LimitOrderRequest, StopOrderRequest or "
                f"StopLimitOrderRequest from alpaca.trading.requests), got {type(order_data).__name__}"), self._live)
        r = order_data

        def answer():
            if r.order_class not in (None, OrderClass.SIMPLE) or r.take_profit or r.stop_loss or r.legs:
                raise B.Refused("unsupported", f"order_class {getattr(r.order_class, 'value', 'simple')} (and take_"
                                               "profit, stop_loss, legs) is not modelled: the mock fills simple orders")
            if r.side not in (OrderSide.BUY, OrderSide.SELL):
                raise B.Refused("unsupported", f"side {r.side.value} is not modelled: the mock takes buy and sell")
            a = account()
            crypto, tif = "/" in r.symbol, r.time_in_force.value
            if crypto and tif not in ("gtc", "ioc"):
                raise B.Refused("invalid", f"invalid crypto time_in_force: crypto orders take gtc or ioc, not {tif}")
            if r.notional is not None and r.type != OrderType.MARKET:
                raise B.Refused("invalid", "notional orders must be market orders")
            fractional = r.notional is not None or (r.qty is not None and r.qty != int(r.qty))
            if fractional and not crypto and tif != "day":
                raise B.Refused("invalid", "fractional orders must be DAY orders")
            if r.client_order_id and any(o["client_id"] == r.client_order_id for o in a.orders):
                raise B.Refused("invalid", "client_order_id must be unique")
            o = a.place(symbol=r.symbol, side=r.side.value, qty=r.qty, notional=r.notional, type=r.type.value,
                        limit=getattr(r, "limit_price", None), stop=getattr(r, "stop_price", None), tif=tif,
                        client_id=r.client_order_id, make_id=new_order_id,
                        info={"extended_hours": r.extended_hours, "position_intent": r.position_intent})
            return _order(o)
        return self._call("submit_order", {"order_data": {"class": type(r).__name__, **raw(r.to_request_fields())}},
                          answer)

    def get_orders(self, filter=None):
        if filter is not None and not isinstance(filter, GetOrdersRequest):
            raise BROKER.log_refusal("get_orders", {"filter": repr(filter)[:200]}, TypeError(
                f"get_orders(filter=) takes a GetOrdersRequest, got {type(filter).__name__}"), self._live)
        f = filter

        def answer():
            a = account()
            status = f.status.value if f and f.status else "open"
            out = [o for o in a.orders if status == "all" or (o["status"] == "open") == (status == "open")]
            if f and f.side:
                out = [o for o in out if o["side"] == f.side.value]
            if f and f.symbols:
                out = [o for o in out if o["symbol"] in f.symbols]
            if f and f.after:
                out = [o for o in out if o["created"] > f.after]
            if f and f.until:
                out = [o for o in out if o["created"] < f.until]
            if not (f and f.direction and f.direction.value == "asc"):
                out = out[::-1]
            limit = f.limit if f and f.limit else 50
            if not 1 <= limit <= 500:
                raise B.Refused("invalid", f"limit is from 1 to 500, got {limit}")
            return [_order(o) for o in out[:limit]]
        return self._call("get_orders", {"filter": raw(f.to_request_fields()) if f else None}, answer)

    def get_order_by_id(self, order_id, filter=None):
        def answer():
            try:
                return _order(account().find(order_id))
            except B.Refused:
                raise B.Refused("not_found", f"order not found for {order_id}") from None
        return self._call("get_order_by_id", {"order_id": str(order_id)}, answer)

    def get_order_by_client_id(self, client_id):
        def answer():
            o = next((o for o in account().orders if o["client_id"] == client_id), None)
            if o is None:
                raise B.Refused("not_found", f"order not found for client_order_id {client_id}")
            return _order(o)
        return self._call("get_order_by_client_id", {"client_id": client_id}, answer)

    def cancel_orders(self):
        def answer():
            a = account()
            out = []
            for o in a.open_orders():
                a.cancel(o)
                out.append(CancelOrderResponse(id=uid("order", o["n"]), status=200, body=None))
            return out
        return self._call("cancel_orders", {}, answer)

    def cancel_order_by_id(self, order_id):
        def answer():
            a = account()
            try:
                o = a.find(order_id)
            except B.Refused:
                raise B.Refused("not_found", f"order not found for {order_id}") from None
            if o["status"] != "open":
                raise B.Refused("state", f'order is already in "{_STATUS[o["status"]].value}" state')
            a.cancel(o)
        return self._call("cancel_order_by_id", {"order_id": str(order_id)}, answer)

    # closing positions
    def _close(self, a, asset, qty=None):
        symbol = symbol_of(asset)
        crypto = "/" in symbol
        qty = a.free(asset) if qty is None else qty
        o = a.place(symbol=symbol, side="sell", qty=qty, type="market", tif="gtc" if crypto else "day",
                    make_id=new_order_id)
        return _order(o)

    def close_all_positions(self, cancel_orders=None):
        def answer():
            a = account()
            if cancel_orders:
                for o in a.open_orders():
                    a.cancel(o)
            out = []
            for asset in a.holdings():
                try:
                    o = self._close(a, asset)
                    out.append(ClosePositionResponse(order_id=o.id, status=200, symbol=asset, body=o))
                except B.Refused as exc:
                    body = {"code": 40310000, "message": f"insufficient qty available for order ({exc})",
                            "symbol": asset}
                    out.append(ClosePositionResponse(order_id=None, status=403, symbol=asset, body=body))
            return out
        return self._call("close_all_positions", {"cancel_orders": cancel_orders}, answer)

    def close_position(self, symbol_or_asset_id, close_options=None):
        if close_options is not None and not isinstance(close_options, ClosePositionRequest):
            raise BROKER.log_refusal("close_position", {"close_options": repr(close_options)[:200]}, TypeError(
                f"close_position(close_options=) takes a ClosePositionRequest, got {type(close_options).__name__}"),
                self._live)

        def answer():
            a = account()
            asset = _asset(a, symbol_or_asset_id)
            qty = None
            if close_options is not None:
                qty = close_options.qty if close_options.qty is not None else \
                    B.num(a.held(asset) * close_options.percentage / 100, 9)
            return self._close(a, asset, qty)
        return self._call("close_position", {"symbol_or_asset_id": str(symbol_or_asset_id),
                                             "close_options": raw(close_options.to_request_fields()) if close_options
                                             else None}, answer)

