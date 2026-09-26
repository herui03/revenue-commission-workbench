"""Application services: every state change goes through here (CLI and web share them).

Each function runs in one transaction and writes its audit entry in that same
transaction. Errors are raised as `WorkflowError(code, message)` with stable codes.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any

from . import db, engine
from .money import CURRENCIES
from .periods_util import MAX_YEAR, MIN_YEAR, is_period, is_supported_date, last_closed_period, month_end, \
    next_period, period_of
from .repo import activity_periods, load_engine_input, run_lines

POLICY_SUMMARY = (
    "Invented demo policy (not an industry or legal rule): cash-based commission on COLLECTION events; "
    "credit split by contract (largest-remainder cents, ties to smaller rep id); marginal monthly ladder per "
    "rep x month x currency - credited collections up to the plan threshold earn the base rate, the excess "
    "earns the accelerator rate; one half-up rounding per line; refunds reverse the stored original earning "
    "cumulatively (half_up(E*R/x)) and never restore tier capacity; closed months are frozen and late data is "
    "posted as a linked prior-period adjustment in the first open month."
)
MIN_REASON = 10


class WorkflowError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


def _require_period(period: str) -> None:
    """Every workflow entry point validates its period here, before any write (R-5)."""
    if not is_period(period):
        raise WorkflowError("BAD_PERIOD", f"{period!r} is not a YYYY-MM period with a real month in years "
                            f"{MIN_YEAR}-{MAX_YEAR}")


def business_date(conn: sqlite3.Connection) -> date:
    val = db.get_setting(conn, "business_date")
    return date.fromisoformat(val) if val else date.today()


def set_business_date(conn: sqlite3.Connection, new_date: str, actor: str) -> None:
    try:
        d = date.fromisoformat(new_date)
    except (TypeError, ValueError) as exc:
        raise WorkflowError("BAD_DATE", f"{new_date!r} is not an ISO date") from exc
    if not is_supported_date(d) or len(new_date) != 10:
        raise WorkflowError("BAD_DATE", f"{new_date!r} must be YYYY-MM-DD within years {MIN_YEAR}-{MAX_YEAR}")
    with db.tx(conn):
        old = db.get_setting(conn, "business_date")
        if old and d < date.fromisoformat(old):
            raise WorkflowError("DATE_BACKWARDS", f"business date cannot move backwards ({old} -> {d})")
        db.set_setting(conn, "business_date", d.isoformat())
        db.audit(conn, actor, "BUSINESS_DATE_SET", "settings", "business_date", {"from": old, "to": d.isoformat()})


def period_row(conn: sqlite3.Connection, period: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM periods WHERE period = ?", (period,)).fetchone()


def is_closed(conn: sqlite3.Connection, period: str) -> bool:
    last = last_closed_period(conn)
    return bool(last and period <= last)


def first_open_period(conn: sqlite3.Connection) -> str | None:
    last = last_closed_period(conn)
    if last:
        return next_period(last)
    acts = activity_periods(conn)
    return acts[0] if acts else None


def compute(conn: sqlite3.Connection, period: str) -> engine.PeriodResult:
    """In-memory calculation of an open period (read-only)."""
    _require_period(period)
    if is_closed(conn, period):
        raise WorkflowError("PERIOD_CLOSED", f"{period} is closed; its snapshot is immutable")
    try:
        return engine.calculate(period, load_engine_input(conn))
    except engine.EngineError as exc:
        raise WorkflowError("ENGINE", str(exc)) from exc


def latest_run(conn: sqlite3.Connection, period: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM calc_runs WHERE period = ? ORDER BY run_id DESC LIMIT 1", (period,)).fetchone()


def _ensure_period(conn: sqlite3.Connection, period: str) -> None:
    conn.execute("INSERT OR IGNORE INTO periods(period, status) VALUES (?, 'OPEN')", (period,))


def _store_run(conn: sqlite3.Connection, result: engine.PeriodResult, actor: str) -> int:
    cur = conn.execute(
        "INSERT INTO calc_runs(period, created_at, actor, engine_version, result_digest, line_count, blocking_holds,"
        " result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (result.period, db.now_iso(), actor, result.engine_version, result.result_digest, len(result.lines),
         len(result.blocking_holds), db.canonical_json(result.to_json_dict())))
    run_id = int(cur.lastrowid)
    for ln in result.lines:
        conn.execute(
            "INSERT INTO calc_lines(run_id, line_key, line_type, period, original_period, rep_id, currency, event_id,"
            " original_event_id, contract_id, event_date, plan_id, plan_version, split_bps, credited_minor,"
            " credit_reversed_minor, base_portion_minor, accel_portion_minor, base_rate_bps, accel_rate_bps,"
            " exact_amount, amount_minor, detail_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, ln["line_key"], ln["line_type"], ln["period"], ln["original_period"], ln["rep_id"],
             ln["currency"], ln["event_id"], ln["original_event_id"], ln["contract_id"], ln["event_date"],
             ln["plan_id"], ln["plan_version"], ln["split_bps"], ln["credited_minor"], ln["credit_reversed_minor"],
             ln["base_portion_minor"], ln["accel_portion_minor"], ln["base_rate_bps"], ln["accel_rate_bps"],
             ln["exact_amount"], ln["amount_minor"], db.canonical_json(ln["detail"])))
    return run_id


@dataclass
class CalcOutcome:
    run_id: int
    result: engine.PeriodResult
    review_withdrawn: bool


def calculate_period(conn: sqlite3.Connection, period: str, actor: str) -> CalcOutcome:
    _require_period(period)
    with db.tx(conn):
        result = compute(conn, period)
        _ensure_period(conn, period)
        run_id = _store_run(conn, result, actor)
        withdrawn = False
        prow = period_row(conn, period)
        if prow["status"] == "IN_REVIEW":
            reviewed = conn.execute("SELECT result_digest FROM calc_runs WHERE run_id = ?",
                                    (prow["review_run_id"],)).fetchone()
            if reviewed["result_digest"] != result.result_digest:
                conn.execute("UPDATE periods SET status = 'OPEN', review_run_id = NULL, prepared_by = NULL, "
                             "submitted_at = NULL WHERE period = ?", (period,))
                withdrawn = True
        db.audit(conn, actor, "CALCULATE", "period", period,
                 {"run_id": run_id, "result_digest": result.result_digest, "lines": len(result.lines),
                  "blocking_holds": len(result.blocking_holds), "review_withdrawn": withdrawn})
        return CalcOutcome(run_id, result, withdrawn)


def submit_for_review(conn: sqlite3.Connection, period: str, actor: str) -> int:
    _require_period(period)
    with db.tx(conn):
        prow = period_row(conn, period)
        if is_closed(conn, period):
            raise WorkflowError("PERIOD_CLOSED", f"{period} is already closed")
        run = latest_run(conn, period)
        if prow is None or run is None:
            raise WorkflowError("NO_CALCULATION", "calculate the period before submitting it for review")
        if prow["status"] == "IN_REVIEW":
            raise WorkflowError("ALREADY_IN_REVIEW", f"{period} is already in review (run #{prow['review_run_id']})")
        current = compute(conn, period)
        if current.result_digest != run["result_digest"]:
            raise WorkflowError("STALE_RUN", "inputs changed since the latest calculation; recalculate first",
                                _diff(conn, run["run_id"], current))
        if current.blocking_holds:
            raise WorkflowError("BLOCKING_HOLDS", f"{len(current.blocking_holds)} blocking hold(s) must be resolved "
                                "before review", current.blocking_holds)
        conn.execute("UPDATE periods SET status = 'IN_REVIEW', review_run_id = ?, prepared_by = ?, submitted_at = ? "
                     "WHERE period = ?", (run["run_id"], actor, db.now_iso(), period))
        db.audit(conn, actor, "SUBMIT_FOR_REVIEW", "period", period,
                 {"run_id": run["run_id"], "result_digest": run["result_digest"]})
        return int(run["run_id"])


def return_to_draft(conn: sqlite3.Connection, period: str, actor: str, reason: str) -> None:
    _require_period(period)
    _require_reason(reason)
    with db.tx(conn):
        prow = period_row(conn, period)
        if prow is None or prow["status"] != "IN_REVIEW":
            raise WorkflowError("NOT_IN_REVIEW", f"{period} is not in review")
        conn.execute("UPDATE periods SET status = 'OPEN', review_run_id = NULL, prepared_by = NULL, submitted_at = NULL"
                     " WHERE period = ?", (period,))
        db.audit(conn, actor, "RETURN_TO_DRAFT", "period", period, {"reason": reason.strip()})


def _diff(conn: sqlite3.Connection, run_id: int, current: engine.PeriodResult) -> dict[str, Any]:
    d = engine.diff_lines(run_lines(conn, run_id), current.lines)
    stored = json.loads(conn.execute("SELECT result_json FROM calc_runs WHERE run_id = ?", (run_id,)).fetchone()[0])
    now = current.to_json_dict()
    other = [f for f in ("totals", "controls", "kpis", "holds", "excluded", "accounted_event_ids", "inputs_used")
             if db.canonical_json(stored.get(f)) != db.canonical_json(now.get(f))]
    return {"added": [x["line_key"] for x in d["added"]], "removed": [x["line_key"] for x in d["removed"]],
            "changed": d["changed"], "other_changed_sections": other,
            "current_blocking_holds": len(current.blocking_holds)}


def close_period(conn: sqlite3.Connection, period: str, actor: str) -> dict[str, Any]:
    """Freeze the exact reviewed run. Rejects anything that would close unreviewed numbers."""
    _require_period(period)
    with db.tx(conn):
        prow = period_row(conn, period)
        if prow is not None and prow["status"] == "CLOSED":
            raise WorkflowError("ALREADY_CLOSED", f"{period} was closed at {prow['closed_at']}; closing twice is "
                                "not possible and no second payable is created")
        if is_closed(conn, period):
            raise WorkflowError("PERIOD_CLOSED", f"{period} is on or before the last closed period")
        if prow is None or prow["status"] != "IN_REVIEW":
            raise WorkflowError("NOT_IN_REVIEW", f"{period} must be calculated and submitted for review before close")
        if actor.strip().lower() == (prow["prepared_by"] or "").strip().lower():
            raise WorkflowError("SAME_ACTOR", "the reviewer label must differ from the preparer label "
                                "(demo control on labels only - not authentication)")
        bdate = business_date(conn)
        if month_end(period) >= bdate:
            raise WorkflowError("PERIOD_NOT_ENDED", f"{period} ends {month_end(period)}; the business date is {bdate}")
        earlier_open = [p for p in activity_periods(conn) if p < period and not is_closed(conn, p)]
        if earlier_open:
            raise WorkflowError("EARLIER_PERIOD_OPEN", f"close earlier periods first: {', '.join(earlier_open)}")
        current = compute(conn, period)
        if current.blocking_holds:
            raise WorkflowError("BLOCKING_HOLDS", f"{len(current.blocking_holds)} blocking hold(s) exist; resolve "
                                "them, recalculate and resubmit", current.blocking_holds)
        reviewed = conn.execute("SELECT * FROM calc_runs WHERE run_id = ?", (prow["review_run_id"],)).fetchone()
        if reviewed["result_digest"] != current.result_digest:
            raise WorkflowError("STALE_REVIEW", "inputs changed after the review was submitted (new import, plan, "
                                "decision or adjustment). The reviewed numbers are no longer current: recalculate "
                                "and resubmit. Nothing was closed.", _diff(conn, reviewed["run_id"], current))
        closed_at = db.now_iso()
        # Freeze the STORED reviewed run (its lines and result JSON), never the fresh recomputation.
        # The digest equality above proves they are identical in every frozen field.
        snapshot = build_snapshot(conn, period, reviewed, prow["prepared_by"], actor, prow["submitted_at"],
                                  closed_at)
        snap_json = db.canonical_json(snapshot)
        snap_sha = db.sha256_text(snap_json)
        conn.execute("INSERT INTO period_snapshots(period, run_id, snapshot_json, sha256, closed_at) VALUES (?,?,?,?,?)",
                     (period, reviewed["run_id"], snap_json, snap_sha, closed_at))
        for t in snapshot["totals"]:
            conn.execute("INSERT INTO payables(period, rep_id, currency, amount_minor, snapshot_sha256, created_at)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (period, t["rep_id"], t["currency"], t["net_minor"], snap_sha,
                                                        closed_at))
        conn.execute("UPDATE periods SET status = 'CLOSED', closed_run_id = ?, reviewed_by = ?, closed_at = ?, "
                     "snapshot_sha256 = ? WHERE period = ?", (reviewed["run_id"], actor, closed_at, snap_sha, period))
        db.audit(conn, actor, "CLOSE_PERIOD", "period", period,
                 {"run_id": reviewed["run_id"], "snapshot_sha256": snap_sha, "payables": len(snapshot["totals"]),
                  "prepared_by": prow["prepared_by"]})
        from . import variance  # local import: variance depends on services
        variance.refresh_variance(conn, period, actor, _in_tx=True)
        return {"period": period, "run_id": reviewed["run_id"], "snapshot_sha256": snap_sha,
                "payables": len(snapshot["totals"])}


def build_snapshot(conn, period, run, prepared_by, reviewed_by, submitted_at, closed_at) -> dict[str, Any]:
    """Snapshot built only from the stored reviewed run (lines + result JSON)."""
    stored = json.loads(run["result_json"])
    lines = run_lines(conn, run["run_id"])
    reps = {r["rep_id"]: {"display_name": r.get("display_name"), "team": r.get("team")}
            for r in stored["inputs_used"].get("reps", [])}
    return {
        "schema": "rcw-snapshot/1", "period": period, "engine_version": stored["engine_version"],
        "run_id": run["run_id"], "result_digest": run["result_digest"], "policy": POLICY_SUMMARY,
        "prepared_by": prepared_by, "submitted_at": submitted_at, "reviewed_by": reviewed_by, "closed_at": closed_at,
        "business_date_at_close": business_date(conn).isoformat(),
        "reps": reps,
        "lines": lines, "totals": stored["totals"], "controls": stored["controls"], "kpis": stored["kpis"],
        "excluded": stored["excluded"], "accounted_event_ids": stored["accounted_event_ids"],
        "inputs_used": stored["inputs_used"],
        "disclaimer": "Synthetic demo data. Labels are not authenticated identities. No payment was executed.",
    }


def get_snapshot(conn: sqlite3.Connection, period: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM period_snapshots WHERE period = ?", (period,)).fetchone()
    if row is None:
        return None
    ok = db.sha256_text(row["snapshot_json"]) == row["sha256"]
    snap = json.loads(row["snapshot_json"])
    snap["_integrity"] = {"sha256": row["sha256"], "verified": ok}
    return snap


# ------------------------------------------------------------------ decisions & adjustments

def _require_reason(reason: str | None) -> str:
    r = (reason or "").strip()
    if len(r) < MIN_REASON:
        raise WorkflowError("REASON_REQUIRED", f"a reason of at least {MIN_REASON} characters is required")
    return r


def decide_event(conn: sqlite3.Connection, event_id: str, decision: str, reason: str, actor: str) -> str:
    reason = _require_reason(reason)
    if decision not in ("POST_LATE", "EXCLUDE"):
        raise WorkflowError("BAD_DECISION", "decision must be POST_LATE or EXCLUDE")
    with db.tx(conn):
        ev = conn.execute("SELECT * FROM cash_events WHERE event_id = ?", (event_id,)).fetchone()
        if ev is None:
            raise WorkflowError("NOT_FOUND", f"event {event_id} does not exist")
        if conn.execute("SELECT 1 FROM event_decisions WHERE event_id = ?", (event_id,)).fetchone():
            raise WorkflowError("ALREADY_DECIDED", f"event {event_id} already has a decision (decisions are final)")
        inp = load_engine_input(conn)
        if any(event_id in fp.accounted_event_ids for fp in inp.closed.values()):
            raise WorkflowError("ALREADY_FINAL", f"event {event_id} is already inside a closed snapshot")
        ev_period = period_of(ev["event_date"])
        late = is_closed(conn, ev_period)
        first_open = first_open_period(conn)
        if decision == "POST_LATE":
            if not late:
                raise WorkflowError("NOT_LATE", f"event {event_id} belongs to open period {ev_period}; it is "
                                    "calculated normally")
            posting = first_open
        else:
            posting = first_open if late else ev_period
        conn.execute("INSERT INTO event_decisions(event_id, decision, posting_period, reason, actor, decided_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)", (event_id, decision, posting, reason, actor, db.now_iso()))
        _ensure_period(conn, posting)
        db.audit(conn, actor, f"DECIDE_{decision}", "cash_event", event_id,
                 {"posting_period": posting, "original_period": ev_period, "reason": reason})
        return posting


def add_adjustment(conn: sqlite3.Connection, period: str, rep_id: str, currency: str, amount_minor: int,
                   category: str, reason: str, actor: str, case_id: str | None = None) -> int:
    _require_period(period)
    reason = _require_reason(reason)
    if category not in ("COMMISSION_ADJUSTMENT", "PAYOUT_CORRECTION"):
        raise WorkflowError("BAD_CATEGORY", "category must be COMMISSION_ADJUSTMENT or PAYOUT_CORRECTION")
    if currency not in CURRENCIES:
        raise WorkflowError("BAD_CURRENCY", f"currency {currency!r} is not supported")
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor == 0:
        raise WorkflowError("BAD_AMOUNT", "adjustment amount must be a non-zero integer of minor units")
    with db.tx(conn):
        if is_closed(conn, period):
            raise WorkflowError("PERIOD_CLOSED", f"{period} is closed; post the adjustment in an open period")
        if conn.execute("SELECT 1 FROM reps WHERE rep_id = ?", (rep_id,)).fetchone() is None:
            raise WorkflowError("UNKNOWN_REP", f"rep {rep_id} is not in the roster")
        if case_id and conn.execute("SELECT 1 FROM variance_cases WHERE case_id = ?", (case_id,)).fetchone() is None:
            raise WorkflowError("UNKNOWN_CASE", f"case {case_id} does not exist")
        _ensure_period(conn, period)
        cur = conn.execute("INSERT INTO adjustments(period, rep_id, currency, amount_minor, category, reason, case_id,"
                           " actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           (period, rep_id, currency, amount_minor, category, reason, case_id, actor, db.now_iso()))
        adj_id = int(cur.lastrowid)
        db.audit(conn, actor, "ADD_ADJUSTMENT", "adjustment", adj_id,
                 {"period": period, "rep_id": rep_id, "currency": currency, "amount_minor": amount_minor,
                  "category": category, "reason": reason, "case_id": case_id})
        if case_id:
            conn.execute("INSERT INTO case_notes(case_id, kind, author, body, created_at) VALUES (?, 'SYSTEM', ?, ?, ?)",
                         (case_id, actor, f"Adjustment #{adj_id} ({category}, {amount_minor} minor units) posted in "
                                          f"{period}. Posting an adjustment does not pay anyone.", db.now_iso()))
        return adj_id
