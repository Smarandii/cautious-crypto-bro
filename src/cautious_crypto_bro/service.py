from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)

from .domain import (
    AccountPnlSummary,
    AccountStateSummary,
    ApprovalMode,
    AutoApprovalMode,
    ExecutionPlan,
    IncomingPost,
    IntentExecutionOutcome,
    IntentStatus,
    OpenRelation,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    PositionActionType,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .openrouter import IntentExtractor
from .ports import (
    AccountGateway,
    ApprovalSender,
    SignalServiceStore,
)
from .signal_context import (
    SignalContextProvider,
    SignalContextSnapshot,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        if source_processing_lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")

        self._store = store
        self._extractor = extractor
        self._planner = planner
        self._executor = executor
        self._approval_bot = approval_bot
        self._coordinator = coordinator
        self._context_provider = context_provider
        self._auto_approval_mode = auto_approval_mode
        self._source_processing_lease_seconds = source_processing_lease_seconds

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
                    await self._deliver_manual(signal, plan, send_account_state=False)
            else:
                action = await self._store.get_position_action(record_id)
                if action is not None and action.status is IntentStatus.PENDING:
                    await self._deliver_manual(action, send_account_state=False)

    async def run_manual_delivery_recovery(self) -> None:
        while True:
            try:
                await self.recover_manual_deliveries()
            except Exception:
                logger.exception("Manual approval recovery failed")
            await asyncio.sleep(30)

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

        await self._dispatch_planned_intents(
            batch.planned,
            account_state=batch.account_state,
            account_state_error=batch.account_state_error,
            account_pnl=account_pnl,
            account_pnl_error=account_pnl_error,
        )
        await self._dispatch_position_actions(
            batch.position_actions,
            account_state=batch.account_state,
            account_state_error=batch.account_state_error,
            account_pnl=account_pnl,
            account_pnl_error=account_pnl_error,
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
            policy = await self._store.get_execution_policy()
            trading_capital_usdt = await self._executor.wallet_balance_usdt()
            policy = policy.model_copy(
                update={"trading_capital_usdt": (trading_capital_usdt)}
            )

        except Exception as exc:
            planning_errors.append(
                f"Execution policy/capital: {type(exc).__name__}: {exc}"
            )

            logger.exception(
                "Execution policy/capital load failed",
            )
            return planned, planning_errors

        for extracted_intent in signals.open_intents:
            key = (extracted_intent.symbol, extracted_intent.side)

            approval_mode = self._open_approval_mode(
                extracted_intent,
                position_context=(position_context),
                account_state=(account_state),
                duplicate_in_batch=(open_counts[key] > 1),
            )

            intent = extracted_intent.model_copy(
                update={"approval_mode": (approval_mode)}
            )

            try:
                market_context = await self._executor.market_context(intent.symbol)
                plan = self._planner.plan(
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
                continue

            planned.append((intent, plan))

        return planned, planning_errors

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
        account_state: AccountStateSummary | None,
        account_state_error: str | None,
        account_pnl: AccountPnlSummary | None,
        account_pnl_error: str | None,
    ) -> None:
        manual_cards_sent = 0

        for intent, plan in planned:
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
            if account_state is not None:
                exposure = account_state.exposure_for(intent.symbol)

            try:
                sent = await self._deliver_manual(
                    intent,
                    plan,
                    exposure=exposure,
                    exposure_error=(account_state_error),
                    account_state=account_state,
                    account_state_error=(account_state_error),
                    account_pnl=account_pnl,
                    account_pnl_error=(account_pnl_error),
                    send_account_state=(manual_cards_sent == 0),
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
        account_state: AccountStateSummary | None,
        account_state_error: str | None,
        account_pnl: AccountPnlSummary | None,
        account_pnl_error: str | None,
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
                    account_state=account_state,
                    account_state_error=(account_state_error),
                    account_pnl=account_pnl,
                    account_pnl_error=(account_pnl_error),
                    send_account_state=(manual_cards_sent == 0),
                )

            except Exception:
                logger.exception(
                    "Approval delivery failed for persisted position action %s",
                    action.action_id,
                )
                continue

            manual_cards_sent += int(sent)
