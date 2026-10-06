from __future__ import annotations

from datetime import datetime

from .domain import (
    AccountPositionContext,
    AccountStateSummary,
    IntentStatus,
    PositionActionIntent,
    Side,
    SignalContextSnapshot,
    SignalPositionContext,
    SourceOpenContext,
    TradingIntent,
)
from .ports import AccountGateway, SignalContextProviderStore


def build_position_context(
    *,
    channel_id: int,
    source_intents: tuple[
        TradingIntent,
        ...,
    ],
    executed_closes: tuple[
        PositionActionIntent,
        ...,
    ],
    account_state: AccountStateSummary | None,
) -> SignalPositionContext:
    latest_close = _latest_close_times(executed_closes)
    live_keys = _live_symbol_keys(account_state)

    source_history = tuple(
        context
        for context in (
            _source_open_context(intent, account_state, latest_close, live_keys)
            for intent in source_intents
        )
        if context is not None
    )

    return SignalPositionContext(
        source_channel_id=channel_id,
        account_state_available=(account_state is not None),
        source_open_history=source_history,
        account_positions=_account_positions(account_state),
    )


def _latest_close_times(
    executed_closes: tuple[
        PositionActionIntent,
        ...,
    ],
) -> dict[str, datetime]:
    latest_close: dict[str, datetime] = {}

    for action in executed_closes:
        current = latest_close.get(action.symbol)

        if current is None or action.created_at > current:
            latest_close[action.symbol] = action.created_at

    return latest_close


def _live_symbol_keys(
    account_state: AccountStateSummary | None,
) -> set[tuple[str, Side]]:
    if account_state is None:
        return set()

    live_keys = {
        (
            position.symbol,
            position.side,
        )
        for position in account_state.positions
    }

    live_keys.update(
        (
            order.symbol,
            order.side,
        )
        for order in account_state.open_orders
        if (
            not order.reduce_only
            and order.remaining_quantity > 0
            and order.order_link_id.startswith("ccb-")
        )
    )

    return live_keys


def _source_open_context(
    intent: TradingIntent,
    account_state: AccountStateSummary | None,
    latest_close: dict[str, datetime],
    live_keys: set[tuple[str, Side]],
) -> SourceOpenContext | None:
    active_copy: bool | None

    if account_state is None:
        active_copy = None

    elif intent.status is not IntentStatus.EXECUTED:
        active_copy = False

    else:
        closed_at = latest_close.get(intent.symbol)

        active_copy = (intent.symbol, intent.side) in live_keys and (
            closed_at is None or intent.created_at > closed_at
        )

    # With a healthy account snapshot, an old
    # EXECUTED intent that has no remaining live
    # position/order is irrelevant to the model.
    if intent.status is IntentStatus.EXECUTED and active_copy is False:
        return None

    return SourceOpenContext(
        symbol=intent.symbol,
        side=intent.side,
        status=intent.status,
        message_id=(intent.source.message_id),
        created_at=intent.created_at,
        active_copy=active_copy,
    )


def _account_positions(
    account_state: AccountStateSummary | None,
) -> tuple[AccountPositionContext, ...]:
    if account_state is None:
        return ()

    return tuple(
        AccountPositionContext(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            avg_price=position.avg_price,
        )
        for position in account_state.positions
    )


class SignalContextProvider:
    def __init__(
        self,
        *,
        store: SignalContextProviderStore,
        executor: AccountGateway,
    ) -> None:
        self._store = store
        self._executor = executor

    async def snapshot(
        self,
        channel_id: int,
    ) -> SignalContextSnapshot:
        source_intents = await self._store.get_recent_source_intents(channel_id)

        executed_closes = await self._store.get_recent_executed_closes()

        account_state = None
        account_state_error = None

        try:
            account_state = await self._executor.account_state()

        except Exception as exc:
            account_state_error = f"{type(exc).__name__}: {exc}"

        return SignalContextSnapshot(
            position_context=(
                build_position_context(
                    channel_id=channel_id,
                    source_intents=source_intents,
                    executed_closes=(executed_closes),
                    account_state=account_state,
                )
            ),
            account_state=account_state,
            account_state_error=(account_state_error),
        )
