import pytest

from cautious_crypto_bro.telegram_replay import (
    TelegramReplayError,
    parse_telegram_post_url,
)


def test_parse_private_channel_post() -> None:
    reference = parse_telegram_post_url("https://t.me/c/2243423111/7905")

    assert reference.entity == -1002243423111

    assert reference.message_id == 7905


def test_parse_public_channel_post() -> None:
    reference = parse_telegram_post_url("https://t.me/example_channel/123")

    assert reference.entity == "example_channel"

    assert reference.message_id == 123


def test_parse_public_preview_post() -> None:
    reference = parse_telegram_post_url("https://t.me/s/example_channel/456")

    assert reference.entity == "example_channel"

    assert reference.message_id == 456


def test_reject_non_telegram_url() -> None:
    with pytest.raises(
        TelegramReplayError,
        match="t.me",
    ):
        parse_telegram_post_url("https://example.com/post/1")
