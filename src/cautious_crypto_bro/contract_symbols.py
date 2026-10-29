"""Reviewed Bybit contract aliases; never infer arbitrary numeric prefixes."""

from collections.abc import Awaitable, Callable, Iterable
from decimal import Decimal

from .domain import (
    InstrumentContext,
    SourceOpenContext,
    TradeExecutionError,
    TradingIntent,
)

BYBIT_CONTRACT_ALIASES = {"PEPEUSDT": "1000PEPEUSDT"}


class ContractUnitError(ValueError):
    """An alias cannot safely interpret the supplied quote-price units."""


def contract_evidence_aliases(symbol: str) -> tuple[str, ...]:
    return tuple(
        raw
        for raw, target in BYBIT_CONTRACT_ALIASES.items()
        if target == symbol.upper()
    )


def owned_action_symbol(symbol: str, history: Iterable[SourceOpenContext]) -> str:
    target = BYBIT_CONTRACT_ALIASES.get(symbol)
    known = {record.symbol for record in history}
    if target is not None and symbol not in known and target in known:
        return target
    return symbol


def _check_quote_units(intent: TradingIntent, context: InstrumentContext) -> None:
    prices = [
        intent.entry.price,
        intent.entry.range_low,
        intent.entry.range_high,
        intent.stop_loss,
        intent.take_profit,
    ]
    market = context.market_price
    if not market.is_finite() or market <= 0:
        raise ContractUnitError(
            "Cannot verify contract quote units without a positive market price"
        )
    # Zen of Python: "In the face of ambiguity, refuse the temptation to guess."
    # This guard applies only to alias resolution; it never rescales source prices.
    # An order-of-magnitude envelope excludes 1000x quote-unit mistakes. Native
    # contracts retain the existing planning rules, including distant entries.
    for price in prices:
        if price is None:
            continue
        quoted = Decimal(str(price))
        if not quoted.is_finite() or not market / 10 <= quoted <= market * 10:
            raise ContractUnitError(
                "Ambiguous alias quote units; numeric prices were not converted"
            )


async def resolve_open_contract(
    intent: TradingIntent,
    market_context: Callable[[str], Awaitable[InstrumentContext]],
) -> tuple[TradingIntent, InstrumentContext | None]:
    target = BYBIT_CONTRACT_ALIASES.get(intent.symbol)
    if target is None:
        return intent, None
    try:
        context = await market_context(intent.symbol)
        return intent, context  # A valid exact exchange contract always wins.
    except TradeExecutionError as exc:
        message = str(exc).lower()
        invalid_symbol = message.startswith("bybit rejected request: 10001 ") and (
            "symbol invalid" in message or "invalid symbol" in message
        )
        if not invalid_symbol:
            raise
    context = await market_context(target)
    _check_quote_units(intent, context)
    return intent.model_copy(update={"symbol": target}), context
