from __future__ import annotations

import asyncio
import logging
from typing import Protocol
from uuid import UUID

from ..domain import ApprovalMode, AutoApprovalMode
from ..ports import ExecutionCoordinator
from .delivery import SignalDelivery
from .queued_open import QueuedOpenExecution

# Retain the existing namespace for recovery log filters and operator diagnostics.
logger = logging.getLogger("cautious_crypto_bro.service")


class RecoveryStore(Protocol):
    async def get_pending_auto_intent_ids(self) -> tuple[UUID, ...]: ...

    async def get_pending_auto_action_ids(self) -> tuple[UUID, ...]: ...

    async def ensure_auto_outcome_delivery(
        self, record_id: UUID, kind: str
    ) -> None: ...

    async def mark_failed(self, intent_id: UUID, error: str) -> None: ...

    async def mark_position_action_failed(
        self, action_id: UUID, error: str
    ) -> None: ...


class SignalRecovery:
    """Own startup backlog policy and periodic recovery orchestration."""

    def __init__(
        self,
        *,
        store: RecoveryStore,
        coordinator: ExecutionCoordinator,
        queued_opens: QueuedOpenExecution,
        delivery: SignalDelivery,
        auto_approval_mode: AutoApprovalMode,
    ) -> None:
        self._store = store
        self._coordinator = coordinator
        self.queued_opens = queued_opens
        self.delivery = delivery
        self._auto_approval_mode = auto_approval_mode

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
