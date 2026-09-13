# Testing

Run commands from the repository root.

## Helper

```bash
pyapp() {
  docker compose run --rm -T \
    --entrypoint /app/.venv/bin/python \
    app "$@"
}
```

## Tests

```bash
docker compose --profile test run --rm --build test
git diff --check
git status --short
```

## Telegram replay

Replay uses the original Telegram text/images, reconstructs albums, applies
current guidance and runs the production extractor.

```bash
# Extract and plan
pyapp scripts/replay_telegram_post.py '<telegram-post-url>'

# Extraction only
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --intent-only

# Send normal approval card
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --send-approval
```

Replay does not mark the source as seen. Planning uses current Bybit market
data, so old signals may extract correctly but fail current planning.

## Configuration

```bash
# Execution policy
pyapp scripts/set_execution_policy.py
pyapp scripts/set_execution_policy.py --help

# Global guidance
cat <<'EOF2' | pyapp scripts/set_guidance.py --global
Guidance text here.
EOF2

# Channel guidance
cat <<'EOF2' | pyapp scripts/set_guidance.py --channel -1002132062264
Guidance text here.
EOF2
```

## Smoke tests

```bash
# Local image → OpenRouter
docker compose run --rm -T \
  -v "$PWD:/workspace:ro" \
  --entrypoint /app/.venv/bin/python \
  app scripts/smoke_openrouter_image.py \
  /workspace/path/to/image.png \
  --caption 'caption text'

# Bybit Demo dry run
pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE --side LONG --omit-tp

# Submit fresh Bybit Demo orders
pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE --side LONG --omit-tp \
  --cancel-existing --execute
```

`--execute` uses Bybit Demo, never mainnet.

## App lifecycle

```bash
docker compose up -d --build --force-recreate app
docker compose logs -f app

# Build without restarting the running container
docker compose build app
```

Do not use `docker compose down -v` unless you intentionally want to delete the
Telegram/SQLite and Redis state.

## Startup lookback

`TELEGRAM_STARTUP_LOOKBACK_HOURS=5` scans recent posts on startup; `0` disables
it. Albums are processed as one post, oldest posts first, and already-seen
source messages are deduplicated. Approval is still required.
