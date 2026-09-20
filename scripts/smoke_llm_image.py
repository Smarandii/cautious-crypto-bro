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
from cautious_crypto_bro.llm_factory import (
    build_intent_extractor,
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

    extractor = build_intent_extractor(
        settings,
    )

    try:
        signals = await extractor.extract(
            post,
            global_guidance=global_guidance,
            channel_guidance=channel_guidance,
        )
    finally:
        await extractor.close()

    if not signals.actionable:
        print("NO ACTIONABLE SIGNAL")
        return

    print(f"OPEN INTENTS: {len(signals.open_intents)}")

    for index, intent in enumerate(
        signals.open_intents,
        start=1,
    ):
        print()
        print(f"=== OPEN INTENT {index} ===")
        print(intent.model_dump_json(indent=2))

    print(f"POSITION ACTIONS: {len(signals.position_actions)}")

    for index, action in enumerate(
        signals.position_actions,
        start=1,
    ):
        print()
        print(f"=== POSITION ACTION {index} ===")
        print(action.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())
