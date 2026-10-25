# Excluded forensic cases — 2026-10-25

Tracks [#129](https://github.com/Smarandii/cautious-crypto-bro/issues/129) and
qualifies the [profitability checkpoint](profitability-checkpoint-2026-10-25.md).
This investigation changes no reconciliation rule or trading behavior.

## Evidence

Use the checkpoint's finalized bundle, SHA-256
`433912a3276add712d4826a04dace4a9fad059ff2d72b3b420e257cbf66247ce`,
and its read-only database snapshot. Inspect the shared reconciler's entry-order
lineage, symbol/side time windows, close executions and Closed PnL records.
Compare opening order IDs and their `ccb-v2-<owner>-<leg>` links against saved
plans and executed position actions. Keep identifiers and raw rows private.

| Category | Cases | Evidence | Interpretation |
| --- | ---: | --- | --- |
| Overlapping saved strategies | 2 | An earlier plan's entry leg fills after the next same-symbol strategy starts. A subsequent window contains that leg and a close row created before its own start but updated inside it. | A start-time partition cannot separate these historical owners. One window has no attributed closes; the other appears 191.30% closed. |
| Opening owner missing from snapshot | 2 | Each window includes one additional opening fill with a different `ccb-v2` owner prefix. Neither owner matches any saved plan in the snapshot, and neither order matches a stored executed position action. Total opening quantity matches 108.29% and 126.63% of the recorded owner's entry quantity, respectively, equal to their apparent closure fractions. | Extra opening quantity explains the overclosure mechanically. The owner and its original risk/stop are unavailable; its PnL cannot safely be charged to the recorded plan. |
| Position still open at cutoff | 1 | No close executions or Closed PnL rows occur in its window; the account snapshot has the corresponding open position. | The realized result is not final. |

The two overlapping cases belong to the same historical symbol sequence. This
is not evidence that the current deployment permits new overlapping owners.
The two missing-owner cases are not established manual trades: their order links
use this application's convention. Nor does that convention alone recover the
missing saved policy or justify treating them as part of another strategy.

All three overclosed windows have closing-execution quantities consistent with
their attributed Closed PnL quantities. The symptom is therefore not explained
by duplicate Closed PnL rows alone. Zero unmatched **currently open** entry orders
does not prove complete historical order ownership.

## Decision and limits

Keep all five cases excluded from completed-position results. Moving a close
between windows using its creation time would not resolve merged position cost
basis or the earlier entry leg that filled after the next owner began. Accepting
all `ccb-` opening orders would invent ownership and initial risk for missing plans.
Do not weaken the overclosure guard or manufacture a whole-system profit result.

The 78-case checkpoint is a descriptive subset, not a complete strategy track
record. Its absence of an overclosure flag is not a proof that every historical
cash flow has perfect ownership. Recovering an older authoritative database or
deployment archive could permit further attribution; the present snapshot cannot.
Future attribution changes must retain separate coverage for overlapping owners,
unknown owner prefixes, unrelated account trades and genuinely open positions.

For reproduction, call `forensic_pnl.reconcile(BUNDLE)` and inspect rows where
`complete` is false. In `bybit/executions.jsonl`, compare zero-`closedSize` Trade
fills against each row's stored `entry_order_ids`, side and `[start, end)` window.
Cross-check nonzero-`closedSize` fills with `bybit/closed_pnl.jsonl` by order ID,
including both creation and update times. Compare owner prefixes against every
saved plan, not only the exporter-selected filled subset. Do not publish raw rows.

## Engineering rationale

[Zen of Python](https://peps.python.org/pep-0020/): "In the face of ambiguity,
refuse the temptation to guess." Missing owners and merged historical positions
remain unallocated. [Grug, Chesterton's Fence](https://grugbrain.dev/)
(faithful paraphrase): understand a safeguard before removing it. The existing
overclosure exclusion prevents these extra fills from silently changing net-R.
