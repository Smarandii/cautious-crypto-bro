"""Durable manual approvals and AUTO notices share one delivery lifecycle."""

from __future__ import annotations

import logging
from typing import Protocol
from uuid import UUID

from ..domain import (
    AccountPnlSummary,
    AccountStateSummary,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    TradingIntent,
)
from ..ports import ApprovalSender, AutoOutcomeRepository, ManualDeliveryRepository

logger = logging.getLogger(__name__)


class DeliveryStore(ManualDeliveryRepository, AutoOutcomeRepository, Protocol):
    async def get_intent(self, intent_id: UUID) -> TradingIntent | None: ...

    async def get_execution_plan(self, intent_id: UUID) -> ExecutionPlan | None: ...

    async def get_position_action(
        self, action_id: UUID
    ) -> PositionActionIntent | None: ...

    async def get_account_pnl_summary(self) -> AccountPnlSummary | None: ...


class DeliveryExchange(Protocol):
    async def account_state(self) -> AccountStateSummary: ...


class SignalDelivery:
    def __init__(
        self,
        *,
        store: DeliveryStore,
        executor: DeliveryExchange,
        approval_bot: ApprovalSender,
    ) -> None:
        self._store = store
        self._executor = executor
        self._approval_bot = approval_bot

    async def manual(
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

    async def recover_manual(self) -> None:
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
                    await self.manual(signal, plan)
            else:
                action = await self._store.get_position_action(record_id)
                if action is not None and action.status is IntentStatus.PENDING:
                    await self.manual(action)

    async def recover_auto_outcomes(self) -> None:
        for record_id, kind in await self._store.pending_auto_outcome_deliveries():
            try:
                await self.auto_outcome(record_id, kind)
            except Exception:
                logger.exception("AUTO outcome retry failed for %s", record_id)

    async def _send_auto_preview(self) -> None:
        # Refresh exposure after execution; accounting uses the periodic ledger.
        state = None
        state_error = None
        try:
            state = await self._executor.account_state()
        except Exception as exc:
            state_error = f"{type(exc).__name__}: {exc}"
        pnl, pnl_error = await self.stored_account_pnl()
        try:
            await self._approval_bot.send_account_snapshot(
                state, state_error=state_error, pnl=pnl, pnl_error=pnl_error
            )
        except Exception:
            logger.exception("AUTO account preview delivery failed")

    async def _send_auto_outcome(
        self,
        outcome: IntentExecutionOutcome | PositionActionExecutionOutcome,
    ) -> None:
        if not outcome.recovered:
            await self._send_auto_preview()
        if isinstance(outcome, IntentExecutionOutcome):
            await self._approval_bot.send_auto_intent_outcome(outcome)
        else:
            await self._approval_bot.send_auto_action_outcome(outcome)

    async def auto_outcome(
        self,
        record_id: UUID,
        kind: str,
        outcome: IntentExecutionOutcome | PositionActionExecutionOutcome | None = None,
    ) -> bool:
        if outcome is not None and outcome.status in {
            IntentStatus.PENDING,
            IntentStatus.EXECUTING,
        }:
            return False
        # Legacy table/method names also serve AUTO terminal notices, not just cards.
        token = await self._store.claim_manual_delivery(record_id)
        if token is None:
            return False
        try:
            if outcome is None:
                outcome = await self._store.get_recorded_auto_outcome(record_id, kind)
            if outcome is None:
                raise RuntimeError("Terminal AUTO outcome no longer available")
            await self._send_auto_outcome(outcome)
        except Exception:
            await self._store.finish_manual_delivery(record_id, token, delivered=False)
            raise
        await self._store.finish_manual_delivery(record_id, token, delivered=True)
        return True

    async def stored_account_pnl(
        self,
    ) -> tuple[AccountPnlSummary | None, str | None]:
        try:
            return await self._store.get_account_pnl_summary(), None

        except Exception as exc:
            logger.exception("Stored account P&L preview unavailable")
            return None, f"{type(exc).__name__}: {exc}"
