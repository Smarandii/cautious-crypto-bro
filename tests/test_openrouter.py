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
            "url": (
                "data:image/png;base64,AQI="
            ),
        },
    }


def test_guidance_is_added_to_prompt() -> None:
    content = _build_user_content(
        IncomingPost(source=source()),
        global_guidance="Global rule",
        channel_guidance="Trader rule",
    )

    assert isinstance(content, str)

    assert (
        "Global guidance:\nGlobal rule"
        in content
    )

    assert (
        "Channel-specific guidance:\n"
        "Trader rule"
        in content
    )

    assert content.index(
        "Global guidance:"
    ) < content.index(
        "Channel-specific guidance:"
    )

    assert content.index(
        "Channel-specific guidance:"
    ) < content.index(
        "Telegram post text/caption:"
    )


def test_provider_error_inside_http_success_is_rejected() -> None:
    from cautious_crypto_bro.openrouter import (
        _completion_content,
    )

    response = {
        "provider": "NextBit",
        "choices": [
            {
                "finish_reason": "error",
                "error": {
                    "code": 502,
                    "message": (
                        "Network connection lost."
                    ),
                    "metadata": {
                        "error_type": (
                            "provider_unavailable"
                        ),
                    },
                },
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"actionable": true'
                    ),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=(
            "provider=NextBit.*"
            "code=502.*"
            "Network connection lost"
        ),
    ):
        _completion_content(
            response
        )


def test_truncated_completion_is_rejected() -> None:
    from cautious_crypto_bro.openrouter import (
        _completion_content,
    )

    response = {
        "provider": "Parasail",
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": (
                        '{"actionable": true'
                        + " " * 1000
                    ),
                },
            }
        ],
    }

    import pytest

    with pytest.raises(
        ValueError,
        match=(
            "provider=Parasail.*"
            "finish_reason=length"
        ),
    ):
        _completion_content(
            response
        )
