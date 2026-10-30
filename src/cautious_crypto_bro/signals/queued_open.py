from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from ..domain import (
    AccountSnapshotDelivery,
    ApprovalMode,
    AutoApprovalMode,
    ExecutionPlan,
    SignalExtraction,
    TradingIntent,
)
from ..ports import (
    ExecutionCoordinator,
    PlanningCandidateRepository,
    SignalContextProvider,
)
from .delivery import SignalDelivery
from .planning import SignalPlanning

logger = logging.getLogger(__name__)


class QueuedOpenStore(PlanningCandidateRepository, Protocol):
    async def mark_failed(self, intent_id: UUID, error: str) -> None: ...


class QueuedOpenExecution:
    """Own persisted OPEN dispatch and bounded candidate/restart recovery."""

    def __init__(
        self,
        *,
        store: QueuedOpenStore,
        planning: SignalPlanning,
        context_provider: SignalContextProvider,
        coordinator: ExecutionCoordinator,
        delivery: SignalDelivery,
        auto_approval_mode: AutoApprovalMode,
    ) -> None:
        self._store = store
        self._planning = planning
        self._context_provider = context_provider
        self._coordinator = coordinator
        self.delivery = delivery
        self._auto_approval_mode = auto_approval_mode

    async def recover(self) -> None:
        await self._recover_transferred()
        # Bound each pass so delivery recovery is not starved by a large backlog.
        for _ in range(10):
            candidate = await self._store.claim_planning_candidate()
            if candidate is None:
                return
            intent = candidate.intent
            if datetime.now(UTC) >= candidate.deadline:
                await self._store.finish_planning_candidate(
                    intent.intent_id,
                    candidate.claim_token,
                    status="EXPIRED",
                    error="Original source deadline exceeded",
                )
                continue
            try:
                context = await self._context_provider.snapshot(
                    intent.source.channel_id
                )
                planning = await self._planning.plan_open_intents(
                    SignalExtraction(open_intents=(intent,)),
                    context,
                    retain_retry_candidates=False,
                    original_duplicate=candidate.duplicate_in_batch,
                    original_manual=intent.approval_mode is ApprovalMode.MANUAL,
                )
                planned, errors, rejections = (
                    planning.planned,
                    planning.errors,
                    planning.rejections,
                )
                if not planned:
                    await self._store.finish_planning_candidate(
                        intent.intent_id,
                        candidate.claim_token,
                        status="REJECTED" if rejections else "PENDING",
                        error="; ".join(errors + rejections)
                        or "Portfolio admission unavailable",
                    )
                    continue
                recovered, plan = planned[0]
                if not await self._store.transfer_planning_candidate(
                    candidate, recovered, plan
                ):
                    continue
            except Exception as exc:
                await self._store.finish_planning_candidate(
                    intent.intent_id,
                    candidate.claim_token,
                    status="PENDING",
                    error=f"{type(exc).__name__}: {exc}",
                )
                logger.exception(
                    "Candidate planning retry failed for %s", intent.intent_id
                )
                continue
            # Once transferred, existing execution/uncertain-outcome recovery owns it.
            pnl, pnl_error = await self.delivery.stored_account_pnl()
            await self.dispatch(
                [(recovered, plan)],
                snapshot=AccountSnapshotDelivery(
                    state=context.account_state,
                    state_error=context.account_state_error,
                    pnl=pnl,
                    pnl_error=pnl_error,
                ),
            )

    async def _recover_transferred(self) -> None:
        for intent_id in await self._store.pending_transferred_auto_candidates():
            if self._auto_approval_mode is AutoApprovalMode.DISABLED:
                await self._store.mark_failed(
                    intent_id, "AUTO recovery cancelled: auto approval is disabled"
                )
                continue
            try:
                outcome = await self._coordinator.execute_intent(
                    intent_id, approval_mode=ApprovalMode.AUTO
                )
                await self.delivery.auto_outcome(intent_id, "OPEN", outcome)
            except Exception:
                logger.exception(
                    "Transferred candidate recovery failed for %s", intent_id
                )

    async def dispatch(
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
                    await self.delivery.auto_outcome(intent.intent_id, "OPEN", outcome)
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
                sent = await self.delivery.manual(
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
