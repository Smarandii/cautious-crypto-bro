"""Exercise the full probe with real adapter normalization and synthetic broker behavior."""

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from test_demo_e2e import demo
from test_remaining_concerns import Exchange, executor_for

from cautious_crypto_bro.domain import Side

pytestmark = pytest.mark.integration


class PairedExchange(Exchange):
    def __init__(self, side, peer_retires):
        super().__init__(side)
        self.peer_retires = peer_retires
        self.executions = []
        self.canceled = []

    def position(self):
        return {**super().position(), "symbol": demo.SYMBOL}

    def create(self, body):
        response = super().create(body)
        order = self.orders[body["orderLinkId"]]
        self.executions.append(
            {
                "execId": "fill-" + order["orderId"],
                "execType": "Trade",
                "orderId": order["orderId"],
                "orderLinkId": body["orderLinkId"],
                "symbol": demo.SYMBOL,
                "side": body["side"],
                "execQty": body["qty"],
                "execTime": str(int(datetime.now(UTC).timestamp() * 1000)),
            }
        )
        if not body.get("reduceOnly"):
            assert body["tpslMode"] == "Partial"
            assert body["slOrderType"] == body["tpOrderType"] == "Market"
            for kind, field in (("TP", "takeProfit"), ("SL", "stopLoss")):
                key = body["orderLinkId"] + "-" + kind
                self.orders[key] = {
                    **order,
                    "orderId": key,
                    "orderLinkId": "",
                    "parentOrderLinkId": body["orderLinkId"],
                    "side": "Sell" if body["side"] == "Buy" else "Buy",
                    "reduceOnly": True,
                    "closeOnTrigger": True,
                    "stopOrderType": "PartialTakeProfit"
                    if kind == "TP"
                    else "PartialStopLoss",
                    "triggerPrice": body[field],
                    "qty": body["qty"],
                    "leavesQty": body["qty"],
                    "cumExecQty": "0",
                    "orderStatus": "Untriggered",
                }
        if not self.size:
            for item in self.orders.values():
                if item.get("parentOrderLinkId"):
                    item["orderStatus"] = "Cancelled"
        return response

    def handle(self, request):
        path = request.url.path
        body = (
            json.loads(request.content) if request.content else dict(request.url.params)
        )
        if path == "/v5/execution/list":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {"list": self.executions, "nextPageCursor": ""},
                },
            )
        if path == "/v5/order/realtime":
            active = [
                o
                for o in self.orders.values()
                if o["orderStatus"] in {"New", "PartiallyFilled", "Untriggered"}
            ]
            return httpx.Response(
                200,
                json={"retCode": 0, "result": {"list": active, "nextPageCursor": ""}},
            )
        response = super().handle(request)
        if path == "/v5/order/cancel":
            self.canceled.append(body["orderId"])
            target = next(
                o for o in self.orders.values() if o["orderId"] == body["orderId"]
            )
            if self.peer_retires and target.get("stopOrderType") == "PartialTakeProfit":
                for item in self.orders.values():
                    if item.get("parentOrderLinkId") == target["parentOrderLinkId"]:
                        item["orderStatus"] = "Cancelled"
        return response


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("peer_retires", [False, True])
def test_full_paired_probe_preserves_b_and_cleans_only_owned_orders(
    tmp_path, monkeypatch, capsys, side, peer_retires
):
    monkeypatch.setenv("CCB_PROBE_EVIDENCE_DIR", str(tmp_path))
    exchange = PairedExchange(side, peer_retires)
    with executor_for(exchange) as executor:
        asyncio.run(demo.observe_zone_stops(executor, side, paired_cancellation=True))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    observation = next(
        r for r in records if r["check"] == "paired_zone_cancellation_observation"
    )
    assert observation["a_peer_retired"] is peer_retires
    assert observation["b_original_pair_preserved"] is True
    assert observation["activation_proven"] is False
    assert exchange.size == Decimal("0")
    assert len(exchange.canceled) == 1
    assert exchange.canceled[0].endswith("-a-TP")
    assert records[-1]["check"] == "cleanup" and records[-1]["flat"] is True
    journal = json.loads(next(tmp_path.glob("ownership-*.json")).read_text())
    assert journal["observations"]["zone_entries"]["net_quantity"] != "0"
    assert len(journal["observations"]["zone_entries"]["children"]) == 4
    assert (
        journal["observations"]["pair_terminal_history"]["a_tp_status"] == "Cancelled"
    )
