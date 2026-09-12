from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from cautious_crypto_bro.domain import Entry, EntryType, Side, SourceMessage, TradingIntent


def source() -> SourceMessage:
    return SourceMessage(
        channel_id=-100123,
        channel_title="Trader",
        channel_username="trader",
        message_id=42,
        published_at=datetime.now(timezone.utc),
        received_at=datetime.now(timezone.utc),
        text="LONG BTC 100k SL 98k TP 105k",
    )


def test_valid_long_limit_intent() -> None:
    intent = TradingIntent(
        source=source(), symbol="BTC/USDT", side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=100_000),
        stop_loss=98_000, take_profit=105_000,
        summary="Long BTC from support.", confidence=0.95,
    )
    assert intent.symbol == "BTCUSDT"


def test_long_geometry_rejects_stop_above_entry() -> None:
    with pytest.raises(ValidationError):
        TradingIntent(
            source=source(), symbol="BTCUSDT", side=Side.LONG,
            entry=Entry(type=EntryType.LIMIT, price=100_000),
            stop_loss=101_000, take_profit=105_000,
            summary="Invalid", confidence=0.5,
        )


def test_limit_requires_price() -> None:
    with pytest.raises(ValidationError):
        Entry(type=EntryType.LIMIT)


def test_market_rejects_price() -> None:
    with pytest.raises(ValidationError):
        Entry(type=EntryType.MARKET, price=100)

def test_private_channel_url() -> None:
    private_source = source().model_copy(
        update={
            "channel_id": -1002132062264,
            "channel_username": None,
            "message_id": 11482,
        }
    )

    assert private_source.telegram_url == "https://t.me/c/2132062264/11482"
