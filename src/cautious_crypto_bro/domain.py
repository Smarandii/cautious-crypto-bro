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


class EntryType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    RANGE = "RANGE"


class PositionActionType(StrEnum):
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class ExecutionOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TakeProfitSource(StrEnum):
    TRADER = "TRADER"
    POLICY = "POLICY"


class IntentStatus(StrEnum):
    PENDING = "PENDING"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


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

    # Providers/models have emitted both:
    #   "entry": "MARKET"
    # and:
    #   "entry": {"type": "MARKET", ...}
    entry: str | ExtractedEntryPayload | None = None

    # Compatibility with observed provider vocabulary.
    entry_semantics: str | None = None

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
    stop_loss: float = Field(gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_trade_geometry(
        self,
    ) -> TradingIntent:
        self.symbol = self.symbol.upper().replace("/", "").replace("-", "")

        if not self.symbol.endswith("USDT"):
            raise ValueError("MVP supports only USDT linear symbols")

        if self.entry.type is EntryType.MARKET:
            if self.take_profit is None:
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
            if not self.stop_loss < low:
                raise ValueError("LONG requires stop_loss below entry/range")

            if self.take_profit is not None and not high < self.take_profit:
                raise ValueError("LONG requires take_profit above entry/range")

        else:
            if not high < self.stop_loss:
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_position_action(
        self,
    ) -> PositionActionIntent:
        self.symbol = self.symbol.upper().replace("/", "").replace("-", "")

        if not self.symbol.endswith("USDT"):
            raise ValueError("MVP supports only USDT linear symbols")

        if self.action is PositionActionType.REDUCE and self.close_pct is None:
            raise ValueError("REDUCE requires close_pct")

        if self.action is PositionActionType.CLOSE and self.close_pct is not None:
            raise ValueError("CLOSE must not contain close_pct")

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


class ExecutionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trading_capital_usdt: Decimal = Field(gt=0)
    risk_per_trade_pct: Decimal = Field(
        gt=0,
        le=10,
    )
    range_order_count: int = Field(
        ge=1,
        le=10,
    )
    exit_policy: ExitPolicy = Field(
        default_factory=ExitPolicy,
    )

    @property
    def risk_budget_usdt(self) -> Decimal:
        return self.trading_capital_usdt * self.risk_per_trade_pct / Decimal("100")


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
    take_profit: Decimal = Field(gt=0)
    take_profit_targets: tuple[
        PlannedTakeProfit,
        ...,
    ] = ()
    take_profit_source: TakeProfitSource = TakeProfitSource.TRADER
    policy: ExecutionPolicy
    planned_max_loss_usdt: Decimal = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_risk_budget(
        self,
    ) -> ExecutionPlan:
        if self.planned_max_loss_usdt > self.policy.risk_budget_usdt:
            raise ValueError("Execution plan exceeds configured risk budget")

        if self.take_profit_targets:
            total_close_pct = sum(
                (target.close_pct for target in self.take_profit_targets),
                Decimal("0"),
            )

            if total_close_pct != Decimal("100"):
                raise ValueError("Planned TP close percentages must total 100")

        return self
