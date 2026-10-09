from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    ExecutionPlan,
    PlannedOrder,
    PositionStrategy,
    Side,
    StoreError,
    StrategyStatus,
    StrategyV2Policy,
)
from .ports import AccountGateway, PositionSupervisorStore
from .position_management.exit_installer import ExitInstaller
from .position_management.fill_detector import FillDetector
from .position_management.trailing_stop_manager import TrailingStopManager

logger = logging.getLogger(__name__)


class PositionSupervisor:
    def __init__(
        self,
        *,
        store: PositionSupervisorStore,
        executor: AccountGateway,
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
        self._trailing_stop_manager = TrailingStopManager(executor=executor)
        self._fill_detector = FillDetector(executor=executor)
        self._exit_installer = ExitInstaller(
            executor=executor,
            trailing_stop_manager=self._trailing_stop_manager,
            fill_detector=self._fill_detector,
            store=store,
        )

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
        # The durable strategy snapshot must be loaded while holding the
        # same lock used for exchange mutations and quarantine writes.
        first_error: Exception | None = None

        async with self._mutation_lock:
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

            account: AccountStateSummary | None = None

            by_symbol = self._strategies_by_symbol(records)

            for symbol, strategies in by_symbol.items():
                account, error = await self._reconcile_symbol_locked(
                    symbol,
                    strategies,
                    account,
                )

                if error is not None and first_error is None:
                    first_error = error

        # Preserve fail-closed startup and direct callers without starving
        # healthy symbols in the recurring supervisor loop.
        if first_error is not None:
            raise first_error

    @staticmethod
    def _strategies_by_symbol(
        records: Sequence[
            tuple[
                PositionStrategy,
                ExecutionPlan,
            ]
        ],
    ) -> dict[
        str,
        list[
            tuple[
                PositionStrategy,
                ExecutionPlan,
            ]
        ],
    ]:
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

        return by_symbol

    async def _reconcile_symbol_locked(
        self,
        symbol: str,
        strategies: list[
            tuple[
                PositionStrategy,
                ExecutionPlan,
            ]
        ],
        account: AccountStateSummary | None,
    ) -> tuple[AccountStateSummary | None, Exception | None]:
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

            return account, None

        state, plan = strategies[0]

        if state.status is StrategyStatus.UNCERTAIN:
            if state.strategy_id not in self._reported_uncertain:
                logger.warning(
                    "Strategy %s %s is UNCERTAIN; leaving exchange state untouched",
                    state.strategy_id,
                    state.symbol,
                )
                self._reported_uncertain.add(state.strategy_id)
            return account, None

        if account is None:
            account = await self._executor.account_state()

        try:
            return await self._reconcile(state, plan, account), None
        except StoreError:
            # All strategies share durable storage; no further trading
            # is safe when its state cannot be read or committed.
            raise
        except Exception as exc:
            logger.exception(
                "Strategy %s %s reconciliation failed",
                state.strategy_id,
                symbol,
            )
            # A mutation may have succeeded before the error. Refresh
            # account state before reconciling another strategy.
            return None, exc

    async def _blocked_reconcile_outcome(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition | None,
        pending_entries: tuple[str, ...],
    ) -> AccountStateSummary | None:
        if state.status is StrategyStatus.CLOSING:
            return await self._handle_closing(state, account, position)

        if position is None:
            return await self._handle_missing_position(
                state,
                pending_entries,
                account,
            )

        if position.side is not state.side:
            return await self._manual_override(
                state,
                account,
                "position side mismatch",
            )

        if abs(position.avg_price - plan.stop_loss) <= 0:
            return await self._manual_override(
                state,
                account,
                "invalid stop loss",
            )

        return None

    async def _reconcile(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        account, expiry_handled = await self._handle_expired_entries(
            state,
            plan,
            account,
        )
        if expiry_handled:
            return account

        position = self._position(state, account)
        pending_entries = self._pending_entries(state, account)

        blocked = await self._blocked_reconcile_outcome(
            state,
            plan,
            account,
            position,
            pending_entries,
        )

        if blocked is not None:
            return blocked

        assert position is not None

        if state.installing_exits:
            return await self._handle_installing_exits(
                state,
                plan,
                account,
                position,
            )

        override_reason = self._manual_override_reason(
            state,
            plan,
            account,
            position,
        )

        if override_reason is not None:
            return await self._manual_override(
                state,
                account,
                override_reason,
            )

        # Complete interrupted protection handoffs even when the exit
        # revision was persisted on a previous reconciliation.
        if self._exit_installer.partial_stops(state, plan, account):
            account = await self._exit_installer.handoff_partial_stops(
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
            return await self._handle_entry_building(
                state,
                plan,
                account,
                position,
                live_r,
                strategy,
            )

        if state.rebalance_needed:
            return await self._handle_rebalance(
                state,
                plan,
                account,
                strategy,
            )

        return await self._handle_profit_management(
            state,
            plan,
            account,
            position,
            live_r,
            strategy,
        )

    async def _handle_closing(
        self,
        state: PositionStrategy,
        account: AccountStateSummary,
        position: AccountPosition | None,
    ) -> AccountStateSummary:
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

    async def _handle_missing_position(
        self,
        state: PositionStrategy,
        pending_entries: tuple[str, ...],
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        if state.last_position_qty is not None and not pending_entries:
            await self._save(
                state.model_copy(
                    update={
                        "status": (StrategyStatus.CLOSED),
                    }
                )
            )
        return account

    async def _manual_override(
        self,
        state: PositionStrategy,
        account: AccountStateSummary,
        reason: str,
    ) -> AccountStateSummary:
        if reason != "position side mismatch" and reason != "invalid stop loss":
            logger.warning(
                "Strategy %s %s -> MANUAL_OVERRIDE: %s",
                state.strategy_id,
                state.symbol,
                reason,
            )

        await self._save(
            state.model_copy(
                update={
                    "status": (StrategyStatus.MANUAL_OVERRIDE),
                }
            )
        )
        return account

    async def _handle_installing_exits(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> AccountStateSummary:
        # Resume the same revision and sizing snapshot; completed orders retain
        # their unique IDs in exchange history and must never be resubmitted.
        snapshot = replace(
            position,
            size=state.last_position_qty or position.size,
            avg_price=state.last_avg_price or position.avg_price,
        )
        account, stop, trail = await self._exit_installer.install_structure(
            state,
            plan,
            snapshot,
            account,
            enable_trailing=state.trailing_active,
        )
        await self._save(
            state.model_copy(
                update={
                    "installing_exits": False,
                    "protected_stop_loss": stop,
                    "trailing_distance": trail,
                    "status": (
                        StrategyStatus.PROFIT_PROTECTED
                        if state.trailing_active
                        else StrategyStatus.OPEN_RISK
                    ),
                }
            )
        )
        return account

    async def _handle_entry_building(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
        live_r: Decimal,
        strategy: StrategyV2Policy,
    ) -> AccountStateSummary:
        previous_position_qty = state.last_position_qty

        state, fixed_exit_filled = await self._fill_detector.detect(
            state,
            account,
            position,
        )

        if fixed_exit_filled:
            return await self._reconcile_after_fixed_exit(state, plan, account)

        position_grew = (
            previous_position_qty is not None and position.size > previous_position_qty
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
            return await self._continue_accumulation(
                state,
                plan,
                position,
                account,
                reinstall_structure=(structure_missing or position_grew),
            )

        return await self._freeze_entries(state, plan, account, strategy)

    async def _reconcile_after_fixed_exit(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> AccountStateSummary:
        await self._executor.cancel_pending_entries(state.symbol)
        account = await self._executor.account_state()
        updated_position = self._position(state, account)
        if updated_position is None:
            await self._save(state.model_copy(update={"status": StrategyStatus.CLOSED}))
            return account
        state = state.model_copy(
            update={
                "entry_frozen": True,
                "last_position_qty": updated_position.size,
                "last_avg_price": updated_position.avg_price,
            }
        )
        await self._save(state)
        return await self._reconcile(state, plan, account)

    async def _continue_accumulation(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        position: AccountPosition,
        account: AccountStateSummary,
        *,
        reinstall_structure: bool,
    ) -> AccountStateSummary:
        if reinstall_structure:
            state = state.model_copy(
                update={
                    "exit_revision": (state.exit_revision + 1),
                }
            )

            state, account = await self._install_structure(
                state,
                plan,
                position,
                account,
                enable_trailing=False,
            )

        await self._save(state)
        return account

    async def _freeze_entries(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        strategy: StrategyV2Policy,
        *,
        cancel_pending: bool = True,
    ) -> AccountStateSummary:
        if cancel_pending:
            await self._executor.cancel_pending_entries(state.symbol)
        account = await self._executor.account_state()
        updated_position = self._position(state, account)

        if updated_position is None:
            await self._save(
                state.model_copy(
                    update={
                        "status": (StrategyStatus.CLOSED),
                    }
                )
            )
            return account

        live_r = self._live_r(
            updated_position,
            plan.stop_loss,
        )

        state = state.model_copy(
            update={
                "entry_frozen": True,
                "base_position_qty": (updated_position.size),
                "last_position_qty": (updated_position.size),
                "last_avg_price": (updated_position.avg_price),
                "exit_revision": (state.exit_revision + 1),
                "rebalance_needed": False,
            }
        )

        trailing_enabled = live_r >= strategy.trailing_activation_r

        state, account = await self._install_structure(
            state,
            plan,
            updated_position,
            account,
            enable_trailing=trailing_enabled,
        )

        state = state.model_copy(
            update={
                "status": (
                    StrategyStatus.PROFIT_PROTECTED
                    if trailing_enabled
                    else StrategyStatus.OPEN_RISK
                ),
                "trailing_active": trailing_enabled,
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
    ) -> tuple[PositionStrategy, AccountStateSummary]:
        (
            account,
            installed_stop,
            installed_trail,
        ) = await self._exit_installer.install_structure(
            state,
            plan,
            position,
            account,
            enable_trailing=enable_trailing,
        )

        return (
            state.model_copy(
                update={
                    "protected_stop_loss": (installed_stop),
                    "trailing_distance": (installed_trail),
                }
            ),
            account,
        )

    async def _handle_rebalance(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        strategy: StrategyV2Policy,
    ) -> AccountStateSummary:
        account = await self._executor.account_state()
        position = self._position(state, account)

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

        trailing = state.trailing_active or (live_r >= strategy.trailing_activation_r)

        (
            account,
            installed_stop,
            installed_trail,
        ) = await self._exit_installer.install_structure(
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

    async def _handle_profit_management(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
        live_r: Decimal,
        strategy: StrategyV2Policy,
    ) -> AccountStateSummary:
        current_position = position

        state, _ = await self._fill_detector.detect(
            state,
            account,
            current_position,
        )

        if not state.trailing_active and (live_r >= strategy.trailing_activation_r):
            await self._executor.cancel_pending_entries(state.symbol)
            context = await self._executor.market_context(state.symbol)

            trailing_distance = self._trailing_stop_manager.trailing_distance(
                plan,
                current_position,
                context,
            )

            protected_stop = self._trailing_stop_manager.protected_stop(
                plan,
                current_position,
                context,
                previous=(state.protected_stop_loss),
            )

            await self._executor.set_position_protection(
                state.symbol,
                protected_stop,
                trailing_distance=(trailing_distance),
            )

            account = await self._executor.account_state()
            verified_position = self._position(state, account)

            if verified_position is None:
                await self._save(
                    state.model_copy(
                        update={
                            "status": (StrategyStatus.CLOSED),
                        }
                    )
                )
                return account

            self._trailing_stop_manager.verify_protection(
                verified_position,
                protected_stop,
                trailing_distance,
            )

            current_position = verified_position

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
                "last_position_qty": (current_position.size),
                "last_avg_price": (current_position.avg_price),
            }
        )

        await self._save(state)
        return account

    def _manual_override_reason(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        for check in (
            self._unattributed_partial_stop,
            self._owned_entry_drift,
            self._size_increased_after_freeze,
            self._average_changed_after_freeze,
            self._protection_drift,
            self._trailing_drift,
        ):
            reason = check(state, plan, account, position)

            if reason is not None:
                return reason

        return None

    def _unattributed_partial_stop(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        # Bybit carries the originating entry's orderLinkId on attached
        # TP/SL orders. Stop price and side alone cannot prove ownership.
        expected_parents = {
            self._fill_detector.entry_link_id(state, order.name)
            for order in plan.orders
        }
        if any(
            order.symbol == state.symbol
            and order.stop_order_type == "PartialStopLoss"
            and order.trigger_price == plan.stop_loss
            and order.parent_order_link_id not in expected_parents
            for order in account.open_orders
        ):
            return "unattributed partial stop-loss at the strategy stop; manual review required"

        return None

    def _owned_entry_drift(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        if state.entry_frozen:
            return None

        expected = {
            self._fill_detector.entry_link_id(
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

            reason = self._entry_order_drift(state, order, expected)

            if reason is not None:
                return reason

        return None

    @staticmethod
    def _entry_order_drift(
        state: PositionStrategy,
        order: AccountOrder,
        expected: dict[str, PlannedOrder],
    ) -> str | None:
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

    @staticmethod
    def _size_increased_after_freeze(
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        if (
            state.entry_frozen
            and not state.rebalance_needed
            and state.last_position_qty is not None
            and position.size > state.last_position_qty
        ):
            return "position size increased after entry freeze"

        return None

    @staticmethod
    def _average_changed_after_freeze(
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        if (
            state.entry_frozen
            and not state.rebalance_needed
            and state.last_avg_price is not None
            and position.avg_price != state.last_avg_price
        ):
            return "average entry changed after entry freeze"

        return None

    def _protection_drift(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
        if (
            state.protected_stop_loss is not None
            and position.stop_loss != state.protected_stop_loss
            and not (
                position.stop_loss is None
                and self._exit_installer.partial_stops(state, plan, account)
            )
        ):
            return (
                "position stop changed outside "
                "Strategy V2: "
                f"expected="
                f"{state.protected_stop_loss}, "
                f"live={position.stop_loss}"
            )

        return None

    @staticmethod
    def _trailing_drift(
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> str | None:
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
        return tuple(
            order.order_link_id
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and not order.reduce_only
                and order.order_link_id.startswith(
                    f"ccb-v2-{state.strategy_id.hex[:20]}-e"
                )
            )
        )

    @staticmethod
    def _planned_entry_link_ids(
        state: PositionStrategy,
        plan: ExecutionPlan,
    ) -> set[str]:
        return {
            f"ccb-v2-{state.strategy_id.hex[:20]}-{planned.name.lower()}"
            for planned in plan.orders
        }

    async def _handle_expired_entries(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
    ) -> tuple[AccountStateSummary, bool]:
        position = self._position(state, account)
        account, expired = await self._expire_stale_entries(
            state,
            plan,
            account,
            position,
        )
        if not expired:
            return account, False

        position = self._position(state, account)
        pending_entries = self._pending_entries(state, account)
        if position is None and not pending_entries:
            await self._save(state.model_copy(update={"status": StrategyStatus.CLOSED}))
            return account, True
        if position is not None and not state.entry_frozen:
            account = await self._freeze_entries(
                state,
                plan,
                account,
                plan.policy.strategy_v2,
                cancel_pending=False,
            )
            return account, True
        return account, False

    async def _expire_stale_entries(
        self,
        state: PositionStrategy,
        plan: ExecutionPlan,
        account: AccountStateSummary,
        position: AccountPosition | None,
    ) -> tuple[AccountStateSummary, bool]:
        if state.status in {
            StrategyStatus.CLOSING,
            StrategyStatus.MANUAL_OVERRIDE,
            StrategyStatus.UNCERTAIN,
        }:
            return account, False
        ttl_minutes = plan.policy.strategy_v2.entry_order_ttl_minutes
        expires_at = plan.created_at + timedelta(minutes=ttl_minutes)
        if ttl_minutes == 0 or datetime.now(UTC) < expires_at:
            return account, False

        expected_link_ids = self._planned_entry_link_ids(state, plan)
        stale_entries = tuple(
            order
            for order in account.open_orders
            if (
                order.symbol == state.symbol
                and not order.reduce_only
                and order.order_link_id in expected_link_ids
            )
        )
        if stale_entries:
            logger.info(
                "Expiring %d Strategy V2 entry leg(s) for %s after %d minutes",
                len(stale_entries),
                state.symbol,
                ttl_minutes,
            )
            for order in stale_entries:
                await self._executor.cancel_order(state.symbol, order.order_id)
            account = await self._executor.account_state()
        elif position is None:
            logger.info(
                "Closing expired unfilled Strategy V2 plan for %s after %d minutes",
                state.symbol,
                ttl_minutes,
            )

        return account, True

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
