import asyncio

from fakes import (
    FakeAccountGateway,
    FakePositionSupervisorStore,
    _require_account_gateway,
    _require_position_supervisor_store,
    make_account_position,
    make_account_state,
    make_execution_plan,
    make_position_strategy,
)

from cautious_crypto_bro.domain import StrategyStatus
from cautious_crypto_bro.position_supervisor import PositionSupervisor


def test_fake_account_gateway_conforms_to_protocol() -> None:
    gateway = FakeAccountGateway()
    assert _require_account_gateway(gateway) is gateway


def test_fake_position_supervisor_store_conforms_to_protocol() -> None:
    plan = make_execution_plan()
    state = make_position_strategy(plan)
    store = FakePositionSupervisorStore(state, plan)
    assert _require_position_supervisor_store(store) is store


def test_position_supervisor_runs_with_fakes() -> None:
    plan = make_execution_plan()
    state = make_position_strategy(plan)
    store = FakePositionSupervisorStore(state, plan)
    gateway = FakeAccountGateway(
        state=make_account_state(make_account_position()),
    )

    supervisor = PositionSupervisor(
        store=store,
        executor=gateway,
        mutation_lock=asyncio.Lock(),
        poll_interval_seconds=1,
    )

    asyncio.run(supervisor.reconcile_once())

    assert gateway.cancelled_entries == 1
    assert len(gateway.exits) == 3
    assert store.state.entry_frozen is True
    assert store.state.status is StrategyStatus.PROFIT_PROTECTED
