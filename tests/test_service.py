import asyncio
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal

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

        def plan(
            self,
            intent,
            policy,
            context,
        ):
            self.capital_snapshots.append(policy.trading_capital_usdt)

            return SimpleNamespace(
                intent_id=intent.intent_id,
                orders=(object(),),
            )

    class AccountState:
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

        assert len(bot.calls) == 2

        assert bot.calls[0][2]["send_account_state"] is True
        assert bot.calls[1][2]["send_account_state"] is False

        assert bot.calls[0][2]["exposure"] == ("exposure:BTCUSDT")
        assert bot.calls[1][2]["exposure"] == ("exposure:ETHUSDT")

    asyncio.run(run())
