# INCIDENTS — why the rules say what they say

**Audience: anyone EDITING this repo. The runtime never loads this file.**

`robinhood-momentum-routine-autonomous.md` is read by an LLM on all ~16 runs a day, so every
sentence in it is a per-run cost. The rules live there; the history that produced them lives here.

Read this before "simplifying" a rule that looks redundant, over-specific, or paranoid. Nearly
every oddity below was written after a live failure, several of which cost real money. If you
remove a rule, remove its entry here too — and if you add a rule because something broke, add the
story here rather than in the routine.

Organised by the routine section the rule lives in.

---

## ORDER HANDLING — fractional fallback

**2026-07-13, SKYQ.** A fractional/dollar-based buy was alerted by `review_equity_order` with
`EQUITY_FRACTIONALLY_UNTRADABLE_ERROR_BUY`. The run improvised a whole-share fallback (56 shares
@ $4.0099), re-reviewed it clean, and it filled. The improvisation was correct, so it was codified
the same day (commit `5e2dec6`): that one alert is a routing correction, every other alert stays a
hard skip.

## ORDER HANDLING — durable placement intent and reconciliation

**2026-07-31, lost acknowledgement could duplicate an order (P1 review finding).** Placement
retries previously depended on the agent remembering to reuse a `ref_id`, with no durable record
of the exact payload or whether a timed-out call had reached Robinhood. Current order-query results
do not expose `ref_id`, so a crash, malformed response, or fresh-UUID retry could create a second
buy or sell while the first order was already live. No loss was attributed; the safety review
caught the gap. `order_intents.py` now persists the immutable payload and one UUID before the call,
reconciles fresh fully paginated orders/positions by broker ID or one exact post-baseline
fingerprint, and allows one same-run replay only after a proven no-match, always with the same UUID.
Prior-run replay, explicit-rejection replay, ambiguous matches, partial-fill resubmission, chained
stop retries, and corrupt journal state fail closed; every fill-bearing order is reconciled to a
terminal cumulative quantity before notification or ledger append.

## ORDER HANDLING — stop verification ("placement is not protection")

**2026-07-14, UBXG.** The broker cancelled a freshly placed stop **77 ms** after creation: the
price had already fallen through the stop level, and a sell-stop at or above the market is invalid
on arrival. The position sat naked for **23 minutes** on a falling name until the next run's
stop-coverage audit repaired it at $4.80 — which then filled at $4.65, a −13.4% / −$43.70 loss,
roughly 2× the designed 5% floor. Fix `dacb647`: verify every stop by id immediately, re-place
cancelled/rejected stops at a fresh level.

**2026-07-14, escalation.** Added by `6375eb3`: a stop that fails verification **twice** halts new
entries for that run and appends to `ALERTS.md`. Rationale — never add fresh exposure while a held
position is knowingly unprotected.

## CURRENT TIME — the clock script

**2026-07-21, the silent-failure clock bug** (commit `37d2c68`). The 08:07 run was caught writing
itself a throwaway script mid-run just to work out what time it was. Root cause was a spec gap: the
routine depended on the current time in six places but never said where to get it, so every run
improvised a clock.

The dangerous part surfaced while testing the fix: on hosts without tzdata,
`TZ=America/Los_Angeles date` returns **GMT with no error at all** — 7 hours off — and Python's
`zoneinfo` raises `ZoneInfoNotFoundError`. An improvising run doesn't crash; it gets a
wrong-but-plausible time and can sail straight through the opening blackout or count "filled today"
against the wrong calendar date. This is why `market_clock.py` computes DST from the rule itself
(2nd Sunday March / 1st Sunday November) depending only on UTC. **Never "simplify" it back to a
timezone library.** The named failing commands stay in the routine deliberately — they stop a run
from rediscovering them.

**2026-07-22, the constant-substitution near-miss** (commit `67555a8`). A run invoked the script
with `--no-buy-first-minutes 5` while `constants.md` said `45`. Safe only by luck: the arithmetic
still tripped the block. The mismatch could have unlocked buying inside the blackout window. Fix:
the script reads the value from `constants.md` itself; the flag survives only as a test override;
a missing `constants.md` now errors loudly rather than defaulting to 0 (which would have silently
disabled the gate — the worst failure mode for a safety script).

**2026-07-29, configuration-halt precedence gap (P1 review finding).** The routine correctly said an unreadable `constants.md` should halt, but its DRY RUN and generic clock-failure fallbacks still let live profit-taking, stop repairs, and dust sweeps run. That made a configuration failure a partial run rather than a halt. No loss was attributed; the safety review caught it. Constants validation now happens before the clock or any broker call, and a read/validation failure stops the entire run without defaults, cached values, or safety orders.

**Generalised lesson for editors:** any `<CONSTANT_NAME>` placeholder in a shell command is an
invitation for the model to type a value from memory instead of reading the file. Treat every
placeholder in a command line with suspicion. `filter_scan.py` still has five of them.

**2026-07-22, `py` on Linux** (commit `85e38c4`). The clock commit had documented the call as
`python3 market_clock.py  (Windows: py -3 …)`. That parenthetical was meant for humans running
tests locally, but the routine reads it as an equally valid choice, and the Linux sandbox agent
picked `py -3` first — exit 127, `bash: py: command not found`, in both the 11:07 and 11:37 runs.
`py -3` belongs only in AGENTS.md and README.md.

**2026-08-07, WindowsApps alias caused a false missing-Python halt.** The 13:01 scheduled run
never reached its lifecycle start, configuration check, broker connector, or scan. `py -3` failed
before process startup with `The file cannot be accessed by the system`; the Codex workspace
dependency lookup then stalled; and `Get-Command python` returned only the zero-byte Microsoft
Store alias under `Microsoft\WindowsApps`, which failed with the same message. The task halted
safely, but two real Python 3 runtimes were available. The immediately preceding 12:34 run had
encountered the same launcher failure, found a real absolute interpreter, and completed normally.
The fix is the checked-in `resolve_python.ps1`: it rejects Store aliases, probes every permitted
absolute candidate for Python 3, returns one validated path, and requires the invocation to reuse
that path for every helper. A Codex ACL/access-denied failure gets one narrow host-capable retry;
mere PATH resolution is never proof that a runtime works.

**2026-08-07, valid Windows paths were second-guessed or embedded in Python source.** A later run's
first resolver call returned a valid, launch-probed Python 3 path, but the orchestration made an
unnecessary second call to reconcile the optional preferred hint's displayed JSON escapes with
the resolver's normalized output. Near report completion, the status file was valid on disk, but
its first parse check embedded the native `D:\...\run-reports\...` path inside `-c` Python source;
the `\r` sequence was interpreted as a carriage-return escape and produced a false verifier
failure. The unchanged file passed when the path was retried with forward slashes. Neither event
affected broker work, persisted data, or trading decisions, but both added noise and latency.

The resolver receipt is now terminal and authoritative: its returned `python` path is used directly
without comparing, rewriting, or rerunning to "correct" the hint. File paths cross the PowerShell /
Python boundary as separate argv values, never as interpolated Python source and never through
manual slash rewriting. Regression tests launch the returned executable and verify a JSON file
through a native backslash path.

**2026-07-29, exchange-calendar gap (P1 review finding).** The clock classified every weekday by
fixed wall-clock hours, so an NYSE holiday could look like a normal regular session. With
`REGULAR_HOURS_BUY_ONLY = false`, that gap could also reach the extended-hours entry path. No loss
was attributed to it; the safety review caught it before a trade. The fix adds a reviewed,
checked-in NYSE calendar for 2026-2028, recognizes full closures and 1:00 p.m. ET early closes,
and fails closed outside the reviewed coverage. `entry_session_open` is now the single
unconditional entry clearance: it blocks scanning and new buys but never disables profit-taking,
stop repairs, dust sweeps, or the other protection work.

## RUN COORDINATION — overlap and stale-session fencing

**2026-07-30, overlapping-run race (P1 review finding).** Runs are scheduled 30 minutes apart,
but the routine had no shared ownership check. A slow first run and a newly started second run
could both read the same positions, open orders, and buying power, then independently place the
same candidate. The first run also treated its start-time session as valid indefinitely, so a
long evaluation could review or place after an early close or other session transition. No loss
was attributed; the review caught both paths.

The fix uses `run_lock.py`, not a hand-authored lock file. A one-row SQLite transaction makes
acquisition and expired-lease takeover atomic across Windows and Unix. Each owner receives a
random fencing token and must renew at phase boundaries and immediately before every broker
mutation; after takeover, the old token cannot renew, release, cancel, or place. The 20-minute
lease is intentionally shorter than the 30-minute schedule so a crashed task does not strand the
account behind a permanent lock. A still-running old task may finish read-only work after expiry,
but it cannot mutate once a newer owner exists. Every buy also re-runs `market_clock.py` before
review and immediately before placement; the fresh reading controls only current entry
eligibility and routing, while the original clock remains authoritative for filenames, trading
date, ledger windows, and the report.

**2026-08-07, pre-broker sequencing correction.** The startup requirements were individually
correct but distributed across sections with competing immediate-next wording. One scheduled run
created and preflighted scratch, checked the order-intent journal, and called `pending` with a
placeholder UUID before invoking lease acquisition. The agent caught the omission before
`get_accounts` or any other broker call, acquired the real lease, reran `pending` with the
lease-issued token, renewed normally, and completed without an order or financial impact.

The fix defines one numbered startup chain and tests its order: lifecycle, validated constants,
START CLOCK and lifecycle binding, lease acquisition, scratch creation/preflight, journal
check/pending with the exact acquired token, rules version, then account resolution. Scratch and
journal work are explicitly forbidden before acquisition, and placeholder, invocation, remembered,
or otherwise invented fencing tokens are never authoritative.

**2026-08-11, Claude Cowork FUSE transaction left a hot rollback journal that blocked read-only
lifecycle validation.** A Claude
Cowork/local-agent run opened the Windows checkout through its isolated Linux VM and a writable
FUSE mount. Host `powershell.exe` was unavailable, so the agent fell back to
`/usr/bin/python3` and ran `run_lifecycle.py start`. SQLite began a write transaction and
created a valid rollback journal, then returned `disk I/O error` during transactional I/O on that
bridge and left the transaction hot. No lifecycle start event committed, no lease was
acquired, no Robinhood call or mutation occurred, and no account status file changed. The adjacent
hot journal then made the dashboard's intentionally read-only SQLite open fail with
`attempt to write a readonly database`, because rollback recovery itself requires a write.

The failed agent ignored the lifecycle-start terminal rule, ran further SQLite probes, created an
unlinked normal-looking report, misattributed the journal to the prior successful run, and advised
deleting it. That advice was unsafe: the journal carried SQLite before-images needed for recovery.
After the runner was stopped, host-native `py -3 run_lifecycle.py export` let SQLite perform its
own rollback, republished the projection, preserved all 326 committed events, and restored the
dashboard without changing the last truthful account snapshot.

**Rule produced:** a Windows-hosted checkout exposed through POSIX/FUSE is never treated as native
Linux. If the host Windows resolver cannot run, the routine halts before lifecycle rather than
falling back to sandbox Python; Claude uses the Desktop Code tab with Environment: Local on native
Windows, not Cowork/local-agent. Before a write-capable lifecycle mutation opens production state,
the helper rejects known shared mounts and probes unknown POSIX filesystems using disposable state.
A failed lifecycle start is terminal:
no later probes or run artifacts. The dashboard remains read-only and gives the supported
host-native `run_lifecycle.py export` recovery action. No agent or operator manually deletes,
renames, overwrites, edits, or bypasses SQLite journal/WAL/SHM sidecars.

**2026-08-11 14:59 follow-up, the new guard halted safely but the recovery instruction was
incomplete.** The next Claude scheduled attempt ran in a Bash execution context and the sole
bootstrap command failed with exit 127 because `powershell.exe` was not found. This time the agent
followed the hardened rule: it did not substitute sandbox Python, did not start lifecycle, made no
Robinhood call, and created no report, status, lock, intent, or other run artifact. Repository and
lifecycle timestamps confirmed that the attempt wrote nothing.

Screenshots showed the Code tab with the **Local** sidebar filter and new-session selector already
selected, but those controls do not migrate an existing scheduled task. Although the exported run
contained no scheduler metadata, app-local session provenance placed it under
`local-agent-mode-sessions`, labeled it `sessionType: scheduled`, and linked it to the legacy task
store. The native Code scheduler task list was empty, while genuine Code Local sessions launched
PowerShell. This definitively identified the 14:59 firing as the legacy Cowork/local-agent task.
Telling the operator only to “select Local” therefore repeated an action already taken and did not
migrate the schedule.

**Follow-up rule produced:** pause the legacy task and create a new, uniquely named task through
Code → Routines → New routine → Local, bound to the exact native Windows main checkout with
worktree isolation off. Run it once under supervision with `DRY_RUN = true`; require both the
PowerShell resolver and `get_accounts`, then the complete lifecycle/report/status/dashboard smoke
test. Delete the paused legacy task only after that proof. Until this succeeds, the Claude Code
scheduler path remains unproven end to end.

**2026-08-11 15:55 resolution, native Claude Code Local demonstrated the platform path but did not
complete the prescribed acceptance test.** Part A alone ran from the native Windows main checkout
with worktree isolation off and `DRY_RUN = false`; Part B was never tested with **Run now**. Part A
used PowerShell and one resolver-bound Windows Python, called `get_accounts` and the required
read-only account tools, completed lifecycle and lease handling, published its report and status
snapshot, released the lease, and left the dashboard healthy. Monitoring observed no unexpected
SQLite journal, WAL, or SHM sidecar.

The account was flat and the run occurred after hours, so the regular-hours-only gate skipped the
entry path before the scan, daily-loss snapshot, or any order work. No order-mutation tool was
called. That made the live-mode run safe, but it proves only the native execution substrate,
connector/account-read path, coordination state, artifacts, and dashboard—not the required
`DRY_RUN = true` acceptance, Part B, an entry-eligible scan, or an order path.

The installed Claude Local form rejected the twice-hourly expression `0,30 6-13 * * 1-5`
with “Scheduled tasks must run at most once per hour.” The documented workaround uses two complementary
hourly Local tasks with the same prompt: Part A `0 6-13 * * 1-5` and Part B
`30 6-13 * * 1-5`. Because a saved Custom cron becomes active immediately, the safe rule is to
create both tasks with **Schedule: Manual**, test each with **Run now** and `DRY_RUN = true`, inspect
each task's Allowed permissions, and add the Custom crons only after both proofs pass. If a cron
task already exists, immediately Pause it in task detail and verify that it is disabled and no run
began. The permission control currently labeled **Auto** must not auto-approve
`place_equity_order` or `cancel_equity_order`; if the build cannot keep both approval-gated, live
Claude trading stays disabled. Cron previews must show the intended Pacific bounds after any
timezone conversion, and randomized start delays are expected.

Legacy cleanup remains separate: return to the original Cowork/Scheduled interface, pause the
legacy task there, and verify that it no longer fires. The Code Routines list does not control it.
Delete the legacy task only after both replacement proofs pass and no further legacy firing occurs.

## BROKER TIMESTAMPS

**2026-07-17.** `datetime.fromisoformat()` in the sandbox raised
`ValueError: Invalid isoformat string: '2026-07-09T15:41:35.16+00:00'`. Broker timestamps carry
variable-precision fractional seconds (`.16`, `.785`, `.708917` all observed live). A timestamp
parse error must never abort a safety check, hence the string-comparison rule.

## BROKER ORDER OBJECTS — the schema block

**2026-07-20.** A run discovered it had been filtering `get_equity_orders` on a nonexistent
`order_id` field and on `type == "stop"` — the real id field was `id`, and the returned stop shape
observed at the time was `type: "market"` with `trigger: "stop"`. Every order filter in the routine
(stop-coverage audit, stop-count guard, re-entry cooldown, stop-fill discovery, ledger dedupe)
depended on that wrong shape. Fixed by documenting the then-verified schema once (commit `224b125`).

**2026-07-29, stop-payload schema contradiction (P1 review finding).** The same document later told order placement to send a stop type, contradicting the market-plus-stop-trigger connector input observed at the time. A live agent could therefore choose the rejected shape while attempting protection. The routine was consolidated around that connector contract.

**2026-07-31, connector order-contract drift.** The connector input schema changed again:
regular-hours notional market orders now use `dollar_amount`, and stop orders use
`type: "stop_market"` plus `stop_price`; `dollar_based_amount` and the stop-order `trigger` input
are no longer accepted. The routine and its contract tests had pinned the older fields, which could
reject a buy or protective-stop repair. Updated every order payload to the current contract and
made returned-order filtering tolerate both normalized `type: "stop_market"` results and the
older broker-returned `type: "market"` plus `trigger: "stop"` shape without inferring stops from
`stop_price` alone.

## CONFIGURATION PREFLIGHT — ad-hoc validator rejected a valid value form

**2026-07-31, ambiguous scheduled-run preflight.** A scheduled agent generated its own PowerShell
table/value checks, then reported that the first command had "rejected its own value-form check."
The file was ultimately valid and no broker call preceded the successful second check, but the
diagnostic could not identify a bad constant because the generated validator itself was the variable.

Fixed by making `validate_constants.py --json` the mandatory first-action authority. It owns the
exact 31-row schema, literal forms, ranges, and cross-setting safety constraints; returns the parsed
values so the agent never re-parses the table; and emits line-specific errors before any broker or
market action. Scheduled runs may no longer invent a PowerShell/regex substitute or decide that a
validator failure is harmless and continue.

## DAILY-LOSS CIRCUIT BREAKER — cost basis is not a daily baseline

**2026-07-31, false daily-P&L sign (P1 review finding).** The breaker added Robinhood's
cost-basis realized P&L to every open position's lifetime unrealized P&L and called the result
"trailing-day P&L." AVIR demonstrated the error: it was bought below $4.36, closed the prior
session around $4.51, and sold the next morning at $4.42. Robinhood correctly reported a lifetime
realized profit of $4.28, but the account actually lost about $6.07 that broker day because the
relevant baseline was the prior close, not the old purchase price. The guard could therefore show
the wrong sign and admit new exposure during a real daily drawdown.

Fixed by making `daily_loss.py` the sole breaker authority. It consumes fresh raw broker responses,
fully paginates unfiltered equity orders, filters individual executions by Eastern date, includes
fees and partial fills, reconstructs opening quantities from executions, reconciles them against
`intraday_quantity`, and marks opening shares from split-adjusted official prior closes. It also
reconciles every order's executions to `cumulative_quantity` and rejects stale/future current marks.
Any missing page or execution, malformed value, stale price/close, duplicate conflict, or quantity
mismatch blocks entries instead of falling back to cost-basis P&L. `get_realized_pnl` remains
dashboard/report telemetry only.

## TRADE LEDGER — rules_version and `--no-optional-locks`

**2026-07-14.** Two scheduled runs were killed mid `git status` index-refresh (the 09:07 and 10:07
runs), orphaning `.git/index.lock` and **blocking all commits for hours**. Hardened in `423f858`:
the version-stamp check uses `git --no-optional-locks`, which is purely read-only and has nothing
to orphan.

## TRADE LEDGER — verified appends ("issued is not persisted")

**2026-07-15, the phantom-append incident.** The 10:18 run reported 2 rows appended (a JTAI
stop-fill and a GNE profit-take) that never reached disk. The 10:36 run self-healed via
broker-derived fill discovery and order-id dedupe, exactly as designed — zero trading impact, since
the ledger never informs decisions. Fix: verified read-back with absolute path, retry, and loud
failure.

**2026-07-22, two authorities in conflict** (commit `4d374ed`). A run reversed itself **three times
in a row** over the read-back: *"Actually the instruction says verified by read-back after write…
/ Actually, the Edit tool says 'no need to Read it back' since it tracks file state. But the
routine says I should verify. / Wait, I'll verify via bash since it's quicker."* This is a distinct
bug class: not a spec gap, but the routine's rule conflicting with the harness's own guidance. The
resolution matters — the harness hint answers "did the tool call succeed", while this check exists
because a *successful-looking* edit is precisely the 2026-07-15 failure mode. The routine now
mandates one exact `grep -c` so no run re-litigates it, and the user's own steer was that the
agent's instinct to use bash "since it's quicker" was right.

## FIRST — profit-take ordering

**2026-07-16, JFIN.** A profit-take sell reviewed before cancelling the stop bounced with
`EQUITY_MAX_SELL_SHARES_EXCEEDED` — the resting stop reserves the position's whole shares.
Cancel-first is mandatory, not stylistic.

**2026-07-27, MGNX — the confirm-cancel step was removed on purpose. Do not restore it.**
The routine used to cancel, re-fetch the stop to confirm `state == cancelled`, then review, then
sell. Measured against the broker's own timestamps on the +$10.60 MGNX profit-take: stop cancelled
`13:39:54.169Z`, sell placed `13:40:22.385Z`, sell filled `13:40:22.497Z`. **28.2 seconds between
cancel and placement; 112 ms of actual execution.** The entire delay was agent round-trips — three
of them, at roughly 9 s each — and it cost about $0.81 of drift on 81.4 shares (~7% of the gain),
the quote having been ~$3.84 at the decision and $3.8301 at the fill.

The re-fetch was redundant *because of* the JFIN finding above: a sell reviewed while the stop
still holds the shares bounces at review. So a clean `review_equity_order` already proves the
shares are free, which proves the cancel took effect. The re-fetch established nothing the next
call did not, and it lengthened the window in which the position sits with neither a stop nor a
sell — removing it makes that window shorter, not longer. The alert path is still handled: an
alert on share count means the cancel has not settled, so wait ~2 s and re-review once.

Loading `cancel_equity_order` / `review_equity_order` / `place_equity_order` moved into Step 1 for
the same reason — a tool-load turn inside the exit sequence is another ~9 s of drift, and by then
the gain that justified the exit is already stale.

**Not available: native OCO.** Investigated 2026-07-15 — the broker allows one open sell per share,
so a resting sell-limit would displace the resting stop, trading bounded missed profit for
unbounded unprotected downside. Rejected by Alexander ("it's best to be protected"). A one-cancels-
other order would remove this dance entirely; do not relitigate until the broker offers one.

## FIRST — stop-coverage audit

**Week 1 (~2026-07-08).** A stop silently expired overnight and a position fell **49%** while
believed "protected". This is the incident the entire audit exists for.

**2026-07-16, SXT.** A run queried orders with `state=open`, concluded SXT's stop was missing, and
attempted a duplicate repair. Robinhood holds live stops in state `confirmed`, which the `open`
filter silently hides — only the broker's share-reservation rejection stopped the duplicate. Query
by symbol, never by `state=open`.

**2026-07-29, stop-quantity coverage gap (P1 review finding).** The audit called any confirmed or queued stop active regardless of quantity, so a stale partial stop could leave most of a position naked while still passing. No loss was attributed; the review caught it before one. The audit now sums valid distinct active-stop quantities against `floor(position quantity)`, supplements only a shortfall so existing protection stays in place, and halts new entries when stop quantity data is malformed or over-covered.

## Step 6 — scan schema and the oversized-result file

**2026-07-20.** The 08:06 and 08:36 runs each independently rediscovered the scan result's JSON
shape, guessing `instruments` / `cells` before landing on `data.result.results[]` — burning
commands and tokens and risking a live mis-parse, on the same day production testing began. Fixed
with the checked-in `filter_scan.py` (commit `224b125`), which documents the schema once.

**2026-07-30, scan-handoff formatting gap (P1 review finding).** The routine then treated the script's formatted stdout table as its working list. That presentation rounds Last to 3 decimals, relative volume to 2, and volume to 0, yet the next step multiplies volume by price; it also makes headers and spacing part of trading state. No loss was attributed; the review caught it. The routine now writes a fresh per-run `filter_scan.py --json-out` file, consumes only its validated JSON fields, and skips entries rather than falling back to stdout or stale output when that handoff fails.

**Windows path in a Linux sandbox.** The harness replies with a `C:\Users\…` path for oversized
results; past runs corrupted it while retyping. Locate by basename with `find` instead.

**2026-08-07, inline scan transport truncation.** The 11:01 run's broker scan succeeded with 283
matches and every required visible column, but entry still failed closed as `scan handoff failure`.
The complete result was roughly 155 KB and remained inline: no scan source file or
`working-list.json` was ever created. The immediately preceding 10:34 run, under the same rules,
persisted a 155,220-byte scan envelope and filtered it successfully. This proved the broker,
saved-scan schema, and deterministic filter were healthy; the missing boundary was transport from
the successful tool call to disk. The earlier envelope fixes only help after complete JSON reaches
the filter. Inline scan capture must therefore compose the single `run_scan` call and verbatim
file-change persistence atomically, before any model-visible text/yield can truncate the result,
and expose only a compact path/byte-count receipt. Absence of an auto-created file or a visibly
named Write tool is not evidence of failure; only an attempted persistence/read/filter failure may
produce `scan handoff failure`. Never rescan to repair this boundary.

## Step 8 — the script owns the math

**Week 1.** A live run multiplied a share volume by the **date string**, twice, before getting it
right. That is why all entry math lives in tested Python and why a script or handoff failure must
block entry rather than reopen an ad-hoc calculation path — a wrong index that doesn't crash
silently corrupts every entry decision.

**2026-07-30, evaluator-handoff and short-history gaps (P1 review findings).** The routine wrote
candidate-evaluator JSON but still told the agent to use formatted stdout, manually repeat some
of the math, and fall back to ad-hoc code if the evaluator failed. Its pre-RSI and final passes
also shared the persistent output path, so a failed final pass could leave an RSI-disabled
artifact where the complete gate record was expected. Separately, the evaluator merely labelled
an abbreviated bar history and could still approve a high-volume one-bar name using that smaller
sample. No loss was attributed; the review caught both paths. The pre-RSI pass now writes a
transient, validated JSON handoff; only a validated final RSI-enabled JSON result can authorize
entry or become the persistent gate record. Evaluator or handoff failures skip entries, and fewer
bars than either configured history window blocks the name before any candidate math runs.

**2026-08-06, evaluator transport-envelope mismatch.** Two valid historicals results were saved
in the standard MCP `content` + `structuredContent.data.results` envelope, but
`evaluate_candidates.py` accepted only the inner `data.results` payload. The run called
`get_equity_historicals` twice again and created corrective `-raw.json` files, adding roughly
90 seconds; the refetched data happened to match, but a second live snapshot could drift. The
evaluator now unwraps exactly one object-valued `structuredContent` layer, rejects reported or
malformed tool errors, and the routine forbids extraction, corrective copies, or refetches.

**2026-07-22, unasked-for judgment** (commit `2b4ffff`). A run remarked *"the bars don't have
explicit interpolated fields, so I'm treating them as real data points"* — reasoning about a
decision `evaluate_candidates.py` already owns. **Investigated and there was no bug:** the script
uses `b.get("interpolated")`, so a missing field is falsy and the bar counts as real, exactly what
the run concluded. Verified live that absent interpolated fields are normal, not API drift: 84 bars
across the four thinnest names of that run, zero interpolated fields, because with the default
`regular` bounds the API omits session gaps rather than padding them. The interpolated bars the
protection was built for (a FISN IPO placeholder at a flat $16.00, zero volume, which would have
invented a fake dip) are pre-listing padding. **The agent's answer was RIGHT and the fix was still
worth making** — a judgment the model makes correctly today is one it can make wrongly tomorrow.

**2026-07-22, `spawn E2BIG`** (commit `aa1edee`). The 09:07 run had 13 candidates' daily bars to
save and embedded that JSON into a shell command, blowing the OS argument limit. **The non-obvious
detail worth keeping: a heredoc does NOT fix this.** The entire bash command text is passed as one
argument to `bash -c`, so `cat > f << 'EOF'`, `echo`/`printf` redirection and `python3 -c "…"` all
fail identically once the payload is large — the limit is on the command, not on how the data is
quoted inside it. The data was already sitting in a harness-saved file the whole time; the routine
just never told the agent to use it.

**2026-07-28, the RSI map was the one place a run still had to retype data.** The 11:06 run failed
with `json.decoder.JSONDecodeError: Expecting ',' delimiter` — it had hand-assembled
`{"INLF": {"data": {... "indicators": [{"series": [...]}]}}}` and dropped the `]` closing the
`indicators` array. It then read `evaluate_candidates.py` mid-run with `sed` (which this document
forbids) to work out the expected shape, and recovered by rewriting in a simpler form.

**The cause was this document, not the run.** Step 10 said *"save the responses into a scratch JSON
map (SYMBOL → response)"* — and a symbol-keyed map is something that can only be built by hand.
Meanwhile `--bars` had taken N raw response files and read the symbol out of each ever since the
E2BIG fix, so the historicals path had been transcription-free for six days while the RSI path
still required assembling nested JSON by eye. The rule "never hand-transcribe tool data" was
applied to one of the two and not the other.

Fixed by making `--rsi-file` behave exactly like `--bars`: `nargs="+"`, and a file whose
`data.symbol` is present is keyed from that. Responses are now saved verbatim. The symbol-keyed
map still parses, so older invocations keep working.

**Worth generalising: if a rule exists because retyping data is dangerous, audit every path that
takes data, not just the one that failed.** The E2BIG fix looked complete because the failure it
was written for could not recur; the identical hazard simply moved to the input nobody had broken
yet.

**Same day, 12:36 — the residue of the same hazard, and where it finally bottoms out.** The run
followed the new rule correctly: three RSI responses, three separate files, no keying. One of the
three (`rsi_NVA.json`) gained a single extra `}` at the end. Nothing distinguishes that file from
the other two — each `Write` is an independent generation event, and "save verbatim" has NO
mechanical enforcement for inline responses: the Write tool's content is re-generated by the model
token by token; this harness has no copy operation from context to disk. So the per-file error
rate cannot be pushed to zero by instructions alone. (Improvement to note: 11:06 crashed mid-script
and read forbidden code to recover; 12:36 caught its own error BEFORE running anything, fixed the
file, ran clean — and then bought AVIR, the first entry through the completed wall.)

**2026-07-29, 12:01 — third occurrence, and the instruction was the cause.** Same defect again: an
extra brace, this time in both RSI files, plus a separate typo in a long UUID-laden output path
(`…8dea34` written as `…8dea94`). The pre-run `json.load` validation added the day before CAUGHT
it before the script ran, so the cost was a rewrite rather than a crash — the guard worked.

**The realisation that closed it: "save the response verbatim" was never protecting the values.**
There is no copy operation in this harness; the agent regenerates every token of the file either
way. Asking for the raw nested response therefore made the numbers no safer and simply added ~200
tokens of structure to get wrong on top of them. Across three failures, 3 were structural and 0
were value errors.

So the routine now asks for the smallest thing the script actually reads — ONE file, a flat map of
symbol → array of RSI numbers, e.g. `{"EMAT": [41.6, 40.2, 39.1]}`. `begins_at`, `params`,
`bounds` and the long `guide` string were pure transcription surface and are never read. One file
also means one path to mistype instead of several, which addresses the path typo in the same
change. No script change — `load_rsi_map` already accepts a bare list per symbol.

**This is not a reversal of the raw-file fix above.** That one removed hand-assembly of *nested
responses*; nesting was the failure, not keying. A map of flat number arrays is a different
object, and the script still accepts raw responses for anything that already passes them.

The fix is procedural, and a rejected alternative is worth recording. The routine now requires
validating every authored JSON with one `json.load` command BEFORE the script runs — one cheap
pre-check covering every malformation, with "re-Write that file" as the recovery. A lenient
trailing-garbage parser was built first and **rejected the same day**: replaying the actual NVA
bytes showed the extra `}` sits BEFORE the `]` closing the indicators array — mid-document, not
trailing — so the leniency covered only a failure shape that has never occurred, while adding a
soft-accept path to a trading pipeline whose stated bias is fail-loud. `load_json` in the script
is now deliberately strict and its docstring says why; a regression test pins the exact NVA bytes
as must-fail-loudly so a tolerant reader cannot sneak back in.

**Also from the 12:36 run, pinned before it gets improvised twice: the broker rejects sub-penny
stop prices.** The computed stop $4.2039 was refused at review; $4.20 passed. The routine now says
round DOWN to 2 decimals — a down-round can only widen the stop by a fraction of a cent, an
up-round tightens it past the designed level.

**2026-07-24, inline responses and the scratch-location conflict** (commit `f07b961`). The 07:37
run — the first ever to traverse the whole entry path — hit two gaps at once. (1) Historicals came
back **inline** (5 candidates × 26 bars), so the "use the harness-saved file" route did not exist,
and the run re-read the routine three times reconciling "use the `Write` tool" against "save
UNMODIFIED". They were never in conflict: the ban is on retyping or restructuring bars, not on
copying a response whole. (2) A genuine contradiction — "all scratch in a sandbox `mktemp` dir" and
"author scratch with the `Write` tool" cannot both hold, because the Write tool writes through the
harness on the Windows side and cannot reach a Linux sandbox path. The run improvised the session
**outputs directory**, which was the right answer and is now the rule.

The 08:07 run hit the same gap and deliberated much longer, at one point talking itself toward
re-trying the E2BIG failure: *"the historicals file is about 40KB … which should fit within the OS
argument limit"* — size estimation softening a NEVER. It backed off on its own, but that branch
only looked attractive because every legal route appeared blocked. The 08:37 run picked the worst
option and wrote `scratch_cand_*.json` into the project folder. **Three consecutive runs, three
different answers to the same unspecified degree of freedom.**

## Step 10 — the RSI curl-up entry gate

**2026-07-15, why the gate exists.** Entries fired on DEPTH ALONE with no signal the fall had
bottomed: 4 of 5 fresh dip-buys knifed out the same morning (PLSM 6 min, SKYQ 26, UBXG 25, OPTH
59). Backtested against the week's three knives using historical RSI at each exact buy moment, the
gate blocked 2 of 3 (UBXG "never oversold" — a collapsed pump; JTAI "still falling"), avoiding
$54.83 of $72.39 in damage. PLSM passed because it was bought 13 minutes after the open — which is
why `NO_BUY_FIRST_MINUTES` exists alongside it.

**2026-07-24, the one-bar curl bought three knives** (commit `cc2df7e`, `RSI_CONFIRM_BARS` 1 → 2).
The 08:37 run bought four names; three stopped out within 46 minutes — GRDX in **61 seconds** (the
bid had already reached the stop level when the stop was placed, so it filled on arrival), EXYN in
40 minutes, LODE in 46 — tripping `STOP_COUNT_HALT` and benching the day at −$37.90 realized.
LODE's RSI ran 45.4 → 41.4 → 12.2 → 11.0 → 9.39 → **9.88**: freefall into single digits, then a
+0.49-point uptick that satisfied `confirm=1`. The same names had been correctly blocked as "still
falling" by the 07:37 and 08:07 runs; one flat half-hour unlocked them. **For a sustained knife, a
1-bar confirm is a delay, not a filter.** Replayed at `confirm=2`, all three losers are blocked and
the day's one survivor (MGNX, two rising bars) still passes — honest caveat, n=4 in-sample.

**2026-07-27, BIYA — the mirror image of the falling knife** (`RSI_MAX_ENTRY` = 60). The first buy
made under the new entry wall stopped out in **six minutes** for −$11.48. The gate record explains
it in one line: RSI ran `24.6 → 24.3 → 24.3 → 68.9 → 75.6 → 76.6` — a **44-point leap in a single
bar** — and the gate passed it, correctly by its own rules. `RSI_OVERSOLD` is tested as a *minimum
over `RSI_LOOKBACK_BARS`*, so the oversold touch sat four bars back at the edge of the window,
already fully reversed, while the current reading was 76.6 and rising. Both conditions were
satisfied literally by a name that was overbought and whose move was spent.

Liquidity ($5.7M median) and spread (0.53%) were genuinely fine — those gates were not at fault.
The fix is a ceiling on the CURRENT value: `confirm` stops you buying too early, and nothing had
stopped you buying too late. Separation is wide rather than fitted — BIYA at 76.6, MGNX (taken for
+$10.60) at 35.9; any cap between 50 and 70 splits them.

**This is also the case the persisted gate record was built for** (2026-07-26, `ba01864`). Alexander
asked for it so that *"we can research how a bad trade made it through our wall of decisions"* — the
first bad trade arrived the next session, and the record answered it without re-fetching anything.

**Standing constraint (Alexander, 2026-07-24): no depth-based exclusions, ever.** A proposed
`DIP_ENTRY_MAX_PCT` cap was rejected — "I want this agent to be good at buying all dips."
Knife-filtering must come from TIMING signals (has the fall stopped?), never from excluding depth.
Success bar: LODE-like same-session knife-outs filtered ~90%. Remaining timing-side levers if
`confirm=2` underfilters: minimum curl magnitude, an RSI recovery floor, a confirmation bar,
volume-capitulation. The backlogged per-name trend filter conflicts with this constraint and is
parked until 2.0, where it becomes a selectable skill criterion rather than a platform rule.

## Connector failures

**2026-07-23.** `get_equity_positions` returned "The connector's server isn't responding." The run
handled it well — it cross-checked the successful `get_portfolio` (`equity_value = $0` ⟹ no
positions, a sound one-directional inference) and retried once — but stated its conclusion
*before* the retry, making the retry decorative, and the run report showed a completely clean run
with no mention of the failure. It was safe because the account happened to be flat: with
`equity_value > 0` and a dead positions call, holdings exist that cannot be audited for stop
coverage. **Safe by account state, not by spec.**

**2026-08-11, invalid connector authorization omitted every Robinhood tool.** Twelve invocations
from 06:05 through 11:12 Pacific completed deterministic startup but received none of the required
Robinhood MCP tools, so even `get_accounts` could not be attempted. Codex's internal logs recorded
OAuth refresh `invalid_grant`, said reauthorization was required, and omitted the not-ready MCP
server. The runs safely failed closed before broker activity, but the visible coordination halt
named only missing tools and gave the operator no recovery action; scheduled attempts repeated
until the connection was reauthorized.

**Rule produced:** absence of the required Robinhood tool surface is a pre-broker account-scope
halt, not a retryable connector call or an empty-account result. The visible halt must recommend
reauthorizing or re-creating the Robinhood MCP connection, restarting Codex, and verifying that a
fresh task exposes `get_accounts` before scheduled trading resumes.

## REPORT — halted-run discipline

**2026-07-13.** Identical halted runs ranged 13.5K–50K tokens (~3×) purely from improvised
thoroughness. Guard/breaker/gate halts now emit a fixed minimal report (commit `a4b5e75`).

## REPORT — the closing file-card line

**2026-07-17.** Markdown links to local files render in the transcript but are **not clickable** —
verified with both project-relative paths and `file:///` URIs. Plain text only.

**2026-07-22, followed to the letter and still failed** (commit `5088538`). The earlier rule said
name the report "in plain text, NOT a markdown link". The 09:07 run wrote ``Report saved to
`run-reports/rhmra-log-….md` `` using a **code span** — not a link, so it complied with the letter
— and still lost the file card, leaving the user to hunt for the file by path. The 14:27 run used
the plain closing line and got its card. Three properties differed simultaneously (code span vs
plain text, path prefix vs bare filename, followed by a summary vs being last), so the true trigger
is not isolable; the fix mandates all three. **Position is the leading candidate:** every run that
worked *closed* with the naming line, and the day's most expensive run (~95K tokens) still got its
card, so this was an ordering deviation rather than adherence degrading under load. The 15:30 run
later showed backticks inside the `<run-summary>` block with the card still rendering, which
further supports position.

**The lesson, stated exactly: an instruction that enumerates ONE forbidden form implicitly permits
every other wrong form.** Specify the required shape, not the prohibited one.

**2026-07-27 — the three-property theory above is FALSIFIED as sufficient. Do not re-derive it.**
Two runs that day closed with byte-identical line shapes — last line, bare filename, unformatted
text, all three properties satisfied — and only one produced a card:

    12:37  Output file — open from the file panel on the right: rhmra-log-2026_07_27-12_37.md (run report)   -> CARD
    13:07  Output file — open from the file panel on the right: rhmra-log-2026_07_27-13_07.md (run report)   -> NO CARD

Both reports were complete on disk (8,365 and 3,481 chars), so nothing failed in writing. The
difference was a discrete `Presented file(s)` step in the transcript, present in the run that got a
card and absent in the one that did not. It does not expand into a tool call, so it is the harness
surfacing a file rather than something the agent invokes.

Across four runs checked that day the trigger tracked one thing exactly: **whether the run READ the
file back after writing it.** 11:37, 12:07 and 12:37 all show "read a file" in their step summary,
all three presented, all three got cards; 13:07 shows no read, no presentation, no card. Hence the
read-back rule now in the REPORT section — which also earns its place independently as a
persistence check, so it stands even if this mechanism is later shown to be something else.

**Standing caution for whoever reads this next:** this question has now produced three confident
diagnoses that were each incomplete (`250e979` "not a markdown link", `5088538` the three
properties, and before those a claim that script-created files could not get cards at all, which
Alexander disproved with a screenshot). Every one was inferred from correlation on a handful of
runs, and n here is 4. Write rules around the ACTION to take, not around a theory of what the
harness does with it.

**Also disproved here:** the claim that a script-created file cannot get a transcript card. The
user produced a screenshot disproving it. Cards come from *naming the file in the closing message*,
regardless of who created it.

## REPORT — write with the tool, never a shell

**2026-07-16.** PowerShell `Get-Content`/`Set-Content` round-trips and shell redirection mis-decode
UTF-8: em-dashes and ✓/🔔 came out as mojibake across `constants.md`, costing a cleanup commit
(`fbdc579`). Use the runner's file-change/edit facility for every human-authored project artifact
a run creates; the visible tool does not need to be named literally `Write`. This report/status
rule does not establish provenance for raw broker responses, whose stricter transport limits are
covered by the daily-loss incidents below.

## DASHBOARD P&L — unexplained one-cent differences

**2026-08-03 through 2026-08-04.** The account card showed broker realized P&L of **$16.55** while
the strategy table showed **$16.54**; the next day the same disagreement appeared as **$21.29**
versus **$21.28**. Both displays looked exact, so even a one-cent disagreement made the financial
dashboard look untrustworthy.

**Root cause:** the strategy side had been derived from rounded position-average values and mixed
floating/display arithmetic rather than the exact acquisition executions in the local trade
ledger. For example, a basis rounded from 2.6299 to 2.6300 changes a fractional-share sale by more
than a cent before display rounding. Robinhood's account-wide realized figure also has a different
scope and remains authoritative; agreement must be demonstrated, not manufactured.

**Rules produced:** `ledger_pnl.py` is now the sole strategy-P&L calculator. It chronologically
reconstructs matched acquisition pools from exact base-10 execution quantities/prices, applies one
documented per-fill half-away-from-zero cent policy, and exposes sanitized integer cents to both
desktop and phone views. Missing or unmatched basis is visibly incomplete/estimated, never zero.
Agreement is quiet; only a real broker/strategy difference or incomplete comparison raises a
warning, and the broker figure remains labeled as authoritative.

## VIEW ON PHONE — OAuth, Drive downloads, and durable local state

**2026-08-03 through 2026-08-04.** The first Google callback ended at **Google Drive was not
connected** because the selected Desktop OAuth client required its generated client credential
during token exchange even with PKCE. After authorization worked, the phone could remain at
“Loading the latest encrypted snapshot” because media downloads used the Drive metadata host
instead of the dedicated download endpoint. Pull-to-refresh then exposed another rough edge: the
verified dashboard disappeared behind setup/reconnect UI, and a forget/re-pair cycle could fail
while replacing local pairing storage.

**Rules produced:** the normal end-user path now uses a maintainer-operated, stateless OAuth relay;
the Desktop secret stays in the relay's encrypted deployment secret and users need no downloaded
OAuth JSON, Cloudflare account, API key, or public laptop server. Authorization uses S256 PKCE and
one-time state, Drive access is limited to the user's hidden `appDataFolder`, and snapshots remain
AES-256-GCM ciphertext until decrypted on the paired device. The Drive client uses the media
download endpoint. The installed phone app retains its non-extractable pairing key and last
verified dashboard across polling refreshes and page reloads, shows a disconnected/reconnect state
without erasing that dashboard, and requires the same Google account on both sides because each
account has a separate app-data area. Forget/stop/disconnect actions are separate and explicit.

**Release lesson:** moving Google's audience to **In production** removes the test-user allowlist;
it does not by itself substitute for any separate branding/scope verification. A release smoke
test therefore uses an account that was never a test user and exercises connect, pair, download,
pull-to-refresh, reconnect, stop, disconnect, and forget behavior.

## DAILY LOSS — broker snapshot persistence and invisible invocations

**2026-08-03 through 2026-08-04.** Seven visible runs showed `halted`, but all seven were
snapshot/file-handoff failures rather than genuine daily-loss trips. The three examined August 4
halts were concrete shape/persistence failures: 08:05 supplied a positions page whose `data`
was not an object; 11:03 could not initialize the scratch writer and had no usable raw snapshots;
12:02 supplied an orders page whose `data.orders` was not an array. The breaker correctly failed
closed and placed no new order, but a generic HALTED label made infrastructure trouble look like
strategy risk.

Two more invocations were invisible: the 07:32 run spent about 42 minutes trying to capture roughly
130 orders and then lost its lease, and the 09:02 run spent about 32 minutes before losing ownership
to 09:33. Neither reached the status-snapshot writer, and the dashboard timeline read only status
filenames. The absence looked like a scheduler gap even though the scheduler had started work.
The 11:32 run completed at 11:44:59, proving the later 12:02 halt was not caused by overlap.

**Root cause class:** raw MCP responses crossed a fragile context-to-disk boundary. A model-authored
JSON copy, a session Node writer, or an unverified scratch path could truncate, re-key, wrap, or fail
to persist a response. Retrying only the visibly bad page would also create a mixed-time breaker
snapshot. Separately, using an account-state status file as scheduler telemetry guaranteed blind
spots whenever a run stopped before the final refresh.

**Rules produced:** `broker_snapshot.py` now preflights the real session scratch directory and
deterministically unwraps, semantically validates, atomically stages, reads back, hashes, and
cursor-seals every breaker input. The preferred source is the harness's own tool-result file; the
intended fallback is one complete untouched response carried by the runner's file-change facility.
Any failure discards the entire generation and permits one wholly fresh generation—never a page
repair or cross-generation mix. `run_lifecycle.py` records every invocation before configuration
or broker access in an append-only journal and publishes a strict safe projection, letting the
dashboard distinguish real risk halt, snapshot failure, overlap, lease loss, configuration halt,
coordination halt, and final-status unavailability without exposing account or credential data.

**Superseded on 2026-08-13:** the source-selection rule above is retained as incident history, but the runtime no longer prefers a harness tool-result file or permits a fallback transport. The first successful real `get_accounts` response now binds one native-temp `SOURCE_ROOT` and one file-change method for the entire invocation; deterministic consumers reject every other source root.

**2026-08-04 22:35 follow-up.** A manual closed-session run reproduced `snapshot failure` even
though every Robinhood read succeeded. The run searched for an automatically mirrored tool-result
file, invented two nonexistent portfolio source paths, and then passed `--request-cursor FIRST` to
portfolio staging (a flag valid only for positions/orders). It also performed the entry-only daily
loss guard despite an already-consulted START CLOCK that said `session: closed` and
`entry_session_open: false`. At 22:35 Pacific it was 01:35 Eastern the next day; after-hours had
ended at 20:00 Eastern, so the truthful neutral label was `market closed`.

**Mitigation implemented (unit-tested; live scheduler validation pending):** deterministic
entry-impossibility gates now run after FIRST's
position-safety work but before SECOND. A closed-session, regular-hours-policy, blackout,
low-buying-power, or SPY-blocked run skips the daily-loss and stop-count entry guards, records them
as not evaluated, and completes normally. Eligible runs preflight a static file-change
response-source probe, may not guess or search for a result path, and must omit pagination flags
for portfolio/quotes. That probe proves
only that the selected external source path can be written, read, and parsed; it cannot prove that
a later full broker response crossed the harness boundary byte-for-byte. The runner also still
needs a writable external source area. Semantic staging, hashing, the two-generation retry, and
tomorrow's end-to-end automation run remain the defenses for that residual tool-surface risk. The
dashboard maps a completed structured closed session to `market closed` while retaining red
`snapshot failure` for a genuinely eligible run whose breaker capture fails.

**Superseded on 2026-08-13:** the static probe and residual writable-source-area rule above are historical. The real sensitive `get_accounts` canary now performs the sole transport test, and its invocation-bound native-temp root is reused mechanically for broker, scan, and historical inputs; there is no later probe, guessed path, alternate writer, or transport retry.

**2026-08-07 15:35 recurrence.** An eligible scheduled run constructed one generic staging
wrapper that unconditionally appended `--request-cursor FIRST` for every response kind. The
portfolio helper correctly rejected that pagination flag, so generation A was discarded before
its final position/order staging. Generation B restarted from fresh broker calls and completed
successfully; the lifecycle completed, the account remained flat, and there were no orders,
cancellations, intents, ledger writes, notifications, or financial impact. The mistake still
wasted generation A and consumed the only whole-generation retry.

Because the prose-only prohibition had now failed twice, the routine no longer describes one
generic stage command. It provides separate literal templates for portfolio, quotes,
positions/orders pages, and positions/orders aggregation; defines `FIRST` only as the synthetic
initial request cursor for positions/orders; requires an argv check before non-paginated staging;
and forbids a polymorphic cursor-bearing wrapper. Regression tests verify the command matrix and
that both portfolio and quote staging reject either pagination flag. The deterministic helper
remains strict; weakening its rejection would hide orchestration mistakes and compromise snapshot
provenance.

**2026-08-13 11:34, harmless probe passed but the real sensitive save was denied.** Generation A
wrote and validated a tiny synthetic probe under the Codex visualizations directory, then called
`get_portfolio`. The same file-change facility could create a file at that location, but policy
replaced the sensitive portfolio payload with an `isError: true` denial envelope because a
visualization path is not trusted for brokerage data. `broker_snapshot.py` correctly rejected the
envelope. An unrelated `TextEncoder is not defined` experiment then obscured the real failure.

Generation B repeated another harmless probe in that same irrelevant directory, but wrote its real
responses under the native runtime `%TEMP%` directory instead; those files staged successfully and
the run completed coherently. The account remained flat, every candidate was rejected, and no
intent or order was created. The recovery nevertheless duplicated one discovery portfolio call,
used two probes, and added roughly 1 minute 43 seconds between the failed A capture and B capture
(about 2 minutes 9 seconds including the first probe). This was destination-sensitive policy, not
a Robinhood response-shape problem and not an OS filesystem ACL failure.

**Rules produced:** a harmless probe cannot prove that the same destination will accept sensitive
brokerage content. Startup now uses the first successful `get_accounts` response itself as the one
real save canary and account-resolution source. The complete unchanged response is written once as
the sole entry in a unique native-temp `SOURCE_ROOT`; `broker_snapshot.py bind-transport`
validates it, privacy-deletes it, permanently records that the invocation spent its one binding
attempt, and atomically binds that exact resolved root plus a helper-owned identity marker to the
invocation scratch ID. A failed first attempt cannot be retried. Every later broker, scan,
historicals, quote-map, and JSON handoff must reuse the same file-change facility and a fresh
direct-child filename in that root; the deterministic consumers mechanically reject an alternate,
nested, symlinked, replaced, or unbound root. A later denial, missing file, or path/method mismatch
is terminal—no save retry, path experiment, visualization fallback, or generation B. Generation B
remains available only when a fully persisted generation A later fails semantic or coherence
validation, and it must reuse the startup-bound transport. Native Local temp directories can
persist after a run; the routine neither falsely claims they evaporate nor improvises risky
recursive cleanup.

**2026-08-13 13:33 follow-up, the real canary succeeded but account scope was discarded with it.**
The first successful `get_accounts` response was saved in the bound native-temp source root, and
`bind-transport` correctly validated and privacy-deleted it. The runner had retained only the
file-change receipt—not the response data needed to resolve the configured account name. Because
the new contract also correctly forbade a second `get_accounts` call, the run halted fail-closed
after transport binding. It made no later broker call or order, created no normal report/status,
and released its lease and lifecycle cleanly. The transport protection worked; the account-scope
handoff was incomplete.

**Rule produced:** `bind-transport` now accepts the exact validated `AGENTIC_ACCOUNT_NAME`,
exact-matches it while strictly reading the saved canary, requires one agentic-enabled match, and
returns `account_name`, `account_number`, and `agentic_enabled` in its ephemeral success receipt
before privacy-deleting the canary. The run binds account scope only from that validated receipt;
raw/model-visible response data, narration, memory, and prior runs are never authorities. The
persistent attempt, transport, and source-root markers remain account-number-free. A missing,
duplicate, disabled, or malformed match is `coordination-halt` / `account-scope-failed`; a real
save/path/envelope failure remains `snapshot-failure` / `snapshot-write-failed`. Neither permits a
second save path or a second `get_accounts` call.

## STATUS SNAPSHOT — deterministic publication and dashboard fallback

**2026-08-12, Claude authored three malformed final snapshots in eight completed runs.** The
08:13 snapshot replaced the required contract with a richer invented object, the 09:35 snapshot
omitted required `guards` structure, and the 10:13 snapshot omitted `schema_version` entirely.
All three files were syntactically valid JSON, so the routine's old `json.load` read-back accepted
them even though consumers could not interpret them safely. The dashboard correctly refused the
newest unsupported object, but it left account values from the previous render in the DOM beneath
the error banner. That stale display could be mistaken for the rejected run's current account
state. The eight completed runs and one correctly fenced overlap made zero broker write calls, so
the incident affected observability rather than orders or account state.

**Rules produced:** an LLM-authored status document is only a candidate. `status_snapshot.py` is
the schema and publication authority: it accepts a candidate only from the marked session scratch
directory, validates strict UTF-8/JSON, duplicate/non-finite values, exact keys, types, enums, and
cross-field semantics, then stages, flushes, reads back, hashes, and atomically publishes the exact
run-start filename without overwriting an existing file. A failed candidate may be rewritten and
submitted once; a second failure leaves every prior truthful snapshot untouched and closes the
lifecycle as `final-status-unavailable` / `status-write-failed` without attaching a status file.

The dashboard independently validates status files. It scans newest to oldest, renders the newest
valid snapshot, and shows a visible warning naming the rejected newest file and the fallback it
selected. If no valid snapshot exists, or client-side validation rejects the server response, it
clears account, position, freshness, rules, P&L, and the local pending phone-share view instead of
retaining stale DOM. Any previously uploaded strictly valid encrypted snapshot remains truthful,
ages visibly, and expires normally; rejected data never replaces it. A lifecycle invocation whose
linked status is rejected remains visible with a neutral
`status rejected` / `status unavailable` outcome; malformed account data never invents a risk-halt
label. Rejected files remain local and readable for incident review, but are never display data.

**2026-08-12 11:43 follow-up, Claude lost bound invocation state across context compaction.**
A native Claude Code Local run started lifecycle at 11:43:22 Pacific and bound START CLOCK at
11:43:42, but after automatic context compaction it began constructing report, gate, and status
artifacts with an 11:45 timestamp. It attempted unsupported lifecycle `status`/`export`
invocations to rediscover the start minute, switched from the resolver-bound Python 3.12
executable to `py -3` (Python 3.14), skipped the SECOND phase renewal, and issued parts of the
ordered final refresh in parallel. Its two `get_realized_pnl` calls omitted `asset_classes:
["equity"]`, so the connector rejected both. It first used a `git describe` string as
`rules_version`, then truncated that to a commit-like value that described the wrong rule era.

The deterministic lifecycle correctly rejected the 11:45 report/status names. Claude eventually
wrote corrected 11:43 files and finished the invocation, but only after it had released the lease;
the structurally valid 11:45 status remained as an unlinked orphan and was new enough to displace
the lifecycle-linked snapshot in the dashboard. Performance telemetry also used lifecycle finish
as the strategy end, recording 28:53 instead of the required FIRST-to-REPORT interval of 20:57.
The account was flat and the run made no order review, placement, cancellation, or other broker
mutation, so the incident affected coordination, observability, and timing telemetry rather than
account state.

**Rules produced:** lifecycle preflight now returns and binds the exact seconds-bearing run start,
artifact stamp, and expected report/gate/status names; a strict read-only
`run_lifecycle.py status --invocation-id` call is the sole recovery path after compaction.
`status_snapshot.py publish` and `verify` bind the candidate, timestamp, and expected filename
to that still-running invocation. The runner may neither round the run start nor switch away from
the resolver-bound interpreter. `rules_version.py` owns rule-era Git interpretation. Every phase
repeats its lease-renewal requirement locally; final broker reads are sequential; realized-P&L
uses one explicit equity/day/timezone payload and an identical retry; artifact corrections finish
before lease release; and strategy timing remains the FIRST renewal through REPORT renewal only.
The dashboard quarantines otherwise valid snapshots that cannot be linked to the lifecycle
invocation, preserving the newest truthful linked account state and showing an orphan warning.

**2026-08-12 15:10 follow-up, Sonnet 4.6 auto-compaction omitted retained execution state.**
During a native Claude Code Local market-hours run, automatic context compaction reduced the
working context from 167,326 tokens to 7,025 and took 2m04. The resulting summary omitted the exact
resolver-bound `PYTHON_EXE`. After compaction, Claude switched to literal `py -3`, invented an
unsupported daily-loss CLI action, mistyped one character in an already-valid scratch-file path,
and used `--help` probes to rediscover documented helper syntax. Those probes failed before making
any broker or durable-state mutation, and the correct daily-loss artifact was subsequently read.

Finalization also drifted from the prescribed order. Claude supplied START CLOCK instead of the
FIRST renewal as the strategy-start boundary, published status before writing the report, skipped
the mandatory report read-back, and called performance telemetry before reading and appending to
automation memory. The lifecycle, status, and performance stores remained structurally healthy,
the lease was released, the account remained flat, and there was no order review, placement,
cancellation, or other broker mutation. The incident affected instruction adherence, artifact
presentation, and benchmark timing quality rather than trading or state integrity.

**Deterministic hardening produced:** compaction summaries are not invocation-state authorities.
The resolver/lifecycle layer now owns a private active-context recovery receipt so a compacted
native Windows run can rebind the exact validated interpreter and lifecycle artifact names rather
than switch to `py -3` or guess. FIRST and REPORT write fixed, host-stamped lifecycle markers;
`run_performance.py` derives the strategy interval from the unique marker pair and refuses a
conflicting caller value. `status_snapshot.py` now requires and strictly reads back the exact
lifecycle-bound report before it can publish or verify status, so status cannot precede the report.
Automation memory is now disabled for this stateless routine, eliminating the contradictory
post-telemetry memory tool call and leaving `record-internal` as the final tool call. Missing or
ambiguous recovery/timing state produces an unavailable metric or a safe halt, never a
reconstructed value. This run remains excluded from fair comparisons.

**Positive validation from the 16:44 follow-up:** the deterministic guardrails did exactly what
they were designed to do. After context compaction, Claude attempted obsolete helper syntax, a
prohibited discovery probe, and incorrect relative report/status paths. Every invalid command was
rejected before it could publish, link, overwrite, or reinterpret authoritative state. Claude then
used the lifecycle-bound commands successfully; the report, status, gate, lifecycle, lease, and
performance records remained valid and mutually consistent, and no broker mutation occurred.
This is an important defense-in-depth success: model instruction drift became visible,
recoverable noise instead of silent state corruption or unintended trading authority.

**2026-08-12 16:44 follow-up, a successful daily-loss result was redundantly reopened through
interpolated Python source.** The deterministic evaluator completed successfully, wrote a valid
`daily-loss-A.json`, and reported a clear breaker. Claude then made an unnecessary diagnostic
`python -c` call whose source text embedded the native `C:\Users\...` path. Python interpreted
the path's `\U` as the start of a Unicode escape and rejected the command. The unchanged JSON read
successfully through the framework file reader immediately afterward, so the false error changed
neither the breaker verdict nor broker state.

**Rule produced:** calculation-mode `daily_loss.py` now emits its complete authoritative result as
one compact JSON object on stdout after the atomic output write. The routine consumes that object
directly and treats the scratch file as audit-only; it explicitly forbids a second file-read tool,
shell command, or Python command and therefore never interpolates a Windows path into `-c` source.

## PERFORMANCE TELEMETRY — completed run without an external run-duration observation

**2026-08-12 13:27, Claude completed normally but the dashboard showed its external reference
duration as `not measured`.** The run's internal performance record was healthy: Routine
total was 21:36, Strategy execution was 21:19, and Routine overhead was 0:17. The external
duration remained null because no human or runner callback submitted a source-specific
observation after the run. Successful lifecycle completion therefore guaranteed internal timing
but did not guarantee any user-visible approximation of the whole run.

**Rules produced:** `record-internal` now reuses its single append-time host-clock reading as the
end of a source-labelled `final-summary-boundary` measurement beginning at lifecycle-bound START
CLOCK. The helper returns the exact Pacific Run start and Run end plus Comparable run duration, and
the agent copies those values into the final on-screen Run Summary immediately after telemetry.
This measurement is observability only: it adds no clock call, broker call, lease action, lifecycle
event, saved-report rewrite, or status-snapshot field. A later explicit Codex, Claude,
runner-metadata, or manual observation is displayed as Reference run duration and remains
fallback/context while the automatic Comparable run duration is available. Fair model-performance
comparisons use the same automatic boundaries, session class, workload path, configuration cohort,
and preferably rules version, with runner/model identity as the explicit comparison dimension.
Neither label claims scheduler-start or task-completion boundaries.

**2026-08-12 18:40, a successful Codex closed-session run retained valid durations but all timing identity fields were `unknown`.** The lifecycle and performance records completed normally, including Comparable run duration, Routine total, Strategy execution, and Routine overhead. The person launching the run knew it was Codex, but the task input supplied no usable `TIMING_IDENTITY` declaration and no complete direct current-task metadata reached the routine. The prior fail-closed contract therefore discarded runner, model, and configuration together rather than guess. No trading, report, status, lifecycle, or timing value was wrong; only the benchmark cohort identity was unavailable.

**Rule produced:** immediately after the routine-file read returns and before subsequent launcher/helper/broker work, the running model now makes one structured self-report from only identity explicitly exposed by its current framework, using `unknown` independently for missing fields. After lifecycle start and constants validation—but before START CLOCK—the checked-in performance helper resolves that claim exactly once against a deterministic alias registry, applies direct-metadata → declaration → self-report → unknown precedence, and persists the invocation-bound result for finalization after any context compaction. It does not fuzzy-match a broad family to a specific model or invent a reasoning/effort setting. A selected partial or unrecognized self-report, an invalid stronger source, or a known-field conflict carries an identity warning; lower-source incompleteness does not disqualify valid independent metadata or a valid declaration when known fields agree. Every selected self-reported identity is unverified display fallback and remains outside primary fair-comparison cohorts unless a later independent source replaces it in the projection. Identity resolution is non-authoritative and a failure falls back to unknown without affecting the run.

**2026-08-12 19:15 follow-up, Claude exposed split identity evidence rather than one introspection API.** Operator-provided Claude screenshots showed that the exact model was known only because the current system prompt stated it, while the hosting process exposed runner/configuration evidence through the `CLAUDECODE` and `CLAUDE_EFFORT` environment keys. Claude correctly reported that no local Python script can introspect the serving model and that no exact-model environment key was present. Other environment details shown in the screenshots were unrelated and potentially sensitive; they are neither inputs nor telemetry.

**Rule produced:** the deterministic Python resolver now reads exactly those two allowlisted keys with `os.environ.get` and never enumerates or persists the environment. The routine/model must not inspect, relay, or pass environment values. Because inherited environment values are spoofable, they are corroboration rather than authentication. Runner/configuration may therefore carry `runtime-environment` field provenance while an exact system-prompt model remains `self-reported`; the aggregate becomes `composite` and stays outside primary fair-comparison cohorts. Schema v4 records each field's provenance and the dashboard shows it only inside the selected run's existing orange identity diagnostic. A complete synchronized `TIMING_IDENTITY` declaration remains the strongest symmetric source available to both Claude and Codex.

---

## The pattern across all of these

Most of these are the same bug class: **the spec didn't say, so the agent improvised.** One
(`4d374ed`) is rarer and worth distinguishing — two specs said different things. Every single one
needed a human to diagnose from run logs. The invocation lifecycle did self-report the August 4
`snapshot failure`; it did not diagnose the bad handoff or recognize that the entry-only work was
irrelevant in a closed session. That remaining observability gap should be stated plainly rather
than glossed over.

The design conclusion: writing a spec for an LLM agent is closer to API design than to
documentation. Every unspecified degree of freedom is eventually exercised, usually within days,
and often three different ways in three consecutive runs.
