from decimal import Decimal

import pytest
from test_remaining_concerns import make_intent

from cautious_crypto_bro.domain import ExecutionPolicy, InstrumentContext
from cautious_crypto_bro.execution import ExecutionPlanner
from cautious_crypto_bro.storage._plan_payload import load_strategy_plan


@pytest.mark.parametrize(
    "saved_cap,source_cap,include_source,expected",
    [
        (None, 112, True, 112),
        (Decimal("115"), 112, True, Decimal("115")),
        (None, None, True, None),
        (None, 112, False, None),
    ],
)
def test_strategy_plan_recovers_only_missing_trader_cap(
    saved_cap, source_cap, include_source, expected
):
    intent = make_intent(tp=source_cap)
    plan = (
        ExecutionPlanner()
        .plan(
            intent,
            ExecutionPolicy(trading_capital_usdt=Decimal("6800")),
            InstrumentContext(
                Decimal("100"),
                Decimal("0.1"),
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("5"),
            ),
        )
        .model_copy(update={"trader_take_profit": saved_cap})
    )
    loaded = load_strategy_plan(
        plan.model_dump_json(), intent.model_dump_json() if include_source else None
    )
    assert loaded.trader_take_profit == expected
    assert loaded.model_dump(exclude={"trader_take_profit"}) == plan.model_dump(
        exclude={"trader_take_profit"}
    )
