import argparse
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import closing, redirect_stdout
from datetime import datetime
from urllib.parse import parse_qs, urlsplit


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import broker_snapshot
import daily_loss
import order_intents
import run_lifecycle
import run_lock
import validate_constants


PYTHON = sys.executable
LIFECYCLE_SCRIPT = os.path.join(ROOT, "run_lifecycle.py")
RUN_LOCK_SCRIPT = os.path.join(ROOT, "run_lock.py")


class LifecycleHardeningTests(unittest.TestCase):
    START_UTC = "2026-08-28T13:00:00Z"
    RUN_START_PT = "2026-08-28T06:00:01-07:00"
    AS_OF_UTC = "2026-08-28T20:00:00Z"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_file = os.path.join(self.temporary.name, "lifecycle.sqlite3")
        self.projection_file = os.path.join(
            self.temporary.name, "lifecycle.json"
        )
        self.lock_file = os.path.join(self.temporary.name, "lock.sqlite3")
        self.context_file = os.path.join(self.temporary.name, "context.json")
        self.intent_state_file = os.path.join(self.temporary.name, "intents.sqlite3")
        self.constants = validate_constants.validate_constants_file()

    def _prepare_intent(self, token, purpose="dip-buy"):
        intent = {
            "schema_version": 1, "account_name": "Agentic",
            "run_token": token, "run_start_utc": self.START_UTC,
            "rules_version": "abcdef1",
            "constants_sha256": self.constants.source_sha256,
            "purpose": purpose, "replaces_intent_id": None,
            "order": {
                "symbol": "TEST", "side": "buy" if purpose == "dip-buy" else "sell",
                "type": "market", "market_hours": "regular_hours",
                "time_in_force": "gfd",
                **({"dollar_amount": "100.00"} if purpose == "dip-buy" else {"quantity": "1"}),
            },
            "baseline": {
                "observed_at_utc": "2026-08-28T13:00:04Z",
                "position_quantity": "0" if purpose == "dip-buy" else "1",
                "symbol_order_ids": [],
            },
        }
        path = Path(self.temporary.name) / f"intent-{uuid.uuid4()}.json"
        path.write_text(json.dumps(intent), encoding="utf-8")
        return order_intents.prepare(
            self.intent_state_file, str(path), "2026-08-28T13:00:05Z"
        )["intent_id"]

    def _entry_authorizer(self, now="2026-08-28T13:00:07Z"):
        return lambda token: run_lifecycle.authorize_entry_intent(
            run_token=token, state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file, lock_file=self.lock_file,
            now_utc=now,
        )

    @staticmethod
    def _timestamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def _bound_scratch(self):
        temp_root = Path(tempfile.gettempdir()).resolve(strict=True)
        scratch = Path(
            tempfile.mkdtemp(prefix="rhmra-session-", dir=str(temp_root))
        ).resolve(strict=True)
        source_root = Path(
            tempfile.mkdtemp(prefix="rhmra-source-", dir=str(temp_root))
        ).resolve(strict=True)
        self.addCleanup(shutil.rmtree, scratch, True)
        self.addCleanup(shutil.rmtree, source_root, True)
        source_stat = os.lstat(source_root)
        broker_snapshot._preflight_directory(
            scratch,
            prepared_source_root=source_root,
            prepared_source_root_identity=(
                source_stat.st_dev,
                source_stat.st_ino,
            ),
        )
        canary = source_root / "get-accounts.json"
        canary.write_bytes(
            broker_snapshot._canonical_bytes(
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
                }
            )
        )
        receipt = broker_snapshot._bind_transport(
            argparse.Namespace(
                scratch=str(scratch),
                source_root=str(source_root),
                canary=str(canary),
                account_name="Agentic",
            )
        )
        self.assertTrue(receipt["ok"])
        return scratch

    def _stage_set(self, scratch, kind, documents, generation="A"):
        _scratch, marker = broker_snapshot.validate_scratch_directory(scratch)
        set_id = str(uuid.uuid4())
        outputs = []
        metadata_paths = []
        metadata_documents = []
        expected_cursor = "FIRST"
        for index, document in enumerate(documents, 1):
            output = scratch / f"{kind}-{generation.lower()}-{index}.json"
            raw = broker_snapshot._canonical_bytes(document)
            if kind in {"positions", "orders"}:
                request_cursor = expected_cursor
                next_url = document["data"]["next"]
                next_cursor = (
                    parse_qs(urlsplit(next_url).query)["cursor"][0]
                    if next_url
                    else None
                )
                expected_cursor = next_cursor or ""
            else:
                request_cursor = None
                next_cursor = None
            metadata = broker_snapshot._stage_metadata_document(
                scratch_id=marker["scratch_id"],
                generation=generation,
                kind=kind,
                filename=output.name,
                payload_sha256=broker_snapshot._sha256(raw),
                set_id=set_id,
                set_index=index,
                set_file_count=len(documents),
                set_complete=True,
                request_cursor=request_cursor,
                next_cursor=next_cursor,
            )
            outputs.append(str(output))
            metadata_paths.append(
                str(output) + broker_snapshot.STAGE_METADATA_SUFFIX
            )
            metadata_documents.append(metadata)
        prepared = broker_snapshot._prepare_atomic_files(
            [*outputs, *metadata_paths], [*documents, *metadata_documents]
        )
        broker_snapshot._commit_atomic_files(prepared)
        return outputs

    @staticmethod
    def _stop_order(index):
        return {
            "id": f"stop-{index}",
            "symbol": "XYZ",
            "side": "sell",
            "type": "stop_market",
            "trigger": "stop",
            "stop_price": "9",
            "created_at": "2026-08-28T14:59:00Z",
            "state": "filled",
            "cumulative_quantity": "1",
            "fees": "0",
            "executions": [
                {
                    "id": f"execution-{index}",
                    "price": "10",
                    "quantity": "1",
                    "timestamp": "2026-08-28T15:00:00Z",
                    "fees": "0",
                }
            ],
        }

    def _staged_inputs(self, scratch, *, stop_count=0, two_position_pages=False):
        portfolio_document = {"data": {"total_value": "1000"}}
        if two_position_pages:
            position_documents = [
                {
                    "data": {
                        "positions": [],
                        "next": (
                            "https://agent.robinhood.com/positions"
                            "?cursor=cursor-two"
                        ),
                    }
                },
                {"data": {"positions": [], "next": None}},
            ]
        else:
            position_documents = [
                {"data": {"positions": [], "next": None}}
            ]
        order_documents = [
            {
                "data": {
                    "orders": [
                        self._stop_order(index)
                        for index in range(stop_count)
                    ],
                    "next": None,
                }
            }
        ]
        quote_documents = []
        if stop_count:
            quote_documents = [
                {
                    "data": {
                        "results": [
                            {
                                "quote": {
                                    "symbol": "XYZ",
                                    "last_trade_price": "10",
                                    "venue_last_trade_time": (
                                        "2026-08-28T19:59:00Z"
                                    ),
                                    "last_non_reg_trade_price": None,
                                    "venue_last_non_reg_trade_time": None,
                                    "adjusted_previous_close": "10",
                                    "previous_close_date": "2026-08-27",
                                    "has_traded": True,
                                    "state": "active",
                                },
                                "close": {
                                    "symbol": "XYZ",
                                    "date": "2026-08-27",
                                    "price": "10",
                                    "interpolated": False,
                                    "source": "sip-list-exchange-close",
                                },
                            }
                        ]
                    }
                }
            ]
        paths = {
            "portfolio": self._stage_set(
                scratch, "portfolio", [portfolio_document]
            ),
            "positions": self._stage_set(
                scratch, "positions", position_documents
            ),
            "orders": self._stage_set(scratch, "orders", order_documents),
            "quotes": (
                self._stage_set(scratch, "quotes", quote_documents)
                if quote_documents
                else []
            ),
        }
        documents = {
            "portfolio": portfolio_document,
            "positions": position_documents,
            "orders": order_documents,
            "quotes": quote_documents,
        }
        return paths, documents

    def _start_first(self):
        started = run_lifecycle.start_invocation(
            invocation_id=str(uuid.uuid4()),
            state_file=self.state_file,
            projection_file=self.projection_file,
            lock_file=self.lock_file,
            now_utc=self.START_UTC,
        )
        invocation_id = started["invocation_id"]
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="preflight",
            run_start_pt=self.RUN_START_PT,
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:02Z",
        )
        acquired = run_lifecycle.acquire_and_bind_active_context(
            invocation_id=invocation_id,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:03Z",
        )
        self.assertTrue(acquired["ok"])
        token = acquired["run_lock_token"]
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="position-management",
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:04Z",
        )
        return invocation_id, token

    def _enter_second(self, scratch=None):
        invocation_id, token = self._start_first()
        scratch = scratch or self._bound_scratch()
        receipt = run_lifecycle.enter_second(
            invocation_id=invocation_id,
            run_token=token,
            scratch=str(scratch),
            expected_constants_sha256=self.constants.source_sha256,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:05Z",
        )
        self.assertTrue(receipt["ok"])
        return invocation_id, token, scratch, receipt

    def _run_daily_loss(self, invocation_id, token, scratch, *, stop_count=0,
                        two_position_pages=False, positions_override=None,
                        paths=None):
        if paths is None:
            paths, _documents = self._staged_inputs(
                scratch,
                stop_count=stop_count,
                two_position_pages=two_position_pages,
            )
        positions = positions_override or paths["positions"]
        output = scratch / "daily-loss-a.json"
        arguments = [
            "--portfolio", paths["portfolio"][0],
            "--positions", *positions,
            "--orders", *paths["orders"],
        ]
        if paths["quotes"]:
            arguments.extend(["--quotes", *paths["quotes"]])
        arguments.extend(
            [
                "--snapshot-generation", "A",
                "--trading-date", "2026-08-28",
                "--stop-date-pt", "2026-08-28",
                "--as-of-utc", self.AS_OF_UTC,
                "--json-out", str(output),
                "--failure-json",
                "--invocation-id", invocation_id,
                "--run-token", token,
                "--expected-constants-sha256", self.constants.source_sha256,
                "--lifecycle-state-file", self.state_file,
                "--lifecycle-projection-file", self.projection_file,
                "--lifecycle-context-file", self.context_file,
                "--lifecycle-lock-file", self.lock_file,
            ]
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            return_code = daily_loss.main(
                arguments, lifecycle_now_utc="2026-08-28T13:00:06Z"
            )
        document = json.loads(stdout.getvalue())
        return return_code, document, output, paths

    def _finish_release(self, invocation_id, token, classification,
                        reason_code=None):
        return run_lifecycle.release_and_finish_invocation(
            invocation_id=invocation_id,
            run_token=token,
            classification=classification,
            reason_code=reason_code,
            state_file=self.state_file,
            projection_file=self.projection_file,
            report_dir=self.temporary.name,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:07Z",
        )

    def test_enter_second_is_owner_fenced_bound_idempotent_and_token_private(self):
        invocation_id, token = self._start_first()
        scratch = self._bound_scratch()
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle.enter_second(
                invocation_id=invocation_id,
                run_token=str(uuid.uuid4()),
                scratch=str(scratch),
                expected_constants_sha256=self.constants.source_sha256,
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:05Z",
            )
        first = run_lifecycle.enter_second(
            invocation_id=invocation_id,
            run_token=token,
            scratch=str(scratch),
            expected_constants_sha256=self.constants.source_sha256,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:05Z",
        )
        retry = run_lifecycle.enter_second(
            invocation_id=invocation_id,
            run_token=token,
            scratch=str(scratch),
            expected_constants_sha256=self.constants.source_sha256,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        self.assertTrue(first["recorded"])
        self.assertFalse(retry["recorded"])
        self.assertEqual(first["scratch_id"], retry["scratch_id"])
        self.assertEqual(first["stop_count_halt"], 3)
        self.assertNotIn(token, json.dumps(first))
        with closing(sqlite3.connect(self.state_file)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_second_contexts "
                "WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_cross_invocation_scratch_reuse_is_rejected_atomically(self):
        first_id, first_token, scratch, _receipt = self._enter_second()
        run_lifecycle.complete_second(
            invocation_id=first_id,
            run_token=first_token,
            outcome="coordination-terminal",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        self._finish_release(
            first_id, first_token, "coordination-halt", "coordination-state"
        )
        second_id, second_token = self._start_first()
        with self.assertRaises(sqlite3.IntegrityError):
            run_lifecycle.enter_second(
                invocation_id=second_id,
                run_token=second_token,
                scratch=str(scratch),
                expected_constants_sha256=self.constants.source_sha256,
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:05Z",
            )
        with closing(sqlite3.connect(self.state_file)) as connection:
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_checkpoints "
                "WHERE invocation_id = ?",
                (second_id,),
            ).fetchone()[0]
        self.assertEqual(checkpoints, 0)

    def test_lifecycle_daily_loss_wrapper_is_durable_private_and_idempotent(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, output, _paths = self._run_daily_loss(
            invocation_id, token, scratch, stop_count=0
        )
        self.assertEqual(code, 0, document)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), document)
        self.assertEqual(
            set(document),
            {
                "schema_version", "action", "ok", "mode", "generation",
                "invocation_id", "constants_sha256",
                "daily_loss_halt_pct", "stop_count_halt",
                "daily_loss_tripped", "stop_count_tripped",
                "entry_guard_outcome", "result",
            },
        )
        self.assertEqual(document["entry_guard_outcome"], "clear")
        self.assertNotIn(token, json.dumps(document))
        with closing(sqlite3.connect(self.state_file)) as connection:
            first = connection.execute(
                "SELECT sequence, result_sha256, sources_sha256 "
                "FROM lifecycle_second_evidence WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        code, retry, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch, stop_count=0, paths=_paths
        )
        self.assertEqual(code, 0, retry)
        self.assertEqual(retry, document)
        with closing(sqlite3.connect(self.state_file)) as connection:
            second = connection.execute(
                "SELECT sequence, result_sha256, sources_sha256 "
                "FROM lifecycle_second_evidence WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
        self.assertEqual(first, second)
        entry = run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="entry-scan",
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:07Z",
        )
        self.assertEqual(entry["phase"], "entry-scan")

    def test_stop_count_combined_guard_boundaries(self):
        threshold = int(self.constants.values["STOP_COUNT_HALT"])
        for stop_count, expected in (
            (threshold - 1, "clear"),
            (threshold, "tripped"),
            (threshold + 1, "tripped"),
        ):
            with self.subTest(stop_count=stop_count):
                invocation_id, token, scratch, _receipt = self._enter_second()
                code, document, _output, _paths = self._run_daily_loss(
                    invocation_id, token, scratch, stop_count=stop_count
                )
                self.assertEqual(code, 0, document)
                self.assertEqual(document["entry_guard_outcome"], expected)
                self.assertEqual(
                    document["stop_count_tripped"], stop_count >= threshold
                )
                if expected == "clear":
                    self._finish_release(invocation_id, token, "completed")
                else:
                    with self.assertRaises(run_lifecycle.LifecycleConflict):
                        run_lifecycle.record_event(
                            invocation_id=invocation_id,
                            phase="entry-scan",
                            state_file=self.state_file,
                            projection_file=self.projection_file,
                            now_utc="2026-08-28T13:00:07Z",
                        )
                    self._finish_release(
                        invocation_id,
                        token,
                        "risk-halt",
                        "stop-count-tripped",
                    )

    def test_public_completion_cannot_mint_clear_or_unlock_entry(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        for outcome in ("clear", "tripped"):
            with self.subTest(outcome=outcome), self.assertRaises(
                run_lifecycle.LifecycleError
            ):
                run_lifecycle.complete_second(
                    invocation_id=invocation_id,
                    run_token=token,
                    outcome=outcome,
                    state_file=self.state_file,
                    projection_file=self.projection_file,
                    context_file=self.context_file,
                    lock_file=self.lock_file,
                    now_utc="2026-08-28T13:00:06Z",
                )
        run_lifecycle.complete_second(
            invocation_id=invocation_id,
            run_token=token,
            outcome="snapshot-terminal",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase="entry-scan",
                state_file=self.state_file,
                projection_file=self.projection_file,
                now_utc="2026-08-28T13:00:07Z",
            )
        report = run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="report",
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:07Z",
        )
        self.assertEqual(report["phase"], "report")

    def test_entry_phases_cannot_skip_enter_second(self):
        invocation_id, _token = self._start_first()
        for phase in ("entry-scan", "entry-evaluation", "order-placement"):
            with self.subTest(phase=phase), self.assertRaisesRegex(
                run_lifecycle.LifecycleConflict,
                "requires exactly the deterministic daily-loss-clear",
            ):
                run_lifecycle.record_event(
                    invocation_id=invocation_id,
                    phase=phase,
                    state_file=self.state_file,
                    projection_file=self.projection_file,
                    now_utc="2026-08-28T13:00:05Z",
                )

    def test_generic_event_cannot_mint_daily_loss_phase(self):
        invocation_id, _token = self._start_first()
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict,
            "only by enter-second",
        ):
            run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase="daily-loss",
                state_file=self.state_file,
                projection_file=self.projection_file,
                now_utc="2026-08-28T13:00:05Z",
            )

    def test_nonclear_second_terminal_blocks_every_entry_phase(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        run_lifecycle.complete_second(
            invocation_id=invocation_id,
            run_token=token,
            outcome="coordination-terminal",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        for phase in ("entry-scan", "entry-evaluation", "order-placement"):
            with self.subTest(phase=phase), self.assertRaisesRegex(
                run_lifecycle.LifecycleConflict,
                "requires exactly the deterministic daily-loss-clear",
            ):
                run_lifecycle.record_event(
                    invocation_id=invocation_id,
                    phase=phase,
                    state_file=self.state_file,
                    projection_file=self.projection_file,
                    now_utc="2026-08-28T13:00:07Z",
                )

    def test_clear_second_terminal_allows_every_entry_phase(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        for offset, phase in enumerate(
            ("entry-scan", "entry-evaluation", "order-placement"), 7
        ):
            receipt = run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase=phase,
                state_file=self.state_file,
                projection_file=self.projection_file,
                now_utc=f"2026-08-28T13:00:{offset:02d}Z",
            )
            self.assertEqual(receipt["phase"], phase)

    def test_dip_buy_begin_requires_real_clear_authorization(self):
        invocation_id, token = self._start_first()
        intent_id = self._prepare_intent(token)
        with self.assertRaisesRegex(
            order_intents.OrderIntentError, "daily-loss-clear"
        ):
            order_intents.begin_or_retry(
                self.intent_state_file, intent_id, token,
                "2026-08-28T13:00:06Z", retrying=False,
                entry_authorizer=self._entry_authorizer("2026-08-28T13:00:06Z"),
            )
        with closing(sqlite3.connect(self.intent_state_file)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, submit_attempts FROM intents WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone(),
                ("prepared", 0),
            )

        scratch = self._bound_scratch()
        run_lifecycle.enter_second(
            invocation_id=invocation_id, run_token=token, scratch=str(scratch),
            expected_constants_sha256=self.constants.source_sha256,
            state_file=self.state_file, projection_file=self.projection_file,
            context_file=self.context_file, lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        receipt = order_intents.begin_or_retry(
            self.intent_state_file, intent_id, token,
            "2026-08-28T13:00:08Z", retrying=False,
            entry_authorizer=self._entry_authorizer("2026-08-28T13:00:08Z"),
        )
        self.assertEqual(receipt["status"], "submitting")
        self.assertNotIn(token, json.dumps(receipt))

    def test_dip_buy_rejects_terminal_nonclear_and_invalid_owner(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        run_lifecycle.complete_second(
            invocation_id=invocation_id, run_token=token,
            outcome="coordination-terminal", state_file=self.state_file,
            projection_file=self.projection_file, context_file=self.context_file,
            lock_file=self.lock_file, now_utc="2026-08-28T13:00:06Z",
        )
        intent_id = self._prepare_intent(token)
        with self.assertRaises(order_intents.OrderIntentError):
            order_intents.begin_or_retry(
                self.intent_state_file, intent_id, token,
                "2026-08-28T13:00:07Z", retrying=False,
                entry_authorizer=self._entry_authorizer(),
            )
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle.authorize_entry_intent(
                run_token=str(uuid.uuid4()), state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file, lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:07Z",
            )

    def test_non_entry_sell_begin_does_not_require_second(self):
        _invocation_id, token = self._start_first()
        intent_id = self._prepare_intent(token, purpose="profit-take")
        calls = []
        receipt = order_intents.begin_or_retry(
            self.intent_state_file, intent_id, token,
            "2026-08-28T13:00:06Z", retrying=False,
            entry_authorizer=lambda value: calls.append(value),
        )
        self.assertEqual(receipt["status"], "submitting")
        self.assertEqual(calls, [])

    def test_dip_buy_retry_is_fenced_before_state_mutation(self):
        invocation_id, token = self._start_first()
        intent_id = self._prepare_intent(token)
        injected = lambda _token: {
            "schema_version": 1, "action": "authorize-entry-intent",
            "ok": True, "invocation_id": invocation_id,
            "phase": "entry-scan", "entry_guard_outcome": "clear",
            "lease_renewed": True,
        }
        order_intents.begin_or_retry(
            self.intent_state_file, intent_id, token,
            "2026-08-28T13:00:06Z", retrying=False,
            entry_authorizer=injected,
        )
        order_intents.mark_unknown(
            self.intent_state_file, intent_id, "transport_error", None,
            "2026-08-28T13:00:07Z",
        )
        with closing(sqlite3.connect(self.intent_state_file)) as connection:
            connection.execute(
                "UPDATE intents SET last_observed_at = ? WHERE intent_id = ?",
                ("2026-08-28T13:00:08Z", intent_id),
            )
            connection.commit()
        with self.assertRaisesRegex(
            order_intents.OrderIntentError, "daily-loss-clear"
        ):
            order_intents.begin_or_retry(
                self.intent_state_file, intent_id, token,
                "2026-08-28T13:00:09Z", retrying=True,
                entry_authorizer=self._entry_authorizer("2026-08-28T13:00:09Z"),
            )
        with closing(sqlite3.connect(self.intent_state_file)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status, submit_attempts FROM intents WHERE intent_id = ?",
                    (intent_id,),
                ).fetchone(),
                ("unknown", 1),
            )

    def test_entry_authorizer_rejects_expired_or_replaced_owner(self):
        _invocation_id, token = self._start_first()
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "expired"
        ):
            run_lifecycle.authorize_entry_intent(
                run_token=token, state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file, lock_file=self.lock_file,
                now_utc="2026-08-28T14:00:00Z",
            )

        second = run_lifecycle.start_invocation(
            invocation_id=str(uuid.uuid4()), state_file=self.state_file,
            projection_file=self.projection_file, lock_file=self.lock_file,
            now_utc="2026-08-28T14:00:01Z",
        )
        run_lifecycle.record_event(
            invocation_id=second["invocation_id"], phase="preflight",
            run_start_pt="2026-08-28T07:00:01-07:00",
            state_file=self.state_file, projection_file=self.projection_file,
            now_utc="2026-08-28T14:00:02Z",
        )
        run_lifecycle.acquire_and_bind_active_context(
            invocation_id=second["invocation_id"], state_file=self.state_file,
            projection_file=self.projection_file, context_file=self.context_file,
            lock_file=self.lock_file, now_utc="2026-08-28T14:00:03Z",
        )
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle.authorize_entry_intent(
                run_token=token, state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file, lock_file=self.lock_file,
                now_utc="2026-08-28T14:00:04Z",
            )

    def test_reversed_sealed_pages_cannot_record_clear(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        paths, documents = self._staged_inputs(
            scratch, two_position_pages=True
        )
        result = daily_loss.calculate_daily_loss(
            documents["portfolio"],
            documents["positions"],
            documents["orders"],
            documents["quotes"],
            "2026-08-28",
            self.AS_OF_UTC,
            self.constants.raw_values["DAILY_LOSS_HALT_PCT"],
            "2026-08-28",
        )
        wrapper = {
            "schema_version": 1,
            "action": "daily-loss",
            "ok": True,
            "mode": "calculation",
            "generation": "A",
            "invocation_id": invocation_id,
            "constants_sha256": self.constants.source_sha256,
            "daily_loss_halt_pct": self.constants.raw_values[
                "DAILY_LOSS_HALT_PCT"
            ],
            "stop_count_halt": int(self.constants.values["STOP_COUNT_HALT"]),
            "daily_loss_tripped": False,
            "stop_count_tripped": False,
            "entry_guard_outcome": "clear",
            "result": result,
        }
        output = scratch / "daily-loss-a.json"
        daily_loss._write_json_no_clobber_or_match(str(output), wrapper)
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleError, "sealed set order"
        ):
            run_lifecycle.complete_second_result(
                invocation_id=invocation_id,
                run_token=token,
                result_file=str(output),
                generation="A",
                portfolio=paths["portfolio"][0],
                positions=list(reversed(paths["positions"])),
                orders=paths["orders"],
                quotes=paths["quotes"],
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:06Z",
            )

    def test_stale_constants_hash_and_outside_target_fail_before_write(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        paths, _documents = self._staged_inputs(scratch)
        outside = Path(self.temporary.name) / "daily-loss-a.json"
        arguments = [
            "--portfolio", paths["portfolio"][0],
            "--positions", *paths["positions"],
            "--orders", *paths["orders"],
            "--snapshot-generation", "A",
            "--trading-date", "2026-08-28",
            "--stop-date-pt", "2026-08-28",
            "--as-of-utc", self.AS_OF_UTC,
            "--json-out", str(outside),
            "--failure-json",
            "--invocation-id", invocation_id,
            "--run-token", token,
            "--expected-constants-sha256", "0" * 64,
            "--lifecycle-state-file", self.state_file,
            "--lifecycle-projection-file", self.projection_file,
            "--lifecycle-context-file", self.context_file,
            "--lifecycle-lock-file", self.lock_file,
        ]
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = daily_loss.main(
                arguments, lifecycle_now_utc="2026-08-28T13:00:06Z"
            )
        self.assertEqual(code, 2)
        self.assertFalse(outside.exists())
        self.assertEqual(
            json.loads(stdout.getvalue())["error"]["code"],
            "daily_loss_binding_invalid",
        )
        arguments[arguments.index("0" * 64)] = self.constants.source_sha256
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = daily_loss.main(
                arguments, lifecycle_now_utc="2026-08-28T13:00:06Z"
            )
        self.assertEqual(code, 2)
        self.assertFalse(outside.exists())
        self.assertIn("outside the bound SECOND context", stdout.getvalue())

    def test_phase_allowlist_rejects_regression_after_terminal(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        run_lifecycle.complete_second(
            invocation_id=invocation_id,
            run_token=token,
            outcome="coordination-terminal",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        for phase in (
            "scheduled", "coordination", "preflight", "initial-snapshot",
            "daily-loss", "position-management", "entry-scan", "finished",
        ):
            with self.subTest(phase=phase), self.assertRaises(
                run_lifecycle.LifecycleConflict
            ):
                run_lifecycle.record_event(
                    invocation_id=invocation_id,
                    phase=phase,
                    state_file=self.state_file,
                    projection_file=self.projection_file,
                    now_utc="2026-08-28T13:00:07Z",
                )
        for phase in ("final-refresh", "report", "status-publish"):
            receipt = run_lifecycle.record_event(
                invocation_id=invocation_id,
                phase=phase,
                state_file=self.state_file,
                projection_file=self.projection_file,
                now_utc="2026-08-28T13:00:07Z",
            )
            self.assertEqual(receipt["phase"], phase)

    def test_release_finish_reports_post_release_failure_truthfully(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        real_append = run_lifecycle._append_event
        self.addCleanup(setattr, run_lifecycle, "_append_event", real_append)

        def fail_append(**_kwargs):
            raise sqlite3.OperationalError("injected pre-commit failure")

        run_lifecycle._append_event = fail_append
        with self.assertRaises(run_lifecycle.ReleaseFinishError) as captured:
            self._finish_release(invocation_id, token, "completed")
        self.assertTrue(captured.exception.recorded is False)
        self.assertNotIn(token, str(captured.exception))
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:07Z"
            )

    def test_release_finish_prevalidation_failure_retains_lease(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            self._finish_release(invocation_id, token, "completed")
        self.assertEqual(
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:07Z"
            ),
            token,
        )

    def test_raw_finish_cannot_bypass_owned_release_finish(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "must use release-finish"
        ):
            run_lifecycle.finish_invocation(
                invocation_id=invocation_id,
                classification="completed",
                state_file=self.state_file,
                projection_file=self.projection_file,
                report_dir=self.temporary.name,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:07Z",
            )
        self.assertEqual(
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:07Z"
            ),
            token,
        )

    def test_raw_finish_allows_only_proven_lease_loss_after_context(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        run_lifecycle.complete_second(
            invocation_id=invocation_id,
            run_token=token,
            outcome="coordination-terminal",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:06Z",
        )
        release = run_lock.release(
            token,
            lock_file=self.lock_file,
            now=self._timestamp("2026-08-28T13:00:06Z"),
        )
        self.assertTrue(release["ok"])
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "only as lease-lost"
        ):
            run_lifecycle.finish_invocation(
                invocation_id=invocation_id,
                classification="coordination-halt",
                reason_code="coordination-state",
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:07Z",
            )
        receipt = run_lifecycle.finish_invocation(
            invocation_id=invocation_id,
            classification="lease-lost",
            reason_code="lease-ownership-lost",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:07Z",
        )
        self.assertEqual(receipt["classification"], "lease-lost")

    def test_overwritten_shared_context_cannot_unbind_older_invocation(self):
        first_id, first_token = self._start_first()
        second = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:21:00Z",
        )
        second_id = second["invocation_id"]
        run_lifecycle.record_event(
            invocation_id=second_id,
            phase="preflight",
            run_start_pt="2026-08-28T06:21:01-07:00",
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:21:02Z",
        )
        acquired = run_lifecycle.acquire_and_bind_active_context(
            invocation_id=second_id,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:21:03Z",
        )
        self.assertTrue(acquired["ok"])
        second_token = acquired["run_lock_token"]
        self.assertNotEqual(first_token, second_token)
        self.assertEqual(
            json.loads(Path(self.context_file).read_text(encoding="utf-8"))[
                "invocation_id"
            ],
            second_id,
        )
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "only as lease-lost"
        ):
            run_lifecycle.finish_invocation(
                invocation_id=first_id,
                classification="completed",
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:21:04Z",
            )
        lost = run_lifecycle.finish_invocation(
            invocation_id=first_id,
            classification="lease-lost",
            reason_code="lease-ownership-lost",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:21:04Z",
        )
        self.assertEqual(lost["classification"], "lease-lost")
        self.assertEqual(
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:21:04Z"
            ),
            second_token,
        )

    def test_context_write_failure_still_leaves_durable_owner_fence(self):
        started = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            lock_file=self.lock_file,
            now_utc=self.START_UTC,
        )
        invocation_id = started["invocation_id"]
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="preflight",
            run_start_pt=self.RUN_START_PT,
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:02Z",
        )
        acquisition = run_lock.acquire(
            lock_file=self.lock_file,
            lease_seconds=run_lock.DEFAULT_LEASE_SECONDS,
            now=self._timestamp("2026-08-28T13:00:03Z"),
        )
        token = acquisition["token"]
        real_write = run_lifecycle._atomic_write_context
        self.addCleanup(
            setattr, run_lifecycle, "_atomic_write_context", real_write
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("injected context publication failure")

        run_lifecycle._atomic_write_context = fail_write
        with self.assertRaisesRegex(OSError, "context publication failure"):
            run_lifecycle.bind_active_context(
                invocation_id=invocation_id,
                run_token=token,
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:03Z",
            )
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "must use release-finish"
        ):
            run_lifecycle.finish_invocation(
                invocation_id=invocation_id,
                classification="completed",
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:04Z",
            )
        self.assertEqual(
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:04Z"
            ),
            token,
        )
        run_lock.release(
            token,
            lock_file=self.lock_file,
            now=self._timestamp("2026-08-28T13:00:04Z"),
        )
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleConflict, "only as lease-lost"
        ):
            run_lifecycle.finish_invocation(
                invocation_id=invocation_id,
                classification="coordination-halt",
                reason_code="coordination-state",
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:05Z",
            )

    def test_combined_bind_failure_compensation_allows_exact_coordination_halt(self):
        started = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            lock_file=self.lock_file,
            now_utc=self.START_UTC,
        )
        invocation_id = started["invocation_id"]
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="preflight",
            run_start_pt=self.RUN_START_PT,
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:02Z",
        )
        real_write = run_lifecycle._atomic_write_context
        self.addCleanup(
            setattr, run_lifecycle, "_atomic_write_context", real_write
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("injected combined context failure")

        run_lifecycle._atomic_write_context = fail_write
        receipt = run_lifecycle.acquire_and_bind_active_context(
            invocation_id=invocation_id,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:03Z",
        )
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["reason"], "bind_context_failed")
        self.assertTrue(receipt["lease_released"])
        self.assertTrue(receipt["compensation_recorded"])
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle.finish_invocation(
                invocation_id=invocation_id,
                classification="completed",
                state_file=self.state_file,
                projection_file=self.projection_file,
                context_file=self.context_file,
                lock_file=self.lock_file,
                now_utc="2026-08-28T13:00:04Z",
            )
        finished = run_lifecycle.finish_invocation(
            invocation_id=invocation_id,
            classification="coordination-halt",
            reason_code="coordination-state",
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:04Z",
        )
        self.assertEqual(finished["classification"], "coordination-halt")

    def test_malformed_release_success_cannot_authorize_bind_compensation(self):
        started = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            lock_file=self.lock_file,
            now_utc=self.START_UTC,
        )
        invocation_id = started["invocation_id"]
        run_lifecycle.record_event(
            invocation_id=invocation_id,
            phase="preflight",
            run_start_pt=self.RUN_START_PT,
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:02Z",
        )
        real_write = run_lifecycle._atomic_write_context
        real_release = run_lifecycle.run_lock_module.release
        self.addCleanup(
            setattr, run_lifecycle, "_atomic_write_context", real_write
        )
        self.addCleanup(
            setattr, run_lifecycle.run_lock_module, "release", real_release
        )

        def fail_write(*_args, **_kwargs):
            raise OSError("injected combined context failure")

        def malformed_release(token, **kwargs):
            receipt = real_release(token, **kwargs)
            receipt["token"] = str(uuid.uuid4())
            return receipt

        run_lifecycle._atomic_write_context = fail_write
        run_lifecycle.run_lock_module.release = malformed_release
        receipt = run_lifecycle.acquire_and_bind_active_context(
            invocation_id=invocation_id,
            state_file=self.state_file,
            projection_file=self.projection_file,
            context_file=self.context_file,
            lock_file=self.lock_file,
            now_utc="2026-08-28T13:00:03Z",
        )
        self.assertFalse(receipt["ok"])
        self.assertEqual(
            receipt["reason"], "bind_context_failed_release_unconfirmed"
        )
        self.assertFalse(receipt["lease_released"])
        self.assertFalse(receipt["compensation_recorded"])
        with closing(sqlite3.connect(self.state_file)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_lease_compensations "
                "WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_release_finish_release_failure_retains_lease(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        real_release = run_lifecycle.run_lock_module.release
        self.addCleanup(
            setattr, run_lifecycle.run_lock_module, "release", real_release
        )

        def fail_release(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected release failure")

        run_lifecycle.run_lock_module.release = fail_release
        with self.assertRaisesRegex(
            run_lifecycle.LifecycleError, "owner-fenced lease release failed"
        ):
            self._finish_release(invocation_id, token, "completed")
        self.assertEqual(
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:07Z"
            ),
            token,
        )

    def test_release_finish_projection_failure_reports_recorded(self):
        invocation_id, token, scratch, _receipt = self._enter_second()
        code, document, _output, _paths = self._run_daily_loss(
            invocation_id, token, scratch
        )
        self.assertEqual(code, 0, document)
        real_publish = run_lifecycle._publish_after_append
        self.addCleanup(setattr, run_lifecycle, "_publish_after_append", real_publish)

        def fail_publish(*_args, **_kwargs):
            raise sqlite3.OperationalError("injected projection failure")

        run_lifecycle._publish_after_append = fail_publish
        with self.assertRaises(run_lifecycle.ReleaseFinishError) as captured:
            self._finish_release(invocation_id, token, "completed")
        self.assertTrue(captured.exception.recorded)
        self.assertNotIn(token, str(captured.exception))
        with self.assertRaises(run_lifecycle.LifecycleConflict):
            run_lifecycle._active_lease_token(
                self.lock_file, "2026-08-28T13:00:07Z"
            )
        with closing(sqlite3.connect(self.state_file)) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM lifecycle_events "
                "WHERE invocation_id = ? AND event_type = 'finish'",
                (invocation_id,),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_start_reconciles_only_old_unleased_and_infers_sibling_lock(self):
        original_default_lock = run_lifecycle.DEFAULT_LOCK_FILE
        sentinel_default_lock = os.path.join(
            self.temporary.name, "must-not-use-default-lock.sqlite3"
        )
        Path(sentinel_default_lock).write_bytes(b"untouched sentinel")
        run_lifecycle.DEFAULT_LOCK_FILE = sentinel_default_lock
        self.addCleanup(
            setattr,
            run_lifecycle,
            "DEFAULT_LOCK_FILE",
            original_default_lock,
        )
        first = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T13:00:00Z",
        )
        inferred = os.path.join(
            self.temporary.name, "rhmra-run-lock.sqlite3"
        )
        second = run_lifecycle.start_invocation(
            state_file=self.state_file,
            projection_file=self.projection_file,
            now_utc="2026-08-28T14:00:00Z",
        )
        self.assertEqual(second["reconciled_abandoned_count"], 1)
        self.assertTrue(os.path.exists(inferred))
        self.assertEqual(
            Path(sentinel_default_lock).read_bytes(), b"untouched sentinel"
        )
        projection = run_lifecycle.validate_current_projection(
            self.state_file, self.projection_file
        )
        old = next(
            row for row in projection["records"]
            if row["invocation_id"] == first["invocation_id"]
        )
        self.assertEqual(old["classification"], "coordination-halt")
        self.assertIsNone(old["report_file"])
        self.assertIsNone(old["status_file"])

    def test_every_lifecycle_cli_action_rejects_now_override(self):
        actions = (
            "start", "event", "finish", "release-finish", "status",
            "enter-second", "complete-second", "reconcile-abandoned",
            "acquire-bind-context", "bind-context", "recover-context",
            "export", "validate",
        )
        for action in actions:
            proc = subprocess.run(
                [
                    PYTHON,
                    LIFECYCLE_SCRIPT,
                    action,
                    "--state-file", self.state_file,
                    "--projection-file", self.projection_file,
                    "--now-utc", "2026-08-28T13:00:00Z",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 1, proc.stdout)
            receipt = json.loads(proc.stdout)
            self.assertEqual(receipt["reason"], "lifecycle_state_error")
            self.assertIn("test-only through the imported API", receipt["detail"])

    def test_daily_loss_cli_rejects_lifecycle_now_override(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = daily_loss.main(
                [
                    "--portfolio", "missing-portfolio.json",
                    "--positions", "missing-positions.json",
                    "--orders", "missing-orders.json",
                    "--trading-date", "2026-08-28",
                    "--stop-date-pt", "2026-08-28",
                    "--as-of-utc", self.AS_OF_UTC,
                    "--snapshot-generation", "A",
                    "--json-out", "daily-loss-a.json",
                    "--failure-json",
                    "--lifecycle-now-utc", "2099-01-01T00:00:00Z",
                ]
            )
        self.assertEqual(code, 2)
        receipt = json.loads(stdout.getvalue())
        self.assertEqual(receipt["error"]["code"], "daily_loss_binding_invalid")
        self.assertIn("test-only through the imported API", receipt["error"]["message"])

    def test_run_lock_cli_cannot_fast_forward_a_live_lease(self):
        first = run_lock.acquire(
            lock_file=self.lock_file,
            lease_seconds=60,
            now=self._timestamp("2026-08-28T13:00:00Z"),
        )
        token = first["token"]
        proc = subprocess.run(
            [
                PYTHON, RUN_LOCK_SCRIPT, "acquire",
                "--lock-file", self.lock_file,
                "--lease-seconds", "60",
                "--now-utc", "2099-01-01T00:00:00Z",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1, proc.stdout)
        receipt = json.loads(proc.stdout)
        self.assertEqual(receipt["reason"], "coordination_state_error")
        self.assertIn("test-only through the imported API", receipt["detail"])
        self.assertNotIn(token, proc.stdout)
        with closing(sqlite3.connect(self.lock_file)) as connection:
            owner = connection.execute(
                "SELECT token FROM run_lease WHERE singleton = 1"
            ).fetchone()[0]
        self.assertEqual(owner, token)

    def test_second_tables_are_append_only(self):
        invocation_id, token, _scratch, _receipt = self._enter_second()
        with closing(sqlite3.connect(self.state_file)) as connection:
            for table in (
                "lifecycle_lease_bindings", "lifecycle_checkpoints",
                "lifecycle_second_contexts",
            ):
                with self.subTest(table=table), self.assertRaises(
                    sqlite3.IntegrityError
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE invocation_id = ?",
                        (invocation_id,),
                    )
                connection.rollback()
            connection.execute(
                "INSERT INTO lifecycle_lease_compensations "
                "(invocation_id, lease_token_sha256, disposition, "
                "occurred_at_utc) VALUES (?, ?, 'bind-failure-released', ?)",
                (
                    invocation_id,
                    run_lifecycle._token_digest(token),
                    "2026-08-28T13:00:06Z",
                ),
            )
            connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM lifecycle_lease_compensations "
                    "WHERE invocation_id = ?",
                    (invocation_id,),
                )
            connection.rollback()


if __name__ == "__main__":
    unittest.main()
