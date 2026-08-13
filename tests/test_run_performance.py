import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
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
    RUN_START_PT = "2026-08-11T12:00:30-07:00"

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
        run_start_pt=None,
        strategy_markers=False,
    ):
        invocation_id = invocation_id or str(uuid.uuid4())
        run_lifecycle.start_invocation(
            invocation_id=invocation_id,
            state_file=self.lifecycle_state,
            projection_file=self.lifecycle_projection,
            now_utc=start,
        )
        if run_start_pt is not None:
            run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase="preflight",
                run_start_pt=run_start_pt,
                state_file=self.lifecycle_state,
                projection_file=self.lifecycle_projection,
                now_utc=datetime.fromisoformat(run_start_pt).astimezone(
                    timezone.utc
                ).isoformat().replace("+00:00", "Z"),
            )
        if strategy_markers:
            run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase="position-management",
                state_file=self.lifecycle_state,
                projection_file=self.lifecycle_projection,
                now_utc=self.STRATEGY_START,
            )
            run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase="report",
                state_file=self.lifecycle_state,
                projection_file=self.lifecycle_projection,
                now_utc=self.STRATEGY_FINISH,
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

    def identity_kwargs(self, invocation_id, **overrides):
        values = {
            "invocation_id": invocation_id,
            "self_identity": "Codex|Codex Luna 5.6|high",
            "declared_identity": "absent",
            "metadata_identity": "absent",
            "state_file": self.state,
            "projection_file": self.projection,
            "lifecycle_state_file": self.lifecycle_state,
            "lifecycle_projection_file": self.lifecycle_projection,
            "now_utc": "2026-08-11T19:00:01Z",
            "runtime_environment": {},
        }
        values.update(overrides)
        return values

    def finish_started_lifecycle(self, invocation_id, *, finish=FINISH):
        run_lifecycle.finish_invocation(
            invocation_id=invocation_id,
            classification="completed",
            state_file=self.lifecycle_state,
            projection_file=self.lifecycle_projection,
            now_utc=finish,
        )

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
                "estimated_run_start_pt",
                "estimated_run_end_pt",
                "estimated_run_total_ms",
                "estimated_run_total_display",
                "estimate_clock_source",
                "projection_record_count",
            },
        )
        self.assertEqual(internal["routine_total_ms"], 600_000)
        self.assertEqual(internal["strategy_execution_ms"], 360_000)
        self.assertEqual(internal["routine_overhead_ms"], 240_000)
        self.assertEqual(
            internal["schema_version"], run_performance.RECEIPT_SCHEMA_VERSION
        )
        self.assertIsNone(internal["estimated_run_start_pt"])
        self.assertIsNone(internal["estimated_run_end_pt"])
        self.assertIsNone(internal["estimated_run_total_ms"])
        self.assertIsNone(internal["estimated_run_total_display"])
        self.assertIsNone(internal["estimate_clock_source"])

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
        self.assertEqual(
            document["schema_version"], run_performance.PROJECTION_SCHEMA_VERSION
        )
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

    def test_host_stamped_lifecycle_markers_supply_strategy_boundaries(self):
        invocation_id = self.finish_lifecycle(strategy_markers=True)
        receipt = run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
            )
        )
        self.assertEqual(receipt["strategy_execution_ms"], 360_000)
        self.assertEqual(receipt["routine_overhead_ms"], 240_000)
        record = self.read_projection()["records"][0]
        self.assertEqual(record["strategy_started_at_utc"], self.STRATEGY_START)
        self.assertEqual(record["strategy_finished_at_utc"], self.STRATEGY_FINISH)

    def test_lifecycle_markers_reject_conflicting_supplied_boundaries(self):
        invocation_id = self.finish_lifecycle(strategy_markers=True)
        with self.assertRaisesRegex(
            run_performance.PerformanceError,
            "conflict with host-stamped lifecycle markers",
        ):
            run_performance.record_internal(
                **self.internal_kwargs(
                    invocation_id,
                    strategy_start_utc="2026-08-11T19:01:59Z",
                )
            )
        self.assertFalse(os.path.exists(self.state))

    def test_final_summary_duration_reuses_one_clock_and_remains_primary(self):
        invocation_id = self.finish_lifecycle(run_start_pt=self.RUN_START_PT)
        boundary = "2026-08-11T19:10:10Z"
        with mock.patch.object(
            run_performance,
            "_now_utc",
            wraps=run_performance._now_utc,
        ) as clock:
            receipt = run_performance.record_internal(
                **self.internal_kwargs(invocation_id, now_utc=boundary)
            )
        clock.assert_called_once_with(boundary)
        self.assertEqual(receipt["estimated_run_start_pt"], self.RUN_START_PT)
        self.assertEqual(
            receipt["estimated_run_end_pt"], "2026-08-11T12:10:10-07:00"
        )
        self.assertEqual(receipt["estimated_run_total_ms"], 580_000)
        self.assertEqual(receipt["estimated_run_total_display"], "9:40")
        self.assertEqual(
            receipt["estimate_clock_source"],
            run_performance.ESTIMATE_CLOCK_SOURCE,
        )

        estimated = self.read_projection()["records"][0]
        self.assertLess(
            estimated["task_duration_ms"], estimated["routine_total_ms"]
        )
        self.assertEqual(estimated["task_duration_ms"], 580_000)
        self.assertEqual(
            estimated["task_clock_source"],
            run_performance.ESTIMATE_CLOCK_SOURCE,
        )
        self.assertEqual(estimated["task_observed_at_utc"], boundary)
        self.assertIsNone(estimated["outside_lifecycle_ms"])
        self.assertIsNone(estimated["total_overhead_ms"])
        run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )

        observed = run_performance.observe_task(
            **self.task_kwargs(invocation_id)
        )
        self.assertEqual(observed["selected_clock_source"], "codex-worked-for")
        self.assertEqual(observed["outside_lifecycle_ms"], 120_000)
        self.assertEqual(observed["total_overhead_ms"], 360_000)
        canonical = self.read_projection()["records"][0]
        self.assertEqual(canonical["task_duration_ms"], 580_000)
        self.assertEqual(
            canonical["task_clock_source"],
            run_performance.ESTIMATE_CLOCK_SOURCE,
        )
        self.assertIsNone(canonical["outside_lifecycle_ms"])
        self.assertIsNone(canonical["total_overhead_ms"])

        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            references = connection.execute(
                "SELECT * FROM performance_events "
                "WHERE invocation_id = ? AND event_type = 'task'",
                (invocation_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["task_duration_ms"], 720_000)
        self.assertEqual(references[0]["clock_source"], "codex-worked-for")

    def test_external_observation_is_fallback_without_canonical_duration(self):
        invocation_id = self.finish_lifecycle(run_start_pt=None)
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        receipt = run_performance.observe_task(
            **self.task_kwargs(invocation_id)
        )
        self.assertEqual(receipt["selected_clock_source"], "codex-worked-for")
        self.assertEqual(receipt["outside_lifecycle_ms"], 120_000)
        self.assertEqual(receipt["total_overhead_ms"], 360_000)
        fallback = self.read_projection()["records"][0]
        self.assertEqual(fallback["task_duration_ms"], 720_000)
        self.assertEqual(fallback["task_clock_source"], "codex-worked-for")
        self.assertEqual(fallback["outside_lifecycle_ms"], 120_000)
        self.assertEqual(fallback["total_overhead_ms"], 360_000)

    def test_estimate_duration_display_rounds_like_dashboard(self):
        self.assertEqual(run_performance._duration_display(0), "0:00")
        self.assertEqual(run_performance._duration_display(59_499), "0:59")
        self.assertEqual(run_performance._duration_display(59_500), "1:00")
        self.assertEqual(run_performance._duration_display(3_661_500), "1:01:02")

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
        with self.assertRaisesRegex(run_performance.PerformanceError, "expected one of"):
            run_performance.observe_task(
                **self.task_kwargs(
                    invocation_id,
                    clock_source=run_performance.ESTIMATE_CLOCK_SOURCE,
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

    def test_identity_registry_is_exact_and_preserves_partial_self_report(self):
        partial = run_performance._resolve_identity_claims(
            "absent",
            "absent",
            " Claude Desktop Code | Claude Sonnet 5 | unknown ",
        )
        self.assertEqual(
            partial,
            {
                "runner": "claude",
                "model": "claude-sonnet-5",
                "configuration": "unknown",
                "identity_source": "self-reported",
                "identity_warning": "self-incomplete",
                "runner_identity_source": "self-reported",
                "model_identity_source": "self-reported",
                "configuration_identity_source": "unknown",
            },
        )
        generic = run_performance._resolve_identity_claims(
            "absent", "absent", "OpenAI Codex|GPT-5|reasoning=high"
        )
        self.assertEqual(generic["runner"], "codex")
        self.assertEqual(generic["model"], "unknown")
        self.assertEqual(generic["configuration"], "reasoning=high")
        self.assertEqual(generic["identity_warning"], "self-unrecognized")
        sol = run_performance._resolve_identity_claims(
            "absent", "absent", "Codex Desktop|GPT 5.6 Sol|high"
        )
        self.assertEqual(sol["model"], "gpt-5.6-sol")
        self.assertIsNone(sol["identity_warning"])

        missing_self = run_performance._resolve_identity_claims(
            "absent", "absent", "absent"
        )
        self.assertEqual(missing_self["identity_source"], "unknown")
        self.assertEqual(missing_self["identity_warning"], "self-unrecognized")

    def test_identity_precedence_and_deterministic_conflict_warnings(self):
        matching = run_performance._resolve_identity_claims(
            "Codex Desktop|GPT 5.6 Luna|high",
            "codex|gpt-5.6-luna|reasoning=high",
            "OpenAI Codex|Codex Luna 5.6|high",
        )
        self.assertEqual(matching["identity_source"], "run-metadata")
        self.assertIsNone(matching["identity_warning"])

        conflict = run_performance._resolve_identity_claims(
            "codex|gpt-5.6-luna|reasoning=high",
            "claude|claude-sonnet-5|effort=high",
            "Claude Code|Claude Sonnet 5|high",
        )
        self.assertEqual(conflict["runner"], "codex")
        self.assertEqual(
            conflict["identity_warning"], "metadata-declaration-conflict"
        )

        fallback = run_performance._resolve_identity_claims(
            "invalid",
            "Claude Code|Claude Sonnet 5|effort high",
            "Claude|Claude Sonnet 5|high",
        )
        self.assertEqual(fallback["identity_source"], "declared")
        self.assertEqual(fallback["identity_warning"], "metadata-invalid")

    def test_claude_runtime_environment_exact_effort_values_are_composite(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                resolved = run_performance._resolve_identity_claims(
                    "absent",
                    "absent",
                    "unknown|Claude Sonnet 5|unknown",
                    {"CLAUDECODE": "1", "CLAUDE_EFFORT": effort},
                )
                self.assertEqual(resolved["runner"], "claude")
                self.assertEqual(resolved["model"], "claude-sonnet-5")
                self.assertEqual(resolved["configuration"], f"effort={effort}")
                self.assertEqual(resolved["identity_source"], "composite")
                self.assertEqual(
                    resolved["runner_identity_source"], "runtime-environment"
                )
                self.assertEqual(resolved["model_identity_source"], "self-reported")
                self.assertEqual(
                    resolved["configuration_identity_source"], "runtime-environment"
                )
                self.assertEqual(
                    resolved["identity_warning"], "runtime-self-unverified"
                )

    def test_runtime_environment_missing_and_invalid_evidence_is_bounded(self):
        missing_marker = run_performance._resolve_identity_claims(
            "absent",
            "absent",
            "unknown|Claude Sonnet 5|unknown",
            {"CLAUDE_EFFORT": "high"},
        )
        self.assertEqual(missing_marker["identity_source"], "self-reported")
        self.assertEqual(
            missing_marker["identity_warning"], "runtime-environment-invalid"
        )

        missing_effort = run_performance._resolve_identity_claims(
            "absent",
            "absent",
            "unknown|Claude Sonnet 5|unknown",
            {"CLAUDECODE": "1"},
        )
        self.assertEqual(missing_effort["runner"], "claude")
        self.assertEqual(missing_effort["configuration"], "unknown")
        self.assertEqual(missing_effort["identity_source"], "composite")
        self.assertEqual(
            missing_effort["identity_warning"], "runtime-self-unverified"
        )

        for environment in (
            {"CLAUDECODE": "0", "CLAUDE_EFFORT": "high"},
            {"CLAUDECODE": "1", "CLAUDE_EFFORT": "turbo"},
            {"CLAUDECODE": "1", "CLAUDE_EFFORT": "HIGH"},
        ):
            with self.subTest(environment=environment):
                result = run_performance._resolve_identity_claims(
                    "absent",
                    "absent",
                    "unknown|Claude Sonnet 5|unknown",
                    environment,
                )
                self.assertEqual(
                    result["identity_warning"], "runtime-environment-invalid"
                )

    def test_runtime_environment_reads_only_two_allowlisted_keys(self):
        class GuardedEnvironment:
            def __init__(self):
                self.reads = []

            def get(self, key, default=None):
                self.reads.append(key)
                if key == "CLAUDECODE":
                    return "1"
                if key == "CLAUDE_EFFORT":
                    return "high"
                raise AssertionError(f"forbidden environment key read: {key}")

            def __iter__(self):
                raise AssertionError("environment enumeration is forbidden")

        environment = GuardedEnvironment()
        claim = run_performance._runtime_environment_claim(environment)
        self.assertEqual(environment.reads, ["CLAUDECODE", "CLAUDE_EFFORT"])
        self.assertEqual(claim["runner"], "claude")
        self.assertEqual(claim["configuration"], "effort=high")

    def test_runtime_self_conflict_does_not_promote_self_report(self):
        resolved = run_performance._resolve_identity_claims(
            "absent",
            "absent",
            "Codex|Codex Luna 5.6|high",
            {"CLAUDECODE": "1", "CLAUDE_EFFORT": "high"},
        )
        self.assertEqual(resolved["runner"], "claude")
        self.assertEqual(resolved["model"], "unknown")
        self.assertEqual(resolved["configuration"], "effort=high")
        self.assertEqual(resolved["identity_source"], "runtime-environment")
        self.assertEqual(resolved["identity_warning"], "runtime-self-conflict")

    def test_runtime_only_partial_identity_is_explicitly_incomplete(self):
        resolved = run_performance._resolve_identity_claims(
            "absent",
            "absent",
            "unknown|unknown|unknown",
            {"CLAUDECODE": "1", "CLAUDE_EFFORT": "high"},
        )
        self.assertEqual(resolved["identity_source"], "runtime-environment")
        self.assertEqual(resolved["model"], "unknown")
        self.assertEqual(
            resolved["identity_warning"], "runtime-environment-incomplete"
        )

    def test_resolve_identity_persists_before_clock_and_record_consumes_it(self):
        invocation_id = self.start_lifecycle()
        receipt = run_performance.resolve_identity(
            **self.identity_kwargs(invocation_id)
        )
        self.assertEqual(
            set(receipt),
            {
                "schema_version",
                "action",
                "ok",
                "invocation_id",
                "runner",
                "model",
                "configuration",
                "identity_source",
                "identity_warning",
                "runner_identity_source",
                "model_identity_source",
                "configuration_identity_source",
            },
        )
        self.assertEqual(receipt["identity_source"], "self-reported")
        self.assertIsNone(receipt["identity_warning"])
        self.finish_started_lifecycle(invocation_id)
        kwargs = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            kwargs.pop(field)
        run_performance.record_internal(**kwargs)
        record = self.read_projection()["records"][0]
        self.assertEqual(record["runner"], "codex")
        self.assertEqual(record["model"], "gpt-5.6-luna")
        self.assertEqual(record["configuration"], "reasoning=high")
        self.assertEqual(record["identity_source"], "self-reported")
        self.assertIsNone(record["identity_warning"])

        connection = sqlite3.connect(self.state)
        connection.row_factory = sqlite3.Row
        try:
            internal = connection.execute(
                "SELECT * FROM performance_events WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(internal["runner"], "unknown")
        self.assertEqual(internal["identity_source"], "unknown")

    def test_runtime_environment_snapshot_is_canonical_private_and_immutable(self):
        invocation_id = self.start_lifecycle()
        environment = {
            "CLAUDECODE": "1",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_CODE_SESSION_ID": "private-session-id",
            "BAGGAGE": "private-baggage",
            "USER_EMAIL": "private@example.test",
        }
        receipt = run_performance.resolve_identity(
            **self.identity_kwargs(
                invocation_id,
                self_identity="unknown|Claude Sonnet 5|unknown",
                runtime_environment=environment,
            )
        )
        environment["CLAUDE_EFFORT"] = "low"
        environment["CLAUDE_CODE_SESSION_ID"] = "changed-session-id"
        serialized_receipt = json.dumps(receipt, sort_keys=True)
        for private in (
            "private-session-id",
            "private-baggage",
            "private@example.test",
            "changed-session-id",
        ):
            self.assertNotIn(private, serialized_receipt)

        self.finish_started_lifecycle(invocation_id)
        kwargs = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            kwargs.pop(field)
        run_performance.record_internal(**kwargs)
        record = self.read_projection()["records"][0]
        self.assertEqual(record["configuration"], "effort=high")
        self.assertEqual(record["identity_source"], "composite")
        self.assertEqual(record["model_identity_source"], "self-reported")
        serialized_projection = json.dumps(record, sort_keys=True)

        connection = sqlite3.connect(self.state)
        try:
            stored = connection.execute(
                "SELECT * FROM performance_identities WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        finally:
            connection.close()
        serialized_row = repr(stored)
        for private in (
            "private-session-id",
            "private-baggage",
            "private@example.test",
            "changed-session-id",
        ):
            self.assertNotIn(private, serialized_projection)
            self.assertNotIn(private, serialized_row)

    def test_invalid_runtime_values_are_not_persisted_verbatim(self):
        invocation_id = self.start_lifecycle()
        secret_marker = "1;TOKEN=do-not-store"
        secret_effort = "high;PASSWORD=do-not-store"
        receipt = run_performance.resolve_identity(
            **self.identity_kwargs(
                invocation_id,
                self_identity="unknown|Claude Sonnet 5|unknown",
                runtime_environment={
                    "CLAUDECODE": secret_marker,
                    "CLAUDE_EFFORT": secret_effort,
                },
            )
        )
        self.assertEqual(
            receipt["identity_warning"], "runtime-environment-invalid"
        )
        connection = sqlite3.connect(self.state)
        try:
            row = connection.execute(
                "SELECT * FROM performance_identities WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        finally:
            connection.close()
        for secret in (secret_marker, secret_effort):
            self.assertNotIn(secret, json.dumps(receipt, sort_keys=True))
            self.assertNotIn(secret, repr(row))

    def test_strong_resolution_rejects_conflict_and_accepts_unknown_fallback(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(
            **self.identity_kwargs(
                invocation_id,
                metadata_identity="Codex|gpt-5.6-luna|reasoning=high",
            )
        )
        self.finish_started_lifecycle(invocation_id)
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "invocation-bound resolution"
        ):
            run_performance.record_internal(
                **self.internal_kwargs(
                    invocation_id,
                    runner="claude",
                    model="claude-sonnet-5",
                    configuration="effort=high",
                    identity_source="declared",
                )
            )
        run_performance.record_internal(
            **self.internal_kwargs(
                invocation_id,
                runner="unknown",
                model="unknown",
                configuration="unknown",
                identity_source="unknown",
            )
        )
        record = self.read_projection()["records"][0]
        self.assertEqual(record["identity_source"], "run-metadata")
        self.assertEqual(record["model"], "gpt-5.6-luna")

    def test_identity_resolution_requires_running_lifecycle_and_is_append_only(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        with self.assertRaises(run_performance.PerformanceConflict):
            run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        connection = sqlite3.connect(self.state)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE performance_identities SET model = 'changed'"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM performance_identities")
        finally:
            connection.close()

        finished_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z", finish="2026-08-11T20:01:00Z"
        )
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "requires a running lifecycle"
        ):
            run_performance.resolve_identity(
                **self.identity_kwargs(
                    finished_id, now_utc="2026-08-11T20:00:01Z"
                )
            )

    def test_trusted_external_identity_must_be_complete_before_superseding(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        self.finish_started_lifecycle(invocation_id)
        internal = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            internal.pop(field)
        run_performance.record_internal(**internal)
        run_performance.observe_task(
            **self.task_kwargs(
                invocation_id,
                model="unknown",
                configuration="unknown",
                identity_source="manual-ui",
                clock_source="manual-observation",
            )
        )

        record = next(
            item
            for item in self.read_projection()["records"]
            if item["invocation_id"] == invocation_id
        )
        self.assertEqual(record["runner"], "codex")
        self.assertEqual(record["model"], "gpt-5.6-luna")
        self.assertEqual(record["configuration"], "reasoning=high")
        self.assertEqual(record["identity_source"], "self-reported")
        self.assertIsNone(record["identity_warning"])

    def test_complete_trusted_external_identity_clears_only_self_warnings(self):
        for index, self_identity in enumerate(
            (
                "unknown|unknown|unknown",
                "OpenAI Codex|GPT-5|reasoning=high",
            )
        ):
            with self.subTest(self_identity=self_identity):
                invocation_id = self.start_lifecycle(
                    start=f"2026-08-11T20:0{index}:00Z"
                )
                run_performance.resolve_identity(
                    **self.identity_kwargs(
                        invocation_id,
                        self_identity=self_identity,
                        now_utc=f"2026-08-11T20:0{index}:01Z",
                    )
                )
                self.finish_started_lifecycle(
                    invocation_id, finish=f"2026-08-11T20:0{index}:30Z"
                )
                internal = self.internal_kwargs(
                    invocation_id,
                    strategy_start_utc=None,
                    strategy_end_utc=None,
                    now_utc=f"2026-08-11T20:0{index}:31Z",
                )
                for field in ("runner", "model", "configuration", "identity_source"):
                    internal.pop(field)
                run_performance.record_internal(**internal)
                run_performance.observe_task(
                    **self.task_kwargs(
                        invocation_id,
                        task_duration_ms=40_000,
                        clock_source="manual-observation",
                        now_utc=f"2026-08-11T20:0{index}:32Z",
                    )
                )

                record = next(
                    item
                    for item in self.read_projection()["records"]
                    if item["invocation_id"] == invocation_id
                )
                self.assertEqual(record["runner"], "codex")
                self.assertEqual(record["model"], "gpt-5.6-luna")
                self.assertEqual(record["configuration"], "reasoning=high")
                self.assertEqual(record["identity_source"], "manual-ui")
                self.assertIsNone(record["identity_warning"])

    def test_trusted_external_rehabilitates_matching_runtime_composite(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(
            **self.identity_kwargs(
                invocation_id,
                self_identity="unknown|Claude Sonnet 5|unknown",
                runtime_environment={"CLAUDECODE": "1", "CLAUDE_EFFORT": "high"},
            )
        )
        self.finish_started_lifecycle(invocation_id)
        internal = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            internal.pop(field)
        run_performance.record_internal(**internal)
        run_performance.observe_task(
            **self.task_kwargs(
                invocation_id,
                runner="claude",
                model="claude-sonnet-5",
                configuration="effort=high",
                identity_source="run-metadata",
                clock_source="manual-observation",
            )
        )
        record = self.read_projection()["records"][0]
        self.assertEqual(record["identity_source"], "run-metadata")
        self.assertEqual(record["runner_identity_source"], "run-metadata")
        self.assertEqual(record["model_identity_source"], "run-metadata")
        self.assertEqual(record["configuration_identity_source"], "run-metadata")
        self.assertIsNone(record["identity_warning"])

    def test_trusted_external_rehabilitates_invalid_lower_runtime_evidence(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(
            **self.identity_kwargs(
                invocation_id,
                self_identity="Claude|Claude Sonnet 5|high",
                runtime_environment={
                    "CLAUDECODE": "spoofed",
                    "CLAUDE_EFFORT": "high",
                },
            )
        )
        self.finish_started_lifecycle(invocation_id)
        internal = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            internal.pop(field)
        run_performance.record_internal(**internal)
        run_performance.observe_task(
            **self.task_kwargs(
                invocation_id,
                runner="claude",
                model="claude-sonnet-5",
                configuration="effort=high",
                identity_source="run-metadata",
                clock_source="manual-observation",
            )
        )
        record = self.read_projection()["records"][0]
        self.assertEqual(record["identity_source"], "run-metadata")
        self.assertIsNone(record["identity_warning"])

    def test_trusted_external_conflicts_name_runtime_or_self_provenance(self):
        cases = (
            (
                "unknown|unknown|unknown",
                {"runner": "codex", "model": "gpt-5.6-luna", "configuration": "reasoning=high"},
                "metadata-runtime-conflict",
            ),
            (
                "unknown|Claude Sonnet 5|unknown",
                {"runner": "claude", "model": "claude-sonnet-4-6", "configuration": "effort=high"},
                "metadata-self-conflict",
            ),
        )
        for index, (self_identity, trusted, warning) in enumerate(cases):
            with self.subTest(warning=warning):
                invocation_id = self.start_lifecycle(
                    start=f"2026-08-11T20:1{index}:00Z"
                )
                run_performance.resolve_identity(
                    **self.identity_kwargs(
                        invocation_id,
                        self_identity=self_identity,
                        runtime_environment={
                            "CLAUDECODE": "1",
                            "CLAUDE_EFFORT": "high",
                        },
                    )
                )
                self.finish_started_lifecycle(
                    invocation_id, finish=f"2026-08-11T20:1{index}:30Z"
                )
                internal = self.internal_kwargs(
                    invocation_id,
                    strategy_start_utc=None,
                    strategy_end_utc=None,
                    now_utc=f"2026-08-11T20:1{index}:31Z",
                )
                for field in ("runner", "model", "configuration", "identity_source"):
                    internal.pop(field)
                run_performance.record_internal(**internal)
                run_performance.observe_task(
                    **self.task_kwargs(
                        invocation_id,
                        **trusted,
                        identity_source="run-metadata",
                        clock_source="manual-observation",
                        now_utc=f"2026-08-11T20:1{index}:32Z",
                    )
                )
                record = next(
                    item
                    for item in self.read_projection()["records"]
                    if item["invocation_id"] == invocation_id
                )
                self.assertEqual(record["identity_source"], "run-metadata")
                self.assertEqual(record["identity_warning"], warning)

    def test_resolve_identity_revalidates_running_lifecycle_inside_transaction(self):
        invocation_id = self.start_lifecycle()
        original = run_performance._require_running_lifecycle_invocation
        calls = 0

        def finish_after_initial_check(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            if calls == 1:
                self.finish_started_lifecycle(invocation_id)
            return result

        with mock.patch.object(
            run_performance,
            "_require_running_lifecycle_invocation",
            side_effect=finish_after_initial_check,
        ):
            with self.assertRaisesRegex(
                run_performance.PerformanceError,
                "requires a running lifecycle",
            ):
                run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        self.assertEqual(calls, 2)
        connection = sqlite3.connect(self.state)
        try:
            identity_count = connection.execute(
                "SELECT COUNT(*) FROM performance_identities"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(identity_count, 0)

    def test_resolve_identity_rejects_internal_observation_that_wins_race(self):
        invocation_id = self.start_lifecycle()
        original = run_performance._require_running_lifecycle_invocation
        calls = 0

        def finalize_after_initial_check(*args):
            nonlocal calls
            calls += 1
            result = original(*args)
            if calls == 1:
                self.finish_started_lifecycle(invocation_id)
                run_performance.record_internal(
                    **self.internal_kwargs(invocation_id)
                )
            return result

        with mock.patch.object(
            run_performance,
            "_require_running_lifecycle_invocation",
            side_effect=finalize_after_initial_check,
        ):
            with self.assertRaisesRegex(
                run_performance.PerformanceConflict,
                "cannot follow an internal observation",
            ):
                run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        self.assertEqual(calls, 1)
        connection = sqlite3.connect(self.state)
        try:
            identity_count = connection.execute(
                "SELECT COUNT(*) FROM performance_identities"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(identity_count, 0)
        run_performance.validate_current_projection_read_only(
            self.state, self.projection
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
        invocation_id = self.finish_lifecycle(run_start_pt=self.RUN_START_PT)
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        connection = sqlite3.connect(self.state)
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE performance_events SET model = 'changed' WHERE sequence = 1"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM performance_events WHERE sequence = 1")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute(
                    "UPDATE performance_estimates "
                    "SET estimated_run_total_ms = estimated_run_total_ms + 1"
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                connection.execute("DELETE FROM performance_estimates")
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

    def test_read_only_accepts_v1_and_next_writer_migrates_without_event_loss(self):
        legacy_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(legacy_id))
        run_performance.observe_task(**self.task_kwargs(legacy_id))
        connection = sqlite3.connect(self.state)
        try:
            legacy_events = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
            connection.execute("DROP TABLE performance_estimates")
            connection.execute("DROP TABLE performance_identities")
            connection.execute(
                "UPDATE performance_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(run_performance.LEGACY_JOURNAL_SCHEMA_VERSION),),
            )
            connection.commit()
        finally:
            connection.close()

        with open(self.projection, encoding="utf-8") as handle:
            legacy_projection_file = json.load(handle)
        legacy_projection_file["schema_version"] = (
            run_performance.LEGACY_PROJECTION_SCHEMA_VERSION
        )
        for record in legacy_projection_file["records"]:
            record.pop("identity_warning")
            for field in ("runner", "model", "configuration"):
                record.pop(f"{field}_identity_source")
        with open(self.projection, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(legacy_projection_file, handle, sort_keys=True, indent=2)
            handle.write("\n")

        legacy_projection = run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )
        self.assertEqual(
            legacy_projection["schema_version"],
            run_performance.LEGACY_PROJECTION_SCHEMA_VERSION,
        )
        legacy_record = legacy_projection["records"][0]
        self.assertEqual(legacy_record["invocation_id"], legacy_id)
        self.assertEqual(legacy_record["task_duration_ms"], 720_000)
        self.assertEqual(legacy_record["task_clock_source"], "codex-worked-for")

        current_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z",
            finish="2026-08-11T20:10:00Z",
            run_start_pt="2026-08-11T13:00:30-07:00",
        )
        receipt = run_performance.record_internal(
            **self.internal_kwargs(
                current_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                now_utc="2026-08-11T20:11:00Z",
            )
        )
        self.assertEqual(receipt["estimated_run_total_ms"], 630_000)

        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute(
                "SELECT value FROM performance_metadata "
                "WHERE key = 'schema_version'"
            ).fetchone()[0]
            migrated_events = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
            estimate_count = connection.execute(
                "SELECT COUNT(*) FROM performance_estimates"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, str(run_performance.JOURNAL_SCHEMA_VERSION))
        self.assertEqual(migrated_events[:len(legacy_events)], legacy_events)
        self.assertEqual(len(migrated_events), len(legacy_events) + 1)
        self.assertEqual(estimate_count, 1)
        migrated_projection = run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )
        self.assertEqual(
            migrated_projection["schema_version"],
            run_performance.PROJECTION_SCHEMA_VERSION,
        )
        migrated_legacy = next(
            record
            for record in migrated_projection["records"]
            if record["invocation_id"] == legacy_id
        )
        self.assertEqual(migrated_legacy["task_duration_ms"], 720_000)
        self.assertEqual(
            migrated_legacy["task_clock_source"], "codex-worked-for"
        )

    def test_v2_journal_migrates_to_v3_without_event_loss(self):
        legacy_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(legacy_id))
        connection = sqlite3.connect(self.state)
        try:
            before = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
            connection.execute("DROP TABLE performance_identities")
            connection.execute(
                "UPDATE performance_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(run_performance.PREVIOUS_JOURNAL_SCHEMA_VERSION),),
            )
            connection.commit()
        finally:
            connection.close()
        document = self.read_projection()
        document["schema_version"] = run_performance.PREVIOUS_PROJECTION_SCHEMA_VERSION
        for record in document["records"]:
            record.pop("identity_warning")
            for field in ("runner", "model", "configuration"):
                record.pop(f"{field}_identity_source")
        with open(self.projection, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, sort_keys=True, indent=2)
            handle.write("\n")
        run_performance.validate_current_projection_read_only(
            self.state, self.projection
        )

        current_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z", finish="2026-08-11T20:10:00Z"
        )
        run_performance.record_internal(
            **self.internal_kwargs(
                current_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                now_utc="2026-08-11T20:11:00Z",
            )
        )
        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute(
                "SELECT value FROM performance_metadata "
                "WHERE key = 'schema_version'"
            ).fetchone()[0]
            after = connection.execute(
                "SELECT * FROM performance_events ORDER BY sequence"
            ).fetchall()
            identity_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(performance_identities)"
                )
            }
        finally:
            connection.close()
        self.assertEqual(version, str(run_performance.JOURNAL_SCHEMA_VERSION))
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(identity_columns, run_performance._IDENTITY_COLUMNS)

    def test_v3_identity_journal_migrates_to_v4_without_identity_loss(self):
        invocation_id = self.start_lifecycle()
        run_performance.resolve_identity(**self.identity_kwargs(invocation_id))
        self.finish_started_lifecycle(invocation_id)
        kwargs = self.internal_kwargs(invocation_id)
        for field in ("runner", "model", "configuration", "identity_source"):
            kwargs.pop(field)
        run_performance.record_internal(**kwargs)
        connection = sqlite3.connect(self.state)
        try:
            original = connection.execute(
                "SELECT invocation_id, occurred_at_utc, runner, model, configuration, "
                "identity_source, identity_warning FROM performance_identities"
            ).fetchone()
            connection.execute("DROP TRIGGER performance_identities_no_update")
            connection.execute("DROP TRIGGER performance_identities_no_delete")
            connection.execute("ALTER TABLE performance_identities RENAME TO identity_v4")
            connection.execute(
                """
                CREATE TABLE performance_identities (
                    invocation_id TEXT PRIMARY KEY,
                    occurred_at_utc TEXT NOT NULL,
                    runner TEXT NOT NULL,
                    model TEXT NOT NULL,
                    configuration TEXT NOT NULL,
                    identity_source TEXT NOT NULL,
                    identity_warning TEXT
                )
                """
            )
            connection.execute(
                "INSERT INTO performance_identities VALUES (?, ?, ?, ?, ?, ?, ?)",
                original,
            )
            connection.execute("DROP TABLE identity_v4")
            connection.execute(
                """
                CREATE TRIGGER performance_identities_no_update
                BEFORE UPDATE ON performance_identities
                BEGIN
                    SELECT RAISE(ABORT, 'performance identities are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER performance_identities_no_delete
                BEFORE DELETE ON performance_identities
                BEGIN
                    SELECT RAISE(ABORT, 'performance identities are append-only');
                END
                """
            )
            connection.execute(
                "UPDATE performance_metadata SET value = ? "
                "WHERE key = 'schema_version'",
                (str(run_performance.IDENTITY_JOURNAL_SCHEMA_VERSION),),
            )
            connection.commit()
        finally:
            connection.close()

        v3_projection = self.read_projection()
        v3_projection["schema_version"] = (
            run_performance.IDENTITY_PROJECTION_SCHEMA_VERSION
        )
        for record in v3_projection["records"]:
            for field in ("runner", "model", "configuration"):
                record.pop(f"{field}_identity_source")
        with open(self.projection, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(v3_projection, handle, sort_keys=True, indent=2)
            handle.write("\n")
        before_state = Path(self.state).read_bytes()
        before_projection = Path(self.projection).read_bytes()
        self.assertEqual(
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            ),
            v3_projection,
        )
        self.assertEqual(Path(self.state).read_bytes(), before_state)
        self.assertEqual(Path(self.projection).read_bytes(), before_projection)

        current_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z", finish="2026-08-11T20:10:00Z"
        )
        run_performance.record_internal(
            **self.internal_kwargs(
                current_id,
                strategy_start_utc=None,
                strategy_end_utc=None,
                now_utc="2026-08-11T20:11:00Z",
            )
        )

        connection = sqlite3.connect(self.state)
        try:
            version = connection.execute(
                "SELECT value FROM performance_metadata WHERE key = 'schema_version'"
            ).fetchone()[0]
            migrated = connection.execute(
                "SELECT * FROM performance_identities WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(version, str(run_performance.JOURNAL_SCHEMA_VERSION))
        self.assertEqual(migrated[0:7], original)
        self.assertEqual(migrated[7:], ("self-reported", "self-reported", "self-reported"))
        record = self.read_projection()["records"][0]
        self.assertEqual(record["runner_identity_source"], "self-reported")
        self.assertEqual(record["model_identity_source"], "self-reported")
        self.assertEqual(record["configuration_identity_source"], "self-reported")

    def test_legacy_projection_rejects_v3_self_report_semantics(self):
        invocation_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        document = self.read_projection()
        record = document["records"][0]
        record["runner"] = "claude"
        record["model"] = "claude-sonnet-5"
        record["configuration"] = "unknown"
        record["identity_source"] = "self-reported"
        record["identity_warning"] = "self-incomplete"
        record["runner_identity_source"] = "self-reported"
        record["model_identity_source"] = "self-reported"
        record["configuration_identity_source"] = "unknown"
        self.assertEqual(run_performance.validate_projection(document), document)

        v3 = json.loads(json.dumps(document))
        v3["schema_version"] = run_performance.IDENTITY_PROJECTION_SCHEMA_VERSION
        for field in ("runner", "model", "configuration"):
            v3["records"][0].pop(f"{field}_identity_source")
        self.assertEqual(run_performance.validate_projection(v3), v3)
        v3["records"][0]["identity_warning"] = "runtime-self-unverified"
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "identity_warning"
        ):
            run_performance.validate_projection(v3)

        v3_row = {
            "invocation_id": str(uuid.uuid4()),
            "occurred_at_utc": "2026-08-11T19:00:01Z",
            "runner": "claude",
            "model": "claude-sonnet-5",
            "configuration": "unknown",
            "identity_source": "self-reported",
            "identity_warning": "runtime-self-unverified",
        }
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "identity_warning"
        ):
            run_performance._validate_resolved_identity_row(v3_row, "v3 identity")

        for version in (
            run_performance.LEGACY_PROJECTION_SCHEMA_VERSION,
            run_performance.PREVIOUS_PROJECTION_SCHEMA_VERSION,
        ):
            legacy = json.loads(json.dumps(document))
            legacy["schema_version"] = version
            legacy["records"][0].pop("identity_warning")
            for field in ("runner", "model", "configuration"):
                legacy["records"][0].pop(f"{field}_identity_source")
            with self.subTest(version=version), self.assertRaises(
                run_performance.PerformanceError
            ):
                run_performance.validate_projection(legacy)

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

    def test_estimate_schema_tampering_is_rejected(self):
        invocation_id = self.finish_lifecycle(run_start_pt=self.RUN_START_PT)
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        connection = sqlite3.connect(self.state)
        try:
            connection.execute("DROP TRIGGER performance_estimates_no_update")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaisesRegex(run_performance.PerformanceError, "append-only"):
            run_performance.validate_current_projection_read_only(
                self.state, self.projection
            )

    def test_internal_event_and_estimate_insert_are_one_transaction(self):
        first_id = self.finish_lifecycle()
        run_performance.record_internal(**self.internal_kwargs(first_id))
        second_id = self.finish_lifecycle(
            start="2026-08-11T20:00:00Z",
            finish="2026-08-11T20:10:00Z",
            run_start_pt="2026-08-11T13:00:30-07:00",
        )
        with mock.patch.object(
            run_performance, "ESTIMATE_CLOCK_SOURCE", "invalid-estimate-source"
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                run_performance.record_internal(
                    **self.internal_kwargs(
                        second_id,
                        strategy_start_utc=None,
                        strategy_end_utc=None,
                        now_utc="2026-08-11T20:11:00Z",
                    )
                )
        connection = sqlite3.connect(self.state)
        try:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM performance_events WHERE invocation_id = ?",
                (second_id,),
            ).fetchone()[0]
            estimate_count = connection.execute(
                "SELECT COUNT(*) FROM performance_estimates WHERE invocation_id = ?",
                (second_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(event_count, 0)
        self.assertEqual(estimate_count, 0)

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
        invocation_id = self.finish_lifecycle(run_start_pt=self.RUN_START_PT)
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
            before_estimates = connection.execute(
                "SELECT * FROM performance_estimates ORDER BY internal_sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(before), 1)
        self.assertEqual(len(before_estimates), 1)
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
            after_estimates = connection.execute(
                "SELECT * FROM performance_estimates ORDER BY internal_sequence"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(after, before)
        self.assertEqual(after_estimates, before_estimates)

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

    def test_projection_v1_is_readable_only_with_legacy_external_semantics(self):
        invocation_id = self.finish_lifecycle(run_start_pt=None)
        run_performance.record_internal(**self.internal_kwargs(invocation_id))
        run_performance.observe_task(**self.task_kwargs(invocation_id))
        document = self.read_projection()
        document["schema_version"] = (
            run_performance.LEGACY_PROJECTION_SCHEMA_VERSION
        )
        document["records"][0].pop("identity_warning")
        for field in ("runner", "model", "configuration"):
            document["records"][0].pop(f"{field}_identity_source")
        self.assertEqual(
            run_performance.validate_projection(document), document
        )
        with self.assertRaisesRegex(
            run_performance.PerformanceError, "unsupported value"
        ):
            run_performance.validate_projection(document, allow_legacy=False)

        document["records"][0]["task_clock_source"] = (
            run_performance.ESTIMATE_CLOCK_SOURCE
        )
        document["records"][0]["task_observed_at_utc"] = document[
            "records"
        ][0]["internal_recorded_at_utc"]
        document["records"][0]["outside_lifecycle_ms"] = None
        document["records"][0]["total_overhead_ms"] = None
        with self.assertRaisesRegex(
            run_performance.PerformanceError,
            "legacy projection cannot contain canonical",
        ):
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

    def test_cli_resolve_identity_emits_exact_receipt(self):
        invocation_id = self.start_lifecycle()
        resolved = subprocess.run(
            [
                sys.executable,
                os.path.join(ROOT, "run_performance.py"),
                "resolve-identity",
                "--invocation-id",
                invocation_id,
                "--self-identity",
                "Claude Desktop Code|Claude Sonnet 5|unknown",
                "--declared-identity",
                "absent",
                "--metadata-identity",
                "absent",
                "--state-file",
                self.state,
                "--projection-file",
                self.projection,
                "--lifecycle-state-file",
                self.lifecycle_state,
                "--lifecycle-projection-file",
                self.lifecycle_projection,
                "--now-utc",
                "2026-08-11T19:00:01Z",
            ],
            text=True,
            capture_output=True,
            check=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key not in ("CLAUDECODE", "CLAUDE_EFFORT")
            },
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        self.assertEqual(resolved.stderr, "")
        self.assertEqual(len(resolved.stdout.splitlines()), 1)
        payload = json.loads(resolved.stdout)
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "action",
                "ok",
                "invocation_id",
                "runner",
                "model",
                "configuration",
                "identity_source",
                "identity_warning",
                "runner_identity_source",
                "model_identity_source",
                "configuration_identity_source",
            },
        )
        self.assertEqual(payload["action"], "resolve-identity")
        self.assertEqual(payload["runner"], "claude")
        self.assertEqual(payload["model"], "claude-sonnet-5")
        self.assertEqual(payload["configuration"], "unknown")
        self.assertEqual(payload["identity_source"], "self-reported")
        self.assertEqual(payload["identity_warning"], "self-incomplete")


if __name__ == "__main__":
    unittest.main()
