import asyncio
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("failed_reconciliation", [None, 1, 2])
def test_initial_reconciliation_precedes_any_execution(
    monkeypatch: pytest.MonkeyPatch,
    failed_reconciliation: int | None,
) -> None:
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

    async def quarantine() -> tuple[tuple[()], tuple[()]]:
        events.append("store.quarantine")
        return (), ()

    store = SimpleNamespace(
        initialize=step("store.initialize"),
        quarantine_interrupted_executions=quarantine,
    )
    runtime_store = SimpleNamespace(
        initialize=step("runtime.initialize"),
        close=step("runtime.close"),
    )
    extractor = SimpleNamespace(close=step("extractor.close"))
    executor = SimpleNamespace(close=lambda: events.append("executor.close"))
    supervisor = SimpleNamespace(
        reconcile_once=step("supervisor.reconcile"),
        run=step("supervisor.run"),
    )
    async def warning(**kwargs) -> None:
        events.append("bot.recovery_warning")

    bot = SimpleNamespace(
        start=step("bot.start"),
        send_recovery_warning=warning,
        run=step("bot.run"),
        close=step("bot.close"),
    )
    service = SimpleNamespace(
        recover_auto_execution=step("service.recover"),
        on_message=AsyncMock(),
    )
    source = SimpleNamespace(
        start=step("source.start"),
        run_until_disconnected=step("source.run"),
        disconnect=step("source.disconnect"),
    )
    settings = SimpleNamespace(
        log_level="INFO",
        database_path=":memory:",
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
        telegram_api_id=1,
        telegram_api_hash="test-hash",
        telegram_session_name="test",
        telegram_source_channels=(),
        telegram_startup_lookback_hours=1,
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

    if failed_reconciliation is not None:
        with pytest.raises(RuntimeError, match="Initial account state"):
            asyncio.run(app.async_main())

        assert events.count("supervisor.reconcile") == failed_reconciliation
        assert events.index("store.quarantine") < events.index("supervisor.reconcile")
        assert ("service.recover" in events) is (failed_reconciliation == 2)
        assert "bot.run" not in events
        assert "source.start" not in events
    else:
        asyncio.run(app.async_main())

        assert events.index("store.quarantine") < events.index("supervisor.reconcile")
        assert events.index("bot.recovery_warning") < events.index("supervisor.reconcile")
        assert events.index("supervisor.reconcile") < events.index("service.recover")
        assert events.count("supervisor.reconcile") == 2
        assert events.index("service.recover") < events.index("source.start")
        assert events.index(
            "supervisor.reconcile", events.index("service.recover")
        ) < events.index("source.start")
        assert events.index("service.recover") < events.index("bot.run")
        assert events.index("service.recover") < events.index("supervisor.run")

    assert events[-5:] == [
        "source.disconnect",
        "bot.close",
        "extractor.close",
        "runtime.close",
        "executor.close",
    ]
