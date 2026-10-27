"""Safety checks for the opt-in live suite; these tests never access a network."""

import asyncio
import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cautious_crypto_bro.domain import Side

spec = importlib.util.spec_from_file_location(
    "demo_e2e", Path(__file__).parents[1] / "scripts" / "e2e_bybit_demo.py"
)
assert spec is not None and spec.loader is not None
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def test_live_suite_requires_opt_in_before_creating_client(monkeypatch):
    def forbidden():
        raise AssertionError("Client must not be created without opt-in")

    monkeypatch.setattr(demo, "executor", forbidden)
    with pytest.raises(AssertionError, match="Explicit --execute-demo required"):
        asyncio.run(demo.main(SimpleNamespace(resume_db=None, execute_demo=False)))


def test_cleanup_ignores_other_symbols():
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(
                positions=[SimpleNamespace(symbol="BTCUSDT", side=Side.LONG)],
                open_orders=[],
            )
        ),
        execute_position_action=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    asyncio.run(demo.cleanup(fake, Side.LONG, "ccb-v2-owned-"))
    fake.account_state.assert_awaited_once()
    fake.execute_position_action.assert_not_called()
    fake.cancel_order.assert_not_called()


@pytest.mark.parametrize(
    "link, protective",
    [("other-owner", False), ("other-owner", True), ("ccb-action-foreign", False)],
)
def test_cleanup_refuses_unowned_orders_on_test_symbol(link, protective):
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(
                positions=[],
                open_orders=[
                    SimpleNamespace(
                        symbol=demo.SYMBOL,
                        order_link_id=link,
                        is_protective=protective,
                        order_id="foreign-order",
                    )
                ],
            )
        ),
        execute_position_action=AsyncMock(),
        cancel_order=AsyncMock(),
    )
    fake.cancel_order.side_effect = lambda *_: (
        fake.account_state.return_value.open_orders.clear()
    )
    with pytest.raises(RuntimeError, match="Cleanup unverified"):
        asyncio.run(demo.cleanup(fake, Side.LONG, "ccb-v2-owned-"))
    fake.execute_position_action.assert_not_called()
    fake.cancel_order.assert_not_called()


def test_poll_failure_is_bounded():
    fake = SimpleNamespace(account_state=AsyncMock(return_value=object()))
    with pytest.raises(AssertionError, match="Timed out: never ready"):
        asyncio.run(demo.wait_account(fake, lambda _: False, "never ready", seconds=0))
    fake.account_state.assert_awaited_once()


def test_cleanup_refuses_same_side_exposure_without_run_executions():
    account = SimpleNamespace(
        positions=[
            SimpleNamespace(symbol=demo.SYMBOL, side=Side.LONG, size=Decimal("1"))
        ],
        open_orders=[],
    )
    fake = SimpleNamespace(
        account_state=AsyncMock(return_value=account),
        execute_position_action=AsyncMock(
            side_effect=lambda *_: account.positions.clear()
        ),
        cancel_order=AsyncMock(),
    )
    with pytest.raises(RuntimeError, match="Cleanup unverified"):
        asyncio.run(demo.cleanup(fake, Side.LONG, "ccb-v2-owned-"))
    fake.execute_position_action.assert_not_called()
    fake.cancel_order.assert_not_called()


def test_zone_probe_refuses_existing_exposure():
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(
                positions=[SimpleNamespace(symbol=demo.SYMBOL)], open_orders=[]
            )
        ),
        market_context=AsyncMock(),
    )
    with pytest.raises(AssertionError, match="must be flat"):
        asyncio.run(demo.observe_zone_stops(fake, Side.LONG))
    fake.market_context.assert_not_awaited()


@pytest.mark.parametrize(
    "fault", [None, "foreign_fill", "lost_accepted", "lost_unconfirmed"]
)
def test_owned_cleanup_reconciles_fills_and_does_not_replay_close(fault, tmp_path):
    class Exchange:
        def __init__(self):
            self.positions = []
            self.orders = []
            self.fills = []
            self.posts = []
            self.cancels = []
            self._client = SimpleNamespace(
                private_post=self.post, paginate_private_list=self.history
            )

        async def account_state(self):
            return SimpleNamespace(
                positions=list(self.positions), open_orders=list(self.orders)
            )

        def post(self, path, params):
            durable = json.loads(self.journal.read_text())
            assert params["orderLinkId"] in durable["submitted_links"]
            if params.get("reduceOnly"):
                assert durable["cleanup_close_submitted"] is True
            self.posts.append(dict(params))
            order_id = f"order-{len(self.posts)}"
            closing = params.get("reduceOnly", False)
            if closing and fault == "lost_unconfirmed":
                raise TimeoutError("No acknowledgment or fill")
            self.fills.append(
                {
                    "execId": f"execution-{len(self.fills)}",
                    "execType": "Trade",
                    "orderId": order_id,
                    "orderLinkId": params["orderLinkId"],
                    "symbol": demo.SYMBOL,
                    "side": params["side"],
                    "execQty": params["qty"],
                }
            )
            self.positions = (
                []
                if closing
                else [
                    SimpleNamespace(
                        symbol=demo.SYMBOL, side=Side.LONG, size=Decimal("1")
                    )
                ]
            )
            if not closing:
                self.orders = [
                    SimpleNamespace(
                        symbol=demo.SYMBOL,
                        order_id="owned-stop",
                        order_link_id="",
                        parent_order_link_id=params["orderLinkId"],
                        is_protective=True,
                    )
                ]
            if closing and fault == "lost_accepted":
                raise TimeoutError("Fill accepted; acknowledgment lost")
            return {"result": {"orderId": order_id}}

        def history(self, path, params):
            if fault == "foreign_fill" and len(self.fills) == 1:
                self.fills.append(
                    {
                        "execId": "foreign-execution",
                        "execType": "Trade",
                        "orderId": "foreign-order",
                        "orderLinkId": "foreign-link",
                        "symbol": demo.SYMBOL,
                        "side": "Buy",
                        "execQty": "1",
                    }
                )
                self.positions = [
                    SimpleNamespace(
                        symbol=demo.SYMBOL, side=Side.LONG, size=Decimal("2")
                    )
                ]
            return list(self.fills)

        async def cancel_order(self, symbol, order_id):
            self.cancels.append(order_id)
            self.orders = [order for order in self.orders if order.order_id != order_id]

    async def run():
        exchange = Exchange()
        journal = tmp_path / "ownership.json"
        exchange.journal = journal
        owner = await demo.ProbeOwnership.start(exchange, journal_path=journal)
        try:
            exchange._client.private_post(
                "/v5/order/create",
                {
                    "symbol": demo.SYMBOL,
                    "orderLinkId": "exact-run-entry",
                    "side": "Buy",
                    "qty": "1",
                },
            )
            if fault in {"foreign_fill", "lost_unconfirmed"}:
                with pytest.raises(RuntimeError, match="Cleanup unverified"):
                    await demo.cleanup(exchange, Side.LONG, "run-", ownership=owner)
                assert exchange.cancels == []
                assert len(exchange.posts) == (1 if fault == "foreign_fill" else 2)
                assert exchange.positions
            else:
                await demo.cleanup(exchange, Side.LONG, "run-", ownership=owner)
                assert len(exchange.posts) == 2
                assert exchange.posts[-1]["qty"] == "1"
                assert exchange.posts[-1]["reduceOnly"] is True
                assert exchange.cancels == ["owned-stop"]
                assert not exchange.positions and not exchange.orders
        finally:
            owner.restore()
        durable = json.loads(journal.read_text())
        assert "exact-run-entry" in durable["submitted_links"]
        assert durable["cleanup_close_submitted"] == (fault != "foreign_fill")

    asyncio.run(run())


def test_probe_refuses_missing_persistent_evidence_before_exchange_read(monkeypatch):
    monkeypatch.delenv("CCB_PROBE_EVIDENCE_DIR", raising=False)
    exchange = SimpleNamespace(account_state=AsyncMock())
    with pytest.raises(AssertionError, match="Persistent probe evidence"):
        asyncio.run(demo.ProbeOwnership.start(exchange))
    exchange.account_state.assert_not_called()


def test_journal_write_failure_prevents_submission(tmp_path, monkeypatch):
    async def run():
        post = AsyncMock()
        exchange = SimpleNamespace(
            account_state=AsyncMock(
                return_value=SimpleNamespace(positions=[], open_orders=[])
            ),
            _client=SimpleNamespace(private_post=post),
        )
        owner = await demo.ProbeOwnership.start(
            exchange, journal_path=tmp_path / "owner.json"
        )

        def unavailable():
            raise OSError("Evidence volume unavailable")

        monkeypatch.setattr(owner, "save", unavailable)
        try:
            with pytest.raises(OSError, match="Evidence volume unavailable"):
                owner.submit(
                    "/v5/order/create", {"symbol": demo.SYMBOL, "orderLinkId": "owned"}
                )
            post.assert_not_called()
        finally:
            owner.restore()

    asyncio.run(run())


def test_zone_probe_refuses_excessive_minimum_notional():
    fake = SimpleNamespace(
        account_state=AsyncMock(
            return_value=SimpleNamespace(positions=[], open_orders=[])
        ),
        market_context=AsyncMock(
            return_value=SimpleNamespace(
                min_qty=Decimal("1"),
                min_notional=Decimal("100"),
                market_price=Decimal("1"),
                qty_step=Decimal("1"),
            )
        ),
    )
    with pytest.raises(AssertionError, match="notional bound exceeded"):
        asyncio.run(demo.observe_zone_stops(fake, Side.SHORT))
