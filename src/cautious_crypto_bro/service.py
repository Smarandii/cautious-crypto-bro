from __future__ import annotations

import logging

from .approval_bot import ApprovalBot
from .domain import IncomingPost
from .openrouter import OpenRouterIntentExtractor
from .storage import IntentStore

logger = logging.getLogger(__name__)


class SignalService:
    def __init__(
        self,
        *,
        store: IntentStore,
        extractor: OpenRouterIntentExtractor,
        approval_bot: ApprovalBot,
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._approval_bot = approval_bot

    async def on_message(
        self,
        post: IncomingPost,
    ) -> None:
        source = post.source

        if not await self._store.save_source(source):
            logger.debug(
                "Duplicate Telegram message %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        try:
            global_guidance, channel_guidance = (
                await self._store.get_guidance(
                    source.channel_id
                )
            )

            intent = await self._extractor.extract(
                post,
                global_guidance=global_guidance,
                channel_guidance=channel_guidance,
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

        await self._store.create_intent(intent)
        await self._approval_bot.send_intent(intent)

        logger.info(
            "Created trading intent %s",
            intent.intent_id,
        )
