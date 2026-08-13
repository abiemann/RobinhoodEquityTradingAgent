#!/usr/bin/env python3
"""Validate and atomically publish the dashboard's account status snapshot.

The trading runner authors one candidate JSON file in its session scratch
directory.  This helper is the sole path from that candidate to the stable
``run-reports/rhmra-status-*.json`` namespace: it strictly parses and validates
the complete schema, stages the unchanged bytes beside the destination, and
commits them with an atomic no-clobber link.  A malformed candidate or failed
read-back therefore cannot replace an earlier truthful snapshot.

The pure :func:`validate_status_snapshot` and read-only
:func:`load_status_snapshot` functions are also the schema authority for
consumers such as ``dashboard/serve.py``.  Neither function writes to disk.

Typical invocation::

    python status_snapshot.py publish \
        --invocation-id 11111111-1111-4111-8111-111111111111 \
        --scratch C:\\path\\to\\session-scratch \
        --candidate C:\\path\\to\\session-scratch\\rhmra-status-candidate.json \
        --report D:\\project\\run-reports\\rhmra-log-2026_01_02-10_13.md \
        --output D:\\project\\run-reports\\rhmra-status-2026_01_02-10_13.json

Every CLI invocation emits exactly one JSON object on stdout.  Exit zero means
the final file was atomically published and read back successfully.  Any
nonzero or malformed result is publication-indeterminate until the caller runs
the candidate-bound, read-only ``verify`` action; it must never blindly retry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence

import broker_snapshot
import run_lifecycle


SCHEMA_VERSION = 1
MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_SNAPSHOT_BYTES = 1_000_000
MAX_REPORT_BYTES = 5_000_000
MAX_POSITIONS = 1_000
MAX_SKIP_REASON_CHARS = 1_000
ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_DIR = ROOT / "run-reports"

SESSIONS = frozenset(
    {
        "pre-market",
        "regular",
        "after-hours",
        "closed",
        "closed-weekend",
        "closed-holiday",
        "closed-early",
        "calendar-unknown",
    }
)
CIRCUIT_BREAKER_STATES = frozenset(
    {"clear", "tripped", "indeterminate", "not-evaluated"}
)
ENTRY_PHASES = frozenset({"ran", "skipped", "halted"})
STOP_STATES = frozenset({"confirmed", "queued", "none"})

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "run_start_pt",
        "rules_version",
        "dry_run",
        "session",
        "account",
        "realized_pnl_today",
        "positions",
        "guards",
    }
)
_ACCOUNT_KEYS = frozenset(
    {"total_value", "cash", "buying_power", "equity_value"}
)
_POSITION_KEYS = frozenset(
    {
        "symbol",
        "quantity",
        "avg_buy_price",
        "current_price",
        "stop_price",
        "stop_state",
    }
)
_GUARD_KEYS = frozenset(
    {
        "circuit_breaker",
        "stop_fills_today",
        "entry_phase",
        "entry_skip_reason",
    }
)
_PT_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?-(?:07|08):00$"
)
_RULES_VERSION_RE = re.compile(r"^(?:[0-9a-f]{4,40}(?:-dirty)?|unknown)$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,15}$")
_STATUS_FILENAME_RE = re.compile(r"^rhmra-status-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}\.json$")
_REPORT_FILENAME_RE = re.compile(r"^rhmra-log-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}\.md$")


class StatusSnapshotError(ValueError):
    """A status snapshot or publication request is unsafe to accept."""


class StatusSnapshotCliError(StatusSnapshotError):
    """Invalid command-line usage that must remain a JSON diagnostic."""


class StatusSnapshotMissing(StatusSnapshotError):
    """The expected final status path does not exist."""


class StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise StatusSnapshotCliError(message)


def _reject_nonfinite(token: str) -> None:
    raise StatusSnapshotError(f"non-finite JSON constant {token!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise StatusSnapshotError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _loads_strict(text: str, context: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_strict_object,
        )
    except StatusSnapshotError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise StatusSnapshotError(
            f"{context}: cannot parse strict JSON: {exc}"
        ) from exc


def _read_strict_json(path: os.PathLike[str] | str) -> tuple[Any, bytes]:
    display = os.fspath(path)
    try:
        source = Path(path)
        if source.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise StatusSnapshotError(
                f"{display}: snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
            )
        with source.open("rb") as handle:
            raw = handle.read(MAX_SNAPSHOT_BYTES + 1)
    except StatusSnapshotError:
        raise
    except OSError as exc:
        raise StatusSnapshotError(f"{display}: cannot read snapshot: {exc}") from exc
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise StatusSnapshotError(
            f"{display}: snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StatusSnapshotError(f"{display}: snapshot is not UTF-8") from exc
    return _loads_strict(text, display), raw


def _object(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise StatusSnapshotError(f"{context}: expected an object")
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: frozenset[str], context: str
) -> None:
    keys = set(value)
    if keys == expected:
        return
    missing = sorted(expected - keys)
    extra = sorted(keys - expected)
    details: list[str] = []
    if missing:
        details.append("missing " + ", ".join(repr(key) for key in missing))
    if extra:
        details.append("unexpected " + ", ".join(repr(key) for key in extra))
    raise StatusSnapshotError(f"{context}: invalid keys ({'; '.join(details)})")


def _finite_number(value: Any, context: str) -> int | float | Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise StatusSnapshotError(f"{context}: expected a finite JSON number")
    if isinstance(value, Decimal):
        finite = value.is_finite()
    elif isinstance(value, float):
        finite = math.isfinite(value)
    else:
        finite = True
    if not finite:
        raise StatusSnapshotError(f"{context}: expected a finite JSON number")
    if abs(number := value) > MAX_SAFE_INTEGER:
        raise StatusSnapshotError(
            f"{context}: magnitude exceeds the JSON safe-integer boundary"
        )
    return number


def _positive_number(value: Any, context: str) -> int | float | Decimal:
    number = _finite_number(value, context)
    if number <= 0:
        raise StatusSnapshotError(f"{context}: expected a number greater than zero")
    return number


def _enum(value: Any, allowed: frozenset[str], context: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise StatusSnapshotError(f"{context}: expected one of {choices}")
    return value


def _validate_run_start_pt(value: Any) -> datetime:
    if not isinstance(value, str) or _PT_TIMESTAMP_RE.fullmatch(value) is None:
        raise StatusSnapshotError(
            "status.run_start_pt: expected an ISO 8601 Pacific timestamp "
            "with -07:00 or -08:00 offset"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise StatusSnapshotError(
            "status.run_start_pt: invalid calendar timestamp"
        ) from exc
    if parsed.utcoffset() not in {timedelta(hours=-7), timedelta(hours=-8)}:
        raise StatusSnapshotError("status.run_start_pt: invalid Pacific offset")
    return parsed


def validate_status_snapshot(document: Any) -> Mapping[str, Any]:
    """Validate the complete status schema and return *document* unchanged.

    This function is pure: it performs no filesystem or global-state mutation.
    It raises :class:`StatusSnapshotError` on the first unsafe field.
    """

    status = _object(document, "status")
    _exact_keys(status, _TOP_LEVEL_KEYS, "status")

    if type(status["schema_version"]) is not int or status["schema_version"] != 1:
        raise StatusSnapshotError("status.schema_version: expected integer 1")
    run_start = _validate_run_start_pt(status["run_start_pt"])

    rules_version = status["rules_version"]
    if (
        not isinstance(rules_version, str)
        or _RULES_VERSION_RE.fullmatch(rules_version) is None
    ):
        raise StatusSnapshotError(
            "status.rules_version: expected a short lowercase git hash, "
            "optional -dirty suffix, or unknown"
        )
    if type(status["dry_run"]) is not bool:
        raise StatusSnapshotError("status.dry_run: expected a boolean")
    _enum(status["session"], SESSIONS, "status.session")

    account = _object(status["account"], "status.account")
    _exact_keys(account, _ACCOUNT_KEYS, "status.account")
    for key in sorted(_ACCOUNT_KEYS):
        _finite_number(account[key], f"status.account.{key}")

    realized = status["realized_pnl_today"]
    if realized is not None:
        _finite_number(realized, "status.realized_pnl_today")

    positions = status["positions"]
    if not isinstance(positions, list):
        raise StatusSnapshotError("status.positions: expected an array")
    if len(positions) > MAX_POSITIONS:
        raise StatusSnapshotError(
            f"status.positions: exceeds {MAX_POSITIONS} entries"
        )
    seen_symbols: set[str] = set()
    for index, raw_position in enumerate(positions):
        context = f"status.positions[{index}]"
        position = _object(raw_position, context)
        _exact_keys(position, _POSITION_KEYS, context)
        symbol = position["symbol"]
        if not isinstance(symbol, str) or _SYMBOL_RE.fullmatch(symbol) is None:
            raise StatusSnapshotError(f"{context}.symbol: invalid ticker symbol")
        if symbol in seen_symbols:
            raise StatusSnapshotError(f"{context}.symbol: duplicate symbol {symbol!r}")
        seen_symbols.add(symbol)
        _positive_number(position["quantity"], f"{context}.quantity")
        _positive_number(position["avg_buy_price"], f"{context}.avg_buy_price")
        _positive_number(position["current_price"], f"{context}.current_price")
        stop_state = _enum(position["stop_state"], STOP_STATES, f"{context}.stop_state")
        stop_price = position["stop_price"]
        if stop_state == "none":
            if stop_price is not None:
                raise StatusSnapshotError(
                    f"{context}.stop_price: must be null when stop_state is none"
                )
        else:
            if stop_price is None:
                raise StatusSnapshotError(
                    f"{context}.stop_price: required for an active stop"
                )
            _positive_number(stop_price, f"{context}.stop_price")

    equity_value = account["equity_value"]
    if (not positions and equity_value != 0) or (positions and equity_value == 0):
        raise StatusSnapshotError(
            "status: positions and account.equity_value disagree about whether "
            "the account holds equity"
        )
    try:
        equity_decimal = Decimal(str(equity_value))
        quoted_equity = sum(
            (
                Decimal(str(position["quantity"]))
                * Decimal(str(position["current_price"]))
                for position in positions
            ),
            Decimal(0),
        )
    except (InvalidOperation, ValueError) as exc:
        raise StatusSnapshotError(
            "status: cannot calculate quoted-equity coherence"
        ) from exc
    quoted_tolerance = max(
        Decimal("0.05"),
        Decimal("0.01") * max(abs(equity_decimal), abs(quoted_equity)),
    )
    if abs(equity_decimal - quoted_equity) > quoted_tolerance:
        raise StatusSnapshotError(
            "status: account.equity_value and quoted position equity are incoherent"
        )

    guards = _object(status["guards"], "status.guards")
    _exact_keys(guards, _GUARD_KEYS, "status.guards")
    circuit_breaker = _enum(
        guards["circuit_breaker"],
        CIRCUIT_BREAKER_STATES,
        "status.guards.circuit_breaker",
    )
    stop_fills = guards["stop_fills_today"]
    if stop_fills is not None:
        if (
            type(stop_fills) is not int
            or stop_fills < 0
            or stop_fills > MAX_SAFE_INTEGER
        ):
            raise StatusSnapshotError(
                "status.guards.stop_fills_today: expected a non-negative "
                "safe integer or null"
            )
    entry_phase = _enum(
        guards["entry_phase"], ENTRY_PHASES, "status.guards.entry_phase"
    )
    skip_reason = guards["entry_skip_reason"]
    if entry_phase == "ran":
        if skip_reason is not None:
            raise StatusSnapshotError(
                "status.guards.entry_skip_reason: must be null when entry_phase ran"
            )
    else:
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            raise StatusSnapshotError(
                "status.guards.entry_skip_reason: expected a non-empty string "
                "when entry_phase did not run"
            )
        if len(skip_reason) > MAX_SKIP_REASON_CHARS:
            raise StatusSnapshotError(
                "status.guards.entry_skip_reason: exceeds 1000 characters"
            )

    if circuit_breaker == "not-evaluated":
        if stop_fills is not None or entry_phase != "skipped":
            raise StatusSnapshotError(
                "status.guards: not-evaluated requires null stop_fills_today "
                "and skipped entry_phase"
            )

    # Retain the parse solely to make the filename derivation below explicit;
    # the validator intentionally returns the caller's original mapping.
    del run_start
    return status


def load_status_snapshot(path: os.PathLike[str] | str) -> Mapping[str, Any]:
    """Strictly load and validate *path* without writing or changing it."""

    document, _raw = _read_strict_json(path)
    return validate_status_snapshot(document)


def _expected_filename(document: Mapping[str, Any]) -> str:
    run_start = datetime.fromisoformat(str(document["run_start_pt"]))
    return run_start.strftime("rhmra-status-%Y_%m_%d-%H_%M.json")


def _validate_lifecycle_binding(
    document: Mapping[str, Any], output_path: Path, *, invocation_id: str,
    lifecycle_state_file: os.PathLike[str] | str,
    lifecycle_projection_file: os.PathLike[str] | str,
) -> Mapping[str, Any]:
    '''Bind one candidate to the lifecycle journal's exact start timestamp.'''
    try:
        receipt = run_lifecycle.invocation_status(
            invocation_id=invocation_id,
            state_file=os.fspath(lifecycle_state_file),
            projection_file=os.fspath(lifecycle_projection_file),
        )
    except (run_lifecycle.LifecycleError, OSError) as exc:
        raise StatusSnapshotError(f'lifecycle: {exc}') from exc
    if receipt['classification'] != 'running':
        raise StatusSnapshotError('lifecycle: invocation is not currently running')
    if document['run_start_pt'] != receipt['run_start_pt']:
        expected = receipt['run_start_pt']
        raise StatusSnapshotError(
            f'status.run_start_pt: must exactly match lifecycle binding ({expected})'
        )
    if output_path.name != receipt['expected_status_file']:
        expected = receipt['expected_status_file']
        raise StatusSnapshotError(
            f'output: filename must match lifecycle binding ({expected})'
        )
    return receipt


def _validated_output_path(
    output: os.PathLike[str] | str,
    report_dir: os.PathLike[str] | str,
    *,
    require_exists: bool,
) -> tuple[Path, Path]:
    if not Path(output).is_absolute():
        raise StatusSnapshotError("output: expected an absolute path")
    if not Path(report_dir).is_absolute():
        raise StatusSnapshotError("report_dir: expected an absolute path")
    try:
        report_path = Path(report_dir).resolve(strict=True)
    except OSError as exc:
        raise StatusSnapshotError(
            f"report_dir: cannot resolve directory: {exc}"
        ) from exc
    if not report_path.is_dir():
        raise StatusSnapshotError("report_dir: expected a directory")

    output_path = Path(output)
    try:
        output_parent = output_path.parent.resolve(strict=True)
    except OSError as exc:
        raise StatusSnapshotError(f"output: cannot resolve parent: {exc}") from exc
    if output_parent != report_path:
        raise StatusSnapshotError("output: expected a direct child of report_dir")
    if _STATUS_FILENAME_RE.fullmatch(output_path.name) is None:
        raise StatusSnapshotError("output: invalid status snapshot filename")

    if require_exists:
        if not output_path.exists():
            raise StatusSnapshotMissing("output: expected status snapshot is absent")
        if output_path.is_symlink() or not output_path.is_file():
            raise StatusSnapshotError("output: expected a regular non-symlink file")
        try:
            if output_path.resolve(strict=True).parent != report_path:
                raise StatusSnapshotError(
                    "output: resolved snapshot escapes report_dir"
                )
        except OSError as exc:
            raise StatusSnapshotError(f"output: cannot resolve snapshot: {exc}") from exc
    elif output_path.exists():
        raise StatusSnapshotError("output: refusing to replace an existing snapshot")
    return output_path, report_path


def _validate_bound_report(
    report: os.PathLike[str] | str,
    report_dir: os.PathLike[str] | str,
    expected_filename: str,
) -> Path:
    """Strictly read back the lifecycle-bound report before status publication."""

    if not Path(report).is_absolute():
        raise StatusSnapshotError("report: expected an absolute path")
    if not Path(report_dir).is_absolute():
        raise StatusSnapshotError("report_dir: expected an absolute path")
    try:
        report_directory = Path(report_dir).resolve(strict=True)
    except OSError as exc:
        raise StatusSnapshotError(
            f"report_dir: cannot resolve directory: {exc}"
        ) from exc
    if not report_directory.is_dir():
        raise StatusSnapshotError("report_dir: expected a directory")

    supplied = Path(report)
    if supplied.name != expected_filename:
        raise StatusSnapshotError(
            f"report: filename must match lifecycle binding ({expected_filename})"
        )
    if _REPORT_FILENAME_RE.fullmatch(supplied.name) is None:
        raise StatusSnapshotError("report: invalid run report filename")
    if supplied.is_symlink():
        raise StatusSnapshotError("report: expected a regular non-symlink file")
    try:
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise StatusSnapshotError(f"report: cannot resolve file: {exc}") from exc
    if not resolved.is_file() or resolved.parent != report_directory:
        raise StatusSnapshotError(
            "report: expected a regular non-symlink direct child of report_dir"
        )
    try:
        size = resolved.stat().st_size
        if size <= 0:
            raise StatusSnapshotError("report: expected a non-empty file")
        if size > MAX_REPORT_BYTES:
            raise StatusSnapshotError(
                f"report: exceeds {MAX_REPORT_BYTES} bytes"
            )
        with resolved.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
    except StatusSnapshotError:
        raise
    except OSError as exc:
        raise StatusSnapshotError(f"report: cannot read file: {exc}") from exc
    if len(raw) > MAX_REPORT_BYTES:
        raise StatusSnapshotError(f"report: exceeds {MAX_REPORT_BYTES} bytes")
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StatusSnapshotError("report: file is not strict UTF-8") from exc
    return resolved


def _load_published_status_snapshot_with_raw(
    path: os.PathLike[str] | str,
    report_dir: os.PathLike[str] | str,
) -> tuple[Mapping[str, Any], bytes, Path]:
    output_path, _report_path = _validated_output_path(
        path, report_dir, require_exists=True
    )
    document, raw = _read_strict_json(output_path)
    validated = validate_status_snapshot(document)
    expected_filename = _expected_filename(validated)
    if output_path.name != expected_filename:
        raise StatusSnapshotError(
            f"output: filename must match run_start_pt ({expected_filename})"
        )
    return validated, raw, output_path


def load_published_status_snapshot(
    path: os.PathLike[str] | str,
    report_dir: os.PathLike[str] | str = DEFAULT_REPORT_DIR,
) -> Mapping[str, Any]:
    """Read-only loader for one final snapshot directly inside report_dir.

    In addition to the complete schema, this rejects symlinks, non-regular
    files, path escapes, invalid basenames, and a basename that disagrees with
    the document's authoritative ``run_start_pt`` minute.
    """

    document, _raw, _output_path = _load_published_status_snapshot_with_raw(
        path, report_dir
    )
    return document


def _success_result(
    action: str, output_path: Path, raw: bytes
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "status_file": output_path.name,
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def verify_published_status_snapshot(
    output: os.PathLike[str] | str,
    *,
    candidate: os.PathLike[str] | str | None = None,
    scratch: os.PathLike[str] | str | None = None,
    report: os.PathLike[str] | str | None = None,
    report_dir: os.PathLike[str] | str = DEFAULT_REPORT_DIR,
    invocation_id: str | None = None,
    lifecycle_state_file: os.PathLike[str] | str = (
        run_lifecycle.DEFAULT_STATE_FILE
    ),
    lifecycle_projection_file: os.PathLike[str] | str = (
        run_lifecycle.DEFAULT_PROJECTION_FILE
    ),
) -> dict[str, Any]:
    """Read-only validation and optional lost-receipt reconciliation.

    When candidate and scratch are supplied together, the lifecycle-bound
    report is required and is read back before the final file is accepted as
    byte-identical to that valid candidate in the marked session scratch.
    Candidate-bound verification is lost-receipt recovery and therefore runs
    before lifecycle finish; it intentionally rejects a finished invocation.
    """

    if (candidate is None) != (scratch is None):
        raise StatusSnapshotError(
            "verify: candidate and scratch must be supplied together"
        )
    if candidate is not None and invocation_id is None:
        raise StatusSnapshotError(
            'verify: candidate-bound verification requires invocation_id'
        )
    if candidate is not None and report is None:
        raise StatusSnapshotError(
            'verify: candidate-bound verification requires report'
        )

    _validated, raw, output_path = _load_published_status_snapshot_with_raw(
        output, report_dir
    )
    if candidate is not None and scratch is not None:
        if not Path(candidate).is_absolute() or not Path(scratch).is_absolute():
            raise StatusSnapshotError(
                "verify: candidate and scratch must be absolute paths"
            )
        try:
            scratch_path, _marker = broker_snapshot.validate_scratch_directory(
                scratch
            )
        except (broker_snapshot.SnapshotError, OSError) as exc:
            raise StatusSnapshotError(
                f"scratch: not a preflighted broker-snapshot directory: {exc}"
            ) from exc
        supplied_candidate = Path(candidate)
        if supplied_candidate.is_symlink():
            raise StatusSnapshotError("candidate: symbolic links are not allowed")
        try:
            candidate_path = supplied_candidate.resolve(strict=True)
        except OSError as exc:
            raise StatusSnapshotError(
                f"candidate: cannot resolve file: {exc}"
            ) from exc
        if (
            candidate_path.is_symlink()
            or not candidate_path.is_file()
            or candidate_path.parent != scratch_path
            or candidate_path.name != "rhmra-status-candidate.json"
        ):
            raise StatusSnapshotError(
                "candidate: expected rhmra-status-candidate.json directly inside scratch"
            )
        candidate_document, candidate_raw = _read_strict_json(candidate_path)
        candidate_validated = validate_status_snapshot(candidate_document)
        receipt = _validate_lifecycle_binding(
            candidate_validated, output_path, invocation_id=invocation_id,
            lifecycle_state_file=lifecycle_state_file,
            lifecycle_projection_file=lifecycle_projection_file,
        )
        _validate_bound_report(
            report, report_dir, receipt['expected_report_file']
        )
        if _expected_filename(candidate_validated) != output_path.name:
            raise StatusSnapshotError(
                "candidate: run_start_pt does not match the final status filename"
            )
        if candidate_raw != raw:
            raise StatusSnapshotError(
                "output: final snapshot is not byte-identical to the candidate"
            )
    return _success_result("verify", output_path, raw)


def publish_status_snapshot(
    candidate: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    *,
    scratch: os.PathLike[str] | str,
    report: os.PathLike[str] | str,
    report_dir: os.PathLike[str] | str = DEFAULT_REPORT_DIR,
    invocation_id: str,
    lifecycle_state_file: os.PathLike[str] | str = (
        run_lifecycle.DEFAULT_STATE_FILE
    ),
    lifecycle_projection_file: os.PathLike[str] | str = (
        run_lifecycle.DEFAULT_PROJECTION_FILE
    ),
) -> dict[str, Any]:
    """Validate *candidate* and atomically publish it at *output*.

    The candidate must be a direct child of the supplied scratch directory.
    The lifecycle-bound report must already be a bounded, strict UTF-8 direct
    child of report_dir, and the output must be a new correctly named direct
    child of that directory. Existing output files are never replaced.
    """

    for label, supplied in (
        ("candidate", candidate),
        ("scratch", scratch),
    ):
        if not Path(supplied).is_absolute():
            raise StatusSnapshotError(f"{label}: expected an absolute path")

    try:
        scratch_path, _scratch_marker = broker_snapshot.validate_scratch_directory(
            scratch
        )
    except (broker_snapshot.SnapshotError, OSError) as exc:
        raise StatusSnapshotError(
            f"scratch: not a preflighted broker-snapshot directory: {exc}"
        ) from exc

    supplied_candidate = Path(candidate)
    if supplied_candidate.is_symlink():
        raise StatusSnapshotError("candidate: symbolic links are not allowed")
    try:
        candidate_path = supplied_candidate.resolve(strict=True)
    except OSError as exc:
        raise StatusSnapshotError(f"candidate: cannot resolve file: {exc}") from exc
    if (
        candidate_path.is_symlink()
        or not candidate_path.is_file()
        or candidate_path.parent != scratch_path
        or candidate_path.name != "rhmra-status-candidate.json"
    ):
        raise StatusSnapshotError(
            "candidate: expected rhmra-status-candidate.json directly inside scratch"
        )

    output_path, report_path = _validated_output_path(
        output, report_dir, require_exists=False
    )

    document, raw = _read_strict_json(candidate_path)
    validated = validate_status_snapshot(document)
    receipt = _validate_lifecycle_binding(
        validated, output_path, invocation_id=invocation_id,
        lifecycle_state_file=lifecycle_state_file,
        lifecycle_projection_file=lifecycle_projection_file,
    )
    _validate_bound_report(
        report, report_path, receipt['expected_report_file']
    )
    expected_filename = _expected_filename(validated)
    if output_path.name != expected_filename:
        raise StatusSnapshotError(
            f"output: filename must match run_start_pt ({expected_filename})"
        )

    descriptor = -1
    temporary: str | None = None
    linked = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{output_path.name}.", suffix=".tmp", dir=report_path
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        staged_document, staged_raw = _read_strict_json(temporary)
        validate_status_snapshot(staged_document)
        if staged_raw != raw:
            raise StatusSnapshotError("output: staged snapshot read-back mismatch")
        # Re-check at the commit boundary.  The first binding check happens
        # before staging; this second check prevents a lifecycle finish that
        # races staging from being followed by publication for a closed run.
        receipt = _validate_lifecycle_binding(
            staged_document, output_path, invocation_id=invocation_id,
            lifecycle_state_file=lifecycle_state_file,
            lifecycle_projection_file=lifecycle_projection_file,
        )
        _validate_bound_report(
            report, report_path, receipt['expected_report_file']
        )
        try:
            os.link(temporary, output_path)
        except FileExistsError as exc:
            raise StatusSnapshotError(
                "output: snapshot appeared concurrently; refusing to replace it"
            ) from exc
        except OSError as exc:
            raise StatusSnapshotError(
                f"output: atomic no-clobber publication failed: {exc}"
            ) from exc
        linked = True
        final_document, final_raw = _read_strict_json(output_path)
        validate_status_snapshot(final_document)
        if final_raw != raw:
            raise StatusSnapshotError("output: published snapshot read-back mismatch")
        try:
            os.unlink(temporary)
        except OSError:
            # The final hard link has already been strictly read back and is
            # authoritative.  A hidden temporary hard link that cannot be
            # removed is harmless residue, not a publication failure.
            pass
        temporary = None
    except Exception:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if linked and temporary is not None:
            try:
                if output_path.exists() and os.path.samefile(temporary, output_path):
                    os.unlink(output_path)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise

    return _success_result("publish", output_path, raw)


def _parser() -> StrictArgumentParser:
    parser = StrictArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    publish = subparsers.add_parser(
        "publish", help="validate and atomically publish one status candidate"
    )
    publish.add_argument("--invocation-id", required=True)
    publish.add_argument("--scratch", required=True)
    publish.add_argument("--candidate", required=True)
    publish.add_argument("--report", required=True)
    publish.add_argument("--output", required=True)
    publish.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help=argparse.SUPPRESS,
    )
    verify = subparsers.add_parser(
        "verify", help="read-only validation of an existing final snapshot"
    )
    verify.add_argument("--invocation-id", required=True)
    verify.add_argument("--scratch", required=True)
    verify.add_argument("--candidate", required=True)
    verify.add_argument("--report", required=True)
    verify.add_argument("--output", required=True)
    verify.add_argument(
        "--report-dir",
        default=str(DEFAULT_REPORT_DIR),
        help=argparse.SUPPRESS,
    )
    for command in (publish, verify):
        command.add_argument(
            "--lifecycle-state-file",
            default=run_lifecycle.DEFAULT_STATE_FILE,
            help=argparse.SUPPRESS,
        )
        command.add_argument(
            "--lifecycle-projection-file",
            default=run_lifecycle.DEFAULT_PROJECTION_FILE,
            help=argparse.SUPPRESS,
        )
    return parser


def _print_json(document: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _error_result(action: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": False,
        "error": {
            "code": (
                "usage_error"
                if isinstance(exc, StatusSnapshotCliError)
                else (
                    "status_snapshot_missing"
                    if isinstance(exc, StatusSnapshotMissing)
                    else "invalid_status_snapshot"
                )
            ),
            "message": str(exc),
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = (
        arguments[0]
        if arguments[:1] in (["publish"], ["verify"])
        else "unknown"
    )
    try:
        args = _parser().parse_args(arguments)
        if args.action == "publish":
            result = publish_status_snapshot(
                args.candidate,
                args.output,
                scratch=args.scratch,
                report=args.report,
                report_dir=args.report_dir,
                invocation_id=args.invocation_id,
                lifecycle_state_file=args.lifecycle_state_file,
                lifecycle_projection_file=args.lifecycle_projection_file,
            )
        else:
            result = verify_published_status_snapshot(
                args.output,
                candidate=args.candidate,
                scratch=args.scratch,
                report=args.report,
                report_dir=args.report_dir,
                invocation_id=args.invocation_id,
                lifecycle_state_file=args.lifecycle_state_file,
                lifecycle_projection_file=args.lifecycle_projection_file,
            )
        _print_json(result)
        return 0
    except (StatusSnapshotError, OSError) as exc:
        _print_json(_error_result(action, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
