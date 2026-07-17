# Robinhood Agentic Momentum Routine

A scan-driven, autonomous equities trading routine for a Robinhood **Agentic** account. It screens for liquid, unusually-active stocks in a set price band, takes profits on winners, buys pullbacks, and sets protective stops — placing orders through the Robinhood agentic-trading MCP tools with per-trade notifications.

> ⚠️ **This project is not production ready. Use it at your own risk.** See the [Disclaimer](#disclaimer).

## What it does

Each run, the agent:

1. Manages existing holdings — sells winners up `TAKE_PROFIT_PCT`+ and cancels their stops.
2. Checks a daily-loss circuit breaker and halts new buys if the account is down past a set threshold for the day.
3. Builds a working list — stocks in the `$PRICE_MIN–$PRICE_MAX` band, trading at elevated **relative volume**, that have **moved** at least a minimum % on the day, ranked by relative volume.
4. Applies a **median dollar-volume liquidity floor** so thin names that can't be exited at size are dropped.
5. Opens new positions — buys names trading more than `DIP_ENTRY_PCT`% below their recent high **whose RSI has curled up from oversold** (reversal confirmation — depth alone is a falling knife), then places a stop `STOP_LOSS_PCT`% below the fill.

All trading is scoped to a single account, resolved **by name** at runtime.

## Strategy in one line

*Liquid, in-band, unusually-active movers that have pulled back off their recent high — bought with a stop, trimmed for profit.*

## Configuration

**Live trading is off unless you turn it on.** The routine places real entry orders only when a file named `LIVE_TRADING` exists next to the routine document; with no such file it runs in **dry run** — logging every would-be buy and stop instead of placing it. The file is gitignored, so a fresh clone never trades live, and enabling it is a local act rather than a committed edit. (Protection of existing positions — profit-taking, stop repairs, dust sweeps — is always live in both modes.)

All other tunable values live in the **Constants** table at the top of the routine document — edit there, nowhere else. Purpose of each:

| Constant | Purpose |
|---|---|
| `AGENTIC_ACCOUNT_NAME` | Account to trade, matched by name (default `"Agentic"`). |
| `PRICE_MIN` / `PRICE_MAX` | Price band for the screen. |
| `MIN_REL_VOLUME` | Relative-volume floor (also self-disables the routine when the market is closed). |
| `MIN_ABS_PCT_CHANGE` | Minimum daily move — filters out flat names. |
| `SCAN_TITLE` | Saved Robinhood scan the routine runs, resolved by exact title each run. |
| `MIN_MEDIAN_DOLLAR_VOLUME` | Liquidity floor (median $ volume). |
| `HIGH_LOOKBACK_DAYS` / `VOLUME_LOOKBACK_DAYS` | Lookback windows for the recent high and the liquidity median. |
| `TOP_N` | Max candidate list size. (fewer is better) |
| `DIP_ENTRY_PCT` / `TAKE_PROFIT_PCT` / `STOP_LOSS_PCT` | Entry, profit-take, and stop thresholds. |
| `RSI_PERIOD` / `RSI_INTERVAL` / `RSI_OVERSOLD` / `RSI_LOOKBACK_BARS` / `RSI_CONFIRM_BARS` | RSI curl-up entry gate: a dip is only buyable once it was oversold and has turned up. |
| `REENTRY_COOLDOWN_DAYS` | No re-entry for this many days after a symbol stops out. |
| `BUY_SIZE_PCT` / `MAX_POSITION_PCT` | Position sizing and cap. |
| `MIN_ORDER_DOLLARS` | Smallest allowed buy when downsizing to available buying power; below it, skip. |
| `DUST_SWEEP_ENABLED` | Daily cleanup of fractional stop-loss residue ("dust") on the first regular-session run. |
| `DAILY_LOSS_HALT_PCT` | Daily-loss circuit breaker. |
| `STOP_COUNT_HALT` | Halt new buys for the day after this many stop fills. |
| `SKIP_BUY_IF_SPY_RED` | Skip scanning/buying for the current run while SPY trades below its previous close. |
| `NO_BUY_FIRST_MINUTES` | Opening blackout: no buys during the session's first N minutes (selling/protection unaffected). |
| `REGULAR_HOURS_ONLY` | If `true`, no extended-hours entries. |
| `EXT_HOURS_LIMIT_BUFFER_PCT` | Limit buffer for extended-hours buys. |

## Requirements

- A Robinhood account with **agentic trading enabled**, connected via the Robinhood MCP server (`https://agent.robinhood.com/mcp/trading`).
- An agent runner/scheduler that loads the routine and honors per-tool approval settings.
- **Model:** configure the runner to use **Claude Sonnet** (current: `claude-sonnet-4-6`).

## Guardrails

- **Account scope** resolved by name every run; halts if the name matches zero, multiple, or a non-agentic account — never falls back to another account.
- **Daily-loss circuit breaker** halts new buys after a set drawdown.
- **Stop-count guard**: several stop fills in one day halt new buys until the next session — catches the slow bleed the P&L breaker can miss.
- **SPY red-day gate**: no dip-buying while the broad market itself is selling; per-run and self-clearing, so a green afternoon resumes trading the same day.
- **Opening blackout**: no buys in the session's first `NO_BUY_FIRST_MINUTES` — indicators can't see violence inside the first forming bars; profit-taking and stops stay active.
- **Liquidity floor** (median $ volume) keeps positions exitable.
- **Per-position stop-loss** and a **max position cap** — every stop is verified after placement, and broker-cancelled stops are re-placed immediately at a fresh level. A double failure halts new entries for that run and raises a line in the local `ALERTS.md` for human attention.
- **Re-entry cooldown**: a symbol whose stop filled is untouchable for `REENTRY_COOLDOWN_DAYS`, blocking revenge re-entries.
- **Broker compliance check** (`review_equity_order`) before every order.
- **Info notification** on every buy and sell.
- **Append-only trade ledger** (`trade-ledger.csv`, local/gitignored): every fill recorded with order id, price, reason, realized P&L, and the rules version (git hash of the routine doc) that produced it — the raw data for win-rate and expectancy review per rule era.

## Known tradeoffs

- **Does not function when the market is closed** — relative volume reads ~1 off-hours, so the entry list is empty by design.
- **A relative-volume + movement screen structurally surfaces volatile names** (falling knives, momentum spikes). The filters keep them *tradable*, not *safe* — human judgment is the intended backstop during testing.
- **Extended-hours buys are not immediately stop-protected** (stops only trigger in the regular session). Set `REGULAR_HOURS_ONLY = true` to avoid this.

## Testing before going live

1. Leave dry run on (no `LIVE_TRADING` file) and let a few scheduled runs log the entries they *would* have placed — no capital at risk. Do the same after any strategy-constant change.
2. Keep `place_equity_order` on **"Needs approval"** in the agent's tool permissions.
3. Run for several sessions and confirm: the candidate list looks sane, approvals actually fire on the scheduled runner, notifications land, and fills + stop placement behave.
4. Confirm the market-order and stop-order field names against the tool schema on the first regular-hours run (only the extended-hours limit path is verified so far).
5. Only after the above look right, consider dropping the approval gate and going live by creating the `LIVE_TRADING` file.

## Tools

- **tests/test_scripts.py** — dependency-free regression tests for the two deterministic scripts (`py -3 tests/test_scripts.py` / `python3 tests/test_scripts.py`); run them before committing script changes. Expected values were verified against live API data.
- **PriceBandScanner** (`tools/PriceBandScanner.md` + `tools/price_band_scanner.py`) — a read-only companion agent, scheduled once daily after market close. It runs the same saved scan, buckets the day's most-active stocks into price bands, and reports each band's median/mean % change, breadth, and best/worst names — evidence for choosing the `PRICE_MIN`/`PRICE_MAX` band. It never touches accounts or orders. Logs to `tools/logs/PriceBandScanner-log-YYYY_MM_DD.md` plus a same-named `.png` chart of the band medians (local only, gitignored). **Schedule it after the US close but before Asia starts trading — i.e., before 5:00 PM PT, when Robinhood's overnight (24/5) session opens and its prints would contaminate the day's data; ~1:05 PM PT is ideal.**

## Usage Example

Run the routine as a **scheduled task in Cowork** (Claude desktop app). The task's prompt tells the agent to read `robinhood-momentum-routine-autonomous.md` and execute it exactly as written — so edits to the document take effect on the next run without touching the task. Set the working folder to this repo, pick the model (per the runtime requirement), and choose Act mode:

![Edit scheduled task dialog in Cowork, with the prompt pointing at the routine document and Sonnet selected as the model](images/cowork-edit-scheduled-task.png)

Schedule it for market hours — this example fires every 30 minutes, Monday–Friday, 6:00 AM–1:59 PM PT (covering the 9:30 AM–4:00 PM ET session):

![Scheduled tasks list in Cowork showing the Robinhood momentum routine running every 30 minutes on weekdays](images/cowork-scheduled-tasks.png)

Note: scheduled tasks only run while the computer is awake — enable **Keep awake** (visible above the task card) so mid-day runs aren't missed.

## Disclaimer

**This project is not production ready — use it entirely at your own risk.** It is a personal execution framework for a self-specified strategy, hardened through live iteration but never formally validated: there is no backtesting, and the strategy parameters are untested against historical data (the regression tests in `tests/` cover the deterministic script math, not the strategy). It is **not financial advice** and not a recommendation of any screen, ticker, or parameter. Automated trading of volatile, unusually-active stocks carries real risk of loss, and an autonomous agent acts on your account without asking first. Understand the code, start with the order-approval gate on, and use only money you can afford to lose.
