# Quick start

> **This is real-money trading software.** Never paste a password, MFA code, brokerage account number, OAuth secret or token, phone QR code, or private pairing link into chat. Complete sign-in only in Robinhood's official browser flow.

## 1. Connect Robinhood once

The agent cannot access Robinhood until its MCP connector is added and authorized:

- **ChatGPT or Codex:** open **Settings → Plugins → MCPs → Add → Add MCP server**. Choose **Streamable HTTP**.
- **Claude:** open **Settings → Connectors → Add → Add custom connector**.

Name it `Robinhood Trader` and use this server URL:

`https://agent.robinhood.com/mcp/trading`

Complete Robinhood's official authorization flow, then restart or reopen the LLM app so the connector loads. A successful connection exposes tools such as `get_accounts`.

**Write down the exact display name of your Agentic account**—often simply `Agentic`. The setup prompt will verify that name and write it into `AGENTIC_ACCOUNT_NAME` in `constants.md`; never use or copy the account number.

## 2. Paste one prompt

Open this project in **ChatGPT, Codex, Claude, or another capable LLM environment** and paste the prompt below. The assistant handles the repository, its included Python, validation, tests, and setup; you should need to act only for an unavoidable safety decision.

```text
Set up RobinhoodEquityTradingAgent and perform one safe first test for me:
https://github.com/abiemann/RobinhoodEquityTradingAgent.git

I am non-technical. Do the technical work yourself, keep updates concise, and ask me to act only when official browser authorization, a required Robinhood UI change, or a real-money-risk decision makes that unavoidable. Do not ask me to run commands that you can run yourself.

Safety requirements:

- Never ask me to paste passwords, MFA codes, account numbers, OAuth files, secrets or tokens, phone QR codes, or private pairing links into chat.
- Keep `DRY_RUN = true`. Never enable live trading, commit or push `DRY_RUN = false`, weaken tests, change strategy rules or trading constants, or loosen approval gates. The only configuration edit allowed during setup is the verified `AGENTIC_ACCOUNT_NAME` described below.
- Keep every order-placement or order-cancellation tool set to `Needs approval`.
- Dry run prevents new entries, but it may still sell or protect existing positions and modify related orders.
- Do not create or enable a schedule. Scheduling and live trading require separate, later consent.
- Preserve all existing files and local changes. If a safety check fails, stop rather than bypass it.

Proceed autonomously:

1. Confirm that the authorized Robinhood MCP connector is available now. If it is not, give me the one-time connector instructions from `QUICKSTART.md` and stop. Do not claim setup succeeded or attempt broker work until I restart or reopen the app and return with the connector loaded.
2. Use the project already open if present. Otherwise clone it into a safe new writable subfolder without overwriting anything. Read `AGENTS.md`, the relevant README setup and safety sections, and `robinhood-momentum-routine-autonomous.md`.
3. Use the coding environment's included Python 3 runtime. Run `validate_constants.py --json` and confirm `DRY_RUN` is exactly `true`. If validation fails, diagnose and stop; do not alter safety checks to make them pass.
4. Call `get_accounts` read-only. Find the exact display name of the agentic-enabled account without repeating or storing its account number. If exactly one exists, show me only that name and ask me to confirm it; if several exist, show only their names and ask which one to use. After confirmation, write the exact name into `AGENTIC_ACCOUNT_NAME` in `constants.md` as a local, uncommitted edit. Re-run `validate_constants.py --json` and require success. Do not continue until the confirmed name and validated constant match exactly.
5. Run the complete test suite with `python3 -m unittest discover -s tests` or the environment's Windows equivalent. Verify that the runner can save unchanged Robinhood MCP results to temporary files as the routine requires. If either check fails, diagnose and stop.
6. Verify the order-mutation approval settings yourself. If you cannot inspect them, tell me exactly where to confirm `Needs approval`, then wait.
7. Using read-only Robinhood tools, verify that the confirmed `AGENTIC_ACCOUNT_NAME` still resolves to exactly one agentic-enabled account. Do not repeat account numbers, balances, holdings, or order details in chat. Report only whether the match succeeded and whether positions or open orders exist.
8. Verify the saved scan named by `SCAN_TITLE` and its required columns: `Last`, `Relative volume`, `% Change`, and `Volume`. Create the missing saved scan if the documented tools support that safely. If columns require a manual Robinhood UI change, give me only those steps and wait.
9. If positions or open orders exist, explain that a dry run may modify or sell them and ask for explicit confirmation before running anything. If the matched account is flat with no open orders, this prompt authorizes one supervised execution of the checked-in routine with `DRY_RUN = true`.
10. Summarize the first-test result in plain language. Confirm that live mode remains off, the account-specific name remains local and uncommitted, and no schedule was created. Then offer, but do not start, either the dashboard and phone viewer or later scheduling guidance.
```

The first test cannot place a new entry because `DRY_RUN` remains on. If the account already holds positions or has open orders, the assistant pauses first because protective management can still perform real broker actions.

For configuration details, troubleshooting, dashboard setup, and the separate path to scheduling or live trading, see [README.md](README.md).
