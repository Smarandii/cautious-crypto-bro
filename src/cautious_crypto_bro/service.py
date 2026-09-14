from __future__ import annotations

import logging

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .domain import (
    ExecutionPlan,
    IncomingPost,
    TradingIntent,
)
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
            lease_seconds=self._source_processing_lease_seconds,
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

            intents = await self._extractor.extract(
                post,
                global_guidance=global_guidance,
                channel_guidance=channel_guidance,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Intent extraction failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if not intents:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )
            return

        try:
            policy = await self._store.get_execution_policy()
        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Execution policy load failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ] = []

        planning_errors: list[str] = []

        for intent in intents:
            try:
                context = await self._executor.market_context(intent.symbol)

                plan = self._planner.plan(
                    intent,
                    policy,
                    context,
                )

            except Exception as exc:
                error = f"{intent.symbol}: {type(exc).__name__}: {exc}"

                planning_errors.append(error)

                logger.exception(
                    "Execution planning failed for candidate %s from %s/%s",
                    intent.symbol,
                    source.channel_id,
                    source.message_id,
                )
                continue

            planned.append(
                (
                    intent,
                    plan,
                )
            )

        if not planned:
            await self._store.mark_source_failed(
                source,
                claim_token,
                (
                    "No extracted candidate could be planned: "
                    + " | ".join(planning_errors)
                ),
            )
            return

        if planning_errors:
            logger.warning(
                "Planning kept %d/%d candidate(s) for %s/%s; "
                "%d candidate(s) were omitted",
                len(planned),
                len(intents),
                source.channel_id,
                source.message_id,
                len(planning_errors),
            )

        account_state = None
        account_state_error = None

        try:
            account_state = await self._executor.account_state()

        except Exception as exc:
            account_state_error = f"{type(exc).__name__}: {exc}"

            # Account state is informational.
            # Failure must not discard valid signals.
            logger.exception(
                "Account-state check failed for %s/%s",
                source.channel_id,
                source.message_id,
            )

        try:
            finalized = await self._store.create_intents_with_plans_and_complete_source(
                planned,
                claim_token,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Intent batch persistence failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if not finalized:
            logger.warning(
                "Lost processing claim before intent batch persistence for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        for index, (intent, plan) in enumerate(planned):
            exposure = None

            if account_state is not None:
                exposure = account_state.exposure_for(intent.symbol)

            try:
                await self._approval_bot.send_intent(
                    intent,
                    plan,
                    exposure=exposure,
                    exposure_error=account_state_error,
                    account_state=account_state,
                    account_state_error=account_state_error,
                    send_account_state=(index == 0),
                )

            except Exception:
                # All intent/plan pairs are already durable.
                # Delivery retry/outbox remains separate debt.
                logger.exception(
                    "Approval delivery failed for persisted intent %s",
                    intent.intent_id,
                )
                continue

            logger.info(
                "Created trading intent %s with %d planned order(s) "
                "from %d candidate(s) in source post",
                intent.intent_id,
                len(plan.orders),
                len(intents),
            )
