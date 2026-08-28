#!/usr/bin/env python3
"""Fail-closed, exact daily P&L circuit-breaker calculation.

This module consumes *raw* JSON responses from the Robinhood MCP connector.
It reconstructs the account's change in value since the official close of
the preceding NYSE session:

    current shares * current price
    + today's sell proceeds
    - today's buy cost
    - opening shares * adjusted previous close
    - per-execution fees

The calculation deliberately does not use lifetime cost basis.  That makes a
loss incurred today visible even when an overnight holding remains profitable
relative to its original purchase price.

The command is a two-pass interface because the quote symbols are only known
after positions and executions have been reconciled:

1. Discovery:

       python daily_loss.py --positions positions-1.json \
           --orders orders-1.json --trading-date 2026-07-31 \
           --stop-date-pt 2026-07-31 \
           --as-of-utc 2026-07-31T16:00:00Z \
           --symbols-out quote-symbols.json

2. Calculation:

       python daily_loss.py --portfolio portfolio.json \
           --positions positions-1.json --orders orders-1.json \
           --quotes quotes-1.json --trading-date 2026-07-31 \
           --stop-date-pt 2026-07-31 \
           --as-of-utc 2026-07-31T16:00:00Z --halt-pct 5 \
           --json-out daily-loss.json

An unattended lifecycle-bound calculation additionally supplies the active
``--invocation-id``/``--run-token`` and the startup-pinned
``--expected-constants-sha256``.  In that mode ``--halt-pct`` is forbidden:
the helper validates the repository constants itself, derives both the daily
loss and stop-count guards, publishes one deterministic no-clobber wrapper at
the bound scratch basename ``daily-loss-a.json`` or ``daily-loss-b.json``,
records the owner-fenced SECOND checkpoint, and only then emits the unchanged
wrapper on stdout.  A lost-receipt retry succeeds only when the durable bytes
and bound evidence are exactly identical.  The run token is never written to
the wrapper or stdout.

All monetary and quantity arithmetic uses :class:`decimal.Decimal`.  JSON
numbers are loaded directly as Decimal values, non-finite JSON constants are
rejected, pagination is checked, duplicate broker IDs must be identical, and
malformed or unreconciled input fails without publishing a usable result.
Both modes emit exactly one compact machine-readable JSON object on stdout
after their atomic output write; the discovery receipt contains the exact
ordered quote-symbol array as well as its count and time binding.
When ``--failure-json`` is supplied, an expected CLI failure likewise emits
one phase-typed JSON object on stdout so orchestration can distinguish broker
semantic inconsistency from binding, input, and output failures.  Without that
flag, failures retain the human-readable stderr interface.

The calculation result also derives the Pacific-day stop-fill count and
stopped-out symbols from the same reconciled FINAL order pages, so the caller
never needs a second order read.  The importable entry points are
:func:`discover_required_symbols` and :func:`calculate_daily_loss`.  They
accept already-decoded raw connector documents.  :func:`load_json` preserves
JSON-number precision for callers loading those documents from disk.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from broker_snapshot import (
    STAGE_METADATA_SUFFIX,
    SnapshotError,
    validate_generation_inputs,
)
from market_calendar import core_session_schedule
from market_clock import (
    EASTERN_STD_OFFSET,
    PACIFIC_STD_OFFSET,
    session_state,
    zone_time,
)
import run_lifecycle as run_lifecycle_module
from validate_constants import ConstantsValidationError, validate_constants_file


SCHEMA_VERSION = 1
OFFICIAL_CLOSE_SOURCE = "sip-list-exchange-close"
MAX_ACTIVE_QUOTE_AGE_SECONDS = Decimal("900")
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
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UTC_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?P<fraction>\.\d+)?(?:Z|\+00:00)$"
)


class DailyLossError(ValueError):
    """Raised when broker input cannot support a safe daily-loss verdict."""


class DailyLossInternalError(DailyLossError):
    """Raised when a helper-owned invariant fails rather than broker semantics."""


def _reject_nonfinite_json(token: str) -> None:
    raise DailyLossError(f"non-finite JSON constant {token!r} is not allowed")


def load_json(path: os.PathLike[str] | str) -> Any:
    """Load strict JSON while retaining every input decimal exactly."""

    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_float=Decimal,
                parse_int=Decimal,
                parse_constant=_reject_nonfinite_json,
            )
    except (OSError, json.JSONDecodeError) as exc:
        raise DailyLossError(f"{path}: cannot load strict JSON: {exc}") from exc


def _decimal(value: Any, field: str, *, positive: bool = False,
             nonnegative: bool = False) -> Decimal:
    """Parse a finite Decimal without ever routing through binary float."""

    if isinstance(value, bool) or value is None or isinstance(value, float):
        raise DailyLossError(f"{field}: expected an exact finite decimal")
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, str):
            if not value or value != value.strip():
                raise InvalidOperation
            number = Decimal(value)
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise DailyLossError(f"{field}: expected an exact finite decimal") from exc
    if not number.is_finite():
        raise DailyLossError(f"{field}: must be finite")
    if positive and number <= 0:
        raise DailyLossError(f"{field}: must be greater than zero")
    if nonnegative and number < 0:
        raise DailyLossError(f"{field}: must not be negative")
    return number


def decimal_string(value: Decimal) -> str:
    """Return a canonical, exponent-free exact decimal string."""

    if not isinstance(value, Decimal) or not value.is_finite():
        raise DailyLossInternalError(
            "internal error: output decimal is not finite"
        )
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _exact_product(*values: Decimal) -> Decimal:
    """Multiply finite Decimals with enough local precision for no rounding."""

    if not values:
        return Decimal(1)
    precision = sum(max(1, len(value.as_tuple().digits)) for value in values) + 8
    with localcontext() as context:
        context.prec = max(32, precision)
        result = Decimal(1)
        for value in values:
            result *= value
        return result


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    """Sum finite Decimals with enough aligned precision for no rounding."""

    materialized = list(values)
    if not materialized:
        return Decimal(0)
    minimum_exponent = min(value.as_tuple().exponent for value in materialized)
    maximum_integer_digits = max(
        1,
        max(max(value.adjusted() + 1, 0) for value in materialized),
    )
    carry_digits = len(str(len(materialized))) + 2
    precision = maximum_integer_digits - minimum_exponent + carry_digits
    with localcontext() as context:
        context.prec = max(32, precision)
        return sum(materialized, Decimal(0))


def _parse_date(value: date | str, field: str = "trading date") -> date:
    if isinstance(value, datetime):
        raise DailyLossError(f"{field}: expected YYYY-MM-DD")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise DailyLossError(f"{field}: expected YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise DailyLossError(f"{field}: invalid calendar date") from exc


@dataclass(frozen=True)
class UtcInstant:
    """UTC instant retaining arbitrary input fractional-second precision."""

    whole_second: datetime
    fraction: Decimal
    canonical: str

    @property
    def comparison_key(self) -> tuple[datetime, Decimal]:
        return self.whole_second, self.fraction


def parse_utc_instant(value: str, field: str = "UTC timestamp") -> UtcInstant:
    """Parse a strict ISO-8601 UTC timestamp without losing nanoseconds."""

    if not isinstance(value, str):
        raise DailyLossError(f"{field}: expected an ISO-8601 UTC string")
    match = _UTC_RE.fullmatch(value)
    if not match:
        raise DailyLossError(
            f"{field}: expected YYYY-MM-DDTHH:MM:SS[.fraction]Z"
        )
    fraction_text = match.group("fraction")
    fraction = Decimal(f"0{fraction_text}") if fraction_text else Decimal(0)
    try:
        whole_second = datetime(
            *_parse_date(match.group("date"), field).timetuple()[:3],
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise DailyLossError(f"{field}: invalid UTC timestamp") from exc
    canonical_fraction = ""
    if fraction:
        canonical_fraction = decimal_string(fraction)[1:]
    canonical = whole_second.strftime("%Y-%m-%dT%H:%M:%S") + canonical_fraction + "Z"
    return UtcInstant(whole_second, fraction, canonical)


def _instant_after(left: UtcInstant, right: UtcInstant) -> bool:
    return left.comparison_key > right.comparison_key


def _instant_equal(left: UtcInstant, right: UtcInstant) -> bool:
    return left.comparison_key == right.comparison_key


def _eastern_date(instant: UtcInstant) -> date:
    return _eastern_datetime(instant).date()


def _eastern_datetime(instant: UtcInstant) -> datetime:
    eastern, _name, _offset = zone_time(
        instant.whole_second, EASTERN_STD_OFFSET, "EST", "EDT"
    )
    return eastern


def _pacific_date(instant: UtcInstant) -> date:
    pacific, _name, _offset = zone_time(
        instant.whole_second, PACIFIC_STD_OFFSET, "PST", "PDT"
    )
    return pacific.date()


def previous_nyse_session(trading_day: date | str) -> date:
    """Return the preceding reviewed NYSE core-session date, or fail closed."""

    day = _parse_date(trading_day)
    status, _close_minute = core_session_schedule(day)
    if status == "unknown":
        raise DailyLossError(f"{day}: NYSE calendar coverage is unknown")
    if status not in {"normal", "early-close", "holiday"}:
        raise DailyLossError(f"{day}: unrecognized NYSE calendar status {status!r}")

    candidate = day - timedelta(days=1)
    while True:
        if candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
            continue
        status, close_minute = core_session_schedule(candidate)
        if status == "unknown":
            raise DailyLossError(
                f"{candidate}: previous NYSE session is outside reviewed coverage"
            )
        if status == "holiday" or close_minute is None:
            candidate -= timedelta(days=1)
            continue
        if status not in {"normal", "early-close"}:
            raise DailyLossError(
                f"{candidate}: unrecognized NYSE calendar status {status!r}"
            )
        return candidate


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DailyLossError(f"{field}: expected an object")
    return value


def _page_rows(
    pages: Sequence[Mapping[str, Any]], array_name: str
) -> list[Mapping[str, Any]]:
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes, Mapping)):
        raise DailyLossError(f"{array_name} pages: expected a nonempty sequence")
    if not pages:
        raise DailyLossError(f"{array_name} pages: at least one page is required")

    rows: list[Mapping[str, Any]] = []
    final_index = len(pages) - 1
    seen_next_cursors: set[str] = set()
    for index, page_value in enumerate(pages):
        page = _as_mapping(page_value, f"{array_name} page {index + 1}")
        data = _as_mapping(
            page.get("data"), f"{array_name} page {index + 1}.data"
        )
        if array_name not in data:
            raise DailyLossError(
                f"{array_name} page {index + 1}.data.{array_name}: missing"
            )
        page_rows = data[array_name]
        if not isinstance(page_rows, list):
            raise DailyLossError(
                f"{array_name} page {index + 1}.data.{array_name}: "
                "expected an array"
            )

        next_value = data.get("next")
        next_is_nonempty = (
            isinstance(next_value, str) and bool(next_value.strip())
        )
        if next_is_nonempty:
            parsed_next = urlsplit(next_value.strip())
            cursor_values = parse_qs(parsed_next.query).get("cursor", [])
            if (
                parsed_next.scheme not in {"http", "https"}
                or not parsed_next.netloc
                or len(cursor_values) != 1
                or not cursor_values[0]
            ):
                raise DailyLossError(
                    f"{array_name} page {index + 1}: next is not a "
                    "cursor-bearing URL"
                )
            cursor = cursor_values[0]
            if cursor in seen_next_cursors:
                raise DailyLossError(
                    f"{array_name} page {index + 1}: repeated next cursor"
                )
            seen_next_cursors.add(cursor)
        if index < final_index:
            if not next_is_nonempty:
                raise DailyLossError(
                    f"{array_name} page {index + 1}: nonfinal page has no next"
                )
        elif next_is_nonempty:
            raise DailyLossError(
                f"{array_name} page {index + 1}: final page unexpectedly has next"
            )
        elif next_value is not None and not isinstance(next_value, str):
            raise DailyLossError(
                f"{array_name} page {index + 1}: next must be a URL string or null"
            )

        for row_index, row in enumerate(page_rows):
            if row is None:
                raise DailyLossError(
                    f"{array_name} page {index + 1} row "
                    f"{row_index + 1}: null row is indeterminate"
                )
            rows.append(
                _as_mapping(
                    row,
                    f"{array_name} page {index + 1} row {row_index + 1}",
                )
            )
    return rows


def _symbol(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DailyLossError(f"{field}: expected a nonempty symbol")
    normalized = value.upper()
    if any(character.isspace() for character in normalized):
        raise DailyLossError(f"{field}: symbol contains whitespace")
    return normalized


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    intraday_quantity: Decimal


@dataclass
class Flow:
    buy_quantity: Decimal = Decimal(0)
    sell_quantity: Decimal = Decimal(0)
    buy_notional: Decimal = Decimal(0)
    sell_notional: Decimal = Decimal(0)
    execution_fees: Decimal = Decimal(0)
    execution_count: int = 0


@dataclass(frozen=True)
class ReconciledSymbol:
    symbol: str
    current_quantity: Decimal
    reported_intraday_quantity: Decimal | None
    buy_quantity: Decimal
    sell_quantity: Decimal
    net_execution_quantity: Decimal
    opening_quantity: Decimal
    buy_notional: Decimal
    sell_notional: Decimal
    execution_fees: Decimal
    execution_count: int

    @property
    def needs_current_price(self) -> bool:
        return self.current_quantity > 0

    @property
    def needs_previous_close(self) -> bool:
        return self.opening_quantity > 0

    @property
    def needs_quote(self) -> bool:
        return self.needs_current_price or self.needs_previous_close


@dataclass(frozen=True)
class Reconciliation:
    trading_day: date
    stop_day_pt: date
    previous_session: date
    as_of: UtcInstant
    symbols: tuple[ReconciledSymbol, ...]
    unique_order_count: int
    today_execution_count: int
    stop_fills_today: int
    stopped_out_symbols: tuple[str, ...]

    @property
    def required_symbols(self) -> list[str]:
        return [item.symbol for item in self.symbols if item.needs_quote]


def _positions(pages: Sequence[Mapping[str, Any]]) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    for index, row in enumerate(_page_rows(pages, "positions"), 1):
        symbol = _symbol(row.get("symbol"), f"position {index}.symbol")
        quantity = _decimal(
            row.get("quantity"), f"position {symbol}.quantity", positive=True
        )
        if "intraday_quantity" not in row:
            raise DailyLossError(
                f"position {symbol}.intraday_quantity: missing"
            )
        intraday = _decimal(
            row["intraday_quantity"], f"position {symbol}.intraday_quantity"
        )
        position_type = row.get("type")
        if position_type is not None and position_type != "long":
            raise DailyLossError(
                f"position {symbol}.type: only long positions are supported"
            )
        if symbol in positions:
            raise DailyLossError(f"position {symbol}: duplicate position row")
        positions[symbol] = Position(symbol, quantity, intraday)
    return positions


def _returned_sell_is_stop(
    order: Mapping[str, Any], order_id: str
) -> bool:
    """Classify one fill-bearing sell using the pinned returned-order shape."""

    type_value = order.get("type")
    if (
        not isinstance(type_value, str)
        or not type_value
        or type_value != type_value.strip()
    ):
        raise DailyLossError(
            f"order {order_id}: stop classification is indeterminate "
            "because type is missing or malformed"
        )
    order_type = type_value.lower()

    trigger_value = order.get("trigger")
    if trigger_value is None:
        trigger: str | None = None
    elif (
        isinstance(trigger_value, str)
        and trigger_value
        and trigger_value == trigger_value.strip()
    ):
        trigger = trigger_value.lower()
    else:
        raise DailyLossError(
            f"order {order_id}: stop classification is indeterminate "
            "because trigger is malformed"
        )

    raw_stop_price = order.get("stop_price")
    has_positive_stop_price = False
    if raw_stop_price is not None:
        stop_price = _decimal(
            raw_stop_price,
            f"order {order_id}.stop_price",
            nonnegative=True,
        )
        has_positive_stop_price = stop_price > 0

    if order_type in {"stop_market", "stop_limit"}:
        if trigger not in {None, "stop"}:
            raise DailyLossError(
                f"order {order_id}: stop classification is indeterminate "
                "because type and trigger are contradictory"
            )
        return True

    if order_type not in {"market", "limit"}:
        raise DailyLossError(
            f"order {order_id}: stop classification is indeterminate "
            f"because type {type_value!r} is unrecognized"
        )
    if trigger == "stop":
        if order_type != "market":
            raise DailyLossError(
                f"order {order_id}: stop classification is indeterminate "
                "because type and trigger are contradictory"
            )
        return True
    if trigger not in {None, "immediate"}:
        raise DailyLossError(
            f"order {order_id}: stop classification is indeterminate "
            f"because trigger {trigger_value!r} is unrecognized"
        )
    if has_positive_stop_price:
        raise DailyLossError(
            f"order {order_id}: stop classification is indeterminate "
            "because a positive stop_price has no recognized stop marker"
        )
    return False


def _execution_rows(
    pages: Sequence[Mapping[str, Any]],
    trading_day: date,
    stop_day_pt: date,
    as_of: UtcInstant,
) -> tuple[dict[str, Flow], int, int, int, tuple[str, ...]]:
    order_rows = _page_rows(pages, "orders")
    orders_by_id: dict[str, Mapping[str, Any]] = {}
    unique_orders: list[Mapping[str, Any]] = []
    for index, order in enumerate(order_rows, 1):
        order_id = order.get("id")
        if not isinstance(order_id, str) or not order_id.strip():
            raise DailyLossError(f"order {index}.id: expected a nonempty string")
        prior = orders_by_id.get(order_id)
        if prior is not None:
            if prior != order:
                raise DailyLossError(
                    f"order {order_id}: conflicting duplicate order ID"
                )
            continue
        orders_by_id[order_id] = order
        unique_orders.append(order)

    flows: dict[str, Flow] = {}
    executions_by_id: dict[
        str, tuple[str, str, str, Mapping[str, Any]]
    ] = {}
    today_execution_count = 0
    stop_order_ids_today: set[str] = set()
    stopped_out_symbols: set[str] = set()

    for order in unique_orders:
        order_id = order["id"]
        state_value = order.get("state")
        if not isinstance(state_value, str) or state_value not in ORDER_STATES:
            raise DailyLossError(
                f"order {order_id}.state: expected a recognized order state"
            )
        if "cumulative_quantity" not in order:
            raise DailyLossError(
                f"order {order_id}.cumulative_quantity: missing"
            )
        cumulative_quantity = _decimal(
            order["cumulative_quantity"],
            f"order {order_id}.cumulative_quantity",
            nonnegative=True,
        )
        executions = order.get("executions")
        if not isinstance(executions, list):
            raise DailyLossError(
                f"order {order_id}.executions: expected an array"
            )
        symbol: str | None = None
        side: str | None = None
        order_execution_quantity = Decimal(0)
        has_execution_on_stop_day = False

        for execution_index, raw_execution in enumerate(executions, 1):
            if raw_execution is None:
                raise DailyLossError(
                    f"order {order_id}.executions[{execution_index}]: "
                    "null execution is indeterminate"
                )
            execution = _as_mapping(
                raw_execution,
                f"order {order_id}.executions[{execution_index}]",
            )
            if symbol is None:
                symbol = _symbol(
                    order.get("symbol"), f"order {order_id}.symbol"
                )
                side_value = order.get("side")
                if not isinstance(side_value, str):
                    raise DailyLossError(
                        f"order {order_id}.side: expected buy or sell"
                    )
                side = side_value.lower()
                if side not in {"buy", "sell"}:
                    raise DailyLossError(
                        f"order {order_id}.side: expected buy or sell"
                    )
            execution_id = execution.get("id")
            if not isinstance(execution_id, str) or not execution_id.strip():
                raise DailyLossError(
                    f"order {order_id} execution {execution_index}.id: "
                    "expected a nonempty string"
                )
            fingerprint = (order_id, symbol, side, execution)
            prior_execution = executions_by_id.get(execution_id)
            if prior_execution is not None:
                if prior_execution != fingerprint:
                    raise DailyLossError(
                        f"execution {execution_id}: conflicting duplicate "
                        "execution ID"
                    )
                continue
            executions_by_id[execution_id] = fingerprint

            quantity = _decimal(
                execution.get("quantity"),
                f"execution {execution_id}.quantity",
                positive=True,
            )
            order_execution_quantity = _exact_sum(
                (order_execution_quantity, quantity)
            )
            timestamp = parse_utc_instant(
                execution.get("timestamp"),
                f"execution {execution_id}.timestamp",
            )
            if _instant_after(timestamp, as_of):
                raise DailyLossError(
                    f"execution {execution_id}: timestamp is later than as-of"
                )
            if _pacific_date(timestamp) == stop_day_pt:
                has_execution_on_stop_day = True
            execution_day = _eastern_date(timestamp)
            if execution_day < trading_day:
                continue
            if execution_day > trading_day:
                raise DailyLossError(
                    f"execution {execution_id}: execution ET date is after "
                    "the trading date"
                )

            price = _decimal(
                execution.get("price"),
                f"execution {execution_id}.price",
                positive=True,
            )
            if "fees" not in execution:
                raise DailyLossError(f"execution {execution_id}.fees: missing")
            fees = _decimal(
                execution["fees"],
                f"execution {execution_id}.fees",
                nonnegative=True,
            )
            notional = _exact_product(quantity, price)

            assert symbol is not None and side is not None
            flow = flows.setdefault(symbol, Flow())
            if side == "buy":
                flow.buy_quantity = _exact_sum((flow.buy_quantity, quantity))
                flow.buy_notional = _exact_sum((flow.buy_notional, notional))
            else:
                flow.sell_quantity = _exact_sum((flow.sell_quantity, quantity))
                flow.sell_notional = _exact_sum((flow.sell_notional, notional))
            flow.execution_fees = _exact_sum((flow.execution_fees, fees))
            flow.execution_count += 1
            today_execution_count += 1

        if order_execution_quantity != cumulative_quantity:
            raise DailyLossError(
                f"order {order_id}: execution quantities "
                f"{decimal_string(order_execution_quantity)} do not equal "
                f"cumulative_quantity {decimal_string(cumulative_quantity)}"
            )
        if (
            state_value in {
                "filled", "partially_filled", "partially_filled_rest_cancelled"
            }
            and cumulative_quantity <= 0
        ):
            raise DailyLossError(
                f"order {order_id}: {state_value} order has no executed "
                "quantity"
            )

        if has_execution_on_stop_day:
            assert symbol is not None and side is not None
            if side == "sell" and _returned_sell_is_stop(order, order_id):
                stop_order_ids_today.add(order_id)
                stopped_out_symbols.add(symbol)

    return (
        flows,
        len(unique_orders),
        today_execution_count,
        len(stop_order_ids_today),
        tuple(sorted(stopped_out_symbols)),
    )


def _reconcile(
    position_pages: Sequence[Mapping[str, Any]],
    order_pages: Sequence[Mapping[str, Any]],
    trading_date: date | str,
    as_of_utc: str,
    stop_date_pt: date | str,
) -> Reconciliation:
    trading_day = _parse_date(trading_date)
    stop_day_pt = _parse_date(stop_date_pt, "stop date PT")
    previous_session = previous_nyse_session(trading_day)
    as_of = parse_utc_instant(as_of_utc, "as-of UTC")
    if _eastern_date(as_of) != trading_day:
        raise DailyLossError(
            "as-of UTC does not fall on --trading-date in US Eastern time"
        )
    if _pacific_date(as_of) != stop_day_pt:
        raise DailyLossError(
            "as-of UTC does not fall on --stop-date-pt in US Pacific time"
        )

    positions = _positions(position_pages)
    (
        flows,
        unique_order_count,
        today_execution_count,
        stop_fills_today,
        stopped_out_symbols,
    ) = _execution_rows(order_pages, trading_day, stop_day_pt, as_of)

    reconciled: list[ReconciledSymbol] = []
    for symbol in sorted(set(positions) | set(flows)):
        position = positions.get(symbol)
        flow = flows.get(symbol, Flow())
        current_quantity = position.quantity if position else Decimal(0)
        net_execution_quantity = _exact_sum(
            (flow.buy_quantity, -flow.sell_quantity)
        )
        reported_intraday = (
            position.intraday_quantity if position is not None else None
        )
        if (
            reported_intraday is not None
            and net_execution_quantity != reported_intraday
        ):
            raise DailyLossError(
                f"{symbol}: today's net executions "
                f"{decimal_string(net_execution_quantity)} do not equal "
                f"intraday_quantity {decimal_string(reported_intraday)}"
            )
        opening_quantity = _exact_sum(
            (current_quantity, -flow.buy_quantity, flow.sell_quantity)
        )
        if opening_quantity < 0:
            raise DailyLossError(
                f"{symbol}: reconstructed opening quantity is negative"
            )
        reconciled.append(
            ReconciledSymbol(
                symbol=symbol,
                current_quantity=current_quantity,
                reported_intraday_quantity=reported_intraday,
                buy_quantity=flow.buy_quantity,
                sell_quantity=flow.sell_quantity,
                net_execution_quantity=net_execution_quantity,
                opening_quantity=opening_quantity,
                buy_notional=flow.buy_notional,
                sell_notional=flow.sell_notional,
                execution_fees=flow.execution_fees,
                execution_count=flow.execution_count,
            )
        )

    return Reconciliation(
        trading_day=trading_day,
        stop_day_pt=stop_day_pt,
        previous_session=previous_session,
        as_of=as_of,
        symbols=tuple(reconciled),
        unique_order_count=unique_order_count,
        today_execution_count=today_execution_count,
        stop_fills_today=stop_fills_today,
        stopped_out_symbols=stopped_out_symbols,
    )


def discover_required_symbols(
    position_pages: Sequence[Mapping[str, Any]],
    order_pages: Sequence[Mapping[str, Any]],
    trading_date: date | str,
    as_of_utc: str,
    stop_date_pt: date | str,
) -> list[str]:
    """Return sorted symbols needing quotes after exact share reconciliation."""

    return _reconcile(
        position_pages, order_pages, trading_date, as_of_utc, stop_date_pt
    ).required_symbols


# A descriptive alias for importers that read the output as quote symbols.
required_quote_symbols = discover_required_symbols


def _portfolio_total(portfolio_doc: Mapping[str, Any]) -> Decimal:
    portfolio = _as_mapping(portfolio_doc, "portfolio")
    data = _as_mapping(portfolio.get("data"), "portfolio.data")
    if "total_value" not in data:
        raise DailyLossError("portfolio.data.total_value: missing")
    return _decimal(
        data["total_value"], "portfolio.data.total_value", positive=True
    )


def _quote_results(
    quote_batches: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    if (
        not isinstance(quote_batches, Sequence)
        or isinstance(quote_batches, (str, bytes, Mapping))
    ):
        raise DailyLossError("quote batches: expected a sequence")
    if not quote_batches:
        return {}

    by_symbol: dict[str, Mapping[str, Any]] = {}
    for batch_index, batch_value in enumerate(quote_batches, 1):
        batch = _as_mapping(batch_value, f"quote batch {batch_index}")
        data = _as_mapping(batch.get("data"), f"quote batch {batch_index}.data")
        if "results" not in data:
            raise DailyLossError(
                f"quote batch {batch_index}.data.results: missing"
            )
        results = data["results"]
        if results is None:
            results = []
        if not isinstance(results, list):
            raise DailyLossError(
                f"quote batch {batch_index}.data.results: "
                "expected an array or null"
            )
        if len(results) > 20:
            raise DailyLossError(
                f"quote batch {batch_index}: more than 20 results can omit "
                "official closes"
            )
        for result_index, result_value in enumerate(results, 1):
            if result_value is None:
                continue
            result = _as_mapping(
                result_value,
                f"quote batch {batch_index} result {result_index}",
            )
            quote = _as_mapping(
                result.get("quote"),
                f"quote batch {batch_index} result {result_index}.quote",
            )
            symbol = _symbol(
                quote.get("symbol"),
                f"quote batch {batch_index} result {result_index}.quote.symbol",
            )
            close_value = result.get("close")
            if close_value is not None:
                close = _as_mapping(
                    close_value,
                    f"quote batch {batch_index} result {result_index}.close",
                )
                if "symbol" in close and close["symbol"] is not None:
                    close_symbol = _symbol(
                        close["symbol"],
                        f"quote batch {batch_index} result "
                        f"{result_index}.close.symbol",
                    )
                    if close_symbol != symbol:
                        raise DailyLossError(
                            f"quote result {symbol}: close symbol is "
                            f"{close_symbol}"
                        )
            prior = by_symbol.get(symbol)
            if prior is not None:
                if prior != result:
                    raise DailyLossError(
                        f"quote {symbol}: conflicting duplicate result"
                    )
                continue
            by_symbol[symbol] = result
    return by_symbol


def _current_price(
    symbol: str, quote: Mapping[str, Any]
) -> tuple[str, Decimal, UtcInstant]:
    candidates: list[tuple[str, Decimal, UtcInstant]] = []
    pairs = (
        ("last_trade", "last_trade_price", "venue_last_trade_time"),
        (
            "last_non_reg_trade",
            "last_non_reg_trade_price",
            "venue_last_non_reg_trade_time",
        ),
    )
    for source, price_key, time_key in pairs:
        price_value = quote.get(price_key)
        time_value = quote.get(time_key)
        if price_value is None and time_value is None:
            continue
        if price_value is None or time_value is None:
            raise DailyLossError(
                f"quote {symbol}: {price_key} and {time_key} must appear together"
            )
        price = _decimal(
            price_value, f"quote {symbol}.{price_key}", positive=True
        )
        instant = parse_utc_instant(
            time_value, f"quote {symbol}.{time_key}"
        )
        candidates.append((source, price, instant))

    if not candidates:
        raise DailyLossError(f"quote {symbol}: no timestamped current price")
    if len(candidates) == 1:
        return candidates[0]

    first, second = candidates
    if _instant_equal(first[2], second[2]):
        if first[1] != second[1]:
            raise DailyLossError(
                f"quote {symbol}: tied venue timestamps have different prices"
            )
        return first
    return first if _instant_after(first[2], second[2]) else second


def _validate_current_price_time(
    symbol: str,
    timestamp: UtcInstant,
    trading_day: date,
    previous_session: date,
    as_of: UtcInstant,
) -> None:
    """Reject future or stale marks that could understate a live loss."""

    if _instant_after(timestamp, as_of):
        raise DailyLossError(
            f"quote {symbol}: current-price timestamp is later than as-of"
        )
    quote_day = _eastern_date(timestamp)
    if quote_day not in {previous_session, trading_day}:
        raise DailyLossError(
            f"quote {symbol}: current-price timestamp is stale"
        )

    as_of_et = _eastern_datetime(as_of)
    session, _since_open, _calendar, entry_open, _close = session_state(
        as_of_et
    )
    if session in {"regular", "after-hours"} and quote_day != trading_day:
        raise DailyLossError(
            f"quote {symbol}: current-price timestamp predates today's "
            "open session"
        )
    if entry_open and quote_day == trading_day:
        whole_seconds = int(
            (as_of.whole_second - timestamp.whole_second).total_seconds()
        )
        age_seconds = (
            Decimal(whole_seconds) + as_of.fraction - timestamp.fraction
        )
        if age_seconds > MAX_ACTIVE_QUOTE_AGE_SECONDS:
            raise DailyLossError(
                f"quote {symbol}: current-price timestamp is more than "
                "15 minutes old during an open entry session"
            )


def _validated_prices(
    item: ReconciledSymbol,
    result: Mapping[str, Any],
    previous_session: date,
    trading_day: date,
    as_of: UtcInstant,
) -> tuple[Decimal | None, str | None, UtcInstant | None, Decimal | None]:
    quote = _as_mapping(result.get("quote"), f"quote {item.symbol}.quote")
    current_price: Decimal | None = None
    current_source: str | None = None
    current_timestamp: UtcInstant | None = None
    adjusted_previous_close: Decimal | None = None

    if item.needs_current_price:
        if quote.get("state") != "active":
            raise DailyLossError(f"quote {item.symbol}: state is not active")
        if quote.get("has_traded") is not True:
            raise DailyLossError(f"quote {item.symbol}: has_traded is not true")
        current_source, current_price, current_timestamp = _current_price(
            item.symbol, quote
        )
        _validate_current_price_time(
            item.symbol,
            current_timestamp,
            trading_day,
            previous_session,
            as_of,
        )

    if item.needs_previous_close:
        if "adjusted_previous_close" not in quote:
            raise DailyLossError(
                f"quote {item.symbol}.adjusted_previous_close: missing"
            )
        adjusted_previous_close = _decimal(
            quote["adjusted_previous_close"],
            f"quote {item.symbol}.adjusted_previous_close",
            positive=True,
        )
        close = _as_mapping(
            result.get("close"), f"quote {item.symbol}.close"
        )
        if close.get("interpolated") is not False:
            raise DailyLossError(
                f"quote {item.symbol}.close: official close is interpolated "
                "or unverified"
            )
        if close.get("date") != previous_session.isoformat():
            raise DailyLossError(
                f"quote {item.symbol}.close.date: expected "
                f"{previous_session.isoformat()}"
            )
        _decimal(
            close.get("price"),
            f"quote {item.symbol}.close.price",
            positive=True,
        )
        source = close.get("source")
        if source is not None and source != OFFICIAL_CLOSE_SOURCE:
            raise DailyLossError(
                f"quote {item.symbol}.close.source: not an official "
                "list-exchange close"
            )
        previous_close_date = quote.get("previous_close_date")
        if previous_close_date != previous_session.isoformat():
            raise DailyLossError(
                f"quote {item.symbol}.previous_close_date: does not match "
                "the expected previous session"
            )

    return (
        current_price,
        current_source,
        current_timestamp,
        adjusted_previous_close,
    )


def _reconciliation_json(item: ReconciledSymbol) -> dict[str, Any]:
    return {
        "symbol": item.symbol,
        "current_quantity": decimal_string(item.current_quantity),
        "reported_intraday_quantity": (
            None
            if item.reported_intraday_quantity is None
            else decimal_string(item.reported_intraday_quantity)
        ),
        "buy_quantity": decimal_string(item.buy_quantity),
        "sell_quantity": decimal_string(item.sell_quantity),
        "net_execution_quantity": decimal_string(
            item.net_execution_quantity
        ),
        "opening_quantity": decimal_string(item.opening_quantity),
        "buy_notional": decimal_string(item.buy_notional),
        "sell_notional": decimal_string(item.sell_notional),
        "execution_fees": decimal_string(item.execution_fees),
        "execution_count": item.execution_count,
    }


def calculate_daily_loss(
    portfolio_doc: Mapping[str, Any],
    position_pages: Sequence[Mapping[str, Any]],
    order_pages: Sequence[Mapping[str, Any]],
    quote_batches: Sequence[Mapping[str, Any]],
    trading_date: date | str,
    as_of_utc: str,
    halt_pct: Decimal | str | int,
    stop_date_pt: date | str,
) -> dict[str, Any]:
    """Calculate and return a schema-versioned fail-closed daily-loss result.

    ``halt_pct`` is a percentage (``5`` means five percent).  The breaker
    trips when daily P&L is exactly equal to or below the negative threshold.
    Every Decimal in the returned JSON-compatible mapping is represented by
    an exact string.
    """

    total_value = _portfolio_total(portfolio_doc)
    halt_percentage = _decimal(halt_pct, "halt percentage", positive=True)
    reconciliation = _reconcile(
        position_pages,
        order_pages,
        trading_date,
        as_of_utc,
        stop_date_pt,
    )
    quotes = _quote_results(quote_batches)

    required = set(reconciliation.required_symbols)
    missing = sorted(required - set(quotes))
    if missing:
        raise DailyLossError(
            "missing required quote result(s): " + ", ".join(missing)
        )
    unexpected = sorted(set(quotes) - required)
    if unexpected:
        raise DailyLossError(
            "unexpected quote result(s): " + ", ".join(unexpected)
        )

    symbol_results: list[dict[str, Any]] = []
    pnl_values: list[Decimal] = []
    for item in reconciliation.symbols:
        current_price: Decimal | None = None
        current_source: str | None = None
        current_timestamp: UtcInstant | None = None
        adjusted_previous_close: Decimal | None = None
        if item.needs_quote:
            (
                current_price,
                current_source,
                current_timestamp,
                adjusted_previous_close,
            ) = _validated_prices(
                item,
                quotes[item.symbol],
                reconciliation.previous_session,
                reconciliation.trading_day,
                reconciliation.as_of,
            )

        current_value = (
            Decimal(0)
            if not item.needs_current_price
            else _exact_product(item.current_quantity, current_price)
        )
        opening_value = (
            Decimal(0)
            if not item.needs_previous_close
            else _exact_product(
                item.opening_quantity, adjusted_previous_close
            )
        )
        pnl = _exact_sum(
            (
                current_value,
                item.sell_notional,
                -item.buy_notional,
                -opening_value,
                -item.execution_fees,
            )
        )
        pnl_values.append(pnl)

        detail = _reconciliation_json(item)
        detail.update(
            {
                "current_price": (
                    None
                    if current_price is None
                    else decimal_string(current_price)
                ),
                "current_price_source": current_source,
                "current_price_timestamp_utc": (
                    None
                    if current_timestamp is None
                    else current_timestamp.canonical
                ),
                "current_value": decimal_string(current_value),
                "adjusted_previous_close": (
                    None
                    if adjusted_previous_close is None
                    else decimal_string(adjusted_previous_close)
                ),
                "opening_value": decimal_string(opening_value),
                "daily_pnl": decimal_string(pnl),
            }
        )
        symbol_results.append(detail)

    daily_pnl = _exact_sum(pnl_values)
    halt_threshold = _exact_product(
        total_value, halt_percentage, Decimal("0.01")
    )
    halt_new_buys = daily_pnl <= -halt_threshold
    loss_amount = max(-daily_pnl, Decimal(0))
    with localcontext() as context:
        context.prec = 40
        loss_pct_of_total = loss_amount / total_value * Decimal(100)

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "tripped" if halt_new_buys else "clear",
        "halt_new_buys": halt_new_buys,
        "trading_date_et": reconciliation.trading_day.isoformat(),
        "stop_count_date_pt": reconciliation.stop_day_pt.isoformat(),
        "stop_fills_today": reconciliation.stop_fills_today,
        "stopped_out_symbols": list(reconciliation.stopped_out_symbols),
        "as_of_utc": reconciliation.as_of.canonical,
        "previous_session_date": reconciliation.previous_session.isoformat(),
        "total_value": decimal_string(total_value),
        "halt_pct": decimal_string(halt_percentage),
        "halt_threshold": decimal_string(halt_threshold),
        "daily_pnl": decimal_string(daily_pnl),
        "loss_amount": decimal_string(loss_amount),
        "loss_pct_of_total": decimal_string(loss_pct_of_total),
        "required_quote_symbols": reconciliation.required_symbols,
        "reconciliation": {
            "unique_order_count": reconciliation.unique_order_count,
            "today_execution_count": reconciliation.today_execution_count,
            "symbols": symbol_results,
        },
    }


# A concise alias for importers that describe the operation as evaluation.
evaluate_daily_loss = calculate_daily_loss


def _load_many(paths: Sequence[str]) -> list[Any]:
    return [load_json(path) for path in paths]


def _write_json_atomic(path: str, document: Any) -> None:
    destination = Path(path)
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        raise DailyLossError(f"{path}: output directory does not exist")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(document, handle, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    except (OSError, TypeError, ValueError) as exc:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        raise DailyLossError(f"{path}: cannot publish JSON: {exc}") from exc


def _write_json_no_clobber_or_match(
    path: str, document: Any
) -> tuple[bytes, bool]:
    """Publish deterministic evidence once, or reconcile identical bytes.

    A pre-existing path is never removed or replaced.  An exact byte match is
    the only idempotent lost-receipt recovery; a mismatch is retained for
    audit and fails closed.
    """
    destination = Path(path)
    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        raise DailyLossError(f"{path}: output directory does not exist")
    try:
        raw = (
            json.dumps(
                document,
                indent=2,
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DailyLossError(f"{path}: cannot serialize JSON: {exc}") from exc
    descriptor = -1
    temporary = ""
    created = False
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=parent
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, destination)
            created = True
        except FileExistsError:
            created = False
        try:
            before = os.lstat(destination)
            persisted = destination.read_bytes()
            after = os.lstat(destination)
        except OSError as exc:
            raise DailyLossError(
                f"{path}: cannot read back durable JSON: {exc}"
            ) from exc
        if (
            destination.is_symlink()
            or not destination.is_file()
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or persisted != raw
        ):
            raise DailyLossError(
                f"{path}: existing durable JSON does not exactly match this "
                "deterministic result"
            )
        return raw, created
    except DailyLossError:
        raise
    except OSError as exc:
        raise DailyLossError(f"{path}: cannot publish durable JSON: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def _validate_output_not_input(output: str, inputs: Sequence[str]) -> None:
    output_path = os.path.normcase(os.path.abspath(output))
    for input_path in inputs:
        if output_path == os.path.normcase(os.path.abspath(input_path)):
            raise DailyLossError(
                f"{output}: output path must not overwrite an input file"
            )


def _remove_failed_output(path: str | None, inputs: Sequence[str]) -> None:
    if path is None:
        return
    output_path = os.path.normcase(os.path.abspath(path))
    input_paths = {
        os.path.normcase(os.path.abspath(input_path)) for input_path in inputs
    }
    if output_path in input_paths:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve the original validation error; an unwritable stale output
        # is itself not evidence that a new valid result was published.
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--portfolio", help="calculation mode: raw get_portfolio JSON file"
    )
    parser.add_argument(
        "--positions",
        nargs="+",
        required=True,
        metavar="FILE",
        help="raw get_equity_positions pages, in pagination order",
    )
    parser.add_argument(
        "--orders",
        nargs="+",
        required=True,
        metavar="FILE",
        help="raw get_equity_orders pages, in pagination order",
    )
    parser.add_argument(
        "--quotes",
        nargs="+",
        metavar="FILE",
        help="calculation mode: raw get_equity_quotes batches",
    )
    parser.add_argument(
        "--trading-date", required=True, help="trading date in US Eastern, YYYY-MM-DD"
    )
    parser.add_argument(
        "--stop-date-pt",
        required=True,
        help="START CLOCK Pacific date for stop-fill counting, YYYY-MM-DD",
    )
    parser.add_argument(
        "--as-of-utc",
        required=True,
        help="calculation cutoff as an ISO-8601 UTC timestamp",
    )
    parser.add_argument(
        "--halt-pct", help="calculation mode: positive daily-loss halt percentage"
    )
    parser.add_argument(
        "--snapshot-generation",
        choices=("A", "B"),
        help=(
            "require every staged input to carry matching broker_snapshot "
            "generation provenance"
        ),
    )
    parser.add_argument(
        "--failure-json",
        action="store_true",
        help=(
            "emit one phase-typed JSON failure receipt on stdout instead of "
            "human-readable stderr"
        ),
    )
    parser.add_argument("--invocation-id", help=argparse.SUPPRESS)
    parser.add_argument("--run-token", help=argparse.SUPPRESS)
    parser.add_argument("--lifecycle-state-file", help=argparse.SUPPRESS)
    parser.add_argument("--lifecycle-projection-file", help=argparse.SUPPRESS)
    parser.add_argument("--lifecycle-context-file", help=argparse.SUPPRESS)
    parser.add_argument("--lifecycle-lock-file", help=argparse.SUPPRESS)
    parser.add_argument("--lifecycle-now-utc", help=argparse.SUPPRESS)
    parser.add_argument(
        "--expected-constants-sha256",
        help=(
            "lifecycle calculation: exact startup validate_constants.py hash"
        ),
    )
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "--json-out", help="write the final circuit-breaker result here"
    )
    output_group.add_argument(
        "--symbols-out",
        help="discovery mode: write the sorted required quote-symbol array here",
    )
    return parser


def _failure_receipt(
    *,
    mode: str,
    generation: str | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    """Return the closed machine failure envelope used by unattended runs."""

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "action": "daily-loss",
        "ok": False,
        "mode": mode,
        "generation": generation,
        "error": {"code": code, "message": message},
    }
    if code == "daily_loss_semantic_invalid" and generation in {"A", "B"}:
        receipt["recovery_action"] = (
            "generation-b"
            if generation == "A"
            else "snapshot-second-attempt-failed"
        )
    return receipt


def main(
    argv: Sequence[str] | None = None,
    *,
    lifecycle_now_utc: str | None = None,
) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    inputs = list(args.positions) + list(args.orders)
    if args.portfolio:
        inputs.append(args.portfolio)
    if args.quotes:
        inputs.extend(args.quotes)
    output = args.symbols_out or args.json_out
    mode = "discovery" if args.symbols_out else "calculation"
    phase = "binding"
    lifecycle_requested = any(
        value is not None
        for value in (
            args.invocation_id,
            args.run_token,
            args.lifecycle_state_file,
            args.lifecycle_projection_file,
            args.lifecycle_context_file,
            args.lifecycle_lock_file,
            args.lifecycle_now_utc,
            args.expected_constants_sha256,
        )
    )
    lifecycle_constants = None

    try:
        if args.lifecycle_now_utc is not None:
            raise DailyLossError(
                "--lifecycle-now-utc is test-only through the imported API "
                "and is not valid on the CLI"
            )
        if lifecycle_requested:
            if args.invocation_id is None or args.run_token is None:
                raise DailyLossError(
                    "lifecycle completion requires --invocation-id and "
                    "--run-token together"
                )
            if args.symbols_out:
                raise DailyLossError(
                    "lifecycle completion is valid only in calculation mode"
                )
            if args.snapshot_generation is None:
                raise DailyLossError(
                    "lifecycle completion requires --snapshot-generation"
                )
            if args.expected_constants_sha256 is None:
                raise DailyLossError(
                    "lifecycle completion requires "
                    "--expected-constants-sha256"
                )
            if args.halt_pct is not None:
                raise DailyLossError(
                    "lifecycle calculation derives DAILY_LOSS_HALT_PCT from "
                    "the bound constants and does not accept --halt-pct"
                )
            try:
                lifecycle_constants = validate_constants_file()
            except ConstantsValidationError as exc:
                raise DailyLossError(
                    f"constants validation failed: {exc}"
                ) from exc
            if (
                not re.fullmatch(
                    r"[0-9a-f]{64}", args.expected_constants_sha256
                )
                or lifecycle_constants.source_sha256
                != args.expected_constants_sha256
            ):
                raise DailyLossError(
                    "constants changed after startup validation"
                )
        if args.symbols_out:
            unused_discovery_options = []
            if args.portfolio:
                unused_discovery_options.append("--portfolio")
            if args.quotes:
                unused_discovery_options.append("--quotes")
            if args.halt_pct is not None:
                unused_discovery_options.append("--halt-pct")
            if unused_discovery_options:
                raise DailyLossError(
                    "discovery mode does not accept unused calculation "
                    "option(s): " + ", ".join(unused_discovery_options)
                )
        _validate_output_not_input(output, inputs)
        _parse_date(args.trading_date, "--trading-date")
        _parse_date(args.stop_date_pt, "--stop-date-pt")
        parse_utc_instant(args.as_of_utc, "--as-of-utc")
        if args.json_out:
            missing_options = []
            if not args.portfolio:
                missing_options.append("--portfolio")
            if args.halt_pct is None and not lifecycle_requested:
                missing_options.append("--halt-pct")
            if missing_options:
                raise DailyLossError(
                    "calculation mode requires " + ", ".join(missing_options)
                )
            if not lifecycle_requested:
                _decimal(args.halt_pct, "--halt-pct", positive=True)

        phase = "input"
        paths_by_kind = {
            "positions": list(args.positions),
            "orders": list(args.orders),
            "quotes": list(args.quotes or []),
            "portfolio": [args.portfolio] if args.portfolio else [],
        }
        has_staging_metadata = any(
            os.path.exists(path + STAGE_METADATA_SUFFIX) for path in inputs
        )
        if args.snapshot_generation is not None:
            try:
                validate_generation_inputs(
                    paths_by_kind, args.snapshot_generation
                )
            except SnapshotError as exc:
                raise DailyLossError(
                    f"snapshot generation provenance failed: {exc}"
                ) from exc
        elif has_staging_metadata:
            raise DailyLossError(
                "staged snapshot inputs require --snapshot-generation"
            )
        position_pages = _load_many(args.positions)
        order_pages = _load_many(args.orders)
        portfolio_document = load_json(args.portfolio) if args.portfolio else None
        quote_batches = _load_many(args.quotes or [])

        if args.symbols_out:
            phase = "semantic"
            reconciliation = _reconcile(
                position_pages,
                order_pages,
                args.trading_date,
                args.as_of_utc,
                args.stop_date_pt,
            )
            symbols = reconciliation.required_symbols
            phase = "output"
            _write_json_atomic(args.symbols_out, symbols)
            print(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "action": "discover-symbols",
                        "ok": True,
                        "trading_date_et": args.trading_date,
                        "as_of_utc": args.as_of_utc,
                        "symbol_count": len(symbols),
                        "symbols": symbols,
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
            return 0

        phase = "semantic"
        result = calculate_daily_loss(
            portfolio_document,
            position_pages,
            order_pages,
            quote_batches,
            args.trading_date,
            args.as_of_utc,
            (
                lifecycle_constants.raw_values["DAILY_LOSS_HALT_PCT"]
                if lifecycle_requested
                else args.halt_pct
            ),
            args.stop_date_pt,
        )
        phase = "output"
        if lifecycle_requested:
            assert lifecycle_constants is not None
            stop_count_halt = int(
                lifecycle_constants.values["STOP_COUNT_HALT"]
            )
            daily_loss_tripped = result["halt_new_buys"]
            stop_count_tripped = (
                result["stop_fills_today"] >= stop_count_halt
            )
            stdout_result = {
                "schema_version": SCHEMA_VERSION,
                "action": "daily-loss",
                "ok": True,
                "mode": "calculation",
                "generation": args.snapshot_generation,
                "invocation_id": args.invocation_id,
                "constants_sha256": lifecycle_constants.source_sha256,
                "daily_loss_halt_pct": lifecycle_constants.raw_values[
                    "DAILY_LOSS_HALT_PCT"
                ],
                "stop_count_halt": stop_count_halt,
                "daily_loss_tripped": daily_loss_tripped,
                "stop_count_tripped": stop_count_tripped,
                "entry_guard_outcome": (
                    "tripped"
                    if daily_loss_tripped or stop_count_tripped
                    else "clear"
                ),
                "result": result,
            }
            try:
                run_lifecycle_module.validate_second_result_target(
                    invocation_id=args.invocation_id,
                    run_token=args.run_token,
                    result_file=os.path.abspath(args.json_out),
                    generation=args.snapshot_generation,
                    expected_constants_sha256=(
                        lifecycle_constants.source_sha256
                    ),
                    state_file=(
                        args.lifecycle_state_file
                        or run_lifecycle_module.DEFAULT_STATE_FILE
                    ),
                    projection_file=(
                        args.lifecycle_projection_file
                        or run_lifecycle_module.DEFAULT_PROJECTION_FILE
                    ),
                    context_file=(
                        args.lifecycle_context_file
                        or run_lifecycle_module.DEFAULT_CONTEXT_FILE
                    ),
                    lock_file=args.lifecycle_lock_file,
                    now_utc=lifecycle_now_utc,
                )
            except (
                run_lifecycle_module.LifecycleError,
                OSError,
                sqlite3.Error,
            ) as exc:
                raise DailyLossError(
                    f"lifecycle SECOND target validation failed: {exc}"
                ) from exc
            _write_json_no_clobber_or_match(args.json_out, stdout_result)
            phase = "checkpoint"
            try:
                checkpoint = run_lifecycle_module.complete_second_result(
                    invocation_id=args.invocation_id,
                    run_token=args.run_token,
                    result_file=os.path.abspath(args.json_out),
                    generation=args.snapshot_generation,
                    portfolio=os.path.abspath(args.portfolio),
                    positions=[os.path.abspath(path) for path in args.positions],
                    orders=[os.path.abspath(path) for path in args.orders],
                    quotes=[os.path.abspath(path) for path in (args.quotes or [])],
                    state_file=(
                        args.lifecycle_state_file
                        or run_lifecycle_module.DEFAULT_STATE_FILE
                    ),
                    projection_file=(
                        args.lifecycle_projection_file
                        or run_lifecycle_module.DEFAULT_PROJECTION_FILE
                    ),
                    context_file=(
                        args.lifecycle_context_file
                        or run_lifecycle_module.DEFAULT_CONTEXT_FILE
                    ),
                    lock_file=args.lifecycle_lock_file,
                    now_utc=lifecycle_now_utc,
                )
            except (
                run_lifecycle_module.LifecycleError,
                OSError,
                sqlite3.Error,
            ) as exc:
                raise DailyLossError(
                    f"lifecycle SECOND completion failed: {exc}"
                ) from exc
            if checkpoint["outcome"] != stdout_result["entry_guard_outcome"]:
                raise DailyLossError(
                    "lifecycle SECOND completion outcome changed after binding"
                )
        else:
            _write_json_atomic(args.json_out, result)
            stdout_result = result
        print(
            json.dumps(
                stdout_result,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
        return 0
    except (DailyLossError, OSError) as exc:
        if not lifecycle_requested:
            _remove_failed_output(output, inputs)
        if args.failure_json:
            if isinstance(exc, DailyLossInternalError) or (
                isinstance(exc, OSError) and phase == "semantic"
            ):
                code = "daily_loss_internal_failed"
            else:
                code = {
                    "binding": "daily_loss_binding_invalid",
                    "input": "daily_loss_input_failed",
                    "semantic": "daily_loss_semantic_invalid",
                    "output": "daily_loss_output_failed",
                    "checkpoint": "daily_loss_checkpoint_failed",
                }.get(phase, "daily_loss_internal_failed")
            print(
                json.dumps(
                    _failure_receipt(
                        mode=mode,
                        generation=args.snapshot_generation,
                        code=code,
                        message=str(exc),
                    ),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            )
        else:
            print(f"daily_loss.py: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
