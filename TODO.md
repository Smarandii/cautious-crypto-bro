# Remaining work

Strategy V2 is implemented; package version and release tag are v2.2.1. The
independent-zone feature remains unreleased under #123.
Current behavior lives in [STRATEGY.md](STRATEGY.md).

- Preserve a path to mainnet after performance evaluation: add an explicit
  environment selection with Demo as the default, separate credentials/state,
  environment-labelled operator messages and explicit activation. Keep the shared
  exchange client reusable; current deployments and tests remain on Demo.

- Account explicitly for fees, funding and slippage in performance attribution
  and protected-profit floors.
- Detect manual edits/cancellations of every owned order type, especially fixed
  exits, without confusing them with fills.
- Surface strategy state, live protection and quarantine reasons in Telegram.
- Define safe recovery for legacy overlapping ladders; never automatically
  resume ambiguous ownership.
- Expand multilingual extraction checks for mixed entry/lifecycle posts and
  profit-card interpretation.
- Compare final implementation with forensic replay: drawdown, full-stop losses,
  profit give-back, realized R after costs and fill quality.
- Consider chart-derived entries only with reliable calibration, never invented prices.
- Defer speech/video ingestion until execution and extraction quality justify it.
