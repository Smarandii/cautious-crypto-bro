from datetime import datetime, timezone

from cautious_crypto_bro.approval_bot import ApprovalBot
from cautious_crypto_bro.domain import Entry, EntryType, Side, SourceMessage, TradingIntent


def test_render_contains_decision_context() -> None:
    intent = TradingIntent(
        source=SourceMessage(
            channel_id=-100123,
            channel_title="Trader & Co",
            channel_username="trader",
            message_id=99,
            published_at=datetime(2026, 9, 12, 18, 0, tzinfo=timezone.utc),
            received_at=datetime(2026, 9, 12, 18, 0, 1, tzinfo=timezone.utc),
            text="signal",
        ),
        symbol="ETHUSDT", side=Side.LONG,
        entry=Entry(type=EntryType.LIMIT, price=4000),
        stop_loss=3900, take_profit=4300,
        summary="Bounce from support.", confidence=0.9,
    )
    rendered = ApprovalBot._render(intent)
    assert "LONG ETHUSDT" in rendered
    assert "R:R:" in rendered
    assert "Open source message" in rendered
    assert "Trader &amp; Co" in rendered
