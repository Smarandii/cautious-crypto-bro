# Cautious Crypto Bro

Weekend MVP: watch selected Telegram trader channels, turn live text/captions into structured trading intents, show those intents in an approval Telegram bot, and place a Bybit **Demo Trading** order only after explicit approval.

## MVP scope

Implemented:

- live Telegram channel ingestion via a dedicated Telegram user session
- text/caption signal interpretation through OpenRouter
- strict Pydantic `TradingIntent` validation
- Telegram approval cards with `Execute` / `Skip`
- only the configured Telegram user can approve trades
- Bybit Demo Trading execution
- fixed configurable USDT notional sizing
- quantity rounding to Bybit instrument rules
- SL/TP passed with the order
- SQLite persistence for source messages, intents, decisions, and execution result
- stale-intent check before execution
- idempotent state transition so the same intent cannot be executed twice

Deliberately not implemented yet:

- image / voice / video understanding
- multi-message idea-thread reconstruction
- edits to an existing trade
- multiple take-profits / scaling
- live position monitoring and PnL reporting
- historical backtesting

## Runtime model

The application is Docker-first. You do not need a project-local Python installation or `.venv`.

- `uv` resolves and installs Python dependencies inside Docker.
- `uv.lock` is committed for reproducible builds.
- Telegram session state and SQLite live in the Docker named volume `app_state`.
- `.env` is the only local runtime configuration file and is gitignored.

## Initial setup

Create your local environment file:

```bash
cp .env.example .env
```

Fill in `.env` with Telegram, OpenRouter, and Bybit Demo credentials.

Generate the lock file using the uv Docker image, so uv does not need to be installed on the host:

```bash
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "$PWD:/workspace" \
  -w /workspace \
  ghcr.io/astral-sh/uv:0.10.0-python3.12-bookworm-slim \
  uv lock
```

Build and test:

```bash
docker compose build
docker compose --profile test run --rm test
```

## First Telegram login

Telethon needs one interactive login for the dedicated Telegram account. Run:

```bash
docker compose run --rm app
```

Enter the Telegram phone/code/password when prompted. The resulting session is saved in the Docker named volume, so subsequent containers reuse it.

Stop the foreground process after authentication, then start normally:

```bash
docker compose up -d app
docker compose logs -f app
```

## Useful commands

Run tests:

```bash
docker compose --profile test run --rm test
```

Rebuild after dependency or source changes:

```bash
docker compose build app
```

Run in foreground:

```bash
docker compose up app
```

Stop the application without deleting state:

```bash
docker compose down
```

Delete all local application state, including the Telegram session and SQLite database:

```bash
docker compose down -v
```

## Trading behavior

The LLM is not allowed to place orders. It may only emit a `TradingIntent`.

An intent must contain:

- `symbol`
- `side`
- `entry`
- `stop_loss`
- `take_profit`
- `summary`
- confidence

If the model decides the post is not an actionable trade, or cannot identify required trade parameters without inventing them, no approval card is created.

`Execute` performs deterministic validation and then calls Bybit Demo Trading.

## Important

This repository intentionally defaults to Bybit Demo Trading through `pybit(..., demo=True)`. Do not change this to live trading until the execution and validation layers have been deliberately redesigned and reviewed.
