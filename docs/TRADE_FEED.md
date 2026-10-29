# Public Demo trade feed

The dashboard refreshes `trades.json` every minute. The Compose `publisher`
service exports read-only Bybit Demo evidence and publishes through the existing
Pages workflow every 15 minutes. Exchange coverage older than 30 minutes is stale.
Historical aggregates retain their own timestamp and never override current risk.

## Compose service

Use the same verified runtime image as the app. Provision the private
`cautious-crypto-bro_publisher_secrets` Docker volume out of band with three UTF-8
files, readable only by the service: `github_token`, `bybit_api_key` and
`bybit_api_secret`. The GitHub token needs Actions write permission for this
repository. Use Bybit Demo credentials; the exporter makes read requests only.
Do not put credential values in commands, source, images, logs or workflow input.
Do not share the app's full `.env` with this service.

For first-time provisioning, this foreground command prompts without echoing
values and writes only to the private Docker volume. Set `CCB_IMAGE` to the
verified runtime image first; it is a one-time setup, not a background host job.

```sh
docker volume create cautious-crypto-bro_publisher_secrets
docker run --rm -it --network none \
  --mount type=volume,source=cautious-crypto-bro_publisher_secrets,target=/run/secrets \
  "$CCB_IMAGE" /app/.venv/bin/python -c '
import getpass, os
from pathlib import Path
root = Path("/run/secrets")
root.chmod(0o700)
for name in ("github_token", "bybit_api_key", "bybit_api_secret"):
    value = getpass.getpass(name + ": ").strip()
    if not value:
        raise SystemExit("Empty credential refused")
    target = root / name
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value + "\n")
    target.chmod(0o600)
'
```

```sh
docker compose --profile publisher up -d publisher
docker compose --profile publisher ps publisher
docker compose logs --tail 10 publisher
docker compose exec publisher /app/.venv/bin/python /app/scripts/publish_trade_feed.py --health
```

The profile is opt-in so other deployments do not unexpectedly publish. Once
started, `restart: unless-stopped` restores the service after Docker restarts.
No host process, Windows scheduled task, WSL/Git command or Docker socket is used
by the running publisher. It does not expose a port or accept trading commands.

`app_state` is mounted read-only at `/state`; the code opens only SQLite in
read-only mode. This shared volume also contains existing app state files; it is
not a per-file isolation boundary. Selected publisher credentials are mounted
read-only at `/run/secrets`. Temporary forensic archives, the singleton lock and
atomic health receipt live in a separate private `publisher_work` volume.
The container root filesystem is read-only and all capabilities are dropped.
Compose shares the explicit Demo-policy environment with the app; it does not
copy Telegram or LLM credentials into the publisher environment.

The existing exporters run in one bounded Python child inside the publisher
container, with a 270-second timeout. GitHub dispatch has a 30-second timeout;
public bytes must match the validated feed within the publication deadline before
a cycle succeeds. Private temporary files are removed on normal success/failure;
an interrupted container may leave an abandoned private work directory.
Each completed cycle logs only a public receipt or failure type. Failed cycles
retry at the next interval; the old public feed visibly ages.

Health is unavailable at startup, false after a failed cycle and false when the
last verified receipt is over 30 minutes old. A dispatch acceptance alone is not
publication success. A singleton lock on the shared work volume prevents two
publisher containers from exporting concurrently.

## Migration from the Windows task

Wait for any existing scheduled run to finish, then disable
`CautiousCryptoBro-PublicTradeFeed` before starting the container. Keep the old
launcher available for rollback. Verify a real cycle reports `published: true`,
the public feed matches it, and container health passes. Then unregister the
Windows task and retire its launcher. Never run the two publishers concurrently.

## Public contract and privacy

Only the latest 100 complete, attributed whole trades are exported. Total and
omitted counts are explicit. Partial close rows, incomplete positions and probe
orders do not become separate trades. Rows contain symbol, side, filled entry
quantity, net USDT PnL, net R, UTC close time and frozen risk percentage.
Current base risk, LONG multiplier, momentum gate, skip fraction, portfolio cap
and exit profile are separate fields. Unknown metrics remain unknown.

`dashboard_feed.py` validates an exact field whitelist, finite numbers, bounded
counts, timestamp syntax and known profiles. Raw source text, channel/post IDs,
intent/order/execution IDs, credentials and private archives are excluded.
PnL-only exports skip market candles; research exports retain their default.
The Pages workflow accepts only validated public JSON, without shell interpolation.
Source-only rebuilds preserve the last published feed when available.

## Rollback

Stop the publisher service before enabling the previous verified scheduling
method. If needed, restore the prior schema-15 image; no database migration or
trading-policy change is introduced here. Keep the credential/work volumes
private and retain the feed timestamp so old evidence cannot appear current.
No historical trade is relabeled with new sizing settings.

These observations do not prove profitability or satisfy #248's natural
trade-magnitude requirement by themselves.
