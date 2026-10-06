# Security Policy

This project submits orders to an exchange on behalf of a human operator. Treat
every bug report here as potentially money-affecting and report it privately.

## Reporting a vulnerability

Use **GitHub private vulnerability reporting**:

`Security` → `Report a vulnerability` →
<https://github.com/Smarandii/cautious-crypto-bro/security/advisories/new>

Do not open a public issue, discussion, or pull request for an unreported
vulnerability. If private reporting is unavailable to you, open a public issue
containing only the affected version and a request for a private channel — no
details.

Please include:

- affected version or commit (`git rev-parse HEAD`)
- what an attacker controls, and what they gain
- reproduction steps or a replay fixture
- whether real orders, funds, or credentials were exposed

Expect an acknowledgement within 72 hours. Triage, fix, and coordinated
disclosure timing are agreed per report; there is no fixed SLA. Credit is
offered unless you prefer otherwise.

## Scope

In scope:

- anything that can place, modify, or cancel an order without intended approval
- anything that leaks `.env`, Telegram session files, or API credentials
- authentication or authorization gaps on the approval bot, replay scripts, or
  the management endpoints
- untrusted Telegram text or media reaching order placement contrary to the
  documented trust model in [STRATEGY.md](STRATEGY.md)
- dependency or build compromise reaching runtime or image

Out of scope:

- incorrect trading decisions produced by the documented, intended rules
- losses from market movement, bad signals, or exchange behaviour
- the upstream Telegram, Bybit, or LLM provider's own security posture
- findings that require an already-compromised host or a malicious dependency
  author, absent a concrete amplification path

## Threat model in one page

The design assumes four inputs are hostile or unreliable, and constrains each:

| Input | Trust level | Containment |
| --- | --- | --- |
| Telegram post text and images | Untrusted | LLM extracts structure only; code owns sizing, order semantics and risk. `AUTO_APPROVAL_MODE` defaults to `disabled`. Ambiguity becomes `UNCERTAIN` and stops. |
| LLM provider response | Semi-trusted | Output is parsed into a strict schema; anything unparseable fails closed. Evidence is recorded for replay. |
| Exchange API | Semi-trusted | Potentially-accepted submissions are quarantined rather than retried. Pre-submit failures close the empty strategy. |
| Operator approval | Trusted, but local | Gated on `TELEGRAM_APPROVER_USER_ID` inside the approval chat. |

Keep these intact. Loosening any of them is a security change, not a feature
change, and needs a deliberate decision rather than a default flip.

## Hardening expectations for contributors and operators

- `.env` holds live secrets. It is gitignored. Never commit it, paste it into an
  issue, or add it to an audit bundle or `repomix` export.
- Use **Bybit Demo keys only**, scoped to trading and read, never withdrawal.
  Demo-only operation is a safety property, not a deployment preference.
- `app_state` holds the Telegram session and SQLite database; `redis_state`
  holds Redis. Both are unencrypted at rest. `docker compose down -v` destroys
  them irreversibly.
- Any change to a path that reaches `bybit` execution is a privileged change.
  Say so in the PR description and expect the review to focus there.
- Any change that alters planning determinism, `UNCERTAIN` handling, source
  claiming, or approval gating needs a regression test proving the new behavior,
  not just the old behavior still passing.

## Out-of-band caveats

Passing tests does not prove reliability. Historical `MARKET` plans replayed
against current prices can fail, and Demo passes do not imply mainnet safety.
These are correctness limits, not vulnerabilities, unless a report shows a
specific path where a documented guard is absent.
