#!/usr/bin/env python3
"""Focused contracts for the local run-performance dashboard surface."""

import http.client
import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DASHBOARD_PATH = os.path.join(ROOT, "dashboard", "serve.py")
DASHBOARD_HTML = os.path.join(ROOT, "dashboard", "index.html")
DASHBOARD_SPEC = importlib.util.spec_from_file_location(
    "dashboard_performance_serve", DASHBOARD_PATH
)
assert DASHBOARD_SPEC and DASHBOARD_SPEC.loader
DASHBOARD = importlib.util.module_from_spec(DASHBOARD_SPEC)
DASHBOARD_SPEC.loader.exec_module(DASHBOARD)


def performance_record(**overrides):
    record = {
        "invocation_id": "11111111-1111-4111-8111-111111111111",
        "lifecycle_started_at_utc": "2026-08-11T23:37:45Z",
        "lifecycle_finished_at_utc": "2026-08-11T23:42:01Z",
        "strategy_started_at_utc": None,
        "strategy_finished_at_utc": None,
        "session": "after-hours",
        "runner": "codex",
        "model": "luna-5.6",
        "configuration": "high",
        "identity_source": "run-metadata",
        "internal_recorded_at_utc": "2026-08-11T23:42:01Z",
        "task_duration_ms": None,
        "task_clock_source": None,
        "task_observed_at_utc": None,
        "routine_total_ms": 256000,
        "strategy_execution_ms": None,
        "routine_overhead_ms": None,
        "outside_lifecycle_ms": None,
        "total_overhead_ms": None,
    }
    record.update(overrides)
    return record


def performance_document(records):
    return {
        "schema_version": 1,
        "record_limit": 512,
        "record_count": len(records),
        "source_event_high_watermark": len(records),
        "records": records,
    }


class PerformanceEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = self.temporary.name
        os.makedirs(os.path.join(self.repo, "run-reports"))
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

    def request(self, path):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=2
        )
        try:
            connection.request("GET", path, headers={"Host": "127.0.0.1"})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def request_json(self, path):
        status, body = self.request(path)
        return status, json.loads(body)

    def create_private_placeholders(self):
        report_dir = os.path.join(self.repo, "run-reports")
        state_file = os.path.join(
            report_dir, os.path.basename(DASHBOARD.run_performance.DEFAULT_STATE_FILE)
        )
        projection_file = os.path.join(
            report_dir,
            os.path.basename(DASHBOARD.run_performance.DEFAULT_PROJECTION_FILE),
        )
        for path in (state_file, projection_file):
            with open(path, "wb") as handle:
                handle.write(b"private test placeholder")
        return state_file, projection_file

    def test_missing_performance_state_returns_empty_projection(self):
        status, document = self.request_json("/api/performance")
        self.assertEqual(status, 200)
        self.assertEqual(
            document,
            {
                "schema_version": 1,
                "record_limit": DASHBOARD.run_performance.PROJECTION_LIMIT,
                "record_count": 0,
                "source_event_high_watermark": 0,
                "records": [],
            },
        )

    def test_partial_projection_preserves_null_measurements(self):
        state_file, projection_file = self.create_private_placeholders()
        expected = performance_document([performance_record()])
        with mock.patch.object(
            DASHBOARD.run_performance,
            "validate_current_projection_read_only",
            return_value=expected,
        ) as validate:
            status, document = self.request_json("/api/performance")
        self.assertEqual(status, 200)
        self.assertEqual(document, expected)
        self.assertIsNone(document["records"][0]["task_duration_ms"])
        self.assertIsNone(document["records"][0]["strategy_execution_ms"])
        validate.assert_called_once_with(state_file, projection_file)

    def test_full_projection_returns_all_timing_components(self):
        self.create_private_placeholders()
        expected = performance_document([
            performance_record(
                task_duration_ms=363000,
                task_clock_source="codex-worked-for",
                task_observed_at_utc="2026-08-11T23:43:48Z",
                strategy_started_at_utc="2026-08-11T23:38:30Z",
                strategy_finished_at_utc="2026-08-11T23:40:30Z",
                strategy_execution_ms=120000,
                routine_overhead_ms=136000,
                outside_lifecycle_ms=107000,
                total_overhead_ms=243000,
            )
        ])
        with mock.patch.object(
            DASHBOARD.run_performance,
            "validate_current_projection_read_only",
            return_value=expected,
        ):
            status, document = self.request_json("/api/performance")
        self.assertEqual(status, 200)
        self.assertEqual(document, expected)
        timing = document["records"][0]
        self.assertEqual(timing["task_duration_ms"], 363000)
        self.assertEqual(timing["routine_total_ms"], 256000)
        self.assertEqual(timing["strategy_execution_ms"], 120000)
        self.assertEqual(timing["routine_overhead_ms"], 136000)

    def test_corrupt_performance_is_isolated_from_lifecycle_endpoint(self):
        self.create_private_placeholders()
        with mock.patch.object(
            DASHBOARD.run_performance,
            "validate_current_projection_read_only",
            side_effect=ValueError("performance projection is corrupt"),
        ):
            status, document = self.request_json("/api/performance")
        self.assertEqual(status, 500)
        self.assertIn("corrupt", document["error"])

        lifecycle_status, lifecycle = self.request_json("/api/runs")
        self.assertEqual(lifecycle_status, 200)
        self.assertEqual(lifecycle["records"], [])

    def test_private_performance_files_are_not_statically_served(self):
        state_file, projection_file = self.create_private_placeholders()
        for name in (os.path.basename(state_file), os.path.basename(projection_file)):
            status, _body = self.request("/run-reports/" + name)
            self.assertEqual(status, 403)


class PerformanceClientContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(DASHBOARD_HTML, encoding="utf-8") as handle:
            cls.source = handle.read()

    def test_performance_section_is_separate_and_uses_exact_terms(self):
        source = self.source
        runs = source.index('<div class="runs" id="runs">')
        performance = source.index('<section id="performance-section">')
        eras = source.index("Strategy realized P&amp;L by rules era")
        self.assertLess(runs, performance)
        self.assertLess(performance, eras)
        for term in (
            "Run performance",
            "Select a starred run above to view its timing details.",
            "End-to-end task",
            "Routine total",
            "Strategy execution",
            "Routine overhead",
            'getJSON("/api/performance")',
        ):
            self.assertIn(term, source)
        self.assertIn("performanceRecordsByInvocation.get(selectedPerformanceInvocationId)", source)
        self.assertIn("indexed.set(record.invocation_id, record)", source)
        self.assertIn("performanceRecordRow(record)", source)
        self.assertNotIn("Date.parse(right.lifecycle_started_at_utc)", source)
        self.assertNotIn("[...document.records].reverse()", source)
        self.assertNotIn("meat and potatoes", source.lower())

    def test_timing_chips_are_exact_id_accessible_controls(self):
        source = self.source
        runs_start = source.index("async function renderRuns")
        runs_end = source.index("function collectRunView", runs_start)
        runs = source[runs_start:runs_end]
        self.assertIn(
            "performanceRecordsByInvocation.has(invocationId)", runs
        )
        self.assertIn("availablePerformanceIds.add(invocationId)", runs)
        self.assertIn('<button type="button" class="run ', runs)
        self.assertIn('data-performance-invocation-id="${esc(invocationId)}"', runs)
        self.assertIn('aria-controls="performance"', runs)
        self.assertIn('aria-pressed="${selected ? "true" : "false"}"', runs)
        self.assertIn('aria-label="${esc(accessibleLabel)}"', runs)
        self.assertIn('class="run-performance-marker" aria-hidden="true">*</span>', runs)
        self.assertIn('<span class="sr-only">Timing details available.</span>', runs)
        self.assertIn("if (!hasPerformance)", runs)
        self.assertIn('return `<span class="run ', runs)
        self.assertNotIn("performanceStartLabel", runs)
        self.assertNotIn("lifecycle_started_at_utc) ===", runs)

    def test_selection_renders_one_record_and_survives_only_valid_today_match(self):
        source = self.source
        selected_start = source.index("function renderSelectedPerformance")
        selected_end = source.index("function renderPerformance(document)", selected_start)
        selected = source[selected_start:selected_end]
        self.assertIn("performanceRecordsByInvocation.get(selectedPerformanceInvocationId)", selected)
        self.assertIn("performanceRecordRow(record)", selected)
        self.assertNotIn(".map(record", selected)

        sync_start = source.index("function syncPerformanceSelection")
        sync_end = source.index("function runOutcomeText", sync_start)
        sync = source[sync_start:sync_end]
        self.assertIn("!availableInvocationIds.has(selectedPerformanceInvocationId)", sync)
        self.assertIn("selectedPerformanceInvocationId = null", sync)
        self.assertIn("renderSelectedPerformance()", sync)
        self.assertIn("syncPerformanceSelection(availablePerformanceIds)", source)
        self.assertIn("syncPerformanceSelection(new Set())", source)
        legacy_start = source.index("async function renderLegacyRuns")
        legacy_end = source.index("async function renderRuns", legacy_start)
        legacy = source[legacy_start:legacy_end]
        self.assertIn('no invocations yet', legacy)
        self.assertIn("syncPerformanceSelection(new Set())", legacy)

        handler = source[source.index("$('runs').addEventListener('click'"):
                         source.index("$('phone-share-button').addEventListener")]
        self.assertIn("button.run[data-performance-invocation-id]", handler)
        self.assertIn("performanceRecordsByInvocation.has(invocationId)", handler)
        self.assertIn("selectedPerformanceInvocationId = invocationId", handler)
        self.assertNotIn("scrollIntoView", handler)

    def test_marker_styling_is_compact_touchable_and_distinguishes_selection(self):
        source = self.source
        self.assertIn("button.run.has-performance", source)
        self.assertIn("min-height:44px", source)
        self.assertIn(".run-performance-marker", source)
        self.assertIn("position:absolute", source)
        self.assertIn("top:2px", source)
        self.assertIn("right:4px", source)
        self.assertIn("button.run.has-performance:focus-visible", source)
        self.assertIn("button.run.has-performance.performance-selected", source)

    def test_null_duration_renders_not_measured_without_coercing_zero(self):
        start = self.source.index("function formatPerformanceDuration")
        end = self.source.index("function performanceSessionLabel", start)
        formatter = self.source[start:end]
        self.assertIn('milliseconds === null', formatter)
        self.assertIn('return "not measured";', formatter)
        self.assertIn('Number.isSafeInteger(milliseconds)', formatter)
        self.assertNotIn('if (!milliseconds)', formatter)
        self.assertIn('milliseconds >= 0', self.source)

    def test_every_dynamic_identity_and_detail_reaches_an_escape_boundary(self):
        source = self.source
        self.assertIn("identity.slice(1).map(esc)", source)
        self.assertIn("${esc(identity[0])}", source)
        self.assertIn("${esc(performanceStartLabel(record.lifecycle_started_at_utc))}", source)
        self.assertIn("${esc(performanceSessionLabel(record.session))}", source)
        self.assertIn('${esc("Identity source: " + record.identity_source)}', source)
        self.assertIn('title="${esc(details)}"', source)

    def test_failure_uses_nonfatal_section_notice(self):
        self.assertIn('id="performance-notice"', self.source)
        self.assertIn("renderPerformanceUnavailable(error instanceof Error", self.source)
        performance_fetch = self.source.index('getJSON("/api/performance")')
        performance_block = self.source[
            performance_fetch:self.source.index("let rows = null;", performance_fetch)
        ]
        self.assertNotIn('banner(', performance_block)
        unavailable_start = self.source.index("function renderPerformanceUnavailable")
        unavailable_end = self.source.index("function performanceRecordRow", unavailable_start)
        unavailable = self.source[unavailable_start:unavailable_end]
        self.assertIn("performanceRecordsByInvocation = new Map()", unavailable)
        self.assertIn("selectedPerformanceInvocationId = null", unavailable)

    def test_phone_timing_summary_is_bounded_display_only_and_schema_compatible(self):
        summary_start = self.source.index("function phoneTimingSummary")
        append_start = self.source.index(
            "function appendPhoneTimingSummary", summary_start
        )
        summary = self.source[summary_start:append_start]
        append_end = self.source.index(
            "function performanceDurationCell", append_start
        )
        append = self.source[append_start:append_end]

        self.assertIn("if (!validPerformanceRecord(record)) return null", summary)
        self.assertIn("PHONE_SHARE_TOOLTIP_LIMIT = 500", self.source)
        self.assertIn(
            "summary.length <= PHONE_SHARE_TOOLTIP_LIMIT ? summary : null",
            summary,
        )
        self.assertIn("].join('\\n')", summary)
        self.assertIn("if (!summary) return baseTooltip", append)
        self.assertIn("`${baseTooltip}\\n${summary}`", append)
        self.assertIn(
            "combined.length <= PHONE_SHARE_TOOLTIP_LIMIT ? combined : baseTooltip",
            append,
        )
        for label in (
            "session:",
            "runner / model / configuration:",
            "End-to-end task:",
            "Routine total:",
            "Strategy execution:",
            "Routine overhead:",
        ):
            self.assertIn(label, summary)
        for private_field in (
            "invocation_id",
            "started_at",
            "finished_at",
            "identity_source",
            "clock_source",
            "observed_at",
            "status_file",
            "path",
        ):
            self.assertNotIn(private_field, summary)

        start = self.source.index("function buildPhoneView")
        end = self.source.index("function base64Url", start)
        phone_view = self.source[start:end]
        self.assertIn("runView.push({ time, label, phase, tooltip })", phone_view)
        self.assertIn("runs: runView", phone_view)
        self.assertIn("eras: eraView", phone_view)
        self.assertNotIn("timing:", phone_view.lower())

    def test_phone_timing_uses_only_exact_starred_chip_invocation_lookup(self):
        collect_start = self.source.index("function collectRunView")
        collect_end = self.source.index("function aggregateEras", collect_start)
        collect = self.source[collect_start:collect_end]
        self.assertIn("chip.dataset.performanceInvocationId", collect)
        self.assertIn(
            "performanceRecordsByInvocation.get(invocationId)", collect
        )
        self.assertIn(
            "tooltip: appendPhoneTimingSummary(baseTooltip, record)", collect
        )
        self.assertIn("chip.querySelector('.t')", collect)
        self.assertIn("chip.querySelector('.o')", collect)
        self.assertIn("['ran', 'skipped', 'halted'].find", collect)
        self.assertNotIn("performanceStartLabel", collect)
        self.assertNotIn("lifecycle_started_at_utc", collect)
        self.assertNotIn("run-performance-marker", collect)
        self.assertNotIn("timing:", collect.lower())

    def test_mobile_performance_rows_expose_column_labels(self):
        self.assertIn(".performance-table td::before", self.source)
        self.assertIn("minmax(0, 1.2fr)", self.source)
        self.assertIn("overflow-wrap:anywhere", self.source)
        self.assertIn("content:attr(data-label)", self.source)
        for label in (
            "Start / session",
            "Runner / model / configuration",
            "End-to-end task",
            "Routine total",
            "Strategy execution",
            "Routine overhead",
        ):
            self.assertIn(f'data-label="{label}"', self.source)


if __name__ == "__main__":
    unittest.main()
