from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal

import httpx

from .domain import (
    ExecutionOrderType,
    ExecutionPlan,
    Side,
)
from .execution import InstrumentContext

logger = logging.getLogger(__name__)

DEMO_BASE_URL = (
    "https://api-demo.bybit.com"
)
RECV_WINDOW_MS = 5_000
CLOCK_SYNC_TTL_SECONDS = 300


def _wall_clock_ms() -> int:
    return (
        time.time_ns()
        // 1_000_000
    )


class TradeExecutionError(
    RuntimeError
):
    pass


class BybitDemoExecutor:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._api_key = api_key
        self._api_secret = (
            api_secret
        )

        self._client = httpx.Client(
            base_url=DEMO_BASE_URL,
            timeout=httpx.Timeout(
                10.0
            ),
        )

        self._clock_offset_ms = 0
        self._clock_synced_at = 0.0

    async def market_context(
        self,
        symbol: str,
    ) -> InstrumentContext:
        return await asyncio.to_thread(
            self._market_context_sync,
            symbol,
        )

    async def execute(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._execute_sync,
            plan,
        )

    def close(self) -> None:
        self._client.close()

    def _market_context_sync(
        self,
        symbol: str,
    ) -> InstrumentContext:
        market_price = (
            self._last_price(
                symbol
            )
        )

        response = self._public_get(
            "/v5/market/instruments-info",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        items = (
            response
            .get("result", {})
            .get("list", [])
        )

        if not items:
            raise TradeExecutionError(
                "No Bybit instrument "
                f"found for {symbol}"
            )

        instrument = items[0]
        lot = instrument[
            "lotSizeFilter"
        ]
        price_filter = instrument[
            "priceFilter"
        ]

        return InstrumentContext(
            market_price=market_price,
            tick_size=Decimal(
                price_filter[
                    "tickSize"
                ]
            ),
            qty_step=Decimal(
                lot["qtyStep"]
            ),
            min_qty=Decimal(
                lot["minOrderQty"]
            ),
            min_notional=Decimal(
                lot.get(
                    "minNotionalValue",
                    "0",
                )
            ),
        )

    def _execute_sync(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        self._sync_clock()

        if any(
            order.order_type
            is ExecutionOrderType.MARKET
            for order in plan.orders
        ):
            if len(plan.orders) != 1:
                raise TradeExecutionError(
                    "Market execution plan "
                    "must contain exactly one order"
                )

            market_price = (
                self._last_price(
                    plan.symbol
                )
            )

            self._validate_market_plan(
                plan,
                market_price,
            )

        requests = [
            self._order_params(
                plan,
                index,
            )
            for index in range(
                len(plan.orders)
            )
        ]

        if len(requests) == 1:
            body = {
                "category": "linear",
                **requests[0],
            }

            response = (
                self._private_post(
                    "/v5/order/create",
                    body,
                )
            )

            order_id = (
                response
                .get("result", {})
                .get("orderId")
            )

            if not order_id:
                raise TradeExecutionError(
                    "Bybit returned success "
                    "without orderId"
                )

            return (
                str(order_id),
            )

        response = self._private_post(
            "/v5/order/create-batch",
            {
                "category": "linear",
                "request": requests,
            },
        )

        return self._parse_batch_result(
            plan.symbol,
            requests,
            response,
        )

    def _order_params(
        self,
        plan: ExecutionPlan,
        index: int,
    ) -> dict[str, object]:
        order = plan.orders[
            index
        ]

        params: dict[
            str,
            object,
        ] = {
            "symbol": plan.symbol,
            "side": (
                "Buy"
                if plan.side
                is Side.LONG
                else "Sell"
            ),
            "orderType": (
                "Market"
                if order.order_type
                is ExecutionOrderType.MARKET
                else "Limit"
            ),
            "qty": self._fmt(
                order.quantity
            ),
            "takeProfit": self._fmt(
                plan.take_profit
            ),
            "stopLoss": self._fmt(
                plan.stop_loss
            ),
            "tpslMode": "Partial",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "timeInForce": (
                "IOC"
                if order.order_type
                is ExecutionOrderType.MARKET
                else "GTC"
            ),
            "positionIdx": 0,
            "orderLinkId": (
                f"ccb-"
                f"{plan.intent_id.hex[:24]}"
                f"-{index + 1}"
            ),
        }

        if (
            order.order_type
            is ExecutionOrderType.LIMIT
        ):
            assert (
                order.price is not None
            )

            params["price"] = (
                self._fmt(
                    order.price
                )
            )

        return params

    def _parse_batch_result(
        self,
        symbol: str,
        requests: list[
            dict[str, object]
        ],
        response: dict,
    ) -> tuple[str, ...]:
        results = (
            response
            .get("result", {})
            .get("list", [])
        )

        statuses = (
            response
            .get("retExtInfo", {})
            .get("list", [])
        )

        if (
            len(results)
            != len(requests)
            or len(statuses)
            != len(requests)
        ):
            self._cancel_batch_best_effort(
                symbol,
                [
                    str(
                        request[
                            "orderLinkId"
                        ]
                    )
                    for request
                    in requests
                ],
            )

            raise TradeExecutionError(
                "Bybit batch response "
                "did not match submitted "
                "order count"
            )

        order_ids: list[
            str
        ] = []
        accepted_link_ids: list[
            str
        ] = []
        failures: list[
            str
        ] = []

        for index, (
            result,
            status,
        ) in enumerate(
            zip(
                results,
                statuses,
                strict=True,
            )
        ):
            code = status.get(
                "code"
            )

            order_id = result.get(
                "orderId"
            )

            link_id = str(
                requests[index][
                    "orderLinkId"
                ]
            )

            if (
                str(code) == "0"
                and order_id
            ):
                order_ids.append(
                    str(order_id)
                )
                accepted_link_ids.append(
                    link_id
                )
            else:
                failures.append(
                    f"order {index + 1}: "
                    f"{code} "
                    f"{status.get('msg')}"
                )

        if failures:
            if accepted_link_ids:
                self._cancel_batch_best_effort(
                    symbol,
                    accepted_link_ids,
                )

            raise TradeExecutionError(
                "Bybit batch partially "
                "failed: "
                + "; ".join(
                    failures
                )
            )

        return tuple(order_ids)

    def _cancel_batch_best_effort(
        self,
        symbol: str,
        order_link_ids: list[
            str
        ],
    ) -> None:
        if not order_link_ids:
            return

        try:
            response = (
                self._private_post(
                    "/v5/order/cancel-batch",
                    {
                        "category": "linear",
                        "request": [
                            {
                                "symbol": symbol,
                                "orderLinkId": (
                                    order_link_id
                                ),
                            }
                            for order_link_id
                            in order_link_ids
                        ],
                    },
                )
            )

            statuses = (
                response
                .get(
                    "retExtInfo",
                    {},
                )
                .get(
                    "list",
                    [],
                )
            )

            failed = [
                status
                for status
                in statuses
                if str(
                    status.get(
                        "code"
                    )
                )
                != "0"
            ]

            if failed:
                logger.error(
                    "Rollback cancellation "
                    "returned failures: %s",
                    failed,
                )
        except Exception:
            logger.exception(
                "Failed to roll back "
                "partially accepted Bybit batch"
            )

    def _validate_market_plan(
        self,
        plan: ExecutionPlan,
        market_price: Decimal,
    ) -> None:
        order = plan.orders[0]

        if (
            plan.side is Side.LONG
            and not (
                plan.stop_loss
                < market_price
                < plan.take_profit
            )
        ):
            raise TradeExecutionError(
                "Market price is outside "
                "LONG stop/target geometry"
            )

        if (
            plan.side is Side.SHORT
            and not (
                plan.take_profit
                < market_price
                < plan.stop_loss
            )
        ):
            raise TradeExecutionError(
                "Market price is outside "
                "SHORT stop/target geometry"
            )

        current_risk = (
            order.quantity
            * abs(
                market_price
                - plan.stop_loss
            )
        )

        if (
            current_risk
            > plan.policy.risk_budget_usdt
        ):
            raise TradeExecutionError(
                "Market moved enough that "
                "execution would exceed "
                "the configured risk budget"
            )

    def _last_price(
        self,
        symbol: str,
    ) -> Decimal:
        response = self._public_get(
            "/v5/market/tickers",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        items = (
            response
            .get("result", {})
            .get("list", [])
        )

        if not items:
            raise TradeExecutionError(
                "No Bybit ticker found "
                f"for {symbol}"
            )

        return Decimal(
            items[0]["lastPrice"]
        )

    def _sync_clock(
        self,
        *,
        force: bool = False,
    ) -> None:
        if (
            not force
            and self._clock_synced_at
            and (
                time.monotonic()
                - self._clock_synced_at
            )
            < CLOCK_SYNC_TTL_SECONDS
        ):
            return

        last_error: (
            Exception | None
        ) = None

        for attempt in range(3):
            try:
                t0 = (
                    _wall_clock_ms()
                )

                response = (
                    self._client.get(
                        "/v5/market/time"
                    )
                )

                t1 = (
                    _wall_clock_ms()
                )

                response.raise_for_status()
                data = response.json()

                if (
                    str(
                        data.get(
                            "retCode",
                            0,
                        )
                    )
                    != "0"
                ):
                    raise TradeExecutionError(
                        "Bybit server-time "
                        "request failed: "
                        f"{data.get('retCode')} "
                        f"{data.get('retMsg')}"
                    )

                server_ms = (
                    self._server_time_ms(
                        data
                    )
                )

                midpoint_ms = (
                    t0 + t1
                ) // 2

                self._clock_offset_ms = (
                    server_ms
                    - midpoint_ms
                )

                self._clock_synced_at = (
                    time.monotonic()
                )

                logger.info(
                    "Bybit clock offset "
                    "%+d ms (RTT %d ms)",
                    self._clock_offset_ms,
                    t1 - t0,
                )

                return

            except Exception as exc:
                last_error = exc

                if attempt < 2:
                    time.sleep(
                        0.25
                        * (
                            attempt
                            + 1
                        )
                    )

        raise TradeExecutionError(
            "Could not synchronize "
            "clock with Bybit: "
            f"{last_error}"
        )

    @staticmethod
    def _server_time_ms(
        data: dict,
    ) -> int:
        if data.get(
            "time"
        ) is not None:
            return int(
                data["time"]
            )

        result = (
            data.get("result")
            or {}
        )

        if (
            result.get(
                "timeNano"
            )
            is not None
        ):
            return (
                int(
                    result[
                        "timeNano"
                    ]
                )
                // 1_000_000
            )

        if (
            result.get(
                "timeSecond"
            )
            is not None
        ):
            return (
                int(
                    result[
                        "timeSecond"
                    ]
                )
                * 1000
            )

        raise TradeExecutionError(
            "Bybit server-time response "
            "contained no timestamp"
        )

    def _auth_timestamp(
        self,
    ) -> str:
        self._sync_clock()

        return str(
            _wall_clock_ms()
            + self._clock_offset_ms
        )

    def _private_post(
        self,
        path: str,
        body: dict[
            str,
            object,
        ],
    ) -> dict:
        body_json = json.dumps(
            body,
            separators=(
                ",",
                ":",
            ),
            ensure_ascii=False,
        )

        for attempt in range(2):
            timestamp = (
                self._auth_timestamp()
            )

            payload = (
                timestamp
                + self._api_key
                + str(
                    RECV_WINDOW_MS
                )
                + body_json
            )

            signature = hmac.new(
                self._api_secret.encode(),
                payload.encode(),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": (
                    self._api_key
                ),
                "X-BAPI-TIMESTAMP": (
                    timestamp
                ),
                "X-BAPI-RECV-WINDOW": (
                    str(
                        RECV_WINDOW_MS
                    )
                ),
                "X-BAPI-SIGN": (
                    signature
                ),
                "Content-Type": (
                    "application/json"
                ),
            }

            try:
                response = (
                    self._client.post(
                        path,
                        content=body_json,
                        headers=headers,
                    )
                )

                response.raise_for_status()

            except httpx.HTTPError as exc:
                raise TradeExecutionError(
                    "Bybit HTTP request "
                    f"failed: {exc}"
                ) from exc

            data = response.json()
            code = data.get(
                "retCode"
            )

            if str(code) == "0":
                return data

            if (
                str(code) == "10002"
                and attempt == 0
            ):
                logger.warning(
                    "Bybit rejected request "
                    "timestamp; "
                    "re-synchronizing clock"
                )

                self._sync_clock(
                    force=True
                )

                continue

            raise TradeExecutionError(
                "Bybit rejected request: "
                f"{code} "
                f"{data.get('retMsg')}"
            )

        raise TradeExecutionError(
            "Bybit request failed "
            "after clock re-sync"
        )

    def _public_get(
        self,
        path: str,
        params: dict[
            str,
            str,
        ],
    ) -> dict:
        try:
            response = (
                self._client.get(
                    path,
                    params=params,
                )
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise TradeExecutionError(
                "Bybit HTTP request "
                f"failed: {exc}"
            ) from exc

        data = response.json()

        if (
            str(
                data.get(
                    "retCode",
                    0,
                )
            )
            != "0"
        ):
            raise TradeExecutionError(
                "Bybit rejected request: "
                f"{data.get('retCode')} "
                f"{data.get('retMsg')}"
            )

        return data

    @staticmethod
    def _fmt(
        value: Decimal,
    ) -> str:
        return format(
            value.normalize(),
            "f",
        )
