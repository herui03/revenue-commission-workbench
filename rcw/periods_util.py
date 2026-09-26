"""Calendar-month period helpers ("YYYY-MM") — the single place periods and dates are validated.

Supported range (documented demo limit): years 2000-2099 inclusive. Every period string that
enters the system (imports, CLI, web, workflow services) goes through `is_period`, and every
business date through `is_supported_date`, so values such as "0000-01", "2026-13",
"2026-04\\n" or "9999-12-31" are rejected instead of crashing or being persisted.
"""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date

MIN_YEAR, MAX_YEAR = 2000, 2099
PERIOD_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")   # used with fullmatch only (ASCII digits, no newline)


class PeriodRangeError(ValueError):
    pass


def is_period(text: object) -> bool:
    if not isinstance(text, str) or not PERIOD_RE.fullmatch(text):
        return False
    return MIN_YEAR <= int(text[:4]) <= MAX_YEAR


def is_supported_date(d: date) -> bool:
    return MIN_YEAR <= d.year <= MAX_YEAR


def require_period(text: object) -> str:
    if not is_period(text):
        raise PeriodRangeError(f"{text!r} is not a YYYY-MM period between {MIN_YEAR}-01 and {MAX_YEAR}-12")
    return text  # type: ignore[return-value]


def period_of(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.year:04d}-{d.month:02d}"


def period_start(period: str) -> date:
    require_period(period)
    return date(int(period[:4]), int(period[5:7]), 1)


def month_end(period: str) -> date:
    require_period(period)
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def next_period(period: str) -> str:
    require_period(period)
    y, m = int(period[:4]), int(period[5:7])
    return require_period(f"{y + (m == 12):04d}-{(m % 12) + 1:02d}")


def prev_period(period: str) -> str:
    require_period(period)
    y, m = int(period[:4]), int(period[5:7])
    return require_period(f"{y - (m == 1):04d}-{12 if m == 1 else m - 1:02d}")


def last_closed_period(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(period) AS p FROM periods WHERE status = 'CLOSED'").fetchone()
    return row["p"] if row and row["p"] else None


def period_label(period: str) -> str:
    return period_start(period).strftime("%B %Y")
