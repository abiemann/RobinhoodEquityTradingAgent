#!/usr/bin/env python3
"""Deterministically stage raw broker snapshots for ``daily_loss.py``.

The trading routine receives broker responses through an MCP transport.  This
helper removes only the known transport envelope, validates the broker payload,
and atomically writes canonical raw JSON.  It never asks an agent to transcribe,
summarize, or re-key a response.

Typical use::

    python broker_snapshot.py preflight --scratch C:\\path\\to\\session-scratch

    python broker_snapshot.py stage --kind portfolio \
        --generation A \
        --source C:\\tool-results\\portfolio.json \
        --output C:\\scratch\\portfolio.json

For a complete paginated response, repeat ``--source``, ``--output``, and
``--request-cursor`` in request order.  The first request cursor is the literal
``FIRST``; every later value is the cursor returned by the preceding page::

    python broker_snapshot.py stage --kind positions \
        --generation A \
        --source positions-result-1.json --source positions-result-2.json \
        --output positions-1.json --output positions-2.json \
        --request-cursor FIRST --request-cursor cursor-from-page-1

``--allow-more`` is available only for deliberately staging an incomplete
prefix while fetching more pages.  Without it, positions/orders staging proves
that the final supplied page has no continuation cursor.  Every staged payload
gets a canonical provenance sidecar.  ``daily_loss.py --snapshot-generation``
requires one complete, aggregate-sealed set per kind and rejects changed files,
broken cursor chains, scratch-session mixing, and generation A/B mixing.

Accepted source shapes are deliberately narrow:

* a raw broker payload whose root contains ``data``;
* an MCP result whose ``structuredContent`` contains that raw payload; or
* an MCP result containing exactly one JSON ``content`` text block.

Every operational invocation writes exactly one JSON object to stdout.  The
script is stdlib-only and preserves JSON number precision with ``Decimal``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
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


class SnapshotError(ValueError):
    """Raised when a source cannot prove a valid raw broker snapshot."""


class CliError(SnapshotError):
    """Raised for command-line usage errors that must remain JSON output."""


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
    for field in ("cash", "buying_power", "equity_value"):
        if field in data and data[field] is not None:
            _decimal(data[field], f"portfolio.data.{field}", nonnegative=True)
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


def _validate_payloads(
    kind: str,
    payloads: Sequence[Mapping[str, Any]],
    request_cursors: Sequence[str] | None,
    allow_more: bool,
) -> tuple[list[dict[str, Any]], list[str] | None]:
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
    cursors = _validate_pagination(kind, metadata, request_cursors, allow_more)
    return metadata, cursors


def _scratch_marker_document(scratch_id: str) -> dict[str, Any]:
    return {
        'schema_version': Decimal(SCHEMA_VERSION),
        'marker': 'rhmra-broker-snapshot-scratch',
        'purpose': 'daily-loss-raw-broker-staging',
        'scratch_id': scratch_id,
    }


def _validated_output_scratch(
    outputs: Sequence[str],
) -> tuple[Path, Mapping[str, Any]]:
    parents = {Path(output).parent.resolve(strict=True) for output in outputs}
    if len(parents) != 1:
        raise SnapshotError('all staged outputs must share one scratch directory')
    scratch = next(iter(parents))
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
        if os.path.exists(output) or os.path.exists(_stage_metadata_path(output)):
            raise SnapshotError(f"{output}: staged output already exists")
        parent = os.path.dirname(output)
        if not parent or not os.path.isdir(parent):
            raise SnapshotError(f"{output}: output directory does not exist")
    scratch, marker = _validated_output_scratch(absolute_outputs)
    return absolute_sources, absolute_outputs, scratch, marker


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


def _stage(args: argparse.Namespace) -> dict[str, Any]:
    sources, outputs, scratch, marker = _absolute_distinct_paths(
        args.source, args.output
    )
    for source in sources:
        source_path = Path(source).resolve(strict=True)
        if source_path.parent == scratch:
            _validated_stage_metadata(
                source,
                scratch=scratch,
                marker=marker,
                expected_generation=args.generation,
                expected_kind=args.kind,
            )
    payloads: list[Mapping[str, Any]] = []
    envelopes: list[str] = []
    source_hashes: list[str] = []
    for source in sources:
        document, source_raw = _read_source(source)
        payload, envelope = _unwrap_source(document, source)
        payloads.append(payload)
        envelopes.append(envelope)
        source_hashes.append(_sha256(source_raw))

    metadata, request_cursors = _validate_payloads(
        args.kind, payloads, args.request_cursor, args.allow_more
    )
    metadata_paths = [_stage_metadata_path(output) for output in outputs]
    for metadata_path in metadata_paths:
        if os.path.exists(metadata_path):
            raise SnapshotError(f"{metadata_path}: staging provenance already exists")
    payload_raws = [_canonical_bytes(payload) for payload in payloads]
    set_id = str(uuid.uuid4())
    complete = not args.allow_more
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
    all_prepared = _prepare_atomic_files(
        [*outputs, *metadata_paths], [*payloads, *metadata_documents]
    )
    prepared = all_prepared[:len(outputs)]
    _commit_atomic_files(all_prepared)

    files: list[dict[str, Any]] = []
    for index, (source, output, envelope, source_hash, page, prepared_item) in enumerate(
        zip(sources, outputs, envelopes, source_hashes, metadata, prepared), 1
    ):
        entry: dict[str, Any] = {
            "index": index,
            "source": source,
            "output": output,
            "transport": envelope,
            "source_sha256": source_hash,
            "payload_sha256": _sha256(prepared_item[2]),
            "provenance": metadata_paths[index - 1],
            "row_count": page["row_count"],
            "next_cursor": page["next_cursor"],
        }
        if request_cursors is not None:
            entry["request_cursor"] = request_cursors[index - 1]
        files.append(entry)
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "stage",
        "ok": True,
        "kind": args.kind,
        "generation": args.generation,
        "set_id": set_id,
        "complete": not args.allow_more,
        "file_count": len(files),
        "files": files,
    }


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    if not os.path.isabs(args.scratch):
        raise SnapshotError("--scratch must be an absolute path")
    scratch = Path(args.scratch).resolve(strict=True)
    if not scratch.is_dir():
        raise SnapshotError(f"{scratch}: scratch path is not a directory")
    project = Path(__file__).resolve().parent
    if scratch == project or project in scratch.parents:
        raise SnapshotError("scratch directory must be outside the project folder")

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
    prepared = _prepare_atomic_files([marker_path], [marker])
    _commit_atomic_files(prepared)

    return {
        "schema_version": SCHEMA_VERSION,
        "action": "preflight",
        "ok": True,
        "scratch": str(scratch),
        "scratch_id": scratch_id,
        "sentinel_sha256": _sha256(raw),
        "write_read_parse": True,
        "cleanup_verified": True,
    }


def _parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    preflight = subparsers.add_parser("preflight", help="verify session scratch I/O")
    preflight.add_argument("--scratch", required=True)

    stage = subparsers.add_parser("stage", help="unwrap, validate, and stage snapshots")
    stage.add_argument("--kind", choices=SNAPSHOT_KINDS, required=True)
    stage.add_argument("--generation", choices=("A", "B"), required=True)
    stage.add_argument("--source", action="append", required=True, metavar="FILE")
    stage.add_argument("--output", action="append", required=True, metavar="FILE")
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


def _error_result(action: str, exc: Exception) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": False,
        "error": {
            "code": "invalid_snapshot" if not isinstance(exc, CliError) else "usage_error",
            "message": str(exc),
        },
    }


def _print_json(result: Mapping[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    action = arguments[0] if arguments and arguments[0] in {"preflight", "stage"} else "unknown"
    try:
        args = _parser().parse_args(arguments)
        result = _preflight(args) if args.action == "preflight" else _stage(args)
        _print_json(result)
        return 0
    except (SnapshotError, OSError) as exc:
        _print_json(_error_result(action, exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
