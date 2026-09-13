# TODO

## Dynamic OpenRouter provider circuit breaker

Replace the current static provider exclusions with runtime provider health
tracking.

Desired behaviour:

- identify provider-attributable failures, for example:
  - embedded provider errors / 5xx
  - repeated malformed structured output
  - truncated `finish_reason=length` degeneration
- put the failing provider into a cooldown, initially 12 hours
- dynamically add providers in cooldown to the OpenRouter `ignore` list
- retry inference so the retry is routed to another healthy provider
- automatically restore a provider after its cooldown expires
- record failure reason, failure time and cooldown expiry for diagnostics
- avoid penalizing a provider for failures that cannot confidently be
  attributed to that provider

For the current single-instance deployment, SQLite is sufficient and avoids
adding Redis only for this feature. Redis becomes useful if multiple app
instances need to share provider-health state.

The current hardcoded `nextbit` and `parasail` exclusions can eventually be
replaced by this mechanism.

## Active trade / duplicate exposure guard

Track active mirrored trades by symbol and reject opening another trade when
there is already active Cautious Crypto Bro exposure for that symbol.

This should eventually consider both:

- an existing Bybit position
- unfilled Cautious Crypto Bro entry orders

Do this together with trade lifecycle support rather than inferring semantic
trade identity from Telegram text.
