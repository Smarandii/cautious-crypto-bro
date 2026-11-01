"""Deterministic evidence for current-post lifecycle actions and relations."""

from __future__ import annotations

import re
import unicodedata

from ..contract_symbols import contract_evidence_aliases
from ..domain import (
    PositionActionType,
    SourceMessage,
)

DEFAULT_REDUCTION_PCT = 50.0


def _normalize_evidence_text(
    value: str,
) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


_LIFECYCLE_NEGATION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    re.compile(
        r"\b(?:не|нельзя|без)\s+"
        r"(?:(?:надо|нужно|стоит|будем)\s+)?"
        r"(?:сейчас\s+)?фикс(?=\s|\d)"
    ),
    re.compile(
        r"\b(?:не|нельзя)\s+"
        r"(?:(?:надо|нужно|стоит|будем)\s+)?"
        r"(?:сейчас\s+)?"
        r"(?:"
        r"закрыва\w*|закрыть|закроем|"
        r"фиксир\w*|"
        r"тейк\w*|"
        r"выхож\w*|выход\w*"
        r")\b"
    ),
    re.compile(
        r"\b(?:do\s+not|don['’]?t|never|not\s+to|no\s+need\s+to)\s+"
        r"(?:(?:fully|completely|partially|immediately|currently|ever|now|yet)\s+){0,3}"
        r"(?:close|exit|reduce|trim|take)\b"
    ),
    re.compile(
        r"\bnot\s+"
        r"(?:closing|exiting|reducing|trimming|taking)\b"
    ),
)


# Observed current-caption suggestions; bare future "закроем" is not enough.
_SUGGESTED_REDUCE_PATTERN = re.compile(
    r"\b(?:часть\s+можно\s+закрыть|давайте\s+закроем\s+часть)\b"
)

_SHORTHAND_REDUCE_PATTERN = re.compile(
    r"\bфикс\s*(?:"
    r"\d+(?:[.,]\d+)?\s*(?:%|процент(?:а|ов)?\b)"
    r"|\d+\s*/\s*\d+)"
)


_REDUCE_INSTRUCTION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    _SUGGESTED_REDUCE_PATTERN,
    _SHORTHAND_REDUCE_PATTERN,
    # Russian take-profit milestone, unspecified size.
    # Example: "Фиксируем 3 тейк".
    # The ordinal identifies the TP milestone, not position size.
    re.compile(
        r"\b(?:"
        r"фиксируем|фиксирую|"
        r"зафиксируем|зафиксирую"
        r")\b"
        r".{0,20}"
        r"\b(?:\d+\s*)?тейк\w*\b"
    ),
    # Russian reversed partial-close wording.
    # Examples: "часть закройте", "половину закрой".
    re.compile(
        r"\b(?:"
        r"часть|половин\w*|четверт\w*"
        r")\b"
        r".{0,20}"
        r"\b(?:"
        r"закройте|закрой|закрывай|закрываем|"
        r"фиксируйте|фиксируй|фиксируем"
        r")\b"
    ),
    # Russian explicit partial-close wording:
    # "закрываем часть", "фиксируем половину", etc.
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть|"
        r"фиксируем|фиксирую|фиксируй|"
        r"зафиксируем|зафиксирую"
        r")\b"
        r".{0,40}"
        r"\b(?:часть|половин\w*|четверт\w*)\b"
    ),
    re.compile(
        r"\bчастичн\w*\b"
        r".{0,25}"
        r"\b(?:закрыва\w*|фиксир\w*)\b"
    ),
    # Explicit numeric reduction:
    # "закрываем 30%", "close 1/3", etc.
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть|"
        r"фиксируем|фиксирую|фиксируй|"
        r"тейкаем|тейкать|"
        r"close|reduce|trim"
        r")\b"
        r".{0,40}"
        r"(?:"
        r"\d+(?:[.,]\d+)?\s*"
        r"(?:"
        r"%|"
        r"percent(?:s)?\b|"
        r"процент(?:а|ов)?\b"
        r")"
        r"|"
        r"\d+\s*/\s*\d+"
        r")"
    ),
    # English partial-close wording.
    re.compile(
        r"\b(?:"
        r"close|closing|reduce|trim|take|taking"
        r")\b"
        r".{0,40}"
        r"\b(?:part|partial|some|half|quarter)\b"
    ),
    re.compile(
        r"\b(?:take|taking)\b"
        r".{0,20}"
        r"\b(?:some|partial)\b"
        r".{0,20}"
        r"\bprofit\b"
    ),
    # Observed MENSA wording:
    # "оставлю небольшую часть ... остальное ... тейкать"
    re.compile(
        r"\bостав\w*\b"
        r".{0,60}"
        r"\bчаст\w*\b"
        r".{0,100}"
        r"\b(?:тейк\w*|фиксир\w*|закрыва\w*)\b"
    ),
)


_CLOSE_INSTRUCTION_PATTERNS: tuple[
    re.Pattern[str],
    ...,
] = (
    re.compile(
        r"\b(?:"
        r"закрываем|закрываю|закрывай|закрыть"
        r")\b"
    ),
    re.compile(
        r"\b(?:выхожу|выходим|выйти)\b"
        r".{0,25}"
        r"\b(?:из\s+)?позици\w*\b"
    ),
    re.compile(
        r"\bclose\b"
        r"(?:"
        r"\s+(?:the\s+)?"
        r"(?:position|trade|long|short)\b"
        r"|"
        r"\s+(?:it|this|now)\b"
        r"|"
        r"\s+(?:completely|fully)\b"
        r"|"
        r"(?=\s*(?:[.!?…]|$))"
        r")"
    ),
    re.compile(
        r"\bexit\b"
        r"(?:"
        r"\s+(?:the\s+)?"
        r"(?:position|trade|long|short)\b"
        r"|"
        r"\s+(?:it|this|now)\b"
        r"|"
        r"(?=\s*(?:[.!?…]|$))"
        r")"
    ),
)


def _symbol_close_variants(
    symbol: str,
) -> tuple[str, ...]:
    compact = re.sub(
        r"[^a-z0-9]",
        "",
        symbol.casefold(),
    )

    if not compact:
        return ()

    variants = {compact}
    for alias in contract_evidence_aliases(symbol):
        variants.update(_symbol_close_variants(alias))

    for quote in (
        "usdt",
        "usdc",
        "usd",
    ):
        if compact.endswith(quote) and len(compact) > len(quote):
            variants.add(compact[: -len(quote)])

    return tuple(
        sorted(
            variants,
            key=len,
            reverse=True,
        )
    )


def _symbol_specific_close_match(
    text: str,
    symbol: str,
) -> re.Match[str] | None:
    variants = _symbol_close_variants(symbol)

    if not variants:
        return None

    symbol_pattern = "|".join(re.escape(value) for value in variants)

    return re.search(
        (
            r"\b(?:close|exit)\b"
            r"\s+(?:the\s+)?"
            rf"(?:{symbol_pattern})\b"
            r"(?:\s+"
            r"(?:completely|fully|now)"
            r")?"
        ),
        text,
    )


def _optional_reduce_overridden_by_self_hold(
    text: str,
) -> bool:
    normalized = _normalize_evidence_text(text)

    optional_reduce = re.search(
        r"\bесли\b"
        r".{0,80}"
        r"\b(?:можете|можешь)\b"
        r".{0,40}"
        r"\b(?:закрыть|зафиксировать)\b"
        r".{0,20}"
        r"\b(?:часть|половин\w*)\b",
        normalized,
    )

    self_hold = re.search(
        r"\bя\b"
        r".{0,30}"
        r"\b(?:"
        r"подержу|держу|"
        r"пока\s+подержу|"
        r"пока\s+держу"
        r")\b",
        normalized,
    )

    return optional_reduce is not None and self_hold is not None


def _deterministic_action_evidence(
    text: str,
    action_type: PositionActionType,
    symbol: str,
) -> str | None:
    normalized = _normalize_evidence_text(text)

    if not normalized:
        return None

    if (
        _SUGGESTED_REDUCE_PATTERN.search(normalized)
        or _SHORTHAND_REDUCE_PATTERN.search(normalized)
    ) and re.search(
        r"\b(?:если|когда|при|после|потом|позже|завтра|вчера)\b", normalized
    ):
        # A suggestion tied to a future condition or a recap is not an action
        # now, and must not fall through to the generic CLOSE matcher either.
        return None

    if any(pattern.search(normalized) for pattern in _LIFECYCLE_NEGATION_PATTERNS):
        return None

    reduce_matches = tuple(
        m
        for pattern in _REDUCE_INSTRUCTION_PATTERNS
        for m in pattern.finditer(normalized)
    )

    if action_type is PositionActionType.REDUCE:
        if not reduce_matches:
            return None

        return reduce_matches[0].group(0)

    # First try an explicit CLOSE naming the same symbol
    # Gemma identified, e.g. "Close POL" for POLUSDT.
    symbol_close = _symbol_specific_close_match(
        normalized,
        symbol,
    )

    if symbol_close is not None:
        if not any(
            symbol_close.start() < reduce_match.end()
            and reduce_match.start() < symbol_close.end()
            for reduce_match in reduce_matches
        ):
            return symbol_close.group(0)

    # Generic CLOSE remains valid, but only when that
    # particular close phrase is not part of a REDUCE
    # instruction such as "close half".
    for close_match in (
        m
        for pattern in _CLOSE_INSTRUCTION_PATTERNS
        for m in pattern.finditer(normalized)
    ):
        if any(
            close_match.start() < reduce_match.end()
            and reduce_match.start() < close_match.end()
            for reduce_match in reduce_matches
        ):
            continue

        return close_match.group(0)

    return None


def _action_clauses(text: str) -> tuple[str, ...]:
    """Return sentence-like source clauses while preserving commas and amounts."""
    normalized = unicodedata.normalize("NFKC", text)
    if not normalized:
        return ()

    return tuple(
        clause.strip()
        for clause in re.split(r"[.!?…;\n]+", normalized)
        if clause.strip()
    )


def _clause_containing_evidence(
    post_text: str,
    evidence_text: str,
) -> str | None:
    evidence = _normalize_evidence_text(evidence_text)
    if not evidence:
        return None

    for clause in _action_clauses(post_text):
        if evidence in _normalize_evidence_text(clause):
            return clause

    return None


def _current_post_action_evidence(
    source: SourceMessage,
    action_type: PositionActionType,
    symbol: str,
    model_evidence_text: str | None,
) -> str | None:
    post_text = _normalize_evidence_text(source.text)

    if not post_text:
        return None

    if action_type is PositionActionType.CANCEL_ENTRIES:
        return _cancel_entries_evidence(source.text, symbol)

    if (
        action_type is PositionActionType.REDUCE
        and _optional_reduce_overridden_by_self_hold(post_text)
    ):
        return None

    for scope in _action_scopes(source.text, model_evidence_text):
        if not _lifecycle_symbol_matches(scope, symbol):
            continue
        evidence = _deterministic_action_evidence(
            scope,
            action_type,
            symbol,
        )

        if evidence is not None:
            # Return the complete source clause, not only the regex match.
            # This keeps authoritative percentages/fractions and surrounding
            # negation in the same deterministic scope.
            return scope

    return None


def _lifecycle_symbol_matches(text: str, symbol: str) -> bool:
    # Preserve explicit ticker evidence before case-folding the instruction.
    # Bare uppercase prose is not a ticker; unknown uppercase tokens make the
    # clause ambiguous and therefore require manual interpretation.
    prose = set(
        "I A AN THE WE YOU IT THIS NOW ALL FULL FULLY CLOSE CLOSING EXIT EXITING "
        "REDUCE REDUCING TRIM TAKE TAKING PROFIT PROFITS POSITION POSITIONS TRADE "
        "LONG SHORT PART PARTIAL HALF QUARTER TP SL USD USDT USDC "
        "DO NOT DONT NEVER HOLD KEEP".split()
    )
    mentions = {
        token.casefold()
        for token in re.findall(r"\b[0-9]*[A-Z][A-Z0-9]{1,14}\b", text)
        if token not in prose
    }
    mentions.update(re.findall(r"\$([a-z0-9]+)\b", text.casefold()))
    mentions.update(re.findall(r"\b([a-z0-9]+(?:usdt|usdc|usd))\b", text.casefold()))
    return not mentions or mentions.issubset(_symbol_close_variants(symbol))


def _cancel_entries_evidence(
    text: str,
    symbol: str,
) -> str | None:
    # Bind the ticker to this instruction, never arbitrary nearby text.
    clauses = re.split(r"[.!?…;\n]+", text)
    for index, clause in enumerate(clauses):
        normalized = _normalize_evidence_text(clause)
        match = re.search(
            r"\b(?:cancel|отменяем|отменить|отменяю)\b[^.!?\n]{0,60}?"
            r"(?:лимит\w*|заявк\w*|ордер\w*|entr\w*|limit\w*|orders?)",
            normalized,
        )
        if match is None or re.search(
            r"\b(?:не|not|don't|never)\s+(?:cancel|отмен\w*)", normalized
        ):
            continue
        # Uppercase ticker tokens are evidence from the actual post.
        tickers = re.findall(r"\b[0-9]*[A-Z][A-Z0-9]{1,14}\b", clause)
        scope = normalized
        if not tickers and index:
            previous = clauses[index - 1]
            tickers = re.findall(r"\b[0-9]*[A-Z][A-Z0-9]{1,14}\b", previous)
            scope = _normalize_evidence_text(previous) + ". " + normalized
        variants = _symbol_close_variants(symbol)
        if tickers and all(t.lower() in variants for t in tickers):
            return scope
    return None


def _action_scopes(
    post_text: str,
    model_evidence_text: str | None,
) -> list[str]:
    scopes: list[str] = []

    if model_evidence_text:
        clause = _clause_containing_evidence(
            post_text,
            model_evidence_text,
        )
        if clause is not None:
            scopes.append(clause)

    for clause in _action_clauses(post_text):
        if clause not in scopes:
            scopes.append(clause)

    return scopes


def _reduction_pct_from_evidence(
    evidence_text: str | None,
) -> float | None:
    if evidence_text is None:
        return None

    text = _normalize_evidence_text(evidence_text)

    # Explicit percentages are authoritative.
    # Examples: 30%, 0.5%, 30 percent,
    # 30 процентов.
    percent_match = re.search(
        r"(?<![\d.,])"
        r"(\d+(?:[.,]\d+)?)"
        r"\s*%",
        text,
    )

    if percent_match is None:
        percent_match = re.search(
            r"(?<![\d.,])"
            r"(\d+(?:[.,]\d+)?)"
            r"\s+"
            r"(?:percent(?:s)?|"
            r"процент(?:а|ов)?)"
            r"\b",
            text,
        )

    if percent_match is not None:
        value = float(percent_match.group(1).replace(",", "."))

        if 0 < value < 100:
            return value

        return None

    # Explicit mathematical fractions.
    fraction_match = re.search(
        r"(?<!\d)"
        r"(\d+)"
        r"\s*/\s*"
        r"(\d+)"
        r"(?!\d)",
        text,
    )

    if fraction_match is not None:
        numerator = int(fraction_match.group(1))
        denominator = int(fraction_match.group(2))

        if denominator > 0 and 0 < numerator < denominator:
            return numerator / denominator * 100

        return None

    # Common exact natural-language fractions.
    if re.search(r"\bhalf\b", text) or "половин" in text:
        return 50.0

    if re.search(r"\bquarter\b", text) or "четверт" in text:
        return 25.0

    # This helper is called only after the model has
    # classified the current caption as an explicit
    # REDUCE instruction and the exact caption evidence
    # has passed the destructive-action evidence gate.
    return DEFAULT_REDUCTION_PCT


def _caption_is_market_update(text: str) -> bool:
    text = _normalize_evidence_text(text)
    update = re.search(
        r"\b(?:профит\w*|тейк\w*|не\s+закрываю|держим|держу|"
        r"стоп\s+в\s+б[у/]+|taking\s+profit|take\s+profit|"
        r"still\s+holding|not\s+closing|move\s+stop\s+to\s+breakeven)\b",
        text,
    )
    new_entry = re.search(
        r"\b(?:открываю|открываем|вхожу|входим|беру|берем|подобрал|"
        r"перезахожу|перезаходим|open|opening|enter|entering|buy|sell|reenter|"
        r"(?:long|short|лонг|шорт)\s+(?:at\s+)?(?:market|маркетом))\b",
        text,
    )
    return update is not None and new_entry is None
