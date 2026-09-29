from __future__ import annotations

from typing import TYPE_CHECKING

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
