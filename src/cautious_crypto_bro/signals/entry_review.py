"""Bound model review proposals to a current caption and trusted copy state."""

from __future__ import annotations

import re

from ..domain import (
    EntryReview,
    IntentExtraction,
    Side,
    SignalPositionContext,
    SourceMessage,
)

_FILLED = re.compile(
    r"\b(?:(?:my|our)\b.{0,100}\b(?:entry|limit(?: order)?)\b.{0,80}"
    r"\b(?:just filled|has just filled|(?:has )?filled (?:just )?now)|"
    r"(?:моя|наша)?\s*лимитк[аи]\b.{0,100}\b(?:исполнилась|исполнились|сработала)|"
    r"лимитный ордер\b.{0,100}\bтолько что исполнился|"
    r"сейчас исполнилась лимитка\b.{0,100})",
    re.IGNORECASE,
)
_EXCLUDED = re.compile(
    r"\b(?:if|when|would|could|example|imagine|yesterday|previous|historical|"
    r"recap|results|not|never|hasn't|didn't|если|когда|пример|вчера|итоги|не)\b",
    re.IGNORECASE,
)
_BARE_LIMIT_FILL = re.compile(
    r"(?i:\bлимитка\s+по\s+)\$?([A-Z][A-Z0-9]{1,29})"
    r"(?i:\s+(?:исполнилась|сработала)\b)"
)
_CURRENT_ENTRY = re.compile(
    r"\b(?:по\s+рынку\s+вход|вход\s+по\s+этой\s+монете)"
    r"\s+сейчас\s+(?:тоже\s+)?актуален(?=\s*(?:[.!]|$))",
    re.IGNORECASE,
)


def _bare_ticker_reentry(text: str, evidence: str, symbol: str) -> bool:
    fill = _BARE_LIMIT_FILL.search(evidence)
    if fill is None or not _CURRENT_ENTRY.search(text):
        return False
    ticker = fill[1]
    caption_tickers = set(re.findall(r"(?<!\w)[A-Z][A-Z0-9]{1,29}(?!\w)", text))
    return symbol == f"{ticker}USDT" and caption_tickers == {ticker}


def entry_reviews(
    source: SourceMessage,
    extraction: IntentExtraction,
    context: SignalPositionContext | None,
    executable_symbols: set[str],
) -> tuple[EntryReview, ...]:
    """A model proposal alone cannot establish current fill or symbol attribution.

    This deliberately bounded English/Russian caption contract does not claim
    complete natural-language recognition. Images cannot supply review evidence.
    """
    result: dict[str, EntryReview] = {}
    text = source.text or ""
    for raw in extraction.entry_reviews:
        evidence = raw.evidence_text.strip()
        if (
            not evidence
            or evidence not in text
            or not _FILLED.search(evidence)
            or _EXCLUDED.search(text)
            or re.search(
                r"\b(?:long|short|buy|sell|лонг|шорт)\b", evidence, re.IGNORECASE
            )
            or raw.symbol in executable_symbols
        ):
            continue
        symbols = re.findall(
            r"(?<![A-Z0-9])[A-Z0-9]{2,30}USDT(?![A-Z0-9])", evidence.upper()
        )
        if set(symbols) != {raw.symbol} and not _bare_ticker_reentry(
            text, evidence, raw.symbol
        ):
            continue
        if context is not None and any(
            context.has_existing_copy(raw.symbol, side) for side in Side
        ):
            continue
        result[raw.symbol] = EntryReview(
            source=source,
            symbol=raw.symbol,
            evidence_text=evidence,
            ownership_unknown=context is None or not context.account_state_available,
        )
    return tuple(result.values())
