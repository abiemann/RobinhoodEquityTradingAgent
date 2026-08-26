#!/usr/bin/env python3
"""Authoritative run clock for the Robinhood momentum routine.

Every time-dependent decision in robinhood-momentum-routine-autonomous.md
(opening blackout, session-aware order style, "filled today" counting,
re-entry cooldown, dust lookback, ledger timestamps, report header) reads
its time from THIS script. The routine captures one start clock for all
run-wide timestamps and the deterministic historical-data start boundary,
then invokes the same script
immediately before each buy so a long run cannot use a stale session verdict.
It never derives the time any other way.

Why a script instead of a shell command: the obvious approaches are not
portable and fail SILENTLY. On Windows (Git Bash / CPython without the
`tzdata` package) `TZ=America/New_York date` returns GMT rather than
erroring, and `zoneinfo.ZoneInfo("America/New_York")` raises
ZoneInfoNotFoundError. A run that improvises either one can get a
plausible-looking wrong clock and mis-evaluate the opening blackout.
This script depends only on UTC (always available) and computes the US
Eastern/Pacific offsets from the DST rule itself, so it gives the same
answer on Windows and in the Linux sandbox.

Usage (routine):  python3 market_clock.py --json \
                    --expected-constants-sha256 <preflight source_sha256>
Pre-buy recheck:  use the same command and preflight hash
Human-readable:    python3 market_clock.py
Testing:           python3 market_clock.py --now-utc 2026-07-21T15:07:00Z
                   python3 market_clock.py --no-buy-first-minutes 5    # override

The blackout minute count is read through validate_constants.py's shared,
full-file validator. The routine must NOT substitute the value on the
command line — an agent invoked this with `--no-buy-first-minutes 5`
against a constants.md of 45 on 2026-07-22 (safe by luck, could have
unlocked buying inside the blackout). --no-buy-first-minutes stays as a
test override only.

NYSE full closures and 1:00 p.m. ET early closes live in the reviewed,
checked-in market_calendar.py table (currently 2026--2028). A weekday beyond
that coverage is calendar-unknown and cannot open a new entry; the routine
may keep managing existing positions.

US DST rule (since 2007): starts the second Sunday in March at 02:00
local standard time, ends the first Sunday in November at 02:00 local
daylight time. Eastern is UTC-5 (EST) / UTC-4 (EDT); Pacific is UTC-8
(PST) / UTC-7 (PDT).

TESTED BY tests/test_scripts.py — after ANY edit to this file, run
`python3 tests/test_scripts.py` (Windows: `py -3 tests\\test_scripts.py`)
and require all tests to pass before committing.
"""

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from market_calendar import REGULAR_OPEN_MINUTE, core_session_schedule
from validate_constants import ConstantsValidationError, validate_constants_file

EASTERN_STD_OFFSET = -5
PACIFIC_STD_OFFSET = -8
SCHEMA_VERSION = 1

PREMARKET_OPEN = (4, 0)
AFTERHOURS_CLOSE = (20, 0)


def _nth_weekday(year, month, weekday, n):
    """Date of the nth given weekday (0=Monday) in a month."""
    d = datetime(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _is_dst(utc_naive, std_offset):
    """True if US DST is in effect at this UTC instant for a zone.

    Transitions happen at 02:00 LOCAL, so they land at different UTC
    instants per zone: spring-forward at 02:00 standard time, fall-back
    at 02:00 daylight time (one hour further east in UTC terms).
    """
    year = utc_naive.year
    start_local = _nth_weekday(year, 3, 6, 2).replace(hour=2)   # 2nd Sunday, March
    end_local = _nth_weekday(year, 11, 6, 1).replace(hour=2)    # 1st Sunday, November
    start_utc = start_local - timedelta(hours=std_offset)
    end_utc = end_local - timedelta(hours=std_offset + 1)
    return start_utc <= utc_naive < end_utc


def zone_time(utc_dt, std_offset, std_name, dst_name):
    utc_naive = utc_dt.replace(tzinfo=None)
    dst = _is_dst(utc_naive, std_offset)
    offset = std_offset + (1 if dst else 0)
    return utc_naive + timedelta(hours=offset), (dst_name if dst else std_name), offset


def session_state(et_dt):
    """Return session, clock and calendar verdicts for an Eastern datetime.

    ``entry_session_open`` means the current date/time is a known window in
    which a new entry MAY be considered. It deliberately says nothing about
    the other entry gates or per-symbol broker tradability.
    """
    if et_dt.weekday() >= 5:
        return "closed-weekend", None, "weekend", False, None
    calendar_status, close_m = core_session_schedule(et_dt.date())
    if calendar_status == "unknown":
        return "calendar-unknown", None, calendar_status, False, None
    if calendar_status == "holiday":
        return "closed-holiday", None, calendar_status, False, None

    minutes = et_dt.hour * 60 + et_dt.minute
    open_m = REGULAR_OPEN_MINUTE
    pre_m = PREMARKET_OPEN[0] * 60 + PREMARKET_OPEN[1]
    after_m = AFTERHOURS_CLOSE[0] * 60 + AFTERHOURS_CLOSE[1]
    since_open = minutes - open_m
    if minutes < pre_m:
        return "closed", since_open, calendar_status, False, close_m
    if minutes < open_m:
        return "pre-market", since_open, calendar_status, calendar_status == "normal", close_m
    if minutes < close_m:
        return "regular", since_open, calendar_status, True, close_m
    if calendar_status == "early-close":
        return "closed-early", since_open, calendar_status, False, close_m
    if minutes < after_m:
        return "after-hours", since_open, calendar_status, True, close_m
    return "closed", since_open, calendar_status, False, close_m


def format_et_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def read_no_buy_first_minutes(constants_path=None):
    """Load NO_BUY_FIRST_MINUTES through the full constants validator.

    Reading it here — instead of taking it as a CLI flag the routine
    substitutes by hand — closes an improvisation gap: on 2026-07-22 an
    agent invoked this script with `--no-buy-first-minutes 5` when
    constants.md said 45 (safe by luck; the mismatch could have unlocked
    buying inside the blackout window).
    """
    validated = validate_constants_file(constants_path)
    return validated.values["NO_BUY_FIRST_MINUTES"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-buy-first-minutes", type=int, default=None,
                    help="OPTIONAL override for tests; the routine must NOT pass this — the value is read from constants.md so the agent never re-types it")
    ap.add_argument("--constants", help="path to constants.md (testing only)")
    ap.add_argument(
        "--expected-constants-sha256",
        help="preflight constants hash; REQUIRED by the routine, optional for tests",
    )
    ap.add_argument("--now-utc", help="override the clock, ISO-8601 UTC (testing only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        validated_constants = validate_constants_file(args.constants)
    except ConstantsValidationError as exc:
        print(
            f"market_clock.py: ERROR: constants validation failed: "
            f"{exc.errors[0]}",
            file=sys.stderr,
        )
        return 2

    if args.no_buy_first_minutes is None:
        blackout_minutes = validated_constants.values["NO_BUY_FIRST_MINUTES"]
        constants_sha256 = validated_constants.source_sha256
    else:
        blackout_minutes = args.no_buy_first_minutes
        constants_sha256 = None

    if args.expected_constants_sha256 is not None:
        expected_hash = args.expected_constants_sha256
        if (
            len(expected_hash) != 64
            or any(character not in "0123456789abcdef" for character in expected_hash)
        ):
            print(
                "market_clock.py: ERROR: --expected-constants-sha256 must be "
                "64 lowercase hex characters",
                file=sys.stderr,
            )
            return 2
        if constants_sha256 is None:
            print(
                "market_clock.py: ERROR: --expected-constants-sha256 cannot "
                "be combined with the blackout test override",
                file=sys.stderr,
            )
            return 2
        if constants_sha256 != expected_hash:
            print(
                "market_clock.py: ERROR: constants.md changed after preflight: "
                "SHA-256 does not match --expected-constants-sha256",
                file=sys.stderr,
            )
            return 2

    if args.now_utc:
        utc = datetime.strptime(args.now_utc.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        utc = datetime.now(timezone.utc)

    et, et_name, _ = zone_time(utc, EASTERN_STD_OFFSET, "EST", "EDT")
    pt, pt_name, pt_offset = zone_time(
        utc, PACIFIC_STD_OFFSET, "PST", "PDT"
    )
    state, since_open, calendar_status, entry_session_open, regular_close = session_state(et)

    lookback_days = max(
        validated_constants.values["VOLUME_LOOKBACK_DAYS"],
        validated_constants.values["HIGH_LOOKBACK_DAYS"],
    )
    # Convert the configured trading-day requirement to a conservative
    # calendar window.  One base week covers short windows; each additional
    # 20 requested sessions adds another calendar day of closure margin so
    # large, still-valid lookbacks cannot be starved by accumulated holidays.
    # With the current 20-day lookbacks this remains exactly 35 calendar days.
    closure_margin_days = 7 + (lookback_days - 1) // 20
    historical_calendar_days = (
        (lookback_days * 7 + 4) // 5 + closure_margin_days
    )
    historicals_start_time = (
        utc - timedelta(days=historical_calendar_days)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    in_blackout = (state == "regular" and since_open is not None
                   and since_open < blackout_minutes)

    out = {
        "schema_version": SCHEMA_VERSION,
        "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "et": f"{et:%Y-%m-%d %H:%M:%S} {et_name}",
        "pt": f"{pt:%Y-%m-%d %H:%M:%S} {pt_name}",
        "pt_iso": pt.replace(
            tzinfo=timezone(timedelta(hours=pt_offset))
        ).isoformat(timespec="seconds"),
        "date_et": et.strftime("%Y-%m-%d"),
        "date_pt": pt.strftime("%Y-%m-%d"),
        "historicals_start_time": historicals_start_time,
        "constants_sha256": constants_sha256,
        "session": state,
        "calendar_status": calendar_status,
        "regular_close_et": None if regular_close is None else format_et_time(regular_close),
        "entry_session_open": entry_session_open,
        "minutes_since_open": since_open,
        "opening_blackout": in_blackout,
    }

    if args.json:
        print(json.dumps(out, indent=2))
    else:
        print(f"UTC     {out['utc']}")
        print(f"ET      {out['et']}")
        print(f"PT      {out['pt']}   (trading day {out['date_pt']} Pacific)")
        print(f"Session {state}" + (f"  |  {since_open} min since 09:30 ET open" if since_open is not None else ""))
        close_label = "closed" if out["regular_close_et"] is None else f"{out['regular_close_et']} ET"
        print(f"Calendar {calendar_status}  |  regular close {close_label}  |  "
              f"new-entry window {'open' if entry_session_open else 'closed'}")
        verdict = "BLOCKED — opening blackout" if in_blackout else "clear"
        print(f"Blackout (first {blackout_minutes} min, from constants.md): {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
