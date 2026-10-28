"""Synthetic race scenarios for independent-zone protection (#139).

Native amendment behavior is observed by the probe suite; these adapter-level
checks pin the read/verification semantics that races must not break.
"""

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from test_demo_e2e import demo
from test_remaining_concerns import executor_for
from test_zone_probe_integration import PairedExchange

from cautious_crypto_bro.domain import Side

pytestmark = pytest.mark.integration

QUANTITIES = [Decimal("1"), Decimal("2")]
STOPS = [Decimal("95"), Decimal("94")]
TARGETS = [Decimal("108"), Decimal("109")]


def _child(order_id, parent, kind, qty, trigger):
    return SimpleNamespace(
        symbol=demo.SYMBOL,
        order_id=order_id,
        order_link_id=f"{parent}-{kind}",
        parent_order_link_id=parent,
        kind=kind,
        remaining_quantity=Decimal(qty),
        trigger_price=Decimal(trigger),
        reduce_only=True,
    )


def _account(size, children):
    return SimpleNamespace(
        positions=[
            SimpleNamespace(symbol=demo.SYMBOL, side=Side.LONG, size=Decimal(size))
        ]
        if size
        else [],
        open_orders=list(children),
    )


def test_late_entry_fill_cannot_consume_the_other_zone_allocation() -> None:
    a_sl = _child("a-sl", "zone-a", "SL", "1", "95")
    a_tp = _child("a-tp", "zone-a", "TP", "1", "108")
    early = _account("1", [a_sl, a_tp])

    snapshot_a = demo.zone_pair_snapshot(
        early, ["zone-a"], [QUANTITIES[0]], [STOPS[0]], [TARGETS[0]]
    )[0]
    demo.require_original_pair(early, "zone-a", snapshot_a)

    # Zone B fills late; A's identities, quantities and triggers must not move
    # and B's children attribute to B's own allocation only.
    b_sl = _child("b-sl", "zone-b", "SL", "2", "94")
    b_tp = _child("b-tp", "zone-b", "TP", "2", "109")
    late = _account("3", [a_sl, a_tp, b_sl, b_tp])

    snapshot_both = demo.zone_pair_snapshot(
        late, ["zone-a", "zone-b"], QUANTITIES, STOPS, TARGETS
    )
    demo.require_original_pair(late, "zone-a", snapshot_both[0])
    demo.require_original_pair(late, "zone-b", snapshot_both[1])
    assert demo.pair_evidence(late, {"zone-a", "zone-b"})["net_quantity"] == "3"


def test_lost_amendment_acknowledgement_never_reamends_or_adopts(tmp_path, monkeypatch):
    monkeypatch.setenv("CCB_PROBE_EVIDENCE_DIR", str(tmp_path))

    class AmendingExchange(PairedExchange):
        def __init__(self, side):
            super().__init__(side, False)
            self.amend_requests = 0

        def handle(self, request):
            if request.url.path == "/v5/order/amend":
                self.amend_requests += 1
                body = (
                    json.loads(request.content)
                    if request.content
                    else dict(request.url.params)
                )
                order = self.orders[body["orderLinkId"]]
                order["triggerPrice"] = body["triggerPrice"]
                raise httpx.ReadTimeout("Injected lost amend ack", request=request)
            return super().handle(request)

    exchange = AmendingExchange(Side.LONG)
    link = "ccb-zone-late-a-SL"

    async def amend_once(executor):
        ownership = await demo.ProbeOwnership.start(
            executor, journal_path=tmp_path / "owner.json"
        )
        exchange.orders[link] = {
            "orderId": "id-1",
            "orderLinkId": link,
            "symbol": demo.SYMBOL,
            "side": "Sell",
            "qty": "1",
            "leavesQty": "1",
            "triggerPrice": "95",
            "orderStatus": "Untriggered",
            "reduceOnly": True,
            "parentOrderLinkId": "ccb-zone-late-a",
        }
        ownership.links.add(link)
        ownership.order_ids.add("id-1")
        try:
            try:
                await asyncio.to_thread(
                    executor._client.private_post,
                    "/v5/order/amend",
                    {
                        "category": "linear",
                        "symbol": demo.SYMBOL,
                        "orderLinkId": link,
                        "triggerPrice": "93",
                    },
                )
            except Exception:
                pass  # lost acknowledgement: reconcile from fresh reads only
            account = await executor.account_state()
            await ownership.verify(account, Side.LONG)
            return ownership
        finally:
            ownership.restore()

    with executor_for(exchange) as executor:
        ownership = asyncio.run(amend_once(executor))

    assert exchange.amend_requests == 1
    amended = [
        order for order in exchange.orders.values() if order["orderLinkId"] == link
    ]
    assert len(amended) == 1 and amended[0]["triggerPrice"] == "93"
    assert ownership.links == {link}
    assert ownership.order_ids == {"id-1"}
