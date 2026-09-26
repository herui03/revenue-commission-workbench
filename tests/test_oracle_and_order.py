"""Seeded random datasets: engine vs independent oracle, order independence, draft == frozen path."""
import random
import unittest

from rcw import services
from tests import oracle
from tests.helpers import CASH_HEADER, PREPARER, REVIEWER, financial_view, imp, new_conn

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04"]


def make_dataset(seed: int, n_collections: int = 160):
    rng = random.Random(seed)
    reps = [f"REP-{i:02d}" for i in range(6)]
    plans = [
        {"plan_id": "P-A", "currency": "SGD", "effective_from": "2026-01-01", "version": 1, "threshold": 1_000_000, "base_bps": 500, "accel_bps": 800},
        {"plan_id": "P-A", "currency": "USD", "effective_from": "2026-01-01", "version": 1, "threshold": 800_000, "base_bps": 400, "accel_bps": 750},
        {"plan_id": "P-A", "currency": "SGD", "effective_from": "2026-03-01", "version": 2, "threshold": 1_200_000, "base_bps": 500, "accel_bps": 900},
        {"plan_id": "P-A", "currency": "USD", "effective_from": "2026-03-01", "version": 2, "threshold": 800_000, "base_bps": 450, "accel_bps": 777},
        {"plan_id": "P-B", "currency": "SGD", "effective_from": "2026-01-01", "version": 1, "threshold": 300_000, "base_bps": 300, "accel_bps": 1000},
        {"plan_id": "P-B", "currency": "USD", "effective_from": "2026-01-01", "version": 1, "threshold": 0, "base_bps": 0, "accel_bps": 650},
    ]
    assignments = [{"assignment_id": f"AS-{r}", "rep_id": r, "plan_id": "P-A", "effective_from": "2026-01-01",
                    "effective_to": ""} for r in reps]
    assignments.append({"assignment_id": "AS-MOVE", "rep_id": reps[0], "plan_id": "P-B", "effective_from": "2026-02-01",
                        "effective_to": "2026-03-31"})
    contracts, splits = [], {}
    for i in range(25):
        cid = f"K-{i:03d}"
        ccy = rng.choice(["SGD", "USD"])
        contracts.append({"contract_id": cid, "currency": ccy})
        k = rng.choice([1, 1, 2, 2, 3])
        chosen = rng.sample(reps, k)
        cuts = sorted(rng.sample(range(1, 10000), k - 1))
        bps = [b - a for a, b in zip([0] + cuts, cuts + [10000])]
        splits[cid] = dict(zip(chosen, bps))
    events = []
    for i in range(n_collections):
        c = rng.choice(contracts)
        month = rng.choice(MONTHS)
        day = rng.randint(1, 28)
        amount = rng.choice([rng.randint(1, 99), rng.randint(100, 50_000), rng.randint(50_000, 2_500_000)])
        events.append({"event_id": f"E-{i:05d}", "event_type": "COLLECTION", "contract_id": c["contract_id"],
                       "event_date": f"{month}-{day:02d}", "currency": c["currency"], "amount": amount,
                       "original_event_id": ""})
    n = 0
    for e in list(events):
        if rng.random() < 0.35:
            parts = rng.randint(1, 3)
            budget = e["amount"] if rng.random() < 0.4 else rng.randint(1, e["amount"])
            cuts = sorted(rng.sample(range(1, budget), min(parts - 1, budget - 1))) if budget > 1 else []
            amounts = [b - a for a, b in zip([0] + cuts, cuts + [budget])]
            start = MONTHS.index(e["event_date"][:7])
            for amt in amounts:
                month = MONTHS[rng.randint(start, len(MONTHS) - 1)]
                day = rng.randint(int(e["event_date"][8:]) if month == e["event_date"][:7] else 1, 28)
                events.append({"event_id": f"R-{n:05d}", "event_type": "REFUND", "contract_id": e["contract_id"],
                               "event_date": f"{month}-{day:02d}", "currency": e["currency"], "amount": amt,
                               "original_event_id": e["event_id"]})
                n += 1
    return {"reps": reps, "plans": plans, "assignments": assignments, "contracts": contracts, "splits": splits,
            "events": events}


def money(minor: int) -> str:
    return f"{minor // 100}.{minor % 100:02d}"


def load(conn, ds, rng: random.Random, chunks: int = 1):
    imp(conn, "reps", "rep_id,display_name,team\n" + "".join(f"{r},{r} (test),T\n" for r in ds["reps"]))
    imp(conn, "plans", "plan_id,version,currency,effective_from,threshold_amount,base_rate_pct,accelerator_rate_pct,"
        "description\n" + "".join(f"{p['plan_id']},{p['version']},{p['currency']},{p['effective_from']},"
                                  f"{money(p['threshold'])},{p['base_bps'] // 100}.{p['base_bps'] % 100:02d},"
                                  f"{p['accel_bps'] // 100}.{p['accel_bps'] % 100:02d},x\n" for p in ds["plans"]))
    imp(conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
        + "".join(f"{a['assignment_id']},{a['rep_id']},{a['plan_id']},{a['effective_from']},{a['effective_to']}\n"
                  for a in ds["assignments"]))
    imp(conn, "contracts", "contract_id,account_id,account_name,currency,booking_date,booked_amount,product\n"
        + "".join(f"{c['contract_id']},ACC-{c['contract_id']},Acct {c['contract_id']},{c['currency']},2026-01-01,"
                  f"1.00,x\n" for c in ds["contracts"]))
    imp(conn, "splits", "contract_id,rep_id,split_pct\n" + "".join(
        f"{cid},{rep},{b // 100}.{b % 100:02d}\n" for cid, sp in ds["splits"].items() for rep, b in sp.items()))
    colls = [e for e in ds["events"] if e["event_type"] == "COLLECTION"]
    refunds = [e for e in ds["events"] if e["event_type"] == "REFUND"]
    rng.shuffle(colls)
    rng.shuffle(refunds)
    if chunks == 0:                        # one file, collections and refunds interleaved at random (R-4)
        mixed = colls + refunds
        rng.shuffle(mixed)
        groups = [mixed]
    else:
        groups = [colls[i::chunks] for i in range(chunks)] + [refunds]
    for g in groups:
        text = CASH_HEADER + "".join(f"{e['event_id']},{e['event_type']},{e['contract_id']},{e['event_date']},"
                                     f"{e['currency']},{money(e['amount'])},{e['original_event_id']},,\n" for e in g)
        r = imp(conn, "cash_events", text)
        assert r.quarantined == 0, [x for x in r.rows if x["outcome"] == "QUARANTINED"][:3]


def engine_totals(conn) -> dict:
    out = {}
    for p in MONTHS:
        if services.is_closed(conn, p):
            totals = services.get_snapshot(conn, p)["totals"]
        else:
            totals = services.compute(conn, p).totals
        for t in totals:
            out[(p, t["rep_id"], t["currency"])] = t["net_minor"]
    return out


class OracleCrossCheck(unittest.TestCase):
    def test_engine_matches_oracle_on_seeded_datasets(self):
        for seed in (1, 2, 3, 4, 5, 6):
            ds = make_dataset(seed)
            conn = new_conn("2026-05-15")
            load(conn, ds, random.Random(seed), chunks=0 if seed % 2 else 1)
            got = engine_totals(conn)
            want = oracle.expected_totals(ds["contracts"], ds["splits"], ds["plans"], ds["assignments"], ds["events"])
            with self.subTest(seed=seed, events=len(ds["events"])):
                self.assertEqual(got, want)
                for p in MONTHS:
                    res = services.compute(conn, p)
                    self.assertTrue(all(c["passed"] for c in res.controls), [c for c in res.controls if not c["passed"]])
                    self.assertEqual(res.holds, [])

    def test_frozen_path_equals_draft_path(self):
        """Closing months one by one (so later refunds read frozen lines) must not change any number."""
        for seed in (7, 8):
            ds = make_dataset(seed, n_collections=120)
            conn = new_conn("2026-05-15")
            load(conn, ds, random.Random(seed))
            draft = engine_totals(conn)
            views = {p: financial_view(services.compute(conn, "2026-04").lines) for p in ["2026-04"]}
            for p in MONTHS[:-1]:
                services.calculate_period(conn, p, PREPARER)
                services.submit_for_review(conn, p, PREPARER)
                services.close_period(conn, p, REVIEWER)
            with self.subTest(seed=seed):
                self.assertEqual(engine_totals(conn), draft)
                self.assertEqual(financial_view(services.compute(conn, "2026-04").lines), views["2026-04"])


class OrderIndependence(unittest.TestCase):
    def test_shuffled_rows_and_file_splits_give_identical_money(self):
        ds = make_dataset(42, n_collections=100)
        views = []
        for trial, chunks in enumerate((1, 0, 0, 3, 5)):
            conn = new_conn("2026-05-15")
            load(conn, ds, random.Random(1000 + trial), chunks=chunks)
            views.append({p: financial_view(services.compute(conn, p).lines) for p in MONTHS})
        for v in views[1:]:
            self.assertEqual(v, views[0])


if __name__ == "__main__":
    unittest.main()
