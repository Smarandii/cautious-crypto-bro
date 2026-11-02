# Original-source interpretation audit — 2026-10-31 cohort

Report date: 2026-11-03. Scope: [#316](https://github.com/Smarandii/cautious-crypto-bro/issues/316),
under the unfinished [participation and magnitude goal #248](https://github.com/Smarandii/cautious-crypto-bro/issues/248).

**Recorded intake did not establish correct interpretation.** Reviewing the
original inputs found three extraction-contract discrepancies and one ticker
transcription discrepancy. Their separate fixes are delivered. One filled-entry
eligibility question remains unresolved in
[#340](https://github.com/Smarandii/cautious-crypto-bro/issues/340).
This bounded audit does not establish complete participation, counterfactual
lost profits or a profitable strategy.

## Frozen population

The publication-time window is
`2026-10-31T02:53:16.701917+00:00` through
`2026-11-01T02:53:16.701917+00:00`. The original coverage run used a
120-second settling delay and a 2,000-message limit per watched source.
All three history scans completed; none reported a partial boundary album.
Every album member had to be at or before the window end.

| Frozen unit | Count |
| --- | ---: |
| Supported visible post groups | 42 |
| Individual member messages | 47 |
| Albums, counted once per group | 5 |
| Image-bearing post groups | 26 |
| Retained images | 31 |
| Text-only post groups | 16 |
| Unsupported groups, excluded | 1 |

The three sources contribute 10, 15 and 17 supported groups. The excluded group
had neither supported text nor a supported image. Deleted/inaccessible history,
later posts, and the correctness of source claims are outside this population.
Seven posts showed edits after processing; one retrieved caption differed from
the original stored caption. The audit uses the original application inputs,
not a later edited caption substituted into the historical decision.

A separate stored-publication funnel contained 45 source records. It is a
different population. Its counts must not be subtracted from these 42 visible
groups to infer missing Telegram posts. Later 24-hour coverage checks are also
separate windows, not additions to this frozen cohort.

## Evidence and confidence

Three questions are assessed separately:

| Question | Evidence | What it proves |
| --- | --- | --- |
| Which input did the app process? | 42 exact original request-fingerprint matches, including all 31 image byte streams | Identity of the original application request inputs |
| Which decision was recorded? | All 42 frozen decision JSON records match the stored production decisions | Integrity of the compared decision records |
| Was the interpretation appropriate? | Manual comparison of original captions/images, raw extraction, normalization, trusted context and recorded dispositions | A bounded policy judgment or a documented discrepancy/unknown |

No per-image digest had been stored independently at original processing time.
Initial review therefore treated original media identity as uncertain. Subsequent
reconstruction matched every stored **composite** request fingerprint, which
already included image MIME types and byte hashes alongside source text,
prompt/schema/model parameters, guidance and trusted position context. All
16 text-only and 26 image-bearing requests matched, including the seven edited
posts. For the changed caption, reconstruction used its stored original text.

The reconstruction used the historical v2.2.47 fingerprint function and prompt,
with the then-unchanged domain and locked Pydantic versions. Changing caption
text or image bytes broke the matching fingerprint in negative controls. No
live record was backfilled to manufacture provenance. These matches bind the
app's inputs; they do not certify provider compliance or semantic correctness.

Every post has a private annotation and a separate confidence entry. Original
annotations are retained, including their earlier media uncertainty; the
fingerprint result supersedes that uncertainty for this corpus. Review-compatible
exclusions remain manual judgments, not independently measured model accuracy.
The unresolved case stays unresolved despite complete ingestion and input proof.

Private source identities, captions, images, raw model responses and per-post
fingerprints stay outside the repository and release assets. Original images
were inspected locally; none were uploaded to an external model for this audit.
No new-model agreement is used as ground truth.

## Recorded production dispositions

These are source-level dispositions from the frozen evidence, not counts of
complete exchange lifecycles or unique profitable trades.

| Recorded disposition | Post groups |
| --- | ---: |
| PROCESSED_NO_OP | 33 |
| NORMALIZATION_FILTERED | 3 |
| FAILED | 2 |
| EXECUTED | 3 |
| PLANNING_REJECTED | 1 |
| Total | 42 |

## Review findings

Each post appears once in the primary review grouping below. A post can also
have an orthogonal freshness or admission limit; the grouping is not a causal
partition of profit or an estimate of missed-trade recall.

| Primary review category | Groups | Confidence and consequence |
| --- | ---: | --- |
| Commentary, history, prospective instruction or already-copied update compatible with exclusion | 31 | Manual judgment using original inputs and trusted context; no accuracy percentage claimed |
| Current-caption authorization guard | 1 | Old embedded chat content did not authorize a current action |
| Extracted close blocked by age | 1 | Original deadline explains refusal; no replay justified |
| Recorded executed source decision | 3 | Stored execution disposition; not proof of profitable full lifecycle |
| Recorded planning rejection | 1 | Admission error and expired deadline explain the record; guard correctness remains a separate question |
| Extraction-contract discrepancy | 3 | Original input and stored proposal/reason conflict with documented evidence rules; separate fixes below |
| Ticker transcription discrepancy | 1 | Original image and recorded ticker differ; historical missed close/loss not demonstrated |
| Filled-entry semantics unresolved | 1 | Caption lacks independent direction; chart/promotion cannot supply trustworthy current-entry facts |
| Total | 42 | Input identity and interpretation confidence remain separate |

The planning rejection involved unverifiable portfolio stop risk because a
position lacked an exchange stop price. Its original deadline had already
elapsed before receipt, after approximately 2.33 hours of publication-to-receipt
delay. A later post for the same asset executed. This explains the rejection;
it neither proves the guard was wrong nor establishes a permanently missed
position. Protection must not be relaxed to make this historical case pass.

Both failed lifecycle records were stale. In the ticker discrepancy, the
correctly transcribed contract was also absent from the saved account context.
Neither those records nor the three interpretation discrepancies establish
that a corrected extraction would have passed freshness, account, ownership,
portfolio and protection checks and produced a profitable order.

## Delivered changes and remaining work

| Finding | Separate issue | Delivered behavior |
| --- | --- | --- |
| Model demanded an absent stop for a current exchange position panel | [#317](https://github.com/Smarandii/cautious-crypto-bro/issues/317) | Clarifies optional-stop extraction; deterministic planning/protection still decide admission |
| Model demanded the ticker in the caption despite a current attached position image | [#318](https://github.com/Smarandii/cautious-crypto-bro/issues/318) | Resolves the action symbol from the current attachment when caption authorization is sufficient |
| Promotional imagery vetoed an independent current-caption entry | [#319](https://github.com/Smarandii/cautious-crypto-bro/issues/319) | Keeps caption facts separate from marketing data; does not infer an add from copied history |
| Unverified unmatched action symbol could disappear as an ordinary skip | [#338](https://github.com/Smarandii/cautious-crypto-bro/issues/338) | Failed verification receives durable non-executable human review; no fuzzy ticker replacement |

The first three changes have synthetic real-provider development checks and
service/store regressions. They improve the specified boundary, not guarantee
future semantic compliance. #338 has controlled instrument/account and real
service/SQLite/delivery regressions; valid but wrongly transcribed unheld
contract names remain a limitation. Expired manual instruction delivery was
already addressed separately in
[#308](https://github.com/Smarandii/cautious-crypto-bro/issues/308).

The remaining filled-entry case declares that a limit filled but lacks caption
direction, a verified current position panel and matching trusted copied
ownership. A chart plus promotional card cannot safely supply those missing
facts. [#340](https://github.com/Smarandii/cautious-crypto-bro/issues/340) preserves
the unresolved question of non-executable review routing. It must not become a
fresh market order or a claimed correct exclusion merely to close this report.

This report closes only the fixed-cohort review in #316. The full participation
and natural Demo profit-magnitude requirements in #248, the broader
[audit #262](https://github.com/Smarandii/cautious-crypto-bro/issues/262), and
[independent zones #123](https://github.com/Smarandii/cautious-crypto-bro/issues/123)
remain unfinished. Runtime approval, risk, protection, entry and exit policy
were not changed by this report. No historical instruction was replayed, no
retrospective approval was issued, and no live synthetic order was placed.

## Inspectable evidence trail

- [Original-input reconstruction and limits](https://github.com/Smarandii/cautious-crypto-bro/issues/316#issuecomment-6591381199).
- [Aggregate checkpoint under the parent goal](https://github.com/Smarandii/cautious-crypto-bro/issues/248#issuecomment-6591381669).
- Local private evidence: `audit-artifacts/interpretation316/` contains the frozen
  cohort/group membership, original media, manual annotations, decision-integrity
  comparison, fingerprint reconstruction and per-post confidence map. These are
  deliberately excluded from source distributions and public artifacts.

[Zen of Python](https://peps.python.org/pep-0020/), "Explicit is better than
implicit" and "In the face of ambiguity, refuse the temptation to guess",
informed the separate confidence questions and unresolved classification.
[Grug, Testing](https://grugbrain.dev/#testing) (faithful paraphrase: test meaningful
interactions at stable boundaries) informed comparing original source evidence
with recorded decisions and downstream gates instead of treating intake counts
or model reruns as successful trading participation.
