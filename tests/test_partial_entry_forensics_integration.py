"""Accepted entry attempts must survive the real store -> export -> PnL boundary."""

import asyncio
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from test_quantity_limited_entry_integration import QuantityLimitedExchange
from test_remaining_concerns import executor_for, prepare

from cautious_crypto_bro.bybit.client import DEMO_BASE_URL, BybitClient
from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    IntentStatus,
    OpenRelation,
    Side,
    StrategyStatus,
)
from cautious_crypto_bro.forensic_pnl import reconcile
from cautious_crypto_bro.storage import IntentStore
from cautious_crypto_bro.zone_ledger import ZoneOrder, zone_group_id

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import export_forensic_replay as exporter  # noqa: E402

pytestmark = pytest.mark.integration


async def executed_attempt(
    database,
    side,
    failure,
    bind_close,
    *,
    settle_entries=True,
    fill_scale_ins=False,
    participation=False,
    fill_mode="full",
    entry_type=EntryType.MARKET,
    exchange_class=QuantityLimitedExchange,
    native_stop=False,
):
    exchange = exchange_class(database)
    exchange.side = side
    exchange.mark = Decimal("100")
    exchange.max_market_qty = exchange.max_limit_qty = Decimal("2")
    exchange.failure = failure
    exchange.fill_mode = fill_mode
    entry = Entry(type=EntryType.MARKET)
    if entry_type is EntryType.LIMIT:
        entry = Entry(type=entry_type, price=100)
    elif entry_type is EntryType.RANGE:
        entry = Entry(
            type=entry_type,
            range_low=98 if side is Side.LONG else 100,
            range_high=100 if side is Side.LONG else 102,
        )
    with executor_for(exchange) as executor:
        store, intent, _, coordinator, _ = await prepare(
            database,
            exchange,
            executor,
            side=side,
            mode=ApprovalMode.AUTO,
            relation=OpenRelation.NEW,
            entry=entry,
        )
        if participation:
            saved = await store.get_execution_plan(intent.intent_id)
            strategy = saved.policy.strategy_v2.model_copy(
                update={
                    "long_participation_arm": "take",
                    "long_participation_momentum": "unavailable",
                }
            )
            policy = saved.policy.model_copy(update={"strategy_v2": strategy})
            await store.update_execution_plan(
                saved.model_copy(update={"policy": policy})
            )
        outcome = await coordinator.execute_intent(
            intent.intent_id, approval_mode=ApprovalMode.AUTO
        )
    assert outcome.status is (IntentStatus.FAILED if failure else IntentStatus.EXECUTED)
    records = await store.get_active_position_strategies()
    assert len(records) == 1, outcome.message
    state, plan = records[0]
    if failure:
        assert state.status is StrategyStatus.UNCERTAIN
    size = exchange.size
    assert size > 0
    for order in exchange.orders.values():
        if order["orderStatus"] == "New":
            if fill_scale_ins:
                exchange.fill(order["orderLinkId"])
            elif settle_entries:
                order["orderStatus"] = "Cancelled"
    await close_attempt(store, exchange, plan, side, bind_close, native_stop)
    # Reopen the real store; no exporter may change the failed claim/quarantine.
    restarted = IntentStore(database)
    restored = await restarted.get_execution_plan(intent.intent_id)
    assert restored == plan
    bindings = await restarted.zone_bound_orders(zone_group_id(plan.symbol, side))
    return exchange, plan, state, bindings


async def close_attempt(store, exchange, plan, side, bind_close, native_stop):
    exchange.fill_price = (
        plan.stop_loss
        if native_stop
        else Decimal("110")
        if side is Side.LONG
        else Decimal("90")
    )
    parents = [
        b
        for b in await store.zone_bound_orders(zone_group_id(plan.symbol, side))
        if b.role == "ENTRY"
    ]
    quantities = (
        [
            (b.order_link_id, Decimal(exchange.orders[b.order_link_id]["cumExecQty"]))
            for b in parents
        ]
        if native_stop
        else [("", exchange.size)]
    )
    assert sum(size for _, size in quantities) == exchange.size
    for index, (parent_link, size) in enumerate(quantities):
        if size == 0:
            continue
        closing_link = f"ccb-v2-{plan.intent_id.hex[:20]}-x{index}"
        assert len(closing_link) <= 36
        response = exchange.create(
            {
                "symbol": plan.symbol,
                "side": "Sell" if side is Side.LONG else "Buy",
                "orderType": "Market",
                "qty": str(size),
                "reduceOnly": True,
                "orderLinkId": closing_link,
            }
        )
        if native_stop:
            closing = exchange.orders[closing_link]
            closing.update(
                orderLinkId="",
                parentOrderLinkId=parent_link,
                stopOrderType="PartialStopLoss",
                triggerPrice=str(plan.stop_loss),
            )
        if bind_close:
            await store.bind_zone_order(
                ZoneOrder(
                    zone_id=plan.intent_id,
                    order_id=response["result"]["orderId"],
                    order_link_id="" if native_stop else closing_link,
                    parent_order_link_id=parent_link,
                    role="STOP" if native_stop else "EXIT",
                    quantity=size,
                )
            )


def export_attempt(database, exchange, bundle, monkeypatch, edit_pages=None):
    timestamp = int(datetime.now(UTC).timestamp() * 1000)
    executions = []
    for index, order in enumerate(exchange.orders.values()):
        quantity = Decimal(order["cumExecQty"])
        if quantity == 0:
            continue
        price = Decimal(order["avgPrice"])
        executions.append(
            {
                "execId": f"execution-{index}",
                "execType": "Trade",
                "symbol": order["symbol"],
                "side": order["side"],
                "orderId": order["orderId"],
                "orderLinkId": order["orderLinkId"],
                "execTime": str(timestamp + index),
                "execQty": str(quantity),
                "execPrice": str(price),
                "execFee": str(quantity * price / 1000),
                "closedSize": str(quantity if order["reduceOnly"] else 0),
            }
        )
    entries = [e for e in executions if Decimal(e["closedSize"]) == 0]
    closes = [e for e in executions if Decimal(e["closedSize"]) > 0]
    close = closes[0]
    entry_quantity = sum(Decimal(e["execQty"]) for e in entries)
    entry_fees = sum(Decimal(e["execFee"]) for e in entries)
    close_fee = sum(Decimal(e["execFee"]) for e in closes)
    funding = Decimal("-0.3")
    close_price = Decimal(close["execPrice"])
    gross = sum(
        Decimal(e["execQty"])
        * (close_price - Decimal(e["execPrice"]))
        * (1 if e["side"] == "Buy" else -1)
        for e in entries
    )
    net = gross - entry_fees - close_fee + funding
    pnl = {
        "orderId": close["orderId"],
        "symbol": "BTCUSDT",
        "side": close["side"],
        "closedSize": str(entry_quantity),
        "closedPnl": str(net),
        "openFee": str(entry_fees),
        "closeFee": str(close_fee),
        "updatedTime": str(timestamp + 100),
    }
    pnl_rows = []
    for item in closes:
        fraction = Decimal(item["execQty"]) / entry_quantity
        pnl_rows.append(
            pnl
            | {
                "orderId": item["orderId"],
                "closedSize": item["execQty"],
                "closedPnl": str(
                    (gross - entry_fees + funding) * fraction - Decimal(item["execFee"])
                ),
                "openFee": str(entry_fees * fraction),
                "closeFee": item["execFee"],
            }
        )
    pages = {
        "/v5/execution/list": executions,
        "/v5/order/history": list(exchange.orders.values()),
        "/v5/order/realtime": [
            o for o in exchange.orders.values() if o["orderStatus"] == "New"
        ],
        "/v5/position/list": [],
        "/v5/account/transaction-log": [
            {
                "type": "SETTLEMENT",
                "symbol": "BTCUSDT",
                "transactionTime": str(timestamp + 50),
                "funding": str(funding),
            }
        ],
        "/v5/position/closed-pnl": pnl_rows,
    }
    if edit_pages is not None:
        edit_pages(pages)

    def respond(request):
        assert request.method == "GET", "Forensic export mutated the exchange"
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "time": timestamp})
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": pages[request.url.path], "nextPageCursor": ""},
            },
        )

    client = BybitClient(DEMO_BASE_URL, "synthetic-key", "synthetic-secret")
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url=DEMO_BASE_URL, transport=httpx.MockTransport(respond)
    )
    monkeypatch.setattr(exporter, "BybitClient", lambda **kwargs: client)
    before = database.read_bytes()
    try:
        exporter.export_bundle(
            bundle,
            skip_market_candles=True,
            access=exporter.ForensicAccess(
                database, "synthetic-key", "synthetic-secret"
            ),
        )
    finally:
        client.close()
    assert database.read_bytes() == before
    return executions, entries, pnl, funding


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("failure", ["primary_ack", "batch_ack", "batch_item", ""])
def test_partial_attempt_fills_and_fees_remain_attributable_without_false_success(
    tmp_path, monkeypatch, side, failure
):
    database = tmp_path / "state.sqlite3"
    exchange, plan, state, bindings = asyncio.run(
        executed_attempt(database, side, failure, True)
    )
    bundle = tmp_path / "forensic.zip"
    executions, entries, pnl, funding = export_attempt(
        database, exchange, bundle, monkeypatch
    )
    with zipfile.ZipFile(bundle) as archive:
        lineage = json.loads(archive.read("forensic/analysis/trade_lineage.jsonl"))[
            "intents"
        ]
        assert len(lineage) == 1
        item = lineage[0]
        assert item["status"] == ("FAILED" if failure else "EXECUTED")
        assert set(item["entry_order_ids"]) == {
            b.order_id for b in bindings if b.role == "ENTRY"
        }
        assert {e["execId"] for e in item["bybit"]["executions"]} == {
            e["execId"] for e in entries
        }
        assert {e["execId"] for e in item["bybit"]["plan_executions"]} == {
            e["execId"] for e in executions
        }
        assert item["entry_evidence"]["strategy_status"] == state.status.value
    positions = reconcile(bundle)
    assert len(positions) == 1
    position = positions[0]
    assert position["attempt_status"] == item["status"]
    assert position["fill_pattern"] == "E1"
    assert position["entry_qty"] == pytest.approx(
        sum(float(e["execQty"]) for e in entries)
    )
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["fees"] == pytest.approx(
        float(pnl["openFee"]) + float(pnl["closeFee"])
    )
    assert position["funding"] == pytest.approx(float(funding))
    assert position["complete"] is (failure in {"", "batch_ack"})
    from cautious_crypto_bro.dashboard_metrics import public_trade_history

    feed = public_trade_history(positions, available=True)
    assert feed["total_completed"] == int(position["complete"])
    assert str(plan.intent_id) not in json.dumps(feed)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT status FROM intents").fetchone()[0] == item["status"]
        assert (
            db.execute("SELECT status FROM position_strategies").fetchone()[0]
            == state.status.value
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_uncertain_attempt_never_guesses_unbound_closing_order_ownership(
    tmp_path, monkeypatch, side
):
    database = tmp_path / "state.sqlite3"
    exchange, _, _, _ = asyncio.run(
        executed_attempt(database, side, "batch_ack", False)
    )
    bundle = tmp_path / "unbound.zip"
    export_attempt(database, exchange, bundle, monkeypatch)
    position = reconcile(bundle)[0]
    assert position["attempt_status"] == "FAILED"
    assert position["entry_identity_complete"] is True
    assert position["complete"] is False
    assert position["ambiguous_rows"] == 1
    assert position["closed_qty"] == 0 and position["net_pnl"] == 0


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_primary_slice_aggregation_stays_separate_from_filled_scale_ins(
    tmp_path, monkeypatch, side
):
    database = tmp_path / "scale-ins.sqlite3"
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(database, side, "", True, fill_scale_ins=True)
    )
    bundle = tmp_path / "scale-ins.zip"
    _, entries, pnl, _ = export_attempt(database, exchange, bundle, monkeypatch)
    with zipfile.ZipFile(bundle) as archive:
        item = json.loads(archive.read("forensic/analysis/trade_lineage.jsonl"))[
            "intents"
        ][0]
    primary = item["bybit"]["executions"]
    assert len(primary) < len(entries)
    assert sum(Decimal(e["execQty"]) for e in primary) == plan.orders[0].quantity
    assert all(e["orderLinkId"].rsplit("-", 1)[-1].startswith("e1c") for e in primary)
    position = reconcile(bundle)[0]
    assert position["fill_pattern"] == "E1+E2+E3"
    assert position["complete"] is True
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))


@pytest.mark.parametrize(
    "gap",
    [
        "pending",
        "missing_history",
        "missing_execution",
        "missing_fee",
        "conflicting_link",
    ],
)
def test_unsettled_or_incomplete_entry_evidence_cannot_become_complete_pnl(
    tmp_path, monkeypatch, gap
):
    database = tmp_path / "incomplete.sqlite3"
    exchange, _, _, _ = asyncio.run(
        executed_attempt(
            database,
            Side.LONG,
            "" if gap == "pending" else "batch_ack",
            True,
            settle_entries=gap != "pending",
        )
    )

    def edit(pages):
        if gap == "missing_history":
            pages["/v5/order/history"] = pages["/v5/order/history"][1:]
        elif gap == "missing_execution":
            pages["/v5/execution/list"] = pages["/v5/execution/list"][1:]
        elif gap == "missing_fee":
            del pages["/v5/execution/list"][0]["execFee"]
        elif gap == "conflicting_link":
            pages["/v5/order/history"][0] = pages["/v5/order/history"][0] | {
                "orderLinkId": "foreign-link"
            }

    bundle = tmp_path / "incomplete.zip"
    export_attempt(database, exchange, bundle, monkeypatch, edit)
    position = reconcile(bundle)[0]
    assert position["entry_identity_complete"] is True
    assert position["entry_orders_settled"] is False
    assert position["complete"] is False


def test_binding_cannot_claim_an_entry_quantity_outside_the_saved_plan(
    tmp_path, monkeypatch
):
    database = tmp_path / "corrupt.sqlite3"
    exchange, _, _, _ = asyncio.run(
        executed_attempt(database, Side.LONG, "batch_ack", True)
    )
    with closing(sqlite3.connect(database)) as db:
        key, payload = db.execute(
            "SELECT order_id,payload_json FROM zone_orders WHERE json_extract(payload_json,'$.role')='ENTRY' LIMIT 1"
        ).fetchone()
        body = json.loads(payload)
        body["quantity"] = str(Decimal(body["quantity"]) * 2)
        db.execute(
            "UPDATE zone_orders SET payload_json=? WHERE order_id=?",
            (json.dumps(body), key),
        )
        db.commit()
    bundle = tmp_path / "corrupt.zip"
    with pytest.raises(
        ValueError, match="inconsistent with its frozen physical request"
    ):
        export_attempt(database, exchange, bundle, monkeypatch)
    assert not bundle.exists()


@pytest.mark.parametrize("failure", ["primary_ack", "batch_ack"])
def test_failed_attempt_with_fills_is_not_classified_as_failed_before_fill(
    tmp_path, monkeypatch, failure
):
    from export_dashboard_data import _participation_cohort

    database = tmp_path / "cohort.sqlite3"
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(database, Side.LONG, failure, True, participation=True)
    )
    bundle = tmp_path / "cohort.zip"
    export_attempt(database, exchange, bundle, monkeypatch)
    positions = reconcile(bundle)
    assert positions[0]["long_participation_arm"] == "take"
    with zipfile.ZipFile(bundle) as archive:
        assignment = json.loads(
            archive.read("forensic/analysis/long_participation_assignments.jsonl")
        )["assignments"][0]
    assert assignment["status"] == "FAILED" and assignment["bybit_order_ids"]
    summary = _participation_cohort(
        [
            {
                "intent_id": str(plan.intent_id),
                "arm": "take",
                "status": "FAILED",
                "order_ids": [],
                "approval_mode": "AUTO",
                "long_participation_momentum": "unavailable",
            }
        ],
        positions,
        skip_fraction=0.5,
        forensic_available=True,
        reviewable=True,
    )
    assert summary["take_filled_position_count"] == 1
    assert summary["take_unresolved_count"] == (1 if failure == "primary_ack" else 0)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_partial_ioc_children_report_actual_fills_instead_of_submitted_quantities(
    tmp_path, monkeypatch, side
):
    database = tmp_path / "partial-ioc.sqlite3"
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(database, side, "", True, fill_mode="partial")
    )
    bundle = tmp_path / "partial-ioc.zip"
    export_attempt(database, exchange, bundle, monkeypatch)
    position = reconcile(bundle)[0]
    submitted = sum(s.quantity for s in plan.entry_slices if s.leg_name == "E1")
    assert position["entry_qty"] == pytest.approx(float(plan.orders[0].quantity))
    assert position["entry_qty"] < float(submitted)
    assert position["entry_orders_settled"] is True
    assert position["complete"] is True


class RejectedPrimaryLimitExchange(QuantityLimitedExchange):
    def create(self, body):
        if not body.get("reduceOnly", False) and body["orderType"] == "Limit":
            if body["orderLinkId"].rsplit("-", 1)[-1].startswith("e1"):
                return {
                    "retCode": 10001,
                    "retMsg": "Synthetic primary rejection",
                    "result": {},
                }
            response = super().create(body)
            if response["retCode"] == 0:
                self.fill(body["orderLinkId"])
            return response
        return super().create(body)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("entry_type", [EntryType.LIMIT, EntryType.RANGE])
def test_filled_scale_ins_survive_export_when_primary_limit_was_rejected(
    tmp_path, monkeypatch, side, entry_type
):
    database = tmp_path / "no-primary.sqlite3"
    exchange, _, _, _ = asyncio.run(
        executed_attempt(
            database,
            side,
            "primary_rejected",
            True,
            entry_type=entry_type,
            exchange_class=RejectedPrimaryLimitExchange,
        )
    )
    bundle = tmp_path / "no-primary.zip"
    _, entries, pnl, _ = export_attempt(database, exchange, bundle, monkeypatch)
    with zipfile.ZipFile(bundle) as archive:
        item = json.loads(archive.read("forensic/analysis/trade_lineage.jsonl"))[
            "intents"
        ][0]
    assert item["status"] == "FAILED" and not item["bybit"]["executions"]
    assert item["bybit"]["plan_executions"]
    position = reconcile(bundle)[0]
    assert position["fill_pattern"] == "E2+E3"
    assert position["entry_qty"] == pytest.approx(
        sum(float(e["execQty"]) for e in entries)
    )
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["entry_identity_complete"] is False
    assert position["complete"] is False


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_bound_native_stop_keeps_partial_attempt_fees_and_close_identity(
    tmp_path, monkeypatch, side
):
    database = tmp_path / "native-stop.sqlite3"
    exchange, _, _, bindings = asyncio.run(
        executed_attempt(database, side, "primary_ack", True, native_stop=True)
    )
    bundle = tmp_path / "native-stop.zip"
    _, _, pnl, _ = export_attempt(database, exchange, bundle, monkeypatch)
    assert any(
        b.role == "STOP" and b.parent_order_link_id and not b.order_link_id
        for b in bindings
    )
    position = reconcile(bundle)[0]
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["fees"] == pytest.approx(
        float(pnl["openFee"]) + float(pnl["closeFee"])
    )
    assert position["ambiguous_rows"] == 0
    assert position["complete"] is False


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("failure", ["primary_ack", "batch_ack", "batch_item"])
def test_exchange_parent_links_attribute_native_stops_without_requiring_closing_bindings(
    tmp_path, monkeypatch, side, failure
):
    database = tmp_path / "attached-stop.sqlite3"
    exchange, _, state, bindings = asyncio.run(
        executed_attempt(database, side, failure, False, native_stop=True)
    )
    assert all(b.role == "ENTRY" for b in bindings)
    bundle = tmp_path / "attached-stop.zip"
    executions, _, pnl, _ = export_attempt(database, exchange, bundle, monkeypatch)
    position = reconcile(bundle)[0]
    assert position["net_pnl"] == pytest.approx(float(pnl["closedPnl"]))
    assert position["net_pnl"] < 0
    assert position["fees"] == pytest.approx(
        float(pnl["openFee"]) + float(pnl["closeFee"])
    )
    assert position["complete"] is (failure == "batch_ack")
    assert position["strategy_status"] == state.status.value == "UNCERTAIN"
    assert position["ambiguous_rows"] == 0
    with zipfile.ZipFile(bundle) as archive:
        item = json.loads(archive.read("forensic/analysis/trade_lineage.jsonl"))[
            "intents"
        ][0]
    assert {e["execId"] for e in item["bybit"]["plan_executions"]} == {
        e["execId"] for e in executions
    }
    assert item["entry_evidence"]["attached_closing_orders"]


@pytest.mark.parametrize(
    "conflict", ["parent", "quantity", "side", "client_link", "closing_role"]
)
def test_attached_stop_requires_consistent_parent_quantity_side_and_closing_role(
    tmp_path, monkeypatch, conflict
):
    database = tmp_path / "conflicting-stop.sqlite3"
    exchange, plan, _, _ = asyncio.run(
        executed_attempt(database, Side.LONG, "batch_ack", False, native_stop=True)
    )

    def edit(pages):
        closing = next(
            row
            for row in pages["/v5/order/history"]
            if row.get("stopOrderType") == "PartialStopLoss"
        )
        if conflict == "parent":
            closing["parentOrderLinkId"] = "foreign-entry"
        elif conflict == "quantity":
            closing["qty"] = str(Decimal(closing["qty"]) * 2)
        elif conflict == "side":
            closing["side"] = "Buy"
        elif conflict == "client_link":
            closing["orderLinkId"] = f"ccb-v2-{plan.intent_id.hex[:20]}-x9"
        else:
            closing["reduceOnly"] = closing["closeOnTrigger"] = False

    bundle = tmp_path / "conflicting-stop.zip"
    export_attempt(database, exchange, bundle, monkeypatch, edit)
    position = reconcile(bundle)[0]
    assert position["complete"] is False
    assert position["ambiguous_rows"] == 1
