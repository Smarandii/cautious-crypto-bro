# Testing and operations

Run from repository root. Local checks need Python 3.12 and uv.

## Quality gate

```sh
uv sync --frozen --group dev
uv run ruff format --check src scripts tests
uv run ruff check src scripts tests
uv run pyright
uv run pytest -q
uv lock --check
uv build
uv run pre-commit run --all-files
git diff --check
```

`uv run pytest -q` always reports branch coverage and fails below the
`fail_under` floor in `pyproject.toml`. The floor is a ratchet: it sits just
under the measured value and is raised whenever coverage improves, so a drop
fails the gate rather than passing unnoticed. Raise it, never lower it.

To find what is untested:

```sh
uv run pytest -q --cov-report=html
# report written to htmlcov/
```

Default pytest uses offline/mocked boundaries and real temporary SQLite.
Live Demo testing is opt-in; passing tests does not prove complete reliability.

## Configured helpers

These Bash examples use Compose configuration, Redis and persistent app state.
Helpers may initialize schema, populate caches or call paid providers.

```bash
pyapp() {
  docker compose run --rm -T --entrypoint /app/.venv/bin/python app "$@"
}
pyapp scripts/replay_telegram_post.py '<telegram-post-url>'
pyapp scripts/replay_telegram_post.py '<telegram-post-url>' --intent-only
pyapp scripts/set_execution_policy.py
pyapp scripts/set_execution_policy.py --risk-pct 1
cat guidance.txt | pyapp scripts/set_guidance.py --global
cat guidance.txt | pyapp scripts/set_guidance.py --channel -1001234567890
```

Post replay does not submit orders or mark the source processed. Historical MARKET
plans can fail against today's prices. Capital comes from live wallet balance.

```bash
mkdir -p audit-output
docker compose run --rm -T -v "$PWD/audit-output:/audit-output" \
  --entrypoint /app/.venv/bin/python app scripts/audit_recent_signals.py \
  --hours 5 --output-dir /audit-output
```

Add `--cache-only` to avoid fresh model calls; keep each audit's evidence separately.
Offline comparison: `uv run python scripts/replay_strategy_v2.py <forensic-bundle>`.
The replay ranks candidates only on the earliest chronological training segment,
then reports a separate middle validation segment and latest chronological holdout.
The 2026-10-07 bundle's validation/holdout results have already been inspected;
they are no longer untouched evidence for future policy selection.
It includes the current policy baseline and no-E3 allocation hypotheses; neither
the no-E3 hypotheses nor a positive replay result is a recommendation. No-E3
profiles are counterfactual only because current V2 policy requires positive risk
allocation for each entry leg. Policy-valid candidates also enforce the production
trailing-distance and minimum-lock constraints before ranking.

For a fresh read-only snapshot from the running Demo app, export and replay inside
the container so the tool reads Docker's SQLite volume and credentials:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/export_forensic_replay.py \
  --output /tmp/ccb-forensic.zip
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip --top 15
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/ccb-forensic.zip
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/ccb-forensic.zip \
  --prospective-live-long-risk
```

The public dashboard's win rate, average win/loss, profit factor, and expectancy
must come from complete V2 positions in that forensic bundle—not raw account
Closed P&L rows, which can contain partial exits. Build an uploadable static
snapshot from Docker's live database and the same archive with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/export_dashboard_data.py \
  /state/cautious_crypto_bro.sqlite3 /tmp/ccb-dashboard/data.json \
  --forensic-bundle /tmp/ccb-forensic.zip
docker cp cautious-crypto-bro-app-1:/tmp/ccb-dashboard/data.json docs/data.json
docker cp cautious-crypto-bro-app-1:/tmp/ccb-dashboard/data.js docs/data.js
```

This writes whole-position performance to `strategy_pnl`; account-level partial
records stay separately labeled under `account_pnl_records`. Without the
forensic bundle, the page deliberately leaves strategy win/loss metrics
unavailable instead of substituting the account-record win rate.

To compare the frozen reduced-E3/later-target challenger with the current policy
without rerunning the full grid:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip \
  --compare-late-targets-reduced-e3
```

This candidate was surfaced after reviewing the 2026-10-07 search results. Its
paired bootstrap is descriptive and IID; only later unseen Demo trades can
provide prospective evidence.

The reviewed bundle's ablation found that reducing E3 allocation alone worsened
holdout (−1.951R vs current −1.341R). Keeping current E3 allocation while moving
to later targets/earlier trailing scored +0.795R, but worsened 10 of 18 cases;
two ADA cases contributed +2.291R of paired improvement. The combined frozen
candidate scored +0.511R and improved 11 of 18. These are post-hoc diagnostics:
the combined profile is a broader shadow hypothesis, not proof that E3 reduction
itself improves profitability.

An offline E3 activation-delay diagnostic is available with
`--e3-delay-diagnostics-only`. In the reviewed bundle, delaying current-policy
E3 activation by 240 minutes improved holdout from −1.341R to −0.938R, still
negative; delay had negligible benefit for the combined challenger at 60 minutes
and reduced it slightly at 240 minutes. Delay thresholds were evaluated
retrospectively, so this is not a policy recommendation.

Fresh Docker/Bybit Demo export at `2026-10-07T17:09:29Z` contained the same 57
filled cases and 52 complete positions; the 1-minute candles were refreshed but
no post-freeze closed trades had accrued. Replaying the frozen combined challenger
on those refreshed candles (superseding earlier 16:47Z replay values) returned
+0.297R on holdout versus −1.602R for current
policy (PF 1.08 vs 0.72; WR 66.7% vs 61.1%). The paired delta was +1.899R across
18 cases, but its IID 95% interval was [−0.374R, +4.826R]; 11/18 cases improved.
Because this holdout has already been repeatedly inspected, it is no longer
independent validation. Both prospective shadows still had 0/20 eligible cases.

The refreshed full grid's best training-ranked profile returned +0.080R on
holdout, while the best training-ranked no-E3 counterfactual returned −0.888R;
neither establishes positive expectancy, and zero E3 allocation is invalid under
the current production policy. The current-policy replay grouped by *observed*
entry fills returned +4.635R for E1-only (23 cases), +4.386R for E1+E2 (13), and
−12.245R for E1+E2+E3 (21; −4.084R on its 9 holdout cases). This cohort is
selected by adverse price movement that causes deeper limit fills, so it is a
diagnostic for E3 timing/exposure—not a causal estimate that canceling E3 would
improve results. A 240-minute E3 delay improved current-policy replay holdout
from −1.602R to −1.199R but remained negative; simulated 0.50R and 0.75R stop
caps after E3 worsened holdout to −2.018R and −2.191R.

A new research-only E3 confirmation test waits until E3 is touched, then enters
at a later 1-minute candle close back through E2, charging the configured taker
fee and preserving E3's planned stop-risk weight. On the further 17:27Z snapshot
(same 57 cases and 52 complete positions), it
underperformed baseline in training (−2.592R vs −2.127R) and holdout (−1.702R
vs −1.544R); the paired holdout change was −0.159R, IID 95% interval
[−1.178R, +0.766R], with only 6/18 cases improved. It slightly reduced mean
loss size but also reduced mean win size. Reject this variant; do not freeze it
for prospective review or apply it to live entries. The simulation also omits
slippage and excludes the confirmation candle's pre-entry OHLC movement after
the close fill; subsequent candles are still replayed.

Risk-weight sensitivity with the *current* exits also rejects simple
front-loading: holdout was −1.959R for 80/15/5, −2.088R for 79/20/1, and
−2.190R for 89/10/1 versus −1.544R baseline. Combining smaller E3 allocations
with later exits was less bad but not positive: 80/19/1 returned −0.212R and
80/15/5 returned −0.095R; the frozen 70/25/5 profile returned +0.341R. Every
paired 95% interval included zero. Thus the earlier equal-risk sizing point
estimate does not translate into a reliable entry-weight policy.

The refreshed exit-only profile (current entry weights, later targets and
earlier trailing) returned +0.593R on the 18-case holdout (PF 1.25, WR 72.2%)
versus −1.544R baseline. Its paired IID 95% interval was [−1.369R, +6.006R];
a circular 4-trade block bootstrap gave [−1.186R, +5.601R]. Only 5/18 paired
cases improved. Two ADA SHORT cases contributed +2.291R of the +2.136R net
paired delta; without those two, the remaining delta was −0.155R. Therefore
this apparent holdout gain is highly concentrated and is not sufficient to
promote the candidate. The frozen prospective exit shadow remains the relevant
test, and the 17:27Z archive still had 0 eligible post-freeze closed trades.

Sizing diagnostic on the same 52 closed positions: summed net outcome was
+0.698R while realized net dollars were −$229.05, because average initial stop
risk was $53.14 on winners and $70.96 on losers. Uniformly reweighting every
trade to the sample's mean initial risk ($58.63) gives a +$40.92 point estimate,
but uncertainty is overwhelming: total-P&L IID bootstrap 95% interval
[−$551, +$605], and circular 4-trade block-bootstrap interval [−$553, +$639].
This assumes linear P&L/fee scaling and unchanged entries/fills, and does not
model a new order-sizing policy. It motivates investigating why the live risk
budget is only partially used on shallower fills, but is not evidence to raise
E1 allocation or change sizing.

Extraction-confidence filter check on the same 52 reconciled trades found no
usable threshold: 44/52 scored 0.99 (68.2% wins, −$195.63 net, PF 0.80), while
the remaining eight were split across scores 0.92, 0.93, 0.96, 0.97 (one trade
each) and 0.98 (four trades, −$37.88). The tiny lower-score groups swing from
large losses to wins and cannot establish a monotonic relationship; quartile
cuts are also dominated by ties at 0.99. Confidence is extraction certainty,
not predicted profitability, and should not be used as a live entry filter.

Source×direction health filter discovery (post-hoc; not a production
recommendation): allow a source+side until it has at least three fully closed
prior trades, then suppress new signals when the prior three net outcomes sum to
≤0R. Each historical decision used only outcomes closed before that entry; the
unfiltered baseline outcomes of suppressed signals remain in the paper history.
Across the 52 positions, this rule would have kept 40 and suppressed 12. Kept
net P&L was −$37.84 versus −$229.05 baseline (+$191.21); early70 was slightly
worse (−$175.50 vs −$167.01), while the latest30 moved from −$62.03 to +$137.66.
The latest30 paired delta was +2.739R, but IID 95% interval [−0.409R, +6.301R]
and circular 4-trade interval [0R, +5.478R] are wide, and the three-trade window
was selected after inspecting these data. This is a new prospective paper
shadow, frozen at `2026-10-07T17:42:41Z`, and only later fully reconciled
positions count toward prospective evaluation:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/ccb-forensic.zip \
  --prospective-source-side-health-shadow
```

At the freeze, the 17:27Z archive had 0 eligible post-freeze positions; the
Docker SQLite P&L sync watermark was 14:34Z. Keep the source/side gate shadowed
until a new independent sample accumulates; a historical gain is not proof of
future profitability.

Fresh Bybit Demo export at `2026-10-07T17:52:05Z` again contained 57 filled
cases and 52 complete positions (138 Closed P&L rows); no new reconciled
positions had closed. The source/side, long-risk, primary-exit, and exit-only
prospective shadows therefore each remained at 0 eligible cases. On the refreshed
1-minute candles, current-policy replay was −2.123R in train, +0.539R in
validation, and −1.570R in the already-inspected 18-case holdout. Delaying E3
activation 240 minutes reduced current-policy holdout loss to −1.167R, but train
and holdout remained negative. Post-E3 stop caps at 0.50R and 0.75R returned
−2.052R and −2.160R on holdout, both worse than current policy. The frozen
combined challenger returned +0.314R (PF 1.08, 67% wins) on this holdout; this
was +1.884R paired against current policy, but its IID 95% interval was
[−0.400R, +4.814R] and only 11/18 cases improved. It remained negative on LONGs
(−1.321R) and positive on SHORTs (+1.635R), so the aggregate gain is direction-
dependent and may be source/time-confounded. The candidate's train result was
−1.030R versus −2.123R current, and its validation result was +2.347R versus
+0.539R current; all segments are already inspected. This repeatedly examined,
small sample is not independent validation and does not justify changing live
policy. These diagnostics use the refreshed archive and supersede earlier
candle-refresh values above; the actual closed-trade sample did not change.

An experimental `--time-stop-diagnostics-only` mode tests close-confirmed exits
after 1h, 4h, 12h, or 24h when the 1-minute close is at or below 0R, −0.25R, or
−0.50R. No profile beat current policy on the training segment (baseline
−2.123R; best time-stop profile −2.720R), so there is no training-selected
challenger to promote. The train-selected 1h/−0.25R rule returned −2.876R on
holdout versus −1.570R baseline. The best holdout-only change, 12h/−0.50R,
improved holdout by +0.784R but remained negative at −0.787R, had a paired IID
95% interval of [−0.542R, +2.527R], and performed worse in training (−3.683R
versus −2.123R). Reject time-based exits for now; the interval and contradictory
chronological segments do not establish an improvement. The entire grid is
post-hoc and the holdout has already been repeatedly inspected.

Reproduce the time-stop research diagnostic without altering any live orders:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip \
  --time-stop-diagnostics-only
```

Stop geometry was also tested independently from entry spacing. The 2x stop-only
profile kept E2/E3 prices fixed; the 2x stop-and-grid profile doubled both the
planned stop distance and E2/E3 spacing while resizing each leg to preserve its
share of nominal 1R. The stop-and-grid profile was training-selected: net was
+2.952R train, +2.047R validation, and −0.539R holdout, against current policy
at −2.123R, +0.539R, and −1.570R respectively. Its paired deltas were positive
in all three segments, but all IID intervals included zero; the holdout delta
was +1.031R with a wide [−4.276R, +7.202R] interval. On holdout its average
win/loss magnitude ratio improved to 0.846, but 50% wins remained below the
54.2% breakeven rate, so it still lost. Stop-only 2x did worse on holdout
(−0.848R), showing that moving the entry grid also mattered in the simulation.
The replay cannot model changed fills, minimum sizes, slippage, or liquidation
risk. This is a fixed prospective shadow—not a live change—frozen at
`2026-10-07T18:13:30Z`; its first eligible count was 0/20 on the pre-freeze
17:52Z archive. A direct read-only Bybit Demo API check at 18:17Z confirmed no
new Closed P&L rows since that archive and no executions since the freeze:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip \
  --prospective-stop-distance-shadow
```

Actual Closed P&L on the 52 complete positions also shows a strong E3-fill risk
marker: positions filled only at E1 or E1+E2 netted +$668.04 across 32 trades
(31 wins, 1 loss), while positions filled through E3 netted −$897.09 across 20
trades (5 wins, 15 losses; average win $44.35, average loss −$74.59, payoff
ratio 0.595, breakeven win rate 62.7%, PF 0.20). The E3-filled group lost on both
directions (LONG −$560.52, SHORT −$336.57). This is a realized-risk diagnostic,
not causal evidence for canceling E3: trades reach the deeper E3 limit after a
larger adverse move, so fill-pattern cohorts are selected by price path. It
does, however, support prioritizing the frozen geometry shadow that reduces
simulated full-E3 fills. Its holdout simulation shifted the three-leg cohort
from 9 cases/−4.018R to 5 cases/−2.741R, while total holdout remained negative
and uncertain; cohort composition differs, so this is not a matched causal
comparison.

To evaluate that frozen challenger prospectively without changing live orders,
export a fresh bundle and run:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip --prospective-shadow
```

This mode includes only positions opened after the challenger's fixed
`2026-10-07T15:53:00Z` freeze time whose actual closed size reconciles. It
compares simulated candidate exits/fills with the current policy on the same
future candles; it does not execute candidate orders. Treat results as shadow
evidence, not a production recommendation. The report marks 20 reconciled cases
as ready for review, not as automatic proof of profitability.

A second, exit-only candidate is tracked separately with
`--prospective-exit-shadow`. It preserves current entry weights and depths
(60/25/15%, 0.33/0.66R) while changing trailing activation to 0.40R and targets
to 1/2/4R with 15/20/25% reductions. In a 33-profile exploratory sweep over
the 52 fully reconciled historical positions, it was the top training-ranked
profile; on the 16-position chronological holdout it returned +0.874R, PF 1.42,
and 81.25% wins versus current −1.262R, PF 0.76, and 68.75% wins. Its paired
IID-bootstrap 95% interval was [−1.355R, +5.995R], so the sample remains too
small and uncertain to establish an improvement. The profile was frozen at
`2026-10-07T16:28:40Z`; only later fully reconciled positions enter this shadow
comparison. This is offline analysis only and does not change orders or live
policy:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip \
  --prospective-exit-shadow
```

The exit-only candidate did **not** improve the raw average-win/average-loss
ratio on that holdout: it was 0.329 versus 0.346 for current policy. Instead,
its average loss fell from 1.058R to 0.687R while average win fell from 0.366R
to 0.226R, and win rate rose from 68.75% to 81.25%. Its estimated break-even
win rate was about 75.3%, below the observed 81.25%. Conversely, the profile
with the highest training win/loss ratio (0.440) had only a 62.5% holdout win
rate and lost 1.311R. This indicates that maximizing payoff ratio alone is not
enough; expectancy depends on payoff ratio and win rate together. Both findings
remain small-sample historical diagnostics, not prospective proof.

A separate directional-sizing shadow is available with
`--prospective-long-risk-shadow`: it linearly scales LONG PnL/risk to 25% and
leaves SHORT positions unchanged. In the 52-position archive this changed the
historical point estimate from −$229.05 (−7.51% risk-weighted return) to +$63.11
(+3.01%). It was positive in both chronological segments (+$16.62 early70,
+$46.49 latest30), but every 95% IID-bootstrap risk-return interval included
zero; this counterfactual also assumes proportional fills/fees and ignores
minimum size, slippage and market impact. It is a risk-allocation hypothesis,
not evidence that LONG signals improve or a change to live sizing. The candidate
was frozen at `2026-10-07T16:40:50Z`; only later reconciled positions enter its
shadow:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/ccb-forensic.zip \
  --prospective-long-risk-shadow
```

The directional effect is confounded by source and time. The largest source
cohort (29 positions) moved from +$73.65 in its first 17 positions to −$172.29
in its latest 12. Another 10-position cohort made +$133.58 overall, but 8 of its
positions were SHORTs (all winners, +$204.79) while its 2 LONGs lost $71.21.
These are post-hoc, small cohorts; they argue against treating a static side or
source filter as proven. Keep the aggregate long-risk overlay in shadow and
inspect source × side × time before drawing a live-policy conclusion.

Accounting correction: Bybit's Closed P&L includes opening/closing fees and
funding. The analyzer retains mapped settlement totals for reconciliation but
does not add them to Closed P&L again. The previous analyzer double-counted
$2.58 of mapped funding on this archive; corrected 52-position metrics are
−$229.05 net, $24.74 average win, −$69.99 average loss, a 0.354 win/loss ratio,
73.88% break-even win rate, 69.23% observed win rate, and 0.795 profit factor.
The risk-scaling and channel figures above were recalculated after the fix.
See Bybit's [closed-P&L API](https://bybit-exchange.github.io/docs/v5/position/close-pnl)
and [P&L calculation](https://www.bybit.com/en/help-center/article/Profit-Loss-calculations-USDT-Contract).

Execution-cost decomposition of that same 52-position archive: inferred gross
price P&L was −$115.91, fees were $115.72, mapped signed funding was +$2.58,
and closed-P&L net was −$229.05. Fees were about 99.8% of the *magnitude* of
gross P&L, but gross P&L was already negative; eliminating all fees would still
leave this sample down roughly $115.91. The 36 net winners averaged $24.74 net
and $1.62 in fees each (6.55% of average net win). Net losers averaged $3.58
fees each. LONGs were −$346.43 gross / −$389.54 net across 20 positions;
SHORTs were +$230.52 gross / +$160.49 net across 32. Approximately $103.24
(89.2%) of total fees were on `Market` closed-P&L rows, versus $12.48 on
`Limit` rows; a position can contribute to both categories. This is a reason to
measure maker/taker mix and avoidable market exits in shadow, not evidence that
converting exits to limits would preserve fills or improve expectancy. The
analyzer now prints `cost_decomposition` cohorts using closed-P&L `openFee`,
`closeFee` and signed settlement funding, with gross price P&L derived as
`closedPnl + fees - funding`.

Portfolio exposure check: among the 52 reconciled positions, peak overlapping
initial stop risk was about $460.64 across 7 positions (the ledger records 1%
 risk per trade, so 7% nominal aggregate risk). An earlier exploratory sizing
counterfactual reported +$23.36 at a 5% cap and +$13.07 at a 3% cap, but its
exact overlap/cap semantics were not recorded. Treat those figures as
superseded by the reproducible model audit below; overlapping exposure remains
an important risk monitor.

### Portfolio-cap model audit

An explicit reconstruction was run against the refreshed 52-position archive:
set the cap to 3× or 5× mean planned per-trade stop risk ($72.26), release risk
at each reconciled close, and proportionally scale only a new overlapping
position to the remaining capacity. This assumes dollar P&L and fees scale
linearly and does not model changed fills. The 3× cap produced +$24.98 total,
but its paired improvement was +$254.03 and the three largest positive trade
deltas contributed +$228.76; without those three, the cap still loses about
$203.78. Its train/validation/holdout results were −$208.86 / +$14.93 /
+$218.92, with 17/52 positions scaled. The 5× cap remained negative at
−$126.36; its entire +$102.68 paired improvement came from only two scaled
holdout positions, and it still lost in total. IID and circular-block bootstrap
intervals for both caps' mean dollar P&L included zero. These results do not
support a portfolio-cap change.

The exact model is implemented by the analyzer and runs with its standard
report:

```sh
uv run --no-project --no-cache python scripts/analyze_forensic_pnl.py <bundle.zip>
```

It uses fixed budgets of 3×/5× the sample's mean planned stop-risk ($216.77 /
$361.29 here), not a live equity-linked cap; closes release capacity, and only
new overlapping risk is scaled. The 3× IID and circular-block 95% intervals for
mean P&L were [−$9.87,+$10.17] and [−$10.68,+$11.08] per position; for 5× they
were [−$14.94,+$9.59] and [−$15.20,+$10.26]. All include zero. These results
do not establish a tradable edge or support a production cap.

The reproducible model does not match the earlier +$23.36 (5×) / +$13.07 (3×)
figures; do not combine them or treat them as independent confirmations. The
older calculation remains unexplained and should not guide risk policy.

The exporter uses Bybit Demo order/execution history, settlement records and
closed-PnL rows, plus 1-minute candles. It writes no credentials into the
archive and refuses to overwrite an existing output path. The PnL analyzer groups
partial closes by symbol, side and strategy lifetime, and reports only positions
whose closed size reconciles to actual entry fills. The replay is a historical
diagnostic; a candidate must also hold up on later trades before changing policy.
The forensic lineage includes only non-content intent metadata (source channel ID,
entry type, extractor confidence, risk percentage and planned max loss) for cohort
diagnostics; message text and summaries are omitted. Confidence is an extraction
score, not a probability of profit. PnL summaries include an approximate Wilson
interval for win rate and the observed win/loss-magnitude breakeven rate, plus
dollar-weighted return on summed initial risk and average risk size for winners
versus losers. Post-hoc channel cohorts are exploratory, not independent validation.
The analyzer also reports a retrospective long-side risk-scaling sensitivity; it
assumes PnL and fees scale linearly with size and ignores altered fills, slippage,
minimum order sizes and market impact. Its deterministic IID bootstrap intervals
do not account for temporal dependence. Neither is a policy recommendation.

Corrected shadow diagnostic (offline, separate from the frozen primary run):

```sh
uv run python scripts/reconcile_shadow_actions.py --run-dir <archived-run> \
  --engine <frozen-replay_engine.py> --output <new-output-directory>
```

Requires the archive's config, snapshot, inputs, summary and actual ledger JSON.
Matches fully filled lifecycle orders before comparing exchange timestamps;
unfilled actions remain unresolved. Pins the existing engine hash, preserves
policies/window, refuses output overwrite, and reports baseline discrepancies.
This execution-based diagnostic cannot reconstruct unexecuted source actions.

Other tools in `scripts/` cover Telegram authorization, image-provider smoke checks,
cooldown stress checks and configurable Bybit smoke planning.
`smoke_bybit_trade.py --execute` submits Demo orders; default behavior is preview.

## Live Bybit Demo integration

```sh
python scripts/run_bybit_demo_e2e.py --execute-demo
```

Requires Docker Compose, configured Demo credentials, and an empty DOGEUSDT
position with no DOGE orders. One operator at a time. LONG/SHORT cases submit
real Demo orders: initial notional ≤ $75, planned total ≤ $200, frozen risk ≤ $5,
with 10% initially reserved for quote movement.

The runner pauses the app, uses temporary SQLite databases, then unpauses the same
process only after verified cleanup. Other symbols remain untouched; their native
protection continues while app reconciliation pauses. No Telegram messages, LLM
calls, production migrations or deployment occur.

Scenarios: staged entry/rebasing; stop handoff and target caps; fresh-process restart;
interrupted exit installation; changed/removed stops; TP fill and entry freeze;
Russian 25% REDUCE/rebalance; accepted-but-unconfirmed reduction quarantine; CLOSE
and zero-position/order cleanup. Partial-fill timing and fills during installation
are also tested deterministically.

Evidence: `audit-artifacts/bybit-demo-<timestamp>/results.jsonl` and `manifest.json`
record checks, cleanup receipts, revision, dirty files, hashes and app identity.
If cleanup fails, the app stays paused. Inspect DOGE exposure and evidence before
unpausing; host/Docker failures also need manual inspection.

## Fresh Demo checkpoint — 2026-10-07 18:32Z

A read-only Bybit Demo export completed at `2026-10-07T18:32:50Z`: 57 filled
Strategy V2 cases, 52 size-reconciled positions, 138 Closed P&L rows, and 581
execution rows. No position closed after the prospective-shadow freeze points:
the combined entry/exit challenger, exit-only challenger, stop/grid geometry,
long-risk scaling shadow, and source/side-health filter each still have 0
eligible cases toward their 20-case review target. Keep all candidates in paper
shadow; this archive adds refreshed candles, not independent outcome evidence.

The unchanged 52-position sample remains −$229.05 net, with $24.74 average
wins, −$69.99 average losses, a 0.354 dollar payoff ratio, and −$4.40 dollar
expectancy per position. The same records sum to +0.698R unweighted across
positions (+0.013R expectancy per position), while winners carried $53.14 mean
initial risk and losers $70.96. These differ because dollar P&L weights larger
positions more heavily while summed R gives each position its own initial-risk
denominator; the positive unweighted R sum does not mean the account made money.
Any sizing change remains unproven: the linear rescaling counterfactual assumes
unchanged fills, proportional fees, and no minimum-size or market-impact effects.
The analyzer's 10,000-resample percentile bootstrap estimates the payoff ratio
at 0.280–0.458 (IID) or 0.290–0.437 (circular blocks of four positions). Mean
expectancy intervals still include zero: −0.178R to +0.195R IID and −0.190R to
+0.210R with blocks; dollar expectancy intervals were −$17.41 to +$8.10 and
−$18.47 to +$9.35 respectively. Thus the average-win/average-loss imbalance is
clear in this sample, but neither positive nor negative mean expectancy is
established. Bootstrap intervals are descriptive for this small, nonstationary
sample and do not replace the frozen prospective shadows.
For an interpretable break-even target at the observed 69.23% win rate, holding
loss size constant requires mean net winners of at least $31.11 (+25.7%); holding
win size constant requires mean net losses no worse than −$55.67 (20.5% smaller).
If the observed payoff ratio stays fixed instead, win rate must reach 73.88%.
These are one-variable-at-a-time arithmetic thresholds, not predicted candidate
results.

The frozen exit-only challenger was also replayed on the refreshed candles for
the same 52 positions. Translating each simulated R by that position's actual
initial risk estimates a paired +$403.57 change versus current replay; split
changes were +$101.56 train, +$141.95 validation, and +$160.06 holdout. This is
not a reliable account-dollar forecast: the same translation puts current
replay at −$404.32 versus realized −$229.05, and only 12/52 cases improve. The
challenger's all-sample payoff ratio in R is lower (0.268 vs 0.324), despite
higher replay expectancy, and the reviewed holdout gain was concentrated in two
ADA SHORTs with a bootstrap interval spanning zero. Keep the exit candidate in
shadow; it may improve expectancy, but it does not fix the payoff ratio or
establish positive production P&L.

An actual-price excursion diagnostic used only full 1-minute bars between the
entry fills and reconciled close (excluding any entry-fill and exit partial
minutes). Of the 36 realized winners, mean favorable excursion was +0.751R
(median +0.738R); only 6 reached +1R and none reached +2R. Of 16 realized
losers, mean excursion was +0.264R and only one reached +0.5R. For the 20 full-
E3 positions, favorable excursion before the E3 fill averaged +0.218R; none
reached +0.5R before adding E3. A +0.25R pre-E3 gate would have allowed 8/20
historical E3 fills, 7 of which ultimately lost. This is path-conditioned
diagnostic evidence, not a causal cancellation counterfactual: removing E3
changes average entry, exposure, fees, and eventual exits. It does show that
2R/4R take-profit assumptions have little support in these realized paths, and
that a +0.5R-before-E3 confirmation rule would almost always disable E3 without
separating the winners. Continue to investigate loss avoidance and realized
size allocation rather than assuming wider targets will fix the payoff ratio.

A focused fixed-exit screen tested taking 100% of the remaining position at
+0.5R, preserving the current entry weights/depths, fees, and adverse-first
candle ordering. This is an offline counterfactual with actual historical entry
fills held fixed. On all 52 positions, replay expectancy was −0.044R versus
−0.051R for current exits, but the paired gain was only +0.342R total; removing
the three largest positive trade deltas made it −0.262R. The 26/10/16
train/validation/holdout paired changes were +0.622R / +0.063R / −0.343R. Only
4/16 holdout cases improved, with IID and circular-block intervals spanning
zero; the holdout delta after removing its three largest positive cases was
−0.802R. Reject this rule as an optimization candidate: it did not transfer
to holdout, and the all-sample improvement is concentrated. Do not freeze or
apply it to production.

Early-trail candidate (research-only, frozen `2026-10-07T18:45:48Z`): preserve
current entry sizing/depths and 0.5/1/1.5R targets, but activate trailing at
0.20R with 0.10R distance. This geometry satisfies the existing policy's
minimum-lock invariant. It was motivated by the +0.5R excursion separation,
then selected after a small retrospective comparison; treat its old results as
post-hoc, not independent validation. On the 52 fully reconciled historical
positions, replay returned +1.471R train, +1.529R validation, and +1.567R
holdout, but every paired 95% IID interval included zero (respectively
[−1.145R,+8.909R], [−1.829R,+4.822R], [−2.211R,+8.218R]). The candidate
produced 50/52 wins in the risk-scaled dollar replay, but only $7.72 mean win
against a $77.25 mean loss (0.10 payoff ratio, 90.9% breakeven win rate).
Every circular 4-position block interval also crossed zero (train
[−0.834R,+8.221R], validation [−1.786R,+4.214R], holdout
[−2.657R,+8.518R]).
Its apparent $231.37 total depends on only two losses and linear dollar scaling;
the same conversion estimates current replay at −$404.32 versus −$229.05
actually realized. This is a fragile small-win/high-win-rate hypothesis, not a
profitability claim. Its prospective shadow is isolated with
`--prospective-early-trail-shadow`; the 18:32Z archive predates its freeze and
therefore contributes 0 cases. Require at least 20 later fully reconciled
positions plus positive paired expectancy and a block-bootstrap interval that
does not cross zero before considering any policy change.

## Fresh Demo checkpoint — 2026-10-07 18:56Z

A new read-only Bybit Demo export completed at `2026-10-07T18:56:11Z` with 57
filled V2 cases, the same 52 size-reconciled positions, 138 Closed P&L rows,
581 execution rows, 312 funding settlements, and refreshed one-minute candles
for 28 symbols. No completed V2 positions were opened after any of the seven
frozen paper-shadow timestamps; all prospective shadow counts remain zero.
The existing 52-position result is therefore unchanged: −$229.05 realized,
36/52 winners, $24.74 mean win, −$69.99 mean loss, 0.354 payoff ratio, and
−$4.40 expectancy per complete position. The existing account-wide
`docs/data.json` snapshot is a different aggregation (151 Closed P&L records,
+$55.45) and was generated at 12:42Z; its source ledger last synced at 14:34Z.
Do not treat that account-level partial-close series as the V2
reconciled-position result.

Additional replay-only checks on the refreshed candles did not identify a
robust fix. Tightening the post-E3 loss cap to 0.50R improved train to −0.705R
but worsened holdout from −1.791R to −2.035R; a 0.75R cap worsened holdout to
−2.381R. Delaying E3 activation to 240 minutes improved the current-policy
holdout to −1.388R but left it negative. Requiring an E3 touch followed by a
later close through E2 changed holdout by −0.102R (18 cases; four-case circular
block 95% interval [−0.830R,+0.639R]). Time-stop variants also left holdout
negative; the best observed holdout delta was +1.022R at 720 minutes / close
≤−0.50R, but that holdout still lost −0.769R and the paired interval crossed
zero [−0.066R,+2.658R]. These are post-hoc screens, not prospective evidence;
keep live policy unchanged and continue collecting frozen shadows.

The training-ranked grid also surfaced one additional depth/exit profile for
prospective shadow only: preserve 60/25/15% risk weights, move entry depths to
0.25/0.50R, trail at 0.40R with 0.30R distance, and reduce 15/20/25% at 1/2/4R.
On the 52 complete historical positions, its train/validation/holdout net was
−0.657R/+2.072R/+0.517R versus baseline −2.073R/+0.345R/−1.262R. Holdout
average win/loss was 0.388 (0.304R/−0.782R) versus baseline 0.346
(0.366R/−1.058R). The paired holdout delta was +1.779R (7/16 cases improved);
the circular four-case bootstrap interval was [+0.045R,+3.672R], while the IID
interval crossed zero [−0.567R,+4.797R]. These are not prospective results:
the profile came from a broad grid, the dataset and holdout have already been
inspected, and the block interval is fragile at n=16. It is frozen at
`2026-10-07T19:10:00Z` under `--prospective-depth-exit-shadow`; only later
fully reconciled positions count. Require at least 20 such positions, positive
paired expectancy, and a circular-block interval excluding zero before any
policy review. This is one more paper candidate, not a production change.
On the same holdout, its paired delta remained positive when lifecycle/funding
events were omitted (+1.756R) and when the assumed fee rate was raised from
0.055% to 0.075% (+1.841R); corresponding four-case block intervals also
excluded zero, but every IID interval still crossed zero. These are sensitivity
checks on the same already-inspected 16 trades, not additional independent
evidence.
The gain is concentrated: only 7/16 paired cases improved, and the three
largest favorable deltas contributed +2.233R; the other 13 cases sum to a
−0.454R paired delta. Thus the positive aggregate is not a broad per-trade
improvement and the historical holdout does not establish a general edge.
For any future shadow review, report the contribution concentration and require
the paired delta to remain positive after removing its three largest favorable
case deltas, in addition to the existing sample-size and block-interval gates.
The paired replay JSON now reports both the top-three positive contribution and
the delta after removing those three cases, including in prospective reports.
A repeat of the full parameter grid with `--reconciled-only` retained 52/57
cases and selected the same training-ranked depth/exit profile. Across the top
five training-ranked profiles, every holdout paired delta became negative after
removing its three largest positive cases (range −0.841R to −0.069R); the
training adjusted deltas were also all negative (−1.030R to −0.744R). The
current profile is therefore not a broad improvement under this robustness
screen; keep it shadow-only. Future historical searches can use
`--reconciled-only` to exclude open or size-unreconciled positions from ranking.
Simulated holdout fill mix was E1-only 10/16, E1+E2 1/16, and full E3 5/16,
versus 6/16, 2/16, and 8/16 under baseline. The candidate's five full-E3
simulations still lost 0.688R in aggregate. This is consistent with reduced
deep-fill exposure, but the fill cohorts are generated by price paths and do
not establish that selectively suppressing E3 would cause the same gain.

A further research-only ranking mode, `--rank-by-concentration-adjusted-train`,
orders candidates by training paired delta after subtracting the three largest
positive case deltas. On the same 52 reconciled cases, the top profile scored
+0.172R after this adjustment but still had negative training net (−1.613R)
and negative holdout net (−1.001R); its validation was +0.362R. The current
policy ranked third with a zero adjusted paired delta. This suggests the grid
contains a modest concentration-adjusted training improvement, but no
candidate demonstrated positive overall holdout performance; no profile is
eligible for production. Validation and holdout remain reporting-only and are
not used in ranking. The focused ranking test, full suite (453 passed, 79.70%
coverage), and Ruff check passed on 2026-10-07.

## Prospective refresh — 2026-10-07 19:15Z

The first fresh snapshot after the 19:10Z candidate freeze completed at
`2026-10-07T19:15:02Z`. It still contains 57 filled V2 cases, 52 fully
reconciled positions, 138 Closed P&L rows, 581 executions, and 312 funding
settlements; replay confirms zero fully reconciled positions opened after the
freeze. All seven prospective shadows remain at zero eligible cases. Therefore
this refresh supplies no candidate-performance evidence; the live 52-position
baseline is unchanged and every policy candidate remains shadow-only. A separate
read-only query of the live app SQLite during this check found zero execution
plans created after the freeze, confirming there were no newer V2 entries to
reconcile at that time.

## Fresh Demo checkpoint — 2026-10-07 19:37Z

A new read-only Docker/Bybit Demo export completed at `2026-10-07T19:37:35Z`.
It still contains 57 filled V2 cases, 52 fully size-reconciled positions, 138
Closed P&L rows, 581 executions, and 312 funding settlements. The realized
position result is unchanged: −$229.05 net, 36/52 wins, $24.74 average win,
−$69.99 average loss, 0.354 win/loss ratio, and −$4.40 expectancy per
position. Gross price P&L is −$115.91 before $115.72 in fees; therefore fees
are material but are not the sole cause of the loss. Winners averaged $53.14
initial stop risk, versus $70.96 for losers, consistent with partial entry
fills under-allocating exposure on winners and deeper fills increasing risk on
losers. This is a path-conditioned association, not a causal E3 effect.

The frozen three-loss source/direction health rule was also replayed
walk-forward on these same reconciled cases. On the earliest 36 cases, filtering
after three prior closed losses suppresses eight cases whose combined realized
P&L was +$8.49, making the filtered segment $8.49 worse than its −$167.01
baseline. On the latest 16, it suppresses four cases totaling −$199.69 and
changes the segment from −$62.03 to +$137.66; one skipped case was a +$29.60
winner. This late improvement is concentrated in four decisions and was
discovered on an already-inspected historical sample; it is not independent
evidence. A window sensitivity over one through six prior losses did not remove
the training/holdout instability. Keep the rule paper-only and do not tune its
window on this holdout.

The app's account P&L sync timestamp remains `2026-10-07T14:34:14Z`, but a
read-only ledger/log check found 15 later source messages completed with no new
intents; the latest attempted intent was rejected by the existing DOGE strategy
ownership preflight. Since this code syncs account P&L only when a batch has an
intent or position action, the old timestamp is explained by a lack of later
actionable batches, not evidence of a sync crash. There are still zero cases
after the 19:10Z depth/exit freeze and zero after the 17:42Z source/side-health
freeze. No new prospective performance evidence is available; all candidates
remain unapproved for production.

The concentration-adjusted policy grid was rerun against the 19:37Z candle
refresh. It retained the same top profile (65/25/10 risk, 0.33/0.66R entry
depths, 0.50R/0.30R trailing, current 0.5/1/1.5R targets): −1.613R train,
+0.362R validation, and −1.001R holdout, versus current policy at −2.073R,
+0.345R, and −1.262R. The candidate's +0.172R training delta after excluding
its three largest positive cases is not enough to offset its overall negative
training and holdout results. Refreshing candles did not reveal a profitable
profile or make the historical holdout independent; no production setting is
recommended.

A payoff-focused fixed-entry exit sensitivity tested earlier trail levels and
later partial targets against the same 26/10/16 chronological splits. Early
locks illustrate the win-rate trap: at 0.20R activation / 0.05R distance the
replay win rate was 92% in training and 100% in validation/holdout, but mean
winner was only about 0.13R in training and 0.12R in holdout against a roughly
1R mean loss. This raises win rate while worsening the payoff imbalance.
The strongest runner point estimate used current entries, 15/20/25% reductions
at 1/2/4R, and 0.40R activation / 0.10R trailing distance. It returned
+0.038R train, +2.526R validation, and +1.703R holdout; holdout payoff ratio
was 0.422 versus 0.346 baseline. However, its paired delta fell to −0.328R in
training and −0.140R in holdout after excluding the three largest positive
cases. The IID paired intervals also crossed zero (train [−0.422R,+5.544R],
holdout [−0.504R,+6.937R]); validation and holdout had only 10 and 16 cases.
Thus the apparent improvement is concentrated and does not support promotion
or a new shadow freeze. Continue to judge candidates on payoff, expectancy,
contribution concentration, and new prospective cases together.

Directional sizing was rechecked on the same realized-dollar ledger. Scaling
LONG outcomes/risk to 25% while leaving SHORTs unchanged gives +$16.62 in the
earliest 36 positions and +$46.49 in the latest 16, versus baseline losses of
−$167.01 and −$62.03. The point estimate is directionally consistent, but the
break-even LONG multiplier varies from 0.318 in the early segment to 0.571 in
the latest segment, and the 95% circular-block total-P&L intervals at 25% LONG
risk remain very wide: [−$406.28,+$385.69] early and [−$257.51,+$363.92] late.
The full-sample block interval is [−$476.17,+$561.27]. Therefore this is still
only the existing 25%-LONG shadow, not a stable sizing recommendation. Its
prospective eligible count remains zero; the latest observed position is still
before its freeze period.

An exploratory pre-entry regime check tested whether 1h/4h/24h return aligned
with the trade side. The 4h countertrend cohort was +$56.28 train, −$28.02
validation, and +$89.79 holdout (17/6/7 positions); its holdout gain had 69.5%
of winner dollars in the three largest wins. If unavailable-history trades are
allowed rather than excluded, the two unknown cases also lose $108.29. The
24h countertrend grouping was profitable in early history but slightly negative
in the latest 16. These small, screened cohorts do not establish a reusable
market-regime filter; do not promote or freeze one.

To prospectively compare the more plausible directional risk hypothesis, the
read-only analyzer now supports `--prospective-long-risk-curve-shadow`. It
compares 0%, 25%, 50%, and 100% LONG risk, with SHORT risk unchanged, using only
fully reconciled positions opened after its separate `2026-10-07T19:50:18Z`
freeze. This is an offline linear-size sensitivity, not simulated altered
fills and not a live policy change. The current archive correctly reports
0/20 eligible positions. The 455-test suite passed with 79.70% coverage, and
Ruff passed after this addition.

## Prospective risk-curve refresh — 2026-10-07 19:57Z

A read-only Docker/Bybit Demo export completed at `2026-10-07T19:57:04Z` and
still contains 57 filled V2 cases, 52 fully reconciled positions, 138 Closed
P&L rows, 581 executions, and 312 funding settlements. No position opened
after the 19:50:18Z sizing-curve freeze has closed; the new risk-curve shadow
remains 0/20. Several older positions are still active in the app, but their
entries predate the freeze and they do not count toward prospective evidence.
The export adds refreshed candles only; realized performance is unchanged and
no live order or sizing policy was changed.

## Dashboard metrics source — 2026-10-07 20:12Z

The dashboard snapshot now separates full-position V2 performance from the
account's raw Closed P&L records. Whole-position cards use only fully
reconciled trades from the forensic bundle: 52 positions, 69.23% wins, $24.74
average win, −$69.99 average loss, 0.3535 payoff ratio, 0.7955 profit factor,
−$4.40 expectancy per position, and −$229.05 net. The 151 account-level
partial-close rows (+$55.45, 80.79% positive) remain visible only in a clearly
labeled operations block; they no longer drive the strategy scorecard. When a
forensic bundle is unavailable, the scorecard explicitly withholds strategy
metrics instead of substituting partial rows.

The refreshed `docs/data.json` and `docs/data.js` were generated from the
Docker SQLite snapshot and the existing read-only Bybit Demo archive. The new
exporter accepts `--forensic-bundle`; tests cover excluding incomplete
positions and separating account rows from whole-position outcomes. The full
suite passes: 459 tests, 79.74% coverage. No trade execution or policy code was
changed.

## Dashboard and shadow refresh — 2026-10-07 20:26Z

A fresh read-only Bybit Demo export completed at `2026-10-07T20:26:14Z` with
the same 57 filled V2 cases, 52 complete positions, 138 Closed P&L rows, 581
executions, and 312 funding settlements. Whole-position performance is
unchanged: 36 wins / 16 losses, $24.74 average win, −$69.99 average loss,
0.3535 payoff ratio, −$4.40 expectancy, and −$229.05 net. The frozen
LONG-risk curve still has 0/20 eligible post-freeze positions. The app's
account P&L sync watermark remains `2026-10-07T14:34:14Z`; account rows are
not substitutes for completed V2 trades. `docs/data.json` and `docs/data.js`
were regenerated from the live Docker SQLite snapshot and fresh archive. The
focused dashboard tests passed (15), and the full suite passed (459 tests,
79.74% coverage). No live order or policy was changed.

The dashboard now also shows seeded IID and circular four-position block
bootstrap intervals for mean whole-position P&L, mean R, and win/loss payoff.
On this snapshot, the block-bootstrap 95% intervals are −$18.47 to +$9.35 per
position and −0.190R to +0.210R; both include zero. The 69.23% observed win
rate also does not prove a positive edge: the sample is small, and the observed
break-even win rate is 73.88%. These intervals communicate uncertainty; they
do not establish causality or justify changing live settings.

## Intent outcome classification — 2026-10-07 20:38Z

A read-only query of the live Docker SQLite snapshot found 86 executed, 40
failed, and 3 skipped intents. The 40 failures classify as 20 risk-budget
guards, 5 position-ownership guards, 12 stale requests, 1 minimum-order guard,
and 2 exchange/API or other errors. Thus the raw failure percentage is not an
execution-error rate; most failed intents were intentional no-order safety
outcomes. The dashboard now shows these categories instead of presenting the
entire failed count as failed exchange execution.

The 38 stale manual-delivery position-action failures are historical queue
orphans. Schema migration 7→8 created the durable delivery table without
backfilling older pending records; recovery intentionally marks such orphans
older than 24 hours failed rather than replaying stale trade actions. The
38 stale position-action records date from 2026-09-16 through 2026-09-23, and
8 stale intent records date from 2026-09-14 through 2026-09-17. Current source ingestion inserts
delivery rows transactionally with intents/actions, and a recovery loop scans
every 30 seconds. Do not retry or reopen these old records. There have been no
new plans/actions since 2026-10-07 14:34Z, so there is no new strategy sample.
The refreshed dashboard-classification tests and full suite pass: 460 tests,
79.79% coverage. No live trading policy or orders were changed.

The dashboard scorecard now also exposes the arithmetic payoff target and
average initial stop-risk by outcome. At the observed win/loss count, average
winner must reach $31.11 or average loser shrink to −$55.67 to break even,
holding the other value fixed; realized initial risk averaged $53.14 on winners
and $70.96 on losers. This highlights the sizing/fill-mix contribution to the
dollar-versus-R divergence. It is diagnostic evidence, not proof that changing
entry weights will improve returns; the full suite passes 460 tests at 79.84%
coverage after this addition.

## E3 post-fill risk-trim diagnostic — 2026-10-07 20:43Z

A research-only replay tested trimming only when E3 filled. It waits until that
one-minute candle closes, skips the trim if the protective stop was touched in
the same bar, charges a taker fee, and reduces just enough quantity to cap
projected stop-out loss at 0.75R, 0.85R, or 0.95R. Slippage and order-book depth
are not modeled. The 52 reconciled cases were split 26/10/16 chronologically;
this historical holdout is already inspected and is not independent evidence.

All three caps worsened the 16-case holdout versus baseline (−1.262R): paired
deltas were −0.639R (0.75R cap), −0.182R (0.85R), and −0.155R (0.95R). Every
four-position block-bootstrap interval crossed zero, and each paired delta
became more negative after removing its three largest positive contributions.
Small training improvements did not transfer to validation/holdout. Reject
this trim variant; do not freeze or apply it to production. The updated full
suite passes: 461 tests, 79.84% coverage. No live policy or orders were changed.

## Maker/taker fee-model sensitivity — 2026-10-08

The replay's single 0.055% fee estimate charges the same rate to every fill.
Actual V2 entry executions in the forensic archive were 58 maker limit fills
($12.10 fees on $53,853 notional; 0.0225%) and 53 taker market fills
($39.35 on $66,175; 0.0595%). Matched V2 exits were 42 maker limit fills
($3.96 on $17,336; 0.0228%) and 73 market taker fills ($61.38 on $102,171;
0.0601%).

Added `--fee-schedule-diagnostics-only`, a historical sensitivity check using
the archive's median maker/taker fee rates (0.0200% / 0.0550%). E1 is priced at
its observed maker status; scale-in legs and TP limits are modeled as maker;
reclaim-at-close, protective/market exits, and other market closes are priced
as taker. This is not fill-level execution modeling: strategy changes may alter
whether or when a limit fills, and slippage/queue position are not modeled.

On 52 reconciled cases, the current-policy maker/taker schedule improved the
paired historical result by +0.364R versus the scalar schedule (holdout +0.129R),
but current policy remained negative in train (−1.884R), validation (+0.390R),
and holdout (−1.133R). The frozen later-target/reduced-E3 challenger scored
+0.824R in the already-inspected holdout under the maker/taker schedule; its
paired holdout delta versus current was +1.957R, but fell to −0.093R after
removing the three largest positive paired cases, and its IID 95% interval
crossed zero. The circular-block interval disagreed, underscoring unstable
inference with 16 holdout positions and clustered outcomes. Do not promote this
challenger from this archive; collect prospective, fully reconciled shadow
cases. The fee correction alone does not make current policy profitable.

The dedicated test container passed the full suite after this diagnostic was
added: 462 tests, 79.84% coverage. No live settings, orders, commit, or push were
changed.

## Realized loss concentration — 2026-10-08

The reconciled 52-position distribution shows the payoff gap is not mainly a
single-outlier problem. Across 16 losers, the median was −$76.45 (10th–90th
percentile −$79.57 to −$52.01), versus a largest loss of −$89.39. The three
largest losses contributed 22.2% of total loss dollars. Winners had a $20.09
median and $24.74 mean; the three largest winners contributed 19.3% of total
winner dollars. This looks more like repeated near-stop losses paired with
small wins than a few catastrophic tails. It argues for investigating setup
selection and exposure allocation rather than tuning only emergency loss caps.

Directional outcomes remain a candidate for prospective sizing review, not a
live rule: LONG was 11/20 winners with mean loss −$73.40, while SHORT was 25/32
winners with mean loss −$65.59. These post-hoc cohorts are small, and the
separate frozen directional shadows have no eligible post-freeze closes yet.

For context, linearly scaling LONG realized P&L and risk to 25% while keeping
SHORTs unchanged yields +$63.11 over the full sample (PF 1.10, payoff ratio
0.489, arithmetic break-even win rate 67.1%); the unscaled sample was −$229.05
(PF 0.80, payoff 0.354, break-even 73.9%). The point-estimate improvement is
positive in both early 36 (+$183.63 paired delta) and latest 16 (+$108.52).
However, those gains are highly concentrated: removing the three largest
favorable per-trade sizing deltas leaves only +$5.93 in the early 36 and changes
the latest 16 to −$73.82. In the latest segment, three losing LONGs account for
+$182.34 of sizing benefit, while reducing the four winning LONGs gives back
$73.82. Four-position circular-block 95% intervals for paired dollar
improvement also cross zero in every segment: full [−$79,+$687], early
[−$106,+$511], latest [−$81,+$288]. This is a selected, concentrated
counterfactual—not evidence to change live risk—and assumes linear fills/costs.
The prospective report now includes paired block intervals and top-three
concentration, and its readiness gate requires 20 LONG closes, not 20
mixed-direction positions. Do not promote the 25% multiplier unless new
prospective results also stay positive after removing the three largest gains
and the paired block interval excludes zero.

## Prospective-shadow refresh — 2026-10-08 01:11 Asia/Tbilisi

A fresh read-only Docker/Bybit Demo snapshot completed at `2026-10-07T21:11Z`:
57 filled V2 cases, 52 complete-size-reconciled positions, 581 execution rows,
138 Closed P&L rows, and 312 funding settlements. These counts match the prior
snapshot; the refresh adds candle history, not new realized-outcome evidence.
All eight frozen shadows (combined entry/exit, exit-only, stop/grid, early-trail,
depth/exit, LONG risk, LONG risk curve, and source/side health) still have zero
eligible post-freeze closed positions toward their 20-case review targets.
Audit found the LONG-risk shadows had been counting unaffected SHORT positions
toward that sample-size gate. The reports now require 20 post-freeze LONG
outcomes (while retaining all trades in the portfolio comparison); their
affected-case counts are still zero, so readiness remains collecting.
The paired-sizing report now prints IID and circular-block 95% intervals,
improvement after removing the three largest positive deltas, and counts of
LONG wins harmed versus LONG losses helped. The full suite passes: 464 tests,
79.84% coverage; Ruff lint/format pass. Pyright could not launch in the test
image because its bundled Node runtime requires the missing `libatomic.so.1`;
type-check status is unverified. No live settings or orders were changed.

A separate read-only position-list query observed four nonzero Demo positions;
they remain open and are excluded from closed-position win/loss and expectancy
metrics. The query showed a stop-loss value on each, but this is not a validation
of stop execution or a profitability result. No live settings or orders were
changed. Continue collecting prospective outcomes; do not promote candidates
from the already-inspected historical sample.

## Profitability evidence refresh — 2026-10-08 01:34 Asia/Tbilisi

A fresh read-only Docker/Bybit Demo archive completed at `2026-10-07T21:34Z`.
It still contains 52 complete positions from 57 filled V2 cases; the account
P&L sync watermark remains `2026-10-07T14:34Z`. Thus the realized scorecard has
not improved: 36/52 wins (69.23%), average win $24.74, average loss −$69.99,
profit factor 0.7955, expectancy −$4.40/position, net −$229.05. Mean realized
R is only +0.0134R/position, with the four-position block-bootstrap interval
crossing zero. A high hit rate is not proof of signal quality or positive edge.

The loss is not just an exit-payoff issue. Across complete positions, gross
price P&L is −$115.91, fees are $115.72, and funding is +$2.58; fees are about
99.8% of the absolute gross price loss. Winners carried $53.14 mean initial
risk versus $70.96 on losers, so larger dollar exposure on losing trades also
helps explain why dollar expectancy is worse than mean R. This motivates
prospective testing of sizing consistency and execution-cost reduction, not a
claim that either will make the strategy profitable.

On the already-inspected 18-position holdout, the frozen 70/25/5 entry and
late-target challenger moved from −1.879R to +0.054R (+1.933R paired), but the
IID 95% interval crosses zero and removing its three largest positive case
deltas makes the paired improvement −0.143R. It remains a shadow candidate,
not a promotion. Using observed maker/taker rates instead of the scalar fee
estimate improves the holdout replay by +0.131R, but the current-policy
holdout is still −1.748R; this is a measurement correction, not a live edge.
Additional fresh-candle diagnostics do not support a simple E3 stop cap: 0.50R
and 0.75R caps returned −2.031R and −2.709R on holdout, respectively, versus
−1.879R baseline. Post-E3 trims also worsened holdout at 0.75R (−2.530R),
0.85R (−2.068R), and 0.95R (−2.036R); paired block intervals crossed zero and
top-three-adjusted deltas were negative. Waiting for a later close reclaim
through E2 before entering E3 was worse in train (−0.465R paired) and holdout
(−0.106R), with intervals crossing zero. Reject these variants for now.

A paired no-E3 allocation diagnostic (75% E1 / 25% E2, with current exits)
also demonstrates why payoff ratio cannot be optimized alone. On the latest
16-case chronological holdout, average win/loss magnitude ratio improved from
0.346 to 0.539, and average loss magnitude fell from 1.058R to 0.752R; however,
win rate fell from 68.75% to 56.25%, expectancy worsened from −0.079R to
−0.101R, and net moved from −1.262R to −1.615R. The paired delta was −0.353R
(IID 95% interval [−1.616R,+0.720R]); after removing its three largest positive
case deltas it was −0.635R. This counterfactual is not a recommendation; it
shows that fewer deep fills or a better win/loss ratio do not by themselves
make the full strategy more profitable.

A separate fixed 80/20/0 E1/E2/E3 risk split with current exits was also
replayed on the same 52 candles and event histories (entry fills are simulated,
not held fixed). It returned −3.577R versus −2.627R for current weights; the
paired change was −0.950R. The 26/10/16 split deltas were −0.400R, +0.243R,
and −0.793R. On holdout, average loss improved from −1.048R to −0.800R, but win
rate fell from 68.75% to 56.25%, expectancy worsened from −0.071R to −0.120R,
and maximum drawdown rose from 3.844R to 4.287R. The paired IID/block 95%
intervals were [−2.641R,+0.746R] / [−2.422R,+0.513R], and the holdout change
after removing its three largest positive case deltas was −1.122R. Reject this
allocation; do not continue mining entry weights on this already-inspected
sample.

A 2×2 entry/exit decomposition on the same 52 complete positions (chronological
splits 26/10/16) points more toward exits than entry weights. On the 16-case
holdout, changing exits alone (current 60/25/15 entries; 0.40R trail activation;
1/2/4R targets) reached +0.874R versus −1.262R baseline. Changing only entry
weights to 70/25/5 reached −1.373R; combining both reached +0.757R. Training
paired deltas were +1.072R for exits alone, −0.707R for entry weights alone,
and +1.097R combined. However, all IID intervals crossed zero; the exits-only
holdout delta was concentrated in its top three cases (−0.705R after removing
them), and the combined delta became −0.056R after that adjustment. Keep the
already-frozen exit-only candidate as the leading *shadow* to evaluate, not a
production change; its latest archive still has zero post-freeze closes.

The broad historical grid search was stopped because it is repeated mining of
this same small sample. The frozen combined challenger still has zero eligible
post-freeze closes (`--prospective-shadow` reports collecting/target:20).
All frozen prospective shadows need new reconciled closes; no live order or
risk setting was changed.

The 2026-10-08 05:36 UTC exchange snapshot also exposes account-level open
positions separately from the realized V2 scorecard: four positions (one long,
three short), −$59.10 unrealized PnL, and $87.22 gross mark-to-stop risk, with
all four reporting stops. These are account exposure figures, not closed
strategy outcomes; the stop estimate excludes gaps, slippage, and fees. The
account-PnL cache watermark is 2026-10-07 14:34 UTC, so it is stale relative to
that exchange snapshot. The refreshed archive adds one Closed PnL row but still
has only 52 complete V2 positions; realized strategy metrics and prospective
shadow sample counts therefore remain unchanged.

## Profitability evidence refresh — 2026-10-08 05:47Z

A new read-only Bybit Demo forensic export completed at `2026-10-08T05:47:15Z`
with an open-position snapshot at `05:43:29Z`. It contains 57 filled V2 cases,
52 complete-size-reconciled positions, 587 execution rows, 139 Closed P&L rows,
316 funding settlements, and four open account positions. Compared with the
prior archive, execution rows increased by six and Closed P&L rows by one, but
there is still no additional complete V2 position. The account P&L cache
watermark is still `2026-10-07T14:34:14Z`; do not treat the stale account cache
as a current closed-performance refresh.

Reconciliation confirms the 52-position strategy baseline remains −$229.05
net: 36/52 winners (69.23%), $24.74 mean net win, −$69.99 mean net loss, 0.354
dollar payoff ratio, PF 0.795, and −$4.40 dollar expectancy per position.
Gross price P&L is −$115.91 and fees are $115.72; costs are material, but
removing all observed fees alone would only bring this historical sample near
flat, not establish a tradable edge. Mean net expectancy remains uncertain:
the existing four-position block-bootstrap interval is −$0.190R to +$0.210R.

Direction is the strongest currently visible risk-allocation hypothesis:
20 LONG positions returned −$389.54 (11 wins; 55.0% win rate; −$19.48 mean
position expectancy), while 32 SHORTs returned +$160.49 (25 wins; 78.1% win
rate; +$5.02 mean position expectancy). This does not establish intrinsically
bad LONG signal quality: the cohorts are small, exposure differs, and the
historical result is vulnerable to regime and selection effects. Retrospective
linear 25% LONG sizing would make the observed dollar total positive, but its
bootstrap interval crosses zero and it assumes fills, fees, and slippage scale
unchanged. Keep it as a paper shadow only.

The same direction-risk sensitivity was checked in the report's chronological
early-70% and latest-30% partitions. At current LONG sizing, those segments
returned −$167.01 (36 positions) and −$62.03 (16 positions); scaling LONG risk
to 50% changed them to −$44.59 and +$10.31, while 25% changed them to +$16.62
and +$46.49. These are exploratory partitions, not independent train/test
proof; every corresponding IID return interval still crosses zero. The
direction signal is therefore worth continued prospective testing, but the
observed crossover is not a validated multiplier.

The fresh archive was checked against all frozen prospective gates: combined
entry/exit, exit-only, stop-distance, early-trail, depth/exit, LONG risk,
LONG risk curve, and source/side health each have zero eligible post-freeze
closed cases (LONG gates require 20 LONG outcomes). Thus the refresh produces
no evidence to promote or reject any candidate. Keep production risk and orders
unchanged; prioritize prospective LONG-vs-SHORT sizing evaluation over further
historical parameter search, and reassess only after enough new reconciled
outcomes accumulate.

## Demo outcome refresh — 2026-10-08 05:53Z

A subsequent read-only export completed at `2026-10-08T05:53:47Z` (open
positions snapshot `05:50:16Z`). It reports 57 filled V2 cases and 52 complete
positions, unchanged from 05:47Z; executions rose to 588, Closed P&L rows to
140, lifecycle actions to 36, and funding settlements remained 316. The extra
account close is not a new completed V2 position, so all frozen strategy
shadows still have zero eligible post-freeze outcomes.

The updated fill-pattern decomposition remains stark: E1/E1+E2-only positions
were 31/32 winners and +$668.04 net, while E3-filled positions were 5/20
winners and −$897.09 net. E3-filled LONGs were 2/11 winners (−$560.52); E3-
filled SHORTs were 3/9 winners (−$336.57). This points to deep-fill exposure as
a major realized-risk marker across directions, but not a causal E3 effect:
the E3 cohort is selected by the adverse price path that reaches the deeper
limit. Prior no-E3 and reduced-E3 historical replays did not establish robust
holdout improvement. Do not simply disable E3; preserve it as a hypothesis for
prospective matched replay, alongside the already-frozen entry/exit shadows.

The implementation explains a plausible dollar-P&L mechanism: the default
V2 entry-risk split is 60%/25%/15% across E1/E2/E3, and each leg's quantity is
computed from its risk share divided by that entry's stop distance. The planner
caps the sum of planned stop risk at the per-trade budget. A position that wins
before deeper entries fill therefore realizes less dollar exposure than a
losing path that fills E3 and then stops. This is consistent with winners'
lower mean initial risk ($53.14 versus $70.96 for losers) and the E3 cohort's
losses, but it does not prove the 70/25/5 or any other allocation is profitable;
that needs matched replay and prospective validation.

The app's read-only SQLite snapshot explains why those prospective gates are
not advancing: source-message processing reached `2026-10-08 05:49:39`, with
1,480 stored messages (1,469 completed, 11 failed), but the newest execution
plan is still `2026-10-07T11:59:14Z` and there are no intents created since
that plan. This is evidence of no newer recorded actionable intent/plan, not
proof that there were no market opportunities; no raw message contents were
used in this check. Continue outcome-based evaluation when a new V2 position is
actually opened and fully reconciled. If faster offline research is needed,
do not reuse failed plans as synthetic trades: a simulator would need actual
source-signal snapshots and a causal, risk-bounded fill model, rather than
relaxing prospective gates or counting account-level partial closes as
complete strategy outcomes.

The same database has 85 V2 plans total: 66 linked to executed intents and 19
failed. Aggregated failure classes are 10 `EntryPreflightError`, six
`TradeExecutionError`, and three stale-intent rejections. Sanitized reasons show
11 were stopped because market movement would exceed the configured risk
budget (six execution errors plus five preflight errors), five were blocked by
existing strategy ownership, and three were stale. None retained Bybit order
IDs, and no strategy is currently `UNCERTAIN`. These are rejected attempts,
not additional realized trades or valid hypothetical fills; do not include
them in payoff calculations. Error summaries were sanitized; no raw signal
text or full error payloads were used.

## Allocation replay correction — retrospective only

The earlier combined-allocation replay incorrectly multiplied LONG `net_r`,
MFE, and funding R by the nominal risk-size multiplier. R is normalized by
initial risk, so reducing position size changes dollar PnL and dollar risk but
does not change a trade's normalized R. Disregard the previously reported
combined-profile R metrics and LONG-sizing incremental R deltas; they were
artifacts of that scaling error. The separate dollar-level sensitivity in
`analyze_forensic_pnl.py` scales both PnL and risk and remains explicitly
counterfactual.

After correction, replaying only the exit candidate on 54 reconciled positions
gave train −0.762R versus current −1.859R (27 cases), validation +2.152R versus
+0.347R (10 cases), and holdout −0.244R versus current −2.264R (17 cases).
Holdout paired improvement was +2.020R, but the IID 95% interval crossed zero
([−0.270R,+4.875R]); the circular-block interval was [+0.412R,+4.027R]. The
three largest positive cases contributed +2.076R, and removing them left only
−0.055R paired delta. The holdout remains negative under the candidate and has
been repeatedly inspected, so these retrospective results do not establish a
profitable exit policy or justify promotion. LONG sizing must be evaluated in
dollars and risk-weighted return, not by scaling normalized R.

The active Demo profile is not the older frozen exit-only candidate: Demo uses
the same 60/25/15 entry weights and 1/2/4R targets, but its trailing distance is
0.10R rather than 0.30R. `live_demo_payoff_exit_candidate()` now mirrors the
deployed profile exactly; the two replay candidates are reported separately.
On the refreshed 54-position archive, this exact-profile replay scored
+0.370R train, +2.334R validation, and +0.701R holdout, versus −1.859R,
+0.347R, and −2.264R for current policy. Holdout paired delta was +2.965R
(17 cases), but its IID interval crossed zero (−0.523R to +7.050R), and the
three largest improvements contributed +3.106R; removing those left −0.140R.
This is still a repeatedly inspected retrospective sample, not independent
evidence. The live tagged cohort remains the only promotion evidence and is
currently 0/20 complete.

Pairing that exact 0.10R-trail profile with 70/25/5 entry weights scored
+0.508R train, +2.359R validation, and +0.424R holdout. Against the live
60/25/15 profile, the holdout delta was −0.277R (IID 95% interval
[−3.516R,+1.954R]; circular-block [−3.619R,+2.081R]); removing the three
largest positive case deltas left −1.131R. Despite improving 14/17 individual
holdout cases, it reduced total holdout return and has wide uncertainty, so it
does not justify changing the current Demo entry allocation.

The offline comparison is reproducible with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/compare_allocation_interaction.py /tmp/ccb-forensic.zip
```

## Confidence diagnostic — 2026-10-08

On the same 52 reconciled V2 positions, 44 carry stored model confidence 0.99.
Within that common confidence bucket, LONG was 9/17 winners (52.9%) and
−$345.40 net (−0.239R expectancy), while SHORT was 21/27 winners (77.8%) and
+$149.77 (+0.169R expectancy). The other confidence levels contain only one
to three trades per side, too few to validate a cutoff. A confidence-threshold
filter is therefore not a supported optimization; high model confidence and
overall win rate do not establish signal quality or positive expectancy.

## Dashboard data refresh — 2026-10-08 06:06Z

The dashboard snapshot was regenerated read-only from the app SQLite database
and the 05:53Z forensic archive. It now reflects 1,480 source messages and an
account-P&L sync watermark of `2026-10-08T05:49:38Z`. The account-level ledger
contains 154 partial Closed-P&L rows totaling +$61.66; this aggregation is
distinct from the V2 whole-position scorecard and is not evidence that V2 is
profitable. The four-position account snapshot at `05:50:16Z` shows −$48.10
unrealized PnL and $95.64 estimated gross mark-to-stop risk, with stops reported
on all four positions.

The reconciled V2 scorecard remains 52 positions, −$229.05 net, $24.74 average
win, and −$69.99 average loss. No new intents or execution plans have appeared
since the 05:53Z check, so no strategy candidate gained a prospective outcome.
The dashboard data refresh changes reporting artifacts only; no orders, risk
settings, or running services were modified.

### Direction result by chronological slice

A side-only diagnostic on the same 52 reconciled positions used a chronological
26/10/16 train/validation/holdout split. SHORT results were positive in each
slice: +1.794R (16 positions), +1.218R (7), and +0.226R (9). LONG results were
negative: −2.620R (10 positions), −0.075R (3), and −1.656R (7). On the holdout,
SHORT net was +$82.66 and LONG net was −$144.70. This consistency makes
direction-specific exposure a worthwhile prospective hypothesis, but not a
validated filter: all SHORT IID and circular four-trade block-bootstrap
intervals include zero (holdout mean-R intervals are [−0.289,+0.646] IID and
[−0.235,+0.648] block), and the validation/holdout cohorts are very small.
Do not disable LONGs or change sizing from this retrospective split. Frozen
prospective gates remain the source of promotion evidence.

## Demo refresh — 2026-10-08 06:15Z

A fresh read-only Bybit Demo export completed at `06:15:19Z`, with open
positions sampled at `06:11:48Z`. It again contains 57 filled V2 cases, 52
complete reconciled positions, 588 execution rows, 140 account Closed-P&L rows,
316 funding settlements, and four open account positions. V2 realized metrics
and every frozen prospective shadow count are unchanged; in particular, the
exit-only shadow remains at 0/20 post-freeze positions.

The separate open-account snapshot totals −$56.31 unrealized P&L and about
$87.43 gross mark-to-stop exposure across the four positions; each reports a
stop. The estimate is from mark to configured stop and excludes gaps, fees, and
slippage. SQLite processing reached 1,481 source messages (1,470 completed,
11 failed), while the newest plan/intent timestamps remain
`2026-10-07T11:59:14Z` / `2026-10-07T11:59:13Z`. Thus the source stream advanced,
but no new actionable plan or fully reconciled V2 result appeared. This does
not establish that no market opportunity occurred. No orders or risk settings
were modified.

## Live Demo LONG sizing experiment — 2026-10-08 06:42Z

The app now applies a 0.25 multiplier to the 1% base risk budget for newly
planned LONG trades only. SHORT plans retain 1%; existing plans, positions, and
working entry orders are not resized. This is a Demo experiment, not a validated
profitability improvement.

A refreshed read-only export at `07:09:06Z` still contains 57 filled V2 cases,
53 complete positions, 589 execution rows, 141 exchange Closed-P&amp;L rows, and
316 funding settlements. A previously open XRP SHORT completed at +$15.62
(+0.356R); its plan predates the experiment, so it is historical baseline data,
not an experimental LONG or contemporaneous control. The reconciled baseline
remains 37/53 winners (69.81%), net −$213.42, average win $24.50, average loss
−$69.99, payoff 0.350, PF 0.809, and −$4.03 per position; mean expectancy is
+0.020R with IID and block-bootstrap intervals spanning zero.

The dollar payoff ratio (0.350) is not the same as the risk-normalized payoff
ratio (0.462). At the observed 69.81% win rate, the normalized break-even win
rate is 68.40%, so mean R is barely positive (+0.020R) while mean dollar PnL is
negative (−$4.03). Losing positions carried $70.96 average initial risk versus
$52.89 for winners. This points first to inconsistent exposure allocation as
the dollar-PnL driver; the live LONG sizing trial tests that allocation, while
any claim that a new exit profile improves payoff still requires prospective
outcomes.

Re-running the frozen directional sizing sensitivity on this export leaves the
0.25x LONG setting as a plausible exposure-control experiment, not a proven
edge: linear scaling estimates +$32.24 on the earlier 37-position segment and
+$46.49 on the latest 16, versus −$151.39 and −$62.03 at equal LONG/SHORT
risk. The corresponding IID return intervals remain very wide (−23.71% to
+26.16%, and −40.32% to +51.90%). The model assumes PnL, fees, fills, and risk
scale linearly; it does not model changed fills, minimum order sizes, slippage,
or market impact. At that snapshot, 0.25x was retained prospectively; the
2026-10-08 08:49Z follow-up below supersedes that setting for new LONG plans.

At the refreshed snapshot, there are still zero newly planned or executed
experimental trades and the actual cohort remains 0/20 completed LONGs, with
zero contemporaneous SHORT controls. The latest ingested Telegram post was
classified as general volatility commentary, not an actionable signal. The
account snapshot at `07:05:24Z` shows three open positions (one LONG, two
SHORT), −$70.86 unrealized P&amp;L, and estimated $67.89 gross mark-to-stop risk;
all three have stops. These are account exposure figures, not strategy outcomes.

The paper sizing shadows now require baseline 1% risk plans, preventing live
0.25%-risk outcomes from being rescaled a second time. Use
`--prospective-live-long-risk` to compare actual reduced-risk LONG outcomes in R
with contemporaneous 1%-risk SHORT controls. The updated analyzer tests pass;
the full suite passes 485 tests at 79.96% coverage, and Ruff lint/format checks
pass.

## Live Demo payoff-exit experiment — 2026-10-08 07:25Z

The Compose app now selects the tagged `payoff_challenger` profile for newly
planned Demo trades. Entry sizing/depths are unchanged; exits take 15/20/25% at
1/2/4R, leave a 40% runner, and activate a 0.10R trailing distance at +0.40R.
The profile is serialized into each new plan, so existing plans and open
positions retain their baseline 0.5/1/1.5R exits and 0.5/0.3R trail.

This profile is an experiment, not a promotion. The historical payoff screen
showed +0.038R train, +2.526R validation, and +1.703R holdout with a 0.422
holdout win/loss ratio versus 0.346 baseline, but the gain was concentrated in
three cases and the paired confidence intervals crossed zero. Only new,
fully-reconciled positions tagged `payoff_challenger` count toward the 20-case
review target. Use `analyze_forensic_pnl.py BUNDLE
--prospective-demo-exit-profile` to report win/loss ratio, expectancy, and
bootstrap intervals. Do not promote based on the reused holdout.

The app was recreated at `07:25:15Z`. Post-restart checks confirmed the same
two exchange-open positions (SOL and DOGE) with their original stops, 124
existing plans still tagged baseline, and zero candidate plans at activation.

A post-rollout forensic refresh at `07:29:43Z` reconciled one more pre-existing
SHORT loss, bringing the baseline to 54 complete positions: 37 wins / 17 losses
(68.52%), −$286.54 net, 0.349 payoff ratio, 0.760 profit factor, and −$5.31 per
position. Mean R is +0.0012 with IID and block-bootstrap intervals spanning
zero. This close used a baseline plan and is not a challenger result. The
candidate remains 0 planned / 0 completed of 20. The account snapshot at
`07:26:05Z` has two open positions, −$5.46 unrealized, and estimated $59.43
gross mark-to-stop risk; both retain stops.

## Early-trail Demo experiment — 2026-10-08 08:23Z

The active new-plan profile is now separately tagged `payoff_early_trail`:
same 60/25/15 entry allocation, 15/20/25% exits at 1/2/4R, 40% runner, and
0.10R trail distance, but activation moves from +0.40R to +0.20R. The former
`payoff_challenger` profile remains unchanged in code and serialized old plans.
The exact-profile retrospective replay favored +0.20R in train and holdout but
not validation; the holdout paired interval crossed zero and removing its top
three positive contributions reversed the result. This is a hypothesis, not a
profitability claim.

Full suite: 487 passed at 79.96% coverage; focused profile/metrics tests: 91
passed; Ruff check and format check passed. The Demo app was recreated without
changing Redis or state volumes. AUTO approval remains `all`; LONG risk remains
0.25x. Startup catch-up generated two new tagged plan records: HYPEUSDT was
blocked by the ownership guard, and XRPUSDT LONG executed at 0.25% risk. A
fresh Bybit archive at `08:29:14Z` reconciles 1 filled tagged position, 0
completed. XRP has a 1.3825 reduce-only stop and reduce-only target orders;
the pre-existing DOGE/SOL positions retain their stops. The dashboard snapshot
at `08:33:15Z` shows 2 planned, 1 executed, 1 filled, 0 complete; three account
positions all have stops. Do not score the open XRP position as a win/loss.

## Reduced LONG exposure follow-up — 2026-10-08 08:49Z

The reconciled 54-position scorecard has 20 LONGs at −$389.54 and 34 SHORTs at
+$103.00. Mechanically rescaling LONG dollar PnL while leaving SHORTs unchanged
projects +$5.62 total at 0.25x LONG risk and +$64.05 at 0.10x. The implied
historical break-even LONG multiplier is about 0.264x. This is a post-hoc linear
counterfactual (not a fill- or slippage-aware strategy replay); the uncertainty
is large, and it does not demonstrate a predictive edge.

Based on the poor LONG dollar expectancy (−$19.48/trade, 0.410 profit factor)
and that sensitivity, new Demo LONG plans now use 0.10x the base risk, while
SHORT risk and the `payoff_early_trail` exits remain unchanged. The already-open
XRP LONG retains its frozen 0.25%-risk policy and orders. The 0.10x cohort starts
at `2026-10-08T08:49:47Z`; analyze only fully reconciled LONGs at 0.10% risk
against contemporaneous 1%-risk SHORT controls with `--prospective-live-long-risk`.
The 0.25x XRP trade remains separately observable in the payoff-profile cohort.
Review the new sizing hypothesis after 20 completed 0.10x LONGs; do not interpret
the retrospective counterfactual as evidence that the new setting is profitable.

## Deployment and state

```sh
docker compose up -d --build app
docker compose logs -f app
docker compose stop app
```

Back up SQLite before state repair; stop the app to prevent concurrent mutations.
Restore a paused strategy only after checking ownership, orders and protection.
Never replay uncertain submissions blindly. Do not run `docker compose down -v`
unless intentionally erasing SQLite, Telegram-session and Redis volumes.
