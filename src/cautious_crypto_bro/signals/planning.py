from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from ..contract_symbols import (
    BYBIT_CONTRACT_ALIASES,
    ContractUnitError,
    owned_action_symbol,
    resolve_open_contract,
)
from ..domain import (
    AccountStateSummary,
    ApprovalMode,
    AutoApprovalMode,
    ExecutionPlan,
    ExecutionPolicy,
    InstrumentContext,
    IntentStatus,
    OpenRelation,
    PositionActionIntent,
    PositionActionType,
    PositionStrategy,
    Side,
    SignalContextSnapshot,
    SignalExtraction,
    SignalPositionContext,
    TradeExecutionError,
    TradingIntent,
)
from ..execution import ExecutionPlanner, ExecutionPlanningError
from ..momentum import classify as classify_momentum
from ..portfolio_risk import (
    open_stop_risk_usdt,
    policy_with_remaining_portfolio_risk,
)

logger = logging.getLogger(__name__)


class PlanningStore(Protocol):
    async def get_execution_policy(self) -> ExecutionPolicy: ...

    async def get_active_position_strategies(
        self,
    ) -> tuple[tuple[PositionStrategy, ExecutionPlan], ...]: ...


class PlanningExchange(Protocol):
    async def market_context(self, symbol: str) -> InstrumentContext: ...

    async def wallet_balance_usdt(self) -> Decimal: ...

    async def one_minute_closes(
        self, symbol: str, *, limit: int = 120
    ) -> list[tuple[int, Decimal]]: ...


class OpenApprovalSafety(Protocol):
    def auto_open_safety_reason(
        self, intent: TradingIntent, account_state: AccountStateSummary
    ) -> str | None: ...


@dataclass(frozen=True)
class OpenPlanningResult:
    planned: list[tuple[TradingIntent, ExecutionPlan]]
    errors: list[str]
    rejections: list[str]
    retry_candidates: list[tuple[TradingIntent, bool, datetime]]


class SignalPlanning:
    """Plan source candidates without claiming, persisting or dispatching them."""

    def __init__(
        self,
        *,
        store: PlanningStore,
        planner: ExecutionPlanner,
        executor: PlanningExchange,
        coordinator: OpenApprovalSafety,
        auto_approval_mode: AutoApprovalMode = AutoApprovalMode.DISABLED,
        intent_max_age_seconds: int = 900,
        demo_long_risk_multiplier: Decimal = Decimal("1"),
        demo_long_exit_control_fraction: Decimal = Decimal("0"),
        demo_long_participation_skip_fraction: Decimal = Decimal("0"),
        demo_long_momentum_gate: bool = False,
        demo_exit_profile: str = "baseline",
        demo_portfolio_stop_risk_cap_usdt: Decimal | None = None,
        demo_entry_order_ttl_minutes: int = 0,
    ) -> None:
        if intent_max_age_seconds <= 0:
            raise ValueError("Intent maximum age must be positive")
        if not Decimal("0") < demo_long_risk_multiplier <= Decimal("1"):
            raise ValueError("Demo LONG risk multiplier must be in (0, 1]")
        if not Decimal("0") <= demo_long_exit_control_fraction <= Decimal("1"):
            raise ValueError(
                "Demo LONG exit control fraction must be between zero and one"
            )
        if not Decimal("0") <= demo_long_participation_skip_fraction <= Decimal("1"):
            raise ValueError(
                "Demo LONG participation skip fraction must be between zero and one"
            )
        if (
            demo_long_exit_control_fraction > 0
            and demo_exit_profile != "payoff_early_tight_trail_long_015"
        ):
            raise ValueError(
                "Demo LONG exit control split requires the long_015 profile"
            )
        if demo_exit_profile not in {
            "baseline",
            "payoff_challenger",
            "payoff_early_trail",
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
        }:
            raise ValueError("Unsupported Demo exit profile")
        if (
            demo_portfolio_stop_risk_cap_usdt is not None
            and demo_portfolio_stop_risk_cap_usdt <= 0
        ):
            raise ValueError("Demo portfolio stop-risk cap must be positive")
        if demo_entry_order_ttl_minutes < 0:
            raise ValueError("Demo entry order TTL must not be negative")

        self._store = store
        self._planner = planner
        self._executor = executor
        self._coordinator = coordinator
        self._auto_approval_mode = auto_approval_mode
        self._intent_max_age_seconds = intent_max_age_seconds
        self._demo_long_risk_multiplier = demo_long_risk_multiplier
        self._demo_long_exit_control_fraction = demo_long_exit_control_fraction
        self._demo_long_participation_skip_fraction = (
            demo_long_participation_skip_fraction
        )
        self._demo_long_momentum_gate = demo_long_momentum_gate
        self._demo_exit_profile = demo_exit_profile
        self._demo_portfolio_stop_risk_cap_usdt = demo_portfolio_stop_risk_cap_usdt
        self._demo_entry_order_ttl_minutes = demo_entry_order_ttl_minutes

    @staticmethod
    def _manual(
        message: str,
        *args: object,
    ) -> ApprovalMode:
        logger.warning(message, *args)
        return ApprovalMode.MANUAL

    def _open_approval_mode(
        self,
        intent: TradingIntent,
        *,
        position_context: SignalPositionContext,
        account_state: AccountStateSummary | None,
        duplicate_in_batch: bool,
    ) -> ApprovalMode:
        if intent.source.recovery_review_reason:
            return ApprovalMode.MANUAL
        if self._auto_approval_mode is AutoApprovalMode.DISABLED:
            return ApprovalMode.MANUAL

        if intent.relation not in {
            OpenRelation.NEW,
            OpenRelation.ADD_OR_REENTRY,
        }:
            return self._manual(
                "OPEN %s from %s/%s remains MANUAL because relation is %s",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
                intent.relation.value,
            )

        if duplicate_in_batch:
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL because the same symbol/"
                "side appears multiple times in "
                "the current post",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        if account_state is None or not position_context.account_state_available:
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL because trusted live "
                "account state is unavailable",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        if intent.relation is OpenRelation.NEW and position_context.has_existing_copy(
            intent.symbol,
            intent.side,
        ):
            return self._manual(
                "OPEN %s from %s/%s remains "
                "MANUAL: model classified NEW "
                "but trusted source history "
                "already has a copied/in-flight "
                "position",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
            )

        safety_reason = self._coordinator.auto_open_safety_reason(
            intent,
            account_state,
        )

        if safety_reason is not None:
            return self._manual(
                "OPEN %s from %s/%s remains MANUAL: %s",
                intent.symbol,
                intent.source.channel_id,
                intent.source.message_id,
                safety_reason,
            )

        return ApprovalMode.AUTO

    def _position_action_approval_mode(
        self,
        action: PositionActionIntent,
        *,
        account_state: AccountStateSummary | None,
    ) -> ApprovalMode:
        if action.source.recovery_review_reason:
            return ApprovalMode.MANUAL
        if account_state is None:
            return self._manual(
                "Position action %s %s remains "
                "MANUAL because live account "
                "state is unavailable",
                action.action.value,
                action.symbol,
            )

        if action.action is PositionActionType.CANCEL_ENTRIES:
            # Execution resolves ownership from durable source records, not position count.
            executable = True
        else:
            positions = tuple(
                position
                for position in account_state.positions
                if position.symbol == action.symbol
            )
            executable = len(positions) == 1 and (
                action.expected_side is None
                or action.expected_side is positions[0].side
            )

        if not executable:
            logger.info(
                "Position action %s %s skipped; no matching live position",
                action.action.value,
                action.symbol,
            )
            return ApprovalMode.SKIPPED

        if self._auto_approval_mode is not AutoApprovalMode.ALL:
            return ApprovalMode.MANUAL

        return ApprovalMode.AUTO

    async def plan_open_intents(
        self,
        signals: SignalExtraction,
        context_snapshot: SignalContextSnapshot,
        *,
        retain_retry_candidates: bool = True,
        original_duplicate: bool = False,
        original_manual: bool = False,
    ) -> OpenPlanningResult:
        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ] = []
        planning_errors: list[str] = []
        planning_rejections: list[str] = []
        retry_candidates: list[tuple[TradingIntent, bool, datetime]] | None = (
            [] if retain_retry_candidates else None
        )

        original_intents = {i.intent_id: i for i in signals.open_intents}
        (
            signals,
            resolved_contexts,
            resolution_counts,
        ) = await self._resolve_open_contracts(
            signals,
            planning_errors,
            planning_rejections,
            retry_candidates,
            original_duplicate=original_duplicate,
        )
        if not signals.open_intents:
            return OpenPlanningResult(
                planned, planning_errors, planning_rejections, retry_candidates or []
            )

        account_state = context_snapshot.account_state
        position_context = context_snapshot.position_context

        open_counts = self._candidate_counts(signals, original_duplicate)
        open_counts = Counter(
            {
                key: max(count, resolution_counts[key])
                for key, count in open_counts.items()
            }
        )
        retry_counts = Counter(
            {
                (original_intents[i.intent_id].symbol, i.side): open_counts[
                    (i.symbol, i.side)
                ]
                for i in signals.open_intents
            }
        )

        try:
            policy = await self._capital_frozen_policy()
        except Exception as exc:
            planning_errors.append(
                f"Execution policy/capital: {type(exc).__name__}: {exc}"
            )

            logger.exception(
                "Execution policy/capital load failed",
            )
            self._remember_retry_candidates(
                tuple(original_intents[i.intent_id] for i in signals.open_intents),
                retry_counts,
                retry_candidates,
            )
            return OpenPlanningResult(
                planned, planning_errors, planning_rejections, retry_candidates or []
            )

        existing_portfolio_risk = await self._existing_portfolio_stop_risk(
            account_state,
            planning_errors,
        )
        if existing_portfolio_risk is None:
            self._remember_retry_candidates(
                tuple(original_intents[i.intent_id] for i in signals.open_intents),
                retry_counts,
                retry_candidates,
            )
            return OpenPlanningResult(
                planned, planning_errors, planning_rejections, retry_candidates or []
            )

        batch_reserved_risk = Decimal("0")

        for extracted_intent in signals.open_intents:
            intent = self._routed_intent(
                extracted_intent,
                position_context,
                account_state,
                open_counts,
                original_manual=original_manual,
            )
            retry_intent = original_intents[intent.intent_id].model_copy(
                update={"approval_mode": intent.approval_mode}
            )

            momentum_stratum = await self._gate_momentum_stratum(intent)

            intent_policy = self._policy_for_intent_side(
                policy,
                intent.side,
                intent.intent_id,
                participation_eligible=(intent.approval_mode is ApprovalMode.AUTO),
                momentum_stratum=momentum_stratum,
            )

            if self._demo_portfolio_stop_risk_cap_usdt is not None:
                remaining_portfolio_risk = (
                    self._demo_portfolio_stop_risk_cap_usdt
                    - existing_portfolio_risk
                    - batch_reserved_risk
                )
                if remaining_portfolio_risk <= 0:
                    logger.info(
                        "Skipping %s because portfolio stop-risk cap is full",
                        intent.symbol,
                    )
                    continue
                try:
                    intent_policy = policy_with_remaining_portfolio_risk(
                        intent_policy,
                        remaining_portfolio_risk,
                    )
                except ValueError as exc:
                    planning_errors.append(
                        f"{intent.symbol}: portfolio stop-risk cap: {exc}"
                    )
                    self._remember_retry_candidates(
                        (retry_intent,),
                        retry_counts,
                        retry_candidates,
                    )
                    continue

            previous_error_count = len(planning_errors)
            plan = await self._plan_single_intent(
                intent,
                intent_policy,
                planning_errors,
                planning_rejections,
                market_context=resolved_contexts.get(intent.intent_id),
            )
            self._remember_retry_candidates(
                (retry_intent,) if len(planning_errors) > previous_error_count else (),
                retry_counts,
                retry_candidates,
            )

            if plan is not None:
                if intent_policy.strategy_v2.long_participation_arm == "skip":
                    intent = intent.model_copy(
                        update={
                            "approval_mode": ApprovalMode.SKIPPED,
                            "status": IntentStatus.SKIPPED,
                        }
                    )
                else:
                    batch_reserved_risk += plan.planned_max_loss_usdt
                planned.append((intent, plan))

        return OpenPlanningResult(
            planned, planning_errors, planning_rejections, retry_candidates or []
        )

    async def _resolve_open_contracts(
        self,
        signals: SignalExtraction,
        errors: list[str],
        rejections: list[str],
        retry_candidates: list[tuple[TradingIntent, bool, datetime]] | None,
        *,
        original_duplicate: bool,
    ) -> tuple[
        SignalExtraction, dict[UUID, InstrumentContext], Counter[tuple[str, Side]]
    ]:
        resolved: list[TradingIntent] = []
        contexts: dict[UUID, InstrumentContext] = {}
        prospective = Counter(
            (BYBIT_CONTRACT_ALIASES.get(i.symbol, i.symbol), i.side)
            for i in signals.open_intents
        )
        counts = Counter(
            {
                (i.symbol, i.side): max(
                    prospective[
                        (BYBIT_CONTRACT_ALIASES.get(i.symbol, i.symbol), i.side)
                    ],
                    2 if original_duplicate else 1,
                )
                for i in signals.open_intents
            }
        )
        for intent in signals.open_intents:
            if intent.symbol not in BYBIT_CONTRACT_ALIASES:
                resolved.append(intent)
                continue
            try:
                canonical, context = await resolve_open_contract(
                    intent, self._executor.market_context
                )
            except Exception as exc:
                reason = f"{intent.symbol}: {type(exc).__name__}: {exc}"
                if isinstance(exc, ContractUnitError) or (
                    isinstance(exc, TradeExecutionError)
                    and exc.is_permanent_read_rejection
                ):
                    rejections.append(reason)
                else:
                    errors.append(reason)
                    self._remember_retry_candidates((intent,), counts, retry_candidates)
                logger.info("Contract resolution unavailable: %s", reason)
                continue
            resolved.append(canonical)
            if context is not None:
                contexts[intent.intent_id] = context
        return replace(signals, open_intents=tuple(resolved)), contexts, counts

    @staticmethod
    def _candidate_counts(
        signals: SignalExtraction, original_duplicate: bool
    ) -> Counter[tuple[str, Side]]:
        counts = Counter(
            (intent.symbol, intent.side) for intent in signals.open_intents
        )
        if original_duplicate:
            counts.update(dict.fromkeys(counts, 1))
        return counts

    def _remember_retry_candidates(
        self,
        intents: tuple[TradingIntent, ...],
        counts: Counter[tuple[str, Side]],
        candidates: list[tuple[TradingIntent, bool, datetime]] | None,
    ) -> None:
        if candidates is None:
            return
        for intent in intents:
            deadline = min(intent.created_at, intent.source.published_at) + timedelta(
                seconds=self._intent_max_age_seconds
            )
            candidates.append(
                (intent, counts[(intent.symbol, intent.side)] > 1, deadline)
            )

    async def _existing_portfolio_stop_risk(
        self,
        account_state: AccountStateSummary | None,
        planning_errors: list[str],
    ) -> Decimal | None:
        cap = self._demo_portfolio_stop_risk_cap_usdt
        if cap is None:
            return Decimal("0")
        if account_state is None:
            planning_errors.append(
                "Portfolio stop-risk cap: live account snapshot is unavailable"
            )
            return None
        try:
            active_strategies = await self._store.get_active_position_strategies()
            return open_stop_risk_usdt(account_state, active_strategies)
        except Exception as exc:
            planning_errors.append(
                f"Portfolio stop-risk cap: {type(exc).__name__}: {exc}"
            )
            logger.exception("Portfolio stop-risk snapshot failed")
            return None

    async def _gate_momentum_stratum(self, intent) -> str | None:
        """The gate's classification for an eligible LONG; None otherwise."""
        if not (
            self._demo_long_momentum_gate
            and intent.side is Side.LONG
            and intent.approval_mode is ApprovalMode.AUTO
        ):
            return None
        return await self._prior_momentum_stratum(intent.symbol)

    async def _prior_momentum_stratum(self, symbol: str) -> str | None:
        """Classify prior 60m momentum exactly like the #183 review's strata."""
        try:
            candles = await self._executor.one_minute_closes(symbol)
        except Exception as exc:
            logger.info(
                "Momentum gate: %s candle read failed: %s",
                symbol,
                type(exc).__name__,
            )
            return None
        try:
            return classify_momentum(
                [{"time": start, "close": float(close)} for start, close in candles],
                signal_time_ms=int(datetime.now(UTC).timestamp() * 1000),
                side="LONG",
            )
        except Exception:
            logger.exception("Momentum gate classification failed for %s", symbol)
            return None

    def _policy_for_intent_side(
        self,
        policy: ExecutionPolicy,
        side: Side,
        intent_id: UUID | None = None,
        *,
        participation_eligible: bool = True,
        momentum_stratum: str | None = None,
    ) -> ExecutionPolicy:
        strategy = policy.strategy_v2
        updates = {}
        if side is Side.LONG:
            updates["risk_per_trade_pct"] = (
                policy.risk_per_trade_pct * self._demo_long_risk_multiplier
            )

        if (
            self._demo_long_exit_control_fraction > 0
            and self._demo_exit_profile == "payoff_early_tight_trail_long_015"
        ):
            if side is Side.SHORT:
                strategy = strategy.model_copy(
                    update={"exit_profile": "payoff_early_tight_trail"}
                )
            else:
                if intent_id is None:
                    raise ValueError(
                        "Randomized LONG exit assignment requires an intent ID"
                    )
                control_bucket = int.from_bytes(
                    hashlib.sha256(intent_id.bytes).digest()[:8],
                    "big",
                )
                control_threshold = int(
                    self._demo_long_exit_control_fraction * (1 << 64)
                )
                is_control = control_bucket < control_threshold
                strategy = strategy.model_copy(
                    update={
                        "exit_profile": (
                            "payoff_early_tight_trail_long_ab_020_control"
                            if is_control
                            else "payoff_early_tight_trail_long_ab_015"
                        ),
                        "trailing_activation_r": (
                            Decimal("0.20") if is_control else Decimal("0.15")
                        ),
                    }
                )
        elif (
            side is Side.LONG
            and self._demo_exit_profile == "payoff_early_tight_trail_long_015"
        ):
            strategy = strategy.model_copy(
                update={"trailing_activation_r": Decimal("0.15")}
            )
        if (
            side is Side.LONG
            and participation_eligible
            and (
                self._demo_long_momentum_gate
                or self._demo_long_participation_skip_fraction > 0
            )
        ):
            strategy = self._assign_participation_arm(
                strategy, side, intent_id, momentum_stratum
            )
        if strategy != policy.strategy_v2:
            updates["strategy_v2"] = strategy
        return policy.model_copy(update=updates) if updates else policy

    def _assign_participation_arm(
        self,
        strategy,
        side: Side,
        intent_id: UUID | None,
        momentum_stratum: str | None,
    ):
        """Assign the Demo LONG take/skip arm; the gate beats the coin flip."""
        if intent_id is None:
            raise ValueError(
                "Randomized LONG participation assignment requires an intent ID"
            )
        participation_updates: dict[str, object] = {}
        gated = self._demo_long_momentum_gate and momentum_stratum is not None
        if gated:
            participation_updates["long_participation_momentum"] = (
                "positive" if momentum_stratum == "skip_positive" else "nonpositive"
            )
            participation_updates["long_participation_arm"] = (
                "take" if momentum_stratum == "skip_positive" else "skip"
            )
            return strategy.model_copy(update=participation_updates)

        if self._demo_long_momentum_gate:
            participation_updates["long_participation_momentum"] = "unavailable"
        participation_bucket = int.from_bytes(
            hashlib.sha256(b"long-participation-v1:" + intent_id.bytes).digest()[:8],
            "big",
        )
        skip_threshold = int(self._demo_long_participation_skip_fraction * (1 << 64))
        participation_updates["long_participation_arm"] = (
            "skip" if participation_bucket < skip_threshold else "take"
        )
        return strategy.model_copy(update=participation_updates)

    async def _capital_frozen_policy(self) -> ExecutionPolicy:
        policy = await self._store.get_execution_policy()
        trading_capital_usdt = await self._executor.wallet_balance_usdt()
        updates: dict[str, object] = {"trading_capital_usdt": trading_capital_usdt}
        strategy = policy.strategy_v2
        strategy_changed = False
        if self._demo_exit_profile in {
            "payoff_challenger",
            "payoff_early_trail",
            "payoff_early_tight_trail",
            "payoff_early_tight_trail_long_015",
        }:
            trailing_activation_r = (
                Decimal("0.2")
                if self._demo_exit_profile
                in {
                    "payoff_early_trail",
                    "payoff_early_tight_trail",
                    "payoff_early_tight_trail_long_015",
                }
                else Decimal("0.4")
            )
            strategy = strategy.model_copy(
                update={
                    "exit_profile": self._demo_exit_profile,
                    "first_take_profit_r": Decimal("1"),
                    "second_take_profit_r": Decimal("2"),
                    "third_take_profit_r": Decimal("4"),
                    "first_take_profit_pct": Decimal("15"),
                    "second_take_profit_pct": Decimal("20"),
                    "third_take_profit_pct": Decimal("25"),
                    "runner_pct": Decimal("40"),
                    "trailing_activation_r": trailing_activation_r,
                    "trailing_distance_r": (
                        Decimal("0.05")
                        if self._demo_exit_profile
                        in {
                            "payoff_early_tight_trail",
                            "payoff_early_tight_trail_long_015",
                        }
                        else Decimal("0.1")
                    ),
                }
            )
            strategy_changed = True
        if self._demo_entry_order_ttl_minutes:
            strategy = strategy.model_copy(
                update={
                    "entry_order_ttl_minutes": self._demo_entry_order_ttl_minutes,
                }
            )
            strategy_changed = True
        if strategy_changed:
            updates["strategy_v2"] = strategy
        return policy.model_copy(update=updates)

    def _routed_intent(
        self,
        extracted_intent: TradingIntent,
        position_context: SignalPositionContext,
        account_state: AccountStateSummary | None,
        open_counts: Counter[tuple[str, Side]],
        *,
        original_manual: bool = False,
    ) -> TradingIntent:
        key = (extracted_intent.symbol, extracted_intent.side)

        approval_mode = self._open_approval_mode(
            extracted_intent,
            position_context=(position_context),
            account_state=(account_state),
            duplicate_in_batch=(open_counts[key] > 1),
        )

        return extracted_intent.model_copy(
            update={
                "approval_mode": ApprovalMode.MANUAL
                if original_manual
                else approval_mode
            }
        )

    async def _plan_single_intent(
        self,
        intent: TradingIntent,
        policy: ExecutionPolicy,
        planning_errors: list[str],
        planning_rejections: list[str],
        *,
        market_context: InstrumentContext | None = None,
    ) -> ExecutionPlan | None:
        try:
            if market_context is None:
                market_context = await self._executor.market_context(intent.symbol)
            return self._planner.plan(
                intent,
                policy,
                market_context,
            )

        except ExecutionPlanningError as exc:
            reason = f"{intent.symbol}: {type(exc).__name__}: {exc}"
            planning_rejections.append(reason)
            logger.info("Execution candidate rejected: %s", reason)
            return None
        except TradeExecutionError as exc:
            error = f"{intent.symbol}: {type(exc).__name__}: {exc}"
            if exc.is_permanent_read_rejection:
                # Permanent rejections complete once; temporary read responses
                # retain their candidate for the existing bounded recovery path.
                planning_rejections.append(error)
                logger.info("Execution candidate rejected by exchange: %s", error)
                return None
            planning_errors.append(error)
            logger.exception(
                "Execution planning failed for candidate %s",
                intent.symbol,
            )
            return None
        except Exception as exc:
            error = f"{intent.symbol}: {type(exc).__name__}: {exc}"
            planning_errors.append(error)

            logger.exception(
                "Execution planning failed for candidate %s",
                intent.symbol,
            )
            return None

    def plan_position_actions(
        self,
        signals: SignalExtraction,
        *,
        account_state: AccountStateSummary | None,
        position_context: SignalPositionContext | None = None,
    ) -> tuple[PositionActionIntent, ...]:
        history = position_context.source_open_history if position_context else ()
        actions = tuple(
            action.model_copy(
                update={
                    "symbol": owned_action_symbol(
                        action.symbol,
                        history
                        if position_context is not None
                        and position_context.source_channel_id
                        == action.source.channel_id
                        else (),
                    )
                }
            )
            for action in signals.position_actions
        )
        return tuple(
            action.model_copy(
                update={
                    "approval_mode": (
                        self._position_action_approval_mode(
                            action,
                            account_state=(account_state),
                        )
                    )
                }
            )
            for action in actions
        )
