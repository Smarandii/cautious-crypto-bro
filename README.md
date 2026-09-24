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

`AUTO_APPROVAL_MODE` defaults to `disabled`; `open_only` enables eligible OPEN
signals, and `all` also enables eligible REDUCE/CLOSE/CANCEL_ENTRIES actions. Both manual and
AUTO entries reject symbols already controlled by an active strategy or live
exposure/orders. Independent V2 ladders cannot share a net position.

## Behavior

- OPEN needs symbol, direction and explicit MARKET/LIMIT/RANGE semantics.
  Missing stops use a deterministic 2% fallback; missing targets use policy exits.
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
checks and operations, and [TODO.md](TODO.md) for remaining work.

## Development

```sh
uv sync --python 3.12 --group dev
uv run pre-commit install
uv run pytest -q
```

SQLite and Telegram sessions persist in Docker volume `app_state`; Redis uses
`redis_state`. Audit exports and backups stay local and outside build context.
