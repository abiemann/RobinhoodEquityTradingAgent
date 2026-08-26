#!/usr/bin/env python3
"""Deterministically stage raw broker snapshots for ``daily_loss.py``.

The trading routine receives broker responses through an MCP transport.  This
helper removes only the known transport envelope, validates the broker payload,
and atomically writes canonical raw JSON.  It never asks an agent to transcribe,
summarize, or re-key a response.

Typical use::

    python broker_snapshot.py preflight --create-scratch

    python broker_snapshot.py bind-transport \
        --scratch C:\\path\\from\\preflight\\rhmra-session-UUID \
        --source-root C:\\path\\from\\preflight\\rhmra-source-UUID \
        --canary C:\\path\\to\\temp\\rhmra-source-UUID\\get-accounts.json \
        --account-name Agentic

The bind command must receive the exact scratch and source_root values from
the same successful create receipt.  For compatibility, an existing
caller-owned directory can still receive a scratch-only I/O preflight, but
that legacy mode does not prepare a response-source root and cannot establish
a transport binding::

    python broker_snapshot.py preflight --scratch C:\\path\\to\\session-scratch

    python broker_snapshot.py stage --kind portfolio \
        --generation A \
        --source-purpose daily-loss-a-final-portfolio-0 \
        --auto-output-scratch C:\\scratch

The unattended routine always uses ``--auto-output-scratch`` so this helper,
not model-authored JavaScript, allocates fresh direct-child output names.  The
explicit ``--output`` form remains available for compatibility and tests.

For a complete paginated response, repeat ``--source`` and
``--request-cursor`` in request order; helper allocation creates one output per
source.  The first request cursor is the literal ``FIRST``; every later value
is the cursor returned by the preceding page::

    python broker_snapshot.py stage --kind positions \
        --generation A \
        --source positions-result-1.json --source positions-result-2.json \
        --auto-output-scratch C:\\scratch \
        --request-cursor FIRST --request-cursor cursor-from-page-1

``--allow-more`` is available only for deliberately staging an incomplete
prefix while fetching more pages.  Without it, positions/orders staging proves
that the final supplied page has no continuation cursor.  Every staged payload
gets a canonical provenance sidecar.  ``daily_loss.py --snapshot-generation``
requires one complete, aggregate-sealed set per kind and rejects changed files,
broken cursor chains, scratch-session mixing, and generation A/B mixing.
Portfolio and quotes are intrinsically non-paginated.  Their staging ignores
stray cursor/allow-more arguments and always emits complete cursor-free
provenance; positions/orders retain strict cursor-chain semantics.

Accepted source shapes are deliberately narrow:

* a raw broker payload whose root contains ``data``;
* an MCP result whose ``structuredContent`` contains that raw payload; or
* an MCP result containing exactly one JSON ``content`` text block.

Every operational invocation writes exactly one JSON object to stdout.  The
script is stdlib-only and preserves JSON number precision with ``Decimal``.
A successful ``stage`` receipt keeps detailed provenance records in ``files``
and exposes their exact ordered path strings separately in ``output_paths``.
Consumers must use ``output_paths`` for argv/path handoffs; a ``files`` entry is
a descriptor object, never a pathname.

Stage failures are phase-typed: ``stage_input_failed`` means the bound source,
scratch, or prior staged input was unavailable; ``stage_response_invalid``
means the committed object was an error or not a recognized complete connector
result; ``stage_semantic_invalid`` means a valid result envelope failed
deterministic broker semantics;
``stage_binding_invalid`` means caller pagination arguments do not bind those
validated pages; ``stage_internal_failed`` means helper-owned metadata
allocation failed locally; and
``stage_write_failed`` means validated bytes could not be atomically written.
Only the semantic code is eligible for the routine's one whole-generation
retry.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit


SCHEMA_VERSION = 1
SCRATCH_MARKER = '.rhmra-broker-snapshot-scratch.json'
TRANSPORT_MARKER = '.rhmra-broker-response-transport.json'
TRANSPORT_MARKER_NAME = 'rhmra-broker-response-transport'
TRANSPORT_ATTEMPT_MARKER = '.rhmra-broker-response-transport-attempt.json'
TRANSPORT_ATTEMPT_MARKER_NAME = 'rhmra-broker-response-transport-attempt'
TRANSPORT_PREPARATION_MARKER = (
    '.rhmra-broker-response-source-root-prepared.json'
)
TRANSPORT_PREPARATION_MARKER_NAME = (
    'rhmra-broker-response-source-root-prepared'
)
TRANSPORT_ROOT_MARKER = '.rhmra-broker-response-source-root.json'
TRANSPORT_ROOT_MARKER_NAME = 'rhmra-broker-response-source-root'
TRANSPORT_KIND = 'file-change'
SOURCE_RESERVATION_MARKER_PREFIX = '.rhmra-broker-response-source-reservation-'
SOURCE_RESERVATION_MARKER_NAME = 'rhmra-broker-response-source-reservation'
SOURCE_TERMINAL_MARKER_PREFIX = '.rhmra-broker-response-source-terminal-'
SOURCE_TERMINAL_MARKER_NAME = 'rhmra-broker-response-source-terminal'
SOURCE_RESERVE_LOCK = '.rhmra-broker-response-source-reserve-lock.json'
SOURCE_RESERVE_LOCK_NAME = 'rhmra-broker-response-source-reserve-lock'
STAGE_RETRY_MARKER = '.rhmra-broker-snapshot-stage-retry.json'
STAGE_RETRY_MARKER_NAME = 'rhmra-broker-snapshot-stage-retry'
STAGE_RETRY_EXHAUSTED_MARKER = (
    '.rhmra-broker-snapshot-stage-retry-exhausted.json'
)
STAGE_RETRY_EXHAUSTED_MARKER_NAME = (
    'rhmra-broker-snapshot-stage-retry-exhausted'
)
STAGE_RETRY_REASON = 'daily-loss-semantic-invalid'
STAGE_RETRY_OUTCOMES = frozenset({'completed', 'failed'})
SOURCE_PURPOSE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,47}$')
DAILY_LOSS_PURPOSE_RE = re.compile(
    r'^daily-loss-([ab])-(?:discovery-(?:positions|orders)|marks-quotes|'
    r'final-(?:portfolio|positions|orders))-(0|[1-9][0-9]{0,2})$'
)
FIRST_POSITIONS_PURPOSE_PREFIX = 'first-positions-'
FIRST_POSITIONS_BASE_RE = re.compile(
    r'^first-positions-(0|[1-9][0-9]*)$'
)
FIRST_POSITIONS_RETRY_RE = re.compile(
    r'^first-positions-(0|[1-9][0-9]*)-retry$'
)
MAX_FIRST_POSITIONS_PAGE_COUNT = 1000
SOURCE_ABORT_REASONS = frozenset(
    {'connector-failed', 'serialization-failed', 'file-change-failed'}
)
SNAPSHOT_KINDS = ("portfolio", "positions", "orders", "quotes")
ORDER_STATES = frozenset(
    {
        "new",
        "queued",
        "confirmed",
        "unconfirmed",
        "partially_filled",
        "pending_cancelled",
        "filled",
        "cancelled",
        "rejected",
        "failed",
        "voided",
        "partially_filled_rest_cancelled",
    }
)
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
STAGE_METADATA_SUFFIX = ".rhmra-stage.json"
_WINDOWS = os.name == 'nt'
_WINDOWS_FILE_CHANGE_DIRECTORY_ACCESS = 0x001200AB
_WINDOWS_FILE_ALL_ACCESS = 0x001F01FF
_WINDOWS_OBJECT_INHERIT_ACE = 0x01
_WINDOWS_CONTAINER_INHERIT_ACE = 0x02
_WINDOWS_INHERIT_ONLY_ACE = 0x08
_WINDOWS_DACL_PROTECTED = 0x1000
_WINDOWS_ACCESS_ALLOWED_ACE_TYPE = 0


class _WindowsAclHeader(ctypes.Structure):
    _fields_ = (
        ('revision', ctypes.c_ubyte),
        ('reserved_1', ctypes.c_ubyte),
        ('size', ctypes.c_ushort),
        ('ace_count', ctypes.c_ushort),
        ('reserved_2', ctypes.c_ushort),
    )


class _WindowsAceHeader(ctypes.Structure):
    _fields_ = (
        ('ace_type', ctypes.c_ubyte),
        ('ace_flags', ctypes.c_ubyte),
        ('ace_size', ctypes.c_ushort),
    )


class SnapshotError(ValueError):
    """Raised when a source cannot prove a valid raw broker snapshot."""


class CliError(SnapshotError):
    """Raised for command-line usage errors that must remain JSON output."""


class ScratchCreateError(SnapshotError):
    """Raised when helper-owned native-temp scratch creation cannot start."""


class AccountScopeError(SnapshotError):
    """Raised when a valid accounts payload cannot bind the configured scope."""


class TransportAlreadyAttemptedError(SnapshotError):
    """Raised when another caller already owns the one-shot transport bind."""

    def __init__(
        self,
        message: str,
        *,
        marker_stable: bool = False,
        canary_instance: str | None = None,
    ) -> None:
        super().__init__(message)
        self.marker_stable = marker_stable
        self.canary_instance = canary_instance


class SourceHandoffError(SnapshotError):
    """Raised for a deterministic response-source journal violation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class StageError(SnapshotError):
    """Base class for one precisely classified staging phase failure."""

    code = "invalid_snapshot"


class StageInputError(StageError):
    """The bound source, scratch, or prior staged input was unavailable."""

    code = "stage_input_failed"


class StageSemanticError(StageError):
    """Persisted inputs failed deterministic broker-snapshot semantics."""

    code = "stage_semantic_invalid"

    def __init__(self, message: str, recovery_action: str) -> None:
        super().__init__(message)
        self.recovery_action = recovery_action


class StageResponseError(StageError):
    """The committed object was not a successful recognized connector result."""

    code = "stage_response_invalid"


class StageBindingError(StageError):
    """Caller pagination arguments do not bind the validated staged pages."""

    code = "stage_binding_invalid"


class StageInternalError(StageError):
    """Helper-owned staging metadata/allocation failed locally."""

    code = "stage_internal_failed"


class StageWriteError(StageError):
    """Validated snapshot bytes could not be atomically persisted."""

    code = "stage_write_failed"


class StageRetryStateError(StageError):
    """The invocation-wide one-generation semantic retry state is invalid."""

    code = "stage_retry_state_failed"


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError(message)


def _reject_nonfinite(token: str) -> None:
    raise SnapshotError(f"non-finite JSON constant {token!r} is not allowed")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _loads_strict(text: str, context: str) -> Any:
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_strict_object,
        )
    except SnapshotError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise SnapshotError(f"{context}: cannot parse strict JSON: {exc}") from exc


def _read_source(path: str) -> tuple[Any, bytes]:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        raise SnapshotError(f"{path}: cannot read source result: {exc}") from exc
    if raw.startswith(b"\\ufeff"):
        raise SnapshotError(
            f"{path}: source result has forbidden literal six-byte \\ufeff "
            "prefix before JSON"
        )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SnapshotError(
            f"{path}: source result has forbidden UTF-8 BOM before JSON"
        )
    framed = raw[:-1] if raw.endswith(b"\n") else raw
    if not framed.startswith(b"{") or not framed.endswith(b"}"):
        raise SnapshotError(
            f"{path}: source result must be exactly one JSON object with no "
            "leading prefix or trailing decoration; one terminal LF is allowed"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"{path}: source result is not UTF-8") from exc
    return _loads_strict(text, path), raw


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SnapshotError(f"{context}: expected an object")
    return value


def _unwrap_source(document: Any, context: str) -> tuple[Mapping[str, Any], str]:
    """Remove one known MCP envelope without transforming the API payload."""

    root = _mapping(document, context)
    if "isError" in root:
        if not isinstance(root["isError"], bool):
            raise SnapshotError(f"{context}.isError: expected a boolean")
        if root["isError"]:
            raise SnapshotError(f"{context}: broker tool result reports an error")
    if "data" in root:
        return root, "raw"

    if "structuredContent" in root:
        structured = _mapping(
            root["structuredContent"], f"{context}.structuredContent"
        )
        if "data" not in structured:
            raise SnapshotError(
                f"{context}.structuredContent: missing broker data object"
            )
        return structured, "structuredContent"

    if "content" in root:
        content = root["content"]
        if not isinstance(content, list) or len(content) != 1:
            raise SnapshotError(
                f"{context}.content: expected exactly one JSON text block"
            )
        block = _mapping(content[0], f"{context}.content[0]")
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise SnapshotError(
                f"{context}.content[0]: expected a JSON text block"
            )
        decoded = _mapping(
            _loads_strict(block["text"], f"{context}.content[0].text"),
            f"{context}.content[0].text",
        )
        if "data" in decoded:
            return decoded, "content.text"
        if "structuredContent" in decoded:
            structured = _mapping(
                decoded["structuredContent"],
                f"{context}.content[0].text.structuredContent",
            )
            if "data" not in structured:
                raise SnapshotError(
                    f"{context}.content[0].text.structuredContent: "
                    "missing broker data object"
                )
            return structured, "content.text.structuredContent"
        raise SnapshotError(
            f"{context}.content[0].text: missing broker data object"
        )

    raise SnapshotError(
        f"{context}: unrecognized result shape; expected raw data or a known "
        "MCP transport envelope"
    )


def _canonical_number(value: Decimal) -> str:
    if not value.is_finite():
        raise SnapshotError("non-finite JSON number is not allowed")
    return str(value)


def _canonical_json(value: Any) -> str:
    """Serialize strict JSON deterministically without losing number precision."""

    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return _canonical_number(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SnapshotError("JSON object keys must be strings")
        return "{" + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical_json(value[key])
            for key in sorted(value)
        ) + "}"
    raise SnapshotError(f"unsupported JSON value type {type(value).__name__}")


def _canonical_bytes(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _decimal(
    value: Any,
    context: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise SnapshotError(f"{context}: expected an exact finite decimal")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, str) and value and value == value.strip():
            number = Decimal(value)
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise SnapshotError(f"{context}: expected an exact finite decimal") from exc
    if not number.is_finite():
        raise SnapshotError(f"{context}: must be finite")
    if positive and number <= 0:
        raise SnapshotError(f"{context}: must be greater than zero")
    if nonnegative and number < 0:
        raise SnapshotError(f"{context}: must not be negative")
    return number


def _exact_sum(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal(0)
    minimum_exponent = min(value.as_tuple().exponent for value in values)
    integer_digits = max(1, max(max(value.adjusted() + 1, 0) for value in values))
    precision = integer_digits - minimum_exponent + len(str(len(values))) + 2
    with localcontext() as context:
        context.prec = max(32, precision)
        return sum(values, Decimal(0))


def _text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SnapshotError(f"{context}: expected a nonempty string")
    return value


def _symbol(value: Any, context: str) -> str:
    symbol = _text(value, context).upper()
    if any(character.isspace() for character in symbol):
        raise SnapshotError(f"{context}: symbol contains whitespace")
    return symbol


def _utc_timestamp(value: Any, context: str) -> str:
    timestamp = _text(value, context)
    if not _UTC_RE.fullmatch(timestamp):
        raise SnapshotError(f"{context}: expected an ISO-8601 UTC timestamp")
    try:
        datetime.strptime(timestamp[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise SnapshotError(f"{context}: invalid UTC timestamp") from exc
    return timestamp


def _date(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise SnapshotError(f"{context}: expected YYYY-MM-DD")
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise SnapshotError(f"{context}: invalid calendar date") from exc
    return value


def _data(payload: Mapping[str, Any], context: str) -> Mapping[str, Any]:
    return _mapping(payload.get("data"), f"{context}.data")


def _next_cursor(data: Mapping[str, Any], context: str) -> str | None:
    value = data.get("next")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value != value.strip():
        raise SnapshotError(f"{context}.next: expected a URL string or null")
    parsed = urlsplit(value)
    cursors = parse_qs(parsed.query).get("cursor", [])
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or len(cursors) != 1
        or not cursors[0]
    ):
        raise SnapshotError(f"{context}.next: expected a cursor-bearing URL")
    return cursors[0]


def _validate_portfolio(
    payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if len(payloads) != 1:
        raise SnapshotError("portfolio staging requires exactly one source")
    data = _data(payloads[0], "portfolio")
    if "total_value" not in data:
        raise SnapshotError("portfolio.data.total_value: missing")
    _decimal(data["total_value"], "portfolio.data.total_value", positive=True)
    for field in ("cash", "equity_value"):
        if field in data and data[field] is not None:
            _decimal(data[field], f"portfolio.data.{field}", nonnegative=True)
    if "buying_power" not in data:
        raise SnapshotError("portfolio.data.buying_power: missing")
    buying_power = data["buying_power"]
    if isinstance(buying_power, Mapping):
        if "buying_power" not in buying_power:
            raise SnapshotError(
                "portfolio.data.buying_power.buying_power: missing"
            )
        _decimal(
            buying_power["buying_power"],
            "portfolio.data.buying_power.buying_power",
            nonnegative=True,
        )
    else:
        # Older connector releases exposed the authoritative amount directly.
        _decimal(
            buying_power,
            "portfolio.data.buying_power",
            nonnegative=True,
        )
    return [{"row_count": None, "next_cursor": None}]


def _validate_positions(
    payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen_symbols: set[str] = set()
    metadata: list[dict[str, Any]] = []
    for page_index, payload in enumerate(payloads, 1):
        context = f"positions page {page_index}"
        data = _data(payload, context)
        rows = data.get("positions")
        if not isinstance(rows, list):
            raise SnapshotError(f"{context}.data.positions: expected an array")
        for row_index, value in enumerate(rows, 1):
            row = _mapping(value, f"{context} row {row_index}")
            symbol = _symbol(row.get("symbol"), f"{context} row {row_index}.symbol")
            if symbol in seen_symbols:
                raise SnapshotError(f"positions: duplicate position row for {symbol}")
            seen_symbols.add(symbol)
            _decimal(
                row.get("quantity"),
                f"position {symbol}.quantity",
                positive=True,
            )
            if "intraday_quantity" not in row:
                raise SnapshotError(f"position {symbol}.intraday_quantity: missing")
            _decimal(row["intraday_quantity"], f"position {symbol}.intraday_quantity")
            if row.get("type") not in {None, "long"}:
                raise SnapshotError(
                    f"position {symbol}.type: only long positions are supported"
                )
        metadata.append(
            {"row_count": len(rows), "next_cursor": _next_cursor(data, context)}
        )
    return metadata


def _validate_order_row(
    order: Mapping[str, Any], context: str
) -> tuple[str, dict[str, tuple[Any, ...]]]:
    order_id = _text(order.get("id"), f"{context}.id")
    state = order.get("state")
    if state not in ORDER_STATES:
        raise SnapshotError(f"order {order_id}.state: unrecognized order state")
    if "cumulative_quantity" not in order:
        raise SnapshotError(f"order {order_id}.cumulative_quantity: missing")
    cumulative = _decimal(
        order["cumulative_quantity"],
        f"order {order_id}.cumulative_quantity",
        nonnegative=True,
    )
    executions = order.get("executions")
    if not isinstance(executions, list):
        raise SnapshotError(f"order {order_id}.executions: expected an array")

    symbol: str | None = None
    side: str | None = None
    total = Decimal(0)
    execution_fingerprints: dict[str, tuple[Any, ...]] = {}
    for execution_index, value in enumerate(executions, 1):
        execution = _mapping(
            value, f"order {order_id}.executions[{execution_index}]"
        )
        if symbol is None:
            symbol = _symbol(order.get("symbol"), f"order {order_id}.symbol")
            side_value = order.get("side")
            if not isinstance(side_value, str) or side_value.lower() not in {
                "buy",
                "sell",
            }:
                raise SnapshotError(f"order {order_id}.side: expected buy or sell")
            side = side_value.lower()
        execution_id = _text(
            execution.get("id"),
            f"order {order_id} execution {execution_index}.id",
        )
        assert symbol is not None and side is not None
        fingerprint = (order_id, symbol, side, execution)
        prior = execution_fingerprints.get(execution_id)
        if prior is not None and prior == fingerprint:
            continue
        if prior is not None:
            raise SnapshotError('conflicting duplicate execution ID ' + execution_id)
        execution_fingerprints[execution_id] = fingerprint
        quantity = _decimal(
            execution.get("quantity"),
            f"execution {execution_id}.quantity",
            positive=True,
        )
        _decimal(
            execution.get("price"),
            f"execution {execution_id}.price",
            positive=True,
        )
        if "fees" not in execution:
            raise SnapshotError(f"execution {execution_id}.fees: missing")
        _decimal(
            execution["fees"], f"execution {execution_id}.fees", nonnegative=True
        )
        _utc_timestamp(
            execution.get("timestamp"), f"execution {execution_id}.timestamp"
        )
        total = _exact_sum((total, quantity))
    if total != cumulative:
        raise SnapshotError(
            f"order {order_id}: execution quantities do not equal "
            "cumulative_quantity"
        )
    if state in {"filled", "partially_filled", "partially_filled_rest_cancelled"} and cumulative <= 0:
        raise SnapshotError(f"order {order_id}: {state} order has no executed quantity")
    return order_id, execution_fingerprints


def _validate_orders(
    payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    orders: dict[str, Mapping[str, Any]] = {}
    executions: dict[str, tuple[Any, ...]] = {}
    metadata: list[dict[str, Any]] = []
    for page_index, payload in enumerate(payloads, 1):
        context = f"orders page {page_index}"
        data = _data(payload, context)
        rows = data.get("orders")
        if not isinstance(rows, list):
            raise SnapshotError(f"{context}.data.orders: expected an array")
        for row_index, value in enumerate(rows, 1):
            order = _mapping(value, f"{context} row {row_index}")
            order_id, order_executions = _validate_order_row(
                order, f"{context} row {row_index}"
            )
            prior_order = orders.get(order_id)
            if prior_order is not None and prior_order != order:
                raise SnapshotError(f"order {order_id}: conflicting duplicate order ID")
            orders[order_id] = order
            for execution_id, fingerprint in order_executions.items():
                prior_execution = executions.get(execution_id)
                if prior_execution is not None and prior_execution != fingerprint:
                    raise SnapshotError(
                        f"execution {execution_id}: conflicting duplicate execution ID"
                    )
                executions[execution_id] = fingerprint
        metadata.append(
            {"row_count": len(rows), "next_cursor": _next_cursor(data, context)}
        )
    return metadata


def _validate_quote_pair(quote: Mapping[str, Any], symbol: str, price: str, stamp: str) -> None:
    price_value = quote.get(price)
    stamp_value = quote.get(stamp)
    if price_value is None and stamp_value is None:
        return
    if price_value is None or stamp_value is None:
        raise SnapshotError(f"quote {symbol}: {price} and {stamp} must appear together")
    _decimal(price_value, f"quote {symbol}.{price}", positive=True)
    _utc_timestamp(stamp_value, f"quote {symbol}.{stamp}")


def _validate_quotes(
    payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen: dict[str, Mapping[str, Any]] = {}
    metadata: list[dict[str, Any]] = []
    for batch_index, payload in enumerate(payloads, 1):
        context = f"quotes batch {batch_index}"
        data = _data(payload, context)
        if 'results' not in data:
            raise SnapshotError(f'{context}.data.results: missing')
        raw_results = data.get("results")
        results = [] if raw_results is None else raw_results
        if not isinstance(results, list):
            raise SnapshotError(f"{context}.data.results: expected an array or null")
        if len(results) > 20:
            raise SnapshotError(
                f"{context}: more than 20 results can omit official closes"
            )
        for result_index, value in enumerate(results, 1):
            if value is None:
                continue
            result = _mapping(value, f"{context} result {result_index}")
            quote = _mapping(
                result.get("quote"), f"{context} result {result_index}.quote"
            )
            symbol = _symbol(
                quote.get("symbol"),
                f"{context} result {result_index}.quote.symbol",
            )
            _validate_quote_pair(
                quote, symbol, "last_trade_price", "venue_last_trade_time"
            )
            _validate_quote_pair(
                quote,
                symbol,
                "last_non_reg_trade_price",
                "venue_last_non_reg_trade_time",
            )
            if "adjusted_previous_close" in quote:
                _decimal(
                    quote["adjusted_previous_close"],
                    f"quote {symbol}.adjusted_previous_close",
                    positive=True,
                )
            close_value = result.get("close")
            if close_value is not None:
                close = _mapping(close_value, f"quote {symbol}.close")
                if close.get("symbol") is not None:
                    close_symbol = _symbol(close["symbol"], f"quote {symbol}.close.symbol")
                    if close_symbol != symbol:
                        raise SnapshotError(
                            f"quote {symbol}: close symbol is {close_symbol}"
                        )
                if "price" in close:
                    _decimal(close["price"], f"quote {symbol}.close.price", positive=True)
                if "date" in close:
                    _date(close["date"], f"quote {symbol}.close.date")
                if "interpolated" in close and not isinstance(close["interpolated"], bool):
                    raise SnapshotError(f"quote {symbol}.close.interpolated: expected a boolean")
            prior = seen.get(symbol)
            if prior is not None and prior != result:
                raise SnapshotError(f"quote {symbol}: conflicting duplicate result")
            seen[symbol] = result
        metadata.append({"row_count": len(results), "next_cursor": None})
    return metadata


def _validate_pagination(
    kind: str,
    metadata: Sequence[Mapping[str, Any]],
    request_cursors: Sequence[str] | None,
    allow_more: bool,
) -> list[str] | None:
    if kind not in {"positions", "orders"}:
        if request_cursors:
            raise SnapshotError(f"--request-cursor is not valid for {kind}")
        if allow_more:
            raise SnapshotError(f"--allow-more is not valid for {kind}")
        return None

    page_count = len(metadata)
    if request_cursors is None:
        if page_count != 1:
            raise SnapshotError(
                f"{kind}: multi-page staging requires request cursor linkage"
            )
        cursors = ["FIRST"]
    else:
        cursors = list(request_cursors)
    if page_count == 1 and len(cursors) == 1:
        _text(cursors[0], f'{kind} page 1 request cursor')
        final_next = metadata[0]['next_cursor']
        if final_next is not None and not allow_more:
            raise SnapshotError(f'{kind}: final supplied page still has a next cursor')
        if final_next is None and allow_more:
            raise SnapshotError(f'{kind}: --allow-more given but final page has no next cursor')
        return cursors
    if len(cursors) != page_count or not cursors or cursors[0] != "FIRST":
        raise SnapshotError(
            f"{kind}: request cursors must align with pages and start with FIRST"
        )

    seen_returned: set[str] = set()
    expected = "FIRST"
    for index, (request_cursor, page) in enumerate(zip(cursors, metadata), 1):
        if request_cursor != expected:
            raise SnapshotError(f"{kind} page {index}: request cursor breaks the chain")
        next_cursor = page["next_cursor"]
        if next_cursor is not None:
            if next_cursor in seen_returned:
                raise SnapshotError(f"{kind}: repeated next cursor")
            seen_returned.add(next_cursor)
        if index < page_count and next_cursor is None:
            raise SnapshotError(f"{kind} page {index}: nonfinal page has no next")
        expected = next_cursor or ""

    final_next = metadata[-1]["next_cursor"]
    if final_next is not None and not allow_more:
        raise SnapshotError(f"{kind}: final supplied page still has a next cursor")
    if final_next is None and allow_more:
        raise SnapshotError(f"{kind}: --allow-more given but final page has no next cursor")
    return cursors


def _validate_payload_documents(
    kind: str, payloads: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    if not payloads:
        raise SnapshotError("at least one --source is required")
    if kind == "portfolio":
        metadata = _validate_portfolio(payloads)
    elif kind == "positions":
        metadata = _validate_positions(payloads)
    elif kind == "orders":
        metadata = _validate_orders(payloads)
    elif kind == "quotes":
        metadata = _validate_quotes(payloads)
    else:
        raise SnapshotError(f"unsupported snapshot kind {kind!r}")
    return metadata


def _validate_payloads(
    kind: str,
    payloads: Sequence[Mapping[str, Any]],
    request_cursors: Sequence[str] | None,
    allow_more: bool,
) -> tuple[list[dict[str, Any]], list[str] | None]:
    metadata = _validate_payload_documents(kind, payloads)
    cursors = _validate_pagination(kind, metadata, request_cursors, allow_more)
    return metadata, cursors


def _scratch_marker_document(scratch_id: str) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': 'rhmra-broker-snapshot-scratch',
        'purpose': 'daily-loss-raw-broker-staging',
        'scratch_id': scratch_id,
    }


def _transport_marker_document(
    *, scratch_id: str, source_root: str, source_root_id: str,
    canary_sha256: str
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': TRANSPORT_MARKER_NAME,
        'scratch_id': scratch_id,
        'transport': TRANSPORT_KIND,
        'source_root': source_root,
        'source_root_id': source_root_id,
        'canary_sha256': canary_sha256,
    }


def _transport_attempt_marker_document(
    *, scratch_id: str, source_root: str,
    canary_instance: str | None,
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': TRANSPORT_ATTEMPT_MARKER_NAME,
        'scratch_id': scratch_id,
        'transport': TRANSPORT_KIND,
        'source_root': source_root,
        'canary_instance': canary_instance,
    }


def _transport_preparation_marker_document(
    *, scratch_id: str, source_root: str, source_root_id: str,
    source_root_identity: tuple[int, int],
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': TRANSPORT_PREPARATION_MARKER_NAME,
        'scratch_id': scratch_id,
        'transport': TRANSPORT_KIND,
        'source_root': source_root,
        'source_root_id': source_root_id,
        'source_root_device': str(source_root_identity[0]),
        'source_root_inode': str(source_root_identity[1]),
    }


def _transport_root_marker_document(
    *, scratch_id: str, source_root_id: str
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': TRANSPORT_ROOT_MARKER_NAME,
        'scratch_id': scratch_id,
        'source_root_id': source_root_id,
    }


def validate_scratch_directory(
    scratch_path: os.PathLike[str] | str,
) -> tuple[Path, Mapping[str, Any]]:
    '''Read-only validation of one successfully preflighted scratch directory.'''

    if not os.path.isabs(scratch_path):
        raise SnapshotError('scratch directory must be an absolute path')
    scratch = Path(scratch_path).resolve(strict=True)
    if not scratch.is_dir():
        raise SnapshotError(f'{scratch}: scratch path is not a directory')
    project = Path(__file__).resolve().parent
    if scratch == project or project in scratch.parents:
        raise SnapshotError('staged outputs must remain outside the project folder')
    marker_path = scratch / SCRATCH_MARKER
    marker, raw = _read_source(str(marker_path))
    marker = _mapping(marker, str(marker_path))
    if set(marker) != {
        'schema_version', 'marker', 'purpose', 'scratch_id',
    }:
        raise SnapshotError(f'{scratch}: invalid broker-snapshot scratch marker')
    scratch_id = marker.get('scratch_id')
    if (
        marker.get('schema_version') != Decimal(SCHEMA_VERSION)
        or marker.get('marker') != 'rhmra-broker-snapshot-scratch'
        or marker.get('purpose') != 'daily-loss-raw-broker-staging'
        or not isinstance(scratch_id, str)
        or _UUID_RE.fullmatch(scratch_id) is None
        or raw != _canonical_bytes(marker)
    ):
        raise SnapshotError(f'{scratch}: invalid broker-snapshot scratch marker')
    return scratch, marker


def _validated_output_scratch(
    outputs: Sequence[str],
) -> tuple[Path, Mapping[str, Any]]:
    parents = {Path(output).parent.resolve(strict=True) for output in outputs}
    if len(parents) != 1:
        raise SnapshotError('all staged outputs must share one scratch directory')
    return validate_scratch_directory(next(iter(parents)))


def _validated_transport_attempt_marker(
    scratch: Path, scratch_marker: Mapping[str, Any]
) -> Mapping[str, Any]:
    marker_path = scratch / TRANSPORT_ATTEMPT_MARKER
    document, raw = _read_source(str(marker_path))
    document = _mapping(document, str(marker_path))
    if set(document) != {
        'schema_version', 'marker', 'scratch_id', 'transport', 'source_root',
        'canary_instance',
    } or raw != _canonical_bytes(document):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response transport-attempt marker'
        )

    source_root = document.get('source_root')
    canary_instance = document.get('canary_instance')
    valid_canary_binding = canary_instance is None or (
        isinstance(canary_instance, str)
        and re.fullmatch(r'[0-9a-f]{64}', canary_instance) is not None
    )
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != TRANSPORT_ATTEMPT_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or document.get('transport') != TRANSPORT_KIND
        or not isinstance(source_root, str)
        or not os.path.isabs(source_root)
        or not valid_canary_binding
    ):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response transport-attempt marker'
        )
    return document


def _validated_transport_preparation_marker(
    scratch: Path, scratch_marker: Mapping[str, Any]
) -> Mapping[str, Any]:
    marker_path = scratch / TRANSPORT_PREPARATION_MARKER
    document, raw = _read_source(str(marker_path))
    document = _mapping(document, str(marker_path))
    if set(document) != {
        'schema_version', 'marker', 'scratch_id', 'transport',
        'source_root', 'source_root_id', 'source_root_device',
        'source_root_inode',
    } or raw != _canonical_bytes(document):
        raise SnapshotError(
            f'{marker_path}: invalid prepared broker-response source root'
        )

    source_root = document.get('source_root')
    source_root_id = document.get('source_root_id')
    source_root_device = document.get('source_root_device')
    source_root_inode = document.get('source_root_inode')
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != TRANSPORT_PREPARATION_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or document.get('transport') != TRANSPORT_KIND
        or not isinstance(source_root, str)
        or not os.path.isabs(source_root)
        or not isinstance(source_root_id, str)
        or _UUID_RE.fullmatch(source_root_id) is None
        or not isinstance(source_root_device, str)
        or re.fullmatch(r'(?:0|[1-9][0-9]*)', source_root_device) is None
        or not isinstance(source_root_inode, str)
        or re.fullmatch(r'(?:0|[1-9][0-9]*)', source_root_inode) is None
    ):
        raise SnapshotError(
            f'{marker_path}: invalid prepared broker-response source root'
        )

    source_root_path = Path(source_root)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    project = Path(__file__).resolve().parent
    if (
        str(source_root_path) != source_root
        or source_root_path.parent != temp_root
        or not source_root_path.name.startswith('rhmra-source-')
        or source_root_path == project
        or project in source_root_path.parents
        or source_root_path in project.parents
    ):
        raise SnapshotError(
            f'{marker_path}: invalid prepared broker-response source root'
        )
    return document


def _validated_prepared_source_root(
    preparation: Mapping[str, Any],
) -> Path:
    source_root = Path(preparation['source_root'])
    try:
        current = os.lstat(source_root)
    except OSError as exc:
        raise SnapshotError(
            f'{source_root}: cannot inspect prepared response-source root: {exc}'
        ) from exc
    is_junction = getattr(source_root, 'is_junction', lambda: False)
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or is_junction()
        or source_root.resolve(strict=True) != source_root
        or str(current.st_dev) != preparation['source_root_device']
        or str(current.st_ino) != preparation['source_root_inode']
    ):
        raise SnapshotError(
            f'{source_root}: prepared response-source root instance changed'
        )
    return source_root


def _validated_transport_marker(
    scratch: Path, scratch_marker: Mapping[str, Any]
) -> Mapping[str, Any]:
    preparation = _validated_transport_preparation_marker(
        scratch, scratch_marker
    )
    prepared_source_root = _validated_prepared_source_root(preparation)
    attempt = _validated_transport_attempt_marker(scratch, scratch_marker)
    marker_path = scratch / TRANSPORT_MARKER
    document, raw = _read_source(str(marker_path))
    document = _mapping(document, str(marker_path))
    if set(document) != {
        'schema_version', 'marker', 'scratch_id', 'transport',
        'source_root', 'source_root_id', 'canary_sha256',
    } or raw != _canonical_bytes(document):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response transport marker'
        )

    source_root = document.get('source_root')
    source_root_id = document.get('source_root_id')
    canary_sha256 = document.get('canary_sha256')
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != TRANSPORT_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or document.get('transport') != TRANSPORT_KIND
        or not isinstance(source_root, str)
        or not os.path.isabs(source_root)
        or source_root != attempt['source_root']
        or attempt['canary_instance'] is None
        or source_root != preparation['source_root']
        or not isinstance(source_root_id, str)
        or _UUID_RE.fullmatch(source_root_id) is None
        or source_root_id != preparation['source_root_id']
        or not isinstance(canary_sha256, str)
        or re.fullmatch(r'[0-9a-f]{64}', canary_sha256) is None
    ):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response transport marker'
        )

    source_root_path = Path(source_root)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if (
        source_root_path != prepared_source_root
        or source_root_path.is_symlink()
        or source_root_path.resolve(strict=True) != source_root_path
        or source_root_path.parent != temp_root
        or not source_root_path.is_dir()
    ):
        raise SnapshotError(f'{marker_path}: invalid bound response-source root')
    root_marker = _validated_transport_root_marker(source_root_path, scratch_marker)
    if root_marker['source_root_id'] != source_root_id:
        raise SnapshotError(f'{marker_path}: response-source root instance changed')
    return document


def _validated_transport_root_marker(
    source_root: Path, scratch_marker: Mapping[str, Any]
) -> Mapping[str, Any]:
    marker_path = source_root / TRANSPORT_ROOT_MARKER
    document, raw = _read_source(str(marker_path))
    document = _mapping(document, str(marker_path))
    if set(document) != {
        'schema_version', 'marker', 'scratch_id', 'source_root_id',
    } or raw != _canonical_bytes(document):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response source-root marker'
        )
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != TRANSPORT_ROOT_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or not isinstance(document.get('source_root_id'), str)
        or _UUID_RE.fullmatch(document['source_root_id']) is None
    ):
        raise SnapshotError(
            f'{marker_path}: invalid broker-response source-root marker'
        )
    return document


def _validated_source_purpose(value: Any, context: str = '--purpose') -> str:
    if not isinstance(value, str) or SOURCE_PURPOSE_RE.fullmatch(value) is None:
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{context}: expected 1-48 lowercase letters, digits, or hyphens, '
            'beginning with a letter or digit',
        )
    return value


def _daily_loss_purpose_binding(
    purpose: str,
) -> tuple[str, str] | None:
    match = DAILY_LOSS_PURPOSE_RE.fullmatch(purpose)
    if match is None:
        if purpose.startswith('daily-loss-'):
            raise SourceHandoffError(
                'source_purpose_invalid',
                f'{purpose}: DAILY-LOSS purpose must use one canonical '
                'generation/phase/kind/index combination',
            )
        return None
    parts = purpose.split('-')
    generation = 'A' if match.group(1) == 'a' else 'B'
    kind = parts[-2]
    return generation, kind


def _validated_first_request_cursor_chain(
    values: Sequence[str] | None, purpose: str
) -> tuple[str, ...] | None:
    first_match = (
        FIRST_POSITIONS_BASE_RE.fullmatch(purpose)
        or FIRST_POSITIONS_RETRY_RE.fullmatch(purpose)
    )
    if first_match is None:
        if values:
            raise SourceHandoffError(
                'source_purpose_invalid',
                '--first-request-cursor is valid only for a FIRST positions purpose',
            )
        return None
    if not values:
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST reservation requires its complete request-cursor chain',
        )
    cursors = tuple(values)
    if any(
        not isinstance(value, str)
        or not value
        or value != value.strip()
        for value in cursors
    ):
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST request cursors must be nonempty exact strings',
        )
    if cursors[0] != 'FIRST' or len(set(cursors)) != len(cursors):
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST cursor chain must start with FIRST and be unique',
        )
    if len(cursors) > MAX_FIRST_POSITIONS_PAGE_COUNT:
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST cursor chain exceeds the '
            f'{MAX_FIRST_POSITIONS_PAGE_COUNT}-page limit',
        )
    index_match = re.search(r'first-positions-([0-9]+)', purpose)
    if index_match is None or int(index_match.group(1)) != len(cursors) - 1:
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST page index must equal the zero-based cursor position',
        )
    return cursors


def _first_request_cursors_sha256(cursors: Sequence[str]) -> str:
    return _sha256(_canonical_bytes(list(cursors)))


def _source_purpose_key(purpose: str) -> str:
    return hashlib.sha256(purpose.encode('ascii')).hexdigest()


def _source_reservation_marker_path(scratch: Path, purpose: str) -> Path:
    return scratch / (
        SOURCE_RESERVATION_MARKER_PREFIX + _source_purpose_key(purpose) + '.json'
    )


def _source_terminal_marker_path(scratch: Path, purpose: str) -> Path:
    return scratch / (
        SOURCE_TERMINAL_MARKER_PREFIX + _source_purpose_key(purpose) + '.json'
    )


def _source_filename(reservation_id: str) -> str:
    return f'rhmra-source-{reservation_id}.json'


def _source_reservation_document(
    *, scratch_id: str, source_root_id: str, purpose: str,
    reservation_id: str,
    first_request_cursors: Sequence[str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': SOURCE_RESERVATION_MARKER_NAME,
        'scratch_id': scratch_id,
        'source_root_id': source_root_id,
        'purpose': purpose,
        'reservation_id': reservation_id,
        'source_filename': _source_filename(reservation_id),
    }
    if first_request_cursors is not None:
        document.update({
            'first_request_cursor_count': Decimal(len(first_request_cursors)),
            'first_request_cursors_sha256': _first_request_cursors_sha256(
                first_request_cursors
            ),
        })
    return document


def _source_committed_document(
    *, reservation: Mapping[str, Any], source_sha256: str,
    source_identity: Mapping[str, str],
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': SOURCE_TERMINAL_MARKER_NAME,
        'scratch_id': reservation['scratch_id'],
        'source_root_id': reservation['source_root_id'],
        'purpose': reservation['purpose'],
        'reservation_id': reservation['reservation_id'],
        'source_filename': reservation['source_filename'],
        'status': 'committed',
        'source_sha256': source_sha256,
        'source_device': source_identity['device'],
        'source_inode': source_identity['inode'],
        'source_size': source_identity['size'],
        'source_mtime_ns': source_identity['mtime_ns'],
        'source_ctime_ns': source_identity['ctime_ns'],
    }


def _source_aborted_document(
    *, reservation: Mapping[str, Any], reason: str,
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': SOURCE_TERMINAL_MARKER_NAME,
        'scratch_id': reservation['scratch_id'],
        'source_root_id': reservation['source_root_id'],
        'purpose': reservation['purpose'],
        'reservation_id': reservation['reservation_id'],
        'source_filename': reservation['source_filename'],
        'status': 'aborted',
        'reason': reason,
    }


def _journal_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _source_stat_document(value: os.stat_result) -> dict[str, str]:
    return {
        'device': str(value.st_dev),
        'inode': str(value.st_ino),
        'size': str(value.st_size),
        'mtime_ns': str(value.st_mtime_ns),
        'ctime_ns': str(value.st_ctime_ns),
    }


def _read_immutable_journal_marker(
    path: Path, scratch: Path,
) -> tuple[Mapping[str, Any], bytes]:
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: cannot inspect source-handoff journal marker: {exc}',
        ) from exc
    if (
        path.parent != scratch
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: source-handoff journal marker must be an immutable '
            'non-symlink regular direct child of scratch',
        )
    try:
        if path.resolve(strict=True) != path:
            raise SourceHandoffError(
                'source_journal_invalid',
                f'{path}: source-handoff journal marker path changed',
            )
        document, raw = _read_source(str(path))
        after = os.lstat(path)
    except SourceHandoffError:
        raise
    except (OSError, SnapshotError) as exc:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: cannot read source-handoff journal marker: {exc}',
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or _journal_stat_identity(before) != _journal_stat_identity(after)
    ):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: source-handoff journal marker changed while being read',
        )
    try:
        document = _mapping(document, str(path))
        canonical = _canonical_bytes(document)
    except SnapshotError as exc:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid source-handoff journal marker: {exc}',
        ) from exc
    if raw != canonical:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: source-handoff journal marker is not canonical JSON',
        )
    return document, raw


def _write_immutable_journal_marker(
    path: Path, document: Mapping[str, Any], scratch: Path,
) -> None:
    raw = _canonical_bytes(document)
    descriptor = -1
    temporary = ''
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f'.{path.name}.', suffix='.tmp', dir=str(scratch)
        )
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise SourceHandoffError(
                    'source_journal_write_failed',
                    f'{path}: incomplete immutable source-handoff marker write',
                )
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        staged, staged_raw = _read_source(temporary)
        if staged != document or staged_raw != raw:
            raise SourceHandoffError(
                'source_journal_write_failed',
                f'{path}: staged source-handoff marker read-back mismatch',
            )
        os.link(temporary, path)
        try:
            persisted, persisted_raw = _read_immutable_journal_marker(
                path, scratch
            )
        except Exception as exc:
            raise SourceHandoffError(
                'source_journal_write_failed',
                f'{path}: immutable source-handoff marker was committed but '
                'could not be verified',
            ) from exc
        if persisted != document or persisted_raw != raw:
            raise SourceHandoffError(
                'source_journal_write_failed',
                f'{path}: immutable source-handoff marker read-back mismatch',
            )
    except FileExistsError:
        raise
    except SourceHandoffError:
        raise
    except OSError as exc:
        raise SourceHandoffError(
            'source_journal_write_failed',
            f'{path}: cannot atomically commit source-handoff marker: {exc}',
        ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _source_reserve_lock_document(
    *, scratch_id: str, lock_id: str,
) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': SOURCE_RESERVE_LOCK_NAME,
        'scratch_id': scratch_id,
        'lock_id': lock_id,
    }


def _validated_source_reserve_lock(
    scratch: Path, scratch_marker: Mapping[str, Any],
) -> Mapping[str, Any]:
    path = scratch / SOURCE_RESERVE_LOCK
    document, _raw = _read_immutable_journal_marker(path, scratch)
    if set(document) != {
        'schema_version', 'marker', 'scratch_id', 'lock_id',
    } or (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != SOURCE_RESERVE_LOCK_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or not isinstance(document.get('lock_id'), str)
        or _UUID_RE.fullmatch(document['lock_id']) is None
    ):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid source-reservation lock marker',
        )
    return document


def _acquire_source_reserve_lock(
    scratch: Path, scratch_marker: Mapping[str, Any],
) -> Mapping[str, Any]:
    path = scratch / SOURCE_RESERVE_LOCK
    document = _source_reserve_lock_document(
        scratch_id=scratch_marker['scratch_id'], lock_id=str(uuid.uuid4())
    )
    try:
        _write_immutable_journal_marker(path, document, scratch)
    except FileExistsError as exc:
        _validated_source_reserve_lock(scratch, scratch_marker)
        raise SourceHandoffError(
            'source_journal_busy',
            'another source reservation is active or was interrupted; '
            'do not start another broker call',
        ) from exc
    return document


def _release_source_reserve_lock(
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> None:
    path = scratch / SOURCE_RESERVE_LOCK
    persisted = _validated_source_reserve_lock(scratch, scratch_marker)
    if persisted != expected:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: source-reservation lock ownership changed',
        )
    try:
        os.unlink(path)
    except OSError as exc:
        raise SourceHandoffError(
            'source_journal_write_failed',
            f'{path}: cannot release source-reservation lock: {exc}',
        ) from exc


def _reject_pending_source_handoff(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
) -> None:
    reservation_re = re.compile(
        '^' + re.escape(SOURCE_RESERVATION_MARKER_PREFIX)
        + r'[0-9a-f]{64}\.json$'
    )
    terminal_re = re.compile(
        '^' + re.escape(SOURCE_TERMINAL_MARKER_PREFIX)
        + r'[0-9a-f]{64}\.json$'
    )
    reservations: dict[str, Mapping[str, Any]] = {}
    terminal_keys: set[str] = set()
    try:
        entries = list(scratch.iterdir())
    except OSError as exc:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{scratch}: cannot enumerate source-handoff journal: {exc}',
        ) from exc
    for entry in entries:
        if reservation_re.fullmatch(entry.name):
            reservation = _validated_source_reservation_at_path(
                entry,
                scratch=scratch,
                scratch_marker=scratch_marker,
                transport_marker=transport_marker,
            )
            key = _source_purpose_key(reservation['purpose'])
            if key in reservations:
                raise SourceHandoffError(
                    'source_journal_invalid',
                    f'{entry}: duplicate source-reservation purpose hash',
                )
            reservations[key] = reservation
        elif terminal_re.fullmatch(entry.name):
            terminal_keys.add(
                entry.name[len(SOURCE_TERMINAL_MARKER_PREFIX):-5]
            )
    if terminal_keys - set(reservations):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{scratch}: source journal has an orphan terminal marker',
        )
    for key, reservation in reservations.items():
        terminal = _validated_source_terminal(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            reservation=reservation,
        )
        if terminal is None:
            raise SourceHandoffError(
                'source_handoff_pending',
                f"{reservation['purpose']}: an earlier source handoff is "
                'still pending; do not start another broker call',
            )
        source_root = Path(transport_marker['source_root'])
        source_path = source_root / reservation['source_filename']
        if terminal['status'] == 'aborted' and os.path.lexists(source_path):
            raise SourceHandoffError(
                'source_file_invalid',
                f'{source_path}: aborted source unexpectedly exists',
            )


def _validated_source_reservation_at_path(
    path: Path,
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    expected_purpose: str | None = None,
) -> Mapping[str, Any]:
    document, _raw = _read_immutable_journal_marker(path, scratch)
    base_fields = {
        'schema_version', 'marker', 'scratch_id', 'source_root_id', 'purpose',
        'reservation_id', 'source_filename',
    }
    purpose = document.get('purpose')
    first_purpose = (
        isinstance(purpose, str)
        and purpose.startswith(FIRST_POSITIONS_PURPOSE_PREFIX)
    )
    expected_fields = set(base_fields)
    if first_purpose:
        expected_fields.update({
            'first_request_cursor_count', 'first_request_cursors_sha256',
        })
    if set(document) != expected_fields:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid source-reservation marker fields',
        )
    reservation_id = document.get('reservation_id')
    source_filename = document.get('source_filename')
    first_cursor_count = document.get('first_request_cursor_count')
    first_cursor_sha256 = document.get('first_request_cursors_sha256')
    canonical_first_match = (
        FIRST_POSITIONS_BASE_RE.fullmatch(purpose)
        or FIRST_POSITIONS_RETRY_RE.fullmatch(purpose)
        if isinstance(purpose, str)
        else None
    )
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != SOURCE_RESERVATION_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or document.get('source_root_id') != transport_marker['source_root_id']
        or not isinstance(purpose, str)
        or SOURCE_PURPOSE_RE.fullmatch(purpose) is None
        or (expected_purpose is not None and purpose != expected_purpose)
        or not isinstance(reservation_id, str)
        or _UUID_RE.fullmatch(reservation_id) is None
        or source_filename != _source_filename(reservation_id)
        or path != _source_reservation_marker_path(scratch, purpose)
        or (
            first_purpose
            and (
                canonical_first_match is None
                or isinstance(first_cursor_count, bool)
                or not isinstance(first_cursor_count, Decimal)
                or first_cursor_count != first_cursor_count.to_integral_value()
                or first_cursor_count < 1
                or first_cursor_count > MAX_FIRST_POSITIONS_PAGE_COUNT
                or int(canonical_first_match.group(1))
                != int(first_cursor_count) - 1
                or not isinstance(first_cursor_sha256, str)
                or re.fullmatch(r'[0-9a-f]{64}', first_cursor_sha256) is None
            )
        )
    ):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid or foreign source-reservation marker',
        )
    return document


def _validated_source_reservation(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    purpose: str,
) -> Mapping[str, Any]:
    path = _source_reservation_marker_path(scratch, purpose)
    if not os.path.lexists(path):
        terminal_path = _source_terminal_marker_path(scratch, purpose)
        if os.path.lexists(terminal_path):
            raise SourceHandoffError(
                'source_journal_invalid',
                f'{terminal_path}: orphan source-terminal marker',
            )
        raise SourceHandoffError(
            'source_reservation_missing',
            f'{purpose}: no source handoff was reserved',
        )
    return _validated_source_reservation_at_path(
        path,
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        expected_purpose=purpose,
    )


def _validated_source_terminal(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    reservation: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    purpose = reservation['purpose']
    path = _source_terminal_marker_path(scratch, purpose)
    if not os.path.lexists(path):
        return None
    document, _raw = _read_immutable_journal_marker(path, scratch)
    status_value = document.get('status')
    common = {
        'schema_version', 'marker', 'scratch_id', 'source_root_id', 'purpose',
        'reservation_id', 'source_filename', 'status',
    }
    if status_value == 'committed':
        expected_fields = common | {
            'source_sha256', 'source_device', 'source_inode', 'source_size',
            'source_mtime_ns', 'source_ctime_ns',
        }
    elif status_value == 'aborted':
        expected_fields = common | {'reason'}
    else:
        expected_fields = set()
    if set(document) != expected_fields:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid source-terminal marker fields',
        )
    if (
        document.get('schema_version') != Decimal(SCHEMA_VERSION)
        or document.get('marker') != SOURCE_TERMINAL_MARKER_NAME
        or document.get('scratch_id') != scratch_marker['scratch_id']
        or document.get('source_root_id') != transport_marker['source_root_id']
        or document.get('purpose') != purpose
        or document.get('reservation_id') != reservation['reservation_id']
        or document.get('source_filename') != reservation['source_filename']
        or path != _source_terminal_marker_path(scratch, purpose)
    ):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid or foreign source-terminal marker',
        )
    if status_value == 'committed':
        if (
            not isinstance(document.get('source_sha256'), str)
            or re.fullmatch(r'[0-9a-f]{64}', document['source_sha256']) is None
            or any(
                not isinstance(document.get(field), str)
                or re.fullmatch(r'(?:0|[1-9][0-9]*)', document[field]) is None
                for field in (
                    'source_device', 'source_inode', 'source_size',
                    'source_mtime_ns', 'source_ctime_ns',
                )
            )
        ):
            raise SourceHandoffError(
                'source_journal_invalid',
                f'{path}: invalid committed source identity',
            )
    elif document.get('reason') not in SOURCE_ABORT_REASONS:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{path}: invalid source-abort reason',
        )
    return document


def _validated_source_journal_context(
    scratch_path: os.PathLike[str] | str,
) -> tuple[Path, Mapping[str, Any], Mapping[str, Any], Path]:
    scratch, scratch_marker = validate_scratch_directory(scratch_path)
    transport_marker = _validated_transport_marker(scratch, scratch_marker)
    source_root = Path(transport_marker['source_root'])
    return scratch, scratch_marker, transport_marker, source_root


def _read_reserved_source(
    source_root: Path, reservation: Mapping[str, Any],
) -> tuple[Path, Any, bytes, Mapping[str, str]]:
    source_path = source_root / reservation['source_filename']
    try:
        before = os.lstat(source_path)
    except OSError as exc:
        raise SourceHandoffError(
            'source_file_missing',
            f'{source_path}: reserved source file is unavailable: {exc}',
        ) from exc
    try:
        resolved = source_path.resolve(strict=True)
    except OSError as exc:
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_path}: cannot resolve reserved source file: {exc}',
        ) from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or resolved != source_path
        or resolved.parent != source_root
    ):
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_path}: reserved source must be a non-symlink regular '
            'direct child of the bound response-source root',
        )
    try:
        document, raw = _read_source(str(source_path))
        after = os.lstat(source_path)
    except SnapshotError as exc:
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_path}: reserved source is not strict JSON: {exc}',
        ) from exc
    except OSError as exc:
        raise SourceHandoffError(
            'source_file_changed',
            f'{source_path}: reserved source changed while being read: {exc}',
        ) from exc
    if (
        stat.S_ISLNK(after.st_mode)
        or not stat.S_ISREG(after.st_mode)
        or _journal_stat_identity(before) != _journal_stat_identity(after)
    ):
        raise SourceHandoffError(
            'source_file_changed',
            f'{source_path}: reserved source changed while being read',
        )
    return source_path, document, raw, _source_stat_document(after)


def _verify_committed_source(
    source_root: Path,
    reservation: Mapping[str, Any],
    terminal: Mapping[str, Any],
) -> tuple[Path, Any, bytes]:
    if terminal.get('status') != 'committed':
        raise SourceHandoffError(
            'source_handoff_aborted',
            f"{reservation['purpose']}: source handoff was aborted",
        )
    source_path, document, raw, identity = _read_reserved_source(
        source_root, reservation
    )
    expected_identity = {
        'device': terminal['source_device'],
        'inode': terminal['source_inode'],
        'size': terminal['source_size'],
        'mtime_ns': terminal['source_mtime_ns'],
        'ctime_ns': terminal['source_ctime_ns'],
    }
    if _sha256(raw) != terminal['source_sha256'] or identity != expected_identity:
        raise SourceHandoffError(
            'source_file_changed',
            f'{source_path}: committed source identity or content changed',
        )
    return source_path, document, raw


def _validated_bound_source_path(
    source_arg: str, bound_source_root: Path
) -> Path:
    if not os.path.isabs(source_arg):
        raise SourceHandoffError(
            'source_file_invalid',
            'broker-response source must be an absolute path',
        )
    source_input = Path(source_arg)
    try:
        source_stat = os.lstat(source_input)
    except OSError as exc:
        raise SourceHandoffError(
            'source_file_missing',
            f'{source_input}: cannot inspect broker-response source: {exc}'
        ) from exc
    try:
        source_path = source_input.resolve(strict=True)
    except OSError as exc:
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_input}: cannot resolve broker-response source: {exc}',
        ) from exc
    if (
        stat.S_ISLNK(source_stat.st_mode)
        or not stat.S_ISREG(source_stat.st_mode)
        or source_path.parent != bound_source_root
    ):
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_arg}: external source must be a non-symlink regular '
            'direct child of the invocation-bound response-source root'
        )
    return source_path


def _reservation_for_bound_source_path(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_path: Path,
) -> Mapping[str, Any]:
    matches: list[Mapping[str, Any]] = []
    marker_name_re = re.compile(
        '^' + re.escape(SOURCE_RESERVATION_MARKER_PREFIX)
        + r'[0-9a-f]{64}\.json$'
    )
    try:
        entries = sorted(scratch.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{scratch}: cannot enumerate source-handoff journal: {exc}',
        ) from exc
    for entry in entries:
        if marker_name_re.fullmatch(entry.name) is None:
            continue
        reservation = _validated_source_reservation_at_path(
            entry,
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
        )
        if reservation['source_filename'] == source_path.name:
            matches.append(reservation)
    if not matches:
        raise SourceHandoffError(
            'source_unregistered',
            f'{source_path}: source was not reserved by the deterministic '
            'handoff journal',
        )
    if len(matches) != 1:
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{source_path}: source has multiple journal reservations',
        )
    return matches[0]


def _validated_committed_source_for_path(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    source_arg: str,
) -> tuple[Path, Any, bytes]:
    source_path = _validated_bound_source_path(source_arg, source_root)
    reservation = _reservation_for_bound_source_path(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_path=source_path,
    )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    if terminal is None:
        # The file exists and is validly shaped, but an immutable commit marker
        # has not sealed its bytes and identity.  Consumers must never infer
        # commitment merely from the path's presence.
        _read_reserved_source(source_root, reservation)
        raise SourceHandoffError(
            'source_commit_required',
            f"{reservation['purpose']}: reserved source exists but is not "
            'committed',
        )
    return _verify_committed_source(source_root, reservation, terminal)


def _validated_committed_source_for_purpose(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    purpose: str,
) -> tuple[Path, Any, bytes]:
    purpose = _validated_source_purpose(purpose, 'source purpose')
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=purpose,
    )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    source_path = source_root / reservation['source_filename']
    if terminal is None:
        if os.path.lexists(source_path):
            _read_reserved_source(source_root, reservation)
            raise SourceHandoffError(
                'source_commit_required',
                f'{purpose}: reserved source exists but is not committed',
            )
        raise SourceHandoffError(
            'source_handoff_pending',
            f'{purpose}: source handoff is reserved but has no file or terminal '
            'outcome; the broker call must not be repeated',
        )
    if terminal['status'] == 'aborted':
        if os.path.lexists(source_path):
            raise SourceHandoffError(
                'source_file_invalid',
                f'{source_path}: aborted source unexpectedly exists',
            )
        raise SourceHandoffError(
            'source_handoff_aborted',
            f"{purpose}: source handoff was aborted ({terminal['reason']})",
        )
    return _verify_committed_source(source_root, reservation, terminal)


def validate_bound_external_json_source(
    scratch_path: os.PathLike[str] | str,
    source_path: os.PathLike[str] | str,
) -> tuple[Path, Any, bytes]:
    '''Read one strict-JSON source only if it belongs to the bound temp root.

    The returned tuple is ``(resolved_path, parsed_document, original_bytes)``.
    Callers can consume those exact bytes without reopening the path after its
    transport provenance has been checked.
    '''

    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    return _validated_committed_source_for_path(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        source_arg=os.fspath(source_path),
    )


def validate_bound_external_json_sources(
    scratch_path: os.PathLike[str] | str,
    source_paths: Sequence[os.PathLike[str] | str],
) -> list[tuple[Path, Any, bytes]]:
    '''Validate and read several external JSON files against one binding.'''

    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    return [
        _validated_committed_source_for_path(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            source_arg=os.fspath(path),
        )
        for path in source_paths
    ]


def validate_bound_external_json_purpose(
    scratch_path: os.PathLike[str] | str,
    purpose: str,
) -> tuple[Path, Any, bytes]:
    '''Read the immutable committed source registered for one logical purpose.'''

    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    return _validated_committed_source_for_purpose(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        purpose=purpose,
    )


def validate_bound_external_json_purposes(
    scratch_path: os.PathLike[str] | str,
    purposes: Sequence[str],
) -> list[tuple[Path, Any, bytes]]:
    '''Read committed sources for several purposes under one binding.'''

    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    return [
        _validated_committed_source_for_purpose(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            purpose=purpose,
        )
        for purpose in purposes
    ]


def _validate_source_retry_authorization_in_context(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    base_purpose: str,
    retry_purpose: str,
) -> Mapping[str, Any]:
    base = _validated_source_purpose(base_purpose, 'base source purpose')
    retry = _validated_source_purpose(retry_purpose, 'retry source purpose')
    if retry != base + '-retry':
        raise SourceHandoffError(
            'source_retry_not_authorized',
            f'{retry}: retry purpose must exactly equal {base}-retry',
        )
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=base,
    )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    source_path = source_root / reservation['source_filename']
    if terminal is None:
        if os.path.lexists(source_path):
            _read_reserved_source(source_root, reservation)
            raise SourceHandoffError(
                'source_commit_required',
                f'{base}: reserved source exists but is not committed',
            )
        raise SourceHandoffError(
            'source_handoff_pending',
            f'{base}: source handoff has no terminal outcome',
        )
    if terminal['status'] != 'aborted' or terminal.get('reason') != 'connector-failed':
        raise SourceHandoffError(
            'source_retry_not_authorized',
            f'{base}: retry requires an aborted connector-failed handoff',
        )
    if os.path.lexists(source_path):
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_path}: aborted source unexpectedly exists',
        )
    return reservation


def validate_bound_source_retry_authorization(
    scratch_path: os.PathLike[str] | str,
    base_purpose: str,
    retry_purpose: str,
) -> None:
    '''Prove that one ``-retry`` source follows an aborted connector read.

    The retry name alone is not authority.  Its exact base reservation must
    have reached the immutable ``aborted`` / ``connector-failed`` outcome and
    must still have no response file.  Reservation calls enforce this before
    the retry broker read; consumers repeat it as defense in depth.
    '''

    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    base_reservation = _validate_source_retry_authorization_in_context(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        base_purpose=base_purpose,
        retry_purpose=retry_purpose,
    )
    retry_reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=retry_purpose,
    )
    if (
        retry_reservation.get('first_request_cursor_count')
        != base_reservation.get('first_request_cursor_count')
        or retry_reservation.get('first_request_cursors_sha256')
        != base_reservation.get('first_request_cursors_sha256')
    ):
        raise SourceHandoffError(
            'source_retry_not_authorized',
            f'{retry_purpose}: retry cursor binding differs from its base',
        )


def _raise_first_request_binding(message: str) -> None:
    raise SourceHandoffError('request_binding_invalid', message)


def _validate_first_reservation_cursor_binding(
    reservation: Mapping[str, Any], request_cursors: Sequence[str]
) -> None:
    purpose = reservation.get('purpose')
    if (
        reservation.get('first_request_cursor_count')
        != Decimal(len(request_cursors))
        or reservation.get('first_request_cursors_sha256')
        != _first_request_cursors_sha256(request_cursors)
    ):
        _raise_first_request_binding(
            f'{purpose}: immutable FIRST reservation does not bind the '
            'submitted request-cursor chain'
        )


def _validated_committed_first_page_in_context(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    page_index: int,
    request_cursors: Sequence[str],
) -> tuple[str, Any]:
    base_purpose = f'first-positions-{page_index}'
    try:
        base_reservation = _validated_source_reservation(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            purpose=base_purpose,
        )
    except SourceHandoffError as exc:
        if exc.code == 'source_reservation_missing':
            _raise_first_request_binding(
                f'{base_purpose}: prior FIRST page was not reserved and '
                'committed before the next page'
            )
        raise
    prefix = tuple(request_cursors[:page_index + 1])
    _validate_first_reservation_cursor_binding(base_reservation, prefix)
    base_terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=base_reservation,
    )
    if base_terminal is None:
        raise SourceHandoffError(
            'source_handoff_pending',
            f'{base_purpose}: prior FIRST page has no terminal outcome',
        )
    if base_terminal['status'] == 'committed':
        _path, document, _raw = _verify_committed_source(
            source_root, base_reservation, base_terminal
        )
        return base_purpose, document
    if base_terminal.get('reason') != 'connector-failed':
        _raise_first_request_binding(
            f'{base_purpose}: prior FIRST page did not produce a committed '
            'positions response'
        )
    base_source = source_root / base_reservation['source_filename']
    if os.path.lexists(base_source):
        raise SourceHandoffError(
            'source_file_invalid',
            f'{base_source}: aborted source unexpectedly exists',
        )

    retry_purpose = base_purpose + '-retry'
    _validate_source_retry_authorization_in_context(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        base_purpose=base_purpose,
        retry_purpose=retry_purpose,
    )
    try:
        retry_reservation = _validated_source_reservation(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            purpose=retry_purpose,
        )
    except SourceHandoffError as exc:
        if exc.code == 'source_reservation_missing':
            _raise_first_request_binding(
                f'{retry_purpose}: prior FIRST retry was not committed before '
                'the next page'
            )
        raise
    _validate_first_reservation_cursor_binding(retry_reservation, prefix)
    retry_terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=retry_reservation,
    )
    if retry_terminal is None:
        raise SourceHandoffError(
            'source_handoff_pending',
            f'{retry_purpose}: prior FIRST retry has no terminal outcome',
        )
    if retry_terminal['status'] != 'committed':
        _raise_first_request_binding(
            f'{retry_purpose}: prior FIRST retry did not produce a committed '
            'positions response'
        )
    _path, document, _raw = _verify_committed_source(
        source_root, retry_reservation, retry_terminal
    )
    return retry_purpose, document


def _validate_first_prior_page_chain_in_context(
    *,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    request_cursors: Sequence[str],
) -> None:
    prior_count = len(request_cursors) - 1
    if prior_count <= 0:
        return
    payloads: list[Mapping[str, Any]] = []
    purposes: list[str] = []
    for page_index in range(prior_count):
        purpose, document = _validated_committed_first_page_in_context(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            page_index=page_index,
            request_cursors=request_cursors,
        )
        try:
            payload, _envelope = _unwrap_source(
                document, f'source purpose {purpose!r}'
            )
        except SnapshotError as exc:
            raise SourceHandoffError(
                'source_contract_invalid',
                f'{purpose}: committed FIRST response envelope is invalid: {exc}',
            ) from exc
        purposes.append(purpose)
        payloads.append(payload)
    try:
        metadata = _validate_positions(payloads)
    except SnapshotError as exc:
        raise SourceHandoffError(
            'source_contract_invalid',
            f'{purposes[-1]}: committed FIRST positions chain is invalid: {exc}',
        ) from exc
    for page_index, page in enumerate(metadata):
        expected_next = request_cursors[page_index + 1]
        if page['next_cursor'] != expected_next:
            _raise_first_request_binding(
                f'{purposes[page_index]}: broker-returned next cursor does '
                'not authorize the submitted next FIRST request cursor'
            )


def validate_bound_first_positions_request_binding(
    scratch_path: os.PathLike[str] | str,
    source_purposes: Sequence[str],
    request_cursors: Sequence[str],
) -> None:
    '''Bind FIRST consumer cursor arguments to immutable reservations.

    Reservation performs the pre-read proof against earlier committed page
    payloads.  Consumers repeat the journal count/hash proof so a caller
    cannot substitute a different cursor chain after the broker response was
    saved.
    '''

    purposes = tuple(source_purposes)
    cursors = tuple(request_cursors)
    if not purposes or len(purposes) != len(cursors):
        _raise_first_request_binding(
            'FIRST source-purpose and request-cursor counts must be equal and '
            'nonzero'
        )
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    for page_index, purpose in enumerate(purposes):
        match = (
            FIRST_POSITIONS_BASE_RE.fullmatch(purpose)
            or FIRST_POSITIONS_RETRY_RE.fullmatch(purpose)
            if isinstance(purpose, str)
            else None
        )
        if match is None or int(match.group(1)) != page_index:
            _raise_first_request_binding(
                'FIRST source purposes must be the ordered canonical page '
                'namespace with at most one exact -retry suffix'
            )
        reservation = _validated_source_reservation(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            purpose=purpose,
        )
        _validate_first_reservation_cursor_binding(
            reservation, cursors[:page_index + 1]
        )
        if FIRST_POSITIONS_RETRY_RE.fullmatch(purpose) is not None:
            _validate_source_retry_authorization_in_context(
                scratch=scratch,
                scratch_marker=scratch_marker,
                transport_marker=transport_marker,
                source_root=source_root,
                base_purpose=purpose[:-len('-retry')],
                retry_purpose=purpose,
            )


def validate_bound_first_positions_page_request_binding(
    scratch_path: os.PathLike[str] | str,
    source_purpose: str,
    request_cursors: Sequence[str],
) -> None:
    '''Bind one FIRST page consumer to its complete immutable cursor chain.

    Unlike the final set consumer, a page consumer supplies one current source
    purpose and the full request chain through that page.  The purpose index
    must therefore equal ``len(request_cursors) - 1`` rather than the purpose
    and cursor counts being equal.
    '''

    cursors = _validated_first_request_cursor_chain(
        request_cursors, source_purpose
    )
    if cursors is None:  # The FIRST-only validator cannot return None here.
        _raise_first_request_binding(
            f'{source_purpose}: expected a canonical FIRST positions purpose'
        )
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(scratch_path)
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
    )
    _validate_first_prior_page_chain_in_context(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        request_cursors=cursors,
    )
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=source_purpose,
    )
    _validate_first_reservation_cursor_binding(reservation, cursors)
    if FIRST_POSITIONS_RETRY_RE.fullmatch(source_purpose) is not None:
        _validate_source_retry_authorization_in_context(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            base_purpose=source_purpose[:-len('-retry')],
            retry_purpose=source_purpose,
        )


def _stage_metadata_path(path: str) -> str:
    return path + STAGE_METADATA_SUFFIX


def _stage_metadata_document(
    *,
    scratch_id: str,
    generation: str,
    kind: str,
    filename: str,
    payload_sha256: str,
    set_id: str,
    set_index: int,
    set_file_count: int,
    set_complete: bool,
    request_cursor: str | None,
    next_cursor: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": Decimal(SCHEMA_VERSION),
        "marker": "rhmra-broker-snapshot-stage",
        "scratch_id": scratch_id,
        "generation": generation,
        "kind": kind,
        "filename": filename,
        "payload_sha256": payload_sha256,
        "set_id": set_id,
        "set_index": Decimal(set_index),
        "set_file_count": Decimal(set_file_count),
        "set_complete": set_complete,
        "request_cursor": request_cursor,
        "next_cursor": next_cursor,
    }


def _validated_stage_metadata(
    path: str,
    *,
    scratch: Path,
    marker: Mapping[str, Any],
    expected_generation: str | None,
    expected_kind: str | None = None,
) -> Mapping[str, Any]:
    resolved = Path(path).resolve(strict=True)
    if resolved.parent != scratch:
        raise SnapshotError(f"{path}: staged input is not directly inside scratch")
    metadata_path = _stage_metadata_path(str(resolved))
    document, raw = _read_source(metadata_path)
    document = _mapping(document, metadata_path)
    if set(document) != {
        "schema_version", "marker", "scratch_id", "generation", "kind",
        "filename", "payload_sha256", "set_id", "set_index",
        "set_file_count", "set_complete", "request_cursor", "next_cursor",
    } or raw != _canonical_bytes(document):
        raise SnapshotError(f"{metadata_path}: invalid staging provenance")
    generation = document.get("generation")
    kind = document.get("kind")
    digest = document.get("payload_sha256")
    set_id = document.get("set_id")
    set_index = document.get("set_index")
    set_file_count = document.get("set_file_count")
    set_complete = document.get("set_complete")
    request_cursor = document.get("request_cursor")
    next_cursor = document.get("next_cursor")
    if (
        document.get("schema_version") != Decimal(SCHEMA_VERSION)
        or document.get("marker") != "rhmra-broker-snapshot-stage"
        or document.get("scratch_id") != marker["scratch_id"]
        or generation not in ("A", "B")
        or kind not in SNAPSHOT_KINDS
        or document.get("filename") != resolved.name
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(set_id, str)
        or _UUID_RE.fullmatch(set_id) is None
        or not isinstance(set_index, Decimal)
        or set_index != set_index.to_integral_value()
        or set_index < 1
        or not isinstance(set_file_count, Decimal)
        or set_file_count != set_file_count.to_integral_value()
        or set_file_count < 1
        or set_index > set_file_count
        or not isinstance(set_complete, bool)
        or (request_cursor is not None and not isinstance(request_cursor, str))
        or (next_cursor is not None and not isinstance(next_cursor, str))
        or _sha256(resolved.read_bytes()) != digest
    ):
        raise SnapshotError(f"{metadata_path}: invalid staging provenance")
    if expected_generation is not None and generation != expected_generation:
        raise SnapshotError(
            f"{path}: staged generation {generation} does not match "
            f"{expected_generation}"
        )
    if expected_kind is not None and kind != expected_kind:
        raise SnapshotError(
            f"{path}: staged kind {kind} does not match {expected_kind}"
        )
    return document


def validate_generation_inputs(
    paths_by_kind: Mapping[str, Sequence[str]], generation: str
) -> None:
    if generation not in ("A", "B"):
        raise SnapshotError("snapshot generation must be A or B")
    flattened = [
        os.path.abspath(path)
        for paths in paths_by_kind.values()
        for path in paths
    ]
    if not flattened:
        raise SnapshotError("snapshot generation has no input files")
    parents = {Path(path).parent.resolve(strict=True) for path in flattened}
    if len(parents) != 1:
        raise SnapshotError("snapshot generation inputs must share one scratch")
    scratch = next(iter(parents))
    _scratch, marker = _validated_output_scratch(flattened)
    for kind, paths in paths_by_kind.items():
        if kind not in SNAPSHOT_KINDS:
            raise SnapshotError(f"unsupported snapshot kind {kind!r}")
        documents = [
            _validated_stage_metadata(
                path,
                scratch=scratch,
                marker=marker,
                expected_generation=generation,
                expected_kind=kind,
            )
            for path in paths
        ]
        if not documents:
            continue
        set_ids = {document["set_id"] for document in documents}
        counts = {int(document["set_file_count"]) for document in documents}
        indices = sorted(int(document["set_index"]) for document in documents)
        if (
            len(set_ids) != 1
            or counts != {len(documents)}
            or indices != list(range(1, len(documents) + 1))
            or not all(document["set_complete"] for document in documents)
        ):
            raise SnapshotError(
                f"{kind}: inputs are not one complete aggregate-sealed set"
            )
        ordered = sorted(documents, key=lambda item: int(item["set_index"]))
        if kind in ("positions", "orders"):
            if ordered[0]["request_cursor"] != "FIRST":
                raise SnapshotError(f"{kind}: sealed set does not begin at FIRST")
            for previous, current in zip(ordered, ordered[1:]):
                if previous["next_cursor"] != current["request_cursor"]:
                    raise SnapshotError(f"{kind}: sealed cursor chain is broken")
            if ordered[-1]["next_cursor"] is not None:
                raise SnapshotError(f"{kind}: sealed set is not terminal")


def _absolute_distinct_paths(
    sources: Sequence[str], outputs: Sequence[str]
) -> tuple[list[str], list[str], Path, Mapping[str, Any]]:
    if len(sources) != len(outputs):
        raise SnapshotError("--source and --output counts must match")
    absolute_sources = [os.path.abspath(path) for path in sources]
    absolute_outputs = [os.path.abspath(path) for path in outputs]
    normalized_sources = {os.path.normcase(path) for path in absolute_sources}
    normalized_outputs = [os.path.normcase(path) for path in absolute_outputs]
    if len(set(normalized_outputs)) != len(normalized_outputs):
        raise SnapshotError("--output paths must be unique")
    if normalized_sources.intersection(normalized_outputs):
        raise SnapshotError("a staged output must not overwrite a source result")
    for output in absolute_outputs:
        parent = os.path.dirname(output)
        if not parent or not os.path.isdir(parent):
            raise SnapshotError(f"{output}: output directory does not exist")
    scratch, marker = _validated_output_scratch(absolute_outputs)
    return absolute_sources, absolute_outputs, scratch, marker


def _ensure_stage_targets_absent(outputs: Sequence[str]) -> None:
    for output in outputs:
        if os.path.exists(output) or os.path.exists(_stage_metadata_path(output)):
            raise SnapshotError(f"{output}: staged output already exists")


def _helper_allocated_output_paths(
    scratch: str, kind: str, generation: str, count: int
) -> list[str]:
    if count < 1:
        raise SnapshotError("stage requires at least one source")
    allocation_id = str(uuid.uuid4())
    canonical_scratch, _marker = validate_scratch_directory(scratch)
    return [
        os.path.join(
            os.fspath(canonical_scratch),
            f"rhmra-stage-{generation.lower()}-{kind}-{allocation_id}-{index}.json",
        )
        for index in range(1, count + 1)
    ]


def _stage_retry_marker_document(scratch_id: str) -> dict[str, Any]:
    return {
        "schema_version": Decimal(SCHEMA_VERSION),
        "marker": STAGE_RETRY_MARKER_NAME,
        "scratch_id": scratch_id,
        "state": "generation-b-authorized",
        "reason": STAGE_RETRY_REASON,
    }


def _stage_retry_exhausted_marker_document(
    scratch_id: str, outcome: str
) -> dict[str, Any]:
    return {
        "schema_version": Decimal(SCHEMA_VERSION),
        "marker": STAGE_RETRY_EXHAUSTED_MARKER_NAME,
        "scratch_id": scratch_id,
        "state": "generation-b-exhausted",
        "outcome": outcome,
    }


def _validated_stage_retry_marker(
    scratch: Path, marker: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    path = scratch / STAGE_RETRY_MARKER
    if not os.path.lexists(path):
        return None
    try:
        document, _raw = _read_immutable_journal_marker(path, scratch)
    except (SourceHandoffError, SnapshotError, OSError) as exc:
        raise StageRetryStateError(
            f"{path}: cannot validate the semantic-retry marker: {exc}"
        ) from exc
    expected = _stage_retry_marker_document(str(marker["scratch_id"]))
    if document != expected:
        raise StageRetryStateError(
            f"{path}: semantic-retry marker does not match this invocation"
        )
    return document


def _validated_stage_retry_exhausted_marker(
    scratch: Path, marker: Mapping[str, Any]
) -> Mapping[str, Any] | None:
    path = scratch / STAGE_RETRY_EXHAUSTED_MARKER
    if not os.path.lexists(path):
        return None
    try:
        document, _raw = _read_immutable_journal_marker(path, scratch)
    except (SourceHandoffError, SnapshotError, OSError) as exc:
        raise StageRetryStateError(
            f"{path}: cannot validate the exhausted retry marker: {exc}"
        ) from exc
    outcome = document.get("outcome")
    if (
        not isinstance(outcome, str)
        or outcome not in STAGE_RETRY_OUTCOMES
        or document
        != _stage_retry_exhausted_marker_document(
            str(marker["scratch_id"]), outcome
        )
    ):
        raise StageRetryStateError(
            f"{path}: exhausted retry marker does not match this invocation"
        )
    if _validated_stage_retry_marker(scratch, marker) is None:
        raise StageRetryStateError(
            f"{path}: exhausted retry marker has no authorization marker"
        )
    return document


def _validate_stage_generation_authorization(
    scratch: Path, marker: Mapping[str, Any], generation: str
) -> None:
    retry_marker = _validated_stage_retry_marker(scratch, marker)
    exhausted_marker = _validated_stage_retry_exhausted_marker(scratch, marker)
    if exhausted_marker is not None:
        raise StageRetryStateError(
            "generation B is exhausted and no later generation stage is allowed"
        )
    if generation == "A" and retry_marker is not None:
        raise StageRetryStateError(
            "generation A cannot restart after generation B was authorized"
        )
    if generation == "B" and retry_marker is None:
        raise StageRetryStateError(
            "generation B has no deterministic semantic-retry authorization"
        )


def _authorize_stage_generation_b(
    scratch: Path, marker: Mapping[str, Any]
) -> None:
    if _validated_stage_retry_marker(scratch, marker) is not None:
        raise StageRetryStateError(
            "the one whole-generation semantic retry was already authorized"
        )
    path = scratch / STAGE_RETRY_MARKER
    document = _stage_retry_marker_document(str(marker["scratch_id"]))
    try:
        _write_immutable_journal_marker(path, document, scratch)
    except FileExistsError as exc:
        # A concurrent claimant is still a spent retry, never permission for
        # another A-to-B transition.
        try:
            _validated_stage_retry_marker(scratch, marker)
        except StageRetryStateError:
            raise
        raise StageRetryStateError(
            "the one whole-generation semantic retry was already authorized"
        ) from exc
    except (SourceHandoffError, SnapshotError, OSError) as exc:
        raise StageRetryStateError(
            f"{path}: cannot persist semantic-retry authorization: {exc}"
        ) from exc


def _exhaust_stage_generation_b(
    scratch: Path, marker: Mapping[str, Any], outcome: str
) -> bool:
    if outcome not in STAGE_RETRY_OUTCOMES:
        raise StageRetryStateError(
            f"invalid generation-B terminal outcome {outcome!r}"
        )
    if _validated_stage_retry_marker(scratch, marker) is None:
        raise StageRetryStateError(
            "generation B cannot finish before it is authorized"
        )
    existing = _validated_stage_retry_exhausted_marker(scratch, marker)
    if existing is not None:
        if existing["outcome"] == outcome:
            return True
        raise StageRetryStateError(
            "generation B was already exhausted with a different outcome"
        )
    path = scratch / STAGE_RETRY_EXHAUSTED_MARKER
    document = _stage_retry_exhausted_marker_document(
        str(marker["scratch_id"]), outcome
    )
    try:
        _write_immutable_journal_marker(path, document, scratch)
    except FileExistsError as exc:
        existing = _validated_stage_retry_exhausted_marker(scratch, marker)
        if existing is not None and existing["outcome"] == outcome:
            return True
        raise StageRetryStateError(
            "generation B was exhausted concurrently with a different outcome"
        ) from exc
    except (SourceHandoffError, SnapshotError, OSError) as exc:
        raise StageRetryStateError(
            f"{path}: cannot persist exhausted retry state: {exc}"
        ) from exc
    _validated_stage_retry_exhausted_marker(scratch, marker)
    return False


def _authorize_stage_generation_b_transition(
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    *,
    require_committed_a: bool = True,
) -> bool:
    reserve_lock = _acquire_source_reserve_lock(scratch, scratch_marker)
    active_error = False
    try:
        _reject_pending_source_handoff(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
        )
        if require_committed_a:
            reservation_re = re.compile(
                '^' + re.escape(SOURCE_RESERVATION_MARKER_PREFIX)
                + r'[0-9a-f]{64}\.json$'
            )
            has_committed_a = False
            try:
                entries = list(scratch.iterdir())
            except OSError as exc:
                raise StageRetryStateError(
                    f'{scratch}: cannot inspect generation-A evidence: {exc}'
                ) from exc
            for entry in entries:
                if reservation_re.fullmatch(entry.name) is None:
                    continue
                reservation = _validated_source_reservation_at_path(
                    entry,
                    scratch=scratch,
                    scratch_marker=scratch_marker,
                    transport_marker=transport_marker,
                )
                binding = _daily_loss_purpose_binding(
                    reservation['purpose']
                )
                if binding is None or binding[0] != 'A':
                    continue
                terminal = _validated_source_terminal(
                    scratch=scratch,
                    scratch_marker=scratch_marker,
                    transport_marker=transport_marker,
                    reservation=reservation,
                )
                if (
                    terminal is not None
                    and terminal['status'] == 'committed'
                ):
                    has_committed_a = True
                    break
            if not has_committed_a:
                raise StageRetryStateError(
                    'generation B requires at least one committed canonical '
                    'DAILY-LOSS generation-A response'
                )
        if _validated_stage_retry_exhausted_marker(
            scratch, scratch_marker
        ) is not None:
            raise StageRetryStateError("generation B is already exhausted")
        idempotent = _validated_stage_retry_marker(
            scratch, scratch_marker
        ) is not None
        if not idempotent:
            _authorize_stage_generation_b(scratch, scratch_marker)
        return idempotent
    except BaseException:
        active_error = True
        raise
    finally:
        try:
            _release_source_reserve_lock(
                scratch, scratch_marker, reserve_lock
            )
        except Exception:
            if not active_error:
                raise


def _finish_stage_generation_b_transition(
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    outcome: str,
) -> bool:
    reserve_lock = _acquire_source_reserve_lock(scratch, scratch_marker)
    active_error = False
    try:
        _reject_pending_source_handoff(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
        )
        return _exhaust_stage_generation_b(
            scratch, scratch_marker, outcome
        )
    except BaseException:
        active_error = True
        raise
    finally:
        try:
            _release_source_reserve_lock(
                scratch, scratch_marker, reserve_lock
            )
        except Exception:
            if not active_error:
                raise


def _prepare_atomic_files(
    outputs: Sequence[str], payloads: Sequence[Mapping[str, Any]]
) -> list[tuple[str, str, bytes]]:
    prepared: list[tuple[str, str, bytes]] = []
    try:
        for output, payload in zip(outputs, payloads):
            raw = _canonical_bytes(payload)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{os.path.basename(output)}.",
                suffix=".tmp",
                dir=os.path.dirname(output),
            )
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                parsed, persisted_raw = _read_source(temporary)
                if parsed != payload or persisted_raw != raw:
                    raise SnapshotError(f"{output}: staged JSON read-back mismatch")
            except Exception:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
            prepared.append((temporary, output, raw))
        return prepared
    except Exception:
        for temporary, _output, _raw in prepared:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _commit_atomic_files(prepared: Sequence[tuple[str, str, bytes]]) -> None:
    committed: list[tuple[str, str]] = []
    try:
        for temporary, output, _raw in prepared:
            try:
                os.link(temporary, output)
            except FileExistsError as exc:
                raise SnapshotError(
                    f"{output}: staged output appeared concurrently"
                ) from exc
            except OSError as exc:
                raise SnapshotError(
                    f"{output}: atomic no-clobber commit failed: {exc}"
                ) from exc
            committed.append((temporary, output))
        for _temporary, output, raw in prepared:
            parsed, persisted_raw = _read_source(output)
            if persisted_raw != raw:
                raise SnapshotError(f"{output}: committed JSON read-back mismatch")
            # Parsing above also proves strict JSON.  Equality was checked for
            # each temporary file before the commit.
            if not isinstance(parsed, Mapping):
                raise SnapshotError(f"{output}: committed payload is not an object")
        for temporary, _output, _raw in prepared:
            os.unlink(temporary)
    except Exception:
        for temporary, output in committed:
            try:
                if os.path.exists(output) and os.path.samefile(temporary, output):
                    os.unlink(output)
            except OSError:
                pass
        for temporary, _output, _raw in prepared:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise


def _stage_impl(
    args: argparse.Namespace, stage_state: dict[str, Any]
) -> dict[str, Any]:
    # Pagination flags are semantically inert for snapshot kinds that cannot
    # paginate.  Normalize them before validation and provenance generation.
    paginated = args.kind in {"positions", "orders"}
    request_cursors = args.request_cursor if paginated else None
    allow_more = args.allow_more if paginated else False
    prevalidated_documents: dict[str, tuple[Any, bytes]] = {}
    source_purposes: list[str] | None = args.source_purpose
    source_arguments = source_purposes if source_purposes is not None else args.source
    if args.auto_output_scratch is not None:
        scratch, marker = validate_scratch_directory(
            args.auto_output_scratch
        )
        output_mode = "helper-allocated"
    else:
        requested_outputs = args.output
        absolute_outputs = [
            os.path.abspath(path) for path in requested_outputs
        ]
        scratch, marker = _validated_output_scratch(absolute_outputs)
        output_mode = "caller-supplied"
    transport_marker = _validated_transport_marker(scratch, marker)
    stage_state["scratch"] = scratch
    stage_state["scratch_marker"] = marker
    stage_state["transport_marker"] = transport_marker
    _validate_stage_generation_authorization(
        scratch, marker, args.generation
    )
    _reject_pending_source_handoff(
        scratch=scratch,
        scratch_marker=marker,
        transport_marker=transport_marker,
    )
    if args.auto_output_scratch is not None:
        requested_outputs = _helper_allocated_output_paths(
            str(scratch),
            args.kind,
            args.generation,
            len(source_arguments),
        )
    if source_purposes is not None:
        if len(source_purposes) != len(requested_outputs):
            raise SnapshotError(
                "--source-purpose and --output counts must match"
            )
        for source_purpose in source_purposes:
            binding = _daily_loss_purpose_binding(source_purpose)
            if binding is not None and (
                binding[0] != args.generation or binding[1] != args.kind
            ):
                raise StageBindingError(
                    f'{source_purpose}: DAILY-LOSS purpose generation/kind '
                    'does not match the stage command'
                )
        validated_purposes = validate_bound_external_json_purposes(
            scratch, source_purposes
        )
        purpose_sources = [str(path) for path, _document, _raw in validated_purposes]
        sources, outputs, scratch, marker = _absolute_distinct_paths(
            purpose_sources, requested_outputs
        )
        prevalidated_documents = {
            source: (document, raw)
            for source, (_path, document, raw) in zip(
                sources, validated_purposes
            )
        }
    else:
        sources, outputs, scratch, marker = _absolute_distinct_paths(
            args.source, requested_outputs
        )
    bound_source_root = Path(transport_marker['source_root'])
    external_documents: dict[str, tuple[Any, bytes]] = dict(
        prevalidated_documents
    )
    for source in sources:
        if source in external_documents:
            continue
        source_input = Path(source)
        try:
            os.lstat(source_input)
        except OSError as exc:
            raise SnapshotError(
                f'{source_input}: cannot inspect broker-response source: {exc}'
            ) from exc
        source_path = source_input.resolve(strict=True)
        if source_path.parent == scratch:
            _validated_stage_metadata(
                source,
                scratch=scratch,
                marker=marker,
                expected_generation=args.generation,
                expected_kind=args.kind,
            )
        else:
            _resolved, document, raw = _validated_committed_source_for_path(
                scratch=scratch,
                scratch_marker=marker,
                transport_marker=transport_marker,
                source_root=bound_source_root,
                source_arg=source,
            )
            external_documents[source] = (document, raw)
    source_documents: list[Any] = []
    source_raws: list[bytes] = []
    source_contexts: list[str] = []
    for source_index, source in enumerate(sources):
        if source in external_documents:
            document, source_raw = external_documents[source]
        else:
            document, source_raw = _read_source(source)
        source_context = (
            source
            if source_purposes is None
            else f"source purpose {source_purposes[source_index]!r}"
        )
        source_documents.append(document)
        source_raws.append(source_raw)
        source_contexts.append(source_context)

    stage_state["phase"] = "response"
    payloads: list[Mapping[str, Any]] = []
    envelopes: list[str] = []
    source_hashes: list[str] = []
    for document, source_raw, source_context in zip(
        source_documents, source_raws, source_contexts
    ):
        payload, envelope = _unwrap_source(document, source_context)
        payloads.append(payload)
        envelopes.append(envelope)
        source_hashes.append(_sha256(source_raw))

    stage_state["phase"] = "semantic"
    metadata = _validate_payload_documents(args.kind, payloads)
    stage_state["phase"] = "binding"
    request_cursors = _validate_pagination(
        args.kind, metadata, request_cursors, allow_more
    )
    stage_state["phase"] = "semantic"
    metadata_paths = [_stage_metadata_path(output) for output in outputs]
    payload_raws = [_canonical_bytes(payload) for payload in payloads]
    stage_state["phase"] = "internal"
    set_id = str(uuid.uuid4())
    complete = not allow_more
    metadata_documents = [
        _stage_metadata_document(
            scratch_id=marker["scratch_id"],
            generation=args.generation,
            kind=args.kind,
            filename=Path(output).name,
            payload_sha256=_sha256(payload_raw),
            set_id=set_id,
            set_index=index,
            set_file_count=len(outputs),
            set_complete=complete,
            request_cursor=(
                request_cursors[index - 1]
                if request_cursors is not None else None
            ),
            next_cursor=page["next_cursor"],
        )
        for index, (output, payload_raw, page) in enumerate(
            zip(outputs, payload_raws, metadata), 1
        )
    ]
    stage_state["phase"] = "write"
    _ensure_stage_targets_absent(outputs)
    all_prepared = _prepare_atomic_files(
        [*outputs, *metadata_paths], [*payloads, *metadata_documents]
    )
    prepared = all_prepared[:len(outputs)]
    _commit_atomic_files(all_prepared)

    stage_state["phase"] = "receipt"
    files: list[dict[str, Any]] = []
    for index, (source, output, envelope, source_hash, page, prepared_item) in enumerate(
        zip(sources, outputs, envelopes, source_hashes, metadata, prepared), 1
    ):
        entry: dict[str, Any] = {
            "index": index,
            "output": output,
            "transport": envelope,
            "source_sha256": source_hash,
            "payload_sha256": _sha256(prepared_item[2]),
            "provenance": metadata_paths[index - 1],
            "row_count": page["row_count"],
            "next_cursor": page["next_cursor"],
        }
        if source_purposes is None:
            entry["source"] = source
        else:
            entry["source_purpose"] = source_purposes[index - 1]
        if request_cursors is not None:
            entry["request_cursor"] = request_cursors[index - 1]
        files.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "stage",
        "ok": True,
        "kind": args.kind,
        "generation": args.generation,
        "output_mode": output_mode,
        "set_id": set_id,
        "complete": not allow_more,
        "file_count": len(files),
        "output_paths": [entry["output"] for entry in files],
        "files": files,
    }


def _stage(args: argparse.Namespace) -> dict[str, Any]:
    stage_state: dict[str, Any] = {"phase": "input"}
    try:
        return _stage_impl(args, stage_state)
    except StageError:
        raise
    except SourceHandoffError:
        raise
    except (SnapshotError, OSError) as exc:
        phase_error = {
            "input": StageInputError,
            "response": StageResponseError,
            "semantic": StageSemanticError,
            "binding": StageBindingError,
            "internal": StageInternalError,
            "write": StageWriteError,
        }.get(stage_state["phase"])
        if phase_error is None:
            raise
        if phase_error is StageSemanticError:
            scratch = stage_state.get("scratch")
            marker = stage_state.get("scratch_marker")
            transport_marker = stage_state.get("transport_marker")
            if (
                not isinstance(scratch, Path)
                or not isinstance(marker, Mapping)
                or not isinstance(transport_marker, Mapping)
            ):
                raise StageRetryStateError(
                    "semantic failure occurred without bound retry state"
                ) from exc
            if args.generation == "A":
                _authorize_stage_generation_b_transition(
                    scratch,
                    marker,
                    transport_marker,
                    require_committed_a=False,
                )
                recovery_action = "generation-b"
            else:
                _finish_stage_generation_b_transition(
                    scratch, marker, transport_marker, "failed"
                )
                recovery_action = "snapshot-second-attempt-failed"
            raise StageSemanticError(str(exc), recovery_action) from exc
        raise phase_error(str(exc)) from exc


def _authorize_generation_b(args: argparse.Namespace) -> dict[str, Any]:
    scratch, scratch_marker, transport_marker, _source_root = (
        _validated_source_journal_context(args.scratch)
    )
    idempotent = _authorize_stage_generation_b_transition(
        scratch, scratch_marker, transport_marker
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "authorize-generation-b",
        "ok": True,
        "scratch": str(scratch),
        "state": "generation-b-authorized",
        "reason": STAGE_RETRY_REASON,
        "idempotent": idempotent,
    }


def _finish_generation_b(args: argparse.Namespace) -> dict[str, Any]:
    scratch, scratch_marker, transport_marker, _source_root = (
        _validated_source_journal_context(args.scratch)
    )
    idempotent = _finish_stage_generation_b_transition(
        scratch, scratch_marker, transport_marker, args.outcome
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "finish-generation-b",
        "ok": True,
        "scratch": str(scratch),
        "state": "generation-b-exhausted",
        "outcome": args.outcome,
        "idempotent": idempotent,
    }


def _preflight_directory(
    scratch: Path,
    *,
    prepared_source_root: Path | None = None,
    prepared_source_root_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    if not scratch.is_dir():
        raise SnapshotError(f"{scratch}: scratch path is not a directory")
    project = Path(__file__).resolve().parent
    if scratch == project or project in scratch.parents:
        raise SnapshotError("scratch directory must be outside the project folder")
    if (prepared_source_root is None) != (
        prepared_source_root_identity is None
    ):
        raise SnapshotError(
            'prepared response-source root identity is incomplete'
        )
    if prepared_source_root is not None:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        if (
            scratch.parent != temp_root
            or not scratch.name.startswith('rhmra-session-')
            or prepared_source_root.parent != temp_root
            or not prepared_source_root.name.startswith('rhmra-source-')
            or prepared_source_root == scratch
        ):
            raise SnapshotError(
                'helper-created scratch and response-source root must be '
                'distinct direct children of native temp'
            )
        try:
            source_root_stat = os.lstat(prepared_source_root)
        except OSError as exc:
            raise SnapshotError(
                f'{prepared_source_root}: cannot inspect prepared '
                f'response-source root: {exc}'
            ) from exc
        is_junction = getattr(
            prepared_source_root, 'is_junction', lambda: False
        )
        if (
            stat.S_ISLNK(source_root_stat.st_mode)
            or not stat.S_ISDIR(source_root_stat.st_mode)
            or is_junction()
            or prepared_source_root.resolve(strict=True)
            != prepared_source_root
            or (
                source_root_stat.st_dev,
                source_root_stat.st_ino,
            ) != prepared_source_root_identity
            or any(prepared_source_root.iterdir())
        ):
            raise SnapshotError(
                f'{prepared_source_root}: prepared response-source root is '
                'not the unchanged empty helper-created directory'
            )

    sentinel = {
        "schema_version": Decimal(SCHEMA_VERSION),
        "sentinel": "rhmra-scratch-preflight",
        "write_read_parse": True,
    }
    raw = _canonical_bytes(sentinel)
    descriptor = -1
    path: str | None = None
    try:
        descriptor, path = tempfile.mkstemp(
            prefix=".rhmra-scratch-preflight-", suffix=".json", dir=str(scratch)
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        parsed, read_raw = _read_source(path)
        if parsed != sentinel or read_raw != raw:
            raise SnapshotError("scratch sentinel read-back mismatch")
        os.unlink(path)
        path = None
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if path is not None:
            try:
                os.unlink(path)
            except OSError:
                pass

    marker_path = str(scratch / SCRATCH_MARKER)
    if os.path.exists(marker_path):
        raise SnapshotError(f'{marker_path}: scratch marker already exists')
    scratch_id = str(uuid.uuid4())
    marker = _scratch_marker_document(scratch_id)
    marker_paths = [marker_path]
    marker_documents: list[Mapping[str, Any]] = [marker]
    source_root_id: str | None = None
    if prepared_source_root is not None:
        preparation_path = str(scratch / TRANSPORT_PREPARATION_MARKER)
        if os.path.exists(preparation_path):
            raise SnapshotError(
                f'{preparation_path}: prepared source-root marker already exists'
            )
        source_root_id = str(uuid.uuid4())
        preparation = _transport_preparation_marker_document(
            scratch_id=scratch_id,
            source_root=str(prepared_source_root),
            source_root_id=source_root_id,
            source_root_identity=prepared_source_root_identity,
        )
        marker_paths.append(preparation_path)
        marker_documents.append(preparation)
    prepared = _prepare_atomic_files(marker_paths, marker_documents)
    _commit_atomic_files(prepared)

    result = {
        "schema_version": SCHEMA_VERSION,
        "action": "preflight",
        "ok": True,
        "scratch": str(scratch),
        "scratch_id": scratch_id,
        "sentinel_sha256": _sha256(raw),
        "write_read_parse": True,
        "cleanup_verified": True,
    }
    if prepared_source_root is not None:
        assert source_root_id is not None
        result['source_root'] = str(prepared_source_root)
        result['source_root_id'] = source_root_id
    return result


def _cleanup_created_scratch(
    scratch: Path,
    temp_root: Path,
    created_identity: tuple[int, int],
) -> list[str]:
    """Remove only the unchanged empty directory created by this invocation."""

    failures: list[str] = []
    if (
        scratch.parent != temp_root
        or not scratch.name.startswith("rhmra-session-")
    ):
        return ["refusing cleanup outside the helper-owned native-temp namespace"]
    try:
        current = os.lstat(scratch)
    except OSError as exc:
        return [f"cannot inspect helper-created scratch directory: {exc}"]
    is_junction = getattr(scratch, "is_junction", lambda: False)
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or is_junction()
    ):
        return ["refusing cleanup of a replaced or redirected scratch directory"]
    if (current.st_dev, current.st_ino) != created_identity:
        return ["refusing cleanup because scratch directory identity changed"]
    try:
        os.rmdir(scratch)
    except OSError as exc:
        failures.append(f"cannot remove helper-created scratch directory: {exc}")
    return failures


def _cleanup_created_source_root(
    source_root: Path,
    temp_root: Path,
    created_identity: tuple[int, int],
) -> list[str]:
    """Remove only the unchanged empty source root created by this invocation."""

    if (
        source_root.parent != temp_root
        or not source_root.name.startswith('rhmra-source-')
    ):
        return [
            'refusing response-source cleanup outside the helper-owned '
            'native-temp namespace'
        ]
    try:
        current = os.lstat(source_root)
    except OSError as exc:
        return [f'cannot inspect helper-created response-source root: {exc}']
    is_junction = getattr(source_root, 'is_junction', lambda: False)
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or is_junction()
    ):
        return [
            'refusing cleanup of a replaced or redirected response-source root'
        ]
    if (current.st_dev, current.st_ino) != created_identity:
        return [
            'refusing cleanup because response-source root identity changed'
        ]
    try:
        os.rmdir(source_root)
    except OSError as exc:
        return [f'cannot remove helper-created response-source root: {exc}']
    return []


def _windows_security_api() -> tuple[Any, Any]:
    """Return the configured Windows security and allocation APIs."""

    advapi32 = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    pointer = ctypes.c_void_p
    pointer_pointer = ctypes.POINTER(pointer)
    dword_pointer = ctypes.POINTER(ctypes.c_uint32)

    advapi32.GetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        pointer_pointer,
        pointer_pointer,
        pointer_pointer,
        pointer_pointer,
        pointer_pointer,
    )
    advapi32.GetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.SetNamedSecurityInfoW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_int,
        ctypes.c_uint32,
        pointer,
        pointer,
        pointer,
        pointer,
    )
    advapi32.SetNamedSecurityInfoW.restype = ctypes.c_uint32
    advapi32.ConvertSidToStringSidW.argtypes = (
        pointer,
        pointer_pointer,
    )
    advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        pointer_pointer,
        dword_pointer,
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        ctypes.c_int
    )
    advapi32.GetSecurityDescriptorDacl.argtypes = (
        pointer,
        ctypes.POINTER(ctypes.c_int),
        pointer_pointer,
        ctypes.POINTER(ctypes.c_int),
    )
    advapi32.GetSecurityDescriptorDacl.restype = ctypes.c_int
    advapi32.GetSecurityDescriptorControl.argtypes = (
        pointer,
        ctypes.POINTER(ctypes.c_ushort),
        dword_pointer,
    )
    advapi32.GetSecurityDescriptorControl.restype = ctypes.c_int
    advapi32.GetAce.argtypes = (
        pointer,
        ctypes.c_uint32,
        pointer_pointer,
    )
    advapi32.GetAce.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = (pointer,)
    kernel32.LocalFree.restype = pointer
    return advapi32, kernel32


def _windows_api_error(operation: str, code: int | None = None) -> OSError:
    if code is None:
        code = ctypes.get_last_error()
    return OSError(code, f'{operation} failed: {ctypes.FormatError(code)}')


def _windows_sid_string(sid: Any, api: tuple[Any, Any]) -> str:
    advapi32, kernel32 = api
    allocated = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(allocated)):
        raise _windows_api_error('ConvertSidToStringSidW')
    try:
        value = ctypes.wstring_at(allocated)
        if not value.startswith('S-'):
            raise OSError('ConvertSidToStringSidW returned an invalid SID')
        return value
    finally:
        kernel32.LocalFree(allocated)


def _windows_path_owner_sid(path: Path, api: tuple[Any, Any]) -> str:
    advapi32, kernel32 = api
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001,  # OWNER_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if status:
        raise _windows_api_error('GetNamedSecurityInfoW(owner)', status)
    try:
        if not owner.value:
            raise OSError(f'{path}: Windows owner SID is missing')
        return _windows_sid_string(owner, api)
    finally:
        kernel32.LocalFree(descriptor)


def _windows_set_directory_dacl(
    path: Path,
    sddl: str,
    api: tuple[Any, Any],
) -> None:
    advapi32, kernel32 = api
    descriptor = ctypes.c_void_p()
    descriptor_size = ctypes.c_uint32()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,  # SDDL_REVISION_1
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise _windows_api_error(
            'ConvertStringSecurityDescriptorToSecurityDescriptorW'
        )
    try:
        present = ctypes.c_int()
        defaulted = ctypes.c_int()
        dacl = ctypes.c_void_p()
        if not advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise _windows_api_error('GetSecurityDescriptorDacl')
        if not present.value or not dacl.value:
            raise OSError('constructed Windows DACL is missing')
        status = advapi32.SetNamedSecurityInfoW(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL + PROTECTED_DACL
            None,
            None,
            dacl,
            None,
        )
        if status:
            raise _windows_api_error('SetNamedSecurityInfoW', status)
    finally:
        kernel32.LocalFree(descriptor)


def _windows_read_directory_acl(
    path: Path,
    api: tuple[Any, Any],
) -> tuple[str, bool, list[tuple[str, int, int]]]:
    advapi32, kernel32 = api
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    status = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000001 | 0x00000004,  # OWNER + DACL_SECURITY_INFORMATION
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status:
        raise _windows_api_error('GetNamedSecurityInfoW(DACL)', status)
    try:
        if not owner.value or not dacl.value:
            raise OSError(f'{path}: Windows owner or DACL is missing')
        control = ctypes.c_ushort()
        revision = ctypes.c_uint32()
        if not advapi32.GetSecurityDescriptorControl(
            descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            raise _windows_api_error('GetSecurityDescriptorControl')

        acl = _WindowsAclHeader.from_address(dacl.value)
        entries: list[tuple[str, int, int]] = []
        for index in range(acl.ace_count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                raise _windows_api_error('GetAce')
            header = _WindowsAceHeader.from_address(ace.value)
            if header.ace_type != _WINDOWS_ACCESS_ALLOWED_ACE_TYPE:
                raise OSError(f'{path}: Windows DACL contains a non-allow ACE')
            if header.ace_size < 12:
                raise OSError(f'{path}: Windows DACL contains a truncated ACE')
            mask = ctypes.c_uint32.from_address(
                ace.value + ctypes.sizeof(_WindowsAceHeader)
            ).value
            sid = ctypes.c_void_p(
                ace.value + ctypes.sizeof(_WindowsAceHeader) + 4
            )
            entries.append(
                (_windows_sid_string(sid, api), header.ace_flags, mask)
            )
        return (
            _windows_sid_string(owner, api),
            bool(control.value & _WINDOWS_DACL_PROTECTED),
            entries,
        )
    finally:
        kernel32.LocalFree(descriptor)


def _windows_prepare_file_change_directory(
    path: Path,
    temp_root: Path,
) -> None:
    """Install and verify the least-privilege cross-principal DACL bridge."""

    api = _windows_security_api()
    helper_sid = _windows_path_owner_sid(path, api)
    temp_owner_sid = _windows_path_owner_sid(temp_root, api)
    writer_sids = {temp_owner_sid}
    writer_sids.discard(helper_sid)

    inheritable = _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_CONTAINER_INHERIT_ACE
    owner_file_only = _WINDOWS_OBJECT_INHERIT_ACE | _WINDOWS_INHERIT_ONLY_ACE
    sddl_aces = [
        f'(A;OICI;FA;;;{helper_sid})',
        '(A;OICI;FA;;;SY)',
        '(A;OICI;FA;;;BA)',
        '(A;OIIO;FA;;;OW)',
    ]
    sddl_aces.extend(
        f'(A;;0x{_WINDOWS_FILE_CHANGE_DIRECTORY_ACCESS:08x};;;{sid})'
        for sid in sorted(writer_sids)
    )
    _windows_set_directory_dacl(path, 'D:P' + ''.join(sddl_aces), api)

    expected = [
        (helper_sid, inheritable, _WINDOWS_FILE_ALL_ACCESS),
        ('S-1-5-18', inheritable, _WINDOWS_FILE_ALL_ACCESS),
        ('S-1-5-32-544', inheritable, _WINDOWS_FILE_ALL_ACCESS),
        ('S-1-3-4', owner_file_only, _WINDOWS_FILE_ALL_ACCESS),
    ]
    expected.extend(
        (sid, 0, _WINDOWS_FILE_CHANGE_DIRECTORY_ACCESS)
        for sid in writer_sids
    )
    actual_owner, protected, actual = _windows_read_directory_acl(path, api)
    if actual_owner != helper_sid:
        raise OSError(f'{path}: Windows directory owner changed during ACL setup')
    if not protected:
        raise OSError(f'{path}: Windows DACL is not protected')
    if sorted(actual) != sorted(expected):
        raise OSError(f'{path}: Windows DACL verification mismatch')


def _create_file_change_temp_directory(temp_root: Path, prefix: str) -> str:
    """Create a private temp directory with a narrow file-change bridge."""

    created = tempfile.mkdtemp(prefix=prefix, dir=str(temp_root))
    if not _WINDOWS:
        return created

    candidate = Path(created)
    try:
        _windows_prepare_file_change_directory(candidate, temp_root)
    except Exception as caught:
        exc = (
            caught
            if isinstance(caught, OSError)
            else OSError(f'Windows file-change ACL setup failed: {caught}')
        )
        try:
            os.rmdir(candidate)
        except OSError as cleanup_exc:
            raise OSError(
                f'{exc}; cannot remove ACL-setup directory: {cleanup_exc}'
            ) from exc
        if exc is caught:
            raise
        raise exc from caught
    return created


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    if not args.create_scratch:
        if not os.path.isabs(args.scratch):
            raise SnapshotError("--scratch must be an absolute path")
        scratch = Path(args.scratch).resolve(strict=True)
        return _preflight_directory(scratch)

    try:
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        created_text = _create_file_change_temp_directory(
            temp_root, 'rhmra-session-'
        )
    except OSError as exc:
        raise ScratchCreateError(
            f"cannot create native-temp scratch directory: {exc}"
        ) from exc

    scratch = Path(created_text)
    created_identity: tuple[int, int] | None = None
    source_root: Path | None = None
    source_root_identity: tuple[int, int] | None = None
    try:
        created_stat = os.lstat(scratch)
        is_junction = getattr(scratch, "is_junction", lambda: False)
        if (
            stat.S_ISLNK(created_stat.st_mode)
            or not stat.S_ISDIR(created_stat.st_mode)
            or is_junction()
        ):
            raise SnapshotError("helper-created scratch path is not a real directory")
        created_identity = (created_stat.st_dev, created_stat.st_ino)
        scratch = scratch.resolve(strict=True)
        if scratch.parent != temp_root:
            raise SnapshotError(
                "helper-created scratch directory is not a direct child of native temp"
            )
        try:
            source_root_text = _create_file_change_temp_directory(
                temp_root, 'rhmra-source-'
            )
        except OSError as exc:
            raise ScratchCreateError(
                f'cannot create native-temp response-source directory: {exc}'
            ) from exc
        source_root = Path(source_root_text)
        source_root_stat = os.lstat(source_root)
        source_root_is_junction = getattr(
            source_root, 'is_junction', lambda: False
        )
        if (
            stat.S_ISLNK(source_root_stat.st_mode)
            or not stat.S_ISDIR(source_root_stat.st_mode)
            or source_root_is_junction()
        ):
            raise SnapshotError(
                'helper-created response-source path is not a real directory'
            )
        source_root_identity = (
            source_root_stat.st_dev,
            source_root_stat.st_ino,
        )
        source_root = source_root.resolve(strict=True)
        if source_root.parent != temp_root:
            raise SnapshotError(
                'helper-created response-source directory is not a direct '
                'child of native temp'
            )
        return _preflight_directory(
            scratch,
            prepared_source_root=source_root,
            prepared_source_root_identity=source_root_identity,
        )
    except Exception as exc:
        cleanup_failures: list[str] = []
        if source_root is not None:
            if source_root_identity is not None:
                cleanup_failures.extend(
                    _cleanup_created_source_root(
                        source_root, temp_root, source_root_identity
                    )
                )
            else:
                cleanup_failures.append(
                    'response-source identity was not established; '
                    'no cleanup attempted'
                )
        if created_identity is not None:
            cleanup_failures.extend(
                _cleanup_created_scratch(
                    scratch, temp_root, created_identity
                )
            )
        else:
            cleanup_failures.append(
                'scratch identity was not established; no cleanup attempted'
            )
        message = str(exc)
        if cleanup_failures:
            message += "; cleanup failed: " + "; ".join(cleanup_failures)
        error_type = (
            ScratchCreateError
            if isinstance(exc, ScratchCreateError)
            else SnapshotError
        )
        raise error_type(message) from exc


def _validated_transport_paths(
    source_root_arg: str, canary_arg: str, scratch: Path
) -> tuple[Path, Path]:
    if not os.path.isabs(source_root_arg):
        raise SnapshotError('--source-root must be an absolute path')
    source_root_input = Path(source_root_arg)
    try:
        source_root_stat = os.lstat(source_root_input)
    except OSError as exc:
        raise SnapshotError(
            f'{source_root_input}: cannot inspect response-source root: {exc}'
        ) from exc
    if stat.S_ISLNK(source_root_stat.st_mode) or not stat.S_ISDIR(
        source_root_stat.st_mode
    ):
        raise SnapshotError('response-source root must be a non-symlink directory')
    source_root = source_root_input.resolve(strict=True)
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    if source_root.parent != temp_root:
        raise SnapshotError(
            'response-source root must be a direct child of the runtime temp directory'
        )
    project = Path(__file__).resolve().parent
    if (
        source_root == project
        or project in source_root.parents
        or source_root in project.parents
    ):
        raise SnapshotError(
            'response-source root must be disjoint from the project folder'
        )
    if not os.path.isabs(canary_arg):
        raise SnapshotError('--canary must be an absolute path')
    canary_input = Path(canary_arg)
    try:
        canary_stat = os.lstat(canary_input)
    except OSError as exc:
        raise SnapshotError(
            f'{canary_input}: cannot inspect transport canary: {exc}'
        ) from exc
    if stat.S_ISLNK(canary_stat.st_mode) or not stat.S_ISREG(
        canary_stat.st_mode
    ):
        raise SnapshotError('transport canary must be a non-symlink regular file')
    canary = canary_input.resolve(strict=True)
    if canary.parent != source_root:
        raise SnapshotError(
            'transport canary must be a direct child of the response-source root'
        )
    return source_root, canary


def _transport_canary_instance(
    canary: Path, canary_stat: os.stat_result
) -> str:
    identity = {
        'path': str(canary),
        'device': str(canary_stat.st_dev),
        'inode': str(canary_stat.st_ino),
        'ctime_ns': str(canary_stat.st_ctime_ns),
        'mtime_ns': str(canary_stat.st_mtime_ns),
        'size': str(canary_stat.st_size),
        'mode': str(canary_stat.st_mode),
        'links': str(canary_stat.st_nlink),
    }
    return _sha256(_canonical_bytes(identity))


def _safe_transport_canary_cleanup_candidate(
    source_root_arg: str, canary_arg: str
) -> tuple[Path, str] | None:
    if not os.path.isabs(source_root_arg) or not os.path.isabs(canary_arg):
        return None
    source_root_input = Path(source_root_arg)
    canary_input = Path(canary_arg)
    try:
        source_root_stat = os.lstat(source_root_input)
        canary_stat = os.lstat(canary_input)
        if stat.S_ISLNK(source_root_stat.st_mode):
            return None
        if not stat.S_ISDIR(source_root_stat.st_mode):
            return None
        source_root_is_junction = getattr(
            source_root_input, 'is_junction', lambda: False
        )
        if source_root_is_junction():
            return None
        if stat.S_ISLNK(canary_stat.st_mode):
            return None
        if not stat.S_ISREG(canary_stat.st_mode):
            return None
    except OSError:
        return None
    try:
        source_root = source_root_input.resolve(strict=True)
        canary = canary_input.resolve(strict=True)
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except OSError:
        return None
    if source_root == temp_root or temp_root not in source_root.parents:
        return None
    if canary.parent != source_root:
        return None
    project = Path(__file__).resolve().parent
    if source_root == project or project in source_root.parents:
        return None
    try:
        current_canary_stat = os.lstat(canary)
    except OSError:
        return None
    if (
        stat.S_ISLNK(current_canary_stat.st_mode)
        or not stat.S_ISREG(current_canary_stat.st_mode)
        or _transport_canary_instance(canary, current_canary_stat)
        != _transport_canary_instance(canary, canary_stat)
    ):
        return None
    return canary, _transport_canary_instance(canary, current_canary_stat)


def _remove_transport_canary(
    candidate: tuple[Path, str] | None,
) -> None:
    if candidate is None:
        return
    canary, expected_instance = candidate
    try:
        canary_stat = os.lstat(canary)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SnapshotError(
            f'{canary}: transport canary privacy cleanup inspection failed: {exc}'
        ) from exc
    if (
        stat.S_ISLNK(canary_stat.st_mode)
        or not stat.S_ISREG(canary_stat.st_mode)
        or canary.resolve(strict=True) != canary
        or _transport_canary_instance(canary, canary_stat) != expected_instance
    ):
        raise SnapshotError(
            f'{canary}: transport canary instance changed before privacy cleanup'
        )
    try:
        os.unlink(canary)
    except OSError as exc:
        raise SnapshotError(
            f'{canary}: transport canary privacy cleanup failed: {exc}'
        ) from exc


def _record_transport_attempt(
    scratch: Path, scratch_id: str, source_root: Path,
    canary_candidate: tuple[Path, str] | None,
) -> None:
    marker_path = scratch / TRANSPORT_ATTEMPT_MARKER
    already_attempted_message = (
        f'{marker_path}: broker-response transport binding was already attempted'
    )
    if os.path.lexists(marker_path):
        try:
            existing = _validated_transport_attempt_marker(
                scratch, {'scratch_id': scratch_id}
            )
        except SnapshotError:
            raise TransportAlreadyAttemptedError(already_attempted_message)
        raise TransportAlreadyAttemptedError(
            already_attempted_message,
            marker_stable=True,
            canary_instance=existing['canary_instance'],
        )
    canary_instance = (
        canary_candidate[1] if canary_candidate is not None else None
    )
    marker = _transport_attempt_marker_document(
        scratch_id=scratch_id,
        source_root=str(source_root),
        canary_instance=canary_instance,
    )
    raw = _canonical_bytes(marker)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, 'O_BINARY'):
        flags |= os.O_BINARY
    descriptor = -1
    try:
        descriptor = os.open(marker_path, flags, 0o600)
    except FileExistsError as exc:
        raise TransportAlreadyAttemptedError(
            already_attempted_message
        ) from exc
    except OSError as exc:
        raise SnapshotError(
            f'{marker_path}: cannot create irreversible transport-attempt marker: {exc}'
        ) from exc
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise SnapshotError(
                    f'{marker_path}: incomplete transport-attempt marker write'
                )
            offset += written
        os.fsync(descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        persisted, persisted_raw = _read_source(str(marker_path))
        if persisted != marker or persisted_raw != raw:
            raise SnapshotError(
                f'{marker_path}: transport-attempt marker read-back mismatch'
            )
    except Exception as exc:
        if os.path.lexists(marker_path):
            raise SnapshotError(
                f'{marker_path}: irreversible transport-attempt marker was created '
                'but could not be verified'
            ) from exc
        raise


def _bind_transport(args: argparse.Namespace) -> dict[str, Any]:
    """Bind one proven file-change source root for the entire invocation."""

    cleanup_candidate = _safe_transport_canary_cleanup_candidate(
        args.source_root, args.canary
    )
    if not os.path.isabs(args.scratch):
        _remove_transport_canary(cleanup_candidate)
        raise SnapshotError('--scratch must be an absolute path')
    try:
        scratch, scratch_marker = validate_scratch_directory(args.scratch)
    except Exception:
        _remove_transport_canary(cleanup_candidate)
        raise
    try:
        preparation = _validated_transport_preparation_marker(
            scratch, scratch_marker
        )
    except Exception:
        _remove_transport_canary(cleanup_candidate)
        raise
    attempt_source_root = Path(preparation['source_root'])
    attempt_canary_candidate = (
        cleanup_candidate
        if (
            cleanup_candidate is not None
            and args.source_root == preparation['source_root']
            and cleanup_candidate[0].parent == attempt_source_root
        )
        else None
    )
    source_root: Path | None = None
    validation_error: Exception | None = None
    raw = b''
    resolved_account: Mapping[str, Any] | None = None
    account_name: str | None = None
    try:
        _record_transport_attempt(
            scratch,
            scratch_marker['scratch_id'],
            attempt_source_root,
            attempt_canary_candidate,
        )
        account_name = _text(args.account_name, '--account-name')
        if args.source_root != preparation['source_root']:
            raise SnapshotError(
                '--source-root must exactly equal the helper-prepared '
                'source_root from this invocation preflight'
            )
        prepared_source_root = _validated_prepared_source_root(preparation)
        source_root, canary = _validated_transport_paths(
            args.source_root, args.canary, scratch
        )
        if source_root != prepared_source_root:
            raise SnapshotError(
                'response-source root does not match the helper-prepared '
                'directory instance'
            )
        if source_root == scratch:
            raise SnapshotError(
                'response-source root must be a sibling of the marked scratch '
                'directory'
            )
        root_entries = list(source_root.iterdir())
        if len(root_entries) != 1 or root_entries[0] != canary:
            raise SnapshotError(
                'response-source root must contain exactly the transport '
                'canary before binding'
            )
        document, raw = _read_source(str(canary))
        payload, _envelope = _unwrap_source(document, str(canary))
        data = _mapping(payload.get('data'), f'{canary}.data')
        accounts = data.get('accounts')
        if not isinstance(accounts, list):
            raise SnapshotError(
                f'{canary}.data.accounts: expected the get_accounts array'
            )
        try:
            matches: list[Mapping[str, Any]] = []
            for index, value in enumerate(accounts):
                account = _mapping(value, f'{canary}.data.accounts[{index}]')
                labels: list[str] = []
                for field in ('nickname', 'name'):
                    label = account.get(field)
                    if label is not None:
                        labels.append(
                            _text(
                                label,
                                f'{canary}.data.accounts[{index}].{field}',
                            )
                        )
                if account_name in labels:
                    matches.append(account)
            if len(matches) != 1:
                raise AccountScopeError(
                    f'{canary}.data.accounts: expected exactly one account named '
                    f'{account_name!r}; found {len(matches)}'
                )
            resolved_account = matches[0]
            account_number = _text(
                resolved_account.get('account_number'),
                f'{canary}.data.accounts matching '
                f'{account_name!r}.account_number',
            )
            agentic_allowed = resolved_account.get('agentic_allowed')
            if not isinstance(agentic_allowed, bool):
                raise AccountScopeError(
                    f'{canary}.data.accounts matching '
                    f'{account_name!r}.agentic_allowed: expected a boolean'
                )
            if not agentic_allowed:
                raise AccountScopeError(
                    f'{canary}.data.accounts matching {account_name!r}: '
                    'account is not accessible to this agent'
                )
        except AccountScopeError:
            raise
        except SnapshotError as exc:
            raise AccountScopeError(str(exc)) from exc
    except TransportAlreadyAttemptedError as exc:
        same_live_canary = (
            cleanup_candidate is not None
            and exc.marker_stable
            and exc.canary_instance == cleanup_candidate[1]
        )
        if not exc.marker_stable or same_live_canary:
            # An O_EXCL loser cannot inspect a marker that may still be in the
            # winner's write/fsync window.  A stable marker can identify the
            # exact live canary instance owned by an earlier caller.  Neither
            # case may unlink that shared file.  A later, different instance
            # is a prohibited sequential retry and is privacy-cleaned below.
            cleanup_candidate = None
        validation_error = exc
    except Exception as exc:
        validation_error = exc
    _remove_transport_canary(cleanup_candidate)
    if validation_error is not None:
        raise validation_error

    assert source_root is not None
    assert resolved_account is not None
    assert account_name is not None

    canary_sha256 = _sha256(raw)
    source_root_id = preparation['source_root_id']
    marker = _transport_marker_document(
        scratch_id=scratch_marker['scratch_id'],
        source_root=str(source_root),
        source_root_id=source_root_id,
        canary_sha256=canary_sha256,
    )
    root_marker = _transport_root_marker_document(
        scratch_id=scratch_marker['scratch_id'], source_root_id=source_root_id
    )
    prepared = _prepare_atomic_files(
        [str(source_root / TRANSPORT_ROOT_MARKER), str(scratch / TRANSPORT_MARKER)],
        [root_marker, marker],
    )
    _commit_atomic_files(prepared)
    _validated_transport_marker(scratch, scratch_marker)
    return {
        'schema_version': SCHEMA_VERSION,
        'action': 'bind-transport',
        'ok': True,
        'transport': TRANSPORT_KIND,
        'scratch': str(scratch),
        'scratch_id': scratch_marker['scratch_id'],
        'source_root': str(source_root),
        'canary_sha256': canary_sha256,
        'canary_removed': True,
        'account_name': account_name,
        'account_number': account_number,
        'agentic_allowed': agentic_allowed,
    }


def _validated_reservation_id(value: Any) -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise SourceHandoffError(
            'source_reservation_invalid',
            '--reservation-id must be the lowercase UUIDv4 from reserve-source',
        )
    return value


def _source_receipt_base(
    *,
    action: str,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    reservation: Mapping[str, Any],
) -> dict[str, Any]:
    receipt = {
        'schema_version': SCHEMA_VERSION,
        'action': action,
        'ok': True,
        'scratch': str(scratch),
        'scratch_id': scratch_marker['scratch_id'],
        'source_root': str(source_root),
        'source_root_id': transport_marker['source_root_id'],
        'purpose': reservation['purpose'],
        'reservation_id': reservation['reservation_id'],
        'source': str(source_root / reservation['source_filename']),
    }
    if 'first_request_cursor_count' in reservation:
        receipt.update({
            'first_request_cursor_count': int(
                reservation['first_request_cursor_count']
            ),
            'first_request_cursors_sha256': reservation[
                'first_request_cursors_sha256'
            ],
        })
    return receipt


def _committed_source_receipt(
    *,
    action: str,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
    reservation: Mapping[str, Any],
    terminal: Mapping[str, Any],
    idempotent: bool,
) -> dict[str, Any]:
    receipt = _source_receipt_base(
        action=action,
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        reservation=reservation,
    )
    receipt.update(
        {
            'status': 'committed',
            'source_sha256': terminal['source_sha256'],
            'source_size': int(terminal['source_size']),
            'idempotent': idempotent,
        }
    )
    return receipt


def _reserve_source(args: argparse.Namespace) -> dict[str, Any]:
    purpose = _validated_source_purpose(args.purpose)
    daily_loss_binding = _daily_loss_purpose_binding(purpose)
    retry_of = (
        _validated_source_purpose(args.retry_of, '--retry-of')
        if args.retry_of is not None
        else None
    )
    first_base = FIRST_POSITIONS_BASE_RE.fullmatch(purpose) is not None
    first_retry = FIRST_POSITIONS_RETRY_RE.fullmatch(purpose) is not None
    if (
        purpose.startswith(FIRST_POSITIONS_PURPOSE_PREFIX)
        and not first_base
        and not first_retry
    ):
        raise SourceHandoffError(
            'source_purpose_invalid',
            f'{purpose}: FIRST purpose must be first-positions-N or '
            'first-positions-N-retry with a canonical zero-based index',
        )
    if first_retry and retry_of is None:
        raise SourceHandoffError(
            'source_retry_not_authorized',
            f'{purpose}: FIRST retry reservation requires --retry-of',
        )
    if retry_of is not None and (
        not first_retry
        or FIRST_POSITIONS_BASE_RE.fullmatch(retry_of) is None
        or purpose != retry_of + '-retry'
    ):
        raise SourceHandoffError(
            'source_retry_not_authorized',
            f'{purpose}: --retry-of must name its canonical FIRST base purpose',
        )
    first_request_cursors = _validated_first_request_cursor_chain(
        args.first_request_cursor, purpose
    )
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(args.scratch)
    )
    reserve_lock = _acquire_source_reserve_lock(scratch, scratch_marker)
    active_error = False
    try:
        _reject_pending_source_handoff(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
        )
        if daily_loss_binding is not None:
            _validate_stage_generation_authorization(
                scratch, scratch_marker, daily_loss_binding[0]
            )
        if first_request_cursors is not None:
            _validate_first_prior_page_chain_in_context(
                scratch=scratch,
                scratch_marker=scratch_marker,
                transport_marker=transport_marker,
                source_root=source_root,
                request_cursors=first_request_cursors,
            )
        if retry_of is not None:
            base_reservation = _validate_source_retry_authorization_in_context(
                scratch=scratch,
                scratch_marker=scratch_marker,
                transport_marker=transport_marker,
                source_root=source_root,
                base_purpose=retry_of,
                retry_purpose=purpose,
            )
            if (
                first_request_cursors is None
                or base_reservation.get('first_request_cursor_count')
                != Decimal(len(first_request_cursors))
                or base_reservation.get('first_request_cursors_sha256')
                != _first_request_cursors_sha256(first_request_cursors)
            ):
                raise SourceHandoffError(
                    'source_retry_not_authorized',
                    f'{purpose}: retry cursor chain does not match its base',
                )
        receipt = _reserve_source_locked(
            purpose=purpose,
            first_request_cursors=first_request_cursors,
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
        )
        if retry_of is not None:
            receipt['retry_of'] = retry_of
        return receipt
    except BaseException:
        active_error = True
        raise
    finally:
        try:
            _release_source_reserve_lock(
                scratch, scratch_marker, reserve_lock
            )
        except Exception:
            # Preserve the actionable reservation failure when both the
            # operation and lock cleanup fail.  A retained lock still fences
            # every later reservation in this invocation.
            if not active_error:
                raise


def _reserve_source_locked(
    *,
    purpose: str,
    first_request_cursors: Sequence[str] | None,
    scratch: Path,
    scratch_marker: Mapping[str, Any],
    transport_marker: Mapping[str, Any],
    source_root: Path,
) -> dict[str, Any]:
    reservation_path = _source_reservation_marker_path(scratch, purpose)
    terminal_path = _source_terminal_marker_path(scratch, purpose)
    if os.path.lexists(reservation_path):
        reservation = _validated_source_reservation_at_path(
            reservation_path,
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            expected_purpose=purpose,
        )
        terminal = _validated_source_terminal(
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            reservation=reservation,
        )
        state = terminal['status'] if terminal is not None else 'reserved'
        raise SourceHandoffError(
            'source_purpose_duplicate',
            f'{purpose}: source purpose already has immutable state {state!r}',
        )
    if os.path.lexists(terminal_path):
        raise SourceHandoffError(
            'source_journal_invalid',
            f'{terminal_path}: orphan source-terminal marker',
        )

    reservation_id = ''
    source_path: Path | None = None
    for _attempt in range(4):
        candidate = str(uuid.uuid4())
        candidate_path = source_root / _source_filename(candidate)
        if not os.path.lexists(candidate_path):
            reservation_id = candidate
            source_path = candidate_path
            break
    if source_path is None:
        raise SourceHandoffError(
            'source_file_conflict',
            'could not allocate a unique response-source filename',
        )
    reservation = _source_reservation_document(
        scratch_id=scratch_marker['scratch_id'],
        source_root_id=transport_marker['source_root_id'],
        purpose=purpose,
        reservation_id=reservation_id,
        first_request_cursors=first_request_cursors,
    )
    try:
        _write_immutable_journal_marker(
            reservation_path, reservation, scratch
        )
    except FileExistsError as exc:
        # Another caller won this logical purpose.  Validate its marker so a
        # malformed collision cannot be mistaken for a normal duplicate.
        _validated_source_reservation_at_path(
            reservation_path,
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            expected_purpose=purpose,
        )
        raise SourceHandoffError(
            'source_purpose_duplicate',
            f'{purpose}: source purpose was reserved concurrently',
        ) from exc
    persisted = _validated_source_reservation_at_path(
        reservation_path,
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        expected_purpose=purpose,
    )
    receipt = _source_receipt_base(
        action='reserve-source',
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        reservation=persisted,
    )
    receipt.update({'status': 'reserved', 'idempotent': False})
    return receipt


def _commit_source(args: argparse.Namespace) -> dict[str, Any]:
    purpose = _validated_source_purpose(args.purpose)
    reservation_id = (
        _validated_reservation_id(args.reservation_id)
        if args.reservation_id is not None
        else None
    )
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(args.scratch)
    )
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=purpose,
    )
    if (
        reservation_id is not None
        and reservation['reservation_id'] != reservation_id
    ):
        raise SourceHandoffError(
            'source_reservation_mismatch',
            f'{purpose}: reservation id does not match the immutable journal',
        )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    if terminal is not None:
        if terminal['status'] != 'committed':
            raise SourceHandoffError(
                'source_handoff_aborted',
                f"{purpose}: source handoff was already aborted "
                f"({terminal['reason']})",
            )
        _verify_committed_source(source_root, reservation, terminal)
        return _committed_source_receipt(
            action='commit-source',
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            reservation=reservation,
            terminal=terminal,
            idempotent=True,
        )

    _source_path, _document, raw, identity = _read_reserved_source(
        source_root, reservation
    )
    intended_terminal = _source_committed_document(
        reservation=reservation,
        source_sha256=_sha256(raw),
        source_identity=identity,
    )
    terminal_path = _source_terminal_marker_path(scratch, purpose)
    created = True
    try:
        _write_immutable_journal_marker(
            terminal_path, intended_terminal, scratch
        )
    except FileExistsError:
        created = False
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    if terminal is None:
        raise SourceHandoffError(
            'source_journal_write_failed',
            f'{terminal_path}: terminal marker was not persisted',
        )
    if terminal['status'] != 'committed':
        raise SourceHandoffError(
            'source_handoff_aborted',
            f'{purpose}: an abort won the terminal-state race',
        )
    _verify_committed_source(source_root, reservation, terminal)
    return _committed_source_receipt(
        action='commit-source',
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        reservation=reservation,
        terminal=terminal,
        idempotent=not created,
    )


def _abort_source(args: argparse.Namespace) -> dict[str, Any]:
    purpose = _validated_source_purpose(args.purpose)
    reservation_id = _validated_reservation_id(args.reservation_id)
    reason = args.reason
    if reason not in SOURCE_ABORT_REASONS:
        raise SourceHandoffError(
            'source_abort_reason_invalid',
            '--reason must be connector-failed, serialization-failed, or '
            'file-change-failed',
        )
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(args.scratch)
    )
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=purpose,
    )
    if reservation['reservation_id'] != reservation_id:
        raise SourceHandoffError(
            'source_reservation_mismatch',
            f'{purpose}: reservation id does not match the immutable journal',
        )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    if terminal is not None:
        if terminal['status'] == 'committed':
            _verify_committed_source(source_root, reservation, terminal)
            raise SourceHandoffError(
                'source_handoff_committed',
                f'{purpose}: committed source cannot be aborted',
            )
        source_path = source_root / reservation['source_filename']
        if os.path.lexists(source_path):
            raise SourceHandoffError(
                'source_file_invalid',
                f'{source_path}: aborted source unexpectedly exists',
            )
        if terminal['reason'] != reason:
            raise SourceHandoffError(
                'source_handoff_conflict',
                f'{purpose}: abort reason does not match the immutable terminal '
                'marker',
            )
        receipt = _source_receipt_base(
            action='abort-source',
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            reservation=reservation,
        )
        receipt.update(
            {
                'status': 'aborted',
                'reason': reason,
                'source': None,
                'idempotent': True,
            }
        )
        return receipt

    source_path = source_root / reservation['source_filename']
    if os.path.lexists(source_path):
        # Do not permit abort to discard a response that may already have been
        # written.  Only commit-source may inspect and seal this state.
        raise SourceHandoffError(
            'source_commit_required',
            f'{purpose}: reserved source exists; commit is the only permitted '
            'terminal action',
        )
    intended_terminal = _source_aborted_document(
        reservation=reservation, reason=reason
    )
    terminal_path = _source_terminal_marker_path(scratch, purpose)
    created = True
    try:
        _write_immutable_journal_marker(
            terminal_path, intended_terminal, scratch
        )
    except FileExistsError:
        created = False
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    if terminal is None:
        raise SourceHandoffError(
            'source_journal_write_failed',
            f'{terminal_path}: terminal marker was not persisted',
        )
    if terminal['status'] != 'aborted':
        _verify_committed_source(source_root, reservation, terminal)
        raise SourceHandoffError(
            'source_handoff_committed',
            f'{purpose}: a commit won the terminal-state race',
        )
    if terminal['reason'] != reason:
        raise SourceHandoffError(
            'source_handoff_conflict',
            f'{purpose}: concurrent abort used a different reason',
        )
    if os.path.lexists(source_path):
        raise SourceHandoffError(
            'source_file_invalid',
            f'{source_path}: source appeared after the abort terminal was sealed',
        )
    receipt = _source_receipt_base(
        action='abort-source',
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        reservation=reservation,
    )
    receipt.update(
        {
            'status': 'aborted',
            'reason': reason,
            'source': None,
            'idempotent': not created,
        }
    )
    return receipt


def _lookup_source(args: argparse.Namespace) -> dict[str, Any]:
    purpose = _validated_source_purpose(args.purpose)
    scratch, scratch_marker, transport_marker, source_root = (
        _validated_source_journal_context(args.scratch)
    )
    reservation = _validated_source_reservation(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        purpose=purpose,
    )
    terminal = _validated_source_terminal(
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        reservation=reservation,
    )
    source_path = source_root / reservation['source_filename']
    if terminal is None:
        if os.path.lexists(source_path):
            _read_reserved_source(source_root, reservation)
            receipt = _source_receipt_base(
                action='lookup-source',
                scratch=scratch,
                scratch_marker=scratch_marker,
                transport_marker=transport_marker,
                source_root=source_root,
                reservation=reservation,
            )
            receipt.update(
                {
                    'status': 'reserved',
                    'recovery_action': 'commit-only',
                    'idempotent': True,
                }
            )
            return receipt
        receipt = _source_receipt_base(
            action='lookup-source',
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            reservation=reservation,
        )
        receipt.update(
            {
                'status': 'reserved',
                'source': None,
                'recovery_action': 'halt',
                'idempotent': True,
            }
        )
        return receipt
    if terminal['status'] == 'aborted':
        if os.path.lexists(source_path):
            raise SourceHandoffError(
                'source_file_invalid',
                f'{source_path}: aborted source unexpectedly exists',
            )
        receipt = _source_receipt_base(
            action='lookup-source',
            scratch=scratch,
            scratch_marker=scratch_marker,
            transport_marker=transport_marker,
            source_root=source_root,
            reservation=reservation,
        )
        receipt.update(
            {
                'status': 'aborted',
                'reason': terminal['reason'],
                'source': None,
                'recovery_action': 'none',
                'idempotent': True,
            }
        )
        return receipt
    _verify_committed_source(source_root, reservation, terminal)
    receipt = _committed_source_receipt(
        action='lookup-source',
        scratch=scratch,
        scratch_marker=scratch_marker,
        transport_marker=transport_marker,
        source_root=source_root,
        reservation=reservation,
        terminal=terminal,
        idempotent=True,
    )
    receipt['recovery_action'] = 'consume'
    return receipt


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    preflight = subparsers.add_parser(
        "preflight", help="create or verify session scratch I/O"
    )
    scratch_mode = preflight.add_mutually_exclusive_group(required=True)
    scratch_mode.add_argument("--scratch")
    scratch_mode.add_argument("--create-scratch", action="store_true")

    bind_transport = subparsers.add_parser(
        'bind-transport', help='bind one invocation-wide response-source transport'
    )
    bind_transport.add_argument('--scratch', required=True)
    bind_transport.add_argument('--source-root', required=True)
    bind_transport.add_argument('--canary', required=True)
    bind_transport.add_argument('--account-name', required=True)

    reserve_source = subparsers.add_parser(
        'reserve-source',
        help='reserve one immutable logical response-source handoff',
    )
    reserve_source.add_argument('--scratch', required=True)
    reserve_source.add_argument('--purpose', required=True)
    reserve_source.add_argument(
        '--retry-of',
        help='authorize PURPOSE-retry from an aborted connector-failed PURPOSE',
    )
    reserve_source.add_argument(
        '--first-request-cursor',
        action='append',
        help='repeat the complete FIRST positions cursor chain before its read',
    )

    commit_source = subparsers.add_parser(
        'commit-source',
        help='seal the exact JSON file written for a source reservation',
    )
    commit_source.add_argument('--scratch', required=True)
    commit_source.add_argument('--purpose', required=True)
    commit_source.add_argument(
        '--reservation-id',
        help=(
            'optional compatibility check; when omitted, commit the immutable '
            'reservation bound to --scratch and --purpose'
        ),
    )

    abort_source = subparsers.add_parser(
        'abort-source',
        help='seal an absent response source as an explicit failed handoff',
    )
    abort_source.add_argument('--scratch', required=True)
    abort_source.add_argument('--purpose', required=True)
    abort_source.add_argument('--reservation-id', required=True)
    abort_source.add_argument('--reason', choices=sorted(SOURCE_ABORT_REASONS), required=True)

    lookup_source = subparsers.add_parser(
        'lookup-source',
        help='verify and recover one terminal source-handoff receipt',
    )
    lookup_source.add_argument('--scratch', required=True)
    lookup_source.add_argument('--purpose', required=True)

    authorize_generation_b = subparsers.add_parser(
        'authorize-generation-b',
        help='persist the one DAILY-LOSS semantic retry authorization',
    )
    authorize_generation_b.add_argument('--scratch', required=True)

    finish_generation_b = subparsers.add_parser(
        'finish-generation-b',
        help='persist the terminal outcome of the one DAILY-LOSS B generation',
    )
    finish_generation_b.add_argument('--scratch', required=True)
    finish_generation_b.add_argument(
        '--outcome', choices=sorted(STAGE_RETRY_OUTCOMES), required=True
    )

    stage = subparsers.add_parser("stage", help="unwrap, validate, and stage snapshots")
    stage.add_argument("--kind", choices=SNAPSHOT_KINDS, required=True)
    stage.add_argument("--generation", choices=("A", "B"), required=True)
    stage_source = stage.add_mutually_exclusive_group(required=True)
    stage_source.add_argument("--source", action="append", metavar="FILE")
    stage_source.add_argument(
        "--source-purpose", action="append", metavar="PURPOSE",
        help="repeat committed response-source purposes in request order",
    )
    stage_output = stage.add_mutually_exclusive_group(required=True)
    stage_output.add_argument("--output", action="append", metavar="FILE")
    stage_output.add_argument(
        "--auto-output-scratch",
        metavar="SCRATCH",
        help="allocate one fresh direct-child output per source in SCRATCH",
    )
    stage.add_argument(
        "--request-cursor",
        action="append",
        metavar="FIRST_OR_CURSOR",
        help="repeat for positions/orders pages in request order",
    )
    stage.add_argument(
        "--allow-more",
        action="store_true",
        help="allow the last supplied positions/orders page to have a next cursor",
    )
    return parser


def _error_result(
    action: str,
    exc: Exception,
    *,
    stage_kind: str | None = None,
    stage_generation: str | None = None,
) -> dict[str, Any]:
    if isinstance(exc, CliError):
        code = "usage_error"
    elif isinstance(exc, SourceHandoffError):
        code = exc.code
    elif isinstance(exc, StageError):
        code = exc.code
    elif isinstance(exc, ScratchCreateError):
        code = "scratch_create_failed"
    elif isinstance(exc, AccountScopeError):
        code = "account_scope_failed"
    else:
        code = "invalid_snapshot"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": False,
        "error": {
            "code": code,
            "message": str(exc),
        },
    }
    if (
        action == "stage"
        and stage_kind in SNAPSHOT_KINDS
        and stage_generation in {"A", "B"}
    ):
        result["kind"] = stage_kind
        result["generation"] = stage_generation
        if isinstance(exc, StageSemanticError):
            result["recovery_action"] = exc.recovery_action
    return result


def _print_json(result: Mapping[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _reject_hyphenated_stage_action(arguments: Sequence[str]) -> None:
    if not arguments or not arguments[0].startswith("stage-"):
        return
    candidate = arguments[0]
    kind = candidate[len("stage-") :]
    if kind in SNAPSHOT_KINDS:
        raise CliError(
            f"invalid action {candidate!r}: the kind is a separate argument, "
            f"so use 'stage --kind {kind}'"
        )
    raise CliError(
        f"invalid action {candidate!r}: {kind!r} is not a staged kind and "
        f"the kind is a separate argument. Staged kinds are "
        f"{', '.join(SNAPSHOT_KINDS)}. Historicals results, RSI inputs, and "
        "quote inputs are never staged through this helper: reserve, persist, "
        "and commit the raw response through the bound source journal, then "
        "pass its purpose to evaluate_candidates.py"
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = (
        arguments[0]
        if arguments and arguments[0] in {
            'preflight', 'bind-transport', 'reserve-source', 'commit-source',
            'abort-source', 'lookup-source', 'authorize-generation-b',
            'finish-generation-b', 'stage',
        }
        else "unknown"
    )
    args: argparse.Namespace | None = None
    try:
        _reject_hyphenated_stage_action(arguments)
        args = _parser().parse_args(arguments)
        if args.action == "preflight":
            result = _preflight(args)
        elif args.action == 'bind-transport':
            result = _bind_transport(args)
        elif args.action == 'reserve-source':
            result = _reserve_source(args)
        elif args.action == 'commit-source':
            result = _commit_source(args)
        elif args.action == 'abort-source':
            result = _abort_source(args)
        elif args.action == 'lookup-source':
            result = _lookup_source(args)
        elif args.action == 'authorize-generation-b':
            result = _authorize_generation_b(args)
        elif args.action == 'finish-generation-b':
            result = _finish_generation_b(args)
        else:
            result = _stage(args)
        _print_json(result)
        return 0
    except (SnapshotError, OSError) as exc:
        _print_json(
            _error_result(
                action,
                exc,
                stage_kind=(
                    args.kind if action == "stage" and args is not None else None
                ),
                stage_generation=(
                    args.generation
                    if action == "stage" and args is not None else None
                ),
            )
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
