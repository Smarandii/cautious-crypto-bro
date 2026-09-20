from __future__ import annotations

import argparse
import asyncio
import logging

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
    ReadOnlyProviderCooldownStore,
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
        provider_cooldown_store=(ReadOnlyProviderCooldownStore(runtime_store)),
        provider_cooldown_seconds=(
            settings.openrouter_provider_cooldown_hours * 60 * 60
        ),
        evaluation_cache=(runtime_store),
        evaluation_cache_seconds=(settings.openrouter_evaluation_cache_hours * 60 * 60),
    )

    try:
        signals = await extractor.extract(
            post,
            global_guidance=global_guidance,
            channel_guidance=channel_guidance,
        )
    finally:
        await extractor.close()
        await runtime_store.close()

    print()
    print("=== OPEN TRADING INTENTS ===")

    print(f"Candidates: {len(signals.open_intents)}")

    for index, intent in enumerate(
        signals.open_intents,
        start=1,
    ):
        print()
        print(f"=== OPEN INTENT {index} ===")
        print(intent.model_dump_json(indent=2))

    print()
    print("=== POSITION ACTIONS ===")
    print(f"Candidates: {len(signals.position_actions)}")

    for index, action in enumerate(
        signals.position_actions,
        start=1,
    ):
        print()
        print(f"=== POSITION ACTION {index} ===")
        print(action.model_dump_json(indent=2))

    if not signals.actionable:
        print()
        print("NO ACTIONABLE SIGNAL")
        return 0

    if args.intent_only:
        return 0

    policy = await store.get_execution_policy() if signals.open_intents else None

    executor = BybitDemoExecutor(
        api_key=settings.bybit_api_key,
        api_secret=settings.bybit_api_secret,
    )

    bot = None

    if args.send_approval:
        bot = ApprovalBot(
            token=settings.telegram_bot_token,
            approval_chat_id=settings.telegram_approval_chat_id,
            approver_user_id=settings.telegram_approver_user_id,
            max_age_seconds=settings.intent_max_age_seconds,
            store=store,
            executor=executor,
        )

    planner = ExecutionPlanner()
    planning_failures = 0
    approvals_sent = 0

    try:
        for index, intent in enumerate(
            signals.open_intents,
            start=1,
        ):
            try:
                context = await executor.market_context(intent.symbol)

                assert policy is not None

                plan = planner.plan(
                    intent,
                    policy,
                    context,
                )

            except Exception as exc:
                planning_failures += 1

                print()
                print(f"=== EXECUTION PLAN {index} ({intent.symbol}) ===")
                print("PLANNING FAILED")
                print(f"{type(exc).__name__}: {exc}")
                continue

            print()
            print(f"=== EXECUTION PLAN {index} ({intent.symbol}) ===")
            print(plan.model_dump_json(indent=2))

            if not args.send_approval:
                continue

            await store.create_intent_with_plan(
                intent,
                plan,
            )

            assert bot is not None

            await bot.send_intent(
                intent,
                plan,
                send_account_state=(approvals_sent == 0),
            )

            approvals_sent += 1

        for action in signals.position_actions:
            print()
            print(f"=== POSITION ACTION {action.symbol} ===")
            print(action.model_dump_json(indent=2))

            if not args.send_approval:
                continue

            await store.create_position_action(action)

            assert bot is not None

            try:
                account_state = await executor.account_state()
                account_state_error = None
            except Exception as exc:
                account_state = None
                account_state_error = f"{type(exc).__name__}: {exc}"

            await bot.send_position_action(
                action,
                account_state=account_state,
                account_state_error=(account_state_error),
                send_account_state=(approvals_sent == 0),
            )

            approvals_sent += 1

        print()

        if args.send_approval:
            print(f"Approval cards sent: {approvals_sent}")
            print(
                "Pressing Execute uses the normal "
                "approval path and places Bybit Demo orders."
            )
        else:
            print("Replay complete. Nothing was persisted or executed.")
            print("Use --send-approval to create normal approval cards.")

        return 2 if planning_failures else 0

    finally:
        if bot is not None:
            await bot.close()

        executor.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
