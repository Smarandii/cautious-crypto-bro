# Opportunity-coverage audit — 2026-10-26

## Scope and method

A fresh eight-hour run covered `2026-10-26T12:37:11Z` through
`2026-10-26T20:37:11Z` across the three watched Telegram sources. The audit
replayed the same Telegram groups through the production extraction prompt and
model (`openai/gpt-5.6-luna`) with the evaluation cache bypassed, then compared
the fresh result with the stored production decision for each group.

The run produced 26 post groups:

| Category | Count |
| --- | ---: |
| Non-actionable | 23 |
| Actionable under the fresh evaluation | 2 |
| Ignored by the Telegram pipeline | 1 |

The ignored group contained no text and no supported image, so it never reached
extraction. The fresh actionable groups were one false-positive promotional
recap and one real 25% reduce instruction.

Private evidence, including source images and the full decision bundle, is kept
under `audit-artifacts/coverage-20261026/` and is not committed.

## Production-versus-fresh differences

Three groups differed; only one indicates a fresh-extraction risk.

1. **Promotional recap card (group 3866).** The fresh evaluation emitted two
   `NEW OPEN` candidates (ENAUSDT and SUIUSDT) from Bitunix Pro performance
   cards showing large realized percentages, QR and partner/referral branding.
   Production correctly treated the post as non-actionable. This is an
   extraction-stability risk, not a production miss. Prompt hardening is
   implemented in #167.
2. **Screenshot-only BNB entry (group 3870).** Production extracted a
   `BNBUSDT LONG` MARKET candidate from an exchange screenshot with general
   market commentary; the fresh run treated it as an observation. Production's
   documented screenshot-entry behavior is deliberate. The intent was then
   skipped by the frozen LONG-participation experiment, not by a parser or
   ingestion failure.
3. **INJ close instruction (group 8880).** Both runs extracted the explicit
   `CLOSE` instruction, but normalization dropped it because there was no
   active copied INJ position. This is the intended position-action scope, not
   a miss. The ZEC 25% reduce in group 8884 was also persisted and skipped for
   the same reason.

## Paired image-replay evidence

The #167 prompt guard was tested by replaying the private stored image bytes
with the current prompt, the production model and the evaluation cache bypassed.
No original image, caption, hash or exchange identity is committed.

- **Negative control (captionless promotional recap), 3/3 runs:** zero OPEN
  intents, zero position actions, `actionable=false`. The model identified ROI
  badges, historical/current prices, QR/referral branding and marketing panels
  rather than a live exchange position. An earlier no-context run also produced
  zero OPENs 3/3.
- **Production positive control (genuine live-position screenshot, original
  caption and production position context), 2/2 runs:** exactly one `BNBUSDT
  LONG` MARKET OPEN and zero lifecycle actions. This reproduces the existing
  screenshot-entry behavior.
- **Synthetic positive control (same screenshot, explicitly synthetic
  current-entry caption and empty trusted source history), 2/2 runs:** exactly
  one `BNBUSDT LONG` MARKET OPEN. This labels the control construction and
  proves the guard still permits an actual exchange position panel with a
  current entry instruction.

The prompt-substring assertion in `tests/test_openrouter.py` remains a
regression guardrail; it is not presented as behavioral proof.

## Post-deploy coverage window

After `66a8216` was deployed, a fresh eight-hour audit covered
`2026-10-26T15:53Z` through `2026-10-26T23:53Z`. It found 9 groups: 0
actionable, 8 non-actionable, 1 pipeline-ignored and 0 evaluation errors. The
ignored group had no text or supported image. This window did not include the
original promotional card, so the paired replay above is the direct evidence
for that guard rather than this later audit.

Private artifacts for this window are retained locally under
`audit-artifacts/coverage-20261027/` and are not committed.

## Conclusion and limitations

No supported ingestion gap was found in the 26-group window. The older
historical `UNKNOWN` decisions remain unclassified and are not evidence of
missed posts. The audit covers one model, one prompt revision and one eight-hour
window; the private artifacts are not a substitute for repeating the run after
prompt or model changes.

Follow-up: the #162 guard is deployed at `66a8216`; repeat a fresh coverage
window periodically and compare the same categories.
