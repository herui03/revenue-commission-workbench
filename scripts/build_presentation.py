"""Build the offline, self-contained DEMO REPLAY page: docs/presentation/walkthrough.html

Every number shown is read from run artifacts in docs/evidence/ (produced by
scripts/refresh_evidence.sh); every image is an embedded screenshot from the Playwright run.
Nothing here recomputes or invents results. The page says so at the top.

    python3 scripts/build_presentation.py
"""
from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EV = ROOT / "docs" / "evidence"
OUT = ROOT / "docs" / "presentation" / "walkthrough.html"


def load(name: str):
    p = EV / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def img(name: str, alt: str) -> str:
    p = EV / "screenshots" / f"{name}.png"
    if not p.exists():
        return f'<p class="missing">Screenshot {html.escape(name)} not captured in this run.</p>'
    data = base64.b64encode(p.read_bytes()).decode()
    return (f'<figure><img loading="lazy" src="data:image/png;base64,{data}" alt="{html.escape(alt)}">'
            f'<figcaption>{html.escape(alt)} — screenshot <code>{html.escape(name)}.png</code></figcaption></figure>')


def money(minor: int) -> str:
    sign = "−" if minor < 0 else ""
    units, frac = divmod(abs(minor), 100)
    return f"{sign}{units:,}.{frac:02d}"


def main() -> int:
    story = load("demo_exports/story_evidence.json") or {}
    e2e = load("e2e_results.json") or {}
    bench = load("benchmark.json") or {}
    ci = load("ci_run.json") or {}
    tests_txt = (EV / "test_results.txt").read_text(encoding="utf-8") if (EV / "test_results.txt").exists() else ""
    m = re.search(r"(\d+) passed(?:, (\d+) subtests passed)?[^\n]*in ([0-9.]+)s", tests_txt)
    tests_line = m.group(0) if m else "test log not found"
    names = {"REP-AURORA": "Aurora", "REP-BIRCH": "Birch", "REP-CEDAR": "Cedar", "REP-DELTA": "Delta"}

    pay_rows = "".join(
        f"<tr><td>{p['period']}</td><td>{names.get(p['rep_id'], p['rep_id'])}</td><td>{p['currency']}</td>"
        f"<td class=n>{money(p['amount_minor'])}</td></tr>" for p in story.get("payables", []))
    case_rows = "".join(
        f"<tr><td>{c['case_id']}</td><td>{c['period']}</td><td>{names.get(c['rep_id'], c['rep_id'])}</td>"
        f"<td>{c['currency']}</td><td class=n>{money(c['expected_minor'])}</td><td class=n>{money(c['recorded_minor'])}</td>"
        f"<td class=n>{'+' if c['variance_minor'] > 0 else ''}{money(c['variance_minor'])}</td><td>{c['status']}</td></tr>"
        for c in story.get("cases", []))
    checks = e2e.get("checks", [])
    check_rows = "".join(f"<li class={'ok' if c['passed'] else 'bad'}>{'✓' if c['passed'] else '✗'} "
                         f"{html.escape(c['check'])}</li>" for c in checks)
    bc = bench.get("controls", {})
    bt = bench.get("timings", {})
    benv = bench.get("environment", {})
    ci_line = (f"GitHub Actions run #{ci.get('run_number')} on commit <code>{html.escape(str(ci.get('head_sha', ''))[:12])}</code>: "
               + ", ".join(f"{html.escape(j['name'])} = {html.escape(j['conclusion'])}" for j in ci.get("jobs", []))
               ) if ci else "CI record not captured."

    sections = [
        ("0:00", "The problem", "problem", f"""
<p>A RevOps analyst has to answer six questions every month: <b>what cash came in</b>, <b>what is commission-eligible</b>,
<b>what each rep is owed</b>, <b>why a number changed</b>, <b>why payroll's recorded payout differs</b>, and <b>how a closed
month is protected from being rewritten</b>.</p>
<div class=say><b>Say:</b> “Bookings, invoices, cash and commission are four different numbers. This workbench pays on
<i>cash collected</i> under one invented demo policy, and every number can be traced to a source row.”</div>"""),
        ("0:20", "Load data — validation is visible", "load", f"""
<p>One click loads stage 1 (data as of 6 May 2026). Four rows are quarantined on purpose, from three planted problems:
a receipt dated in the future, a split that totals 90% (both of its rows) and a payout for an unknown rep. An exact
duplicate row is skipped, not double counted.</p>
<p class=note>Recorded screenshot: its guide text still says “three rows” — wording corrected later; the import result
itself (4 quarantined rows) is unchanged. See docs/evidence/README.md.</p>
{img('01_overview_stage1', 'Overview after loading stage 1: guided checklist and period picker')}
{img('12_import_batch_control_totals', 'Import batch: rows read = accepted + duplicate + quarantined, amounts reconcile')}
<div class=say><b>Say:</b> “Files are identified by their SHA-256, not their name. Nothing is silently dropped: every row
is accepted, a duplicate, or quarantined with a reason, and the control totals reconcile.”</div>"""),
        ("0:45", "April — split sale and threshold crossing", "april", f"""
<p>Cedar and Delta share a 60/40 split of USD 12,345.67: 7,407.40 / 4,938.27 (the odd cent goes to the larger remainder).
Cedar then crosses the 10,000.00 monthly threshold: 2,592.60 earns 5%, 2,407.40 earns 8%.</p>
{img('04_statement_cedar_split_crossing', 'Cedar April statement: split credit and threshold crossing')}
{img('05_line_evidence_threshold_crossing', 'Evidence chain: cash event → contract → split → plan rule → calculation')}
<div class=say><b>Say:</b> “Every line shows the rate version, the bracket portions, the split and one half-up
rounding. Click any line and you reach the file and row it came from.”</div>"""),
        ("1:20", "Review and close — history is frozen", "close", f"""
<p>analyst-1 submits; the workbench refuses to let the same label close. manager-1 closes: the reviewed run becomes an
immutable snapshot with a SHA-256, and payables are recorded (no payment is executed).</p>
{img('03_april_closed', 'April closed: frozen snapshot, per-currency metrics, bridge chart, control checks')}
<p class=note>Recorded screenshot: its export card still reads “byte-identical every time”. The current wording is
“unchanged by any later application operation (not protected from tampering by the database-file owner)”.</p>
<div class=say><b>Say:</b> “Close re-computes and compares every frozen field with what was reviewed. If anything changed
after review — even a booking-only import — close is refused as stale.”</div>"""),
        ("1:40", "Late data after close — no restatement", "late", f"""
<p>Stage 2 arrives in June: plan v2 (accelerator 9% from May), May cash, and a receipt dated 29 April that the bank feed
delivered late. April is closed, so the late receipt becomes a <b>blocking hold</b> on May.</p>
{img('06_may_late_hold_blocks_close', 'May: LATE_EVENT_PENDING hold blocks submission until a reviewer decides')}
<p>The reviewer posts it into May as a prior-period adjustment with a reason. It is tiered on April's frozen ladder at
April's 8% (USD 120.00). April's export is byte-identical before and after:
<b>{'identical ✓' if story.get('april_export_identical') else 'NOT identical ✗'}</b>
(SHA-256 <code>{html.escape(str(story.get('april_statement_sha256_after', ''))[:16])}…</code>).</p>
{img('07_may_closed', 'May closed after the late receipt was posted with a reason')}
<p class=note>Recorded screenshot: its export card also still reads “byte-identical every time” (current wording:
“unchanged by any later application operation, not protected from tampering by the database-file owner”).</p>"""),
        ("2:10", "Partial refund — clawback at the original rate", "refund", f"""
<p>A 1,000.00 refund of Aurora's April 2,000.00 collection is reversed against the <i>stored</i> original earning (8%),
not May's new 9% rate: −80.00. Refunds never restore tier capacity.</p>
{img('08_clawback_evidence', 'Clawback evidence: cumulative reversal against the original earning line')}
<div class=say><b>Say:</b> “Reversal is cumulative — half_up(E × refunded ÷ collected) — so three partial refunds of a
10-cent commission reverse exactly 10 cents, with no rounding drift.”</div>"""),
        ("2:25", "Payout variance — explain, don't restate", "variance", f"""
<p>The synthetic payout register disagrees three times. The workbench suggests causes by matching amounts to lines.</p>
<table><tr><th>Case</th><th>Period</th><th>Rep</th><th>Ccy</th><th>Expected</th><th>Recorded</th><th>Variance</th><th>Status at end of scripted run</th></tr>{case_rows}</table>
{img('09_variance_may', 'Expected vs recorded payouts for May')}
{img('10_case_resolved', 'Case VC-0002 resolved as MISSED_CLAWBACK — explained, not paid')}
<div class=say><b>Say:</b> “Resolved means explained. It is not a correction and not a payment.”</div>"""),
        ("2:45", "Evidence and honest limits", "evidence", f"""
<ul>
<li><b>Automated tests:</b> {html.escape(tests_line)} (developer self-tests; hand-computed expectations committed before the engine).</li>
<li><b>Browser E2E:</b> {e2e.get('passed', '?')} passed, {e2e.get('failed', '?')} failed ({html.escape(str(e2e.get('environment', {}).get('browser', '')))}, run {html.escape(str(e2e.get('generated_at', '')))}).</li>
<li><b>Benchmark:</b> {bench.get('n_events', '?'):,} seeded synthetic cash events; independent oracle matched
{bc.get('oracle_keys_matching', '?')}/{bc.get('oracle_keys_compared', '?')} rep-month-currency totals; credit conserved:
{bc.get('credit_conserved')}; refunds conserved: {bc.get('refund_conserved')}; import {bt.get('import_all_files_s')} s,
calculate + review + close of 6 months {bt.get('calculate_submit_close_6_months_s')} s on {html.escape(str(benv.get('cpu', '')))}
({benv.get('logical_cpus')} logical CPUs, Python {benv.get('python')}).</li>
<li><b>CI:</b> {ci_line}</li>
</ul>
<p class=warn><b>What this does not prove:</b> that the invented policy matches any real commission plan, market or
employer; enforced segregation of duties (actors are labels); a tamper-proof audit trail (it is append-only in the app and
hash-chained, but the database owner can rewrite it); or any business impact. All data is synthetic.</p>
<h3>Expected payouts frozen at close (from the scripted run)</h3>
<table><tr><th>Period</th><th>Rep</th><th>Ccy</th><th>Expected payout</th></tr>{pay_rows}</table>
<h3>Browser checks</h3><ul class=checks>{check_rows}</ul>"""),
    ]
    nav = "".join(f'<a href="#{sid}"><span>{t}</span>{html.escape(title)}</a>' for t, title, sid, _ in sections)
    body = "".join(f'<section id="{sid}"><div class=ts>{t}</div><h2>{html.escape(title)}</h2>{content}</section>'
                   for t, title, sid, content in sections)
    built = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Commission Workbench Replay</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#15181d;--ink2:#4a505a;--line:#e1e4e9;--accent:#2456c9;--warn:#8a5a00;--warnbg:#fdf3dc;--ok:#1d7342;--bad:#b3261e}}
@media (prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#0f1115;--card:#171a20;--ink:#f2f4f7;--ink2:#c5cad3;--line:#2a2f39;--accent:#7ea2ff;--warn:#f0b44a;--warnbg:#33270c;--ok:#4cc57f;--bad:#ff8a80}}}}
:root[data-theme=dark]{{--bg:#0f1115;--card:#171a20;--ink:#f2f4f7;--ink2:#c5cad3;--line:#2a2f39;--accent:#7ea2ff;--warn:#f0b44a;--warnbg:#33270c;--ok:#4cc57f;--bad:#ff8a80}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}}
.replay{{position:sticky;top:0;z-index:5;background:var(--warnbg);color:var(--warn);border-bottom:1px solid var(--line);padding:8px 16px;font-weight:600;text-align:center;font-size:.92rem}}
.layout{{display:grid;grid-template-columns:240px minmax(0,1fr);gap:24px;max-width:1240px;margin:0 auto;padding:24px 16px}}
nav{{position:sticky;top:64px;align-self:start;display:flex;flex-direction:column;gap:4px}}
nav a{{color:var(--ink2);text-decoration:none;padding:6px 8px;border-radius:8px;font-size:.92rem}}nav a:hover{{background:var(--card)}}
nav span{{display:inline-block;width:42px;color:var(--accent);font-variant-numeric:tabular-nums;font-weight:600}}
header h1{{margin:0 0 6px;font-size:1.7rem}}header p{{color:var(--ink2);margin:0 0 8px}}
section{{scroll-margin-top:72px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:0 0 18px;min-width:0}}
.ts{{display:inline-block;background:var(--accent);color:#fff;border-radius:999px;padding:1px 10px;font-size:.8rem;font-weight:700}}
h2{{margin:8px 0 10px;font-size:1.25rem}}figure{{margin:12px 0}}figure img{{width:100%;height:auto;border:1px solid var(--line);border-radius:8px}}
figcaption{{font-size:.82rem;color:var(--ink2)}}.say{{border-left:4px solid var(--accent);padding:8px 12px;margin:12px 0;background:var(--bg);border-radius:0 8px 8px 0}}
table{{border-collapse:collapse;width:100%;font-size:.9rem;margin:8px 0;display:block;overflow-x:auto}}th,td{{border-bottom:1px solid var(--line);padding:6px 8px;text-align:left;white-space:nowrap}}td.n{{text-align:right;font-variant-numeric:tabular-nums}}
.warn{{background:var(--warnbg);padding:10px 12px;border-radius:8px}}.checks{{columns:2;font-size:.88rem}}.ok{{color:var(--ok)}}.bad{{color:var(--bad)}}
code{{font-size:.85em;overflow-wrap:anywhere}}.note{{font-size:.86rem;color:var(--ink2);border-left:3px solid var(--line);padding-left:10px}}.missing{{color:var(--bad)}}
@media (max-width:860px){{.layout{{grid-template-columns:minmax(0,1fr)}}nav{{position:static;flex-direction:row;flex-wrap:wrap}}.checks{{columns:1}}}}
</style></head><body>
<div class="replay" role="note">DEMO REPLAY — recorded screenshots &amp; numbers from docs/evidence/. Not a live system or live results. Synthetic data.</div>
<div class="layout"><nav aria-label="Walkthrough">{nav}</nav><main>
<header><h1>Revenue &amp; Commission Operations Workbench</h1>
<p>3-minute guided walkthrough of a synthetic, independent portfolio prototype (built with AI assistance). Page built {built}
from artifacts: <code>story_evidence.json</code>, <code>e2e_results.json</code>, <code>benchmark.json</code>,
<code>test_results.txt</code>, <code>ci_run.json</code>, and Playwright screenshots.</p>
<p>To run it live instead: <code>python3 -m rcw demo</code> (no install) or double-click <code>launch_demo.command</code> on a Mac.</p>
<p class=note>Screenshots and numbers were recorded from the application at commit 8e8f7cc. Later commits changed only
user-facing wording, not calculations; the known differences are noted beside the affected screenshots and listed in
docs/evidence/README.md.</p></header>
{body}
</main></div></body></html>
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(page, encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
