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
import io
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
from contextlib import contextmanager, redirect_stdout
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path

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
import broker_snapshot as broker_snapshot_module
from broker_snapshot import (
    SnapshotError,
    validate_bound_external_json_source,
    validate_bound_external_json_sources,
    validate_generation_inputs,
)
from market_calendar import (CALENDAR_YEARS, CLOSED_DATES,
                             EARLY_CLOSE_MINUTES_BY_DATE,
                             NORMAL_REGULAR_CLOSE_MINUTE,
                             REGULAR_OPEN_MINUTE)
import run_lifecycle as lifecycle_module

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


@contextmanager
def bound_source_root(scratch):
    """Preflight scratch, bind one accounts canary, then yield its source root."""
    run_cli(BROKER_SNAPSHOT, ["preflight", "--scratch", scratch])
    source_root = tempfile.mkdtemp(prefix="rhmra-response-source-")
    try:
        canary = os.path.join(source_root, "get-accounts-canary.json")
        with open(canary, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "data": {
                        "accounts": [
                            {
                                "nickname": "Agentic",
                                "account_number": "test-account",
                                "agentic_allowed": True,
                            }
                        ]
                    }
                },
                handle,
            )
        run_cli(BROKER_SNAPSHOT, [
            "bind-transport", "--scratch", scratch,
            "--source-root", source_root, "--canary", canary,
            "--account-name", "Agentic",
        ])
        yield source_root
    finally:
        shutil.rmtree(source_root, ignore_errors=True)


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
                result = json.load(f)
            self.assertEqual(json.loads(proc.stdout), result)
            self.assertEqual(proc.stdout.count("\n"), 1)
            return result

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
        self.assertIn(
            'save-transport failure = `snapshot-failure` / '
            '`snapshot-write-failed`',
            lifecycle,
        )
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
        self.assertIn("startup SAVE TRANSPORT BINDING", block)
        self.assertIn("startup-bound file-change facility", block)
        self.assertIn("Never invent or search for a result filename", block)
        self.assertNotIn("broker_snapshot.py source-preflight", routine)
        self.assertEqual(routine.count("broker_snapshot.py bind-transport"), 1)
        self.assertIn("same bound `SOURCE_ROOT`", block)
        self.assertIn("same bound `SOURCE_ROOT` and file-change facility", block)
        self.assertNotIn("start of EACH generation", block)
        self.assertNotIn("fresh source area and probe", block)
        self.assertIn("immediate terminal transport failure", block)
        self.assertIn("MUST NOT start B", block)
        self.assertIn("Generation B is reserved only", block)
        self.assertIn(
            "including all `content`, `structuredContent`, `data`, pagination, "
            "transport-envelope, and `guide` fields",
            block,
        )
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
        self.assertIn(
            "SAME startup-bound `SOURCE_ROOT` and file-change facility",
            block,
        )
        self.assertIn("never combine generations", block)
        self.assertIn("never run generation C", block)
        self.assertIn("`snapshot-failure` / `snapshot-second-attempt-failed`", block)
        self.assertIn("at most 15 minutes old", block)
        self.assertIn(
            "`as_of_utc` exactly DAILY-LOSS FINAL's `utc`",
            block,
        )
        self.assertIn(
            "stdout is exactly one compact JSON object containing the "
            "complete authoritative result",
            block,
        )
        self.assertIn("Bind and validate that stdout object directly", block)
        self.assertIn("retained only as a scratch audit artifact", block)
        self.assertIn(
            "do not read it with a file tool, shell command, or second "
            "Python command",
            block,
        )
        self.assertIn(
            "never embed its Windows path in `-c` source",
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

        scratch_preflight = routine.index(
            "broker_snapshot.py preflight --create-scratch"
        )
        self.assertLess(scratch_preflight, routine.index("### DAILY-LOSS CIRCUIT BREAKER"))
        self.assertNotIn(
            "broker_snapshot.py preflight --scratch <absolute scratch>",
            routine,
        )
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
    @staticmethod
    def evaluate_proc(args):
        return subprocess.run(
            [sys.executable, EVALUATE, *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    @staticmethod
    def evaluation_args(hist, quotes, output, scratch=None):
        args = [] if scratch is None else ["--scratch", scratch]
        return args + [
            "--bars", hist,
            "--quotes", quotes,
            "--volume-lookback-days", "20",
            "--high-lookback-days", "5",
            "--min-median-dollar-volume", "175000",
            "--dip-entry-pct", "5",
            "--json-out", output,
        ]

    def test_cli_requires_scratch_and_writes_no_output(self):
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ) as source_root:
            hist = self.write_json(
                source_root, "hist.json", {"data": {"results": []}}
            )
            quotes = self.write_json(source_root, "quotes.json", {})
            output = os.path.join(scratch, "missing-scratch-output.json")
            proc = self.evaluate_proc(
                self.evaluation_args(hist, quotes, output)
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--scratch", proc.stderr)
            self.assertFalse(os.path.exists(output))

    def test_cli_rejects_alternate_and_mixed_roots_without_output(self):
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ) as source_root, tempfile.TemporaryDirectory() as alternate:
            bound_hist = self.write_json(
                source_root, "hist.json", {"data": {"results": []}}
            )
            alternate_hist = self.write_json(
                alternate, "hist.json", {"data": {"results": []}}
            )
            alternate_quotes = self.write_json(alternate, "quotes.json", {})
            for name, hist in (
                ("alternate", alternate_hist),
                ("mixed", bound_hist),
            ):
                output = os.path.join(scratch, f"{name}-root-output.json")
                proc = self.evaluate_proc(
                    self.evaluation_args(
                        hist, alternate_quotes, output, scratch=scratch
                    )
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(os.path.exists(output))

    def test_cli_rejects_tampered_transport_marker_without_output(self):
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ) as source_root:
            hist = self.write_json(
                source_root, "hist.json", {"data": {"results": []}}
            )
            quotes = self.write_json(source_root, "quotes.json", {})
            marker = os.path.join(
                scratch, ".rhmra-broker-response-transport.json"
            )
            with open(marker, "w", encoding="utf-8") as handle:
                json.dump({}, handle)
            output = os.path.join(scratch, "tampered-marker-output.json")
            proc = self.evaluate_proc(
                self.evaluation_args(
                    hist, quotes, output, scratch=scratch
                )
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(os.path.exists(output))

    @staticmethod
    def write_json(directory, name, value):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
        return path

    def run_eval(self, hist_payload, quotes, extra=None, return_document=False):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            hist = os.path.join(source_root, "hist.json")
            qts = os.path.join(source_root, "quotes.json")
            out = os.path.join(td, "out.json")
            with open(hist, "w", encoding="utf-8") as f:
                json.dump(hist_payload, f)
            with open(qts, "w", encoding="utf-8") as f:
                json.dump(quotes, f)
            cli_extra = list(extra or [])
            if "--rsi-file" in cli_extra:
                first = cli_extra.index("--rsi-file") + 1
                last = first
                while last < len(cli_extra) and not cli_extra[last].startswith("--"):
                    last += 1
                for index in range(first, last):
                    bound_rsi = os.path.join(source_root, f"rsi-{index - first}.json")
                    shutil.copyfile(cli_extra[index], bound_rsi)
                    cli_extra[index] = bound_rsi
            run_cli(EVALUATE, ["--scratch", td, "--bars", hist, "--quotes", qts,
                               "--volume-lookback-days", "20", "--high-lookback-days", "5",
                               "--min-median-dollar-volume", "175000", "--dip-entry-pct", "5",
                               "--json-out", out] + cli_extra)
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
                       if "`& '<PYTHON_EXE>' evaluate_candidates.py --scratch" in line
                       and "--bars" in line)
        self.assertIn('<PYTHON_EXE>', command)
        self.assertIn('--scratch', command)
        self.assertNotIn('py -3 evaluate_candidates.py', command)
        self.assertNotIn('python3 evaluate_candidates.py', command)
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
    @staticmethod
    def filter_proc(args):
        return subprocess.run(
            [sys.executable, FILTER, *args],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    @staticmethod
    def filter_args(scan, output, scratch=None):
        args = [] if scratch is None else ["--scratch", scratch]
        return args + [
            "--scan-file", scan,
            "--price-min", "2.50",
            "--price-max", "5",
            "--min-rel-volume", "2",
            "--min-abs-pct-change", "3",
            "--top-n", "15",
            "--json-out", output,
        ]

    def test_cli_requires_scratch_and_writes_no_output(self):
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ) as source_root:
            scan = os.path.join(source_root, "scan.json")
            with open(scan, "w", encoding="utf-8") as handle:
                json.dump(
                    {"data": {"result": {"results": [], "total_items": 0}}},
                    handle,
                )
            output = os.path.join(scratch, "missing-scratch-output.json")
            proc = self.filter_proc(self.filter_args(scan, output))
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--scratch", proc.stderr)
            self.assertFalse(os.path.exists(output))

    def test_cli_rejects_alternate_root_without_output(self):
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ), tempfile.TemporaryDirectory() as alternate:
            scan = os.path.join(alternate, "scan.json")
            with open(scan, "w", encoding="utf-8") as handle:
                json.dump(
                    {"data": {"result": {"results": [], "total_items": 0}}},
                    handle,
                )
            output = os.path.join(scratch, "alternate-root-output.json")
            proc = self.filter_proc(
                self.filter_args(scan, output, scratch=scratch)
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(os.path.exists(output))

    def run_filter(self, rows, top_n=15, mcp_envelope=False, mcp_error=False):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            scan = os.path.join(source_root, "scan.json")
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
            run_cli(FILTER, ["--scratch", td, "--scan-file", scan,
                             "--price-min", "2.50", "--price-max", "5",
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
        if action == "acknowledge" and "--transport-scratch" not in args:
            transport_scratch, _source_root = self.ack_transport()
            command += ["--transport-scratch", transport_scratch]
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

    def ack_transport(self):
        if not hasattr(self, "_ack_transport_scratch"):
            scratch_context = tempfile.TemporaryDirectory()
            scratch = scratch_context.__enter__()
            self.addCleanup(scratch_context.cleanup)
            source_context = bound_source_root(scratch)
            source_root = source_context.__enter__()
            self.addCleanup(source_context.__exit__, None, None, None)
            self._ack_transport_scratch = scratch
            self._ack_transport_source_root = source_root
        return self._ack_transport_scratch, self._ack_transport_source_root

    def write_ack_response(self, name, value):
        _scratch, source_root = self.ack_transport()
        return self.write_json(source_root, name, value)

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
            response = self.write_ack_response(
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
            stop_response = self.write_ack_response(
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

    def test_acknowledge_rejects_alternate_root_without_state_mutation(self):
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
            transport_scratch, _source_root = self.ack_transport()
            before_sha256 = hashlib.sha256(Path(state).read_bytes()).hexdigest()
            with tempfile.TemporaryDirectory(
                prefix="rhmra-unbound-response-"
            ) as alternate_root:
                response = self.write_json(
                    alternate_root,
                    "response.json",
                    {"data": {"order": self.broker_order()}},
                )
                rejected = self.invoke(
                    state,
                    "acknowledge",
                    "--intent-id",
                    intent_id,
                    "--response",
                    response,
                    "--transport-scratch",
                    transport_scratch,
                    expected_success=False,
                    now="2026-07-31T16:00:03Z",
                )
            self.assertIn(
                "direct child of the invocation-bound response-source root",
                rejected["detail"],
            )
            self.assertEqual(
                hashlib.sha256(Path(state).read_bytes()).hexdigest(),
                before_sha256,
            )
            connection = sqlite3.connect(state)
            try:
                row = connection.execute(
                    "SELECT status, submit_attempts, broker_order_id "
                    "FROM intents WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual(row, ("submitting", 1, None))

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
            partial_response = self.write_ack_response(
                "partial.json", {"data": {"order": partial}}
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
            response = self.write_ack_response(
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
            response = self.write_ack_response(
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
                    response = self.write_ack_response(
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
            response = self.write_ack_response(
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
            working_response = self.write_ack_response(
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
            chain_parent_response = self.write_ack_response(
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
            chain_child_response = self.write_ack_response(
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
    def setUp(self):
        self._transport_roots = {}

    def tearDown(self):
        for source_root in self._transport_roots.values():
            shutil.rmtree(source_root, ignore_errors=True)

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

    @staticmethod
    def valid_accounts_document(
        *, account_number='test-account', label_field='nickname',
        account_name='Agentic', agentic_allowed=True,
    ):
        return {
            'data': {
                'accounts': [
                    {
                        label_field: account_name,
                        'account_number': account_number,
                        'agentic_allowed': agentic_allowed,
                    }
                ]
            }
        }

    def preflight(self, scratch, *, bind_transport=True):
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
        if bind_transport:
            source_root = tempfile.mkdtemp(
                prefix='rhmra-response-source-' + document['scratch_id'] + '-'
            )
            canary = self.write_json(
                source_root,
                'get-accounts-canary.json',
                self.valid_accounts_document(),
            )
            bound, receipt = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertEqual(bound.returncode, 0, (receipt, bound.stderr))
            self.assertEqual(receipt['action'], 'bind-transport')
            self.assertTrue(receipt['ok'])
            self.assertTrue(receipt['canary_removed'])
            self.assertFalse(os.path.exists(canary))
            self._transport_roots[os.path.abspath(scratch)] = source_root
        return document

    def source_root(self, scratch):
        return self._transport_roots[os.path.abspath(scratch)]

    def test_create_scratch_preflights_without_a_caller_supplied_path(self):
        proc, document = self.invoke('preflight', '--create-scratch')
        self.assertEqual(proc.returncode, 0, (document, proc.stderr))
        self.assertEqual(
            set(document),
            {
                'schema_version', 'action', 'ok', 'scratch', 'sentinel_sha256',
                'scratch_id', 'write_read_parse', 'cleanup_verified',
            },
        )
        self.assertEqual(document['schema_version'], 1)
        self.assertEqual(document['action'], 'preflight')
        self.assertTrue(document['ok'])
        self.assertTrue(document['write_read_parse'])
        self.assertTrue(document['cleanup_verified'])
        self.assertRegex(
            document['scratch_id'],
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
            r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
        )
        self.assertRegex(document['sentinel_sha256'], r'^[0-9a-f]{64}$')

        scratch = Path(document['scratch'])
        marker = scratch / '.rhmra-broker-snapshot-scratch.json'
        try:
            self.assertTrue(scratch.is_absolute())
            self.assertEqual(
                scratch.parent,
                Path(tempfile.gettempdir()).resolve(strict=True),
            )
            self.assertTrue(scratch.name.startswith('rhmra-session-'))
            self.assertTrue(scratch.is_dir())
            self.assertFalse(scratch.is_symlink())
            self.assertFalse(
                getattr(scratch, 'is_junction', lambda: False)()
            )
            with open(marker, encoding='utf-8') as handle:
                marker_document = json.load(handle)
            self.assertEqual(
                marker_document['scratch_id'],
                document['scratch_id'],
            )
            self.assertFalse(
                any(
                    child.name.startswith('.rhmra-scratch-preflight-')
                    for child in scratch.iterdir()
                )
            )
        finally:
            marker.unlink(missing_ok=True)
            if scratch.exists():
                os.rmdir(scratch)

    def test_preflight_requires_exactly_one_scratch_mode(self):
        missing, missing_document = self.invoke('preflight')
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing_document['action'], 'preflight')
        self.assertEqual(missing_document['error']['code'], 'usage_error')

        with tempfile.TemporaryDirectory() as td:
            both, both_document = self.invoke(
                'preflight', '--scratch', td, '--create-scratch'
            )
        self.assertNotEqual(both.returncode, 0)
        self.assertEqual(both_document['action'], 'preflight')
        self.assertEqual(both_document['error']['code'], 'usage_error')

    def test_create_scratch_failure_has_stable_error_code(self):
        stdout = io.StringIO()
        with mock.patch.object(
            broker_snapshot_module.tempfile,
            'mkdtemp',
            side_effect=OSError('simulated create failure'),
        ), redirect_stdout(stdout):
            result = broker_snapshot_module.main(
                ['preflight', '--create-scratch']
            )
        self.assertEqual(result, 2)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document['action'], 'preflight')
        self.assertFalse(document['ok'])
        self.assertEqual(
            document['error']['code'],
            'scratch_create_failed',
        )

    def test_failed_created_preflight_removes_only_its_empty_directory(self):
        captured = {}

        def fail_preflight(path):
            captured['scratch'] = Path(path)
            raise SnapshotError('simulated sentinel failure')

        args = mock.Mock(create_scratch=True, scratch=None)
        with mock.patch.object(
            broker_snapshot_module,
            '_preflight_directory',
            side_effect=fail_preflight,
        ):
            with self.assertRaisesRegex(
                SnapshotError, 'simulated sentinel failure'
            ):
                broker_snapshot_module._preflight(args)
        self.assertFalse(captured['scratch'].exists())

    def test_failed_created_preflight_preserves_unexpected_content(self):
        captured = {}

        def fail_with_unexpected_file(path):
            scratch = Path(path)
            captured['scratch'] = scratch
            (scratch / 'unexpected.json').write_text(
                '{}', encoding='utf-8'
            )
            raise SnapshotError('simulated primary failure')

        args = mock.Mock(create_scratch=True, scratch=None)
        try:
            with mock.patch.object(
                broker_snapshot_module,
                '_preflight_directory',
                side_effect=fail_with_unexpected_file,
            ):
                with self.assertRaisesRegex(
                    SnapshotError,
                    'simulated primary failure; cleanup failed:',
                ):
                    broker_snapshot_module._preflight(args)
            scratch = captured['scratch']
            self.assertTrue(scratch.is_dir())
            self.assertTrue((scratch / 'unexpected.json').is_file())
        finally:
            scratch = captured.get('scratch')
            if scratch is not None and scratch.exists():
                (scratch / 'unexpected.json').unlink(missing_ok=True)
                os.rmdir(scratch)

    def test_attempt_tombstone_survives_post_create_readback_failure(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = Path(td)
            scratch_id = '00000000-0000-4000-8000-000000000001'
            source_root = Path(tempfile.gettempdir()) / 'rhmra-response-source-test'
            with mock.patch.object(
                broker_snapshot_module,
                '_read_source',
                side_effect=SnapshotError('simulated readback failure'),
            ):
                with self.assertRaises(SnapshotError):
                    broker_snapshot_module._record_transport_attempt(
                        scratch, scratch_id, source_root
                    )
            marker = scratch / '.rhmra-broker-response-transport-attempt.json'
            self.assertTrue(marker.exists())
            with self.assertRaisesRegex(SnapshotError, 'already attempted'):
                broker_snapshot_module._record_transport_attempt(
                    scratch, scratch_id, source_root
                )

    def test_failed_bind_consumes_one_shot_and_removes_accounts_canary(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            scratch_result = self.preflight(scratch, bind_transport=False)
            source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self._transport_roots[os.path.abspath(scratch)] = source_root
            rejected_canary = self.write_json(
                source_root,
                'errored-get-accounts-canary.json',
                {
                    'isError': True,
                    'structuredContent': {'data': {'accounts': []}},
                },
            )
            rejected, error = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', rejected_canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(error['ok'])
            self.assertFalse(os.path.exists(rejected_canary))
            self.assertFalse(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport.json'
            )))
            self.assertTrue(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )))

            attempt_path = os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )
            with open(attempt_path, 'rb') as handle:
                original_attempt = handle.read()
            self.assertEqual(
                json.loads(original_attempt),
                {
                    'schema_version': 1,
                    'marker': 'rhmra-broker-response-transport-attempt',
                    'scratch_id': scratch_result['scratch_id'],
                    'transport': 'file-change',
                    'source_root': os.path.realpath(source_root),
                },
            )

            retry_canary = self.write_json(
                source_root, 'valid-retry.json', {'data': {'accounts': []}}
            )
            rejected, error = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', retry_canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(error['ok'])
            self.assertIn('already attempted', error['error']['message'])
            self.assertFalse(os.path.exists(retry_canary))
            self.assertFalse(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport.json'
            )))
            with open(attempt_path, 'rb') as handle:
                self.assertEqual(handle.read(), original_attempt)

    def test_bind_rejects_forbidden_json_prefixes_without_stripping_or_retry(self):
        payload = json.dumps(
            self.valid_accounts_document(), separators=(',', ':'), sort_keys=True
        ).encode('utf-8')
        cases = (
            (
                'literal-unicode-escape',
                b'\\ufeff' + payload,
                'source result has forbidden literal six-byte \\ufeff prefix '
                'before JSON',
            ),
            (
                'utf8-bom',
                b'\xef\xbb\xbf' + payload,
                'source result has forbidden UTF-8 BOM before JSON',
            ),
            (
                'leading-space',
                b' ' + payload,
                'source result must be exactly one JSON object with no leading '
                'prefix or trailing decoration',
            ),
            (
                'leading-newline',
                b'\n' + payload,
                'source result must be exactly one JSON object with no leading '
                'prefix or trailing decoration',
            ),
            (
                'trailing-space',
                payload + b' ',
                'source result must be exactly one JSON object with no leading '
                'prefix or trailing decoration',
            ),
            (
                'extra-terminal-newline',
                payload + b'\n\n',
                'source result must be exactly one JSON object with no leading '
                'prefix or trailing decoration',
            ),
            (
                'json-array',
                b'[]',
                'source result must be exactly one JSON object with no leading '
                'prefix or trailing decoration',
            ),
        )
        for label, raw, expected_message in cases:
            with self.subTest(prefix=label), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch, bind_transport=False)
                source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
                self.addCleanup(shutil.rmtree, source_root, True)
                canary = os.path.join(source_root, 'accounts.json')
                with open(canary, 'wb') as handle:
                    handle.write(raw)

                rejected, error = self.invoke(
                    'bind-transport', '--scratch', scratch,
                    '--source-root', source_root, '--canary', canary,
                    '--account-name', 'Agentic',
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(error['ok'])
                self.assertIn(expected_message, error['error']['message'])
                self.assertFalse(os.path.exists(canary))
                self.assertTrue(os.path.exists(os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport-attempt.json',
                )))
                self.assertFalse(os.path.exists(os.path.join(
                    scratch, '.rhmra-broker-response-transport.json',
                )))
                self.assertFalse(os.path.exists(os.path.join(
                    source_root,
                    '.rhmra-broker-response-source-root.json',
                )))

                retry_canary = self.write_json(
                    source_root, 'valid-retry.json',
                    self.valid_accounts_document(),
                )
                retry, retry_error = self.invoke(
                    'bind-transport', '--scratch', scratch,
                    '--source-root', source_root, '--canary', retry_canary,
                    '--account-name', 'Agentic',
                )
                self.assertNotEqual(retry.returncode, 0)
                self.assertFalse(retry_error['ok'])
                self.assertIn(
                    'already attempted', retry_error['error']['message']
                )
                self.assertFalse(os.path.exists(retry_canary))

    def test_invalid_scratch_does_not_consume_attempt_or_cleanup_canary(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self.addCleanup(shutil.rmtree, source_root, True)
            canary = self.write_json(
                source_root, 'accounts.json', self.valid_accounts_document()
            )
            rejected, error = self.invoke(
                'bind-transport', '--scratch', scratch,
                '--source-root', source_root, '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(error['ok'])
            self.assertFalse(os.path.exists(canary))
            self.assertFalse(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )))

            self.preflight(scratch, bind_transport=False)
            canary = self.write_json(
                source_root, 'accounts-after-preflight.json',
                self.valid_accounts_document(),
            )
            bound, receipt = self.invoke(
                'bind-transport', '--scratch', scratch,
                '--source-root', source_root, '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertEqual(bound.returncode, 0, (receipt, bound.stderr))
            self.assertFalse(os.path.exists(canary))

    def test_path_validation_failures_consume_attempt_and_cleanup_canary(self):
        for layout in ('nested-root', 'nested-canary', 'extra-entry'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch, bind_transport=False)
                outer = tempfile.mkdtemp(prefix='rhmra-response-source-')
                self.addCleanup(shutil.rmtree, outer, True)
                source_root = outer
                if layout == 'nested-root':
                    source_root = os.path.join(outer, 'nested')
                    os.mkdir(source_root)
                canary_parent = source_root
                if layout == 'nested-canary':
                    canary_parent = os.path.join(source_root, 'nested')
                    os.mkdir(canary_parent)
                canary = self.write_json(
                    canary_parent, 'accounts.json',
                    self.valid_accounts_document(),
                )
                if layout == 'extra-entry':
                    self.write_text(source_root, 'unexpected.txt', 'extra')

                rejected, error = self.invoke(
                    'bind-transport', '--scratch', scratch,
                    '--source-root', source_root, '--canary', canary,
                    '--account-name', 'Agentic',
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertFalse(error['ok'])
                self.assertEqual(
                    os.path.exists(canary), layout == 'nested-canary'
                )
                self.assertTrue(os.path.exists(os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport-attempt.json',
                )))
                self.assertFalse(os.path.exists(os.path.join(
                    scratch, '.rhmra-broker-response-transport.json'
                )))

                retry_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
                self.addCleanup(shutil.rmtree, retry_root, True)
                retry_canary = self.write_json(
                    retry_root, 'retry.json', {'data': {'accounts': []}}
                )
                retry, retry_error = self.invoke(
                    'bind-transport', '--scratch', scratch,
                    '--source-root', retry_root, '--canary', retry_canary,
                    '--account-name', 'Agentic',
                )
                self.assertNotEqual(retry.returncode, 0)
                self.assertIn('already attempted', retry_error['error']['message'])
                self.assertFalse(os.path.exists(retry_canary))

    def test_successful_bind_uses_separate_scratch_and_is_one_shot(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            scratch_result = self.preflight(scratch, bind_transport=False)
            source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self._transport_roots[os.path.abspath(scratch)] = source_root
            canary_document = {
                'content': [
                    {
                        'type': 'text',
                        'text': '{"data":{"accounts":[]}}',
                    }
                ],
                'structuredContent': {
                    'data': {
                        'accounts': [
                            {
                                'account_number': 'sensitive-account',
                                'nickname': 'Agentic',
                                'agentic_allowed': True,
                            }
                        ]
                    },
                    'guide': 'synthetic connector guide',
                },
            }
            canary = self.write_json(
                source_root, 'get-accounts-canary.json', canary_document
            )
            with open(canary, 'rb') as handle:
                expected_digest = hashlib.sha256(handle.read()).hexdigest()

            proc, receipt = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertEqual(proc.returncode, 0, (receipt, proc.stderr))
            self.assertEqual(
                set(receipt),
                {
                    'schema_version', 'action', 'ok', 'transport', 'scratch',
                    'scratch_id', 'source_root', 'canary_sha256',
                    'canary_removed', 'account_name', 'account_number',
                    'agentic_allowed',
                },
            )
            self.assertEqual(receipt['schema_version'], 1)
            self.assertEqual(receipt['action'], 'bind-transport')
            self.assertTrue(receipt['ok'])
            self.assertEqual(receipt['scratch'], os.path.realpath(scratch))
            self.assertEqual(receipt['scratch_id'], scratch_result['scratch_id'])
            self.assertEqual(receipt['transport'], 'file-change')
            self.assertEqual(receipt['source_root'], os.path.realpath(source_root))
            self.assertEqual(receipt['canary_sha256'], expected_digest)
            self.assertTrue(receipt['canary_removed'])
            self.assertFalse(os.path.exists(canary))
            self.assertEqual(receipt['account_name'], 'Agentic')
            self.assertEqual(receipt['account_number'], 'sensitive-account')
            self.assertIs(receipt['agentic_allowed'], True)

            marker_path = os.path.join(
                scratch, '.rhmra-broker-response-transport.json'
            )
            with open(marker_path, 'rb') as handle:
                original_marker = handle.read()
            marker_document = json.loads(original_marker)
            self.assertRegex(
                marker_document['source_root_id'],
                r'^[0-9a-f]{8}-[0-9a-f-]{27}$',
            )
            self.assertEqual(
                set(marker_document),
                {
                    'schema_version', 'marker', 'scratch_id', 'transport',
                    'source_root', 'source_root_id', 'canary_sha256',
                },
            )
            self.assertEqual(marker_document['scratch_id'], scratch_result['scratch_id'])
            self.assertEqual(marker_document['source_root'], os.path.realpath(source_root))
            self.assertEqual(marker_document['canary_sha256'], expected_digest)
            persistent_marker_paths = [
                os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport-attempt.json',
                ),
                marker_path,
                os.path.join(
                    source_root,
                    '.rhmra-broker-response-source-root.json',
                ),
            ]
            persistent_markers = ''
            for persistent_marker_path in persistent_marker_paths:
                with open(persistent_marker_path, encoding='utf-8') as handle:
                    persistent_markers += handle.read()
            for private_value in (
                'sensitive-account', 'Agentic', 'account_name',
                'account_number', 'agentic_allowed', 'agentic_enabled',
            ):
                self.assertNotIn(private_value, persistent_markers)

            for label, retry_root in (
                ('same-root', source_root),
                ('alternate-root', tempfile.mkdtemp(prefix='rhmra-response-source-')),
            ):
                if retry_root != source_root:
                    self.addCleanup(shutil.rmtree, retry_root, True)
                retry_canary = self.write_json(
                    retry_root, f'{label}.json', {'data': {'accounts': []}}
                )
                rejected, error = self.invoke(
                    'bind-transport',
                    '--scratch', scratch,
                    '--source-root', retry_root,
                    '--canary', retry_canary,
                    '--account-name', 'Agentic',
                )
                with self.subTest(second_bind=label):
                    self.assertNotEqual(rejected.returncode, 0)
                    self.assertFalse(error['ok'])
                    self.assertFalse(os.path.exists(retry_canary))
                    with open(marker_path, 'rb') as handle:
                        self.assertEqual(handle.read(), original_marker)

    def test_bind_resolves_account_name_field_before_canary_deletion(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self.addCleanup(shutil.rmtree, source_root, True)
            canary = self.write_json(
                source_root,
                'accounts.json',
                self.valid_accounts_document(
                    label_field='name', account_number='name-field-account'
                ),
            )
            proc, receipt = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertEqual(proc.returncode, 0, (receipt, proc.stderr))
            self.assertFalse(os.path.exists(canary))
            self.assertEqual(receipt['account_name'], 'Agentic')
            self.assertEqual(receipt['account_number'], 'name-field-account')
            self.assertIs(receipt['agentic_allowed'], True)

    def test_bind_rejects_unresolved_duplicate_disabled_or_malformed_account(self):
        valid = {
            'nickname': 'Agentic',
            'account_number': 'test-account',
            'agentic_allowed': True,
        }
        cases = {
            'no-match': [
                {
                    'nickname': 'Other',
                    'account_number': 'other-account',
                    'agentic_allowed': True,
                }
            ],
            'duplicate-match': [
                valid,
                {
                    'name': 'Agentic',
                    'account_number': 'duplicate-account',
                    'agentic_allowed': True,
                },
            ],
            'disabled': [
                {
                    **valid,
                    'agentic_allowed': False,
                }
            ],
            'empty-account-number': [
                {
                    **valid,
                    'account_number': '',
                }
            ],
            'nonboolean-agentic-allowed': [
                {
                    **valid,
                    'agentic_allowed': 'true',
                }
            ],
            'null-agentic-allowed': [
                {
                    **valid,
                    'agentic_allowed': None,
                }
            ],
            'missing-agentic-allowed': [
                {
                    'nickname': 'Agentic',
                    'account_number': 'test-account',
                }
            ],
            'legacy-agentic-enabled-only': [
                {
                    'nickname': 'Agentic',
                    'account_number': 'test-account',
                    'agentic_enabled': True,
                }
            ],
            'malformed-account-entry': ['not-an-account-object'],
            'malformed-account-label': [
                {
                    **valid,
                    'nickname': 123,
                }
            ],
        }
        for label, accounts in cases.items():
            with self.subTest(case=label), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch, bind_transport=False)
                source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
                self.addCleanup(shutil.rmtree, source_root, True)
                canary = self.write_json(
                    source_root,
                    'accounts.json',
                    {'data': {'accounts': accounts}},
                )
                proc, error = self.invoke(
                    'bind-transport',
                    '--scratch', scratch,
                    '--source-root', source_root,
                    '--canary', canary,
                    '--account-name', 'Agentic',
                )
                self.assertNotEqual(proc.returncode, 0, error)
                self.assertFalse(error['ok'])
                if label in {
                    'nonboolean-agentic-allowed',
                    'null-agentic-allowed',
                    'missing-agentic-allowed',
                    'legacy-agentic-enabled-only',
                }:
                    self.assertIn(
                        '.agentic_allowed: expected a boolean',
                        error['error']['message'],
                    )
                if label == 'disabled':
                    self.assertIn(
                        'account is not accessible to this agent',
                        error['error']['message'],
                    )
                self.assertFalse(os.path.exists(canary))
                self.assertTrue(os.path.exists(os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport-attempt.json',
                )))
                self.assertFalse(os.path.exists(os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport.json',
                )))
                self.assertFalse(os.path.exists(os.path.join(
                    source_root,
                    '.rhmra-broker-response-source-root.json',
                )))

    def test_stage_requires_bound_transport_and_reuses_exact_root_for_a_and_b(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            unbound_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self.addCleanup(shutil.rmtree, unbound_root, True)
            source = self.write_json(
                unbound_root,
                'unbound.json',
                {
                    'data': {
                        'total_value': '1500.01',
                        'cash': '100',
                        'buying_power': '100',
                    }
                },
            )
            output = os.path.join(scratch, 'unbound-output.json')
            rejected, error = self.stage(
                'portfolio', [source], [output], generation='A'
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse(error['ok'])
            self.assertFalse(os.path.exists(output))

            source_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self._transport_roots[os.path.abspath(scratch)] = source_root
            canary = self.write_json(
                source_root, 'accounts.json', self.valid_accounts_document()
            )
            bound, receipt = self.invoke(
                'bind-transport',
                '--scratch', scratch,
                '--source-root', source_root,
                '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertEqual(bound.returncode, 0, (receipt, bound.stderr))
            payload = {
                'data': {
                    'total_value': '1500.01',
                    'cash': '100',
                    'buying_power': '100',
                }
            }
            for generation in ('A', 'B'):
                generation_source = self.write_json(
                    source_root, f'portfolio-{generation}.json', payload
                )
                generation_output = os.path.join(
                    scratch, f'portfolio-{generation}-out.json'
                )
                staged, result = self.stage(
                    'portfolio',
                    [generation_source],
                    [generation_output],
                    generation=generation,
                )
                with self.subTest(generation=generation):
                    self.assertEqual(staged.returncode, 0, result)
                    self.assertTrue(result['ok'])
                    self.assertEqual(result['generation'], generation)

            alternate_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self.addCleanup(shutil.rmtree, alternate_root, True)
            alternate_source = self.write_json(
                alternate_root, 'alternate.json', payload
            )
            nested_root = os.path.join(source_root, 'nested')
            os.mkdir(nested_root)
            nested_source = self.write_json(nested_root, 'nested.json', payload)
            for label, rejected_source in (
                ('alternate-root', alternate_source),
                ('nested-child', nested_source),
            ):
                rejected_output = os.path.join(scratch, f'{label}-out.json')
                proc, result = self.stage(
                    'portfolio', [rejected_source], [rejected_output]
                )
                with self.subTest(path=label):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertFalse(result['ok'])
                    self.assertFalse(os.path.exists(rejected_output))

            real_source = self.write_json(source_root, 'real.json', payload)
            link_source = os.path.join(source_root, 'linked.json')
            try:
                os.symlink(real_source, link_source)
            except (OSError, NotImplementedError):
                link_source = None
            if link_source is not None:
                linked_output = os.path.join(scratch, 'linked-out.json')
                proc, result = self.stage(
                    'portfolio', [link_source], [linked_output]
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertFalse(result['ok'])
                self.assertFalse(os.path.exists(linked_output))

    def test_recreated_source_root_with_new_instance_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            source_root = self.source_root(scratch)
            shutil.rmtree(source_root)
            os.mkdir(source_root)
            marker_path = os.path.join(
                source_root, '.rhmra-broker-response-source-root.json'
            )
            with open(
                os.path.join(scratch, '.rhmra-broker-snapshot-scratch.json'),
                encoding='utf-8',
            ) as handle:
                scratch_id = json.load(handle)['scratch_id']
            self.write_json(
                source_root,
                '.rhmra-broker-response-source-root.json',
                {
                    'schema_version': 1,
                    'marker': 'rhmra-broker-response-source-root',
                    'scratch_id': scratch_id,
                    'source_root_id': '00000000-0000-4000-8000-000000000001',
                },
            )
            source = self.write_json(source_root, 'scan.json', {'data': {}})
            with self.assertRaises(SnapshotError):
                validate_bound_external_json_source(scratch, source)

    def test_concurrent_bind_attempts_produce_exactly_one_bound_transport(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            roots = [
                tempfile.mkdtemp(prefix='rhmra-response-source-')
                for _ in range(2)
            ]
            for root in roots:
                self.addCleanup(shutil.rmtree, root, True)
            canaries = [
                self.write_json(
                    root, 'accounts.json', self.valid_accounts_document()
                )
                for root in roots
            ]

            def bind(index):
                return self.invoke(
                    'bind-transport',
                    '--scratch', scratch,
                    '--source-root', roots[index],
                    '--canary', canaries[index],
                    '--account-name', 'Agentic',
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(bind, range(2)))
            successes = [
                document for proc, document in outcomes if proc.returncode == 0
            ]
            failures = [
                document for proc, document in outcomes if proc.returncode != 0
            ]
            self.assertEqual(len(successes), 1, outcomes)
            self.assertEqual(len(failures), 1, outcomes)
            self.assertTrue(successes[0]['ok'])
            self.assertFalse(failures[0]['ok'])
            self.assertIn('already attempted', failures[0]['error']['message'])
            self.assertFalse(any(os.path.exists(path) for path in canaries))

    def test_exported_validator_reads_only_direct_children_of_bound_root(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            source_root = self.source_root(scratch)
            document = {'data': {'result': 'preserve-exactly'}}
            source = self.write_json(source_root, 'handoff.json', document)

            resolved, parsed, raw = validate_bound_external_json_source(
                scratch, source
            )
            self.assertEqual(resolved, Path(os.path.realpath(source)))
            self.assertEqual(parsed, document)
            with open(source, 'rb') as handle:
                self.assertEqual(raw, handle.read())
            self.assertEqual(
                validate_bound_external_json_sources(scratch, [source]),
                [(resolved, parsed, raw)],
            )

            alternate_root = tempfile.mkdtemp(prefix='rhmra-response-source-')
            self.addCleanup(shutil.rmtree, alternate_root, True)
            alternate = self.write_json(
                alternate_root, 'alternate.json', document
            )
            nested_root = os.path.join(source_root, 'nested-validator')
            os.mkdir(nested_root)
            nested = self.write_json(nested_root, 'nested.json', document)
            for label, rejected_source in (
                ('alternate', alternate),
                ('nested', nested),
            ):
                with self.subTest(source=label):
                    with self.assertRaises(SnapshotError):
                        validate_bound_external_json_source(
                            scratch, rejected_source
                        )

            root_marker = os.path.join(
                source_root, '.rhmra-broker-response-source-root.json'
            )
            os.unlink(root_marker)
            with self.assertRaises(SnapshotError):
                validate_bound_external_json_source(scratch, source)

    def test_legacy_source_preflight_action_is_not_available(self):
        proc, result = self.invoke(
            'source-preflight', '--scratch', 'unused', '--source', 'unused'
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['action'], 'unknown')

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
            source_root = self.source_root(scratch)

            raw = {
                'data': {
                    'total_value': '1500.01',
                    'cash': '100',
                    'buying_power': '100',
                }
            }
            raw_source = self.write_json(source_root, 'raw.json', raw)
            raw_output = os.path.join(scratch, 'raw-out.json')
            proc, result = self.stage('portfolio', [raw_source], [raw_output])
            self.assertEqual(proc.returncode, 0, result)
            self.assertEqual(result['files'][0]['transport'], 'raw')

            positions = {'data': {'positions': [], 'next': None}}
            structured_source = self.write_json(
                source_root,
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
                source_root,
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
            source_root = self.source_root(scratch)
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
                    source_root,
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
            source_root = self.source_root(scratch)
            malformed = (
                ('missing', {'display_currency': 'USD'}),
                ('null', {'buying_power': None}),
                ('object', {'buying_power': {'amount': '1508.9700'}}),
                ('nonfinite', {'buying_power': 'NaN'}),
                ('negative', {'buying_power': '-0.01'}),
            )
            for label, buying_power in malformed:
                source = self.write_json(
                    source_root,
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
            source_root = self.source_root(scratch)
            malformed = (
                ('missing', {}),
                ('null', {'buying_power': None}),
            )
            for label, fields in malformed:
                source = self.write_json(
                    source_root,
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
            source_root = self.source_root(scratch)
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
                source = self.write_json(source_root, f'{kind}.json', payload)
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
            source_root = self.source_root(scratch)
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
                (
                    'portfolio',
                    self.write_text(source_root, 'duplicate.json', duplicate),
                ),
                (
                    'portfolio',
                    self.write_text(source_root, 'nonfinite.json', nonfinite),
                ),
                (
                    'portfolio',
                    self.write_text(source_root, 'malformed.json', malformed),
                ),
                (
                    'positions',
                    self.write_json(
                        source_root, 'positions-shape.json',
                        {'data': {'positions': {}, 'next': None}},
                    ),
                ),
                (
                    'orders',
                    self.write_json(
                        source_root,
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
                        source_root, 'quotes-shape.json',
                        {'data': {'results': [{}] * 21}},
                    ),
                ),
                (
                    'portfolio',
                    self.write_json(
                        source_root,
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
                        source_root,
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
                        source_root,
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
            source_root = self.source_root(scratch)
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
            source = self.write_json(source_root, 'portfolio.json', payload)
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
            source_root = self.source_root(scratch)
            next_url = 'https://agent.robinhood.com/positions?cursor=cursor-two'
            page_one_source = self.write_json(
                source_root,
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
                source_root,
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
            source_root = self.source_root(scratch)
            positions_source = self.write_json(
                source_root, 'positions.json',
                {'data': {'positions': [], 'next': None}},
            )
            orders_source = self.write_json(
                source_root, 'orders.json',
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

    def test_state_filesystem_preflight_rejects_fuse_before_live_state(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            journal_file = state_file + '-journal'
            with open(state_file, 'wb') as handle:
                handle.write(b'production-state')
            with open(journal_file, 'wb') as handle:
                handle.write(b'production-journal')
            before = {
                state_file: b'production-state',
                journal_file: b'production-journal',
            }

            with (
                mock.patch.object(lifecycle_module.os, 'name', 'posix'),
                mock.patch.object(
                    lifecycle_module,
                    '_linux_filesystem_type',
                    return_value='fuse.cowork',
                ),
                mock.patch.object(
                    lifecycle_module, '_probe_sqlite_state_directory'
                ) as probe,
            ):
                with self.assertRaisesRegex(
                    lifecycle_module.LifecycleError,
                    'before production journal access',
                ) as raised:
                    lifecycle_module._prepare_state_directory(td)

            probe.assert_not_called()
            self.assertIn('Code tab with Environment Local', str(raised.exception))
            self.assertIn('Cowork/local-agent', str(raised.exception))
            for path, expected in before.items():
                with open(path, 'rb') as handle:
                    self.assertEqual(handle.read(), expected)

    def test_disposable_state_probe_cleans_up_after_disk_io_failure(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            journal_file = state_file + '-journal'
            with open(state_file, 'wb') as handle:
                handle.write(b'production-state')
            with open(journal_file, 'wb') as handle:
                handle.write(b'production-journal')
            before = {
                state_file: b'production-state',
                journal_file: b'production-journal',
            }

            with mock.patch.object(
                lifecycle_module.sqlite3,
                'connect',
                side_effect=sqlite3.OperationalError('disk I/O error'),
            ):
                with self.assertRaisesRegex(
                    lifecycle_module.LifecycleError, 'disk I/O error'
                ):
                    lifecycle_module._probe_sqlite_state_directory(td)

            self.assertFalse(
                any(
                    name.startswith('.rhmra-sqlite-preflight-')
                    for name in os.listdir(td)
                )
            )
            for path, expected in before.items():
                with open(path, 'rb') as handle:
                    self.assertEqual(handle.read(), expected)

    def test_disposable_state_probe_accepts_native_filesystem(self):
        with tempfile.TemporaryDirectory() as td:
            lifecycle_module._probe_sqlite_state_directory(td)
            self.assertEqual(os.listdir(td), [])

    def test_sqlite_probe_closes_connection_when_rollback_fails(self):
        connection = mock.MagicMock()
        connection.in_transaction = True
        connection.execute.side_effect = sqlite3.OperationalError(
            'probe failed'
        )
        connection.rollback.side_effect = sqlite3.OperationalError(
            'rollback failed'
        )

        with mock.patch.object(
            lifecycle_module.sqlite3,
            'connect',
            return_value=connection,
        ):
            with self.assertRaisesRegex(
                sqlite3.OperationalError, 'rollback failed'
            ):
                lifecycle_module._exercise_sqlite_probe('probe.sqlite3')

        connection.rollback.assert_called_once_with()
        connection.close.assert_called_once_with()

    def test_mountinfo_parser_decodes_escaped_paths_and_uses_longest_mount(self):
        mountinfo = [
            '1 0 0:1 / / rw - overlay overlay rw',
            r'2 1 0:2 / /sessions/D:\134Projects rw - fuse.cowork bridge rw',
            r'3 2 0:3 / /sessions/D:\134Projects/Native rw - ext4 disk rw',
        ]
        self.assertEqual(
            lifecycle_module._filesystem_type_from_mountinfo(
                r'/sessions/D:\Projects/Repository', mountinfo
            ),
            'fuse.cowork',
        )
        self.assertEqual(
            lifecycle_module._filesystem_type_from_mountinfo(
                r'/sessions/D:\Projects/Native/Repository', mountinfo
            ),
            'ext4',
        )

    def test_connect_closes_handle_on_pragma_or_begin_failure(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            for target in (
                'PRAGMA busy_timeout = 10000',
                'PRAGMA synchronous = FULL',
                'PRAGMA foreign_keys = ON',
                'BEGIN IMMEDIATE',
            ):
                with self.subTest(target=target):
                    connection = mock.MagicMock()
                    connection.in_transaction = False

                    def execute(statement, *args):
                        if statement == target:
                            raise sqlite3.OperationalError('disk I/O error')
                        return mock.MagicMock()

                    connection.execute.side_effect = execute
                    with (
                        mock.patch.object(
                            lifecycle_module,
                            '_prepare_state_directory',
                        ) as prepare_state_directory,
                        mock.patch.object(
                            lifecycle_module.sqlite3,
                            'connect',
                            return_value=connection,
                        ),
                    ):
                        with self.assertRaisesRegex(
                            sqlite3.OperationalError, 'disk I/O error'
                        ):
                            lifecycle_module._connect(state_file)
                    prepare_state_directory.assert_called_once_with(td)
                    connection.close.assert_called_once_with()

    def test_disposable_probe_cleanup_failure_is_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                lifecycle_module.shutil,
                'rmtree',
                side_effect=PermissionError('probe still open'),
            ):
                with self.assertRaisesRegex(
                    lifecycle_module.LifecycleError,
                    'disposable probe cleanup failed',
                ):
                    lifecycle_module._probe_sqlite_state_directory(td)
            leftovers = [
                os.path.join(td, name)
                for name in os.listdir(td)
                if name.startswith('.rhmra-sqlite-preflight-')
            ]
            self.assertEqual(len(leftovers), 1)
            shutil.rmtree(leftovers[0])

    def test_hot_journal_detection_rejects_undersized_magic_file(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            journal_file = state_file + '-journal'
            with open(journal_file, 'wb') as handle:
                handle.write(
                    lifecycle_module._SQLITE_ROLLBACK_MAGIC + bytes(504)
                )
            self.assertFalse(
                lifecycle_module._has_hot_rollback_journal(state_file)
            )
            with open(journal_file, 'ab') as handle:
                handle.write(b'x')
            self.assertTrue(
                lifecycle_module._has_hot_rollback_journal(state_file)
            )

    def test_readonly_error_without_hot_journal_stays_generic(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            with open(state_file, 'wb') as handle:
                handle.write(b'placeholder')
            error = sqlite3.OperationalError(
                'attempt to write a readonly database'
            )
            with mock.patch.object(
                lifecycle_module.sqlite3, 'connect', side_effect=error
            ):
                with self.assertRaises(
                    lifecycle_module.LifecycleError
                ) as raised:
                    lifecycle_module.validate_current_projection_read_only(
                        state_file, projection_file
                    )
            message = str(raised.exception)
            self.assertIn('cannot open lifecycle journal read-only', message)
            self.assertNotIn('run_lifecycle.py export', message)

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

    def write_published_status(
        self, state_file,
        *,
        run_start_pt='2026-08-04T09:00:01-07:00',
        filename='rhmra-status-2026_08_04-09_00.json',
    ):
        report_dir = os.path.join(os.path.dirname(state_file), 'run-reports')
        os.makedirs(report_dir, exist_ok=True)
        document = {
            'schema_version': 1,
            'run_start_pt': run_start_pt,
            'rules_version': 'abcd123',
            'dry_run': True,
            'session': 'regular',
            'account': {
                'total_value': 100.0, 'cash': 100.0,
                'buying_power': 100.0, 'equity_value': 0.0,
            },
            'realized_pnl_today': 0.0,
            'positions': [],
            'guards': {
                'circuit_breaker': 'clear',
                'stop_fills_today': 0,
                'entry_phase': 'skipped',
                'entry_skip_reason': 'test fixture',
            },
        }
        with open(
            os.path.join(report_dir, filename),
            'w', encoding='utf-8', newline='\n',
        ) as handle:
            json.dump(document, handle, allow_nan=False)
            handle.write('\n')
        return report_dir

    def write_active_lease(
        self, lock_file, token,
        *, acquired=1785859201.0, renewed=1785859201.0,
        expires=1785860401.0,
    ):
        connection = sqlite3.connect(lock_file)
        try:
            connection.execute(
                'CREATE TABLE run_lease ('
                'singleton INTEGER PRIMARY KEY CHECK (singleton = 1), '
                'token TEXT NOT NULL, acquired_at REAL NOT NULL, '
                'renewed_at REAL NOT NULL, expires_at REAL NOT NULL)'
            )
            connection.execute(
                'INSERT INTO run_lease VALUES (1, ?, ?, ?, ?)',
                (token, acquired, renewed, expires),
            )
            connection.commit()
        finally:
            connection.close()

    def test_active_context_recovers_launcher_and_binding_without_memory(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            binding = self.bind(state_file, projection_file, invocation_id)
            run_token = 'lease-owner-token'
            self.write_active_lease(lock_file, run_token)

            proc, bound = self.invoke(
                state_file, projection_file, 'bind-context',
                '--invocation-id', invocation_id,
                '--run-token', run_token,
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, (bound, proc.stderr))
            self.assertEqual(bound['action'], 'bind-context')
            self.assertEqual(bound['python'], os.path.abspath(sys.executable))
            self.assertEqual(bound['invocation_id'], invocation_id)
            for name in (
                'run_start_pt', 'artifact_stamp', 'expected_report_file',
                'expected_gate_file', 'expected_status_file',
            ):
                self.assertEqual(bound[name], binding[name])

            with open(context_file, encoding='utf-8') as handle:
                private_receipt = json.load(handle)
            self.assertEqual(
                set(private_receipt),
                {
                    'schema_version', 'python', 'version', 'invocation_id',
                    'run_start_pt', 'artifact_stamp', 'expected_report_file',
                    'expected_gate_file', 'expected_status_file',
                    'lease_token_sha256',
                },
            )
            self.assertNotIn(run_token, json.dumps(private_receipt))
            self.assertEqual(
                private_receipt['lease_token_sha256'],
                hashlib.sha256(run_token.encode()).hexdigest(),
            )

            with open(state_file, 'rb') as handle:
                before_state = handle.read()
            with open(projection_file, 'rb') as handle:
                before_projection = handle.read()
            proc, recovered = self.invoke(
                state_file, projection_file, 'recover-context',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:03Z',
            )
            self.assertEqual(proc.returncode, 0, (recovered, proc.stderr))
            self.assertEqual(recovered['action'], 'recover-context')
            self.assertEqual(recovered['classification'], 'running')
            self.assertEqual(recovered['invocation_id'], invocation_id)
            self.assertEqual(recovered['python'], os.path.abspath(sys.executable))
            with open(state_file, 'rb') as handle:
                self.assertEqual(handle.read(), before_state)
            with open(projection_file, 'rb') as handle:
                self.assertEqual(handle.read(), before_projection)

    def test_active_context_fails_closed_for_wrong_or_replaced_lease(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            self.bind(state_file, projection_file, invocation_id)
            self.write_active_lease(lock_file, 'owner')

            proc, rejected = self.invoke(
                state_file, projection_file, 'bind-context',
                '--invocation-id', invocation_id,
                '--run-token', 'not-owner',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 2, rejected)
            self.assertIn('does not own', rejected['detail'])
            self.assertFalse(os.path.exists(context_file))

            proc, _bound = self.invoke(
                state_file, projection_file, 'bind-context',
                '--invocation-id', invocation_id,
                '--run-token', 'owner',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, _bound)
            connection = sqlite3.connect(lock_file)
            try:
                connection.execute(
                    "UPDATE run_lease SET token = 'replacement' WHERE singleton = 1"
                )
                connection.commit()
            finally:
                connection.close()
            proc, rejected = self.invoke(
                state_file, projection_file, 'recover-context',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:03Z',
            )
            self.assertEqual(proc.returncode, 2, rejected)
            self.assertIn('no longer owns', rejected['detail'])

    def test_active_context_strictly_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            self.bind(state_file, projection_file, invocation_id)
            self.write_active_lease(lock_file, 'owner')
            proc, _bound = self.invoke(
                state_file, projection_file, 'bind-context',
                '--invocation-id', invocation_id,
                '--run-token', 'owner',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, _bound)
            with open(context_file, encoding='utf-8') as handle:
                receipt = json.load(handle)
            receipt['expected_report_file'] = 'rhmra-log-2099_01_01-00_00.md'
            with open(context_file, 'w', encoding='utf-8') as handle:
                json.dump(receipt, handle)
            proc, rejected = self.invoke(
                state_file, projection_file, 'recover-context',
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:03Z',
            )
            self.assertEqual(proc.returncode, 1, rejected)
            self.assertIn('expected_report_file is inconsistent', rejected['detail'])

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

    def test_status_returns_exact_authoritative_artifact_binding_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = self.start(
                state_file, projection_file, now='2026-08-12T18:43:41Z'
            )
            invocation_id = started['invocation_id']
            proc, bound = self.invoke(
                state_file, projection_file, 'event',
                '--invocation-id', invocation_id, '--phase', 'preflight',
                '--run-start-pt', '2026-08-12T11:43:42-07:00',
                now='2026-08-12T18:43:42Z',
            )
            self.assertEqual(proc.returncode, 0, bound)
            expected_names = {
                'artifact_stamp': '2026_08_12-11_43',
                'expected_report_file': 'rhmra-log-2026_08_12-11_43.md',
                'expected_gate_file': 'rhmra-gates-2026_08_12-11_43.json',
                'expected_status_file': 'rhmra-status-2026_08_12-11_43.json',
            }
            for key, expected in expected_names.items():
                self.assertEqual(bound[key], expected)

            with open(state_file, 'rb') as handle:
                before_state = handle.read()
            with open(projection_file, 'rb') as handle:
                before_projection = handle.read()
            proc, status = self.invoke(
                state_file, projection_file, 'status',
                '--invocation-id', invocation_id,
            )
            self.assertEqual(proc.returncode, 0, status)
            self.assertEqual(
                set(status),
                {
                    'schema_version', 'action', 'ok', 'invocation_id',
                    'classification', 'phase', 'run_start_pt',
                    'artifact_stamp', 'expected_report_file',
                    'expected_gate_file', 'expected_status_file',
                },
            )
            self.assertEqual(status['run_start_pt'], '2026-08-12T11:43:42-07:00')
            for key, expected in expected_names.items():
                self.assertEqual(status[key], expected)
            with open(state_file, 'rb') as handle:
                self.assertEqual(handle.read(), before_state)
            with open(projection_file, 'rb') as handle:
                self.assertEqual(handle.read(), before_projection)

    def test_status_rejects_missing_unbound_and_finished_invocations_as_json(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            cases = (
                (
                    '--invocation-id',
                    '22222222-2222-4222-8222-222222222222',
                    'has not been started',
                ),
                ('--invocation-id', invocation_id, 'no Pacific time binding'),
            )
            for *arguments, detail in cases:
                with self.subTest(detail=detail):
                    proc, rejected = self.invoke(
                        state_file, projection_file, 'status', *arguments
                    )
                    self.assertEqual(proc.returncode, 2)
                    self.assertEqual(rejected['reason'], 'lifecycle_conflict')
                    self.assertIn(detail, rejected['detail'])

            self.bind(state_file, projection_file, invocation_id)
            report_dir = self.write_published_status(state_file)
            proc, finished = self.invoke(
                state_file, projection_file, 'finish',
                '--invocation-id', invocation_id,
                '--classification', 'completed',
                '--report-file', 'rhmra-log-2026_08_04-09_00.md',
                '--status-file', 'rhmra-status-2026_08_04-09_00.json',
                '--report-dir', report_dir,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, finished)
            proc, rejected = self.invoke(
                state_file, projection_file, 'status',
                '--invocation-id', invocation_id,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn('already finished', rejected['detail'])

            proc, rejected = self.invoke(state_file, projection_file, 'status')
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(rejected['reason'], 'lifecycle_state_error')
            self.assertIn('requires --invocation-id', rejected['detail'])

            proc, rejected = self.invoke(
                state_file, projection_file, 'status',
                '--invocation-id', 'not-a-uuid',
            )
            self.assertEqual(proc.returncode, 1)
            self.assertEqual(rejected['reason'], 'lifecycle_state_error')
            self.assertIn('canonical UUID', rejected['detail'])

    def test_finish_strict_loads_status_and_requires_exact_bound_second(self):
        cases = (
            ('missing', None, 'published snapshot validation failed'),
            ('invalid', {}, 'published snapshot validation failed'),
            (
                'rounded',
                '2026-08-04T09:00:00-07:00',
                'must exactly match the lifecycle binding',
            ),
        )
        for label, status_value, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                state_file = os.path.join(td, 'lifecycle.sqlite3')
                projection_file = os.path.join(td, 'lifecycle.json')
                started = self.start(state_file, projection_file)
                invocation_id = started['invocation_id']
                self.bind(state_file, projection_file, invocation_id)
                report_dir = os.path.join(td, 'run-reports')
                os.makedirs(report_dir, exist_ok=True)
                if status_value == {}:
                    with open(
                        os.path.join(
                            report_dir,
                            'rhmra-status-2026_08_04-09_00.json',
                        ),
                        'w', encoding='utf-8',
                    ) as handle:
                        json.dump(status_value, handle)
                elif isinstance(status_value, str):
                    self.write_published_status(
                        state_file, run_start_pt=status_value
                    )
                proc, rejected = self.invoke(
                    state_file, projection_file, 'finish',
                    '--invocation-id', invocation_id,
                    '--classification', 'completed',
                    '--report-file', 'rhmra-log-2026_08_04-09_00.md',
                    '--status-file', 'rhmra-status-2026_08_04-09_00.json',
                    '--report-dir', report_dir,
                    now='2026-08-04T16:00:02Z',
                )
                self.assertEqual(proc.returncode, 1, rejected)
                self.assertIn(expected_error, rejected['detail'])
                with open(projection_file, encoding='utf-8') as handle:
                    record = json.load(handle)['records'][0]
                self.assertEqual(record['classification'], 'running')
                self.assertTrue(
                    all(event['type'] != 'finish' for event in record['events'])
                )

        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = self.start(state_file, projection_file)
            invocation_id = started['invocation_id']
            self.bind(state_file, projection_file, invocation_id)
            report_dir = self.write_published_status(state_file)
            proc, finished = self.invoke(
                state_file, projection_file, 'finish',
                '--invocation-id', invocation_id,
                '--classification', 'completed',
                '--report-file', 'rhmra-log-2026_08_04-09_00.md',
                '--status-file', 'rhmra-status-2026_08_04-09_00.json',
                '--report-dir', report_dir,
                now='2026-08-04T16:00:02Z',
            )
            self.assertEqual(proc.returncode, 0, finished)
            self.assertEqual(
                finished['status_file'],
                'rhmra-status-2026_08_04-09_00.json',
            )

    def test_terminal_classifications_and_exactly_once_finish(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            report_dir = self.write_published_status(state_file)
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
                        '--report-dir', report_dir,
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
            report_dir = self.write_published_status(state_file)
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
                '--report-dir', report_dir,
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
            (os.path.join("dashboard", "favicon.svg"),
             '<svg xmlns="http://www.w3.org/2000/svg"><circle/><text>R</text></svg>'),
            (os.path.join(
                "run-reports", "rhmra-status-2026_08_04-12_02.json"
            ), "{}"),
            (os.path.join(
                "run-reports", "rhmra-log-2026_08_04-12_02.md"
            ), "Report symbols: \u2713 \u2014\n"),
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

    def request_details(self, method, path, host="127.0.0.1"):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        try:
            connection.request(method, path, headers={"Host": host})
            response = connection.getresponse()
            return (
                response.status,
                response.read(),
                {name.lower(): value for name, value in response.getheaders()},
            )
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

    def test_favicon_redirect_and_asset_preserve_the_static_allowlist(self):
        for method in ("GET", "HEAD"):
            status, body, headers = self.request_details(method, "/favicon.ico")
            self.assertEqual(status, 302, method)
            self.assertEqual(headers.get("location"), "/dashboard/favicon.svg")
            self.assertEqual(headers.get("cache-control"), "no-store")
            self.assertEqual(headers.get("content-length"), "0")
            self.assertEqual(body, b"")

            status, body, headers = self.request_details(
                method, "/dashboard/favicon.svg"
            )
            self.assertEqual(status, 200, method)
            self.assertEqual(headers.get("content-type"), "image/svg+xml")
            if method == "GET":
                self.assertIn(b"<svg", body)
                self.assertIn(b">R</text>", body)
            else:
                self.assertEqual(body, b"")

        self.assertEqual(self.request("GET", "/favicon.svg")[0], 403)
        self.assertEqual(self.request("GET", "/README.md")[0], 403)

    def test_markdown_reports_declare_utf8_for_get_and_head(self):
        path = "/run-reports/rhmra-log-2026_08_04-12_02.md"
        expected_text = "Report symbols: \u2713 \u2014" + os.linesep
        expected = expected_text.encode("utf-8")
        for method in ("GET", "HEAD"):
            status, body, headers = self.request_details(method, path)
            self.assertEqual(status, 200, method)
            self.assertEqual(
                headers.get("content-type"),
                "text/markdown; charset=utf-8",
                method,
            )
            self.assertEqual(headers.get("content-length"), str(len(expected)))
            if method == "GET":
                self.assertEqual(body, expected)
                self.assertEqual(body.decode("utf-8"), expected_text)
            else:
                self.assertEqual(body, b"")

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
        report_dir = os.path.join(self.repo, "run-reports")
        status_path = os.path.join(
            report_dir, "rhmra-status-2026_08_04-12_02.json"
        )
        with open(status_path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                {
                    "schema_version": 1,
                    "run_start_pt": "2026-08-04T12:02:00-07:00",
                    "rules_version": "abcd123",
                    "dry_run": True,
                    "session": "regular",
                    "account": {
                        "total_value": 100.0, "cash": 100.0,
                        "buying_power": 100.0, "equity_value": 0.0,
                    },
                    "realized_pnl_today": 0.0,
                    "positions": [],
                    "guards": {
                        "circuit_breaker": "clear",
                        "stop_fills_today": 0,
                        "entry_phase": "skipped",
                        "entry_skip_reason": "test fixture",
                    },
                },
                handle, allow_nan=False,
            )
            handle.write("\n")

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
            "--report-dir",
            report_dir,
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

        baseline_record_count = document['record_count']
        baseline_high_watermark = document['source_event_high_watermark']
        baseline_size = os.path.getsize(state_file)
        connection = sqlite3.connect(state_file)
        try:
            baseline_page_count = connection.execute(
                'PRAGMA page_count'
            ).fetchone()[0]
            baseline_event_count = connection.execute(
                'SELECT count(*) FROM lifecycle_events'
            ).fetchone()[0]
        finally:
            connection.close()

        crash_script = '''
import os
import sqlite3
import sys

connection = sqlite3.connect(sys.argv[1], isolation_level=None)
connection.execute('PRAGMA journal_mode = DELETE')
connection.execute('PRAGMA synchronous = FULL')
connection.execute('PRAGMA cache_size = 1')
connection.execute('BEGIN IMMEDIATE')
connection.execute('CREATE TABLE interrupted_write (payload BLOB)')
connection.execute(
    'INSERT INTO interrupted_write(payload) VALUES (zeroblob(1048576))'
)
os._exit(0)
'''
        crashed = subprocess.run(
            [sys.executable, '-c', crash_script, state_file],
            text=True,
            capture_output=True,
        )
        self.assertEqual(crashed.returncode, 0, crashed.stderr)
        journal_file = state_file + '-journal'
        self.assertTrue(os.path.isfile(journal_file))
        with open(journal_file, 'rb') as handle:
            self.assertEqual(
                handle.read(8), lifecycle_module._SQLITE_ROLLBACK_MAGIC
            )

        def digest(path):
            with open(path, 'rb') as handle:
                return hashlib.sha256(handle.read()).hexdigest()

        before_read_only = {
            state_file: digest(state_file),
            journal_file: digest(journal_file),
        }
        status, body = self.request('GET', '/api/runs')
        self.assertEqual(status, 500)
        recovery_error = json.loads(body)['error']
        self.assertIn('interrupted SQLite transaction', recovery_error)
        self.assertIn('run_lifecycle.py export', recovery_error)
        self.assertIn('Never delete', recovery_error)
        self.assertEqual(
            {path: digest(path) for path in before_read_only},
            before_read_only,
        )

        recovered = lifecycle('export')
        self.assertEqual(recovered['record_count'], baseline_record_count)
        self.assertEqual(
            recovered['source_event_high_watermark'],
            baseline_high_watermark,
        )
        self.assertFalse(os.path.exists(journal_file))
        self.assertEqual(os.path.getsize(state_file), baseline_size)
        status, body = self.request('GET', '/api/runs')
        self.assertEqual(status, 200)
        document = json.loads(body)
        self.assertEqual(document['record_count'], 1)
        connection = sqlite3.connect(state_file)
        try:
            interrupted_tables = connection.execute(
                'SELECT count(*) FROM sqlite_master '
                'WHERE type = \'table\' AND name = \'interrupted_write\''
            ).fetchone()[0]
            recovered_page_count = connection.execute(
                'PRAGMA page_count'
            ).fetchone()[0]
            recovered_event_count = connection.execute(
                'SELECT count(*) FROM lifecycle_events'
            ).fetchone()[0]
            integrity = connection.execute(
                'PRAGMA integrity_check'
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(interrupted_tables, 0)
        self.assertEqual(recovered_page_count, baseline_page_count)
        self.assertEqual(recovered_event_count, baseline_event_count)
        self.assertEqual(integrity, 'ok')

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
        with open(os.path.join(ROOT, "rules_version.py"), encoding="utf-8") as handle:
            rules_version_helper = handle.read()

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
        self.assertIn('"order_intents.py"', rules_version_helper)
        self.assertIn('"ledger_pnl.py"', rules_version_helper)
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
        bootstrap_contract = routine[bootstrap_start:lifecycle_start]
        self.assertIn(
            'Windows-hosted checkout exposed inside a POSIX/FUSE sandbox',
            bootstrap_contract,
        )
        self.assertIn(
            'never fall back to `/usr/bin/python3`', bootstrap_contract
        )
        self.assertIn(
            'Code sidebar', bootstrap_contract
        )
        self.assertIn('Environment: Local', bootstrap_contract)
        self.assertIn('Cowork/local-agent', bootstrap_contract)
        self.assertIn(
            'before opening the production journal', bootstrap_contract
        )
        lifecycle_contract = routine[
            lifecycle_start:routine.index('### ACCOUNT SCOPE')
        ]
        self.assertIn(
            'terminal for that execution context', lifecycle_contract
        )
        self.assertIn('current shell, not the host OS', lifecycle_contract)
        self.assertIn('native Windows Git Bash', lifecycle_contract)
        self.assertIn(
            'make no further tool call or state-file open', lifecycle_contract
        )
        self.assertIn(
            'do not create a report, status snapshot, gate record',
            lifecycle_contract,
        )
        self.assertIn('`.sqlite3-journal`', lifecycle_contract)
        self.assertIn(
            'Never advise deleting, renaming, copying over, editing',
            lifecycle_contract,
        )
        bootstrap = routine[bootstrap_start:lifecycle_start]
        self.assertIn("ONLY action permitted before invocation lifecycle start", bootstrap)
        self.assertIn("`load_workspace_dependencies`", bootstrap)
        self.assertIn("no more than 20 seconds", bootstrap)
        self.assertIn("-File ./resolve_python.ps1", bootstrap)
        self.assertIn("forward-slash `./resolve_python.ps1`", bootstrap)
        self.assertIn("works in both PowerShell and native Git Bash", bootstrap)
        self.assertNotIn("-File .\\resolve_python.ps1", bootstrap)
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
        self.assertIn("escape it for the current shell", bootstrap)
        self.assertIn("must not be rejected or rewritten", bootstrap)
        self.assertIn('POSIX/native Git Bash', bootstrap)
        self.assertIn('`\'"\'"\'`', bootstrap)

        lifecycle = routine[lifecycle_start:routine.index("### ACCOUNT SCOPE")]
        self.assertIn("`& '<PYTHON_EXE>' run_lifecycle.py start`", lifecycle)
        self.assertIn("already-bound launcher", lifecycle)
        self.assertIn("already-bound `PYTHON_EXE` is the sole launcher", routine)
        self.assertNotIn(
            "[json.load(open(p, encoding='utf-8')) for p in sys.argv[1:]]",
            routine,
        )
        self.assertNotIn("'<file1>' '<file2>'", routine)
        self.assertNotIn(
            '**Validate every derived JSON before running the script',
            routine,
        )
        self.assertNotIn("<file1> <file2> …", routine)

        status_start = routine.index("**Publish the STATUS SNAPSHOT")
        status_end = routine.index("The filename is exactly:", status_start)
        status = routine[status_start:status_end]
        self.assertIn("`status_snapshot.py` is the sole schema authority", status)
        self.assertIn(
            "status_snapshot.py publish --invocation-id '<INVOCATION_ID>' "
            "--scratch", status
        )
        self.assertIn(
            "status_snapshot.py verify --invocation-id '<INVOCATION_ID>' "
            "--scratch", status
        )
        self.assertIn(
            "--report '<absolute project>\\run-reports\\"
            "<EXPECTED_REPORT_FILE>'",
            status,
        )
        self.assertIn("the already-bound `PYTHON_EXE`", status)
        self.assertIn("Every path is a separate opaque argument", status)
        self.assertIn("byte-identical to this invocation's valid scratch candidate", status)
        self.assertIn("`status_snapshot_missing`", status)
        self.assertIn("There is no third candidate rewrite", status)
        self.assertIn("`final-status-unavailable` / `status-write-failed`", status)
        self.assertNotIn("json.load(open(sys.argv[1], encoding='utf-8'))", status)
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

        git_bash_candidates = (
            os.path.join(
                os.environ.get("ProgramFiles", r"C:\Program Files"),
                "Git",
                "bin",
                "bash.exe",
            ),
            shutil.which("bash.exe"),
        )
        git_bash = None
        for candidate in git_bash_candidates:
            if candidate is None or not os.path.isfile(candidate):
                continue
            bash_identity = subprocess.run(
                [candidate, "-c", "uname -s"],
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=20,
            )
            bash_system = bash_identity.stdout.strip().upper()
            if (
                bash_identity.returncode == 0
                and bash_system.startswith(("MINGW", "MSYS"))
            ):
                git_bash = candidate
                break

        def posix_literal(value):
            return "'" + value.replace("'", "'\"'\"'") + "'"

        self.assertEqual(posix_literal("a'b"), "'a'\"'\"'b'")

        if git_bash is not None:
            resolver_command = (
                "powershell.exe -NoProfile -NonInteractive "
                "-ExecutionPolicy Bypass -File ./resolve_python.ps1 "
                f"-PreferredPath {posix_literal(sys.executable)}"
            )
            through_bash = subprocess.run(
                [git_bash, "-c", resolver_command],
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=20,
            )
            self.assertEqual(
                through_bash.returncode, 0, through_bash.stderr
            )
            bash_document = json.loads(through_bash.stdout)
            self.assertEqual(bash_document["schema_version"], 1)
            self.assertEqual(bash_document["status"], "valid")
            self.assertTrue(os.path.isabs(bash_document["python"]))
            self.assertNotIn(
                "microsoft\\windowsapps", bash_document["python"].lower()
            )
            self.assertEqual(
                bash_document["version"].split(".", 1)[0], "3"
            )

            bash_python_command = (
                f"{posix_literal(bash_document['python'])} -I -c "
                f"{posix_literal('import os; print(os.name)')}"
            )
            through_bash_python = subprocess.run(
                [git_bash, "-c", bash_python_command],
                text=True,
                capture_output=True,
                cwd=ROOT,
                timeout=20,
            )
            self.assertEqual(
                through_bash_python.returncode,
                0,
                through_bash_python.stderr,
            )
            self.assertEqual(through_bash_python.stdout.strip(), "nt")

        with open(RESOLVE_PYTHON, encoding="utf-8-sig") as f:
            resolver = f.read()
        self.assertIn("Microsoft[\\\\/]WindowsApps", resolver)
        self.assertIn(".cache\\codex-runtimes\\*\\dependencies\\python\\python.exe", resolver)
        self.assertIn("Python\\bin\\python.exe", resolver)
        self.assertIn("Programs\\Python\\Python*\\python.exe", resolver)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"),
                         "Windows PowerShell context recovery contract")
    def test_windows_resolver_recovers_active_context_without_remembered_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            for name in (
                'resolve_python.ps1', 'run_lifecycle.py', 'market_clock.py',
                'market_calendar.py', 'validate_constants.py',
            ):
                shutil.copy2(os.path.join(ROOT, name), os.path.join(td, name))
            report_dir = os.path.join(td, 'run-reports')
            os.makedirs(report_dir)
            state_file = os.path.join(report_dir, 'rhmra-run-lifecycle.sqlite3')
            projection_file = os.path.join(report_dir, 'rhmra-run-lifecycle.json')
            context_file = os.path.join(report_dir, 'rhmra-active-context.json')
            lock_file = os.path.join(report_dir, 'rhmra-run-lock.sqlite3')

            now = lifecycle_module.datetime.now(
                lifecycle_module.timezone.utc
            ).replace(microsecond=0)
            start_text = now.isoformat().replace('+00:00', 'Z')
            preflight_time = now + lifecycle_module.timedelta(seconds=1)
            preflight_text = preflight_time.isoformat().replace('+00:00', 'Z')
            pt_naive, _name, offset = lifecycle_module.zone_time(
                preflight_time,
                lifecycle_module.PACIFIC_STD_OFFSET,
                'PST',
                'PDT',
            )
            run_start_pt = pt_naive.replace(
                tzinfo=lifecycle_module.timezone(
                    lifecycle_module.timedelta(hours=offset)
                )
            ).isoformat()
            started = lifecycle_module.start_invocation(
                state_file=state_file,
                projection_file=projection_file,
                now_utc=start_text,
            )
            lifecycle_module.record_event(
                invocation_id=started['invocation_id'],
                phase='preflight',
                run_start_pt=run_start_pt,
                state_file=state_file,
                projection_file=projection_file,
                now_utc=preflight_text,
            )
            connection = sqlite3.connect(lock_file)
            try:
                connection.execute(
                    'CREATE TABLE run_lease ('
                    'singleton INTEGER PRIMARY KEY CHECK (singleton = 1), '
                    'token TEXT NOT NULL, acquired_at REAL NOT NULL, '
                    'renewed_at REAL NOT NULL, expires_at REAL NOT NULL)'
                )
                connection.execute(
                    'INSERT INTO run_lease VALUES (1, ?, ?, ?, ?)',
                    ('owner', now.timestamp(), now.timestamp(),
                     now.timestamp() + 1200),
                )
                connection.commit()
            finally:
                connection.close()
            lifecycle_module.bind_active_context(
                invocation_id=started['invocation_id'],
                run_token='owner',
                state_file=state_file,
                projection_file=projection_file,
                context_file=context_file,
                lock_file=lock_file,
                now_utc=preflight_text,
            )

            proc = subprocess.run(
                [
                    'powershell.exe', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-File',
                    os.path.join(td, 'resolve_python.ps1'),
                    '-RecoverActiveContext',
                ],
                text=True,
                capture_output=True,
                cwd=td,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            recovered = json.loads(proc.stdout)
            self.assertEqual(
                set(recovered),
                {
                    'schema_version', 'status', 'python', 'version',
                    'invocation_id', 'classification', 'phase',
                    'run_start_pt', 'artifact_stamp', 'expected_report_file',
                    'expected_gate_file', 'expected_status_file',
                },
            )
            self.assertEqual(recovered['status'], 'recovered')
            self.assertEqual(recovered['invocation_id'], started['invocation_id'])
            self.assertEqual(recovered['python'], os.path.abspath(sys.executable))
            self.assertEqual(recovered['run_start_pt'], run_start_pt)

            connection = sqlite3.connect(lock_file)
            try:
                connection.execute(
                    "UPDATE run_lease SET token = 'replacement' WHERE singleton = 1"
                )
                connection.commit()
            finally:
                connection.close()
            rejected = subprocess.run(
                [
                    'powershell.exe', '-NoProfile', '-NonInteractive',
                    '-ExecutionPolicy', 'Bypass', '-File',
                    os.path.join(td, 'resolve_python.ps1'),
                    '-RecoverActiveContext',
                ],
                text=True,
                capture_output=True,
                cwd=td,
                timeout=20,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(rejected.stdout, '')
            self.assertIn('failed closed', rejected.stderr)

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
            '`run_performance.py resolve-identity --invocation-id '
            '<INVOCATION_ID>',
            '`market_clock.py --json --expected-constants-sha256',
            '`run_lifecycle.py event --invocation-id <INVOCATION_ID> --phase preflight --run-start-pt <START CLOCK pt_iso>`',
            '`run_lock.py acquire`',
            '`run_lifecycle.py bind-context --invocation-id <INVOCATION_ID> --run-token <RUN_LOCK_TOKEN>`',
            '`broker_snapshot.py preflight --create-scratch`',
            '`order_intents.py check`',
            '`order_intents.py pending --run-token <RUN_LOCK_TOKEN>`',
            'Resolve `rules_version`',
            'Call `get_accounts` as the first broker operation',
        )
        startup_positions = [startup.index(marker) for marker in startup_markers]
        self.assertEqual(startup_positions, sorted(startup_positions))
        self.assertIn('Do not create or preflight scratch', startup)
        self.assertIn('touch the order-intent journal', startup)
        self.assertIn('before successful lease acquisition', startup)
        self.assertIn('Never invent a placeholder token', startup)
        self.assertIn('only the successful `run_lock.py acquire` result can supply it', startup)
        self.assertIn(
            'Items 1–12 normally succeed before the one `get_accounts` '
            'transport canary',
            startup,
        )
        self.assertIn('If item 10 or 11 fails', startup)
        self.assertIn('named read-only positions/orders calls', startup)
        self.assertIn('is the sole exception', startup)
        self.assertIn('no broker mutation is permitted', startup)
        self.assertIn('first successful response', startup)
        self.assertIn(
            'An errored connector call may use the routine\'s one generic '
            'read retry',
            startup,
        )
        self.assertIn(
            'after the first successful response never call it again',
            startup,
        )
        self.assertIn('one mandatory SAVE TRANSPORT BINDING', startup)
        self.assertIn(
            'save that COMPLETE unchanged successful response exactly once',
            startup,
        )
        self.assertIn(
            'pass the exact validated `AGENTIC_ACCOUNT_NAME` from item 3',
            startup,
        )
        self.assertIn(
            'bind both the transport and account scope only from the helper\'s validated receipt',
            startup,
        )
        self.assertIn('receipt-issued account scope', startup)
        transport_binding = routine.split(
            '**SAVE TRANSPORT BINDING', 1
        )[1].split('### ORDER-INTENT JOURNAL', 1)[0]
        self.assertEqual(
            transport_binding.count('broker_snapshot.py bind-transport'),
            1,
        )
        self.assertIn('invocation\'s ONE save-path test', transport_binding)
        self.assertIn(
            'first broker operation under the generic read-retry rule',
            transport_binding,
        )
        self.assertIn(
            'Never call `get_accounts` again after that success',
            transport_binding,
        )
        self.assertEqual(
            transport_binding.count(
                'const payload = JSON.stringify(fullToolResult);'
            ),
            1,
        )
        target_declaration = (
            'const targetPath = "<fresh absolute SOURCE_ROOT direct-child '
            'path using / separators>";'
        )
        self.assertIn(target_declaration, transport_binding)
        self.assertLess(
            transport_binding.index(target_declaration),
            transport_binding.index('const fullToolResult = await'),
        )
        self.assertIn('const parsed = JSON.parse(payload);', transport_binding)
        self.assertIn('payload[0] !== "{"', transport_binding)
        self.assertIn('payload[payload.length - 1] !== "}"', transport_binding)
        self.assertIn(
            '"\\n+" + payload.replaceAll("\\n", "\\n+") + '
            '"\\n*** End Patch"',
            transport_binding,
        )
        self.assertIn('Put zero bytes or characters before `{`', transport_binding)
        self.assertIn('literal six-character `\\ufeff`', transport_binding)
        self.assertIn('no real U+FEFF BOM', transport_binding)
        self.assertIn('Never use `String(fullToolResult)`', transport_binding)
        self.assertIn('never emit the raw result or `payload`', transport_binding)
        self.assertIn('exact composed JSON save recipe', transport_binding)
        self.assertNotIn('"\\n+\\ufeff" + payload', transport_binding)
        self.assertIn("--account-name '<validated AGENTIC_ACCOUNT_NAME>'", transport_binding)
        self.assertIn('exactly these twelve fields', transport_binding)
        for field in (
            '`account_name`', '`account_number`', '`agentic_allowed`',
        ):
            self.assertIn(field, transport_binding)
        self.assertIn(
            'Bind `ACCOUNT_NAME`, `ACCOUNT_NUMBER`, and `AGENTIC_ALLOWED` '
            'only from this validated receipt',
            transport_binding,
        )
        self.assertIn('raw response', transport_binding)
        self.assertIn('model memory', transport_binding)
        self.assertIn(
            '`coordination-halt` / `account-scope-failed`',
            transport_binding,
        )
        self.assertIn(
            '`snapshot-failure` / `snapshot-write-failed`',
            transport_binding,
        )
        self.assertIn('make no additional broker call', transport_binding)
        self.assertIn('do not try another directory or writer', transport_binding)
        self.assertIn('do not create a second probe', transport_binding)
        self.assertIn('do not start generation A or B', transport_binding)
        self.assertIn('do not retry the save', transport_binding)

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
        self.assertEqual(
            coordination.count('broker_snapshot.py preflight --create-scratch'),
            2,
        )
        self.assertIn(
            "`& '<PYTHON_EXE>' broker_snapshot.py preflight --create-scratch`",
            coordination,
        )
        self.assertIn(
            "`'<PYTHON_EXE>' broker_snapshot.py preflight --create-scratch`",
            coordination,
        )
        self.assertIn('exactly these eight fields', coordination)
        for field in (
            '`schema_version`', '`action`', '`ok`', '`scratch`',
            '`scratch_id`', '`sentinel_sha256`', '`write_read_parse`',
            '`cleanup_verified`',
        ):
            self.assertIn(field, coordination)
        self.assertIn('resolved non-symlink direct child', coordination)
        self.assertIn('canonical lowercase UUIDv4 string', coordination)
        self.assertIn(
            'Bind `<scratch>` and `SCRATCH_ID` only from this validated receipt',
            coordination,
        )
        self.assertIn('opaque invocation state', coordination)
        self.assertIn(
            'never type, copy, shorten, reconstruct, normalize, or '
            're-transcribe the path',
            coordination,
        )
        self.assertIn(
            'Do not separately call `New-Item`, `mkdir`, `mktemp`, `mkdtemp`',
            coordination,
        )
        self.assertIn('Never retry with `--scratch`', coordination)
        self.assertIn('exactly these four top-level fields', coordination)
        self.assertIn(
            'exactly the two nonempty string fields `code` and `message`',
            coordination,
        )
        self.assertIn('`error.code` exactly `"scratch_create_failed"`', coordination)
        self.assertIn('`error.code` exactly `"invalid_snapshot"`', coordination)
        self.assertIn('any other code', coordination)
        self.assertNotIn(
            'broker_snapshot.py preflight --scratch <absolute scratch>',
            coordination,
        )
        self.assertNotIn(
            'create one NEW session-scoped scratch directory',
            coordination,
        )

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

    def test_routine_timing_boundaries_are_retained_and_telemetry_is_nonfatal(self):
        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()

        coordination = routine.split(
            '### RUN COORDINATION — fenced single-flight lease', 1
        )[1].split('### ORDER-INTENT JOURNAL', 1)[0]
        for required in (
            'FIRST and REPORT strategy timing is helper-owned, never '
            'model-owned',
            'Immediately after the successful FIRST phase-entry renewal',
            'host-stamped lifecycle `position-management` event',
            'immediately after the successful REPORT phase-entry renewal',
            'host-stamped lifecycle `report` event',
            'pass no timestamp, and never retry either marker',
            'A missing, failed, partial, or duplicate marker makes Strategy '
            'execution and Routine overhead unavailable',
            'Never retain, reconstruct, or pass a renewal timestamp to '
            'performance telemetry',
            'refuses conflicting caller values',
        ):
            self.assertIn(required, coordination)

        lifecycle_finish = routine.index(
            '**Finalize lifecycle after persistence and release:**'
        )
        report_readback = routine.index('**Then READ THE REPORT BACK, once')
        telemetry_start = routine.index(
            '### PERFORMANCE TELEMETRY — after lifecycle finish'
        )
        summary_start = routine.index(
            '### FINAL ON-SCREEN RUN SUMMARY — immediately after '
            'performance telemetry'
        )
        self.assertLess(lifecycle_finish, report_readback)
        self.assertLess(report_readback, telemetry_start)
        self.assertLess(telemetry_start, summary_start)
        self.assertLess(
            routine.index('run_performance.py record-internal'), summary_start
        )

        telemetry = routine[telemetry_start:summary_start]
        self.assertEqual(
            routine.count('run_performance.py record-internal'), 1
        )
        for required in (
            'Only after all permitted report/status persistence and '
            'read-back',
            'single successful lifecycle `finish`',
            'exactly once for that invocation',
            '--invocation-id <INVOCATION_ID>',
            '--session',
            "run_performance.py record-internal --invocation-id "
            "<INVOCATION_ID> --session",
            'Do not pass `--runner`, `--model`, `--configuration`, or '
            '`--identity-source` from the model',
            'consumes the invocation-bound identity persisted by the sole '
            'pre-START-CLOCK `resolve-identity` call',
            'missing or unusable identity binding becomes all-unknown '
            'identity',
            'Never pass strategy timestamps',
            'derives the unique host-stamped `position-management` through '
            '`report` lifecycle pair',
            'missing, partial, or duplicated',
            'START CLOCK\'s `session` unchanged',
            '`unknown` only when the invocation finished without a '
            'successful START CLOCK',
            'direct metadata, declaration, self-report, and warning '
            'precedence are never reconstructed at finalization',
            'do not retry or call another timing command',
            'final tool call of a normally reported run',
            'reuse its one internal-record host-clock reading',
            'never make a second clock call',
            '`estimated_run_start_pt`',
            '`estimated_run_end_pt`',
            '`estimated_run_total_ms`',
            '`estimated_run_total_display`',
            '`estimate_clock_source`',
            '`estimate_clock_source` to equal `final-summary-boundary`',
            'all five estimate fields to be null',
            'Never invent, repair, round, reformat, or calculate an estimate',
            'observational and non-authoritative',
            'must never change trading, broker calls, saved report/status '
            'contents, lease handling, lifecycle classification/reason, '
            'or the run result',
            'Timing unavailable: <diagnostic>',
            'before the mandatory last output-file line',
            'The helper value is **Comparable run duration**',
            'later `observe-task` value from an explicit external or '
            'manual source',
            '**Reference run duration** only',
            'never let it displace an available Comparable run duration',
            'same automatic boundary, session class, workload path, '
            'configuration cohort, and preferably rules version',
            'runner/model identity is the explicit comparison dimension',
            'Neither label claims scheduler-start or task-completion '
            'boundaries',
        ):
            self.assertIn(required, telemetry)

        final_summary = routine[summary_start:routine.index(
            'The filename is exactly:', summary_start
        )]
        for required in (
            'Immediately after validating the `record-internal` receipt, '
            'make no further tool call',
            'inside a `<run-summary>` tag',
            'Run start: <estimated_run_start_pt>',
            'Run end: <estimated_run_end_pt>',
            'Comparable run duration: <estimated_run_total_display>',
            'helper-formatted duration byte-for-byte',
            'final transcript summary',
            'Never add them by rewriting the saved report after release',
            'never add `run_end_pt` or any estimate field to the status '
            'snapshot',
            'omit all three lines rather than filling them with guesses',
            'must be the **LAST** line',
            'the run-summary goes BEFORE it',
        ):
            self.assertIn(required, final_summary)
        self.assertNotIn(
            '"run_end_pt"',
            routine.split('**Publish the STATUS SNAPSHOT', 1)[1].split(
                '### PERFORMANCE TELEMETRY', 1
            )[0],
        )

    def test_timing_identity_and_metric_names_do_not_guess(self):
        documents = {}
        for filename in (
            'robinhood-momentum-routine-autonomous.md',
            'README.md',
            'QUICKSTART.md',
            'CLAUDE-LOCAL-SCHEDULING.md',
            'INCIDENTS.md',
        ):
            with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                documents[filename] = f.read()
            self.assertNotIn(
                'meat and potatoes', documents[filename].lower(), filename
            )
            self.assertNotIn('End-to-end task', documents[filename], filename)
            self.assertNotIn(
                'Run total (estimated)', documents[filename], filename
            )
            self.assertNotIn(
                'Observed run duration', documents[filename], filename
            )
            self.assertNotIn(
                'Run duration (estimated)', documents[filename], filename
            )
            self.assertNotIn(
                'Run duration (observed)', documents[filename], filename
            )

        routine = documents['robinhood-momentum-routine-autonomous.md']
        identity = routine.split(
            '### TIMING IDENTITY — deterministic provenance with '
            'self-report fallback', 1
        )[1].split('## Tradeoffs / known limitations', 1)[0]
        for required in (
            'Immediately after the routine-file read returns',
            'before any subsequent launcher/helper/broker call',
            'Who am I in this running task?',
            'runner product, exact model family/version, and current '
            'reasoning/effort setting',
            'SELF_IDENTITY=<runner>|<model>|<configuration>',
            'use the literal string `unknown` for each field',
            '1–48 ASCII characters',
            'shell-inert grammar',
            'may not contain `|`, quotes, backticks, `$`, separators, '
            'slashes, or control characters',
            'never quote or escape an unsafe component into the command',
            'Do not consult or copy a `TIMING_IDENTITY` declaration',
            'the deterministic registry, prior runs, memory, available '
            'tools/connectors, or a global configuration',
            'Do not ask the user, emit the claim in conversation, call a '
            'discovery tool, probe a helper, or repeat',
            'TIMING_IDENTITY: runner=<runner> model=<model> '
            'config=<configuration>',
            'DECLARED_IDENTITY=absent',
            'DECLARED_IDENTITY=invalid',
            'METADATA_IDENTITY=absent',
            'METADATA_IDENTITY=invalid',
            'run_performance.py resolve-identity',
            'before `market_clock.py` and START CLOCK',
            '--self-identity \'<SELF_IDENTITY>\'',
            '--declared-identity \'<DECLARED_IDENTITY>\'',
            '--metadata-identity \'<METADATA_IDENTITY>\'',
            'complete direct metadata, then one valid declaration, then '
            'deterministic runtime evidence combined with an exact '
            'registry-resolved self-report, then unknown',
            'reads only the official `CLAUDECODE` runtime marker and '
            '`CLAUDE_EFFORT` setting with exact `os.environ.get` calls',
            'it never enumerates the environment',
            'Do not inspect, enumerate, copy, echo, store, or pass '
            'environment values yourself',
            'the resolver accepts no environment argument',
            'inherited and spoofable corroboration, not authentication',
            'aggregate `composite` provenance',
            'never maps a vague family label such as `GPT-5`',
            'never maps a vague family label such as `GPT-5` to a specific '
            'model or invents a missing setting',
            '`runner_identity_source`',
            '`model_identity_source`',
            '`configuration_identity_source`',
            '`identity_warning`',
            'Any unknown field, self-reported field, or identity conflict '
            'excludes the record',
            '`TIMING_IDENTITY` declaration synchronized with actual task '
            'settings',
            'strongest symmetric source available to both Claude and Codex',
            'exactly one JSON object with these twelve fields and no others',
            '`runtime-environment`, `self-reported`, `composite`, or '
            '`unknown`',
            'persists the same invocation-bound result',
            'context compaction never authorizes a second '
            'self-identification or resolver call',
            'observational only',
            'continue the trading routine unchanged',
            'no trading, lifecycle, lease, report, status, or broker '
            'authority',
        ):
            self.assertIn(required, identity)

        readme = documents['README.md']
        tested_on = readme.split('## Tested On', 1)[1].split(
            '## Guardrails', 1
        )[0]
        for required in (
            '**Comparable run duration**',
            '**Reference run duration**',
            '**Routine total**',
            '**Strategy execution**',
            '**Routine overhead**',
            '`lifecycle finished_at_utc - lifecycle started_at_utc`',
            'unique host-stamped lifecycle `report - position-management` '
            'interval',
            '`Routine total - Strategy execution`',
            '`Reference run duration - Routine total`',
            'single host-clock reading reused by `record-internal`',
            'exact Run start, Run end, and helper-formatted duration',
            'does not alter the saved report, status schema, lease, '
            'lifecycle outcome, or trading behavior',
            'optional duration supplied after the run by an explicit '
            'external source',
            'not the canonical Claude-versus-Codex metric',
            'unavailable rather than zero',
            'keep after-hours and market-hours samples separate',
            'a later observer can attach a source-specific Reference run '
            'duration',
            '`run_performance.py observe-task`',
            '`codex-worked-for` only for Codex',
            '`claude-run-duration` only for Claude',
            'preserves the source alongside the value',
            'remains secondary when Comparable run duration exists',
            'same session class, workload path, and configuration cohort',
            'preferably rules version',
            'runner/model identity explicit as the comparison dimension',
        ):
            self.assertIn(required, tested_on)
        self.assertIn(
            'A schema-v2-or-newer `final-summary-boundary` value is labeled '
            '**Comparable run duration**',
            readme,
        )
        self.assertIn(
            'Legacy schema-v1 files are reference-only', readme
        )
        self.assertIn(
            'Schema v4 preserves runner, model, and configuration provenance '
            'separately',
            readme,
        )
        self.assertIn(
            'Any unknown field, conflict, or self-reported field is shown in '
            'the existing orange identity diagnostic',
            readme,
        )
        self.assertIn(
            'never changes the chip\'s independent run-health color', readme
        )
        self.assertIn(
            'only bounded enum provenance, never raw environment data',
            readme,
        )

    def test_post_run_observation_commands_are_complete_and_source_preserving(self):
        codex_observe = (
            '& \'<PYTHON_EXE>\' run_performance.py observe-task '
            '--invocation-id \'<INVOCATION_ID>\' '
            '--task-duration-ms <DURATION_MS> --runner codex '
            '--model \'gpt-5.6-luna\' '
            '--configuration \'reasoning=high\' '
            '--identity-source manual-ui '
            '--clock-source codex-worked-for'
        )
        claude_observe = (
            '& \'<PYTHON_EXE>\' run_performance.py observe-task '
            '--invocation-id \'<INVOCATION_ID>\' '
            '--task-duration-ms <DURATION_MS> --runner claude '
            '--model \'claude-sonnet-5\' '
            '--configuration \'effort=high\' '
            '--identity-source manual-ui '
            '--clock-source claude-run-duration'
        )
        documents = {}
        for filename in (
            'README.md', 'QUICKSTART.md', 'CLAUDE-LOCAL-SCHEDULING.md'
        ):
            with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                documents[filename] = f.read()

        for filename in ('README.md', 'QUICKSTART.md'):
            self.assertEqual(documents[filename].count(codex_observe), 1)
            self.assertEqual(documents[filename].count(claude_observe), 1)
        self.assertEqual(
            documents['CLAUDE-LOCAL-SCHEDULING.md'].count(claude_observe), 1
        )
        self.assertNotIn(
            codex_observe, documents['CLAUDE-LOCAL-SCHEDULING.md']
        )
        for filename in documents:
            for required in (
                'Comparable run duration',
                'final-summary-boundary',
                'secondary',
                'Reference run duration',
            ):
                self.assertIn(required, documents[filename], filename)
        self.assertIn(
            'same `record-internal` host-clock reading',
            documents['QUICKSTART.md'],
        )
        self.assertIn(
            'same `record-internal` host-clock reading',
            documents['CLAUDE-LOCAL-SCHEDULING.md'],
        )

        readme = documents['README.md']
        self.assertIn(
            '17m41 Reference run duration (Codex runner metadata)', readme
        )
        self.assertNotIn(
            '17m41 Reference run duration (Codex app UI)', readme
        )
        self.assertIn(
            'An observation supplied by runner metadata must retain '
            '`--identity-source run-metadata` and '
            '`--clock-source runner-metadata`',
            readme,
        )

    def test_scheduler_prompts_declare_exact_timing_identity(self):
        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        self.assertIn(
            'current preferred models are **Claude Sonnet 5 '
            '(effort high)** and **Codex Luna 5.6 (reasoning high)**',
            readme,
        )
        self.assertIn(
            '| Claude Desktop Code tab, Environment Local, native Windows '
            'checkout | `claude-sonnet-4-6`; `effort=high` |',
            readme,
        )
        self.assertIn(
            'New Sonnet 5 runs use a separate `claude-sonnet-5` '
            'comparison cohort; never relabel old 4.6 timings as Sonnet 5',
            readme,
        )
        scheduling = readme.split('### Scheduling', 1)[1].split(
            '### View on Phone setup', 1
        )[0]
        codex_prompt = scheduling.split(
            'Use this prompt in Codex:', 1
        )[1].split('Use this prompt in Claude Desktop Code Local:', 1)[0]
        claude_prompt = scheduling.split(
            'Use this prompt in Claude Desktop Code Local:', 1
        )[1].split('Keep exactly one `TIMING_IDENTITY` line', 1)[0]
        codex_declaration = (
            'TIMING_IDENTITY: runner=codex model=gpt-5.6-luna '
            'config=reasoning=high'
        )
        claude_declaration = (
            'TIMING_IDENTITY: runner=claude model=claude-sonnet-5 '
            'config=effort=high'
        )
        self.assertEqual(codex_prompt.count('TIMING_IDENTITY:'), 1)
        self.assertEqual(claude_prompt.count('TIMING_IDENTITY:'), 1)
        self.assertIn(codex_declaration, codex_prompt)
        self.assertIn(claude_declaration, claude_prompt)
        launch_boundary = (
            '`mark_chapter` is not part of this routine; do not call it. '
            'Begin directly with the routine-file read below, and make no '
            'other model-authored tool call before that read completes.'
        )
        for label, prompt in (
            ('README Codex prompt', codex_prompt),
            ('README Claude prompt', claude_prompt),
        ):
            with self.subTest(prompt=label):
                self.assertEqual(prompt.count(launch_boundary), 1)
                self.assertLess(
                    prompt.index(launch_boundary),
                    prompt.index('TIMING_IDENTITY:'),
                )
        for forbidden in ('TIMING_IDENTITY', 'runner=', 'model='):
            self.assertNotIn(forbidden, launch_boundary)
        codex_setup_image = os.path.join(
            ROOT, 'images', 'codex-automation-setup.png'
        )
        self.assertTrue(os.path.isfile(codex_setup_image))
        self.assertGreater(os.path.getsize(codex_setup_image), 0)
        self.assertIn(
            '![ChatGPT/Codex automation identity example showing '
            'matching TIMING_IDENTITY, GPT-5.6 Luna, and High '
            'reasoning](images/codex-automation-setup.png)',
            codex_prompt,
        )
        self.assertIn(
            '**Model: GPT-5.6 Luna** maps to '
            '`model=gpt-5.6-luna`', codex_prompt
        )
        self.assertIn(
            '**Reasoning: High** maps to '
            '`config=reasoning=high`', codex_prompt
        )
        self.assertIn(
            '**Identity-mapping reference only:** use the complete '
            'maintained prompt above as the copyable source of truth',
            codex_prompt,
        )
        self.assertIn(
            'UI layout, task name/date, project/path, schedule, '
            'notifications, and other settings may vary', codex_prompt
        )
        claude_setup_image = os.path.join(
            ROOT, 'images', 'claude-automation-part-a-setup.png'
        )
        self.assertTrue(os.path.isfile(claude_setup_image))
        self.assertGreater(os.path.getsize(claude_setup_image), 0)
        self.assertIn(
            '![Claude Desktop Local Part A scheduler showing matching '
            'TIMING_IDENTITY, Sonnet 5, Current branch, Worktree off, '
            'Auto, and the hourly weekday cron]'
            '(images/claude-automation-part-a-setup.png)',
            claude_prompt,
        )
        self.assertIn(
            '**Sonnet 5** maps to `model=claude-sonnet-5`',
            claude_prompt,
        )
        self.assertIn(
            '**effort high** maps to `config=effort=high`',
            claude_prompt,
        )
        for required in (
            '**post-validation Part A settings reference**',
            'Use the complete maintained prompt above rather than '
            'transcribing the screenshot',
            'Do not activate this Custom cron until both tasks pass the '
            'supervised Manual `DRY_RUN = true` checks',
            '**Auto** is only the form label',
            'Confirm Allowed permissions and the Pacific-time schedule '
            'preview',
        ):
            self.assertIn(required, claude_prompt)
        self.assertIn(
            'synchronized with the runner, model, and configuration '
            'actually selected', scheduling
        )
        self.assertIn('does not switch the model', scheduling)
        self.assertIn(
            'identity formats are runner-specific and are not '
            'interchangeable', scheduling
        )
        self.assertIn(
            'Codex automation in the ChatGPT/Codex app uses the exact '
            'OpenAI model ID `gpt-5.6-luna` and '
            '`config=reasoning=high`', scheduling
        )
        self.assertIn(
            'current Claude Desktop Code setup uses '
            '`claude-sonnet-5` and `config=effort=high`', scheduling
        )
        self.assertIn(
            'one exact, all-or-nothing tuple', scheduling
        )
        self.assertIn(
            'do not use Claude\'s `effort=high` configuration with '
            'Codex', scheduling
        )
        for required in (
            'one pre-helper self-report',
            'only framework-explicit identity',
            'Direct metadata remains strongest',
            'reads only `CLAUDECODE` and `CLAUDE_EFFORT`',
            'without enumerating the environment',
            'runtime evidence plus self-report can produce a `composite` '
            'identity',
            'Any unknown field, self-reported field, or conflict excludes '
            'the sample from primary fair comparisons',
            'strongest symmetric source across Claude and Codex',
            'no trading authority',
        ):
            self.assertIn(required, scheduling)

        with open(
            os.path.join(ROOT, 'CLAUDE-LOCAL-SCHEDULING.md'),
            encoding='utf-8',
        ) as f:
            claude_guide = f.read()
        task_prompt = claude_guide.split('```text', 1)[1].split('```', 1)[0]
        self.assertEqual(task_prompt.count('TIMING_IDENTITY:'), 1)
        self.assertIn(claude_declaration, task_prompt)
        self.assertEqual(task_prompt.count(launch_boundary), 1)
        self.assertLess(
            task_prompt.index(launch_boundary),
            task_prompt.index('TIMING_IDENTITY:'),
        )
        self.assertIn(
            'launch boundary is unconditional and does not depend on '
            '`TIMING_IDENTITY`',
            claude_guide,
        )
        self.assertIn(
            'keep it synchronized with the model and effort actually '
            'selected', claude_guide
        )
        for required in (
            'one structured self-report',
            'checked-in exact-match registry',
            'reads only `CLAUDECODE` and `CLAUDE_EFFORT` through exact '
            '`os.environ.get` calls',
            'never enumerates, copies, echoes, stores, or passes environment '
            'values',
            'inherited environment can be spoofed',
            'corroboration rather than authentication',
            'field-level `composite`',
            'Any unknown field, self-reported field, or conflict excludes '
            'the run from primary fair-comparison cohorts',
            'strongest symmetric source across Claude and Codex',
            'cannot affect trading',
        ):
            self.assertIn(required, claude_guide)

        with open(os.path.join(ROOT, 'QUICKSTART.md'), encoding='utf-8') as f:
            quickstart = f.read().split(
                '## Timing for later scheduled runs', 1
            )[1]
        self.assertIn('copy exactly one matching declaration', quickstart)
        self.assertIn(codex_declaration, quickstart)
        self.assertIn(claude_declaration, quickstart)
        self.assertIn(
            'source-specific **Reference run duration** can still be '
            'attached',
            quickstart,
        )
        for required in (
            'one structured self-report',
            'Complete direct task metadata',
            'registry-recognized self-report',
            'reads only the allowlisted `CLAUDECODE` runtime marker and '
            '`CLAUDE_EFFORT` setting',
            'never enumerates or persists the environment',
            'inherited/spoofable corroboration rather than authentication',
            'field-level `composite`',
            'Any unknown field, self-reported field, or conflict excludes '
            'the record from primary fair-comparison cohorts',
            'strongest symmetric comparison source for Claude and Codex',
        ):
            self.assertIn(required, quickstart)

        with open(os.path.join(ROOT, 'INCIDENTS.md'), encoding='utf-8') as f:
            incident = f.read()
        for required in (
            'Claude exposed split identity evidence rather than one '
            'introspection API',
            '`CLAUDECODE` and `CLAUDE_EFFORT` environment keys',
            'no local Python script can introspect the serving model',
            'reads exactly those two allowlisted keys with '
            '`os.environ.get`',
            'never enumerates or persists the environment',
            'corroboration rather than authentication',
            'Schema v4 records each field\'s provenance',
            'Claude called unavailable `mark_chapter` before reading the '
            'routine',
            'No such tool available',
            'no order, cancellation, or broker mutation',
            'This launch boundary never branches on',
        ):
            self.assertIn(required, incident)

        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()
        for required in (
            '`mark_chapter` is not part of this routine; never call it',
            'Do not call any framework chapter, progress, or phase tool',
            '`run_lifecycle.py` helper is the only run-phase recorder',
            'unconditional and has no dependency on `TIMING_IDENTITY`',
        ):
            self.assertIn(required, routine)

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
        self.assertIn(
            "Before identity resolution, `market_clock.py`, `get_accounts`",
            preflight,
        )
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
        self.assertIn(
            'one non-authoritative identity resolver', preflight
        )
        self.assertIn('pre-START-CLOCK helper actions', preflight)
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
        self.assertLess(
            routine.index("run_performance.py resolve-identity"),
            clock_command,
        )

        rules_version = routine.split("**rules_version**", 1)[1].split(
            "\n\nAppend one row", 1
        )[0]
        self.assertIn("& '<PYTHON_EXE>' rules_version.py", rules_version)
        self.assertIn(
            "exactly `schema_version`, `status`, and `rules_version`",
            rules_version,
        )
        self.assertIn("The helper owns the canonical rule-set file list", rules_version)
        self.assertIn(
            "Never run, interpret, or substitute `git describe`, `git log`, "
            "`git status`, or `git diff`",
            rules_version,
        )
        self.assertNotIn("git log -1", rules_version)
        self.assertNotIn("status --porcelain", rules_version)

        dry_run = routine.split("### DRY RUN", 1)[1].split("### CURRENT TIME", 1)[0]
        self.assertIn("NOT DRY RUN", dry_run)
        self.assertIn("never substitute `true`", dry_run)

    def test_routine_binds_lifecycle_artifacts_and_recovers_without_guessing(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        lifecycle = routine.split(
            "### INVOCATION LIFECYCLE", 1
        )[1].split("**Mandatory configuration preflight", 1)[0]
        for required in (
            "BOUND_RUN_START_PT",
            "ARTIFACT_STAMP",
            "EXPECTED_REPORT_FILE",
            "EXPECTED_GATE_FILE",
            "EXPECTED_STATUS_FILE",
            "retained invocation state through any context compaction",
            "Never round `BOUND_RUN_START_PT` to a minute",
            "run_lifecycle.py status --invocation-id <INVOCATION_ID>",
            "exactly these eleven fields",
            "action: \"status\"",
            "classification `running`",
            "This command is read-only",
            "Never use `run_lifecycle.py export`, a second START CLOCK, "
            "direct SQLite/projection access, a context summary",
            "Never pass the test-only lifecycle path overrides",
            "-RecoverActiveContext",
            "exact raw `RUN_LOCK_TOKEN` remains retained",
            "exactly these thirteen fields",
            '`status: "recovered"`, classification `running`',
            "The receipt cannot recover a lost raw lease token",
            "make no further broker call or artifact write and stop "
            "fail-closed",
        ):
            self.assertIn(required, lifecycle)

        bootstrap = routine.split(
            "### PYTHON LAUNCHER BOOTSTRAP", 1
        )[1].split("### INVOCATION LIFECYCLE", 1)[0]
        for required in (
            "PYTHON_EXE` is retained invocation state",
            "MUST survive context compaction unchanged",
            "If the exact binding is lost before the lease-bound "
            "active-context receipt exists, stop",
            "sole exception is the explicit `-RecoverActiveContext` path",
            "never execute a literal `py`, `python`, or `python3` command "
            "later in the run",
        ):
            self.assertIn(required, bootstrap)

        report = routine.split("### REPORT", 1)[1]
        status = report.split(
            "**Publish the STATUS SNAPSHOT", 1
        )[1].split("### PERFORMANCE TELEMETRY", 1)[0]
        self.assertIn(
            '"run_start_pt": "<exact BOUND_RUN_START_PT', status
        )
        self.assertIn("copy `BOUND_RUN_START_PT` byte-for-byte", status)
        self.assertIn("never round it to `HH:MM:00`", status)
        self.assertEqual(
            status.count(
                "status_snapshot.py publish --invocation-id '<INVOCATION_ID>'"
            ),
            2,
        )
        self.assertEqual(
            status.count(
                "status_snapshot.py verify --invocation-id '<INVOCATION_ID>'"
            ),
            1,
        )
        self.assertIn(
            "status_file` exactly equal to `EXPECTED_STATUS_FILE", status
        )
        self.assertIn(
            "--report '<absolute project>\\run-reports\\"
            "<EXPECTED_REPORT_FILE>'",
            status,
        )
        self.assertIn(
            "report is the nonempty, strict-UTF-8 lifecycle-bound file",
            status,
        )
        self.assertLess(
            status.index("Before any candidate rewrite or second publish"),
            status.index("replace the scratch candidate once"),
        )
        self.assertIn(
            "exact invocation-bound `publish` command one final time", status
        )

        coordination = routine.split(
            "### RUN COORDINATION", 1
        )[1].split("### ORDER-INTENT JOURNAL", 1)[0]
        self.assertIn(
            "Before release, require the report's bare name to equal "
            "`EXPECTED_REPORT_FILE`", coordination
        )
        self.assertIn(
            "never release and then create, rename, rewrite, or repair",
            coordination,
        )
        for required in (
            "run_lifecycle.py bind-context --invocation-id "
            "'<INVOCATION_ID>' --run-token '<RUN_LOCK_TOKEN>'",
            '`action: "bind-context"`',
            "`python` exactly equal to `PYTHON_EXE`",
            "stores only a SHA-256 ownership binding, never the raw token",
            "stop before scratch creation or any broker call",
        ):
            self.assertIn(required, coordination)
        lifecycle_finish = report.split(
            "**Finalize lifecycle after persistence and release:**", 1
        )[1].split("**AUTOMATION MEMORY IS DISABLED", 1)[0]
        self.assertIn("--report-file <EXPECTED_REPORT_FILE>", lifecycle_finish)
        self.assertIn("--status-file <EXPECTED_STATUS_FILE>", lifecycle_finish)
        self.assertIn(
            "must never trigger a post-release rewrite or a second `finish`",
            lifecycle_finish,
        )
        self.assertIn(
            'save-transport failure uses `snapshot-failure` / '
            '`snapshot-write-failed`',
            lifecycle_finish,
        )

    def test_routine_repeats_phase_fences_and_serializes_final_refresh(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        phase_ranges = (
            ("**FIRST ", "**PRE-SECOND", "FIRST"),
            ("**SECOND ", "**THIRD ", "SECOND"),
            ("**THIRD ", "**FOURTH ", "THIRD"),
            ("**FOURTH ", "### REPORT", "FOURTH"),
            ("### REPORT", "**FINAL STATUS REFRESH", "REPORT"),
        )
        for start, end, phase in phase_ranges:
            section = routine.split(start, 1)[1].split(end, 1)[0]
            self.assertIn(f"**{phase} phase-entry fence:**", section)
            self.assertIn("required", section)
            self.assertIn("lease renewal", section)
            self.assertIn("retained `PYTHON_EXE`", section)
            self.assertIn("exact `RUN_LOCK_TOKEN`", section)
        second = routine.split("**SECOND ", 1)[1].split(
            "**THIRD ", 1
        )[0]
        self.assertIn("The FIRST renewal does not satisfy SECOND", second)

        final_refresh = routine.split(
            "**FINAL STATUS REFRESH", 1
        )[1].split(
            "**Finalize lifecycle after persistence and release:**", 1
        )[0]
        pnl_payload = (
            '{ "account_number": "<resolved at runtime>", "span": "day", '
            '"asset_classes": ["equity"], '
            '"timezone": "America/New_York" }'
        )
        self.assertIn(pnl_payload, final_refresh)
        self.assertIn(
            "Complete, validate, and dedupe each call/page before starting "
            "the next", final_refresh
        )
        self.assertIn(
            "strictly sequential and must never be issued in parallel",
            final_refresh,
        )
        self.assertIn(
            "retry MUST repeat the identical full payload above", final_refresh
        )
        self.assertIn(
            "never omit the asset class, substitute a start/end date form, "
            "or change arguments", final_refresh
        )

        evaluator = routine.split(
            "Then RE-RUN with", 1
        )[1].split(
            "**FINAL machine-readable handoff", 1
        )[0]
        self.assertIn(
            "--json-out run-reports/<EXPECTED_GATE_FILE>", evaluator
        )
        self.assertIn('<PYTHON_EXE>', evaluator)
        self.assertIn('--scratch', evaluator)
        self.assertNotIn('py -3 evaluate_candidates.py', evaluator)
        self.assertNotIn('python3 evaluate_candidates.py', evaluator)
        self.assertIn(
            "never rebuild it from current time, another clock, or a context "
            "summary", evaluator
        )

        first = routine.split("**FIRST phase-entry fence:**", 1)[1].split(
            "**PRE-SECOND", 1
        )[0]
        self.assertIn(
            "run_lifecycle.py event --invocation-id <INVOCATION_ID> "
            "--phase position-management",
            first,
        )
        report_phase = routine.split("**REPORT phase-entry fence:**", 1)[1].split(
            "**FINAL STATUS REFRESH", 1
        )[0]
        self.assertIn(
            "run_lifecycle.py event --invocation-id <INVOCATION_ID> "
            "--phase report",
            report_phase,
        )
        self.assertIn(
            "Lifecycle `finish` is never a strategy boundary", report_phase
        )
        telemetry = routine.split(
            "### PERFORMANCE TELEMETRY", 1
        )[1].split("The filename is exactly:", 1)[0]
        self.assertIn(
            "derives the unique host-stamped `position-management` through "
            "`report` lifecycle pair",
            telemetry,
        )
        self.assertIn(
            "Never pass strategy timestamps",
            telemetry,
        )

    def test_routine_keeps_automation_memory_bounded_and_non_authoritative(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        memory_policy = routine.split(
            "**AUTOMATION MEMORY IS DISABLED AND NEVER AUTHORITATIVE:**", 1
        )[1].split("**Scratch hygiene:**", 1)[0]
        self.assertIn("every run is stateless", memory_policy)
        self.assertIn(
            "Never read, create, edit, append to, or replace `memory.md`",
            memory_policy,
        )
        self.assertIn("never call a framework memory tool", memory_policy)
        self.assertIn("Memory is not a recovery channel", memory_policy)
        self.assertIn("verified report/status artifacts", memory_policy)

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("Treat every run as stateless", readme)
        self.assertEqual(readme.count("do not call a framework memory tool"), 2)
        self.assertNotIn("never append scan or account details", readme)
    def test_routine_uses_machine_readable_scan_handoff(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        for forbidden in (
            'find /sessions',
            'locate it by basename',
            'harness-created tool-result file/resource',
            'broker_snapshot.py source-preflight',
            're-`Write`',
            're-Write',
            'The sandbox evaporates',
        ):
            self.assertNotIn(forbidden, routine)
        self.assertIn(
            'Never use a harness-advertised path, search for a result file',
            routine,
        )

        scan_phase = routine.split("6. `run_scan`", 1)[1].split("**FOURTH", 1)[0]
        filter_command = next(
            line for line in scan_phase.splitlines()
            if 'filter_scan.py --' in line
        )
        self.assertIn('<PYTHON_EXE>', filter_command)
        self.assertIn('--scratch', filter_command)
        self.assertNotIn('py -3 filter_scan.py', filter_command)
        self.assertNotIn('python3 filter_scan.py', filter_command)
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
        self.assertIn("deterministic output schema/value checks fail", scan_phase)
        self.assertIn("do NOT fall back to formatted stdout, a stale file, or ad-hoc filtering", scan_phase)
        self.assertIn("empty `working_list: []` is valid", scan_phase)
        self.assertIn("standard MCP envelope at `structuredContent.data.result`", scan_phase)
        self.assertIn("never call `run_scan` again", scan_phase)
        self.assertIn("startup-bound `SOURCE_ROOT`", scan_phase)
        self.assertIn("same bound file-change capability", scan_phase)
        self.assertIn("same composed tool operation", scan_phase)
        self.assertIn("tools.apply_patch", scan_phase)
        self.assertIn("EXACT COMPOSED JSON SAVE RECIPE", scan_phase)
        self.assertIn("COMPLETE `fullToolResult`", scan_phase)
        self.assertIn("zero-prefix/zero-decoration", scan_phase)
        self.assertIn("any `text(...)`, `yield_control`", scan_phase)
        self.assertIn("compact saved-path receipt", scan_phase)
        self.assertIn(
            "do not run `TextEncoder`, an ad-hoc byte counter, add a "
            "BOM/prefix, or perform another path/save experiment",
            scan_phase,
        )
        self.assertIn(
            "A save denial or path mismatch is terminal for the entire run as "
            "`snapshot-failure` / `snapshot-write-failed`",
            scan_phase,
        )
        self.assertIn("Never emit, print, or yield `JSON.stringify(scanResult)`", scan_phase)
        self.assertIn(
            'An actual failed write, failed strict read of that just-written '
            'file',
            scan_phase,
        )
        self.assertIn(
            'invocation-bound source-validation failure is run-level '
            '`snapshot-failure` / `snapshot-write-failed`',
            scan_phase,
        )
        self.assertIn(
            'semantic/output failure after a successful bound read remains '
            'the entry-only `scan handoff failure`',
            scan_phase,
        )
        self.assertIn(
            'do not retry the save, locate another copy, or switch paths or '
            'transports',
            scan_phase,
        )
        self.assertNotIn("under the current scratch directory", scan_phase)
        prefilter = routine.split("8. **Pre-filter the WORKING LIST", 1)[1].split("**The next three bullets", 1)[0]
        self.assertIn("unrounded `volume` × `last`", prefilter)
        self.assertIn("Only the FINAL RSI-enabled `evaluate_candidates.py --json-out`", routine)
        self.assertIn("Transient JSON handoffs are deliberately different", routine)
        self.assertIn(
            "A save denial or unreadable bound file is terminal for the entire "
            "run as `snapshot-failure` / `snapshot-write-failed`",
            routine,
        )
        self.assertIn(
            "Any unbound, alternate-root, nested, missing, changed, or "
            "unreadable input is run-level `snapshot-failure` / "
            "`snapshot-write-failed`",
            routine,
        )
        self.assertIn(
            "correctly bound and strictly read input that later fails "
            "deterministic schema, semantic, or evaluator-output validation "
            "is instead terminal only for that candidate or entry phase",
            routine,
        )
        pre_rsi_command = next(
            line for line in routine.splitlines()
            if "`& '<PYTHON_EXE>' evaluate_candidates.py --scratch" in line
            and '--bars' in line
        )
        self.assertIn('<PYTHON_EXE>', pre_rsi_command)
        self.assertIn('--scratch', pre_rsi_command)
        self.assertNotIn('py -3 evaluate_candidates.py', pre_rsi_command)
        self.assertNotIn('python3 evaluate_candidates.py', pre_rsi_command)

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
            'startup sequence reaches item 13 after items 1–12 have succeeded',
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
            'Claude Code recovery-path override',
            'Customize → Connectors',
            'Settings → Connectors',
            '`/mcp`',
            'choose Re-authenticate',
            'If reauthentication still fails',
            'remove only that single existing Robinhood connector',
            'add it back once there',
            'complete OAuth, and restart Claude',
            'never leave or create a duplicate',
            'same main checkout with worktree isolation off',
            'click Run now',
            'proof that the scheduled context can see the connector',
            'Only if no account connector exists and the standalone `claude` CLI is installed',
            'claude mcp add --transport http robinhood-trading',
            'optionally confirm it with `claude mcp list`',
            'never use the CLI to create a duplicate',
        ):
            self.assertIn(required, connector)

        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        self.assertIn('Settings → Plugins → MCPs', readme)
        self.assertIn('remove and re-create the MCP connection', readme)
        self.assertIn('fresh task exposes `get_accounts`', readme)
        self.assertIn('still absent in that', readme)
        self.assertIn(
            'Claude Desktop Code tab, Environment Local', readme
        )
        for required in (
            '## Tested On',
            '| Runner | Model / configuration | After-hours timing record | Market-hours timing record | Status |',
            '4m40 Reference run duration (Claude transcript); '
            '4m05 Routine total (lifecycle)',
            'Pending — measure during a market-hours run',
            '6m03 Reference run duration (Codex app UI); '
            '4m16 Routine total (lifecycle)',
            '17m41 Reference run duration (Codex runner metadata); '
            '14m24 Routine total (lifecycle)',
            'historical wall-clock records from this installation',
            'not controlled benchmarks',
            'different reference sources',
            'useful context but not the canonical fair comparison',
            'supporting Routine total durations were 4m05 and 4m16',
            "Codex's market-hours reference was 17m41",
            '2026-08-10 full market-hours Routine total',
            '15-candidate scan was 14m24',
            'market-hours timing has not yet been measured',
            'Future Claude-versus-Codex and model-version comparisons use '
            '**Comparable run duration**',
            'boundaries are identical on both runners',
            'same session class, workload path, and configuration cohort',
            'Repeated samples are preferable to a single run',
        ):
            self.assertIn(required, readme)
        self.assertNotIn('## Models and runner support', readme)
        for required in (
            'Part A native bootstrap/MCP reads/lifecycle+lease',
            'prescribed per-task `DRY_RUN` acceptance still required',
            '**Part A only**',
            'used `DRY_RUN = false`',
            'Part B was never tested with **Run now**',
            'after hours with a flat account',
            'no order-mutation tool call',
            '**Schedule: Manual** first',
            'saved Custom cron becomes active immediately',
            'immediately Pause it in task detail',
            'Only after both proofs pass',
            'Scheduled tasks must run at most once per hour.',
            '`0 6-13 * * 1-5`',
            '`30 6-13 * * 1-5`',
            'local machine/app timezone',
            'intended Pacific bounds',
            'randomized start delays',
            'permission control is currently labeled **Auto**',
            'Allowed permissions',
            '`place_equity_order`',
            '`cancel_equity_order`',
            'do not enable live Claude trading',
            'original **Cowork/Scheduled** interface',
            'Code **Routines** list does not control it',
            'bind its exact returned `PYTHON_EXE`',
        ):
            self.assertIn(required, readme)
        self.assertNotIn(
            'Schedules must run at most once per hour', readme
        )
        self.assertNotIn('deterministic start delays', readme)
        self.assertNotIn(
            'host-native `py -3 run_lifecycle.py export`', readme
        )
        self.assertIn('Routines → New routine → Local', readme)
        self.assertIn('isolated-worktree option **off**', readme)
        self.assertIn('Customize → Connectors', readme)
        self.assertIn('Settings → Connectors', readme)
        self.assertIn('Do not add a duplicate', readme)
        self.assertIn('choose **Re-authenticate**', readme)
        self.assertIn('If reauthentication still fails', readme)
        self.assertIn(
            'remove only that single existing Robinhood connector', readme
        )
        self.assertIn('add it back once there', readme)
        self.assertIn('complete OAuth, and restart Claude', readme)
        self.assertIn('never leave or create a duplicate', readme)
        self.assertIn('same native Windows main checkout', readme)
        self.assertIn('Desktop local scheduled task', readme)
        self.assertIn('success proves the scheduled context can see the connector', readme)
        self.assertIn(
            'Only if no account connector exists and the standalone `claude` CLI is installed',
            readme,
        )
        self.assertIn('optionally confirm it with `claude mcp list`', readme)
        self.assertIn('never use the CLI to create a duplicate', readme)
        self.assertIn(
            'Read `./robinhood-momentum-routine-autonomous.md`', readme
        )
        self.assertNotIn(
            'Read `\\RobinhoodEquityTradingAgent\\robinhood-momentum-routine-autonomous.md`',
            readme,
        )
        self.assertIn('(CLAUDE-LOCAL-SCHEDULING.md)', readme)
        with open(os.path.join(ROOT, 'QUICKSTART.md'), encoding='utf-8') as f:
            quickstart = f.read()
        self.assertIn(
            'brand-new Claude Desktop **Code** session whose '
            '**Environment** selector above the prompt is **Local**',
            quickstart,
        )
        self.assertIn('Do not use Claude Cowork/local-agent', quickstart)
        self.assertIn('do not fall back to `/usr/bin/python3`', quickstart)
        self.assertIn('genuinely native Linux/macOS checkout', quickstart)
        self.assertIn('(CLAUDE-LOCAL-SCHEDULING.md)', quickstart)

        guide_path = os.path.join(ROOT, 'CLAUDE-LOCAL-SCHEDULING.md')
        with open(guide_path, encoding='utf-8') as f:
            claude_guide = f.read()
        for required in (
            'execution-environment example only',
            'copy the prompt, schedule, model, permission mode',
            'this guide—not from the image',
            '**Schedule: Manual**',
            'Saving a Local task with a Custom cron makes it active '
            'immediately',
            'Only after both Manual **Run now** proofs pass',
            'immediately open its task detail, choose **Pause**',
            'verify that it is disabled and that no run began',
            'Scheduled tasks must run at most once per hour.',
            '`0 6-13 * * 1-5`',
            '`30 6-13 * * 1-5`',
            'randomized delay',
            'local machine/app timezone',
            'intended Pacific bounds',
            'original **Cowork/Scheduled** interface',
            'Code **Routines** list does not control that legacy task',
            'permission control is currently labeled **Auto**',
            '**Allowed permissions**',
            'If this Claude build cannot keep both',
            'do not enable live Claude trading',
            '**Part A only**',
            '`DRY_RUN = false`',
            'Part B was never tested with **Run now**',
            'after hours with a flat account',
            'no order-mutation tool call',
            'bind the exact returned `python` value as `PYTHON_EXE`',
            'Never substitute a bare `py`, `python`, or `python3`',
            'Sonnet 5 with effort high (`claude-sonnet-5`, '
            '`effort=high`)',
            'change each task\'s model selector and `TIMING_IDENTITY` '
            'line together',
            'An intentionally retained Sonnet 4.6 task must use '
            '`model=claude-sonnet-4-6 config=effort=high`',
            '**post-validation Part A settings/identity reference**',
            'pictured Custom cron must be added only after both Manual '
            '`DRY_RUN = true` proofs pass',
            '**Auto** label does not prove mutation safety',
        ):
            self.assertIn(required, claude_guide)
        self.assertNotIn(
            'Schedules must run at most once per hour', claude_guide
        )
        self.assertNotIn('deterministic delay', claude_guide)
        self.assertNotIn(
            '`py -3 run_lifecycle.py export`', claude_guide
        )

        manual_creation = claude_guide.index(
            'Create uniquely named Part A and Part B Local tasks with '
            '**Schedule: Manual**'
        )
        part_a_run = claude_guide.index(
            'While Part A still has **Schedule: Manual**, open it and '
            'click **Run now**'
        )
        part_b_run = claude_guide.index(
            'Repeat **Run now** for Part B while it is still Manual'
        )
        add_crons = claude_guide.index(
            'Only after both Manual tests succeed should you edit Part A '
            'and Part B to their Custom crons'
        )
        self.assertLess(manual_creation, part_a_run)
        self.assertLess(part_a_run, part_b_run)
        self.assertLess(part_b_run, add_crons)

        for relative_image in (
            'images/claude-local-routine-form.png',
            'images/claude-local-hourly-limit.png',
            'images/claude-automation-part-a-setup.png',
        ):
            with self.subTest(image=relative_image):
                self.assertIn(f']({relative_image})', claude_guide)
                image_path = os.path.join(
                    ROOT, *relative_image.split('/')
                )
                self.assertTrue(os.path.isfile(image_path), image_path)
                self.assertGreater(os.path.getsize(image_path), 0, image_path)

        with open(os.path.join(ROOT, 'INCIDENTS.md'), encoding='utf-8') as f:
            incidents = f.read()
        resolution = incidents.split(
            '**2026-08-11 15:55 resolution', 1
        )[1].split('## BROKER TIMESTAMPS', 1)[0]
        resolution = re.sub(r'\s+', ' ', resolution)
        for required in (
            'Part A alone ran',
            '`DRY_RUN = false`',
            'Part B was never tested with **Run now**',
            'flat and the run occurred after hours',
            'No order-mutation tool was called',
            '`DRY_RUN = true` acceptance',
            'Scheduled tasks must run at most once per hour.',
            'saved Custom cron becomes active immediately',
            '**Schedule: Manual**',
            'add the Custom crons only after both proofs pass',
            'immediately Pause it in task detail',
            'permission control currently labeled **Auto**',
            'Allowed permissions',
            'intended Pacific bounds',
            'randomized start delays',
            'original Cowork/Scheduled interface',
            'Code Routines list does not control it',
        ):
            self.assertIn(required, resolution)
        self.assertNotIn(
            'Schedules must run at most once per hour', resolution
        )
        self.assertNotIn('deterministic start delays', resolution)

    def test_quickstart_preserves_safe_first_test_contract_and_local_links(self):
        with open(os.path.join(ROOT, 'QUICKSTART.md'), encoding='utf-8') as f:
            quickstart = f.read()
        connector = quickstart.split(
            '## 1. Connect Robinhood once', 1
        )[1].split('## 2. Paste one prompt', 1)[0]
        setup_prompt = quickstart.split('```text', 1)[1].split(
            '```', 1
        )[0]

        for required in (
            'ChatGPT Desktop in Codex mode, or Codex',
            'exact settings path may vary by app version',
            '(README.md#first-time-app-setup)',
            'Inspect the installed connectors first',
            'exactly one Robinhood connector should exist',
            'setup must never create a duplicate',
            'Authenticate** or **Re-authenticate',
            'Add one only if no Robinhood connector exists',
            'If no Robinhood connector exists and you add one',
            'first inspect the account connector in Claude Desktop',
            'add exactly one custom connector and complete OAuth',
            'ask which known connector to keep',
            'remove the duplicates explicitly',
            'never silently delete an unknown connector',
            'Restart Claude, then open a brand-new **Code** session',
            '**Environment** selector above the prompt to **Local**',
            'select this exact repository\'s main checkout',
            'keep worktree isolation **off**',
            'Code sidebar is only a list filter',
            'remove only that single connector',
            'add it back once',
            'verify `get_accounts` in another fresh task/session',
            'Never leave or create a duplicate',
            'before any broker work',
        ):
            self.assertIn(required, connector)

        claude = connector.split('- **Claude Code:**', 1)[1].split(
            '\n\n', 1
        )[0]
        claude_order = (
            'account connector',
            'complete OAuth',
            'Restart Claude',
            'brand-new **Code** session',
            '**Environment** selector above the prompt',
            'exact repository\'s main checkout',
            '`/mcp`',
        )
        for earlier, later in zip(claude_order, claude_order[1:]):
            self.assertLess(claude.index(earlier), claude.index(later))

        for required in (
            'Never ask me to paste passwords, MFA codes, account numbers',
            'changing `DRY_RUN` from `false` to `true` if necessary',
            'writing the confirmed `AGENTIC_ACCOUNT_NAME`',
            'Never change `DRY_RUN` from `true` to `false`',
            're-run validation after either authorized edit',
            'Neither `place_equity_order` nor `cancel_equity_order` '
            'may be preapproved',
            'In Codex, keep both set to `Needs approval`',
            'control may be labeled `Auto`',
            '`Allowed permissions`',
            'stop before running the routine',
            'Dry run prevents new entries, but it may still sell or '
            'protect existing positions',
            'Do not create or enable a schedule',
            'If this exact repository checkout is already open',
            'Otherwise open a safe writable parent folder',
            'clone into a new subfolder without overwriting anything',
            'stop before lifecycle or broker work',
            'open that cloned repository in a brand-new native '
            'project session',
            'Do not continue from the parent-folder session',
            'sidebar Local filter is insufficient',
            'powershell.exe -NoProfile -NonInteractive '
            '-ExecutionPolicy Bypass -File ./resolve_python.ps1',
            'bind the exact returned absolute Python 3 path as '
            '`PYTHON_EXE`',
            'reuse that exact executable with current-shell literal '
            'quoting',
            'Never substitute a bare `python`, `python3`, `py`, or a '
            'generic “Windows equivalent.”',
            '`powershell.exe` is unavailable',
            'close the wrong context',
            'sidebar Local filter alone is insufficient',
            'genuinely native Linux/macOS checkout',
            'does not validate the Claude Windows Desktop scheduler '
            'path',
            'zero agentic-enabled accounts, stop',
            'if several exist, show only their display names',
            'must then resolve to exactly one account',
            'must be agentic enabled',
            'no default, partial-match, first-account, or '
            'account-number fallback',
            'same bound `PYTHON_EXE`',
            '`\'<PYTHON_EXE>\' -m unittest discover -s tests`',
            'Do not use a bare launcher or invent an ad-hoc '
            'serializer, path, or extra broker call',
            'first supervised entry-eligible run with '
            '`DRY_RUN = true` remains the end-to-end proof of '
            'broker-response staging',
            'Creating a missing saved scan is a broker-side setup '
            'mutation',
            'obtain my explicit confirmation before creating it',
            'ask for explicit confirmation before running anything',
            'no schedule was created',
        ):
            self.assertIn(required, setup_prompt)

        resolver = setup_prompt.index(
            'powershell.exe -NoProfile -NonInteractive '
            '-ExecutionPolicy Bypass -File ./resolve_python.ps1'
        )
        validate = setup_prompt.index(
            'Execute `validate_constants.py --json`', resolver
        )
        full_suite = setup_prompt.index(
            '`\'<PYTHON_EXE>\' -m unittest discover -s tests`',
            validate,
        )
        self.assertLess(resolver, validate)
        self.assertLess(validate, full_suite)
        self.assertNotIn('its included Python', quickstart)
        self.assertNotIn(
            '`python3 -m unittest discover -s tests`', setup_prompt
        )
        self.assertNotIn(
            "or the environment's Windows equivalent", setup_prompt
        )
        self.assertIn(
            'Only after the safe first test succeeds and you separately '
            'consent to scheduling',
            quickstart,
        )
        self.assertIn('(CLAUDE-LOCAL-SCHEDULING.md)', quickstart)

        root_real = os.path.realpath(ROOT)
        local_targets = set()
        # This pattern intentionally covers ordinary links and image links.
        for raw_target in re.findall(
            r'\[[^\]]*\]\(([^)]+)\)', quickstart
        ):
            target = raw_target.strip().strip('<>')
            if target.startswith('#') or re.match(
                r'^(?:https?|mailto):', target, re.IGNORECASE
            ):
                continue
            path_target = target.split('#', 1)[0]
            self.assertTrue(path_target, raw_target)
            self.assertFalse(os.path.isabs(path_target), raw_target)
            resolved = os.path.realpath(os.path.join(ROOT, path_target))
            self.assertEqual(
                os.path.normcase(os.path.commonpath((root_real, resolved))),
                os.path.normcase(root_real),
                f'QUICKSTART link escapes the repository: {raw_target}',
            )
            self.assertTrue(os.path.isfile(resolved), resolved)
            self.assertGreater(os.path.getsize(resolved), 0, resolved)
            local_targets.add(path_target.replace('\\', '/'))
        self.assertIn('README.md', local_targets)
        self.assertIn('CLAUDE-LOCAL-SCHEDULING.md', local_targets)

        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        pre_live = readme.split(
            '## Testing before going live', 1
        )[1].split('## Architecture', 1)[0]
        for required in (
            'both `place_equity_order` and `cancel_equity_order`',
            '**"Needs approval"** (or the platform\'s equivalent)',
            'Neither mutation tool may be preapproved',
            'control may be labeled **Auto**',
            'inspect **Allowed permissions**',
            'both tools to remain approval-gated',
            'stop before running the routine or enabling live trading',
        ):
            self.assertIn(required, pre_live)

    def test_claude_schedule_migration_requires_a_new_code_local_routine(self):
        documents = (
            'robinhood-momentum-routine-autonomous.md',
            'README.md',
            'CLAUDE-LOCAL-SCHEDULING.md',
        )

        for filename in documents:
            with self.subTest(document=filename):
                with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                    text = re.sub(r'\s+', ' ', f.read().lower())

                # Anchor these checks to the migration guidance. Generic
                # mentions elsewhere in these long documents must not pass it.
                sidebar = text.rfind('sidebar')
                self.assertNotEqual(
                    sidebar, -1,
                    f'{filename} must explain the Claude sidebar Local trap',
                )
                guide = text[max(0, sidebar - 1500):sidebar + 6000]

                legacy_cowork = (
                    re.search(
                        r'(?:legacy|old|existing).{0,160}'
                        r'(?:cowork|local-agent).{0,160}'
                        r'(?:routine|scheduled task|schedule)', guide,
                    )
                    or re.search(
                        r'(?:cowork|local-agent).{0,160}'
                        r'(?:routine|scheduled task|schedule).{0,160}'
                        r'(?:legacy|old|existing)', guide,
                    )
                )
                self.assertIsNotNone(
                    legacy_cowork,
                    f'{filename} must identify the old Cowork schedule as legacy',
                )

                sidebar_trap = text[max(0, sidebar - 100):sidebar + 500]
                self.assertIn('local', sidebar_trap)
                self.assertTrue(
                    any(term in sidebar_trap for term in (
                        'does not', 'doesn\'t', 'cannot', 'will not', 'do not',
                        'do not assume',
                    )),
                    f'{filename} must negate migration by sidebar selection',
                )
                self.assertTrue(
                    any(term in sidebar_trap for term in (
                        'migrate', 'convert', 'move', 'recreate', 'change',
                    )),
                    f'{filename} must explain what sidebar Local cannot do',
                )
                self.assertTrue(
                    any(term in sidebar_trap for term in (
                        'existing', 'old', 'legacy',
                    )),
                    f'{filename} must identify the task that is not migrated',
                )
                self.assertRegex(
                    sidebar_trap,
                    r'(?:local.{0,100}selector.{0,100}new session|'
                    r'new[- ]session.{0,100}(?:local|selector)|'
                    r'(?:choosing|selecting).{0,80}local.{0,80}'
                    r'(?:new chat|new session))',
                    f'{filename} must cover the Local new-session selector',
                )

                self.assertRegex(
                    guide,
                    r'(?:new|newly created|replacement).{0,240}'
                    r'(?:routine|scheduled task)',
                )
                self.assertRegex(
                    guide,
                    r'(?:(?:new|replacement).{0,120}'
                    r'(?:uniquely named|distinct name|new name).{0,120}'
                    r'(?:task|routine)|(?:task|routine).{0,120}'
                    r'(?:uniquely named|distinct name|new name))',
                )
                self.assertIn('code', guide)
                self.assertRegex(
                    guide,
                    r'code.{0,100}routines?.{0,100}new routine.{0,100}local',
                )
                self.assertTrue(
                    any(path in guide for path in (
                        r'd:\projects\robinhoodequitytradingagent',
                        'd:/projects/robinhoodequitytradingagent',
                    ))
                    or re.search(
                        r'(?:this )?exact.{0,100}native windows.{0,100}'
                        r'(?:checkout|project folder|repository)', guide,
                    ),
                    f'{filename} must require the exact native repository',
                )
                self.assertRegex(
                    guide,
                    r'(?:(?:worktree|worktree isolation).{0,80}'
                    r'(?:off|disabled)|(?:off|disable).{0,80}worktree)',
                )
                self.assertIn('run now', guide)
                self.assertRegex(guide, r'dry_run\s*=\s*`?true')
                self.assertIn('powershell', guide)
                self.assertIn('resolver', guide)
                self.assertIn('get_accounts', guide)
                self.assertRegex(guide, r'(?:proof|prove|require.{0,80}success)')

                pause = re.search(
                    r'(?:pause|disable).{0,160}(?:old|legacy|existing)|'
                    r'(?:old|legacy|existing).{0,160}(?:pause|disable)',
                    guide,
                )
                self.assertIsNotNone(pause)
                run_now_after_pause = guide.find('run now', pause.start())
                self.assertNotEqual(
                    run_now_after_pause,
                    -1,
                    f'{filename} must test the replacement after pausing '
                    'the old task',
                )
                self.assertLess(
                    pause.start(), run_now_after_pause,
                    f'{filename} must pause the old task before testing',
                )

                delete_only_after_success = (
                    re.search(
                        r'(?:delet(?:e|ing)|remove).{0,120}'
                        r'(?:old|legacy|existing)'
                        r'.{0,240}(?:only after|after).{0,160}'
                        r'(?:success|succeeds|passes)', guide,
                    )
                    or re.search(
                        r'(?:only after|after).{0,160}'
                        r'(?:success|succeeds|passes|proof).{0,240}'
                        r'(?:delet(?:e|ing)|remove).{0,120}'
                        r'(?:old|legacy|existing)',
                        guide,
                    )
                    or re.search(
                        r'(?:do not|never).{0,100}'
                        r'(?:delet(?:e|ing)|remove)'
                        r'.{0,160}(?:old|legacy|existing).{0,240}'
                        r'(?:until|unless).{0,160}'
                        r'(?:success|succeeds|passes)', guide,
                    )
                    or re.search(
                        r'(?:success|successful|proof|require).{0,300}'
                        r'before.{0,100}(?:delet(?:e|ing)|remove)', guide,
                    )
                )
                self.assertIsNotNone(
                    delete_only_after_success,
                    f'{filename} must retain the old task until the new '
                    'routine succeeds',
                )

                enable_only_after_success = (
                    re.search(
                        r'(?:success|successful|proof|require).{0,320}'
                        r'before.{0,120}'
                        r'(?:enabl(?:e|ing)|activat(?:e|ing))', guide,
                    )
                    or re.search(
                        r'before.{0,120}'
                        r'(?:enabl(?:e|ing)|activat(?:e|ing)).{0,320}'
                        r'(?:success|successful|proof|require)', guide,
                    )
                    or re.search(
                        r'before.{0,120}(?:activating|enabling).{0,320}'
                        r'(?:success|successful|proof|require)', guide,
                    )
                    or re.search(
                        r'(?:enabl(?:e|ed|ing)|activat(?:e|ed|ing)).{0,160}'
                        r'only after.{0,160}'
                        r'(?:success|succeed|successful|proof)', guide,
                    )
                    or re.search(
                        r'(?:only after|after).{0,160}'
                        r'(?:success|succeed|successful|proof).{0,160}'
                        r'(?:enabl(?:e|ed|ing)|activat(?:e|ed|ing))', guide,
                    )
                )
                self.assertIsNotNone(
                    enable_only_after_success,
                    f'{filename} must not enable the replacement schedule '
                    'until the supervised proof succeeds',
                )

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
        valid_place_response = journal.split(
            "- **Valid place response:**", 1
        )[1].split("- **Connector/request rejection", 1)[0]
        self.assertIn("fresh unique direct-child JSON file", valid_place_response)
        self.assertIn("invocation-bound `SOURCE_ROOT`", valid_place_response)
        self.assertIn("--transport-scratch <absolute scratch>", valid_place_response)
        self.assertIn("bound response save", valid_place_response)
        self.assertIn("transport validation", valid_place_response)
        self.assertIn("strict parse", valid_place_response)
        self.assertIn("semantic acknowledgement", valid_place_response)
        self.assertIn("`malformed_response`", valid_place_response)
        self.assertIn("`acknowledgement_failure`", valid_place_response)
        self.assertIn("ORDER-STATE HALT", valid_place_response)
        self.assertIn("never retry the save", valid_place_response)
        self.assertIn("switch path or writer", valid_place_response)
        self.assertIn("second placement", valid_place_response)
        retryable_transient = journal.split(
            "- **Transient timeout/server/connector failure", 1
        )[1].split("- **Second transient failure:**", 1)[0]
        self.assertNotIn("acknowledgement_failure", retryable_transient)
        self.assertIn(
            "acknowledgement-rejected response belongs to the "
            "no-placement-retry HALT above",
            retryable_transient,
        )
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
        self.assertIn(
            'Never use a harness-advertised path, search for a result file, '
            'extract `structuredContent`',
            phase,
        )
        self.assertIn("call `get_equity_historicals` again", phase)
        self.assertIn(
            'Persist historicals and derived handoffs only through the '
            'startup-bound file-change facility and `SOURCE_ROOT`',
            phase,
        )
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
