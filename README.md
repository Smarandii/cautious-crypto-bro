# Cautious Crypto Bro

Live Telegram trading ideas → structured `TradingIntent` → human approval → Bybit Demo order.

## Run

```bash
cp .env.example .env
# Fill .env. TELEGRAM_SOURCE_CHANNELS is a JSON array, e.g. [-1002132062264]

docker compose build
docker compose --profile test run --rm test
docker compose run --rm app   # first Telegram login / foreground run
```

After login:

```bash
docker compose up -d app
docker compose logs -f app
```

Runtime state (Telethon session + SQLite) lives in the `app_state` Docker volume.

## Invariants

- Demo trading only.
- The LLM produces intents; only deterministic code places orders.
- Missing or ambiguous trade parameters are rejected rather than invented.
- Required intent fields: symbol, side, entry, stop loss, take profit.
