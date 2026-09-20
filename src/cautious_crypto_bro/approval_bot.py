from __future__ import annotations

import html
import logging
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
    AccountPosition,
    AccountStateSummary,
    SymbolExposure,
)
from .domain import (
    ApprovalMode,
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
from .execution_coordinator import (
    ExecutionCoordinator,
    IntentExecutionOutcome,
    PositionActionExecutionOutcome,
)
from .storage import (
    AccountPnlSummary,
    IntentStore,
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
        store: IntentStore,
        coordinator: ExecutionCoordinator,
    ) -> None:
        self._bot = Bot(token=token)
        self._dispatcher = Dispatcher()
        self._router = Router()

        self._dispatcher.include_router(self._router)

        self._approval_chat_id = approval_chat_id
        self._approver_user_id = approver_user_id
        self._store = store
        self._coordinator = coordinator

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

    async def _authorized(self, callback: CallbackQuery) -> bool:
        if callback.from_user.id == self._approver_user_id:
            return True

        await callback.answer("Not authorized", show_alert=True)
        return False

    async def _send_account_snapshot(
        self,
        state: AccountStateSummary | None,
        *,
        state_error: str | None,
        pnl: AccountPnlSummary | None,
        pnl_error: str | None,
    ) -> None:
        try:
            await self._bot.send_message(
                chat_id=self._approval_chat_id,
                text=self._render_account_state(
                    state,
                    error=state_error,
                    pnl=pnl,
                    pnl_error=pnl_error,
                ),
                parse_mode="HTML",
                disable_web_page_preview=True,
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
        account_state: AccountStateSummary | None = None,
        account_state_error: str | None = None,
        account_pnl: AccountPnlSummary | None = None,
        account_pnl_error: str | None = None,
        send_account_state: bool = True,
    ) -> None:
        if send_account_state:
            await self._send_account_snapshot(
                account_state,
                state_error=account_state_error,
                pnl=account_pnl,
                pnl_error=account_pnl_error,
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
        account_pnl: AccountPnlSummary | None = None,
        account_pnl_error: str | None = None,
        send_account_state: bool = True,
    ) -> None:
        if send_account_state:
            await self._send_account_snapshot(
                account_state,
                state_error=account_state_error,
                pnl=account_pnl,
                pnl_error=account_pnl_error,
            )

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
        if not await self._authorized(callback):
            return

        intent_id = UUID(callback_data.intent_id)

        await callback.answer("Executing on Bybit Demo…")

        outcome = await self._coordinator.execute_intent(
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

        await self._edit_card(
            callback,
            outcome.intent,
            outcome.plan,
            suffix,
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

        outcome = await self._coordinator.execute_position_action(
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

        suffix = self._render_action_outcome(outcome)

        await self._edit_position_action_card(
            callback,
            outcome.action,
            suffix,
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

        await self._edit_card(
            callback,
            intent,
            plan,
            "\n\n<b>SKIPPED</b>",
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

        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=(
                self._render(
                    outcome.intent,
                    outcome.plan,
                )
                + suffix
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
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

        await self._bot.send_message(
            chat_id=self._approval_chat_id,
            text=(
                self._render_position_action(outcome.action)
                + self._render_action_outcome(
                    outcome,
                    auto=True,
                )
            ),
            parse_mode="HTML",
            disable_web_page_preview=True,
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
                "⚠️ <b>AUTO EXECUTION "
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

    @staticmethod
    def _render_action_outcome(
        outcome: PositionActionExecutionOutcome,
        *,
        auto: bool = False,
    ) -> str:
        prefix = "AUTO-" if auto else ""

        if outcome.status is not IntentStatus.EXECUTED or outcome.result is None:
            return (
                "\n\n"
                f"<b>{prefix}"
                f"{html.escape(outcome.status.value)}"
                "</b>\n<code>"
                f"{html.escape(outcome.message)}"
                "</code>"
            )

        result = outcome.result

        suffix = (
            "\n\n"
            f"<b>{prefix}EXECUTED ON BYBIT DEMO</b>"
            "\nOrder: "
            f"<code>"
            f"{html.escape(result.order_id)}"
            "</code>\nPosition before: "
            f"<b>{result.position_side.value} "
            f"{ApprovalBot._fmt_decimal(result.position_size_before)}"
            "</b>"
        )

        if result.submitted_quantity is None:
            suffix += "\nSubmitted: <b>full reduce-only close</b>"
        else:
            suffix += (
                "\nSubmitted reduction: <b>"
                f"{ApprovalBot._fmt_decimal(result.submitted_quantity)}"
                "</b>"
            )

        if result.cancelled_entry_orders:
            suffix += (
                f"\nCancelled CCB entry orders: <b>{result.cancelled_entry_orders}</b>"
            )

        return suffix

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
            instruction = "Close 100%"
        else:
            assert action.close_pct is not None
            instruction = f"Reduce {action.close_pct:g}%"

        context_lines: list[str] = []
        pending_entries = 0

        if account_state is None:
            if account_state_error:
                context_lines.append("⚠️ Current position unavailable.")
            else:
                context_lines.append(
                    "Current position will be resolved from Bybit on Execute."
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
                context_lines.append("⚠️ No current position found.")

            elif len(positions) > 1:
                context_lines.append(
                    "⚠️ Multiple positions found; execution will fail safe."
                )

            else:
                position = positions[0]

                context_lines.append(
                    "Current: "
                    f"<b>{position.side.value} "
                    f"{ApprovalBot._fmt_decimal(position.size)}"
                    " @ "
                    f"{ApprovalBot._fmt_decimal(position.avg_price)}"
                    "</b>"
                    " · Mark "
                    f"{ApprovalBot._fmt_decimal(position.mark_price)}"
                )

        expected = (
            action.expected_side.value
            if action.expected_side is not None
            else "not specified"
        )

        return (
            "⚠️ <b>ACCOUNT-WIDE POSITION ACTION</b>\n\n"
            f"<b>{html.escape(action.action.value)} "
            f"{html.escape(action.symbol)}</b>"
            f" — {html.escape(instruction)}\n\n"
            + "\n".join(context_lines)
            + "\n"
            + "Expected side: "
            f"<b>{html.escape(expected)}</b>\n" + "CCB entries cancelled on Execute: "
            f"<b>{pending_entries}</b>\n" + "Position is re-read from live Bybit "
            "state when Execute is pressed.\n\n" + "Trader: "
            f"<b>{html.escape(action.source.channel_title)}</b>\n" + source_line
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
                    f"{ApprovalBot._fmt_decimal(target.close_pct)}%"
                )

            tp_block = f"<b>TPs ({source_label})</b>\n" + "\n".join(tp_lines)
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

            rr_label = "R:R"

        rr = reward / risk if risk > 0 else Decimal("0")

        orders_text = "\n".join(order_lines)

        risk_pct = ApprovalBot._fmt_decimal(plan.policy.risk_per_trade_pct)

        planned_loss = ApprovalBot._fmt_decimal(plan.planned_max_loss_usdt)

        exposure_block = ApprovalBot._render_exposure(
            intent,
            exposure=exposure,
            exposure_error=(exposure_error),
        )

        total_qty_text = ApprovalBot._fmt_decimal(total_qty)

        return (
            f"<b>{html.escape(intent.side.value)} "
            f"{html.escape(intent.symbol)}</b>\n\n"
            f"{exposure_block}"
            f"Entry: <b>{html.escape(signal_entry)}</b>\n"
            f"SL: <b>{ApprovalBot._fmt_decimal(plan.stop_loss)}</b>\n"
            f"{tp_block}\n\n"
            f"<b>Orders: {len(plan.orders)} · "
            f"total {total_qty_text}</b>\n"
            f"{orders_text}\n\n"
            f"Risk: <b>≤ {planned_loss} USDT</b> "
            f"({risk_pct}% policy)\n"
            f"{rr_label}: <b>{float(rr):.2f}</b>\n\n"
            f"Trader: <b>"
            f"{html.escape(intent.source.channel_title)}</b>\n"
            f"{source_line}"
        )

    @staticmethod
    def _render_account_state(
        state: AccountStateSummary | None,
        *,
        error: str | None = None,
        pnl: AccountPnlSummary | None = None,
        pnl_error: str | None = None,
    ) -> str:
        if state is None:
            return (
                "📊 <b>BYBIT DEMO — CURRENT EXPOSURE</b>\n\n"
                "⚠️ Account state unavailable.\n"
                "The approval card follows normally."
            )

        total_notional = sum(
            (position.size * position.mark_price for position in state.positions),
            Decimal("0"),
        )

        position_word = "position" if len(state.positions) == 1 else "positions"

        lines = [
            "📊 <b>BYBIT DEMO — ACCOUNT</b>",
            "",
        ]

        lines.append("<b>Account P&amp;L</b>")

        if pnl is not None:
            combined = pnl.realized_pnl + state.unrealised_pnl

            lines.extend(
                [
                    (
                        "Realized (tracked): "
                        f"<b>{ApprovalBot._fmt_signed(pnl.realized_pnl)} "
                        "USDT</b>"
                    ),
                    (
                        "Live uPnL (Bybit): "
                        f"<b>{ApprovalBot._fmt_signed(state.unrealised_pnl)} "
                        "USDT</b>"
                    ),
                    (f"Combined: <b>{ApprovalBot._fmt_signed(combined)} USDT</b>"),
                    (
                        "History: since "
                        f"<b>{pnl.history_start_at.date().isoformat()}</b>"
                        " · "
                        f"{pnl.record_count} realized record(s)"
                        " · "
                        f"{pnl.positive_count} positive"
                        " / "
                        f"{pnl.negative_count} negative"
                    ),
                    "",
                ]
            )

        elif pnl_error is not None:
            lines.extend(
                [
                    "Realized (tracked): <b>unavailable</b>",
                    (
                        "Live uPnL (Bybit): "
                        f"<b>{ApprovalBot._fmt_signed(state.unrealised_pnl)} "
                        "USDT</b>"
                    ),
                    "",
                ]
            )

        else:
            lines.extend(
                [
                    "Realized (tracked): <b>not synced</b>",
                    (
                        "Live uPnL (Bybit): "
                        f"<b>{ApprovalBot._fmt_signed(state.unrealised_pnl)} "
                        "USDT</b>"
                    ),
                    "",
                ]
            )

        lines.extend(
            [
                (
                    f"<b>{len(state.positions)} "
                    f"{position_word}</b>"
                    " · Notional ≈ "
                    f"<b>{total_notional:.2f} USDT</b>"
                ),
            ]
        )

        if state.positions:
            for position in state.positions[:6]:
                notional = position.size * position.mark_price

                lines.extend(
                    [
                        "",
                        (
                            f"<b>{html.escape(position.symbol)} "
                            f"{html.escape(position.side.value)}</b>"
                            " · "
                            f"{ApprovalBot._fmt_decimal(position.size)}"
                            " · ≈ "
                            f"{notional:.2f} USDT"
                        ),
                        (
                            "Entry "
                            f"{ApprovalBot._fmt_decimal(position.avg_price)}"
                            " → Mark "
                            f"{ApprovalBot._fmt_decimal(position.mark_price)}"
                            " · uPnL "
                            f"{ApprovalBot._fmt_signed(position.unrealised_pnl)} "
                            "USDT"
                        ),
                        ApprovalBot._render_position_protection(
                            position,
                            state.open_orders,
                        ),
                    ]
                )

            if len(state.positions) > 6:
                lines.extend(
                    [
                        "",
                        (f"… +{len(state.positions) - 6} more positions"),
                    ]
                )

        else:
            lines.extend(
                [
                    "",
                    "No open positions.",
                ]
            )

        entry_count = sum(
            1
            for order in state.open_orders
            if order.kind
            in {
                "ENTRY",
                "CONDITIONAL",
            }
        )

        protective_count = sum(1 for order in state.open_orders if order.is_protective)

        reduce_count = sum(1 for order in state.open_orders if order.kind == "REDUCE")

        lines.extend(
            [
                "",
                (
                    "Pending orders: "
                    f"<b>{entry_count}</b> entry"
                    " · "
                    f"<b>{protective_count}</b> protective"
                    " · "
                    f"<b>{reduce_count}</b> reduce/close"
                ),
            ]
        )

        return "\n".join(lines)

    @staticmethod
    def _render_position_protection(
        position: AccountPosition,
        open_orders: tuple[
            AccountOrder,
            ...,
        ],
    ) -> str:
        stop_prices: set[Decimal] = set()
        take_profit_prices: set[Decimal] = set()
        trailing_stop = False

        if position.stop_loss is not None:
            stop_prices.add(position.stop_loss)

        if position.take_profit is not None:
            take_profit_prices.add(position.take_profit)

        for order in open_orders:
            if order.symbol != position.symbol:
                continue

            if not order.is_protective:
                continue

            if order.kind == "TRAILING":
                trailing_stop = True
                continue

            if order.trigger_price is None:
                continue

            if order.kind == "SL":
                stop_prices.add(order.trigger_price)

            elif order.kind == "TP":
                take_profit_prices.add(order.trigger_price)

        if not stop_prices and not take_profit_prices and not trailing_stop:
            return "⚠️ No SL/TP protection detected"

        def render_prices(
            prices: set[Decimal],
        ) -> str:
            ordered = sorted(prices)
            shown = ordered[:4]

            rendered = " / ".join(ApprovalBot._fmt_decimal(price) for price in shown)

            if len(ordered) > 4:
                rendered += f" / +{len(ordered) - 4}"

            return rendered

        parts = []

        if stop_prices:
            parts.append("SL " + render_prices(stop_prices))
        else:
            parts.append("SL —")

        if take_profit_prices:
            parts.append("TP " + render_prices(take_profit_prices))
        else:
            parts.append("TP —")

        if trailing_stop:
            parts.append("Trailing stop active")

        return "Protection: " + " · ".join(parts)

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
