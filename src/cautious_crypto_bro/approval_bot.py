from __future__ import annotations

import asyncio
import html
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from aiogram import (
    Bot,
    Dispatcher,
    F,
    Router,
)
from aiogram.filters.callback_data import (
    CallbackData,
)
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    TelegramObject,
)
from aiogram.utils.backoff import (
    BackoffConfig,
)

from .approval_presenter import (
    render,
    render_account_state,
    render_action_outcome,
    render_position_action,
)
from .cancellation import finish_before_cancellation
from .domain import (
    AccountPnlSummary,
    AccountSnapshotDelivery,
    AccountStateSummary,
    ApprovalMode,
    ExecutionPlan,
    IntentExecutionOutcome,
    IntentStatus,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    PositionActionType,
    SymbolExposure,
    TradingIntent,
)
from .ports import (
    ApprovalBotStore,
    ManualApprovalExecutor,
)

logger = logging.getLogger(__name__)

POLLING_BACKOFF = BackoffConfig(
    min_delay=5.0,
    max_delay=30.0,
    factor=1.5,
    jitter=0.0,
)


class IntentAction(
    CallbackData,
    prefix="intent",
):
    action: str
    intent_id: str


class PositionActionCallback(
    CallbackData,
    prefix="position",
):
    action: str
    action_id: str


class ApprovalBot:
    def __init__(
        self,
        *,
        token: str,
        approval_chat_id: int,
        approver_user_id: int,
        store: ApprovalBotStore,
        executor: ManualApprovalExecutor,
    ) -> None:
        self._bot = Bot(token=token)
        self._dispatcher = Dispatcher()
        self._router = Router()
        self._update_tasks: set[asyncio.Task[Any]] = set()
        self._dispatcher.update.outer_middleware(self._track_update)

        self._dispatcher.include_router(self._router)

        self._approval_chat_id = approval_chat_id
        self._approver_user_id = approver_user_id
        self._store = store
        self._executor = executor

        self._router.callback_query(IntentAction.filter(F.action == "execute"))(
            self._execute
        )

        self._router.callback_query(IntentAction.filter(F.action == "skip"))(self._skip)

        self._router.callback_query(
            PositionActionCallback.filter(F.action == "execute")
        )(self._execute_position_action)

        self._router.callback_query(PositionActionCallback.filter(F.action == "skip"))(
            self._skip_position_action
        )

    async def start(self) -> None:
        await self._bot.delete_webhook(drop_pending_updates=False)

    async def run(self) -> None:
        polling = asyncio.create_task(
            self._dispatcher.start_polling(
                self._bot,
                backoff_config=POLLING_BACKOFF,
                handle_signals=False,
                close_bot_session=False,
            )
        )
        try:
            await asyncio.shield(polling)
        finally:
            await finish_before_cancellation(self._finish_polling(polling))

    async def _track_update(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        task = asyncio.current_task()
        assert task is not None
        self._update_tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self._update_tasks.discard(task)

    async def _finish_polling(self, polling: asyncio.Task[Any]) -> None:
        # Let a newly scheduled poller enter its lifecycle before asking it to stop.
        await asyncio.sleep(0)
        if not polling.done():
            try:
                await self._dispatcher.stop_polling()
            except RuntimeError:
                polling.cancel()  # Startup never entered polling.
        results = list(await asyncio.gather(polling, return_exceptions=True))
        updates = tuple(self._update_tasks)
        for task in updates:
            task.cancel()
        results += await asyncio.gather(*updates, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.error(
                    "Telegram task failed during shutdown",
                    exc_info=(type(result), result, result.__traceback__),
                )

    async def close(self) -> None:
        await self._bot.session.close()

    async def run_strategy_alert_delivery(self) -> None:
        while True:
            try:
                await self.deliver_strategy_alerts()
            except Exception:
                logger.exception("Failed to read pending strategy alerts")
            await asyncio.sleep(30)

    async def deliver_strategy_alerts(self) -> None:
        for state in await self._store.get_pending_strategy_alerts():
            assert state.pause_reason is not None
            try:
                await self._send_html(
                    "⚠️ <b>STRATEGY AUTOMATION PAUSED</b>\n"
                    f"<b>{html.escape(state.symbol)}</b>\n"
                    f"<code>{state.strategy_id}</code>\n"
                    f"{html.escape(state.pause_reason)}\n\n"
                    "Orders and protection were left untouched. "
                    "Review the position and its exits on Bybit before resuming automation."
                )
                await self._store.mark_strategy_alert_delivered(
                    state.strategy_id, state.pause_reason
                )
            except Exception:
                logger.exception(
                    "Strategy pause alert delivery failed for %s", state.strategy_id
                )

    async def _authorized(self, callback: CallbackQuery) -> bool:
        if callback.from_user.id == self._approver_user_id:
            return True

        await callback.answer("Not authorized", show_alert=True)
        return False

    async def _send_html(
        self,
        text: str,
        *,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=reply_markup,
        )

    async def _edit_message(
        self,
        callback: CallbackQuery,
        text: str,
    ) -> None:
        message = callback.message

        if isinstance(message, Message):
            await message.edit_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )

    async def send_account_snapshot(
        self,
        state: AccountStateSummary | None,
        *,
        state_error: str | None,
        pnl: AccountPnlSummary | None,
        pnl_error: str | None,
    ) -> None:
        try:
            await self._send_html(
                render_account_state(
                    state,
                    error=state_error,
                    pnl=pnl,
                    pnl_error=pnl_error,
                )
            )
        except Exception:
            # Informational only; the actionable card must still be delivered.
            logger.exception("Failed to send account snapshot")

    async def send_intent(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
        *,
        exposure: SymbolExposure | None = None,
        exposure_error: str | None = None,
        snapshot: AccountSnapshotDelivery | None = None,
    ) -> None:
        if snapshot is not None:
            await self.send_account_snapshot(
                snapshot.state,
                state_error=snapshot.state_error,
                pnl=snapshot.pnl,
                pnl_error=snapshot.pnl_error,
            )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Execute",
                        callback_data=(
                            IntentAction(
                                action="execute",
                                intent_id=str(intent.intent_id),
                            ).pack()
                        ),
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="Skip",
                        callback_data=(
                            IntentAction(
                                action="skip",
                                intent_id=str(intent.intent_id),
                            ).pack()
                        ),
                        style="danger",
                    ),
                ]
            ]
        )

        await self._send_html(
            render(
                intent,
                plan,
                exposure=exposure,
                exposure_error=exposure_error,
            ),
            reply_markup=keyboard,
        )

    async def send_position_action(
        self,
        action: PositionActionIntent,
        *,
        snapshot: AccountSnapshotDelivery | None = None,
    ) -> None:
        if snapshot is not None:
            await self.send_account_snapshot(
                snapshot.state,
                state_error=snapshot.state_error,
                pnl=snapshot.pnl,
                pnl_error=snapshot.pnl_error,
            )

        execute_label = (
            "Cancel source entries"
            if action.action is PositionActionType.CANCEL_ENTRIES
            else "Execute close"
            if action.action is PositionActionType.CLOSE
            else "Execute reduction"
        )

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=execute_label,
                        callback_data=(
                            PositionActionCallback(
                                action="execute",
                                action_id=str(action.action_id),
                            ).pack()
                        ),
                        style="danger",
                    ),
                    InlineKeyboardButton(
                        text="Skip",
                        callback_data=(
                            PositionActionCallback(
                                action="skip",
                                action_id=str(action.action_id),
                            ).pack()
                        ),
                    ),
                ]
            ]
        )

        await self._send_html(
            render_position_action(
                action,
                account_state=snapshot.state if snapshot is not None else None,
                account_state_error=(
                    snapshot.state_error if snapshot is not None else None
                ),
            ),
            reply_markup=keyboard,
        )

    async def _execute(
        self,
        callback: CallbackQuery,
        callback_data: IntentAction,
    ) -> None:
        if not await self._authorized(callback):
            return

        intent_id = UUID(callback_data.intent_id)

        await callback.answer("Executing on Bybit Demo…")

        outcome = await self._executor.execute_intent(
            intent_id,
            approval_mode=(ApprovalMode.MANUAL),
            user_id=(callback.from_user.id),
        )

        if outcome.intent is None or outcome.plan is None:
            await callback.answer(
                outcome.message,
                show_alert=True,
            )
            return

        if outcome.status is IntentStatus.EXECUTED:
            ids_text = "\n".join(
                (f"<code>{html.escape(order_id)}</code>")
                for order_id in outcome.order_ids
            )

            suffix = f"\n\n<b>EXECUTED ON BYBIT DEMO</b>\n{ids_text}"
        else:
            suffix = (
                "\n\n"
                f"<b>{html.escape(outcome.status.value)}</b>"
                "\n<code>"
                f"{html.escape(outcome.message)}"
                "</code>"
            )

        await self._edit_message(
            callback,
            render(outcome.intent, outcome.plan) + suffix,
        )

    async def _execute_position_action(
        self,
        callback: CallbackQuery,
        callback_data: PositionActionCallback,
    ) -> None:
        if not await self._authorized(callback):
            return

        action_id = UUID(callback_data.action_id)

        await callback.answer("Executing on Bybit Demo…")

        outcome = await self._executor.execute_position_action(
            action_id,
            approval_mode=(ApprovalMode.MANUAL),
            user_id=(callback.from_user.id),
        )

        if outcome.action is None:
            await callback.answer(
                outcome.message,
                show_alert=True,
            )
            return

        suffix = render_action_outcome(outcome)

        await self._edit_message(
            callback,
            render_position_action(outcome.action) + suffix,
        )

    async def _skip_position_action(
        self,
        callback: CallbackQuery,
        callback_data: PositionActionCallback,
    ) -> None:
        if not await self._authorized(callback):
            return

        action_id = UUID(callback_data.action_id)

        action = await self._store.get_position_action(action_id)

        if action is None:
            await callback.answer(
                "Position action no longer exists",
                show_alert=True,
            )
            return

        skipped = await self._store.mark_position_action_skipped(
            action_id,
            callback.from_user.id,
        )

        if not skipped:
            await callback.answer(
                "Position action was already handled",
                show_alert=True,
            )
            return

        await callback.answer("Skipped")

        await self._edit_message(
            callback,
            render_position_action(action) + "\n\n<b>SKIPPED</b>",
        )

    async def _skip(
        self,
        callback: CallbackQuery,
        callback_data: IntentAction,
    ) -> None:
        if not await self._authorized(callback):
            return

        intent_id = UUID(callback_data.intent_id)

        intent = await self._store.get_intent(intent_id)

        if intent is None:
            await callback.answer(
                "Intent no longer exists",
                show_alert=True,
            )
            return

        plan = await self._store.get_execution_plan(intent_id)

        if plan is None:
            await callback.answer(
                "Execution plan no longer exists",
                show_alert=True,
            )
            return

        if not await self._store.mark_skipped(
            intent_id,
            callback.from_user.id,
        ):
            await callback.answer(
                "Intent was already handled",
                show_alert=True,
            )
            return

        await callback.answer("Skipped")

        await self._edit_message(
            callback,
            render(intent, plan) + "\n\n<b>SKIPPED</b>",
        )

    async def send_auto_intent_outcome(
        self,
        outcome: IntentExecutionOutcome,
    ) -> None:
        if outcome.intent is None or outcome.plan is None:
            await self._bot.send_message(
                chat_id=self._approval_chat_id,
                text=(
                    "<b>AUTO EXECUTION ERROR</b>\n"
                    f"<code>"
                    f"{html.escape(outcome.message)}"
                    f"</code>"
                ),
                parse_mode="HTML",
            )
            return

        if outcome.status is IntentStatus.EXECUTED:
            ids_text = "\n".join(
                (f"<code>{html.escape(order_id)}</code>")
                for order_id in outcome.order_ids
            )

            suffix = f"\n\n<b>AUTO-EXECUTED ON BYBIT DEMO</b>\n{ids_text}"
        else:
            suffix = (
                "\n\n"
                "<b>AUTO EXECUTION "
                f"{html.escape(outcome.status.value)}"
                "</b>\n<code>"
                f"{html.escape(outcome.message)}"
                "</code>"
            )

        await self._send_html(
            render(
                outcome.intent,
                outcome.plan,
            )
            + suffix
        )

    async def send_auto_action_outcome(
        self,
        outcome: PositionActionExecutionOutcome,
    ) -> None:
        if outcome.action is None:
            await self._bot.send_message(
                chat_id=self._approval_chat_id,
                text=(
                    "<b>AUTO EXECUTION ERROR</b>\n"
                    f"<code>"
                    f"{html.escape(outcome.message)}"
                    f"</code>"
                ),
                parse_mode="HTML",
            )
            return

        await self._send_html(
            render_position_action(outcome.action)
            + render_action_outcome(
                outcome,
                auto=True,
            )
        )

    async def send_recovery_warning(
        self,
        *,
        uncertain_intents: int,
        uncertain_actions: int,
    ) -> None:
        if uncertain_intents == 0 and uncertain_actions == 0:
            return

        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=(
                "⚠️ <b>INTERRUPTED EXECUTION "
                "RECOVERY WARNING</b>\n\n"
                "Previous process stopped while "
                "execution was in progress. "
                "These records were quarantined "
                "instead of retried automatically."
                "\nOPEN intents: "
                f"<b>{uncertain_intents}</b>"
                "\nPosition actions: "
                f"<b>{uncertain_actions}</b>"
            ),
            parse_mode="HTML",
        )
