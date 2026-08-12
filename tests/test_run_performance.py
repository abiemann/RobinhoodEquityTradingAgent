import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import run_lifecycle
import run_performance


class RunPerformanceTests(unittest.TestCase):
    START = "2026-08-11T19:00:00Z"
    FINISH = "2026-08-11T19:10:00Z"
    STRATEGY_START = "2026-08-11T19:02:00Z"
    STRATEGY_FINISH = "2026-08-11T19:08:00Z"
    INTERNAL_NOW = "2026-08-11T19:11:00Z"
    TASK_NOW = "2026-08-11T19:12:00Z"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = self.temporary.name
        self.lifecycle_state = os.path.join(root, "lifecycle.sqlite3")
        self.lifecycle_projection = os.path.join(root, "lifecycle.json")
        self.state = os.path.join(root, "performance.sqlite3")
        self.projection = os.path.join(root, "performance.json")

    def tearDown(self):
        self.temporary.cleanup()

    def finish_lifecycle(
        self,
        *,
        invocation_id=None,
        start=START,
        finish=FINISH,
    ):
        invocation_id = invocation_id or str(uuid.uuid4())
        run_lifecycle.start_invocation(
            invocation_id=invocation_id,
            state_file=self.lifecycle_state,
            projection_file=self.lifecycle_projection,
            now_utc=start,
        )
        run_lifecycle.finish_invocation(
            invocation_id=invocation_id,
            classification="completed",
            state_file=self.lifecycle_state,
            projection_file=self.lifecycle_projection,
            now_utc=finish,
        )
        return invocation_id

    def start_lifecycle(self, *, invocation_id=None, start=START):
        invocation_id = invocation_id or str(uuid.uuid4())
        run_lifecycle.start_invocation(
            invocation_id=invocation_id,
            state_file=self.lifecycle_state,
            projection_file=self.lifecycle_projection,
            now_utc=start,
        )
        return invocation_id

    def internal_kwargs(self, invocation_id, **overrides):
        values = {
            "invocation_id": invocation_id,
            "strategy_start_utc": self.STRATEGY_START,
            "strategy_end_utc": self.STRATEGY_FINISH,
            "session": "after-hours",
            "runner": "codex",
            "model": "gpt-5.6-luna",
            "configuration": "reasoning=high",
            "identity_source": "task-definition",
            "state_file": self.state,
            "projection_file": self.projection,
            "lifecycle_state_file": self.lifecycle_state,
            "lifecycle_projection_file": self.lifecycle_projection,
            "now_utc": self.INTERNAL_NOW,
        }
        values.update(overrides)
        return values

    def task_kwargs(self, invocation_id, **overrides):
        values = {
            "invocation_id": invocation_id,
            "task_duration_ms": 720_000,
            "runner": "codex",
            "model": "gpt-5.6-luna",
            "configuration": "reasoning=high",
            "identity_source": "manual-ui",
            "clock_source": "codex-worked-for",
            "state_file": self.state,
            "projection_file": self.projection,
            "lifecycle_state_file": self.lifecycle_state,
            "lifecycle_projection_file": self.lifecycle_projection,
            "now_utc": self.TASK_NOW,
        }
        values.update(overrides)
        return values

    def read_projection(self):
        with open(self.projection, encoding="utf-8") as handle:
            return json.load(handle)

    def test_record_internal_and_task_observation_compute_exact_metrics(self):
        invocation_id = self.finish_lifecycle()
        internal = run_performance.record_internal(
            **self.internal_kwargs(invocation_id)
        )
        self.assertEqual(
            set(internal),
            {
                "schema_version",
                "action",
                "ok",
                "invocation_id",
                "sequence",
                "routine_total_ms",
                "strategy_execution_ms",
                "routine_overhead_ms",
                "projection_record_count",
            },
        )
        self.assertEqual(internal["routine_total_ms"], 600_000)
        self.assertEqual(internal["strategy_execution_ms"], 360_000)
        self.assertEqual(internal["routine_overhead_ms"], 240_000)

        task = run_performance.observe_task(**self.task_kwargs(invocation_id))
        self.assertEqual(
            set(task),
            {
                "schema_version",
                "action",
                "ok",
                "invocation_id",
                "sequence",
                "task_duration_ms",
                "clock_source",
                "selected_clock_source",
                "outside_lifecycle_ms",
                "total_overhead_ms",
                "projection_record_count",
            },
        )
        self.assertEqual(task["outside_lifecycle_ms"], 120_000)
        self.assertEqual(task["total_overhead_ms"], 360_000)

        document = self.read_projection()
        self.assertEqual(
            set(document),
            {
                "schema_version",
                "record_limit",
                "record_count",
                "source_event_high_watermark",
                "records",
            },
        )
        self.assertEqual(document["record_count"], 1)
        record = document["records"][0]
        self.assertEqual(set(record), run_performance._RECORD_KEYS)
        self.assertEqual(record["lifecycle_started_at_utc"], self.START)
        self.assertEqual(record["lifecycle_finished_at_utc"], self.FINISH)
        self.assertEqual(record["strategy_started_at_utc"], self.STRATEGY_START)
        self.assertEqual(record["strategy_finished_at_utc"], self.STRATEGY_FINISH)
        self.assertEqual(record["task_duration_ms"], 720_000)
        self.assertEqual(record["task_clock_source"], "codex-worked-for")
        self.assertEqual(record["routine_total_ms"], 600_000)
        self.assertEqual(record["strategy_execution_ms"], 360_000)
        self.assertEqual(record["routine_overhead_ms"], 240_000)
        self.assertEqual(record["outside_lifecycle_ms"], 120_000)
        self.assertEqual(record["total_overhead_ms"], 360_000)
        self.assertEqual(record["identity_source"], "manual-ui")
        self.assertEqual(
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            ),
            document,
        )

    def test_historical_internal_record_without_strategy_preserves_nulls(self):
        invocation_id = self.finish_lifecycle()
        kwargs = self.internal_kwargs(invocation_id)
        kwargs["strategy_start_utc"] = None
        kwargs["strategy_end_utc"] = None
        result = run_performance.record_internal(**kwargs)
        self.assertIsNone(result["strategy_execution_ms"])
        self.assertIsNone(result["routine_overhead_ms"])
        run_performance.observe_task(**self.task_kwargs(invocation_id))
        record = self.read_projection()["records"][0]
        self.assertIsNone(record["strategy_started_at_utc"])
        self.assertIsNone(record["strategy_finished_at_utc"])
        self.assertIsNone(record["strategy_execution_ms"])
        self.assertIsNone(record["routine_overhead_ms"])
        self.assertEqual(record["outside_lifecycle_ms"], 120_000)
        self.assertIsNone(record["total_overhead_ms"])

    def test_strategy_boundaries_are_both_or_neither_and_contained(self):
        invocation_id = self.finish_lifecycle()
        for overrides in (
            {"strategy_end_utc": None},
            {"strategy_start_utc": None},
            {"strategy_start_utc": "2026-08-11T18:59:59Z"},
            {"strategy_end_utc": "2026-08-11T19:10:01Z"},
            {
                "strategy_start_utc": "2026-08-11T19:09:00Z",
                "strategy_end_utc": "2026-08-11T19:08:00Z",
            },
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(run_performance.PerformanceError):
                    run_performance.record_internal(
                        **self.internal_kwargs(invocation_id, **overrides)
                    )
        self.assertFalse(os.path.exists(self.state))

    def test_requires_finished_validated_lifecycle_projection(self):
        running_id = self.start_lifecycle()
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "not finished"
        ):
            run_performance.record_internal(**self.internal_kwargs(running_id))
        self.assertFalse(os.path.exists(self.state))

        finished_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z", finish="2026-08-11T20:01:00Z"
        )
        with open(self.lifecycle_projection, encoding="utf-8") as handle:
            corrupt = json.load(handle)
        corrupt["unexpected"] = True
        with open(self.lifecycle_projection, "w", encoding="utf-8") as handle:
            json.dump(corrupt, handle)
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "validate lifecycle"
        ):
            run_performance.record_internal(
                **self.internal_kwargs(
                    finished_id,
                    strategy_start_utc=None,
                    strategy_end_utc=None,
                    now_utc="2026-08-11T20:02:00Z",
                )
            )
        self.assertFalse(os.path.exists(self.state))

    def test_record_time_cannot_precede_lifecycle_finish(self):
        invocation_id = self.finish_lifecycle()
        with self.assertRaisesRegex(run_performance.PerformanceError, "precedes"):
            run_performance.record_internal(
                **self.internal_kwargs(
                    invocation_id, now_utc="2026-08-11T19:09:59Z"
                )
            )

    def test_duplicate_internal_and_duplicate_task_source_are_conflicts(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        with self.assertRaises(run_performance.PerformanceConflict):
            run_performance.record_internal(**self.internal_kwargs(invocation_id))
        run_performance.observe_task(**self.task_kwargs(invocation_id))
        with self.assertRaises(run_performance.PerformanceConflict):
            run_performance.observe_task(
                **self.task_kwargs(
                    invocation_id,
                    task_duration_ms=800_000,
                    now_utc="2026-08-11T19:13:00Z",
                )
            )

    def test_task_requires_internal_positive_duration_and_lifecycle_floor(self):
        invocation_id = self.finish_lifecycle()
        with self.assertRaisesRegex(
            run_performance.PerformanceConflict, "recorded first"
        ):
            run_performance.observe_task(**self.task_kwargs(invocation_id))
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        for value in (0, -1, True, "1.5", "0600000"):
            with self.subTest(value=value):
                with self.assertRaises(run_performance.PerformanceError):
                    run_performance.observe_task(
                        **self.task_kwargs(invocation_id, task_duration_ms=value)
                    )
        with self.assertRaisesRegex(run_performance.PerformanceError, "shorter"):
            run_performance.observe_task(
                **self.task_kwargs(invocation_id, task_duration_ms=599_999)
            )

    def test_task_sources_use_fixed_priority_and_retain_selected_source(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        manual = run_performance.observe_task(
            **self.task_kwargs(
                invocation_id,
                task_duration_ms=800_000,
                clock_source="manual-observation",
                now_utc="2026-08-11T19:12:00Z",
            )
        )
        self.assertEqual(manual["selected_clock_source"], "manual-observation")
        metadata = run_performance.observe_task(
            **self.task_kwargs(
                invocation_id,
                task_duration_ms=700_000,
                clock_source="runner-metadata",
                identity_source="run-metadata",
                model="gpt-5.6-luna",
                now_utc="2026-08-11T19:13:00Z",
            )
        )
        self.assertEqual(metadata["selected_clock_source"], "runner-metadata")
        record = self.read_projection()["records"][0]
        self.assertEqual(record["task_clock_source"], "runner-metadata")
        self.assertEqual(record["task_duration_ms"], 700_000)
        self.assertEqual(record["model"], "gpt-5.6-luna")
        self.assertEqual(record["identity_source"], "run-metadata")

    def test_strict_session_identity_and_clock_validation(self):
        invocation_id = self.finish_lifecycle()
        invalid_internal = (
            {"session": "market-hours"},
            {"runner": "other"},
            {"model": "account: 123456"},
            {"model": "123456"},
            {"configuration": "high/unsafe"},
            {
                "runner": "codex",
                "model": "Luna 5.6",
                "configuration": "high",
                "identity_source": "unknown",
            },
        )
        for overrides in invalid_internal:
            with self.subTest(overrides=overrides):
                with self.assertRaises(run_performance.PerformanceError):
                    run_performance.record_internal(
                        **self.internal_kwargs(invocation_id, **overrides)
                    )
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        with self.assertRaisesRegex(run_performance.PerformanceError, "Codex"):
            run_performance.observe_task(
                **self.task_kwargs(
                    invocation_id,
                    runner="claude",
                    clock_source="codex-worked-for",
                )
            )
        with self.assertRaisesRegex(run_performance.PerformanceError, "conflicts"):
            run_performance.observe_task(
                **self.task_kwargs(
                    invocation_id,
                    runner="claude",
                    model="Sonnet 4.6",
                    clock_source="claude-run-duration",
                )
            )

    def test_documented_runner_configurations_and_identity_consistency(self):
        codex_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(codex_id))
        for overrides in (
            {"model": "gpt-5.6-luna-other"},
            {"configuration": "reasoning=medium"},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(
                    run_performance.PerformanceError, "conflicts"
                ):
                    run_performance.observe_task(
                        **self.task_kwargs(codex_id, **overrides)
                    )

        claude_id = self.finish_lifecycle()
        run_performance.record_internal(
            **self.internal_kwargs(
                claude_id,
                runner="claude",
                model="claude-sonnet-4-6",
                configuration="effort=high",
            )
        )
        result = run_performance.observe_task(
            **self.task_kwargs(
                claude_id,
                runner="claude",
                model="claude-sonnet-4-6",
                configuration="effort=high",
                clock_source="claude-run-duration",
            )
        )
        self.assertTrue(result["ok"])

        unknown_id = self.finish_lifecycle()
        run_performance.record_internal(
            **self.internal_kwargs(
                unknown_id,
                runner="unknown",
                model="unknown",
                configuration="unknown",
                identity_source="unknown",
            )
        )
        run_performance.observe_task(**self.task_kwargs(unknown_id))
        with self.assertRaisesRegex(run_performance.PerformanceError, "identity conflicts"):
            run_performance.observe_task(
                **self.task_kwargs(
                    unknown_id,
                    model="gpt-5.6-luna-other",
                    clock_source="runner-metadata",
                    identity_source="run-metadata",
                    now_utc="2026-08-11T19:13:00Z",
                )
            )

    def test_all_unknown_identity_is_explicitly_supported(self):
        invocation_id = self.finish_lifecycle()
        result = run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                session="unknown",
                runner="unknown",
                model="unknown",
                configuration="unknown",
                identity_source="unknown",
            )
        )
        self.assertTrue(result["ok"])
        record = self.read_projection()["records"][0]
        self.assertEqual(record["session"], "unknown")
        self.assertEqual(record["runner"], "unknown")

    def test_calendar_unknown_session_is_preserved(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                session='calendar-unknown',
            )
        )
        self.assertEqual(
            self.read_projection()['records'][0]['session'],
            'calendar-unknown',
        )

    def test_append_only_guards_reject_updates_and_deletes(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        connection = sqlite3.connect(self.state)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE performance_events SET model = 'changed' WHERE sequence = 1"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM performance_events WHERE sequence = 1")
        finally:
            connection.close()

    def test_read_only_validation_does_not_repair_or_create_state(self):
        with self.assertRaisesRegex(run_performance.PerformanceError, "missing"):
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            )
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.projection))

        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        before_state = Path(self.state).read_bytes()
        before_projection = Path(self.projection).read_bytes()
        run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )
        self.assertEqual(Path(self.state).read_bytes(), before_state)
        self.assertEqual(Path(self.projection).read_bytes(), before_projection)

    def test_path_aliases_are_rejected_before_mutation(self):
        invocation_id = self.finish_lifecycle()
        equivalent_state = os.path.join(
            os.path.dirname(self.state), ".", os.path.basename(self.state)
        )
        with self.assertRaisesRegex(run_performance.PerformanceError, "aliases"):
            run_performance.publish_projection(self.state, equivalent_state)
        self.assertFalse(os.path.exists(self.state))

        lifecycle_before = Path(self.lifecycle_state).read_bytes()
        with self.assertRaisesRegex(run_performance.PerformanceError, "aliases"):
            run_performance.record_internal(
                **self.internal_kwargs(
                    invocation_id,
                    projection_file=self.lifecycle_state,
                )
            )
        self.assertEqual(Path(self.lifecycle_state).read_bytes(), lifecycle_before)
        self.assertFalse(os.path.exists(self.state))

    def test_projection_corruption_staleness_and_schema_tampering_fail(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        document = self.read_projection()
        document["records"][0]["routine_total_ms"] += 1
        with open(self.projection, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        with self.assertRaises(run_performance.PerformanceError):
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            )

        run_performance.publish_projection(self.state, self.projection)
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("DROP TRIGGER performance_events_no_update")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(run_performance.PerformanceError, "append-only"):
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            )

    def test_projection_rejects_booleans_for_integer_fields(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                now_utc=self.INTERNAL_NOW,
            )
        )
        for path in (
            ("schema_version",),
            ("record_count",),
            ("records", 0, "routine_total_ms"),
        ):
            with self.subTest(path=path):
                document = self.read_projection()
                target = document
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = True
                with self.assertRaisesRegex(
                    run_performance.PerformanceError, "integer"
                ):
                    run_performance.validate_projection(document)

    def test_committed_observation_survives_projection_publish_failure(self):
        invocation_id = self.finish_lifecycle()
        with mock.patch.object(
            run_performance,
            "_atomic_write_projection",
            side_effect=OSError("simulated projection failure"),
        ):
            with self.assertRaises(run_performance.ProjectionPublishError) as raised:
                run_performance.record_internal(
                    **self.internal_kwargs(invocation_id)
                )
        self.assertEqual(raised.exception.action, "record-internal")
        self.assertEqual(raised.exception.invocation_id, invocation_id)
        self.assertTrue(os.path.isfile(self.state))
        self.assertFalse(os.path.exists(self.projection))

        connection = sqlite3.connect(self.state)
        try:
            before = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(before), 1)
        with self.assertRaises(run_performance.PerformanceError):
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            )

        exported = run_performance.publish_projection(self.state, self.projection)
        self.assertEqual(exported["record_count"], 1)
        self.assertEqual(exported["records"][0]["invocation_id"], invocation_id)
        self.assertEqual(
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            ),
            exported,
        )
        connection = sqlite3.connect(self.state)
        try:
            after = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(after, before)

    def test_projection_rejects_unknown_keys_and_guessed_zero_metrics(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
            )
        )
        document = self.read_projection()
        document["records"][0]["note"] = "forbidden"
        with self.assertRaisesRegex(run_performance.PerformanceError, "unknown key"):
            run_performance.validate_projection(document)
        document = self.read_projection()
        document["records"][0]["strategy_execution_ms"] = 0
        with self.assertRaisesRegex(run_performance.PerformanceError, "retain null"):
            run_performance.validate_projection(document)

    def test_projection_is_bounded_to_512_recent_internal_records(self):
        connection = run_performance._connect(self.state)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for index in range(run_performance.PROJECTION_LIMIT + 1):
                invocation_id = str(uuid.UUID(int=index + 1))
                connection.execute(
                    """
                    INSERT INTO performance_events (
                        invocation_id, event_type, occurred_at_utc,
                        lifecycle_started_at_utc, lifecycle_finished_at_utc,
                        strategy_started_at_utc, strategy_finished_at_utc, session,
                        task_duration_ms, runner, model, configuration,
                        identity_source, clock_source
                    ) VALUES (?, 'internal', ?, ?, ?, NULL, NULL, 'unknown',
                              NULL, 'unknown', 'unknown', 'unknown', 'unknown', NULL)
                    """,
                    (
                        invocation_id,
                        "2026-08-11T19:00:02Z",
                        "2026-08-11T19:00:00Z",
                        "2026-08-11T19:00:01Z",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        document = run_performance.publish_projection(self.state, self.projection)
        self.assertEqual(document["record_count"], 512)
        self.assertEqual(len(document["records"]), 512)
        self.assertEqual(document["source_event_high_watermark"], 513)
        self.assertEqual(document["records"][0]["invocation_id"], str(uuid.UUID(int=2)))
        self.assertEqual(document["records"][-1]["invocation_id"], str(uuid.UUID(int=513)))

    def test_task_observation_for_record_outside_projection_window(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        connection = run_performance._connect(self.state)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for index in range(run_performance.PROJECTION_LIMIT):
                later_id = str(uuid.UUID(int=index + 1))
                connection.execute(
                    """
                    INSERT INTO performance_events (
                        invocation_id, event_type, occurred_at_utc,
                        lifecycle_started_at_utc, lifecycle_finished_at_utc,
                        strategy_started_at_utc, strategy_finished_at_utc, session,
                        task_duration_ms, runner, model, configuration,
                        identity_source, clock_source
                    ) VALUES (?, 'internal', ?, ?, ?, NULL, NULL, 'unknown',
                              NULL, 'unknown', 'unknown', 'unknown', 'unknown', NULL)
                    """,
                    (
                        later_id,
                        "2026-08-11T19:11:00Z",
                        "2026-08-11T19:00:00Z",
                        "2026-08-11T19:10:00Z",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        run_performance.publish_projection(self.state, self.projection)
        self.assertNotIn(
            invocation_id,
            {record["invocation_id"] for record in self.read_projection()["records"]},
        )
        result = run_performance.observe_task(**self.task_kwargs(invocation_id))
        self.assertEqual(result["selected_clock_source"], "codex-worked-for")
        self.assertEqual(result["outside_lifecycle_ms"], 120_000)
        run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )

    def test_filesystem_preflight_failure_happens_before_performance_state_open(self):
        invocation_id = self.finish_lifecycle()
        with mock.patch.object(
            run_performance.run_lifecycle,
            "_prepare_state_directory",
            side_effect=run_lifecycle.LifecycleError("unsafe filesystem"),
        ):
            with self.assertRaisesRegex(
                run_performance.PerformanceError, "filesystem preflight"
            ):
                run_performance.record_internal(
                    **self.internal_kwargs(invocation_id)
                )
        self.assertFalse(os.path.exists(self.state))

    def test_cli_export_validate_and_errors_emit_one_strict_json_object(self):
        command = [
            sys.executable,
            os.path.join(ROOT, "run_performance.py"),
            "export",
            "--state-file",
            self.state,
            "--projection-file",
            self.projection,
        ]
        exported = subprocess.run(command, text=True, capture_output=True, check=False)
        self.assertEqual(exported.returncode, 0, exported.stderr)
        self.assertEqual(exported.stderr, "")
        export_lines = exported.stdout.splitlines()
        self.assertEqual(len(export_lines), 1)
        self.assertEqual(json.loads(export_lines[0])["action"], "export")

        validated = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "run_performance.py"),
                "validate",
                "--state-file",
                self.state,
                "--projection-file",
                self.projection,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertEqual(len(validated.stdout.splitlines()), 1)
        self.assertTrue(json.loads(validated.stdout)["ok"])

        invalid = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "run_performance.py"),
                "export",
                "--notes",
                "account data is forbidden",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid.returncode, 1)
        self.assertEqual(invalid.stderr, "")
        invalid_lines = invalid.stdout.splitlines()
        self.assertEqual(len(invalid_lines), 1)
        payload = json.loads(invalid_lines[0])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["reason"], "performance_state_error")


if __name__ == "__main__":
    unittest.main()
