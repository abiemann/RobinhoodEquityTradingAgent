#!/usr/bin/env python3
"""Non-authoritative, append-only performance telemetry for RHMRA runs.

This sidecar measures a finished lifecycle without changing lifecycle state or
trading authority.  It must be called only as best-effort telemetry after the
lifecycle has finished; a failure here never authorizes, blocks, or changes
broker work.

The SQLite journal retains every explicit observation.  The bounded JSON
projection contains the 512 most recent internally recorded invocations at:

  run-reports/rhmra-run-performance.json

Commands (successful commands emit exactly one JSON object):

  py -3 run_performance.py record-internal --invocation-id UUID \
      --strategy-start-utc 2026-08-11T19:10:00Z \
      --strategy-end-utc 2026-08-11T19:13:00Z \
      --session after-hours --runner codex --model gpt-5.6-luna \
      --configuration reasoning=high --identity-source task-definition
  py -3 run_performance.py observe-task --invocation-id UUID \
      --task-duration-ms 363000 --runner codex --model gpt-5.6-luna \
      --configuration reasoning=high --identity-source manual-ui \
      --clock-source codex-worked-for
  py -3 run_performance.py export
  py -3 run_performance.py validate

``record-internal`` validates the lifecycle database and projection read-only,
requires a finished invocation, and records lifecycle boundaries plus the
optional both-or-neither FIRST/REPORT renewal boundaries.  When the lifecycle
has an authoritative Pacific run start, that same command's single observation
clock also records the canonical final-summary-boundary run duration used for
fair runner/model comparisons.  ``observe-task`` attaches a positive external
reference duration to an existing internal record.  The canonical automatic
duration remains selected whenever it exists; legacy records without one fall
back to the highest-priority external reference.  Different external clock
sources may coexist, while a duplicate source is rejected.

Only fixed enums and short, restricted identity labels are accepted.  There is
no notes, account, symbol, credential, token, broker response, or arbitrary
metadata field.  Missing measurements remain null and are never rendered as a
guessed zero.

Exit codes:
  0  action succeeded
  1  invalid input, unsafe/corrupt state, or projection publication failure
  2  duplicate/conflicting observation
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import run_lifecycle


RECEIPT_SCHEMA_VERSION = 1
LEGACY_PROJECTION_SCHEMA_VERSION = 1
PROJECTION_SCHEMA_VERSION = 2
# Backward-compatible public name: command receipts remain schema version 1.
SCHEMA_VERSION = RECEIPT_SCHEMA_VERSION
JOURNAL_SCHEMA_VERSION = 2
LEGACY_JOURNAL_SCHEMA_VERSION = 1
PROJECTION_LIMIT = 512
ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATE_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-performance.sqlite3"
)
DEFAULT_PROJECTION_FILE = os.path.join(
    ROOT, "run-reports", "rhmra-run-performance.json"
)
DEFAULT_LIFECYCLE_STATE_FILE = run_lifecycle.DEFAULT_STATE_FILE
DEFAULT_LIFECYCLE_PROJECTION_FILE = run_lifecycle.DEFAULT_PROJECTION_FILE

SESSIONS = (
    "calendar-unknown",
    "regular",
    "pre-market",
    "after-hours",
    "closed",
    "closed-weekend",
    "closed-holiday",
    "closed-early",
    "unknown",
)
RUNNERS = ("codex", "claude", "unknown")
IDENTITY_SOURCES = (
    "run-metadata",
    "task-definition",
    "manual-ui",
    "declared",
    "unknown",
)
CLOCK_SOURCES = (
    "runner-metadata",
    "codex-worked-for",
    "claude-run-duration",
    "manual-stopwatch",
    "manual-observation",
    "unknown",
)
CLOCK_SOURCE_PRIORITY = {value: index for index, value in enumerate(CLOCK_SOURCES)}
ESTIMATE_CLOCK_SOURCE = "final-summary-boundary"

_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
)
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,63}$")
_CONFIGURATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()+=-]{0,47}$")

_TOP_LEVEL_KEYS = {
    "schema_version",
    "record_limit",
    "record_count",
    "source_event_high_watermark",
    "records",
}
_RECORD_KEYS = {
    "invocation_id",
    "lifecycle_started_at_utc",
    "lifecycle_finished_at_utc",
    "strategy_started_at_utc",
    "strategy_finished_at_utc",
    "session",
    "runner",
    "model",
    "configuration",
    "identity_source",
    "internal_recorded_at_utc",
    "task_duration_ms",
    "task_clock_source",
    "task_observed_at_utc",
    "routine_total_ms",
    "strategy_execution_ms",
    "routine_overhead_ms",
    "outside_lifecycle_ms",
    "total_overhead_ms",
}
_EVENT_COLUMNS = {
    "sequence",
    "invocation_id",
    "event_type",
    "occurred_at_utc",
    "lifecycle_started_at_utc",
    "lifecycle_finished_at_utc",
    "strategy_started_at_utc",
    "strategy_finished_at_utc",
    "session",
    "task_duration_ms",
    "runner",
    "model",
    "configuration",
    "identity_source",
    "clock_source",
}
_ESTIMATE_COLUMNS = {
    "invocation_id",
    "internal_sequence",
    "occurred_at_utc",
    "run_start_pt",
    "run_end_pt",
    "estimated_run_total_ms",
    "clock_source",
}


class PerformanceError(ValueError):
    """Performance input, storage, or projection is unsafe to interpret."""


class PerformanceConflict(PerformanceError):
    """An append duplicates or conflicts with an existing observation."""


class ProjectionPublishError(PerformanceError):
    """An observation committed but its JSON projection was not published."""

    def __init__(self, message: str, action: str, invocation_id: str):
        super().__init__(message)
        self.action = action
        self.invocation_id = invocation_id


class StrictArgumentParser(argparse.ArgumentParser):
    """Turn argparse usage errors into the helper's strict JSON envelope."""

    def error(self, message: str) -> None:
        raise PerformanceError(message)


def _object_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PerformanceError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PerformanceError(f"non-finite JSON number is forbidden: {value}")


def _load_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
    except PerformanceError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PerformanceError(f"cannot read strict projection JSON: {exc}") from exc


def _exact_keys(
    value: Mapping[str, Any], required: Iterable[str], context: str
) -> None:
    required_set = set(required)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - required_set)
    if missing:
        raise PerformanceError(f"{context}: missing key(s): {', '.join(missing)}")
    if extra:
        raise PerformanceError(f"{context}: unknown key(s): {', '.join(extra)}")


def _canonical_uuid(value: Any, context: str) -> str:
    if not isinstance(value, str):
        raise PerformanceError(f"{context}: expected a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise PerformanceError(f"{context}: expected a canonical UUID") from exc
    canonical = str(parsed)
    if value != canonical:
        raise PerformanceError(
            f"{context}: expected a lowercase hyphenated canonical UUID"
        )
    return canonical


def _canonical_utc(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise PerformanceError(f"{context}: expected ISO-8601 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PerformanceError(f"{context}: invalid UTC timestamp") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if value != canonical:
        raise PerformanceError(f"{context}: timestamp is not canonical")
    return canonical


def _utc_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _now_utc(value: str | None) -> str:
    if value is None:
        value = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return _canonical_utc(value, "--now-utc")


def _canonical_run_start_pt(value: Any, context: str) -> str:
    try:
        return run_lifecycle._canonical_run_start_pt(value, context)
    except run_lifecycle.LifecycleError as exc:
        raise PerformanceError(str(exc)) from exc


def _utc_to_pacific(value: str) -> str:
    utc_value = _utc_datetime(_canonical_utc(value, "UTC-to-Pacific source"))
    local, _name, offset = run_lifecycle.zone_time(
        utc_value, run_lifecycle.PACIFIC_STD_OFFSET, "PST", "PDT"
    )
    rendered = local.replace(
        tzinfo=timezone(timedelta(hours=offset))
    ).isoformat()
    return _canonical_run_start_pt(rendered, "derived Pacific timestamp")


def _elapsed_pt_ms(start_pt: str, end_pt: str, context: str) -> int:
    start = datetime.fromisoformat(
        _canonical_run_start_pt(start_pt, f"{context}.start")
    )
    end = datetime.fromisoformat(
        _canonical_run_start_pt(end_pt, f"{context}.end")
    )
    delta = end - start
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    if microseconds < 0:
        raise PerformanceError(f"{context}: end precedes start")
    return microseconds // 1000


def _duration_display(milliseconds: int) -> str:
    milliseconds = _nonnegative_int(milliseconds, "duration display")
    total_seconds = (milliseconds + 500) // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    if total_minutes < 60:
        return f"{total_minutes}:{seconds:02d}"
    return f"{total_minutes // 60}:{total_minutes % 60:02d}:{seconds:02d}"


def _enum(value: Any, allowed: Sequence[str], context: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise PerformanceError(f"{context}: expected one of {', '.join(allowed)}")
    return value


def _safe_label(
    value: Any, pattern: re.Pattern[str], context: str
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise PerformanceError(f"{context}: expected a short safe identity label")
    if value != value.strip() or "  " in value:
        raise PerformanceError(f"{context}: identity label is not canonical")
    if value.isdigit():
        raise PerformanceError(f"{context}: numeric-only labels are forbidden")
    return value


def _identity(
    runner: Any,
    model: Any,
    configuration: Any,
    identity_source: Any,
    context: str = "identity",
) -> tuple[str, str, str, str]:
    runner = _enum(runner, RUNNERS, f"{context}.runner")
    model = _safe_label(model, _MODEL_RE, f"{context}.model")
    configuration = _safe_label(
        configuration, _CONFIGURATION_RE, f"{context}.configuration"
    )
    identity_source = _enum(
        identity_source, IDENTITY_SOURCES, f"{context}.identity_source"
    )
    if identity_source == "unknown" and (
        runner != "unknown" or model != "unknown" or configuration != "unknown"
    ):
        raise PerformanceError(
            f"{context}: unknown provenance requires unknown runner/model/configuration"
        )
    return runner, model, configuration, identity_source


def _nonnegative_int(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PerformanceError(f"{context}: expected a nonnegative integer")
    return value


def _positive_int(value: Any, context: str) -> int:
    if isinstance(value, str):
        if not re.fullmatch(r"[1-9][0-9]*", value):
            raise PerformanceError(f"{context}: expected a positive integer")
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PerformanceError(f"{context}: expected a positive integer")
    return value


def _elapsed_ms(start_utc: str, end_utc: str, context: str) -> int:
    delta = _utc_datetime(end_utc) - _utc_datetime(start_utc)
    microseconds = (
        (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
    )
    if microseconds < 0:
        raise PerformanceError(f"{context}: end precedes start")
    return microseconds // 1000


def _validate_strategy_pair(
    start: Any,
    end: Any,
    lifecycle_start: str,
    lifecycle_end: str,
    context: str,
) -> tuple[str | None, str | None]:
    if (start is None) != (end is None):
        raise PerformanceError(f"{context}: strategy boundaries are both-or-neither")
    if start is None:
        return None, None
    start = _canonical_utc(start, f"{context}.strategy_started_at_utc")
    end = _canonical_utc(end, f"{context}.strategy_finished_at_utc")
    if not (
        _utc_datetime(lifecycle_start)
        <= _utc_datetime(start)
        <= _utc_datetime(end)
        <= _utc_datetime(lifecycle_end)
    ):
        raise PerformanceError(
            f"{context}: strategy boundaries must be contained by lifecycle"
        )
    return start, end


def _finished_lifecycle_record(
    invocation_id: str,
    lifecycle_state_file: str,
    lifecycle_projection_file: str,
) -> dict[str, Any]:
    try:
        document = run_lifecycle.validate_current_projection_read_only(
            lifecycle_state_file, lifecycle_projection_file
        )
    except (run_lifecycle.LifecycleError, OSError, sqlite3.Error) as exc:
        raise PerformanceError(f"cannot validate lifecycle read-only: {exc}") from exc
    record = next(
        (
            candidate
            for candidate in document["records"]
            if candidate["invocation_id"] == invocation_id
        ),
        None,
    )
    if record is None:
        raise PerformanceError("invocation is not present in lifecycle projection")
    if record["finished_at_utc"] is None or record["classification"] == "running":
        raise PerformanceError("invocation lifecycle is not finished")
    lifecycle_start = _canonical_utc(
        record["started_at_utc"], "lifecycle.started_at_utc"
    )
    lifecycle_finish = _canonical_utc(
        record["finished_at_utc"], "lifecycle.finished_at_utc"
    )
    _elapsed_ms(lifecycle_start, lifecycle_finish, "lifecycle")
    run_start_pt = record["run_start_pt"]
    if run_start_pt is not None:
        run_start_pt = _canonical_run_start_pt(
            run_start_pt, "lifecycle.run_start_pt"
        )
    return {
        "started_at_utc": lifecycle_start,
        "finished_at_utc": lifecycle_finish,
        "run_start_pt": run_start_pt,
    }


def _sql_values(values: Sequence[str]) -> str:
    return ", ".join(repr(value) for value in values)


def _path_key(path: Any, context: str) -> str:
    try:
        raw = os.fspath(path)
    except TypeError as exc:
        raise PerformanceError(f"{context}: expected a filesystem path") from exc
    if not isinstance(raw, str) or not raw:
        raise PerformanceError(f"{context}: expected a nonempty filesystem path")
    return os.path.normcase(os.path.realpath(os.path.abspath(raw)))


def _reject_path_aliases(**paths: Any) -> None:
    seen: dict[str, str] = {}
    for name, path in paths.items():
        key = _path_key(path, name)
        previous = seen.get(key)
        if previous is not None:
            raise PerformanceError(
                f"{name} aliases {previous}; timing paths must be distinct"
            )
        seen[key] = name


def _create_estimate_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE performance_estimates (
            invocation_id TEXT PRIMARY KEY,
            internal_sequence INTEGER NOT NULL,
            occurred_at_utc TEXT NOT NULL,
            run_start_pt TEXT NOT NULL,
            run_end_pt TEXT NOT NULL,
            estimated_run_total_ms INTEGER NOT NULL
                CHECK (estimated_run_total_ms >= 0),
            clock_source TEXT NOT NULL
                CHECK (clock_source = {ESTIMATE_CLOCK_SOURCE!r}),
            FOREIGN KEY (internal_sequence)
                REFERENCES performance_events(sequence)
        )
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX performance_one_estimate_internal "
        "ON performance_estimates(internal_sequence)"
    )
    connection.execute(
        """
        CREATE TRIGGER performance_estimates_no_update
        BEFORE UPDATE ON performance_estimates
        BEGIN
            SELECT RAISE(ABORT, 'performance estimates are append-only');
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER performance_estimates_no_delete
        BEFORE DELETE ON performance_estimates
        BEGIN
            SELECT RAISE(ABORT, 'performance estimates are append-only');
        END
        """
    )


def _connect(state_file: str) -> sqlite3.Connection:
    path = os.path.abspath(state_file)
    directory = os.path.dirname(path)
    if not directory:
        raise PerformanceError("state file must have a parent directory")
    try:
        run_lifecycle._prepare_state_directory(directory)
    except run_lifecycle.LifecycleError as exc:
        raise PerformanceError(f"performance state filesystem preflight failed: {exc}") from exc
    connection = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
    except Exception:
        connection.close()
        raise

    sessions_sql = _sql_values(SESSIONS)
    runners_sql = _sql_values(RUNNERS)
    identities_sql = _sql_values(IDENTITY_SOURCES)
    clocks_sql = _sql_values(CLOCK_SOURCES)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS performance_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS performance_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                invocation_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('internal', 'task')),
                occurred_at_utc TEXT NOT NULL,
                lifecycle_started_at_utc TEXT,
                lifecycle_finished_at_utc TEXT,
                strategy_started_at_utc TEXT,
                strategy_finished_at_utc TEXT,
                session TEXT CHECK (session IN ({sessions_sql})),
                task_duration_ms INTEGER,
                runner TEXT NOT NULL CHECK (runner IN ({runners_sql})),
                model TEXT NOT NULL,
                configuration TEXT NOT NULL,
                identity_source TEXT NOT NULL
                    CHECK (identity_source IN ({identities_sql})),
                clock_source TEXT CHECK (clock_source IN ({clocks_sql})),
                CHECK (
                    (event_type = 'internal'
                     AND lifecycle_started_at_utc IS NOT NULL
                     AND lifecycle_finished_at_utc IS NOT NULL
                     AND ((strategy_started_at_utc IS NULL
                           AND strategy_finished_at_utc IS NULL)
                          OR
                          (strategy_started_at_utc IS NOT NULL
                           AND strategy_finished_at_utc IS NOT NULL))
                     AND session IS NOT NULL
                     AND task_duration_ms IS NULL
                     AND clock_source IS NULL)
                    OR
                    (event_type = 'task'
                     AND lifecycle_started_at_utc IS NULL
                     AND lifecycle_finished_at_utc IS NULL
                     AND strategy_started_at_utc IS NULL
                     AND strategy_finished_at_utc IS NULL
                     AND session IS NULL
                     AND task_duration_ms > 0
                     AND clock_source IS NOT NULL)
                )
            )
            """
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS performance_one_internal "
            "ON performance_events(invocation_id) WHERE event_type = 'internal'"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS performance_one_task_source "
            "ON performance_events(invocation_id, clock_source) "
            "WHERE event_type = 'task'"
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS performance_events_no_update
            BEFORE UPDATE ON performance_events
            BEGIN
                SELECT RAISE(ABORT, 'performance events are append-only');
            END
            """
        )
        connection.execute(
            """
            CREATE TRIGGER IF NOT EXISTS performance_events_no_delete
            BEFORE DELETE ON performance_events
            BEGIN
                SELECT RAISE(ABORT, 'performance events are append-only');
            END
            """
        )
        row = connection.execute(
            "SELECT value FROM performance_metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO performance_metadata(key, value) VALUES (?, ?)",
                ("schema_version", str(JOURNAL_SCHEMA_VERSION)),
            )
            _create_estimate_schema(connection)
        elif row["value"] == str(LEGACY_JOURNAL_SCHEMA_VERSION):
            _validate_schema(connection, allow_legacy=True)
            _create_estimate_schema(connection)
            connection.execute(
                "UPDATE performance_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(JOURNAL_SCHEMA_VERSION),),
            )
        elif row["value"] == str(JOURNAL_SCHEMA_VERSION):
            _validate_schema(connection, allow_legacy=False)
        else:
            raise PerformanceError("unsupported performance journal schema version")
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
        _validate_schema(connection, allow_legacy=False)
    except Exception:
        connection.close()
        raise
    return connection


def _validate_schema(
    connection: sqlite3.Connection, *, allow_legacy: bool = True
) -> int:
    info = connection.execute("PRAGMA table_info(performance_events)").fetchall()
    if {row["name"] for row in info} != _EVENT_COLUMNS:
        raise PerformanceError("performance event journal has an unsafe schema")
    primary = {row["name"] for row in info if row["pk"]}
    if primary != {"sequence"}:
        raise PerformanceError("performance event journal has an unsafe primary key")
    metadata_info = connection.execute(
        "PRAGMA table_info(performance_metadata)"
    ).fetchall()
    if {row["name"] for row in metadata_info} != {"key", "value"}:
        raise PerformanceError("performance metadata has an unsafe schema")
    metadata = connection.execute(
        "SELECT key, value FROM performance_metadata"
    ).fetchall()
    metadata_pairs = [(row["key"], row["value"]) for row in metadata]
    if len(metadata_pairs) != 1 or metadata_pairs[0][0] != "schema_version":
        raise PerformanceError("unsupported performance journal metadata")
    try:
        version = int(metadata_pairs[0][1])
    except (TypeError, ValueError) as exc:
        raise PerformanceError("unsupported performance journal metadata") from exc
    if version not in (LEGACY_JOURNAL_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION):
        raise PerformanceError("unsupported performance journal metadata")
    if version == LEGACY_JOURNAL_SCHEMA_VERSION and not allow_legacy:
        raise PerformanceError("legacy performance journal requires migration")
    triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'performance_events'"
        )
    }
    if not {
        "performance_events_no_update",
        "performance_events_no_delete",
    }.issubset(triggers):
        raise PerformanceError("performance event journal lacks append-only guards")
    indexes = {
        row["name"]: bool(row["unique"])
        for row in connection.execute("PRAGMA index_list(performance_events)")
    }
    if indexes.get("performance_one_internal") is not True or indexes.get(
        "performance_one_task_source"
    ) is not True:
        raise PerformanceError("performance event journal lacks uniqueness guards")

    estimate_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'performance_estimates'"
    ).fetchone()
    if version == LEGACY_JOURNAL_SCHEMA_VERSION:
        if estimate_table is not None:
            raise PerformanceError(
                "legacy performance journal has unexpected estimate state"
            )
        return version
    if estimate_table is None:
        raise PerformanceError("performance estimate journal is missing")
    estimate_info = connection.execute(
        "PRAGMA table_info(performance_estimates)"
    ).fetchall()
    if {row["name"] for row in estimate_info} != _ESTIMATE_COLUMNS:
        raise PerformanceError("performance estimate journal has an unsafe schema")
    estimate_primary = {row["name"] for row in estimate_info if row["pk"]}
    if estimate_primary != {"invocation_id"}:
        raise PerformanceError(
            "performance estimate journal has an unsafe primary key"
        )
    estimate_triggers = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = 'performance_estimates'"
        )
    }
    if not {
        "performance_estimates_no_update",
        "performance_estimates_no_delete",
    }.issubset(estimate_triggers):
        raise PerformanceError("performance estimate journal lacks append-only guards")
    estimate_indexes = {
        row["name"]: bool(row["unique"])
        for row in connection.execute("PRAGMA index_list(performance_estimates)")
    }
    if estimate_indexes.get("performance_one_estimate_internal") is not True:
        raise PerformanceError("performance estimate journal lacks uniqueness guards")
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(performance_estimates)"
    ).fetchall()
    if not any(
        row["table"] == "performance_events"
        and row["from"] == "internal_sequence"
        and row["to"] == "sequence"
        for row in foreign_keys
    ):
        raise PerformanceError("performance estimate journal lacks event binding")
    return version


def _validate_event_identity(row: Mapping[str, Any], context: str) -> None:
    _identity(
        row["runner"],
        row["model"],
        row["configuration"],
        row["identity_source"],
        context,
    )


def _validate_internal_row(row: Mapping[str, Any], context: str) -> dict[str, Any]:
    if row["event_type"] != "internal":
        raise PerformanceError(f"{context}: expected internal event")
    invocation_id = _canonical_uuid(row["invocation_id"], f"{context}.invocation_id")
    occurred = _canonical_utc(row["occurred_at_utc"], f"{context}.occurred_at_utc")
    lifecycle_start = _canonical_utc(
        row["lifecycle_started_at_utc"], f"{context}.lifecycle_started_at_utc"
    )
    lifecycle_finish = _canonical_utc(
        row["lifecycle_finished_at_utc"], f"{context}.lifecycle_finished_at_utc"
    )
    routine_total = _elapsed_ms(lifecycle_start, lifecycle_finish, f"{context}.lifecycle")
    if _utc_datetime(occurred) < _utc_datetime(lifecycle_finish):
        raise PerformanceError(f"{context}: recorded before lifecycle finished")
    strategy_start, strategy_finish = _validate_strategy_pair(
        row["strategy_started_at_utc"],
        row["strategy_finished_at_utc"],
        lifecycle_start,
        lifecycle_finish,
        context,
    )
    session = _enum(row["session"], SESSIONS, f"{context}.session")
    if row["task_duration_ms"] is not None or row["clock_source"] is not None:
        raise PerformanceError(f"{context}: internal event has task-only fields")
    _validate_event_identity(row, f"{context}.identity")
    strategy_ms = (
        None
        if strategy_start is None
        else _elapsed_ms(strategy_start, strategy_finish, f"{context}.strategy")
    )
    return {
        "sequence": row["sequence"],
        "invocation_id": invocation_id,
        "occurred_at_utc": occurred,
        "lifecycle_started_at_utc": lifecycle_start,
        "lifecycle_finished_at_utc": lifecycle_finish,
        "strategy_started_at_utc": strategy_start,
        "strategy_finished_at_utc": strategy_finish,
        "session": session,
        "runner": row["runner"],
        "model": row["model"],
        "configuration": row["configuration"],
        "identity_source": row["identity_source"],
        "routine_total_ms": routine_total,
        "strategy_execution_ms": strategy_ms,
    }


def _validate_task_row(
    row: Mapping[str, Any], internal: Mapping[str, Any], context: str
) -> dict[str, Any]:
    if row["event_type"] != "task":
        raise PerformanceError(f"{context}: expected task event")
    invocation_id = _canonical_uuid(row["invocation_id"], f"{context}.invocation_id")
    if invocation_id != internal["invocation_id"]:
        raise PerformanceError(f"{context}: task/internal invocation mismatch")
    occurred = _canonical_utc(row["occurred_at_utc"], f"{context}.occurred_at_utc")
    if _utc_datetime(occurred) < _utc_datetime(internal["occurred_at_utc"]):
        raise PerformanceError(f"{context}: task observed before internal record")
    for name in (
        "lifecycle_started_at_utc",
        "lifecycle_finished_at_utc",
        "strategy_started_at_utc",
        "strategy_finished_at_utc",
        "session",
    ):
        if row[name] is not None:
            raise PerformanceError(f"{context}: task event has internal-only field {name}")
    duration = _positive_int(row["task_duration_ms"], f"{context}.task_duration_ms")
    if duration < internal["routine_total_ms"]:
        raise PerformanceError(f"{context}: task duration is shorter than lifecycle")
    clock = _enum(row["clock_source"], CLOCK_SOURCES, f"{context}.clock_source")
    _validate_event_identity(row, f"{context}.identity")
    if clock == "codex-worked-for" and row["runner"] != "codex":
        raise PerformanceError(f"{context}: Codex clock source requires Codex runner")
    if clock == "claude-run-duration" and row["runner"] != "claude":
        raise PerformanceError(f"{context}: Claude clock source requires Claude runner")
    if internal["identity_source"] != "unknown":
        for field in ("runner", "model", "configuration"):
            if row[field] != internal[field]:
                raise PerformanceError(
                    f"{context}: {field} conflicts with internal record"
                )
    return {
        "sequence": row["sequence"],
        "occurred_at_utc": occurred,
        "task_duration_ms": duration,
        "clock_source": clock,
        "runner": row["runner"],
        "model": row["model"],
        "configuration": row["configuration"],
        "identity_source": row["identity_source"],
    }


def _validate_estimate_row(
    row: Mapping[str, Any], internal: Mapping[str, Any], context: str
) -> dict[str, Any]:
    invocation_id = _canonical_uuid(row["invocation_id"], f"{context}.invocation_id")
    if invocation_id != internal["invocation_id"]:
        raise PerformanceError(f"{context}: estimate/internal invocation mismatch")
    internal_sequence = _positive_int(
        row["internal_sequence"], f"{context}.internal_sequence"
    )
    if internal_sequence != internal["sequence"]:
        raise PerformanceError(f"{context}: estimate/internal sequence mismatch")
    occurred = _canonical_utc(row["occurred_at_utc"], f"{context}.occurred_at_utc")
    if occurred != internal["occurred_at_utc"]:
        raise PerformanceError(f"{context}: estimate/internal clock mismatch")
    run_start_pt = _canonical_run_start_pt(
        row["run_start_pt"], f"{context}.run_start_pt"
    )
    run_end_pt = _canonical_run_start_pt(
        row["run_end_pt"], f"{context}.run_end_pt"
    )
    if run_end_pt != _utc_to_pacific(occurred):
        raise PerformanceError(f"{context}: run end does not match observed clock")
    duration = _nonnegative_int(
        row["estimated_run_total_ms"], f"{context}.estimated_run_total_ms"
    )
    if duration != _elapsed_pt_ms(run_start_pt, run_end_pt, context):
        raise PerformanceError(f"{context}: estimated duration is inconsistent")
    if row["clock_source"] != ESTIMATE_CLOCK_SOURCE:
        raise PerformanceError(f"{context}: invalid estimate clock source")
    return {
        "invocation_id": invocation_id,
        "internal_sequence": internal_sequence,
        "occurred_at_utc": occurred,
        "run_start_pt": run_start_pt,
        "run_end_pt": run_end_pt,
        "estimated_run_total_ms": duration,
        "clock_source": ESTIMATE_CLOCK_SOURCE,
    }


def _rows_for_projection(
    connection: sqlite3.Connection, limit: int = PROJECTION_LIMIT
) -> list[sqlite3.Row]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise PerformanceError("projection limit must be a positive integer")
    return connection.execute(
        """
        WITH recent AS (
            SELECT invocation_id, sequence AS internal_sequence
            FROM performance_events
            WHERE event_type = 'internal'
            ORDER BY sequence DESC
            LIMIT ?
        )
        SELECT event.*, recent.internal_sequence
        FROM recent
        JOIN performance_events AS event
          ON event.invocation_id = recent.invocation_id
        ORDER BY recent.internal_sequence ASC, event.sequence ASC
        """,
        (limit,),
    ).fetchall()


def _build_projection(
    connection: sqlite3.Connection, limit: int = PROJECTION_LIMIT
) -> dict[str, Any]:
    rows = _rows_for_projection(connection, limit)
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    previous_sequence = 0
    for index, row in enumerate(rows):
        sequence = row["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise PerformanceError(f"journal row {index}: invalid sequence")
        if sequence <= previous_sequence and row["event_type"] == "internal":
            raise PerformanceError("performance internal sequence order is invalid")
        if row["event_type"] == "internal":
            internal = _validate_internal_row(row, f"journal row {sequence}")
            invocation_id = internal["invocation_id"]
            if invocation_id in grouped:
                raise PerformanceError("duplicate internal performance record")
            grouped[invocation_id] = {"internal": internal, "tasks": []}
            order.append(invocation_id)
            previous_sequence = sequence
        elif row["event_type"] == "task":
            invocation_id = _canonical_uuid(
                row["invocation_id"], f"journal row {sequence}.invocation_id"
            )
            if invocation_id not in grouped:
                raise PerformanceError("task observation precedes internal record")
            task = _validate_task_row(
                row, grouped[invocation_id]["internal"], f"journal row {sequence}"
            )
            if any(
                existing["clock_source"] == task["clock_source"]
                for existing in grouped[invocation_id]["tasks"]
            ):
                raise PerformanceError("duplicate task clock source")
            grouped[invocation_id]["tasks"].append(task)
        else:
            raise PerformanceError(f"journal row {sequence}: invalid event type")

    estimate_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' "
        "AND name = 'performance_estimates'"
    ).fetchone()
    if estimate_table is not None and order:
        placeholders = ", ".join("?" for _ in order)
        estimate_rows = connection.execute(
            "SELECT * FROM performance_estimates WHERE invocation_id IN ("
            + placeholders
            + ")",
            order,
        ).fetchall()
        for row in estimate_rows:
            invocation_id = _canonical_uuid(
                row["invocation_id"], "estimate.invocation_id"
            )
            if invocation_id not in grouped:
                raise PerformanceError("estimate has no projected internal record")
            if "estimate" in grouped[invocation_id]:
                raise PerformanceError("duplicate performance estimate")
            grouped[invocation_id]["estimate"] = _validate_estimate_row(
                row,
                grouped[invocation_id]["internal"],
                f"estimate for {invocation_id}",
            )

    records: list[dict[str, Any]] = []
    for invocation_id in order:
        internal = grouped[invocation_id]["internal"]
        tasks = grouped[invocation_id]["tasks"]
        estimate = grouped[invocation_id].get("estimate")
        known_task_identities = {
            (task["runner"], task["model"], task["configuration"])
            for task in tasks
            if task["identity_source"] != "unknown"
        }
        if internal["identity_source"] == "unknown" and len(known_task_identities) > 1:
            raise PerformanceError("task identity conflicts with prior task observation")
        selected_external = (
            None
            if not tasks
            else min(
                tasks,
                key=lambda task: (
                    CLOCK_SOURCE_PRIORITY[task["clock_source"]], task["sequence"]
                ),
            )
        )
        strategy_ms = internal["strategy_execution_ms"]
        routine_total = internal["routine_total_ms"]
        routine_overhead = (
            None if strategy_ms is None else routine_total - strategy_ms
        )
        if estimate is not None:
            task_duration = estimate["estimated_run_total_ms"]
            task_clock_source = estimate["clock_source"]
            task_observed_at = estimate["occurred_at_utc"]
            outside = None
        elif selected_external is not None:
            task_duration = selected_external["task_duration_ms"]
            task_clock_source = selected_external["clock_source"]
            task_observed_at = selected_external["occurred_at_utc"]
            outside = task_duration - routine_total
        else:
            task_duration = None
            task_clock_source = None
            task_observed_at = None
            outside = None
        total_overhead = (
            None
            if estimate is not None
            or selected_external is None
            or strategy_ms is None
            else task_duration - strategy_ms
        )
        if (
            selected_external is not None
            and selected_external["identity_source"] != "unknown"
        ):
            identity = selected_external
        elif internal["identity_source"] != "unknown":
            identity = internal
        else:
            identity = next(
                (task for task in tasks if task["identity_source"] != "unknown"),
                internal,
            )
        record = {
            "invocation_id": invocation_id,
            "lifecycle_started_at_utc": internal["lifecycle_started_at_utc"],
            "lifecycle_finished_at_utc": internal["lifecycle_finished_at_utc"],
            "strategy_started_at_utc": internal["strategy_started_at_utc"],
            "strategy_finished_at_utc": internal["strategy_finished_at_utc"],
            "session": internal["session"],
            "runner": identity["runner"],
            "model": identity["model"],
            "configuration": identity["configuration"],
            "identity_source": identity["identity_source"],
            "internal_recorded_at_utc": internal["occurred_at_utc"],
            "task_duration_ms": task_duration,
            "task_clock_source": task_clock_source,
            "task_observed_at_utc": task_observed_at,
            "routine_total_ms": routine_total,
            "strategy_execution_ms": strategy_ms,
            "routine_overhead_ms": routine_overhead,
            "outside_lifecycle_ms": outside,
            "total_overhead_ms": total_overhead,
        }
        records.append(record)
    watermark_row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) AS sequence FROM performance_events"
    ).fetchone()
    document = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "record_limit": limit,
        "record_count": len(records),
        "source_event_high_watermark": watermark_row["sequence"],
        "records": records,
    }
    validate_projection(document, allow_legacy=False)
    return document


def validate_projection(
    document: Any, *, allow_legacy: bool = True
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise PerformanceError("projection: expected an object")
    _exact_keys(document, _TOP_LEVEL_KEYS, "projection")
    schema_version = _nonnegative_int(
        document["schema_version"], "projection.schema_version"
    )
    allowed_versions = {PROJECTION_SCHEMA_VERSION}
    if allow_legacy:
        allowed_versions.add(LEGACY_PROJECTION_SCHEMA_VERSION)
    if schema_version not in allowed_versions:
        raise PerformanceError("projection.schema_version: unsupported value")
    record_limit = _nonnegative_int(document["record_limit"], "projection.record_limit")
    if record_limit != PROJECTION_LIMIT:
        raise PerformanceError("projection.record_limit: invalid value")
    records = document["records"]
    if not isinstance(records, list) or len(records) > PROJECTION_LIMIT:
        raise PerformanceError("projection.records: expected a bounded array")
    record_count = _nonnegative_int(document["record_count"], "projection.record_count")
    if record_count != len(records):
        raise PerformanceError("projection.record_count: does not match records")
    watermark = _nonnegative_int(
        document["source_event_high_watermark"],
        "projection.source_event_high_watermark",
    )
    if records and watermark == 0:
        raise PerformanceError("projection watermark cannot be zero with records")

    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        context = f"projection.records[{index}]"
        if not isinstance(record, dict):
            raise PerformanceError(f"{context}: expected an object")
        _exact_keys(record, _RECORD_KEYS, context)
        invocation_id = _canonical_uuid(record["invocation_id"], f"{context}.invocation_id")
        if invocation_id in seen_ids:
            raise PerformanceError(f"{context}.invocation_id: duplicate invocation")
        seen_ids.add(invocation_id)
        lifecycle_start = _canonical_utc(
            record["lifecycle_started_at_utc"], f"{context}.lifecycle_started_at_utc"
        )
        lifecycle_finish = _canonical_utc(
            record["lifecycle_finished_at_utc"], f"{context}.lifecycle_finished_at_utc"
        )
        routine_total = _elapsed_ms(lifecycle_start, lifecycle_finish, f"{context}.lifecycle")
        if _nonnegative_int(
            record["routine_total_ms"], f"{context}.routine_total_ms"
        ) != routine_total:
            raise PerformanceError(f"{context}.routine_total_ms: inconsistent value")
        internal_recorded = _canonical_utc(
            record["internal_recorded_at_utc"], f"{context}.internal_recorded_at_utc"
        )
        if _utc_datetime(internal_recorded) < _utc_datetime(lifecycle_finish):
            raise PerformanceError(f"{context}: internal record predates lifecycle finish")
        strategy_start, strategy_finish = _validate_strategy_pair(
            record["strategy_started_at_utc"],
            record["strategy_finished_at_utc"],
            lifecycle_start,
            lifecycle_finish,
            context,
        )
        _enum(record["session"], SESSIONS, f"{context}.session")
        _identity(
            record["runner"],
            record["model"],
            record["configuration"],
            record["identity_source"],
            f"{context}.identity",
        )
        if strategy_start is None:
            if any(
                record[name] is not None
                for name in (
                    "strategy_execution_ms",
                    "routine_overhead_ms",
                    "total_overhead_ms",
                )
            ):
                raise PerformanceError(f"{context}: missing strategy must retain null metrics")
        else:
            strategy_ms = _elapsed_ms(
                strategy_start, strategy_finish, f"{context}.strategy"
            )
            if _nonnegative_int(
                record["strategy_execution_ms"], f"{context}.strategy_execution_ms"
            ) != strategy_ms:
                raise PerformanceError(
                    f"{context}.strategy_execution_ms: inconsistent value"
                )
            if _nonnegative_int(
                record["routine_overhead_ms"], f"{context}.routine_overhead_ms"
            ) != routine_total - strategy_ms:
                raise PerformanceError(f"{context}.routine_overhead_ms: inconsistent value")

        task_values = (
            record["task_duration_ms"],
            record["task_clock_source"],
            record["task_observed_at_utc"],
        )
        if all(value is None for value in task_values):
            if record["outside_lifecycle_ms"] is not None:
                raise PerformanceError(f"{context}: missing task must retain null outside metric")
            if record["total_overhead_ms"] is not None:
                raise PerformanceError(f"{context}: missing task must retain null total metric")
        elif any(value is None for value in task_values):
            raise PerformanceError(f"{context}: task observation fields are all-or-none")
        else:
            task_clock_source = record["task_clock_source"]
            is_estimate = task_clock_source == ESTIMATE_CLOCK_SOURCE
            if (
                is_estimate
                and schema_version == LEGACY_PROJECTION_SCHEMA_VERSION
            ):
                raise PerformanceError(
                    f"{context}: legacy projection cannot contain canonical "
                    "run-duration timing"
                )
            task_duration = (
                _nonnegative_int(
                    record["task_duration_ms"], f"{context}.task_duration_ms"
                )
                if is_estimate
                else _positive_int(
                    record["task_duration_ms"], f"{context}.task_duration_ms"
                )
            )
            if not is_estimate:
                if task_duration < routine_total:
                    raise PerformanceError(
                        f"{context}: task duration is shorter than lifecycle"
                    )
                _enum(
                    task_clock_source,
                    CLOCK_SOURCES,
                    f"{context}.task_clock_source",
                )
            task_observed = _canonical_utc(
                record["task_observed_at_utc"], f"{context}.task_observed_at_utc"
            )
            if is_estimate:
                if task_observed != internal_recorded:
                    raise PerformanceError(
                        f"{context}: estimated task clock must equal internal boundary"
                    )
                if record["outside_lifecycle_ms"] is not None:
                    raise PerformanceError(
                        f"{context}: estimated task must retain null outside metric"
                    )
                if record["total_overhead_ms"] is not None:
                    raise PerformanceError(
                        f"{context}: estimated task must retain null total metric"
                    )
            else:
                if _utc_datetime(task_observed) < _utc_datetime(internal_recorded):
                    raise PerformanceError(
                        f"{context}: task observation predates internal record"
                    )
                if _nonnegative_int(
                    record["outside_lifecycle_ms"],
                    f"{context}.outside_lifecycle_ms",
                ) != task_duration - routine_total:
                    raise PerformanceError(
                        f"{context}.outside_lifecycle_ms: inconsistent value"
                    )
                if strategy_start is not None:
                    expected_total = task_duration - record["strategy_execution_ms"]
                    if _nonnegative_int(
                        record["total_overhead_ms"],
                        f"{context}.total_overhead_ms",
                    ) != expected_total:
                        raise PerformanceError(
                            f"{context}.total_overhead_ms: inconsistent value"
                        )
    return document


def _atomic_write_projection(path: str, document: Mapping[str, Any]) -> None:
    validate_projection(document, allow_legacy=False)
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    if not directory:
        raise PerformanceError("projection file must have a parent directory")
    try:
        run_lifecycle._prepare_state_directory(directory)
    except run_lifecycle.LifecycleError as exc:
        raise PerformanceError(
            f"performance projection filesystem preflight failed: {exc}"
        ) from exc
    descriptor, temporary = tempfile.mkstemp(
        prefix=".rhmra-run-performance-", suffix=".tmp", dir=directory
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
        validate_projection(readback, allow_legacy=False)
        if readback != document:
            raise PerformanceError("projection readback did not match serialized data")
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
    _reject_path_aliases(
        performance_state_file=state_file,
        performance_projection_file=projection_file,
    )
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


def _connect_read_only(state_file: str) -> sqlite3.Connection:
    absolute = os.path.abspath(state_file)
    if not os.path.isfile(absolute):
        raise PerformanceError("performance event journal is missing")
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
        return connection
    except PerformanceError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        if run_lifecycle._is_readonly_error(exc) and run_lifecycle._has_hot_rollback_journal(
            absolute
        ):
            raise PerformanceError(
                "cannot open performance journal read-only: an interrupted SQLite "
                "transaction requires host-native recovery; run run_performance.py "
                "export from the native host and never edit or delete SQLite sidecars"
            ) from exc
        raise PerformanceError(
            f"cannot open performance journal read-only: {exc}"
        ) from exc


def validate_current_projection_read_only(
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
) -> dict[str, Any]:
    _reject_path_aliases(
        performance_state_file=state_file,
        performance_projection_file=projection_file,
    )
    connection = _connect_read_only(state_file)
    try:
        connection.execute("BEGIN")
        expected = _build_projection(connection)
        actual = _load_json(projection_file)
        validate_projection(actual, allow_legacy=True)
        comparable = actual
        if actual["schema_version"] == LEGACY_PROJECTION_SCHEMA_VERSION:
            comparable = dict(actual)
            comparable["schema_version"] = PROJECTION_SCHEMA_VERSION
        if comparable != expected:
            raise PerformanceError("projection is valid JSON but stale or inconsistent")
        connection.rollback()
        return actual
    except PerformanceError:
        if connection.in_transaction:
            connection.rollback()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection.in_transaction:
            connection.rollback()
        raise PerformanceError(f"cannot validate performance projection: {exc}") from exc
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
            f"observation committed but projection publication failed: {exc}",
            action,
            invocation_id,
        ) from exc


def record_internal(
    *,
    invocation_id: str,
    session: str,
    runner: str,
    model: str,
    configuration: str,
    identity_source: str,
    strategy_start_utc: str | None = None,
    strategy_end_utc: str | None = None,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    lifecycle_state_file: str = DEFAULT_LIFECYCLE_STATE_FILE,
    lifecycle_projection_file: str = DEFAULT_LIFECYCLE_PROJECTION_FILE,
    now_utc: str | None = None,
) -> dict[str, Any]:
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    session = _enum(session, SESSIONS, "session")
    runner, model, configuration, identity_source = _identity(
        runner, model, configuration, identity_source
    )
    _reject_path_aliases(
        performance_state_file=state_file,
        performance_projection_file=projection_file,
        lifecycle_state_file=lifecycle_state_file,
        lifecycle_projection_file=lifecycle_projection_file,
    )
    lifecycle = _finished_lifecycle_record(
        invocation_id, lifecycle_state_file, lifecycle_projection_file
    )
    lifecycle_start = lifecycle["started_at_utc"]
    lifecycle_finish = lifecycle["finished_at_utc"]
    strategy_start, strategy_finish = _validate_strategy_pair(
        strategy_start_utc,
        strategy_end_utc,
        lifecycle_start,
        lifecycle_finish,
        "record-internal",
    )
    occurred = _now_utc(now_utc)
    if _utc_datetime(occurred) < _utc_datetime(lifecycle_finish):
        raise PerformanceError("record-internal time precedes lifecycle finish")
    estimated_run_start_pt = lifecycle["run_start_pt"]
    if estimated_run_start_pt is None:
        estimated_run_end_pt = None
        estimated_run_total_ms = None
        estimate_clock_source = None
        estimated_run_total_display = None
    else:
        estimated_run_end_pt = _utc_to_pacific(occurred)
        estimated_run_total_ms = _elapsed_pt_ms(
            estimated_run_start_pt,
            estimated_run_end_pt,
            "final-summary-boundary",
        )
        estimate_clock_source = ESTIMATE_CLOCK_SOURCE
        estimated_run_total_display = _duration_display(estimated_run_total_ms)

    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM performance_events "
            "WHERE invocation_id = ? AND event_type = 'internal'",
            (invocation_id,),
        ).fetchone() is not None:
            raise PerformanceConflict("internal observation already exists")
        cursor = connection.execute(
            """
            INSERT INTO performance_events (
                invocation_id, event_type, occurred_at_utc,
                lifecycle_started_at_utc, lifecycle_finished_at_utc,
                strategy_started_at_utc, strategy_finished_at_utc, session,
                task_duration_ms, runner, model, configuration,
                identity_source, clock_source
            ) VALUES (?, 'internal', ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL)
            """,
            (
                invocation_id,
                occurred,
                lifecycle_start,
                lifecycle_finish,
                strategy_start,
                strategy_finish,
                session,
                runner,
                model,
                configuration,
                identity_source,
            ),
        )
        sequence = int(cursor.lastrowid)
        if estimated_run_start_pt is not None:
            connection.execute(
                """
                INSERT INTO performance_estimates (
                    invocation_id, internal_sequence, occurred_at_utc,
                    run_start_pt, run_end_pt, estimated_run_total_ms,
                    clock_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invocation_id,
                    sequence,
                    occurred,
                    estimated_run_start_pt,
                    estimated_run_end_pt,
                    estimated_run_total_ms,
                    estimate_clock_source,
                ),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    document = _publish_after_append(
        "record-internal", invocation_id, state_file, projection_file
    )
    record = next(
        item for item in document["records"] if item["invocation_id"] == invocation_id
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "record-internal",
        "ok": True,
        "invocation_id": invocation_id,
        "sequence": sequence,
        "routine_total_ms": record["routine_total_ms"],
        "strategy_execution_ms": record["strategy_execution_ms"],
        "routine_overhead_ms": record["routine_overhead_ms"],
        "estimated_run_start_pt": estimated_run_start_pt,
        "estimated_run_end_pt": estimated_run_end_pt,
        "estimated_run_total_ms": estimated_run_total_ms,
        "estimated_run_total_display": estimated_run_total_display,
        "estimate_clock_source": estimate_clock_source,
        "projection_record_count": document["record_count"],
    }


def observe_task(
    *,
    invocation_id: str,
    task_duration_ms: int,
    runner: str,
    model: str,
    configuration: str,
    identity_source: str,
    clock_source: str,
    state_file: str = DEFAULT_STATE_FILE,
    projection_file: str = DEFAULT_PROJECTION_FILE,
    lifecycle_state_file: str = DEFAULT_LIFECYCLE_STATE_FILE,
    lifecycle_projection_file: str = DEFAULT_LIFECYCLE_PROJECTION_FILE,
    now_utc: str | None = None,
) -> dict[str, Any]:
    invocation_id = _canonical_uuid(invocation_id, "invocation_id")
    task_duration_ms = _positive_int(task_duration_ms, "task_duration_ms")
    runner, model, configuration, identity_source = _identity(
        runner, model, configuration, identity_source
    )
    _reject_path_aliases(
        performance_state_file=state_file,
        performance_projection_file=projection_file,
        lifecycle_state_file=lifecycle_state_file,
        lifecycle_projection_file=lifecycle_projection_file,
    )
    clock_source = _enum(clock_source, CLOCK_SOURCES, "clock_source")
    if clock_source == "codex-worked-for" and runner != "codex":
        raise PerformanceError("Codex clock source requires Codex runner")
    if clock_source == "claude-run-duration" and runner != "claude":
        raise PerformanceError("Claude clock source requires Claude runner")
    lifecycle = _finished_lifecycle_record(
        invocation_id, lifecycle_state_file, lifecycle_projection_file
    )
    routine_total = _elapsed_ms(
        lifecycle["started_at_utc"], lifecycle["finished_at_utc"], "lifecycle"
    )
    if task_duration_ms < routine_total:
        raise PerformanceError("task duration is shorter than lifecycle")
    occurred = _now_utc(now_utc)

    connection = _connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        internal_row = connection.execute(
            "SELECT * FROM performance_events "
            "WHERE invocation_id = ? AND event_type = 'internal'",
            (invocation_id,),
        ).fetchone()
        if internal_row is None:
            raise PerformanceConflict("internal observation must be recorded first")
        internal = _validate_internal_row(internal_row, "existing internal observation")
        if (
            internal["lifecycle_started_at_utc"] != lifecycle["started_at_utc"]
            or internal["lifecycle_finished_at_utc"] != lifecycle["finished_at_utc"]
        ):
            raise PerformanceError("internal observation conflicts with lifecycle")
        if _utc_datetime(occurred) < _utc_datetime(internal["occurred_at_utc"]):
            raise PerformanceError("task observation predates internal observation")
        if internal["identity_source"] != "unknown":
            for field, candidate in (
                ("runner", runner),
                ("model", model),
                ("configuration", configuration),
            ):
                if candidate != internal[field]:
                    raise PerformanceError(
                        f"task {field} conflicts with internal observation"
                    )
        existing_task_rows = connection.execute(
            "SELECT * FROM performance_events WHERE invocation_id = ? "
            "AND event_type = 'task' ORDER BY sequence ASC",
            (invocation_id,),
        ).fetchall()
        existing_tasks = [
            _validate_task_row(row, internal, "existing task observation")
            for row in existing_task_rows
        ]
        if any(task["clock_source"] == clock_source for task in existing_tasks):
            raise PerformanceConflict("task observation clock source already exists")
        known_identities = {
            (task["runner"], task["model"], task["configuration"])
            for task in existing_tasks
            if task["identity_source"] != "unknown"
        }
        if identity_source != "unknown":
            known_identities.add((runner, model, configuration))
        if len(known_identities) > 1:
            raise PerformanceError("task identity conflicts with prior task observation")
        cursor = connection.execute(
            """
            INSERT INTO performance_events (
                invocation_id, event_type, occurred_at_utc,
                lifecycle_started_at_utc, lifecycle_finished_at_utc,
                strategy_started_at_utc, strategy_finished_at_utc, session,
                task_duration_ms, runner, model, configuration,
                identity_source, clock_source
            ) VALUES (?, 'task', ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                invocation_id,
                occurred,
                task_duration_ms,
                runner,
                model,
                configuration,
                identity_source,
                clock_source,
            ),
        )
        sequence = int(cursor.lastrowid)
        candidate = {
            "sequence": sequence,
            "task_duration_ms": task_duration_ms,
            "clock_source": clock_source,
            "runner": runner,
            "model": model,
            "configuration": configuration,
            "identity_source": identity_source,
            "occurred_at_utc": occurred,
        }
        selected_external = min(
            existing_tasks + [candidate],
            key=lambda task: (
                CLOCK_SOURCE_PRIORITY[task["clock_source"]], task["sequence"]
            ),
        )
        # Receipt schema v1 reports selection among external observations.  The
        # projection's schema-v2 canonical automatic duration is independent;
        # preserving the receipt contract lets existing callers attach and
        # audit a reference without silently changing its returned meaning.
        selected_clock_source = selected_external["clock_source"]
        selected_duration = selected_external["task_duration_ms"]
        selected_outside = selected_duration - routine_total
        selected_total_overhead = (
            None
            if internal["strategy_execution_ms"] is None
            else selected_duration - internal["strategy_execution_ms"]
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    document = _publish_after_append(
        "observe-task", invocation_id, state_file, projection_file
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "observe-task",
        "ok": True,
        "invocation_id": invocation_id,
        "sequence": sequence,
        "task_duration_ms": task_duration_ms,
        "clock_source": clock_source,
        "selected_clock_source": selected_clock_source,
        "outside_lifecycle_ms": selected_outside,
        "total_overhead_ms": selected_total_overhead,
        "projection_record_count": document["record_count"],
    }


def _reject_unused(args: argparse.Namespace, allowed: set[str]) -> None:
    action_fields = {
        "invocation_id",
        "strategy_start_utc",
        "strategy_end_utc",
        "session",
        "task_duration_ms",
        "runner",
        "model",
        "configuration",
        "identity_source",
        "clock_source",
    }
    for name in sorted(action_fields - allowed):
        if getattr(args, name) is not None:
            raise PerformanceError(
                f"--{name.replace('_', '-')} is not valid for {args.action}"
            )


def _require(args: argparse.Namespace, names: Sequence[str]) -> None:
    missing = [name for name in names if getattr(args, name) is None]
    if missing:
        rendered = ", ".join(f"--{name.replace('_', '-')}" for name in missing)
        raise PerformanceError(f"{args.action} requires {rendered}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = StrictArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "action", choices=("record-internal", "observe-task", "export", "validate")
    )
    parser.add_argument("--invocation-id")
    parser.add_argument("--strategy-start-utc")
    parser.add_argument("--strategy-end-utc")
    parser.add_argument("--session")
    parser.add_argument("--task-duration-ms")
    parser.add_argument("--runner")
    parser.add_argument("--model")
    parser.add_argument("--configuration")
    parser.add_argument("--identity-source")
    parser.add_argument("--clock-source")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--projection-file", default=DEFAULT_PROJECTION_FILE)
    parser.add_argument("--lifecycle-state-file", default=DEFAULT_LIFECYCLE_STATE_FILE)
    parser.add_argument(
        "--lifecycle-projection-file", default=DEFAULT_LIFECYCLE_PROJECTION_FILE
    )
    parser.add_argument(
        "--now-utc", help="test/diagnostic override, canonical ISO-8601 UTC"
    )
    args: argparse.Namespace | None = None
    action = "unknown"
    try:
        args = parser.parse_args(argv)
        action = args.action
        if action == "record-internal":
            allowed = {
                "invocation_id",
                "strategy_start_utc",
                "strategy_end_utc",
                "session",
                "runner",
                "model",
                "configuration",
                "identity_source",
            }
            _reject_unused(args, allowed)
            _require(
                args,
                (
                    "invocation_id",
                    "session",
                    "runner",
                    "model",
                    "configuration",
                    "identity_source",
                ),
            )
            result = record_internal(
                invocation_id=args.invocation_id,
                strategy_start_utc=args.strategy_start_utc,
                strategy_end_utc=args.strategy_end_utc,
                session=args.session,
                runner=args.runner,
                model=args.model,
                configuration=args.configuration,
                identity_source=args.identity_source,
                state_file=args.state_file,
                projection_file=args.projection_file,
                lifecycle_state_file=args.lifecycle_state_file,
                lifecycle_projection_file=args.lifecycle_projection_file,
                now_utc=args.now_utc,
            )
        elif action == "observe-task":
            allowed = {
                "invocation_id",
                "task_duration_ms",
                "runner",
                "model",
                "configuration",
                "identity_source",
                "clock_source",
            }
            _reject_unused(args, allowed)
            _require(
                args,
                (
                    "invocation_id",
                    "task_duration_ms",
                    "runner",
                    "model",
                    "configuration",
                    "identity_source",
                    "clock_source",
                ),
            )
            result = observe_task(
                invocation_id=args.invocation_id,
                task_duration_ms=args.task_duration_ms,
                runner=args.runner,
                model=args.model,
                configuration=args.configuration,
                identity_source=args.identity_source,
                clock_source=args.clock_source,
                state_file=args.state_file,
                projection_file=args.projection_file,
                lifecycle_state_file=args.lifecycle_state_file,
                lifecycle_projection_file=args.lifecycle_projection_file,
                now_utc=args.now_utc,
            )
        elif action == "export":
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
            document = validate_current_projection_read_only(
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
    except PerformanceConflict as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": action,
            "ok": False,
            "reason": "performance_conflict",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 2
    except (PerformanceError, OSError, sqlite3.Error) as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": action,
            "ok": False,
            "reason": "performance_state_error",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False, sort_keys=True))
        return 1

    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
