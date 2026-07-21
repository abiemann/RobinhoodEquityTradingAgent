#!/usr/bin/env python3
"""Regression tests for the two deterministic scripts.

Run:  py -3 tests/test_scripts.py   (or: python3 tests/test_scripts.py)

Stdlib only — no pytest, no fixtures on disk. Each test drives the real CLI
via subprocess and asserts on --json-out / --chart-out, so the scripts are
tested exactly as the agents invoke them. Expected values for FISN/TTRX were
verified against live API data on 2026-07-07.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALUATE = os.path.join(ROOT, "evaluate_candidates.py")
SCANNER = os.path.join(ROOT, "tools", "price_band_scanner.py")
FILTER = os.path.join(ROOT, "filter_scan.py")
CLOCK = os.path.join(ROOT, "market_clock.py")


def bar(date, close, high, volume, interpolated=False):
    b = {"begins_at": date + "T00:00:00Z", "open_price": str(close), "close_price": str(close),
         "high_price": str(high), "low_price": str(close), "volume": volume, "session": "reg"}
    if interpolated:
        b["interpolated"] = True
    return b


FISN_BARS = (
    [bar(f"2026-06-{d:02d}", 16.00, 16.00, 0, True) for d in (1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16, 17)]
    + [bar("2026-06-18", 14.56, 19.00, 2778991), bar("2026-06-22", 12.42, 15.50, 1609217),
       bar("2026-06-23", 12.44, 13.25, 805261), bar("2026-06-24", 9.76, 14.57, 1339008),
       bar("2026-06-25", 10.03, 10.4799, 823666), bar("2026-06-26", 10.75, 11.43, 591918),
       bar("2026-06-29", 11.05, 11.24, 277711), bar("2026-06-30", 11.04, 11.22, 231255),
       bar("2026-07-01", 10.49, 10.80, 238844), bar("2026-07-02", 10.29, 10.5899, 144588)]
)

TTRX_BARS = [
    bar("2026-06-01", 6.33, 6.75, 268969), bar("2026-06-02", 6.42, 6.51, 74623),
    bar("2026-06-03", 6.02, 6.30, 91501), bar("2026-06-04", 5.745, 6.03, 48226),
    bar("2026-06-05", 5.28, 5.8644, 50668), bar("2026-06-08", 5.33, 5.50, 21912),
    bar("2026-06-09", 5.055, 5.21, 43707), bar("2026-06-10", 5.53, 5.97, 61129),
    bar("2026-06-11", 5.86, 5.98, 29897), bar("2026-06-12", 6.045, 6.1899, 55854),
    bar("2026-06-15", 6.42, 6.60, 70528), bar("2026-06-16", 6.16, 6.56, 31630),
    bar("2026-06-17", 6.03, 6.5798, 45236), bar("2026-06-18", 5.87, 6.37, 36642),
    bar("2026-06-22", 5.77, 5.93, 18529), bar("2026-06-23", 5.99, 5.99, 10098),
    bar("2026-06-24", 6.09, 6.15, 28340), bar("2026-06-25", 6.82, 6.98, 49022),
    bar("2026-06-26", 6.93, 6.98, 113337), bar("2026-06-29", 6.91, 6.95, 28558),
    bar("2026-06-30", 7.30, 7.39, 59162), bar("2026-07-01", 6.85, 7.36, 51365),
    bar("2026-07-02", 7.12, 7.32, 20642),
]


def run_cli(script, args):
    proc = subprocess.run([sys.executable, script] + args, capture_output=True, text=True, cwd=ROOT)
    if proc.returncode != 0:
        raise AssertionError(f"{os.path.basename(script)} exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout


class EvaluateCandidatesTests(unittest.TestCase):
    def run_eval(self, hist_payload, quotes, extra=None):
        with tempfile.TemporaryDirectory() as td:
            hist = os.path.join(td, "hist.json")
            qts = os.path.join(td, "quotes.json")
            out = os.path.join(td, "out.json")
            with open(hist, "w", encoding="utf-8") as f:
                json.dump(hist_payload, f)
            with open(qts, "w", encoding="utf-8") as f:
                json.dump(quotes, f)
            run_cli(EVALUATE, ["--bars", hist, "--quotes", qts,
                               "--volume-lookback-days", "20", "--high-lookback-days", "5",
                               "--min-median-dollar-volume", "175000", "--dip-entry-pct", "5",
                               "--json-out", out] + (extra or []))
            with open(out, encoding="utf-8") as f:
                return {r["symbol"]: r for r in json.load(f)["results"]}

    def test_live_verified_fisn_ttrx(self):
        payload = {"data": {"results": [{"symbol": "FISN", "bars": FISN_BARS},
                                        {"symbol": "TTRX", "bars": TTRX_BARS}]}}
        res = self.run_eval(payload, {"FISN": 9.843, "TTRX": "7.84"})
        fisn = res["FISN"]
        self.assertAlmostEqual(fisn["median_dollar_volume"], 743905.26, delta=1.0)
        self.assertAlmostEqual(fisn["recent_high"], 11.43, delta=0.001)
        self.assertAlmostEqual(fisn["pct_below_high"], 13.88, delta=0.01)
        self.assertTrue(fisn["buy_candidate"])
        ttrx = res["TTRX"]
        self.assertAlmostEqual(ttrx["recent_high"], 7.39, delta=0.001)
        self.assertFalse(ttrx["buy_candidate"])
        self.assertIn("above", ttrx["skip_reason"])
        self.assertLess(ttrx["pct_below_high"], 0)

    def test_interpolated_bars_excluded_from_high(self):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 99.0, 0, True), bar("2026-07-06", 4.6, 99.0, 0, True),
                bar("2026-07-07", 4.6, 99.0, 0, True)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        res = self.run_eval(payload, {"SYNX": 4.0},
                            extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                   "--min-median-dollar-volume", "0"])
        wait = res["SYNX"]
        self.assertAlmostEqual(wait["recent_high"], 5.0, delta=0.001)
        self.assertTrue(wait["buy_candidate"])

    def test_all_interpolated_high_window_skips(self):
        bars = [bar(f"2026-07-{d:02d}", 4.6, 99.0, 0, True) for d in (1, 2, 3, 6, 7)]
        payload = {"results": [{"symbol": "GHST", "bars": bars}]}
        res = self.run_eval(payload, {"GHST": 4.0},
                            extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                   "--min-median-dollar-volume", "0"])
        self.assertFalse(res["GHST"]["buy_candidate"])
        self.assertIn("no real", res["GHST"]["skip_reason"])

    def run_eval_rsi(self, rsi_payload):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        with tempfile.TemporaryDirectory() as td:
            rsi_path = os.path.join(td, "rsi.json")
            with open(rsi_path, "w", encoding="utf-8") as f:
                json.dump(rsi_payload, f)
            return self.run_eval(payload, {"SYNX": 4.0},
                                 extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                        "--min-median-dollar-volume", "0",
                                        "--rsi-file", rsi_path, "--rsi-oversold", "35",
                                        "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1"])["SYNX"]

    def test_rsi_gate_blocks_falling_knife(self):
        res = self.run_eval_rsi({"SYNX": {"rsi": [42, 39, 36, 33, 31, 29]}})
        self.assertEqual(res["rsi_gate"], "block")
        self.assertFalse(res["buy_candidate"])
        self.assertIn("still falling", res["rsi_reason"])

    def test_rsi_gate_passes_oversold_curl(self):
        res = self.run_eval_rsi({"SYNX": {"rsi": [40, 36, 33, 30, 29, 34]}})
        self.assertEqual(res["rsi_gate"], "pass")
        self.assertTrue(res["buy_candidate"])
        self.assertIn("curl confirmed", res["rsi_reason"])

    def test_rsi_gate_blocks_never_oversold(self):
        res = self.run_eval_rsi({"SYNX": {"rsi": [55, 52, 50, 48, 47, 49]}})
        self.assertEqual(res["rsi_gate"], "block")
        self.assertFalse(res["buy_candidate"])
        self.assertIn("never oversold", res["rsi_reason"])

    def test_rsi_gate_blocks_missing_data(self):
        res = self.run_eval_rsi({})
        self.assertEqual(res["rsi_gate"], "block")
        self.assertFalse(res["buy_candidate"])
        self.assertIn("no/insufficient data", res["rsi_reason"])

    def test_rsi_closes_fallback_wilder(self):
        falling = list(range(60, 40, -1))
        rising_tail = falling + [41, 42.5]
        res = self.run_eval_rsi({"SYNX": {"closes": rising_tail}})
        self.assertEqual(res["rsi_gate"], "pass")
        self.assertTrue(res["buy_candidate"])

    def test_liquidity_floor_skips(self):
        bars = [bar(f"2026-07-{d:02d}", 4.5, 5.0, 100) for d in (1, 2, 3, 6, 7)]
        payload = {"results": [{"symbol": "THIN", "bars": bars}]}
        res = self.run_eval(payload, {"THIN": 4.0},
                            extra=["--volume-lookback-days", "5", "--high-lookback-days", "5"])
        self.assertFalse(res["THIN"]["buy_candidate"])
        self.assertIn("illiquid", res["THIN"]["skip_reason"])


def scan_row(sym, last, pct_change, rel_vol):
    return {"ticker": sym, "columns": {"Last": str(last), "% Change": str(pct_change),
                                       "Relative volume": str(rel_vol), "Symbol": sym, "Volume": "1000"}}


class PriceBandScannerTests(unittest.TestCase):
    def run_scan(self, rows, chart=False):
        with tempfile.TemporaryDirectory() as td:
            scan = os.path.join(td, "scan.json")
            out = os.path.join(td, "out.json")
            png = os.path.join(td, "chart.png")
            with open(scan, "w", encoding="utf-8") as f:
                json.dump({"data": {"result": {"results": rows, "total_items": len(rows)}}}, f)
            args = ["--scan-file", scan, "--band-edges", "1,2.5,5,10,15,30,60,100,200,400",
                    "--json-out", out]
            if chart:
                args += ["--chart-out", png, "--chart-date", "TEST"]
            run_cli(SCANNER, args)
            with open(out, encoding="utf-8") as f:
                data = json.load(f)
            png_bytes = None
            if chart:
                with open(png, "rb") as f:
                    png_bytes = f.read()
            return data, png_bytes

    def test_banding_medians_and_conversion(self):
        rows = [scan_row("AAA", 3.0, 0.05, 250.0), scan_row("BBB", 3.5, -0.01, 12.0),
                scan_row("CCC", 4.0, 0.02, 3.0), scan_row("EDG", 5.0, 0.0301, 2.0),
                scan_row("PNY", 0.99, 0.10, 2.0),
                {"ticker": "BAD", "columns": {"Symbol": "BAD"}}]
        data, _ = self.run_scan(rows)
        self.assertEqual(data["rows_skipped"], 1)
        self.assertFalse(data["degenerate_sample"])
        self.assertAlmostEqual(data["max_relative_volume"], 250.0, delta=0.001)
        bands = {b["band"]: b for b in data["bands"]}
        b25 = bands["$2.5-5"]
        self.assertEqual(b25["count"], 3)
        self.assertAlmostEqual(b25["median_pct"], 2.0, delta=0.001)
        self.assertAlmostEqual(b25["pct_positive"], 66.67, delta=0.1)
        self.assertEqual(bands["$5-10"]["count"], 1)
        self.assertAlmostEqual(bands["$5-10"]["median_pct"], 3.01, delta=0.001)
        self.assertEqual(bands["< $1"]["count"], 1)

    def test_degenerate_sample_flag(self):
        rows = [scan_row(f"D{i}", 2.0 + i, 0.001, 1.0 + i * 0.02) for i in range(5)]
        data, _ = self.run_scan(rows)
        self.assertTrue(data["degenerate_sample"])
        self.assertLess(data["max_relative_volume"], 1.5)

    def test_chart_renders_valid_png(self):
        rows = [scan_row("AAA", 3.0, 0.05, 250.0), scan_row("NEG", 12.0, -0.08, 40.0)]
        _, png = self.run_scan(rows, chart=True)
        self.assertIsNotNone(png)
        self.assertGreater(len(png), 1000)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")


class FilterScanTests(unittest.TestCase):
    def run_filter(self, rows, top_n=15):
        with tempfile.TemporaryDirectory() as td:
            scan = os.path.join(td, "scan.json")
            out = os.path.join(td, "out.json")
            with open(scan, "w", encoding="utf-8") as f:
                json.dump({"data": {"result": {"results": rows, "total_items": len(rows)}}}, f)
            run_cli(FILTER, ["--scan-file", scan, "--price-min", "2.50", "--price-max", "5",
                             "--min-rel-volume", "2", "--min-abs-pct-change", "3",
                             "--top-n", str(top_n), "--json-out", out])
            with open(out, encoding="utf-8") as f:
                return json.load(f)

    def test_filters_band_relvol_and_move(self):
        rows = [scan_row("KEEP", 4.45, 0.1528, 557.75),
                scan_row("LOWPX", 2.49, 0.10, 50.0),
                scan_row("HIPX", 5.01, 0.10, 50.0),
                scan_row("LOWRV", 3.00, 0.10, 1.9),
                scan_row("FLAT", 3.00, 0.0299, 50.0),
                scan_row("NEGMOVE", 3.00, -0.0500, 9.0),
                scan_row("EDGEPX", 5.00, 0.0300, 2.0),
                {"ticker": "BROKEN", "columns": {"Symbol": "BROKEN"}}]
        data = self.run_filter(rows)
        symbols = [w["symbol"] for w in data["working_list"]]
        self.assertEqual(symbols, ["KEEP", "NEGMOVE", "EDGEPX"])
        self.assertEqual(data["rows_skipped"], 1)
        keep = data["working_list"][0]
        self.assertAlmostEqual(keep["day_pct_change"], 15.28, delta=0.001)
        edge = data["working_list"][2]
        self.assertAlmostEqual(edge["last"], 5.00, delta=0.001)
        self.assertAlmostEqual(edge["day_pct_change"], 3.00, delta=0.001)

    def test_top_n_caps_by_relative_volume(self):
        rows = [scan_row(f"S{i}", 3.0, 0.05, 10.0 + i) for i in range(6)]
        data = self.run_filter(rows, top_n=3)
        symbols = [w["symbol"] for w in data["working_list"]]
        self.assertEqual(symbols, ["S5", "S4", "S3"])
        self.assertEqual(data["passed_filters"], 6)


class MarketClockTests(unittest.TestCase):
    def clock(self, now_utc, blackout=0):
        args = ["--now-utc", now_utc, "--json"]
        if blackout:
            args += ["--no-buy-first-minutes", str(blackout)]
        return json.loads(run_cli(CLOCK, args))

    def test_summer_offsets_are_daylight(self):
        # 2026-07-21 15:07Z — the run that had to improvise a clock.
        c = self.clock("2026-07-21T15:07:00Z")
        self.assertEqual(c["et"], "2026-07-21 11:07:00 EDT")
        self.assertEqual(c["pt"], "2026-07-21 08:07:00 PDT")
        self.assertEqual(c["session"], "regular")
        self.assertEqual(c["minutes_since_open"], 97)

    def test_winter_offsets_are_standard(self):
        c = self.clock("2026-01-15T15:07:00Z")
        self.assertEqual(c["et"], "2026-01-15 10:07:00 EST")
        self.assertEqual(c["pt"], "2026-01-15 07:07:00 PST")

    def test_dst_spring_forward_boundary_eastern(self):
        # 2026 spring-forward: 2nd Sunday of March = Mar 8, 02:00 EST = 07:00Z.
        before = self.clock("2026-03-08T06:59:00Z")
        after = self.clock("2026-03-08T07:00:00Z")
        self.assertEqual(before["et"], "2026-03-08 01:59:00 EST")
        self.assertEqual(after["et"], "2026-03-08 03:00:00 EDT")

    def test_dst_fall_back_boundary_eastern(self):
        # 2026 fall-back: 1st Sunday of November = Nov 1, 02:00 EDT = 06:00Z.
        before = self.clock("2026-11-01T05:59:00Z")
        after = self.clock("2026-11-01T06:00:00Z")
        self.assertEqual(before["et"], "2026-11-01 01:59:00 EDT")
        self.assertEqual(after["et"], "2026-11-01 01:00:00 EST")

    def test_zones_switch_at_their_own_local_2am(self):
        # Between 07:00Z and 10:00Z on spring-forward day, ET is already on
        # daylight time while PT is still on standard time.
        c = self.clock("2026-03-08T08:00:00Z")
        self.assertEqual(c["et"], "2026-03-08 04:00:00 EDT")
        self.assertEqual(c["pt"], "2026-03-08 00:00:00 PST")

    def test_opening_blackout_window(self):
        # Open 09:30 ET = 13:30Z in summer; blackout covers the first 45 min.
        self.assertTrue(self.clock("2026-07-21T13:35:00Z", blackout=45)["opening_blackout"])
        self.assertTrue(self.clock("2026-07-21T14:14:00Z", blackout=45)["opening_blackout"])
        self.assertFalse(self.clock("2026-07-21T14:15:00Z", blackout=45)["opening_blackout"])

    def test_sessions_and_weekend(self):
        self.assertEqual(self.clock("2026-07-21T12:00:00Z")["session"], "pre-market")
        self.assertEqual(self.clock("2026-07-21T20:30:00Z")["session"], "after-hours")
        self.assertEqual(self.clock("2026-07-22T01:00:00Z")["session"], "closed")
        self.assertEqual(self.clock("2026-07-18T15:00:00Z")["session"], "closed-weekend")

    def test_pacific_trading_day_rolls_before_utc_day(self):
        # 2026-07-22 03:00Z is still 2026-07-21 in Pacific — the date used
        # for "filled today" counting must be the Pacific one.
        self.assertEqual(self.clock("2026-07-22T03:00:00Z")["date_pt"], "2026-07-21")


if __name__ == "__main__":
    unittest.main(verbosity=2)
