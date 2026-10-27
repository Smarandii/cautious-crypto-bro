# Changelog

All notable changes to this project are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Validated on Bybit Demo.** These releases do not certify mainnet operation or
live profitability. Descriptive Demo checkpoints are not proof of a profitable
edge; see [STRATEGY.md](STRATEGY.md) for behavior and limitations.

## [Unreleased]

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
