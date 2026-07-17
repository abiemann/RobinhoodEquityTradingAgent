# Constants — the routine's tunable values

All tunable values for `robinhood-momentum-routine-autonomous.md` live HERE and nowhere else. The routine loads this file at the start of every run and references the values by name only. If this file is missing or unreadable, the routine HALTS and places no orders.

**`DRY_RUN` rule:** the committed value of `DRY_RUN` is always `true` (anyone cloning this repo gets log-only entries). To trade live on your machine, set it to `false` as a LOCAL, UNCOMMITTED edit — never commit `false`.

| Constant | Value | Meaning |
|---|---|---|
| `DRY_RUN` | `true` | If `true`, the run evaluates everything but places NO new-entry orders — each would-be buy (and its stop) is logged in full instead (see the DRY RUN block in the routine document). Protection of EXISTING positions stays LIVE even in dry run: profit-taking, stop repairs, and dust sweeps still execute. Committed value MUST stay `true`; going live is a local uncommitted edit to this line. |
| `AGENTIC_ACCOUNT_NAME` | `"Agentic"` | The ONLY account the routine may read or trade. |
| `PRICE_MIN` | `2.50` | Lower bound of the last-price band (USD). |
| `PRICE_MAX` | `5` | Upper bound of the last-price band (USD). |
| `MIN_REL_VOLUME` | `2` | Relative-volume **floor** to qualify (today's pace vs. normal; `2` = twice normal). Not the selector — `TOP_N` ranking does that. Kept > 1 so the routine self-disables when the market is closed (rel vol reads ~1 for all names off-hours → empty list) and so quiet days don't pad the list with normal-volume names. |
| `MIN_ABS_PCT_CHANGE` | `3` | Minimum absolute daily move, in **percent**, for a name to qualify. Filters out flat SPACs/near-NAV churners that have high relative volume but aren't going anywhere. |
| `TOP_N` | `15` | Max names kept each run. After filtering, survivors are ranked by relative volume (highest first) and the top `TOP_N` become the working list. |
| `SCAN_TITLE` | `"Volume field probe"` | Exact title of the saved Robinhood scan the routine runs. Resolved to its scan_id via `get_scans` each run — the id itself is never hardcoded. This scan is known-good: STOCK-only filter with `Last`, `Relative volume`, `% Change`, and `Volume` visible, sorted by relative volume descending. |
| `HIGH_LOOKBACK_DAYS` | `5` | Trading-day window used to find each name's recent intraday high. |
| `VOLUME_LOOKBACK_DAYS` | `20` | Trading-day window used to compute each name's median daily dollar volume for the liquidity floor. |
| `MIN_MEDIAN_DOLLAR_VOLUME` | `175000` | Liquidity floor: skip any name whose **median** daily dollar volume (median over `VOLUME_LOOKBACK_DAYS` of volume × close) is below this. Median, not mean, so a single one-day volume spike — exactly what this strategy chases — can't lift an otherwise-thin name over the floor. Removes names that can't be exited at size. |
| `DIP_ENTRY_PCT` | `5` | Buy when current price is more than this % below the `HIGH_LOOKBACK_DAYS` high. |
| `RSI_PERIOD` | `14` | RSI lookback period in bars for the entry gate, computed on `RSI_INTERVAL` bars. |
| `RSI_INTERVAL` | `30minute` | Bar interval for the entry-gate RSI — matches the run cadence. |
| `RSI_OVERSOLD` | `35` | The candidate must have printed RSI at/below this within the last `RSI_LOOKBACK_BARS` bars to be buyable (35, not the classic 30 — starting point to tune; these small-cap dips rarely print 30). |
| `RSI_LOOKBACK_BARS` | `5` | Window (in `RSI_INTERVAL` bars) in which the oversold touch must have occurred. |
| `RSI_CONFIRM_BARS` | `1` | Consecutive rising RSI values required — the "curl up" saying the fall has at least locally stopped. |
| `TAKE_PROFIT_PCT` | `2.5` | Sell a held position when it is up this % or more vs. entry. |
| `BUY_SIZE_PCT` | `20` | Order size for each buy, as a % of total account value. |
| `MIN_ORDER_DOLLARS` | `50` | Smallest allowed buy. When buying power can't fund a full `BUY_SIZE_PCT` order, the order is DOWNSIZED to available buying power — but never below this floor; below it, skip the buy (tiny fills create dust positions that can't carry a whole-share stop). |
| `EXT_HOURS_LIMIT_BUFFER_PCT` | `0.5` | For extended-hours buys only: how far above the current price to set the limit, so the order is marketable but capped against wide after-hours spreads. Raise it if extended-hours orders aren't filling; lower it to cap slippage. |
| `REGULAR_HOURS_ONLY` | `false` | If `true`, the routine only opens new positions during regular market hours and skips all buys in extended sessions. If `false`, it also buys in extended hours using whole-share limit orders. See Tradeoffs in the routine document. |
| `MAX_POSITION_PCT` | `20` | Hard cap on any single position, as a % of total account value. Must be ≥ `BUY_SIZE_PCT`. |
| `STOP_LOSS_PCT` | `3.5` | Stop-loss sell placed this % below the actual fill price. |
| `REENTRY_COOLDOWN_DAYS` | `3` | After a symbol's stop-loss FILLS, do not re-enter that symbol for this many calendar days — a revenge-trade guard: a name that just proved it can fall `STOP_LOSS_PCT`% does not get bought again on the next dip signal. Checked against the broker's filled-order history (`get_equity_orders`) each run, never against local files. |
| `DAILY_LOSS_HALT_PCT` | `5` | Halt new buys for the day if trailing-day loss reaches this % of total account value. |
| `STOP_COUNT_HALT` | `3` | Halt new buys for the rest of the day once this many stop-loss sells have FILLED today. A regime guard: at `STOP_LOSS_PCT` = 3.5 and ~20% positions, each stop costs only ~0.7% of the account, so several stop-outs can bleed all day without ever tripping `DAILY_LOSS_HALT_PCT`. Counted from the broker's filled-order history; resets naturally at the next trading day. |
| `SKIP_BUY_IF_SPY_RED` | `true` | If `true`, each run checks SPY's day change before scanning: SPY below its previous close = the broad market is selling, so skip scanning and new buys FOR THIS RUN ONLY. Per-run and self-clearing — the next run re-checks, so a green afternoon resumes buying the same day. Holdings management, stop repairs, and dust sweeps are never gated by this. |
| `NO_BUY_FIRST_MINUTES` | `45` | Opening blackout: NO buys during the first this-many minutes of the regular session (09:30 ET onward). Completed-bar indicators are blind to violence inside the session's first forming bars — PLSM (2026-07-13) was bought 13 minutes after the open and knifed in 6; no indicator could have seen it. Profit-taking, stop repairs, and dust sweeps run normally during the blackout — only buying waits. |
