# Strategy V2

Strategy V2 manages entries, exits, and profit protection independently. The goal is to reduce loss frequency and prevent profitable trades from becoming losing trades while keeping a runner for larger moves.

## Objectives

Apply these priorities in order:

1. Limit the maximum planned loss.
2. Prevent profitable trades from becoming losing trades.
3. Realize small net profits after fees and funding.
4. Keep part of each winning position open for larger moves.
5. Follow trader guidance when it does not conflict with the risk rules above.

The LLM extracts trader intent. Deterministic code manages risk and execution.

## Capital and risk

Use Bybit Unified Account `totalWalletBalance` as the capital base. Do not use a hardcoded capital value.

Snapshot the capital base when you create a strategy. Do not resize an open strategy when the account balance changes.

Calculate the strategy risk budget as:

```text
risk_budget = capital_base_usd * risk_per_trade_pct / 100
```

Use 1% risk per strategy unless the execution policy specifies another value.

Do not use `totalEquity` for sizing because it includes unrealized derivatives PnL.

## Entry ladder

Create up to three entry legs for every new strategy.

Default MARKET candidate:

- E1: 65% of risk at market.
- E2: 20% of risk at 0.25R toward the original stop.
- E3: 15% of risk at 0.50R toward the original stop.

For LIMIT signals, use the trader limit as E1 and place E2 and E3 deeper toward the original stop.

For RANGE signals, use the near edge, midpoint, and far edge in execution order.

Size each leg by risk, not by equal quantity:

```text
leg_quantity = leg_risk_budget / abs(entry_price - original_stop)
```

Round quantities down to the Bybit quantity step. The combined worst-case loss at the original stop must not exceed the strategy risk budget.

Cancel all unfilled entry legs when any of these events occurs:

- TP1 fills.
- Price reaches the profit-protection threshold.
- A REDUCE signal executes.
- A CLOSE signal executes.
- Profit trailing activates.

After entry freeze, the strategy must not increase exposure automatically.

## Exit ladder

Manage exits independently from entries.

Default candidate:

- TP1: close 25% at +0.50R.
- TP2: close 25% at +1.00R.
- TP3: close 25% at +1.50R.
- Runner: keep the remaining 25% under trailing protection.

A trader-provided TP does not disable partial exits. Use it as a later target or runner reference.

If the trader TP is below +0.50R, keep the trade manual until policy says otherwise.

## Profit protection

Keep the original stop as catastrophe protection.

When price reaches the configured protection threshold, freeze entries and activate a position-level trailing stop.

Initial candidate:

```text
activation = +0.50R
trail_distance = 0.45R
nominal_initial_floor = +0.05R
```

The actual protected floor must cover expected closing fees, funding, slippage reserve, and a positive profit buffer.

Never move profit protection backward automatically.

## REDUCE and CLOSE

REDUCE must:

1. Freeze and cancel pending entries.
2. Re-read the live position.
3. Execute the requested reduction.
4. Confirm the fill.
5. Re-read the remaining position.
6. Rebuild remaining exit quantities.
7. Reconcile static and trailing protection.

CLOSE must cancel CCB entry and exit orders, close the full remaining position with a reduce-only order, and verify that the position is zero.

Trader CLOSE always overrides the strategy.

## Runtime states

Use these strategy states:

- `PLANNED`
- `ENTERING`
- `OPEN_RISK`
- `PROFIT_PROTECTED`
- `CLOSING`
- `CLOSED`
- `MANUAL_OVERRIDE`
- `UNCERTAIN`

Treat successful order submission separately from successful position entry.

If live Bybit state differs from persisted strategy state because of an unexplained manual change, enter `MANUAL_OVERRIDE` and stop automatic mutations for that strategy.

## Reconciliation rules

Use Bybit as the source of truth for live positions and orders.

For every destructive mutation:

```text
read -> validate -> mutate -> read -> verify
```

Never remove the last effective loss protection before replacement protection is confirmed.

Never allow exit quantities to exceed the live remaining position.

On restart, reconcile live state before taking action. Do not replay destructive actions blindly.

## Rollout

Before Strategy V2 manages new positions automatically:

1. Replay candidate parameters against the forensic history.
2. Select parameters by loss prevention first, not maximum historical PnL.
3. Add deterministic tests for risk, fill, protection, and restart behavior.
4. Run only on Bybit Demo.
5. Review live results before changing any parameter or enabling real funds.

## V2.0 defaults

Use these initial Demo parameters:

- Allocate entry risk 60% / 25% / 15%.
- Place follow-up entries at 0.33R and 0.66R toward the original stop.
- Close 25% at 0.5R, 25% at 1R, and 25% at 1.5R.
- Keep the remaining 25% as the runner.
- Activate trailing protection at 0.5R with a 0.3R distance.
- Protect at least 0.05R after expected fees and slippage.

These values are V2.0 defaults, not universal optima. Change them only after
replay and Demo evidence support the change.
