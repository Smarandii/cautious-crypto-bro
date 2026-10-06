from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TYPE_CHECKING

from ..domain import Side, TakeProfitSource
from ._helpers import _find_position, _round_down, _round_price
from .fill_detector import FillDetector
from .trailing_stop_manager import TrailingStopManager

if TYPE_CHECKING:
    from ..domain import (
        AccountOrder,
        AccountPosition,
        AccountStateSummary,
        ExecutionPlan,
        InstrumentContext,
        PlannedTakeProfit,
        PositionStrategy,
    )
    from ..ports import AccountGateway, PositionSupervisorStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExitGeometry:
    """Values derived from the plan and live position, shared by every exit leg."""

    position: AccountPosition
    context: InstrumentContext
    remaining_weight: Decimal
    risk_distance: Decimal
    cap: Decimal | None
    target_rs: list[Decimal]


class ExitInstaller:
    def __init__(
        self,
        *,
        executor: AccountGateway,
        trailing_stop_manager: TrailingStopManager,
        fill_detector: FillDetector,
        store: PositionSupervisorStore,
    ) -> None:
        self._executor = executor
        self._trailing_stop_manager = trailing_stop_manager
        self._fill_detector = fill_detector
        self._store = store

    async def install_structure(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        account: AccountStateSummary,
        *,
        enable_trailing: bool,
    ) -> tuple[
        AccountStateSummary,
        Decimal,
        Decimal | None,
    ]:
        context, protected_stop, trailing_distance = await self._resolve_protection(
            state,
            plan,
            position,
            enable_trailing,
        )
        await self._save_installing_snapshot(
            state,
            position,
            protected_stop,
            trailing_distance,
            enable_trailing,
        )
        await self._apply_protection(
            state,
            position,
            protected_stop,
            trailing_distance,
        )
        await self._cancel_stale_exits(state, account)
        await self._confirm_protection_and_handoff(
            state,
            plan,
            protected_stop,
            trailing_distance,
            enable_trailing,
        )
        expected_exits = await self._install_take_profit_exits(
            state,
            plan,
            position,
            account,
            context,
        )
        verified = await self._verify_installation(
            state,
            position,
            protected_stop,
            trailing_distance,
            expected_exits,
        )
        return (verified, protected_stop, trailing_distance)

    async def _resolve_protection(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        enable_trailing: bool,
    ) -> tuple[InstrumentContext, Decimal, Decimal | None]:
        context = await self._executor.market_context(state.symbol)

        trailing_distance = (
            self._trailing_stop_manager.trailing_distance(
                plan,
                position,
                context,
            )
            if enable_trailing
            else None
        )

        protected_stop = (
            self._trailing_stop_manager.protected_stop(
                plan,
                position,
                context,
                previous=(state.protected_stop_loss),
            )
            if enable_trailing
            else (state.protected_stop_loss or plan.stop_loss)
        )

        if position.stop_loss is not None:
            protected_stop = (
                max(protected_stop, position.stop_loss)
                if position.side is Side.LONG
                else min(protected_stop, position.stop_loss)
            )
        if state.trailing_active:
            trailing_distance = state.trailing_distance or trailing_distance
        return context, protected_stop, trailing_distance

    async def _save_installing_snapshot(
        self,
        state: PositionStrategy,
        position: AccountPosition,
        protected_stop: Decimal,
        trailing_distance: Decimal | None,
        enable_trailing: bool,
    ) -> None:
        await self._save(
            state.model_copy(
                update={
                    "installing_exits": True,
                    "protected_stop_loss": protected_stop,
                    "trailing_active": enable_trailing or state.trailing_active,
                    "trailing_distance": trailing_distance,
                    "last_position_qty": position.size,
                    "last_avg_price": position.avg_price,
                }
            )
        )

    async def _apply_protection(
        self,
        state: PositionStrategy,
        position: AccountPosition,
        protected_stop: Decimal,
        trailing_distance: Decimal | None,
    ) -> None:
        if (
            position.stop_loss != protected_stop
            or position.trailing_stop != trailing_distance
        ):
            await self._executor.set_position_protection(
                state.symbol,
                protected_stop,
                trailing_distance=trailing_distance,
            )

    async def _cancel_stale_exits(
        self,
        state: PositionStrategy,
        account: AccountStateSummary,
    ) -> None:
        # The new revision is durable before replacing older owned exits.
        prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-t"
        for order in account.open_orders:
            if (
                order.symbol == state.symbol
                and order.reduce_only
                and order.order_link_id.startswith(prefix)
                and not order.order_link_id.endswith(f"r{state.exit_revision}")
            ):
                await self._executor.cancel_order(state.symbol, order.order_id)

    async def _confirm_protection_and_handoff(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        protected_stop: Decimal,
        trailing_distance: Decimal | None,
        enable_trailing: bool,
    ) -> None:
        # Never cancel partial protection until Bybit confirms the full stop.
        verified = await self._executor.account_state()
        live_position = _find_position(state, verified)
        if live_position is None:
            raise RuntimeError("Position disappeared during protection handoff")
        self._trailing_stop_manager.verify_protection(
            live_position,
            protected_stop,
            trailing_distance,
        )
        await self.handoff_partial_stops(
            state.model_copy(
                update={
                    "protected_stop_loss": protected_stop,
                    "trailing_active": enable_trailing,
                    "trailing_distance": trailing_distance,
                }
            ),
            plan,
            live_position,
            verified,
        )

    async def _install_take_profit_exits(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        account: AccountStateSummary,
        context: InstrumentContext,
    ) -> dict[str, tuple[Decimal, Decimal]]:
        done = (
            state.tp1_done,
            state.tp2_done,
            state.tp3_done,
        )

        remaining_weight = plan.runner_pct + sum(
            (
                target.close_pct
                for index, target in enumerate(plan.take_profit_targets)
                if not done[index]
            ),
            Decimal("0"),
        )

        risk_distance = abs(position.avg_price - plan.stop_loss)
        cap = plan.trader_take_profit or (
            plan.take_profit
            if plan.take_profit_source is TakeProfitSource.TRADER
            else None
        )

        geometry = _ExitGeometry(
            position=position,
            context=context,
            remaining_weight=remaining_weight,
            risk_distance=risk_distance,
            cap=cap,
            target_rs=self._resolve_target_rs(plan, position, risk_distance, cap),
        )

        expected_exits: dict[str, tuple[Decimal, Decimal]] = {}
        if remaining_weight <= 0:
            return expected_exits

        for index, target in enumerate(
            plan.take_profit_targets,
            start=1,
        ):
            if done[index - 1]:
                continue

            leg = self._planned_exit(
                state,
                geometry,
                target,
                index,
                expected_exits,
            )

            if leg is None:
                continue

            link_id, quantity, price = leg
            expected_exits[link_id] = (quantity, price)

            await self._reconcile_or_place_exit(
                state,
                position,
                account,
                link_id,
                quantity,
                price,
            )

        return expected_exits

    def _planned_exit(
        self,
        state: PositionStrategy,
        geometry: _ExitGeometry,
        target: PlannedTakeProfit,
        index: int,
        expected_exits: dict[str, tuple[Decimal, Decimal]],
    ) -> tuple[str, Decimal, Decimal] | None:
        """Return (link_id, quantity, price), or None to leave the leg in the runner."""
        position = geometry.position
        context = geometry.context

        quantity = _round_down(
            (position.size * target.close_pct / geometry.remaining_weight),
            context.qty_step,
        )

        if quantity <= 0 or quantity < context.min_qty:
            logger.warning(
                "%s TP%s rounds below "
                "minimum quantity; leaving "
                "that share in the runner",
                state.symbol,
                index,
            )
            return None

        price = self._compute_exit_price(
            position,
            geometry.target_rs[index - 1],
            geometry.risk_distance,
            geometry.cap,
            context,
        )

        if (
            price <= position.avg_price
            if position.side is Side.LONG
            else price >= position.avg_price
        ):
            raise RuntimeError(
                "Trader TP geometry cannot fit profitable exchange ticks"
            )

        if price in [p for _, p in expected_exits.values()]:
            raise RuntimeError("Trader TP geometry cannot fit distinct exchange ticks")

        if context.min_notional and (quantity * price < context.min_notional):
            logger.warning(
                "%s TP%s rounds below "
                "minimum notional; leaving "
                "that share in the runner",
                state.symbol,
                index,
            )
            return None

        return (
            self._fill_detector.exit_link_id(state, index),
            quantity,
            price,
        )

    async def _reconcile_or_place_exit(
        self,
        state: PositionStrategy,
        position: AccountPosition,
        account: AccountStateSummary,
        link_id: str,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        existing = await self._find_existing_exit(state, link_id, account)

        if existing:
            if len(existing) != 1 or not self._matching_exit(
                existing[0], position.side, quantity, price
            ):
                raise RuntimeError(
                    f"Existing exit {link_id} conflicts with the planned policy"
                )
            return

        await self._executor.place_reduce_only_exit(
            symbol=state.symbol,
            position_side=position.side,
            quantity=quantity,
            price=price,
            order_link_id=link_id,
        )

    @staticmethod
    def _resolve_target_rs(
        plan: ExecutionPlan,
        position: AccountPosition,
        risk_distance: Decimal,
        cap: Decimal | None,
    ) -> list[Decimal]:
        target_rs = [target.r_multiple for target in plan.take_profit_targets]
        if cap is None:
            return target_rs
        cap_r = (
            (cap - position.avg_price)
            if position.side is Side.LONG
            else (position.avg_price - cap)
        ) / risk_distance
        if cap_r <= 0:
            raise RuntimeError("Trader TP cap is no longer beyond the live entry")
        if cap_r < target_rs[-1]:
            if cap_r > target_rs[0]:
                target_rs = [target_rs[0], (target_rs[0] + cap_r) / 2, cap_r]
            else:
                target_rs = [r * cap_r / target_rs[-1] for r in target_rs]
        return target_rs

    @staticmethod
    def _compute_exit_price(
        position: AccountPosition,
        target_r: Decimal,
        risk_distance: Decimal,
        cap: Decimal | None,
        context: InstrumentContext,
    ) -> Decimal:
        if position.side is Side.LONG:
            raw_price = position.avg_price + (target_r * risk_distance)
        else:
            raw_price = position.avg_price - (target_r * risk_distance)

        price = _round_price(
            raw_price,
            context.tick_size,
        )

        if cap is not None:
            rounding = ROUND_FLOOR if position.side is Side.LONG else ROUND_CEILING
            cap_price = (cap / context.tick_size).to_integral_value(
                rounding=rounding
            ) * context.tick_size
            price = (
                min(price, cap_price)
                if position.side is Side.LONG
                else max(price, cap_price)
            )
        return price

    async def _find_existing_exit(
        self,
        state: PositionStrategy,
        link_id: str,
        account: AccountStateSummary,
    ) -> list[AccountOrder]:
        existing = [
            order
            for order in account.open_orders
            if order.symbol == state.symbol and order.order_link_id == link_id
        ]
        if not existing:
            historical = await self._executor.strategy_order(state.symbol, link_id)
            if historical is not None:
                existing = [historical]
        return existing

    async def _verify_installation(
        self,
        state: PositionStrategy,
        position: AccountPosition,
        protected_stop: Decimal,
        trailing_distance: Decimal | None,
        expected_exits: dict[str, tuple[Decimal, Decimal]],
    ) -> AccountStateSummary:
        verified = await self._executor.account_state()
        verified_position = _find_position(state, verified)
        if verified_position is None:
            raise RuntimeError("Position disappeared while installing exits")
        self._trailing_stop_manager.verify_protection(
            verified_position,
            protected_stop,
            trailing_distance,
        )
        for link_id, (quantity, price) in expected_exits.items():
            matches = await self._find_existing_exit(state, link_id, verified)
            if len(matches) != 1 or not self._matching_exit(
                matches[0], position.side, quantity, price
            ):
                raise RuntimeError(
                    f"Bybit did not confirm expected Strategy V2 exit {link_id}"
                )
        return verified

    @staticmethod
    def _matching_exit(
        order: AccountOrder,
        position_side: Side,
        quantity: Decimal,
        price: Decimal,
    ) -> bool:
        expected_side = Side.SHORT if position_side is Side.LONG else Side.LONG
        return (
            order.side is expected_side
            and order.reduce_only
            and order.order_type == "Limit"
            and order.quantity == quantity
            and order.status in {"New", "PartiallyFilled", "Filled"}
            and order.price == price
        )

    def partial_stops(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> tuple[str, ...]:
        """Select attached stops by their verified parent entry link."""
        expected_side = Side.SHORT if state.side is Side.LONG else Side.LONG
        expected_parents = {
            self._fill_detector.entry_link_id(state, order.name)
            for order in plan.orders
        }
        return tuple(
            order.order_id
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and order.side is expected_side
                and order.stop_order_type == "PartialStopLoss"
                and order.trigger_price == plan.stop_loss
                and order.parent_order_link_id in expected_parents
                and order.order_id
                and order.remaining_quantity > 0
            )
        )

    async def handoff_partial_stops(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        partial_ids = self.partial_stops(state, plan, account)
        if not partial_ids:
            return account

        expected_stop = state.protected_stop_loss or plan.stop_loss
        live_position = position
        # Refuse to cancel any order if protection changed unexpectedly.
        if position.stop_loss != expected_stop:
            if position.stop_loss is not None:
                raise RuntimeError(
                    f"Cannot hand off {state.symbol}: full stop "
                    f"{position.stop_loss} differs from expected {expected_stop}"
                )
            await self._executor.set_position_protection(
                state.symbol,
                expected_stop,
                trailing_distance=(
                    state.trailing_distance if state.trailing_active else None
                ),
            )
            account = await self._executor.account_state()
            live_position = _find_position(state, account)
            if live_position is None:
                raise RuntimeError("Position disappeared during protection handoff")

        self._trailing_stop_manager.verify_protection(
            live_position,
            expected_stop,
            state.trailing_distance if state.trailing_active else None,
        )
        # Re-read immediately before cancelling. A stop may have triggered
        # between the original snapshot and the protection confirmation.
        account = await self._executor.account_state()
        live_position = _find_position(state, account)
        if live_position is None:
            return account
        self._trailing_stop_manager.verify_protection(
            live_position,
            expected_stop,
            state.trailing_distance if state.trailing_active else None,
        )
        remaining_ids = set(self.partial_stops(state, plan, account))
        for order_id in partial_ids:
            if order_id in remaining_ids:
                await self._executor.cancel_order(state.symbol, order_id)
        return await self._executor.account_state()

    async def _save(
        self,
        state: PositionStrategy,
    ) -> None:
        from datetime import UTC, datetime

        await self._store.save_position_strategy(
            state.model_copy(
                update={
                    "updated_at": (datetime.now(UTC)),
                }
            )
        )
