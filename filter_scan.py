#!/usr/bin/env python3
"""Working-list filter for the Robinhood momentum routine (Step 6 of
robinhood-momentum-routine-autonomous.md).

Consumes the RAW run_scan JSON result (the file the harness saves when the
response exceeds the context cap) and applies the routine's client-side
screen: price band, relative-volume floor, minimum absolute day move, then
ranks by relative volume and keeps the top N.

Usage:
  python3 filter_scan.py --scan-file <run_scan result file> \
      --price-min 2.50 --price-max 5 --min-rel-volume 2 \
      --min-abs-pct-change 3 --top-n 15 [--json-out working_list.json]

All five constant flags are REQUIRED so values always come from Constants.md
— no silent stale defaults.

Verified response schema (live 2026-07-06 → 2026-07-17; do NOT rediscover it
per run): {"data": {"result": {"results": [...], "total_items": N, ...}},
"guide": ...}. Each row: {"ticker": "XYZ", "instrument_id": ..., "columns":
{"Last": "4.45", "% Change": "0.1528", "Relative volume": "557.75",
"Volume": "20372901", "Symbol": "XYZ", ...}} — prices and volumes are
STRINGS, and "% Change" is a DECIMAL FRACTION (0.0301 = 3.01%), converted
to percent here. Rows missing needed fields are skipped and counted.

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
`python3 tests/test_scripts.py` (Windows: `py -3 tests\test_scripts.py`)
and require all tests to pass before committing. Expected values are
live-verified; if an intentional behavior change breaks one, update the
expectation deliberately — never delete a test to go green.
"""

import argparse
import json
import sys


def load_result(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
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
    ap.add_argument("--scan-file", required=True, help="raw run_scan JSON result file")
    ap.add_argument("--price-min", type=float, required=True)
    ap.add_argument("--price-max", type=float, required=True)
    ap.add_argument("--min-rel-volume", type=float, required=True)
    ap.add_argument("--min-abs-pct-change", type=float, required=True,
                    help="minimum absolute day move in PERCENT (scan emits a decimal fraction; converted here)")
    ap.add_argument("--top-n", type=int, required=True)
    ap.add_argument("--json-out", help="optional path for the machine-readable working list")
    args = ap.parse_args()

    result = load_result(args.scan_file)
    rows = result.get("results", [])
    total_items = result.get("total_items", len(rows))

    survivors = []
    skipped_fields = 0
    for row in rows:
        cols = row.get("columns", {})
        try:
            last = float(cols["Last"])
            rel_vol = float(cols["Relative volume"])
            pct = float(cols["% Change"]) * 100.0
            volume = float(cols.get("Volume", "0"))
        except (KeyError, TypeError, ValueError):
            skipped_fields += 1
            continue
        if not (args.price_min <= last <= args.price_max):
            continue
        if rel_vol < args.min_rel_volume:
            continue
        if abs(pct) < args.min_abs_pct_change:
            continue
        survivors.append({"symbol": row.get("ticker") or cols.get("Symbol", "?"),
                          "last": last, "rel_volume": rel_vol,
                          "day_pct_change": pct, "volume": volume})

    survivors.sort(key=lambda s: s["rel_volume"], reverse=True)
    working = survivors[:args.top_n]

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

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"total_items": total_items, "rows_returned": len(rows),
                       "rows_skipped": skipped_fields, "passed_filters": len(survivors),
                       "working_list": working}, f, indent=2)
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
