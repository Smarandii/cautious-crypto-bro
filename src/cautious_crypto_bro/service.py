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
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._planner = planner
        self._executor = executor
        self._approval_bot = (
            approval_bot
        )

    async def on_message(
        self,
        post: IncomingPost,
    ) -> None:
        source = post.source

        if not await self._store.save_source(
            source
        ):
            logger.debug(
                "Duplicate Telegram message %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        try:
            (
                global_guidance,
                channel_guidance,
            ) = await self._store.get_guidance(
                source.channel_id
            )

            intent = (
                await self._extractor.extract(
                    post,
                    global_guidance=(
                        global_guidance
                    ),
                    channel_guidance=(
                        channel_guidance
                    ),
                )
            )

        except Exception:
            logger.exception(
                "Intent extraction failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if intent is None:
            return

        try:
            policy = (
                await self._store
                .get_execution_policy()
            )

            context = (
                await self._executor
                .market_context(
                    intent.symbol
                )
            )

            plan = self._planner.plan(
                intent,
                policy,
                context,
            )

        except Exception:
            logger.exception(
                "Execution planning failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        await self._store.create_intent_with_plan(
            intent,
            plan,
        )

        await self._approval_bot.send_intent(
            intent,
            plan,
        )

        logger.info(
            "Created trading intent %s "
            "with %d planned order(s)",
            intent.intent_id,
            len(plan.orders),
        )
