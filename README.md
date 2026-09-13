# Cautious Crypto Bro

Telegram text/images → OpenRouter → `TradingIntent` → deterministic execution
plan → human approval → Bybit Demo.

## Run

```bash
cp .env.example .env
# Fill .env; TELEGRAM_SOURCE_CHANNELS is a JSON array.

docker compose up -d --build app
docker compose logs -f app

Runtime state lives in the persistent app_state Docker volume.

Verify
docker compose --profile test run --rm --build test

For historical Telegram replay, OpenRouter/image tests, execution-policy
commands and Bybit Demo smoke tests, see TESTING.md.

Invariants
Bybit Demo only.
The LLM interprets signals; deterministic code creates execution plans and
places orders.
Human approval is required before a generated trading intent is executed.
Missing or ambiguous required signal information is rejected rather than
guessed.
