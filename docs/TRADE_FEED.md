# Public Demo trade feed

The dashboard's trade table refreshes `trades.json` every minute. A local publisher
exports read-only Bybit Demo evidence and publishes it through the existing Pages
workflow every 15 minutes. Exchange coverage older than 30 minutes is marked stale.
The aggregate scorecard retains its own snapshot timestamp; it is not the live
trade feed and its historical risk must not be read as today's configuration.

## Run on the local runtime host

After deploying the release containing these scripts:

```powershell
python scripts/publish_trade_feed.py --once
python scripts/publish_trade_feed.py
```

Use the project's Python environment. Docker must be accessible; Windows uses
the default WSL distribution's Docker CLI. The application container must be
`cautious-crypto-bro-app-1`. Git Credential Manager must already hold a GitHub
credential with Actions write access for this repository. Never put a token in
the command, public files, workflow input or browser.

The continuous process owns a separate publisher lock, exports at most once per
15 minutes, and prints only a public success receipt or a failure type. Each
container export is limited to four minutes, and publication is verified against
the public feed before success is reported. Failed cycles retry at the next
interval; the old feed visibly ages. Run this process under the local host's
process supervisor for restart recovery. No runtime scheduler is installed merely
by merging the code; deployment must install and verify it explicitly.

## Public contract and privacy

Only the latest 100 complete, attributed whole trades are exported. Total and
omitted counts are explicit. Partial close rows, incomplete positions and probe
orders do not become separate trade rows. Each row contains symbol, side, filled
entry quantity, net USDT PnL, net R, UTC close time and frozen risk percentage.
Current base risk, LONG multiplier, momentum gate, skip fraction, portfolio cap
and exit profile are separate fields. Unknown metrics stay unknown.

`dashboard_feed.py` validates an exact field whitelist, finite numbers, bounded
counts, timestamp syntax and known profiles. Raw source text, channel/post IDs,
intent/order/execution IDs, credentials and private archives are excluded.
The local producer leaves credentials inside the runtime container and performs
no exchange mutation. Private temporary archives are removed after each cycle.
PnL-only exports skip market candles; research exports retain their default.

The Pages workflow accepts only validated public JSON, without shell interpolation
of its input. Source-only rebuilds attempt to preserve the last published feed.
The local publisher verifies the resulting public bytes as structured JSON; an
accepted dispatch alone is not publication success.

## Rollback

Stop the local publisher and redeploy the prior application image if necessary.
There is no database migration or trading-policy change. An older Pages version
can be rebuilt from its source revision; retain the feed timestamp so old evidence
cannot appear current. No historical trade is relabeled with new sizing settings.

These observations do not prove profitability or satisfy #248's natural live
trade-magnitude requirement by themselves.
