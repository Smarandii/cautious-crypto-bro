# Filled-entry reviews

A current caption declaring a named USDT entry has just filled can require human
review when direction is unavailable. This is information, never an approval:
the notice has a source link and publication time, no Execute button, side,
price, leverage or order. Existing independently evidenced entries are unchanged.

The model proposes an exact caption quotation. Deterministic normalization checks
the quotation, one explicit matching USDT symbol and bounded English/Russian
current-fill wording; it rejects historical, conditional, hypothetical, negative
and promotional statements. Matching active or in-flight source copies suppress
the notice regardless of side. Unavailable account context is explicitly unknown,
not proof that a position was never copied. This bounded recognition is not a
claim that all languages or all paraphrases are recognized. No old posts are
replayed or automatically re-evaluated.

Original source metadata/text, the accepted quotation and evaluation fingerprint
remain private in schema17's additive `entry_reviews` table. Reservation checks
the source-processing claim inside the same transaction as insertion. One record
per source/symbol is retained across retries. Delivery has a five-minute claim
lease and retries failed sends after thirty seconds; periodic recovery also
recovers process loss. Telegram acknowledgement loss can duplicate a notice;
remote exactly-once delivery is not promised. The queue is independent of trade
approval records and cannot become a trade through existing callbacks.

## Schema and rollback

Schema16 -> 17 only creates the review table. Existing trading tables and their
records are unchanged. Old application versions reject user_version17, so do not
start the old image directly and do not restore a pre-upgrade database after new
trading activity.

For rollback, stop the app and publisher through the normal maintenance/cutover
flow, then use the **new image** to run:

```text
python scripts/rollback_entry_review_schema.py /state/intents.sqlite3 /state/before-review-rollback.sqlite3
```

Substitute the actual configured database path and a new protected backup path.
The command acquires the application's state-ownership lock, checks schema17,
table shape and database integrity, backs up the **current** database without
overwriting an existing file, then changes only user_version to16. It retains all
current trading ownership and review records. Restore the verified old image,
restart app and publisher together, then verify authenticated startup, source
reader and live position protection. Pending review delivery pauses on the old
version. Re-upgrading to17 reuses the retained table and resumes pending notices;
it does not recreate trades or replay source history. Backups contain private
account/source state and must stay in the protected state volume.

Design rationale: [Zen of Python](https://peps.python.org/pep-0020/), “In the face
of ambiguity, refuse the temptation to guess”: this path records an unknown
direction without fitting it into an executable trading object. [Grug, Testing](https://grugbrain.dev/#testing)
(faithful paraphrase): verify the service, SQLite and bot interactions, including
failure/restart and exclusions, rather than trusting model agreement alone.
