"""Seeded larger-dataset benchmark: 10,000 cash events through the real importer, engine and close.

Reports (never invents) correctness controls against the independent oracle in tests/oracle.py,
runtime per stage, the environment and every denominator. Writes docs/evidence/benchmark.json and
docs/08_benchmark.md.

    python3 scripts/benchmark.py            # stdlib only, ~30-60 s
    python3 scripts/benchmark.py --events 2000 --out /tmp/x   # smaller smoke run (CI)

What this shows: the arithmetic and controls hold at 10k events and how long it takes on this
machine. What it does NOT show: that the invented policy matches any real plan or market.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rcw import db, importer, services  # noqa: E402
from tests import oracle  # noqa: E402  (independent reference model; not production code)

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def m(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d}"


def pct(bps: int) -> str:
    return f"{bps // 100}.{bps % 100:02d}"


def generate(seed: int, n_events: int, n_reps: int = 40, n_contracts: int = 1500) -> dict:
    rng = random.Random(seed)
    reps = [f"REP-{i:03d}" for i in range(n_reps)]
    plans = []
    for ccy in ("SGD", "USD"):
        plans.append({"plan_id": "CASH-STD", "version": 1, "currency": ccy, "effective_from": "2026-01-01",
                      "threshold": 1_000_000, "base_bps": 500, "accel_bps": 800})
        plans.append({"plan_id": "CASH-STD", "version": 2, "currency": ccy, "effective_from": "2026-04-01",
                      "threshold": 1_000_000, "base_bps": 500, "accel_bps": 900})
    assignments = [{"assignment_id": f"AS-{r}", "rep_id": r, "plan_id": "CASH-STD", "effective_from": "2026-01-01",
                    "effective_to": ""} for r in reps]
    contracts, splits = [], {}
    for i in range(n_contracts):
        cid = f"K-{i:05d}"
        ccy = "SGD" if rng.random() < 0.55 else "USD"
        contracts.append({"contract_id": cid, "currency": ccy})
        k = rng.choices([1, 2, 3], weights=[60, 30, 10])[0]
        chosen = rng.sample(reps, k)
        cuts = sorted(rng.sample(range(1, 10000), k - 1))
        splits[cid] = dict(zip(chosen, [b - a for a, b in zip([0] + cuts, cuts + [10000])]))
    n_refunds = n_events // 8
    n_coll = n_events - n_refunds
    events = []
    for i in range(n_coll):
        c = rng.choice(contracts)
        month = rng.choice(MONTHS)
        events.append({"event_id": f"E-{i:06d}", "event_type": "COLLECTION", "contract_id": c["contract_id"],
                       "event_date": f"{month}-{rng.randint(1, 28):02d}", "currency": c["currency"],
                       "amount": rng.choice([rng.randint(1, 999), rng.randint(1_000, 200_000),
                                            rng.randint(200_000, 3_000_000)]), "original_event_id": ""})
    remaining = {e["event_id"]: e["amount"] for e in events}
    colls = list(events)
    j = 0
    while j < n_refunds:
        e = rng.choice(colls)
        left = remaining[e["event_id"]]
        if left <= 0:
            continue
        amt = left if rng.random() < 0.3 else rng.randint(1, left)
        remaining[e["event_id"]] -= amt
        start = MONTHS.index(e["event_date"][:7])
        month = MONTHS[rng.randint(start, len(MONTHS) - 1)]
        day = rng.randint(int(e["event_date"][8:]) if month == e["event_date"][:7] else 1, 28)
        events.append({"event_id": f"R-{j:06d}", "event_type": "REFUND", "contract_id": e["contract_id"],
                       "event_date": f"{month}-{day:02d}", "currency": e["currency"], "amount": amt,
                       "original_event_id": e["event_id"]})
        j += 1
    return {"reps": reps, "plans": plans, "assignments": assignments, "contracts": contracts, "splits": splits,
            "events": events}


def csv_files(ds: dict, rng: random.Random) -> list[tuple[str, str]]:
    evs = list(ds["events"])
    rng.shuffle(evs)   # file order must not matter
    return [
        ("reps", "rep_id,display_name,team\n" + "".join(f"{r},{r} (synthetic),T\n" for r in ds["reps"])),
        ("plans", "plan_id,version,currency,effective_from,threshold_amount,base_rate_pct,accelerator_rate_pct,"
                  "description\n" + "".join(f"{p['plan_id']},{p['version']},{p['currency']},{p['effective_from']},"
                                            f"{m(p['threshold'])},{pct(p['base_bps'])},{pct(p['accel_bps'])},bench\n"
                                            for p in ds["plans"])),
        ("assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n" + "".join(
            f"{a['assignment_id']},{a['rep_id']},{a['plan_id']},{a['effective_from']},\n" for a in ds["assignments"])),
        ("contracts", "contract_id,account_id,account_name,currency,booking_date,booked_amount,product\n" + "".join(
            f"{c['contract_id']},ACC-{c['contract_id']},Account {c['contract_id']} (synthetic),{c['currency']},"
            f"2026-01-01,1.00,bench\n" for c in ds["contracts"])),
        ("splits", "contract_id,rep_id,split_pct\n" + "".join(
            f"{cid},{rep},{pct(b)}\n" for cid, sp in ds["splits"].items() for rep, b in sp.items())),
        ("cash_events", "event_id,event_type,contract_id,event_date,currency,amount,original_event_id,invoice_ref,memo\n"
         + "".join(f"{e['event_id']},{e['event_type']},{e['contract_id']},{e['event_date']},{e['currency']},"
                   f"{m(e['amount'])},{e['original_event_id']},,\n" for e in evs)),
    ]


def environment() -> dict:
    cpu = platform.processor() or ""
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version, "os": platform.platform(),
            "cpu": cpu or "unknown", "logical_cpus": os.cpu_count(), "note": "single process, single thread"}


def run(n_events: int, seed: int, work: Path) -> dict:
    ds = generate(seed, n_events)
    timings, controls = {}, {}
    conn = db.connect(work / "bench.db")
    db.init_db(conn)
    db.set_setting(conn, "business_date", "2026-07-15")
    files = csv_files(ds, random.Random(seed + 1))
    t = time.perf_counter()
    import_results = {}
    for kind, text in files:
        r = importer.import_csv(conn, kind, f"{kind}.csv", text.encode(), mode="strict", actor="bench")
        import_results[kind] = {"status": r.status, "rows": r.rows_read, "accepted": r.accepted}
        assert r.status == "COMMITTED", (kind, r.summary())
    timings["import_all_files_s"] = time.perf_counter() - t

    # draft calculation of every month (all months open)
    t = time.perf_counter()
    draft_totals = {}
    for p in MONTHS:
        res = services.compute(conn, p)
        for tt in res.totals:
            draft_totals[(p, tt["rep_id"], tt["currency"])] = tt["net_minor"]
    timings["draft_compute_6_months_s"] = time.perf_counter() - t

    # sequential calculate -> submit -> close for every month
    t = time.perf_counter()
    lines = 0
    all_controls_pass = True
    per_close = []
    for p in MONTHS:
        t1 = time.perf_counter()
        out = services.calculate_period(conn, p, "preparer-bench")
        services.submit_for_review(conn, p, "preparer-bench")
        services.close_period(conn, p, "reviewer-bench")
        per_close.append(round(time.perf_counter() - t1, 3))
        lines += len(out.result.lines)
        all_controls_pass &= all(c["passed"] for c in out.result.controls)
    timings["calculate_submit_close_6_months_s"] = time.perf_counter() - t
    timings["per_month_calculate_submit_close_s"] = dict(zip(MONTHS, per_close))

    closed_totals = {}
    for p in MONTHS:
        for tt in services.get_snapshot(conn, p)["totals"]:
            closed_totals[(p, tt["rep_id"], tt["currency"])] = tt["net_minor"]

    t = time.perf_counter()
    want = oracle.expected_totals(ds["contracts"], ds["splits"], ds["plans"], ds["assignments"],
                                  [{**e} for e in ds["events"]])
    timings["oracle_s"] = time.perf_counter() - t
    keys = set(want) | set(closed_totals)
    matches = sum(1 for k in keys if want.get(k, 0) == closed_totals.get(k, 0))

    # conservation & control totals straight from the database
    coll = {r["currency"]: r["s"] for r in conn.execute(
        "SELECT currency, SUM(amount_minor) s FROM cash_events WHERE event_type='COLLECTION' GROUP BY currency")}
    refs = {r["currency"]: r["s"] for r in conn.execute(
        "SELECT currency, SUM(amount_minor) s FROM cash_events WHERE event_type='REFUND' GROUP BY currency")}
    closing_runs = [r[0] for r in conn.execute("SELECT closed_run_id FROM periods WHERE status='CLOSED'")]
    q = ",".join("?" * len(closing_runs))
    credited = {r["currency"]: r["s"] for r in conn.execute(
        f"SELECT currency, SUM(credited_minor) s FROM calc_lines WHERE run_id IN ({q}) GROUP BY currency", closing_runs)}
    reversed_ = {r["currency"]: r["s"] for r in conn.execute(
        f"SELECT currency, SUM(credit_reversed_minor) s FROM calc_lines WHERE run_id IN ({q}) GROUP BY currency",
        closing_runs)}
    payables = {r["currency"]: r["s"] for r in conn.execute(
        "SELECT currency, SUM(amount_minor) s FROM payables GROUP BY currency")}
    full = [oid for oid, left in _remaining(ds).items() if left == 0]
    controls = {
        "events": {"collections": sum(1 for e in ds["events"] if e["event_type"] == "COLLECTION"),
                   "refunds": sum(1 for e in ds["events"] if e["event_type"] == "REFUND"),
                   "fully_refunded_collections": len(full)},
        "reps": len(ds["reps"]), "contracts": len(ds["contracts"]),
        "multi_rep_splits": sum(1 for s in ds["splits"].values() if len(s) > 1),
        "calc_lines_in_closed_snapshots": lines,
        "collected_minor": coll, "credited_minor": credited, "credit_conserved": coll == credited,
        "refunded_minor": refs, "credit_reversed_minor": reversed_, "refund_conserved": refs == reversed_,
        "engine_controls_all_passed": all_controls_pass,
        "oracle_keys_compared": len(keys), "oracle_keys_matching": matches,
        "draft_equals_closed": draft_totals == closed_totals,
        "payables_minor": payables,
        "audit_chain": db.verify_audit_chain(conn),
    }
    # order independence: same rows, different file order, into a fresh DB
    conn2 = db.connect(work / "bench2.db")
    db.init_db(conn2)
    db.set_setting(conn2, "business_date", "2026-07-15")
    for kind, text in csv_files(ds, random.Random(seed + 99)):
        importer.import_csv(conn2, kind, f"{kind}.csv", text.encode(), mode="strict", actor="bench")
    d1 = _fin_digest(conn, closing_runs)
    controls["shuffled_import_same_totals"] = _draft_totals(conn2) == sorted(
        (k[0], k[1], k[2], v) for k, v in draft_totals.items())
    conn.close()
    conn2.close()
    return {"n_events": len(ds["events"]), "seed": seed, "timings": {k: (round(v, 3) if isinstance(v, float) else v)
                                                                      for k, v in timings.items()},
            "controls": controls, "imports": import_results, "environment": environment(), "digest": d1}


def _remaining(ds: dict) -> dict:
    left = {e["event_id"]: e["amount"] for e in ds["events"] if e["event_type"] == "COLLECTION"}
    for e in ds["events"]:
        if e["event_type"] == "REFUND":
            left[e["original_event_id"]] -= e["amount"]
    return left


def _draft_totals(conn) -> list:
    out = []
    for p in MONTHS:
        for t in services.compute(conn, p).totals:
            out.append((p, t["rep_id"], t["currency"], t["net_minor"]))
    return sorted(out)


def _fin_digest(conn, runs) -> str:
    q = ",".join("?" * len(runs))
    rows = conn.execute(f"SELECT line_key, amount_minor FROM calc_lines WHERE run_id IN ({q}) ORDER BY line_key",
                        runs).fetchall()
    return hashlib.sha256(json.dumps([list(r) for r in rows]).encode()).hexdigest()


def _mm(minor: int) -> str:
    return f"{minor // 100:,}.{minor % 100:02d}"


def write_markdown(res: dict, path: Path) -> None:
    c, t, e = res["controls"], res["timings"], res["environment"]
    per = " · ".join(f"{k}: {v} s" for k, v in t["per_month_calculate_submit_close_s"].items())
    ccys = sorted(c["collected_minor"])
    money_rows = "\n".join(
        f"| {ccy} | {_mm(c['collected_minor'][ccy])} | {_mm(c['credited_minor'][ccy])} | {_mm(c['refunded_minor'][ccy])} "
        f"| {_mm(c['credit_reversed_minor'][ccy])} | {_mm(c['payables_minor'][ccy])} |" for ccy in ccys)
    path.write_text(f"""# 08 · Benchmark (generated from docs/evidence/benchmark.json — do not edit by hand)

Command: `python3 scripts/benchmark.py` (stdlib only). Seed `{res['seed']}`. Synthetic data only.

## What was run
* **{res['n_events']:,} cash events**: {c['events']['collections']:,} collections + {c['events']['refunds']:,} refunds
  ({c['events']['fully_refunded_collections']} collections fully refunded, many partially, some in later months),
  {c['reps']} reps, {c['contracts']:,} contracts ({c['multi_rep_splits']} with 2-3 rep splits), SGD + USD,
  6 months, plan v1 then v2 (accelerator 8% → 9% from April).
* Strict-mode import of every file (rows shuffled) → draft calculation of all 6 months →
  calculate + submit + close each month in order → compare with the **independent oracle**
  (`tests/oracle.py`, written separately with Fractions and plain loops) → re-import the same rows
  in a different order into a second database.

## Correctness controls (arithmetic evidence)
| Control | Result |
|---|---|
| Oracle vs closed snapshots, per period × rep × currency | **{c['oracle_keys_matching']} / {c['oracle_keys_compared']} match** |
| Credited cents == collected cents (per currency) | {c['credit_conserved']} |
| Credit reversed == refunded cents (per currency) | {c['refund_conserved']} |
| Engine control checks in every month | {c['engine_controls_all_passed']} |
| Draft totals (all months open) == totals after sequential close | {c['draft_equals_closed']} |
| Shuffled re-import gives identical totals | {c['shuffled_import_same_totals']} |
| Audit hash chain | {'intact' if c['audit_chain']['ok'] else 'BROKEN'} ({c['audit_chain']['entries']} entries) |
| Calculation lines frozen in snapshots | {c['calc_lines_in_closed_snapshots']:,} |

| Currency | Collected | Credited | Refunded | Credit reversed | Expected payouts |
|---|---:|---:|---:|---:|---:|
{money_rows}

## Runtime on this machine (one run, wall clock)
| Stage | Seconds |
|---|---:|
| Import all files (strict mode, validation + provenance) | {t['import_all_files_s']} |
| Draft compute of 6 open months | {t['draft_compute_6_months_s']} |
| Calculate + submit + close, 6 months in order | {t['calculate_submit_close_6_months_s']} |
| Oracle (reference) | {t['oracle_s']} |
| **Total benchmark wall time** | **{t['total_wall_s']}** |

Per month (calculate + submit + close): {per}. Close deliberately recomputes the month several
times (calculate, submit, close stale-check, variance refresh), so it costs more than one draft.

Environment: {e['cpu']}, {e['logical_cpus']} logical CPUs, Python {e['python']}, SQLite {e['sqlite']},
{e['os']}; {e['note']}.

## What this does and does not show
* **Shows:** the importer, engine and close workflow keep every cent and every control at 10k events,
  results are independent of file order, and the timings above on this machine.
* **Does not show:** real-world validity of the invented policy, performance on other hardware,
  concurrency, or any time saved for a real team (nothing was measured against a manual process).
  The oracle shares the policy with the engine, so agreement proves consistency with the *policy*,
  not that the policy is right.
""", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=20260926)
    ap.add_argument("--out", default=str(ROOT / "docs" / "evidence"))
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        t = time.perf_counter()
        res = run(args.events, args.seed, Path(tmp))
        res["timings"]["total_wall_s"] = round(time.perf_counter() - t, 3)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "benchmark.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    if out.resolve() == (ROOT / "docs" / "evidence").resolve():
        write_markdown(res, ROOT / "docs" / "08_benchmark.md")
    c = res["controls"]
    ok = (c["credit_conserved"] and c["refund_conserved"] and c["engine_controls_all_passed"]
          and c["oracle_keys_matching"] == c["oracle_keys_compared"] and c["draft_equals_closed"]
          and c["shuffled_import_same_totals"] and c["audit_chain"]["ok"])
    print(json.dumps({"ok": ok, "n_events": res["n_events"], "timings": res["timings"],
                      "oracle": f"{c['oracle_keys_matching']}/{c['oracle_keys_compared']}"}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
