from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from uuid import UUID

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from .bybit import BybitDemoExecutor
from .domain import EntryType, IntentStatus, TradingIntent
from .storage import IntentStore

logger = logging.getLogger(__name__)


class IntentAction(CallbackData, prefix="intent"):
    action: str
    intent_id: str


class ApprovalBot:
    def __init__(self, *, token: str, approval_chat_id: int, approver_user_id: int,
                 max_age_seconds: int, store: IntentStore, executor: BybitDemoExecutor) -> None:
        self._bot = Bot(token=token)
        self._dispatcher = Dispatcher()
        self._router = Router()
        self._dispatcher.include_router(self._router)
        self._approval_chat_id = approval_chat_id
        self._approver_user_id = approver_user_id
        self._max_age_seconds = max_age_seconds
        self._store = store
        self._executor = executor

        self._router.callback_query(IntentAction.filter(F.action == "execute"))(self._execute)
        self._router.callback_query(IntentAction.filter(F.action == "skip"))(self._skip)

    async def start(self) -> None:
        await self._bot.delete_webhook(drop_pending_updates=False)

    async def run(self) -> None:
        await self._dispatcher.start_polling(self._bot)

    async def close(self) -> None:
        await self._bot.session.close()

    async def send_intent(self, intent: TradingIntent) -> None:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Execute",
                callback_data=IntentAction(action="execute", intent_id=str(intent.intent_id)).pack(),
                style="success",
            ),
            InlineKeyboardButton(
                text="Skip",
                callback_data=IntentAction(action="skip", intent_id=str(intent.intent_id)).pack(),
                style="danger",
            ),
        ]])
        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=self._render(intent),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    async def _execute(self, callback: CallbackQuery, callback_data: IntentAction) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer("Not authorized", show_alert=True)
            return

        intent_id = UUID(callback_data.intent_id)
        intent = await self._store.get_intent(intent_id)
        if intent is None:
            await callback.answer("Intent no longer exists", show_alert=True)
            return
        if intent.status is not IntentStatus.PENDING:
            await callback.answer(f"Already {intent.status.value.lower()}", show_alert=True)
            return

        age = (datetime.now(timezone.utc) - intent.created_at).total_seconds()
        if age > self._max_age_seconds:
            await callback.answer(f"Intent is stale ({int(age)}s). Not executed.", show_alert=True)
            return

        if not await self._store.claim_for_execution(intent_id, callback.from_user.id):
            await callback.answer("Intent was already handled", show_alert=True)
            return

        await callback.answer("Executing on Bybit Demo…")

        try:
            order_id = await self._executor.execute(intent)
        except Exception as exc:
            logger.exception("Execution failed for %s", intent_id)
            await self._store.mark_failed(intent_id, str(exc))
            await self._edit_card(callback, intent, f"\n\n<b>FAILED</b>\n<code>{html.escape(str(exc))}</code>")
            return

        await self._store.mark_executed(intent_id, order_id)
        await self._edit_card(
            callback, intent,
            f"\n\n<b>EXECUTED ON BYBIT DEMO</b>\nOrder ID: <code>{html.escape(order_id)}</code>"
        )

    async def _skip(self, callback: CallbackQuery, callback_data: IntentAction) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer("Not authorized", show_alert=True)
            return

        intent_id = UUID(callback_data.intent_id)
        intent = await self._store.get_intent(intent_id)
        if intent is None:
            await callback.answer("Intent no longer exists", show_alert=True)
            return
        if not await self._store.mark_skipped(intent_id, callback.from_user.id):
            await callback.answer("Intent was already handled", show_alert=True)
            return

        await callback.answer("Skipped")
        await self._edit_card(callback, intent, "\n\n<b>SKIPPED</b>")

    async def _edit_card(self, callback: CallbackQuery, intent: TradingIntent, suffix: str) -> None:
        if callback.message is not None:
            await callback.message.edit_text(
                self._render(intent) + suffix,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )

    @staticmethod
    def _render(intent: TradingIntent) -> str:
        source_url = intent.source.telegram_url
        source_line = (
            f'<a href="{html.escape(source_url)}">Open source message</a>'
            if source_url else "Source link unavailable"
        )
        entry = "Market" if intent.entry.type is EntryType.MARKET else f"{intent.entry.price:g}"

        risk_block = ""
        if intent.entry.price is not None:
            e = intent.entry.price
            if intent.side.value == "LONG":
                risk = (e - intent.stop_loss) / e * 100
                reward = (intent.take_profit - e) / e * 100
            else:
                risk = (intent.stop_loss - e) / e * 100
                reward = (e - intent.take_profit) / e * 100
            rr = reward / risk if risk > 0 else 0
            risk_block = (
                f"\nRisk to SL: <b>{risk:.2f}%</b>"
                f"\nReward to TP: <b>{reward:.2f}%</b>"
                f"\nR:R: <b>{rr:.2f}</b>"
            )

        return (
            f"<b>{html.escape(intent.side.value)} {html.escape(intent.symbol)}</b>\n\n"
            f"Entry: <b>{html.escape(entry)}</b>\n"
            f"Stop loss: <b>{intent.stop_loss:g}</b>\n"
            f"Take profit: <b>{intent.take_profit:g}</b>"
            f"{risk_block}\n\n"
            f"Confidence: <b>{intent.confidence:.0%}</b>\n"
            f"Thesis: {html.escape(intent.summary)}\n\n"
            f"Trader: {html.escape(intent.source.channel_title)}\n"
            f"Published: {html.escape(intent.source.published_at.isoformat())}\n"
            f"{source_line}"
        )
