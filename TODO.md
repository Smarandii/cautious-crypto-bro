# Roadmap

## LLM provider abstraction

Separate provider transport from signal-extraction semantics. Define a small
provider-neutral interface implemented initially by `OpenRouterProvider` and
`OpenCodeGoProvider`, with provider/model selection controlled by environment
configuration.

Support an ordered fallback chain so a provider can fail over to another
configured provider on transport/provider failure or invalid structured output.
Failover must stay bounded and must never bypass deterministic validation,
planning, idempotency, or execution safeguards.

Keep prompts, schema validation, signal normalization, and trading semantics
provider-independent so additional providers can be added without changing the
execution pipeline. Add replay/shadow evaluation before changing the production
provider.

## Performance accounting

Persist fills, exits, and fees so realized signal performance can be
reconstructed reliably.

The Bybit position is shared and lifecycle actions are account-wide by symbol,
so performance reporting must not assume that a live position belongs to one
source channel without an explicit attribution model.

## MENSA visual structure

Interpret chart entry structures only when image calibration is reliable.
Derive structural stops deterministically from channel guidance and reject
low-confidence geometry instead of inventing prices.

## Deferred

Speech-to-text and video ingestion are currently lower value than improving
text/image signal recall.
