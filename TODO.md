# TODO — deferred work before a stable 1.2

## Circuit breaker extensions: stop-count and drawdown guards
- Halt new buys when N stop-losses fill within a window (e.g. 3 in one day) — a regime signal the daily P&L breaker can miss.
- Halt or de-risk on cumulative drawdown from the account's high-water mark (the daily breaker resets every day and never sees a slow bleed).

## Version-stamp every run
Include the routine document's git commit hash in each run report (and optionally a column in `trade-ledger.csv`), so every trade is attributable to the exact rule set that produced it.

## Tests for the deterministic scripts
Small test suite for `evaluate_candidates.py` and `tools/price_band_scanner.py` using the already-verified fixtures: FISN/TTRX medians and highs (live-checked 2026-07-07), the interpolated-bar handling, and the degenerate-sample guard fixture.
