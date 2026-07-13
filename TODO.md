# TODO — roadmap

## 2.0 — Strategy skills: selectable trading patterns

Split the single baked-in strategy into pluggable pattern "skills", so the user selects which pattern the agent trades:

```
skills/
├── cup-and-handle/
│   ├── SKILL.md          ← criteria, decision rules, when NOT to buy
│   └── examples/         ← RAW VALUES, not images (token-efficient, machine-checkable)
│       ├── valid-setup-1.json      (bar series + one-line annotation of why it qualifies)
│       ├── failed-on-news.json     (bar series + why it must NOT be bought)
│       └── unstable-handle.json
└── buy-the-dip/
    ├── SKILL.md
    └── examples/
```

Design notes (from 2026-07-12 discussion):

- **Platform vs. strategy split.** The routine document becomes the PLATFORM — account scope, session rules, order handling, guards, ledger, dust sweep, report — and Step 8–10's entry criteria move into the active skill's `SKILL.md`. Today's dip logic becomes the first skill (`buy-the-dip`), extracted, not rewritten.
- **Selection — one pattern or a list.** A new constant `ACTIVE_PATTERNS` (e.g. `"buy-the-dip"` or `"buy-the-dip, cup-and-handle"`) names the skill folders in PRIORITY ORDER; the run loads each listed skill's `SKILL.md` and halts entries if any is missing. A candidate qualifies when it matches ANY active pattern; if several match, the first in the list wins attribution. Interactive sessions can present the discovered `skills/` folders as a clickable (multi-select) choice via AskUserQuestion; scheduled runs read the constant only.
- **Examples are raw bar series, not images — and mostly NEGATIVE.** Each example file holds the numeric bars (OHLCV) of a known setup plus a one-line annotation. The high-value data is the failures: keep at least 2 negative examples per positive one, each annotated with WHICH disqualifier killed it (`failed-on-news`, `unstable-handle`, `earnings-in-window`, …). An LLM judge is biased toward finding the pattern it was asked about; a negative-heavy corpus is the counterweight against false positives. The agent pattern-matches each surviving candidate's actual bars against the active skills — deterministic `check.py` first, then judgment informed by the example series as numeric few-shot references.
- **Explicit disqualifiers are first-class — and default-deny.** Every SKILL.md carries a "Do NOT signal buy if" section, e.g.: earnings inside the handle window (checkable broker-native via `get_earnings_calendar`), pending macro event (FOMC — needs a deterministic date source, e.g. a static annual table in the skill), handle drifting downward more than X% (bars-derived, lives in `check.py`). Every machine-checkable disqualifier MUST be in `check.py`, not just prose. The verdict stance is default-deny: a candidate is disqualified unless every criterion passes AND no disqualifier fires — the burden of proof is on the pattern, never on the skip.
- **The deterministic layer stays sacred** (per CLAUDE.md): every quantifiable criterion in a SKILL.md (cup depth %, handle drift bounds, dip %) also ships as a per-skill checker script (`skills/<name>/check.py`) with tests in `tests/`. Prose explains judgment; scripts compute numbers; vision breaks ties.
- **Risk stays platform-level.** Skills define ENTRY criteria only. Stops, sizing, downsizing, cooldowns, circuit breakers, and guards are invariant across patterns — a skill can tighten them, never loosen them.
- **Ledger attribution.** Each fill's `reason` records WHICH pattern matched (e.g. `cup-and-handle`), so `trade-ledger.csv` supports per-pattern win rate and expectancy — with multiple active patterns, comparison becomes measurable, not anecdotal.
- **Data source: the broker itself — fully hands-off.** Bars come from `get_equity_historicals` (the raw-response → script pipeline Step 8 already uses; schema live-verified, interpolated-bar handling exists). No manual exports ever. Each SKILL.md declares its required lookback (a cup-and-handle needs ~60–90 daily bars; a dip needs ~30) so the platform fetches the right window per active skill.
- **Two entry points, one code path.** The same skill serves (a) the scheduled pipeline — candidates that survive platform filters get pattern-checked automatically — and (b) an interactive verdict mode: ask "check TICKER for a valid cup-and-handle" and the agent fetches the bars, runs `skills/cup-and-handle/check.py`, weighs the example series, and returns a verdict with reasons. Same checker, same examples, no divergence between what you can ask and what the robot trades.
