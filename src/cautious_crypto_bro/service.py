from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import Decimal
from uuid import UUID

from .domain import (
    AccountSnapshotDelivery,
    AccountStateSummary,
    ApprovalMode,
    AutoApprovalMode,
    ExecutionPlan,
    IncomingPost,
    PositionActionIntent,
    SignalContextSnapshot,
    SignalExtraction,
    SourceMessage,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .ports import (
    AccountGateway,
    ApprovalSender,
    ExecutionCoordinator,
    IntentExtractor,
    SignalContextProvider,
    SignalServiceStore,
)
from .signals.delivery import SignalDelivery
from .signals.planning import SignalPlanning
from .signals.queued_open import QueuedOpenExecution

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
    planning_rejections: list[str]
    retry_candidates: list[tuple[TradingIntent, bool, datetime]]
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
        intent_max_age_seconds: int = 900,
        demo_long_risk_multiplier: Decimal = Decimal("1"),
        demo_long_exit_control_fraction: Decimal = Decimal("0"),
        demo_long_participation_skip_fraction: Decimal = Decimal("0"),
        demo_long_momentum_gate: bool = False,
        demo_exit_profile: str = "baseline",
        demo_portfolio_stop_risk_cap_usdt: Decimal | None = None,
        demo_entry_order_ttl_minutes: int = 0,
    ) -> None:
        if source_processing_lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")
        self._planning = SignalPlanning(
            store=store,
            planner=planner,
            executor=executor,
            coordinator=coordinator,
            auto_approval_mode=auto_approval_mode,
            intent_max_age_seconds=intent_max_age_seconds,
            demo_long_risk_multiplier=demo_long_risk_multiplier,
            demo_long_exit_control_fraction=demo_long_exit_control_fraction,
            demo_long_participation_skip_fraction=demo_long_participation_skip_fraction,
            demo_long_momentum_gate=demo_long_momentum_gate,
            demo_exit_profile=demo_exit_profile,
            demo_portfolio_stop_risk_cap_usdt=demo_portfolio_stop_risk_cap_usdt,
            demo_entry_order_ttl_minutes=demo_entry_order_ttl_minutes,
        )
        self._store = store
        self._extractor = extractor
        self._executor = executor
        self.delivery = SignalDelivery(
            store=store,
            executor=executor,
            approval_bot=approval_bot,
            lifecycle_max_age_seconds=intent_max_age_seconds,
        )
        self._coordinator = coordinator
        self._context_provider = context_provider
        self._auto_approval_mode = auto_approval_mode
        self._source_processing_lease_seconds = source_processing_lease_seconds
        self.queued_opens = QueuedOpenExecution(
            store=store,
            planning=self._planning,
            context_provider=context_provider,
            coordinator=coordinator,
            delivery=self.delivery,
            auto_approval_mode=auto_approval_mode,
        )

    async def run_delivery_recovery(self) -> None:
        while True:
            try:
                await self.queued_opens.recover()
            except Exception:
                logger.exception("Candidate planning recovery failed")
            try:
                await self.delivery.recover_manual()
            except Exception:
                logger.exception("Manual approval recovery failed")
            try:
                await self.delivery.recover_auto_outcomes()
            except Exception:
                logger.exception("AUTO outcome recovery failed")
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

    async def _reserve_recovery_notices(
        self, pending_intents: tuple[UUID, ...], pending_actions: tuple[UUID, ...]
    ) -> None:
        for intent_id in pending_intents:
            await self._store.ensure_auto_outcome_delivery(intent_id, "OPEN")
        for action_id in pending_actions:
            await self._store.ensure_auto_outcome_delivery(action_id, "ACTION")

    async def recover_auto_execution(
        self,
    ) -> None:
        pending_intents = await self._store.get_pending_auto_intent_ids()

        pending_actions = await self._store.get_pending_auto_action_ids()

        # Reserve notices before any legacy pending work can reach the exchange.
        await self._reserve_recovery_notices(pending_intents, pending_actions)

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
                await self.delivery.auto_outcome(intent_id, "OPEN", outcome)
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
                await self.delivery.auto_outcome(action_id, "ACTION", outcome)
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

        if post.rejection_reason is not None:
            if not await self._store.mark_source_completed(
                source, claim_token, reason=post.rejection_reason
            ):
                raise RuntimeError("Source rejection could not be recorded")
            logger.warning(
                "Rejected Telegram post %s/%s: %s",
                source.channel_id,
                source.message_id,
                post.rejection_reason,
            )
            return

        batch = await self._extract_and_plan(post, claim_token)
        if batch is None:
            return

        if (
            not batch.planned
            and not batch.position_actions
            and not batch.retry_candidates
        ):
            await self._complete_or_fail_source(
                source,
                claim_token,
                batch.planning_errors,
                batch.planning_rejections,
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

        finalized = await self._persist_batch(
            source,
            claim_token,
            batch.planned,
            batch.position_actions,
            batch.retry_candidates,
        )
        if not finalized:
            return

        account_pnl, account_pnl_error = await self.delivery.stored_account_pnl()
        snapshot = AccountSnapshotDelivery(
            state=batch.account_state,
            state_error=batch.account_state_error,
            pnl=account_pnl,
            pnl_error=account_pnl_error,
        )
        await self.queued_opens.dispatch(
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

        if not await self._record_source_decision(
            source, claim_token, signals, context_snapshot
        ):
            return None

        if not signals.actionable:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )
            return None

        planning = await self._planning.plan_open_intents(
            signals,
            context_snapshot,
        )
        planned, planning_errors = planning.planned, planning.errors
        planning_rejections, retry_candidates = (
            planning.rejections,
            planning.retry_candidates,
        )
        position_actions = self._planning.plan_position_actions(
            signals,
            account_state=(context_snapshot.account_state),
            position_context=context_snapshot.position_context,
        )

        if not await self._record_source_decision(
            source,
            claim_token,
            signals,
            context_snapshot,
            planning={
                "planned_intent_ids": [str(intent.intent_id) for intent, _ in planned],
                "position_action_ids": [
                    str(action.action_id) for action in position_actions
                ],
                "errors": planning_errors + planning_rejections,
                "retryable_errors": planning_errors,
                "rejections": planning_rejections,
                "contract_resolutions": [
                    {
                        "intent_id": str(intent.intent_id),
                        "reported_symbol": original.symbol,
                        "contract_symbol": intent.symbol,
                    }
                    for intent, _ in planned
                    for original in signals.open_intents
                    if original.intent_id == intent.intent_id
                    and original.symbol != intent.symbol
                ],
            },
        ):
            return None

        return _PlannedBatch(
            planned=planned,
            position_actions=position_actions,
            planning_errors=planning_errors,
            planning_rejections=planning_rejections,
            retry_candidates=retry_candidates,
            account_state=context_snapshot.account_state,
            account_state_error=context_snapshot.account_state_error,
        )

    async def _record_source_decision(
        self,
        source: SourceMessage,
        claim_token: str,
        signals: SignalExtraction,
        context_snapshot: SignalContextSnapshot,
        *,
        planning: dict | None = None,
    ) -> bool:
        # Alternate extractors without original evidence remain explicitly unknown.
        if signals.raw_extraction is None:
            return True
        payload = {
            "version": 1,
            "phase": "PLANNED" if planning is not None else "NORMALIZED",
            "evaluation_fingerprint": signals.evaluation_fingerprint,
            "evaluation_model": signals.evaluation_model,
            "source": source.model_dump(mode="json"),
            "extraction": signals.raw_extraction.model_dump(mode="json"),
            "position_context": context_snapshot.position_context.model_dump(
                mode="json"
            ),
            "normalized": {
                "open_intents": [
                    item.model_dump(mode="json") for item in signals.open_intents
                ],
                "position_actions": [
                    item.model_dump(mode="json") for item in signals.position_actions
                ],
            },
            "planning": planning,
        }
        try:
            return await self._store.record_source_decision(
                source, claim_token, json.dumps(payload, ensure_ascii=False)
            )
        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"Decision persistence failed: {type(exc).__name__}",
            )
            logger.exception("Source decision persistence failed")
            return False

    async def _complete_or_fail_source(
        self,
        source: SourceMessage,
        claim_token: str,
        planning_errors: list[str],
        planning_rejections: list[str],
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
        elif planning_rejections:
            if not await self._store.mark_source_completed(
                source,
                claim_token,
                reason="Execution planning rejected: "
                + " | ".join(planning_rejections),
            ):
                raise RuntimeError("Source planning rejection could not be recorded")
        else:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )

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
        retry_candidates: list[tuple[TradingIntent, bool, datetime]],
    ) -> bool:
        try:
            finalized = await self._store.create_signal_batch_and_complete_source(
                planned,
                position_actions,
                claim_token,
                planning_candidates=retry_candidates,
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

    async def _dispatch_position_actions(
        self,
        position_actions: tuple[PositionActionIntent, ...],
        *,
        snapshot: AccountSnapshotDelivery,
    ) -> None:
        manual_cards_sent = 0

        for action in position_actions:
            if action.approval_mode is ApprovalMode.SKIPPED:
                # Resolve durably instead of leaving an eternal PENDING row:
                # the trader's lifecycle instruction has no matching live
                # position, so it can never become executable later.
                skipped = await self._store.mark_position_action_skipped(
                    action.action_id,
                    None,
                )
                if skipped:
                    logger.info(
                        "Position action %s %s skipped durably; "
                        "no matching live position",
                        action.action.value,
                        action.symbol,
                    )
                continue

            if action.approval_mode is ApprovalMode.AUTO:
                outcome = await self._coordinator.execute_position_action(
                    action.action_id,
                    approval_mode=(ApprovalMode.AUTO),
                )

                try:
                    await self.delivery.auto_outcome(
                        action.action_id, "ACTION", outcome
                    )
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
                sent = await self.delivery.manual(
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
