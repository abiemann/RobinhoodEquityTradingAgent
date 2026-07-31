#!/usr/bin/env python3
"""Deterministically validate and load the routine's ``constants.md``.

The scheduled trading routine must run this script before its clock, account
lookup, or any other broker/market action.  It is intentionally strict:

* the Markdown constants table must be present and structurally valid;
* every known constant must appear exactly once and no unknown row is allowed;
* values must use the documented literal form and satisfy type/range rules;
* coupled safety settings are checked together; and
* invalid UTF-8, NaN-like values, missing rows, and duplicates fail closed.

Successful JSON output is the machine-readable handoff for the rest of the
run.  A validation failure exits nonzero, writes actionable diagnostics to
stderr, and emits no stdout that could be mistaken for a usable result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1

REQUIRED_CONSTANTS = (
    "DRY_RUN",
    "AGENTIC_ACCOUNT_NAME",
    "PRICE_MIN",
    "PRICE_MAX",
    "MIN_REL_VOLUME",
    "MIN_ABS_PCT_CHANGE",
    "TOP_N",
    "SCAN_TITLE",
    "HIGH_LOOKBACK_DAYS",
    "VOLUME_LOOKBACK_DAYS",
    "MIN_MEDIAN_DOLLAR_VOLUME",
    "DIP_ENTRY_PCT",
    "RSI_PERIOD",
    "RSI_INTERVAL",
    "RSI_OVERSOLD",
    "RSI_LOOKBACK_BARS",
    "RSI_MAX_ENTRY",
    "RSI_CONFIRM_BARS",
    "MAX_SPREAD_BUY_PCT",
    "TAKE_PROFIT_PCT",
    "BUY_SIZE_PCT",
    "MIN_ORDER_DOLLARS",
    "EXT_HOURS_LIMIT_BUFFER_PCT",
    "REGULAR_HOURS_BUY_ONLY",
    "MAX_POSITION_PCT",
    "STOP_LOSS_PCT",
    "REENTRY_COOLDOWN_DAYS",
    "DAILY_LOSS_HALT_PCT",
    "STOP_COUNT_HALT",
    "SKIP_BUY_IF_SPY_RED",
    "NO_BUY_FIRST_MINUTES",
)

BOOLEAN_CONSTANTS = frozenset(
    {"DRY_RUN", "REGULAR_HOURS_BUY_ONLY", "SKIP_BUY_IF_SPY_RED"}
)
STRING_CONSTANTS = frozenset({"AGENTIC_ACCOUNT_NAME", "SCAN_TITLE"})
INTEGER_BOUNDS: Mapping[str, tuple[int | None, int | None]] = {
    "TOP_N": (1, None),
    "HIGH_LOOKBACK_DAYS": (1, None),
    "VOLUME_LOOKBACK_DAYS": (1, None),
    "RSI_PERIOD": (2, None),
    "RSI_LOOKBACK_BARS": (2, None),
    "RSI_CONFIRM_BARS": (1, None),
    "REENTRY_COOLDOWN_DAYS": (0, None),
    "STOP_COUNT_HALT": (1, None),
    "NO_BUY_FIRST_MINUTES": (0, 390),
}
DECIMAL_BOUNDS: Mapping[
    str, tuple[Decimal | None, bool, Decimal | None, bool]
] = {
    # name: (minimum, minimum inclusive, maximum, maximum inclusive)
    "PRICE_MIN": (Decimal(0), False, None, True),
    "PRICE_MAX": (Decimal(0), False, None, True),
    "MIN_REL_VOLUME": (Decimal(1), False, None, True),
    "MIN_ABS_PCT_CHANGE": (Decimal(0), False, None, True),
    "MIN_MEDIAN_DOLLAR_VOLUME": (Decimal(0), False, None, True),
    "DIP_ENTRY_PCT": (Decimal(0), False, Decimal(100), False),
    "RSI_OVERSOLD": (Decimal(0), True, Decimal(100), True),
    "RSI_MAX_ENTRY": (Decimal(0), True, Decimal(100), True),
    "MAX_SPREAD_BUY_PCT": (Decimal(0), False, Decimal(100), False),
    "TAKE_PROFIT_PCT": (Decimal(0), False, None, True),
    "BUY_SIZE_PCT": (Decimal(0), False, Decimal(100), True),
    "MIN_ORDER_DOLLARS": (Decimal(0), False, None, True),
    "EXT_HOURS_LIMIT_BUFFER_PCT": (Decimal(0), True, None, True),
    "MAX_POSITION_PCT": (Decimal(0), False, Decimal(100), True),
    "STOP_LOSS_PCT": (Decimal(0), False, Decimal(100), False),
    "DAILY_LOSS_HALT_PCT": (Decimal(0), False, Decimal(100), True),
}
RSI_INTERVALS = frozenset(
    {
        "15second",
        "30second",
        "minute",
        "5minute",
        "10minute",
        "30minute",
        "hour",
        "4hour",
        "day",
        "week",
        "month",
        "3month",
        "6month",
        "year",
        "5year",
        "10year",
        "20year",
        "50year",
    }
)

_HEADER_RE = re.compile(
    r"^\|\s*Constant\s*\|\s*Value\s*\|\s*Meaning\s*\|\s*$"
)
_SEPARATOR_RE = re.compile(
    r"^\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|\s*:?-{3,}:?\s*\|\s*$"
)
_ROW_RE = re.compile(
    r"^\|\s*`(?P<name>[A-Z][A-Z0-9_]*)`\s*"
    r"\|\s*`(?P<value>[^`]*)`\s*"
    r"\|\s*(?P<meaning>(?:\\\||[^|])*)\s*\|\s*$"
)
_ROW_NAME_RE = re.compile(r"^\|\s*`(?P<name>[^`]*)`")
_INTEGER_RE = re.compile(r"^(?:0|[1-9]\d*)$")
_DECIMAL_RE = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d+)?$")


class ConstantsValidationError(ValueError):
    """Raised when constants.md cannot support a safe configuration."""

    def __init__(self, errors: Sequence[str]):
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ParsedRow:
    name: str
    raw_value: str
    line_number: int


@dataclass(frozen=True)
class ValidatedConstants:
    path: Path
    source_sha256: str
    values: Mapping[str, Any]
    raw_values: Mapping[str, str]

    def json_document(self) -> dict[str, Any]:
        json_values = {
            name: (
                self.raw_values[name]
                if isinstance(self.values[name], Decimal)
                else self.values[name]
            )
            for name in REQUIRED_CONSTANTS
        }
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "valid",
            "constant_count": len(REQUIRED_CONSTANTS),
            "source": self.path.name,
            "source_sha256": self.source_sha256,
            "values": json_values,
        }


def default_constants_path() -> Path:
    return Path(__file__).resolve().with_name("constants.md")


def _read_utf8(path: Path) -> tuple[bytes, str]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConstantsValidationError(
            [f"{path}: cannot read constants file: {exc}"]
        ) from exc
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConstantsValidationError(
            [f"{path}: constants file is not valid UTF-8: {exc}"]
        ) from exc


def _parse_table(text: str, path: Path) -> tuple[dict[str, ParsedRow], list[str]]:
    lines = text.splitlines()
    header_indexes = [
        index for index, line in enumerate(lines) if _HEADER_RE.fullmatch(line)
    ]
    errors: list[str] = []
    if len(header_indexes) != 1:
        errors.append(
            f"{path}: expected exactly one '| Constant | Value | Meaning |' "
            f"table header; found {len(header_indexes)}"
        )
        return {}, errors

    header_index = header_indexes[0]
    if (
        header_index + 1 >= len(lines)
        or not _SEPARATOR_RE.fullmatch(lines[header_index + 1])
    ):
        errors.append(
            f"{path} line {header_index + 2}: malformed constants table separator"
        )
        return {}, errors

    rows: dict[str, ParsedRow] = {}
    index = header_index + 2
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            break
        if not line.lstrip().startswith("|"):
            break
        line_number = index + 1
        match = _ROW_RE.fullmatch(line)
        if not match:
            name_match = _ROW_NAME_RE.match(line)
            label = (
                f" for {name_match.group('name')}"
                if name_match is not None
                else ""
            )
            errors.append(
                f"{path} line {line_number}: malformed constant row{label}; "
                "expected | `NAME` | `value` | meaning |"
            )
            index += 1
            continue

        name = match.group("name")
        if name not in REQUIRED_CONSTANTS:
            errors.append(
                f"{path} line {line_number}: unexpected constant {name}"
            )
        elif name in rows:
            errors.append(
                f"{path} line {line_number}: duplicate constant {name}; "
                f"first appeared on line {rows[name].line_number}"
            )
        else:
            rows[name] = ParsedRow(name, match.group("value"), line_number)
        index += 1

    table_row_indexes = set(range(header_index + 2, index))
    for outside_index, line in enumerate(lines):
        if outside_index in table_row_indexes:
            continue
        name_match = _ROW_NAME_RE.match(line)
        if (
            name_match is not None
            and re.fullmatch(r"[A-Z][A-Z0-9_]*", name_match.group("name"))
        ):
            errors.append(
                f"{path} line {outside_index + 1}: constant-like row "
                f"{name_match.group('name')} appears outside the single "
                "constants table"
            )

    missing = [name for name in REQUIRED_CONSTANTS if name not in rows]
    if missing:
        errors.append(f"{path}: missing required constant(s): {', '.join(missing)}")
    return rows, errors


def _location(path: Path, row: ParsedRow) -> str:
    return f"{path} line {row.line_number}: {row.name}"


def _parse_boolean(path: Path, row: ParsedRow) -> tuple[Any, str | None]:
    if row.raw_value == "true":
        return True, None
    if row.raw_value == "false":
        return False, None
    return None, f"{_location(path, row)} must be exactly true or false"


def _parse_string(path: Path, row: ParsedRow) -> tuple[Any, str | None]:
    try:
        value = json.loads(row.raw_value)
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, str):
        return None, (
            f"{_location(path, row)} must be a double-quoted JSON string"
        )
    if not value or value != value.strip():
        return None, f"{_location(path, row)} must be a nonempty trimmed string"
    if any(
        ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
        for character in value
    ):
        return None, (
            f"{_location(path, row)} must not contain control characters "
            "or unpaired Unicode surrogates"
        )
    return value, None


def _parse_integer(path: Path, row: ParsedRow) -> tuple[Any, str | None]:
    if not _INTEGER_RE.fullmatch(row.raw_value):
        return None, (
            f"{_location(path, row)} must be a base-10 integer without "
            "a sign, exponent, or leading zero"
        )
    try:
        value = int(row.raw_value)
    except ValueError:
        return None, (
            f"{_location(path, row)} integer literal exceeds the supported size"
        )
    minimum, maximum = INTEGER_BOUNDS[row.name]
    if minimum is not None and value < minimum:
        return None, f"{_location(path, row)} must be >= {minimum}"
    if maximum is not None and value > maximum:
        return None, f"{_location(path, row)} must be <= {maximum}"
    return value, None


def _parse_decimal(path: Path, row: ParsedRow) -> tuple[Any, str | None]:
    if not _DECIMAL_RE.fullmatch(row.raw_value):
        return None, (
            f"{_location(path, row)} must be a plain nonnegative decimal "
            "without a sign, exponent, NaN, Infinity, or leading zero"
        )
    try:
        value = Decimal(row.raw_value)
    except InvalidOperation:
        return None, f"{_location(path, row)} is not a valid decimal"
    if not value.is_finite():
        return None, f"{_location(path, row)} must be finite"

    minimum, minimum_inclusive, maximum, maximum_inclusive = DECIMAL_BOUNDS[
        row.name
    ]
    if minimum is not None:
        invalid = value < minimum if minimum_inclusive else value <= minimum
        if invalid:
            operator = ">=" if minimum_inclusive else ">"
            return None, f"{_location(path, row)} must be {operator} {minimum}"
    if maximum is not None:
        invalid = value > maximum if maximum_inclusive else value >= maximum
        if invalid:
            operator = "<=" if maximum_inclusive else "<"
            return None, f"{_location(path, row)} must be {operator} {maximum}"

    return value, None


def _parse_interval(path: Path, row: ParsedRow) -> tuple[Any, str | None]:
    if row.raw_value not in RSI_INTERVALS:
        return None, (
            f"{_location(path, row)} must be one of: "
            + ", ".join(sorted(RSI_INTERVALS))
        )
    return row.raw_value, None


def _validate_rows(
    rows: Mapping[str, ParsedRow], path: Path
) -> tuple[dict[str, Any], list[str]]:
    values: dict[str, Any] = {}
    errors: list[str] = []
    for name in REQUIRED_CONSTANTS:
        row = rows.get(name)
        if row is None:
            continue
        if name in BOOLEAN_CONSTANTS:
            value, error = _parse_boolean(path, row)
        elif name in STRING_CONSTANTS:
            value, error = _parse_string(path, row)
        elif name in INTEGER_BOUNDS:
            value, error = _parse_integer(path, row)
        elif name in DECIMAL_BOUNDS:
            value, error = _parse_decimal(path, row)
        elif name == "RSI_INTERVAL":
            value, error = _parse_interval(path, row)
        else:  # pragma: no cover - guarded by the complete schema test
            value, error = None, f"internal schema missing for {name}"
        if error is not None:
            errors.append(error)
        else:
            values[name] = value

    def compare(
        left: str, operator: str, right: str, explanation: str
    ) -> None:
        if left not in values or right not in values:
            return
        passed = (
            values[left] < values[right]
            if operator == "<"
            else values[left] <= values[right]
        )
        if not passed:
            errors.append(
                f"{path}: {left} must be {operator} {right} ({explanation}); "
                f"got {values[left]} and {values[right]}"
            )

    compare("PRICE_MIN", "<", "PRICE_MAX", "price band must have width")
    compare(
        "RSI_OVERSOLD",
        "<=",
        "RSI_MAX_ENTRY",
        "oversold threshold cannot exceed the entry ceiling",
    )
    compare(
        "RSI_CONFIRM_BARS",
        "<=",
        "RSI_LOOKBACK_BARS",
        "confirmation must fit inside the RSI window",
    )
    compare(
        "MAX_SPREAD_BUY_PCT",
        "<",
        "STOP_LOSS_PCT",
        "entry spread must stay below the protective stop distance",
    )
    compare(
        "BUY_SIZE_PCT",
        "<=",
        "MAX_POSITION_PCT",
        "a standard buy cannot exceed the position cap",
    )
    return values, errors


def validate_constants_file(
    path: os.PathLike[str] | str | None = None,
) -> ValidatedConstants:
    """Load and validate a constants file, returning typed exact-source data."""

    constants_path = Path(path) if path is not None else default_constants_path()
    raw, text = _read_utf8(constants_path)
    rows, errors = _parse_table(text, constants_path)
    values, value_errors = _validate_rows(rows, constants_path)
    errors.extend(value_errors)
    if errors:
        raise ConstantsValidationError(errors)
    raw_values = {name: rows[name].raw_value for name in REQUIRED_CONSTANTS}
    return ValidatedConstants(
        path=constants_path,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        values={name: values[name] for name in REQUIRED_CONSTANTS},
        raw_values=raw_values,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--constants",
        help="alternate constants.md path (tests/tools only; routine omits it)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the validated machine-readable handoff",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = validate_constants_file(args.constants)
    except ConstantsValidationError as exc:
        print(f"validate_constants.py: ERROR: {exc.errors[0]}", file=sys.stderr)
        for error in exc.errors[1:]:
            print(f"- {error}", file=sys.stderr)
        return 2

    if args.json:
        print(
            json.dumps(
                result.json_document(),
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    else:
        print(
            f"constants valid: {len(REQUIRED_CONSTANTS)} unique values "
            f"({result.source_sha256[:12]})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
