# Profitability and opportunity-coverage checkpoint — 2026-10-25

Tracks [issue #126](https://github.com/Smarandii/cautious-crypto-bro/issues/126).
No trading or risk setting is changed by this report.

## Evidence and method

- Export the saved-plan snapshot and Bybit Demo history with
  `scripts/export_forensic_replay.py --output BUNDLE.zip`.
- Use `cautious_crypto_bro.forensic_pnl.reconcile`, `summarize`,
  `concentration_summary`, and `bootstrap_performance_intervals` for the owned
  position cohort. Exclude incomplete or ambiguous cases from realized metrics.
- The existing size-reconciliation criterion accepts 99%–100% closure, allowing
  floating-point roundoff above 100%, with no ambiguous rows and at least one
  Closed PnL row. Report any residual positions separately; this criterion alone
  does not prove the account is flat.
- Use Closed PnL as the realized net result. Funding and trading-fee attribution
  remain diagnostics; do not add funding or subtract fees from net a second time.
  Cross-check the [execution](https://bybit-exchange.github.io/docs/v5/order/execution),
  [Closed PnL](https://bybit-exchange.github.io/docs/v5/position/close-pnl), and
  [transaction-log](https://bybit-exchange.github.io/docs/v5/account/transaction-log)
  response contracts.
- Report equal-risk R metrics alongside actual-risk-weighted return and profit
  factor. A positive sum of trade R does not necessarily imply a positive sum of
  realized money outcomes when risk sizes differ.
- Use the existing 10,000-sample bootstrap with seed 741903 and a circular
  four-trade block sensitivity check. These descriptive intervals do not remove
  source/symbol/time clustering, selection bias or prior inspection of the data.
- Inspect the already-frozen LONG risk, exit-profile, participation and entry-TTL
  cohorts with the existing analyzers. Reaching an interim count is a review
  checkpoint, not proof of an edge or permission to raise risk.

Raw source content, channel identities, transaction/order identifiers, prices and
account-dollar amounts stay in local audit artifacts. Only normalized aggregates,
counts and reproducibility metadata belong in this report.

## Results

The finalized bundle passed SHA-256 and ZIP integrity checks. Database snapshot:
2026-10-25 16:05:35 UTC; Closed PnL cutoff: 16:05:38 UTC; open-position snapshot:
16:06:04 UTC. Export completed at 16:24:48 UTC using commit
`6be33f0f485fb996976215b766e687df09864978`.
Bundle SHA-256: `433912a3276add712d4826a04dace4a9fad059ff2d72b3b420e257cbf66247ce`.

There are 83 filled owned positions, 828 execution rows, 196 Closed PnL rows and
445 funding settlements across 48 symbols. No entry order was unmatched.
Seventy-eight positions reconcile to 100% closure with no ambiguous rows. Five
are excluded: three overclosed attribution windows and two other incomplete
cases. No case qualifies only through the 99% tolerance. The account snapshot
contains one open position; the different snapshots are not an atomic account view.

| Cohort | Closed | Win rate | Net R | Mean net R | Average win/loss R | Profit factor R | Actual-money profit factor |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| All | 78 | 67.95% | -2.529 | -0.032 | 0.421 | 0.893 | 0.706 |
| LONG | 44 | 61.36% | -6.636 | -0.151 | 0.371 | 0.590 | 0.424 |
| SHORT | 34 | 76.47% | +4.108 | +0.121 | 0.479 | 1.558 | 1.156 |

The observed win rate does not overcome the payoff imbalance overall. Average
winning trades earn 0.396R; average losing trades lose 0.941R. Their descriptive
break-even win rate is 70.37%, above the observed 67.95%. Actual-risk-weighted
net return is -11.40% of summed initial trade risk, **not an account-equity return**.

Circular four-trade block 95% bootstrap intervals for mean net R are
[-0.183, +0.118] overall, [-0.391, +0.077] LONG and [-0.062, +0.301] SHORT.
All include zero. The IID intervals also include zero. These mixed-policy,
observational results neither establish a SHORT edge nor justify increasing risk.

Attributed fees total 2.507R and signed funding +0.035R. These are diagnostics
already represented in net Closed PnL. Removing the three largest R winners
leaves -6.392R overall; SHORT alone remains +1.557R. Neither sensitivity check
is an independent holdout.

| Frozen exit profile | Filled | Fully reconciled | Net R |
| --- | ---: | ---: | ---: |
| baseline | 61 | 58 | -1.204 |
| payoff_early_trail | 5 | 5 | -1.626 |
| payoff_early_tight_trail | 1 | 1 | -0.975 |
| payoff_early_tight_trail_long_015 | 3 | 3 | -0.818 |
| payoff_early_tight_trail_long_ab_015 | 4 | 4 | +0.403 |
| payoff_early_tight_trail_long_ab_020_control | 9 | 7 | +1.691 |

These small cohorts do not support choosing a winning exit policy. The 240-minute
entry-TTL cohort has 13 filled positions, 11 fully reconciled. LONG participation
has 12 take assignments (10 completed, two unresolved) and 25 skip assignments,
with no invalid assignment records. The existing analyzer reports `collecting`:
the frozen review requires at least 20 assignments per arm and no unresolved takes.

Decision: retain the current guards and frozen experiments. Resolve excluded-case
attribution before using this checkpoint as a complete strategy track record;
improve valid-post coverage through the separately tracked findings below.
The attribution investigation is tracked in
[#129](https://github.com/Smarandii/cautious-crypto-bro/issues/129).
Its [case analysis](forensic-attribution-limits-2026-10-25.md) identifies historical
owner overlap, missing-owner opening fills and one still-open position. The
reported subset is not a complete strategy track record.

Reproduce the cohort reports with `scripts/analyze_forensic_pnl.py BUNDLE.zip`
and `scripts/analyze_long_participation.py BUNDLE.zip`. The checkpoint's primary
table additionally filters reconciler-complete rows to
`abs(closed_fraction - 1) <= 1e-9`; all 78 complete rows satisfy this filter.
Use `summarize`, `cost_summary`, `concentration_summary` and
`bootstrap_performance_intervals` on that cohort and on its LONG/SHORT subsets.

## Opportunity coverage

The separate 24-hour cache-only source audit scanned 37 Telegram posts. Thirty-six
had completed processing records; one empty parser-ignored item had no processing
record. This found no supported ingestion gap in that scanned window, but does
not prove all historical posts or model decisions were handled correctly.

- [#122](https://github.com/Smarandii/cautious-crypto-bro/issues/122) found a concrete
  shorthand REDUCE normalization loss; the fix shipped in
  [#124](https://github.com/Smarandii/cautious-crypto-bro/pull/124).
- [#123](https://github.com/Smarandii/cautious-crypto-bro/issues/123) records the
  multi-zone entry limitation under the single-owner strategy guard.
- [#125](https://github.com/Smarandii/cautious-crypto-bro/issues/125) records decision
  traceability: 23 audit cache misses prevented reconstruction of the original
  decisions. Those misses are not production extraction failures.

Increasing valid-post coverage and demonstrating net profitability are separate
requirements. This checkpoint does not change risk, participation, exits or
historical source completion state.

## Engineering rationale

[Zen of Python](https://peps.python.org/pep-0020/): "In the face of ambiguity, refuse the temptation to guess."
Incomplete attribution and underpowered cohorts remain unresolved. Grug,
[Chesterton's Fence](https://grugbrain.dev/) (faithful paraphrase): understand an existing safeguard before
removing it. Existing risk and ownership guards remain in place during review.
