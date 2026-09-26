"""Read-only loaders: database rows -> engine inputs and view models."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .engine import (Adjustment, Assignment, CashEvent, Contract, Decision, EngineInput, FrozenPeriod,
                     PlanVersion)

LINE_COLUMNS = ["line_key", "line_type", "period", "original_period", "rep_id", "currency", "event_id",
                "original_event_id", "contract_id", "event_date", "plan_id", "plan_version", "split_bps",
                "credited_minor", "credit_reversed_minor", "base_portion_minor", "accel_portion_minor",
                "base_rate_bps", "accel_rate_bps", "exact_amount", "amount_minor"]


def line_from_row(row: sqlite3.Row) -> dict[str, Any]:
    d = {c: row[c] for c in LINE_COLUMNS}
    d["detail"] = json.loads(row["detail_json"])
    return d


def run_lines(conn: sqlite3.Connection, run_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM calc_lines WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
    return [line_from_row(r) for r in rows]


def load_closed(conn: sqlite3.Connection) -> dict[str, FrozenPeriod]:
    closed: dict[str, FrozenPeriod] = {}
    for p in conn.execute("SELECT period, closed_run_id FROM periods WHERE status = 'CLOSED' ORDER BY period"):
        run = conn.execute("SELECT result_json FROM calc_runs WHERE run_id = ?", (p["closed_run_id"],)).fetchone()
        result = json.loads(run["result_json"])
        closed[p["period"]] = FrozenPeriod(p["period"], run_lines(conn, p["closed_run_id"]),
                                           set(result["accounted_event_ids"]))
    return closed


def load_engine_input(conn: sqlite3.Connection) -> EngineInput:
    contracts = {r["contract_id"]: Contract(r["contract_id"], r["account_id"], r["account_name"], r["currency"],
                                            r["booking_date"], r["booked_amount_minor"], r["product"],
                                            r["batch_id"], r["source_row"])
                 for r in conn.execute("SELECT * FROM contracts")}
    splits: dict[str, list[tuple[str, int]]] = {}
    for r in conn.execute("SELECT contract_id, rep_id, split_bps FROM splits ORDER BY contract_id, rep_id"):
        splits.setdefault(r["contract_id"], []).append((r["rep_id"], r["split_bps"]))
    plans = [PlanVersion(r["plan_id"], r["version"], r["currency"], r["effective_from"], r["threshold_minor"],
                         r["base_rate_bps"], r["accel_rate_bps"], r["description"], r["batch_id"], r["source_row"])
             for r in conn.execute("SELECT * FROM plan_versions ORDER BY plan_id, currency, version")]
    assignments = [Assignment(r["assignment_id"], r["rep_id"], r["plan_id"], r["effective_from"], r["effective_to"],
                              r["batch_id"], r["source_row"])
                   for r in conn.execute("SELECT * FROM plan_assignments ORDER BY assignment_id")]
    events = {r["event_id"]: CashEvent(r["event_id"], r["event_type"], r["contract_id"], r["event_date"],
                                       r["currency"], r["amount_minor"], r["original_event_id"], r["invoice_ref"],
                                       r["memo"], r["batch_id"], r["source_row"], r["row_hash"])
              for r in conn.execute("SELECT * FROM cash_events")}
    decisions = {r["event_id"]: Decision(r["event_id"], r["decision"], r["posting_period"], r["reason"], r["actor"],
                                         r["decided_at"])
                 for r in conn.execute("SELECT * FROM event_decisions")}
    adjustments = [Adjustment(r["adj_id"], r["period"], r["rep_id"], r["currency"], r["amount_minor"], r["category"],
                              r["reason"], r["case_id"], r["actor"], r["created_at"])
                   for r in conn.execute("SELECT * FROM adjustments ORDER BY adj_id")]
    quarantined = []
    for r in conn.execute("SELECT * FROM quarantine_rows WHERE file_kind = 'cash_events' AND status = 'OPEN' "
                          "ORDER BY q_id"):
        quarantined.append({"q_id": r["q_id"], "batch_id": r["batch_id"], "source_row": r["source_row"],
                            "natural_key": json.loads(r["natural_key"]), "reasons": json.loads(r["reasons_json"]),
                            "event_date": r["event_date"], "currency": r["currency"],
                            "amount_minor": r["amount_minor"]})
    reps = {r["rep_id"]: dict(r) for r in conn.execute("SELECT rep_id, display_name, team FROM reps")}
    return EngineInput(contracts, splits, plans, assignments, events, decisions, adjustments, load_closed(conn),
                       quarantined, reps)


def rep_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["rep_id"]: r["display_name"] for r in conn.execute("SELECT rep_id, display_name FROM reps")}


def activity_periods(conn: sqlite3.Connection) -> list[str]:
    """Months with cash activity, adjustments or a stored period row, ascending."""
    ps = {r["p"] for r in conn.execute("SELECT DISTINCT substr(event_date, 1, 7) AS p FROM cash_events")}
    ps |= {r["period"] for r in conn.execute("SELECT DISTINCT period FROM adjustments")}
    ps |= {r["period"] for r in conn.execute("SELECT period FROM periods")}
    ps |= {r["posting_period"] for r in conn.execute("SELECT DISTINCT posting_period FROM event_decisions")}
    return sorted(p for p in ps if p)
