## What and why

<!-- What changes, and what problem it solves. Link the issue. -->

Closes #

## Safety impact

Answer all four. "None" needs a reason, not just the word.

- [ ] Reaches order placement, sizing, leverage, or exchange state
- [ ] Alters planning determinism (wall clock, randomness, network in planning)
- [ ] Alters `UNCERTAIN` handling, source claiming, or approval gating
- [ ] Relaxes an existing fail-closed guard

Describe the fail-closed behavior for ambiguous input, if this changes extraction
or planning:

<!-- If any box above is checked, explain how the change fails safe. -->

## Tests

- [ ] New branch has a test that fails without this change
- [ ] Bug fix includes a regression test reproducing the original failure
- [ ] Any exchange-response change includes a malformed/partial-payload case

Command run and result (paste your own, not CI's):

```
uv run pre-commit run --all-files
uv run pytest -q
```

## Conventions

- [ ] Conventional Commit subject, scoped to the touched module
- [ ] No new `C901` violation; complexity is decomposed, not the ceiling raised
- [ ] No secrets, `.env`, Telegram session files, or audit payloads in the diff
- [ ] No mainnet path introduced
- [ ] Demo credentials only

## Docs

- [ ] Behavior change reflected in [STRATEGY.md](../STRATEGY.md),
      [TESTING.md](../TESTING.md), or [TODO.md](../TODO.md)
- [ ] No operator-facing change without a doc note, if applicable
