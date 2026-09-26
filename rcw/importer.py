"""CSV import with validation, provenance, idempotency and quarantine.

Guarantees
----------
* **File idempotency** — the SHA-256 of the raw bytes is the file identity. Re-submitting
  the same bytes (any filename) is a recorded no-op (`DUPLICATE_FILE`).
* **Row idempotency** — each row has a natural key (e.g. ``["E-1"]``). An identical row
  that already exists is skipped as `DUPLICATE_ROW`; a *different* row with an existing
  key is quarantined as `CONFLICT_EXISTING`. Accepted rows are never overwritten.
* **No silent partial success** — `strict` mode commits all rows or none; `quarantine`
  mode commits valid rows and stores every invalid row with reason codes, and the batch
  records control totals (rows and amounts per currency) that must reconcile.
* **Order independence** — rows are validated in a canonical order (business date, id),
  never file order; conflicting duplicates inside one file quarantine *all* copies.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

from . import db
from .money import CURRENCIES, MoneyFormatError, parse_amount, parse_percent_to_bps
from .periods_util import last_closed_period, month_end, period_of, period_start

ID_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,39}$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")  # ASCII digits only (D-001)
PERIOD_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ROWS = 200_000

FILE_KINDS: dict[str, list[str]] = {
    "reps": ["rep_id", "display_name", "team"],
    "contracts": ["contract_id", "account_id", "account_name", "currency", "booking_date", "booked_amount", "product"],
    "splits": ["contract_id", "rep_id", "split_pct"],
    "plans": ["plan_id", "version", "currency", "effective_from", "threshold_amount", "base_rate_pct",
              "accelerator_rate_pct", "description"],
    "assignments": ["assignment_id", "rep_id", "plan_id", "effective_from", "effective_to"],
    "cash_events": ["event_id", "event_type", "contract_id", "event_date", "currency", "amount",
                    "original_event_id", "invoice_ref", "memo"],
    "recorded_payouts": ["record_id", "rep_id", "period", "currency", "amount", "source_system", "memo"],
}
KIND_LABELS = {
    "reps": "Rep roster", "contracts": "Contracts / bookings", "splits": "Credit splits",
    "plans": "Commission plan versions", "assignments": "Plan assignments",
    "cash_events": "Cash collections & refunds", "recorded_payouts": "Recorded payout register",
}
# Import order that satisfies references (used by the demo loader and docs).
KIND_ORDER = ["reps", "plans", "assignments", "contracts", "splits", "cash_events", "recorded_payouts"]


@dataclass
class RowResult:
    source_row: int
    raw: dict[str, str]
    record: dict[str, Any] | None = None
    key: tuple | None = None
    reasons: list[dict[str, str]] = field(default_factory=list)
    outcome: str = "PENDING"          # ACCEPTED | DUPLICATE_ROW | QUARANTINED
    row_hash: str | None = None
    dup_of: "RowResult | None" = None  # in-file copy of another row

    def fail(self, code: str, message: str) -> None:
        if not any(r["code"] == code for r in self.reasons):
            self.reasons.append({"code": code, "message": message})

    @property
    def ok(self) -> bool:
        return not self.reasons and self.outcome == "PENDING"


@dataclass
class ImportResult:
    batch_id: int
    kind: str
    filename: str
    sha256: str
    mode: str
    status: str
    rows_read: int = 0
    accepted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    file_errors: list[dict[str, str]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    control: dict[str, Any] = field(default_factory=dict)
    duplicate_of: int | None = None

    @property
    def committed(self) -> bool:
        return self.status in ("COMMITTED", "COMMITTED_WITH_QUARANTINE")

    def summary(self) -> str:
        if self.status == "DUPLICATE_FILE":
            return f"{self.kind}: duplicate file (same SHA-256 as batch #{self.duplicate_of}) - nothing imported"
        if self.status == "REJECTED":
            errs = self.file_errors or [r for r in self.rows if r["outcome"] != "ACCEPTED"]
            return f"{self.kind}: REJECTED - {len(errs)} problem(s); nothing committed"
        return (f"{self.kind}: {self.accepted} accepted, {self.duplicates} duplicate, "
                f"{self.quarantined} quarantined of {self.rows_read} rows ({self.status})")


# ------------------------------------------------------------------ field parsers

def _id(row: RowResult, col: str, value: str, *, required: bool = True) -> str | None:
    v = (value or "").strip()
    if not v:
        if required:
            row.fail("MISSING_VALUE", f"{col} is required")
        return None
    if not ID_RE.fullmatch(v):
        row.fail("ID_FORMAT", f"{col} {v!r} must be 1-40 chars of A-Z, 0-9, '_' or '-' (uppercase, no spaces)")
        return None
    return v


def _text(row: RowResult, col: str, value: str, max_len: int, *, required: bool = True) -> str:
    v = (value or "").strip()
    if required and not v:
        row.fail("MISSING_VALUE", f"{col} is required")
    if len(v) > max_len:
        row.fail("TEXT_TOO_LONG", f"{col} longer than {max_len} characters")
    if any(ord(ch) < 32 for ch in v):
        row.fail("CONTROL_CHARACTER", f"{col} contains control characters")
    return v


def _date(row: RowResult, col: str, value: str, *, required: bool = True) -> date | None:
    v = (value or "").strip()
    if not v:
        if required:
            row.fail("MISSING_VALUE", f"{col} is required")
        return None
    if not DATE_RE.fullmatch(v):
        row.fail("DATE_FORMAT", f"{col} {v!r} must be an ISO date YYYY-MM-DD")
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        row.fail("DATE_FORMAT", f"{col} {v!r} is not a real calendar date")
        return None


def _currency(row: RowResult, value: str) -> str | None:
    v = (value or "").strip()
    if v not in CURRENCIES:
        row.fail("CURRENCY_UNSUPPORTED", f"currency {v!r} is not supported (supported: {', '.join(CURRENCIES)})")
        return None
    return v


def _amount(row: RowResult, col: str, value: str, *, allow_negative: bool = False, positive: bool = True) -> int | None:
    try:
        amt = parse_amount(value, allow_negative=allow_negative)
    except MoneyFormatError as exc:
        row.fail("AMOUNT_FORMAT", f"{col}: {exc}")
        return None
    if positive and amt <= 0:
        # keep the parsed value so control totals still count it (the row is quarantined anyway)
        row.fail("AMOUNT_NOT_POSITIVE", f"{col} must be greater than zero")
    return amt


def _pct(row: RowResult, col: str, value: str) -> int | None:
    try:
        return parse_percent_to_bps(value)
    except MoneyFormatError as exc:
        row.fail("PERCENT_FORMAT", f"{col}: {exc}")
        return None


# ------------------------------------------------------------------ context

class Ctx:
    """Reference data visible to validation (DB state + rows accepted earlier in this file)."""

    def __init__(self, conn: sqlite3.Connection, business_date: date):
        self.conn = conn
        self.business_date = business_date
        self.last_closed = last_closed_period(conn)
        self.reps = {r["rep_id"] for r in conn.execute("SELECT rep_id FROM reps")}
        self.contracts = {r["contract_id"]: dict(r) for r in conn.execute("SELECT * FROM contracts")}
        self.plan_ids = {r["plan_id"] for r in conn.execute("SELECT DISTINCT plan_id FROM plan_versions")}

    def closed_through(self) -> date | None:
        return month_end(self.last_closed) if self.last_closed else None


# ------------------------------------------------------------------ per-kind row validators

def _v_reps(row: RowResult, ctx: Ctx) -> None:
    rid = _id(row, "rep_id", row.raw["rep_id"])
    name = _text(row, "display_name", row.raw["display_name"], 80)
    team = _text(row, "team", row.raw["team"], 60)
    row.key = (rid,) if rid else None
    row.record = {"rep_id": rid, "display_name": name, "team": team}


def _v_contracts(row: RowResult, ctx: Ctx) -> None:
    cid = _id(row, "contract_id", row.raw["contract_id"])
    aid = _id(row, "account_id", row.raw["account_id"])
    aname = _text(row, "account_name", row.raw["account_name"], 120)
    ccy = _currency(row, row.raw["currency"])
    bdate = _date(row, "booking_date", row.raw["booking_date"])
    booked = _amount(row, "booked_amount", row.raw["booked_amount"])
    product = _text(row, "product", row.raw["product"], 80)
    row.key = (cid,) if cid else None
    row.record = {"contract_id": cid, "account_id": aid, "account_name": aname, "currency": ccy,
                  "booking_date": bdate.isoformat() if bdate else None, "booked_amount_minor": booked,
                  "product": product}
    row.currency, row.amount = ccy, booked  # type: ignore[attr-defined]


def _v_splits(row: RowResult, ctx: Ctx) -> None:
    cid = _id(row, "contract_id", row.raw["contract_id"])
    rid = _id(row, "rep_id", row.raw["rep_id"])
    bps = _pct(row, "split_pct", row.raw["split_pct"])
    if cid and cid not in ctx.contracts:
        row.fail("UNKNOWN_CONTRACT", f"contract {cid} has not been imported")
    if rid and rid not in ctx.reps:
        row.fail("UNKNOWN_REP", f"rep {rid} is not in the rep roster")
    if bps is not None and bps <= 0:
        row.fail("PERCENT_FORMAT", "split_pct must be greater than zero")
    row.key = (cid, rid) if cid and rid else None
    row.record = {"contract_id": cid, "rep_id": rid, "split_bps": bps}


def _v_plans(row: RowResult, ctx: Ctx) -> None:
    pid = _id(row, "plan_id", row.raw["plan_id"])
    ver_txt = (row.raw["version"] or "").strip()
    version = int(ver_txt) if re.fullmatch(r"[0-9]{1,4}", ver_txt) and int(ver_txt) >= 1 else None
    if version is None:
        row.fail("VERSION_FORMAT", f"version {ver_txt!r} must be a whole number 1-9999")
    ccy = _currency(row, row.raw["currency"])
    eff = _date(row, "effective_from", row.raw["effective_from"])
    if eff and eff.day != 1:
        row.fail("EFFECTIVE_DATE_NOT_MONTH_START", "plan versions must start on the 1st day of a month")
    threshold = _amount(row, "threshold_amount", row.raw["threshold_amount"], positive=False)
    base = _pct(row, "base_rate_pct", row.raw["base_rate_pct"])
    accel = _pct(row, "accelerator_rate_pct", row.raw["accelerator_rate_pct"])
    desc = _text(row, "description", row.raw["description"], 200, required=False)
    closed_through = ctx.closed_through()
    if eff and closed_through and eff <= closed_through:
        row.fail("RETROACTIVE_PLAN", f"effective_from {eff} falls in or before closed period {ctx.last_closed}; "
                 "closed periods are immutable - post an adjustment instead")
    row.key = (pid, version, ccy) if pid and version and ccy else None
    row.record = {"plan_id": pid, "version": version, "currency": ccy,
                  "effective_from": eff.isoformat() if eff else None, "threshold_minor": threshold,
                  "base_rate_bps": base, "accel_rate_bps": accel, "description": desc}


def _v_assignments(row: RowResult, ctx: Ctx) -> None:
    aid = _id(row, "assignment_id", row.raw["assignment_id"])
    rid = _id(row, "rep_id", row.raw["rep_id"])
    pid = _id(row, "plan_id", row.raw["plan_id"])
    eff = _date(row, "effective_from", row.raw["effective_from"])
    to = _date(row, "effective_to", row.raw["effective_to"], required=False)
    if rid and rid not in ctx.reps:
        row.fail("UNKNOWN_REP", f"rep {rid} is not in the rep roster")
    if pid and pid not in ctx.plan_ids:
        row.fail("UNKNOWN_PLAN", f"plan {pid} has no imported versions")
    if eff and eff.day != 1:
        row.fail("EFFECTIVE_DATE_NOT_MONTH_START", "assignments must start on the 1st day of a month")
    if to and (to != month_end(period_of(to)) or (eff and to < eff)):
        row.fail("EFFECTIVE_TO_INVALID", "effective_to must be blank or a month-end date on/after effective_from")
    closed_through = ctx.closed_through()
    if eff and closed_through and eff <= closed_through:
        row.fail("RETROACTIVE_ASSIGNMENT", f"effective_from {eff} falls in or before closed period {ctx.last_closed}")
    row.key = (aid,) if aid else None
    row.record = {"assignment_id": aid, "rep_id": rid, "plan_id": pid,
                  "effective_from": eff.isoformat() if eff else None,
                  "effective_to": to.isoformat() if to else None}


def _v_cash(row: RowResult, ctx: Ctx) -> None:
    eid = _id(row, "event_id", row.raw["event_id"])
    etype = (row.raw["event_type"] or "").strip()
    if etype not in ("COLLECTION", "REFUND"):
        row.fail("EVENT_TYPE", f"event_type {etype!r} must be COLLECTION or REFUND")
    cid = _id(row, "contract_id", row.raw["contract_id"])
    edate = _date(row, "event_date", row.raw["event_date"])
    ccy = _currency(row, row.raw["currency"])
    amt = _amount(row, "amount", row.raw["amount"])
    orig_txt = (row.raw["original_event_id"] or "").strip()
    orig = None
    if etype == "REFUND":
        if not orig_txt:
            row.fail("MISSING_ORIGINAL", "a REFUND must name the original_event_id it returns")
        else:
            orig = _id(row, "original_event_id", orig_txt)
    elif etype == "COLLECTION" and orig_txt:
        row.fail("UNEXPECTED_ORIGINAL", "a COLLECTION must leave original_event_id blank")
    invoice_ref = _text(row, "invoice_ref", row.raw["invoice_ref"], 60, required=False)
    memo = _text(row, "memo", row.raw["memo"], 200, required=False)
    contract = ctx.contracts.get(cid) if cid else None
    if cid and contract is None:
        row.fail("UNKNOWN_CONTRACT", f"contract {cid} has not been imported")
    if contract and ccy and contract["currency"] != ccy:
        row.fail("CURRENCY_MISMATCH", f"cash currency {ccy} differs from contract {cid} currency {contract['currency']}")
    if edate and edate > ctx.business_date:
        row.fail("FUTURE_DATED", f"event_date {edate} is after the workbench business date {ctx.business_date}")
    row.key = (eid,) if eid else None
    row.record = {"event_id": eid, "event_type": etype, "contract_id": cid,
                  "event_date": edate.isoformat() if edate else None, "currency": ccy, "amount_minor": amt,
                  "original_event_id": orig, "invoice_ref": invoice_ref, "memo": memo}
    row.currency, row.amount, row.biz_date = ccy, amt, (edate.isoformat() if edate else None)  # type: ignore[attr-defined]


def _v_payouts(row: RowResult, ctx: Ctx) -> None:
    rec = _id(row, "record_id", row.raw["record_id"])
    rid = _id(row, "rep_id", row.raw["rep_id"])
    per = (row.raw["period"] or "").strip()
    if not PERIOD_RE.fullmatch(per):
        row.fail("PERIOD_FORMAT", f"period {per!r} must be YYYY-MM")
        per = None
    ccy = _currency(row, row.raw["currency"])
    amt = _amount(row, "amount", row.raw["amount"], allow_negative=True, positive=False)
    if amt == 0:
        row.fail("AMOUNT_ZERO", "a recorded payout of zero carries no information")
    src = _text(row, "source_system", row.raw["source_system"], 40)
    memo = _text(row, "memo", row.raw["memo"], 200, required=False)
    if rid and rid not in ctx.reps:
        row.fail("UNKNOWN_REP", f"rep {rid} is not in the rep roster")
    if per and period_start(per) > ctx.business_date:
        row.fail("FUTURE_DATED", f"period {per} starts after the business date {ctx.business_date}")
    row.key = (rec,) if rec else None
    row.record = {"record_id": rec, "rep_id": rid, "period": per, "currency": ccy, "amount_minor": amt,
                  "source_system": src, "memo": memo}
    row.currency, row.amount = ccy, amt  # type: ignore[attr-defined]


VALIDATORS: dict[str, Callable[[RowResult, Ctx], None]] = {
    "reps": _v_reps, "contracts": _v_contracts, "splits": _v_splits, "plans": _v_plans,
    "assignments": _v_assignments, "cash_events": _v_cash, "recorded_payouts": _v_payouts,
}

TABLES = {
    "reps": ("reps", ["rep_id"]),
    "contracts": ("contracts", ["contract_id"]),
    "splits": ("splits", ["contract_id", "rep_id"]),
    "plans": ("plan_versions", ["plan_id", "version", "currency"]),
    "assignments": ("plan_assignments", ["assignment_id"]),
    "cash_events": ("cash_events", ["event_id"]),
    "recorded_payouts": ("recorded_payouts", ["record_id"]),
}


def _sort_key(row: RowResult) -> tuple:
    """Canonical processing order: business date (if any), then natural key, then content hash."""
    biz = getattr(row, "biz_date", None) or ""
    key = [str(k) for k in (row.key or ())]
    return (biz, key, row.row_hash or "", row.source_row)


def _insert_key(row: RowResult) -> tuple:
    is_refund = bool(row.record) and row.record.get("event_type") == "REFUND"
    return (is_refund, _sort_key(row))


# ------------------------------------------------------------------ group checks (need all rows)

def _group_contracts(rows: list[RowResult], ctx: Ctx) -> None:
    names: dict[str, set[str]] = {}
    for r in rows:
        if r.ok and r.record["account_id"]:
            names.setdefault(r.record["account_id"], set()).add(r.record["account_name"])
    existing = {}
    for c in ctx.contracts.values():
        existing.setdefault(c["account_id"], c["account_name"])
    for r in rows:
        if not r.ok:
            continue
        aid = r.record["account_id"]
        seen = names.get(aid, set()) | ({existing[aid]} if aid in existing else set())
        if len(seen) > 1:
            r.fail("ACCOUNT_NAME_CONFLICT", f"account {aid} appears with different names {sorted(seen)}; "
                   "an account id must identify exactly one account")


def _group_splits(rows: list[RowResult], ctx: Ctx) -> None:
    existing: dict[str, int] = {}
    for rr in ctx.conn.execute("SELECT contract_id, SUM(split_bps) AS s FROM splits GROUP BY contract_id"):
        existing[rr["contract_id"]] = rr["s"]
    by_contract: dict[str, list[RowResult]] = {}
    for r in rows:
        cid = r.record.get("contract_id") if r.record else None
        if cid and r.outcome != "DUPLICATE_ROW":
            by_contract.setdefault(cid, []).append(r)
    for cid, group in by_contract.items():
        if cid in existing:
            for r in group:
                if r.ok:
                    r.fail("CONFLICT_EXISTING", f"contract {cid} already has a credit split; splits are immutable "
                           "once loaded (correct via adjustment)")
            continue
        if any(not r.ok for r in group):
            for r in group:
                if r.ok:
                    r.fail("SPLIT_GROUP_INCOMPLETE", f"another split row for {cid} is invalid; the whole split "
                           "is held back so a partial split is never loaded")
            continue
        total = sum(r.record["split_bps"] for r in group)
        if total != 10_000:
            for r in group:
                r.fail("SPLIT_TOTAL_NOT_100", f"splits for {cid} total {total / 100:.2f}% (must be exactly 100.00%)")


def _group_plans(rows: list[RowResult], ctx: Ctx) -> None:
    existing = [dict(r) for r in ctx.conn.execute("SELECT plan_id, version, currency, effective_from FROM plan_versions")]
    candidates = [r for r in rows if r.ok]
    # same plan/currency/effective_from twice (in file or vs DB) is ambiguous
    for r in candidates:
        rec = r.record
        clash_db = [e for e in existing if e["plan_id"] == rec["plan_id"] and e["currency"] == rec["currency"]
                    and e["effective_from"] == rec["effective_from"] and e["version"] != rec["version"]]
        clash_file = [o for o in candidates if o is not r and o.record["plan_id"] == rec["plan_id"]
                      and o.record["currency"] == rec["currency"] and o.record["effective_from"] == rec["effective_from"]
                      and o.record["version"] != rec["version"]]
        if clash_db or clash_file:
            r.fail("PLAN_DATE_CONFLICT", "two plan versions start on the same date for this plan and currency")
    # version order must match effective date order
    for r in [c for c in candidates if c.ok]:
        rec = r.record
        peers = [e for e in existing if e["plan_id"] == rec["plan_id"] and e["currency"] == rec["currency"]]
        peers += [o.record for o in candidates if o is not r and o.ok and o.record["plan_id"] == rec["plan_id"]
                  and o.record["currency"] == rec["currency"]]
        for p in peers:
            if p["version"] == rec["version"]:
                continue
            if (p["version"] < rec["version"]) != (p["effective_from"] < rec["effective_from"]):
                r.fail("PLAN_VERSION_ORDER", f"version {rec['version']} and version {p['version']} are not in "
                       "effective-date order")


def _group_assignments(rows: list[RowResult], ctx: Ctx) -> None:
    existing = [dict(r) for r in ctx.conn.execute("SELECT assignment_id, rep_id, effective_from FROM plan_assignments")]
    candidates = [r for r in rows if r.ok]
    for r in candidates:
        rec = r.record
        clash = [e for e in existing if e["rep_id"] == rec["rep_id"] and e["effective_from"] == rec["effective_from"]
                 and e["assignment_id"] != rec["assignment_id"]]
        clash += [o for o in candidates if o is not r and o.record["rep_id"] == rec["rep_id"]
                  and o.record["effective_from"] == rec["effective_from"]]
        if clash:
            r.fail("ASSIGNMENT_AMBIGUOUS", f"rep {rec['rep_id']} would have two plan assignments starting "
                   f"{rec['effective_from']}; the effective plan would be ambiguous")


def _group_cash(rows: list[RowResult], ctx: Ctx) -> None:
    conn = ctx.conn
    # every still-valid row of this file, whatever its type (so a refund of a refund is named as such)
    valid_in_file = {r.record["event_id"]: r for r in rows if r.ok}
    quarantined_ids = {r.record["event_id"] for r in rows
                       if r.record and r.record.get("event_id") and r.reasons}
    refunds = sorted((r for r in rows if r.ok and r.record["event_type"] == "REFUND"), key=_sort_key)
    refunded_so_far: dict[str, int] = {}
    for r in refunds:
        rec = r.record
        oid = rec["original_event_id"]
        original = None
        db_row = conn.execute("SELECT * FROM cash_events WHERE event_id = ?", (oid,)).fetchone()
        if db_row is not None:
            original = dict(db_row)
        elif oid in valid_in_file and valid_in_file[oid] is not r:
            original = valid_in_file[oid].record
        if original is None:
            extra = " (it is quarantined in this file)" if oid in quarantined_ids else ""
            r.fail("UNKNOWN_ORIGINAL_EVENT", f"original event {oid} is not an imported collection{extra}")
            continue
        if original["event_type"] != "COLLECTION":
            r.fail("ORIGINAL_NOT_COLLECTION", f"original event {oid} is a {original['event_type']}, not a COLLECTION")
            continue
        if original["contract_id"] != rec["contract_id"]:
            r.fail("REFUND_CONTRACT_MISMATCH", f"refund contract {rec['contract_id']} differs from the original "
                   f"collection's contract {original['contract_id']}")
        if original["currency"] != rec["currency"]:
            r.fail("CURRENCY_MISMATCH", f"refund currency {rec['currency']} differs from original {original['currency']}")
        if rec["event_date"] < original["event_date"]:
            r.fail("REFUND_BEFORE_COLLECTION", f"refund dated {rec['event_date']} precedes collection "
                   f"{oid} dated {original['event_date']}")
        if not r.ok:
            continue
        if oid not in refunded_so_far:
            prior = conn.execute("SELECT COALESCE(SUM(amount_minor), 0) AS s FROM cash_events "
                                 "WHERE original_event_id = ?", (oid,)).fetchone()["s"]
            refunded_so_far[oid] = prior
        if refunded_so_far[oid] + rec["amount_minor"] > original["amount_minor"]:
            r.fail("OVER_REFUND", f"cumulative refunds on {oid} would be {refunded_so_far[oid] + rec['amount_minor']} "
                   f"minor units, above the collected {original['amount_minor']}")
            continue
        refunded_so_far[oid] += rec["amount_minor"]


GROUP_CHECKS: dict[str, Callable[[list[RowResult], Ctx], None]] = {
    "contracts": _group_contracts, "splits": _group_splits, "plans": _group_plans,
    "assignments": _group_assignments, "cash_events": _group_cash,
}


# ------------------------------------------------------------------ main entry point

def _business_date(conn: sqlite3.Connection) -> date:
    val = db.get_setting(conn, "business_date")
    return date.fromisoformat(val) if val else date.today()


def _row_hash(kind: str, record: dict[str, Any]) -> str:
    return db.sha256_json({"kind": kind, "record": record})


def _decode(data: bytes) -> str:
    text = data.decode("utf-8")
    return text[1:] if text.startswith("﻿") else text


def import_csv(conn: sqlite3.Connection, kind: str, filename: str, data: bytes, *,
               mode: str = "quarantine", actor: str = "analyst-1") -> ImportResult:
    if kind not in FILE_KINDS:
        raise ValueError(f"unknown file kind {kind!r}; expected one of {sorted(FILE_KINDS)}")
    if mode not in ("strict", "quarantine"):
        raise ValueError("mode must be 'strict' or 'quarantine'")
    sha = hashlib.sha256(data).hexdigest()
    safe_name = (filename or "upload.csv").replace("\\", "/").split("/")[-1][:120] or "upload.csv"

    with db.tx(conn):
        dup = conn.execute("SELECT batch_id FROM import_batches WHERE sha256 = ? AND status IN "
                           "('COMMITTED','COMMITTED_WITH_QUARANTINE')", (sha,)).fetchone()
        if dup:
            batch_id = _insert_batch(conn, kind, safe_name, sha, len(data), mode, "DUPLICATE_FILE", actor,
                                     duplicate_of=dup["batch_id"])
            db.audit(conn, actor, "IMPORT_DUPLICATE_FILE", "import_batch", batch_id,
                     {"kind": kind, "filename": safe_name, "sha256": sha, "duplicate_of": dup["batch_id"]})
            return ImportResult(batch_id, kind, safe_name, sha, mode, "DUPLICATE_FILE", duplicate_of=dup["batch_id"])

        file_errors, rows = _parse(kind, data)
        result = ImportResult(0, kind, safe_name, sha, mode, "PENDING", rows_read=len(rows))
        if file_errors:
            result.status, result.file_errors = "REJECTED", file_errors
            result.batch_id = _insert_batch(conn, kind, safe_name, sha, len(data), mode, "REJECTED", actor,
                                            file_errors=file_errors, rows_read=len(rows))
            db.audit(conn, actor, "IMPORT_REJECTED", "import_batch", result.batch_id,
                     {"kind": kind, "filename": safe_name, "sha256": sha, "file_errors": file_errors})
            return result

        ctx = Ctx(conn, _business_date(conn))
        validate = VALIDATORS[kind]
        for r in rows:
            validate(r, ctx)
            if r.record is not None:
                r.row_hash = _row_hash(kind, r.record)
        rows_sorted = sorted(rows, key=_sort_key)

        # duplicates / conflicts inside the file
        by_key: dict[tuple, list[RowResult]] = {}
        for r in rows_sorted:
            if r.key is not None:
                by_key.setdefault(r.key, []).append(r)
        for key, group in by_key.items():
            if len(group) < 2:
                continue
            hashes = {r.row_hash for r in group}
            if len(hashes) > 1:
                for r in group:
                    r.fail("CONFLICT_IN_FILE", f"key {list(key)} appears {len(group)} times in this file with "
                           "different values; none is loaded")
            elif not group[0].reasons:
                for r in group[1:]:
                    r.outcome = "DUPLICATE_ROW"
                    r.dup_of = group[0]

        # duplicates / conflicts against existing rows
        table, key_cols = TABLES[kind]
        where = " AND ".join(f"{c} = ?" for c in key_cols)
        for r in rows_sorted:
            if r.key is None or r.outcome == "DUPLICATE_ROW":
                continue
            existing = conn.execute(f"SELECT row_hash, batch_id FROM {table} WHERE {where}", r.key).fetchone()
            if existing is None:
                continue
            if existing["row_hash"] == r.row_hash and not r.reasons:
                r.outcome = "DUPLICATE_ROW"
            elif existing["row_hash"] == r.row_hash:
                r.outcome = "DUPLICATE_ROW"   # identical to an accepted row: skip, no new error
                r.reasons.clear()
            else:
                r.fail("CONFLICT_EXISTING", f"key {list(r.key)} already loaded from batch #{existing['batch_id']} "
                       "with different values; the existing row is kept unchanged")

        if kind in GROUP_CHECKS:
            GROUP_CHECKS[kind](rows_sorted, ctx)

        for r in rows_sorted:
            if r.outcome == "DUPLICATE_ROW":
                continue
            r.outcome = "QUARANTINED" if r.reasons else "ACCEPTED"
        for r in rows_sorted:  # an in-file copy of a row that ended up quarantined is quarantined too
            if r.dup_of is not None and r.dup_of.outcome == "QUARANTINED":
                r.outcome = "QUARANTINED"
                r.fail("DUPLICATE_OF_QUARANTINED_ROW", f"identical to row {r.dup_of.source_row}, which is quarantined")

        result.accepted = sum(1 for r in rows if r.outcome == "ACCEPTED")
        result.duplicates = sum(1 for r in rows if r.outcome == "DUPLICATE_ROW")
        result.quarantined = sum(1 for r in rows if r.outcome == "QUARANTINED")
        result.control = _control_totals(kind, rows)
        result.rows = [{"source_row": r.source_row, "key": list(r.key) if r.key else None, "outcome": r.outcome,
                        "reasons": r.reasons} for r in sorted(rows, key=lambda x: x.source_row)]

        if mode == "strict" and result.quarantined:
            result.status = "REJECTED"
            result.control["row_errors"] = [x for x in result.rows if x["outcome"] == "QUARANTINED"][:1000]
            result.batch_id = _insert_batch(conn, kind, safe_name, sha, len(data), mode, "REJECTED", actor,
                                            rows_read=len(rows), control=result.control)
            db.audit(conn, actor, "IMPORT_REJECTED", "import_batch", result.batch_id,
                     {"kind": kind, "filename": safe_name, "sha256": sha, "mode": mode,
                      "invalid_rows": result.quarantined})
            return result

        result.status = "COMMITTED_WITH_QUARANTINE" if result.quarantined else "COMMITTED"
        result.batch_id = _insert_batch(conn, kind, safe_name, sha, len(data), mode, result.status, actor,
                                        rows_read=len(rows), accepted=result.accepted,
                                        duplicates=result.duplicates, quarantined=result.quarantined,
                                        control=result.control)
        superseded = 0
        # Dependency-safe insertion (R-4): every COLLECTION before any REFUND, so a refund's foreign key to a
        # same-file collection always resolves. Financial evaluation does not depend on insertion order: the
        # engine re-sorts by (business date, id) and each row keeps its own source_row provenance.
        for r in sorted(rows, key=_insert_key):
            if r.outcome == "ACCEPTED":
                _insert_row(conn, kind, r, result.batch_id)
                superseded += _supersede(conn, kind, r, result.batch_id, actor)
            elif r.outcome == "QUARANTINED":
                conn.execute(
                    "INSERT INTO quarantine_rows(batch_id, file_kind, source_row, natural_key, raw_json, reasons_json,"
                    " event_date, currency, amount_minor) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.batch_id, kind, r.source_row, db.canonical_json(list(r.key) if r.key else None),
                     db.canonical_json(r.raw), db.canonical_json(r.reasons), getattr(r, "biz_date", None),
                     getattr(r, "currency", None), getattr(r, "amount", None)))
        result.control["quarantine_superseded"] = superseded
        db.audit(conn, actor, "IMPORT_COMMITTED", "import_batch", result.batch_id,
                 {"kind": kind, "filename": safe_name, "sha256": sha, "mode": mode, "accepted": result.accepted,
                  "duplicates": result.duplicates, "quarantined": result.quarantined,
                  "superseded_quarantine_rows": superseded})
        return result


def _parse(kind: str, data: bytes) -> tuple[list[dict[str, str]], list[RowResult]]:
    errors: list[dict[str, str]] = []
    if len(data) > MAX_FILE_BYTES:
        return [{"code": "FILE_TOO_LARGE", "message": f"file exceeds {MAX_FILE_BYTES // (1024 * 1024)} MB"}], []
    try:
        text = _decode(data)
    except UnicodeDecodeError:
        return [{"code": "ENCODING", "message": "file is not valid UTF-8"}], []
    if not text.strip():
        return [{"code": "EMPTY_FILE", "message": "file is empty"}], []
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except csv.Error as exc:
        return [{"code": "CSV_SYNTAX", "message": str(exc)}], []
    header = [h.strip() for h in header]
    expected = FILE_KINDS[kind]
    dups = sorted({h for h in header if header.count(h) > 1})
    if dups:
        errors.append({"code": "HEADER_DUPLICATE", "message": f"duplicate column(s) {dups}: ambiguous, file rejected"})
    missing = [c for c in expected if c not in header]
    if missing:
        errors.append({"code": "HEADER_MISSING", "message": f"missing column(s) {missing} for {kind}"})
    unknown = [h for h in header if h not in expected]
    if unknown:
        errors.append({"code": "HEADER_UNKNOWN", "message": f"unexpected column(s) {unknown} for {kind}"})
    if errors:
        return errors, []
    rows: list[RowResult] = []
    try:
        for values in reader:
            if not values or all(not v.strip() for v in values):
                continue  # blank line: nothing to import (counted nowhere, carries no data)
            if len(rows) >= MAX_ROWS:
                return [{"code": "TOO_MANY_ROWS", "message": f"more than {MAX_ROWS} rows"}], []
            r = RowResult(source_row=reader.line_num, raw={})
            if len(values) != len(header):
                r.raw = {h: (values[i] if i < len(values) else "") for i, h in enumerate(header)}
                r.fail("FIELD_COUNT", f"expected {len(header)} fields, found {len(values)}")
            else:
                r.raw = dict(zip(header, values))
            rows.append(r)
    except csv.Error as exc:
        return [{"code": "CSV_SYNTAX", "message": f"line {reader.line_num}: {exc}"}], []
    return [], rows


def _control_totals(kind: str, rows: list[RowResult]) -> dict[str, Any]:
    ctrl: dict[str, Any] = {
        "rows": {"read": len(rows),
                 "accepted": sum(1 for r in rows if r.outcome == "ACCEPTED"),
                 "duplicate": sum(1 for r in rows if r.outcome == "DUPLICATE_ROW"),
                 "quarantined": sum(1 for r in rows if r.outcome == "QUARANTINED")},
    }
    ctrl["rows"]["reconciles"] = ctrl["rows"]["read"] == (
        ctrl["rows"]["accepted"] + ctrl["rows"]["duplicate"] + ctrl["rows"]["quarantined"])
    if kind in ("cash_events", "recorded_payouts", "contracts"):
        # amounts_minor[currency][label] -> read/accepted/duplicate/quarantined (label = event type or 'amount')
        amounts: dict[str, dict[str, dict[str, Any]]] = {}
        unparsed = 0
        for r in rows:
            ccy, amt = getattr(r, "currency", None), getattr(r, "amount", None)
            if ccy is None or amt is None:
                unparsed += 1
                continue
            label = (r.record.get("event_type") or "?") if kind == "cash_events" else "amount"
            b = amounts.setdefault(ccy, {}).setdefault(label, {"read": 0, "accepted": 0, "duplicate": 0,
                                                                 "quarantined": 0})
            b["read"] += amt
            b[{"ACCEPTED": "accepted", "DUPLICATE_ROW": "duplicate", "QUARANTINED": "quarantined"}[r.outcome]] += amt
        for per_ccy in amounts.values():
            for b in per_ccy.values():
                b["reconciles"] = b["read"] == b["accepted"] + b["duplicate"] + b["quarantined"]
        ctrl["amounts_minor"] = amounts
        ctrl["rows_without_parseable_amount"] = unparsed
    return ctrl


def _insert_batch(conn, kind, filename, sha, size, mode, status, actor, *, duplicate_of=None, file_errors=None,
                  rows_read=0, accepted=0, duplicates=0, quarantined=0, control=None) -> int:
    cur = conn.execute(
        "INSERT INTO import_batches(file_kind, original_filename, sha256, size_bytes, mode, status, duplicate_of,"
        " rows_read, rows_accepted, rows_duplicate, rows_quarantined, file_errors_json, control_json, actor,"
        " imported_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, filename, sha, size, mode, status, duplicate_of, rows_read, accepted, duplicates, quarantined,
         db.canonical_json(file_errors or []), db.canonical_json(control or {}), actor, db.now_iso()))
    return int(cur.lastrowid)


def _insert_row(conn: sqlite3.Connection, kind: str, r: RowResult, batch_id: int) -> None:
    table, _ = TABLES[kind]
    rec = dict(r.record)
    rec.update({"batch_id": batch_id, "source_row": r.source_row, "row_hash": r.row_hash})
    cols = list(rec)
    conn.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                 [rec[c] for c in cols])


def _supersede(conn: sqlite3.Connection, kind: str, r: RowResult, batch_id: int, actor: str) -> int:
    key = db.canonical_json(list(r.key))
    cur = conn.execute(
        "UPDATE quarantine_rows SET status = 'SUPERSEDED', resolution = ?, resolved_by = ?, resolved_at = ?"
        " WHERE file_kind = ? AND natural_key = ? AND status = 'OPEN'",
        (f"valid row with the same key accepted in batch #{batch_id}", actor, db.now_iso(), kind, key))
    if cur.rowcount:
        db.audit(conn, actor, "QUARANTINE_SUPERSEDED", "quarantine", key,
                 {"kind": kind, "batch_id": batch_id, "rows": cur.rowcount})
    return cur.rowcount


def dismiss_quarantine_row(conn: sqlite3.Connection, q_id: int, reason: str, actor: str) -> None:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise ValueError("REASON_REQUIRED: explain the dismissal in at least 10 characters")
    with db.tx(conn):
        row = conn.execute("SELECT * FROM quarantine_rows WHERE q_id = ?", (q_id,)).fetchone()
        if row is None:
            raise ValueError("NOT_FOUND: quarantine row does not exist")
        if row["status"] != "OPEN":
            raise ValueError(f"NOT_OPEN: quarantine row is already {row['status']}")
        conn.execute("UPDATE quarantine_rows SET status = 'DISMISSED', resolution = ?, resolved_by = ?, "
                     "resolved_at = ? WHERE q_id = ?", (reason, actor, db.now_iso(), q_id))
        db.audit(conn, actor, "QUARANTINE_DISMISSED", "quarantine", q_id,
                 {"kind": row["file_kind"], "natural_key": json.loads(row["natural_key"]), "reason": reason})
