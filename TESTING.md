# Testing and diagnostics

Run commands from the repository root.

## Local quality gate

Static analysis and unit tests run locally with `uv`:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pyright
uv run pytest -q
uv lock --check
uv run pre-commit run --all-files
git diff --check

Do not use Docker as the normal unit-test/static-check environment.

Docker Compose is for runtime and integration checks that require application
configuration, Redis, Telegram, Bybit Demo, or an LLM provider.

App-container helper

For scripts that need the application environment:

pyapp() {
  docker compose run --rm -T \
    --entrypoint /app/.venv/bin/python \
    app "$@"
}

Use -T for heredoc-driven Docker commands.

Replay a Telegram post

Replay reconstructs the Telegram text/images/albums, loads current guidance, and
runs the production extractor.

pyapp scripts/replay_telegram_post.py '<telegram-post-url>'
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --intent-only
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --send-approval

Replay does not mark the source post as processed.

Historical MARKET signals can fail planning when current Bybit market geometry
is no longer compatible with the historical signal.

REDUCE/CLOSE are executable only when current Telegram text/caption contains
explicit destructive-action evidence.

Audit recent signals
rm -rf audit-output
mkdir -p audit-output

docker compose run --rm -T \
  -v "$PWD/audit-output:/audit-output" \
  --entrypoint /app/.venv/bin/python \
  app \
  /app/scripts/audit_recent_signals.py \
  --hours 5 \
  --output-dir /audit-output

Use --cache-only to avoid fresh model calls.

Strategy replay

Strategy V2 replay/forensic tools should remain offline and deterministic.

Use them to compare candidate parameter sets by loss prevention rather than only
gross historical PnL.

The frozen V2.0 defaults are documented in
STRATEGY.md.

Execution policy

Show the current execution policy:

pyapp scripts/set_execution_policy.py

Update risk per strategy:

pyapp scripts/set_execution_policy.py --risk-pct 1

Capital is not configured here. New plans read live Bybit
totalWalletBalance and freeze it into the execution plan.

Guidance

Global:

cat guidance.txt | pyapp scripts/set_guidance.py --global

Per channel:

cat guidance.txt | pyapp scripts/set_guidance.py --channel <channel-id>
Bybit Demo smoke tests

The repository must use Bybit Demo credentials only.

Basic trade smoke:

pyapp scripts/smoke_bybit_trade.py \
  --entry-type RANGE \
  --side LONG \
  --omit-tp

Adding --execute submits real orders to the Bybit Demo account.

Before any targeted lifecycle smoke:

stop the normal app;
choose a symbol with no existing position/orders;
use an isolated SQLite database under /state;
keep risk deliberately small while satisfying Bybit minimum quantity and
notional constraints;
verify cleanup from live Bybit state afterward.

Example:

docker compose stop app

docker compose run --rm -T \
  -e DATABASE_PATH=/state/ccb-v2-smoke.sqlite3 \
  app /app/.venv/bin/python - <<'PY'
# runtime smoke code
PY
V2 runtime scenarios validated for v2.0.0

The release validation exercised these scenarios on Bybit Demo:

MARKET E1 executes before E2/E3;
actual E1 fill price/quantity are persisted into the effective plan;
E2/E3 are derived from the confirmed E1 fill;
adverse-fill risk cannot exceed the frozen budget;
E1 has immediate catastrophe protection through Bybit PartialStopLoss;
supervisor installs and verifies the position-level stop before removing the
partial stop;
fixed TP1/TP2/TP3 exits are installed from live position quantity;
fresh-process reconciliation leaves an unchanged active strategy untouched;
REDUCE cancels stale entries/exits and waits for the live reduced quantity;
exits rebuild from the actual REDUCE remainder;
unchanged stop protection does not produce a fatal reconciliation error;
CLOSE waits until Bybit reports zero position;
supervisor persists the strategy as CLOSED;
final cleanup leaves zero position and zero stale V2 orders.
Failure semantics

A submitted destructive lifecycle order is not considered successful merely
because Bybit returned an order ID.

If the resulting live position cannot be confirmed, the action becomes
UNCERTAIN.

UNCERTAIN strategies are quarantined from automatic supervisor mutations.

Manual/unexplained changes can move supported strategies to
MANUAL_OVERRIDE.

App lifecycle

Start/recreate:

docker compose up -d --build --force-recreate app
docker compose logs -f app

Stop only the app:

docker compose stop app

Do not run:

docker compose down -v

unless the intention is to erase persistent SQLite/Telegram/Redis state.
