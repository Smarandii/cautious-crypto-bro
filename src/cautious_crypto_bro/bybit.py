from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import ROUND_DOWN, Decimal

import httpx

from .domain import EntryType, Side, TradingIntent

logger = logging.getLogger(__name__)

DEMO_BASE_URL = "https://api-demo.bybit.com"
RECV_WINDOW_MS = 5_000
CLOCK_SYNC_TTL_SECONDS = 300


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class TradeExecutionError(RuntimeError):
    pass


class BybitDemoExecutor:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        notional_usdt: float,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._notional_usdt = Decimal(str(notional_usdt))

        self._client = httpx.Client(
            base_url=DEMO_BASE_URL,
            timeout=httpx.Timeout(10.0),
        )

        self._clock_offset_ms = 0
        self._clock_synced_at = 0.0

    async def execute(self, intent: TradingIntent) -> str:
        return await asyncio.to_thread(self._execute_sync, intent)

    def close(self) -> None:
        self._client.close()

    def _execute_sync(self, intent: TradingIntent) -> str:
        self._sync_clock()

        market_price = self._last_price(intent.symbol)

        sizing_price = (
            Decimal(str(intent.entry.price))
            if intent.entry.type is EntryType.LIMIT
            else market_price
        )

        if intent.entry.type is EntryType.MARKET:
            self._validate_market_geometry(intent, market_price)

        qty = self._quantity_for_notional(intent.symbol, sizing_price)

        params: dict[str, object] = {
            "category": "linear",
            "symbol": intent.symbol,
            "side": "Buy" if intent.side is Side.LONG else "Sell",
            "orderType": (
                "Market"
                if intent.entry.type is EntryType.MARKET
                else "Limit"
            ),
            "qty": self._fmt(qty),
            "takeProfit": self._fmt(
                Decimal(str(intent.take_profit))
            ),
            "stopLoss": self._fmt(
                Decimal(str(intent.stop_loss))
            ),
            "tpslMode": "Full",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "timeInForce": "GTC",
        }

        if intent.entry.type is EntryType.LIMIT:
            params["price"] = self._fmt(
                Decimal(str(intent.entry.price))
            )

        response = self._private_post(
            "/v5/order/create",
            params,
        )

        order_id = response.get("result", {}).get("orderId")

        if not order_id:
            raise TradeExecutionError(
                "Bybit returned success without orderId"
            )

        return str(order_id)

    def _last_price(self, symbol: str) -> Decimal:
        response = self._public_get(
            "/v5/market/tickers",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        items = response.get("result", {}).get("list", [])

        if not items:
            raise TradeExecutionError(
                f"No Bybit ticker found for {symbol}"
            )

        return Decimal(items[0]["lastPrice"])

    def _quantity_for_notional(
        self,
        symbol: str,
        price: Decimal,
    ) -> Decimal:
        response = self._public_get(
            "/v5/market/instruments-info",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        items = response.get("result", {}).get("list", [])

        if not items:
            raise TradeExecutionError(
                f"No Bybit instrument found for {symbol}"
            )

        lot = items[0]["lotSizeFilter"]

        step = Decimal(lot["qtyStep"])
        min_qty = Decimal(lot["minOrderQty"])
        min_notional = Decimal(
            lot.get("minNotionalValue", "0")
        )

        raw = self._notional_usdt / price
        qty = (
            (raw / step).to_integral_value(
                rounding=ROUND_DOWN
            )
            * step
        )

        if qty < min_qty:
            minimum = min_qty * price
            raise TradeExecutionError(
                f"Configured notional "
                f"{self._notional_usdt} USDT is too small "
                f"for {symbol}; minimum is approximately "
                f"{minimum:.2f} USDT"
            )

        if min_notional and qty * price < min_notional:
            raise TradeExecutionError(
                f"Configured notional too small: "
                f"{qty * price} < "
                f"minNotionalValue {min_notional}"
            )

        return qty

    def _sync_clock(self, *, force: bool = False) -> None:
        if (
            not force
            and self._clock_synced_at
            and time.monotonic() - self._clock_synced_at
            < CLOCK_SYNC_TTL_SECONDS
        ):
            return

        last_error: Exception | None = None

        for attempt in range(3):
            try:
                t0 = _wall_clock_ms()

                response = self._client.get(
                    "/v5/market/time"
                )

                t1 = _wall_clock_ms()

                response.raise_for_status()
                data = response.json()

                if str(data.get("retCode", 0)) != "0":
                    raise TradeExecutionError(
                        "Bybit server-time request failed: "
                        f"{data.get('retCode')} "
                        f"{data.get('retMsg')}"
                    )

                server_ms = self._server_time_ms(data)
                midpoint_ms = (t0 + t1) // 2

                self._clock_offset_ms = (
                    server_ms - midpoint_ms
                )
                self._clock_synced_at = time.monotonic()

                logger.info(
                    "Bybit clock offset %+d ms (RTT %d ms)",
                    self._clock_offset_ms,
                    t1 - t0,
                )

                return

            except Exception as exc:
                last_error = exc

                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))

        raise TradeExecutionError(
            f"Could not synchronize clock with Bybit: "
            f"{last_error}"
        )

    @staticmethod
    def _server_time_ms(data: dict) -> int:
        if data.get("time") is not None:
            return int(data["time"])

        result = data.get("result") or {}

        if result.get("timeNano") is not None:
            return int(result["timeNano"]) // 1_000_000

        if result.get("timeSecond") is not None:
            return int(result["timeSecond"]) * 1000

        raise TradeExecutionError(
            "Bybit server-time response contained no timestamp"
        )

    def _auth_timestamp(self) -> str:
        self._sync_clock()

        return str(
            _wall_clock_ms() + self._clock_offset_ms
        )

    def _private_post(
        self,
        path: str,
        body: dict[str, object],
    ) -> dict:
        body_json = json.dumps(
            body,
            separators=(",", ":"),
            ensure_ascii=False,
        )

        for attempt in range(2):
            timestamp = self._auth_timestamp()

            payload = (
                timestamp
                + self._api_key
                + str(RECV_WINDOW_MS)
                + body_json
            )

            signature = hmac.new(
                self._api_secret.encode(),
                payload.encode(),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(
                    RECV_WINDOW_MS
                ),
                "X-BAPI-SIGN": signature,
                "Content-Type": "application/json",
            }

            try:
                response = self._client.post(
                    path,
                    content=body_json,
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise TradeExecutionError(
                    f"Bybit HTTP request failed: {exc}"
                ) from exc

            data = response.json()
            code = data.get("retCode")

            if str(code) == "0":
                return data

            if str(code) == "10002" and attempt == 0:
                logger.warning(
                    "Bybit rejected request timestamp; "
                    "re-synchronizing clock"
                )
                self._sync_clock(force=True)
                continue

            raise TradeExecutionError(
                f"Bybit rejected order: "
                f"{code} {data.get('retMsg')}"
            )

        raise TradeExecutionError(
            "Bybit request failed after clock re-sync"
        )

    def _public_get(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict:
        try:
            response = self._client.get(
                path,
                params=params,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise TradeExecutionError(
                f"Bybit HTTP request failed: {exc}"
            ) from exc

        data = response.json()

        if str(data.get("retCode", 0)) != "0":
            raise TradeExecutionError(
                f"Bybit rejected request: "
                f"{data.get('retCode')} "
                f"{data.get('retMsg')}"
            )

        return data

    @staticmethod
    def _validate_market_geometry(
        intent: TradingIntent,
        market_price: Decimal,
    ) -> None:
        stop_loss = Decimal(str(intent.stop_loss))
        take_profit = Decimal(str(intent.take_profit))

        if (
            intent.side is Side.LONG
            and not stop_loss
            < market_price
            < take_profit
        ):
            raise TradeExecutionError(
                "Market price is outside LONG "
                "stop/target geometry"
            )

        if (
            intent.side is Side.SHORT
            and not take_profit
            < market_price
            < stop_loss
        ):
            raise TradeExecutionError(
                "Market price is outside SHORT "
                "stop/target geometry"
            )

    @staticmethod
    def _fmt(value: Decimal) -> str:
        return format(value.normalize(), "f")
