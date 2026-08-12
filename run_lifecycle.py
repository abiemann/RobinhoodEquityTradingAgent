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
  py -3 run_lifecycle.py export
  py -3 run_lifecycle.py validate

``start`` deliberately precedes configuration and market-clock work, so its
Pacific run timestamp begins as null.  A successful clock preflight binds
``run_start_pt`` exactly once through a ``preflight`` event.  File references
require that binding; an early configuration halt remains visible without it.

Only fixed classifications, phases, and reason codes are accepted.  There is
deliberately no free-text, account, credential, lease-token, broker-token, or
API-response field.  ``invocation_id`` is a non-secret correlation UUID.
``--state-file``, ``--projection-file``, and ``--now-utc`` are for tests and
diagnostics; trading runs use the checked-in defaults.

Exit codes:
  0  action succeeded
  1  invalid input, unsafe/corrupt state, or projection publication failure
  2  lifecycle conflict (duplicate start, unknown invocation, already finished)
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from market_clock import PACIFIC_STD_OFFSET, zone_time


SCHEMA_VERSION = 1
PROJECTION_LIMIT = 512
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-lifecycle.sqlite3"
)
DEFAULT_PROJECTION_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-lifecycle.json"
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


def _invocation_rows(
    connection: sqlite3.Connection, invocation_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT * FROM lifecycle_events WHERE invocation_id = ? "
        "ORDER BY sequence ASC",
        (invocation_id,),
    ).fetchall()


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
            if not rows or rows[0]["event_type"] != "start":
                raise LifecycleConflict("invocation has not been started")
            if any(row["event_type"] == "finish" for row in rows):
                raise LifecycleConflict("invocation is already finished")
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
    invocation_id: str | None = None,
    now_utc: str | None = None,
) -> dict[str, Any]:
    invocation_id = (
        str(uuid.uuid4())
        if invocation_id is None
        else _canonical_uuid(invocation_id, "invocation_id")
    )
    occurred_at = _now_utc(now_utc)
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
        "occurred_at_utc": occurred_at,
    }


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
    now_utc: str | None = None,
) -> dict[str, Any]:
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
    finally:
        connection.close()
    if not rows or rows[0]["event_type"] != "start":
        raise LifecycleConflict("invocation has not been started")
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


def _reject_unused(args: argparse.Namespace, allowed: set[str]) -> None:
    optional = {
        "invocation_id",
        "run_start_pt",
        "classification",
        "phase",
        "reason_code",
        "report_file",
        "status_file",
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
        "action", choices=("start", "event", "finish", "export", "validate")
    )
    parser.add_argument("--invocation-id")
    parser.add_argument("--run-start-pt")
    parser.add_argument("--classification", choices=CLASSIFICATIONS)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--reason-code", choices=REASON_CODES)
    parser.add_argument("--report-file")
    parser.add_argument("--status-file")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--projection-file", default=DEFAULT_PROJECTION_FILE)
    parser.add_argument(
        "--now-utc", help="test/diagnostic override, canonical ISO-8601 UTC"
    )
    args = parser.parse_args()

    try:
        if args.action == "start":
            _reject_unused(args, {"invocation_id"})
            result = start_invocation(
                invocation_id=args.invocation_id,
                state_file=args.state_file,
                projection_file=args.projection_file,
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
