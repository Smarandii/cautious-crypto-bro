"""Refresh public Demo trades locally every 15 minutes, without repository commits.

Run on the runtime host. Bybit credentials stay in the application container;
GitHub authentication comes from Git Credential Manager on this host.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cautious_crypto_bro.dashboard_feed import validate_public_feed  # noqa: E402
from cautious_crypto_bro.state_ownership import state_ownership  # noqa: E402

REPOSITORY = "Smarandii/cautious-crypto-bro"
PUBLIC_URL = "https://smarandii.github.io/cautious-crypto-bro/trades.json"


def _run(
    command: list[str], *, input_text: str | None = None, timeout: int = 270
) -> str:
    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        env=os.environ | {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )
    if result.returncode:
        raise RuntimeError("Local feed command failed; private diagnostics withheld")
    return result.stdout


def _github_dispatch(feed: dict) -> None:
    credentials = _run(
        ["git", "credential", "fill"],
        input_text="protocol=https\nhost=github.com\n\n",
        timeout=30,
    )
    fields = dict(
        line.split("=", 1) for line in credentials.splitlines() if "=" in line
    )
    token = fields.get("password")
    if not token:
        raise RuntimeError("Git Credential Manager has no GitHub credential")
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


def refresh_once(container: str = "cautious-crypto-bro-app-1") -> dict:
    docker = ["wsl.exe", "--exec", "docker"] if os.name == "nt" else ["docker"]
    prefix = "/tmp/ccb-trade-feed-" + uuid4().hex
    bundle, output = prefix + ".zip", prefix + ".json"
    try:
        # GNU timeout also bounds work inside the container if host observation fails.
        for script, args in [
            (
                "export_forensic_replay.py",
                ["--output", bundle, "--skip-market-candles"],
            ),
            ("export_trade_feed.py", [bundle, output]),
        ]:
            _run(
                docker
                + [
                    "exec",
                    container,
                    "timeout",
                    "--kill-after=10",
                    "240",
                    "/app/.venv/bin/python",
                    "/app/scripts/" + script,
                    *args,
                ]
            )
        raw = _run(
            docker
            + [
                "exec",
                container,
                "/app/.venv/bin/python",
                "-c",
                "import pathlib,sys;sys.stdout.write(pathlib.Path(sys.argv[1]).read_text())",
                output,
            ],
            timeout=30,
        )
        feed = validate_public_feed(json.loads(raw))
        _github_dispatch(feed)
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
    finally:
        _run(
            docker
            + [
                "exec",
                container,
                "/app/.venv/bin/python",
                "-c",
                "import pathlib,sys;[pathlib.Path(p).unlink(missing_ok=True) for p in sys.argv[1:]]",
                bundle,
                output,
            ],
            timeout=30,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    with state_ownership(ROOT / "audit-artifacts" / "trade-feed-publisher"):
        publish_loop(once=args.once)


def publish_loop(*, once: bool) -> None:
    while True:
        started = time.monotonic()
        try:
            receipt = refresh_once()
        except Exception as error:
            # Never print command output, credentials, API response bodies or lineage.
            receipt = {"published": False, "error_type": type(error).__name__}
        print(
            json.dumps({"checked_at": datetime.now(UTC).isoformat(), **receipt}),
            flush=True,
        )
        if once:
            raise SystemExit(0 if receipt["published"] else 1)
        time.sleep(max(1, 900 - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
