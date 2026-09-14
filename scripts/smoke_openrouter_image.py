from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
from datetime import UTC, datetime
from pathlib import Path

from cautious_crypto_bro.config import (
    get_settings,
)
from cautious_crypto_bro.domain import (
    ImageAttachment,
    IncomingPost,
    SourceMessage,
)
from cautious_crypto_bro.openrouter import (
    OpenRouterIntentExtractor,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=("%(levelname)s %(name)s: %(message)s"),
    )

    parser = argparse.ArgumentParser(
        description=("Run a local image through the production intent extractor.")
    )

    parser.add_argument(
        "image",
        type=Path,
    )

    parser.add_argument(
        "--caption",
        default="",
    )

    parser.add_argument(
        "--channel-id",
        type=int,
        default=0,
    )

    parser.add_argument(
        "--channel-title",
        default="CCB image smoke test",
    )

    args = parser.parse_args()

    if not args.image.is_file():
        raise SystemExit(f"Image not found: {args.image}")

    media_type, _ = mimetypes.guess_type(args.image.name)

    if not media_type or not media_type.startswith("image/"):
        raise SystemExit(f"Could not determine image MIME type: {args.image}")

    settings = get_settings()

    store = IntentStore(settings.database_path)

    await store.initialize()

    global_guidance, channel_guidance = await store.get_guidance(args.channel_id)

    print(
        "Guidance: "
        f"global={'yes' if global_guidance else 'no'}, "
        f"channel={'yes' if channel_guidance else 'no'}"
    )

    now = datetime.now(UTC)

    post = IncomingPost(
        source=SourceMessage(
            channel_id=args.channel_id,
            channel_title=args.channel_title,
            channel_username=None,
            message_id=0,
            published_at=now,
            received_at=now,
            text=args.caption,
        ),
        images=(
            ImageAttachment(
                media_type=media_type,
                data=args.image.read_bytes(),
            ),
        ),
    )

    extractor = OpenRouterIntentExtractor(
        api_key=settings.openrouter_api_key,
        model=settings.openrouter_model,
        base_url=settings.openrouter_base_url,
        inference_timeout_seconds=(settings.openrouter_inference_timeout_seconds),
        max_attempts=(settings.openrouter_inference_max_attempts),
    )

    try:
        intents = await extractor.extract(
            post,
            global_guidance=global_guidance,
            channel_guidance=channel_guidance,
        )
    finally:
        await extractor.close()

    if not intents:
        print("NO ACTIONABLE INTENT")
        return

    print(f"ACTIONABLE INTENTS: {len(intents)}")

    for index, intent in enumerate(
        intents,
        start=1,
    ):
        print()
        print(f"=== INTENT {index} ===")
        print(intent.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
