# Cautious Crypto Bro

Human-in-the-loop crypto signal execution:

```text
Telegram text / images / albums
        ↓
OpenRouter → TradingIntent
        ↓
deterministic ExecutionPlan
        ↓
Telegram approval
        ↓
Bybit Demo
```

Supports multiple Telegram sources, album reconstruction, startup lookback,
persistent source dedup, trader guidance, risk-based sizing and fallback
take-profit ladders.

## Run

```bash
cp .env.example .env
# Fill .env. TELEGRAM_SOURCE_CHANNELS is a JSON array.

docker compose up -d --build app
docker compose logs -f app
```

Runtime state lives in the persistent `app_state` Docker volume.

## Verify

```bash
docker compose --profile test run --rm --build test
```

See [TESTING.md](TESTING.md) for replay and smoke-test commands.
See [TODO.md](TODO.md) for outstanding work.

## Invariants

- Bybit Demo only.
- The LLM interprets signals; deterministic code builds and executes plans.
- Human approval is required before execution.
- Missing or unreliable required trading values are rejected rather than guessed.
