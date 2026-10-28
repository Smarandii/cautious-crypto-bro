"""Run the full amended-pair observation through the real HTTP adapter."""

import asyncio
import json
from decimal import Decimal

import httpx
import pytest
from test_demo_e2e import demo
from test_remaining_concerns import executor_for
from test_zone_probe_integration import PairedExchange

from cautious_crypto_bro.domain import Side

pytestmark = pytest.mark.integration


class AmendedPairExchange(PairedExchange):
    def __init__(self, side, peer_retires, *, lost_ack=False, fault=None):
        super().__init__(side, peer_retires)
        self.lost_ack = lost_ack
        self.fault = fault
        self.amended = []

    def handle(self, request):
        path = request.url.path
        body = (
            json.loads(request.content) if request.content else dict(request.url.params)
        )
        if path == "/v5/order/amend":
            self.requests.append((path, body))
            self.amended.append(body["orderId"])
            target = next(
                o for o in self.orders.values() if o["orderId"] == body["orderId"]
            )
            target["triggerPrice"] = body["triggerPrice"]
            if self.fault == "b_drift":
                b = next(
                    o
                    for o in self.orders.values()
                    if o.get("parentOrderLinkId", "").endswith("-b")
                    and o.get("stopOrderType") == "PartialStopLoss"
                )
                b["triggerPrice"] = str(Decimal(b["triggerPrice"]) + 1)
            if self.fault == "foreign":
                self.orders["foreign"] = {
                    **target,
                    "orderId": "foreign",
                    "orderLinkId": "foreign",
                    "parentOrderLinkId": "foreign",
                }
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "orderId": "foreign"
                        if self.fault == "bad_ack"
                        else target["orderId"]
                    },
                },
            )
        response = super().handle(request)
        if (
            self.lost_ack
            and path == "/v5/order/cancel"
            and body["orderId"].endswith("-a-TP")
        ):
            raise httpx.ReadTimeout(
                "Cancellation accepted, response lost", request=request
            )
        return response


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("peer_retires", [False, True])
@pytest.mark.parametrize("lost_ack", [False, True])
def test_amended_pair_observes_actual_retirement_without_replay(
    tmp_path, monkeypatch, capsys, side, peer_retires, lost_ack
):
    monkeypatch.setenv("CCB_PROBE_EVIDENCE_DIR", str(tmp_path))
    exchange = AmendedPairExchange(side, peer_retires, lost_ack=lost_ack)
    with executor_for(exchange) as executor:
        monkeypatch.setattr(demo, "executor", lambda: executor)
        asyncio.run(demo.observe_zone_amendment(side, paired_cancellation=True))
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    observation = next(
        r
        for r in records
        if r["check"] == "zone_amendment_pair_cancellation_observation"
    )
    assert observation["a_peer_retired"] is peer_retires
    assert observation["cancellation_acknowledged"] is not lost_ack
    assert observation["amendment_verified"] and observation["net_quantity_unchanged"]
    assert (
        observation["b_original_pair_preserved"]
        and observation["activation_proven"] is False
    )
    assert observation["settling_seconds"] == 2
    assert len(exchange.amended) == len(exchange.canceled) == 1
    assert exchange.amended[0].endswith("-a-SL") and exchange.canceled[0].endswith(
        "-a-TP"
    )
    assert (
        exchange.size == 0
        and records[-1]["check"] == "cleanup"
        and records[-1]["flat"] is True
    )
    journal = json.loads(next(tmp_path.glob("ownership-*.json")).read_text())
    amended = journal["observations"]["after_amendment"]
    after = journal["observations"]["after_pair_cancel"]
    assert amended["net_quantity"] == after["net_quantity"]
    assert [o for o in amended["children"] if o["parent_link"].endswith("-b")] == [
        o for o in after["children"] if o["parent_link"].endswith("-b")
    ]
    history = journal["observations"]["pair_terminal_history"]
    assert history["a_sl_status"] == ("Cancelled" if peer_retires else "Untriggered")


@pytest.mark.parametrize("fault", ["b_drift", "foreign", "bad_ack"])
def test_amended_pair_refuses_drift_or_unknown_evidence_before_cancel(
    tmp_path, monkeypatch, capsys, fault
):
    monkeypatch.setenv("CCB_PROBE_EVIDENCE_DIR", str(tmp_path))
    exchange = AmendedPairExchange(Side.LONG, True, fault=fault)
    with executor_for(exchange) as executor:
        monkeypatch.setattr(demo, "executor", lambda: executor)
        with pytest.raises((AssertionError, RuntimeError)):
            asyncio.run(
                demo.observe_zone_amendment(Side.LONG, paired_cancellation=True)
            )
    assert len(exchange.amended) == 1 and exchange.canceled == []
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert not any(
        r["check"] == "zone_amendment_pair_cancellation_observation" for r in records
    )
    if fault == "foreign":
        assert exchange.size > 0 and not any(r["check"] == "cleanup" for r in records)
        assert len([p for p, b in exchange.requests if p == "/v5/order/create"]) == 2
    else:
        assert exchange.size == 0 and records[-1]["check"] == "cleanup"
