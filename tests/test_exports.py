"""Exports: reproducible frozen statements, CSV formula protection, HTML escaping."""
import csv
import io
import unittest

from rcw import exports, services, variance
from rcw.exports import Num, csv_safe
from tests.helpers import FIX, PREPARER, REVIEWER, cash, close, imp, load_standard, new_conn


class CsvSafetyTests(unittest.TestCase):
    def test_formula_prefixes_escaped_numbers_untouched(self):
        for text in ("=SUM(1,2)", "+1", "-cmd", "@x", "\tTab", "\rCR"):
            self.assertEqual(csv_safe(text), "'" + text)
        self.assertEqual(csv_safe(Num("-80.00")), "-80.00")
        self.assertEqual(csv_safe(-8000), "-8000")
        self.assertEqual(csv_safe("plain"), "plain")
        self.assertEqual(csv_safe(None), "")


class FrozenExportTests(unittest.TestCase):
    def test_closed_export_identical_after_later_changes(self):
        """Reviewer supplement (e): later plan change, import, adjustment and case resolution change nothing."""
        c = new_conn()
        load_standard(c)
        cash(c, ["E1,COLLECTION,K-6040,2026-04-06,SGD,20000.00,,,", "E2,COLLECTION,K-USD-A,2026-04-08,USD,99.99,,,"])
        imp(c, "recorded_payouts", "record_id,rep_id,period,currency,amount,source_system,memo\n"
            "P1,REP-A,2026-04,SGD,600.00,SIM,april\n")
        close(c, "2026-04")
        before = {"csv": exports.statement_csv(exports.statement_model(c, "2026-04")),
                  "html": exports.statement_html(exports.statement_model(c, "2026-04")),
                  "control_frozen": _frozen_part(exports.control_csv(exports.control_report(c, "2026-04")))}
        imp(c, "plans", FIX["plan_v2"])
        cash(c, ["E3,COLLECTION,K-6040,2026-05-06,SGD,5000.00,,,", "R1,REFUND,K-6040,2026-05-07,SGD,1000.00,E1,,",
                 "L1,COLLECTION,K-SGD-A,2026-04-30,SGD,10.00,,,"])
        services.decide_event(c, "L1", "POST_LATE", "late bank feed for April receipts", REVIEWER)
        services.add_adjustment(c, "2026-05", "REP-A", "SGD", -100, "PAYOUT_CORRECTION",
                                "recover April overpayment next month", PREPARER)
        case = c.execute("SELECT case_id FROM variance_cases").fetchone()[0]
        variance.update_case(c, case, "ops-1", status="RESOLVED", reason_code="MANUAL_ENTRY_ERROR",
                             resolution_note="register keyed 600.00 instead of 660.00")
        close(c, "2026-05")
        imp(c, "recorded_payouts", "record_id,rep_id,period,currency,amount,source_system,memo\n"
            "P2,REP-A,2026-04,SGD,60.00,SIM,top-up recorded after close\n")
        after = {"csv": exports.statement_csv(exports.statement_model(c, "2026-04")),
                 "html": exports.statement_html(exports.statement_model(c, "2026-04")),
                 "control_frozen": _frozen_part(exports.control_csv(exports.control_report(c, "2026-04")))}
        self.assertEqual(before, after)
        live = exports.control_csv(exports.control_report(c, "2026-04"))
        self.assertIn("RESOLVED", live)           # live case status is reported separately
        self.assertIn("recorded_payout,LIVE,REP-A,SGD,recorded,660.00", live)   # 600 + 60 recorded, live
        self.assertTrue(services.get_snapshot(c, "2026-04")["_integrity"]["verified"])

    def test_statement_csv_structure_and_escaping(self):
        c = new_conn()
        imp(c, "reps", "rep_id,display_name,team\nREP-A,=HYPERLINK(\"http://x\") <script>alert(1)</script>,T\n")
        for kind in ("plans", "assignments", "contracts", "splits"):
            text = FIX["standard"][kind]
            if kind in ("assignments", "splits"):
                text = "\n".join(l for l in text.splitlines() if "REP-B" not in l and "REP-C" not in l
                                 and "6040" not in l and "5050" not in l) + "\n"
            imp(c, kind, text)
        cash(c, ["E1,COLLECTION,K-SGD-A,2026-04-06,SGD,100.00,,,"])
        services.add_adjustment(c, "2026-04", "REP-A", "SGD", -100, "COMMISSION_ADJUSTMENT",
                                "<b>bold</b> and =cmd in a reason", PREPARER)
        model = exports.statement_model(c, "2026-04")
        rows = list(csv.reader(io.StringIO(exports.statement_csv(model))))
        header = rows[0]
        first = dict(zip(header, rows[1]))
        self.assertTrue(first["rep_name"].startswith("'="))
        self.assertEqual(first["amount"], "5.00")
        adj = dict(zip(header, rows[2]))
        self.assertEqual(adj["amount"], "-1.00")          # numeric, not formula-escaped
        html = exports.statement_html(model)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<b>bold</b>", html)


def _frozen_part(control_csv_text: str) -> str:
    rows = list(csv.reader(io.StringIO(control_csv_text)))
    frozen = [r for r in rows[1:] if r[1] == "FROZEN"]
    assert len(frozen) > 10, frozen
    return repr(frozen)


if __name__ == "__main__":
    unittest.main()
