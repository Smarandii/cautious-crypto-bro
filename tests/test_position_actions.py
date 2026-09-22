import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from unittest.mock import patch

import aiosqlite
import httpx

from cautious_crypto_bro.bybit import (
    AccountPosition,
    AccountStateSummary,
    BybitDemoExecutor,
    PositionActionExecutionResult,
)
from cautious_crypto_bro.domain import (
    ApprovalMode,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    SourceMessage,
)
from cautious_crypto_bro.execution_coordinator import (
    ExecutionCoordinator,
)
from cautious_crypto_bro.storage import (
    LATEST_SCHEMA_VERSION,
    IntentStore,
)


def source() -> SourceMessage:
    now = datetime.now(UTC)

    return SourceMessage(
        channel_id=-1002243423111,
        channel_title="Мысли Эмилии",
        channel_username=None,
        message_id=7917,
        published_at=now,
        received_at=now,
        text="Закрываем половину",
    )


def test_position_action_batch_is_persisted_atomically(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        item = source()

        claim = await store.claim_source(
            item,
            lease_seconds=300,
        )

        assert claim is not None

        action = PositionActionIntent(
            source=item,
            symbol="NEARUSDT",
            action=PositionActionType.REDUCE,
            close_pct=50,
            expected_side=Side.LONG,
            summary="Close half",
            confidence=1,
        )

        assert await store.create_signal_batch_and_complete_source(
            (),
            (action,),
            claim,
        )

        stored = await store.get_position_action(action.action_id)

        assert stored is not None
        assert stored.close_pct == 50

        async with aiosqlite.connect(tmp_path / "state.sqlite3") as db:
            cursor = await db.execute("PRAGMA user_version")
            assert (await cursor.fetchone())[0] == LATEST_SCHEMA_VERSION

    asyncio.run(run())


def test_partial_reduce_uses_live_position_size() -> None:
    requests = []

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        requests.append(request)

        if request.url.path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "side": "Buy",
                                "size": "10",
                                "avgPrice": "2.2",
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [],
                        "nextPageCursor": "",
                    },
                },
            )

        if request.url.path == "/v5/market/instruments-info":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "lotSizeFilter": {
                                    "qtyStep": "0.1",
                                    "minOrderQty": "0.1",
                                }
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/order/create":
            body = json.loads(request.read().decode())

            assert body["symbol"] == "NEARUSDT"
            assert body["side"] == "Sell"
            assert body["orderType"] == "Market"
            assert body["qty"] == "3"
            assert body["reduceOnly"] is True
            assert "takeProfit" not in body
            assert "stopLoss" not in body

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"orderId": "reduce-1"},
                },
            )

        raise AssertionError(f"Unexpected request: {request.url}")

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handler),
    )

    action = PositionActionIntent(
        source=source(),
        symbol="NEARUSDT",
        action=PositionActionType.REDUCE,
        close_pct=30,
        expected_side=Side.LONG,
        summary="Close 30%",
        confidence=1,
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            result = executor._execute_position_action_sync(action)

        assert result.order_id == "reduce-1"
        assert result.submitted_quantity == Decimal("3")

    finally:
        executor.close()


def test_full_close_cancels_ccb_entries_first() -> None:
    cancelled = False

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        nonlocal cancelled

        if request.url.path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "side": "Buy",
                                "size": "4.2",
                                "avgPrice": "2.2",
                            }
                        ]
                    },
                },
            )

        if request.url.path == "/v5/order/realtime":
            entries = []

            if not cancelled:
                entries = [
                    {
                        "orderId": "entry-1",
                        "orderLinkId": "ccb-old-1",
                        "side": "Buy",
                        "price": "2.0",
                        "leavesQty": "1",
                        "reduceOnly": False,
                    }
                ]

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": entries,
                        "nextPageCursor": "",
                    },
                },
            )

        if request.url.path == "/v5/order/cancel":
            body = json.loads(request.read().decode())

            assert body["orderLinkId"] == "ccb-old-1"

            cancelled = True

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "orderId": "entry-1",
                        "orderLinkId": "ccb-old-1",
                    },
                },
            )

        if request.url.path == "/v5/order/create":
            body = json.loads(request.read().decode())

            assert cancelled
            assert body["qty"] == "0"
            assert body["side"] == "Sell"
            assert body["reduceOnly"] is True
            assert body["closeOnTrigger"] is True

            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"orderId": "close-1"},
                },
            )

        raise AssertionError(f"Unexpected request: {request.url}")

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()

    executor._client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handler),
    )

    action = PositionActionIntent(
        source=source(),
        symbol="NEARUSDT",
        action=PositionActionType.CLOSE,
        expected_side=Side.LONG,
        summary="Close NEAR",
        confidence=1,
    )

    try:
        with (
            patch.object(
                executor,
                "_sync_clock",
            ),
            patch("cautious_crypto_bro.bybit.time.sleep"),
        ):
            result = executor._execute_position_action_sync(action)

        assert result.order_id == "close-1"
        assert result.cancelled_entry_orders == 1
        assert result.submitted_quantity is None

    finally:
        executor.close()


class CoordinatorExecutor:
    def __init__(
        self,
        sizes: list[Decimal],
    ) -> None:
        self.sizes = sizes
        self.read_index = 0

    async def execute_position_action(
        self,
        action,
    ):
        assert action.action is PositionActionType.REDUCE

        return PositionActionExecutionResult(
            order_id="reduce-confirm-test",
            position_side=Side.LONG,
            position_size_before=(Decimal("10")),
            submitted_quantity=(Decimal("3")),
            cancelled_entry_orders=2,
            cancelled_exit_orders=3,
        )

    async def account_state(self):
        index = min(
            self.read_index,
            len(self.sizes) - 1,
        )

        size = self.sizes[index]

        self.read_index += 1

        return AccountStateSummary(
            as_of=datetime.now(UTC),
            positions=(
                AccountPosition(
                    symbol="NEARUSDT",
                    side=Side.LONG,
                    size=size,
                    avg_price=Decimal("2.2"),
                    mark_price=Decimal("2.2"),
                    unrealised_pnl=Decimal("0"),
                    status="Normal",
                    take_profit=None,
                    stop_loss=Decimal("2"),
                ),
            ),
            open_orders=(),
        )


async def persist_reduce_action(
    store: IntentStore,
) -> PositionActionIntent:
    item = source()

    claim = await store.claim_source(
        item,
        lease_seconds=300,
    )

    assert claim is not None

    action = PositionActionIntent(
        source=item,
        symbol="NEARUSDT",
        action=PositionActionType.REDUCE,
        close_pct=30,
        expected_side=Side.LONG,
        summary="Close 30%",
        confidence=1,
    )

    assert await store.create_signal_batch_and_complete_source(
        (),
        (action,),
        claim,
    )

    return action


def test_coordinator_waits_for_reduce_position_change(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")

        await store.initialize()

        action = await persist_reduce_action(store)

        executor = CoordinatorExecutor(
            [
                Decimal("10"),
                Decimal("10"),
                Decimal("7"),
            ]
        )

        coordinator = ExecutionCoordinator(
            store=store,
            executor=cast(
                BybitDemoExecutor,
                executor,
            ),
            max_age_seconds=3600,
        )

        async def no_sleep(
            _: float,
        ) -> None:
            return None

        with patch(
            "cautious_crypto_bro.execution_coordinator.asyncio.sleep",
            no_sleep,
        ):
            outcome = await coordinator.execute_position_action(
                action.action_id,
                approval_mode=(ApprovalMode.MANUAL),
            )

        assert outcome.status is IntentStatus.EXECUTED

        assert executor.read_index == 3

        stored = await store.get_position_action(action.action_id)

        assert stored is not None
        assert stored.status is IntentStatus.EXECUTED

    asyncio.run(run())


def test_coordinator_marks_unconfirmed_reduce_uncertain(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")

        await store.initialize()

        action = await persist_reduce_action(store)

        executor = CoordinatorExecutor(
            [
                Decimal("10"),
            ]
        )

        coordinator = ExecutionCoordinator(
            store=store,
            executor=cast(
                BybitDemoExecutor,
                executor,
            ),
            max_age_seconds=3600,
        )

        async def no_sleep(
            _: float,
        ) -> None:
            return None

        with patch(
            "cautious_crypto_bro.execution_coordinator.asyncio.sleep",
            no_sleep,
        ):
            outcome = await coordinator.execute_position_action(
                action.action_id,
                approval_mode=(ApprovalMode.MANUAL),
            )

        assert outcome.status is IntentStatus.UNCERTAIN

        stored = await store.get_position_action(action.action_id)

        assert stored is not None
        assert stored.status is IntentStatus.UNCERTAIN

        assert executor.read_index == 20

    asyncio.run(run())
