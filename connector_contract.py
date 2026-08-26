#!/usr/bin/env python3
"""Validate small Robinhood connector contracts deterministically.

The trading routine receives broker tool results through the immutable source
journal owned by :mod:`broker_snapshot`.  This helper consumes one committed
source purpose and emits a compact receipt for the connector shapes that
must otherwise be interpreted by an LLM:

``portfolio``
    Normalize the authoritative account totals to exact decimal strings.

``quote``
    Normalize exactly one requested equity quote and deterministically compare
    its current trade price with its official previous close.

``page``
    Validate one committed positions/orders page and normalize its pagination
    state.  In particular, an omitted or empty ``data.next`` is a valid
    terminal page, not connector-schema failure.  It binds the complete
    request-cursor chain, rejects repeated cursors and a chain over 1,000
    pages, and never returns position rows or staged-file paths.

``first-positions-set``
    Validate FIRST's complete ordered positions pagination as one set, bind
    its request/next cursor chain, reject symbols duplicated across pages,
    prove any ``-retry`` purpose follows an aborted connector failure, and
    return one compact validated positions projection.

``scan``
    Resolve one saved scan by exact title and validate its visible columns and
    scalar sort description.

``scan-update``
    Bind an ``update_scan_config`` response to the expected scan id and expose
    whether its authoritative ``data.result.sorted_by`` value is correct.

Every invocation prints exactly one JSON object.  The helper is stdlib-only;
it never calls Robinhood and never mutates the saved scan or brokerage account.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from broker_snapshot import (
    SnapshotError,
    SourceHandoffError,
    _unwrap_source,
    _validate_orders,
    _validate_positions,
    validate_bound_external_json_purposes,
    validate_bound_first_positions_page_request_binding,
    validate_bound_first_positions_request_binding,
    validate_bound_source_retry_authorization,
)


SCHEMA_VERSION = 1
MAX_PAGE_COUNT = 1000
FIRST_POSITIONS_PURPOSE_PREFIX = "first-positions-"
FIRST_POSITIONS_PURPOSE_RE = re.compile(
    r"^first-positions-(0|[1-9][0-9]*)(?:-retry)?$"
)
FIRST_POSITIONS_RETRY_RE = re.compile(
    r"^first-positions-(?:0|[1-9][0-9]*)-retry$"
)
REQUIRED_SCAN_COLUMNS = (
    "Last",
    "Relative volume",
    "% Change",
    "Volume",
)
REQUIRED_SCAN_SORT = "Relative volume desc"


class ContractError(ValueError):
    """Raised when a committed connector response violates its contract."""


class CliError(ContractError):
    """Raised for command-line errors that must be returned as JSON."""


class SourceUnavailableError(ContractError):
    """Raised when the committed source journal cannot supply exact bytes."""


class SourceJournalError(ContractError):
    """Preserve one exact deterministic source-journal error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RequestBindingError(ContractError):
    """Raised when runner-owned cursor or FIRST-purpose arguments are invalid."""


class PaginationStopError(ContractError):
    """Raised when a returned cursor cycles or exceeds the safety ceiling."""


def _validated_first_positions_purpose(value: Any, context: str) -> str:
    purpose = _nonempty_text(value, context)
    if FIRST_POSITIONS_PURPOSE_RE.fullmatch(purpose) is None:
        raise RequestBindingError(
            f"{context}: expected canonical first-positions-N or "
            "first-positions-N-retry purpose"
        )
    return purpose


def _validate_first_page_index(
    purpose: str, request_cursors: Sequence[str]
) -> None:
    match = FIRST_POSITIONS_PURPOSE_RE.fullmatch(purpose)
    if match is None:  # The namespace validator owns the detailed error.
        _validated_first_positions_purpose(purpose, "--source-purpose")
        raise AssertionError("unreachable FIRST purpose validation")
    expected_index = len(request_cursors) - 1
    if int(match.group(1)) != expected_index:
        raise RequestBindingError(
            "--source-purpose: FIRST page index must equal the zero-based "
            "request-cursor position"
        )


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliError(message)


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError(f"{context}: expected an object")
    return value


def _nonempty_text(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ContractError(f"{context}: expected a nonempty string")
    return value


def _nonnegative_decimal_string(value: Any, context: str) -> str:
    """Return one exact finite nonnegative value as a JSON-safe string."""

    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ContractError(
            f"{context}: expected an exact finite nonnegative decimal"
        )
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
        raise ContractError(
            f"{context}: expected an exact finite nonnegative decimal"
        ) from exc
    if not number.is_finite():
        raise ContractError(f"{context}: must be finite")
    if number < 0:
        raise ContractError(f"{context}: must not be negative")
    # Decimal preserves all significant input digits and avoids converting an
    # exact broker string through binary floating point.  Collapse signed zero
    # because -0 and 0 have the same nonnegative account meaning.
    return "0" if number == 0 else str(number)


def _finite_decimal_string(
    value: Any, context: str, *, positive: bool = False
) -> str:
    """Return one exact finite decimal without binary-float coercion."""

    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise ContractError(f"{context}: expected an exact finite decimal")
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
        raise ContractError(f"{context}: expected an exact finite decimal") from exc
    if not number.is_finite():
        raise ContractError(f"{context}: must be finite")
    if positive and number <= 0:
        raise ContractError(f"{context}: must be positive")
    return "0" if number == 0 else str(number)


def normalize_portfolio(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the normalized authoritative portfolio receipt."""

    data = _mapping(payload.get("data"), "portfolio.data")
    values: dict[str, str] = {}
    for field in ("total_value", "cash", "equity_value"):
        if field not in data:
            raise ContractError(f"portfolio.data.{field}: missing")
        values[field] = _nonnegative_decimal_string(
            data[field], f"portfolio.data.{field}"
        )

    if "buying_power" not in data:
        raise ContractError("portfolio.data.buying_power: missing")
    raw_buying_power = data["buying_power"]
    if isinstance(raw_buying_power, Mapping):
        if "buying_power" not in raw_buying_power:
            raise ContractError(
                "portfolio.data.buying_power.buying_power: missing"
            )
        raw_buying_power = raw_buying_power["buying_power"]
        buying_power_context = "portfolio.data.buying_power.buying_power"
    else:
        # Retain compatibility with the older connector contract, which
        # exposed the authoritative amount directly at data.buying_power.
        buying_power_context = "portfolio.data.buying_power"
    values["buying_power"] = _nonnegative_decimal_string(
        raw_buying_power, buying_power_context
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "portfolio",
        "ok": True,
        "values": values,
    }


def inspect_quote(payload: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    """Normalize one requested quote and own the previous-close comparison."""

    expected_symbol = _nonempty_text(symbol, "--symbol")
    if expected_symbol != expected_symbol.upper():
        raise ContractError("--symbol: expected an uppercase symbol")

    data = _mapping(payload.get("data"), "quote.data")
    results = data.get("results")
    if not isinstance(results, list):
        raise ContractError("quote.data.results: expected an array")
    if len(results) != 1:
        raise ContractError("quote.data.results: expected exactly one result")

    result = _mapping(results[0], "quote.data.results[0]")
    quote = _mapping(result.get("quote"), "quote.data.results[0].quote")
    actual_symbol = _nonempty_text(
        quote.get("symbol"), "quote.data.results[0].quote.symbol"
    )
    if actual_symbol != expected_symbol:
        raise ContractError(
            "quote.data.results[0].quote.symbol: does not match --symbol"
        )

    current_text = _finite_decimal_string(
        quote.get("last_trade_price"),
        "quote.data.results[0].quote.last_trade_price",
        positive=True,
    )
    previous_text = _finite_decimal_string(
        quote.get("previous_close"),
        "quote.data.results[0].quote.previous_close",
        positive=True,
    )
    current = Decimal(current_text)
    previous = Decimal(previous_text)
    change_percent = (current - previous) * Decimal(100) / previous
    display = change_percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if display == 0:
        display = abs(display)

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "quote",
        "ok": True,
        "symbol": actual_symbol,
        "current_price": current_text,
        "previous_close": previous_text,
        "change_percent": _finite_decimal_string(change_percent, "quote change"),
        "change_percent_display": format(display, ".2f"),
        "below_previous_close": current < previous,
    }


def _validated_request_cursor_chain(
    request_cursors: Sequence[str], context: str
) -> list[str]:
    try:
        cursors = [
            _nonempty_text(value, f"--request-cursor[{index}]")
            for index, value in enumerate(request_cursors)
        ]
    except ContractError as exc:
        raise RequestBindingError(str(exc)) from exc
    if not cursors:
        raise RequestBindingError(
            f"{context}: expected at least one request cursor"
        )
    if len(cursors) > MAX_PAGE_COUNT:
        raise RequestBindingError(
            f"{context}: page count exceeds the {MAX_PAGE_COUNT}-page limit"
        )
    if cursors[0] != "FIRST":
        raise RequestBindingError(
            f"{context}: first request cursor must be FIRST"
        )
    if len(set(cursors)) != len(cursors):
        raise RequestBindingError(f"{context}: repeated request cursor")
    return cursors


def inspect_page(
    payload: Mapping[str, Any],
    kind: str,
    source_purpose: str,
    request_cursors: Sequence[str],
) -> dict[str, Any]:
    """Validate one paginated connector page and normalize its cursor state."""

    expected_kind = _nonempty_text(kind, "--kind")
    expected_purpose = _nonempty_text(source_purpose, "--source-purpose")
    if expected_purpose.startswith(FIRST_POSITIONS_PURPOSE_PREFIX):
        if expected_kind != "positions":
            raise RequestBindingError(
                "--source-purpose: FIRST positions namespace requires --kind positions"
            )
        expected_purpose = _validated_first_positions_purpose(
            expected_purpose, "--source-purpose"
        )
    cursors = _validated_request_cursor_chain(request_cursors, "page")
    if expected_purpose.startswith(FIRST_POSITIONS_PURPOSE_PREFIX):
        _validate_first_page_index(expected_purpose, cursors)
    try:
        if expected_kind == "positions":
            metadata = _validate_positions([payload])
        elif expected_kind == "orders":
            metadata = _validate_orders([payload])
        else:
            raise ContractError("--kind: expected positions or orders")
    except SnapshotError as exc:
        raise ContractError(str(exc)) from exc
    if len(metadata) != 1:
        raise ContractError(f"{expected_kind}: expected exactly one page")
    page = metadata[0]
    row_count = page.get("row_count")
    next_cursor = page.get("next_cursor")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 0
        or (
            next_cursor is not None
            and (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor != next_cursor.strip()
            )
        )
    ):
        raise ContractError(f"{expected_kind}: invalid normalized page metadata")
    if next_cursor in set(cursors):
        raise PaginationStopError(
            f"{expected_kind}: next cursor repeats an already requested cursor"
        )
    if len(cursors) == MAX_PAGE_COUNT and next_cursor is not None:
        raise PaginationStopError(
            f"{expected_kind}: continuation exceeds the {MAX_PAGE_COUNT}-page limit"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "page",
        "ok": True,
        "kind": expected_kind,
        "source_purpose": expected_purpose,
        "request_cursor": cursors[-1],
        "request_cursors": cursors,
        "row_count": row_count,
        "next_cursor": next_cursor,
        "complete": next_cursor is None,
    }


def _project_position_rows(
    payloads: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    """Return only the exact FIRST fields from already validated pages."""

    rows: list[dict[str, str]] = []
    for page_index, payload in enumerate(payloads, 1):
        data = _mapping(payload.get("data"), f"positions page {page_index}.data")
        raw_rows = data.get("positions")
        if not isinstance(raw_rows, list):  # Explicit for type narrowing.
            raise ContractError(
                f"positions page {page_index}.data.positions: expected an array"
            )
        for row_index, value in enumerate(raw_rows, 1):
            context = f"positions page {page_index} row {row_index}"
            row = _mapping(value, context)
            symbol = _nonempty_text(row.get("symbol"), f"{context}.symbol")
            if symbol != symbol.upper():
                raise ContractError(
                    f"{context}.symbol: expected uppercase symbol"
                )
            if "average_buy_price" not in row:
                raise ContractError(
                    f"position {symbol}.average_buy_price: missing"
                )
            rows.append(
                {
                    "symbol": symbol,
                    "quantity": _finite_decimal_string(
                        row.get("quantity"),
                        f"position {symbol}.quantity",
                        positive=True,
                    ),
                    "intraday_quantity": _finite_decimal_string(
                        row.get("intraday_quantity"),
                        f"position {symbol}.intraday_quantity",
                    ),
                    "average_buy_price": _finite_decimal_string(
                        row["average_buy_price"],
                        f"position {symbol}.average_buy_price",
                        positive=True,
                    ),
                }
            )
    return rows


def inspect_first_positions_set(
    payloads: Sequence[Mapping[str, Any]],
    source_purposes: Sequence[str],
    request_cursors: Sequence[str],
) -> dict[str, Any]:
    """Validate and project FIRST's complete ordered positions set."""

    if not payloads:
        raise ContractError("first-positions-set: expected at least one page")
    purposes = [
        _nonempty_text(value, f"--source-purpose[{index}]")
        for index, value in enumerate(source_purposes)
    ]
    cursors = _validated_request_cursor_chain(
        request_cursors, "first-positions-set"
    )
    if len(payloads) != len(purposes) or len(payloads) != len(cursors):
        raise ContractError(
            "first-positions-set: payload, source-purpose, and request-cursor "
            "counts differ"
        )
    if len(set(purposes)) != len(purposes):
        raise ContractError("first-positions-set: duplicate source purpose")
    for index, purpose in enumerate(purposes):
        base_purpose = f"first-positions-{index}"
        if purpose not in {base_purpose, base_purpose + "-retry"}:
            raise ContractError(
                "first-positions-set: source purposes must belong to the "
                "ordered FIRST page namespace first-positions-0..N with only "
                "the exact optional -retry suffix"
            )
    try:
        metadata = _validate_positions(payloads)
    except SnapshotError as exc:
        raise ContractError(str(exc)) from exc
    for index, page in enumerate(metadata):
        next_cursor = page["next_cursor"]
        if index + 1 < len(metadata):
            if next_cursor is None:
                raise ContractError(
                    f"first-positions-set page {index + 1}: terminal page has "
                    "a successor"
                )
            if cursors[index + 1] != next_cursor:
                raise ContractError(
                    f"first-positions-set page {index + 2}: request cursor "
                    "mismatch"
                )
        elif next_cursor is not None:
            raise ContractError(
                "first-positions-set: final page is not terminal"
            )
    rows = _project_position_rows(payloads)
    row_count = sum(page["row_count"] for page in metadata)
    if len(rows) != row_count:
        raise ContractError(
            "first-positions-set: projected row count mismatch"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "first-positions-set",
        "ok": True,
        "source_purposes": purposes,
        "request_cursors": cursors,
        "page_count": len(payloads),
        "row_count": row_count,
        "complete": True,
        "rows": rows,
    }


def inspect_scan(payload: Mapping[str, Any], title: str) -> dict[str, Any]:
    """Resolve and validate one saved scan without mutating it."""

    expected_title = _nonempty_text(title, "--title")
    data = _mapping(payload.get("data"), "scan.data")
    scans = data.get("scans")
    if not isinstance(scans, list):
        raise ContractError("scan.data.scans: expected an array")

    matches: list[Mapping[str, Any]] = []
    for index, value in enumerate(scans):
        scan = _mapping(value, f"scan.data.scans[{index}]")
        scan_title = scan.get("title")
        if not isinstance(scan_title, str):
            raise ContractError(
                f"scan.data.scans[{index}].title: expected a string"
            )
        if scan_title == expected_title:
            matches.append(scan)
    if len(matches) > 1:
        raise ContractError(
            f"scan.data.scans: multiple scans have title {expected_title!r}"
        )

    if not matches:
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "scan",
            "ok": True,
            "title": expected_title,
            "found": False,
            "scan_id": None,
            "visible_columns": [],
            "missing_columns": list(REQUIRED_SCAN_COLUMNS),
            "columns_valid": False,
            "sorting": None,
            "sort_valid": False,
            "cortex_managed": None,
            "needs_sort_update": False,
            "entry_ready": False,
        }

    scan = matches[0]
    scan_id = _nonempty_text(scan.get("scan_id"), "saved scan.scan_id")
    columns = scan.get("columns")
    if not isinstance(columns, list):
        raise ContractError("saved scan.columns: expected an array")
    visible_columns: list[str] = []
    seen_columns: set[str] = set()
    for index, value in enumerate(columns):
        column = _mapping(value, f"saved scan.columns[{index}]")
        name = _nonempty_text(
            column.get("display_name"),
            f"saved scan.columns[{index}].display_name",
        )
        visible = column.get("visible")
        if not isinstance(visible, bool):
            raise ContractError(
                f"saved scan.columns[{index}].visible: expected a boolean"
            )
        if name in seen_columns:
            raise ContractError(f"saved scan.columns: duplicate column {name!r}")
        seen_columns.add(name)
        if visible:
            visible_columns.append(name)

    missing_columns = [
        name for name in REQUIRED_SCAN_COLUMNS if name not in visible_columns
    ]
    columns_valid = not missing_columns

    sorting = scan.get("sorting")
    if sorting is not None and not isinstance(sorting, str):
        raise ContractError("saved scan.sorting: expected a string or null")
    if isinstance(sorting, str) and (
        not sorting or sorting != sorting.strip()
    ):
        raise ContractError("saved scan.sorting: expected a nonempty scalar")
    sort_valid = sorting == REQUIRED_SCAN_SORT

    cortex_managed = scan.get("cortex_managed")
    if not isinstance(cortex_managed, bool):
        raise ContractError("saved scan.cortex_managed: expected a boolean")
    needs_sort_update = columns_valid and not sort_valid and not cortex_managed

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "scan",
        "ok": True,
        "title": expected_title,
        "found": True,
        "scan_id": scan_id,
        "visible_columns": visible_columns,
        "missing_columns": missing_columns,
        "columns_valid": columns_valid,
        "sorting": sorting,
        "sort_valid": sort_valid,
        "cortex_managed": cortex_managed,
        "needs_sort_update": needs_sort_update,
        "entry_ready": columns_valid and sort_valid,
    }


def inspect_scan_update(
    payload: Mapping[str, Any], expected_scan_id: str
) -> dict[str, Any]:
    """Validate one saved-scan update response against the intended scan."""

    intended_scan_id = _nonempty_text(expected_scan_id, "--scan-id")
    data = _mapping(payload.get("data"), "scan update.data")
    result = _mapping(data.get("result"), "scan update.data.result")
    scan_id = _nonempty_text(
        result.get("scan_id"), "scan update.data.result.scan_id"
    )
    if scan_id != intended_scan_id:
        raise ContractError(
            "scan update.data.result.scan_id: does not match the expected scan"
        )
    sorting = result.get("sorted_by")
    if not isinstance(sorting, str) or not sorting or sorting != sorting.strip():
        raise ContractError(
            "scan update.data.result.sorted_by: expected a nonempty scalar"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "scan-update",
        "ok": True,
        "scan_id": scan_id,
        "sorting": sorting,
        "sort_valid": sorting == REQUIRED_SCAN_SORT,
    }


def _load_payloads(
    scratch: str, source_purposes: Sequence[str]
) -> list[Mapping[str, Any]]:
    try:
        validated = validate_bound_external_json_purposes(
            scratch, list(source_purposes)
        )
    except SourceHandoffError as exc:
        raise SourceJournalError(exc.code, str(exc)) from exc
    except (SnapshotError, OSError) as exc:
        raise SourceUnavailableError(str(exc)) from exc
    if len(validated) != len(source_purposes):
        raise SourceUnavailableError(
            "committed source-purpose count does not match the request"
        )
    payloads: list[Mapping[str, Any]] = []
    for source_purpose, (_path, document, _raw) in zip(
        source_purposes, validated
    ):
        payload, _envelope = _unwrap_source(
            document, f"source purpose {source_purpose!r}"
        )
        payloads.append(payload)
    return payloads


def _load_payload(scratch: str, source_purpose: str) -> Mapping[str, Any]:
    return _load_payloads(scratch, [source_purpose])[0]


def _validate_first_retry_authorizations(
    scratch: str, source_purposes: Sequence[str]
) -> None:
    for index, value in enumerate(source_purposes):
        purpose = _validated_first_positions_purpose(
            value, f"--source-purpose[{index}]"
        )
        if FIRST_POSITIONS_RETRY_RE.fullmatch(purpose) is None:
            continue
        base_purpose = purpose[:-len("-retry")]
        try:
            validate_bound_source_retry_authorization(
                scratch, base_purpose, purpose
            )
        except SourceHandoffError as exc:
            raise SourceJournalError(exc.code, str(exc)) from exc
        except (SnapshotError, OSError) as exc:
            raise SourceUnavailableError(str(exc)) from exc


def _validate_first_journal_cursor_binding(
    scratch: str,
    source_purposes: Sequence[str],
    request_cursors: Sequence[str],
) -> None:
    try:
        validate_bound_first_positions_request_binding(
            scratch, source_purposes, request_cursors
        )
    except SourceHandoffError as exc:
        if exc.code == "request_binding_invalid":
            raise RequestBindingError(str(exc)) from exc
        raise SourceJournalError(exc.code, str(exc)) from exc
    except (SnapshotError, OSError) as exc:
        raise SourceUnavailableError(str(exc)) from exc


def _validate_first_page_journal_cursor_binding(
    scratch: str,
    source_purpose: str,
    request_cursors: Sequence[str],
) -> None:
    try:
        validate_bound_first_positions_page_request_binding(
            scratch, source_purpose, request_cursors
        )
    except SourceHandoffError as exc:
        if exc.code == "request_binding_invalid":
            raise RequestBindingError(str(exc)) from exc
        raise SourceJournalError(exc.code, str(exc)) from exc
    except (SnapshotError, OSError) as exc:
        raise SourceUnavailableError(str(exc)) from exc


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    portfolio = subparsers.add_parser(
        "portfolio", help="normalize one committed portfolio response"
    )
    portfolio.add_argument("--scratch", required=True)
    portfolio.add_argument("--source-purpose", required=True)

    quote = subparsers.add_parser(
        "quote", help="normalize one committed quote response"
    )
    quote.add_argument("--scratch", required=True)
    quote.add_argument("--source-purpose", required=True)
    quote.add_argument("--symbol", required=True)

    page = subparsers.add_parser(
        "page", help="inspect one committed positions/orders page"
    )
    page.add_argument("--scratch", required=True)
    page.add_argument("--source-purpose", required=True)
    page.add_argument("--kind", choices=("positions", "orders"), required=True)
    page.add_argument("--request-cursor", action="append", required=True)

    first_positions_set = subparsers.add_parser(
        "first-positions-set",
        help="validate and project FIRST's complete positions set",
    )
    first_positions_set.add_argument("--scratch", required=True)
    first_positions_set.add_argument(
        "--source-purpose", action="append", required=True
    )
    first_positions_set.add_argument(
        "--request-cursor", action="append", required=True
    )

    scan = subparsers.add_parser(
        "scan", help="inspect one committed get_scans response"
    )
    scan.add_argument("--scratch", required=True)
    scan.add_argument("--source-purpose", required=True)
    scan.add_argument("--title", required=True)

    scan_update = subparsers.add_parser(
        "scan-update", help="validate one committed update_scan_config response"
    )
    scan_update.add_argument("--scratch", required=True)
    scan_update.add_argument("--source-purpose", required=True)
    scan_update.add_argument("--scan-id", required=True)
    return parser


def _error_result(action: str, exc: Exception) -> dict[str, Any]:
    code = (
        "usage_error"
        if isinstance(exc, CliError)
        else "request_binding_invalid"
        if isinstance(exc, RequestBindingError)
        else "pagination_stopped"
        if isinstance(exc, PaginationStopError)
        else exc.code
        if isinstance(exc, SourceJournalError)
        else "source_unavailable"
        if isinstance(exc, SourceUnavailableError)
        else "invalid_contract"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": False,
        "error": {
            "code": code,
            "message": str(exc),
        },
    }


def _print_json(result: Mapping[str, Any]) -> None:
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = (
        arguments[0]
        if arguments
        and arguments[0]
        in {
            "portfolio",
            "quote",
            "page",
            "first-positions-set",
            "scan",
            "scan-update",
        }
        else "unknown"
    )
    try:
        args = _parser().parse_args(arguments)
        if args.action == "first-positions-set":
            first_cursors = _validated_request_cursor_chain(
                args.request_cursor, "first-positions-set"
            )
            _validate_first_journal_cursor_binding(
                args.scratch, args.source_purpose, first_cursors
            )
            _validate_first_retry_authorizations(
                args.scratch, args.source_purpose
            )
            payloads = _load_payloads(args.scratch, args.source_purpose)
            result = inspect_first_positions_set(
                payloads, args.source_purpose, args.request_cursor
            )
        else:
            if (
                args.action == "page"
                and args.source_purpose.startswith("first-positions-")
            ):
                if args.kind != "positions":
                    raise RequestBindingError(
                        "--source-purpose: FIRST positions namespace requires "
                        "--kind positions"
                    )
                first_cursors = _validated_request_cursor_chain(
                    args.request_cursor, "page"
                )
                first_purpose = _validated_first_positions_purpose(
                    args.source_purpose, "--source-purpose"
                )
                _validate_first_page_index(first_purpose, first_cursors)
                _validate_first_retry_authorizations(
                    args.scratch, [args.source_purpose]
                )
                _validate_first_page_journal_cursor_binding(
                    args.scratch, args.source_purpose, first_cursors
                )
            payload = _load_payload(args.scratch, args.source_purpose)
            if args.action == "portfolio":
                result = normalize_portfolio(payload)
            elif args.action == "quote":
                result = inspect_quote(payload, args.symbol)
            elif args.action == "page":
                result = inspect_page(
                    payload,
                    args.kind,
                    args.source_purpose,
                    args.request_cursor,
                )
            elif args.action == "scan":
                result = inspect_scan(payload, args.title)
            else:
                result = inspect_scan_update(payload, args.scan_id)
        _print_json(result)
        return 0
    except (ContractError, SnapshotError, OSError) as exc:
        _print_json(_error_result(action, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
