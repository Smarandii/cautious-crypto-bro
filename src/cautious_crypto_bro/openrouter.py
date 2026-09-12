from __future__ import annotations

import asyncio
import base64
import logging
import time

import httpx

from .domain import (
    IncomingPost,
    IntentExtraction,
    TradingIntent,
)

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a conservative crypto trading-signal parser.
Do not give trading advice and do not invent missing information.

Decide whether ONE Telegram post, including any attached images,
contains one complete, immediately actionable trade proposal
representable by the supplied schema.

MVP rules:
- use the text/caption and attached images together
- only USDT linear/perpetual-style symbols
- required: symbol, LONG/SHORT, entry semantics, stop loss, take profit
- MARKET only if the author clearly says enter now/at market or clearly
  states they entered now
- otherwise an explicit numeric entry price is required and entry.type
  must be LIMIT
- exactly one stop and one target
- values visible in an image may be used only when they are explicit
  and clearly legible
- never estimate prices from chart geometry, line position, vague levels,
  or unlabeled visual elements
- if the post is commentary, an update to an older idea, incomplete,
  ambiguous, or contains multiple conflicting setups, actionable=false
- confidence is extraction confidence, not probability of profit
- summary is one short sentence describing the trader's stated thesis
""".strip()


def _build_user_content(
    post: IncomingPost,
) -> str | list[dict[str, object]]:
    source = post.source

    text = (
        f"Channel: {source.channel_title}\n"
        f"Published: {source.published_at.isoformat()}\n\n"
        "Telegram post text/caption:\n"
        f"{source.text or '(no caption)'}"
    )

    if not post.images:
        return text

    content: list[dict[str, object]] = [
        {
            "type": "text",
            "text": text,
        }
    ]

    for image in post.images:
        encoded = base64.b64encode(
            image.data
        ).decode("ascii")

        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": (
                        f"data:{image.media_type};"
                        f"base64,{encoded}"
                    ),
                },
            }
        )

    return content


class OpenRouterIntentExtractor:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
    ) -> None:
        self._model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(45.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def extract(
        self,
        post: IncomingPost,
    ) -> TradingIntent | None:
        source = post.source

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": _build_user_content(post),
                },
            ],
            "temperature": 0,
            "max_tokens": 512,
            "reasoning": {
                "effort": "none",
            },
            "provider": {
                "sort": "latency",
                "require_parameters": True,
            },
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "trading_intent_extraction",
                    "strict": True,
                    "schema": (
                        IntentExtraction.model_json_schema()
                    ),
                },
            },
        }

        started = time.monotonic()

        try:
            async with asyncio.timeout(20):
                response = await self._client.post(
                    "/chat/completions",
                    json=payload,
                )
        except TimeoutError:
            elapsed = time.monotonic() - started

            raise RuntimeError(
                "OpenRouter inference exceeded "
                f"20s ({elapsed:.1f}s)"
            ) from None

        elapsed = time.monotonic() - started

        logger.info(
            "OpenRouter inference for %s/%s "
            "(%d image(s)) completed in %.2fs",
            source.channel_id,
            source.message_id,
            len(post.images),
            elapsed,
        )

        response.raise_for_status()

        content = response.json()[
            "choices"
        ][0]["message"]["content"]

        extraction = (
            IntentExtraction.model_validate_json(content)
        )

        if (
            not extraction.actionable
            or extraction.intent is None
        ):
            logger.info(
                "No actionable intent for %s/%s: %s",
                source.channel_id,
                source.message_id,
                extraction.reason,
            )
            return None

        raw = extraction.intent

        return TradingIntent(
            source=source,
            symbol=raw.symbol,
            side=raw.side,
            entry=raw.entry,
            stop_loss=raw.stop_loss,
            take_profit=raw.take_profit,
            summary=raw.summary,
            confidence=raw.confidence,
        )
