"""Validate an explicitly supplied public trade feed before Pages publication."""

import json
import os
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cautious_crypto_bro.dashboard_balance import (  # noqa: E402
    MAX_BALANCE_BYTES,
    decode_balance_feed,
    validate_balance_feed,
)
from cautious_crypto_bro.dashboard_feed import (  # noqa: E402
    decode_public_feed,
)


def install_balance() -> None:
    encoded = os.environ.get("PUBLIC_BALANCE_FEED", "")
    try:
        if encoded:
            feed = decode_balance_feed(encoded)
        else:
            try:
                with urlopen(
                    "https://smarandii.github.io/cautious-crypto-bro/balance.json",
                    timeout=20,
                ) as response:
                    raw = response.read(MAX_BALANCE_BYTES + 1)
            except URLError:
                return  # No first publication yet: chart explicitly says unavailable.
            if len(raw) > MAX_BALANCE_BYTES:
                raise ValueError("Oversized public balance feed")
            feed = validate_balance_feed(json.loads(raw))
    except (ValueError, TypeError, UnicodeError):
        raise SystemExit("Public balance validation failed; input withheld") from None
    (ROOT / "docs" / "balance.json").write_text(
        json.dumps(feed, allow_nan=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    raw = os.environ.get("PUBLIC_TRADE_FEED", "")
    if not raw:
        # A source-only Pages rebuild must preserve the latest published feed.
        try:
            with urlopen(
                "https://smarandii.github.io/cautious-crypto-bro/trades.json",
                timeout=20,
            ) as response:
                raw = response.read(60001).decode("utf-8")
        except (URLError, UnicodeError):
            return  # Initial publication or outage: the UI explicitly shows unavailable.
    if len(raw.encode("utf-8")) > 60000:
        raise SystemExit("Trade feed exceeds public payload limit")
    try:
        feed = decode_public_feed(raw)
    except (ValueError, TypeError):
        raise SystemExit(
            "Public trade feed validation failed; input withheld"
        ) from None
    (ROOT / "docs" / "trades.json").write_text(
        json.dumps(feed, allow_nan=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
    install_balance()
