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
