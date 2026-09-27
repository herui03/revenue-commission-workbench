"""Pure commission engine: no database, no clock, no randomness.

Given immutable inputs it returns the calculation for one period. The web UI, the CLI,
exports and the close workflow all call this one function.

Policy implemented (invented demo policy — see docs/03_policy_decision_log.md):

* Cash-based. A COLLECTION earns commission; its cash is credited to reps by the
  contract's split (largest-remainder cents, ties to the smaller rep id).
* Ladder per (period, rep, currency): gross positive credited collections, walked in
  (event_date, event_id) order. Credit up to the plan threshold earns the base rate,
  the excess earns the accelerator rate. One half-up rounding per line.
* A REFUND reverses the *stored* original earning line(s): after refunds totalling R of a
  collection of amount x, the cumulative reversal is half_up(E * R / x). Refunds never
  reduce attainment and never touch other deals.
* Periods whose month is <= the last closed month are frozen. Events dated there that are
  not in a snapshot are *late*: they wait on a blocking hold until a reviewer posts them
  into the first open month (tiered on the frozen ladder of their own month, at their own
  month's plan version) or excludes them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from .db import canonical_json, sha256_json
from .money import allocate_largest_remainder, cumulative_reversal, earning_for_portions, fraction_to_str
from .periods_util import next_period, period_of

ENGINE_VERSION = "rcw-engine/1.0"
KEY_SEP = ":"   # never allowed inside an ID (IDs are [A-Z0-9_-]), so keys cannot collide

EARNING, CLAWBACK = "EARNING", "CLAWBACK"
LATE_EARNING, LATE_CLAWBACK = "LATE_EARNING", "LATE_CLAWBACK"
MANUAL_ADJUSTMENT = "MANUAL_ADJUSTMENT"
LINE_TYPES = (EARNING, CLAWBACK, LATE_EARNING, LATE_CLAWBACK, MANUAL_ADJUSTMENT)

BLOCKING, INFO = "BLOCKING", "INFO"


class EngineError(ValueError):
    pass


def line_key(*parts: str) -> str:
    for p in parts:
        if KEY_SEP in str(p):
            raise EngineError(f"key part {p!r} contains the reserved separator")
    return KEY_SEP.join(str(p) for p in parts)


# ------------------------------------------------------------------ inputs

@dataclass(frozen=True)
class CashEvent:
    event_id: str
    event_type: str
    contract_id: str
    event_date: str
    currency: str
    amount_minor: int
    original_event_id: str | None = None
    invoice_ref: str = ""
    memo: str = ""
    batch_id: int = 0
    source_row: int = 0
    row_hash: str = ""

    @property
    def period(self) -> str:
        return period_of(self.event_date)


@dataclass(frozen=True)
class Contract:
    contract_id: str
    account_id: str
    account_name: str
    currency: str
    booking_date: str
    booked_amount_minor: int
    product: str = ""
    batch_id: int = 0
    source_row: int = 0


@dataclass(frozen=True)
class PlanVersion:
    plan_id: str
    version: int
    currency: str
    effective_from: str
    threshold_minor: int
    base_rate_bps: int
    accel_rate_bps: int
    description: str = ""
    batch_id: int = 0
    source_row: int = 0


@dataclass(frozen=True)
class Assignment:
    assignment_id: str
    rep_id: str
    plan_id: str
    effective_from: str
    effective_to: str | None = None
    batch_id: int = 0
    source_row: int = 0


@dataclass(frozen=True)
class Decision:
    event_id: str
    decision: str            # POST_LATE | EXCLUDE
    posting_period: str
    reason: str
    actor: str = ""
    decided_at: str = ""


@dataclass(frozen=True)
class Adjustment:
    adj_id: int
    period: str
    rep_id: str
    currency: str
    amount_minor: int
    category: str
    reason: str
    case_id: str | None = None
    actor: str = ""
    created_at: str = ""


@dataclass
class FrozenPeriod:
    period: str
    lines: list[dict[str, Any]]
    accounted_event_ids: set[str]


@dataclass
class EngineInput:
    contracts: dict[str, Contract]
    splits: dict[str, list[tuple[str, int]]]
    plan_versions: list[PlanVersion]
    assignments: list[Assignment]
    events: dict[str, CashEvent]
    decisions: dict[str, Decision] = field(default_factory=dict)
    adjustments: list[Adjustment] = field(default_factory=list)
    closed: dict[str, FrozenPeriod] = field(default_factory=dict)
    quarantined_cash: list[dict[str, Any]] = field(default_factory=list)
    reps: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class PeriodResult:
    period: str
    lines: list[dict[str, Any]]
    holds: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    totals: list[dict[str, Any]]
    controls: list[dict[str, Any]]
    kpis: dict[str, dict[str, int]]
    accounted_event_ids: list[str]
    inputs_used: dict[str, Any]
    result_digest: str
    first_open_period: str | None
    engine_version: str = ENGINE_VERSION

    @property
    def blocking_holds(self) -> list[dict[str, Any]]:
        return [h for h in self.holds if h["severity"] == BLOCKING]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "period": self.period, "engine_version": self.engine_version, "result_digest": self.result_digest,
            "holds": self.holds, "excluded": self.excluded, "totals": self.totals, "controls": self.controls,
            "kpis": self.kpis, "accounted_event_ids": self.accounted_event_ids, "inputs_used": self.inputs_used,
            "first_open_period": self.first_open_period,
        }


# ------------------------------------------------------------------ helpers

def _resolve_assignment(assignments: list[Assignment], rep_id: str, on: str) -> tuple[Assignment | None, str | None]:
    """`assignments` is the rep's own list (pre-indexed by rep id in `calculate`)."""
    cands = [a for a in assignments if a.rep_id == rep_id and a.effective_from <= on
             and (a.effective_to is None or on <= a.effective_to)]
    if not cands:
        return None, "NO_ASSIGNMENT"
    latest = max(a.effective_from for a in cands)
    top = [a for a in cands if a.effective_from == latest]
    if len(top) > 1:
        return None, "AMBIGUOUS_ASSIGNMENT"
    return top[0], None


def _resolve_plan(versions: list[PlanVersion], plan_id: str, currency: str, on: str) -> PlanVersion | None:
    cands = [v for v in versions if v.plan_id == plan_id and v.currency == currency and v.effective_from <= on]
    if not cands:
        return None
    return max(cands, key=lambda v: (v.effective_from, v.version))


def _hold(code: str, message: str, *, severity: str = BLOCKING, event: CashEvent | None = None,
          rep_id: str | None = None, amount: int | None = None, currency: str | None = None,
          extra: dict[str, Any] | None = None) -> dict[str, Any]:
    h = {"code": code, "severity": severity, "message": message,
         "event_id": event.event_id if event else None, "rep_id": rep_id,
         "currency": currency or (event.currency if event else None),
         "amount_minor": amount if amount is not None else (event.amount_minor if event else None),
         "event_date": event.event_date if event else None}
    if extra:
        h.update(extra)
    return h


def _event_ref(ev: CashEvent) -> dict[str, Any]:
    return {"event_id": ev.event_id, "event_type": ev.event_type, "contract_id": ev.contract_id,
            "event_date": ev.event_date, "currency": ev.currency, "amount_minor": ev.amount_minor,
            "original_event_id": ev.original_event_id, "invoice_ref": ev.invoice_ref, "memo": ev.memo,
            "batch_id": ev.batch_id, "source_row": ev.source_row, "row_hash": ev.row_hash}


@dataclass
class _Item:
    event: CashEvent
    posting_period: str
    original_period: str
    late: bool

    @property
    def order(self) -> tuple:
        return (self.posting_period, self.event.event_date, self.event.event_id)


# ------------------------------------------------------------------ main calculation

def calculate(period: str, inp: EngineInput) -> PeriodResult:
    if period in inp.closed:
        raise EngineError(f"PERIOD_CLOSED: {period} is closed; read its snapshot instead")
    last_closed = max(inp.closed) if inp.closed else None
    if last_closed and period <= last_closed:
        raise EngineError(f"PERIOD_CLOSED: {period} is on or before the last closed period {last_closed}")
    first_open = next_period(last_closed) if last_closed else None

    accounted_frozen: set[str] = set()
    frozen_lines: list[dict[str, Any]] = []
    for fp in inp.closed.values():
        accounted_frozen |= fp.accounted_event_ids
        frozen_lines.extend(fp.lines)

    # ---- classify every event not already inside a closed snapshot
    items: list[_Item] = []
    pending_late: list[CashEvent] = []
    excluded: list[dict[str, Any]] = []
    excluded_ids: set[str] = set()
    for ev in inp.events.values():
        if ev.event_id in accounted_frozen:
            continue
        dec = inp.decisions.get(ev.event_id)
        is_late = last_closed is not None and ev.period <= last_closed
        if dec and dec.decision == "EXCLUDE":
            excluded_ids.add(ev.event_id)
            if dec.posting_period == period:
                excluded.append({**_event_ref(ev), "reason": dec.reason, "decided_by": dec.actor,
                                 "decided_at": dec.decided_at, "late": is_late})
            continue
        if is_late:
            if dec and dec.decision == "POST_LATE":
                items.append(_Item(ev, dec.posting_period, ev.period, True))
            else:
                pending_late.append(ev)
        else:
            items.append(_Item(ev, ev.period, ev.period, False))

    items = [it for it in items if it.posting_period <= period]
    items.sort(key=lambda it: it.order)
    pending_ids = {e.event_id for e in pending_late}

    holds: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []            # all computed lines (posting <= period)
    used_plans: dict[tuple, PlanVersion] = {}
    used_assignments: dict[str, Assignment] = {}
    used_contracts: set[str] = set()

    # ---- ladders: start from frozen credited amounts (closed months)
    ladder: dict[tuple[str, str, str], int] = {}
    for ln in frozen_lines:
        if ln["line_type"] in (EARNING, LATE_EARNING):
            k = (ln["original_period"], ln["rep_id"], ln["currency"])
            ladder[k] = ladder.get(k, 0) + ln["credited_minor"]

    # ---- original earning state per collection: frozen first
    orig_lines: dict[str, dict[str, dict[str, Any]]] = {}
    for ln in frozen_lines:
        if ln["line_type"] in (EARNING, LATE_EARNING):
            orig_lines.setdefault(ln["event_id"], {})[ln["rep_id"]] = ln
    refunded_before: dict[str, int] = {}
    credit_reversed: dict[str, dict[str, int]] = {}
    frozen_refund_events: dict[str, set[str]] = {}
    for ln in frozen_lines:
        if ln["line_type"] in (CLAWBACK, LATE_CLAWBACK):
            oid = ln["original_event_id"]
            frozen_refund_events.setdefault(oid, set()).add(ln["event_id"])
            cr = credit_reversed.setdefault(oid, {})
            cr[ln["rep_id"]] = cr.get(ln["rep_id"], 0) + ln["credit_reversed_minor"]
    for oid, refund_ids in frozen_refund_events.items():
        refunded_before[oid] = sum(inp.events[r].amount_minor for r in refund_ids if r in inp.events)

    held_collections: dict[str, str] = {}   # event_id -> reason (collection could not be fully computed)
    by_rep: dict[str, list[Assignment]] = {}
    for a in inp.assignments:
        by_rep.setdefault(a.rep_id, []).append(a)
    by_plan: dict[tuple[str, str], list[PlanVersion]] = {}
    for v in inp.plan_versions:
        by_plan.setdefault((v.plan_id, v.currency), []).append(v)

    # ---- collections, in global order
    for it in (i for i in items if i.event.event_type == "COLLECTION"):
        ev = it.event
        contract = inp.contracts.get(ev.contract_id)
        splits = inp.splits.get(ev.contract_id) or []
        target = it.posting_period == period
        if contract is None or not splits:
            held_collections[ev.event_id] = "NO_SPLIT"
            if target:
                holds.append(_hold("NO_SPLIT", f"Contract {ev.contract_id} has no credit split; the cash cannot be "
                                   "credited to any rep. Import the split (or exclude the event with a reason).",
                                   event=ev))
            continue
        used_contracts.add(ev.contract_id)
        credits = allocate_largest_remainder(ev.amount_minor, sorted(splits))
        split_map = dict(splits)
        for rep_id in sorted(credits):
            credit = credits[rep_id]
            assignment, err = _resolve_assignment(by_rep.get(rep_id, []), rep_id, ev.event_date)
            plan = None
            if assignment is not None:
                plan = _resolve_plan(by_plan.get((assignment.plan_id, ev.currency), []), assignment.plan_id,
                                     ev.currency, ev.event_date)
                if plan is None:
                    err = "NO_PLAN_VERSION"
            if err:
                held_collections[ev.event_id] = err
                if target:
                    msg = {
                        "NO_ASSIGNMENT": f"Rep {rep_id} has no plan assignment covering {ev.event_date}.",
                        "AMBIGUOUS_ASSIGNMENT": f"Rep {rep_id} has more than one plan assignment starting on the "
                                                f"same date covering {ev.event_date}.",
                        "NO_PLAN_VERSION": f"Plan {assignment.plan_id if assignment else '?'} has no {ev.currency} "
                                           f"version effective on {ev.event_date}.",
                    }[err]
                    holds.append(_hold(err, msg + " Credit is held and excluded from totals.", event=ev,
                                       rep_id=rep_id, amount=credit))
                continue
            used_assignments[assignment.assignment_id] = assignment
            used_plans[(plan.plan_id, plan.version, plan.currency)] = plan
            lk = (it.original_period, rep_id, ev.currency)
            before = ladder.get(lk, 0)
            room = max(0, plan.threshold_minor - before)
            base_portion = min(credit, room)
            accel_portion = credit - base_portion
            ladder[lk] = before + credit
            amount, exact = earning_for_portions(base_portion, plan.base_rate_bps, accel_portion, plan.accel_rate_bps)
            ltype = LATE_EARNING if it.late else EARNING
            line = {
                "line_key": line_key(ltype, ev.event_id, rep_id), "line_type": ltype,
                "period": it.posting_period, "original_period": it.original_period, "rep_id": rep_id,
                "currency": ev.currency, "event_id": ev.event_id, "original_event_id": None,
                "contract_id": ev.contract_id, "event_date": ev.event_date,
                "plan_id": plan.plan_id, "plan_version": plan.version, "split_bps": split_map[rep_id],
                "credited_minor": credit, "credit_reversed_minor": 0,
                "base_portion_minor": base_portion, "accel_portion_minor": accel_portion,
                "base_rate_bps": plan.base_rate_bps, "accel_rate_bps": plan.accel_rate_bps,
                "exact_amount": exact, "amount_minor": amount,   # Fraction; formatted below for this period only
                "detail": {
                    "event_amount_minor": ev.amount_minor,
                    "split_rule": "largest remainder; ties to smaller rep id",
                    "split_all": {r: b for r, b in sorted(splits)},
                    "credits_all": {r: credits[r] for r in sorted(credits)},
                    "attainment_before_minor": before, "attainment_after_minor": before + credit,
                    "threshold_minor": plan.threshold_minor, "assignment_id": assignment.assignment_id,
                    "rounding": "half-up once per line", "rounding_delta": None,
                    "late": it.late, "provenance": {"batch_id": ev.batch_id, "source_row": ev.source_row},
                },
            }
            lines.append(line)
            orig_lines.setdefault(ev.event_id, {})[rep_id] = line

    # ---- refunds, in global order
    for it in (i for i in items if i.event.event_type == "REFUND"):
        ev = it.event
        target = it.posting_period == period
        oid = ev.original_event_id or ""
        original = inp.events.get(oid)
        reason = None
        if original is None:
            reason = ("ORIGINAL_ON_HOLD", f"Original collection {oid} is not available.")
        elif oid in excluded_ids:
            reason = ("ORIGINAL_EXCLUDED", f"Original collection {oid} was excluded from commission; review this "
                      "refund (exclude it too, or post a manual adjustment).")
        elif oid in pending_ids:
            reason = ("ORIGINAL_PENDING", f"Original collection {oid} is a late event awaiting a decision.")
        elif oid in held_collections:
            reason = ("ORIGINAL_ON_HOLD", f"Original collection {oid} is on hold ({held_collections[oid]}); its "
                      "earning is not final, so the clawback is held too.")
        elif oid not in orig_lines:
            reason = ("ORIGINAL_ON_HOLD", f"Original collection {oid} has not been posted in or before {period}.")
        if reason:
            if target:
                holds.append(_hold(reason[0], reason[1], event=ev))
            continue
        per_rep = orig_lines[oid]
        x = original.amount_minor
        r_prev = refunded_before.get(oid, 0)
        r_cum = r_prev + ev.amount_minor
        crev_prev = credit_reversed.setdefault(oid, {})
        remaining = [(rep, per_rep[rep]["credited_minor"] - crev_prev.get(rep, 0)) for rep in sorted(per_rep)]
        credit_alloc = allocate_largest_remainder(ev.amount_minor, remaining)
        ltype = LATE_CLAWBACK if it.late else CLAWBACK
        for rep_id in sorted(per_rep):
            o = per_rep[rep_id]
            e = o["amount_minor"]
            cum_prev = cumulative_reversal(e, r_prev, x)
            cum_now = cumulative_reversal(e, r_cum, x)
            rev = cum_now - cum_prev
            exact = Fraction(e * ev.amount_minor, x)
            line = {
                "line_key": line_key(ltype, ev.event_id, rep_id), "line_type": ltype,
                "period": it.posting_period, "original_period": it.original_period, "rep_id": rep_id,
                "currency": ev.currency, "event_id": ev.event_id, "original_event_id": oid,
                "contract_id": ev.contract_id, "event_date": ev.event_date,
                "plan_id": o["plan_id"], "plan_version": o["plan_version"], "split_bps": o["split_bps"],
                "credited_minor": 0, "credit_reversed_minor": credit_alloc[rep_id],
                "base_portion_minor": 0, "accel_portion_minor": 0,
                "base_rate_bps": o["base_rate_bps"], "accel_rate_bps": o["accel_rate_bps"],
                "exact_amount": -exact, "amount_minor": -rev,
                "detail": {
                    "original_line_key": o["line_key"], "original_period": o["period"],
                    "original_earning_minor": e, "original_credited_minor": o["credited_minor"],
                    "original_collection_minor": x, "refund_minor": ev.amount_minor,
                    "refunded_before_minor": r_prev, "refunded_after_minor": r_cum,
                    "cumulative_reversal_before_minor": cum_prev, "cumulative_reversal_after_minor": cum_now,
                    "method": "cumulative: reversal = half_up(E*R_after/x) - half_up(E*R_before/x)",
                    "credit_rule": "refund cash allocated by not-yet-reversed credit (largest remainder)",
                    "late": it.late, "provenance": {"batch_id": ev.batch_id, "source_row": ev.source_row},
                },
            }
            lines.append(line)
            crev_prev[rep_id] = crev_prev.get(rep_id, 0) + credit_alloc[rep_id]
        refunded_before[oid] = r_cum
        if original is not None:
            used_contracts.add(original.contract_id)

    # ---- manual adjustments (separate, never disguised as source earnings)
    for adj in sorted((a for a in inp.adjustments if a.period == period), key=lambda a: a.adj_id):
        lines.append({
            "line_key": line_key("ADJ", str(adj.adj_id)), "line_type": MANUAL_ADJUSTMENT,
            "period": period, "original_period": period, "rep_id": adj.rep_id, "currency": adj.currency,
            "event_id": None, "original_event_id": None, "contract_id": None, "event_date": None,
            "plan_id": None, "plan_version": None, "split_bps": None, "credited_minor": 0,
            "credit_reversed_minor": 0, "base_portion_minor": 0, "accel_portion_minor": 0,
            "base_rate_bps": None, "accel_rate_bps": None,
            "exact_amount": fraction_to_str(Fraction(adj.amount_minor)), "amount_minor": adj.amount_minor,
            "detail": {"adj_id": adj.adj_id, "category": adj.category, "reason": adj.reason,
                       "case_id": adj.case_id, "entered_by": adj.actor, "entered_at": adj.created_at},
        })

    # ---- holds for late events and quarantined cash rows
    if first_open is not None and period == first_open:
        for ev in sorted(pending_late, key=lambda e: (e.event_date, e.event_id)):
            holds.append(_hold("LATE_EVENT_PENDING",
                               f"{ev.event_type.title()} dated {ev.event_date} belongs to closed period {ev.period} "
                               "and arrived after close. The closed statement will not be restated: post it here as "
                               "a prior-period adjustment (reason required) or exclude it.", event=ev,
                               extra={"original_period": ev.period}))
    for q in inp.quarantined_cash:
        qd = q.get("event_date")
        qp = period_of(qd) if qd else None
        if qp is None:
            applies = True
        elif last_closed and qp <= last_closed:
            applies = period == first_open
        else:
            applies = qp == period
        if applies:
            holds.append({"code": "QUARANTINED_CASH_ROW", "severity": BLOCKING,
                          "message": f"Quarantined cash row (batch #{q['batch_id']}, row {q['source_row']}): "
                                     f"{'; '.join(r['code'] for r in q['reasons'])}. Supersede it with a corrected "
                                     "file or dismiss it with a reason before close.",
                          "event_id": (q.get("natural_key") or [None])[0], "rep_id": None,
                          "currency": q.get("currency"), "amount_minor": q.get("amount_minor"),
                          "event_date": qd, "q_id": q["q_id"]})

    period_lines = [ln for ln in lines if ln["period"] == period]
    for ln in period_lines:   # exact values are only rendered for the period being reported
        if isinstance(ln["exact_amount"], Fraction):
            exact = ln["exact_amount"]
            ln["exact_amount"] = fraction_to_str(exact)
            if ln["line_type"] in (EARNING, LATE_EARNING):
                ln["detail"]["rounding_delta"] = fraction_to_str(Fraction(ln["amount_minor"]) - exact)
    period_lines.sort(key=lambda ln: (_type_rank(ln["line_type"]), ln.get("event_date") or "",
                                      ln.get("event_id") or "", ln["rep_id"] or "", ln["line_key"]))
    holds.sort(key=lambda h: (h["severity"] != BLOCKING, h["code"], h.get("event_date") or "",
                              h.get("event_id") or "", h.get("rep_id") or ""))

    target_items = [it for it in items if it.posting_period == period]
    held_event_ids = {h["event_id"] for h in holds if h.get("event_id")}
    accounted = sorted({it.event.event_id for it in target_items if it.event.event_id not in held_event_ids}
                       | {e["event_id"] for e in excluded})

    totals = _totals(period_lines)
    controls = _controls(period, period_lines, target_items, holds, excluded, inp, orig_lines, refunded_before)
    kpis = _kpis(period, inp, period_lines)
    inputs_used = _inputs_used(inp, target_items, excluded, used_contracts, used_plans, used_assignments, period)
    inputs_used["reps"] = [{"rep_id": r, **{k: v for k, v in inp.reps.get(r, {}).items() if k != "rep_id"}}
                           for r in sorted({ln["rep_id"] for ln in period_lines if ln["rep_id"]})]
    # The digest binds EVERY value that a close would freeze (lines, holds, exclusions, totals, controls,
    # KPIs, accounted events and the exact inputs), so any unreviewed change - even a booking-only import
    # that changes no commission line - makes a reviewed run stale.
    digest = sha256_json({"engine": ENGINE_VERSION, "period": period, "lines": period_lines, "holds": holds,
                          "excluded": excluded, "totals": totals, "controls": controls, "kpis": kpis,
                          "accounted_event_ids": accounted, "inputs": sha256_json(inputs_used)})
    return PeriodResult(period, period_lines, holds, excluded, totals, controls, kpis, accounted, inputs_used,
                        digest, first_open)


def _type_rank(t: str) -> int:
    return LINE_TYPES.index(t)


def _totals(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[tuple[str, str], dict[str, Any]] = {}
    for ln in lines:
        k = (ln["rep_id"], ln["currency"])
        t = by.setdefault(k, {"rep_id": k[0], "currency": k[1], "credited_minor": 0, "base_portion_minor": 0,
                              "accel_portion_minor": 0, "earnings_minor": 0, "clawbacks_minor": 0,
                              "late_minor": 0, "manual_minor": 0, "net_minor": 0, "credit_reversed_minor": 0,
                              "line_count": 0, "attainment_minor": 0, "threshold_minor": None})
        t["line_count"] += 1
        t["net_minor"] += ln["amount_minor"]
        if ln["line_type"] == EARNING:
            t["earnings_minor"] += ln["amount_minor"]
            t["credited_minor"] += ln["credited_minor"]
            t["base_portion_minor"] += ln["base_portion_minor"]
            t["accel_portion_minor"] += ln["accel_portion_minor"]
            t["attainment_minor"] = max(t["attainment_minor"], ln["detail"]["attainment_after_minor"])
            t["threshold_minor"] = ln["detail"]["threshold_minor"]
        elif ln["line_type"] == CLAWBACK:
            t["clawbacks_minor"] += ln["amount_minor"]
            t["credit_reversed_minor"] += ln["credit_reversed_minor"]
        elif ln["line_type"] in (LATE_EARNING, LATE_CLAWBACK):
            t["late_minor"] += ln["amount_minor"]
        else:
            t["manual_minor"] += ln["amount_minor"]
    return [by[k] for k in sorted(by)]


def _controls(period, lines, target_items, holds, excluded, inp, orig_lines, refunded_before) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    held = {h["event_id"] for h in holds if h.get("event_id")}
    credited_by_event: dict[str, int] = {}
    reversed_by_event: dict[str, int] = {}
    for ln in lines:
        if ln["line_type"] in (EARNING, LATE_EARNING):
            credited_by_event[ln["event_id"]] = credited_by_event.get(ln["event_id"], 0) + ln["credited_minor"]
        elif ln["line_type"] in (CLAWBACK, LATE_CLAWBACK):
            reversed_by_event[ln["event_id"]] = reversed_by_event.get(ln["event_id"], 0) + ln["credit_reversed_minor"]
    # 1. split conservation per processed collection
    bad = []
    for it in target_items:
        ev = it.event
        if ev.event_type != "COLLECTION" or ev.event_id in held:
            continue
        if credited_by_event.get(ev.event_id, 0) != ev.amount_minor:
            bad.append(ev.event_id)
    controls.append({"name": "Split conservation: credited cents == collected cents for every collection",
                     "passed": not bad, "detail": f"{len(bad)} mismatching collections" if bad else "all collections conserve"})
    # 2. refund credit conservation
    bad = []
    for it in target_items:
        ev = it.event
        if ev.event_type != "REFUND" or ev.event_id in held:
            continue
        if reversed_by_event.get(ev.event_id, 0) != ev.amount_minor:
            bad.append(ev.event_id)
    controls.append({"name": "Refund conservation: credit reversed == refunded cents for every refund",
                     "passed": not bad, "detail": f"{len(bad)} mismatching refunds" if bad else "all refunds conserve"})
    # 3. bracket arithmetic
    bad = [ln["line_key"] for ln in lines if ln["line_type"] in (EARNING, LATE_EARNING)
           and ln["base_portion_minor"] + ln["accel_portion_minor"] != ln["credited_minor"]]
    controls.append({"name": "Bracket arithmetic: base portion + accelerator portion == credited cash",
                     "passed": not bad, "detail": f"{len(bad)} lines fail" if bad else "all earning lines pass"})
    # 4. clawback never exceeds the original earning; full refunds reverse it exactly
    over, inexact = [], []
    for oid, per_rep in orig_lines.items():
        ev = inp.events.get(oid)
        if ev is None:
            continue
        r = refunded_before.get(oid, 0)
        for rep, o in per_rep.items():
            cum = cumulative_reversal(o["amount_minor"], r, ev.amount_minor)
            if cum > o["amount_minor"]:
                over.append(oid)
            if r == ev.amount_minor and cum != o["amount_minor"]:
                inexact.append(oid)
    controls.append({"name": "Clawback bound: cumulative reversal <= original earning; full refund reverses exactly",
                     "passed": not over and not inexact,
                     "detail": "holds for every refunded collection" if not over and not inexact
                     else f"{len(over)} over, {len(inexact)} inexact"})
    # 5. cash reconciliation for events dated in the period
    recon: dict[str, dict[str, int]] = {}
    processed = {it.event.event_id for it in target_items if not it.late}
    excluded_ids = {e["event_id"] for e in excluded}
    ok = True
    for ev in inp.events.values():
        if ev.period != period:
            continue
        b = recon.setdefault(ev.currency, {}).setdefault(
            ev.event_type, {"dated_in_period": 0, "processed": 0, "held": 0, "excluded": 0})
        b["dated_in_period"] += ev.amount_minor
        if ev.event_id in held:
            b["held"] += ev.amount_minor
        elif ev.event_id in excluded_ids:
            b["excluded"] += ev.amount_minor
        elif ev.event_id in processed:
            b["processed"] += ev.amount_minor
    for per_ccy in recon.values():
        for b in per_ccy.values():
            b["reconciles"] = b["dated_in_period"] == b["processed"] + b["held"] + b["excluded"]
            ok = ok and b["reconciles"]
    controls.append({"name": "Cash reconciliation: cash dated in period == processed + held + excluded",
                     "passed": ok, "detail": recon})
    return controls


def _kpis(period: str, inp: EngineInput, lines: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    k: dict[str, dict[str, int]] = {}

    def bucket(ccy: str) -> dict[str, int]:
        return k.setdefault(ccy, {"bookings_minor": 0, "bookings_count": 0, "cash_collected_minor": 0,
                                  "refunds_minor": 0, "net_cash_minor": 0, "credited_minor": 0,
                                  "earnings_minor": 0, "clawbacks_minor": 0, "late_minor": 0, "manual_minor": 0,
                                  "net_payable_minor": 0})
    for c in inp.contracts.values():
        if period_of(c.booking_date) == period:
            b = bucket(c.currency)
            b["bookings_minor"] += c.booked_amount_minor
            b["bookings_count"] += 1
    for ev in inp.events.values():
        if ev.period == period:
            b = bucket(ev.currency)
            if ev.event_type == "COLLECTION":
                b["cash_collected_minor"] += ev.amount_minor
            else:
                b["refunds_minor"] += ev.amount_minor
    for ln in lines:
        b = bucket(ln["currency"])
        b["net_payable_minor"] += ln["amount_minor"]
        if ln["line_type"] == EARNING:
            b["credited_minor"] += ln["credited_minor"]
            b["earnings_minor"] += ln["amount_minor"]
        elif ln["line_type"] == CLAWBACK:
            b["clawbacks_minor"] += ln["amount_minor"]
        elif ln["line_type"] in (LATE_EARNING, LATE_CLAWBACK):
            b["late_minor"] += ln["amount_minor"]
        else:
            b["manual_minor"] += ln["amount_minor"]
    for b in k.values():
        b["net_cash_minor"] = b["cash_collected_minor"] - b["refunds_minor"]
    return {c: k[c] for c in sorted(k)}


def _inputs_used(inp, target_items, excluded, used_contracts, used_plans, used_assignments, period) -> dict[str, Any]:
    ev_ids = {it.event.event_id for it in target_items} | {e["event_id"] for e in excluded}
    orig_ids = {inp.events[i].original_event_id for i in ev_ids if inp.events[i].original_event_id}
    events = [_event_ref(inp.events[i]) for i in sorted(ev_ids | orig_ids) if i in inp.events]
    contracts = [vars(inp.contracts[c]) for c in sorted(used_contracts) if c in inp.contracts]
    splits = [{"contract_id": c, "rep_id": r, "split_bps": b} for c in sorted(used_contracts)
              for r, b in sorted(inp.splits.get(c, []))]
    plans = [vars(used_plans[k]) for k in sorted(used_plans)]
    assignments = [vars(used_assignments[k]) for k in sorted(used_assignments)]
    decisions = [vars(inp.decisions[i]) for i in sorted(inp.decisions)
                 if inp.decisions[i].posting_period == period]
    adjustments = [vars(a) for a in sorted(inp.adjustments, key=lambda a: a.adj_id) if a.period == period]
    return {"events": events, "contracts": contracts, "splits": splits, "plan_versions": plans,
            "assignments": assignments, "decisions": decisions, "adjustments": adjustments}


def diff_lines(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """What changed between two runs (used to explain a stale review)."""
    o = {ln["line_key"]: ln for ln in old}
    n = {ln["line_key"]: ln for ln in new}
    added = [n[k] for k in sorted(n.keys() - o.keys())]
    removed = [o[k] for k in sorted(o.keys() - n.keys())]
    changed = [{"line_key": k, "old_amount_minor": o[k]["amount_minor"], "new_amount_minor": n[k]["amount_minor"]}
               for k in sorted(o.keys() & n.keys())
               if canonical_json(_cmp(o[k])) != canonical_json(_cmp(n[k]))]
    return {"added": added, "removed": removed, "changed": changed}


def _cmp(ln: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in ln.items() if k not in ("run_id",)}
