from __future__ import annotations

import logging
from datetime import (
    UTC,
    datetime,
    timedelta,
)

from .approval_bot import ApprovalBot
from .bybit import BybitDemoExecutor
from .domain import (
    ExecutionPlan,
    IncomingPost,
    TradingIntent,
)
from .execution import ExecutionPlanner
from .openrouter import (
    OpenRouterIntentExtractor,
)
from .storage import IntentStore

logger = logging.getLogger(__name__)


class SignalService:
    def __init__(
        self,
        *,
        store: IntentStore,
        extractor: OpenRouterIntentExtractor,
        planner: ExecutionPlanner,
        executor: BybitDemoExecutor,
        approval_bot: ApprovalBot,
        source_processing_lease_seconds: int = 300,
    ) -> None:
        if source_processing_lease_seconds <= 0:
            raise ValueError("Source processing lease must be positive")

        self._store = store
        self._extractor = extractor
        self._planner = planner
        self._executor = executor
        self._approval_bot = approval_bot
        self._source_processing_lease_seconds = source_processing_lease_seconds

    async def _sync_account_pnl(
        self,
    ):
        now = datetime.now(UTC)

        sync_state = await self._store.get_account_pnl_sync_state()

        if sync_state is None:
            history_start = now - timedelta(days=7)
            sync_start = history_start

        else:
            history_start = sync_state.history_start_at

            # Re-read one overlapping day because
            # a Bybit closed-PnL row may be updated
            # after the first fill/partial close.
            sync_start = min(
                sync_state.last_synced_at,
                now,
            ) - timedelta(days=1)

        records = await self._executor.closed_pnl_history(
            sync_start,
            now,
        )

        await self._store.upsert_closed_pnl(records)

        await self._store.mark_account_pnl_synced(
            history_start_at=history_start,
            last_synced_at=now,
        )

        return await self._store.get_account_pnl_summary()

    async def on_message(
        self,
        post: IncomingPost,
    ) -> None:
        source = post.source

        claim_token = await self._store.claim_source(
            source,
            lease_seconds=(self._source_processing_lease_seconds),
        )

        if claim_token is None:
            logger.debug(
                "Telegram message already completed or currently processing %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        try:
            (
                global_guidance,
                channel_guidance,
            ) = await self._store.get_guidance(source.channel_id)

            signals = await self._extractor.extract(
                post,
                global_guidance=global_guidance,
                channel_guidance=channel_guidance,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Signal extraction failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if not signals.actionable:
            await self._store.mark_source_completed(
                source,
                claim_token,
            )
            return

        planned: list[
            tuple[
                TradingIntent,
                ExecutionPlan,
            ]
        ] = []

        planning_errors: list[str] = []

        if signals.open_intents:
            try:
                policy = await self._store.get_execution_policy()

            except Exception as exc:
                planning_errors.append(f"Execution policy: {type(exc).__name__}: {exc}")

                logger.exception(
                    "Execution policy load failed for %s/%s",
                    source.channel_id,
                    source.message_id,
                )

            else:
                for intent in signals.open_intents:
                    try:
                        context = await self._executor.market_context(intent.symbol)

                        plan = self._planner.plan(
                            intent,
                            policy,
                            context,
                        )

                    except Exception as exc:
                        error = f"{intent.symbol}: {type(exc).__name__}: {exc}"

                        planning_errors.append(error)

                        logger.exception(
                            "Execution planning failed for candidate %s from %s/%s",
                            intent.symbol,
                            source.channel_id,
                            source.message_id,
                        )
                        continue

                    planned.append(
                        (
                            intent,
                            plan,
                        )
                    )

        position_actions = signals.position_actions

        if not planned and not position_actions:
            if planning_errors:
                await self._store.mark_source_failed(
                    source,
                    claim_token,
                    (
                        "No extracted OPEN candidate "
                        "could be planned: " + " | ".join(planning_errors)
                    ),
                )
            else:
                await self._store.mark_source_completed(
                    source,
                    claim_token,
                )

            return

        if planning_errors:
            logger.warning(
                "Signal batch kept %d OPEN "
                "candidate(s) and %d position "
                "action(s) for %s/%s; "
                "%d OPEN candidate(s) failed",
                len(planned),
                len(position_actions),
                source.channel_id,
                source.message_id,
                len(planning_errors),
            )

        account_state = None
        account_state_error = None
        account_pnl = None
        account_pnl_error = None

        try:
            account_pnl = await self._sync_account_pnl()

        except Exception as exc:
            account_pnl_error = f"{type(exc).__name__}: {exc}"

            logger.exception(
                "Account P&L sync failed for %s/%s",
                source.channel_id,
                source.message_id,
            )

        try:
            account_state = await self._executor.account_state()

        except Exception as exc:
            account_state_error = f"{type(exc).__name__}: {exc}"

            logger.exception(
                "Account-state check failed for %s/%s",
                source.channel_id,
                source.message_id,
            )

        try:
            finalized = await self._store.create_signal_batch_and_complete_source(
                planned,
                position_actions,
                claim_token,
            )

        except Exception as exc:
            await self._store.mark_source_failed(
                source,
                claim_token,
                f"{type(exc).__name__}: {exc}",
            )

            logger.exception(
                "Signal batch persistence failed for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        if not finalized:
            logger.warning(
                "Lost processing claim before signal batch persistence for %s/%s",
                source.channel_id,
                source.message_id,
            )
            return

        cards_sent = 0

        for intent, plan in planned:
            exposure = None

            if account_state is not None:
                exposure = account_state.exposure_for(intent.symbol)

            try:
                await self._approval_bot.send_intent(
                    intent,
                    plan,
                    exposure=exposure,
                    exposure_error=(account_state_error),
                    account_state=account_state,
                    account_state_error=(account_state_error),
                    account_pnl=account_pnl,
                    account_pnl_error=(account_pnl_error),
                    send_account_state=(cards_sent == 0),
                )

            except Exception:
                logger.exception(
                    "Approval delivery failed for persisted intent %s",
                    intent.intent_id,
                )
                continue

            cards_sent += 1

            logger.info(
                "Created OPEN trading intent %s with %d planned order(s)",
                intent.intent_id,
                len(plan.orders),
            )

        for action in position_actions:
            try:
                await self._approval_bot.send_position_action(
                    action,
                    account_state=account_state,
                    account_state_error=(account_state_error),
                    account_pnl=account_pnl,
                    account_pnl_error=(account_pnl_error),
                    send_account_state=(cards_sent == 0),
                )

            except Exception:
                logger.exception(
                    "Approval delivery failed for persisted position action %s",
                    action.action_id,
                )
                continue

            cards_sent += 1

            logger.info(
                "Created position action %s: %s %s",
                action.action_id,
                action.action.value,
                action.symbol,
            )
