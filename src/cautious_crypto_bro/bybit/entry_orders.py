from __future__ import annotations

from decimal import Decimal

from ..domain import (
    ExecutionOrderType,
    ExecutionPlan,
    MarketPrimaryExecutionResult,
    Side,
    TradeExecutionError,
)
from .normalize import format_decimal, parse_decimal


def require_v2(plan: ExecutionPlan) -> None:
    if plan.strategy_version != 2:
        raise TradeExecutionError(
            f"Strategy V{plan.strategy_version} is read-only; only V2 executes"
        )


def validate_staged_market_plan(plan: ExecutionPlan) -> None:
    require_v2(plan)

    if len(plan.orders) != 3:
        raise TradeExecutionError("Strategy V2 requires three entry legs")

    if plan.orders[0].order_type is not ExecutionOrderType.MARKET:
        raise TradeExecutionError("Staged V2 execution requires MARKET E1")

    if any(
        order.order_type is not ExecutionOrderType.LIMIT for order in plan.orders[1:]
    ):
        raise TradeExecutionError("Staged V2 E2/E3 must be LIMIT orders")


def validate_primary_identity(record: dict, request: dict[str, object]) -> None:
    if any(
        record.get(key) != request[key]
        for key in ("symbol", "side", "orderType", "orderLinkId")
    ):
        raise TradeExecutionError("Physical primary fill identity mismatch")
    if (
        parse_decimal(record.get("qty")) != Decimal(str(request["qty"]))
        or record.get("reduceOnly") is not False
    ):
        raise TradeExecutionError("Physical primary fill quantity/role mismatch")


def filled_primary_result(
    record: dict,
    order_id: str,
    *,
    allow_empty: bool = False,
) -> MarketPrimaryExecutionResult | None:
    status = str(record.get("orderStatus") or "")
    filled_quantity = parse_decimal(record.get("cumExecQty"))
    average_price = parse_decimal(record.get("avgPrice"))

    # IOC cancellation can leave an unfilled remainder. A terminal status
    # establishes the final executed quantity used to size E2/E3.
    if (
        filled_quantity > 0
        and average_price > 0
        and status in {"Filled", "Cancelled", "PartiallyFilledCanceled"}
    ):
        return MarketPrimaryExecutionResult(
            order_id=order_id,
            average_fill_price=average_price,
            filled_quantity=filled_quantity,
        )

    if filled_quantity <= 0 and status in {
        "Rejected",
        "Cancelled",
        "Deactivated",
        "PartiallyFilledCanceled",
    }:
        if allow_empty and filled_quantity == 0:
            return MarketPrimaryExecutionResult(order_id, Decimal("0"), Decimal("0"))
        raise TradeExecutionError(f"V2 MARKET E1 ended without a fill: {status}")

    return None


def entry_order_params(plan: ExecutionPlan, index: int) -> dict[str, object]:
    require_v2(plan)
    order = plan.entry_orders[index]
    params: dict[str, object] = {
        "symbol": plan.symbol,
        "side": ("Buy" if plan.side is Side.LONG else "Sell"),
        "orderType": (
            "Market" if order.order_type is ExecutionOrderType.MARKET else "Limit"
        ),
        "qty": format_decimal(order.quantity),
        "stopLoss": format_decimal(plan.stop_loss),
        "tpslMode": "Partial",
        "slOrderType": "Market",
        "timeInForce": (
            "IOC" if order.order_type is ExecutionOrderType.MARKET else "GTC"
        ),
        "positionIdx": 0,
        "reduceOnly": False,
        "orderLinkId": f"ccb-v2-{plan.intent_id.hex[:20]}-{order.name.lower()}",
    }

    if order.order_type is ExecutionOrderType.LIMIT:
        assert order.price is not None
        params["price"] = format_decimal(order.price)

    return params


def parse_batch_result(
    requests: list[dict[str, object]],
    response: dict,
) -> tuple[str, ...]:
    result = response.get("result")
    extension = response.get("retExtInfo")
    results = result.get("list") if isinstance(result, dict) else None
    statuses = extension.get("list") if isinstance(extension, dict) else None

    if (
        not isinstance(results, list)
        or not isinstance(statuses, list)
        or any(not isinstance(item, dict) for item in results + statuses)
        or len(results) != len(requests)
        or len(statuses) != len(requests)
    ):
        raise TradeExecutionError(
            "Bybit batch response is invalid or does not match submitted order count"
        )

    order_ids: list[str] = []
    failures: list[str] = []

    for index, (item_result, status) in enumerate(zip(results, statuses, strict=True)):
        code = status.get("code")
        order_id = item_result.get("orderId")

        if (
            str(code) == "0"
            and isinstance(order_id, str)
            and order_id.strip()
            and order_id not in order_ids
            and item_result.get("orderLinkId", requests[index]["orderLinkId"])
            == requests[index]["orderLinkId"]
        ):
            order_ids.append(order_id)
        else:
            detail = (
                "invalid or duplicate order ID"
                if str(code) == "0"
                else f"{code} {status.get('msg')}"
            )
            failures.append(f"order {index + 1}: {detail}")

    if failures:
        raise TradeExecutionError(
            "Bybit batch partially failed: " + "; ".join(failures)
        )

    return tuple(order_ids)


def validate_market_plan(plan: ExecutionPlan, market_price: Decimal) -> None:
    require_v2(plan)
    current_risk = Decimal("0")

    for order in plan.orders:
        entry_price = (
            market_price
            if order.order_type is ExecutionOrderType.MARKET
            else order.reference_price
        )

        if plan.side is Side.LONG and not (plan.stop_loss < entry_price):
            raise TradeExecutionError("Market moved outside LONG V2 stop geometry")

        if plan.side is Side.SHORT and not (entry_price < plan.stop_loss):
            raise TradeExecutionError("Market moved outside SHORT V2 stop geometry")

        current_risk += order.quantity * abs(entry_price - plan.stop_loss)

    if current_risk > plan.policy.risk_budget_usdt:
        raise TradeExecutionError(
            "Market moved enough that Strategy V2 execution "
            "would exceed the risk budget: "
            f"quote={market_price}, risk={current_risk}, "
            f"budget={plan.policy.risk_budget_usdt}"
        )
