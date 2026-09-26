"""Bundled synthetic demo: loaders shared by the CLI and the web UI, plus the full scripted story."""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Callable

from . import db, exports, importer, services, variance

DEMO_DIR = Path(__file__).resolve().parent.parent / "data" / "demo"
STAGE_DATES = {1: "2026-05-06", 2: "2026-06-04"}
KIND_BY_FILE = {
    "reps": "reps", "plans": "plans", "plans_v2": "plans", "assignments": "assignments", "contracts": "contracts",
    "splits": "splits", "cash_events": "cash_events", "recorded_payouts": "recorded_payouts",
}
PREPARER, REVIEWER, PAYROLL = "analyst-1", "manager-1", "ops-1"
SYSTEM = "system-demo"


def kind_for(path: Path) -> str:
    stem = path.stem.split("_", 1)[1]  # "06_cash_events" -> "cash_events"
    return KIND_BY_FILE[stem]


def fresh_db(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    for suffix in ("", "-wal", "-shm", "-journal"):
        q = Path(str(p) + suffix)
        if q.exists():
            q.unlink()
    conn = db.connect(p)
    db.init_db(conn)
    return conn


def load_stage(conn: sqlite3.Connection, stage: int, actor: str = SYSTEM) -> list[importer.ImportResult]:
    services.set_business_date(conn, STAGE_DATES[stage], actor)
    results = []
    for f in sorted((DEMO_DIR / f"stage{stage}").glob("*.csv")):
        results.append(importer.import_csv(conn, kind_for(f), f.name, f.read_bytes(), mode="quarantine", actor=actor))
    with db.tx(conn):
        db.set_setting(conn, "demo_stage", str(stage))
        db.audit(conn, actor, "DEMO_STAGE_LOADED", "settings", "demo_stage", {"stage": stage})
    return results


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def play_full_story(conn: sqlite3.Connection, out_dir: Path | None = None,
                    say: Callable[[str], None] = lambda s: None) -> dict[str, Any]:
    """Run the whole demo story end to end. Returns evidence used by the CLI and tests."""
    ev: dict[str, Any] = {"steps": []}

    def step(msg: str) -> None:
        ev["steps"].append(msg)
        say(msg)

    def write(name: str, text: str) -> None:
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / name).write_text(text, encoding="utf-8")

    for r in load_stage(conn, 1):
        step(f"[stage 1 import] {r.filename}: {r.summary()}")
    run = services.calculate_period(conn, "2026-04", PREPARER)
    step(f"[April] calculated run #{run.run_id}: {len(run.result.lines)} lines, "
         f"{len(run.result.blocking_holds)} blocking holds")
    services.submit_for_review(conn, "2026-04", PREPARER)
    step(f"[April] submitted for review by {PREPARER}")
    closed = services.close_period(conn, "2026-04", REVIEWER)
    step(f"[April] closed by {REVIEWER}; snapshot sha256 {closed['snapshot_sha256'][:16]}...")
    april_before = {
        "statement_csv": exports.statement_csv(exports.statement_model(conn, "2026-04")),
        "statement_html": exports.statement_html(exports.statement_model(conn, "2026-04")),
    }
    write("2026-04_statement.csv", april_before["statement_csv"])
    write("2026-04_statement.html", april_before["statement_html"])
    cases = {c["rep_id"]: c for c in conn.execute("SELECT * FROM variance_cases WHERE period = '2026-04'")}
    cedar = cases.get("REP-CEDAR")
    if cedar:
        variance.update_case(conn, cedar["case_id"], PAYROLL, owner=PAYROLL, status="INVESTIGATING")
        variance.add_note(conn, cedar["case_id"], PAYROLL, "Payroll register shows 620.37 = 5% of all credited "
                          "cash; checking whether the accelerator was keyed.")
        step(f"[April] variance case {cedar['case_id']} (Cedar USD {cedar['variance_minor']} minor units) "
             "assigned and under investigation")

    for r in load_stage(conn, 2):
        step(f"[stage 2 import] {r.filename}: {r.summary()}")
    run = services.calculate_period(conn, "2026-05", PREPARER)
    holds = [h["code"] + ":" + str(h["event_id"]) for h in run.result.blocking_holds]
    step(f"[May] calculated run #{run.run_id}: blocking holds {holds}")
    try:
        services.submit_for_review(conn, "2026-05", PREPARER)
    except services.WorkflowError as exc:
        step(f"[May] submit refused as expected: {exc.code}")
        ev["may_submit_refused"] = exc.code
    services.decide_event(conn, "E-2026-0431", "POST_LATE", "Bank confirmed receipt on 29 Apr; April is closed so "
                          "post as a prior-period adjustment in May", REVIEWER)
    step("[May] late April receipt E-2026-0431 posted into May as a prior-period adjustment (reason recorded)")
    run = services.calculate_period(conn, "2026-05", PREPARER)
    step(f"[May] recalculated run #{run.run_id}: {len(run.result.lines)} lines, "
         f"{len(run.result.blocking_holds)} blocking holds")
    services.submit_for_review(conn, "2026-05", PREPARER)
    closed_may = services.close_period(conn, "2026-05", REVIEWER)
    step(f"[May] closed by {REVIEWER}; snapshot sha256 {closed_may['snapshot_sha256'][:16]}...")

    if cedar:
        variance.update_case(conn, cedar["case_id"], PAYROLL, status="RESOLVED", reason_code="RATE_ERROR",
                             resolution_note="Register applied 5% to all credit; accelerator uplift 72.22 missing. "
                                             "Correction to be handled in a future payout (not done here).")
        step(f"[April] case {cedar['case_id']} resolved (explained, not paid) after close")
    april_after_csv = exports.statement_csv(exports.statement_model(conn, "2026-04"))
    april_after_html = exports.statement_html(exports.statement_model(conn, "2026-04"))
    ev["april_statement_sha256_before"] = sha256_text(april_before["statement_csv"])
    ev["april_statement_sha256_after"] = sha256_text(april_after_csv)
    ev["april_html_identical"] = april_before["statement_html"] == april_after_html
    ev["april_export_identical"] = april_before["statement_csv"] == april_after_csv and ev["april_html_identical"]
    step(f"[April] re-exported after plan v2, late data, May close and case resolution: identical = "
         f"{ev['april_export_identical']}")

    for p in ("2026-04", "2026-05"):
        write(f"{p}_statement.csv" if p != "2026-04" else "2026-04_statement_reexport.csv",
              exports.statement_csv(exports.statement_model(conn, p)))
        if p == "2026-05":
            write(f"{p}_statement.html", exports.statement_html(exports.statement_model(conn, p)))
        rep = exports.control_report(conn, p)
        write(f"{p}_control_variance.csv", exports.control_csv(rep))
        write(f"{p}_control_variance.html", exports.control_html(rep))
    ev["payables"] = [dict(r) for r in conn.execute("SELECT period, rep_id, currency, amount_minor FROM payables "
                                                    "ORDER BY period, rep_id, currency")]
    ev["cases"] = [dict(r) for r in conn.execute("SELECT case_id, period, rep_id, currency, expected_minor, "
                                                 "recorded_minor, variance_minor, status FROM variance_cases "
                                                 "ORDER BY case_id")]
    ev["audit"] = db.verify_audit_chain(conn)
    return ev
