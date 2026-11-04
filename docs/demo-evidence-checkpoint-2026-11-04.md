# Demo evidence checkpoint — 2026-11-04

Scope: [#359](https://github.com/Smarandii/cautious-crypto-bro/issues/359),
under [#248](https://github.com/Smarandii/cautious-crypto-bro/issues/248).
This audit separates intake, recorded interpretation, execution and reconciled
outcomes. It does not establish complete participation or a profitable strategy.
The broader magnitude and architecture goals remain open.

## Capture and boundaries

The inspected local release was v2.2.64 (`1661aa6`). The forensic capture ran
from `2026-11-04T20:52:08Z` through `2026-11-04T20:52:40Z`. Each exchange
history has its own as-of and coverage bounds; the separate SQLite backup has
its own snapshot time. These are not an atomic exchange/database snapshot.

The existing restricted publisher exported exchange histories without market
candles. Its terminal-order retention file was copied to an isolated private
directory before passing it to the exporter: that optional exporter path is a
**writer**, even though its live SQLite input is read-only. The completed archive
and source backup were copied and SHA-256 verified before deleting only the
known temporary export directory. Production retention and database writers,
model calls, replay, order mutations and publication dispatch were not used.
Raw archives, source identifiers/content, account statistics and receipts remain
private and are not repository or release assets.

## What the evidence supports

| Question | Finding and limit |
| --- | --- |
| Did supported visible publications reach intake? | The bounded settled history scan reported complete intake coverage. This applies only to its inspected window and supported visible groups. |
| Are recorded skips evidence of missed entries? | The inspected publication-window skips were lifecycle actions. Their original recorded account contexts were available and did not authorize an executable matching position. This validates the skip against recorded context, not a reconstruction of historical exchange state. |
| Does a no-op prove a post had no useful instruction? | No. Raw extraction, normalization and context are different stages. Filtered updates/actions and extraction no-ops require original-input semantic review; a completed source row alone cannot certify correctness. |
| Does a closed strategy certify its whole-trade result? | No. Physical entry identity, terminal settlement, quantities, fees and signed funding remain required. Missing terminal facts remain unknown. |
| Does example-scale magnitude establish profitability or an exit-policy winner? | No. A completed frozen-policy lifecycle matches the requested example scale through captured Bybit Closed-PnL and execution records. This is a magnitude observation only; complete/incomplete outcomes and frozen exit/participation cohorts remain separate. |

The visible-history window was `2026-11-03T20:53:13.908703Z` through
`2026-11-04T20:53:13.908703Z`, with a 120-second settling delay and a
2,000-message limit per source. Its scope excludes edits, deleted/inaccessible
history, interpretation correctness and execution success. The separate
processing-`updated_at` report and the earlier stored-publication snapshot have
different populations; do not subtract their totals to infer missing posts.

## Outcome and cohort discipline

Reconciliation uses the existing whole-lifecycle implementation in
`src/cautious_crypto_bro/forensic_pnl.py`, including incomplete records.
Descriptive expectancy, average win/loss, payoff and IID/circular-block bootstrap
intervals are retained privately. Closed-PnL rows, win rate and wallet movements
are not substitutes for this analysis. Completed-only selection and unresolved
historical terminal evidence limit inference.

Frozen risk percentages are serialized decimal strings. Normalize them
numerically before comparing values; `3`, `3.0` and numeric `3` must not
accidentally define different sizing cohorts. Partition by original risk,
exit profile, entry TTL and participation arm/mechanism. Use the verified
plan-creation cutover separately from fill/close times. Frozen records do not
contain every global configuration field, so a timestamp is not proof of a
fully identical policy. Never relabel old plans using today's settings.

The magnitude observation was checked against original frozen sizing, settled
physical entries, a matching non-probe Closed-PnL row and actual closing execution
quantity. Its private receipt retains the audit identity; account values and
order identifiers are not public evidence. An observed large loss also meets
the operator's two-sided magnitude example; it cannot establish profitable scale
or justify changing exits.

The inspected historical settlement gaps predate prospective terminal retention;
absence of an old zero-fill terminal row is not evidence of cancellation. Keep
such cases uncertified rather than manufacturing a backfill. Current open
lifecycles also remain incomplete even when entry settlement is verified.

## Decisions and remaining work

Preserve the approved sizing, portfolio limit, entry guards and exit policy.
This pass demonstrates no new ingestion defect requiring a speculative change.
It does not certify every original caption/image interpretation. Continue
original-input review with provenance intact; do not send private inputs to a
new model or silently replace original inputs with edited history.

[#312](https://github.com/Smarandii/cautious-crypto-bro/issues/312) remains the
independent-zone native-trailing contract investigation. Shared position-level
trailing is not proof of independent zone behavior; activation still requires
the broker contract and restart/offline protection evidence.

[Zen of Python](https://peps.python.org/pep-0020/), “Explicit is better than
implicit,” informed the separate evidence stages and cohort definitions.
[Grug, Chesterton's Fence](https://grugbrain.dev/#chestertons-fence), faithfully
paraphrased: understand existing boundaries before removing them. Here that
means retaining settlement/protection guards while investigating evidence gaps.
