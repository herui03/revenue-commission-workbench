"""Statement and control/variance exports (CSV + self-contained HTML), stdlib only.

* A CLOSED period is exported only from its stored snapshot JSON, so the output is
  byte-identical no matter what is imported, re-planned or resolved later.
* Text cells that a spreadsheet could execute (= + - @ tab CR) are prefixed with an
  apostrophe. Money is written as plain decimal numbers (e.g. -80.00) and is not escaped.
* All HTML is escaped with html.escape.
"""
from __future__ import annotations

import csv
import html
import io
import sqlite3
from typing import Any

from .engine import BLOCKING
from .explain import TYPE_LABELS, explain_line
from .money import format_bps, format_minor, format_plain
from .repo import rep_names
from .services import compute, get_snapshot, is_closed, period_row
from .variance import variance_table

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class Num(str):
    """Marker: a machine-formatted number that must not be formula-escaped."""


def csv_safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Num):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    return "'" + s if s.startswith(FORMULA_PREFIXES) else s


def _csv(rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return buf.getvalue()


def statement_model(conn: sqlite3.Connection, period: str, rep_id: str | None = None,
                    currency: str | None = None) -> dict[str, Any]:
    if is_closed(conn, period):
        snap = get_snapshot(conn, period)
        if snap is None:
            return {"period": period, "status": "CLOSED", "lines": [], "totals": [], "excluded": [], "holds": [],
                    "names": {}, "meta": {"note": "closed without activity"}, "controls": [], "kpis": {}}
        names = {r: v.get("display_name", r) for r, v in snap.get("reps", {}).items()}
        meta = {"snapshot_sha256": snap["_integrity"]["sha256"], "integrity_verified": snap["_integrity"]["verified"],
                "closed_at": snap["closed_at"], "prepared_by": snap["prepared_by"], "reviewed_by": snap["reviewed_by"],
                "result_digest": snap["result_digest"], "engine_version": snap["engine_version"],
                "run_id": snap["run_id"]}
        model = {"period": period, "status": "CLOSED", "lines": snap["lines"], "totals": snap["totals"],
                 "excluded": snap["excluded"], "holds": [], "controls": snap["controls"], "kpis": snap["kpis"],
                 "names": names, "meta": meta}
    else:
        res = compute(conn, period)
        prow = period_row(conn, period)
        status = prow["status"] if prow else "OPEN"
        model = {"period": period, "status": "DRAFT" if status == "OPEN" else status, "lines": res.lines,
                 "totals": res.totals, "excluded": res.excluded, "holds": res.holds, "controls": res.controls,
                 "kpis": res.kpis, "names": rep_names(conn),
                 "meta": {"result_digest": res.result_digest, "engine_version": res.engine_version}}
    if rep_id:
        model["lines"] = [ln for ln in model["lines"] if ln["rep_id"] == rep_id]
        model["totals"] = [t for t in model["totals"] if t["rep_id"] == rep_id]
    if currency:
        model["lines"] = [ln for ln in model["lines"] if ln["currency"] == currency]
        model["totals"] = [t for t in model["totals"] if t["currency"] == currency]
    return model


STATEMENT_HEADER = ["period", "status", "rep_id", "rep_name", "currency", "line_type", "line_key", "event_id",
                    "original_event_id", "event_date", "contract_id", "original_period", "plan_id", "plan_version",
                    "split_pct", "credited", "credit_reversed", "below_threshold_portion", "above_threshold_portion",
                    "base_rate", "accelerator_rate", "exact_minor_units", "amount", "explanation"]


def statement_csv(model: dict[str, Any]) -> str:
    rows: list[list[Any]] = [STATEMENT_HEADER]
    for ln in model["lines"]:
        rows.append([model["period"], model["status"], ln["rep_id"], model["names"].get(ln["rep_id"], ln["rep_id"]),
                     ln["currency"], ln["line_type"], ln["line_key"], ln["event_id"], ln["original_event_id"],
                     ln["event_date"], ln["contract_id"], ln["original_period"], ln["plan_id"], ln["plan_version"],
                     Num(format_bps(ln["split_bps"]).rstrip("%")) if ln["split_bps"] is not None else None,
                     Num(format_plain(ln["credited_minor"])), Num(format_plain(ln["credit_reversed_minor"])),
                     Num(format_plain(ln["base_portion_minor"])), Num(format_plain(ln["accel_portion_minor"])),
                     Num(format_bps(ln["base_rate_bps"]).rstrip("%")) if ln["base_rate_bps"] is not None else None,
                     Num(format_bps(ln["accel_rate_bps"]).rstrip("%")) if ln["accel_rate_bps"] is not None else None,
                     ln["exact_amount"] if not ln["exact_amount"].startswith("~") else ln["exact_amount"],
                     Num(format_plain(ln["amount_minor"])), explain_line(ln)])
    rows.append([])
    rows.append(["period", "status", "rep_id", "rep_name", "currency", "credited_collections", "commission_earned",
                 "clawbacks", "late_adjustments", "manual_adjustments", "expected_payout"])
    for t in model["totals"]:
        rows.append([model["period"], model["status"], t["rep_id"], model["names"].get(t["rep_id"], t["rep_id"]),
                     t["currency"], Num(format_plain(t["credited_minor"])), Num(format_plain(t["earnings_minor"])),
                     Num(format_plain(t["clawbacks_minor"])), Num(format_plain(t["late_minor"])),
                     Num(format_plain(t["manual_minor"])), Num(format_plain(t["net_minor"]))])
    rows.append([])
    for k, v in sorted(model["meta"].items()):
        rows.append(["meta", k, v])
    rows.append(["meta", "disclaimer", "Synthetic demo. Commission is an invented cash-based policy, not revenue "
                 "recognition. No payment executed."])
    return _csv(rows)


_CSS = """
body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1c2330;margin:24px;max-width:1200px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:24px 0 8px}h3{font-size:14px;margin:16px 0 6px}
.banner{padding:8px 12px;border-radius:6px;margin:8px 0 16px;font-weight:600}
.closed{background:#e7f4ec;border:1px solid #3c8c5a}.draft{background:#fff4de;border:1px solid #c98a12}
table{border-collapse:collapse;width:100%;margin:6px 0 12px;font-size:12.5px}
th,td{border:1px solid #d5dae3;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f2f4f8}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.neg{color:#a1261b}.muted{color:#5b6576;font-size:12px}.pass{color:#2f7a4a}.fail{color:#a1261b;font-weight:600}
code{font-size:12px;word-break:break-all}
"""


def _e(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _money_td(minor: int, ccy: str) -> str:
    cls = "num neg" if minor < 0 else "num"
    return f'<td class="{cls}">{_e(format_minor(minor, ccy))}</td>'


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width,initial-scale=1\"><title>{_e(title)}</title><style>{_CSS}</style></head>"
            f"<body>{body}</body></html>\n")


def _banner(model: dict[str, Any]) -> str:
    meta = model["meta"]
    if model["status"] == "CLOSED":
        return (f'<div class="banner closed">CLOSED - frozen snapshot. SHA-256 <code>{_e(meta.get("snapshot_sha256"))}'
                f'</code> (integrity {"verified" if meta.get("integrity_verified") else "FAILED"}). Closed '
                f'{_e(meta.get("closed_at"))}; prepared by label {_e(meta.get("prepared_by"))}, reviewed by label '
                f'{_e(meta.get("reviewed_by"))}.</div>')
    return (f'<div class="banner draft">{_e(model["status"])} - not final. Result digest '
            f'<code>{_e(meta.get("result_digest"))}</code>. Numbers can change until the period is closed.</div>')


def statement_html(model: dict[str, Any]) -> str:
    out = [f"<h1>Commission statement - {_e(model['period'])}</h1>",
           '<p class="muted">Synthetic demo. Cash-based invented policy; not revenue recognition; '
           "no payment executed. Labels are not authenticated identities.</p>", _banner(model)]
    keys = sorted({(t["rep_id"], t["currency"]) for t in model["totals"]})
    if not keys:
        out.append("<p>No commission lines for this selection.</p>")
    for rep, ccy in keys:
        t = next(x for x in model["totals"] if x["rep_id"] == rep and x["currency"] == ccy)
        out.append(f"<h2>{_e(model['names'].get(rep, rep))} ({_e(rep)}) - {_e(ccy)}</h2>")
        out.append("<table><tr><th>Credited collections</th><th>Commission earned</th><th>Clawbacks</th>"
                   "<th>Late adjustments</th><th>Manual adjustments</th><th>Expected payout</th></tr><tr>"
                   + "".join(_money_td(t[k], ccy) for k in ("credited_minor", "earnings_minor", "clawbacks_minor",
                                                           "late_minor", "manual_minor", "net_minor"))
                   + "</tr></table>")
        out.append("<table><tr><th>Type</th><th>Event / date</th><th>Split</th><th>Credited</th>"
                   "<th>Explanation</th><th>Amount</th></tr>")
        for ln in (x for x in model["lines"] if x["rep_id"] == rep and x["currency"] == ccy):
            split = format_bps(ln["split_bps"]) if ln["split_bps"] is not None else ""
            credited = ln["credited_minor"] or -ln["credit_reversed_minor"]
            out.append(f"<tr><td>{_e(TYPE_LABELS[ln['line_type']])}</td><td>{_e(ln['event_id'] or '')}<br>"
                       f"<span class=muted>{_e(ln['event_date'] or '')}</span></td><td>{_e(split)}</td>"
                       f"{_money_td(credited, ccy)}<td>{_e(explain_line(ln))}</td>"
                       f"{_money_td(ln['amount_minor'], ccy)}</tr>")
        out.append("</table>")
    if model["excluded"]:
        out.append("<h2>Excluded events (reviewer decision, not paid)</h2><table><tr><th>Event</th><th>Date</th>"
                   "<th>Amount</th><th>Reason</th><th>Decided by</th></tr>")
        for e in model["excluded"]:
            out.append(f"<tr><td>{_e(e['event_id'])}</td><td>{_e(e['event_date'])}</td>"
                       f"{_money_td(e['amount_minor'], e['currency'])}<td>{_e(e['reason'])}</td>"
                       f"<td>{_e(e['decided_by'])}</td></tr>")
        out.append("</table>")
    blocking = [h for h in model["holds"] if h["severity"] == BLOCKING]
    if blocking:
        out.append(f"<h2>Blocking holds ({len(blocking)}) - excluded from the numbers above</h2><ul>")
        out.extend(f"<li><b>{_e(h['code'])}</b> {_e(h['message'])}</li>" for h in blocking)
        out.append("</ul>")
    return _page(f"Statement {model['period']}", "\n".join(out))


def control_report(conn: sqlite3.Connection, period: str) -> dict[str, Any]:
    model = statement_model(conn, period)
    var = variance_table(conn, period)
    return {"model": model, "variance": var}


def control_csv(report: dict[str, Any]) -> str:
    """One tidy table. `basis` says where each number comes from:
    FROZEN = closed snapshot (never changes), DRAFT = current calculation, LIVE = register/cases as of export."""
    model, var = report["model"], report["variance"]
    calc_basis = "FROZEN" if model["status"] == "CLOSED" else "DRAFT"
    rows: list[list[Any]] = [["section", "basis", "item", "currency", "metric", "value", "note"]]
    rows.append(["status", calc_basis, "period", "", "period_status", model["status"],
                 model["meta"].get("snapshot_sha256") or model["meta"].get("result_digest")])
    for c in model["controls"]:
        rows.append(["control", calc_basis, c["name"], "", "passed", c["passed"],
                     c["detail"] if isinstance(c["detail"], str) else "see HTML report"])
    for ccy, k in model["kpis"].items():
        for metric, value in k.items():
            rows.append(["kpi", calc_basis, "period", ccy, metric,
                         value if metric.endswith("count") else Num(format_plain(value)), ""])
    for h in model["holds"]:
        rows.append(["hold", calc_basis, h["code"], h.get("currency") or "", h["severity"],
                     Num(format_plain(h["amount_minor"])) if h.get("amount_minor") is not None else None, h["message"]])
    for r in var["rows"]:
        rows.append(["expected_payout", calc_basis, r["rep_id"], r["currency"], "expected",
                     Num(format_plain(r["expected_minor"])), ""])
    for r in var["rows"]:
        rows.append(["recorded_payout", "LIVE", r["rep_id"], r["currency"], "recorded",
                     Num(format_plain(r["recorded_minor"])), "payout register as of export"])
        rows.append(["variance", "LIVE", r["rep_id"], r["currency"], "recorded_minus_expected",
                     Num(format_plain(r["variance_minor"])), ""])
    for r in var["rows"]:
        c = r["case"]
        if c:
            rows.append(["case_status", "LIVE", c["case_id"], r["currency"], "status", c["status"],
                         f"rep={c['rep_id']}; owner={c['owner'] or ''}; reason={c['reason_code'] or ''}; "
                         "resolved means explained - not corrected or paid"])
    return _csv(rows)


def control_html(report: dict[str, Any]) -> str:
    model, var = report["model"], report["variance"]
    out = [f"<h1>Control &amp; variance report - {_e(model['period'])}</h1>", _banner(model),
           "<h2>Control checks</h2><table><tr><th>Check</th><th>Result</th><th>Detail</th></tr>"]
    for c in model["controls"]:
        detail = c["detail"] if isinstance(c["detail"], str) else _recon_html(c["detail"])
        out.append(f"<tr><td>{_e(c['name'])}</td><td class=\"{'pass' if c['passed'] else 'fail'}\">"
                   f"{'PASS' if c['passed'] else 'FAIL'}</td><td>{detail if not isinstance(c['detail'], str) else _e(detail)}"
                   "</td></tr>")
    out.append("</table><h2>Period metrics (per currency, never combined)</h2>"
               "<table><tr><th>Currency</th><th>Bookings signed</th><th>Cash collected</th><th>Refunds</th>"
               "<th>Net cash</th><th>Credited to reps</th><th>Commission earned</th><th>Clawbacks</th>"
               "<th>Late adj.</th><th>Manual adj.</th><th>Expected payouts</th></tr>")
    for ccy, k in model["kpis"].items():
        out.append(f"<tr><td>{_e(ccy)}</td>" + "".join(_money_td(k[m], ccy) for m in (
            "bookings_minor", "cash_collected_minor", "refunds_minor", "net_cash_minor", "credited_minor",
            "earnings_minor", "clawbacks_minor", "late_minor", "manual_minor", "net_payable_minor")) + "</tr>")
    out.append("</table>")
    if model["holds"]:
        out.append("<h2>Holds</h2><ul>" + "".join(f"<li><b>{_e(h['code'])}</b> ({_e(h['severity'])}) "
                                                   f"{_e(h['message'])}</li>" for h in model["holds"]) + "</ul>")
    out.append(f"<h2>Expected vs recorded payouts (expected basis: {_e(var['basis'])})</h2>"
               "<table><tr><th>Rep</th><th>Currency</th><th>Expected</th><th>Recorded</th>"
               "<th>Variance (recorded - expected)</th></tr>")
    for r in var["rows"]:
        out.append(f"<tr><td>{_e(r['rep_id'])}</td><td>{_e(r['currency'])}</td>"
                   + _money_td(r["expected_minor"], r["currency"]) + _money_td(r["recorded_minor"], r["currency"])
                   + _money_td(r["variance_minor"], r["currency"]) + "</tr>")
    out.append("</table><h2>Investigation status (live as of export - not part of any frozen snapshot)</h2>"
               "<p class=muted>Resolved means the variance is explained. It does not mean money was corrected or "
               "paid.</p><table><tr><th>Case</th><th>Rep</th><th>Currency</th><th>Status</th><th>Owner</th>"
               "<th>Reason code</th></tr>")
    for r in var["rows"]:
        c = r["case"]
        if c:
            out.append(f"<tr><td>{_e(c['case_id'])}</td><td>{_e(c['rep_id'])}</td><td>{_e(c['currency'])}</td>"
                       f"<td>{_e(c['status'])}</td><td>{_e(c['owner'] or '')}</td><td>{_e(c['reason_code'] or '')}"
                       "</td></tr>")
    out.append("</table>")
    return _page(f"Control report {model['period']}", "\n".join(out))


def _recon_html(detail: dict[str, Any]) -> str:
    parts = []
    for ccy, per in sorted(detail.items()):
        for etype, b in sorted(per.items()):
            parts.append(f"{_e(ccy)} {_e(etype)}: dated {_e(format_minor(b['dated_in_period'], ccy))} = processed "
                         f"{_e(format_minor(b['processed'], ccy))} + held {_e(format_minor(b['held'], ccy))} + excluded "
                         f"{_e(format_minor(b['excluded'], ccy))}")
    return "<br>".join(parts) or "no cash dated in period"
