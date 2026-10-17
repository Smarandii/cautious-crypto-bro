# Strategy V2

Bybit Demo only. The LLM extracts intent; code owns sizing, entries, exits and
protection. Historical V1 plans remain readable but cannot execute.

## Capital, stops and entries

Each plan freezes Unified Account `totalWalletBalance`:
`risk_budget = wallet_balance × risk_per_trade_pct / 100` (default 1%).
Unrealized PnL is not used as capital. Later wallet changes do not resize the plan.
The budget covers entry-to-stop price loss, not fees, funding or stop slippage.

Plans freeze explicit trader leverage from text/images, or 10x when unspecified.
Leverage does not multiply the risk-sized order quantities. Before MARKET E1 or
the LIMIT/RANGE batch, Bybit must accept that leverage for both sides of the
one-way symbol. An already-matching setting succeeds; unsupported leverage or
other setting failures reject the entry without submitting orders. E2/E3 retain
the leverage applied before E1. Historical plans without leverage default to 10x.

Trader stops take precedence. Otherwise a 2% fallback uses the MARKET snapshot,
LIMIT price, or adverse RANGE edge (LONG lower / SHORT upper). Stops round outward
to ticks and must remain positive. Plans persist the concrete stop and
`stop_loss_source`; actual MARKET fills retain that stop.

| Leg | Risk allocation | MARKET / LIMIT level |
| --- | --- | --- |
| E1 | 60% | Market snapshot / trader limit |
| E2 | 25% | 0.33 of the E1-to-stop distance toward stop |
| E3 | 15% | 0.66 of the E1-to-stop distance toward stop |

RANGE uses near edge, midpoint and far edge in execution order.
Each quantity is leg risk divided by distance to stop, rounded down to quantity step.
Reject plans below exchange minimums or unable to support a partial exit and runner.

MARKET refreshes sizing at the submission quote, reducing E1 quantity when needed
to fit its frozen risk allocation; it never increases the original E1 quantity.
The refreshed ladder must pass stop, exchange-minimum and exit-capacity checks.
It executes E1 first, confirms its fill and persists the rebased plan before
E2/E3 submission. Adverse slippage reduces remaining allocations to stay inside
the frozen budget. LIMIT/RANGE entries submit as a batch.

OPEN approvals do not expire by age. Submission rechecks live price: MARKET must
retain valid stop geometry and fit the frozen risk budget; LIMIT/RANGE must not
have crossed E1 (LONG below E1 / SHORT above E1). Resting limits retain their
original prices even when the market is far away; entries never chase it.

Only one V2 strategy may own a symbol. Active strategies, positions or pending
entry orders block another OPEN, including manual approval. Resolve existing
ownership before adding another ladder.

## Exits and protection

| Allocation | Default target |
| --- | --- |
| TP1: 25% | +0.50R |
| TP2: 25% | +1.00R |
| TP3: 25% | +1.50R |
| Runner: 25% | Trailing protection |

Exits are independent reduce-only orders sized from actual quantity; rounding
residue stays in the runner. Compatible trader targets constrain later exits;
a target too close for the first policy exit is rejected.

Each entry carries the original stop. The supervisor verifies a position-level
stop before removing superseded attached partial stops. It never deliberately
weakens established protection; unexplained changes pause automation.

Any fixed-TP fill, profit-protection activation, REDUCE or CLOSE freezes remaining
entries. At +0.50R, native trailing distance is 0.30R with a +0.05R minimum floor.
The anchor uses Bybit break-even price when available, otherwise average entry.
This does not guarantee positive realized PnL after all costs.

## Demo payoff-exit experiment

The Compose Demo app selects
`DEMO_EXIT_PROFILE=payoff_early_tight_trail_long_015` for newly planned trades.
It keeps the 60/25/15 entry allocation and uses 15/20/25% reductions at
+1/+2/+4R, a 40% runner, and a 0.05R trail distance. The trail activates at
+0.15R for LONGs and +0.20R for SHORTs. This is separately tagged from the
prior `payoff_early_trail` profile (0.10R trail distance) and the
`payoff_early_tight_trail` profile (+0.20R activation for both sides). Existing
plans and open positions continue using their saved policy.

This is an experiment, not a promoted strategy: historical replay gains were
concentrated and uncertainty intervals crossed zero. Score only complete,
reconciled positions tagged with this profile using
`analyze_forensic_pnl.py BUNDLE --prospective-demo-exit-profile
--demo-exit-profile payoff_early_tight_trail_long_015`; review after 20
completed positions as an interim checkpoint only. That sample is not powered
to establish a modest edge; the sample-size limits and required metrics are in
`TESTING.md`. Continue to report side-specific payoff ratio, expectancy, costs,
concentration, and uncertainty before deciding whether to keep or promote it.

For a cleaner prospective comparison, Compose assigns newly planned LONGs 50/50
by a stable SHA-256 bucket of intent ID: control uses the same tight-trail policy
at +0.20R; treatment uses +0.15R. Both retain the same 0.10x LONG risk, 0.05R
trail distance, entry/target rules, portfolio cap, and 240-minute entry TTL.
Plans persist distinct tags (`payoff_early_tight_trail_long_ab_020_control` and
`payoff_early_tight_trail_long_ab_015`); SHORT behavior is unchanged and is not
part of this comparison. Review each arm separately after 20 complete LONGs as
an interim check. This assignment improves comparability; it does not remove
symbol/source clustering or make 20 cases sufficient to prove a modest edge.

An exact-profile replay on the 54 fully reconciled historical positions found
baseline vs challenger net results of −1.859R vs +0.370R in train (27 cases),
+0.347R vs +2.334R in validation (10), and −2.264R vs +0.701R in the already
inspected holdout (17). In that holdout the challenger had 0.041R expectancy
(76.5% wins, +0.290R average win, −0.766R average loss), versus −0.133R for
baseline. Its paired holdout delta was +2.965R, but the IID 95% interval was
[−0.491R, +7.147R]; removing the three largest positive case contributions
leaves −0.140R. Treat this as a fragile replay hypothesis, not evidence of
live profitability. No plans were created under that original profile.

The early-trail variant was compared on the same previously inspected cases.
Relative to +0.40R activation, +0.20R returned +0.938R vs +0.370R in train,
+1.189R vs +2.334R in validation, and +1.748R vs +0.701R in holdout. Its
holdout paired delta was +1.047R (IID 95% interval [−2.633R, +5.195R]);
removing its three largest positive case contributions changes the delta to
−1.902R. This evidence is also fragile; the new profile is a Demo-only
prospective experiment, not a proven improvement. Replay suggests the earlier
trail improves many smaller outcomes but validation gave back about 1.145R.
Score only new positions tagged `payoff_early_trail`. The profile started with
no tagged plans; after deployment, catch-up created two plan records: one was
blocked by the existing-position ownership guard, and one XRPUSDT LONG was
executed at 0.25% risk. At the 2026-10-08 08:33Z snapshot, that position is
filled/open (1/20 filled, 0/20 complete), with a 1.3825 stop. All three
account-open positions have stops; the pre-existing DOGE/SOL positions retain
their saved policies. Do not count the open trade as realized performance.

As of 2026-10-08 08:49:47Z, newly planned LONGs use 0.10x the base risk; SHORTs
remain at base risk. This is a new prospective Demo sizing cohort. Existing
plans, including the open XRP LONG at 0.25x, keep their frozen sizing and exits.
Evaluate LONG risk in dollars and risk-weighted return; do not combine different
risk multipliers when comparing dollar average wins/losses.

## Demo portfolio stop-risk cap experiment

New Demo plans are limited by a $400 total open stop-risk cap. Planning estimates
current position risk from live average price to exchange stop and includes
unfilled V2 entry legs; each new plan is sized only to remaining capacity. The
execution preflight recomputes current risk before submitting. If account state,
an existing stop, or an outstanding entry cannot be risk-bounded, the new entry
fails closed. Existing positions and orders are never resized or cancelled by
this cap. The cap was raised from $340 on 2026-10-09 after a read-only account
snapshot measured $366.17 of existing protected exposure and the planner was
skipping new signals at zero capacity. At that snapshot, the new limit allowed
at most $33.83 additional stop risk; future plans remain bounded by live
remaining capacity. This is an operational data-collection adjustment, not an
evidence-based profitability improvement. Evaluate the cap separately from the
0.10x LONG and payoff-profile cohorts.

## Demo entry freshness experiment

Compose sets `DEMO_ENTRY_ORDER_TTL_MINUTES=240` for newly created plans. After
four hours, the supervisor cancels only still-open entry legs linked to that
plan. If any quantity filled, it freezes further entries and manages the live
position with the plan's saved stop and exits; it never closes or resizes that
position. Existing plans missing the field default to zero (no expiry), so
currently resting HYPE/UNI and other saved orders are unaffected. The four-hour
limit matches the documented signal follow-through observation horizon, but is
a freshness hypothesis—not evidence of higher expectancy. Measure only fully
reconciled plans tagged with `entry_order_ttl_minutes=240` using
`analyze_forensic_pnl.py BUNDLE --prospective-demo-entry-order-ttl-minutes 240`.

## Lifecycle actions

REDUCE/CLOSE require an explicit current-caption instruction. Images may identify
symbol/side but cannot authorize an action. Explicit fractions/percentages win;
an unspecified partial close defaults to 50%. HOLD is informational.
`INTENT_MAX_AGE_SECONDS` limits lifecycle approvals only (default 900 seconds).

Execution validates live side/size, cancels CCB entries and fixed exits, re-reads
exposure, submits reduce-only orders, then confirms the resulting position.
REDUCE freezes entries and requests exit rebalancing. CLOSE overrides the runner;
the supervisor completes the transition to CLOSED.

## Recovery

States: ENTERING, OPEN_RISK, PROFIT_PROTECTED, CLOSING, CLOSED, MANUAL_OVERRIDE,
UNCERTAIN. Intent status is separate: EXECUTED can mean accepted pending limits.

- Exchange mutations and supervision share an account lock.
- Proven pre-submit failures close their empty strategy.
- Potentially accepted or partially completed submissions stay UNCERTAIN.
- MANUAL_OVERRIDE and UNCERTAIN prevent automatic strategy mutations.
- Multiple active owners of a symbol pause supervision rather than guess ownership.
- Startup reconciles before AUTO recovery and again before ingestion.
- Exit installation persists its revision before mutations; recovery reuses
  matching open/filled orders. Fill detection uses executed quantity.
- Manual approval delivery retries durably. A crash after sending but before
  acknowledgement may duplicate a card; execution claims prevent duplicate trades.

SQLite schema 8 supports migrations from 4–7. Back up state before repair.
Resume paused strategies only after ownership, live orders and protection agree.

## Randomized Demo LONG participation test

`DEMO_LONG_PARTICIPATION_SKIP_FRACTION=0.50` independently assigns eligible,
auto-approved new Strategy V2 LONG plans to `take` or `skip` using a stable
SHA-256 bucket with a separate salt from the 0.15R/0.20R exit assignment. The
assignment is saved inside the frozen plan. A skip is persisted with
`IntentStatus.SKIPPED` and `ApprovalMode.SKIPPED`, is never sent for approval or
execution, and does not reserve batch stop-risk capacity. Take plans continue
through the existing risk, exit-arm, and protection policy. Manual or otherwise
unsafe signals are not randomized. Existing orders, positions, and serialized
plans are unchanged.

This Demo-only test asks whether allocating risk to the current LONG signal
stream adds after-cost portfolio P&L. Skips contribute zero exposure/P&L by
design; that is a policy-allocation comparison, not evidence that skipped
signals themselves would have lost. Keep participation arms separate from the
LONG-risk and exit-profile cohorts. Do not compare arms while any take
assignment is unresolved; use a minimum 20 assignments per arm and 20 fully
reconciled take positions as an interim review only, not a profitability claim.
Use `analyze_long_participation.py BUNDLE` after exporting a fresh forensic
archive; only the take arm has fees/funding and whole-position payoff metrics.
