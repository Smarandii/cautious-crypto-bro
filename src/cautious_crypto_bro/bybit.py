from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import ROUND_DOWN, Decimal
from urllib.parse import urlencode

import httpx

from .domain import (
    ClosedPnlRecord,
    ExecutionOrderType,
    ExecutionPlan,
    PositionActionIntent,
    PositionActionType,
    Side,
)
from .execution import InstrumentContext

logger = logging.getLogger(__name__)

DEMO_BASE_URL = "https://api-demo.bybit.com"
RECV_WINDOW_MS = 5_000
CLOCK_SYNC_TTL_SECONDS = 300


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class TradeExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PositionExposure:
    side: Side
    size: Decimal
    avg_price: Decimal


@dataclass(frozen=True, slots=True)
class PositionActionExecutionResult:
    order_id: str
    position_side: Side
    position_size_before: Decimal
    submitted_quantity: Decimal | None
    cancelled_entry_orders: int


@dataclass(frozen=True, slots=True)
class OpenOrderExposure:
    side: Side
    remaining_quantity: Decimal
    order_id: str
    order_link_id: str
    price: Decimal | None


@dataclass(frozen=True, slots=True)
class SymbolExposure:
    symbol: str
    positions: tuple[
        PositionExposure,
        ...,
    ] = ()
    pending_entry_orders: tuple[
        OpenOrderExposure,
        ...,
    ] = ()


@dataclass(frozen=True, slots=True)
class AccountPosition:
    symbol: str
    side: Side
    size: Decimal
    avg_price: Decimal
    mark_price: Decimal
    unrealised_pnl: Decimal
    status: str
    take_profit: Decimal | None
    stop_loss: Decimal | None


@dataclass(frozen=True, slots=True)
class AccountOrder:
    symbol: str
    side: Side
    order_type: str
    status: str
    quantity: Decimal
    remaining_quantity: Decimal
    price: Decimal | None
    avg_price: Decimal | None
    order_id: str
    order_link_id: str
    reduce_only: bool
    updated_at: datetime
    stop_order_type: str = ""
    create_type: str = ""
    trigger_price: Decimal | None = None
    close_on_trigger: bool = False

    @property
    def kind(self) -> str:
        stop_type = self.stop_order_type

        if stop_type in {
            "TakeProfit",
            "PartialTakeProfit",
        }:
            return "TP"

        if stop_type in {
            "StopLoss",
            "PartialStopLoss",
        }:
            return "SL"

        if stop_type == "TrailingStop":
            return "TRAILING"

        if self.reduce_only or self.close_on_trigger:
            return "REDUCE"

        if stop_type == "Stop":
            return "CONDITIONAL"

        return "ENTRY"

    @property
    def is_protective(self) -> bool:
        return self.kind in {
            "TP",
            "SL",
            "TRAILING",
        }


@dataclass(frozen=True, slots=True)
class AccountStateSummary:
    as_of: datetime
    positions: tuple[
        AccountPosition,
        ...,
    ]
    open_orders: tuple[
        AccountOrder,
        ...,
    ]

    @property
    def unrealised_pnl(
        self,
    ) -> Decimal:
        return sum(
            (position.unrealised_pnl for position in self.positions),
            Decimal("0"),
        )

    def exposure_for(
        self,
        symbol: str,
    ) -> SymbolExposure:
        symbol = symbol.upper()

        positions = tuple(
            PositionExposure(
                side=position.side,
                size=position.size,
                avg_price=(position.avg_price),
            )
            for position in self.positions
            if position.symbol == symbol
        )

        pending = tuple(
            OpenOrderExposure(
                side=order.side,
                remaining_quantity=(order.remaining_quantity),
                order_id=order.order_id,
                order_link_id=(order.order_link_id),
                price=order.price,
            )
            for order in self.open_orders
            if (
                order.symbol == symbol
                and order.remaining_quantity > 0
                and not order.reduce_only
                and order.order_link_id.startswith("ccb-")
            )
        )

        return SymbolExposure(
            symbol=symbol,
            positions=positions,
            pending_entry_orders=pending,
        )


class BybitDemoExecutor:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

        self._client = httpx.Client(
            base_url=DEMO_BASE_URL,
            timeout=httpx.Timeout(10.0),
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

    async def account_state(
        self,
    ) -> AccountStateSummary:
        return await asyncio.to_thread(self._account_state_sync)

    async def wallet_balance_usdt(self) -> Decimal:
        return await asyncio.to_thread(self._wallet_balance_usdt_sync)

    async def closed_pnl_history(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]:
        return await asyncio.to_thread(
            self._closed_pnl_history_sync,
            start,
            end,
        )

    async def execute(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._execute_sync,
            plan,
        )

    async def execute_position_action(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult:
        return await asyncio.to_thread(
            self._execute_position_action_sync,
            action,
        )

    def close(self) -> None:
        self._client.close()

    def _wallet_balance_usdt_sync(
        self,
    ) -> Decimal:
        response = self._private_get(
            "/v5/account/wallet-balance",
            {
                "accountType": "UNIFIED",
            },
        )

        accounts = response.get("result", {}).get("list", [])

        if (
            not isinstance(accounts, list)
            or len(accounts) != 1
            or not isinstance(accounts[0], dict)
        ):
            raise TradeExecutionError(
                "Bybit wallet balance response did not contain exactly one account"
            )

        balance = self._decimal(accounts[0].get("totalWalletBalance"))

        if balance <= 0:
            raise TradeExecutionError("Bybit totalWalletBalance must be positive")

        return balance

    def _account_state_sync(
        self,
    ) -> AccountStateSummary:
        self._sync_clock()

        now = datetime.now(UTC)
        position_items = self._paginate_private_list(
            "/v5/position/list",
            {
                "category": "linear",
                "settleCoin": "USDT",
                "limit": 200,
            },
        )

        positions: list[AccountPosition] = []

        for item in position_items:
            size = self._decimal(item.get("size"))

            if size <= 0:
                continue

            symbol = str(item.get("symbol") or "").upper()

            if not symbol:
                continue

            avg_price = self._decimal(item.get("avgPrice"))

            mark_price = self._decimal(item.get("markPrice"))

            if avg_price <= 0 or mark_price <= 0:
                raise TradeExecutionError(
                    "Active Bybit position contains invalid pricing"
                )

            positions.append(
                AccountPosition(
                    symbol=symbol,
                    side=self._side_from_bybit(item.get("side")),
                    size=size,
                    avg_price=avg_price,
                    mark_price=mark_price,
                    unrealised_pnl=(self._decimal(item.get("unrealisedPnl"))),
                    status=str(item.get("positionStatus") or "Unknown"),
                    take_profit=(self._optional_decimal(item.get("takeProfit"))),
                    stop_loss=(self._optional_decimal(item.get("stopLoss"))),
                )
            )

        open_items = self._paginate_private_list(
            "/v5/order/realtime",
            {
                "category": "linear",
                "settleCoin": "USDT",
                "openOnly": 0,
                "limit": 50,
            },
        )

        open_orders = tuple(
            self._account_order_from_item(item)
            for item in open_items
            if self._decimal(item.get("leavesQty")) > 0
        )

        return AccountStateSummary(
            as_of=now,
            positions=tuple(
                sorted(
                    positions,
                    key=lambda position: position.symbol,
                )
            ),
            open_orders=tuple(
                sorted(
                    open_orders,
                    key=lambda order: order.updated_at,
                    reverse=True,
                )
            ),
        )

    def _closed_pnl_history_sync(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]:
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Closed-PnL range must be timezone-aware")

        start = start.astimezone(UTC)
        end = end.astimezone(UTC)

        if end <= start:
            return ()

        self._sync_clock()

        records: dict[
            str,
            ClosedPnlRecord,
        ] = {}

        window_start = start

        while window_start < end:
            window_end = min(
                window_start + timedelta(days=7),
                end,
            )

            items = self._paginate_private_list(
                "/v5/position/closed-pnl",
                {
                    "category": "linear",
                    "startTime": int(window_start.timestamp() * 1000),
                    "endTime": int(window_end.timestamp() * 1000),
                    "limit": 100,
                },
            )

            for item in items:
                record = self._closed_pnl_record_from_item(item)

                records[record.record_id] = record

            window_start = window_end

        return tuple(
            sorted(
                records.values(),
                key=lambda item: item.updated_at,
            )
        )

    def _closed_pnl_record_from_item(
        self,
        item: dict,
    ) -> ClosedPnlRecord:
        symbol = str(item.get("symbol") or "").upper()

        order_id = str(item.get("orderId") or "")

        updated_ms = int(item.get("updatedTime") or item.get("createdTime") or 0)

        if not symbol:
            raise TradeExecutionError("Closed-PnL record has no symbol")

        if updated_ms <= 0:
            raise TradeExecutionError("Closed-PnL record has no timestamp")

        if order_id:
            record_id = f"{symbol}:{order_id}"

        else:
            # Some non-standard settlement records
            # may not carry an orderId. Build a
            # deterministic identifier rather than
            # silently dropping realized P&L.
            identity = {
                "symbol": symbol,
                "side": item.get("side"),
                "execType": item.get("execType"),
                "closedSize": (item.get("closedSize")),
                "closedPnl": (item.get("closedPnl")),
                "avgEntryPrice": (item.get("avgEntryPrice")),
                "avgExitPrice": (item.get("avgExitPrice")),
                "updatedTime": updated_ms,
            }

            digest = hashlib.sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            record_id = f"{symbol}:synthetic:{digest}"

        close_side = str(item.get("side") or "")

        if close_side == "Sell":
            position_side = Side.LONG
        elif close_side == "Buy":
            position_side = Side.SHORT
        else:
            raise TradeExecutionError("Closed-PnL record has invalid side")

        return ClosedPnlRecord(
            record_id=record_id,
            order_id=order_id,
            symbol=symbol,
            position_side=position_side,
            closed_pnl=self._decimal(item.get("closedPnl")),
            closed_size=self._decimal(item.get("closedSize")),
            avg_entry_price=(self._optional_decimal(item.get("avgEntryPrice"))),
            avg_exit_price=(self._optional_decimal(item.get("avgExitPrice"))),
            updated_at=datetime.fromtimestamp(
                updated_ms / 1000,
                tz=UTC,
            ),
        )

    def _paginate_private_list(
        self,
        path: str,
        params: dict[
            str,
            object,
        ],
    ) -> list[dict]:
        params = dict(params)
        items: list[dict] = []

        while True:
            response = self._private_get(
                path,
                params,
            )

            result = response.get("result", {})

            page = result.get(
                "list",
                [],
            )

            items.extend(
                item
                for item in page
                if isinstance(
                    item,
                    dict,
                )
            )

            cursor = str(result.get("nextPageCursor") or "")

            if not cursor:
                break

            params["cursor"] = cursor

        return items

    def _account_order_from_item(
        self,
        item: dict,
    ) -> AccountOrder:
        updated_ms = int(item.get("updatedTime") or item.get("createdTime") or 0)

        return AccountOrder(
            symbol=str(item.get("symbol") or "").upper(),
            side=self._side_from_bybit(item.get("side")),
            order_type=str(item.get("orderType") or "Unknown"),
            status=str(item.get("orderStatus") or "Unknown"),
            quantity=self._decimal(item.get("qty")),
            remaining_quantity=(self._decimal(item.get("leavesQty"))),
            price=self._optional_decimal(item.get("price")),
            avg_price=(self._optional_decimal(item.get("avgPrice"))),
            order_id=str(item.get("orderId") or ""),
            order_link_id=str(item.get("orderLinkId") or ""),
            reduce_only=(
                item.get("reduceOnly") is True
                or str(item.get("reduceOnly")).casefold() == "true"
            ),
            updated_at=datetime.fromtimestamp(
                updated_ms / 1000,
                tz=UTC,
            ),
            stop_order_type=str(item.get("stopOrderType") or ""),
            create_type=str(item.get("createType") or ""),
            trigger_price=(self._optional_decimal(item.get("triggerPrice"))),
            close_on_trigger=(
                item.get("closeOnTrigger") is True
                or str(item.get("closeOnTrigger")).casefold() == "true"
            ),
        )

    def _exposure_sync(
        self,
        symbol: str,
    ) -> SymbolExposure:
        self._sync_clock()

        position_response = self._private_get(
            "/v5/position/list",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        positions: list[PositionExposure] = []

        position_items = position_response.get("result", {}).get("list", [])

        for item in position_items:
            if not isinstance(
                item,
                dict,
            ):
                continue

            size = Decimal(str(item.get("size") or "0"))

            if size <= 0:
                continue

            avg_price_raw = item.get("avgPrice")

            if avg_price_raw is None or str(avg_price_raw).strip() == "":
                raise TradeExecutionError(
                    "Active Bybit position contains no average price"
                )

            positions.append(
                PositionExposure(
                    side=(self._side_from_bybit(item.get("side"))),
                    size=size,
                    avg_price=Decimal(str(avg_price_raw)),
                )
            )

        pending_orders: list[OpenOrderExposure] = []

        order_params: dict[
            str,
            object,
        ] = {
            "category": "linear",
            "symbol": symbol,
            "openOnly": 0,
            "orderFilter": "Order",
            "limit": 50,
        }

        while True:
            order_response = self._private_get(
                "/v5/order/realtime",
                order_params,
            )

            result = order_response.get("result", {})

            order_items = result.get(
                "list",
                [],
            )

            for item in order_items:
                if not isinstance(
                    item,
                    dict,
                ):
                    continue

                order_link_id = str(item.get("orderLinkId") or "")

                # Only warn about pending entry
                # orders created by this app.
                if not order_link_id.startswith("ccb-"):
                    continue

                reduce_only = item.get("reduceOnly")

                if reduce_only is True or str(reduce_only).casefold() == "true":
                    continue

                remaining = Decimal(str(item.get("leavesQty") or "0"))

                if remaining <= 0:
                    continue

                price_raw = str(item.get("price") or "").strip()

                price = (
                    None
                    if price_raw
                    in {
                        "",
                        "0",
                        "0.0",
                        "0.00",
                    }
                    else Decimal(price_raw)
                )

                pending_orders.append(
                    OpenOrderExposure(
                        side=(self._side_from_bybit(item.get("side"))),
                        remaining_quantity=(remaining),
                        order_id=str(item.get("orderId") or ""),
                        order_link_id=(order_link_id),
                        price=price,
                    )
                )

            cursor = str(result.get("nextPageCursor") or "")

            if not cursor:
                break

            order_params["cursor"] = cursor

        return SymbolExposure(
            symbol=symbol,
            positions=tuple(positions),
            pending_entry_orders=tuple(pending_orders),
        )

    def _execute_position_action_sync(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult:
        self._sync_clock()

        initial_exposure = self._exposure_sync(action.symbol)

        # Validate before causing any side effects.
        self._position_for_action(
            action,
            initial_exposure,
        )

        # A lifecycle instruction supersedes stale
        # CCB entry orders for this symbol. Otherwise
        # an old scale-in order could rebuild exposure
        # immediately after a REDUCE/CLOSE.
        cancelled_entries = self._cancel_ccb_entry_orders_sync(action.symbol)

        # Resolve position size again after cancellation.
        # An entry could have filled concurrently while
        # cancellations were being processed.
        current_exposure = self._exposure_sync(action.symbol)

        position = self._position_for_action(
            action,
            current_exposure,
        )

        submitted_quantity: Decimal | None

        if action.action is PositionActionType.CLOSE:
            submitted_quantity = None
            qty = "0"

        else:
            assert action.close_pct is not None

            quantity = self._partial_reduce_quantity(
                action.symbol,
                position.size,
                Decimal(str(action.close_pct)),
            )

            submitted_quantity = quantity
            qty = self._fmt(quantity)

        side = "Sell" if position.side is Side.LONG else "Buy"

        body: dict[
            str,
            object,
        ] = {
            "category": "linear",
            "symbol": action.symbol,
            "side": side,
            "orderType": "Market",
            "qty": qty,
            "timeInForce": "IOC",
            "positionIdx": 0,
            "reduceOnly": True,
            "orderLinkId": (f"ccb-action-{action.action_id.hex[:24]}"),
        }

        if action.action is PositionActionType.CLOSE:
            body["closeOnTrigger"] = True

        response = self._private_post(
            "/v5/order/create",
            body,
        )

        order_id = response.get("result", {}).get("orderId")

        if not order_id:
            raise TradeExecutionError("Bybit returned success without orderId")

        return PositionActionExecutionResult(
            order_id=str(order_id),
            position_side=position.side,
            position_size_before=position.size,
            submitted_quantity=(submitted_quantity),
            cancelled_entry_orders=(cancelled_entries),
        )

    @staticmethod
    def _position_for_action(
        action: PositionActionIntent,
        exposure: SymbolExposure,
    ) -> PositionExposure:
        if not exposure.positions:
            raise TradeExecutionError(f"No active position for {action.symbol}")

        if len(exposure.positions) != 1:
            raise TradeExecutionError(
                f"Expected exactly one active position for {action.symbol}"
            )

        position = exposure.positions[0]

        if (
            action.expected_side is not None
            and position.side is not action.expected_side
        ):
            raise TradeExecutionError(
                "Current position side does not "
                "match signal expectation: "
                f"current={position.side.value}, "
                f"expected="
                f"{action.expected_side.value}"
            )

        return position

    def _instrument_info(
        self,
        symbol: str,
    ) -> dict:
        response = self._public_get(
            "/v5/market/instruments-info",
            {
                "category": "linear",
                "symbol": symbol,
            },
        )

        items = response.get("result", {}).get(
            "list",
            [],
        )

        if not items:
            raise TradeExecutionError(f"No Bybit instrument found for {symbol}")

        return items[0]

    def _partial_reduce_quantity(
        self,
        symbol: str,
        position_size: Decimal,
        close_pct: Decimal,
    ) -> Decimal:
        lot = self._instrument_info(symbol)["lotSizeFilter"]

        qty_step = Decimal(str(lot["qtyStep"]))
        min_qty = Decimal(str(lot["minOrderQty"]))

        raw = position_size * close_pct / Decimal("100")

        units = (raw / qty_step).to_integral_value(rounding=ROUND_DOWN)

        quantity = units * qty_step

        if quantity <= 0 or quantity < min_qty:
            raise TradeExecutionError(
                "Requested partial reduction rounds below Bybit minimum quantity"
            )

        if quantity >= position_size:
            raise TradeExecutionError(
                "REDUCE would close the full position; use CLOSE instead"
            )

        return quantity

    def _cancel_ccb_entry_orders_sync(
        self,
        symbol: str,
    ) -> int:
        cancelled: set[str] = set()

        for _ in range(5):
            exposure = self._exposure_sync(symbol)

            pending = exposure.pending_entry_orders

            if not pending:
                return len(cancelled)

            for order in pending:
                link_id = order.order_link_id

                if link_id in cancelled:
                    continue

                response = self._private_post(
                    "/v5/order/cancel",
                    {
                        "category": "linear",
                        "symbol": symbol,
                        "orderLinkId": link_id,
                    },
                )

                result = response.get(
                    "result",
                    {},
                )

                if not result.get("orderId"):
                    raise TradeExecutionError(
                        "Bybit accepted no order ID "
                        "while cancelling CCB entry "
                        f"{link_id}"
                    )

                cancelled.add(link_id)

            time.sleep(0.25)

        remaining = self._exposure_sync(symbol).pending_entry_orders

        if remaining:
            raise TradeExecutionError(
                "CCB entry orders are still active "
                "after cancellation; refusing full close"
            )

        return len(cancelled)

    def _market_context_sync(
        self,
        symbol: str,
    ) -> InstrumentContext:
        market_price = self._last_price(symbol)
        instrument = self._instrument_info(symbol)

        lot = instrument["lotSizeFilter"]
        price_filter = instrument["priceFilter"]

        return InstrumentContext(
            market_price=market_price,
            tick_size=Decimal(price_filter["tickSize"]),
            qty_step=Decimal(lot["qtyStep"]),
            min_qty=Decimal(lot["minOrderQty"]),
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

        market_orders = [
            order
            for order in plan.orders
            if (order.order_type is ExecutionOrderType.MARKET)
        ]

        if market_orders:
            if len(market_orders) != len(plan.orders):
                raise TradeExecutionError(
                    "Execution plan cannot mix MARKET and LIMIT orders"
                )

            market_price = self._last_price(plan.symbol)

            self._validate_market_plan(
                plan,
                market_price,
            )

        requests = [
            self._order_params(
                plan,
                index,
            )
            for index in range(len(plan.orders))
        ]

        if len(requests) == 1:
            body = {
                "category": "linear",
                **requests[0],
            }

            response = self._private_post(
                "/v5/order/create",
                body,
            )

            order_id = response.get("result", {}).get("orderId")

            if not order_id:
                raise TradeExecutionError("Bybit returned success without orderId")

            return (str(order_id),)

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
        order = plan.orders[index]

        take_profit = (
            order.take_profit if order.take_profit is not None else plan.take_profit
        )

        params: dict[
            str,
            object,
        ] = {
            "symbol": plan.symbol,
            "side": ("Buy" if plan.side is Side.LONG else "Sell"),
            "orderType": (
                "Market" if order.order_type is ExecutionOrderType.MARKET else "Limit"
            ),
            "qty": self._fmt(order.quantity),
            "takeProfit": self._fmt(take_profit),
            "stopLoss": self._fmt(plan.stop_loss),
            "tpslMode": "Partial",
            "tpOrderType": "Market",
            "slOrderType": "Market",
            "timeInForce": (
                "IOC" if order.order_type is ExecutionOrderType.MARKET else "GTC"
            ),
            "positionIdx": 0,
            "orderLinkId": (f"ccb-{plan.intent_id.hex[:24]}-{index + 1}"),
        }

        if order.order_type is ExecutionOrderType.LIMIT:
            assert order.price is not None

            params["price"] = self._fmt(order.price)

        return params

    def _parse_batch_result(
        self,
        symbol: str,
        requests: list[dict[str, object]],
        response: dict,
    ) -> tuple[str, ...]:
        results = response.get("result", {}).get("list", [])

        statuses = response.get("retExtInfo", {}).get("list", [])

        if len(results) != len(requests) or len(statuses) != len(requests):
            self._cancel_batch_best_effort(
                symbol,
                [str(request["orderLinkId"]) for request in requests],
            )

            raise TradeExecutionError(
                "Bybit batch response did not match submitted order count"
            )

        order_ids: list[str] = []
        accepted_link_ids: list[str] = []
        failures: list[str] = []

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
            code = status.get("code")

            order_id = result.get("orderId")

            link_id = str(requests[index]["orderLinkId"])

            if str(code) == "0" and order_id:
                order_ids.append(str(order_id))
                accepted_link_ids.append(link_id)
            else:
                failures.append(f"order {index + 1}: {code} {status.get('msg')}")

        if failures:
            if accepted_link_ids:
                self._cancel_batch_best_effort(
                    symbol,
                    accepted_link_ids,
                )

            raise TradeExecutionError(
                "Bybit batch partially failed: " + "; ".join(failures)
            )

        return tuple(order_ids)

    def _cancel_batch_best_effort(
        self,
        symbol: str,
        order_link_ids: list[str],
    ) -> None:
        if not order_link_ids:
            return

        try:
            response = self._private_post(
                "/v5/order/cancel-batch",
                {
                    "category": "linear",
                    "request": [
                        {
                            "symbol": symbol,
                            "orderLinkId": (order_link_id),
                        }
                        for order_link_id in order_link_ids
                    ],
                },
            )

            statuses = response.get(
                "retExtInfo",
                {},
            ).get(
                "list",
                [],
            )

            failed = [status for status in statuses if str(status.get("code")) != "0"]

            if failed:
                logger.error(
                    "Rollback cancellation returned failures: %s",
                    failed,
                )
        except Exception:
            logger.exception("Failed to roll back partially accepted Bybit batch")

    def _validate_market_plan(
        self,
        plan: ExecutionPlan,
        market_price: Decimal,
    ) -> None:
        for order in plan.orders:
            take_profit = (
                order.take_profit if order.take_profit is not None else plan.take_profit
            )

            if plan.side is Side.LONG and not (
                plan.stop_loss < market_price < take_profit
            ):
                raise TradeExecutionError(
                    "Market price is outside LONG stop/target geometry"
                )

            if plan.side is Side.SHORT and not (
                take_profit < market_price < plan.stop_loss
            ):
                raise TradeExecutionError(
                    "Market price is outside SHORT stop/target geometry"
                )

        total_quantity = sum(
            (order.quantity for order in plan.orders),
            Decimal("0"),
        )

        current_risk = total_quantity * abs(market_price - plan.stop_loss)

        if current_risk > plan.policy.risk_budget_usdt:
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

        items = response.get("result", {}).get("list", [])

        if not items:
            raise TradeExecutionError(f"No Bybit ticker found for {symbol}")

        return Decimal(items[0]["lastPrice"])

    def _sync_clock(
        self,
        *,
        force: bool = False,
    ) -> None:
        if (
            not force
            and self._clock_synced_at
            and (time.monotonic() - self._clock_synced_at) < CLOCK_SYNC_TTL_SECONDS
        ):
            return

        last_error: Exception | None = None

        for attempt in range(3):
            try:
                t0 = _wall_clock_ms()

                response = self._client.get("/v5/market/time")

                t1 = _wall_clock_ms()

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

                server_ms = self._server_time_ms(data)

                midpoint_ms = (t0 + t1) // 2

                self._clock_offset_ms = server_ms - midpoint_ms

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
            f"Could not synchronize clock with Bybit: {last_error}"
        )

    @staticmethod
    def _server_time_ms(
        data: dict,
    ) -> int:
        if data.get("time") is not None:
            return int(data["time"])

        result = data.get("result") or {}

        if result.get("timeNano") is not None:
            return int(result["timeNano"]) // 1_000_000

        if result.get("timeSecond") is not None:
            return int(result["timeSecond"]) * 1000

        raise TradeExecutionError("Bybit server-time response contained no timestamp")

    def _auth_timestamp(
        self,
    ) -> str:
        self._sync_clock()

        return str(_wall_clock_ms() + self._clock_offset_ms)

    def _private_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        if method == "GET":
            encoded_payload = urlencode(
                [(key, str(value)) for key, value in payload.items()]
            )
        elif method == "POST":
            encoded_payload = json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        else:
            raise ValueError(f"Unsupported private Bybit HTTP method: {method}")

        for attempt in range(2):
            timestamp = self._auth_timestamp()
            signature_payload = (
                timestamp + self._api_key + str(RECV_WINDOW_MS) + encoded_payload
            )
            signature = hmac.new(
                self._api_secret.encode(),
                signature_payload.encode(),
                hashlib.sha256,
            ).hexdigest()

            headers = {
                "X-BAPI-API-KEY": self._api_key,
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
                "X-BAPI-SIGN": signature,
            }

            try:
                if method == "GET":
                    response = self._client.get(
                        f"{path}?{encoded_payload}",
                        headers=headers,
                    )
                else:
                    headers["Content-Type"] = "application/json"
                    response = self._client.post(
                        path,
                        content=encoded_payload,
                        headers=headers,
                    )

                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise TradeExecutionError(f"Bybit HTTP request failed: {exc}") from exc

            data = response.json()
            code = data.get("retCode")

            if str(code) == "0":
                return data

            if str(code) == "10002" and attempt == 0:
                logger.warning(
                    "Bybit rejected request timestamp; re-synchronizing clock"
                )
                self._sync_clock(force=True)
                continue

            raise TradeExecutionError(
                f"Bybit rejected request: {code} {data.get('retMsg')}"
            )

        raise TradeExecutionError("Bybit request failed after clock re-sync")

    def _private_get(
        self,
        path: str,
        params: dict[str, object],
    ) -> dict:
        return self._private_request("GET", path, params)

    def _private_post(
        self,
        path: str,
        body: dict[str, object],
    ) -> dict:
        return self._private_request("POST", path, body)

    def _public_get(
        self,
        path: str,
        params: dict[
            str,
            str,
        ],
    ) -> dict:
        try:
            response = self._client.get(
                path,
                params=params,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise TradeExecutionError(f"Bybit HTTP request failed: {exc}") from exc

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
                f"Bybit rejected request: {data.get('retCode')} {data.get('retMsg')}"
            )

        return data

    @staticmethod
    def _decimal(
        value: object,
    ) -> Decimal:
        raw = str(value if value is not None else "0").strip()

        if not raw:
            return Decimal("0")

        return Decimal(raw)

    @classmethod
    def _optional_decimal(
        cls,
        value: object,
    ) -> Decimal | None:
        parsed = cls._decimal(value)

        return parsed if parsed != 0 else None

    @staticmethod
    def _side_from_bybit(
        side: object,
    ) -> Side:
        if side == "Buy":
            return Side.LONG

        if side == "Sell":
            return Side.SHORT

        raise TradeExecutionError(f"Unexpected Bybit position/order side: {side!r}")

    @staticmethod
    def _fmt(
        value: Decimal,
    ) -> str:
        return format(
            value.normalize(),
            "f",
        )
