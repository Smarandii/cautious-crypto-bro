# Changelog

All notable changes to this project are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Validated on Bybit Demo.** These releases do not certify mainnet operation or
live profitability. Descriptive Demo checkpoints are not proof of a profitable
edge; see [STRATEGY.md](STRATEGY.md) for behavior and limitations.

## [Unreleased]

## [2.2.42] - 2026-10-31

### Fixed

- Bound zone execution-history reads to relevant frozen allocation creation
  times, retaining one day of clock/lookback headroom and exchange retention
  limits (#300). Recent-only allocations avoid unnecessary archive lanes;
  missing, naive, invalid or future legacy timestamps keep conservative catch-up.
- Replay a completed archive tail from its relevant lower boundary even when
  the wall clock advances. Previously the moving 48-hour boundary could keep
  reads near that edge indefinitely, stranding delayed older terminal fills.
  Unfinished pages retain their cursor/window ownership across restart.
- Recompute bounds for newly enrolled older allocations without inferring fills
  or dropping terminal groups. Coverage checkpoints describe only consumed
  pages; creation times never become evidence that history is complete.

### Validation and compatibility

- Add 15 real SQLite/native HTTP integration cases for both directions, recent
  and older allocations, late older enrollment, unknown clocks, cached query
  state, moving-boundary pagination, restart and explicit retention limits.
- On disposable copies of one production snapshot, a complete eligible-lane
  sweep reserves 8 query slots rather than12: six recent plus two archive lanes,
  compared with six of each. The existing request-rate/page ceilings are
  unchanged; this reallocates capacity to relevant reads rather than claiming
  a lower live request rate. Frozen ledger tables remain identical, with zero
  exchange API calls and no writes to the live database during comparison.
- Schema15 and existing persisted query rows remain readable; the allocation
  boundary is transient and recomputed after restart. No migration, dependency,
  risk, entry/exit policy or exchange mutation is introduced. Older versions
  retain state readability but can repeat irrelevant reads or miss delayed
  archive fills at the moving boundary. Multi-zone activation remains disabled;
  #123, #245, #248 and the full #262 audit retain their outstanding scope.

## [2.2.41] - 2026-10-31

### Fixed

- Require terminal entry-order, fill/fee and captured pending-order evidence for
  every completed whole-trade sample, including ordinary pre-v2.2.40 entries
  (#297). A closed primary does not complete a strategy with live E2/E3 orders.
- Reuse one terminal-entry validator in the exporter and reconciler. Recheck raw
  archive snapshots and their explicit capture times; missing, stale or
  contradictory evidence remains unresolved. Legacy archives remain readable,
  with unknown settlement rather than an implicit completed sample.
- Preserve actual PnL, fees, funding and frozen cohort metadata. Completion
  filters now also apply this evidence boundary to research reviews, dashboard
  aggregates and public trade rows. No orders are cancelled to complete a report.

### Validation and compatibility

- Add 36 real SQLite/native HTTP/export/reconciliation cases covering both
  directions and MARKET/LIMIT/RANGE, pending and settled scale-ins, partial IOC,
  missing/stale/contradictory archive evidence, and actual dashboard/public filters.
- A read-only production comparison on one captured snapshot retains economic
  totals while certifying 35 of 92 previously counted completed trades. The other
  57 lack terminal-entry history in the captured exchange response; this does
  not imply that all 57 have open exposure. Bybit history retention can prevent
  retrospective certification. Counts are descriptive at the audit capture.
- Schema15, dependencies, risk and entry/exit policy are unchanged. No migration,
  exchange mutation, historical source replay or synthetic live order is needed.
  Rollback preserves state readability, but older report readers can overstate
  verified completion when settlement evidence is absent. Mainnet and profitable
  expectancy remain unproven; #248 and the full #262 audit stay open.

## [2.2.40] - 2026-10-31

### Fixed

- Persist exact ordinary E1/E2/E3 request ownership before exchange submission
  (#296), including orders below exchange quantity ceilings. Preserve original
  client IDs, three logical risk budgets and native stop protection.
- Submit MARKET primary requests from the refreshed, durably saved preparation
  rather than resizing them inside an unrecorded submission. Recheck leverage,
  exchange ceilings and account ownership before mutation.
- Recover confirmed accepted order identities after acknowledgement loss or a
  failed remaining-plan write. Reload the durable plan during recovery; never
  retry order creation or promote failed/uncertain attempts to successful ones.

### Validation and compatibility

- Real SQLite and exchange HTTP integration covers MARKET/LIMIT/RANGE, both
  directions, partial IOC fills, acknowledgement loss, restart recovery and
  failed persistence before submission or after primary acceptance.
- Schema 15 remains unchanged. The existing optional physical-entry mapping now
  also describes ordinary singleton requests; v2.2.39 understands this format.
  No migration, dependency, risk-setting or exit-policy change is required.
- Demo validation does not certify mainnet or a profitable edge. Historical
  failures lacking durable ownership are not replayed; unavailable evidence
  remains unresolved. Legacy ordinary lifecycle settlement remains tracked in
  #297; profit magnitude and the full audit remain open in #248 and #262.

## [2.2.39] - 2026-10-30

### Fixed

- Include confirmed accepted entries from failed or uncertain submission attempts
  in forensic exports (#294). Read plans, bindings and strategy status from one
  read-only database snapshot; preserve the actual attempt status, frozen policy,
  source and cohort metadata. Never promote a failed intent or resume quarantine.
- Aggregate every physical E1 child's actual fills, keeping scale-ins separate.
  Retain filled E2/E3 evidence even if a primary LIMIT/RANGE entry was rejected.
  Reconcile submitted and filled quantities independently for partial IOC orders.
- Attribute closing executions by confirmed zone identity or Bybit's explicit
  attached-parent relationship. Validate native partial-close symbol, direction,
  quantity and closing role; a shared client-ID prefix does not establish ownership.
  Include actual fees, funding and native-stop losses without counting them twice.
- Require complete entry identities and settled order/fill/fee history before
  counting a mapped or failed attempt as a completed economic lifecycle. Pending
  entries, missing evidence and ambiguous closing identities remain incomplete.
  Filled failed attempts no longer masquerade as failed-before-fill assignments.

### Validation and compatibility

- Add 39 real SQLite/client/coordinator/export/reconciler integration cases across
  LONG and SHORT, MARKET/LIMIT/RANGE, partial acceptance and IOC fills, acknowledgement
  loss, restarted storage, native stops, missing/conflicting history and frozen
  assignment bookkeeping. Preserve ordinary successful exports and legacy bundles.
- Read-only validation against all 105 eligible production plans preserves their
  stored successful entry IDs; no trading mutation or source replay is performed.
- Schema 15, dependencies, risk settings, entries/stops/exits and runtime trading
  behavior remain unchanged. Rollback to v2.2.38 needs no state migration; regenerate
  reports with v2.2.39 to retain the new completeness checks. Older consumers that
  ignore the additive evidence flags cannot certify uncertain-attempt completeness.
- Unowned legacy failures, unavailable history and ambiguous closes remain
  unassigned or incomplete for human review. This release does not complete the
  natural profit-magnitude experiment (#248) or the full architecture audit (#262).

## [2.2.38] - 2026-10-30

### Fixed

- Submit quantity-limited entries as deterministic physical children of the
  unchanged E1/E2/E3 logical legs (#291, #288). Read current market/limit ceilings,
  quantity steps and minima; balance child quantities without reducing approved
  logical risk. Reject unrepresentable allocations and allocations exceeding
  native partial-stop capacity before submitting entries.
- Persist refreshed primary requests before submission and fill-derived scale-in
  allocations before submitting E2/E3. Aggregate confirmed primary fills while
  retaining original submitted quantities, including partially filled or empty
  terminal IOC children. Each entry retains its native stop-loss request.
- Recover accepted physical identities by exact exchange evidence after partial
  failures, lost acknowledgements and restart. Keep uncertain strategies paused
  for human review; never replay entry creation. Freeze the initial zone allocation
  before mapped submission and retain confirmed child bindings through recovery.

### Validation and compatibility

- Exercise MARKET, LIMIT and RANGE submission against real SQLite and the HTTP
  client, including durable request quantities, unchanged logical reference
  geometry, partial/empty primary fills, invalid limits, protection capacity,
  lost acknowledgements, partial batch acceptance and restart without replay.
- Read-only verification of the affected LONGXIA plan produces four physical
  entries with unchanged logical geometry under current public exchange limits.
  No historical signal replay or synthetic live trade is performed.
- Schema 15, risk settings, exit policy and dependencies remain unchanged.
  v2.2.37 can read and supervise mapped plans for rollback, but cannot submit new
  mapped entries. A fresh quote requiring a primary downsize fails preflight
  rather than changing a durably prepared request. Bybit acceptance and live
  profitability remain subject to actual market and account conditions.

## [2.2.37] - 2026-10-30

### Added

- Optional validated physical-entry slices under unchanged E1/E2/E3 logical
  strategy legs (#290, prerequisite for #288). Persist leg/child identities,
  submitted quantities and complete confirmed MARKET-primary fill evidence;
  reject ambiguous identities, coverage, quantity totals and invalid fills.
  Physical ownership retains submitted quantities even when IOC fills are partial.
- Use one physical-entry projection for supervisor drift/TTL checks, native
  attached-stop ownership/handoff, portfolio reference-price lookup and zone
  enrollment. Logical risk allocations, entries/stops/exits and whole-position
  ownership remain unchanged; children never receive independent risk budgets.

### Validation and rollback

- Real SQLite/HTTP/supervisor tests cover persistence/restart, corrupt mappings,
  child quantity/identity drift, TTL cancellation, native-stop drift and verified
  handoff, portfolio risk fallback, zone bindings and confirmed partial fills.
  Empty metadata is omitted: the actual v2.2.36 reader accepts identical ordinary
  plan JSON, with schema15 unchanged.
- This is an ownership/reader compatibility checkpoint, not quantity-aware
  submission. Mapped entry submission is explicitly blocked until #291 delivers
  ceiling-aware allocation, fill aggregation and accepted/uncertain-child handling.
  #288 remains open; oversize orders are not fixed by this release. No mapped
  plans are generated in production at this checkpoint. Keep the previous pinned
  image and restore-verified state backup; later slicing releases must retain this
  reader/ownership-capable image for rollback rather than an older reader.
- No settings, risk budget, entry/exit/protection policy or history replay change.
  #248 natural magnitude and #262 full architecture audit remain unfinished.

## [2.2.36] - 2026-10-30

### Fixed

- Validate every symbol position row before leverage preparation instead of
  rejecting all multi-row replies (#287). Bybit Demo can return distinct flat
  rows whose historical fields differ. Require exact symbol, explicit one-way
  mode, zero validated exposure, permitted flat status and one agreed validated
  risk ID. Multiple rows must each report an empty side; missing, conflicting,
  exposed, hedged or malformed evidence still blocks before mutation.
- Preserve current/projected tier ceilings, strict public mark, all-open-order
  checks and frozen risk geometry. Do not select the first row and ignore others.
  Single-row compatibility and the shared coordinator/cancellation path remain.

### Validation and rollback

- Real SQLite/coordinator/HTTP regressions reproduce rejected consistent flat
  rows for MARKET/LIMIT/RANGE entries, then verify success and unchanged durable
  geometry. Forty first/second-row conflict cases and empty responses verify no
  leverage/order mutation or strategy creation. Existing execution, tier,
  cancellation and source-recovery integration suites remain required.
- Schema 15 and trading settings/entries/exits are unchanged. Keep the previous
  pinned image and restore-verified backup. Verify actual affected-symbol
  preparation with writes disabled, authenticated supervision/protection, visible
  source intake and Docker publication before tagging. No historical replay;
  #248 magnitude and the full #262 audit remain open. Oversized per-order
  quantities are a separate remaining participation limitation.

## [2.2.35] - 2026-10-30

### Changed

- Give persisted OPEN dispatch and bounded candidate/restart recovery one owner
  in signals/queued_open.py (#285). Source intake and the periodic scheduler call
  that owner directly; remove the former service methods without facades or
  callbacks into the source coordinator. The owner reuses planning, delivery,
  context and execution-coordinator boundaries with five storage operations.
- Preserve original candidate IDs/deadlines, claim transfer, approval provenance,
  ten-candidate passes, retry backoff and uncertain-mutation no-replay handling.
  Initial and recovered OPENs retain the same coordinator and delivery ordering.
  Source extraction/persistence, account sync and position-action dispatch remain
  in their existing orchestration. Moved logs identify the queued-OPEN owner.

### Validation and rollback

- Compare all three moved method bodies and twelve retained service methods
  structurally after owner-reference normalization; constructor wiring is the
  deliberate change. Repeat the same real SQLite/HTTP source, candidate recovery,
  manual/AUTO delivery, cancellation, preview and workflow tests before/after.
- Schema 15, candidate JSON and trading settings are unchanged. Keep the previous
  pinned image and restore-verified backup for rollback. Verify authenticated
  startup, supervision/protection, visible post intake and Docker publication
  before tagging. #248 natural trade magnitude and the full #262 audit remain
  incomplete; this refactor does not establish profitability.

## [2.2.34] - 2026-10-30

### Fixed

- Retain actionable candidates across temporary Bybit planning reads (#283).
  Validated GET response codes 429/10000/10006/10016 carry explicit read-recovery
  metadata; planning no longer parses a provider error message to decide whether
  a rejection is permanent. Existing diagnostic messages remain readable.
- Persist candidate-only batches under the existing source claim transaction
  when every OPEN fails temporarily. Source completion cannot precede durable
  candidate insertion; invalid owners/sources/deadlines and insert failures do
  not leave a completed source without its candidate.
- Preserve already-selected approval modes in candidate JSON, where ordinary
  TradingIntent serialization excluded them. Legacy and previously MANUAL
  candidates stay MANUAL. IDs/deadlines/backoff remain unchanged; mutation errors
  keep existing failure/uncertain-strategy handling without automatic order replay.

### Validation and rollback

- Real SQLite/HTTP/source/recovery regressions cover public/private GET failures,
  restart, original approval/deadline preservation, backoff/expiry, permanent and
  malformed responses, candidate-only rollback/claim validation, legacy payloads
  and accepted-mutation responses without replay. Coverage remains UNRESOLVED
  while a candidate is pending and reflects execution after recovery.
- Schema 15 and trading settings are unchanged. Candidate JSON adds an optional
  approval_mode field accepted by earlier readers; legacy payloads are not
  relabeled or promoted. Rollback retains the previous pinned schema-15 image
  and restore-verified backup. No forced historical replay; #248 magnitude and
  the full #262 architecture scope remain open.

## [2.2.33] - 2026-10-30

### Changed

- Give durable manual approval and AUTO outcome delivery one owner under
  `signals/delivery.py` (#281). Initial dispatch and restart recovery call it
  directly; remove the service-owned delivery implementations and facades.
  Keep claim/send/completion, recorded outcomes, account previews and retry
  loading together. Planning recovery remains service orchestration.

### Validation and rollback

- Preserve seven moved methods and fifteen retained orchestration methods by AST
  comparison, allowing only owner/method reference changes. The same focused
  SQLite/HTTP/Telegram workflows pass before and after extraction; real-store
  lease tests cover concurrent delivery and cancellation followed by expiry.
- Schema 15, strategy, risk settings, source identity/deadlines and exchange
  protection are unchanged. Rollback uses the previous schema-15 pinned image
  and restore-verified backup; no historical replay. #262's recovery/illegal-state
  and full audit work remains open, as does #248's natural magnitude evidence.

## [2.2.32] - 2026-10-30

### Fixed

- Admit explicit flat one-way Bybit rows with empty status/position mark fields
  (#279). Read and validate the exact symbol's public ticker mark for projected
  notional; missing, malformed, nonpositive or nonfinite marks fail explicitly.
  Do not substitute last-traded price. Non-flat/mode/tier/ownership guards remain.

### Validation and rollback

- Reproduce the deployed failure for MARKET/LIMIT/RANGE with real SQLite/HTTP,
  then cover missing/unknown/liquidating status and invalid/wrong-symbol ticker
  marks alongside tier, refreshed-market, durable-plan and cancellation cases.
- The v2.2.31 deployment was rolled back before tagging because its live probe
  rejected valid empty flat-position fields. This checkpoint includes #277's
  tier-aware preparation after that correction; v2.2.31 was never released.
- Schema 15 and trading settings remain unchanged. Retain the restore-verified
  backup and previous pinned image for rollback; no historical replay. #248's
  natural trade magnitude and the full #262 architecture audit remain unfinished.

## [2.2.31] - 2026-10-30 (unreleased; deployment rolled back)

### Fixed

- Prepare tier-admissible leverage before entry mutation (#277). Read fresh
  instrument limits, current symbol tier, paginated public tiers and all open
  orders; reserve all planned legs for projected notional. Persist leverage-only
  adjustments under the existing coordinator lock before strategy creation.
  Retain original intent leverage, quantities, stops, exits and source deadlines.
- Revalidate admission before setting leverage. Unknown/unsupported evidence,
  exposure, entry orders and changed constraints fail explicitly; risk tiers and
  margin modes are never changed. Margin/permission failures do not replay entries.

### Validation and rollback

- Real SQLite/coordinator/planner/HTTP integration covers MARKET/LIMIT/RANGE,
  stale saved plans, persisted-before-mutation ordering, multi-page tiers, changing
  constraints, malformed/unsupported evidence, server rejection and cancellation.
- Schema 15 and settings are unchanged; older plans remain loadable. Rollback
  uses the previous schema-15 image and restore-verified backup. No historical
  replay or profitability claim. #274 retains its completion audit; #248 magnitude
  evidence and the broader #262 refactor remain incomplete.

## [2.2.30] - 2026-10-30

### Fixed

- Apply validated instrument leverage min/max/step constraints when building new
  entry plans (#275). A request above the instrument maximum is reduced to a
  supported value; a request below the minimum is rejected rather than increased.
  Original intent leverage remains stored, while the plan reports the selected
  value. Risk-based quantities, stop geometry and exits are unchanged.
- Require valid live Bybit leverage-filter metadata; malformed or missing bounds
  fail before planning. Offline contexts without a filter retain compatibility.

### Validation and rollback

- Reproduce the 50x versus 12.5x rejection through real planner/SQLite/HTTP
  integration for market and limit paths, then verify supported submissions,
  provenance, fractional steps, defaults, invalid bounds and unchanged geometry.
- Schema 15 and trading settings are unchanged; older persisted plans remain
  loadable. Rollback uses the prior schema-15 image and a restore-verified backup.
  No historical signal is replayed. Parent #274 remains open for live account
  risk-tier/notional and submission-time validation; #248 magnitude evidence
  and the broader #262 refactor are also incomplete.

## [2.2.29] - 2026-10-30

### Changed

- Give signal planning one owner under `signals/planning.py` (#265). Initial
  processing and durable candidate recovery use the same planning boundary with
  explicit plan, error, rejection and retry-candidate results. Source claims,
  decision persistence, durable recovery and delivery remain in `SignalService`.
- Remove moved routing, contract resolution, budget, policy and action-planning
  implementations from the service. Planning collaborators expose only the
  reads and approval-safety query they need; no exchange mutation moves here.

### Validation and rollback

- Compare existing initial/recovery workflows, contract aliases and duplicate
  routing, partial batches, portfolio admission, deterministic participation,
  policy snapshots and original retry identity/deadlines before and after.
- Schema 15, current risk settings, strategy exits and public feed stay unchanged.
  Keep a restore-verified state backup and prior schema-15 image for rollback.
  The broader #262 architecture audit and #248 natural trade-magnitude evidence
  remain incomplete; this checkpoint does not establish profitability.

## [2.2.28] - 2026-10-29

### Changed

- Keep an idle application-owned SQLite connection until runtime shutdown so
  the read-only Docker publisher can open live WAL state between app operations
  (#271). Complete the initial read before idling to allow normal checkpoints;
  close the connection with the existing cancellation-safe cleanup boundary.
- Move periodic public trade publication into an opt-in Docker Compose publisher
  (#268). The service reuses the runtime image and read-only exporters directly,
  with no host WSL/Docker-exec/Git commands or Docker socket. Share policy values
  with the app; mount state and selected credentials read-only, with a separate
  private work volume for bounded exports, singleton ownership and health receipts.
- Preserve feed validation, whole-trade reconciliation, 15-minute cadence,
  publication verification and private diagnostics. Health requires a recent
  successful publication; startup and failed cycles never claim readiness.

### Validation and rollback

- Verify HTTP/SQLite/export/publication integration, private-field rejection,
  bounded command failure, singleton ownership and stale/unavailable health.
  Verify a live service publication before removing the prior Windows task.
- Schema 15 and trading behavior are unchanged. Keep a restore-verified state
  backup and previous schema-15 image. Rollback stops the publisher before
  restoring the old schedule; never run both exporters concurrently. The broader
  #262 refactor, independent-zone activation and #248 magnitude review remain open.

## [2.2.27] - 2026-10-29

### Changed

- Replace the decorative public dashboard with plain, accessible Demo evidence
  (#264): current trades and settings first, dated historical analysis separately,
  system fonts, stable named metric bindings and ordinary expandable history.
  Remove authored health grades, protection counts, illustrative diagrams,
  numbered narratives and the stale manually counted test badge.
- Keep all published trade rows available while showing the latest ten first.
  Preserve frozen-risk cohorts, whole-position versus account-row distinctions,
  fees/funding, uncertainty intervals and historical assignment provenance.
- Load current evidence independently with bounded fetches. A failed current
  feed shows unknown settings rather than relabeling a historical budget as
  current; a refresh failure retains the last successful current evidence and
  its own timestamp. Stale evidence and bundled historical fallback stay explicit.

### Validation and rollback

- Exercise real public payloads and synthetic reordered, empty, stale,
  unavailable, independent-feed and bundled-fallback cases in the browser;
  inspect desktop and phone rendering. Producer privacy/data contracts remain
  unchanged. No framework, runtime dependency or frontend build step is added.
- Schema 15, trading strategy, approvals, risk settings and exchange behavior
  are unchanged. Preserve a restore-verified state backup before deployment;
  the previous schema-15 image remains compatible. The broader #262 architecture
  refactor, independent-zone activation and #248 magnitude evaluation remain open.

## [2.2.26] - 2026-10-29

### Changed

- Move provider-neutral signal extraction, transport normalization and lifecycle
  evidence into capability-owned `signals` modules (#263). OpenRouter retains
  its transport adapter; runtime factories, audit/replay tools and tests import
  interpretation rules directly from their owners. Remove the former extractor
  module and generic extraction facade exports without compatibility wrappers.

### Validation and rollback

- Compare all 32 moved definitions against the previous syntax trees, including
  the model prompt; exercise provider, cache, lifecycle, source recovery and
  SQLite workflow integration through existing tests. No dependency, model
  schema, fingerprint, entry/exit strategy or risk-setting change.
- Schema 15 is unchanged. Preserve a restore-verified state backup before
  deployment; the previous schema-15 image remains compatible. Never replay
  historical signals as part of this refactor. The broader #262 architecture/UI
  work, independent-zone activation and #248 outcome evaluation remain open.

## [2.2.25] - 2026-10-29

### Fixed

- Recover later-visible zone executions with equal or older timestamps (#256).
  Observed event maxima no longer exclude history. Durable recent and archival
  query lanes replay explicit windows against confirmed order ownership, including
  closed strategies, while execution IDs keep quantity and fee evidence idempotent.
- Bound each recording pass to four single-page reads, each containing at most
  100 rows. Persist cursor history and query scheduling across restart. Conflicts,
  malformed pages, repeated cursors and the 100-page query limit remain explicit
  failures; they never advance a query's completed-window record.
- Align the archival retention boundary by UTC day with a full day of request
  headroom (#260), preserving same-day pagination while keeping requests inside
  Bybit's moving two-year limit. An advanced boundary restarts expired coverage
  explicitly and never claims excluded history complete.

### Validation and rollback

- HTTP-adapter/SQLite integration covers delayed/equal-time fills, new bindings,
  terminal strategies, incomplete-page retry, cursor restart, fair query budgets,
  retained quantities/fees and schema preservation. Legacy migration/ownership
  checks remain in place. The published 2.2.24 reader rejects schema 15 unchanged.
- Schema 15 adds query-progress state and preserves existing records. Stop all
  writers and keep a restore-verified pre-upgrade backup. Rollback requires the
  matching schema-14 backup and exchange reconciliation, retaining post-upgrade
  evidence first; never lower the version pragma or start an old image on schema 15.
- The 730-day query lookback is a recorded limitation; exchange retention can be
  shorter. Consumed windows are replayed and do not prove immutable history.
  No entry/exit/risk-policy change, historical signal replay or new trading
  mutation. Independent-zone activation and #245/#123 remain incomplete.

## [2.2.24] - 2026-10-29

### Fixed

- Resolve the reviewed PEPEUSDT alias to Bybit's 1000PEPEUSDT contract only
  after an explicit invalid-symbol response (#257). Valid exact contracts take
  precedence. Source prices remain unchanged; incompatible quote units are
  rejected rather than multiplied or divided. Raw extraction evidence is retained.
- Resolve lifecycle aliases only against the same source's contract history.
  Canonical contract identity participates in duplicate and ownership checks;
  partial failures retain duplicate restrictions, original IDs and retry deadlines.
  Recovered alias candidates recheck quote units before planning.

### Validation and rollback

- Synthetic exchange-boundary and real-SQLite regressions cover LONG/SHORT
  planning, quote-unit rejection, exact precedence, auth/transport failures,
  lifecycle scope and partial-batch restart/expiry behavior without real orders.
- No schema migration, historical replay, or trading-policy change. Roll back
  to the previous image if needed; newly supported aliases will again be rejected.
- #248 remains open pending a naturally closed new-risk Demo trade at the
  requested magnitude. Improved post coverage does not establish profitability.

## [2.2.23] - 2026-10-29

### Fixed

- Separate LONG participation reports by frozen assignment mechanism within each
  sizing cohort (#243/#244). Deterministic momentum decisions, hash fallback,
  historical untagged records and unknown provenance remain separately visible.
  Gated, mixed and unknown samples cannot satisfy randomized review gates;
  historical untagged readiness is descriptive and claims no causal/ITT effect.
- Dashboard participation labels follow the recorded assignment mechanism rather
  than labeling every historical cohort randomized. Current settings and the
  independently refreshed whole-trade feed remain separate.

### Validation and rollback

- Regression fixtures exercise all-gated and mixed cohorts, frozen metadata,
  unknown provenance and unresolved takes through CLI/dashboard report boundaries.
  Verify rendered labels with a synthetic deterministic cohort.
- No schema migration or trading-policy change. Roll back to the prior image and
  Pages source if needed; retain the public feed's own evidence timestamp.
- #248 remains open for a naturally closed Demo trade at the requested magnitude.
  Cohort labels and configured sizing are not proof of improved expectancy.

## [2.2.22] - 2026-10-29

### Added

- Publish a bounded, public Demo whole-trade feed and readable dashboard table
  (#253, child of #248): quantity, signed net PnL/R, UTC close time and frozen risk.
  Current sizing/coverage settings appear separately. Missing, stale and failed
  refreshes are explicit; aggregate cards keep their separate snapshot date.
- Local read-only 15-minute refresh through the existing Pages workflow, with
  strict public-field validation and public-site verification. Bybit credentials
  remain local; GitHub credentials come from the host's credential manager.
  PnL-only forensic exports can omit market candles without changing research defaults.

### Validation and rollback

- Exercise actual partial-close reconciliation, public-field privacy, unknown
  values, bounded rows, Pages installation and PnL-only private history requests.
  Check the phone layout and empty/stale/unavailable views with synthetic fixtures.
- No database migration or trading-policy change. Stop the local feed publisher
  and redeploy the previous image to roll back. The publisher needs an explicitly
  installed host process and existing repository Actions write access.
- #248 remains open until a natural new-risk Demo close demonstrates the requested
  magnitude. A published table or a larger configured budget is not that evidence;
  neither establishes improved expectancy or mainnet readiness.

## [2.2.21] - 2026-10-29

Operator-directed wallet-utilization checkpoint (#248, standing top priority).

### Added

- Observe exchange executions for already enrolled zone-order identities
  (#245/#249), using existing ledger ownership and execution-ID checks.
  Recording is observational groundwork: it does not enable independent-zone
  trading or certify complete historical evidence. Existing entry, exit and
  position-protection behavior is unchanged.

### Changed

- Raise the stored execution policy's base per-trade risk from 1% to 3% of
  the live wallet (~212 USDT stop budget per trade) so the strategy's verified
  good win/loss shape operates at real magnitude; outcomes in the ~±100 USDT
  ballpark are the operator's goal and a rare full stop-out (~-212 USDT) is
  explicitly accepted. Exits, entries, stops, profiles and every strategy
  behavior are untouched.
- Disable the momentum gate and the participation skip fraction so every
  actionable post trades; the operator confirmed the signal source and
  per-trade security are good. The momentum-gate cohort evidence (#223/#183)
  remains recorded and inspectable; plans from this boundary form an
  unlabeled-gate cohort and pooled comparisons across it are descriptive only.
- Portfolio cap stays 4000 USDT (~18 concurrent full-risk plans); Compose
  carries the two flag changes and the policy row is updated through the
  existing `scripts/set_execution_policy.py` with a stopped-writer state
  backup taken first.

### Upgrade, validation and limitations

- Schema and strategy behavior unchanged. Rollback restores the policy to 1%
  and the flags (gate on, skip 0.50) for future plans; saved plans and
  positions are never rewritten. Take a stopped-writer, restore-verified state
  backup before applying the policy update; verify the deployed revision,
  the printed policy (risk_pct=3), the two Compose flags, Demo
  authentication, supervision health and native stop protection after
  deployment. Watch trade-level outcomes via Bybit Closed-PnL and record the
  first |100 USDT|-scale close in #248.
- This release delivers the requested settings checkpoint, not the complete
  profit-magnitude objective. The public trade-level dashboard and an observed
  outcome at the requested scale remain acceptance items in #248. Larger sizing
  alone does not establish improved expectancy or guarantee that outcome.

## [2.2.20] - 2026-10-28

Durable zone-enrollment checkpoint.

### Added

- Every successfully executed Strategy V2 plan now enrolls into the allocation
  ledger (#240, part of #123): its plan+intent are frozen as a zone in the
  deterministic per-symbol/side group, and each submitted ENTRY identity
  (broker order id, `ccb-v2-…-eN` link, planned quantity, role ENTRY) is
  bound exactly once. The ledger remains evidence only — it grants no
  mutation permission — and a ledger write failure never fails a filled
  execution; it is logged loudly for supervision to reconcile.

### Upgrade, validation and limitations

- Schema 14 unchanged; coordinator-level tests cover enrollment identities,
  ledger-failure semantics, execution-failure no-writes and group
  determinism; the full suite passes. V1 plans, position actions, intake
  guards and activation gates are untouched: multi-zone intake, the group
  protection writer and activation evidence remain open on #123.

## [2.2.19] - 2026-10-28

Deterministic exchange-rejection terminal planning checkpoint.

### Fixed

- Stop the live retry loop on deterministically rejected exchange instrument
  reads (#237): a supported post for a symbol Bybit explicitly rejects with a
  retCode response (e.g. `10001 symbol invalid` for the PEPE/1000SHIB
  contracts, retried 71 times over two hours on the deployed runtime) now
  completes the source once with a durable planning rejection instead of
  entering the retry path. Transport failures (`Bybit HTTP request failed`,
  timeouts) keep the retryable behavior. A typed `is_exchange_rejection`
  predicate mirrors the existing client message-code parsing, and the
  coverage disposition exposes `exchange_rejected_request` so the class is
  visible in source-coverage reports. No symbol aliasing is introduced:
  mapping a source's symbol onto a different contract stays a separate
  policy decision.

### Upgrade, validation and limitations

- Schema 14 and all trading settings are unchanged; the change affects only
  planning failure classification. Rollback is a redeploy of the previous
  image. After deployment the looped source completes with its recorded
  rejection and the channel's intake coverage no longer reports incomplete.

## [2.2.18] - 2026-10-28

### Added

- Opt-in `report_source_coverage.py --telegram-history` audit (#235) compares
  supported visible posts with the durable source ledger and current-attempt
  decisions. Unlike the default stored-only report, it detects canonical posts
  that were never stored. It uses an isolated session clone with updates disabled,
  metadata reads only, and a read-only SQLite snapshot. No media downloads,
  model/exchange calls, message sends or production initialization occur.
- Publication-window and whole-album settling rules, bounded per-channel history
  scans, explicit truncation/channel errors and incomplete-intake diagnostics.
  Interrupted boundary albums cannot become false missing canonical IDs.
  Recorded pre-model rejection remains distinct from unexplained missing evidence.

### Validation, upgrade and limitations

- Integration checks cover real SQLite preservation, canonical albums, window and
  scan boundaries, current-attempt decisions, channel isolation and CLI session
  cleanup. Exit status is 0 for complete visible intake, 2 for gaps/incomplete
  history and 1 for audit failure. Default offline reporting is unchanged.
- Schema 14 and trading settings remain unchanged. Use a stopped-writer,
  restore-verified backup; rollback to 2.2.17 retains the current state and schema.
- Coverage applies only to supported visible history in the requested window,
  after its settling margin. It does not certify deleted/inaccessible posts,
  edits, model classification or execution success. Missing posts are reported,
  never automatically replayed. Historical outage recovery policy and #123's
  unfinished independent-zone execution are unchanged.

## [2.2.17] - 2026-10-28

### Added

- Independent native-stop coverage evidence in allocation snapshots (#233).
  Reconstruct each ENTRY parent's remaining quantity from exact executions and
  check its own live market stop at the frozen zone trigger. Matching account or
  zone totals cannot hide missing, changed, duplicate, oversized or stale stops.
  Unallocated reductions and whole-position TP/SL/trailing controls fail coverage.
- Coverage diagnostics remain separate from quantity reconciliation. No account
  means unobserved coverage; a flat group is not proof of a live protection path.
  Integration checks exercise both sides, restart, unequal zones, multiple
  parents, partial exits, replacement stops and preserved read-only state.

### Upgrade and limitations

- Schema 14 and trading settings are unchanged. Take a stopped-writer,
  restore-verified backup; binary rollback to 2.2.16 retains schema compatibility
  and current state. Do not restore older state over newer exchange activity.
- This is snapshot evidence for the unfinished group coordinator, not permission
  to mutate the exchange. Overall protection remains unverified and trading
  disabled for allocations. Dynamic stop revisions, complete native exit
  lifecycle, fresh account reads under the mutation lock, enrollment and source
  actions remain required by #123. No new live probe was needed or submitted.

## [2.2.16] - 2026-10-28

### Added

- Native zone-child bindings (#231) retain an exact broker order ID and a
  same-zone confirmed ENTRY parent link, without inventing a client order link.
  TP/SL siblings can coexist; unknown parents, conflicting ownership and
  oversized children fail closed. Account snapshots check parent identity and
  native TP/SL role alongside existing quantity/execution conservation checks.

### Upgrade, validation and limitations

- Schema 14 makes absent native client links NULL while retaining uniqueness
  for submitted client links. The transactional upgrade preserves legacy order
  payloads and executions and checks foreign-key integrity before commit.
  Integration coverage includes both sides, restart, populated schema-13
  migration and failed-upgrade rollback/retry.
- Stop the writer and take a restore-verified current-state backup before
  upgrading. Older binaries reject schema 14. Prefer a forward fix; do not
  restore an old backup over newer exchange/ledger activity. See the explicit
  conditional downgrade requirements in `docs/independent-zone-execution.md`.
- Allocation evidence remains inactive: protection is unverified and trading
  disabled. Full enrollment, coordinated native protection, source actions and
  restart-safe group execution in #123 remain unfinished. Risk settings and
  signal ingestion behavior are unchanged; no new native trade probe is needed.

## [2.2.15] - 2026-10-28

### Added

- Opt-in `--zone-amendment-pair-cancellation` rehearsal (#229): verify zone A's
  one-sided stop amendment, cancel its exact TP once, then observe whether the
  original peer SL retires or remains live after a two-second settling window.
  Verify B's original pair and unchanged net quantity before cleanup. Both
  decisive outcomes are observations, never multi-zone activation proof.
- Reuse exact ownership journals, lost-acknowledgement read reconciliation,
  bounded Demo notional and flat-cleanup gates. The runner requires distinct
  LONG/SHORT receipts for the new phase; old amendment-only receipts cannot
  satisfy it. Foreign evidence, identity mismatch and B drift fail closed.

### Upgrade, validation and limitations

- Probe/runner changes only; schema 13, trading settings and saved plans are
  unchanged. Deploy with a stopped-writer, restore-verified backup and verify
  the exact revision, authentication, supervision and existing native stops.
  Rollback retains current state and settings with the previous image.
- Focused validation exercises the real HTTP adapter and simulated retained/lost
  peer binding, lost acknowledgements without replay, foreign orders, B drift,
  malformed acknowledgements and runner cleanup/receipt gates. Native results
  require a separately coordinated run after deployment and remain pending at
  this code checkpoint. This does not test trigger-fired OCO, partial-fill timing
  or complete independent-zone offline behavior; #123 remains unactivated.

## [2.2.14] - 2026-10-28

Zone amendment evidence checkpoint and trigger-phase scope correction.

### Changed

- Revert the on-demand TP-trigger phase of the zone amendment rehearsal
  (#227): Bybit rejects amendments that cross a partial TP/SL trigger against
  the position side (error 10001), so a native trigger-fired peer
  cancellation cannot be forced deterministically. The deterministic
  one-sided amendment observation (v2.2.13, both sides, preserved identities,
  triggers and B pair) stands as the native evidence; trigger-fired peer
  cancellation, partial-fill timing and immediate-fill-during-installation
  remain documented synthetic/contract limitations for #123's gates.
- Record the amendment observation table, the synthetic late-fill and
  lost-amendment-ack scenarios, and the explicit unobserved list in
  `docs/native-zone-stop-observation.md` (#139).

### Upgrade, validation and limitations

- Probe/docs only; schema 13, settings and service behavior unchanged.
  Rollback is a redeploy of the previous image. #139 closes with this
  evidence; #123 remains gated on the supervisor group-protection path and
  activation evidence.

## [2.2.13] - 2026-10-28

Native one-sided stop amendment observation for independent zones.

### Added

- New opt-in probe mode `--zone-amendment` (#225, part of #139): amends zone
  A's partial stop on the real Bybit Demo adapter and verifies from fresh
  reads that A's child identity and requested trigger hold, zone B's exact
  pair is untouched, and net quantity is unchanged, for both LONG and SHORT
  under the exclusive runner boundary. The orchestration runner refuses to
  resume the app unless both receipts acknowledge the amendment with
  preserved identities and no activation claim.
- Synthetic adapter-level race scenarios pin late-entry-fill allocation
  isolation and lost-amendment-acknowledgement reconciliation (exactly one
  amend request, fresh-read reconciliation, no adoption of unknown
  identities).

### Upgrade, validation and limitations

- Probe and tests only; schema 13, settings and service behavior unchanged.
  Rollback is a redeploy of the previous image. Native amendment receipts are
  recorded in docs after the post-deploy rehearsal; partial-fill timing and
  mainnet behavior remain outside the observed matrix.

## [2.2.12] - 2026-10-28

Momentum-gated Demo LONG participation checkpoint.

### Changed

- Add `DEMO_LONG_MOMENTUM_GATE` (deployed on, #223): the pre-registered #183
  review gate was met (20/20 reconciled takes, 28 skips, zero invalid
  records) and its momentum strata produced an adequate positive result only
  for entries after a direction-aligned positive prior 60-minute return
  (+1.81 USDT and +0.313R per assignment; day-clustered 95% CIs exclude zero
  in both units), while the nonpositive stratum stayed a coin flip. New LONG
  plans are therefore assigned take/skip by that classifier at planning time
  instead of the coin flip; unavailable candle evidence falls back to the
  stable bucket and records the stratum as unavailable, so no post is lost
  to a data hiccup.
- Persist the stratum (`long_participation_momentum`:
  positive/nonpositive/unavailable) on the plan's strategy metadata and in
  forensic assignment records; schema 13 is unchanged. `DEMO_LONG_
  PARTICIPATION_SKIP_FRACTION` keeps its meaning for the fallback path;
  turning the gate off restores the randomized experiment exactly.
- The export's lineage also records every plan-order execution
  (`plan_executions`), and Closed-PnL attribution prefers the closing
  order's identity with the symbol+time window as fallback (#221), so
  rehearsal orders can no longer over-close a position's attribution.

### Upgrade, validation and limitations

- No sizing, cap, exit, approval or SHORT behavior changes; the gate affects
  only newly planned Demo LONGs and is reversible per cohort. The strata
  evidence is observational from a small sample; review the gated cohort as
  it grows and keep cohort boundaries separate in every report. Rollback:
  turn the gate off; existing plans and positions are unchanged.

## [2.2.11] - 2026-10-28

Portfolio cap-cohort labeling checkpoint.

### Changed

- Label planning-side records with the portfolio cap epoch at the recorded
  2.2.10 deployment boundary (`2026-10-27T20:11:55+00:00`, #213): the
  participation CLI prints the boundary and per-cohort epoch counts, and the
  dashboard exposes `cap_epoch_boundary`, `cap_epoch_summary` and per-row
  `cap_epoch` labels beside the existing `sizing_cohorts`. Boundary equality
  belongs to the new epoch; timezone-free, malformed or missing creation
  evidence stays visible as `unknown` and cannot establish readiness. No
  readiness gate, trading behavior or setting is changed.

### Upgrade, validation and limitations

- Schema 13 is unchanged; reporting code only, so rollback is a redeploy of
  the previous image. The full unit suite passed (1170 tests, 87.09%
  coverage). The post-change read-only checkpoint in #207/#213 found zero
  historically-skipped posts that would have fit the new cap (161 production
  decisions scanned; 28 participation skips, 3 manual skips, no capacity
  skips), so the 4000-USDT cap is preventive capacity for the 1.0x era.
- An erroneous v2.2.11 tag was pushed briefly before this release-prep
  revision existed and was deleted unpublished; the release workflow's
  tag/version/changelog assertion refused it, and the published tag now
  targets this exact revision.

## [2.2.10] - 2026-10-27

Operator-directed portfolio stop-risk cap checkpoint.

### Changed

- Raise the Compose portfolio stop-risk cap from 400 to 4000 USDT for newly
  planned positions (#207). Combined with the restored 1.0x LONG sizing this
  admits about 56 fully-risked concurrent plans at the current wallet (the
  coverage tier of the earlier 0.10x era) instead of about 5, so same-day
  multi-post signals are no longer bound to zero planner capacity by the
  per-symbol equivalents of the previous cap. Sizing, base risk, exits,
  leverage and participation randomization are unchanged; existing plans and
  positions keep their frozen policies and never gain capacity retroactively.
- Document the cap-cohort boundary and its tail risk: the worst-case
  simultaneous stop-loss is now approximately 57% of the wallet if many plans
  hit their stops together; the operator explicitly accepted this tenfold
  exposure scaling. Larger caps scale losses and profits together and do not
  establish expectancy.

### Upgrade, validation and limitations

- Schema 13 is unchanged. A stopped-writer, restore-verified state backup was
  taken before the change; the archive SHA-256 stays in the private backups
  receipt. Cohort reporting that labels pre/post-cap plans in the CLI and
  dashboard, plus a read-only checkpoint counting previously skipped posts
  that fit the new cap, follows as a separate issue (#207 stays open until
  that labeling ships). Rollback restores 400 for future plans and does not
  rewrite saved plans or close positions.
- Deployment checks: verify the exact deployed revision and env cap value,
  native stop protection, supervision connectivity, and that the operator
  approved the value with its slot and tail arithmetic recorded in #207.

## [2.2.9] - 2026-10-27

Probe verification checkpoint for the native process-loss rehearsal.

### Fixed

- Give the run-owned Demo rehearsal's ownership verification bounded
  read-only convergence while an execution-versus-position read round can
  still lag (#198). A fresh-process recovery may now repeat the
  conservation read at most twice more instead of failing immediately on
  read-visibility delay, while identity, foreign-execution, unexpected
  side and already-submitted cleanup guards keep failing closed on the
  first attempt and the conservation equality stays exact. Delayed and
  exhausted rounds are recorded only in the private rehearsal journal.
  Production planning, sizing and service behavior are unchanged.

### Upgrade, validation and limitations

- Schema 13 is unchanged; scripts and tests only, so there is no state
  migration and redeploying the previous image fully rolls back. Validate
  the probe subprocess/adapter harness, full unit suite and the exact
  deployed revision, Demo authentication, supervision health and native
  stop protection before and after deployment.
- The bounded native LONG and SHORT process-loss rehearsal itself remains
  pending: redeploy this image, then rerun the exclusive rehearsal under
  current settings. The convergence-check CI evidence (#190 acceptance)
  covers adapter behavior, not a new native observation; #190, #139 and
  #123 stay open.

## [2.2.8] - 2026-10-27

### Fixed

- Separate exit-profile and randomized LONG-exit arm results across the known
  2.2.6 sizing cutover (#203). Reconciled positions preserve saved plan creation
  time, so historical plans filled later cannot supply the new cohort's sample
  target or dollar averages. Cap-limited new plans remain in their creation
  cohort. Unknown creation evidence stays visible and cannot establish review
  readiness; unaffected SHORTs retain separate results and never complete a
  LONG sample. CLI and dashboard exports label and retain each cohort.

### Upgrade, validation and limitations

- Trading settings, frozen plans and schema 13 are unchanged. Take a stopped,
  restore-verified state backup; verify the exact deployment, authenticated Demo
  access, supervision and native stops. Rollback retains current state and the
  2.2.6 sizing configuration; no data migration is required.
- Regression coverage exercises real forensic lineage, executions/closed-PnL
  and SQLite consumers for both randomized exit arms, 19/20 new completions,
  old-plan late fills, cap-limited plans, missing/timezone-free timestamps and
  preserved SHORT costs/results. Existing forensic and report checks remain.
- This provides honest descriptive exit cohorts, not proof of profitability
  or a powered experiment. Review #183, independent-zone execution #123/#139
  and native process-loss proof #190/#198 remain unfinished.

## [2.2.7] - 2026-10-27

### Fixed

- Separate LONG participation review at the verified 2.2.6 sizing cutover
  (#201). Historical completed trades no longer supply the new cohort's sample
  target or inflate its dollar averages. CLI output labels each cohort; the
  dashboard shows current-cohort readiness and retains historical results in
  its export. Unknown creation times remain visible and never establish ready
  status. Correct the dashboard's outdated fixed 0.10x LONG-sizing caption.

### Upgrade, validation and limitations

- Trading settings, saved policies and schema 13 are unchanged. Use a stopped,
  restore-verified state backup and verify the exact deployed revision,
  authentication, supervision and native stops. Rollback retains current state;
  do not overwrite newer state or revert the 2.2.6 sizing configuration.
- Regression coverage uses real forensic reconciliation and SQLite consumers
  across historical/current samples, the exact timezone-normalized cutover,
  invalid/missing times and existing unresolved/invalid assignment gates.
- This fixes participation review only. Other cross-cohort historical research
  remains descriptive; profitability review (#183), independent-zone execution
  (#123/#139) and native process-loss proof (#190/#198) remain unfinished.

## [2.2.6] - 2026-10-27

Operator-directed Bybit Demo LONG sizing checkpoint.

### Changed

- Restore the Compose LONG risk multiplier from 0.10 to 1.0 for new plans
  (#199). With the current 1% base risk, nominal LONG stop risk increases from
  0.1% to 1% of live wallet balance before portfolio capacity and rounding.
  SHORT sizing, the 400-USDT portfolio cap, leverage, exits and randomized
  participation/control settings are unchanged. Existing plans and positions
  keep their frozen policies.
- Document the sizing-cohort boundary and prohibit interpreting pooled dollar
  outcomes across the cutover as an unchanged-risk experiment. Larger sizing
  scales losses too and does not establish positive expectancy or guarantee
  tenfold realized profits.

### Upgrade, validation and limitations

- Schema 13 is unchanged. Preserve a private pre-cutover forensic snapshot and
  stopped-writer, restore-verified state backup. Validate focused planning,
  service and portfolio-cap tests, normal CI and resolved Compose settings;
  verify the exact deployed revision, Demo authentication and native stops.
- Roll back the image/configuration with current state retained and restore
  LONG multiplier 0.10 for future plans. Do not overwrite newer state or alter
  existing plans merely to reverse this sizing change.
- Mainnet, profitability review (#183), complete independent-zone execution
  (#123/#139) and native process-loss proof (#190/#198) remain unfinished.

## [2.2.5] - 2026-10-27

Bybit Demo exchange-outage diagnosis and safe-read resilience checkpoint.

### Fixed

- Report sanitized timeout, connection, HTTP-status, invalid-response and Bybit
  rejection causes in startup and running supervision notices. IP-allowlist
  instructions and public-IP lookup now require a confirmed 10010 rejection;
  unrelated read failures no longer suggest an IP change (#192/#194).
- Retry a timed-out private GET once within the existing two-attempt budget,
  regenerating signed headers. Persistent failures still escalate; application
  rejections such as 10010 are not treated as timeouts. A POST timeout remains
  one submission because its outcome can be uncertain. Explicit timestamp
  rejection retains its clock-resynchronization behavior (#193/#195).

### Added

- An opt-in bounded process-loss probe: a worker flushes exact entry/protection
  evidence before abrupt exit, and a separate context reloads its known journal
  for verification and attributable cleanup without entry replay (#190/#191).
  Synthetic subprocess/adapter tests pass; native LONG/SHORT process-loss
  observations remain pending. This is not a generic historical-journal recovery
  command or an independent-zone offline-safety guarantee.

### Upgrade, validation and limitations

- Schema 13 and risk/trading settings remain unchanged. Take a stopped-writer,
  restore-verified state backup before deployment. Retain the previous image for
  rollback with current state preserved; reconcile exchange changes and never
  lower SQLite `user_version`.
- Focused validation includes 76 combined alert/read integration cases and 91
  probe/runner/subprocess cases, plus normal CI, type, lint and container checks.
  Candidate images are built from canonical committed archives and source hashes
  are checked. Verify the exact release revision, authenticated Bybit reads,
  Telegram startup, supervision and native stops before tagging.
- One timed-out private read can incur one additional existing 10-second attempt;
  health thresholds and mutation retries are unchanged. Clock synchronization
  retains its existing separate budget. No live fault injection or test Telegram
  messages were used to validate the outage fixes.
- Multi-zone activation remains disabled under #123/#139. Native #190 recovery
  proof, coverage policy #179 and profitability review #183 remain unfinished.
  This release certifies neither mainnet operation nor a profitable edge.

## [2.2.4] - 2026-10-27

Demo probe and profitability-review reporting checkpoint above `v2.2.3`.

### Fixed

- Require at least 20 fully reconciled take positions before LONG participation
  reports signal readiness, in addition to adequate per-arm assignments, valid
  records and no unresolved takes. Failed-before-fill assignments remain zero
  outcomes for intention-to-treat accounting. Expose the completed-position
  target in analyzer output and dashboard data (#185/#186).

### Added

- An opt-in, run-owned paired TP cancellation probe with exact private identity
  journals, independent cleanup verification and required LONG/SHORT receipts
  (#184). Both native cases observed A's TP and SL deactivate after its exact TP
  was canceled, while B's original pair, triggers and quantity were preserved.
  Terminal cleanup and app resume were verified (#168/#187).

### Upgrade, validation and limitations

- Schema 13 and trading settings remain unchanged. Take a stopped-writer,
  restore-verified state backup before deployment. Rollback uses the retained
  `v2.2.3` image with current state preserved; reconcile exchange changes and
  never lower SQLite `user_version`.
- Local validation: 61 probe/runner/adapter cases and 28 reporting/integration
  cases, plus CI, lint, type and container checks. Native LONG/SHORT observations
  and private journals were independently checked on the unchanged trading
  behavior. The release revision must pass exact-source, authentication,
  supervision, Telegram startup and native-stop verification before tagging.
- This does not prove exit-to-retirement races, late/partial fills, amendments,
  restart or offline allocation safety. Multi-zone execution remains disabled
  under #123/#139. Coverage policy #179 and profitability review #183 remain
  open; the latest preflight has only 19 take assignments and 27 skips. No risk
  policy changes, mainnet certification or profitable edge are claimed.

## [2.2.3] - 2026-10-27

Coverage-reporting and Demo-probe safety patch on top of `v2.2.2`. This remains
a Bybit Demo release; it does not certify mainnet operation or profitable edge,
and it does not enable multi-zone execution.

### Upgrade and validation

- No SQLite schema change is introduced; schema 13 remains current. Stop the app
  and take a complete restore-verified state backup before the version-only
  deployment. Rollback is an image rollback to `v2.2.2`; preserve the current
  database, reconcile exchange changes and never lower `user_version`.
- CI, CodeQL, container scanning, package build/install checks and the release
  tag/version/changelog gate protect this release. Exact merged-revision and
  local Bybit Demo/Telegram deployment evidence is recorded on the release issue.
- Multi-zone execution remains unfinished under #123. Native quantity-scoped
  protection and run-owned cleanup remain required; #168 still needs its probe
  implementation and a separate exclusive rehearsal. Mainnet remains
  unimplemented and is not enabled.

### Changed

- Add aggregate terminal planning-rejection reasons to the read-only source
  coverage report: minimum size or fixed-exit capacity, price geometry,
  portfolio stop-risk cap and other. The field counts rejection occurrences, not
  unique sources, and retains no raw source text. No risk, sizing, exit or
  execution behavior changes; the coverage-policy decision remains open under
  #179.
- Require run ownership before the isolated Demo probe cancels orders. Cleanup
  now refuses an unowned symbol instead of adopting or mutating it. This affects
  probe tooling only; the live strategy path is unchanged. The guard is delivered
  by #174.

## [2.2.2] - 2026-10-27

Protection-maintenance patch on top of `v2.2.1`. This remains a Bybit Demo
release; it does not certify mainnet operation or profitable edge, and it does
not enable multi-zone execution.

### Upgrade and validation

- No SQLite schema change is introduced; schema 13 remains current. Stop the app
  and take a complete restore-verified state backup before the version-only
  deployment. Rollback is an image rollback to `v2.2.1`; preserve the current
  database, reconcile exchange changes and never lower `user_version`.
- CI, CodeQL, container scanning, package build/install checks and the release
  tag/version/changelog gate protect this release. Exact merged-revision and
  local Bybit Demo/Telegram deployment evidence is recorded on the release issue.
- Multi-zone execution remains unfinished under #123. Native quantity-scoped
  protection and run-owned cleanup remain required; #169 must be verified before
  any #168 rehearsal. Mainnet remains unimplemented and is not enabled.

### Fixed

- Re-verify the full-position stop after each partial-stop cancellation during
  the handoff from attached partial stops to a strategy-owned full stop. If the
  full stop disappeared at the cancellation boundary, reinstall and confirm it
  before cancelling another partial stop; a conflicting non-null stop fails
  closed without being overwritten. The fix closes #175 and preserves the #139
  requirement that one zone cannot leave another allocation unprotected.

## [2.2.1] - 2026-10-27

Coverage, extraction and repository-hardening maintenance on top of `v2.1.1`.
This remains a Bybit Demo release; it does not certify mainnet operation or
profitable edge, and it does not enable multi-zone execution.

### Upgrade and validation

- No SQLite schema change is introduced; schema 13 remains current. Stop the app
  and take a complete restore-verified state backup before the version-only
  deployment. Rollback is an image rollback to `v2.1.1`; preserve the current
  database, reconcile exchange changes and never lower `user_version`.
- CI, CodeQL, container scanning, package build/install checks and the release
  tag/version/changelog gate protect this release. Exact merged-revision and
  local Bybit Demo/Telegram deployment evidence is recorded on the release issue.
- Multi-zone execution remains unfinished under #123. Native quantity-scoped
  protection and run-owned cleanup remain required; #169 must be verified before
  any #168 rehearsal. Mainnet remains unimplemented and is not enabled.

### Fixed

- Apply the configured entry-order TTL to legacy Strategy V2 plans that were
  stored before the TTL field existed. A legacy ladder can no longer own a
  symbol indefinitely or leave unfilled entry orders after its expiry window.
  A configured value of zero still disables automatic entry expiry.
- Reject promotional and performance-recap cards as live OPEN evidence. ROI
  tables, QR/referral branding and historical recap panels must not become
  entries; an empty caption with only promotional images is non-actionable.

### Changed

- Run the test suite in parallel by default with `pytest-xdist`, keep the
  coverage gate, document the `-n 0` serial escape hatch and raise the state
  backup subprocess timeout from 15 to 60 seconds.
- Report required container checks on every pull request, including changes that
  do not touch container paths.

### Documentation

- Record the 26-group opportunity-coverage audit, the paired promotional/live
  screenshot image replay and the post-deploy coverage window without committing
  private images, source captions or exchange identities.

## [2.1.1] - 2026-10-26

Security maintenance, source-completion and opportunity-coverage fixes on top of
`v2.1.0`. This remains a Bybit Demo release; it does not certify mainnet
operation or profitable edge.

### Upgrade and validation

- SQLite schema 13 adds the planning recovery queue. Stop the app and take a
  complete restore-verified state backup before upgrading from schema 12 or
  earlier supported versions. Older images reject schema 13; rollback requires
  exchange reconciliation and a matching pre-upgrade database. Never lower
  `user_version` to force compatibility. Historical missing candidates are not
  automatically replayed.
- CI, CodeQL, container scanning, package build/install checks and the release
  tag/version/changelog gate protect this release. Exact merged-revision and local
  Bybit Demo/Telegram deployment evidence is recorded on the linked release issue.
- Multi-zone execution remains unfinished under #123. The allocation ledger does
  not enable multiple competing protection writers; native quantity-scoped
  protection and the complete restart/source-action path remain required.
  Mainnet remains unimplemented and is not enabled by this release.

### Fixed

- Retain transiently failed OPEN candidates when other signals in the same post
  succeed. Recover their original IDs without repeating extraction or successful
  siblings, preserving approval restrictions and the original expiry deadline.
  Pending recovery is reported as unresolved in source coverage.
- Complete Telegram posts with explicit execution-planning rejections as audited
  policy decisions instead of retrying their media and extraction indefinitely.
  Preserve rejection reasons and retry transient processing failures; this does
  not relax sizing rules or require a Telegram session reset.

### Security

- Install current Debian OpenSSL security updates in the runtime image before
  application dependencies are installed.
- Update `pydantic-settings` to 2.16.1, development `ruff` to 0.17.2 and pinned
  workflow actions (setup-uv 10.3.2, CodeQL action 4.39.1).
- Keep Docker Dependabot on the supported Python 3.12 line. A newer interpreter
  requires its own runtime migration, packaging validation and rollback plan;
  digest refreshes of the pinned 3.12 tag continue automatically.

## [2.1.0] - 2026-10-26

Refactoring, correctness and repository hardening since `v2.0.0`.

### Upgrade and validation

- SQLite schema 12 supports upgrades from schemas 4–11. Stop the old app and take
  a complete restore-verified state backup before upgrading. Old images reject
  schema 12. Rollback requires reconciling exchange changes and restoring a
  matching pre-upgrade database; do not lower `user_version` or overwrite current
  exchange protection. Existing trading/source rows are preserved, and missing
  historical decisions or zone ownership are not invented.
- Runtime validation is local Bybit Demo with authenticated Telegram/Bybit access,
  fresh supervision and pending-entry stop checks. CI, container scanning, CodeQL
  and package-build checks gate release preparation. Exact release/deployment
  evidence is recorded in the linked release workflow issue #137.
- Multi-zone execution remains unfinished under #123. Each POI must receive its
  own full budget; the ledger foundation does not enable multiple competing
  protection writers. Native quantity-scoped protection, portfolio admission and
  complete restart/source-action handling remain required before activation.

### Added

- Durable per-attempt original model extraction, normalization, source/context and
  planning evidence, independent of the six-hour evaluation cache. Coverage audits
  prefer original production decisions; a SQLite-only report distinguishes known
  no-op, filtered, rejected, routed, executed and unresolved outcomes from unknown
  historical evidence. Stale attempts cannot replace current evidence.
- Independent-zone allocation ledger with immutable frozen plans/source lineage,
  confirmed order bindings and idempotent execution IDs. Separate zone quantities
  survive restart and out-of-order delivery; reports expose conflicts, overclosure,
  overfill, changed/unattributed orders and net-position mismatches. Quantity
  reconciliation never implies verified protection or permission to trade.
- Labeled MRs, delivery milestones and immutable version-tag/release guidance,
  reusing the existing tag-triggered verification and package publication workflow.

- Treat malformed Bybit JSON/envelopes as exchange failures so startup gates,
  supervision backoff and operator escalation remain active. Reject missing
  response codes and invalid successful result shapes without echoing bodies
  or automatically replaying mutation requests.

- Require a fresh account read before any supervisor transition to CLOSED,
  including expiry and protection handoffs. Keep positions that appear during
  mixed position/order snapshots under active supervision.

- Cancel a strategy's remaining entries after its observed position disappears,
  including during CLOSE. Confirm account state before recording CLOSED so an
  entry fill during cancellation remains under supervision; leave foreign orders
  and protective exits untouched.
- Include the current runtime public IP in Bybit IP-binding outage alerts. Look
  it up only during throttled delivery, with a five-second timeout and public-IP
  validation; lookup failure remains explicit and does not suppress the alert.

- Reject backup restore drills whose Telegram session has no authorization record,
  binary key or valid endpoint, even when SQLite integrity passes. Report key
  presence without exposing it; offline checks do not prove server-side authorization.

- Pause Strategy V2 before exchange mutations when an attached stop with a verified
  entry parent changes trigger price or side; persist human-review alerts across
  restarts while leaving existing orders and protection untouched.

- Include actionable Bybit `10010` IP-allowlist instructions and the API-management
  link in startup and runtime supervision outage notices, retaining existing
  escalation limits and secret-safe error handling.

- Flag durable paused or uncertain records in authorized `/status`, linking to
  `/attention` for reasons. Distinguish storage failures from an empty review queue;
  stored status does not claim live exchange protection.

- Block container CI on fixable high or critical runtime vulnerabilities after
  uploading the existing vulnerability report and SBOM for diagnosis.

- Report post-startup supervision failures through authorized `/status` and
  rate-limited human alerts, independently of position protection. Back off missing
  shared exchange account snapshots without slowing healthy-symbol reconciliation;
  retry failed notifications and announce recovery after a successful pass.
- Keep operator polling and authorized `/status` available during Bybit startup
  outages. Block trading callbacks and ingestion until existing reconciliation
  gates complete; retry exchange failures with capped backoff and rate-limited
  human escalation, preserving AUTO recovery order and cancellation cleanup.
- Ambiguous strategy owners now retain a durable pause reason and queue the
  existing retryable Telegram alert for every affected strategy, without exchange
  requests or automatic ownership selection. Quarantine all conflicting owners
  atomically so a failed write cannot leave one owner eligible for automation.
- Telegram reader recovery keeps operator control and position protection alive
  during reader outages, retries connections with bounded frequency and escalates
  to human review. Add authorized `/source`, private QR `/source_login`, pinned
  account checks and a stopped-app local authorization helper for 2FA. Include
  the optional account identity record in verified state backups.
- Read-only Compose supervision-progress healthcheck, based on successful passes
  rather than a running process. Expire progress after five minutes, clear stale
  markers under state ownership, and keep observability write failures separate
  from protection work. This does not auto-restart or certify other components.
- Authorized `/attention [page]` shows durable paused strategies and uncertain
  entry/action executions with IDs and reasons. The read-only paginated view
  retains acknowledged and legacy pauses and distinguishes storage failures from
  an empty list, without querying or changing the exchange.
- Offline state-backup CLI snapshots the application database and Telegram session
  under the existing owner lock, includes committed WAL data, refuses overwrites
  and verifies an isolated restore before success. Add a Docker backup/restore
  drill with saved hashes, image identity and explicit cutover checks.

### Changed

- Research tools share packaged forensic reconciliation, statistics and frozen
  cohort definitions instead of importing the analyzer CLI. Keep analyzer imports
  compatible and preserve report output, accounting formulas and random seeds.
- Extracted strategy supervision into phased builders and split execution,
  coordinator, approval presentation and extraction into named modules rather
  than growing top-level files.
- Gate application complexity at `C901=10`; operator scripts and tests stay
  exempt as linear run-once procedures.
- Planning derives scale-in legs separately from rebase logic, and exits plan
  separately from placement.
- Default Compose runs packaged image code with only its state volume. Select
  `docker-compose.dev.yaml` explicitly for developer source mounts; pin a release
  image through `CCB_IMAGE` without a private Compose override.

### Fixed

- Entry failure now commits its intent result and strategy disposition together.
  A failed write retains the execution claim for restart warning/quarantine,
  while interrupted OPEN recovery still protects verified primary fills.
- Validate every position-action exposure page with the shared Bybit paginator.
  Reject missing lists and repeated cursors before cancellation or close submission,
  and include later position pages instead of silently dropping their exposure.

- Reject missing or malformed exchange list pages and cyclic page cursors before
  account safety checks can mistake unavailable exposure for an empty account.
- Remove closed-P&L history fetches from signal processing and AUTO result
  previews. The existing periodic worker owns history sync; previews use the
  stored ledger with its successful-sync timestamp and explicit mixed-age total.
  Keep persistence, execution and approval delivery independent of history outages.
- Classify failed initial close/reduce reads as preflight failures, keeping the
  existing strategy active when no mutation began. Retain durable uncertainty
  after cancellation or submission begins, including failures on later reads.
- Treat unavailable exchange quantities as errors instead of zero exposure. Reject
  invalid active symbols, position average/mark prices and stops, and inconsistent
  order quantities. Retain explicit zero positions and partial protective stops.
- Share a two-post intake limit across Telegram live, album, catch-up and retry
  paths, before downloading images or claiming source work. Release admission on
  cancellation and preserve existing durable source recovery without adding tasks.
- Prepare and claim queued entries only after acquiring the mutation lock, leaving
  cancelled waiters pending instead of falsely quarantining them on restart.
  Shield the claim through execution and durable outcome as one operation.
- Claim close/reduce/cancel actions only after acquiring the mutation lock, so
  queue time counts toward expiry and cancelling a waiter leaves it pending.
  Shield the claim and execution together once the lock is held.
- Bound raw Telegram image payloads to 20 MiB per post using download streams.
  Reject oversized albums as a whole before model/exchange access, retain all
  captions and a durable terminal reason, and continue with later posts. Keep
  transient download recovery and stop catch-up when rejection cannot be recorded.
- Own SIGTERM at application scope instead of stopping only Telegram polling.
  Stop polling and drain active update handlers before closing clients; preserve
  state ownership through cleanup, including repeated termination and startup.
  Allow a two-minute Compose stop grace period; forced termination still uses
  startup quarantine.
- Fail the application after a terminal Telegram listener disconnect instead of
  leaving ingestion absent while background tasks keep running. Use existing
  TaskGroup cancellation, client cleanup and Compose restart behavior.
- Restrict source distributions to explicit public project paths so local caches
  and environment variants cannot enter release archives from the workspace root.
  Preserve shipped source, scripts, tests and docs; verify real archives with
  synthetic private-file canaries and install the resulting wheel in CI.
- Ignore private `.env` variants in Git and Docker, including nested Docker
  paths, while retaining public `.env.example` templates. Verify the boundary
  with an isolated Git repository and an exported scratch-image build in CI.
- Reserve AUTO outcome notices before execution and retry recorded terminal
  outcomes after Telegram failure/restart, independently of AUTO trading mode.
  Reuse durable delivery claims without a schema change; retries never call Bybit
  or execute work. Preserve immediate rich results; recovered action cards show
  saved status/order references and explicitly omit unavailable fill details.
- Close acquired runtime clients after partial startup failure and continue cleanup
  after a close fails, retaining exception chains and shared-state ownership until
  cleanup finishes. Close Redis's owned pool even when its client close fails.
- Hold shared-state ownership from before SQLite initialization through runtime
  cleanup; reject duplicate app startup without quarantining another owner's work.
- Defer task cancellation through locked exchange execution and durable outcomes;
  drain Bybit workers before releasing locks or closing clients, including repeated
  cancellation. Preserve cancellation propagation and log delayed worker errors.
- Stop the supervisor symbol sweep on raw SQLite write failures, preserving the
  storage error cause instead of continuing exchange mutations on other symbols.
- Persist submitted fixed-exit geometry and pause on owned-exit drift, including
  canceled partially filled orders. Keep genuine fills and omitted small exits
  distinct; retry operator pause alerts durably after Telegram failures/restart.
- Add schema 9 migration for exit snapshots and pause alerts. Installed legacy
  revisions without a snapshot require review after verified protection handoff.
- Defer unavailable Telegram retry posts for 24 hours in durable source state,
  avoiding one-minute refetch/log loops across restarts. Retain the payload,
  retry daily, allow verified live reappearance and preserve active claims.
- Recover durable failed/stale source posts independently of the Telegram history
  cursor, including full image albums after restart. Keep active claims intact.
- Recover all pending AUTO records rather than silently stopping after 100 opens
  and 100 actions. Retried lifecycle instructions retain publication-age limits.
- Cover these workflows with real SQLite and application components; remove
  runtime protocol-identity checks that did not validate behavior.
- Quarantine interrupted intents, position actions and strategy rows atomically
  before the first reconciliation pass, rather than discovering them mid-poll.
- Require verified parent-order links before cancelling attached partial stops,
  and retain active conditional partial stops when Bybit reports zero `leavesQty`.
- Verify full position protection before removing legacy partial stops; verify
  fixed exits and reuse matching orders when installation retries.
- Regress execution-outcome persistence under the shared mutation lock, so an
  outcome is persisted before the lock is released.
- Stop polling Bybit for quarantined strategies, which was pure load against the
  API with no possible outcome.
- Recover skipped signal posts in the Telegram source.

### Security

- Stream selected Dockerfiles into the local lint container with networking
  disabled and no host mounts. Keep ignored credentials and backups inaccessible
  to the linter, and remove the Bash quoting dependency from the Windows hook.

- Pin the official Python 3.12 base separately from uv to pick up Debian security
  fixes and Python 3.12.15. Preserve the existing uv binaries and frozen application
  dependencies; expose a public `os` stage for scanning before source is copied.

- Update development-only virtualenv to 21.14.12 to fix shell activation command
  injection advisories GHSA-5vjq-rrrf-7h2q, GHSA-8rjx-v5ww-45pp,
  GHSA-c947-3pg5-gm8q and GHSA-vxj4-q4g9-crj7. Refresh its python-discovery
  dependency; production runtime dependencies are unchanged.

- Update the pinned certifi bundle to 2026.10.19 for GHSA-6q48-6c7w-9gw2.
  Remove the affected emSign ECC Root CA - G3 while retaining unaffected roots
  and normal TLS certificate/hostname verification.
- A submission that may have reached the exchange is quarantined instead of
  resent, and a restart never retries an ambiguous operation automatically.

## [2.0.0] - 2026-09-22

Strategy V2. The execution model was replaced end to end.

### Added

- Strategy V2 policy, and an execution planner switched to it.
- Supervised Strategy V2 positions with live balance for risk calculations, so
  capital is frozen rather than guessed.
- Durable auto-approval execution core and selectable automatic trade approval.
- Profit protection enforcement, including exit installation on initial fill
  and rejection of undersized exit structures.
- Position-aware signal classification and a default risk-based take-profit
  ladder.
- Selectable LLM provider abstraction with an ordered fallback chain, covering
  OpenRouter and OpenCode Go, with isolated per-provider cooldowns.
- Strategy V2 replay and lifecycle-aware analysis tooling.

### Changed

- Removed legacy v1 policy configuration.
- Confirmed V2 position actions before applying them, and skip unchanged
  protection writes instead of resubmitting them.
- Skip unchanged V2 protection writes; preserve Strategy V2 exit multiples.

## [0.3.1] - 2026-09-15

### Fixed

- Persist account PnL and improve approval summaries.
- Ignore local backups.

## [0.3.0] - 2026-09-14

### Added

- Multiple intents per Telegram post.
- Position lifecycle actions, with explicit evidence required before a reduce,
  close or cancel is treated as actionable.
- Isolated OpenRouter provider cooldowns.

### Fixed

- Reject invalid quote-only symbols.
- Derive reduce percentage from signal evidence.
- Require explicit lifecycle action evidence.
- Harden the signal extraction boundary.

## [0.2.0] - 2026-09-14

### Added

- Versioned SQLite migrations.

### Changed

- Hardened static checks and CI, and consolidated code style and
  documentation.

## [0.1.0] - 2026-09-13

- First tagged release. Telegram signal ingestion, OpenRouter extraction, and
  Bybit Demo execution.

[Unreleased]: https://github.com/Smarandii/cautious-crypto-bro/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/Smarandii/cautious-crypto-bro/compare/v0.3.1...v2.0.0
[0.3.1]: https://github.com/Smarandii/cautious-crypto-bro/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Smarandii/cautious-crypto-bro/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Smarandii/cautious-crypto-bro/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Smarandii/cautious-crypto-bro/releases/tag/v0.1.0
