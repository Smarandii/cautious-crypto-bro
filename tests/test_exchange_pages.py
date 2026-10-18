"""Exchange page failures must stop entry before any account mutation."""

import asyncio
from decimal import Decimal

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, make_intent, source

from cautious_crypto_bro.domain import (
    ApprovalMode,
    ExecutionPolicy,
    InstrumentContext,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    Side,
    TradeExecutionError,
)
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator
from cautious_crypto_bro.storage import IntentStore

pytestmark = pytest.mark.integration


class SymbolPagedExchange(Exchange):
    def __init__(self, endpoint, pages):
        super().__init__()
        self.size = Decimal("1")
        self.endpoint = endpoint
        self.pages = pages
        self.page_requests = []

    def handle(self, request):
        if request.url.path == self.endpoint and "symbol" in request.url.params:
            self.page_requests.append(dict(request.url.params))
            # Bound the broken implementation too, without hanging the suite.
            assert len(self.page_requests) <= len(self.pages), "Pagination did not stop"
            return httpx.Response(200, json=self.pages[len(self.page_requests) - 1])
        return super().handle(request)


@pytest.mark.parametrize("endpoint", ["/v5/position/list", "/v5/order/realtime"])
@pytest.mark.parametrize("fault", ["missing-list", "repeated-cursor"])
def test_invalid_symbol_pages_stop_close_before_any_mutation(endpoint, fault):
    async def run():
        pages = (
            [{"retCode": 0, "result": {}}]
            if fault == "missing-list"
            else [
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "A"}},
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "A"}},
            ]
        )
        exchange = SymbolPagedExchange(endpoint, pages)
        action = PositionActionIntent(
            source=source("Close BTCUSDT"),
            symbol="BTCUSDT",
            action=PositionActionType.CLOSE,
            expected_side=Side.LONG,
            summary="Close position",
            confidence=1,
        )
        with executor_for(exchange) as executor:
            with pytest.raises(TradeExecutionError, match="Bybit.*page"):
                await executor.execute_position_action(action)
        assert len(exchange.page_requests) == len(pages)
        assert not any(
            path in {"/v5/order/cancel", "/v5/order/create", "/v5/order/create-batch"}
            for path, _ in exchange.requests
        )

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["/v5/position/list", "/v5/order/realtime"])
def test_symbol_exposure_includes_later_pages_and_preserves_filters(endpoint):
    exchange = SymbolPagedExchange(endpoint, [])
    row = exchange.position() | {
        "orderLinkId": "ccb-existing",
        "reduceOnly": False,
        "leavesQty": "1",
        "price": "100",
    }
    exchange.pages = [
        {"retCode": 0, "result": {"list": [], "nextPageCursor": "next"}},
        {"retCode": 0, "result": {"list": [row], "nextPageCursor": ""}},
    ]
    with executor_for(exchange) as executor:
        exposure = executor._exposure_sync("BTCUSDT")
    if endpoint == "/v5/position/list":
        assert len(exposure.positions) == 1
        assert exposure.positions[0].size == Decimal("1")
    else:
        assert len(exposure.pending_entry_orders) == 1
    first, second = exchange.page_requests
    assert first["symbol"] == "BTCUSDT"
    assert second == first | {"cursor": "next"}
    if endpoint == "/v5/order/realtime":
        assert first["openOnly"] == "0"
        assert first["orderFilter"] == "Order"
        assert first["limit"] == "50"


async def prepared_entry(path, executor, mode=ApprovalMode.MANUAL):
    store = IntentStore(path)
    await store.initialize()
    intent = make_intent(mode=mode).model_copy(update={"relation": OpenRelation.NEW})
    planner = ExecutionPlanner()
    plan = planner.plan(
        intent,
        ExecutionPolicy(trading_capital_usdt=Decimal("6800")),
        InstrumentContext(
            Decimal("100"),
            Decimal(".1"),
            Decimal(".001"),
            Decimal(".001"),
            Decimal("5"),
        ),
    )
    claim = await store.claim_source(intent.source, lease_seconds=300)
    await store.create_signal_batch_and_complete_source(((intent, plan),), (), claim)
    coordinator = ExecutionCoordinator(
        store=store,
        executor=executor,
        planner=planner,
        max_age_seconds=3600,
        portfolio_stop_risk_cap_usdt=Decimal("100"),
    )
    return store, intent, coordinator


class PagedExchange(Exchange):
    def __init__(self, endpoint, pages):
        super().__init__()
        self.endpoint = endpoint
        self.pages = pages
        self.page_requests = []

    def handle(self, request):
        if request.url.path == self.endpoint and "settleCoin" in request.url.params:
            self.page_requests.append(dict(request.url.params))
            assert len(self.page_requests) <= len(self.pages), "Pagination did not stop"
            return httpx.Response(200, json=self.pages[len(self.page_requests) - 1])
        return super().handle(request)


BAD_PAGES = [
    pytest.param({"retCode": 0}, id="missing-result"),
    pytest.param({"retCode": 0, "result": {}}, id="missing-list"),
    pytest.param({"retCode": 0, "result": []}, id="non-object-result"),
    pytest.param({"retCode": 0, "result": {"list": {}}}, id="non-array-list"),
    pytest.param({"retCode": 0, "result": {"list": [None]}}, id="non-object-row"),
    pytest.param(
        {"retCode": 0, "result": {"list": [], "nextPageCursor": 1}},
        id="non-string-cursor",
    ),
]


@pytest.mark.parametrize("endpoint", ["/v5/position/list", "/v5/order/realtime"])
@pytest.mark.parametrize("bad_page", BAD_PAGES)
def test_invalid_account_page_fails_before_entry(tmp_path, endpoint, bad_page):
    async def run():
        exchange = PagedExchange(endpoint, [bad_page])
        with executor_for(exchange) as executor:
            store, intent, coordinator = await prepared_entry(
                tmp_path / "state.db", executor, mode=ApprovalMode.AUTO
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.AUTO
            )
            assert outcome.status is IntentStatus.FAILED
            assert "Bybit" in outcome.message and "page" in outcome.message
            assert (
                await store.get_intent(intent.intent_id)
            ).status is IntentStatus.FAILED
            assert await store.get_active_position_strategies() == ()
            assert not exchange.orders
            assert not any(
                path
                in {
                    "/v5/position/set-leverage",
                    "/v5/order/create",
                    "/v5/order/create-batch",
                }
                for path, _ in exchange.requests
            )

    asyncio.run(run())


@pytest.mark.parametrize("cursors", [("A", "A"), ("A", "B", "A")])
def test_cursor_cycle_fails_instead_of_looping_or_returning_partial_state(
    tmp_path, cursors
):
    async def run():
        exchange = PagedExchange(
            "/v5/position/list",
            [
                {"retCode": 0, "result": {"list": [], "nextPageCursor": cursor}}
                for cursor in cursors
            ],
        )
        with executor_for(exchange) as executor:
            _, intent, coordinator = await prepared_entry(
                tmp_path / "state.db", executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.FAILED
            assert "page cursor repeated" in outcome.message
            assert len(exchange.page_requests) == len(cursors)
            assert not exchange.orders

    asyncio.run(run())


def test_valid_empty_pages_remain_tradable_and_request_parameters_are_preserved(
    tmp_path,
):
    async def run():
        exchange = PagedExchange(
            "/v5/position/list",
            [
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "next"}},
                {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}},
            ],
        )
        with executor_for(exchange) as executor:
            _, intent, coordinator = await prepared_entry(
                tmp_path / "state.db", executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.EXECUTED
            assert len(exchange.page_requests) == 2
            first, second = exchange.page_requests
            assert "cursor" not in first
            assert second == first | {"cursor": "next"}
            assert exchange.orders

    asyncio.run(run())


@pytest.mark.parametrize("endpoint", ["/v5/position/list", "/v5/order/realtime"])
def test_exposure_after_empty_intermediate_page_still_blocks_entry(tmp_path, endpoint):
    async def run():
        row = {
            "symbol": "BTCUSDT",
            "side": "Buy",
            "size": "1",
            "avgPrice": "100",
            "markPrice": "100",
            "stopLoss": "90",
            "qty": "1",
            "leavesQty": "1",
            "price": "100",
            "orderType": "Limit",
            "orderStatus": "New",
            "orderId": "existing",
            "orderLinkId": "",
            "createdTime": "1",
            "updatedTime": "1",
        }
        exchange = PagedExchange(
            endpoint,
            [
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "A"}},
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "B"}},
                {"retCode": 0, "result": {"list": [row], "nextPageCursor": ""}},
            ],
        )
        with executor_for(exchange) as executor:
            state = await executor.account_state()
            assert (
                len(
                    state.positions
                    if endpoint.endswith("position/list")
                    else state.open_orders
                )
                == 1
            )
            exchange.page_requests.clear()
            _, intent, coordinator = await prepared_entry(
                tmp_path / "state.db", executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.FAILED
            assert len(exchange.page_requests) == 3
            assert not exchange.orders

    asyncio.run(run())


def test_malformed_later_page_discards_partial_account_state(tmp_path):
    async def run():
        exchange = PagedExchange(
            "/v5/position/list",
            [
                {"retCode": 0, "result": {"list": [], "nextPageCursor": "next"}},
                {"retCode": 0, "result": {"list": [None]}},
            ],
        )
        with executor_for(exchange) as executor:
            _, intent, coordinator = await prepared_entry(
                tmp_path / "state.db", executor
            )
            outcome = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert outcome.status is IntentStatus.FAILED
            assert "invalid page list" in outcome.message
            assert len(exchange.page_requests) == 2
            assert not exchange.orders

    asyncio.run(run())
