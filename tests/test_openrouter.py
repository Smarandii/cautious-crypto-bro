from datetime import datetime, timezone

from cautious_crypto_bro.domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)
from cautious_crypto_bro.openrouter import (
    _build_user_content,
)


def source() -> SourceMessage:
    now = datetime.now(timezone.utc)

    return SourceMessage(
        channel_id=-1001234567890,
        channel_title="Test channel",
        channel_username=None,
        message_id=123,
        published_at=now,
        received_at=now,
        text="LONG BTCUSDT",
    )


def test_text_post_keeps_text_content() -> None:
    content = _build_user_content(
        IncomingPost(source=source())
    )

    assert isinstance(content, str)
    assert "LONG BTCUSDT" in content


def test_image_post_builds_base64_content() -> None:
    content = _build_user_content(
        IncomingPost(
            source=source(),
            images=(
                ImageAttachment(
                    media_type="image/png",
                    data=b"\x01\x02",
                ),
            ),
        )
    )

    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1] == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64,AQI=",
        },
    }
