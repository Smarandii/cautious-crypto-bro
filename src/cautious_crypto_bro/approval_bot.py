from __future__ import annotations

import html
import logging
from datetime import (
    UTC,
    datetime,
)
from decimal import Decimal
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
)
from aiogram.utils.backoff import (
    BackoffConfig,
)

from .bybit import (
    AccountOrder,
    AccountStateSummary,
    BybitDemoExecutor,
    SymbolExposure,
)
from .domain import (
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    IntentStatus,
    PositionActionIntent,
    PositionActionType,
    Side,
    TakeProfitSource,
    TradingIntent,
)
from .storage import IntentStore

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
        max_age_seconds: int,
        store: IntentStore,
        executor: BybitDemoExecutor,
    ) -> None:
        self._bot = Bot(token=token)
        self._dispatcher = Dispatcher()
        self._router = Router()

        self._dispatcher.include_router(self._router)

        self._approval_chat_id = approval_chat_id
        self._approver_user_id = approver_user_id
        self._max_age_seconds = max_age_seconds
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
        await self._dispatcher.start_polling(
            self._bot,
            backoff_config=(POLLING_BACKOFF),
        )

    async def close(self) -> None:
        await self._bot.session.close()

    async def send_intent(
        self,
        intent: TradingIntent,
        plan: ExecutionPlan,
        *,
        exposure: SymbolExposure | None = None,
        exposure_error: str | None = None,
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
        send_account_state: bool = True,
    ) -> None:
        if send_account_state:
            try:
                await self._bot.send_message(
                    chat_id=self._approval_chat_id,
                    text=self._render_account_state(
                        account_state,
                        error=account_state_error,
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                # The snapshot is informational and
                # must never prevent delivery of the
                # actionable approval card.
                logger.exception("Failed to send account snapshot")

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

        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=self._render(
                intent,
                plan,
                exposure=exposure,
                exposure_error=exposure_error,
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    async def send_position_action(
        self,
        action: PositionActionIntent,
        *,
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
        send_account_state: bool = True,
    ) -> None:
        if send_account_state:
            try:
                await self._bot.send_message(
                    chat_id=self._approval_chat_id,
                    text=self._render_account_state(
                        account_state,
                        error=account_state_error,
                    ),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Failed to send account snapshot")

        execute_label = (
            "Execute close"
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

        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=self._render_position_action(
                action,
                account_state=account_state,
                account_state_error=(account_state_error),
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=keyboard,
        )

    async def _execute(
        self,
        callback: CallbackQuery,
        callback_data: IntentAction,
    ) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
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

        if intent.status is not IntentStatus.PENDING:
            await callback.answer(
                f"Already {intent.status.value.lower()}",
                show_alert=True,
            )
            return

        age = (datetime.now(UTC) - intent.created_at).total_seconds()

        if age > self._max_age_seconds:
            await callback.answer(
                f"Intent is stale ({int(age)}s). Not executed.",
                show_alert=True,
            )
            return

        if not await self._store.claim_for_execution(
            intent_id,
            callback.from_user.id,
        ):
            await callback.answer(
                "Intent was already handled",
                show_alert=True,
            )
            return

        await callback.answer("Executing on Bybit Demo…")

        try:
            order_ids = await self._executor.execute(plan)

        except Exception as exc:
            logger.exception(
                "Execution failed for %s",
                intent_id,
            )

            await self._store.mark_failed(
                intent_id,
                str(exc),
            )

            await self._edit_card(
                callback,
                intent,
                plan,
                (f"\n\n<b>FAILED</b>\n<code>{html.escape(str(exc))}</code>"),
            )

            return

        await self._store.mark_executed(
            intent_id,
            order_ids,
        )

        ids_text = "\n".join(
            (f"<code>{html.escape(order_id)}</code>") for order_id in order_ids
        )

        await self._edit_card(
            callback,
            intent,
            plan,
            (f"\n\n<b>EXECUTED ON BYBIT DEMO</b>\n{ids_text}"),
        )

    async def _execute_position_action(
        self,
        callback: CallbackQuery,
        callback_data: PositionActionCallback,
    ) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
            return

        action_id = UUID(callback_data.action_id)

        action = await self._store.get_position_action(action_id)

        if action is None:
            await callback.answer(
                "Position action no longer exists",
                show_alert=True,
            )
            return

        if action.status is not IntentStatus.PENDING:
            await callback.answer(
                f"Already {action.status.value.lower()}",
                show_alert=True,
            )
            return

        age = (datetime.now(UTC) - action.created_at).total_seconds()

        if age > self._max_age_seconds:
            await callback.answer(
                f"Position action is stale ({int(age)}s). Not executed.",
                show_alert=True,
            )
            return

        claimed = await self._store.claim_position_action_for_execution(
            action_id,
            callback.from_user.id,
        )

        if not claimed:
            await callback.answer(
                "Position action was already handled",
                show_alert=True,
            )
            return

        await callback.answer("Executing on Bybit Demo…")

        try:
            result = await self._executor.execute_position_action(action)

        except Exception as exc:
            logger.exception(
                "Position action execution failed for %s",
                action_id,
            )

            await self._store.mark_position_action_failed(
                action_id,
                str(exc),
            )

            await self._edit_position_action_card(
                callback,
                action,
                (f"\n\n<b>FAILED</b>\n<code>{html.escape(str(exc))}</code>"),
            )
            return

        await self._store.mark_position_action_executed(
            action_id,
            result.order_id,
        )

        suffix = (
            "\n\n"
            "<b>EXECUTED ON BYBIT DEMO</b>\n"
            "Order: "
            f"<code>{html.escape(result.order_id)}</code>\n"
            "Position before: "
            f"<b>{result.position_side.value} "
            f"{self._fmt_decimal(result.position_size_before)}</b>"
        )

        if result.submitted_quantity is None:
            suffix += "\nSubmitted: <b>full reduce-only close</b>"
        else:
            suffix += (
                "\nSubmitted reduction: "
                f"<b>"
                f"{self._fmt_decimal(result.submitted_quantity)}"
                f"</b>"
            )

        if result.cancelled_entry_orders:
            suffix += (
                f"\nCancelled CCB entry orders: <b>{result.cancelled_entry_orders}</b>"
            )

        await self._edit_position_action_card(
            callback,
            action,
            suffix,
        )

    async def _skip_position_action(
        self,
        callback: CallbackQuery,
        callback_data: PositionActionCallback,
    ) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
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

        await self._edit_position_action_card(
            callback,
            action,
            "\n\n<b>SKIPPED</b>",
        )

    async def _skip(
        self,
        callback: CallbackQuery,
        callback_data: IntentAction,
    ) -> None:
        if callback.from_user.id != self._approver_user_id:
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
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

        await self._edit_card(
            callback,
            intent,
            plan,
            "\n\n<b>SKIPPED</b>",
        )

    async def _edit_card(
        self,
        callback: CallbackQuery,
        intent: TradingIntent,
        plan: ExecutionPlan,
        suffix: str,
    ) -> None:
        message = callback.message

        if isinstance(message, Message):
            await message.edit_text(
                self._render(
                    intent,
                    plan,
                )
                + suffix,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )

    async def _edit_position_action_card(
        self,
        callback: CallbackQuery,
        action: PositionActionIntent,
        suffix: str,
    ) -> None:
        message = callback.message

        if isinstance(message, Message):
            await message.edit_text(
                self._render_position_action(
                    action,
                )
                + suffix,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )

    @staticmethod
    def _render_position_action(
        action: PositionActionIntent,
        *,
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
    ) -> str:
        source_url = action.source.telegram_url

        source_line = (
            (f'<a href="{html.escape(source_url)}">Open source message</a>')
            if source_url
            else "Source link unavailable"
        )

        if action.action is PositionActionType.CLOSE:
            instruction = "Close current position completely"
        else:
            assert action.close_pct is not None

            instruction = f"Reduce current position by {action.close_pct:g}%"

        position_lines: list[str] = []
        pending_entries = 0

        if account_state is None:
            if account_state_error:
                position_lines.extend(
                    [
                        "⚠️ Current account state unavailable.",
                        (f"<code>{html.escape(account_state_error)}</code>"),
                    ]
                )
            else:
                position_lines.append(
                    "Position size and side are "
                    "resolved from live Bybit state "
                    "when Execute is pressed."
                )

        else:
            positions = [
                position
                for position in account_state.positions
                if position.symbol == action.symbol
            ]

            exposure = account_state.exposure_for(action.symbol)

            pending_entries = len(exposure.pending_entry_orders)

            if not positions:
                position_lines.append("⚠️ No current position found.")

            elif len(positions) > 1:
                position_lines.append(
                    "⚠️ Multiple positions found; execution will fail safe."
                )

            else:
                position = positions[0]

                position_lines.extend(
                    [
                        (
                            "Current position: "
                            f"<b>"
                            f"{html.escape(position.side.value)} "
                            f"{ApprovalBot._fmt_decimal(position.size)}"
                            f"</b>"
                        ),
                        (
                            "Entry: "
                            f"<b>"
                            f"{ApprovalBot._fmt_decimal(position.avg_price)}"
                            f"</b>"
                            " → Mark: "
                            f"<b>"
                            f"{ApprovalBot._fmt_decimal(position.mark_price)}"
                            f"</b>"
                        ),
                        (
                            "Unrealized P&amp;L: "
                            f"<b>"
                            f"{ApprovalBot._fmt_signed(position.unrealised_pnl)} "
                            "USDT</b>"
                        ),
                    ]
                )

        pending_line = f"Pending CCB entry orders to cancel: <b>{pending_entries}</b>"

        expected = (
            action.expected_side.value
            if action.expected_side is not None
            else "not specified"
        )

        return (
            "⚠️ <b>POSITION ACTION — ACCOUNT WIDE</b>\n\n"
            f"<b>{html.escape(action.action.value)} "
            f"{html.escape(action.symbol)}</b>\n"
            f"{html.escape(instruction)}\n\n"
            + "\n".join(position_lines)
            + "\n"
            + pending_line
            + "\n"
            + "Expected side from signal: "
            f"<b>{html.escape(expected)}</b>\n\n" + "This targets the current Bybit "
            "position for this symbol regardless "
            "of which channel or signal opened it.\n\n" + "Confidence: "
            f"<b>{action.confidence:.0%}</b>\n" + "Reason: "
            f"{html.escape(action.summary)}\n\n" + "Trader: "
            f"{html.escape(action.source.channel_title)}\n" + "Published: "
            f"{html.escape(action.source.published_at.isoformat())}\n" + source_line
        )

    @staticmethod
    def _render(
        intent: TradingIntent,
        plan: ExecutionPlan,
        *,
        exposure: SymbolExposure | None = None,
        exposure_error: str | None = None,
    ) -> str:
        source_url = intent.source.telegram_url

        source_line = (
            (f'<a href="{html.escape(source_url)}">Open source message</a>')
            if source_url
            else "Source link unavailable"
        )

        if intent.entry.type is EntryType.MARKET:
            signal_entry = "Market"

        elif intent.entry.type is EntryType.LIMIT:
            assert intent.entry.price is not None

            signal_entry = f"{intent.entry.price:g}"

        else:
            assert intent.entry.range_low is not None
            assert intent.entry.range_high is not None

            signal_entry = f"{intent.entry.range_low:g} – {intent.entry.range_high:g}"

        if plan.take_profit_targets:
            source_label = (
                "Policy fallback"
                if (plan.take_profit_source is TakeProfitSource.POLICY)
                else "Trader"
            )

            tp_lines = []

            for target in plan.take_profit_targets:
                tp_lines.append(
                    f"{html.escape(target.name.title())}: "
                    f"<b>"
                    f"{ApprovalBot._fmt_decimal(target.price)}"
                    f"</b> — "
                    f"{ApprovalBot._fmt_decimal(target.close_pct)}% "
                    f"(~"
                    f"{target.r_multiple:.2f}R"
                    f")"
                )

            tp_block = f"<b>Take-profit ladder ({source_label})</b>\n" + "\n".join(
                tp_lines
            )
        else:
            tp_block = (
                f"Take profit: <b>{ApprovalBot._fmt_decimal(plan.take_profit)}</b>"
            )

        order_lines = []

        for index, order in enumerate(
            plan.orders,
            start=1,
        ):
            qty = ApprovalBot._fmt_decimal(order.quantity)

            if order.order_type is ExecutionOrderType.MARKET:
                order_lines.append(f"{index}. Market × {qty}")
            else:
                assert order.price is not None

                price = ApprovalBot._fmt_decimal(order.price)

                order_lines.append(f"{index}. {price} × {qty}")

        total_qty = sum(
            (order.quantity for order in plan.orders),
            Decimal("0"),
        )

        weighted_entry = (
            sum(
                (order.reference_price * order.quantity for order in plan.orders),
                Decimal("0"),
            )
            / total_qty
        )

        if intent.side is Side.LONG:
            risk = (weighted_entry - plan.stop_loss) / weighted_entry * Decimal("100")
        else:
            risk = (plan.stop_loss - weighted_entry) / weighted_entry * Decimal("100")

        if plan.take_profit_targets:
            reward = Decimal("0")

            for target in plan.take_profit_targets:
                if intent.side is Side.LONG:
                    target_reward = (
                        (target.price - weighted_entry)
                        / weighted_entry
                        * Decimal("100")
                    )
                else:
                    target_reward = (
                        (weighted_entry - target.price)
                        / weighted_entry
                        * Decimal("100")
                    )

                reward += target_reward * target.close_pct / Decimal("100")

            reward_label = "Blended reward if all TPs hit"
            rr_label = "Blended R:R"

        else:
            if intent.side is Side.LONG:
                reward = (
                    (plan.take_profit - weighted_entry)
                    / weighted_entry
                    * Decimal("100")
                )
            else:
                reward = (
                    (weighted_entry - plan.take_profit)
                    / weighted_entry
                    * Decimal("100")
                )

            reward_label = "Reward to TP"
            rr_label = "R:R"

        rr = reward / risk if risk > 0 else Decimal("0")

        orders_text = "\n".join(order_lines)

        capital = ApprovalBot._fmt_decimal(plan.policy.trading_capital_usdt)

        risk_pct = ApprovalBot._fmt_decimal(plan.policy.risk_per_trade_pct)

        risk_budget = ApprovalBot._fmt_decimal(plan.policy.risk_budget_usdt)

        planned_loss = ApprovalBot._fmt_decimal(plan.planned_max_loss_usdt)

        exposure_block = ApprovalBot._render_exposure(
            intent,
            exposure=exposure,
            exposure_error=(exposure_error),
        )

        return (
            f"<b>"
            f"{html.escape(intent.side.value)} "
            f"{html.escape(intent.symbol)}"
            f"</b>\n\n"
            f"{exposure_block}"
            f"Signal entry: "
            f"<b>{html.escape(signal_entry)}</b>\n"
            f"Stop loss: "
            f"<b>"
            f"{ApprovalBot._fmt_decimal(plan.stop_loss)}"
            f"</b>\n"
            f"{tp_block}\n\n"
            f"<b>Execution plan — "
            f"{len(plan.orders)} order(s)</b>\n"
            f"{orders_text}\n\n"
            f"Capital: "
            f"<b>{capital} USDT</b>\n"
            f"Risk policy: "
            f"<b>{risk_pct}% = "
            f"{risk_budget} USDT</b>\n"
            f"Planned price loss at SL: "
            f"<b>≤ {planned_loss} USDT</b>\n"
            f"Risk to SL: "
            f"<b>{float(risk):.2f}%</b>\n"
            f"{reward_label}: "
            f"<b>{float(reward):.2f}%</b>\n"
            f"{rr_label}: "
            f"<b>{float(rr):.2f}</b>\n\n"
            f"Confidence: "
            f"<b>{intent.confidence:.0%}</b>\n"
            f"Thesis: "
            f"{html.escape(intent.summary)}\n\n"
            f"Trader: "
            f"{html.escape(intent.source.channel_title)}\n"
            f"Published: "
            f"{html.escape(intent.source.published_at.isoformat())}\n"
            f"{source_line}"
        )

    @staticmethod
    def _render_account_state(
        state: AccountStateSummary | None,
        *,
        error: str | None = None,
    ) -> str:
        if state is None:
            return (
                "📊 <b>BYBIT DEMO — "
                "ACCOUNT SNAPSHOT</b>\n\n"
                "⚠️ Account state unavailable.\n"
                "The approval card follows normally."
            )

        realized = ApprovalBot._fmt_signed(state.realized_pnl_today)

        unrealized = ApprovalBot._fmt_signed(state.unrealised_pnl)

        lines = [
            "📊 <b>BYBIT DEMO — ACCOUNT SNAPSHOT</b>",
            "",
            "<b>Today (UTC)</b>",
            (f"Realized P&amp;L: <b>{realized} USDT</b>"),
            (f"Unrealized P&amp;L: <b>{unrealized} USDT</b>"),
            "",
            (f"<b>Open positions: {len(state.positions)}</b>"),
        ]

        if state.positions:
            for position in state.positions[:8]:
                position_line = (
                    "• "
                    f"{html.escape(position.symbol)} "
                    f"{html.escape(position.side.value)} "
                    f"{ApprovalBot._fmt_decimal(position.size)}"
                    "\n  Entry "
                    f"{ApprovalBot._fmt_decimal(position.avg_price)}"
                    " → Mark "
                    f"{ApprovalBot._fmt_decimal(position.mark_price)}"
                    " · uPnL "
                    f"{ApprovalBot._fmt_signed(position.unrealised_pnl)}"
                    " USDT"
                    " · "
                    f"{html.escape(position.status)}"
                )

                protection = []

                if position.stop_loss is not None:
                    protection.append(
                        "SL " + ApprovalBot._fmt_decimal(position.stop_loss)
                    )

                if position.take_profit is not None:
                    protection.append(
                        "TP " + ApprovalBot._fmt_decimal(position.take_profit)
                    )

                if protection:
                    position_line += "\n  " + " · ".join(protection)

                lines.append(position_line)

            if len(state.positions) > 8:
                lines.append(f"• … +{len(state.positions) - 8} more")
        else:
            lines.append("• None")

        lines.extend(
            [
                "",
                (f"<b>Open orders: {len(state.open_orders)}</b>"),
            ]
        )

        if state.open_orders:
            entry_count = sum(
                1
                for order in state.open_orders
                if order.kind
                in {
                    "ENTRY",
                    "CONDITIONAL",
                }
            )

            protective_count = sum(
                1 for order in state.open_orders if order.is_protective
            )

            reduce_count = sum(
                1 for order in state.open_orders if order.kind == "REDUCE"
            )

            lines.append(
                "Entry: "
                f"{entry_count}"
                " · Protective: "
                f"{protective_count}"
                " · Reduce/close: "
                f"{reduce_count}"
            )

            for order in state.open_orders[:6]:
                lines.append(ApprovalBot._render_account_order(order))

            if len(state.open_orders) > 6:
                lines.append(f"• … +{len(state.open_orders) - 6} more")
        else:
            lines.append("• None")

        terminal = state.terminal_orders_24h

        filled_count = sum(1 for order in terminal if order.status == "Filled")

        cancelled_count = sum(1 for order in terminal if "Cancel" in order.status)

        other_count = len(terminal) - filled_count - cancelled_count

        deactivated_count = sum(
            1 for order in terminal if order.status == "Deactivated"
        )

        other_count = len(terminal) - filled_count - cancelled_count - deactivated_count

        breakdown = (
            f"Filled {filled_count}"
            " · "
            f"Cancelled {cancelled_count}"
            " · "
            f"Deactivated {deactivated_count}"
        )

        if other_count:
            breakdown += f" · Other {other_count}"

        lines.extend(
            [
                "",
                (f"<b>Recent terminal orders — last 24h: {len(terminal)}</b>"),
                breakdown,
            ]
        )

        if terminal:
            for order in terminal[:6]:
                lines.append(ApprovalBot._render_account_order(order))

            if len(terminal) > 6:
                lines.append(f"• … +{len(terminal) - 6} more")
        else:
            lines.append("• None")

        return "\n".join(lines)

    @staticmethod
    def _render_account_order(
        order: AccountOrder,
    ) -> str:
        quantity = ApprovalBot._fmt_decimal(order.quantity)

        kind = order.kind

        if order.is_protective and order.trigger_price is not None:
            price = "trigger " + ApprovalBot._fmt_decimal(order.trigger_price)

        elif order.avg_price is not None:
            price = ApprovalBot._fmt_decimal(order.avg_price)

        elif order.price is not None:
            price = ApprovalBot._fmt_decimal(order.price)

        else:
            price = "Market"

        if kind == "ENTRY":
            description = (
                f"{html.escape(order.side.value)} {html.escape(order.order_type)}"
            )

        elif kind == "CONDITIONAL":
            description = f"CONDITIONAL {html.escape(order.side.value)}"

        elif kind == "REDUCE":
            description = f"REDUCE {html.escape(order.side.value)}"

        else:
            description = kind

        return (
            "• "
            f"{html.escape(order.symbol)} "
            f"{description} "
            f"{quantity} @ {price}"
            " — "
            f"{html.escape(order.status)}"
        )

    @staticmethod
    def _fmt_signed(
        value: Decimal,
    ) -> str:
        rendered = f"{value:.2f}"

        if value > 0:
            return "+" + rendered

        return rendered

    @staticmethod
    def _render_exposure(
        intent: TradingIntent,
        *,
        exposure: SymbolExposure | None,
        exposure_error: str | None,
    ) -> str:
        if exposure_error is not None:
            return (
                "⚠️ <b>EXPOSURE CHECK "
                "UNAVAILABLE</b>\n"
                "Could not read the current "
                "Bybit position/open CCB orders. "
                "Execute remains available, but "
                "shared-symbol exposure may "
                "already exist.\n\n"
            )

        if exposure is None:
            return ""

        warnings: list[str] = []

        for position in exposure.positions:
            size = ApprovalBot._fmt_decimal(position.size)
            avg_price = ApprovalBot._fmt_decimal(position.avg_price)

            if position.side is intent.side:
                warnings.append(
                    "⚠️ <b>EXISTING SAME-SIDE "
                    "EXPOSURE</b>\n"
                    f"Bybit already has "
                    f"<b>{position.side.value} "
                    f"{html.escape(intent.symbol)}"
                    f"</b>: {size} @ "
                    f"{avg_price}.\n"
                    f"Executing this "
                    f"{intent.side.value} will "
                    "add to the same one-way "
                    "position and change its "
                    "average entry."
                )
            else:
                warnings.append(
                    "⚠️ <b>EXISTING OPPOSITE "
                    "EXPOSURE</b>\n"
                    f"Bybit already has "
                    f"<b>{position.side.value} "
                    f"{html.escape(intent.symbol)}"
                    f"</b>: {size} @ "
                    f"{avg_price}.\n"
                    f"Executing this "
                    f"{intent.side.value} may "
                    "reduce, close, or reverse "
                    "that position depending on "
                    "filled quantity."
                )

        if exposure.pending_entry_orders:
            same_side = [
                order
                for order in exposure.pending_entry_orders
                if (order.side is intent.side)
            ]

            opposite_side = [
                order
                for order in exposure.pending_entry_orders
                if (order.side is not intent.side)
            ]

            details: list[str] = []

            if same_side:
                details.append(f"{len(same_side)} same-side")

            if opposite_side:
                details.append(f"{len(opposite_side)} opposite-side")

            warnings.append(
                "⚠️ <b>PENDING CCB ENTRY "
                "ORDERS</b>\n"
                + ", ".join(details)
                + " unfilled order(s) for "
                + html.escape(intent.symbol)
                + " may fill later and further "
                "change the shared one-way "
                "position."
            )

        if not warnings:
            return ""

        return "\n\n".join(warnings) + "\n\n"

    @staticmethod
    def _fmt_decimal(
        value: Decimal,
    ) -> str:
        return format(
            value.normalize(),
            "f",
        )
