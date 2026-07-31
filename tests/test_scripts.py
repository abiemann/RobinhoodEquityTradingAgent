#!/usr/bin/env python3
"""Regression suite for deterministic subroutines and routine contracts.

Run:  py -3 tests/test_scripts.py   (or: python3 tests/test_scripts.py)

Stdlib only — no pytest, no fixtures on disk. Each test drives the real CLI
via subprocess and asserts on --json-out / --chart-out, so the scripts are
tested exactly as the agents invoke them. Expected values for FISN/TTRX were
verified against live API data on 2026-07-07.
"""

import http.client
import importlib.util
import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALUATE = os.path.join(ROOT, "evaluate_candidates.py")
SCANNER = os.path.join(ROOT, "tools", "price_band_scanner.py")
FILTER = os.path.join(ROOT, "filter_scan.py")
CLOCK = os.path.join(ROOT, "market_clock.py")
RUN_LOCK = os.path.join(ROOT, "run_lock.py")
DAILY_LOSS = os.path.join(ROOT, "daily_loss.py")
DASHBOARD = os.path.join(ROOT, "dashboard", "serve.py")

sys.path.insert(0, ROOT)
from evaluate_candidates import spread_gate
from market_calendar import (CALENDAR_YEARS, CLOSED_DATES,
                             EARLY_CLOSE_MINUTES_BY_DATE,
                             NORMAL_REGULAR_CLOSE_MINUTE,
                             REGULAR_OPEN_MINUTE)

DASHBOARD_SPEC = importlib.util.spec_from_file_location("dashboard_serve", DASHBOARD)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader
DASHBOARD_SERVER = importlib.util.module_from_spec(DASHBOARD_SPEC)
DASHBOARD_SPEC.loader.exec_module(DASHBOARD_SERVER)


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


class DailyLossTests(unittest.TestCase):
    TRADING_DATE = "2026-07-31"
    AS_OF_UTC = "2026-07-31T20:00:00Z"
    PREVIOUS_SESSION = "2026-07-30"

    @staticmethod
    def position(symbol, quantity, intraday_quantity):
        return {
            "symbol": symbol,
            "quantity": str(quantity),
            "intraday_quantity": str(intraday_quantity),
            "type": "long",
        }

    @staticmethod
    def execution(execution_id, price, quantity, timestamp="2026-07-31T15:00:00.123456789Z", fees="0"):
        return {
            "id": execution_id,
            "price": str(price),
            "quantity": str(quantity),
            "timestamp": timestamp,
            "fees": str(fees),
        }

    @staticmethod
    def order(
        order_id,
        symbol,
        side,
        executions,
        created_at="2026-07-31T14:59:00Z",
        *,
        state=None,
        cumulative_quantity=None,
    ):
        if cumulative_quantity is None:
            cumulative_quantity = sum(
                (
                    Decimal(str(execution["quantity"]))
                    for execution in executions
                    if execution is not None
                ),
                Decimal(0),
            )
        cumulative_decimal = Decimal(str(cumulative_quantity))
        if state is None:
            state = (
                "filled"
                if cumulative_decimal > 0
                else "confirmed"
            )
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "created_at": created_at,
            "state": state,
            "cumulative_quantity": str(cumulative_quantity),
            "fees": "999.99",  # cumulative order fee must never be added to execution fees
            "executions": executions,
        }

    @classmethod
    def quote(cls, symbol, current, previous, *, nonreg=None,
              close_date=None, last_time="2026-07-31T19:59:00Z",
              nonreg_time="2026-07-31T19:59:30Z"):
        quote = {
            "symbol": symbol,
            "last_trade_price": str(current),
            "venue_last_trade_time": last_time,
            "last_non_reg_trade_price": None if nonreg is None else str(nonreg),
            "venue_last_non_reg_trade_time": None if nonreg is None else nonreg_time,
            "adjusted_previous_close": str(previous),
            "previous_close_date": close_date or cls.PREVIOUS_SESSION,
            "has_traded": True,
            "state": "active",
        }
        return {
            "quote": quote,
            "close": {
                "symbol": symbol,
                "date": close_date or cls.PREVIOUS_SESSION,
                "price": str(previous),
                "interpolated": False,
                "source": "sip-list-exchange-close",
            },
        }

    @staticmethod
    def page(name, rows, next_value=None):
        return {"data": {name: rows, "next": next_value}}

    def invoke(self, *, positions=None, orders=None, quotes=None,
               total_value="1000", halt_pct="5", expected_success=True):
        positions = positions or [self.page("positions", [])]
        orders = orders or [self.page("orders", [])]
        quotes = quotes or []
        with tempfile.TemporaryDirectory() as td:
            def write_documents(prefix, documents):
                paths = []
                for index, document in enumerate(documents, 1):
                    path = os.path.join(td, f"{prefix}-{index}.json")
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(document, f)
                    paths.append(path)
                return paths

            portfolio_path = os.path.join(td, "portfolio.json")
            with open(portfolio_path, "w", encoding="utf-8") as f:
                json.dump({"data": {"total_value": total_value}}, f)
            position_paths = write_documents("positions", positions)
            order_paths = write_documents("orders", orders)
            quote_paths = write_documents("quotes", quotes)
            output_path = os.path.join(td, "daily-loss.json")
            args = [
                "--portfolio", portfolio_path,
                "--positions", *position_paths,
                "--orders", *order_paths,
            ]
            if quote_paths:
                args += ["--quotes", *quote_paths]
            args += [
                "--trading-date", self.TRADING_DATE,
                "--as-of-utc", self.AS_OF_UTC,
                "--halt-pct", halt_pct,
                "--json-out", output_path,
            ]
            proc = subprocess.run(
                [sys.executable, DAILY_LOSS] + args,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            if not expected_success:
                self.assertNotEqual(proc.returncode, 0, proc.stdout)
                self.assertFalse(os.path.exists(output_path))
                return proc
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(output_path, encoding="utf-8") as f:
                return json.load(f)

    def test_avir_overnight_winner_is_a_real_day_loss_and_old_order_is_included(self):
        avir = self.order(
            "order-avir",
            "AVIR",
            "sell",
            [self.execution("exec-avir", "4.42", "67.344596", fees="0.01")],
            created_at="2026-07-28T23:00:00Z",
        )
        result = self.invoke(
            orders=[
                self.page("orders", [], "https://example.test/orders?cursor=next"),
                self.page("orders", [avir]),
            ],
            quotes=[{"data": {"results": [self.quote("AVIR", "4.42", "4.51")]}}],
            total_value="1471.19",
        )
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["trading_date_et"], self.TRADING_DATE)
        self.assertEqual(result["daily_pnl"], "-6.07101364")
        self.assertEqual(result["status"], "clear")
        self.assertFalse(result["halt_new_buys"])
        self.assertEqual(result["required_quote_symbols"], ["AVIR"])
        self.assertEqual(result["reconciliation"]["unique_order_count"], 1)
        self.assertEqual(result["reconciliation"]["today_execution_count"], 1)

    def test_today_buy_uses_execution_cost_and_trips_at_loss_threshold(self):
        result = self.invoke(
            positions=[self.page("positions", [self.position("NEW", "10", "10")])],
            orders=[self.page("orders", [
                self.order("order-buy", "NEW", "buy", [
                    self.execution("exec-buy", "10", "10", fees="0.02")
                ])
            ])],
            quotes=[{"data": {"results": [self.quote("NEW", "9", "8")]}}],
            total_value="100",
            halt_pct="10",
        )
        self.assertEqual(result["daily_pnl"], "-10.02")
        self.assertEqual(result["halt_threshold"], "10")
        self.assertEqual(result["loss_pct_of_total"], "10.02")
        self.assertEqual(result["status"], "tripped")
        self.assertTrue(result["halt_new_buys"])

    def test_partial_overnight_sale_uses_prior_close_and_exact_boundary_trips(self):
        result = self.invoke(
            positions=[self.page("positions", [self.position("PART", "6", "-4")])],
            orders=[self.page("orders", [
                self.order("order-sell", "PART", "sell", [
                    self.execution("exec-sell", "90", "4")
                ])
            ])],
            quotes=[{"data": {"results": [self.quote("PART", "95", "100")]}}],
            total_value="1000",
            halt_pct="7",
        )
        self.assertEqual(result["daily_pnl"], "-70")
        self.assertEqual(result["halt_threshold"], "70")
        self.assertEqual(result["status"], "tripped")

        clear = self.invoke(
            positions=[self.page("positions", [self.position("PART", "6", "-4")])],
            orders=[self.page("orders", [
                self.order("order-sell", "PART", "sell", [
                    self.execution("exec-sell", "90", "4")
                ])
            ])],
            quotes=[{"data": {"results": [self.quote("PART", "95.0001", "100")]}}],
            total_value="1000",
            halt_pct="7",
        )
        self.assertEqual(clear["daily_pnl"], "-69.9994")
        self.assertEqual(clear["status"], "clear")

    def test_same_day_round_trip_needs_no_quote_and_charges_execution_fees_once(self):
        result = self.invoke(
            orders=[self.page("orders", [
                self.order("order-buy", "ROUND", "buy", [
                    self.execution("exec-buy", "10", "10", fees="0.10")
                ]),
                self.order("order-sell", "ROUND", "sell", [
                    self.execution("exec-sell", "9", "10", fees="0.10")
                ]),
            ], "")],
            quotes=[],
        )
        self.assertEqual(result["required_quote_symbols"], [])
        self.assertEqual(result["daily_pnl"], "-10.2")
        self.assertEqual(result["status"], "clear")

    def test_newer_nonregular_quote_is_the_current_mark(self):
        result = self.invoke(
            positions=[self.page("positions", [self.position("LATE", "1", "0")])],
            quotes=[{"data": {"results": [
                self.quote("LATE", "100", "100", nonreg="90")
            ]}}],
            total_value="100",
            halt_pct="10",
        )
        detail = result["reconciliation"]["symbols"][0]
        self.assertEqual(detail["current_price"], "90")
        self.assertEqual(detail["current_price_source"], "last_non_reg_trade")
        self.assertEqual(result["daily_pnl"], "-10")
        self.assertEqual(result["status"], "tripped")

    def test_incomplete_pagination_and_intraday_mismatch_fail_closed(self):
        incomplete = self.invoke(
            orders=[self.page("orders", [], "https://example.test/orders?cursor=missing")],
            expected_success=False,
        )
        self.assertIn("final page unexpectedly has next", incomplete.stderr)

        repeated_cursor = self.invoke(
            orders=[
                self.page(
                    "orders",
                    [],
                    "https://example.test/orders?cursor=repeated",
                ),
                self.page(
                    "orders",
                    [],
                    "https://example.test/orders?cursor=repeated",
                ),
                self.page("orders", []),
            ],
            expected_success=False,
        )
        self.assertIn("repeated next cursor", repeated_cursor.stderr)

        mismatch = self.invoke(
            positions=[self.page("positions", [self.position("BAD", "10", "5")])],
            orders=[self.page("orders", [
                self.order("order-buy", "BAD", "buy", [
                    self.execution("exec-buy", "10", "4")
                ])
            ])],
            expected_success=False,
        )
        self.assertIn("do not equal intraday_quantity", mismatch.stderr)

    def test_wrong_close_date_and_nonfinite_portfolio_fail_closed(self):
        wrong_close = self.invoke(
            positions=[self.page("positions", [self.position("OLD", "1", "0")])],
            quotes=[{"data": {"results": [
                self.quote("OLD", "9", "10", close_date="2026-07-29")
            ]}}],
            expected_success=False,
        )
        self.assertIn("expected 2026-07-30", wrong_close.stderr)

        nonfinite = self.invoke(total_value=float("nan"), expected_success=False)
        self.assertIn("non-finite JSON constant", nonfinite.stderr)

    def test_discovery_mode_outputs_the_reconciled_quote_set(self):
        with tempfile.TemporaryDirectory() as td:
            positions_path = os.path.join(td, "positions.json")
            orders_path = os.path.join(td, "orders.json")
            symbols_path = os.path.join(td, "symbols.json")
            with open(positions_path, "w", encoding="utf-8") as f:
                json.dump(
                    self.page("positions", [self.position("HELD", "2", "0")]),
                    f,
                )
            with open(orders_path, "w", encoding="utf-8") as f:
                json.dump(
                    self.page("orders", [
                        self.order("closed-order", "CLOSED", "sell", [
                            self.execution("closed-execution", "9", "3")
                        ])
                    ]),
                    f,
                )
            run_cli(
                DAILY_LOSS,
                [
                    "--positions", positions_path,
                    "--orders", orders_path,
                    "--trading-date", self.TRADING_DATE,
                    "--as-of-utc", self.AS_OF_UTC,
                    "--symbols-out", symbols_path,
                ],
            )
            with open(symbols_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["CLOSED", "HELD"])

    def test_prior_day_execution_is_ignored_but_future_execution_fails(self):
        ignored = self.invoke(
            orders=[self.page("orders", [
                self.order("old-order", "OLD", "buy", [
                    self.execution(
                        "old-execution",
                        "10",
                        "1",
                        timestamp="2026-07-31T03:59:59.999999999Z",
                    )
                ])
            ])],
        )
        self.assertEqual(ignored["daily_pnl"], "0")
        self.assertEqual(ignored["reconciliation"]["today_execution_count"], 0)

        future = self.invoke(
            orders=[self.page("orders", [
                self.order("future-order", "FUT", "buy", [
                    self.execution(
                        "future-execution",
                        "10",
                        "1",
                        timestamp="2026-07-31T20:00:00.000000001Z",
                    )
                ])
            ])],
            expected_success=False,
        )
        self.assertIn("timestamp is later than as-of", future.stderr)

    def test_conflicting_duplicate_execution_id_fails_closed(self):
        conflict = self.invoke(
            orders=[self.page("orders", [
                self.order("order-one", "DUP", "buy", [
                    self.execution("same-execution", "10", "1")
                ]),
                self.order("order-two", "DUP", "buy", [
                    self.execution("same-execution", "11", "1")
                ]),
            ])],
            expected_success=False,
        )
        self.assertIn("conflicting duplicate execution ID", conflict.stderr)

    def test_missing_or_truncated_execution_list_fails_closed(self):
        missing = self.order(
            "filled-without-executions",
            "CLOSED",
            "sell",
            [],
            state="filled",
            cumulative_quantity="5",
        )
        result = self.invoke(
            orders=[self.page("orders", [missing])],
            expected_success=False,
        )
        self.assertIn(
            "execution quantities 0 do not equal cumulative_quantity 5",
            result.stderr,
        )

        truncated = self.order(
            "truncated-executions",
            "PART",
            "sell",
            [self.execution("only-one", "10", "1")],
            state="partially_filled",
            cumulative_quantity="2",
        )
        result = self.invoke(
            orders=[self.page("orders", [truncated])],
            expected_success=False,
        )
        self.assertIn(
            "execution quantities 1 do not equal cumulative_quantity 2",
            result.stderr,
        )

        null_execution = self.order(
            "null-execution",
            "NULL",
            "buy",
            [None],
            state="confirmed",
            cumulative_quantity="0",
        )
        result = self.invoke(
            orders=[self.page("orders", [null_execution])],
            expected_success=False,
        )
        self.assertIn("null execution is indeterminate", result.stderr)

    def test_future_and_stale_open_session_quotes_fail_closed(self):
        position = [self.page(
            "positions", [self.position("TIME", "1", "0")]
        )]
        future = self.invoke(
            positions=position,
            quotes=[{"data": {"results": [
                self.quote(
                    "TIME",
                    "9",
                    "10",
                    last_time="2026-07-31T20:00:00.000000001Z",
                )
            ]}}],
            expected_success=False,
        )
        self.assertIn("timestamp is later than as-of", future.stderr)

        stale = self.invoke(
            positions=position,
            quotes=[{"data": {"results": [
                self.quote(
                    "TIME",
                    "9",
                    "10",
                    last_time="2026-07-31T19:44:59Z",
                )
            ]}}],
            expected_success=False,
        )
        self.assertIn("more than 15 minutes old", stale.stderr)

    def test_unexpected_quote_and_oversized_quote_batch_fail_closed(self):
        unexpected = self.invoke(
            quotes=[{"data": {"results": [self.quote("EXTRA", "10", "10")]}}],
            expected_success=False,
        )
        self.assertIn("unexpected quote result", unexpected.stderr)

        too_many = [
            self.quote(f"S{index}", "10", "10") for index in range(21)
        ]
        oversized = self.invoke(
            positions=[self.page("positions", [self.position("S0", "1", "0")])],
            quotes=[{"data": {"results": too_many}}],
            expected_success=False,
        )
        self.assertIn("more than 20 results", oversized.stderr)

    def test_routine_makes_helper_json_the_only_daily_loss_authority(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()
        block = routine.split("### DAILY-LOSS CIRCUIT BREAKER", 1)[1].split(
            "### RUN THESE STEPS IN ORDER", 1
        )[0]
        self.assertIn("SOLE authority is therefore `daily_loss.py`", block)
        self.assertIn("use NO `created_at_gte`, `state`, `symbol`, or `placed_agent` filter", block)
        self.assertIn("Follow `data.next` until it is absent/empty", block)
        self.assertIn("execution timestamp rather than order creation time", block)
        self.assertIn("`intraday_quantity`", block)
        self.assertIn("`adjusted_previous_close`", block)
        self.assertIn("`cumulative_quantity`", block)
        self.assertIn("Null rows/elements are indeterminate", block)
        self.assertIn("DAILY-LOSS DISCOVERY", block)
        self.assertIn("DAILY-LOSS FINAL", block)
        self.assertIn(
            "separate set of FINAL raw files",
            block,
        )
        self.assertIn("Never evaluate with the earlier discovery files", block)
        self.assertIn(
            "--portfolio <FINAL raw portfolio file>",
            block,
        )
        self.assertIn("at most 15 minutes old", block)
        self.assertIn(
            "`as_of_utc` exactly DAILY-LOSS FINAL's `utc`",
            block,
        )
        self.assertIn("`schema_version` exactly `1`", block)
        self.assertIn("make no new buys", block)
        self.assertIn("NEVER feed its result into `daily_loss.py`", block)
        self.assertNotIn("compute trailing-day P&L", block)
        snapshot = routine.split("**Publish the STATUS SNAPSHOT", 1)[1].split(
            "The filename is exactly:", 1
        )[0]
        self.assertIn("or null only when that telemetry call failed twice", snapshot)
        self.assertIn("<clear|tripped|indeterminate>", snapshot)

        with open(
            os.path.join(ROOT, "dashboard", "index.html"),
            encoding="utf-8",
        ) as f:
            dashboard = f.read()
        self.assertIn('typeof n === "number" && Number.isFinite(n)', dashboard)
        self.assertIn('"unavailable"', dashboard)

        scratch_creation = routine.index(
            "create one NEW session-scoped scratch directory"
        )
        self.assertLess(
            scratch_creation,
            routine.index("### DAILY-LOSS CIRCUIT BREAKER"),
        )
        scan_phase = routine.split("6. `run_scan`", 1)[1].split(
            "**FOURTH", 1
        )[0]
        self.assertIn(
            "Reuse the NEW session-scoped scratch directory already created",
            scan_phase,
        )


class EvaluateCandidatesTests(unittest.TestCase):
    def run_eval(self, hist_payload, quotes, extra=None, return_document=False):
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
                document = json.load(f)
            if return_document:
                return document
            return {r["symbol"]: r for r in document["results"]}

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

    def test_json_output_identifies_whether_rsi_gate_was_enabled(self):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        common = ["--volume-lookback-days", "5", "--high-lookback-days", "5",
                  "--min-median-dollar-volume", "0"]

        pre_rsi = self.run_eval(payload, {"SYNX": 4.0}, extra=common, return_document=True)
        self.assertEqual(pre_rsi["schema_version"], 1)
        self.assertIs(pre_rsi["rsi_gate_enabled"], False)

        with tempfile.TemporaryDirectory() as td:
            rsi_path = os.path.join(td, "rsi.json")
            with open(rsi_path, "w", encoding="utf-8") as f:
                json.dump({"SYNX": [40, 36, 33, 30, 29, 34]}, f)
            final = self.run_eval(
                payload, {"SYNX": 4.0},
                extra=common + ["--rsi-file", rsi_path, "--rsi-oversold", "35",
                                "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1",
                                "--rsi-max-entry", "60", "--rsi-period", "14"],
                return_document=True,
            )
        self.assertEqual(final["schema_version"], 1)
        self.assertIs(final["rsi_gate_enabled"], True)
        self.assertEqual(final["results"][0]["rsi_gate"], "pass")

    def test_insufficient_history_is_blocked_before_candidate_math(self):
        payload = {"results": [{"symbol": "SHORT",
                                "bars": [bar("2026-07-30", 4.5, 5.0, 900000)]}]}
        short = self.run_eval(payload, {"SHORT": 4.0})["SHORT"]

        self.assertTrue(short["insufficient_history"])
        self.assertFalse(short["buy_candidate"])
        self.assertIsNone(short["median_dollar_volume"])
        self.assertIsNone(short["recent_high"])
        self.assertIsNone(short["pct_below_high"])
        self.assertIn("1 bars < required 20", short["skip_reason"])

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

    def run_eval_rsi(self, rsi_payload, period=14):
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
                                        "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1",
                                        "--rsi-max-entry", "60", "--rsi-period", str(period)])["SYNX"]

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

    def test_rsi_closes_fallback_uses_configured_period(self):
        # This has only five RSI values at period 2, but none at period 14.
        # It proves the fallback receives the configured period rather than a
        # silent code default.
        closes = [10, 9, 8, 7, 6, 6.1, 6.2]
        configured = self.run_eval_rsi({"SYNX": {"closes": closes}}, period=2)
        default_period = self.run_eval_rsi({"SYNX": {"closes": closes}}, period=14)
        self.assertEqual(configured["rsi_gate"], "pass")
        self.assertTrue(configured["buy_candidate"])
        self.assertEqual(default_period["rsi_gate"], "block")
        self.assertIn("insufficient", default_period["rsi_reason"])

    def test_rsi_file_requires_an_explicit_period(self):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        with tempfile.TemporaryDirectory() as td:
            rsi_path = os.path.join(td, "rsi.json")
            with open(rsi_path, "w", encoding="utf-8") as f:
                json.dump({"SYNX": {"rsi": [40, 36, 33, 30, 29, 34]}}, f)
            with self.assertRaises(AssertionError):
                self.run_eval(payload, {"SYNX": 4.0},
                              extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                     "--min-median-dollar-volume", "0", "--rsi-file", rsi_path,
                                     "--rsi-oversold", "35", "--rsi-lookback-bars", "5",
                                     "--rsi-confirm-bars", "1", "--rsi-max-entry", "60"])

    def test_routine_passes_configured_rsi_period(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()
        command = next(line for line in routine.splitlines()
                       if "python3 evaluate_candidates.py" in line)
        self.assertIn("--rsi-period <RSI_PERIOD>", command)

    def run_eval_spread(self, quote, max_spread="2.0"):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        return self.run_eval(payload, {"SYNX": quote},
                             extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                    "--min-median-dollar-volume", "0",
                                    "--max-spread-buy-pct", max_spread])["SYNX"]

    def test_spread_gate_blocks_wide_book(self):
        # GRDX as quoted at its 2026-07-24 buy: 3.88% spread, stop filled on arrival
        res = self.run_eval_spread({"price": 3.0783, "bid": 2.97, "ask": 3.09})
        self.assertEqual(res["spread_gate"], "block")
        self.assertFalse(res["buy_candidate"])
        self.assertAlmostEqual(res["spread_pct"], 3.96, delta=0.01)
        self.assertIn("spread gate", res["skip_reason"])

    def test_spread_gate_passes_tight_book(self):
        # MGNX from the same run: a penny wide on a $3.68 bid
        res = self.run_eval_spread({"price": 3.6999, "bid": 3.68, "ask": 3.69})
        self.assertEqual(res["spread_gate"], "pass")
        self.assertTrue(res["buy_candidate"])
        self.assertAlmostEqual(res["spread_pct"], 0.27, delta=0.01)

    def test_spread_gate_accepts_raw_quote_object(self):
        res = self.run_eval_spread({"last_trade_price": "3.6999",
                                    "bid_price": "3.680000", "ask_price": "3.690000"})
        self.assertEqual(res["spread_gate"], "pass")
        self.assertTrue(res["buy_candidate"])

    def test_spread_gate_blocks_missing_and_zero_quotes(self):
        bare = self.run_eval_spread(4.0)
        self.assertEqual(bare["spread_gate"], "block")
        self.assertIn("no bid/ask", bare["skip_reason"])
        zero = self.run_eval_spread({"price": 4.0, "bid": 0, "ask": 4.1})
        self.assertEqual(zero["spread_gate"], "block")
        self.assertIn("unusable quote", zero["skip_reason"])

    def test_nonfinite_quotes_and_spread_threshold_fail_loudly(self):
        for quote in (
            {"price": "NaN", "bid": 3.68, "ask": 3.69},
            {"price": 3.70, "bid": "Infinity", "ask": 3.69},
            {"price": 3.70, "bid": 3.68, "ask": "-Infinity"},
        ):
            with self.assertRaises(AssertionError):
                self.run_eval_spread(quote)
        with self.assertRaises(AssertionError):
            self.run_eval_spread({"price": 3.70, "bid": 3.68, "ask": 3.69},
                                 max_spread="NaN")

    def test_spread_gate_directly_blocks_nonfinite_values(self):
        for bid, ask, threshold in (
            (float("nan"), 4.0, 2.0),
            (3.0, float("inf"), 2.0),
            (3.0, 4.0, float("nan")),
        ):
            passes, reason, pct = spread_gate(bid, ask, threshold)
            self.assertFalse(passes)
            self.assertIsNone(pct)
            self.assertIn("non-finite", reason)

    def test_nonfinite_historical_and_rsi_values_fail_loudly(self):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        bars[-1]["close_price"] = "NaN"
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        with self.assertRaises(AssertionError):
            self.run_eval(payload, {"SYNX": 4.0},
                          extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                 "--min-median-dollar-volume", "0"])
        huge_bars = [bar("2026-07-01", 1.0, 2.0, "1e308"),
                     bar("2026-07-02", 1.0, 2.0, "1e308")]
        huge_payload = {"results": [{"symbol": "HUGE", "bars": huge_bars}]}
        with self.assertRaises(AssertionError):
            self.run_eval(huge_payload, {"HUGE": 1.0},
                          extra=["--volume-lookback-days", "2", "--high-lookback-days", "2",
                                 "--min-median-dollar-volume", "0"])
        with self.assertRaises(AssertionError):
            self.run_eval_rsi({"SYNX": {"rsi": [40, 36, 33, 30, 29, "NaN"]}})

    def test_spread_gate_disabled_when_flag_absent(self):
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        res = self.run_eval(payload, {"SYNX": {"price": 4.0, "bid": 2.97, "ask": 3.09}},
                            extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                   "--min-median-dollar-volume", "0"])["SYNX"]
        self.assertNotIn("spread_gate", res)
        self.assertTrue(res["buy_candidate"])

    def test_spread_gate_boundary_passes_at_threshold(self):
        # exactly 2.00% wide - the rule rejects only spreads GREATER than the max
        res = self.run_eval_spread({"price": 4.0, "bid": 3.96, "ask": 4.04})
        self.assertAlmostEqual(res["spread_pct"], 2.00, delta=0.001)
        self.assertEqual(res["spread_gate"], "pass")
        self.assertTrue(res["buy_candidate"])

    def test_rsi_confirm_bars_2_names_which_bar_failed(self):
        # LODE's real series on 2026-07-24: a collapse whose LAST bar ticked up.
        # confirm=1 buys it; confirm=2 blocks it. The reason must say which bar
        # failed, or a report cannot show that RSI_CONFIRM_BARS did the blocking.
        series = {"SYNX": {"rsi": [45.42, 41.38, 12.20, 11.00, 9.39, 9.88]}}
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}]}
        with tempfile.TemporaryDirectory() as td:
            rsi_path = os.path.join(td, "rsi.json")
            with open(rsi_path, "w", encoding="utf-8") as f:
                json.dump(series, f)
            common = ["--volume-lookback-days", "5", "--high-lookback-days", "5",
                      "--min-median-dollar-volume", "0", "--rsi-file", rsi_path,
                      "--rsi-oversold", "35", "--rsi-lookback-bars", "5",
                      "--rsi-max-entry", "60", "--rsi-period", "14"]
            one = self.run_eval(payload, {"SYNX": 4.0}, extra=common + ["--rsi-confirm-bars", "1"])["SYNX"]
            two = self.run_eval(payload, {"SYNX": 4.0}, extra=common + ["--rsi-confirm-bars", "2"])["SYNX"]
        self.assertTrue(one["buy_candidate"])
        self.assertFalse(two["buy_candidate"])
        self.assertIn("confirm bar 2 of 2", two["rsi_reason"])

    def test_rsi_max_entry_blocks_a_spent_bounce(self):
        # BIYA's real series, 2026-07-27: oversold 4 bars back, then a 44-point
        # single-bar leap to overbought. Passed the gate, was bought, and stopped
        # out 6 minutes later for -$11.48. The oversold touch was stale.
        res = self.run_eval_rsi({"SYNX": {"rsi": [24.58, 24.34, 24.34, 68.92, 75.59, 76.65]}})
        self.assertEqual(res["rsi_gate"], "block")
        self.assertFalse(res["buy_candidate"])
        self.assertIn("bounce already run", res["rsi_reason"])

    def test_rsi_max_entry_allows_a_live_bounce(self):
        # MGNX's real series, 2026-07-24: oversold and still low while curling.
        # Bought, and taken for +$10.60 - must stay buyable.
        res = self.run_eval_rsi({"SYNX": {"rsi": [27.18, 25.95, 11.64, 11.12, 31.43, 35.94]}})
        self.assertEqual(res["rsi_gate"], "pass")
        self.assertTrue(res["buy_candidate"])

    def test_rsi_max_entry_boundary_passes_at_cap(self):
        # exactly at the cap - the rule rejects only values ABOVE it
        res = self.run_eval_rsi({"SYNX": {"rsi": [30, 28, 25, 40, 55, 60]}})
        self.assertEqual(res["rsi_gate"], "pass")
        self.assertTrue(res["buy_candidate"])

    def raw_rsi_response(self, symbol, values):
        """Real get_equity_technical_indicators shape, verified live 2026-07-28."""
        return {"data": {"symbol": symbol, "interval": "30minute", "bounds": "regular",
                         "indicators": [{"type": "rsi", "params": {"period": 14},
                                         "series": [{"begins_at": f"2026-07-28T{16+i//2:02d}:"
                                                                  f"{'30' if i % 2 else '00'}:00Z",
                                                     "value": v} for i, v in enumerate(values)]}]},
                "guide": "Convert begins_at (UTC) to the user's timezone for display."}

    def test_rsi_accepts_raw_response_files_without_hand_keying(self):
        # a run must be able to save each response VERBATIM and pass the files;
        # hand-building a symbol-keyed map is what broke the 2026-07-28 11:06 run
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}, {"symbol": "OTHR", "bars": bars}]}
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "rsi1.json")
            p2 = os.path.join(td, "rsi2.json")
            with open(p1, "w", encoding="utf-8") as f:
                json.dump(self.raw_rsi_response("SYNX", [40, 36, 33, 30, 29, 34]), f)
            with open(p2, "w", encoding="utf-8") as f:
                json.dump(self.raw_rsi_response("OTHR", [42, 39, 36, 33, 31, 29]), f)
            res = self.run_eval(payload, {"SYNX": 4.0, "OTHR": 4.0},
                                extra=["--volume-lookback-days", "5", "--high-lookback-days", "5",
                                       "--min-median-dollar-volume", "0",
                                       "--rsi-file", p1, p2, "--rsi-oversold", "35",
                                       "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1",
                                       "--rsi-max-entry", "60", "--rsi-period", "14"])
        # symbols came out of data.symbol in each file, and both files merged
        self.assertEqual(res["SYNX"]["rsi_gate"], "pass")
        self.assertTrue(res["SYNX"]["buy_candidate"])
        self.assertEqual(res["OTHR"]["rsi_gate"], "block")
        self.assertIn("still falling", res["OTHR"]["rsi_reason"])
        self.assertEqual(res["SYNX"]["rsi_series"], [40, 36, 33, 30, 29, 34])

    def test_malformed_authored_json_fails_loudly(self):
        # Both real 2026-07-28 failures were MID-document slips in LLM-authored
        # files: a missing ] (11:06) and an extra } BEFORE a closing ] (12:36,
        # byte-for-byte below). No parser can safely recover either, so the
        # script must fail loudly -- the routine's pre-run json.load validation
        # is the layer that catches these cheaply. Pins strictness so a future
        # "tolerant reader" does not sneak in; one was built and rejected the
        # day of the incident because it covered only never-observed cases.
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "NVA", "bars": bars}]}
        good = json.dumps(self.raw_rsi_response("NVA", [40.26, 38.82, 37.38, 42.02, 43.16, 41.41]))
        nva_exact = good.replace("]}]}", "]}}]}")        # the 12:36 file's actual defect
        common = ["--volume-lookback-days", "5", "--high-lookback-days", "5",
                  "--min-median-dollar-volume", "0", "--rsi-oversold", "35",
                  "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1",
                  "--rsi-max-entry", "60", "--rsi-period", "14"]
        with tempfile.TemporaryDirectory() as td:
            for name, text in (("interior.json", nva_exact), ("truncated.json", good[:-40])):
                p = os.path.join(td, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                with self.assertRaises(AssertionError, msg=name):
                    self.run_eval(payload, {"NVA": 4.0}, extra=common + ["--rsi-file", p])

    def test_rsi_series_is_recorded_for_threshold_sweeps(self):
        # the saved series must let a later analysis re-answer the gate at other
        # RSI_OVERSOLD / RSI_CONFIRM_BARS values without re-fetching indicators
        series = [45.42, 41.38, 12.20, 11.00, 9.39, 9.88]
        res = self.run_eval_rsi({"SYNX": {"rsi": series}})
        self.assertEqual(res["rsi_series"], series)

    def test_rsi_block_at_first_bar_is_labelled_bar_1(self):
        res = self.run_eval_rsi({"SYNX": {"rsi": [42, 39, 36, 33, 31, 29]}})
        self.assertIn("confirm bar 1 of 1", res["rsi_reason"])

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

    def test_json_handoff_has_complete_unrounded_working_list(self):
        row = scan_row("PREC", 4.45678, 0.0345678, 12.34567)
        row["columns"]["Volume"] = "1234.56789"
        data = self.run_filter([row])

        self.assertEqual(set(data), {"total_items", "rows_returned", "rows_skipped",
                                     "passed_filters", "working_list"})
        self.assertEqual(data["total_items"], 1)
        self.assertEqual(data["rows_returned"], 1)
        self.assertEqual(data["rows_skipped"], 0)
        self.assertEqual(data["passed_filters"], 1)
        for counter in ("total_items", "rows_returned", "rows_skipped", "passed_filters"):
            self.assertIs(type(data[counter]), int)
            self.assertGreaterEqual(data[counter], 0)
        self.assertEqual(len(data["working_list"]), 1)
        handoff = data["working_list"][0]
        self.assertEqual(set(handoff), {"symbol", "last", "rel_volume", "day_pct_change", "volume"})
        self.assertEqual(handoff["symbol"], "PREC")
        self.assertTrue(handoff["symbol"])
        for field in ("last", "rel_volume", "day_pct_change", "volume"):
            self.assertIs(type(handoff[field]), float)
            self.assertTrue(math.isfinite(handoff[field]))
        self.assertAlmostEqual(handoff["last"], 4.45678)
        self.assertAlmostEqual(handoff["rel_volume"], 12.34567)
        self.assertAlmostEqual(handoff["day_pct_change"], 3.45678)
        self.assertAlmostEqual(handoff["volume"], 1234.56789)

    def test_nonfinite_scan_fields_are_skipped(self):
        rows = [scan_row("KEEP", 4.0, 0.05, 10.0)]
        for symbol, field, value in (
            ("BADLAST", "Last", "NaN"),
            ("BADRV", "Relative volume", "Infinity"),
            ("BADPCT", "% Change", "-Infinity"),
            ("BADPCTOVERFLOW", "% Change", "1e308"),
            ("BADVOL", "Volume", "NaN"),
        ):
            row = scan_row(symbol, 4.0, 0.05, 10.0)
            row["columns"][field] = value
            rows.append(row)
        data = self.run_filter(rows)
        self.assertEqual([w["symbol"] for w in data["working_list"]], ["KEEP"])
        self.assertEqual(data["rows_skipped"], 5)

    def test_nonfinite_json_constant_fails_loudly(self):
        row = scan_row("BAD", 4.0, 0.05, 10.0)
        row["columns"]["Last"] = float("nan")
        with self.assertRaises(AssertionError):
            self.run_filter([row])


class RunLockTests(unittest.TestCase):
    def invoke(self, lock_file, action, token=None, now=None, lease_seconds=60):
        args = [sys.executable, RUN_LOCK, action,
                "--lock-file", lock_file,
                "--lease-seconds", str(lease_seconds)]
        if token is not None:
            args += ["--token", token]
        if now is not None:
            args += ["--now-utc", now]
        proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
        try:
            document = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"run_lock.py emitted non-JSON stdout: {proc.stdout!r}; stderr={proc.stderr!r}")
        return proc, document

    def test_only_one_concurrent_run_acquires_the_lease(self):
        with tempfile.TemporaryDirectory() as td:
            lock_file = os.path.join(td, "lease.sqlite3")

            def attempt(_):
                return self.invoke(lock_file, "acquire",
                                   now="2026-07-30T16:00:00Z")

            with ThreadPoolExecutor(max_workers=8) as pool:
                attempts = list(pool.map(attempt, range(8)))

        winners = [(proc, doc) for proc, doc in attempts if proc.returncode == 0]
        blocked = [(proc, doc) for proc, doc in attempts if proc.returncode == 2]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(blocked), 7)
        self.assertTrue(winners[0][1]["ok"])
        self.assertTrue(winners[0][1]["token"])
        for _, document in blocked:
            self.assertFalse(document["ok"])
            self.assertEqual(document["reason"], "active_run")
            self.assertNotIn("token", document.get("holder", {}))

    def test_renew_release_and_reacquire_require_the_owner_token(self):
        with tempfile.TemporaryDirectory() as td:
            lock_file = os.path.join(td, "lease.sqlite3")
            first_proc, first = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:00:00Z"
            )
            self.assertEqual(first_proc.returncode, 0)
            token = first["token"]

            blocked_proc, blocked = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:00:30Z"
            )
            self.assertEqual(blocked_proc.returncode, 2)
            self.assertEqual(blocked["reason"], "active_run")

            renew_proc, renewed = self.invoke(
                lock_file, "renew", token=token,
                now="2026-07-30T16:00:50Z"
            )
            self.assertEqual(renew_proc.returncode, 0)
            self.assertTrue(renewed["ok"])
            self.assertEqual(renewed["expires_at"], "2026-07-30T16:01:50Z")

            wrong_proc, wrong = self.invoke(
                lock_file, "release", token="not-the-owner",
                now="2026-07-30T16:01:00Z"
            )
            self.assertEqual(wrong_proc.returncode, 3)
            self.assertEqual(wrong["reason"], "ownership_lost")

            still_blocked_proc, _ = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:01:20Z"
            )
            self.assertEqual(still_blocked_proc.returncode, 2)

            release_proc, released = self.invoke(
                lock_file, "release", token=token,
                now="2026-07-30T16:01:30Z"
            )
            self.assertEqual(release_proc.returncode, 0)
            self.assertTrue(released["ok"])

            replacement_proc, replacement = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:01:31Z"
            )
            self.assertEqual(replacement_proc.returncode, 0)
            self.assertNotEqual(replacement["token"], token)

    def test_expired_takeover_fences_the_old_owner(self):
        with tempfile.TemporaryDirectory() as td:
            lock_file = os.path.join(td, "lease.sqlite3")
            _, first = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:00:00Z"
            )
            first_token = first["token"]

            takeover_proc, takeover = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:01:00Z"
            )
            self.assertEqual(takeover_proc.returncode, 0)
            self.assertTrue(takeover["recovered_expired_lease"])
            self.assertNotEqual(takeover["token"], first_token)

            stale_renew_proc, stale_renew = self.invoke(
                lock_file, "renew", token=first_token,
                now="2026-07-30T16:01:01Z"
            )
            self.assertEqual(stale_renew_proc.returncode, 3)
            self.assertEqual(stale_renew["reason"], "ownership_lost")

            stale_release_proc, stale_release = self.invoke(
                lock_file, "release", token=first_token,
                now="2026-07-30T16:01:02Z"
            )
            self.assertEqual(stale_release_proc.returncode, 3)
            self.assertEqual(stale_release["reason"], "ownership_lost")

            owner_renew_proc, owner_renew = self.invoke(
                lock_file, "renew", token=takeover["token"],
                now="2026-07-30T16:01:03Z"
            )
            self.assertEqual(owner_renew_proc.returncode, 0)
            self.assertTrue(owner_renew["ok"])

    def test_corrupt_coordination_database_fails_closed(self):
        with tempfile.TemporaryDirectory() as td:
            lock_file = os.path.join(td, "lease.sqlite3")
            with open(lock_file, "wb") as f:
                f.write(b"not a sqlite database")
            proc, document = self.invoke(
                lock_file, "acquire", now="2026-07-30T16:00:00Z"
            )
        self.assertEqual(proc.returncode, 1)
        self.assertFalse(document["ok"])
        self.assertEqual(document["reason"], "coordination_state_error")


class DashboardServerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo = self.temp_dir.name
        os.makedirs(os.path.join(self.repo, "dashboard"))
        os.makedirs(os.path.join(self.repo, "run-reports"))
        for name, content in (
            ("README.md", "private dashboard test fixture"),
            ("constants.md", "| `DRY_RUN` | `true` | test mode |"),
            ("trade-ledger.csv", "symbol,price\\nTEST,1.00\\n"),
            (os.path.join("dashboard", "index.html"), "<h1>Dashboard fixture</h1>"),
            (os.path.join("run-reports", "rhmra-status-test.json"), "{}"),
        ):
            with open(os.path.join(self.repo, name), "w", encoding="utf-8") as f:
                f.write(content)
        self.original_repo = DASHBOARD_SERVER.REPO
        DASHBOARD_SERVER.REPO = self.repo

        class QuietHandler(DASHBOARD_SERVER.Handler):
            def log_message(self, format, *args):
                pass

        self.server = DASHBOARD_SERVER.ThreadingHTTPServer(("127.0.0.1", 0), QuietHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        DASHBOARD_SERVER.REPO = self.original_repo
        self.temp_dir.cleanup()

    def request(self, method, path, host="127.0.0.1"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        try:
            connection.request(method, path, headers={"Host": host})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_whitelist_rejects_traversal_and_serves_allowed_files(self):
        for method in ("GET", "HEAD"):
            for path in ("/dashboard/../README.md", "/dashboard/%2e%2e/README.md",
                         "/run-reports/%2e%2e/README.md"):
                status, body = self.request(method, path)
                self.assertEqual(status, 403, f"{method} {path}")
                self.assertNotIn(b"private dashboard test fixture", body)

            # The standard handler decodes URLs once. A doubly encoded dot
            # sequence is therefore a literal nonexistent filename (404), not
            # a second decode into traversal; pin that distinction as well.
            status, body = self.request(method, "/dashboard/%252e%252e/README.md")
            self.assertEqual(status, 404, method)
            self.assertNotIn(b"private dashboard test fixture", body)

        status, body = self.request("GET", "/dashboard/index.html")
        self.assertEqual(status, 200)
        self.assertIn(b"Dashboard fixture", body)
        self.assertEqual(self.request("GET", "/run-reports/rhmra-status-test.json")[0], 200)
        self.assertEqual(self.request("GET", "/trade-ledger.csv")[0], 200)
        self.assertEqual(self.request("GET", "/README.md")[0], 403)
        self.assertEqual(self.request("GET", "/dashboard/index.html", host="example.test")[0], 403)

    def test_config_reports_current_dry_run_setting_and_fails_closed(self):
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"dry_run": True})

        constants_path = os.path.join(self.repo, "constants.md")
        with open(constants_path, "w", encoding="utf-8") as f:
            f.write("| `DRY_RUN` | `false` | live mode |")
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"dry_run": False})

        with open(constants_path, "w", encoding="utf-8") as f:
            f.write("| `DRY_RUN` | `maybe` | malformed |")
        status, body = self.request("GET", "/api/config")
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(document["dry_run"])
        self.assertIn("exactly one valid DRY_RUN row", document["error"])


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
        self.assertEqual(c["date_et"], "2026-07-21")
        self.assertEqual(c["date_pt"], "2026-07-21")
        self.assertEqual(c["session"], "regular")
        self.assertEqual(c["calendar_status"], "normal")
        self.assertEqual(c["regular_close_et"], "16:00")
        self.assertIs(type(c["entry_session_open"]), bool)
        self.assertTrue(c["entry_session_open"])
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

    def test_reads_no_buy_first_minutes_from_constants_md(self):
        # No --no-buy-first-minutes flag: the script must read the value from
        # constants.md rather than defaulting silently. Regression for the
        # 2026-07-22 06:37 run where an agent passed 5 against a real 45.
        c = self.clock("2026-07-22T13:37:00Z")   # 09:37 ET = 7 min past open
        self.assertTrue(c["opening_blackout"],
                        "with constants.md at 45, 7 min past open must block; a silent 0 default would falsely clear")

    def test_missing_constants_file_errors_loudly(self):
        # If constants.md cannot be found, the script must fail rather than
        # default to 0 (which silently disables the blackout).
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, CLOCK, "--constants", os.path.join(td, "nope.md"),
                                "--now-utc", "2026-07-22T13:37:00Z", "--json"],
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)

    def test_unreadable_or_malformed_constants_file_errors_loudly(self):
        # Invalid UTF-8 and a non-numeric blackout row are both configuration
        # failures. Neither may silently become a zero-minute blackout.
        cases = (
            ("invalid-utf8.md", b"\xff"),
            ("malformed.md", b"| `NO_BUY_FIRST_MINUTES` | `not-a-number` |\n"),
        )
        with tempfile.TemporaryDirectory() as td:
            for filename, content in cases:
                with self.subTest(filename=filename):
                    constants = os.path.join(td, filename)
                    with open(constants, "wb") as f:
                        f.write(content)
                    r = subprocess.run([sys.executable, CLOCK, "--constants", constants,
                                        "--now-utc", "2026-07-22T13:37:00Z", "--json"],
                                       capture_output=True, text=True)
                    self.assertNotEqual(r.returncode, 0)

    def test_sessions_and_weekend(self):
        pre = self.clock("2026-07-21T12:00:00Z")
        self.assertEqual(pre["session"], "pre-market")
        self.assertTrue(pre["entry_session_open"])
        after = self.clock("2026-07-21T20:30:00Z")
        self.assertEqual(after["session"], "after-hours")
        self.assertTrue(after["entry_session_open"])
        closed = self.clock("2026-07-22T01:00:00Z")
        self.assertEqual(closed["session"], "closed")
        self.assertFalse(closed["entry_session_open"])
        weekend = self.clock("2026-07-18T15:00:00Z")
        self.assertEqual(weekend["session"], "closed-weekend")
        self.assertFalse(weekend["entry_session_open"])

    def test_exchange_calendar_table_is_valid(self):
        self.assertEqual(CALENDAR_YEARS, frozenset({2026, 2027, 2028}))
        expected_closed = frozenset({
            # Published NYSE full closures, 2026
            date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
            date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
            date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
            date(2026, 12, 25),
            # 2027
            date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
            date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
            date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25),
            date(2027, 12, 24),
            # 2028 (New Year's Day is Saturday, so NYSE does not observe it)
            date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14),
            date(2028, 5, 29), date(2028, 6, 19), date(2028, 7, 4),
            date(2028, 9, 4), date(2028, 11, 23), date(2028, 12, 25),
        })
        expected_early_closes = {
            date(2026, 11, 27): 13 * 60,
            date(2026, 12, 24): 13 * 60,
            date(2027, 11, 26): 13 * 60,
            date(2028, 7, 3): 13 * 60,
            date(2028, 11, 24): 13 * 60,
        }
        self.assertEqual(CLOSED_DATES, expected_closed)
        self.assertEqual(EARLY_CLOSE_MINUTES_BY_DATE, expected_early_closes)
        self.assertFalse(CLOSED_DATES & set(EARLY_CLOSE_MINUTES_BY_DATE))
        scheduled_days = set(CLOSED_DATES) | set(EARLY_CLOSE_MINUTES_BY_DATE)
        self.assertTrue(all(day.year in CALENDAR_YEARS and day.weekday() < 5
                            for day in scheduled_days))
        self.assertTrue(all(REGULAR_OPEN_MINUTE < close < NORMAL_REGULAR_CLOSE_MINUTE
                            for close in EARLY_CLOSE_MINUTES_BY_DATE.values()))

    def test_exchange_holidays_block_new_entry_windows(self):
        # These UTC instants are 11:00 ET, inside a normal full-day core
        # session. They must still be closed on their published holidays.
        for now_utc in ("2026-04-03T15:00:00Z", "2026-06-19T15:00:00Z",
                        "2026-07-03T15:00:00Z", "2026-11-26T16:00:00Z",
                        "2026-12-25T16:00:00Z", "2027-12-24T16:00:00Z"):
            c = self.clock(now_utc)
            self.assertEqual(c["session"], "closed-holiday", now_utc)
            self.assertEqual(c["calendar_status"], "holiday", now_utc)
            self.assertFalse(c["entry_session_open"], now_utc)
            self.assertIsNone(c["minutes_since_open"], now_utc)
            self.assertFalse(c["opening_blackout"], now_utc)

    def test_early_close_limits_new_entries_to_shortened_regular_session(self):
        for premarket, before_close, at_close in (
            ("2026-11-27T14:00:00Z", "2026-11-27T17:59:00Z", "2026-11-27T18:00:00Z"),
            ("2026-12-24T14:00:00Z", "2026-12-24T17:59:00Z", "2026-12-24T18:00:00Z"),
            ("2028-07-03T13:00:00Z", "2028-07-03T16:59:00Z", "2028-07-03T17:00:00Z"),
        ):
            pre = self.clock(premarket)
            self.assertEqual(pre["session"], "pre-market", premarket)
            self.assertEqual(pre["calendar_status"], "early-close", premarket)
            self.assertFalse(pre["entry_session_open"], premarket)

            before = self.clock(before_close)
            self.assertEqual(before["session"], "regular", before_close)
            self.assertEqual(before["regular_close_et"], "13:00", before_close)
            self.assertTrue(before["entry_session_open"], before_close)

            closed = self.clock(at_close)
            self.assertEqual(closed["session"], "closed-early", at_close)
            self.assertFalse(closed["entry_session_open"], at_close)

    def test_unknown_calendar_coverage_blocks_new_entries(self):
        c = self.clock("2029-01-02T16:00:00Z")  # Tuesday, 11:00 ET
        self.assertEqual(c["session"], "calendar-unknown")
        self.assertEqual(c["calendar_status"], "unknown")
        self.assertFalse(c["entry_session_open"])
        self.assertIsNone(c["regular_close_et"])

    def test_routine_has_an_unconditional_calendar_entry_gate(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()
        self.assertIn("`python3 market_clock.py --json`", routine)
        self.assertIn("entry_session_open", routine)
        self.assertIn("exactly the JSON boolean `true`", routine)
        self.assertIn("This applies regardless of `REGULAR_HOURS_BUY_ONLY`", routine)

    def test_routine_fences_overlaps_and_rechecks_time_before_buys(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        coordination = routine.split(
            "### RUN COORDINATION — fenced single-flight lease", 1
        )[1].split("### WINDOWS / CODEX PYTHON ENVIRONMENT", 1)[0]
        self.assertIn("before `rules_version`, `get_accounts`, or ANY broker call", coordination)
        self.assertIn("`python3 run_lock.py acquire`", coordination)
        self.assertIn("`schema_version` exactly `1`", coordination)
        self.assertIn("`ok` exactly the JSON boolean `true`", coordination)
        self.assertIn("`RUN_LOCK_TOKEN`", coordination)
        self.assertIn('`reason: "active_run"`', coordination)
        self.assertIn("OVERLAP HALT", coordination)
        self.assertIn("Make no broker calls", coordination)
        self.assertIn('supersede REPORT, its "every run" status-snapshot rule', coordination)
        self.assertIn("expires after 20 minutes", coordination)
        self.assertIn("At the start of FIRST, SECOND, THIRD, FOURTH, and REPORT", coordination)
        self.assertIn("immediately before EVERY `cancel_equity_order` and `place_equity_order`", coordination)
        self.assertIn("ownership is lost: make no further broker calls or order changes", coordination)
        self.assertIn("`python3 run_lock.py release --token <RUN_LOCK_TOKEN>`", coordination)
        self.assertIn("final operational action", coordination)

        order_handling = routine.split(
            "### ORDER HANDLING — AUTONOMOUS, WITH NOTIFICATION", 1
        )[1].split("### DRY RUN", 1)[0]
        self.assertIn("renew this run's fencing lease", order_handling)
        self.assertIn("do not cancel or place", order_handling)

        pre_buy = routine.split(
            "**PRE-BUY LEASE + CLOCK REVALIDATION (EVERY candidate, including DRY RUN):**", 1
        )[1].split("11. For each remaining candidate", 1)[0]
        self.assertIn("run a fresh `market_clock.py --json`", pre_buy)
        self.assertIn("`entry_session_open` is exactly `true`", pre_buy)
        self.assertIn("`opening_blackout` is exactly `false`", pre_buy)
        self.assertIn('`session` must also be exactly `"regular"`', pre_buy)
        self.assertIn("immediately before `place_equity_order`", pre_buy)
        self.assertIn("run the fresh clock check AGAIN", pre_buy)
        self.assertIn("never place a payload reviewed for a different session", pre_buy)
        self.assertIn("Do not fall back to the START CLOCK", pre_buy)
        self.assertIn("In DRY RUN", pre_buy)

    def test_routine_full_halts_when_constants_cannot_be_read(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        preflight_start = routine.index("**Mandatory configuration preflight")
        clock_command = routine.index("`python3 market_clock.py --json`")
        self.assertLess(preflight_start, clock_command)
        preflight = routine[preflight_start:routine.index("\n\nNote the `DRY_RUN`", preflight_start)]
        self.assertIn("Before `market_clock.py`, `get_accounts`", preflight)
        self.assertIn("FULL-RUN HALT immediately", preflight)
        self.assertIn("This is NOT DRY RUN", preflight)
        self.assertIn("do not review, place, or cancel any order", preflight)
        self.assertIn("normal report, ledger, status snapshot", preflight)
        self.assertNotIn("treat it as `true`", routine)

        dry_run = routine.split("### DRY RUN", 1)[1].split("### CURRENT TIME", 1)[0]
        self.assertIn("NOT DRY RUN", dry_run)
        self.assertIn("never substitute `true`", dry_run)

    def test_routine_uses_machine_readable_scan_handoff(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        scan_phase = routine.split("6. `run_scan`", 1)[1].split("**FOURTH", 1)[0]
        self.assertIn("--json-out <scratch>/working-list.json", scan_phase)
        self.assertIn("NEW session-scoped scratch directory", scan_phase)
        self.assertIn("Machine-readable handoff (REQUIRED)", scan_phase)
        self.assertIn("working_list", scan_phase)
        self.assertIn("SOLE authority for candidate data and scan counts", scan_phase)
        self.assertIn("formatted stdout table is diagnostic-only", scan_phase)
        self.assertIn("All four counters must be non-negative JSON integers", scan_phase)
        self.assertIn("finite JSON-number", scan_phase)
        self.assertIn("no duplicate symbols", scan_phase)
        self.assertIn("skip the entry phase (Steps 8–12)", scan_phase)
        self.assertIn("any schema/value check fails", scan_phase)
        self.assertIn("do NOT fall back to formatted stdout, a stale file, or ad-hoc filtering", scan_phase)
        self.assertIn("empty `working_list: []` is valid", scan_phase)
        prefilter = routine.split("8. **Pre-filter the WORKING LIST", 1)[1].split("**The next three bullets", 1)[0]
        self.assertIn("unrounded `volume` × `last`", prefilter)
        self.assertIn("Only the FINAL RSI-enabled `evaluate_candidates.py --json-out`", routine)
        self.assertIn("Transient JSON handoffs are deliberately different", routine)

    def test_routine_uses_final_evaluator_json_as_sole_entry_authority(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        phase = routine.split("8. **Pre-filter the WORKING LIST", 1)[1].split(
            "11. For each remaining candidate", 1
        )[0]
        self.assertIn("--json-out <scratch>/pre-rsi-gates.json", phase)
        self.assertIn("PRE-RSI machine-readable handoff (REQUIRED)", phase)
        self.assertIn("`schema_version` exactly `1`", phase)
        self.assertIn("`rsi_gate_enabled` exactly the JSON boolean `false`", phase)
        self.assertIn("SOLE authority for the pre-RSI verdicts", phase)
        self.assertIn("candidate evaluation handoff failure", phase)
        self.assertIn("Do NOT use formatted stdout, a stale gate file, or ad-hoc calculations", phase)
        self.assertIn("final `buy_candidate` is exactly `true`", phase)
        self.assertIn("`rsi_gate` is exactly `\"pass\"`", phase)
        self.assertIn("`insufficient_history` is exactly `false`", phase)
        self.assertIn("`rsi_gate_enabled` must be exactly the JSON boolean `true`", phase)
        self.assertIn("SOLE authority for Step 11 and the report", phase)
        self.assertIn("NEVER buy from the PRE-RSI JSON", phase)
        self.assertIn("final candidate evaluation handoff failure", phase)
        self.assertNotIn("fall back to ad-hoc code", phase)
        self.assertNotIn("calculate how far below that high", phase)

    def test_routine_has_one_canonical_stop_market_schema(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        schema = routine.split("### BROKER ORDER OBJECTS", 1)[1].split("### TRADE LEDGER", 1)[0]
        self.assertIn("Canonical equity stop-market payload", schema)
        canonical_input = schema.split(
            "**Canonical equity stop-market payload", 1
        )[1].split("**Returned-order stop predicate", 1)[0]
        self.assertIn('"type": "stop_market"', canonical_input)
        self.assertIn('"stop_price": "<two-decimal price>"', canonical_input)
        self.assertIn('"time_in_force": "gtc"', canonical_input)
        self.assertNotIn('"trigger": "stop"', canonical_input)
        self.assertIn("Add a fresh `ref_id` only to `place_equity_order`", canonical_input)
        self.assertIn("`review_equity_order` has no `ref_id` input", canonical_input)
        self.assertNotIn('"type": "stop"', routine)
        self.assertNotIn("dollar_based_amount", routine)

        placement = routine.split("### SESSION-AWARE ORDER STYLE", 1)[1].split("### DAILY-LOSS", 1)[0]
        self.assertIn("Canonical equity stop-market payload", placement)
        self.assertIn('"dollar_amount": "<effective order size', placement)
        self.assertIn('including `type: "stop_market"` and `stop_price`, with no `trigger` input', placement)
        retry = routine.split("**Verify every stop after placing it", 1)[1].split("**ALERTS.md", 1)[0]
        self.assertIn("Canonical-stop requirement for every retry", retry)
        self.assertIn("Canonical equity stop-market payload", retry)
        audit = routine.split("**Stop-coverage audit", 1)[1].split("**Dust sweep", 1)[0]
        self.assertIn("Canonical stop for a coverage repair", audit)
        self.assertIn("Canonical equity stop-market payload", audit)
        step_12 = routine.split("12. After a buy fills:", 1)[1].split("### REPORT", 1)[0]
        self.assertIn("Canonical equity stop-market payload", step_12)
        whole_share_guard = routine.split("**Whole-share stop guard", 1)[1].split("12. After a buy fills:", 1)[0]
        self.assertIn("do NOT submit a zero-share stop", whole_share_guard)
        self.assertIn("`confirmed` or `queued` (active/working stops)", schema)
        self.assertIn('`type == "stop_market"`', schema)
        self.assertIn('`type == "stop_limit"`', schema)
        self.assertIn('`type == "market"` and `trigger == "stop"`', schema)
        self.assertIn("Never infer a stop from `stop_price` alone", schema)
        self.assertIn("makes stop classification INDETERMINATE", schema)

    def test_routine_requires_full_stop_quantity_coverage(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        audit = routine.split("**Stop-coverage audit", 1)[1].split("**Dust sweep", 1)[0]
        self.assertIn("required_stop_qty = floor(position quantity)", audit)
        self.assertIn("exact decimals", audit)
        self.assertIn("dedupe rows by `id` before counting", audit)
        self.assertIn("covered_stop_qty >= required_stop_qty", audit)
        self.assertIn("covered_stop_qty < required_stop_qty", audit)
        self.assertIn("supplemental stop for exactly `repair_qty`", audit)
        self.assertIn("covered_stop_qty > required_stop_qty", audit)
        self.assertIn("do not automatically cancel a currently protective stop", audit)

    def test_pacific_trading_day_rolls_before_utc_day(self):
        # 2026-07-22 03:00Z is still 2026-07-21 in Pacific — the date used
        # for "filled today" counting must be the Pacific one.
        clock = self.clock("2026-07-22T03:00:00Z")
        self.assertEqual(clock["date_pt"], "2026-07-21")
        self.assertEqual(clock["date_et"], "2026-07-21")

    def test_eastern_broker_date_can_lead_pacific_report_date(self):
        # 04:30Z in summer is 00:30 ET but still 21:30 PT on the prior date.
        # The daily-loss helper must use date_et; report filenames keep date_pt.
        clock = self.clock("2026-07-22T04:30:00Z")
        self.assertEqual(clock["date_et"], "2026-07-22")
        self.assertEqual(clock["date_pt"], "2026-07-21")


if __name__ == "__main__":
    unittest.main(verbosity=2)
