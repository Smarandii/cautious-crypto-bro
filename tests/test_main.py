import asyncio
import signal
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.integration


def _fake_application(monkeypatch, tmp_path, failed_reconciliation=None):
    app = import_module("cautious_crypto_bro.main")
    events: list[str] = []

    def step(name: str):
        async def invoke() -> None:
            events.append(name)
            if name == "supervisor.reconcile" and failed_reconciliation == events.count(
                name
            ):
                raise RuntimeError("Initial account state unavailable")

        return invoke

    async def quarantine():
        events.append("store.quarantine")
        return (), ()

    async def reset_stale(_lease_seconds: int):
        events.append("store.reset_stale")
        return 0

    store = SimpleNamespace(
        get_source_history_cursor=AsyncMock(return_value=None),
        save_source_history_cursor=AsyncMock(),
        source_completed=AsyncMock(return_value=True),
        pending_recovery_notices=AsyncMock(return_value=((), ())),
        acknowledge_recovery_notices=AsyncMock(),
        initialize=step("store.initialize"),
        quarantine_interrupted_executions=quarantine,
        reset_stale_processing_sources=reset_stale,
    )
    runtime_store = SimpleNamespace(
        initialize=step("runtime.initialize"),
        close=step("runtime.close"),
    )
    extractor = SimpleNamespace(close=step("extractor.close"))

    def close_executor():
        with pytest.raises(RuntimeError, match="already owned"):
            with app.state_ownership(settings.database_path):
                pytest.fail("Ownership was released before exchange client cleanup")
        events.append("executor.close")

    executor = SimpleNamespace(close=close_executor)
    supervisor = SimpleNamespace(
        reconcile_once=step("supervisor.reconcile"),
        run=step("supervisor.run"),
    )

    async def warning(**kwargs) -> None:
        events.append("bot.recovery_warning")

    bot = SimpleNamespace(
        report_supervision_state=AsyncMock(),
        set_startup_blocked=lambda reason: events.append(
            "bot.ready" if reason is None else "bot.blocked"
        ),
        start=step("bot.start"),
        send_recovery_warning=warning,
        run=step("bot.run"),
        run_strategy_alert_delivery=step("bot.strategy_alerts"),
        close=step("bot.close"),
    )
    service = SimpleNamespace(
        run_delivery_recovery=step("manual.recover"),
        run_periodic_account_pnl_sync=step("pnl.sync"),
        recover_auto_execution=step("service.recover"),
        on_message=AsyncMock(),
    )
    source = SimpleNamespace(
        start=step("source.start"),
        run_until_disconnected=step("source.run"),
        run_catchup=step("source.catchup"),
        disconnect=step("source.disconnect"),
    )

    async def source_worker():
        await source.start()
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(source.run_catchup())
            await source.run_until_disconnected()

    monkeypatch.setattr(
        app, "SourceRecovery", lambda *a, **kw: SimpleNamespace(run=source_worker)
    )
    bot.bind_source_recovery = lambda recovery: setattr(
        bot, "_source_recovery", recovery
    )
    settings = SimpleNamespace(
        log_level="INFO",
        database_path=tmp_path / "state.db",
        redis_url="redis://localhost",
        redis_max_connections=1,
        redis_pool_timeout_seconds=1,
        bybit_api_key="demo-key",
        bybit_api_secret="demo-secret",
        intent_max_age_seconds=300,
        telegram_bot_token="test-token",
        telegram_approval_chat_id=1,
        telegram_approver_user_id=1,
        auto_approval_mode="all",
        source_processing_lease_seconds=300,
        demo_long_risk_multiplier=1,
        demo_long_exit_control_fraction=0,
        demo_long_participation_skip_fraction=0,
        demo_long_momentum_gate=False,
        demo_portfolio_stop_risk_cap_usdt=340,
        demo_exit_profile="baseline",
        demo_entry_order_ttl_minutes=0,
        telegram_api_id=1,
        telegram_api_hash="test-hash",
        telegram_session_name="test",
        telegram_source_channels=(),
        telegram_startup_lookback_hours=1,
        telegram_catchup_interval_seconds=60,
    )

    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "IntentStore", lambda *_: store)
    monkeypatch.setattr(app, "RedisRuntimeStore", lambda *_, **__: runtime_store)
    monkeypatch.setattr(app, "build_intent_extractor", lambda *_, **__: extractor)
    monkeypatch.setattr(app, "ExecutionPlanner", lambda: object())
    monkeypatch.setattr(app, "BybitDemoExecutor", lambda **_: executor)
    monkeypatch.setattr(app, "ExecutionCoordinator", lambda **_: object())
    monkeypatch.setattr(app, "PositionSupervisor", lambda **_: supervisor)
    monkeypatch.setattr(app, "ApprovalBot", lambda **_: bot)
    monkeypatch.setattr(app, "SignalContextProvider", lambda **_: object())
    monkeypatch.setattr(app, "SignalService", lambda **_: service)
    monkeypatch.setattr(app, "TelegramSource", lambda **_: source)
    return SimpleNamespace(
        app=app,
        events=events,
        settings=settings,
        runtime_store=runtime_store,
        extractor=extractor,
        executor=executor,
        bot=bot,
        source=source,
        service=service,
        supervisor=supervisor,
    )


@pytest.mark.parametrize("failed_reconciliation", [None, 1, 2])
def test_initial_reconciliation_precedes_any_execution(
    monkeypatch: pytest.MonkeyPatch,
    failed_reconciliation: int | None,
    tmp_path,
) -> None:
    fake = _fake_application(monkeypatch, tmp_path, failed_reconciliation)
    app, events, settings = fake.app, fake.events, fake.settings
    from cautious_crypto_bro.supervision_health import (
        publish_success,
        supervision_health_path,
    )

    progress = supervision_health_path(settings.database_path)
    publish_success(progress)
    store = app.IntentStore(settings.database_path)
    initialize = store.initialize

    async def initialize_without_stale_progress():
        assert not progress.exists()
        await initialize()

    store.initialize = initialize_without_stale_progress
    start = fake.source.start

    async def source_start_with_progress():
        publish_success(progress)
        await start()

    fake.source.start = source_start_with_progress

    if failed_reconciliation is not None:
        with pytest.raises(ExceptionGroup) as caught:
            asyncio.run(app.async_main())
        assert len(caught.value.exceptions) == 1
        assert str(caught.value.exceptions[0]) == "Initial account state unavailable"

        assert events.count("supervisor.reconcile") == failed_reconciliation
        assert events.index("store.quarantine") < events.index("store.reset_stale")
        assert events.index("store.reset_stale") < events.index("supervisor.reconcile")
        assert ("service.recover" in events) is (failed_reconciliation == 2)
        assert "bot.run" not in events
        assert "source.start" not in events
    else:
        asyncio.run(app.async_main())

        assert events.index("store.quarantine") < events.index("store.reset_stale")
        assert events.index("store.reset_stale") < events.index("supervisor.reconcile")
        assert "bot.recovery_warning" not in events
        assert events.index("supervisor.reconcile") < events.index("service.recover")
        assert events.count("supervisor.reconcile") == 2
        assert events.index("service.recover") < events.index("source.start")
        assert events.index(
            "supervisor.reconcile", events.index("service.recover")
        ) < events.index("source.start")
        assert events.index("bot.blocked") < events.index("bot.ready")
        assert events.index("service.recover") < events.index("bot.ready")
        assert events.index("service.recover") < events.index("supervisor.run")
        assert "pnl.sync" in events
        # The catch-up poll must run alongside the live listener, or
        # dropped Telegram pushes stay dropped.
        assert "source.catchup" in events
        assert events.index("source.start") < events.index("source.catchup")

    assert events[-5:] == [
        "source.disconnect",
        "bot.close",
        "executor.close",
        "extractor.close",
        "runtime.close",
    ]
    # Startup failures and normal shutdown both release ownership after cleanup.
    assert not progress.exists()
    with app.state_ownership(settings.database_path):
        pass


@pytest.mark.parametrize(
    ("failed_at", "closed"),
    [
        ("redis_initialize", ["runtime.close"]),
        ("extractor", ["runtime.close"]),
        ("executor", ["extractor.close", "runtime.close"]),
        ("source", ["bot.close", "executor.close", "extractor.close", "runtime.close"]),
    ],
)
def test_partial_startup_closes_every_acquired_client(
    monkeypatch, tmp_path, failed_at, closed
):
    fake = _fake_application(monkeypatch, tmp_path)
    failure = RuntimeError("construction failed")

    def fail(*args, **kwargs):
        raise failure

    if failed_at == "redis_initialize":
        fake.runtime_store.initialize = AsyncMock(side_effect=failure)
    else:
        constructor = {
            "extractor": "build_intent_extractor",
            "executor": "BybitDemoExecutor",
            "source": "TelegramSource",
        }[failed_at]
        monkeypatch.setattr(fake.app, constructor, fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(fake.app.async_main())
    assert caught.value is failure
    assert [item for item in fake.events if item.endswith(".close")] == closed
    assert "bot.start" not in fake.events
    with fake.app.state_ownership(fake.settings.database_path):
        pass


@pytest.mark.parametrize("listener_fails", [False, True])
def test_reader_outage_keeps_operator_and_protection_alive_until_shutdown(
    monkeypatch, tmp_path, listener_fails
):
    from cautious_crypto_bro.source_recovery import SourceRecovery
    from cautious_crypto_bro.supervision_health import (
        publish_success,
        supervision_health_path,
    )
    from cautious_crypto_bro.telegram_source import TelegramSource

    fake = _fake_application(monkeypatch, tmp_path)
    monkeypatch.setattr(fake.app, "SourceRecovery", SourceRecovery)
    fake.source.session_user_id = AsyncMock(return_value=1)
    fake.source.read_warnings = lambda: ()
    notified = asyncio.Event()

    async def notice(text):
        fake.events.append("source.alert")
        notified.set()

    fake.bot.send_source_notice = notice
    start = fake.source.start

    async def source_start():
        publish_success(supervision_health_path(fake.settings.database_path))
        await start()

    fake.source.start = source_start

    async def background():
        try:
            await asyncio.Event().wait()
        finally:
            fake.events.append("background.stopped")

    async def disconnected():
        fake.events.append("source.disconnected")
        if listener_fails:
            raise RuntimeError("synthetic listener failure")

    # Keep the actual source adapter; replace only its Telegram client boundary.
    client = SimpleNamespace(run_until_disconnected=disconnected)

    async def listener():
        await TelegramSource.run_until_disconnected(SimpleNamespace(_client=client))

    fake.source.run_until_disconnected = listener
    fake.source.run_catchup = background
    fake.bot.run = background
    fake.bot.run_strategy_alert_delivery = background
    fake.supervisor.run = background
    service = fake.service
    service.run_delivery_recovery = background
    service.run_periodic_account_pnl_sync = background

    async def run():
        task = asyncio.create_task(fake.app.async_main())
        try:
            await asyncio.wait_for(notified.wait(), timeout=2)
            assert not task.done()
            assert fake.bot._source_recovery.state == "OFFLINE"
            assert supervision_health_path(fake.settings.database_path).exists()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
    assert fake.events.count("background.stopped") >= 5
    assert max(
        i for i, event in enumerate(fake.events) if event == "background.stopped"
    ) < max(i for i, event in enumerate(fake.events) if event == "source.disconnect")
    assert fake.events[-5:] == [
        "source.disconnect",
        "bot.close",
        "executor.close",
        "extractor.close",
        "runtime.close",
    ]
    assert not supervision_health_path(fake.settings.database_path).exists()
    with fake.app.state_ownership(fake.settings.database_path):
        pass


def test_cleanup_attempts_all_clients_and_preserves_failure_chain(
    monkeypatch, tmp_path
):
    fake = _fake_application(monkeypatch, tmp_path, failed_reconciliation=1)

    async def disconnect():
        fake.events.append("source.disconnect")
        raise RuntimeError("source close failed")

    async def close_extractor():
        fake.events.append("extractor.close")
        raise RuntimeError("extractor close failed")

    fake.source.disconnect = disconnect
    fake.extractor.close = close_extractor
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(fake.app.async_main())
    assert fake.events[-5:] == [
        "source.disconnect",
        "bot.close",
        "executor.close",
        "extractor.close",
        "runtime.close",
    ]
    failures = []
    error = caught.value
    while error is not None:
        failures.append(str(error))
        error = error.__context__
    assert failures == [
        "extractor close failed",
        "source close failed",
        "unhandled errors in a TaskGroup (1 sub-exception)",
        "Initial account state unavailable",
    ]
    assert isinstance(error := caught.value.__context__.__context__, ExceptionGroup)
    assert str(error.exceptions[0]) == "Initial account state unavailable"
    with fake.app.state_ownership(fake.settings.database_path):
        pass


def test_sigterm_cleans_partial_startup_and_restores_handler(monkeypatch, tmp_path):
    fake = _fake_application(monkeypatch, tmp_path)
    previous = signal.getsignal(signal.SIGTERM)

    async def terminate():
        signal.raise_signal(signal.SIGTERM)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.Event().wait()

    fake.bot.start = terminate
    asyncio.run(fake.app.async_main())
    assert fake.events[-5:] == [
        "source.disconnect",
        "bot.close",
        "executor.close",
        "extractor.close",
        "runtime.close",
    ]
    assert signal.getsignal(signal.SIGTERM) == previous
    with fake.app.state_ownership(fake.settings.database_path):
        pass


def test_unrequested_cancellation_propagates_after_cleanup(monkeypatch, tmp_path):
    fake = _fake_application(monkeypatch, tmp_path)
    previous = signal.getsignal(signal.SIGTERM)
    fake.bot.start = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fake.app.async_main())
    assert fake.events[-1] == "runtime.close"
    assert signal.getsignal(signal.SIGTERM) == previous
