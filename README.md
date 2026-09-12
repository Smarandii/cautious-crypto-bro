# Cautious Crypto Bro

Telegram text/images → OpenRouter → `TradingIntent` → human approval → Bybit Demo.

## Run

```bash
cp .env.example .env
# Fill .env; TELEGRAM_SOURCE_CHANNELS is a JSON array.

docker compose run --rm app
docker compose up -d --build app
docker compose logs -f app
```

## Verify

```bash
docker compose --profile test run --rm --build test
docker compose run --rm -T --entrypoint /app/.venv/bin/python app scripts/smoke_bybit_trade.py --execute
```

The smoke command creates a real Demo order. Runtime state lives in the `app_state` Docker volume.

## Invariants

- Bybit Demo only.
- The LLM produces intents; only deterministic code places orders.
- Missing or ambiguous trade parameters are rejected.
