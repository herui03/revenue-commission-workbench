"""Hand-computed business scenarios (HC-xx) run through importer -> engine -> workflow.

Expected numbers come only from tests/expected/hand_calculations.json (typed by hand before the
engine existed). Nothing here derives an expectation from production code.
"""
import unittest

from tests.helpers import EXPECTED, FIELD_MAP, find_line, lines_for, run_scenario, totals_for


class HandScenarioTests(unittest.TestCase):
    def test_every_hand_scenario(self):
        self.assertGreaterEqual(len(EXPECTED["scenarios"]), 15)
        for sc in EXPECTED["scenarios"]:
            with self.subTest(scenario=sc["id"], title=sc["title"]):
                conn = run_scenario(sc)
                for period, expected_lines in sc.get("expect_lines", {}).items():
                    lines = lines_for(conn, period)
                    for exp in expected_lines:
                        ln = find_line(lines, exp["type"], exp["event"], exp["rep"])
                        self.assertIsNotNone(ln, f"{sc['id']}: missing {exp['type']} {exp['event']} {exp['rep']}")
                        for field, col in FIELD_MAP.items():
                            if field in exp:
                                self.assertEqual(ln[col], exp[field],
                                                 f"{sc['id']} {ln['line_key']} {field}: {ln[col]} != {exp[field]}")
                for period, reps in sc.get("expect_totals", {}).items():
                    self.assertEqual(totals_for(conn, period), reps, f"{sc['id']} totals {period}")


if __name__ == "__main__":
    unittest.main()
