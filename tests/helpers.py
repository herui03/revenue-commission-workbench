"""Test helpers. Expected answers live in tests/expected/*.json and are typed by hand."""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rcw import db, exports, importer, services

EXPECTED = json.loads((Path(__file__).parent / "expected" / "hand_calculations.json").read_text(encoding="utf-8"))
FIX = EXPECTED["fixtures"]
CASH_HEADER = FIX["cash_header"]
PREPARER, REVIEWER = "analyst-1", "manager-1"


def fixed_clock() -> None:
    counter = itertools.count()
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.set_clock(lambda: (start + timedelta(seconds=next(counter))).isoformat().replace("+00:00", "Z"))


def new_conn(business_date: str = "2026-07-15"):
    fixed_clock()
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.set_setting(conn, "business_date", business_date)
    return conn


_counter = itertools.count()


def imp(conn, kind: str, text: str, *, mode: str = "quarantine", name: str | None = None) -> importer.ImportResult:
    return importer.import_csv(conn, kind, name or f"{kind}_{next(_counter)}.csv", text.encode("utf-8"), mode=mode,
                               actor="test")


def load_standard(conn) -> None:
    for kind in ("reps", "plans", "assignments", "contracts", "splits"):
        r = imp(conn, kind, FIX["standard"][kind])
        assert r.status == "COMMITTED", (kind, r.summary(), r.rows)


def cash(conn, rows: list[str], **kw) -> importer.ImportResult:
    return imp(conn, "cash_events", CASH_HEADER + "".join(r + "\n" for r in rows), **kw)


def close(conn, period: str) -> dict[str, Any]:
    services.calculate_period(conn, period, PREPARER)
    services.submit_for_review(conn, period, PREPARER)
    return services.close_period(conn, period, REVIEWER)


def lines_for(conn, period: str) -> list[dict[str, Any]]:
    if services.is_closed(conn, period):
        return services.get_snapshot(conn, period)["lines"]
    return services.compute(conn, period).lines


def totals_for(conn, period: str) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for t in exports.statement_model(conn, period)["totals"]:
        out.setdefault(t["rep_id"], {})[t["currency"]] = t["net_minor"]
    return out


FIELD_MAP = {"credited": "credited_minor", "base_portion": "base_portion_minor", "accel_portion": "accel_portion_minor",
             "accel_bps": "accel_rate_bps", "plan_version": "plan_version", "split_bps": "split_bps",
             "credit_reversed": "credit_reversed_minor", "original_event": "original_event_id",
             "original_period": "original_period", "amount": "amount_minor"}


def find_line(lines, ltype: str, event: str, rep: str) -> dict[str, Any] | None:
    for ln in lines:
        if ln["line_type"] == ltype and ln["event_id"] == event and ln["rep_id"] == rep:
            return ln
    return None


def financial_view(lines: list[dict[str, Any]]) -> list[tuple]:
    """Money-relevant projection of lines (drops provenance such as batch id / source row)."""
    return sorted((ln["line_key"], ln["period"], ln["original_period"], ln["amount_minor"], ln["credited_minor"],
                   ln["credit_reversed_minor"], ln["base_portion_minor"], ln["accel_portion_minor"],
                   ln["base_rate_bps"], ln["accel_rate_bps"], ln["plan_version"], ln["split_bps"])
                  for ln in lines)


def run_scenario(sc: dict[str, Any]):
    """Execute a hand-calculation scenario through the real importer, engine and workflow."""
    conn = new_conn(sc.get("business_date", "2026-07-15"))
    load_standard(conn)
    for extra in sc.get("extra_imports", []):
        imp(conn, extra["kind"], FIX[extra["fixture"]])
    r = cash(conn, sc["cash"])
    assert r.quarantined == 0, r.rows
    tags: dict[str, str] = {}
    for step in sc["steps"]:
        a = step["action"]
        if a == "calculate":
            services.calculate_period(conn, step["period"], PREPARER)
        elif a == "close":
            close(conn, step["period"])
        elif a == "snapshot_export":
            m = exports.statement_model(conn, step["period"])
            tags[step["tag"]] = exports.statement_csv(m) + exports.statement_html(m)
        elif a == "assert_export_unchanged":
            m = exports.statement_model(conn, step["period"])
            assert tags[step["tag"]] == exports.statement_csv(m) + exports.statement_html(m), "export changed"
        elif a == "import":
            rr = imp(conn, step["kind"], FIX[step["fixture"]])
            assert rr.quarantined == 0, rr.rows
        elif a == "import_cash":
            rr = cash(conn, step["rows"])
            assert rr.quarantined == 0, rr.rows
        elif a == "expect_hold":
            res = services.compute(conn, step["period"])
            assert any(h["code"] == step["code"] and h["event_id"] == step["event"] for h in res.holds), res.holds
        elif a == "expect_close_error":
            try:
                services.submit_for_review(conn, step["period"], PREPARER)
                services.close_period(conn, step["period"], REVIEWER)
            except services.WorkflowError as exc:
                assert exc.code == step["code"], exc
            else:
                raise AssertionError("close unexpectedly succeeded")
        elif a == "decide":
            services.decide_event(conn, step["event"], step["decision"], step["reason"], REVIEWER)
        else:
            raise AssertionError(f"unknown step {a}")
    return conn
