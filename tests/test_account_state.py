from datetime import (
    UTC,
    datetime,
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
            tzinfo=UTC,
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
                                "updatedTime": str(now_ms),
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
                                "updatedTime": str(now_ms),
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
                            {"closedPnl": "12.4"},
                            {"closedPnl": "-2.1"},
                        ],
                        "nextPageCursor": "",
                    },
                },
            )

        raise AssertionError(f"Unexpected request {request.url}")

    executor = BybitDemoExecutor(
        api_key="key",
        api_secret="secret",
    )

    executor._client.close()
    executor._client = httpx.Client(
        base_url=("https://api-demo.bybit.com"),
        transport=httpx.MockTransport(handler),
    )

    try:
        with patch.object(
            executor,
            "_sync_clock",
        ):
            state = executor._account_state_sync()

        assert state.unrealised_pnl == Decimal("4.9152")

        assert len(state.positions) == 1

        assert len(state.open_orders) == 1

        exposure = state.exposure_for("BTCUSDT")

        assert len(exposure.pending_entry_orders) == 1

    finally:
        executor.close()


def test_account_state_render_is_compact() -> None:
    now = datetime(
        2026,
        9,
        13,
        12,
        0,
        tzinfo=UTC,
    )

    position = AccountPosition(
        symbol="NEARUSDT",
        side=Side.LONG,
        size=Decimal("819.2"),
        avg_price=Decimal("2.304"),
        mark_price=Decimal("2.31"),
        unrealised_pnl=Decimal("4.9152"),
        status="Normal",
        take_profit=None,
        stop_loss=None,
    )

    entry = AccountOrder(
        symbol="BTCUSDT",
        side=Side.LONG,
        order_type="Limit",
        status="New",
        quantity=Decimal("0.01"),
        remaining_quantity=Decimal("0.01"),
        price=Decimal("112000"),
        avg_price=None,
        order_id="entry",
        order_link_id="ccb-entry",
        reduce_only=False,
        updated_at=now,
    )

    stop = AccountOrder(
        symbol="NEARUSDT",
        side=Side.SHORT,
        order_type="Market",
        status="Untriggered",
        quantity=Decimal("819.2"),
        remaining_quantity=Decimal("819.2"),
        price=None,
        avg_price=None,
        order_id="sl",
        order_link_id="",
        reduce_only=False,
        updated_at=now,
        stop_order_type="StopLoss",
        trigger_price=Decimal("2.221"),
    )

    take_profit = AccountOrder(
        symbol="NEARUSDT",
        side=Side.SHORT,
        order_type="Market",
        status="Untriggered",
        quantity=Decimal("819.2"),
        remaining_quantity=Decimal("819.2"),
        price=None,
        avg_price=None,
        order_id="tp",
        order_link_id="",
        reduce_only=False,
        updated_at=now,
        stop_order_type="TakeProfit",
        trigger_price=Decimal("2.47"),
    )

    state = AccountStateSummary(
        as_of=now,
        positions=(position,),
        open_orders=(
            entry,
            stop,
            take_profit,
        ),
    )

    rendered = ApprovalBot._render_account_state(state)

    assert "1 position" in rendered

    assert "Notional ≈ <b>1892.35 USDT</b>" in rendered

    assert "Live uPnL (Bybit): <b>+4.92 USDT</b>" in rendered

    assert "NEARUSDT LONG</b> · 819.2" in rendered

    assert "Entry 2.304 → Mark 2.31 · uPnL +4.92 USDT" in rendered

    assert "Protection: SL 2.221 · TP 2.47" in rendered

    assert (
        "Pending orders: "
        "<b>1</b> entry · "
        "<b>2</b> protective · "
        "<b>0</b> reduce/close" in rendered
    )

    assert "Realized (tracked): <b>not synced</b>" in rendered

    assert "Combined:" not in rendered

    assert "Recent terminal orders" not in rendered


def test_protective_order_is_classified_and_rendered() -> None:
    now = datetime.now(UTC)

    order = AccountOrder(
        symbol="NEARUSDT",
        side=Side.SHORT,
        order_type="Market",
        status="Untriggered",
        quantity=Decimal("327.7"),
        remaining_quantity=(Decimal("327.7")),
        price=None,
        avg_price=None,
        order_id="tp-1",
        order_link_id="",
        reduce_only=False,
        updated_at=now,
        stop_order_type=("PartialTakeProfit"),
        create_type=("CreateByPartialTakeProfit"),
        trigger_price=Decimal("2.47"),
    )

    assert order.kind == "TP"
    assert order.is_protective

    rendered = ApprovalBot._render_account_order(order)

    assert "NEARUSDT TP 327.7 @ trigger 2.47" in rendered
