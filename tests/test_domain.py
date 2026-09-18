from datetime import (
    UTC,
    datetime,
)

import pytest
from pydantic import ValidationError

from cautious_crypto_bro.domain import (
    Entry,
    EntryType,
    Side,
    SourceMessage,
    TradingIntent,
)


def source() -> SourceMessage:
    return SourceMessage(
        channel_id=-100123,
        channel_title="Trader",
        channel_username="trader",
        message_id=42,
        published_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        text=("LONG BTC 100k SL 98k TP 105k"),
    )


def test_valid_long_limit_intent() -> None:
    intent = TradingIntent(
        source=source(),
        symbol="BTC/USDT",
        side=Side.LONG,
        entry=Entry(
            type=EntryType.LIMIT,
            price=100_000,
        ),
        stop_loss=98_000,
        take_profit=105_000,
        summary="Long BTC from support.",
        confidence=0.95,
    )

    assert intent.symbol == "BTCUSDT"
    assert "status" not in intent.model_dump()


def test_range_requires_two_boundaries() -> None:
    with pytest.raises(ValidationError):
        Entry(
            type=EntryType.RANGE,
            range_low=99_000,
        )


def test_open_intent_rejects_quote_only_symbol() -> None:
    with pytest.raises(ValidationError):
        TradingIntent(
            source=source(),
            symbol="USDT",
            side=Side.LONG,
            entry=Entry(
                type=EntryType.MARKET,
            ),
            stop_loss=1,
            take_profit=None,
            summary="Invalid quote-only symbol",
            confidence=1,
        )
