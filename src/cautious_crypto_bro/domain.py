from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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


class ExecutionOrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


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
            return (
                f"https://t.me/"
                f"{self.channel_username.lstrip('@')}/"
                f"{self.message_id}"
            )

        channel_id = str(self.channel_id)

        if channel_id.startswith("-100"):
            return (
                f"https://t.me/c/"
                f"{channel_id[4:]}/"
                f"{self.message_id}"
            )

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
    def validate_shape(self) -> "Entry":
        if self.type is EntryType.MARKET:
            if any(
                value is not None
                for value in (
                    self.price,
                    self.range_low,
                    self.range_high,
                )
            ):
                raise ValueError(
                    "MARKET entry must not contain price or range"
                )

            return self

        if self.type is EntryType.LIMIT:
            if self.price is None:
                raise ValueError(
                    "LIMIT entry requires a price"
                )

            if (
                self.range_low is not None
                or self.range_high is not None
            ):
                raise ValueError(
                    "LIMIT entry must not contain a range"
                )

            return self

        if self.price is not None:
            raise ValueError(
                "RANGE entry must not contain a single price"
            )

        if (
            self.range_low is None
            or self.range_high is None
        ):
            raise ValueError(
                "RANGE entry requires range_low and range_high"
            )

        if self.range_low >= self.range_high:
            raise ValueError(
                "RANGE requires range_low < range_high"
            )

        return self


class ExtractedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    side: Side
    entry: Entry
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)


class IntentExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actionable: bool
    reason: str = Field(min_length=1, max_length=500)
    intent: ExtractedIntent | None = None

    @model_validator(mode="after")
    def consistent_actionability(
        self,
    ) -> "IntentExtraction":
        if self.actionable and self.intent is None:
            raise ValueError(
                "actionable=true requires intent"
            )

        if (
            not self.actionable
            and self.intent is not None
        ):
            raise ValueError(
                "actionable=false requires intent=null"
            )

        return self


class TradingIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: UUID = Field(default_factory=uuid4)
    source: SourceMessage
    symbol: str
    side: Side
    entry: Entry
    stop_loss: float = Field(gt=0)
    take_profit: float = Field(gt=0)
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0, le=1)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )
    status: IntentStatus = Field(
        default=IntentStatus.PENDING,
        exclude=True,
    )

    @model_validator(mode="after")
    def validate_trade_geometry(
        self,
    ) -> "TradingIntent":
        self.symbol = (
            self.symbol.upper()
            .replace("/", "")
            .replace("-", "")
        )

        if not self.symbol.endswith("USDT"):
            raise ValueError(
                "MVP supports only USDT linear symbols"
            )

        if self.entry.type is EntryType.MARKET:
            if (
                self.side is Side.LONG
                and not self.stop_loss
                < self.take_profit
            ):
                raise ValueError(
                    "LONG requires stop_loss < take_profit"
                )

            if (
                self.side is Side.SHORT
                and not self.take_profit
                < self.stop_loss
            ):
                raise ValueError(
                    "SHORT requires take_profit < stop_loss"
                )

            return self

        if self.entry.type is EntryType.LIMIT:
            references = (
                self.entry.price,
            )
        else:
            references = (
                self.entry.range_low,
                self.entry.range_high,
            )

        low = min(
            value
            for value in references
            if value is not None
        )
        high = max(
            value
            for value in references
            if value is not None
        )

        if (
            self.side is Side.LONG
            and not (
                self.stop_loss
                < low
                <= high
                < self.take_profit
            )
        ):
            raise ValueError(
                "LONG requires "
                "stop_loss < entry/range < take_profit"
            )

        if (
            self.side is Side.SHORT
            and not (
                self.take_profit
                < low
                <= high
                < self.stop_loss
            )
        ):
            raise ValueError(
                "SHORT requires "
                "take_profit < entry/range < stop_loss"
            )

        return self


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

    @property
    def risk_budget_usdt(self) -> Decimal:
        return (
            self.trading_capital_usdt
            * self.risk_per_trade_pct
            / Decimal("100")
        )


class PlannedOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_type: ExecutionOrderType
    quantity: Decimal = Field(gt=0)
    price: Decimal | None = Field(
        default=None,
        gt=0,
    )
    reference_price: Decimal = Field(gt=0)

    @model_validator(mode="after")
    def validate_shape(
        self,
    ) -> "PlannedOrder":
        if (
            self.order_type
            is ExecutionOrderType.MARKET
        ):
            if self.price is not None:
                raise ValueError(
                    "MARKET planned order "
                    "must not contain price"
                )
        elif self.price is None:
            raise ValueError(
                "LIMIT planned order requires price"
            )

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
        max_length=10,
    )
    stop_loss: Decimal = Field(gt=0)
    take_profit: Decimal = Field(gt=0)
    policy: ExecutionPolicy
    planned_max_loss_usdt: Decimal = Field(
        ge=0
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(
            timezone.utc
        )
    )

    @model_validator(mode="after")
    def validate_risk_budget(
        self,
    ) -> "ExecutionPlan":
        if (
            self.planned_max_loss_usdt
            > self.policy.risk_budget_usdt
        ):
            raise ValueError(
                "Execution plan exceeds "
                "configured risk budget"
            )

        return self
