from __future__ import annotations

import logging

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .domain import IncomingPost
from .execution import ExecutionPlanner
from .openrouter import (
    OpenRouterIntentExtractor,
)
from .storage import IntentStore

logger = logging.getLogger(__name__)


class SignalService:
    def __init__(
        self,
        *,
        store: IntentStore,
        extractor: OpenRouterIntentExtractor,
        planner: ExecutionPlanner,
        executor: BybitDemoExecutor,
        approval_bot: ApprovalBot,
        source_processing_lease_seconds: int = 300,
    ) -> None:
        if source_processing_lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")

        self._store = store
        self._extractor = extractor
        self._planner = planner
        self._executor = executor
        self._approval_bot = approval_bot
        self._source_processing_lease_seconds = source_processing_lease_seconds

    async def on_message(
        self,
        post: IncomingPost,
    ) -> None:
        source = post.source

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
            return

        try:
            (
                global_guidance,
                channel_guidance,
            ) = await self._store.get_guidance(source.channel_id)

            intent = await self._extractor.extract(
                post,
                global_guidance=(global_guidance),
                channel_guidance=(channel_guidance),
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                (f"{type(exc).__name__}: {exc}"),
            )

            logger.exception(
                "Intent extraction failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if intent is None:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )
            return

        try:
            policy = await self._store.get_execution_policy()

            context = await self._executor.market_context(intent.symbol)

            plan = self._planner.plan(
                intent,
                policy,
                context,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                (f"{type(exc).__name__}: {exc}"),
            )

            logger.exception(
                "Execution planning failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        account_state = None
        account_state_error = None
        exposure = None
        exposure_error = None

        try:
            account_state = await self._executor.account_state()

            exposure = account_state.exposure_for(intent.symbol)

        except Exception as exc:
            account_state_error = f"{type(exc).__name__}: {exc}"
            exposure_error = account_state_error

            # Account state is informational.
            # Failure must not discard a valid signal.
            logger.exception(
                "Account-state check failed for %s/%s",
                source.channel_id,
                source.message_id,
            )

        try:
            finalized = await self._store.create_intent_with_plan_and_complete_source(
                intent,
                plan,
                claim_token,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                (f"{type(exc).__name__}: {exc}"),
            )

            logger.exception(
                "Intent persistence failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if not finalized:
            logger.warning(
                "Lost processing claim before intent persistence for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        try:
            await self._approval_bot.send_intent(
                intent,
                plan,
                exposure=exposure,
                exposure_error=(exposure_error),
                account_state=(account_state),
                account_state_error=(account_state_error),
            )
        except Exception:
            # The durable intent/plan already exists.
            # Retrying the whole source here could create
            # duplicate intents. Approval delivery needs
            # its own retry/outbox mechanism.
            logger.exception(
                "Approval delivery failed for persisted intent %s",
                intent.intent_id,
            )
            return

        logger.info(
            "Created trading intent %s with %d planned order(s)",
            intent.intent_id,
            len(plan.orders),
        )
