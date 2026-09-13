# Cautious Crypto Bro

Human-in-the-loop crypto signal execution from Telegram to Bybit Demo.

```text
Telegram text / images / albums
        ↓
OpenRouter signal extraction
        ↓
deterministic risk + execution plan
        ↓
account snapshot + Telegram approval
        ↓
Bybit Demo

The LLM interprets trader posts. Deterministic code controls sizing, entries,
stops and fallback take-profit ladders. Nothing is executed without human
approval.

This project currently supports Bybit Demo only. Do not use production API
keys.

Quick start

Requirements:

Docker + Docker Compose
a Telegram account
an OpenRouter account
a Bybit account

Clone the repo, then:

cp .env.example .env
1. Get the required credentials

Fill .env with:

Telegram API ID + hash — create a Telegram developer application at
my.telegram.org.
Telegram bot token — create a bot with @BotFather.
Telegram approver user/chat IDs — the user and chat that will receive and
approve trades.
OpenRouter API key — create one in your OpenRouter account.
Bybit Demo API key + secret — create a Bybit account, enable Demo Trading,
then create API credentials for the demo environment.

If you need a Bybit account, this is my referral link:
https://www.bybit.com/invite?ref=Y5B5E38

It is a referral link.

2. Authorize Telegram and find source channel IDs

Build the app:

docker compose build app

Run the Telegram dialog helper interactively:

docker compose run --rm -it \
  --entrypoint /app/.venv/bin/python \
  app scripts/list_telegram_dialogs.py

On first run, Telegram will ask you to authorize the user session. The helper
then prints your available chats/channels and their IDs.

Put the channels you want to follow in .env:

TELEGRAM_SOURCE_CHANNELS=[-1001234567890,-1009876543210]
3. Start
docker compose up -d app
docker compose logs -f app

The app scans the configured startup lookback window and then listens for live
Telegram posts.

When it finds an executable signal it sends:

current Bybit account state;
the extracted trade and deterministic execution plan;
Execute / Skip buttons.
Development

Install uv, then:

uv sync --group dev
uv run pre-commit install

Quality checks:

uv run ruff format .
uv run ruff check .
uv run pytest

The same checks run in GitHub Actions.

Useful operational and replay commands are in TESTING.md.
Current roadmap: TODO.md.

Safety model
Bybit Demo only.
Human approval is mandatory.
LLM output never directly places an order.
Position sizing and execution are deterministic.
Missing execution-critical values are never invented.
