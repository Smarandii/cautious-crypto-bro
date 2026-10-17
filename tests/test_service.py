import asyncio
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from cautious_crypto_bro.domain import (
    IncomingPost,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
)
from cautious_crypto_bro.service import (
    SignalService,
)
from cautious_crypto_bro.signal_context import (
    SignalContextSnapshot,
)


def test_periodic_pnl_sync_runs_immediately_and_repeats(monkeypatch) -> None:
    service = SignalService(
        store=object(),
        extractor=object(),
        planner=object(),
        executor=object(),
        approval_bot=object(),
        coordinator=object(),
        context_provider=object(),
    )
    sync_pnl = AsyncMock(side_effect=[RuntimeError("temporary API failure"), None])
    monkeypatch.setattr(service, "_sync_account_pnl", sync_pnl)
    sleep_intervals = []

    async def stop_after_second_sleep(interval_seconds: int) -> None:
        sleep_intervals.append(interval_seconds)
        if len(sleep_intervals) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(
        "cautious_crypto_bro.service.asyncio.sleep", stop_after_second_sleep
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_periodic_account_pnl_sync())

    assert sync_pnl.await_count == 2
    assert sleep_intervals == [900, 900]


def post() -> IncomingPost:
    now = datetime.now(UTC)

    return IncomingPost(
        source=SourceMessage(
            channel_id=-1001234567890,
            channel_title="Trader",
            channel_username=None,
            message_id=123,
            published_at=now,
            received_at=now,
            text="signal",
        )
    )


class DuplicateStore:
    async def claim_source(
        self,
        source,
        *,
        lease_seconds,
    ):
        return None


class MustNotBeCalled:
    def __getattr__(
        self,
        name,
    ):
        raise AssertionError(f"{name} must not be called")


class ContextProvider:
    def __init__(self, account_state=None) -> None:
        self.account_state = account_state

    async def snapshot(self, channel_id):
        return SignalContextSnapshot(
            position_context=SignalPositionContext(
                source_channel_id=channel_id,
                account_state_available=(self.account_state is not None),
            ),
            account_state=self.account_state,
            account_state_error=None,
        )


def test_duplicate_source_stops_before_processing() -> None:
    async def run() -> None:
        never = MustNotBeCalled()

        service = SignalService(
            store=DuplicateStore(),
            extractor=never,
            planner=never,
            executor=never,
            approval_bot=never,
            coordinator=never,
            context_provider=never,
        )

        await service.on_message(post())

    asyncio.run(run())


def test_skipped_participation_arm_is_not_dispatched() -> None:
    from cautious_crypto_bro.domain import (
        AccountSnapshotDelivery,
        ApprovalMode,
        Entry,
        EntryType,
        IntentStatus,
        Side,
        TradingIntent,
    )

    intent = TradingIntent(
        source=post().source,
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.MARKET),
        stop_loss=90,
        take_profit=120,
        summary="randomized skipped signal",
        confidence=1,
        approval_mode=ApprovalMode.SKIPPED,
        status=IntentStatus.SKIPPED,
    )
    service = SignalService(
        store=MustNotBeCalled(),
        extractor=MustNotBeCalled(),
        planner=MustNotBeCalled(),
        executor=MustNotBeCalled(),
        approval_bot=MustNotBeCalled(),
        coordinator=MustNotBeCalled(),
        context_provider=MustNotBeCalled(),
    )

    asyncio.run(
        service._dispatch_planned_intents(
            [(intent, object())],
            snapshot=AccountSnapshotDelivery(),
        )
    )


def test_participation_skip_is_persistable_and_does_not_reserve_cap() -> None:
    from types import SimpleNamespace
    from uuid import UUID

    from cautious_crypto_bro.domain import (
        AccountStateSummary,
        ApprovalMode,
        AutoApprovalMode,
        Entry,
        EntryType,
        ExecutionPolicy,
        IntentStatus,
        OpenRelation,
        Side,
        SignalExtraction,
        SignalPositionContext,
        StrategyV2Policy,
        TradingIntent,
    )

    class Store:
        async def get_execution_policy(self):
            return ExecutionPolicy(
                trading_capital_usdt=Decimal("1000"),
                risk_per_trade_pct=Decimal("1"),
                strategy_v2=StrategyV2Policy(),
            )

        async def get_active_position_strategies(self):
            return ()

    class Planner:
        def plan(self, intent, policy, _context):
            return SimpleNamespace(
                intent_id=intent.intent_id,
                policy=policy,
                planned_max_loss_usdt=policy.risk_budget_usdt,
            )

    class Executor:
        async def wallet_balance_usdt(self):
            return Decimal("1000")

        async def market_context(self, symbol):
            return symbol

    class Coordinator:
        def auto_open_safety_reason(self, _intent, _account_state):
            return None

    service = SignalService(
        store=Store(),
        extractor=object(),
        planner=Planner(),
        executor=Executor(),
        approval_bot=object(),
        coordinator=Coordinator(),
        context_provider=object(),
        auto_approval_mode=AutoApprovalMode.ALL,
        demo_exit_profile="payoff_early_tight_trail_long_015",
        demo_long_participation_skip_fraction=Decimal("0.50"),
        demo_portfolio_stop_risk_cap_usdt=Decimal("12"),
    )
    base_policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
    )
    skip_id = next(
        UUID(int=index)
        for index in range(1, 500)
        if service._policy_for_intent_side(
            base_policy, Side.LONG, UUID(int=index)
        ).strategy_v2.long_participation_arm
        == "skip"
    )
    take_id = next(
        UUID(int=index)
        for index in range(500, 1000)
        if service._policy_for_intent_side(
            base_policy, Side.LONG, UUID(int=index)
        ).strategy_v2.long_participation_arm
        == "take"
    )
    source = post().source

    def intent(intent_id, symbol):
        return TradingIntent(
            intent_id=intent_id,
            source=source,
            symbol=symbol,
            side=Side.LONG,
            entry=Entry(type=EntryType.MARKET),
            stop_loss=90,
            take_profit=120,
            summary="randomized participation candidate",
            confidence=1,
            relation=OpenRelation.NEW,
        )

    now = datetime.now(UTC)
    context = SignalContextSnapshot(
        position_context=SignalPositionContext(
            source_channel_id=source.channel_id,
            account_state_available=True,
        ),
        account_state=AccountStateSummary(as_of=now, positions=(), open_orders=()),
        account_state_error=None,
    )
    planned, errors = asyncio.run(
        service._plan_open_intents(
            SignalExtraction(
                open_intents=(intent(skip_id, "BTCUSDT"), intent(take_id, "ETHUSDT"))
            ),
            context,
        )
    )

    assert errors == []
    assert len(planned) == 2
    skipped, skip_plan = planned[0]
    taken, take_plan = planned[1]
    assert skipped.approval_mode is ApprovalMode.SKIPPED
    assert skipped.status is IntentStatus.SKIPPED
    assert skip_plan.policy.strategy_v2.long_participation_arm == "skip"
    assert taken.approval_mode is ApprovalMode.AUTO
    assert taken.status is IntentStatus.PENDING
    assert take_plan.policy.strategy_v2.long_participation_arm == "take"
    assert take_plan.planned_max_loss_usdt == Decimal("10")


class RetryStore:
    def __init__(self) -> None:
        self.status = "READY"
        self.claim = None
        self.failed = 0
        self.completed = 0

    async def claim_source(
        self,
        source,
        *,
        lease_seconds,
    ):
        if self.status not in {
            "READY",
            "FAILED",
        }:
            return None

        self.status = "PROCESSING"
        self.claim = f"claim-{self.failed}"
        return self.claim

    async def get_guidance(
        self,
        channel_id,
    ):
        return None, None

    async def mark_source_failed(
        self,
        source,
        claim_token,
        error,
    ):
        assert claim_token == self.claim
        self.status = "FAILED"
        self.failed += 1
        return True

    async def mark_source_completed(
        self,
        source,
        claim_token,
    ):
        assert claim_token == self.claim
        self.status = "COMPLETED"
        self.completed += 1
        return True


class FailOnceExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(
        self,
        post,
        **kwargs,
    ):
        self.calls += 1

        if self.calls == 1:
            raise RuntimeError("temporary failure")

        return SignalExtraction()


def test_extraction_failure_is_retryable() -> None:
    async def run() -> None:
        store = RetryStore()
        extractor = FailOnceExtractor()
        never = MustNotBeCalled()

        service = SignalService(
            store=store,
            extractor=extractor,
            planner=never,
            executor=never,
            approval_bot=never,
            coordinator=never,
            context_provider=ContextProvider(),
        )

        source_post = post()

        await service.on_message(source_post)

        assert store.status == "FAILED"
        assert store.failed == 1

        await service.on_message(source_post)

        assert extractor.calls == 2
        assert store.status == "COMPLETED"
        assert store.completed == 1

    asyncio.run(run())


def test_multiple_intents_are_planned_persisted_and_sent() -> None:
    from types import SimpleNamespace

    from cautious_crypto_bro.domain import (
        Entry,
        EntryType,
        ExecutionPolicy,
        Side,
        TradingIntent,
    )

    class Store:
        def __init__(self) -> None:
            self.persisted = []

        async def claim_source(
            self,
            source,
            *,
            lease_seconds,
        ):
            return "claim"

        async def get_guidance(
            self,
            channel_id,
        ):
            return None, None

        async def get_execution_policy(self):
            return ExecutionPolicy(
                trading_capital_usdt=Decimal("6800"),
                risk_per_trade_pct=Decimal("1"),
                range_order_count=3,
            )

        async def get_active_position_strategies(self):
            return ()

        async def claim_manual_delivery(self, record_id):
            return "claim"

        async def finish_manual_delivery(self, record_id, token, *, delivered):
            assert delivered

        async def create_signal_batch_and_complete_source(
            self,
            items,
            position_actions,
            claim_token,
        ):
            assert claim_token == "claim"
            assert position_actions == ()
            self.persisted = list(items)
            return True

        async def mark_source_failed(
            self,
            source,
            claim_token,
            error,
        ):
            raise AssertionError(f"source unexpectedly failed: {error}")

    class Extractor:
        def __init__(self, intents) -> None:
            self.intents = intents

        async def extract(
            self,
            post,
            **kwargs,
        ):
            return SignalExtraction(open_intents=tuple(self.intents))

    class Planner:
        def __init__(self) -> None:
            self.capital_snapshots = []
            self.risk_percentages = []

        def plan(
            self,
            intent,
            policy,
            context,
        ):
            self.capital_snapshots.append(policy.trading_capital_usdt)
            self.risk_percentages.append(policy.risk_per_trade_pct)

            return SimpleNamespace(
                intent_id=intent.intent_id,
                orders=(object(),),
                planned_max_loss_usdt=policy.risk_budget_usdt,
            )

    class AccountState:
        positions = ()
        open_orders = ()

        def exposure_for(
            self,
            symbol,
        ):
            return f"exposure:{symbol}"

    class Executor:
        def __init__(self) -> None:
            self.market_context_calls = []
            self.wallet_balance_calls = 0

        async def wallet_balance_usdt(self):
            self.wallet_balance_calls += 1
            return Decimal("7400")

        async def market_context(
            self,
            symbol,
        ):
            self.market_context_calls.append(symbol)
            return symbol

    class Bot:
        def __init__(self) -> None:
            self.calls = []

        async def send_intent(
            self,
            intent,
            plan,
            **kwargs,
        ):
            self.calls.append(
                (
                    intent,
                    plan,
                    kwargs,
                )
            )

    async def run() -> None:
        source_post = post()

        first = TradingIntent(
            source=source_post.source,
            symbol="BTCUSDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.LIMIT,
                price=100,
            ),
            stop_loss=90,
            take_profit=120,
            summary="BTC",
            confidence=1,
        )

        second = TradingIntent(
            source=source_post.source,
            symbol="ETHUSDT",
            side=Side.SHORT,
            entry=Entry(
                type=EntryType.LIMIT,
                price=200,
            ),
            stop_loss=220,
            take_profit=170,
            summary="ETH",
            confidence=1,
        )

        store = Store()
        executor = Executor()
        planner = Planner()
        bot = Bot()

        service = SignalService(
            store=store,
            extractor=Extractor(
                (
                    first,
                    second,
                )
            ),
            planner=planner,
            executor=executor,
            approval_bot=bot,
            coordinator=MustNotBeCalled(),
            context_provider=ContextProvider(AccountState()),
            demo_long_risk_multiplier=Decimal("0.25"),
            demo_portfolio_stop_risk_cap_usdt=Decimal("50"),
        )

        await service.on_message(source_post)

        assert len(store.persisted) == 2
        assert executor.wallet_balance_calls == 1

        assert executor.market_context_calls == [
            "BTCUSDT",
            "ETHUSDT",
        ]

        assert planner.capital_snapshots == [
            Decimal("7400"),
            Decimal("7400"),
        ]
        assert planner.risk_percentages == [
            Decimal("0.25"),
            Decimal("31.5") * Decimal("100") / Decimal("7400"),
        ]

        assert len(bot.calls) == 2

        assert bot.calls[0][2]["snapshot"] is not None
        assert bot.calls[1][2]["snapshot"] is None

        assert bot.calls[0][2]["exposure"] == ("exposure:BTCUSDT")
        assert bot.calls[1][2]["exposure"] == ("exposure:ETHUSDT")

    asyncio.run(run())


def test_full_portfolio_cap_completes_source_without_retriable_planning_failure() -> (
    None
):
    from cautious_crypto_bro.domain import (
        AccountPosition,
        AccountStateSummary,
        Entry,
        EntryType,
        ExecutionPolicy,
        Side,
        TradingIntent,
    )

    class Store:
        completed = 0
        failed = 0

        async def claim_source(self, source, *, lease_seconds):
            return "claim"

        async def get_guidance(self, channel_id):
            return None, None

        async def get_execution_policy(self):
            return ExecutionPolicy(
                trading_capital_usdt=Decimal("1000"),
                risk_per_trade_pct=Decimal("1"),
            )

        async def get_active_position_strategies(self):
            return ()

        async def mark_source_completed(self, source, claim_token):
            self.completed += 1
            return True

        async def mark_source_failed(self, source, claim_token, error):
            self.failed += 1
            return True

    class Extractor:
        async def extract(self, source_post, **kwargs):
            intent = TradingIntent(
                source=source_post.source,
                symbol="BTCUSDT",
                side=Side.LONG,
                entry=Entry(type=EntryType.LIMIT, price=100),
                stop_loss=90,
                take_profit=120,
                summary="BTC long",
                confidence=1,
            )
            return SignalExtraction(open_intents=(intent,))

    class Wallet:
        async def wallet_balance_usdt(self):
            return Decimal("1000")

    now = datetime.now(UTC)
    state = AccountStateSummary(
        as_of=now,
        positions=(
            AccountPosition(
                symbol="ETHUSDT",
                side=Side.LONG,
                size=Decimal("10"),
                avg_price=Decimal("100"),
                mark_price=Decimal("100"),
                unrealised_pnl=Decimal("0"),
                status="Open",
                take_profit=None,
                stop_loss=Decimal("90"),
            ),
        ),
        open_orders=(),
    )
    store = Store()
    service = SignalService(
        store=store,
        extractor=Extractor(),
        planner=MustNotBeCalled(),
        executor=Wallet(),
        approval_bot=MustNotBeCalled(),
        coordinator=MustNotBeCalled(),
        context_provider=ContextProvider(state),
        demo_portfolio_stop_risk_cap_usdt=Decimal("50"),
    )

    asyncio.run(service.on_message(post()))

    assert store.completed == 1
    assert store.failed == 0
