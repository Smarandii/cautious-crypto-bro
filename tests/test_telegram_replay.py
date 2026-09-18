from cautious_crypto_bro.telegram_replay import (
    parse_telegram_post_url,
)


def test_parse_private_channel_post() -> None:
    reference = parse_telegram_post_url("https://t.me/c/2243423111/7905")

    assert reference.entity == -1002243423111

    assert reference.message_id == 7905
