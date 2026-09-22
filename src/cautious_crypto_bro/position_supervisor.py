from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_UP,
    Decimal,
)

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
            return

        async with self._mutation_lock:
            account = await self._executor.account_state()

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

        live_r = self._live_r(
            position,
            plan.stop_loss,
        )

        strategy = plan.policy.strategy_v2

        if not state.entry_frozen:
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

            if live_r < strategy.trailing_activation_r and not state.rebalance_needed:
                await self._save(state)

                return account

            await self._executor.cancel_pending_entries(state.symbol)

            # Read again after cancelling accumulation.
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

            account = await self._install_structure(
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

            account = await self._install_structure(
                state,
                plan,
                position,
                account,
                enable_trailing=trailing,
            )

            state = state.model_copy(
                update={
                    "trailing_active": trailing,
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

            await self._executor.set_position_protection(
                state.symbol,
                plan.stop_loss,
                trailing_distance=(trailing_distance),
            )

            state = state.model_copy(
                update={
                    "trailing_active": True,
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
    ) -> AccountStateSummary:
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

        # Capture only the entry-attached partial
        # SL orders before installing the Full stop.
        partial_sl_ids = tuple(
            order.order_id
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and order.stop_order_type == "PartialStopLoss"
            )
        )

        await self._executor.set_position_protection(
            state.symbol,
            plan.stop_loss,
            trailing_distance=trailing_distance,
        )

        # The Full stop is already live before old
        # entry-attached partial stops are removed.
        for order_id in partial_sl_ids:
            await self._executor.cancel_order(
                state.symbol,
                order_id,
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

        if remaining_weight <= 0:
            return await self._executor.account_state()

        risk_distance = abs(position.avg_price - plan.stop_loss)

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

            if quantity < context.min_qty or quantity <= 0:
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
