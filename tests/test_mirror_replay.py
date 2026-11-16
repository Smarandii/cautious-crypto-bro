from decimal import Decimal as D

import pytest

from cautious_crypto_bro.domain import Side
from cautious_crypto_bro.mirror_replay import MirrorBar, replay_mirror


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
