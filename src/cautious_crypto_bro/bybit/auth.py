from __future__ import annotations

import hashlib
import hmac
import logging
import time
from collections.abc import Callable
from typing import Any

from ..domain import TradeExecutionError

logger = logging.getLogger(__name__)

RECV_WINDOW_MS = 5_000
CLOCK_SYNC_TTL_SECONDS = 300


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


class BybitAuth:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        public_get: Callable[[str, dict[str, Any] | None], dict],
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._public_get = public_get
        self._clock_offset_ms = 0
        self._clock_synced_at = 0.0

    def sync_clock(
        self,
        *,
        force: bool = False,
    ) -> None:
        if (
            not force
            and self._clock_synced_at
            and (time.monotonic() - self._clock_synced_at) < CLOCK_SYNC_TTL_SECONDS
        ):
            return

        last_error: Exception | None = None

        for attempt in range(3):
            try:
                t0 = _wall_clock_ms()
                data = self._public_get(
                    "/v5/market/time",
                    {},
                )
                t1 = _wall_clock_ms()

                if str(data.get("retCode", 0)) != "0":
                    raise TradeExecutionError(
                        "Bybit server-time request failed: "
                        f"{data.get('retCode')} {data.get('retMsg')}"
                    )

                server_ms = self._server_time_ms(data)
                midpoint_ms = (t0 + t1) // 2
                self._clock_offset_ms = server_ms - midpoint_ms
                self._clock_synced_at = time.monotonic()

                logger.info(
                    "Bybit clock offset %+d ms (RTT %d ms)",
                    self._clock_offset_ms,
                    t1 - t0,
                )

                return

            except Exception as exc:
                last_error = exc

                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))

        raise TradeExecutionError(
            f"Could not synchronize clock with Bybit: {last_error}"
        )

    @staticmethod
    def _server_time_ms(
        data: dict,
    ) -> int:
        if data.get("time") is not None:
            return int(data["time"])

        result = data.get("result") or {}

        if result.get("timeNano") is not None:
            return int(result["timeNano"]) // 1_000_000

        if result.get("timeSecond") is not None:
            return int(result["timeSecond"]) * 1000

        raise TradeExecutionError("Bybit server-time response contained no timestamp")

    def timestamp(
        self,
    ) -> str:
        self.sync_clock()

        return str(_wall_clock_ms() + self._clock_offset_ms)

    def sign(
        self,
        payload: str,
        *,
        timestamp: str | None = None,
    ) -> str:
        if timestamp is None:
            timestamp = self.timestamp()

        signature_payload = timestamp + self._api_key + str(RECV_WINDOW_MS) + payload

        # HMAC-SHA256, which is what the Bybit V5 API specifies. CodeQL's
        # py/weak-sensitive-data-hashing rule flags SHA-256 here because it
        # targets password hashing and does not distinguish HMAC from a bare
        # digest. HMAC-SHA256 is not a weak choice, and the fix it appears to
        # suggest -- a different hash function -- would break API auth.
        return hmac.new(
            self._api_secret.encode(),
            signature_payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    def auth_headers(
        self,
        method: str,
        payload: str,
    ) -> dict[str, str]:
        timestamp = self.timestamp()

        headers = {
            "X-BAPI-API-KEY": self._api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
            "X-BAPI-SIGN": self.sign(
                payload,
                timestamp=timestamp,
            ),
        }

        if method == "POST":
            headers["Content-Type"] = "application/json"

        return headers
