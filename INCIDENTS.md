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

**2026-08-14 06:03, a model-recopied scratch path lost five characters before preflight.** The
first scheduled invocation of the day created a valid native-temp session directory whose random
suffix contained `de758`, then manually authored the path again for `broker_snapshot.py preflight`
without those five characters. The helper correctly rejected the different nonexistent path and
the lifecycle closed as a visible snapshot failure. This was not an ACL, broker, scan, or JSON
problem. No Robinhood call, scan, order review, placement, or cancellation occurred, and later
scheduled invocations completed startup normally.

**Rule produced:** scratch identity is now helper-owned reserved state. After lease and active-context
binding, one retained-interpreter `broker_snapshot.py preflight --create-scratch` command creates
the native-temp directory, performs the full write/fsync/read/strict-parse/remove proof, leaves the
marker, and returns its exact absolute path plus canonical scratch ID in one strict receipt. The
runner binds both only from that receipt and passes the retained values programmatically; it never
calls `New-Item`, `mkdir`, `mktemp`/`mkdtemp`, supplies a startup `--scratch`, or copies, shortens,
normalizes, reconstructs, or retypes a random path from text or memory. There is no alternate
directory or retry. A strict `scratch_create_failed` error remains coordination-state failure;
post-create `invalid_snapshot` or an unprovable/malformed failure remains the dedicated scratch
preflight snapshot failure.

**2026-08-14 12:02, Claude called unavailable `mark_chapter` before reading the routine.** The
model emitted a direct `mark_chapter` tool call even though that framework convenience was not
available. Claude Desktop rejected it immediately with `No such tool available`; no repository
helper, lifecycle state, broker operation, or artifact was touched. Claude then read the routine,
completed the authoritative lifecycle and broker-read path normally, published report/status
artifacts, released the lease, and made no order, cancellation, or broker mutation.

**Rule produced:** every maintained direct-run launch prompt unconditionally says that
`mark_chapter` is not part of the routine and begins directly with the routine-file read, with no
other model-authored tool call before that read completes. The routine mirrors the prohibition and
keeps `run_lifecycle.py` as the sole run-phase recorder. This launch boundary never branches on
`TIMING_IDENTITY`; missing, invalid, or unknown identity remains telemetry only.

**2026-08-14 12:41 follow-up, Claude attempted an invalid bulk `TaskCreate` after reading the
new boundary.** Claude passed one stringified array of seven workflow items through an unsupported
`tasks` parameter. The client rejected the call before execution because the actual singular
tool requires top-level `subject` and `description`, rejects `tasks`, and had not been loaded
through tool discovery. Claude did not retry. The authoritative run continued, completed with
valid report, status, lifecycle, lease-release, and performance records, and made no order review,
placement, cancellation, or broker mutation; the SPY red-day gate independently skipped entry.

**Follow-up rule produced:** category wording alone was insufficient because Claude did not treat a
framework task list as prohibited progress bookkeeping. Planning remains internal. Every maintained
direct-run prompt and the routine now name `TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`,
and `mark_chapter` explicitly, forbid discovering or loading them through `ToolSearch`, and ban
all framework planning/task-list/todo/chapter/progress/phase tools at every point. The prohibition
is runner-neutral and never depends on `TIMING_IDENTITY`.

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

> **Superseded Claude deployment guidance:** the substrate diagnosis and deterministic filesystem
> guard below remain valid, but the August 2026 runner-compatibility decision supersedes every
> instruction here to create, repair, test, or enable a Claude schedule. Keep legacy Claude tasks
> disabled and migrate this project to Codex.

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

**Historical follow-up rule (superseded for deployment):** pause the legacy task and create a new, uniquely named task through
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

> **Historical only:** the Claude task-creation procedure below is preserved as incident evidence,
> not current setup guidance. Do not follow it; the August 2026 compatibility decision requires
> Claude schedules to remain disabled.

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

**2026-08-25 06:33 and 07:33, Codex mistook deferred Robinhood tools for an absent connector.**
Both invocations completed deterministic startup, but Codex declared `get_accounts` unavailable
without consulting its `ALL_TOOLS` registry and closed as `coordination-halt` /
`account-scope-failed`. The same single active automation's adjacent 07:03 and 08:03 invocations
did consult that registry, found `mcp__robinhood_mcp__get_accounts`, and continued; the 08:03 run
completed its scan. The false halts made no Robinhood request or broker mutation and released their
leases, so fail-closed containment worked, but their authorization recovery message diagnosed a
connector failure that had not been established.

**Rule produced:** in Codex, a tool missing from the initially displayed namespace is not proof
that it is unavailable. Immediately before startup item 13, the exact composed operation filters
`ALL_TOOLS` for the canonical Robinhood `get_accounts` registration. One exact callable match ends
discovery and must be invoked; zero matches or one non-callable match enters the unavailable-tool
path, while duplicate exact matches fail closed without choosing. The retry reuses the same
captured callable and never repeats discovery. Skipped discovery alone never authorizes
reauthentication, connector removal, or an expired-authorization diagnosis. This supersedes none
of the 2026-08-11 real `invalid_grant` recovery rule; it adds the proof required before that rule
applies.

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

**2026-08-19 11:40, the staging vocabulary was applied to work that is never staged.** During the
PRE-RSI evaluation pass a run invoked `broker_snapshot.py stage-quotes` once and
`broker_snapshot.py stage-historicals` twice. All three exited 2 with `usage_error`, cost three
calls plus a re-read of the command format, and touched no broker or durable state. The mistake
was one category error with two symptoms: staging belongs to the FINAL STATUS REFRESH snapshot
generations, while Step 8-9 historicals results, RSI inputs, and quote maps are file-change
handoffs consumed directly by `evaluate_candidates.py` and are never staged at all. Because the
routine documented no staging path for historicals, the run invented `stage-historicals`, and that
invention then contaminated the adjacent quotes call into `stage-quotes` by symmetry. The
hyphenated shape is itself plausible: the helper really does expose `bind-transport`, so a
hyphenated compound action is a reasonable guess about a vocabulary that in fact takes the kind as
a separate `--kind` value.

**Rules produced:** the routine now states the closed set — `broker_snapshot.py` accepts exactly
`preflight`, `bind-transport`, and `stage`; the only staged kinds are `portfolio`, `positions`,
`orders`, and `quotes`; the kind is never hyphenated onto the action; and Steps 8-9 call this
helper for nothing at all. Because prose alone had already failed for the staging wrapper, the
helper also answers the mistake deterministically: any `stage-<suffix>` action is rejected before
argument parsing with a message naming the exact correct command, so `stage-quotes` returns
`use 'stage --kind quotes'` and `stage-historicals` returns the non-staged-kind explanation and
points at `evaluate_candidates.py`. Regression tests cover every staged kind's hyphenated form and
were verified to fail with the guard removed.

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
exact-matches it while strictly reading the saved canary, requires the connector's Boolean
`agentic_allowed` on the one match, and
returns `account_name`, `account_number`, and caller-relative `agentic_allowed` in its ephemeral success receipt
before privacy-deleting the canary. The run binds account scope only from that validated receipt;
raw/model-visible response data, narration, memory, and prior runs are never authorities. The
persistent attempt, transport, and source-root markers remain account-number-free. A missing,
duplicate, disabled, or malformed match is `coordination-halt` / `account-scope-failed`; a real
save/path/envelope failure remains `snapshot-failure` / `snapshot-write-failed`. Neither permits a
second save path or a second `get_accounts` call.

**2026-08-13 14:19 follow-up, the save succeeded but the serializer decorated the JSON.** The
composed Codex operation correctly called `JSON.stringify` on the complete successful
`get_accounts` result and wrote one canary in the bound native-temp source root, but it inserted the
literal six ASCII characters `\ufeff` before the opening `{`. This was not a UTF-8 BOM, Windows ACL
failure, write denial, or broker-schema failure; the strict reader correctly rejected byte zero as
invalid JSON and privacy-deleted the canary. Exactly one Robinhood call occurred (`get_accounts`),
with no portfolio, position, order, scan, placement, or cancellation call. The lease was released,
the lifecycle closed as `snapshot-failure` / `snapshot-write-failed`, the failure report and timing
were recorded, and the previous truthful status snapshot remained untouched.

The same forensic review found a second latent mismatch before another test run: the live connector
returns `agentic_allowed`, while the first account-scope helper revision expected the invented field
`agentic_enabled`. **Rules produced:** the routine now pins one exact composed serializer: save the
complete `JSON.stringify(fullToolResult)` object, parse-check it in memory, and give `apply_patch`
exactly the serialized object with no prefix, suffix, BOM, whitespace, fence, label, comment, or
object coercion. Every later broker/scan/historical handoff repeats that recipe and the same bound
root. The strict helper does not strip either literal `\ufeff` or a real UTF-8 BOM; either consumes
the one attempt and fails closed. Account selection now validates the connector's exact Boolean
`agentic_allowed` and preserves that exact caller-relative name in the receipt. Regression tests cover
both forbidden prefixes, deletion, permanent no-retry fencing, missing/wrong/false eligibility, and
account-free persistent markers.

**2026-08-14 13:41, Claude's real canary write was denied at the required root and it improvised a
nested fallback.** Startup completed through scratch preflight and the empty order-intent check, then
the invocation's one `get_accounts` call succeeded. Claude Desktop's unattended permission
classifier denied the file tool's write to the new direct child of native `%TEMP%`. Instead of
stopping, the runner created a different source directory one level below its pre-approved session
scratchpad and saved the response there. `broker_snapshot.py bind-transport` correctly rejected that
directory because it was not a direct child of the runtime temp directory. The lifecycle closed as
`snapshot-failure` / `snapshot-write-failed`, the lease released cleanly, and the previous truthful
status remained untouched. No position was examined, no scan or entry guard ran, and no order was
reviewed, placed, or cancelled.

> **Superseded Claude deployment clause:** the transport rule remains current, but the historical
> Claude permission instruction below must not be used to create or repair a Claude schedule.

**Rule produced:** source-root identity now follows the same reserved-state rule as scratch identity.
The retained `broker_snapshot.py preflight --create-scratch` operation creates both helper-owned
native-temp directories and returns their exact paths and IDs in one validated receipt. That receipt
is the sole path authority: the runner never creates, chooses, randomizes, copies, retypes,
normalizes, relocates, or replaces `SOURCE_ROOT`. The first successful `get_accounts` response
remains the single real sensitive save canary and may not be called or saved again. A Claude Local
scheduler must be granted narrow file-edit access to the helper-owned `rhmra-session-*` and
`rhmra-source-*` temp namespaces before its supervised test; that host capability is unconditional
and never selected by `TIMING_IDENTITY`. A denied canary write remains terminal, with no nested path,
alternate writer, second root, or generation B.

**2026-08-17 06:02–09:33, eight Codex schedules could call Robinhood but could not save the
accounts canary.** Every invocation completed startup through the helper preflight, and the first
`get_accounts` call itself succeeded. The immediately composed file-change operation then failed
to create its required direct-child JSON file in the receipt-issued `rhmra-source-*` root. Both
that root and the matching `rhmra-session-*` scratch had been created by `tempfile.mkdtemp` under
the command sandbox's Windows identity with a protected DACL granting no create right to Codex's
separate host file-change identity. All eight source
roots remained empty. No account binding, later broker read, scan, guard, order review, placement,
cancellation, or final status refresh ran; every invocation failed closed as `snapshot-failure` /
`snapshot-write-failed` and the previous truthful status remained untouched.

This was a real Windows cross-principal ACL mismatch, not Robinhood availability, malformed JSON,
account schema, or the August 13 destination-sensitive policy denial. Friday's helper-owned-root
change had no successful Codex acceptance run: the earlier successful Codex path let the host file
facility create its own root, while the post-change success used Claude's different permission
setup. The first Codex schedules on the new helper-owned contract exposed the incompatible
assumption that command-created native-temp directories were writable by the host file-change
facility.

**Rule produced:** `broker_snapshot.py preflight --create-scratch` remains the sole creator and
identity authority for both random direct-child directories. On Windows it now deterministically
prepares and verifies a least-privilege per-directory capability bridge: the runner's file-change
principals can create fresh direct-child payloads and the exact status candidate, the helper can
consume those writer-owned children, and the added cross-principal writer-only capability does not
inherit to helper-owned markers. Non-Windows owner-private temp creation is unchanged. The
runner/model never selects a path, runs `icacls`, repairs an ACL, broadens all of `%TEMP%`, or
substitutes a fallback. Capability
preparation failure stays pre-broker `scratch_create_failed`; the first real `get_accounts` save
remains the sole end-to-end sensitive-write canary, and a denial after that call remains terminal
`snapshot-write-failed` with no retry, probe, alternate writer/root, second account call, or
generation B. A supervised `DRY_RUN = true` acceptance must prove both the canary in `SOURCE_ROOT`
and `rhmra-status-candidate.json` in scratch for each runner separately before its schedule is
enabled.

**2026-08-20 13:03, Codex truncated the helper-issued `SOURCE_ROOT` during the save/bind
handoff.** Startup preflight correctly created and issued `rhmra-source-ygsx1eo8`, but the
composed accounts save and later `bind-transport` arguments used the shortened sibling
`rhmra-source-ygsx1eo`. The first `get_accounts` request itself succeeded and its complete response
was written under that alternate root. `broker_snapshot.py bind-transport` correctly rejected
`--source-root` because it did not exactly equal this invocation's helper-prepared root, consumed
the one binding attempt, and privacy-deleted the canary. Account scope was never established. No
position, order, scan, entry guard, review, placement, cancellation, notification, or final status
refresh followed; the permitted failure report was persisted and read back, the lease released,
and lifecycle finished `snapshot-failure` / `snapshot-write-failed`. This was a model transcription
defect after a valid helper receipt, not Robinhood downtime, malformed account data,
or a failure of the Windows capability bridge.

**Rule produced:** Codex now uses separate executor-local bootstrap and transport state. Bootstrap
first clears and stores the exact Python-resolver receipt as `launcher-bound`, extends that same
object with the exact validated-constants receipt as `configuration-bound`, then adds the exact
active-context receipt unchanged after lease binding and advances the surrounding bootstrap state
to `context-bound`. Every later operation loads the
Python executable, configured account name, invocation, and lifecycle
artifact names from those receipts; none may be copied from visible output. Every nested Windows
command explicitly selects `powershell.exe`, even if the outer environment exposes another shell.
Nested `exec_command.session_id` values are drained inside the still-running isolate with
`write_stdin`; only an outer `functions.exec` cell ID is continued with `functions.wait`, so a live
helper is never orphaned or duplicated.

Preflight loads that context, binds the native project root, clears the transport slot, validates
and stores the exact scratch/source receipt, and emits no random path. The startup accounts cell
then derives its canary and all bind arguments from loaded state, saves the first successful
complete response, and invokes `bind-transport` before any model-visible output. Its
`account-call-started` through `canary-saved` phases are non-retriable fences; successful binding
stores account scope and enters `transport-bound`, while failure records terminal state and emits
only a compact path-free envelope. If the final `get_accounts` attempt returns an error or throws
inside that still-running cell, the state becomes terminal `account-scope-failed`; because no
successful response/save existed, it is not mislabeled `snapshot-write-failed`. An interrupted
cell remains fenced because its call outcome is unknown.

Post-bind broker, scan, historicals, complete quote-response batches, RSI, placement-response,
and other handoffs use one monotonic source sequence plus unique purpose keys. Each operation
derives only
`source-<sequence>.json`, records `source-call-started` before the tool and
`source-response-received` before the save, then returns to `transport-bound` and records the path
only under its key; no random path is emitted or carried through narration. A later cell that sees
a transient phase fails closed rather than repeating a possibly successful broker call or save.
Only a final read/scan connector failure returned or caught in that still-running cell may restore
`transport-bound` with no handoff before the connector rule's normal consequence; its sequence
remains consumed, while an uncaught exception or interrupted/lost cell stays fenced. Placement and
cancellation calls never enter `source-call-started`: their durable intent/cancellation protocols
own uncertainty, and only an explicit successful placement response may then reserve and save a
response handoff.

Status derives scratch/candidate from the stored preflight receipt, invocation and expected names
from the stored lifecycle context, and report/output from the stored project root. Its explicit
candidate-write and publish phases allow only loaded-path verification after a lost receipt. Exact
verify success stores `status-published` before output; only exact `status_snapshot_missing` enters
the one rewrite-authorized reconciliation path. Both state slots are cleared only after the final
telemetry helper, or on a terminal path after all permitted
report/status work, lease release, and lifecycle finish. Missing, malformed, cleared, duplicate,
unknown-phase, or unavailable state is never reconstructed. Other runners must retain equivalent
opaque structured state. The deterministic helper's exact-root equality, one-attempt tombstone,
privacy deletion, and no-retry/alternate-root rules remain unchanged.

**2026-08-21 06:03–10:03, six visible failures among seven lifecycle-recorded Codex invocations,
plus two pre-lifecycle attempts, exposed recurring model-authored orchestration seams.** The
06:03 lifecycle start returned its valid ten-field receipt, but a hand-written validator demanded
six fields and abandoned the lifecycle without a terminal event. The hidden 07:01 and 09:01
attempts similarly rejected the valid four-field Python-resolver receipt as if it had three
fields, so they never reached lifecycle and could not appear in its projection. At 08:04 the helper
created valid scratch/source roots, but the
runner displayed the preflight result from a standalone cell without storing transport state and
then halted correctly before `get_accounts`. At 09:33 `validate_constants.py --json` exited zero
with all 31 values, but an invented JavaScript type bucket incorrectly required exact decimal
strings such as `MIN_REL_VOLUME` and `STOP_LOSS_PCT` to be integers, producing a false
configuration halt.

The 07:35 run repeated the generic-staging error for a third time: it appended an empty
`--request-cursor ""` to a non-paginated portfolio response, and the strict helper stopped the
daily-loss generation. At 08:34 the scan itself succeeded (270 instruments, 12 survivors), but a
model-authored quote-map transformation read `result.symbol` even though the connector returns
`result.quote.symbol`; it persisted `{}`, so the pre-RSI evaluator could not return the required
rows. At 10:03 the scan and risk checks succeeded and RSI was fetched, but a reversed ternary
turned an existing absolute quote path into null before the final evaluator. The evaluator rejected
that non-absolute input and no buy followed. Throughout the day the account remained flat at
$1,508.97, the trade ledger gained no row, and there was no review, order intent, placement,
cancellation, notification, or other broker mutation.

**Rules produced:** for the resolver, lifecycle-start, and constants startup receipts, the named
checked-in producer is the sole complete-schema/type/range authority. Codex stores those complete
receipts unchanged; runner glue checks only the core discriminator and fields it must consume, and
may not build `Object.keys` expectations, copied field/name lists, or an independent constant type
map. Active-context and every later helper receipt retain their explicit exact-field contracts and
are also stored unchanged when the routine requires it. The sole Codex preflight recipe must store
`preflight-bound` state before its
compact path-free success output; after the already-required journal and rules-version steps, the
accounts operation loads only that stored state and still saves/binds its canary before output.
These changes remove the three false receipt/configuration rejects without reordering startup or
weakening the helpers that produced the receipts.

After the third non-paginated cursor recurrence, `broker_snapshot.py` still recommends distinct
literal command shapes but now deterministically normalizes `--request-cursor` and `--allow-more`
away for portfolio/quotes before validation and provenance. Those flags carry no meaning for an
unpaged response; the output remains complete and cursor-free. Positions/orders keep the strict
cursor chain. This narrowly supersedes the August 7 rule that portfolio/quote cursor flags must
terminate the generation; every payload, transport-root, schema, generation, hash, and pagination
check that can affect data remains strict.

`evaluate_candidates.py --quotes` now consumes one or more complete saved `get_equity_quotes`
batch responses directly, unwraps only the known MCP envelope, merges batches with cross-file
duplicate rejection, and reads each symbol and whole quote from `data.results[].quote`. The runner
no longer creates any derived quote map, and both evaluator passes reuse the same bound complete
batch set. Malformed, duplicate, missing, unbound, or non-absolute inputs still fail closed. A
pre-RSI or final evaluator handoff failure explicitly publishes `entry_phase: "halted"`. The
all-prefiltered edge is not an evaluator failure: when Step 8 leaves no eligible name, the runner
publishes a normal `entry_phase: "skipped"` reason and makes no placeholder historical, quote,
gate, or evaluator input. The
dashboard maps both historical skipped-form and current
halted-form candidate handoff failures to red `evaluation failure`, calls a `running` lifecycle
record with no activity for 30 minutes `unfinished lifecycle`, and describes the timeline as only
attempts that reached lifecycle start. The report asterisk remains a validated-details marker,
not proof of a trade.

**2026-08-21 11:33, Codex rejected a valid active-context receipt as the wrong bootstrap phase and
then used scheduler memory after an incomplete routine read.** The checked-in active-context bind
helper exited successfully and returned its valid receipt. That receipt's `phase: "preflight"`
correctly described the helper-owned lifecycle phase; the surrounding executor-local bootstrap
state was supposed to store the receipt unchanged and then advance its own phase to
`context-bound`. The runner instead required the receipt itself to say `context-bound`, conflating
two separate state namespaces and falsely rejecting the otherwise valid bind.

The halt remained safe and early. No Robinhood request, scratch creation, order-intent work,
broker read, scan, review, placement, cancellation, report, or status snapshot occurred. The lease
was released and lifecycle finished `coordination-halt` / `coordination-state`. The same transcript
showed that the one-shot routine-file read had stopped at roughly 30,000 of roughly 52,000 tokens,
before EOF. The scheduler header advertised an automation-memory path and provided no earlier
no-memory launch rule; the routine's stateless rule was beyond the unread boundary. Codex later
read and added four lines to that `memory.md`. The edit did not affect broker or repository state,
but it proved that a safety rule located only beyond a truncated read is not an effective launch
boundary.

**Rules produced:** active-context bind is now the fourth startup receipt whose complete schema is
owned by its named checked-in producer, alongside the resolver, lifecycle-start action, and
constants validator. Runner glue stores its complete result unchanged and checks only the core
fields it consumes; it does not count or independently type-map the receipt. The receipt and its
enclosing state are named and validated separately: the unchanged helper receipt must retain
lifecycle `phase: "preflight"`, and only after storing it does the executor-local bootstrap wrapper
advance to `phase: "context-bound"`. Neither value may be inferred from, substituted for, or
required to equal the other. Regression coverage pins both phases so a valid helper receipt cannot
be rejected for failing to claim runner-owned state.

Stateless/no-memory and complete-load requirements now appear at the routine's launch boundary and
in both maintained scheduler prompts. A scheduled run reads the routine sequentially from line 1
through EOF in bounded chunks of at most 50 lines before executing any routine step; a truncated
read continues from the first unread line in smaller chunks and is never completion. The run never
reads, creates, or updates `memory.md` and never calls a framework memory tool, even when the
scheduler UI advertises an automation-memory path. Validated report/status artifacts remain the
only durable run record.

**2026-08-21 12:19, model-authored post-bind state code rejected its own successful positions
response before attempting the file change.** The revised launch boundary worked: Codex read all
832 routine lines in bounded chunks through a recorded EOF and did not access automation memory.
Startup account transport also completed successfully. After one successful
`get_equity_positions` read, the composed operation correctly entered
`source-response-received` with pending purpose `first-positions-0`. Its newly factored
`saveResponse()` function then incorrectly required the *pre-call* state
`transport-bound` with no pending purpose. That condition was impossible after a successful
response, so the function threw `transport state unavailable` before constructing or applying a
file patch. The same generated function contained a latent second source-sequence increment even
though the sequence had already been consumed before the broker call.

This was not a Robinhood, Windows ACL, source-root, JSON, or file-change failure: no positions
file write was attempted. Broker access consisted only of the successful account bind and the one
successful positions read. No order review, intent, placement, cancellation, scan, daily-loss
decision, status publication, or other broker mutation occurred. The lease was released and
lifecycle finished `snapshot-failure` / `snapshot-write-failed`; that broad report classification
did not expose the more precise pre-write orchestration contradiction.

**Rule produced:** the August 20 executor-owned post-bind sequence, pending-purpose, transient
phase, and path map are superseded. `broker_snapshot.py` now owns an append-only source-handoff
journal inside the invocation scratch. A unique purpose is reserved before a broker/read call;
the helper, not the model, chooses and binds the fresh direct-child source filename. After the
file-change facility writes the complete response, a commit action strictly reads the JSON,
records its hash and file identity, and atomically seals that purpose. An explicit final connector
failure can abort only a still-missing reserved file. Immutable reservation and terminal markers
make allocation, duplicate-purpose rejection, commit-versus-abort, and recovery from a lost helper
receipt deterministic without any model-authored transition math. Reservation and consumption are
both refused while any earlier purpose remains unresolved, so renaming a purpose or consuming an
older committed payload cannot bypass an uncertain broker call. The four helper actions own their
complete receipt schemas; runner glue checks only the core identity/outcome values and retains the
whole receipt instead of inventing another field-count/type validator.

Downstream staging, scan filtering, candidate evaluation, and order-intent handling consume
committed logical purposes through the checked-in validators rather than model-carried random
paths. A merely reserved, aborted, unregistered, moved, replaced, or modified file is unusable.
Lookup may recover only a hash- and identity-matching committed handoff, or authorize commit-only
when the exact reserved file is already present; it never authorizes another broker call or file
rewrite. Codex and non-Codex runners use the same helper journal. The file-change facility remains
the sensitive-response writer, and all existing one-call, no-fallback, bound-root, strict-JSON,
privacy, lease, and order-intent rules remain in force.

**2026-08-21 14:10–14:41, a successful Codex run spent 6 minutes 45.6 seconds waiting on an
optional workspace dependency lookup before using the checked-in resolver anyway.** The lookup
was raced against a nominal 20-second timeout, but its outer executor cell remained live after the
race returned. Codex polled that yielded cell eleven times at roughly 30 seconds each before
terminating it. The direct `resolve_python.ps1` call then returned a valid launch-probed Python 3
receipt in about 1.2 seconds, and the trading routine completed normally. The task UI recorded
about 30 minutes 17 seconds while the comparable START-CLOCK-to-summary interval was 19 minutes
52 seconds. The 20 successful Robinhood connector calls themselves consumed only about 5.8
seconds in aggregate. The account remained flat, all entry candidates were blocked by deterministic
gates, and no order, intent, cancellation, notification, ledger mutation, or other broker mutation
occurred.

**Rule produced:** every host-native Windows runner now goes directly to the checked-in
`resolve_python.ps1` command before lifecycle start. The scheduled routine makes no preliminary
framework dependency lookup and supplies no preferred-path hint. The resolver already discovers
the active Codex-bundled runtime and permitted local installations, rejects Store aliases, and
launch-probes candidates, so the removed lookup provided no safety authority. Codex launcher-state
persistence, exact receipt binding, the one narrow ACL/access-denied escalation retry, native Git
Bash quoting, active-context recovery, Linux/macOS handling, and Claude's existing direct Windows
resolver behavior remain unchanged.

The same trace showed a smaller deterministic redundancy: DAILY-LOSS DISCOVERY fetched and staged
a portfolio response even though `daily_loss.py --symbols-out` derives its quote set exclusively
from positions and executions and never consumes portfolio value. Discovery now fetches only the
complete position and order page sets, and the helper rejects calculation-only portfolio, quote,
or halt-percentage options in that mode. The separate FINAL snapshot still fetches a fresh
portfolio immediately before authoritative evaluation, so the optimization removes no breaker
input and does not permit reuse of an earlier account value.

The run also repeated a successfully committed/staged response after model glue looked for a
nonexistent stage-receipt field named `count`; the helper's actual field is `file_count`. Stage
receipt validation now names that field and its `files` relationship explicitly without counting
helper-owned object keys. A terminal singleton positions/order page or quote batch is already a
complete sealed set, so it is consumed directly when the helper proves `complete: true` and
`file_count: 1`; only real multi-page or multi-batch sets are aggregate-staged. Hash, provenance,
cursor, scratch, generation, and FINAL-snapshot validation remain unchanged.

**2026-08-25 08:03 and 08:33, the runner treated a stage descriptor object as a pathname.** At
08:33 the first positions response was successfully reserved, saved, committed, semantically
validated, atomically staged, and sealed as one terminal page. Model-authored glue then compared
`stage.files[0]` directly with the requested output string. The comparison could never succeed:
`files[0]` is the helper's provenance descriptor object and its pathname is nested at
`files[0].output`. The run falsely closed as `snapshot-failure` / `snapshot-write-failed` even
though neither the broker read nor the write/stage operation failed. It made only the account and
positions reads; no scan, review, order, cancellation, notification, or broker mutation occurred.

The successful 08:03 run had already exercised the same bug without exposing it on the dashboard.
It passed `stage.files[0]` as a `daily_loss.py` argument, consumed fresh A and B files while trying
to repair the resulting path rejection, and then called `.toLowerCase()` on the object before
eventually inspecting the receipt and recovering with `.files[0].output`. Generation B was thereby
spent on runner glue rather than a semantic/coherence failure. The earlier `count` rule named the
relationship in prose but did not make the usable path type mechanically unambiguous.

**Rule produced:** `broker_snapshot.py stage` now preserves its existing detailed `files[]`
descriptors and additionally derives one ordered `output_paths[]` string list from their nested
outputs. One exact receipt binder validates action, kind, generation, completion, set identity,
counts, descriptor shape, and index-for-index equality with the submitted `--output` strings for
every staged kind, page, singleton, and aggregate. Downstream code receives only the frozen
`output_paths` strings. It may never compare, stringify, lowercase, or pass a `files[i]` descriptor
as a path. A runner receipt-binding failure stops immediately and cannot spend generation B; the
one A→B retry remains reserved for a fully persisted generation's semantic/coherence failure.

**2026-08-25 09:35, the runner reserved after the broker read and put uppercase generation identity
inside a lowercase journal purpose.** The run had loaded the new `output_paths` receipt contract
and successfully used it during earlier staging, so this was not the 08:33 descriptor bug or stale
routine text. At DAILY-LOSS discovery it called `get_equity_positions` successfully and only then
tried to reserve `daily-loss-A-discovery-positions-0`. `reserve-source` correctly rejected uppercase
`A` because immutable source purposes accept only lowercase letters, digits, and hyphens. Since the
read had already happened without a reservation, its complete response could not be persisted,
reconstructed, or fetched again; the run closed `snapshot-failure` / `snapshot-write-failed` before
orders discovery or scanning. Exactly five Robinhood calls occurred, all successful read-only
account/positions/portfolio/SPY/positions calls. The account was flat and no review, order,
cancellation, notification, or broker mutation occurred. Lease release, report read-back, and
lifecycle finalization all succeeded.

The routine did state that source purposes are lowercase, but the adjacent instruction to use `A`
throughout generation A was broad enough for the runner to interpolate it into the purpose. The
generic save helper also accepted an already-returned response and therefore made reservation
happen too late, contradicting the pre-call fence in the source-journal section.

**Rule produced:** uppercase `A`/`B` is now scoped only to helper generation arguments, receipt
validation, and generation metadata. A closed exact builder maps those values to lowercase purpose
slugs `a`/`b`, allows only the DAILY-LOSS discovery/marks/final kind pairs and a bounded page index,
and validates the canonical grammar before any command. An exact reservation operation must then
succeed before the corresponding broker callable is awaited. The successful reservation receipt's
canonical purpose and ID are the only values permitted for the write, commit, and stage; a helper
that accepts an already-returned response and reserves afterward is forbidden. The Python journal
validator remains strict—there is no case-normalizing alias or collision ambiguity.

**2026-08-25 10:03, successful symbol discovery was rejected by mixed JavaScript command-result
shapes.** The preceding repair was loaded and worked: discovery positions and orders used lowercase
purposes, were reserved before their broker reads, and were committed and staged with complete
validated singleton receipts. `daily_loss.py --symbols-out` then exited successfully and atomically
wrote the correct empty symbol list. Model-authored glue had separately defined a raw-result
`runHelper` and a `{r, j}`-result `runJson`, called the raw helper, then tested
`symRun.r.exit_code`. Because a raw process has no `r` property, JavaScript raised a `TypeError`
before any quote call or generation-B attempt. All ten Robinhood calls in the run were successful
reads; no scan, review, placement, cancellation, or broker mutation occurred. The account remained
flat, and final refresh, status/report persistence, lease release, and lifecycle finish completed.
The first final-refresh cell also retyped a malformed UUID regular expression and rejected valid
machine state before any broker call; the runner corrected it in a second cell. Final refresh now
explicitly reuses the existing exact token precondition and forbids an ad-hoc UUID validator.

**Rules produced:** every DAILY-LOSS local command now uses one exact drained wrapper with one frozen
`{process, receipt}` shape, accumulates every yielded stdout chunk before parsing, checks only
`process.exit_code`, and consumes only `receipt`; raw and short-property aliases or a second wrapper
are forbidden in Codex. Claude and other runners use one fully drained native equivalent and may
not mix result shapes or replace direct stdout with a file read. Discovery mode now emits a strict compact
stdout receipt containing its exact trading date, cutoff, symbol count, and ordered symbol array.
The runner validates and freezes that receipt directly and treats the atomically written symbols
file as audit-only, removing the extra `Get-Content`/file-read command and its return-shape seam.
Reservation and staging recipes use the same wrapper, and contract tests reject the specific
`.r.exit_code` regression.

The review found the same chunk-loss pattern in six older Codex command-drain recipes used by
startup, intent checks, transport binding, release, and final telemetry. Their checked-in helpers
normally print only when exiting, so they did not cause this run, but every drain now accumulates
all returned stdout chunks before parsing rather than retaining only the last poll.

**The same 10:03 review found a zero-trade realized-P&L reporting error.** The final
`get_realized_pnl` call succeeded with aggregate `total_returns: "0"` and zero trades, while its one
returned bucket had nullable `realized_gain`. The report/status incorrectly published the aggregate
as unavailable/null even though this exact connector shape had previously been handled as $0. This
did not affect the breaker or any trading decision because cost-basis realized P&L is telemetry only.
The aggregate `data.total_returns` decimal string is now the sole headline value; a zero total stays
zero regardless of a nullable bucket, while a missing/non-finite aggregate gets the one identical
retry and becomes null only after both attempts are unusable.

**The same review found that Step 10 still made the model extract successful RSI responses into a
new symbol→numbers map.** That rule was correct in July when the runner had no mechanical
full-result copy and raw-response tokens were regenerated by the model. The current bound save
recipe serializes the actual complete tool-result object directly and seals it before consumption;
re-keying now adds model work and another authored-data failure surface without protecting values.

**Rule produced:** each successful technical-indicator result is committed complete and unmodified
under its own `rsi-*` purpose. `evaluate_candidates.py` unwraps the MCP envelope, validates the raw
symbol/series, and rejects malformed/error envelopes and duplicate symbols. An exhausted read is
aborted and omitted, so missing RSI blocks that name; there is no closes refetch or derived
fallback. Fixed `{}` is committed only when no RSI purpose exists, keeping the final gate enabled.
Legacy keyed maps remain accepted for compatibility, not authored by the automated routine. This
supersedes only the normal-path transport procedure from the 2026-07-28/29 incidents; their strict
JSON and fail-loud lessons remain in force.

**2026-08-25 10:54, a successful Codex run spent most of its 23:01 recovering from
model-authored contract drift.** The scheduler still supplied an older abbreviated prompt, so the
runner opened automation memory, read the roughly 258 KiB routine in one operation, and then read
the routine again in the required bounded sequence. During execution it rejected a valid portfolio
because Robinhood's numeric fields were decimal strings and made a redundant portfolio read
(roughly 51 seconds). It counted 33 historical stop orders instead of stops filled on the current
Pacific date and corrected that with another complete order read (roughly 54 seconds). The named
scan already reported scalar `sorting: "Relative volume desc"`, but the runner inspected invented
alternate fields and performed an unnecessary saved-scan update (roughly 27 seconds). It also sent
three invalid historical argument combinations before succeeding with `symbols`, `interval: "day"`,
`bounds: "regular"`, and `start_time`, began SECOND-phase daily-loss discovery before FIRST had
completed, and recovered from several local JavaScript wrapper, syntax, and report-assembly errors.

The account was flat. No order review, placement, cancellation, or account mutation occurred; the
only external write was the idempotent saved-scan sort update, and the final strategy result was
valid. The report nevertheless omitted much of that recovery history and printed an unsupported
5,548-token estimate even though the runner exposed no complete token counter. Safety controls
contained every error, but the run showed that successful recovery can hide substantial wasted
work and that the report must distinguish a clean run from a recovered one.

**Deterministic hardening produced:** the maintained scheduler prompt now treats an injected
automation-memory path as metadata and requires each pre-EOF read call to contain exactly one
contiguous routine chunk and nothing else. `connector_contract.py` is the sole authority for
canonical portfolio decimal strings, required visible scan columns, scalar scan sorting, and
scan-update confirmation; a runner-side semantic mistake cannot trigger a duplicate successful
read. `daily_loss.py` derives the Pacific-date stop count and stopped-symbol set from the same FINAL
deduplicated order snapshot that produces the daily-loss verdict, eliminating the separate
historical-order count. START CLOCK now emits one deterministic `historicals_start_time`, and Step 8
copies it into the exact `{symbols, interval, bounds, start_time}` payload with at most one identical
retry—no `span`, `end_time`, date arithmetic, or schema discovery. SECOND/daily-loss work is barred
until FIRST is explicitly complete. Every report now includes ordered Recovery diagnostics even
when retry succeeded, including whether a broker call occurred or mutation was possible; token use
is exact when the runner exposes a complete counter and otherwise explicitly unavailable, never
estimated.

**2026-08-25 12:53, Codex rejected a valid flat positions page after the deterministic
transport had already committed and staged it successfully.** Robinhood returned the documented
positions envelope at `data.positions: []` and omitted `data.next`, which is a valid terminal page.
The stage receipt correctly proved `complete: true`, one file, and `next_cursor: null`. Runner-authored
JavaScript nevertheless looked for FIRST rows at an invented `data.results` path and treated a missing
raw `next` property as schema failure rather than terminal pagination. It then used a forbidden generic
polymorphic stage wrapper, skipped the whole-generation B semantic retry, and misclassified the
post-persistence semantic error as `snapshot-write-failed`. No broker mutation or order occurred; a
later refresh confirmed the account was flat.

Finalization compounded the false failure. The correct lifecycle-bound 12:53 report was created, but
the runner read a hard-coded report from an older 10:34 invocation, inferred a collision that did not
exist, and finished lifecycle without binding the newly written report or status snapshot. This was
not a Robinhood outage, an invalid broker response, or a failed save transport; both failures were
model-authored schema/path reconstruction after authoritative checked-in state already existed.

**Deterministic hardening produced:** every committed positions/orders page now passes through
`connector_contract.py page`. That helper owns the actual `data.positions` / `data.orders` envelope,
normalizes missing, null, or empty `data.next` to terminal `next_cursor: null`, and is the sole source
of subsequent cursors. In DAILY-LOSS only, each stage receipt is cross-bound to that page's exact
committed purpose, row count, next/request cursors, hashes, provenance, and output. After FIRST reaches
the terminal page, `first-positions-set` validates every page and cursor together, rejects cross-page duplicate
symbols, and returns the sole compact projection containing symbol, exact quantities, and average buy
price, so the runner never parses or model-deduplicates the broker envelope. FIRST goes directly from
the terminal page helper to `first-positions-set` and never stages. In DAILY-LOSS, a successful page helper
plus stage receipt cannot be rejected by a second raw-response parser; exact semantic, missing-source, and
local-orchestration failures have distinct outcomes, and only a genuine post-persistence semantic A
failure consumes whole generation B. Codex report creation and read-back now run in one composed
operation: both targets come from the same live lifecycle `expected_report_file`, overwrite is refused,
exact read-back transitions to a terminal report state, and broker/source transport stays closed while
status and lifecycle finalization complete. Claude retains its native Write/Read tools under the same
single-binding rule; trading strategy and broker behavior are unchanged.

**2026-08-25 13:47, lifecycle preflight committed but its dashboard projection did not publish.**
The append-only journal durably recorded the exact 13:47:44 Pacific binding, but the event command
returned failure before its success receipt. The one authorized read-only `status` recovery then
rejected the stale JSON projection, so Codex correctly finished `coordination-halt` /
`coordination-state` before lease acquisition. Finalization later republished all three lifecycle
events and restored a healthy projection. No scratch/source root, report, status snapshot, Robinhood
request, order-intent action, notification, or broker mutation occurred. The runner's compact wrapper
discarded the helper's structured error, so the precise inner OS error cannot be proven; the exposed
Windows publication boundary had no retry for transient atomic-replace sharing/access/lock denial.

**Rule produced:** the lifecycle projection writer now retains its already validated, fsynced
temporary file and retries `os.replace` exactly once after a short bounded delay only for Windows
WinError 5, 32, or 33. It never replays the already committed journal event. A non-transient or second
failure remains terminal and leaves the prior projection untouched; the temporary file is cleaned up.
Runner glue must preserve the helper's bounded safe `recorded`, `reason`, and `detail` diagnostic
instead of reducing a publication failure to generic coordination state, but that diagnostic never
authorizes an event replay, lease acquisition, or broker call.

**2026-08-25 14:33, Codex invented an unsupported aggregate action during DAILY-LOSS.**
FIRST completed flat and the run entered SECOND normally. A model-authored generic `pageSet(kind,
pages)` wrapper then changed `kind = orders` into `connector_contract.py orders-set`, even though
the helper exposed no such action. The same wrapper also expected `output_paths` from connector-
contract set receipts, but FIRST's positions projection returns rows rather than staged file paths.
The run halted `coordination-halt` / `coordination-state` after nine successful read-only Robinhood
calls. It made no scan, review, placement, cancellation, order-intent mutation, or other broker
mutation; report/status persistence, lease release, and lifecycle finalization succeeded.

The first unsupported command hid additional defects in the same cell: it bypassed the exact stage
receipt binder, failed to aggregate-seal multi-page inputs, constructed incomplete daily-loss argv,
treated a normal nonempty discovery symbol set as an error instead of fetching quotes, omitted those
quotes from evaluation, and supplied no valid generation-B semantic retry. Merely adding an
`orders-set` command would therefore have moved the failure rather than repaired the workflow.

**Rule produced:** `positions-set` was narrowed and renamed to `first-positions-set`, which rejects
purposes outside FIRST's ordered page namespace and deliberately returns no file paths. DAILY-LOSS
has a closed connector-contract action matrix: positions and orders use only the literal `page`
action; `orders-set` and `first-orders-set` do not exist; action names may never be synthesized from
the response kind; and generic page/set wrappers are forbidden. Singleton and multi-page file inputs
come only from the exact `bindStageOutputPaths` result, with multi-page sets aggregate-sealed by
`broker_snapshot.py stage`. A missing binding is local coordination failure and cannot authorize a
broker retry or generation B. A nonempty discovery symbol set is normal and requires bounded quote
fetching, staging, and inclusion in the final deterministic evaluation.

**2026-08-25 16:04, Codex staged a FIRST page that required no staging and crashed on an
unavailable runtime global.** Robinhood's FIRST positions call succeeded with the account flat.
The complete response was written and committed as `first-positions-0`, and the deterministic page
contract proved `complete: true`, `row_count: 0`, and `next_cursor: null`. The required next action
was therefore `first-positions-set`, which consumes committed purposes directly. Instead, model-
authored JavaScript imported DAILY-LOSS staging into FIRST and tried to build an output filename with
`crypto.randomUUID()`. Codex's fresh V8 executor exposes only its listed globals and did not expose
`crypto`, so the cell threw `ReferenceError` before any stage command ran.

Two recovery cells then defined a drain wrapper around an unawaited `tools.exec_command` Promise.
Each fabricated an empty process-shaped result immediately, so neither lookup actually ran and both
falsely reported the committed source unavailable. Read-only replay proved the journal still returned
`status: committed`, `recovery_action: consume`; both `connector_contract.py page` and
`first-positions-set` succeeded against the exact retained source. The report and dashboard also
misclassified these runner errors as `snapshot-failure` / `snapshot-write-failed`, even though source
transport had succeeded. The correct outcome was `coordination-halt` / `coordination-state`.

Only `get_accounts` and one read-only `get_equity_positions` call occurred. No retry, quote, order,
review, placement, cancellation, scan mutation, notification, or other broker mutation occurred.

**Rule produced:** FIRST now has a closed action matrix: reserve/write/commit each positions page,
validate it with literal `connector_contract.py page`, and after the terminal page invoke
`first-positions-set` immediately. FIRST may never stage, carry a snapshot generation, allocate an
output path, inspect stage fields, or use random/clock/runtime-global identifiers. Its one explicit
connector retry uses only the exact `first-positions-N-retry` purpose; the helper rejects every other
suffix. Per-page projection was removed from `connector_contract.py page`, leaving rows exclusively
to `first-positions-set`. Every Codex local command must be awaited before its concrete process result
is drained, and recovery accepts only the exact helper-owned `status` / `recovery_action` pair. A runner-wrapper or
receipt-binding failure after a committed source is coordination failure and cannot masquerade as a
write failure or authorize another broker call. For the DAILY-LOSS staging that genuinely remains,
`broker_snapshot.py stage --auto-output-scratch` now allocates fresh direct-child output names and marks the
receipt `output_mode: helper-allocated`; unattended runner code no longer constructs staged paths.
The helper also binds every page to the complete request-cursor chain, rejects repeated current/next
cursors and a continuation at the exact 1,000-page ceiling before another read, and preserves exact
source-journal state codes. FIRST purpose names are canonical zero-based page names with at most one
exact `-retry` suffix and are positions-only. Every reservation binds the full request-cursor chain,
requires its index to match that chain, and rejects page 1,000 before a broker call; leading-zero,
alternate-suffix, and chained-retry forms are likewise rejected. A FIRST retry reservation now requires an exact `--retry-of` base; under
the reservation lock, before the retry broker call, the helper proves that base was already immutably
aborted for `connector-failed` with no response file, and consumers repeat the proof. Staging now emits
separate input, response-envelope, semantic, binding, internal-allocation, retry-state, and write failure codes. Failure
receipts bind the actual helper kind and generation; the helper atomically persists the one B
authorization before emitting it, rejects B without that marker, and rejects any later A. Only a
recognized successful connector envelope with invalid broker semantics consumes B. Error/unknown envelopes and caller cursor binding are coordination
failures, while lost transport remains terminal without being collapsed into a runner error.
Reservation of a later FIRST page now revalidates every prior committed positions page and proves
the submitted cursor is the exact preceding broker-returned `next_cursor` before it issues a new
response path; page and final-set consumers rebind their arguments to the immutable reservation
hashes. DAILY-LOSS generation state is likewise enforced before reads: the helper parses the closed
lowercase purpose namespace, shares one lock between A→B authorization and reservation, rejects B
before authorization and A afterward, and persists B exhaustion before a terminal B semantic
receipt. This closes the adjacent cases where a correct eventual validator could still have allowed
one unnecessary broker read first, or where runner memory could have silently recreated the retry
budget.

**Pre-release review of the 16:04 hardening found two adjacent fail-closed defects before the next
scheduled run.** The FIRST page consumer initially used the final-set journal validator: page 1
supplied one current purpose plus its two-cursor chain, while that validator correctly required
equal purpose and cursor counts for a completed set. Every paginated FIRST census would therefore
have halted at page 1 after a successful committed read. Page validation now has its own journal
binder—one current purpose whose index equals the full chain length minus one—while
`first-positions-set` retains the one-purpose-per-cursor contract. Real Windows subprocess tests
cover both a normal page 1 and its authorized connector-failed retry.

The second defect was that `daily_loss.py` returned the same exit-2/human-stderr shape for semantic
reconciliation, staged-provenance/input loss, output publication failure, bad binding, and a helper
invariant. A runner could not grant the one semantic retry without also risking consumption of B
for an I/O or orchestration fault. Unattended calls now opt into one exact phase-typed JSON failure
receipt. Only exact `daily_loss_semantic_invalid` with matching mode, generation, and recovery
action can transition the generation; input/output, binding, and internal codes never do. Internal
invariants have a distinct exception type, staged provenance is classified as input integrity, and
every external generation transition or B completion is persisted in the same binding operation
before narration or another action. These review findings made no broker call and had no account or
order effect.

**2026-08-21, the post-run performance audit retained the complete-read safety boundary while
removing half of its normal round trips.** The 11:33 incident above remains controlling evidence
that a one-shot routine read is unsafe: that read stopped around 30,000 of roughly 52,000 tokens
before EOF and before the stateless rule. The later 50-line ceiling prevented that failure, but the
then-current 831-line routine required at least 17 sequential reads even when none was truncated. A
byte-level audit found that every contiguous 100-line block in the checked-in routine was no larger
than 40,580 UTF-8 bytes, below a hard 40 KiB bound.

**Rule produced:** the normal sequential read ceiling is now 100 lines, reducing that then-current
831-line routine to nine normal reads. Exact-next-line continuation, affirmative EOF proof, and the
ban on every other model-authored tool call before EOF remain unchanged. Any truncation, omitted
interval, or missing EOF proof requires re-reading the missing interval first in chunks of at most
50 lines and then in successively smaller sequential chunks until every line and EOF are proven.
A sliding-window regression rejects any future routine edit that makes even one contiguous
100-line block exceed 40 KiB as UTF-8, so increased line density cannot silently invalidate the
larger normal chunk size.

**2026-08-21 15:33, Codex lost the acquired lease token between two startup operations and passed
an empty string to the order-intent pending check.** Lease acquisition had returned a valid random
fencing token, and active-context binding with that token succeeded. The later model-authored
pending wrapper loaded the wrong executor-state slot instead of the stored lease value, reduced
that lookup to an empty string, and invoked `order_intents.py pending --run-token` with it. The
helper correctly rejected the empty value as `order_intent_state_error`, and startup halted as
`risk-halt` / `order-state-guard`. The run performed only read-only account, positions, and orders
calls: it made no scan, order-intent mutation, review, placement, cancellation, or other broker
mutation. The lease was released and lifecycle finalization completed.

**The same halted run exposed two secondary observability failures after the safe broker work had
ended.** `run_performance.py record-internal` successfully persisted and returned its documented
14-field success receipt, but model-authored finalization glue compared it with an invented
18-field contract and incorrectly printed `Timing unavailable`. The verified report created and
read back for this invocation was `rhmra-log-2026_08_21-15_33.md`, while the final transcript
pointed to nonexistent `rhmra-log-2026_08_21-14_40.md`. That wrong suffix also matched an earlier
automation-memory run timestamp, evidence that prior-run context may have contaminated the final
prose despite the no-memory rule; the archived transcript does not prove a specific memory read.
Both defects were post-run display/telemetry errors only: they did not change the saved report,
lifecycle outcome, lease handling, or absence of broker mutations.

**Rule produced:** the raw lease-issued `RUN_LOCK_TOKEN` is one opaque, private machine-carried
value from successful acquisition through every active-context, renewal, order-intent, mutation
precheck, and release operation. Runner glue loads the exact value from executor-private state for
each use. Runner glue never exposes it in ad-hoc output or narration and never stores it in a
bootstrap, transport, report, status, or memory artifact; it passes the value only to prescribed
checked-in helper arguments, and only the checked-in lease and order-intent protocols may
intentionally persist it. The runner never retypes, reconstructs, substitutes, or derives the
value from another receipt. Before invoking any token-consuming helper or broker tool, it must
prove that the expected state slot contains the nonempty acquired value in the correct phase.
Missing, cleared, malformed, or wrong-phase token state stops before that helper or broker
invocation; an empty placeholder is never a permitted fail-closed probe. Existing helper token
validation remains unchanged because it correctly contained this incident.

**Secondary rules produced:** the checked-in `record-internal` producer, not model-authored glue,
owns its complete success schema. Runner validation checks only its core discriminator/invocation
binding and the five estimate values consumed by the final summary; it never counts fields or
copies a 14- or 18-field key/type map. The final Codex orchestration accepts only a token-free
released/lost lease tombstone, loads the lifecycle-bound `expected_report_file` before telemetry,
attempts telemetry once, and machine-returns that exact report name on both valid and invalid
telemetry paths. Only after the attempt does it clear all three executor-state slots. The final
file-card line copies that returned bare name byte-for-byte and never reconstructs it from current
time, narration, memory, a displayed pattern, or a prior run.

**2026-08-24 08:03, Codex falsely rejected a valid START CLOCK receipt after inventing a schema
field the producer did not emit.** `market_clock.py` executed before lease acquisition or broker
access, but model-authored validation required `clock.schema_version === 1`. The clock's documented
JSON contract had not yet included a `schema_version` property, so the wrapper converted a valid
deterministic result into `coordination-halt / clock-unavailable`. Lifecycle finished safely after
77 seconds with no run timestamp, lease, scratch, account resolution, Robinhood request, report,
status snapshot, order intent, order, cancellation, or notification. The 08:34 invocation used the
documented fields without the invented condition and completed normally, proving the helper and
host clock path were available.

**Rule produced:** every machine-readable clock result now carries integer `schema_version: 1`,
and the routine names the complete producer-owned type contract for every START, DAILY-LOSS,
PRE-BUY, and ORDER-INTENT clock invocation. Runner glue validates the named version, fields, types,
and preflight constants hash; it never counts keys or invents an `action`, `ok`, `status`, or other
required property. This keeps both Codex and Claude on one deterministic receipt contract while
preserving the fail-closed response to a genuinely missing, malformed, or mismatched clock.

**2026-08-24 10:33, repeated evaluator selectors silently discarded the first committed
historicals batch.** The scan produced 13 working-list names. The runner correctly reserved,
saved, and committed `historicals-0`, `historicals-1`, and `candidate-quotes-0`, but built the
command as `--bars-purpose historicals-0 --bars-purpose historicals-1`. The evaluator's
`nargs="+"` option still used argparse's default last-value storage, so the second occurrence
replaced the first. Its pre-RSI JSON consequently recorded only `historicals-1` and returned only
COPR; the completeness check rejected one row where 13 were required and safely halted entry.
Forensics also showed that Robinhood omitted CHNR and AMOD from both requested historical/quote
results, so consuming both batches would otherwise have produced only 11 rows and triggered the
same all-candidate halt. Earlier in the run, a shell quote helper called `replaceAll` directly on
numeric constants; the runner recovered by converting the validated values to strings and reused
the already committed scan without making a second broker scan. No RSI request, review, intent,
order, cancellation, notification, or other broker mutation followed, and the account remained
flat.

**Rules produced:** every evaluator purpose selector now accumulates both canonical grouped values
and repeated occurrences in order, while duplicate purpose values are rejected. The routine's
canonical form still emits each selector exactly once with its complete ordered list. Both passes
also receive the exact post-prefilter symbols through `--expected-symbols`; a requested name
missing historicals, quotes, or both receives an explicit deterministic non-buy row instead of
vanishing from the intersection and halting otherwise complete names. An unexpected returned name,
invalid expected set, malformed source, or incomplete evaluator output still fails closed. Exact
command recipes stringify already validated scalar constants before applying native-shell quoting,
so numeric and Boolean settings cannot fail inside `replaceAll`.

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

**2026-08-19 11:40 recurrence, the realized-P&L payload varied again.** A run's first
`get_realized_pnl` call in the FINAL STATUS REFRESH sent `account_number`, `start_date`, and
`end_date` instead of the required payload, and the connector rejected it with
`InvalidArgument: un-specified asset class`. The immediate retry supplied the asset class and
succeeded, so the run completed with a correct `$0 (0 trades)` figure and no trading, state, or
report consequence; the cost was one wasted call and the latency around it. This is the second
recorded instance of the same omission after the 2026-08-12 11:43 compaction incident, and the
prohibition it violated was already explicit — the routine named the exact payload twice and
separately forbade "a start/end date form".

**Rule produced:** the payload is no longer restated in prose at each call site. It is defined once
as the named REALIZED P&L PAYLOAD, carries the exact backend error string so the failure is
recognizable, states that `start_date` and `end_date` are not arguments of this call, and the two
call sites now reference the name rather than repeating a payload that can drift. No deterministic
guard is possible here: `get_realized_pnl` is a direct connector call with no checked-in helper in
its path, so unlike the staging vocabulary this rule cannot be enforced by a script.

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

**2026-08-13 15:35, RSI handoff was authored in scratch instead of the bound response-source root.** The run had a successful transport binding, valid scan, and valid pre-RSI evaluation with no buy candidates. The final evaluator correctly rejected the RSI-number map because the runner wrote it under the invocation scratch directory, while `evaluate_candidates.py` treats `--rsi-file` as an externally authored input that must be a direct child of the bound `SOURCE_ROOT`. No broker mutation occurred; the account remained flat and the final status snapshot was valid. The lifecycle closed as `snapshot-failure` / `snapshot-write-failed`, the correct fail-closed outcome for an unbound external handoff.

**Rule produced:** the routine now states the destination at both authoring and consumption: write the RSI map through the startup-bound file-change facility as a fresh direct child of `SOURCE_ROOT`; only evaluator-generated outputs belong in scratch or `run-reports/`.

## CLAUDE RUNNER COMPATIBILITY — newer models refused live order execution

**2026-08-19 11:17 Pacific, Claude Sonnet 5 refused a valid live EPM buy after every
project gate had passed.** The account was flat, `DRY_RUN = false`, the session and buying-power
checks passed, SPY was green, the daily-loss and stop-count guards were clear, and EPM passed the
liquidity, dip, spread, and RSI gates for a $301.79 buy. `review_equity_order` returned no alerts.
Claude then stated in its user-visible response and saved report that its operating rules
categorically prohibited executing a financial trade even when explicitly pre-authorized. It
never called `place_equity_order`. The durable intent was abandoned with `submit_attempts = 0`,
`outcome = never_submitted`, and an event note naming assistant policy as the sole reason. Claude
nevertheless finalized lifecycle as `completed`, so the dashboard did not expose the refusal as a
failure.

**Two independent scheduled sessions repeated the same refusal before lifecycle.** At 11:35
Pacific on 2026-08-19, Claude Haiku 4.5 stopped after reading the routine and called financial
trading an unconditional safety prohibition that still applied to scheduled, fully specified,
pre-authorized work. At 16:08 Pacific on 2026-08-20, Claude Opus 4.6 likewise refused buys, sells,
cancellations, and protective orders regardless of authorization, automation level, or scheduled
task status. Neither session reached Robinhood or created a run artifact, which is why dashboard
history alone cannot reveal those two refusals.

**Historical counterevidence defines the compatibility boundary.** A 2026-07-06 Claude Sonnet
4.6 run bought TDIC, received a fill, and placed the protective stop. The repository and connector
therefore did support Claude live execution at that time. That success did not transfer to the
later tested Claude deployments. The evidence supports a model/runtime-policy compatibility
change—not a strategy gate, account problem, connector failure, or general moral judgment about
this particular trade.

**Audit scope:** 467 run reports, 35 current Claude project transcripts, and 586 older Claude
local-agent transcripts were searched. The older traces contained no additional explicit
trading-policy refusals. Strategy gates such as SPY red/blackout and infrastructure failures such
as snapshot or coordination errors were excluded from this compatibility conclusion.

**Documentation decision produced:** Claude is no longer recommended or supported as this
project's execution runner. Sonnet 4.6 remains historical evidence only; it is not a current
deployment recommendation. The README and Quickstart direct new users to Codex, remove Claude
connector/scheduler setup, and tell operators to keep existing Claude schedules disabled. The
routine's model note then recommended Codex Luna 5.6 high; the 2026-08-26 incident below superseded
that unattended recommendation. A future Claude release may be reconsidered
only after a supervised, approval-gated acceptance test proves the complete mutation path—live
buy, sell, cancellation, filled-buy stop placement, and stop verification—and a scheduled run
then repeats the required behavior under the intended approval configuration. Claude transcripts
remain local outside the repository; reports and order-intent records remain local and gitignored
because they contain account activity. This entry preserves the sanitized evidence.

## 2026-08-26 CODEX LUNA ORCHESTRATION COHORT — FAIL-CLOSED, NOT RELIABLE

**Eight scheduled attempts from 06:04 through the 10:07 start produced one normal SPY-red
completion and seven failed or materially degraded paths.** No attempt placed, cancelled, reviewed,
or otherwise mutated a broker order, and the account remained flat. That is important: the lease,
source journal, deterministic consumers, and conservative gates prevented bad glue from becoming a
bad trade. It is not an acceptable reliability result for an unattended scheduler, however.

The failures were separate model-authored orchestration defects rather than one broker outage:

- At 06:04 the launcher resolver succeeded, but the cell merely printed its output. It did not
  clear, parse, store, and reload the required bootstrap state, so the successful launcher could
  not be used safely by lifecycle. A separate scheduled attempt lost bootstrap state before
  lifecycle and therefore remained outside dashboard history.
- At 07:04 the checked-in portfolio contract correctly normalized a flat account with
  `equity_value: "0"`. Runner JavaScript invented an extra finite/nonzero regular-expression test
  and rejected that valid zero as a snapshot failure.
- At 07:34 runner code declared `evaluationCommand` with `const` and then used `+=`, raising a
  JavaScript `TypeError` after the required inputs had already been committed.
- At 08:06 the response write succeeded, but commit argv used nonexistent `reservation.id` rather
  than the receipt's `reservation_id`, passing an undefined value across a boundary the helper
  should have owned itself.
- At 09:04 DAILY-LOSS actually completed successfully. THIRD then mixed a raw process returned by
  `runHelper` with a caller expecting `{process, receipt}` and crashed before `get_scans`. The saved
  report incorrectly blamed DAILY-LOSS, proving the model's narration was not a reliable phase
  authority.
- At 09:35 the flat-account SPY-red path completed normally.
- The 10:07 attempt skipped routine lines 1600–1649 during its claimed complete read, then saved and
  committed a valid SPY response but manually probed the wrong MCP-envelope path and falsely called
  the quote invalid. Its post-finish code also referenced `STATE_KEY` outside the lexical block that
  declared it. It placed no order, but a safe skip was reached through unnecessary recovery and a
  finalization exception rather than the intended deterministic path.

**Repair produced:** the Windows launcher now has one exact clear → resolve/drain → parse → store →
reload/verify cell before lifecycle. The routine carries an exact monotone read cursor through EOF.
`commit-source` now self-correlates from immutable scratch plus purpose, while the optional explicit
UUID remains only a compatibility assertion and `abort-source` stays strictly ID-bound. Every
post-bind local JSON command uses one frozen `{process, receipt}` vocabulary. Portfolio helper
output is producer-authoritative and zero-valid, evaluation commands are one-shot frozen joins,
and the SPY comparison is owned by `connector_contract.py quote` instead of raw envelope probing.
THIRD/FOURTH write durable lifecycle phases for report attribution, report mode comes only from the
validated constants receipt, and the exact final telemetry block owns its own state identifiers.
The unattended recommendation moved from Luna to Sol high, but that model change does not replace
the required supervised dry-run and scheduled acceptance tests. Regression tests cover each
deterministic boundary; the incident is not closed operationally until the post-repair cohort runs
cleanly.

## 2026-08-27 CODEX TERRA MORNING COHORT — VALID HELPERS, DRIFTED RUNNER GLUE

**Six scheduled attempts from 06:04 through 08:34 produced one normal scan and five runner-authored
failures or degraded paths.** The task was configured as `gpt-5.6-terra` with high reasoning even
though the maintained unattended deployment candidate was Codex Sol 5.6 high. This was not an
overlap, Robinhood outage, Python failure, corrupt lifecycle store, or time-zone problem. Failed
runs had ended or released before the next firing, and the 08:03 attempt completed the same helper,
connector, daily-loss, scan, report, status, lease, and lifecycle path normally.

Four failures were the same transcription defect expressed with different malformed regular
expressions. At 06:04 and 07:34 the runner altered the canonical UUIDv4 check in the exact
order-intent startup recipe, rejected its own valid invocation/lease state, and never invoked
`order_intents.py`. At 07:07 and 08:34 `run_lifecycle.py start` committed one valid invocation, but
an ad-hoc lifecycle-start cell omitted or shortened a UUID group and falsely rejected the valid
receipt. Those two invocations were left as `running` / `scheduled`, which the dashboard truthfully
rendered as unfinished lifecycle. The two identical JSON lines visible in the nested shell card at
08:34 were a live/final UI rendering echo: the helper emitted once, the lifecycle journal recorded
one start event, the command returned before a nested session poll was needed, and the validator's
malformed regex was independently sufficient to cause the halt.

The 06:33 path was a separate control-flow omission. Startup, the order-intent check, account scope,
FIRST positions, portfolio, and the pre-SECOND eligibility check all succeeded. The runner then
skipped the mandatory DAILY-LOSS chain entirely, entered report fencing, and labeled final status
unavailable. `daily_loss.py` did not fail; it was never invoked. At 08:03 the runner copied the
canonical UUID validator correctly, completed DAILY-LOSS and the full scan/evaluator path, screened
15 candidates, and placed no order because every candidate failed a deterministic strategy gate.

No audited attempt reviewed, placed, cancelled, or otherwise mutated a broker order. The account
remained flat. The fencing, journals, deterministic consumers, and fail-closed terminal paths again
prevented unreliable runner glue from becoming an unsafe trade, but five failures in six attempts
is not acceptable unattended reliability.

**Rules produced:** lifecycle start now has one complete exact Codex cell beside the exact launcher
cell. It loads only machine-carried launcher state, invokes and drains the helper once, uses the one
canonical UUIDv4 validator, stores the complete unchanged receipt, reloads it, and emits only a
compact path- and ID-free result. Every fenced Codex block labeled EXACT is executable source and
may not be shortened, minified, reformatted, or regenerated. Regression coverage pins lifecycle
operation order, command and drain cardinality, the exact UUID validator, retained receipt, compact
output, and the helper's one-line stdout guarantee. An entry-eligible pre-SECOND result now has one
explicit next-action fence: SECOND renewal followed by the prescribed DAILY-LOSS operation; an
unattempted chain cannot be invented as a snapshot or final-status failure. The scheduled deployment
was corrected from Terra to the documented Sol high candidate and remains paused until the required supervised
`DRY_RUN = true` acceptance and an eligible scheduled-run acceptance succeed. A stronger model is
defense in depth, not a substitute for moving more orchestration into deterministic checked-in
code; the incident remains open until a clean early-session cohort proves the repaired boundary.

## 2026-08-27 09:19 PT CODEX SOL — VALID ORDER REVIEW, INVENTED ALERTS ENVELOPE

**APYX passed every deterministic entry gate, and Robinhood returned a valid $301.79 market-buy
review, but the runner rejected that successful response after guessing the wrong schema.** This
attempt used the intended `gpt-5.6-sol` model with high reasoning and had already crossed the
repaired startup boundaries. The complete committed response contained direct order identity,
`order_checks: {}`, quote data, and a nonempty market-data disclosure. Its save was journaled and
hash-bound successfully. There was no Robinhood outage, write failure, corrupt response, or
compliance alert.

After commit, model-authored JavaScript correctly found the response's direct `data` object and
then required `review.alerts` to be an array. The connector does not return that field: its current
contract is the `data.order_checks` object, where `{}` means clean and a nonempty object carries a
broker check under current `alert_type` or historical `alertType`. The invented `alerts[]`
requirement therefore converted a clean
review into `coordination-halt`. No order intent was prepared or begun, no placement or cancellation
was attempted, and the account remained flat. Final refresh, report/status publication, lease
release, and lifecycle finish all succeeded.

The 09:34 `overlap skipped` entry was not another defect. Updating/activating the automation around
09:15 launched an off-grid run whose lifecycle began at 09:19; that run renewed its lease at 09:33,
so the normal 09:30 schedule occurrence correctly declined to overlap it.

**Rule produced:** `connector_contract.py review` now owns the committed review response. It binds
the exact symbol, side, order type, exclusive quantity/dollar amount, and applicable limit/stop
price from the response and validates/carries the request's non-echoed session and time-in-force
through the intent journal's canonical order validator. It requires `data.order_checks` to be an
object; treats only an empty object as clean; preserves unknown nonempty checks as blocked;
normalizes only validated current `alert_type` and historical `alertType` aliases; requires the
nullable `quote_data` field; normalizes absent/null disclosure to null; and preserves empty or
nonempty disclosure strings exactly. Its Decimal-aware ASCII-safe receipt serializer also preserves
nested numeric check details as JSON numbers and Unicode disclosures across the Windows console
boundary. The routine explicitly forbids runner code from probing raw
MCP envelope, `alerts`, or `order_checks` fields. A matching clean helper receipt is mandatory
before `begin` or placement and normally precedes `prepare`; the named profit-take exception prepares
first so its protective stop can be cancelled safely, then requires the matching clean receipt
before `begin`. A semantic helper rejection after a successfully committed review cannot trigger
another broker read. A pre-commit review transport failure aborts its still-empty reservation and
fails that dependent order path without retry; the fractional-routing and profit-take settlement
corrections remain the two named new-review exceptions after valid broker responses.

## 2026-08-27 13:51 PT CODEX TERRA — EXACT LEASE BLOCK RE-AUTHORED AGAIN

**A valid lifecycle invocation reached preflight, then the runner changed the UUID validator in
the exact lease-acquisition cell and rejected its own valid state before the lock helper ran.**
The lifecycle invocation was `7483a5a7-32e3-4c1f-bd8f-fa3ac30a2293`. The checked-in routine used
the canonical UUIDv4 version group `-4[0-9a-f]{3}-`; Terra emitted
`-[89ab][0-9a-f]{3}-` in that position instead. The invocation's valid `4c1f` group therefore
failed the model-authored prerequisite with `validated launcher/config state is unavailable`.

This was not a lock, lifecycle, Python, Robinhood, or account failure. `run_lock.py acquire` and
`run_lifecycle.py bind-context` were never called. No lease or order intent existed, no broker tool
was invoked, no report/status file was created, and lifecycle safely finished as
`coordination-halt` / `coordination-state`. Terra identity telemetry was independently correct:
`codex` / `gpt-5.6-terra` / `reasoning=high`. The earlier 13:04 Terra run had also completed
normally, so this failure was not caused by an unknown model configuration.

**Rule produced:** UUID grammar no longer appears anywhere in model-authored routine JavaScript.
Checked-in producers and consumers remain the canonical UUID authorities; runner glue checks only
nonempty producer strings and exact cross-receipt equality. Startup lease acquisition and active
context binding now use one checked-in `run_lifecycle.py acquire-bind-context` action. It validates
the invocation and preflight phase in Python, calls the fenced SQLite acquisition, binds the
digest-only context receipt, returns the raw token only after both succeed, distinguishes an active
owner with a token-free result, and performs one owner-fenced compensating release if binding
fails. Regression coverage accepts the exact incident UUID, proves the success token is private,
proves overlap output is token-free, proves bind-failure cleanup, and proves a non-preflight
invocation cannot touch the lock. A process killed after acquisition can still leave the safe
time-bounded lease until expiry; no recovery path deletes or edits the database.

This extends the day's Terra cohort to eight attempts: two normal completions and six failed or
degraded paths. It confirms that labeling a block exact is not enough when the runner can recreate
its validation syntax. The maintained unattended candidate remains Sol high, and the new boundary
still requires supervised acceptance before live scheduling.

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
