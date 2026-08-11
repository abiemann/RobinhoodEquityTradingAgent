#!/usr/bin/env python3
"""Regression suite for deterministic subroutines and routine contracts.

Run:  py -3 tests/test_scripts.py   (or: python3 tests/test_scripts.py)

Stdlib only — no pytest, no fixtures on disk. Each test drives the real CLI
via subprocess and asserts on --json-out / --chart-out, so the scripts are
tested exactly as the agents invoke them. Expected values for FISN/TTRX were
verified against live API data on 2026-07-07.
"""

import csv
import hashlib
import http.client
import importlib.util
import json
import math
import os
import re
import shutil
import sqlite3
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
ORDER_INTENTS = os.path.join(ROOT, "order_intents.py")
DAILY_LOSS = os.path.join(ROOT, "daily_loss.py")
VALIDATE_CONSTANTS = os.path.join(ROOT, "validate_constants.py")
DASHBOARD = os.path.join(ROOT, "dashboard", "serve.py")

BROKER_SNAPSHOT = os.path.join(ROOT, 'broker_snapshot.py')
RUN_LIFECYCLE = os.path.join(ROOT, 'run_lifecycle.py')
RESOLVE_PYTHON = os.path.join(ROOT, 'resolve_python.ps1')

sys.path.insert(0, ROOT)
from evaluate_candidates import spread_gate
import validate_constants as constants_validator
from broker_snapshot import SnapshotError, validate_generation_inputs
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


class ConstantsValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(
            os.path.join(ROOT, "constants.md"), encoding="utf-8"
        ) as handle:
            cls.valid_text = handle.read()

    @staticmethod
    def replace_value(text, name, value):
        pattern = re.compile(
            rf"^(\|\s*`{re.escape(name)}`\s*\|\s*`)[^`]*(`\s*\|)",
            re.MULTILINE,
        )
        replaced, count = pattern.subn(
            lambda match: match.group(1) + value + match.group(2),
            text,
        )
        if count != 1:
            raise AssertionError(f"expected one {name} row; replaced {count}")
        return replaced

    def invoke(self, *, text=None, raw=None, expected_success=True):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "constants.md")
            if raw is not None:
                with open(path, "wb") as handle:
                    handle.write(raw)
            else:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(self.valid_text if text is None else text)
            proc = subprocess.run(
                [
                    sys.executable,
                    VALIDATE_CONSTANTS,
                    "--constants",
                    path,
                    "--json",
                ],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
        if expected_success:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            return json.loads(proc.stdout)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(proc.stdout, "")
        self.assertIn("validate_constants.py: ERROR:", proc.stderr)
        return proc

    def test_real_file_emits_all_typed_values_and_exact_decimals(self):
        document = self.invoke()
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["status"], "valid")
        self.assertEqual(document["constant_count"], 31)
        self.assertEqual(
            set(document["values"]),
            set(constants_validator.REQUIRED_CONSTANTS),
        )
        self.assertIs(type(document["values"]["DRY_RUN"]), bool)
        self.assertIs(type(document["values"]["TOP_N"]), int)
        self.assertEqual(document["values"]["PRICE_MIN"], "2.50")
        self.assertEqual(document["values"]["RSI_INTERVAL"], "30minute")
        self.assertRegex(document["source_sha256"], r"^[0-9a-f]{64}$")

        schema_names = (
            constants_validator.BOOLEAN_CONSTANTS
            | constants_validator.STRING_CONSTANTS
            | set(constants_validator.INTEGER_BOUNDS)
            | set(constants_validator.DECIMAL_BOUNDS)
            | {"RSI_INTERVAL"}
        )
        self.assertEqual(schema_names, set(constants_validator.REQUIRED_CONSTANTS))

    def test_runtime_accepts_both_dry_run_modes_and_digit_prefixed_interval(self):
        for mode in ("true", "false"):
            with self.subTest(mode=mode):
                text = self.replace_value(self.valid_text, "DRY_RUN", mode)
                document = self.invoke(text=text)
                self.assertIs(document["values"]["DRY_RUN"], mode == "true")
                self.assertEqual(
                    document["values"]["RSI_INTERVAL"], "30minute"
                )

    def test_missing_invalid_utf8_malformed_unknown_and_duplicate_fail(self):
        invalid_utf8 = self.invoke(raw=b"\xff", expected_success=False)
        self.assertIn("not valid UTF-8", invalid_utf8.stderr)

        missing_header = self.invoke(
            text="# no constants table\n", expected_success=False
        )
        self.assertIn("expected exactly one", missing_header.stderr)

        missing_row_text = re.sub(
            r"^\| `TOP_N` .*\r?\n?", "", self.valid_text, flags=re.MULTILINE
        )
        missing_row = self.invoke(text=missing_row_text, expected_success=False)
        self.assertIn("missing required constant(s): TOP_N", missing_row.stderr)

        malformed_text = re.sub(
            r"^\| `TOP_N` \| `15` \|",
            "| `TOP_N` | 15 |",
            self.valid_text,
            count=1,
            flags=re.MULTILINE,
        )
        malformed = self.invoke(text=malformed_text, expected_success=False)
        self.assertIn("malformed constant row for TOP_N", malformed.stderr)

        extra_column_text = re.sub(
            r"^(\| `TOP_N` \| `15` \|.*)\|$",
            r"\1| `999` |",
            self.valid_text,
            count=1,
            flags=re.MULTILINE,
        )
        extra_column = self.invoke(
            text=extra_column_text, expected_success=False
        )
        self.assertIn("malformed constant row for TOP_N", extra_column.stderr)

        top_n_line = next(
            line
            for line in self.valid_text.splitlines()
            if re.match(r"^\|\s*`TOP_N`\s*\|", line)
        )
        duplicate = self.invoke(
            text=self.valid_text.rstrip() + "\n" + top_n_line + "\n",
            expected_success=False,
        )
        self.assertIn("duplicate constant TOP_N", duplicate.stderr)

        outside_table = self.invoke(
            text=self.valid_text.rstrip() + "\n\n" + top_n_line + "\n",
            expected_success=False,
        )
        self.assertIn(
            "constant-like row TOP_N appears outside", outside_table.stderr
        )

        unknown = self.invoke(
            text=self.valid_text.rstrip()
            + "\n| `TYPO_CONSTANT` | `1` | typo |\n",
            expected_success=False,
        )
        self.assertIn("unexpected constant TYPO_CONSTANT", unknown.stderr)

    def test_invalid_literal_forms_and_ranges_name_the_constant(self):
        cases = (
            ("DRY_RUN", "TRUE", "must be exactly true or false"),
            ("TOP_N", "14.0", "must be a base-10 integer"),
            ("PRICE_MIN", "NaN", "plain nonnegative decimal"),
            ("PRICE_MIN", "1e2", "plain nonnegative decimal"),
            ("PRICE_MIN", "02.5", "plain nonnegative decimal"),
            ("RSI_INTERVAL", "30 minute", "must be one of"),
            ("NO_BUY_FIRST_MINUTES", "391", "must be <= 390"),
            ("AGENTIC_ACCOUNT_NAME", '""', "nonempty trimmed string"),
            (
                "AGENTIC_ACCOUNT_NAME",
                '"\\ud800"',
                "unpaired Unicode surrogates",
            ),
        )
        for name, value, reason in cases:
            with self.subTest(name=name, value=value):
                text = self.replace_value(self.valid_text, name, value)
                proc = self.invoke(text=text, expected_success=False)
                self.assertIn(name, proc.stderr)
                self.assertIn(reason, proc.stderr)

        oversized = self.replace_value(
            self.valid_text, "TOP_N", "9" * 5000
        )
        proc = self.invoke(text=oversized, expected_success=False)
        self.assertIn("TOP_N integer literal exceeds the supported size", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_cross_field_safety_constraints_fail_closed(self):
        cases = (
            ("PRICE_MIN", "5", "PRICE_MIN must be < PRICE_MAX"),
            (
                "RSI_OVERSOLD",
                "61",
                "RSI_OVERSOLD must be <= RSI_MAX_ENTRY",
            ),
            (
                "RSI_CONFIRM_BARS",
                "6",
                "RSI_CONFIRM_BARS must be <= RSI_LOOKBACK_BARS",
            ),
            (
                "MAX_SPREAD_BUY_PCT",
                "3.5",
                "MAX_SPREAD_BUY_PCT must be < STOP_LOSS_PCT",
            ),
            (
                "BUY_SIZE_PCT",
                "21",
                "BUY_SIZE_PCT must be <= MAX_POSITION_PCT",
            ),
        )
        for name, value, reason in cases:
            with self.subTest(name=name):
                text = self.replace_value(self.valid_text, name, value)
                proc = self.invoke(text=text, expected_success=False)
                self.assertIn(reason, proc.stderr)

    def test_crlf_and_shell_metacharacters_are_inert_data(self):
        lf = self.valid_text.replace("\r\n", "\n")
        crlf = lf.replace("\n", "\r\n")
        lf_values = self.invoke(text=lf)["values"]
        crlf_values = self.invoke(text=crlf)["values"]
        self.assertEqual(lf_values, crlf_values)

        title = 'Agentic; $(Get-Process) & "quoted"'
        encoded = json.dumps(title)
        text = self.replace_value(
            self.valid_text, "AGENTIC_ACCOUNT_NAME", encoded
        )
        document = self.invoke(text=text)
        self.assertEqual(document["values"]["AGENTIC_ACCOUNT_NAME"], title)


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

    def test_async_cancel_and_partial_rest_cancelled_states_are_reconciled(self):
        result = self.invoke(
            positions=[self.page(
                "positions", [self.position("PART", "2", "2")]
            )],
            orders=[self.page("orders", [
                self.order(
                    "partial-rest-cancelled",
                    "PART",
                    "buy",
                    [self.execution("partial-fill", "9", "2")],
                    state="partially_filled_rest_cancelled",
                ),
                self.order(
                    "cancel-pending",
                    "WAIT",
                    "buy",
                    [],
                    state="pending_cancelled",
                    cumulative_quantity="0",
                ),
            ])],
            quotes=[{"data": {"results": [
                self.quote("PART", "10", "8")
            ]}}],
        )
        self.assertEqual(result["daily_pnl"], "2")
        self.assertEqual(result["reconciliation"]["unique_order_count"], 2)
        self.assertEqual(result["reconciliation"]["today_execution_count"], 1)

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
        lifecycle = routine.split(
            "### INVOCATION LIFECYCLE", 1
        )[1].split("**Mandatory configuration preflight", 1)[0]
        self.assertLess(
            routine.index("run_lifecycle.py start"),
            routine.index("validate_constants.py --json"),
        )
        self.assertIn("finish the invocation exactly once", lifecycle.lower())
        self.assertIn("<START CLOCK pt_iso>", lifecycle)
        self.assertIn("clock-unavailable", lifecycle)
        for classification in (
            "completed",
            "risk-halt",
            "snapshot-failure",
            "overlap",
            "configuration-halt",
            "coordination-halt",
            "lease-lost",
            "final-status-unavailable",
        ):
            self.assertIn(classification, lifecycle)
        self.assertIn("MUST NOT be emitted", lifecycle)
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
            "separate generation-specific FINAL set",
            block,
        )
        self.assertIn("Never evaluate with the earlier discovery files", block)
        self.assertIn(
            "--portfolio <sealed FINAL portfolio file>",
            block,
        )
        self.assertIn("harness-created tool-result file/resource", block)
        self.assertIn("`fileChange` / file-edit / apply-patch", block)
        self.assertIn("Do not require a tool literally named `Write`", block)
        self.assertIn("NEVER invent a filename, guess a temp location", block)
        self.assertIn("broker_snapshot.py source-preflight", block)
        self.assertIn("including all `data`, pagination, transport-envelope, and `guide` fields", block)
        matrix = block.split("**STAGING COMMAND MATRIX", 1)[1].split(
            "For positions and orders, stage each returned page", 1
        )[0]

        def matrix_command(label):
            match = re.search(
                rf"- {re.escape(label)}: `([^`]+)`",
                matrix,
            )
            self.assertIsNotNone(match, label)
            return match.group(1)

        portfolio_command = matrix_command("Portfolio template")
        quotes_command = matrix_command("Quotes template (one batch or aggregate)")
        page_command = matrix_command("Positions/orders page template")
        aggregate_command = matrix_command("Positions/orders aggregate template")
        self.assertIn("--kind portfolio", portfolio_command)
        self.assertIn("--kind quotes", quotes_command)
        self.assertIn("--kind <positions|orders>", page_command)
        self.assertIn("--kind <positions|orders>", aggregate_command)
        for command in (portfolio_command, quotes_command):
            self.assertNotIn("--request-cursor", command)
            self.assertNotIn("--allow-more", command)
            self.assertNotIn("FIRST", command)
        self.assertIn("--request-cursor <FIRST|exact prior next cursor>", page_command)
        self.assertIn("[--allow-more]", page_command)
        self.assertIn("--request-cursor FIRST", aggregate_command)
        self.assertIn("--request-cursor '<exact later cursor>'", aggregate_command)
        self.assertNotIn("--allow-more", aggregate_command)
        self.assertIn(
            "Never use one generic or polymorphic staging wrapper",
            matrix,
        )
        self.assertIn("inspect the literal argv tokens", matrix)
        self.assertIn(
            "not a universal first-call, first-response, or first-file marker",
            matrix,
        )
        self.assertGreaterEqual(block.count("--snapshot-generation <A|B>"), 2)
        self.assertIn("shared set ID", block)
        self.assertIn("provenance sidecar", block)
        self.assertIn("aggregate-seal", block)
        self.assertIn("abandon ALL of A", block)
        self.assertIn("exactly one whole generation B", block)
        self.assertIn("never combine generations", block)
        self.assertIn("never run generation C", block)
        self.assertIn("`snapshot-failure` / `snapshot-second-attempt-failed`", block)
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
        self.assertIn("<clear|tripped|indeterminate|not-evaluated>", snapshot)
        self.assertIn("<integer|null>", snapshot)

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
        scratch_preflight = routine.index(
            "broker_snapshot.py preflight --scratch <absolute scratch>"
        )
        self.assertLess(
            scratch_creation,
            routine.index("### DAILY-LOSS CIRCUIT BREAKER"),
        )
        self.assertLess(scratch_creation, scratch_preflight)
        self.assertLess(scratch_preflight, routine.index("### DAILY-LOSS CIRCUIT BREAKER"))
        scan_phase = routine.split("6. `run_scan`", 1)[1].split(
            "**FOURTH", 1
        )[0]
        self.assertIn(
            "Reuse the NEW session-scoped scratch directory already created",
            scan_phase,
        )

    def test_routine_status_snapshot_uses_one_post_mutation_generation(self):
        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()

        step_12 = routine.index('12. After the buy intent is terminal')
        refresh_start = routine.index(
            '**FINAL STATUS REFRESH — ONE COHERENT POST-MUTATION GENERATION:**'
        )
        publish_start = routine.index('**Publish the STATUS SNAPSHOT', refresh_start)
        self.assertLess(step_12, refresh_start)
        self.assertLess(refresh_start, publish_start)

        refresh = routine[refresh_start:publish_start]
        for mutation in (
            'profit-take',
            'dust sweep',
            'stop fill/repair',
            'entry buy',
            'partial fill',
            'cancellation',
            'replacement order',
        ):
            self.assertIn(mutation, refresh)
        self.assertIn('`confirmed`/`queued` protective stops', refresh)
        self.assertIn('mandatory and read-only in LIVE and DRY RUN', refresh)
        self.assertIn('every page of `get_equity_positions` as the BEFORE census', refresh)
        self.assertIn('`get_portfolio`', refresh)
        self.assertIn('`get_realized_pnl`', refresh)
        self.assertIn('every page of `get_equity_orders`', refresh)
        self.assertIn('with NO state filter', refresh)
        self.assertIn('`get_equity_positions` again as the AFTER census', refresh)
        self.assertIn('held-position fingerprint', refresh)
        self.assertIn('BEFORE and AFTER fingerprints to match exactly', refresh)
        self.assertIn('`quoted_equity = sum(quantity * current_price)`', refresh)
        self.assertIn('0.01 * max(abs(equity_value), abs(quoted_equity))', refresh)
        self.assertIn('require `equity_value == 0` exactly when', refresh.lower())
        self.assertIn('zero `equity_value` paired with a held position', refresh)
        self.assertIn('normalized authoritative buying-power scalar', refresh)
        self.assertIn(
            '`account.buying_power` set to the normalized authoritative scalar',
            refresh,
        )
        self.assertIn('generic immediate one-retry rule', refresh)
        self.assertIn('new generation must NOT resurrect the twice-failed read', refresh)
        self.assertIn('zero-equity tiebreak', refresh)
        self.assertIn('Only when every required read succeeded', refresh)
        self.assertIn(
            'discard the ENTIRE generation and perform exactly one new generation',
            refresh,
        )
        self.assertIn('There is no third generation', refresh)
        self.assertIn('never combine values across generations', refresh)
        self.assertIn('SOLE source of all four `account` fields', refresh)
        self.assertIn(
            'SOLE non-null source of `realized_pnl_today`',
            refresh,
        )
        self.assertIn(
            'Never splice fields or reuse FIRST, SECOND/DAILY-LOSS, pre-buy, or Step 12',
            refresh,
        )
        self.assertIn('do NOT create a new `rhmra-status-*.json`', refresh)
        self.assertIn('do NOT call release', refresh)
        self.assertIn('lease remains valid', refresh)
        self.assertIn('release normally', refresh)
        self.assertIn('previous truthful snapshot', refresh)

        snapshot = routine[publish_start : routine.index('The filename is exactly:', publish_start)]
        self.assertIn(
            'successful FINAL STATUS REFRESH is the exclusive source for `account` and `positions`',
            snapshot,
        )
        self.assertIn('explicit final-status-unavailable path', snapshot)
        self.assertIn('figure from the FINAL STATUS REFRESH', snapshot)
        self.assertNotIn('get_realized_pnl figure from SECOND', snapshot)
        self.assertNotIn('never make a new API call for the snapshot', snapshot)

        normalization = routine.split(
            '**Portfolio buying-power normalization', 1
        )[1].split('**PRE-SECOND ENTRY-FEASIBILITY GATES', 1)[0]
        self.assertIn('`data.buying_power.buying_power`', normalization)
        self.assertIn('legacy scalar', normalization)
        self.assertIn(
            'Never substitute or combine `unleveraged_buying_power`',
            normalization,
        )
        self.assertIn('status `account.buying_power`', normalization)


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

    def test_historicals_accept_saved_mcp_structured_content_envelope(self):
        payload = {"data": {"results": [{"symbol": "FISN", "bars": FISN_BARS}]}}
        wrapped = {
            "content": [{"type": "text", "text": "saved tool result"}],
            "structuredContent": payload,
            "isError": False,
        }
        fisn = self.run_eval(wrapped, {"FISN": 9.843})["FISN"]
        self.assertTrue(fisn["buy_candidate"])
        self.assertAlmostEqual(fisn["recent_high"], 11.43, delta=0.001)

    def test_historicals_reject_malformed_mcp_envelopes(self):
        payload = {"data": {"results": [{"symbol": "FISN", "bars": FISN_BARS}]}}
        bad_envelopes = (
            {"structuredContent": payload, "isError": True},
            {"structuredContent": payload, "isError": "false"},
            {"structuredContent": "not an object", "data": payload["data"]},
            {"content": [{"type": "text", "text": json.dumps(payload)}]},
        )
        for wrapped in bad_envelopes:
            with self.subTest(wrapper_keys=list(wrapped)):
                with self.assertRaises(AssertionError):
                    self.run_eval(wrapped, {"FISN": 9.843})

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
    def run_filter(self, rows, top_n=15, mcp_envelope=False, mcp_error=False):
        with tempfile.TemporaryDirectory() as td:
            scan = os.path.join(td, "scan.json")
            out = os.path.join(td, "out.json")
            document = {
                "data": {"result": {"results": rows, "total_items": len(rows)}}
            }
            if mcp_envelope:
                document = {
                    "content": [{"type": "text", "text": "saved tool result"}],
                    "structuredContent": document,
                    "isError": mcp_error,
                }

            with open(scan, "w", encoding="utf-8") as f:
                json.dump(document, f)
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

    def test_accepts_saved_mcp_structured_content_envelope(self):
        data = self.run_filter(
            [scan_row("KEEP", 4.45, 0.1528, 557.75)],
            mcp_envelope=True,
        )
        self.assertEqual(
            [row["symbol"] for row in data["working_list"]],
            ["KEEP"],
        )

    def test_rejects_mcp_error_envelope(self):
        with self.assertRaises(AssertionError):
            self.run_filter(
                [scan_row("KEEP", 4.45, 0.1528, 557.75)],
                mcp_envelope=True,
                mcp_error=True,
            )

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


class OrderIntentTests(unittest.TestCase):
    RUN_TOKEN = "11111111-1111-4111-8111-111111111111"
    OTHER_RUN_TOKEN = "22222222-2222-4222-8222-222222222222"
    BASELINE_ORDER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def invoke(self, state_file, action, *args, expected_success=True, now=None):
        command = [
            sys.executable,
            ORDER_INTENTS,
            action,
            "--state-file",
            state_file,
        ]
        if now is not None:
            command += ["--now-utc", now]
        command += list(args)
        proc = subprocess.run(
            command, capture_output=True, text=True, cwd=ROOT
        )
        try:
            document = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f"order_intents.py emitted non-JSON stdout: {proc.stdout!r}; "
                f"stderr={proc.stderr!r}"
            )
        self.assertEqual(proc.stderr, "")
        if expected_success:
            self.assertEqual(proc.returncode, 0, document)
            self.assertTrue(document["ok"])
        else:
            self.assertNotEqual(proc.returncode, 0, document)
            self.assertFalse(document["ok"])
            self.assertEqual(document["reason"], "order_intent_state_error")
        return document

    @staticmethod
    def write_json(directory, name, value):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def intent(
        self,
        *,
        purpose="dip-buy",
        order=None,
        run_token=None,
        position_quantity="0",
        baseline_ids=None,
        replaces_intent_id=None,
    ):
        if order is None:
            order = {
                "symbol": "TEST",
                "side": "buy",
                "type": "market",
                "dollar_amount": "100.00",
                "market_hours": "regular_hours",
                "time_in_force": "gfd",
            }
        return {
            "schema_version": 1,
            "account_name": "Agentic",
            "run_token": run_token or self.RUN_TOKEN,
            "run_start_utc": "2026-07-31T16:00:00Z",
            "rules_version": "abcdef1",
            "constants_sha256": "a" * 64,
            "purpose": purpose,
            "replaces_intent_id": replaces_intent_id,
            "order": order,
            "baseline": {
                "observed_at_utc": "2026-07-31T16:00:01Z",
                "position_quantity": position_quantity,
                "symbol_order_ids": (
                    [self.BASELINE_ORDER_ID]
                    if baseline_ids is None
                    else baseline_ids
                ),
            },
        }

    @staticmethod
    def broker_order(
        *,
        order_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        symbol="TEST",
        side="buy",
        order_type="market",
        trigger="immediate",
        state="filled",
        quantity=None,
        dollar_amount="100.00",
        cumulative="5",
        executions=None,
        price=None,
        stop_price=None,
        market_hours="regular_hours",
        time_in_force="gfd",
        created_at="2026-07-31T16:00:02Z",
    ):
        if executions is None:
            executions = [] if Decimal(cumulative) == 0 else [
                {
                    "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "price": "20.00",
                    "quantity": cumulative,
                    "timestamp": "2026-07-31T16:00:03.12Z",
                    "fees": "0.00",
                }
            ]
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "trigger": trigger,
            "state": state,
            "quantity": quantity,
            "dollar_based_amount": (
                None
                if dollar_amount is None
                else {"amount": dollar_amount, "currency_code": "USD"}
            ),
            "cumulative_quantity": cumulative,
            "executions": executions,
            "price": price,
            "stop_price": stop_price,
            "market_hours": market_hours,
            "time_in_force": time_in_force,
            "placed_agent": "agentic",
            "created_at": created_at,
        }

    def prepare(self, td, state, intent=None, name="intent.json"):
        path = self.write_json(td, name, intent or self.intent())
        return self.invoke(
            state,
            "prepare",
            "--intent",
            path,
            now="2026-07-31T16:00:01Z",
        )

    def test_same_ref_is_persisted_for_one_immediate_retry_only(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            ref_id = prepared["ref_id"]
            self.assertRegex(
                ref_id,
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
                r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            )
            self.assertEqual(prepared["intent_id"], ref_id)
            self.assertEqual(prepared["place_order"]["ref_id"], ref_id)

            begun = self.invoke(
                state,
                "begin",
                "--intent-id",
                ref_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            self.assertEqual(begun["attempt"], 1)
            self.assertEqual(begun["place_order"], prepared["place_order"])
            self.assertEqual(
                begun["baseline_sha256"], prepared["baseline_sha256"]
            )
            self.assertEqual(begun["intent_sha256"], prepared["intent_sha256"])
            self.invoke(
                state,
                "mark-unknown",
                "--intent-id",
                ref_id,
                "--code",
                "timeout",
                now="2026-07-31T16:00:03Z",
            )
            premature = self.invoke(
                state,
                "retry",
                "--intent-id",
                ref_id,
                "--run-token",
                self.RUN_TOKEN,
                expected_success=False,
                now="2026-07-31T16:00:04Z",
            )
            self.assertIn("no-match reconciliation", premature["detail"])
            empty_orders = self.write_json(
                td,
                "retry-orders.json",
                {"data": {"orders": [], "next": None}},
            )
            empty_positions = self.write_json(
                td,
                "retry-positions.json",
                {"data": {"positions": [], "next": None}},
            )
            reconciled = self.invoke(
                state,
                "observe",
                "--intent-id",
                ref_id,
                "--orders",
                empty_orders,
                "--positions",
                empty_positions,
                "--as-of-utc",
                "2026-07-31T16:00:05Z",
                now="2026-07-31T16:00:05Z",
            )
            self.assertFalse(reconciled["matched"])
            self.assertEqual(reconciled["status"], "unknown")
            pending = self.invoke(
                state, "pending", "--run-token", self.RUN_TOKEN
            )
            self.assertTrue(pending["intents"][0]["same_run_retry_available"])
            retried = self.invoke(
                state,
                "retry",
                "--intent-id",
                ref_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:06Z",
            )
            self.assertEqual(retried["attempt"], 2)
            self.assertEqual(retried["place_order"], prepared["place_order"])
            self.assertEqual(
                retried["baseline_sha256"], prepared["baseline_sha256"]
            )
            self.assertEqual(
                retried["intent_sha256"], prepared["intent_sha256"]
            )
            self.invoke(
                state,
                "mark-unknown",
                "--intent-id",
                ref_id,
                "--code",
                "transport_error",
                now="2026-07-31T16:00:07Z",
            )
            third = self.invoke(
                state,
                "retry",
                "--intent-id",
                ref_id,
                "--run-token",
                self.RUN_TOKEN,
                expected_success=False,
                now="2026-07-31T16:00:08Z",
            )
            self.assertIn("one same-run", third["detail"])
            stale = self.invoke(
                state,
                "retry",
                "--intent-id",
                ref_id,
                "--run-token",
                self.OTHER_RUN_TOKEN,
                expected_success=False,
                now="2026-07-31T16:30:00Z",
            )
            self.assertIn("cross-run replay is forbidden", stale["detail"])

    def test_acknowledgement_tracks_split_fill_and_normalized_stop(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            intent_id = prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            executions = [
                {
                    "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "price": "1.48",
                    "quantity": "67",
                    "timestamp": "2026-07-31T16:00:03.1Z",
                    "fees": "0",
                },
                {
                    "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "price": "1.50",
                    "quantity": "0.344596",
                    "timestamp": "2026-07-31T16:00:03.22Z",
                    "fees": "0.01",
                },
            ]
            response = self.write_json(
                td,
                "response.json",
                {
                    "data": {
                        "order": self.broker_order(
                            cumulative="67.344596", executions=executions
                        )
                    }
                },
            )
            ack = self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                intent_id,
                "--response",
                response,
                now="2026-07-31T16:00:04Z",
            )
            self.assertEqual(ack["status"], "resolved")
            self.assertEqual(ack["filled_quantity"], "67.344596")
            self.assertEqual(ack["whole_filled_quantity"], "67")
            expected_average = (
                Decimal("67") * Decimal("1.48")
                + Decimal("0.344596") * Decimal("1.50")
            ) / Decimal("67.344596")
            self.assertEqual(ack["average_fill_price"], format(expected_average, "f"))
            self.assertEqual(
                ack["last_execution_at"], "2026-07-31T16:00:03.22Z"
            )
            self.assertTrue(ack["requires_stop_audit"])
            self.assertTrue(ack["ledger_ready"])

            stop_order = {
                "symbol": "TEST",
                "side": "sell",
                "type": "stop_market",
                "quantity": "67",
                "stop_price": "1.40",
                "market_hours": "regular_hours",
                "time_in_force": "gtc",
            }
            stop_intent = self.intent(
                purpose="initial-stop",
                order=stop_order,
                position_quantity="67.344596",
            )
            stop_prepared = self.prepare(
                td, state, stop_intent, "stop-intent.json"
            )
            stop_id = stop_prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                stop_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:05Z",
            )
            stop_response = self.write_json(
                td,
                "stop-response.json",
                {
                    "data": {
                        "order": self.broker_order(
                            order_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
                            side="sell",
                            order_type="market",
                            trigger="stop",
                            state="confirmed",
                            quantity="67.000000",
                            dollar_amount=None,
                            cumulative="0",
                            stop_price="1.400000",
                            time_in_force="gtc",
                        )
                    }
                },
            )
            stop_ack = self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                stop_id,
                "--response",
                stop_response,
                now="2026-07-31T16:00:06Z",
            )
            self.assertEqual(stop_ack["status"], "resolved")
            self.assertEqual(stop_ack["outcome"], "active_stop")
            self.assertEqual(stop_ack["stop_coverage_quantity"], "67.000000")

    def test_crash_recovery_can_record_no_match_from_submitting(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            intent_id = prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            orders = self.write_json(
                td,
                "crash-recovery-orders.json",
                {"data": {"orders": [], "next": None}},
            )
            positions = self.write_json(
                td,
                "crash-recovery-positions.json",
                {"data": {"positions": [], "next": None}},
            )
            recovered = self.invoke(
                state,
                "observe",
                "--intent-id",
                intent_id,
                "--orders",
                orders,
                "--positions",
                positions,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertFalse(recovered["matched"])
            self.assertEqual(recovered["status"], "unknown")
            pending = self.invoke(
                state, "pending", "--run-token", self.OTHER_RUN_TOKEN
            )
            self.assertEqual(
                pending["intents"][0]["last_error_code"],
                "recovery_no_match",
            )
            self.assertFalse(
                pending["intents"][0]["same_run_retry_available"]
            )

    def test_partial_buy_is_cancelled_then_observed_at_final_quantity(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            order = {
                "symbol": "TEST",
                "side": "buy",
                "type": "market",
                "quantity": "10",
                "market_hours": "regular_hours",
                "time_in_force": "gfd",
            }
            prepared = self.prepare(td, state, self.intent(order=order))
            intent_id = prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            partial = self.broker_order(
                state="partially_filled",
                quantity="10.000000",
                dollar_amount=None,
                cumulative="3.000000",
            )
            partial_response = self.write_json(
                td, "partial.json", {"data": {"order": partial}}
            )
            ack = self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                intent_id,
                "--response",
                partial_response,
                now="2026-07-31T16:00:04Z",
            )
            self.assertEqual(ack["status"], "partially_filled")
            self.assertEqual(ack["remaining_quantity"], "7.000000")
            self.assertTrue(ack["cancel_unfilled_remainder"])
            self.assertFalse(ack["ledger_ready"])

            final_executions = [
                partial["executions"][0],
                {
                    "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "price": "20.01",
                    "quantity": "1.000000",
                    "timestamp": "2026-07-31T16:00:05Z",
                    "fees": "0",
                },
            ]
            final_order = self.broker_order(
                state="partially_filled_rest_cancelled",
                quantity="10.000000",
                dollar_amount=None,
                cumulative="4.000000",
                executions=final_executions,
            )
            orders_file = self.write_json(
                td, "orders.json", {"data": {"orders": [final_order], "next": None}}
            )
            positions_file = self.write_json(
                td,
                "positions.json",
                {"data": {"positions": [{"symbol": "TEST", "quantity": "4"}], "next": None}},
            )
            observed = self.invoke(
                state,
                "observe",
                "--intent-id",
                intent_id,
                "--orders",
                orders_file,
                "--positions",
                positions_file,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertEqual(observed["status"], "resolved")
            self.assertEqual(observed["filled_quantity"], "4.000000")
            self.assertEqual(observed["position_quantity"], "4")
            self.assertTrue(observed["requires_stop_audit"])
            self.assertTrue(observed["ledger_ready"])

    def test_unknown_reconciles_only_one_post_baseline_fingerprint(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            intent_id = prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            self.invoke(
                state,
                "mark-unknown",
                "--intent-id",
                intent_id,
                "--code",
                "transport_error",
                now="2026-07-31T16:00:03Z",
            )
            candidate = self.broker_order()
            orders_file = self.write_json(
                td, "orders.json", {"data": {"orders": [candidate], "next": None}}
            )
            positions_file = self.write_json(
                td,
                "positions.json",
                {"data": {"positions": [{"symbol": "TEST", "quantity": "5"}], "next": None}},
            )
            observed = self.invoke(
                state,
                "observe",
                "--intent-id",
                intent_id,
                "--orders",
                orders_file,
                "--positions",
                positions_file,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertTrue(observed["matched"])
            self.assertEqual(
                observed["match_reason"], "unique_post_baseline_fingerprint"
            )

            second_state = os.path.join(td, "second.sqlite3")
            second = self.prepare(td, second_state, name="second-intent.json")
            second_id = second["intent_id"]
            self.invoke(
                second_state,
                "begin",
                "--intent-id",
                second_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            duplicate = dict(candidate)
            duplicate["id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            duplicate["created_at"] = "2026-07-31T16:00:04Z"
            duplicate["executions"] = [dict(candidate["executions"][0])]
            duplicate["executions"][0]["id"] = "99999999-9999-4999-8999-999999999999"
            ambiguous_orders = self.write_json(
                td,
                "ambiguous-orders.json",
                {"data": {"orders": [candidate, duplicate], "next": None}},
            )
            ambiguous = self.invoke(
                second_state,
                "observe",
                "--intent-id",
                second_id,
                "--orders",
                ambiguous_orders,
                "--positions",
                positions_file,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertFalse(ambiguous["matched"])
            self.assertEqual(ambiguous["status"], "indeterminate")
            self.assertEqual(len(ambiguous["candidate_order_ids"]), 2)
            empty_orders = self.write_json(
                td,
                "no-matches.json",
                {"data": {"orders": [], "next": None}},
            )
            still_indeterminate = self.invoke(
                second_state,
                "observe",
                "--intent-id",
                second_id,
                "--orders",
                empty_orders,
                "--positions",
                positions_file,
                "--as-of-utc",
                "2026-07-31T16:02:00Z",
                now="2026-07-31T16:02:00Z",
            )
            self.assertEqual(still_indeterminate["status"], "indeterminate")
            pending = self.invoke(
                second_state,
                "pending",
                "--run-token",
                self.RUN_TOKEN,
            )
            self.assertFalse(pending["intents"][0]["same_run_retry_available"])

    def test_payload_validation_and_corrupt_journal_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            bad = self.intent()
            bad["order"]["account_number"] = "sensitive-account"
            bad_path = self.write_json(td, "bad.json", bad)
            rejected = self.invoke(
                state,
                "prepare",
                "--intent",
                bad_path,
                expected_success=False,
                now="2026-07-31T16:00:01Z",
            )
            self.assertIn("account_number", rejected["detail"])
            self.assertFalse(os.path.exists(state) and b"sensitive-account" in open(state, "rb").read())

            corrupt = os.path.join(td, "corrupt.sqlite3")
            with open(corrupt, "wb") as handle:
                handle.write(b"not sqlite")
            result = self.invoke(
                corrupt, "check", expected_success=False,
                now="2026-07-31T16:00:01Z",
            )
            self.assertIn("database", result["detail"].lower())

            empty = os.path.join(td, "empty.sqlite3")
            open(empty, "wb").close()
            result = self.invoke(
                empty, "check", expected_success=False,
                now="2026-07-31T16:00:01Z",
            )
            self.assertIn("existing journal is empty", result["detail"])

            truncated = os.path.join(td, "missing-schema.sqlite3")
            connection = sqlite3.connect(truncated)
            connection.execute("CREATE TABLE unrelated(value TEXT)")
            connection.commit()
            connection.close()
            result = self.invoke(
                truncated, "check", expected_success=False,
                now="2026-07-31T16:00:01Z",
            )
            self.assertIn("unsafe schema", result["detail"])

    def test_helper_rejects_order_shapes_the_routine_never_authorizes(self):
        invalid_intents = {
            "stop-limit": self.intent(
                purpose="initial-stop",
                order={
                    "symbol": "TEST", "side": "sell", "type": "stop_limit",
                    "quantity": "5", "limit_price": "8.90",
                    "stop_price": "9.00", "market_hours": "regular_hours",
                    "time_in_force": "gtc",
                },
            ),
            "fractional-stop": self.intent(
                purpose="initial-stop",
                order={
                    "symbol": "TEST", "side": "sell", "type": "stop_market",
                    "quantity": "5.5", "stop_price": "9.00",
                    "market_hours": "regular_hours", "time_in_force": "gtc",
                },
            ),
            "dollar-sell": self.intent(
                purpose="profit-take",
                order={
                    "symbol": "TEST", "side": "sell", "type": "market",
                    "dollar_amount": "50", "market_hours": "regular_hours",
                    "time_in_force": "gfd",
                },
            ),
            "gtc-entry": self.intent(order={
                "symbol": "TEST", "side": "buy", "type": "market",
                "dollar_amount": "100", "market_hours": "regular_hours",
                "time_in_force": "gtc",
            }),
            "all-day-entry": self.intent(order={
                "symbol": "TEST", "side": "buy", "type": "limit",
                "quantity": "10", "limit_price": "10",
                "market_hours": "all_day_hours", "time_in_force": "gfd",
            }),
            "fractional-extended": self.intent(order={
                "symbol": "TEST", "side": "buy", "type": "limit",
                "quantity": "10.5", "limit_price": "10",
                "market_hours": "extended_hours", "time_in_force": "gfd",
            }),
            "regular-limit-entry": self.intent(order={
                "symbol": "TEST", "side": "buy", "type": "limit",
                "quantity": "10", "limit_price": "10",
                "market_hours": "regular_hours", "time_in_force": "gfd",
            }),
        }
        with tempfile.TemporaryDirectory() as td:
            for name, intent in invalid_intents.items():
                with self.subTest(name=name):
                    state = os.path.join(td, f"{name}.sqlite3")
                    path = self.write_json(td, f"{name}.json", intent)
                    result = self.invoke(
                        state,
                        "prepare",
                        "--intent",
                        path,
                        expected_success=False,
                        now="2026-07-31T16:00:01Z",
                    )
                    self.assertIn("intent.order", result["detail"])

    def test_only_operator_can_resolve_no_submission_and_abandon_is_audited(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            first = self.prepare(td, state)
            first_id = first["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                first_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            self.invoke(
                state,
                "mark-unknown",
                "--intent-id",
                first_id,
                "--code",
                "unverified_rejection",
                now="2026-07-31T16:00:03Z",
            )
            empty_orders = self.write_json(
                td,
                "rejected-orders.json",
                {"data": {"orders": [], "next": None}},
            )
            empty_positions = self.write_json(
                td,
                "rejected-positions.json",
                {"data": {"positions": [], "next": None}},
            )
            self.invoke(
                state,
                "observe",
                "--intent-id",
                first_id,
                "--orders",
                empty_orders,
                "--positions",
                empty_positions,
                "--as-of-utc",
                "2026-07-31T16:00:04Z",
                now="2026-07-31T16:00:04Z",
            )
            denied_retry = self.invoke(
                state,
                "retry",
                "--intent-id",
                first_id,
                "--run-token",
                self.RUN_TOKEN,
                expected_success=False,
                now="2026-07-31T16:00:05Z",
            )
            self.assertIn("explicit rejection", denied_retry["detail"])
            resolved = self.invoke(
                state,
                "operator-resolve-not-submitted",
                "--intent-id",
                first_id,
                "--note",
                "human verified in Robinhood that no order was created",
                now="2026-07-31T16:00:06Z",
            )
            self.assertEqual(resolved["status"], "resolved")
            self.assertFalse(resolved["blocking"])

            second = self.prepare(td, state, name="second.json")
            abandoned = self.invoke(
                state,
                "abandon-prepared",
                "--intent-id",
                second["intent_id"],
                "--note",
                "fresh run revalidated and did not dispatch this intent",
                now="2026-07-31T16:30:00Z",
            )
            self.assertEqual(abandoned["status"], "abandoned")
            status = self.invoke(state, "check")
            self.assertEqual(status["pending_count"], 0)
            self.assertGreaterEqual(status["event_count"], 5)

    def test_persisted_baseline_events_and_fills_fail_closed_on_regression(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            intent_id = prepared["intent_id"]

            connection = sqlite3.connect(state)
            connection.execute(
                "UPDATE intents SET baseline_json = ? WHERE intent_id = ?",
                ('{"observed_at_utc":"2026-07-31T16:00:01Z",'
                 '"position_quantity":"99","symbol_order_ids":[]}', intent_id),
            )
            connection.commit()
            connection.close()
            corrupted = self.invoke(state, "check", expected_success=False)
            self.assertIn("baseline hash", corrupted["detail"])

            state = os.path.join(td, "monotonic.sqlite3")
            prepared = self.prepare(td, state, name="monotonic.json")
            intent_id = prepared["intent_id"]
            self.invoke(
                state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            response = self.write_json(
                td,
                "partial-response.json",
                {"data": {"order": self.broker_order(
                    state="partially_filled", cumulative="5"
                )}},
            )
            self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                intent_id,
                "--response",
                response,
                now="2026-07-31T16:00:03Z",
            )
            stale_order = self.broker_order(
                state="partially_filled", cumulative="4"
            )
            orders_file = self.write_json(
                td,
                "stale-orders.json",
                {"data": {"orders": [stale_order], "next": None}},
            )
            positions_file = self.write_json(
                td,
                "stale-positions.json",
                {"data": {"positions": [
                    {"symbol": "TEST", "quantity": "5"}
                ], "next": None}},
            )
            regression = self.invoke(
                state,
                "observe",
                "--intent-id",
                intent_id,
                "--orders",
                orders_file,
                "--positions",
                positions_file,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                expected_success=False,
                now="2026-07-31T16:01:00Z",
            )
            self.assertIn("reduce the persisted cumulative fill", regression["detail"])

            connection = sqlite3.connect(state)
            connection.execute(
                "UPDATE intent_events SET detail_json = '[]' WHERE sequence = 1"
            )
            connection.commit()
            connection.close()
            bad_event = self.invoke(state, "check", expected_success=False)
            self.assertIn("stored event detail", bad_event["detail"])

    def test_never_submitted_intents_cannot_be_observed(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            prepared = self.prepare(td, state)
            orders = self.write_json(
                td, "empty-orders.json", {"data": {"orders": [], "next": None}}
            )
            positions = self.write_json(
                td,
                "empty-positions.json",
                {"data": {"positions": [], "next": None}},
            )
            result = self.invoke(
                state,
                "observe",
                "--intent-id",
                prepared["intent_id"],
                "--orders",
                orders,
                "--positions",
                positions,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                expected_success=False,
                now="2026-07-31T16:01:00Z",
            )
            self.assertIn("never-submitted", result["detail"])
            checked = self.invoke(state, "check")
            self.assertEqual(checked["pending_count"], 1)

    def test_terminal_fill_snapshot_is_immutable(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "terminal.sqlite3")
            order = {
                "symbol": "TEST",
                "side": "buy",
                "type": "market",
                "quantity": "5",
                "market_hours": "regular_hours",
                "time_in_force": "gfd",
            }
            prepared = self.prepare(td, state, self.intent(order=order))
            self.invoke(
                state,
                "begin",
                "--intent-id",
                prepared["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            response = self.write_json(
                td,
                "terminal-response.json",
                {"data": {"order": self.broker_order(
                    quantity="5",
                    dollar_amount=None,
                    cumulative="5",
                )}},
            )
            self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                prepared["intent_id"],
                "--response",
                response,
                now="2026-07-31T16:00:04Z",
            )
            changed_execution = [{
                "id": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                "price": "21.00",
                "quantity": "5",
                "timestamp": "2026-07-31T16:00:03.12Z",
                "fees": "0.00",
            }]
            changed_order = self.broker_order(
                quantity="5",
                dollar_amount=None,
                cumulative="5",
                executions=changed_execution,
            )
            orders = self.write_json(
                td,
                "changed-terminal-orders.json",
                {"data": {"orders": [changed_order], "next": None}},
            )
            positions = self.write_json(
                td,
                "terminal-positions.json",
                {"data": {"positions": [
                    {"symbol": "TEST", "quantity": "5"}
                ], "next": None}},
            )
            changed = self.invoke(
                state,
                "observe",
                "--intent-id",
                prepared["intent_id"],
                "--orders",
                orders,
                "--positions",
                positions,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                expected_success=False,
                now="2026-07-31T16:01:00Z",
            )
            self.assertIn("terminal order's average fill", changed["detail"])

    def test_broker_lifecycle_contradictions_fail_closed(self):
        cases = (
            ("filled-zero", "filled", "0"),
            ("partial-zero", "partially_filled", "0"),
            ("partial-full", "partially_filled", "10"),
            ("rest-cancelled-full", "partially_filled_rest_cancelled", "10"),
        )
        quantity_order = {
            "symbol": "TEST", "side": "buy", "type": "market",
            "quantity": "10", "market_hours": "regular_hours",
            "time_in_force": "gfd",
        }
        with tempfile.TemporaryDirectory() as td:
            for name, broker_state, cumulative in cases:
                with self.subTest(name=name):
                    state = os.path.join(td, f"{name}.sqlite3")
                    prepared = self.prepare(
                        td,
                        state,
                        self.intent(order=quantity_order),
                        f"{name}-intent.json",
                    )
                    self.invoke(
                        state,
                        "begin",
                        "--intent-id",
                        prepared["intent_id"],
                        "--run-token",
                        self.RUN_TOKEN,
                        now="2026-07-31T16:00:02Z",
                    )
                    response = self.write_json(
                        td,
                        f"{name}-response.json",
                        {"data": {"order": self.broker_order(
                            state=broker_state,
                            quantity="10",
                            dollar_amount=None,
                            cumulative=cumulative,
                        )}},
                    )
                    result = self.invoke(
                        state,
                        "acknowledge",
                        "--intent-id",
                        prepared["intent_id"],
                        "--response",
                        response,
                        expected_success=False,
                        now="2026-07-31T16:00:04Z",
                    )
                    self.assertRegex(result["detail"], r"filled|partial")

    def test_stop_retry_is_linked_to_one_terminal_zero_fill_parent(self):
        stop_order = {
            "symbol": "TEST", "side": "sell", "type": "stop_market",
            "quantity": "5", "stop_price": "9.00",
            "market_hours": "regular_hours", "time_in_force": "gtc",
        }
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            parent = self.prepare(
                td,
                state,
                self.intent(
                    purpose="initial-stop", order=stop_order,
                    position_quantity="5",
                ),
                "parent.json",
            )
            self.invoke(
                state,
                "begin",
                "--intent-id",
                parent["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            response = self.write_json(
                td,
                "cancelled-parent.json",
                {"data": {"order": self.broker_order(
                    side="sell", order_type="market", trigger="stop",
                    state="cancelled", quantity="5", dollar_amount=None,
                    cumulative="0", stop_price="9.000000",
                    time_in_force="gtc",
                )}},
            )
            self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                parent["intent_id"],
                "--response",
                response,
                now="2026-07-31T16:00:03Z",
            )
            retry_intent = self.intent(
                purpose="stop-retry",
                order=stop_order,
                position_quantity="5",
                replaces_intent_id=parent["intent_id"],
            )
            retry = self.prepare(td, state, retry_intent, "retry.json")
            self.assertEqual(retry["replaces_intent_id"], parent["intent_id"])
            self.invoke(
                state,
                "abandon-prepared",
                "--intent-id",
                retry["intent_id"],
                "--note",
                "test proves only one child can ever replace the parent",
                now="2026-07-31T16:00:05Z",
            )
            second_path = self.write_json(td, "second-retry.json", retry_intent)
            second = self.invoke(
                state,
                "prepare",
                "--intent",
                second_path,
                expected_success=False,
                now="2026-07-31T16:00:06Z",
            )
            self.assertIn("already has a retry", second["detail"])

            ambiguous_state = os.path.join(td, "ambiguous-stop.sqlite3")
            unresolved = self.prepare(
                td,
                ambiguous_state,
                self.intent(
                    purpose="initial-stop", order=stop_order,
                    position_quantity="5",
                ),
                "unresolved-stop.json",
            )
            self.invoke(
                ambiguous_state,
                "begin",
                "--intent-id",
                unresolved["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            self.invoke(
                ambiguous_state,
                "mark-unknown",
                "--intent-id",
                unresolved["intent_id"],
                "--code",
                "timeout",
                now="2026-07-31T16:00:03Z",
            )
            repair_path = self.write_json(
                td,
                "repair-over-unknown.json",
                self.intent(
                    purpose="stop-repair", order=stop_order,
                    position_quantity="5",
                ),
            )
            repair = self.invoke(
                ambiguous_state,
                "prepare",
                "--intent",
                repair_path,
                expected_success=False,
                now="2026-07-31T16:00:04Z",
            )
            self.assertIn("ambiguous stop intent", repair["detail"])

            working_state = os.path.join(td, "working-stop.sqlite3")
            working = self.prepare(
                td,
                working_state,
                self.intent(
                    purpose="initial-stop",
                    order=stop_order,
                    position_quantity="5",
                ),
                "working-stop.json",
            )
            self.invoke(
                working_state,
                "begin",
                "--intent-id",
                working["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            working_response = self.write_json(
                td,
                "working-stop-response.json",
                {"data": {"order": self.broker_order(
                    side="sell",
                    order_type="market",
                    trigger="stop",
                    state="new",
                    quantity="5",
                    dollar_amount=None,
                    cumulative="0",
                    stop_price="9.000000",
                    time_in_force="gtc",
                )}},
            )
            acknowledged = self.invoke(
                working_state,
                "acknowledge",
                "--intent-id",
                working["intent_id"],
                "--response",
                working_response,
                now="2026-07-31T16:00:03Z",
            )
            self.assertEqual(acknowledged["status"], "working")
            working_repair_path = self.write_json(
                td,
                "repair-over-working.json",
                self.intent(
                    purpose="stop-repair",
                    order=stop_order,
                    position_quantity="5",
                ),
            )
            working_repair = self.invoke(
                working_state,
                "prepare",
                "--intent",
                working_repair_path,
                expected_success=False,
                now="2026-07-31T16:00:04Z",
            )
            self.assertIn("ambiguous stop intent", working_repair["detail"])

            chain_state = os.path.join(td, "chained-stop-retry.sqlite3")
            chain_parent = self.prepare(
                td,
                chain_state,
                self.intent(
                    purpose="initial-stop",
                    order=stop_order,
                    position_quantity="5",
                ),
                "chain-parent.json",
            )
            self.invoke(
                chain_state,
                "begin",
                "--intent-id",
                chain_parent["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            chain_parent_response = self.write_json(
                td,
                "chain-parent-response.json",
                {"data": {"order": self.broker_order(
                    side="sell",
                    order_type="market",
                    trigger="stop",
                    state="cancelled",
                    quantity="5",
                    dollar_amount=None,
                    cumulative="0",
                    stop_price="9.000000",
                    time_in_force="gtc",
                )}},
            )
            self.invoke(
                chain_state,
                "acknowledge",
                "--intent-id",
                chain_parent["intent_id"],
                "--response",
                chain_parent_response,
                now="2026-07-31T16:00:03Z",
            )
            chain_child_intent = self.intent(
                purpose="stop-retry",
                order=stop_order,
                position_quantity="5",
                replaces_intent_id=chain_parent["intent_id"],
            )
            chain_child = self.prepare(
                td,
                chain_state,
                chain_child_intent,
                "chain-child.json",
            )
            self.invoke(
                chain_state,
                "begin",
                "--intent-id",
                chain_child["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:04Z",
            )
            chain_child_response = self.write_json(
                td,
                "chain-child-response.json",
                {"data": {"order": self.broker_order(
                    order_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    side="sell",
                    order_type="market",
                    trigger="stop",
                    state="cancelled",
                    quantity="5",
                    dollar_amount=None,
                    cumulative="0",
                    stop_price="9.000000",
                    time_in_force="gtc",
                    created_at="2026-07-31T16:00:05Z",
                )}},
            )
            self.invoke(
                chain_state,
                "acknowledge",
                "--intent-id",
                chain_child["intent_id"],
                "--response",
                chain_child_response,
                now="2026-07-31T16:00:06Z",
            )
            chained_path = self.write_json(
                td,
                "forbidden-chain.json",
                self.intent(
                    purpose="stop-retry",
                    order=stop_order,
                    position_quantity="5",
                    replaces_intent_id=chain_child["intent_id"],
                ),
            )
            chained = self.invoke(
                chain_state,
                "prepare",
                "--intent",
                chained_path,
                expected_success=False,
                now="2026-07-31T16:00:07Z",
            )
            self.assertIn("terminal zero-fill stop parent", chained["detail"])

    def test_baseline_freshness_and_cursor_chain_are_enforced(self):
        with tempfile.TemporaryDirectory() as td:
            stale_state = os.path.join(td, "stale.sqlite3")
            stale_path = self.write_json(td, "stale.json", self.intent())
            stale = self.invoke(
                stale_state,
                "prepare",
                "--intent",
                stale_path,
                expected_success=False,
                now="2026-07-31T16:03:00Z",
            )
            self.assertIn("baseline is stale", stale["detail"])

            state = os.path.join(td, "cursors.sqlite3")
            prepared = self.prepare(td, state, name="cursor-intent.json")
            self.invoke(
                state,
                "begin",
                "--intent-id",
                prepared["intent_id"],
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            self.invoke(
                state,
                "mark-unknown",
                "--intent-id",
                prepared["intent_id"],
                "--code",
                "timeout",
                now="2026-07-31T16:00:03Z",
            )
            first_page = self.write_json(
                td,
                "orders-page-1.json",
                {"structuredContent": {"data": {
                    "orders": [],
                    "next": "https://agent.robinhood.com/orders?cursor=cursor-two",
                }}},
            )
            second_page = self.write_json(
                td,
                "orders-page-2.json",
                {"data": {"orders": [self.broker_order()], "next": None}},
            )
            positions = self.write_json(
                td,
                "cursor-positions.json",
                {"data": {"positions": [
                    {"symbol": "TEST", "quantity": "5"}
                ], "next": None}},
            )
            broken = self.invoke(
                state,
                "observe",
                "--intent-id",
                prepared["intent_id"],
                "--orders",
                first_page,
                second_page,
                "--order-request-cursors",
                "FIRST",
                "wrong-cursor",
                "--positions",
                positions,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                expected_success=False,
                now="2026-07-31T16:01:00Z",
            )
            self.assertIn("breaks the chain", broken["detail"])
            observed = self.invoke(
                state,
                "observe",
                "--intent-id",
                prepared["intent_id"],
                "--orders",
                first_page,
                second_page,
                "--order-request-cursors",
                "FIRST",
                "cursor-two",
                "--positions",
                positions,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertTrue(observed["matched"])


class BrokerSnapshotTests(unittest.TestCase):
    def invoke(self, *args):
        proc = subprocess.run(
            [sys.executable, BROKER_SNAPSHOT, *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        try:
            document = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f'broker_snapshot.py emitted non-JSON stdout: {proc.stdout!r}; '
                f'stderr={proc.stderr!r}'
            )
        return proc, document

    @staticmethod
    def write_json(directory, name, document):
        path = os.path.join(directory, name)
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            json.dump(document, handle, separators=(',', ':'), sort_keys=True)
            handle.write('\n')
        return path

    @staticmethod
    def write_text(directory, name, value):
        path = os.path.join(directory, name)
        with open(path, 'w', encoding='utf-8', newline='\n') as handle:
            handle.write(value)
        return path

    def preflight(self, scratch):
        proc, document = self.invoke('preflight', '--scratch', scratch)
        self.assertEqual(proc.returncode, 0, (document, proc.stderr))
        self.assertEqual(
            set(document),
            {
                'schema_version', 'action', 'ok', 'scratch', 'sentinel_sha256',
                'scratch_id', 'write_read_parse', 'cleanup_verified',
            },
        )
        self.assertTrue(document['ok'])
        return document

    def test_source_preflight_proves_external_file_change_transport(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            scratch_result = self.preflight(scratch)
            source = self.write_json(
                td,
                'source-probe.json',
                {
                    'schema_version': 1,
                    'purpose': 'rhmra-broker-response-source-probe',
                },
            )

            proc, result = self.invoke(
                'source-preflight', '--scratch', scratch, '--source', source
            )
            self.assertEqual(proc.returncode, 0, (result, proc.stderr))
            self.assertEqual(result['action'], 'source-preflight')
            self.assertTrue(result['ok'])
            self.assertEqual(result['scratch_id'], scratch_result['scratch_id'])
            self.assertEqual(result['source'], os.path.abspath(source))
            self.assertTrue(result['strict_json'])
            self.assertTrue(result['outside_scratch'])

            inside = self.write_json(
                scratch,
                'inside-probe.json',
                {
                    'schema_version': 1,
                    'purpose': 'rhmra-broker-response-source-probe',
                },
            )
            rejected, error = self.invoke(
                'source-preflight', '--scratch', scratch, '--source', inside
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn('outside the marked scratch directory', error['error']['message'])

    def stage(self, kind, sources, outputs, *extra, generation='A'):
        args = ['stage', '--kind', kind, '--generation', generation]
        for source in sources:
            args += ['--source', source]
        for output in outputs:
            args += ['--output', output]
        args += list(extra)
        return self.invoke(*args)

    def test_strict_transport_envelopes_are_unwrapped_without_rekeying(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)

            raw = {
                'data': {
                    'total_value': '1500.01',
                    'cash': '100',
                    'buying_power': '100',
                }
            }
            raw_source = self.write_json(td, 'raw.json', raw)
            raw_output = os.path.join(scratch, 'raw-out.json')
            proc, result = self.stage('portfolio', [raw_source], [raw_output])
            self.assertEqual(proc.returncode, 0, result)
            self.assertEqual(result['files'][0]['transport'], 'raw')

            positions = {'data': {'positions': [], 'next': None}}
            structured_source = self.write_json(
                td,
                'structured.json',
                {'structuredContent': positions},
            )
            structured_output = os.path.join(scratch, 'structured-out.json')
            proc, result = self.stage(
                'positions', [structured_source], [structured_output]
            )
            self.assertEqual(proc.returncode, 0, result)
            self.assertEqual(result['files'][0]['transport'], 'structuredContent')

            orders = {'data': {'orders': [], 'next': None}}
            text_source = self.write_json(
                td,
                'text.json',
                {
                    'content': [
                        {
                            'type': 'text',
                            'text': json.dumps(orders, separators=(',', ':')),
                        }
                    ]
                },
            )
            text_output = os.path.join(scratch, 'text-out.json')
            proc, result = self.stage('orders', [text_source], [text_output])
            self.assertEqual(proc.returncode, 0, result)
            self.assertEqual(result['files'][0]['transport'], 'content.text')

            for output, expected in (
                (raw_output, raw),
                (structured_output, positions),
                (text_output, orders),
            ):
                with open(output, encoding='utf-8') as handle:
                    self.assertEqual(json.load(handle), expected)

    def test_portfolio_stage_accepts_current_and_legacy_buying_power_shapes(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            shapes = (
                (
                    'current',
                    {
                        'buying_power': '1508.9700',
                        'display_currency': 'USD',
                        'unleveraged_buying_power': '1508.9700',
                    },
                ),
                ('legacy', '1508.9700'),
            )
            for label, buying_power in shapes:
                payload = {
                    'data': {
                        'total_value': '1508.97',
                        'cash': '1508.97',
                        'buying_power': buying_power,
                        'equity_value': '0',
                    }
                }
                source = self.write_json(
                    td,
                    f'{label}-portfolio.json',
                    {'structuredContent': payload},
                )
                output = os.path.join(scratch, f'{label}-portfolio-out.json')
                proc, result = self.stage('portfolio', [source], [output])
                with self.subTest(shape=label):
                    self.assertEqual(proc.returncode, 0, result)
                    self.assertEqual(
                        result['files'][0]['transport'],
                        'structuredContent',
                    )
                    with open(output, encoding='utf-8') as handle:
                        self.assertEqual(json.load(handle), payload)

    def test_portfolio_stage_rejects_malformed_nested_buying_power(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            malformed = (
                ('missing', {'display_currency': 'USD'}),
                ('null', {'buying_power': None}),
                ('object', {'buying_power': {'amount': '1508.9700'}}),
                ('nonfinite', {'buying_power': 'NaN'}),
                ('negative', {'buying_power': '-0.01'}),
            )
            for label, buying_power in malformed:
                source = self.write_json(
                    td,
                    f'{label}-buying-power.json',
                    {
                        'data': {
                            'total_value': '1508.97',
                            'buying_power': buying_power,
                        }
                    },
                )
                output = os.path.join(scratch, f'{label}-rejected.json')
                proc, result = self.stage('portfolio', [source], [output])
                with self.subTest(case=label):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(result['error']['code'], 'invalid_snapshot')
                    self.assertIn(
                        'portfolio.data.buying_power.buying_power',
                        result['error']['message'],
                    )
                    self.assertFalse(os.path.exists(output))

    def test_portfolio_stage_rejects_missing_or_null_outer_buying_power(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            malformed = (
                ('missing', {}),
                ('null', {'buying_power': None}),
            )
            for label, fields in malformed:
                source = self.write_json(
                    td,
                    f'{label}-outer-buying-power.json',
                    {
                        'data': {
                            'total_value': '1508.97',
                            **fields,
                        }
                    },
                )
                output = os.path.join(scratch, f'{label}-outer-rejected.json')
                proc, result = self.stage('portfolio', [source], [output])
                with self.subTest(case=label):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(result['error']['code'], 'invalid_snapshot')
                    self.assertIn(
                        'portfolio.data.buying_power',
                        result['error']['message'],
                    )
                    self.assertFalse(os.path.exists(output))

    def test_non_paginated_stage_rejects_pagination_cursor_flags(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            payloads = {
                'portfolio': {
                    'data': {
                        'total_value': '1500.01',
                        'cash': '100',
                        'buying_power': '100',
                    }
                },
                'quotes': {'data': {'results': []}},
            }
            pagination_flags = (
                ('request-cursor', ('--request-cursor', 'FIRST')),
                ('allow-more', ('--allow-more',)),
            )
            for kind, payload in payloads.items():
                source = self.write_json(td, f'{kind}.json', payload)
                for label, extra in pagination_flags:
                    output = os.path.join(
                        scratch, f'{kind}-{label}-rejected.json'
                    )
                    proc, result = self.stage(
                        kind, [source], [output], *extra
                    )
                    with self.subTest(kind=kind, flag=label):
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertIn(
                            f'{extra[0]} is not valid for {kind}',
                            result['error']['message'],
                        )
                        self.assertFalse(os.path.exists(output))

    def test_strict_json_and_malformed_semantic_shapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            quote = chr(34)
            duplicate = (
                '{' + quote + 'data' + quote + ':{' + quote + 'total_value' +
                quote + ':' + quote + '1' + quote + '},' + quote + 'data' +
                quote + ':{' + quote + 'total_value' + quote + ':' + quote +
                '2' + quote + '}}'
            )
            nonfinite = (
                '{' + quote + 'data' + quote + ':{' + quote + 'total_value' +
                quote + ':NaN}}'
            )
            malformed = '{'
            cases = [
                ('portfolio', self.write_text(td, 'duplicate.json', duplicate)),
                ('portfolio', self.write_text(td, 'nonfinite.json', nonfinite)),
                ('portfolio', self.write_text(td, 'malformed.json', malformed)),
                (
                    'positions',
                    self.write_json(
                        td, 'positions-shape.json',
                        {'data': {'positions': {}, 'next': None}},
                    ),
                ),
                (
                    'orders',
                    self.write_json(
                        td,
                        'orders-shape.json',
                        {
                            'data': {
                                'orders': [
                                    {
                                        'id': 'order-1',
                                        'state': 'filled',
                                        'symbol': 'TEST',
                                        'side': 'sell',
                                        'cumulative_quantity': '2',
                                        'executions': [
                                            {
                                                'id': 'execution-1',
                                                'quantity': '1',
                                                'price': '10',
                                                'fees': '0',
                                                'timestamp': '2026-08-04T16:00:00Z',
                                            }
                                        ],
                                    }
                                ],
                                'next': None,
                            }
                        },
                    ),
                ),
                (
                    'quotes',
                    self.write_json(
                        td, 'quotes-shape.json',
                        {'data': {'results': [{}] * 21}},
                    ),
                ),
                (
                    'portfolio',
                    self.write_json(
                        td,
                        'ambiguous-envelope.json',
                        {
                            'content': [
                                {'type': 'text', 'text': '{}'},
                                {'type': 'text', 'text': '{}'},
                            ]
                        },
                    ),
                ),
                (
                    'portfolio',
                    self.write_json(
                        td,
                        'tool-error.json',
                        {
                            'isError': True,
                            'structuredContent': {
                                'data': {'total_value': '1500.01'}
                            },
                        },
                    ),
                ),
                (
                    'portfolio',
                    self.write_json(
                        td,
                        'invalid-error-flag.json',
                        {
                            'isError': 'false',
                            'structuredContent': {
                                'data': {'total_value': '1500.01'}
                            },
                        },
                    ),
                ),
            ]
            for index, (kind, source) in enumerate(cases):
                output = os.path.join(scratch, f'rejected-{index}.json')
                with self.subTest(kind=kind, source=os.path.basename(source)):
                    proc, result = self.stage(kind, [source], [output])
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertFalse(result['ok'])
                    self.assertEqual(
                        result['error']['code'], 'invalid_snapshot'
                    )
                    self.assertFalse(os.path.exists(output))

    def test_preflight_marker_containment_readback_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            sibling = os.path.join(td, 'sibling')
            nested = os.path.join(scratch, 'nested')
            os.mkdir(scratch)
            os.mkdir(sibling)
            os.mkdir(nested)
            preflight = self.preflight(scratch)
            self.assertTrue(preflight['write_read_parse'])
            self.assertTrue(preflight['cleanup_verified'])
            marker = os.path.join(
                scratch, '.rhmra-broker-snapshot-scratch.json'
            )
            with open(marker, encoding='utf-8') as handle:
                marker_document = json.load(handle)
            self.assertEqual(
                marker_document,
                {
                    'schema_version': 1,
                    'marker': 'rhmra-broker-snapshot-scratch',
                    'purpose': 'daily-loss-raw-broker-staging',
                    'scratch_id': preflight['scratch_id'],
                },
            )
            self.assertFalse(
                any(name.startswith('.rhmra-scratch-preflight-')
                    for name in os.listdir(scratch))
            )
            second, second_result = self.invoke(
                'preflight', '--scratch', scratch
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertFalse(second_result['ok'])

            payload = {
                'data': {
                    'total_value': '1500.01',
                    'buying_power': '100',
                }
            }
            source = self.write_json(td, 'portfolio.json', payload)
            output = os.path.join(scratch, 'portfolio-staged.json')
            proc, result = self.stage('portfolio', [source], [output])
            self.assertEqual(proc.returncode, 0, result)
            with open(output, 'rb') as handle:
                persisted = handle.read()
            self.assertEqual(
                result['files'][0]['payload_sha256'],
                hashlib.sha256(persisted).hexdigest(),
            )
            self.assertEqual(json.loads(persisted), payload)

            proc, rejected = self.stage('portfolio', [source], [output])
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(rejected['ok'])
            with open(output, 'rb') as handle:
                self.assertEqual(handle.read(), persisted)

            for escaped in (
                os.path.join(sibling, 'escaped.json'),
                os.path.join(nested, 'nested.json'),
            ):
                proc, rejected = self.stage('portfolio', [source], [escaped])
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(rejected['ok'])
                self.assertFalse(os.path.exists(escaped))

    def test_cursor_chain_can_be_staged_pagewise_then_sealed_as_a_generation(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            next_url = 'https://agent.robinhood.com/positions?cursor=cursor-two'
            page_one_source = self.write_json(
                td,
                'positions-one.json',
                {
                    'data': {
                        'positions': [
                            {
                                'symbol': 'ONE',
                                'quantity': '1',
                                'intraday_quantity': '0',
                                'type': 'long',
                            }
                        ],
                        'next': next_url,
                    }
                },
            )
            page_two_source = self.write_json(
                td,
                'positions-two.json',
                {
                    'data': {
                        'positions': [
                            {
                                'symbol': 'TWO',
                                'quantity': '2',
                                'intraday_quantity': '1',
                                'type': 'long',
                            }
                        ],
                        'next': None,
                    }
                },
            )
            page_one = os.path.join(scratch, 'page-one.json')
            page_two = os.path.join(scratch, 'page-two.json')
            proc, first = self.stage(
                'positions', [page_one_source], [page_one],
                '--request-cursor', 'FIRST', '--allow-more',
            )
            self.assertEqual(proc.returncode, 0, first)
            self.assertFalse(first['complete'])
            self.assertEqual(first['files'][0]['next_cursor'], 'cursor-two')
            proc, second = self.stage(
                'positions', [page_two_source], [page_two],
                '--request-cursor', 'cursor-two',
            )
            self.assertEqual(proc.returncode, 0, second)
            self.assertTrue(second['complete'])
            with self.assertRaisesRegex(
                SnapshotError, 'one complete aggregate-sealed set'
            ):
                validate_generation_inputs(
                    {'positions': [page_one, page_two]}, 'A'
                )

            sealed_one = os.path.join(scratch, 'sealed-one.json')
            sealed_two = os.path.join(scratch, 'sealed-two.json')
            proc, sealed = self.stage(
                'positions', [page_one, page_two], [sealed_one, sealed_two],
                '--request-cursor', 'FIRST',
                '--request-cursor', 'cursor-two',
            )
            self.assertEqual(proc.returncode, 0, sealed)
            self.assertTrue(sealed['complete'])
            self.assertEqual(sealed['file_count'], 2)
            validate_generation_inputs(
                {'positions': [sealed_one, sealed_two]}, 'A'
            )
            self.assertEqual(
                [item['request_cursor'] for item in sealed['files']],
                ['FIRST', 'cursor-two'],
            )

            wrong_one = os.path.join(scratch, 'wrong-one.json')
            wrong_two = os.path.join(scratch, 'wrong-two.json')
            proc, rejected = self.stage(
                'positions', [page_one, page_two], [wrong_one, wrong_two],
                '--request-cursor', 'FIRST',
                '--request-cursor', 'wrong-cursor',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(rejected['ok'])
            self.assertFalse(os.path.exists(wrong_one))
            self.assertFalse(os.path.exists(wrong_two))

            incomplete = os.path.join(scratch, 'incomplete.json')
            proc, rejected = self.stage(
                'positions', [page_one], [incomplete],
                '--request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(rejected['ok'])
            self.assertFalse(os.path.exists(incomplete))

    def test_daily_loss_rejects_cross_generation_staged_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            positions_source = self.write_json(
                td, 'positions.json',
                {'data': {'positions': [], 'next': None}},
            )
            orders_source = self.write_json(
                td, 'orders.json',
                {'data': {'orders': [], 'next': None}},
            )
            positions_a = os.path.join(scratch, 'positions-a.json')
            orders_b = os.path.join(scratch, 'orders-b.json')
            self.assertEqual(
                self.stage(
                    'positions', [positions_source], [positions_a],
                    '--request-cursor', 'FIRST', generation='A',
                )[0].returncode,
                0,
            )
            self.assertEqual(
                self.stage(
                    'orders', [orders_source], [orders_b],
                    '--request-cursor', 'FIRST', generation='B',
                )[0].returncode,
                0,
            )
            symbols = os.path.join(scratch, 'symbols.json')
            proc = subprocess.run(
                [
                    sys.executable, DAILY_LOSS,
                    '--positions', positions_a,
                    '--orders', orders_b,
                    '--snapshot-generation', 'A',
                    '--trading-date', '2026-08-04',
                    '--as-of-utc', '2026-08-04T19:00:00Z',
                    '--symbols-out', symbols,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('generation B does not match A', proc.stderr)
            self.assertFalse(os.path.exists(symbols))


class RunLifecycleTests(unittest.TestCase):
    TERMINAL_REASONS = {
        'completed': None,
        'risk-halt': 'daily-loss-tripped',
        'snapshot-failure': 'snapshot-retry-exhausted',
        'configuration-halt': 'configuration-invalid',
        'runtime-budget': 'runtime-deadline',
        'overlap': 'scheduler-overlap',
        'coordination-halt': 'coordination-state',
        'lease-lost': 'lease-ownership-lost',
        'final-status-unavailable': 'status-write-failed',
    }

    def invoke(self, state_file, projection_file, action, *extra, now=None):
        args = [
            sys.executable,
            RUN_LIFECYCLE,
            action,
            '--state-file',
            state_file,
            '--projection-file',
            projection_file,
        ]
        if now is not None:
            args += ['--now-utc', now]
        args += list(extra)
        proc = subprocess.run(args, capture_output=True, text=True, cwd=ROOT)
        try:
            document = json.loads(proc.stdout)
        except json.JSONDecodeError:
            self.fail(
                f'run_lifecycle.py emitted non-JSON stdout: {proc.stdout!r}; '
                f'stderr={proc.stderr!r}'
            )
        return proc, document

    def start(self, state_file, projection_file, now='2026-08-04T16:00:00Z'):
        proc, document = self.invoke(
            state_file, projection_file, 'start', now=now
        )
        self.assertEqual(proc.returncode, 0, (document, proc.stderr))
        self.assertTrue(document['ok'])
        self.assertEqual(document['classification'], 'running')
        self.assertEqual(document['phase'], 'scheduled')
        self.assertIsNone(document['run_start_pt'])
        return document

    def bind(self, state_file, projection_file, invocation_id):
        proc, document = self.invoke(
            state_file,
            projection_file,
            'event',
            '--invocation-id',
            invocation_id,
            '--phase',
            'preflight',
            '--run-start-pt',
            '2026-08-04T09:00:01-07:00',
            now='2026-08-04T16:00:01Z',
        )
        self.assertEqual(proc.returncode, 0, (document, proc.stderr))
        self.assertEqual(document['run_start_pt'], '2026-08-04T09:00:01-07:00')
        return document

    def test_concurrent_same_second_starts_create_unique_invocations(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')

            def attempt(_):
                return self.invoke(
                    state_file,
                    projection_file,
                    'start',
                    now='2026-08-04T16:00:00Z',
                )

            with ThreadPoolExecutor(max_workers=8) as pool:
                attempts = list(pool.map(attempt, range(8)))

            self.assertTrue(
                all(proc.returncode == 0 for proc, _document in attempts),
                attempts,
            )
            identifiers = [document['invocation_id'] for _proc, document in attempts]
            self.assertEqual(len(set(identifiers)), 8)
            self.assertEqual(
                len({document['sequence'] for _proc, document in attempts}), 8
            )
            with open(projection_file, encoding='utf-8') as handle:
                projection = json.load(handle)
            self.assertEqual(projection['record_count'], 8)
            self.assertEqual(
                {record['invocation_id'] for record in projection['records']},
                set(identifiers),
            )
            self.assertTrue(
                all(record['run_start_pt'] is None
                    for record in projection['records'])
            )
            proc, validated = self.invoke(
                state_file, projection_file, 'validate'
            )
            self.assertEqual(proc.returncode, 0, validated)
            self.assertEqual(validated['record_count'], 8)

    def test_terminal_classifications_and_exactly_once_finish(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            finished_ids = {}
            for classification, reason in self.TERMINAL_REASONS.items():
                started = self.start(state_file, projection_file)
                invocation_id = started['invocation_id']
                self.bind(state_file, projection_file, invocation_id)
                extra = [
                    '--invocation-id',
                    invocation_id,
                    '--classification',
                    classification,
                    '--report-file',
                    'rhmra-log-2026_08_04-09_00.md',
                ]
                if reason is not None:
                    extra += ['--reason-code', reason]
                if classification not in {
                    'overlap', 'lease-lost', 'final-status-unavailable'
                }:
                    extra += [
                        '--status-file',
                        'rhmra-status-2026_08_04-09_00.json',
                    ]
                proc, finished = self.invoke(
                    state_file,
                    projection_file,
                    'finish',
                    *extra,
                    now='2026-08-04T16:00:02Z',
                )
                self.assertEqual(proc.returncode, 0, finished)
                self.assertEqual(finished['classification'], classification)
                self.assertEqual(finished['reason_code'], reason)
                finished_ids[invocation_id] = classification

                proc, duplicate = self.invoke(
                    state_file,
                    projection_file,
                    'finish',
                    *extra,
                    now='2026-08-04T16:00:03Z',
                )
                self.assertEqual(proc.returncode, 2)
                self.assertFalse(duplicate['ok'])
                self.assertEqual(duplicate['reason'], 'lifecycle_conflict')

            with open(projection_file, encoding='utf-8') as handle:
                projection = json.load(handle)
            actual = {
                record['invocation_id']: record['classification']
                for record in projection['records']
            }
            self.assertEqual(actual, finished_ids)
            for record in projection['records']:
                self.assertEqual(record['events'][0]['type'], 'start')
                self.assertEqual(record['events'][-1]['type'], 'finish')
                self.assertEqual(record['duration_seconds'], 2)

            started = self.start(state_file, projection_file)
            proc, rejected = self.invoke(
                state_file,
                projection_file,
                'finish',
                '--invocation-id',
                started['invocation_id'],
                '--classification',
                'risk-halt',
                now='2026-08-04T16:00:01Z',
            )
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(rejected['ok'])
            self.assertEqual(rejected['reason'], 'lifecycle_state_error')

            proc, wrong_pair = self.invoke(
                state_file,
                projection_file,
                'finish',
                '--invocation-id',
                started['invocation_id'],
                '--classification',
                'overlap',
                '--reason-code',
                'daily-loss-tripped',
                now='2026-08-04T16:00:01Z',
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn('overlap requires reason code', wrong_pair['detail'])

            proc, completed_reason = self.invoke(
                state_file,
                projection_file,
                'finish',
                '--invocation-id',
                started['invocation_id'],
                '--classification',
                'completed',
                '--reason-code',
                'completed',
                now='2026-08-04T16:00:01Z',
            )
            self.assertEqual(proc.returncode, 1)
            self.assertIn(
                'completed requires reason code none',
                completed_reason['detail'],
            )

    def test_clock_binding_rejects_wrong_season_and_noncontemporaneous_time(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            for timestamp in (
                '2026-08-04T08:00:01-08:00',
                '2026-08-04T10:00:01-07:00',
            ):
                with self.subTest(timestamp=timestamp):
                    proc, rejected = self.invoke(
                        state_file,
                        projection_file,
                        'event',
                        '--invocation-id',
                        invocation_id,
                        '--phase',
                        'preflight',
                        '--run-start-pt',
                        timestamp,
                        now='2026-08-04T17:00:01Z',
                    )
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse(rejected['ok'])

    def test_projection_is_bounded_to_secret_free_fixed_schema(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            self.bind(state_file, projection_file, invocation_id)
            proc, event = self.invoke(
                state_file,
                projection_file,
                'event',
                '--invocation-id',
                invocation_id,
                '--phase',
                'daily-loss',
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, event)
            proc, finished = self.invoke(
                state_file,
                projection_file,
                'finish',
                '--invocation-id',
                invocation_id,
                '--classification',
                'completed',
                '--report-file',
                'rhmra-log-2026_08_04-09_00.md',
                '--status-file',
                'rhmra-status-2026_08_04-09_00.json',
                now='2026-08-04T16:00:03Z',
            )
            self.assertEqual(proc.returncode, 0, finished)

            with open(projection_file, encoding='utf-8') as handle:
                raw = handle.read()
            projection = json.loads(raw)
            self.assertEqual(
                set(projection),
                {
                    'schema_version', 'record_limit', 'record_count',
                    'source_event_high_watermark', 'records',
                },
            )
            self.assertEqual(projection['record_limit'], 512)
            self.assertEqual(projection['record_count'], 1)
            record = projection['records'][0]
            self.assertEqual(
                set(record),
                {
                    'invocation_id', 'run_start_pt', 'started_at_utc',
                    'finished_at_utc', 'duration_seconds', 'classification',
                    'latest_phase', 'reason_code', 'report_file', 'status_file',
                    'events',
                },
            )
            for event_record in record['events']:
                self.assertEqual(
                    set(event_record),
                    {
                        'sequence', 'type', 'classification',
                        'occurred_at_utc', 'phase', 'reason_code',
                    },
                )
            lowered = raw.lower()
            for forbidden in (
                'account_number', 'fencing_token', 'access_token',
                'client_secret', 'credentials', 'broker_response',
            ):
                self.assertNotIn(forbidden, lowered)

    def test_validate_rejects_corruption_and_export_repairs_projection(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            self.start(state_file, projection_file)
            proc, validated = self.invoke(
                state_file, projection_file, 'validate'
            )
            self.assertEqual(proc.returncode, 0, validated)

            with open(projection_file, encoding='utf-8') as handle:
                stale = json.load(handle)
            stale['record_count'] = 0
            with open(
                projection_file, 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump(stale, handle, separators=(',', ':'))
            proc, rejected = self.invoke(
                state_file, projection_file, 'validate'
            )
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(rejected['ok'])
            proc, exported = self.invoke(
                state_file, projection_file, 'export'
            )
            self.assertEqual(proc.returncode, 0, exported)
            proc, validated = self.invoke(
                state_file, projection_file, 'validate'
            )
            self.assertEqual(proc.returncode, 0, validated)

            quote = chr(34)
            corrupt_documents = (
                (
                    '{' + quote + 'schema_version' + quote + ':1,' + quote +
                    'schema_version' + quote + ':1}'
                ),
                '{' + quote + 'schema_version' + quote + ':NaN}',
                '{',
            )
            for corrupt in corrupt_documents:
                with open(
                    projection_file, 'w', encoding='utf-8', newline='\n'
                ) as handle:
                    handle.write(corrupt)
                proc, rejected = self.invoke(
                    state_file, projection_file, 'validate'
                )
                self.assertEqual(proc.returncode, 1)
                self.assertFalse(rejected['ok'])
                proc, exported = self.invoke(
                    state_file, projection_file, 'export'
                )
                self.assertEqual(proc.returncode, 0, exported)

            proc, validated = self.invoke(
                state_file, projection_file, 'validate'
            )
            self.assertEqual(proc.returncode, 0, validated)
            self.assertEqual(validated['record_count'], 1)


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
        with open(
            os.path.join(ROOT, "constants.md"), encoding="utf-8"
        ) as constants_file:
            constants_text = constants_file.read()
        self.valid_constants = ConstantsValidatorTests.replace_value(
            constants_text, "DRY_RUN", "true"
        )
        self.valid_constants = ConstantsValidatorTests.replace_value(
            self.valid_constants, "NO_BUY_FIRST_MINUTES", "45"
        )
        for name, content in (
            ("README.md", "private dashboard test fixture"),
            ("constants.md", self.valid_constants),
            ("trade-ledger.csv", "symbol,price\\nTEST,1.00\\n"),
            (os.path.join("dashboard", "index.html"), "<h1>Dashboard fixture</h1>"),
            (os.path.join(
                "run-reports", "rhmra-status-2026_08_04-12_02.json"
            ), "{}"),
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
        self.assertEqual(
            self.request(
                "GET", "/run-reports/rhmra-status-2026_08_04-12_02.json"
            )[0],
            200,
        )
        self.assertEqual(self.request("GET", "/trade-ledger.csv")[0], 200)
        self.assertEqual(self.request("GET", "/README.md")[0], 403)
        self.assertEqual(self.request("GET", "/dashboard/index.html", host="example.test")[0], 403)

    def test_config_reports_dashboard_settings_and_fails_closed(self):
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"dry_run": True, "no_buy_first_minutes": 45})

        constants_path = os.path.join(self.repo, "constants.md")
        with open(constants_path, "w", encoding="utf-8") as f:
            f.write(
                ConstantsValidatorTests.replace_value(
                    self.valid_constants, "DRY_RUN", "false"
                )
            )
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"dry_run": False, "no_buy_first_minutes": 45})

        with open(constants_path, "w", encoding="utf-8") as f:
            f.write(
                ConstantsValidatorTests.replace_value(
                    self.valid_constants, "NO_BUY_FIRST_MINUTES", "60"
                )
            )
        status, body = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"dry_run": True, "no_buy_first_minutes": 60})

        with open(constants_path, "w", encoding="utf-8") as f:
            f.write(
                ConstantsValidatorTests.replace_value(
                    self.valid_constants, "DRY_RUN", "maybe"
                )
            )
        status, body = self.request("GET", "/api/config")
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(document["dry_run"])
        self.assertIsNone(document["no_buy_first_minutes"])
        self.assertIn("DRY_RUN must be exactly true or false", document["error"])

        with open(constants_path, "w", encoding="utf-8") as f:
            f.write(
                ConstantsValidatorTests.replace_value(
                    self.valid_constants, "TOP_N", "0"
                )
            )
        status, body = self.request("GET", "/api/config")
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(document["dry_run"])
        self.assertIsNone(document["no_buy_first_minutes"])
        self.assertIn("TOP_N", document["error"])

        with open(constants_path, "w", encoding="utf-8") as f:
            f.write(
                ConstantsValidatorTests.replace_value(
                    self.valid_constants, "TOP_N", "9" * 5000
                )
            )
        status, body = self.request("GET", "/api/config")
        document = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsNone(document["dry_run"])
        self.assertIsNone(document["no_buy_first_minutes"])
        self.assertIn("integer literal exceeds the supported size", document["error"])

    def test_lifecycle_api_validates_projection_and_private_state_is_not_static(self):
        state_file = os.path.join(
            self.repo, "run-reports", "rhmra-run-lifecycle.sqlite3"
        )
        projection_file = os.path.join(
            self.repo, "run-reports", "rhmra-run-lifecycle.json"
        )

        def lifecycle(*args):
            proc = subprocess.run(
                [
                    sys.executable,
                    RUN_LIFECYCLE,
                    *args,
                    "--state-file",
                    state_file,
                    "--projection-file",
                    projection_file,
                ],
                text=True,
                capture_output=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            return json.loads(proc.stdout)

        started = lifecycle(
            "start", "--now-utc", "2026-08-04T19:02:00Z"
        )
        lifecycle(
            "event",
            "--invocation-id",
            started["invocation_id"],
            "--phase",
            "preflight",
            "--run-start-pt",
            "2026-08-04T12:02:00-07:00",
            "--now-utc",
            "2026-08-04T19:02:01Z",
        )
        lifecycle(
            "finish",
            "--invocation-id",
            started["invocation_id"],
            "--classification",
            "snapshot-failure",
            "--reason-code",
            "snapshot-second-attempt-failed",
            "--report-file",
            "rhmra-log-2026_08_04-12_02.md",
            "--status-file",
            "rhmra-status-2026_08_04-12_02.json",
            "--now-utc",
            "2026-08-04T19:03:00Z",
        )

        status, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["record_count"], 1)
        self.assertEqual(
            document["records"][0]["classification"], "snapshot-failure"
        )
        self.assertEqual(
            self.request(
                "GET", "/run-reports/rhmra-run-lifecycle.json"
            )[0],
            403,
        )
        self.assertEqual(
            self.request(
                "GET", "/run-reports/rhmra-run-lifecycle.sqlite3"
            )[0],
            403,
        )

        current_projection = document
        lifecycle(
            "start", "--now-utc", "2026-08-04T19:04:00Z"
        )
        with open(projection_file, "w", encoding="utf-8") as handle:
            json.dump(current_projection, handle)
        status, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 500)
        self.assertIn("stale or inconsistent", json.loads(body)["error"])

        with open(projection_file, "w", encoding="utf-8") as handle:
            handle.write('{"schema_version":1,"records":[]}')
        status, body = self.request("GET", "/api/runs")
        self.assertEqual(status, 500)
        self.assertIn("error", json.loads(body))

    def test_ledger_api_exposes_matched_pool_cents_and_eastern_day(self):
        ledger_path = os.path.join(self.repo, "trade-ledger.csv")
        with open(ledger_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([
                "timestamp_pt", "order_id", "symbol", "side", "quantity", "price",
                "notional", "reason", "realized_pnl", "rules_version",
            ])
            writer.writerow([
                "2026-08-04T08:45:40.188-07:00", "buy", "THRY", "buy",
                "113.878854", "2.629900", "299.489998134600", "dip-buy", "", "f8ae9d9",
            ])
            writer.writerow([
                "2026-08-04T10:37:05.098-07:00", "sell", "THRY", "sell",
                "113.878854", "2.731000", "311.003150274000", "profit-take",
                "11.501764254000", "f8ae9d9",
            ])

        status, body = self.request("GET", "/api/ledger")
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document["schema_version"], 1)
        sell = document["rows"][1]
        self.assertEqual(document["rounding_policy"], "per-fill-half-away-from-zero-to-cent")
        self.assertEqual(sell["pnl_source"], "matched-ledger-pool")
        self.assertEqual(sell["realized_pnl"], "11.5131521394")
        self.assertEqual(sell["realized_pnl_cents"], 1151)
        self.assertEqual(sell["day_et"], "2026-08-04")
        self.assertEqual(sell["recorded_difference"], "0.0113878854")


class DashboardClientContractTests(unittest.TestCase):
    def test_dashboard_compares_broker_and_strategy_with_server_rounded_cents(self):
        with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as handle:
            dashboard = handle.read()
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8"
        ) as handle:
            routine = handle.read()

        self.assertIn('getJSON("/api/ledger")', dashboard)
        self.assertIn("const LEDGER_ROUNDING_POLICY =", dashboard)
        self.assertIn("row?.realized_pnl_cents", dashboard)
        self.assertIn('dateInTimeZone(snap.run_start_pt, "America/New_York")', dashboard)
        self.assertNotIn("Broker and strategy agree to the cent", dashboard)
        self.assertIn("Broker vs strategy difference", dashboard)
        self.assertIn("Broker vs strategy comparison incomplete", dashboard)
        self.assertIn("available_fill_count", dashboard)
        self.assertIn('status: qualified ? "qualified"', dashboard)
        self.assertIn("Broker realized today", dashboard)
        self.assertIn("Strategy realized P&amp;L by rules era (ledger fill basis)", dashboard)
        self.assertIn("let rows = null;", dashboard)
        self.assertNotIn("P&L reconciled", dashboard)
        self.assertNotIn("unattributed", dashboard)
        self.assertNotIn("parseFloat(f[i.realized_pnl])", dashboard)
        self.assertIn("ledger_pnl.py --ledger", routine)
        self.assertIn('--sale-time "<exact final last-execution timestamp in Pacific>"', routine)
        self.assertIn('status: "matched-ledger-pool"', routine)
        self.assertIn("order_intents.py ledger_pnl.py", routine)
        self.assertIn("NEVER use the rounded `get_equity_positions.average_buy_price`", routine)
        self.assertNotIn("realized_pnl = (price − average cost) × quantity", routine)

    def test_phone_share_wire_and_key_lifecycle_contracts_stay_aligned(self):
        with open(os.path.join(ROOT, 'dashboard', 'index.html'), encoding='utf-8') as f:
            dashboard = f.read()
        qr_path = os.path.join(ROOT, 'dashboard', 'vendor', 'qrcode-lite.js')
        with open(qr_path, encoding='utf-8') as f:
            qr_encoder = f.read()
        self.assertIn('PHONE_SHARE_SCHEMA, session.id, sequence, capturedAt, session.expiresAt', dashboard)
        self.assertIn(
            'const aad = phoneShareEncoder.encode(JSON.stringify([', dashboard
        )
        self.assertIn(
            "{ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }",
            dashboard,
        )
        self.assertLess(
            dashboard.index('if (!session.lastUploaded)'),
            dashboard.index('RhmraQr.render'),
        )
        self.assertIn('The QR remains hidden', dashboard)
        self.assertIn('PHONE_SHARE_UPLOAD_FRESHNESS_MS = 900 * 1000', dashboard)
        self.assertIn('body: pendingUpload.body', dashboard)
        self.assertIn(
            'session.sequence = Math.max(session.sequence, session.pendingUpload.sequence)',
            dashboard,
        )
        self.assertIn('root.RhmraQr = Object.freeze({ matrix, render })', qr_encoder)
        self.assertIn('dashboard/vendor/qrcode-lite.js', dashboard)
        self.assertIn('.phone-share-dialog[open] { position:fixed;', dashboard)
        self.assertIn('inset:50% auto auto 50%; margin:0;', dashboard)
        self.assertIn('transform:translate(-50%, -50%);', dashboard)
        self.assertIn('<span>View on Phone</span>', dashboard)

    def test_phone_share_preserves_unavailable_realized_pnl(self):
        with open(os.path.join(ROOT, 'dashboard', 'index.html'), encoding='utf-8') as f:
            dashboard = f.read()

        self.assertIn('const realizedToday = snap.realized_pnl_today;', dashboard)
        self.assertIn(
            'realizedToday !== null && safeShareNumber(realizedToday) === null',
            dashboard,
        )
        self.assertIn('realized_pnl_today: realizedToday,', dashboard)
        self.assertNotIn(
            'snap.account?.buying_power,\n                         snap.realized_pnl_today',
            dashboard,
        )

    def test_dashboard_merges_invocations_and_names_terminal_outcomes(self):
        with open(
            os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8"
        ) as handle:
            dashboard = handle.read()
        self.assertIn('getJSON("/api/runs")', dashboard)
        self.assertIn('typeof record.status_file === "string"', dashboard)
        self.assertIn("statusByName.get(name)", dashboard)
        self.assertIn("function lifecycleOwnsFillWindow(record)", dashboard)
        self.assertIn(
            '"overlap", "configuration-halt", "coordination-halt", "runtime-budget"',
            dashboard,
        )
        self.assertIn('reason === "scratch-preflight-failed"', dashboard)
        self.assertIn(
            "const fillOwners = records.filter(lifecycleOwnsFillWindow)",
            dashboard,
        )
        owner_function = re.search(
            r"function lifecycleOwnsFillWindow\(record\) \{(.*?)\n\}",
            dashboard,
            re.DOTALL,
        )
        self.assertIsNotNone(owner_function)
        excluded = set(re.findall(
            r'"([a-z-]+)"',
            owner_function.group(1).split("].includes", 1)[0],
        ))
        self.assertIn("overlap", excluded)
        self.assertIn("coordination-halt", excluded)
        self.assertNotIn("lease-lost", excluded)
        self.assertNotIn("final-status-unavailable", excluded)
        self.assertIn("const nextFillOwner = new Map(", dashboard)
        self.assertIn("const next = nextFillOwner.get(record)", dashboard)
        self.assertNotIn("const next = records[index + 1]?.start", dashboard)
        for label in (
            "risk halt",
            "snapshot failure",
            "configuration halt",
            "overlap skipped",
            "coordination halt",
            "lease lost",
            "final status unavailable",
        ):
            self.assertIn(label, dashboard)
        self.assertIn('return "skipped";', dashboard)
        self.assertIn('return "halted";', dashboard)
        self.assertIn('function skipLabel(reason, session)', dashboard)
        self.assertIn('"closed", "closed-weekend", "closed-holiday", "closed-early"', dashboard)
        self.assertIn('return "market closed";', dashboard)
        self.assertIn('skipLabel(reason, s.session)', dashboard)
        self.assertIn('skipLabel(reason, status.session)', dashboard)
        self.assertIn(
            "every scheduler invocation; the label shows its trade or terminal outcome",
            dashboard,
        )

    def test_position_symbols_link_to_robinhood_safely(self):
        with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
            dashboard = f.read()

        self.assertIn(
            "https://robinhood.com/stocks/${encodeURIComponent(symbol)}"
            "?source=lists_section_position",
            dashboard,
        )
        self.assertIn('class="position-symbol"', dashboard)
        self.assertIn('target="_blank" rel="noopener noreferrer"', dashboard)
        self.assertIn("${esc(stockUrl)}", dashboard)

    def test_run_tooltips_use_structured_symbols_and_local_blackout_time(self):
        with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
            dashboard = f.read()

        self.assertIn(
            "row.timestamp_pt, row.day, row.day_et, row.symbol, row.side, row.reason,",
            dashboard,
        )
        self.assertIn('statusName.replace(/^rhmra-status-/, "rhmra-gates-")', dashboard)
        self.assertIn("r.buy_candidate === false", dashboard)
        self.assertIn('f.side === "buy" && f.reason === "dip-buy"', dashboard)
        self.assertIn("entry phase ran — bought:", dashboard)
        self.assertIn("scan and evaluation performed", dashboard)
        self.assertIn("filtered out:", dashboard)
        self.assertIn("Opening blackout until", dashboard)
        self.assertIn('timeZone: "America/New_York"', dashboard)
        self.assertIn("timeZoneName: \"short\"", dashboard)
        self.assertIn("esc(tip)", dashboard)

        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()
        blackout = routine.split("**Opening-blackout gate", 1)[1].split(
            "4. Call `get_scans`", 1
        )[0]
        self.assertIn("status snapshot's `entry_skip_reason`", blackout)
        self.assertIn("first <validated numeric NO_BUY_FIRST_MINUTES value> min", blackout)
        self.assertIn("never emit the literal constant name inside the reason", blackout)
        self.assertNotIn(
            '"scan and entry evaluation skipped: opening blackout '
            '(first `NO_BUY_FIRST_MINUTES` min of session)"',
            blackout,
        )

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
        self.assertEqual(c["pt_iso"], "2026-07-21T08:07:00-07:00")
        self.assertEqual(c["date_et"], "2026-07-21")
        self.assertEqual(c["date_pt"], "2026-07-21")
        self.assertEqual(c["session"], "regular")
        self.assertEqual(c["calendar_status"], "normal")
        self.assertEqual(c["regular_close_et"], "16:00")
        self.assertIs(type(c["entry_session_open"]), bool)
        self.assertTrue(c["entry_session_open"])
        self.assertEqual(c["minutes_since_open"], 97)
        self.assertRegex(c["constants_sha256"], r"^[0-9a-f]{64}$")

    def test_winter_offsets_are_standard(self):
        c = self.clock("2026-01-15T15:07:00Z")
        self.assertEqual(c["et"], "2026-01-15 10:07:00 EST")
        self.assertEqual(c["pt"], "2026-01-15 07:07:00 PST")
        self.assertEqual(c["pt_iso"], "2026-01-15T07:07:00-08:00")

    def test_clock_pt_iso_binds_directly_to_lifecycle(self):
        clock = self.clock("2026-07-21T15:07:00Z")
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            start = subprocess.run(
                [
                    sys.executable, RUN_LIFECYCLE, 'start',
                    '--state-file', state_file,
                    '--projection-file', projection_file,
                    '--now-utc', clock['utc'],
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(start.returncode, 0, start.stderr)
            invocation_id = json.loads(start.stdout)['invocation_id']
            bind = subprocess.run(
                [
                    sys.executable, RUN_LIFECYCLE, 'event',
                    '--invocation-id', invocation_id,
                    '--phase', 'preflight',
                    '--run-start-pt', clock['pt_iso'],
                    '--state-file', state_file,
                    '--projection-file', projection_file,
                    '--now-utc', clock['utc'],
                ],
                cwd=ROOT, capture_output=True, text=True,
            )
            self.assertEqual(bind.returncode, 0, bind.stdout + bind.stderr)
            self.assertEqual(
                json.loads(bind.stdout)['run_start_pt'], clock['pt_iso']
            )

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

    def test_clock_reuses_full_validator_not_a_single_row_regex(self):
        with open(os.path.join(ROOT, "constants.md"), encoding="utf-8") as f:
            constants_text = f.read()
        constants_text = ConstantsValidatorTests.replace_value(
            constants_text, "TOP_N", "0"
        )
        with tempfile.TemporaryDirectory() as td:
            constants = os.path.join(td, "constants.md")
            with open(constants, "w", encoding="utf-8") as f:
                f.write(constants_text)
            result = subprocess.run(
                [
                    sys.executable,
                    CLOCK,
                    "--constants",
                    constants,
                    "--now-utc",
                    "2026-07-22T13:37:00Z",
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TOP_N", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        oversized_text = ConstantsValidatorTests.replace_value(
            constants_text, "TOP_N", "9" * 5000
        )
        with tempfile.TemporaryDirectory() as td:
            constants = os.path.join(td, "constants.md")
            with open(constants, "w", encoding="utf-8") as f:
                f.write(oversized_text)
            oversized = subprocess.run(
                [
                    sys.executable,
                    CLOCK,
                    "--constants",
                    constants,
                    "--now-utc",
                    "2026-07-22T13:37:00Z",
                    "--json",
                ],
                capture_output=True,
                text=True,
            )
        self.assertEqual(oversized.returncode, 2)
        self.assertIn("integer literal exceeds the supported size", oversized.stderr)
        self.assertNotIn("Traceback", oversized.stderr)

    def test_expected_constants_hash_pins_the_preflight_configuration(self):
        with open(os.path.join(ROOT, "constants.md"), encoding="utf-8") as f:
            constants_text = f.read()
        constants_text = ConstantsValidatorTests.replace_value(
            constants_text, "DRY_RUN", "true"
        )
        with tempfile.TemporaryDirectory() as td:
            constants = os.path.join(td, "constants.md")
            with open(constants, "w", encoding="utf-8", newline="") as f:
                f.write(constants_text)
            expected_hash = constants_validator.validate_constants_file(
                constants
            ).source_sha256
            command = [
                sys.executable,
                CLOCK,
                "--constants",
                constants,
                "--expected-constants-sha256",
                expected_hash,
                "--now-utc",
                "2026-07-22T13:37:00Z",
                "--json",
            ]
            matching = subprocess.run(
                command, capture_output=True, text=True
            )
            self.assertEqual(matching.returncode, 0, matching.stderr)
            self.assertEqual(
                json.loads(matching.stdout)["constants_sha256"],
                expected_hash,
            )

            changed_text = ConstantsValidatorTests.replace_value(
                constants_text, "DRY_RUN", "false"
            )
            with open(constants, "w", encoding="utf-8", newline="") as f:
                f.write(changed_text)
            changed = subprocess.run(
                command, capture_output=True, text=True
            )

        self.assertEqual(changed.returncode, 2)
        self.assertEqual(changed.stdout, "")
        self.assertIn("constants.md changed after preflight", changed.stderr)
        self.assertNotIn("usage:", changed.stderr)

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
        self.assertIn(
            "`python3 market_clock.py --json --expected-constants-sha256 "
            "<preflight source_sha256>`",
            routine,
        )
        self.assertIn("entry_session_open", routine)
        self.assertIn("exactly the JSON boolean `true`", routine)
        self.assertIn("This applies regardless of `REGULAR_HOURS_BUY_ONLY`", routine)
        run_order = routine.split("### RUN THESE STEPS IN ORDER", 1)[1]
        feasibility = run_order.index("**PRE-SECOND ENTRY-FEASIBILITY GATES")
        second = run_order.index("**SECOND — circuit breaker check")
        self.assertLess(feasibility, second)
        pre_second = run_order[feasibility:second]
        self.assertIn("do NOT run SECOND", pre_second)
        self.assertIn('`circuit_breaker: "not-evaluated"`', pre_second)
        self.assertIn('`stop_fills_today: null`', pre_second)
        self.assertIn("finish lifecycle as normal `completed`", pre_second)

    def test_routine_binds_one_verified_python_before_lifecycle(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        bootstrap_start = routine.index("### PYTHON LAUNCHER BOOTSTRAP")
        lifecycle_start = routine.index("### INVOCATION LIFECYCLE")
        self.assertLess(bootstrap_start, lifecycle_start)
        bootstrap = routine[bootstrap_start:lifecycle_start]
        self.assertIn("ONLY action permitted before invocation lifecycle start", bootstrap)
        self.assertIn("`load_workspace_dependencies`", bootstrap)
        self.assertIn("no more than 20 seconds", bootstrap)
        self.assertIn(".\\resolve_python.ps1", bootstrap)
        self.assertIn("`-PreferredPath`", bootstrap)
        self.assertIn("one-way candidate hint", bootstrap)
        self.assertIn("displayed escape for one Windows", bootstrap)
        self.assertIn("A valid resolver receipt ends launcher resolution immediately", bootstrap)
        self.assertIn("returned `python` field is already launch-probed", bootstrap)
        self.assertIn("Bind it without comparing it to the hint", bootstrap)
        self.assertIn("Never rerun a successful resolver", bootstrap)
        self.assertIn("sole permitted second invocation", bootstrap)
        self.assertIn("an absolute `python` path outside `Microsoft\\WindowsApps`", bootstrap)
        self.assertIn("launch-probes every candidate", bootstrap)
        self.assertIn("Never substitute `Get-Command python` / `where python`", bootstrap)
        self.assertIn("a bare `python` or `python.exe`", bootstrap)
        self.assertIn("retry that exact resolver command once", bootstrap)
        self.assertIn("`sandbox_permissions: require_escalated`", bootstrap)
        self.assertIn("reuse that exact absolute path", bootstrap)

        lifecycle = routine[lifecycle_start:routine.index("### ACCOUNT SCOPE")]
        self.assertIn("`& '<PYTHON_EXE>' run_lifecycle.py start`", lifecycle)
        self.assertIn("already-bound launcher", lifecycle)
        self.assertIn("already-bound `PYTHON_EXE` is the sole launcher", routine)
        self.assertIn(
            "[json.load(open(p, encoding='utf-8')) for p in sys.argv[1:]]",
            routine,
        )
        self.assertIn("'<file1>' '<file2>'", routine)
        self.assertIn("each native path as its own quoted argument", routine)
        self.assertNotIn("<file1> <file2> …", routine)

        status_start = routine.index("**Publish the STATUS SNAPSHOT")
        status_end = routine.index("The filename is exactly:", status_start)
        status = routine[status_start:status_end]
        self.assertIn("verify it parses and persisted by running EXACTLY with the already-bound launcher", status)
        self.assertIn("json.load(open(sys.argv[1], encoding='utf-8'))", status)
        self.assertIn("'<absolute native status path>'", status)
        self.assertIn("The status path is the separate final argument", status)
        self.assertIn("Never interpolate an absolute path into the `-c` source", status)
        self.assertNotIn("json.load(open('<absolute path>", routine)
        self.assertNotIn("verify `py -3 --version`", routine)
        self.assertNotIn("running EXACTLY: `python3 -c", routine)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"),
                         "Windows PowerShell resolver contract")
    def test_windows_python_resolver_returns_verified_absolute_python3(self):
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                RESOLVE_PYTHON,
                "-PreferredPath",
                sys.executable,
            ],
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads(proc.stdout)
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(document["status"], "valid")
        self.assertTrue(os.path.isabs(document["python"]))
        self.assertNotIn("microsoft\\windowsapps", document["python"].lower())
        self.assertEqual(document["version"].split(".", 1)[0], "3")

        direct = subprocess.run(
            [
                document["python"],
                "-I",
                "-c",
                "import sys; print(sys.version_info.major); print(sys.executable)",
            ],
            text=True,
            capture_output=True,
            cwd=ROOT,
            timeout=20,
        )
        self.assertEqual(direct.returncode, 0, direct.stderr)
        direct_lines = direct.stdout.splitlines()
        self.assertEqual(direct_lines[0], "3")
        self.assertEqual(
            os.path.normcase(os.path.realpath(direct_lines[1])),
            os.path.normcase(os.path.realpath(document["python"])),
        )

        with open(RESOLVE_PYTHON, encoding="utf-8-sig") as f:
            resolver = f.read()
        self.assertIn("Microsoft[\\\\/]WindowsApps", resolver)
        self.assertIn(".cache\\codex-runtimes\\*\\dependencies\\python\\python.exe", resolver)
        self.assertIn("Python\\bin\\python.exe", resolver)
        self.assertIn("Programs\\Python\\Python*\\python.exe", resolver)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"),
                         "Windows PowerShell status verifier contract")
    def test_windows_status_verifier_passes_native_path_as_argv(self):
        with tempfile.TemporaryDirectory() as td:
            report_dir = os.path.join(td, "run-reports")
            os.makedirs(report_dir)
            status_path = os.path.join(report_dir, "rhmra-status-test.json")
            with open(status_path, "w", encoding="utf-8") as handle:
                json.dump({"schema_version": 1, "status": "ok"}, handle)

            def ps_literal(value):
                return "'" + value.replace("'", "''") + "'"

            python_source = (
                "import json,sys; "
                "json.load(open(sys.argv[1], encoding='utf-8'))"
            )
            command = (
                f"& {ps_literal(sys.executable)} -I -c "
                f"{chr(34)}{python_source}{chr(34)} "
                f"{ps_literal(status_path)}"
            )
            proc = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    command,
                ],
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=20,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_routine_fences_overlaps_and_rechecks_time_before_buys(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        startup = routine.split(
            '### STARTUP SEQUENCE — complete exactly before normal account or broker access', 1
        )[1].split('### ACCOUNT SCOPE', 1)[0]
        startup_markers = (
            'verified absolute `PYTHON_EXE`',
            '`run_lifecycle.py start`',
            '`validate_constants.py --json`',
            '`market_clock.py --json --expected-constants-sha256',
            '`run_lifecycle.py event --invocation-id <INVOCATION_ID> --phase preflight --run-start-pt <START CLOCK pt_iso>`',
            '`run_lock.py acquire`',
            '`create one NEW session-scoped scratch directory`',
            '`broker_snapshot.py preflight --scratch <scratch>`',
            '`order_intents.py check`',
            '`order_intents.py pending --run-token <RUN_LOCK_TOKEN>`',
            'Resolve `rules_version`',
            'Call `get_accounts`',
        )
        startup_positions = [startup.index(marker) for marker in startup_markers]
        self.assertEqual(startup_positions, sorted(startup_positions))
        self.assertIn('Do not create or preflight scratch', startup)
        self.assertIn('touch the order-intent journal', startup)
        self.assertIn('before successful lease acquisition', startup)
        self.assertIn('Never invent a placeholder token', startup)
        self.assertIn('only the successful `run_lock.py acquire` result can supply it', startup)
        self.assertIn('Items 1–11 normally succeed before `get_accounts`', startup)
        self.assertIn('If item 9 or 10 fails', startup)
        self.assertIn('named read-only positions/orders calls', startup)
        self.assertIn('is the sole exception', startup)
        self.assertIn('no broker mutation is permitted', startup)

        coordination = routine.split(
            "### RUN COORDINATION — fenced single-flight lease", 1
        )[1].split("### ORDER-INTENT JOURNAL", 1)[0]
        self.assertIn(
            "before scratch creation or preflight, any order-intent journal command, "
            "`rules_version`, `get_accounts`, or ANY broker call",
            coordination,
        )
        self.assertIn(
            "A configuration validation/hash failure stops the full run "
            "before lease acquisition",
            coordination,
        )
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

        self.assertIn('before scratch creation or preflight', coordination)
        self.assertIn('any order-intent journal command', coordination)

        journal = routine.split('### ORDER-INTENT JOURNAL', 1)[1].split(
            '### BROKER TIMESTAMPS', 1
        )[0]
        self.assertIn('Only after successful lease acquisition', journal)
        self.assertIn('successful scratch preflight', journal)
        self.assertIn('exact bound lease-issued token', journal)
        self.assertIn('Never run either journal command before acquisition', journal)
        self.assertIn('never use a placeholder', journal)

        order_handling = routine.split(
            "### ORDER HANDLING — AUTONOMOUS, WITH NOTIFICATION", 1
        )[1].split("### DRY RUN", 1)[0]
        self.assertIn("renew this run's fencing lease", order_handling)
        self.assertIn("do not cancel or place", order_handling)

        pre_buy = routine.split(
            "**PRE-BUY LEASE + CLOCK REVALIDATION (EVERY candidate, including DRY RUN):**", 1
        )[1].split("11. For each remaining candidate", 1)[0]
        self.assertIn(
            "run a fresh `market_clock.py --json "
            "--expected-constants-sha256 <preflight source_sha256>`",
            pre_buy,
        )
        self.assertIn("`entry_session_open` is exactly `true`", pre_buy)
        self.assertIn("`opening_blackout` is exactly `false`", pre_buy)
        self.assertIn('`session` must also be exactly `"regular"`', pre_buy)
        self.assertIn("immediately before `place_equity_order`", pre_buy)
        self.assertIn("run the fresh clock check AGAIN", pre_buy)
        self.assertIn("never place a payload reviewed for a different session", pre_buy)
        self.assertIn("Do not fall back to START CLOCK", pre_buy)
        self.assertIn("In DRY RUN", pre_buy)

    def test_routine_full_halts_when_constants_cannot_be_read(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        preflight_start = routine.index("**Mandatory configuration preflight")
        clock_command = routine.index(
            "`python3 market_clock.py --json --expected-constants-sha256 "
            "<preflight source_sha256>`"
        )
        self.assertLess(preflight_start, clock_command)
        preflight = routine[preflight_start:routine.index("\n\nNote the `DRY_RUN`", preflight_start)]
        self.assertIn("Before `market_clock.py`, `get_accounts`", preflight)
        self.assertIn("`& '<PYTHON_EXE>' validate_constants.py --json`", preflight)
        self.assertIn("Do not construct a PowerShell, regex, prose, or ad-hoc replacement validator", preflight)
        self.assertIn("`values` as the SOLE configuration authority", preflight)
        self.assertIn('`status` is exactly `"valid"`', preflight)
        self.assertIn("`constant_count` is exactly `31`", preflight)
        self.assertIn("Never independently re-read or re-parse table rows", preflight)
        self.assertIn(
            "`market_clock.py` is the only permitted later file reader",
            preflight,
        )
        self.assertIn("Never declare the checked-in validator wrong", preflight)
        self.assertIn("FULL-RUN HALT immediately", preflight)
        self.assertIn("This is NOT DRY RUN", preflight)
        self.assertIn("do not review, place, or cancel any order", preflight)
        self.assertIn("normal report, ledger, status snapshot", preflight)
        self.assertNotIn("treat it as `true`", routine)
        self.assertLess(
            routine.index("validate_constants.py --json"),
            clock_command,
        )

        rules_version = routine.split("**rules_version**", 1)[1].split(
            "\n\nAppend one row", 1
        )[0]
        self.assertIn(
            "robinhood-momentum-routine-autonomous.md constants.md "
            "validate_constants.py",
            rules_version,
        )

        dry_run = routine.split("### DRY RUN", 1)[1].split("### CURRENT TIME", 1)[0]
        self.assertIn("NOT DRY RUN", dry_run)
        self.assertIn("never substitute `true`", dry_run)

    def test_routine_keeps_automation_memory_bounded_and_non_authoritative(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        memory_policy = routine.split(
            "**AUTOMATION MEMORY IS BOUNDED AND NEVER AUTHORITATIVE:**", 1
        )[1].split("**Scratch hygiene:**", 1)[0]
        self.assertIn("every run is stateless with respect to `memory.md`", memory_policy)
        self.assertIn("Never use its contents for trading decisions", memory_policy)
        self.assertIn("replace its entire contents; never append", memory_policy)
        self.assertIn("Write exactly one line", memory_policy)
        self.assertIn("Store no candidates, scan counts, balances", memory_policy)

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("Treat every run as stateless", readme)
        self.assertIn("never append scan or account details", readme)
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
        self.assertIn("standard MCP envelope at `structuredContent.data.result`", scan_phase)
        self.assertIn("never call `run_scan` again", scan_phase)
        self.assertIn("successful SECOND generation's proven external source area", scan_phase)
        self.assertIn("same composed tool operation", scan_phase)
        self.assertIn("tools.apply_patch", scan_phase)
        self.assertIn("any `text(...)`, `yield_control`", scan_phase)
        self.assertIn("compact receipt containing the saved path, UTF-8 byte count, and write status", scan_phase)
        self.assertIn("Never emit, print, or yield `JSON.stringify(scanResult)`", scan_phase)
        self.assertIn("Do not infer that persistence is unavailable", scan_phase)
        self.assertIn("Only an actual failed file-change operation", scan_phase)
        self.assertNotIn("under the current scratch directory", scan_phase)
        prefilter = routine.split("8. **Pre-filter the WORKING LIST", 1)[1].split("**The next three bullets", 1)[0]
        self.assertIn("unrounded `volume` × `last`", prefilter)
        self.assertIn("Only the FINAL RSI-enabled `evaluate_candidates.py --json-out`", routine)
        self.assertIn("Transient JSON handoffs are deliberately different", routine)

    def test_routine_surfaces_missing_mcp_tools_with_reconnection_help(self):
        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()

        connector = routine.split('### CONNECTOR FAILURES', 1)[1].split(
            '### ORDER HANDLING', 1
        )[0]
        for required in (
            'startup sequence reaches item 12 after items 1–11 have succeeded',
            'not exposed or callable',
            'No Robinhood request was attempted',
            '`coordination-halt` / `account-scope-failed`',
            'release the lease',
            'Settings → Plugins → MCPs',
            '`robinhood-trading`',
            'choose Authenticate',
            'still absent in that fresh task',
            'remove and re-create',
            '`get_accounts`',
            'fresh task',
            'Do not rerun this automation until that fresh-task check succeeds',
        ):
            self.assertIn(required, connector)

        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        self.assertIn('Settings → Plugins → MCPs', readme)
        self.assertIn('remove and re-create the MCP connection', readme)
        self.assertIn('fresh task exposes `get_accounts`', readme)
        self.assertIn('still absent in that', readme)

    def test_routine_requires_durable_order_intent_reconciliation(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        connector = routine.split("### CONNECTOR FAILURES", 1)[1].split(
            "### ORDER HANDLING", 1
        )[0]
        self.assertIn(
            "Never apply this generic retry paragraph to "
            "`place_equity_order` or `cancel_equity_order`",
            connector,
        )

        journal = routine.split("### ORDER-INTENT JOURNAL", 1)[1].split(
            "### BROKER TIMESTAMPS", 1
        )[0]
        self.assertIn("order_intents.py check", journal)
        self.assertIn("order_intents.py pending --run-token <RUN_LOCK_TOKEN>", journal)
        self.assertIn("before FIRST or any broker mutation", journal)
        self.assertIn("--order-request-cursors FIRST", journal)
        self.assertIn("--position-request-cursors FIRST", journal)
        self.assertIn("the exact cursor used for each later request", journal)
        self.assertIn('"replaces_intent_id": null', journal)
        self.assertIn("`baseline_sha256`", journal)
        self.assertIn("`intent_sha256`", journal)
        for purpose in (
            "dip-buy",
            "profit-take",
            "dust-sweep",
            "initial-stop",
            "stop-repair",
            "stop-retry",
            "profit-take-stop-restore",
        ):
            self.assertIn(purpose, journal)
        self.assertIn("BEFORE any retry", journal)
        self.assertIn('`match_reason: "no_match"`', journal)
        self.assertIn("`same_run_retry_available: true`", journal)
        self.assertIn("SAME `ref_id`", journal)
        self.assertIn("there is no third call", journal)
        self.assertIn("unverified_rejection", journal)
        self.assertIn("can never use automatic retry", journal)
        self.assertIn("A prior-run unresolved entry must NEVER be resubmitted", journal)
        self.assertIn("A partially filled buy is never submitted again", journal)
        self.assertIn("explicit human recovery", journal)
        self.assertNotIn("record-not-submitted", routine)

        dry_run = routine.split("### DRY RUN", 1)[1].split("### CURRENT TIME", 1)[0]
        self.assertIn("A simulated entry creates NO order-intent row", dry_run)

        profit_take = routine.split("2. If a position is up", 1)[1].split(
            "**Stop-coverage audit", 1
        )[0]
        self.assertIn(
            "any exit before `begin` is mandatory finally-style cleanup",
            profit_take,
        )
        self.assertIn("abandon the never-begun intent", profit_take)
        self.assertIn("if cancellation was proven effective", profit_take)
        self.assertIn("ownership was lost", profit_take)
        self.assertIn("do not bypass that guard", profit_take)
        self.assertIn("Once `begin` succeeds, never abandon the intent", profit_take)

        notifications = routine.split("### ORDER HANDLING", 1)[1].split(
            "### DRY RUN", 1
        )[0]
        self.assertIn("terminal reconciliation proves a positive fill", notifications)
        self.assertIn("verification proves it is active", notifications)

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
        self.assertIn("standard MCP envelope at `structuredContent.data.results`", phase)
        self.assertIn("Never extract `structuredContent`", phase)
        self.assertIn("call `get_equity_historicals` again", phase)
        self.assertIn("direct-envelope rule applies only to historical `--bars` files", phase)
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
        self.assertIn(
            "persisted `ref_id` returned by `order_intents.py begin`/`retry`",
            canonical_input,
        )
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
        step_12 = routine.split(
            "12. After the buy intent is terminal", 1
        )[1].split("### REPORT", 1)[0]
        self.assertIn("canonical regular-hours GTC `stop_market` payload", step_12)
        whole_share_guard = routine.split(
            "**Whole-share stop guard", 1
        )[1].split("12. After the buy intent is terminal", 1)[0]
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
        self.assertIn("do not automatically cancel protection", audit)

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
