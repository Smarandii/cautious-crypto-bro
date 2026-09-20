from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from collections import Counter
from datetime import (
    UTC,
    datetime,
    timedelta,
)
from pathlib import Path
from typing import Any

import aiosqlite
from telethon import TelegramClient

from cautious_crypto_bro.bybit import (
    BybitDemoExecutor,
)
from cautious_crypto_bro.config import (
    get_settings,
)
from cautious_crypto_bro.domain import (
    IntentExtraction,
    SignalPositionContext,
)
from cautious_crypto_bro.llm_factory import (
    build_intent_extractor,
)
from cautious_crypto_bro.openrouter import (
    SYSTEM_PROMPT,
    IntentExtractor,
    _evaluation_fingerprint,
    _signals_from_extraction,
)
from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)
from cautious_crypto_bro.signal_context import (
    build_position_context,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)
from cautious_crypto_bro.telegram_replay import (
    TelegramReplayError,
    cloned_telegram_session,
)
from cautious_crypto_bro.telegram_source import (
    _as_utc,
    _group_telegram_messages,
    telegram_messages_to_post,
)

CATEGORY_ORDER = (
    "ACTIONABLE",
    "NON_ACTIONABLE",
    "PIPELINE_IGNORED",
    "EVALUATION_ERROR",
)

IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def raw_group_text(
    messages: tuple[Any, ...],
) -> str:
    texts: list[str] = []

    for message in messages:
        text = (message.message or "").strip()

        if text and text not in texts:
            texts.append(text)

    return "\n".join(texts)


def media_metadata(
    message: Any,
) -> dict[str, Any]:
    file = getattr(
        message,
        "file",
        None,
    )

    mime_type = (
        getattr(
            file,
            "mime_type",
            None,
        )
        if file is not None
        else None
    )

    if (
        getattr(
            message,
            "photo",
            None,
        )
        is not None
    ):
        kind = "photo"
        mime_type = mime_type or "image/jpeg"

    elif getattr(
        message,
        "voice",
        None,
    ):
        kind = "voice"

    elif getattr(
        message,
        "video",
        None,
    ):
        kind = "video"

    elif getattr(
        message,
        "audio",
        None,
    ):
        kind = "audio"

    elif mime_type and mime_type.startswith("image/"):
        kind = "image"

    elif mime_type and mime_type.startswith("video/"):
        kind = "video"

    elif mime_type and mime_type.startswith("audio/"):
        kind = "audio"

    elif getattr(
        message,
        "document",
        None,
    ):
        kind = "document"

    else:
        kind = "none"

    return {
        "message_id": message.id,
        "grouped_id": getattr(
            message,
            "grouped_id",
            None,
        ),
        "kind": kind,
        "mime_type": mime_type,
        "file_name": (
            getattr(
                file,
                "name",
                None,
            )
            if file is not None
            else None
        ),
    }


def telegram_url(
    entity: Any,
    channel_id: int,
    message_id: int,
) -> str:
    username = getattr(
        entity,
        "username",
        None,
    )

    if username:
        return f"https://t.me/{username}/{message_id}"

    raw = str(abs(channel_id))

    if raw.startswith("100"):
        raw = raw[3:]

    return f"https://t.me/c/{raw}/{message_id}"


async def fetch_recent_groups(
    client: TelegramClient,
    entity: Any,
    cutoff: datetime,
) -> tuple[
    tuple[Any, ...],
    ...,
]:
    messages: list[Any] = []

    last_included_group: int | None = None

    async for message in client.iter_messages(entity):
        grouped_id = getattr(
            message,
            "grouped_id",
            None,
        )

        if _as_utc(message.date) < cutoff:
            # Mirror production startup-lookback
            # behavior when cutoff intersects an album.
            if last_included_group is not None and grouped_id == last_included_group:
                messages.append(message)
                continue

            break

        messages.append(message)

        last_included_group = grouped_id

    return _group_telegram_messages(messages)


async def current_app_state(
    database_path: Path,
    channel_id: int,
    message_id: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source_processing": None,
        "persisted_intents": [],
        "persisted_position_actions": [],
    }

    async with aiosqlite.connect(database_path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(
            """
            SELECT
                status,
                attempt_count,
                last_error
            FROM source_messages
            WHERE
                channel_id = ?
                AND message_id = ?
            """,
            (
                channel_id,
                message_id,
            ),
        )

        row = await cursor.fetchone()

        if row is not None:
            result["source_processing"] = {
                "status": row["status"],
                "attempt_count": row["attempt_count"],
                "last_error": row["last_error"],
            }

        cursor = await db.execute(
            """
            SELECT
                intent_id,
                status,
                error,
                created_at
            FROM intents
            WHERE
                channel_id = ?
                AND message_id = ?
            ORDER BY created_at
            """,
            (
                channel_id,
                message_id,
            ),
        )

        rows = await cursor.fetchall()

        result["persisted_intents"] = [
            {
                "intent_id": row["intent_id"],
                "status": row["status"],
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

        cursor = await db.execute(
            """
            SELECT
                action_id,
                status,
                error,
                created_at
            FROM position_actions
            WHERE
                channel_id = ?
                AND message_id = ?
            ORDER BY created_at
            """,
            (
                channel_id,
                message_id,
            ),
        )

        rows = await cursor.fetchall()

        result["persisted_position_actions"] = [
            {
                "action_id": row["action_id"],
                "status": row["status"],
                "error": row["error"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    return result


async def historical_position_context(
    *,
    store: IntentStore,
    executor: BybitDemoExecutor,
    channel_id: int,
    published_at: datetime,
) -> tuple[SignalPositionContext, str | None]:
    source_intents = tuple(
        intent
        for intent in await store.get_recent_source_intents(channel_id)
        if intent.created_at < published_at
    )

    executed_closes = tuple(
        action
        for action in await store.get_recent_executed_closes()
        if action.created_at < published_at
    )

    account_state = None
    account_state_error = None

    try:
        account_state = await executor.account_state()
    except Exception as exc:
        account_state_error = f"{type(exc).__name__}: {exc}"

    return (
        build_position_context(
            channel_id=channel_id,
            source_intents=source_intents,
            executed_closes=executed_closes,
            account_state=account_state,
        ),
        account_state_error,
    )


async def read_evaluation(
    *,
    post: Any,
    global_guidance: str | None,
    channel_guidance: str | None,
    position_context: SignalPositionContext,
    runtime_store: RedisRuntimeStore,
    extractor: IntentExtractor,
    cache_only: bool,
    fresh: bool,
) -> tuple[
    IntentExtraction | None,
    str,
    str | None,
]:
    fingerprint = _evaluation_fingerprint(
        post,
        model=extractor.cache_identity,
        global_guidance=global_guidance,
        channel_guidance=channel_guidance,
        position_context=position_context,
    )

    if not fresh:
        try:
            payload = await runtime_store.get_evaluation(fingerprint)
        except Exception as exc:
            return (
                None,
                "cache-read-error",
                (f"{type(exc).__name__}: {exc}"),
            )

        if payload is not None:
            try:
                return (
                    IntentExtraction.model_validate_json(payload),
                    "cache",
                    None,
                )
            except Exception as exc:
                return (
                    None,
                    "invalid-cache",
                    (f"{type(exc).__name__}: {exc}"),
                )

    if cache_only:
        return (
            None,
            "cache-miss",
            ("No matching cached production evaluation"),
        )

    try:
        await extractor.extract(
            post,
            global_guidance=global_guidance,
            channel_guidance=channel_guidance,
            position_context=position_context,
            bypass_evaluation_cache=fresh,
        )
    except Exception as exc:
        return (
            None,
            "fresh-error",
            (f"{type(exc).__name__}: {exc}"),
        )

    try:
        payload = await runtime_store.get_evaluation(fingerprint)
    except Exception as exc:
        return (
            None,
            "fresh-cache-read-error",
            (f"{type(exc).__name__}: {exc}"),
        )

    if payload is None:
        return (
            None,
            "fresh-no-cache",
            (
                "Fresh extraction completed "
                "but no validated evaluation "
                "was available in Redis"
            ),
        )

    try:
        extraction = IntentExtraction.model_validate_json(payload)
    except Exception as exc:
        return (
            None,
            "fresh-invalid-cache",
            (f"{type(exc).__name__}: {exc}"),
        )

    return (
        extraction,
        "fresh",
        None,
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit recent Telegram posts against current LLM signal decisions."
        )
    )

    parser.add_argument(
        "--hours",
        type=float,
        default=5.0,
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("audit-output"),
    )

    parser.add_argument(
        "--cache-only",
        action="store_true",
        help=("Do not call the LLM provider for cache misses."),
    )

    parser.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Ignore matching "
            "evaluation-cache reads and "
            "re-evaluate every supported post."
        ),
    )

    args = parser.parse_args()

    if args.hours <= 0:
        parser.error("--hours must be positive")

    if args.cache_only and args.fresh:
        parser.error("--cache-only and --fresh cannot be used together")

    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s %(levelname)s %(name)s: %(message)s"),
    )

    settings = get_settings()

    output_dir = args.output_dir.resolve()

    images_dir = output_dir / "images"

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    images_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    generated_at = datetime.now(UTC)

    cutoff = generated_at - timedelta(hours=args.hours)

    store = IntentStore(settings.database_path)
    await store.initialize()

    runtime_store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(settings.redis_max_connections),
        pool_timeout_seconds=(settings.redis_pool_timeout_seconds),
    )

    await runtime_store.initialize()

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )

    extractor = build_intent_extractor(
        settings,
        evaluation_cache=runtime_store,
        provider_cooldown_store=runtime_store,
        persist_provider_cooldowns=False,
    )

    records: list[dict[str, Any]] = []

    channel_guidance_report: dict[
        str,
        str | None,
    ] = {}

    global_guidance_report: str | None = None

    sequence = 0

    try:
        with cloned_telegram_session(settings.telegram_session_name) as audit_session:
            client = TelegramClient(
                audit_session,
                settings.telegram_api_id,
                settings.telegram_api_hash,
                receive_updates=False,
            )

            await client.connect()

            try:
                if not (await client.is_user_authorized()):
                    raise TelegramReplayError(
                        "Cloned Telegram session is not authorized"
                    )

                dialogs_loaded = False

                for configured_channel in settings.telegram_source_channels:
                    try:
                        entity = await client.get_entity(configured_channel)
                    except ValueError:
                        if not dialogs_loaded:
                            await client.get_dialogs()

                            dialogs_loaded = True

                        entity = await client.get_entity(configured_channel)

                    channel_title = getattr(
                        entity,
                        "title",
                        None,
                    ) or str(configured_channel)

                    print(f"Fetching {channel_title}...")

                    groups = await fetch_recent_groups(
                        client,
                        entity,
                        cutoff,
                    )

                    print(f"  {len(groups)} post(s)")

                    for group in groups:
                        sequence += 1

                        group = tuple(group)

                        canonical = group[0]

                        channel_id = int(canonical.chat_id)

                        message_id = int(min(message.id for message in group))

                        published_at = min(_as_utc(message.date) for message in group)

                        raw_text = raw_group_text(group)

                        raw_media = [media_metadata(message) for message in group]

                        (
                            global_guidance,
                            channel_guidance,
                        ) = await store.get_guidance(channel_id)

                        if global_guidance_report is None:
                            global_guidance_report = global_guidance

                        channel_guidance_report[(f"{channel_title} ({channel_id})")] = (
                            channel_guidance
                        )

                        app_state = await current_app_state(
                            settings.database_path,
                            channel_id,
                            message_id,
                        )

                        record: dict[
                            str,
                            Any,
                        ] = {
                            "channel_title": (channel_title),
                            "channel_id": (channel_id),
                            "message_id": (message_id),
                            "message_ids": [message.id for message in group],
                            "published_at": (published_at.isoformat()),
                            "url": telegram_url(
                                entity,
                                channel_id,
                                message_id,
                            ),
                            "raw_text": (raw_text),
                            "raw_media": (raw_media),
                            "app_state": (app_state),
                            "images": [],
                            "evaluation": None,
                            "evaluation_error": (None),
                            "decision_source": ("not-evaluated"),
                        }

                        try:
                            post = await telegram_messages_to_post(
                                group,
                                chat=entity,
                            )
                        except Exception as exc:
                            record["category"] = "EVALUATION_ERROR"

                            record["evaluation_error"] = (
                                f"Telegram conversion: {type(exc).__name__}: {exc}"
                            )

                            records.append(record)
                            continue

                        if post is None:
                            record["category"] = "PIPELINE_IGNORED"

                            record["decision_source"] = "production-parser"

                            record["evaluation_error"] = (
                                "Production Telegram "
                                "conversion produced no "
                                "supported text/image post"
                            )

                            records.append(record)
                            continue

                        for image_index, image in enumerate(
                            post.images,
                            start=1,
                        ):
                            extension = IMAGE_EXTENSIONS.get(
                                image.media_type,
                                ".bin",
                            )

                            filename = (
                                f"{sequence:04d}_"
                                f"{channel_id}_"
                                f"{message_id}_"
                                f"{image_index}"
                                f"{extension}"
                            )

                            image_path = images_dir / filename

                            image_path.write_bytes(image.data)

                            record["images"].append(
                                {
                                    "filename": (filename),
                                    "media_type": (image.media_type),
                                    "sha256": hashlib.sha256(image.data).hexdigest(),
                                }
                            )

                        try:
                            (
                                position_context,
                                account_state_error,
                            ) = await historical_position_context(
                                store=store,
                                executor=executor,
                                channel_id=channel_id,
                                published_at=post.source.published_at,
                            )
                        except Exception as exc:
                            record["category"] = "EVALUATION_ERROR"
                            record["decision_source"] = "context-error"
                            record["evaluation_error"] = (
                                f"Context snapshot: {type(exc).__name__}: {exc}"
                            )
                            records.append(record)
                            continue

                        record["position_context"] = json.loads(
                            position_context.model_dump_json()
                        )
                        record["account_state_error"] = account_state_error

                        (
                            extraction,
                            decision_source,
                            evaluation_error,
                        ) = await read_evaluation(
                            post=post,
                            global_guidance=(global_guidance),
                            channel_guidance=(channel_guidance),
                            position_context=position_context,
                            runtime_store=(runtime_store),
                            extractor=(extractor),
                            cache_only=(args.cache_only),
                            fresh=args.fresh,
                        )

                        record["decision_source"] = decision_source

                        record["evaluation_error"] = evaluation_error

                        if extraction is None:
                            record["category"] = "EVALUATION_ERROR"

                        else:
                            record["evaluation"] = json.loads(
                                extraction.model_dump_json()
                            )

                            executable = _signals_from_extraction(
                                post.source,
                                extraction,
                            )

                            record["derived_actionable"] = executable.actionable

                            record["derived_open_intents"] = [
                                json.loads(intent.model_dump_json())
                                for intent in executable.open_intents
                            ]

                            record["derived_position_actions"] = [
                                json.loads(action.model_dump_json())
                                for action in executable.position_actions
                            ]

                            if executable.actionable:
                                record["category"] = "ACTIONABLE"
                            else:
                                record["category"] = "NON_ACTIONABLE"

                        records.append(record)

            finally:
                await client.disconnect()

    finally:
        await extractor.close()
        await runtime_store.close()
        executor.close()

    records.sort(key=lambda record: record["published_at"])

    bundle_path = output_dir / "audit_bundle.json"

    bundle = {
        "generated_at": (generated_at.isoformat()),
        "cutoff": (cutoff.isoformat()),
        "hours": args.hours,
        "fresh": args.fresh,
        "provider": settings.llm_provider,
        "model": extractor.cache_identity,
        "system_prompt": (SYSTEM_PROMPT),
        "global_guidance": (global_guidance_report),
        "channel_guidance": (channel_guidance_report),
        "records": records,
    }

    bundle_path.write_text(
        json.dumps(
            bundle,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    counts = Counter(record["category"] for record in records)

    print()
    print("=== AUDIT COMPLETE ===")
    print(f"Total posts: {len(records)}")

    for category in CATEGORY_ORDER:
        print(f"{category}: {counts.get(category, 0)}")

    print()
    print(f"Bundle: {bundle_path}")
    print(f"Images: {images_dir}")

    fresh_count = sum(1 for record in records if (record["decision_source"] == "fresh"))

    cache_count = sum(1 for record in records if (record["decision_source"] == "cache"))

    print()
    print(f"Cached decisions reused: {cache_count}")
    print(f"Fresh LLM calls: {fresh_count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
