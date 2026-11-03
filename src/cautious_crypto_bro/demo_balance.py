"""Reconstruct USDT wallet closes from complete, cash-balanced Demo ledger windows.

This is account activity, not strategy attribution or unrealized equity. Callers
must fetch every page and category in the stated interval and anchor it with an
unchanged USDT wallet snapshot. Private identifiers never enter the result.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from itertools import groupby

from .bybit.client import BybitClient


def _component(row: dict, name: str) -> Decimal:
    value = row.get(name)
    # Bybit documents empty funding/fee fields for events where they do not apply.
    return _decimal("0" if value == "" else value)


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError("Incomplete Demo wallet evidence")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Invalid Demo wallet amount") from None
    if not number.is_finite():
        raise ValueError("Non-finite Demo wallet amount")
    return number


def _cash_path(rows: list[dict], opening: Decimal | None) -> list[Decimal]:
    """Order equal-millisecond cash changes by their actual balance edges.

    Bybit IDs are identifiers, not ordering evidence. An Euler trail consumes
    each cash transition exactly once, including zero-change records and cycles.
    """
    edges: dict[Decimal, list[Decimal]] = defaultdict(list)
    expected: Counter[tuple[Decimal, Decimal]] = Counter()
    degree: Counter[Decimal] = Counter()
    for row in rows:
        after = _decimal(row["cashBalance"])
        before = after - _decimal(row["change"])
        edges[before].append(after)
        expected[before, after] += 1
        degree[before] += 1
        degree[after] -= 1
    starts = [balance for balance, count in degree.items() if count == 1]
    ends = [balance for balance, count in degree.items() if count == -1]
    if any(abs(count) > 1 for count in degree.values()) or not (
        len(starts) == len(ends) == 1 or not starts and not ends
    ):
        raise ValueError("Demo ledger has contradictory cash transitions")
    root = opening
    if root is None:
        root = starts[0] if starts else next(iter(edges))
    stack = [root]
    reverse = []
    while stack:
        if edges[stack[-1]]:
            stack.append(edges[stack[-1]].pop())
        else:
            reverse.append(stack.pop())
    path = list(reversed(reverse))
    if (
        len(path) != len(rows) + 1
        or Counter(zip(path, path[1:], strict=False)) != expected
        or (starts and (path[0] != starts[0] or path[-1] != ends[0]))
    ):
        raise ValueError("Demo ledger cash-balance chain is incomplete")
    return path


def _validate_row(row: dict, first_ms: int, last_ms: int) -> None:
    if row.get("currency") != "USDT":
        raise ValueError("Non-USDT activity prevents an overall USDT history")
    timestamp = row.get("transactionTime")
    if not isinstance(timestamp, str) or not timestamp.isdecimal():
        raise ValueError("Invalid Demo ledger timestamp")
    if not first_ms <= int(timestamp) <= last_ms:
        raise ValueError("Demo ledger row lies outside its queried window")
    if _decimal(row.get("change")) != (
        _component(row, "cashFlow")
        + _component(row, "funding")
        - _component(row, "fee")
    ):
        raise ValueError("Demo ledger cash components disagree")
    for name in ("type", "category", "symbol", "side"):
        value = row.get(name)
        if not isinstance(value, str) or len(value) > 80:
            raise ValueError("Invalid Demo ledger activity field")
    if row["side"] not in ("Buy", "Sell", "None", ""):
        raise ValueError("Unknown Demo ledger execution side")
    _decimal(row.get("cashBalance"))
    for name in ("qty", "tradePrice"):
        _component(row, name)


def _validated_rows(rows: list[dict], first_ms: int, last_ms: int) -> list[dict]:
    unique: dict[str, dict] = {}
    for row in rows:
        identity = row.get("id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("Demo ledger identity is missing")
        if identity in unique:
            if unique[identity] != row:
                raise ValueError("Conflicting Demo ledger duplicate")
            continue
        _validate_row(row, first_ms, last_ms)
        unique[identity] = row
    return sorted(unique.values(), key=lambda row: int(row["transactionTime"]))


def daily_wallet_history(
    rows: list[dict],
    *,
    start: datetime,
    as_of: datetime,
    wallet_balance: str,
) -> dict:
    """Build uncapped account activity and verified daily closes, excluding UPL.

    Today and a first day queried after UTC midnight remain provisional.
    """
    if (
        start.tzinfo is None
        or as_of.tzinfo is None
        or start.utcoffset() != timedelta(0)
        or as_of.utcoffset() != timedelta(0)
        or not start < as_of
        or as_of - start > timedelta(days=32)
    ):
        raise ValueError("Invalid Demo wallet history interval")
    wallet = _decimal(wallet_balance)
    ordered = _validated_rows(
        rows, int(start.timestamp() * 1000), int(as_of.timestamp() * 1000)
    )
    groups = []
    previous = None
    opening = None
    for timestamp, batch in groupby(ordered, lambda row: int(row["transactionTime"])):
        path = _cash_path(list(batch), previous)
        if opening is None:
            opening = path[0]
        previous = path[-1]
        groups.append((timestamp, previous))
    if previous is not None and previous != wallet:
        raise ValueError("Demo ledger does not reconcile to its wallet anchor")

    activities = _public_activities(ordered)
    days = _daily_closes(start, as_of, groups, opening, wallet, activities)
    return {
        "metric": "USDT wallet balance",
        "currency": "USDT",
        "timezone": "UTC",
        "includes_unrealized_pnl": False,
        "start": start.isoformat(),
        "as_of": as_of.isoformat(),
        "days": days,
    }


def _public_activities(ordered: list[dict]) -> dict[str, list[dict]]:
    activities: dict[str, list[dict]] = defaultdict(list)
    for row in ordered:
        at = datetime.fromtimestamp(int(row["transactionTime"]) / 1000, UTC)
        activities[at.date().isoformat()].append(
            {
                "at": at.isoformat(),
                "kind": row["type"],
                "category": row["category"],
                "symbol": row["symbol"],
                "side": row["side"],
                "quantity": format(_component(row, "qty"), "f"),
                "price": format(_component(row, "tradePrice"), "f"),
                "cash_flow": format(_component(row, "cashFlow"), "f"),
                "fee": format(_component(row, "fee"), "f"),
                "funding": format(_component(row, "funding"), "f"),
                "wallet_change": format(_decimal(row["change"]), "f"),
            }
        )

    return activities


def _daily_closes(
    start: datetime,
    as_of: datetime,
    groups: list[tuple[int, Decimal]],
    opening: Decimal | None,
    wallet: Decimal,
    activities: dict[str, list[dict]],
) -> list[dict]:
    days = []
    cursor = start.replace(hour=0, minute=0, second=0, microsecond=0)
    balance = opening
    index = 0
    while cursor.date() <= as_of.date():
        next_day = cursor + timedelta(days=1)
        opening_balance = balance
        while index < len(groups) and groups[index][0] < int(
            next_day.timestamp() * 1000
        ):
            balance = groups[index][1]
            index += 1
        current = cursor.date() == as_of.date()
        # With no cash records, do not project today's anchor into the past.
        close = wallet if current else balance
        change = (
            close - opening_balance
            if close is not None and opening_balance is not None
            else None
        )
        date = cursor.date().isoformat()
        days.append(
            {
                "date": date,
                "balance": format(close, "f") if close is not None else None,
                "change": format(change, "f") if change is not None else None,
                "complete": close is not None and start <= cursor and not current,
                "activity": activities.get(date, []),
            }
        )
        cursor = next_day
    return days


def _wallet(response: dict) -> str:
    accounts = response.get("result", {}).get("list")
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise ValueError("Demo wallet account is unavailable")
    account = accounts[0]
    if not isinstance(account, dict) or account.get("accountType") != "UNIFIED":
        raise ValueError("Unsupported Demo wallet account")
    coins = account.get("coin")
    if not isinstance(coins, list) or any(not isinstance(c, dict) for c in coins):
        raise ValueError("Demo wallet currencies are unavailable")
    usdt = [c for c in coins if c.get("coin") == "USDT"]
    if len(usdt) != 1:
        raise ValueError("Demo USDT wallet is unavailable")
    for coin in coins:
        amount = _decimal(coin.get("walletBalance"))
        if coin.get("coin") != "USDT" and amount != 0:
            raise ValueError("Funded non-USDT wallet prevents an overall USDT view")
        for name in ("spotBorrow", "borrowAmount", "accruedInterest"):
            if _component(coin, name) != 0:
                raise ValueError("Borrowed wallet requires separate balance semantics")
    return str(_decimal(usdt[0]["walletBalance"]))


def fetch_daily_wallet_history(client: BybitClient) -> dict:
    """Read all categories/pages, then verify an unchanged account anchor.

    A changing wallet, reset gap, unsupported currency or missing page makes this
    attempt unavailable. The publisher should retain its last verified series
    with its original timestamp rather than publish a convincing false curve.
    """
    first = client.private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    balance = _wallet(first)
    timestamp = first.get("time")
    if type(timestamp) is not int or timestamp <= 0:
        raise ValueError("Demo wallet capture time is unavailable")
    as_of = datetime.fromtimestamp(timestamp / 1000, UTC)
    start = (as_of - timedelta(days=30)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_ms = int(start.timestamp() * 1000)
    rows = []
    window_ms = 7 * 86400 * 1000
    for window in range(start_ms, timestamp + 1, window_ms):
        rows.extend(
            client.paginate_private_list(
                "/v5/account/transaction-log",
                {
                    "accountType": "UNIFIED",
                    "startTime": window,
                    "endTime": min(window + window_ms - 1, timestamp),
                    "limit": 50,
                },
            )
        )
    after = client.private_get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if _wallet(after) != balance:
        raise ValueError("Demo wallet changed while reading its ledger")
    return daily_wallet_history(rows, start=start, as_of=as_of, wallet_balance=balance)
