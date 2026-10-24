from __future__ import annotations

from typing import TYPE_CHECKING

from ._helpers import _matches_exit

if TYPE_CHECKING:
    from ..domain import AccountPosition, AccountStateSummary, PositionStrategy
    from ..ports import AccountGateway


class FillDetector:
    def __init__(
        self,
        *,
        executor: AccountGateway,
    ) -> None:
        self._executor = executor

    async def drift_reason(
        self, state: PositionStrategy, account: AccountStateSummary
    ) -> str | None:
        if state.exit_revision <= 0 or (
            state.rebalance_needed and not state.installing_exits
        ):
            return None
        if state.expected_exits is None:
            return "Legacy exit snapshot unavailable; manual review required"
        if self._has_unexpected_exit(state, account):
            return "Unexpected owned fixed exit outside the saved revision"
        completed = {
            self.exit_link_id(state, index)
            for index, done in enumerate(
                (state.tp1_done, state.tp2_done, state.tp3_done), 1
            )
            if done
        }
        for link, (quantity, price) in state.expected_exits.items():
            if link in completed:
                continue
            order = next(
                (
                    item
                    for item in account.open_orders
                    if item.symbol == state.symbol and item.order_link_id == link
                ),
                None,
            )
            if order is None:
                order = await self._executor.strategy_order(state.symbol, link)
            if order is None:
                if state.installing_exits:
                    # A crash can leave saved legs not yet submitted.
                    continue
                return f"Owned exit missing or unconfirmed: {link}"
            if not _matches_exit(order, state.side, quantity, price):
                return (
                    f"Owned exit changed or canceled: {link}; status={order.status}; "
                    f"expected qty/price={quantity}/{price}, "
                    f"live={order.quantity}/{order.price}"
                )
        return None

    @staticmethod
    def _has_unexpected_exit(
        state: PositionStrategy, account: AccountStateSummary
    ) -> bool:
        prefix = f"ccb-v2-{state.strategy_id.hex[:20]}-t"
        for order in account.open_orders:
            if (
                order.symbol == state.symbol
                and order.order_link_id.startswith(prefix)
                and order.order_link_id not in (state.expected_exits or {})
            ):
                if state.installing_exits:
                    # Older revisions can remain while cancellation is pending.
                    revision = order.order_link_id.rsplit("r", 1)[-1]
                    if revision.isdecimal() and int(revision) < state.exit_revision:
                        continue
                return True
        return False

    async def detect(
        self,
        state: PositionStrategy,
        account: AccountStateSummary,
        position: AccountPosition,
    ) -> tuple[PositionStrategy, bool]:
        if state.exit_revision <= 0:
            return state, False
        done = [state.tp1_done, state.tp2_done, state.tp3_done]
        filled = any(done)
        for index in range(1, 4):
            if done[index - 1]:
                continue
            link = self.exit_link_id(state, index)
            order = next(
                (
                    o
                    for o in account.open_orders
                    if o.symbol == state.symbol and o.order_link_id == link
                ),
                None,
            )
            if order is None:
                order = await self._executor.strategy_order(state.symbol, link)
            if order is not None and order.executed_quantity > 0:
                filled = True
                done[index - 1] = order.status == "Filled"
        return state.model_copy(
            update={
                "tp1_done": done[0],
                "tp2_done": done[1],
                "tp3_done": done[2],
            }
        ), filled

    @staticmethod
    def entry_link_id(
        state: PositionStrategy,
        name: str,
    ) -> str:
        return f"ccb-v2-{state.strategy_id.hex[:20]}-{name.lower()}"

    @staticmethod
    def exit_link_id(
        state: PositionStrategy,
        index: int,
    ) -> str:
        return f"ccb-v2-{state.strategy_id.hex[:20]}-t{index}r{state.exit_revision}"
