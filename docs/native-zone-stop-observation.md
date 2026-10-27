# Native partial stops after an independent exit

Tracked in [#142](https://github.com/Smarandii/cautious-crypto-bro/issues/142),
under [#139](https://github.com/Smarandii/cautious-crypto-bro/issues/139).
This is evidence for the independent-zone contract, not feature activation.

## Observed on Bybit Demo, 2026-10-25 UTC

The isolated runner paused the normal application without restarting it. DOGEUSDT
had no position or orders before each case. Two market entry parents opened the
same side with quantities of one and two units and different attached partial
stops. Each generated one distinct stop with its exact parent link, planned
trigger and filled quantity. A separate reduce-only market order then closed
one unit; its own order history confirmed the complete fill.

Both LONG and SHORT produced the same fresh-snapshot result after a two-second
settling interval:

| Evidence | Remaining quantity in units |
| --- | ---: |
| Physical position | 2 |
| A's original partial stop | 1 |
| B's original partial stop | 2 |

B's original stop identity and trigger remained unchanged. A's stop did not
disappear or shrink despite A's confirmed independent exit. Cleanup subsequently
closed the test position and verified no test-symbol orders remained for each
side. The normal application resumed without restart; its production database
was not written by the probe. Exact local receipts and order identities remain
private under `audit-artifacts/bybit-demo-20261025-231113-918752`.

## Consequence and limits

An attached partial stop is not a logical allocation lifecycle manager. A group
coordinator must resolve stale A protection before it can consume B's remaining
allocation. Merely observing native stops for both entries does not prove safe
multi-zone execution. This probe did not trigger the stale stop, demonstrate
cross-zone consumption, test paired OCO amendment/cancellation, inject late fills
or lost acknowledgements, or prove offline/restart safety. Those remain #139
gates. Do not replace different zone stops with one shared stop or enable #123.

[Bybit's trading-stop documentation](https://bybit-exchange.github.io/docs/v5/position/trading-stop)
describes position-size adjustment and warns that one-sided amendments break
TP/SL binding; it does not promise per-source allocation ownership.

## Repeat the bounded observation

```powershell
python scripts/run_bybit_demo_e2e.py --execute-demo --zone-protection
```

This places Demo orders. The runner uses the currently deployed application image
and mounts the current probe script read-only, recording its hash. It refuses an
already paused/stopped app, requires a flat test symbol, bounds aggregate test
notional, and only resumes the app after matching terminal cleanup receipts. A
passing observation means the experiment completed, not that independent-zone
protection is safe. No mainnet option exists.

## Paired cancellation observed — 2026-10-27

The #168 rehearsal used deployed revision `eafb56432799c7fb2a343001c155dbcd16c5c1c7`.
Both cases started flat and unowned on DOGEUSDT, with aggregate entry notional
bounded to 50 USDT per case. A and B had unequal one-unit/two-unit allocations
and four distinct exact parent-linked TP/SL children. A's independent reduction
was confirmed before its original TP was canceled once.

| Case | A protection after independent close | A TP after explicit cancellation | A peer SL | B original pair and two-unit remainder |
| --- | --- | --- | --- | --- |
| LONG | One-unit SL remained | Deactivated | Deactivated | Preserved |
| SHORT | One-unit SL remained | Deactivated | Deactivated | Preserved |

Both cancellations were acknowledged. Fresh account reads and terminal history
established the result; private before/after journals independently verified B's
exact child IDs, quantities and triggers. Cleanup needed reconciliation retries,
then verified no test-symbol position or orders in both cases. The normal app
resumed without restart and was healthy; both existing app-managed positions
retained their expected native stops. No production allocation rows were written.
Exact identities and receipts remain private in
`audit-artifacts/bybit-demo-20261027-102347-202917`.

This observation supports retiring this particular attached pair by canceling its
exact TP child. It does **not** remove the interval between A's independent exit
and pair retirement, during which stale A protection still exists. It does not
prove trigger/cancel races, late or partial fills, one-sided amendments, restart
recovery or offline allocation safety. No multi-zone activation is justified;
#139 and #123 remain open. Lost-acknowledgment handling has synthetic regression
coverage; this native rehearsal received acknowledgments in both cases.

## Run-owned cleanup evidence

For the bounded #190 process-loss rehearsal, use the same runner with
`--execute-demo --zone-process-recovery`. A worker flushes two entries and four
exact parent-linked native children to its run's journal, then deliberately exits
with code 86 without executing Python cleanup. The orchestrator confirms that
worker is terminal, waits at least three seconds and opens a new adapter context
in the other process. It reloads only that known run's journal, checks executions,
net exposure and every original child identity/quantity/trigger, then performs
attributable cleanup. The saved cleanup-close flag prevents replay of an
unresolved close. Both side receipts and independently verified cleanup remain
required for success and app resume respectively.

Malformed or foreign evidence remains unresolved. This is not an operator command
for adopting arbitrary historical journals, nor a guarantee against host power
loss, exchange-side races or a complete independent-zone offline lifecycle.
Synthetic subprocess/adapter tests verify this recovery path; native conclusions
require a separate exclusive Demo run. No #190 native result is claimed here yet.

For #168, use `--execute-demo --zone-pair-cancellation` with the same runner.
This attaches distinct TP/SL pairs to A and B, confirms A's independent reduction,
then cancels A's original TP once. It records whether A's original SL retires or
remains open, checks B's original pair and remaining quantity, and retains exact
identities and terminal history in the private ownership journal. Lost cancellation
acknowledgments are reconciled without resubmitting. Both LONG and SHORT receipts
are required. Missing observation evidence fails the suite; verified flat cleanup
still permits app resume. Arrange an exclusive Demo window with the runtime owner
before invoking it. This mode does not enable multi-zone trading or prove restart,
late-fill or offline safety. Synthetic adapter tests validate probe wiring only;
native exchange conclusions require actual Demo receipts.

The runner mounts its private evidence directory at `/probe-evidence` and supplies
`CCB_PROBE_EVIDENCE_DIR`. Each case starts only from a flat test symbol and writes
an ownership journal there. Exact submitted order links are flushed before the
exchange call; acknowledged order IDs and observed parent-linked children are
retained. These private journals survive removal of the temporary probe container.
File flushing and atomic replacement protect against probe-process termination;
this is not a tested guarantee against host power loss or storage hardware failure.

Cleanup rejects unknown orders, including unrelated protective orders and generic
`ccb-action-` links. Before closing exposure, it reconciles all execution pages
since the flat start against the run's exact identities and the fresh net position.
It submits only that attributable quantity with `reduceOnly`; it does not ask a
generic CLOSE action to expand its size from a later account snapshot. An
unresolved close acknowledgment is not submitted again.

Unavailable evidence storage, unknown executions, unmatched exposure or unknown
protective identities leave cleanup unresolved and prevent automatic app resume.
In particular, a full-position stop without attributable identity is not adopted
merely because its price, quantity or symbol matches. Operator reconciliation may
be required in such a failure. Keep the private journal and receipts; never
force a cleanup-success receipt or resume over unresolved ownership.

These checks do not provide an atomic exchange lock against human trading or a
second account writer. The exclusive rehearsal window remains required. They do
not prove independent-zone exit races, paired-order lifecycle or offline safety.

[Grug, Testing](https://grugbrain.dev/) (faithful paraphrase): exercise real system
boundaries with a small reliable end-to-end suite. That principle informed this
native observation instead of treating a simulated stop lifecycle as broker proof.
