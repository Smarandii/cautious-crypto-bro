from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountPnlSummary,
    AccountSnapshotDelivery,
    AccountStateSummary,
    ApprovalMode,
    AutoApprovalMode,
    ExecutionPlan,
    ExecutionPolicy,
    IncomingPost,
    IntentExecutionOutcome,
    IntentStatus,
    OpenRelation,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    PositionActionType,
    Side,
    SignalContextSnapshot,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .portfolio_risk import (
    open_stop_risk_usdt,
    policy_with_remaining_portfolio_risk,
)
from .ports import (
    AccountGateway,
    ApprovalSender,
    ExecutionCoordinator,
    IntentExtractor,
    SignalContextProvider,
    SignalServiceStore,
)

logger = logging.getLogger(__name__)
ACCOUNT_PNL_SYNC_INTERVAL_SECONDS = 15 * 60


@dataclass(frozen=True)
class _PlannedBatch:
    planned: list[
        tuple[
            TradingIntent,
            ExecutionPlan,
        ]
    ]
    position_actions: tuple[PositionActionIntent, ...]
    planning_errors: list[str]
    account_state: AccountStateSummary | None
    account_state_error: str | None


class SignalService:
    def __init__(
        self,
        *,
        store: SignalServiceStore,
        extractor: IntentExtractor,
        planner: ExecutionPlanner,
        executor: AccountGateway,
        approval_bot: ApprovalSender,
        coordinator: ExecutionCoordinator,
        context_provider: SignalContextProvider,
        auto_approval_mode: AutoApprovalMode = (AutoApprovalMode.DISABLED),
        source_processing_lease_seconds: int = 300,
        demo_long_risk_multiplier: Decimal = Decimal("1"),
        demo_long_exit_control_fraction: Decimal = Decimal("0"),
        demo_long_participation_skip_fraction: Decimal = Decimal("0"),
        demo_exit_profile: str = "baseline",
        demo_portfolio_stop_risk_cap_usdt: Decimal | None = None,
        demo_entry_order_ttl_minutes: int = 0,
    ) -> None:
        if source_processing_lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")
        if not Decimal("0") < demo_long_risk_multiplier <= Decimal("1"):
            raise ValueError("Demo LONG risk multiplier must be in (0, 1]")
        if not Decimal("0") <= demo_long_exit_control_fraction <= Decimal("1"):
            raise ValueError(
                "Demo LONG exit control fraction must be between zero and one"
            )
        if not Decimal("0") <= demo_long_participation_skip_fraction <= Decimal("1"):
            raise ValueError(
                "Demo LONG participation skip fraction must be between zero and one"
            )
        if (
            demo_long_exit_control_fraction > 0
            and demo_exit_profile != "payoff_early_tight_trail_long_015"
        ):
            raise ValueError(
                "Demo LONG exit control split requires the long_015 profile"
            )
        if demo_exit_profile not in {
            "baseline",
            "payoff_challenger",
            "payoff_early_trail",
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
        }:
            raise ValueError("Unsupported Demo exit profile")
        if (
            demo_portfolio_stop_risk_cap_usdt is not None
            and demo_portfolio_stop_risk_cap_usdt <= 0
        ):
            raise ValueError("Demo portfolio stop-risk cap must be positive")
        if demo_entry_order_ttl_minutes < 0:
            raise ValueError("Demo entry order TTL must not be negative")

        self._store = store
        self._extractor = extractor
        self._planner = planner
        self._executor = executor
        self._approval_bot = approval_bot
        self._coordinator = coordinator
        self._context_provider = context_provider
        self._auto_approval_mode = auto_approval_mode
        self._source_processing_lease_seconds = source_processing_lease_seconds
        self._demo_long_risk_multiplier = demo_long_risk_multiplier
        self._demo_long_exit_control_fraction = demo_long_exit_control_fraction
        self._demo_long_participation_skip_fraction = (
            demo_long_participation_skip_fraction
        )
        self._demo_exit_profile = demo_exit_profile
        self._demo_portfolio_stop_risk_cap_usdt = demo_portfolio_stop_risk_cap_usdt
        self._demo_entry_order_ttl_minutes = demo_entry_order_ttl_minutes

    async def _deliver_manual(
        self,
        signal: TradingIntent | PositionActionIntent,
        plan: ExecutionPlan | None = None,
        **kwargs,
    ) -> bool:
        record_id = (
            signal.intent_id if isinstance(signal, TradingIntent) else signal.action_id
        )
        token = await self._store.claim_manual_delivery(record_id)
        if token is None:
            return False
        try:
            if isinstance(signal, TradingIntent):
                if plan is None:
                    raise ValueError("Manual OPEN delivery requires its persisted plan")
                await self._approval_bot.send_intent(signal, plan, **kwargs)
            else:
                await self._approval_bot.send_position_action(signal, **kwargs)
        except Exception:
            await self._store.finish_manual_delivery(record_id, token, delivered=False)
            logger.exception(
                "Approval delivery failed for %s; retry scheduled", record_id
            )
            return False
        await self._store.finish_manual_delivery(record_id, token, delivered=True)
        return True

    async def recover_manual_deliveries(self) -> None:
        requeued, stale = await self._store.reconcile_manual_deliveries()
        if requeued or stale:
            logger.info(
                "Manual delivery reconciliation: %d requeued, %d stale failed",
                requeued,
                stale,
            )

        for record_id, kind in await self._store.pending_manual_deliveries():
            if kind == "OPEN":
                signal = await self._store.get_intent(record_id)
                plan = await self._store.get_execution_plan(record_id)
                if (
                    signal is not None
                    and plan is not None
                    and signal.status is IntentStatus.PENDING
                ):
                    await self._deliver_manual(signal, plan)
            else:
                action = await self._store.get_position_action(record_id)
                if action is not None and action.status is IntentStatus.PENDING:
                    await self._deliver_manual(action)

    async def run_manual_delivery_recovery(self) -> None:
        while True:
            try:
                await self.recover_manual_deliveries()
            except Exception:
                logger.exception("Manual approval recovery failed")
            await asyncio.sleep(30)

    async def run_periodic_account_pnl_sync(self) -> None:
        while True:
            try:
                await self._sync_account_pnl()
            except Exception:
                logger.exception("Periodic account P&L sync failed")
            await asyncio.sleep(ACCOUNT_PNL_SYNC_INTERVAL_SECONDS)

    async def _sync_account_pnl(
        self,
    ):
        now = datetime.now(UTC)

        sync_state = await self._store.get_account_pnl_sync_state()

        if sync_state is None:
            history_start = now - timedelta(days=7)
            sync_start = history_start

        else:
            history_start = sync_state.history_start_at

            sync_start = min(
                sync_state.last_synced_at,
                now,
            ) - timedelta(days=1)

        records = await self._executor.closed_pnl_history(
            sync_start,
            now,
        )

        await self._store.upsert_closed_pnl(records)

        await self._store.mark_account_pnl_synced(
            history_start_at=history_start,
            last_synced_at=now,
        )

        return await self._store.get_account_pnl_summary()

    @staticmethod
    def _manual(
        message: str,
        *args: object,
    ) -> ApprovalMode:
        logger.warning(message, *args)
        return ApprovalMode.MANUAL

    def _open_approval_mode(
        self,
        intent: TradingIntent,
        *,
        position_context: SignalPositionContext,
        account_state: AccountStateSummary | None,
        duplicate_in_batch: bool,
    ) -> ApprovalMode:
        if self._auto_approval_mode is AutoApprovalMode.DISABLED:
            return ApprovalMode.MANUAL

        if intent.relation not in {
            OpenRelation.NEW,
            OpenRelation.ADD_OR_REENTRY,
        }:
            return self._manual(
                "OPEN %s from %s/%s remains MANUAL because relation is %s",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
                intent.relation.value,
            )

        if duplicate_in_batch:
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL because the same symbol/"
                "side appears multiple times in "
                "the current post",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        if account_state is None or not position_context.account_state_available:
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL because trusted live "
                "account state is unavailable",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        if intent.relation is OpenRelation.NEW and position_context.has_existing_copy(
            intent.symbol,
            intent.side,
        ):
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL: model classified NEW "
                "but trusted source history "
                "already has a copied/in-flight "
                "position",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        safety_reason = self._coordinator.auto_open_safety_reason(
            intent,
            account_state,
        )

        if safety_reason is not None:
            return self._manual(
                "OPEN %s from %s/%s remains MANUAL: %s",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
                safety_reason,
            )

        return ApprovalMode.AUTO

    def _position_action_approval_mode(
        self,
        action: PositionActionIntent,
        *,
        account_state: AccountStateSummary | None,
    ) -> ApprovalMode:
        if account_state is None:
            return self._manual(
                "Position action %s %s remains "
                "MANUAL because live account "
                "state is unavailable",
                action.action.value,
                action.symbol,
            )

        if action.action is PositionActionType.CANCEL_ENTRIES:
            # Execution resolves ownership from durable source records, not position count.
            executable = True
        else:
            positions = tuple(
                position
                for position in account_state.positions
                if position.symbol == action.symbol
            )
            executable = len(positions) == 1 and (
                action.expected_side is None
                or action.expected_side is positions[0].side
            )

        if not executable:
            logger.info(
                "Position action %s %s skipped; no matching live position",
                action.action.value,
                action.symbol,
            )
            return ApprovalMode.SKIPPED

        if self._auto_approval_mode is not AutoApprovalMode.ALL:
            return ApprovalMode.MANUAL

        return ApprovalMode.AUTO

    async def _send_auto_outcome(
        self, outcome: IntentExecutionOutcome | PositionActionExecutionOutcome
    ) -> None:
        # Fetch after execution so the preview includes the resulting account state.
        state = pnl = None
        state_error = pnl_error = None
        try:
            state = await self._executor.account_state()
        except Exception as exc:
            state_error = f"{type(exc).__name__}: {exc}"
        try:
            pnl = await self._sync_account_pnl()
        except Exception as exc:
            pnl_error = f"{type(exc).__name__}: {exc}"
        try:
            await self._approval_bot.send_account_snapshot(
                state, state_error=state_error, pnl=pnl, pnl_error=pnl_error
            )
        except Exception:
            logger.exception("AUTO account preview delivery failed")
        if isinstance(outcome, IntentExecutionOutcome):
            await self._approval_bot.send_auto_intent_outcome(outcome)
        else:
            await self._approval_bot.send_auto_action_outcome(outcome)

    async def recover_auto_execution(
        self,
    ) -> None:
        pending_intents = await self._store.get_pending_auto_intent_ids()

        pending_actions = await self._store.get_pending_auto_action_ids()

        if self._auto_approval_mode is AutoApprovalMode.DISABLED:
            for intent_id in pending_intents:
                await self._store.mark_failed(
                    intent_id,
                    ("AUTO recovery cancelled: auto approval is disabled"),
                )

            for action_id in pending_actions:
                await self._store.mark_position_action_failed(
                    action_id,
                    ("AUTO recovery cancelled: auto approval is disabled"),
                )

            return

        for intent_id in pending_intents:
            outcome = await self._coordinator.execute_intent(
                intent_id,
                approval_mode=(ApprovalMode.AUTO),
            )

            try:
                await self._send_auto_outcome(outcome)
            except Exception:
                logger.exception(
                    "Failed to send recovered AUTO intent outcome %s",
                    intent_id,
                )

        if self._auto_approval_mode is not AutoApprovalMode.ALL:
            for action_id in pending_actions:
                await self._store.mark_position_action_failed(
                    action_id,
                    (
                        "AUTO recovery cancelled: "
                        "current mode does not "
                        "allow position actions"
                    ),
                )

            return

        for action_id in pending_actions:
            outcome = await self._coordinator.execute_position_action(
                action_id,
                approval_mode=(ApprovalMode.AUTO),
            )

            try:
                await self._send_auto_outcome(outcome)
            except Exception:
                logger.exception(
                    "Failed to send recovered AUTO action outcome %s",
                    action_id,
                )

    async def on_message(
        self,
        post: IncomingPost,
    ) -> None:
        source = post.source

        claim_token = await self._claim_source_or_skip(source)
        if claim_token is None:
            return

        batch = await self._extract_and_plan(post, claim_token)
        if batch is None:
            return

        if not batch.planned and not batch.position_actions:
            await self._complete_or_fail_source(
                source,
                claim_token,
                batch.planning_errors,
            )
            return

        if batch.planning_errors:
            logger.warning(
                "Signal batch kept %d OPEN "
                "candidate(s) and %d position "
                "action(s) for %s/%s; "
                "%d OPEN candidate(s) failed",
                len(batch.planned),
                len(batch.position_actions),
                source.channel_id,
                source.message_id,
                len(batch.planning_errors),
            )

        account_pnl, account_pnl_error = await self._sync_account_pnl_safe(source)

        finalized = await self._persist_batch(
            source,
            claim_token,
            batch.planned,
            batch.position_actions,
        )
        if not finalized:
            return

        snapshot = AccountSnapshotDelivery(
            state=batch.account_state,
            state_error=batch.account_state_error,
            pnl=account_pnl,
            pnl_error=account_pnl_error,
        )
        await self._dispatch_planned_intents(
            batch.planned,
            snapshot=snapshot,
        )
        await self._dispatch_position_actions(
            batch.position_actions,
            snapshot=snapshot,
        )

    async def _claim_source_or_skip(
        self,
        source: SourceMessage,
    ) -> str | None:
        claim_token = await self._store.claim_source(
            source,
            lease_seconds=(self._source_processing_lease_seconds),
        )

        if claim_token is None:
            logger.debug(
                "Telegram message already completed or currently processing %s/%s",
                source.channel_id,
                source.message_id,
            )

        return claim_token

    async def _extract_and_plan(
        self,
        post: IncomingPost,
        claim_token: str,
    ) -> _PlannedBatch | None:
        source = post.source

        try:
            global_guidance, channel_guidance = await self._store.get_guidance(
                source.channel_id
            )
            context_snapshot = await self._context_provider.snapshot(source.channel_id)
            signals = await self._extractor.extract(
                post,
                global_guidance=global_guidance,
                channel_guidance=channel_guidance,
                position_context=context_snapshot.position_context,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Signal extraction failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return None

        if not signals.actionable:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )
            return None

        planned, planning_errors = await self._plan_open_intents(
            signals,
            context_snapshot,
        )
        position_actions = self._build_position_actions(
            signals,
            account_state=(context_snapshot.account_state),
        )

        return _PlannedBatch(
            planned=planned,
            position_actions=position_actions,
            planning_errors=planning_errors,
            account_state=context_snapshot.account_state,
            account_state_error=context_snapshot.account_state_error,
        )

    async def _plan_open_intents(
        self,
        signals: SignalExtraction,
        context_snapshot: SignalContextSnapshot,
    ) -> tuple[
        list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        list[str],
    ]:
        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ] = []
        planning_errors: list[str] = []

        if not signals.open_intents:
            return planned, planning_errors

        account_state = context_snapshot.account_state
        position_context = context_snapshot.position_context

        open_counts = Counter(
            (intent.symbol, intent.side) for intent in signals.open_intents
        )

        try:
            policy = await self._capital_frozen_policy()
        except Exception as exc:
            planning_errors.append(
                f"Execution policy/capital: {type(exc).__name__}: {exc}"
            )

            logger.exception(
                "Execution policy/capital load failed",
            )
            return planned, planning_errors

        existing_portfolio_risk = await self._existing_portfolio_stop_risk(
            account_state,
            planning_errors,
        )
        if existing_portfolio_risk is None:
            return planned, planning_errors

        batch_reserved_risk = Decimal("0")

        for extracted_intent in signals.open_intents:
            intent = self._routed_intent(
                extracted_intent,
                position_context,
                account_state,
                open_counts,
            )

            intent_policy = self._policy_for_intent_side(
                policy,
                intent.side,
                intent.intent_id,
                participation_eligible=(intent.approval_mode is ApprovalMode.AUTO),
            )

            if self._demo_portfolio_stop_risk_cap_usdt is not None:
                remaining_portfolio_risk = (
                    self._demo_portfolio_stop_risk_cap_usdt
                    - existing_portfolio_risk
                    - batch_reserved_risk
                )
                if remaining_portfolio_risk <= 0:
                    logger.info(
                        "Skipping %s because portfolio stop-risk cap is full",
                        intent.symbol,
                    )
                    continue
                try:
                    intent_policy = policy_with_remaining_portfolio_risk(
                        intent_policy,
                        remaining_portfolio_risk,
                    )
                except ValueError as exc:
                    planning_errors.append(
                        f"{intent.symbol}: portfolio stop-risk cap: {exc}"
                    )
                    continue

            plan = await self._plan_single_intent(
                intent,
                intent_policy,
                planning_errors,
            )

            if plan is not None:
                if intent_policy.strategy_v2.long_participation_arm == "skip":
                    intent = intent.model_copy(
                        update={
                            "approval_mode": ApprovalMode.SKIPPED,
                            "status": IntentStatus.SKIPPED,
                        }
                    )
                else:
                    batch_reserved_risk += plan.planned_max_loss_usdt
                planned.append((intent, plan))

        return planned, planning_errors

    async def _existing_portfolio_stop_risk(
        self,
        account_state: AccountStateSummary | None,
        planning_errors: list[str],
    ) -> Decimal | None:
        cap = self._demo_portfolio_stop_risk_cap_usdt
        if cap is None:
            return Decimal("0")
        if account_state is None:
            planning_errors.append(
                "Portfolio stop-risk cap: live account snapshot is unavailable"
            )
            return None
        try:
            active_strategies = await self._store.get_active_position_strategies()
            return open_stop_risk_usdt(account_state, active_strategies)
        except Exception as exc:
            planning_errors.append(
                f"Portfolio stop-risk cap: {type(exc).__name__}: {exc}"
            )
            logger.exception("Portfolio stop-risk snapshot failed")
            return None

    def _policy_for_intent_side(
        self,
        policy: ExecutionPolicy,
        side: Side,
        intent_id: UUID | None = None,
        *,
        participation_eligible: bool = True,
    ) -> ExecutionPolicy:
        strategy = policy.strategy_v2
        updates = {}
        if side is Side.LONG:
            updates["risk_per_trade_pct"] = (
                policy.risk_per_trade_pct * self._demo_long_risk_multiplier
            )

        if (
            self._demo_long_exit_control_fraction > 0
            and self._demo_exit_profile == "payoff_early_tight_trail_long_015"
        ):
            if side is Side.SHORT:
                strategy = strategy.model_copy(
                    update={"exit_profile": "payoff_early_tight_trail"}
                )
            else:
                if intent_id is None:
                    raise ValueError(
                        "Randomized LONG exit assignment requires an intent ID"
                    )
                control_bucket = int.from_bytes(
                    hashlib.sha256(intent_id.bytes).digest()[:8],
                    "big",
                )
                control_threshold = int(
                    self._demo_long_exit_control_fraction * (1 << 64)
                )
                is_control = control_bucket < control_threshold
                strategy = strategy.model_copy(
                    update={
                        "exit_profile": (
                            "payoff_early_tight_trail_long_ab_020_control"
                            if is_control
                            else "payoff_early_tight_trail_long_ab_015"
                        ),
                        "trailing_activation_r": (
                            Decimal("0.20") if is_control else Decimal("0.15")
                        ),
                    }
                )
        elif (
            side is Side.LONG
            and self._demo_exit_profile == "payoff_early_tight_trail_long_015"
        ):
            strategy = strategy.model_copy(
                update={"trailing_activation_r": Decimal("0.15")}
            )
        if (
            side is Side.LONG
            and participation_eligible
            and self._demo_long_participation_skip_fraction > 0
        ):
            if intent_id is None:
                raise ValueError(
                    "Randomized LONG participation assignment requires an intent ID"
                )
            participation_bucket = int.from_bytes(
                hashlib.sha256(b"long-participation-v1:" + intent_id.bytes).digest()[
                    :8
                ],
                "big",
            )
            skip_threshold = int(
                self._demo_long_participation_skip_fraction * (1 << 64)
            )
            strategy = strategy.model_copy(
                update={
                    "long_participation_arm": (
                        "skip" if participation_bucket < skip_threshold else "take"
                    )
                }
            )
        if strategy != policy.strategy_v2:
            updates["strategy_v2"] = strategy
        return policy.model_copy(update=updates) if updates else policy

    async def _capital_frozen_policy(self) -> ExecutionPolicy:
        policy = await self._store.get_execution_policy()
        trading_capital_usdt = await self._executor.wallet_balance_usdt()
        updates: dict[str, object] = {"trading_capital_usdt": trading_capital_usdt}
        strategy = policy.strategy_v2
        strategy_changed = False
        if self._demo_exit_profile in {
            "payoff_challenger",
            "payoff_early_trail",
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
        }:
            trailing_activation_r = (
                Decimal("0.2")
                if self._demo_exit_profile
                in {
                    "payoff_early_trail",
                    "payoff_early_tight_trail",
                    "payoff_early_tight_trail_long_015",
                }
                else Decimal("0.4")
            )
            strategy = strategy.model_copy(
                update={
                    "exit_profile": self._demo_exit_profile,
                    "first_take_profit_r": Decimal("1"),
                    "second_take_profit_r": Decimal("2"),
                    "third_take_profit_r": Decimal("4"),
                    "first_take_profit_pct": Decimal("15"),
                    "second_take_profit_pct": Decimal("20"),
                    "third_take_profit_pct": Decimal("25"),
                    "runner_pct": Decimal("40"),
                    "trailing_activation_r": trailing_activation_r,
                    "trailing_distance_r": (
                        Decimal("0.05")
                        if self._demo_exit_profile
                        in {
                            "payoff_early_tight_trail",
                            "payoff_early_tight_trail_long_015",
                        }
                        else Decimal("0.1")
                    ),
                }
            )
            strategy_changed = True
        if self._demo_entry_order_ttl_minutes:
            strategy = strategy.model_copy(
                update={
                    "entry_order_ttl_minutes": self._demo_entry_order_ttl_minutes,
                }
            )
            strategy_changed = True
        if strategy_changed:
            updates["strategy_v2"] = strategy
        return policy.model_copy(update=updates)

    def _routed_intent(
        self,
        extracted_intent: TradingIntent,
        position_context: SignalPositionContext,
        account_state: AccountStateSummary | None,
        open_counts: Counter[tuple[str, Side]],
    ) -> TradingIntent:
        key = (extracted_intent.symbol, extracted_intent.side)

        approval_mode = self._open_approval_mode(
            extracted_intent,
            position_context=(position_context),
            account_state=(account_state),
            duplicate_in_batch=(open_counts[key] > 1),
        )

        return extracted_intent.model_copy(update={"approval_mode": (approval_mode)})

    async def _plan_single_intent(
        self,
        intent: TradingIntent,
        policy: ExecutionPolicy,
        planning_errors: list[str],
    ) -> ExecutionPlan | None:
        try:
            market_context = await self._executor.market_context(intent.symbol)
            return self._planner.plan(
                intent,
                policy,
                market_context,
            )

        except Exception as exc:
            error = f"{intent.symbol}: {type(exc).__name__}: {exc}"
            planning_errors.append(error)

            logger.exception(
                "Execution planning failed for candidate %s",
                intent.symbol,
            )
            return None

    def _build_position_actions(
        self,
        signals: SignalExtraction,
        *,
        account_state: AccountStateSummary | None,
    ) -> tuple[PositionActionIntent, ...]:
        return tuple(
            action.model_copy(
                update={
                    "approval_mode": (
                        self._position_action_approval_mode(
                            action,
                            account_state=(account_state),
                        )
                    )
                }
            )
            for action in signals.position_actions
        )

    async def _complete_or_fail_source(
        self,
        source: SourceMessage,
        claim_token: str,
        planning_errors: list[str],
    ) -> None:
        if planning_errors:
            await self._store.mark_source_failed(
                source,
                claim_token,
                (
                    "No extracted OPEN "
                    "candidate could be planned: " + " | ".join(planning_errors)
                ),
            )
        else:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )

    async def _sync_account_pnl_safe(
        self,
        source: SourceMessage,
    ) -> tuple[AccountPnlSummary | None, str | None]:
        try:
            return await self._sync_account_pnl(), None

        except Exception as exc:
            logger.exception(
                "Account P&L sync failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return None, f"{type(exc).__name__}: {exc}"

    async def _persist_batch(
        self,
        source: SourceMessage,
        claim_token: str,
        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        position_actions: tuple[PositionActionIntent, ...],
    ) -> bool:
        try:
            finalized = await self._store.create_signal_batch_and_complete_source(
                planned,
                position_actions,
                claim_token,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Signal batch persistence failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return False

        if not finalized:
            logger.warning(
                "Lost processing claim before signal batch persistence for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return False

        return True

    async def _dispatch_planned_intents(
        self,
        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ],
        *,
        snapshot: AccountSnapshotDelivery,
    ) -> None:
        manual_cards_sent = 0

        for intent, plan in planned:
            if intent.approval_mode is ApprovalMode.SKIPPED:
                continue

            if intent.approval_mode is ApprovalMode.AUTO:
                outcome = await self._coordinator.execute_intent(
                    intent.intent_id,
                    approval_mode=(ApprovalMode.AUTO),
                )

                try:
                    await self._send_auto_outcome(outcome)
                except Exception:
                    logger.exception(
                        "AUTO outcome delivery failed for intent %s",
                        intent.intent_id,
                    )

                logger.info(
                    "AUTO OPEN %s finished %s",
                    intent.intent_id,
                    outcome.status.value,
                )
                continue

            exposure = None
            if snapshot.state is not None:
                exposure = snapshot.state.exposure_for(intent.symbol)

            try:
                sent = await self._deliver_manual(
                    intent,
                    plan,
                    exposure=exposure,
                    exposure_error=(snapshot.state_error),
                    snapshot=(snapshot if manual_cards_sent == 0 else None),
                )

            except Exception:
                logger.exception(
                    "Approval delivery failed for persisted intent %s",
                    intent.intent_id,
                )
                continue

            manual_cards_sent += int(sent)

    async def _dispatch_position_actions(
        self,
        position_actions: tuple[PositionActionIntent, ...],
        *,
        snapshot: AccountSnapshotDelivery,
    ) -> None:
        manual_cards_sent = 0

        for action in position_actions:
            if action.approval_mode is ApprovalMode.SKIPPED:
                continue

            if action.approval_mode is ApprovalMode.AUTO:
                outcome = await self._coordinator.execute_position_action(
                    action.action_id,
                    approval_mode=(ApprovalMode.AUTO),
                )

                try:
                    await self._send_auto_outcome(outcome)
                except Exception:
                    logger.exception(
                        "AUTO outcome delivery failed for action %s",
                        action.action_id,
                    )

                logger.info(
                    "AUTO position action %s finished %s",
                    action.action_id,
                    outcome.status.value,
                )
                continue

            try:
                sent = await self._deliver_manual(
                    action,
                    snapshot=(snapshot if manual_cards_sent == 0 else None),
                )

            except Exception:
                logger.exception(
                    "Approval delivery failed for persisted position action %s",
                    action.action_id,
                )
                continue

            manual_cards_sent += int(sent)
