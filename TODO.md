# TODO

## Trade lifecycle and performance attribution

Exposure warnings are implemented for existing Bybit positions and pending CCB entry orders. Execution remains user-controlled even when signals will net in the shared one-way account.

Add OPEN / REDUCE / CLOSE handling for trader follow-up messages and persist fills, exits and fees so per-channel performance can be reconstructed even when same-symbol signals overlap.

## MENSA visual structure extraction

Interpret advanced chart-based setups instead of requiring every price in text.

- Detect entry rectangles only with reliable chart calibration.
- Use explicit anchors / price axis to derive numeric range boundaries.
- Derive structural stop deterministically from channel guidance.
- Preserve the anchor as one entry order; distribute remaining entries across
  the range.
- Risk budget determines quantity, not stop placement.
- Reject low-confidence geometry rather than invent prices.
