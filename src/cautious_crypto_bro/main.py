from __future__ import annotations

import asyncio
import logging

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .config import get_settings
from .execution import ExecutionPlanner
from .openrouter import (
    OpenRouterIntentExtractor,
)
from .runtime_store import (
    RedisRuntimeStore,
)
from .service import SignalService
from .storage import IntentStore
from .telegram_source import (
    TelegramSource,
)


async def async_main() -> None:
    settings = get_settings()

    logging.basicConfig(
        level=getattr(
            logging,
            settings.log_level.upper(),
            logging.INFO,
        ),
        format=("%(asctime)s %(levelname)s %(name)s: %(message)s"),
    )

    store = IntentStore(settings.database_path)
    await store.initialize()

    runtime_store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(settings.redis_max_connections),
        pool_timeout_seconds=(settings.redis_pool_timeout_seconds),
    )
    await runtime_store.initialize()

    extractor = OpenRouterIntentExtractor(
        api_key=(settings.openrouter_api_key),
        model=(settings.openrouter_model),
        base_url=(settings.openrouter_base_url),
        inference_timeout_seconds=(settings.openrouter_inference_timeout_seconds),
        max_attempts=(settings.openrouter_inference_max_attempts),
        provider_cooldown_store=(runtime_store),
        provider_cooldown_seconds=(
            settings.openrouter_provider_cooldown_hours * 60 * 60
        ),
        evaluation_cache=(runtime_store),
        evaluation_cache_seconds=(settings.openrouter_evaluation_cache_hours * 60 * 60),
    )

    planner = ExecutionPlanner()

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=(settings.bybit_api_secret),
    )

    bot = ApprovalBot(
        token=settings.telegram_bot_token,
        approval_chat_id=(settings.telegram_approval_chat_id),
        approver_user_id=(settings.telegram_approver_user_id),
        max_age_seconds=(settings.intent_max_age_seconds),
        store=store,
        executor=executor,
    )

    await bot.start()

    service = SignalService(
        store=store,
        extractor=extractor,
        planner=planner,
        executor=executor,
        approval_bot=bot,
        source_processing_lease_seconds=(settings.source_processing_lease_seconds),
    )

    source = TelegramSource(
        api_id=settings.telegram_api_id,
        api_hash=(settings.telegram_api_hash),
        session_name=(settings.telegram_session_name),
        channels=(settings.telegram_source_channels),
        on_message=(service.on_message),
        startup_lookback_hours=(settings.telegram_startup_lookback_hours),
    )

    try:
        async with asyncio.TaskGroup() as tg:
            # Approval callbacks must already be active
            # while startup lookback is creating cards.
            tg.create_task(bot.run())

            await source.start()

            tg.create_task(source.run_until_disconnected())
    finally:
        await source.disconnect()
        await bot.close()
        await extractor.close()
        await runtime_store.close()
        executor.close()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
