"""Calendar-month period helpers ("YYYY-MM")."""
from __future__ import annotations

import calendar
import re
import sqlite3
from datetime import date

PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")


def is_period(text: str) -> bool:
    return bool(PERIOD_RE.fullmatch(text or ""))


def period_of(d: date | str) -> str:
    if isinstance(d, str):
        d = date.fromisoformat(d)
    return f"{d.year:04d}-{d.month:02d}"


def period_start(period: str) -> date:
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, 1)


def month_end(period: str) -> date:
    y, m = int(period[:4]), int(period[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])


def next_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y + (m == 12):04d}-{(m % 12) + 1:02d}"


def prev_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y - (m == 1):04d}-{12 if m == 1 else m - 1:02d}"


def last_closed_period(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(period) AS p FROM periods WHERE status = 'CLOSED'").fetchone()
    return row["p"] if row and row["p"] else None


def period_label(period: str) -> str:
    return period_start(period).strftime("%B %Y")
