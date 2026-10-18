from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import FrameType
from uuid import UUID

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .config import Settings, get_settings
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .llm_factory import build_intent_extractor
from .position_supervisor import PositionSupervisor
from .runtime_store import RedisRuntimeStore
from .service import SignalService
from .signal_context import SignalContextProvider
from .state_ownership import state_ownership
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

    loop = asyncio.get_running_loop()
    application = asyncio.current_task()
    assert application is not None
    shutdown_requested = False

    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        nonlocal shutdown_requested
        if not shutdown_requested:
            shutdown_requested = True
            loop.call_soon_threadsafe(application.cancel)

    previous_handler = signal.signal(signal.SIGTERM, request_shutdown)
    try:
        with state_ownership(settings.database_path):
            await _run_owned_application(settings)
    except asyncio.CancelledError:
        if not shutdown_requested:
            raise
        logging.getLogger(__name__).info("SIGTERM shutdown complete")
    finally:
        signal.signal(signal.SIGTERM, previous_handler)


async def _run_owned_application(settings: Settings) -> None:
    store = IntentStore(settings.database_path)
    await store.initialize()
    (
        interrupted_intents,
        interrupted_actions,
    ) = await store.quarantine_interrupted_executions()
    stale_sources = await store.reset_stale_processing_sources(
        settings.source_processing_lease_seconds,
    )
    if stale_sources:
        logging.getLogger(__name__).warning(
            "Reset %d stale source message(s) stuck in PROCESSING",
            stale_sources,
        )

    async with AsyncExitStack() as cleanup:
        runtime_store = await _build_runtime_store(settings, cleanup=cleanup)
        runtime = await _build_runtime(
            settings,
            store=store,
            runtime_store=runtime_store,
            cleanup=cleanup,
        )
        await _run_application(
            runtime,
            interrupted_intents=interrupted_intents,
            interrupted_actions=interrupted_actions,
        )


@dataclass(frozen=True, slots=True)
class _Runtime:
    """Every collaborator the process needs, wired once at startup."""

    bot: ApprovalBot
    supervisor: PositionSupervisor
    service: SignalService
    source: TelegramSource


async def _build_runtime_store(
    settings: Settings, *, cleanup: AsyncExitStack
) -> RedisRuntimeStore:
    runtime_store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(settings.redis_max_connections),
        pool_timeout_seconds=(settings.redis_pool_timeout_seconds),
    )
    cleanup.push_async_callback(runtime_store.close)
    await runtime_store.initialize()
    return runtime_store


async def _build_runtime(
    settings: Settings,
    *,
    store: IntentStore,
    runtime_store: RedisRuntimeStore,
    cleanup: AsyncExitStack,
) -> _Runtime:
    extractor = build_intent_extractor(
        settings,
        evaluation_cache=runtime_store,
        provider_cooldown_store=runtime_store,
    )
    cleanup.push_async_callback(extractor.close)

    planner = ExecutionPlanner()

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=(settings.bybit_api_secret),
        planner=planner,
    )
    cleanup.callback(executor.close)

    account_mutation_lock = asyncio.Lock()

    coordinator = ExecutionCoordinator(
        store=store,
        executor=executor,
        planner=planner,
        max_age_seconds=(settings.intent_max_age_seconds),
        execution_lock=(account_mutation_lock),
        portfolio_stop_risk_cap_usdt=(settings.demo_portfolio_stop_risk_cap_usdt),
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
        executor=coordinator,
    )
    cleanup.push_async_callback(bot.close)

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
        demo_long_risk_multiplier=(settings.demo_long_risk_multiplier),
        demo_long_exit_control_fraction=(settings.demo_long_exit_control_fraction),
        demo_long_participation_skip_fraction=(
            settings.demo_long_participation_skip_fraction
        ),
        demo_exit_profile=(settings.demo_exit_profile),
        demo_portfolio_stop_risk_cap_usdt=(settings.demo_portfolio_stop_risk_cap_usdt),
        demo_entry_order_ttl_minutes=(settings.demo_entry_order_ttl_minutes),
    )

    async def retry_message_ids(channel_id: int) -> tuple[int, ...]:
        return await store.get_retryable_source_message_ids(
            channel_id, lease_seconds=settings.source_processing_lease_seconds
        )

    source = TelegramSource(
        api_id=settings.telegram_api_id,
        api_hash=(settings.telegram_api_hash),
        session_name=(settings.telegram_session_name),
        channels=(settings.telegram_source_channels),
        on_message=(service.on_message),
        startup_lookback_hours=(settings.telegram_startup_lookback_hours),
        catchup_interval_seconds=(settings.telegram_catchup_interval_seconds),
        retry_message_ids=retry_message_ids,
    )
    cleanup.push_async_callback(source.disconnect)

    return _Runtime(
        bot=bot,
        supervisor=supervisor,
        service=service,
        source=source,
    )


async def _run_application(
    runtime: _Runtime,
    *,
    interrupted_intents: tuple[UUID, ...],
    interrupted_actions: tuple[UUID, ...],
) -> None:
    await runtime.bot.start()
    try:
        await runtime.bot.send_recovery_warning(
            uncertain_intents=len(interrupted_intents),
            uncertain_actions=len(interrupted_actions),
        )
    except Exception:
        logging.getLogger(__name__).exception(
            "Failed to send interrupted-execution recovery warning"
        )

    # Quarantine is durable before the first supervisor reconciliation.
    # Fail startup if the resulting account-state reconciliation fails.
    await runtime.supervisor.reconcile_once()
    await runtime.service.recover_auto_execution()
    # AUTO recovery may open new positions; protect them before ingestion.
    await runtime.supervisor.reconcile_once()

    async with asyncio.TaskGroup() as tg:
        # Approval callbacks must already be active
        # while startup lookback is creating cards.
        tg.create_task(runtime.bot.run())
        tg.create_task(runtime.bot.run_strategy_alert_delivery())
        tg.create_task(runtime.supervisor.run())
        tg.create_task(runtime.service.run_delivery_recovery())
        tg.create_task(runtime.service.run_periodic_account_pnl_sync())

        await runtime.source.start()

        tg.create_task(runtime.source.run_until_disconnected())
        tg.create_task(runtime.source.run_catchup())


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
