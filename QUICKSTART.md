# Quick start

> **This is real-money trading software.** Never paste a password, MFA code, brokerage account number, OAuth secret or token, phone QR code, or private pairing link into chat. Complete sign-in only in Robinhood's official browser flow.

## 1. Connect Robinhood once

The agent cannot access Robinhood until its MCP connector is added and authorized. Inspect the installed connectors first: exactly one Robinhood connector should exist, and setup must never create a duplicate.

- **ChatGPT Desktop in Codex mode, or Codex:** in the desktop build observed for this project, open **Settings → Plugins → MCPs**. Labels and the exact settings path may vary by app version, so use the [README first-time app setup](README.md#first-time-app-setup) for the current details. If exactly one Robinhood connector already exists, select it and choose **Authenticate** or **Re-authenticate**; do not add another. Add one only if no Robinhood connector exists.
- **Claude Code:** first inspect the account connector in Claude Desktop under **Customize → Connectors** (or **Settings → Connectors**). If one Robinhood connector exists, keep that one and complete or renew its account authorization; if none exists, add exactly one custom connector and complete OAuth. If more than one exists, stop and ask which known connector to keep, then have the user remove the duplicates explicitly; never silently delete an unknown connector. Restart Claude, then open a brand-new **Code** session. Set the **Environment** selector above the prompt to **Local**, select this exact repository's main checkout, keep worktree isolation **off**, and open `/mcp` to select the Robinhood server and authenticate or re-authenticate it. The **Local** choice in the Code sidebar is only a list filter and is not proof that the new session is Local.

If no Robinhood connector exists and you add one, name it `Robinhood Trader` and use this server URL:

`https://agent.robinhood.com/mcp/trading`

Complete Robinhood's official authorization flow, then restart the app and open a brand-new task/session in the exact project context above. Require `get_accounts` there before any broker work. If it is still absent after authenticating or re-authenticating the one existing connector, remove only that single connector, add it back once, complete OAuth, restart the app, and verify `get_accounts` in another fresh task/session. Never leave or create a duplicate, and do not attempt broker work until this check succeeds.

**Write down the exact display name of your Agentic account**—often simply `Agentic`. The setup prompt will verify that name and write it into `AGENTIC_ACCOUNT_NAME` in `constants.md`; never use or copy the account number.

## 2. Paste one prompt

Open this project in **ChatGPT Desktop in Codex mode or Codex**, or in a brand-new Claude Desktop **Code** session whose **Environment** selector above the prompt is **Local** with native Windows project access, then paste the prompt below. Do not treat an ordinary ChatGPT chat as equivalent to Codex mode. In Claude Code, select the exact main checkout and keep worktree isolation **off** so the session sees its local uncommitted configuration and shared gitignored runtime state; the sidebar's Local filter alone is insufficient. Do not use Claude Cowork/local-agent, Remote/Cloud, or WSL access to this Windows checkout for the live routine: those environments can interpose filesystem semantics that are unsafe for the project's SQLite coordination state. The assistant handles the repository, a verified available Python runtime, validation, tests, and setup; you should need to act only for an unavoidable safety decision.

**Local sensitive-temp data:** the retained `broker_snapshot.py preflight --create-scratch` command first creates and proves run scratch in one operation; the automation binds its exact path and ID only from that validated receipt and never authors or recopies the random path. Each run also creates one invocation-bound native-temp `SOURCE_ROOT` containing broker, scan, and historical JSON. The first successful `get_accounts` result is written with the routine's exact full-object `JSON.stringify`/`JSON.parse`/zero-prefix save recipe and consumed by `broker_snapshot.py bind-transport --account-name <validated AGENTIC_ACCOUNT_NAME>`; that helper validates live `agentic_allowed`, returns the matched account scope in its stable receipt, and privacy-deletes the canary, so the routine never re-derives an account number from model-visible text or makes a second lookup. Every later response repeats the same serializer and bound root—no BOM, label, fence, alternate writer, or retry. Other source files are local-only, never served or committed, but can remain after a Local run or crash. Remove them only through the future deterministic cleanup/helper, or manually after every runner is stopped and the exact current-run paths have been verified. Never ask a model to improvise a recursive temp deletion.

```text
Set up RobinhoodEquityTradingAgent and perform one safe first test for me:
https://github.com/abiemann/RobinhoodEquityTradingAgent.git

I am non-technical. Do the technical work yourself, keep updates concise, and ask me to act only when official browser authorization, a required Robinhood UI change, or a real-money-risk decision makes that unavoidable. Do not ask me to run commands that you can run yourself.

Safety requirements:

- Never ask me to paste passwords, MFA codes, account numbers, OAuth files, secrets or tokens, phone QR codes, or private pairing links into chat.
- Keep `DRY_RUN = true`. Never enable live trading, commit or push `DRY_RUN = false`, weaken tests, change strategy rules or other trading constants, or loosen approval gates. The only configuration edits this setup authorizes are changing `DRY_RUN` from `false` to `true` if necessary and writing the confirmed `AGENTIC_ACCOUNT_NAME` described below. Never change `DRY_RUN` from `true` to `false`, and re-run validation after either authorized edit.
- Neither `place_equity_order` nor `cancel_equity_order` may be preapproved. In Codex, keep both set to `Needs approval`. In Claude, the control may be labeled `Auto`; inspect the resulting `Allowed permissions` and require both mutation tools to remain approval-gated. If that cannot be guaranteed, stop before running the routine.
- Dry run prevents new entries, but it may still sell or protect existing positions and modify related orders.
- Do not create or enable a schedule. Scheduling and live trading require separate, later consent.
- Preserve all existing files and local changes. If a safety check fails, stop rather than bypass it.

Proceed autonomously:

1. Confirm that `get_accounts` is available in this brand-new task/session. If it is not, follow the idempotent connector instructions above and stop. Do not claim setup succeeded or attempt broker work until exactly one connector is authorized, the app has restarted, and `get_accounts` works in another fresh task/session.
2. If this exact repository checkout is already open as the current native project, use it. Otherwise open a safe writable parent folder, clone into a new subfolder without overwriting anything, and then stop before lifecycle or broker work so I can open that cloned repository in a brand-new native project session. Do not continue from the parent-folder session. In Claude Desktop Code, set the **Environment** selector above the prompt to **Local**, select the exact cloned main checkout, and keep worktree isolation off; the sidebar Local filter is insufficient. Read `AGENTS.md`, the relevant README setup and safety sections, and `robinhood-momentum-routine-autonomous.md`.
3. Before any lifecycle command, require a host-native project/runtime pair. On native Windows, execute exactly `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ./resolve_python.ps1`. Require its valid JSON receipt, bind the exact returned absolute Python 3 path as `PYTHON_EXE`, and reuse that exact executable with current-shell literal quoting for validation, the full test suite, and every later checked-in Python helper. Never substitute a bare `python`, `python3`, `py`, or a generic “Windows equivalent.” Native Git Bash still invokes this Windows resolver and then executes its returned Windows path without PowerShell's leading `&`. If this Windows checkout is exposed through a POSIX/FUSE sandbox or `powershell.exe` is unavailable, do not fall back to `/usr/bin/python3`: stop before lifecycle and broker work, close the wrong context, and start a brand-new native project session on the exact repository. For Claude, that means a new Desktop Code session with the Environment selector above the prompt set to Local, the exact main checkout selected, and worktree isolation off; the sidebar Local filter alone is insufficient. A genuinely native Linux/macOS checkout may instead launch-probe and bind an absolute host `python3` under the same reuse rule, but that does not validate the Claude Windows Desktop scheduler path. Execute `validate_constants.py --json` with the bound `PYTHON_EXE` and require `DRY_RUN` to be exactly `true`. If it is `false`, change only that row from `false` to `true` and re-run validation. For any other validation failure, diagnose and stop; do not alter safety checks to make them pass.
4. Call `get_accounts` read-only without repeating or storing account numbers. If there are zero agentic-enabled accounts, stop. If exactly one exists, show me only its display name and ask me to confirm it; if several exist, show only their display names and ask which one to use. The chosen display name must then resolve to exactly one account and that account must be agentic enabled; otherwise stop with no default, partial-match, first-account, or account-number fallback. After confirmation, write the exact name into `AGENTIC_ACCOUNT_NAME` in `constants.md` as a local, uncommitted edit. Re-run `validate_constants.py --json` with the same bound `PYTHON_EXE` and require success. Do not continue until the confirmed name and validated constant match exactly.
5. Using the exact already-bound `PYTHON_EXE`, run the complete suite as `'<PYTHON_EXE>' -m unittest discover -s tests` with the current shell's required command prefix and literal quoting. Do not use a bare launcher or invent an ad-hoc serializer, path, or extra broker call as a setup proof. The first supervised entry-eligible run with `DRY_RUN = true` remains the end-to-end proof of broker-response staging. If the suite fails, diagnose and stop.
6. Verify the permission outcome yourself: neither `place_equity_order` nor `cancel_equity_order` is preapproved. In Codex require `Needs approval` for both. In Claude, where the control may say `Auto`, inspect `Allowed permissions` and confirm both tools remain approval-gated. If you cannot inspect or guarantee that outcome, tell me exactly where to check and stop before running the routine.
7. Using read-only Robinhood tools, verify again that the confirmed `AGENTIC_ACCOUNT_NAME` resolves to exactly one agentic-enabled account. Do not repeat account numbers, balances, holdings, or order details in chat. Report only whether the exact match succeeded and whether positions or open orders exist; otherwise stop with no fallback.
8. Verify the saved scan named by `SCAN_TITLE` and its required columns: `Last`, `Relative volume`, `% Change`, and `Volume`. Creating a missing saved scan is a broker-side setup mutation: explain that change and obtain my explicit confirmation before creating it with a documented tool. If creation is unsupported or columns require a manual Robinhood UI change, give me only those steps and wait.
9. If positions or open orders exist, explain that a dry run may modify or sell them and ask for explicit confirmation before running anything. If the matched account is flat with no open orders, this prompt authorizes one supervised execution of the checked-in routine with `DRY_RUN = true`.
10. Summarize the first-test result in plain language. Confirm that live mode remains off, the account-specific name remains local and uncommitted, and no schedule was created. Then offer, but do not start, either the dashboard and phone viewer or later scheduling guidance.
```

The first test cannot place a new entry because `DRY_RUN` remains on. If the account already holds positions or has open orders, the assistant pauses first because protective management can still perform real broker actions.

For configuration details, troubleshooting, dashboard setup, and the separate path to scheduling or live trading, see [README.md](README.md).

Only after the safe first test succeeds and you separately consent to scheduling, follow [Schedule the agent with Claude Desktop Local](CLAUDE-LOCAL-SCHEDULING.md).

## Timing for later scheduled runs

When you later create a scheduled task, copy exactly one matching declaration into its task prompt:

- Codex: `TIMING_IDENTITY: runner=codex model=gpt-5.6-luna config=reasoning=high`
- Claude Desktop Code Local (current Sonnet 5 setup): `TIMING_IDENTITY: runner=claude model=claude-sonnet-5 config=effort=high`

Keep that one line synchronized with the runner, model, and configuration actually selected in the task settings; if a selection changes, update the line before the next run. It records what ran and does not select or switch the model. The routine records Routine total, Strategy execution, and Routine overhead. Immediately before its final on-screen Run Summary, the same `record-internal` host-clock reading also records **Comparable run duration** from lifecycle-bound START CLOCK through `final-summary-boundary` and prints exact Run start/Run end timestamps plus the helper-formatted duration. Those automatic boundaries are identical on Claude and Codex and do not change the saved report or status schema. A source-specific **Reference run duration** can still be attached with the optional post-run observation below.

Before START CLOCK, the routine also makes one structured self-report from only identity that the current framework explicitly exposes and passes it through the checked-in exact-match registry. Complete direct task metadata is strongest, then the declaration above. If neither exists, the Python resolver—not the model or routine—reads only the allowlisted `CLAUDECODE` runtime marker and `CLAUDE_EFFORT` setting with exact `os.environ.get` calls and may combine that evidence with a registry-recognized self-report. It never enumerates or persists the environment. Runtime evidence is inherited/spoofable corroboration rather than authentication and does not expose Claude's exact model, so a system-prompt model combined with runtime runner/configuration becomes field-level `composite` and remains unverified. The projection records runner, model, and configuration provenance separately. Any unknown field, self-reported field, or conflict excludes the record from primary fair-comparison cohorts until independent evidence replaces it. Keep the synchronized declaration: it is the strongest symmetric comparison source for Claude and Codex and normally the only complete source for the selected configuration. Identity resolution is observational only and cannot change trading or lifecycle behavior.

After the run is complete, resolve and bind the checked-in resolver's exact Python path as `PYTHON_EXE`, replace `<INVOCATION_ID>` and `<DURATION_MS>`, and copy exactly one matching PowerShell command. For Codex's displayed **Worked for** duration:

```powershell
& '<PYTHON_EXE>' run_performance.py observe-task --invocation-id '<INVOCATION_ID>' --task-duration-ms <DURATION_MS> --runner codex --model 'gpt-5.6-luna' --configuration 'reasoning=high' --identity-source manual-ui --clock-source codex-worked-for
```

For Claude's recorded run duration:

```powershell
& '<PYTHON_EXE>' run_performance.py observe-task --invocation-id '<INVOCATION_ID>' --task-duration-ms <DURATION_MS> --runner claude --model 'claude-sonnet-5' --configuration 'effort=high' --identity-source manual-ui --clock-source claude-run-duration
```

Use these `manual-ui` templates only for the named human-observed source; do not relabel runner metadata. The attached value is displayed as **Reference run duration** and remains secondary to the automatic **Comparable run duration**; it is fallback/context for historical or incomplete runs. Fair performance comparisons require the same automatic boundary, session class, workload path, configuration cohort, and preferably rules version. Keep runner/model identity explicit as the comparison dimension. See [README Tested On](README.md#tested-on) for the formulas, source rules, POSIX-shell form, and local comparison dashboard.
