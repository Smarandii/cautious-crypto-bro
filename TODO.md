# Roadmap

## Multi-intent, higher-recall extraction

Allow one Telegram post to produce multiple independent trade candidates.

- Prefer surfacing supported candidates over rejecting an entire mixed post.
- Existing-position screenshots should produce MARKET, not LIMIT at historical
  average entry.
- Updates/commentary must not automatically disqualify an otherwise complete
  trade.
- Keep execution-critical values strict: never invent symbol, side, entry or
  stop.

## Trade lifecycle

Add OPEN / REDUCE / CLOSE handling for trader follow-up messages.

Persist fills, exits and fees so per-channel performance can be reconstructed
when same-symbol signals overlap in the shared Bybit position.

## MENSA visual structure

Interpret chart entry structures only when image calibration is reliable.
Derive structural stops deterministically from channel guidance and reject
low-confidence geometry instead of inventing prices.

## Deferred

Speech-to-text and video ingestion: currently low expected value compared with
improving text/image signal recall.
