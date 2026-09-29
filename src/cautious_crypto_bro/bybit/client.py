from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from urllib.parse import urlencode

import httpx

from ..domain import TradeExecutionError
from .auth import BybitAuth

logger = logging.getLogger(__name__)

DEMO_BASE_URL = "https://api-demo.bybit.com"


class BybitClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
    ) -> None:
        self._http_client = httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(10.0),
        )
        self._auth = BybitAuth(
            api_key=api_key,
            api_secret=api_secret,
            public_get=self.public_get,
        )

    def close(
        self,
    ) -> None:
        self._http_client.close()

    def sync_clock(
        self,
        *,
        force: bool = False,
    ) -> None:
        self._auth.sync_clock(force=force)

    def public_get(
        self,
        path: str,
        params: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> dict:
        params = params or {}

        try:
            response = self._http_client.get(
                path,
                params=params,
            )

            response.raise_for_status()

        except httpx.HTTPError as exc:
            raise TradeExecutionError(f"Bybit HTTP request failed: {exc}") from exc

        data = response.json()

        if str(data.get("retCode", 0)) != "0":
            raise TradeExecutionError(
                f"Bybit rejected request: {data.get('retCode')} {data.get('retMsg')}"
            )

        return data

    def private_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
    ) -> dict:
        if method == "GET":
            encoded_payload = urlencode(
                [(key, str(value)) for key, value in payload.items()]
            )
        elif method == "POST":
            encoded_payload = json.dumps(
                payload,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        else:
            raise ValueError(f"Unsupported private Bybit HTTP method: {method}")

        for attempt in range(2):
            headers = self._auth.auth_headers(
                method,
                encoded_payload,
            )

            try:
                if method == "GET":
                    response = self._http_client.get(
                        f"{path}?{encoded_payload}",
                        headers=headers,
                    )
                else:
                    response = self._http_client.post(
                        path,
                        content=encoded_payload,
                        headers=headers,
                    )

                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise TradeExecutionError(f"Bybit HTTP request failed: {exc}") from exc

            data = response.json()
            code = data.get("retCode")

            if str(code) == "0":
                return data

            if str(code) == "10002" and attempt == 0:
                logger.warning(
                    "Bybit rejected request timestamp; re-synchronizing clock"
                )
                self._auth.sync_clock(force=True)
                continue

            raise TradeExecutionError(
                f"Bybit rejected request: {code} {data.get('retMsg')}"
            )

        raise TradeExecutionError("Bybit request failed after clock re-sync")

    def private_get(
        self,
        path: str,
        params: dict[str, object],
    ) -> dict:
        return self.private_request(
            "GET",
            path,
            params,
        )

    def private_post(
        self,
        path: str,
        body: dict[str, object],
    ) -> dict:
        return self.private_request(
            "POST",
            path,
            body,
        )

    def paginate_private_list(
        self,
        path: str,
        params: dict[str, object],
    ) -> list[dict]:
        params = dict(params)
        items: list[dict] = []

        while True:
            response = self.private_get(
                path,
                params,
            )

            result = response.get("result", {})

            page = result.get(
                "list",
                [],
            )

            items.extend(
                item
                for item in page
                if isinstance(
                    item,
                    dict,
                )
            )

            cursor = str(result.get("nextPageCursor") or "")

            if not cursor:
                break

            params["cursor"] = cursor

        return items

    def cancel_batch_best_effort(
        self,
        symbol: str,
        order_link_ids: list[str],
    ) -> None:
        if not order_link_ids:
            return

        try:
            response = self.private_post(
                "/v5/order/cancel-batch",
                {
                    "category": "linear",
                    "request": [
                        {
                            "symbol": symbol,
                            "orderLinkId": (order_link_id),
                        }
                        for order_link_id in order_link_ids
                    ],
                },
            )

            statuses = response.get(
                "retExtInfo",
                {},
            ).get(
                "list",
                [],
            )

            failed = [status for status in statuses if str(status.get("code")) != "0"]

            if failed:
                logger.error(
                    "Rollback cancellation returned failures: %s",
                    failed,
                )
        except Exception:
            logger.exception("Failed to roll back partially accepted Bybit batch")
