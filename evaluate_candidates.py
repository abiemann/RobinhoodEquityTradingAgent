#!/usr/bin/env python3
"""Candidate math for the Robinhood momentum routine (see
robinhood-momentum-routine-autonomous.md, Step 8).

Consumes saved get_equity_historicals tool results — do not transcribe bars
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

TESTED BY the full `tests/` suite — after ANY edit to this file, run
`python3 -m unittest discover -s tests` (Windows: `py -3 -m unittest discover -s tests`)
and require all tests to pass before committing. Expected values are
live-verified; if an intentional behavior change breaks one, update the
expectation deliberately — never delete a test to go green.

Usage:
  python evaluate_candidates.py --scratch <preflighted scratch> \
      (--bars hist1.json [hist2.json ...] | --bars-purpose KEY [KEY ...]) \
      (--quotes quotes1.json [quotes2.json ...] | --quotes-purpose KEY [KEY ...]) \
      [--expected-symbols SYMBOL [SYMBOL ...]] \
      --volume-lookback-days 20 --high-lookback-days 5 \
      --min-median-dollar-volume 175000 --dip-entry-pct 5 \
      [--max-spread-buy-pct 2.0] [--json-out results.json]

--bars files: saved get_equity_historicals results. Accepted shapes:
  standard MCP envelope containing structuredContent.data.results
  {"data": {"results": [...]}}   (full tool response)
  {"results": [...]}             (data envelope)
  [...]                          (bare results list)
--rsi-file/--rsi-purpose: one or more sources, each a RAW get_equity_technical_indicators
  response saved verbatim (the symbol is read from data.symbol, so responses
  are never re-keyed or retyped). A symbol-keyed map is still accepted.
--expected-symbols: the exact post-prefilter candidate set. When supplied, a
  requested symbol omitted from historicals or quotes is emitted as an
  explicit blocked row instead of disappearing from the result intersection.
--quotes files: one or more saved get_equity_quotes results. Accepted raw shapes:
  standard MCP envelope containing structuredContent.data.results
  {"data": {"results": [{"quote": {...}, "close": {...}}]}}
  {"results": [{"quote": {...}, "close": {...}}]}
  The symbol is read from each result's quote.symbol; the complete response is
  consumed directly, so callers never need to build a derived quote map.
  Legacy JSON maps of SYMBOL -> current price remain accepted, e.g.
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

from broker_snapshot import (
    validate_bound_external_json_purposes,
    validate_bound_external_json_sources,
)


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


def unwrap_tool_result(doc, path):
    """Remove one known MCP envelope without transforming broker data."""
    if isinstance(doc, dict) and "isError" in doc:
        if not isinstance(doc["isError"], bool):
            raise ValueError(f"{path}: isError: expected a boolean")
        if doc["isError"]:
            raise ValueError(f"{path}: broker tool result reports an error")
    if isinstance(doc, dict) and "structuredContent" in doc:
        structured = doc["structuredContent"]
        if not isinstance(structured, dict):
            raise ValueError(f"{path}: structuredContent: expected an object")
        return structured
    return doc


def load_results(doc, path):
    doc = unwrap_tool_result(doc, path)
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


def load_quotes(doc, path):
    """Return SYMBOL -> parsed quote from a complete saved quote response.

    The current connector returns rows at ``data.results[]`` with the ticker
    nested at ``row.quote.symbol``.  Accepting that response directly removes
    the model-authored re-keying step that can silently turn valid quotes into
    an empty map.  The older symbol-keyed map remains supported for existing
    callers.
    """
    doc = unwrap_tool_result(doc, path)
    if not isinstance(doc, dict):
        raise ValueError(
            f"{path}: unrecognized shape - expected a get_equity_quotes "
            "response or symbol-keyed quote map"
        )

    has_results = False
    results = None
    if "data" in doc:
        data = doc["data"]
        if not isinstance(data, dict):
            raise ValueError(f"{path}: data: expected an object")
        if "results" not in data:
            raise ValueError(f"{path}: data.results: missing")
        has_results = True
        results = data["results"]
    elif "results" in doc:
        has_results = True
        results = doc["results"]

    if not has_results:
        if any(key in doc for key in ("content", "structuredContent", "isError")):
            raise ValueError(
                f"{path}: unrecognized tool envelope - expected quote results"
            )
        return {
            sym.upper(): parse_quote(sym, value)
            for sym, value in doc.items()
        }

    if results is None:
        results = []
    if not isinstance(results, list):
        raise ValueError(f"{path}: quote results: expected an array or null")

    quotes = {}
    for index, value in enumerate(results, 1):
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{path}: quote result {index}: expected an object")
        quote = value.get("quote")
        if not isinstance(quote, dict):
            raise ValueError(
                f"{path}: quote result {index}.quote: expected an object"
            )
        symbol = quote.get("symbol")
        context = f"{path}: quote result {index}.quote.symbol"
        if (
            not isinstance(symbol, str)
            or not symbol
            or symbol != symbol.strip()
            or any(character.isspace() for character in symbol)
        ):
            raise ValueError(f"{context}: expected a nonempty ticker string")
        symbol = symbol.upper()
        if symbol in quotes:
            raise ValueError(f"{path}: duplicate quote result for {symbol}")
        quotes[symbol] = parse_quote(symbol, quote)
    return quotes


def load_quote_documents(documents):
    """Merge one or more complete quote responses without re-keying them.

    The connector limits a quote request to a bounded symbol batch.  Keeping
    each complete response in its own file preserves that boundary while this
    deterministic merge rejects cross-batch duplicates.
    """
    merged = {}
    for path, document in documents:
        batch = load_quotes(document, path)
        duplicate = sorted(set(merged) & set(batch))
        if duplicate:
            raise ValueError(
                f"{path}: duplicate quote result across files for "
                + ", ".join(duplicate)
            )
        merged.update(batch)
    return merged


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


def _rsi_symbol(value, context):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{context}: expected a nonempty ticker string")
    return value.upper()


def _rsi_values(series, context):
    if not isinstance(series, list):
        raise ValueError(f"{context}: expected an array")
    values = []
    for index, item in enumerate(series, 1):
        if isinstance(item, dict):
            if "value" not in item:
                raise ValueError(f"{context} item {index}: missing value")
            item = item["value"]
        if isinstance(item, bool):
            raise ValueError(f"{context} item {index}: invalid number")
        values.append(finite_float(item, f"{context} item {index}"))
    return values


def _indicator_rsi_series(value, context):
    if not isinstance(value, dict):
        raise ValueError(f"{context}: expected an object")
    indicators = value.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        raise ValueError(f"{context}.indicators: expected a nonempty array")

    rsi_indicators = []
    for index, indicator in enumerate(indicators, 1):
        if not isinstance(indicator, dict):
            raise ValueError(f"{context}.indicators item {index}: expected an object")
        indicator_type = indicator.get("type")
        if indicator_type is None or indicator_type == "rsi":
            rsi_indicators.append((index, indicator))
    if len(rsi_indicators) != 1:
        raise ValueError(
            f"{context}.indicators: expected exactly one RSI indicator"
        )
    index, indicator = rsi_indicators[0]
    if "series" not in indicator:
        raise ValueError(
            f"{context}.indicators item {index}.series: missing"
        )
    return _rsi_values(
        indicator["series"], f"{context}.indicators item {index}.series"
    )


def _rsi_series(value, period, context):
    """Ascending RSI values from one accepted legacy per-symbol shape."""
    if isinstance(value, dict) and "data" in value:
        data = value["data"]
        return _indicator_rsi_series(data, f"{context}.data")
    if isinstance(value, dict) and "indicators" in value:
        return _indicator_rsi_series(value, context)
    if isinstance(value, dict) and "rsi" in value:
        return _rsi_values(value["rsi"], f"{context}.rsi")
    if isinstance(value, dict) and "closes" in value:
        closes = value["closes"]
        if not isinstance(closes, list):
            raise ValueError(f"{context}.closes: expected an array")
        return wilder_rsi(closes, period)
    if isinstance(value, list):
        return _rsi_values(value, context)
    raise ValueError(
        f"{context}: expected a raw RSI response, RSI array, rsi object, "
        "or closes fallback"
    )


def load_rsi_map(documents, period):
    """Map SYMBOL -> ascending RSI values, merged across one or more files.

    Each file is EITHER a complete MCP envelope or raw
    get_equity_technical_indicators response for one symbol -- the symbol is
    read out of data.symbol, so responses are saved verbatim and nothing is
    re-keyed by hand -- OR a legacy symbol-keyed map whose values are a raw
    response, bare series, {"rsi": [...]}, or {"closes": [...]} (Wilder RSI
    computed here). Raw response structure and cross-input symbol uniqueness
    are validated before any gate math.

    The raw-file form exists because hand-assembling the keyed map is exactly
    what produced a malformed-JSON run failure; see INCIDENTS.md."""
    out = {}
    for path, original in documents:
        doc = unwrap_tool_result(original, path)
        if not isinstance(doc, dict):
            raise ValueError(
                f"{path}: unrecognized shape - expected a "
                "get_equity_technical_indicators response or symbol-keyed map"
            )
        if any(key in doc for key in ("content", "structuredContent", "isError")):
            raise ValueError(
                f"{path}: unrecognized tool envelope - expected RSI data"
            )

        entries = []
        if "data" in doc:
            data = doc["data"]
            if not isinstance(data, dict):
                raise ValueError(f"{path}: data: expected an object")
            if "symbol" not in data:
                raise ValueError(f"{path}: data.symbol: missing")
            symbol = _rsi_symbol(data["symbol"], f"{path}: data.symbol")
            series = _indicator_rsi_series(data, f"{path}: data")
            entries.append((symbol, series))
        else:
            for symbol_value, value in doc.items():
                symbol = _rsi_symbol(symbol_value, f"{path}: symbol")
                if isinstance(value, dict) and "data" in value:
                    nested = value["data"]
                    if not isinstance(nested, dict):
                        raise ValueError(
                            f"{path}: {symbol}.data: expected an object"
                        )
                    if "symbol" in nested:
                        nested_symbol = _rsi_symbol(
                            nested["symbol"], f"{path}: {symbol}.data.symbol"
                        )
                        if nested_symbol != symbol:
                            raise ValueError(
                                f"{path}: {symbol}: nested RSI symbol is "
                                f"{nested_symbol}"
                            )
                entries.append(
                    (symbol, _rsi_series(value, period, f"{path}: {symbol}"))
                )

        for symbol, series in entries:
            if symbol in out:
                raise ValueError(
                    f"{path}: duplicate RSI symbol across inputs for {symbol}"
                )
            out[symbol] = series
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
    ap.add_argument("--scratch", required=True,
                    help="absolute preflighted scratch whose transport binding owns every input")
    bars_input = ap.add_mutually_exclusive_group(required=True)
    bars_input.add_argument(
        "--bars", nargs="+", help="raw get_equity_historicals JSON file(s)"
    )
    bars_input.add_argument(
        "--bars-purpose",
        nargs="+",
        action="extend",
        help="committed response-source purpose(s) containing historicals",
    )
    quotes_input = ap.add_mutually_exclusive_group(required=True)
    quotes_input.add_argument(
        "--quotes",
        nargs="+",
        help="one or more saved get_equity_quotes responses (legacy SYMBOL -> quote maps also accepted)",
    )
    quotes_input.add_argument(
        "--quotes-purpose",
        nargs="+",
        action="extend",
        help="committed response-source purpose(s) containing quotes",
    )
    ap.add_argument(
        "--expected-symbols",
        nargs="+",
        action="extend",
        help=(
            "exact post-prefilter symbols; missing historical/quote inputs "
            "become explicit blocked rows"
        ),
    )
    ap.add_argument("--volume-lookback-days", type=int, required=True)
    ap.add_argument("--high-lookback-days", type=int, required=True)
    ap.add_argument("--min-median-dollar-volume", type=finite_float_arg, required=True)
    ap.add_argument("--dip-entry-pct", type=finite_float_arg, required=True)
    ap.add_argument("--max-spread-buy-pct", type=finite_float_arg, help="MAX_SPREAD_BUY_PCT - enables the bid/ask spread gate; requires bid+ask in quote inputs")
    ap.add_argument("--json-out", help="optional path for machine-readable results")
    rsi_input = ap.add_mutually_exclusive_group()
    rsi_input.add_argument("--rsi-file", nargs="+", help="one or more RSI files - each a RAW get_equity_technical_indicators response (symbol read from it) or a symbol-keyed map; enables the RSI curl-up entry gate")
    rsi_input.add_argument(
        "--rsi-purpose",
        nargs="+",
        action="extend",
        help="committed response-source purpose(s) containing RSI responses; enables the RSI curl-up entry gate",
    )
    ap.add_argument("--rsi-oversold", type=finite_float_arg, help="RSI_OVERSOLD (required with an RSI input)")
    ap.add_argument("--rsi-lookback-bars", type=int, help="RSI_LOOKBACK_BARS (required with an RSI input)")
    ap.add_argument("--rsi-confirm-bars", type=int, help="RSI_CONFIRM_BARS (required with an RSI input)")
    ap.add_argument("--rsi-max-entry", type=finite_float_arg, help="RSI_MAX_ENTRY - highest CURRENT RSI still buyable (required with an RSI input)")
    ap.add_argument("--rsi-period", type=int,
                    help="RSI_PERIOD, required with an RSI input so the closes fallback uses the configured period")
    args = ap.parse_args()

    def reject_duplicates(values, option):
        if values is not None and len(values) != len(set(values)):
            ap.error(f"{option} must list every value exactly once")

    reject_duplicates(args.bars_purpose, "--bars-purpose")
    reject_duplicates(args.quotes_purpose, "--quotes-purpose")
    reject_duplicates(args.rsi_purpose, "--rsi-purpose")
    if args.expected_symbols is not None:
        args.expected_symbols = [
            _rsi_symbol(value, "--expected-symbols")
            for value in args.expected_symbols
        ]
        reject_duplicates(args.expected_symbols, "--expected-symbols")

    def resolve_inputs(paths, purposes):
        if purposes is not None:
            validated = validate_bound_external_json_purposes(
                args.scratch, purposes
            )
            labels = purposes
        else:
            validated = validate_bound_external_json_sources(args.scratch, paths)
            labels = paths
        return [
            (label, document)
            for label, (_path, document, _raw) in zip(labels, validated)
        ]

    bar_documents = resolve_inputs(args.bars, args.bars_purpose)
    quote_documents = resolve_inputs(args.quotes, args.quotes_purpose)
    rsi_documents = (
        resolve_inputs(args.rsi_file, args.rsi_purpose)
        if args.rsi_file is not None or args.rsi_purpose is not None
        else []
    )

    rsi_map = None
    if rsi_documents:
        if (args.rsi_period is None or args.rsi_oversold is None
                or args.rsi_lookback_bars is None or args.rsi_confirm_bars is None
                or args.rsi_max_entry is None):
            ap.error("--rsi-file/--rsi-purpose requires --rsi-period, --rsi-oversold, "
                     "--rsi-lookback-bars, --rsi-confirm-bars, and --rsi-max-entry")
        rsi_map = load_rsi_map(rsi_documents, args.rsi_period)

    quotes = load_quote_documents(quote_documents)

    bars_by_symbol = {}
    for path, document in bar_documents:
        for result in load_results(document, path):
            sym = result["symbol"].upper()
            if sym in bars_by_symbol:
                print(f"WARNING: {sym} appears in more than one --bars file; using the later one", file=sys.stderr)
            bars_by_symbol[sym] = sorted(result["bars"], key=lambda b: b["begins_at"])

    for sym in sorted(set(quotes) - set(bars_by_symbol)):
        print(f"WARNING: {sym} has a quote but no bars data", file=sys.stderr)
    for sym in sorted(set(bars_by_symbol) - set(quotes)):
        print(f"WARNING: {sym} has bars but no quote in --quotes", file=sys.stderr)

    if args.expected_symbols is not None:
        expected = set(args.expected_symbols)
        unexpected_bars = sorted(set(bars_by_symbol) - expected)
        unexpected_quotes = sorted(set(quotes) - expected)
        if unexpected_bars:
            raise ValueError(
                "historicals returned symbol(s) outside --expected-symbols: "
                + ", ".join(unexpected_bars)
            )
        if unexpected_quotes:
            raise ValueError(
                "quotes returned symbol(s) outside --expected-symbols: "
                + ", ".join(unexpected_quotes)
            )
        evaluation_symbols = sorted(expected)
    else:
        evaluation_symbols = sorted(set(bars_by_symbol) & set(quotes))

    rows = []
    required_history = max(args.volume_lookback_days, args.high_lookback_days)
    for sym in evaluation_symbols:
        bars = bars_by_symbol.get(sym)
        quote = quotes.get(sym)
        if bars is None or quote is None:
            missing = []
            if bars is None:
                missing.append("historicals")
            if quote is None:
                missing.append("quote")
            rows.append({
                "symbol": sym,
                "current_price": quote[0] if quote is not None else None,
                "buy_candidate": False,
                "median_dollar_volume": None,
                "recent_high": None,
                "pct_below_high": None,
                "skip_reason": "missing candidate input: " + " and ".join(missing),
                "spread_pct": None,
                "insufficient_history": bars is None or len(bars) < required_history,
            })
            continue

        current, bid, ask = quote
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
            "-" if r["current_price"] is None else f"${r['current_price']:.3f}",
            "-" if r["pct_below_high"] is None else f"{r['pct_below_high']:+.2f}%",
            "-" if r["spread_pct"] is None else f"{r['spread_pct']:.2f}%",
            ("BUY CANDIDATE" if r["buy_candidate"] else f"SKIP ({r['skip_reason']})")
            + (" [insufficient history]" if r["insufficient_history"] else ""),
        ))
    print()
    print("Buy candidates:", [r["symbol"] for r in rows if r["buy_candidate"]] or "none")

    if args.json_out:
        parameter_values = vars(args).copy()
        if args.bars_purpose is not None:
            parameter_values["bars"] = args.bars_purpose
        if args.quotes_purpose is not None:
            parameter_values["quotes"] = args.quotes_purpose
        if args.rsi_purpose is not None:
            parameter_values["rsi_file"] = args.rsi_purpose
        params = {
            key: value
            for key, value in parameter_values.items()
            if key not in {
                "scratch", "bars_purpose", "quotes_purpose", "rsi_purpose"
            }
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 1, "rsi_gate_enabled": rsi_map is not None,
                       "params": params, "results": rows},
                      f, indent=2, allow_nan=False)
        print(f"JSON written to {args.json_out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
