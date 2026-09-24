# Testing and operations

Run from repository root. Local checks need Python 3.12 and uv.

## Quality gate

```sh
uv sync --frozen --group dev
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run pyright
uv run pytest -q
uv lock --check
uv build
uv run pre-commit run --all-files
git diff --check
```

Default pytest uses offline/mocked boundaries and real temporary SQLite.
Live Demo testing is opt-in; passing tests does not prove complete reliability.

## Configured helpers

These Bash examples use Compose configuration, Redis and persistent app state.
Helpers may initialize schema, populate caches or call paid providers.

```bash
pyapp() {
  docker compose run --rm -T --entrypoint /app/.venv/bin/python app "$@"
}
pyapp scripts/replay_telegram_post.py '<telegram-post-url>'
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --intent-only
pyapp scripts/set_execution_policy.py
pyapp scripts/set_execution_policy.py --risk-pct 1
cat guidance.txt | pyapp scripts/set_guidance.py --global
cat guidance.txt | pyapp scripts/set_guidance.py --channel -1001234567890
```

Post replay does not submit orders or mark the source processed. Historical MARKET
plans can fail against today's prices. Capital comes from live wallet balance.

```bash
mkdir -p audit-output
docker compose run --rm -T -v "$PWD/audit-output:/audit-output" \
  --entrypoint /app/.venv/bin/python app scripts/audit_recent_signals.py \
  --hours 5 --output-dir /audit-output
```

Add `--cache-only` to avoid fresh model calls; keep each audit's evidence separately.
Offline comparison: `uv run python scripts/replay_strategy_v2.py <forensic-bundle>`.

Other tools in `scripts/` cover Telegram authorization, image-provider smoke checks,
cooldown stress checks and configurable Bybit smoke planning.
`smoke_bybit_trade.py --execute` submits Demo orders; default behavior is preview.

## Live Bybit Demo integration

```sh
python scripts/run_bybit_demo_e2e.py --execute-demo
```

Requires Docker Compose, configured Demo credentials, and an empty DOGEUSDT
position with no DOGE orders. One operator at a time. LONG/SHORT cases submit
real Demo orders: initial notional ≤ $75, planned total ≤ $200, frozen risk ≤ $5,
with 10% initially reserved for quote movement.

The runner pauses the app, uses temporary SQLite databases, then unpauses the same
process only after verified cleanup. Other symbols remain untouched; their native
protection continues while app reconciliation pauses. No Telegram messages, LLM
calls, production migrations or deployment occur.

Scenarios: staged entry/rebasing; stop handoff and target caps; fresh-process restart;
interrupted exit installation; changed/removed stops; TP fill and entry freeze;
Russian 25% REDUCE/rebalance; accepted-but-unconfirmed reduction quarantine; CLOSE
and zero-position/order cleanup. Partial-fill timing and fills during installation
are also tested deterministically.

Evidence: `audit-artifacts/bybit-demo-<timestamp>/results.jsonl` and `manifest.json`
record checks, cleanup receipts, revision, dirty files, hashes and app identity.
If cleanup fails, the app stays paused. Inspect DOGE exposure and evidence before
unpausing; host/Docker failures also need manual inspection.

## Deployment and state

```sh
docker compose up -d --build app
docker compose logs -f app
docker compose stop app
```

Back up SQLite before state repair; stop the app to prevent concurrent mutations.
Restore a paused strategy only after checking ownership, orders and protection.
Never replay uncertain submissions blindly. Do not run `docker compose down -v`
unless intentionally erasing SQLite, Telegram-session and Redis volumes.
