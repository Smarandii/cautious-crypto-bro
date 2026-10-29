"""Export the public Demo trade feed from a private forensic bundle and read-only DB."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from export_dashboard_data import forensic_as_of  # noqa: E402

from cautious_crypto_bro.config import get_settings  # noqa: E402
from cautious_crypto_bro.dashboard_feed import public_feed_from_snapshot  # noqa: E402
from cautious_crypto_bro.dashboard_metrics import public_trade_history  # noqa: E402
from cautious_crypto_bro.forensic_pnl import reconcile  # noqa: E402


def build_trade_feed(database, bundle: Path, risk: dict) -> dict:
    policy = database.execute(
        "SELECT risk_per_trade_pct FROM execution_policy WHERE id=1"
    ).fetchone()
    history = public_trade_history(reconcile(bundle), available=True)
    history["as_of"] = forensic_as_of(bundle)
    return public_feed_from_snapshot(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "trade_history": history,
            "risk": {
                **risk,
                "risk_per_trade_pct": float(policy[0]) if policy else None,
            },
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    risk = {
        "long_risk_multiplier": float(settings.demo_long_risk_multiplier),
        "long_momentum_gate_enabled": settings.demo_long_momentum_gate,
        "long_participation_skip_fraction": float(
            settings.demo_long_participation_skip_fraction
        ),
        "portfolio_stop_risk_cap_usdt": float(
            settings.demo_portfolio_stop_risk_cap_usdt
        )
        if settings.demo_portfolio_stop_risk_cap_usdt is not None
        else None,
        "exit_profile": settings.demo_exit_profile,
    }
    uri = settings.database_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as database:
        feed = build_trade_feed(database, args.bundle, risk)
    args.output.write_text(json.dumps(feed, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
