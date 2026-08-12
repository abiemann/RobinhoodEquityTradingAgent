import copy
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import broker_snapshot
import run_lifecycle
import status_snapshot


SCRIPT = os.path.join(ROOT, "status_snapshot.py")


def write_valid_scratch_marker(scratch):
    marker = {
        "schema_version": 1,
        "marker": "rhmra-broker-snapshot-scratch",
        "purpose": "daily-loss-raw-broker-staging",
        "scratch_id": str(uuid.uuid4()),
    }
    Path(scratch, broker_snapshot.SCRATCH_MARKER).write_bytes(
        (
            json.dumps(
                marker,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    )


def valid_snapshot(run_start="2026-01-02T10:13:27-08:00"):
    return {
        "schema_version": 1,
        "run_start_pt": run_start,
        "rules_version": "abcd123",
        "dry_run": False,
        "session": "regular",
        "account": {
            "total_value": 100.00,
            "cash": 75.00,
            "buying_power": 75.00,
            "equity_value": 258.97,
        },
        "realized_pnl_today": 0,
        "positions": [
            {
                "symbol": "TEST.A",
                "quantity": 5.5,
                "avg_buy_price": 40.0,
                "current_price": 47.0854545,
                "stop_price": 38.0,
                "stop_state": "confirmed",
            }
        ],
        "guards": {
            "circuit_breaker": "clear",
            "stop_fills_today": 0,
            "entry_phase": "ran",
            "entry_skip_reason": None,
        },
    }


def claude_alternate_snapshot_shape():
    """Sanitized fixture matching the alternate Claude field shape."""

    return {
        "schema_version": 1,
        "invocation_id": "00000000-0000-4000-8000-000000000001",
        "strategy_started_at_utc": "2026-01-02T15:14:17Z",
        "strategy_finished_at_utc": "2026-01-02T15:37:00Z",
        "rules_version": "abcdef1",
        "constants_sha256": "a" * 64,
        "dry_run": False,
        "account_name": "Example",
        "timing": {
            "runner": "unknown",
            "model": "unknown",
            "config": "unknown",
            "identity_source": "unknown",
        },
        "circuit_breakers": {
            "daily_loss_pct": 0.0,
            "daily_loss_status": "clear",
            "stop_count_today": 0,
            "stop_count_status": "clear",
            "spy_direction": "green",
            "spy_status": "clear",
            "overall": "clear",
        },
        "scan": {
            "scan_title": "Example scan",
            "total_rows": 12,
            "passed_filter": 2,
        },
        "working_list": ["TESTA", "TESTB"],
        "buy_candidates": [],
        "orders_placed": 0,
        "orders_filled": 0,
        "realized_pnl_today_usd": 0.0,
        "portfolio_final": {
            "total_value": "100.00",
            "cash": "100.00",
            "equity_value": "0",
            "open_positions": 0,
        },
        "gate_record_file": "rhmra-gates-2026_01_02-08_13.json",
        "outcome": "no_candidates",
    }


def claude_flat_snapshot_shape():
    """Sanitized fixture matching the flat Claude field shape."""

    return {
        "invocation_id": "00000000-0000-4000-8000-000000000002",
        "run_start_pt": "2026-01-02T10:13:03-08:00",
        "run_start_utc": "2026-01-02T18:13:03Z",
        "session": "regular",
        "mode": "LIVE",
        "dry_run": False,
        "rules_version": "abcdef1",
        "account_type": "cash",
        "total_value": 100.00,
        "cash": 100.00,
        "equity_value": 0.00,
        "buying_power": 100.00,
        "positions_held": 0,
        "circuit_breaker": "clear",
        "daily_pnl": 0.00,
        "stop_count_today": 0,
        "scan_total": 12,
        "scan_returned": 10,
        "filter_passed": 2,
        "working_list": 2,
        "pre_filter_dropped": 0,
        "evaluated": 2,
        "buy_candidates": 0,
        "orders_placed": 0,
        "fills": 0,
        "strategy_start_utc": "2026-01-02T18:14:25Z",
        "strategy_end_utc": "2026-01-02T18:17:42Z",
        "report_file": "rhmra-log-2026_01_02-10_13.md",
    }


class StatusSnapshotValidationTests(unittest.TestCase):
    def test_valid_document_is_returned_unchanged(self):
        document = valid_snapshot()
        self.assertIs(status_snapshot.validate_status_snapshot(document), document)

    def test_valid_flat_account_and_not_evaluated_skip(self):
        document = valid_snapshot()
        document["session"] = "closed-holiday"
        document["account"]["equity_value"] = 0
        document["positions"] = []
        document["realized_pnl_today"] = None
        document["guards"] = {
            "circuit_breaker": "not-evaluated",
            "stop_fills_today": None,
            "entry_phase": "skipped",
            "entry_skip_reason": "market closed",
        }
        status_snapshot.validate_status_snapshot(document)

    def assert_invalid(self, mutate, message=None):
        document = valid_snapshot()
        mutate(document)
        with self.assertRaises(status_snapshot.StatusSnapshotError) as caught:
            status_snapshot.validate_status_snapshot(document)
        if message is not None:
            self.assertIn(message, str(caught.exception))

    def test_requires_exact_top_level_keys(self):
        self.assert_invalid(
            lambda value: value.update({"invocation_id": "invented"}),
            "unexpected 'invocation_id'",
        )
        self.assert_invalid(
            lambda value: value.pop("guards"),
            "missing 'guards'",
        )

    def test_requires_exact_nested_keys(self):
        self.assert_invalid(
            lambda value: value["account"].update({"account_name": "Example"}),
            "account_name",
        )
        self.assert_invalid(
            lambda value: value["positions"][0].pop("stop_state"),
            "stop_state",
        )
        self.assert_invalid(
            lambda value: value["guards"].update({"scan_total": 12}),
            "scan_total",
        )

    def test_booleans_are_not_numbers(self):
        self.assert_invalid(
            lambda value: value["account"].update({"cash": True}),
            "finite JSON number",
        )
        self.assert_invalid(
            lambda value: value["guards"].update({"stop_fills_today": False}),
            "non-negative safe integer",
        )

    def test_nonfinite_numbers_are_rejected(self):
        for number in (math.inf, -math.inf, math.nan):
            with self.subTest(number=number):
                self.assert_invalid(
                    lambda value, number=number: value["account"].update(
                        {"cash": number}
                    ),
                    "finite JSON number",
                )

    def test_all_numbers_must_fit_the_json_safe_boundary(self):
        self.assert_invalid(
            lambda value: value["account"].update(
                {"total_value": status_snapshot.MAX_SAFE_INTEGER + 1}
            ),
            "safe-integer boundary",
        )
        self.assert_invalid(
            lambda value: value["positions"][0].update(
                {"current_price": -(status_snapshot.MAX_SAFE_INTEGER + 1)}
            ),
            "safe-integer boundary",
        )
        self.assert_invalid(
            lambda value: value["guards"].update(
                {"stop_fills_today": status_snapshot.MAX_SAFE_INTEGER + 1}
            ),
            "safe integer",
        )

    def test_timestamp_rules_version_boolean_and_session_are_strict(self):
        self.assert_invalid(
            lambda value: value.update(
                {"run_start_pt": "2026-01-02T10:13:27Z"}
            ),
            "Pacific timestamp",
        )
        self.assert_invalid(
            lambda value: value.update({"rules_version": "C51DAD2"}),
            "lowercase git hash",
        )
        self.assert_invalid(
            lambda value: value.update({"dry_run": 0}), "boolean"
        )
        self.assert_invalid(
            lambda value: value.update({"session": "market-hours"}),
            "expected one of",
        )

    def test_position_values_symbol_uniqueness_and_stop_pair_are_strict(self):
        self.assert_invalid(
            lambda value: value["positions"][0].update({"quantity": 0}),
            "greater than zero",
        )
        self.assert_invalid(
            lambda value: value["positions"][0].update({"symbol": "test"}),
            "invalid ticker",
        )

        def duplicate(value):
            value["positions"].append(copy.deepcopy(value["positions"][0]))

        self.assert_invalid(duplicate, "duplicate symbol")
        self.assert_invalid(
            lambda value: value["positions"][0].update(
                {"stop_state": "none", "stop_price": 38.0}
            ),
            "must be null",
        )
        self.assert_invalid(
            lambda value: value["positions"][0].update(
                {"stop_state": "queued", "stop_price": None}
            ),
            "required for an active stop",
        )

    def test_equity_and_positions_must_agree(self):
        self.assert_invalid(
            lambda value: value["account"].update({"equity_value": 0}),
            "disagree",
        )

        def no_positions_with_equity(value):
            value["positions"] = []

        self.assert_invalid(no_positions_with_equity, "disagree")

    def test_quoted_equity_must_match_within_final_refresh_tolerance(self):
        self.assert_invalid(
            lambda value: value["account"].update({"equity_value": 200.0}),
            "quoted position equity are incoherent",
        )
        document = valid_snapshot()
        quoted = (
            document["positions"][0]["quantity"]
            * document["positions"][0]["current_price"]
        )
        document["account"]["equity_value"] = quoted * 1.009
        status_snapshot.validate_status_snapshot(document)

    def test_guard_cross_field_rules_are_strict(self):
        self.assert_invalid(
            lambda value: value["guards"].update(
                {"entry_skip_reason": "but phase ran"}
            ),
            "must be null",
        )
        self.assert_invalid(
            lambda value: value["guards"].update(
                {"entry_phase": "skipped", "entry_skip_reason": ""}
            ),
            "non-empty string",
        )
        self.assert_invalid(
            lambda value: value["guards"].update(
                {
                    "circuit_breaker": "not-evaluated",
                    "stop_fills_today": 0,
                    "entry_phase": "skipped",
                    "entry_skip_reason": "already impossible",
                }
            ),
            "not-evaluated requires",
        )

    def test_position_and_skip_reason_cardinality_is_bounded(self):
        def too_many_positions(value):
            template = value["positions"][0]
            value["positions"] = [
                {
                    **template,
                    "symbol": f"T{index}",
                }
                for index in range(status_snapshot.MAX_POSITIONS + 1)
            ]

        self.assert_invalid(too_many_positions, "exceeds 1000 entries")
        self.assert_invalid(
            lambda value: value["guards"].update(
                {
                    "entry_phase": "skipped",
                    "entry_skip_reason": "x"
                    * (status_snapshot.MAX_SKIP_REASON_CHARS + 1),
                }
            ),
            "exceeds 1000 characters",
        )

    def test_strict_loader_rejects_duplicate_keys_and_nonfinite_literals(self):
        with tempfile.TemporaryDirectory() as td:
            duplicate = Path(td, "duplicate.json")
            duplicate.write_text(
                '{"schema_version":1,"schema_version":1}', encoding="utf-8"
            )
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "duplicate JSON object key"
            ):
                status_snapshot.load_status_snapshot(duplicate)

            nonfinite = Path(td, "nonfinite.json")
            nonfinite.write_text('{"value":NaN}', encoding="utf-8")
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "non-finite JSON constant"
            ):
                status_snapshot.load_status_snapshot(nonfinite)

    def test_rejects_observed_claude_alternate_schema_shape(self):
        with self.assertRaisesRegex(
            status_snapshot.StatusSnapshotError, "invalid keys"
        ):
            status_snapshot.validate_status_snapshot(
                claude_alternate_snapshot_shape()
            )

    def test_rejects_observed_claude_flat_schema_shape(self):
        with self.assertRaisesRegex(
            status_snapshot.StatusSnapshotError, "invalid keys"
        ):
            status_snapshot.validate_status_snapshot(claude_flat_snapshot_shape())


class StatusSnapshotPublicationTests(unittest.TestCase):
    def paths(self, td, document=None):
        base = Path(td)
        scratch = base / "scratch"
        reports = base / "run-reports"
        scratch.mkdir()
        reports.mkdir()
        write_valid_scratch_marker(scratch)
        document = valid_snapshot() if document is None else document
        candidate = scratch / "rhmra-status-candidate.json"
        candidate.write_text(
            json.dumps(document, allow_nan=False), encoding="utf-8"
        )
        output = reports / "rhmra-status-2026_01_02-10_13.json"
        state = base / "lifecycle.sqlite3"
        projection = base / "lifecycle.json"
        invocation_id = "11111111-1111-4111-8111-111111111111"
        run_lifecycle.start_invocation(
            invocation_id=invocation_id,
            state_file=str(state),
            projection_file=str(projection),
            now_utc="2026-01-02T18:13:26Z",
        )
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="preflight",
            run_start_pt="2026-01-02T10:13:27-08:00",
            state_file=str(state),
            projection_file=str(projection),
            now_utc="2026-01-02T18:13:27Z",
        )
        self.lifecycle_binding = {
            "invocation_id": invocation_id,
            "lifecycle_state_file": state,
            "lifecycle_projection_file": projection,
        }
        return scratch, reports, candidate, output

    def test_publish_preserves_candidate_bytes_and_validates_final_readback(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            candidate_bytes = candidate.read_bytes()
            result = status_snapshot.publish_status_snapshot(
                candidate, output, scratch=scratch, report_dir=reports,
                **self.lifecycle_binding
            )
            self.assertEqual(
                result,
                {
                    "schema_version": 1,
                    "action": "publish",
                    "ok": True,
                    "status_file": output.name,
                    "byte_count": len(candidate_bytes),
                    "sha256": __import__("hashlib").sha256(candidate_bytes).hexdigest(),
                },
            )
            self.assertEqual(output.read_bytes(), candidate_bytes)
            status_snapshot.load_status_snapshot(output)

    def test_invalid_candidate_leaves_previous_truthful_snapshot_untouched(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            previous = reports / "rhmra-status-2026_01_02-09_11.json"
            previous_bytes = b'{"truthful":"previous"}\n'
            previous.write_bytes(previous_bytes)
            malformed = valid_snapshot()
            malformed.pop("schema_version")
            candidate.write_text(json.dumps(malformed), encoding="utf-8")

            with self.assertRaises(status_snapshot.StatusSnapshotError):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )

            self.assertFalse(output.exists())
            self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_observed_claude_shapes_never_publish_or_touch_previous_snapshot(self):
        for label, malformed in (
            ("alternate schema", claude_alternate_snapshot_shape()),
            ("flat schema", claude_flat_snapshot_shape()),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as td:
                scratch, reports, candidate, output = self.paths(td, malformed)
                previous = reports / "rhmra-status-2026_01_02-09_11.json"
                previous_bytes = b"previous truthful snapshot\n"
                previous.write_bytes(previous_bytes)
                with self.assertRaises(status_snapshot.StatusSnapshotError):
                    status_snapshot.publish_status_snapshot(
                        candidate, output, scratch=scratch, report_dir=reports,
                        **self.lifecycle_binding
                    )
                self.assertFalse(output.exists())
                self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_missing_or_malformed_scratch_marker_blocks_publication(self):
        for marker_bytes in (None, b"{}\n"):
            with self.subTest(marker_bytes=marker_bytes), tempfile.TemporaryDirectory() as td:
                scratch, reports, candidate, output = self.paths(td)
                marker = scratch / broker_snapshot.SCRATCH_MARKER
                marker.unlink()
                if marker_bytes is not None:
                    marker.write_bytes(marker_bytes)
                with self.assertRaisesRegex(
                    status_snapshot.StatusSnapshotError,
                    "not a preflighted broker-snapshot directory",
                ):
                    status_snapshot.publish_status_snapshot(
                        candidate, output, scratch=scratch, report_dir=reports,
                        **self.lifecycle_binding
                    )
                self.assertFalse(output.exists())

    def test_existing_output_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            existing = b"existing truthful bytes"
            output.write_bytes(existing)
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "refusing to replace"
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertEqual(output.read_bytes(), existing)

    def test_candidate_must_be_direct_child_of_external_scratch(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            nested = scratch / "nested"
            nested.mkdir()
            nested_candidate = nested / candidate.name
            nested_candidate.write_bytes(candidate.read_bytes())
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "directly inside scratch"
            ):
                status_snapshot.publish_status_snapshot(
                    nested_candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )

    def test_candidate_symbolic_link_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(
                    status_snapshot.StatusSnapshotError,
                    "symbolic links are not allowed",
                ):
                    status_snapshot.publish_status_snapshot(
                        candidate, output, scratch=scratch, report_dir=reports,
                        **self.lifecycle_binding
                    )

    def test_oversized_candidate_is_rejected_without_publication(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            candidate.write_bytes(b" " * (status_snapshot.MAX_SNAPSHOT_BYTES + 1))
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "exceeds 1000000 bytes"
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertFalse(output.exists())

    def test_output_filename_must_match_authoritative_run_start(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, _output = self.paths(td)
            wrong = reports / "rhmra-status-2026_01_02-10_14.json"
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "filename must match"
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, wrong, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )

    def test_lifecycle_rejects_observed_11_45_artifacts_for_11_43_run(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, _output = self.paths(td)
            wrong = valid_snapshot('2026-01-02T10:15:27-08:00')
            candidate.write_text(json.dumps(wrong), encoding='utf-8')
            output = reports / 'rhmra-status-2026_01_02-10_15.json'
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError,
                'must exactly match lifecycle binding',
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertFalse(output.exists())

    def test_lifecycle_rejects_same_minute_rounded_candidate_without_clobber(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            rounded = valid_snapshot('2026-01-02T10:13:00-08:00')
            candidate.write_text(json.dumps(rounded), encoding='utf-8')
            previous = reports / 'rhmra-status-2026_01_02-09_59.json'
            previous_bytes = b'previous truthful status bytes'
            previous.write_bytes(previous_bytes)
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError,
                r'10:13:27-08:00',
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertFalse(output.exists())
            self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_publish_rejects_unknown_and_finished_invocations(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            unknown = dict(self.lifecycle_binding)
            unknown['invocation_id'] = str(uuid.uuid4())
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, 'has not been started'
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **unknown
                )
            self.assertFalse(output.exists())

            run_lifecycle.finish_invocation(
                invocation_id=self.lifecycle_binding['invocation_id'],
                classification='final-status-unavailable',
                reason_code='status-write-failed',
                report_file='rhmra-log-2026_01_02-10_13.md',
                state_file=str(self.lifecycle_binding['lifecycle_state_file']),
                projection_file=str(
                    self.lifecycle_binding['lifecycle_projection_file']
                ),
                report_dir=str(reports),
                now_utc='2026-01-02T18:13:28Z',
            )
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, 'already finished'
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertFalse(output.exists())

    def test_read_only_verify_recovers_a_lost_publish_receipt_without_rewrite(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            publish = status_snapshot.publish_status_snapshot(
                candidate, output, scratch=scratch, report_dir=reports,
                **self.lifecycle_binding
            )
            original = output.read_bytes()

            recovered = status_snapshot.verify_published_status_snapshot(
                output,
                candidate=candidate,
                scratch=scratch,
                report_dir=reports,
                **self.lifecycle_binding,
            )
            self.assertEqual(recovered["action"], "verify")
            self.assertEqual(
                {key: value for key, value in recovered.items() if key != "action"},
                {key: value for key, value in publish.items() if key != "action"},
            )
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "refusing to replace"
            ):
                status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertEqual(output.read_bytes(), original)

    def test_candidate_bound_verify_rejects_finished_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            status_snapshot.publish_status_snapshot(
                candidate, output, scratch=scratch, report_dir=reports,
                **self.lifecycle_binding
            )
            run_lifecycle.finish_invocation(
                invocation_id=self.lifecycle_binding['invocation_id'],
                classification='completed',
                report_file='rhmra-log-2026_01_02-10_13.md',
                status_file=output.name,
                state_file=str(self.lifecycle_binding['lifecycle_state_file']),
                projection_file=str(
                    self.lifecycle_binding['lifecycle_projection_file']
                ),
                report_dir=str(reports),
                now_utc='2026-01-02T18:13:28Z',
            )
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, 'already finished'
            ):
                status_snapshot.verify_published_status_snapshot(
                    output, candidate=candidate, scratch=scratch,
                    report_dir=reports, **self.lifecycle_binding
                )

    def test_read_only_verify_distinguishes_absent_output_without_creating_it(self):
        with tempfile.TemporaryDirectory() as td:
            _scratch, reports, _candidate, output = self.paths(td)
            with self.assertRaises(status_snapshot.StatusSnapshotMissing):
                status_snapshot.verify_published_status_snapshot(
                    output,
                    candidate=_candidate,
                    scratch=_scratch,
                    report_dir=reports,
                    **self.lifecycle_binding,
                )
            self.assertFalse(output.exists())

    def test_verify_rejects_valid_same_name_output_with_different_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            foreign = valid_snapshot()
            foreign["account"]["cash"] = 74.0
            output.write_text(json.dumps(foreign), encoding="utf-8")
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "not byte-identical"
            ):
                status_snapshot.verify_published_status_snapshot(
                    output,
                    candidate=candidate,
                    scratch=scratch,
                    report_dir=reports,
                    **self.lifecycle_binding,
                )

    def test_published_loader_rejects_filename_timestamp_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            _scratch, reports, candidate, _output = self.paths(td)
            mismatched = reports / "rhmra-status-2026_01_02-10_14.json"
            mismatched.write_bytes(candidate.read_bytes())
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "filename must match"
            ):
                status_snapshot.load_published_status_snapshot(
                    mismatched, reports
                )

    def test_published_loader_rejects_nonregular_or_symlink_path(self):
        with tempfile.TemporaryDirectory() as td:
            _scratch, reports, _candidate, output = self.paths(td)
            output.mkdir()
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "regular non-symlink"
            ):
                status_snapshot.load_published_status_snapshot(output, reports)

        with tempfile.TemporaryDirectory() as td:
            _scratch, reports, candidate, output = self.paths(td)
            output.write_bytes(candidate.read_bytes())
            with mock.patch.object(Path, "is_symlink", return_value=True):
                with self.assertRaisesRegex(
                    status_snapshot.StatusSnapshotError, "regular non-symlink"
                ):
                    status_snapshot.load_published_status_snapshot(output, reports)

    def test_published_loader_rejects_path_outside_report_dir(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            outside = scratch / output.name
            outside.write_bytes(candidate.read_bytes())
            with self.assertRaisesRegex(
                status_snapshot.StatusSnapshotError, "direct child of report_dir"
            ):
                status_snapshot.load_published_status_snapshot(outside, reports)

    def test_temp_cleanup_failure_after_valid_commit_remains_success(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            real_unlink = os.unlink

            def fail_hidden_temp(path, *args, **kwargs):
                if Path(path).name.startswith(f".{output.name}."):
                    raise PermissionError("simulated cleanup denial")
                return real_unlink(path, *args, **kwargs)

            with mock.patch("status_snapshot.os.unlink", side_effect=fail_hidden_temp):
                result = status_snapshot.publish_status_snapshot(
                    candidate, output, scratch=scratch, report_dir=reports,
                    **self.lifecycle_binding
                )
            self.assertIs(result["ok"], True)
            self.assertTrue(output.exists())
            status_snapshot.load_published_status_snapshot(output, reports)

    def test_lifecycle_finish_racing_staging_blocks_atomic_commit(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            real_validate = status_snapshot._validate_lifecycle_binding
            calls = 0

            def finish_before_commit(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    run_lifecycle.finish_invocation(
                        invocation_id=self.lifecycle_binding['invocation_id'],
                        classification='final-status-unavailable',
                        reason_code='status-write-failed',
                        report_file='rhmra-log-2026_01_02-10_13.md',
                        state_file=str(
                            self.lifecycle_binding['lifecycle_state_file']
                        ),
                        projection_file=str(
                            self.lifecycle_binding['lifecycle_projection_file']
                        ),
                        now_utc='2026-01-02T18:13:28Z',
                    )
                return real_validate(*args, **kwargs)

            with mock.patch.object(
                status_snapshot,
                '_validate_lifecycle_binding',
                side_effect=finish_before_commit,
            ):
                with self.assertRaisesRegex(
                    status_snapshot.StatusSnapshotError, 'already finished'
                ):
                    status_snapshot.publish_status_snapshot(
                        candidate, output, scratch=scratch, report_dir=reports,
                        **self.lifecycle_binding
                    )
            self.assertEqual(calls, 2)
            self.assertFalse(output.exists())
            self.assertEqual(
                [path.name for path in reports.iterdir()],
                [],
            )

    def test_cli_emits_one_valid_success_envelope(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "publish",
                    "--invocation-id",
                    self.lifecycle_binding["invocation_id"],
                    "--scratch",
                    str(scratch),
                    "--candidate",
                    str(candidate),
                    "--output",
                    str(output),
                    "--report-dir",
                    str(reports),
                    "--lifecycle-state-file",
                    str(self.lifecycle_binding["lifecycle_state_file"]),
                    "--lifecycle-projection-file",
                    str(self.lifecycle_binding["lifecycle_projection_file"]),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stderr, "")
            response = json.loads(proc.stdout)
            self.assertEqual(
                set(response),
                {"schema_version", "action", "ok", "status_file", "byte_count", "sha256"},
            )
            self.assertEqual(response["action"], "publish")
            self.assertIs(response["ok"], True)
            self.assertEqual(response["status_file"], output.name)
            self.assertTrue(output.exists())

    def test_cli_failure_is_json_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as td:
            scratch, reports, candidate, output = self.paths(td)
            candidate.write_text('{"schema_version":1}', encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    SCRIPT,
                    "publish",
                    "--invocation-id",
                    self.lifecycle_binding["invocation_id"],
                    "--scratch",
                    str(scratch),
                    "--candidate",
                    str(candidate),
                    "--output",
                    str(output),
                    "--report-dir",
                    str(reports),
                    "--lifecycle-state-file",
                    str(self.lifecycle_binding["lifecycle_state_file"]),
                    "--lifecycle-projection-file",
                    str(self.lifecycle_binding["lifecycle_projection_file"]),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=20,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(proc.stderr, "")
            response = json.loads(proc.stdout)
            self.assertEqual(
                response,
                {
                    "schema_version": 1,
                    "action": "publish",
                    "ok": False,
                    "error": {
                        "code": "invalid_status_snapshot",
                        "message": response["error"]["message"],
                    },
                },
            )
            self.assertFalse(output.exists())


class StatusSnapshotRoutineContractTests(unittest.TestCase):
    def test_routine_uses_only_candidate_bound_atomic_publication(self):
        routine = Path(
            ROOT, "robinhood-momentum-routine-autonomous.md"
        ).read_text(encoding="utf-8")
        block = routine.split("**Publish the STATUS SNAPSHOT", 1)[1].split(
            "### PERFORMANCE TELEMETRY", 1
        )[0]
        self.assertIn(
            "`<scratch>/rhmra-status-candidate.json` directly inside",
            block,
        )
        self.assertIn("existing, marked, broker-preflighted", block)
        self.assertIn("Never use the file-writing tool", block)
        self.assertIn(
            "status_snapshot.py publish --invocation-id '<INVOCATION_ID>' "
            "--scratch",
            block,
        )
        self.assertIn(
            "status_snapshot.py verify --invocation-id '<INVOCATION_ID>' "
            "--scratch",
            block,
        )
        self.assertIn("--candidate '<absolute scratch>", block)
        self.assertIn("the already-bound `PYTHON_EXE`", block)
        self.assertIn("exactly one initial publication command", block)
        self.assertIn(
            "byte-identical to this invocation's valid scratch candidate", block
        )
        self.assertIn(
            'error.code: "status_snapshot_missing"', block
        )
        self.assertIn("proves that no final file exists", block)
        self.assertIn("replace the scratch candidate once", block)
        self.assertIn("one final time", block)
        self.assertIn("There is no third candidate rewrite", block)
        self.assertIn("`final-status-unavailable` / `status-write-failed`", block)
        self.assertIn("retain no status filename", block)
        self.assertNotIn(
            "json.load(open(sys.argv[1], encoding='utf-8'))", block
        )

    def test_status_helper_is_part_of_rules_version(self):
        routine = Path(
            ROOT, "robinhood-momentum-routine-autonomous.md"
        ).read_text(encoding="utf-8")
        rules_line = next(
            line
            for line in routine.splitlines()
            if line.startswith("**rules_version**")
        )
        helper = Path(ROOT, "rules_version.py").read_text(encoding="utf-8")
        self.assertIn("rules_version.py", rules_line)
        self.assertIn("canonical rule-set file list", rules_line)
        self.assertIn('"status_snapshot.py"', helper)
        self.assertIn('"run_lifecycle.py"', helper)


if __name__ == "__main__":
    unittest.main()
