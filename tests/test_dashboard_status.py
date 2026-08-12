#!/usr/bin/env python3
"""Strict status-snapshot fallback contracts for the local dashboard."""

import http.client
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from status_snapshot import StatusSnapshotError, validate_status_snapshot


DASHBOARD_PATH = os.path.join(ROOT, "dashboard", "serve.py")
DASHBOARD_HTML = os.path.join(ROOT, "dashboard", "index.html")
DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "dashboard_status_serve", DASHBOARD_PATH
)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader
DASHBOARD = importlib.util.module_from_spec(DASHBOARD_SPEC)
DASHBOARD_SPEC.loader.exec_module(DASHBOARD)


def valid_status(run_start="2026-08-12T09:11:00-07:00"):
    return {
        "schema_version": 1,
        "run_start_pt": run_start,
        "rules_version": "abcd123",
        "dry_run": False,
        "session": "regular",
        "account": {
            "total_value": 1000.0,
            "cash": 1000.0,
            "buying_power": 1000.0,
            "equity_value": 0.0,
        },
        "realized_pnl_today": 0.0,
        "positions": [],
        "guards": {
            "circuit_breaker": "clear",
            "stop_fills_today": 0,
            "entry_phase": "ran",
            "entry_skip_reason": None,
        },
    }


# These field sets reproduce the two materially different malformed shapes
# Claude published on 2026-08-12. Values are intentionally innocuous: the
# structural drift alone must be enough to reject them.
CLAUDE_0813 = {
    key: None
    for key in (
        "schema_version", "invocation_id", "strategy_started_at_utc",
        "strategy_finished_at_utc", "rules_version", "constants_sha256",
        "dry_run", "account_name", "timing", "circuit_breakers", "scan",
        "working_list", "buy_candidates", "orders_placed", "orders_filled",
        "realized_pnl_today_usd", "portfolio_final", "gate_record_file",
        "outcome",
    )
}
CLAUDE_0813["schema_version"] = 1

CLAUDE_1013 = {
    key: None
    for key in (
        "invocation_id", "run_start_pt", "run_start_utc", "session", "mode",
        "dry_run", "rules_version", "account_type", "total_value", "cash",
        "equity_value", "buying_power", "positions_held", "circuit_breaker",
        "daily_pnl", "stop_count_today", "scan_total", "scan_returned",
        "filter_passed", "working_list", "pre_filter_dropped", "evaluated",
        "buy_candidates", "orders_placed", "fills", "strategy_start_utc",
        "strategy_end_utc", "report_file",
    )
}


class LatestStatusEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = self.temporary.name
        self.report_dir = os.path.join(self.repo, "run-reports")
        os.makedirs(self.report_dir)
        self.original_repo = DASHBOARD.REPO
        DASHBOARD.REPO = self.repo

        class QuietHandler(DASHBOARD.Handler):
            def log_message(self, format, *args):
                pass

        self.server = DASHBOARD.ThreadingHTTPServer(
            ("127.0.0.1", 0), QuietHandler
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        DASHBOARD.REPO = self.original_repo
        self.temporary.cleanup()

    def write_status(self, name, document):
        with open(
            os.path.join(self.report_dir, name), "w", encoding="utf-8"
        ) as handle:
            json.dump(document, handle, allow_nan=False)

    def request_json(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        connection.close()
        return response.status, json.loads(body)

    def latest(self):
        return self.request_json("/api/latest")

    def test_newest_valid_preserves_backward_compatible_response(self):
        name = "rhmra-status-2026_08_12-09_11.json"
        document = valid_status()
        self.write_status(name, document)

        status, response = self.latest()

        self.assertEqual(status, 200)
        self.assertEqual(response, {"filename": name, "data": document})

    def test_two_real_claude_shapes_fall_back_to_newest_valid(self):
        valid_name = "rhmra-status-2026_08_12-07_37.json"
        malformed_0813 = "rhmra-status-2026_08_12-08_13.json"
        malformed_1013 = "rhmra-status-2026_08_12-10_13.json"
        document = valid_status("2026-08-12T07:37:00-07:00")
        self.write_status(valid_name, document)
        self.write_status(malformed_0813, CLAUDE_0813)
        self.write_status(malformed_1013, CLAUDE_1013)

        status, response = self.latest()

        self.assertEqual(status, 200)
        self.assertEqual(response["filename"], valid_name)
        self.assertEqual(response["data"], document)
        self.assertEqual(
            response["warning"],
            {
                "code": "newest_snapshot_rejected",
                "rejected_filename": malformed_1013,
                "fallback_filename": valid_name,
                "rejected_count": 2,
            },
        )
        self.assertNotIn(self.repo, json.dumps(response))

    def test_all_invalid_returns_no_data_and_structured_warning(self):
        older = "rhmra-status-2026_08_12-08_13.json"
        newest = "rhmra-status-2026_08_12-10_13.json"
        self.write_status(older, CLAUDE_0813)
        self.write_status(newest, CLAUDE_1013)

        status, response = self.latest()

        self.assertEqual(status, 200)
        self.assertIsNone(response["filename"])
        self.assertIsNone(response["data"])
        self.assertEqual(response["warning"]["rejected_filename"], newest)
        self.assertIsNone(response["warning"]["fallback_filename"])
        self.assertEqual(response["warning"]["rejected_count"], 2)

    def test_index_strictly_partitions_duplicate_nonfinite_and_oversized_files(self):
        valid_name = "rhmra-status-2026_08_12-07_37.json"
        duplicate_name = "rhmra-status-2026_08_12-08_13.json"
        nonfinite_name = "rhmra-status-2026_08_12-09_35.json"
        oversized_name = "rhmra-status-2026_08_12-10_13.json"
        self.write_status(
            valid_name, valid_status("2026-08-12T07:37:00-07:00")
        )
        with open(
            os.path.join(self.report_dir, duplicate_name),
            "w", encoding="utf-8",
        ) as handle:
            handle.write('{"schema_version":1,"schema_version":1}')
        with open(
            os.path.join(self.report_dir, nonfinite_name),
            "w", encoding="utf-8",
        ) as handle:
            handle.write('{"schema_version":NaN}')
        with open(
            os.path.join(self.report_dir, oversized_name),
            "wb",
        ) as handle:
            handle.write(b" " * 1_000_001)

        status, response = self.request_json("/api/index")

        self.assertEqual(status, 200)
        self.assertEqual(
            response,
            {
                "status": [valid_name],
                "rejected_status": [
                    duplicate_name, nonfinite_name, oversized_name,
                ],
                "gates": [],
            },
        )

    def test_index_rejects_valid_schema_under_mismatched_filename(self):
        mismatched_name = "rhmra-status-2026_08_12-10_13.json"
        self.write_status(
            mismatched_name, valid_status("2026-08-12T09:11:00-07:00")
        )

        index_status, index = self.request_json("/api/index")
        latest_status, latest = self.latest()

        self.assertEqual(index_status, 200)
        self.assertEqual(
            index,
            {
                "status": [],
                "rejected_status": [mismatched_name],
                "gates": [],
            },
        )
        self.assertEqual(latest_status, 200)
        self.assertIsNone(latest["filename"])
        self.assertEqual(
            latest["warning"]["rejected_filename"], mismatched_name
        )

    def test_index_rejects_status_symlink(self):
        target = os.path.join(self.repo, "outside-valid.json")
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(
                valid_status("2026-08-12T10:13:00-07:00"),
                handle,
                allow_nan=False,
            )
        name = "rhmra-status-2026_08_12-10_13.json"
        link = os.path.join(self.report_dir, name)
        try:
            os.symlink(target, link)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation is unavailable: {exc}")

        status, index = self.request_json("/api/index")

        self.assertEqual(status, 200)
        self.assertEqual(index["status"], [])
        self.assertEqual(index["rejected_status"], [name])


class DashboardStatusClientContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DASHBOARD_HTML, encoding="utf-8") as handle:
            cls.source = handle.read()

    def function_source(self, name, next_name):
        start = self.source.index(f"function {name}")
        end = self.source.index(f"function {next_name}", start)
        return self.source[start:end]

    def test_real_claude_shapes_fail_shared_and_client_exact_field_contract(self):
        for document in (CLAUDE_0813, CLAUDE_1013):
            with self.subTest(keys=sorted(document)):
                with self.assertRaises(StatusSnapshotError):
                    validate_status_snapshot(document)

        fields_match = re.search(
            r"const STATUS_SNAPSHOT_FIELDS = \[(.*?)\];",
            self.source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(fields_match)
        client_fields = set(re.findall(r'"([a-z_]+)"', fields_match.group(1)))
        expected = set(valid_status())
        self.assertEqual(client_fields, expected)
        self.assertNotEqual(set(CLAUDE_0813), client_fields)
        self.assertNotEqual(set(CLAUDE_1013), client_fields)
        validator = self.function_source("validStatusSnapshot", "numberToCents")
        self.assertIn(
            "hasExactFields(snapshot, STATUS_SNAPSHOT_FIELDS)", validator
        )

    def test_client_timestamp_and_reason_rules_match_python_validator(self):
        timestamp = self.function_source(
            "validStatusTimestamp", "validStatusSnapshot"
        )
        self.assertIn("(?:\\.\\d{1,6})?", timestamp)
        self.assertIn("year % 400 === 0", timestamp)
        self.assertIn("day <= days[month - 1]", timestamp)
        validator = self.function_source("validStatusSnapshot", "numberToCents")
        self.assertIn("validStatusTimestamp(snapshot.run_start_pt)", validator)
        self.assertIn("guards.entry_skip_reason.trim().length > 0", validator)
        self.assertIn("[...guards.entry_skip_reason].length <= 1000", validator)
        self.assertIn("snapshot.positions.length > 1000", validator)
        self.assertIn("const quotedEquity = snapshot.positions.reduce", validator)
        self.assertIn("Math.abs(snapshot.account.equity_value - quotedEquity)", validator)
        self.assertIn("0.01 * Math.max", validator)

        finite = self.function_source("finiteNumber", "validStatusTimestamp")
        self.assertIn("Math.abs(value) <= Number.MAX_SAFE_INTEGER", finite)

        invalid_date = valid_status("2026-02-29T09:11:00-08:00")
        with self.assertRaises(StatusSnapshotError):
            validate_status_snapshot(invalid_date)

    def test_future_rejected_filename_cannot_replace_authoritative_run_day(self):
        selector = self.function_source('latestRunDay', 'renderLegacyRuns')
        self.assertIn('const authoritativeDays = [', selector)
        self.assertIn('...statusNames.map(name => name.slice(13, 23))', selector)
        self.assertIn('...lifecycleRecords.map(invocationDay).filter(Boolean)', selector)
        self.assertIn(
            'const days = authoritativeDays.length ? authoritativeDays : fallbackDays',
            selector,
        )
        runs = self.source[
            self.source.index('async function renderRuns'):
            self.source.index('function renderEras', self.source.index('async function renderRuns'))
        ]
        self.assertIn(
            'latestRunDay(statusNames, rejectedStatusNames, lifecycle)', runs
        )
        self.assertNotIn(
            '...rejectedStatusNames.map(name => name.slice(13, 23)),', runs
        )

    def test_all_invalid_clears_every_stale_account_surface(self):
        clear = self.function_source("clearSnapshot", "renderSnapshotFallbackWarning")
        for element in (
            '$("rules").textContent = ""',
            '$("staleness").textContent = ""',
            '$("account").innerHTML',
            '$("positions").innerHTML',
            '$("pnl-reconciliation").hidden = true',
            "latestPhoneView = null",
        ):
            self.assertIn(element, clear)
        refresh_start = self.source.index("async function refresh()")
        refresh = self.source[
            refresh_start:self.source.index("// runs land every", refresh_start)
        ]
        self.assertIn("else if (!latest.data)", refresh)
        self.assertGreaterEqual(refresh.count("clearSnapshot()"), 2)

    def test_fallback_warning_and_historical_statuses_are_fail_closed(self):
        warning = self.function_source(
            "renderSnapshotFallbackWarning", "renderMode"
        )
        self.assertIn('warning.code !== "newest_snapshot_rejected"', warning)
        self.assertIn("warning.rejected_filename", warning)
        self.assertIn("warning.fallback_filename", warning)
        self.assertIn("warning.rejected_count", warning)

        runs = self.source[
            self.source.index("async function renderLegacyRuns"):
            self.source.index("function renderEras", self.source.index("async function renderLegacyRuns"))
        ]
        self.assertGreaterEqual(
            runs.count("const valid = validStatusSnapshot(status)"), 2
        )
        self.assertIn("completed; status rejected", runs)
        self.assertIn('displayPhase = "unavailable"', runs)
        self.assertIn("no guard outcome was inferred from rejected data", runs)
        self.assertIn("rejectedStatusNames = []", runs)
        self.assertIn("statusRejected: true", runs)

        refresh_start = self.source.index("async function refresh()")
        refresh = self.source[
            refresh_start:self.source.index("// runs land every", refresh_start)
        ]
        self.assertIn("Array.isArray(idx.rejected_status)", refresh)
        self.assertIn(": []", refresh)


if __name__ == "__main__":
    unittest.main()
