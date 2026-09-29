from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from .domain import (
    AccountOrder,
    AccountPnlSummary,
    AccountPnlSyncState,
    AccountStateSummary,
    ApprovalMode,
    ClosedPnlRecord,
    ExecutionPlan,
    ExecutionPolicy,
    IncomingPost,
    InstrumentContext,
    IntentExecutionOutcome,
    MarketPrimaryExecutionResult,
    PositionActionExecutionOutcome,
    PositionActionExecutionResult,
    PositionActionIntent,
    PositionStrategy,
    Side,
    SignalContextSnapshot,
    SignalExtraction,
    SignalPositionContext,
    SourceMessage,
    StrategyStatus,
    SymbolExposure,
    TradingIntent,
)

# ---------------------------------------------------------------------------
# Infrastructure ports
# ---------------------------------------------------------------------------


class AccountGateway(Protocol):
    async def market_context(self, symbol: str) -> InstrumentContext: ...

    async def account_state(self) -> AccountStateSummary: ...

    async def wallet_balance_usdt(self) -> Decimal: ...

    async def closed_pnl_history(
        self,
        start: datetime,
        end: datetime,
    ) -> tuple[ClosedPnlRecord, ...]: ...

    async def execute(self, plan: ExecutionPlan) -> tuple[str, ...]: ...

    async def execute_market_primary(
        self,
        plan: ExecutionPlan,
    ) -> MarketPrimaryExecutionResult: ...

    async def execute_remaining_entries(
        self,
        plan: ExecutionPlan,
    ) -> tuple[str, ...]: ...

    async def execute_position_action(
        self,
        action: PositionActionIntent,
    ) -> PositionActionExecutionResult: ...

    async def cancel_pending_entries(self, symbol: str) -> int: ...

    async def cancel_strategy_exits(self, symbol: str) -> int: ...

    async def cancel_order(self, symbol: str, order_id: str) -> None: ...

    async def set_position_protection(
        self,
        symbol: str,
        stop_loss: Decimal,
        *,
        trailing_distance: Decimal | None = None,
    ) -> None: ...

    async def place_reduce_only_exit(
        self,
        *,
        symbol: str,
        position_side: Side,
        quantity: Decimal,
        price: Decimal,
        order_link_id: str,
    ) -> str: ...

    async def strategy_order(
        self,
        symbol: str,
        link_id: str,
    ) -> AccountOrder | None: ...

    def close(self) -> None: ...


class ApprovalSender(Protocol):
    async def send_account_snapshot(
        self,
        state: AccountStateSummary | None,
        *,
        state_error: str | None,
        pnl: AccountPnlSummary | None,
        pnl_error: str | None,
    ) -> None: ...

    async def send_intent(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
        *,
        exposure: SymbolExposure | None = None,
        exposure_error: str | None = None,
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
        account_pnl: AccountPnlSummary | None = None,
        account_pnl_error: str | None = None,
        send_account_state: bool = True,
    ) -> None: ...

    async def send_position_action(
        self,
        action: PositionActionIntent,
        *,
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
    ) -> None: ...

    async def send_auto_intent_outcome(
        self,
        outcome: IntentExecutionOutcome,
    ) -> None: ...

    async def send_auto_action_outcome(
        self,
        outcome: PositionActionExecutionOutcome,
    ) -> None: ...

    async def send_recovery_warning(
        self,
        *,
        uncertain_intents: int,
        uncertain_actions: int,
    ) -> None: ...


class ManualApprovalExecutor(Protocol):
    async def execute_intent(
        self,
        intent_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> IntentExecutionOutcome: ...

    async def execute_position_action(
        self,
        action_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> PositionActionExecutionOutcome: ...


class IntentExtractor(Protocol):
    async def extract(
        self,
        post: IncomingPost,
        *,
        global_guidance: str | None = None,
        channel_guidance: str | None = None,
        position_context: SignalPositionContext | None = None,
    ) -> SignalExtraction: ...


class ExecutionCoordinator(Protocol):
    def auto_open_safety_reason(
        self,
        intent: TradingIntent,
        account_state: AccountStateSummary,
    ) -> str | None: ...

    async def execute_intent(
        self,
        intent_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> IntentExecutionOutcome: ...

    async def execute_position_action(
        self,
        action_id: UUID,
        *,
        approval_mode: ApprovalMode,
        user_id: int | None = None,
    ) -> PositionActionExecutionOutcome: ...


class SignalContextProvider(Protocol):
    async def snapshot(
        self,
        channel_id: int,
    ) -> SignalContextSnapshot: ...


# ---------------------------------------------------------------------------
# Repository role protocols
# ---------------------------------------------------------------------------


class SourceRepository(Protocol):
    async def initialize(self) -> None: ...

    async def claim_source(
        self,
        source: SourceMessage,
        *,
        lease_seconds: int,
    ) -> str | None: ...

    async def mark_source_completed(
        self,
        source: SourceMessage,
        claim_token: str,
    ) -> bool: ...

    async def mark_source_failed(
        self,
        source: SourceMessage,
        claim_token: str,
        error: str,
    ) -> bool: ...

    async def create_signal_batch_and_complete_source(
        self,
        items: Sequence[tuple[TradingIntent, ExecutionPlan]],
        position_actions: Sequence[PositionActionIntent],
        claim_token: str,
    ) -> bool: ...

    async def reset_stale_processing_sources(
        self,
        lease_seconds: int,
    ) -> int: ...


class GuidanceRepository(Protocol):
    async def get_guidance(
        self,
        channel_id: int,
    ) -> tuple[str | None, str | None]: ...

    async def set_guidance(
        self,
        content: str,
        *,
        channel_id: int | None = None,
    ) -> None: ...


class ExecutionPolicyRepository(Protocol):
    async def get_execution_policy(self) -> ExecutionPolicy: ...

    async def set_execution_policy(self, policy: ExecutionPolicy) -> None: ...


class IntentRepository(Protocol):
    async def get_intent(self, intent_id: UUID) -> TradingIntent | None: ...

    async def get_execution_plan(
        self,
        intent_id: UUID,
    ) -> ExecutionPlan | None: ...

    async def update_execution_plan(self, plan: ExecutionPlan) -> None: ...

    async def claim_for_execution(
        self,
        intent_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool: ...

    async def mark_skipped(
        self,
        intent_id: UUID,
        user_id: int,
    ) -> bool: ...

    async def mark_executed(
        self,
        intent_id: UUID,
        order_ids: tuple[str, ...],
    ) -> None: ...

    async def mark_failed(
        self,
        intent_id: UUID,
        error: str,
    ) -> None: ...

    async def get_recent_source_intents(
        self,
        channel_id: int,
        *,
        limit: int = 50,
    ) -> tuple[TradingIntent, ...]: ...


class PositionActionRepository(Protocol):
    async def get_position_action(
        self,
        action_id: UUID,
    ) -> PositionActionIntent | None: ...

    async def claim_position_action_for_execution(
        self,
        action_id: UUID,
        user_id: int | None,
        *,
        expected_approval_mode: ApprovalMode | None = None,
    ) -> bool: ...

    async def mark_position_action_skipped(
        self,
        action_id: UUID,
        user_id: int,
    ) -> bool: ...

    async def complete_position_action(
        self,
        action: PositionActionIntent,
        order_id: str,
    ) -> None: ...

    async def mark_position_action_failed(
        self,
        action_id: UUID,
        error: str,
    ) -> None: ...

    async def mark_position_action_uncertain(
        self,
        action_id: UUID,
        symbol: str,
        order_id: str | None,
        error: str,
    ) -> None: ...

    async def get_recent_executed_closes(
        self,
        *,
        limit: int = 100,
    ) -> tuple[PositionActionIntent, ...]: ...

    async def entry_cancellation_targets(
        self,
        action: PositionActionIntent,
    ) -> tuple[UUID, ...]: ...

    async def record_entry_cancellation(
        self,
        action: PositionActionIntent,
        targets: tuple[UUID, ...],
        *,
        complete: bool = False,
        flat: bool = False,
        error: str | None = None,
    ) -> None: ...


class PositionStrategyRepository(Protocol):
    async def ensure_position_strategy(self, plan: ExecutionPlan) -> None: ...

    async def get_active_position_strategies(
        self,
    ) -> tuple[tuple[PositionStrategy, ExecutionPlan], ...]: ...

    async def save_position_strategy(self, state: PositionStrategy) -> None: ...

    async def set_position_strategy_status(
        self,
        strategy_id: UUID,
        status: StrategyStatus,
    ) -> None: ...

    async def request_strategy_rebalance(self, symbol: str) -> None: ...

    async def request_strategy_close(self, symbol: str) -> None: ...


class ManualDeliveryRepository(Protocol):
    async def pending_manual_deliveries(
        self,
    ) -> tuple[tuple[UUID, str], ...]: ...

    async def claim_manual_delivery(
        self,
        record_id: UUID,
    ) -> str | None: ...

    async def finish_manual_delivery(
        self,
        record_id: UUID,
        token: str,
        *,
        delivered: bool,
    ) -> None: ...

    async def reconcile_manual_deliveries(
        self,
        *,
        stale_after_seconds: float = 86400,
    ) -> tuple[int, int]: ...


class PnlRepository(Protocol):
    async def upsert_closed_pnl(
        self,
        records: Sequence[ClosedPnlRecord],
    ) -> None: ...

    async def get_account_pnl_sync_state(
        self,
    ) -> AccountPnlSyncState | None: ...

    async def mark_account_pnl_synced(
        self,
        *,
        history_start_at: datetime,
        last_synced_at: datetime,
    ) -> None: ...

    async def get_account_pnl_summary(self) -> AccountPnlSummary | None: ...


class RecoveryRepository(Protocol):
    async def quarantine_interrupted_executions(
        self,
    ) -> tuple[tuple[UUID, ...], tuple[UUID, ...]]: ...

    async def get_pending_auto_intent_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[UUID, ...]: ...

    async def get_pending_auto_action_ids(
        self,
        *,
        limit: int = 100,
    ) -> tuple[UUID, ...]: ...


# ---------------------------------------------------------------------------
# Service-specific combined protocols
# ---------------------------------------------------------------------------


class SignalServiceStore(
    SourceRepository,
    GuidanceRepository,
    ExecutionPolicyRepository,
    IntentRepository,
    PositionActionRepository,
    ManualDeliveryRepository,
    PnlRepository,
    RecoveryRepository,
    Protocol,
):
    """Everything SignalService needs from durable storage."""


class ExecutionCoordinatorStore(
    IntentRepository,
    PositionActionRepository,
    PositionStrategyRepository,
    Protocol,
):
    """Everything ExecutionCoordinator needs from durable storage."""


class PositionSupervisorStore(
    PositionStrategyRepository,
    PositionActionRepository,
    IntentRepository,
    Protocol,
):
    """Everything PositionSupervisor needs from durable storage."""


class ApprovalBotStore(
    IntentRepository,
    PositionActionRepository,
    Protocol,
):
    """Everything ApprovalBot needs from durable storage."""


class SignalContextProviderStore(
    IntentRepository,
    PositionActionRepository,
    Protocol,
):
    """Everything SignalContextProvider needs from durable storage."""


class MainStore(
    SourceRepository,
    RecoveryRepository,
    Protocol,
):
    """Everything the composition root needs for startup."""
