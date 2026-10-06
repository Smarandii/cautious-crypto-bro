from __future__ import annotations

import html
from decimal import Decimal

from .domain import (
    AccountOrder,
    AccountPnlSummary,
    AccountPosition,
    AccountStateSummary,
    EntryType,
    ExecutionOrderType,
    ExecutionPlan,
    IntentStatus,
    OpenOrderExposure,
    PositionActionExecutionOutcome,
    PositionActionIntent,
    PositionActionType,
    PositionExposure,
    Side,
    SymbolExposure,
    TakeProfitSource,
    TradingIntent,
)


def fmt_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def fmt_signed(value: Decimal) -> str:
    rendered = f"{value:.2f}"

    if value > 0:
        return "+" + rendered

    return rendered


def source_line(source_url: str | None) -> str:
    return (
        f'<a href="{html.escape(source_url)}">Open source message</a>'
        if source_url
        else "Source link unavailable"
    )


def render_action_outcome(
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
        f"{fmt_decimal(result.position_size_before)}"
        "</b>"
    )

    if result.submitted_quantity is None:
        suffix += "\nSubmitted: <b>full reduce-only close</b>"
    else:
        suffix += (
            f"\nSubmitted reduction: <b>{fmt_decimal(result.submitted_quantity)}</b>"
        )

    if result.cancelled_entry_orders:
        suffix += (
            f"\nCancelled CCB entry orders: <b>{result.cancelled_entry_orders}</b>"
        )

    return suffix


def render_position_action(
    action: PositionActionIntent,
    *,
    account_state: AccountStateSummary | None = None,
    account_state_error: str | None = None,
) -> str:
    action_source_line = source_line(action.source.telegram_url)

    if action.action is PositionActionType.CANCEL_ENTRIES:
        return (
            f"<b>CANCEL ENTRIES {html.escape(action.symbol)}</b>\n\n"
            "Cancel pending CCB entries from this source only.\n"
            "Positions, stops, exits and other sources are preserved.\n"
            "Ownership is rechecked on Execute.\n\n"
            f"Trader: <b>{html.escape(action.source.channel_title)}</b>\n{action_source_line}"
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
                f"{fmt_decimal(position.size)}"
                " @ "
                f"{fmt_decimal(position.avg_price)}"
                "</b>"
                " · Mark "
                f"{fmt_decimal(position.mark_price)}"
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
        f"<b>{html.escape(action.source.channel_title)}</b>\n" + action_source_line
    )


def render_exposure(
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

    warnings = [
        _position_exposure_warning(intent, position) for position in exposure.positions
    ]

    if exposure.pending_entry_orders:
        warnings.append(_pending_entry_warning(intent, exposure.pending_entry_orders))

    if not warnings:
        return ""

    return "\n\n".join(warnings) + "\n\n"


def _position_exposure_warning(
    intent: TradingIntent,
    position: PositionExposure,
) -> str:
    size = fmt_decimal(position.size)
    avg_price = fmt_decimal(position.avg_price)

    headline = (
        "EXISTING SAME-SIDE EXPOSURE"
        if position.side is intent.side
        else "EXISTING OPPOSITE EXPOSURE"
    )

    consequence = (
        "Executing this "
        f"{intent.side.value} will "
        "add to the same one-way "
        "position and change its "
        "average entry."
        if position.side is intent.side
        else (
            "Executing this "
            f"{intent.side.value} may "
            "reduce, close, or reverse "
            "that position depending on "
            "filled quantity."
        )
    )

    return (
        f"⚠️ <b>{headline}</b>\n"
        f"Bybit already has "
        f"<b>{position.side.value} "
        f"{html.escape(intent.symbol)}"
        f"</b>: {size} @ "
        f"{avg_price}.\n"
        f"{consequence}"
    )


def _pending_entry_warning(
    intent: TradingIntent,
    pending_entry_orders: tuple[OpenOrderExposure, ...],
) -> str:
    same_side = sum(1 for order in pending_entry_orders if (order.side is intent.side))

    opposite_side = sum(
        1 for order in pending_entry_orders if (order.side is not intent.side)
    )

    details: list[str] = []

    if same_side:
        details.append(f"{same_side} same-side")

    if opposite_side:
        details.append(f"{opposite_side} opposite-side")

    return (
        "⚠️ <b>PENDING CCB ENTRY "
        "ORDERS</b>\n"
        + ", ".join(details)
        + " unfilled order(s) for "
        + html.escape(intent.symbol)
        + " may fill later and further "
        "change the shared one-way "
        "position."
    )


def render_position_protection(
    position: AccountPosition,
    open_orders: tuple[AccountOrder, ...],
) -> str:
    stop_prices, take_profit_prices, trailing_stop = _collect_protection(
        position,
        open_orders,
    )

    if not stop_prices and not take_profit_prices and not trailing_stop:
        return "⚠️ No SL/TP protection detected"

    parts = [
        ("SL " + _render_protection_prices(stop_prices) if stop_prices else "SL —"),
        (
            "TP " + _render_protection_prices(take_profit_prices)
            if take_profit_prices
            else "TP —"
        ),
    ]

    if trailing_stop:
        parts.append("Trailing stop active")

    return "Protection: " + " · ".join(parts)


def _collect_protection(
    position: AccountPosition,
    open_orders: tuple[AccountOrder, ...],
) -> tuple[set[Decimal], set[Decimal], bool]:
    stop_prices: set[Decimal] = set()
    take_profit_prices: set[Decimal] = set()
    trailing_stop = bool(position.trailing_stop)

    if position.stop_loss is not None:
        stop_prices.add(position.stop_loss)

    if position.take_profit is not None:
        take_profit_prices.add(position.take_profit)

    for order in open_orders:
        if order.symbol != position.symbol:
            continue

        v2_price = _v2_take_profit_price(order, position)
        if v2_price is not None:
            take_profit_prices.add(v2_price)

        if not order.is_protective:
            continue

        if _merge_attached_protection(order, stop_prices, take_profit_prices):
            trailing_stop = True

    return stop_prices, take_profit_prices, trailing_stop


def _v2_take_profit_price(
    order: AccountOrder,
    position: AccountPosition,
) -> Decimal | None:
    # V2 take profits are standalone reduce-only limits, not attached TP orders.
    if (
        order.is_v2_take_profit
        and order.side is not position.side
        and order.status in {"New", "PartiallyFilled"}
        and order.remaining_quantity > 0
        and order.price is not None
    ):
        return order.price

    return None


def _merge_attached_protection(
    order: AccountOrder,
    stop_prices: set[Decimal],
    take_profit_prices: set[Decimal],
) -> bool:
    if order.kind == "TRAILING":
        return True

    if order.trigger_price is None:
        return False

    if order.kind == "SL":
        stop_prices.add(order.trigger_price)
    elif order.kind == "TP":
        take_profit_prices.add(order.trigger_price)

    return False


def _render_protection_prices(prices: set[Decimal]) -> str:
    ordered = sorted(prices)
    shown = ordered[:4]

    rendered = " / ".join(fmt_decimal(price) for price in shown)

    if len(ordered) > 4:
        rendered += f" / +{len(ordered) - 4}"

    return rendered


def render_account_state(
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

    lines = [
        "📊 <b>BYBIT DEMO — ACCOUNT</b>",
        "",
        "<b>Account P&amp;L</b>",
    ]

    lines.extend(_account_pnl_lines(state, pnl, pnl_error))
    lines.extend(_open_position_lines(state))
    lines.extend(_pending_order_lines(state))

    return "\n".join(lines)


def _account_pnl_lines(
    state: AccountStateSummary,
    pnl: AccountPnlSummary | None,
    pnl_error: str | None,
) -> list[str]:
    if pnl is not None:
        combined = pnl.realized_pnl + state.unrealised_pnl

        return [
            (f"Realized (tracked): <b>{fmt_signed(pnl.realized_pnl)} USDT</b>"),
            (f"Live uPnL (Bybit): <b>{fmt_signed(state.unrealised_pnl)} USDT</b>"),
            (f"Combined: <b>{fmt_signed(combined)} USDT</b>"),
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

    if pnl_error is not None:
        tracked = "unavailable"
    else:
        tracked = "not synced"

    return [
        f"Realized (tracked): <b>{tracked}</b>",
        (f"Live uPnL (Bybit): <b>{fmt_signed(state.unrealised_pnl)} USDT</b>"),
        "",
    ]


def _open_position_lines(state: AccountStateSummary) -> list[str]:
    total_notional = sum(
        (position.size * position.mark_price for position in state.positions),
        Decimal("0"),
    )

    position_word = "position" if len(state.positions) == 1 else "positions"

    lines = [
        (
            f"<b>{len(state.positions)} "
            f"{position_word}</b>"
            " · Notional ≈ "
            f"<b>{total_notional:.2f} USDT</b>"
        ),
    ]

    if not state.positions:
        lines.extend(
            [
                "",
                "No open positions.",
            ]
        )
        return lines

    for position in state.positions[:6]:
        notional = position.size * position.mark_price

        lines.extend(
            [
                "",
                (
                    f"<b>{html.escape(position.symbol)} "
                    f"{html.escape(position.side.value)}</b>"
                    " · "
                    f"{fmt_decimal(position.size)}"
                    " · ≈ "
                    f"{notional:.2f} USDT"
                ),
                (
                    "Entry "
                    f"{fmt_decimal(position.avg_price)}"
                    " → Mark "
                    f"{fmt_decimal(position.mark_price)}"
                    " · uPnL "
                    f"{fmt_signed(position.unrealised_pnl)} "
                    "USDT"
                ),
                render_position_protection(
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

    return lines


def _pending_order_lines(state: AccountStateSummary) -> list[str]:
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
        1
        for order in state.open_orders
        if order.is_protective or order.is_v2_take_profit
    )

    reduce_count = sum(
        1
        for order in state.open_orders
        if order.kind == "REDUCE" and not order.is_v2_take_profit
    )

    return [
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


def render(
    intent: TradingIntent,
    plan: ExecutionPlan,
    *,
    exposure: SymbolExposure | None = None,
    exposure_error: str | None = None,
) -> str:
    intent_source_line = source_line(intent.source.telegram_url)

    signal_entry = _render_signal_entry(intent)

    tp_block = _render_take_profit_block(plan)

    order_lines = _render_order_lines(plan)

    total_qty, weighted_entry = _weighted_entry(plan)

    rr_label, rr = _risk_reward(intent, plan, weighted_entry)

    orders_text = "\n".join(order_lines)

    risk_pct = fmt_decimal(plan.policy.risk_per_trade_pct)

    planned_loss = fmt_decimal(plan.planned_max_loss_usdt)

    exposure_block = render_exposure(
        intent,
        exposure=exposure,
        exposure_error=(exposure_error),
    )

    total_qty_text = fmt_decimal(total_qty)

    capital = plan.policy.trading_capital_usdt

    capital_text = fmt_decimal(capital) if capital is not None else "unavailable"

    return (
        f"<b>{html.escape(intent.side.value)} "
        f"{html.escape(intent.symbol)}</b>\n\n"
        f"{exposure_block}"
        f"Entry: <b>{html.escape(signal_entry)}</b>\n"
        f"Leverage: <b>{fmt_decimal(plan.leverage)}x</b> "
        f"({'Trader' if intent.leverage is not None else 'Default'})\n"
        f"SL: <b>{fmt_decimal(plan.stop_loss)}</b> "
        f"({plan.stop_loss_source.value})\n"
        f"{tp_block}\n\n"
        f"<b>Orders: {len(plan.orders)} · "
        f"total {total_qty_text}</b>\n"
        f"{orders_text}\n\n"
        "Capital: <b>"
        f"{capital_text} USDT</b>\n"
        f"Risk: <b>≤ {planned_loss} USDT</b> "
        f"({risk_pct}% policy)\n"
        f"{rr_label}: <b>{float(rr):.2f}</b>\n\n"
        f"Trader: <b>"
        f"{html.escape(intent.source.channel_title)}</b>\n"
        f"{intent_source_line}"
    )


def _render_signal_entry(intent: TradingIntent) -> str:
    if intent.entry.type is EntryType.MARKET:
        return "Market"

    if intent.entry.type is EntryType.LIMIT:
        assert intent.entry.price is not None

        return f"{intent.entry.price:g}"

    assert intent.entry.range_low is not None
    assert intent.entry.range_high is not None

    return f"{intent.entry.range_low:g} – {intent.entry.range_high:g}"


def _render_take_profit_block(plan: ExecutionPlan) -> str:
    if not plan.take_profit_targets:
        return f"Take profit: <b>{fmt_decimal(plan.take_profit)}</b>"

    if plan.strategy_version >= 2:
        source_label = (
            "Strategy V2"
            if (plan.take_profit_source is TakeProfitSource.POLICY)
            else "Trader-guided V2"
        )
    else:
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
            f"{fmt_decimal(target.price)}"
            f"</b> — "
            f"{fmt_decimal(target.close_pct)}%"
        )

    if plan.strategy_version >= 2:
        tp_lines.append(f"Runner: <b>{fmt_decimal(plan.runner_pct)}%</b>")

    return f"<b>TPs ({source_label})</b>\n" + "\n".join(tp_lines)


def _render_order_lines(plan: ExecutionPlan) -> list[str]:
    order_lines: list[str] = []

    for index, order in enumerate(
        plan.orders,
        start=1,
    ):
        qty = fmt_decimal(order.quantity)

        if plan.strategy_version >= 2:
            label = html.escape(order.name)

            risk_allocation = (
                fmt_decimal(order.risk_pct) if order.risk_pct is not None else "?"
            )

            prefix = f"{label} · {risk_allocation}% risk"
        else:
            prefix = str(index)

        if order.order_type is ExecutionOrderType.MARKET:
            order_lines.append(f"{prefix}: Market × {qty}")
        else:
            assert order.price is not None

            price = fmt_decimal(order.price)

            order_lines.append(f"{prefix}: {price} × {qty}")

    return order_lines


def _weighted_entry(plan: ExecutionPlan) -> tuple[Decimal, Decimal]:
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

    return total_qty, weighted_entry


def _risk_reward(
    intent: TradingIntent,
    plan: ExecutionPlan,
    weighted_entry: Decimal,
) -> tuple[str, Decimal]:
    if intent.side is Side.LONG:
        risk = (weighted_entry - plan.stop_loss) / weighted_entry * Decimal("100")
    else:
        risk = (plan.stop_loss - weighted_entry) / weighted_entry * Decimal("100")

    if plan.take_profit_targets:
        reward = Decimal("0")

        for target in plan.take_profit_targets:
            if intent.side is Side.LONG:
                target_reward = (
                    (target.price - weighted_entry) / weighted_entry * Decimal("100")
                )
            else:
                target_reward = (
                    (weighted_entry - target.price) / weighted_entry * Decimal("100")
                )

            reward += target_reward * target.close_pct / Decimal("100")

        rr_label = "Blended R:R"

    else:
        if intent.side is Side.LONG:
            reward = (
                (plan.take_profit - weighted_entry) / weighted_entry * Decimal("100")
            )
        else:
            reward = (
                (weighted_entry - plan.take_profit) / weighted_entry * Decimal("100")
            )

        rr_label = "R:R"

    rr = reward / risk if risk > 0 else Decimal("0")

    return rr_label, rr
