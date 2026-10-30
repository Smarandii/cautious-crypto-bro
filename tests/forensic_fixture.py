"""Explicit terminal requests for synthetic archive scenarios with full fills."""

from datetime import UTC, datetime
from decimal import Decimal


def settled_entry_fixture(intents, fills, quantities, *, fee="0"):
    history = []
    latest = max(int(fill["execTime"]) for fill in fills) + 86_400_000

    def stamp(milliseconds):
        return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat()

    for item in intents:
        descriptions = []
        for index, order_id in enumerate(item["entry_order_ids"], start=1):
            quantity = quantities[order_id]
            trades = [fill for fill in fills if fill["orderId"] == order_id]
            assert sum(Decimal(fill["execQty"]) for fill in trades) == Decimal(quantity)
            link = f"fixture-{order_id}-e{index}"
            side = "Buy" if item["intent"]["side"] == "LONG" else "Sell"
            for fill in trades:
                fill.update(
                    symbol=item["intent"]["symbol"],
                    side=side,
                    orderLinkId=link,
                    execFee=fee,
                )
            descriptions.append(
                {
                    "order_id": order_id,
                    "order_link_id": link,
                    "leg_name": f"E{index}",
                    "quantity": quantity,
                }
            )
            history.append(
                {
                    "orderId": order_id,
                    "orderLinkId": link,
                    "symbol": item["intent"]["symbol"],
                    "side": side,
                    "qty": quantity,
                    "cumExecQty": quantity,
                    "reduceOnly": False,
                    "orderStatus": "Filled",
                    "updatedTime": str(latest - 1000),
                }
            )
        item["entry_evidence"] = {
            "entry_identity_complete": True,
            "entry_orders_settled": True,
            "entry_orders": descriptions,
        }
    return {
        "metadata.json": [
            {
                "entry_order_history_coverage_start": stamp(0),
                "entry_order_history_as_of": stamp(latest),
                "open_entry_orders_as_of": stamp(latest + 1000),
                "created_at": stamp(latest + 2000),
            }
        ],
        "bybit/entry_order_history.jsonl": history,
        "bybit/open_entry_orders.jsonl": [],
    }
