from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from ..domain import (
    AccountOrder,
    AccountStateSummary,
    ExecutionPlan,
    Side,
    SourceMessage,
    StoreError,
    TradingIntent,
)
from ..zone_ledger import ZoneExecution, ZoneOrder
from ._connection import _Store, sqlite_connection


def _order_matches_binding(order: AccountOrder, binding: ZoneOrder, side: Side) -> bool:
    opening = binding.role == "ENTRY"
    expected_side = (
        side if opening else (Side.SHORT if side is Side.LONG else Side.LONG)
    )
    closing_order = order.reduce_only or order.close_on_trigger or order.is_protective
    return (
        order.order_link_id == binding.order_link_id
        and order.side is expected_side
        and order.quantity == binding.quantity
        and Decimal("0") <= order.remaining_quantity <= order.quantity
        and closing_order != opening
    )


def _zone_balances(
    zones: list[tuple[str, str]],
    orders: list[ZoneOrder],
    executions: list[ZoneExecution],
) -> tuple[list[dict], list[str]]:
    balances = []
    issues = []
    for zone_id, plan_json in zones:
        plan = ExecutionPlan.model_validate_json(plan_json)
        owned = {
            order.order_id: order for order in orders if str(order.zone_id) == zone_id
        }
        fills = [fill for fill in executions if fill.order_id in owned]
        entered = sum(
            (fill.quantity for fill in fills if owned[fill.order_id].role == "ENTRY"),
            Decimal("0"),
        )
        closed = sum(
            (fill.quantity for fill in fills if owned[fill.order_id].role != "ENTRY"),
            Decimal("0"),
        )
        if closed > entered:
            issues.append("ZONE_OVERCLOSED")
        for order in owned.values():
            if (
                sum(
                    (
                        fill.quantity
                        for fill in fills
                        if fill.order_id == order.order_id
                    ),
                    Decimal("0"),
                )
                > order.quantity
            ):
                issues.append("ORDER_OVERFILLED")
        balances.append(
            {
                "zone_id": zone_id,
                "risk_budget_usdt": plan.policy.risk_budget_usdt,
                "entered_quantity": entered,
                "closed_quantity": closed,
                "remaining_quantity": entered - closed,
            }
        )
    return balances, issues


class ZoneLedgerRepositoryImpl(_Store):
    async def register_zone(self, group_id: UUID, intent_id: UUID) -> None:
        """Freeze a saved plan and lineage; do not enroll it in live supervision."""
        async with sqlite_connection(self._database_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT p.payload_json,i.payload_json FROM execution_plans p "
                "JOIN intents i USING(intent_id) WHERE p.intent_id=?",
                (str(intent_id),),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoreError("Zone requires a saved intent and plan")
            plan = ExecutionPlan.model_validate_json(row[0])
            intent = TradingIntent.model_validate_json(row[1])
            if plan.intent_id != intent_id or intent.intent_id != intent_id:
                raise StoreError("Zone plan identity does not match its intent")
            if plan.symbol != intent.symbol or plan.side is not intent.side:
                raise StoreError("Zone plan geometry does not match its intent")
            if plan.strategy_version != 2 or plan.policy.risk_budget_usdt <= 0:
                raise StoreError("Zone requires a positive frozen Strategy V2 budget")
            await db.execute(
                "INSERT OR IGNORE INTO zone_groups(group_id,symbol,side) VALUES (?,?,?)",
                (str(group_id), plan.symbol, plan.side.value),
            )
            cursor = await db.execute(
                "SELECT symbol,side FROM zone_groups WHERE group_id=?", (str(group_id),)
            )
            group_row = await cursor.fetchone()
            if group_row is None or tuple(group_row) != (plan.symbol, plan.side.value):
                raise StoreError("Zone group symbol/side conflict")
            values = (
                str(group_id),
                plan.model_dump_json(),
                intent.source.model_dump_json(),
            )
            cursor = await db.execute(
                "SELECT group_id,plan_json,source_json FROM zone_allocations WHERE zone_id=?",
                (str(intent_id),),
            )
            old = await cursor.fetchone()
            if old is not None:
                if (
                    old[0] != str(group_id)
                    or ExecutionPlan.model_validate_json(old[1]) != plan
                    or SourceMessage.model_validate_json(old[2]) != intent.source
                ):
                    raise StoreError("Frozen zone allocation cannot be replaced")
            else:
                await db.execute(
                    "INSERT INTO zone_allocations(zone_id,group_id,plan_json,source_json) VALUES (?,?,?,?)",
                    (str(intent_id), *values),
                )
            await db.commit()

    async def bind_zone_order(self, order: ZoneOrder) -> None:
        """Bind a confirmed order identity once; prefixes never infer ownership."""
        async with sqlite_connection(self._database_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT 1 FROM zone_allocations WHERE zone_id=?", (str(order.zone_id),)
            )
            if await cursor.fetchone() is None:
                raise StoreError("Order has no registered zone")
            cursor = await db.execute(
                "SELECT payload_json FROM zone_orders WHERE order_id=? OR order_link_id=?",
                (order.order_id, order.order_link_id),
            )
            rows = list(await cursor.fetchall())
            if rows:
                if len(rows) != 1 or ZoneOrder.model_validate_json(rows[0][0]) != order:
                    raise StoreError(
                        "Exchange order identity already belongs to another allocation"
                    )
            else:
                await db.execute(
                    "INSERT INTO zone_orders(order_id,zone_id,order_link_id,payload_json) VALUES (?,?,?,?)",
                    (
                        order.order_id,
                        str(order.zone_id),
                        order.order_link_id,
                        order.model_dump_json(),
                    ),
                )
            await db.commit()

    async def record_zone_execution(self, execution: ZoneExecution) -> None:
        async with sqlite_connection(self._database_path) as db:
            await db.execute("PRAGMA foreign_keys=ON")
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT o.payload_json,g.side FROM zone_orders o "
                "JOIN zone_allocations z USING(zone_id) JOIN zone_groups g USING(group_id) "
                "WHERE o.order_id=?",
                (execution.order_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                raise StoreError("Execution has no confirmed zone order owner")
            order = ZoneOrder.model_validate_json(row[0])
            opening = order.role == "ENTRY"
            expected_side = (
                Side(row[1])
                if opening
                else (Side.SHORT if row[1] == "LONG" else Side.LONG)
            )
            expected_closed = Decimal("0") if opening else execution.quantity
            if (
                execution.side is not expected_side
                or execution.closed_quantity != expected_closed
            ):
                raise StoreError(
                    "Execution direction/closed quantity conflicts with its order role"
                )
            cursor = await db.execute(
                "SELECT payload_json FROM zone_executions WHERE execution_id=?",
                (execution.execution_id,),
            )
            old = await cursor.fetchone()
            if old is not None:
                if ZoneExecution.model_validate_json(old[0]) != execution:
                    raise StoreError(
                        "Exchange execution identity conflicts with retained evidence"
                    )
            else:
                await db.execute(
                    "INSERT INTO zone_executions(execution_id,order_id,payload_json) VALUES (?,?,?)",
                    (
                        execution.execution_id,
                        execution.order_id,
                        execution.model_dump_json(),
                    ),
                )
            await db.commit()

    async def zone_group_snapshot(
        self, group_id: UUID, account: AccountStateSummary | None = None
    ) -> dict:
        """Quantity evidence only. No protection or mutation readiness is implied."""
        async with sqlite_connection(self._database_path) as db:
            await db.execute("PRAGMA query_only=ON")
            await db.execute("BEGIN")
            cursor = await db.execute(
                "SELECT symbol,side FROM zone_groups WHERE group_id=?", (str(group_id),)
            )
            group = await cursor.fetchone()
            if group is None:
                raise StoreError("Unknown zone group")
            symbol, side = group
            cursor = await db.execute(
                "SELECT zone_id,plan_json FROM zone_allocations WHERE group_id=? ORDER BY zone_id",
                (str(group_id),),
            )
            zones = [(row[0], row[1]) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT o.payload_json FROM zone_orders o JOIN zone_allocations z USING(zone_id) WHERE z.group_id=?",
                (str(group_id),),
            )
            orders = [
                ZoneOrder.model_validate_json(row[0]) for row in await cursor.fetchall()
            ]
            cursor = await db.execute(
                "SELECT e.payload_json FROM zone_executions e JOIN zone_orders o USING(order_id) JOIN zone_allocations z USING(zone_id) WHERE z.group_id=?",
                (str(group_id),),
            )
            executions = [
                ZoneExecution.model_validate_json(row[0])
                for row in await cursor.fetchall()
            ]
        balances, issues = _zone_balances(zones, orders, executions)
        total_quantity = sum(
            (zone["remaining_quantity"] for zone in balances), Decimal("0")
        )
        if account is not None:
            positions = [
                position for position in account.positions if position.symbol == symbol
            ]
            if len(positions) > 1 or any(
                position.side.value != side for position in positions
            ):
                issues.append("POSITION_SIDE_OR_COUNT_MISMATCH")
            live_quantity = sum((position.size for position in positions), Decimal("0"))
            if live_quantity != total_quantity:
                issues.append("POSITION_QUANTITY_MISMATCH")
            known = {order.order_id: order for order in orders}
            for order in account.open_orders:
                if order.symbol != symbol:
                    continue
                binding = known.get(order.order_id)
                if binding is None:
                    issues.append("UNATTRIBUTED_OPEN_ORDER")
                elif not _order_matches_binding(order, binding, Side(side)):
                    issues.append("ORDER_BINDING_MISMATCH")
                elif order.executed_quantity != sum(
                    (
                        fill.quantity
                        for fill in executions
                        if fill.order_id == order.order_id
                    ),
                    Decimal("0"),
                ):
                    issues.append("ORDER_EXECUTION_MISMATCH")
        return {
            "group_id": str(group_id),
            "symbol": symbol,
            "side": side,
            "zones": balances,
            "total_risk_budget_usdt": sum(
                (zone["risk_budget_usdt"] for zone in balances), Decimal("0")
            ),
            "remaining_quantity": total_quantity,
            "issues": tuple(sorted(set(issues))),
            "quantity_reconciled": not issues if account is not None else None,
            "protection_verified": False,
            "trading_enabled": False,
        }
