# Roadmap

Strategy V2 is the release plan for `v2.0.0`.

Development is on `release/v2.0.0`. Automated execution remains restricted to
Bybit Demo.

See [STRATEGY.md](STRATEGY.md) for the current strategy contract.

## v2.0.0 transition

### Foundation

- [x] Commit the Strategy V2 contract and roadmap.
- [x] Replace runtime hardcoded capital sizing with live Bybit
      `totalWalletBalance`.
- [x] Snapshot live capital into each new execution plan.
- [x] Fail closed for OPEN planning when live wallet balance is unavailable.
- [x] Remove persisted configurable trading capital from the active execution
      policy.
- [x] Add offline Strategy V2 replay tooling for the forensic dataset.
- [x] Benchmark entry weights, pullback depths, exit shares, TP3, trailing
      activation, trail distance, and profit-floor candidates.
- [x] Freeze V2.0 defaults using loss-prevention criteria.

### Strategy model and planning

- [x] Add the minimal Strategy V2 entry, exit, runner, trailing, and floor
      parameters to the execution policy.
- [x] Make new execution plans Strategy V2 while retaining historical V1 plan
      parsing.
- [x] Separate entry legs from exit legs.
- [x] Add risk-weighted MARKET, LIMIT, and RANGE entry ladders.
- [x] Size each entry leg against the frozen risk budget and original stop.
- [x] Enforce total worst-case stop loss after tick/quantity rounding.
- [x] Use partial exits even when the trader supplies a take-profit.
- [x] Keep an explicit runner allocation.
- [x] Reject V2 plans too small to support a partial exit plus runner.
- [x] Rebase MARKET scale-ins from the actual E1 fill and conservatively
      downsize E2/E3 when fill slippage consumes additional risk budget.

### Bybit execution primitives

- [x] Reuse `BybitDemoExecutor`.
- [x] Add the V2 operations required for wallet balance, entry
      placement/cancellation, reduce-only exits, position-level protection,
      lifecycle actions, and fill confirmation.
- [x] Preserve clock synchronization and HTTP error handling.
- [x] Confirm destructive position actions from re-read live Bybit state.
- [x] Give directly created V2 entry and fixed-exit orders deterministic
      `orderLinkId` values.
- [x] Treat unchanged position protection as idempotent reconciliation rather
      than a fatal mutation.

### Durable position strategies

- [x] Add `StrategyStatus` distinct from Telegram `IntentStatus`.
- [x] Add durable `position_strategies` runtime state.
- [x] Keep frozen capital, risk budget, stop, entry/exit plan, and source intent
      in the persisted execution plan while storing live lifecycle/protection
      state in `position_strategies`.
- [x] Add sequential SQLite migrations through schema v7.
- [x] Preserve historical V1 `ExecutionPlan` parsing and cover v4/v5/v6 to v7
      schema migrations in tests.
- [x] Distinguish order acceptance from confirmed MARKET entry/lifecycle state.
- [x] Persist the actual-fill-derived MARKET plan before E2/E3 are submitted.

### Entry execution

- [x] Persist the V2 strategy before the first exchange mutation.
- [x] Submit LIMIT/RANGE ladders without multiplying entries by TP slices.
- [x] For MARKET, execute E1 first and derive E2/E3 from its confirmed actual
      fill.
- [x] Freeze/cancel unfilled entries on fixed-TP progress, protection
      activation, REDUCE, or CLOSE.
- [x] Prevent automatic exposure growth after entry freeze.

### Exit and protection execution

- [x] Build TP1/TP2/TP3 as independent reduce-only exits from live position
      quantity.
- [x] Leave quantity-rounding residue in the runner.
- [x] Establish and verify one position-level catastrophe stop.
- [x] Verify the initial MARKET E1 `PartialStopLoss` before handing protection
      to the position-level stop.
- [x] Activate native Bybit trailing protection at the configured live-R
      threshold.
- [x] Never move profit protection backward automatically.
- [ ] Extend the minimum protected floor to explicit realized fee, funding, and
      slippage accounting rather than relying on Bybit break-even plus the V2.0
      positive R buffer.

### Position supervisor

- [x] Add `position_supervisor.py` without a separate scheduler dependency.
- [x] Run it from the existing `asyncio.TaskGroup`.
- [x] Use polling as the initial implementation.
- [x] Reconcile persisted active strategies with live Bybit positions/orders.
- [x] Use Bybit as the live source of truth.
- [x] Prevent unplanned exposure growth.
- [x] Keep existing exchange protection intact when reconciliation cannot
      safely proceed.
- [x] Quarantine `UNCERTAIN` strategies from further automated mutation.

### REDUCE and CLOSE

- [x] Keep strict current-message evidence requirements for destructive actions.
- [x] REDUCE freezes entries, cancels stale orders, re-reads, reduces, confirms
      live quantity, and rebuilds against the actual remainder.
- [x] Remove stale-quantity rebuilding after REDUCE.
- [x] CLOSE cancels CCB orders, submits reduce-only close, and confirms zero
      exposure.
- [x] Explicit trader CLOSE overrides runner/trailing behavior.
- [x] Treat submitted-but-unconfirmed lifecycle actions as `UNCERTAIN`.

### Manual changes and restart recovery

- [ ] Complete manual-edit detection for every V2-owned order type, including
      direct edits/cancellation of fixed TP orders.
- [x] Enter `MANUAL_OVERRIDE` for supported unexplained entry/protection/position
      changes instead of fighting the live account.
- [x] Preserve exchange-side protection while automation is paused.
- [x] Extend the existing execution quarantine flow rather than adding a second
      recovery subsystem.
- [ ] Guarantee that startup performs one complete strategy reconciliation
      before startup-lookback processing can cause any V2 exchange mutation.
- [x] Never blindly replay REDUCE/CLOSE after a crash.
- [x] Validate fresh-process restart of an active V2 strategy without
      unnecessary exchange mutations.

### Telegram visibility

- [x] Show frozen live wallet capital and exact planned maximum loss on OPEN
      approval cards.
- [x] Show E1/E2/E3, fixed exits, and runner allocation.
- [ ] Add current durable strategy state and live protection state to relevant
      Telegram runtime/status views.
- [x] Manual approval displays the exact deterministic maximum planned loss.

### Demo rollout

- [x] Exercise the complete V2 path on Bybit Demo: staged MARKET entry,
      independent exits, runner, protection handoff, lifecycle reconciliation,
      restart recovery, REDUCE, and CLOSE.
- [x] Validate actual-fill MARKET rebasing against live Bybit Demo orders.
- [x] Validate immediate catastrophe protection before supervisor handoff.
- [ ] Re-run the forensic exporter against the final implementation and compare
      V2 with V1 on profitable-to-losing round trips, full-stop losses,
      drawdown, peak-profit give-back, realized R after costs, runner capture,
      fill quality, and strategy-management failures.
- [x] Keep the safer frozen parameter set when historical performance
      differences are small.

### V2 cleanup and release

- [x] Remove the legacy configurable `trading_capital_usdt` database field and
      `--capital-usdt` CLI option.
- [ ] Audit and remove any unnecessary residual V1 runtime execution branches
      while preserving historical V1 plan readability.
- [ ] Run a final code audit for duplicated V1/V2 execution logic.
- [x] Update README and TESTING documentation for V2 behavior.
- [x] Run production-like Demo OPEN/restart/REDUCE/CLOSE and MARKET
      actual-fill/protection-handoff smoke tests.
- [ ] Run the final post-documentation local quality gate.
- [ ] Merge `release/v2.0.0` to `master`.
- [ ] Tag `v2.0.0`.

## Later work

### Performance accounting

Persist enough fill, exit, fee, funding, and strategy-transition data to
reconstruct realized signal performance without assuming that a shared Bybit
position belongs to one source channel.

Use explicit realized execution costs when setting the protected-profit floor.

### Manual mutation coverage

Extend owned-order reconciliation to detect manual edits or cancellation of
fixed V2 TP orders without confusing genuine fills with manual intervention.

### Startup ordering

Make startup reconciliation an explicit barrier before startup-lookback messages
are allowed to trigger V2 exchange mutations.

### MENSA visual structure

Interpret chart entry structures only when image calibration is reliable.
Derive structural stops deterministically from channel guidance and reject
low-confidence geometry instead of inventing prices.

### Deferred ingestion

Speech-to-text and video ingestion remain lower priority than Strategy V2
execution quality.
