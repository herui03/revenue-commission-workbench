"""Expected-vs-recorded payout variance and investigation cases.

variance = recorded - expected, per (period, rep, currency).
* expected comes from the closed payables (basis CLOSED) or the current draft (basis DRAFT);
* recorded comes from the imported (synthetic) payout register and is never edited.

Case identity is a surrogate id (VC-0001) with a UNIQUE (period, rep_id, currency) constraint —
composite keys are separate columns, never a concatenated string. Refresh visits every key that
has an expected amount, a recorded amount OR an existing case, updates amounts (to zero when
both sides disappear), keeps id/owner/notes/resolution, and applies these system transitions:
  variance becomes 0            -> CLEARED  (from any status)
  CLEARED and variance != 0     -> REOPENED
  RESOLVED and variance changed -> REOPENED (the explained amount is no longer the amount)
Resolving a case records an explanation; it does not move or pay money.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db
from .engine import CLAWBACK, EARNING, LATE_CLAWBACK, LATE_EARNING, MANUAL_ADJUSTMENT
from .money import format_minor, half_up_div
from .periods_util import is_period
from .repo import run_lines
from .services import WorkflowError, compute, get_snapshot, is_closed

STATUSES = ("OPEN", "INVESTIGATING", "RESOLVED", "REOPENED", "CLEARED")
REASON_CODES = {
    "MISSED_CLAWBACK": "Recorded payout did not apply a refund clawback",
    "RATE_ERROR": "Recorded payout used the wrong rate or ignored the accelerator",
    "TIMING_LATE_ADJUSTMENT": "Prior-period adjustment posted after the payout register was cut",
    "DUPLICATE_PAYMENT": "Same amount recorded more than once",
    "MANUAL_ENTRY_ERROR": "Keying error in the payout register",
    "EXPECTED_WAS_DRAFT": "Compared against a draft that later changed",
    "OTHER": "Other (explain in the resolution note)",
}
TRANSITIONS = {
    "OPEN": {"INVESTIGATING", "RESOLVED"},
    "INVESTIGATING": {"RESOLVED", "OPEN"},
    "REOPENED": {"INVESTIGATING", "RESOLVED"},
    "RESOLVED": {"REOPENED"},
    "CLEARED": {"REOPENED"},
}


def expected_for(conn: sqlite3.Connection, period: str, *, as_of_run: int | None = None
                 ) -> tuple[dict[tuple[str, str], int], str, list[dict[str, Any]]]:
    """Expected payouts, their basis, and the lines they come from (read-only)."""
    if not is_period(period):
        raise WorkflowError("BAD_PERIOD", f"{period!r} is not a supported YYYY-MM period")
    if as_of_run is not None:
        run = conn.execute("SELECT * FROM calc_runs WHERE run_id = ? AND period = ?", (as_of_run, period)).fetchone()
        if run is None:
            raise WorkflowError("NOT_FOUND", f"run #{as_of_run} does not belong to {period}")
        lines = run_lines(conn, as_of_run)
        basis = "CLOSED" if conn.execute("SELECT 1 FROM periods WHERE closed_run_id = ?", (as_of_run,)).fetchone() \
            else "DRAFT"
    elif is_closed(conn, period):
        snap = get_snapshot(conn, period)
        lines = snap["lines"] if snap else []
        basis = "CLOSED"
    else:
        lines = compute(conn, period).lines
        basis = "DRAFT"
    out: dict[tuple[str, str], int] = {}
    for ln in lines:
        k = (ln["rep_id"], ln["currency"])
        out[k] = out.get(k, 0) + ln["amount_minor"]
    return out, basis, lines


def recorded_for(conn: sqlite3.Connection, period: str) -> tuple[dict[tuple[str, str], int], list[dict[str, Any]]]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM recorded_payouts WHERE period = ? ORDER BY record_id",
                                          (period,))]
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        k = (r["rep_id"], r["currency"])
        out[k] = out.get(k, 0) + r["amount_minor"]
    return out, rows


def variance_table(conn: sqlite3.Connection, period: str, *, as_of_run: int | None = None) -> dict[str, Any]:
    """Read-only comparison (used by pages, exports and as-of views). Never writes."""
    expected, basis, lines = expected_for(conn, period, as_of_run=as_of_run)
    recorded, rec_rows = recorded_for(conn, period)
    cases = {(c["rep_id"], c["currency"]): dict(c) for c in
             conn.execute("SELECT * FROM variance_cases WHERE period = ?", (period,))}
    rows = []
    # Existing case keys are always revisited, so a case whose expected AND recorded both disappear
    # (e.g. the collection was excluded) is refreshed to zero instead of keeping stale amounts.
    for k in sorted(set(expected) | set(recorded) | set(cases)):
        e, r = expected.get(k, 0), recorded.get(k, 0)
        rows.append({"rep_id": k[0], "currency": k[1], "expected_minor": e, "recorded_minor": r,
                     "variance_minor": r - e, "has_expected": k in expected, "has_recorded": k in recorded,
                     "case": cases.get(k)})
    return {"period": period, "basis": basis, "rows": rows, "lines": lines, "recorded_rows": rec_rows,
            "as_of_run": as_of_run}


def _next_case_id(conn: sqlite3.Connection) -> str:
    n = conn.execute("SELECT COUNT(*) AS n FROM variance_cases").fetchone()["n"] + 1
    return f"VC-{n:04d}"


def _note(conn, case_id: str, kind: str, author: str, body: str) -> None:
    conn.execute("INSERT INTO case_notes(case_id, kind, author, body, created_at) VALUES (?, ?, ?, ?, ?)",
                 (case_id, kind, author, body, db.now_iso()))


def refresh_variance(conn: sqlite3.Connection, period: str, actor: str, *, _in_tx: bool = False) -> dict[str, int]:
    with db.tx(conn):
        table = variance_table(conn, period)
        basis = table["basis"]
        stats = {"created": 0, "updated": 0, "reopened": 0, "cleared": 0, "unchanged": 0}
        now = db.now_iso()
        for row in table["rows"]:
            case = row["case"]
            e, r, v = row["expected_minor"], row["recorded_minor"], row["variance_minor"]
            row_basis = basis if row["has_expected"] else "NONE"
            if case is None:
                if v == 0:
                    continue
                case_id = _next_case_id(conn)
                conn.execute("INSERT INTO variance_cases(case_id, period, rep_id, currency, expected_minor, "
                             "expected_basis, recorded_minor, variance_minor, status, created_at, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)",
                             (case_id, period, row["rep_id"], row["currency"], e, row_basis, r, v, now, now))
                _note(conn, case_id, "SYSTEM", actor, f"Case opened: expected {format_minor(e, row['currency'])} "
                      f"({row_basis}), recorded {format_minor(r, row['currency'])}, variance "
                      f"{format_minor(v, row['currency'], signed=True)}.")
                stats["created"] += 1
                continue
            changed = (case["expected_minor"], case["recorded_minor"], case["expected_basis"]) != (e, r, row_basis)
            if not changed:
                stats["unchanged"] += 1
                continue
            status = case["status"]
            new_status = status
            if v == 0 and status != "CLEARED":
                new_status = "CLEARED"          # nothing left to explain (owner/notes/resolution kept)
            elif v != 0 and status == "CLEARED":
                new_status = "REOPENED"
            elif v != 0 and status == "RESOLVED" and v != case["variance_at_resolution"]:
                new_status = "REOPENED"         # the explained amount is no longer the amount
            conn.execute("UPDATE variance_cases SET expected_minor = ?, expected_basis = ?, recorded_minor = ?, "
                         "variance_minor = ?, status = ?, updated_at = ? WHERE case_id = ?",
                         (e, row_basis, r, v, new_status, now, case["case_id"]))
            msg = (f"Refreshed: variance {format_minor(case['variance_minor'], row['currency'], signed=True)} -> "
                   f"{format_minor(v, row['currency'], signed=True)} (expected basis {row_basis}).")
            if new_status != status:
                msg += f" Status {status} -> {new_status}."
                stats["reopened" if new_status == "REOPENED" else "cleared"] += 1
            else:
                stats["updated"] += 1
            _note(conn, case["case_id"], "SYSTEM", actor, msg)
        db.audit(conn, actor, "VARIANCE_REFRESH", "period", period, {"basis": basis, **stats})
        return stats


def get_case(conn: sqlite3.Connection, case_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM variance_cases WHERE case_id = ?", (case_id,)).fetchone()
    return dict(row) if row else None


def case_notes(conn: sqlite3.Connection, case_id: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM case_notes WHERE case_id = ? ORDER BY note_id", (case_id,))]


def update_case(conn: sqlite3.Connection, case_id: str, actor: str, *, status: str | None = None,
                owner: str | None = None, reason_code: str | None = None, resolution_note: str | None = None) -> None:
    with db.tx(conn):
        case = get_case(conn, case_id)
        if case is None:
            raise WorkflowError("NOT_FOUND", f"case {case_id} does not exist")
        changes: dict[str, Any] = {}
        if owner is not None and owner.strip() != (case["owner"] or ""):
            if len(owner.strip()) > 60:
                raise WorkflowError("BAD_OWNER", "owner label is too long")
            changes["owner"] = owner.strip() or None
        if reason_code is not None and reason_code != (case["reason_code"] or ""):
            if reason_code and reason_code not in REASON_CODES:
                raise WorkflowError("BAD_REASON_CODE", f"unknown reason code {reason_code!r}")
            changes["reason_code"] = reason_code or None
        if status is not None and status != case["status"]:
            if status not in TRANSITIONS.get(case["status"], set()):
                raise WorkflowError("BAD_TRANSITION", f"cannot move a case from {case['status']} to {status}")
            if status == "RESOLVED":
                note = (resolution_note or "").strip()
                code = changes.get("reason_code", case["reason_code"])
                if not code:
                    raise WorkflowError("REASON_REQUIRED", "choose a reason code before resolving")
                if len(note) < 10:
                    raise WorkflowError("REASON_REQUIRED", "a resolution note of at least 10 characters is required")
                changes["resolution_note"] = note
                changes["variance_at_resolution"] = case["variance_minor"]
            changes["status"] = status
        if not changes:
            return
        changes["updated_at"] = db.now_iso()
        sets = ", ".join(f"{k} = ?" for k in changes)
        conn.execute(f"UPDATE variance_cases SET {sets} WHERE case_id = ?", [*changes.values(), case_id])
        visible = {k: v for k, v in changes.items() if k != "updated_at"}
        _note(conn, case_id, "SYSTEM", actor, "Updated: " + ", ".join(f"{k}={v}" for k, v in visible.items())
              + (". Resolved means explained - it does not correct or pay money." if visible.get("status") == "RESOLVED"
                 else ""))
        db.audit(conn, actor, "CASE_UPDATE", "variance_case", case_id, visible)


def add_note(conn: sqlite3.Connection, case_id: str, actor: str, body: str) -> None:
    body = (body or "").strip()
    if not body or len(body) > 2000:
        raise WorkflowError("BAD_NOTE", "a note must be 1-2000 characters")
    with db.tx(conn):
        if get_case(conn, case_id) is None:
            raise WorkflowError("NOT_FOUND", f"case {case_id} does not exist")
        _note(conn, case_id, "USER", actor, body)
        db.audit(conn, actor, "CASE_NOTE", "variance_case", case_id, {"chars": len(body)})


def suggestions(case: dict[str, Any], lines: list[dict[str, Any]], recorded_rows: list[dict[str, Any]],
                basis: str) -> list[dict[str, Any]]:
    """Deterministic candidate explanations. Heuristics for an analyst to confirm, never conclusions."""
    v = case["variance_minor"]
    ccy = case["currency"]
    mine = [ln for ln in lines if ln["rep_id"] == case["rep_id"] and ln["currency"] == ccy]
    recs = [r for r in recorded_rows if r["rep_id"] == case["rep_id"] and r["currency"] == ccy]
    out: list[dict[str, Any]] = []
    if v == 0:
        return out
    for ln in mine:
        if ln["amount_minor"] and v == -ln["amount_minor"]:
            out.append({"code": _code_for(ln), "match": "exact", "evidence": [ln["line_key"]],
                        "message": f"Variance equals minus line {ln['line_key']} "
                                   f"({format_minor(ln['amount_minor'], ccy)}): the register appears to omit it."})
        elif ln["amount_minor"] and v == ln["amount_minor"]:
            out.append({"code": "DUPLICATE_PAYMENT", "match": "exact", "evidence": [ln["line_key"]],
                        "message": f"Variance equals line {ln['line_key']}: the register may count it twice."})
    for label, types, code in (("all clawbacks", (CLAWBACK, LATE_CLAWBACK), "MISSED_CLAWBACK"),
                               ("all late prior-period adjustments", (LATE_EARNING, LATE_CLAWBACK),
                                "TIMING_LATE_ADJUSTMENT"),
                               ("all manual adjustments", (MANUAL_ADJUSTMENT,), "OTHER")):
        group = [ln for ln in mine if ln["line_type"] in types]
        total = sum(ln["amount_minor"] for ln in group)
        if len(group) > 1 and total and v == -total:
            out.append({"code": code, "match": "exact", "evidence": [ln["line_key"] for ln in group],
                        "message": f"Variance equals minus {label} ({format_minor(total, ccy)})."})
    earn = [ln for ln in mine if ln["line_type"] in (EARNING, LATE_EARNING)]
    uplift = sum(ln["amount_minor"] - half_up_div(ln["credited_minor"] * ln["base_rate_bps"], 10_000) for ln in earn)
    if uplift and abs(v + uplift) <= len(earn):
        out.append({"code": "RATE_ERROR", "match": "exact" if v == -uplift else "within rounding",
                    "evidence": [ln["line_key"] for ln in earn if ln["accel_portion_minor"]],
                    "message": f"Recorded payout matches base rate on all credited cash: the accelerator uplift of "
                               f"{format_minor(uplift, ccy)} is missing."})
    amounts = [r["amount_minor"] for r in recs]
    if len(amounts) != len(set(amounts)):
        out.append({"code": "DUPLICATE_PAYMENT", "match": "pattern", "evidence": [r["record_id"] for r in recs],
                    "message": "Two register rows carry the same amount - check for a duplicate recording."})
    if not recs:
        out.append({"code": "OTHER", "match": "pattern", "evidence": [],
                    "message": "Nothing recorded yet for this rep/currency - possibly a timing difference."})
    if not mine and recs:
        out.append({"code": "MANUAL_ENTRY_ERROR", "match": "pattern", "evidence": [r["record_id"] for r in recs],
                    "message": "A payout was recorded but no commission is expected."})
    if basis == "DRAFT":
        out.append({"code": "EXPECTED_WAS_DRAFT", "match": "context", "evidence": [],
                    "message": "Expected is a DRAFT; it can still change before the period closes."})
    return out


def _code_for(ln: dict[str, Any]) -> str:
    return {CLAWBACK: "MISSED_CLAWBACK", LATE_CLAWBACK: "TIMING_LATE_ADJUSTMENT",
            LATE_EARNING: "TIMING_LATE_ADJUSTMENT", MANUAL_ADJUSTMENT: "OTHER"}.get(ln["line_type"], "MANUAL_ENTRY_ERROR")
