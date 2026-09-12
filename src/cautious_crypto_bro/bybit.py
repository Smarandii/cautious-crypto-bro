from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN

from pybit.unified_trading import HTTP

from .domain import EntryType, Side, TradingIntent


class TradeExecutionError(RuntimeError):
    pass


class BybitDemoExecutor:
    def __init__(self, *, api_key: str, api_secret: str, notional_usdt: float) -> None:
        self._session = HTTP(testnet=False, demo=True, api_key=api_key, api_secret=api_secret)
        self._notional_usdt = Decimal(str(notional_usdt))

    async def execute(self, intent: TradingIntent) -> str:
        return await asyncio.to_thread(self._execute_sync, intent)

    def _execute_sync(self, intent: TradingIntent) -> str:
        market_price = self._last_price(intent.symbol)
        sizing_price = Decimal(str(intent.entry.price)) if intent.entry.type is EntryType.LIMIT else market_price
        qty = self._quantity_for_notional(intent.symbol, sizing_price)

        params: dict[str, object] = {
            "category": "linear",
            "symbol": intent.symbol,
            "side": "Buy" if intent.side is Side.LONG else "Sell",
            "orderType": "Market" if intent.entry.type is EntryType.MARKET else "Limit",
            "qty": self._fmt(qty),
            "takeProfit": self._fmt(Decimal(str(intent.take_profit))),
            "stopLoss": self._fmt(Decimal(str(intent.stop_loss))),
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "timeInForce": "GTC",
        }
        if intent.entry.type is EntryType.LIMIT:
            params["price"] = self._fmt(Decimal(str(intent.entry.price)))

        response = self._session.place_order(**params)
        if response.get("retCode") != 0:
            raise TradeExecutionError(f"Bybit rejected order: {response.get('retCode')} {response.get('retMsg')}")
        order_id = response.get("result", {}).get("orderId")
        if not order_id:
            raise TradeExecutionError("Bybit returned success without orderId")
        return str(order_id)

    def _last_price(self, symbol: str) -> Decimal:
        response = self._session.get_tickers(category="linear", symbol=symbol)
        items = response.get("result", {}).get("list", [])
        if not items:
            raise TradeExecutionError(f"No Bybit ticker found for {symbol}")
        return Decimal(items[0]["lastPrice"])

    def _quantity_for_notional(self, symbol: str, price: Decimal) -> Decimal:
        response = self._session.get_instruments_info(category="linear", symbol=symbol)
        items = response.get("result", {}).get("list", [])
        if not items:
            raise TradeExecutionError(f"No Bybit instrument found for {symbol}")

        lot = items[0]["lotSizeFilter"]
        step = Decimal(lot["qtyStep"])
        min_qty = Decimal(lot["minOrderQty"])
        min_notional = Decimal(lot.get("minNotionalValue", "0"))

        raw = self._notional_usdt / price
        qty = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty < min_qty:
            raise TradeExecutionError(f"Configured notional too small: qty {qty} < minOrderQty {min_qty}")
        if min_notional and qty * price < min_notional:
            raise TradeExecutionError(f"Configured notional too small: {qty * price} < minNotionalValue {min_notional}")
        return qty

    @staticmethod
    def _fmt(value: Decimal) -> str:
        return format(value.normalize(), "f")
