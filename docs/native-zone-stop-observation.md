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

[Grug, Testing](https://grugbrain.dev/) (faithful paraphrase): exercise real system
boundaries with a small reliable end-to-end suite. That principle informed this
native observation instead of treating a simulated stop lifecycle as broker proof.
