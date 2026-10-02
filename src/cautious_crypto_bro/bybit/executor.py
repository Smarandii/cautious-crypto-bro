from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import ROUND_DOWN, Decimal

from ..domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ClosedPnlRecord,
    EntryPreflightError,
    ExecutionOrderType,
    ExecutionPlan,
    InstrumentContext,
    MarketPrimaryExecutionResult,
    OpenOrderExposure,
    PositionActionExecutionResult,
    PositionActionIntent,
    PositionActionPreflightError,
    PositionActionType,
    PositionExposure,
    Side,
    SymbolExposure,
    TradeExecutionError,
)
from .client import DEMO_BASE_URL, BybitClient

logger = logging.getLogger(__name__)


class BybitDemoExecutor:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._client = BybitClient(
            base_url=DEMO_BASE_URL,
            api_key=api_key,
            api_secret=api_secret,
        )

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

    async def execute_market_primary(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult:
        return await asyncio.to_thread(
            self._execute_market_primary_sync,
            plan,
        )

    async def execute_remaining_entries(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        return await asyncio.to_thread(
            self._execute_remaining_entries_sync,
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

    async def cancel_pending_entries(
        self,
        symbol: str,
    ) -> int:
        return await asyncio.to_thread(
            self._cancel_ccb_entry_orders_sync,
            symbol,
        )

    async def cancel_strategy_exits(
        self,
        symbol: str,
    ) -> int:
        return await asyncio.to_thread(
            self._cancel_ccb_exit_orders_sync,
            symbol,
        )

    async def cancel_order(
        self,
        symbol: str,
        order_id: str,
    ) -> None:
        await asyncio.to_thread(
            self._cancel_order_sync,
            symbol,
            order_id,
        )

    async def set_position_protection(
        self,
        symbol: str,
        stop_loss: Decimal,
        *,
        trailing_distance: Decimal | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._set_position_protection_sync,
            symbol,
            stop_loss,
            trailing_distance,
        )

    async def place_reduce_only_exit(
        self,
        *,
        symbol: str,
        position_side: Side,
        quantity: Decimal,
        price: Decimal,
        order_link_id: str,
    ) -> str:
        return await asyncio.to_thread(
            self._place_reduce_only_exit_sync,
            symbol,
            position_side,
            quantity,
            price,
            order_link_id,
        )

    def close(self) -> None:
        self._client.close()

    def _cancel_order_sync(
        self,
        symbol: str,
        order_id: str,
    ) -> None:
        self._sync_clock()

        response = self._client.private_post(
            "/v5/order/cancel",
            {
                "category": "linear",
                "symbol": symbol,
                "orderId": order_id,
            },
        )

        if not response.get(
            "result",
            {},
        ).get("orderId"):
            raise TradeExecutionError(
                "Bybit accepted no order ID while cancelling order"
            )

    def _set_position_protection_sync(
        self,
        symbol: str,
        stop_loss: Decimal,
        trailing_distance: Decimal | None,
    ) -> None:
        self._sync_clock()

        body: dict[str, object] = {
            "category": "linear",
            "symbol": symbol,
            "tpslMode": "Full",
            "positionIdx": 0,
            "stopLoss": self._fmt(stop_loss),
            "slTriggerBy": "LastPrice",
        }

        if trailing_distance is not None:
            if trailing_distance <= 0:
                raise TradeExecutionError("Trailing distance must be positive")

            body["trailingStop"] = self._fmt(trailing_distance)

        self._client.private_post(
            "/v5/position/trading-stop",
            body,
        )

    def _place_reduce_only_exit_sync(
        self,
        symbol: str,
        position_side: Side,
        quantity: Decimal,
        price: Decimal,
        order_link_id: str,
    ) -> str:
        if quantity <= 0 or price <= 0:
            raise TradeExecutionError("Exit quantity and price must be positive")

        self._sync_clock()

        side = "Sell" if position_side is Side.LONG else "Buy"

        response = self._client.private_post(
            "/v5/order/create",
            {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": self._fmt(quantity),
                "price": self._fmt(price),
                "timeInForce": "GTC",
                "positionIdx": 0,
                "reduceOnly": True,
                "orderLinkId": order_link_id,
            },
        )

        order_id = response.get(
            "result",
            {},
        ).get("orderId")

        if not order_id:
            raise TradeExecutionError("Bybit returned success without exit orderId")

        return str(order_id)

    def _wallet_balance_usdt_sync(
        self,
    ) -> Decimal:
        response = self._client.private_get(
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
        position_items = self._client.paginate_private_list(
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
                    break_even_price=(
                        self._optional_decimal(item.get("breakEvenPrice"))
                    ),
                    trailing_stop=(self._optional_decimal(item.get("trailingStop"))),
                )
            )

        open_items = self._client.paginate_private_list(
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
            if (
                self._decimal(item.get("leavesQty")) > 0
                or (
                    item.get("stopOrderType") == "PartialStopLoss"
                    and str(item.get("orderStatus")) == "Untriggered"
                    and self._decimal(item.get("qty")) > 0
                )
            )
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

            items = self._client.paginate_private_list(
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
            remaining_quantity=(
                self._decimal(item.get("leavesQty"))
                or (
                    self._decimal(item.get("qty"))
                    if (
                        item.get("stopOrderType") == "PartialStopLoss"
                        and item.get("orderStatus") == "Untriggered"
                    )
                    else Decimal("0")
                )
            ),
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
            parent_order_link_id=str(item.get("parentOrderLinkId") or ""),
            executed_quantity=self._decimal(item.get("cumExecQty")),
        )

    async def strategy_order(self, symbol: str, link_id: str) -> AccountOrder | None:
        """Read open or completed orders; absence from open orders is not a fill."""
        return await asyncio.to_thread(self._strategy_order_sync, symbol, link_id)

    def _strategy_order_sync(self, symbol: str, link_id: str) -> AccountOrder | None:
        for endpoint in ("/v5/order/realtime", "/v5/order/history"):
            response = self._client.private_get(
                endpoint,
                {"category": "linear", "symbol": symbol, "orderLinkId": link_id},
            )
            for item in response.get("result", {}).get("list", []):
                if item.get("orderLinkId") == link_id and item.get("symbol") == symbol:
                    return self._account_order_from_item(item)
        return None

    def _exposure_sync(
        self,
        symbol: str,
    ) -> SymbolExposure:
        self._sync_clock()

        position_response = self._client.private_get(
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
            order_response = self._client.private_get(
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
        if action.action is PositionActionType.CANCEL_ENTRIES:
            raise PositionActionPreflightError(
                "Entry cancellation requires source-scoped coordinator"
            )
        self._sync_clock()

        initial_exposure = self._exposure_sync(action.symbol)

        # Validate before causing any side effects.
        try:
            self._position_for_action(
                action,
                initial_exposure,
            )
        except TradeExecutionError as exc:
            raise PositionActionPreflightError(str(exc)) from exc

        # A lifecycle instruction supersedes stale
        # CCB entry orders for this symbol. Otherwise
        # an old scale-in order could rebuild exposure
        # immediately after a REDUCE/CLOSE.
        cancelled_entries = self._cancel_ccb_entry_orders_sync(action.symbol)

        # Existing V2 reduce-only exits must not race
        # a trader-authorized REDUCE/CLOSE.
        cancelled_exits = self._cancel_ccb_exit_orders_sync(action.symbol)

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

        response = self._client.private_post(
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
            cancelled_exit_orders=(cancelled_exits),
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
        response = self._client.public_get(
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

    def _cancel_ccb_exit_orders_sync(
        self,
        symbol: str,
    ) -> int:
        cancelled: set[str] = set()

        items = self._client.paginate_private_list(
            "/v5/order/realtime",
            {
                "category": "linear",
                "symbol": symbol,
                "openOnly": 0,
                "limit": 50,
            },
        )

        for item in items:
            link_id = str(item.get("orderLinkId") or "")

            if not link_id.startswith("ccb-v2-") or "-t" not in link_id:
                continue

            reduce_only = item.get("reduceOnly")

            if not (reduce_only is True or str(reduce_only).casefold() == "true"):
                continue

            if self._decimal(item.get("leavesQty")) <= 0:
                continue

            order_id = str(item.get("orderId") or "")

            if not order_id:
                continue

            self._cancel_order_sync(
                symbol,
                order_id,
            )

            cancelled.add(order_id)

        return len(cancelled)

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

                response = self._client.private_post(
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

    @staticmethod
    def _require_v2(plan: ExecutionPlan) -> None:
        if plan.strategy_version != 2:
            raise TradeExecutionError(
                f"Strategy V{plan.strategy_version} is read-only; only V2 executes"
            )

    @classmethod
    def _validate_staged_market_plan(
        cls,
        plan: ExecutionPlan,
    ) -> None:
        cls._require_v2(plan)

        if len(plan.orders) != 3:
            raise TradeExecutionError("Strategy V2 requires three entry legs")

        if plan.orders[0].order_type is not ExecutionOrderType.MARKET:
            raise TradeExecutionError("Staged V2 execution requires MARKET E1")

        if any(
            order.order_type is not ExecutionOrderType.LIMIT
            for order in plan.orders[1:]
        ):
            raise TradeExecutionError("Staged V2 E2/E3 must be LIMIT orders")

    def _wait_for_market_fill_sync(
        self,
        symbol: str,
        order_id: str,
    ) -> MarketPrimaryExecutionResult:
        terminal_without_fill = {
            "Rejected",
            "Cancelled",
            "Deactivated",
            "PartiallyFilledCanceled",
        }

        params: dict[str, object] = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }

        for attempt in range(20):
            record = None

            for path in (
                "/v5/order/realtime",
                "/v5/order/history",
            ):
                response = self._client.private_get(
                    path,
                    params,
                )

                items = response.get(
                    "result",
                    {},
                ).get(
                    "list",
                    [],
                )

                record = next(
                    (
                        item
                        for item in items
                        if str(item.get("orderId") or "") == order_id
                    ),
                    None,
                )

                if record is not None:
                    break

            if record is not None:
                status = str(record.get("orderStatus") or "")

                filled_quantity = self._decimal(record.get("cumExecQty"))

                average_price = self._decimal(record.get("avgPrice"))

                remaining = self._decimal(record.get("leavesQty"))

                if (
                    filled_quantity > 0
                    and average_price > 0
                    and (status == "Filled" or remaining <= 0)
                ):
                    return MarketPrimaryExecutionResult(
                        order_id=order_id,
                        average_fill_price=(average_price),
                        filled_quantity=(filled_quantity),
                    )

                if filled_quantity <= 0 and status in terminal_without_fill:
                    raise TradeExecutionError(
                        f"V2 MARKET E1 ended without a fill: {status}"
                    )

            if attempt < 19:
                time.sleep(0.25)

        raise TradeExecutionError("Timed out confirming V2 MARKET E1 fill")

    def _set_leverage_sync(self, plan: ExecutionPlan) -> None:
        leverage = self._fmt(plan.leverage)
        self._client.private_post(
            "/v5/position/set-leverage",
            {
                "category": "linear",
                "symbol": plan.symbol,
                "buyLeverage": leverage,
                "sellLeverage": leverage,
            },
        )

    def _execute_market_primary_sync(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult:
        try:
            self._validate_staged_market_plan(plan)
            self._sync_clock()
            market_price = self._last_price(plan.symbol)
            self._validate_market_plan(plan, market_price)
            request = self._order_params(plan, 0)
            self._set_leverage_sync(plan)
        except Exception as exc:
            raise EntryPreflightError(str(exc)) from exc

        response = self._client.private_post(
            "/v5/order/create",
            {
                "category": "linear",
                **request,
            },
        )

        order_id = response.get(
            "result",
            {},
        ).get("orderId")

        if not order_id:
            raise TradeExecutionError("Bybit returned success without E1 orderId")

        return self._wait_for_market_fill_sync(
            plan.symbol,
            str(order_id),
        )

    def _execute_remaining_entries_sync(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        self._validate_staged_market_plan(plan)

        self._sync_clock()

        requests = [
            self._order_params(
                plan,
                index,
            )
            for index in (
                1,
                2,
            )
        ]

        response = self._client.private_post(
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

    def _execute_sync(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        try:
            self._require_v2(plan)
            if any(
                order.order_type is ExecutionOrderType.MARKET for order in plan.orders
            ):
                raise TradeExecutionError(
                    "Strategy V2 MARKET plans require staged E1 execution"
                )
            self._sync_clock()
            market_price = self._last_price(plan.symbol)
            if not market_price.is_finite() or market_price <= 0:
                raise TradeExecutionError(
                    "Bybit ticker price must be positive and finite"
                )
            primary = plan.orders[0].reference_price
            if plan.side is Side.LONG and market_price < primary:
                raise TradeExecutionError(
                    "Market is already below the primary LONG entry"
                )
            if plan.side is Side.SHORT and market_price > primary:
                raise TradeExecutionError(
                    "Market is already above the primary SHORT entry"
                )
            requests = [
                self._order_params(plan, index) for index in range(len(plan.orders))
            ]
            self._set_leverage_sync(plan)
        except Exception as exc:
            raise EntryPreflightError(str(exc)) from exc

        response = self._client.private_post(
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
        self._require_v2(plan)

        order = plan.orders[index]

        params: dict[str, object] = {
            "symbol": plan.symbol,
            "side": ("Buy" if plan.side is Side.LONG else "Sell"),
            "orderType": (
                "Market" if order.order_type is ExecutionOrderType.MARKET else "Limit"
            ),
            "qty": self._fmt(order.quantity),
            "stopLoss": self._fmt(plan.stop_loss),
            "tpslMode": "Partial",
            "slOrderType": "Market",
            "timeInForce": (
                "IOC" if order.order_type is ExecutionOrderType.MARKET else "GTC"
            ),
            "positionIdx": 0,
            "reduceOnly": False,
            "orderLinkId": (f"ccb-v2-{plan.intent_id.hex[:20]}-{order.name.lower()}"),
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
            self._client.cancel_batch_best_effort(
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
                self._client.cancel_batch_best_effort(
                    symbol,
                    accepted_link_ids,
                )

            raise TradeExecutionError(
                "Bybit batch partially failed: " + "; ".join(failures)
            )

        return tuple(order_ids)

    def _validate_market_plan(
        self,
        plan: ExecutionPlan,
        market_price: Decimal,
    ) -> None:
        self._require_v2(plan)

        current_risk = Decimal("0")

        for order in plan.orders:
            if order.order_type is ExecutionOrderType.MARKET:
                entry_price = market_price
            else:
                entry_price = order.reference_price

            if plan.side is Side.LONG and not (plan.stop_loss < entry_price):
                raise TradeExecutionError("Market moved outside LONG V2 stop geometry")

            if plan.side is Side.SHORT and not (entry_price < plan.stop_loss):
                raise TradeExecutionError("Market moved outside SHORT V2 stop geometry")

            current_risk += order.quantity * abs(entry_price - plan.stop_loss)

        if current_risk > plan.policy.risk_budget_usdt:
            raise TradeExecutionError(
                "Market moved enough that Strategy V2 execution "
                "would exceed the risk budget"
            )

    def _last_price(
        self,
        symbol: str,
    ) -> Decimal:
        response = self._client.public_get(
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
        self._client.sync_clock(force=force)

    def _private_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        return self._client.private_request(
            method,
            path,
            payload,
        )

    def _private_get(
        self,
        path: str,
        params: dict[str, object],
    ) -> dict:
        return self._client.private_get(
            path,
            params,
        )

    def _private_post(
        self,
        path: str,
        body: dict[str, object],
    ) -> dict:
        return self._client.private_post(
            path,
            body,
        )

    def _public_get(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict:
        return self._client.public_get(
            path,
            params,
        )

    def _paginate_private_list(
        self,
        path: str,
        params: dict[str, object],
    ) -> list[dict]:
        return self._client.paginate_private_list(
            path,
            params,
        )

    def _cancel_batch_best_effort(
        self,
        symbol: str,
        order_link_ids: list[str],
    ) -> None:
        self._client.cancel_batch_best_effort(
            symbol,
            order_link_ids,
        )

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
