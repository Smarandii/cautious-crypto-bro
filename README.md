# Cautious Crypto Bro

Telegram text/images/albums → LLM extraction → deterministic Strategy V2 planning →
manual approval or AUTO routing → Bybit Demo → durable position supervision.

**Bybit Demo only.** The LLM interprets posts; code controls sizing, orders and risk.
Use Demo credentials, never real-money API keys.

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

`AUTO_APPROVAL_MODE` defaults to `disabled`; `open_only` enables eligible OPEN
signals, and `all` also enables eligible REDUCE/CLOSE/CANCEL_ENTRIES actions. Both manual and
AUTO entries reject symbols already controlled by an active strategy or live
exposure/orders. Independent V2 ladders cannot share a net position.
Every AUTO result includes an account preview fetched after execution, including
recovered actions. Preview failures do not suppress the execution result.

## Behavior

- Telegram posts have a 20 MiB total raw-image budget, shared across the whole
  album. Downloads use bounded output streams. Exceeding the budget rejects the
  entire post before extraction; no partial images or captions alone reach the
  model. The source ledger records a terminal `COMPLETED` processing disposition
  with the rejection reason in `last_error`, and logs a warning. Later posts keep
  progressing; oversized posts are not retried automatically. Repost smaller
  images as a new post. Transient and empty downloads retain normal retry behavior.
- OPEN needs symbol, direction and explicit MARKET/LIMIT/RANGE semantics.
  Missing stops use a deterministic 2% fallback; missing targets use policy exits.
- Entries copy explicit trader leverage from text/images, with a 10x fallback.
  Bybit must accept the leverage before entry orders are submitted.
- Profit/holding updates do not authorize new entries. REDUCE/CLOSE require
  explicit current-caption evidence; images can identify symbol/side.
- CANCEL_ENTRIES withdraws earlier pending entries from the same Telegram source
  and symbol. Positions, protection, other sources and newer entries remain intact.
  Ambiguous cancellation stays UNCERTAIN; restart never retries it automatically.
- Plans freeze wallet capital and a default 1% price-risk budget.
  Fees and stop slippage can make realized losses larger.
- MARKET E1 fills before E2/E3 are calculated and submitted.
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

For native development:

```sh
uv sync --python 3.12 --group dev
uv run pre-commit install
uv run pytest -q
```

Source distributions use explicit public paths in `pyproject.toml`; add new
top-level distribution files there deliberately. Private state and credentials
must stay outside the included source, scripts, tests and docs directories.
CI builds with synthetic local-file canaries, checks archive contents and imports
the installed wheel from an isolated environment outside the checkout.

SQLite and Telegram sessions persist in Docker volume `app_state`; Redis uses
`redis_state`. Audit exports and backups stay local and outside build context.
