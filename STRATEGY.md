# Strategy V2

Strategy V2 manages entries, exits, and protection deterministically on Bybit
Demo. The LLM interprets trader intent; deterministic code owns risk sizing,
entry geometry, exits, lifecycle actions, and reconciliation.

## Objectives

Apply these priorities in order:

1. Limit the maximum planned loss.
2. Prevent profitable trades from becoming losing trades where practical.
3. Realize partial profits while retaining a runner.
4. Preserve exchange-side protection through crashes, restarts, and lifecycle
   changes.
5. Follow trader guidance when it does not conflict with deterministic risk
   controls.

## Capital and risk

Use Bybit Unified Account `totalWalletBalance` as the capital base.

Capital is read when a new execution plan is created and frozen into that plan.
An existing strategy does not resize when the wallet balance later changes.

```text
risk_budget = capital_base_usdt * risk_per_trade_pct / 100

The default policy is 1% risk per strategy unless configured otherwise.

Do not use totalEquity as the sizing base because it includes unrealized
derivatives PnL.

Entry ladder

Every Strategy V2 plan has three entry legs:

E1 = 60% of risk
E2 = 25% of risk
E3 = 15% of risk
MARKET

For MARKET signals:

Plan E1 from the current market snapshot.
Execute E1 first.
Confirm the actual Bybit fill price and filled quantity.
Persist a new effective execution plan using the actual E1 fill.
Derive E2 at 0.33R toward the original stop.
Derive E3 at 0.66R toward the original stop.
Submit E2/E3 only after the fill-derived plan is durable.

If adverse E1 slippage consumes more than the nominal E1 risk allocation,
remaining E2/E3 risk is scaled down so the total worst-case loss still cannot
exceed the frozen strategy risk budget.

LIMIT

Use the trader limit as E1.

Derive E2 and E3 at 0.33R and 0.66R toward the original stop.

RANGE

Use the three range levels in execution order:

near edge;
midpoint;
far edge.
Sizing

Each leg is sized by risk rather than equal quantity:

leg_quantity =
    leg_risk_budget / abs(entry_price - original_stop)

Round quantities down to the Bybit quantity step.

The combined worst-case loss at the original stop must remain within the frozen
strategy risk budget after tick and quantity rounding.

Reject automatic planning if the primary position is too small to support at
least one valid partial exit while preserving a runner.

Entry freeze

Cancel all remaining entry legs when any of these events occurs:

a fixed TP fills;
price reaches the profit-protection activation threshold;
REDUCE executes;
CLOSE executes;
trailing protection activates.

After entry freeze, Strategy V2 must never increase exposure automatically.

Exit ladder

Entries and exits are independent.

Default fixed exits:

TP1: 25% at +0.50R
TP2: 25% at +1.00R
TP3: 25% at +1.50R
Runner: 25%

Fixed exits are reduce-only and are rebuilt from the actual live position
quantity.

Quantity rounding residue remains in the runner.

A trader-provided take-profit does not disable partial exits. When compatible
with the V2 risk geometry it can constrain the later fixed targets.

A trader TP at or below the first +0.50R target is rejected for automatic V2
execution.

Initial protection

Each V2 entry submitted to Bybit carries the original stop.

For a filled MARKET E1, Bybit may represent that protection as a
PartialStopLoss conditional order rather than position.stopLoss.

The supervisor performs a verified protection handoff:

observe the live filled position;
establish one position-level catastrophe stop;
verify that stop from live Bybit state;
remove superseded per-entry partial stop orders.

The strategy therefore keeps loss protection during the handoff instead of
cancelling the old protection first.

Profit protection

The original catastrophe stop is the minimum protection floor.

At:

activation = +0.50R

Strategy V2 freezes remaining entries and enables native Bybit trailing
protection:

trail_distance = 0.30R
minimum_locked_profit = +0.05R

The protection anchor uses Bybit breakEvenPrice when available, otherwise the
live average entry.

Protection must never move backward automatically.

The current +0.05R floor is a V2.0 Demo policy. Full realized
fee/funding/slippage accounting remains future work; it should not be described
as a guaranteed net-profit floor.

REDUCE

REDUCE performs:

validate the live position and expected side;
cancel pending CCB entry legs;
cancel current V2 fixed exits;
re-read live exposure;
submit the reduce-only market reduction;
wait until Bybit confirms the reduced live quantity;
freeze future entries;
mark the strategy for rebalance;
rebuild exits from the actual remaining quantity;
reconcile protection.

If Bybit accepts the order but the resulting position cannot be confirmed, the
action and strategy enter UNCERTAIN rather than being retried blindly.

CLOSE

CLOSE performs:

cancel CCB entry legs;
cancel V2 fixed exits;
re-read the position;
submit a reduce-only full close;
wait until Bybit reports zero exposure;
persist CLOSING;
allow the supervisor to transition the durable strategy to CLOSED.

Trader CLOSE overrides runner and trailing behavior.

Runtime states

Strategy V2 uses:

ENTERING
OPEN_RISK
PROFIT_PROTECTED
CLOSING
CLOSED
MANUAL_OVERRIDE
UNCERTAIN

IntentStatus and StrategyStatus are separate concepts.

Successful order submission is not treated as proof of a successful position
mutation.

Reconciliation

Bybit is the source of truth for live exposure and orders.

For destructive mutations use the pattern:

read -> validate -> mutate -> read -> verify

Important invariants:

never remove the last effective loss protection before replacement
protection is confirmed;
never move profit protection backward;
never let planned V2 exits exceed the live remaining position;
never rebuild exposure after entry freeze;
do not blindly replay destructive actions after a crash;
quarantine uncertain execution instead of guessing.

The durable execution plan is also the anti-tamper reference for V2-owned entry
orders.

Restart behavior

The runtime persists Strategy V2 state in SQLite and reconciles it against live
Bybit state through PositionSupervisor.

Confirmed Demo testing covers:

active-strategy process restart with no unnecessary mutations;
REDUCE followed by restart-safe exit rebuilding;
confirmed CLOSE and durable CLOSED state;
staged MARKET E1 actual-fill rebasing;
initial PartialStopLoss to full-position stop handoff.
V2.0 defaults

The frozen initial Demo defaults are:

Entry risk:        60% / 25% / 15%
Entry depths:      0R / 0.33R / 0.66R
Fixed exits:       25% / 25% / 25%
Runner:            25%
TP levels:         0.50R / 1.00R / 1.50R
Trail activation:  0.50R
Trail distance:    0.30R
Minimum floor:     +0.05R

These are V2.0 Demo defaults derived from limited forensic history. They are not
universal optima and should only change after replay and Demo evidence support
the change.

Environment

Strategy V2 is currently restricted to Bybit Demo.

Do not use production API credentials.
