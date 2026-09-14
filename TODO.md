# Roadmap

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
