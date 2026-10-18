# Changelog

All notable changes to this project are recorded here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**Bybit Demo only.** No release in this project has ever targeted mainnet, and
none should. There are no published return, drawdown or win-rate figures; see
[STRATEGY.md](STRATEGY.md) for what the code does and [TODO.md](TODO.md) for
what remains unproven.

## [Unreleased]

Refactoring, correctness and repository hardening since `v2.0.0`.

### Added

- Authorized `/attention [page]` shows durable paused strategies and uncertain
  entry/action executions with IDs and reasons. The read-only paginated view
  retains acknowledged and legacy pauses and distinguishes storage failures from
  an empty list, without querying or changing the exchange.
- Offline state-backup CLI snapshots the application database and Telegram session
  under the existing owner lock, includes committed WAL data, refuses overwrites
  and verifies an isolated restore before success. Add a Docker backup/restore
  drill with saved hashes, image identity and explicit cutover checks.

### Changed

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

- Remove closed-P&L history fetches from signal processing and AUTO result
  previews. The existing periodic worker owns history sync; previews use the
  stored ledger with its successful-sync timestamp and explicit mixed-age total.
  Keep persistence, execution and approval delivery independent of history outages.
- Own SIGTERM at application scope instead of stopping only Telegram polling.
  Stop polling and drain active update handlers before closing clients; preserve
  state ownership through cleanup, including repeated termination and startup.
  Allow a two-minute Compose stop grace period; forced termination still uses
  startup quarantine.
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
