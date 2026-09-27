"""Variance cases, suggestions, read-only as-of views, and the demo story's expected numbers."""
import unittest

from rcw import db, demo, services, variance
from tests.helpers import EXPECTED, PREPARER, REVIEWER, cash, close, imp, load_standard, new_conn

PAYOUT_HEADER = "record_id,rep_id,period,currency,amount,source_system,memo\n"


def case_for(conn, period, rep, ccy):
    return conn.execute("SELECT * FROM variance_cases WHERE period = ? AND rep_id = ? AND currency = ?",
                        (period, rep, ccy)).fetchone()


class DemoStoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.helpers import fixed_clock
        fixed_clock()
        cls.conn = demo.fresh_db(":memory:")
        cls.ev = demo.play_full_story(cls.conn)

    def test_expected_payouts_match_hand_values(self):
        got: dict = {}
        for p in self.ev["payables"]:
            got.setdefault(p["period"], {}).setdefault(p["rep_id"], {})[p["currency"]] = p["amount_minor"]
        self.assertEqual(got, EXPECTED["demo_story"]["expected_payouts"])

    def test_variances_match_hand_values(self):
        got: dict = {}
        for c in self.ev["cases"]:
            got.setdefault(c["period"], {}).setdefault(c["rep_id"], {})[c["currency"]] = c["variance_minor"]
        self.assertEqual(got, EXPECTED["demo_story"]["variances"])

    def test_story_properties(self):
        self.assertTrue(self.ev["april_export_identical"])
        self.assertEqual(self.ev["may_submit_refused"], "BLOCKING_HOLDS")
        self.assertTrue(self.ev["audit"]["ok"])
        q = {r["status"] for r in self.conn.execute("SELECT status FROM quarantine_rows WHERE file_kind='cash_events'")}
        self.assertEqual(q, {"SUPERSEDED"})   # the future-dated May row was superseded by the June file

    def test_suggestions_find_the_planted_causes(self):
        expect = {("2026-04", "REP-CEDAR"): "RATE_ERROR", ("2026-05", "REP-AURORA"): "MISSED_CLAWBACK",
                  ("2026-05", "REP-CEDAR"): "TIMING_LATE_ADJUSTMENT"}
        for (period, rep), code in expect.items():
            t = variance.variance_table(self.conn, period)
            row = next(r for r in t["rows"] if r["rep_id"] == rep)
            sugg = variance.suggestions(row["case"], t["lines"], t["recorded_rows"], t["basis"])
            with self.subTest(period=period, rep=rep):
                self.assertIn((code, "exact"), [(s["code"], s["match"]) for s in sugg])


class CaseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,1000.00,,,"])     # expected 50.00
        imp(self.conn, "recorded_payouts", PAYOUT_HEADER + "P1,REP-A,2026-04,SGD,45.00,SIM,april\n")

    def test_ids_notes_preserved_and_reopen_on_change(self):
        c = self.conn
        variance.refresh_variance(c, "2026-04", "ops-1")
        case = case_for(c, "2026-04", "REP-A", "SGD")
        self.assertEqual((case["variance_minor"], case["expected_basis"], case["status"]), (-500, "DRAFT", "OPEN"))
        cid = case["case_id"]
        variance.update_case(c, cid, "ops-1", owner="ops-1", status="INVESTIGATING")
        variance.add_note(c, cid, "ops-1", "Payroll keyed 45.00; asking why.")
        with self.assertRaises(services.WorkflowError):
            variance.update_case(c, cid, "ops-1", status="RESOLVED", resolution_note="too short")
        variance.update_case(c, cid, "ops-1", status="RESOLVED", reason_code="MANUAL_ENTRY_ERROR",
                             resolution_note="Keying error confirmed by payroll team")
        payables_before = c.execute("SELECT COUNT(*) FROM payables").fetchone()[0]
        self.assertEqual(payables_before, 0)          # resolving never creates or moves money
        close(c, "2026-04")                           # expected basis becomes CLOSED, amount unchanged
        case = case_for(c, "2026-04", "REP-A", "SGD")
        self.assertEqual((case["case_id"], case["status"], case["expected_basis"]), (cid, "RESOLVED", "CLOSED"))
        imp(c, "recorded_payouts", PAYOUT_HEADER + "P2,REP-A,2026-04,SGD,3.00,SIM,top-up\n")
        variance.refresh_variance(c, "2026-04", "ops-1")
        case = case_for(c, "2026-04", "REP-A", "SGD")
        self.assertEqual((case["case_id"], case["status"], case["variance_minor"], case["owner"]),
                         (cid, "REOPENED", -200, "ops-1"))
        bodies = [n["body"] for n in variance.case_notes(c, cid)]
        self.assertIn("Payroll keyed 45.00; asking why.", bodies)
        imp(c, "recorded_payouts", PAYOUT_HEADER + "P3,REP-A,2026-04,SGD,2.00,SIM,final\n")
        variance.refresh_variance(c, "2026-04", "ops-1")
        self.assertEqual(case_for(c, "2026-04", "REP-A", "SGD")["status"], "CLEARED")
        with self.assertRaises(services.WorkflowError):
            variance.update_case(c, cid, "ops-1", status="INVESTIGATING")   # CLEARED -> only REOPENED

    def test_excluded_collection_clears_existing_case(self):
        # R-3 regression: an existing case must be revisited even when expected and recorded both vanish.
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,1000.00,,,"])
        variance.refresh_variance(c, "2026-04", "ops-1")
        case = case_for(c, "2026-04", "REP-A", "SGD")
        self.assertEqual((case["variance_minor"], case["recorded_minor"]), (-5000, 0))
        variance.update_case(c, case["case_id"], "ops-1", owner="ops-1", status="INVESTIGATING")
        variance.add_note(c, case["case_id"], "ops-1", "checking whether this receipt is commissionable")
        services.decide_event(c, "E1", "EXCLUDE", "deposit is not commissionable under the plan", REVIEWER)
        services.calculate_period(c, "2026-04", PREPARER)
        variance.refresh_variance(c, "2026-04", "ops-1")
        after = case_for(c, "2026-04", "REP-A", "SGD")
        self.assertEqual((after["case_id"], after["expected_minor"], after["recorded_minor"], after["variance_minor"],
                          after["status"], after["owner"], after["expected_basis"]),
                         (case["case_id"], 0, 0, 0, "CLEARED", "ops-1", "NONE"))
        self.assertIn("checking whether this receipt is commissionable",
                      [n["body"] for n in variance.case_notes(c, case["case_id"])])

    def test_historical_as_of_views_are_read_only(self):
        c = self.conn
        run1 = services.calculate_period(c, "2026-04", PREPARER).run_id
        variance.refresh_variance(c, "2026-04", "ops-1")
        cash(c, ["E2,COLLECTION,K-SGD-A,2026-04-07,SGD,1000.00,,,"])
        services.calculate_period(c, "2026-04", PREPARER)
        before = db.db_fingerprint(c)
        old = variance.variance_table(c, "2026-04", as_of_run=run1)
        now = variance.variance_table(c, "2026-04")
        self.assertEqual(old["rows"][0]["expected_minor"], 5000)
        self.assertEqual(now["rows"][0]["expected_minor"], 10000)
        self.assertEqual(old["rows"][0]["case"]["variance_minor"], -500)   # case untouched by viewing
        self.assertEqual(db.db_fingerprint(c), before)

    def test_bad_transitions_and_codes(self):
        variance.refresh_variance(self.conn, "2026-04", "ops-1")
        cid = case_for(self.conn, "2026-04", "REP-A", "SGD")["case_id"]
        with self.assertRaises(services.WorkflowError):
            variance.update_case(self.conn, cid, "ops-1", status="REOPENED")
        with self.assertRaises(services.WorkflowError):
            variance.update_case(self.conn, cid, "ops-1", reason_code="NOT_A_CODE")
        with self.assertRaises(services.WorkflowError):
            variance.add_note(self.conn, cid, "ops-1", "   ")


if __name__ == "__main__":
    unittest.main()
