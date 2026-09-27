"""Command-line interface (stdlib only). Shares every rule with the web UI via rcw.services."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import db, demo, exports, importer, services, variance
from .money import MoneyFormatError, format_minor, parse_amount
from .repo import activity_periods, rep_names

DEFAULT_DB = os.environ.get("RCW_DB", "instance/workbench.db")


def _conn(path: str):
    conn = db.connect(path)
    db.init_db(conn)
    return conn


def _print_result_table(conn, period: str) -> None:
    model = exports.statement_model(conn, period)
    names = model["names"]
    print(f"\n{period}  status={model['status']}")
    print(f"  {'rep':<22} {'ccy':<4} {'credited':>14} {'earned':>11} {'clawback':>11} {'late':>10} {'manual':>10} "
          f"{'expected':>11}")
    for t in model["totals"]:
        print(f"  {names.get(t['rep_id'], t['rep_id']):<22} {t['currency']:<4} "
              f"{format_minor(t['credited_minor'], ascii_minus=True):>14} "
              f"{format_minor(t['earnings_minor'], ascii_minus=True):>11} "
              f"{format_minor(t['clawbacks_minor'], ascii_minus=True):>11} "
              f"{format_minor(t['late_minor'], ascii_minus=True):>10} "
              f"{format_minor(t['manual_minor'], ascii_minus=True):>10} "
              f"{format_minor(t['net_minor'], ascii_minus=True):>11}")
    for h in model["holds"]:
        print(f"  HOLD {h['severity']:<8} {h['code']}: {h['message']}")


def cmd_demo(args) -> int:
    out = Path(args.out)
    conn = demo.fresh_db(args.db)
    print("Revenue & Commission Operations Workbench - scripted demo (synthetic data, offline)\n")
    ev = demo.play_full_story(conn, out, say=lambda s: print("  " + s))
    for p in ("2026-04", "2026-05"):
        _print_result_table(conn, p)
    print("\nVariance cases (recorded - expected):")
    names = rep_names(conn)
    for c in ev["cases"]:
        print(f"  {c['case_id']} {c['period']} {names.get(c['rep_id'], c['rep_id']):<20} {c['currency']} "
              f"expected {format_minor(c['expected_minor'], ascii_minus=True):>9} "
              f"recorded {format_minor(c['recorded_minor'], ascii_minus=True):>9} "
              f"variance {format_minor(c['variance_minor'], ascii_minus=True, signed=True):>9}  {c['status']}")
    print(f"\nApril statement SHA-256 before later changes: {ev['april_statement_sha256_before']}")
    print(f"April statement SHA-256 after  later changes: {ev['april_statement_sha256_after']}")
    print(f"April export byte-identical: {ev['april_export_identical']}")
    print(f"Audit chain: {'OK' if ev['audit']['ok'] else 'BROKEN'} ({ev['audit']['entries']} entries)")
    evidence = {k: v for k, v in ev.items() if k != "audit"}
    evidence["audit_chain"] = ev["audit"]
    (out / "story_evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(f"\nExports and story_evidence.json written to {out}/ ; database at {args.db}")
    print("Next: `python -m rcw serve` (needs Flask) and open http://127.0.0.1:5057")
    return 0 if ev["april_export_identical"] and ev["audit"]["ok"] else 1


def cmd_import(args) -> int:
    conn = _conn(args.db)
    data = Path(args.file).read_bytes()
    r = importer.import_csv(conn, args.kind, Path(args.file).name, data, mode=args.mode, actor=args.actor)
    print(r.summary())
    for fe in r.file_errors:
        print(f"  FILE {fe['code']}: {fe['message']}")
    for row in r.rows:
        if row["outcome"] == "QUARANTINED":
            print(f"  row {row['source_row']} {row['key']}: " + "; ".join(f"{x['code']} ({x['message']})"
                                                                     for x in row["reasons"]))
    print(json.dumps(r.control, indent=2))
    return 0 if r.status != "REJECTED" else 2


def cmd_calc(args) -> int:
    conn = _conn(args.db)
    out = services.calculate_period(conn, args.period, args.actor)
    print(f"run #{out.run_id} digest {out.result.result_digest[:16]}... lines {len(out.result.lines)} "
          f"blocking holds {len(out.result.blocking_holds)}" + (" (review withdrawn)" if out.review_withdrawn else ""))
    _print_result_table(conn, args.period)
    return 0


def _wf(fn):
    def run(args) -> int:
        try:
            return fn(args) or 0
        except services.WorkflowError as exc:
            print(f"REFUSED {exc.code}: {exc.message}", file=sys.stderr)
            if exc.details:
                print(json.dumps(exc.details, indent=2, default=str)[:4000], file=sys.stderr)
            return 3
    return run


@_wf
def cmd_submit(args):
    run_id = services.submit_for_review(_conn(args.db), args.period, args.actor)
    print(f"{args.period} submitted for review (run #{run_id})")


@_wf
def cmd_close(args):
    res = services.close_period(_conn(args.db), args.period, args.actor)
    print(f"{args.period} closed: run #{res['run_id']} snapshot {res['snapshot_sha256']} payables {res['payables']}")


@_wf
def cmd_decide(args):
    posting = services.decide_event(_conn(args.db), args.event_id, {"post": "POST_LATE", "exclude": "EXCLUDE"}[
        args.decision], args.reason, args.actor)
    print(f"decision recorded; posting period {posting}")


@_wf
def cmd_adjust(args):
    try:
        amount = parse_amount(args.amount, allow_negative=True)
    except MoneyFormatError as exc:
        raise services.WorkflowError("BAD_AMOUNT", str(exc)) from exc
    adj = services.add_adjustment(_conn(args.db), args.period, args.rep, args.currency, amount, args.category,
                                  args.reason, args.actor, args.case)
    print(f"adjustment #{adj} recorded")


@_wf
def cmd_business_date(args):
    services.set_business_date(_conn(args.db), args.date, args.actor)
    print(f"business date set to {args.date}")


@_wf
def cmd_statement(args):
    conn = _conn(args.db)
    model = exports.statement_model(conn, args.period, args.rep, args.currency)
    text = exports.statement_csv(model) if args.format == "csv" else exports.statement_html(model)
    _emit(text, args.out)


@_wf
def cmd_control(args):
    conn = _conn(args.db)
    rep = exports.control_report(conn, args.period)
    _emit(exports.control_csv(rep) if args.format == "csv" else exports.control_html(rep), args.out)


@_wf
def cmd_variance(args):
    conn = _conn(args.db)
    if args.refresh:
        print(variance.refresh_variance(conn, args.period, args.actor))
    t = variance.variance_table(conn, args.period)
    print(f"expected basis: {t['basis']}")
    for r in t["rows"]:
        c = r["case"] or {}
        print(f"  {r['rep_id']:<12} {r['currency']} expected {format_minor(r['expected_minor'], ascii_minus=True):>10} "
              f"recorded {format_minor(r['recorded_minor'], ascii_minus=True):>10} "
              f"variance {format_minor(r['variance_minor'], ascii_minus=True, signed=True):>10} "
              f"{c.get('case_id', '')} {c.get('status', '')}")


def cmd_status(args) -> int:
    conn = _conn(args.db)
    print(f"business date: {services.business_date(conn)}")
    for p in activity_periods(conn):
        row = services.period_row(conn, p)
        status = "CLOSED" if services.is_closed(conn, p) else (row["status"] if row else "OPEN")
        print(f"  {p}: {status}")
    chain = db.verify_audit_chain(conn)
    print(f"audit chain: {'OK' if chain['ok'] else 'BROKEN at ' + str(chain['broken_at_seq'])} "
          f"({chain['entries']} entries)")
    return 0


def cmd_reset_demo(args) -> int:
    """DESTRUCTIVE (explicit only): back up the database file, then recreate it with demo stage 1."""
    if not args.yes:
        print("Refusing to reset without --yes. This replaces the demo database (a backup copy is kept).",
              file=sys.stderr)
        return 5
    db_file = Path(args.db)
    if db_file.exists():
        import shutil
        from datetime import datetime
        backup_dir = db_file.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"{db_file.stem}-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db_file.suffix}"
        shutil.copy2(db_file, backup)
        print(f"Backup of the old database: {backup}")
    conn = demo.fresh_db(db_file)
    for r in demo.load_stage(conn, 1):
        print("  " + r.summary())
    conn.close()
    print(f"Demo database reset to stage 1 at {db_file}")
    return 0


def cmd_serve(args) -> int:
    try:
        from .web import create_app
    except ImportError as exc:  # pragma: no cover - depends on environment
        print(f"Flask is required for the web UI ({exc}). Install with: pip install -r requirements.txt",
              file=sys.stderr)
        return 4
    db_file = Path(args.db)
    if args.reset_demo:
        if not args.yes:
            print("Refusing to reset without --yes (this deletes the demo database).", file=sys.stderr)
            return 5
        conn = demo.fresh_db(db_file)
        for r in demo.load_stage(conn, 1):
            print(r.summary())
        conn.close()
        print(f"Demo database reset and re-seeded at {db_file}")
    elif args.seed_if_missing and not db_file.exists():
        conn = demo.fresh_db(db_file)
        for r in demo.load_stage(conn, 1):
            print(r.summary())
        conn.close()
        print(f"New demo database seeded at {db_file}")
    elif db_file.exists():
        print(f"Using existing database {db_file} (your earlier work is kept)")
    app = create_app(args.db)
    print(f"Serving on http://127.0.0.1:{args.port} (localhost only). Ctrl+C to stop.")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


def _emit(text: str, out: str | None) -> None:
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
        print(f"wrote {out}")
    else:
        sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rcw", description="Revenue & Commission Operations Workbench (synthetic demo)")
    p.add_argument("--db", default=DEFAULT_DB, help=f"SQLite file (default {DEFAULT_DB}; env RCW_DB)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("demo", help="reset a demo DB and play the whole story offline")
    s.add_argument("--out", default="out/demo")
    s.set_defaults(fn=cmd_demo)

    s = sub.add_parser("serve", help="run the web UI on 127.0.0.1")
    s.add_argument("--port", type=int, default=5057)
    s.add_argument("--seed-if-missing", action="store_true",
                   help="if the database file does not exist yet, create it with demo stage 1 (never resets)")
    s.add_argument("--reset-demo", action="store_true", help="DESTRUCTIVE: delete the database and re-seed stage 1")
    s.add_argument("--yes", action="store_true", help="confirm --reset-demo")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("reset-demo", help="DESTRUCTIVE: back up, then recreate the database with demo stage 1")
    s.add_argument("--yes", action="store_true", help="required confirmation")
    s.set_defaults(fn=cmd_reset_demo)

    s = sub.add_parser("import", help="import a CSV")
    s.add_argument("kind", choices=sorted(importer.FILE_KINDS))
    s.add_argument("file")
    s.add_argument("--mode", choices=["strict", "quarantine"], default="quarantine")
    s.add_argument("--actor", default="analyst-1")
    s.set_defaults(fn=cmd_import)

    for name, fn, helptext in (("calc", cmd_calc, "calculate a draft"), ("submit", cmd_submit, "submit for review"),
                               ("close", cmd_close, "close (freeze) a reviewed period")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("period")
        s.add_argument("--actor", default="analyst-1" if name != "close" else "manager-1")
        s.set_defaults(fn=fn)

    s = sub.add_parser("decide", help="post a late event or exclude an event")
    s.add_argument("event_id")
    s.add_argument("decision", choices=["post", "exclude"])
    s.add_argument("--reason", required=True)
    s.add_argument("--actor", default="manager-1")
    s.set_defaults(fn=cmd_decide)

    s = sub.add_parser("adjust", help="manual adjustment in an open period")
    s.add_argument("period")
    s.add_argument("rep")
    s.add_argument("currency")
    s.add_argument("amount", help="e.g. -80.00")
    s.add_argument("--category", default="COMMISSION_ADJUSTMENT",
                   choices=["COMMISSION_ADJUSTMENT", "PAYOUT_CORRECTION"])
    s.add_argument("--reason", required=True)
    s.add_argument("--case")
    s.add_argument("--actor", default="analyst-1")
    s.set_defaults(fn=cmd_adjust)

    s = sub.add_parser("business-date", help="set the workbench business date (forward only)")
    s.add_argument("date")
    s.add_argument("--actor", default="analyst-1")
    s.set_defaults(fn=cmd_business_date)

    for name, fn in (("statement", cmd_statement), ("control-report", cmd_control)):
        s = sub.add_parser(name)
        s.add_argument("period")
        s.add_argument("--format", choices=["csv", "html"], default="csv")
        s.add_argument("--out")
        if name == "statement":
            s.add_argument("--rep")
            s.add_argument("--currency")
        s.set_defaults(fn=fn)

    s = sub.add_parser("variance", help="show (and optionally refresh) variance cases")
    s.add_argument("period")
    s.add_argument("--refresh", action="store_true")
    s.add_argument("--actor", default="ops-1")
    s.set_defaults(fn=cmd_variance)

    s = sub.add_parser("status")
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)
