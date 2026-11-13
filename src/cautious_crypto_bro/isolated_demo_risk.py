from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

from .domain import AccountStateSummary, ExecutionPlan, PositionStrategy, StrategyStatus
from .portfolio_risk import PortfolioRiskUnavailable, open_stop_risk_usdt
from .ports import AccountGateway, ExecutionCoordinatorStore


class IsolatedDemoPortfolio:
    """One risk ceiling and mutation lock for account-bound Demo workers."""

    def __init__(
        self,
        accounts: tuple[tuple[AccountGateway, ExecutionCoordinatorStore], ...],
        cap_usdt: Decimal,
        *,
        mutation_lock: asyncio.Lock | None = None,
    ) -> None:
        if len(accounts) < 2 or len({id(executor) for executor, _ in accounts}) != len(
            accounts
        ):
            raise ValueError("Distinct isolated Demo executors are required")
        if len({id(store) for _, store in accounts}) != len(accounts):
            raise ValueError("Distinct isolated Demo stores are required")
        if not cap_usdt.is_finite() or cap_usdt <= 0:
            raise ValueError("Portfolio stop-risk cap must be positive and finite")
        self._accounts = accounts
        self.cap_usdt = cap_usdt
        self.mutation_lock = (
            mutation_lock if mutation_lock is not None else asyncio.Lock()
        )

    def includes(
        self, executor: AccountGateway, store: ExecutionCoordinatorStore
    ) -> bool:
        return any(
            account is executor and state is store for account, state in self._accounts
        )

    async def open_risk_usdt(self) -> Decimal:
        total = Decimal("0")
        for executor, store in self._accounts:
            account = await executor.account_state()
            strategies = await store.get_active_position_strategies()
            total += _account_commitment(account, strategies)
        return total


def _account_commitment(
    account: AccountStateSummary,
    strategies: tuple[tuple[PositionStrategy, ExecutionPlan], ...],
) -> Decimal:
    owned_symbols = {state.symbol for state, _ in strategies}
    if len(owned_symbols) != len(strategies):
        raise PortfolioRiskUnavailable("An isolated account has conflicting owners")

    total = open_stop_risk_usdt(
        replace(
            account,
            positions=tuple(
                p for p in account.positions if p.symbol not in owned_symbols
            ),
            open_orders=tuple(
                o for o in account.open_orders if o.symbol not in owned_symbols
            ),
        ),
        (),
    )
    for state, plan in strategies:
        if state.status is StrategyStatus.UNCERTAIN:
            raise PortfolioRiskUnavailable(
                "An isolated account has unresolved entry exposure"
            )
        observed = open_stop_risk_usdt(
            replace(
                account,
                positions=tuple(
                    p for p in account.positions if p.symbol == state.symbol
                ),
                open_orders=tuple(
                    o for o in account.open_orders if o.symbol == state.symbol
                ),
            ),
            ((state, plan),),
        )
        # Preserve the reservation until the supervisor settles the strategy.
        # An acknowledged entry can be temporarily absent from the snapshot.
        total += max(observed, plan.planned_max_loss_usdt)
    return total
