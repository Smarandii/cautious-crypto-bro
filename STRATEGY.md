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
