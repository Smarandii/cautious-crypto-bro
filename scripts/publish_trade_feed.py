"""Container-owned read-only Demo trade publication; no host shell or Docker socket."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen
from uuid import uuid4

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cautious_crypto_bro.dashboard_feed import validate_public_feed  # noqa: E402
from cautious_crypto_bro.state_ownership import state_ownership  # noqa: E402

REPOSITORY = "Smarandii/cautious-crypto-bro"
PUBLIC_URL = "https://smarandii.github.io/cautious-crypto-bro/trades.json"
INTERVAL_SECONDS = 15 * 60
EXPORT_TIMEOUT_SECONDS = 270


class PublisherSettings(BaseSettings):
    # The publisher must not require Telegram/LLM credentials or load the app .env.
    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    database_path: Path = Path("/state/cautious_crypto_bro.sqlite3")
    publisher_secrets_dir: Path = Path("/run/secrets")
    publisher_work_dir: Path = Path("/work")
    demo_long_risk_multiplier: Decimal = Field(default=Decimal("1"), gt=0, le=1)
    demo_long_momentum_gate: bool = False
    demo_long_participation_skip_fraction: Decimal = Field(
        default=Decimal("0"), ge=0, le=1
    )
    demo_portfolio_stop_risk_cap_usdt: Decimal | None = Field(default=None, gt=0)
    demo_exit_profile: str = "baseline"


def _secret(settings: PublisherSettings, name: str) -> str:
    value = (settings.publisher_secrets_dir / name).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("Publisher credential file is empty")
    return value


def export_feed(settings: PublisherSettings, bundle: Path, output: Path) -> None:
    from export_forensic_replay import ForensicAccess, export_bundle
    from export_trade_feed import build_trade_feed

    access = ForensicAccess(
        database_path=settings.database_path,
        bybit_api_key=_secret(settings, "bybit_api_key"),
        bybit_api_secret=_secret(settings, "bybit_api_secret"),
    )
    export_bundle(bundle, skip_market_candles=True, access=access)
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
        feed = build_trade_feed(database, bundle, risk)
    output.write_text(json.dumps(feed, allow_nan=False) + "\n", encoding="utf-8")


def _run_export(settings: PublisherSettings, bundle: Path, output: Path) -> None:
    # One bounded Python child inside this container; no host CLI or sibling exec.
    environment = dict(os.environ)
    for name, value in settings.model_dump().items():
        if value is None:
            environment.pop(name.upper(), None)
        else:
            environment[name.upper()] = str(value)
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--export",
            str(bundle),
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=EXPORT_TIMEOUT_SECONDS,
        env=environment,
    )
    if result.returncode:
        raise RuntimeError("Publisher export failed; private diagnostics withheld")


def _github_dispatch(feed: dict, settings: PublisherSettings) -> None:
    feed = validate_public_feed(feed)
    token = _secret(settings, "github_token")
    payload = {
        "ref": "master",
        "inputs": {"trade_feed": json.dumps(feed, separators=(",", ":"))},
    }
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/actions/workflows/pages.yml/dispatches",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "ccb-public-trade-feed",
        },
    )
    with urlopen(request, timeout=30) as response:
        if response.status != 204:
            raise RuntimeError("Pages dispatch was not accepted")


def _verify_publication(feed: dict) -> dict:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            request = Request(
                PUBLIC_URL + "?refresh=" + uuid4().hex,
                headers={"Cache-Control": "no-cache"},
            )
            with urlopen(request, timeout=15) as response:
                published = validate_public_feed(json.load(response))
            if published == feed:
                return {
                    "published": True,
                    "as_of": feed["trade_history"]["as_of"],
                    "completed_trades": feed["trade_history"]["total_completed"],
                }
        except (OSError, ValueError):
            pass
        time.sleep(15)
    raise RuntimeError("Pages publication not verified before deadline")


def refresh_once(settings: PublisherSettings) -> dict:
    with TemporaryDirectory(
        prefix="trade-feed-", dir=settings.publisher_work_dir
    ) as directory:
        bundle = Path(directory) / "private.zip"
        output = Path(directory) / "public.json"
        _run_export(settings, bundle, output)
        feed = validate_public_feed(json.loads(output.read_text(encoding="utf-8")))
        _github_dispatch(feed, settings)
        return _verify_publication(feed)


def _write_receipt(settings: PublisherSettings, receipt: dict) -> None:
    target = settings.publisher_work_dir / "status.json"
    temporary = target.with_suffix(".next")
    temporary.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    temporary.replace(target)


def healthy(settings: PublisherSettings) -> bool:
    try:
        receipt = json.loads(
            (settings.publisher_work_dir / "status.json").read_text(encoding="utf-8")
        )
        age = (
            datetime.now(UTC) - datetime.fromisoformat(receipt["checked_at"])
        ).total_seconds()
        return receipt.get("published") is True and 0 <= age <= 2 * INTERVAL_SECONDS
    except (OSError, ValueError, KeyError, TypeError):
        return False


def publish_loop(settings: PublisherSettings, *, once: bool) -> None:
    while True:
        started = time.monotonic()
        try:
            receipt = refresh_once(settings)
        except Exception as error:
            receipt = {"published": False, "error_type": type(error).__name__}
        receipt = {"checked_at": datetime.now(UTC).isoformat(), **receipt}
        _write_receipt(settings, receipt)
        print(json.dumps(receipt), flush=True)
        if once:
            raise SystemExit(0 if receipt["published"] else 1)
        time.sleep(max(1, INTERVAL_SECONDS - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--health", action="store_true")
    mode.add_argument("--export", nargs=2, type=Path, metavar=("BUNDLE", "OUTPUT"))
    args = parser.parse_args()
    settings = PublisherSettings()
    if args.health:
        raise SystemExit(0 if healthy(settings) else 1)
    if args.export:
        export_feed(settings, *args.export)
        return
    settings.publisher_work_dir.mkdir(parents=True, exist_ok=True)
    with state_ownership(settings.publisher_work_dir / "publisher"):
        _write_receipt(
            settings, {"published": False, "checked_at": datetime.now(UTC).isoformat()}
        )
        publish_loop(settings, once=args.once)


if __name__ == "__main__":
    main()
