# Independent zones, coordinated exchange execution

Contract investigation: [#133](https://github.com/Smarandii/cautious-crypto-bro/issues/133).
Feature delivery remains open in [#123](https://github.com/Smarandii/cautious-crypto-bro/issues/123).
This document does not enable multi-zone trading or change a running policy.

## Required result

Every explicit POI/zone receives its own full configured per-trade budget, with
the current side-specific risk multiplier applied separately. Two eligible zones
each configured for B units of stop risk reserve up to 2B in total, subject to
exchange rounding and the account-wide cap. They do not receive B/2 each.
Keep each zone's explicit entry geometry and explicit or policy-derived stop.
Do not merge disjoint ranges, move all zones to a convenient common stop, or
bypass the existing account ownership guard.

The observed post has two same-symbol, same-side RANGE candidates and no explicit
stops or targets. Each range currently derives its own policy stop. A shared-stop
combined ladder would therefore change the existing policy, even if its total
budget were multiplied by two. Raw source evidence remains private.

## Existing boundaries

- `SignalPlanning.plan_open_intents` plans candidates separately, reserves batch
  risk and routes repeated symbol/side candidates to manual review.
- `ExecutionCoordinator._assert_open_preflight` rejects a second symbol owner
  under the account mutation lock. This also applies to manual approvals.
- `ExecutionPlan._validate_v2_ladder` requires three entry legs, one stop and
  three fixed exits plus a runner. This is a per-zone plan, not a net-position
  allocation ledger.
- `PositionSupervisor` and its protection helpers compare the whole exchange
  position, average price, full-position stop and trailing state against one
  strategy. Running two copies of this supervisor would create competing writers.
- `open_stop_risk_usdt` measures a whole-position stop and outstanding entries.
  It must continue to reject unprotected or unattributed exposure; it cannot
  silently infer independent zone risk from a single exchange average price.

Bybit [position modes](https://bybit-exchange.github.io/docs/v5/position/position-mode)
separate Buy/Sell sides, not two same-side logical positions. Its
[trading-stop API](https://bybit-exchange.github.io/docs/v5/position/trading-stop)
supports full and partial TP/SL, while full-position trailing is a position
control. Partial TP/SL modification/binding semantics require specific proof;
documentation alone does not establish safe independent-zone behavior.

## Ownership and allocation contract

Keep one mutation coordinator for the physical symbol/side position. Under it,
retain independent zone records with immutable zone IDs, source lineage, frozen
plans/budgets, entry order IDs, fill quantities, remaining quantities and owned
protection/exit identifiers. Separate logical allocation from physical ownership.
Preserve three entry legs per zone; do not squeeze several zones into three legs.

Record exchange executions idempotently by execution ID and attribute them by
verified order ownership. An order-link prefix is corroboration, not sufficient
authority to adopt an unknown order. Aggregate zone quantities must reconcile
with the actual position before any further mutation. Partial fills and late fills
must update the correct zone, not whichever strategy was most recently created.
Do not use start-time windows to resolve overlapping owners.

One zone's exits must reduce only its remaining allocation. Net-position equality
alone does not prove that a specific exit filled: confirm its order/executions.
Unknown acknowledgements retain known IDs, quarantine the affected group and
notify the operator without resubmitting. External quantity, order, stop or
trailing changes pause the group before cleanup, TTL or protection mutations.

Rebuild state on restart from durable allocations and confirmed exchange events.
Missing events, unknown orders or quantity discrepancies remain unresolved;
do not distribute unexplained position quantity among zones to make totals match.

## Independent risk and protection

Freeze the normal configured budget per zone. Check all admitted zone commitments
plus existing live/pending risk under the shared mutation lock immediately before
submission. Reject/defer zones that cannot fit their full budget; preserve an
explicit reason and independent record for each. Do not divide one budget among
zones or silently shrink their allocations merely to admit more zones.

For each zone, measure remaining filled quantity against its verified stop, and
pending entry quantity against its own planned stop. Avoid counting the same
filled or pending quantity twice. Reserve outstanding acknowledged submissions
until their terminal outcome is known. A missing protection/ownership proof makes
portfolio risk unavailable and prevents additional risk.

Quantity-scoped native protection must remain effective when the app is offline.
One zone taking profit or stopping out must not leave another unprotected, and a
stale stop/exit must not consume another zone after its own allocation is flat.
Do not emulate independent stops by repeatedly overwriting the full-position stop.
Do not use full-position trailing as a separate zone's trailing state.

Before rollout, prove native paired TP/SL cancellation, quantity adjustment and
amendment behavior for partial fills/exits, cancellation races and late fills.
If the available native lifecycle cannot preserve independent allocations, keep
activation disabled and record the precise failed scenario. Do not substitute a
shared stop or an app-only protective promise to pass tests.

## Source actions, evidence and reporting

Each zone has its own approval, entry lifetime and cancellation lineage. A source
withdrawal cancels its attributable pending entries, not other sources' zones or
protective orders. Existing unscoped REDUCE/CLOSE semantics are account-wide;
allocate confirmed reductions explicitly across current zone quantities. An
instruction targeting one POI needs trustworthy zone evidence; ambiguous targeting
requires review rather than selecting a zone by current profit or entry order.

Retain original extraction/normalization and link every zone decision to its
allocation and execution evidence. Expose skipped, rejected, pending and uncertain
zones independently so partial post coverage is visible.

Bybit's merged-position accounting and logical per-zone economic results are not
interchangeable. Reconcile actual account Closed PnL separately; label per-zone
allocation estimates and unrealized amounts. Do not claim realized zone profit
by splitting account PnL using guessed ownership. Preserve frozen experiment
assignment units, risk multipliers and policy tags; report a changed sampling unit
separately rather than silently pooling it into the old cohort.

## Atomic implementation sequence

1. Add durable group/zone allocation and idempotent execution accounting. Use an
   additive schema migration, preserve all legacy rows, and expose reconciliation
   discrepancies read-only. Do not enroll legacy live positions by guessing their
   fills or activate a new protection writer in this step.
2. Prove and implement native quantity-scoped protection behind the single group
   coordinator. Exercise partial TP/SL lifecycle, unknown acknowledgements and
   offline survival. Simulated exchange tests are necessary, but are not evidence
   that Bybit's native cancellation/binding behavior matches the simulation.
3. Complete zone enrollment, entry submission, supervision, source actions and
   restart recovery as one coherent path. Preserve old single-owner positions
   until flat; never run old and new writers for the same physical position.
4. Connect multi-zone intake/approval and portfolio admission to that proven path.
   Verify full independent budgets, explicit rejection reasons and per-zone audit
   coverage. Deploy only after the complete path's integration/limited Demo gates
   pass; keep #123 open until then.

Each step needs its own issue, branch, atomic MR and validation evidence. Intermediate
infrastructure does not constitute delivered multi-zone support.

Step 1 is tracked in [#135](https://github.com/Smarandii/cautious-crypto-bro/issues/135).
It adds allocation evidence only; parent #123 remains open for the complete path.

Acceptance scenarios: two disjoint LONG zones with different default stops; SHORT
symmetry; one zone filled and another pending; partial fills in both; one zone
exiting while another remains open; late fill after cancellation; TTL affecting
only its intended entries; source withdrawal; ambiguous account-wide reduction;
external drift; lost acknowledgements; restart during enrollment/protection;
insufficient portfolio capacity; and unrelated pre-existing exposure. In each,
assert budgets, allocation quantities, actual exchange protection, order lineage,
operator escalation and absence of duplicate submissions.

## Native-child identity and schema 14

Bybit's observed native TP/SL children have an empty `orderLinkId` and an
explicit `parentOrderLinkId`. Bind their exact `orderId` only after the parent's
nonempty client link has been bound to an ENTRY in the same zone. Keep submitted
client links unique; never substitute a fabricated child link or adopt by prefix.
The allocation model retains the empty broker field; SQLite stores its absent
unique key as NULL so siblings do not collide. Snapshot matching checks the exact
parent, closing side, native TP/SL role, positive trigger and quantity bounds.
These checks are ownership evidence, not a protection-lifecycle certification.

Upgrade with the writer stopped and a restore-verified current-state backup.
The schema-13-to-14 transaction rebuilds only the order table, copies payloads
unchanged and checks all foreign keys before committing the new version. Failed
upgrades roll back the table and version together; restart retries the upgrade.
No mixed-version writers are supported. Older binaries reject version 14.

Prefer a forward fix for rollback. A downgrade must operate on a new backup of
the **current** stopped-writer state and is permitted only if there are no native
bindings (no NULL client links and no nonempty parent fields). In that case an
explicit, reviewed transaction may remove only empty `parent_order_link_id`
defaults from submitted-order JSON, restore the NOT NULL UNIQUE order-link
constraint, verify unchanged identities/executions and foreign keys, then set
version 13. If native evidence exists, retain schema 14 and use a compatible
binary; do not delete that evidence or restore an older state to force rollback.

## Native-stop coverage evidence

The transactional group snapshot reports `native_stop_coverage_verified`,
`native_stop_issues` and one `native_stop_coverage` row per bound ENTRY parent.
Its remaining quantity comes from exact entry executions minus closing
executions linked through that parent's native children. An unscoped reduction
leaves parent quantities unknown, rather than allocating the reduction by guess.
Equal account or zone quantities cannot replace per-parent conservation.

Every positive remainder needs exactly matching stop quantity from confirmed
native market SL children at that zone's frozen plan trigger. Check exact IDs,
parent links, closing side/flags, untriggered state and retained quantities.
Reject missing, changed, duplicate, oversized and stale native children, and
unscoped position TP/SL/trailing controls. Without an account snapshot coverage
is unobserved; a flat group does not certify a live protection path.

This evidence deliberately does not set `protection_verified` or enable trading.
It does not authorize cancel/replace or certify the interval between those
operations. A future single group writer must obtain fresh account state under
the shared mutation lock and satisfy the complete native lifecycle, revision,
enrollment, risk-admission and source-action contract before changing exposure.
Dynamic stop revisions require durable expected-trigger evidence; they cannot
silently overwrite the frozen stop to match external drift.

## Rationale

[Zen of Python](https://peps.python.org/pep-0020/): "Explicit is better than implicit."
Separate zone budgets and allocation evidence from the physical position.
[Grug, Chesterton's Fence](https://grugbrain.dev/) (faithful paraphrase): understand
the existing safeguard before removing it. Preserve one mutation owner and prove
the replacement protection lifecycle before allowing multiple zones.
