"""Period workflow: transitions, stale review, freezing, late data, holds, adjustments, immutability."""
import json
import sqlite3
import unittest

from rcw import db, exports, services
from tests.helpers import (FIX, PREPARER, REVIEWER, cash, close, find_line, imp, lines_for, load_standard, new_conn,
                           totals_for)


def err(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except services.WorkflowError as exc:
        return exc.code
    raise AssertionError("expected WorkflowError")


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,10000.00,,,"])

    def test_invalid_transitions(self):
        c = self.conn
        self.assertEqual(err(services.close_period, c, "2026-04", REVIEWER), "NOT_IN_REVIEW")
        self.assertEqual(err(services.submit_for_review, c, "2026-04", PREPARER), "NO_CALCULATION")
        services.calculate_period(c, "2026-04", PREPARER)
        self.assertEqual(err(services.close_period, c, "2026-04", REVIEWER), "NOT_IN_REVIEW")
        services.submit_for_review(c, "2026-04", PREPARER)
        self.assertEqual(err(services.submit_for_review, c, "2026-04", PREPARER), "ALREADY_IN_REVIEW")
        self.assertEqual(err(services.close_period, c, "2026-04", PREPARER), "SAME_ACTOR")
        self.assertEqual(err(services.close_period, c, "2026-04", " Analyst-1 "), "SAME_ACTOR")
        services.close_period(c, "2026-04", REVIEWER)
        payables = c.execute("SELECT COUNT(*) FROM payables").fetchone()[0]
        self.assertEqual(err(services.close_period, c, "2026-04", REVIEWER), "ALREADY_CLOSED")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM payables").fetchone()[0], payables)
        self.assertEqual(err(services.calculate_period, c, "2026-04", PREPARER), "PERIOD_CLOSED")
        self.assertEqual(err(services.submit_for_review, c, "2026-04", PREPARER), "PERIOD_CLOSED")
        self.assertEqual(err(services.add_adjustment, c, "2026-04", "REP-A", "SGD", 100, "COMMISSION_ADJUSTMENT",
                             "a valid long reason", PREPARER), "PERIOD_CLOSED")

    def test_period_not_ended_and_chronological_order(self):
        c = new_conn("2026-05-20")
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,", "E2,COLLECTION,K-SGD-A,2026-05-06,SGD,100.00,,,"])
        services.calculate_period(c, "2026-05", PREPARER)
        services.submit_for_review(c, "2026-05", PREPARER)
        self.assertEqual(err(services.close_period, c, "2026-05", REVIEWER), "PERIOD_NOT_ENDED")
        services.set_business_date(c, "2026-06-02", "t")
        self.assertEqual(err(services.close_period, c, "2026-05", REVIEWER), "EARLIER_PERIOD_OPEN")
        close(c, "2026-04")
        services.close_period(c, "2026-05", REVIEWER)
        self.assertEqual(err(services.set_business_date, c, "2026-01-01", "t"), "DATE_BACKWARDS")

    def test_return_to_draft_requires_reason(self):
        services.calculate_period(self.conn, "2026-04", PREPARER)
        services.submit_for_review(self.conn, "2026-04", PREPARER)
        self.assertEqual(err(services.return_to_draft, self.conn, "2026-04", REVIEWER, "short"), "REASON_REQUIRED")
        services.return_to_draft(self.conn, "2026-04", REVIEWER, "please double-check the split")
        self.assertEqual(services.period_row(self.conn, "2026-04")["status"], "OPEN")


class StaleReviewTests(unittest.TestCase):
    """Reviewer supplement (a) and finding R-2: never close unreviewed numbers."""

    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-01-06,SGD,10000.00,,,"])
        services.calculate_period(self.conn, "2026-01", PREPARER)
        services.submit_for_review(self.conn, "2026-01", PREPARER)

    def assert_stale_then_recoverable(self, expect_section=None):
        c = self.conn
        try:
            services.close_period(c, "2026-01", REVIEWER)
        except services.WorkflowError as exc:
            self.assertEqual(exc.code, "STALE_REVIEW")
            if expect_section:
                self.assertIn(expect_section, exc.details["other_changed_sections"])
        else:
            self.fail("stale review was closed")
        self.assertEqual(c.execute("SELECT COUNT(*) FROM period_snapshots").fetchone()[0], 0)
        self.assertEqual(c.execute("SELECT COUNT(*) FROM payables").fetchone()[0], 0)
        out = services.calculate_period(c, "2026-01", PREPARER)
        self.assertTrue(out.review_withdrawn)
        services.submit_for_review(c, "2026-01", PREPARER)
        services.close_period(c, "2026-01", REVIEWER)
        return services.get_snapshot(c, "2026-01")

    def test_new_cash_after_review_is_stale(self):
        cash(self.conn, ["E2,COLLECTION,K-SGD-A,2026-01-20,SGD,2000.00,,,"])
        snap = self.assert_stale_then_recoverable()
        self.assertEqual(sum(t["net_minor"] for t in snap["totals"]), 66000)

    def test_booking_only_import_after_review_is_stale(self):
        # R-2: no commission line changes, but the bookings KPI would be frozen unreviewed.
        imp(self.conn, "contracts", FIX["standard"]["contracts"].splitlines()[0] + "\n"
            "K-JAN,ACC-9,January Booking (test),SGD,2026-01-28,7777.00,Booking only\n")
        snap = self.assert_stale_then_recoverable(expect_section="kpis")
        # four standard SGD fixture contracts booked 2026-01-15 at 500,000.00 each + the new 7,777.00 booking
        self.assertEqual(snap["kpis"]["SGD"]["bookings_minor"], 4 * 50_000_000 + 777_700)

    def test_adjustment_after_review_is_stale(self):
        services.add_adjustment(self.conn, "2026-01", "REP-A", "SGD", -1500, "COMMISSION_ADJUSTMENT",
                                "correct a keying error found in review", PREPARER)
        snap = self.assert_stale_then_recoverable()
        self.assertEqual(sum(t["net_minor"] for t in snap["totals"]), 50000 - 1500)

    def test_plan_version_after_review_is_stale(self):
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-05-08,SGD,11000.00,,,"])
        services.calculate_period(c, "2026-05", PREPARER)
        services.submit_for_review(c, "2026-05", PREPARER)
        imp(c, "plans", FIX["plan_v2"])            # v2 (9%) effective 2026-05-01 arrives after review
        self.assertEqual(err(services.close_period, c, "2026-05", REVIEWER), "STALE_REVIEW")
        services.calculate_period(c, "2026-05", PREPARER)
        services.submit_for_review(c, "2026-05", PREPARER)
        services.close_period(c, "2026-05", REVIEWER)
        self.assertEqual(totals_for(c, "2026-05"), {"REP-A": {"SGD": 59000}})

    def test_quarantined_reference_row_does_not_make_review_stale(self):
        imp(self.conn, "plans", FIX["plan_v2"].replace("2026-05-01", "2026-01-01").replace(",2,", ",0,")
            .replace("CASH-STD,0", "ALT-PLAN,1"))
        imp(self.conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
            "AS-A-ALT,REP-A,ALT-PLAN,2026-01-01,\n")
        # same effective date as AS-A -> ambiguous; quarantined, so nothing changes and close proceeds
        services.close_period(self.conn, "2026-01", REVIEWER)

    def test_submit_requires_fresh_run(self):
        services.return_to_draft(self.conn, "2026-01", REVIEWER, "rework needed after import")
        cash(self.conn, ["E2,COLLECTION,K-SGD-A,2026-01-20,SGD,2000.00,,,"])
        self.assertEqual(err(services.submit_for_review, self.conn, "2026-01", PREPARER), "STALE_RUN")

    def test_recalc_with_identical_result_keeps_review(self):
        out = services.calculate_period(self.conn, "2026-01", PREPARER)
        self.assertFalse(out.review_withdrawn)
        self.assertEqual(services.period_row(self.conn, "2026-01")["status"], "IN_REVIEW")

    def test_snapshot_is_built_from_the_reviewed_run(self):
        prow = services.period_row(self.conn, "2026-01")
        services.close_period(self.conn, "2026-01", REVIEWER)
        snap = services.get_snapshot(self.conn, "2026-01")
        self.assertEqual(snap["run_id"], prow["review_run_id"])
        stored = json.loads(self.conn.execute("SELECT result_json FROM calc_runs WHERE run_id = ?",
                                              (prow["review_run_id"],)).fetchone()[0])
        for field in ("totals", "controls", "kpis", "excluded", "inputs_used", "accounted_event_ids"):
            self.assertEqual(db.canonical_json(snap[field]), db.canonical_json(stored[field]), field)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)

    def hold_codes(self, period="2026-04"):
        return [h["code"] for h in services.compute(self.conn, period).blocking_holds]

    def test_no_split_hold_blocks_close_and_clears_when_split_arrives(self):
        imp(self.conn, "contracts", FIX["standard"]["contracts"].splitlines()[0] + "\n"
            "K-NOSPLIT,ACC-8,Eight (test),SGD,2026-01-15,1.00,T\n")
        cash(self.conn, ["E1,COLLECTION,K-NOSPLIT,2026-04-06,SGD,100.00,,,", "E2,COLLECTION,K-SGD-A,2026-04-07,SGD,100.00,,,",
                         "R1,REFUND,K-NOSPLIT,2026-04-08,SGD,50.00,E1,,"])
        self.assertEqual(sorted(self.hold_codes()), ["NO_SPLIT", "ORIGINAL_ON_HOLD"])
        services.calculate_period(self.conn, "2026-04", PREPARER)
        self.assertEqual(err(services.submit_for_review, self.conn, "2026-04", PREPARER), "BLOCKING_HOLDS")
        res = services.compute(self.conn, "2026-04")
        self.assertEqual(totals_for(self.conn, "2026-04"), {"REP-A": {"SGD": 500}})
        recon = [c for c in res.controls if c["name"].startswith("Cash reconciliation")][0]
        self.assertEqual(recon["detail"]["SGD"]["COLLECTION"]["held"], 10000)
        self.assertTrue(recon["passed"])
        imp(self.conn, "splits", "contract_id,rep_id,split_pct\nK-NOSPLIT,REP-B,100\n")
        self.assertEqual(self.hold_codes(), [])
        close(self.conn, "2026-04")
        self.assertEqual(totals_for(self.conn, "2026-04"), {"REP-A": {"SGD": 500}, "REP-B": {"SGD": 250}})

    def test_missing_assignment_and_missing_plan_version(self):
        imp(self.conn, "reps", "rep_id,display_name,team\nREP-N,New (test),T\n")
        imp(self.conn, "contracts", FIX["standard"]["contracts"].splitlines()[0] + "\n"
            "K-N,ACC-7,Seven (test),USD,2026-01-15,1.00,T\n")
        imp(self.conn, "splits", "contract_id,rep_id,split_pct\nK-N,REP-N,100\n")
        cash(self.conn, ["E1,COLLECTION,K-N,2026-04-06,USD,100.00,,,"])
        self.assertEqual(self.hold_codes(), ["NO_ASSIGNMENT"])
        imp(self.conn, "plans", "plan_id,version,currency,effective_from,threshold_amount,base_rate_pct,"
            "accelerator_rate_pct,description\nSGD-ONLY,1,SGD,2026-01-01,10000.00,5.00,8.00,sgd only\n")
        imp(self.conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
            "AS-N,REP-N,SGD-ONLY,2026-04-01,\n")
        self.assertEqual(self.hold_codes(), ["NO_PLAN_VERSION"])

    def test_assignment_supersession_and_end_date(self):
        imp(self.conn, "plans", "plan_id,version,currency,effective_from,threshold_amount,base_rate_pct,"
            "accelerator_rate_pct,description\nFLAT-10,1,SGD,2026-01-01,0.00,10.00,10.00,flat 10 pct\n")
        imp(self.conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
            "AS-A-JUN,REP-A,FLAT-10,2026-06-01,2026-06-30\n")
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-05-06,SGD,100.00,,,", "E2,COLLECTION,K-SGD-A,2026-06-06,SGD,100.00,,,",
                         "E3,COLLECTION,K-SGD-A,2026-07-06,SGD,100.00,,,"])
        self.assertEqual(find_line(lines_for(self.conn, "2026-05"), "EARNING", "E1", "REP-A")["amount_minor"], 500)
        self.assertEqual(find_line(lines_for(self.conn, "2026-06"), "EARNING", "E2", "REP-A")["amount_minor"], 1000)
        self.assertEqual(find_line(lines_for(self.conn, "2026-07"), "EARNING", "E3", "REP-A")["amount_minor"], 500)

    def test_quarantined_cash_row_blocks_until_dismissed(self):
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,", "E2,COLLECTION,K-SGD-A,2026-04-07,SGD,1e3,,,"])
        self.assertEqual(self.hold_codes(), ["QUARANTINED_CASH_ROW"])
        self.assertEqual(self.hold_codes("2026-05"), [])
        from rcw import importer
        q_id = self.conn.execute("SELECT q_id FROM quarantine_rows").fetchone()[0]
        with self.assertRaises(ValueError):
            importer.dismiss_quarantine_row(self.conn, q_id, "short", REVIEWER)
        importer.dismiss_quarantine_row(self.conn, q_id, "source team confirmed the row was a test entry", REVIEWER)
        self.assertEqual(self.hold_codes(), [])
        close(self.conn, "2026-04")

    def test_exclusion_decision(self):
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,", "E2,COLLECTION,K-SGD-A,2026-04-07,SGD,200.00,,,"])
        self.assertEqual(err(services.decide_event, self.conn, "E1", "EXCLUDE", "short", REVIEWER), "REASON_REQUIRED")
        self.assertEqual(err(services.decide_event, self.conn, "E1", "POST_LATE", "not late at all really",
                             REVIEWER), "NOT_LATE")
        services.decide_event(self.conn, "E1", "EXCLUDE", "customer paid a deposit that is not commissionable",
                              REVIEWER)
        self.assertEqual(err(services.decide_event, self.conn, "E1", "EXCLUDE", "second decision attempt",
                             REVIEWER), "ALREADY_DECIDED")
        res = services.compute(self.conn, "2026-04")
        self.assertEqual([e["event_id"] for e in res.excluded], ["E1"])
        self.assertEqual(totals_for(self.conn, "2026-04"), {"REP-A": {"SGD": 1000}})
        close(self.conn, "2026-04")
        self.assertIn("E1", services.get_snapshot(self.conn, "2026-04")["accounted_event_ids"])


class LateDataTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)
        cash(self.conn, ["E1,COLLECTION,K-6040,2026-04-06,SGD,20000.00,,,"])
        close(self.conn, "2026-04")
        self.april = exports.statement_csv(exports.statement_model(self.conn, "2026-04"))

    def test_late_refund_claws_back_frozen_earning(self):
        # A: 12,000 -> 50,000 + 2,000@8% = 16,000 -> 66,000; B: 8,000 @5% = 40,000.
        cash(self.conn, ["R1,REFUND,K-6040,2026-04-28,SGD,5000.00,E1,,late credit note"])
        self.assertEqual([h["code"] for h in services.compute(self.conn, "2026-05").blocking_holds],
                         ["LATE_EVENT_PENDING"])
        self.assertEqual(services.compute(self.conn, "2026-06").blocking_holds, [])
        services.decide_event(self.conn, "R1", "POST_LATE", "credit note dated April received in May", REVIEWER)
        lines = lines_for(self.conn, "2026-05")
        # cumulative: half_up(66,000 * 5,000/20,000) = 16,500 ; half_up(40,000 * 0.25) = 10,000
        self.assertEqual(find_line(lines, "LATE_CLAWBACK", "R1", "REP-A")["amount_minor"], -16500)
        self.assertEqual(find_line(lines, "LATE_CLAWBACK", "R1", "REP-B")["amount_minor"], -10000)
        self.assertEqual(find_line(lines, "LATE_CLAWBACK", "R1", "REP-A")["credit_reversed_minor"], 300000)
        self.assertEqual(exports.statement_csv(exports.statement_model(self.conn, "2026-04")), self.april)

    def test_late_event_posts_only_into_first_open_period_and_is_final(self):
        cash(self.conn, ["L1,COLLECTION,K-SGD-A,2026-04-29,SGD,100.00,,,"])
        posting = services.decide_event(self.conn, "L1", "POST_LATE", "bank feed arrived late for April", REVIEWER)
        self.assertEqual(posting, "2026-05")
        close(self.conn, "2026-05")
        self.assertEqual(err(services.decide_event, self.conn, "L1", "EXCLUDE", "changing my mind later",
                             REVIEWER), "ALREADY_DECIDED")
        june = services.compute(self.conn, "2026-06")
        self.assertEqual(june.holds, [])
        self.assertEqual(june.lines, [])

    def test_refund_of_excluded_collection_is_held(self):
        cash(self.conn, ["L1,COLLECTION,K-SGD-A,2026-04-29,SGD,100.00,,,",
                         "R9,REFUND,K-SGD-A,2026-05-03,SGD,10.00,L1,,"])
        services.decide_event(self.conn, "L1", "EXCLUDE", "duplicate of a receipt already paid", REVIEWER)
        codes = [h["code"] for h in services.compute(self.conn, "2026-05").blocking_holds]
        self.assertEqual(codes, ["ORIGINAL_EXCLUDED"])
        services.decide_event(self.conn, "R9", "EXCLUDE", "refund of an excluded duplicate receipt", REVIEWER)
        self.assertEqual(services.compute(self.conn, "2026-05").blocking_holds, [])


class AdjustmentTests(unittest.TestCase):
    def test_manual_adjustments_are_separate_and_need_reason(self):
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,1000.00,,,"])
        self.assertEqual(err(services.add_adjustment, c, "2026-04", "REP-A", "SGD", 100, "COMMISSION_ADJUSTMENT", "",
                             PREPARER), "REASON_REQUIRED")
        self.assertEqual(err(services.add_adjustment, c, "2026-04", "REP-A", "SGD", 0, "COMMISSION_ADJUSTMENT",
                             "zero is meaningless", PREPARER), "BAD_AMOUNT")
        self.assertEqual(err(services.add_adjustment, c, "2026-04", "REP-X", "SGD", 5, "COMMISSION_ADJUSTMENT",
                             "unknown rep test", PREPARER), "UNKNOWN_REP")
        self.assertEqual(err(services.add_adjustment, c, "2026-04", "REP-A", "EUR", 5, "COMMISSION_ADJUSTMENT",
                             "unknown currency", PREPARER), "BAD_CURRENCY")
        services.add_adjustment(c, "2026-04", "REP-A", "SGD", 1234, "COMMISSION_ADJUSTMENT",
                                "spiff approved by sales leadership", PREPARER)
        res = services.compute(c, "2026-04")
        t = res.totals[0]
        self.assertEqual((t["earnings_minor"], t["manual_minor"], t["net_minor"]), (5000, 1234, 6234))
        adj = [ln for ln in res.lines if ln["line_type"] == "MANUAL_ADJUSTMENT"]
        self.assertEqual(len(adj), 1)
        self.assertIsNone(adj[0]["event_id"])
        self.assertEqual(res.kpis["SGD"]["earnings_minor"], 5000)   # not disguised as source earnings


class ImmutabilityTests(unittest.TestCase):
    def test_triggers_block_edits_and_audit_chain_detects_tampering(self):
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,1000.00,,,"])
        close(c, "2026-04")
        for sql in ("UPDATE cash_events SET amount_minor = 1", "DELETE FROM cash_events",
                    "UPDATE calc_lines SET amount_minor = 1", "DELETE FROM calc_lines",
                    "UPDATE period_snapshots SET sha256 = 'x'", "UPDATE payables SET amount_minor = 1",
                    "UPDATE periods SET status = 'OPEN'", "UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log",
                    "UPDATE plan_versions SET accel_rate_bps = 900",
                    "INSERT INTO adjustments(period, rep_id, currency, amount_minor, category, reason, actor, "
                    "created_at) VALUES ('2026-04','REP-A','SGD',5,'COMMISSION_ADJUSTMENT','sneaky edit here','x','t')"):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.DatabaseError):
                c.execute(sql)
        self.assertTrue(db.verify_audit_chain(c)["ok"])
        # Honest limitation: whoever owns the DB file can drop a trigger; the hash chain then flags the edit.
        c.execute("DROP TRIGGER trg_audit_no_update")
        c.execute("UPDATE audit_log SET actor = 'mallory' WHERE seq = 3")
        chain = db.verify_audit_chain(c)
        self.assertFalse(chain["ok"])
        self.assertEqual(chain["broken_at_seq"], 3)

    def test_every_mutation_is_audited(self):
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,1000.00,,,"])
        close(c, "2026-04")
        actions = [r[0] for r in c.execute("SELECT action FROM audit_log ORDER BY seq")]
        for a in ("IMPORT_COMMITTED", "CALCULATE", "SUBMIT_FOR_REVIEW", "CLOSE_PERIOD", "VARIANCE_REFRESH"):
            self.assertIn(a, actions)


if __name__ == "__main__":
    unittest.main()
