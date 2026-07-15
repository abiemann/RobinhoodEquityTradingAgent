#!/usr/bin/env python3
"""Candidate math for the Robinhood momentum routine (see
robinhood-momentum-routine-autonomous.md, Step 8).

Consumes RAW get_equity_historicals JSON responses — do not transcribe bars
by hand. Computes, per symbol:
  - liquidity floor: median daily dollar volume (volume x close) over the last
    --volume-lookback-days bars; interpolated bars count as $0 days
  - recent high: max high_price over the last --high-lookback-days bars,
    REAL bars only (interpolated bars are placeholder prices nobody paid)
  - % below high vs the current price, and the dip-entry verdict

The script does NOT know about held positions or open orders — overlay those
skips (Step 10 of the routine) on its output.

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
`python3 tests/test_scripts.py` (Windows: `py -3 tests\test_scripts.py`)
and require all tests to pass before committing. Expected values are
live-verified; if an intentional behavior change breaks one, update the
expectation deliberately — never delete a test to go green.

Usage:
  python evaluate_candidates.py --bars hist1.json [hist2.json ...] \
      --quotes quotes.json \
      --volume-lookback-days 20 --high-lookback-days 5 \
      --min-median-dollar-volume 175000 --dip-entry-pct 5 \
      [--json-out results.json]

--bars files: raw get_equity_historicals responses. Accepted shapes:
  {"data": {"results": [...]}}   (full tool response)
  {"results": [...]}             (data envelope)
  [...]                          (bare results list)
--quotes file: plain JSON map of SYMBOL -> current price, e.g.
  {"FISN": 9.843, "TTRX": "7.84"}
All four constant flags are REQUIRED so values always come from the routine
document's Constants table — no silent stale defaults.
"""

import argparse
import json
import statistics
import sys


def load_results(path):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        if "results" in doc:
            return doc["results"]
        if "data" in doc and isinstance(doc["data"], dict) and "results" in doc["data"]:
            return doc["data"]["results"]
    raise ValueError(f"{path}: unrecognized shape - expected a get_equity_historicals response")


def wilder_rsi(closes, period=14):
    closes = [float(c) for c in closes]
    if len(closes) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = []
    for i in range(period, len(gains) + 1):
        if i > period:
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out.append(100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss))
    return out


def load_rsi_map(path, period):
    """Map SYMBOL -> ascending RSI values. Accepts, per symbol: a raw
    get_equity_technical_indicators response, a bare series list, a plain
    {"rsi": [...]} list, or {"closes": [...]} (Wilder RSI computed here)."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    out = {}
    for sym, val in doc.items():
        if isinstance(val, dict) and "data" in val:
            val = val["data"]
        if isinstance(val, dict) and "indicators" in val:
            val = val["indicators"][0]["series"]
        if isinstance(val, dict) and "rsi" in val:
            val = val["rsi"]
        if isinstance(val, dict) and "closes" in val:
            out[sym.upper()] = wilder_rsi(val["closes"], period)
            continue
        if isinstance(val, list):
            out[sym.upper()] = [float(x["value"]) if isinstance(x, dict) else float(x) for x in val]
            continue
        out[sym.upper()] = []
    return out


def rsi_gate(values, oversold, lookback, confirm):
    """Returns (passes, reason). Deterministic curl check: min RSI over the
    last `lookback` values must be <= oversold, AND the last `confirm` steps
    must each be rising. Missing/short data BLOCKS (conservative)."""
    if not values or len(values) < max(lookback, confirm + 1):
        return False, f"RSI gate: no/insufficient data ({len(values or [])} values) - blocked"
    window_min = min(values[-lookback:])
    if window_min > oversold:
        return False, f"RSI gate: never oversold (min {window_min:.1f} > {oversold:g})"
    for i in range(1, confirm + 1):
        if not values[-i] > values[-i - 1]:
            return False, f"RSI gate: still falling ({values[-i - 1]:.1f} -> {values[-i]:.1f})"
    return True, (f"RSI curl confirmed: min {window_min:.1f} <= {oversold:g}, "
                  f"rising {values[-2]:.1f} -> {values[-1]:.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", nargs="+", required=True, help="raw get_equity_historicals JSON file(s)")
    ap.add_argument("--quotes", required=True, help="JSON map of SYMBOL -> current price")
    ap.add_argument("--volume-lookback-days", type=int, required=True)
    ap.add_argument("--high-lookback-days", type=int, required=True)
    ap.add_argument("--min-median-dollar-volume", type=float, required=True)
    ap.add_argument("--dip-entry-pct", type=float, required=True)
    ap.add_argument("--json-out", help="optional path for machine-readable results")
    ap.add_argument("--rsi-file", help="JSON map SYMBOL -> RSI data (raw indicator response, bare series, {'rsi': [...]}, or {'closes': [...]} fallback); enables the RSI curl-up entry gate")
    ap.add_argument("--rsi-oversold", type=float, help="RSI_OVERSOLD (required with --rsi-file)")
    ap.add_argument("--rsi-lookback-bars", type=int, help="RSI_LOOKBACK_BARS (required with --rsi-file)")
    ap.add_argument("--rsi-confirm-bars", type=int, help="RSI_CONFIRM_BARS (required with --rsi-file)")
    ap.add_argument("--rsi-period", type=int, default=14, help="RSI_PERIOD, used only for the closes fallback (default 14)")
    args = ap.parse_args()

    rsi_map = None
    if args.rsi_file:
        if args.rsi_oversold is None or args.rsi_lookback_bars is None or args.rsi_confirm_bars is None:
            ap.error("--rsi-file requires --rsi-oversold, --rsi-lookback-bars, and --rsi-confirm-bars")
        rsi_map = load_rsi_map(args.rsi_file, args.rsi_period)

    with open(args.quotes, "r", encoding="utf-8") as f:
        quotes = {sym.upper(): float(px) for sym, px in json.load(f).items()}

    bars_by_symbol = {}
    for path in args.bars:
        for result in load_results(path):
            sym = result["symbol"].upper()
            if sym in bars_by_symbol:
                print(f"WARNING: {sym} appears in more than one --bars file; using the later one", file=sys.stderr)
            bars_by_symbol[sym] = sorted(result["bars"], key=lambda b: b["begins_at"])

    for sym in sorted(set(quotes) - set(bars_by_symbol)):
        print(f"WARNING: {sym} has a quote but no bars data - not evaluated", file=sys.stderr)
    for sym in sorted(set(bars_by_symbol) - set(quotes)):
        print(f"WARNING: {sym} has bars but no quote in --quotes - not evaluated", file=sys.stderr)

    rows = []
    for sym in sorted(set(bars_by_symbol) & set(quotes)):
        bars = bars_by_symbol[sym]
        current = quotes[sym]
        row = {"symbol": sym, "current_price": current, "buy_candidate": False,
               "median_dollar_volume": None, "recent_high": None,
               "pct_below_high": None, "skip_reason": None,
               "insufficient_history": len(bars) < args.volume_lookback_days}

        window = bars[-args.volume_lookback_days:]
        # interpolated bars carry volume 0, so they naturally contribute $0 days
        dollar_vols = [float(b["volume"]) * float(b["close_price"]) for b in window]
        row["median_dollar_volume"] = statistics.median(dollar_vols)

        if row["median_dollar_volume"] < args.min_median_dollar_volume:
            row["skip_reason"] = (f"illiquid: median ${row['median_dollar_volume']:,.0f}/day "
                                  f"< floor ${args.min_median_dollar_volume:,.0f}")
            rows.append(row)
            continue

        real_recent = [b for b in bars[-args.high_lookback_days:] if not b.get("interpolated")]
        if not real_recent:
            row["skip_reason"] = f"no real (non-interpolated) bars in the last {args.high_lookback_days} bars"
            rows.append(row)
            continue

        row["recent_high"] = max(float(b["high_price"]) for b in real_recent)
        row["pct_below_high"] = (row["recent_high"] - current) / row["recent_high"] * 100.0

        if row["pct_below_high"] > args.dip_entry_pct:
            if rsi_map is None:
                row["buy_candidate"] = True
                row["rsi_gate"] = "disabled"
            else:
                ok, reason = rsi_gate(rsi_map.get(sym, []), args.rsi_oversold,
                                      args.rsi_lookback_bars, args.rsi_confirm_bars)
                row["rsi_gate"] = "pass" if ok else "block"
                row["rsi_reason"] = reason
                if ok:
                    row["buy_candidate"] = True
                else:
                    row["skip_reason"] = reason
        elif row["pct_below_high"] <= 0:
            row["skip_reason"] = "at or above recent high - not a dip"
        else:
            row["skip_reason"] = (f"only {row['pct_below_high']:.2f}% below high "
                                  f"(need >{args.dip_entry_pct:g}%)")
        rows.append(row)

    fmt = "{:<7} {:>14} {:>9} {:>9} {:>8} {}"
    print(fmt.format("Symbol", "Median $Vol", "5d High", "Current", "%Below", "Verdict"))
    print("-" * 78)
    for r in rows:
        print(fmt.format(
            r["symbol"],
            f"${r['median_dollar_volume']:,.0f}",
            "-" if r["recent_high"] is None else f"${r['recent_high']:.3f}",
            f"${r['current_price']:.3f}",
            "-" if r["pct_below_high"] is None else f"{r['pct_below_high']:+.2f}%",
            ("BUY CANDIDATE" if r["buy_candidate"] else f"SKIP ({r['skip_reason']})")
            + (" [insufficient history]" if r["insufficient_history"] else ""),
        ))
    print()
    print("Buy candidates:", [r["symbol"] for r in rows if r["buy_candidate"]] or "none")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"params": vars(args), "results": rows}, f, indent=2)
        print(f"JSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
