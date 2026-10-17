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
  --forensic-bundle /tmp/ccb-forensic.zip \
  --demo-long-risk-multiplier 0.10 \
  --demo-exit-profile payoff_early_tight_trail_long_015 \
  --demo-portfolio-stop-risk-cap-usdt 400
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

Refreshed stop-geometry check — 2026-10-08 13:45Z
----------------------------------------------------

On the 13:29Z archive, rerun with `--reconciled-only` to exclude 6 of 61
incomplete positions. The 55 complete cases split 27/11/17 chronologically.
The 2x stop-and-grid profile remained train-selected (+3.374R train), but lost
−0.301R on holdout versus −2.463R for baseline. The best holdout net among
these profiles was only +0.225R for stop-only 1.5x, with PF 1.07; 1.5x
stop-and-grid made +0.067R (PF 1.02). Paired 95% intervals crossed zero for
all geometry variants. A narrower 0.75x stop looked better on holdout by
paired delta (+1.898R), but still lost −0.565R net. These 17-case results do
not establish a profitable stop geometry; keep the frozen shadow and do not
change production settings. The API check at 13:45Z found no new closed-PnL
record since 09:50Z, and every current Demo position had a stop.

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

## Current profitability evidence — 2026-10-08 13:26Z

A fresh read-only Bybit Demo archive reconciles 55 complete positions (38 wins,
17 losses): 69.09% win rate, 0.3403 average-win/loss ratio, 0.7606 profit
factor, −$285.60 net and −$5.19 expectancy per position. The IID 95% expectancy
interval is −$17.83 to +$6.71 per position; four-position block bootstrap is
−$17.54 to +$6.60. Overall expectancy remains uncertain, but LONG is the clear
historical drag: 21 LONGs returned −$388.60 (−$18.50/position, 0.4118 PF,
−0.2031R), while 34 SHORTs returned +$103.00 (+$3.03/position, 1.1935 PF,
+0.1298R). These are after-cost reconciled outcomes, not partial Closed-PnL rows.
At the observed overall 38/17 win/loss count, break-even requires average win
≥$31.39 instead of $23.88 (+31.5%), or average loss no worse than −$53.37
instead of −$70.17 (23.9% smaller), holding the other value fixed. LONGs are
more severe: at 12/9, the corresponding targets are ≥$55.05 average win or
average loss no worse than −$30.23. This confirms that 0.10x LONG sizing can
limit dollar exposure, but cannot repair the LONG cohort's negative expectancy
in R by itself.
Gross price P&L was −$171.66, fees were $117.32, and signed funding was +$3.38;
fees equal 68.3% of absolute gross price P&L. Removing every fee would still
leave roughly −$168.28 net (−$3.06 per position), so execution-cost reduction
can help but cannot repair the underlying loss distribution alone. LONGs lost
$344.90 gross before $43.17 fees and −$0.53 funding; SHORTs gained $173.24 gross,
but $74.15 fees and +$3.91 funding left +$103.00 net. The dashboard now shows
this cost decomposition overall and by direction.

The prospective 0.10x LONG cohort currently has 2 filled positions and 0
completed; the 20-completion review gate is not met. The early-trail profile has
3 filled positions and 1 completed, but that single completion is the separate
prior 0.25%-risk XRP LONG (+$0.94, +0.0864R), not evidence for the new 0.10x
cohort. Keep settings unchanged while collecting complete outcomes; neither
historical sensitivity nor a one-trade payoff result establishes a profitable
rule.

The 20-position gate is a review checkpoint, not a statistical promotion
threshold. The historical LONG sample (21 positions, mean −0.203R, standard
deviation 0.734R) implies roughly 103 independent LONG outcomes to detect an
effect of that observed size with 80% power at a two-sided 5% level under a
normal/IID approximation. This plug-in estimate is optimistic if trades are
clustered or market conditions shift; judge the cohort by risk-normalized return
and uncertainty, not by reaching 20 alone.

Frozen prospective exit shadows were rerun on this archive. The exit-only
candidate is flat on its one eligible closed LONG (0.000R paired delta). The
early-trail replay moves that same case from −0.0265R under the replay baseline
to +0.0627R (+0.0893R paired); removing the top-three positive contributions
removes the entire gain. This is one simulated case, not a second independent
trade or proof that the actual live profile is profitable. The replay reporter
now emits null metrics for a direction with zero cases instead of crashing while
summarizing an empty side.

The dashboard exporter now accepts explicit `--demo-long-risk-multiplier` and
`--demo-exit-profile` options. Supply the running Compose values when exporting
outside the container; otherwise host defaults can incorrectly label live Demo
experiments inactive. The strategy scorecard also exposes gross price P&L,
fees, signed funding, and fee share so cost drag is visible beside net payoff.

## Closed-P&L freshness

At the 2026-10-08 13:49Z live check, Telegram sources had completed messages
through 13:41Z while the SQLite account-P&L watermark remained at 10:18Z. This
was not an API error: non-actionable posts return before the existing
message-triggered P&L sync. A direct read-only Bybit query confirmed no missing
Closed-P&L records, but a close between actionable signals could leave the app
ledger stale. The service now has a supervised 15-minute sync loop, including
an immediate first sync and retry after logged failures. It only reads Bybit
Closed P&L and updates the local SQLite cache; it does not place or change
orders. The current running app was not restarted, so the fix takes effect only
after a later deployment/restart. The full suite passes 501 tests at 80.01%
coverage. That run also caught and fixed a related reporting edge case: the
allocation comparison now emits defined payoff/breakeven values for all-win or
all-loss segments, and unavailable values for an empty segment.

## Fixed-horizon signal follow-through — 2026-10-08 14:15Z

To separate directional entry quality from managed trade P&L, a research-only
diagnostic measures close-to-entry movement after 5m, 15m, 1h, 4h, and 24h in
initial-stop R. It anchors to the first actual fill (not the final average that
can include later E2/E3 fills), includes all filled cases with available future
candles (avoiding selection on eventual full closure), and ignores exits and
fees. On the 14:15Z archive, 25 LONGs averaged −0.213R at 1h (32.0% positive;
circular block-4 95% interval [−0.428R,−0.048R]) and 23 averaged −0.236R at 4h
(26.1% positive; [−0.376R,−0.095R]). SHORTs averaged +0.040R at 1h (n=36;
interval [−0.101R,+0.166R]) and −0.032R at 4h (n=34; [−0.333R,+0.284R]).

This is evidence against assuming the high realized win rate proves strong
directional LONG signal quality: losses may come from entry selection as well
as exit management. Treat the result as exploratory, not confirmatory: five
correlated horizons were inspected on a reused sample, the side groups are
small, and the intervals do not correct for multiple comparisons. It does not
justify disabling LONGs or changing the live experiment. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_signal_followthrough.py /tmp/ccb-forensic.zip
```

The isolated prospective LONG cohort (plan time >= `2026-10-08T08:49:47Z`,
0.10% risk, `payoff_early_trail`) has only two filled cases in this archive.
Both have negative 1h first-fill-to-close movement (mean -0.356R); one has a
completed 4h horizon at -0.282R. The 1h block interval is degenerate at n=2 and
is not meaningful uncertainty quantification. Both positions remained open
with stops at the snapshot, so these are interim marks—not realized P&L or
complete-position outcomes. Keep the prior 0.25%-risk XRP trade out of this
cohort, and wait for completed cases before judging live risk or exits.

## Exit and execution-cost diagnostics — 2026-10-08 14:21Z

Replayed the 14:15Z archive (61 filled V2 cases, 55 complete) through the
time-stop, post-E3 risk-trim, later-target/reduced-E3, and maker/taker-fee
diagnostics. No exit candidate is ready to promote. The time-stop threshold
selected on training (60 minutes, close <= -0.25R) loses 1.165R versus current
on the 19-case holdout; alternatives with positive holdout deltas had negative
training deltas and wide intervals. E3 stop-risk trims were neutral to worse
on holdout (best tested delta -0.009R; intervals include zero).

The post-hoc 70/25/5 entry allocation with 1/2/4R targets improved holdout
point-estimate net from -2.122R to -0.482R (19 cases; paired delta +1.640R),
but its block-bootstrap 95% interval is [-0.311R,+3.871R] and removing the
three largest positive case deltas changes the paired delta to -0.436R. It
remains negative in both training (-0.619R/30) and holdout. The candidate was
selected after inspecting this archive; only future unseen data can validate
it, and this replay is not evidence to change production.

Using observed maker/taker rates instead of the scalar fee assumption improves
holdout replay by +0.129R on average (19 cases; block 95% interval
[+0.070R,+0.190R]), but the corrected holdout still returns -1.993R total,
-0.105R per case, and 0.648 profit factor. This is a cost-model correction,
not a demonstrated profitable strategy. The broader account result remains
negative after actual fees, so fee reduction alone is insufficient.

## Source/direction decomposition — 2026-10-08 14:21Z

Completed-position groups suggest losses are not explained by direction alone:
the largest source/side groups are small (8-21 positions), LONG expectancy is
negative in the larger observed groups, and SHORT results vary by source. One
source's 8/8 winning SHORTs account for +$204.79, while another source's 21
SHORTs are only +$0.05R per position and -$24.33 net. These are descriptive
and highly vulnerable to source/regime selection; no source/side gate is
promoted without a predeclared chronological prospective sample.

The existing walk-forward 3-loss source/side health gate would have filtered
13 of 55 completed cases: retained net was -$95.33 versus -$285.60 baseline
(paired delta +$190.27), but the IID 95% interval for that delta is
[-$90.79,+$521.81]. The retained set still lost $2.27 per position overall;
LONG expectancy remained -0.191R. Most importantly, the only post-freeze
completed case was the prior 0.25%-risk XRP winner (+$0.94), which the rule
would have suppressed. This is not a dependable improvement; keep the gate
paper-only and continue collecting its prospective sample.

## Bootstrap block-length sensitivity — 2026-10-08 14:32Z

To check dependence on the existing four-position block assumption, recomputed
performance intervals with circular block lengths 8 and 12 (2,000 draws each)
on the same 55 complete outcomes. Overall mean-net-R intervals remain broad and
cross zero: [-0.159R,+0.161R] at block 8 and [-0.180R,+0.171R] at block 12.
LONG intervals also cross zero at both lengths ([-0.556R,+0.156R] and
[-0.544R,+0.138R]). SHORT's block-8 interval crosses zero, while block 12 is
slightly positive ([+0.014R,+0.238R]); with just 34 SHORTs and only about five
non-overlapping 12-position blocks, this sensitivity is not evidence of a
reliable directional edge. The conclusion is unchanged: current samples do
not establish positive risk-adjusted expectancy or a deployable rule.

## First completed 0.10x LONG — 2026-10-08 14:42Z

The XRPUSDT LONG planned at 0.10% risk under `payoff_early_trail` closed its
full 366.6-unit position. Fresh reconciliation tags it to the 0.10% cohort:
net +$0.8312 (+0.1170R), fees $0.4621, funding $0. The cohort is now 1/20
completed, with one other filled position open at that snapshot. One winner cannot
estimate a payoff ratio or establish expectancy; its one-case bootstrap
interval is degenerate and uninformative. The broader payoff-profile cohort
is 2/20 complete, combining this case with the prior 0.25%-risk XRP winner;
keep those risk groups separate.

The refreshed 14:40Z strategy archive reconciles 56 complete positions:
39 wins, 17 losses, 69.64% win rate, 0.3318 average-win/loss ratio, -$284.77
net, and -$5.09 mean net per position. LONGs are 22 positions at -$17.63 and
-0.189R expectancy; SHORTs are 34 at +$3.03 and +0.130R. Gross price P&L is
-$170.37, fees $117.78, funding +$3.38. The four-position block 95% interval
for mean net R remains [-0.168R,+0.175R]. The new winner nudges the sample but
does not change the conclusion that system expectancy is unproven and overall
net remains negative.

The account-level Closed-PnL cache synced through 14:42Z and now has 157 rows.
Those rows include partial exits and are displayed separately from the
56-position whole-trade scorecard. The dashboard exporter can also supplement a
stale cache from a forensic archive only when the archive's declared coverage
spans the cache watermark; it leaves SQLite and the running service untouched.

The 14:45Z archive adds a new GALAUSDT LONG plan and E1 fill (25,812 units at
0.002314), tagged 0.10% risk and `payoff_early_trail`, with a 0.002146 stop.
The open Demo account now has five positions, all with stops. The live-risk
cohort is 3 filled / 1 completed / 2 open; the broader payoff-profile cohort
is 4 filled / 2 completed. No additional realized result is added by this
entry, so the realized scorecard above is unchanged. Dashboard data was
regenerated from this archive and the synchronized local cache. The full
project suite then passed 503 tests at 80.04% coverage; focused dashboard tests,
Ruff, format check, and `git diff --check` also passed.

Re-running the frozen early-trail shadow after the XRP close gives 2 eligible
post-freeze LONGs: baseline replay -0.1287R total versus candidate +0.1587R,
a paired +0.2874R. Both cases improve, but removing the largest positive
contribution removes the entire gain; the block-4 interval is degenerate at
n=2. This is a useful prospective lead, not conclusive evidence, and combines
the prior 0.25%-risk XRP with the new 0.10%-risk XRP. The independent live
0.10%-risk cohort remains only 1/20 complete. Leave settings unchanged and
continue separating the cohorts.

## Reconciled chronological replay — 2026-10-08 15:00Z

Re-ran the historical parameter search on the 14:54Z forensic archive, limited
to 56 size-reconciled positions and split chronologically into 28 training,
11 validation, and 17 untouched holdout cases. Baseline results were
-1.690R/PF 0.77 in training, +0.588R/PF 1.28 in validation, and
-2.829R/PF 0.56 in holdout. Holdout LONGs were -2.780R/PF 0.15/29% wins;
SHORTs were -0.049R/PF 0.98/70% wins.

The top 15 candidates ranked only on training results all remained negative
on holdout (-1.459R to -3.557R). The top-ranked candidate had just +0.168R in
training, then -2.730R in holdout; the candidate with the best holdout result
was not the training winner and still lost -1.459R. These results do not
support selecting a profitable exit parameter set and are vulnerable to
multiple-comparison selection. A diagnostic 0.50R post-E3 stop cap improved
holdout from -2.829R to -2.116R, but also lost money and validation changed
from +0.588R to -0.242R. Treat it as a risk-reduction lead only, not a
deployable strategy. E3 fill cohorts are adverse-path-selected and are not a
causal estimate of the effect of adding E3.

A separate paired replay of the frozen `payoff_early_trail` exit-only profile
returned +0.090R on this holdout versus -2.829R for baseline exits
(PF 1.03, 64.7% wins versus 52.9%). The paired delta was +2.920R, but its
IID 95% interval [-0.606R,+7.009R] and block-4 interval
[-0.134R,+6.292R] both span zero. Removing the three largest case deltas
changes the paired result to -0.186R. Two sequential ADA SHORT cases alone
contribute +2.555R, indicating strong symbol/time concentration. Adding the
reduced-E3 candidate to this exit profile lowers holdout net to -0.615R; the
increment versus exit-only is -0.706R with a wide interval. This supports
keeping the exit profile as a prospective lead, not claiming a robust
historical edge. The actual `payoff_early_trail` cohort has only two completed
positions, so separate from this retrospective simulation it remains
underpowered.

The same reconciled positions show the exposure mechanism more clearly:
E1-only trades were 21/22 winners and +$319.04 net, with $41.76 average
realized initial stop risk versus $69.73 planned; E1+E2 trades were 12/12
winners and +$365.57 net, with $60.73 average realized risk versus $71.54
planned. Full E3 trades were only 6/22 winners and -$969.37 net; their
average realized risk ($69.86) matched planned risk ($69.86). Across all
completed positions, winners averaged $50.64 realized risk against $71.14
for losers, while average planned maximum loss was much closer ($69.02 vs
$72.82). Thus the dollar loss reflects both poor payoff in R (average win
+0.427R vs average loss -0.963R) and larger realized exposure on losers.
The fill-pattern contrast is still path-conditioned: reaching E3 requires a
deeper adverse move, so it does not prove that canceling E3 would cause better
results. It does make entry completion/exposure a first-class diagnostic,
alongside exit management.

Conclusion: the current evidence does not show that exit tuning alone fixes
the payoff problem. Holdout LONG losses dominate, but the contemporaneous
SHORT holdout is also slightly negative; do not infer a profitable SHORT-only
policy from direction splits. Keep the reduced-risk LONG and exit-profile
cohorts isolated and collect prospective full-position results before any
production adjustment. The extracted `confidence` field is explicitly
extraction confidence, not probability of profit; do not use it as a sizing
signal without a separate, calibrated predictive study.

## Demo experiment monitor — 2026-10-08 15:51Z

Since the 15:20Z snapshot, ZECUSDT received an E2 fill of 0.23 at 1167 on its
older 1.0%-risk baseline LONG plan (stop 1087.8). GALAUSDT's 0.10%-risk
`payoff_early_trail` plan filled E2 (15,989 at 0.002259) and E3 (19,019 at
0.002203), reaching 60,820 total; the full position later closed at 0.002145
for -$7.4083 net (-1.0250R), including $0.1202 fees and no funding.

The older BNBUSDT LONG plan (created 2026-09-24, 1.0% risk, baseline) filled
E1 (1.06 at 744.1), E2 (0.51 at 738.8), and E3 (0.36 at 733.4). Its current
1.93 position retains stop 703.8. BTCUSDT E1 (0.026 at 81,253.3) filled on an
older 2026-09-29 baseline plan, with stop 79,628.2; E2/E3 were still open GTC
orders at the last realtime inspection. Keep both legacy plans distinct from
the current reduced-risk cohort; no order was canceled or modified.

SOLUSDT's 0.2 partial T2R4 close at 110.81 booked +$1.5274 Closed-PnL, while
0.4 remains open with stop 123.62. Do not count that partial row as a completed
position. DOGEUSDT and ENAUSDT also received final-leg fills on their existing
plans. MINAUSDT opened under the current 0.10%-risk `payoff_early_trail`
configuration (planned stop 0.07815, planned max loss $7.2200), filled E1 at
0.07981, and fully closed at 0.08024 for +$0.8978 net (+0.2060R), with
$0.2311 fees and no funding.

The fresh 15:51Z archive contains 65 filled V2 cases, 616 execution rows, 147
Closed-PnL rows, and six open positions; all six positions have exchange-side
stops (BNB 703.8, BTC 79,628.2, DOGE 0.07824, ENA 0.1982, SOL 123.62, ZEC
1087.8). Across 58 completed whole positions, the scorecard is 40 wins and 18
losses (69.0%), -$291.28 net, -$5.02 expectancy per position, and 0.341
average win/loss. Fees are $118.13 and signed funding +$3.38; gross price PnL
is -$176.53. The IID bootstrap 95% interval for mean net R is [-0.1901,
0.1607], and the circular-block interval is [-0.1809, 0.1576].
By side, 24 LONGs net -$394.28 (-$16.43 expectancy, 0.293 average win/loss;
$43.99 fees, -$0.53 funding), while 34 SHORTs net +$103.00 (+$3.03
expectancy, 0.367 average win/loss; $74.15 fees, +$3.91 funding). Their IID
mean-net-R intervals are [-0.4940, 0.0660] for LONG and [-0.0876, 0.3306] for
SHORT. The four-position payoff-profile subset is LONG-only, with $1.41 fees
and no funding.

The 0.10%-risk LONG cohort now has 3/20 completed cases: 2 wins, 1 loss,
-$5.68 net, and -0.2340R expectancy; average win/loss is 0.1576R. Its win-rate
95% Wilson interval is [20.8%, 93.9%], while the IID bootstrap 95% interval for
mean net R is [-1.0250, 0.2060]. There are no contemporaneous SHORT controls.
The `payoff_early_trail` cohort has 4/20 completed cases: 3 wins, 1 loss,
-$4.74 net, and -0.1539R expectancy; average win/loss is 0.1332R. Its win-rate
95% Wilson interval is [30.1%, 95.4%] and IID bootstrap mean net R interval is
[-0.7395, 0.1761]. Both cohorts remain too small to establish profitability.
Dashboard JSON/JS were regenerated from the 15:51Z archive and SQLite.

## Directional/source optimization review — 2026-10-08 15:57Z

The latest 58-position whole-trade scorecard does not support treating the
aggregate 69.0% win rate as proof of good signal quality. LONGs are 14/24 wins
(58.3%), -$394.28 net, -0.207R expectancy, and 0.361 average win/loss in R;
SHORTs are 26/34 wins (76.5%), +$103.00 net, +0.130R expectancy, and 0.492
average win/loss in R. The separate account Closed-PnL row win rate is not a
trade-level win rate because partial exits are multiple rows per position.

An exploratory first-fill follow-through analysis on 65 filled cases finds
LONG signed movement negative at 60 minutes (-0.241R, IID bootstrap 95% CI
[-0.466,-0.083], n=26) and 240 minutes (-0.238R, [-0.392,-0.084], n=24).
SHORT follow-through at those horizons is near zero and uncertain. This
analysis ignores exits, fees, and later fills, so it is diagnostic evidence
about directional movement—not standalone profitability or causal proof.

The 0.40R-activation `payoff_challenger` replay—not the live early-trail
profile—returned -0.968R versus -3.887R for baseline on the latest 18-case
chronological holdout. Its paired IID interval was [-0.596R,+6.981R], and the
top three positive case deltas exceeded the total gain. The exact live
`payoff_early_trail` replay (0.20R activation, 0.10R trail) instead returned
+1.089R train, +1.385R validation, and +0.665R holdout. On holdout it won 17/18,
but average win was only +0.099R versus -1.020R average loss (0.097R win/loss,
91.1% break-even win rate, +0.037R expectancy). Its paired holdout delta versus
baseline was +4.553R, with IID interval [-0.772R,+10.131R] and circular-block
interval [-0.904R,+9.673R]; removing the three largest positive deltas leaves
+1.028R. This is encouraging retrospective evidence but highly uncertain and
already inspected, not prospective proof. Adding reduced E3 to the exact live
profile returned -1.516R on holdout, -2.182R versus the live profile (IID
interval [-5.626R,+0.189R]); do not promote that interaction.

Source-by-side results are strongly heterogeneous. `Scalping Blog | Адель`
SHORT cases were 8/8 winners (+$204.79, +0.482R mean) and 3/3 winners in
holdout (+$93.79); its LONG cases were 1/3 winners (-$78.62). However, this
subgroup was identified post hoc and is small. `Мысли Эмилии` LONGs shifted
from 4/4 wins in validation (+$132.67) to 4/7 in holdout (-$219.49), showing
why in-sample source selection is unsafe. These are shadow leads only; keep
settings unchanged until a predeclared side/source rule is tested on new
prospective cases with an adequate sample and costs included.

The frozen paper-only source/side health gate (suppress after the last three
closed cases sum to <=0R) now has five post-freeze cases. It suppresses three
positions that actually netted +$2.67 and retains two losing positions totaling
-$14.68: the filtered subset is -$14.68 versus -$12.01 baseline, a -$2.67
paired delta (IID 95% interval [-$4.50,-$0.83]). The gate's first five cases
make its error mode concrete: it suppressed winners and retained both losers.
At only 5/20 cases this is not a calibrated gate; do not deploy it. The separate
0.00/0.25/0.50/1.00 LONG risk-curve shadow still has 0/20 eligible cases.

Follow-up diagnostics on the same completed cases show the live early-trail
profile's holdout was directionally uneven: SHORTs returned +0.875R/9 (9/9
winners), while LONGs returned -0.210R/9 (8/9 winners, with the one loss at
-1.020R). Train SHORTs returned +1.258R/18 and validation SHORTs +0.982R/7;
LONGs returned -0.169R/11 and +0.402R/4 respectively. The tiny, all-winning
SHORT holdout and the LONG reversal do not establish a durable side filter.

Whole-position fill-pattern associations are also stark: LONG E1-only was
7/7 wins and +$76.11, E1+E2 was 4/4 and +$96.70, while full E1+E2+E3 was 3/13
and -$567.10. SHORT full-E3 cases were 3/10 and -$409.68. This must not be
interpreted as the causal effect of removing E3: deeper adverse price movement
selects trades into the full-E3 cohort, and completed-case replay of reduced E3
has not consistently improved holdout.

A fixed LONG-only close-confirmed time-stop grid (12 rules, train-selected)
also fails consistency testing. The rule selected by its training result,
60m/close<=0R, changed paired return by +1.656R in train but -1.882R in
validation and +1.664R in holdout; the respective IID intervals were
[-2.310,+6.157], [-3.807,-0.276], and [-0.613,+4.498]R. The validation loss
rejects it as a robust rule despite the positive holdout point estimate. A
fixed runner-allocation grid on the live early-trail profile also had near-zero
holdout deltas (+0.002R to +0.008R), all with intervals spanning zero; the
average win/loss ratio remained about 0.097R. No additional exit-control
change is justified by these repeated post-hoc searches.

A separate trail-width sweep illustrates the payoff trade-off: widening to
0.40R activation/0.30R distance raised holdout win/loss to 0.399R but returned
-1.597R (11/18 wins), versus +0.665R and 0.097R under the live early trail
(17/18). At 0.75R/0.50R, payoff rose to 0.743R while holdout fell to -3.300R
(7/18). Improving the ratio alone is not enough; realized expectancy is the
objective. Keep the current profile unchanged pending prospective evidence.

A direct post-E3 stop-risk trim was also replayed against the exact early-trail
profile. On holdout, caps of 0.50R, 0.75R, and 0.95R returned -1.095R, -0.236R,
and +0.503R respectively, each below the untrimmed +0.665R. The 0.95R cap
reduced average loss from 1.020R to 0.482R but also lowered win rate from 94.4%
to 88.9%; its paired delta was -0.163R (IID 95% interval [-0.444R,+0.095R]).
All three caps also lost paired return in validation. This limits loss size but
does not improve expectancy reliably; do not deploy it.

## Corrected exits and entry-quality diagnostics — 2026-10-08 16:43Z

Correction: the exploratory diagnostics initially labeled “active early trail”
used the baseline 0.5/1/1.5R target schedule. Production `payoff_early_trail`
uses 1/2/4R targets with 15/20/25% partial exits and a 40% runner. The results
below supersede those earlier calculations and use the exact production profile.

On the 16:42Z archive, the exact early-trail profile returned +1.089R train,
+1.385R validation, and +0.665R holdout. Delaying E3 by 240 minutes returned
+0.202R, +1.484R, and -2.423R. The paired holdout change was -3.089R (IID 95%
interval [-6.652R,+0.156R], circular-block-4 [-6.431R,0.000R]). LONG holdout
moved from -0.210R (8/9 wins) to -2.345R (6/9); SHORT moved from +0.875R (9/9)
to -0.079R (8/9). Reject the delay; this already-inspected replay is not
independent validation.

A fixed 3x3 early-close grid (5/15/30 minutes; close thresholds 0/-0.25/-0.50R)
also failed: all nine rules reduced return versus the exact profile across
train, validation, and holdout. The train-selected 30m/close <= -0.25R rule
returned -0.660R, -0.731R, and -0.948R, with paired changes -1.749R, -2.115R,
and -1.613R. The 5m/close <= 0R rule was +0.181R on holdout but lost in train
(-1.358R) and validation (-0.234R); its holdout delta was -0.485R. Do not
promote early adverse-close exits.

The exporter now includes a four-hour pre-fill candle warm-up, and the
follow-through analyzer reads those raw candles separately from replay candles
(which intentionally exclude pre-entry bars). On 58 reconciled positions, the
15m pre-signal signed move was near zero for both sides. The 60m mean was
+0.092R for LONGs and -0.023R for SHORTs, with both intervals spanning zero.
At 240m, LONGs averaged +0.262R (n=28; block-4 95% interval [-0.080R,+0.645R]);
SHORTs averaged -0.218R (n=36; [-0.399R,-0.027R]), meaning SHORT signals tended
to be countertrend before entry. However, trend-alignment groups did not
generalize across chronological splits: 60m LONG countertrend cases went from
-0.541R in train (n=3) to +0.252R in holdout (n=4), while aligned LONGs moved
from -0.263R (n=8) to -0.845R (n=5). The groups are small and regime-dependent;
do not deploy a simple trend filter.

Finally, ignoring trader TP caps under the exact profile changed paired return
by +0.772R in train, +0.389R in validation, and only +0.010R in holdout. All
holdout confidence intervals included zero, and removing the three largest
positive deltas left -0.009R. Trader-TP capping alone does not explain the
low-payoff problem; do not remove it based on this search.

## Profitability optimization checkpoint — 2026-10-08 17:33Z

The fresh archive has 59 complete positions: 40/59 wins (67.8%), -$298.55 net,
$22.73 average net winner versus -$63.56 average net loser (0.358 dollar
payoff), -$5.06 expectancy per position, and -0.0265R expectancy. In R units,
the payoff ratio is 0.435 and the break-even win rate is 69.7%; in dollars the
break-even win rate is 73.7%. Losing positions carried larger initial risk on
average than winners. At the observed 40/19 win/loss counts, average winners
would need to rise to about $30.19 (+32.8%), or average loss fall to about
$47.84 (-24.7%), to break even. These are arithmetic thresholds, not forecasts.
The side split remains stark: 25 LONGs net -$401.55 with -0.239R expectancy;
34 SHORTs net +$103.00 with +0.130R expectancy. The SHORT 95% win-rate interval
is [60.0%,87.6%], so this is promising direction evidence but not a proven
filter.

The 0.10%-risk LONG cohort is 4/20 complete (XRP +$0.83, GALA -$7.41, MINA
+$0.90, ENA -$7.27): -$12.95 net, -0.428R expectancy, 0.118 dollar / 0.159R
average win/loss, 50% wins, $0.895 fees, and -$0.004 funding. Its IID 95%
mean-net-R interval is [-1.018R,+0.162R]. The payoff-profile cohort is 5/20
complete because it also includes the earlier XRP at 0.25% risk; keep that case
separate when judging sizing. The five-profile total is -$12.01 net, -0.325R
expectancy, 0.121 dollar / 0.134R average win/loss, $1.490 fees, and -$0.004
funding; its IID mean-R interval is [-0.797R,+0.147R]. Neither cohort shows a
profitable edge so far.

Two older frozen replay shadows now contain five eligible cases, all LONGs.
They compare against the legacy 0.5/1/1.5R policy, not the active
`payoff_early_trail` policy, so they cannot decide whether to change the current
Demo profile. Their challengers still have negative simulated expectancy
(-0.618R and -0.350R); gains are concentrated in three cases. A new, explicitly
prospective `--prospective-demo-trail-shadow` was frozen at 17:32Z to compare
the active 0.20R/0.10R early trail against a 0.40R/0.10R trigger, holding the
1/2/4R targets, allocation, and risk geometry fixed. It starts at 0/20 on this
archive; no pre-freeze cases are counted. Run it with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/ccb-forensic.zip \
  --prospective-demo-trail-shadow
```

At the same snapshot, five current positions had $266.44 stop risk and eight
older pending entry orders $169.23, or $435.67 combined—$95.67 above the $340
cap. The position-side estimate uses the exchange's configured stop-loss price
(conservative for SOL, which also reports an active trailing stop). The cap
affects newly planned entries only and does not cancel existing orders. No
orders, positions, or live settings were changed; the pending entries remain
untouched pending direct operator direction.

Read-only recheck — 2026-10-08 17:45Z
--------------------------------------

The fresh Bybit Demo archive (17:44:57Z) still has 65 filled Strategy V2 cases,
59 complete reconciliations, 149 Closed-P&L rows, and 625 executions—the same
counts as the 17:30Z archive. The app ledger has no new intent after 15:34Z.
All four completed 0.10%-risk LONGs still carry `payoff_early_trail`; this
cohort remains 4/20, -$12.95 net, -0.4282R expectancy, 0.118 average dollar
win/loss and 0.159 in R, with IID mean-R 95% interval [-1.018R,+0.162R]. The
five-position profile cohort remains 5/20, -$12.01 net and -0.3252R; it
includes the separate earlier 0.25%-risk XRP case. No new completed case was
observed. The five open positions still have exchange stop protection. Their
marks changed, so generated dashboard snapshots were refreshed; reconciled
closed-position P&L did not change.

The frozen prospective trail challenger (0.40R activation versus the active
0.20R, with the same 0.10R trail distance and target schedule) remains at 0/20
eligible cases on this archive. No post-freeze position has completed for this
comparison; the older retrospective replay is not substituted for it.

The direct paired historical comparison clarifies its baseline: over the
chronological 50/20/30 split, 0.40R activation versus the live 0.20R profile
changed net return by -0.379R in train, +0.151R in validation, and -1.459R in
the latest 18-case holdout. The holdout IID 95% interval is [-5.660R,+2.379R]
and circular-block-4 is [-4.529R,+1.978R]; excluding its three largest positive
case deltas leaves -2.803R. Although 0.40R beats the legacy baseline in that
holdout, it loses to the actual active 0.20R comparator. Do not promote it from
the retrospective result. The interaction reporter now emits this direct paired
comparison; reproduce with `compare_allocation_interaction.py BUNDLE`.

The new `--retrospective-current-demo-sizing` report applies the current 0.10x
rule to only the 20 historical 1.0%-risk LONGs, preserving four already-live
0.10%-risk LONGs, the separate 0.25%-risk XRP, and all SHORTs. Under a linear
size/P&L/fee assumption, total net changes from -$298.55 to +$52.03 (+$350.59
paired), but the IID 95% interval for that paired dollar delta is
[-$53.36,+$787.68] and circular block-4 is [-$95.17,+$825.64]. Excluding the
three largest positive deltas leaves +$126.92. Crucially, risk-normalized
expectancy is unchanged at -0.0265R per position: smaller LONG sizing reduces
dollar exposure but does not fix the system's payoff/expectancy. This is a
retrospective, linear counterfactual—not a prospective profitability result.
Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/profitability-20261008T1738Z.zip \
  --retrospective-current-demo-sizing
```

The eight legacy, unfilled entry orders remain untouched alongside the five
open positions; their combined stop-risk estimate still exceeds the $340 cap.
No settings, services, orders, or positions were changed.

## Deployment and state

## Demo total-risk-cap experiment — 2026-10-08

A fixed $340 open stop-risk cap is enabled for newly planned Demo entries. On
the 58-position historical archive, the retrospective 5x-mean-planned-risk
cap changed total net P&L from −$291.28 to −$19.92. The already-inspected latest
18-position holdout delta was +$286.20 (paired circular-block-4 95% interval
[$27.61, $597.38]); removing its three largest positive deltas left +$57.44.
Across all 58 cases the paired block interval crossed zero (−$17.14 to
$689.26), and capped net P&L remained negative. Treat this as a sizing
hypothesis, not evidence that the strategy is profitable.

Refresh on the 59-complete-position 17:44Z archive: the 5x cap is $334.85 and
changes the linearly modeled total from −$298.55 to −$34.85 (+$263.70 paired).
The benefit is highly time-concentrated: +$281.28 in the latest 18-position
holdout, −$17.58 in validation, and zero in train. Removing the three largest
positive deltas leaves only +$34.94. Modeled net remains negative overall and
its circular-block-4 mean-P&L interval spans zero; these are retrospective
linear-scaling results that do not model changed fills, slippage, or market
impact. The deployed cap is a risk constraint, not a profitability fix.

The implementation sums current exchange-stop risk plus unfilled V2 ladder
orders, reduces only new plans to remaining capacity, and rechecks the cap
against live state immediately before execution. Unknown/unprotected exposure
fails closed. Existing positions and orders are unchanged. Verify prospective
results separately from the 0.10x LONG and payoff-profile cohorts.

Combined live-allocation replay on the same 59 complete positions scales only
historical 1.0%-risk LONGs to 0.10x, preserves the four actual 0.10x LONGs, the
separate 0.25x XRP, and all SHORTs, then applies a fixed $340 cap to overlapping
position risk. The point estimate moves net P&L from -$298.55 to +$174.41; five
positions would have been skipped and three partially scaled. Mean expectancy
is +0.036R and the R win/loss ratio 0.443, but the 95% mean-R intervals cross
zero (IID [-0.130R,+0.192R], block-4 [-0.125R,+0.186R]). The paired dollar
delta's block-4 interval also crosses zero; train/validation/holdout net deltas
are +$284.87/−$119.40/+$307.50, so the apparent benefit is unstable by period.
The simulation excludes pending-order risk and assumes linear P&L/fee scaling;
it is an allocation hypothesis, not demonstrated profitability. It applies
today's settings retrospectively to old trades and is not an independent
prospective test. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/profitability-20261008T1738Z.zip \
  --retrospective-live-sizing-cap --portfolio-stop-risk-cap-usdt 340
```

An exploratory fixed-cap sweep at $200/$250/$340/$500 produced combined net
point estimates of +$44.26, +$49.31, +$174.41, and +$52.03 respectively, with
mean-R estimates +0.0055/+0.0307/+0.0364/−0.0265. All four block-4 bootstrap
intervals for mean R and mean dollar P&L cross zero. Validation net remained
negative at $200 (−$23.50), $250 (−$61.14), and $340 (−$33.34), while the $340
holdout point estimate was the largest and has already been inspected. There
is no stable cap optimum in this reused sample; do not tune the live $340 cap
to these figures.

Risk-basis sensitivity: applying the $340 cap against full planned maximum
loss (reserving all ladder risk from entry through closure) is more conservative
than the filled-position-risk replay above. It partially scales 6 trades and
skips 6, yielding +$81.89 rather than +$174.41; validation remains negative
(−$47.73) and holdout contributes +$80.74. The combined mean-R point estimate
is +0.031R, but its block-4 95% interval is [-0.128R,+0.178R]; the paired
block-4 mean-dollar-delta interval is [-$2.14,+$16.13]. This reserves planned
risk longer than actual unfilled orders may remain live, so it is a conservative
approximation—not an exact event-level replay. It weakens, but does not reverse,
the conclusion that the apparent edge is unproven. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_forensic_pnl.py /tmp/profitability-20261008T1738Z.zip \
  --retrospective-live-sizing-cap --portfolio-stop-risk-cap-usdt 340 \
  --portfolio-cap-risk-basis planned
```

Sweeping the planned-risk basis at $200/$250/$340/$500 gives net P&L
+$64.67/+$20.15/+$81.89/+$95.76 and mean-R point estimates
−0.0083/+0.0055/+0.0307/−0.0090. Every bootstrap interval for mean R and mean
dollar P&L crosses zero. Validation totals are +$1.90/−$14.41/−$47.73/−$33.34;
holdout is −$11.21/+$6.82/+$80.74/+$80.21. The apparent optimum changes with
exposure basis, and the $500 result has negative R expectancy. This confirms
there is no stable retrospective cap setting to promote; retain the current
cap only as a risk limit pending prospective evidence.

## Current $400-cap sizing refresh — 2026-10-09 08:44Z archive

Re-running the current 0.10x LONG sizing and $400 concurrent stop-risk cap on
the 60 complete-position archive scales 20 baseline-sized LONGs, preserves the
four actual 0.10x LONGs and all SHORTs, and partially scales three positions
while skipping two under the actual-filled-risk basis. The counterfactual net
is +$190.87 (versus −$284.91 actual history), with +0.0137R expectancy and a
0.586 dollar average win/loss ratio at the unchanged 68.33% win rate. These are
retrospective linear-sizing results, not changed-exit results or prospective
proof. The 95% mean-net-R intervals cross zero (IID [−0.159R,+0.175R], block-4
[−0.147R,+0.170R]); validation is −$33.34 while holdout is +$161.68. Pending
order risk, altered fills, minimum sizes, slippage, and market impact are not
modeled. Keep the cap as a risk limit; the four-case prospective 0.10x LONG
cohort remains negative and is the relevant live evidence.

### Entry-order lifecycle coverage — 2026-10-08 18:21Z

The refreshed read-only archive `/tmp/profitability-order-lifecycle-20261008T1816Z.zip`
contains 65 filled V2 cases, 59 fully reconciled positions, 140 historical entry
order rows, and 8 currently open entry-order snapshots. Of 195 expected E1/E2/E3
orders, 53 E2/E3 orders are absent from both order views and have no matching
execution. They cannot be assigned an exact cancellation time from this archive;
Bybit history has limited retention for old unfilled cancellations. This is why
the filled-risk and planned-risk cap simulations remain exposure bounds rather
than an event-accurate replay. The recent cohort can be tracked prospectively
while order lifecycle data is still available.

The 0.10x LONG cohort is 4/20 complete (2 wins, 2 losses, −$12.95 net,
−0.428R expectancy, 0.159R average win/loss ratio); it has no matched SHORT
controls. The `payoff_early_trail` cohort is 5/20 (3 wins, 2 losses, −$12.01 net,
−0.325R expectancy, 0.134R average win/loss ratio) and includes the separate
prior 0.25%-risk XRP case. These samples are too small to select a more
profitable configuration; leave settings unchanged and collect fully closed,
prospective cases.

An exploratory delayed-market-entry diagnostic on the same archive tests waiting
15 or 60 minutes, skipping a case if its original stop is touched while waiting,
then measuring mark-to-market 60 or 240 minutes after entry. A 60-minute wait
changes LONG 60-minute movement from -0.252R (n=29) to +0.055R (n=24), but its
block-4 95% interval is [-0.100R,+0.265R]; at a 240-minute horizon the result is
still -0.175R (n=24, interval [-0.310R,-0.041R]). The wait excludes 5 of the 29
LONG cases, so selection/stop avoidance contributes to the apparent near-term
improvement. A 15-minute wait remains negative at both horizons. This diagnostic
is not a stop/target/fee/slippage-aware trade replay and does not justify a live
delay rule; it only sharpens the entry-timing hypothesis for a prospective,
predeclared test.

## Demo outcome update — 2026-10-08 18:33Z

The 18:28Z archive reconciles the newly closed SOLUSDT SHORT fully: 5.6 units
closed, +$13.64 net (+0.317R), $0.69 fees, and +$0.16 signed funding. It is a
baseline-risk SHORT, not part of either prospective LONG/profile cohort. The
whole-position sample is now 60 complete (41 wins, 19 losses, 68.3% win rate),
−$284.91 net, −$4.75 expectancy/trade, and a 0.354 average win/loss ratio.
Break-even at the observed payoff requires a 73.9% win rate. Mean-R 95% intervals
still span zero (IID [−0.195,+0.151], block-4 [−0.192,+0.147]).

Side results remain sharply different but retrospective: 25 LONGs are 14/25,
−$401.55 net (−$16.06/trade, 0.319 average win/loss), while 35 SHORTs are 27/35,
+$116.65 net (+$3.33/trade, 0.361 average win/loss). This is a reason to keep
side-specific evidence separate, not enough to switch off LONGs: the cohort
selection is historical, the new 0.10x LONG sample is still 4/20 at −$12.95,
and it has no contemporaneous SHORT controls. `payoff_early_trail` remains 5/20
at −$12.01. Dashboard data was regenerated from the 18:28Z archive and live
SQLite snapshot; account Closed-PnL rows remain separately labeled from whole
positions.

The side split is unstable by time window: SHORT train/validation/holdout
expectancy is +0.152R/+0.013R/+0.216R (net +$94.18/−$46.61/+$69.07); LONG is
−0.339R/+0.495R/−0.423R (net −$316.52/+$132.67/−$217.71). The validation LONG
result is only four all-winning trades. This undercuts a blanket SHORT-only
change: the apparent edge is positive in train/holdout but almost flat and
negative in validation. Keep current settings until a prospective side cohort
can be evaluated without selecting the rule on these same folds.

## Payoff concentration check — 2026-10-08 18:52Z

A refreshed archive still reconciles the same 60 whole positions; Bybit's
Closed-PnL response has 150 rows, while the local account cache now has 163
records after a late-history sync. The dashboard was regenerated from the
current SQLite cache and this archive; the account-row series remains separately
labeled from complete-position performance. Cost decomposition sharpens the
optimization priority: gross price P&L is already −$169.54 before $118.91 of
fees, with +$3.54 funding, so eliminating all trading fees would still leave
the sample around −$166. LONG gross price P&L is −$356.95 before $44.07 fees;
SHORT gross is +$187.41, reduced by $74.84 fees (with +$4.08 funding). Thus
LONG entry/exit quality is the primary historical loss source, while reducing
SHORT execution costs could preserve more of its positive gross edge. Excluding
the largest one, three,
or five *winning* positions changes whole-sample net from −$284.91 to −$345.14,
−$456.72, or −$547.62. Excluding the largest three losing positions improves
net to −$36.39 (still negative); excluding five makes it +$119.71. This is
descriptive concentration, not an actionable filter: the loss identities are
selected after observing outcomes, and the full-sample mean-R intervals still
cross zero. It reinforces that containing a small tail of large losses matters,
but does not identify a prospective rule that can do so without also truncating
winners. Do not change stops, direction filters, or live risk based on this
post-hoc deletion test. Keep collecting the separately tagged 0.10x LONG cohort
(4/20) and payoff-profile cohort (5/20); neither sample has reached its review
target, and the LONG cohort has no contemporaneous SHORT control yet.

Path check on those five completed `payoff_early_trail` positions: a replay of
the active 60/25/15 entry split, 0.33/0.66R ladder, 0.20R trail activation,
0.10R distance, and 1/2/4R targets produced −1.730R versus −1.626R realized
(five all-LONG cases; per-case simulated/actual R: XRP +0.063/+0.086, ENA
−1.011/−1.011, XRP +0.110/+0.117, GALA −1.017/−1.025, MINA +0.125/+0.206).
The three winners all reached at least 0.20R favorable excursion; neither
loser reached the 0.20R trail trigger, and both lost about 1R. This small,
post-outcome path comparison is consistent with the trail banking modest gains
after favorable movement, but cannot prevent entries that fail before the
trigger; it does not validate signal quality or justify an entry filter. Keep
the current profile cohort separate and wait for its predeclared 20-position
review.

Paired exit-only counterfactual on those same five fills holds entry allocation,
depths, 1/2/4R targets, target percentages, and observed fees fixed. Changing
only the trail from 0.50R activation / 0.30R distance to the active 0.20R /
0.10R setting changes simulated results from −4.179R to −1.730R (+2.449R;
three of five improve). The early-trail candidate is still negative, and two
cases contribute +2.360R (96%) of the paired gain; after removing those two,
the remaining delta is only +0.089R. This favors retaining the current early
trail over reverting to the slower trail while the Demo cohort is collected,
but the n=5 all-LONG, outcome-concentrated comparison is not evidence of a
profitable strategy or a basis to increase risk.

## Loss cut before trail activation — 2026-10-08 19:07Z

Added a research-only `--untriggered-time-stop-diagnostics-only` replay mode:
after 60/240/720 minutes, close at the 1m close only when price is at or below
−0.25/−0.50/−0.75R and favorable excursion has not reached the active 0.20R
trail trigger. On the 60 complete paths (30 train / 12 validation / 18 already-
inspected holdout), none of the nine fixed variants improved all splits. Most
reduced returns in train and validation as well as holdout. The sole positive
holdout delta was +0.210R for 240m/−0.75R; train and validation were unchanged,
and holdout remained negative at −0.221R. Reject this loss-cut family for now;
the small holdout gain is not independent evidence and does not justify a live
stop change. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py \
  /tmp/profitability-refresh-20261008T1848Z.zip \
  --reconciled-only --untriggered-time-stop-diagnostics-only
```

## Trail activation threshold sensitivity — 2026-10-08 19:10Z

The active-profile GALA LONG peaked at +0.167R, just below the +0.20R trigger,
so a lower +0.15R activation was tested with the same 0.10R trail distance,
entry allocation/depths, 1/2/4R targets, and fee schedule. On the five
`payoff_early_trail` cases, the replay moves from −1.730R at 0.20R to −0.696R
at 0.15R, mostly by changing GALA from −1.017R to +0.050R; this is a
post-selected five-case result. On all 60 complete cases, paired net delta is
only +0.049R (mean +0.0008R; IID 95% [−3.089R,+3.949R], block-4
[−3.757R,+4.659R]). Validation is materially worse (−0.580R, IID
[−1.301R,−0.066R]); holdout improves +0.750R but becomes −0.344R after
removing its three largest positive deltas. Reject 0.15R as a production change;
retain 0.20R while collecting the predeclared live cohort. This is another
example where a compelling near-miss trade does not generalize.

## E3 exposure interaction refresh — 2026-10-08 19:12Z

On the same 60 complete positions, the 24 that filled E3 are 6 wins / 18 losses,
−$984.05 net and −0.634R mean; the 36 that did not fill E3 are 35 wins / 1
loss, +$699.14 net and +0.388R mean. The five completed `payoff_early_trail`
cases show the same association: E3-filled trades are 1/3 and −$13.85, versus
2/2 and +$1.84 for E1-only trades. This sharpens E3 fill as a high-value risk
marker, but does not prove that suppressing E3 would cause the no-E3 outcomes:
E3 fills only after an adverse move and changes average entry, size, and fees.
Prior no-E3/reduced-E3 replays did not show stable holdout gains, so retain the
current allocation and track this pattern prospectively rather than disabling
E3 from the retrospective association.

## Signal-time movement across execution outcomes — 2026-10-08 19:19Z

To test whether filled-only follow-through was hiding execution-selection bias,
added the read-only `scripts/analyze_intent_followthrough.py`, which compares
all `NEW` intents (including failed/skipped executions and intents without a
plan) against archived 1m candles. The live DB had 103 intents, all with plans:
80 EXECUTED and 23 FAILED. At 60m, 85 had candle coverage (74 executed, 11
failed); 18 were unavailable because their symbols were absent from the archive.
At 240m, one more LONG candle path was unavailable. Movement is signed percent
from the close of the first fully closed 1m candle at/after plan creation to the
first close at/after each horizon. It is hypothetical price movement, not
stop-R or executable PnL; fees, slippage, and strategy exits are excluded.

At 60m, all LONGs averaged −0.755% (n=39 across 24 symbols; symbol-cluster
bootstrap 95% interval [−1.345%,−0.301%]); executed LONGs were −0.759% (n=32),
and failed LONGs −0.733% (n=7). All SHORTs averaged +0.338% (n=46 across 22
symbols; interval [−0.027%,+0.690%]), so the short-side point estimate is
uncertain. At 240m, all LONGs averaged −1.547% (n=38 across 23 symbols;
interval [−2.514%,−0.719%]), while all SHORTs averaged +0.019% (n=46; interval
[−0.623%,+0.587%]). The adverse LONG movement is not confined to executed
positions in this archive, but the analysis is still observational: symbol
resampling does not account for shared market-time regimes, correlated signals,
or source-specific selection, and the horizons overlap. It weakens the claim
that the aggregate trade win rate proves uniformly strong signal quality; it
does not show that skipping LONGs improves strategy PnL. Keep current Demo
settings unchanged and collect the separately tagged prospective cohorts.
Reproduce against the current live DB and a refreshed archive with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_intent_followthrough.py \
  /state/cautious_crypto_bro.sqlite3 /tmp/profitability-refresh.zip
```

The same archive's 60 complete realized positions offer only weak source/side
leads. `Scalping Blog | Адель` SHORTs were 9/9 winners (+$218.43; mean win
$24.27), but the chronological split is only 4/4 train, 3/3 validation, and
2/2 holdout. A 9/9 Wilson 95% win-rate lower bound is about 70%, below the
72.4% break-even win rate implied by the overall $24.27 average win versus
$63.56 average loss; with no observed loser, its payoff ratio is unestimated.
Conversely, `Ivan Medvedev` LONGs were 2/5 and −$149.28, but all five occurred
in train, with no later validation/holdout cases. These are source-selection
leads, not validated filters; keep the existing paper-only source/side health
shadow and do not suppress any source/side based on this retrospective split.

A paired active-exit replay confirms that caution: holding the 0.20R/0.10R
trail and 1/2/4R targets fixed, changing 60/25/15 to 70/25/5 gives
−0.459R/+0.843R/−2.581R across train/validation/holdout versus
+1.354R/+1.589R/−0.431R for current sizing. 75/25/0 gives
−0.455R/+0.779R/−3.163R. On the five live-profile fills, the simulated totals
are −2.874R and −2.858R, both worse than −1.730R. These runs reinforce that
the E3-filled cohort's losses do not imply that reducing/removing E3 improves
outcomes; do not change the ladder on this evidence.

## Supervisor-polled trail activation sensitivity — 2026-10-08 19:36Z

Code inspection found that V2 does not arm the trailing stop with an exchange
activation price at entry. `PositionSupervisor` polls every 2 seconds; after it
observes the configured +0.20R threshold, `_freeze_entries` cancels scale-ins,
refreshes account state, and only then installs a trailing stop. The Bybit call
currently sends `trailingStop` without `activePrice`. A fast excursion and
retracement during polling/cancellation/account refresh could therefore leave
the exchange with only the original stop. This is a plausible execution gap,
not evidence that any of the five observed profile trades actually missed
activation. Bybit documents an `activePrice` parameter that can arm a trailing
stop at a specified trigger price ([official V5 Set Trading Stop API](https://bybit-exchange.github.io/docs/v5/position/trading-stop)).

Added a research-only `--trail-activation-poll-diagnostics-only` comparison.
On the 60 reconciled historical paths, requiring the 1m candle close to confirm
the trigger changed net replay by −1.392R in train, +0.129R in validation, and
−1.450R in holdout versus intrabar high/low activation. Train and holdout paired
intervals cross zero; the 1m-close challenger is only a conservative bound,
not a model of the live 2-second poll, and one-minute OHLC cannot resolve the
actual trigger sequence. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py \
  /tmp/profitability-refresh-20261008T1848Z.zip \
  --reconciled-only --trail-activation-poll-diagnostics-only
```

This identifies a concrete reliability improvement to investigate—exchange-
native trigger arming with explicit active price—but it does not establish that
changing protection will make the strategy profitable. Do not modify or replace
live stops from this replay alone; validate the Bybit Demo behavior and restart
recovery path before proposing deployment.

```sh
docker compose up -d --build app
docker compose logs -f app
docker compose stop app
```

Back up SQLite before state repair; stop the app to prevent concurrent mutations.
Restore a paused strategy only after checking ownership, orders and protection.
Never replay uncertain submissions blindly. Do not run `docker compose down -v`
unless intentionally erasing SQLite, Telegram-session and Redis volumes.

## Pre-signal momentum versus realized outcomes — 2026-10-08 19:49Z

The 19:42Z forensic archive contains 65 filled V2 cases, 60 complete
whole-position reconciliations, and 4 live positions. A new research diagnostic
joins each complete case to signed pre-signal momentum using only fully closed
1m candles, then compares actual after-cost net R across chronological 50/20/30
splits. The candidate is deliberately simple and fixed at zero: skip signals
whose preceding 60m/240m direction-aligned momentum is positive. No thresholds
were optimized. Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/analyze_signal_followthrough.py /tmp/profitability-current.zip
```

For the 60m feature, the all-direction baseline mean was −0.028R (n=30) in
training, +0.174R (n=12) in validation, and −0.139R (n=18) in holdout. Skipping
positive aligned momentum retained 16, 7, and 8 cases, with means +0.073R,
+0.037R, and +0.455R respectively. The apparent holdout gain is not supported
by validation and was found after inspecting the sample; it is not prospective
evidence. LONG holdout was −0.423R (n=10) baseline versus +0.252R (n=4) filtered,
but the filtered LONG train mean was −0.541R (n=3), so the direction-specific
result is also inconsistent. At 240m, the overall holdout point estimate moved
from −0.139R to −0.041R, but uncertainty spans zero and LONG holdout remained
negative. Do not deploy this filter; any
further evaluation should use a fresh, separately tagged forward Demo cohort
before considering promotion.

The same archive's fixed-horizon marks show LONG outcomes near flat at 5m
(−0.020R), then negative at 60m (−0.252R; block-4 95% [−0.444,−0.091]) and
240m (−0.290R; [−0.425,−0.145]). This supports investigating entry timing as
well as exits, but does not identify a profitable entry rule. Delayed-entry
counterfactuals remained negative at 4h. Broad time-stop sweeps and stops
restricted to pre-trail positions showed no stable improvement across train,
validation and holdout; retain the active exit profile while collecting
prospective outcomes.

## Prospective 60m momentum-filter shadow — frozen 2026-10-09

The historical filter's 60m holdout gain (+0.455R across eight retained cases)
was not supported in training or validation, and the 240m result was
inconclusive. Freeze a no-live-change shadow check at
`2026-10-09T08:50:14Z`: for each fully reconciled position whose plan is created
after this timestamp, classify the signal using the preceding 60 minutes of
fully closed 1m candles. Keep signals with nonpositive side-aligned momentum;
mark positive-momentum signals as skipped. The zero threshold and horizon are
fixed; do not retune them on this cohort.

Run the existing signal diagnostic with
`--prospective-momentum-filter-after 2026-10-09T08:50:14Z`. It reports all,
retained, and skipped outcomes in net R, with fees/funding included and results
separated by persisted exit profile and side. Current-archive verification
at 12:14Z returns one completed post-cutoff pair: a LONG under
`payoff_early_tight_trail_long_015` at +0.124R, classified retained, with no
skipped case. Its one-case 95% interval is degenerate and conveys no useful
uncertainty estimate. This is an observational counterfactual: all signals
continue to trade, so it estimates selection only and does not model
redeployment of skipped risk or market impact. Review after 20 completed
post-cutoff positions; do not promote unless retained expectancy is positive
with both IID and block-4 95% intervals above zero in the relevant profile/side
groups. It is not a live filter or profitability claim.

## Demo target-distance sweep — 2026-10-08 19:50Z

Compared the active `payoff_early_trail` replay (0.60/0.25/0.15 entry risk,
0.20R activation / 0.10R trail, 1/2/4R targets) with fixed target-only
variants 1.25/2.5/5R, 1.5/3/6R, and 2/4/8R. All target fractions, entry levels,
trail, observed fills, lifecycle events, and maker/taker fees were held fixed.
On 60 complete paths, the 1.25/2.5/5R variant changed net replay by −0.052R in
training (paired block-4 95% interval [−0.254,+0.075]), +0.009R in validation
(n=12), and +0.009R in holdout (n=18). In holdout average winner moved only
from 0.1026R to 0.1032R; average loss remained −1.014R and win rate 88.9%.
Only one holdout case changed, so the tiny apparent gain is not evidence of a
better payoff profile. Wider target variants reproduced the same results,
indicating the active trail usually exits before these targets matter. No
target-distance change is warranted from this replay.

Reproduce with:

```sh
docker compose exec -T app /app/.venv/bin/python \
  /app/scripts/replay_strategy_v2.py /tmp/profitability-current.zip \
  --reconciled-only --demo-target-sweep-diagnostics-only
```

## Demo tight-trail distance experiment — 2026-10-08

Compared 0.05R against the active 0.10R trail distance with 0.20R activation,
entry allocation, targets, fills, and fees fixed. On 60 reconciled paths, the
0.05R candidate lost 0.493R versus baseline in training (block-4 interval
[−1.575,+0.327]), gained 0.033R in validation, and gained 0.333R on the
previously inspected holdout (18 cases; block-4 interval [−0.020,+0.607]).
Holdout LONG delta was +0.256R across 10 cases, but the candidate LONG result
remained −0.939R. This is mixed, post-hoc evidence, not proof of profitability.
Use a separately tagged Demo cohort to test it prospectively; keep existing
plans and open positions on their serialized policies. Review only complete
positions, separately by profile and LONG risk multiplier.

## Directional decomposition under tight-trail exits — 2026-10-09 06:00Z

Replayed the same 60 reconciled historical fills with the exact tight-trail
exit geometry (0.20R activation / 0.05R distance; 1/2/4R targets and
15/20/25% reductions), retaining observed lifecycle/funding events and
maker/taker fees. This is a retrospective R-normalized exit counterfactual;
it is not realized performance for the prospective profile and does not model
the live 0.10x LONG risk multiplier.

SHORTs were positive in each chronological fold: +1.097R/19 in train,
+0.976R/8 in validation, and +0.885R/8 in holdout. Combined, 34/35 were
winners and net replay was +2.958R (+0.085R per SHORT); one loss was −1.065R,
while the mean winner was only +0.118R (0.111 win/loss ratio, roughly 90%
break-even win rate). The 34/35 Wilson 95% interval is 85.5–99.5%, which
includes win rates below break-even; one observed loss is not enough to
estimate the tail-loss frequency reliably.

LONGs netted −0.705R/25 overall: −0.305R/11 in train, +0.539R/4 in validation,
and −0.939R/10 in holdout. The full 60-case tight-trail replay was +2.254R,
but the reused holdout was nearly flat (−0.054R). This makes SHORT-only a
plausible prospective hypothesis, not a proven filter: direction results
remain post-hoc, the holdout was previously inspected, and the short-side
win/loss ratio still demands an unusually high win rate. Do not disable LONGs
or promote a directional filter from this sample; compare a separately tagged
forward short-only shadow against the existing 0.10x LONG cohort before any
policy change.

## Source and direction interaction under tight-trail exits — 2026-10-09 06:00Z

As a fixed decomposition (no source or threshold was selected from outcomes),
grouped the same tight-trail replay by persisted source channel and direction.
`Мысли Эмилии` accounted for 34 cases: 33 wins / 1 loss and +3.208R total.
Its chronological folds were +0.594R/14 (13 wins), +0.970R/7 (7 wins), and
+1.644R/13 (13 wins). The split detail is LONG 2/2 wins in train, 4/4 in
validation, and 7/7 in holdout; SHORT was 11/12, 3/3, and 6/6 respectively.
The main other positive subgroup was `Scalping Blog | Адель` SHORT at 9/9 and
+1.098R, but that is only 4/3/2 trades across the folds. `Ivan Medvedev` LONG
was 3/5 and −1.067R, all in training with no validation or holdout cases;
`Scalping Blog | Адель` LONG was 2/4 and −1.846R, with both losses in holdout.

This suggests outcomes are heterogeneous by source and side, and that a
global LONG ban would discard the positive `Мысли Эмилии` LONG replay. It is
not a source-filter recommendation: the exit policy and source breakdown reuse
inspected historical outcomes, most subgroup counts are small, and the
`Мысли Эмилии` result still depends on one loss against many ~0.1R wins. Keep
all sources/directions in the current low-risk Demo cohort and report new
fully reconciled cases by source, side, and serialized exit profile before
considering any selection rule.

`analyze_forensic_pnl.py BUNDLE --prospective-demo-exit-profile
--demo-exit-profile payoff_early_tight_trail` now emits the same cohort's
per-side and source/side metrics, fees/funding, and bootstrap intervals as
complete tagged positions arrive. This is reporting only; it does not gate or
change execution.

## Broad-search challenger versus active tight-trail exits — 2026-10-09 06:00Z

Reran the broad candidate search on the latest archive (60 reconciled cases,
chronological 30/12/18 split), ranking only on training paired delta after
removing the three largest positive contributions. The top-ranked candidate
used 75/20/5 entry risk, 0.25/0.50R entry depths, 0.75R trail activation,
0.40R trail distance, and 1/2/4R targets with 15/20/25% reductions. It was
then compared on the same cases against the active `payoff_early_tight_trail`
exit geometry (60/25/15, 0.33/0.66R, 0.20R activation / 0.05R trail, same
targets); fills, lifecycle/funding events, and maker/taker fees were held fixed.

The train-selected candidate was worse by 1.264R in training and 5.717R on
holdout; its holdout paired 95% IID interval was [−11.180R,−0.288R] and
circular-block-4 interval [−9.845R,−1.783R]. Validation favored it by only
0.568R, with both intervals crossing zero. On holdout, tight-trail replay was
−0.054R (89% wins, 1.868R max drawdown), while the candidate was −5.771R
(33% wins, 7.380R drawdown). The candidate's larger mean winner (0.507R vs
0.123R) and smaller mean loser (−0.734R vs −1.014R) did not compensate for
the win-rate collapse. This is a direct example that improving average
win/loss alone can worsen realized expectancy. The reused historical holdout,
small sample, and replay assumptions prevent promotion claims; the analysis
does not incorporate the prospective 0.10x LONG-sizing effect. Keep Demo
settings unchanged and continue collecting the separately tagged tight-trail
cohort.

## Prospective cohort reporting integrity — 2026-10-09

The combined `analyze_forensic_pnl.py` invocation now reports both the reduced-
risk LONG and selected exit-profile cohorts; previously the first report
returned early and hid the second. The LONG-risk cohort is additionally limited
to its frozen `payoff_early_trail` profile, and its baseline-sized SHORT controls
must use that same profile. This keeps later `payoff_early_tight_trail` trades
separate rather than double-counting them in the earlier sizing experiment.

The latest archive still has 4 completed LONG-risk cases and 0 completed
tight-trail cases. Since that snapshot, the database has no new intents, plans,
or lifecycle actions, and Bybit has returned no new Closed-PnL rows. At the
latest account check, all 4 open positions and 7 resting entry legs had stop
protection; combined estimated stop risk was $366.17 under the $400 cap. No
strategy filter is promoted from the exploratory historical results.

The reduced-risk LONG cohort remains at 4/20 complete: 2 wins and 2 losses,
50% win rate, 0.118 average dollar win/loss ratio, −0.428R expectancy, and
−$12.95 net P&L. Gross price P&L was −$12.05, fees were $0.895, and signed
funding was −$0.004. The IID 95% mean-net-R interval is [−1.018R,+0.162R];
with only four observations, this is too uncertain for a strategy decision.
There are still no contemporaneous baseline-sized SHORT controls. The
prospective report now emits per-cohort fees/funding and both LONG/control
confidence intervals, including explicit unavailable results for an empty
control group.

## All-intent source/side diagnostic — 2026-10-09

Extended `analyze_intent_followthrough.py` to report 60m and 240m hypothetical
movement by source, side, and execution status. The current database has 103
`NEW` intents with plans; the archive supplies usable horizon candles for 85,
with 18 unavailable. At 240m, LONG movement was −1.578% overall (n=39), similar
for executed signals (−1.595%, n=32) and failed signals (−1.503%, n=7). SHORT
movement was +0.019% overall (n=46; symbol-cluster 95% interval
[−0.623%,+0.587%]).

Source/side results are heterogeneous. `Мысли Эмилии` LONG was −2.765% at 240m
(n=14 across 9 symbols; symbol-cluster interval [−4.176%,−1.339%]), while its
SHORT mean was +0.008% (n=28 across 15 symbols; [−0.608%,+0.640%]). `MENSA
TRADING` LONG was +0.430% (n=8 across 4 symbols; interval crosses zero). These
are post-signal close-to-close marks—not executable P&L—and source groups were
not prespecified. Symbol resampling does not account for shared market-time
regimes, overlapping horizons, stops, exits, fees, or slippage. This weakens the
claim that aggregate win rate establishes signal quality, but does not justify
a source/side filter. Keep the Demo policy unchanged and validate any candidate
on a fresh forward cohort.

A chronological 50/20/30 split with 240m overlap purging preserves the
`Мысли Эмилии` LONG reversal in the inspected holdout: train n=2/1 symbol at
−6.525%, validation n=2/2 symbols at −7.102%, and holdout n=10/8 symbols at
−1.145% (symbol-cluster interval [−2.181%,−0.098%]). Its SHORTs changed from
positive train/validation means (+0.313%/n=15 and +0.418%/n=6, both intervals
crossing zero) to −0.999% in holdout (n=7/4 symbols). This points to substantial
regime dependence; the tiny early folds and four-hour mark horizon are not a
basis for suppressing the source or side.

The preexisting paper-only gate that suppresses a source/side after three
nonpositive completed signals was also checked on its five eligible cases. It
would have suppressed three trades that netted +$2.67; the filtered subset net
was −$14.68 versus −$12.01 baseline, with paired IID 95% dollar delta
[−$4.50,−$0.83]. The gate is both low-sample and harmful in this small review;
it remains paper-only and should not be promoted.

## Stop and E3 counterfactual follow-up — 2026-10-09

Added an excursion breakdown to the trail-distance replay so losing cases can
be separated into those that reached trail activation and those that failed
before it. Across the 60 reconciled paths, every losing case stayed below the
0.20R activation threshold. This points away from trail width as a rescue for
these losses; it does not establish that entries are generally sound.

Stop-distance and E3 alternatives were then checked on the same chronological
30/12/18 train/validation/holdout split. The train-selected 2x stop-and-grid
counterfactual reduced holdout loss from −6.101R to −1.616R, but remained
negative and its paired 95% bootstrap interval for the delta crossed zero
[−2.101R,+10.879R]. It also assumes nominal-risk resizing and omits
liquidation constraints. E3 stop caps remained negative on holdout (−3.331R
for 0.50R; −4.515R for 0.75R). Waiting for a later E2 reclaim improved the
holdout by +0.313R, but its paired interval also crossed zero
[−0.765R,+1.299R].

Exploratory close-confirmed time stops likewise did not produce a positive
holdout result; the train-selected 60m/−0.25R rule returned −4.954R. These
small, reused historical folds are not independent confirmation, and no
candidate is promoted. Keep live settings unchanged and wait for the frozen
prospective tight-trail cohort rather than selecting a strategy from these
replays.

## E3 allocation ablation — 2026-10-09

Tested the more direct “remove the third add” hypothesis with the same
tight-trail exit geometry. The 15% E3 risk was redistributed proportionally
across E1/E2, preserving total planned risk and the E1:E2 allocation ratio.
On the 18-case holdout, the baseline was −0.054R versus −3.364R with E3
disabled; the paired delta was −3.310R (95% circular-block interval
[−6.931R,+0.272R]). The interval still crosses zero, and small-sample
resampling is unstable, but this result rejects treating the conditional
three-leg loss concentration as evidence that removing E3 improves expectancy.
Keep E3 unchanged pending prospective evidence.

## Current-profile target/trail diagnostics — 2026-10-09

Corrected the “active Demo” target/trail replay baseline to mirror the running
`payoff_early_tight_trail` geometry (0.20R activation, 0.05R trail distance);
the old diagnostic had reused the earlier 0.10R trail profile. On the same
18-case holdout, farther target levels changed only one case and improved net
by just +0.003R, leaving the candidate at −0.051R. Target distance alone is
therefore not a material lever in these paths.

With the 0.05R profile as baseline, widening the trail to 0.10R changed holdout
net from −0.054R to −0.387R (paired delta −0.333R; IID 95% interval
[−0.558R,−0.051R], circular-block interval [−0.608R,+0.018R]). At 0.15R,
holdout net was −0.351R and the intervals crossed zero. The LONG slice (n=10)
favored 0.05R over both wider distances, while the SHORT slice is only eight
all-winning cases and is inconclusive. This supports keeping the existing
tight setting for now, but the small, previously inspected holdout is not
prospective validation; no performance claim or further promotion follows.

## Trail activation sweep — 2026-10-09

Added a predeclared 0.10/0.15/0.20/0.25/0.30R activation sweep while holding
the production 0.05R trail distance, entries, target levels, and allocations
fixed. Earlier activation (0.10/0.15R) improved the 18-case holdout by
respectively +0.276R/+0.722R, but the paired IID intervals crossed zero and
the gains were concentrated in only one/two cases; both settings were worse
than baseline in validation. Later activation (0.25/0.30R) improved validation
but lost −1.612R/−2.229R versus baseline on holdout. The LONG and SHORT
subsets also disagreed across time splits. No trigger generalizes across
train, validation, and holdout, so the best holdout result is not a promoted
global setting. It motivated a separately tagged Demo-only hypothesis:
0.15R activation on LONGs and the existing 0.20R on SHORTs, at unchanged 0.05R
trail distance and reduced 0.10x LONG risk. New plans only use this profile;
serialized plans/open positions stay frozen. Review it prospectively after 20
completed tagged positions; this experiment does not establish profitability.

## Prospective-profile sample-size context — 2026-10-09

The latest 60 fully reconciled outcomes have a sample standard deviation of
0.688R per position. Under a normal, independent-outcome approximation, a
20-position sample would have a 95% mean-R interval with a half-width around
0.30R. Detecting a true +0.20R mean with 80% power at a two-sided 5% level
would require about 93 independent outcomes; detecting +0.10R would require
about 371. These are planning estimates, not forecasts or a stopping rule.
Serial correlation, symbol/source clustering, profile-specific variance, and
side-specific effects can require substantially more data. Treat the 20-case
gate as an interim review only; do not promote a profile from win rate or a
positive point estimate alone. Continue to report after-cost R expectancy,
payoff ratio, fees/funding, side splits, and IID plus block-bootstrap intervals
on fully reconciled prospective cases.

## Randomized LONG trail-activation comparison

Newly planned LONGs are assigned 50/50 from the persisted intent UUID using a
stable SHA-256 bucket. Control activates the tight trail at +0.20R; treatment
activates at +0.15R. Both arms retain the same +0.05R trail distance, target and
entry rules, 0.10x base LONG risk, portfolio cap, and 240-minute entry TTL.
Distinct profile tags allow independent measurement. SHORTs are unchanged and
excluded. Existing plans and positions are unaffected.

Analyze each arm using `--prospective-demo-exit-profile` and its exact profile
tag: `payoff_early_tight_trail_long_ab_020_control` or
`payoff_early_tight_trail_long_ab_015`. Compare after-cost expectancy, payoff,
fees/funding, and IID plus block-bootstrap intervals. Twenty complete LONGs per
arm is an interim review, not proof; detecting a +0.20R mean with 80% power was
estimated to require about 93 independent outcomes. Do not promote based on win
rate or a point estimate alone.

## Current execution-fee mix — 2026-10-09 09:19Z archive

Across 67 filled V2 cases, 67 market E1 fills paid $40.31 on $67,933
notional; seven already-limit E1 fills paid $1.39 on $6,926. E2/E3 maker fills
paid $12.29 combined. A same-notional conversion of every market E1 to the
observed 0.0200% maker rate would save about $26.73 before missed fills,
slippage, or adverse selection; this is only a fee ceiling, not an executable
counterfactual. Market stop-loss and trailing exits paid $33.75 and $23.06,
respectively; these are substantial but protective costs, so do not weaken stops
solely to save fees. On the separate 60-position complete scorecard,
gross price P&L was −$169.54 with +$3.54 net funding: even zero fees would leave
about −$166.00. Fees matter, but entry-fee savings alone cannot fix the observed
loss distribution; improving entry selection or the payoff distribution remains
necessary. Do not infer that limit entries preserve fills from this aggregate.

## First completed `payoff_early_tight_trail_long_015` case — 2026-10-09 09:53Z

The fresh Demo archive reconciles SANDUSDT LONG as the first fully completed
position under this profile (2 filled cases, 1 completed). It netted +$0.5342
(+0.1238R) after $0.0948 fees, with +$0.6290 gross price P&L and zero funding.
There is no losing case yet, so the average win/loss and payoff ratio are
undefined; the 100% observed win rate has a Wilson 95% interval of
[20.65%, 100%]. The n=1 bootstrap interval is degenerate and is not useful
uncertainty evidence. This single small winner does not establish profitability
or show that average winners now compensate for full-stop losses. Keep the
profile tagged and continue collecting complete positions. The earlier 0.10x
LONG-risk `payoff_early_trail` cohort remains separate at 4 completed cases,
−0.428R expectancy, and no SHORT controls.

At the same account snapshot, the three new XRPUSDT entry legs were resting
with the planned 1.3177 stop; all 11 open entry orders and all 6 open positions
had stop protection. Combined estimated stop risk was $387.78 of the $400 cap
(96.94% utilization), leaving about $12.22 capacity. No existing orders or
positions were modified. Dashboard data was refreshed from this archive.

After adding that one close, the all-profile reference is 61 fully reconciled
positions (7 remain incomplete): 42/61 wins (68.85%), −$284.37 net, and a
0.346 dollar average-win/average-loss ratio (+$21.98/−$63.56). At the observed
counts, break-even needs a 74.30% win rate, a 30.8% larger average winner, or a
23.6% smaller average loser, holding the other factors fixed. Risk-normalized
payoff is 0.425 (0.412R/−0.969R) with −0.0184R expectancy; IID and block-4 95%
mean-R intervals are [−0.192,+0.150]R and [−0.186,+0.144]R. Gross price P&L is
−$168.91, fees $119.00, and funding +$3.54. The newest chronological 19-case
slice is weaker at −0.125R expectancy and −$148.10 net. LONGs remain negative
in the historical split while SHORTs are positive overall, but neither the
aggregate nor the recent slice establishes a stable direction edge; retain
separate prospective cohorts and do not infer signal quality from win rate.

## Demo entry-order TTL experiment

Compose configures a 240-minute entry-order TTL for newly serialized plans.
The Strategy V2 supervisor cancels only exact owned entry links once the
persisted plan age reaches the TTL; unfilled plans close, while partial fills
keep their live position protected and freeze remaining entry legs. Legacy
plans without the field default to TTL 0 and are unaffected. Validate cohort
results with a fresh forensic bundle and
`analyze_forensic_pnl.py BUNDLE --prospective-demo-entry-order-ttl-minutes 240`.
Report only fully reconciled results; a 20-case review is an interim checkpoint,
not proof of a modest edge. The setting is intended to reduce stale-signal fills
and reserved stop-risk, not to promise improved average win/loss or expectancy.

## Demo profitability check — 2026-10-09 10:41Z

The refreshed read-only archive contains 68 filled V2 cases, 153 exchange
Closed-PnL rows, 61 fully reconciled positions, 6 open positions, and 10 owned
open entry orders. All 6 positions and all 10 entry orders have stop protection;
estimated combined stop risk is $387.78 of the configured $400 cap, leaving
$12.22. HYPE and UNI entry ladders remain open and unchanged. No fully reconciled
position is yet tagged with the 240-minute entry-order TTL.

Keep the two 0.10x LONG experiments separate. The original
`payoff_early_trail` sizing cohort is 4/20 complete (2 wins, 2 losses), net
−$12.95, with +$0.86 average winner versus −$7.34 average loser, a 0.118 dollar
payoff ratio (0.159R), and −0.428R expectancy. Fees were $0.895 and signed
funding −$0.004; there are no contemporaneous SHORT controls. The newer
`payoff_early_tight_trail_long_015` profile is 2 filled/1 complete: its sole
SANDUSDT LONG made +$0.534 (+0.124R) after $0.095 fees and zero funding. With
one winner and no loss, its payoff ratio is undefined and its Wilson 95% win-rate
interval is [20.65%, 100%]. Neither cohort establishes an edge. The dashboard's
LONG-risk cohort now explicitly matches the original `payoff_early_trail`
profile instead of blending later exit profiles into the sizing comparison.

Across all profiles, the 61-position reference remains 42 wins/19 losses,
−$284.37 net, +$21.98 average win versus −$63.56 average loss (0.346 dollar
payoff ratio), and −0.0184R expectancy. Break-even at the observed payoff
requires a 74.30% win rate; observed is 68.85%. IID and block-4 95% mean-R
intervals are [−0.192,+0.150]R and [−0.186,+0.144]R. LONGs are −0.225R over
26 complete cases while SHORTs are +0.135R over 35; these retrospective side
splits are leads, not a validated direction filter.

An all-intent check now has 108 NEW intents with 1-minute candle coverage for
most signals. At 240 minutes, signed close-to-close LONG movement is −1.539%
(n=40; symbol-cluster 95% interval [−2.435%,−0.803%]); executed LONGs are
−1.595% (n=32). SHORT movement is +0.019% (n=46; interval
[−0.623%,+0.587%]). This weakens the claim that aggregate win rate alone proves
signal quality. The movement measure is observational and is not executable
P&L; it does not justify disabling LONGs. Historical replay also finds every
loser failed to reach the tested +0.20R trail-activation threshold, so changing
trail distance alone is unlikely to rescue those losses. Focus further tests on
entry-selection hypotheses with chronological, purged holdouts; do not promote
a source/side filter or alter live settings from this evidence.

## Early-loss cut holdout check — 2026-10-09 10:56Z

Replayed 61 complete positions from the 10:41Z archive using close-confirmed
time/adverse-R cuts only when prior favorable excursion had not reached the
tested +0.20R trail trigger. The 60-minute/−0.25R rule reduced holdout net by
2.037R (holdout n=19); the 60-minute/−0.50R rule reduced it by 2.087R. The
240-minute/−0.50R rule also remained negative on holdout (−0.118R; paired
95% interval [−2.071R,+1.705R]). A 240-minute/−0.75R rule showed +0.210R
holdout delta, but changed no training or validation cases and depended on one
holdout loss (interval [0,+0.629R]); this is an isolated retrospective rescue,
not generalizing evidence. No early-loss-cut candidate is promoted. This
reinforces that entry selection, rather than another tuned exit threshold, is
the next research focus.

## Stop-geometry holdout refresh — 2026-10-09 10:56Z

The same 61-case archive was replayed with stop distance and entry-grid
alternatives while preserving nominal planned risk. The training-selected 2x
stop-and-grid candidate lost −2.028R on holdout (19 cases), versus −6.430R for
baseline; its paired 95% interval was [−2.195R,+10.741R]. A 0.75x stop-only
variant lost −3.201R (paired delta +3.228R, interval [−0.910R,+8.454R]); a
1.5x stop-only variant lost −2.933R (delta +3.496R, interval
[−0.675R,+8.738R]). All candidates remained negative on holdout. The simulated
fill mix and outcomes assume nominal-risk resizing and do not model changed
fills, slippage, or liquidation; these deltas do not validate a production stop
change. No stop geometry is promoted.

## Training-ranked source/side lead — 2026-10-09 11:03Z

On the same 61 complete cases, source×side cohorts with at least three
training trades were ranked by training expectancy in the chronological
30/12/19 split. `Scalping Blog | Адель` SHORT ranked first: training was 4/4
winners (+1.516R), validation 3/3 (+1.227R), and holdout 2/2 (+1.433R). Across
all nine trades it returned +4.176R and +$218.43; mean winner was +0.464R, but
with no observed loser its average win/loss ratio and break-even rate are
undefined. This is the strongest observed source/side candidate so far.

This remains a retrospective, post-hoc lead: source/side combinations and
multiple strategy hypotheses have already been inspected, and the holdout is
not independent. Its 240-minute all-intent movement is only +0.129% over 12
signals with a symbol-cluster interval [−0.916%,+1.416%], so the trade outcome
pattern is not independently explained by strong four-hour directional drift.
Do not whitelist this source or increase its risk from 9 winners. Keep the
current system collecting data and track the pair as a separate forward
candidate; review only fully reconciled after-cost R outcomes, fees, and a
cluster-aware interval after 20 completed forward cases. The 20-case check is
interim, not evidence of a modest edge.

A conditional exchangeability check illustrates the small-sample issue. In
validation there were 6 wins among 8 SHORT positions, so a fixed three-trade
group would be 3/3 winners with probability 20/56 (35.7%) under random label
assignment. In holdout there were also 6 wins among 8 SHORTs, so a fixed
two-trade group would be 2/2 with probability 15/28 (53.6%). Their product is
about 19%, before accounting for temporal dependence or the multiple groups
and hypotheses inspected. This is not a formal p-value; it shows why 3/3 and
2/2 cannot by themselves establish an edge.

## Randomized LONG exit comparison deployed — 2026-10-09 11:28Z

Because the preceding non-randomized `payoff_early_tight_trail_long_015`
profile remained too small and its second completed LONG lost, new LONG plans
are now deterministically randomized 50/50 by SHA-256 of intent UUID. The
control tag uses +0.20R activation and the treatment tag +0.15R. Both arms keep
the same 0.05R trail distance, entry/target rules, 0.10x base LONG risk,
portfolio stop-risk cap, and 240-minute entry TTL. Assignment is serialized in
the plan; existing plans and positions are unchanged. No randomized-arm plans
had been created by the 11:34Z dashboard snapshot, so the experiment has no
results yet.

The FARTCOINUSDT LONG then fully closed: 3,132 units at average exit 0.16209
from average entry 0.16446104. It reconciles under the earlier non-randomized
`payoff_early_tight_trail_long_015` pilot. That pilot is now 2 complete LONGs,
one win and one loss, −$7.3501 total and −0.4899R expectancy. Average net win
was $0.5342 versus a $7.8843 average loss (0.0678 dollar payoff; 0.1122R
payoff); fees were $0.5530 and funding was zero. The 95% IID mean-R interval
was [−1.104R,+0.124R]; with only two cases, intervals and point estimates are
highly unstable. The 50% observed win rate is not evidence that the new exit
profile works.

The whole-system scorecard grew to 62 complete positions: 42 wins and 20
losses, −$292.26 net, +$21.98 average win versus −$60.77 average loss, a 0.362
dollar payoff ratio and −0.0359R expectancy. The 67.74% observed win rate is
below its 73.44% fee-inclusive break-even rate. IID and block-4 95% mean-R
intervals are [−0.210R,+0.131R] and [−0.201R,+0.135R]. LONGs remain negative
overall (27 positions, −0.258R expectancy); the retrospective SHORT result is
still positive (+0.135R/position) but does not validate a side filter.

After the close, five positions and ten entry orders remained, all with
exchange stops; estimated combined stop risk was $380.63/$400 (95.16%). HYPE
and UNI entry ladders were preserved. Dashboard JSON/JS was regenerated at
11:34Z from the Docker database and fresh forensic archive. The newly deployed
randomized arms and 240-minute TTL cohort must be scored separately; keep the
20-case per-arm checkpoint interim and do not select a winner from the already
inspected historical replay.

## Prospective experiment and stale-entry snapshot — 2026-10-09 12:15Z

A fresh read-only Bybit Demo archive contains 69 filled V2 cases, 62 fully
reconciled positions, 7 incomplete cases, 6 open positions, 12 tracked open
entry orders, and no unmatched entries. All 6 positions and all 12 orders have
exchange stop protection. Estimated combined stop risk is $387.09/$400
(96.77%), leaving $12.91 capacity. HYPE and UNI ladders are unchanged.

The randomized 0.20R control has 1 filled and 0 completed positions; the
randomized 0.15R treatment has 1 pending plan, 0 executed, filled, or completed
positions. There is not yet an arm comparison. Keep the earlier, non-randomized
0.15R pilot separate: 2 completed LONGs, 1 win/1 loss, −$7.3501 net,
−0.4899R expectancy, and 0.112R average win/loss. Fees were $0.5530 and funding
was $0. The IID 95% interval for mean R is [−1.104,+0.124]; this two-case pilot
does not establish that the profile helps. The original 0.10x sizing cohort
remains 4/20 complete at −0.428R expectancy, with no contemporaneous SHORT
controls. These prospective results are too small to select a winning policy.

The live account snapshot shows two SAND entries at 38 minutes with their
saved 240-minute TTL. Three XRP entries are 147 minutes old; ZEC is 5,912
minutes old; UNI 7,102 minutes; HYPE 7,453 minutes. These legacy plans have no
saved TTL, so they are not labeled expired and are not affected by the current
TTL setting. All remain stop-protected. The dashboard now displays each
tracked entry's age and saved-TTL status; its generated JSON/JS was refreshed
from this archive. No live orders, positions, or settings were changed.

## Signal-quality and paper-gate refresh — 2026-10-09 12:18Z

Recomputed raw intent markouts for 110 new signals against the 12:14Z archive.
At 240 minutes, signed close-to-close LONG movement is −1.774% across 41
signals (symbol-cluster 95% interval [−2.838%,−0.928%]); executed LONGs are
−1.885% across 33 ([−3.152%,−0.820%]). Executed SHORT movement is −0.031% over
42 signals ([−0.675%,+0.612%]). This is not realized P&L, excludes costs and
exit management, and its symbol bootstrap does not account for shared market
regimes. It does contradict the blanket claim that a high realized win rate
proves every direction/source has sound raw signal quality; the evidence is
specifically unfavorable for this LONG markout sample.

The frozen paper-only source/side health rule has 7 eligible complete cases
after its cutoff (20-case interim review target). It would suppress 4 cases
with −$5.22 combined realized P&L, but the 3 kept cases still have −$14.15 net
and −0.637R expectancy, worse than the 7-case baseline’s −$19.36 and −0.372R.
The paired IID 95% P&L-delta interval is [−$5.09,+$22.72]. This is too small
and uncertain to support enabling the gate; keep it paper-only. Current
randomized exit arms remain at control 1 filled/0 complete and treatment 0
filled/0 complete (one treatment plan pending), so no exit-profile winner can
be selected. No production policy change is supported by this refresh.

## Whole-system payoff and side uncertainty — 2026-10-09 12:22Z

Recomputed all completed-position metrics directly from the 12:14Z archive:
62 cases, 42 wins/20 losses (67.74% win rate), −$292.26 net, $21.98 average
winner versus −$60.77 average loser (0.362 dollar payoff), and −$4.71 per
position. Dollar break-even requires 73.44% wins. In initial-risk units, the
mean winner was +0.412R and mean loser −0.976R (0.422R payoff; 70.33%
break-even), for −0.0359R expectancy. Average initial risk was also smaller on
winners ($48.25) than losers ($61.54), so the dollar payoff gap reflects both
sub-unit winners and a less favorable risk allocation. The 95% IID and block-4
mean-R intervals are [−0.210,+0.131]R and [−0.201,+0.135]R; the win-rate
interval [55.37%,78.05%] includes the dollar break-even rate. Holding the
observed counts fixed, dollar break-even requires the average win to rise to
$28.94 (+31.66%) or the average loss to shrink to −$46.16 (24.04%). In R,
the corresponding thresholds are +0.465R average win (+12.87%) or no worse
than −0.864R average loss (11.40% smaller). The gap between dollar and R
thresholds is consistent with losers having carried more initial risk on
average; uniform risk scaling alone cannot fix negative R expectancy.

The side split is a useful lead but not a validated filter. LONGs are 15/27
winners, −$408.90 net, and −0.258R expectancy; their block-4 mean-R interval is
[−0.557,+0.060]R. SHORTs are 27/35 winners, +$116.65 net, and +0.135R
expectancy; their block-4 interval is [−0.049,+0.315]R. Both intervals cross
zero. Alongside the negative LONG markouts above, this supports prioritizing a
prospective entry-selection/participation experiment over another global exit
sweep, but it does not justify a live long ban or short-only policy. Keep all
historical side and source comparisons labeled retrospective until a
predeclared forward cohort shows positive after-cost expectancy with adequate
uncertainty bounds.

## BTC-relative signal markout check — 2026-10-09 12:30Z

Added a paired benchmark diagnostic to `analyze_intent_followthrough.py`. It
subtracts the same-window, direction-aligned BTCUSDT 4-hour return from each
signal's direction-aligned asset return and bootstraps by UTC signal day. In
the current 12:14Z archive, LONGs underperformed BTC by 1.681 percentage points
across 41 signals/17 days (day-cluster 95% interval [−2.629,−0.890]); executed
LONGs underperformed by 1.862 points across 33 signals/16 days
([−2.977,−1.013]). SHORTs were near zero relative to BTC: −0.057 points across
46 signals/17 days ([−0.497,+0.518]); executed SHORTs were −0.087 across 42
signals/17 days ([−0.541,+0.528]). This suggests the LONG markout is not
explained merely by BTC's average same-window move. It remains a descriptive
markout, not executable P&L: it does not adjust for each altcoin's beta,
signal-specific volatility, exits, fees, or all within-day regime dependence.
The effect is worth forward-testing, not a justification for a global LONG
ban; preserve the active entry-quality shadow and randomized exit trial.

## XRP pilot close and fresh Demo snapshot — 2026-10-09 14:36Z

A fresh read-only Bybit Demo archive reconciles the XRPUSDT LONG as fully
closed: all 74.2 units exited at an average 1.3867 against 1.3762 average entry,
for +$0.7021 net. The position was absent from the refreshed open-position
snapshot. Its serialized policy is the earlier, non-randomized
`payoff_early_tight_trail_long_015` profile, so it belongs only to that pilot.
The pilot is now 3/20 completed (2 wins, 1 loss), −$6.648 net, −0.273R
expectancy, and 0.129R average win/loss payoff. Average net win is $0.618
versus a $7.884 loss; fees are $0.630 and signed funding is zero. The IID 95%
mean-R interval is [−1.104,+0.162]R; with three cases this remains highly
uncertain. This close does not count toward the exact `payoff_early_tight_trail`
cohort (1 filled, 0 complete) or the older `payoff_early_trail` 0.10x sizing
cohort (4/20 complete, −0.428R).

Across all 64 fully reconciled positions, the account is still slightly
negative at −0.0153R expectancy (44/64 wins, 68.75%, versus 69.84% R
break-even), with a 0.432R win/loss ratio. IID and circular-block-4 95% mean-R
intervals are [−0.187,+0.148]R and [−0.173,+0.143]R. The latest closed-position
change improves the point estimate only marginally; it does not resolve the
uncertainty or establish an edge.

At the 14:30Z account snapshot, five open positions and eight entry orders all
had stop protection. Estimated combined stop risk was $378.93/$400 (94.73%),
leaving $21.07; pending HYPE/UNI ladders remain unchanged. The app restarted at
13:10Z and has no newer intents than 11:47Z, so the configured randomized LONG
participation test has no post-restart assignments yet. Dashboard JSON/JS was
regenerated from Docker's SQLite state and this archive. No settings, services,
orders, or open positions were changed.

## Frozen 60-minute momentum shadow refresh — 2026-10-09 14:39Z

Re-ran the pre-signal, close-confirmed 60-minute direction-aligned momentum
shadow on the 14:36Z forensic archive. In the already-inspected chronological
holdout, the unfiltered 20-position mean was −0.201R (circular-block-4 95%
interval [−0.402,−0.019]); retaining only nonpositive pre-signal momentum kept
9 positions with +0.115R mean and interval [−0.135,+0.360]. The LONG slice
moved from −0.283R across 14 to +0.027R across 7 retained cases, but its
interval was [−0.214,+0.268]. This is a retrospective subgroup with a previously
inspected holdout, not a causal or deployable gain.

The frozen prospective shadow now has only 2 complete pairs under
`payoff_early_tight_trail_long_015`; both were retained, averaging +0.143R, and
there are no skipped cases. The filter has not yet separated good from bad
outcomes in forward data. Keep it shadow-only and collect more assigned cases;
do not change entry policy based on the two winners or the historical
holdout.

## Paired payoff impact of the frozen momentum filter — 2026-10-09 14:43Z

Calculated the historical holdout's paired portfolio delta for the frozen
60-minute rule: a retained trade keeps its realized net R, while a skipped
trade contributes zero. Across all 20 holdout positions, the counterfactual
delta is +5.049R total (+0.252R per signal); its IID 95% interval is
[−0.037,+0.541]R and circular-block-4 interval is [+0.040,+0.463]R. For LONGs,
the delta is +4.151R across 14 signals (+0.296R per signal), but both IID
[−0.023,+0.618]R and block-4 [−0.022,+0.629]R intervals include zero.

This paired calculation is a useful prioritization signal, not proof of an
edge: the holdout was already inspected, the threshold was selected after
examining historical outcomes, and the counterfactual assumes skipped fills
would not affect other trades, portfolio capacity, or execution. Keep the rule
shadow-only. A prospective comparison needs enough independently assigned
LONGs on both sides of the pre-signal momentum threshold and complete take-arm
positions before reconsidering it.

## Momentum-stratified randomized allocation reporting — 2026-10-09 14:49Z

Extended `analyze_long_participation.py` to split randomized LONG take/skip
assignments by the frozen 60-minute, direction-aligned momentum rule. Each
stratum reports assignment-arm counts, completed/failed/unresolved take cases,
mean take-arm net USDT and R per assignment, and day-cluster intervals when
there is enough day coverage. The calculation uses only fully closed
pre-signal candles; missing history is reported as unclassified. Outcomes pool
over the independently randomized exit profiles and remain descriptive at low
counts.

The forensic exporter now fetches candle history for every tagged assignment's
symbol and signal time, including skip-only symbols. Before this fix, those
symbols could be absent because candle coverage was derived only from filled
positions, preventing a valid momentum-stratum comparison. Regression tests
cover both the classifier and exporter scope. The latest real archive still has
zero randomized assignments and the analyzer reports
`awaiting_tagged_assignments`; no current performance result changed. The
relevant test group passed 55 tests, and Ruff format/lint passed for all touched
Python files. No runtime settings, services, orders, or positions changed.

## First prospective LONG participation assignment — 2026-10-09 15:00Z

The live database now contains one assignment after the 13:10Z app restart:
the 14:44Z XRPUSDT LONG intent was tagged
`payoff_early_tight_trail_long_ab_015` with `long_participation_arm=skip`, and
was not executed. It is the first observed skip-arm case, not an outcome; the
latest forensic bundle predates the signal, so no post-signal market movement
is yet available for scoring. The exact `payoff_early_tight_trail` cohort and
the earlier 0.10x `payoff_early_trail` cohort remain separate.

At 14:55Z, the account sync contained no Closed-PnL rows newer than the already
recorded 14:25Z XRP close. Bybit still showed five open positions, each with an
exchange stop and matching active stop order; eight entry orders remained
pending, including the preserved HYPE/UNI ladders. No new fill or completed
candidate position was observed, so forensic data and dashboard exports were
not refreshed.

Full tests pass (577), Ruff format and lint pass, and Pyright reports zero
errors. That type check exposed and fixed a missing
`PositionStrategyRepository` composition in `SignalServiceStore`, which now
declares the active-strategy read used by portfolio stop-risk budgeting. No
production settings, services, orders, or positions were changed.

## Source/side paper-gate sample refresh — 2026-10-09 15:02Z

Re-ran the full forensic analyzer and its frozen source/side-health shadow on
the existing 14:36Z archive; no newer Closed-PnL result was present in the live
database. The 20-case source/side review target now has 9 eligible completed
cases. The three-prior-loss gate would suppress 5 cases with −$4.517 realized
P&L, for a +$4.517 descriptive paired dollar delta; its IID 95% interval is
[−$5.837,+$22.014], so the result remains uncertain. The 4 kept cases total
+$50.613 but still have −0.207R mean expectancy, versus −0.151R across the full
9-case baseline. Dollar totals and R tell different stories here because the
observed cases used different risk sizes. This small retrospective shadow does
not justify enabling the gate.

The all-time reconciled sample remains 64 positions: 44 wins, 20 losses,
−$226.80 net, −0.0153R expectancy, and 0.432R win/loss payoff. The latest 20
chronological cases are materially weaker than the earlier 44 (−0.201R versus
+0.069R expectancy), so current research should prioritize diagnosing the
recent regime/source/direction deterioration rather than increasing risk or
optimizing against the aggregate win rate. This is a descriptive split, not a
validated regime detector.

## Recent weakness decomposed by source and side — 2026-10-09 15:04Z

Split the same 20 latest reconciled positions by source and direction, then
compared the largest source's prior completed history using the same groups.
The recent LONG weakness is broad enough that it should not be reduced to a
single permanently bad feed: for `Мысли Эмилии`, earlier LONGs were 5/6 wins at
+0.248R mean, while the latest 8 are 5/8 wins but only 0.157R win/loss payoff
and −0.306R mean. That source's SHORTs also shifted from 12/16 wins and +0.099R
across 16 earlier cases to 3/5 wins and −0.106R across the latest 5.

Across all sources in the latest 20, LONGs are 8/14 wins, −0.283R mean, and
0.290R payoff; SHORTs are 4/6 wins and nearly flat at −0.009R mean. Other
source/side slices are very small (for example, the apparent +0.733R result in
two MENSA LONGs). These post-hoc windows are descriptive, not independent
validation; they point to recent regime decay shared across directions more
than they justify blacklisting a feed. Keep source filters paper-only and
continue the frozen forward assignment; the 14:44Z skipped LONG is still the
only new participation assignment and has no completed markout.

## Signal-versus-exit diagnosis refresh — 2026-10-09 15:06Z

Ran both markout analyzers against the 14:36Z forensic archive. At 240 minutes
after the first actual fill, direction-adjusted LONG movement averaged −0.315R
across 30 cases (23.3% positive; IID 95% [−0.468,−0.163]R; block-4
[−0.469,−0.160]R). SHORTs averaged −0.032R across 34 cases, with both
intervals spanning zero. These are price markouts using the initial stop
distance, not realized trade P&L; they omit exits, costs, subsequent fills, and
cases lacking a full forward window.

At the intent level, 44 LONG signals with a complete 240-minute window had
−1.736% mean directional movement (22.7% positive; day-cluster 95%
[−2.687%,−0.957%]); the 36 executed LONG subset was −1.830%
([−2.964%,−0.806%]). Benchmark-adjusted executed LONG movement was −1.825%
relative to BTC ([−2.646%,−1.008%]). The 46 SHORT signals were near flat
(+0.019%, interval [−0.623%,+0.587%]). These markouts are descriptive and do
not control for all shared market regimes, but they contradict the premise
that a high realized win rate by itself proves LONG signal quality is sound:
LONG timing/selection appears to contribute alongside the low win payoff.

The prospective 0.10x LONG-risk cohort remains only 4 complete positions, so it
cannot validate a fix; the lone 14:44Z skipped assignment has no scored forward
window in this archive. Keep all settings and source gates unchanged while the
randomized LONG participation and exit arms collect prospective cases.

## Side-only risk-off counterfactual — 2026-10-09 15:08Z

Summarized fully reconciled outcomes by side and the same chronological split.
Across all 64 cases, LONGs were 17/29 winners but returned −0.197R per position
with 0.374R payoff and −$343.44 net; IID 95% mean-R interval
[−0.466,+0.068]. SHORTs were 27/35 winners, +0.135R mean, 0.487R payoff, and
+$116.65 net; interval [−0.076,+0.330]. Both uncertainty intervals include
zero.

LONG expectancy was −0.117R across the earlier 44-position segment (15 LONGs)
and −0.283R in the latest 20 (14 LONGs). SHORT expectancy moved from +0.165R
(29 cases) to −0.009R (6 cases). This makes a prospective LONG-suppression
benchmark worth tracking, but the retrospective “keep only SHORT” result is
not a live-policy conclusion: it reuses inspected data and ignores how freed
portfolio capacity could change future SHORT sizing, overlap, and fills. Keep
the active 50/50 randomized LONG participation assignment unchanged until its
take/skip outcomes can be compared prospectively.

## Pre-signal BTC-regime diagnostic — 2026-10-09 15:11Z

Split the signal-level 240-minute LONG markouts on a fixed, pre-signal BTC
60-minute return sign (down versus flat/up); no threshold was fitted. In the
13 BTC-down cases, mean asset return was −2.586% and BTC-relative return was
−2.489% (day-cluster 95% interval [−3.902%,−0.634%]). In the 30 flat/up cases,
asset return was −1.426% and BTC-relative return was −1.360%
([−2.726%,−0.392%]). Executed-only subsets were also negative in both groups
(n=11 and n=25). These are same-archive, overlapping, non-randomized markouts,
not realized P&L or independent confirmation. They suggest that simply
avoiding LONGs when BTC's prior hour is negative would not address the observed
LONG weakness; any richer regime rule needs a new frozen prospective test.
