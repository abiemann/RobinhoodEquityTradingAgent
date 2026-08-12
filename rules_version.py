#!/usr/bin/env python3
"""Resolve the routine's privacy-safe rule-set version deterministically.

The trading routine must never interpret ``git status`` or ``git describe``
itself. This helper returns the latest short commit touching the canonical
rule-set files and appends ``-dirty`` when any of those files differs from
HEAD. The one supported exception is the maintainer's local, uncommitted
``constants.md`` transition from ``DRY_RUN = true`` to ``false``.

Git metadata is telemetry, not a trading prerequisite. Missing or unusable
Git state therefore returns ``unknown`` with a successful, strict JSON
envelope instead of blocking a run.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Sequence


SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parent
RULE_SET_FILES = (
    "robinhood-momentum-routine-autonomous.md",
    "constants.md",
    "validate_constants.py",
    "broker_snapshot.py",
    "status_snapshot.py",
    "run_lifecycle.py",
    "rules_version.py",
    "order_intents.py",
    "ledger_pnl.py",
)
_HASH_RE = re.compile(r"^[0-9a-f]{4,40}$")
_DRY_RUN_TRUE_RE = re.compile(
    r"^(\|\s*`DRY_RUN`\s*\|\s*)`true`(\s*\|.*)$"
)


class RulesVersionError(RuntimeError):
    """Git state could not be interpreted safely."""


def _run_git(root: Path, arguments: Sequence[str]) -> str:
    command = [
        "git",
        "--no-optional-locks",
        "-C",
        str(root),
        *arguments,
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RulesVersionError(f"git unavailable: {exc}") from exc
    if completed.returncode != 0:
        raise RulesVersionError("git command failed")
    return completed.stdout


def _only_expected_live_mode_edit(
    root: Path,
    status_lines: list[str],
    git: Callable[[Path, Sequence[str]], str],
) -> bool:
    if len(status_lines) != 1:
        return False
    status = status_lines[0]
    if (
        len(status) < 4
        or status[:2] != " M"
        or status[3:] != "constants.md"
    ):
        return False
    try:
        committed = git(root, ("show", "HEAD:constants.md"))
        current = (root / "constants.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError, RulesVersionError):
        return False

    committed_lines = committed.splitlines()
    current_lines = current.splitlines()
    if len(committed_lines) != len(current_lines):
        return False
    changed = [
        index
        for index, (before, after) in enumerate(
            zip(committed_lines, current_lines)
        )
        if before != after
    ]
    if len(changed) != 1:
        return False
    index = changed[0]
    match = _DRY_RUN_TRUE_RE.fullmatch(committed_lines[index])
    if match is None:
        return False
    return current_lines[index] == (
        f"{match.group(1)}`false`{match.group(2)}"
    )


def resolve_rules_version(
    root: Path = ROOT,
    *,
    git: Callable[[Path, Sequence[str]], str] = _run_git,
) -> str:
    """Return ``<short-hash>``, ``<short-hash>-dirty``, or ``unknown``."""

    try:
        revision = git(
            root,
            ("log", "-1", "--format=%h", "--", *RULE_SET_FILES),
        ).strip()
        if _HASH_RE.fullmatch(revision) is None:
            return "unknown"
        raw_status = git(
            root,
            (
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--",
                *RULE_SET_FILES,
            ),
        )
        status_lines = [line for line in raw_status.splitlines() if line]
        if status_lines and not _only_expected_live_mode_edit(
            root, status_lines, git
        ):
            revision += "-dirty"
        return revision
    except (OSError, UnicodeError, RulesVersionError):
        return "unknown"


def main() -> int:
    document = {
        "schema_version": SCHEMA_VERSION,
        "status": "valid",
        "rules_version": resolve_rules_version(),
    }
    print(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
