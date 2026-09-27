"""HTTP routes. GET handlers only read; every POST calls one rcw.services / importer / variance function."""
from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

from flask import (Blueprint, Response, abort, current_app, flash, g, redirect, render_template, request, session,
                   url_for)

from .. import db, demo, exports, importer, services, variance
from ..engine import BLOCKING
from ..explain import TYPE_LABELS, explain_line
from ..money import MoneyFormatError, fraction_to_str, parse_amount
from ..periods_util import is_period, period_of
from ..repo import activity_periods, rep_names
from . import ACTORS, charts

bp = Blueprint("ui", __name__)


def conn():
    return g.conn


def actor() -> str:
    return session.get("actor", "analyst-1")


def _flash_error(exc: Exception) -> None:
    if isinstance(exc, services.WorkflowError):
        msg = f"{exc.code}: {exc.message}"
        if exc.code in ("STALE_REVIEW", "STALE_RUN") and isinstance(exc.details, dict):
            d = exc.details
            msg += (f" — lines added {len(d.get('added', []))}, removed {len(d.get('removed', []))}, "
                    f"changed {len(d.get('changed', []))}; other changed sections: "
                    f"{', '.join(d.get('other_changed_sections', [])) or 'none'}.")
        flash(msg, "error")
    else:
        flash(str(exc), "error")


def _require_period(p: str) -> None:
    if not is_period(p):
        abort(404)


def period_state(c, p: str) -> dict[str, Any]:
    closed = services.is_closed(c, p)
    prow = services.period_row(c, p)
    st: dict[str, Any] = {"period": p, "closed": closed, "row": dict(prow) if prow else None}
    if closed:
        st["status"] = "CLOSED"
        return st
    st["status"] = prow["status"] if prow else "OPEN"
    run = services.latest_run(c, p)
    st["latest_run"] = dict(run) if run else None
    try:
        res = services.compute(c, p)
    except services.WorkflowError as exc:
        st["error"] = f"{exc.code}: {exc.message}"
        return st
    st["result"] = res
    st["run_current"] = bool(run and run["result_digest"] == res.result_digest)
    if prow and prow["status"] == "IN_REVIEW":
        rr = c.execute("SELECT result_digest FROM calc_runs WHERE run_id = ?", (prow["review_run_id"],)).fetchone()
        st["review_current"] = rr["result_digest"] == res.result_digest
    return st


def demo_progress(c) -> list[dict[str, Any]]:
    stage = int(db.get_setting(c, "demo_stage", "0") or 0)
    apr = services.is_closed(c, "2026-04")
    may = services.is_closed(c, "2026-05")
    resolved = c.execute("SELECT COUNT(*) FROM variance_cases WHERE status = 'RESOLVED'").fetchone()[0]
    steps = [
        {"title": "Load the demo data (stage 1: data as of 6 May 2026)", "done": stage >= 1,
         "detail": "Reps, plan v1, contracts, splits, April cash, April payout register. Four rows are "
                   "quarantined on purpose, from three planted problems: a future-dated receipt, a split that "
                   "totals 90% (both of its rows) and a payout for an unknown rep."},
        {"title": "Close April 2026", "done": apr, "link": url_for("ui.period", p="2026-04"),
         "detail": "Open April → Calculate → Submit for review as analyst-1 → switch to manager-1 → Close. "
                   "Look for the 60/40 split sale and the threshold crossing."},
        {"title": "Load late-arriving data (stage 2: as of 4 June 2026)", "done": stage >= 2,
         "detail": "Plan v2 (accelerator 9% from May), May cash including a partial refund of an April sale, a "
                   "receipt dated 29 April that arrived after April was closed, and the May payout register."},
        {"title": "Resolve the late April receipt and close May 2026", "done": may,
         "link": url_for("ui.period", p="2026-05"),
         "detail": "May is blocked by a LATE_EVENT_PENDING hold. Post it as a prior-period adjustment (reason "
                   "required), recalculate, submit, close. April's closed statement never changes."},
        {"title": "Investigate payout variances and export", "done": resolved > 0,
         "link": url_for("ui.variance_page"),
         "detail": "Three planted differences: base rate paid on accelerated cash, a missed clawback, and a late "
                   "adjustment the register did not include. Resolve ≠ paid."},
    ]
    nxt = next((i for i, s in enumerate(steps) if not s["done"]), None)
    for i, s in enumerate(steps):
        s["state"] = "done" if s["done"] else ("next" if i == nxt else "todo")
    return steps


# ------------------------------------------------------------------ overview & demo

@bp.get("/")
def index():
    c = conn()
    periods = []
    for p in activity_periods(c):
        st = period_state(c, p)
        if st["closed"]:
            snap = services.get_snapshot(c, p)
            kpis = snap["kpis"] if snap else {}
            holds = 0
        else:
            res = st.get("result")
            kpis = res.kpis if res else {}
            holds = len(res.blocking_holds) if res else 0
        periods.append({"period": p, "status": st["status"], "kpis": kpis, "holds": holds})
    counts = {
        "events": c.execute("SELECT COUNT(*) FROM cash_events").fetchone()[0],
        "quarantine": c.execute("SELECT COUNT(*) FROM quarantine_rows WHERE status = 'OPEN'").fetchone()[0],
        "cases": c.execute("SELECT COUNT(*) FROM variance_cases WHERE status IN ('OPEN','INVESTIGATING','REOPENED')"
                           ).fetchone()[0],
        "batches": c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0],
    }
    stage = int(db.get_setting(c, "demo_stage", "0") or 0)
    return render_template("index.html", periods=periods, counts=counts, steps=demo_progress(c), stage=stage)


@bp.post("/actor")
def set_actor():
    a = request.form.get("actor", "")
    if a in ACTORS:
        session["actor"] = a
        flash(f"Acting as {a} (a demo label, not a login).", "info")
    return redirect(request.form.get("next") or url_for("ui.index"))


def _reset_db() -> Any:
    g.pop("conn").close()
    c = demo.fresh_db(current_app.config["DB_PATH"])
    g.conn = c
    return c


@bp.post("/demo/<action>")
def demo_action(action: str):
    c = conn()
    try:
        if action == "load-stage1":
            if c.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0]:
                flash("The database already has data. Use 'Reset demo' first if you really want to start over.",
                      "error")
                return redirect(url_for("ui.index"))
            results = demo.load_stage(c, 1, actor())
            flash("Stage 1 loaded: " + "; ".join(r.summary() for r in results), "ok")
        elif action == "load-stage2":
            if int(db.get_setting(c, "demo_stage", "0") or 0) != 1 or not services.is_closed(c, "2026-04"):
                flash("Close April 2026 first (the late data only makes sense after April is closed).", "error")
                return redirect(url_for("ui.index"))
            results = demo.load_stage(c, 2, actor())
            flash("Stage 2 loaded: " + "; ".join(r.summary() for r in results), "ok")
            return redirect(url_for("ui.period", p="2026-05"))
        elif action == "full":
            if request.form.get("confirm") != "yes":
                flash("Tick the confirmation box: this replaces the local demo database.", "error")
                return redirect(url_for("ui.index"))
            c = _reset_db()
            demo.play_full_story(c)
            flash("Full demo story played (April and May closed, cases created). Explore freely.", "ok")
        elif action == "reset":
            if request.form.get("confirm") != "yes":
                flash("Tick the confirmation box: reset deletes the local demo database.", "error")
                return redirect(url_for("ui.index"))
            _reset_db()
            flash("Demo database reset to empty.", "ok")
        else:
            abort(404)
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.index"))


# ------------------------------------------------------------------ periods

@bp.get("/periods")
def periods():
    return redirect(url_for("ui.index") + "#periods")


@bp.get("/periods/<p>")
def period(p: str):
    _require_period(p)
    c = conn()
    st = period_state(c, p)
    names = rep_names(c)
    if st["closed"]:
        snap = services.get_snapshot(c, p)
        if snap is None:
            model = {"lines": [], "totals": [], "kpis": {}, "controls": [], "excluded": [], "holds": [], "meta": {}}
        else:
            model = {"lines": snap["lines"], "totals": snap["totals"], "kpis": snap["kpis"],
                     "controls": snap["controls"], "excluded": snap["excluded"], "holds": [], "meta": snap}
            names = {**names, **{r: v.get("display_name", r) for r, v in snap.get("reps", {}).items()}}
    else:
        res = st.get("result")
        model = {"lines": res.lines if res else [], "totals": res.totals if res else [], "kpis": res.kpis if res else {},
                 "controls": res.controls if res else [], "excluded": res.excluded if res else [],
                 "holds": res.holds if res else [], "meta": {}}
    currencies = sorted(model["kpis"])
    chart_attain = {ccy: charts.attainment_svg(model["totals"], names, ccy) for ccy in currencies}
    chart_bridge = {ccy: charts.bridge_svg(model["kpis"][ccy], ccy) for ccy in currencies}
    runs = [dict(r) for r in c.execute("SELECT run_id, created_at, actor, result_digest, line_count, blocking_holds "
                                       "FROM calc_runs WHERE period = ? ORDER BY run_id DESC LIMIT 12", (p,))]
    adjustments = [dict(r) for r in c.execute("SELECT * FROM adjustments WHERE period = ? ORDER BY adj_id", (p,))]
    reps = [dict(r) for r in c.execute("SELECT rep_id, display_name FROM reps ORDER BY rep_id")]
    first_open = services.first_open_period(c)
    all_periods = activity_periods(c)
    return render_template("period.html", p=p, st=st, model=model, names=names, currencies=currencies,
                           chart_attain=chart_attain, chart_bridge=chart_bridge, runs=runs, adjustments=adjustments,
                           reps=reps, first_open=first_open, all_periods=all_periods, BLOCKING=BLOCKING)


def _wf_post(p: str, fn, ok_msg: str):
    _require_period(p)
    try:
        out = fn()
        flash(ok_msg.format(out=out), "ok")
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.period", p=p))


@bp.post("/periods/<p>/calculate")
def calculate(p: str):
    def run():
        out = services.calculate_period(conn(), p, actor())
        extra = " Review withdrawn because the numbers changed." if out.review_withdrawn else ""
        return f"run #{out.run_id} with {len(out.result.lines)} lines, {len(out.result.blocking_holds)} blocking " \
               f"holds.{extra}"
    return _wf_post(p, run, "Calculated: {out}")


@bp.post("/periods/<p>/submit")
def submit(p: str):
    return _wf_post(p, lambda: services.submit_for_review(conn(), p, actor()),
                    "Submitted run #{out} for review. Switch to a different label (e.g. manager-1) to close.")


@bp.post("/periods/<p>/return")
def return_draft(p: str):
    return _wf_post(p, lambda: services.return_to_draft(conn(), p, actor(), request.form.get("reason", "")),
                    "Returned to draft.")


@bp.post("/periods/<p>/close")
def close(p: str):
    return _wf_post(p, lambda: services.close_period(conn(), p, actor())["snapshot_sha256"][:16],
                    "Closed. Snapshot SHA-256 {out}… is now frozen; payables recorded (no payment executed).")


@bp.post("/periods/<p>/adjustments")
def add_adjustment(p: str):
    def run():
        try:
            amount = parse_amount(request.form.get("amount", ""), allow_negative=True)
        except MoneyFormatError as exc:
            raise services.WorkflowError("BAD_AMOUNT", str(exc)) from exc
        return services.add_adjustment(conn(), p, request.form.get("rep_id", ""), request.form.get("currency", ""),
                                       amount, request.form.get("category", ""), request.form.get("reason", ""),
                                       actor(), request.form.get("case_id") or None)
    return _wf_post(p, run, "Manual adjustment #{out} recorded. Recalculate to include it in a reviewable run.")


@bp.post("/events/<event_id>/decide")
def decide(event_id: str):
    back = request.form.get("back_period", "")
    try:
        posting = services.decide_event(conn(), event_id, request.form.get("decision", ""),
                                        request.form.get("reason", ""), actor())
        flash(f"Decision recorded for {event_id} (posting period {posting}). Recalculate to see it.", "ok")
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.period", p=back) if is_period(back) else url_for("ui.event", event_id=event_id))


# ------------------------------------------------------------------ statements & drill-down

@bp.get("/periods/<p>/statements/<rep>/<ccy>")
def statement(p: str, rep: str, ccy: str):
    _require_period(p)
    c = conn()
    try:
        model = exports.statement_model(c, p, rep, ccy)
    except services.WorkflowError:
        abort(404)
    if not model["totals"] and not model["lines"]:
        abort(404)
    groups: dict[str, list] = {}
    for ln in model["lines"]:
        groups.setdefault(ln["line_type"], []).append(ln)
    t = model["totals"][0] if model["totals"] else None
    chart = charts.attainment_svg(model["totals"], model["names"], ccy) if t else ""
    holds = [h for h in model["holds"] if h.get("rep_id") in (rep, None) and h.get("currency") in (ccy, None)]
    return render_template("statement.html", p=p, rep=rep, ccy=ccy, model=model, groups=groups, t=t, chart=chart,
                           holds=holds, TYPE_LABELS=TYPE_LABELS, explain=explain_line)


@bp.get("/periods/<p>/lines/<path:line_key>")
def line(p: str, line_key: str):
    _require_period(p)
    c = conn()
    if services.is_closed(c, p):
        snap = services.get_snapshot(c, p)
        lines, inputs, frozen = (snap["lines"], snap["inputs_used"], True) if snap else ([], {}, True)
    else:
        res = services.compute(c, p)
        lines, inputs, frozen = res.lines, res.inputs_used, False
    ln = next((x for x in lines if x["line_key"] == line_key), None)
    if ln is None:
        abort(404)
    ev = next((e for e in inputs.get("events", []) if e["event_id"] == ln["event_id"]), None)
    orig = next((e for e in inputs.get("events", []) if e["event_id"] == ln.get("original_event_id")), None)
    contract = next((x for x in inputs.get("contracts", []) if x["contract_id"] == ln.get("contract_id")), None)
    split = [dict(s) for s in inputs.get("splits", []) if s["contract_id"] == ln.get("contract_id")]
    amount = (ln.get("detail") or {}).get("event_amount_minor")
    for s in split:   # exact share as a Fraction string - no float anywhere near money
        s["exact_share"] = fraction_to_str(Fraction(amount * s["split_bps"], 10000)) if amount is not None else ""
    plan = next((v for v in inputs.get("plan_versions", []) if v["plan_id"] == ln.get("plan_id")
                 and v["version"] == ln.get("plan_version") and v["currency"] == ln["currency"]), None)
    if plan is None and ln.get("plan_id"):
        row = c.execute("SELECT * FROM plan_versions WHERE plan_id = ? AND version = ? AND currency = ?",
                        (ln["plan_id"], ln["plan_version"], ln["currency"])).fetchone()
        plan = dict(row) if row else None
    assignment = next((a for a in inputs.get("assignments", [])
                       if a["assignment_id"] == (ln.get("detail") or {}).get("assignment_id")), None)
    batch = None
    if ev:
        b = c.execute("SELECT batch_id, original_filename, sha256, imported_at FROM import_batches WHERE batch_id = ?",
                      (ev["batch_id"],)).fetchone()
        batch = dict(b) if b else None
    return render_template("line.html", p=p, ln=ln, ev=ev, orig=orig, contract=contract, split=split, plan=plan,
                           assignment=assignment, batch=batch, frozen=frozen, TYPE_LABELS=TYPE_LABELS,
                           explain=explain_line(ln), names=rep_names(c))


@bp.get("/events/<event_id>")
def event(event_id: str):
    c = conn()
    ev = c.execute("SELECT * FROM cash_events WHERE event_id = ?", (event_id,)).fetchone()
    if ev is None:
        abort(404)
    batch = c.execute("SELECT * FROM import_batches WHERE batch_id = ?", (ev["batch_id"],)).fetchone()
    refunds = [dict(r) for r in c.execute("SELECT * FROM cash_events WHERE original_event_id = ? ORDER BY event_date,"
                                          " event_id", (event_id,))]
    decision = c.execute("SELECT * FROM event_decisions WHERE event_id = ?", (event_id,)).fetchone()
    appearances = []
    candidates = {period_of(ev["event_date"])}
    if decision:
        candidates.add(decision["posting_period"])
    for p in sorted(candidates):
        try:
            if services.is_closed(c, p):
                snap = services.get_snapshot(c, p)
                src_lines = snap["lines"] if snap else []
                basis = "CLOSED snapshot"
            else:
                src_lines = services.compute(c, p).lines
                basis = "current draft"
        except services.WorkflowError:
            continue
        for ln in src_lines:
            if ln["event_id"] == event_id:
                appearances.append({"period": p, "basis": basis, "line": ln})
    if not appearances:
        # a late event lives in a later snapshot
        for p in activity_periods(c):
            if services.is_closed(c, p):
                snap = services.get_snapshot(c, p)
                for ln in (snap["lines"] if snap else []):
                    if ln["event_id"] == event_id:
                        appearances.append({"period": p, "basis": "CLOSED snapshot", "line": ln})
    return render_template("event.html", ev=dict(ev), batch=dict(batch), refunds=refunds,
                           decision=dict(decision) if decision else None, appearances=appearances,
                           names=rep_names(c), TYPE_LABELS=TYPE_LABELS)


@bp.get("/contracts/<contract_id>")
def contract(contract_id: str):
    c = conn()
    k = c.execute("SELECT * FROM contracts WHERE contract_id = ?", (contract_id,)).fetchone()
    if k is None:
        abort(404)
    splits = [dict(r) for r in c.execute("SELECT * FROM splits WHERE contract_id = ? ORDER BY rep_id", (contract_id,))]
    events = [dict(r) for r in c.execute("SELECT * FROM cash_events WHERE contract_id = ? ORDER BY event_date, event_id",
                                         (contract_id,))]
    return render_template("contract.html", k=dict(k), splits=splits, events=events, names=rep_names(c))


# ------------------------------------------------------------------ imports & quarantine

@bp.get("/imports")
def imports():
    c = conn()
    batches = [dict(r) for r in c.execute("SELECT * FROM import_batches ORDER BY batch_id DESC LIMIT 200")]
    return render_template("imports.html", batches=batches, kinds=importer.FILE_KINDS, labels=importer.KIND_LABELS,
                           order=importer.KIND_ORDER)


@bp.post("/imports")
def upload():
    f = request.files.get("file")
    kind = request.form.get("kind", "")
    mode = request.form.get("mode", "quarantine")
    if f is None or not f.filename:
        flash("Choose a CSV file to upload.", "error")
        return redirect(url_for("ui.imports"))
    if kind not in importer.FILE_KINDS or mode not in ("strict", "quarantine"):
        flash("Choose a file type and an import mode.", "error")
        return redirect(url_for("ui.imports"))
    data = f.read()
    r = importer.import_csv(conn(), kind, f.filename, data, mode=mode, actor=actor())
    flash(r.summary(), "ok" if r.status == "COMMITTED" else ("info" if r.status == "DUPLICATE_FILE" else "error"))
    return redirect(url_for("ui.batch", batch_id=r.batch_id))


@bp.get("/imports/<int:batch_id>")
def batch(batch_id: int):
    c = conn()
    b = c.execute("SELECT * FROM import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if b is None:
        abort(404)
    b = dict(b)
    control = json.loads(b["control_json"])
    file_errors = json.loads(b["file_errors_json"])
    q = [_q(r) for r in c.execute("SELECT * FROM quarantine_rows WHERE batch_id = ? ORDER BY source_row", (batch_id,))]
    return render_template("batch.html", b=b, control=control, file_errors=file_errors, q=q,
                           label=importer.KIND_LABELS.get(b["file_kind"], b["file_kind"]))


def _q(r) -> dict[str, Any]:
    d = dict(r)
    d["raw"] = json.loads(d["raw_json"])
    d["reasons"] = json.loads(d["reasons_json"])
    d["key"] = json.loads(d["natural_key"])
    return d


@bp.get("/quarantine")
def quarantine():
    c = conn()
    status = request.args.get("status", "OPEN")
    kind = request.args.get("kind", "")
    sql, args = "SELECT * FROM quarantine_rows WHERE 1=1", []
    if status in ("OPEN", "DISMISSED", "SUPERSEDED"):
        sql += " AND status = ?"
        args.append(status)
    if kind in importer.FILE_KINDS:
        sql += " AND file_kind = ?"
        args.append(kind)
    rows = [_q(r) for r in c.execute(sql + " ORDER BY q_id DESC LIMIT 500", args)]
    return render_template("quarantine.html", rows=rows, status=status, kind=kind, kinds=importer.FILE_KINDS,
                           labels=importer.KIND_LABELS)


@bp.post("/quarantine/<int:q_id>/dismiss")
def dismiss(q_id: int):
    try:
        importer.dismiss_quarantine_row(conn(), q_id, request.form.get("reason", ""), actor())
        flash(f"Quarantine row #{q_id} dismissed with your reason (audited).", "ok")
    except ValueError as exc:
        flash(str(exc), "error")
    return redirect(request.form.get("next") or url_for("ui.quarantine"))


# ------------------------------------------------------------------ variance & cases

@bp.get("/variance")
def variance_page():
    c = conn()
    with_payouts = [r[0] for r in c.execute("SELECT DISTINCT period FROM recorded_payouts ORDER BY period")]
    options = sorted(set(with_payouts) | set(activity_periods(c)))
    p = request.args.get("period") or (with_payouts[-1] if with_payouts else (options[-1] if options else ""))
    if not p:
        return render_template("variance.html", p=None, options=[], table=None, runs=[], as_of=None, names={})
    _require_period(p)
    as_of = request.args.get("as_of_run", type=int)
    try:
        table = variance.variance_table(c, p, as_of_run=as_of)
    except services.WorkflowError as exc:
        _flash_error(exc)
        return redirect(url_for("ui.variance_page", period=p))
    runs = [dict(r) for r in c.execute("SELECT run_id, created_at FROM calc_runs WHERE period = ? ORDER BY run_id DESC",
                                       (p,))]
    return render_template("variance.html", p=p, options=options, table=table, runs=runs, as_of=as_of,
                           names=rep_names(c))


@bp.post("/variance/refresh")
def variance_refresh():
    p = request.form.get("period", "")
    _require_period(p)
    try:
        stats = variance.refresh_variance(conn(), p, actor())
        flash(f"Cases refreshed for {p}: " + ", ".join(f"{k} {v}" for k, v in stats.items()), "ok")
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.variance_page", period=p))


@bp.get("/cases/<case_id>")
def case(case_id: str):
    c = conn()
    cs = variance.get_case(c, case_id)
    if cs is None:
        abort(404)
    table = variance.variance_table(c, cs["period"])
    sugg = variance.suggestions(cs, table["lines"], table["recorded_rows"], table["basis"])
    lines = [ln for ln in table["lines"] if ln["rep_id"] == cs["rep_id"] and ln["currency"] == cs["currency"]]
    recs = [r for r in table["recorded_rows"] if r["rep_id"] == cs["rep_id"] and r["currency"] == cs["currency"]]
    open_periods = [p for p in activity_periods(c) if not services.is_closed(c, p)]
    fo = services.first_open_period(c)
    if fo and fo not in open_periods:
        open_periods.insert(0, fo)
    adjustments = [dict(r) for r in c.execute("SELECT * FROM adjustments WHERE case_id = ? ORDER BY adj_id", (case_id,))]
    return render_template("case.html", cs=cs, notes=variance.case_notes(c, case_id), sugg=sugg, lines=lines,
                           recs=recs, basis=table["basis"], transitions=sorted(variance.TRANSITIONS.get(cs["status"], [])),
                           reason_codes=variance.REASON_CODES, open_periods=open_periods, adjustments=adjustments,
                           names=rep_names(c), TYPE_LABELS=TYPE_LABELS)


@bp.post("/cases/<case_id>/update")
def case_update(case_id: str):
    try:
        variance.update_case(conn(), case_id, actor(), status=request.form.get("status") or None,
                             owner=request.form.get("owner"), reason_code=request.form.get("reason_code"),
                             resolution_note=request.form.get("resolution_note"))
        flash("Case updated (audited).", "ok")
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.case", case_id=case_id))


@bp.post("/cases/<case_id>/notes")
def case_note(case_id: str):
    try:
        variance.add_note(conn(), case_id, actor(), request.form.get("body", ""))
        flash("Note added.", "ok")
    except services.WorkflowError as exc:
        _flash_error(exc)
    return redirect(url_for("ui.case", case_id=case_id))


# ------------------------------------------------------------------ audit, policy, exports

@bp.get("/audit")
def audit():
    c = conn()
    action = request.args.get("action", "")
    sql, args = "SELECT * FROM audit_log", []
    if action:
        sql += " WHERE action = ?"
        args.append(action)
    rows = [dict(r) for r in c.execute(sql + " ORDER BY seq DESC LIMIT 300", args)]
    for r in rows:
        r["details"] = json.loads(r["details_json"])
    actions = [r[0] for r in c.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]
    return render_template("audit.html", rows=rows, chain=db.verify_audit_chain(c), actions=actions, action=action)


@bp.get("/policy")
def policy():
    c = conn()
    plans = [dict(r) for r in c.execute("SELECT * FROM plan_versions ORDER BY plan_id, currency, version")]
    assignments = [dict(r) for r in c.execute("SELECT * FROM plan_assignments ORDER BY rep_id, effective_from")]
    return render_template("policy.html", plans=plans, assignments=assignments, names=rep_names(c),
                           policy=services.POLICY_SUMMARY)


@bp.get("/exports/<p>/<name>")
def export(p: str, name: str):
    _require_period(p)
    c = conn()
    rep = request.args.get("rep") or None
    ccy = request.args.get("ccy") or None
    try:
        if name in ("statement.csv", "statement.html"):
            model = exports.statement_model(c, p, rep, ccy)
            body = exports.statement_csv(model) if name.endswith(".csv") else exports.statement_html(model)
        elif name in ("control.csv", "control.html"):
            rep_ = exports.control_report(c, p)
            body = exports.control_csv(rep_) if name.endswith(".csv") else exports.control_html(rep_)
        else:
            abort(404)
    except services.WorkflowError as exc:
        _flash_error(exc)
        return redirect(url_for("ui.period", p=p))
    stem = name.rsplit(".", 1)[0]
    suffix = "".join(f"_{x}" for x in (rep, ccy) if x and all(ch.isalnum() or ch in "-_" for ch in x))
    ext = name.rsplit(".", 1)[1]
    mime = "text/csv; charset=utf-8" if ext == "csv" else "text/html; charset=utf-8"
    disp = "attachment" if ext == "csv" or request.args.get("download") else "inline"
    return Response(body, content_type=mime,
                    headers={"Content-Disposition": f'{disp}; filename="rcw_{stem}_{p}{suffix}.{ext}"'})


def _error_page(code: int, message: str):
    try:
        return render_template("error.html", code=code, message=message), code
    except Exception:  # e.g. untrusted Host header: no URL adapter exists, so templates cannot build links (D-005)
        return Response(f"{code}: {message}\n", status=code, content_type="text/plain; charset=utf-8")


@bp.app_errorhandler(404)
def not_found(_e):
    return _error_page(404, "Not found - check the link or go back to the overview.")


@bp.app_errorhandler(400)
def bad_request(e):
    return _error_page(400, getattr(e, "description", None) or "Bad request")


@bp.app_errorhandler(413)
def too_large(_e):
    return _error_page(413, "File too large (max 5 MB).")
