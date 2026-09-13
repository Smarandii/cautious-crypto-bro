from datetime import (
    datetime,
    timezone,
)
from decimal import Decimal
from unittest.mock import patch

import httpx

from cautious_crypto_bro.approval_bot import (
    ApprovalBot,
)
from cautious_crypto_bro.bybit import (
    AccountOrder,
    AccountPosition,
    AccountStateSummary,
    BybitDemoExecutor,
)
from cautious_crypto_bro.domain import (
    Side,
)


def test_account_state_reads_bybit_snapshot() -> None:
    now_ms = int(
        datetime(
            2026,
            9,
            13,
            12,
            0,
            tzinfo=timezone.utc,
        ).timestamp()
        * 1000
    )

    def handler(
        request: httpx.Request,
    ) -> httpx.Response:
        path = request.url.path

        if path == "/v5/position/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "NEARUSDT",
                                "side": "Buy",
                                "size": "819.2",
                                "avgPrice": "2.304",
                                "markPrice": "2.31",
                                "unrealisedPnl": "4.9152",
                                "positionStatus": "Normal",
                                "takeProfit": "",
                                "stopLoss": "2.221",
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/order/realtime":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "side": "Buy",
                                "orderType": "Limit",
                                "orderStatus": "New",
                                "qty": "0.01",
                                "leavesQty": "0.01",
                                "price": "112000",
                                "avgPrice": "",
                                "orderId": "open-1",
                                "orderLinkId": "ccb-open-1",
                                "reduceOnly": False,
                                "updatedTime": str(
                                    now_ms
                                ),
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/order/history":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "ETHUSDT",
                                "side": "Sell",
                                "orderType": "Limit",
                                "orderStatus": "Cancelled",
                                "qty": "0.2",
                                "leavesQty": "0.2",
                                "price": "4700",
                                "avgPrice": "",
                                "orderId": "closed-1",
                                "orderLinkId": "ccb-old",
                                "reduceOnly": False,
                                "updatedTime": str(
                                    now_ms
                                ),
                            }
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        if path == "/v5/position/closed-pnl":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "closedPnl": "12.4"
                            },
                            {
                                "closedPnl": "-2.1"
                            },
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        raise AssertionError(
            f"Unexpected request {request.url}"
        )

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()
    executor._client = httpx.Client(
        base_url=(
            "https://api-demo.bybit.com"
        ),
        transport=httpx.MockTransport(
            handler
        ),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            state = (
                executor
                ._account_state_sync()
            )

        assert (
            state.realized_pnl_today
            == Decimal("10.3")
        )

        assert (
            state.unrealised_pnl
            == Decimal("4.9152")
        )

        assert len(
            state.positions
        ) == 1

        assert len(
            state.open_orders
        ) == 1

        assert len(
            state.terminal_orders_24h
        ) == 1

        exposure = state.exposure_for(
            "BTCUSDT"
        )

        assert len(
            exposure.pending_entry_orders
        ) == 1

    finally:
        executor.close()


def test_account_state_render_is_compact() -> None:
    now = datetime(
        2026,
        9,
        13,
        12,
        0,
        tzinfo=timezone.utc,
    )

    state = AccountStateSummary(
        as_of=now,
        realized_pnl_today=(
            Decimal("12.4")
        ),
        positions=(
            AccountPosition(
                symbol="NEARUSDT",
                side=Side.LONG,
                size=Decimal("819.2"),
                avg_price=Decimal("2.304"),
                mark_price=Decimal("2.31"),
                unrealised_pnl=(
                    Decimal("4.9152")
                ),
                status="Normal",
                take_profit=None,
                stop_loss=Decimal("2.221"),
            ),
        ),
        open_orders=(
            AccountOrder(
                symbol="BTCUSDT",
                side=Side.LONG,
                order_type="Limit",
                status="New",
                quantity=Decimal("0.01"),
                remaining_quantity=(
                    Decimal("0.01")
                ),
                price=Decimal("112000"),
                avg_price=None,
                order_id="1",
                order_link_id="ccb-1",
                reduce_only=False,
                updated_at=now,
            ),
        ),
        terminal_orders_24h=(
            AccountOrder(
                symbol="ETHUSDT",
                side=Side.SHORT,
                order_type="Limit",
                status="Cancelled",
                quantity=Decimal("0.2"),
                remaining_quantity=(
                    Decimal("0.2")
                ),
                price=Decimal("4700"),
                avg_price=None,
                order_id="2",
                order_link_id="ccb-2",
                reduce_only=False,
                updated_at=now,
            ),
        ),
    )

    rendered = (
        ApprovalBot
        ._render_account_state(
            state
        )
    )

    assert (
        "Realized P&amp;L: "
        "<b>+12.40 USDT</b>"
        in rendered
    )

    assert (
        "Unrealized P&amp;L: "
        "<b>+4.92 USDT</b>"
        in rendered
    )

    assert (
        "Open positions: 1"
        in rendered
    )

    assert (
        "NEARUSDT LONG 819.2"
        in rendered
    )

    assert (
        "Open orders: 1"
        in rendered
    )

    assert (
        "Recent terminal "
        "orders — last 24h: 1"
        in rendered
    )


def test_protective_order_is_classified_and_rendered() -> None:
    now = datetime.now(
        timezone.utc
    )

    order = AccountOrder(
        symbol="NEARUSDT",
        side=Side.SHORT,
        order_type="Market",
        status="Untriggered",
        quantity=Decimal("327.7"),
        remaining_quantity=(
            Decimal("327.7")
        ),
        price=None,
        avg_price=None,
        order_id="tp-1",
        order_link_id="",
        reduce_only=False,
        updated_at=now,
        stop_order_type=(
            "PartialTakeProfit"
        ),
        create_type=(
            "CreateByPartialTakeProfit"
        ),
        trigger_price=Decimal("2.47"),
    )

    assert order.kind == "TP"
    assert order.is_protective

    rendered = (
        ApprovalBot
        ._render_account_order(
            order
        )
    )

    assert (
        "NEARUSDT TP 327.7 "
        "@ trigger 2.47"
        in rendered
    )


def test_terminal_breakdown_separates_deactivated() -> None:
    now = datetime.now(
        timezone.utc
    )

    terminal = tuple(
        AccountOrder(
            symbol="NEARUSDT",
            side=Side.SHORT,
            order_type="Market",
            status=status,
            quantity=Decimal("1"),
            remaining_quantity=(
                Decimal("0")
            ),
            price=None,
            avg_price=None,
            order_id=str(index),
            order_link_id="",
            reduce_only=False,
            updated_at=now,
        )
        for index, status in enumerate(
            (
                "Filled",
                "Cancelled",
                "Deactivated",
            )
        )
    )

    state = AccountStateSummary(
        as_of=now,
        realized_pnl_today=(
            Decimal("0")
        ),
        positions=(),
        open_orders=(),
        terminal_orders_24h=(
            terminal
        ),
    )

    rendered = (
        ApprovalBot
        ._render_account_state(
            state
        )
    )

    assert (
        "Filled 1 · Cancelled 1 "
        "· Deactivated 1"
        in rendered
    )
