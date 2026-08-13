#!/usr/bin/env python3
"""Durable order-intent journal for Robinhood equity placements.

The Robinhood Trading MCP deduplicates transient placement retries only when
the same ``ref_id`` is reused.  This helper creates that UUID once, persists
the exact logical order before the broker call, records acknowledgement and
later broker states, and keeps ambiguous outcomes blocking until reconciled.

The helper never talks to Robinhood and never stores an account number.  The
routine still resolves the configured account name on every run and adds that
run's account number only to the MCP call.  Broker order/position responses
remain the source of truth.

Commands (all emit one JSON object):

  order_intents.py check
  order_intents.py pending [--run-token UUID]
  order_intents.py prepare --intent FILE
  order_intents.py begin --intent-id UUID --run-token UUID
  order_intents.py retry --intent-id UUID --run-token UUID
  order_intents.py acknowledge --intent-id UUID --response FILE --transport-scratch SCRATCH
  order_intents.py mark-unknown --intent-id UUID --code CODE
  order_intents.py observe --intent-id UUID --orders FILE... \
      [--order-request-cursors FIRST CURSOR...] --positions FILE... \
      [--position-request-cursors FIRST CURSOR...] --as-of-utc TIMESTAMP
  order_intents.py abandon-prepared --intent-id UUID --note TEXT
  order_intents.py operator-bind --intent-id UUID --order-id UUID --note TEXT
  order_intents.py operator-resolve-not-submitted --intent-id UUID --note TEXT

``--state-file`` and ``--now-utc`` are for tests and diagnostics.  The live
routine uses the checked-in default under gitignored ``run-reports/``.

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
``python3 tests/test_scripts.py`` (Windows: ``py -3 tests\\test_scripts.py``)
and require all tests to pass before committing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit

from broker_snapshot import validate_bound_external_json_source


SCHEMA_VERSION = 1
MAX_BASELINE_AGE_SECONDS = 120
BROKER_CLOCK_SKEW_SECONDS = 5
DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "run-reports",
    "rhmra-order-intents.sqlite3",
)

PURPOSES = frozenset(
    {
        "dip-buy",
        "profit-take",
        "dust-sweep",
        "initial-stop",
        "stop-repair",
        "stop-retry",
        "profit-take-stop-restore",
    }
)
STATUSES = frozenset(
    {
        "prepared",
        "submitting",
        "unknown",
        "acknowledged",
        "working",
        "partially_filled",
        "resolved",
        "indeterminate",
        "abandoned",
    }
)
WORKING_BROKER_STATES = frozenset(
    {
        "new",
        "queued",
        "confirmed",
        "unconfirmed",
        "partially_filled",
        "pending_cancelled",
    }
)
TERMINAL_BROKER_STATES = frozenset(
    {
        "filled",
        "cancelled",
        "rejected",
        "failed",
        "voided",
        "partially_filled_rest_cancelled",
    }
)
UNSUPPORTED_LONG_ONLY_STATES = frozenset({"locating", "locate_failed"})
BROKER_STATES = (
    WORKING_BROKER_STATES
    | TERMINAL_BROKER_STATES
    | UNSUPPORTED_LONG_ONLY_STATES
)

INTENT_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "account_name",
        "run_token",
        "run_start_utc",
        "rules_version",
        "constants_sha256",
        "purpose",
        "replaces_intent_id",
        "order",
        "baseline",
    }
)
ORDER_COMMON_KEYS = frozenset(
    {"symbol", "side", "type", "market_hours", "time_in_force"}
)
ORDER_OPTIONAL_KEYS = frozenset(
    {"quantity", "dollar_amount", "limit_price", "stop_price"}
)
BASELINE_KEYS = frozenset(
    {"observed_at_utc", "position_quantity", "symbol_order_ids"}
)

_DECIMAL_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$"
)


class OrderIntentError(ValueError):
    """Persistent intent input/state is malformed or unsafe to interpret."""


def _object_no_duplicates(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OrderIntentError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise OrderIntentError(f"non-finite JSON number is forbidden: {value}")


def load_json(path: str, context: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(
                handle,
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
    except OrderIntentError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OrderIntentError(f"{context}: cannot read strict JSON: {exc}") from exc


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OrderIntentError(f"{context}: expected an object")
    return value


def _exact_keys(
    value: Mapping[str, Any],
    required: Iterable[str],
    optional: Iterable[str],
    context: str,
) -> None:
    required_set = set(required)
    allowed = required_set | set(optional)
    missing = sorted(required_set - set(value))
    extra = sorted(set(value) - allowed)
    if missing:
        raise OrderIntentError(f"{context}: missing key(s): {', '.join(missing)}")
    if extra:
        raise OrderIntentError(f"{context}: unknown key(s): {', '.join(extra)}")


def _text(value: Any, context: str, *, maximum: int = 300) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrderIntentError(f"{context}: expected a non-empty string")
    if value != value.strip():
        raise OrderIntentError(f"{context}: leading/trailing whitespace is forbidden")
    if len(value) > maximum:
        raise OrderIntentError(f"{context}: exceeds {maximum} characters")
    return value


def _uuid(value: Any, context: str) -> str:
    text = _text(value, context, maximum=36)
    try:
        parsed = uuid.UUID(text)
    except (ValueError, AttributeError) as exc:
        raise OrderIntentError(f"{context}: expected a canonical UUID") from exc
    canonical = str(parsed)
    if text != canonical:
        raise OrderIntentError(f"{context}: expected a lowercase canonical UUID")
    return canonical


def _decimal(
    value: Any,
    context: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        raise OrderIntentError(
            f"{context}: expected a finite base-10 decimal string"
        )
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise OrderIntentError(f"{context}: invalid decimal") from exc
    if not parsed.is_finite():
        raise OrderIntentError(f"{context}: decimal must be finite")
    if positive and parsed <= 0:
        raise OrderIntentError(f"{context}: must be greater than zero")
    if nonnegative and parsed < 0:
        raise OrderIntentError(f"{context}: must be nonnegative")
    return parsed


def _utc(value: Any, context: str) -> datetime:
    text = _text(value, context, maximum=40)
    if not _UTC_RE.fullmatch(text):
        raise OrderIntentError(f"{context}: expected an ISO-8601 UTC timestamp")
    try:
        # Broker precision varies; seconds are sufficient for matching windows.
        parsed = datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        raise OrderIntentError(f"{context}: invalid UTC timestamp") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _utc_sort_key(value: Any, context: str) -> tuple[datetime, Decimal, str]:
    text = _text(value, context, maximum=50)
    instant = _utc(text, context)
    match = _UTC_RE.fullmatch(text)
    assert match is not None
    fraction_match = re.search(r"\.(\d+)(?:Z|\+00:00)$", text)
    fraction = (
        Decimal(f"0.{fraction_match.group(1)}")
        if fraction_match is not None
        else Decimal(0)
    )
    return instant, fraction, text


def _now(value: str | None) -> tuple[str, datetime]:
    if value is None:
        current = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        current = _utc(value, "--now-utc").replace(microsecond=0)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ"), current


def _canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stored_sha256(value: Any, context: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise OrderIntentError(f"{context}: malformed SHA-256 value")
    return value


def validate_order(raw: Any) -> dict[str, Any]:
    order = dict(_mapping(raw, "intent.order"))
    _exact_keys(order, ORDER_COMMON_KEYS, ORDER_OPTIONAL_KEYS, "intent.order")

    symbol = _text(order["symbol"], "intent.order.symbol", maximum=15)
    if not _SYMBOL_RE.fullmatch(symbol):
        raise OrderIntentError(
            "intent.order.symbol: expected an uppercase stock ticker"
        )
    side = order["side"]
    if side not in {"buy", "sell"}:
        raise OrderIntentError("intent.order.side: expected buy or sell")
    order_type = order["type"]
    if order_type not in {"market", "limit", "stop_market"}:
        raise OrderIntentError("intent.order.type: unsupported order type")
    market_hours = order["market_hours"]
    if market_hours not in {"regular_hours", "extended_hours"}:
        raise OrderIntentError("intent.order.market_hours: unsupported session")
    time_in_force = order["time_in_force"]
    if time_in_force not in {"gfd", "gtc"}:
        raise OrderIntentError("intent.order.time_in_force: expected gfd or gtc")

    has_quantity = "quantity" in order
    has_dollars = "dollar_amount" in order
    if has_quantity == has_dollars:
        raise OrderIntentError(
            "intent.order: provide exactly one of quantity or dollar_amount"
        )
    if has_quantity:
        _decimal(order["quantity"], "intent.order.quantity", positive=True)
    else:
        _decimal(
            order["dollar_amount"], "intent.order.dollar_amount", positive=True
        )

    required_prices: set[str] = set()
    if order_type == "limit":
        required_prices.add("limit_price")
    if order_type == "stop_market":
        required_prices.add("stop_price")
    for key in ("limit_price", "stop_price"):
        if key in required_prices and key not in order:
            raise OrderIntentError(f"intent.order.{key}: missing")
        if key not in required_prices and key in order:
            raise OrderIntentError(
                f"intent.order.{key}: forbidden for type {order_type}"
            )
        if key in order:
            _decimal(order[key], f"intent.order.{key}", positive=True)

    if has_dollars and not (
        order_type == "market" and market_hours == "regular_hours"
    ):
        raise OrderIntentError(
            "intent.order.dollar_amount: requires a regular-hours market order"
        )
    if order_type in {"limit", "stop_market"} and not has_quantity:
        raise OrderIntentError(
            f"intent.order.quantity: required for type {order_type}"
        )
    if has_dollars and side != "buy":
        raise OrderIntentError("intent.order.dollar_amount: sell orders require quantity")
    if order_type == "stop_market":
        if side != "sell" or market_hours != "regular_hours":
            raise OrderIntentError(
                "intent.order: stops must be regular-hours sell orders"
            )
        if time_in_force != "gtc":
            raise OrderIntentError("intent.order: protective stops must be gtc")
        quantity = _decimal(order["quantity"], "intent.order.quantity", positive=True)
        if quantity != quantity.to_integral_value():
            raise OrderIntentError("intent.order.quantity: stops require whole shares")
    elif time_in_force != "gfd":
        raise OrderIntentError("intent.order: non-stop orders must be gfd")
    if market_hours != "regular_hours" and order_type != "limit":
        raise OrderIntentError(
            "intent.order: extended-hours sessions require a limit order"
        )
    if market_hours == "extended_hours":
        quantity = _decimal(order["quantity"], "intent.order.quantity", positive=True)
        if quantity != quantity.to_integral_value():
            raise OrderIntentError(
                "intent.order.quantity: extended-hours orders require whole shares"
            )
    return order


def validate_baseline(
    raw: Any,
    run_start_utc: str,
    context: str = "intent.baseline",
) -> dict[str, Any]:
    baseline = dict(_mapping(raw, context))
    _exact_keys(baseline, BASELINE_KEYS, (), context)
    observed = _text(
        baseline["observed_at_utc"],
        f"{context}.observed_at_utc",
        maximum=40,
    )
    if _utc(observed, f"{context}.observed_at_utc") < _utc(
        run_start_utc, "intent.run_start_utc"
    ):
        raise OrderIntentError(
            f"{context}.observed_at_utc: predates the run start"
        )
    _decimal(
        baseline["position_quantity"],
        f"{context}.position_quantity",
        nonnegative=True,
    )
    ids = baseline["symbol_order_ids"]
    if not isinstance(ids, list):
        raise OrderIntentError(f"{context}.symbol_order_ids: expected an array")
    normalized_ids = [
        _uuid(value, f"{context}.symbol_order_ids[{index}]")
        for index, value in enumerate(ids)
    ]
    if len(set(normalized_ids)) != len(normalized_ids):
        raise OrderIntentError(f"{context}.symbol_order_ids: duplicate order ID")
    baseline["symbol_order_ids"] = normalized_ids
    return baseline


def validate_intent_document(raw: Any) -> dict[str, Any]:
    intent = dict(_mapping(raw, "intent"))
    _exact_keys(intent, INTENT_REQUIRED_KEYS, (), "intent")
    if intent["schema_version"] != SCHEMA_VERSION or isinstance(
        intent["schema_version"], bool
    ):
        raise OrderIntentError("intent.schema_version: expected integer 1")
    account_name = _text(intent["account_name"], "intent.account_name")
    run_token = _uuid(intent["run_token"], "intent.run_token")
    run_start = _text(intent["run_start_utc"], "intent.run_start_utc", maximum=40)
    _utc(run_start, "intent.run_start_utc")
    rules_version = _text(intent["rules_version"], "intent.rules_version")
    constants_sha256 = intent["constants_sha256"]
    if not isinstance(constants_sha256, str) or not _SHA256_RE.fullmatch(
        constants_sha256
    ):
        raise OrderIntentError(
            "intent.constants_sha256: expected 64 lowercase hex characters"
        )
    purpose = intent["purpose"]
    if purpose not in PURPOSES:
        raise OrderIntentError("intent.purpose: unsupported purpose")
    replaces_intent_id = intent["replaces_intent_id"]
    if purpose == "stop-retry":
        replaces_intent_id = _uuid(
            replaces_intent_id, "intent.replaces_intent_id"
        )
    elif replaces_intent_id is not None:
        raise OrderIntentError(
            "intent.replaces_intent_id: allowed only for stop-retry"
        )
    order = validate_order(intent["order"])

    expected_side = "buy" if purpose == "dip-buy" else "sell"
    if order["side"] != expected_side:
        raise OrderIntentError(
            f"intent.order.side: purpose {purpose} requires {expected_side}"
        )
    stop_purposes = {
        "initial-stop", "stop-repair", "stop-retry",
        "profit-take-stop-restore",
    }
    if purpose in stop_purposes and order["type"] != "stop_market":
        raise OrderIntentError(
            f"intent.order.type: purpose {purpose} requires stop_market"
        )
    if purpose not in stop_purposes and order["type"] == "stop_market":
        raise OrderIntentError(
            f"intent.order.type: purpose {purpose} cannot be a stop order"
        )
    if purpose in {"profit-take", "dust-sweep"} and not (
        order["type"] == "market"
        and order["market_hours"] == "regular_hours"
        and "quantity" in order
    ):
        raise OrderIntentError(
            f"intent.order: purpose {purpose} requires a regular-hours "
            "quantity market sell"
        )
    if purpose == "dip-buy":
        valid_entry_shape = (
            order["market_hours"] == "regular_hours"
            and order["type"] == "market"
        ) or (
            order["market_hours"] == "extended_hours"
            and order["type"] == "limit"
            and "quantity" in order
        )
        if not valid_entry_shape:
            raise OrderIntentError(
                "intent.order: dip-buy requires a regular-hours market order "
                "or an extended-hours whole-share limit order"
            )
        if "quantity" in order:
            quantity = _decimal(
                order["quantity"], "intent.order.quantity", positive=True
            )
            if quantity != quantity.to_integral_value():
                raise OrderIntentError(
                    "intent.order.quantity: share-based dip buys require whole shares"
                )

    baseline = validate_baseline(intent["baseline"], run_start)

    return {
        "schema_version": SCHEMA_VERSION,
        "account_name": account_name,
        "run_token": run_token,
        "run_start_utc": run_start,
        "rules_version": rules_version,
        "constants_sha256": constants_sha256,
        "purpose": purpose,
        "replaces_intent_id": replaces_intent_id,
        "order": order,
        "baseline": baseline,
    }


def immutable_intent_document(document: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "account_name": document["account_name"],
        "run_token": document["run_token"],
        "run_start_utc": document["run_start_utc"],
        "rules_version": document["rules_version"],
        "constants_sha256": document["constants_sha256"],
        "purpose": document["purpose"],
        "replaces_intent_id": document["replaces_intent_id"],
        "order": document["order"],
        "baseline": document["baseline"],
    }


def _validate_schema(connection: sqlite3.Connection) -> None:
    expected_columns = {
        "metadata": {
            "key", "value",
        },
        "intents": {
            "intent_id", "ref_id", "account_name", "run_token",
            "run_start_utc", "rules_version", "constants_sha256",
            "purpose", "replaces_intent_id", "order_json", "order_sha256",
            "baseline_json", "baseline_sha256", "intent_sha256", "status",
            "submit_attempts", "first_submit_at", "last_submit_at",
            "broker_order_id", "broker_state", "cumulative_quantity",
            "average_fill_price", "last_execution_at", "last_observed_at",
            "outcome", "last_error_code", "created_at", "updated_at",
        },
        "intent_events": {
            "sequence", "intent_id", "occurred_at", "event", "detail_json",
        },
    }
    expected_primary_keys = {
        "metadata": {"key"},
        "intents": {"intent_id"},
        "intent_events": {"sequence"},
    }
    nullable_columns = {
        "metadata": set(),
        "intents": {
            "replaces_intent_id", "first_submit_at", "last_submit_at",
            "broker_order_id", "broker_state", "average_fill_price",
            "last_execution_at", "last_observed_at", "outcome",
            "last_error_code",
        },
        "intent_events": set(),
    }
    expected_unique = {
        "metadata": {("key",)},
        "intents": {
            ("intent_id",), ("ref_id",), ("broker_order_id",),
            ("replaces_intent_id",),
        },
        "intent_events": set(),
    }
    for table, expected in expected_columns.items():
        info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        actual = {item["name"] for item in info}
        if actual != expected:
            raise OrderIntentError(f"journal table {table} has an unsafe schema")
        primary_keys = {item["name"] for item in info if item["pk"]}
        if primary_keys != expected_primary_keys[table]:
            raise OrderIntentError(
                f"journal table {table} has unsafe primary-key constraints"
            )
        for item in info:
            if item["type"].upper() not in {"TEXT", "INTEGER"}:
                raise OrderIntentError(
                    f"journal table {table} has an unsafe column type"
                )
            if (
                item["name"] not in nullable_columns[table]
                and not item["notnull"]
                and not item["pk"]
            ):
                raise OrderIntentError(
                    f"journal table {table} has an unsafe nullability constraint"
                )
        unique_columns: set[tuple[str, ...]] = set()
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if not index["unique"]:
                continue
            columns = tuple(
                row["name"]
                for row in connection.execute(
                    f"PRAGMA index_info({index['name']})"
                ).fetchall()
            )
            unique_columns.add(columns)
        if unique_columns != expected_unique[table]:
            raise OrderIntentError(
                f"journal table {table} has unsafe uniqueness constraints"
            )
    event_fks = connection.execute("PRAGMA foreign_key_list(intent_events)").fetchall()
    if len(event_fks) != 1 or (
        event_fks[0]["table"], event_fks[0]["from"], event_fks[0]["to"]
    ) != ("intents", "intent_id", "intent_id"):
        raise OrderIntentError("journal event foreign key is unsafe")
    intent_fks = connection.execute("PRAGMA foreign_key_list(intents)").fetchall()
    if len(intent_fks) != 1 or (
        intent_fks[0]["table"], intent_fks[0]["from"], intent_fks[0]["to"]
    ) != ("intents", "replaces_intent_id", "intent_id"):
        raise OrderIntentError("journal replacement foreign key is unsafe")


def connect(state_file: str) -> sqlite3.Connection:
    path = os.path.abspath(state_file)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    created = False
    try:
        descriptor = os.open(
            path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise OrderIntentError(
                "existing journal is empty or not a regular database file"
            )
    else:
        os.close(descriptor)
        created = True

    connection = sqlite3.connect(path, timeout=5, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    if created:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                "CREATE TABLE metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                """
                CREATE TABLE intents (
                    intent_id TEXT PRIMARY KEY,
                    ref_id TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL,
                    run_token TEXT NOT NULL,
                    run_start_utc TEXT NOT NULL,
                    rules_version TEXT NOT NULL,
                    constants_sha256 TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    replaces_intent_id TEXT UNIQUE REFERENCES intents(intent_id),
                    order_json TEXT NOT NULL,
                    order_sha256 TEXT NOT NULL,
                    baseline_json TEXT NOT NULL,
                    baseline_sha256 TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    submit_attempts INTEGER NOT NULL,
                    first_submit_at TEXT,
                    last_submit_at TEXT,
                    broker_order_id TEXT UNIQUE,
                    broker_state TEXT,
                    cumulative_quantity TEXT NOT NULL,
                    average_fill_price TEXT,
                    last_execution_at TEXT,
                    last_observed_at TEXT,
                    outcome TEXT,
                    last_error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE intent_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    FOREIGN KEY(intent_id) REFERENCES intents(intent_id)
                )
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise

    _validate_schema(connection)
    metadata = connection.execute("SELECT key, value FROM metadata").fetchall()
    if len(metadata) != 1 or metadata[0]["key"] != "schema_version":
        connection.close()
        raise OrderIntentError("journal metadata is missing or malformed")
    if metadata[0]["value"] != str(SCHEMA_VERSION):
        connection.close()
        raise OrderIntentError(
            f"journal schema {metadata[0]['value']!r} is not supported"
        )
    return connection


def _row(connection: sqlite3.Connection, intent_id: str) -> sqlite3.Row:
    normalized = _uuid(intent_id, "intent_id")
    row = connection.execute(
        "SELECT * FROM intents WHERE intent_id = ?", (normalized,)
    ).fetchone()
    if row is None:
        raise OrderIntentError(f"intent {normalized} does not exist")
    validate_persisted_row(row)
    return row


def validate_persisted_row(row: sqlite3.Row) -> None:
    _uuid(row["intent_id"], "stored intent_id")
    _uuid(row["ref_id"], "stored ref_id")
    if row["intent_id"] != row["ref_id"]:
        raise OrderIntentError("stored intent_id/ref_id identity is inconsistent")
    account_name = _text(row["account_name"], "stored account_name")
    run_token = _uuid(row["run_token"], "stored run_token")
    run_start = _text(row["run_start_utc"], "stored run_start_utc", maximum=40)
    run_start_instant = _utc(run_start, "stored run_start_utc")
    rules_version = _text(row["rules_version"], "stored rules_version")
    constants_sha256 = _stored_sha256(
        row["constants_sha256"], "stored constants hash"
    )
    order_sha256 = _stored_sha256(
        row["order_sha256"], "stored order payload hash"
    )
    baseline_sha256 = _stored_sha256(
        row["baseline_sha256"], "stored baseline hash"
    )
    intent_sha256 = _stored_sha256(
        row["intent_sha256"], "stored immutable intent hash"
    )
    if row["purpose"] not in PURPOSES:
        raise OrderIntentError("stored purpose is unsupported")
    replaces_intent_id = row["replaces_intent_id"]
    if replaces_intent_id is not None:
        replaces_intent_id = _uuid(
            replaces_intent_id, "stored replaces_intent_id"
        )
    try:
        order = json.loads(
            row["order_json"], object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
        baseline = json.loads(
            row["baseline_json"], object_pairs_hook=_object_no_duplicates,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, OrderIntentError) as exc:
        raise OrderIntentError("stored JSON is malformed") from exc
    document = validate_intent_document({
        "schema_version": SCHEMA_VERSION,
        "account_name": account_name,
        "run_token": run_token,
        "run_start_utc": run_start,
        "rules_version": rules_version,
        "constants_sha256": constants_sha256,
        "purpose": row["purpose"],
        "replaces_intent_id": replaces_intent_id,
        "order": order,
        "baseline": baseline,
    })
    order = document["order"]
    baseline = document["baseline"]
    if row["order_json"] != _canonical(order):
        raise OrderIntentError("stored order payload is not canonical")
    if row["baseline_json"] != _canonical(baseline):
        raise OrderIntentError("stored baseline is not canonical")
    if _sha256(row["order_json"]) != order_sha256:
        raise OrderIntentError("stored order payload hash does not match")
    if _sha256(row["baseline_json"]) != baseline_sha256:
        raise OrderIntentError("stored baseline hash does not match")
    if _sha256(_canonical(immutable_intent_document(document))) != intent_sha256:
        raise OrderIntentError("stored immutable intent hash does not match")
    if row["status"] not in STATUSES:
        raise OrderIntentError("stored status is unsupported")
    attempts = row["submit_attempts"]
    if isinstance(attempts, bool) or not isinstance(attempts, int) or not 0 <= attempts <= 2:
        raise OrderIntentError("stored submit_attempts is invalid")
    first_submit: datetime | None = None
    last_submit: datetime | None = None
    if attempts == 0 and (row["first_submit_at"] or row["last_submit_at"]):
        raise OrderIntentError("stored submit timestamps conflict with zero attempts")
    if attempts > 0:
        first_submit = _utc(row["first_submit_at"], "stored first_submit_at")
        last_submit = _utc(row["last_submit_at"], "stored last_submit_at")
        if last_submit < first_submit:
            raise OrderIntentError("stored submit timestamps regress")
    if row["broker_order_id"] is not None:
        _uuid(row["broker_order_id"], "stored broker_order_id")
    if row["broker_state"] is not None and row["broker_state"] not in BROKER_STATES:
        raise OrderIntentError("stored broker state is unsupported")
    cumulative = _decimal(
        row["cumulative_quantity"],
        "stored cumulative_quantity",
        nonnegative=True,
    )
    if row["broker_state"] is not None and row["broker_order_id"] is None:
        raise OrderIntentError("stored broker state has no broker order ID")
    if cumulative > 0 and (
        row["broker_order_id"] is None or row["broker_state"] is None
    ):
        raise OrderIntentError("stored fill quantity has no broker evidence")
    if cumulative == 0:
        if row["average_fill_price"] is not None or row["last_execution_at"] is not None:
            raise OrderIntentError("stored zero-fill intent has fill metadata")
    else:
        _decimal(
            row["average_fill_price"],
            "stored average_fill_price",
            positive=True,
        )
        _utc(row["last_execution_at"], "stored last_execution_at")
    last_observed: datetime | None = None
    if row["last_observed_at"] is not None:
        last_observed = _utc(row["last_observed_at"], "stored last_observed_at")
        if first_submit is None:
            raise OrderIntentError("stored observation has no submission")
        if last_observed < first_submit - timedelta(
            seconds=BROKER_CLOCK_SKEW_SECONDS
        ):
            raise OrderIntentError("stored observation predates submission")
    if row["broker_state"] is not None and last_observed is None:
        raise OrderIntentError("stored broker state has no observation timestamp")
    if cumulative > 0 and last_observed is None:
        raise OrderIntentError("stored fill has no observation timestamp")
    if row["last_execution_at"] is not None and last_observed is not None:
        if _utc(row["last_execution_at"], "stored last_execution_at") > (
            last_observed + timedelta(seconds=BROKER_CLOCK_SKEW_SECONDS)
        ):
            raise OrderIntentError("stored fill timestamp is later than observation")
    outcome = row["outcome"]
    if outcome is not None:
        outcome = _text(outcome, "stored outcome", maximum=100)
    last_error_code = row["last_error_code"]
    if last_error_code is not None:
        _text(last_error_code, "stored last_error_code", maximum=100)
    if row["status"] in {"prepared", "abandoned"}:
        if (
            attempts != 0
            or row["broker_order_id"] is not None
            or row["broker_state"] is not None
            or cumulative != 0
        ):
            raise OrderIntentError("stored unsubmitted intent has broker evidence")
    elif attempts == 0:
        raise OrderIntentError("stored submitted status has zero attempts")
    if row["status"] == "acknowledged" and row["broker_order_id"] is None:
        raise OrderIntentError("stored acknowledged intent has no broker order ID")
    if row["status"] == "submitting" and (
        row["broker_order_id"] is not None
        or row["broker_state"] is not None
        or cumulative != 0
    ):
        raise OrderIntentError("stored submitting intent has broker evidence")
    if row["status"] == "unknown" and (
        row["broker_order_id"] is not None
        or row["broker_state"] is not None
        or cumulative != 0
        or last_error_code is None
    ):
        raise OrderIntentError("stored unknown intent is internally inconsistent")
    if row["status"] == "working" and (
        row["broker_order_id"] is None
        or row["broker_state"]
        not in WORKING_BROKER_STATES - {"partially_filled"}
    ):
        raise OrderIntentError("stored working status conflicts with broker state")
    if row["status"] == "partially_filled" and row["broker_state"] != "partially_filled":
        raise OrderIntentError("stored partial status conflicts with broker state")
    if row["broker_state"] in TERMINAL_BROKER_STATES and row["status"] != "resolved":
        raise OrderIntentError("stored terminal broker state is not resolved")
    if row["status"] == "resolved":
        if outcome is None:
            raise OrderIntentError("stored resolved intent has no outcome")
        if row["broker_order_id"] is None:
            if (
                row["broker_state"] is not None
                or cumulative != 0
                or outcome != "operator_confirmed_not_submitted"
            ):
                raise OrderIntentError(
                    "stored no-broker resolution is internally inconsistent"
                )
        elif row["broker_state"] not in TERMINAL_BROKER_STATES:
            if not (
                order["type"] == "stop_market"
                and row["broker_state"] in {"confirmed", "queued"}
                and cumulative == 0
                and outcome == "active_stop"
            ):
                raise OrderIntentError(
                    "stored nonterminal resolution is not an active stop"
                )
    if row["status"] == "prepared" and outcome is not None:
        raise OrderIntentError("stored prepared intent has an outcome")
    if row["status"] == "abandoned" and outcome != "never_submitted":
        raise OrderIntentError("stored abandoned intent has an invalid outcome")
    if row["status"] == "acknowledged" and (
        row["broker_state"] is not None
        or cumulative != 0
        or outcome != "operator_bound_pending_observation"
    ):
        raise OrderIntentError(
            "stored acknowledged intent is internally inconsistent"
        )
    if row["status"] in {"working", "partially_filled"} and outcome is not None:
        raise OrderIntentError("stored active intent has a terminal outcome")
    created = _utc(row["created_at"], "stored created_at")
    updated = _utc(row["updated_at"], "stored updated_at")
    if created < run_start_instant:
        raise OrderIntentError("stored intent predates its run start")
    if updated < created:
        raise OrderIntentError("stored updated_at predates created_at")
    if first_submit is not None and first_submit < created:
        raise OrderIntentError("stored first submission predates preparation")
    if last_submit is not None and last_submit > updated:
        raise OrderIntentError("stored last submission is later than updated_at")


def validate_persisted_relationships(
    connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
) -> None:
    by_id = {row["intent_id"]: row for row in rows}
    ambiguous_stops: set[tuple[str, str]] = set()
    equivalent_unresolved: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        order = json.loads(row["order_json"])
        parent_id = row["replaces_intent_id"]
        if parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None:
                raise OrderIntentError("stored stop retry has no parent intent")
            parent_order = json.loads(parent["order_json"])
            if (
                row["purpose"] != "stop-retry"
                or parent["purpose"] == "stop-retry"
                or parent["account_name"] != row["account_name"]
                or parent_order["symbol"] != order["symbol"]
                or parent_order["type"] != "stop_market"
                or order["type"] != "stop_market"
                or parent["status"] != "resolved"
                or parent["broker_state"]
                not in {"cancelled", "rejected", "failed", "voided"}
                or Decimal(parent["cumulative_quantity"]) != 0
            ):
                raise OrderIntentError(
                    "stored stop retry has an invalid replacement relationship"
                )

        if row["status"] in {"resolved", "abandoned"}:
            continue
        equivalent_key = (
            row["account_name"],
            row["purpose"],
            order["symbol"],
            order["side"],
            order["type"],
        )
        if equivalent_key in equivalent_unresolved:
            raise OrderIntentError("equivalent unresolved intents overlap")
        equivalent_unresolved.add(equivalent_key)
        if order["type"] == "stop_market" and row["status"] in {
            "prepared",
            "submitting",
            "unknown",
            "indeterminate",
            "working",
        }:
            stop_key = (row["account_name"], order["symbol"])
            if stop_key in ambiguous_stops:
                raise OrderIntentError("ambiguous unresolved stop intents overlap")
            ambiguous_stops.add(stop_key)


def validate_persisted_state(
    connection: sqlite3.Connection,
) -> list[sqlite3.Row]:
    rows = connection.execute("SELECT * FROM intents").fetchall()
    for row in rows:
        validate_persisted_row(row)
    validate_persisted_relationships(connection, rows)
    return rows


def validate_persisted_events(
    connection: sqlite3.Connection, known_intent_ids: set[str]
) -> int:
    rows = connection.execute(
        "SELECT sequence, intent_id, occurred_at, event, detail_json "
        "FROM intent_events ORDER BY sequence"
    ).fetchall()
    for row in rows:
        sequence = row["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
            raise OrderIntentError("stored event sequence is invalid")
        intent_id = _uuid(row["intent_id"], "stored event intent_id")
        if intent_id not in known_intent_ids:
            raise OrderIntentError("stored event references an unknown intent")
        _utc(row["occurred_at"], "stored event occurred_at")
        _text(row["event"], "stored event name", maximum=80)
        try:
            detail = json.loads(
                row["detail_json"],
                object_pairs_hook=_object_no_duplicates,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, OrderIntentError) as exc:
            raise OrderIntentError("stored event detail is malformed") from exc
        detail = _mapping(detail, "stored event detail")
        if row["detail_json"] != _canonical(detail):
            raise OrderIntentError("stored event detail is not canonical")
    return len(rows)


def _event(
    connection: sqlite3.Connection,
    intent_id: str,
    occurred_at: str,
    event: str,
    detail: Mapping[str, Any],
) -> None:
    connection.execute(
        "INSERT INTO intent_events(intent_id, occurred_at, event, detail_json) "
        "VALUES (?, ?, ?, ?)",
        (intent_id, occurred_at, event, _canonical(detail)),
    )


def _stored_order(row: sqlite3.Row) -> dict[str, Any]:
    order = json.loads(
        row["order_json"], object_pairs_hook=_object_no_duplicates,
        parse_constant=_reject_constant,
    )
    order["ref_id"] = row["ref_id"]
    return order


def _public_intent(row: sqlite3.Row, current_run_token: str | None = None) -> dict[str, Any]:
    order = json.loads(row["order_json"])
    retry_available = (
        row["status"] == "unknown"
        and row["submit_attempts"] == 1
        and current_run_token is not None
        and row["run_token"] == current_run_token
        and row["last_error_code"] != "unverified_rejection"
        and row["last_observed_at"] is not None
        and row["last_submit_at"] is not None
        and _utc(row["last_observed_at"], "stored last_observed_at")
        >= _utc(row["last_submit_at"], "stored last_submit_at")
    )
    return {
        "intent_id": row["intent_id"],
        "ref_id": row["ref_id"],
        "account_name": row["account_name"],
        "purpose": row["purpose"],
        "symbol": order["symbol"],
        "side": order["side"],
        "type": order["type"],
        "status": row["status"],
        "submit_attempts": row["submit_attempts"],
        "broker_order_id": row["broker_order_id"],
        "broker_state": row["broker_state"],
        "cumulative_quantity": row["cumulative_quantity"],
        "average_fill_price": row["average_fill_price"],
        "last_execution_at": row["last_execution_at"],
        "replaces_intent_id": row["replaces_intent_id"],
        "last_error_code": row["last_error_code"],
        "last_observed_at": row["last_observed_at"],
        "same_run_retry_available": retry_available,
        "updated_at": row["updated_at"],
    }


def prepare(state_file: str, intent_file: str, now: str) -> dict[str, Any]:
    document = validate_intent_document(load_json(intent_file, "intent file"))
    now_instant = _utc(now, "current UTC time")
    baseline_instant = _utc(
        document["baseline"]["observed_at_utc"],
        "intent.baseline.observed_at_utc",
    )
    if baseline_instant > now_instant + timedelta(seconds=BROKER_CLOCK_SKEW_SECONDS):
        raise OrderIntentError("intent baseline is later than preparation time")
    if now_instant - baseline_instant > timedelta(
        seconds=MAX_BASELINE_AGE_SECONDS
    ):
        raise OrderIntentError("intent baseline is stale at preparation time")
    order_json = _canonical(document["order"])
    baseline_json = _canonical(document["baseline"])
    immutable_json = _canonical(immutable_intent_document(document))
    intent_id = str(uuid.uuid4())

    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing_rows = validate_persisted_state(connection)
        unresolved = [
            row
            for row in existing_rows
            if row["status"] not in {"resolved", "abandoned"}
        ]
        if document["purpose"] == "stop-retry":
            parent = _row(connection, document["replaces_intent_id"])
            parent_order = json.loads(parent["order_json"])
            if (
                parent["account_name"] != document["account_name"]
                or parent_order["symbol"] != document["order"]["symbol"]
                or parent_order["type"] != "stop_market"
                or parent["purpose"] == "stop-retry"
                or parent["status"] != "resolved"
                or parent["broker_state"]
                not in {"cancelled", "rejected", "failed", "voided"}
                or Decimal(parent["cumulative_quantity"]) != 0
            ):
                raise OrderIntentError(
                    "stop-retry requires a matching terminal zero-fill stop parent"
                )
            prior_child = connection.execute(
                "SELECT intent_id FROM intents WHERE replaces_intent_id = ?",
                (parent["intent_id"],),
            ).fetchone()
            if prior_child is not None:
                raise OrderIntentError("the stop parent already has a retry intent")
        if document["purpose"] == "dip-buy" and unresolved:
            raise OrderIntentError(
                "cannot prepare new exposure while an earlier intent is unresolved"
            )
        for existing in unresolved:
            existing_order = json.loads(existing["order_json"])
            if (
                existing["account_name"] == document["account_name"]
                and existing_order["symbol"] == document["order"]["symbol"]
                and existing_order["type"] == "stop_market"
                and document["order"]["type"] == "stop_market"
                and existing["status"]
                in {
                    "prepared",
                    "submitting",
                    "unknown",
                    "indeterminate",
                    "working",
                }
            ):
                raise OrderIntentError(
                    "an unresolved ambiguous stop intent already exists"
                )
            if (
                existing["account_name"] == document["account_name"]
                and existing["purpose"] == document["purpose"]
                and existing_order["symbol"] == document["order"]["symbol"]
                and existing_order["side"] == document["order"]["side"]
                and existing_order["type"] == document["order"]["type"]
            ):
                raise OrderIntentError(
                    "an equivalent unresolved intent already exists"
                )

        connection.execute(
            """
            INSERT INTO intents(
                intent_id, ref_id, account_name, run_token, run_start_utc,
                rules_version, constants_sha256, purpose, replaces_intent_id,
                order_json, order_sha256, baseline_json, baseline_sha256,
                intent_sha256, status, submit_attempts,
                cumulative_quantity, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', 0, '0', ?, ?)
            """,
            (
                intent_id,
                intent_id,
                document["account_name"],
                document["run_token"],
                document["run_start_utc"],
                document["rules_version"],
                document["constants_sha256"],
                document["purpose"],
                document["replaces_intent_id"],
                order_json,
                _sha256(order_json),
                baseline_json,
                _sha256(baseline_json),
                _sha256(immutable_json),
                now,
                now,
            ),
        )
        _event(connection, intent_id, now, "prepared", {
            "order_sha256": _sha256(order_json),
            "intent_sha256": _sha256(immutable_json),
            "baseline": document["baseline"],
            "replaces_intent_id": document["replaces_intent_id"],
        })
        validate_persisted_state(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    order = dict(document["order"])
    order["ref_id"] = intent_id
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "prepare",
        "ok": True,
        "intent_id": intent_id,
        "ref_id": intent_id,
        "status": "prepared",
        "order_sha256": _sha256(order_json),
        "baseline_sha256": _sha256(baseline_json),
        "intent_sha256": _sha256(immutable_json),
        "replaces_intent_id": document["replaces_intent_id"],
        "place_order": order,
    }


def begin_or_retry(
    state_file: str,
    intent_id: str,
    run_token: str,
    now: str,
    *,
    retrying: bool,
) -> dict[str, Any]:
    run_token = _uuid(run_token, "run_token")
    action = "retry" if retrying else "begin"
    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_persisted_state(connection)
        row = _row(connection, intent_id)
        if row["run_token"] != run_token:
            raise OrderIntentError(
                "cross-run replay is forbidden; reconcile the stale intent"
            )
        if retrying:
            if row["status"] != "unknown" or row["submit_attempts"] != 1:
                raise OrderIntentError(
                    "retry requires one same-run ambiguous submission"
                )
            if row["last_error_code"] == "unverified_rejection":
                raise OrderIntentError(
                    "an explicit rejection cannot use automatic retry"
                )
            if (
                row["last_observed_at"] is None
                or row["last_submit_at"] is None
                or _utc(row["last_observed_at"], "stored last_observed_at")
                < _utc(row["last_submit_at"], "stored last_submit_at")
            ):
                raise OrderIntentError(
                    "retry requires a post-failure no-match reconciliation"
                )
            attempts = 2
            first_submit_at = row["first_submit_at"]
        else:
            if row["status"] != "prepared" or row["submit_attempts"] != 0:
                raise OrderIntentError("begin requires an unsubmitted prepared intent")
            attempts = 1
            first_submit_at = now
        connection.execute(
            "UPDATE intents SET status = 'submitting', submit_attempts = ?, "
            "first_submit_at = ?, last_submit_at = ?, updated_at = ? "
            "WHERE intent_id = ?",
            (attempts, first_submit_at, now, now, row["intent_id"]),
        )
        _event(connection, row["intent_id"], now, action, {
            "attempt": attempts,
            "order_sha256": row["order_sha256"],
        })
        validate_persisted_state(connection)
        connection.commit()
        place_order = _stored_order(row)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "intent_id": intent_id,
        "ref_id": intent_id,
        "status": "submitting",
        "attempt": attempts,
        "order_sha256": row["order_sha256"],
        "baseline_sha256": row["baseline_sha256"],
        "intent_sha256": row["intent_sha256"],
        "place_order": place_order,
    }


def mark_unknown(
    state_file: str,
    intent_id: str,
    code: str,
    detail: str | None,
    now: str,
) -> dict[str, Any]:
    allowed_codes = {
        "transport_error",
        "timeout",
        "malformed_response",
        "acknowledgement_failure",
        "unverified_rejection",
    }
    if code not in allowed_codes:
        raise OrderIntentError("unknown failure code")
    if detail is not None:
        detail = _text(detail, "detail", maximum=500)
    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_persisted_state(connection)
        row = _row(connection, intent_id)
        if row["status"] == "unknown":
            connection.commit()
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "mark-unknown",
                "ok": True,
                "intent_id": row["intent_id"],
                "status": "unknown",
                "submit_attempts": row["submit_attempts"],
                "blocking": True,
            }
        if row["status"] != "submitting":
            raise OrderIntentError("only a submitting intent can become unknown")
        connection.execute(
            "UPDATE intents SET status = 'unknown', last_error_code = ?, "
            "updated_at = ? WHERE intent_id = ?",
            (code, now, row["intent_id"]),
        )
        _event(connection, row["intent_id"], now, "unknown", {
            "code": code, "detail": detail,
        })
        validate_persisted_state(connection)
        connection.commit()
        attempts = row["submit_attempts"]
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "mark-unknown",
        "ok": True,
        "intent_id": intent_id,
        "status": "unknown",
        "submit_attempts": attempts,
        "blocking": True,
    }


def _extract_place_order(response: Any) -> Mapping[str, Any]:
    root = _mapping(response, "place response")
    if "data" in root:
        data = _mapping(root["data"], "place response.data")
    elif "structuredContent" in root:
        structured = _mapping(
            root["structuredContent"], "place response.structuredContent"
        )
        data = _mapping(structured.get("data"), "place response.data")
    else:
        raise OrderIntentError("place response: missing data object")
    order = data.get("order")
    if order is None:
        raise OrderIntentError("place response.data.order: missing or null")
    return _mapping(order, "place response.data.order")


def _normalized_broker_type(order: Mapping[str, Any], context: str) -> str:
    order_type = order.get("type")
    trigger = order.get("trigger")
    if order_type in {"stop_market", "stop_limit"}:
        if trigger not in {None, "stop"}:
            raise OrderIntentError(f"{context}.trigger: contradicts stop type")
        return order_type
    if order_type not in {"market", "limit"}:
        raise OrderIntentError(f"{context}.type: unsupported broker type")
    if trigger == "stop":
        return "stop_market" if order_type == "market" else "stop_limit"
    if trigger in {None, "immediate"}:
        return order_type
    raise OrderIntentError(f"{context}.trigger: unsupported broker trigger")


def _broker_decimal(
    value: Any, context: str, *, positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    if not isinstance(value, str):
        raise OrderIntentError(f"{context}: expected a decimal string")
    # Broker strings may contain leading or trailing zeroes; prohibit exponent/NaN.
    if not re.fullmatch(r"(?:0|[1-9]\d*|0\d+)(?:\.\d+)?", value):
        raise OrderIntentError(f"{context}: malformed decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise OrderIntentError(f"{context}: invalid decimal") from exc
    if not parsed.is_finite():
        raise OrderIntentError(f"{context}: decimal must be finite")
    if positive and parsed <= 0:
        raise OrderIntentError(f"{context}: must be positive")
    if nonnegative and parsed < 0:
        raise OrderIntentError(f"{context}: must be nonnegative")
    return parsed


def validate_broker_order(
    raw: Any,
    expected: Mapping[str, Any],
    context: str = "broker order",
    *,
    not_before: datetime | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    order = _mapping(raw, context)
    order_id = _uuid(order.get("id"), f"{context}.id")
    if order.get("symbol") != expected["symbol"]:
        raise OrderIntentError(f"{context}.symbol: does not match intent")
    if order.get("side") != expected["side"]:
        raise OrderIntentError(f"{context}.side: does not match intent")
    normalized_type = _normalized_broker_type(order, context)
    if normalized_type != expected["type"]:
        raise OrderIntentError(f"{context}.type: does not match intent")
    if order.get("market_hours") != expected["market_hours"]:
        raise OrderIntentError(f"{context}.market_hours: does not match intent")
    if order.get("time_in_force") != expected["time_in_force"]:
        raise OrderIntentError(f"{context}.time_in_force: does not match intent")
    if order.get("placed_agent") != "agentic":
        raise OrderIntentError(f"{context}.placed_agent: expected agentic")

    returned_quantity: Decimal | None = None
    if "quantity" in expected:
        returned_quantity = _broker_decimal(
            order.get("quantity"), f"{context}.quantity", positive=True
        )
        if returned_quantity != Decimal(expected["quantity"]):
            raise OrderIntentError(f"{context}.quantity: does not match intent")
    else:
        dollars = _mapping(
            order.get("dollar_based_amount"),
            f"{context}.dollar_based_amount",
        )
        amount = _broker_decimal(
            dollars.get("amount"),
            f"{context}.dollar_based_amount.amount",
            positive=True,
        )
        if amount != Decimal(expected["dollar_amount"]):
            raise OrderIntentError(
                f"{context}.dollar_based_amount.amount: does not match intent"
            )

    if "limit_price" in expected:
        if _broker_decimal(
            order.get("price"), f"{context}.price", positive=True
        ) != Decimal(expected["limit_price"]):
            raise OrderIntentError(f"{context}.price: does not match intent")
    if "stop_price" in expected:
        if _broker_decimal(
            order.get("stop_price"), f"{context}.stop_price", positive=True
        ) != Decimal(expected["stop_price"]):
            raise OrderIntentError(f"{context}.stop_price: does not match intent")

    state = order.get("state")
    if state not in BROKER_STATES:
        raise OrderIntentError(f"{context}.state: unrecognized state")
    cumulative = _broker_decimal(
        order.get("cumulative_quantity"),
        f"{context}.cumulative_quantity",
        nonnegative=True,
    )
    if returned_quantity is not None and cumulative > returned_quantity:
        raise OrderIntentError(
            f"{context}.cumulative_quantity: exceeds requested quantity"
        )

    executions_raw = order.get("executions")
    if executions_raw is None:
        executions_raw = []
    if not isinstance(executions_raw, list):
        raise OrderIntentError(f"{context}.executions: expected an array or null")
    executions: dict[str, tuple[str, str, str, str]] = {}
    execution_sum = Decimal(0)
    execution_notional = Decimal(0)
    latest_execution: tuple[datetime, Decimal, str] | None = None
    for index, item in enumerate(executions_raw):
        execution = _mapping(item, f"{context}.executions[{index}]")
        execution_id = _uuid(
            execution.get("id"), f"{context}.executions[{index}].id"
        )
        quantity_text = execution.get("quantity")
        quantity = _broker_decimal(
            quantity_text,
            f"{context}.executions[{index}].quantity",
            positive=True,
        )
        price_text = execution.get("price")
        price = _broker_decimal(
            price_text, f"{context}.executions[{index}].price", positive=True
        )
        fees_text = execution.get("fees")
        _broker_decimal(
            fees_text, f"{context}.executions[{index}].fees", nonnegative=True
        )
        timestamp_key = _utc_sort_key(
            execution.get("timestamp"), f"{context}.executions[{index}].timestamp"
        )
        timestamp = timestamp_key[2]
        if as_of is not None and timestamp_key[0] > as_of + timedelta(
            seconds=BROKER_CLOCK_SKEW_SECONDS
        ):
            raise OrderIntentError(
                f"{context}.executions[{index}].timestamp: later than observation"
            )
        fingerprint = (quantity_text, price_text, fees_text, timestamp)
        prior = executions.get(execution_id)
        if prior is not None:
            if prior != fingerprint:
                raise OrderIntentError(
                    f"{context}.executions: conflicting duplicate execution ID"
                )
            continue
        executions[execution_id] = fingerprint
        execution_sum += quantity
        execution_notional += quantity * price
        if latest_execution is None or timestamp_key > latest_execution:
            latest_execution = timestamp_key
    if execution_sum != cumulative:
        raise OrderIntentError(
            f"{context}.executions: quantity sum does not equal cumulative_quantity"
        )

    created_at = _text(order.get("created_at"), f"{context}.created_at", maximum=50)
    created_instant = _utc(created_at, f"{context}.created_at")
    if not_before is not None and created_instant < not_before:
        raise OrderIntentError(f"{context}.created_at: predates submission window")
    if as_of is not None and created_instant > as_of + timedelta(
        seconds=BROKER_CLOCK_SKEW_SECONDS
    ):
        raise OrderIntentError(f"{context}.created_at: later than observation")
    if latest_execution is not None and latest_execution[0] < created_instant - timedelta(
        seconds=BROKER_CLOCK_SKEW_SECONDS
    ):
        raise OrderIntentError(f"{context}.executions: fill predates order creation")

    if state == "filled":
        if cumulative <= 0:
            raise OrderIntentError(f"{context}: filled state has zero quantity")
        if returned_quantity is not None and cumulative != returned_quantity:
            raise OrderIntentError(
                f"{context}: filled state does not equal requested quantity"
            )
    if state in {"partially_filled", "partially_filled_rest_cancelled"}:
        if cumulative <= 0:
            raise OrderIntentError(f"{context}: partial state has zero quantity")
        if returned_quantity is not None and cumulative >= returned_quantity:
            raise OrderIntentError(
                f"{context}: partial state is not below requested quantity"
            )
    if (
        state == "pending_cancelled"
        and returned_quantity is not None
        and cumulative >= returned_quantity
    ):
        raise OrderIntentError(
            f"{context}: pending cancellation has no unfilled remainder"
        )
    if state in {"new", "queued", "confirmed", "unconfirmed"} and cumulative != 0:
        raise OrderIntentError(
            f"{context}: unfilled working state carries executed quantity"
        )

    average_fill_price: Decimal | None = None
    last_execution_at: str | None = None
    if cumulative > 0:
        average_fill_price = execution_notional / cumulative
        last_execution_at = latest_execution[2] if latest_execution is not None else None
        assert last_execution_at is not None
        broker_average_raw = order.get("average_price")
        if broker_average_raw is not None:
            broker_average = _broker_decimal(
                broker_average_raw, f"{context}.average_price", positive=True
            )
            if abs(broker_average - average_fill_price) > Decimal("0.000001"):
                raise OrderIntentError(
                    f"{context}.average_price: conflicts with executions"
                )
    return {
        "id": order_id,
        "state": state,
        "cumulative_quantity": cumulative,
        "requested_quantity": returned_quantity,
        "created_at": created_at,
        "normalized_type": normalized_type,
        "average_fill_price": average_fill_price,
        "last_execution_at": last_execution_at,
        "raw": dict(order),
    }


def _lifecycle(expected: Mapping[str, Any], broker: Mapping[str, Any]) -> dict[str, Any]:
    state = broker["state"]
    cumulative: Decimal = broker["cumulative_quantity"]
    requested: Decimal | None = broker["requested_quantity"]
    is_stop = expected["type"] == "stop_market"
    if state in UNSUPPORTED_LONG_ONLY_STATES:
        status = "indeterminate"
        outcome = "unsupported_long_only_state"
    elif state in TERMINAL_BROKER_STATES:
        status = "resolved"
        outcome = state
    elif is_stop and state in {"confirmed", "queued"} and cumulative == 0:
        status = "resolved"
        outcome = "active_stop"
    elif state == "partially_filled":
        status = "partially_filled"
        outcome = None
    else:
        status = "working"
        outcome = None
    remaining = None if requested is None else requested - cumulative
    whole_filled = cumulative.to_integral_value(rounding=ROUND_FLOOR)
    return {
        "status": status,
        "outcome": outcome,
        "terminal": state in TERMINAL_BROKER_STATES,
        "blocking": status not in {"resolved", "abandoned"},
        "filled_quantity": format(cumulative, "f"),
        "whole_filled_quantity": format(whole_filled, "f"),
        "average_fill_price": (
            None
            if broker["average_fill_price"] is None
            else format(broker["average_fill_price"], "f")
        ),
        "last_execution_at": broker["last_execution_at"],
        "remaining_quantity": None if remaining is None else format(remaining, "f"),
        "requires_stop_audit": cumulative > 0,
        "cancel_unfilled_remainder": (
            not is_stop
            and state in {"new", "queued", "confirmed", "unconfirmed", "partially_filled"}
        ),
        "ledger_ready": state in TERMINAL_BROKER_STATES and cumulative > 0,
        "stop_coverage_quantity": (
            format(remaining, "f")
            if is_stop
            and remaining is not None
            and state in {"confirmed", "queued", "partially_filled"}
            else "0"
        ),
        "stop_coverage_indeterminate": is_stop and state == "pending_cancelled",
    }


def _save_broker_observation(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    broker: Mapping[str, Any],
    now: str,
    as_of_text: str,
    event_name: str,
) -> dict[str, Any]:
    expected = json.loads(row["order_json"])
    lifecycle = _lifecycle(expected, broker)
    prior_cumulative = Decimal(row["cumulative_quantity"])
    if broker["cumulative_quantity"] < prior_cumulative:
        raise OrderIntentError(
            "broker observation would reduce the persisted cumulative fill"
        )
    prior_state = row["broker_state"]
    if (
        prior_state in TERMINAL_BROKER_STATES
        and (
            broker["state"] != prior_state
            or broker["cumulative_quantity"] != prior_cumulative
        )
    ):
        raise OrderIntentError(
            "broker observation would change an already terminal order"
        )
    if prior_state in TERMINAL_BROKER_STATES:
        prior_average = (
            None
            if row["average_fill_price"] is None
            else Decimal(row["average_fill_price"])
        )
        if broker["average_fill_price"] != prior_average:
            raise OrderIntentError(
                "broker observation would change a terminal order's average fill"
            )
        prior_last_execution = row["last_execution_at"]
        if (
            broker["last_execution_at"] is None
            or prior_last_execution is None
        ):
            if broker["last_execution_at"] != prior_last_execution:
                raise OrderIntentError(
                    "broker observation would change a terminal order's fill time"
                )
        elif _utc_sort_key(
            broker["last_execution_at"], "broker last execution"
        )[:2] != _utc_sort_key(
            prior_last_execution, "stored last_execution_at"
        )[:2]:
            raise OrderIntentError(
                "broker observation would change a terminal order's fill time"
            )
    as_of = _utc(as_of_text, "broker observation as-of")
    if row["last_observed_at"] is not None and as_of < _utc(
        row["last_observed_at"], "stored last_observed_at"
    ):
        raise OrderIntentError("broker observation as-of regressed")
    conflict = connection.execute(
        "SELECT intent_id FROM intents WHERE broker_order_id = ? "
        "AND intent_id != ?",
        (broker["id"], row["intent_id"]),
    ).fetchone()
    if conflict is not None:
        raise OrderIntentError("broker order ID is already bound to another intent")
    connection.execute(
        "UPDATE intents SET status = ?, broker_order_id = ?, broker_state = ?, "
        "cumulative_quantity = ?, average_fill_price = ?, last_execution_at = ?, "
        "last_observed_at = ?, outcome = ?, updated_at = ? "
        "WHERE intent_id = ?",
        (
            lifecycle["status"],
            broker["id"],
            broker["state"],
            lifecycle["filled_quantity"],
            lifecycle["average_fill_price"],
            lifecycle["last_execution_at"],
            as_of_text,
            lifecycle["outcome"],
            now,
            row["intent_id"],
        ),
    )
    _event(connection, row["intent_id"], now, event_name, {
        "broker_order_id": broker["id"],
        "broker_state": broker["state"],
        "cumulative_quantity": lifecycle["filled_quantity"],
        "average_fill_price": lifecycle["average_fill_price"],
        "last_execution_at": lifecycle["last_execution_at"],
        "as_of_utc": as_of_text,
        "status": lifecycle["status"],
    })
    updated = connection.execute(
        "SELECT * FROM intents WHERE intent_id = ?", (row["intent_id"],)
    ).fetchone()
    assert updated is not None
    validate_persisted_state(connection)
    return lifecycle


def acknowledge(
    state_file: str,
    intent_id: str,
    response_file: str,
    transport_scratch: str,
    now: str,
) -> dict[str, Any]:
    _resolved_response, response, _raw_response = (
        validate_bound_external_json_source(transport_scratch, response_file)
    )
    raw_order = _extract_place_order(response)
    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_persisted_state(connection)
        row = _row(connection, intent_id)
        if row["status"] not in {
            "submitting", "unknown", "acknowledged", "working",
            "partially_filled", "resolved",
        }:
            raise OrderIntentError("intent is not awaiting a broker response")
        if row["status"] == "resolved" and row["broker_order_id"] is None:
            raise OrderIntentError("a resolved no-broker intent is immutable")
        expected = json.loads(row["order_json"])
        not_before = _utc(
            row["first_submit_at"], "stored first_submit_at"
        ) - timedelta(seconds=BROKER_CLOCK_SKEW_SECONDS)
        broker = validate_broker_order(
            raw_order,
            expected,
            "place response order",
            not_before=not_before,
            as_of=_utc(now, "acknowledgement time"),
        )
        if row["broker_order_id"] not in {None, broker["id"]}:
            raise OrderIntentError("idempotent retry returned a different broker order ID")
        lifecycle = _save_broker_observation(
            connection, row, broker, now, now, "acknowledged"
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "acknowledge",
        "ok": True,
        "intent_id": intent_id,
        "broker_order_id": broker["id"],
        "broker_state": broker["state"],
        **lifecycle,
    }


def _pages(
    paths: Sequence[str],
    request_cursors: Sequence[str] | None,
    row_key: str,
    context: str,
) -> list[Mapping[str, Any]]:
    if not paths:
        raise OrderIntentError(f"{context}: at least one page is required")
    if request_cursors is None:
        if len(paths) != 1:
            raise OrderIntentError(
                f"{context}: multi-page input requires request cursor linkage"
            )
        request_cursors = ["FIRST"]
    if len(request_cursors) != len(paths) or request_cursors[0] != "FIRST":
        raise OrderIntentError(
            f"{context}: request cursors must align with pages and start with FIRST"
        )
    rows: list[Mapping[str, Any]] = []
    seen_cursors: set[str] = set()
    expected_request_cursor = "FIRST"
    for page_index, path in enumerate(paths):
        request_cursor = request_cursors[page_index]
        if request_cursor != expected_request_cursor:
            raise OrderIntentError(
                f"{context} page {page_index + 1}: request cursor breaks the chain"
            )
        if request_cursor != "FIRST":
            request_cursor = _text(
                request_cursor,
                f"{context} page {page_index + 1} request cursor",
                maximum=500,
            )
            if request_cursor in seen_cursors:
                raise OrderIntentError(f"{context}: repeated request cursor")
            seen_cursors.add(request_cursor)
        root = _mapping(load_json(path, f"{context} page"), f"{context} page")
        if "data" in root:
            data = _mapping(root.get("data"), f"{context} page.data")
        elif "structuredContent" in root:
            structured = _mapping(
                root.get("structuredContent"),
                f"{context} page.structuredContent",
            )
            data = _mapping(
                structured.get("data"), f"{context} page.structuredContent.data"
            )
        else:
            raise OrderIntentError(f"{context} page: missing data object")
        page_rows = data.get(row_key)
        if not isinstance(page_rows, list):
            raise OrderIntentError(
                f"{context} page.data.{row_key}: expected an array"
            )
        for item_index, item in enumerate(page_rows):
            if item is None:
                raise OrderIntentError(
                    f"{context} page {page_index + 1} row {item_index + 1}: null"
                )
            rows.append(_mapping(item, f"{context} row"))
        next_value = data.get("next")
        has_next = isinstance(next_value, str) and bool(next_value.strip())
        next_cursor: str | None = None
        if has_next:
            parsed = urlsplit(next_value.strip())
            cursor_values = parse_qs(parsed.query).get("cursor", [])
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or len(cursor_values) != 1
                or not cursor_values[0]
            ):
                raise OrderIntentError(
                    f"{context} page {page_index + 1}: next is not a "
                    "cursor-bearing URL"
                )
            next_cursor = cursor_values[0]
            if next_cursor in seen_cursors:
                raise OrderIntentError(f"{context}: repeated next cursor")
        if page_index < len(paths) - 1 and not has_next:
            raise OrderIntentError(f"{context}: nonfinal page has no next cursor")
        if page_index == len(paths) - 1 and has_next:
            raise OrderIntentError(f"{context}: final page still has a next cursor")
        if not has_next and next_value is not None and not isinstance(next_value, str):
            raise OrderIntentError(
                f"{context} page {page_index + 1}: next must be a URL string or null"
            )
        expected_request_cursor = next_cursor or ""
    return rows


def _dedupe_orders(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    by_id: dict[str, Mapping[str, Any]] = {}
    for index, order in enumerate(rows):
        order_id = _uuid(order.get("id"), f"orders[{index}].id")
        prior = by_id.get(order_id)
        if prior is not None and prior != order:
            raise OrderIntentError("orders: conflicting duplicate order ID")
        by_id[order_id] = order
    return list(by_id.values())


def _position_quantity(
    rows: Sequence[Mapping[str, Any]], symbol: str
) -> Decimal:
    by_symbol: dict[str, Decimal] = {}
    for index, position in enumerate(rows):
        position_symbol = _text(
            position.get("symbol"), f"positions[{index}].symbol", maximum=15
        )
        quantity = _broker_decimal(
            position.get("quantity"),
            f"positions[{index}].quantity",
            nonnegative=True,
        )
        if position_symbol in by_symbol:
            raise OrderIntentError("positions: duplicate symbol")
        by_symbol[position_symbol] = quantity
    return by_symbol.get(symbol, Decimal(0))


def _identity_match(
    candidate: Mapping[str, Any],
    expected: Mapping[str, Any],
    baseline: Mapping[str, Any],
    not_before: datetime,
    as_of: datetime,
) -> bool:
    if candidate.get("symbol") != expected["symbol"]:
        return False
    if candidate.get("side") != expected["side"]:
        return False
    if candidate.get("placed_agent") != "agentic":
        return False
    candidate_id = _uuid(candidate.get("id"), "candidate.id")
    if candidate_id in set(baseline["symbol_order_ids"]):
        return False
    if _normalized_broker_type(candidate, "candidate") != expected["type"]:
        return False
    market_hours = candidate.get("market_hours")
    if market_hours not in {"regular_hours", "extended_hours"}:
        raise OrderIntentError("candidate.market_hours: unsupported session")
    if market_hours != expected["market_hours"]:
        return False
    time_in_force = candidate.get("time_in_force")
    if time_in_force not in {"gfd", "gtc"}:
        raise OrderIntentError("candidate.time_in_force: unsupported value")
    if time_in_force != expected["time_in_force"]:
        return False
    created_at = _utc(candidate.get("created_at"), "candidate.created_at")
    if created_at < not_before:
        return False
    if created_at > as_of + timedelta(seconds=BROKER_CLOCK_SKEW_SECONDS):
        raise OrderIntentError("candidate.created_at: later than observation as-of")
    if "quantity" in expected:
        if _broker_decimal(
            candidate.get("quantity"), "candidate.quantity", positive=True
        ) != Decimal(expected["quantity"]):
            return False
    else:
        dollars = _mapping(
            candidate.get("dollar_based_amount"),
            "candidate.dollar_based_amount",
        )
        if _broker_decimal(
            dollars.get("amount"),
            "candidate.dollar_based_amount.amount",
            positive=True,
        ) != Decimal(expected["dollar_amount"]):
            return False
    if "limit_price" in expected and _broker_decimal(
        candidate.get("price"), "candidate.price", positive=True
    ) != Decimal(expected["limit_price"]):
        return False
    if "stop_price" in expected and _broker_decimal(
        candidate.get("stop_price"), "candidate.stop_price", positive=True
    ) != Decimal(expected["stop_price"]):
        return False
    return True


def observe(
    state_file: str,
    intent_id: str,
    order_files: Sequence[str],
    order_request_cursors: Sequence[str] | None,
    position_files: Sequence[str],
    position_request_cursors: Sequence[str] | None,
    as_of_text: str,
    now: str,
) -> dict[str, Any]:
    as_of = _utc(as_of_text, "--as-of-utc")
    orders = _dedupe_orders(
        _pages(order_files, order_request_cursors, "orders", "orders")
    )
    positions = _pages(
        position_files, position_request_cursors, "positions", "positions"
    )

    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_persisted_state(connection)
        row = _row(connection, intent_id)
        if row["status"] in {"prepared", "abandoned"}:
            raise OrderIntentError("a never-submitted intent cannot be observed")
        if row["status"] == "resolved" and row["broker_order_id"] is None:
            raise OrderIntentError("a resolved no-broker intent is immutable")
        expected = json.loads(row["order_json"])
        baseline = json.loads(row["baseline_json"])
        current_position = _position_quantity(positions, expected["symbol"])
        baseline_position = Decimal(baseline["position_quantity"])
        if as_of < _utc(baseline["observed_at_utc"], "baseline.observed_at_utc"):
            raise OrderIntentError("--as-of-utc predates the intent baseline")
        if row["last_observed_at"] is not None and as_of < _utc(
            row["last_observed_at"], "stored last_observed_at"
        ):
            raise OrderIntentError("--as-of-utc regresses the prior observation")
        not_before = _utc(
            row["first_submit_at"], "stored first_submit_at"
        ) - timedelta(seconds=BROKER_CLOCK_SKEW_SECONDS)

        if row["broker_order_id"] is not None:
            matches = [
                order for order in orders
                if order.get("id") == row["broker_order_id"]
            ]
        else:
            matches = [
                order for order in orders
                if _identity_match(
                    order, expected, baseline, not_before, as_of
                )
            ]

        if len(matches) == 1:
            broker = validate_broker_order(
                matches[0], expected, not_before=not_before, as_of=as_of
            )
            lifecycle = _save_broker_observation(
                connection, row, broker, now, as_of_text, "observed"
            )
            match_reason = (
                "broker_order_id" if row["broker_order_id"] is not None
                else "unique_post_baseline_fingerprint"
            )
            connection.commit()
            result = {
                "matched": True,
                "match_reason": match_reason,
                "broker_order_id": broker["id"],
                "broker_state": broker["state"],
                **lifecycle,
            }
        elif len(matches) == 0:
            new_status = (
                "unknown"
                if row["broker_order_id"] is None
                and row["status"] != "indeterminate"
                else "indeterminate"
            )
            last_error_code = row["last_error_code"]
            if new_status == "unknown" and last_error_code is None:
                last_error_code = "recovery_no_match"
            connection.execute(
                "UPDATE intents SET status = ?, outcome = NULL, "
                "last_error_code = ?, last_observed_at = ?, updated_at = ? "
                "WHERE intent_id = ?",
                (
                    new_status,
                    last_error_code,
                    as_of_text,
                    now,
                    row["intent_id"],
                ),
            )
            _event(connection, row["intent_id"], now, "observation-unresolved", {
                "reason": "no_match",
                "position_quantity": format(current_position, "f"),
            })
            updated = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (row["intent_id"],)
            ).fetchone()
            assert updated is not None
            validate_persisted_state(connection)
            connection.commit()
            result = {
                "matched": False,
                "match_reason": "no_match",
                "status": new_status,
                "blocking": True,
                "requires_stop_audit": current_position != baseline_position,
            }
        else:
            connection.execute(
                "UPDATE intents SET status = 'indeterminate', "
                "outcome = NULL, last_observed_at = ?, updated_at = ? "
                "WHERE intent_id = ?",
                (as_of_text, now, row["intent_id"]),
            )
            candidate_ids = sorted(_uuid(item.get("id"), "candidate.id") for item in matches)
            _event(connection, row["intent_id"], now, "observation-unresolved", {
                "reason": "multiple_matches", "candidate_order_ids": candidate_ids,
            })
            updated = connection.execute(
                "SELECT * FROM intents WHERE intent_id = ?", (row["intent_id"],)
            ).fetchone()
            assert updated is not None
            validate_persisted_state(connection)
            connection.commit()
            result = {
                "matched": False,
                "match_reason": "multiple_matches",
                "candidate_order_ids": candidate_ids,
                "status": "indeterminate",
                "blocking": True,
                "requires_stop_audit": True,
            }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    result.update(
        {
            "schema_version": SCHEMA_VERSION,
            "action": "observe",
            "ok": True,
            "intent_id": intent_id,
            "position_quantity": format(current_position, "f"),
            "position_delta_from_baseline": format(
                current_position - baseline_position, "f"
            ),
        }
    )
    return result


def pending(
    state_file: str, current_run_token: str | None = None
) -> dict[str, Any]:
    if current_run_token is not None:
        current_run_token = _uuid(current_run_token, "run_token")
    connection = connect(state_file)
    try:
        all_rows = validate_persisted_state(connection)
        rows = sorted(
            (
                row
                for row in all_rows
                if row["status"] not in {"resolved", "abandoned"}
            ),
            key=lambda row: (row["created_at"], row["intent_id"]),
        )
        items = [_public_intent(row, current_run_token) for row in rows]
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "pending",
        "ok": True,
        "blocking": bool(items),
        "pending_count": len(items),
        "intents": items,
    }


def check(state_file: str) -> dict[str, Any]:
    connection = connect(state_file)
    try:
        integrity = [
            row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()
        ]
        if integrity != ["ok"]:
            raise OrderIntentError("journal database integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise OrderIntentError("journal database foreign-key check failed")
        rows = validate_persisted_state(connection)
        event_count = validate_persisted_events(
            connection, {row["intent_id"] for row in rows}
        )
        pending_count = sum(
            row["status"] not in {"resolved", "abandoned"} for row in rows
        )
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "check",
        "ok": True,
        "intent_count": len(rows),
        "event_count": event_count,
        "pending_count": pending_count,
        "blocking": pending_count > 0,
    }


def _operator_transition(
    state_file: str,
    intent_id: str,
    action: str,
    note: str,
    now: str,
    order_id: str | None = None,
) -> dict[str, Any]:
    note = _text(note, "note", maximum=500)
    connection = connect(state_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_persisted_state(connection)
        row = _row(connection, intent_id)
        if action == "abandon-prepared":
            if (
                row["status"] != "prepared"
                or row["submit_attempts"] != 0
                or row["broker_order_id"] is not None
            ):
                raise OrderIntentError(
                    "only a never-submitted prepared intent can be abandoned"
                )
            new_status = "abandoned"
            outcome = "never_submitted"
            broker_order_id = None
        elif action == "operator-bind":
            if row["status"] not in {"submitting", "unknown", "indeterminate"}:
                raise OrderIntentError("only an unresolved intent can be manually bound")
            broker_order_id = _uuid(order_id, "order_id")
            if row["broker_order_id"] not in {None, broker_order_id}:
                raise OrderIntentError(
                    "operator binding cannot replace a known broker order ID"
                )
            conflict = connection.execute(
                "SELECT intent_id FROM intents WHERE broker_order_id = ? "
                "AND intent_id != ?",
                (broker_order_id, row["intent_id"]),
            ).fetchone()
            if conflict is not None:
                raise OrderIntentError(
                    "broker order ID is already bound to another intent"
                )
            new_status = "acknowledged"
            outcome = "operator_bound_pending_observation"
        elif action == "operator-resolve-not-submitted":
            if row["status"] not in {"submitting", "unknown", "indeterminate"}:
                raise OrderIntentError("only an unresolved intent can be resolved")
            if row["broker_order_id"] is not None or Decimal(
                row["cumulative_quantity"]
            ) != 0:
                raise OrderIntentError(
                    "an intent with broker evidence cannot be marked not submitted"
                )
            broker_order_id = None
            new_status = "resolved"
            outcome = "operator_confirmed_not_submitted"
        else:
            raise OrderIntentError("unsupported operator transition")
        connection.execute(
            "UPDATE intents SET status = ?, outcome = ?, broker_order_id = ?, "
            "updated_at = ? WHERE intent_id = ?",
            (new_status, outcome, broker_order_id, now, row["intent_id"]),
        )
        _event(connection, row["intent_id"], now, action, {
            "note": note, "broker_order_id": broker_order_id,
        })
        validate_persisted_state(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "schema_version": SCHEMA_VERSION,
        "action": action,
        "ok": True,
        "intent_id": intent_id,
        "status": new_status,
        "outcome": outcome,
        "broker_order_id": broker_order_id,
        "blocking": new_status not in {"resolved", "abandoned"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "action",
        choices=(
            "check", "pending", "prepare", "begin", "retry",
            "acknowledge", "mark-unknown",
            "observe", "abandon-prepared",
            "operator-bind", "operator-resolve-not-submitted",
        ),
    )
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    parser.add_argument("--now-utc", help="test-only UTC clock override")
    parser.add_argument("--intent", help="strict prepared-intent JSON file")
    parser.add_argument("--intent-id")
    parser.add_argument("--run-token")
    parser.add_argument("--response", help="raw place_equity_order JSON response")
    parser.add_argument(
        "--transport-scratch",
        help="preflighted scratch whose binding owns --response",
    )
    parser.add_argument("--code")
    parser.add_argument("--detail")
    parser.add_argument("--orders", nargs="+")
    parser.add_argument("--order-request-cursors", nargs="+")
    parser.add_argument("--positions", nargs="+")
    parser.add_argument("--position-request-cursors", nargs="+")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--note")
    parser.add_argument("--order-id")
    args = parser.parse_args()
    action = args.action

    try:
        now, _ = _now(args.now_utc)
        if action == "check":
            result = check(args.state_file)
        elif action == "pending":
            result = pending(args.state_file, args.run_token)
        elif action == "prepare":
            if not args.intent:
                raise OrderIntentError("prepare requires --intent")
            result = prepare(args.state_file, args.intent, now)
        elif action in {"begin", "retry"}:
            if not args.intent_id or not args.run_token:
                raise OrderIntentError(
                    f"{action} requires --intent-id and --run-token"
                )
            result = begin_or_retry(
                args.state_file,
                args.intent_id,
                args.run_token,
                now,
                retrying=action == "retry",
            )
        elif action == "acknowledge":
            if (
                not args.intent_id
                or not args.response
                or not args.transport_scratch
            ):
                raise OrderIntentError(
                    "acknowledge requires --intent-id, --response, and "
                    "--transport-scratch"
                )
            result = acknowledge(
                args.state_file,
                args.intent_id,
                args.response,
                args.transport_scratch,
                now,
            )
        elif action == "mark-unknown":
            if not args.intent_id or not args.code:
                raise OrderIntentError(
                    "mark-unknown requires --intent-id and --code"
                )
            result = mark_unknown(
                args.state_file, args.intent_id, args.code, args.detail, now
            )
        elif action == "observe":
            if (
                not args.intent_id
                or not args.orders
                or not args.positions
                or not args.as_of_utc
            ):
                raise OrderIntentError(
                    "observe requires --intent-id, --orders, --positions, and --as-of-utc"
                )
            result = observe(
                args.state_file,
                args.intent_id,
                args.orders,
                args.order_request_cursors,
                args.positions,
                args.position_request_cursors,
                args.as_of_utc,
                now,
            )
        elif action == "abandon-prepared":
            if not args.intent_id or not args.note:
                raise OrderIntentError(
                    "abandon-prepared requires --intent-id and --note"
                )
            result = _operator_transition(
                args.state_file, args.intent_id, action, args.note, now
            )
        elif action == "operator-bind":
            if not args.intent_id or not args.order_id or not args.note:
                raise OrderIntentError(
                    "operator-bind requires --intent-id, --order-id, and --note"
                )
            result = _operator_transition(
                args.state_file,
                args.intent_id,
                action,
                args.note,
                now,
                args.order_id,
            )
        else:
            if not args.intent_id or not args.note:
                raise OrderIntentError(
                    "operator-resolve-not-submitted requires --intent-id and --note"
                )
            result = _operator_transition(
                args.state_file, args.intent_id, action, args.note, now
            )
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": action,
            "ok": False,
            "reason": "order_intent_state_error",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False))
        return 1

    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
