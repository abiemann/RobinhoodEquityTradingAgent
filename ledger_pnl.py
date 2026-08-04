#!/usr/bin/env python3
"""Auditable ledger-derived realized P&L for the local trading strategy.

Robinhood remains authoritative for account-wide realized P&L. This helper
calculates strategy telemetry from exact decimal execution aggregates in the
append-only ledger. It deliberately describes its result as a matched ledger
pool, not as broker tax-lot or tax-basis accounting.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction

from market_clock import EASTERN_STD_OFFSET, PACIFIC_STD_OFFSET, zone_time


SCHEMA_VERSION = 1
ROUNDING_POLICY = "per-fill-half-away-from-zero-to-cent"
REQUIRED_COLUMNS = (
    "timestamp_pt",
    "order_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "reason",
    "realized_pnl",
    "rules_version",
)
DECIMAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?\Z")
SYMBOL_RE = re.compile(r"[A-Z0-9.-]{1,12}\Z")
TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?[+-]\d{2}:\d{2}\Z"
)


class LedgerPnlError(ValueError):
    """The ledger cannot be interpreted safely."""


def _fraction(value: str, *, positive: bool = False) -> Fraction:
    text = str(value).strip()
    if not DECIMAL_RE.fullmatch(text):
        raise LedgerPnlError("invalid decimal value")
    result = Fraction(text)
    if positive and result <= 0:
        raise LedgerPnlError("quantity and price must be positive")
    return result


def _decimal_text(value: Fraction, places: int = 18) -> str:
    """Return a stable diagnostic decimal; displayed cents use _round_cents."""
    if not isinstance(value, Fraction):
        raise TypeError("value must be a Fraction")
    with localcontext() as context:
        context.prec = max(80, places * 3)
        decimal = Decimal(value.numerator) / Decimal(value.denominator)
        quantum = Decimal(1).scaleb(-places)
        decimal = decimal.quantize(quantum, rounding=ROUND_HALF_UP)
    text = format(decimal, "f").rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def _round_cents(value: Fraction) -> int:
    """Round one fill directly from an exact rational, half away from zero."""
    negative = value < 0
    scaled = abs(value) * 100
    whole, remainder = divmod(scaled.numerator, scaled.denominator)
    if remainder * 2 >= scaled.denominator:
        whole += 1
    return -whole if negative else whole


def _timestamp_details(text: str, *, context: str) -> tuple[datetime, str, str]:
    """Validate a true Pacific timestamp and return UTC instant, PT day, ET day."""
    if not TIMESTAMP_RE.fullmatch(text):
        raise LedgerPnlError(f"invalid timestamp {context}")
    try:
        supplied = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerPnlError(f"invalid timestamp {context}") from exc
    if supplied.tzinfo is None or supplied.utcoffset() is None:
        raise LedgerPnlError(f"invalid timestamp {context}")
    instant = supplied.astimezone(timezone.utc)
    pacific, _pacific_name, pacific_offset = zone_time(
        instant, PACIFIC_STD_OFFSET, "PST", "PDT"
    )
    if supplied.utcoffset() != timedelta(hours=pacific_offset):
        raise LedgerPnlError(f"timestamp is not Pacific time {context}")
    if supplied.replace(tzinfo=None) != pacific:
        raise LedgerPnlError(f"timestamp has an invalid Pacific offset {context}")
    eastern, _eastern_name, _eastern_offset = zone_time(
        instant, EASTERN_STD_OFFSET, "EST", "EDT"
    )
    return instant, pacific.date().isoformat(), eastern.date().isoformat()


def _read_rows(path: str) -> list[dict[str, object]]:
    try:
        handle = open(path, newline="", encoding="utf-8")
    except OSError as exc:
        raise LedgerPnlError("trade ledger is unavailable") from exc
    with handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        if not fields or len(fields) != len(set(fields)):
            raise LedgerPnlError("trade ledger header is invalid")
        missing = [name for name in REQUIRED_COLUMNS if name not in fields]
        if missing:
            raise LedgerPnlError("trade ledger is missing required columns")
        raw_rows = list(reader)

    seen_orders: set[str] = set()
    rows: list[dict[str, object]] = []
    for line_number, raw in enumerate(raw_rows, start=2):
        timestamp = (raw.get("timestamp_pt") or "").strip()
        order_id = (raw.get("order_id") or "").strip()
        symbol = (raw.get("symbol") or "").strip()
        side = (raw.get("side") or "").strip().lower()
        reason = (raw.get("reason") or "").strip()
        rules_version = (raw.get("rules_version") or "").strip()
        if not order_id or order_id in seen_orders:
            raise LedgerPnlError(f"missing or duplicate order id on ledger line {line_number}")
        seen_orders.add(order_id)
        if not SYMBOL_RE.fullmatch(symbol) or side not in {"buy", "sell"}:
            raise LedgerPnlError(f"invalid trade identity on ledger line {line_number}")
        if not reason or not rules_version:
            raise LedgerPnlError(f"missing ledger attribution on line {line_number}")
        instant, day_pt, day_et = _timestamp_details(
            timestamp, context=f"on ledger line {line_number}"
        )
        try:
            quantity = _fraction(raw.get("quantity") or "", positive=True)
            price = _fraction(raw.get("price") or "", positive=True)
            recorded_text = (raw.get("realized_pnl") or "").strip()
            recorded = _fraction(recorded_text) if recorded_text else None
        except LedgerPnlError as exc:
            raise LedgerPnlError(f"invalid numeric value on ledger line {line_number}") from exc
        rows.append({
            "timestamp_pt": timestamp,
            "instant": instant,
            "day": day_pt,
            "day_et": day_et,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "reason": reason,
            "recorded": recorded,
            "rules_version": rules_version,
            "line_number": line_number,
        })

    rows.sort(key=lambda row: (row["instant"], row["order_id"]))
    previous_by_symbol: dict[str, dict[str, object]] = {}
    for row in rows:
        previous = previous_by_symbol.get(str(row["symbol"]))
        if (
            previous is not None
            and previous["instant"] == row["instant"]
            and previous["side"] != row["side"]
        ):
            raise LedgerPnlError(
                f"ambiguous same-time buy/sell sequence for {row['symbol']}"
            )
        previous_by_symbol[str(row["symbol"])] = row
    return rows


def _empty_inventory() -> dict[str, object]:
    return {"quantity": Fraction(0), "cost": Fraction(0), "reliable": True}


def _apply_inventory(
    inventories: dict[str, dict[str, object]], row: dict[str, object]
) -> tuple[Fraction | None, Fraction | None]:
    """Apply one chronological row; return matched P&L and pool basis."""
    symbol = str(row["symbol"])
    side = str(row["side"])
    quantity = Fraction(row["quantity"])
    price = Fraction(row["price"])
    inventory = inventories.get(symbol)
    if side == "buy":
        if inventory is None:
            inventory = _empty_inventory()
            inventories[symbol] = inventory
        if bool(inventory["reliable"]):
            inventory["quantity"] = Fraction(inventory["quantity"]) + quantity
            inventory["cost"] = Fraction(inventory["cost"]) + quantity * price
        return None, None

    if (
        inventory is not None
        and bool(inventory["reliable"])
        and Fraction(inventory["quantity"]) >= quantity
    ):
        inventory_quantity = Fraction(inventory["quantity"])
        inventory_cost = Fraction(inventory["cost"])
        consumed_cost = inventory_cost * quantity / inventory_quantity
        basis = consumed_cost / quantity
        matched = quantity * price - consumed_cost
        inventory["quantity"] = inventory_quantity - quantity
        inventory["cost"] = inventory_cost - consumed_cost
        if Fraction(inventory["quantity"]) == 0:
            inventories.pop(symbol, None)
        return matched, basis

    if inventory is None:
        inventory = _empty_inventory()
        inventories[symbol] = inventory
    inventory["reliable"] = False
    return None, None


def reconcile_ledger(path: str) -> dict[str, object]:
    """Return chronological sanitized rows and ledger-pool P&L telemetry."""
    inventories: dict[str, dict[str, object]] = {}
    output: list[dict[str, object]] = []
    for row in _read_rows(path):
        matched, basis = _apply_inventory(inventories, row)
        side = str(row["side"])
        recorded = row["recorded"]
        selected = matched if matched is not None else recorded if side == "sell" else None
        source = (
            "not-applicable" if side == "buy"
            else "matched-ledger-pool" if matched is not None
            else "recorded-estimate" if recorded is not None
            else "unavailable"
        )
        difference = (
            matched - Fraction(recorded)
            if matched is not None and recorded is not None
            else None
        )
        output.append({
            "timestamp_pt": row["timestamp_pt"],
            "day": row["day"],
            "day_et": row["day_et"],
            "symbol": row["symbol"],
            "side": side,
            "reason": row["reason"],
            "rules_version": row["rules_version"],
            "realized_pnl": _decimal_text(selected) if selected is not None else None,
            "realized_pnl_cents": _round_cents(selected) if selected is not None else None,
            "recorded_realized_pnl": (
                _decimal_text(Fraction(recorded)) if recorded is not None else None
            ),
            "pnl_source": source,
            "matched_basis_price": _decimal_text(basis) if basis is not None else None,
            "recorded_difference": _decimal_text(difference) if difference is not None else None,
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "rounding_policy": ROUNDING_POLICY,
        "rows": output,
    }


def calculate_sale(
    path: str,
    symbol: str,
    quantity_text: str,
    price_text: str,
    sale_time_text: str,
) -> dict[str, object]:
    """Calculate a prospective sale from ledger rows earlier than the sale."""
    symbol = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise LedgerPnlError("symbol is invalid")
    quantity = _fraction(quantity_text, positive=True)
    price = _fraction(price_text, positive=True)
    sale_instant, _day_pt, _day_et = _timestamp_details(
        sale_time_text.strip(), context="for prospective sale"
    )

    inventories: dict[str, dict[str, object]] = {}
    for row in _read_rows(path):
        if row["instant"] > sale_instant:
            break
        if row["instant"] == sale_instant:
            if row["symbol"] == symbol:
                raise LedgerPnlError("same-time symbol history is ambiguous")
            continue
        _apply_inventory(inventories, row)

    inventory = inventories.get(symbol)
    if (
        inventory is None
        or not bool(inventory["reliable"])
        or Fraction(inventory["quantity"]) < quantity
    ):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "symbol": symbol,
            "reason": "complete matched ledger acquisition pool is unavailable",
        }
    inventory_quantity = Fraction(inventory["quantity"])
    basis = Fraction(inventory["cost"]) / inventory_quantity
    pnl = quantity * (price - basis)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "matched-ledger-pool",
        "rounding_policy": ROUNDING_POLICY,
        "symbol": symbol,
        "quantity": _decimal_text(quantity),
        "sale_price": _decimal_text(price),
        "sale_time_pt": sale_time_text.strip(),
        "basis_price": _decimal_text(basis),
        "realized_pnl": _decimal_text(pnl),
        "realized_pnl_cents": _round_cents(pnl),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ledger", default=os.path.join(os.path.dirname(__file__), "trade-ledger.csv")
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--quantity", required=True)
    parser.add_argument("--sale-price", required=True)
    parser.add_argument("--sale-time", required=True)
    args = parser.parse_args(argv)
    try:
        result = calculate_sale(
            args.ledger, args.symbol, args.quantity, args.sale_price, args.sale_time
        )
    except LedgerPnlError as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "unavailable",
            "symbol": str(args.symbol).strip().upper(),
            "reason": str(exc),
        }
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    return 0 if result.get("status") == "matched-ledger-pool" else 2


if __name__ == "__main__":
    sys.exit(main())