from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class EntryType(StrEnum):
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
            return f"https://t.me/{self.channel_username.lstrip('@')}/{self.message_id}"
        return None


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: EntryType
    price: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_price(self) -> "Entry":
        if self.type is EntryType.LIMIT and self.price is None:
            raise ValueError("LIMIT entry requires a price")
        if self.type is EntryType.MARKET and self.price is not None:
            raise ValueError("MARKET entry must not contain a price")
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
    def consistent_actionability(self) -> "IntentExtraction":
        if self.actionable and self.intent is None:
            raise ValueError("actionable=true requires intent")
        if not self.actionable and self.intent is not None:
            raise ValueError("actionable=false requires intent=null")
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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: IntentStatus = IntentStatus.PENDING

    @model_validator(mode="after")
    def validate_trade_geometry(self) -> "TradingIntent":
        normalized = self.symbol.upper().replace("/", "").replace("-", "")
        object.__setattr__(self, "symbol", normalized)
        if not normalized.endswith("USDT"):
            raise ValueError("MVP supports only USDT linear symbols")

        reference = self.entry.price
        if reference is not None:
            if self.side is Side.LONG and not self.stop_loss < reference < self.take_profit:
                raise ValueError("LONG requires stop_loss < entry < take_profit")
            if self.side is Side.SHORT and not self.take_profit < reference < self.stop_loss:
                raise ValueError("SHORT requires take_profit < entry < stop_loss")
        else:
            if self.side is Side.LONG and not self.stop_loss < self.take_profit:
                raise ValueError("LONG requires stop_loss < take_profit")
            if self.side is Side.SHORT and not self.take_profit < self.stop_loss:
                raise ValueError("SHORT requires take_profit < stop_loss")
        return self
