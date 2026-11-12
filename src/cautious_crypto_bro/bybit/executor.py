from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from ..cancellation import finish_before_cancellation
from ..domain import (
    AccountOrder,
    AccountStateSummary,
    ClosedPnlRecord,
    EntryPreflightError,
    EntrySlice,
    ExecutionOrderType,
    ExecutionPlan,
    InstrumentContext,
    MarketPrimaryExecutionResult,
    PositionActionExecutionResult,
    PositionActionIntent,
    Side,
    SymbolExposure,
    TradeExecutionError,
)
from ..execution import ExecutionPlanner
from ..execution_history import HISTORY_WINDOW_MS, ExecutionHistoryPage
from . import account_reads, entry_orders, market_reads, position_actions
from .client import DEMO_BASE_URL, BybitClient
from .entry_limits import allocate_entry_slices
from .leverage import (
    RiskTier,
    flat_position_risk_tier,
    instrument_leverage_filter,
    tier_leverage,
)
from .normalize import (
    account_order_from_item,
    format_decimal,
)

logger = logging.getLogger(__name__)


async def _run_sync[Result](function: Callable[..., Result], *args: object) -> Result:
    return await finish_before_cancellation(asyncio.to_thread(function, *args))


class BybitDemoExecutor:
    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        planner: ExecutionPlanner | None = None,
    ) -> None:
        self._client = BybitClient(
            base_url=DEMO_BASE_URL,
            api_key=api_key,
            api_secret=api_secret,
        )
        self._planner = planner if planner is not None else ExecutionPlanner()

    async def _authenticated_uid(self) -> int:
        try:
            response = await _run_sync(
                self._client.private_request, "GET", "/v5/user/query-api", {}
            )
        except TradeExecutionError:
            raise TradeExecutionError("Demo account identity is unavailable") from None
        uid = response.get("result", {}).get("userID")
        if type(uid) is not int or uid <= 0:
            raise TradeExecutionError("Demo account identity is invalid")
        return uid

    @staticmethod
    async def verify_account_isolation(
        accounts: tuple[tuple[int, BybitDemoExecutor], ...],
    ) -> None:
        """Verify the expected UID for each Demo executor before routing."""
        expected = tuple(uid for uid, _ in accounts)
        if len(expected) < 2 or any(
            type(uid) is not int or uid <= 0 for uid in expected
        ):
            raise ValueError("At least two valid Demo account identities are required")
        if len(set(expected)) != len(expected):
            raise ValueError("Expected Demo account identities must be distinct")

        observed = []
        for _, executor in accounts:
            observed.append(await executor._authenticated_uid())
        if len(set(observed)) != len(observed):
            raise TradeExecutionError("Demo accounts are not isolated")
        if tuple(observed) != expected:
            raise TradeExecutionError(
                "Demo account identity does not match configuration"
            )

    async def market_context(
        self,
        symbol: str,
    ) -> InstrumentContext:
        return await _run_sync(
            self._market_context_sync,
            symbol,
        )

    async def execution_history_page(
        self, symbol: str, start_ms: int, end_ms: int, cursor: str = ""
    ) -> ExecutionHistoryPage:
        if start_ms <= 0 or not start_ms <= end_ms <= start_ms + HISTORY_WINDOW_MS:
            raise ValueError("Execution history requires a bounded seven-day window")
        return await _run_sync(
            self._execution_history_page_sync, symbol, start_ms, end_ms, cursor
        )

    def _execution_history_page_sync(
        self, symbol: str, start_ms: int, end_ms: int, cursor: str
    ) -> ExecutionHistoryPage:
        return account_reads.execution_history_page(
            self._client, symbol, start_ms, end_ms, cursor
        )

    async def execution_history(
        self,
        symbol: str,
        since_ms: int,
    ) -> tuple[dict, ...]:
        """Raw private execution rows for a symbol since a millisecond time."""
        return await _run_sync(
            self._execution_history_sync,
            symbol,
            since_ms,
        )

    def _execution_history_sync(
        self,
        symbol: str,
        since_ms: int,
    ) -> tuple[dict, ...]:
        return account_reads.execution_history(self._client, symbol, since_ms)

    async def one_minute_closes(
        self,
        symbol: str,
        *,
        limit: int = 120,
    ) -> list[tuple[int, Decimal]]:
        """Fully-formed 1m candle closes as (start_ms, close), oldest last."""
        return await _run_sync(
            self._one_minute_closes_sync,
            symbol,
            limit,
        )

    def _one_minute_closes_sync(
        self,
        symbol: str,
        limit: int,
    ) -> list[tuple[int, Decimal]]:
        return market_reads.one_minute_closes(self._client, symbol, limit)

    async def account_state(
        self,
    ) -> AccountStateSummary:
        return await _run_sync(self._account_state_sync)

    async def wallet_balance_usdt(self) -> Decimal:
        return await _run_sync(self._wallet_balance_usdt_sync)

    async def closed_pnl_history(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]:
        return await _run_sync(
            self._closed_pnl_history_sync,
            start,
            end,
        )

    async def prepare_entry_plan(self, plan: ExecutionPlan) -> ExecutionPlan:
        return await _run_sync(self._prepare_entry_plan_sync, plan)

    async def prepare_market_primary(self, plan: ExecutionPlan) -> ExecutionPlan:
        context = await self.market_context(plan.symbol)
        refreshed = self._planner.refresh_market_plan(
            plan.model_copy(update={"entry_slices": ()}), context
        )
        return await self.prepare_remaining_entries(refreshed)

    async def prepare_remaining_entries(self, plan: ExecutionPlan) -> ExecutionPlan:
        instrument = await _run_sync(self._instrument_info, plan.symbol)
        return allocate_entry_slices(plan, instrument)

    async def execute(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        return await _run_sync(
            self._execute_sync,
            plan,
        )

    async def execute_market_primary(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult:
        return await _run_sync(
            self._execute_market_primary_sync,
            plan,
        )

    async def execute_remaining_entries(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]:
        return await _run_sync(
            self._execute_remaining_entries_sync,
            plan,
        )

    async def execute_prepared_market_primary(
        self, plan: ExecutionPlan
    ) -> MarketPrimaryExecutionResult:
        return await _run_sync(self._execute_prepared_market_primary_sync, plan)

    async def execute_position_action(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult:
        return await _run_sync(
            self._execute_position_action_sync,
            action,
        )

    async def cancel_pending_entries(
        self,
        symbol: str,
    ) -> int:
        return await _run_sync(
            self._cancel_ccb_entry_orders_sync,
            symbol,
        )

    async def cancel_order(
        self,
        symbol: str,
        order_id: str,
    ) -> None:
        await _run_sync(
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
        await _run_sync(
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
        return await _run_sync(
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
            "stopLoss": format_decimal(stop_loss),
            "slTriggerBy": "LastPrice",
        }

        if trailing_distance is not None:
            if trailing_distance <= 0:
                raise TradeExecutionError("Trailing distance must be positive")

            body["trailingStop"] = format_decimal(trailing_distance)

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
                "qty": format_decimal(quantity),
                "price": format_decimal(price),
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
        return account_reads.wallet_balance_usdt(self._client)

    def _account_state_sync(
        self,
    ) -> AccountStateSummary:
        return account_reads.account_state(self._client)

    def _closed_pnl_history_sync(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]:
        return account_reads.closed_pnl_history(self._client, start, end)

    async def strategy_order(self, symbol: str, link_id: str) -> AccountOrder | None:
        """Read open or completed orders; absence from open orders is not a fill."""
        return await _run_sync(self._strategy_order_sync, symbol, link_id)

    def _strategy_order_sync(self, symbol: str, link_id: str) -> AccountOrder | None:
        return account_reads.strategy_order(self._client, symbol, link_id)

    def _exposure_sync(
        self,
        symbol: str,
    ) -> SymbolExposure:
        return account_reads.symbol_exposure(self._client, symbol)

    def _execute_position_action_sync(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult:
        return position_actions.execute_position_action(
            self._client, action, self._cancel_order_sync
        )

    def _instrument_info(
        self,
        symbol: str,
    ) -> dict:
        return market_reads.instrument_info(self._client, symbol)

    def _cancel_ccb_exit_orders_sync(
        self,
        symbol: str,
    ) -> int:
        return position_actions.cancel_ccb_exit_orders(
            self._client, symbol, self._cancel_order_sync
        )

    def _cancel_ccb_entry_orders_sync(
        self,
        symbol: str,
    ) -> int:
        return position_actions.cancel_ccb_entry_orders(self._client, symbol)

    def _market_context_sync(
        self,
        symbol: str,
    ) -> InstrumentContext:
        return market_reads.build_market_context(
            self._instrument_info(symbol),
            self._last_price(symbol),
        )

    _require_v2 = staticmethod(entry_orders.require_v2)
    _validate_staged_market_plan = staticmethod(
        entry_orders.validate_staged_market_plan
    )
    _validate_primary_identity = staticmethod(entry_orders.validate_primary_identity)
    _filled_primary_result = staticmethod(entry_orders.filled_primary_result)
    _order_params = staticmethod(entry_orders.entry_order_params)
    _parse_batch_result = staticmethod(entry_orders.parse_batch_result)
    _validate_market_plan = staticmethod(entry_orders.validate_market_plan)

    def _wait_for_market_fill_sync(
        self,
        symbol: str,
        order_id: str,
        *,
        allow_empty: bool = False,
        expected_request: dict[str, object] | None = None,
    ) -> MarketPrimaryExecutionResult:
        params: dict[str, object] = {
            "category": "linear",
            "symbol": symbol,
            "orderId": order_id,
        }

        for attempt in range(20):
            record = self._find_order_record(params, order_id)

            if record is not None:
                if expected_request is not None:
                    self._validate_primary_identity(record, expected_request)
                filled = self._filled_primary_result(
                    record, order_id, allow_empty=allow_empty
                )

                if filled is not None:
                    return filled

            if attempt < 19:
                time.sleep(0.25)

        raise TradeExecutionError("Timed out confirming V2 MARKET E1 fill")

    def _find_order_record(
        self,
        params: dict[str, object],
        order_id: str,
    ) -> dict | None:
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
                (item for item in items if str(item.get("orderId") or "") == order_id),
                None,
            )

            if record is not None:
                return record

        return None

    def _prepare_entry_plan_sync(self, plan: ExecutionPlan) -> ExecutionPlan:
        self._require_v2(plan)
        self._sync_clock()
        instrument = self._instrument_info(plan.symbol)
        limits = instrument_leverage_filter(instrument)
        positions = self._client.paginate_private_list(
            "/v5/position/list", {"category": "linear", "symbol": plan.symbol}
        )
        current_id = flat_position_risk_tier(positions, plan.symbol)
        orders = self._client.paginate_private_list(
            "/v5/order/realtime",
            {
                "category": "linear",
                "symbol": plan.symbol,
                "openOnly": 0,
                "limit": 50,
            },
        )
        for item in orders:
            order = account_order_from_item(item)
            if order.symbol != plan.symbol:
                raise EntryPreflightError("Entry-order symbol evidence is invalid")
            if order.remaining_quantity > 0 and not (
                order.reduce_only or order.close_on_trigger or order.is_protective
            ):
                raise EntryPreflightError(
                    "Existing entry orders prevent leverage preparation"
                )

        tiers = self._risk_tiers_sync(plan.symbol)
        mark = self._mark_price(plan.symbol)
        # Whole-net-position ownership requires a flat symbol with no entry
        # orders. Reserve all new legs at their reference price or the current
        # mark, whichever is higher; never resize frozen risk geometry here.
        projected_notional = sum(
            (
                order.quantity * max(mark, order.reference_price)
                for order in plan.orders
            ),
            Decimal("0"),
        )
        selected = tier_leverage(
            plan.leverage, limits, tiers, current_id, projected_notional
        )
        prepared = (
            plan
            if selected == plan.leverage
            else plan.model_copy(update={"leverage": selected})
        )
        prepared = allocate_entry_slices(prepared, instrument)
        if prepared.entry_slices and len(prepared.entry_orders) + len(orders) > 20:
            raise EntryPreflightError(
                "Existing protective orders exhaust partial-stop capacity"
            )
        return prepared

    def _risk_tiers_sync(self, symbol: str) -> tuple[RiskTier, ...]:
        return market_reads.risk_tiers_for_symbol(self._client, symbol)

    def _set_leverage_sync(self, plan: ExecutionPlan) -> None:
        prepared = self._prepare_entry_plan_sync(plan)
        if prepared.leverage != plan.leverage or [
            self._order_params(prepared, i) for i in range(len(prepared.entry_orders))
        ] != [self._order_params(plan, i) for i in range(len(plan.entry_orders))]:
            raise EntryPreflightError(
                "Entry constraints changed; prepare and persist the entry plan again"
            )
        leverage = format_decimal(plan.leverage)
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
        if plan.entry_slices:
            return self._execute_sliced_market_primary_sync(plan)
        try:
            self._validate_staged_market_plan(plan)
            self._sync_clock()
            context = self._market_context_sync(plan.symbol)
            refreshed = self._planner.refresh_market_plan(plan, context)
            self._validate_market_plan(refreshed, context.market_price)
            request = self._order_params(refreshed, 0)
            logger.info(
                "Refreshed V2 MARKET E1 for %s: quote=%s old_qty=%s qty=%s "
                "planned_risk=%s budget=%s",
                plan.intent_id,
                context.market_price,
                plan.orders[0].quantity,
                refreshed.orders[0].quantity,
                refreshed.planned_max_loss_usdt,
                plan.policy.risk_budget_usdt,
            )
            self._set_leverage_sync(refreshed)
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

        filled = self._wait_for_market_fill_sync(
            plan.symbol,
            str(order_id),
        )
        return MarketPrimaryExecutionResult(
            order_id=filled.order_id,
            average_fill_price=filled.average_fill_price,
            filled_quantity=filled.filled_quantity,
            entry_slices=(
                EntrySlice(
                    leg_name="E1",
                    index=0,
                    quantity=refreshed.orders[0].quantity,
                    filled_quantity=filled.filled_quantity,
                ),
            ),
        )

    def _execute_sliced_market_primary_sync(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult:
        self._validate_staged_market_plan(plan)
        self._sync_clock()
        context = self._market_context_sync(plan.symbol)
        refreshed = self._planner.refresh_market_plan(
            plan.model_copy(update={"entry_slices": ()}), context
        )
        # E2/E3 are rebuilt from actual fills later. A favorable quote need
        # not invalidate their prospective geometry; only a required primary
        # downsize would change the already persisted physical requests.
        if refreshed.orders[0].quantity < plan.orders[0].quantity:
            raise EntryPreflightError(
                "Market requires a primary downsize after durable slice preparation"
            )
        self._validate_market_plan(plan, context.market_price)
        self._set_leverage_sync(plan)
        return self._submit_primary_slices_sync(plan)

    def _execute_prepared_market_primary_sync(
        self, plan: ExecutionPlan
    ) -> MarketPrimaryExecutionResult:
        """Submit the refreshed requests already persisted by the coordinator."""
        try:
            self._validate_staged_market_plan(plan)
            if not plan.entry_slices:
                raise EntryPreflightError(
                    "Prepared primary requires durable entry mapping"
                )
            self._validate_market_plan(plan, plan.orders[0].reference_price)
            self._set_leverage_sync(plan)
        except Exception as exc:
            raise EntryPreflightError(str(exc)) from exc
        return self._submit_primary_slices_sync(plan)

    def _submit_primary_slices_sync(
        self, plan: ExecutionPlan
    ) -> MarketPrimaryExecutionResult:
        ids: list[str] = []
        fills = []
        total = Decimal("0")
        cost = Decimal("0")
        for index, child in enumerate(plan.entry_slices):
            if child.leg_name != "E1":
                continue
            request = self._order_params(plan, index)
            response = self._client.private_post(
                "/v5/order/create",
                {"category": "linear", **request},
            )
            order_id = str(response.get("result", {}).get("orderId") or "")
            if (
                not order_id
                or order_id in ids
                or response.get("result", {}).get("orderLinkId")
                != request["orderLinkId"]
            ):
                raise TradeExecutionError("Invalid physical primary acknowledgement")
            ids.append(order_id)
            result = self._wait_for_market_fill_sync(
                plan.symbol,
                order_id,
                allow_empty=True,
                expected_request=request,
            )
            if result.filled_quantity > child.quantity:
                raise TradeExecutionError(
                    "Physical primary fill exceeds submitted quantity"
                )
            total += result.filled_quantity
            cost += result.filled_quantity * result.average_fill_price
            fills.append(
                child.model_copy(update={"filled_quantity": result.filled_quantity})
            )
        if total <= 0:
            raise TradeExecutionError("Physical primary has no confirmed fill")
        return MarketPrimaryExecutionResult(
            order_id=ids[0],
            average_fill_price=cost / total,
            filled_quantity=total,
            order_ids=tuple(ids),
            entry_slices=tuple(fills),
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
            for index, order in enumerate(plan.entry_orders)
            if order.order_type is ExecutionOrderType.LIMIT
        ]

        return self._submit_entry_batch(plan.symbol, requests)

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
                self._order_params(plan, index)
                for index in range(len(plan.entry_orders))
            ]
            self._set_leverage_sync(plan)
        except Exception as exc:
            raise EntryPreflightError(str(exc)) from exc

        return self._submit_entry_batch(plan.symbol, requests)

    def _submit_entry_batch(
        self,
        symbol: str,
        requests: list[dict[str, object]],
    ) -> tuple[str, ...]:
        try:
            ids: list[str] = []
            for offset in range(0, len(requests), 20):
                batch = requests[offset : offset + 20]
                response = self._client.private_post(
                    "/v5/order/create-batch",
                    {"category": "linear", "request": batch},
                )
                ids.extend(self._parse_batch_result(batch, response))
            if len(ids) != len(set(ids)):
                raise TradeExecutionError("Duplicate physical entry acknowledgement")
            return tuple(ids)
        except Exception:
            # A lost or malformed acknowledgement does not prove rejection.
            # Cancel only submitted entry links; never replay their creation.
            self._client.cancel_batch_best_effort(
                symbol, [str(request["orderLinkId"]) for request in requests]
            )
            raise

    def _mark_price(self, symbol: str) -> Decimal:
        return market_reads.mark_price(self._client, symbol)

    def _last_price(
        self,
        symbol: str,
    ) -> Decimal:
        return market_reads.last_price(self._client, symbol)

    def _sync_clock(
        self,
        *,
        force: bool = False,
    ) -> None:
        self._client.sync_clock(force=force)
