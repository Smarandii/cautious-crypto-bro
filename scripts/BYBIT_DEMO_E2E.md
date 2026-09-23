# Bybit Demo integration suite

From the repository root, with Python 3.12 and Docker Compose available:

```sh
python scripts/run_bybit_demo_e2e.py --execute-demo
```

This is an opt-in trading test, excluded from ordinary `pytest`. It places small
real orders on **Bybit Demo**, using credentials already configured in `.env`.
It requires an empty DOGEUSDT position and no DOGEUSDT orders. Other symbols are
left untouched. Each LONG/SHORT case starts with at most $75 initial notional,
at most $200 planned total entry notional, and a $5 frozen risk budget. Initial
sizing reserves 10% of that budget for quote movement; normal execution risk
checks remain enabled.

The runner pauses the normal app, runs a separate container with temporary SQLite
databases, then unpauses the **same process** after verified test-symbol cleanup.
It does not restart/deploy the app, migrate production SQLite, send Telegram
messages, or call an LLM. Existing native exchange protection on other symbols
continues while the app is paused, but app reconciliation is temporarily paused.
Only one operator should run this suite against the account at a time.

If cleanup cannot be verified, the runner fails and leaves the app paused. Inspect
the reported evidence directory and DOGE exposure before unpausing it. `finally`
cleanup handles ordinary assertion/API failures; a killed host or Docker outage
still requires manual inspection.

| Scenario | Boundary exercised |
| --- | --- |
| LONG and SHORT MARKET OPEN | Real planner, SQLite, coordinator, E1 fill, E2/E3 placement, immediate protection |
| Stop handoff and TP cap | Real full stop, removal of attached partial stop, three distinct bounded targets |
| Unchanged restart | Separate Python process reopens the same test database without rebuilding exits |
| Interrupted installation | Application exception injected after a real accepted exit; fresh process reuses it |
| Tightened and removed stop | Real exchange change; supervisor quarantines and preserves the change; test restores stop |
| TP fill and freeze | Test amends TP1 to a marketable price; real execution/history triggers cancellation of remaining entries |
| Russian 25% REDUCE | Production evidence normalization, durable action, real exact-size reduction and exit rebuild |
| Confirmation failure | Real accepted reduction with injected client read failure; UNCERTAIN persists across fresh process |
| CLOSE and cleanup | Coordinator confirmation and verified zero DOGE position/orders |

Live tests deliberately distinguish exchange operations from application fault
injection. Partial-fill timing and an immediate fill *during* installation remain
covered by deterministic HTTP-boundary regression tests, not forced live-market
conditions. Telegram delivery and LLM accuracy are outside this suite. A passing
run is evidence for these scenarios, not proof of complete reliability.

Evidence is saved under `audit-artifacts/bybit-demo-<timestamp>/`: `results.jsonl`
contains assertions/results and cleanup receipts; `manifest.json` records the Git
revision, dirty tracked files, application/suite SHA-256 hashes, and original app
container identity/start time. It never records API credentials. Failed runs are
retained rather than overwritten.

Offline safeguards and deterministic regressions:

```powershell
python -m pytest tests/test_demo_e2e.py tests/test_remaining_concerns.py -q
```
