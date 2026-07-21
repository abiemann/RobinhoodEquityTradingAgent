#!/usr/bin/env python3
"""Authoritative run clock for the Robinhood momentum routine.

Every time-dependent decision in robinhood-momentum-routine-autonomous.md
(opening blackout, session-aware order style, "filled today" counting,
re-entry cooldown, dust lookback, ledger timestamps, report header) reads
its time from THIS script — run once at the start of a run. The routine
never derives the time any other way.

Why a script instead of a shell command: the obvious approaches are not
portable and fail SILENTLY. On Windows (Git Bash / CPython without the
`tzdata` package) `TZ=America/New_York date` returns GMT rather than
erroring, and `zoneinfo.ZoneInfo("America/New_York")` raises
ZoneInfoNotFoundError. A run that improvises either one can get a
plausible-looking wrong clock and mis-evaluate the opening blackout.
This script depends only on UTC (always available) and computes the US
Eastern/Pacific offsets from the DST rule itself, so it gives the same
answer on Windows and in the Linux sandbox.

Usage:
  python3 market_clock.py [--no-buy-first-minutes 45] [--json]
  python3 market_clock.py --now-utc 2026-07-21T15:07:00Z    # for testing

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

EASTERN_STD_OFFSET = -5
PACIFIC_STD_OFFSET = -8

REGULAR_OPEN = (9, 30)
REGULAR_CLOSE = (16, 0)
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
    """Market session for an Eastern-time datetime, plus minutes since the open."""
    if et_dt.weekday() >= 5:
        return "closed-weekend", None
    minutes = et_dt.hour * 60 + et_dt.minute
    open_m = REGULAR_OPEN[0] * 60 + REGULAR_OPEN[1]
    close_m = REGULAR_CLOSE[0] * 60 + REGULAR_CLOSE[1]
    pre_m = PREMARKET_OPEN[0] * 60 + PREMARKET_OPEN[1]
    after_m = AFTERHOURS_CLOSE[0] * 60 + AFTERHOURS_CLOSE[1]
    since_open = minutes - open_m
    if minutes < pre_m:
        return "closed", since_open
    if minutes < open_m:
        return "pre-market", since_open
    if minutes < close_m:
        return "regular", since_open
    if minutes < after_m:
        return "after-hours", since_open
    return "closed", since_open


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-buy-first-minutes", type=int, default=0,
                    help="NO_BUY_FIRST_MINUTES from Constants.md; reports the opening-blackout verdict")
    ap.add_argument("--now-utc", help="override the clock, ISO-8601 UTC (testing only)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.now_utc:
        utc = datetime.strptime(args.now_utc.rstrip("Z")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    else:
        utc = datetime.now(timezone.utc)

    et, et_name, _ = zone_time(utc, EASTERN_STD_OFFSET, "EST", "EDT")
    pt, pt_name, _ = zone_time(utc, PACIFIC_STD_OFFSET, "PST", "PDT")
    state, since_open = session_state(et)

    in_blackout = (state == "regular" and since_open is not None
                   and since_open < args.no_buy_first_minutes)

    out = {
        "utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "et": f"{et:%Y-%m-%d %H:%M:%S} {et_name}",
        "pt": f"{pt:%Y-%m-%d %H:%M:%S} {pt_name}",
        "date_pt": pt.strftime("%Y-%m-%d"),
        "session": state,
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
        if args.no_buy_first_minutes:
            verdict = "BLOCKED — opening blackout" if in_blackout else "clear"
            print(f"Blackout (first {args.no_buy_first_minutes} min): {verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
