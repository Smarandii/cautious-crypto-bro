from decimal import Decimal as D

import pytest

from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.mirror_replay import MirrorBar, replay_mirror


@pytest.mark.parametrize(
    ("side", "exit_kind", "prices"),
    [
        (Side.LONG, "target", ("101", "89")),
        (Side.SHORT, "target", ("111", "99")),
        (Side.LONG, "stop", ("106", "99")),
        (Side.SHORT, "stop", ("101", "94")),
    ],
)
@pytest.mark.parametrize("rate", [D("0.01"), D("-0.01")])
def test_exit_candle_settles_funding_before_intrabar_exit(
    side, rate, exit_kind, prices
):
    source_long = side is Side.LONG
    stop, target = (D(90), D(105)) if source_long else (D(110), D(95))
    high, low = map(D, prices)
    result = replay_mirror(
        side,
        D(100),
        stop,
        target,
        [
            MirrorBar(D(100), D(101), D(99), D("0.5")),
            MirrorBar(D(100), D(101), D(99), rate),
            MirrorBar(D(100), high, low, rate),
        ],
        market_entry=True,
        entry_fee=D(0),
        exit_fee=D(0),
    )
    # Two settlements after entry; the entry-candle rate must be excluded.
    price_r = D(2) if exit_kind == "target" else D(-1)
    signed_funding_r = D(40) * rate * (1 if source_long else -1)
    assert result.status == exit_kind
    assert result.net_r == price_r + signed_funding_r


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("opening", [D(89), D(111)])
def test_funding_at_gap_exit_does_not_invent_settlement_order(side, opening):
    stop, target = (D(90), D(105)) if side is Side.LONG else (D(110), D(95))
    result = replay_mirror(
        side,
        D(100),
        stop,
        target,
        [
            MirrorBar(D(100), D(101), D(99)),
            MirrorBar(opening, opening + 1, opening - 1, D("0.01")),
        ],
        market_entry=True,
        entry_fee=D(0),
        exit_fee=D(0),
    )
    assert result.status == "ambiguous"
    assert result.net_r is None


def test_open_mirror_marks_at_horizon_with_exit_fee():
    outcome = replay_mirror(
        Side.LONG,
        D("100"),
        D("90"),
        D("105"),
        [MirrorBar(D("100"), D("101"), D("98"))],
        market_entry=True,
        entry_fee=D("0.001"),
        exit_fee=D("0.001"),
        horizon_close=D("98"),
    )
    assert outcome.status == "open"
    assert outcome.net_r == D("0.3604")


@pytest.mark.parametrize(("entry_bars", "status"), [(1, "unfilled"), (2, "target")])
def test_limit_entry_expiry_preserves_later_mirror_exit(entry_bars, status):
    outcome = replay_mirror(
        Side.LONG,
        D("100"),
        D("90"),
        D("105"),
        [
            MirrorBar(D("99"), D("99.5"), D("98")),
            MirrorBar(D("100"), D("101"), D("99")),
            MirrorBar(D("100"), D("101"), D("89")),
        ],
        market_entry=False,
        entry_fee=D(0),
        exit_fee=D(0),
        limit_entry_bars=entry_bars,
    )
    assert outcome.status == status
    assert outcome.net_r == (D(2) if status == "target" else None)


@pytest.mark.parametrize(
    ("side", "stop", "first_target", "high", "low", "expected"),
    [
        (Side.LONG, "90", "105", "101", "89", "2.010"),
        (Side.SHORT, "110", "95", "111", "99", "1.590"),
    ],
)
def test_mirror_uses_original_stop_for_full_exit(
    side, stop, first_target, high, low, expected
):
    result = replay_mirror(
        side,
        D("100"),
        D(stop),
        D(first_target),
        [
            MirrorBar(D("100"), D("100"), D("100")),
            MirrorBar(D("100"), D("100"), D("100"), D("0.01")),
            MirrorBar(D("100"), D(high), D(low)),
        ],
        market_entry=True,
        entry_fee=D("0.005"),
        exit_fee=D("0.005"),
    )
    assert result.status == "target"
    assert result.net_r == D(expected)


@pytest.mark.parametrize(
    ("bars", "status", "net_r"),
    [
        ([("98", "99", "89")], "unfilled", None),
        ([("98", "106", "89")], "ambiguous", None),
        ([("100", "106", "89")], "ambiguous", None),
        ([("100", "102", "99"), ("107", "108", "90")], "stop", "-1.614"),
    ],
)
def test_limit_mirror_exposes_ambiguous_fills_and_prices_stop_gaps(bars, status, net_r):
    result = replay_mirror(
        Side.LONG,
        D("100"),
        D("90"),
        D("105"),
        [MirrorBar(*(D(value) for value in bar)) for bar in bars],
        market_entry=False,
        entry_fee=D(0),
        exit_fee=D(0),
        stop_slippage=D("0.01"),
    )
    assert result.status == status
    assert result.net_r == (D(net_r) if net_r is not None else None)
