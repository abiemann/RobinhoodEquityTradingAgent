#!/usr/bin/env python3
"""Working-list filter for the Robinhood momentum routine (Step 6 of
robinhood-momentum-routine-autonomous.md).

Consumes the saved run_scan JSON result (either the direct payload or the
standard MCP structuredContent envelope) and applies the routine's client-side
screen: price band, relative-volume floor, minimum absolute day move, then
ranks by relative volume and keeps the top N.

Usage:
  python3 filter_scan.py --scratch <preflighted scratch> \
      (--scan-file <run_scan result file> | --scan-purpose <purpose>) \
      --expected-constants-sha256 <startup constants receipt source_sha256> \
      [--json-out working_list.json]

The five strategy values are loaded directly from the validated root
``constants.md`` and its exact source hash must match the startup receipt — no
model-authored duplicate values or silent stale defaults.

With ``--json-out``, the script atomically writes and reads back the validated
handoff, then emits exactly one compact JSON success receipt on stdout.  The
receipt carries the same TOP_N-bounded unrounded rows inline plus the durable
file's basename, byte count, and SHA-256.  It also binds those rows to the
validated invocation scratch ID, exact scan selector, and hash of the committed
scan-source bytes, so a runner never needs a second model-authored file-read
command and cannot reuse a stale same-basename receipt.  Without
``--json-out``, stdout remains the human-readable diagnostic table.

Verified response schema (live 2026-07-06 → 2026-07-17; do NOT rediscover it
per run): {"data": {"result": {"results": [...], "total_items": N, ...}},
"guide": ...}. Each row: {"ticker": "XYZ", "instrument_id": ..., "columns":
{"Last": "4.45", "% Change": "0.1528", "Relative volume": "557.75",
"Volume": "20372901", "Symbol": "XYZ", ...}} — prices and volumes are
STRINGS, and "% Change" is a DECIMAL FRACTION (0.0301 = 3.01%), converted
to percent here. Rows missing or carrying non-finite needed fields are skipped
and counted.

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
`python3 -m unittest discover -s tests` (Windows:
`py -3 -m unittest discover -s tests`) and require all tests to pass before
committing. Expected values are
live-verified; if an intentional behavior change breaks one, update the
expectation deliberately — never delete a test to go green.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from broker_snapshot import (
    validate_bound_external_json_purpose,
    validate_bound_external_json_source,
    validate_scratch_directory,
)
from validate_constants import validate_constants_file


SCHEMA_VERSION = 1
_HANDOFF_KEYS = {
    "total_items",
    "rows_returned",
    "rows_skipped",
    "passed_filters",
    "working_list",
}
_WORKING_ROW_KEYS = {
    "symbol",
    "last",
    "rel_volume",
    "day_pct_change",
    "volume",
}
_CANONICAL_SYMBOL_RE = re.compile(
    r"[A-Z][A-Z0-9]*(?:\.[A-Z0-9]+)*\Z",
    re.ASCII,
)
_MAX_SYMBOL_LENGTH = 32


def _reject_nonfinite_json(token):
    raise ValueError(f"non-finite JSON constant {token!r} is not allowed")


def finite_float(value, field):
    """Parse a finite float or fail closed with a useful field name."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field}: invalid number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field}: must be finite")
    return number


def sha256_arg(value):
    if re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise argparse.ArgumentTypeError(
            "must be exactly 64 lowercase hexadecimal characters"
        )
    return value


def canonical_symbol(value):
    """Return one exact Robinhood ticker or reject display/alias forms."""
    if (
        type(value) is not str
        or value != value.strip()
        or not value.isascii()
        or not 1 <= len(value) <= _MAX_SYMBOL_LENGTH
        or _CANONICAL_SYMBOL_RE.fullmatch(value) is None
    ):
        raise ValueError(
            "expected exact trimmed uppercase ASCII ticker text "
            "(letters/digits with dot-separated class suffixes)"
        )
    return value


def validate_handoff(document, top_n):
    """Validate the complete machine handoff before it becomes authoritative."""
    if not isinstance(document, dict) or set(document) != _HANDOFF_KEYS:
        raise ValueError("working-list handoff: unexpected root schema")
    for field in (
        "total_items",
        "rows_returned",
        "rows_skipped",
        "passed_filters",
    ):
        value = document[field]
        if type(value) is not int or value < 0:
            raise ValueError(
                f"working-list handoff: {field}: expected a non-negative integer"
            )
    if document["total_items"] < document["rows_returned"]:
        raise ValueError(
            "working-list handoff: total_items cannot be smaller than rows_returned"
        )
    if document["rows_skipped"] > document["rows_returned"]:
        raise ValueError(
            "working-list handoff: rows_skipped cannot exceed rows_returned"
        )
    if document["passed_filters"] > document["rows_returned"]:
        raise ValueError(
            "working-list handoff: passed_filters cannot exceed rows_returned"
        )
    if (
        document["rows_skipped"] + document["passed_filters"]
        > document["rows_returned"]
    ):
        raise ValueError(
            "working-list handoff: skipped and passing rows cannot exceed "
            "rows_returned"
        )

    working = document["working_list"]
    if not isinstance(working, list) or len(working) > top_n:
        raise ValueError(
            "working-list handoff: working_list must be an array bounded by top_n"
        )
    if len(working) > document["passed_filters"]:
        raise ValueError(
            "working-list handoff: working_list cannot exceed passed_filters"
        )
    symbols = set()
    previous_rel_volume = math.inf
    for index, row in enumerate(working):
        context = f"working-list handoff: working_list[{index}]"
        if not isinstance(row, dict) or set(row) != _WORKING_ROW_KEYS:
            raise ValueError(f"{context}: unexpected row schema")
        try:
            symbol = canonical_symbol(row["symbol"])
        except ValueError as exc:
            raise ValueError(f"{context}.symbol: {exc}") from exc
        if symbol in symbols:
            raise ValueError(f"{context}.symbol: duplicate symbol {symbol!r}")
        symbols.add(symbol)
        for field in ("last", "rel_volume", "day_pct_change", "volume"):
            value = row[field]
            if type(value) is not float or not math.isfinite(value):
                raise ValueError(f"{context}.{field}: expected a finite JSON number")
        if row["last"] <= 0:
            raise ValueError(f"{context}.last: must be greater than zero")
        if row["rel_volume"] < 0 or row["volume"] < 0:
            raise ValueError(
                f"{context}: rel_volume and volume must be non-negative"
            )
        if row["rel_volume"] > previous_rel_volume:
            raise ValueError(
                "working-list handoff: working_list is not ordered by "
                "non-increasing rel_volume"
            )
        previous_rel_volume = row["rel_volume"]
    return document


def _directory_identity(path):
    status = os.stat(path, follow_symlinks=False)
    return status.st_dev, status.st_ino


def _revalidate_scratch_binding(scratch, expected_scratch_id, expected_identity):
    canonical_scratch, marker = validate_scratch_directory(scratch)
    if canonical_scratch != scratch:
        raise ValueError("scratch canonical path changed during filter execution")
    if marker["scratch_id"] != expected_scratch_id:
        raise ValueError("scratch_id changed during filter execution")
    if _directory_identity(canonical_scratch) != expected_identity:
        raise ValueError("scratch directory identity changed during filter execution")


def write_handoff(
    path,
    scratch,
    document,
    top_n,
    *,
    expected_scratch_id,
    expected_scratch_identity,
):
    """Atomically persist, read back, and bind one validated scratch handoff."""
    validate_handoff(document, top_n)
    scratch_path = Path(scratch).resolve(strict=True)
    output_path = Path(os.path.abspath(os.fspath(path)))
    if os.path.lexists(output_path):
        raise ValueError("--json-out already exists; refusing to replace it")
    resolved_output = output_path.resolve(strict=False)
    if resolved_output.parent != scratch_path:
        raise ValueError(
            "--json-out must be a direct child of the preflighted scratch directory"
        )

    raw = (
        json.dumps(
            document,
            ensure_ascii=True,
            allow_nan=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="wb",
            prefix=f".{resolved_output.name}.",
            suffix=".tmp",
            dir=scratch_path,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        _revalidate_scratch_binding(
            scratch_path, expected_scratch_id, expected_scratch_identity
        )
        try:
            os.link(temporary, resolved_output)
        except FileExistsError as exc:
            raise ValueError(
                "--json-out already exists; refusing to replace it"
            ) from exc
        try:
            temporary.unlink()
        except OSError:
            # The authoritative name is already a complete hard link to the
            # flushed bytes. A hidden temporary link is harmless and must not
            # turn that valid publication into a false failure.
            pass
        temporary = None
        readback = resolved_output.read_bytes()
        if readback != raw:
            raise OSError("working-list handoff read-back did not match written bytes")
        parsed = json.loads(
            readback.decode("utf-8"),
            parse_constant=_reject_nonfinite_json,
        )
        validate_handoff(parsed, top_n)
        if parsed != document:
            raise OSError("working-list handoff read-back changed the document")
        _revalidate_scratch_binding(
            scratch_path, expected_scratch_id, expected_scratch_identity
        )
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        # Once published, never unlink by pathname during failure cleanup.
        # Another process could have replaced that name between the failed
        # read/validation and cleanup. Leaving the no-clobber artifact for
        # audit is safe because this invocation emits no success receipt.
        raise

    return resolved_output, readback


def success_receipt(
    output_path,
    raw,
    document,
    *,
    scratch,
    scratch_id,
    scan_selector_kind,
    scan_purpose,
    scan_file,
    scan_source_raw,
    constants_source_sha256,
    price_min,
    price_max,
    min_rel_volume,
    min_abs_pct_change,
    top_n,
):
    """Return the compact, inline authority for a successful filter run."""
    return {
        "schema_version": SCHEMA_VERSION,
        "action": "filter-scan",
        "ok": True,
        "scratch": str(scratch),
        "scratch_id": scratch_id,
        "scan_selector_kind": scan_selector_kind,
        "scan_purpose": scan_purpose,
        "scan_file": scan_file,
        "scan_source_sha256": hashlib.sha256(scan_source_raw).hexdigest(),
        "constants_source_sha256": constants_source_sha256,
        "price_min": price_min,
        "price_max": price_max,
        "min_rel_volume": min_rel_volume,
        "min_abs_pct_change": min_abs_pct_change,
        "top_n": top_n,
        "working_list_file": output_path.name,
        "byte_count": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        **document,
    }


def load_result(doc, path):
    if isinstance(doc, dict) and "isError" in doc:
        if not isinstance(doc["isError"], bool):
            raise ValueError(f"{path}: isError: expected a boolean")
        if doc["isError"]:
            raise ValueError(f"{path}: broker tool result reports an error")
    if isinstance(doc, dict) and "structuredContent" in doc:
        structured = doc["structuredContent"]
        if not isinstance(structured, dict):
            raise ValueError(f"{path}: structuredContent: expected an object")
        doc = structured
    if isinstance(doc, dict):
        if "data" in doc and isinstance(doc["data"], dict) and "result" in doc["data"]:
            return doc["data"]["result"]
        if "result" in doc:
            return doc["result"]
        if "results" in doc:
            return doc
    raise ValueError(f"{path}: unrecognized shape - expected a run_scan result")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scratch", required=True,
                    help="absolute preflighted scratch whose transport binding owns the scan response")
    scan_input = ap.add_mutually_exclusive_group(required=True)
    scan_input.add_argument("--scan-file", help="raw run_scan JSON result file")
    scan_input.add_argument(
        "--scan-purpose",
        help="committed response-source purpose containing the raw run_scan result",
    )
    ap.add_argument(
        "--expected-constants-sha256",
        type=sha256_arg,
        required=True,
        help="exact source_sha256 from the startup validated constants receipt",
    )
    ap.add_argument("--json-out", help="optional path for the machine-readable working list")
    args = ap.parse_args()

    canonical_scratch, scratch_marker = validate_scratch_directory(args.scratch)
    scratch_identity = _directory_identity(canonical_scratch)
    validated_constants = validate_constants_file()
    if validated_constants.source_sha256 != args.expected_constants_sha256:
        raise ValueError(
            "constants.md SHA-256 does not match --expected-constants-sha256"
        )
    price_min = finite_float(validated_constants.values["PRICE_MIN"], "PRICE_MIN")
    price_max = finite_float(validated_constants.values["PRICE_MAX"], "PRICE_MAX")
    min_rel_volume = finite_float(
        validated_constants.values["MIN_REL_VOLUME"], "MIN_REL_VOLUME"
    )
    min_abs_pct_change = finite_float(
        validated_constants.values["MIN_ABS_PCT_CHANGE"],
        "MIN_ABS_PCT_CHANGE",
    )
    top_n = validated_constants.values["TOP_N"]
    if type(top_n) is not int or top_n < 1:
        raise ValueError("TOP_N: validated value must be a positive integer")

    if args.scan_purpose is not None:
        scan_path, scan_document, scan_bytes = (
            validate_bound_external_json_purpose(
                canonical_scratch, args.scan_purpose
            )
        )
        scan_label = args.scan_purpose
        scan_selector_kind = "purpose"
        receipt_scan_purpose = args.scan_purpose
        receipt_scan_file = None
    else:
        scan_path, scan_document, scan_bytes = (
            validate_bound_external_json_source(canonical_scratch, args.scan_file)
        )
        scan_label = args.scan_file
        scan_selector_kind = "file"
        receipt_scan_purpose = None
        receipt_scan_file = str(scan_path)
    result = load_result(scan_document, scan_label)
    if not isinstance(result, dict):
        raise ValueError(f"{scan_label}: run_scan result must be an object")
    missing_result_fields = [
        field for field in ("results", "total_items") if field not in result
    ]
    if missing_result_fields:
        raise ValueError(
            f"{scan_label}: run_scan result missing required field(s): "
            + ", ".join(missing_result_fields)
        )
    rows = result["results"]
    if not isinstance(rows, list):
        raise ValueError(f"{scan_label}: results must be an array")
    total_items = result["total_items"]
    if isinstance(total_items, Decimal):
        if total_items != total_items.to_integral_value():
            raise ValueError("total_items: expected an integer")
        total_items = int(total_items)
    if type(total_items) is not int or total_items < 0:
        raise ValueError("total_items: expected a non-negative integer")

    survivors = []
    skipped_fields = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped_fields += 1
            continue
        cols = row.get("columns", {})
        if not isinstance(cols, dict):
            skipped_fields += 1
            continue
        try:
            last = finite_float(cols["Last"], "Last")
            rel_vol = finite_float(cols["Relative volume"], "Relative volume")
            pct = finite_float(cols["% Change"], "% Change") * 100.0
            if not math.isfinite(pct):
                raise ValueError("% Change: percent must be finite")
            volume = finite_float(cols["Volume"], "Volume")
        except (KeyError, TypeError, ValueError):
            skipped_fields += 1
            continue
        if not (price_min <= last <= price_max):
            continue
        if rel_vol < min_rel_volume:
            continue
        if abs(pct) < min_abs_pct_change:
            continue
        try:
            symbol = canonical_symbol(row.get("ticker"))
            visible_symbol = canonical_symbol(cols.get("Symbol"))
        except ValueError:
            skipped_fields += 1
            continue
        if visible_symbol != symbol:
            skipped_fields += 1
            continue
        survivors.append({"symbol": symbol, "last": last, "rel_volume": rel_vol,
                          "day_pct_change": pct, "volume": volume})

    survivors.sort(key=lambda s: s["rel_volume"], reverse=True)
    working = survivors[:top_n]

    document = {
        "total_items": total_items,
        "rows_returned": len(rows),
        "rows_skipped": skipped_fields,
        "passed_filters": len(survivors),
        "working_list": working,
    }
    if args.json_out:
        output_path, raw = write_handoff(
            args.json_out,
            canonical_scratch,
            document,
            top_n,
            expected_scratch_id=scratch_marker["scratch_id"],
            expected_scratch_identity=scratch_identity,
        )
        print(
            json.dumps(
                success_receipt(
                    output_path,
                    raw,
                    document,
                    scratch=canonical_scratch,
                    scratch_id=scratch_marker["scratch_id"],
                    scan_selector_kind=scan_selector_kind,
                    scan_purpose=receipt_scan_purpose,
                    scan_file=receipt_scan_file,
                    scan_source_raw=scan_bytes,
                    constants_source_sha256=validated_constants.source_sha256,
                    price_min=validated_constants.raw_values["PRICE_MIN"],
                    price_max=validated_constants.raw_values["PRICE_MAX"],
                    min_rel_volume=validated_constants.raw_values[
                        "MIN_REL_VOLUME"
                    ],
                    min_abs_pct_change=validated_constants.raw_values[
                        "MIN_ABS_PCT_CHANGE"
                    ],
                    top_n=top_n,
                ),
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    else:
        print(f"Scan rows: {len(rows)} returned of {total_items} total matches"
              f"{'; ' + str(skipped_fields) + ' rows skipped (missing fields)' if skipped_fields else ''}. "
              f"{len(survivors)} passed all filters; working list = top {len(working)} by relative volume.")
        print()
        fmt = "{:>4} {:<7} {:>9} {:>10} {:>9} {:>14}"
        print(fmt.format("Rank", "Symbol", "Last", "RelVol", "Day%", "Volume"))
        print("-" * 60)
        for i, s in enumerate(working, 1):
            print(fmt.format(i, s["symbol"], f"${s['last']:.3f}", f"{s['rel_volume']:.2f}x",
                             f"{s['day_pct_change']:+.2f}%", f"{s['volume']:,.0f}"))
        if not working:
            print("(empty working list — market closed or nothing qualified)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
