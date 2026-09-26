"""Import validation, idempotency, quarantine, strict atomicity and control totals."""
import json
import random
import unittest

from rcw import importer, services
from tests.helpers import CASH_HEADER, EXPECTED, FIX, cash, close, imp, load_standard, new_conn


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def reasons_of(result, key):
    for r in result.rows:
        if r["key"] == [key]:
            return [x["code"] for x in r["reasons"]], r["outcome"]
    raise AssertionError(f"row {key} not found in {result.rows}")


class ValidationCaseTests(unittest.TestCase):
    def test_hand_listed_cash_validation_cases(self):
        for case in EXPECTED["validation"]:
            with self.subTest(case=case["id"], title=case["title"]):
                conn = new_conn()
                load_standard(conn)
                r = cash(conn, [case["row"]])
                self.assertEqual(r.quarantined, 1, r.rows)
                codes = r.rows[0]["reasons"]
                self.assertIn(case["code"], [c["code"] for c in codes], codes)
                self.assertEqual(count(conn, "cash_events"), 0)
                q = conn.execute("SELECT * FROM quarantine_rows").fetchone()
                self.assertEqual(q["status"], "OPEN")
                self.assertIn(case["code"], q["reasons_json"])

    def test_hand_listed_refund_cases(self):
        rv = EXPECTED["refund_validation"]
        for case in rv["cases"]:
            with self.subTest(case=case["id"], title=case["title"]):
                conn = new_conn()
                load_standard(conn)
                self.assertEqual(cash(conn, [rv["setup_row"]]).accepted, 1)
                r = cash(conn, case["rows"])
                accepted = [row["key"][0] for row in r.rows if row["outcome"] == "ACCEPTED"]
                self.assertEqual(sorted(accepted), sorted(case["accepted"]), r.rows)
                for key, code in case["quarantined"].items():
                    codes, outcome = reasons_of(r, key)
                    self.assertEqual(outcome, "QUARANTINED")
                    self.assertIn(code, codes)

    def test_over_refund_is_order_independent(self):
        rows = ["R1,REFUND,K-SGD-A,2026-04-11,SGD,600.00,E0,,", "R2,REFUND,K-SGD-A,2026-04-12,SGD,500.00,E0,,"]
        for order in (rows, rows[::-1]):
            conn = new_conn()
            load_standard(conn)
            cash(conn, ["E0,COLLECTION,K-SGD-A,2026-04-10,SGD,1000.00,,,"])
            r = cash(conn, order)
            self.assertEqual([x["key"][0] for x in r.rows if x["outcome"] == "ACCEPTED"], ["R1"])

    def test_trailing_newline_ids_and_periods_rejected(self):
        from rcw.periods_util import is_period
        self.assertFalse(is_period("2026-04\n"))
        self.assertTrue(is_period("2026-04"))
        with self.assertRaises(services.WorkflowError):
            services.compute(new_conn(), "2026-04\n")


class SameDayRefundTests(unittest.TestCase):
    """R-4 (external reviewer, executed): a refund whose id sorts before its same-day collection crashed the
    insert with a FOREIGN KEY error. Both row orders must commit and give +500 -200 = 300 at 5%."""

    ROWS = ["A-REFUND,REFUND,K-SGD-A,2026-01-02,SGD,40,Z-COLLECT,INV,Same-day refund",
            "Z-COLLECT,COLLECTION,K-SGD-A,2026-01-02,SGD,100,,INV,Collection"]

    def test_both_row_orders_commit_in_strict_and_quarantine_mode(self):
        for mode in ("strict", "quarantine"):
            for rows in (self.ROWS, self.ROWS[::-1]):
                with self.subTest(mode=mode, first=rows[0][:9]):
                    conn = new_conn()
                    load_standard(conn)
                    r = cash(conn, rows, mode=mode)
                    self.assertEqual((r.status, r.accepted, r.quarantined), ("COMMITTED", 2, 0), r.rows)
                    res = services.compute(conn, "2026-01")
                    amounts = sorted(ln["amount_minor"] for ln in res.lines)
                    self.assertEqual(amounts, [-200, 500])
                    self.assertEqual(res.totals[0]["net_minor"], 300)
                    src = {e: conn.execute("SELECT source_row FROM cash_events WHERE event_id = ?", (e,)).fetchone()[0]
                           for e in ("A-REFUND", "Z-COLLECT")}
                    self.assertEqual(src["A-REFUND"], 2 if rows[0].startswith("A-") else 3)   # provenance kept


class FileLevelTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)

    def test_duplicate_header_rejected(self):
        r = imp(self.conn, "cash_events", "event_id,event_type,contract_id,event_date,currency,amount,amount,"
                "original_event_id,invoice_ref,memo\n")
        self.assertEqual(r.status, "REJECTED")
        self.assertEqual(r.file_errors[0]["code"], "HEADER_DUPLICATE")

    def test_missing_and_unknown_headers_rejected(self):
        r = imp(self.conn, "cash_events", "event_id,event_type,contract_id,event_date,currency,amt,original_event_id,"
                "invoice_ref,memo\nE1,COLLECTION,K-SGD-A,2026-04-01,SGD,1.00,,,\n")
        self.assertEqual(r.status, "REJECTED")
        self.assertEqual({e["code"] for e in r.file_errors}, {"HEADER_MISSING", "HEADER_UNKNOWN"})
        self.assertEqual(count(self.conn, "cash_events"), 0)

    def test_empty_and_non_utf8(self):
        self.assertEqual(imp(self.conn, "cash_events", "").file_errors[0]["code"], "EMPTY_FILE")
        r = importer.import_csv(self.conn, "cash_events", "x.csv", b"\xff\xfe\x00bad", actor="t")
        self.assertEqual(r.file_errors[0]["code"], "ENCODING")

    def test_bom_and_blank_lines_and_column_order(self):
        text = ("﻿amount,event_id,event_type,contract_id,event_date,currency,original_event_id,invoice_ref,memo\n"
                "10.00,E1,COLLECTION,K-SGD-A,2026-04-01,SGD,,,\n\n  \n")
        r = imp(self.conn, "cash_events", text)
        self.assertEqual((r.status, r.accepted), ("COMMITTED", 1))

    def test_field_count_mismatch(self):
        r = cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-01,SGD,10.00,,"])
        self.assertIn("FIELD_COUNT", [c["code"] for c in r.rows[0]["reasons"]])


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)
        self.rows = ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,10000.00,,,", "E2,COLLECTION,K-SGD-A,2026-04-20,SGD,2000.00,,,",
                     "R1,REFUND,K-SGD-A,2026-04-25,SGD,500.00,E2,,"]
        self.text = CASH_HEADER + "".join(r + "\n" for r in self.rows)
        self.first = imp(self.conn, "cash_events", self.text, name="april.csv")

    def test_same_bytes_other_filename_is_noop(self):
        before = services.compute(self.conn, "2026-04").result_digest
        r = imp(self.conn, "cash_events", self.text, name="april_COPY_final_v2.csv")
        self.assertEqual(r.status, "DUPLICATE_FILE")
        self.assertEqual(r.duplicate_of, self.first.batch_id)
        self.assertEqual(count(self.conn, "cash_events"), 3)
        self.assertEqual(services.compute(self.conn, "2026-04").result_digest, before)

    def test_reordered_file_rows_are_duplicates(self):
        text = CASH_HEADER + "".join(r + "\n" for r in reversed(self.rows))
        r = imp(self.conn, "cash_events", text)
        self.assertEqual(r.status, "COMMITTED")
        self.assertEqual((r.accepted, r.duplicates, r.quarantined), (0, 3, 0))

    def test_conflicting_existing_row_is_quarantined_not_overwritten(self):
        r = cash(self.conn, ["E2,COLLECTION,K-SGD-A,2026-04-20,SGD,2500.00,,,"])
        codes, outcome = reasons_of(r, "E2")
        self.assertEqual(outcome, "QUARANTINED")
        self.assertIn("CONFLICT_EXISTING", codes)
        amt = self.conn.execute("SELECT amount_minor FROM cash_events WHERE event_id = 'E2'").fetchone()[0]
        self.assertEqual(amt, 200000)

    def test_conflict_inside_one_file_quarantines_all_copies(self):
        r = cash(self.conn, ["E9,COLLECTION,K-SGD-A,2026-04-21,SGD,10.00,,,", "E9,COLLECTION,K-SGD-A,2026-04-21,SGD,11.00,,,"])
        self.assertEqual((r.accepted, r.quarantined), (0, 2))
        self.assertTrue(all("CONFLICT_IN_FILE" in [c["code"] for c in x["reasons"]] for x in r.rows))

    def test_identical_duplicate_inside_file_counted_once(self):
        r = cash(self.conn, ["E9,COLLECTION,K-SGD-A,2026-04-21,SGD,10.00,,,", "E9,COLLECTION,K-SGD-A,2026-04-21,SGD,10.00,,,"])
        self.assertEqual((r.accepted, r.duplicates, r.quarantined), (1, 1, 0))

    def test_strict_mode_is_atomic(self):
        r = cash(self.conn, ["E7,COLLECTION,K-SGD-A,2026-04-21,SGD,10.00,,,", "E8,COLLECTION,K-SGD-A,2026-04-21,SGD,1e3,,,"],
                 mode="strict")
        self.assertEqual(r.status, "REJECTED")
        self.assertEqual(count(self.conn, "cash_events"), 3)
        self.assertEqual(count(self.conn, "quarantine_rows"), 0)
        batch = self.conn.execute("SELECT * FROM import_batches WHERE batch_id = ?", (r.batch_id,)).fetchone()
        self.assertEqual(batch["status"], "REJECTED")
        self.assertIn("AMOUNT_FORMAT", batch["control_json"])
        # a rejected file does not block resubmitting the same bytes later
        r2 = cash(self.conn, ["E7,COLLECTION,K-SGD-A,2026-04-21,SGD,10.00,,,", "E8,COLLECTION,K-SGD-A,2026-04-21,SGD,1e3,,,"],
                  mode="strict")
        self.assertEqual(r2.status, "REJECTED")

    def test_control_totals_reconcile(self):
        r = cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,10000.00,,,",       # duplicate
                             "E5,COLLECTION,K-SGD-A,2026-04-07,SGD,300.00,,,",         # accepted
                             "E6,COLLECTION,K-SGD-A,2026-04-08,SGD,0.00,,,",           # quarantined (zero)
                             "E7,COLLECTION,K-USD-A,2026-04-08,USD,5.00,,,",           # accepted USD
                             "E8,COLLECTION,K-SGD-A,2026-04-08,SGD,abc,,,"])           # quarantined, no amount
        c = r.control
        self.assertEqual(c["rows"], {"read": 5, "accepted": 2, "duplicate": 1, "quarantined": 2, "reconciles": True})
        sgd = c["amounts_minor"]["SGD"]["COLLECTION"]
        self.assertEqual(sgd, {"read": 1030000, "accepted": 30000, "duplicate": 1000000, "quarantined": 0,
                               "reconciles": True})
        self.assertEqual(c["amounts_minor"]["USD"]["COLLECTION"]["accepted"], 500)
        self.assertEqual(c["rows_without_parseable_amount"], 1)

    def test_quarantined_row_superseded_by_later_valid_row(self):
        services.set_business_date(self.conn, "2026-07-15", "t")
        r = cash(self.conn, ["F1,COLLECTION,K-SGD-A,2026-04-09,SGD,1,250.00,,,"])  # 10 fields -> FIELD_COUNT
        self.assertEqual(r.quarantined, 1)
        cash(self.conn, ["F1,COLLECTION,K-SGD-A,2026-04-09,SGD,1250.00,,,"])
        q = self.conn.execute("SELECT status FROM quarantine_rows").fetchone()
        self.assertEqual(q["status"], "SUPERSEDED")


class ReferenceDataTests(unittest.TestCase):
    def setUp(self):
        self.conn = new_conn()
        load_standard(self.conn)

    def test_split_total_and_group_integrity(self):
        imp(self.conn, "contracts", FIX["standard"]["contracts"].splitlines()[0] + "\n"
            "K-NEW,ACC-9,Nine (test),SGD,2026-01-15,1.00,T\nK-NEW2,ACC-9,Nine (test),SGD,2026-01-15,1.00,T\n")
        r = imp(self.conn, "splits", "contract_id,rep_id,split_pct\nK-NEW,REP-A,60\nK-NEW,REP-B,39.99\n"
                "K-NEW2,REP-A,50\nK-NEW2,REP-NOPE,50\n")
        by = {tuple(x["key"]): [c["code"] for c in x["reasons"]] for x in r.rows}
        self.assertIn("SPLIT_TOTAL_NOT_100", by[("K-NEW", "REP-A")])
        self.assertIn("UNKNOWN_REP", by[("K-NEW2", "REP-NOPE")])
        self.assertIn("SPLIT_GROUP_INCOMPLETE", by[("K-NEW2", "REP-A")])
        self.assertEqual(r.accepted, 0)

    def test_existing_split_is_immutable(self):
        r = imp(self.conn, "splits", "contract_id,rep_id,split_pct\nK-SGD-A,REP-B,100\n")
        self.assertIn("CONFLICT_EXISTING", [c["code"] for c in r.rows[0]["reasons"]])

    def test_account_name_conflict(self):
        r = imp(self.conn, "contracts", FIX["standard"]["contracts"].splitlines()[0] + "\n"
                "K-X,ACC-1,Different Name (test),SGD,2026-01-15,1.00,T\n")
        self.assertIn("ACCOUNT_NAME_CONFLICT", [c["code"] for c in r.rows[0]["reasons"]])

    def test_ambiguous_assignment_and_plan_rules(self):
        r = imp(self.conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
                "AS-X,REP-A,CASH-STD,2026-01-01,\n")
        self.assertIn("ASSIGNMENT_AMBIGUOUS", [c["code"] for c in r.rows[0]["reasons"]])
        r = imp(self.conn, "assignments", "assignment_id,rep_id,plan_id,effective_from,effective_to\n"
                "AS-Y,REP-A,CASH-STD,2026-02-15,\nAS-Z,REP-A,NOPE,2026-03-01,2026-03-15\n")
        codes = {x["key"][0]: [c["code"] for c in x["reasons"]] for x in r.rows}
        self.assertIn("EFFECTIVE_DATE_NOT_MONTH_START", codes["AS-Y"])
        self.assertIn("UNKNOWN_PLAN", codes["AS-Z"])
        self.assertIn("EFFECTIVE_TO_INVALID", codes["AS-Z"])
        r = imp(self.conn, "plans", FIX["plan_v2"].splitlines()[0] + "\n"
                "CASH-STD,3,SGD,2026-03-01,10000.00,5.00,9.00,out of order\n"
                "CASH-STD,4,SGD,2026-06-15,10000.00,5.00,9.00,mid month\n")
        codes = {tuple(x["key"]): [c["code"] for c in x["reasons"]] for x in r.rows}
        self.assertIn("EFFECTIVE_DATE_NOT_MONTH_START", codes[("CASH-STD", 4, "SGD")])

    def test_retroactive_plan_after_close(self):
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,"])
        close(self.conn, "2026-03") if False else None
        close(self.conn, "2026-04")
        r = imp(self.conn, "plans", FIX["plan_v2"].replace("2026-05-01", "2026-04-01"))
        self.assertTrue(all("RETROACTIVE_PLAN" in [c["code"] for c in x["reasons"]] for x in r.rows))
        r = imp(self.conn, "plans", FIX["plan_v2"])
        self.assertEqual(r.accepted, 2)

    def test_reimport_of_identical_old_plan_after_close_is_duplicate_not_error(self):
        cash(self.conn, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,"])
        close(self.conn, "2026-04")
        r = imp(self.conn, "plans", FIX["standard"]["plans"] + "\n")   # different bytes, same rows
        self.assertEqual((r.status, r.duplicates, r.quarantined), ("COMMITTED", 2, 0))

    def test_ids_are_case_sensitive_by_rejection(self):
        r = imp(self.conn, "reps", "rep_id,display_name,team\nrep-a,lower (test),T\n")
        self.assertIn("ID_FORMAT", [c["code"] for c in r.rows[0]["reasons"]])


class FuzzTests(unittest.TestCase):
    def test_random_garbage_never_crashes_and_always_reconciles(self):
        rng = random.Random(11)
        alphabet = "ABC0129-.,:;\"' =+@\t٣é"
        for i in range(150):
            conn = new_conn()
            load_standard(conn)
            rows = []
            for _ in range(rng.randint(1, 8)):
                fields = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 6))) for _ in range(9)]
                if rng.random() < 0.5:
                    fields[1] = rng.choice(["COLLECTION", "REFUND"])
                    fields[2] = rng.choice(["K-SGD-A", "K-USD-A", "K-NOPE"])
                    fields[3] = rng.choice(["2026-04-01", "2026-13-01", "x"])
                    fields[4] = rng.choice(["SGD", "USD"])
                rows.append(",".join('"' + f.replace('"', '""') + '"' for f in fields))
            r = cash(conn, rows)
            with self.subTest(i=i):
                self.assertIn(r.status, ("COMMITTED", "COMMITTED_WITH_QUARANTINE", "REJECTED"))
                if r.status != "REJECTED":
                    self.assertTrue(r.control["rows"]["reconciles"])
                    stored = count(conn, "cash_events") + count(conn, "quarantine_rows")
                    self.assertEqual(stored, r.accepted + r.quarantined)
                    for per in r.control.get("amounts_minor", {}).values():
                        self.assertTrue(all(b["reconciles"] for b in per.values()))


if __name__ == "__main__":
    unittest.main()
