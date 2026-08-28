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
import uuid
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest import mock
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
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
CONNECTOR_CONTRACT = os.path.join(ROOT, 'connector_contract.py')
RUN_LIFECYCLE = os.path.join(ROOT, 'run_lifecycle.py')
RESOLVE_PYTHON = os.path.join(ROOT, 'resolve_python.ps1')

_BOUND_SOURCE_SCRATCHES = {}

sys.path.insert(0, ROOT)
from evaluate_candidates import (
    load_quote_documents,
    load_quotes,
    load_rsi_map,
    spread_gate,
)
import validate_constants as constants_validator
import broker_snapshot as broker_snapshot_module
import daily_loss as daily_loss_module
import filter_scan as filter_scan_module
import market_clock as market_clock_module
import order_intents as order_intents_module
import run_lock as run_lock_module
from broker_snapshot import (
    SourceHandoffError,
    SnapshotError,
    validate_bound_external_json_purpose,
    validate_bound_external_json_purposes,
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


def run_imported_main(main_function, argv, **injected):
    """Exercise an imported CLI dispatcher with test-only injected state.

    Production subprocesses must never receive clock-override flags.  Helpers
    that deliberately expose an imported-only clock hook are captured here
    with the same stdout/stderr/return-code shape used by CLI assertions.
    """
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            returncode = main_function(list(argv), **injected)
        except SystemExit as exc:
            returncode = exc.code
    if returncode is None:
        returncode = 0
    return subprocess.CompletedProcess(
        list(argv), int(returncode), stdout.getvalue(), stderr.getvalue()
    )


def _bound_source_scratch(directory):
    return _BOUND_SOURCE_SCRATCHES.get(os.path.normcase(os.path.realpath(directory)))


def write_test_source(directory, name, *, value=None, raw=None):
    """Write a normal fixture, journaling it when ``directory`` is bound.

    The production validators accept only committed source handoffs.  Keeping
    this conversion in one test helper lets legacy path-mode coverage remain
    meaningful without giving tests a bypass unavailable to the real runner.
    """

    scratch = _bound_source_scratch(directory)
    if scratch is None:
        path = os.path.join(directory, name)
    else:
        purpose = f"test-{uuid.uuid4().hex}"
        receipt = json.loads(run_cli(BROKER_SNAPSHOT, [
            "reserve-source", "--scratch", scratch, "--purpose", purpose,
        ]))
        path = receipt["source"]
    if raw is None:
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
    else:
        mode = "wb" if isinstance(raw, bytes) else "w"
        kwargs = {} if mode == "wb" else {"encoding": "utf-8", "newline": "\n"}
        with open(path, mode, **kwargs) as handle:
            handle.write(raw)
    if scratch is not None:
        run_cli(BROKER_SNAPSHOT, [
            "commit-source", "--scratch", scratch, "--purpose", purpose,
        ])
    return path


def commit_test_source_purpose(scratch, purpose, value):
    """Reserve, write, and commit one named response-source handoff."""

    reserved = json.loads(run_cli(BROKER_SNAPSHOT, [
        "reserve-source", "--scratch", scratch, "--purpose", purpose,
    ]))
    with open(
        reserved["source"], "w", encoding="utf-8", newline="\n"
    ) as handle:
        json.dump(value, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")
    committed = json.loads(run_cli(BROKER_SNAPSHOT, [
        "commit-source", "--scratch", scratch, "--purpose", purpose,
    ]))
    return reserved, committed


def prepare_source_root_for_test(scratch, scratch_id=None):
    """Install the helper-owned source-root marker used by legacy test fixtures."""
    scratch_path = Path(os.path.realpath(scratch))
    if scratch_id is None:
        with open(
            scratch_path / '.rhmra-broker-snapshot-scratch.json',
            encoding='utf-8',
        ) as handle:
            scratch_id = json.load(handle)['scratch_id']
    temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
    source_root = Path(tempfile.mkdtemp(
        prefix='rhmra-source-', dir=str(temp_root)
    )).resolve(strict=True)
    source_stat = os.lstat(source_root)
    source_root_id = str(uuid.uuid4())
    document = broker_snapshot_module._transport_preparation_marker_document(
        scratch_id=scratch_id,
        source_root=str(source_root),
        source_root_id=source_root_id,
        source_root_identity=(source_stat.st_dev, source_stat.st_ino),
    )
    marker = scratch_path / (
        '.rhmra-broker-response-source-root-prepared.json'
    )
    with open(marker, 'xb') as handle:
        handle.write(broker_snapshot_module._canonical_bytes(document))
    return str(source_root), source_root_id


@contextmanager
def bound_source_root(scratch):
    """Preflight scratch, bind one accounts canary, then yield its source root."""
    run_cli(BROKER_SNAPSHOT, ["preflight", "--scratch", scratch])
    source_root, _source_root_id = prepare_source_root_for_test(scratch)
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
        source_key = os.path.normcase(os.path.realpath(source_root))
        _BOUND_SOURCE_SCRATCHES[source_key] = os.path.realpath(scratch)
        yield source_root
    finally:
        _BOUND_SOURCE_SCRATCHES.pop(
            os.path.normcase(os.path.realpath(source_root)), None
        )
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
            ("HIGH_LOOKBACK_DAYS", "1001", "must be <= 1000"),
            ("VOLUME_LOOKBACK_DAYS", "1001", "must be <= 1000"),
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
        order_type="market",
        trigger=None,
        stop_price=None,
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
        document = {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "created_at": created_at,
            "state": state,
            "cumulative_quantity": str(cumulative_quantity),
            "fees": "999.99",  # cumulative order fee must never be added to execution fees
            "executions": executions,
        }
        if trigger is not None:
            document["trigger"] = trigger
        if stop_price is not None:
            document["stop_price"] = str(stop_price)
        return document

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
               total_value="1000", halt_pct="5", stop_date_pt=None,
               expected_success=True):
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
                "--stop-date-pt", stop_date_pt or self.TRADING_DATE,
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

    def test_failure_json_keeps_binding_input_and_output_failures_out_of_retry(self):
        with tempfile.TemporaryDirectory() as td:
            positions_path = os.path.join(td, "positions.json")
            orders_path = os.path.join(td, "orders.json")
            portfolio_path = os.path.join(td, "portfolio.json")
            for path, document in (
                (positions_path, self.page("positions", [])),
                (orders_path, self.page("orders", [])),
                (portfolio_path, {"data": {"total_value": "1000"}}),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)

            common = [
                "--portfolio", portfolio_path,
                "--orders", orders_path,
                "--trading-date", self.TRADING_DATE,
                "--stop-date-pt", self.TRADING_DATE,
                "--as-of-utc", self.AS_OF_UTC,
                "--halt-pct", "5",
                "--failure-json",
            ]
            cases = (
                (
                    "binding",
                    ["--positions", positions_path, "--json-out", positions_path],
                    "daily_loss_binding_invalid",
                ),
                (
                    "input",
                    [
                        "--positions", os.path.join(td, "missing.json"),
                        "--json-out", os.path.join(td, "input-out.json"),
                    ],
                    "daily_loss_input_failed",
                ),
                (
                    "output",
                    [
                        "--positions", positions_path,
                        "--json-out", os.path.join(td, "missing", "out.json"),
                    ],
                    "daily_loss_output_failed",
                ),
            )
            for label, specific, expected_code in cases:
                with self.subTest(phase=label):
                    proc = subprocess.run(
                        [sys.executable, DAILY_LOSS, *common, *specific],
                        cwd=ROOT,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertEqual(proc.stderr, "")
                    self.assertEqual(proc.stdout.count("\n"), 1)
                    receipt = json.loads(proc.stdout)
                    self.assertEqual(
                        set(receipt),
                        {
                            "schema_version", "action", "ok", "mode",
                            "generation", "error",
                        },
                    )
                    self.assertEqual(receipt["schema_version"], 1)
                    self.assertEqual(receipt["action"], "daily-loss")
                    self.assertIs(receipt["ok"], False)
                    self.assertEqual(receipt["mode"], "calculation")
                    self.assertIsNone(receipt["generation"])
                    self.assertEqual(
                        set(receipt["error"]), {"code", "message"}
                    )
                    self.assertEqual(receipt["error"]["code"], expected_code)
                    self.assertNotIn("recovery_action", receipt)

    def test_failure_json_classifies_helper_invariant_as_internal(self):
        with tempfile.TemporaryDirectory() as td:
            positions_path = os.path.join(td, "positions.json")
            orders_path = os.path.join(td, "orders.json")
            portfolio_path = os.path.join(td, "portfolio.json")
            output_path = os.path.join(td, "daily-loss.json")
            for path, document in (
                (positions_path, self.page("positions", [])),
                (orders_path, self.page("orders", [])),
                (portfolio_path, {"data": {"total_value": "1000"}}),
            ):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(document, handle)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with mock.patch.object(
                daily_loss_module,
                "calculate_daily_loss",
                side_effect=daily_loss_module.DailyLossInternalError(
                    "internal test invariant"
                ),
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                status = daily_loss_module.main(
                    [
                        "--portfolio", portfolio_path,
                        "--positions", positions_path,
                        "--orders", orders_path,
                        "--trading-date", self.TRADING_DATE,
                        "--stop-date-pt", self.TRADING_DATE,
                        "--as-of-utc", self.AS_OF_UTC,
                        "--halt-pct", "5",
                        "--json-out", output_path,
                        "--failure-json",
                    ]
                )
            self.assertEqual(status, 2)
            self.assertEqual(stderr.getvalue(), "")
            receipt = json.loads(stdout.getvalue())
            self.assertEqual(
                receipt["error"]["code"], "daily_loss_internal_failed"
            )
            self.assertNotIn("recovery_action", receipt)
            self.assertFalse(os.path.exists(output_path))

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
            stdout = run_cli(
                DAILY_LOSS,
                [
                    "--positions", positions_path,
                    "--orders", orders_path,
                    "--trading-date", self.TRADING_DATE,
                    "--stop-date-pt", self.TRADING_DATE,
                    "--as-of-utc", self.AS_OF_UTC,
                    "--symbols-out", symbols_path,
                ],
            )
            self.assertEqual(stdout.count("\n"), 1)
            receipt = json.loads(stdout)
            self.assertEqual(
                receipt,
                {
                    "schema_version": 1,
                    "action": "discover-symbols",
                    "ok": True,
                    "trading_date_et": self.TRADING_DATE,
                    "as_of_utc": self.AS_OF_UTC,
                    "symbol_count": 2,
                    "symbols": ["CLOSED", "HELD"],
                },
            )
            with open(symbols_path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), ["CLOSED", "HELD"])

    def test_discovery_mode_rejects_unused_calculation_inputs(self):
        with tempfile.TemporaryDirectory() as td:
            positions_path = os.path.join(td, "positions.json")
            orders_path = os.path.join(td, "orders.json")
            portfolio_path = os.path.join(td, "portfolio.json")
            quotes_path = os.path.join(td, "quotes.json")
            with open(positions_path, "w", encoding="utf-8") as f:
                json.dump(self.page("positions", []), f)
            with open(orders_path, "w", encoding="utf-8") as f:
                json.dump(self.page("orders", []), f)
            with open(portfolio_path, "w", encoding="utf-8") as f:
                json.dump({"data": {"total_value": "1000"}}, f)
            with open(quotes_path, "w", encoding="utf-8") as f:
                json.dump({"data": {"results": []}}, f)

            cases = (
                ("--portfolio", portfolio_path),
                ("--quotes", quotes_path),
                ("--halt-pct", "5"),
            )
            for index, extra in enumerate(cases):
                output_path = os.path.join(td, f"symbols-{index}.json")
                proc = subprocess.run(
                    [
                        sys.executable,
                        DAILY_LOSS,
                        "--positions", positions_path,
                        "--orders", orders_path,
                        "--trading-date", self.TRADING_DATE,
                        "--stop-date-pt", self.TRADING_DATE,
                        "--as-of-utc", self.AS_OF_UTC,
                        "--symbols-out", output_path,
                        *extra,
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                with self.subTest(option=extra[0]):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn(
                        "discovery mode does not accept unused calculation "
                        "option(s)",
                        proc.stderr,
                    )
                    self.assertIn(extra[0], proc.stderr)
                    self.assertFalse(os.path.exists(output_path))

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

    def test_stop_fill_summary_uses_distinct_orders_and_pacific_execution_day(self):
        normalized_stop = self.order(
            "normalized-stop",
            "ALPHA",
            "sell",
            [
                self.execution(
                    "normalized-fill-1",
                    "9",
                    "1",
                    timestamp="2026-07-31T07:00:00.000000001Z",
                ),
                self.execution(
                    "normalized-fill-2",
                    "9",
                    "1",
                    timestamp="2026-07-31T15:00:00.708917Z",
                ),
            ],
            order_type="stop_market",
            stop_price="9",
        )
        legacy_stop = self.order(
            "legacy-stop",
            "BETA",
            "sell",
            [self.execution("legacy-fill", "9", "1")],
            state="cancelled",
            order_type="market",
            trigger="stop",
            stop_price="9",
        )
        prior_pacific_stop = self.order(
            "prior-pacific-stop",
            "OLD",
            "sell",
            [
                self.execution(
                    "prior-pacific-fill",
                    "9",
                    "1",
                    timestamp="2026-07-31T06:59:59.999999999Z",
                )
            ],
            order_type="stop_limit",
            stop_price="9",
        )
        ordinary_sell = self.order(
            "ordinary-sell",
            "PROFIT",
            "sell",
            [self.execution("ordinary-fill", "9", "1")],
            order_type="limit",
            trigger="immediate",
            stop_price="0",
        )
        matching_buys = [
            self.order(
                "buy-alpha",
                "ALPHA",
                "buy",
                [self.execution("buy-alpha-fill", "9", "2")],
            ),
            self.order(
                "buy-beta",
                "BETA",
                "buy",
                [self.execution("buy-beta-fill", "9", "1")],
            ),
            self.order(
                "buy-old",
                "OLD",
                "buy",
                [self.execution("buy-old-fill", "9", "1")],
            ),
            self.order(
                "buy-profit",
                "PROFIT",
                "buy",
                [self.execution("buy-profit-fill", "9", "1")],
            ),
        ]
        result = self.invoke(
            orders=[self.page(
                "orders",
                [
                    normalized_stop,
                    normalized_stop,
                    legacy_stop,
                    prior_pacific_stop,
                    ordinary_sell,
                    *matching_buys,
                ],
            )],
        )
        self.assertEqual(result["stop_count_date_pt"], self.TRADING_DATE)
        self.assertEqual(result["stop_fills_today"], 2)
        self.assertEqual(result["stopped_out_symbols"], ["ALPHA", "BETA"])

    def test_relevant_sell_with_indeterminate_stop_identity_fails_closed(self):
        matching_buy = self.order(
            "matching-buy",
            "BAD",
            "buy",
            [self.execution("matching-buy-fill", "9", "1")],
        )
        missing_type = self.order(
            "missing-type",
            "BAD",
            "sell",
            [self.execution("missing-type-fill", "9", "1")],
        )
        missing_type.pop("type")
        positive_price_without_marker = self.order(
            "unmarked-stop-price",
            "BAD",
            "sell",
            [self.execution("unmarked-fill", "9", "1")],
            stop_price="8",
        )
        contradictory = self.order(
            "contradictory-stop",
            "BAD",
            "sell",
            [self.execution("contradictory-fill", "9", "1")],
            order_type="stop_market",
            trigger="immediate",
            stop_price="8",
        )
        cases = (
            (missing_type, "missing or malformed"),
            (positive_price_without_marker, "no recognized stop marker"),
            (contradictory, "type and trigger are contradictory"),
        )
        for sell, reason in cases:
            with self.subTest(order=sell["id"]):
                proc = self.invoke(
                    orders=[self.page("orders", [matching_buy, sell])],
                    expected_success=False,
                )
                self.assertIn("stop classification is indeterminate", proc.stderr)
                self.assertIn(reason, proc.stderr)

    def test_stop_date_must_match_final_as_of_pacific_date(self):
        proc = self.invoke(
            stop_date_pt="2026-07-30",
            expected_success=False,
        )
        self.assertIn(
            "as-of UTC does not fall on --stop-date-pt in US Pacific time",
            proc.stderr,
        )

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
        drain_contracts = re.findall(
            r"const drainCommand = async result => \{(.*?)\n\};",
            routine,
            re.DOTALL,
        )
        self.assertEqual(len(drain_contracts), 6)
        for drain_contract in drain_contracts:
            self.assertIn('let output = String(current.output ?? "");', drain_contract)
            self.assertIn('output += String(next.output ?? "");', drain_contract)
            self.assertIn(
                "return Object.freeze({...current, output});",
                drain_contract,
            )
        lifecycle = routine.split(
            "### INVOCATION LIFECYCLE", 1
        )[1].split("**Mandatory configuration preflight", 1)[0]
        self.assertLess(
            routine.index("run_lifecycle.py start"),
            routine.index("validate_constants.py --json"),
        )
        self.assertIn(
            "terminalize the invocation exactly once", lifecycle.lower()
        )
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
        self.assertNotIn("const runHelper", block)
        self.assertNotIn("const runJson", block)
        self.assertNotIn(".r.exit_code", block)
        self.assertIn("SOLE authority is therefore `daily_loss.py`", block)
        self.assertIn(
            "DEFINITION ONLY — THIS BLOCK IMPLEMENTS SECOND AND MUST NOT "
            "EXECUTE HERE",
            block,
        )
        self.assertIn(
            "recording the `position-management` lifecycle event, does not "
            "authorize",
            block,
        )
        self.assertEqual(block.count("--stop-date-pt <START date_pt>"), 2)
        self.assertIn("stop_count_date_pt", block)
        self.assertIn("stop_fills_today", block)
        self.assertIn("stopped_out_symbols", block)
        self.assertIn(
            "successful FINAL lifecycle-bound `daily_loss.py` wrapper is "
            "the SOLE authority",
            block,
        )
        self.assertIn("Never issue a separate `get_equity_orders` call", block)
        self.assertIn("count historical order rows", block)
        connector_failures = routine.split("### CONNECTOR FAILURES", 1)[1].split(
            "### ORDER HANDLING", 1
        )[0]
        self.assertIn(
            "FIRST's initial Step 1 `get_equity_positions` census only",
            connector_failures,
        )
        self.assertIn(
            "Every later positions failure—DAILY-LOSS, pre-buy revalidation, "
            "order-intent reconciliation, or FINAL STATUS REFRESH",
            connector_failures,
        )
        self.assertIn("MUST NEVER reuse `first-portfolio`", connector_failures)
        timestamp_contract = routine.split("### BROKER TIMESTAMPS", 1)[1].split(
            "### BROKER ORDER OBJECTS", 1
        )[0]
        self.assertNotIn('stop-count "filled today"', timestamp_contract)
        self.assertIn("stop-count guard is explicitly excluded", timestamp_contract)
        returned_order_contract = routine.split(
            "### BROKER ORDER OBJECTS", 1
        )[1].split("### TRADE LEDGER", 1)[0]
        self.assertIn(
            "stop-count guard is not a runner-side filter", returned_order_contract
        )
        self.assertIn("use NO `created_at_gte`, `state`, `symbol`, or `placed_agent` filter", block)
        self.assertIn(
            "Follow the helper-returned `next_cursor` until it is null",
            block,
        )
        page_contract = block.split(
            "**DETERMINISTIC POSITIONS/ORDERS PAGE CONTRACT", 1
        )[1].split("For positions and orders, stage each returned page", 1)[0]
        for required in (
            "`connector_contract.py page` with `--scratch '<scratch>'`",
            "sourceReservation.purpose",
            "--kind <positions|orders>",
            "repeated `--request-cursor <FIRST|exact prior next_cursor>`",
            "repeated, or overlong submitted request-cursor chain",
            "continuation at the exact 1,000-page ceiling",
            "Missing, null, and empty raw `data.next` are all valid terminal pages",
            "`next_cursor: null`, `complete: true`",
            "Its `complete` is the SOLE authority",
            "its `next_cursor` is the SOLE authority",
            "Never inspect or re-parse `fullToolResult`",
            "never define `getNext`, `sourceData`",
            "semantic generation failure",
            "`request_binding_invalid`",
            "`pagination_stopped`",
            "`source_file_missing`, `source_file_changed`, or",
            "generic `source_unavailable`",
            "`snapshot-write-failed` without B",
            "`coordination-halt` / `coordination-state`",
        ):
            self.assertIn(required, page_contract)
        action_matrix = block.split(
            "**CLOSED DAILY-LOSS CONNECTOR-CONTRACT ACTION MATRIX:**", 1
        )[1].split("`broker_snapshot.py` accepts exactly nine actions", 1)[0]
        for required in (
            "only with the literal",
            "`connector_contract.py page` action",
            "`first-positions-set` belongs only to FIRST",
            "deliberately has no `output_paths`",
            "`orders-set`",
            "`first-orders-set` do not exist",
            "Never synthesize an action name from `kind`",
            "`pageSet`, `positionSet`, `positionsSet`, `orderSet`, or `ordersSet`",
            "exact frozen `stagedOutputPaths` returned by",
            "`bindStageOutputPaths`",
            "outside `bindStageOutputPaths`",
            "without another\nbroker call and without consuming generation B",
        ):
            self.assertIn(required, action_matrix)
        self.assertNotIn("const getNext", block)
        self.assertNotIn("const sourceData", block)
        stage_binding = block.split(
            "const bindStageOutputPaths = (", 1
        )[1].split("const stagedOutputPaths", 1)[0]
        for required in (
            "expectedPageBinding",
            "expectedFileCount",
            'stageReceipt.output_mode !== "helper-allocated"',
            "descriptor.source_purpose !== expectedPageBinding.source_purpose",
            "descriptor.row_count !== expectedPageBinding.row_count",
            "descriptor.next_cursor !== expectedPageBinding.next_cursor",
            "descriptor.request_cursor !== expectedPageBinding.request_cursor",
            "descriptor.source_sha256",
            "descriptor.payload_sha256",
            "descriptor.provenance",
            "pageStageBinding",
            'code === "stage_semantic_invalid"',
            'code === "stage_input_failed"',
            '"stage_binding_invalid"',
            '"stage_response_invalid"',
            '"stage_retry_state_failed"',
            '"stage_internal_failed"',
            'code === "stage_write_failed"',
            '"source_file_missing"',
            '"source_file_changed"',
            '"source_file_invalid"',
            '["action", "error", "generation", "kind", "ok", "schema_version"]',
            'hasExactJsonKeys(receipt, expectedFailureKeys)',
            '["action", "error", "generation", "kind", "ok", "recovery_action", "schema_version"]',
            'hasExactJsonKeys(receipt.error, ["code", "message"])',
            'receipt.kind === expectedKind',
            'receipt.generation === expectedGeneration',
            'receipt.recovery_action === expectedSemanticRecovery',
            'recovery_action: receipt.recovery_action',
            'receipt.error.message.trim().length > 0',
            'trustedStageFailureCodes.includes(receipt.error.code)',
            '"generation-b"',
            '"snapshot-second-attempt-failed"',
            '"snapshot-write-failed"',
            '"coordination-state"',
        ):
            self.assertIn(required, stage_binding)
        daily_loss_failure_binding = block.split(
            "const bindDailyLossCommandFailure = (", 1
        )[1].split("The stage failure binder runs", 1)[0]
        for required in (
            'commandResult.process.exit_code === 2',
            'receipt.action === "daily-loss"',
            'receipt.mode === expectedMode',
            'receipt.generation === expectedGeneration',
            '"daily_loss_semantic_invalid"',
            '"daily_loss_binding_invalid"',
            '"daily_loss_input_failed"',
            '"daily_loss_output_failed"',
            '"daily_loss_internal_failed"',
            'receipt.error.code === "daily_loss_checkpoint_failed"',
            'expectedMode === "calculation"',
            'receipt.recovery_action === expectedRecovery',
            'failure: "daily-loss-command-unclassified"',
            'recovery_action: "coordination-state"',
            'recovery_action: "retry-identical-calculation"',
        ):
            self.assertIn(required, daily_loss_failure_binding)
        self.assertIn(
            "Exact `daily_loss_checkpoint_failed` is accepted only when "
            "the same closed envelope binds `mode: \"calculation\"`",
            block,
        )
        self.assertIn(
            "Its sole classification is `retry-identical-calculation`; it "
            "is not `coordination-state`",
            block,
        )
        self.assertIn(
            "initializes one local `checkpointRetryUsed` Boolean to false",
            block,
        )
        self.assertIn(
            "invoke the already-frozen `evaluationCommand` exactly once",
            block,
        )
        self.assertIn("never execute a third calculation", block)
        self.assertEqual(block.count("--failure-json"), 3)
        self.assertIn(
            "every allowed external A semantic outcome",
            block,
        )
        self.assertIn(
            "same binding operation must obtain and validate the exact "
            "`authorize-generation-b` receipt",
            block,
        )
        self.assertIn(
            "same composed operation that binds a successful B evaluation "
            "must persist `finish-generation-b "
            "--outcome completed` before continuing",
            block,
        )
        self.assertIn(
            "Never infer semantic failure from stderr, an exception message, "
            "exit code 2",
            block,
        )
        self.assertIn("execution timestamp rather than order creation time", block)
        self.assertIn("`intraday_quantity`", block)
        self.assertIn("`adjusted_previous_close`", block)
        self.assertIn("`cumulative_quantity`", block)
        self.assertIn("Null rows/elements are indeterminate", block)
        self.assertIn("DAILY-LOSS DISCOVERY", block)
        self.assertIn("DAILY-LOSS FINAL", block)
        discovery_step = block.split(
            "1. Re-fetch every page of `get_equity_positions`", 1
        )[1].split("2. Immediately after", 1)[0]
        self.assertNotIn("get_portfolio", discovery_step)
        self.assertIn("Do not fetch or stage a DISCOVERY portfolio", discovery_step)
        self.assertIn(
            "`daily_loss.py --symbols-out` rejects the calculation-only "
            "`--portfolio`, `--quotes`, and `--halt-pct` options",
            discovery_step,
        )
        self.assertIn(
            "proved terminal singleton directly or aggregate-seal only a "
            "multi-page set",
            discovery_step,
        )
        quote_discovery_step = block.split(
            "3. Ask the helper for the exact quote set", 1
        )[1].split("4. Immediately after", 1)[0]
        self.assertIn("use one proved terminal batch directly", quote_discovery_step)
        self.assertIn(
            "aggregate-seal only when there are multiple quote batches",
            quote_discovery_step,
        )
        self.assertIn(
            "A nonempty bound discovery `symbols` array is normal",
            quote_discovery_step,
        )
        self.assertIn(
            'not an "unexpected held symbols" condition',
            quote_discovery_step,
        )
        self.assertIn(
            "the bound quote paths in the final `daily_loss.py --quotes` arguments",
            quote_discovery_step,
        )
        final_step = block.split("5. Now re-fetch", 1)[1].split(
            "6. Run the authoritative evaluation", 1
        )[0]
        self.assertIn("`get_portfolio`", final_step)
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
        ordered = routine.split("### RUN THESE STEPS IN ORDER", 1)[1]
        first = ordered.split("**SECOND — circuit breaker check**", 1)[0]
        second = ordered.split("**SECOND — circuit breaker check**", 1)[1].split(
            "**THIRD —", 1
        )[0]
        self.assertIn("FIRST completion boundary", first)
        self.assertIn("bind `FIRST_COMPLETE`", first)
        self.assertIn("require `FIRST_COMPLETE`", second)
        self.assertIn(
            "execute the DAILY-LOSS CIRCUIT BREAKER block above", second
        )
        dust = first.split("**Dust sweep", 1)[1].split(
            "**Deterministic portfolio normalization", 1
        )[0]
        self.assertIn("never start SECOND/stop-count work", dust)
        self.assertIn("The field is literally `file_count`, never `count`", block)
        self.assertIn(
            "positive integer `file_count` equal to both the length of `files` "
            "and the length of `output_paths`",
            block,
        )
        self.assertIn(
            "tolerate helper-owned extra bookkeeping fields instead of "
            "counting object keys",
            block,
        )
        purpose_binding = block.split(
            "**SNAPSHOT LOCAL-COMMAND RESULT AND SOURCE RESERVATION", 1
        )[1].split("For every successful broker call", 1)[0]
        self.assertEqual(block.count("const runSnapshotJsonCommand = async command =>"), 1)
        self.assertIn("in Codex, every local helper command", purpose_binding)
        self.assertIn("A non-Codex runner uses its one native fully drained", purpose_binding)
        self.assertIn(
            "The exact JavaScript property names apply to Codex only",
            purpose_binding,
        )
        self.assertIn('let stdout = String(process.output ?? "");', purpose_binding)
        self.assertIn("while (process.session_id !== undefined)", purpose_binding)
        self.assertIn('stdout += String(next.output ?? "");', purpose_binding)
        self.assertIn(
            "const finalProcess = Object.freeze({...process, output: stdout});",
            purpose_binding,
        )
        self.assertIn("try { receipt = JSON.parse(stdout); }", purpose_binding)
        self.assertIn(
            "return Object.freeze({process: finalProcess, receipt});",
            purpose_binding,
        )
        self.assertIn(
            "const commandResult = await runSnapshotJsonCommand(command);",
            purpose_binding,
        )
        self.assertIn("const receipt = commandResult.receipt;", purpose_binding)
        self.assertIn("commandResult.process.exit_code !== 0", purpose_binding)
        command_contract_source = purpose_binding.split("```javascript", 1)[1].split(
            "```", 1
        )[0]
        self.assertEqual(command_contract_source.count("tools.exec_command"), 1)
        self.assertNotIn("runHelper", command_contract_source)
        self.assertNotIn("runJson", command_contract_source)
        self.assertNotIn(".r.exit_code", command_contract_source)
        self.assertIn(
            'const generationPurposeSlug = generation === "A" ? "a" : '
            'generation === "B" ? "b" : null;',
            purpose_binding,
        )
        for phase_kind in (
            "discovery:positions",
            "discovery:orders",
            "marks:quotes",
            "final:portfolio",
            "final:positions",
            "final:orders",
        ):
            self.assertIn(f'"{phase_kind}"', purpose_binding)
        self.assertIn(
            'return /^[a-z0-9][a-z0-9-]{0,47}$/.test(purpose)',
            purpose_binding,
        )
        self.assertIn(
            'receipt.purpose !== purpose', purpose_binding
        )
        self.assertIn(
            'purpose: receipt.purpose', purpose_binding
        )
        reserve_position = purpose_binding.index(
            "const sourceReservation = await reserveSnapshotSourceBeforeRead("
        )
        broker_position = purpose_binding.index(
            "const fullToolResult = await resolvedSnapshotRead(brokerArguments);"
        )
        self.assertLess(reserve_position, broker_position)
        self.assertIn(
            "ONLY NOW may this page's already-resolved read-only broker tool "
            "be invoked",
            purpose_binding,
        )
        self.assertIn(
            "Never define or use a `saveSource(purpose, fullToolResult)`-style "
            "helper",
            purpose_binding,
        )
        self.assertIn(
            "Never call a broker tool and then reserve its purpose",
            purpose_binding,
        )
        self.assertNotIn("daily-loss-A-", purpose_binding)
        self.assertNotIn("daily-loss-B-", purpose_binding)
        receipt_binding = block.split(
            "**STAGE RECEIPT PATH BINDING", 1
        )[1].split("**STAGING COMMAND MATRIX", 1)[0]
        for kind in ("portfolio", "positions", "orders", "quotes"):
            self.assertIn(f'"{kind}"', receipt_binding)
        self.assertIn(
            "stageReceipt.file_count !== stageReceipt.files.length",
            receipt_binding,
        )
        self.assertIn(
            "stageReceipt.file_count !== stageReceipt.output_paths.length",
            receipt_binding,
        )
        self.assertIn("descriptor.output !== outputPath", receipt_binding)
        self.assertIn("seenOutputPaths.has(outputPath)", receipt_binding)
        self.assertIn("seenOutputPaths.add(outputPath)", receipt_binding)
        self.assertNotIn("requestedOutputs", receipt_binding)
        self.assertIn(
            'stageReceipt.output_mode !== "helper-allocated"',
            receipt_binding,
        )
        self.assertIn(
            "const stagedOutputPaths = stageBinding.output_paths;",
            receipt_binding,
        )
        self.assertIn(
            "const stageCommandResult = await runSnapshotJsonCommand(stageCommand);",
            receipt_binding,
        )
        self.assertIn(
            "commandResult.process.exit_code !== 0",
            receipt_binding,
        )
        self.assertIn(
            "Use only `stagedOutputPaths` after that binding",
            receipt_binding,
        )
        self.assertIn(
            "Access to `stageReceipt.files` outside the exact validator is "
            "forbidden",
            receipt_binding,
        )
        self.assertIn("MUST NOT consume generation B", receipt_binding)
        validator_source = receipt_binding.split("```javascript", 1)[1].split(
            "```", 1
        )[0]
        self.assertNotIn("stageReceipt.files[0]", validator_source)
        self.assertNotIn("String(descriptor)", validator_source)
        self.assertNotIn("descriptor.toLowerCase", validator_source)
        self.assertNotIn(".r.exit_code", validator_source)
        self.assertIn(
            "The atomically written symbols file is an audit artifact only",
            quote_discovery_step,
        )
        self.assertIn(
            "a non-Codex runner uses the single-shape fully drained native equivalent",
            quote_discovery_step,
        )
        self.assertIn(
            "const symbolCommandResult = await "
            "runSnapshotJsonCommand(symbolCommand);",
            quote_discovery_step,
        )
        self.assertIn(
            'receipt.action !== "discover-symbols"',
            quote_discovery_step,
        )
        self.assertIn(
            "receipt.symbols.length !== receipt.symbol_count",
            quote_discovery_step,
        )
        self.assertIn(
            "const requiredQuoteSymbols = symbolBinding.symbols;",
            quote_discovery_step,
        )
        quote_contract_source = quote_discovery_step.split(
            "```javascript", 1
        )[1].split("```", 1)[0]
        self.assertNotIn("Get-Content", quote_contract_source)
        self.assertNotIn("runHelper", quote_contract_source)
        self.assertNotIn("runJson", quote_contract_source)
        self.assertNotIn(".r.exit_code", quote_contract_source)
        self.assertNotIn("Object.keys(receipt)", quote_contract_source)
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
        quotes_command = matrix_command("Quotes first-stage template")
        page_command = matrix_command("Positions/orders page template")
        aggregate_command = matrix_command("Positions/orders aggregate template")
        self.assertIn("--kind portfolio", portfolio_command)
        self.assertIn("--kind quotes", quotes_command)
        self.assertIn("--kind <positions|orders>", page_command)
        self.assertIn("--kind <positions|orders>", aggregate_command)
        for command in (
            portfolio_command,
            quotes_command,
            page_command,
            aggregate_command,
        ):
            self.assertIn("--auto-output-scratch '<scratch>'", command)
            self.assertNotIn("--output ", command)
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
        self.assertIn(
            'deterministically discards `--request-cursor` and '
            '`--allow-more` when `--kind` is `portfolio` or `quotes`',
            matrix,
        )
        self.assertIn(
            'Positions/orders retain strict cursor-chain validation', matrix
        )
        self.assertIn(
            "not a universal first-call, first-response, or first-file marker",
            matrix,
        )
        self.assertGreaterEqual(block.count("--snapshot-generation <A|B>"), 2)
        self.assertIn("shared set ID", block)
        self.assertIn("provenance sidecar", block)
        self.assertIn("aggregate-seal", block)
        self.assertIn("`complete: true` and `file_count: 1`", block)
        self.assertIn(
            "use that bound string directly and do not stage it again under a "
            "retry or aggregate filename",
            block,
        )
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
            "Durable `<scratch>/daily-loss-<a|b>.json` and stdout are the "
            "same wrapper object",
            block,
        )
        self.assertIn(
            "Store and consume the complete parsed wrapper unchanged",
            block,
        )
        self.assertIn(
            "Never read the durable evidence file with a file tool, shell "
            "command, `Get-Content`, `ReadAllText`, or a second Python "
            "command",
            block,
        )
        self.assertIn("Require schema 1, action `daily-loss`, `ok: true`", block)
        self.assertIn(
            "Pass the exact generation-specific output basename", block
        )
        self.assertIn("make no new buys", block)
        self.assertIn("NEVER feed its result into `daily_loss.py`", block)
        self.assertNotIn("compute trailing-day P&L", block)
        snapshot = routine.split("**Publish the STATUS SNAPSHOT", 1)[1].split(
            "The filename is exactly:", 1
        )[0]
        self.assertIn(
            "or null only when both identical-payload attempts failed or had "
            "invalid aggregates",
            snapshot,
        )
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
        self.assertIn(
            'CODEX LATER TOKEN PRECONDITION pasted exactly and unmodified',
            refresh,
        )
        self.assertIn(
            'never author a second invocation/token validator or UUID regular expression',
            refresh,
        )
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

        realized_contract = routine.split(
            '**Cost-basis realized P&L is telemetry only:**', 1
        )[1].split('### RUN THESE STEPS IN ORDER', 1)[0]
        self.assertIn('sole aggregate dollar figure is `data.total_returns`', realized_contract)
        self.assertIn('finite base-10 decimal string', realized_contract)
        self.assertIn('`data.total_returns: "0"` is a valid $0 result', realized_contract)
        self.assertIn("`number_of_trades` is zero", realized_contract)
        self.assertIn("`realized_gain` is null", realized_contract)
        self.assertIn('both attempts fail or are invalid', realized_contract)
        self.assertIn(
            'a structurally successful but invalid aggregate spends the same one retry',
            refresh,
        )
        self.assertIn('then only that telemetry is unavailable and null', refresh)

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
            '**Deterministic portfolio normalization', 1
        )[1].split('**PRE-SECOND ENTRY-FEASIBILITY GATES', 1)[0]
        self.assertIn('`data.buying_power.buying_power`', normalization)
        self.assertIn('older scalar `data.buying_power`', normalization)
        self.assertIn(
            'never substitutes `unleveraged_buying_power`',
            normalization,
        )
        self.assertIn('reports, and status account fields', normalization)


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

    def test_cli_accepts_purpose_inputs_and_rejects_mixed_selector_modes(self):
        bars = {
            "data": {
                "results": [{"symbol": "FISN", "bars": FISN_BARS}]
            }
        }
        quotes = {
            "data": {
                "results": [{
                    "quote": {
                        "symbol": "FISN",
                        "last_trade_price": "9.843",
                    }
                }]
            }
        }
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ):
            bars_reserved, _bars_committed = commit_test_source_purpose(
                scratch, "candidate-bars-0", bars
            )
            commit_test_source_purpose(
                scratch, "candidate-quotes-0", quotes
            )
            output = os.path.join(scratch, "purpose-evaluation.json")
            common = [
                "--scratch", scratch,
                "--quotes-purpose", "candidate-quotes-0",
                "--volume-lookback-days", "20",
                "--high-lookback-days", "5",
                "--min-median-dollar-volume", "175000",
                "--dip-entry-pct", "5",
            ]
            proc = self.evaluate_proc([
                *common,
                "--bars-purpose", "candidate-bars-0",
                "--json-out", output,
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)
            self.assertEqual(
                [row["symbol"] for row in document["results"]], ["FISN"]
            )

            mixed_output = os.path.join(scratch, "mixed-evaluation.json")
            proc = self.evaluate_proc([
                *common,
                "--bars", bars_reserved["source"],
                "--bars-purpose", "candidate-bars-0",
                "--json-out", mixed_output,
            ])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not allowed with argument", proc.stderr)
            self.assertFalse(os.path.exists(mixed_output))

    @staticmethod
    def write_json(directory, name, value):
        return write_test_source(directory, name, value=value)

    def run_eval(self, hist_payload, quotes, extra=None, return_document=False):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            hist = self.write_json(source_root, "hist.json", hist_payload)
            qts = self.write_json(source_root, "quotes.json", quotes)
            out = os.path.join(td, "out.json")
            cli_extra = list(extra or [])
            if "--rsi-file" in cli_extra:
                first = cli_extra.index("--rsi-file") + 1
                last = first
                while last < len(cli_extra) and not cli_extra[last].startswith("--"):
                    last += 1
                for index in range(first, last):
                    try:
                        with open(cli_extra[index], encoding="utf-8") as handle:
                            rsi_value = json.load(handle)
                    except json.JSONDecodeError:
                        with open(cli_extra[index], "rb") as handle:
                            write_test_source(
                                source_root,
                                f"rsi-{index - first}.json",
                                raw=handle.read(),
                            )
                        raise AssertionError(
                            "malformed RSI fixture unexpectedly committed"
                        )
                    bound_rsi = self.write_json(
                        source_root, f"rsi-{index - first}.json", rsi_value
                    )
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

    def test_quotes_accept_complete_saved_response_without_model_rekeying(self):
        bars = {"data": {"results": [{"symbol": "FISN", "bars": FISN_BARS}]}}
        result = {
            "quote": {
                "symbol": "FISN",
                "last_trade_price": "9.843",
                "bid_price": "9.83",
                "ask_price": "9.85",
            },
            "close": {"symbol": "FISN", "price": "9.70"},
        }
        shapes = (
            {
                "content": [{"type": "text", "text": "saved tool result"}],
                "structuredContent": {"data": {"results": [result]}},
                "isError": False,
            },
            {"data": {"results": [result]}},
            {"results": [result]},
        )
        for quote_response in shapes:
            with self.subTest(root_keys=tuple(quote_response)):
                fisn = self.run_eval(bars, quote_response)["FISN"]
                self.assertTrue(fisn["buy_candidate"])
                self.assertAlmostEqual(fisn["current_price"], 9.843)

    def test_quotes_reject_malformed_complete_responses_and_duplicates(self):
        valid = {
            "quote": {"symbol": "FISN", "last_trade_price": "9.843"},
            "close": {"symbol": "FISN", "price": "9.70"},
        }
        malformed = (
            {"isError": True, "structuredContent": {"data": {"results": [valid]}}},
            {"isError": "false", "structuredContent": {"data": {"results": [valid]}}},
            {"structuredContent": "not an object"},
            {"data": None},
            {"data": {}},
            {"data": {"results": {}}},
            {"data": {"results": ["not an object"]}},
            {"data": {"results": [{"quote": None}]}},
            {"data": {"results": [{"quote": {"last_trade_price": "9.843"}}]}},
            {"data": {"results": [{"quote": {"symbol": " BAD ", "last_trade_price": "9.843"}}]}},
            {"data": {"results": [valid, valid]}},
        )
        for document in malformed:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    load_quotes(document, "quotes.json")

    def test_quotes_merge_multiple_complete_batches_and_reject_cross_batch_duplicates(self):
        fisn = {
            "quote": {"symbol": "FISN", "last_trade_price": "9.843"},
            "close": {"symbol": "FISN", "price": "9.70"},
        }
        ttrx = {
            "quote": {"symbol": "TTRX", "last_trade_price": "7.84"},
            "close": {"symbol": "TTRX", "price": "7.70"},
        }
        documents = (
            ("quotes-1.json", {"data": {"results": [fisn]}}),
            ("quotes-2.json", {"data": {"results": [ttrx]}}),
        )
        merged = load_quote_documents(documents)
        self.assertEqual(set(merged), {"FISN", "TTRX"})
        with self.assertRaisesRegex(ValueError, "duplicate quote result across files"):
            load_quote_documents((documents[0], ("duplicate.json", documents[0][1])))

    def test_cli_accepts_multiple_bound_quote_response_files(self):
        bars = {
            "data": {
                "results": [
                    {"symbol": "FISN", "bars": FISN_BARS},
                    {"symbol": "TTRX", "bars": TTRX_BARS},
                ]
            }
        }
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ) as source_root:
            hist = self.write_json(source_root, "hist.json", bars)
            q1 = self.write_json(
                source_root,
                "quotes-1.json",
                {"data": {"results": [{"quote": {
                    "symbol": "FISN", "last_trade_price": "9.843"
                }}]}},
            )
            q2 = self.write_json(
                source_root,
                "quotes-2.json",
                {"data": {"results": [{"quote": {
                    "symbol": "TTRX", "last_trade_price": "7.84"
                }}]}},
            )
            rsi = self.write_json(
                source_root,
                "rsi.json",
                {
                    "FISN": [40, 36, 33, 30, 29, 34],
                    "TTRX": [40, 36, 33, 30, 29, 34],
                },
            )
            output = os.path.join(scratch, "multi-quote-output.json")
            run_cli(EVALUATE, [
                "--scratch", scratch, "--bars", hist, "--quotes", q1, q2,
                "--volume-lookback-days", "20", "--high-lookback-days", "5",
                "--min-median-dollar-volume", "175000", "--dip-entry-pct", "5",
                "--rsi-file", rsi, "--rsi-oversold", "35",
                "--rsi-lookback-bars", "5", "--rsi-confirm-bars", "1",
                "--rsi-max-entry", "60", "--rsi-period", "14",
                "--json-out", output,
            ])
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)
        self.assertIs(document["rsi_gate_enabled"], True)
        self.assertEqual(
            {row["symbol"] for row in document["results"]},
            {"FISN", "TTRX"},
        )
        fisn = next(row for row in document["results"] if row["symbol"] == "FISN")
        self.assertEqual(fisn["rsi_gate"], "pass")

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
        # A run saves the complete response object returned by the tool and
        # passes it directly; hand-building a symbol-keyed map is what broke
        # the 2026-07-28 11:06 run.
        bars = [bar("2026-07-01", 4.5, 5.0, 900000), bar("2026-07-02", 4.6, 4.9, 900000),
                bar("2026-07-03", 4.6, 4.8, 900000), bar("2026-07-06", 4.6, 4.9, 900000),
                bar("2026-07-07", 4.6, 4.9, 900000)]
        payload = {"results": [{"symbol": "SYNX", "bars": bars}, {"symbol": "OTHR", "bars": bars}]}
        with tempfile.TemporaryDirectory() as td:
            p1 = os.path.join(td, "rsi1.json")
            p2 = os.path.join(td, "rsi2.json")
            with open(p1, "w", encoding="utf-8") as f:
                json.dump({
                    "content": [{"type": "text", "text": "saved tool result"}],
                    "structuredContent": self.raw_rsi_response(
                        "SYNX", [40, 36, 33, 30, 29, 34]
                    ),
                    "isError": False,
                }, f)
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

    def test_cli_accepts_complete_rsi_envelope_by_committed_purpose(self):
        bars = [
            bar("2026-07-01", 4.5, 5.0, 900000),
            bar("2026-07-02", 4.6, 4.9, 900000),
            bar("2026-07-03", 4.6, 4.8, 900000),
            bar("2026-07-06", 4.6, 4.9, 900000),
            bar("2026-07-07", 4.6, 4.9, 900000),
        ]
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ):
            commit_test_source_purpose(
                scratch,
                "historicals-0",
                {"data": {"results": [{"symbol": "SYNX", "bars": bars}]}},
            )
            commit_test_source_purpose(
                scratch,
                "candidate-quotes-0",
                {"data": {"results": [{"quote": {
                    "symbol": "SYNX", "last_trade_price": "4.0"
                }}]}},
            )
            commit_test_source_purpose(
                scratch,
                "rsi-0",
                {
                    "content": [{"type": "text", "text": "saved tool result"}],
                    "structuredContent": self.raw_rsi_response(
                        "SYNX", [40, 36, 33, 30, 29, 34]
                    ),
                    "isError": False,
                },
            )
            output = os.path.join(scratch, "rsi-purpose-output.json")
            proc = self.evaluate_proc([
                "--scratch", scratch,
                "--bars-purpose", "historicals-0",
                "--quotes-purpose", "candidate-quotes-0",
                "--volume-lookback-days", "5",
                "--high-lookback-days", "5",
                "--min-median-dollar-volume", "0",
                "--dip-entry-pct", "5",
                "--rsi-purpose", "rsi-0",
                "--rsi-oversold", "35",
                "--rsi-lookback-bars", "5",
                "--rsi-confirm-bars", "1",
                "--rsi-max-entry", "60",
                "--rsi-period", "14",
                "--json-out", output,
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)

        self.assertIs(document["rsi_gate_enabled"], True)
        self.assertEqual(document["params"]["rsi_file"], ["rsi-0"])
        self.assertEqual(document["results"][0]["rsi_gate"], "pass")

    def test_cli_accumulates_repeated_purpose_flags_and_blocks_missing_inputs(self):
        bars = [
            bar("2026-07-01", 4.5, 5.0, 900000),
            bar("2026-07-02", 4.6, 4.9, 900000),
            bar("2026-07-03", 4.6, 4.8, 900000),
            bar("2026-07-06", 4.6, 4.9, 900000),
            bar("2026-07-07", 4.6, 4.9, 900000),
        ]
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ):
            commit_test_source_purpose(
                scratch,
                "historicals-0",
                {"data": {"results": [
                    {"symbol": "SYNX", "bars": bars},
                    {"symbol": "NOQUOTE", "bars": bars},
                ]}},
            )
            commit_test_source_purpose(
                scratch,
                "historicals-1",
                {"data": {"results": [
                    {"symbol": "OTHR", "bars": bars},
                ]}},
            )
            commit_test_source_purpose(
                scratch,
                "candidate-quotes-0",
                {"data": {"results": [
                    {"quote": {"symbol": "SYNX", "last_trade_price": "4.0"}},
                    {"quote": {"symbol": "NOHIST", "last_trade_price": "4.0"}},
                ]}},
            )
            commit_test_source_purpose(
                scratch,
                "candidate-quotes-1",
                {"data": {"results": [
                    {"quote": {"symbol": "OTHR", "last_trade_price": "4.0"}},
                ]}},
            )
            commit_test_source_purpose(
                scratch,
                "rsi-0",
                self.raw_rsi_response("SYNX", [40, 36, 33, 30, 29, 34]),
            )
            commit_test_source_purpose(
                scratch,
                "rsi-1",
                self.raw_rsi_response("OTHR", [42, 39, 36, 33, 31, 29]),
            )
            output = os.path.join(scratch, "repeated-purpose-output.json")
            proc = self.evaluate_proc([
                "--scratch", scratch,
                "--bars-purpose", "historicals-0",
                "--bars-purpose", "historicals-1",
                "--quotes-purpose", "candidate-quotes-0",
                "--quotes-purpose", "candidate-quotes-1",
                "--expected-symbols", "SYNX",
                "--expected-symbols", "OTHR", "NOHIST", "NOQUOTE", "NONE",
                "--volume-lookback-days", "5",
                "--high-lookback-days", "5",
                "--min-median-dollar-volume", "0",
                "--dip-entry-pct", "5",
                "--rsi-purpose", "rsi-0",
                "--rsi-purpose", "rsi-1",
                "--rsi-oversold", "35",
                "--rsi-lookback-bars", "5",
                "--rsi-confirm-bars", "1",
                "--rsi-max-entry", "60",
                "--rsi-period", "14",
                "--json-out", output,
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)

        self.assertEqual(
            document["params"]["bars"], ["historicals-0", "historicals-1"]
        )
        self.assertEqual(
            document["params"]["quotes"],
            ["candidate-quotes-0", "candidate-quotes-1"],
        )
        self.assertEqual(document["params"]["rsi_file"], ["rsi-0", "rsi-1"])
        self.assertEqual(
            document["params"]["expected_symbols"],
            ["SYNX", "OTHR", "NOHIST", "NOQUOTE", "NONE"],
        )
        by_symbol = {row["symbol"]: row for row in document["results"]}
        self.assertEqual(set(by_symbol), {"SYNX", "OTHR", "NOHIST", "NOQUOTE", "NONE"})
        self.assertTrue(by_symbol["SYNX"]["buy_candidate"])
        self.assertEqual(by_symbol["SYNX"]["rsi_gate"], "pass")
        self.assertFalse(by_symbol["OTHR"]["buy_candidate"])
        self.assertEqual(by_symbol["OTHR"]["rsi_gate"], "block")
        self.assertEqual(
            by_symbol["NOHIST"]["skip_reason"],
            "missing candidate input: historicals",
        )
        self.assertEqual(
            by_symbol["NOQUOTE"]["skip_reason"],
            "missing candidate input: quote",
        )
        self.assertEqual(
            by_symbol["NONE"]["skip_reason"],
            "missing candidate input: historicals and quote",
        )
        self.assertIsNone(by_symbol["NOQUOTE"]["current_price"])
        self.assertFalse(by_symbol["NOQUOTE"]["insufficient_history"])
        self.assertTrue(by_symbol["NOHIST"]["insufficient_history"])

    def test_rsi_rejects_malformed_raw_responses_and_tool_envelopes(self):
        valid = self.raw_rsi_response("SYNX", [40, 36, 33, 30, 29, 34])
        malformed = (
            {"structuredContent": valid, "isError": True},
            {"structuredContent": valid, "isError": "false"},
            {"structuredContent": "not an object", "isError": False},
            {"content": [{"type": "text", "text": json.dumps(valid)}]},
            {"content": [], "data": valid["data"], "isError": False},
            {"data": None},
            {"data": {"indicators": valid["data"]["indicators"]}},
            {"data": {"symbol": " BAD ", "indicators": valid["data"]["indicators"]}},
            {"data": {"symbol": "SYNX", "indicators": []}},
            {"data": {"symbol": "SYNX", "indicators": {}}},
            {"data": {"symbol": "SYNX", "indicators": [None]}},
            {"data": {"symbol": "SYNX", "indicators": [{"type": "macd", "series": []}]}},
            {"data": {"symbol": "SYNX", "indicators": [{"type": "rsi"}]}},
            {"data": {"symbol": "SYNX", "indicators": [{"type": "rsi", "series": {}}]}},
            {"data": {"symbol": "SYNX", "indicators": [{"type": "rsi", "series": [{}]}]}},
            {"data": {"symbol": "SYNX", "indicators": [{"type": "rsi", "series": [True]}]}},
        )
        for index, document in enumerate(malformed):
            with self.subTest(index=index, root_keys=list(document)):
                with self.assertRaises(ValueError):
                    load_rsi_map(((f"rsi-{index}", document),), 14)

    def test_rsi_rejects_duplicate_symbols_across_raw_and_legacy_inputs(self):
        raw = self.raw_rsi_response("synx", [40, 36, 33, 30, 29, 34])
        with self.assertRaisesRegex(
            ValueError, "duplicate RSI symbol across inputs for SYNX"
        ):
            load_rsi_map((
                ("raw", raw),
                ("legacy", {"SYNX": [40, 36, 33, 30, 29, 34]}),
            ), 14)

        with self.assertRaisesRegex(
            ValueError, "duplicate RSI symbol across inputs for SYNX"
        ):
            load_rsi_map((("legacy", {
                "SYNX": [40, 36, 33, 30, 29, 34],
                "synx": [40, 36, 33, 30, 29, 34],
            }),), 14)

    def test_rsi_legacy_map_rejects_nested_symbol_mismatch(self):
        with self.assertRaisesRegex(ValueError, "nested RSI symbol is OTHR"):
            load_rsi_map((("legacy", {
                "SYNX": self.raw_rsi_response(
                    "OTHR", [40, 36, 33, 30, 29, 34]
                ),
            }),), 14)

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
    def constants_result():
        return constants_validator.validate_constants_file()

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
            "--expected-constants-sha256",
            FilterScanTests.constants_result().source_sha256,
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

    def test_cli_accepts_scan_purpose_and_rejects_mixed_selector_modes(self):
        scan_document = {
            "data": {
                "result": {
                    "results": [scan_row("KEEP", 4.45, 0.1528, 557.75)],
                    "total_items": 1,
                }
            }
        }
        with tempfile.TemporaryDirectory() as scratch, bound_source_root(
            scratch
        ):
            reserved, _committed = commit_test_source_purpose(
                scratch, "run-scan", scan_document
            )
            output = os.path.join(scratch, "purpose-filter.json")
            common = [
                "--scratch", scratch,
                "--expected-constants-sha256",
                self.constants_result().source_sha256,
            ]
            proc = self.filter_proc([
                *common,
                "--scan-purpose", "run-scan",
                "--json-out", output,
            ])
            self.assertEqual(proc.returncode, 0, proc.stderr)
            receipt = json.loads(proc.stdout)
            canonical_scratch, marker = (
                broker_snapshot_module.validate_scratch_directory(scratch)
            )
            with open(reserved["source"], "rb") as handle:
                scan_raw = handle.read()
            self.assertEqual(receipt["scratch"], str(canonical_scratch))
            self.assertEqual(receipt["scratch_id"], marker["scratch_id"])
            self.assertEqual(receipt["scan_selector_kind"], "purpose")
            self.assertEqual(receipt["scan_purpose"], "run-scan")
            self.assertIsNone(receipt["scan_file"])
            constants_result = self.constants_result()
            self.assertEqual(
                receipt["constants_source_sha256"],
                constants_result.source_sha256,
            )
            self.assertEqual(
                receipt["price_min"], constants_result.raw_values["PRICE_MIN"]
            )
            self.assertEqual(
                receipt["price_max"], constants_result.raw_values["PRICE_MAX"]
            )
            self.assertEqual(
                receipt["min_rel_volume"],
                constants_result.raw_values["MIN_REL_VOLUME"],
            )
            self.assertEqual(
                receipt["min_abs_pct_change"],
                constants_result.raw_values["MIN_ABS_PCT_CHANGE"],
            )
            self.assertEqual(receipt["top_n"], constants_result.values["TOP_N"])
            self.assertEqual(
                receipt["scan_source_sha256"],
                hashlib.sha256(scan_raw).hexdigest(),
            )
            with open(output, encoding="utf-8") as handle:
                document = json.load(handle)
            self.assertEqual(
                [row["symbol"] for row in document["working_list"]],
                ["KEEP"],
            )

            mixed_output = os.path.join(scratch, "mixed-filter.json")
            proc = self.filter_proc([
                *common,
                "--scan-file", reserved["source"],
                "--scan-purpose", "run-scan",
                "--json-out", mixed_output,
            ])
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("not allowed with argument", proc.stderr)
            self.assertFalse(os.path.exists(mixed_output))

    def run_filter(
        self,
        rows,
        mcp_envelope=False,
        mcp_error=False,
        return_receipt=False,
    ):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
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

            scan = write_test_source(source_root, "scan.json", value=document)
            with open(scan, "rb") as handle:
                scan_raw = handle.read()
            stdout = run_cli(FILTER, ["--scratch", td, "--scan-file", scan,
                                      "--expected-constants-sha256",
                                      self.constants_result().source_sha256,
                                      "--json-out", out])
            receipt = json.loads(stdout)
            canonical_scratch, marker = (
                broker_snapshot_module.validate_scratch_directory(td)
            )
            self.assertEqual(receipt["scratch"], str(canonical_scratch))
            self.assertEqual(receipt["scratch_id"], marker["scratch_id"])
            self.assertEqual(receipt["scan_selector_kind"], "file")
            self.assertIsNone(receipt["scan_purpose"])
            self.assertEqual(receipt["scan_file"], str(Path(scan).resolve()))
            self.assertEqual(
                receipt["scan_source_sha256"],
                hashlib.sha256(scan_raw).hexdigest(),
            )
            with open(out, "rb") as f:
                raw = f.read()
            data = json.loads(raw)
            for field in (
                "total_items",
                "rows_returned",
                "rows_skipped",
                "passed_filters",
                "working_list",
            ):
                self.assertEqual(receipt[field], data[field])
            if return_receipt:
                return data, receipt, raw, stdout
            return data

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

    def test_recognized_result_requires_results_and_total_items(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            common = {
                "results": [scan_row("KEEP", 4.0, 0.05, 10.0)],
                "total_items": 1,
            }
            for missing in ("results", "total_items"):
                result = dict(common)
                del result[missing]
                scan = write_test_source(
                    source_root,
                    f"missing-{missing}.json",
                    value={"data": {"result": result}},
                )
                out = os.path.join(td, f"missing-{missing}-out.json")
                proc = self.filter_proc(
                    self.filter_args(scan, out, scratch=td)
                )

                with self.subTest(missing=missing):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn(
                        f"run_scan result missing required field(s): {missing}",
                        proc.stderr,
                    )
                    self.assertFalse(os.path.exists(out))

    def test_rejects_mcp_error_envelope(self):
        with self.assertRaises(AssertionError):
            self.run_filter(
                [scan_row("KEEP", 4.45, 0.1528, 557.75)],
                mcp_envelope=True,
                mcp_error=True,
            )

    def test_top_n_caps_by_relative_volume(self):
        rows = [scan_row(f"S{i}", 3.0, 0.05, 10.0 + i) for i in range(20)]
        data = self.run_filter(rows)
        symbols = [w["symbol"] for w in data["working_list"]]
        self.assertEqual(symbols, [f"S{i}" for i in range(19, 4, -1)])
        self.assertEqual(data["passed_filters"], 20)

    def test_success_receipt_is_compact_inline_and_bound_to_durable_handoff(self):
        rows = [scan_row(f"S{i}", 3.0, 0.05, 10.0 + i) for i in range(20)]
        data, receipt, raw, stdout = self.run_filter(
            rows, return_receipt=True
        )

        self.assertEqual(stdout.count("\n"), 1)
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "action",
                "ok",
                "scratch",
                "scratch_id",
                "scan_selector_kind",
                "scan_purpose",
                "scan_file",
                "scan_source_sha256",
                "constants_source_sha256",
                "price_min",
                "price_max",
                "min_rel_volume",
                "min_abs_pct_change",
                "top_n",
                "working_list_file",
                "byte_count",
                "sha256",
                "total_items",
                "rows_returned",
                "rows_skipped",
                "passed_filters",
                "working_list",
            },
        )
        self.assertEqual(receipt["schema_version"], 1)
        self.assertEqual(receipt["action"], "filter-scan")
        self.assertIs(receipt["ok"], True)
        self.assertTrue(os.path.isabs(receipt["scratch"]))
        self.assertRegex(
            receipt["scratch_id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        self.assertEqual(receipt["scan_selector_kind"], "file")
        self.assertIsNone(receipt["scan_purpose"])
        self.assertTrue(os.path.isabs(receipt["scan_file"]))
        self.assertRegex(receipt["scan_source_sha256"], r"^[0-9a-f]{64}$")
        constants_result = self.constants_result()
        self.assertEqual(
            receipt["constants_source_sha256"],
            constants_result.source_sha256,
        )
        for field, expected in (
            ("price_min", constants_result.raw_values["PRICE_MIN"]),
            ("price_max", constants_result.raw_values["PRICE_MAX"]),
            ("min_rel_volume", constants_result.raw_values["MIN_REL_VOLUME"]),
            (
                "min_abs_pct_change",
                constants_result.raw_values["MIN_ABS_PCT_CHANGE"],
            ),
        ):
            self.assertIs(type(receipt[field]), str)
            self.assertEqual(receipt[field], expected)
        self.assertIs(type(receipt["top_n"]), int)
        self.assertEqual(receipt["top_n"], constants_result.values["TOP_N"])
        self.assertEqual(receipt["working_list_file"], "out.json")
        self.assertEqual(receipt["byte_count"], len(raw))
        self.assertEqual(receipt["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertRegex(receipt["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(receipt["working_list"], data["working_list"])
        self.assertEqual(
            [row["symbol"] for row in receipt["working_list"]],
            [f"S{i}" for i in range(19, 4, -1)],
        )
        self.assertEqual(
            len(receipt["working_list"]), constants_result.values["TOP_N"]
        )
        self.assertNotIn("Scan rows:", stdout)
        self.assertNotIn(os.sep, receipt["working_list_file"])

    def test_invalid_or_stale_constants_hash_fails_without_handoff_or_receipt(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            scan = write_test_source(
                source_root,
                "scan.json",
                value={
                    "data": {
                        "result": {
                            "results": [scan_row("KEEP", 4.0, 0.05, 10.0)],
                            "total_items": 1,
                        }
                    }
                },
            )
            for label, expected_hash in (
                ("malformed", "not-a-sha256"),
                ("stale", "0" * 64),
            ):
                out = os.path.join(td, f"out-{label}.json")
                args = self.filter_args(scan, out, scratch=td)
                hash_index = args.index("--expected-constants-sha256") + 1
                args[hash_index] = expected_hash
                proc = self.filter_proc(args)
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertFalse(os.path.exists(out))

    def test_duplicate_working_symbol_fails_closed_without_success_receipt(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            document = {
                "data": {
                    "result": {
                        "results": [
                            scan_row("DUP", 4.0, 0.05, 10.0),
                            scan_row("DUP", 4.1, 0.06, 9.0),
                        ],
                        "total_items": 2,
                    }
                }
            }
            scan = write_test_source(source_root, "scan.json", value=document)
            out = os.path.join(td, "working-list.json")
            proc = self.filter_proc(self.filter_args(scan, out, scratch=td))

            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn("duplicate symbol", proc.stderr)
            self.assertFalse(os.path.exists(out))

    def test_existing_handoff_is_never_replaced_or_reported_as_success(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td) as source_root:
            document = {
                "data": {
                    "result": {
                        "results": [scan_row("KEEP", 4.0, 0.05, 10.0)],
                        "total_items": 1,
                    }
                }
            }
            scan = write_test_source(source_root, "scan.json", value=document)
            out = os.path.join(td, "working-list.json")
            sentinel = b'{"authoritative":"original"}\n'
            with open(out, "wb") as handle:
                handle.write(sentinel)

            proc = self.filter_proc(self.filter_args(scan, out, scratch=td))

            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertIn("already exists; refusing to replace it", proc.stderr)
            with open(out, "rb") as handle:
                self.assertEqual(handle.read(), sentinel)

    @staticmethod
    def valid_filter_handoff():
        return {
            "total_items": 1,
            "rows_returned": 1,
            "rows_skipped": 0,
            "passed_filters": 1,
            "working_list": [
                {
                    "symbol": "KEEP",
                    "last": 4.0,
                    "rel_volume": 10.0,
                    "day_pct_change": 5.0,
                    "volume": 1000.0,
                }
            ],
        }

    @staticmethod
    def scratch_binding(scratch):
        canonical, marker = broker_snapshot_module.validate_scratch_directory(
            scratch
        )
        return (
            canonical,
            marker["scratch_id"],
            filter_scan_module._directory_identity(canonical),
        )

    def test_post_publication_readback_failure_leaves_artifact_for_audit(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td):
            out = os.path.join(td, "working-list.json")
            document = self.valid_filter_handoff()
            canonical, scratch_id, scratch_identity = self.scratch_binding(td)
            stdout = io.StringIO()
            original_read_bytes = filter_scan_module.Path.read_bytes

            def fail_output_readback(path_object):
                if path_object == Path(out):
                    raise OSError("injected readback failure")
                return original_read_bytes(path_object)

            with mock.patch.object(
                filter_scan_module.Path,
                "read_bytes",
                autospec=True,
                side_effect=fail_output_readback,
            ), redirect_stdout(stdout):
                with self.assertRaisesRegex(OSError, "injected readback failure"):
                    filter_scan_module.write_handoff(
                        out,
                        canonical,
                        document,
                        1,
                        expected_scratch_id=scratch_id,
                        expected_scratch_identity=scratch_identity,
                    )

            self.assertEqual(stdout.getvalue(), "")
            self.assertTrue(os.path.isfile(out))
            with open(out, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), document)

    def test_unsupported_hard_links_fail_closed_without_output(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td):
            out = os.path.join(td, "working-list.json")
            document = self.valid_filter_handoff()
            canonical, scratch_id, scratch_identity = self.scratch_binding(td)
            stdout = io.StringIO()
            with mock.patch.object(
                filter_scan_module.os,
                "link",
                side_effect=OSError("hard links unsupported"),
            ), redirect_stdout(stdout):
                with self.assertRaisesRegex(OSError, "hard links unsupported"):
                    filter_scan_module.write_handoff(
                        out,
                        canonical,
                        document,
                        1,
                        expected_scratch_id=scratch_id,
                        expected_scratch_identity=scratch_identity,
                    )

            self.assertEqual(stdout.getvalue(), "")
            self.assertFalse(os.path.lexists(out))
            self.assertEqual(
                [name for name in os.listdir(td) if name.endswith(".tmp")],
                [],
            )

    def test_scratch_id_change_before_publication_fails_without_output(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td):
            out = os.path.join(td, "working-list.json")
            document = self.valid_filter_handoff()
            canonical, scratch_id, scratch_identity = self.scratch_binding(td)
            _same_path, marker = (
                broker_snapshot_module.validate_scratch_directory(td)
            )
            changed_marker = dict(marker)
            changed_marker["scratch_id"] = str(uuid.uuid4())

            with mock.patch.object(
                filter_scan_module,
                "validate_scratch_directory",
                return_value=(canonical, changed_marker),
            ):
                with self.assertRaisesRegex(ValueError, "scratch_id changed"):
                    filter_scan_module.write_handoff(
                        out,
                        canonical,
                        document,
                        1,
                        expected_scratch_id=scratch_id,
                        expected_scratch_identity=scratch_identity,
                    )

            self.assertFalse(os.path.lexists(out))

    def test_scratch_identity_change_after_readback_fails_and_keeps_artifact(self):
        with tempfile.TemporaryDirectory() as td, bound_source_root(td):
            out = os.path.join(td, "working-list.json")
            document = self.valid_filter_handoff()
            canonical, scratch_id, scratch_identity = self.scratch_binding(td)
            changed_identity = (scratch_identity[0], scratch_identity[1] + 1)

            with mock.patch.object(
                filter_scan_module,
                "_directory_identity",
                side_effect=[scratch_identity, changed_identity],
            ):
                with self.assertRaisesRegex(
                    ValueError, "scratch directory identity changed"
                ):
                    filter_scan_module.write_handoff(
                        out,
                        canonical,
                        document,
                        1,
                        expected_scratch_id=scratch_id,
                        expected_scratch_identity=scratch_identity,
                    )

            self.assertTrue(os.path.isfile(out))
            with open(out, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), document)

    def test_handoff_validator_enforces_counter_consistency(self):
        valid = self.valid_filter_handoff()
        valid["total_items"] = 2
        valid["rows_returned"] = 2
        filter_scan_module.validate_handoff(valid, 1)

        invalid_documents = []
        for updates in (
            {"rows_skipped": 3},
            {"passed_filters": 3},
            {"rows_skipped": 2, "passed_filters": 1},
            {"passed_filters": 0},
        ):
            document = json.loads(json.dumps(valid))
            document.update(updates)
            invalid_documents.append(document)
        for document in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaises(ValueError):
                    filter_scan_module.validate_handoff(document, 1)

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

    def test_missing_volume_and_ticker_symbol_mismatch_are_skipped(self):
        missing_volume = scan_row("NOVOL", 4.0, 0.05, 12.0)
        del missing_volume["columns"]["Volume"]
        missing_visible_symbol = scan_row("NOSYM", 4.0, 0.05, 11.0)
        del missing_visible_symbol["columns"]["Symbol"]
        mismatched_symbol = scan_row("TICKER", 4.0, 0.05, 10.0)
        mismatched_symbol["columns"]["Symbol"] = "OTHER"
        data = self.run_filter(
            [
                scan_row("KEEP", 4.0, 0.05, 13.0),
                missing_volume,
                missing_visible_symbol,
                mismatched_symbol,
            ]
        )

        self.assertEqual(
            [row["symbol"] for row in data["working_list"]], ["KEEP"]
        )
        self.assertEqual(data["rows_skipped"], 3)
        self.assertEqual(data["passed_filters"], 1)

    def test_only_canonical_robinhood_tickers_enter_working_list(self):
        rows = [
            scan_row("ABC", 4.0, 0.05, 20.0),
            scan_row("abc", 4.0, 0.05, 19.0),
            scan_row("ABC ", 4.0, 0.05, 18.0),
            scan_row("BRK.A", 4.0, 0.05, 17.0),
            scan_row("$ABC", 4.0, 0.05, 16.0),
            scan_row("ABC/WS", 4.0, 0.05, 15.0),
            scan_row("ABC-B", 4.0, 0.05, 14.0),
            scan_row("\N{LATIN CAPITAL LETTER A WITH ACUTE}BC", 4.0, 0.05, 13.0),
        ]
        data = self.run_filter(rows)

        self.assertEqual(
            [row["symbol"] for row in data["working_list"]],
            ["ABC", "BRK.A"],
        )
        self.assertEqual(data["rows_skipped"], 6)
        self.assertEqual(data["passed_filters"], 2)

    def test_handoff_validator_rejects_noncanonical_symbol_text(self):
        base = {
            "total_items": 1,
            "rows_returned": 1,
            "rows_skipped": 0,
            "passed_filters": 1,
            "working_list": [
                {
                    "symbol": "BRK.A",
                    "last": 4.0,
                    "rel_volume": 10.0,
                    "day_pct_change": 5.0,
                    "volume": 1000.0,
                }
            ],
        }
        filter_scan_module.validate_handoff(base, 1)
        for symbol in ("abc", "ABC ", "$ABC", "ABC/WS", "ABC-B", "\x00ABC"):
            document = json.loads(json.dumps(base))
            document["working_list"][0]["symbol"] = symbol
            with self.subTest(symbol=repr(symbol)):
                with self.assertRaises(ValueError):
                    filter_scan_module.validate_handoff(document, 1)

    def test_nonfinite_json_constant_fails_loudly(self):
        row = scan_row("BAD", 4.0, 0.05, 10.0)
        row["columns"]["Last"] = float("nan")
        with self.assertRaises(AssertionError):
            self.run_filter([row])


class OrderIntentTests(unittest.TestCase):
    RUN_TOKEN = "11111111-1111-4111-8111-111111111111"
    OTHER_RUN_TOKEN = "22222222-2222-4222-8222-222222222222"
    BASELINE_ORDER_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    @staticmethod
    def clear_entry_authorization(_run_token):
        return {
            "schema_version": 1,
            "action": "authorize-entry-intent",
            "ok": True,
            "invocation_id": "33333333-3333-4333-8333-333333333333",
            "phase": "order-placement",
            "entry_guard_outcome": "clear",
            "lease_renewed": True,
        }

    def invoke(self, state_file, action, *args, expected_success=True, now=None):
        args = list(args)
        if action == "observe":
            transport_scratch, source_root = self.ack_transport()
            if "--transport-scratch" not in args:
                args += ["--transport-scratch", transport_scratch]
            for flag in ("--orders", "--positions"):
                if flag not in args:
                    continue
                first = args.index(flag) + 1
                last = first
                while last < len(args) and not args[last].startswith("--"):
                    last += 1
                replacements = []
                for index, path in enumerate(args[first:last]):
                    with open(path, encoding="utf-8") as handle:
                        value = json.load(handle)
                    replacements.append(self.write_json(
                        source_root,
                        f"observe-{flag[2:]}-{uuid.uuid4().hex}-{index}.json",
                        value,
                    ))
                args[first:last] = replacements
        cli_args = [
            action,
            "--state-file",
            state_file,
        ]
        if action == "acknowledge" and "--transport-scratch" not in args:
            transport_scratch, _source_root = self.ack_transport()
            cli_args += ["--transport-scratch", transport_scratch]
        cli_args += args
        if now is None or (action == "check" and not expected_success):
            proc = subprocess.run(
                [sys.executable, ORDER_INTENTS, *cli_args],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
        else:
            injected = {"now_utc": now}
            if action in {"begin", "retry"}:
                injected["entry_authorizer"] = self.clear_entry_authorization
            proc = run_imported_main(
                order_intents_module.main, cli_args, **injected
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

    def test_cli_rejects_clock_override_but_imported_tests_can_inject_it(self):
        with tempfile.TemporaryDirectory() as td:
            state = os.path.join(td, "intents.sqlite3")
            rejected = subprocess.run(
                [
                    sys.executable,
                    ORDER_INTENTS,
                    "check",
                    "--state-file",
                    state,
                    "--now-utc",
                    "2026-07-31T16:00:01Z",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(rejected.returncode, 1)
            self.assertEqual(rejected.stderr, "")
            document = json.loads(rejected.stdout)
            self.assertFalse(document["ok"])
            self.assertEqual(document["reason"], "order_intent_state_error")
            self.assertIn("not valid on the CLI", document["detail"])

            injected = self.invoke(
                state,
                "check",
                expected_success=True,
                now="2026-07-31T16:00:01Z",
            )
            self.assertTrue(injected["ok"])

    @staticmethod
    def write_json(directory, name, value):
        return write_test_source(directory, name, value=value)

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

    def test_acknowledge_and_observe_accept_committed_source_purposes(self):
        with tempfile.TemporaryDirectory() as td:
            transport_scratch, _source_root = self.ack_transport()

            ack_state = os.path.join(td, "ack-purpose.sqlite3")
            prepared = self.prepare(td, ack_state)
            intent_id = prepared["intent_id"]
            self.invoke(
                ack_state,
                "begin",
                "--intent-id",
                intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            commit_test_source_purpose(
                transport_scratch,
                "place-order-response-0",
                {"data": {"order": self.broker_order()}},
            )
            acknowledged = self.invoke(
                ack_state,
                "acknowledge",
                "--intent-id",
                intent_id,
                "--response-purpose",
                "place-order-response-0",
                "--transport-scratch",
                transport_scratch,
                now="2026-07-31T16:00:04Z",
            )
            self.assertEqual(acknowledged["status"], "resolved")
            self.assertEqual(
                acknowledged["broker_order_id"],
                "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            )

            observe_state = os.path.join(td, "observe-purpose.sqlite3")
            prepared = self.prepare(
                td, observe_state, name="observe-purpose-intent.json"
            )
            observed_intent_id = prepared["intent_id"]
            self.invoke(
                observe_state,
                "begin",
                "--intent-id",
                observed_intent_id,
                "--run-token",
                self.RUN_TOKEN,
                now="2026-07-31T16:00:02Z",
            )
            commit_test_source_purpose(
                transport_scratch,
                "recovery-orders-0",
                {"data": {"orders": [], "next": None}},
            )
            commit_test_source_purpose(
                transport_scratch,
                "recovery-positions-0",
                {"data": {"positions": [], "next": None}},
            )
            observed = self.invoke(
                observe_state,
                "observe",
                "--intent-id",
                observed_intent_id,
                "--orders-purpose",
                "recovery-orders-0",
                "--positions-purpose",
                "recovery-positions-0",
                "--transport-scratch",
                transport_scratch,
                "--as-of-utc",
                "2026-07-31T16:01:00Z",
                now="2026-07-31T16:01:00Z",
            )
            self.assertFalse(observed["matched"])
            self.assertEqual(observed["status"], "unknown")

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

    def test_acknowledge_rejects_unknown_purpose_without_state_mutation(self):
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
            rejected = self.invoke(
                state,
                "acknowledge",
                "--intent-id",
                intent_id,
                "--response-purpose",
                "unknown-placement-response",
                "--transport-scratch",
                transport_scratch,
                expected_success=False,
                now="2026-07-31T16:00:03Z",
            )
            self.assertIn("no source handoff was reserved", rejected["detail"])
            self.assertEqual(
                hashlib.sha256(Path(state).read_bytes()).hexdigest(),
                before_sha256,
            )

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
        self._transport_root_ids = {}

    def tearDown(self):
        for source_root in self._transport_roots.values():
            _BOUND_SOURCE_SCRATCHES.pop(
                os.path.normcase(os.path.realpath(source_root)), None
            )
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
        if proc.returncode == 0 and args and args[0] == 'bind-transport':
            scratch = args[args.index('--scratch') + 1]
            source_root = args[args.index('--source-root') + 1]
            _BOUND_SOURCE_SCRATCHES[
                os.path.normcase(os.path.realpath(source_root))
            ] = os.path.realpath(scratch)
        return proc, document

    @staticmethod
    def write_json(directory, name, document):
        return write_test_source(directory, name, value=document)

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
        source_root, source_root_id = prepare_source_root_for_test(
            scratch, document['scratch_id']
        )
        scratch_key = os.path.abspath(scratch)
        self._transport_roots[scratch_key] = source_root
        self._transport_root_ids[scratch_key] = source_root_id
        if bind_transport:
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
            _BOUND_SOURCE_SCRATCHES[
                os.path.normcase(os.path.realpath(source_root))
            ] = os.path.realpath(scratch)
        return document

    def source_root(self, scratch):
        return self._transport_roots[os.path.abspath(scratch)]

    def source_root_id(self, scratch):
        return self._transport_root_ids[os.path.abspath(scratch)]

    def reserve_source(
        self, scratch, purpose, *, retry_of=None, first_request_cursors=None
    ):
        arguments = [
            'reserve-source', '--scratch', scratch, '--purpose', purpose
        ]
        if retry_of is not None:
            arguments.extend(['--retry-of', retry_of])
        match = re.fullmatch(
            r'first-positions-(0|[1-9][0-9]*)(?:-retry)?', purpose
        )
        if match is not None:
            if first_request_cursors is None:
                page_index = int(match.group(1))
                first_request_cursors = [
                    'FIRST',
                    *(f'test-cursor-{index}' for index in range(1, page_index + 1)),
                ]
            for cursor in first_request_cursors:
                arguments.extend(['--first-request-cursor', cursor])
        proc, receipt = self.invoke(*arguments)
        self.assertEqual(proc.returncode, 0, receipt)
        return receipt

    def commit_source(self, scratch, purpose, reservation_id=None):
        arguments = [
            'commit-source', '--scratch', scratch, '--purpose', purpose,
        ]
        if reservation_id is not None:
            arguments.extend(['--reservation-id', reservation_id])
        proc, receipt = self.invoke(*arguments)
        self.assertEqual(proc.returncode, 0, receipt)
        return receipt

    def reserve_write_commit(
        self, scratch, purpose, document, *, retry_of=None,
        first_request_cursors=None,
    ):
        reserved = self.reserve_source(
            scratch, purpose, retry_of=retry_of,
            first_request_cursors=first_request_cursors,
        )
        with open(
            reserved['source'], 'w', encoding='utf-8', newline='\n'
        ) as handle:
            json.dump(document, handle, separators=(',', ':'), sort_keys=True)
            handle.write('\n')
        committed = self.commit_source(scratch, purpose)
        return reserved, committed

    def test_source_handoff_journal_reserve_lookup_commit_and_idempotency(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            purpose = 'first-positions-0'
            payload = {'data': {'positions': [], 'next': None}}

            reserved = self.reserve_source(scratch, purpose)
            self.assertEqual(reserved['action'], 'reserve-source')
            self.assertEqual(reserved['status'], 'reserved')
            self.assertEqual(reserved['purpose'], purpose)
            self.assertFalse(reserved['idempotent'])
            self.assertEqual(reserved['first_request_cursor_count'], 1)
            self.assertRegex(
                reserved['first_request_cursors_sha256'], r'^[0-9a-f]{64}$'
            )
            self.assertRegex(
                reserved['reservation_id'],
                r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
                r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
            )
            self.assertEqual(
                os.path.dirname(reserved['source']), self.source_root(scratch)
            )
            self.assertFalse(os.path.exists(reserved['source']))

            proc, pending = self.invoke(
                'lookup-source', '--scratch', scratch, '--purpose', purpose
            )
            self.assertEqual(proc.returncode, 0, pending)
            self.assertEqual(pending['status'], 'reserved')
            self.assertEqual(pending['recovery_action'], 'halt')
            self.assertIsNone(pending['source'])
            self.assertEqual(
                pending['reservation_id'], reserved['reservation_id']
            )

            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump(payload, handle, separators=(',', ':'), sort_keys=True)
                handle.write('\n')
            proc, recoverable = self.invoke(
                'lookup-source', '--scratch', scratch, '--purpose', purpose
            )
            self.assertEqual(proc.returncode, 0, recoverable)
            self.assertEqual(recoverable['status'], 'reserved')
            self.assertEqual(recoverable['recovery_action'], 'commit-only')
            self.assertEqual(recoverable['source'], reserved['source'])
            self.assertEqual(
                recoverable['reservation_id'], reserved['reservation_id']
            )

            committed = self.commit_source(scratch, purpose)
            self.assertEqual(committed['status'], 'committed')
            self.assertFalse(committed['idempotent'])
            self.assertEqual(
                committed['source_sha256'],
                hashlib.sha256(Path(reserved['source']).read_bytes()).hexdigest(),
            )
            repeated = self.commit_source(scratch, purpose)
            self.assertTrue(repeated['idempotent'])
            self.assertEqual(
                repeated['source_sha256'], committed['source_sha256']
            )
            proc, looked_up = self.invoke(
                'lookup-source', '--scratch', scratch, '--purpose', purpose
            )
            self.assertEqual(proc.returncode, 0, looked_up)
            self.assertEqual(looked_up['status'], 'committed')
            self.assertEqual(looked_up['recovery_action'], 'consume')

            by_purpose = validate_bound_external_json_purpose(scratch, purpose)
            by_path = validate_bound_external_json_source(
                scratch, reserved['source']
            )
            self.assertEqual(by_purpose[0], Path(reserved['source']))
            self.assertEqual(by_purpose[1], by_path[1])
            self.assertEqual(by_purpose[2], by_path[2])
            plural = validate_bound_external_json_purposes(scratch, [purpose])
            self.assertEqual(plural, [by_purpose])

            proc, duplicate = self.invoke(
                'reserve-source', '--scratch', scratch, '--purpose', purpose,
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                duplicate['error']['code'], 'source_purpose_duplicate'
            )

    def test_commit_source_self_correlates_and_keeps_explicit_id_check(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            purpose = 'spy-red-check'
            reserved = self.reserve_source(scratch, purpose)

            proc, missing = self.invoke(
                'commit-source', '--scratch', scratch, '--purpose', purpose,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(missing['error']['code'], 'source_file_missing')

            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump({'data': {'results': []}}, handle)
                handle.write('\n')

            proc, mismatch = self.invoke(
                'commit-source', '--scratch', scratch, '--purpose', purpose,
                '--reservation-id', '00000000-0000-4000-8000-000000000000',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                mismatch['error']['code'], 'source_reservation_mismatch'
            )

            committed = self.commit_source(
                scratch, purpose, reserved['reservation_id']
            )
            self.assertEqual(
                committed['reservation_id'], reserved['reservation_id']
            )
            self.assertFalse(committed['idempotent'])
            repeated = self.commit_source(scratch, purpose)
            self.assertTrue(repeated['idempotent'])
            self.assertEqual(
                repeated['reservation_id'], reserved['reservation_id']
            )

    def test_source_handoff_abort_and_global_pending_fence(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            first = self.reserve_source(scratch, 'first-orders-0')

            proc, fenced = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                fenced['error']['code'], 'source_handoff_pending'
            )

            abort_args = (
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-orders-0',
                '--reservation-id', first['reservation_id'],
                '--reason', 'connector-failed',
            )
            proc, aborted = self.invoke(*abort_args)
            self.assertEqual(proc.returncode, 0, aborted)
            self.assertEqual(aborted['status'], 'aborted')
            self.assertEqual(aborted['reason'], 'connector-failed')
            self.assertIsNone(aborted['source'])
            self.assertFalse(aborted['idempotent'])
            proc, repeated = self.invoke(*abort_args)
            self.assertEqual(proc.returncode, 0, repeated)
            self.assertTrue(repeated['idempotent'])
            proc, looked_up = self.invoke(
                'lookup-source', '--scratch', scratch,
                '--purpose', 'first-orders-0',
            )
            self.assertEqual(proc.returncode, 0, looked_up)
            self.assertEqual(looked_up['status'], 'aborted')
            self.assertEqual(looked_up['recovery_action'], 'none')
            self.assertIsNone(looked_up['source'])

            proc, rejected_commit = self.invoke(
                'commit-source', '--scratch', scratch,
                '--purpose', 'first-orders-0',
                '--reservation-id', first['reservation_id'],
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                rejected_commit['error']['code'], 'source_handoff_aborted'
            )

            second = self.reserve_source(scratch, 'first-positions-0')
            with open(
                second['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump({'data': {'positions': [], 'next': None}}, handle)
                handle.write('\n')
            proc, refused_abort = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--reservation-id', second['reservation_id'],
                '--reason', 'file-change-failed',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                refused_abort['error']['code'], 'source_commit_required'
            )
            self.commit_source(
                scratch, 'first-positions-0', second['reservation_id']
            )

    def test_retry_authorization_requires_connector_failed_base_without_file(self):
        with tempfile.TemporaryDirectory() as td:
            def fresh_scratch(name):
                path = os.path.join(td, name)
                os.mkdir(path)
                self.preflight(path)
                return path

            scratch = fresh_scratch('authorized')

            base = self.reserve_source(scratch, 'first-positions-0')
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--reservation-id', base['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

            proc, unauthorized = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0-retry',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                unauthorized['error']['code'], 'source_retry_not_authorized'
            )

            retry_reserved, _retry_committed = self.reserve_write_commit(
                scratch,
                'first-positions-0-retry',
                {'data': {'positions': [], 'next': None}},
                retry_of='first-positions-0',
            )
            self.assertEqual(retry_reserved['retry_of'], 'first-positions-0')
            broker_snapshot_module.validate_bound_source_retry_authorization(
                scratch, 'first-positions-0', 'first-positions-0-retry'
            )

            scratch = fresh_scratch('committed-base')
            self.reserve_write_commit(
                scratch,
                'first-positions-0',
                {'data': {'positions': [], 'next': None}},
            )
            proc, committed_base = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0-retry',
                '--retry-of', 'first-positions-0',
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                committed_base['error']['code'], 'source_retry_not_authorized'
            )

            scratch = fresh_scratch('wrong-reason')
            other = self.reserve_source(scratch, 'first-positions-0')
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--reservation-id', other['reservation_id'],
                '--reason', 'serialization-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)
            proc, wrong_reason = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0-retry',
                '--retry-of', 'first-positions-0',
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                wrong_reason['error']['code'], 'source_retry_not_authorized'
            )

            scratch = fresh_scratch('pending-base')
            pending = self.reserve_source(scratch, 'first-positions-0')
            proc, pending_base = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0-retry',
                '--retry-of', 'first-positions-0',
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                pending_base['error']['code'], 'source_handoff_pending'
            )
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--reservation-id', pending['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

            scratch = fresh_scratch('missing-base')
            proc, missing_base = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-0-retry',
                '--retry-of', 'first-positions-0',
                '--first-request-cursor', 'FIRST',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                missing_base['error']['code'], 'source_reservation_missing'
            )
            # The failed retry authorization did not create a reservation or
            # pending fence that could acquire authority retroactively.
            later_base = self.reserve_source(scratch, 'first-positions-0')
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-0',
                '--reservation-id', later_base['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

            scratch = fresh_scratch('invalid-purpose')
            for invalid_purpose in (
                'first-positions-00',
                'first-positions-00-retry',
                'first-positions-0-retry2',
                'first-positions-0-retry-retry',
            ):
                with self.subTest(invalid_purpose=invalid_purpose):
                    arguments = [
                        'reserve-source', '--scratch', scratch,
                        '--purpose', invalid_purpose,
                    ]
                    if invalid_purpose.endswith('-retry-retry'):
                        arguments.extend([
                            '--retry-of', 'first-positions-0-retry'
                        ])
                    proc, invalid = self.invoke(*arguments)
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(
                        invalid['error']['code'], 'source_purpose_invalid'
                    )

            scratch = fresh_scratch('cursor-mismatch')
            self.reserve_write_commit(
                scratch,
                'first-positions-0',
                {
                    'data': {
                        'positions': [],
                        'next': (
                            'https://api.robinhood.com/positions/'
                            '?cursor=cursor-a'
                        ),
                    }
                },
            )
            base = self.reserve_source(
                scratch, 'first-positions-1',
                first_request_cursors=['FIRST', 'cursor-a'],
            )
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'first-positions-1',
                '--reservation-id', base['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)
            proc, mismatch = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-1-retry',
                '--retry-of', 'first-positions-1',
                '--first-request-cursor', 'FIRST',
                '--first-request-cursor', 'cursor-b',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                mismatch['error']['code'], 'request_binding_invalid'
            )

    def test_first_reservation_binds_page_index_before_broker_read(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)

            cases = (
                ('first-positions-1', ['FIRST'], False),
                (
                    'first-positions-1000',
                    ['FIRST', *(f'cursor-{index}' for index in range(1, 1000))],
                    True,
                ),
            )
            for purpose, cursors, invoke_in_process in cases:
                with self.subTest(purpose=purpose):
                    arguments = [
                        'reserve-source', '--scratch', scratch,
                        '--purpose', purpose,
                    ]
                    for cursor in cursors:
                        arguments.extend(['--first-request-cursor', cursor])
                    if invoke_in_process:
                        # A thousand repeated cursor flags exceed Windows'
                        # CreateProcess command-line limit before the helper
                        # can inspect them.  Exercise the identical parser and
                        # action in-process so the helper's exact page ceiling
                        # remains covered on every platform.
                        stdout = io.StringIO()
                        with redirect_stdout(stdout):
                            returncode = broker_snapshot_module.main(arguments)
                        rejected = json.loads(stdout.getvalue())
                    else:
                        proc, rejected = self.invoke(*arguments)
                        returncode = proc.returncode
                    self.assertNotEqual(returncode, 0)
                    self.assertEqual(
                        rejected['error']['code'], 'source_purpose_invalid'
                    )
                    proc, missing = self.invoke(
                        'lookup-source', '--scratch', scratch,
                        '--purpose', purpose,
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(
                        missing['error']['code'], 'source_reservation_missing'
                    )

    def test_first_reservation_rejects_invented_next_cursor_before_read(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'first-positions-0',
                {
                    'data': {
                        'positions': [],
                        'next': (
                            'https://api.robinhood.com/positions/'
                            '?cursor=broker-cursor'
                        ),
                    }
                },
                first_request_cursors=['FIRST'],
            )

            proc, rejected = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'first-positions-1',
                '--first-request-cursor', 'FIRST',
                '--first-request-cursor', 'invented-cursor',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                rejected['error']['code'], 'request_binding_invalid'
            )
            proc, missing = self.invoke(
                'lookup-source', '--scratch', scratch,
                '--purpose', 'first-positions-1',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                missing['error']['code'], 'source_reservation_missing'
            )

            reserved = self.reserve_source(
                scratch,
                'first-positions-1',
                first_request_cursors=['FIRST', 'broker-cursor'],
            )
            self.assertEqual(reserved['first_request_cursor_count'], 2)

    def test_first_page_one_consumer_binds_full_chain_for_base_and_retry(self):
        for use_retry in (False, True):
            with self.subTest(use_retry=use_retry), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch)
                self.reserve_write_commit(
                    scratch,
                    'first-positions-0',
                    {
                        'data': {
                            'positions': [],
                            'next': (
                                'https://api.robinhood.com/positions/'
                                '?cursor=broker-cursor'
                            ),
                        }
                    },
                    first_request_cursors=['FIRST'],
                )
                purpose = 'first-positions-1'
                if use_retry:
                    base = self.reserve_source(
                        scratch,
                        purpose,
                        first_request_cursors=['FIRST', 'broker-cursor'],
                    )
                    proc, aborted = self.invoke(
                        'abort-source', '--scratch', scratch,
                        '--purpose', purpose,
                        '--reservation-id', base['reservation_id'],
                        '--reason', 'connector-failed',
                    )
                    self.assertEqual(proc.returncode, 0, aborted)
                    purpose += '-retry'
                    self.reserve_write_commit(
                        scratch,
                        purpose,
                        {'data': {'positions': [], 'next': None}},
                        retry_of='first-positions-1',
                        first_request_cursors=['FIRST', 'broker-cursor'],
                    )
                else:
                    self.reserve_write_commit(
                        scratch,
                        purpose,
                        {'data': {'positions': [], 'next': None}},
                        first_request_cursors=['FIRST', 'broker-cursor'],
                    )

                proc = subprocess.run(
                    [
                        sys.executable, CONNECTOR_CONTRACT, 'page',
                        '--scratch', scratch,
                        '--source-purpose', purpose,
                        '--kind', 'positions',
                        '--request-cursor', 'FIRST',
                        '--request-cursor', 'broker-cursor',
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                receipt = json.loads(proc.stdout)
                self.assertEqual(receipt['action'], 'page')
                self.assertEqual(receipt['source_purpose'], purpose)
                self.assertEqual(
                    receipt['request_cursors'], ['FIRST', 'broker-cursor']
                )
                self.assertEqual(receipt['request_cursor'], 'broker-cursor')
                self.assertIs(receipt['complete'], True)

    def test_source_handoff_concurrent_reservations_have_one_winner(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)

            def reserve(purpose):
                return self.invoke(
                    'reserve-source', '--scratch', scratch,
                    '--purpose', purpose,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    reserve, ('concurrent-orders', 'concurrent-positions')
                ))
            winners = [result for result in results if result[0].returncode == 0]
            losers = [result for result in results if result[0].returncode != 0]
            self.assertEqual(len(winners), 1, results)
            self.assertEqual(len(losers), 1, results)
            self.assertIn(
                losers[0][1]['error']['code'],
                {'source_journal_busy', 'source_handoff_pending'},
            )

            winner = winners[0][1]
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', winner['purpose'],
                '--reservation-id', winner['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

    def test_pending_handoff_blocks_consuming_older_committed_source(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch, 'older-positions',
                {'data': {'positions': [], 'next': None}},
            )
            pending = self.reserve_source(scratch, 'newer-orders')
            with self.assertRaises(SourceHandoffError) as raised:
                validate_bound_external_json_purpose(
                    scratch, 'older-positions'
                )
            self.assertEqual(raised.exception.code, 'source_handoff_pending')
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'newer-orders',
                '--reservation-id', pending['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

    def test_source_handoff_rejects_invalid_purpose_and_non_strict_json(self):
        invalid_purposes = (
            '',
            'Uppercase',
            'daily-loss-A-discovery-positions-0',
            'bad_key',
            'a' * 49,
        )
        for purpose in invalid_purposes:
            with self.subTest(purpose=purpose), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch)
                proc, result = self.invoke(
                    'reserve-source', '--scratch', scratch, '--purpose', purpose
                )
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(
                    result['error']['code'], 'source_purpose_invalid'
                )

        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            reserved = self.reserve_source(scratch, 'malformed-response')
            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                handle.write('{"data":1,"data":2}\n')
            proc, result = self.invoke(
                'commit-source', '--scratch', scratch,
                '--purpose', 'malformed-response',
                '--reservation-id', reserved['reservation_id'],
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(result['error']['code'], 'source_file_invalid')
            self.assertFalse(any(
                name.startswith(
                    broker_snapshot_module.SOURCE_TERMINAL_MARKER_PREFIX
                )
                for name in os.listdir(scratch)
            ))

    def test_committed_source_tamper_is_rejected_by_lookup_and_validators(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            reserved, _committed = self.reserve_write_commit(
                scratch,
                'final-quotes-0',
                {'data': {'results': []}},
            )
            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump({'data': {'results': [{'changed': True}]}}, handle)
                handle.write('\n')

            proc, result = self.invoke(
                'lookup-source', '--scratch', scratch,
                '--purpose', 'final-quotes-0',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(result['error']['code'], 'source_file_changed')
            with self.assertRaises(SourceHandoffError) as raised:
                validate_bound_external_json_purpose(
                    scratch, 'final-quotes-0'
                )
            self.assertEqual(raised.exception.code, 'source_file_changed')
            with self.assertRaises(SourceHandoffError) as raised:
                validate_bound_external_json_source(
                    scratch, reserved['source']
                )
            self.assertEqual(raised.exception.code, 'source_file_changed')

    def test_stage_accepts_committed_source_purpose_and_hides_random_path(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            purpose = 'first-portfolio'
            reserved, _committed = self.reserve_write_commit(
                scratch,
                purpose,
                {
                    'data': {
                        'total_value': '1500.01',
                        'cash': '100',
                        'buying_power': '100',
                    }
                },
            )

            rejected_output = os.path.join(scratch, 'mixed.json')
            proc, rejected = self.invoke(
                'stage', '--kind', 'portfolio', '--generation', 'A',
                '--source', reserved['source'],
                '--source-purpose', purpose,
                '--output', rejected_output,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(rejected['error']['code'], 'usage_error')
            self.assertFalse(os.path.exists(rejected_output))

            output = os.path.join(scratch, 'portfolio.json')
            proc, result = self.invoke(
                'stage', '--kind', 'portfolio', '--generation', 'A',
                '--source-purpose', purpose,
                '--output', output,
            )
            self.assertEqual(proc.returncode, 0, result)
            self.assertTrue(result['ok'])
            self.assertEqual(result['files'][0]['source_purpose'], purpose)
            self.assertNotIn('source', result['files'][0])
            self.assertNotIn(reserved['source'], json.dumps(result))
            with open(output, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)['data']['cash'], '100')

    def test_windows_file_change_temp_directory_prepares_narrow_acl(self):
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        expected = str(temp_root / 'rhmra-session-private')

        with mock.patch.object(
            broker_snapshot_module, '_WINDOWS', True
        ), mock.patch.object(
            broker_snapshot_module.tempfile,
            'mkdtemp',
            return_value=expected,
        ) as mkdtemp, mock.patch.object(
            broker_snapshot_module, '_windows_prepare_file_change_directory'
        ) as prepare:
            created = broker_snapshot_module._create_file_change_temp_directory(
                temp_root, 'rhmra-session-'
            )

        self.assertEqual(created, expected)
        mkdtemp.assert_called_once_with(
            prefix='rhmra-session-', dir=str(temp_root)
        )
        prepare.assert_called_once_with(Path(expected), temp_root)

    def test_windows_file_change_acl_is_exact_and_non_inheriting_for_writers(self):
        path = Path(r'C:\Temp\rhmra-session-private')
        temp_root = path.parent
        helper_sid = 'S-1-5-21-300'
        temp_owner_sid = 'S-1-5-21-100'
        inheritable = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_CONTAINER_INHERIT_ACE
        )
        owner_file_only = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_INHERIT_ONLY_ACE
        )
        entries = [
            (
                helper_sid,
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-18',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-32-544',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-3-4',
                owner_file_only,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                temp_owner_sid,
                0,
                broker_snapshot_module._WINDOWS_FILE_CHANGE_DIRECTORY_ACCESS,
            ),
        ]
        api = (mock.Mock(), mock.Mock())
        with mock.patch.object(
            broker_snapshot_module,
            '_windows_security_api',
            return_value=api,
        ), mock.patch.object(
            broker_snapshot_module,
            '_windows_path_owner_sid',
            side_effect=[helper_sid, temp_owner_sid],
        ), mock.patch.object(
            broker_snapshot_module, '_windows_set_directory_dacl'
        ) as set_dacl, mock.patch.object(
            broker_snapshot_module,
            '_windows_read_directory_acl',
            return_value=(helper_sid, True, entries),
        ):
            broker_snapshot_module._windows_prepare_file_change_directory(
                path, temp_root
            )

        set_dacl.assert_called_once_with(
            path,
            'D:P'
            f'(A;OICI;FA;;;{helper_sid})'
            '(A;OICI;FA;;;SY)'
            '(A;OICI;FA;;;BA)'
            '(A;OIIO;FA;;;OW)'
            f'(A;;0x001200ab;;;{temp_owner_sid})',
            api,
        )

    def test_windows_file_change_acl_rejects_readback_mismatches(self):
        path = Path(r'C:\Temp\rhmra-session-private')
        temp_root = path.parent
        helper_sid = 'S-1-5-21-300'
        temp_owner_sid = 'S-1-5-21-100'
        inheritable = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_CONTAINER_INHERIT_ACE
        )
        owner_file_only = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_INHERIT_ONLY_ACE
        )
        entries = [
            (
                helper_sid,
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-18',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-32-544',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-3-4',
                owner_file_only,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                temp_owner_sid,
                0,
                broker_snapshot_module._WINDOWS_FILE_CHANGE_DIRECTORY_ACCESS,
            ),
        ]
        cases = (
            (
                'owner',
                ('S-1-5-21-999', True, entries),
                'owner changed',
            ),
            ('protection', (helper_sid, False, entries), 'not protected'),
            (
                'entries',
                (helper_sid, True, [*entries, entries[-1]]),
                'verification mismatch',
            ),
        )
        for name, readback, message in cases:
            with self.subTest(name=name), mock.patch.object(
                broker_snapshot_module,
                '_windows_security_api',
                return_value=(mock.Mock(), mock.Mock()),
            ), mock.patch.object(
                broker_snapshot_module,
                '_windows_path_owner_sid',
                side_effect=[helper_sid, temp_owner_sid],
            ), mock.patch.object(
                broker_snapshot_module, '_windows_set_directory_dacl'
            ), mock.patch.object(
                broker_snapshot_module,
                '_windows_read_directory_acl',
                return_value=readback,
            ):
                with self.assertRaisesRegex(OSError, message):
                    broker_snapshot_module._windows_prepare_file_change_directory(
                        path, temp_root
                    )

    def test_windows_file_change_acl_needs_no_bridge_for_same_owner(self):
        path = Path(r'C:\Temp\rhmra-session-private')
        temp_root = path.parent
        helper_sid = 'S-1-5-21-300'
        inheritable = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_CONTAINER_INHERIT_ACE
        )
        owner_file_only = (
            broker_snapshot_module._WINDOWS_OBJECT_INHERIT_ACE
            | broker_snapshot_module._WINDOWS_INHERIT_ONLY_ACE
        )
        entries = [
            (
                helper_sid,
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-18',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-5-32-544',
                inheritable,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
            (
                'S-1-3-4',
                owner_file_only,
                broker_snapshot_module._WINDOWS_FILE_ALL_ACCESS,
            ),
        ]
        api = (mock.Mock(), mock.Mock())
        with mock.patch.object(
            broker_snapshot_module,
            '_windows_security_api',
            return_value=api,
        ), mock.patch.object(
            broker_snapshot_module,
            '_windows_path_owner_sid',
            side_effect=[helper_sid, helper_sid],
        ), mock.patch.object(
            broker_snapshot_module, '_windows_set_directory_dacl'
        ) as set_dacl, mock.patch.object(
            broker_snapshot_module,
            '_windows_read_directory_acl',
            return_value=(helper_sid, True, entries),
        ):
            broker_snapshot_module._windows_prepare_file_change_directory(
                path, temp_root
            )

        set_dacl.assert_called_once_with(
            path,
            'D:P'
            f'(A;OICI;FA;;;{helper_sid})'
            '(A;OICI;FA;;;SY)'
            '(A;OICI;FA;;;BA)'
            '(A;OIIO;FA;;;OW)',
            api,
        )

    def test_windows_acl_preparation_failure_removes_created_directory(self):
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        created = Path(tempfile.mkdtemp(prefix='rhmra-acl-failure-'))
        try:
            with mock.patch.object(
                broker_snapshot_module, '_WINDOWS', True
            ), mock.patch.object(
                broker_snapshot_module.tempfile,
                'mkdtemp',
                return_value=str(created),
            ), mock.patch.object(
                broker_snapshot_module,
                '_windows_prepare_file_change_directory',
                side_effect=OSError('simulated ACL failure'),
            ):
                with self.assertRaisesRegex(OSError, 'simulated ACL failure'):
                    broker_snapshot_module._create_file_change_temp_directory(
                        temp_root, 'rhmra-session-'
                    )
            self.assertFalse(created.exists())
        finally:
            shutil.rmtree(created, ignore_errors=True)

    def test_non_windows_file_change_temp_directory_remains_owner_private(self):
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        expected = str(temp_root / 'rhmra-session-private')
        with mock.patch.object(
            broker_snapshot_module, '_WINDOWS', False
        ), mock.patch.object(
            broker_snapshot_module.tempfile,
            'mkdtemp',
            return_value=expected,
        ) as mkdtemp, mock.patch.object(
            broker_snapshot_module, '_windows_prepare_file_change_directory'
        ) as prepare:
            created = broker_snapshot_module._create_file_change_temp_directory(
                temp_root, 'rhmra-session-'
            )

        self.assertEqual(created, expected)
        mkdtemp.assert_called_once_with(
            prefix='rhmra-session-', dir=str(temp_root)
        )
        prepare.assert_not_called()

    def test_created_preflight_uses_file_change_creator_for_both_directories(self):
        real_creator = broker_snapshot_module._create_file_change_temp_directory
        args = mock.Mock(create_scratch=True, scratch=None)
        with mock.patch.object(
            broker_snapshot_module,
            '_create_file_change_temp_directory',
            wraps=real_creator,
        ) as creator:
            document = broker_snapshot_module._preflight(args)

        scratch = Path(document['scratch'])
        source_root = Path(document['source_root'])
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        try:
            self.assertEqual(
                creator.call_args_list,
                [
                    mock.call(temp_root, 'rhmra-session-'),
                    mock.call(temp_root, 'rhmra-source-'),
                ],
            )
            self.assertNotEqual(scratch, source_root)
        finally:
            shutil.rmtree(source_root, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)

    def test_create_scratch_preflights_without_a_caller_supplied_path(self):
        proc, document = self.invoke('preflight', '--create-scratch')
        self.assertEqual(proc.returncode, 0, (document, proc.stderr))
        self.assertEqual(
            set(document),
            {
                'schema_version', 'action', 'ok', 'scratch', 'sentinel_sha256',
                'scratch_id', 'write_read_parse', 'cleanup_verified',
                'source_root', 'source_root_id',
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
        self.assertRegex(
            document['source_root_id'],
            r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-'
            r'[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
        )
        self.assertNotEqual(document['scratch_id'], document['source_root_id'])
        self.assertRegex(document['sentinel_sha256'], r'^[0-9a-f]{64}$')

        scratch = Path(document['scratch'])
        source_root = Path(document['source_root'])
        marker = scratch / '.rhmra-broker-snapshot-scratch.json'
        preparation_marker = scratch / (
            '.rhmra-broker-response-source-root-prepared.json'
        )
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
            self.assertTrue(source_root.is_absolute())
            self.assertEqual(source_root.parent, scratch.parent)
            self.assertNotEqual(source_root, scratch)
            self.assertTrue(source_root.name.startswith('rhmra-source-'))
            self.assertTrue(source_root.is_dir())
            self.assertFalse(source_root.is_symlink())
            self.assertFalse(
                getattr(source_root, 'is_junction', lambda: False)()
            )
            self.assertEqual(list(source_root.iterdir()), [])
            with open(marker, encoding='utf-8') as handle:
                marker_document = json.load(handle)
            self.assertEqual(
                marker_document['scratch_id'],
                document['scratch_id'],
            )
            with open(preparation_marker, 'rb') as handle:
                preparation_raw = handle.read()
            preparation_document = json.loads(preparation_raw)
            self.assertEqual(
                set(preparation_document),
                {
                    'schema_version', 'marker', 'scratch_id', 'transport',
                    'source_root', 'source_root_id', 'source_root_device',
                    'source_root_inode',
                },
            )
            self.assertEqual(
                preparation_raw,
                broker_snapshot_module._canonical_bytes(
                    preparation_document
                ),
            )
            self.assertEqual(
                preparation_document['marker'],
                'rhmra-broker-response-source-root-prepared',
            )
            self.assertEqual(
                preparation_document['scratch_id'], document['scratch_id']
            )
            self.assertEqual(
                preparation_document['source_root'], str(source_root)
            )
            self.assertEqual(
                preparation_document['source_root_id'],
                document['source_root_id'],
            )
            source_stat = os.lstat(source_root)
            self.assertEqual(
                preparation_document['source_root_device'],
                str(source_stat.st_dev),
            )
            self.assertEqual(
                preparation_document['source_root_inode'],
                str(source_stat.st_ino),
            )
            self.assertFalse(
                any(
                    child.name.startswith('.rhmra-scratch-preflight-')
                    for child in scratch.iterdir()
                )
            )
        finally:
            shutil.rmtree(source_root, ignore_errors=True)
            shutil.rmtree(scratch, ignore_errors=True)

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

    def test_hyphenated_stage_action_names_the_correct_command(self):
        proc, document = self.invoke('stage-quotes', '--generation', 'A')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(document['action'], 'unknown')
        self.assertFalse(document['ok'])
        self.assertEqual(document['error']['code'], 'usage_error')
        self.assertIn('stage --kind quotes', document['error']['message'])

    def test_every_staged_kind_rejects_its_hyphenated_form(self):
        for kind in broker_snapshot_module.SNAPSHOT_KINDS:
            with self.subTest(kind=kind):
                proc, document = self.invoke('stage-' + kind)
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(document['error']['code'], 'usage_error')
                self.assertIn(
                    'stage --kind ' + kind, document['error']['message']
                )

    def test_stage_historicals_is_rejected_as_a_non_staged_kind(self):
        proc, document = self.invoke(
            'stage-historicals', '--raw-file', 'hist-batch1.json'
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(document['action'], 'unknown')
        self.assertEqual(document['error']['code'], 'usage_error')
        message = document['error']['message']
        self.assertIn('not a staged kind', message)
        self.assertIn('evaluate_candidates.py', message)
        self.assertNotIn('stage --kind historicals', message)

    def test_stage_action_still_parses_its_kind_argument(self):
        proc, document = self.invoke('stage', '--kind', 'quotes')
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(document['action'], 'stage')
        self.assertEqual(document['error']['code'], 'usage_error')
        self.assertIn('--generation', document['error']['message'])

    def test_create_scratch_failure_has_stable_error_code(self):
        stdout = io.StringIO()
        with mock.patch.object(
            broker_snapshot_module,
            '_create_file_change_temp_directory',
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

    def test_source_root_create_failure_cleans_scratch_with_stable_error_code(self):
        created = []
        real_creator = broker_snapshot_module._create_file_change_temp_directory

        def fail_second_create(temp_root, prefix):
            if created:
                raise OSError('simulated source-root create failure')
            path = real_creator(temp_root, prefix)
            created.append(Path(path))
            return path

        stdout = io.StringIO()
        with mock.patch.object(
            broker_snapshot_module,
            '_create_file_change_temp_directory',
            side_effect=fail_second_create,
        ), redirect_stdout(stdout):
            result = broker_snapshot_module.main(
                ['preflight', '--create-scratch']
            )
        self.assertEqual(result, 2)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document['error']['code'], 'scratch_create_failed')
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())

    def test_failed_created_preflight_removes_both_empty_directories(self):
        captured = {}

        def fail_preflight(path, **kwargs):
            captured['scratch'] = Path(path)
            captured['source_root'] = kwargs['prepared_source_root']
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
        self.assertFalse(captured['source_root'].exists())

    def test_failed_created_preflight_preserves_unexpected_content(self):
        captured = {}

        def fail_with_unexpected_file(path, **kwargs):
            scratch = Path(path)
            source_root = kwargs['prepared_source_root']
            captured['scratch'] = scratch
            captured['source_root'] = source_root
            (scratch / 'unexpected.json').write_text(
                '{}', encoding='utf-8'
            )
            (source_root / 'unexpected.json').write_text(
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
            source_root = captured['source_root']
            self.assertTrue(scratch.is_dir())
            self.assertTrue(source_root.is_dir())
            self.assertTrue((scratch / 'unexpected.json').is_file())
            self.assertTrue((source_root / 'unexpected.json').is_file())
        finally:
            scratch = captured.get('scratch')
            source_root = captured.get('source_root')
            if source_root is not None and source_root.exists():
                (source_root / 'unexpected.json').unlink(missing_ok=True)
                os.rmdir(source_root)
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
                        scratch, scratch_id, source_root, None
                    )
            marker = scratch / '.rhmra-broker-response-transport-attempt.json'
            self.assertTrue(marker.exists())
            with open(marker, encoding='utf-8') as handle:
                self.assertEqual(
                    json.load(handle),
                    {
                        'schema_version': 1,
                        'marker': 'rhmra-broker-response-transport-attempt',
                        'scratch_id': scratch_id,
                        'transport': 'file-change',
                        'source_root': str(source_root),
                        'canary_instance': None,
                    },
                )
            with self.assertRaisesRegex(SnapshotError, 'already attempted'):
                broker_snapshot_module._record_transport_attempt(
                    scratch, scratch_id, source_root, None
                )

    def test_canary_cleanup_refuses_to_delete_a_replacement_instance(self):
        with tempfile.TemporaryDirectory(
            prefix='rhmra-source-', dir=tempfile.gettempdir()
        ) as source_root:
            canary = self.write_text(
                source_root, 'accounts.json', 'original-sensitive-response'
            )
            candidate = (
                broker_snapshot_module._safe_transport_canary_cleanup_candidate(
                    source_root, canary
                )
            )
            self.assertIsNotNone(candidate)
            os.unlink(canary)
            self.write_text(
                source_root,
                'accounts.json',
                'replacement-that-must-not-be-deleted-because-it-is-different',
            )
            with self.assertRaisesRegex(
                SnapshotError,
                'transport canary instance changed before privacy cleanup',
            ):
                broker_snapshot_module._remove_transport_canary(candidate)
            self.assertTrue(os.path.isfile(canary))
            with open(canary, encoding='utf-8') as handle:
                self.assertEqual(
                    handle.read(),
                    'replacement-that-must-not-be-deleted-because-it-is-different',
                )

    def test_failed_bind_consumes_one_shot_and_removes_accounts_canary(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            scratch_result = self.preflight(scratch, bind_transport=False)
            source_root = self.source_root(scratch)
            rejected_canary = self.write_json(
                source_root,
                'errored-get-accounts-canary.json',
                {
                    'isError': True,
                    'structuredContent': {'data': {'accounts': []}},
                },
            )
            rejected_canary_stat = os.lstat(rejected_canary)
            rejected_canary_instance = (
                broker_snapshot_module._transport_canary_instance(
                    Path(rejected_canary), rejected_canary_stat
                )
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
                    'canary_instance': rejected_canary_instance,
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
                source_root = self.source_root(scratch)
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

    def test_invalid_scratch_does_not_consume_attempt_but_cleans_canary(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            source_root = tempfile.mkdtemp(prefix='rhmra-source-')
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
            source_root = self.source_root(scratch)
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

    def test_legacy_preflight_without_prepared_root_cannot_bind(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            preflight, document = self.invoke(
                'preflight', '--scratch', scratch
            )
            self.assertEqual(preflight.returncode, 0, document)
            source_root = tempfile.mkdtemp(
                prefix='rhmra-source-', dir=tempfile.gettempdir()
            )
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
            self.assertIn(
                '.rhmra-broker-response-source-root-prepared.json',
                error['error']['message'],
            )
            self.assertFalse(os.path.exists(canary))
            self.assertFalse(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )))

    def test_bind_rejects_alternate_prepared_root_and_consumes_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            prepared_root = self.source_root(scratch)
            alternate_root = tempfile.mkdtemp(
                prefix='rhmra-source-', dir=tempfile.gettempdir()
            )
            self.addCleanup(shutil.rmtree, alternate_root, True)
            canary = self.write_json(
                alternate_root, 'accounts.json', self.valid_accounts_document()
            )
            rejected, error = self.invoke(
                'bind-transport', '--scratch', scratch,
                '--source-root', alternate_root, '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                'must exactly equal the helper-prepared source_root',
                error['error']['message'],
            )
            self.assertFalse(os.path.exists(canary))
            with open(
                os.path.join(
                    scratch,
                    '.rhmra-broker-response-transport-attempt.json',
                ),
                encoding='utf-8',
            ) as handle:
                attempt = json.load(handle)
            self.assertEqual(attempt['source_root'], prepared_root)

    def test_bind_rejects_recreated_prepared_root_instance(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            source_root = self.source_root(scratch)
            shutil.rmtree(source_root)
            os.mkdir(source_root)
            canary = self.write_json(
                source_root, 'accounts.json', self.valid_accounts_document()
            )
            rejected, error = self.invoke(
                'bind-transport', '--scratch', scratch,
                '--source-root', source_root, '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                'prepared response-source root instance changed',
                error['error']['message'],
            )
            self.assertFalse(os.path.exists(canary))
            self.assertTrue(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )))

    def test_bind_rejects_tampered_prepared_root_identity(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            source_root = self.source_root(scratch)
            marker_path = os.path.join(
                scratch,
                '.rhmra-broker-response-source-root-prepared.json',
            )
            with open(marker_path, encoding='utf-8') as handle:
                marker = json.load(handle)
            marker['source_root_inode'] = str(
                int(marker['source_root_inode']) + 1
            )
            with open(marker_path, 'wb') as handle:
                handle.write(broker_snapshot_module._canonical_bytes(marker))
            canary = self.write_json(
                source_root, 'accounts.json', self.valid_accounts_document()
            )
            rejected, error = self.invoke(
                'bind-transport', '--scratch', scratch,
                '--source-root', source_root, '--canary', canary,
                '--account-name', 'Agentic',
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn(
                'prepared response-source root instance changed',
                error['error']['message'],
            )
            self.assertFalse(os.path.exists(canary))
            self.assertTrue(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport-attempt.json'
            )))

    def test_path_validation_failures_consume_attempt_and_cleanup_canary(self):
        for layout in ('nested-root', 'nested-canary', 'extra-entry'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch, bind_transport=False)
                outer = self.source_root(scratch)
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
            source_root = self.source_root(scratch)
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
                source_root,
                'sensitive-account-get-accounts-canary.json',
                canary_document,
            )
            canary_stat = os.lstat(canary)
            expected_canary_instance = (
                broker_snapshot_module._transport_canary_instance(
                    Path(canary), canary_stat
                )
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
            self.assertEqual(
                marker_document['source_root_id'],
                self.source_root_id(scratch),
            )
            self.assertEqual(marker_document['canary_sha256'], expected_digest)
            attempt_marker_path = os.path.join(
                scratch,
                '.rhmra-broker-response-transport-attempt.json',
            )
            with open(attempt_marker_path, encoding='utf-8') as handle:
                attempt_marker = json.load(handle)
            self.assertEqual(
                set(attempt_marker),
                {
                    'schema_version', 'marker', 'scratch_id', 'transport',
                    'source_root', 'canary_instance',
                },
            )
            self.assertEqual(
                attempt_marker['canary_instance'], expected_canary_instance
            )
            persistent_marker_paths = [
                os.path.join(
                    scratch,
                    '.rhmra-broker-response-source-root-prepared.json',
                ),
                attempt_marker_path,
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
            source_root = self.source_root(scratch)
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
                source_root = self.source_root(scratch)
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
                self.assertEqual(
                    error['error']['code'], 'account_scope_failed'
                )
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

            source_root = self.source_root(scratch)
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
                if generation == 'B':
                    retry_source = self.write_json(
                        source_root,
                        'authorize-generation-b.json',
                        {'data': {'total_value': '1500.01'}},
                    )
                    retry_output = os.path.join(
                        scratch, 'authorize-generation-b-out.json'
                    )
                    failed, failure = self.stage(
                        'portfolio', [retry_source], [retry_output],
                        generation='A',
                    )
                    self.assertNotEqual(failed.returncode, 0, failure)
                    self.assertEqual(
                        failure['error']['code'], 'stage_semantic_invalid'
                    )
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
            with open(marker_path, 'w', encoding='utf-8') as handle:
                json.dump(
                    {
                        'schema_version': 1,
                        'marker': 'rhmra-broker-response-source-root',
                        'scratch_id': scratch_id,
                        'source_root_id': '00000000-0000-4000-8000-000000000001',
                    },
                    handle,
                )
            source = os.path.join(source_root, 'scan.json')
            with open(source, 'w', encoding='utf-8') as handle:
                json.dump({'data': {}}, handle)
            with self.assertRaises(SnapshotError):
                validate_bound_external_json_source(scratch, source)

    def test_concurrent_bind_attempts_produce_exactly_one_bound_transport(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch, bind_transport=False)
            source_root = self.source_root(scratch)
            canary = self.write_json(
                source_root, 'accounts.json', self.valid_accounts_document()
            )

            def bind(_index):
                return self.invoke(
                    'bind-transport',
                    '--scratch', scratch,
                    '--source-root', source_root,
                    '--canary', canary,
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
            self.assertFalse(os.path.exists(canary))
            self.assertTrue(os.path.exists(os.path.join(
                scratch, '.rhmra-broker-response-transport.json'
            )))

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

    def auto_stage(
        self, kind, scratch, sources, *extra, generation='A', by_purpose=False
    ):
        args = ['stage', '--kind', kind, '--generation', generation]
        source_flag = '--source-purpose' if by_purpose else '--source'
        for source in sources:
            args += [source_flag, source]
        args += ['--auto-output-scratch', scratch, *extra]
        return self.invoke(*args)

    def test_helper_allocates_fresh_stage_outputs_for_committed_purposes(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-discovery-positions-0',
                {'data': {'positions': [], 'next': None}},
            )

            proc, receipt = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-a-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                by_purpose=True,
            )

            self.assertEqual(proc.returncode, 0, (receipt, proc.stderr))
            self.assertEqual(receipt['output_mode'], 'helper-allocated')
            self.assertEqual(receipt['file_count'], 1)
            output = receipt['output_paths'][0]
            self.assertEqual(os.path.dirname(output), os.path.abspath(scratch))
            self.assertRegex(
                os.path.basename(output),
                r'^rhmra-stage-a-positions-[0-9a-f-]{36}-1\.json$',
            )
            self.assertTrue(os.path.isfile(output))
            self.assertTrue(os.path.isfile(output + '.rhmra-stage.json'))
            self.assertEqual(receipt['files'][0]['output'], output)
            self.assertEqual(
                receipt['files'][0]['source_purpose'],
                'daily-loss-a-discovery-positions-0',
            )

    def test_helper_allocates_aggregate_outputs_for_staged_sources(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            source_root = self.source_root(scratch)
            next_url = 'https://agent.robinhood.com/orders?cursor=two'
            source_one = self.write_json(
                source_root,
                'orders-one.json',
                {'data': {'orders': [], 'next': next_url}},
            )
            source_two = self.write_json(
                source_root,
                'orders-two.json',
                {'data': {'orders': [], 'next': None}},
            )
            first_proc, first = self.auto_stage(
                'orders', scratch, [source_one],
                '--request-cursor', 'FIRST', '--allow-more',
            )
            second_proc, second = self.auto_stage(
                'orders', scratch, [source_two],
                '--request-cursor', 'two',
            )
            self.assertEqual(first_proc.returncode, 0, first)
            self.assertEqual(second_proc.returncode, 0, second)

            sealed_proc, sealed = self.auto_stage(
                'orders',
                scratch,
                [first['output_paths'][0], second['output_paths'][0]],
                '--request-cursor', 'FIRST',
                '--request-cursor', 'two',
            )
            self.assertEqual(sealed_proc.returncode, 0, sealed)
            self.assertEqual(sealed['output_mode'], 'helper-allocated')
            self.assertEqual(sealed['file_count'], 2)
            self.assertEqual(len(set(sealed['output_paths'])), 2)
            self.assertTrue(
                all(
                    os.path.dirname(path) == os.path.abspath(scratch)
                    for path in sealed['output_paths']
                )
            )
            validate_generation_inputs(
                {'orders': sealed['output_paths']}, 'A'
            )

    def test_stage_error_codes_preserve_transport_and_separate_other_phases(self):
        with self.subTest(phase='input'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            reserved, _committed = self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            os.unlink(reserved['source'])
            proc, error = self.auto_stage(
                'portfolio',
                scratch,
                ['daily-loss-a-final-portfolio-0'],
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'source_file_missing')
            self.assertEqual(error['kind'], 'portfolio')
            self.assertEqual(error['generation'], 'A')
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

        with self.subTest(phase='journal'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            pending = self.reserve_source(
                scratch, 'daily-loss-a-final-portfolio-1'
            )
            proc, error = self.auto_stage(
                'portfolio',
                scratch,
                ['daily-loss-a-final-portfolio-0'],
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'source_handoff_pending')
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))
            proc, aborted = self.invoke(
                'abort-source', '--scratch', scratch,
                '--purpose', 'daily-loss-a-final-portfolio-1',
                '--reservation-id', pending['reservation_id'],
                '--reason', 'connector-failed',
            )
            self.assertEqual(proc.returncode, 0, aborted)

        with self.subTest(phase='changed-input'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            reserved, _committed = self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                json.dump(
                    {'data': {
                        'total_value': '2', 'cash': '2', 'buying_power': '2'
                    }},
                    handle,
                    separators=(',', ':'),
                    sort_keys=True,
                )
                handle.write('\n')
            proc, error = self.auto_stage(
                'portfolio', scratch,
                ['daily-loss-a-final-portfolio-0'], by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'source_file_changed')

        with self.subTest(phase='invalid-input'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            reserved, _committed = self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            with open(
                reserved['source'], 'w', encoding='utf-8', newline='\n'
            ) as handle:
                handle.write('not-json\n')
            proc, error = self.auto_stage(
                'portfolio', scratch,
                ['daily-loss-a-final-portfolio-0'], by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'source_file_invalid')

        with self.subTest(phase='response'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {
                    'isError': True,
                    'structuredContent': {
                        'data': {
                            'total_value': '1',
                            'cash': '1',
                            'buying_power': '1',
                        }
                    },
                },
            )
            proc, error = self.auto_stage(
                'portfolio', scratch,
                ['daily-loss-a-final-portfolio-0'], by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'stage_response_invalid')
            self.assertEqual(error['kind'], 'portfolio')
            self.assertEqual(error['generation'], 'A')
            self.assertEqual(
                set(error),
                {'schema_version', 'action', 'ok', 'kind', 'generation', 'error'},
            )
            self.assertEqual(set(error['error']), {'code', 'message'})
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

        with self.subTest(phase='atomic-link'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            stdout = io.StringIO()
            with mock.patch.object(
                broker_snapshot_module.os,
                'link',
                side_effect=OSError('simulated atomic-link failure'),
            ), redirect_stdout(stdout):
                status = broker_snapshot_module.main(
                    [
                        'stage', '--kind', 'portfolio', '--generation', 'A',
                        '--source-purpose', 'daily-loss-a-final-portfolio-0',
                        '--auto-output-scratch', scratch,
                    ]
                )
            self.assertEqual(status, 2)
            error = json.loads(stdout.getvalue())
            self.assertEqual(error['error']['code'], 'stage_write_failed')
            self.assertFalse(any(
                name.startswith('rhmra-stage-') or name.endswith('.tmp')
                for name in os.listdir(scratch)
            ))

        with self.subTest(phase='internal'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            allocation_id = uuid.UUID('11111111-1111-4111-8111-111111111111')
            stdout = io.StringIO()
            with mock.patch.object(
                broker_snapshot_module.uuid,
                'uuid4',
                side_effect=[
                    allocation_id,
                    OSError('simulated helper metadata allocation failure'),
                ],
            ), redirect_stdout(stdout):
                status = broker_snapshot_module.main(
                    [
                        'stage', '--kind', 'portfolio', '--generation', 'A',
                        '--source-purpose', 'daily-loss-a-final-portfolio-0',
                        '--auto-output-scratch', scratch,
                    ]
                )
            self.assertEqual(status, 2)
            error = json.loads(stdout.getvalue())
            self.assertEqual(error['error']['code'], 'stage_internal_failed')
            self.assertEqual(
                set(error),
                {
                    'schema_version', 'action', 'ok', 'kind', 'generation',
                    'error',
                },
            )
            self.assertEqual(set(error['error']), {'code', 'message'})
            self.assertFalse(os.path.exists(os.path.join(
                scratch, broker_snapshot_module.STAGE_RETRY_MARKER
            )))
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

        with self.subTest(phase='semantic'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-discovery-positions-0',
                {'data': {'positions': 'not-an-array', 'next': None}},
            )
            proc, error = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-a-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'stage_semantic_invalid')
            self.assertEqual(error['recovery_action'], 'generation-b')
            self.assertTrue(os.path.isfile(os.path.join(
                scratch, broker_snapshot_module.STAGE_RETRY_MARKER
            )))
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

            self.reserve_write_commit(
                scratch,
                'daily-loss-b-discovery-positions-0',
                {'data': {'positions': 'still-not-an-array', 'next': None}},
            )
            proc, error = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-b-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                generation='B',
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'stage_semantic_invalid')
            self.assertEqual(
                error['recovery_action'], 'snapshot-second-attempt-failed'
            )
            self.assertTrue(os.path.isfile(os.path.join(
                scratch, broker_snapshot_module.STAGE_RETRY_EXHAUSTED_MARKER
            )))

            proc, error = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-b-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                generation='B',
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                error['error']['code'], 'stage_retry_state_failed'
            )
            self.assertEqual(
                set(error),
                {
                    'schema_version', 'action', 'ok', 'kind', 'generation',
                    'error',
                },
            )
            self.assertEqual(set(error['error']), {'code', 'message'})

            proc, error = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-a-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                generation='A',
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                error['error']['code'], 'stage_retry_state_failed'
            )

        with self.subTest(phase='unauthorized-b'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            proc, error = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'daily-loss-b-final-portfolio-0',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                error['error']['code'], 'stage_retry_state_failed'
            )
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': 'not-a-number'}},
            )
            proc, authorized = self.invoke(
                'authorize-generation-b', '--scratch', scratch
            )
            self.assertEqual(proc.returncode, 0, authorized)
            self.assertEqual(authorized['state'], 'generation-b-authorized')
            self.assertFalse(authorized['idempotent'])
            proc, repeated = self.invoke(
                'authorize-generation-b', '--scratch', scratch
            )
            self.assertEqual(proc.returncode, 0, repeated)
            self.assertTrue(repeated['idempotent'])
            proc, stale_a = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'daily-loss-a-final-portfolio-0',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                stale_a['error']['code'], 'stage_retry_state_failed'
            )

            self.reserve_write_commit(
                scratch,
                'daily-loss-b-final-portfolio-0',
                {'data': {
                    'total_value': '1', 'cash': '1', 'buying_power': '1'
                }},
            )
            proc, staged = self.auto_stage(
                'portfolio', scratch,
                ['daily-loss-b-final-portfolio-0'],
                generation='B', by_purpose=True,
            )
            self.assertEqual(proc.returncode, 0, staged)
            proc, finished = self.invoke(
                'finish-generation-b', '--scratch', scratch,
                '--outcome', 'completed',
            )
            self.assertEqual(proc.returncode, 0, finished)
            self.assertFalse(finished['idempotent'])
            proc, repeated_finish = self.invoke(
                'finish-generation-b', '--scratch', scratch,
                '--outcome', 'completed',
            )
            self.assertEqual(proc.returncode, 0, repeated_finish)
            self.assertTrue(repeated_finish['idempotent'])
            proc, exhausted = self.invoke(
                'reserve-source', '--scratch', scratch,
                '--purpose', 'daily-loss-b-final-portfolio-1',
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(
                exhausted['error']['code'], 'stage_retry_state_failed'
            )
            self.assertTrue(os.path.isfile(staged['output_paths'][0]))

        with self.subTest(phase='binding'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-discovery-positions-0',
                {'data': {'positions': [], 'next': None}},
            )
            proc, error = self.auto_stage(
                'positions',
                scratch,
                ['daily-loss-a-discovery-positions-0'],
                '--request-cursor',
                'FIRST',
                '--allow-more',
                by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'stage_binding_invalid')
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

        with self.subTest(phase='write'), tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            stdout = io.StringIO()
            with mock.patch.object(
                broker_snapshot_module.tempfile,
                'mkstemp',
                side_effect=OSError('simulated stage write failure'),
            ), redirect_stdout(stdout):
                status = broker_snapshot_module.main(
                    [
                        'stage', '--kind', 'portfolio', '--generation', 'A',
                        '--source-purpose', 'daily-loss-a-final-portfolio-0',
                        '--auto-output-scratch', scratch,
                    ]
                )
            self.assertEqual(status, 2)
            error = json.loads(stdout.getvalue())
            self.assertEqual(error['error']['code'], 'stage_write_failed')
            self.assertFalse(any(
                name.startswith('rhmra-stage-')
                for name in os.listdir(scratch)
            ))

    def test_auto_output_requires_exact_bound_scratch_and_excludes_output(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            wrong = os.path.join(td, 'wrong')
            os.mkdir(scratch)
            os.mkdir(wrong)
            self.preflight(scratch)
            source = self.write_json(
                self.source_root(scratch),
                'portfolio.json',
                {'data': {'total_value': '1', 'cash': '1', 'buying_power': '1'}},
            )
            proc, rejected = self.auto_stage(
                'portfolio', wrong, [source]
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(rejected['ok'])
            self.assertEqual(rejected['error']['code'], 'stage_input_failed')

            explicit = os.path.join(scratch, 'explicit.json')
            proc, rejected = self.invoke(
                'stage', '--kind', 'portfolio', '--generation', 'A',
                '--source', source, '--output', explicit,
                '--auto-output-scratch', scratch,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(rejected['error']['code'], 'usage_error')

            proc, rejected = self.auto_stage(
                'portfolio', 'relative-scratch', [source]
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertFalse(rejected['ok'])
            self.assertEqual(rejected['error']['code'], 'stage_input_failed')
            self.assertIn(
                'scratch directory must be an absolute path',
                rejected['error']['message'],
            )

    def test_daily_loss_purpose_namespace_and_stage_binding_are_closed(self):
        valid = {
            'daily-loss-a-discovery-positions-0': ('A', 'positions'),
            'daily-loss-a-discovery-orders-999': ('A', 'orders'),
            'daily-loss-a-marks-quotes-0': ('A', 'quotes'),
            'daily-loss-b-final-portfolio-999': ('B', 'portfolio'),
            'daily-loss-b-final-positions-0': ('B', 'positions'),
            'daily-loss-b-final-orders-999': ('B', 'orders'),
        }
        for purpose, expected in valid.items():
            with self.subTest(valid=purpose):
                self.assertEqual(
                    broker_snapshot_module._daily_loss_purpose_binding(purpose),
                    expected,
                )
        for purpose in (
            'daily-loss-c-final-portfolio-0',
            'daily-loss-a-final-quotes-0',
            'daily-loss-a-marks-portfolio-0',
            'daily-loss-a-final-portfolio-00',
            'daily-loss-a-final-portfolio-1000',
        ):
            with self.subTest(invalid=purpose), self.assertRaises(
                broker_snapshot_module.SourceHandoffError
            ) as caught:
                broker_snapshot_module._daily_loss_purpose_binding(purpose)
            self.assertEqual(caught.exception.code, 'source_purpose_invalid')

        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            self.reserve_write_commit(
                scratch,
                'daily-loss-a-final-portfolio-0',
                {'data': {
                    'total_value': '1', 'cash': '1', 'buying_power': '1'
                }},
            )
            proc, error = self.auto_stage(
                'quotes', scratch,
                ['daily-loss-a-final-portfolio-0'], by_purpose=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(error['error']['code'], 'stage_binding_invalid')
            self.assertFalse(os.path.exists(os.path.join(
                scratch, broker_snapshot_module.STAGE_RETRY_MARKER
            )))

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
            self.assertEqual(result['output_mode'], 'caller-supplied')
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
            malformed = (
                ('missing', {'display_currency': 'USD'}),
                ('null', {'buying_power': None}),
                ('object', {'buying_power': {'amount': '1508.9700'}}),
                ('nonfinite', {'buying_power': 'NaN'}),
                ('negative', {'buying_power': '-0.01'}),
            )
            for label, buying_power in malformed:
                scratch = os.path.join(td, label)
                os.mkdir(scratch)
                self.preflight(scratch)
                source_root = self.source_root(scratch)
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
                proc, result = self.stage(
                    'portfolio', [source], [output], generation='A',
                )
                with self.subTest(case=label):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(
                        result['error']['code'], 'stage_semantic_invalid'
                    )
                    self.assertIn(
                        'portfolio.data.buying_power.buying_power',
                        result['error']['message'],
                    )
                    self.assertFalse(os.path.exists(output))

    def test_portfolio_stage_rejects_missing_or_null_outer_buying_power(self):
        with tempfile.TemporaryDirectory() as td:
            malformed = (
                ('missing', {}),
                ('null', {'buying_power': None}),
            )
            for label, fields in malformed:
                scratch = os.path.join(td, label)
                os.mkdir(scratch)
                self.preflight(scratch)
                source_root = self.source_root(scratch)
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
                proc, result = self.stage(
                    'portfolio', [source], [output], generation='A',
                )
                with self.subTest(case=label):
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertEqual(
                        result['error']['code'], 'stage_semantic_invalid'
                    )
                    self.assertIn(
                        'portfolio.data.buying_power',
                        result['error']['message'],
                    )
                    self.assertFalse(os.path.exists(output))

    def test_non_paginated_stage_normalizes_inert_pagination_cursor_flags(self):
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
                ('empty-request-cursor', ('--request-cursor', '')),
                ('allow-more', ('--allow-more',)),
                (
                    'empty-request-cursor-and-allow-more',
                    ('--request-cursor', '', '--allow-more'),
                ),
            )
            for kind, payload in payloads.items():
                source = self.write_json(source_root, f'{kind}.json', payload)
                for label, extra in pagination_flags:
                    output = os.path.join(scratch, f'{kind}-{label}.json')
                    proc, result = self.stage(
                        kind, [source], [output], *extra
                    )
                    with self.subTest(kind=kind, flag=label):
                        self.assertEqual(proc.returncode, 0, result)
                        self.assertTrue(result['complete'])
                        self.assertNotIn(
                            'request_cursor', result['files'][0]
                        )
                        self.assertTrue(os.path.exists(output))
                        with open(
                            result['files'][0]['provenance'], encoding='utf-8'
                        ) as handle:
                            provenance = json.load(handle)
                        self.assertIsNone(provenance['request_cursor'])
                        self.assertTrue(provenance['set_complete'])

    def test_strict_json_and_malformed_semantic_shapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
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
                ('portfolio', 'duplicate.json', duplicate, True),
                ('portfolio', 'nonfinite.json', nonfinite, True),
                ('portfolio', 'malformed.json', malformed, True),
                (
                    'positions', 'positions-shape.json',
                    {'data': {'positions': {}, 'next': None}}, False,
                ),
                (
                    'orders', 'orders-shape.json',
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
                    }, False,
                ),
                (
                    'quotes', 'quotes-shape.json',
                    {'data': {'results': [{}] * 21}}, False,
                ),
                (
                    'portfolio', 'ambiguous-envelope.json',
                    {
                        'content': [
                            {'type': 'text', 'text': '{}'},
                            {'type': 'text', 'text': '{}'},
                        ]
                    }, False,
                ),
                (
                    'portfolio', 'tool-error.json',
                    {
                        'isError': True,
                        'structuredContent': {
                            'data': {'total_value': '1500.01'}
                        },
                    }, False,
                ),
                (
                    'portfolio', 'invalid-error-flag.json',
                    {
                        'isError': 'false',
                        'structuredContent': {
                            'data': {'total_value': '1500.01'}
                        },
                    }, False,
                ),
            ]
            for index, (kind, name, value, is_text) in enumerate(cases):
                scratch = os.path.join(td, f'case-{index}')
                os.mkdir(scratch)
                self.preflight(scratch)
                source_root = self.source_root(scratch)
                source = (
                    self.write_text(source_root, name, value)
                    if is_text
                    else self.write_json(source_root, name, value)
                )
                output = os.path.join(scratch, f'rejected-{index}.json')
                with self.subTest(kind=kind, source=os.path.basename(source)):
                    proc, result = self.stage(
                        kind, [source], [output], generation='A'
                    )
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertFalse(result['ok'])
                    expected_code = (
                        'source_unregistered'
                        if index < 3
                        else 'stage_response_invalid'
                        if index >= 6
                        else 'stage_semantic_invalid'
                    )
                    self.assertEqual(result['error']['code'], expected_code)
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
            self.assertEqual(
                sealed['output_paths'],
                [os.path.abspath(sealed_one), os.path.abspath(sealed_two)],
            )
            self.assertEqual(
                [item['output'] for item in sealed['files']],
                sealed['output_paths'],
            )
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

    def test_terminal_singleton_page_sets_need_no_second_aggregate_stage(self):
        with tempfile.TemporaryDirectory() as td:
            scratch = os.path.join(td, 'scratch')
            os.mkdir(scratch)
            self.preflight(scratch)
            source_root = self.source_root(scratch)
            positions_source = self.write_json(
                source_root,
                'positions-singleton.json',
                {'data': {'positions': [], 'next': None}},
            )
            orders_source = self.write_json(
                source_root,
                'orders-singleton.json',
                {'data': {'orders': [], 'next': None}},
            )
            positions_output = os.path.join(scratch, 'positions-singleton.json')
            orders_output = os.path.join(scratch, 'orders-singleton.json')

            positions_proc, positions_receipt = self.stage(
                'positions', [positions_source], [positions_output],
                '--request-cursor', 'FIRST',
            )
            orders_proc, orders_receipt = self.stage(
                'orders', [orders_source], [orders_output],
                '--request-cursor', 'FIRST',
            )
            self.assertEqual(positions_proc.returncode, 0, positions_receipt)
            self.assertEqual(orders_proc.returncode, 0, orders_receipt)
            for receipt, expected_output in (
                (positions_receipt, positions_output),
                (orders_receipt, orders_output),
            ):
                self.assertTrue(receipt['complete'])
                self.assertEqual(receipt['file_count'], 1)
                self.assertEqual(len(receipt['files']), 1)
                self.assertIsInstance(receipt['files'][0], dict)
                self.assertEqual(
                    receipt['output_paths'], [os.path.abspath(expected_output)]
                )
                self.assertEqual(
                    receipt['files'][0]['output'], receipt['output_paths'][0]
                )
                self.assertNotEqual(
                    receipt['files'][0], receipt['output_paths'][0]
                )

            validate_generation_inputs(
                {
                    'positions': [positions_output],
                    'orders': [orders_output],
                },
                'A',
            )
            symbols_output = os.path.join(scratch, 'symbols-singleton.json')
            proc = subprocess.run(
                [
                    sys.executable, DAILY_LOSS,
                    '--positions', positions_output,
                    '--orders', orders_output,
                    '--snapshot-generation', 'A',
                    '--trading-date', '2026-08-04',
                    '--stop-date-pt', '2026-08-04',
                    '--as-of-utc', '2026-08-04T19:00:00Z',
                    '--symbols-out', symbols_output,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(
                json.loads(proc.stdout),
                {
                    'schema_version': 1,
                    'action': 'discover-symbols',
                    'ok': True,
                    'trading_date_et': '2026-08-04',
                    'as_of_utc': '2026-08-04T19:00:00Z',
                    'symbol_count': 0,
                    'symbols': [],
                },
            )
            with open(symbols_output, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle), [])

    def test_daily_loss_failure_json_authorizes_only_typed_semantic_a_or_b(self):
        for generation, expected_recovery in (
            ('A', 'generation-b'),
            ('B', 'snapshot-second-attempt-failed'),
        ):
            with self.subTest(generation=generation), tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, 'scratch')
                os.mkdir(scratch)
                self.preflight(scratch)
                if generation == 'B':
                    self.reserve_write_commit(
                        scratch,
                        'daily-loss-a-discovery-positions-0',
                        {'data': {'positions': [], 'next': None}},
                    )
                    proc, authorization = self.invoke(
                        'authorize-generation-b', '--scratch', scratch
                    )
                    self.assertEqual(proc.returncode, 0, authorization)

                slug = generation.lower()
                positions_purpose = (
                    f'daily-loss-{slug}-discovery-positions-0'
                )
                orders_purpose = f'daily-loss-{slug}-discovery-orders-0'
                positions_document = DailyLossTests.page(
                    'positions',
                    [DailyLossTests.position('BAD', '10', '5')],
                )
                orders_document = DailyLossTests.page(
                    'orders',
                    [
                        DailyLossTests.order(
                            'order-buy',
                            'BAD',
                            'buy',
                            [
                                DailyLossTests.execution(
                                    'execution-buy', '10', '4'
                                )
                            ],
                        )
                    ],
                )
                self.reserve_write_commit(
                    scratch, positions_purpose, positions_document
                )
                self.reserve_write_commit(
                    scratch, orders_purpose, orders_document
                )
                positions_proc, positions_receipt = self.auto_stage(
                    'positions', scratch, [positions_purpose],
                    '--request-cursor', 'FIRST', generation=generation,
                    by_purpose=True,
                )
                orders_proc, orders_receipt = self.auto_stage(
                    'orders', scratch, [orders_purpose],
                    '--request-cursor', 'FIRST', generation=generation,
                    by_purpose=True,
                )
                self.assertEqual(
                    positions_proc.returncode, 0, positions_receipt
                )
                self.assertEqual(orders_proc.returncode, 0, orders_receipt)
                portfolio_purpose = f'daily-loss-{slug}-final-portfolio-0'
                self.reserve_write_commit(
                    scratch,
                    portfolio_purpose,
                    {
                        'data': {
                            'total_value': '1000',
                            'cash': '1000',
                            'equity_value': '0',
                            'buying_power': {'buying_power': '1000'},
                        }
                    },
                )
                portfolio_proc, portfolio_receipt = self.auto_stage(
                    'portfolio', scratch, [portfolio_purpose],
                    generation=generation, by_purpose=True,
                )
                self.assertEqual(
                    portfolio_proc.returncode, 0, portfolio_receipt
                )

                common = [
                    sys.executable, DAILY_LOSS,
                    '--positions', positions_receipt['output_paths'][0],
                    '--orders', orders_receipt['output_paths'][0],
                    '--snapshot-generation', generation,
                    '--trading-date', DailyLossTests.TRADING_DATE,
                    '--stop-date-pt', DailyLossTests.TRADING_DATE,
                    '--as-of-utc', DailyLossTests.AS_OF_UTC,
                    '--failure-json',
                ]
                cases = (
                    (
                        'discovery',
                        [
                            '--symbols-out',
                            os.path.join(scratch, f'symbols-{slug}.json'),
                        ],
                    ),
                    (
                        'calculation',
                        [
                            '--portfolio',
                            portfolio_receipt['output_paths'][0],
                            '--halt-pct', '5',
                            '--json-out',
                            os.path.join(scratch, f'daily-loss-{slug}.json'),
                        ],
                    ),
                )
                for expected_mode, mode_arguments in cases:
                    with self.subTest(
                        generation=generation, mode=expected_mode
                    ):
                        proc = subprocess.run(
                            [*common, *mode_arguments],
                            cwd=ROOT,
                            capture_output=True,
                            text=True,
                        )
                        self.assertEqual(proc.returncode, 2, proc.stderr)
                        self.assertEqual(proc.stderr, '')
                        receipt = json.loads(proc.stdout)
                        self.assertEqual(
                            set(receipt),
                            {
                                'schema_version', 'action', 'ok', 'mode',
                                'generation', 'error', 'recovery_action',
                            },
                        )
                        self.assertEqual(receipt['action'], 'daily-loss')
                        self.assertIs(receipt['ok'], False)
                        self.assertEqual(receipt['mode'], expected_mode)
                        self.assertEqual(receipt['generation'], generation)
                        self.assertEqual(
                            receipt['error']['code'],
                            'daily_loss_semantic_invalid',
                        )
                        self.assertEqual(
                            receipt['recovery_action'], expected_recovery
                        )
                        self.assertFalse(os.path.exists(mode_arguments[-1]))

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
            invalid_source = self.write_json(
                source_root,
                'invalid-a.json',
                {'data': {'positions': 'not-an-array', 'next': None}},
            )
            semantic_proc, semantic_error = self.stage(
                'positions', [invalid_source],
                [os.path.join(scratch, 'invalid-a-output.json')],
                '--request-cursor', 'FIRST', generation='A',
            )
            self.assertNotEqual(semantic_proc.returncode, 0)
            self.assertEqual(
                semantic_error['error']['code'], 'stage_semantic_invalid'
            )
            self.assertEqual(
                semantic_error['recovery_action'], 'generation-b'
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
                    '--stop-date-pt', '2026-08-04',
                    '--as-of-utc', '2026-08-04T19:00:00Z',
                    '--symbols-out', symbols,
                    '--failure-json',
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr, '')
            receipt = json.loads(proc.stdout)
            self.assertEqual(
                receipt['error']['code'], 'daily_loss_input_failed'
            )
            self.assertIn('generation B does not match A', receipt['error']['message'])
            self.assertNotIn('recovery_action', receipt)
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
        if now is not None:
            options = {}
            extra = list(extra)
            index = 0
            while index < len(extra):
                flag = extra[index]
                if not isinstance(flag, str) or not flag.startswith("--"):
                    self.fail(f"unsupported lifecycle test argument: {flag!r}")
                if index + 1 >= len(extra):
                    self.fail(f"missing lifecycle test value for {flag}")
                options[flag[2:].replace("-", "_")] = extra[index + 1]
                index += 2
            common = {
                "state_file": state_file,
                "projection_file": projection_file,
                "now_utc": now,
            }
            try:
                if action == "start":
                    document = lifecycle_module.start_invocation(
                        invocation_id=options.get("invocation_id"),
                        lock_file=options.get("lock_file"),
                        **common,
                    )
                elif action == "event":
                    document = lifecycle_module.record_event(
                        invocation_id=options.get("invocation_id"),
                        phase=options.get("phase"),
                        run_start_pt=options.get("run_start_pt"),
                        classification=options.get("classification", "running"),
                        reason_code=options.get("reason_code"),
                        **common,
                    )
                elif action == "finish":
                    document = lifecycle_module.finish_invocation(
                        invocation_id=options.get("invocation_id"),
                        classification=options.get("classification"),
                        phase=options.get("phase", "finished"),
                        reason_code=options.get("reason_code"),
                        report_file=options.get("report_file"),
                        status_file=options.get("status_file"),
                        report_dir=options.get(
                            "report_dir", lifecycle_module.DEFAULT_REPORT_DIR
                        ),
                        context_file=options.get(
                            "context_file", lifecycle_module.DEFAULT_CONTEXT_FILE
                        ),
                        lock_file=options.get("lock_file"),
                        **common,
                    )
                elif action == "acquire-bind-context":
                    document = lifecycle_module.acquire_and_bind_active_context(
                        invocation_id=options.get("invocation_id"),
                        context_file=options.get(
                            "context_file", lifecycle_module.DEFAULT_CONTEXT_FILE
                        ),
                        lock_file=options.get("lock_file"),
                        **common,
                    )
                elif action == "bind-context":
                    document = lifecycle_module.bind_active_context(
                        invocation_id=options.get("invocation_id"),
                        run_token=options.get("run_token"),
                        context_file=options.get(
                            "context_file", lifecycle_module.DEFAULT_CONTEXT_FILE
                        ),
                        lock_file=options.get("lock_file"),
                        **common,
                    )
                elif action == "recover-context":
                    document = lifecycle_module.recover_active_context(
                        context_file=options.get(
                            "context_file", lifecycle_module.DEFAULT_CONTEXT_FILE
                        ),
                        lock_file=options.get("lock_file"),
                        **common,
                    )
                else:
                    self.fail(
                        f"lifecycle test action {action!r} has no imported "
                        "clock-injection adapter"
                    )
                returncode = (
                    0
                    if document.get("ok") is True
                    else 2 if document.get("reason") == "active_run" else 1
                )
            except lifecycle_module.ProjectionPublishError as exc:
                returncode = 1
                document = {
                    "schema_version": lifecycle_module.SCHEMA_VERSION,
                    "action": exc.action,
                    "ok": False,
                    "recorded": True,
                    "invocation_id": exc.invocation_id,
                    "reason": "projection_publication_failed",
                    "detail": str(exc),
                }
            except lifecycle_module.LifecycleConflict as exc:
                returncode = 2
                document = {
                    "schema_version": lifecycle_module.SCHEMA_VERSION,
                    "action": action,
                    "ok": False,
                    "reason": "lifecycle_conflict",
                    "detail": str(exc),
                }
            except (
                lifecycle_module.LifecycleError,
                OSError,
                sqlite3.Error,
            ) as exc:
                returncode = 1
                document = {
                    "schema_version": lifecycle_module.SCHEMA_VERSION,
                    "action": action,
                    "ok": False,
                    "reason": "lifecycle_state_error",
                    "detail": str(exc),
                }
            stdout = json.dumps(document, allow_nan=False, sort_keys=True) + "\n"
            return subprocess.CompletedProcess(
                [action, *extra], returncode, stdout, ""
            ), document

        args = [
            sys.executable,
            RUN_LIFECYCLE,
            action,
            '--state-file',
            state_file,
            '--projection-file',
            projection_file,
        ]
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

    def test_start_cli_emits_exactly_one_json_stdout_line(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            proc, document = self.invoke(
                state_file,
                projection_file,
                'start',
            )

            self.assertEqual(proc.returncode, 0, (document, proc.stderr))
            self.assertEqual(proc.stderr, '')
            stdout_lines = [line for line in proc.stdout.splitlines() if line]
            self.assertEqual(len(stdout_lines), 1, proc.stdout)
            self.assertEqual(json.loads(stdout_lines[0]), document)
            self.assertEqual(document['action'], 'start')
            self.assertTrue(document['ok'])

    @staticmethod
    def windows_replace_error(winerror):
        error = PermissionError(f'simulated Windows replace error {winerror}')
        error.winerror = winerror
        return error

    def test_projection_replace_recovers_once_without_replaying_event(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = lifecycle_module.start_invocation(
                state_file=state_file,
                projection_file=projection_file,
                now_utc='2026-08-25T20:46:49Z',
            )
            real_replace = lifecycle_module.os.replace
            attempts = 0

            def transient_then_success(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise self.windows_replace_error(32)
                return real_replace(source, destination)

            with (
                mock.patch.object(lifecycle_module.os, 'name', 'nt'),
                mock.patch.object(
                    lifecycle_module.os,
                    'replace',
                    side_effect=transient_then_success,
                ),
                mock.patch.object(lifecycle_module.time, 'sleep') as delay,
            ):
                bound = lifecycle_module.record_event(
                    invocation_id=started['invocation_id'],
                    phase='preflight',
                    run_start_pt='2026-08-25T13:47:44-07:00',
                    state_file=state_file,
                    projection_file=projection_file,
                    now_utc='2026-08-25T20:48:01Z',
                )

            self.assertTrue(bound['ok'])
            self.assertEqual(bound['phase'], 'preflight')
            self.assertEqual(attempts, 2)
            delay.assert_called_once_with(
                lifecycle_module.PROJECTION_REPLACE_RETRY_DELAY_SECONDS
            )
            connection = sqlite3.connect(state_file)
            try:
                rows = connection.execute(
                    'SELECT event_type, phase FROM lifecycle_events '
                    'WHERE invocation_id = ? ORDER BY sequence',
                    (started['invocation_id'],),
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(rows, [('start', 'scheduled'), ('event', 'preflight')])
            projection = lifecycle_module.validate_current_projection_read_only(
                state_file, projection_file
            )
            self.assertEqual(projection['records'][0]['latest_phase'], 'preflight')

    def test_projection_replace_retry_exhaustion_is_terminal_and_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = lifecycle_module.start_invocation(
                state_file=state_file,
                projection_file=projection_file,
                now_utc='2026-08-25T20:46:49Z',
            )
            with open(projection_file, 'rb') as handle:
                original_projection = handle.read()
            replace_error = self.windows_replace_error(33)

            with (
                mock.patch.object(lifecycle_module.os, 'name', 'nt'),
                mock.patch.object(
                    lifecycle_module.os,
                    'replace',
                    side_effect=replace_error,
                ) as replace,
                mock.patch.object(lifecycle_module.time, 'sleep') as delay,
            ):
                with self.assertRaises(
                    lifecycle_module.ProjectionPublishError
                ) as raised:
                    lifecycle_module.record_event(
                        invocation_id=started['invocation_id'],
                        phase='preflight',
                        run_start_pt='2026-08-25T13:47:44-07:00',
                        state_file=state_file,
                        projection_file=projection_file,
                        now_utc='2026-08-25T20:48:01Z',
                    )

            self.assertIn('projection publication failed', str(raised.exception))
            self.assertEqual(replace.call_count, 2)
            delay.assert_called_once_with(
                lifecycle_module.PROJECTION_REPLACE_RETRY_DELAY_SECONDS
            )
            with open(projection_file, 'rb') as handle:
                self.assertEqual(handle.read(), original_projection)
            self.assertFalse(
                any(
                    name.startswith('.rhmra-run-lifecycle-')
                    for name in os.listdir(td)
                )
            )
            connection = sqlite3.connect(state_file)
            try:
                event_count = connection.execute(
                    'SELECT count(*) FROM lifecycle_events '
                    'WHERE invocation_id = ?',
                    (started['invocation_id'],),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(event_count, 2)

            lifecycle_module.publish_projection(state_file, projection_file)
            repaired = lifecycle_module.validate_current_projection_read_only(
                state_file, projection_file
            )
            self.assertEqual(repaired['records'][0]['latest_phase'], 'preflight')

    def test_projection_replace_does_not_retry_nontransient_error(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            started = lifecycle_module.start_invocation(
                state_file=state_file,
                projection_file=projection_file,
                now_utc='2026-08-25T20:46:49Z',
            )
            replace_error = self.windows_replace_error(2)

            with (
                mock.patch.object(lifecycle_module.os, 'name', 'nt'),
                mock.patch.object(
                    lifecycle_module.os,
                    'replace',
                    side_effect=replace_error,
                ) as replace,
                mock.patch.object(lifecycle_module.time, 'sleep') as delay,
            ):
                with self.assertRaises(lifecycle_module.ProjectionPublishError):
                    lifecycle_module.record_event(
                        invocation_id=started['invocation_id'],
                        phase='preflight',
                        run_start_pt='2026-08-25T13:47:44-07:00',
                        state_file=state_file,
                        projection_file=projection_file,
                        now_utc='2026-08-25T20:48:01Z',
                    )

            replace.assert_called_once()
            delay.assert_not_called()
            self.assertFalse(
                any(
                    name.startswith('.rhmra-run-lifecycle-')
                    for name in os.listdir(td)
                )
            )

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
            self.assertEqual(bound['classification'], 'running')
            self.assertEqual(bound['phase'], 'preflight')
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

    def test_acquire_bind_context_accepts_incident_uuid_and_keeps_token_private(self):
        self.assertEqual(
            lifecycle_module.DEFAULT_LOCK_FILE,
            lifecycle_module.run_lock_module.DEFAULT_LOCK_FILE,
        )
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            invocation_id = '7483a5a7-32e3-4c1f-bd8f-fa3ac30a2293'
            proc, started = self.invoke(
                state_file, projection_file, 'start',
                '--invocation-id', invocation_id,
                now='2026-08-04T16:00:00Z',
            )
            self.assertEqual(proc.returncode, 0, (started, proc.stderr))
            self.bind(state_file, projection_file, invocation_id)

            proc, receipt = self.invoke(
                state_file, projection_file, 'acquire-bind-context',
                '--invocation-id', invocation_id,
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )

            self.assertEqual(proc.returncode, 0, (receipt, proc.stderr))
            self.assertEqual(receipt['action'], 'acquire-bind-context')
            self.assertTrue(receipt['ok'])
            run_token = receipt['run_lock_token']
            self.assertEqual(str(uuid.UUID(run_token)), run_token)
            self.assertEqual(uuid.UUID(run_token).version, 4)
            context = receipt['context_receipt']
            self.assertEqual(context['action'], 'bind-context')
            self.assertEqual(context['invocation_id'], invocation_id)
            self.assertEqual(context['phase'], 'preflight')

            connection = sqlite3.connect(lock_file)
            try:
                owner = connection.execute(
                    'SELECT token FROM run_lease WHERE singleton = 1'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(owner, run_token)
            with open(context_file, encoding='utf-8') as handle:
                private_context = json.load(handle)
            self.assertNotIn(run_token, json.dumps(private_context))
            self.assertEqual(
                private_context['lease_token_sha256'],
                hashlib.sha256(run_token.encode()).hexdigest(),
            )

    def test_acquire_bind_context_reports_active_owner_without_token(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])
            self.write_active_lease(lock_file, 'existing-owner')

            proc, receipt = self.invoke(
                state_file, projection_file, 'acquire-bind-context',
                '--invocation-id', started['invocation_id'],
                '--context-file', context_file,
                '--lock-file', lock_file,
                now='2026-08-04T16:00:02Z',
            )

            self.assertEqual(proc.returncode, 2, (receipt, proc.stderr))
            self.assertEqual(
                receipt,
                {
                    'schema_version': 1,
                    'action': 'acquire-bind-context',
                    'ok': False,
                    'reason': 'active_run',
                    'holder': {
                        'acquired_at': '2026-08-04T16:00:01Z',
                        'renewed_at': '2026-08-04T16:00:01Z',
                        'expires_at': '2026-08-04T16:20:01Z',
                    },
                },
            )
            self.assertNotIn('token', json.dumps(receipt))
            self.assertFalse(os.path.exists(context_file))

    def test_acquire_bind_context_releases_lease_when_bind_fails(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])

            with mock.patch.object(
                lifecycle_module,
                '_atomic_write_context',
                side_effect=lifecycle_module.LifecycleError('simulated bind failure'),
            ):
                receipt = lifecycle_module.acquire_and_bind_active_context(
                    invocation_id=started['invocation_id'],
                    state_file=state_file,
                    projection_file=projection_file,
                    context_file=context_file,
                    lock_file=lock_file,
                    now_utc='2026-08-04T16:00:02Z',
                )

            self.assertEqual(
                receipt,
                {
                    'schema_version': 1,
                    'action': 'acquire-bind-context',
                    'ok': False,
                    'reason': 'bind_context_failed',
                    'lease_released': True,
                    'compensation_recorded': True,
                },
            )
            self.assertNotIn('token', json.dumps(receipt))
            connection = sqlite3.connect(lock_file)
            try:
                count = connection.execute(
                    'SELECT count(*) FROM run_lease'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)

    def test_acquire_bind_context_reports_unconfirmed_cleanup_without_token(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])

            with (
                mock.patch.object(
                    lifecycle_module,
                    'bind_active_context',
                    side_effect=lifecycle_module.LifecycleError(
                        'simulated bind failure'
                    ),
                ),
                mock.patch.object(
                    lifecycle_module.run_lock_module,
                    'release',
                    return_value={'schema_version': 1, 'action': 'release',
                                  'ok': False, 'reason': 'ownership_lost'},
                ),
            ):
                receipt = lifecycle_module.acquire_and_bind_active_context(
                    invocation_id=started['invocation_id'],
                    state_file=state_file,
                    projection_file=projection_file,
                    context_file=context_file,
                    lock_file=lock_file,
                    now_utc='2026-08-04T16:00:02Z',
                )

            self.assertEqual(
                receipt,
                {
                    'schema_version': 1,
                    'action': 'acquire-bind-context',
                    'ok': False,
                    'reason': 'bind_context_failed_release_unconfirmed',
                    'lease_released': False,
                    'compensation_recorded': False,
                },
            )
            self.assertNotIn('token', json.dumps(receipt))
            connection = sqlite3.connect(lock_file)
            try:
                count = connection.execute(
                    'SELECT count(*) FROM run_lease'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)
            self.assertFalse(os.path.exists(context_file))

    def test_acquire_bind_context_rejects_non_preflight_before_lock_access(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])
            lifecycle_module.record_event(
                invocation_id=started['invocation_id'],
                phase='position-management',
                state_file=state_file,
                projection_file=projection_file,
                now_utc='2026-08-04T16:00:02Z',
            )

            with self.assertRaisesRegex(
                lifecycle_module.LifecycleConflict,
                'requires a running preflight invocation',
            ):
                lifecycle_module.acquire_and_bind_active_context(
                    invocation_id=started['invocation_id'],
                    state_file=state_file,
                    projection_file=projection_file,
                    lock_file=lock_file,
                    now_utc='2026-08-04T16:00:03Z',
                )

            self.assertFalse(os.path.exists(lock_file))

    def test_acquire_bind_context_releases_if_phase_advances_during_bind(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])
            preflight = lifecycle_module.invocation_status(
                invocation_id=started['invocation_id'],
                state_file=state_file,
                projection_file=projection_file,
            )
            advanced = dict(preflight)
            advanced['phase'] = 'daily-loss'

            with mock.patch.object(
                lifecycle_module,
                'invocation_status',
                side_effect=[preflight, advanced],
            ):
                receipt = lifecycle_module.acquire_and_bind_active_context(
                    invocation_id=started['invocation_id'],
                    state_file=state_file,
                    projection_file=projection_file,
                    context_file=context_file,
                    lock_file=lock_file,
                    now_utc='2026-08-04T16:00:02Z',
                )

            self.assertEqual(
                receipt,
                {
                    'schema_version': 1,
                    'action': 'acquire-bind-context',
                    'ok': False,
                    'reason': 'bind_context_failed_compensation_unrecorded',
                    'lease_released': True,
                    'compensation_recorded': False,
                },
            )
            self.assertNotIn('token', json.dumps(receipt))
            connection = sqlite3.connect(lock_file)
            try:
                count = connection.execute(
                    'SELECT count(*) FROM run_lease'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)
            self.assertFalse(os.path.exists(context_file))

    def test_acquire_bind_context_releases_for_invalid_nested_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, 'lifecycle.sqlite3')
            projection_file = os.path.join(td, 'lifecycle.json')
            context_file = os.path.join(td, 'active-context.json')
            lock_file = os.path.join(td, 'lease.sqlite3')
            started = self.start(state_file, projection_file)
            self.bind(state_file, projection_file, started['invocation_id'])

            with mock.patch.object(
                lifecycle_module,
                'bind_active_context',
                return_value={
                    'schema_version': 1,
                    'action': 'bind-context',
                    'ok': True,
                    'invocation_id': started['invocation_id'],
                    'classification': 'running',
                    'phase': 'daily-loss',
                },
            ):
                receipt = lifecycle_module.acquire_and_bind_active_context(
                    invocation_id=started['invocation_id'],
                    state_file=state_file,
                    projection_file=projection_file,
                    context_file=context_file,
                    lock_file=lock_file,
                    now_utc='2026-08-04T16:00:02Z',
                )

            self.assertEqual(
                receipt['reason'],
                'bind_context_failed_compensation_unrecorded',
            )
            self.assertTrue(receipt['lease_released'])
            self.assertFalse(receipt['compensation_recorded'])
            self.assertNotIn('token', json.dumps(receipt))
            connection = sqlite3.connect(lock_file)
            try:
                count = connection.execute(
                    'SELECT count(*) FROM run_lease'
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)

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
                'position-management',
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
        if now is not None:
            timestamp = datetime.fromisoformat(
                now.replace("Z", "+00:00")
            ).timestamp()
            try:
                if action == "acquire":
                    document = run_lock_module.acquire(
                        lock_file=lock_file,
                        lease_seconds=lease_seconds,
                        now=timestamp,
                    )
                elif action == "renew":
                    document = run_lock_module.renew(
                        token,
                        lock_file=lock_file,
                        lease_seconds=lease_seconds,
                        now=timestamp,
                    )
                else:
                    document = run_lock_module.release(
                        token, lock_file=lock_file, now=timestamp
                    )
                returncode = (
                    0
                    if document["ok"]
                    else 2 if action == "acquire" else 3
                )
            except Exception as exc:
                returncode = 1
                document = {
                    "schema_version": run_lock_module.SCHEMA_VERSION,
                    "action": action,
                    "ok": False,
                    "reason": "coordination_state_error",
                    "detail": str(exc),
                }
            return subprocess.CompletedProcess(
                [action],
                returncode,
                json.dumps(document, allow_nan=False) + "\n",
                "",
            ), document

        args = [sys.executable, RUN_LOCK, action,
                "--lock-file", lock_file,
                "--lease-seconds", str(lease_seconds)]
        if token is not None:
            args += ["--token", token]
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
            action, *raw = args
            options = {
                raw[index][2:].replace("-", "_"): raw[index + 1]
                for index in range(0, len(raw), 2)
            }
            now_utc = options.get("now_utc")
            if action == "start":
                return lifecycle_module.start_invocation(
                    state_file=state_file,
                    projection_file=projection_file,
                    now_utc=now_utc,
                )
            if action == "event":
                return lifecycle_module.record_event(
                    invocation_id=options["invocation_id"],
                    phase=options["phase"],
                    run_start_pt=options.get("run_start_pt"),
                    state_file=state_file,
                    projection_file=projection_file,
                    now_utc=now_utc,
                )
            if action == "finish":
                return lifecycle_module.finish_invocation(
                    invocation_id=options["invocation_id"],
                    classification=options["classification"],
                    reason_code=options.get("reason_code"),
                    report_file=options.get("report_file"),
                    status_file=options.get("status_file"),
                    report_dir=options.get("report_dir", report_dir),
                    state_file=state_file,
                    projection_file=projection_file,
                    now_utc=now_utc,
                )
            if action == "export":
                document = lifecycle_module.publish_projection(
                    state_file, projection_file
                )
                return {
                    "record_count": document["record_count"],
                    "source_event_high_watermark": document[
                        "source_event_high_watermark"
                    ],
                }
            self.fail(f"unsupported dashboard lifecycle action: {action}")

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
            "attempts that reached lifecycle; the label shows its outcome; "
            "* opens validated details",
            dashboard,
        )
        self.assertIn('function candidateEvaluationFailure(phase, reason)', dashboard)
        self.assertIn('parts.push("evaluation failure")', dashboard)
        self.assertIn('const STALE_RUNNING_AFTER_MS = 30 * 60 * 1000', dashboard)
        self.assertIn('function staleRunningLifecycle(record, nowMs = Date.now())', dashboard)
        self.assertIn('return "unfinished lifecycle"', dashboard)
        self.assertIn('staleRunning ? "missing terminal event"', dashboard)

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
    def invoke(self, args, *, now_utc):
        return run_imported_main(
            market_clock_module.main, args, now_utc=now_utc
        )

    def clock(self, now_utc, blackout=0):
        args = ["--json"]
        if blackout:
            args += ["--no-buy-first-minutes", str(blackout)]
        proc = self.invoke(args, now_utc=now_utc)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_cli_rejects_clock_override_but_imported_tests_can_inject_it(self):
        rejected = subprocess.run(
            [
                sys.executable,
                CLOCK,
                "--now-utc",
                "2026-07-21T15:07:00Z",
                "--json",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertEqual(rejected.stdout, "")
        self.assertIn("not valid on the CLI", rejected.stderr)
        self.assertEqual(
            self.clock("2026-07-21T15:07:00Z")["utc"],
            "2026-07-21T15:07:00Z",
        )

    def test_summer_offsets_are_daylight(self):
        # 2026-07-21 15:07Z — the run that had to improvise a clock.
        c = self.clock("2026-07-21T15:07:00Z")
        self.assertIs(type(c["schema_version"]), int)
        self.assertEqual(c["schema_version"], 1)
        self.assertEqual(c["et"], "2026-07-21 11:07:00 EDT")
        self.assertEqual(c["pt"], "2026-07-21 08:07:00 PDT")
        self.assertEqual(c["pt_iso"], "2026-07-21T08:07:00-07:00")
        self.assertEqual(c["date_et"], "2026-07-21")
        self.assertEqual(c["date_pt"], "2026-07-21")
        self.assertEqual(
            c["historicals_start_time"], "2026-06-16T15:07:00.000Z"
        )
        self.assertEqual(c["session"], "regular")
        self.assertEqual(c["calendar_status"], "normal")
        self.assertEqual(c["regular_close_et"], "16:00")
        self.assertIs(type(c["entry_session_open"]), bool)
        self.assertTrue(c["entry_session_open"])
        self.assertEqual(c["minutes_since_open"], 97)
        self.assertRegex(c["constants_sha256"], r"^[0-9a-f]{64}$")

    def test_historicals_start_time_expands_with_configured_lookback(self):
        with open(os.path.join(ROOT, "constants.md"), encoding="utf-8") as f:
            constants_text = f.read()
        constants_text = ConstantsValidatorTests.replace_value(
            constants_text, "VOLUME_LOOKBACK_DAYS", "40"
        )
        with tempfile.TemporaryDirectory() as td:
            constants = os.path.join(td, "constants.md")
            with open(constants, "w", encoding="utf-8") as f:
                f.write(constants_text)
            result = self.invoke(
                ["--constants", constants, "--json"],
                now_utc="2026-07-21T15:07:00Z",
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["historicals_start_time"],
            "2026-05-18T15:07:00.000Z",
        )

        long_text = ConstantsValidatorTests.replace_value(
            constants_text, "VOLUME_LOOKBACK_DAYS", "180"
        )
        with tempfile.TemporaryDirectory() as td:
            constants = os.path.join(td, "constants.md")
            with open(constants, "w", encoding="utf-8") as f:
                f.write(long_text)
            long_result = self.invoke(
                ["--constants", constants, "--json"],
                now_utc="2028-12-29T18:00:00Z",
            )
        self.assertEqual(long_result.returncode, 0, long_result.stderr)
        self.assertEqual(
            json.loads(long_result.stdout)["historicals_start_time"],
            "2028-04-06T18:00:00.000Z",
        )

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
            started = lifecycle_module.start_invocation(
                state_file=state_file,
                projection_file=projection_file,
                now_utc=clock['utc'],
            )
            bound = lifecycle_module.record_event(
                invocation_id=started['invocation_id'],
                phase='preflight',
                run_start_pt=clock['pt_iso'],
                state_file=state_file,
                projection_file=projection_file,
                now_utc=clock['utc'],
            )
            self.assertEqual(bound['run_start_pt'], clock['pt_iso'])

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
            r = self.invoke(
                ["--constants", os.path.join(td, "nope.md"), "--json"],
                now_utc="2026-07-22T13:37:00Z",
            )
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
                    r = self.invoke(
                        ["--constants", constants, "--json"],
                        now_utc="2026-07-22T13:37:00Z",
                    )
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
            result = self.invoke(
                ["--constants", constants, "--json"],
                now_utc="2026-07-22T13:37:00Z",
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
            oversized = self.invoke(
                ["--constants", constants, "--json"],
                now_utc="2026-07-22T13:37:00Z",
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
                "--constants",
                constants,
                "--expected-constants-sha256",
                expected_hash,
                "--json",
            ]
            matching = self.invoke(
                command, now_utc="2026-07-22T13:37:00Z"
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
            changed = self.invoke(
                command, now_utc="2026-07-22T13:37:00Z"
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

    def test_routine_uses_the_versioned_clock_receipt_contract(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()
        clock_contract = routine.split(
            "### CURRENT TIME — capture run start; re-check only at named "
            "safety boundaries",
            1,
        )[1].split("### RUN COORDINATION — fenced single-flight lease", 1)[0]

        self.assertIn(
            "`schema_version` exactly the JSON integer `1`", clock_contract
        )
        self.assertIn(
            "The checked-in producer owns this complete clock schema",
            clock_contract,
        )
        self.assertIn(
            "any other property not named here", clock_contract
        )
        self.assertIn(
            "a CONFIGURATION HALT, never a generic clock failure",
            clock_contract,
        )
        self.assertIn(
            "`date_pt`, `historicals_start_time`, `constants_sha256`",
            clock_contract,
        )
        self.assertIn(
            "deterministic `historicals_start_time` computed from the larger "
            "configured bar lookback",
            clock_contract,
        )
        self.assertIn(
            "None of these later readings changes START CLOCK's historicals "
            "window",
            clock_contract,
        )

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
        self.assertIn('Cowork/local-agent', bootstrap_contract)
        self.assertIn(
            'pause or disable that legacy Claude task and leave it disabled',
            bootstrap_contract,
        )
        self.assertIn(
            'do not create or enable a replacement Claude schedule',
            bootstrap_contract,
        )
        self.assertIn(
            'recommended Codex runner on the exact native Windows main '
            'checkout',
            bootstrap_contract,
        )
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
        self.assertIn("go directly to the checked-in Windows resolver", bootstrap)
        self.assertIn("no preliminary framework dependency lookup or hint argument", bootstrap)
        self.assertIn("identical for Codex, Claude", bootstrap)
        self.assertNotIn("load_workspace_dependencies", bootstrap)
        self.assertNotIn("PreferredPath", bootstrap)
        self.assertIn("-File ./resolve_python.ps1", bootstrap)
        self.assertIn("forward-slash `./resolve_python.ps1`", bootstrap)
        self.assertIn("works in both PowerShell and native Git Bash", bootstrap)
        self.assertNotIn("-File .\\resolve_python.ps1", bootstrap)
        self.assertIn("A valid resolver receipt ends launcher resolution immediately", bootstrap)
        self.assertIn("returned `python` field is already launch-probed", bootstrap)
        self.assertIn("Bind it without comparing it to another path", bootstrap)
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

        hinted = subprocess.run(
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
        self.assertEqual(hinted.returncode, 0, hinted.stderr)
        hinted_document = json.loads(hinted.stdout)
        self.assertEqual(hinted_document["schema_version"], 1)
        self.assertEqual(hinted_document["status"], "valid")
        self.assertTrue(os.path.isabs(hinted_document["python"]))
        self.assertNotIn(
            "microsoft\\windowsapps", hinted_document["python"].lower()
        )
        self.assertEqual(hinted_document["version"].split(".", 1)[0], "3")

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
                "-ExecutionPolicy Bypass -File ./resolve_python.ps1"
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
                'market_calendar.py', 'run_lock.py', 'validate_constants.py',
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

    def test_routine_uses_owner_fenced_second_guard_contract(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as handle:
            routine = handle.read()

        second = routine.split("**SECOND — circuit breaker check**", 1)[1].split(
            "**THIRD — build this run", 1
        )[0]
        enter_code = second.split(
            "**CODEX ENTER SECOND — EXACT OWNER-FENCED CELL:**", 1
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        for required in (
            'run_lifecycle.py enter-second --invocation-id ',
            '" --run-token " + quote(lease.run_lock_token)',
            '" --scratch " + quote(preflightReceipt.scratch)',
            '" --expected-constants-sha256 " + '
            'quote(constantsReceipt.source_sha256)',
            'receipt.phase !== "daily-loss"',
            'receipt.entry_eligible !== true',
            'receipt.daily_loss_attempted !== true',
            'receipt.scratch_id !== preflightReceipt.scratch_id',
            'receipt.source_root_id !== preflightReceipt.source_root_id',
            'receipt.constants_sha256 !== constantsReceipt.source_sha256',
            'receipt.daily_loss_halt_pct !== '
            'constantsReceipt.values.DAILY_LOSS_HALT_PCT',
            'receipt.stop_count_halt !== '
            'constantsReceipt.values.STOP_COUNT_HALT',
            'receipt.lease_renewed !== true',
            'second_entry_receipt: receipt',
        ):
            self.assertIn(required, enter_code)
        self.assertNotIn("run_lock.py renew", enter_code)
        self.assertNotIn("run_lifecycle.py event", enter_code)

        daily_loss = routine.split(
            "### DAILY-LOSS CIRCUIT BREAKER", 1
        )[1].split("### RUN THESE STEPS IN ORDER", 1)[0]
        command_line = next(
            line for line in daily_loss.splitlines()
            if "daily_loss.py --portfolio" in line
        )
        for required in (
            "--snapshot-generation <A|B>",
            "--expected-constants-sha256 "
            "<startup constants_receipt.source_sha256>",
            "--invocation-id <INVOCATION_ID>",
            "--run-token <private exact RUN_LOCK_TOKEN>",
            "--json-out <exact bound scratch>/daily-loss-<a|b>.json",
            "--failure-json",
        ):
            self.assertIn(required, command_line)
        command_invocation = command_line.split("`; POSIX-style shell", 1)[0]
        self.assertNotIn("--halt-pct", command_invocation)
        for required in (
            "Require exactly these top-level fields: `schema_version`, "
            "`action`, `ok`, `mode`, `generation`, `invocation_id`, "
            "`constants_sha256`, `daily_loss_halt_pct`, `stop_count_halt`, "
            "`daily_loss_tripped`, `stop_count_tripped`, "
            "`entry_guard_outcome`, and `result`",
            "the runner never selects or records its clear/tripped checkpoint",
            "make **exactly one** retry of the byte-identical "
            "lifecycle-bound command",
            "do not call public `complete-second` to manufacture a "
            "conflicting terminal",
            "daily loss has deterministic precedence when both are true",
        ):
            self.assertIn(required, routine)

        public_completion = second.split(
            "**PUBLIC FAIL-CLOSED SECOND COMPLETION — EXACT:**", 1
        )[1].split("**ENTRY-ELIGIBLE NEXT-ACTION FENCE:**", 1)[0]
        self.assertIn(
            "run_lifecycle.py complete-second --invocation-id <ID> "
            "--run-token <private token> --outcome <typed outcome>",
            public_completion,
        )
        self.assertIn("`snapshot-terminal`", public_completion)
        self.assertIn("`coordination-terminal`", public_completion)
        self.assertIn(
            "The parser does not accept `clear` or `tripped`",
            public_completion,
        )
        self.assertIn("`lease_renewed: true`", public_completion)
        self.assertIn(
            "Do not pass a test-only state or clock override during a "
            "trading run, including `--state-file`, `--now-utc`, or "
            "`--lifecycle-now-utc`",
            routine,
        )
        self.assertIn(
            "production CLIs for `run_lock.py`, `run_lifecycle.py`, "
            "`daily_loss.py`, `market_clock.py`, and `order_intents.py` "
            "reject clock overrides",
            routine,
        )
        order_handling = routine.split(
            "### ORDER HANDLING — AUTONOMOUS, WITH NOTIFICATION", 1
        )[1].split("### DRY RUN — simulate entries, never safety", 1)[0]
        for required in (
            "DIP-BUY BEGIN/RETRY AUTHORIZATION IS CHECKED-IN AND "
            "UNCONDITIONAL",
            "`order_intents.py begin` and `retry` internally call "
            "`run_lifecycle.authorize_entry_intent(run_token=<the exact "
            "journal argument>)`",
            "derives the invocation only from the active-context receipt",
            "requires the durable SECOND attempt plus exactly the "
            "deterministic `daily-loss-clear` checkpoint",
            '`action: "authorize-entry-intent"`, `ok: true`, the bound '
            "`invocation_id`",
            '`entry_guard_outcome: "clear"`, and `lease_renewed: true`',
            "prevents `begin`/`retry` before submission",
            "does not replace the separately required immediate "
            "broker-mutation renewal",
            "Profit-take sells, dust sells, and protective stops do not use "
            "the entry authorizer",
        ):
            self.assertIn(required, order_handling)

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertIn(
            "Every `dip-buy` begin and retry internally invokes the "
            "checked-in lifecycle entry authorizer",
            readme,
        )
        self.assertIn("There is no production CLI bypass", readme)

    def test_routine_fences_overlaps_and_rechecks_time_before_buys(self):
        with open(os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"), encoding="utf-8") as f:
            routine = f.read()

        entry_eligible_fence = routine.split(
            '**ENTRY-ELIGIBLE NEXT-ACTION FENCE:**', 1
        )[1].split('**THIRD — build this run', 1)[0]
        for required in (
            'the next local operation must be the exact owner-fenced '
            '`enter-second` action',
            'followed on success by the first prescribed DAILY-LOSS operation',
            'after a named attempted helper/connector operation is bound by '
            'the matching public',
            'It may not elect to omit the chain',
            'An unattempted DAILY-LOSS chain is runner omission',
            '`final-status-unavailable` remains legal only when',
            'FINAL refresh or status publication was actually attempted',
        ):
            self.assertIn(required, entry_eligible_fence)

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
            '`run_lifecycle.py acquire-bind-context --invocation-id '
            '<INVOCATION_ID>`',
            '`broker_snapshot.py preflight --create-scratch`',
            '`order_intents.py check`',
            '`order_intents.py pending --run-token <RUN_LOCK_TOKEN>`',
            'Resolve `rules_version`',
            'Call `get_accounts` as the first broker operation',
        )
        startup_positions = [startup.index(marker) for marker in startup_markers]
        self.assertEqual(startup_positions, sorted(startup_positions))
        launcher = routine.split(
            '### PYTHON LAUNCHER BOOTSTRAP', 1
        )[1].split('### INVOCATION LIFECYCLE', 1)[0]
        self.assertIn('`rhmra.bootstrap-state.v1`', launcher)
        self.assertIn(
            '{schema_version: 1, phase: "launcher-bound", '
            'resolver_receipt: resolverReceipt}',
            launcher,
        )
        self.assertIn(
            'complete unchanged `constants_receipt`',
            launcher,
        )
        self.assertIn('lifecycle_receipt', launcher)
        self.assertIn('phase: "lifecycle-bound"', launcher)
        self.assertIn('phase: "configuration-bound"', launcher)
        self.assertIn(
            'Only for these four startup receipts—the resolver receipt, '
            'lifecycle-start receipt, constants receipt, and active-context '
            'bind receipt',
            launcher,
        )
        self.assertIn(
            'This exemption does not apply to lifecycle event, status, or '
            'any helper/tool receipt after bind-context',
            launcher,
        )
        self.assertIn(
            'Never build a copied expected-key array, compare '
            '`Object.keys(...)`, or independently revalidate the 31 constants',
            launcher,
        )
        self.assertIn(
            '`MIN_REL_VOLUME` and `STOP_LOSS_PCT` are intentionally JSON '
            'strings, not integers',
            launcher,
        )
        self.assertIn(
            'must not paste the returned `python` string into later '
            'JavaScript',
            launcher,
        )
        launcher_code = launcher.split(
            '**CODEX WINDOWS LAUNCHER BIND — EXACT:**', 1
        )[1].split('```javascript', 1)[1].split('```', 1)[0]
        launcher_markers = (
            'store(BOOTSTRAP_KEY, null);',
            'const resolverInitial = await tools.exec_command({',
            'const resolverProcess = await drainCommand(resolverInitial);',
            'resolverReceipt = JSON.parse(resolverProcess.output)',
            'store(BOOTSTRAP_KEY, {schema_version: 1, phase: "launcher-bound"',
            'const rebound = load(BOOTSTRAP_KEY);',
            'text(JSON.stringify({schema_version: 1, action: '
            '"launcher-state-bound", ok: true}));',
        )
        launcher_positions = [
            launcher_code.index(marker) for marker in launcher_markers
        ]
        self.assertEqual(launcher_positions, sorted(launcher_positions))
        self.assertNotIn('text(resolverProcess.output)', launcher_code)
        lifecycle_start = routine.split(
            '### INVOCATION LIFECYCLE', 1
        )[1].split('A failed lifecycle start is terminal', 1)[0]
        self.assertIn(
            'The checked-in `start` action owns its canonical UUID grammar, '
            'conservative abandoned-run reconciliation, and complete schema',
            lifecycle_start,
        )
        self.assertIn(
            "store the entire parsed receipt unchanged as the bootstrap "
            "state's `lifecycle_receipt` before configuration validation",
            lifecycle_start,
        )
        self.assertIn(
            'Runner glue must not recreate them, count returned fields, '
            'or compare them with a model-authored key array',
            lifecycle_start,
        )
        for required in (
            'nonnegative integer `reconciled_abandoned_count`',
            'Boolean `reconciliation_blocked_by_live_lease`',
            'a live-lease block is diagnostic and never replaces or bypasses '
            "this new invocation's later normal `acquire-bind-context` "
            'overlap handling',
        ):
            self.assertIn(required, lifecycle_start)
        lifecycle_start_code = routine.split(
            '**CODEX LIFECYCLE START BIND — EXACT:**', 1
        )[1].split('```javascript', 1)[1].split('```', 1)[0]
        lifecycle_start_markers = (
            'const bootstrap = load(BOOTSTRAP_KEY);',
            'bootstrap.phase !== "launcher-bound"',
            'const pythonExe = bootstrap.resolver_receipt.python;',
            'const commandArguments = {',
            'let lifecycleProcess = await tools.exec_command(commandArguments);',
            'while (lifecycleProcess.session_id !== undefined)',
            'lifecycleReceipt = JSON.parse(lifecycleProcess.output)',
            'typeof lifecycleReceipt.invocation_id !== "string"',
            'lifecycleReceipt.invocation_id.length === 0',
            '!Number.isSafeInteger('
            'lifecycleReceipt.reconciled_abandoned_count)',
            'lifecycleReceipt.reconciled_abandoned_count < 0',
            'typeof lifecycleReceipt.'
            'reconciliation_blocked_by_live_lease !== "boolean"',
            'store(BOOTSTRAP_KEY, {...bootstrap, phase: "lifecycle-bound"',
            'const rebound = load(BOOTSTRAP_KEY);',
            'text(JSON.stringify({schema_version: 1, action: "lifecycle-state-bound", ok: true}));',
        )
        lifecycle_start_positions = [
            lifecycle_start_code.index(marker)
            for marker in lifecycle_start_markers
        ]
        self.assertEqual(
            lifecycle_start_positions, sorted(lifecycle_start_positions)
        )
        self.assertEqual(lifecycle_start_code.count('tools.exec_command('), 1)
        self.assertEqual(lifecycle_start_code.count('tools.write_stdin('), 1)
        self.assertEqual(lifecycle_start_code.count('run_lifecycle.py start'), 1)
        self.assertIn(
            'lifecycle_receipt: lifecycleReceipt', lifecycle_start_code
        )
        self.assertIn(
            'rebound.lifecycle_receipt.invocation_id !== '
            'lifecycleReceipt.invocation_id',
            lifecycle_start_code,
        )
        for forbidden in (
            'Object.keys',
            'expectedKeys',
            'text(lifecycleProcess.output)',
            'text(lifecycleReceipt)',
            'resolve_python',
            '--invocation-id',
            '--state-file',
            '--projection-file',
            '--now-utc',
            'functions.wait',
            'uuid4',
            '[89ab]',
            'RegExp',
            '/^[0-9a-f]{8}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/',
            '/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/',
        ):
            self.assertNotIn(forbidden, lifecycle_start_code)
        self.assertIn('Do not create or preflight scratch', startup)
        self.assertIn('touch the order-intent journal', startup)
        self.assertIn('before successful combined lease acquisition', startup)
        self.assertIn('Never invent a placeholder token', startup)
        self.assertIn(
            'only the successful checked-in `acquire-bind-context` result '
            'can supply it',
            startup,
        )
        self.assertIn(
            'Items 1–11 normally succeed before the one `get_accounts` '
            'transport canary',
            startup,
        )
        self.assertIn('If item 9 or 10 fails', startup)
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
            'same orchestration call must validate and machine-store the '
            'exact parsed receipt',
            startup,
        )
        self.assertIn(
            'Load the exact machine-carried preflight state from item 8',
            startup,
        )
        self.assertIn(
            'mechanically derive the canary path from the loaded '
            '`SOURCE_ROOT`',
            startup,
        )
        self.assertIn(
            'invoke `bind-transport` in that same operation with `scratch`, '
            '`source_root`, and `canary` taken only from loaded state',
            startup,
        )
        self.assertIn(
            'Every later Codex source/status path is likewise formed from '
            'that loaded state',
            startup,
        )
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
        startup_recipe = transport_binding.split(
            '**EXACT CODEX STARTUP SAVE-AND-BIND RECIPE', 1
        )[1].split('```javascript', 1)[1].split('```', 1)[0]
        target_declaration = (
            'const targetPath = receipt.source_root + separator + '
            '"get-accounts-" + receipt.source_root_id + ".json";'
        )
        self.assertIn(target_declaration, startup_recipe)
        self.assertNotIn(
            '<exact receipt-issued SOURCE_ROOT using / separators>',
            transport_binding,
        )
        self.assertNotIn(
            'const targetPath = "<exact receipt-issued SOURCE_ROOT',
            routine,
        )
        self.assertLess(
            startup_recipe.index(
                'const state = requireState("preflight-bound");'
            ),
            startup_recipe.index(
                'const GET_ACCOUNTS_TOOL = '
                '"mcp__robinhood_mcp__get_accounts";'
            ),
        )
        self.assertLess(
            startup_recipe.index(
                'const GET_ACCOUNTS_TOOL = '
                '"mcp__robinhood_mcp__get_accounts";'
            ),
            startup_recipe.index(target_declaration),
        )
        self.assertLess(
            startup_recipe.index(target_declaration),
            startup_recipe.index(
                'await resolvedGetAccountsTool({})'
            ),
        )
        self.assertLess(
            startup_recipe.index(
                'phase: "account-call-started", canary_path: targetPath'
            ),
            startup_recipe.index(
                'await resolvedGetAccountsTool({})'
            ),
        )
        self.assertIn('const parsed = JSON.parse(payload);', startup_recipe)
        self.assertIn('payload[0] !== "{"', startup_recipe)
        self.assertIn('payload[payload.length - 1] !== "}"', startup_recipe)
        self.assertIn(
            '"\\n+" + payload.replaceAll("\\n", "\\n+") + '
            '"\\n*** End Patch"',
            startup_recipe,
        )
        apply_position = startup_recipe.index(
            'await tools.apply_patch(patch)'
        )
        bind_position = startup_recipe.index(
            'const bindCommand = (isWindows ? "& " : "")'
        )
        bind_exec_position = startup_recipe.index(
            'const bindResult = await drainCommand('
            'await tools.exec_command(bindArgs));'
        )
        text_position = startup_recipe.index(
            'text(JSON.stringify({schema_version: 1, action: '
            '"transport-state-bound", ok: true}));'
        )
        self.assertLess(apply_position, bind_position)
        self.assertLess(bind_position, bind_exec_position)
        self.assertLess(bind_exec_position, text_position)
        for phase in (
            '"account-call-started"',
            '"account-retry-started"',
            '"account-response-received"',
            '"canary-saved"',
            '"transport-bound"',
        ):
            self.assertIn(phase, startup_recipe)
        self.assertIn('let firstFailed = false;', startup_recipe)
        self.assertIn('let retryFailed = false;', startup_recipe)
        retry_phase_position = startup_recipe.index(
            'phase: "account-retry-started"'
        )
        retry_call_position = startup_recipe.index(
            'await resolvedGetAccountsTool({})',
            startup_recipe.index('phase: "account-retry-started"'),
        )
        exhausted_position = startup_recipe.index(
            'canary_path: null, failure_code: "account-scope-failed"'
        )
        account_failure_text_position = startup_recipe.index(
            'text(JSON.stringify({schema_version: 1, action: '
            '"transport-state-failed", ok: false,'
        )
        account_failure_exit_position = startup_recipe.index(
            'exit();', account_failure_text_position
        )
        response_position = startup_recipe.index(
            'phase: "account-response-received"'
        )
        self.assertLess(retry_phase_position, retry_call_position)
        self.assertLess(retry_call_position, exhausted_position)
        self.assertLess(exhausted_position, account_failure_text_position)
        self.assertLess(account_failure_text_position, account_failure_exit_position)
        self.assertLess(account_failure_exit_position, response_position)
        self.assertNotIn(
            'throw new Error("get_accounts retry failed")', startup_recipe
        )
        self.assertIn(
            'error: {code: "account-scope-failed"}', startup_recipe
        )
        self.assertEqual(
            startup_recipe.count('await resolvedGetAccountsTool({})'), 2
        )
        self.assertNotIn('<resolved_get_accounts_tool>', startup_recipe)
        self.assertNotIn('<same_resolved_get_accounts_tool>', startup_recipe)
        self.assertNotIn('text(fullToolResult', startup_recipe)
        self.assertNotIn('text(first', startup_recipe)
        for loaded_argument in (
            'quote(savedState.python_exe)',
            'quote(receipt.scratch)',
            'quote(receipt.source_root)',
            'quote(targetPath)',
            'quote(savedState.configured_account_name)',
            'workdir: savedState.project_root',
        ):
            self.assertIn(loaded_argument, startup_recipe)
        self.assertIn('bindArgs.shell = "powershell.exe"', startup_recipe)
        self.assertIn(
            'while (current.session_id !== undefined)',
            startup_recipe,
        )
        self.assertIn('await tools.write_stdin({session_id:', startup_recipe)
        self.assertNotIn('text(bindResult.output', startup_recipe)
        self.assertIn(
            'failureReceipt.error.code === "account_scope_failed"',
            startup_recipe,
        )
        self.assertIn(
            'failureCode = "account-scope-failed"',
            startup_recipe,
        )
        self.assertIn(
            'error: {code: failureCode}',
            startup_recipe,
        )
        self.assertIn(
            'expectedScratchId = state.receipt.scratch_id;',
            startup_recipe,
        )
        self.assertIn(
            'current.receipt.source_root_id !== expectedSourceRootId',
            startup_recipe,
        )
        self.assertIn(
            'text(JSON.stringify({schema_version: 1, action: '
            '"transport-state-bound", ok: true}));',
            startup_recipe,
        )
        compact_scope_output = startup_recipe.split(
            'text(JSON.stringify({schema_version: 1, action: '
            '"transport-state-bound", ok: true}));',
            1,
        )[1]
        self.assertNotIn('account_number:', compact_scope_output)
        self.assertNotIn('--scratch \'<absolute scratch>\'', startup_recipe)
        self.assertNotIn('--source-root \'<absolute SOURCE_ROOT>\'', startup_recipe)
        self.assertIn(
            'The `account-call-started`, `account-retry-started`, '
            '`account-response-received`, and `canary-saved` phases are '
            'non-retriable fences',
            transport_binding,
        )
        self.assertIn(
            'If that final attempt returns an error or throws inside the '
            'still-running cell',
            transport_binding,
        )
        self.assertIn(
            'no successful response or save existed, so this is not '
            '`snapshot-write-failed`',
            transport_binding,
        )
        self.assertIn(
            'There must be no `text(...)`, `yield_control()`, assistant '
            'narration, raw payload output, or saved-path receipt between '
            'a successful broker call and validated bind result',
            transport_binding,
        )
        self.assertIn(
            'issue only `functions.wait` for that same cell until it '
            'finishes',
            transport_binding,
        )
        self.assertIn('Put zero bytes or characters before `{`', transport_binding)
        self.assertIn('literal six-character `\\ufeff`', transport_binding)
        self.assertIn('no real U+FEFF BOM', transport_binding)
        self.assertIn('Never use `String(fullToolResult)`', transport_binding)
        self.assertIn('Never emit the raw result, `payload`', transport_binding)
        self.assertIn('POST-BIND COMPOSED JSON SAVE RECIPE', transport_binding)
        self.assertNotIn('"\\n+\\ufeff" + payload', transport_binding)
        self.assertIn('exactly these twelve fields', transport_binding)
        for field in (
            '`account_name`', '`account_number`', '`agentic_allowed`',
        ):
            self.assertIn(field, transport_binding)
        self.assertIn(
            'bind `ACCOUNT_NAME`, `ACCOUNT_NUMBER`, and `AGENTIC_ALLOWED` '
            'only from the validated values stored in machine-carried '
            '`transport-bound` state',
            transport_binding,
        )
        self.assertIn('raw response', transport_binding)
        self.assertIn('model memory', transport_binding)
        self.assertIn(
            '`coordination-halt` / `account-scope-failed`',
            transport_binding,
        )
        self.assertIn('strict error code is `account_scope_failed`', transport_binding)
        self.assertIn('compact, path-free `snapshot-write-failed`', transport_binding)
        self.assertIn(
            '`snapshot-failure` / `snapshot-write-failed`',
            transport_binding,
        )
        self.assertIn('make no additional broker call', transport_binding)
        self.assertIn('do not try another directory or writer', transport_binding)
        self.assertIn(
            'do not create a nested/session fallback, second source root, '
            'or second probe',
            transport_binding,
        )
        self.assertIn(
            'Each save reserves a fresh purpose and uses only its '
            'helper-issued source',
            transport_binding,
        )
        self.assertIn(
            'A checked-in consumer receives the logical purpose',
            transport_binding,
        )
        self.assertIn(
            'never the random path',
            transport_binding,
        )
        post_bind_recipe = transport_binding.split(
            '**POST-BIND COMPOSED JSON SAVE RECIPE:**', 1
        )[1].split('If the canary was saved', 1)[0]
        self.assertIn('loading `rhmra.transport-state.v1`', post_bind_recipe)
        self.assertIn('requiring `phase: "transport-bound"`', post_bind_recipe)
        self.assertIn(
            'executor state never allocates a filename, sequence, pending key, '
            'or call/response phase',
            post_bind_recipe,
        )
        self.assertIn('invoke `broker_snapshot.py reserve-source', post_bind_recipe)
        self.assertIn(
            'refuses every new reservation while any earlier purpose lacks an '
            'immutable committed/aborted terminal marker',
            post_bind_recipe,
        )
        self.assertIn(
            'every consumer likewise refuses to run while any purpose is pending',
            post_bind_recipe,
        )
        self.assertIn(
            'four source-handoff actions are checked-in complete-receipt '
            'authorities',
            post_bind_recipe,
        )
        self.assertIn('Do not use `Object.keys`, count returned fields', post_bind_recipe)
        self.assertIn(
            'Helper-owned extra bookkeeping fields are not a failure',
            post_bind_recipe,
        )
        self.assertIn(
            'Use only that receipt\'s returned `source` variable as the '
            'file-change target',
            post_bind_recipe,
        )
        self.assertIn(
            'invoke exactly `broker_snapshot.py commit-source', post_bind_recipe
        )
        self.assertIn(
            'invoke exactly `broker_snapshot.py commit-source --scratch '
            '<same loaded scratch> --purpose <same purpose>`',
            post_bind_recipe,
        )
        self.assertIn(
            'Do not pass `--reservation-id` to `commit-source`',
            post_bind_recipe,
        )
        self.assertIn(
            'helper loads the one immutable reservation identified by scratch '
            'plus purpose',
            post_bind_recipe,
        )
        self.assertIn(
            'explicitly supplied `--reservation-id` remains a checked '
            'backward-compatibility assertion',
            post_bind_recipe,
        )
        self.assertIn(
            '`broker_snapshot.py lookup-source --scratch <loaded scratch> '
            '--purpose <purpose>`',
            post_bind_recipe,
        )
        self.assertIn(
            'reserved with no file means halt without another broker call or '
            'write',
            post_bind_recipe,
        )
        self.assertIn(
            'An interrupted/lost outer cell cannot prove the broker or write '
            'boundary',
            post_bind_recipe,
        )
        self.assertIn(
            'invoke `abort-source` with fixed reason `connector-failed`',
            post_bind_recipe,
        )
        for required in (
            'CONNECTOR-FAILURE ABORT COMMAND — EXACT AND ID-BOUND',
            'same still-running composed operation',
            'The `--reservation-id` operand is mandatory and comes only '
            'from that same receipt',
            'sourceReservation.reservation_id.length === 0',
            '" --reservation-id " + '
            'quote(sourceReservation.reservation_id)',
            'reservation_id` equal to '
            '`sourceReservation.reservation_id`',
            'A purpose-only abort is forbidden',
        ):
            self.assertIn(required, post_bind_recipe)
        abort_block = post_bind_recipe.split(
            '**CONNECTOR-FAILURE ABORT COMMAND — EXACT AND ID-BOUND:**', 1
        )[1].split('After an explicit successful tool result', 1)[0]
        self.assertIn('broker_snapshot.py abort-source', abort_block)
        self.assertIn('--purpose ', abort_block)
        self.assertIn('--reservation-id ', abort_block)
        self.assertLess(
            abort_block.index('--purpose '),
            abort_block.index('--reservation-id '),
        )
        self.assertNotIn(
            'broker_snapshot.py abort-source --scratch " + quote(scratch) +\n'
            '  " --purpose " + quote(sourceReservation.purpose) +\n'
            '  " --reason connector-failed"',
            abort_block,
        )
        self.assertIn('**Mutation-response exception:**', post_bind_recipe)
        self.assertIn(
            'do not reserve a source before '
            '`place_equity_order` or `cancel_equity_order`',
            post_bind_recipe,
        )
        self.assertIn(
            'no runner substitutes an executor-owned phase map',
            post_bind_recipe,
        )
        for obsolete in (
            'next_source_seq', 'pending_handoff', 'source-call-started',
            'source-response-received', 'handoffs[purposeKey]',
        ):
            self.assertNotIn(obsolete, post_bind_recipe)
        self.assertIn(
            'rejects every caller-created, replaced, or alternate root',
            transport_binding,
        )
        self.assertIn('do not start generation A or B', transport_binding)
        self.assertIn('do not retry the save', transport_binding)

        coordination = routine.split(
            "### RUN COORDINATION — fenced single-flight lease", 1
        )[1].split("### ORDER-INTENT JOURNAL", 1)[0]
        self.assertIn(
            "before scratch creation, any order-intent journal command, "
            "`rules_version`, `get_accounts`, or ANY broker call",
            coordination,
        )
        self.assertIn(
            "requires a running preflight binding, acquires the fenced "
            "SQLite lease",
            coordination,
        )
        self.assertIn(
            "`& '<PYTHON_EXE>' run_lifecycle.py acquire-bind-context "
            "--invocation-id '<INVOCATION_ID>'`",
            coordination,
        )
        self.assertIn(
            "`'<PYTHON_EXE>' run_lifecycle.py acquire-bind-context "
            "--invocation-id '<INVOCATION_ID>'`",
            coordination,
        )
        self.assertIn("`schema_version: 1`", coordination)
        self.assertIn("`ok: true`", coordination)
        self.assertIn("`RUN_LOCK_TOKEN`", coordination)
        self.assertIn('`reason: "active_run"`', coordination)
        self.assertIn("OVERLAP HALT", coordination)
        self.assertIn("Make no broker calls", coordination)
        self.assertIn('supersede REPORT, its "every run" status-snapshot rule', coordination)
        self.assertIn("expires after 20 minutes", coordination)
        self.assertIn(
            "At the start of FIRST, THIRD, FOURTH, and REPORT",
            coordination,
        )
        self.assertIn(
            "SECOND is the deliberate exception", coordination
        )
        self.assertIn(
            "`& '<PYTHON_EXE>' run_lock.py renew --token "
            "'<RUN_LOCK_TOKEN>'`",
            coordination,
        )
        self.assertIn("immediately before EVERY `cancel_equity_order` and `place_equity_order`", coordination)
        self.assertIn("ownership is lost: make no further broker calls or order changes", coordination)
        self.assertIn("run_lifecycle.py release-finish", coordination)
        self.assertNotIn(
            "`& '<PYTHON_EXE>' run_lock.py release --token ",
            coordination,
        )
        self.assertIn("final owned-lease operational action", coordination)
        self.assertNotIn("py -3 run_lock.py", coordination)
        self.assertNotIn("python3 run_lock.py", coordination)

        self.assertIn('before scratch creation', coordination)
        self.assertIn('any order-intent journal command', coordination)
        self.assertEqual(
            coordination.count('broker_snapshot.py preflight --create-scratch'),
            3,
        )
        self.assertIn(
            "`& '<PYTHON_EXE>' broker_snapshot.py preflight --create-scratch`",
            coordination,
        )
        self.assertIn(
            "`'<PYTHON_EXE>' broker_snapshot.py preflight --create-scratch`",
            coordination,
        )
        self.assertIn(
            'prepares and verifies the least-privilege OS capability needed '
            'for the separate file-change facility to create fresh '
            'direct-child files in both directories',
            coordination,
        )
        self.assertIn(
            'added cross-principal writer-only capability', coordination
        )
        self.assertIn(
            'extend bootstrap with the complete unchanged nested result as '
            '`context_receipt` plus outer `phase: "context-bound"`',
            coordination,
        )
        preflight_recipe = coordination.split(
            '**CODEX MACHINE-CARRIED PREFLIGHT STATE — REQUIRED:**', 1
        )[1].split('```javascript', 1)[1].split('```', 1)[0]
        clear_position = preflight_recipe.index('store(STATE_KEY, null);')
        command_position = preflight_recipe.index(
            'const preflightResult = await drainCommand('
            'await tools.exec_command(preflightArgs));'
        )
        parse_position = preflight_recipe.index(
            'const receipt = JSON.parse(preflightResult.output);'
        )
        store_position = preflight_recipe.index(
            'store(STATE_KEY, {schema_version: 1, phase: "preflight-bound"'
        )
        compact_output_position = preflight_recipe.index(
            'text(JSON.stringify({schema_version: 1, action: '
            '"preflight-state-bound", ok: true}));'
        )
        self.assertLess(clear_position, command_position)
        self.assertLess(command_position, parse_position)
        self.assertLess(parse_position, store_position)
        self.assertLess(store_position, compact_output_position)
        self.assertIn(
            'must finish by storing `preflight-bound` state before its one '
            'compact path-free success output',
            coordination,
        )
        self.assertIn(
            'Only after this cell succeeds may startup continue to the '
            'order-intent checks and rules-version step',
            coordination,
        )
        self.assertIn(
            'const bootstrap = load(BOOTSTRAP_KEY);',
            preflight_recipe,
        )
        self.assertIn(
            'bootstrap.phase !== "context-bound"',
            preflight_recipe,
        )
        self.assertIn(
            '!bootstrap.lifecycle_receipt',
            preflight_recipe,
        )
        self.assertIn(
            'const pythonExe = bootstrap.resolver_receipt.python;',
            preflight_recipe,
        )
        self.assertIn(
            'bootstrap.constants_receipt.values.AGENTIC_ACCOUNT_NAME',
            preflight_recipe,
        )
        self.assertNotIn('const pythonExe = "<exact retained', preflight_recipe)
        self.assertNotIn('const configuredAccountName = "<exact', preflight_recipe)
        self.assertIn(
            '[Console]::Out.Write((Get-Location).Path)',
            preflight_recipe,
        )
        self.assertIn(
            'workdir: projectRoot',
            preflight_recipe,
        )
        self.assertIn(
            'preflightArgs.shell = "powershell.exe"',
            preflight_recipe,
        )
        self.assertIn(
            'while (current.session_id !== undefined)',
            preflight_recipe,
        )
        self.assertIn('await tools.write_stdin({session_id:', preflight_recipe)
        self.assertIn(
            'configured_account_name: configuredAccountName, '
            'project_root: projectRoot',
            preflight_recipe,
        )
        self.assertIn(
            'const actualKeys = Object.keys(receipt).sort();',
            preflight_recipe,
        )
        self.assertIn(
            'receipt.scratch === receipt.source_root',
            preflight_recipe,
        )
        self.assertNotIn('tools.store', preflight_recipe)
        self.assertNotIn('tools.load', preflight_recipe)
        self.assertNotIn(
            'source_root: receipt.source_root',
            preflight_recipe[compact_output_position:],
        )
        self.assertIn(
            'The fixed slot is cleared before the invocation\'s sole '
            'preflight',
            coordination,
        )
        self.assertIn(
            'Missing, malformed, cleared, wrong-phase, or unavailable state '
            'is terminal before another broker call',
            coordination,
        )
        self.assertIn(
            'an app/task/session boundary that loses the slot fails closed',
            coordination,
        )
        self.assertIn('exactly these ten fields', coordination)
        for field in (
            '`schema_version`', '`action`', '`ok`', '`scratch`',
            '`scratch_id`', '`source_root`', '`source_root_id`',
            '`sentinel_sha256`', '`write_read_parse`', '`cleanup_verified`',
        ):
            self.assertIn(field, coordination)
        self.assertIn('distinct resolved non-symlink direct children', coordination)
        self.assertIn(
            '`scratch_id` and `source_root_id` as nonempty strings and retain '
            'them unchanged',
            coordination,
        )
        self.assertIn(
            'checked-in preflight producer owns canonical identifier grammar; '
            'runner glue must not recreate it',
            coordination,
        )
        self.assertIn(
            'Bind `<scratch>`, `SCRATCH_ID`, `SOURCE_ROOT`, and '
            '`SOURCE_ROOT_ID` only through the validated machine-carried '
            'receipt',
            coordination,
        )
        self.assertIn('opaque invocation state', coordination)
        self.assertIn(
            'never type, copy, shorten, reconstruct, normalize, or '
            're-transcribe either path or identifier',
            coordination,
        )
        self.assertIn('`icacls`, an ACL API, or any file tool', coordination)
        self.assertIn(
            'Do not author, randomize, predict, set permissions on, or repair '
            'either path, and do not pass either path to this command',
            coordination,
        )
        self.assertIn(
            'Do not separately call `New-Item`, `mkdir`, `mktemp`, `mkdtemp`',
            coordination,
        )
        self.assertIn(
            'the one real `get_accounts` canary below remains the sole '
            'end-to-end sensitive-write proof',
            coordination,
        )
        self.assertIn('Never retry with `--scratch`', coordination)
        self.assertIn(
            'create another scratch or source directory', coordination
        )
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

        status_handoff = routine.split(
            '**CODEX STATUS MACHINE HANDOFF — REQUIRED:**', 1
        )[1].split('The Windows preflight prepared this directory', 1)[0]
        for binding in (
            '`scratch` from `state.receipt.scratch`',
            '`invocation_id` from `state.context_receipt.invocation_id`',
            '`state.context_receipt.expected_report_file`',
            '`expected_status_file`',
            '`phase: "status-candidate-write-started"`',
            '`phase: "status-candidate-saved"`',
            '`phase: "status-publish-started"`',
            '`phase: "status-published"`',
            '`status-rewrite-authorized`',
            '`status_snapshot_missing`',
        ):
            self.assertIn(binding, status_handoff)
        self.assertIn(
            'every path placeholder is forbidden in executable cell source',
            status_handoff,
        )
        self.assertIn(
            'Exact publish or verify success—whether initial or after the '
            'permitted second publish—must store the complete '
            '`status_binding`',
            status_handoff,
        )
        self.assertIn(
            'sees `status-candidate-saved`, it may continue directly',
            status_handoff,
        )

        for non_codex_contract in (
            'another runner, retain the validated receipt as one equivalent '
            'opaque structured value',
            'A non-Codex runner must perform the same full-object JSON '
            'serialization',
            'Codex and every non-Codex runner use the same checked-in '
            'reserve/write/commit/lookup protocol',
            'In another harness, use the same checked-in journal actions',
            'A non-Codex runner must use its equivalent opaque structured '
            'state',
        ):
            self.assertIn(non_codex_contract, routine)
        finalization = routine.split(
            '**FIXED FINALIZATION ORDER', 1
        )[1].split('### PERFORMANCE TELEMETRY', 1)[0]
        self.assertIn('`rhmra.transport-state.v1`', finalization)
        self.assertIn('`rhmra.bootstrap-state.v1`', finalization)
        self.assertIn('`rhmra.lease-state.v1`', finalization)
        self.assertIn('clear all three fixed slots', finalization)
        self.assertIn('`store(key, null)`', finalization)
        self.assertIn('after the nested helper completes', finalization)

        journal = routine.split('### ORDER-INTENT JOURNAL', 1)[1].split(
            '### BROKER TIMESTAMPS', 1
        )[0]
        self.assertIn("Use the retained absolute `PYTHON_EXE`", journal)
        self.assertIn("Never execute literal `py`, `python`, or `python3`", journal)
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

    def test_routine_machine_carries_exact_private_lease_token(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        coordination = routine.split(
            "### RUN COORDINATION — fenced single-flight lease", 1
        )[1].split("### ORDER-INTENT JOURNAL", 1)[0]
        lease_contract = coordination.split(
            "**CODEX PRIVATE LEASE STATE — REQUIRED:**", 1
        )[1].split(
            "After the active-context receipt succeeds", 1
        )[0]
        acquire_bind_code = lease_contract.split(
            "```javascript", 1
        )[1].split("```", 1)[0]

        clear_position = acquire_bind_code.index("store(LEASE_KEY, null);")
        combined_position = acquire_bind_code.index(
            "const combinedResult = await drainCommand("
        )
        lease_store_position = acquire_bind_code.index(
            'store(LEASE_KEY, {schema_version: 1, phase: "lease-owned"'
        )
        context_position = acquire_bind_code.index(
            "const receipt = combinedReceipt.context_receipt;"
        )
        compact_output_position = acquire_bind_code.index(
            'action: "context-state-bound", ok: true'
        )
        self.assertLess(clear_position, combined_position)
        self.assertLess(combined_position, lease_store_position)
        self.assertLess(lease_store_position, context_position)
        self.assertLess(context_position, compact_output_position)
        self.assertIn(
            "run_lock_token: combinedReceipt.run_lock_token",
            acquire_bind_code,
        )
        self.assertEqual(acquire_bind_code.count('tools.exec_command('), 1)
        self.assertEqual(
            acquire_bind_code.count('run_lifecycle.py acquire-bind-context'),
            1,
        )
        self.assertNotIn('run_lock.py acquire', acquire_bind_code)
        self.assertNotIn('run_lifecycle.py bind-context', acquire_bind_code)
        self.assertNotIn('uuid4', acquire_bind_code)
        self.assertNotIn('[89ab]', acquire_bind_code)
        self.assertNotIn('RegExp', acquire_bind_code)
        self.assertIn(
            'lease.phase !== "lease-owned"', acquire_bind_code
        )
        self.assertIn(
            'lease.invocation_id !== invocationId', acquire_bind_code
        )
        for holder_field in ("acquired_at", "renewed_at", "expires_at"):
            self.assertIn(
                f"combinedReceipt.holder.{holder_field}", acquire_bind_code
            )
        for required in (
            'combinedReceipt.reason === "bind_context_failed"',
            'combinedReceipt.lease_released === true',
            'combinedReceipt.compensation_recorded === true',
            'combinedReceipt.reason === '
            '"bind_context_failed_compensation_unrecorded"',
            'compensatedBindFailure ? "bind-context-failed-compensated"',
            'unrecordedCompensation ? '
            '"bind-context-failed-compensation-unrecorded"',
        ):
            self.assertIn(required, acquire_bind_code)
        self.assertIn("holder: activeRun ?", acquire_bind_code)
        self.assertIn(
            "Only the existing checked-in deterministic protocols may "
            "persist it",
            lease_contract,
        )
        self.assertIn(
            "`run_lock.py`, called internally by lifecycle, owns the "
            "gitignored lease database",
            lease_contract,
        )
        self.assertIn("`order_intents.py prepare` scratch intent", lease_contract)
        self.assertIn("Runner glue performs no other persistence", lease_contract)
        self.assertNotIn("tool receipt, or filesystem", lease_contract)
        self.assertNotIn('--run-token \'\'', acquire_bind_code)
        self.assertNotIn('quote("")', acquire_bind_code)
        for line in acquire_bind_code.splitlines():
            if "text(" in line:
                self.assertNotIn("run_lock_token", line)
                self.assertNotIn("combinedReceipt.run_lock_token", line)

        preflight_code = coordination.split(
            "**CODEX MACHINE-CARRIED PREFLIGHT STATE — REQUIRED:**", 1
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        self.assertIn(
            'const LEASE_KEY = "rhmra.lease-state.v1";', preflight_code
        )
        self.assertIn('const lease = load(LEASE_KEY);', preflight_code)
        self.assertIn('lease.phase !== "lease-owned"', preflight_code)
        self.assertIn(
            'typeof lease.invocation_id !== "string"', preflight_code
        )
        self.assertIn('lease.invocation_id.length === 0', preflight_code)
        self.assertIn(
            'lease.invocation_id !== bootstrap.context_receipt.invocation_id',
            preflight_code,
        )
        transport_store = preflight_code.split(
            'store(STATE_KEY, {schema_version: 1, phase: "preflight-bound"',
            1,
        )[1].split('text(JSON.stringify({schema_version: 1, action:', 1)[0]
        self.assertIn("lease_binding:", transport_store)
        self.assertNotIn("run_lock_token", transport_store)

        pending_contract = coordination.split(
            "**CODEX ORDER-INTENT STARTUP — EXACT PRIVATE-TOKEN RECIPE:**",
            1,
        )[1].split("The JavaScript checks the helper", 1)[0]
        pending_code = pending_contract.split(
            "```javascript", 1
        )[1].split("```", 1)[0]
        prerequisite_position = pending_code.index(
            'action: "order-intent-prerequisite-failed"'
        )
        check_position = pending_code.index(
            'const checkResult = await runJournal("check");'
        )
        pending_position = pending_code.index(
            'const pendingResult = await runJournal('
        )
        self.assertLess(prerequisite_position, check_position)
        self.assertLess(check_position, pending_position)
        pending_store_position = pending_code.index(
            "order_intent_pending_receipt: pendingReceipt"
        )
        pending_output_position = pending_code.index(
            'action: "order-intent-startup-checked"'
        )
        self.assertLess(pending_position, pending_store_position)
        self.assertLess(pending_store_position, pending_output_position)
        for required in (
            '!state.context_receipt',
            'state.phase !== "preflight-bound"',
            '!absolute(state.python_exe)',
            '!absolute(state.project_root)',
            'lease.phase !== "lease-owned"',
            'typeof invocationId !== "string"',
            'invocationId.length === 0',
            'lease.invocation_id !== invocationId',
            'const runLockToken = lease.run_lock_token;',
            '"pending --run-token " + quote(runLockToken)',
            'do not invoke `check`',
            'do not invoke `pending` with an empty probe',
            'do not call a broker',
            'const postPendingState = load(STATE_KEY);',
            'const postPendingLease = load(LEASE_KEY);',
            'pendingPayload.includes(runLockToken)',
            'order_intent_pending_receipt: pendingReceipt',
            'Recovery must load the complete unchanged '
            '`order_intent_pending_receipt`',
            '`pending` must not be rerun',
        ):
            self.assertIn(required, pending_contract)
        self.assertEqual(pending_code.count("pending --run-token"), 1)
        self.assertNotIn('--run-token \'\'', pending_code)
        self.assertNotIn('quote("")', pending_code)
        self.assertNotIn('context_receipt ? "" : ""', routine)
        for line in pending_code.splitlines():
            if "text(" in line:
                self.assertNotIn("runLockToken", line)
                self.assertNotIn("run_lock_token", line)

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        with open(os.path.join(ROOT, "QUICKSTART.md"), encoding="utf-8") as f:
            quickstart = f.read()
        self.assertIn("three separate executor-local state slots", readme)
        self.assertIn("clears all three slots", readme)
        self.assertNotIn("Codex clears both slots", readme)
        self.assertIn("All three Codex state slots", quickstart)
        self.assertNotIn("Both Codex state slots", quickstart)

        accounts_code = coordination.split(
            "**EXACT CODEX STARTUP SAVE-AND-BIND RECIPE", 1
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        self.assertIn(
            'const LEASE_KEY = "rhmra.lease-state.v1";', accounts_code
        )
        self.assertLess(
            accounts_code.index("requireLease(expectedInvocationId);"),
            accounts_code.index(
                "fullToolResult = await resolvedGetAccountsTool({});"
            ),
        )

        later_contract = coordination.split(
            "**One private token authority for every later use:**", 1
        )[1]
        for operation in (
            "renewal", "enter-second", "daily_loss.py", "complete-second",
            "release-finish", "broker call", "prepare", "pending", "begin",
            "retry",
        ):
            self.assertIn(operation, later_contract)
        self.assertIn(
            'phase: "lease-released"', later_contract
        )
        self.assertIn("phase `lease-lost`", later_contract)
        active_precondition = later_contract.split(
            "**CODEX LATER TOKEN PRECONDITION — REQUIRED IN EACH ACTIVE "
            "CELL:**",
            1,
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        active_guard = active_precondition.index(
            'action: "private-lease-prerequisite-failed"'
        )
        active_token = active_precondition.index(
            "const runLockToken = lease.run_lock_token;"
        )
        self.assertLess(active_guard, active_token)
        for required in (
            'const lease = load(LEASE_KEY);',
            'state.phase !== "transport-bound"',
            'lease.phase !== "lease-owned"',
            'typeof invocationId !== "string"',
            'invocationId.length === 0',
            'lease.invocation_id !== invocationId',
            'const quotedRunLockToken = quote(lease.run_lock_token);',
        ):
            self.assertIn(required, active_precondition)
        self.assertNotIn('state.phase.startsWith("status-")', active_precondition)
        self.assertNotIn('state.phase !== "terminal"', active_precondition)

        release_code = later_contract.split(
            "**CODEX RELEASE-FINISH TOMBSTONE — EXACT:**", 1
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        release_guard = release_code.index(
            'action: "release-finish-prerequisite-failed"'
        )
        self.assertIn(
            'typeof invocationId !== "string"', release_code
        )
        self.assertIn('invocationId.length === 0', release_code)
        self.assertNotIn("|| !invocationId ||", release_code)
        release_call = release_code.index(
            "const result = await drainCommand("
        )
        released_store = release_code.index(
            'store(LEASE_KEY, {schema_version: 1, phase: "lease-released"'
        )
        released_output = release_code.index(
            'text(JSON.stringify({schema_version: 1, action: '
            '"release-finish-complete", ok: true}));'
        )
        self.assertLess(release_guard, release_call)
        self.assertLess(release_call, released_store)
        self.assertLess(released_store, released_output)
        self.assertNotIn("run_lock_token:", release_code)
        self.assertNotIn('uuid4', routine)
        self.assertNotIn('[89ab]', routine)
        javascript_blocks = '\n'.join(
            routine.split('```javascript')[index].split('```', 1)[0]
            for index in range(1, len(routine.split('```javascript')))
        )
        for forbidden_uuid_grammar in (
            '[0-9a-f]{8}', '[0-9A-Fa-f]{8}', '-4[0-9a-f]{3}-',
            '-4[0-9A-Fa-f]{3}-',
        ):
            self.assertNotIn(forbidden_uuid_grammar, javascript_blocks)
        self.assertIn("reportSequenceStarted", release_code)
        self.assertIn("reportTerminal", release_code)
        self.assertIn("status-published", release_code)
        self.assertIn("status-unavailable", release_code)
        for line in release_code.splitlines():
            if "text(" in line:
                self.assertNotIn("runLockToken", line)
                self.assertNotIn("lease.run_lock_token", line)

        finalization = routine.split(
            "**FIXED FINALIZATION ORDER", 1
        )[1].split("### PERFORMANCE TELEMETRY", 1)[0]
        for key in (
            "rhmra.transport-state.v1",
            "rhmra.bootstrap-state.v1",
            "rhmra.lease-state.v1",
        ):
            self.assertIn(key, finalization)
        self.assertIn("clear all three fixed slots", finalization)
        self.assertIn(
            "lease release (or proven ownership loss), and lifecycle finish",
            finalization,
        )

    def test_routine_distinguishes_bind_context_receipt_and_runner_phases(self):
        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()

        startup = routine.split(
            '### STARTUP SEQUENCE — complete exactly before normal account '
            'or broker access', 1
        )[1].split('### ACCOUNT SCOPE', 1)[0]
        self.assertIn(
            'nested receipt\'s lifecycle `phase` to remain `"preflight"`. '
            '`context-bound` is the runner\'s stored-state phase, never the '
            'receipt phase',
            startup,
        )

        coordination = routine.split(
            '### RUN COORDINATION — fenced single-flight lease', 1
        )[1].split('### ORDER-INTENT JOURNAL', 1)[0]
        bind_contract = coordination.split(
            'Successful stdout is one JSON object', 1
        )[1].split(
            'After the active-context receipt succeeds and before '
            '`rules_version`', 1
        )[0]
        self.assertIn(
            'combined action is the canonical UUID/type/phase authority',
            coordination,
        )
        self.assertIn(
            'checked-in helper, not runner JavaScript, owns canonical UUID '
            'validation',
            bind_contract,
        )
        self.assertIn(
            'must not create a UUID regular expression or independently '
            'revalidate either identifier',
            bind_contract,
        )
        self.assertIn('`phase: "preflight"`', bind_contract)
        self.assertIn(
            '`receipt.phase === "preflight"` and the newly stored '
            '`bootstrap.phase === "context-bound"` are both required and '
            'describe different state machines',
            bind_contract,
        )
        self.assertNotIn('exactly these thirteen fields', bind_contract)
        self.assertNotIn('uuid4', bind_contract)

        bind_recipe = bind_contract.split(
            '```javascript', 1
        )[1].split('```', 1)[0]
        receipt_check = bind_recipe.index('receipt.phase !== "preflight"')
        state_store = bind_recipe.index(
            'store(BOOTSTRAP_KEY, {...bootstrap, context_receipt: receipt,'
        )
        self.assertLess(receipt_check, state_store)
        self.assertIn(
            'bootstrap.phase !== "configuration-bound"', bind_recipe
        )
        self.assertIn('phase: "context-bound"', bind_recipe)
        self.assertNotIn('receipt.phase !== "context-bound"', bind_recipe)
        self.assertNotIn('Object.keys', bind_recipe)
        self.assertNotIn('actualKeys', bind_recipe)
        for artifact_field in (
            'run_start_pt',
            'artifact_stamp',
            'expected_report_file',
            'expected_gate_file',
            'expected_status_file',
        ):
            self.assertNotIn(artifact_field, bind_recipe)

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
            '**Finalize owned lifecycle with release-finish — execute now:**'
        )
        report_readback = routine.index('**Then READ THE REPORT BACK, once')
        telemetry_start = routine.index(
            '### PERFORMANCE TELEMETRY — after lifecycle terminalization, '
            'never authoritative'
        )
        summary_start = routine.index(
            '### FINAL ON-SCREEN RUN SUMMARY — immediately after '
            'performance telemetry'
        )
        self.assertLess(report_readback, lifecycle_finish)
        self.assertLess(lifecycle_finish, telemetry_start)
        self.assertLess(telemetry_start, summary_start)
        self.assertLess(
            routine.index('run_performance.py record-internal'), summary_start
        )

        telemetry = routine[telemetry_start:summary_start]
        self.assertEqual(
            routine.count('run_performance.py record-internal'), 2
        )
        for required in (
            'Only after all permitted report/status persistence and '
            'read-back',
            'one successful lifecycle terminalization',
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
            'Immediately after receiving the `final-summary-boundary` handoff, '
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
        self.assertIn(
            'The Windows preflight prepared this directory for that exact '
            'cross-facility direct-child creation',
            routine,
        )

    def test_routine_final_telemetry_machine_returns_report_on_invalid_receipt(self):
        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()

        telemetry = routine.split(
            '### PERFORMANCE TELEMETRY — after lifecycle terminalization, '
            'never authoritative', 1
        )[1].split(
            '### FINAL ON-SCREEN RUN SUMMARY — immediately after '
            'performance telemetry', 1
        )[0]
        contract = telemetry.split(
            '**CODEX FINAL TELEMETRY + REPORT POINTER — EXACT:**', 1
        )[1]
        code = contract.split('```javascript', 1)[1].split('```', 1)[0]

        for required in (
            'const bootstrap = load(BOOTSTRAP_KEY);',
            'const state = load(STATE_KEY);',
            'const lease = load(LEASE_KEY);',
            'bootstrap.context_receipt.expected_report_file',
            'state.context_receipt.expected_report_file !== expectedReportFile',
            'state.phase === "status-published"',
            'state.phase === "status-unavailable"',
            'state.report_binding.expected_report_file !== expectedReportFile',
            'state.report_binding.persisted !== true',
            'state.report_binding.read_back !== true',
            'state.status_binding.status_file !== expectedStatusFile',
            'lease.phase !== "lease-released"',
            'lease.phase !== "lease-lost"',
            'Object.prototype.hasOwnProperty.call(lease, "run_lock_token")',
            'action: "final-summary-boundary"',
            'expected_report_file: expectedReportFile',
            'telemetry_ok: telemetryOk',
            'handoff.timing_unavailable = timingUnavailable',
            'text(JSON.stringify(handoff));',
        ):
            self.assertIn(required, code)

        self.assertNotIn('lease.phase !== "lease-owned"', code)
        self.assertNotIn('lease.run_lock_token', code)
        self.assertNotIn('Object.keys', code)
        self.assertNotIn('expectedKeys', code)
        self.assertNotIn('new Date', code)
        self.assertNotIn('market_clock', code)
        self.assertNotIn('artifact_stamp', code)
        self.assertEqual(code.count('tools.exec_command'), 1)

        call_position = code.index(
            'recordInternalResult = await drainCommand('
        )
        handoff_position = code.index('const handoff = {')
        telemetry_branch = code.index('if (telemetryOk) {')
        telemetry_failure = code.index(
            'handoff.timing_unavailable = timingUnavailable'
        )
        output_position = code.index('text(JSON.stringify(handoff));')
        self.assertLess(call_position, handoff_position)
        self.assertLess(handoff_position, telemetry_branch)
        self.assertLess(telemetry_branch, telemetry_failure)
        for cleanup in (
            'store(STATE_KEY, null);',
            'store(BOOTSTRAP_KEY, null);',
            'store(LEASE_KEY, null);',
        ):
            cleanup_position = code.index(cleanup)
            self.assertLess(call_position, cleanup_position)
            self.assertLess(telemetry_failure, cleanup_position)
            self.assertLess(cleanup_position, output_position)

        for required in (
            'sole complete-schema/type authority',
            'must not count fields, compare `Object.keys(...)`, copy an '
            'expected 14- or 18-field list',
            'still emits `telemetry_ok: false`',
            'exact machine-loaded `expected_report_file`',
        ):
            self.assertIn(required, telemetry)

        final_summary = routine.split(
            '### FINAL ON-SCREEN RUN SUMMARY — immediately after '
            'performance telemetry', 1
        )[1]
        for required in (
            "copied byte-for-byte from the final handoff's "
            '`expected_report_file`',
            'including when `telemetry_ok` is false',
            'Never substitute a filename from memory, current time, '
            'narration, the displayed pattern below, or a prior run',
            'This telemetry branch never changes the report pointer',
        ):
            self.assertIn(required, final_summary)

    def test_supported_docs_keep_helper_owned_temp_bridge_safety(self):
        source_allow = (
            'Edit(//c/Users/<Windows-user>/AppData/Local/Temp/'
            'rhmra-source-*/*.json)'
        )
        status_allow = (
            'Edit(//c/Users/<Windows-user>/AppData/Local/Temp/'
            'rhmra-session-*/rhmra-status-candidate.json)'
        )
        source_marker_deny = (
            'Edit(//c/Users/<Windows-user>/AppData/Local/Temp/'
            'rhmra-source-*/.rhmra-broker-response-source-root.json)'
        )
        for filename in ('README.md', 'QUICKSTART.md'):
            with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                document = f.read()
            with self.subTest(document=filename):
                # Unsupported Claude permission recipes must not reappear.
                self.assertNotIn(source_allow, document)
                self.assertNotIn(status_allow, document)
                self.assertNotIn(source_marker_deny, document)
                self.assertIn('least-privilege per-directory', document)
                self.assertIn('separate file-change facility', document)
                self.assertIn('helper', document)
                self.assertIn('markers', document)
                self.assertIn('changes permissions', document)
                if filename == 'README.md':
                    self.assertIn(
                        'never calls `New-Item`, `mkdir`, `mktemp`/`mkdtemp`, '
                        '`icacls`, or an ACL API',
                        document,
                    )
                else:
                    self.assertIn(
                        'without making the added cross-principal '
                        'writer-only capability inherit to helper markers',
                        document,
                    )

    def test_timing_identity_and_metric_names_do_not_guess(self):
        documents = {}
        for filename in (
            'robinhood-momentum-routine-autonomous.md',
            'README.md',
            'QUICKSTART.md',
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
            'Immediately after the complete bounded routine-file read '
            'reaches EOF',
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
            '--model \'gpt-5.6-sol\' '
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
        for filename in ('README.md', 'QUICKSTART.md'):
            with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                documents[filename] = f.read()

        for filename in ('README.md', 'QUICKSTART.md'):
            self.assertEqual(documents[filename].count(codex_observe), 1)
        self.assertEqual(documents['README.md'].count(claude_observe), 1)
        self.assertEqual(documents['QUICKSTART.md'].count(claude_observe), 0)
        self.assertIn(
            'Historical Claude transcript backfill only',
            documents['README.md'],
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
            'currently recommended deployment candidate is '
            '**Codex Sol 5.6 (reasoning high)**',
            readme,
        )
        self.assertIn(
            '**Claude is not recommended as the execution runner for this '
            'project.**',
            readme,
        )
        self.assertIn(
            '| Claude Desktop Code tab, Environment Local, native Windows '
            'checkout | `claude-sonnet-4-6`; `effort=high` |',
            readme,
        )
        self.assertIn(
            'The later Sonnet 5, Haiku 4.5, and Opus 4.6 refusals are '
            'separate evidence',
            readme,
        )
        scheduling = readme.split('### Scheduling', 1)[1].split(
            '### View on Phone setup', 1
        )[0]
        codex_prompt = scheduling.split(
            'Use this scheduler prompt in Codex:', 1
        )[1].split(
            'Keep exactly one `TIMING_IDENTITY` line in the Codex '
            'scheduler\'s **Instructions**.', 1
        )[0]
        codex_declaration = (
            'TIMING_IDENTITY: runner=codex model=gpt-5.6-sol '
            'config=reasoning=high'
        )
        self.assertEqual(codex_prompt.count('TIMING_IDENTITY:'), 1)
        self.assertIn(codex_declaration, codex_prompt)
        self.assertNotIn('TIMING_IDENTITY: runner=claude', scheduling)
        self.assertIn(
            'Do not put it in the ordinary Description field, memory, or '
            'a separate scheduling guide',
            scheduling,
        )
        launch_boundary = (
            'Planning and progress stay internal. Never call, discover, or '
            'load `mark_chapter`, `TaskCreate`, `TaskUpdate`, `TaskList`, '
            'or `TaskGet`, and never call any framework chapter, planning, '
            'task-list, todo, progress, or phase tool at any point. Do not '
            'use `ToolSearch` for any such tool. Begin directly with the '
            'complete routine-file read below, and make no other '
            'model-authored tool call before the read-through-EOF finishes.'
        )
        for label, prompt in (
            ('README Codex prompt', codex_prompt),
        ):
            with self.subTest(prompt=label):
                self.assertEqual(prompt.count(launch_boundary), 1)
                self.assertLess(
                    prompt.index(launch_boundary),
                    prompt.index('TIMING_IDENTITY:'),
                )
                for required in (
                    'scheduler-injected `Automation memory:` line and path '
                    'are metadata, not a request',
                    'ignore them without opening the path',
                    'bounded chunks of at most 100 lines',
                    'Each pre-EOF tool call must read exactly one contiguous '
                    'bounded chunk of that file and nothing else',
                    'never combine it with memory, another file or command, '
                    'or a whole-file read',
                    'exact next unread line',
                    'final read to prove EOF',
                    'first in chunks of at most 50 lines',
                    'successively smaller sequential chunks',
                    'until every line and EOF are proven',
                    'never treat a partial read as complete',
                ):
                    self.assertIn(required, prompt)
        for forbidden in ('TIMING_IDENTITY', 'runner=', 'model='):
            self.assertNotIn(forbidden, launch_boundary)
        for required in (
            'Planning and progress stay internal',
            '`TaskCreate`',
            '`TaskUpdate`',
            '`TaskList`',
            '`TaskGet`',
            '`mark_chapter`',
            '`ToolSearch`',
            'at any point',
        ):
            self.assertIn(required, launch_boundary)
        self.assertIn(
            '**Model: GPT-5.6 Sol** maps to '
            '`model=gpt-5.6-sol`', codex_prompt
        )
        self.assertIn(
            '**Reasoning: High** maps to '
            '`config=reasoning=high`', codex_prompt
        )
        self.assertIn(
            'Use the complete maintained prompt above as the copyable '
            'source of truth',
            codex_prompt,
        )
        self.assertIn(
            'UI layout, task name/date, project/path, schedule, '
            'notifications, and other settings may vary', codex_prompt
        )
        self.assertNotIn(
            'claude-automation-part-a-setup.png', scheduling
        )
        self.assertNotIn(
            'Use this in the Claude Desktop Code Local scheduler',
            scheduling,
        )
        for required in (
            'Do not create or enable a Claude scheduled task',
            'Sonnet 5, Haiku 4.5, and Opus 4.6 refusals',
            'cannot be repaired by changing this scheduler prompt',
        ):
            self.assertIn(required, scheduling)
        self.assertIn(
            'synchronized with the runner, model, and configuration '
            'actually selected', scheduling
        )
        self.assertIn('does not switch the model', scheduling)
        self.assertIn(
            'Codex automation uses the exact OpenAI model ID '
            '`gpt-5.6-sol` and '
            '`config=reasoning=high`', scheduling
        )
        self.assertIn(
            'one exact, all-or-nothing tuple', scheduling
        )
        for required in (
            'one pre-helper self-report',
            'only framework-explicit identity',
            'direct current-task metadata remains strongest',
            'historical Claude runtime aliases',
            'Any unknown field, self-reported field, or conflict excludes '
            'the sample from primary fair comparisons',
            'no trading authority',
        ):
            self.assertIn(required, scheduling)

        guide_path = os.path.join(ROOT, 'CLAUDE-LOCAL-SCHEDULING.md')
        self.assertFalse(os.path.exists(guide_path))
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', readme)

        with open(os.path.join(ROOT, 'QUICKSTART.md'), encoding='utf-8') as f:
            quickstart = f.read().split(
                '## Timing for later scheduled runs', 1
            )[1]
        self.assertIn('[README scheduling](README.md#scheduling)', quickstart)
        self.assertIn('scheduler\'s **Instructions**', quickstart)
        self.assertNotIn('TIMING_IDENTITY: runner=', quickstart)
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', quickstart)
        self.assertIn(
            'source-specific **Reference run duration** can still be '
            'attached',
            quickstart,
        )
        for required in (
            'one structured self-report',
            'Complete direct task metadata',
            'Historical Claude runtime aliases remain available only so '
            'old performance records retain their provenance',
            'Any unknown field, self-reported field, or conflict excludes '
            'the record from primary fair-comparison cohorts',
            'Do not create or enable a Claude schedule',
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
            'Claude attempted an invalid bulk `TaskCreate` after reading the',
            'Claude did not retry',
            'category wording alone was insufficient',
            'forbid discovering or loading them through `ToolSearch`',
            'never depends on `TIMING_IDENTITY`',
        ):
            self.assertIn(required, incident)

        with open(
            os.path.join(ROOT, 'robinhood-momentum-routine-autonomous.md'),
            encoding='utf-8',
        ) as f:
            routine = f.read()
        for required in (
            'Planning and progress stay internal',
            'Never call, discover, or load `mark_chapter`, `TaskCreate`, '
            '`TaskUpdate`, `TaskList`, or `TaskGet`',
            'never call any framework chapter, planning, task-list, todo, '
            'progress, or phase tool at any point',
            'Do not use `ToolSearch` for any such tool',
            '`run_lifecycle.py` helper is the only run-phase recorder',
            'This rule is unconditional.',
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
            'reason: "projection_publication_failed"',
            "must never repeat the lifecycle event",
            "-RecoverActiveContext",
            "exact raw `RUN_LOCK_TOKEN` remains retained",
            "exactly these twelve fields",
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
            "Before it, require the report's bare name to equal "
            "`EXPECTED_REPORT_FILE`", coordination
        )
        self.assertIn(
            "never release and then create, rename, rewrite, or repair",
            coordination,
        )
        for required in (
            "run_lifecycle.py acquire-bind-context --invocation-id "
            "'<INVOCATION_ID>'",
            '`action: "bind-context"`',
            "`python` exactly equal to `PYTHON_EXE`",
            "stores only a SHA-256 ownership binding, never the raw token",
            "Any other nonzero, missing/unreadable/malformed JSON",
        ):
            self.assertIn(required, coordination)
        lifecycle_finish = report.split(
            "**Finalize owned lifecycle with release-finish:**", 1
        )[1].split("**AUTOMATION MEMORY IS DISABLED", 1)[0]
        self.assertIn(
            "**Finalize owned lifecycle with release-finish — execute now:**",
            report,
        )
        self.assertIn("--report-file <EXPECTED_REPORT_FILE>", lifecycle_finish)
        self.assertIn("--status-file <EXPECTED_STATUS_FILE>", lifecycle_finish)
        self.assertIn(
            "must never trigger a post-release rewrite or a second "
            "terminalization",
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
            self.assertIn("retained `PYTHON_EXE`", section)
            if phase == "SECOND":
                self.assertIn("exact private `RUN_LOCK_TOKEN`", section)
                self.assertIn("run_lifecycle.py enter-second", section)
                self.assertIn("renews the lease", section)
                self.assertIn(
                    "Never issue a standalone SECOND `run_lock.py renew`",
                    section,
                )
            else:
                self.assertIn("exact `RUN_LOCK_TOKEN`", section)
                self.assertIn("lease renewal", section)
        second = routine.split("**SECOND ", 1)[1].split(
            "**THIRD ", 1
        )[0]
        self.assertIn("The FIRST renewal does not satisfy SECOND", second)

        final_refresh = routine.split(
            "**FINAL STATUS REFRESH", 1
        )[1].split(
            "**Report content:**", 1
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
            "retry MUST repeat the identical REALIZED P&L PAYLOAD above",
            final_refresh,
        )
        self.assertIn("REALIZED P&L PAYLOAD", routine)
        self.assertIn(
            "`start_date` and `end_date` are NOT arguments of this call",
            routine,
        )
        self.assertIn("InvalidArgument: un-specified asset class", routine)
        self.assertIn(
            "`broker_snapshot.py` accepts exactly nine actions", routine
        )
        self.assertIn("never hyphenate it onto the action", routine)
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

    def test_routine_requires_complete_stateless_read_before_any_action(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        input_heading = (
            "### INPUT BOUNDARY — complete, stateless routine read before "
            "all action"
        )
        input_start = routine.index(input_heading)
        description_start = routine.index("**Description:**")
        launch_start = routine.index(
            "### LAUNCH BOUNDARY — no framework planning tools"
        )
        runtime_start = routine.index("## Runtime requirement — model")
        self.assertLess(input_start, description_start)
        self.assertLess(description_start, launch_start)
        self.assertLess(launch_start, runtime_start)
        self.assertEqual(routine.count(input_heading), 1)

        memory_policy = routine[input_start:description_start]
        self.assertIn("every run is stateless", memory_policy)
        self.assertIn(
            "scheduler-supplied automation-memory path or content",
            memory_policy,
        )
        self.assertIn(
            "Never read, open, create, edit, append to, or replace "
            "`memory.md`",
            memory_policy,
        )
        self.assertIn("even when the scheduler advertises or injects one", memory_policy)
        self.assertIn("never call a framework memory tool", memory_policy)
        self.assertIn(
            "Memory is not a recovery, progress, or telemetry channel",
            memory_policy,
        )
        self.assertIn("verified report/status artifacts", memory_policy)
        for required in (
            "LOAD THIS ENTIRE FILE BEFORE ACTING",
            "sequentially from line 1",
            "bounded chunks of at most 100 lines",
            "Each pre-EOF tool call must read exactly one contiguous bounded "
            "chunk of this file and nothing else",
            "never combine a chunk with `memory.md`, another file, another "
            "command, or a whole-file read",
            "exact next unread line through EOF",
            "final read to prove that EOF was reached",
            "re-read the missing interval first in bounded chunks of at "
            "most 50 lines",
            "successively smaller sequential chunks until every line and "
            "EOF are proven",
            "make no model-authored tool call except the next bounded read "
            "of this same file",
            "do not self-identify",
            "resolve or launch Python",
            "invoke a helper or broker connector",
            "read another file",
            "write any file or state",
            "an early chunk never authorizes an early action",
            "CONTIGUOUS READ CURSOR — EXACT",
            "initialize `NEXT_ROUTINE_LINE = 1`",
            "requested first line to equal that exact integer",
            "every returned line number to be consecutive",
            "only then set `NEXT_ROUTINE_LINE = <last returned line> + 1`",
            "If a gap is ever detected",
        ):
            self.assertIn(required, memory_policy)

        launch_policy = routine[launch_start:runtime_start]
        for required in (
            "Planning and progress stay internal",
            "Never call, discover, or load `mark_chapter`, `TaskCreate`, "
            "`TaskUpdate`, `TaskList`, or `TaskGet`",
            "never call any framework chapter, planning, task-list, todo, "
            "progress, or phase tool at any point",
            "Do not use `ToolSearch` for any such tool",
            "`run_lifecycle.py` helper is the only run-phase recorder",
            "This rule is unconditional.",
        ):
            self.assertIn(required, launch_policy)

        self.assertIn(
            "Immediately after the complete bounded routine-file read "
            "reaches EOF",
            routine,
        )
        self.assertNotIn(
            "Immediately after the routine-file read returns",
            routine,
        )
        self.assertIn(
            "**STATELESSNESS REMINDER — AUTOMATION MEMORY REMAINS "
            "DISABLED:**",
            routine,
        )

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("Treat every run as stateless", readme)
        self.assertIn('Use this scheduler prompt in Codex', readme)
        self.assertNotIn(
            'Use this in the Claude Desktop Code Local scheduler', readme
        )
        self.assertEqual(readme.count("do not call a framework memory tool"), 1)
        self.assertEqual(
            readme.count(
                "scheduler-injected `Automation memory:` line and path are "
                "metadata, not a request"
            ),
            1,
        )
        self.assertNotIn("never append scan or account details", readme)

    def test_every_contiguous_100_line_routine_block_is_at_most_40_kib(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            "rb",
        ) as f:
            lines = f.read().splitlines(keepends=True)

        self.assertGreaterEqual(len(lines), 100)
        limit = 40 * 1024
        oversized = []
        for start in range(len(lines) - 99):
            byte_count = len(b"".join(lines[start : start + 100]))
            if byte_count > limit:
                oversized.append((start + 1, byte_count))
        self.assertEqual(
            oversized,
            [],
            "100-line routine block(s) exceed the 40 KiB UTF-8 read bound",
        )

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

        scan_contract = routine.split(
            "4. Resolve and validate the saved scan deterministically", 1
        )[1].split("6. `run_scan`", 1)[0]
        for required in (
            "connector_contract.py scan --scratch '<scratch>' --source-purpose "
            "scan-definition --title '<exact SCAN_TITLE>'",
            "scalar `sorting`",
            "`sort_valid: true` means the saved scalar is already exactly "
            "`\"Relative volume desc\"`",
            "`update_scan_config` MUST NOT be called",
            "call `update_scan_config` at most once",
            "sorting_column: \"Relative volume\"",
            "sorting_direction: \"desc\"",
            "connector_contract.py scan-update",
            "returned `data.result.sorted_by`",
            "Never retry this mutation, re-read the scans",
        ):
            self.assertIn(required, scan_contract)

        scan_phase = routine.split("6. `run_scan`", 1)[1].split("**FOURTH", 1)[0]
        filter_command = next(
            line for line in scan_phase.splitlines()
            if 'filter_scan.py --' in line
        )
        self.assertIn('<PYTHON_EXE>', filter_command)
        self.assertIn('--scratch', filter_command)
        self.assertIn('--scan-purpose run-scan', filter_command)
        self.assertIn(
            '--expected-constants-sha256 '
            "'<startup constants_receipt.source_sha256>'",
            filter_command,
        )
        for forbidden_flag in (
            '--price-min', '--price-max', '--min-rel-volume',
            '--min-abs-pct-change', '--top-n',
        ):
            self.assertNotIn(forbidden_flag, filter_command)
        self.assertNotIn('py -3 filter_scan.py', filter_command)
        self.assertNotIn('python3 filter_scan.py', filter_command)
        self.assertIn("--json-out <scratch>/working-list.json", scan_phase)
        self.assertIn("NEW session-scoped scratch directory", scan_phase)
        self.assertIn("Machine-readable handoff (REQUIRED)", scan_phase)
        for required in (
            "successful `filter_scan.py` stdout is exactly one compact JSON "
            "receipt",
            "carries the complete validated counters and TOP_N-bounded "
            "unrounded `working_list` inline",
            "Consume that parsed stdout receipt directly inside the same "
            "composed operation",
            "durable audit artifact only and has no downstream authority",
            "SCAN FILTER RECEIPT BINDING — EXACT",
            'filterReceipt.action === "filter-scan"',
            "filterReceipt.scratch === state.receipt.scratch",
            "filterReceipt.scratch_id === state.receipt.scratch_id",
            'filterReceipt.scan_selector_kind === "purpose"',
            'filterReceipt.scan_purpose === "run-scan"',
            "filterReceipt.scan_source_sha256 === "
            "commitReceipt.source_sha256",
            "filterReceipt.constants_source_sha256 === "
            "constantsReceipt.source_sha256",
            "filterReceipt.price_min === "
            "constantsReceipt.values.PRICE_MIN",
            "filterReceipt.price_max === "
            "constantsReceipt.values.PRICE_MAX",
            "filterReceipt.min_rel_volume === "
            "constantsReceipt.values.MIN_REL_VOLUME",
            "filterReceipt.min_abs_pct_change === "
            "constantsReceipt.values.MIN_ABS_PCT_CHANGE",
            "filterReceipt.top_n === topN",
            'filterReceipt.working_list_file === "working-list.json"',
            "Number.isSafeInteger(filterReceipt.byte_count)",
            "sha256.test(filterReceipt.sha256)",
            "store(STATE_KEY, {...state, working_list_receipt: "
            "filterReceipt})",
            "SOLE authority for candidate data and scan counts",
            "sole complete schema/type/range authority for this receipt and "
            "its inline rows",
            "exact ticker/visible-Symbol equality",
            "symbol uniqueness, and non-increasing relative-volume order",
            "Runner glue deliberately does not count fields, call "
            "`Object.keys`, recreate a ticker regular expression, loop over "
            "numeric fields, check ordering/duplicates/counters",
            "store and consume the complete receipt unchanged",
        ):
            self.assertIn(required, scan_phase)
        scan_binding_code = scan_phase.split(
            "**SCAN FILTER RECEIPT BINDING — EXACT:**", 1
        )[1].split("```javascript", 1)[1].split("```", 1)[0]
        for forbidden in (
            "Object.keys", "canonicalTicker", "exactRowKeys",
            "seenSymbols", "previousRelativeVolume", "for (const row",
            "filterReceipt.total_items", "filterReceipt.rows_returned",
            "filterReceipt.rows_skipped", "filterReceipt.passed_filters",
            "filterReceipt.working_list.length",
        ):
            self.assertNotIn(forbidden, scan_binding_code)
        for forbidden in (
            "after a successful script exit, read "
            "`<scratch>/working-list.json` as JSON",
            "formatted stdout table is diagnostic-only",
            "ReadAllText('<scratch>/working-list.json')",
        ):
            self.assertNotIn(forbidden, scan_phase)
        self.assertIn("skip the entry phase (Steps 8–12)", scan_phase)
        self.assertIn("deterministic output schema/value checks fail", scan_phase)
        self.assertIn("do NOT fall back to formatted stdout, a stale file, or ad-hoc filtering", scan_phase)
        self.assertIn("empty `working_list: []` is valid", scan_phase)
        self.assertIn("standard MCP envelope at `structuredContent.data.result`", scan_phase)
        self.assertIn(
            "skips a row whose `Volume` is absent/non-finite or whose exact "
            "canonical `ticker` differs from exact canonical "
            "`columns.Symbol`",
            scan_phase,
        )
        self.assertIn("never call `run_scan` again", scan_phase)
        self.assertIn("startup-bound `SOURCE_ROOT`", scan_phase)
        self.assertIn("`filter_scan.py --scan-purpose run-scan`", scan_phase)
        self.assertIn("completed `fileChange` / file-edit / apply-patch capability", scan_phase)
        self.assertIn("same composed tool operation", scan_phase)
        self.assertIn("POST-BIND COMPOSED JSON SAVE RECIPE", scan_phase)
        self.assertIn(
            "load `rhmra.bootstrap-state.v1` and "
            "`rhmra.transport-state.v1`",
            scan_phase,
        )
        self.assertIn("COMPLETE `fullToolResult`", scan_phase)
        self.assertIn("zero-prefix/zero-decoration", scan_phase)
        self.assertIn("any `text(...)`, `yield_control`", scan_phase)
        self.assertIn(
            'call self-correlating `commit-source` with the same purpose and '
            'no reservation-ID argument',
            scan_phase,
        )
        self.assertIn(
            "invoke `filter_scan.py --scan-purpose run-scan` with loaded scratch",
            scan_phase,
        )
        self.assertIn("compact path-free `working-list-state-bound`", scan_phase)
        scan_save = scan_phase.split(
            '**Save once, then reuse — atomic transport is REQUIRED:**', 1
        )[1]
        scan_order = (
            'load `rhmra.bootstrap-state.v1` and '
            '`rhmra.transport-state.v1`',
            'call `reserve-source` for purpose `run-scan`',
            'only then await `run_scan`',
            'write it once to the reservation receipt\'s exact source variable',
            'call self-correlating `commit-source` with the same purpose and '
            'no reservation-ID argument',
            'invoke `filter_scan.py --scan-purpose run-scan`',
            'run the exact receipt binding above against that same commit '
            'receipt',
            'store the complete validated filter receipt',
        )
        scan_positions = [scan_save.index(marker) for marker in scan_order]
        self.assertEqual(scan_positions, sorted(scan_positions))
        self.assertIn(
            "do not run `TextEncoder`, an ad-hoc byte counter, add a "
            "BOM/prefix, or perform another path/save experiment",
            scan_phase,
        )
        self.assertIn(
            "A save denial, failed commit, or purpose mismatch is terminal "
            "for the entire run as "
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
        for required in (
            '"symbols": [<that batch\'s exact symbols>]',
            '"interval": "day"',
            '"bounds": "regular"',
            '"start_time": "<START CLOCK historicals_start_time copied '
            'byte-for-byte>"',
            'Never send `span`, `end_time`, or `account_number`',
            'exactly one immediate retry',
            'byte-identical broker payload',
            'Never add, remove, reorder, or change an argument',
            'successful response is committed once and is never refetched',
        ):
            self.assertIn(required, prefilter)
        self.assertIn("Only the FINAL RSI-enabled `evaluate_candidates.py --json-out`", routine)
        self.assertIn("Transient JSON handoffs are deliberately different", routine)
        self.assertIn(
            "A save denial or unreadable bound file is terminal for the entire "
            "run as `snapshot-failure` / `snapshot-write-failed`",
            routine,
        )
        self.assertIn(
            "Any unknown, pending, aborted, unbound, alternate-root, nested, "
            "missing, changed, or "
            "unreadable input is run-level `snapshot-failure` / "
            "`snapshot-write-failed`",
            routine,
        )
        self.assertIn(
            "correctly committed and strictly read input that later fails "
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

    def test_routine_pins_connector_contracts_and_truthful_recovery(self):
        with open(
            os.path.join(ROOT, "robinhood-momentum-routine-autonomous.md"),
            encoding="utf-8",
        ) as f:
            routine = f.read()

        portfolio = routine.split(
            "**Deterministic portfolio normalization", 1
        )[1].split("**PRE-SECOND", 1)[0]
        for required in (
            "connector_contract.py portfolio --scratch '<scratch>' "
            "--source-purpose '<committed-purpose>'",
            "values.total_value",
            "values.cash",
            "values.equity_value",
            "values.buying_power",
            "FIRST completion boundary",
            "bind `FIRST_COMPLETE`",
            "MUST NOT be fetched again",
            "Producer authority is final",
            'The exact strings `"0"`, `"0.0"`, and `"0.0000"` are valid',
            "positivity/nonzero logic, truthiness, or an all-zero rejection",
        ):
            self.assertIn(required, portfolio)

        pre_second = routine.split(
            "**PRE-SECOND ENTRY-FEASIBILITY GATES", 1
        )[1].split("When ANY pre-SECOND gate skips entry", 1)[0]
        for required in (
            "SPY red-day gate — deterministic quote contract",
            "connector_contract.py quote --scratch '<scratch>' "
            "--source-purpose spy-red-check --symbol SPY",
            '`action: "quote"`, and `ok: true`',
            "below_previous_close",
            "change_percent_display",
            "SPY $X vs prev close $Y ($Z%)",
            "complete signed Z string",
            "Do not access or probe `content`, `structuredContent`, `data`, "
            "`results`, `quote`, `last_trade_price`, or `previous_close`",
            "successful committed response whose helper contract fails is "
            "not fetched again",
        ):
            self.assertIn(required, pre_second)

        order_handling = routine.split(
            "### ORDER HANDLING — AUTONOMOUS, WITH NOTIFICATION", 1
        )[1].split("### DRY RUN", 1)[0]
        for required in (
            "connector_contract.py review --scratch '<scratch>' "
            "--source-purpose '<committed-review-purpose>'",
            "--symbol '<exact symbol>' --side '<buy|sell>' "
            "--order-type '<market|limit|stop_market>'",
            "--market-hours '<regular_hours|extended_hours>' "
            "--time-in-force '<gfd|gtc>'",
            "exactly one of `--quantity '<exact quantity>'` or "
            "`--dollar-amount '<exact amount>'`",
            "response-bound exact symbol/side/order type and amount/price",
            "connector response does not echo session/TIF",
            "The current review schema is direct `data.order_checks`, an "
            "object; there is no `alerts` array",
            "must never access, probe, or fallback among raw `content`, "
            "`structuredContent`, `data`, `result`, `alerts`, `order_checks`",
            "An empty `order_checks: {}` produces `clean: true`",
            "Every nonempty object produces `clean: false`",
            "unknown never means clean",
            "successful committed review whose helper contract fails is not "
            "fetched again",
            "explicit review transport error may invoke `abort-source` with "
            "fixed reason `connector-failed`",
            "do not retry that review call",
            "review-specific exception to the generic read retry",
            "Only `clean: true` authorizes the dependent order path",
            "helper `alert_type` exactly equal to "
            "`EQUITY_FRACTIONALLY_UNTRADABLE_ERROR_BUY`",
            "helper `alert_type` exactly equal to "
            "`EQUITY_MAX_SELL_SHARES_EXCEEDED`",
            "unchanged PREPARED profit-take sell under a new committed source "
            "purpose",
            "cancellation recovery independently proved exact terminal "
            "`cancelled` with zero cumulative fill",
            "A recovered `partially_filled_rest_cancelled` stop makes the "
            "prepared sell quantity stale",
            "abandon it, refresh the position and order baseline",
            "These are new logical reviews after valid broker responses, not "
            "retries of a failed transport call",
            "Normally prepare the durable intent only after a clean "
            "`connector_contract.py review` receipt whose response-bound "
            "fields and helper-validated session/TIF match",
            "matching that unchanged prepared payload before `begin`",
        ):
            self.assertIn(required, order_handling)
        self.assertNotIn("Array.isArray(review.alerts)", routine)
        for required in (
            "For broker reads, retry the failed call exactly once",
            "`review_equity_order` is the named exception governed by ORDER "
            "HANDLING",
            "one broker attempt per logical review and no transport retry",
            "two new logical reviews allowed after specific valid broker "
            "checks",
            "Never apply this generic retry paragraph to "
            "`review_equity_order`, `place_equity_order`, or "
            "`cancel_equity_order`",
            "a review abort fails its dependent order path without another "
            "review call",
        ):
            self.assertIn(required, routine)

        pre_place = routine.split(
            "After `review_equity_order` has produced a clean", 1
        )[1].split("### REPORT", 1)[0]
        for required in (
            "response-bound fields and helper-validated session/TIF match the "
            "complete intended payload",
            "merely absent guessed alert is never a clean-review receipt",
            "cannot authorize placement",
        ):
            self.assertIn(required, pre_place)

        evaluation = routine.split(
            "6. Run the authoritative lifecycle-bound evaluation using ONLY", 1
        )[1].split("Only these deterministic failures", 1)[0]
        for required in (
            "const evaluationCommandParts = Object.freeze([",
            "const evaluationCommand = evaluationCommandParts.join(\"\");",
            "Never append, prepend, reassign, or otherwise mutate "
            "`evaluationCommand`",
            "empty frozen `quoteSegments` array",
        ):
            self.assertIn(required, evaluation)
        self.assertNotIn("evaluationCommand +=", routine)

        for required in (
            "--phase entry-scan",
            "--phase entry-evaluation",
            "Durable failure attribution",
            "FAILURE_ORIGIN_PHASE",
            "failure origin unavailable",
            "constants_receipt.values.DRY_RUN",
            "do not author or run a separate cleanup/finalization cell",
        ):
            self.assertIn(required, routine)

        first = routine.split("**FIRST — manage what I already hold", 1)[1].split(
            "**PRE-SECOND", 1
        )[0]
        for required in (
            "**FIRST deterministic positions contract:**",
            "**CLOSED FIRST POSITIONS ACTION MATRIX:**",
            "first-positions-<zero-based page>",
            "connector_contract.py page --scratch '<scratch>'",
            "connector_contract.py first-positions-set --scratch '<scratch>'",
            "repeated `--source-purpose '<first-positions-N[-retry]>'`",
            "repeated `--request-cursor <FIRST|exact prior helper next_cursor>`",
            '`action: "first-positions-set"`, `ok: true`',
            "`rows` array",
            "`symbol`, `quantity`, `intraday_quantity`, and `average_buy_price`",
            "Use only this final first-positions-set receipt's rows",
            "Never\nmodel-dedupe rows",
            "`structuredContent.data.results`",
            "connector's positions array is not named `results`",
            "Missing/null/empty `next`",
            "must not be fetched again",
            "`snapshot-failure` / `snapshot-validation-failed`",
            "`source_file_missing`, `source_file_changed`, or",
            "`source_retry_not_authorized`",
            "`request_binding_invalid`",
            "`pagination_stopped`",
        ):
            self.assertIn(required, first)
        first_matrix = first.split(
            "**CLOSED FIRST POSITIONS ACTION MATRIX:**", 1
        )[1].split("After and only after the helper proves a terminal page", 1)[0]
        for required in (
            "exact CODEX\nLATER TOKEN PRECONDITION",
            "successful normal\npath, the only local actions are `reserve-source`",
            "`commit-source`, and\n`connector_contract.py page`",
            "only exceptional local actions are\n`abort-source`",
            "authorized retry `reserve-source --retry-of` immediately after",
            "`lookup-source` after an interrupted/uncertain boundary",
            "FIRST MUST NOT call `broker_snapshot.py stage`",
            "read `output_paths` or `files`",
            "DAILY-LOSS staging rules do not apply to FIRST",
            "--purpose first-positions-<N>-retry --retry-of first-positions-<N>",
            "normal FIRST reserve receipt plus `retry_of` exactly equal to",
            "`first-positions-<N>`",
            "under its\nreservation lock",
            "failed reserve means no\nretry broker call",
            "There is no other suffix",
            "uses only the exact awaited `lookup-source` recovery matrix",
        ):
            self.assertIn(required, first_matrix)
        self.assertIn(
            "That `first-positions-set` call is FIRST's immediate and only "
            "continuation",
            first,
        )
        self.assertIn(
            "deliberately returns rows without any\nfile path",
            first,
        )
        self.assertIn(
            "`coordination-halt` /\n`coordination-state`",
            first,
        )
        self.assertNotIn("broker_snapshot.py stage --", first)
        self.assertNotIn(" --output ", first)
        self.assertNotIn("connector_contract.py positions-set", first)
        self.assertNotIn('`action: "positions-set"`', first)

        await_boundary = routine.split(
            "**CODEX POST-BIND LOCAL-COMMAND SHAPE — EXACT AND UNIVERSAL:**", 1
        )[1].split("Only a final read/scan connector failure, or", 1)[0]
        universal_wrapper_code = await_boundary.split(
            "```javascript", 1
        )[1].split("```", 1)[0]
        self.assertIn(
            "const initialProcess = await tools.exec_command(commandArguments);",
            await_boundary,
        )
        self.assertIn(
            'let output = String(process.output ?? "");',
            await_boundary,
        )
        self.assertIn("while (process.session_id !== undefined)", await_boundary)
        self.assertIn("await tools.write_stdin({", await_boundary)
        self.assertIn(
            "never assume a `drainCommand` declaration from an earlier cell "
            "exists",
            await_boundary,
        )
        self.assertNotIn(
            "await drainCommand(initialProcess)", await_boundary
        )
        self.assertIn(
            "return Object.freeze({process, receipt});", await_boundary
        )
        universal_wrapper_markers = (
            "const initialProcess = await tools.exec_command(commandArguments);",
            'let output = String(process.output ?? "");',
            "while (process.session_id !== undefined)",
            'output += String(next.output ?? "");',
            "process = next;",
            "process = Object.freeze({...process, output});",
            "receipt = JSON.parse(process.output)",
            "return Object.freeze({process, receipt});",
        )
        universal_wrapper_positions = [
            universal_wrapper_code.index(marker)
            for marker in universal_wrapper_markers
        ]
        self.assertEqual(
            universal_wrapper_positions, sorted(universal_wrapper_positions)
        )
        self.assertEqual(universal_wrapper_code.count('tools.exec_command('), 1)
        self.assertEqual(universal_wrapper_code.count('tools.write_stdin('), 1)
        for forbidden in ('text(', 'notify(', 'yield_control('):
            self.assertNotIn(forbidden, universal_wrapper_code)
        for forbidden in (
            "runHelper", "runJson", "{r, j}", "direct `commandResult.exit_code`",
            "direct `commandResult.output`",
        ):
            self.assertIn(forbidden, await_boundary)
        self.assertIn("`committed` + `consume`", await_boundary)
        self.assertIn("`aborted` + `none`", await_boundary)
        self.assertIn(
            "`coordination-halt` / `coordination-state`",
            await_boundary,
        )
        self.assertNotIn("drain(tools.exec_command(", routine)
        self.assertNotIn("drainCommand(tools.exec_command(", routine)
        javascript_blocks = "\n".join(
            routine.split("```javascript")[index].split("```", 1)[0]
            for index in range(1, len(routine.split("```javascript")))
        )
        self.assertNotIn("crypto.randomUUID()", javascript_blocks)
        for line in javascript_blocks.splitlines():
            if "tools.exec_command(" in line:
                self.assertRegex(line, r"\bawait tools\.exec_command\(")

        final_refresh = routine.split(
            "**FINAL STATUS REFRESH — ONE COHERENT POST-MUTATION GENERATION:**",
            1,
        )[1].split("**Finalize lifecycle", 1)[0]
        self.assertIn("Invoke `connector_contract.py portfolio`", final_refresh)
        self.assertIn(
            "Never re-fetch a successfully committed portfolio", final_refresh
        )
        self.assertIn(
            "`connector_contract.py portfolio` receipt is the SOLE source",
            final_refresh,
        )

        report = routine.split("Then produce the report:", 1)[1].split(
            "**Save the report to disk", 1
        )[0]
        for required in (
            "For every review helper receipt, report its "
            "`market_data_disclosure` even when the review is clean",
            "Whenever `order_checks` is nonempty, report the complete "
            "unchanged object and its `alert_type`",
            "including `alert_type: null` for an unknown check",
            "both the fractional correction's first receipt and its "
            "whole-share receipt",
            "both profit-take settlement reviews when that exception fires",
            "Do not reconstruct an `alerts` array",
        ):
            self.assertIn(required, report)
        self.assertIn("ordered **Recovery diagnostics** section", report)
        self.assertIn("number of extra broker calls", report)
        self.assertIn("whether a broker mutation might have occurred", report)
        self.assertIn(
            "Total tokens used: unavailable — runtime did not expose a "
            "complete total",
            report,
        )
        self.assertIn("Never estimate tokens", report)
        self.assertNotIn("rough `(estimate)`", report)

        summary = routine.split("### FINAL ON-SCREEN RUN SUMMARY", 1)[1]
        self.assertIn("`token usage unavailable`", summary)
        self.assertIn("Never estimate token usage", summary)
        self.assertNotIn("~N tokens (estimate)", summary)
        self.assertIn("including `::inbox-item`", summary)

        report_handoff = routine.split(
            "**CODEX REPORT MACHINE HANDOFF — REQUIRED, ONE CELL:**", 1
        )[1].split("**Non-Codex equivalent:**", 1)[0]
        report_code = report_handoff.split("```javascript", 1)[1].split(
            "```", 1
        )[0]
        for required in (
            'const state = load(STATE_KEY);',
            "state.context_receipt.expected_report_file",
            "const reportMarkdown = REPORT_JSON_STRING;",
            'const reportPath = state.project_root + separator + "run-reports"',
            'phase: "report-write-started"',
            'writingState.phase !== "report-write-started"',
            'phase: "report-persisted"',
            "const patchTarget = reportPath.replaceAll",
            "await tools.apply_patch(patch)",
            "quote(reportPath)",
            "readBack !== payload",
            "expected_report_file: expectedReportFile",
            "persisted: true",
            "read_back: true",
        ):
            self.assertIn(required, report_code)
        self.assertEqual(report_code.count("tools.apply_patch"), 1)
        self.assertEqual(report_code.count("tools.exec_command"), 1)
        self.assertLess(
            report_code.index("await tools.apply_patch(patch)"),
            report_code.index("await tools.exec_command(readArgs)"),
        )
        self.assertNotRegex(
            report_code,
            r"rhmra-log-\d{4}_\d{2}_\d{2}-\d{2}_\d{2}\.md",
        )
        status_handoff = routine.split(
            "**CODEX STATUS MACHINE HANDOFF — REQUIRED:**", 1
        )[1].split("If a later cell sees", 1)[0]
        self.assertIn(
            "state.report_binding.expected_report_file === "
            "state.context_receipt.expected_report_file",
            status_handoff,
        )
        self.assertIn('require `phase: "report-persisted"`', status_handoff)
        self.assertIn('phase: "status-published"', status_handoff)
        self.assertIn("status_binding", status_handoff)
        finalization = routine.split(
            "**Publish the STATUS SNAPSHOT", 1
        )[1].split("### PERFORMANCE TELEMETRY", 1)[0]
        self.assertIn("sees `status-candidate-saved`", finalization)
        self.assertIn(
            "continue directly to the one initial publish", finalization
        )
        self.assertIn("keep the already verified report immutable", finalization)
        self.assertIn('phase: "status-unavailable"', finalization)
        self.assertNotIn("update the report with the file-writing tool", finalization)

        with open(os.path.join(ROOT, "rules_version.py"), encoding="utf-8") as f:
            rules_version_helper = f.read()
        for filename in (
            "market_calendar.py",
            "market_clock.py",
            "daily_loss.py",
            "connector_contract.py",
            "filter_scan.py",
            "evaluate_candidates.py",
        ):
            self.assertIn(f'"{filename}"', rules_version_helper)

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
            'filtered `ALL_TOOLS` for the one canonical name',
            '`mcp__robinhood_mcp__get_accounts`',
            'Never infer absence from the initially displayed namespace',
            'Exactly one metadata match whose property is callable MUST proceed',
            'zero matches, duplicate matches, or one non-callable match',
            'Never choose among duplicates',
            '`get-accounts-zero-matches`',
            '`get-accounts-noncallable-match`',
            '`get-accounts-duplicate-matches`',
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
            'Unsupported Claude runner override',
            'Claude is not a supported execution runner for this project',
            'current tested Claude models refuse the required '
            'financial-trade mutations',
            'Pause or disable this Claude schedule and leave it disabled',
            'Migrate the task to the recommended Codex runner',
            'Do not repair or rerun this Claude automation',
        ):
            self.assertIn(required, connector)

        transport_binding = routine.split(
            '**SAVE TRANSPORT BINDING', 1
        )[1].split('### ORDER-INTENT JOURNAL', 1)[0]
        startup_recipe = transport_binding.split(
            '**EXACT CODEX STARTUP SAVE-AND-BIND RECIPE', 1
        )[1].split('```javascript', 1)[1].split('```', 1)[0]
        exact_name = (
            'const GET_ACCOUNTS_TOOL = '
            '"mcp__robinhood_mcp__get_accounts";'
        )
        discovery = startup_recipe.index('ALL_TOOLS.filter(entry =>')
        terminal_guard = startup_recipe.index(
            'if (toolResolutionFailure !== null)'
        )
        terminal_exit = startup_recipe.index('exit();', terminal_guard)
        call_started = startup_recipe.index(
            'phase: "account-call-started", canary_path: targetPath'
        )
        first_call = startup_recipe.index(
            'await resolvedGetAccountsTool({})'
        )
        self.assertIn(exact_name, startup_recipe)
        self.assertIn('entry.name === GET_ACCOUNTS_TOOL', startup_recipe)
        self.assertIn('getAccountsMatches.length === 0', startup_recipe)
        self.assertIn('getAccountsMatches.length > 1', startup_recipe)
        self.assertIn(
            'typeof getAccountsCandidate !== "function"', startup_recipe
        )
        self.assertIn(
            'const resolvedGetAccountsTool = '
            'getAccountsCandidate.bind(tools);',
            startup_recipe,
        )
        capture = startup_recipe.index(
            'const resolvedGetAccountsTool = '
            'getAccountsCandidate.bind(tools);'
        )
        self.assertLess(discovery, terminal_guard)
        self.assertLess(terminal_guard, terminal_exit)
        self.assertLess(terminal_exit, call_started)
        self.assertLess(capture, call_started)
        self.assertLess(call_started, first_call)
        self.assertEqual(
            startup_recipe.count('ALL_TOOLS.filter(entry =>'), 1
        )
        self.assertEqual(
            startup_recipe.count('tools[getAccountsMatches[0].name]'), 1
        )
        self.assertEqual(
            startup_recipe.count('getAccountsCandidate.bind(tools)'), 1
        )
        self.assertEqual(
            startup_recipe.count('await resolvedGetAccountsTool({})'), 2
        )
        self.assertNotIn('entry.description', startup_recipe)
        terminal_branch = startup_recipe[terminal_guard:terminal_exit]
        for required in (
            'phase: "terminal"',
            'canary_path: null',
            'failure_code: "account-scope-failed"',
            'tool_resolution: toolResolutionFailure',
            'action: "account-tool-resolution-failed"',
            'resolution: toolResolutionFailure',
        ):
            self.assertIn(required, terminal_branch)

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
            'Pending — no Sonnet 5 after-hours sample recorded',
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
            'Historical Claude-versus-Codex and model-version comparisons use '
            '**Comparable run duration**',
            'boundaries are identical on both runners',
            'same session class, workload path, and configuration cohort',
            'Do not create new production Claude cohorts unless a current '
            'release passes both the complete supervised mutation-path '
            'acceptance and the scheduled-run acceptance',
        ):
            self.assertIn(required, readme)
        self.assertIn('### Runner compatibility warning', readme)
        for required in (
            '**Claude is not recommended as the execution runner for this '
            'project.**',
            '2026-07-06 — Claude Sonnet 4.6',
            'Historical live run bought TDIC',
            '2026-08-19 11:17 PT — Claude Sonnet 5',
            'EPM cleared every deterministic gate',
            '`submit_attempts=0` and `outcome=never_submitted`',
            'misleadingly finalized as `completed`',
            '2026-08-19 11:35 PT — Claude Haiku 4.5',
            '2026-08-20 16:08 PT — Claude Opus 4.6',
            'Do not create or enable a Claude schedule',
            'Existing Claude schedules should remain paused or be disabled',
            'Use this scheduler prompt in Codex',
            'TIMING_IDENTITY: runner=codex model=gpt-5.6-sol',
            'both `place_equity_order` and `cancel_equity_order` '
            'approval-gated',
        ):
            self.assertIn(required, readme)
        self.assertNotIn(
            'Schedules must run at most once per hour', readme
        )
        self.assertNotIn('deterministic start delays', readme)
        self.assertNotIn(
            'host-native `py -3 run_lifecycle.py export`', readme
        )
        self.assertNotIn('Use this in the Claude Desktop Code Local scheduler', readme)
        self.assertNotIn('claude mcp add --transport http', readme)
        self.assertIn(
            'Read `./robinhood-momentum-routine-autonomous.md`', readme
        )
        self.assertNotIn(
            'Read `\\RobinhoodEquityTradingAgent\\robinhood-momentum-routine-autonomous.md`',
            readme,
        )
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', readme)
        with open(os.path.join(ROOT, 'QUICKSTART.md'), encoding='utf-8') as f:
            quickstart = f.read()
        self.assertIn(
            '**Claude is not a supported setup target.**', quickstart
        )
        self.assertIn(
            'Use Codex for setup, testing, and execution', quickstart
        )
        self.assertIn(
            'do not create or enable a Claude schedule', quickstart
        )
        self.assertIn('do not fall back to `/usr/bin/python3`', quickstart)
        self.assertIn('genuinely native Linux/macOS checkout', quickstart)
        self.assertIn('[README scheduling](README.md#scheduling)', quickstart)
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', quickstart)

        self.assertFalse(os.path.exists(
            os.path.join(ROOT, 'CLAUDE-LOCAL-SCHEDULING.md')
        ))

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
            '**Claude is not a supported setup target.**',
            'Current tested Claude models refuse the live '
            'financial-trade operations',
            'Use Codex for setup, testing, and execution',
            'do not create or enable a Claude schedule',
            'remove only that single connector',
            'add it back once',
            'verify `get_accounts` in another fresh task/session',
            'Never leave or create a duplicate',
            'before any broker work',
        ):
            self.assertIn(required, connector)

        self.assertNotIn('- **Claude Code:**', connector)

        for required in (
            'Never ask me to paste passwords, MFA codes, account numbers',
            'changing `DRY_RUN` from `false` to `true` if necessary',
            'writing the confirmed `AGENTIC_ACCOUNT_NAME`',
            'Never change `DRY_RUN` from `true` to `false`',
            're-run validation after either authorized edit',
            'Neither `place_equity_order` nor `cancel_equity_order` '
            'may be preapproved',
            'In Codex, keep both set to `Needs approval`',
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
            'genuinely native Linux/macOS checkout',
            'zero agentic-enabled accounts, stop',
            'if several exist, show only their display names',
            'must then resolve to exactly one account',
            'must be agentic enabled',
            'no default, partial-match, first-account, or '
            'account-number fallback',
            'same bound `PYTHON_EXE`',
            '`\'<PYTHON_EXE>\' -m unittest discover -s tests`',
            'Do not use a bare launcher or invent an ad-hoc '
            'serializer, path, ACL repair, or extra broker call',
            'first supervised entry-eligible run with '
            '`DRY_RUN = true` remains the runner-specific end-to-end '
            'proof of the real accounts canary, broker-response staging, '
            'and final scratch status candidate',
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
        self.assertIn('[README scheduling](README.md#scheduling)', quickstart)
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', quickstart)
        self.assertNotIn('TIMING_IDENTITY: runner=', quickstart)

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
        self.assertNotIn('CLAUDE-LOCAL-SCHEDULING.md', local_targets)

        with open(os.path.join(ROOT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        pre_live = readme.split(
            '## Testing before going live', 1
        )[1].split('## Deterministic layer', 1)[0]
        for required in (
            'both `place_equity_order` and `cancel_equity_order`',
            'In Codex, keep both `place_equity_order` and '
            '`cancel_equity_order` on **"Needs approval"**',
            'Neither mutation tool may be preapproved',
            'if both approval gates cannot be guaranteed',
            'stop before running the routine or enabling live trading',
        ):
            self.assertIn(required, pre_live)
        self.assertNotIn('control may be labeled **Auto**', pre_live)
        self.assertNotIn('inspect **Allowed permissions**', pre_live)

    def test_claude_runner_is_explicitly_unsupported(self):
        documents = {}
        for filename in (
            'README.md',
            'QUICKSTART.md',
            'robinhood-momentum-routine-autonomous.md',
            'INCIDENTS.md',
        ):
            with open(os.path.join(ROOT, filename), encoding='utf-8') as f:
                documents[filename] = f.read()

        readme = documents['README.md']
        compatibility = readme.split(
            '### Runner compatibility warning', 1
        )[1].split('## Strategy in one line', 1)[0]
        for required in (
            '**Claude is not recommended as the execution runner for this '
            'project.**',
            'currently tested Claude models refuse live financial-trade '
            'execution at a higher-priority model-policy boundary',
            '2026-07-06 — Claude Sonnet 4.6',
            'Historical live run bought TDIC, received a fill, and placed '
            'its protective stop',
            '2026-08-19 11:17 PT — Claude Sonnet 5',
            'EPM cleared every deterministic gate for a $301.79 buy',
            '`review_equity_order` returned no alerts',
            'No `place_equity_order` call',
            '`submit_attempts=0` and `outcome=never_submitted`',
            'misleadingly finalized as `completed`',
            '2026-08-19 11:35 PT — Claude Haiku 4.5',
            'no lifecycle start, broker execution, or run report',
            '2026-08-20 16:08 PT — Claude Opus 4.6',
            'no lifecycle start or Robinhood call',
            'The evidence audit covered 467 run reports, 35 current Claude '
            'project transcripts, and 586 older Claude local-agent '
            'transcripts',
            'ordinary strategy gates and infrastructure failures were '
            'excluded from this compatibility conclusion',
            'raw Claude transcripts remain local outside the repository',
            'run reports and order-intent database are local and gitignored',
            '[INCIDENTS.md](INCIDENTS.md#claude-runner-compatibility--'
            'newer-models-refused-live-order-execution)',
            'Treat Claude as unsupported for this project',
            'buy, sell, cancellation, protective-stop placement, and stop '
            'verification',
            'repeat the required behavior from a scheduled run',
        ):
            self.assertIn(required, compatibility)
        self.assertIn(
            'The currently recommended deployment candidate is '
            '**Codex Sol 5.6 (reasoning high)**',
            readme,
        )
        for required in (
            'Claude connector and scheduler setup is intentionally not '
            'provided',
            'Do not create or enable a Claude schedule for this project',
            'Existing Claude schedules should remain paused or be disabled',
            'use the recommended Codex setup above instead',
            'Do not create or enable a Claude scheduled task',
            'cannot be repaired by changing this scheduler prompt',
        ):
            self.assertIn(required, readme)

        quickstart = documents['QUICKSTART.md']
        for required in (
            '**Claude is not a supported setup target.**',
            'Current tested Claude models refuse the live '
            'financial-trade operations',
            'Use Codex for setup, testing, and execution',
            'do not create or enable a Claude schedule',
            'Open this project in **ChatGPT Desktop in Codex mode or '
            'Codex**',
            'When you later create a Codex scheduled task',
        ):
            self.assertIn(required, quickstart)

        routine = documents['robinhood-momentum-routine-autonomous.md']
        model_note = 'Use **Codex Sol 5.6 (high)**' + routine.split(
            'Use **Codex Sol 5.6 (high)**', 1
        )[1].split('\n', 1)[0]
        for required in (
            'Use **Codex Sol 5.6 (high)** for this routine',
            'Claude is not a recommended or supported deployment runner',
            'Sonnet 4.6 historically completed live buys and protective '
            'stops',
            'Sonnet 5, Haiku 4.5, and Opus 4.6',
            'higher-priority model-policy boundary despite explicit '
            'authorization',
            'Repository instructions cannot override that boundary',
            'Codex Luna 5.6 remains the efficient high-volume option',
            'not the current unattended recommendation',
        ):
            self.assertIn(required, model_note)
        for required in (
            'pause or disable that legacy Claude task and leave it disabled',
            'do not create or enable a replacement Claude schedule',
            'Migrate the schedule to the recommended Codex runner',
            '**Unsupported Claude runner override:**',
            'Claude is not a supported execution runner for this project',
            'Pause or disable this Claude schedule and leave it disabled',
            'Migrate the task to the recommended Codex runner',
            'Do not repair or rerun this Claude automation',
        ):
            self.assertIn(required, routine)

        incidents = documents['INCIDENTS.md']
        incident = incidents.split(
            '## CLAUDE RUNNER COMPATIBILITY — newer models refused live '
            'order execution', 1
        )[1].split('## The pattern across all of these', 1)[0]
        incident = re.sub(r'\s+', ' ', incident)
        for required in (
            '2026-08-19 11:17 Pacific, Claude Sonnet 5 refused a valid '
            'live EPM buy',
            '`DRY_RUN = false`',
            'EPM passed the liquidity, dip, spread, and RSI gates for a '
            '$301.79 buy',
            '`review_equity_order` returned no alerts',
            'never called `place_equity_order`',
            '`submit_attempts = 0`',
            '`outcome = never_submitted`',
            'assistant policy as the sole reason',
            'finalized lifecycle as `completed`',
            'Claude Haiku 4.5',
            'Claude Opus 4.6',
            'Neither session reached Robinhood or created a run artifact',
            '2026-07-06 Claude Sonnet 4.6 run bought TDIC',
            'not a strategy gate, account problem, connector failure, or '
            'general moral judgment about this particular trade',
            '467 run reports, 35 current Claude project transcripts, and '
            '586 older Claude local-agent transcripts',
            'Strategy gates such as SPY red/blackout and infrastructure '
            'failures such as snapshot or coordination errors were excluded',
            'Claude is no longer recommended or supported',
            'keep existing Claude schedules disabled',
            'live buy, sell, cancellation, filled-buy stop placement, and '
            'stop verification',
            'Claude transcripts remain local outside the repository',
            'reports and order-intent records remain local and gitignored',
        ):
            self.assertIn(required, incident)

        luna_incident = incidents.split(
            '## 2026-08-26 CODEX LUNA ORCHESTRATION COHORT — FAIL-CLOSED, '
            'NOT RELIABLE', 1
        )[1].split('## The pattern across all of these', 1)[0]
        luna_incident = re.sub(r'\s+', ' ', luna_incident)
        for required in (
            'Eight scheduled attempts',
            'one normal SPY-red completion and seven failed or materially '
            'degraded paths',
            'No attempt placed, cancelled, reviewed, or otherwise mutated a '
            'broker order',
            'did not clear, parse, store, and reload the required bootstrap '
            'state',
            '`equity_value: "0"`',
            'declared `evaluationCommand` with `const` and then used `+=`',
            'nonexistent `reservation.id`',
            'mixed a raw process returned by `runHelper` with a caller '
            'expecting `{process, receipt}`',
            'skipped routine lines 1600–1649',
            'manually probed the wrong MCP-envelope path',
            'self-correlates from immutable scratch plus purpose',
            '`connector_contract.py quote`',
            'moved from Luna to Sol high',
        ):
            self.assertIn(required, luna_incident)

        review_incident = incidents.split(
            '## 2026-08-27 09:19 PT CODEX SOL — VALID ORDER REVIEW, '
            'INVENTED ALERTS ENVELOPE', 1
        )[1].split('## The pattern across all of these', 1)[0]
        review_incident = re.sub(r'\s+', ' ', review_incident)
        for required in (
            'APYX passed every deterministic entry gate',
            'valid $301.79 market-buy review',
            '`order_checks: {}`',
            'required `review.alerts` to be an array',
            'No order intent was prepared or begun',
            'no placement or cancellation was attempted',
            '09:34 `overlap skipped` entry was not another defect',
            '`connector_contract.py review` now owns the committed review '
            'response',
            'treats only an empty object as clean',
            'validates/carries the request\'s non-echoed session and '
            'time-in-force',
            'matching clean helper receipt is mandatory before `begin` or '
            'placement and normally precedes `prepare`',
            'named profit-take exception prepares first',
            'pre-commit review transport failure aborts its still-empty '
            'reservation and fails that dependent order path without retry',
        ):
            self.assertIn(required, review_incident)

        for filename in ('README.md', 'QUICKSTART.md'):
            with self.subTest(no_claude_setup=filename):
                text = documents[filename]
                for forbidden in (
                    'Use this in the Claude Desktop Code Local scheduler',
                    'TIMING_IDENTITY: runner=claude',
                    'claude mcp add --transport http',
                    'Routines → New routine → Local',
                ):
                    self.assertNotIn(forbidden, text)
        self.assertNotIn('- **Claude Code:**', quickstart)

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
            "`review_equity_order`, `place_equity_order`, or "
            "`cancel_equity_order`",
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
        self.assertIn("reserve a unique `placement-response-*` purpose", valid_place_response)
        self.assertIn("--response-purpose <that purpose>", valid_place_response)
        self.assertIn("--transport-scratch <loaded scratch>", valid_place_response)
        self.assertIn("No random response path enters the command", valid_place_response)
        self.assertIn("transport validation", valid_place_response)
        self.assertIn("strict parse", valid_place_response)
        self.assertIn("semantic acknowledgement", valid_place_response)
        self.assertIn("`malformed_response`", valid_place_response)
        self.assertIn("`acknowledgement_failure`", valid_place_response)
        self.assertIn("ORDER-STATE HALT", valid_place_response)
        self.assertIn("never retry the save", valid_place_response)
        self.assertIn("switch purpose/path/writer", valid_place_response)
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
            'Persist historicals and complete quote/RSI responses through '
            'the startup-bound file-change facility and `SOURCE_ROOT`',
            phase,
        )
        self.assertIn(
            'save and commit every COMPLETE successful batch response UNMODIFIED under '
            'unique `candidate-quotes-0`, `candidate-quotes-1`, ... purposes',
            phase,
        )
        self.assertIn(
            'Pass all historical purposes after `evaluate_candidates.py '
            '--bars-purpose` and all quote purposes after its single '
            '`--quotes-purpose` flag in both evaluator passes',
            phase,
        )
        self.assertIn("Evaluator selector and candidate-set contract", phase)
        self.assertIn(
            "emit each selector exactly once followed by its complete ordered value list",
            phase,
        )
        self.assertIn(
            "--expected-symbols <remaining-symbol> [more symbols ...]", phase
        )
        self.assertIn(
            "defensively accumulates a repeated selector occurrence but rejects a duplicated value",
            phase,
        )
        self.assertIn("explicit `buy_candidate: false` row", phase)
        self.assertIn("`String(value)` before `replaceAll`", phase)
        self.assertNotIn(
            "const psq = value => \"'\" + value.replaceAll", routine
        )
        self.assertNotIn(
            "const shq = value => \"'\" + value.replaceAll", routine
        )
        self.assertIn(
            '`structuredContent.data.results[].quote.symbol`', phase
        )
        self.assertIn(
            'entry evaluation skipped: no eligible candidates remained after '
            'Step 8 prefilter',
            phase,
        )
        self.assertIn(
            'make no historicals, candidate-quote, evaluator, RSI, review, or '
            'entry-order call',
            phase,
        )
        self.assertIn('Do not create placeholder bars, quote, or gate inputs', phase)
        self.assertIn(
            'does not rewrite them to the pre-SECOND `not-evaluated` form',
            phase,
        )
        self.assertIn(
            'Never build, re-key, copy, or repair a ticker→quote map', phase
        )
        self.assertNotIn('build a JSON map of ticker', phase)
        self.assertIn(
            'commit the COMPLETE result UNMODIFIED', phase
        )
        self.assertIn('`rsi-0`, `rsi-1`, ...', phase)
        self.assertIn(
            'Never extract `series`, re-key a symbol, or build a combined map',
            phase,
        )
        self.assertIn(
            'unwraps the standard MCP envelope, reads `data.symbol`, '
            'validates the RSI series',
            phase,
        )
        self.assertIn(
            'abort its reservation and omit that name', phase
        )
        self.assertIn(
            'Do not fetch historicals, derive closes/RSI, or repair a '
            'malformed committed success',
            phase,
        )
        self.assertIn('fixed purpose `rsi-empty` containing exactly `{}`', phase)
        self.assertIn(
            '--rsi-purpose <every committed rsi-* purpose exactly once>',
            phase,
        )
        self.assertNotIn('rsi-fallback', phase)
        self.assertNotIn('--rsi-purpose rsi-input', phase)
        self.assertIn("candidate evaluation handoff failure", phase)
        self.assertGreaterEqual(
            phase.count('set `entry_phase: "halted"`'), 2
        )
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

    def test_august_28_incident_and_tested_on_are_truthful(self):
        with open(os.path.join(ROOT, "INCIDENTS.md"), encoding="utf-8") as f:
            incidents = f.read()
        cohort = incidents.split(
            "## 2026-08-28 CODEX TERRA EARLY COHORT", 1
        )[1].split("## The pattern across all of these", 1)[0]
        cohort_text = " ".join(cohort.split())
        for required in (
            "eight scheduled launches from 06:05 through 09:32 PT",
            "seven outright orchestration failures and one safely finalized "
            "but degraded scan path",
            "No audited attempt reviewed, prepared, began, placed, "
            "cancelled, or otherwise mutated a broker order",
            "A later 11:16 PT Terra run completed a flat-account SPY-red "
            "skip cleanly",
            "does not validate the failed boundary above",
            "strictly reservation-ID-bound",
            "purpose-only abort was explicitly rejected",
            "production command lines for `run_lock.py`, "
            "`run_lifecycle.py`, `daily_loss.py`, `market_clock.py`, and "
            "`order_intents.py` still exposed test clock overrides",
            "production CLIs now reject `--now-utc` and "
            "`--lifecycle-now-utc`",
            "Only exact `bind_context_failed`, `lease_released: true`, "
            "`compensation_recorded: true` evidence",
            "permits the narrow raw `coordination-halt` / "
            "`coordination-state` finish",
            "a `dip-buy` journal `begin` or same-ref `retry` still trusted "
            "the runner to have obeyed the DAILY-LOSS clear gate",
            "journal now calls the checked-in lifecycle entry authorizer "
            "internally with its exact private run token",
            "There is no production CLI bypass",
            "paused and changed from Terra/high to the maintained Sol/high "
            "candidate",
        ):
            self.assertIn(required, cohort_text)

        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        tested_on = readme.split("## Tested On", 1)[1].split(
            "## Guardrails", 1
        )[0]
        tested_on_text = " ".join(tested_on.split())
        for required in (
            "the eight early scheduled launches from 06:05 through 09:32 PT "
            "produced seven outright orchestration failures and one safely "
            "finalized but degraded scan path",
            "A later 11:16 PT SPY-red skip completed cleanly",
            "did not exercise the entry-eligible DAILY-LOSS, scan-evaluation, "
            "or order path",
            "The schedule was paused and returned to the Sol/high deployment "
            "candidate",
            "only a supervised entry-eligible run can accept the repaired "
            "path",
        ):
            self.assertIn(required, tested_on_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
