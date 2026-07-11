#!/usr/bin/env python3
"""PriceBandScanner math (see tools/PriceBandScanner.md).

Consumes a RAW run_scan JSON result (the file the harness saves when the
response exceeds the context cap) and reports, per price band, how the day's
most-active stocks moved — in PERCENT, never raw dollars, so cheap and
expensive stocks compare fairly.

Usage:
  python3 tools/price_band_scanner.py --scan-file <run_scan result file>
      [--band-edges 1,5,15,30,60,100,200,400] [--json-out results.json]

Accepted --scan-file shapes: {"data":{"result":{...}}}, {"result":{...}},
or the bare result object. Rows missing Last or % Change are skipped and
counted. The scan's "% Change" column is a decimal fraction (0.0301 = 3.01%)
and is converted to percent here.
"""

import argparse
import json
import math
import statistics
import struct
import sys
import zlib


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


def band_label(low, high):
    if low is None:
        return f"< ${high:g}"
    if high is None:
        return f">= ${low:g}"
    return f"${low:g}-{high:g}"


FONT = {
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"),
    "3": (".###.", "#...#", "....#", "..##.", "....#", "#...#", ".###."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
    "A": (".###.", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#.###", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "...#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#.#.#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#.#.#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "%": ("##..#", "##..#", "...#.", "..#..", ".#...", "#..##", "#..##"),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", ".##..", ".##.."),
    ",": (".....", ".....", ".....", ".....", ".##..", "..#..", ".#..."),
    "<": ("...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."),
    ">": (".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "/": ("....#", "....#", "...#.", "..#..", ".#...", "#....", "#...."),
    ":": (".....", ".##..", ".##..", ".....", ".##..", ".##..", "....."),
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
}


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _oklch_hex(L, C, H):
    h = math.radians(H)
    a, b = C * math.cos(h), C * math.sin(h)
    l_ = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m_ = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s_ = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    r = 4.0767416621 * l_ - 3.3077115913 * m_ + 0.2309699292 * s_
    g = -1.2684380046 * l_ + 2.6097574011 * m_ - 0.3413193965 * s_
    bl = -0.0041960863 * l_ - 0.7034186147 * m_ + 1.7076147010 * s_
    if any(v < -0.005 or v > 1.005 for v in (r, g, bl)):
        return None
    def enc(c):
        c = max(0.0, min(1.0, c))
        c = 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1 / 2.4)) - 0.055
        return round(c * 255)
    return "#%02x%02x%02x" % (enc(r), enc(g), enc(bl))


def value_color(v):
    hue = 150 if v >= 0 else 25
    t = min(abs(v), 5.0) / 5.0
    L = 0.78 - t * (0.78 - 0.42)
    c = 0.12 if 0.45 <= L <= 0.70 else 0.10
    hx = None
    while hx is None and c > 0.02:
        hx = _oklch_hex(L, c, hue)
        c -= 0.01
    return hx or "#888888"


class Canvas:
    def __init__(self, w, h, bg="#fcfcfb"):
        self.w, self.h = w, h
        r, g, b = _hex_rgb(bg)
        self.px = bytearray(bytes((r, g, b)) * (w * h))

    def rect(self, x0, y0, x1, y1, color):
        r, g, b = _hex_rgb(color)
        x0, x1 = max(0, int(min(x0, x1))), min(self.w, int(max(x0, x1)))
        y0, y1 = max(0, int(min(y0, y1))), min(self.h, int(max(y0, y1)))
        for y in range(y0, y1):
            row = (y * self.w + x0) * 3
            self.px[row:row + (x1 - x0) * 3] = bytes((r, g, b)) * (x1 - x0)

    def text(self, x, y, s, color, scale=2):
        cx = int(x)
        for ch in s.upper():
            glyph = FONT.get(ch, FONT[" "])
            for gy, rowbits in enumerate(glyph):
                for gx, bit in enumerate(rowbits):
                    if bit == "#":
                        self.rect(cx + gx * scale, y + gy * scale,
                                  cx + (gx + 1) * scale, y + (gy + 1) * scale, color)
            cx += 6 * scale
        return cx

    def text_width(self, s, scale=2):
        return len(s) * 6 * scale

    def save(self, path):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3])
                       for y in range(self.h))
        def chunk(tag, data):
            c = tag + data
            return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c))
        png = (b"\x89PNG\r\n\x1a\n"
               + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 9))
               + chunk(b"IEND", b""))
        with open(path, "wb") as f:
            f.write(png)


INK = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASE = "#c3c2b7"


def render_chart(path, stats, rows_used, total_items, date_str, degenerate=False):
    rowh, left, right = 40, 250, 70
    top = 100 if degenerate else 78
    plot_w = 1000 - left - right
    h = top + rowh * len(stats) + 60
    cv = Canvas(1000, h)
    vals = [s["median_pct"] for s in stats if s["count"] > 0]
    lo = min(0.0, min(vals)) if vals else 0.0
    hi = max(0.0, max(vals)) if vals else 1.0
    lo, hi = math.floor(lo - 0.5), math.ceil(hi + 0.5)
    def X(v):
        return left + (v - lo) / (hi - lo) * plot_w
    cv.text(20, 16, f"PRICEBANDSCANNER {date_str}", INK, 2)
    cv.text(20, 36, f"MEDIAN % CHANGE BY PRICE BAND ({rows_used} OF {total_items} MOST-ACTIVE STOCKS)", MUTED, 2)
    cv.text(20, 56, "COLOR = MEDIAN MOVE: LIGHT = 0%, DARK = +/-5% (CLAMPED), GREEN = GAIN, RED = LOSS", MUTED, 2)
    if degenerate:
        cv.text(20, 78, "WARNING: DEGENERATE SAMPLE (MARKET CLOSED) - NOT ACTIVITY-RANKED", "#d03b3b", 2)
    step = max(1, round((hi - lo) / 8))
    t = lo
    while t <= hi:
        x = X(t)
        cv.rect(x, top - 6, x + 1, h - 40, BASE if t == 0 else GRID)
        lbl = f"{'+' if t > 0 else ''}{t}%"
        cv.text(x - cv.text_width(lbl, 2) / 2, h - 32, lbl, MUTED, 2)
        t += step
    for i, s in enumerate(stats):
        y = top + i * rowh
        label = s["band"] + (f"  N={s['count']}" if s["count"] else "  N=0")
        if 0 < s["count"] < 5:
            label += " (THIN)"
        cv.text(20, y + 6, label, INK, 2)
        if s["count"] == 0:
            cv.text(X(0) + 8, y + 6, "NO NAMES", MUTED, 2)
            continue
        v = s["median_pct"]
        x0, x1 = X(min(0.0, v)), X(max(0.0, v))
        cv.rect(x0, y + 2, x1 if x1 > x0 + 1 else x0 + 2, y + 22, value_color(v))
        vlbl = f"{'+' if v > 0 else ''}{v:.2f}%"
        if v >= 0:
            cv.text(x1 + 8, y + 6, vlbl, MUTED, 2)
        else:
            lx = x0 - 8 - cv.text_width(vlbl, 2)
            if lx < left + 4:
                cv.text(X(0) + 8, y + 6, vlbl, MUTED, 2)
            else:
                cv.text(lx, y + 6, vlbl, MUTED, 2)
    cv.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scan-file", required=True, help="raw run_scan JSON result file")
    ap.add_argument("--band-edges", default="1,2.5,5,10,15,30,60,100,200,400",
                    help="comma-separated ascending price edges (default: 1,2.5,5,10,15,30,60,100,200,400)")
    ap.add_argument("--json-out", help="optional path for machine-readable results")
    ap.add_argument("--chart-out", help="optional path for a PNG chart of the band medians (pure stdlib, no dependencies)")
    ap.add_argument("--chart-date", default="", help="date string shown in the chart title (e.g. 2026-07-10)")
    args = ap.parse_args()

    edges = sorted(float(e) for e in args.band_edges.split(",") if e.strip())
    if not edges:
        print("ERROR: --band-edges produced no edges", file=sys.stderr)
        return 2
    # bands: (<e0), (e0-e1), ..., (>=eN)
    bands = [(None, edges[0])] + list(zip(edges, edges[1:])) + [(edges[-1], None)]

    result = load_result(args.scan_file)
    rows = result.get("results", [])
    total_items = result.get("total_items", len(rows))

    rel_vols = []
    for row in rows:
        try:
            rel_vols.append(float(row.get("columns", {})["Relative volume"]))
        except (KeyError, TypeError, ValueError):
            pass
    max_rv = max(rel_vols) if rel_vols else None
    degenerate = max_rv is not None and max_rv < 1.5

    buckets = {b: [] for b in bands}
    skipped = 0
    for row in rows:
        cols = row.get("columns", {})
        try:
            last = float(cols["Last"])
            pct = float(cols["% Change"]) * 100.0  # decimal fraction -> percent
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        sym = row.get("ticker") or cols.get("Symbol", "?")
        for low, high in bands:
            if (low is None or last >= low) and (high is None or last < high):
                buckets[(low, high)].append((sym, pct))
                break

    stats = []
    for band in bands:
        members = buckets[band]
        if not members:
            stats.append({"band": band_label(*band), "count": 0})
            continue
        pcts = [p for _, p in members]
        best = max(members, key=lambda m: m[1])
        worst = min(members, key=lambda m: m[1])
        stats.append({
            "band": band_label(*band),
            "count": len(members),
            "median_pct": statistics.median(pcts),
            "mean_pct": statistics.fmean(pcts),
            "pct_positive": 100.0 * sum(1 for p in pcts if p > 0) / len(pcts),
            "best": f"{best[0]} {best[1]:+.1f}%",
            "worst": f"{worst[0]} {worst[1]:+.1f}%",
        })

    print(f"Sample: {len(rows)} rows returned of {total_items} total scan matches"
          f" (top of the scan's sort order){'; ' + str(skipped) + ' rows skipped (missing fields)' if skipped else ''}")
    if degenerate:
        print(f"WARNING: DEGENERATE SAMPLE - max relative volume in the sample is {max_rv:.2f} "
              "(~1 means the market is closed or the day has rolled over). The scan's "
              "relative-volume sort is meaningless right now, so these rows are NOT the day's "
              "most-active names (ties often fall alphabetical). Treat this run's band "
              "comparison as invalid.")
    print()
    fmt = "{:<12} {:>6} {:>9} {:>9} {:>7}   {:<18} {:<18}"
    print(fmt.format("Band", "Count", "Median%", "Mean%", "Pos%", "Best", "Worst"))
    print("-" * 88)
    for s in stats:
        if s["count"] == 0:
            print(fmt.format(s["band"], 0, "-", "-", "-", "-", "-"))
        else:
            print(fmt.format(s["band"], s["count"], f"{s['median_pct']:+.2f}", f"{s['mean_pct']:+.2f}",
                             f"{s['pct_positive']:.0f}", s["best"], s["worst"]))

    ranked = sorted((s for s in stats if s["count"] > 0), key=lambda s: s["median_pct"], reverse=True)
    if ranked:
        print()
        print("Most growth (by median % change): " +
              ", ".join(f"{s['band']} ({s['median_pct']:+.2f}%, n={s['count']})" for s in ranked[:3]))
        print("Most losses (by median % change): " +
              ", ".join(f"{s['band']} ({s['median_pct']:+.2f}%, n={s['count']})" for s in ranked[::-1][:3]))

    if args.chart_out:
        render_chart(args.chart_out, stats, len(rows) - skipped, total_items, args.chart_date, degenerate)
        print(f"\nChart written to {args.chart_out}")

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"total_items": total_items, "rows_used": len(rows) - skipped,
                       "rows_skipped": skipped, "max_relative_volume": max_rv,
                       "degenerate_sample": degenerate, "bands": stats}, f, indent=2)
        print(f"\nJSON written to {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
