import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from cautious_crypto_bro.bybit import (
    AccountPosition,
    AccountStateSummary,
)
from cautious_crypto_bro.domain import (
    ApprovalMode,
    AutoApprovalMode,
    Entry,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    ExecutionPolicy,
    IntentExtraction,
    IntentStatus,
    OpenRelation,
    PlannedOrder,
    PositionActionIntent,
    PositionActionType,
    Side,
    SignalPositionContext,
    SourceMessage,
    SourceOpenContext,
    StrategyStatus,
    TradingIntent,
)
from cautious_crypto_bro.execution import ExecutionPlanner, InstrumentContext
from cautious_crypto_bro.execution_coordinator import (
    ExecutionCoordinator,
)
from cautious_crypto_bro.openrouter import (
    _signals_from_extraction,
)
from cautious_crypto_bro.service import (
    SignalService,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


def _source() -> SourceMessage:
    now = datetime.now(UTC)

    return SourceMessage(
        channel_id=-1001234567890,
        channel_title="Trader",
        channel_username=None,
        message_id=123,
        published_at=now,
        received_at=now,
        text="BTC update",
    )


def _intent(
    *,
    relation: OpenRelation = (OpenRelation.NEW),
    approval_mode: ApprovalMode = (ApprovalMode.MANUAL),
) -> TradingIntent:
    return TradingIntent(
        source=_source(),
        symbol="BTCUSDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.LIMIT,
            price=100,
        ),
        stop_loss=90,
        take_profit=120,
        summary="BTC long",
        confidence=1,
        relation=relation,
        approval_mode=approval_mode,
    )


def _plan(
    intent: TradingIntent,
) -> ExecutionPlan:
    policy = ExecutionPolicy(
        trading_capital_usdt=Decimal("1000"),
        risk_per_trade_pct=Decimal("1"),
        range_order_count=1,
    )

    return ExecutionPlan(
        intent_id=intent.intent_id,
        symbol=intent.symbol,
        side=intent.side,
        orders=(
            PlannedOrder(
                order_type=(ExecutionOrderType.LIMIT),
                quantity=Decimal("0.1"),
                price=Decimal("100"),
                reference_price=Decimal("100"),
                take_profit=Decimal("120"),
            ),
        ),
        stop_loss=Decimal("90"),
        take_profit=Decimal("120"),
        policy=policy,
        planned_max_loss_usdt=Decimal("1"),
    )


async def _persist_intent(
    store: IntentStore,
    intent: TradingIntent,
) -> None:
    claim = await store.claim_source(
        intent.source,
        lease_seconds=300,
    )
    assert claim is not None

    assert await store.create_signal_batch_and_complete_source(
        ((intent, _plan(intent)),),
        (),
        claim,
    )


def _position(
    side: Side = Side.LONG,
) -> AccountPosition:
    return AccountPosition(
        symbol="BTCUSDT",
        side=side,
        size=Decimal("1"),
        avg_price=Decimal("100"),
        mark_price=Decimal("101"),
        unrealised_pnl=Decimal("1"),
        status="Normal",
        take_profit=None,
        stop_loss=None,
    )


def _state(
    *positions: AccountPosition,
) -> AccountStateSummary:
    return AccountStateSummary(
        as_of=datetime.now(UTC),
        positions=tuple(positions),
        open_orders=(),
    )


def test_update_existing_is_not_executable() -> None:
    extraction = IntentExtraction.model_validate(
        {
            "actionable": False,
            "reason": ("Existing position update"),
            "intents": [
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "relation": ("UPDATE_EXISTING"),
                    "relation_evidence": ("Already copied BTC"),
                    "summary": ("BTC position update"),
                    "confidence": 1,
                }
            ],
            "position_actions": [],
        }
    )

    signals = _signals_from_extraction(
        _source(),
        extraction,
    )

    assert signals.open_intents == ()
    assert not signals.actionable


def test_auto_open_live_safety_rules() -> None:
    new_intent = _intent(relation=OpenRelation.NEW)

    assert (
        ExecutionCoordinator.auto_open_safety_reason(
            new_intent,
            _state(),
        )
        is None
    )

    assert (
        ExecutionCoordinator.auto_open_safety_reason(
            new_intent,
            _state(_position()),
        )
        is not None
    )

    add_intent = _intent(relation=(OpenRelation.ADD_OR_REENTRY))

    assert (
        ExecutionCoordinator.auto_open_safety_reason(
            add_intent,
            _state(_position()),
        )
        is None
    )

    assert (
        ExecutionCoordinator.auto_open_safety_reason(
            add_intent,
            _state(_position(Side.SHORT)),
        )
        is not None
    )


def test_storage_persists_auto_mode_and_claims_match(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        intent = _intent(approval_mode=ApprovalMode.AUTO)

        await _persist_intent(store, intent)

        stored = await store.get_intent(intent.intent_id)

        assert stored is not None
        assert stored.approval_mode is ApprovalMode.AUTO

        assert not (
            await store.claim_for_execution(
                intent.intent_id,
                123,
                expected_approval_mode=(ApprovalMode.MANUAL),
            )
        )

        assert await store.claim_for_execution(
            intent.intent_id,
            None,
            expected_approval_mode=(ApprovalMode.AUTO),
        )

    asyncio.run(run())


def test_restart_quarantines_inflight_auto_execution(
    tmp_path,
) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()

        intent = _intent(approval_mode=ApprovalMode.AUTO)

        await _persist_intent(store, intent)

        assert await store.claim_for_execution(
            intent.intent_id,
            None,
            expected_approval_mode=(ApprovalMode.AUTO),
        )

        intents, actions = await store.quarantine_interrupted_executions()

        assert intents == (intent.intent_id,)
        assert actions == ()

        stored = await store.get_intent(intent.intent_id)

        assert stored is not None
        assert stored.status is IntentStatus.UNCERTAIN

        assert (await store.get_pending_auto_intent_ids()) == ()

    asyncio.run(run())


def test_restart_atomically_quarantines_manual_actions_and_strategies(tmp_path) -> None:
    async def run() -> None:
        store = IntentStore(tmp_path / "state.sqlite3")
        await store.initialize()
        intent = _intent(approval_mode=ApprovalMode.AUTO)
        await _persist_intent(store, intent)

        strategy_plan = ExecutionPlanner().plan(
            intent,
            ExecutionPolicy(trading_capital_usdt=Decimal("1000")),
            InstrumentContext(
                market_price=Decimal("110"),
                tick_size=Decimal("0.1"),
                qty_step=Decimal("0.001"),
                min_qty=Decimal("0.001"),
                min_notional=Decimal("5"),
            ),
        )
        await store.ensure_position_strategy(strategy_plan)
        assert await store.claim_for_execution(
            intent.intent_id,
            None,
            expected_approval_mode=ApprovalMode.AUTO,
        )

        action_source = _source().model_copy(update={"message_id": 124})
        action = PositionActionIntent(
            source=action_source,
            symbol="BTCUSDT",
            action=PositionActionType.REDUCE,
            close_pct=25,
            summary="Reduce 25%",
            confidence=1,
            approval_mode=ApprovalMode.MANUAL,
        )
        claim = await store.claim_source(action_source, lease_seconds=300)
        assert claim is not None
        assert await store.create_signal_batch_and_complete_source(
            (),
            (action,),
            claim,
        )
        assert await store.claim_position_action_for_execution(
            action.action_id,
            1,
            expected_approval_mode=ApprovalMode.MANUAL,
        )

        intents, actions = await store.quarantine_interrupted_executions()
        assert intents == (intent.intent_id,)
        assert actions == (action.action_id,)
        assert (
            await store.get_intent(intent.intent_id)
        ).status is IntentStatus.UNCERTAIN
        assert (
            await store.get_position_action(action.action_id)
        ).status is IntentStatus.UNCERTAIN
        active = await store.get_active_position_strategies()
        assert len(active) == 1
        assert active[0][0].status is StrategyStatus.UNCERTAIN
        assert await store.quarantine_interrupted_executions() == ((), ())

    asyncio.run(run())


def test_service_auto_routing_requires_trusted_state() -> None:
    class Coordinator:
        @staticmethod
        def auto_open_safety_reason(
            intent,
            account_state,
        ):
            return None

    common = {
        "store": object(),
        "extractor": object(),
        "planner": object(),
        "executor": object(),
        "approval_bot": object(),
        "coordinator": Coordinator(),
        "context_provider": object(),
    }

    service = SignalService(
        **common,
        auto_approval_mode=(AutoApprovalMode.OPEN_ONLY),
    )

    intent = _intent(relation=OpenRelation.NEW)

    empty_context = SignalPositionContext(
        source_channel_id=(intent.source.channel_id),
        account_state_available=True,
    )

    account_state = _state()

    assert (
        service._open_approval_mode(
            intent,
            position_context=empty_context,
            account_state=account_state,
            duplicate_in_batch=False,
        )
        is ApprovalMode.AUTO
    )

    existing_context = SignalPositionContext(
        source_channel_id=(intent.source.channel_id),
        account_state_available=True,
        source_open_history=(
            SourceOpenContext(
                symbol="BTCUSDT",
                side=Side.LONG,
                status=(IntentStatus.EXECUTED),
                message_id=100,
                created_at=(datetime.now(UTC)),
                active_copy=True,
            ),
        ),
    )

    assert (
        service._open_approval_mode(
            intent,
            position_context=(existing_context),
            account_state=account_state,
            duplicate_in_batch=False,
        )
        is ApprovalMode.MANUAL
    )

    action = PositionActionIntent(
        source=_source(),
        symbol="BTCUSDT",
        action=PositionActionType.CLOSE,
        expected_side=Side.LONG,
        summary="Close BTC",
        confidence=1,
    )

    assert (
        service._position_action_approval_mode(
            action,
            account_state=_state(_position()),
        )
        is ApprovalMode.MANUAL
    )

    all_service = SignalService(
        **common,
        auto_approval_mode=(AutoApprovalMode.ALL),
    )

    assert (
        all_service._position_action_approval_mode(
            action,
            account_state=_state(_position()),
        )
        is ApprovalMode.AUTO
    )
