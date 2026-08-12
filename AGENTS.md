# AGENTS.md — working on RobinhoodEquityTradingAgent

Rules for any AI agent (or human) editing this repository.

## Tests are mandatory for script changes
After editing `resolve_python.ps1`, `validate_constants.py`, `broker_snapshot.py`, `daily_loss.py`, `evaluate_candidates.py`, `filter_scan.py`, `market_clock.py`, `market_calendar.py`, `run_lock.py`, `run_lifecycle.py`, `run_performance.py`, `status_snapshot.py`, `order_intents.py`, `dashboard/serve.py`, `dashboard/index.html`, `robinhood-momentum-routine-autonomous.md`, or `tools/price_band_scanner.py`, run:

```
python3 -m unittest discover -s tests     # Windows: py -3 -m unittest discover -s tests
```

Run this from the repository root. It discovers **every** `tests/test_*.py` — the trading scripts plus the phone-share, Drive, credential-store, ledger-P&L and dashboard suites. Do NOT substitute `python3 tests/test_scripts.py`: that file is the largest suite but not the only one, and running it alone silently skips the rest. Any new test file must be importable on its own too — put the repo root on `sys.path` at the top of it, the way the existing files do:

```python
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
```

All tests must pass (exit 0, "OK") before committing. The suite is stdlib-only — no installs needed. Expected values were verified against live API data; if an intentional behavior change breaks a test, update the expectation deliberately and say so in the commit — never delete or weaken a test to go green.

## Read INCIDENTS.md before changing a rule
`INCIDENTS.md` holds the history behind the routine's rules — the live failures, dollar losses, and near-misses that produced them. The routine document itself is read by an LLM on all ~16 runs a day, so it carries rules only; the provenance lives in INCIDENTS.md and the runtime never loads it.

Before removing or "simplifying" anything in `robinhood-momentum-routine-autonomous.md` that looks redundant, over-specific, or paranoid, check INCIDENTS.md for its entry — most of those oddities were written after something broke. When you add a rule because something broke, put the rule in the routine and the story in INCIDENTS.md; when you remove a rule, remove its entry too.

## The deterministic layer is sacred
The markdown documents (`robinhood-momentum-routine-autonomous.md`, `tools/PriceBandScanner.md`) are executed by LLM agents each run; the Python scripts exist so that all math is deterministic and tested. Never move logic from the scripts back into the documents, and never let a document instruct an agent to re-implement script math ad hoc (a documented fallback for a missing/broken script is the only exception).

## Documentation sync
When a change to a routine document alters behavior that README.md describes (constants, run order, guardrails, tools), update README.md in the same commit. The README Configuration table deliberately lists EVERY constant.

## Local-only files
`run-reports/`, `tools/logs/`, `trade-ledger.csv`, and `tmp_*` are gitignored on purpose — they contain account activity or are regenerated. Never commit them or weaken `.gitignore`.

## constants.md is maintainer-controlled
No pull request may add, modify, delete, rename from, or rename to the root `constants.md` file. The required `safety/committed-dry-run` check runs from trusted base-branch code and rejects every PR whose final diff touches that file, regardless of its contents. A developer who wants a checked-in constant changed must contact the maintainer first so the change can be discussed and handled outside the PR.

The committed copy remains the safe default for anyone cloning: `DRY_RUN` must read `true`. The user trades live via a LOCAL, uncommitted `DRY_RUN = false` edit. Never stage or publish that local edit. If the maintainer explicitly authorizes a direct `constants.md` commit, change only the `DRY_RUN` line to `true` for the commit, then restore `false` locally. Never round-trip the whole file through PowerShell `Get-Content`/`Set-Content`, which mis-decodes UTF-8 and corrupts every non-ASCII character (happened 2026-07-16).
