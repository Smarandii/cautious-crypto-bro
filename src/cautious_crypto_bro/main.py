from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import AsyncExitStack
from dataclasses import dataclass
from time import monotonic
from types import FrameType

from .approval_bot import BYBIT_IP_BINDING_HELP, ApprovalBot
from .bybit import BybitDemoExecutor
from .config import Settings, get_settings
from .domain import TradeExecutionError
from .execution import ExecutionPlanner
from .execution_coordinator import ExecutionCoordinator
from .llm_factory import build_intent_extractor
from .position_supervisor import PositionSupervisor
from .runtime_store import RedisRuntimeStore
from .service import SignalService
from .signal_context import SignalContextProvider
from .source_recovery import SourceRecovery
from .state_ownership import state_ownership
from .storage import IntentStore
from .supervision_health import supervision_health_lifecycle, supervision_health_path
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
        with (
            state_ownership(settings.database_path),
            supervision_health_lifecycle(
                supervision_health_path(settings.database_path)
            ),
        ):
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
    await store.quarantine_interrupted_executions()
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
        await _run_application(runtime)


@dataclass(frozen=True, slots=True)
class _Runtime:
    """Every collaborator the process needs, wired once at startup."""

    store: IntentStore
    bot: ApprovalBot
    supervisor: PositionSupervisor
    service: SignalService
    source: TelegramSource
    source_recovery: SourceRecovery


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

    bot = ApprovalBot(
        token=settings.telegram_bot_token,
        approval_chat_id=(settings.telegram_approval_chat_id),
        approver_user_id=(settings.telegram_approver_user_id),
        store=store,
        executor=coordinator,
    )
    cleanup.push_async_callback(bot.close)

    supervisor = PositionSupervisor(
        store=store,
        executor=executor,
        mutation_lock=(account_mutation_lock),
        progress_path=supervision_health_path(settings.database_path),
        report_state=bot.report_supervision_state,
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

    async def defer_unavailable_messages(
        channel_id: int, message_ids: tuple[int, ...]
    ) -> None:
        await store.defer_unavailable_source_messages(
            channel_id,
            message_ids,
            lease_seconds=settings.source_processing_lease_seconds,
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
        defer_unavailable_messages=defer_unavailable_messages,
    )
    cleanup.push_async_callback(source.disconnect)
    source_recovery = SourceRecovery(
        source,
        bot,
        settings.database_path.with_suffix(".telegram-identity.json"),
        settings.telegram_approver_user_id,
    )
    bot.bind_source_recovery(source_recovery)

    return _Runtime(
        store=store,
        bot=bot,
        supervisor=supervisor,
        service=service,
        source=source,
        source_recovery=source_recovery,
    )


async def _run_application(runtime: _Runtime) -> None:
    runtime.bot.set_startup_blocked(
        "Startup reconciliation pending. Trading callbacks and signal ingestion are paused."
    )
    await runtime.bot.start()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(_deliver_recovery_warning(runtime))
        tg.create_task(runtime.bot.run())
        # Operator polling is live, but every trading callback remains gated.
        # Grug, Chesterton's Fence (faithful paraphrase): preserve the existing
        # reconciliation-before-ingestion boundary while restoring operator access.
        # Quarantine is durable before the first supervisor reconciliation.
        outage, next_alert = await _reconcile_before_ingestion(runtime)
        await runtime.service.recover_auto_execution()
        # AUTO recovery may open new positions; protect them before ingestion.
        second_outage, _ = await _reconcile_before_ingestion(
            runtime, next_alert=next_alert
        )
        runtime.bot.set_startup_blocked(None)
        if outage or second_outage:
            await _startup_notice(
                runtime,
                "Exchange startup reconciliation recovered. Trading and signal ingestion may resume.",
            )
        tg.create_task(runtime.bot.run_strategy_alert_delivery())
        tg.create_task(runtime.supervisor.run())
        tg.create_task(runtime.service.run_delivery_recovery())
        tg.create_task(runtime.service.run_periodic_account_pnl_sync())

        tg.create_task(runtime.source_recovery.run())


async def _deliver_recovery_warning(
    runtime: _Runtime,
    *,
    retry_seconds: float = 30,
) -> None:
    while True:
        try:
            intent_ids, action_ids = await runtime.store.pending_recovery_notices()
            if not intent_ids and not action_ids:
                return
            await runtime.bot.send_recovery_warning(
                uncertain_intents=len(intent_ids),
                uncertain_actions=len(action_ids),
            )
            await runtime.store.acknowledge_recovery_notices(intent_ids, action_ids)
            return
        except Exception:
            logging.getLogger(__name__).exception(
                "Failed to send interrupted-execution recovery warning; retry scheduled"
            )
        await asyncio.sleep(retry_seconds)


async def _startup_notice(runtime: _Runtime, text: str) -> bool:
    try:
        await runtime.bot.send_source_notice(text)
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to send exchange startup notice: %s", type(exc).__name__
        )
        return False
    return True


async def _reconcile_before_ingestion(
    runtime: _Runtime,
    *,
    retry_seconds: float = 30,
    alert_seconds: float = 900,
    next_alert: float = 0,
) -> tuple[bool, float]:
    delay = retry_seconds
    outage = False
    while True:
        try:
            await runtime.supervisor.reconcile_once()
            return outage, next_alert
        except TradeExecutionError:
            outage = True
            notice = (
                "Exchange startup reconciliation unavailable. Trading callbacks and "
                "signal ingestion are paused; application protection is not verified. "
                "Check Bybit API key IP binding, permissions, and connectivity. "
                "Account reconciliation will retry automatically, up to five minutes apart."
                "\n" + BYBIT_IP_BINDING_HELP
            )
            runtime.bot.set_startup_blocked(notice)
            logging.getLogger(__name__).warning(
                "Exchange startup reconciliation unavailable; retry in %s seconds",
                delay,
            )
            if monotonic() >= next_alert and await _startup_notice(runtime, notice):
                next_alert = monotonic() + alert_seconds
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
