import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from cautious_crypto_bro.domain import (
    ApprovalMode,
    Entry,
    EntryType,
    ExecutionPlan,
    IntentStatus,
    Side,
    SourceMessage,
    TradingIntent,
)
from cautious_crypto_bro.execution_coordinator import ExecutionCoordinator


class LegacyStore:
    def __init__(self, intent: TradingIntent, plan: ExecutionPlan) -> None:
        self.intent = intent
        self.plan = plan
        self.claimed = False
        self.failure: str | None = None

    async def get_intent(self, intent_id: UUID) -> TradingIntent:
        assert intent_id == self.intent.intent_id
        return self.intent

    async def get_execution_plan(self, intent_id: UUID) -> ExecutionPlan:
        assert intent_id == self.intent.intent_id
        return self.plan

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode,
    ) -> bool:
        assert intent_id == self.intent.intent_id
        assert expected_approval_mode is self.intent.approval_mode
        self.claimed = True
        return True

    async def mark_failed(self, intent_id: UUID, error: str) -> None:
        assert intent_id == self.intent.intent_id
        self.failure = error


class NoExchangeCalls:
    def __getattr__(self, name: str) -> None:
        raise AssertionError(f"Legacy plan reached exchange method: {name}")


@pytest.mark.parametrize(
    "approval_mode",
    [ApprovalMode.MANUAL, ApprovalMode.AUTO],
)
def test_historical_v1_approval_is_rejected_before_exchange(
    approval_mode: ApprovalMode,
) -> None:
    now = datetime.now(UTC)
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=1,
            channel_title="Test",
            message_id=1,
            published_at=now,
            received_at=now,
            text="BTC long",
        ),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100),
        stop_loss=90,
        take_profit=120,
        summary="Historical trade",
        confidence=1,
        approval_mode=approval_mode,
    )
    plan = ExecutionPlan.model_validate(
        {
            "intent_id": str(intent.intent_id),
            "symbol": "BTCUSDT",
            "side": "LONG",
            "orders": [
                {
                    "order_type": "LIMIT",
                    "quantity": "0.1",
                    "price": "100",
                    "reference_price": "100",
                }
            ],
            "stop_loss": "90",
            "take_profit": "120",
            "policy": {
                "trading_capital_usdt": "6800",
                "risk_per_trade_pct": "1",
                "range_order_count": 3,
            },
            "planned_max_loss_usdt": "1",
        }
    )
    assert plan.strategy_version == 1
    assert plan.policy.risk_budget_usdt == Decimal("68")

    store = LegacyStore(intent, plan)
    coordinator = ExecutionCoordinator(
        store=store,
        executor=NoExchangeCalls(),
        max_age_seconds=300,
    )

    outcome = asyncio.run(
        coordinator.execute_intent(
            intent.intent_id,
            approval_mode=approval_mode,
        )
    )

    assert store.claimed
    assert outcome.status is IntentStatus.FAILED
    assert outcome.plan is plan
    assert store.failure == outcome.message
    assert "read-only" in outcome.message
