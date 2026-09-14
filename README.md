# Cautious Crypto Bro

Human-in-the-loop crypto signal execution from Telegram to Bybit Demo.

```text
Telegram text / images / albums
        ↓
OpenRouter signal extraction
        ↓
OPEN / REDUCE / CLOSE normalization
        ↓
deterministic risk + lifecycle execution
        ↓
account snapshot + Telegram approval
        ↓
Bybit Demo
```

The LLM interprets trader posts. Deterministic code controls sizing, entries,
stops, fallback take-profit ladders, lifecycle authorization, and reduction
percentages. Nothing is executed without human approval.

REDUCE and CLOSE are account-wide actions on the current Bybit position for the
symbol. They are not attributed to a specific source channel. A destructive
action must be explicitly authorized by the current Telegram text/caption;
images may identify the symbol or side but cannot authorize a close by
themselves.

> This project currently supports Bybit Demo only. Do not use production API
> keys.

## Quick start

### Requirements

- Docker + Docker Compose
- a Telegram account
- an OpenRouter account
- a Bybit account

Clone the repository, then create your local environment file:

```bash
cp .env.example .env
```

### 1. Get the required credentials

Fill `.env` with:

- **Telegram API ID + hash** — create a Telegram developer application at
  `my.telegram.org`.
- **Telegram bot token** — create a bot with `@BotFather`.
- **Telegram approver user/chat IDs** — the user and chat that will receive and
  approve trades.
- **OpenRouter API key** — create one in your OpenRouter account.
- **Bybit Demo API key + secret** — create a Bybit account, enable Demo Trading,
  then create API credentials for the demo environment.

If you need a Bybit account, this is my referral link:

https://www.bybit.com/invite?ref=Y5B5E38

### 2. Authorize Telegram and find source channel IDs

Build the app:

```bash
docker compose build app
```

Run the Telegram dialog helper interactively:

```bash
docker compose run --rm -it \
  --entrypoint /app/.venv/bin/python \
  app scripts/list_telegram_dialogs.py
```

On first run, Telegram will ask you to authorize the user session. The helper
then prints your available chats/channels and their IDs.

Add the channels you want to follow to `.env`:

```env
TELEGRAM_SOURCE_CHANNELS=[-1001234567890,-1009876543210]
```

### 3. Start the app

```bash
docker compose up -d app
docker compose logs -f app
```

The app scans the configured startup lookback window and then listens for live
Telegram posts.

When it finds an executable signal, it sends:

1. current Bybit account state;
2. either an OPEN trade with its deterministic execution plan or an account-wide
   REDUCE/CLOSE position action;
3. **Execute** / **Skip** buttons.

## Development

Install `uv`, then install the development environment and Git hook:

```bash
uv sync --python 3.12 --group dev
uv run pre-commit install
```

Run the same main quality checks used by CI:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest
```

GitHub Actions also validates the Dockerfile and runtime image on pushes and
pull requests.

Useful replay, audit, diagnostics, and smoke-test commands are in
[TESTING.md](TESTING.md).

Current roadmap: [TODO.md](TODO.md).

## Safety model

- Bybit Demo only.
- Human approval is mandatory.
- LLM output never directly places an order.
- Position sizing and execution are deterministic.
- Missing execution-critical values are never invented.
- REDUCE/CLOSE require explicit evidence in the current Telegram text/caption.
- Images may supply symbol/side context but cannot independently authorize a
  destructive position action.
- REDUCE percentages are derived deterministically from the trader's caption
  evidence instead of trusting the model's numeric convention.
- Lifecycle execution uses reduce-only orders and cancels stale CCB-created
  entry orders for the symbol before reducing or closing the current position.
