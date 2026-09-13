from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from cautious_crypto_bro.approval_bot import (
    ApprovalBot,
)
from cautious_crypto_bro.bybit import (
    BybitDemoExecutor,
)
from cautious_crypto_bro.config import (
    get_settings,
)
from cautious_crypto_bro.execution import (
    ExecutionPlanner,
)
from cautious_crypto_bro.openrouter import (
    OpenRouterIntentExtractor,
)
from cautious_crypto_bro.runtime_store import (
    RedisRuntimeStore,
)
from cautious_crypto_bro.storage import (
    IntentStore,
)
from cautious_crypto_bro.telegram_replay import (
    fetch_telegram_post,
)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=("%(asctime)s %(levelname)s %(name)s: %(message)s"),
    )

    parser = argparse.ArgumentParser(
        description=(
            "Replay one historical Telegram "
            "post through the production "
            "signal pipeline."
        )
    )

    parser.add_argument(
        "url",
        help=("Telegram post URL, for example https://t.me/c/2243423111/7905"),
    )

    parser.add_argument(
        "--debug-dir",
        type=Path,
        help=("Save raw OpenRouter responses for diagnostic inspection."),
    )

    parser.add_argument(
        "--intent-only",
        action="store_true",
        help=("Stop after OpenRouter extraction; do not build an execution plan."),
    )

    parser.add_argument(
        "--send-approval",
        action="store_true",
        help=(
            "Persist the replayed intent/plan "
            "and send the normal Telegram "
            "approval card."
        ),
    )

    args = parser.parse_args()

    if args.intent_only and args.send_approval:
        parser.error("--send-approval requires an execution plan")

    settings = get_settings()

    store = IntentStore(settings.database_path)

    await store.initialize()

    print("Fetching Telegram post...")

    post = await fetch_telegram_post(
        url=args.url,
        api_id=(settings.telegram_api_id),
        api_hash=(settings.telegram_api_hash),
        session_name=(settings.telegram_session_name),
    )

    source = post.source

    print()
    print("=== SOURCE POST ===")
    print(f"Channel: {source.channel_title}")
    print(f"Channel ID: {source.channel_id}")
    print(f"Message ID: {source.message_id}")
    print(f"Published: {source.published_at.isoformat()}")
    print(f"Images: {len(post.images)}")
    print("Caption/text:")
    print(source.text or "(none)")

    (
        global_guidance,
        channel_guidance,
    ) = await store.get_guidance(source.channel_id)

    runtime_store = RedisRuntimeStore(
        settings.redis_url,
        max_connections=(settings.redis_max_connections),
        pool_timeout_seconds=(settings.redis_pool_timeout_seconds),
    )
    await runtime_store.initialize()

    print()
    print(
        "Guidance: "
        f"global="
        f"{'yes' if global_guidance else 'no'}, "
        f"channel="
        f"{'yes' if channel_guidance else 'no'}"
    )

    extractor = OpenRouterIntentExtractor(
        api_key=(settings.openrouter_api_key),
        model=(settings.openrouter_model),
        base_url=(settings.openrouter_base_url),
        inference_timeout_seconds=(settings.openrouter_inference_timeout_seconds),
        max_attempts=(settings.openrouter_inference_max_attempts),
        provider_cooldown_store=(runtime_store),
        provider_cooldown_seconds=(
            settings.openrouter_provider_cooldown_hours * 60 * 60
        ),
        evaluation_cache=(runtime_store),
        evaluation_cache_seconds=(settings.openrouter_evaluation_cache_hours * 60 * 60),
    )

    try:
        intent = await extractor.extract(
            post,
            global_guidance=(global_guidance),
            channel_guidance=(channel_guidance),
            debug_dir=args.debug_dir,
        )
    finally:
        await extractor.close()
        await runtime_store.close()

    print()
    print("=== TRADING INTENT ===")

    if intent is None:
        print("NO ACTIONABLE INTENT")
        return 0

    print(intent.model_dump_json(indent=2))

    if args.intent_only:
        return 0

    policy = await store.get_execution_policy()

    executor = BybitDemoExecutor(
        api_key=(settings.bybit_api_key),
        api_secret=(settings.bybit_api_secret),
    )

    try:
        context = await executor.market_context(intent.symbol)

        try:
            plan = ExecutionPlanner().plan(
                intent,
                policy,
                context,
            )
        except Exception as exc:
            print()
            print("=== EXECUTION PLAN ===")
            print("PLANNING FAILED")
            print(f"{type(exc).__name__}: {exc}")

            print()
            print(
                "The TradingIntent above is "
                "still the extractor result. "
                "Historical MARKET signals may "
                "fail planning because planning "
                "uses current Bybit market data."
            )

            return 2

        print()
        print("=== EXECUTION PLAN ===")
        print(plan.model_dump_json(indent=2))

        if not args.send_approval:
            print()
            print("Replay complete. Nothing was persisted or executed.")
            print("Use --send-approval to create a normal approval card.")
            return 0

        await store.create_intent_with_plan(
            intent,
            plan,
        )

        bot = ApprovalBot(
            token=(settings.telegram_bot_token),
            approval_chat_id=(settings.telegram_approval_chat_id),
            approver_user_id=(settings.telegram_approver_user_id),
            max_age_seconds=(settings.intent_max_age_seconds),
            store=store,
            executor=executor,
        )

        try:
            await bot.send_intent(
                intent,
                plan,
            )
        finally:
            await bot.close()

        print()
        print("Approval card sent.")
        print(
            "Pressing Execute uses the normal "
            "live approval path and places "
            "Bybit Demo orders."
        )

        return 0

    finally:
        executor.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
