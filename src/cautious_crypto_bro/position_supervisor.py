from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
)
from uuid import UUID

from .bybit import (
    AccountPosition,
    AccountStateSummary,
    BybitDemoExecutor,
)
from .domain import (
    ExecutionPlan,
    PositionStrategy,
    Side,
    StrategyStatus,
)
from .execution import InstrumentContext
from .storage import IntentStore

logger = logging.getLogger(__name__)


class PositionSupervisor:
    def __init__(
        self,
        *,
        store: IntentStore,
        executor: BybitDemoExecutor,
        mutation_lock: asyncio.Lock,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("Supervisor poll interval must be positive")

        self._store = store
        self._executor = executor
        self._mutation_lock = mutation_lock
        self._poll_interval_seconds = poll_interval_seconds
        self._reported_uncertain: set[UUID] = set()

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile_once()

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception("Position supervisor reconciliation failed")

            await asyncio.sleep(self._poll_interval_seconds)

    async def reconcile_once(self) -> None:
        records = await self._store.get_active_position_strategies()

        if not records:
            self._reported_uncertain.clear()
            return

        uncertain_ids = {
            state.strategy_id
            for state, _ in records
            if state.status is StrategyStatus.UNCERTAIN
        }
        self._reported_uncertain.intersection_update(uncertain_ids)

        async with self._mutation_lock:
            account: AccountStateSummary | None = None

            by_symbol: dict[
                str,
                list[
                    tuple[
                        PositionStrategy,
                        ExecutionPlan,
                    ]
                ],
            ] = {}

            for state, plan in records:
                by_symbol.setdefault(
                    state.symbol,
                    [],
                ).append(
                    (
                        state,
                        plan,
                    )
                )

            for symbol, strategies in by_symbol.items():
                if len(strategies) != 1:
                    logger.error(
                        "Multiple active V2 strategies map to %s; pausing automation",
                        symbol,
                    )

                    for state, _ in strategies:
                        await self._store.set_position_strategy_status(
                            state.strategy_id,
                            StrategyStatus.MANUAL_OVERRIDE,
                        )

                    continue

                state, plan = strategies[0]

                if state.status is StrategyStatus.UNCERTAIN:
                    if state.strategy_id not in self._reported_uncertain:
                        logger.warning(
                            "Strategy %s %s is UNCERTAIN; leaving exchange state untouched",
                            state.strategy_id,
                            state.symbol,
                        )
                        self._reported_uncertain.add(state.strategy_id)
                    continue

                if account is None:
                    account = await self._executor.account_state()

                account = await self._reconcile(
                    state,
                    plan,
                    account,
                )

    async def _reconcile(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        position = self._position(
            state,
            account,
        )

        pending_entries = self._pending_entries(
            state,
            account,
        )

        if state.status is StrategyStatus.CLOSING:
            if position is None:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )

                return account

            await self._save(
                state.model_copy(
                    update={
                        "last_position_qty": (position.size),
                        "last_avg_price": (position.avg_price),
                    }
                )
            )

            return account

        if position is None:
            if state.last_position_qty is not None and not pending_entries:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )

            return account

        if position.side is not state.side:
            await self._save(
                state.model_copy(
                    update={
                        "status": (StrategyStatus.MANUAL_OVERRIDE),
                    }
                )
            )

            return account

        risk_distance = abs(position.avg_price - plan.stop_loss)

        if risk_distance <= 0:
            await self._save(
                state.model_copy(
                    update={
                        "status": (StrategyStatus.MANUAL_OVERRIDE),
                    }
                )
            )

            return account

        override_reason = self._manual_override_reason(
            state,
            plan,
            account,
            position,
        )

        if override_reason is not None:
            logger.warning(
                "Strategy %s %s -> MANUAL_OVERRIDE: %s",
                state.strategy_id,
                state.symbol,
                override_reason,
            )

            await self._save(
                state.model_copy(
                    update={
                        "status": (StrategyStatus.MANUAL_OVERRIDE),
                    }
                )
            )

            return account

        # Complete interrupted protection handoffs even when the exit
        # revision was persisted on a previous reconciliation.
        if self._partial_stops(state, plan, account):
            account = await self._handoff_partial_stops(
                state,
                plan,
                position,
                account,
            )
            position = self._position(state, account)
            if position is None:
                return account

        live_r = self._live_r(
            position,
            plan.stop_loss,
        )

        strategy = plan.policy.strategy_v2

        if not state.entry_frozen:
            previous_position_qty = state.last_position_qty

            state = self._detect_fixed_exit_fills(
                state,
                account,
                position,
            )

            fixed_exit_filled = any(
                (
                    state.tp1_done,
                    state.tp2_done,
                    state.tp3_done,
                )
            )

            position_grew = (
                previous_position_qty is not None
                and position.size > previous_position_qty
            )

            structure_missing = state.exit_revision == 0

            base_quantity = max(
                (state.base_position_qty or Decimal("0")),
                position.size,
            )

            state = state.model_copy(
                update={
                    "status": (StrategyStatus.OPEN_RISK),
                    "base_position_qty": (base_quantity),
                    "last_position_qty": (position.size),
                    "last_avg_price": (position.avg_price),
                }
            )

            should_freeze = (
                fixed_exit_filled
                or (live_r >= strategy.trailing_activation_r)
                or state.rebalance_needed
            )

            if not should_freeze:
                if structure_missing or position_grew:
                    if not structure_missing:
                        await self._executor.cancel_strategy_exits(state.symbol)

                    state = state.model_copy(
                        update={
                            "exit_revision": (state.exit_revision + 1),
                        }
                    )

                    (
                        account,
                        installed_stop,
                        installed_trail,
                    ) = await self._install_structure(
                        state,
                        plan,
                        position,
                        account,
                        enable_trailing=False,
                    )

                    state = state.model_copy(
                        update={
                            "protected_stop_loss": (installed_stop),
                            "trailing_distance": (installed_trail),
                        }
                    )

                await self._save(state)

                return account

            await self._executor.cancel_pending_entries(state.symbol)

            # Re-read after freezing accumulation.
            # An entry may have filled concurrently
            # while cancellation was in flight.
            account = await self._executor.account_state()

            position = self._position(
                state,
                account,
            )

            if position is None:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )

                return account

            live_r = self._live_r(
                position,
                plan.stop_loss,
            )

            state = state.model_copy(
                update={
                    "entry_frozen": True,
                    "base_position_qty": (position.size),
                    "last_position_qty": (position.size),
                    "last_avg_price": (position.avg_price),
                    "exit_revision": (state.exit_revision + 1),
                    "rebalance_needed": False,
                }
            )

            await self._executor.cancel_strategy_exits(state.symbol)

            (
                account,
                installed_stop,
                installed_trail,
            ) = await self._install_structure(
                state,
                plan,
                position,
                account,
                enable_trailing=(live_r >= strategy.trailing_activation_r),
            )

            state = state.model_copy(
                update={
                    "status": (
                        StrategyStatus.PROFIT_PROTECTED
                        if (live_r >= strategy.trailing_activation_r)
                        else StrategyStatus.OPEN_RISK
                    ),
                    "trailing_active": (live_r >= strategy.trailing_activation_r),
                    "protected_stop_loss": (installed_stop),
                    "trailing_distance": (installed_trail),
                }
            )

            await self._save(state)

            return account

        if state.rebalance_needed:
            await self._executor.cancel_strategy_exits(state.symbol)

            account = await self._executor.account_state()

            position = self._position(
                state,
                account,
            )

            if position is None:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )

                return account

            live_r = self._live_r(
                position,
                plan.stop_loss,
            )

            state = state.model_copy(
                update={
                    "base_position_qty": (position.size),
                    "last_position_qty": (position.size),
                    "last_avg_price": (position.avg_price),
                    "exit_revision": (state.exit_revision + 1),
                    "rebalance_needed": False,
                }
            )

            trailing = state.trailing_active or (
                live_r >= strategy.trailing_activation_r
            )

            (
                account,
                installed_stop,
                installed_trail,
            ) = await self._install_structure(
                state,
                plan,
                position,
                account,
                enable_trailing=trailing,
            )

            state = state.model_copy(
                update={
                    "trailing_active": trailing,
                    "protected_stop_loss": (installed_stop),
                    "trailing_distance": (installed_trail),
                    "status": (
                        StrategyStatus.PROFIT_PROTECTED
                        if trailing
                        else StrategyStatus.OPEN_RISK
                    ),
                }
            )

            await self._save(state)

            return account

        state = self._detect_fixed_exit_fills(
            state,
            account,
            position,
        )

        if not state.trailing_active and (live_r >= strategy.trailing_activation_r):
            await self._executor.cancel_pending_entries(state.symbol)

            context = await self._executor.market_context(state.symbol)

            trailing_distance = self._trailing_distance(
                plan,
                position,
                context,
            )

            protected_stop = self._protected_stop(
                plan,
                position,
                context,
                previous=(state.protected_stop_loss),
            )

            await self._executor.set_position_protection(
                state.symbol,
                protected_stop,
                trailing_distance=(trailing_distance),
            )

            account = await self._executor.account_state()

            verified_position = self._position(
                state,
                account,
            )

            if verified_position is None:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )

                return account

            self._verify_protection(
                verified_position,
                protected_stop,
                trailing_distance,
            )

            position = verified_position

            state = state.model_copy(
                update={
                    "trailing_active": True,
                    "protected_stop_loss": (protected_stop),
                    "trailing_distance": (trailing_distance),
                    "status": (StrategyStatus.PROFIT_PROTECTED),
                }
            )

        state = state.model_copy(
            update={
                "last_position_qty": (position.size),
                "last_avg_price": (position.avg_price),
            }
        )

        await self._save(state)

        return account

    async def _install_structure(
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
        context = await self._executor.market_context(state.symbol)

        trailing_distance = (
            self._trailing_distance(
                plan,
                position,
                context,
            )
            if enable_trailing
            else None
        )

        protected_stop = (
            self._protected_stop(
                plan,
                position,
                context,
                previous=(state.protected_stop_loss),
            )
            if enable_trailing
            else (state.protected_stop_loss or plan.stop_loss)
        )

        if (
            position.stop_loss != protected_stop
            or position.trailing_stop != trailing_distance
        ):
            await self._executor.set_position_protection(
                state.symbol,
                protected_stop,
                trailing_distance=trailing_distance,
            )

        # Never cancel partial protection until Bybit confirms the full stop.
        verified = await self._executor.account_state()
        live_position = self._position(state, verified)
        if live_position is None:
            raise RuntimeError("Position disappeared during protection handoff")
        self._verify_protection(
            live_position,
            protected_stop,
            trailing_distance,
        )
        await self._handoff_partial_stops(
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

        if remaining_weight > 0:
            for index, target in enumerate(
                plan.take_profit_targets,
                start=1,
            ):
                if done[index - 1]:
                    continue

                quantity = self._round_down(
                    (position.size * target.close_pct / remaining_weight),
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
                    continue

                if position.side is Side.LONG:
                    raw_price = position.avg_price + (target.r_multiple * risk_distance)
                else:
                    raw_price = position.avg_price - (target.r_multiple * risk_distance)

                price = self._round_price(
                    raw_price,
                    context.tick_size,
                )

                if context.min_notional and (quantity * price < context.min_notional):
                    logger.warning(
                        "%s TP%s rounds below "
                        "minimum notional; leaving "
                        "that share in the runner",
                        state.symbol,
                        index,
                    )
                    continue

                await self._executor.place_reduce_only_exit(
                    symbol=state.symbol,
                    position_side=position.side,
                    quantity=quantity,
                    price=price,
                    order_link_id=(
                        self._exit_link_id(
                            state,
                            index,
                        )
                    ),
                )

        verified = await self._executor.account_state()
        verified_position = self._position(state, verified)
        if verified_position is None:
            raise RuntimeError("Position disappeared while installing exits")
        self._verify_protection(
            verified_position,
            protected_stop,
            trailing_distance,
        )

        return (verified, protected_stop, trailing_distance)

    @staticmethod
    def _partial_stops(
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> tuple[str, ...]:
        """Identify only matching legacy entry stops, not manual protection."""
        expected_side = Side.SHORT if state.side is Side.LONG else Side.LONG
        return tuple(
            order.order_id
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and order.side is expected_side
                and order.stop_order_type == "PartialStopLoss"
                and order.trigger_price == plan.stop_loss
                and order.order_id
                and order.remaining_quantity > 0
            )
        )

    async def _handoff_partial_stops(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        partial_ids = self._partial_stops(state, plan, account)
        if not partial_ids:
            return account

        expected_stop = state.protected_stop_loss or plan.stop_loss
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
            live_position = self._position(state, account)
            if live_position is None:
                raise RuntimeError("Position disappeared during protection handoff")

        self._verify_protection(
            live_position,
            expected_stop,
            state.trailing_distance if state.trailing_active else None,
        )
        # Re-read immediately before cancelling. A stop may have triggered
        # between the original snapshot and the protection confirmation.
        account = await self._executor.account_state()
        live_position = self._position(state, account)
        if live_position is None:
            return account
        self._verify_protection(
            live_position,
            expected_stop,
            state.trailing_distance if state.trailing_active else None,
        )
        remaining_ids = set(self._partial_stops(state, plan, account))
        for order_id in partial_ids:
            if order_id in remaining_ids:
                await self._executor.cancel_order(state.symbol, order_id)
        return await self._executor.account_state()

    def _detect_fixed_exit_fills(
        self,
        state: PositionStrategy,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> PositionStrategy:
        if (
            state.exit_revision <= 0
            or state.last_position_qty is None
            or position.size >= state.last_position_qty
        ):
            return state

        open_links = {
            order.order_link_id
            for order in account.open_orders
            if (order.symbol == state.symbol and order.reduce_only)
        }

        done = [
            state.tp1_done,
            state.tp2_done,
            state.tp3_done,
        ]

        changed = False

        for index in range(1, 4):
            if done[index - 1]:
                continue

            if (
                self._exit_link_id(
                    state,
                    index,
                )
                not in open_links
            ):
                done[index - 1] = True
                changed = True

        if not changed:
            return state

        return state.model_copy(
            update={
                "tp1_done": done[0],
                "tp2_done": done[1],
                "tp3_done": done[2],
            }
        )

    @staticmethod
    def _entry_link_id(
        state: PositionStrategy,
        name: str,
    ) -> str:
        return f"ccb-v2-{state.strategy_id.hex[:20]}-{name.lower()}"

    @classmethod
    def _manual_override_reason(
        cls,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        if not state.entry_frozen:
            expected = {
                cls._entry_link_id(
                    state,
                    planned.name,
                ): planned
                for planned in plan.orders
            }

            prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-e"

            for order in account.open_orders:
                if (
                    order.symbol != state.symbol
                    or order.kind != "ENTRY"
                    or not order.order_link_id.startswith(prefix)
                ):
                    continue

                planned = expected.get(order.order_link_id)

                if planned is None:
                    return f"unexpected owned V2 entry {order.order_link_id}"

                if order.side is not state.side:
                    return f"owned entry side changed for {order.order_link_id}"

                if order.quantity != planned.quantity:
                    return f"owned entry quantity changed for {order.order_link_id}"

                if planned.price is not None and order.price != planned.price:
                    return (
                        "owned entry price changed "
                        f"for {order.order_link_id}: "
                        f"expected={planned.price}, "
                        f"live={order.price}"
                    )

            return None

        if (
            not state.rebalance_needed
            and state.last_position_qty is not None
            and position.size > state.last_position_qty
        ):
            return "position size increased after entry freeze"

        if (
            not state.rebalance_needed
            and state.last_avg_price is not None
            and position.avg_price != state.last_avg_price
        ):
            return "average entry changed after entry freeze"

        if (
            state.protected_stop_loss is not None
            and position.stop_loss != state.protected_stop_loss
            and not (
                position.stop_loss is None and cls._partial_stops(state, plan, account)
            )
        ):
            return (
                "position stop changed outside "
                "Strategy V2: "
                f"expected="
                f"{state.protected_stop_loss}, "
                f"live={position.stop_loss}"
            )

        if state.trailing_active:
            if state.trailing_distance is None:
                return "persisted trailing state is inconsistent"

            if position.trailing_stop != state.trailing_distance:
                return (
                    "position trailing stop changed "
                    "outside Strategy V2: "
                    f"expected="
                    f"{state.trailing_distance}, "
                    f"live={position.trailing_stop}"
                )

        return None

    @staticmethod
    def _verify_protection(
        position: AccountPosition,
        stop_loss: Decimal,
        trailing_distance: Decimal | None,
    ) -> None:
        if position.stop_loss != stop_loss:
            raise RuntimeError(
                "Bybit did not confirm expected "
                "position stop: "
                f"expected={stop_loss}, "
                f"live={position.stop_loss}"
            )

        if (
            trailing_distance is not None
            and position.trailing_stop != trailing_distance
        ):
            raise RuntimeError(
                "Bybit did not confirm expected "
                "trailing distance: "
                f"expected={trailing_distance}, "
                f"live={position.trailing_stop}"
            )

    @staticmethod
    def _protected_stop(
        plan: ExecutionPlan,
        position: AccountPosition,
        context: InstrumentContext,
        *,
        previous: Decimal | None,
    ) -> Decimal:
        risk_distance = abs(position.avg_price - plan.stop_loss)

        anchor = position.break_even_price or position.avg_price

        buffer = risk_distance * plan.policy.strategy_v2.minimum_locked_profit_r

        if position.side is Side.LONG:
            raw = max(
                plan.stop_loss,
                anchor + buffer,
            )

            if previous is not None:
                raw = max(
                    raw,
                    previous,
                )

            result = (raw / context.tick_size).to_integral_value(
                rounding=ROUND_CEILING
            ) * context.tick_size

            if result >= position.mark_price:
                raise RuntimeError("Protected LONG stop would not be below live price")

        else:
            raw = min(
                plan.stop_loss,
                anchor - buffer,
            )

            if previous is not None:
                raw = min(
                    raw,
                    previous,
                )

            result = (raw / context.tick_size).to_integral_value(
                rounding=ROUND_FLOOR
            ) * context.tick_size

            if result <= 0 or result <= position.mark_price:
                raise RuntimeError("Protected SHORT stop would not be above live price")

        return result

    @staticmethod
    def _position(
        state: PositionStrategy,
        account: AccountStateSummary,
    ) -> AccountPosition | None:
        positions = tuple(
            position
            for position in account.positions
            if position.symbol == state.symbol
        )

        if not positions:
            return None

        if len(positions) != 1:
            raise RuntimeError(f"Expected one live position for {state.symbol}")

        return positions[0]

    @staticmethod
    def _pending_entries(
        state: PositionStrategy,
        account: AccountStateSummary,
    ) -> tuple[str, ...]:
        prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-e"

        return tuple(
            order.order_link_id
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and not order.reduce_only
                and order.order_link_id.startswith(prefix)
            )
        )

    @staticmethod
    def _live_r(
        position: AccountPosition,
        stop_loss: Decimal,
    ) -> Decimal:
        distance = abs(position.avg_price - stop_loss)

        if position.side is Side.LONG:
            favorable = position.mark_price - position.avg_price
        else:
            favorable = position.avg_price - position.mark_price

        return favorable / distance

    @staticmethod
    def _exit_link_id(
        state: PositionStrategy,
        index: int,
    ) -> str:
        return f"ccb-v2-{state.strategy_id.hex[:20]}-t{index}r{state.exit_revision}"

    @staticmethod
    def _trailing_distance(
        plan: ExecutionPlan,
        position: AccountPosition,
        context: InstrumentContext,
    ) -> Decimal:
        distance = abs(position.avg_price - plan.stop_loss)

        raw = distance * plan.policy.strategy_v2.trailing_distance_r

        result = PositionSupervisor._round_price(
            raw,
            context.tick_size,
        )

        if result <= 0:
            raise RuntimeError("Trailing distance rounded to zero")

        return result

    @staticmethod
    def _round_price(
        value: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        return (value / tick_size).to_integral_value(rounding=ROUND_HALF_UP) * tick_size

    @staticmethod
    def _round_down(
        value: Decimal,
        step: Decimal,
    ) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    async def _save(
        self,
        state: PositionStrategy,
    ) -> None:
        await self._store.save_position_strategy(
            state.model_copy(
                update={
                    "updated_at": (datetime.now(UTC)),
                }
            )
        )
