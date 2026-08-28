#!/usr/bin/env python3
"""Cross-platform fenced lease for one Robinhood routine at a time.

The routine is scheduled every 30 minutes, but an earlier agent run can still
be active when the next one starts. A plain "lock file exists" check is not
enough: creation and stale-lock replacement must be atomic, and a stalled old
owner must not regain permission after a newer run takes over.

This script stores one lease row in a local SQLite database. SQLite's
``BEGIN IMMEDIATE`` serializes acquisition and renewal across processes on
Windows, macOS, and Linux. Every successful acquisition returns a random
fencing token. Only that token can renew or release the lease.

The default lease is deliberately shorter than the 30-minute schedule. A live
run renews at phase boundaries and immediately before broker mutations. If a
run crashes, the next scheduled run can recover; if an old run resumes after
takeover, its stale token is rejected before it can cancel or place an order.

Commands (all emit one JSON object):

  python3 run_lock.py acquire
  python3 run_lock.py renew --token <token>
  python3 run_lock.py release --token <token>

``--lock-file`` and ``--lease-seconds`` exist for tests and diagnostics.  A
clock override is available only through the imported Python API; the CLI
rejects ``--now-utc`` so an unattended caller cannot fast-forward through a
live lease.  The trading routine uses the checked-in defaults.

Exit codes:
  0  action succeeded
  1  lock database or argument/state validation error
  2  acquisition blocked by an unexpired owner
  3  renewal/release rejected because ownership was lost

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
``python3 tests/test_scripts.py`` (Windows: ``py -3 tests\\test_scripts.py``)
and require all tests to pass before committing.
"""

import argparse
import json
import math
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone


SCHEMA_VERSION = 1
DEFAULT_LEASE_SECONDS = 20 * 60
DEFAULT_LOCK_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "run-reports",
    "rhmra-run-lock.sqlite3",
)


class LockStateError(ValueError):
    """The persistent lease row is malformed or internally inconsistent."""


def positive_int(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def parse_now(value):
    if value is not None:
        raise ValueError(
            "--now-utc is test-only through the imported API and is not "
            "valid on the CLI"
        )
    return datetime.now(timezone.utc).timestamp()


def iso_utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def validate_lease_seconds(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("lease_seconds must be a positive integer")


def validate_token(token):
    if not isinstance(token, str) or not token.strip():
        raise ValueError("token must be a non-empty string")


def connect(lock_file):
    lock_file = os.path.abspath(lock_file)
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)
    connection = sqlite3.connect(lock_file, timeout=5, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS run_lease (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                token TEXT NOT NULL,
                acquired_at REAL NOT NULL,
                renewed_at REAL NOT NULL,
                expires_at REAL NOT NULL
            )
            """
        )
    except Exception:
        connection.close()
        raise
    return connection


def validate_row(row):
    if row is None or len(row) != 4:
        raise LockStateError("lease row has the wrong shape")
    token, acquired_at, renewed_at, expires_at = row
    validate_token(token)
    timestamps = (acquired_at, renewed_at, expires_at)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in timestamps
    ):
        raise LockStateError("lease timestamps must be finite numbers")
    if not acquired_at <= renewed_at <= expires_at:
        raise LockStateError("lease timestamps are inconsistent")
    return token, float(acquired_at), float(renewed_at), float(expires_at)


def holder_json(row):
    _, acquired_at, renewed_at, expires_at = validate_row(row)
    return {
        "acquired_at": iso_utc(acquired_at),
        "renewed_at": iso_utc(renewed_at),
        "expires_at": iso_utc(expires_at),
    }


def acquire(lock_file=DEFAULT_LOCK_FILE, lease_seconds=DEFAULT_LEASE_SECONDS,
            now=None, token=None):
    validate_lease_seconds(lease_seconds)
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError("now must be finite")
    token = str(uuid.uuid4()) if token is None else token
    validate_token(token)

    connection = connect(lock_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT token, acquired_at, renewed_at, expires_at "
            "FROM run_lease WHERE singleton = 1"
        ).fetchone()
        recovered = False
        if row is not None:
            _, _, _, expires_at = validate_row(row)
            if expires_at > now:
                connection.commit()
                return {
                    "schema_version": SCHEMA_VERSION,
                    "action": "acquire",
                    "ok": False,
                    "reason": "active_run",
                    "holder": holder_json(row),
                }
            recovered = True

        expires_at = now + lease_seconds
        connection.execute(
            """
            INSERT INTO run_lease
                (singleton, token, acquired_at, renewed_at, expires_at)
            VALUES (1, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                token = excluded.token,
                acquired_at = excluded.acquired_at,
                renewed_at = excluded.renewed_at,
                expires_at = excluded.expires_at
            """,
            (token, now, now, expires_at),
        )
        connection.commit()
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "acquire",
            "ok": True,
            "token": token,
            "acquired_at": iso_utc(now),
            "expires_at": iso_utc(expires_at),
            "lease_seconds": lease_seconds,
            "recovered_expired_lease": recovered,
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def renew(token, lock_file=DEFAULT_LOCK_FILE,
          lease_seconds=DEFAULT_LEASE_SECONDS, now=None):
    validate_token(token)
    validate_lease_seconds(lease_seconds)
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError("now must be finite")

    connection = connect(lock_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT token, acquired_at, renewed_at, expires_at "
            "FROM run_lease WHERE singleton = 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "renew",
                "ok": False,
                "reason": "no_active_run",
            }
        owner, acquired_at, _, _ = validate_row(row)
        if owner != token:
            connection.commit()
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "renew",
                "ok": False,
                "reason": "ownership_lost",
                "holder": holder_json(row),
            }
        if now < acquired_at:
            raise LockStateError("renewal time predates acquisition")

        expires_at = now + lease_seconds
        connection.execute(
            "UPDATE run_lease SET renewed_at = ?, expires_at = ? "
            "WHERE singleton = 1 AND token = ?",
            (now, expires_at, token),
        )
        connection.commit()
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "renew",
            "ok": True,
            "token": token,
            "renewed_at": iso_utc(now),
            "expires_at": iso_utc(expires_at),
            "lease_seconds": lease_seconds,
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def release(token, lock_file=DEFAULT_LOCK_FILE, now=None):
    validate_token(token)
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    if not math.isfinite(now):
        raise ValueError("now must be finite")

    connection = connect(lock_file)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT token, acquired_at, renewed_at, expires_at "
            "FROM run_lease WHERE singleton = 1"
        ).fetchone()
        if row is None:
            connection.commit()
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "release",
                "ok": False,
                "reason": "no_active_run",
            }
        owner, _, _, _ = validate_row(row)
        if owner != token:
            connection.commit()
            return {
                "schema_version": SCHEMA_VERSION,
                "action": "release",
                "ok": False,
                "reason": "ownership_lost",
                "holder": holder_json(row),
            }

        connection.execute(
            "DELETE FROM run_lease WHERE singleton = 1 AND token = ?",
            (token,),
        )
        connection.commit()
        return {
            "schema_version": SCHEMA_VERSION,
            "action": "release",
            "ok": True,
            "token": token,
            "released_at": iso_utc(now),
        }
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("action", choices=("acquire", "renew", "release"))
    parser.add_argument("--token", help="owner token returned by acquire")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE,
                        help="SQLite lease database (default: run-reports/rhmra-run-lock.sqlite3)")
    parser.add_argument("--lease-seconds", type=positive_int,
                        default=DEFAULT_LEASE_SECONDS,
                        help="lease duration; routine uses the checked-in default")
    parser.add_argument("--now-utc",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()

    try:
        now = parse_now(args.now_utc)
        if args.action == "acquire":
            result = acquire(args.lock_file, args.lease_seconds, now)
        else:
            if not args.token:
                raise ValueError(f"{args.action} requires --token")
            if args.action == "renew":
                result = renew(args.token, args.lock_file,
                               args.lease_seconds, now)
            else:
                result = release(args.token, args.lock_file, now)
    except Exception as exc:
        result = {
            "schema_version": SCHEMA_VERSION,
            "action": args.action,
            "ok": False,
            "reason": "coordination_state_error",
            "detail": str(exc),
        }
        print(json.dumps(result, allow_nan=False))
        return 1

    print(json.dumps(result, allow_nan=False))
    if result["ok"]:
        return 0
    return 2 if args.action == "acquire" else 3


if __name__ == "__main__":
    sys.exit(main())
