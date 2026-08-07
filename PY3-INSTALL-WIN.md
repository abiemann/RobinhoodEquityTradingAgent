# Python 3 install (Windows)

The agent's own `python3` lives in the sandbox its runner provides — you do not install that one. You need Python on your **own** machine for two things: running the test suite before committing a script change, and running the [Dashboard](README.md#tools).

**Windows does not ship Python, and PowerShell does not include it.** On a fresh machine `python` and `python3` are Microsoft Store *App Execution Alias* stubs — they open the Store instead of running anything, and print:

```
Python was not found; run without arguments to install from the Microsoft Store
```

That message means you hit the alias, not that an install is broken. It is worth recognising, because it looks like a failure rather than a missing program.

1. Install from [python.org/downloads](https://www.python.org/downloads/) — any current 3.x.
2. Keep the **py launcher** option enabled (it is on by default). That is what provides the `py` command these docs use on Windows.
3. Verify in PowerShell — this should print a version, not a Store message:

```
py -3 --version
```

Then use `py -3` anywhere these docs show `python3` — e.g. `py -3 tests\test_scripts.py` or `py -3 dashboard\serve.py`. There is nothing else to install: every script here is standard library only.

Optionally, to stop the Store stubs shadowing your real install, turn them off under **Settings → Apps → Advanced app settings → App execution aliases**.

On macOS and Linux `python3` is normally already present — check with `python3 --version` — and the commands work as written.

## Windows and Codex shell troubleshooting

The PowerShell window opened by the user and the shell used by Codex or a scheduled task may be different environments. It is therefore possible for this to succeed in ordinary PowerShell:

```
PS D:\Projects\RobinhoodEquityTradingAgent> py -3 --version
Python 3.13.7
```

while the same command reports `No installed Python found!` inside a restricted agent shell. That does not mean Python is missing or needs to be reinstalled; the restricted shell cannot see the host Python installation or its launcher registry. Do not work around this by replacing the clock with PowerShell date math or by using `python`/`python3` blindly.

Use the following decision path:

1. Verify `py -3 --version` in the same environment that will run the routine.
2. If it succeeds, use `py -3` for manual tests and the dashboard. Scheduled runs still use the checked-in resolver below so every helper shares one verified absolute interpreter.
3. If it fails only inside Codex, do not try bare `python`, `python.exe`, `where python`, or the first result from `Get-Command python`: Windows commonly returns a zero-byte Microsoft Store alias under `Microsoft\WindowsApps`, and resolving that path does not prove it can launch.
4. Scheduled Windows runs use the checked-in resolver before lifecycle startup:

   ```powershell
   powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File .\resolve_python.ps1
   ```

   It returns one compact JSON object containing a launch-probed absolute Python 3 path. It checks the current Codex runtime and normal local installation locations without hardcoding a Python version, rejects Store aliases, and exhausts its permitted candidates before reporting failure. The routine binds that returned path once and reuses it for every helper.
5. When Codex's `load_workspace_dependencies` lookup returns promptly, the routine passes its exact Python path to the resolver as `-PreferredPath`. If the lookup stalls, the resolver still discovers the bundled runtime from the current environment/cache. An explicit sandbox ACL/access-denied failure gets one narrowly scoped host-capable retry of this read-only resolver before the run may halt.
6. If it fails in ordinary PowerShell too, install Python from [python.org/downloads](https://www.python.org/downloads/), keep the `py` launcher enabled, and verify again.

This environment difference is a sandbox boundary, not a project dependency issue. The scripts use Python's standard library only; no `pip install` step is required.
