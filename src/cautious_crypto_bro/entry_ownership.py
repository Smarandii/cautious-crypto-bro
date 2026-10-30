"""Recover acknowledgements by durable request identity, without order replay."""

from .domain import ExecutionPlan, TradeExecutionError
from .ports import AccountGateway, ExecutionCoordinatorStore, PositionSupervisorStore
from .zone_ledger import ZoneOrder, zone_group_id


async def recover_entry_bindings(
    plan: ExecutionPlan,
    store: ExecutionCoordinatorStore | PositionSupervisorStore,
    executor: AccountGateway,
) -> None:
    group = zone_group_id(plan.symbol, plan.side)
    known = {order.order_link_id for order in await store.zone_bound_orders(group)}
    prefix = f"ccb-v2-{plan.intent_id.hex[:20]}-"
    for child in plan.entry_orders:
        link = prefix + child.name.lower()
        if link in known:
            continue
        order = await executor.strategy_order(plan.symbol, link)
        if order is None:
            continue
        if (
            order.symbol != plan.symbol
            or order.order_link_id != link
            or order.quantity != child.quantity
            or order.side is not plan.side
            or order.reduce_only
            or order.close_on_trigger
            or order.is_protective
        ):
            raise TradeExecutionError("Physical entry recovery identity mismatch")
        await store.bind_zone_order(
            ZoneOrder(
                zone_id=plan.intent_id,
                order_id=order.order_id,
                order_link_id=link,
                role="ENTRY",
                quantity=child.quantity,
            )
        )
