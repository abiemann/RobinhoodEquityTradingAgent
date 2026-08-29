#!/usr/bin/env python3
"""Append-only lifecycle telemetry for every scheduled RHMRA invocation.

Account status snapshots intentionally describe the latest verified account
state, so they cannot also be a complete record of scheduler attempts.  This
helper records invocation lifecycle events in an append-only SQLite journal
and publishes a bounded, deterministic JSON projection for the dashboard.

The journal retains every event.  The JSON projection contains the 512 most
recent invocations (oldest to newest within that window) at:

  run-reports/rhmra-run-lifecycle.json

The projection is validated and atomically replaced after each mutation.  A
later ``export`` repairs a stale projection if a process was interrupted after
committing an event but before publishing JSON.  SQLite ``BEGIN IMMEDIATE``
transactions serialize concurrent writers, and database triggers reject
updates or deletes from the event journal.

Commands (all successful commands emit one JSON object):

  py -3 run_lifecycle.py start
  py -3 run_lifecycle.py event --invocation-id UUID --phase preflight \
      --run-start-pt 2026-08-04T12:02:22-07:00
  py -3 run_lifecycle.py event --invocation-id UUID --phase daily-loss
  py -3 run_lifecycle.py finish --invocation-id UUID \
      --classification snapshot-failure --reason-code orders-page-shape \
      --report-file rhmra-log-2026_08_04-12_02.md \
      --status-file rhmra-status-2026_08_04-12_02.json
  py -3 run_lifecycle.py status --invocation-id UUID
  py -3 run_lifecycle.py enter-second --invocation-id UUID --run-token TOKEN \
      --scratch ABSOLUTE_PATH --expected-constants-sha256 SHA256
  py -3 run_lifecycle.py complete-second --invocation-id UUID \
      --run-token TOKEN --outcome snapshot-terminal
  py -3 run_lifecycle.py reconcile-abandoned --invocation-id UUID
  py -3 run_lifecycle.py release-finish --invocation-id UUID \
      --run-token TOKEN --classification completed
  py -3 run_lifecycle.py acquire-bind-context --invocation-id UUID
  py -3 run_lifecycle.py bind-context --invocation-id UUID --run-token TOKEN
  py -3 run_lifecycle.py recover-context
  py -3 run_lifecycle.py export
  py -3 run_lifecycle.py validate

``start`` deliberately precedes configuration and market-clock work, so its
Pacific run timestamp begins as null.  A successful clock preflight binds
``run_start_pt`` exactly once through a ``preflight`` event.  File references
require that binding; an early configuration halt remains visible without it.

Only fixed classifications, phases, and reason codes are accepted.  Lifecycle
journal, projection, and active-context records deliberately contain no
free-text, account, credential, raw lease-token, broker-token, or API-response
field.  The successful ``acquire-bind-context`` stdout receipt is the sole raw
lease-token exception so nested orchestration can store it privately; every
failure receipt is token-free.  ``invocation_id`` is a non-secret correlation UUID.
``--state-file`` and ``--projection-file`` are for tests and diagnostics;
trading runs use the checked-in defaults.  Clock injection exists only on the
imported Python APIs.  Every CLI action rejects ``--now-utc``.

Exit codes:
  0  action succeeded
  1  invalid input, unsafe/corrupt state, or projection publication failure
  2  lifecycle conflict or an active owner blocking acquire-bind-context
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from market_clock import EASTERN_STD_OFFSET, PACIFIC_STD_OFFSET, zone_time
import run_lock as run_lock_module


SCHEMA_VERSION = 1
PROJECTION_LIMIT = 512
PROJECTION_REPLACE_RETRY_DELAY_SECONDS = 1.0
ABANDONED_INVOCATION_MIN_IDLE_SECONDS = (
    run_lock_module.DEFAULT_LEASE_SECONDS + 60
)
ABANDONED_RECONCILIATION_LEASE_SECONDS = 60
_WINDOWS_TRANSIENT_REPLACE_ERRORS = frozenset({5, 32, 33})
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-lifecycle.sqlite3"
)
DEFAULT_PROJECTION_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-lifecycle.json"
)
DEFAULT_REPORT_DIR = os.path.join(ROOT, "run-reports")
DEFAULT_CONTEXT_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-active-context.json"
)
DEFAULT_LOCK_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-lock.sqlite3"
)

CLASSIFICATIONS = (
    "running",
    "completed",
    "risk-halt",
    "snapshot-failure",
    "configuration-halt",
    "runtime-budget",
    "overlap",
    "coordination-halt",
    "lease-lost",
    "final-status-unavailable",
)
TERMINAL_CLASSIFICATIONS = tuple(
    value for value in CLASSIFICATIONS if value != "running"
)
PHASES = (
    "scheduled",
    "coordination",
    "preflight",
    "initial-snapshot",
    "daily-loss",
    "position-management",
    "entry-scan",
    "entry-evaluation",
    "order-placement",
    "final-refresh",
    "report",
    "status-publish",
    "finished",
)
REASON_CODES = (
    "completed",
    "configuration-invalid",
    "clock-unavailable",
    "account-scope-failed",
    "pre-market",
    "after-hours",
    "blackout",
    "loss-limit",
    "daily-loss-tripped",
    "stop-count-limit",
    "stop-count-tripped",
    "buying-power-limit",
    "order-state-guard",
    "positions-page-shape",
    "orders-page-shape",
    "snapshot-write-failed",
    "snapshot-readback-failed",
    "snapshot-retry-exhausted",
    "snapshot-validation-failed",
    "snapshot-second-attempt-failed",
    "scratch-preflight-failed",
    "runtime-deadline",
    "active-run",
    "scheduler-overlap",
    "coordination-state",
    "lease-renewal-failed",
    "lease-ownership-lost",
    "final-refresh-failed",
    "status-write-failed",
    "operator-stop",
    "unknown",
)
TERMINAL_REASON_CODES = {
    "completed": frozenset({None}),
    "risk-halt": frozenset(
        {"daily-loss-tripped", "stop-count-tripped", "order-state-guard"}
    ),
    "snapshot-failure": frozenset(
        {
            "positions-page-shape",
            "orders-page-shape",
            "snapshot-write-failed",
            "snapshot-readback-failed",
            "snapshot-retry-exhausted",
            "snapshot-validation-failed",
            "snapshot-second-attempt-failed",
            "scratch-preflight-failed",
        }
    ),
    "configuration-halt": frozenset({"configuration-invalid"}),
    "runtime-budget": frozenset({"runtime-deadline"}),
    "overlap": frozenset({"active-run", "scheduler-overlap"}),
    "coordination-halt": frozenset(
        {"clock-unavailable", "account-scope-failed", "coordination-state",
         "operator-stop", "unknown"}
    ),
    "lease-lost": frozenset(
        {"lease-renewal-failed", "lease-ownership-lost"}
    ),
    "final-status-unavailable": frozenset(
        {"final-refresh-failed", "status-write-failed"}
    ),
}
CRITICAL_CHECKPOINTS = (
    "entry-eligible",
    "daily-loss-attempted",
    "daily-loss-clear",
    "daily-loss-tripped",
    "daily-loss-snapshot-terminal",
    "daily-loss-coordination-terminal",
)
# ``daily-loss-result`` was the schema-v1 terminal marker before SECOND was
# split into deterministic clear/tripped evidence and typed fail-closed
# terminals.  Existing journals may contain it, so storage keeps accepting it
# for immutable audit preservation.  Runtime authorization deliberately uses
# only ``CRITICAL_CHECKPOINTS`` and never interprets the retired marker.
RETIRED_CHECKPOINTS = ("daily-loss-result",)
CHECKPOINT_STORAGE_VALUES = (*CRITICAL_CHECKPOINTS, *RETIRED_CHECKPOINTS)
LEGACY_CHECKPOINT_STORAGE_VALUES = (
    "entry-eligible",
    "daily-loss-attempted",
    "daily-loss-result",
)
SECOND_OUTCOMES = (
    "clear",
    "tripped",
    "snapshot-terminal",
    "coordination-terminal",
)
PUBLIC_SECOND_OUTCOMES = (
    "snapshot-terminal",
    "coordination-terminal",
)
_SECOND_OUTCOME_CHECKPOINT = {
    outcome: f"daily-loss-{outcome}" for outcome in SECOND_OUTCOMES
}
_SECOND_TERMINAL_CHECKPOINTS = frozenset(
    _SECOND_OUTCOME_CHECKPOINT.values()
)
MAX_CLOCK_BIND_DELAY_SECONDS = 15 * 60
CLOCK_BIND_SKEW_SECONDS = 30

_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_PT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?-0[78]:00$"
)
_REPORT_RE = re.compile(
    r"^rhmra-log-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2})\.md$"
)
_STATUS_RE = re.compile(
    r"^rhmra-status-(\d{4}_\d{2}_\d{2}-\d{2}_\d{2})\.json$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LifecycleError(ValueError):
    """Lifecycle input, storage, or projection is unsafe to interpret."""


class LifecycleConflict(LifecycleError):
    """A requested append conflicts with the invocation's current state."""


class ProjectionPublishError(LifecycleError):
    """An event committed, but its dashboard projection was not published."""

    def __init__(self, message: str, action: str, invocation_id: str):
        super().__init__(message)
        self.action = action
        self.invocation_id = invocation_id


class ReleaseFinishError(LifecycleError):
    """Lease release succeeded, but lifecycle finalization did not complete."""

    def __init__(
        self,
        message: str,
        invocation_id: str,
        *,
        recorded: bool,
        reason: str,
    ):
        super().__init__(message)
        self.invocation_id = invocation_id
        self.recorded = recorded
        self.reason = reason


def _object_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LifecycleError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise LifecycleError(f"non-finite JSON number is forbidden: {value}")


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
    except LifecycleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"cannot read strict projection JSON: {exc}") from exc


def _exact_keys(
    value: Mapping[str, Any], required: Iterable[str], context: str
) -> None:
    required_set = set(required)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - required_set)
    if missing:
        raise LifecycleError(f"{context}: missing key(s): {', '.join(missing)}")
    if extra:
        raise LifecycleError(f"{context}: unknown key(s): {', '.join(extra)}")


def _canonical_uuid(value: str | None, context: str) -> str:
    if not isinstance(value, str):
        raise LifecycleError(f"{context}: expected a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise LifecycleError(f"{context}: expected a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise LifecycleError(
            f"{context}: expected a lowercase hyphenated canonical UUID"
        )
    return canonical


def _canonical_utc(value: str | None, context: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise LifecycleError(f"{context}: expected ISO-8601 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LifecycleError(f"{context}: invalid UTC timestamp") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise LifecycleError(f"{context}: timestamp is not canonical")
    return canonical


def _now_utc(value: str | None) -> str:
    if value is None:
        value = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return _canonical_utc(value, "--now-utc")


def _canonical_run_start_pt(value: str | None, context: str) -> str:
    if not isinstance(value, str) or not _PT_RE.fullmatch(value):
        raise LifecycleError(
            f"{context}: expected Pacific ISO-8601 timestamp with -07:00 or -08:00"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LifecycleError(f"{context}: invalid Pacific timestamp") from exc
    offset = parsed.utcoffset()
    if offset not in (timedelta(hours=-7), timedelta(hours=-8)):
        raise LifecycleError(f"{context}: invalid Pacific UTC offset")
    utc_value = parsed.astimezone(timezone.utc)
    _pacific, _name, expected_offset = zone_time(
        utc_value, PACIFIC_STD_OFFSET, "PST", "PDT"
    )
    if offset != timedelta(hours=expected_offset):
        raise LifecycleError(
            f"{context}: UTC offset is not valid Pacific time for this instant"
        )
    if parsed.isoformat() != value:
        raise LifecycleError(f"{context}: timestamp is not canonical")
    return value


def _validate_terminal_reason(
    classification: str, reason_code: str | None, context: str
) -> None:
    if classification not in TERMINAL_REASON_CODES:
        raise LifecycleError(f"{context}: expected a terminal classification")
    allowed = TERMINAL_REASON_CODES[classification]
    if reason_code not in allowed:
        rendered = ", ".join(
            "none" if value is None else value
            for value in sorted(allowed, key=lambda value: value or "")
        )
        raise LifecycleError(
            f"{context}: {classification} requires reason code {rendered}"
        )


def _enum(value: Any, allowed: Sequence[str], context: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise LifecycleError(
            f"{context}: expected one of {', '.join(allowed)}"
        )
    return value


def _safe_filename(
    value: str | None,
    pattern: re.Pattern[str],
    expected_stamp: str,
    context: str,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or os.path.basename(value) != value:
        raise LifecycleError(f"{context}: expected a bare telemetry filename")
    match = pattern.fullmatch(value)
    if match is None or match.group(1) != expected_stamp:
        raise LifecycleError(
            f"{context}: filename must match the invocation's Pacific start minute"
        )
    return value


def _run_stamp(run_start_pt: str) -> str:
    parsed = datetime.fromisoformat(run_start_pt)
    return parsed.strftime("%Y_%m_%d-%H_%M")


_MOUNTINFO_ESCAPE_RE = re.compile(r'\\([0-7]{3})')
_UNSAFE_SHARED_FILESYSTEMS = frozenset({'9p', 'drvfs'})
_SQLITE_ROLLBACK_MAGIC = b'\xd9\xd5\x05\xf9\x20\xa1\x63\xd7'
_PREFLIGHTED_STATE_DIRECTORIES: set[str] = set()


def _decode_mountinfo_path(value: str) -> str:
    return _MOUNTINFO_ESCAPE_RE.sub(
        lambda match: chr(int(match.group(1), 8)), value
    )


def _nearest_existing_directory(path: str) -> str | None:
    candidate = os.path.abspath(path)
    while not os.path.isdir(candidate):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return None
        candidate = parent
    return candidate


def _filesystem_type_from_mountinfo(
    candidate: str, lines: Iterable[str]
) -> str | None:
    candidate = candidate.rstrip('/') or '/'
    best: tuple[int, str] | None = None
    for raw_line in lines:
        left, separator, right = raw_line.rstrip('\n').partition(' - ')
        if not separator:
            continue
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or not right_fields:
            continue
        mount_point = _decode_mountinfo_path(left_fields[4]).rstrip('/') or '/'
        contains = (
            candidate == mount_point
            or mount_point == '/'
            or candidate.startswith(mount_point.rstrip('/') + '/')
        )
        if contains and (best is None or len(mount_point) > best[0]):
            best = (len(mount_point), right_fields[0].lower())
    return None if best is None else best[1]


def _linux_filesystem_type(path: str) -> str | None:
    '''Return the most-specific Linux mount type containing path.'''
    if not sys.platform.startswith('linux'):
        return None
    anchor = _nearest_existing_directory(path)
    if anchor is None:
        return None
    candidate = os.path.realpath(anchor)
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as handle:
            return _filesystem_type_from_mountinfo(candidate, handle)
    except OSError:
        return None


def _unsupported_state_filesystem(detail: str) -> LifecycleError:
    return LifecycleError(
        'lifecycle state filesystem preflight failed before production journal '
        f'access: {detail}. Use a host-native project and Python runtime with '
        'local SQLite semantics; in Claude, use the Code tab with Environment '
        'Local on native Windows instead of Cowork/local-agent. Do not retry here '
        'or delete, rename, copy over, or edit SQLite sidecars'
    )


def _exercise_sqlite_probe(probe_file: str) -> None:
    first: sqlite3.Connection | None = None
    try:
        first = sqlite3.connect(probe_file, timeout=2, isolation_level=None)
        journal_mode = first.execute('PRAGMA journal_mode = DELETE').fetchone()
        if journal_mode is None or str(journal_mode[0]).lower() != 'delete':
            raise _unsupported_state_filesystem(
                'SQLite DELETE-journal mode was not available'
            )
        first.execute('PRAGMA synchronous = FULL')
        first.execute('CREATE TABLE probe (value TEXT NOT NULL)')
        first.execute('BEGIN IMMEDIATE')
        first.execute('INSERT INTO probe(value) VALUES (\'committed\')')
        first.commit()
        first.close()
        first = None

        first = sqlite3.connect(probe_file, timeout=2, isolation_level=None)
        row = first.execute('SELECT value FROM probe').fetchone()
        if row is None or row[0] != 'committed':
            raise _unsupported_state_filesystem(
                'a committed SQLite value could not be read back'
            )
        first.execute('BEGIN IMMEDIATE')
        child_code = '''
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
connection.execute('PRAGMA busy_timeout = 0')
try:
    connection.execute('BEGIN IMMEDIATE')
except sqlite3.OperationalError as exc:
    code = getattr(exc, 'sqlite_errorcode', None)
    base_code = code & 0xFF if isinstance(code, int) else None
    message = str(exc).lower()
    connection.close()
    if base_code in (
        getattr(sqlite3, 'SQLITE_BUSY', 5),
        getattr(sqlite3, 'SQLITE_LOCKED', 6),
    ) or any(token in message for token in ('busy', 'locked')):
        print('blocked')
        raise SystemExit(0)
    print('unexpected-lock-error')
    raise SystemExit(2)
connection.rollback()
connection.close()
print('acquired')
raise SystemExit(3)
'''
        try:
            competitor = subprocess.run(
                [sys.executable, '-c', child_code, probe_file],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise _unsupported_state_filesystem(
                f'cross-process SQLite lock probe could not run: {exc}'
            ) from exc
        if competitor.returncode != 0 or competitor.stdout.strip() != 'blocked':
            raise _unsupported_state_filesystem(
                'cross-process SQLite writer exclusion was not enforced'
            )
        first.rollback()
        first.close()
        first = None
    finally:
        if first is not None:
            try:
                if first.in_transaction:
                    first.rollback()
            finally:
                first.close()


def _exercise_atomic_replace_probe(probe_directory: str) -> None:
    temporary = os.path.join(probe_directory, 'replace.tmp')
    replaced = os.path.join(probe_directory, 'replace.json')
    original = json.dumps({'ok': False}, separators=(',', ':')) + '\n'
    receipt = json.dumps({'ok': True}, separators=(',', ':')) + '\n'
    with open(replaced, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(original)
        handle.flush()
        os.fsync(handle.fileno())
    with open(temporary, 'w', encoding='utf-8', newline='\n') as handle:
        handle.write(receipt)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, replaced)
    with open(replaced, encoding='utf-8') as handle:
        if handle.read() != receipt:
            raise _unsupported_state_filesystem(
                'an atomically replaced file could not be read back'
            )


def _probe_sqlite_state_directory(directory: str) -> None:
    '''Exercise the SQLite and atomic-replace semantics used by live state.'''
    try:
        probe_directory = tempfile.mkdtemp(
            prefix='.rhmra-sqlite-preflight-', dir=directory
        )
    except OSError as exc:
        raise _unsupported_state_filesystem(
            f'disposable probe directory could not be created: {exc}'
        ) from exc
    try:
        _exercise_sqlite_probe(
            os.path.join(probe_directory, 'probe.sqlite3')
        )
        _exercise_atomic_replace_probe(probe_directory)
    except LifecycleError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise _unsupported_state_filesystem(str(exc)) from exc
    finally:
        try:
            shutil.rmtree(probe_directory)
        except OSError as exc:
            raise _unsupported_state_filesystem(
                f'disposable probe cleanup failed: {exc}'
            ) from exc


def _prepare_state_directory(directory: str) -> None:
    '''Reject known shared mounts and probe unknown POSIX state filesystems.'''
    if os.name == 'nt':
        os.makedirs(directory, exist_ok=True)
        return

    filesystem_type = _linux_filesystem_type(directory)
    if filesystem_type in _UNSAFE_SHARED_FILESYSTEMS or (
        filesystem_type is not None and filesystem_type.startswith('fuse')
    ):
        raise _unsupported_state_filesystem(
            f'filesystem type {filesystem_type!r} is not supported for live SQLite state'
        )

    os.makedirs(directory, exist_ok=True)
    key = os.path.normcase(os.path.realpath(directory))
    if key not in _PREFLIGHTED_STATE_DIRECTORIES:
        _probe_sqlite_state_directory(directory)
        _PREFLIGHTED_STATE_DIRECTORIES.add(key)


_CHECKPOINT_CHECK_RE = re.compile(
    r"CHECK\s*\(\s*checkpoint\s+IN\s*\((?P<values>.*?)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
_CHECKPOINT_LITERAL_RE = re.compile(r"'((?:''|[^'])*)'")
_CHECKPOINT_MIGRATION_TABLE = "lifecycle_checkpoints_schema_v1_migration"


def _normalize_schema_sql(sql: str) -> str:
    value = " ".join(sql.strip().lower().split())
    return (
        value.replace("create table if not exists", "create table")
        .replace("create trigger if not exists", "create trigger")
        .replace("create unique index if not exists", "create unique index")
    )


def _checkpoint_table_contract_sql(values: Sequence[str]) -> str:
    allowed = ", ".join(repr(item) for item in values)
    return f"""
        CREATE TABLE lifecycle_checkpoints (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            invocation_id TEXT NOT NULL,
            checkpoint TEXT NOT NULL
                CHECK (checkpoint IN ({allowed})),
            occurred_at_utc TEXT NOT NULL
        )
    """


def _checkpoint_constraint_values(
    connection: sqlite3.Connection,
) -> tuple[str, ...] | None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'lifecycle_checkpoints'"
    ).fetchone()
    if row is None:
        return None
    sql = row["sql"]
    if not isinstance(sql, str):
        raise LifecycleError(
            "lifecycle checkpoint journal has no auditable schema"
        )
    matches = list(_CHECKPOINT_CHECK_RE.finditer(sql))
    if len(matches) != 1:
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe CHECK constraint"
        )
    body = matches[0].group("values")
    literals = list(_CHECKPOINT_LITERAL_RE.finditer(body))
    residue = _CHECKPOINT_LITERAL_RE.sub("", body)
    if not literals or residue.replace(",", "").strip():
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe CHECK constraint"
        )
    values = tuple(
        match.group(1).replace("''", "'") for match in literals
    )
    if len(values) != len(set(values)):
        raise LifecycleError(
            "lifecycle checkpoint journal has a duplicated CHECK value"
        )
    return values


def _validate_checkpoint_table_contract(
    connection: sqlite3.Connection,
) -> None:
    info = connection.execute(
        "PRAGMA table_xinfo(lifecycle_checkpoints)"
    ).fetchall()
    column_contract = [
        (0, "sequence", "INTEGER", 0, None, 1, 0),
        (1, "invocation_id", "TEXT", 1, None, 0, 0),
        (2, "checkpoint", "TEXT", 1, None, 0, 0),
        (3, "occurred_at_utc", "TEXT", 1, None, 0, 0),
    ]
    if [tuple(row) for row in info] != column_contract:
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe schema"
        )
    if connection.execute(
        "PRAGMA foreign_key_list(lifecycle_checkpoints)"
    ).fetchall():
        raise LifecycleError(
            "lifecycle checkpoint journal has unexpected foreign keys"
        )
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' "
        "AND name = 'lifecycle_checkpoints'"
    ).fetchone()
    expected_table_sql = {
        _normalize_schema_sql(
            _checkpoint_table_contract_sql(values)
        )
        for values in (
            LEGACY_CHECKPOINT_STORAGE_VALUES,
            CRITICAL_CHECKPOINTS,
            CHECKPOINT_STORAGE_VALUES,
        )
    }
    if (
        table_sql_row is None
        or not isinstance(table_sql_row["sql"], str)
        or _normalize_schema_sql(table_sql_row["sql"])
        not in expected_table_sql
    ):
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe table contract"
        )
    indexes = connection.execute(
        "PRAGMA index_list(lifecycle_checkpoints)"
    ).fetchall()
    if (
        len(indexes) != 1
        or indexes[0]["name"] != "lifecycle_one_checkpoint"
        or indexes[0]["unique"] != 1
        or indexes[0]["origin"] != "c"
        or indexes[0]["partial"] != 0
    ):
        raise LifecycleError(
            "lifecycle checkpoint journal has unexpected indexes"
        )
    index_columns = tuple(
        row["name"]
        for row in connection.execute(
            "PRAGMA index_info(lifecycle_one_checkpoint)"
        ).fetchall()
    )
    if index_columns != ("invocation_id", "checkpoint"):
        raise LifecycleError(
            "lifecycle checkpoint journal lacks its uniqueness guard"
        )
    schema_rows = {
        row["name"]: row["sql"]
        for row in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'lifecycle_checkpoints'"
        )
    }
    if set(schema_rows) != {
        "lifecycle_checkpoints_no_update",
        "lifecycle_checkpoints_no_delete",
    }:
        raise LifecycleError(
            "lifecycle checkpoint journal has unsafe append-only guards"
        )
    index_sql_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        "AND name = 'lifecycle_one_checkpoint'"
    ).fetchone()
    expected_index_sql = _normalize_schema_sql(
        "CREATE UNIQUE INDEX lifecycle_one_checkpoint "
        "ON lifecycle_checkpoints(invocation_id, checkpoint)"
    )
    if (
        index_sql_row is None
        or not isinstance(index_sql_row["sql"], str)
        or _normalize_schema_sql(index_sql_row["sql"]) != expected_index_sql
    ):
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe uniqueness guard"
        )

    expected_triggers = {
        "lifecycle_checkpoints_no_update": _normalize_schema_sql(
            """
            CREATE TRIGGER lifecycle_checkpoints_no_update
            BEFORE UPDATE ON lifecycle_checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle checkpoints are append-only');
            END
            """
        ),
        "lifecycle_checkpoints_no_delete": _normalize_schema_sql(
            """
            CREATE TRIGGER lifecycle_checkpoints_no_delete
            BEFORE DELETE ON lifecycle_checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle checkpoints are append-only');
            END
            """
        ),
    }
    if any(
        not isinstance(schema_rows[name], str)
        or _normalize_schema_sql(schema_rows[name]) != expected
        for name, expected in expected_triggers.items()
    ):
        raise LifecycleError(
            "lifecycle checkpoint journal has unsafe append-only guards"
        )
    if connection.execute(
        "SELECT name FROM sqlite_master WHERE name = ?",
        (_CHECKPOINT_MIGRATION_TABLE,),
    ).fetchone() is not None:
        raise LifecycleError(
            "lifecycle checkpoint migration staging table already exists"
        )


def _migrate_lifecycle_checkpoints_if_needed(
    connection: sqlite3.Connection,
    checkpoints_sql: str,
) -> None:
    values = _checkpoint_constraint_values(connection)
    if values is None:
        return
    value_set = frozenset(values)
    target = frozenset(CHECKPOINT_STORAGE_VALUES)
    if value_set == target and len(values) == len(target):
        return
    recognized_sources = {
        frozenset(LEGACY_CHECKPOINT_STORAGE_VALUES),
        frozenset(CRITICAL_CHECKPOINTS),
    }
    if value_set not in recognized_sources or len(values) != len(value_set):
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsupported CHECK domain"
        )
    _validate_checkpoint_table_contract(connection)
    before = [
        tuple(row)
        for row in connection.execute(
            "SELECT sequence, invocation_id, checkpoint, occurred_at_utc "
            "FROM lifecycle_checkpoints ORDER BY sequence"
        ).fetchall()
    ]
    sequence_row = connection.execute(
        "SELECT seq FROM sqlite_sequence "
        "WHERE name = 'lifecycle_checkpoints'"
    ).fetchone()
    prior_sequence = None if sequence_row is None else int(sequence_row["seq"])
    connection.execute("DROP TRIGGER lifecycle_checkpoints_no_update")
    connection.execute("DROP TRIGGER lifecycle_checkpoints_no_delete")
    connection.execute("DROP INDEX lifecycle_one_checkpoint")
    connection.execute(
        "ALTER TABLE lifecycle_checkpoints RENAME TO "
        + _CHECKPOINT_MIGRATION_TABLE
    )
    connection.execute(
        f"""
        CREATE TABLE lifecycle_checkpoints (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            invocation_id TEXT NOT NULL,
            checkpoint TEXT NOT NULL
                CHECK (checkpoint IN ({checkpoints_sql})),
            occurred_at_utc TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO lifecycle_checkpoints "
        "(sequence, invocation_id, checkpoint, occurred_at_utc) "
        "SELECT sequence, invocation_id, checkpoint, occurred_at_utc FROM "
        + _CHECKPOINT_MIGRATION_TABLE
        + " ORDER BY sequence"
    )
    after = [
        tuple(row)
        for row in connection.execute(
            "SELECT sequence, invocation_id, checkpoint, occurred_at_utc "
            "FROM lifecycle_checkpoints ORDER BY sequence"
        ).fetchall()
    ]
    if after != before:
        raise LifecycleError(
            "lifecycle checkpoint migration did not preserve every row"
        )
    connection.execute("DROP TABLE " + _CHECKPOINT_MIGRATION_TABLE)
    if prior_sequence is not None:
        current_sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name = 'lifecycle_checkpoints'"
        ).fetchone()
        if current_sequence is None:
            connection.execute(
                "INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                ("lifecycle_checkpoints", prior_sequence),
            )
        elif int(current_sequence["seq"]) < prior_sequence:
            connection.execute(
                "UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                (prior_sequence, "lifecycle_checkpoints"),
            )


def _connect(state_file: str) -> sqlite3.Connection:
    path = os.path.abspath(state_file)
    directory = os.path.dirname(path)
    if not directory:
        raise LifecycleError("state file must have a parent directory")
    _prepare_state_directory(directory)
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
    except Exception:
        connection.close()
        raise
    classifications_sql = ", ".join(repr(item) for item in CLASSIFICATIONS)
    phases_sql = ", ".join(repr(item) for item in PHASES)
    reasons_sql = ", ".join(repr(item) for item in REASON_CODES)
    checkpoints_sql = ", ".join(
        repr(item) for item in CHECKPOINT_STORAGE_VALUES
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS lifecycle_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS lifecycle_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL,
                event_type TEXT NOT NULL
                    CHECK (event_type IN ('start', 'event', 'finish')),
                classification TEXT NOT NULL
                    CHECK (classification IN ({classifications_sql})),
                occurred_at_utc TEXT NOT NULL,
                run_start_pt TEXT,
                phase TEXT NOT NULL CHECK (phase IN ({phases_sql})),
                reason_code TEXT CHECK (reason_code IN ({reasons_sql})),
                report_file TEXT,
                status_file TEXT,
                CHECK (
                    (event_type = 'start'
                     AND classification = 'running'
                     AND run_start_pt IS NULL
                     AND phase = 'scheduled'
                     AND reason_code IS NULL
                     AND report_file IS NULL
                     AND status_file IS NULL)
                    OR
                    (event_type = 'event'
                     AND (run_start_pt IS NULL OR phase = 'preflight')
                     AND classification != 'completed'
                     AND report_file IS NULL
                     AND status_file IS NULL)
                    OR
                    (event_type = 'finish'
                     AND run_start_pt IS NULL
                     AND classification != 'running'
                     AND (classification = 'completed' OR reason_code IS NOT NULL))
                )
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_one_start "
            "ON lifecycle_events(invocation_id) WHERE event_type = 'start'"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_one_finish "
            "ON lifecycle_events(invocation_id) WHERE event_type = 'finish'"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_lease_bindings (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                lease_token_sha256 TEXT NOT NULL UNIQUE,
                run_start_pt TEXT NOT NULL,
                artifact_stamp TEXT NOT NULL,
                expected_report_file TEXT NOT NULL,
                expected_gate_file TEXT NOT NULL,
                expected_status_file TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_lease_compensations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                lease_token_sha256 TEXT NOT NULL UNIQUE,
                disposition TEXT NOT NULL
                    CHECK (disposition = 'bind-failure-released'),
                occurred_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS lifecycle_checkpoints (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL,
                checkpoint TEXT NOT NULL
                    CHECK (checkpoint IN ({checkpoints_sql})),
                occurred_at_utc TEXT NOT NULL
            )
            """
        )
        _migrate_lifecycle_checkpoints_if_needed(
            connection, checkpoints_sql
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS lifecycle_one_checkpoint "
            "ON lifecycle_checkpoints(invocation_id, checkpoint)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_second_contexts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                scratch_id TEXT NOT NULL UNIQUE,
                source_root_id TEXT NOT NULL UNIQUE,
                scratch_path_sha256 TEXT NOT NULL UNIQUE,
                source_root_path_sha256 TEXT NOT NULL UNIQUE,
                scratch_marker_sha256 TEXT NOT NULL,
                transport_marker_sha256 TEXT NOT NULL,
                source_root_marker_sha256 TEXT NOT NULL,
                scratch_device TEXT NOT NULL,
                scratch_inode TEXT NOT NULL,
                source_root_device TEXT NOT NULL,
                source_root_inode TEXT NOT NULL,
                constants_sha256 TEXT NOT NULL,
                daily_loss_halt_pct TEXT NOT NULL,
                stop_count_halt INTEGER NOT NULL,
                occurred_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS lifecycle_second_evidence (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL CHECK (outcome IN ('clear', 'tripped')),
                generation TEXT NOT NULL CHECK (generation IN ('A', 'B')),
                result_file TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                sources_sha256 TEXT NOT NULL,
                constants_sha256 TEXT NOT NULL,
                occurred_at_utc TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_update
            BEFORE UPDATE ON lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle events are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_events_no_delete
            BEFORE DELETE ON lifecycle_events
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle events are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_checkpoints_no_update
            BEFORE UPDATE ON lifecycle_checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle checkpoints are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS lifecycle_checkpoints_no_delete
            BEFORE DELETE ON lifecycle_checkpoints
            BEGIN
                SELECT RAISE(ABORT, 'lifecycle checkpoints are append-only');
            END
            """
        )
        for table in (
            "lifecycle_lease_bindings",
            "lifecycle_lease_compensations",
            "lifecycle_second_contexts",
            "lifecycle_second_evidence",
        ):
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_update
                BEFORE UPDATE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )
            connection.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {table}_no_delete
                BEFORE DELETE ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} is append-only');
                END
                """
            )
        _validate_checkpoint_table_contract(connection)
        row = connection.execute(
            "SELECT value FROM lifecycle_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO lifecycle_metadata(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        elif row["value"] != str(SCHEMA_VERSION):
            raise LifecycleError("unsupported lifecycle journal schema version")
        connection.commit()
    except Exception:
        try:
            if connection.in_transaction:
                connection.rollback()
        finally:
            connection.close()
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    try:
        _validate_schema(connection)
    except Exception:
        connection.close()
        raise
    return connection


def _validate_schema(connection: sqlite3.Connection) -> None:
    expected_columns = {
        "sequence",
        "invocation_id",
        "event_type",
        "classification",
        "occurred_at_utc",
        "run_start_pt",
        "phase",
        "reason_code",
        "report_file",
        "status_file",
    }
    info = connection.execute("PRAGMA table_info(lifecycle_events)").fetchall()
    if {row["name"] for row in info} != expected_columns:
        raise LifecycleError("lifecycle event journal has an unsafe schema")
    primary = {row["name"] for row in info if row["pk"]}
    if primary != {"sequence"}:
        raise LifecycleError("lifecycle event journal has an unsafe primary key")
    triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'lifecycle_events'"
        )
    }
    if not {
        "lifecycle_events_no_update",
        "lifecycle_events_no_delete",
    }.issubset(triggers):
        raise LifecycleError("lifecycle event journal lacks append-only guards")
    checkpoint_table = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'lifecycle_checkpoints'"
    ).fetchone()
    # A read-only validation may be the first operation after upgrading an
    # existing schema-v1 journal.  Absence is tolerated there; the next normal
    # mutable connection creates this additive append-only table.
    if checkpoint_table is None:
        return
    checkpoint_info = connection.execute(
        "PRAGMA table_info(lifecycle_checkpoints)"
    ).fetchall()
    if {row["name"] for row in checkpoint_info} != {
        "sequence", "invocation_id", "checkpoint", "occurred_at_utc"
    }:
        raise LifecycleError("lifecycle checkpoint journal has an unsafe schema")
    checkpoint_primary = {
        row["name"] for row in checkpoint_info if row["pk"]
    }
    if checkpoint_primary != {"sequence"}:
        raise LifecycleError(
            "lifecycle checkpoint journal has an unsafe primary key"
        )
    _validate_checkpoint_table_contract(connection)
    additive_tables = {
        "lifecycle_lease_bindings": {
            "sequence", "invocation_id", "lease_token_sha256",
            "run_start_pt", "artifact_stamp", "expected_report_file",
            "expected_gate_file", "expected_status_file", "occurred_at_utc",
        },
        "lifecycle_lease_compensations": {
            "sequence", "invocation_id", "lease_token_sha256",
            "disposition", "occurred_at_utc",
        },
        "lifecycle_second_contexts": {
            "sequence", "invocation_id", "scratch_id", "source_root_id",
            "scratch_path_sha256", "source_root_path_sha256",
            "scratch_marker_sha256", "transport_marker_sha256",
            "source_root_marker_sha256", "scratch_device", "scratch_inode",
            "source_root_device", "source_root_inode", "occurred_at_utc",
            "constants_sha256", "daily_loss_halt_pct", "stop_count_halt",
        },
        "lifecycle_second_evidence": {
            "sequence", "invocation_id", "outcome", "generation",
            "result_file", "result_sha256", "sources_sha256",
            "constants_sha256", "occurred_at_utc",
        },
    }
    for table, expected in additive_tables.items():
        present = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if present is None:
            # Like lifecycle_checkpoints, additive schema-v1 tables are
            # installed by the next normal mutable connection.
            continue
        table_info = connection.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
        if {row["name"] for row in table_info} != expected:
            raise LifecycleError(f"{table} has an unsafe schema")
        if {row["name"] for row in table_info if row["pk"]} != {"sequence"}:
            raise LifecycleError(f"{table} has an unsafe primary key")
        indexes = connection.execute(f"PRAGMA index_list({table})").fetchall()
        unique_columns = {
            tuple(
                item["name"]
                for item in connection.execute(
                    f"PRAGMA index_info({row['name']})"
                ).fetchall()
            )
            for row in indexes
            if row["unique"] == 1
        }
        if table in {
            "lifecycle_lease_bindings", "lifecycle_lease_compensations"
        }:
            required_unique = {
                ("invocation_id",), ("lease_token_sha256",),
            }
        elif table == "lifecycle_second_contexts":
            required_unique = {
                ("invocation_id",), ("scratch_id",), ("source_root_id",),
                ("scratch_path_sha256",), ("source_root_path_sha256",),
            }
        else:
            required_unique = {("invocation_id",)}
        if not required_unique.issubset(unique_columns):
            raise LifecycleError(f"{table} lacks its uniqueness guard")
        triggers = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = ?",
                (table,),
            )
        }
        if not {f"{table}_no_update", f"{table}_no_delete"}.issubset(
            triggers
        ):
            raise LifecycleError(f"{table} lacks append-only guards")


def _rows_for_projection(
    connection: sqlite3.Connection, limit: int = PROJECTION_LIMIT
) -> list[sqlite3.Row]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LifecycleError("projection limit must be a positive integer")
    return connection.execute(
        """
        WITH recent AS (
            SELECT invocation_id, sequence AS start_sequence
            FROM lifecycle_events
            WHERE event_type = 'start'
            ORDER BY sequence DESC
            LIMIT ?
        )
        SELECT e.*, recent.start_sequence
        FROM recent
        JOIN lifecycle_events AS e
          ON e.invocation_id = recent.invocation_id
        ORDER BY recent.start_sequence ASC, e.sequence ASC
        """,
        (limit,),
    ).fetchall()


def _build_projection(
    connection: sqlite3.Connection, limit: int = PROJECTION_LIMIT
) -> dict[str, Any]:
    rows = _rows_for_projection(connection, limit)
    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        invocation_id = row["invocation_id"]
        event = {
            "sequence": row["sequence"],
            "type": row["event_type"],
            "classification": row["classification"],
            "occurred_at_utc": row["occurred_at_utc"],
            "phase": row["phase"],
            "reason_code": row["reason_code"],
        }
        if invocation_id not in by_id:
            if row["event_type"] != "start":
                raise LifecycleError("invocation history does not begin with start")
            record = {
                "invocation_id": invocation_id,
                "run_start_pt": row["run_start_pt"],
                "started_at_utc": row["occurred_at_utc"],
                "finished_at_utc": None,
                "duration_seconds": None,
                "classification": "running",
                "latest_phase": row["phase"],
                "reason_code": None,
                "report_file": None,
                "status_file": None,
                "events": [],
            }
            by_id[invocation_id] = record
            records.append(record)
        record = by_id[invocation_id]
        if row["run_start_pt"] is not None:
            if record["run_start_pt"] is not None:
                raise LifecycleError("invocation has duplicate Pacific time bindings")
            record["run_start_pt"] = row["run_start_pt"]
        record["events"].append(event)
        record["latest_phase"] = row["phase"]
        if row["event_type"] == "finish":
            record["finished_at_utc"] = row["occurred_at_utc"]
            started = datetime.fromisoformat(
                record["started_at_utc"].replace("Z", "+00:00")
            )
            finished = datetime.fromisoformat(
                row["occurred_at_utc"].replace("Z", "+00:00")
            )
            record["duration_seconds"] = int((finished - started).total_seconds())
            record["classification"] = row["classification"]
            record["reason_code"] = row["reason_code"]
            record["report_file"] = row["report_file"]
            record["status_file"] = row["status_file"]
    high_watermark_row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM lifecycle_events"
    ).fetchone()
    document = {
        "schema_version": SCHEMA_VERSION,
        "record_limit": limit,
        "record_count": len(records),
        "source_event_high_watermark": high_watermark_row["sequence"],
        "records": records,
    }
    validate_projection(document)
    return document


def validate_projection(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise LifecycleError("projection: expected an object")
    _exact_keys(
        document,
        {
            "schema_version",
            "record_limit",
            "record_count",
            "source_event_high_watermark",
            "records",
        },
        "projection",
    )
    if document["schema_version"] != SCHEMA_VERSION:
        raise LifecycleError("projection.schema_version: unsupported value")
    limit = document["record_limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise LifecycleError("projection.record_limit: expected a positive integer")
    records = document["records"]
    if not isinstance(records, list) or len(records) > limit:
        raise LifecycleError("projection.records: expected a bounded array")
    if document["record_count"] != len(records):
        raise LifecycleError("projection.record_count: does not match records")
    watermark = document["source_event_high_watermark"]
    if isinstance(watermark, bool) or not isinstance(watermark, int) or watermark < 0:
        raise LifecycleError(
            "projection.source_event_high_watermark: expected a nonnegative integer"
        )

    seen_ids: set[str] = set()
    previous_start_sequence = 0
    for index, record in enumerate(records):
        context = f"projection.records[{index}]"
        if not isinstance(record, dict):
            raise LifecycleError(f"{context}: expected an object")
        _exact_keys(
            record,
            {
                "invocation_id",
                "run_start_pt",
                "started_at_utc",
                "finished_at_utc",
                "duration_seconds",
                "classification",
                "latest_phase",
                "reason_code",
                "report_file",
                "status_file",
                "events",
            },
            context,
        )
        invocation_id = _canonical_uuid(
            record["invocation_id"], f"{context}.invocation_id"
        )
        if invocation_id in seen_ids:
            raise LifecycleError(f"{context}.invocation_id: duplicate invocation")
        seen_ids.add(invocation_id)
        run_start_pt_raw = record["run_start_pt"]
        run_start_pt = (
            None
            if run_start_pt_raw is None
            else _canonical_run_start_pt(
                run_start_pt_raw, f"{context}.run_start_pt"
            )
        )
        started_at = _canonical_utc(
            record["started_at_utc"], f"{context}.started_at_utc"
        )
        if run_start_pt is not None:
            start_utc = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            clock_utc = datetime.fromisoformat(run_start_pt).astimezone(timezone.utc)
            clock_delay = (clock_utc - start_utc).total_seconds()
            if not (
                -CLOCK_BIND_SKEW_SECONDS
                <= clock_delay
                <= MAX_CLOCK_BIND_DELAY_SECONDS
            ):
                raise LifecycleError(
                    f"{context}.run_start_pt: not contemporaneous with invocation start"
                )
        classification = _enum(
            record["classification"], CLASSIFICATIONS, f"{context}.classification"
        )
        latest_phase = _enum(
            record["latest_phase"], PHASES, f"{context}.latest_phase"
        )
        reason = record["reason_code"]
        if reason is not None:
            _enum(reason, REASON_CODES, f"{context}.reason_code")
        if run_start_pt is None:
            if record["report_file"] is not None or record["status_file"] is not None:
                raise LifecycleError(
                    f"{context}: file references require a Pacific time binding"
                )
            report_file = None
            status_file = None
        else:
            stamp = _run_stamp(run_start_pt)
            report_file = _safe_filename(
                record["report_file"], _REPORT_RE, stamp, f"{context}.report_file"
            )
            status_file = _safe_filename(
                record["status_file"], _STATUS_RE, stamp, f"{context}.status_file"
            )
        events = record["events"]
        if not isinstance(events, list) or not events:
            raise LifecycleError(f"{context}.events: expected a non-empty array")
        finish_count = 0
        previous_sequence = 0
        previous_time: datetime | None = None
        for event_index, event in enumerate(events):
            event_context = f"{context}.events[{event_index}]"
            if not isinstance(event, dict):
                raise LifecycleError(f"{event_context}: expected an object")
            _exact_keys(
                event,
                {
                    "sequence",
                    "type",
                    "classification",
                    "occurred_at_utc",
                    "phase",
                    "reason_code",
                },
                event_context,
            )
            sequence = event["sequence"]
            if (
                isinstance(sequence, bool)
                or not isinstance(sequence, int)
                or sequence <= previous_sequence
                or sequence > watermark
            ):
                raise LifecycleError(f"{event_context}.sequence: invalid order")
            previous_sequence = sequence
            event_type = event["type"]
            if event_type not in ("start", "event", "finish"):
                raise LifecycleError(f"{event_context}.type: invalid value")
            event_classification = _enum(
                event["classification"],
                CLASSIFICATIONS,
                f"{event_context}.classification",
            )
            event_time_text = _canonical_utc(
                event["occurred_at_utc"], f"{event_context}.occurred_at_utc"
            )
            event_time = datetime.fromisoformat(
                event_time_text.replace("Z", "+00:00")
            )
            if previous_time is not None and event_time < previous_time:
                raise LifecycleError(f"{event_context}: time moved backwards")
            previous_time = event_time
            event_phase = _enum(
                event["phase"], PHASES, f"{event_context}.phase"
            )
            event_reason = event["reason_code"]
            if event_reason is not None:
                _enum(event_reason, REASON_CODES, f"{event_context}.reason_code")
            if event_type == "finish":
                _validate_terminal_reason(
                    event_classification, event_reason, event_context
                )
            elif event_classification != "running":
                _validate_terminal_reason(
                    event_classification, event_reason, event_context
                )
            if event_index == 0:
                if not (
                    event_type == "start"
                    and event_classification == "running"
                    and event_phase == "scheduled"
                    and event_reason is None
                    and event_time_text == started_at
                ):
                    raise LifecycleError(f"{event_context}: invalid start event")
                if sequence <= previous_start_sequence:
                    raise LifecycleError(f"{event_context}: records are out of order")
                previous_start_sequence = sequence
            elif event_type == "start":
                raise LifecycleError(f"{event_context}: duplicate start event")
            if event_type == "finish":
                finish_count += 1
                if event_index != len(events) - 1:
                    raise LifecycleError(f"{event_context}: finish must be last")

        if latest_phase != events[-1]["phase"]:
            raise LifecycleError(f"{context}.latest_phase: does not match last event")
        finished_at = record["finished_at_utc"]
        duration = record["duration_seconds"]
        if finish_count == 0:
            if any(
                value is not None
                for value in (
                    finished_at,
                    duration,
                    reason,
                    report_file,
                    status_file,
                )
            ) or classification != "running":
                raise LifecycleError(f"{context}: running summary is inconsistent")
        elif finish_count == 1:
            if classification == "running":
                raise LifecycleError(f"{context}: finish cannot remain running")
            canonical_finished = _canonical_utc(
                finished_at, f"{context}.finished_at_utc"
            )
            if canonical_finished != events[-1]["occurred_at_utc"]:
                raise LifecycleError(
                    f"{context}.finished_at_utc: does not match finish event"
                )
            expected_duration = int(
                (
                    datetime.fromisoformat(canonical_finished.replace("Z", "+00:00"))
                    - datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                ).total_seconds()
            )
            if (
                isinstance(duration, bool)
                or not isinstance(duration, int)
                or duration != expected_duration
                or duration < 0
            ):
                raise LifecycleError(f"{context}.duration_seconds: invalid value")
            final_event = events[-1]
            if classification != final_event["classification"]:
                raise LifecycleError(
                    f"{context}.classification: does not match finish event"
                )
            if reason != final_event["reason_code"]:
                raise LifecycleError(
                    f"{context}.reason_code: does not match finish event"
                )
            _validate_terminal_reason(classification, reason, context)
            if classification in (
                "overlap",
                "lease-lost",
                "final-status-unavailable",
            ) and status_file is not None:
                raise LifecycleError(
                    f"{context}.status_file: forbidden for this classification"
                )
        else:
            raise LifecycleError(f"{context}: duplicate finish event")
    return document


def _atomic_write_projection(path: str, document: Mapping[str, Any]) -> None:
    validate_projection(document)
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    if not directory:
        raise LifecycleError("projection file must have a parent directory")
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".rhmra-run-lifecycle-", suffix=".tmp", dir=directory
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        readback = _load_json(temporary)
        validate_projection(readback)
        if readback != document:
            raise LifecycleError("projection readback did not match serialized data")
        try:
            os.replace(temporary, absolute)
        except OSError as exc:
            # A Windows reader can briefly deny replacement of an otherwise
            # valid projection. Retry the already-fsynced temporary file once;
            # never replay the journal mutation that triggered publication.
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None)
                not in _WINDOWS_TRANSIENT_REPLACE_ERRORS
            ):
                raise
            time.sleep(PROJECTION_REPLACE_RETRY_DELAY_SECONDS)
            os.replace(temporary, absolute)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def publish_projection(
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
) -> dict[str, Any]:
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        document = _build_projection(connection)
        _atomic_write_projection(projection_file, document)
        connection.commit()
        return document
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _has_hot_rollback_journal(state_file: str) -> bool:
    journal = os.path.abspath(state_file) + '-journal'
    try:
        if os.path.getsize(journal) <= 512:
            return False
        with open(journal, 'rb') as handle:
            return handle.read(len(_SQLITE_ROLLBACK_MAGIC)) == _SQLITE_ROLLBACK_MAGIC
    except OSError:
        return False


def _is_readonly_error(exc: BaseException) -> bool:
    code = getattr(exc, 'sqlite_errorcode', None)
    if isinstance(code, int) and (
        code & 0xFF
    ) == getattr(sqlite3, 'SQLITE_READONLY', 8):
        return True
    message = str(exc).lower()
    return 'readonly' in message or 'read-only' in message


def _interrupted_transaction_recovery_message() -> str:
    return (
        'cannot open lifecycle journal read-only: an interrupted SQLite '
        'transaction requires host-native recovery. Pause scheduled runners, '
        'then run the checked-in helper from the native host (py -3 '
        'run_lifecycle.py export on Windows, or the bound absolute python3 '
        'path on native Linux/macOS) and validate again. Never delete, '
        'rename, copy over, or edit the .sqlite3-journal sidecar'
    )


def validate_current_projection_read_only(
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
) -> dict[str, Any]:
    absolute = os.path.abspath(state_file)
    if not os.path.isfile(absolute):
        raise LifecycleError("lifecycle event journal is missing")
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            Path(absolute).as_uri() + "?mode=ro",
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        _validate_schema(connection)
    except LifecycleError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        if _is_readonly_error(exc) and _has_hot_rollback_journal(absolute):
            raise LifecycleError(
                _interrupted_transaction_recovery_message()
            ) from exc
        raise LifecycleError(f"cannot open lifecycle journal read-only: {exc}") from exc
    assert connection is not None
    try:
        connection.execute("BEGIN")
        expected = _build_projection(connection)
        actual = _load_json(projection_file)
        validate_projection(actual)
        if actual != expected:
            raise LifecycleError("projection is valid JSON but stale or inconsistent")
        connection.rollback()
        return actual
    except LifecycleError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection.in_transaction:
            connection.rollback()
        raise LifecycleError(f"cannot validate lifecycle projection: {exc}") from exc
    finally:
        connection.close()


def validate_current_projection(
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
) -> dict[str, Any]:
    return validate_current_projection_read_only(state_file, projection_file)


def invocation_status(
    *, invocation_id: str, state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
) -> dict[str, Any]:
    '''Return the authoritative artifact binding for one run.'''
    invocation_id = _canonical_uuid(invocation_id, 'invocation_id')
    document = validate_current_projection_read_only(state_file, projection_file)
    record = next(
        (item for item in document['records']
         if item['invocation_id'] == invocation_id),
        None,
    )
    if record is None:
        raise LifecycleConflict('invocation has not been started')
    run_start_pt = record['run_start_pt']
    if run_start_pt is None:
        raise LifecycleConflict('invocation has no Pacific time binding')
    if record['classification'] != 'running':
        raise LifecycleConflict('invocation is already finished')
    artifact_stamp = _run_stamp(run_start_pt)
    return {
        'schema_version': SCHEMA_VERSION, 'action': 'status', 'ok': True,
        'invocation_id': invocation_id,
        'classification': record['classification'],
        'phase': record['latest_phase'], 'run_start_pt': run_start_pt,
        'artifact_stamp': artifact_stamp,
        'expected_report_file': f'rhmra-log-{artifact_stamp}.md',
        'expected_gate_file': f'rhmra-gates-{artifact_stamp}.json',
        'expected_status_file': f'rhmra-status-{artifact_stamp}.json',
    }


def _active_lease_token(
    lock_file: str = DEFAULT_LOCK_FILE, now_utc: str | None = None,
) -> str:
    """Read and validate the current unexpired lease without mutating it."""
    absolute = os.path.abspath(lock_file)
    if not os.path.isfile(absolute):
        raise LifecycleConflict('active run lease is missing')
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            Path(absolute).as_uri() + '?mode=ro',
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only = ON')
        columns = [
            row['name']
            for row in connection.execute('PRAGMA table_info(run_lease)')
        ]
        if columns != [
            'singleton', 'token', 'acquired_at', 'renewed_at', 'expires_at'
        ]:
            raise LifecycleError('active run lease has an unsupported schema')
        rows = connection.execute(
            'SELECT singleton, token, acquired_at, renewed_at, expires_at '
            'FROM run_lease'
        ).fetchall()
    except LifecycleError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise LifecycleError(f'cannot read active run lease: {exc}') from exc
    finally:
        if connection is not None:
            connection.close()
    if len(rows) != 1 or rows[0]['singleton'] != 1:
        raise LifecycleConflict('active run lease is missing')
    row = rows[0]
    token = row['token']
    if not isinstance(token, str) or not token.strip():
        raise LifecycleError('active run lease token is malformed')
    timestamps = tuple(
        row[name] for name in ('acquired_at', 'renewed_at', 'expires_at')
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in timestamps
    ):
        raise LifecycleError('active run lease timestamps are malformed')
    acquired_at, renewed_at, expires_at = map(float, timestamps)
    if not acquired_at <= renewed_at <= expires_at:
        raise LifecycleError('active run lease timestamps are inconsistent')
    if now_utc is None:
        now_value = datetime.now(timezone.utc).timestamp()
    else:
        now_text = _now_utc(now_utc)
        now_value = datetime.fromisoformat(
            now_text.replace('Z', '+00:00')
        ).timestamp()
    if now_value < acquired_at or now_value >= expires_at:
        raise LifecycleConflict('active run lease is expired')
    return token


def _resolved_lock_file(state_file: str, lock_file: str | None) -> str:
    """Resolve an omitted lease DB without escaping custom test state."""
    state_path = os.path.abspath(state_file)
    if lock_file is None:
        if os.path.normcase(state_path) == os.path.normcase(
            os.path.abspath(DEFAULT_STATE_FILE)
        ):
            resolved = os.path.abspath(DEFAULT_LOCK_FILE)
        else:
            resolved = os.path.join(
                os.path.dirname(state_path), "rhmra-run-lock.sqlite3"
            )
    else:
        if not isinstance(lock_file, str) or not lock_file:
            raise LifecycleError("lock file must be a nonempty path")
        resolved = os.path.abspath(lock_file)
    if os.path.normcase(resolved) == os.path.normcase(state_path):
        raise LifecycleError("lifecycle state and run-lock files must differ")
    return resolved


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _validate_context_receipt(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise LifecycleError('active context receipt must be a JSON object')
    _exact_keys(
        document,
        {
            'schema_version', 'python', 'version', 'invocation_id',
            'run_start_pt', 'artifact_stamp', 'expected_report_file',
            'expected_gate_file', 'expected_status_file',
            'lease_token_sha256',
        },
        'active context receipt',
    )
    if document['schema_version'] != SCHEMA_VERSION or isinstance(
        document['schema_version'], bool
    ):
        raise LifecycleError('active context receipt has unsupported schema')
    python_executable = document['python']
    if (
        not isinstance(python_executable, str)
        or not python_executable
        or not os.path.isabs(python_executable)
        or re.search(
            r'(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]', python_executable
        )
    ):
        raise LifecycleError('active context receipt has an unsafe Python path')
    version = document['version']
    if (
        not isinstance(version, str)
        or re.fullmatch(r'3(?:\.\d+){1,3}(?:[^\s]*)?', version) is None
    ):
        raise LifecycleError('active context receipt requires Python 3')
    invocation_id = _canonical_uuid(
        document['invocation_id'], 'active context invocation_id'
    )
    run_start_pt = _canonical_run_start_pt(
        document['run_start_pt'], 'active context run_start_pt'
    )
    artifact_stamp = _run_stamp(run_start_pt)
    if document['artifact_stamp'] != artifact_stamp:
        raise LifecycleError('active context artifact stamp is inconsistent')
    expected = {
        'expected_report_file': f'rhmra-log-{artifact_stamp}.md',
        'expected_gate_file': f'rhmra-gates-{artifact_stamp}.json',
        'expected_status_file': f'rhmra-status-{artifact_stamp}.json',
    }
    for name, value in expected.items():
        if document[name] != value:
            raise LifecycleError(f'active context {name} is inconsistent')
    digest = document['lease_token_sha256']
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise LifecycleError('active context lease digest is malformed')
    return document


def _load_context_receipt(path: str) -> dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            document = json.load(
                handle,
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
    except LifecycleError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f'cannot read active context receipt: {exc}') from exc
    return _validate_context_receipt(document)


def _atomic_write_context(path: str, document: Mapping[str, Any]) -> None:
    _validate_context_receipt(dict(document))
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    if not directory:
        raise LifecycleError('active context path must have a parent directory')
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix='.rhmra-active-context-', suffix='.tmp', dir=directory
    )
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(
                document, handle, ensure_ascii=False, allow_nan=False,
                sort_keys=True, indent=2,
            )
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
        if _load_context_receipt(temporary) != document:
            raise LifecycleError('active context readback did not match')
        os.replace(temporary, absolute)
        temporary = ''
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _context_result(action: str, document: Mapping[str, Any],
                    lifecycle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        'schema_version': SCHEMA_VERSION,
        'action': action,
        'ok': True,
        'python': document['python'],
        'version': document['version'],
        'invocation_id': document['invocation_id'],
        'classification': lifecycle['classification'],
        'phase': lifecycle['phase'],
        'run_start_pt': document['run_start_pt'],
        'artifact_stamp': document['artifact_stamp'],
        'expected_report_file': document['expected_report_file'],
        'expected_gate_file': document['expected_gate_file'],
        'expected_status_file': document['expected_status_file'],
    }


def _lease_binding_row(
    connection: sqlite3.Connection, invocation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM lifecycle_lease_bindings WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()


def _lease_compensation_row(
    connection: sqlite3.Connection, invocation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM lifecycle_lease_compensations WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()


def _record_lease_binding(
    *,
    document: Mapping[str, Any],
    state_file: str,
    now_utc: str | None,
) -> None:
    """Append the token-private owner binding before returning bind success."""
    occurred_at = _now_utc(now_utc)
    expected = {
        "invocation_id": document["invocation_id"],
        "lease_token_sha256": document["lease_token_sha256"],
        "run_start_pt": document["run_start_pt"],
        "artifact_stamp": document["artifact_stamp"],
        "expected_report_file": document["expected_report_file"],
        "expected_gate_file": document["expected_gate_file"],
        "expected_status_file": document["expected_status_file"],
    }
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = _invocation_rows(connection, expected["invocation_id"])
        _require_open_invocation(rows)
        if rows[-1]["phase"] != "preflight":
            raise LifecycleConflict(
                "lease binding requires the current preflight phase"
            )
        existing = _lease_binding_row(
            connection, expected["invocation_id"]
        )
        if existing is None:
            latest = datetime.fromisoformat(
                rows[-1]["occurred_at_utc"].replace("Z", "+00:00")
            )
            current = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00")
            )
            if current < latest:
                raise LifecycleError(
                    "lease binding time cannot precede lifecycle activity"
                )
            connection.execute(
                """
                INSERT INTO lifecycle_lease_bindings (
                    invocation_id, lease_token_sha256, run_start_pt,
                    artifact_stamp, expected_report_file,
                    expected_gate_file, expected_status_file, occurred_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *(expected[name] for name in (
                        "invocation_id", "lease_token_sha256", "run_start_pt",
                        "artifact_stamp", "expected_report_file",
                        "expected_gate_file", "expected_status_file",
                    )),
                    occurred_at,
                ),
            )
        elif any(existing[name] != value for name, value in expected.items()):
            raise LifecycleConflict(
                "lease binding retry differs from its durable owner context"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _record_lease_compensation(
    *,
    invocation_id: str,
    run_token: str,
    state_file: str,
    now_utc: str | None,
) -> None:
    """Persist proof that combined bind failure released its exact lease."""
    occurred_at = _now_utc(now_utc)
    digest = _token_digest(run_token)
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = _invocation_rows(connection, invocation_id)
        _require_open_invocation(rows)
        binding = _lease_binding_row(connection, invocation_id)
        if binding is None or binding["lease_token_sha256"] != digest:
            raise LifecycleConflict(
                "bind compensation does not match the durable lease binding"
            )
        existing = _lease_compensation_row(connection, invocation_id)
        if existing is None:
            current = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00")
            )
            bound_at = datetime.fromisoformat(
                binding["occurred_at_utc"].replace("Z", "+00:00")
            )
            if current < bound_at:
                raise LifecycleError(
                    "bind compensation time cannot precede its lease binding"
                )
            connection.execute(
                "INSERT INTO lifecycle_lease_compensations "
                "(invocation_id, lease_token_sha256, disposition, "
                "occurred_at_utc) VALUES (?, ?, 'bind-failure-released', ?)",
                (invocation_id, digest, occurred_at),
            )
        elif (
            existing["lease_token_sha256"] != digest
            or existing["disposition"] != "bind-failure-released"
        ):
            raise LifecycleConflict(
                "bind compensation retry differs from durable state"
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def bind_active_context(
    *, invocation_id: str, run_token: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Persist the resolver and artifact bindings for compaction recovery."""
    lock_file = _resolved_lock_file(state_file, lock_file)
    if not isinstance(run_token, str) or not run_token.strip():
        raise LifecycleError('bind-context requires a non-empty run token')
    lifecycle = invocation_status(
        invocation_id=invocation_id,
        state_file=state_file,
        projection_file=projection_file,
    )
    if (
        lifecycle['classification'] != 'running'
        or lifecycle['phase'] != 'preflight'
    ):
        raise LifecycleConflict(
            'bind-context requires a running preflight invocation'
        )
    owner = _active_lease_token(lock_file, now_utc)
    if owner != run_token:
        raise LifecycleConflict('run token does not own the active lease')
    python_executable = os.path.abspath(sys.executable)
    document = {
        'schema_version': SCHEMA_VERSION,
        'python': python_executable,
        'version': sys.version.split()[0],
        'invocation_id': lifecycle['invocation_id'],
        'run_start_pt': lifecycle['run_start_pt'],
        'artifact_stamp': lifecycle['artifact_stamp'],
        'expected_report_file': lifecycle['expected_report_file'],
        'expected_gate_file': lifecycle['expected_gate_file'],
        'expected_status_file': lifecycle['expected_status_file'],
        'lease_token_sha256': _token_digest(run_token),
    }
    _record_lease_binding(
        document=document, state_file=state_file, now_utc=now_utc
    )
    _atomic_write_context(context_file, document)
    return _context_result('bind-context', document, lifecycle)


def acquire_and_bind_active_context(
    *, invocation_id: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
    lease_seconds: int = run_lock_module.DEFAULT_LEASE_SECONDS,
) -> dict[str, Any]:
    """Acquire the run lease and bind lifecycle context in one checked-in action.

    The raw fencing token is returned only on complete success so a nested
    executor can place it directly into private state.  An active owner is a
    token-free overlap result.  If context binding fails after acquisition,
    this function makes one owner-fenced compensating release and returns a
    bounded token-free failure.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    invocation_id = _canonical_uuid(invocation_id, 'invocation_id')
    lifecycle = invocation_status(
        invocation_id=invocation_id,
        state_file=state_file,
        projection_file=projection_file,
    )
    if (
        lifecycle['classification'] != 'running'
        or lifecycle['phase'] != 'preflight'
    ):
        raise LifecycleConflict(
            'acquire-bind-context requires a running preflight invocation'
        )

    lock_now: float | None = None
    if now_utc is not None:
        canonical_now = _now_utc(now_utc)
        lock_now = datetime.fromisoformat(
            canonical_now.replace('Z', '+00:00')
        ).timestamp()

    try:
        acquisition = run_lock_module.acquire(
            lock_file=lock_file,
            lease_seconds=lease_seconds,
            now=lock_now,
        )
    except Exception as exc:
        raise LifecycleError('run lease acquisition failed') from exc
    if (
        not isinstance(acquisition, dict)
        or acquisition.get('schema_version') != run_lock_module.SCHEMA_VERSION
        or acquisition.get('action') != 'acquire'
        or not isinstance(acquisition.get('ok'), bool)
    ):
        raise LifecycleError('run lease acquisition returned invalid state')
    if acquisition.get('ok') is not True:
        if acquisition.get('reason') != 'active_run':
            raise LifecycleError('run lease acquisition returned invalid state')
        holder = acquisition.get('holder')
        if not isinstance(holder, dict) or set(holder) != {
            'acquired_at', 'renewed_at', 'expires_at'
        } or any(not isinstance(holder[name], str) for name in holder):
            raise LifecycleError('active run holder receipt is malformed')
        return {
            'schema_version': SCHEMA_VERSION,
            'action': 'acquire-bind-context',
            'ok': False,
            'reason': 'active_run',
            'holder': holder,
        }

    raw_token = acquisition.get('token')
    try:
        run_token = _canonical_uuid(raw_token, 'acquired run token')
    except LifecycleError:
        released = False
        if isinstance(raw_token, str) and raw_token:
            try:
                release_result = run_lock_module.release(
                    raw_token, lock_file=lock_file, now=lock_now
                )
                released = release_result.get('ok') is True
            except Exception:
                released = False
        reason = (
            'acquired_token_invalid'
            if released
            else 'acquired_token_invalid_release_unconfirmed'
        )
        return {
            'schema_version': SCHEMA_VERSION,
            'action': 'acquire-bind-context',
            'ok': False,
            'reason': reason,
            'lease_released': released,
        }

    try:
        context_receipt = bind_active_context(
            invocation_id=invocation_id,
            run_token=run_token,
            state_file=state_file,
            projection_file=projection_file,
            context_file=context_file,
            lock_file=lock_file,
            now_utc=now_utc,
        )
        if (
            not isinstance(context_receipt, dict)
            or context_receipt.get('action') != 'bind-context'
            or context_receipt.get('ok') is not True
            or context_receipt.get('invocation_id') != invocation_id
            or context_receipt.get('classification') != 'running'
            or context_receipt.get('phase') != 'preflight'
        ):
            raise LifecycleError(
                'active context binding returned invalid state'
            )
    except Exception:
        released = False
        compensation_recorded = False
        try:
            release_result = run_lock_module.release(
                run_token, lock_file=lock_file, now=lock_now
            )
            released = (
                isinstance(release_result, dict)
                and release_result.get('schema_version')
                == run_lock_module.SCHEMA_VERSION
                and release_result.get('action') == 'release'
                and release_result.get('ok') is True
                and release_result.get('token') == run_token
            )
        except Exception:
            released = False
        if released:
            try:
                _record_lease_compensation(
                    invocation_id=invocation_id,
                    run_token=run_token,
                    state_file=state_file,
                    now_utc=now_utc,
                )
                compensation_recorded = True
            except Exception:
                compensation_recorded = False
        reason = (
            'bind_context_failed'
            if released and compensation_recorded
            else (
                'bind_context_failed_compensation_unrecorded'
                if released
                else 'bind_context_failed_release_unconfirmed'
            )
        )
        return {
            'schema_version': SCHEMA_VERSION,
            'action': 'acquire-bind-context',
            'ok': False,
            'reason': reason,
            'lease_released': released,
            'compensation_recorded': compensation_recorded,
        }

    return {
        'schema_version': SCHEMA_VERSION,
        'action': 'acquire-bind-context',
        'ok': True,
        'run_lock_token': run_token,
        'context_receipt': context_receipt,
    }


def recover_active_context(
    *, state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Recover an active invocation without a remembered UUID or launcher."""
    lock_file = _resolved_lock_file(state_file, lock_file)
    document = _load_context_receipt(context_file)
    current_python = os.path.abspath(sys.executable)
    if os.path.normcase(current_python) != os.path.normcase(document['python']):
        raise LifecycleConflict(
            'active context must be opened by its resolver-bound Python'
        )
    if sys.version.split()[0] != document['version']:
        raise LifecycleConflict('active context Python version no longer matches')
    lifecycle = invocation_status(
        invocation_id=document['invocation_id'],
        state_file=state_file,
        projection_file=projection_file,
    )
    for name in (
        'invocation_id', 'run_start_pt', 'artifact_stamp',
        'expected_report_file', 'expected_gate_file', 'expected_status_file',
    ):
        if lifecycle[name] != document[name]:
            raise LifecycleConflict(
                f'active context {name} no longer matches lifecycle'
            )
    owner = _active_lease_token(lock_file, now_utc)
    if _token_digest(owner) != document['lease_token_sha256']:
        raise LifecycleConflict('active context no longer owns the run lease')
    return _context_result('recover-context', document, lifecycle)


def _invocation_rows(
    connection: sqlite3.Connection, invocation_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM lifecycle_events WHERE invocation_id = ? "
        "ORDER BY sequence ASC",
        (invocation_id,),
    ).fetchall()


def _checkpoint_rows(
    connection: sqlite3.Connection, invocation_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT sequence, invocation_id, checkpoint, occurred_at_utc "
        "FROM lifecycle_checkpoints WHERE invocation_id = ? "
        "ORDER BY sequence ASC",
        (invocation_id,),
    ).fetchall()


def _checkpoint_names(rows: Sequence[sqlite3.Row]) -> frozenset[str]:
    return frozenset(row["checkpoint"] for row in rows)


_SECOND_CONTEXT_FIELDS = (
    "scratch_id",
    "source_root_id",
    "scratch_path_sha256",
    "source_root_path_sha256",
    "scratch_marker_sha256",
    "transport_marker_sha256",
    "source_root_marker_sha256",
    "scratch_device",
    "scratch_inode",
    "source_root_device",
    "source_root_inode",
    "constants_sha256",
    "daily_loss_halt_pct",
    "stop_count_halt",
)

_DAILY_LOSS_RESULT_KEYS = frozenset(
    {
        "schema_version", "status", "halt_new_buys", "trading_date_et",
        "stop_count_date_pt", "stop_fills_today", "stopped_out_symbols",
        "as_of_utc", "previous_session_date", "total_value", "halt_pct",
        "halt_threshold", "daily_pnl", "loss_amount", "loss_pct_of_total",
        "required_quote_symbols", "reconciliation",
    }
)
_DAILY_LOSS_EVIDENCE_KEYS = frozenset(
    {
        "schema_version", "action", "ok", "mode", "generation",
        "invocation_id", "constants_sha256", "daily_loss_halt_pct",
        "stop_count_halt", "daily_loss_tripped", "stop_count_tripped",
        "entry_guard_outcome", "result",
    }
)


def _broker_snapshot():
    # Lifecycle startup must remain independent of the large snapshot module;
    # load it only after an owner enters SECOND.
    import broker_snapshot

    return broker_snapshot


def _path_sha256(path: Path) -> str:
    normalized = os.path.normcase(str(path)).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _stable_regular_file_bytes(path: Path, parent: Path) -> bytes:
    try:
        before = os.lstat(path)
        resolved = path.resolve(strict=True)
        raw = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise LifecycleError(f"{path}: cannot read bound evidence: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or resolved.parent != parent
        or os.path.normcase(str(resolved)) != os.path.normcase(str(path))
        or _stat_identity(before) != _stat_identity(after)
    ):
        raise LifecycleError(f"{path}: bound evidence changed while read")
    return raw


def _second_constants_binding(expected_sha256: str) -> dict[str, Any]:
    if (
        not isinstance(expected_sha256, str)
        or _SHA256_RE.fullmatch(expected_sha256) is None
    ):
        raise LifecycleError(
            "expected constants SHA-256 must be 64 lowercase hex characters"
        )
    try:
        import validate_constants
    except ImportError as exc:
        raise LifecycleError("constants validator is unavailable") from exc
    try:
        constants = validate_constants.validate_constants_file()
    except (OSError, validate_constants.ConstantsValidationError) as exc:
        raise LifecycleError(f"constants validation failed: {exc}") from exc
    if constants.source_sha256 != expected_sha256:
        raise LifecycleConflict(
            "constants changed after startup validation"
        )
    return {
        "constants_sha256": constants.source_sha256,
        "daily_loss_halt_pct": constants.raw_values["DAILY_LOSS_HALT_PCT"],
        "stop_count_halt": int(constants.values["STOP_COUNT_HALT"]),
    }


def _second_scratch_binding(scratch_path: str) -> dict[str, Any]:
    if not isinstance(scratch_path, str) or not os.path.isabs(scratch_path):
        raise LifecycleError("scratch must be an absolute path")
    broker_snapshot_module = _broker_snapshot()
    try:
        scratch, marker, transport, source_root = (
            broker_snapshot_module._validated_source_journal_context(
                scratch_path
            )
        )
        scratch_before = os.lstat(scratch)
        source_before = os.lstat(source_root)
        if (
            stat.S_ISLNK(scratch_before.st_mode)
            or not stat.S_ISDIR(scratch_before.st_mode)
            or stat.S_ISLNK(source_before.st_mode)
            or not stat.S_ISDIR(source_before.st_mode)
        ):
            raise LifecycleError("SECOND scratch binding is not a stable directory")
        scratch_marker_path = scratch / broker_snapshot_module.SCRATCH_MARKER
        transport_marker_path = scratch / broker_snapshot_module.TRANSPORT_MARKER
        source_marker_path = (
            source_root / broker_snapshot_module.TRANSPORT_ROOT_MARKER
        )
        scratch_marker_raw = _stable_regular_file_bytes(
            scratch_marker_path, scratch
        )
        transport_marker_raw = _stable_regular_file_bytes(
            transport_marker_path, scratch
        )
        source_marker_raw = _stable_regular_file_bytes(
            source_marker_path, source_root
        )
        scratch_after = os.lstat(scratch)
        source_after = os.lstat(source_root)
        rebound_scratch, rebound_marker, rebound_transport, rebound_source = (
            broker_snapshot_module._validated_source_journal_context(
                str(scratch)
            )
        )
    except LifecycleError:
        raise
    except (OSError, broker_snapshot_module.SnapshotError) as exc:
        raise LifecycleError(
            f"SECOND scratch binding validation failed: {exc}"
        ) from exc
    if (
        _stat_identity(scratch_before) != _stat_identity(scratch_after)
        or _stat_identity(source_before) != _stat_identity(source_after)
        or rebound_scratch != scratch
        or rebound_source != source_root
        or rebound_marker != marker
        or rebound_transport != transport
    ):
        raise LifecycleError("SECOND scratch binding changed during validation")
    return {
        "scratch_id": str(marker["scratch_id"]),
        "source_root_id": str(transport["source_root_id"]),
        "scratch_path_sha256": _path_sha256(scratch),
        "source_root_path_sha256": _path_sha256(source_root),
        "scratch_marker_sha256": hashlib.sha256(scratch_marker_raw).hexdigest(),
        "transport_marker_sha256": hashlib.sha256(
            transport_marker_raw
        ).hexdigest(),
        "source_root_marker_sha256": hashlib.sha256(
            source_marker_raw
        ).hexdigest(),
        "scratch_device": str(scratch_after.st_dev),
        "scratch_inode": str(scratch_after.st_ino),
        "source_root_device": str(source_after.st_dev),
        "source_root_inode": str(source_after.st_ino),
    }


def _second_context_row(
    connection: sqlite3.Connection, invocation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM lifecycle_second_contexts WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()


def _second_evidence_row(
    connection: sqlite3.Connection, invocation_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM lifecycle_second_evidence WHERE invocation_id = ?",
        (invocation_id,),
    ).fetchone()


def _binding_matches(row: sqlite3.Row, binding: Mapping[str, Any]) -> bool:
    return all(row[name] == binding[name] for name in _SECOND_CONTEXT_FIELDS)


def _stable_json_object(path: Path, parent: Path) -> tuple[Mapping[str, Any], bytes]:
    broker_snapshot_module = _broker_snapshot()
    try:
        before = os.lstat(path)
        document, raw = broker_snapshot_module._read_source(str(path))
        after = os.lstat(path)
    except (OSError, broker_snapshot_module.SnapshotError) as exc:
        raise LifecycleError(f"{path}: invalid durable result: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or path.resolve(strict=True).parent != parent
        or _stat_identity(before) != _stat_identity(after)
        or not isinstance(document, Mapping)
    ):
        raise LifecycleError(f"{path}: durable result changed while read")
    return document, raw


def _daily_loss_source_digest(
    *,
    scratch: Path,
    generation: str,
    paths_by_kind: Mapping[str, Sequence[str]],
) -> str:
    broker_snapshot_module = _broker_snapshot()
    expected_kinds = {"portfolio", "positions", "orders", "quotes"}
    if set(paths_by_kind) != expected_kinds:
        raise LifecycleError("daily-loss source kinds are incomplete")
    if len(paths_by_kind["portfolio"]) != 1:
        raise LifecycleError("daily-loss evidence requires one portfolio input")
    if not paths_by_kind["positions"] or not paths_by_kind["orders"]:
        raise LifecycleError(
            "daily-loss evidence requires positions and orders inputs"
        )
    flattened = [
        os.path.abspath(path)
        for kind in ("portfolio", "positions", "orders", "quotes")
        for path in paths_by_kind[kind]
    ]
    normalized = [os.path.normcase(path) for path in flattened]
    if len(normalized) != len(set(normalized)):
        raise LifecycleError("daily-loss evidence contains duplicate inputs")
    for path in map(Path, flattened):
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise LifecycleError(f"{path}: daily-loss input is unavailable") from exc
        if resolved.parent != scratch:
            raise LifecycleError(
                f"{path}: daily-loss input is outside the bound scratch"
            )
    try:
        broker_snapshot_module.validate_generation_inputs(
            paths_by_kind, generation
        )
        _validated_scratch, scratch_marker = (
            broker_snapshot_module.validate_scratch_directory(str(scratch))
        )
    except broker_snapshot_module.SnapshotError as exc:
        raise LifecycleError(
            f"daily-loss staged provenance is invalid: {exc}"
        ) from exc
    manifest: list[dict[str, str]] = []
    for kind in ("portfolio", "positions", "orders", "quotes"):
        try:
            metadata_documents = [
                broker_snapshot_module._validated_stage_metadata(
                    path_text,
                    scratch=scratch,
                    marker=scratch_marker,
                    expected_generation=generation,
                    expected_kind=kind,
                )
                for path_text in paths_by_kind[kind]
            ]
        except broker_snapshot_module.SnapshotError as exc:
            raise LifecycleError(
                f"daily-loss {kind} staged provenance is invalid: {exc}"
            ) from exc
        if [
            int(document["set_index"]) for document in metadata_documents
        ] != list(range(1, len(metadata_documents) + 1)):
            raise LifecycleError(
                f"daily-loss {kind} inputs are not in sealed set order"
            )
        for path_text in paths_by_kind[kind]:
            path = Path(os.path.abspath(path_text))
            payload = _stable_regular_file_bytes(path, scratch)
            provenance_path = Path(
                str(path) + broker_snapshot_module.STAGE_METADATA_SUFFIX
            )
            provenance = _stable_regular_file_bytes(provenance_path, scratch)
            manifest.append(
                {
                    "kind": kind,
                    "filename": path.name,
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "provenance_sha256": hashlib.sha256(
                        provenance
                    ).hexdigest(),
                }
            )
    try:
        broker_snapshot_module.validate_generation_inputs(
            paths_by_kind, generation
        )
    except broker_snapshot_module.SnapshotError as exc:
        raise LifecycleError(
            f"daily-loss staged provenance changed during validation: {exc}"
        ) from exc
    confirmed_manifest: list[dict[str, str]] = []
    for kind in ("portfolio", "positions", "orders", "quotes"):
        for path_text in paths_by_kind[kind]:
            path = Path(os.path.abspath(path_text))
            payload = _stable_regular_file_bytes(path, scratch)
            provenance = _stable_regular_file_bytes(
                Path(str(path) + broker_snapshot_module.STAGE_METADATA_SUFFIX),
                scratch,
            )
            confirmed_manifest.append(
                {
                    "kind": kind,
                    "filename": path.name,
                    "payload_sha256": hashlib.sha256(payload).hexdigest(),
                    "provenance_sha256": hashlib.sha256(
                        provenance
                    ).hexdigest(),
                }
            )
    manifest.sort(key=lambda item: (item["kind"], item["filename"]))
    confirmed_manifest.sort(
        key=lambda item: (item["kind"], item["filename"])
    )
    if confirmed_manifest != manifest:
        raise LifecycleError(
            "daily-loss staged evidence changed before checkpoint binding"
        )
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_open_invocation(rows: Sequence[sqlite3.Row]) -> None:
    if not rows or rows[0]["event_type"] != "start":
        raise LifecycleConflict("invocation has not been started")
    if any(row["event_type"] == "finish" for row in rows):
        raise LifecycleConflict("invocation is already finished")


def _guard_critical_transition(
    *,
    action: str,
    phase: str,
    classification: str,
    reason_code: str | None,
    checkpoint_rows: Sequence[sqlite3.Row],
    pending_checkpoint: str | None = None,
) -> None:
    checkpoints = set(_checkpoint_names(checkpoint_rows))
    if pending_checkpoint is not None:
        if pending_checkpoint not in _SECOND_TERMINAL_CHECKPOINTS:
            raise LifecycleError("pending SECOND checkpoint is invalid")
        checkpoints.add(pending_checkpoint)
    checkpoints = frozenset(checkpoints)
    terminal_checkpoints = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
    if action == "event" and phase == "daily-loss":
        raise LifecycleConflict(
            "daily-loss phase can be entered only by enter-second"
        )
    if action == "event" and phase in {
        "entry-scan", "entry-evaluation", "order-placement",
    } and terminal_checkpoints != {"daily-loss-clear"}:
        raise LifecycleConflict(
            "entry phase requires exactly the deterministic daily-loss-clear "
            "checkpoint"
        )
    if "entry-eligible" not in checkpoints:
        return
    has_terminal = len(terminal_checkpoints) == 1
    if action == "event":
        if not terminal_checkpoints:
            allowed = phase == "daily-loss"
        elif "daily-loss-clear" in terminal_checkpoints:
            allowed = phase in {
                "entry-scan", "entry-evaluation", "order-placement",
                "final-refresh", "report", "status-publish",
            }
        else:
            allowed = phase in {"final-refresh", "report", "status-publish"}
        if not allowed:
            raise LifecycleConflict(
                "entry-eligible invocation cannot use this phase before or "
                "after its DAILY-LOSS terminal checkpoint"
            )
        return
    if action != "finish" or classification == "lease-lost":
        return
    if classification == "completed":
        allowed = "daily-loss-clear" in terminal_checkpoints
    elif classification == "risk-halt":
        if reason_code in {"daily-loss-tripped", "stop-count-tripped"}:
            allowed = "daily-loss-tripped" in terminal_checkpoints
        else:
            allowed = "daily-loss-clear" in terminal_checkpoints
    elif classification == "snapshot-failure":
        allowed = bool(
            terminal_checkpoints
            & {"daily-loss-clear", "daily-loss-snapshot-terminal"}
        )
    elif classification == "coordination-halt":
        allowed = bool(
            terminal_checkpoints
            & {"daily-loss-clear", "daily-loss-coordination-terminal"}
        )
    elif classification == "final-status-unavailable":
        allowed = has_terminal
    else:
        allowed = has_terminal
    if not allowed:
        raise LifecycleConflict(
            "entry-eligible invocation cannot use this terminal "
            "classification before the required daily-loss checkpoint"
        )


def _owned_context_lifecycle(
    *,
    invocation_id: str,
    run_token: str,
    state_file: str,
    projection_file: str,
    context_file: str,
    lock_file: str,
    now_utc: str | None,
) -> tuple[dict[str, Any], str, float | None]:
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    run_token = _canonical_uuid(run_token, "run_token")
    context = _load_context_receipt(context_file)
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(
        context["python"]
    ):
        raise LifecycleConflict(
            "active context belongs to a different Python executable"
        )
    if sys.version.split()[0] != context["version"]:
        raise LifecycleConflict("active context Python version no longer matches")
    if context["invocation_id"] != invocation_id:
        raise LifecycleConflict(
            "active context does not belong to this invocation"
        )
    if context["lease_token_sha256"] != _token_digest(run_token):
        raise LifecycleConflict("run token does not match active context")
    lifecycle = invocation_status(
        invocation_id=invocation_id,
        state_file=state_file,
        projection_file=projection_file,
    )
    for name in (
        "invocation_id", "run_start_pt", "artifact_stamp",
        "expected_report_file", "expected_gate_file", "expected_status_file",
    ):
        if lifecycle[name] != context[name]:
            raise LifecycleConflict(
                f"active context {name} no longer matches lifecycle"
            )
    owner = _active_lease_token(lock_file, now_utc)
    if owner != run_token:
        raise LifecycleConflict("run token does not own the active lease")
    lock_now: float | None = None
    if now_utc is not None:
        lock_now = datetime.fromisoformat(
            _now_utc(now_utc).replace("Z", "+00:00")
        ).timestamp()
    return lifecycle, run_token, lock_now


def _renew_owned_lease(
    *, run_token: str, lock_file: str, lock_now: float | None
) -> None:
    try:
        renewal = run_lock_module.renew(
            run_token,
            lock_file=lock_file,
            lease_seconds=run_lock_module.DEFAULT_LEASE_SECONDS,
            now=lock_now,
        )
    except Exception as exc:
        raise LifecycleError("critical-phase lease renewal failed") from exc
    if (
        not isinstance(renewal, dict)
        or renewal.get("schema_version") != run_lock_module.SCHEMA_VERSION
        or renewal.get("action") != "renew"
        or renewal.get("ok") is not True
        or renewal.get("token") != run_token
        or renewal.get("lease_seconds") != run_lock_module.DEFAULT_LEASE_SECONDS
    ):
        raise LifecycleConflict("critical-phase lease renewal was rejected")


def authorize_entry_intent(
    *,
    run_token: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Prove that the current owner may begin or retry a dip-buy intent.

    The invocation identity comes only from the checked active-context receipt;
    callers cannot select another invocation.  The returned receipt is token
    free.  The lease is renewed before the final append-only lifecycle proof is
    read, so a stale or replaced runner cannot authorize an entry.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    context = _load_context_receipt(context_file)
    invocation_id = context["invocation_id"]
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    _renew_owned_lease(
        run_token=run_token, lock_file=lock_file, lock_now=lock_now
    )
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN")
        rows = _invocation_rows(connection, invocation_id)
        _require_open_invocation(rows)
        if rows[-1]["classification"] != "running":
            raise LifecycleConflict("entry authorization requires a running invocation")
        binding = _lease_binding_row(connection, invocation_id)
        if binding is None or binding["lease_token_sha256"] != _token_digest(run_token):
            raise LifecycleConflict("entry authorization lease binding does not match")
        for name in (
            "run_start_pt", "artifact_stamp", "expected_report_file",
            "expected_gate_file", "expected_status_file",
        ):
            if binding[name] != context[name]:
                raise LifecycleConflict(
                    "entry authorization owner artifacts do not match"
                )
        checkpoints = _checkpoint_names(
            _checkpoint_rows(connection, invocation_id)
        )
        terminal = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
        if terminal != {"daily-loss-clear"}:
            raise LifecycleConflict(
                "dip-buy requires exactly the deterministic daily-loss-clear checkpoint"
            )
        if not {"entry-eligible", "daily-loss-attempted"} <= checkpoints:
            raise LifecycleConflict("dip-buy requires the bound SECOND attempt")
        second_context = _second_context_row(connection, invocation_id)
        evidence = _second_evidence_row(connection, invocation_id)
        if second_context is None or evidence is None:
            raise LifecycleConflict("dip-buy requires durable SECOND evidence")
        if (
            evidence["outcome"] != "clear"
            or evidence["constants_sha256"] != second_context["constants_sha256"]
        ):
            raise LifecycleConflict("dip-buy SECOND evidence is not clear")
        phase = rows[-1]["phase"]
        if phase not in {"daily-loss", "entry-scan", "entry-evaluation", "order-placement"}:
            raise LifecycleConflict("dip-buy is not authorized in the current phase")
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "authorize-entry-intent",
        "ok": True,
        "invocation_id": invocation_id,
        "phase": phase,
        "entry_guard_outcome": "clear",
        "lease_renewed": True,
    }


def enter_second(
    *,
    invocation_id: str,
    run_token: str,
    scratch: str,
    expected_constants_sha256: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Owner-fenced transition from FIRST into attempted DAILY-LOSS.

    Lease renewal, active-context validation, and the append-only phase facts
    are owned by this checked-in action.  Its receipt is deliberately token
    free.  A retry after a lost receipt is idempotent.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    if lifecycle["phase"] not in {"position-management", "daily-loss"}:
        raise LifecycleConflict(
            "enter-second requires position-management or an idempotent "
            "daily-loss retry"
        )
    _renew_owned_lease(
        run_token=run_token, lock_file=lock_file, lock_now=lock_now
    )
    binding = _second_scratch_binding(scratch)
    binding.update(_second_constants_binding(expected_constants_sha256))
    occurred_at = _now_utc(now_utc)
    connection = _connect(state_file)
    recorded = False
    event_sequence: int | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle_rows = _invocation_rows(connection, lifecycle["invocation_id"])
        _require_open_invocation(lifecycle_rows)
        checkpoint_rows = _checkpoint_rows(
            connection, lifecycle["invocation_id"]
        )
        context_row = _second_context_row(
            connection, lifecycle["invocation_id"]
        )
        checkpoints = _checkpoint_names(checkpoint_rows)
        expected = {"entry-eligible", "daily-loss-attempted"}
        if expected.issubset(checkpoints):
            if checkpoints & _SECOND_TERMINAL_CHECKPOINTS:
                raise LifecycleConflict(
                    "enter-second cannot run after DAILY-LOSS is terminal"
                )
            if lifecycle_rows[-1]["phase"] != "daily-loss":
                raise LifecycleError(
                    "DAILY-LOSS checkpoints disagree with lifecycle phase"
                )
            if context_row is None or not _binding_matches(
                context_row, binding
            ):
                raise LifecycleConflict(
                    "DAILY-LOSS scratch binding differs from its entry boundary"
                )
            event_sequence = int(lifecycle_rows[-1]["sequence"])
        else:
            if checkpoints or context_row is not None:
                raise LifecycleError(
                    "DAILY-LOSS entry boundary is incomplete"
                )
            if lifecycle_rows[-1]["phase"] != "position-management":
                raise LifecycleConflict(
                    "enter-second requires the current position-management phase"
                )
            occurred_value = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00")
            )
            previous_value = datetime.fromisoformat(
                lifecycle_rows[-1]["occurred_at_utc"].replace("Z", "+00:00")
            )
            if occurred_value < previous_value:
                raise LifecycleError(
                    "enter-second time cannot precede lifecycle activity"
                )
            cursor = connection.execute(
                """
                INSERT INTO lifecycle_events (
                    invocation_id, event_type, classification,
                    occurred_at_utc, run_start_pt, phase, reason_code,
                    report_file, status_file
                ) VALUES (?, 'event', 'running', ?, NULL, 'daily-loss',
                          NULL, NULL, NULL)
                """,
                (lifecycle["invocation_id"], occurred_at),
            )
            event_sequence = int(cursor.lastrowid)
            for checkpoint in ("entry-eligible", "daily-loss-attempted"):
                connection.execute(
                    "INSERT INTO lifecycle_checkpoints "
                    "(invocation_id, checkpoint, occurred_at_utc) "
                    "VALUES (?, ?, ?)",
                    (lifecycle["invocation_id"], checkpoint, occurred_at),
                )
            context_columns = ", ".join(_SECOND_CONTEXT_FIELDS)
            placeholders = ", ".join("?" for _ in _SECOND_CONTEXT_FIELDS)
            connection.execute(
                "INSERT INTO lifecycle_second_contexts "
                f"(invocation_id, {context_columns}, occurred_at_utc) "
                f"VALUES (?, {placeholders}, ?)",
                (
                    lifecycle["invocation_id"],
                    *(binding[name] for name in _SECOND_CONTEXT_FIELDS),
                    occurred_at,
                ),
            )
            recorded = True
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    if recorded:
        _publish_after_append(
            "enter-second", lifecycle["invocation_id"],
            state_file, projection_file,
        )
    else:
        publish_projection(state_file, projection_file)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "enter-second",
        "ok": True,
        "invocation_id": lifecycle["invocation_id"],
        "phase": "daily-loss",
        "recorded": recorded,
        "event_sequence": event_sequence,
        "entry_eligible": True,
        "daily_loss_attempted": True,
        "scratch_id": binding["scratch_id"],
        "source_root_id": binding["source_root_id"],
        "constants_sha256": binding["constants_sha256"],
        "daily_loss_halt_pct": binding["daily_loss_halt_pct"],
        "stop_count_halt": binding["stop_count_halt"],
        "lease_renewed": True,
    }


def complete_second(
    *,
    invocation_id: str,
    run_token: str,
    outcome: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Record one fail-closed terminal DAILY-LOSS orchestration outcome.

    Public callers can terminalize a failed SECOND attempt, but cannot mint a
    financial ``clear`` or ``tripped`` checkpoint.  Calculation-mode
    ``daily_loss.py`` derives those outcomes from its deterministic result and
    calls :func:`complete_second_result` internally.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    outcome = _enum(outcome, PUBLIC_SECOND_OUTCOMES, "outcome")
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    if lifecycle["phase"] != "daily-loss":
        raise LifecycleConflict("complete-second requires the daily-loss phase")
    _renew_owned_lease(
        run_token=run_token, lock_file=lock_file, lock_now=lock_now
    )
    occurred_at = _now_utc(now_utc)
    checkpoint = _SECOND_OUTCOME_CHECKPOINT[outcome]
    connection = _connect(state_file)
    recorded = False
    sequence: int | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle_rows = _invocation_rows(connection, lifecycle["invocation_id"])
        _require_open_invocation(lifecycle_rows)
        if lifecycle_rows[-1]["phase"] != "daily-loss":
            raise LifecycleConflict(
                "complete-second requires the current daily-loss phase"
            )
        checkpoint_rows = _checkpoint_rows(
            connection, lifecycle["invocation_id"]
        )
        context_row = _second_context_row(
            connection, lifecycle["invocation_id"]
        )
        if context_row is None:
            raise LifecycleError("DAILY-LOSS scratch binding is missing")
        checkpoints = _checkpoint_names(checkpoint_rows)
        if not {"entry-eligible", "daily-loss-attempted"}.issubset(checkpoints):
            raise LifecycleError("DAILY-LOSS attempt boundary is incomplete")
        terminal = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
        if terminal:
            if terminal != {checkpoint}:
                raise LifecycleConflict(
                    "DAILY-LOSS already has a different terminal outcome"
                )
            existing = next(
                row for row in checkpoint_rows
                if row["checkpoint"] == checkpoint
            )
            sequence = int(existing["sequence"])
            occurred_at = existing["occurred_at_utc"]
        else:
            occurred_value = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00")
            )
            latest_value = max(
                datetime.fromisoformat(
                    row["occurred_at_utc"].replace("Z", "+00:00")
                )
                for row in [*lifecycle_rows, *checkpoint_rows]
            )
            if occurred_value < latest_value:
                raise LifecycleError(
                    "complete-second time cannot precede lifecycle activity"
                )
            cursor = connection.execute(
                "INSERT INTO lifecycle_checkpoints "
                "(invocation_id, checkpoint, occurred_at_utc) VALUES (?, ?, ?)",
                (lifecycle["invocation_id"], checkpoint, occurred_at),
            )
            sequence = int(cursor.lastrowid)
            recorded = True
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "complete-second",
        "ok": True,
        "invocation_id": lifecycle["invocation_id"],
        "phase": "daily-loss",
        "outcome": outcome,
        "recorded": recorded,
        "sequence": sequence,
        "lease_renewed": True,
    }


def validate_second_result_target(
    *,
    invocation_id: str,
    run_token: str,
    result_file: str,
    generation: str,
    expected_constants_sha256: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Prove the lifecycle-bound no-clobber target before it is created."""
    lock_file = _resolved_lock_file(state_file, lock_file)
    generation = _enum(generation, ("A", "B"), "generation")
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    if lifecycle["phase"] != "daily-loss":
        raise LifecycleConflict(
            "daily-loss target validation requires the daily-loss phase"
        )
    if not isinstance(result_file, str) or not os.path.isabs(result_file):
        raise LifecycleError("daily-loss result path must be absolute")
    result_path = Path(result_file)
    try:
        scratch = result_path.parent.resolve(strict=True)
    except OSError as exc:
        raise LifecycleError("daily-loss result scratch is unavailable") from exc
    if result_path.name != f"daily-loss-{generation.lower()}.json":
        raise LifecycleError(
            "daily-loss result filename must match its snapshot generation"
        )
    # Compare the target directory to the append-only binding before opening
    # or interpreting any marker under that caller-selected directory.  This
    # both narrows the trust boundary and guarantees an out-of-scratch target
    # fails before it can create an authoritative-looking result file.
    connection = _connect(state_file)
    try:
        context_row = _second_context_row(
            connection, lifecycle["invocation_id"]
        )
        if context_row is None:
            raise LifecycleConflict(
                "daily-loss target has no bound SECOND context"
            )
        if context_row["scratch_path_sha256"] != _path_sha256(scratch):
            raise LifecycleConflict(
                "daily-loss target is outside the bound SECOND context"
            )
    finally:
        connection.close()
    binding = _second_scratch_binding(str(scratch))
    binding.update(_second_constants_binding(expected_constants_sha256))
    connection = _connect(state_file)
    try:
        lifecycle_rows = _invocation_rows(connection, lifecycle["invocation_id"])
        _require_open_invocation(lifecycle_rows)
        if lifecycle_rows[-1]["phase"] != "daily-loss":
            raise LifecycleConflict(
                "daily-loss target validation requires current daily-loss phase"
            )
        checkpoint_rows = _checkpoint_rows(
            connection, lifecycle["invocation_id"]
        )
        checkpoints = _checkpoint_names(checkpoint_rows)
        terminal = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
        if not {"entry-eligible", "daily-loss-attempted"}.issubset(
            checkpoints
        ):
            raise LifecycleConflict(
                "daily-loss target requires one open attempted SECOND boundary"
            )
        context_row = _second_context_row(
            connection, lifecycle["invocation_id"]
        )
        if context_row is None or not _binding_matches(context_row, binding):
            raise LifecycleConflict(
                "daily-loss target is outside the bound SECOND context"
            )
        if terminal:
            evidence = _second_evidence_row(
                connection, lifecycle["invocation_id"]
            )
            if (
                terminal
                not in ({"daily-loss-clear"}, {"daily-loss-tripped"})
                or evidence is None
                or evidence["result_file"] != result_path.name
                or evidence["generation"] != generation
                or evidence["constants_sha256"]
                != binding["constants_sha256"]
            ):
                raise LifecycleConflict(
                    "daily-loss target is already terminal with different evidence"
                )
    finally:
        connection.close()
    _renew_owned_lease(
        run_token=run_token, lock_file=lock_file, lock_now=lock_now
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "validate-second-result-target",
        "ok": True,
        "invocation_id": lifecycle["invocation_id"],
        "generation": generation,
        "result_file": result_path.name,
        "scratch_id": binding["scratch_id"],
        "constants_sha256": binding["constants_sha256"],
        "lease_renewed": True,
    }


def complete_second_result(
    *,
    invocation_id: str,
    run_token: str,
    result_file: str,
    generation: str,
    portfolio: str,
    positions: Sequence[str],
    orders: Sequence[str],
    quotes: Sequence[str] = (),
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Bind a deterministic ``daily_loss.py`` result to SECOND completion.

    This is intentionally not exposed as a lifecycle CLI action.  The
    calculation-mode daily-loss helper calls it only after publishing and
    reading back its deterministic result.  The result status selects the
    checkpoint; no caller supplies ``clear`` or ``tripped``.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    generation = _enum(generation, ("A", "B"), "generation")
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    if lifecycle["phase"] != "daily-loss":
        raise LifecycleConflict(
            "complete-second-result requires the daily-loss phase"
        )
    _renew_owned_lease(
        run_token=run_token, lock_file=lock_file, lock_now=lock_now
    )
    if not isinstance(result_file, str) or not os.path.isabs(result_file):
        raise LifecycleError("daily-loss result path must be absolute")
    result_path = Path(result_file)
    try:
        scratch = result_path.parent.resolve(strict=True)
    except OSError as exc:
        raise LifecycleError("daily-loss result scratch is unavailable") from exc
    if result_path.name != f"daily-loss-{generation.lower()}.json":
        raise LifecycleError(
            "daily-loss result filename must match its snapshot generation"
        )
    binding = _second_scratch_binding(str(scratch))
    paths_by_kind: dict[str, Sequence[str]] = {
        "portfolio": [portfolio],
        "positions": list(positions),
        "orders": list(orders),
        "quotes": list(quotes),
    }
    sources_sha256 = _daily_loss_source_digest(
        scratch=scratch,
        generation=generation,
        paths_by_kind=paths_by_kind,
    )
    document, raw = _stable_json_object(result_path, scratch)
    if set(document) != _DAILY_LOSS_EVIDENCE_KEYS:
        raise LifecycleError("daily-loss durable evidence has an unsupported schema")
    schema_version = document.get("schema_version")
    result_document = document.get("result")
    if (
        isinstance(schema_version, bool)
        or schema_version != SCHEMA_VERSION
        or document.get("action") != "daily-loss"
        or document.get("ok") is not True
        or document.get("mode") != "calculation"
        or document.get("generation") != generation
        or document.get("invocation_id") != lifecycle["invocation_id"]
        or not isinstance(result_document, Mapping)
        or set(result_document) != _DAILY_LOSS_RESULT_KEYS
    ):
        raise LifecycleError("daily-loss durable evidence binding is inconsistent")
    constants_binding = _second_constants_binding(
        document.get("constants_sha256")
    )
    binding.update(constants_binding)
    stop_count_halt = document.get("stop_count_halt")
    daily_loss_tripped = document.get("daily_loss_tripped")
    stop_count_tripped = document.get("stop_count_tripped")
    entry_guard_outcome = document.get("entry_guard_outcome")
    if (
        document.get("daily_loss_halt_pct")
        != constants_binding["daily_loss_halt_pct"]
        or result_document.get("halt_pct")
        != constants_binding["daily_loss_halt_pct"]
        or isinstance(stop_count_halt, bool)
        or stop_count_halt != constants_binding["stop_count_halt"]
        or not isinstance(daily_loss_tripped, bool)
        or not isinstance(stop_count_tripped, bool)
        or entry_guard_outcome not in {"clear", "tripped"}
    ):
        raise LifecycleError(
            "daily-loss durable evidence constants/guard binding is inconsistent"
        )
    result_schema_version = result_document.get("schema_version")
    status_value = result_document.get("status")
    halt_new_buys = result_document.get("halt_new_buys")
    if (
        isinstance(result_schema_version, bool)
        or result_schema_version != SCHEMA_VERSION
        or status_value not in {"clear", "tripped"}
        or not isinstance(halt_new_buys, bool)
        or (status_value == "tripped") != halt_new_buys
    ):
        raise LifecycleError(
            "daily-loss durable result status/halt binding is inconsistent"
        )
    run_start = datetime.fromisoformat(lifecycle["run_start_pt"])
    run_start_utc = run_start.astimezone(timezone.utc)
    eastern_start, _name, _offset = zone_time(
        run_start_utc, EASTERN_STD_OFFSET, "EST", "EDT"
    )
    if (
        result_document.get("stop_count_date_pt")
        != run_start.date().isoformat()
        or result_document.get("trading_date_et")
        != eastern_start.date().isoformat()
    ):
        raise LifecycleError(
            "daily-loss durable result dates do not match lifecycle START"
        )
    try:
        import daily_loss as daily_loss_module
    except ImportError as exc:
        raise LifecycleError("daily-loss deterministic helper is unavailable") from exc
    try:
        recomputed = daily_loss_module.calculate_daily_loss(
            daily_loss_module.load_json(portfolio),
            [daily_loss_module.load_json(path) for path in positions],
            [daily_loss_module.load_json(path) for path in orders],
            [daily_loss_module.load_json(path) for path in quotes],
            result_document["trading_date_et"],
            result_document["as_of_utc"],
            result_document["halt_pct"],
            result_document["stop_count_date_pt"],
        )
    except (OSError, daily_loss_module.DailyLossError) as exc:
        raise LifecycleError(
            f"daily-loss deterministic evidence recomputation failed: {exc}"
        ) from exc
    if recomputed != result_document:
        raise LifecycleError(
            "daily-loss durable result differs from deterministic recomputation"
        )
    derived_daily_loss_tripped = bool(result_document["halt_new_buys"])
    derived_stop_count_tripped = (
        int(result_document["stop_fills_today"])
        >= constants_binding["stop_count_halt"]
    )
    derived_outcome = (
        "tripped"
        if derived_daily_loss_tripped or derived_stop_count_tripped
        else "clear"
    )
    if (
        daily_loss_tripped != derived_daily_loss_tripped
        or stop_count_tripped != derived_stop_count_tripped
        or entry_guard_outcome != derived_outcome
    ):
        raise LifecycleError(
            "daily-loss durable entry-guard outcome is inconsistent"
        )
    outcome = derived_outcome
    checkpoint = _SECOND_OUTCOME_CHECKPOINT[outcome]
    result_sha256 = hashlib.sha256(raw).hexdigest()
    # Prove the exact published bytes are still present immediately before
    # the append-only checkpoint transaction.
    confirm_document, confirm_raw = _stable_json_object(result_path, scratch)
    if confirm_document != document or confirm_raw != raw:
        raise LifecycleError("daily-loss durable result changed before binding")
    occurred_at = _now_utc(now_utc)
    connection = _connect(state_file)
    recorded = False
    sequence: int | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle_rows = _invocation_rows(connection, lifecycle["invocation_id"])
        _require_open_invocation(lifecycle_rows)
        if lifecycle_rows[-1]["phase"] != "daily-loss":
            raise LifecycleConflict(
                "complete-second-result requires the current daily-loss phase"
            )
        checkpoint_rows = _checkpoint_rows(
            connection, lifecycle["invocation_id"]
        )
        checkpoints = _checkpoint_names(checkpoint_rows)
        if not {"entry-eligible", "daily-loss-attempted"}.issubset(
            checkpoints
        ):
            raise LifecycleError("DAILY-LOSS attempt boundary is incomplete")
        context_row = _second_context_row(
            connection, lifecycle["invocation_id"]
        )
        if context_row is None or not _binding_matches(context_row, binding):
            raise LifecycleConflict(
                "daily-loss result does not belong to the bound SECOND scratch"
            )
        terminal = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
        evidence = _second_evidence_row(
            connection, lifecycle["invocation_id"]
        )
        expected_evidence = {
            "outcome": outcome,
            "generation": generation,
            "result_file": result_path.name,
            "result_sha256": result_sha256,
            "sources_sha256": sources_sha256,
            "constants_sha256": constants_binding["constants_sha256"],
        }
        if terminal:
            if terminal != {checkpoint} or evidence is None:
                raise LifecycleConflict(
                    "DAILY-LOSS already has a different terminal outcome"
                )
            if any(
                evidence[name] != value
                for name, value in expected_evidence.items()
            ):
                raise LifecycleConflict(
                    "DAILY-LOSS retry evidence differs from the recorded result"
                )
            existing = next(
                row for row in checkpoint_rows
                if row["checkpoint"] == checkpoint
            )
            sequence = int(existing["sequence"])
            occurred_at = existing["occurred_at_utc"]
        else:
            if evidence is not None:
                raise LifecycleError(
                    "DAILY-LOSS evidence exists without a terminal checkpoint"
                )
            occurred_value = datetime.fromisoformat(
                occurred_at.replace("Z", "+00:00")
            )
            latest_value = max(
                datetime.fromisoformat(
                    row["occurred_at_utc"].replace("Z", "+00:00")
                )
                for row in [*lifecycle_rows, *checkpoint_rows, context_row]
            )
            if occurred_value < latest_value:
                raise LifecycleError(
                    "complete-second-result time cannot precede lifecycle activity"
                )
            evidence_cursor = connection.execute(
                """
                INSERT INTO lifecycle_second_evidence (
                    invocation_id, outcome, generation, result_file,
                    result_sha256, sources_sha256, constants_sha256,
                    occurred_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lifecycle["invocation_id"], outcome, generation,
                    result_path.name, result_sha256, sources_sha256,
                    constants_binding["constants_sha256"], occurred_at,
                ),
            )
            cursor = connection.execute(
                "INSERT INTO lifecycle_checkpoints "
                "(invocation_id, checkpoint, occurred_at_utc) VALUES (?, ?, ?)",
                (lifecycle["invocation_id"], checkpoint, occurred_at),
            )
            if evidence_cursor.lastrowid is None:
                raise LifecycleError("DAILY-LOSS evidence was not recorded")
            sequence = int(cursor.lastrowid)
            recorded = True
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    # Checkpoints and their evidence are deliberately absent from the public
    # dashboard projection, so the append above does not make that projection
    # stale and must not add a post-commit publication failure seam.  A lost
    # stdout receipt can safely replay the identical durable evidence.
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "complete-second-result",
        "ok": True,
        "invocation_id": lifecycle["invocation_id"],
        "phase": "daily-loss",
        "outcome": outcome,
        "generation": generation,
        "result_file": result_path.name,
        "result_sha256": result_sha256,
        "sources_sha256": sources_sha256,
        "constants_sha256": constants_binding["constants_sha256"],
        "daily_loss_tripped": derived_daily_loss_tripped,
        "stop_count_tripped": derived_stop_count_tripped,
        "recorded": recorded,
        "sequence": sequence,
        "lease_renewed": True,
    }


def _record_abandoned_second_terminal_if_exact(
    connection: sqlite3.Connection,
    invocation_id: str,
    occurred_at: str,
) -> bool:
    """Coherently close the exact orphaned SECOND shape, never guess finance."""
    rows = _invocation_rows(connection, invocation_id)
    checkpoints = _checkpoint_names(
        _checkpoint_rows(connection, invocation_id)
    )
    if (
        rows
        and rows[-1]["phase"] == "daily-loss"
        and checkpoints == {"entry-eligible", "daily-loss-attempted"}
        and _second_context_row(connection, invocation_id) is not None
        and _second_evidence_row(connection, invocation_id) is None
    ):
        connection.execute(
            "INSERT INTO lifecycle_checkpoints "
            "(invocation_id, checkpoint, occurred_at_utc) VALUES (?, ?, ?)",
            (
                invocation_id,
                "daily-loss-coordination-terminal",
                occurred_at,
            ),
        )
        return True
    return False


def reconcile_abandoned_invocation(
    *,
    invocation_id: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Terminalize one old, idle invocation only when no lease is live.

    This recovery never releases another owner's lease and never attaches
    report or status artifacts.  Once absence/expiry is proven atomically, it
    takes a short private fencing lease so the abandoned owner cannot resume
    while the terminal event is committed, then releases only that new lease.
    Repeating it after a finish is a successful no-op.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    occurred_at = _now_utc(now_utc)
    occurred_value = datetime.fromisoformat(
        occurred_at.replace("Z", "+00:00")
    )
    connection = _connect(state_file)
    recorded = False
    finish_sequence: int | None = None
    reconciliation_token: str | None = None
    lock_now = occurred_value.timestamp()
    try:
        connection.execute("BEGIN IMMEDIATE")
        lifecycle_rows = _invocation_rows(connection, invocation_id)
        if not lifecycle_rows or lifecycle_rows[0]["event_type"] != "start":
            raise LifecycleConflict("invocation has not been started")
        prior_finish = next(
            (
                row for row in lifecycle_rows
                if row["event_type"] == "finish"
            ),
            None,
        )
        if prior_finish is not None:
            finish_sequence = int(prior_finish["sequence"])
            classification = prior_finish["classification"]
            reason_code = prior_finish["reason_code"]
            connection.commit()
        else:
            activity_times = [
                row["occurred_at_utc"] for row in lifecycle_rows
            ] + [
                row["occurred_at_utc"]
                for row in _checkpoint_rows(connection, invocation_id)
            ]
            latest_activity = max(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in activity_times
            )
            idle_seconds_value = (
                occurred_value - latest_activity
            ).total_seconds()
            if idle_seconds_value < 0:
                raise LifecycleError(
                    "reconciliation time precedes invocation activity"
                )
            if idle_seconds_value < ABANDONED_INVOCATION_MIN_IDLE_SECONDS:
                raise LifecycleConflict(
                    "invocation has not exceeded the abandoned-idle threshold"
                )
            try:
                acquisition = run_lock_module.acquire(
                    lock_file=lock_file,
                    lease_seconds=ABANDONED_RECONCILIATION_LEASE_SECONDS,
                    now=lock_now,
                )
            except Exception as exc:
                raise LifecycleError(
                    "cannot reserve abandoned-invocation reconciliation"
                ) from exc
            if acquisition.get("ok") is not True:
                if acquisition.get("reason") != "active_run":
                    raise LifecycleError(
                        "abandoned-invocation lease receipt is invalid"
                    )
                raise LifecycleConflict(
                    "cannot reconcile an invocation while a run lease is live"
                )
            reconciliation_token = _canonical_uuid(
                acquisition.get("token"), "reconciliation lease token"
            )
            _record_abandoned_second_terminal_if_exact(
                connection, invocation_id, occurred_at
            )
            _guard_critical_transition(
                action="finish",
                phase="finished",
                classification="coordination-halt",
                reason_code="coordination-state",
                checkpoint_rows=_checkpoint_rows(connection, invocation_id),
            )
            cursor = connection.execute(
                """
                INSERT INTO lifecycle_events (
                    invocation_id, event_type, classification,
                    occurred_at_utc, run_start_pt, phase, reason_code,
                    report_file, status_file
                ) VALUES (?, 'finish', 'coordination-halt', ?, NULL,
                          'finished', 'coordination-state', NULL, NULL)
                """,
                (invocation_id, occurred_at),
            )
            finish_sequence = int(cursor.lastrowid)
            classification = "coordination-halt"
            reason_code = "coordination-state"
            recorded = True
            connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        if reconciliation_token is not None:
            try:
                release_result = run_lock_module.release(
                    reconciliation_token, lock_file=lock_file, now=lock_now
                )
            except Exception as release_exc:
                raise LifecycleError(
                    "abandoned-invocation reconciliation failed and its "
                    "fencing-lease release is unconfirmed"
                ) from release_exc
            if release_result.get("ok") is not True:
                raise LifecycleError(
                    "abandoned-invocation reconciliation failed and its "
                    "fencing-lease release is unconfirmed"
                )
        raise
    finally:
        connection.close()

    if reconciliation_token is not None:
        try:
            release_result = run_lock_module.release(
                reconciliation_token, lock_file=lock_file, now=lock_now
            )
        except Exception as exc:
            raise LifecycleError(
                "abandoned invocation was recorded but its fencing-lease "
                "release is unconfirmed"
            ) from exc
        if release_result.get("ok") is not True:
            raise LifecycleError(
                "abandoned invocation was recorded but its fencing-lease "
                "release is unconfirmed"
            )
    if recorded:
        _publish_after_append(
            "reconcile-abandoned", invocation_id, state_file, projection_file
        )
    else:
        publish_projection(state_file, projection_file)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "reconcile-abandoned",
        "ok": True,
        "invocation_id": invocation_id,
        "reconciled": recorded,
        "reason": (
            "abandoned-invocation" if recorded else "already-finished"
        ),
        "minimum_idle_seconds": ABANDONED_INVOCATION_MIN_IDLE_SECONDS,
        "sequence": finish_sequence,
        "classification": classification,
        "reason_code": reason_code,
        "finished_at_utc": (
            occurred_at if recorded else prior_finish["occurred_at_utc"]
        ),
    }


def reconcile_abandoned_invocations(
    *,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Reconcile every sufficiently idle unfinished invocation as one batch."""
    lock_file = _resolved_lock_file(state_file, lock_file)
    occurred_at = _now_utc(now_utc)
    occurred_value = datetime.fromisoformat(
        occurred_at.replace("Z", "+00:00")
    )
    lock_now = occurred_value.timestamp()
    connection = _connect(state_file)
    reconciliation_token: str | None = None
    reconciled_ids: list[str] = []
    blocked_by_live_lease = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        invocation_rows = connection.execute(
            """
            SELECT DISTINCT started.invocation_id
            FROM lifecycle_events AS started
            WHERE started.event_type = 'start'
              AND NOT EXISTS (
                  SELECT 1 FROM lifecycle_events AS finished
                  WHERE finished.invocation_id = started.invocation_id
                    AND finished.event_type = 'finish'
              )
            ORDER BY started.sequence ASC
            """
        ).fetchall()
        for invocation_row in invocation_rows:
            candidate_id = invocation_row["invocation_id"]
            rows = _invocation_rows(connection, candidate_id)
            activity = [
                row["occurred_at_utc"] for row in rows
            ] + [
                row["occurred_at_utc"]
                for row in _checkpoint_rows(connection, candidate_id)
            ]
            latest = max(
                datetime.fromisoformat(value.replace("Z", "+00:00"))
                for value in activity
            )
            idle_seconds = (occurred_value - latest).total_seconds()
            if idle_seconds < 0:
                raise LifecycleError(
                    "reconciliation time precedes invocation activity"
                )
            if idle_seconds >= ABANDONED_INVOCATION_MIN_IDLE_SECONDS:
                reconciled_ids.append(candidate_id)
        if not reconciled_ids:
            connection.commit()
        else:
            try:
                acquisition = run_lock_module.acquire(
                    lock_file=lock_file,
                    lease_seconds=ABANDONED_RECONCILIATION_LEASE_SECONDS,
                    now=lock_now,
                )
            except Exception as exc:
                raise LifecycleError(
                    "cannot reserve abandoned-invocation reconciliation"
                ) from exc
            if acquisition.get("ok") is not True:
                if acquisition.get("reason") != "active_run":
                    raise LifecycleError(
                        "abandoned-invocation lease receipt is invalid"
                    )
                reconciled_ids.clear()
                blocked_by_live_lease = True
                connection.commit()
            else:
                reconciliation_token = _canonical_uuid(
                    acquisition.get("token"), "reconciliation lease token"
                )
                for candidate_id in reconciled_ids:
                    _record_abandoned_second_terminal_if_exact(
                        connection, candidate_id, occurred_at
                    )
                    _guard_critical_transition(
                        action="finish",
                        phase="finished",
                        classification="coordination-halt",
                        reason_code="coordination-state",
                        checkpoint_rows=_checkpoint_rows(
                            connection, candidate_id
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO lifecycle_events (
                            invocation_id, event_type, classification,
                            occurred_at_utc, run_start_pt, phase, reason_code,
                            report_file, status_file
                        ) VALUES (?, 'finish', 'coordination-halt', ?, NULL,
                                  'finished', 'coordination-state', NULL, NULL)
                        """,
                        (candidate_id, occurred_at),
                    )
                connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        if reconciliation_token is not None:
            try:
                release = run_lock_module.release(
                    reconciliation_token, lock_file=lock_file, now=lock_now
                )
            except Exception as release_exc:
                raise LifecycleError(
                    "abandoned batch failed and fencing release is unconfirmed"
                ) from release_exc
            if release.get("ok") is not True:
                raise LifecycleError(
                    "abandoned batch failed and fencing release is unconfirmed"
                )
        raise
    finally:
        connection.close()
    if reconciliation_token is not None:
        try:
            release = run_lock_module.release(
                reconciliation_token, lock_file=lock_file, now=lock_now
            )
        except Exception as exc:
            raise LifecycleError(
                "abandoned batch was recorded but fencing release is unconfirmed"
            ) from exc
        if release.get("ok") is not True:
            raise LifecycleError(
                "abandoned batch was recorded but fencing release is unconfirmed"
            )
    if reconciled_ids:
        try:
            publish_projection(state_file, projection_file)
        except Exception as exc:
            raise ProjectionPublishError(
                f"abandoned batch committed but projection publication failed: {exc}",
                "reconcile-abandoned",
                reconciled_ids[-1],
            ) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "reconcile-abandoned",
        "ok": True,
        "reconciled_count": len(reconciled_ids),
        "blocked_by_live_lease": blocked_by_live_lease,
        "minimum_idle_seconds": ABANDONED_INVOCATION_MIN_IDLE_SECONDS,
    }


def _append_event(
    *,
    action: str,
    state_file: str,
    invocation_id: str,
    occurred_at_utc: str,
    classification: str,
    phase: str,
    reason_code: str | None = None,
    run_start_pt: str | None = None,
    report_file: str | None = None,
    status_file: str | None = None,
) -> int:
    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = _invocation_rows(connection, invocation_id)
        if action == "start":
            if rows:
                raise LifecycleConflict("invocation already exists")
            if run_start_pt is not None:
                raise LifecycleError("start cannot bind Pacific run time")
        else:
            _require_open_invocation(rows)
            _guard_critical_transition(
                action=action,
                phase=phase,
                classification=classification,
                reason_code=reason_code,
                checkpoint_rows=_checkpoint_rows(connection, invocation_id),
            )
            if run_start_pt is not None:
                if action != "event" or phase != "preflight":
                    raise LifecycleError(
                        "Pacific run time can be bound only by a preflight event"
                    )
                if any(row["run_start_pt"] is not None for row in rows):
                    raise LifecycleConflict("Pacific run time is already bound")
                start_value = datetime.fromisoformat(
                    rows[0]["occurred_at_utc"].replace("Z", "+00:00")
                )
                clock_value = datetime.fromisoformat(run_start_pt).astimezone(
                    timezone.utc
                )
                event_value = datetime.fromisoformat(
                    occurred_at_utc.replace("Z", "+00:00")
                )
                clock_delay = (clock_value - start_value).total_seconds()
                if not (
                    -CLOCK_BIND_SKEW_SECONDS
                    <= clock_delay
                    <= MAX_CLOCK_BIND_DELAY_SECONDS
                ) or clock_value > event_value + timedelta(
                    seconds=CLOCK_BIND_SKEW_SECONDS
                ):
                    raise LifecycleError(
                        "Pacific run time is not contemporaneous with this invocation"
                    )
            occurred_at_value = datetime.fromisoformat(
                occurred_at_utc.replace("Z", "+00:00")
            )
            previous_value = datetime.fromisoformat(
                rows[-1]["occurred_at_utc"].replace("Z", "+00:00")
            )
            if occurred_at_value < previous_value:
                raise LifecycleError("event time cannot precede the previous event")
        cursor = connection.execute(
            """
            INSERT INTO lifecycle_events (
                invocation_id, event_type, classification, occurred_at_utc,
                run_start_pt, phase, reason_code, report_file, status_file
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invocation_id,
                action,
                classification,
                occurred_at_utc,
                run_start_pt,
                phase,
                reason_code,
                report_file,
                status_file,
            ),
        )
        sequence = int(cursor.lastrowid)
        connection.commit()
        return sequence
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _insert_finish_row(
    connection: sqlite3.Connection,
    *,
    invocation_id: str,
    classification: str,
    occurred_at: str,
    phase: str,
    reason_code: str | None,
    report_file: str | None,
    status_file: str | None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO lifecycle_events (
            invocation_id, event_type, classification, occurred_at_utc,
            run_start_pt, phase, reason_code, report_file, status_file
        ) VALUES (?, 'finish', ?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            invocation_id, classification, occurred_at, phase,
            reason_code, report_file, status_file,
        ),
    )
    return int(cursor.lastrowid)


def _publish_after_append(
    action: str,
    invocation_id: str,
    state_file: str,
    projection_file: str,
) -> dict[str, Any]:
    try:
        return publish_projection(state_file, projection_file)
    except Exception as exc:
        raise ProjectionPublishError(
            f"event committed but projection publication failed: {exc}",
            action,
            invocation_id,
        ) from exc


def start_invocation(
    *,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    lock_file: str | None = None,
    invocation_id: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    lock_file = _resolved_lock_file(state_file, lock_file)
    invocation_id = (
        str(uuid.uuid4())
        if invocation_id is None
        else _canonical_uuid(invocation_id, "invocation_id")
    )
    occurred_at = _now_utc(now_utc)
    reconciliation = reconcile_abandoned_invocations(
        state_file=state_file,
        projection_file=projection_file,
        lock_file=lock_file,
        now_utc=occurred_at,
    )
    sequence = _append_event(
        action="start",
        state_file=state_file,
        invocation_id=invocation_id,
        occurred_at_utc=occurred_at,
        classification="running",
        phase="scheduled",
        run_start_pt=None,
    )
    document = _publish_after_append(
        "start", invocation_id, state_file, projection_file
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "start",
        "ok": True,
        "invocation_id": invocation_id,
        "sequence": sequence,
        "classification": "running",
        "phase": "scheduled",
        "run_start_pt": None,
        "started_at_utc": occurred_at,
        "reconciled_abandoned_count": reconciliation["reconciled_count"],
        "reconciliation_blocked_by_live_lease": reconciliation[
            "blocked_by_live_lease"
        ],
        "projection_record_count": document["record_count"],
    }


def record_event(
    *,
    invocation_id: str,
    phase: str,
    run_start_pt: str | None = None,
    classification: str = "running",
    reason_code: str | None = None,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    now_utc: str | None = None,
) -> dict[str, Any]:
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    phase = _enum(phase, PHASES, "phase")
    if run_start_pt is not None:
        run_start_pt = _canonical_run_start_pt(run_start_pt, "run_start_pt")
        if phase != "preflight":
            raise LifecycleError(
                "--run-start-pt can be bound only by a preflight event"
            )
    classification = _enum(classification, CLASSIFICATIONS, "classification")
    if classification == "completed":
        raise LifecycleError("completed is a finish classification, not an event")
    if reason_code is not None:
        reason_code = _enum(reason_code, REASON_CODES, "reason_code")
    if classification != "running" and reason_code is None:
        raise LifecycleError("non-running event requires a reason code")
    if classification != "running":
        _validate_terminal_reason(classification, reason_code, "event")
    occurred_at = _now_utc(now_utc)
    sequence = _append_event(
        action="event",
        state_file=state_file,
        invocation_id=invocation_id,
        occurred_at_utc=occurred_at,
        classification=classification,
        phase=phase,
        reason_code=reason_code,
        run_start_pt=run_start_pt,
    )
    _publish_after_append("event", invocation_id, state_file, projection_file)
    artifact_stamp = None if run_start_pt is None else _run_stamp(run_start_pt)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "event",
        "ok": True,
        "invocation_id": invocation_id,
        "sequence": sequence,
        "classification": classification,
        "phase": phase,
        "reason_code": reason_code,
        "run_start_pt": run_start_pt,
        "artifact_stamp": artifact_stamp,
        "expected_report_file": (
            None if artifact_stamp is None else f"rhmra-log-{artifact_stamp}.md"
        ),
        "expected_gate_file": (
            None if artifact_stamp is None else f"rhmra-gates-{artifact_stamp}.json"
        ),
        "expected_status_file": (
            None
            if artifact_stamp is None
            else f"rhmra-status-{artifact_stamp}.json"
        ),
        "occurred_at_utc": occurred_at,
    }


def _pending_fail_closed_second_checkpoint(
    *,
    connection: sqlite3.Connection,
    invocation_id: str,
    classification: str,
    reason_code: str | None,
) -> str | None:
    """Derive only a non-authorizing SECOND terminal for final recovery."""
    rows = _invocation_rows(connection, invocation_id)
    _require_open_invocation(rows)
    checkpoint_rows = _checkpoint_rows(connection, invocation_id)
    checkpoints = _checkpoint_names(checkpoint_rows)
    terminal = checkpoints & _SECOND_TERMINAL_CHECKPOINTS
    if terminal or "entry-eligible" not in checkpoints:
        return None
    if (
        classification == "coordination-halt"
        and reason_code == "coordination-state"
    ):
        checkpoint = "daily-loss-coordination-terminal"
    else:
        return None
    if rows[-1]["phase"] != "daily-loss":
        raise LifecycleConflict(
            "fail-closed SECOND recovery requires the current daily-loss phase"
        )
    if checkpoints != {"entry-eligible", "daily-loss-attempted"}:
        raise LifecycleError(
            "DAILY-LOSS attempt boundary contains unsupported checkpoints"
        )
    if _second_context_row(connection, invocation_id) is None:
        raise LifecycleError("DAILY-LOSS scratch binding is missing")
    if _second_evidence_row(connection, invocation_id) is not None:
        raise LifecycleError(
            "DAILY-LOSS evidence exists without its deterministic checkpoint"
        )
    return checkpoint


def _validate_finish_request(
    *,
    invocation_id: str,
    classification: str,
    reason_code: str | None = None,
    phase: str = "finished",
    report_file: str | None = None,
    status_file: str | None = None,
    state_file: str = DEFAULT_STATE_FILE,
    report_dir: str = DEFAULT_REPORT_DIR,
    pending_second_checkpoint: str | None = None,
) -> tuple[str, str, str | None, str, str | None, str | None]:
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    classification = _enum(
        classification, TERMINAL_CLASSIFICATIONS, "classification"
    )
    phase = _enum(phase, PHASES, "phase")
    if reason_code is not None:
        reason_code = _enum(reason_code, REASON_CODES, "reason_code")
    _validate_terminal_reason(classification, reason_code, "finish")
    connection = _connect(state_file)
    try:
        rows = _invocation_rows(connection, invocation_id)
        _require_open_invocation(rows)
        _guard_critical_transition(
            action="finish",
            phase=phase,
            classification=classification,
            reason_code=reason_code,
            checkpoint_rows=_checkpoint_rows(connection, invocation_id),
            pending_checkpoint=pending_second_checkpoint,
        )
    finally:
        connection.close()
    run_start_pt = next(
        (row["run_start_pt"] for row in rows if row["run_start_pt"] is not None),
        None,
    )
    if run_start_pt is None:
        if report_file is not None or status_file is not None:
            raise LifecycleError(
                "report/status files require a Pacific time binding"
            )
    else:
        stamp = _run_stamp(run_start_pt)
        report_file = _safe_filename(
            report_file, _REPORT_RE, stamp, "report_file"
        )
        status_file = _safe_filename(
            status_file, _STATUS_RE, stamp, "status_file"
        )
    if classification in (
        "overlap",
        "lease-lost",
        "final-status-unavailable",
    ) and status_file is not None:
        raise LifecycleError(
            f"{classification} must not reference an account status snapshot"
        )
    if status_file is not None:
        # Local import avoids a module-level cycle: status_snapshot uses this
        # module's read-only invocation binding during publication.
        import status_snapshot as status_snapshot_module

        status_path = os.path.join(os.path.abspath(report_dir), status_file)
        try:
            status_document = (
                status_snapshot_module.load_published_status_snapshot(
                    status_path, os.path.abspath(report_dir)
                )
            )
        except (
            status_snapshot_module.StatusSnapshotError,
            OSError,
        ) as exc:
            raise LifecycleError(
                f"status_file: published snapshot validation failed: {exc}"
            ) from exc
        if status_document["run_start_pt"] != run_start_pt:
            raise LifecycleError(
                "status_file: published snapshot run_start_pt must exactly match "
                f"the lifecycle binding ({run_start_pt})"
            )
    return (
        invocation_id, classification, reason_code, phase,
        report_file, status_file,
    )


def _guard_raw_finish_ownership(
    *,
    invocation_id: str,
    classification: str,
    reason_code: str | None,
    state_file: str,
    projection_file: str,
    lock_file: str,
    now_utc: str | None,
) -> None:
    """Reserve raw finish for pre-lease or proven lease-loss paths.

    Once a validated active-context receipt belongs to an invocation, a
    still-owned lease must be released and finalized by ``release-finish``.
    If the lease is missing, expired, or now owned by another token, only the
    explicit ``lease-lost`` terminal classification may use raw ``finish``.
    """
    connection = _connect(state_file)
    try:
        binding = _lease_binding_row(connection, invocation_id)
        compensation = _lease_compensation_row(connection, invocation_id)
    finally:
        connection.close()
    if binding is None:
        return
    lifecycle = invocation_status(
        invocation_id=invocation_id,
        state_file=state_file,
        projection_file=projection_file,
    )
    for name in (
        "run_start_pt", "artifact_stamp", "expected_report_file",
        "expected_gate_file", "expected_status_file",
    ):
        if binding[name] != lifecycle[name]:
            raise LifecycleConflict(
                f"active context {name} no longer matches lifecycle"
            )
    if compensation is not None:
        if (
            compensation["lease_token_sha256"]
            != binding["lease_token_sha256"]
            or compensation["disposition"] != "bind-failure-released"
        ):
            raise LifecycleError(
                "durable bind compensation disagrees with lease binding"
            )
        try:
            _active_lease_token(lock_file, now_utc)
        except LifecycleConflict:
            if (
                classification == "coordination-halt"
                and reason_code == "coordination-state"
            ):
                return
        else:
            raise LifecycleConflict(
                "compensated bind failure cannot be raw-finished while any "
                "lease is live"
            )
    try:
        owner = _active_lease_token(lock_file, now_utc)
    except LifecycleConflict:
        if classification == "lease-lost":
            return
        raise LifecycleConflict(
            "active-context invocation without a live owned lease can be "
            "raw-finished only as lease-lost"
        )
    if binding["lease_token_sha256"] == _token_digest(owner):
        raise LifecycleConflict(
            "active-context invocation with a live owned lease must use "
            "release-finish"
        )
    if classification != "lease-lost":
        raise LifecycleConflict(
            "active-context invocation whose lease owner changed can be "
            "raw-finished only as lease-lost"
        )


def finish_invocation(
    *,
    invocation_id: str,
    classification: str,
    reason_code: str | None = None,
    phase: str = "finished",
    report_file: str | None = None,
    status_file: str | None = None,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    report_dir: str = DEFAULT_REPORT_DIR,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    (
        invocation_id, classification, reason_code, phase,
        report_file, status_file,
    ) = _validate_finish_request(
        invocation_id=invocation_id,
        classification=classification,
        reason_code=reason_code,
        phase=phase,
        report_file=report_file,
        status_file=status_file,
        state_file=state_file,
        report_dir=report_dir,
    )
    _guard_raw_finish_ownership(
        invocation_id=invocation_id,
        classification=classification,
        reason_code=reason_code,
        state_file=state_file,
        projection_file=projection_file,
        lock_file=_resolved_lock_file(state_file, lock_file),
        now_utc=now_utc,
    )
    occurred_at = _now_utc(now_utc)
    sequence = _append_event(
        action="finish",
        state_file=state_file,
        invocation_id=invocation_id,
        occurred_at_utc=occurred_at,
        classification=classification,
        phase=phase,
        reason_code=reason_code,
        report_file=report_file,
        status_file=status_file,
    )
    _publish_after_append("finish", invocation_id, state_file, projection_file)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "finish",
        "ok": True,
        "invocation_id": invocation_id,
        "sequence": sequence,
        "classification": classification,
        "phase": phase,
        "reason_code": reason_code,
        "finished_at_utc": occurred_at,
        "report_file": report_file,
        "status_file": status_file,
    }


def release_and_finish_invocation(
    *,
    invocation_id: str,
    run_token: str,
    classification: str,
    reason_code: str | None = None,
    phase: str = "finished",
    report_file: str | None = None,
    status_file: str | None = None,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    report_dir: str = DEFAULT_REPORT_DIR,
    context_file: str = DEFAULT_CONTEXT_FILE,
    lock_file: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    """Owner-fenced release followed by lifecycle finish in one process.

    The finish request and active owner/context are fully validated before the
    release.  The one exact attempted-SECOND coordination fallback is staged
    with finish in the lifecycle transaction and remains non-authorizing.
    SQLite cannot atomically commit across the independent lease and lifecycle
    databases: interruption after release can therefore leave an unleased
    unfinished invocation, never a false success.  The conservative
    abandoned-invocation reconciler is the bounded recovery for that gap.
    """
    lock_file = _resolved_lock_file(state_file, lock_file)
    lifecycle, run_token, lock_now = _owned_context_lifecycle(
        invocation_id=invocation_id,
        run_token=run_token,
        state_file=state_file,
        projection_file=projection_file,
        context_file=context_file,
        lock_file=lock_file,
        now_utc=now_utc,
    )
    preflight_connection = _connect(state_file)
    try:
        pending_second_checkpoint = _pending_fail_closed_second_checkpoint(
            connection=preflight_connection,
            invocation_id=lifecycle["invocation_id"],
            classification=classification,
            reason_code=reason_code,
        )
    finally:
        preflight_connection.close()
    validated = _validate_finish_request(
        invocation_id=lifecycle["invocation_id"],
        classification=classification,
        reason_code=reason_code,
        phase=phase,
        report_file=report_file,
        status_file=status_file,
        state_file=state_file,
        report_dir=report_dir,
        pending_second_checkpoint=pending_second_checkpoint,
    )
    occurred_at = _now_utc(now_utc)
    occurred_value = datetime.fromisoformat(
        occurred_at.replace("Z", "+00:00")
    )
    connection = _connect(state_file)
    lease_released = False
    second_terminal_recorded = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = _invocation_rows(connection, validated[0])
        _require_open_invocation(rows)
        transactional_pending = _pending_fail_closed_second_checkpoint(
            connection=connection,
            invocation_id=validated[0],
            classification=validated[1],
            reason_code=validated[2],
        )
        if transactional_pending != pending_second_checkpoint:
            raise LifecycleConflict(
                "SECOND terminal state changed during release-finish"
            )
        checkpoint_rows = _checkpoint_rows(connection, validated[0])
        latest_activity = max(
            datetime.fromisoformat(
                row["occurred_at_utc"].replace("Z", "+00:00")
            )
            for row in [*rows, *checkpoint_rows]
        )
        if occurred_value < latest_activity:
            raise LifecycleError(
                "release-finish time cannot precede lifecycle activity"
            )
        if pending_second_checkpoint is not None:
            connection.execute(
                "INSERT INTO lifecycle_checkpoints "
                "(invocation_id, checkpoint, occurred_at_utc) "
                "VALUES (?, ?, ?)",
                (validated[0], pending_second_checkpoint, occurred_at),
            )
            second_terminal_recorded = True
            checkpoint_rows = _checkpoint_rows(connection, validated[0])
        _guard_critical_transition(
            action="finish",
            phase=validated[3],
            classification=validated[1],
            reason_code=validated[2],
            checkpoint_rows=checkpoint_rows,
        )
        try:
            release = run_lock_module.release(
                run_token, lock_file=lock_file, now=lock_now
            )
        except Exception as exc:
            raise LifecycleError("owner-fenced lease release failed") from exc
        if (
            not isinstance(release, dict)
            or release.get("schema_version") != run_lock_module.SCHEMA_VERSION
            or release.get("action") != "release"
            or release.get("ok") is not True
            or release.get("token") != run_token
        ):
            raise LifecycleConflict("owner-fenced lease release was rejected")
        lease_released = True
        sequence = _insert_finish_row(
            connection,
            invocation_id=validated[0],
            classification=validated[1],
            occurred_at=occurred_at,
            phase=validated[3],
            reason_code=validated[2],
            report_file=validated[4],
            status_file=validated[5],
        )
        connection.commit()
    except Exception as exc:
        if connection.in_transaction:
            connection.rollback()
        if lease_released:
            raise ReleaseFinishError(
                f"lease released but lifecycle finish was not recorded: {exc}",
                lifecycle["invocation_id"],
                recorded=False,
                reason="post_release_finish_failed",
            ) from exc
        raise
    finally:
        connection.close()
    try:
        _publish_after_append(
            "release-finish", lifecycle["invocation_id"],
            state_file, projection_file,
        )
    except Exception as exc:
        raise ReleaseFinishError(
            f"lease released and lifecycle finish recorded, but projection "
            f"publication failed: {exc}",
            lifecycle["invocation_id"],
            recorded=True,
            reason="projection_publication_failed",
        ) from exc
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "release-finish",
        "ok": True,
        "invocation_id": validated[0],
        "sequence": sequence,
        "classification": validated[1],
        "phase": validated[3],
        "reason_code": validated[2],
        "finished_at_utc": occurred_at,
        "report_file": validated[4],
        "status_file": validated[5],
        "lease_released": True,
        "recorded": True,
        "second_terminal_checkpoint": pending_second_checkpoint,
        "second_terminal_recorded": second_terminal_recorded,
    }


def _reject_unused(args: argparse.Namespace, allowed: set[str]) -> None:
    optional = {
        "invocation_id",
        "run_token",
        "run_start_pt",
        "classification",
        "outcome",
        "scratch",
        "expected_constants_sha256",
        "phase",
        "reason_code",
        "report_file",
        "status_file",
        "report_dir",
    }
    for name in sorted(optional - allowed):
        if getattr(args, name) is not None:
            raise LifecycleError(
                f"--{name.replace('_', '-')} is not valid for {args.action}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "action",
        choices=(
            "start", "event", "finish", "release-finish", "status",
            "enter-second", "complete-second", "reconcile-abandoned",
            "acquire-bind-context", "bind-context", "recover-context",
            "export", "validate",
        ),
    )
    parser.add_argument("--invocation-id")
    parser.add_argument("--run-token")
    parser.add_argument("--run-start-pt")
    parser.add_argument("--classification", choices=CLASSIFICATIONS)
    parser.add_argument("--outcome", choices=PUBLIC_SECOND_OUTCOMES)
    parser.add_argument("--scratch")
    parser.add_argument("--expected-constants-sha256")
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--reason-code", choices=REASON_CODES)
    parser.add_argument("--report-file")
    parser.add_argument("--status-file")
    parser.add_argument("--report-dir", help=argparse.SUPPRESS)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--projection-file", default=DEFAULT_PROJECTION_FILE)
    parser.add_argument("--context-file", default=DEFAULT_CONTEXT_FILE,
                        help=argparse.SUPPRESS)
    parser.add_argument("--lock-file",
                        help=argparse.SUPPRESS)
    parser.add_argument("--now-utc", help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        if args.now_utc is not None:
            raise LifecycleError(
                "--now-utc is test-only through the imported API and is not "
                "valid for any CLI action"
            )
        if args.action == "start":
            _reject_unused(args, {"invocation_id"})
            result = start_invocation(
                invocation_id=args.invocation_id,
                state_file=args.state_file,
                projection_file=args.projection_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "event":
            _reject_unused(
                args,
                {
                    "invocation_id",
                    "run_start_pt",
                    "classification",
                    "phase",
                    "reason_code",
                },
            )
            if args.invocation_id is None or args.phase is None:
                raise LifecycleError("event requires --invocation-id and --phase")
            result = record_event(
                invocation_id=args.invocation_id,
                phase=args.phase,
                run_start_pt=args.run_start_pt,
                classification=args.classification or "running",
                reason_code=args.reason_code,
                state_file=args.state_file,
                projection_file=args.projection_file,
                now_utc=args.now_utc,
            )
        elif args.action == "finish":
            _reject_unused(
                args,
                {
                    "invocation_id",
                    "classification",
                    "phase",
                    "reason_code",
                    "report_file",
                    "status_file",
                    "report_dir",
                },
            )
            if args.invocation_id is None or args.classification is None:
                raise LifecycleError(
                    "finish requires --invocation-id and --classification"
                )
            result = finish_invocation(
                invocation_id=args.invocation_id,
                classification=args.classification,
                phase=args.phase or "finished",
                reason_code=args.reason_code,
                report_file=args.report_file,
                status_file=args.status_file,
                state_file=args.state_file,
                projection_file=args.projection_file,
                report_dir=args.report_dir or DEFAULT_REPORT_DIR,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "release-finish":
            _reject_unused(
                args,
                {
                    "invocation_id", "run_token", "classification", "phase",
                    "reason_code", "report_file", "status_file", "report_dir",
                },
            )
            if (
                args.invocation_id is None
                or args.run_token is None
                or args.classification is None
            ):
                raise LifecycleError(
                    "release-finish requires --invocation-id, --run-token, "
                    "and --classification"
                )
            result = release_and_finish_invocation(
                invocation_id=args.invocation_id,
                run_token=args.run_token,
                classification=args.classification,
                phase=args.phase or "finished",
                reason_code=args.reason_code,
                report_file=args.report_file,
                status_file=args.status_file,
                state_file=args.state_file,
                projection_file=args.projection_file,
                report_dir=args.report_dir or DEFAULT_REPORT_DIR,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "status":
            _reject_unused(args, {"invocation_id"})
            if args.invocation_id is None:
                raise LifecycleError("status requires --invocation-id")
            result = invocation_status(
                invocation_id=args.invocation_id,
                state_file=args.state_file,
                projection_file=args.projection_file,
            )
        elif args.action == "enter-second":
            _reject_unused(
                args,
                {
                    "invocation_id", "run_token", "scratch",
                    "expected_constants_sha256",
                },
            )
            if (
                args.invocation_id is None
                or args.run_token is None
                or args.scratch is None
                or args.expected_constants_sha256 is None
            ):
                raise LifecycleError(
                    "enter-second requires --invocation-id, --run-token, "
                    "--scratch, and --expected-constants-sha256"
                )
            result = enter_second(
                invocation_id=args.invocation_id,
                run_token=args.run_token,
                scratch=args.scratch,
                expected_constants_sha256=args.expected_constants_sha256,
                state_file=args.state_file,
                projection_file=args.projection_file,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "complete-second":
            _reject_unused(args, {"invocation_id", "run_token", "outcome"})
            if (
                args.invocation_id is None
                or args.run_token is None
                or args.outcome is None
            ):
                raise LifecycleError(
                    "complete-second requires --invocation-id, --run-token, "
                    "and --outcome"
                )
            result = complete_second(
                invocation_id=args.invocation_id,
                run_token=args.run_token,
                outcome=args.outcome,
                state_file=args.state_file,
                projection_file=args.projection_file,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "reconcile-abandoned":
            _reject_unused(args, {"invocation_id"})
            if args.invocation_id is None:
                raise LifecycleError(
                    "reconcile-abandoned requires --invocation-id"
                )
            result = reconcile_abandoned_invocation(
                invocation_id=args.invocation_id,
                state_file=args.state_file,
                projection_file=args.projection_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "acquire-bind-context":
            _reject_unused(args, {"invocation_id"})
            if args.invocation_id is None:
                raise LifecycleError(
                    "acquire-bind-context requires --invocation-id"
                )
            result = acquire_and_bind_active_context(
                invocation_id=args.invocation_id,
                state_file=args.state_file,
                projection_file=args.projection_file,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "bind-context":
            _reject_unused(args, {"invocation_id", "run_token"})
            if args.invocation_id is None or args.run_token is None:
                raise LifecycleError(
                    "bind-context requires --invocation-id and --run-token"
                )
            result = bind_active_context(
                invocation_id=args.invocation_id,
                run_token=args.run_token,
                state_file=args.state_file,
                projection_file=args.projection_file,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "recover-context":
            _reject_unused(args, set())
            result = recover_active_context(
                state_file=args.state_file,
                projection_file=args.projection_file,
                context_file=args.context_file,
                lock_file=args.lock_file,
                now_utc=args.now_utc,
            )
        elif args.action == "export":
            _reject_unused(args, set())
            document = publish_projection(args.state_file, args.projection_file)
            result = {
                "schema_version": SCHEMA_VERSION,
                "action": "export",
                "ok": True,
                "record_limit": document["record_limit"],
                "record_count": document["record_count"],
                "source_event_high_watermark": document[
                    "source_event_high_watermark"
                ],
            }
        else:
            _reject_unused(args, set())
            document = validate_current_projection(
                args.state_file, args.projection_file
            )
            result = {
                "schema_version": SCHEMA_VERSION,
                "action": "validate",
                "ok": True,
                "record_limit": document["record_limit"],
                "record_count": document["record_count"],
                "source_event_high_watermark": document[
                    "source_event_high_watermark"
                ],
            }
    except ReleaseFinishError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": "release-finish",
            "ok": False,
            "invocation_id": exc.invocation_id,
            "lease_released": True,
            "recorded": exc.recorded,
            "reason": exc.reason,
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 1
    except ProjectionPublishError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": exc.action,
            "ok": False,
            "recorded": True,
            "invocation_id": exc.invocation_id,
            "reason": "projection_publication_failed",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 1
    except LifecycleConflict as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": args.action,
            "ok": False,
            "reason": "lifecycle_conflict",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 2
    except (LifecycleError, OSError, sqlite3.Error) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": args.action,
            "ok": False,
            "reason": "lifecycle_state_error",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 1

    print(json.dumps(result, allow_nan=False, sort_keys=True))
    if result.get("ok") is not True:
        return 2 if result.get("reason") == "active_run" else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
