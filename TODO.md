# Roadmap

Strategy V2 is the release plan for `v2.0.0`. Keep `master` deployable while
developing on `release/v2.0.0`. Run all automated execution only on Bybit Demo.

See [STRATEGY.md](STRATEGY.md) for the strategy contract.

## v2.0.0 transition

### Foundation

- [x] Commit `STRATEGY_V2.md` and this roadmap.
- [x] Replace runtime hardcoded capital sizing with live Bybit
      `totalWalletBalance`.
- [x] Snapshot live capital into each new execution plan so existing strategies
      never resize when the account balance changes.
- [x] Fail closed for new OPEN planning when live wallet balance is unavailable.
- [x] Keep the existing database capital column only as temporary V1
      compatibility state until the V2 schema migration removes it.
- [x] Add an offline Strategy V2 replay tool for the forensic dataset.
- [x] Benchmark entry weights, pullback depths, exit shares, TP3, trailing
      activation, trail distance, and fee-aware profit floors.
- [x] Freeze V2.0 policy values from loss-prevention metrics, not maximum
      historical PnL.

### Strategy model and planning

- [ ] Extend the existing execution policy with the minimum V2 entry, exit, and
      protection parameters. Do not add a generic strategy framework.
- [ ] Evolve `ExecutionPlan` with a strategy version and V2 fields while keeping
      historical V1 plans readable.
- [ ] Separate entry legs from exit legs.
- [ ] Add risk-weighted MARKET, LIMIT, and RANGE entry ladders.
- [ ] Size every entry leg against the frozen risk budget and original stop.
- [ ] Require all-entry-fill worst-case loss to remain within the risk budget
      after tick and quantity rounding.
- [ ] Use universal partial exits even when the trader supplies a take-profit.
- [ ] Keep an explicit runner allocation.
- [ ] Reject automatic execution when the position is too small to support at
      least one partial exit plus a runner.

### Bybit execution primitives

- [ ] Reuse `BybitDemoExecutor`; do not add an exchange abstraction.
- [ ] Add concrete methods only for the V2 operations that are needed:
      wallet balance, entry placement/cancellation, reduce-only exits,
      position-level trading stop, and order/fill lookup.
- [ ] Preserve clock synchronization and existing HTTP error handling.
- [ ] Verify every destructive mutation by re-reading Bybit state.
- [ ] Give every directly created V2 order a deterministic `orderLinkId`.

### Durable position strategies

- [ ] Add a `StrategyStatus` enum distinct from Telegram `IntentStatus`.
- [ ] Add one durable `position_strategies` table for V2 runtime state.
- [ ] Persist frozen capital, risk budget, original stop, entry freeze state,
      expected live quantity, exit plan, protection state, and source intent IDs.
- [ ] Add a sequential SQLite migration from the current production schema.
- [ ] Migrate a copy of the production database in tests and verify that all V1
      history remains readable.
- [ ] Distinguish successful order submission from actual position entry.

### Entry execution

- [ ] Persist the V2 strategy before the first exchange mutation.
- [ ] Submit deterministic LIMIT/RANGE ladders without multiplying entries by
      take-profit slices.
- [ ] For MARKET signals, execute E1 first and use the actual fill price to
      calculate E2 and E3.
- [ ] Freeze and cancel unfilled entry legs on TP1, profit-protection threshold,
      REDUCE, CLOSE, or trailing activation.
- [ ] After entry freeze, never increase exposure automatically.

### Exit and protection execution

- [ ] Build TP1/TP2/TP3 as independent reduce-only exits against the actual
      frozen position quantity.
- [ ] Assign rounding residue to the runner.
- [ ] Establish one verified position-level catastrophe stop for live V2
      positions.
- [ ] Keep the original catastrophe stop when profit trailing activates.
- [ ] Activate native Bybit trailing protection at the selected live-R threshold.
- [ ] Calculate the minimum protected floor from realized costs, estimated close
      fees, slippage reserve, and a positive profit buffer.
- [ ] Never move profit protection backward automatically.

### Position supervisor

- [ ] Add only one new runtime module: `position_supervisor.py`.
- [ ] Run it from the existing `asyncio.TaskGroup`; do not add a scheduler
      dependency.
- [ ] Start with polling. Add WebSockets only if measured behavior proves polling
      inadequate.
- [ ] Reconcile persisted active strategies with live Bybit positions and orders.
- [ ] Use Bybit as the source of truth for live exposure.
- [ ] Never increase exposure except through persisted planned entry legs.
- [ ] Leave exchange-side protection intact when polling or reconciliation fails.

### REDUCE and CLOSE

- [ ] Keep the current strict LLM evidence requirements for destructive actions.
- [ ] REDUCE: freeze entries, cancel pending entries, re-read, reduce, verify,
      re-read again, rebuild exits, and reconcile protection.
- [ ] Remove the stale-quantity behavior observed after the historical NEAR
      reduction.
- [ ] CLOSE: cancel CCB entries and exits, re-read, reduce-only close the full
      remainder, and verify zero position.
- [ ] Explicit trader CLOSE always overrides runner and trailing behavior.

### Manual changes and restart recovery

- [ ] Detect unexplained live changes to V2-owned orders or positions.
- [ ] Enter `MANUAL_OVERRIDE` instead of fighting a manual change such as the
      historical ENA edit.
- [ ] Preserve effective protection while automation is paused.
- [ ] Extend the existing quarantine/recovery flow instead of creating a second
      recovery subsystem.
- [ ] On startup, reconcile live Bybit state before any V2 mutation.
- [ ] Never blindly replay REDUCE, CLOSE, or protection changes after a crash.

### Telegram visibility

- [ ] Show live wallet capital and frozen risk budget in approval cards.
- [ ] Show E1/E2/E3, fixed exits, runner allocation, strategy state, and current
      protection state.
- [ ] Make manual approval display the exact maximum planned loss before
      execution.

### Demo rollout

- [ ] Run V2 supervisor logic in observe-only mode against current Demo state.
- [ ] Resolve every unexplained ownership mismatch before enabling mutations.
- [ ] Roll out protection and independent exits before full entry laddering.
- [ ] Enable deferred MARKET and deeper LIMIT/RANGE entries only after protection
      behavior is stable.
- [ ] Re-run the forensic exporter and compare V2 with V1 using:
      profitable-to-losing round trips, full-stop losses, maximum drawdown,
      peak-profit give-back, net realized R after costs, runner capture, fill
      quality, and strategy-management failures.
- [ ] Prefer the safer parameter set when performance differences are small.

### V2 cleanup and release

- [ ] Remove the legacy configurable `trading_capital_usdt` database field and
      `--capital-usdt` CLI option.
- [ ] Remove V1 TP-per-entry execution assumptions and obsolete compatibility
      branches after no active strategy requires them.
- [ ] Run a final Ponytail audit for duplicated V1/V2 logic.
- [ ] Update README and TESTING documentation for V2 behavior.
- [ ] Run the full local quality gate and production-like Demo smoke/restart
      tests.
- [ ] Merge `release/v2.0.0` to `master` and tag `v2.0.0`.

## Later work

### Performance accounting

Persist enough fill, exit, fee, funding, and strategy-transition data to
reconstruct realized signal performance without assuming that a shared Bybit
position belongs to one source channel.

### MENSA visual structure

Interpret chart entry structures only when image calibration is reliable.
Derive structural stops deterministically from channel guidance and reject
low-confidence geometry instead of inventing prices.

### Deferred ingestion

Speech-to-text and video ingestion remain lower priority than Strategy V2
execution quality.
