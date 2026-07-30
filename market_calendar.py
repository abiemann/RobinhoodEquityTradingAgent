"""Reviewed NYSE core-session calendar for market_clock.py.

This module deliberately contains data rather than holiday formulas or a
runtime web fetch. Published exchange dates include one-off observances and
early closes, while a network lookup during a trading run could fail open or
silently use stale data. When a date is outside this reviewed coverage,
market_clock.py blocks new entries.

Source: https://www.nyse.com/trade/hours-calendars
Reviewed: 2026-07-29. Coverage: calendar years 2026--2028.
"""

from datetime import date


REGULAR_OPEN_MINUTE = 9 * 60 + 30
NORMAL_REGULAR_CLOSE_MINUTE = 16 * 60
EARLY_REGULAR_CLOSE_MINUTE = 13 * 60

CALENDAR_YEARS = frozenset({2026, 2027, 2028})

# Full NYSE equity-market closures published for the covered years.
CLOSED_DATES = frozenset({
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15),
    date(2027, 3, 26), date(2027, 5, 31), date(2027, 6, 18),
    date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25),
    date(2027, 12, 24),
    # 2028 (NYSE observes no New Year's Day closure because Jan. 1 is Saturday)
    date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14),
    date(2028, 5, 29), date(2028, 6, 19), date(2028, 7, 4),
    date(2028, 9, 4), date(2028, 11, 23), date(2028, 12, 25),
})

# Date -> core-session close, in minutes after midnight Eastern. The routine
# blocks new entries outside the shortened regular session rather than
# assuming broker-specific extended-hours availability on these special days.
EARLY_CLOSE_MINUTES_BY_DATE = {
    date(2026, 11, 27): EARLY_REGULAR_CLOSE_MINUTE,
    date(2026, 12, 24): EARLY_REGULAR_CLOSE_MINUTE,
    date(2027, 11, 26): EARLY_REGULAR_CLOSE_MINUTE,
    date(2028, 7, 3): EARLY_REGULAR_CLOSE_MINUTE,
    date(2028, 11, 24): EARLY_REGULAR_CLOSE_MINUTE,
}


def core_session_schedule(day):
    """Return ``(calendar_status, regular_close_minute)`` for an ET date.

    ``regular_close_minute`` is ``None`` for full closures and uncovered
    years. Weekends are handled by market_clock.py because they need no
    calendar entry and are closed regardless of coverage.
    """
    if day.year not in CALENDAR_YEARS:
        return "unknown", None
    if day in CLOSED_DATES:
        return "holiday", None
    if day in EARLY_CLOSE_MINUTES_BY_DATE:
        return "early-close", EARLY_CLOSE_MINUTES_BY_DATE[day]
    return "normal", NORMAL_REGULAR_CLOSE_MINUTE
