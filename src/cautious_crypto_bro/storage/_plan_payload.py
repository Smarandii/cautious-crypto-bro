from decimal import Decimal

from ..domain import ExecutionPlan, TradingIntent


def load_strategy_plan(plan_json: str, intent_json: str | None) -> ExecutionPlan:
    plan = ExecutionPlan.model_validate_json(plan_json)
    if plan.trader_take_profit is not None or intent_json is None:
        return plan

    # Older plans retained policy targets but lost the original trader cap.
    intent = TradingIntent.model_validate_json(intent_json)
    if intent.take_profit is None:
        return plan
    return plan.model_copy(
        update={"trader_take_profit": Decimal(str(intent.take_profit))}
    )
