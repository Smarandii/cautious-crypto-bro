from __future__ import annotations

import html
import logging
from datetime import (
    datetime,
    timezone,
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
)
from aiogram.utils.backoff import (
    BackoffConfig,
)

from .bybit import (
    BybitDemoExecutor,
    SymbolExposure,
)
from .domain import (
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    IntentStatus,
    Side,
    TakeProfitSource,
    TradingIntent,
)
from .storage import IntentStore

logger = logging.getLogger(
    __name__
)

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
        self._bot = Bot(
            token=token
        )
        self._dispatcher = (
            Dispatcher()
        )
        self._router = Router()

        self._dispatcher.include_router(
            self._router
        )

        self._approval_chat_id = (
            approval_chat_id
        )
        self._approver_user_id = (
            approver_user_id
        )
        self._max_age_seconds = (
            max_age_seconds
        )
        self._store = store
        self._executor = executor

        self._router.callback_query(
            IntentAction.filter(
                F.action
                == "execute"
            )
        )(self._execute)

        self._router.callback_query(
            IntentAction.filter(
                F.action
                == "skip"
            )
        )(self._skip)

    async def start(self) -> None:
        await self._bot.delete_webhook(
            drop_pending_updates=False
        )

    async def run(self) -> None:
        await self._dispatcher.start_polling(
            self._bot,
            backoff_config=(
                POLLING_BACKOFF
            ),
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
    ) -> None:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Execute",
                        callback_data=(
                            IntentAction(
                                action="execute",
                                intent_id=str(
                                    intent.intent_id
                                ),
                            ).pack()
                        ),
                        style="success",
                    ),
                    InlineKeyboardButton(
                        text="Skip",
                        callback_data=(
                            IntentAction(
                                action="skip",
                                intent_id=str(
                                    intent.intent_id
                                ),
                            ).pack()
                        ),
                        style="danger",
                    ),
                ]
            ]
        )

        await self._bot.send_message(
            chat_id=(
                self._approval_chat_id
            ),
            text=self._render(
                intent,
                plan,
                exposure=exposure,
                exposure_error=(
                    exposure_error
                ),
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
        if (
            callback.from_user.id
            != self._approver_user_id
        ):
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
            return

        intent_id = UUID(
            callback_data.intent_id
        )

        intent = await self._store.get_intent(
            intent_id
        )

        if intent is None:
            await callback.answer(
                "Intent no longer exists",
                show_alert=True,
            )
            return

        plan = (
            await self._store
            .get_execution_plan(
                intent_id
            )
        )

        if plan is None:
            await callback.answer(
                "Execution plan "
                "no longer exists",
                show_alert=True,
            )
            return

        if (
            intent.status
            is not IntentStatus.PENDING
        ):
            await callback.answer(
                "Already "
                f"{intent.status.value.lower()}",
                show_alert=True,
            )
            return

        age = (
            datetime.now(
                timezone.utc
            )
            - intent.created_at
        ).total_seconds()

        if age > self._max_age_seconds:
            await callback.answer(
                f"Intent is stale "
                f"({int(age)}s). "
                "Not executed.",
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

        await callback.answer(
            "Executing on Bybit Demo…"
        )

        try:
            order_ids = (
                await self._executor.execute(
                    plan
                )
            )

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
                (
                    "\n\n<b>FAILED</b>\n"
                    f"<code>"
                    f"{html.escape(str(exc))}"
                    f"</code>"
                ),
            )

            return

        await self._store.mark_executed(
            intent_id,
            order_ids,
        )

        ids_text = "\n".join(
            (
                "<code>"
                f"{html.escape(order_id)}"
                "</code>"
            )
            for order_id in order_ids
        )

        await self._edit_card(
            callback,
            intent,
            plan,
            (
                "\n\n"
                "<b>EXECUTED ON "
                "BYBIT DEMO</b>\n"
                f"{ids_text}"
            ),
        )

    async def _skip(
        self,
        callback: CallbackQuery,
        callback_data: IntentAction,
    ) -> None:
        if (
            callback.from_user.id
            != self._approver_user_id
        ):
            await callback.answer(
                "Not authorized",
                show_alert=True,
            )
            return

        intent_id = UUID(
            callback_data.intent_id
        )

        intent = await self._store.get_intent(
            intent_id
        )

        if intent is None:
            await callback.answer(
                "Intent no longer exists",
                show_alert=True,
            )
            return

        plan = (
            await self._store
            .get_execution_plan(
                intent_id
            )
        )

        if plan is None:
            await callback.answer(
                "Execution plan "
                "no longer exists",
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

        await callback.answer(
            "Skipped"
        )

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
        if callback.message is not None:
            await callback.message.edit_text(
                self._render(
                    intent,
                    plan,
                )
                + suffix,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=None,
            )

    @staticmethod
    def _render(
        intent: TradingIntent,
        plan: ExecutionPlan,
        *,
        exposure: SymbolExposure | None = None,
        exposure_error: str | None = None,
    ) -> str:
        source_url = (
            intent.source.telegram_url
        )

        source_line = (
            (
                '<a href="'
                f'{html.escape(source_url)}'
                '">Open source message</a>'
            )
            if source_url
            else "Source link unavailable"
        )

        if (
            intent.entry.type
            is EntryType.MARKET
        ):
            signal_entry = "Market"

        elif (
            intent.entry.type
            is EntryType.LIMIT
        ):
            assert (
                intent.entry.price
                is not None
            )

            signal_entry = (
                f"{intent.entry.price:g}"
            )

        else:
            assert (
                intent.entry.range_low
                is not None
            )
            assert (
                intent.entry.range_high
                is not None
            )

            signal_entry = (
                f"{intent.entry.range_low:g}"
                " – "
                f"{intent.entry.range_high:g}"
            )

        if plan.take_profit_targets:
            source_label = (
                "Policy fallback"
                if (
                    plan.take_profit_source
                    is TakeProfitSource.POLICY
                )
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

            tp_block = (
                f"<b>Take-profit ladder "
                f"({source_label})</b>\n"
                + "\n".join(tp_lines)
            )
        else:
            tp_block = (
                "Take profit: <b>"
                f"{ApprovalBot._fmt_decimal(plan.take_profit)}"
                "</b>"
            )

        order_lines = []

        for index, order in enumerate(
            plan.orders,
            start=1,
        ):
            qty = (
                ApprovalBot._fmt_decimal(
                    order.quantity
                )
            )

            if (
                order.order_type
                is ExecutionOrderType.MARKET
            ):
                order_lines.append(
                    f"{index}. "
                    f"Market × {qty}"
                )
            else:
                assert (
                    order.price
                    is not None
                )

                price = (
                    ApprovalBot._fmt_decimal(
                        order.price
                    )
                )

                order_lines.append(
                    f"{index}. "
                    f"{price} × {qty}"
                )

        total_qty = sum(
            (
                order.quantity
                for order in plan.orders
            ),
            Decimal("0"),
        )

        weighted_entry = (
            sum(
                (
                    order.reference_price
                    * order.quantity
                    for order
                    in plan.orders
                ),
                Decimal("0"),
            )
            / total_qty
        )

        if intent.side is Side.LONG:
            risk = (
                (
                    weighted_entry
                    - plan.stop_loss
                )
                / weighted_entry
                * Decimal("100")
            )
        else:
            risk = (
                (
                    plan.stop_loss
                    - weighted_entry
                )
                / weighted_entry
                * Decimal("100")
            )

        if plan.take_profit_targets:
            reward = Decimal("0")

            for target in plan.take_profit_targets:
                if intent.side is Side.LONG:
                    target_reward = (
                        (
                            target.price
                            - weighted_entry
                        )
                        / weighted_entry
                        * Decimal("100")
                    )
                else:
                    target_reward = (
                        (
                            weighted_entry
                            - target.price
                        )
                        / weighted_entry
                        * Decimal("100")
                    )

                reward += (
                    target_reward
                    * target.close_pct
                    / Decimal("100")
                )

            reward_label = (
                "Blended reward if all TPs hit"
            )
            rr_label = "Blended R:R"

        else:
            if intent.side is Side.LONG:
                reward = (
                    (
                        plan.take_profit
                        - weighted_entry
                    )
                    / weighted_entry
                    * Decimal("100")
                )
            else:
                reward = (
                    (
                        weighted_entry
                        - plan.take_profit
                    )
                    / weighted_entry
                    * Decimal("100")
                )

            reward_label = "Reward to TP"
            rr_label = "R:R"

        rr = (
            reward / risk
            if risk > 0
            else Decimal("0")
        )

        orders_text = "\n".join(
            order_lines
        )

        capital = (
            ApprovalBot._fmt_decimal(
                plan.policy
                .trading_capital_usdt
            )
        )

        risk_pct = (
            ApprovalBot._fmt_decimal(
                plan.policy
                .risk_per_trade_pct
            )
        )

        risk_budget = (
            ApprovalBot._fmt_decimal(
                plan.policy
                .risk_budget_usdt
            )
        )

        planned_loss = (
            ApprovalBot._fmt_decimal(
                plan.planned_max_loss_usdt
            )
        )

        exposure_block = (
            ApprovalBot
            ._render_exposure(
                intent,
                exposure=exposure,
                exposure_error=(
                    exposure_error
                ),
            )
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
            size = (
                ApprovalBot._fmt_decimal(
                    position.size
                )
            )
            avg_price = (
                ApprovalBot._fmt_decimal(
                    position.avg_price
                )
            )

            if (
                position.side
                is intent.side
            ):
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
                for order
                in exposure.pending_entry_orders
                if (
                    order.side
                    is intent.side
                )
            ]

            opposite_side = [
                order
                for order
                in exposure.pending_entry_orders
                if (
                    order.side
                    is not intent.side
                )
            ]

            details: list[str] = []

            if same_side:
                details.append(
                    f"{len(same_side)} "
                    "same-side"
                )

            if opposite_side:
                details.append(
                    f"{len(opposite_side)} "
                    "opposite-side"
                )

            warnings.append(
                "⚠️ <b>PENDING CCB ENTRY "
                "ORDERS</b>\n"
                + ", ".join(details)
                + " unfilled order(s) for "
                + html.escape(
                    intent.symbol
                )
                + " may fill later and further "
                "change the shared one-way "
                "position."
            )

        if not warnings:
            return ""

        return (
            "\n\n".join(
                warnings
            )
            + "\n\n"
        )

    @staticmethod
    def _fmt_decimal(
        value: Decimal,
    ) -> str:
        return format(
            value.normalize(),
            "f",
        )
