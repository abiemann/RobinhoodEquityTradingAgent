# CLAUDE.md — working on RobinhoodEquityTradingAgent

Rules for any AI agent (or human) editing this repository.

## Tests are mandatory for script changes
After editing `evaluate_candidates.py` or `tools/price_band_scanner.py`, run:

```
python3 tests/test_scripts.py     # Windows: py -3 tests\test_scripts.py
```

All tests must pass (exit 0, "OK") before committing. The suite is stdlib-only — no installs needed. Expected values were verified against live API data; if an intentional behavior change breaks a test, update the expectation deliberately and say so in the commit — never delete or weaken a test to go green.

## The deterministic layer is sacred
The markdown documents (`robinhood-momentum-routine-autonomous.md`, `tools/PriceBandScanner.md`) are executed by LLM agents each run; the Python scripts exist so that all math is deterministic and tested. Never move logic from the scripts back into the documents, and never let a document instruct an agent to re-implement script math ad hoc (a documented fallback for a missing/broken script is the only exception).

## Documentation sync
When a change to a routine document alters behavior that README.md describes (constants, run order, guardrails, tools), update README.md in the same commit. The README Configuration table deliberately lists EVERY constant.

## Local-only files
`run-reports/`, `tools/logs/`, `trade-ledger.csv`, and `tmp_*` are gitignored on purpose — they contain account activity or are regenerated. Never commit them or weaken `.gitignore`.

## DRY_RUN must be true in every commit
`Constants.md` is committed, but its `DRY_RUN` value must read `true` in every commit — the safe default for anyone cloning. The user trades live via a LOCAL, uncommitted `DRY_RUN = false` edit. Before committing `Constants.md`, check that line: if it reads `false`, temporarily set it to `true`, commit, then restore `false` in the working tree. Never publish `false`. Do the flip with a precise text edit of that ONE line — never by round-tripping the whole file through PowerShell `Get-Content`/`Set-Content`, which mis-decodes UTF-8 and corrupts every non-ASCII character (happened 2026-07-16).
