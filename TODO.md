# TODO

## Active exposure and trade lifecycle

Prevent new signals from accidentally increasing existing mirrored exposure for
the same symbol.

Account for Bybit positions and unfilled app orders, then extend this into
OPEN / REDUCE / CLOSE handling for trader follow-up messages.

## MENSA visual structure extraction

Interpret advanced chart-based setups instead of requiring every price in text.

- Detect entry rectangles only with reliable chart calibration.
- Use explicit anchors / price axis to derive numeric range boundaries.
- Derive structural stop deterministically from channel guidance.
- Preserve the anchor as one entry order; distribute remaining entries across
  the range.
- Risk budget determines quantity, not stop placement.
- Reject low-confidence geometry rather than invent prices.
