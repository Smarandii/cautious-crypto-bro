from __future__ import annotations

import asyncio
import logging

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .config import get_settings
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .llm_factory import build_intent_extractor
from .position_supervisor import PositionSupervisor
from .runtime_store import RedisRuntimeStore
from .service import SignalService
from .signal_context import SignalContextProvider
from .storage import IntentStore
from .telegram_source import TelegramSource


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
    logging.getLogger("httpx").setLevel(logging.WARNING)

    store = IntentStore(settings.database_path)
    await store.initialize()
    interrupted_intents, interrupted_actions = (
        await store.quarantine_interrupted_executions()
    )

    runtime_store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(settings.redis_max_connections),
        pool_timeout_seconds=(settings.redis_pool_timeout_seconds),
    )
    await runtime_store.initialize()

    extractor = build_intent_extractor(
        settings,
        evaluation_cache=runtime_store,
        provider_cooldown_store=runtime_store,
    )

    planner = ExecutionPlanner()

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=(settings.bybit_api_secret),
    )

    account_mutation_lock = asyncio.Lock()

    coordinator = ExecutionCoordinator(
        store=store,
        executor=executor,
        max_age_seconds=(settings.intent_max_age_seconds),
        execution_lock=(account_mutation_lock),
    )

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=(account_mutation_lock),
    )

    bot = ApprovalBot(
        token=settings.telegram_bot_token,
        approval_chat_id=(settings.telegram_approval_chat_id),
        approver_user_id=(settings.telegram_approver_user_id),
        store=store,
        coordinator=coordinator,
    )

    context_provider = SignalContextProvider(
        store=store,
        executor=executor,
    )

    service = SignalService(
        store=store,
        extractor=extractor,
        planner=planner,
        executor=executor,
        approval_bot=bot,
        coordinator=coordinator,
        context_provider=context_provider,
        auto_approval_mode=(settings.auto_approval_mode),
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
        await bot.start()
        try:
            await bot.send_recovery_warning(
                uncertain_intents=len(interrupted_intents),
                uncertain_actions=len(interrupted_actions),
            )
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to send interrupted-execution recovery warning"
            )

        # Quarantine is durable before the first supervisor reconciliation.
        # Fail startup if the resulting account-state reconciliation fails.
        await supervisor.reconcile_once()
        await service.recover_auto_execution()
        # AUTO recovery may open new positions; protect them before ingestion.
        await supervisor.reconcile_once()

        async with asyncio.TaskGroup() as tg:
            # Approval callbacks must already be active
            # while startup lookback is creating cards.
            tg.create_task(bot.run())
            tg.create_task(supervisor.run())

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
