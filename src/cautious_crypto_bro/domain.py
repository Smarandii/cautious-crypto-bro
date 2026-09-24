from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True, slots=True)
class ClosedPnlRecord:
    record_id: str
    order_id: str
    symbol: str
    position_side: Side
    closed_pnl: Decimal
    closed_size: Decimal
    avg_entry_price: Decimal | None
    avg_exit_price: Decimal | None
    updated_at: datetime


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    RANGE = "RANGE"


class PositionActionType(StrEnum):
    CANCEL_ENTRIES = "CANCEL_ENTRIES"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class OpenRelation(StrEnum):
    NEW = "NEW"
    ADD_OR_REENTRY = "ADD_OR_REENTRY"
    UPDATE_EXISTING = "UPDATE_EXISTING"
    UNCLASSIFIED = "UNCLASSIFIED"


class ApprovalMode(StrEnum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


class AutoApprovalMode(StrEnum):
    DISABLED = "disabled"
    OPEN_ONLY = "open_only"
    ALL = "all"


class ExecutionOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class StopLossSource(StrEnum):
    TRADER = "TRADER"
    POLICY = "POLICY"


class TakeProfitSource(StrEnum):
    TRADER = "TRADER"
    POLICY = "POLICY"


class IntentStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


class StrategyStatus(StrEnum):
    ENTERING = "ENTERING"
    OPEN_RISK = "OPEN_RISK"
    PROFIT_PROTECTED = "PROFIT_PROTECTED"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    UNCERTAIN = "UNCERTAIN"


def _normalize_usdt_symbol(
    symbol: str,
) -> str:
    normalized = symbol.strip().upper().replace("/", "").replace("-", "")

    if not normalized.endswith("USDT"):
        raise ValueError("MVP supports only USDT linear symbols")

    base_asset = normalized[:-4]

    if not base_asset or not base_asset.isalnum():
        raise ValueError("USDT symbol requires a non-empty alphanumeric base asset")

    return normalized


class SourceOpenContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: Side
    status: IntentStatus
    message_id: int
    created_at: datetime

    # True means a successful CCB copy from this
    # source still corresponds to current live
    # account exposure. None means live account
    # state was unavailable.
    active_copy: bool | None = None


class AccountPositionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: Side
    size: Decimal
    avg_price: Decimal


class SignalPositionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_channel_id: int
    account_state_available: bool

    source_open_history: tuple[
        SourceOpenContext,
        ...,
    ] = ()

    account_positions: tuple[
        AccountPositionContext,
        ...,
    ] = ()

    def has_existing_copy(
        self,
        symbol: str,
        side: Side,
    ) -> bool:
        symbol = symbol.upper()

        for item in self.source_open_history:
            if item.symbol != symbol or item.side is not side:
                continue

            if item.status in {
                IntentStatus.PENDING,
                IntentStatus.EXECUTING,
            }:
                return True

            if item.status is IntentStatus.EXECUTED and item.active_copy is True:
                return True

        return False


class SourceMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: int
    channel_title: str
    channel_username: str | None = None
    message_id: int
    published_at: datetime
    received_at: datetime
    text: str

    @property
    def telegram_url(self) -> str | None:
        if self.channel_username:
            return f"https://t.me/{self.channel_username.lstrip('@')}/{self.message_id}"

        channel_id = str(self.channel_id)

        if channel_id.startswith("-100"):
            return f"https://t.me/c/{channel_id[4:]}/{self.message_id}"

        return None


@dataclass(frozen=True, slots=True)
class ImageAttachment:
    media_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class IncomingPost:
    source: SourceMessage
    images: tuple[ImageAttachment, ...] = ()


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EntryType
    price: float | None = Field(default=None, gt=0)
    range_low: float | None = Field(default=None, gt=0)
    range_high: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_shape(self) -> Entry:
        if self.type is EntryType.MARKET:
            if any(
                value is not None
                for value in (
                    self.price,
                    self.range_low,
                    self.range_high,
                )
            ):
                raise ValueError("MARKET entry must not contain price or range")

            return self

        if self.type is EntryType.LIMIT:
            if self.price is None:
                raise ValueError("LIMIT entry requires a price")

            if self.range_low is not None or self.range_high is not None:
                raise ValueError("LIMIT entry must not contain a range")

            return self

        if self.price is not None:
            raise ValueError("RANGE entry must not contain a single price")

        if self.range_low is None or self.range_high is None:
            raise ValueError("RANGE entry requires range_low and range_high")

        if self.range_low >= self.range_high:
            raise ValueError("RANGE requires range_low < range_high")

        return self


class ExtractedEntryPayload(BaseModel):
    """Tolerant transport shape for LLM entry output."""

    model_config = ConfigDict(extra="forbid")

    type: str | None = None
    price: float | None = None
    range_low: float | None = None
    range_high: float | None = None


class ExtractedIntent(BaseModel):
    """LLM transport DTO.

    Deliberately more permissive than TradingIntent.
    Strict trading validation happens when this DTO is
    converted into the executable domain model.
    """

    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: str | None = None

    # Observed provider alias. Domain conversion still
    # normalizes this into the strict Side enum.
    direction: str | None = None

    # Relationship to trusted CCB copy state.
    # None is tolerated at the transport boundary but
    # is never eligible for automatic execution.
    relation: OpenRelation | None = None
    relation_evidence: str | None = Field(
        default=None,
        max_length=300,
    )

    # Providers/models have emitted both:
    #   "entry": "MARKET"
    # and:
    #   "entry": {"type": "MARKET", ...}
    entry: str | ExtractedEntryPayload | None = None

    # Compatibility with observed provider vocabulary.
    entry_semantics: str | None = None
    entry_type: str | None = None

    # Flat entry fields are preferred.
    price: float | None = None
    range_low: float | None = None
    range_high: float | None = None

    stop_loss: float | None = None
    take_profit: float | None = None

    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class ExtractedPositionAction(BaseModel):
    """Tolerant transport DTO for lifecycle instructions."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    action: str
    close_pct: float | None = None

    # Prefer expected_side, but tolerate the model's
    # natural tendency to emit side instead.
    expected_side: str | None = None
    side: str | None = None

    # Exact verbatim evidence from the CURRENT Telegram
    # post text/caption authorizing REDUCE/CLOSE.
    # Images may supply symbol/side context but cannot
    # independently authorize destructive actions.
    evidence_text: str | None = Field(
        default=None,
        max_length=300,
    )

    # These fields are intentionally tolerated because
    # models sometimes copy OPEN-position information
    # into HOLD/REDUCE/CLOSE objects. They are ignored
    # by lifecycle execution.
    entry: str | ExtractedEntryPayload | None = None
    entry_semantics: str | None = None
    price: float | None = None
    range_low: float | None = None
    range_high: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None

    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class IntentExtraction(BaseModel):
    """Raw model response.

    `actionable` is advisory model output only. The
    application derives real actionability after strict
    domain conversion.
    """

    model_config = ConfigDict(extra="forbid")

    actionable: bool
    reason: str = Field(min_length=1, max_length=500)

    intents: tuple[
        ExtractedIntent,
        ...,
    ] = Field(
        default=(),
        max_length=5,
    )

    position_actions: tuple[
        ExtractedPositionAction,
        ...,
    ] = Field(
        default=(),
        max_length=5,
    )


class TradingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: UUID = Field(default_factory=uuid4)
    source: SourceMessage
    symbol: str
    side: Side
    entry: Entry
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    relation: OpenRelation = OpenRelation.UNCLASSIFIED
    relation_evidence: str | None = Field(
        default=None,
        max_length=300,
    )

    approval_mode: ApprovalMode = Field(
        default=ApprovalMode.MANUAL,
        exclude=True,
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_trade_geometry(
        self,
    ) -> TradingIntent:
        self.symbol = _normalize_usdt_symbol(self.symbol)

        if self.entry.type is EntryType.MARKET:
            if self.take_profit is None or self.stop_loss is None:
                return self

            if self.side is Side.LONG and not self.stop_loss < self.take_profit:
                raise ValueError("LONG requires stop_loss < take_profit")

            if self.side is Side.SHORT and not self.take_profit < self.stop_loss:
                raise ValueError("SHORT requires take_profit < stop_loss")

            return self

        if self.entry.type is EntryType.LIMIT:
            references = (self.entry.price,)
        else:
            references = (
                self.entry.range_low,
                self.entry.range_high,
            )

        low = min(value for value in references if value is not None)
        high = max(value for value in references if value is not None)

        if self.side is Side.LONG:
            if self.stop_loss is not None and not self.stop_loss < low:
                raise ValueError("LONG requires stop_loss below entry/range")

            if self.take_profit is not None and not high < self.take_profit:
                raise ValueError("LONG requires take_profit above entry/range")

        else:
            if self.stop_loss is not None and not high < self.stop_loss:
                raise ValueError("SHORT requires stop_loss above entry/range")

            if self.take_profit is not None and not self.take_profit < low:
                raise ValueError("SHORT requires take_profit below entry/range")

        return self


class PositionActionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_id: UUID = Field(default_factory=uuid4)
    source: SourceMessage
    symbol: str
    action: PositionActionType
    close_pct: float | None = Field(
        default=None,
        gt=0,
        lt=100,
    )
    expected_side: Side | None = None
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)

    approval_mode: ApprovalMode = Field(
        default=ApprovalMode.MANUAL,
        exclude=True,
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_position_action(
        self,
    ) -> PositionActionIntent:
        self.symbol = _normalize_usdt_symbol(self.symbol)

        if self.action is PositionActionType.REDUCE and self.close_pct is None:
            raise ValueError("REDUCE requires close_pct")

        if self.action is not PositionActionType.REDUCE and self.close_pct is not None:
            raise ValueError("Only REDUCE may contain close_pct")

        return self


@dataclass(frozen=True, slots=True)
class SignalExtraction:
    open_intents: tuple[
        TradingIntent,
        ...,
    ] = ()
    position_actions: tuple[
        PositionActionIntent,
        ...,
    ] = ()

    @property
    def actionable(self) -> bool:
        return bool(self.open_intents or self.position_actions)


class ExitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    minimum_reward_bps: Decimal = Field(
        default=Decimal("20"),
        ge=0,
        le=1000,
    )

    basic_r_multiple: Decimal = Field(
        default=Decimal("0.5"),
        gt=0,
    )
    basic_close_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )

    medium_r_multiple: Decimal = Field(
        default=Decimal("1"),
        gt=0,
    )
    medium_close_pct: Decimal = Field(
        default=Decimal("35"),
        gt=0,
        lt=100,
    )

    high_r_multiple: Decimal = Field(
        default=Decimal("2"),
        gt=0,
    )
    high_close_pct: Decimal = Field(
        default=Decimal("40"),
        gt=0,
        lt=100,
    )

    @model_validator(mode="after")
    def validate_ladder(
        self,
    ) -> ExitPolicy:
        if not (self.basic_r_multiple < self.medium_r_multiple < self.high_r_multiple):
            raise ValueError("TP R-multiples must increase basic < medium < high")

        total = self.basic_close_pct + self.medium_close_pct + self.high_close_pct

        if total != Decimal("100"):
            raise ValueError("TP close percentages must total 100")

        return self

    @property
    def rules(
        self,
    ) -> tuple[
        tuple[str, Decimal, Decimal],
        ...,
    ]:
        return (
            (
                "BASIC",
                self.basic_r_multiple,
                self.basic_close_pct,
            ),
            (
                "MEDIUM",
                self.medium_r_multiple,
                self.medium_close_pct,
            ),
            (
                "HIGH",
                self.high_r_multiple,
                self.high_close_pct,
            ),
        )


class StrategyV2Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fallback_stop_distance_pct: Decimal = Field(default=Decimal("2"), gt=0, lt=100)

    primary_entry_risk_pct: Decimal = Field(
        default=Decimal("60"),
        gt=0,
        lt=100,
    )
    secondary_entry_risk_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )
    tertiary_entry_risk_pct: Decimal = Field(
        default=Decimal("15"),
        gt=0,
        lt=100,
    )

    secondary_entry_depth_r: Decimal = Field(
        default=Decimal("0.33"),
        gt=0,
        lt=1,
    )
    tertiary_entry_depth_r: Decimal = Field(
        default=Decimal("0.66"),
        gt=0,
        lt=1,
    )

    first_take_profit_r: Decimal = Field(
        default=Decimal("0.5"),
        gt=0,
    )
    second_take_profit_r: Decimal = Field(
        default=Decimal("1"),
        gt=0,
    )
    third_take_profit_r: Decimal = Field(
        default=Decimal("1.5"),
        gt=0,
    )

    first_take_profit_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )
    second_take_profit_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )
    third_take_profit_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )
    runner_pct: Decimal = Field(
        default=Decimal("25"),
        gt=0,
        lt=100,
    )

    trailing_activation_r: Decimal = Field(
        default=Decimal("0.5"),
        gt=0,
    )
    trailing_distance_r: Decimal = Field(
        default=Decimal("0.3"),
        gt=0,
    )
    minimum_locked_profit_r: Decimal = Field(
        default=Decimal("0.05"),
        ge=0,
    )

    @model_validator(mode="after")
    def validate_v2_policy(
        self,
    ) -> StrategyV2Policy:
        entry_total = (
            self.primary_entry_risk_pct
            + self.secondary_entry_risk_pct
            + self.tertiary_entry_risk_pct
        )

        if entry_total != Decimal("100"):
            raise ValueError("V2 entry risk percentages must total 100")

        if not (self.secondary_entry_depth_r < self.tertiary_entry_depth_r):
            raise ValueError("V2 entry depths must increase")

        if not (
            self.first_take_profit_r
            < self.second_take_profit_r
            < self.third_take_profit_r
        ):
            raise ValueError("V2 take-profit R multiples must increase")

        exit_total = (
            self.first_take_profit_pct
            + self.second_take_profit_pct
            + self.third_take_profit_pct
            + self.runner_pct
        )

        if exit_total != Decimal("100"):
            raise ValueError("V2 fixed exits and runner must total 100")

        if self.trailing_distance_r >= self.trailing_activation_r:
            raise ValueError("V2 trailing distance must be smaller than activation R")

        nominal_floor = self.trailing_activation_r - self.trailing_distance_r

        if self.minimum_locked_profit_r > nominal_floor:
            raise ValueError(
                "V2 minimum locked profit "
                "cannot exceed the nominal "
                "initial trailing floor"
            )

        return self

    @property
    def entry_rules(
        self,
    ) -> tuple[
        tuple[Decimal, Decimal],
        ...,
    ]:
        return (
            (
                Decimal("0"),
                self.primary_entry_risk_pct,
            ),
            (
                self.secondary_entry_depth_r,
                self.secondary_entry_risk_pct,
            ),
            (
                self.tertiary_entry_depth_r,
                self.tertiary_entry_risk_pct,
            ),
        )

    @property
    def exit_rules(
        self,
    ) -> tuple[
        tuple[str, Decimal, Decimal],
        ...,
    ]:
        return (
            (
                "TP1",
                self.first_take_profit_r,
                self.first_take_profit_pct,
            ),
            (
                "TP2",
                self.second_take_profit_r,
                self.second_take_profit_pct,
            ),
            (
                "TP3",
                self.third_take_profit_r,
                self.third_take_profit_pct,
            ),
        )


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Runtime configuration does not persist capital.
    # SignalService freezes live Bybit wallet balance
    # into each ExecutionPlan before planning.
    trading_capital_usdt: Decimal | None = Field(
        default=None,
        gt=0,
    )

    risk_per_trade_pct: Decimal = Field(
        default=Decimal("1"),
        gt=0,
        le=10,
    )

    # Historical V1 compatibility only. Keep accepting
    # these fields when old execution plans are loaded,
    # but never serialize them into new V2 plans.
    range_order_count: int = Field(
        default=3,
        ge=1,
        le=10,
        exclude=True,
    )
    exit_policy: ExitPolicy = Field(
        default_factory=ExitPolicy,
        exclude=True,
    )

    strategy_v2: StrategyV2Policy = Field(
        default_factory=StrategyV2Policy,
    )

    @property
    def risk_budget_usdt(self) -> Decimal:
        capital = self.trading_capital_usdt

        if capital is None:
            raise ValueError("Execution policy has no frozen live-capital snapshot")

        return capital * self.risk_per_trade_pct / Decimal("100")


class PlannedTakeProfit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    price: Decimal = Field(gt=0)
    close_pct: Decimal = Field(
        gt=0,
        le=100,
    )
    r_multiple: Decimal = Field(gt=0)


class PlannedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Defaults preserve historical V1 plan loading.
    name: str = Field(
        default="ENTRY",
        min_length=1,
        max_length=20,
    )
    risk_pct: Decimal | None = Field(
        default=None,
        gt=0,
        le=100,
    )

    order_type: ExecutionOrderType
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = Field(
        default=None,
        gt=0,
    )
    reference_price: Decimal = Field(gt=0)
    take_profit: Decimal | None = Field(
        default=None,
        gt=0,
    )

    @model_validator(mode="after")
    def validate_shape(
        self,
    ) -> PlannedOrder:
        if self.order_type is ExecutionOrderType.MARKET:
            if self.price is not None:
                raise ValueError("MARKET planned order must not contain price")
        elif self.price is None:
            raise ValueError("LIMIT planned order requires price")

        return self


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_version: int = Field(
        default=1,
        ge=1,
    )

    intent_id: UUID
    symbol: str
    side: Side
    orders: tuple[
        PlannedOrder,
        ...,
    ] = Field(
        min_length=1,
        max_length=20,
    )
    stop_loss: Decimal = Field(gt=0)
    stop_loss_source: StopLossSource = StopLossSource.TRADER
    take_profit: Decimal = Field(gt=0)
    take_profit_targets: tuple[
        PlannedTakeProfit,
        ...,
    ] = ()
    take_profit_source: TakeProfitSource = TakeProfitSource.TRADER
    trader_take_profit: Decimal | None = Field(default=None, gt=0)

    # V1 plans load as zero runner. New V2 plans set 25%.
    runner_pct: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        lt=100,
    )

    policy: ExecutionPolicy
    planned_max_loss_usdt: Decimal = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_risk_budget(
        self,
    ) -> ExecutionPlan:
        if self.planned_max_loss_usdt > self.policy.risk_budget_usdt:
            raise ValueError("Execution plan exceeds configured risk budget")

        if self.strategy_version >= 2:
            if len(self.orders) != 3:
                raise ValueError("Strategy V2 requires exactly three entry legs")

            if tuple(order.name for order in self.orders) != (
                "E1",
                "E2",
                "E3",
            ):
                raise ValueError("Strategy V2 entries must be E1, E2, E3")

            if any(order.risk_pct is None for order in self.orders):
                raise ValueError("Strategy V2 entries require risk allocations")

            entry_risk_total = sum(
                (order.risk_pct for order in self.orders if order.risk_pct is not None),
                Decimal("0"),
            )

            if entry_risk_total != Decimal("100"):
                raise ValueError("Strategy V2 entry risk allocations must total 100")

            if any(order.take_profit is not None for order in self.orders):
                raise ValueError("Strategy V2 entry orders must not own take profits")

            if len(self.take_profit_targets) != 3:
                raise ValueError("Strategy V2 requires exactly three fixed exits")

            total_close_pct = sum(
                (target.close_pct for target in self.take_profit_targets),
                Decimal("0"),
            )

            if total_close_pct + self.runner_pct != Decimal("100"):
                raise ValueError("Strategy V2 fixed exits and runner must total 100")

            return self

        if self.take_profit_targets:
            total_close_pct = sum(
                (target.close_pct for target in self.take_profit_targets),
                Decimal("0"),
            )

            if total_close_pct != Decimal("100"):
                raise ValueError("Planned TP close percentages must total 100")

        return self


class PositionStrategy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_id: UUID
    symbol: str
    side: Side
    status: StrategyStatus = StrategyStatus.ENTERING

    entry_frozen: bool = False

    # Highest/frozen live quantity observed by the
    # supervisor. Once entry_frozen=True, this is the
    # quantity against which the V2 exit buckets were
    # constructed.
    base_position_qty: Decimal | None = Field(
        default=None,
        gt=0,
    )

    last_position_qty: Decimal | None = Field(
        default=None,
        gt=0,
    )
    last_avg_price: Decimal | None = Field(
        default=None,
        gt=0,
    )

    tp1_done: bool = False
    tp2_done: bool = False
    tp3_done: bool = False

    trailing_active: bool = False

    # Exact protection last verified on Bybit.
    protected_stop_loss: Decimal | None = Field(
        default=None,
        gt=0,
    )
    trailing_distance: Decimal | None = Field(
        default=None,
        gt=0,
    )

    # Increment whenever exits are rebuilt after REDUCE.
    exit_revision: int = Field(
        default=0,
        ge=0,
    )

    rebalance_needed: bool = False
    installing_exits: bool = False

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_strategy(
        self,
    ) -> PositionStrategy:
        self.symbol = _normalize_usdt_symbol(self.symbol)

        return self
