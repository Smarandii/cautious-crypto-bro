# Testing and diagnostics

Run commands from the repository root.

## Quality checks

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Docker test equivalent:

```bash
docker compose --profile test run --rm --build test
```

For scripts that should run inside the app container, this helper is useful:

```bash
pyapp() {
  docker compose run --rm -T \
    --entrypoint /app/.venv/bin/python \
    app "$@"
}
```

## Replay a Telegram post

Replay uses the original Telegram text/images, reconstructs albums, applies
current guidance, and runs the production extractor.

```bash
pyapp scripts/replay_telegram_post.py '<telegram-post-url>'
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --intent-only
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --send-approval
```

Replay does not mark the source as seen. Historical MARKET signals may fail
planning because planning uses current Bybit market data.

## Audit recent signals

Review all configured Telegram sources over a recent window without creating
intents or executing trades:

```bash
rm -rf audit-output
mkdir -p audit-output

docker compose run --rm -T \
  -v "$PWD/audit-output:/audit-output" \
  --entrypoint /app/.venv/bin/python \
  app \
  /app/scripts/audit_recent_signals.py \
  --hours 5 \
  --output-dir /audit-output
```

Use `--cache-only` to avoid fresh OpenRouter calls.

## Configuration

Execution policy:

```bash
pyapp scripts/set_execution_policy.py
pyapp scripts/set_execution_policy.py --help
```

Global guidance:

```bash
cat guidance.txt | pyapp scripts/set_guidance.py --global
```

Channel guidance:

```bash
cat guidance.txt | pyapp scripts/set_guidance.py --channel <channel-id>
```

## Smoke tests

Bybit Demo dry run:

```bash
pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE \
  --side LONG \
  --omit-tp
```

Adding `--execute` submits orders to **Bybit Demo**.

## App lifecycle

```bash
docker compose up -d --build --force-recreate app
docker compose logs -f app
```

Do not run `docker compose down -v` unless you intentionally want to erase
Telegram/SQLite and Redis state.
