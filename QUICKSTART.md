# Quick start

Open this project in **ChatGPT, Codex, Claude, or another capable LLM environment** and paste the prompt below. The assistant will verify that the current environment can use project files, a shell, custom MCP connectors, and tool approvals. It then handles the repository, its included Python, validation, tests, and setup; you should need to act only for Robinhood sign-in or an unavoidable safety decision.

> **This is real-money trading software.** Never paste a password, MFA code, brokerage account number, OAuth secret or token, phone QR code, or private pairing link into chat. Complete sign-in only in Robinhood's official browser flow.

```text
Set up RobinhoodEquityTradingAgent and perform one safe first test for me:
https://github.com/abiemann/RobinhoodEquityTradingAgent.git

I am non-technical. Do the technical work yourself, keep updates concise, and ask me to act only when official browser authorization, a required Robinhood UI change, or a real-money-risk decision makes that unavoidable. Do not ask me to run commands that you can run yourself.

Safety requirements:

- Never ask me to paste passwords, MFA codes, account numbers, OAuth files, secrets or tokens, phone QR codes, or private pairing links into chat.
- Keep `DRY_RUN = true`. Never enable live trading, commit or push `DRY_RUN = false`, weaken tests, change strategy rules or constants, or loosen approval gates.
- Keep every order-placement or order-cancellation tool set to `Needs approval`.
- Dry run prevents new entries, but it may still sell or protect existing positions and modify related orders.
- Do not create or enable a schedule. Scheduling and live trading require separate, later consent.
- Preserve all existing files and local changes. If a safety check fails, stop rather than bypass it.

Proceed autonomously:

1. Use the project already open if present. Otherwise clone it into a safe new writable subfolder without overwriting anything. Read `AGENTS.md`, the relevant README setup and safety sections, and `robinhood-momentum-routine-autonomous.md`.
2. Use the coding environment's included Python 3 runtime. Run `validate_constants.py --json`, confirm `DRY_RUN` is exactly `true`, then run the complete test suite with `python3 -m unittest discover -s tests` or the environment's Windows equivalent. If validation or tests fail, diagnose and stop; do not alter safety checks to make them pass.
3. Verify that the runner can save unchanged Robinhood MCP results to temporary files as the routine requires.
4. Detect whether the Robinhood MCP connector is already available. If not, give me one short set of exact setup steps using `https://agent.robinhood.com/mcp/trading`, then wait while I authorize it in Robinhood's official browser flow and restart the app if required.
5. Verify the order-mutation approval settings yourself. If you cannot inspect them, tell me exactly where to confirm `Needs approval`, then wait.
6. Using read-only Robinhood tools, verify exactly one agentic account matches `AGENTIC_ACCOUNT_NAME`. Do not repeat account numbers, balances, holdings, or order details in chat. Report only whether the match succeeded and whether positions or open orders exist.
7. Verify the saved scan named by `SCAN_TITLE` and its required columns: `Last`, `Relative volume`, `% Change`, and `Volume`. Create the missing saved scan if the documented tools support that safely. If columns require a manual Robinhood UI change, give me only those steps and wait.
8. If positions or open orders exist, explain that a dry run may modify or sell them and ask for explicit confirmation before running anything. If the matched account is flat with no open orders, this prompt authorizes one supervised execution of the checked-in routine with `DRY_RUN = true`.
9. Summarize the first-test result in plain language. Confirm that live mode remains off and no schedule was created. Then offer, but do not start, either the dashboard and phone viewer or later scheduling guidance.
```

The first test cannot place a new entry because `DRY_RUN` remains on. If the account already holds positions or has open orders, the assistant pauses first because protective management can still perform real broker actions.

For configuration details, troubleshooting, dashboard setup, and the separate path to scheduling or live trading, see [README.md](README.md).
