# Testing and development commands

Run commands from the repository root.

## Python-in-app helper

Most utility scripts need the app environment, `.env`, persistent SQLite
database and Telegram session. For a shell session, define:

```bash
pyapp() {
  docker compose run --rm -T \
    --entrypoint /app/.venv/bin/python \
    app "$@"
}

Then commands such as:

pyapp scripts/set_execution_policy.py

are equivalent to the longer docker compose run ... form.

Unit tests
docker compose --profile test run --rm --build test

Before committing:

git diff --check
git status --short
Replay a historical Telegram post

Fetch the original Telegram message and image, apply the current
global/channel guidance, run the production OpenRouter extractor, and build
an execution plan:

pyapp scripts/replay_telegram_post.py \
  'https://t.me/c/2243423111/7905'

Only inspect the generated TradingIntent:

pyapp scripts/replay_telegram_post.py \
  'https://t.me/c/2243423111/7905' \
  --intent-only

Send the replayed intent through the normal approval flow:

pyapp scripts/replay_telegram_post.py \
  '<telegram-post-url>' \
  --send-approval

--send-approval persists a fresh intent/plan and sends the normal Telegram
approval card. Pressing Execute on that card places real Bybit Demo
orders through the normal application path.

Replay itself does not mark the source Telegram message as seen, so the same
URL can be tested repeatedly.

Execution planning always uses current Bybit market/instrument data.
Therefore an old MARKET signal can extract successfully but fail planning
because the market has moved. Use --intent-only when testing historical
signal interpretation only.

Execution policy

Show current policy:

pyapp scripts/set_execution_policy.py

Example update:

pyapp scripts/set_execution_policy.py \
  --capital-usdt 6800 \
  --risk-pct 1 \
  --range-orders 3 \
  --minimum-reward-bps 20 \
  --basic-r 0.5 \
  --basic-close-pct 25 \
  --medium-r 1 \
  --medium-close-pct 35 \
  --high-r 2 \
  --high-close-pct 40
Signal guidance

Global guidance:

cat <<'EOF' | pyapp scripts/set_guidance.py --global
Guidance text here.
EOF

Channel-specific guidance:

cat <<'EOF' | pyapp scripts/set_guidance.py \
  --channel -1002132062264
Channel guidance here.
EOF
Local image → OpenRouter

For an image stored inside the repository:

docker compose run --rm -T \
  -v "$PWD:/workspace:ro" \
  --entrypoint /app/.venv/bin/python \
  app \
  scripts/smoke_openrouter_image.py \
  /workspace/path/to/image.png \
  --caption 'caption text' \
  --channel-id -1002132062264
Synthetic Bybit Demo smoke test

Dry run:

pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE \
  --side LONG \
  --omit-tp

Cancel existing Demo orders for the symbol and submit fresh Demo orders:

pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE \
  --side LONG \
  --omit-tp \
  --cancel-existing \
  --execute

--execute places actual orders on Bybit Demo, never mainnet.

App lifecycle

Build/recreate the running app:

docker compose up -d --build --force-recreate app

Logs:

docker compose logs -f app

Build the app image without restarting the live container:

docker compose build app

The app_state Docker volume contains the Telegram login session and SQLite
runtime state. Do not use docker compose down -v unless you intentionally
want to delete that state.

## Telegram startup lookback

By default the app scans the previous 5 hours of every configured Telegram
source when it starts:

```text
TELEGRAM_STARTUP_LOOKBACK_HOURS=5

Set it to 0 to disable historical scanning.

Live Telegram updates are registered before the historical scan begins, so
messages arriving during startup are still captured.

Historical messages go through the normal SignalService pipeline. The
persistent (channel_id, message_id) source dedup means a Telegram message
already processed before restart is not extracted, planned or approved again.

Lookback messages are processed oldest-first.

The lookback does not automatically execute trades. Any actionable historical
signal still creates the normal human approval card and requires Execute to be
pressed.
