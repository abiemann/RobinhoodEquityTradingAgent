# TODO — deferred work before a stable 1.2

## Circuit breaker extension: drawdown guard
Halt (or de-risk) new buys on cumulative drawdown from the account's high-water mark — the daily breaker resets every day and never sees a multi-day slide. Resume semantics still to be chosen; recommended: trailing 30-day high-water computed from run-report `total_value` values (fail-open if reports are missing), so the guard cannot deadlock a mostly-cash account and un-trips as the peak ages out of the window. (Stop-count guard shipped separately.)

## Tests for the deterministic scripts
Small test suite for `evaluate_candidates.py` and `tools/price_band_scanner.py` using the already-verified fixtures: FISN/TTRX medians and highs (live-checked 2026-07-07), the interpolated-bar handling, and the degenerate-sample guard fixture.
