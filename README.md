# Cautious Crypto Bro

[![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/Smarandii/badge.svg)](https://hub.xerj.org/contributors/Smarandii)

Telegram text/images/albums → LLM extraction → deterministic Strategy V2 planning →
manual approval or AUTO routing → Bybit Demo → durable position supervision.

**Bybit Demo only.** The LLM interprets posts; code controls sizing, orders and risk.
Use Demo credentials, never real-money API keys.

This is the current operating mode, not a permanent architectural restriction.
Future mainnet support is planned as an explicit opt-in after performance
evaluation, with separate credentials and state. The shared Bybit client accepts
an endpoint; the current executor selects Demo. See [TODO.md](TODO.md).

## Setup

Requires Docker Compose, a Telegram account/bot, OpenRouter or OpenCode Go,
and Bybit Demo Trading. Local development uses Python 3.12 and uv.

```sh
cp .env.example .env
docker compose build app
docker compose run --rm -it --entrypoint /app/.venv/bin/python app scripts/list_telegram_dialogs.py
docker compose up -d app
docker compose logs -f app
```

Fill `.env` with credentials and account/channel IDs from [.env.example](.env.example).
The dialog helper authorizes the Telegram session.

Python and uv images are pinned independently so OS security updates can advance
without waiting for uv's derived image. Dependabot tracks both. The public `os`
build target contains the interpreter and uv, before application source is copied.

Container CI uploads its vulnerability report and SBOM before rejecting runtime
images with fixable high or critical vulnerabilities. Findings without an available
fix do not block this gate.

Git and Docker ignore `.env` and `.env.*` files, including nested locations;
`.env.example` remains a public template. Keep real credentials out of examples.

Default Compose runs code packaged in the image and mounts only `/state` into the
app. Editing the checkout does not change the running app; rebuild/recreate to
apply changes. `CCB_IMAGE` selects an image tag or registry digest, defaulting to
`cautious-crypto-bro:local`. For a versioned local build, use a clean checkout:

```powershell
$env:CCB_IMAGE = "cautious-crypto-bro:$(git rev-parse --short HEAD)"
docker compose build app
docker compose up -d --no-build app
```

Keep that `CCB_IMAGE` value for subsequent Compose commands (or put it in `.env`).
For a published image, pull the chosen tag/digest before `up --no-build`. Preserve
a stopped-writer state backup before upgrades; see [STRATEGY.md](STRATEGY.md) for
schema compatibility and rollback.

[State backup and restore drill](docs/state-backups.md) provides a locked,
offline backup command and an isolated verification workflow for SQLite and the
Telegram session. It leaves live restore/cutover as an explicit operator step.

Run one app per shared SQLite state directory. Startup takes an OS-held lock at
`DATABASE_PATH.owner.lock` before database initialization, migration or quarantine,
and holds it through shutdown. A second cooperating instance exits before touching
state or building exchange clients. The OS releases the lock after exit/crash;
leave the small lock file in place. Stop older versions before upgrading because
they do not acquire this lock. Separate databases/hosts and standalone operator
scripts are outside this guard: never run competing writers for the same account.

Once a locked execution step begins, task cancellation waits for the exchange work
and its durable result or quarantine to finish before releasing the account lock.
The caller still receives cancellation afterward; cancellation does not withdraw
orders. Direct Bybit calls also drain their synchronous worker before unwinding.
This can delay shutdown by the duration of an in-flight request/workflow. Hard
process termination still relies on startup quarantine for unknown outcomes.

Interrupted-execution warnings are queued with quarantine in SQLite and retried
without blocking startup. They survive process restarts and are acknowledged only
after Telegram accepts them. Delivery is at least once: a crash or failed local
acknowledgement can produce a duplicate warning. The schema-10 upgrade may send one
reminder for previously quarantined interrupted executions; see STRATEGY.md for
migration and rollback requirements. Exchange executions are never replayed by
this notification queue.

Original production extraction and normalization decisions survive evaluation-cache
expiry (introduced in SQLite schema 11). Use `scripts/report_source_coverage.py --database PATH
--hours 24` for a read-only aggregate report without Telegram, Bybit or model calls.
Historical missing decisions remain unknown; see STRATEGY.md for evidence privacy,
upgrade and rollback requirements.

To compare available channel history with intake records, explicitly opt in:

```console
python scripts/report_source_coverage.py --database PATH --hours 24 --telegram-history
```

Run with the deployed Telegram configuration and session available. This mode
clones the session into disposable scratch space, disables updates, reads message
metadata and a read-only ledger snapshot, then disconnects and removes the clone.
It does not download media, call models or Bybit, send messages, initialize stores
or replay missing posts. Without `--telegram-history`, the report remains offline.

The publication window ends `--settle-seconds` (default 120) before audit startup.
Whole albums crossing its lower boundary are included; albums with a tail newer
than the window end are excluded until all members settle. `--max-messages`
(default 2000) bounds each channel scan, with one extra item to check its boundary.
A cap, partial boundary album or channel error makes history incomplete; other
channels still report. JSON separates `history_complete` from
`intake_coverage_complete`, and includes missing canonical IDs and status counts
without source content, channel titles or credentials. Keep reports private.
Exit 0 means visible supported intake is complete, exit 2 means gaps/incompleteness,
and exit 1 means the audit failed. Successful intake coverage does not prove
coverage of deleted/inaccessible history or edits, correct model classification,
or successful execution. A recorded pre-model rejection counts as a known
disposition; a completed record without either a current-attempt decision or
recorded rejection remains unknown. Legacy ledgers are inspected without upgrade.

The schema-12 zone ledger preserves independent frozen budgets and fill ownership
as groundwork for multiple POIs. It does not enable multi-zone trading; remaining
protection and execution gates are in `docs/independent-zone-execution.md`.

SIGTERM requests application-wide shutdown, including partial startup. Polling
stops accepting updates; active Telegram handlers and non-abortable exchange work
drain before clients close and state ownership is released. Repeated SIGTERM does
not interrupt this cleanup. Compose allows two minutes before force-killing the
container; unusually long workflows can exceed that window and will still need
startup quarantine. SIGINT keeps Python's normal Ctrl-C behavior.

The source reconciles each channel against history every
`TELEGRAM_CATCHUP_INTERVAL_SECONDS` to recover posts absent from live processing.
Only history scans advance the recovery cursor; newer live messages cannot hide
older missed posts. Startup uses a fixed lookback boundary, finishes albums across
batch boundaries, and retries failed history/image downloads on the next poll.
Source claims deduplicate live and history delivery before extraction/execution.
Each poll also retries up to 100 durable failed or expired-processing posts per
channel, reloading their images even when the history cursor or startup window
has moved past them. Active processing claims remain untouched. Position actions
expire from the earlier of extraction time and original publication time; retrying
an old close/reduce instruction does not make it fresh.

After extraction, temporary planning-read failures retain the original candidate,
even when the post has no other planned output. Bybit read responses 429, 10000,
10006 and 10016 use bounded candidate recovery instead of terminal rejection.
Source completion and candidate persistence commit together; coverage reports the
candidate as unresolved until handled. Failed recovery backs off for five minutes
and never extends the original deadline. Already-selected AUTO/MANUAL decisions
are retained; older candidate payloads without that metadata remain MANUAL.
Order submission errors do not gain automatic replay.

## Telegram reader recovery

Reader outages leave the operator bot and position supervisor running. Network
reconnects retry every 30 seconds; missing authorization is rechecked every five
minutes. The approval chat receives an immediate human-review alert and reminders
every 15 minutes while the outage continues. Failed alert delivery is retried.
Channel history failures show `DEGRADED`; unaffected channels keep processing.

Chronological catch-up progress is saved per channel and survives restarts.
A newer live message cannot skip an earlier unprocessed post. Failed/in-flight
processing keeps the cursor in place; completed posts are not downloaded or
extracted again. First enrollment without a saved cursor checks the last 24 hours
(or the longer configured startup lookback); this does not certify older history.
Recovered posts outside `TELEGRAM_STARTUP_LOOKBACK_HOURS` carry a saved review
reason and require MANUAL approval for entry and lifecycle instructions. Approval
cards label them **RECOVERED POST — HUMAN REVIEW REQUIRED** with publication time.
Fresh-post policy, entry TTL and exits stay unchanged. Schema16 is additive, but
rollback to older readers requires restoring the verified pre-migration backup.

The configured operator can use `/source` in the approval chat or privately for
reader status. Use `/source_login` **in the bot's private chat** to receive a
protected QR image. Scan it from Telegram **Settings → Devices → Link Desktop
Device** using the original reader account. Expired codes refresh up to three
times; the bot deletes each image after use or expiry. QR generation uses Segno,
a small pure-Python dependency, without an image-processing service.

The first valid existing session pins the reader account in
`DATABASE_PATH` with suffix `.telegram-identity.json`. Recovery rejects other
accounts before ingestion. If no account was pinned and no session is authorized,
recovery requires the configured operator account. State backups preserve this
identity record when present; older two-file backups remain verifiable.

If Telegram requires 2FA, the bot escalates to a local procedure. **Never send
phone numbers, login codes or passwords to the bot.** In a local interactive
PowerShell terminal, stop the app and run the helper from its active image:

```powershell
$ccbImage = docker inspect cautious-crypto-bro-app-1 --format '{{.Config.Image}}'
docker stop --timeout 120 cautious-crypto-bro-app-1
docker run --rm -it --env-file .env `
  --env TELEGRAM_SESSION_NAME=/state/cautious_crypto_bro `
  --env DATABASE_PATH=/state/cautious_crypto_bro.sqlite3 `
  --mount type=volume,source=cautious-crypto-bro_app_state,target=/state `
  $ccbImage /app/.venv/bin/python /app/scripts/authorize_telegram.py
docker start cautious-crypto-bro-app-1
```

Adjust container, volume and state paths if your deployment uses different names.
The helper takes the application owner lock, collects credentials through hidden
local prompts, authorizes a separate session and checks the pinned account. It
backs up the original session before installing the validated replacement.
Authentication or identity-check failures leave the original session in place.
After restart, confirm `/source` reports `ONLINE` and inspect supervision health.

## Trading behavior

`AUTO_APPROVAL_MODE` defaults to `disabled`; `open_only` enables eligible OPEN
signals, and `all` also enables eligible REDUCE/CLOSE/CANCEL_ENTRIES actions. Both manual and
AUTO entries reject symbols already controlled by an active strategy or live
exposure/orders. Independent V2 ladders cannot share a net position.
Immediate AUTO results include an account preview fetched after execution,
including work executed during startup recovery. Preview failures do not suppress
the execution result.

AUTO terminal notices are reserved with source work and retry durably after
Telegram failure or restart, even if AUTO trading is later disabled. Delayed
delivery is labeled as a recovered record and reads stored status, source/plan
and saved order references only; it never queries Bybit or executes a trade.
Recovered action notices explicitly mark
exact fill details as unavailable. New terminal records get a 30-second grace
period for immediate delivery, and retries use five-minute claims with a
30-second failure backoff. Delivery is at least once: send acceptance followed by
an acknowledgement failure can duplicate a notice, never its trade. Manual card
and AUTO notice recovery failures are isolated. Pre-upgrade pending/interrupted
work is covered; already-terminal history is not bulk-replayed.

Account previews combine live exposure with stored realized P&L. Only the
background accounting worker fetches closed-P&L history: it starts immediately
and waits 15 minutes between attempts. Signal processing and result previews
never fetch that history. They read the existing ledger and display its last
successful sync time in UTC; the combined figure is labeled `stored + live`.
Before the first successful sync, tracked realized P&L is `not synced`. A failed
history request can leave an older timestamp; a ledger read failure displays
`unavailable`. Neither accounting condition blocks execution or manual cards.

## Behavior

- Telegram intake admits at most two posts at once across live messages, albums,
  catch-up and failed-post retries. Posts wait before album resolution, image
  download and source claiming; each admitted post keeps its complete album and
  runs through dispatch before releasing its slot. Cancellation releases capacity;
  an interrupted source claim still follows the existing stale-lease recovery.
  This bounds active post/model pipelines, not individual image bytes or the
  number of waiting Telethon handlers. Exchange mutations remain serialized.
- Telegram posts have a 20 MiB total raw-image budget, shared across the whole
  album. Downloads use bounded output streams. Exceeding the budget rejects the
  entire post before extraction; no partial images or captions alone reach the
  model. The source ledger records a terminal `COMPLETED` processing disposition
  with the rejection reason in `last_error`, and logs a warning. Later posts keep
  progressing; oversized posts are not retried automatically. Repost smaller
  images as a new post. Transient and empty downloads retain normal retry behavior.
- A retry lookup that finds no Telegram post records `UNAVAILABLE` and defers
  that post for 24 hours, including across restarts. The original payload remains
  in SQLite. Daily checks can recover it if access returns; a verified live
  reappearance can process immediately. Available posts continue normally, and
  transport failures retain ordinary retry behavior. Older application images
  ignore `UNAVAILABLE` rows rather than resuming their one-minute retry loop.
- OPEN needs symbol, direction and explicit MARKET/LIMIT/RANGE semantics.
  Missing stops use a deterministic 2% fallback; missing targets use policy exits.
- Entries start with explicit trader leverage from text/images, with a 10x
  fallback, then apply validated instrument min/max/step constraints without
  increasing the request. The original request and selected plan remain
  inspectable. Fresh entry preparation also validates current/projected-notional
  risk tiers and persists any lower leverage before strategy creation. Submission
  revalidates constraints; changes, unknown tiers, existing exposure or entry
  orders fail before submission. Quantities, stops and exits are not resized.
  Bybit margin/permission failures remain bounded, without blind order replay.
- Profit/holding updates do not authorize new entries. REDUCE/CLOSE require
  explicit current-caption evidence; images can identify symbol/side.
- CANCEL_ENTRIES withdraws earlier pending entries from the same Telegram source
  and symbol. Positions, protection, other sources and newer entries remain intact.
  Ambiguous cancellation stays UNCERTAIN; restart never retries it automatically.
- Plans freeze wallet capital and a default 1% price-risk budget.
  Fees and stop slippage can make realized losses larger.
- MARKET E1 fills before E2/E3 are calculated and submitted.
- Entry ladders expire after `DEMO_ENTRY_ORDER_TTL_MINUTES` when configured.
  Legacy plans stored before that field existed adopt the current value instead
  of owning the symbol forever; zero still disables automatic expiry.
- Pre-submit failures close empty strategies; potentially accepted submissions
  stay quarantined. Paused strategies require deliberate recovery.
- Fixed-exit edits, cancellation, or missing orders pause automation with a
  persisted reason and retryable Telegram alert; existing orders remain intact.

See [STRATEGY.md](STRATEGY.md) for execution rules, [TESTING.md](TESTING.md) for
checks and operations, [CHANGELOG.md](CHANGELOG.md) for release history, and
[TODO.md](TODO.md) for remaining work.

## Contributing

GPL-3.0-only. See [CONTRIBUTING.md](CONTRIBUTING.md) for the quality gate and
conventions, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md), and
[SECURITY.md](SECURITY.md) before reporting anything safety-relevant.
[ACCESSIBILITY.md](ACCESSIBILITY.md) covers the operator-facing surfaces.

`master` is protected: CI must pass, force-pushes and deletion of `master` are
blocked, and merged branches are auto-deleted. Protection applies to `master`
only, so feature branches stay freely pushable and deletable.

## Operator review

In the configured approval chat, the configured approver can send `/attention`
or `/attention 2` to inspect paused (`MANUAL_OVERRIDE`) and `UNCERTAIN` strategies,
plus uncertain entry and position-action executions. Each page contains up to five
records with their type, ID, update time and recorded reason. Already-delivered
pause alerts and legacy strategies without a reason remain visible. Long reasons
are explicitly truncated in the message; the complete reason stays in SQLite.

This reads local durable state, not current exchange state. Reconcile positions,
orders and protection on Bybit before any retry. The command neither resumes
automation nor changes execution statuses or acknowledges pause alerts. Pages are
read afresh, so changing records can move between requests; restart at page 1 if
the list changes during review. An empty list does not prove the exchange is flat.

## Supervision progress

Compose checks the application's successful-supervision marker every 30 seconds,
with a two-minute startup grace period. No successful pass for five minutes, a
missing marker, or an invalid marker produces `unhealthy`. Failed reconciliation
passes do not refresh it; a later successful pass restores health. An empty
strategy ledger is a successful pass. This measures supervision progress, not
Telegram delivery, model availability, account connectivity or whether quarantined
positions need operator review. `unhealthy` alone neither sends an alert nor
restarts the container; attach an external monitor to that status if desired.

For native runs, the same read-only probe is:

```sh
uv run python -m cautious_crypto_bro.supervision_health --database data/cautious_crypto_bro.sqlite3
```

The probe reads only `<database>.supervision-health.json`; it never opens SQLite
or calls an API. Success timestamps use Python's system-wide
[monotonic clock](https://docs.python.org/3.12/library/time.html#time.monotonic).
The application clears old progress after taking state ownership and removes it
after resource cleanup. Progress-file write failures are logged without stopping
protection work; the probe eventually becomes unhealthy if publishing remains
broken. The sidecar is ephemeral and excluded from the canonical state backup.

## Development

For source-mounted Docker development, explicitly select the development file:

```sh
docker compose stop app
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d --build app
```

This uses `cautious-crypto-bro:dev` and read-only mounts for `src` and `scripts`.
It replaces the app in the same Compose project and reuses its state/account;
do not run an independent competing project against the same Bybit account.
Return to the default file and chosen `CCB_IMAGE` to deploy packaged code again.
The test profile retains its existing explicit test mounts.

### Exchange startup outages

The bot polls while startup reconciliation is pending. Authorized operators can
use `/status`, `/attention` and `/source`; entry/action approval and skip buttons
remain blocked until both reconciliation passes and AUTO recovery finish.
Signal ingestion starts only after that gate succeeds.

Bybit reconciliation errors retry after 30 seconds, increasing to at most five
minutes between attempts. The operator receives an outage notice (repeated at
most every 15 minutes after successful delivery) and one recovery notice. Failed
notice delivery retries with the next reconciliation attempt. Unavailable
exchange access means application protection is **not verified**; inspect existing
positions and exchange protection through Bybit when possible. `/status` reports
startup readiness, observed runtime supervision failures, and whether local records
require human review. Use `/attention` in the approval chat for stored reasons;
an empty local review queue does not verify live exchange protection. Storage and unexpected
programming errors still fail startup; they are not retried as exchange outages.

After startup, unavailable shared account snapshots retry from 30 seconds up to
five minutes between attempts. Isolated strategy failures retain the normal
protection cadence so healthy symbols continue receiving supervision. Runtime
failures appear in `/status` and queue a human-review notice, repeated at most
every 15 minutes after successful delivery. Failed notice delivery retries every
30 seconds; recovery queues one notice after a successful reconciliation pass.
All supervision sends are at least 30 seconds apart, coalescing rapid state changes.
The existing bot alert worker sends these notifications independently of position
protection, including when Telegram delivery is slow. Existing execution preflights
continue to decide entry/action admissibility during runtime outages.

For Bybit `10010 Unmatched IP`, correct the Demo key's IP binding or use an
authorized connection matching it. Mobile connections may change public IP;
restarting the app cannot repair an exchange-side IP allowlist. Never send API
keys or secrets to the bot.
IP-binding outage alerts look up the running app's current public IP through
`https://api.ipify.org` at delivery time and include it beside the API-management
link. Lookup has a five-second timeout; failures or invalid/non-public responses
are reported explicitly, and the outage alert still sends. Recovery and unrelated
notices do not perform this lookup.

### Native development

```sh
uv sync --python 3.12 --group dev
uv run pre-commit install
uv run pytest -q
```

The local Docker lint hook requires Docker and streams only the selected
Dockerfiles to a container with no network or host mounts. CI also checks the
repository's Docker build context and builds the runtime image.

Source distributions use explicit public paths in `pyproject.toml`; add new
top-level distribution files there deliberately. Private state and credentials
must stay outside the included source, scripts, tests and docs directories.
CI builds with synthetic local-file canaries, checks archive contents and imports
the installed wheel from an isolated environment outside the checkout.

SQLite and Telegram sessions persist in Docker volume `app_state`; Redis uses
`redis_state`. Audit exports and backups stay local and outside build context.

The optional Compose `publisher` service refreshes the public Demo trade feed
every 15 minutes. It uses read-only state and selected credential mounts, private
temporary storage and its own healthcheck; no Windows scheduled task or Docker
socket is required. See [trade-feed setup and rollback](docs/TRADE_FEED.md).
