#!/usr/bin/env python3
"""Candidate math for the Robinhood momentum routine (see
robinhood-momentum-routine-autonomous.md, Step 8).

Consumes RAW get_equity_historicals JSON responses — do not transcribe bars
by hand. Computes, per symbol:
  - liquidity floor: median daily dollar volume (volume x close) over the last
    --volume-lookback-days bars; interpolated bars count as $0 days. A symbol
    with fewer than either configured history window is blocked before math.
  - recent high: max high_price over the last --high-lookback-days bars,
    REAL bars only (interpolated bars are placeholder prices nobody paid)
  - % below high vs the current price, and the dip-entry verdict
  - bid/ask spread gate (optional, --max-spread-buy-pct): rejects a name whose
    quoted spread is too wide to exit cleanly. A spread approaching
    STOP_LOSS_PCT puts the protective stop at the bid the moment it is
    placed, so the position is stopped out on arrival.

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
      [--max-spread-buy-pct 2.0] [--json-out results.json]

--bars files: raw get_equity_historicals responses. Accepted shapes:
  {"data": {"results": [...]}}   (full tool response)
  {"results": [...]}             (data envelope)
  [...]                          (bare results list)
--rsi-file: one or more files, each a RAW get_equity_technical_indicators
  response saved verbatim (the symbol is read from data.symbol, so responses
  are never re-keyed or retyped). A symbol-keyed map is still accepted.
--quotes file: JSON map of SYMBOL -> current price, e.g.
  {"FISN": 9.843, "TTRX": "7.84"}
  To enable the spread gate, give an object per symbol carrying bid and ask —
  either plain keys, or the raw quote object copied verbatim out of a
  get_equity_quotes response (no hand-transcribing needed):
  {"GRDX": {"price": 3.08, "bid": 2.97, "ask": 3.09}}
  {"GRDX": {"last_trade_price": "3.08", "bid_price": "2.97", "ask_price": "3.09"}}
All four constant flags are REQUIRED so values always come from the routine
document's Constants table — no silent stale defaults.

When --json-out is used, the root includes schema_version and
rsi_gate_enabled. The routine uses the latter to distinguish the exploratory
pre-RSI pass from the final RSI-enforced gate record.
"""

import argparse
import json
import math
import statistics
import sys


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


def finite_float_arg(value):
    try:
        return finite_float(value, "argument")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def load_json(path):
    """Strict json.load -- deliberately. Inline tool responses reach files via
    an LLM's Write call, i.e. re-generated token by token, and the observed
    slips are MID-document (2026-07-28: a missing ] at 11:06, an extra }
    BEFORE a closing ] at 12:36) -- shapes no parser can safely recover. A
    lenient trailing-garbage reader was built and rejected the same day: it
    covered only a case that has never occurred. The routine validates every
    authored file with json.load BEFORE invoking this script; a malformed
    file that reaches here must fail loudly, not be guessed at. Non-finite JSON
    constants are rejected here; numeric strings are checked at their field."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f, parse_constant=_reject_nonfinite_json)


def load_results(path):
    doc = load_json(path)
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        if "results" in doc:
            return doc["results"]
        if "data" in doc and isinstance(doc["data"], dict) and "results" in doc["data"]:
            return doc["data"]["results"]
    raise ValueError(f"{path}: unrecognized shape - expected a get_equity_historicals response")


def parse_quote(sym, val):
    """Returns (price, bid, ask); bid/ask are None when not supplied. Accepts a
    bare number/string, or an object using price/bid/ask or the raw API's
    last_trade_price/bid_price/ask_price."""
    if not isinstance(val, dict):
        return finite_float(val, f"{sym}: quote price"), None, None
    price = val.get("price", val.get("last_trade_price"))
    if price is None:
        raise ValueError(f"{sym}: quote object has no price / last_trade_price")
    bid = val.get("bid", val.get("bid_price"))
    ask = val.get("ask", val.get("ask_price"))
    return (finite_float(price, f"{sym}: quote price"),
            finite_float(bid, f"{sym}: bid") if bid is not None else None,
            finite_float(ask, f"{sym}: ask") if ask is not None else None)


def spread_gate(bid, ask, max_pct):
    """Returns (passes, reason, spread_pct) using the relative spread
    (ask - bid) / mid. Missing or nonsensical quote data BLOCKS, matching the
    RSI gate: a name we cannot price is a name we cannot promise to exit."""
    if bid is None or ask is None:
        return False, "spread gate: no bid/ask in --quotes - blocked", None
    try:
        finite = all(math.isfinite(value) for value in (bid, ask, max_pct))
    except TypeError:
        finite = False
    if not finite:
        return False, "spread gate: non-finite quote or threshold - blocked", None
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, f"spread gate: unusable quote (bid {bid:g}, ask {ask:g}) - blocked", None
    # rounded so a spread sitting exactly on the threshold cannot be rejected by
    # binary-float noise (4.04 - 3.96 is 0.08000000000000007)
    mid = bid / 2.0 + ask / 2.0
    pct = round((ask - bid) / mid * 100.0, 6)
    if not math.isfinite(pct):
        return False, "spread gate: non-finite computed spread - blocked", None
    if pct > max_pct:
        return False, (f"spread gate: {pct:.2f}% wide (bid ${bid:.4f} / ask ${ask:.4f}) "
                       f"> max {max_pct:g}%"), pct
    return True, f"spread {pct:.2f}% <= {max_pct:g}%", pct


def wilder_rsi(closes, period=14):
    closes = [finite_float(c, "RSI close") for c in closes]
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


def _rsi_series(val, period):
    """Ascending RSI values from any accepted per-symbol shape."""
    if isinstance(val, dict) and "data" in val:
        val = val["data"]
    if isinstance(val, dict) and "indicators" in val:
        val = val["indicators"][0]["series"]
    if isinstance(val, dict) and "rsi" in val:
        val = val["rsi"]
    if isinstance(val, dict) and "closes" in val:
        return wilder_rsi(val["closes"], period)
    if isinstance(val, list):
        return [finite_float(x["value"], "RSI value") if isinstance(x, dict)
                else finite_float(x, "RSI value") for x in val]
    return []


def load_rsi_map(paths, period):
    """Map SYMBOL -> ascending RSI values, merged across one or more files.

    Each file is EITHER a raw get_equity_technical_indicators response for a
    single symbol -- the symbol is read out of data.symbol, so responses are
    saved verbatim and nothing is ever re-keyed by hand -- OR a symbol-keyed
    map whose values are a raw response, a bare series, {"rsi": [...]}, or
    {"closes": [...]} (Wilder RSI computed here).

    The raw-file form exists because hand-assembling the keyed map is exactly
    what produced a malformed-JSON run failure; see INCIDENTS.md."""
    out = {}
    for path in ([paths] if isinstance(paths, str) else paths):
        doc = load_json(path)
        if isinstance(doc, dict) and isinstance(doc.get("data"), dict) and "symbol" in doc["data"]:
            doc = {doc["data"]["symbol"]: doc}          # raw single-symbol response
        for sym, val in doc.items():
            out[sym.upper()] = _rsi_series(val, period)
    return out


def rsi_gate(values, oversold, lookback, confirm, max_entry=None):
    """Returns (passes, reason). Deterministic curl check: min RSI over the
    last `lookback` values must be <= oversold, the CURRENT value must be at or
    below `max_entry`, AND the last `confirm` steps must each be rising.
    Missing/short data BLOCKS (conservative).

    `max_entry` closes the mirror image of the falling knife. The oversold test
    looks back `lookback` bars, so a touch that has already been fully reversed
    still satisfies it -- a name can leap from oversold to overbought inside the
    window and pass while rising. That is not catching a bounce, it is buying
    after the move is spent."""
    if not values or len(values) < max(lookback, confirm + 1):
        return False, f"RSI gate: no/insufficient data ({len(values or [])} values) - blocked"
    try:
        finite = all(math.isfinite(value) for value in values)
    except TypeError:
        finite = False
    if not finite:
        return False, "RSI gate: non-finite data - blocked"
    try:
        thresholds = (oversold,) + (() if max_entry is None else (max_entry,))
        finite_thresholds = all(math.isfinite(value) for value in thresholds)
    except TypeError:
        finite_thresholds = False
    if not finite_thresholds:
        return False, "RSI gate: non-finite threshold - blocked"
    window_min = min(values[-lookback:])
    if window_min > oversold:
        return False, f"RSI gate: never oversold (min {window_min:.1f} > {oversold:g})"
    if max_entry is not None and values[-1] > max_entry:
        return False, (f"RSI gate: bounce already run (now {values[-1]:.1f} > max entry "
                       f"{max_entry:g}; was oversold at {window_min:.1f})")
    for i in range(1, confirm + 1):
        if not values[-i] > values[-i - 1]:
            # name WHICH confirmation bar failed: "bar 1 of N" is a name still
            # dropping outright, "bar 2 of 2" is one whose latest bar ticked up
            # inside a longer fall - the case RSI_CONFIRM_BARS>1 exists to catch,
            # and the only way a report can show that this setting did the work
            return False, (f"RSI gate: still falling at confirm bar {i} of {confirm} "
                           f"({values[-i - 1]:.1f} -> {values[-i]:.1f})")
    return True, (f"RSI curl confirmed: min {window_min:.1f} <= {oversold:g}, "
                  f"rising {confirm} bar(s) {values[-confirm - 1]:.1f} -> {values[-1]:.1f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bars", nargs="+", required=True, help="raw get_equity_historicals JSON file(s)")
    ap.add_argument("--quotes", required=True, help="JSON map of SYMBOL -> current price")
    ap.add_argument("--volume-lookback-days", type=int, required=True)
    ap.add_argument("--high-lookback-days", type=int, required=True)
    ap.add_argument("--min-median-dollar-volume", type=finite_float_arg, required=True)
    ap.add_argument("--dip-entry-pct", type=finite_float_arg, required=True)
    ap.add_argument("--max-spread-buy-pct", type=finite_float_arg, help="MAX_SPREAD_BUY_PCT - enables the bid/ask spread gate; requires bid+ask in --quotes")
    ap.add_argument("--json-out", help="optional path for machine-readable results")
    ap.add_argument("--rsi-file", nargs="+", help="one or more RSI files - each a RAW get_equity_technical_indicators response (symbol read from it) or a symbol-keyed map; enables the RSI curl-up entry gate")
    ap.add_argument("--rsi-oversold", type=finite_float_arg, help="RSI_OVERSOLD (required with --rsi-file)")
    ap.add_argument("--rsi-lookback-bars", type=int, help="RSI_LOOKBACK_BARS (required with --rsi-file)")
    ap.add_argument("--rsi-confirm-bars", type=int, help="RSI_CONFIRM_BARS (required with --rsi-file)")
    ap.add_argument("--rsi-max-entry", type=finite_float_arg, help="RSI_MAX_ENTRY - highest CURRENT RSI still buyable (required with --rsi-file)")
    ap.add_argument("--rsi-period", type=int,
                    help="RSI_PERIOD, required with --rsi-file so the closes fallback uses the configured period")
    args = ap.parse_args()

    rsi_map = None
    if args.rsi_file:
        if (args.rsi_period is None or args.rsi_oversold is None
                or args.rsi_lookback_bars is None or args.rsi_confirm_bars is None
                or args.rsi_max_entry is None):
            ap.error("--rsi-file requires --rsi-period, --rsi-oversold, "
                     "--rsi-lookback-bars, --rsi-confirm-bars, and --rsi-max-entry")
        rsi_map = load_rsi_map(args.rsi_file, args.rsi_period)

    quotes = {sym.upper(): parse_quote(sym, val) for sym, val in load_json(args.quotes).items()}

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
        current, bid, ask = quotes[sym]
        required_history = max(args.volume_lookback_days, args.high_lookback_days)
        row = {"symbol": sym, "current_price": current, "buy_candidate": False,
               "median_dollar_volume": None, "recent_high": None,
               "pct_below_high": None, "skip_reason": None, "spread_pct": None,
               "insufficient_history": len(bars) < required_history}

        if row["insufficient_history"]:
            row["skip_reason"] = (
                f"insufficient history: {len(bars)} bars < required {required_history} "
                f"(volume lookback {args.volume_lookback_days}, "
                f"high lookback {args.high_lookback_days})"
            )
            rows.append(row)
            continue

        window = bars[-args.volume_lookback_days:]
        # interpolated bars carry volume 0, so they naturally contribute $0 days
        dollar_vols = []
        for b in window:
            volume = finite_float(b["volume"], f"{sym}: bar volume")
            close = finite_float(b["close_price"], f"{sym}: bar close")
            dollar_volume = volume * close
            if not math.isfinite(dollar_volume):
                raise ValueError(f"{sym}: bar dollar volume must be finite")
            dollar_vols.append(dollar_volume)
        row["median_dollar_volume"] = statistics.median(dollar_vols)
        if not math.isfinite(row["median_dollar_volume"]):
            raise ValueError(f"{sym}: median dollar volume must be finite")

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

        row["recent_high"] = max(finite_float(b["high_price"], f"{sym}: bar high")
                                 for b in real_recent)
        row["pct_below_high"] = (row["recent_high"] - current) / row["recent_high"] * 100.0
        if not math.isfinite(row["pct_below_high"]):
            raise ValueError(f"{sym}: percent below high must be finite")

        if row["pct_below_high"] > args.dip_entry_pct:
            # Spread first: it is free (the quote is already in hand) and a block
            # here saves the routine an RSI indicator call for this name.
            if args.max_spread_buy_pct is not None:
                ok, reason, pct = spread_gate(bid, ask, args.max_spread_buy_pct)
                row["spread_pct"] = pct
                row["spread_gate"] = "pass" if ok else "block"
                row["spread_reason"] = reason
                if not ok:
                    row["skip_reason"] = reason
                    rows.append(row)
                    continue

            if rsi_map is None:
                row["buy_candidate"] = True
                row["rsi_gate"] = "disabled"
            else:
                series = rsi_map.get(sym, [])
                ok, reason = rsi_gate(series, args.rsi_oversold, args.rsi_lookback_bars,
                                      args.rsi_confirm_bars, args.rsi_max_entry)
                row["rsi_gate"] = "pass" if ok else "block"
                row["rsi_reason"] = reason
                # the raw series, not just the verdict: spread_pct,
                # median_dollar_volume and pct_below_high are already numbers, so
                # their thresholds can be swept from a saved run. Storing these
                # makes RSI_OVERSOLD and RSI_CONFIRM_BARS equally answerable
                # after the fact, without re-fetching indicators.
                row["rsi_series"] = series
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

    fmt = "{:<7} {:>14} {:>9} {:>9} {:>8} {:>7} {}"
    print(fmt.format("Symbol", "Median $Vol", "5d High", "Current", "%Below", "Spread", "Verdict"))
    print("-" * 86)
    for r in rows:
        print(fmt.format(
            r["symbol"],
            "-" if r["median_dollar_volume"] is None else f"${r['median_dollar_volume']:,.0f}",
            "-" if r["recent_high"] is None else f"${r['recent_high']:.3f}",
            f"${r['current_price']:.3f}",
            "-" if r["pct_below_high"] is None else f"{r['pct_below_high']:+.2f}%",
            "-" if r["spread_pct"] is None else f"{r['spread_pct']:.2f}%",
            ("BUY CANDIDATE" if r["buy_candidate"] else f"SKIP ({r['skip_reason']})")
            + (" [insufficient history]" if r["insufficient_history"] else ""),
        ))
    print()
    print("Buy candidates:", [r["symbol"] for r in rows if r["buy_candidate"]] or "none")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "rsi_gate_enabled": rsi_map is not None,
                       "params": vars(args), "results": rows},
                      f, indent=2, allow_nan=False)
        print(f"JSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
