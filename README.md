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

The source reconciles each channel against history every
`TELEGRAM_CATCHUP_INTERVAL_SECONDS` to recover posts absent from live processing.
Only history scans advance the recovery cursor; newer live messages cannot hide
older missed posts. Startup uses a fixed lookback boundary, finishes albums across
batch boundaries, and retries failed history/image downloads on the next poll.
Source claims deduplicate live and history delivery before extraction/execution.

`AUTO_APPROVAL_MODE` defaults to `disabled`; `open_only` enables eligible OPEN
signals, and `all` also enables eligible REDUCE/CLOSE/CANCEL_ENTRIES actions. Both manual and
AUTO entries reject symbols already controlled by an active strategy or live
exposure/orders. Independent V2 ladders cannot share a net position.
Every AUTO result includes an account preview fetched after execution, including
recovered actions. Preview failures do not suppress the execution result.

## Behavior

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

## Development

```sh
uv sync --python 3.12 --group dev
uv run pre-commit install
uv run pytest -q
```

SQLite and Telegram sessions persist in Docker volume `app_state`; Redis uses
`redis_state`. Audit exports and backups stay local and outside build context.
