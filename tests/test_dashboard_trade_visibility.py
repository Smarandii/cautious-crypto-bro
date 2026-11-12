"""Public rows retain whole-trade reconciliation without leaking lineage."""

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

import pytest
from forensic_fixture import settled_entry_fixture

from cautious_crypto_bro.dashboard_metrics import public_trade_history
from cautious_crypto_bro.forensic_pnl import reconcile

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import install_dashboard_feed as installer  # noqa: E402
from export_trade_feed import build_trade_feed  # noqa: E402

from cautious_crypto_bro.dashboard_feed import (  # noqa: E402
    decode_public_feed,
    encode_public_feed,
    public_feed_from_snapshot,
    validate_public_feed,
)


@pytest.mark.integration
@pytest.mark.parametrize("symbol", ["BTCUSDT", "HUSDT", "QUSDT"])
def test_partial_closes_publish_one_complete_trade_without_private_lineage(
    tmp_path, symbol
):
    bundle = tmp_path / "trades.zip"
    private = "PRIVATE_SOURCE_CANARY"
    lineage = {
        "intents": [
            {
                "intent_id": private,
                "entry_order_ids": [private],
                "intent": {"symbol": symbol, "side": "LONG", "stop_loss": "90"},
                "metadata": {"risk_per_trade_pct": 1, "source_channel_id": private},
            }
        ]
    }
    rows = {
        "analysis/trade_lineage.jsonl": [lineage],
        "bybit/executions.jsonl": [
            {
                "execType": "Trade",
                "orderId": private,
                "execTime": "1000",
                "execQty": "2",
                "execPrice": "100",
                "closedSize": "0",
            }
        ],
        "bybit/closed_pnl.jsonl": [
            {
                "symbol": symbol,
                "side": "Sell",
                "updatedTime": str(time),
                "closedSize": "1",
                "closedPnl": str(pnl),
                "openFee": "0",
                "closeFee": "0",
            }
            for time, pnl in [(2000, 3), (3000, 5)]
        ],
        "bybit/transaction_log.jsonl": [],
    }
    rows.update(
        settled_entry_fixture(
            lineage["intents"], rows["bybit/executions.jsonl"], {private: "2"}
        )
    )
    rows["metadata.json"][0].update(
        created_at="2026-10-29T01:00:00+00:00",
        closed_pnl_as_of="2026-10-28T23:00:00+00:00",
    )
    with ZipFile(bundle, "w") as archive:
        for name, records in rows.items():
            archive.writestr(
                "forensic/" + name, "".join(json.dumps(r) + "\n" for r in records)
            )

    positions = reconcile(bundle)
    result = public_trade_history(positions, available=True)
    assert result["total_completed"] == 1
    assert result["rows"] == [
        {
            "symbol": symbol,
            "side": "LONG",
            "quantity": 2.0,
            "net_pnl_usdt": 8.0,
            "net_r": 0.4,
            "closed_at": "1970-01-01T00:00:03+00:00",
            "risk_per_trade_pct": 1.0,
        }
    ]
    assert private not in json.dumps(result)
    with closing(sqlite3.connect(":memory:")) as database:
        database.executescript(
            "CREATE TABLE execution_policy(id INTEGER, risk_per_trade_pct REAL); INSERT INTO execution_policy VALUES(1,3);"
        )
        feed = build_trade_feed(
            database,
            bundle,
            {
                "long_risk_multiplier": 1.0,
                "long_momentum_gate_enabled": False,
                "long_participation_skip_fraction": 0.0,
                "portfolio_stop_risk_cap_usdt": 4000.0,
                "exit_profile": "payoff_early_tight_trail_long_015",
            },
        )
    assert feed["risk"]["risk_per_trade_pct"] == 3
    assert feed["trade_history"]["rows"][0]["risk_per_trade_pct"] == 1
    assert feed["trade_history"]["as_of"] == "2026-10-28T23:00:00+00:00"
    assert private not in json.dumps(feed)
    assert validate_public_feed(feed) == feed


def test_public_history_preserves_unknown_and_unavailable_values():
    position = {"complete": True, "symbol": "BTCUSDT", "side": "SHORT"}
    result = public_trade_history([position, {"complete": False}], available=True)
    assert result["total_completed"] == 1
    assert result["rows"][0]["net_r"] is None
    assert result["rows"][0]["quantity"] is None
    assert result["rows"][0]["closed_at"] is None
    assert public_trade_history([], available=False)["total_completed"] is None
    with pytest.raises(ValueError, match="positive"):
        public_trade_history([], available=True, limit=0)


@pytest.mark.parametrize("unexpected", ["source_text", "order_id", "api_secret"])
def test_publication_rejects_extra_fields_and_nonfinite_numbers(unexpected):
    history = public_trade_history([], available=True)
    history["as_of"] = None
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-10-29T00:00:00+00:00",
            "risk": {"source_text": "PRIVATE_CANARY"},
            "trade_history": history,
        }
    )
    assert "PRIVATE_CANARY" not in json.dumps(feed)
    feed[unexpected] = "PRIVATE_CANARY"
    with pytest.raises(ValueError, match="fields"):
        validate_public_feed(feed)
    del feed[unexpected]
    feed["risk"]["risk_per_trade_pct"] = float("nan")
    with pytest.raises(ValueError, match="number"):
        validate_public_feed(feed)


def test_pages_installer_writes_only_validated_public_payload(tmp_path, monkeypatch):
    history = public_trade_history([], available=True)
    history["as_of"] = None
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-10-29T00:00:00+00:00",
            "risk": {},
            "trade_history": history,
        }
    )
    (tmp_path / "docs").mkdir()
    monkeypatch.setattr(installer, "ROOT", tmp_path)
    monkeypatch.setenv("PUBLIC_TRADE_FEED", json.dumps(feed))
    installer.main()
    target = tmp_path / "docs" / "trades.json"
    assert json.loads(target.read_text()) == feed
    feed["source_text"] = "PRIVATE_CANARY"
    monkeypatch.setenv("PUBLIC_TRADE_FEED", json.dumps(feed))
    before = target.read_bytes()
    with pytest.raises(SystemExit, match="validation failed"):
        installer.main()
    assert target.read_bytes() == before


def test_bounded_rows_are_newest_first_and_omissions_are_explicit():
    positions = [
        {
            "complete": True,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "closed_at": ms,
            "net_pnl": ms,
        }
        for ms in [1000, 3000, 2000]
    ]
    result = public_trade_history(positions, available=True, limit=2)
    assert [row["net_pnl_usdt"] for row in result["rows"]] == [3000, 2000]
    assert result["total_completed"] == 3
    assert result["omitted_count"] == 1


@pytest.mark.parametrize("symbol", ["HUSDT", "QUSDT"])
def test_final_public_contract_accepts_one_letter_assets(symbol):
    history = public_trade_history(
        [{"complete": True, "symbol": "BTCUSDT", "side": "LONG"}], available=True
    )
    history["as_of"] = None
    history["rows"][0]["symbol"] = symbol
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-10-31T00:00:00+00:00",
            "risk": {},
            "trade_history": history,
        }
    )
    assert validate_public_feed(feed)["trade_history"]["rows"][0]["symbol"] == symbol


@pytest.mark.parametrize(
    "symbol",
    [
        "USDT",
        "hUSDT",
        "H USDT",
        "H/USDT",
        "<H>USDT",
        "HUSDT\n",
        "H" * 41 + "USDT",
        "HUSDC",
    ],
)
def test_both_public_boundaries_reject_malformed_symbols(symbol):
    with pytest.raises(ValueError, match="symbol"):
        public_trade_history(
            [{"complete": True, "symbol": symbol, "side": "LONG"}], available=True
        )
    history = public_trade_history(
        [{"complete": True, "symbol": "BTCUSDT", "side": "LONG"}], available=True
    )
    history["as_of"] = None
    history["rows"][0]["symbol"] = symbol
    with pytest.raises(ValueError, match="symbol"):
        public_feed_from_snapshot(
            {
                "generated_at": "2026-10-31T00:00:00+00:00",
                "risk": {},
                "trade_history": history,
            }
        )


def test_build_channel_analytics_exports_funnel_and_pnl_without_private_ids():
    positions = [
        {
            "complete": True,
            "symbol": "BTCUSDT",
            "side": "LONG",
            "source_channel_id": -100111,
            "entry_qty": 1.0,
            "net_pnl": 45.5,
            "net_r": 0.91,
            "gross_pnl": 47.0,
            "fees": 1.5,
            "funding": 0.0,
            "risk_usdt": 50.0,
            "closed_at": 2000,
            "risk_per_trade_pct": 3.0,
        },
        {
            "complete": True,
            "symbol": "ETHUSDT",
            "side": "SHORT",
            "source_channel_id": -100111,
            "entry_qty": 2.0,
            "closed_qty": 2.0,
            "net_pnl": -15.5,
            "net_r": -0.31,
            "gross_pnl": -14.0,
            "fees": 1.5,
            "funding": 0.0,
            "risk_usdt": 50.0,
            "closed_at": 3000,
            "risk_per_trade_pct": 1.0,
        },
        {
            "complete": False,
            "symbol": "SOLUSDT",
            "side": "LONG",
            "source_channel_id": -100111,
            "entry_qty": 5.0,
            "closed_qty": 5.0,
            "open_entry_orders": 0,
            "ambiguous_rows": 0,
            "net_pnl": 20.0,
            "net_r": 0.4,
            "gross_pnl": 21.0,
            "fees": 1.0,
            "funding": 0.0,
            "risk_usdt": 50.0,
            "closed_at": 4000,
            "risk_per_trade_pct": 3.0,
        },
    ]
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript(
            """
            CREATE TABLE source_messages(channel_id INTEGER, message_id INTEGER, status TEXT);
            CREATE TABLE intents(intent_id TEXT, status TEXT, error TEXT, payload_json TEXT);
            CREATE TABLE position_actions(action_id TEXT, status TEXT, payload_json TEXT);
            INSERT INTO source_messages VALUES (-100111, 1, 'COMPLETED'), (-100111, 2, 'COMPLETED'), (-100111, 3, 'COMPLETED'), (-100222, 10, 'COMPLETED');
            INSERT INTO intents VALUES
              ('i1', 'EXECUTED', NULL, '{"source":{"channel_id":-100111,"message_id":1,"channel_title":"SECRET_ALPHA"}}'),
              ('i2', 'SKIPPED', NULL, '{"source":{"channel_id":-100111,"message_id":2,"channel_title":"SECRET_ALPHA"}}'),
              ('i3', 'FAILED', 'Existing strategy owns BTCUSDT', '{"source":{"channel_id":-100222,"message_id":10,"channel_title":"SECRET_BETA"}}');
            INSERT INTO position_actions VALUES
              ('a1', 'EXECUTED', '{"source":{"channel_id":-100111,"message_id":1,"channel_title":"SECRET_ALPHA"}}');
            CREATE TABLE source_decisions(channel_id INTEGER, message_id INTEGER, attempt_count INTEGER, payload_json TEXT);
            INSERT INTO source_decisions VALUES
              (-100111, 1, 1, '{"extraction":{"intents":[{"symbol":"BTCUSDT","relation":"NEW"}]},"normalized":{"open_intents":[{"symbol":"BTCUSDT"}]},"planning":{"errors":[]}}'),
              (-100111, 2, 1, '{"extraction":{"intents":[{"symbol":"ETHUSDT","relation":"UPDATE_EXISTING"}]},"normalized":{"open_intents":[]}}'),
              (-100111, 3, 1, '{"extraction":{"intents":[]},"normalized":{"open_intents":[]}}');
            """
        )
        from cautious_crypto_bro.dashboard_metrics import build_channel_analytics

        analytics = build_channel_analytics(db, positions)

    assert analytics is not None
    assert analytics["available"] is True
    serialized = json.dumps(analytics)
    assert "-100111" not in serialized and "SECRET_ALPHA" not in serialized
    rows_by_ch = {r["channel"]: r for r in analytics["rows"]}
    # Ordered by channel_id ASC: -100222 is source_1, -100111 is source_2
    ch2 = rows_by_ch["source_2"]
    assert ch2["total_posts"] == 3
    assert ch2["signal_posts"] == 2
    assert ch2["signal_post_ratio_pct"] == 66.67
    assert ch2["open_intents"] == 2
    assert ch2["executed_intents"] == 1
    assert ch2["pickup_rate_pct"] == 50.0
    assert ch2["completed_trades"] == 2
    assert ch2["winning_trades"] == 1 and ch2["losing_trades"] == 1
    assert ch2["win_rate_pct"] == 50.0
    assert ch2["net_pnl_usdt"] == 30.0
    assert ch2["net_r"] == 0.6
    assert ch2["long_trades"] == 2 and ch2["long_win_rate_pct"] == 100.0
    assert ch2["long_net_pnl_usdt"] == 65.5 and ch2["long_net_r"] == 1.31
    assert ch2["short_trades"] == 1 and ch2["short_win_rate_pct"] == 0.0
    assert ch2["short_net_pnl_usdt"] == -15.5 and ch2["short_net_r"] == -0.31
    assert ch2["size_reconciled_trades"] == 3
    assert ch2["size_reconciled_winning_trades"] == 2
    assert ch2["size_reconciled_losing_trades"] == 1
    assert ch2["size_reconciled_win_rate_pct"] == 66.67
    assert ch2["size_reconciled_net_pnl_usdt"] == 50.0
    assert ch2["size_reconciled_net_r"] == 1.0
    assert ch2["current_risk_trades"] == 2
    assert ch2["current_risk_winning_trades"] == 2
    assert ch2["current_risk_losing_trades"] == 0
    assert ch2["current_risk_win_rate_pct"] == 100.0
    assert ch2["current_risk_net_pnl_usdt"] == 65.5
    assert ch2["current_risk_net_r"] == 1.31
    assert ch2["current_risk_avg_win_usdt"] == 32.75
    assert ch2["current_risk_avg_loss_usdt"] is None
    assert ch2["active_open_strategies"] == 0
    assert rows_by_ch["source_1"]["failed_ownership_guard"] == 1
    assert analytics["exit_cohorts"][0]["exit_profile"] == "baseline"
    assert analytics["exit_cohorts"][0]["current_risk_trades"] == 2
    assert analytics["exit_cohorts"][0]["size_reconciled_trades"] == 3
    assert analytics["ladder_cohorts"][0]["ladder_depth"] == "E1_only"
    assert analytics["ladder_cohorts"][0]["size_reconciled_trades"] == 3
    assert analytics["setup_cohorts"][0]["setup"] == "MARKET_TRADER"
    assert analytics["setup_cohorts"][0]["size_reconciled_trades"] == 3
    dec = analytics["decision_cohorts"][0]
    assert dec["channel"] == "source_2"
    assert dec["audited_posts"] == 3
    assert dec["normalized_actionable"] == 1
    assert dec["update_existing_recap"] == 1
    assert dec["non_signal_commentary"] == 1
    assert dec["evidence_filtered"] == 0
    cx = analytics["channel_exit_cohorts"][0]
    assert cx["channel"] == "source_2" and cx["exit_profile"] == "baseline"
    assert cx["size_reconciled_trades"] == 3

    history = public_trade_history(positions, available=True)
    history["as_of"] = "2026-11-08T00:00:00+00:00"
    feed = public_feed_from_snapshot(
        {
            "generated_at": "2026-11-08T00:00:00+00:00",
            "risk": {},
            "trade_history": history,
            "channel_analytics": analytics,
        }
    )
    assert validate_public_feed(feed)["channel_analytics"] == analytics
    encoded_feed = encode_public_feed(feed)
    assert len(encoded_feed) < len(json.dumps(feed))
    assert decode_public_feed(encoded_feed) == feed
    assert decode_public_feed(json.dumps(feed)) == feed

    legacy = json.loads(json.dumps(feed))
    for summary in [
        *legacy["channel_analytics"]["rows"],
        legacy["channel_analytics"]["totals"],
    ]:
        summary.pop("failed_by_reason")
    assert validate_public_feed(legacy) == legacy

    bad_counts = json.loads(json.dumps(feed))
    bad_counts["channel_analytics"]["totals"]["failed_by_reason"]["stale_request"] += 1
    with pytest.raises(ValueError, match="failure total"):
        validate_public_feed(bad_counts)

    bad = json.loads(json.dumps(feed))
    bad["channel_analytics"]["rows"][0]["channel"] = "-100111"
    with pytest.raises(ValueError, match="channel identifier"):
        validate_public_feed(bad)


def test_entry_failure_reasons_agree_across_dashboard_reports():
    from cautious_crypto_bro.dashboard_metrics import (
        build_channel_analytics,
        summarize_intent_outcomes,
    )

    failures = [
        "existing strategy owns the symbol and risk budget exhausted",
        "existing strategy owns the symbol",
        "stale request",
        "notional too small",
        "exchange rejected order",
        "",
    ]
    with closing(sqlite3.connect(":memory:")) as db:
        db.executescript("""
            CREATE TABLE source_messages(channel_id INTEGER, message_id INTEGER, status TEXT);
            CREATE TABLE intents(intent_id TEXT, status TEXT, error TEXT, payload_json TEXT);
            CREATE TABLE position_actions(status TEXT, payload_json TEXT);
            INSERT INTO source_messages VALUES (1, 1, 'COMPLETED'), (2, 1, 'COMPLETED');
            """)
        db.executemany(
            "INSERT INTO intents VALUES (?, 'FAILED', ?, ?)",
            [
                (
                    str(index),
                    reason,
                    json.dumps(
                        {"source": {"channel_id": 1 + index % 2, "message_id": 1}}
                    ),
                )
                for index, reason in enumerate(failures)
            ],
        )
        analytics = build_channel_analytics(db, [])

    assert analytics is not None
    outcomes = summarize_intent_outcomes({"FAILED": len(failures)}, failures)
    totals = analytics["totals"]
    assert totals["failed_by_reason"] == outcomes["failed_by_reason"]
    assert all(count == 1 for count in totals["failed_by_reason"].values())
    assert sum(row["failed_intents"] for row in analytics["rows"]) == len(failures)
    for row in analytics["rows"]:
        assert sum(row["failed_by_reason"].values()) == row["failed_intents"]


@pytest.mark.parametrize("fraction", [None, 0, 0.5, 1, -0.1, 1.1, True])
def test_public_feed_validates_optional_long_exit_control_fraction(fraction):
    timestamp = "2026-11-12T00:00:00+00:00"
    history = public_trade_history([], available=True) | {"as_of": timestamp}
    snapshot = {"generated_at": timestamp, "trade_history": history, "risk": {}}
    legacy = public_feed_from_snapshot(snapshot)
    assert "long_exit_control_fraction" not in validate_public_feed(legacy)["risk"]
    snapshot["risk"]["long_exit_control_fraction"] = fraction
    if fraction is True or fraction in (-0.1, 1.1):
        with pytest.raises(ValueError):
            public_feed_from_snapshot(snapshot)
    else:
        assert (
            public_feed_from_snapshot(snapshot)["risk"]["long_exit_control_fraction"]
            == fraction
        )


def test_long_exit_review_uses_terminal_current_risk_long_trades():
    from cautious_crypto_bro.dashboard_cohorts import build_exit_cohorts
    from cautious_crypto_bro.dashboard_feed import _validate_exit_cohorts
    from cautious_crypto_bro.forensic_pnl import (
        DEMO_LONG_EXIT_AB_CONTROL_PROFILE,
        DEMO_LONG_EXIT_AB_TREATMENT_PROFILE,
    )

    control = DEMO_LONG_EXIT_AB_CONTROL_PROFILE
    treatment = DEMO_LONG_EXIT_AB_TREATMENT_PROFILE
    matching = {
        "exit_profile": control,
        "side": "LONG",
        "complete": True,
        "net_pnl": 10,
        "net_r": 0.1,
    }
    old_risk = matching | {"net_pnl": 50}
    incomplete = matching | {"complete": False, "net_pnl": 100}
    short = matching | {"side": "SHORT", "net_pnl": 200}
    treated = matching | {"exit_profile": treatment, "net_pnl": -20, "net_r": -0.2}
    complete = [matching, old_risk, short, treated]
    current_risk = [matching, incomplete, short, treated]
    cohorts = build_exit_cohorts(complete, [*complete, incomplete], current_risk)
    _validate_exit_cohorts(cohorts)
    by_profile = {row["exit_profile"]: row for row in cohorts}
    assert by_profile[control]["current_risk_terminal"] == {
        "trades": 1,
        "net_pnl_usdt": 10.0,
        "net_r": 0.1,
        "review_target": 20,
    }
    assert by_profile[treatment]["current_risk_terminal"] == {
        "trades": 1,
        "net_pnl_usdt": -20.0,
        "net_r": -0.2,
        "review_target": 20,
    }
    legacy = [
        {k: v for k, v in row.items() if k != "current_risk_terminal"}
        for row in cohorts
    ]
    _validate_exit_cohorts(legacy)
