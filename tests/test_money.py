"""Money primitives: parsing, rounding, allocation, cumulative reversal."""
import random
import re
import unittest
from fractions import Fraction
from pathlib import Path

from rcw.money import (MoneyFormatError, allocate_largest_remainder, cumulative_reversal, earning_for_portions,
                       format_minor, format_plain, half_up_div, parse_amount, parse_percent_to_bps)


class ParseTests(unittest.TestCase):
    def test_valid_amounts(self):
        for text, minor in [("0.01", 1), ("1", 100), ("1.5", 150), ("12345.67", 1234567), (" 7.00 ", 700),
                            ("999999999999.99", 99999999999999)]:
            with self.subTest(text=text):
                if minor > 100_000_000_000:
                    with self.assertRaises(MoneyFormatError):
                        parse_amount(text)
                else:
                    self.assertEqual(parse_amount(text), minor)

    def test_rejected_amounts(self):
        for text in ["", "abc", "1,250.00", "12.345", "1e3", "NaN", "inf", "-5", "+5", "$5", "5.", ".5", "1 000",
                     "0x10", "٣"]:
            with self.subTest(text=text):
                with self.assertRaises(MoneyFormatError):
                    parse_amount(text)

    def test_signed_amounts(self):
        self.assertEqual(parse_amount("-80.00", allow_negative=True), -8000)
        with self.assertRaises(MoneyFormatError):
            parse_amount("--80", allow_negative=True)

    def test_percent(self):
        self.assertEqual(parse_percent_to_bps("60"), 6000)
        self.assertEqual(parse_percent_to_bps("33.33"), 3333)
        self.assertEqual(parse_percent_to_bps("100.00"), 10000)
        for bad in ["100.01", "60.005", "-5", "abc", ""]:
            with self.subTest(bad=bad), self.assertRaises(MoneyFormatError):
                parse_percent_to_bps(bad)

    def test_format(self):
        self.assertEqual(format_minor(-8000, "SGD"), "SGD −80.00")
        self.assertEqual(format_minor(123456789, "USD"), "USD 1,234,567.89")
        self.assertEqual(format_plain(-8000), "-80.00")
        self.assertEqual(format_plain(5), "0.05")


class RoundingTests(unittest.TestCase):
    def test_half_up(self):
        self.assertEqual(half_up_div(5, 10), 1)       # 0.5 -> 1
        self.assertEqual(half_up_div(4, 10), 0)
        self.assertEqual(half_up_div(15, 10), 2)      # 1.5 -> 2 (not banker's 2? yes both)
        self.assertEqual(half_up_div(25, 10), 3)      # 2.5 -> 3 (banker's would give 2)
        with self.assertRaises(ValueError):
            half_up_div(-1, 10)

    def test_single_rounding_per_line(self):
        # 259,260 @5% + 240,740 @8% = 12,963 + 19,259.2 = 32,222.2 -> 32,222 (hand value)
        amount, exact = earning_for_portions(259260, 500, 240740, 800)
        self.assertEqual((amount, exact), (32222, Fraction(322222, 10)))


class AllocationTests(unittest.TestCase):
    def test_hand_values(self):
        self.assertEqual(allocate_largest_remainder(1234567, [("A", 6000), ("B", 4000)]), {"A": 740740, "B": 493827})
        self.assertEqual(allocate_largest_remainder(10001, [("A", 6000), ("B", 4000)]), {"A": 6001, "B": 4000})
        self.assertEqual(allocate_largest_remainder(1, [("REP-B", 5000), ("REP-A", 5000)]), {"REP-A": 1, "REP-B": 0})

    def test_duplicate_keys_rejected(self):
        # Reviewer regression: a repeated key used to overwrite a floor and lose cents.
        with self.assertRaises(ValueError):
            allocate_largest_remainder(100, [("A", 5000), ("A", 5000)])

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            allocate_largest_remainder(-1, [("A", 1)])
        with self.assertRaises(ValueError):
            allocate_largest_remainder(1, [("A", 0)])
        with self.assertRaises(ValueError):
            allocate_largest_remainder(1, [("A", -1), ("B", 2)])

    def test_conservation_and_order_independence_random(self):
        rng = random.Random(20260926)
        for _ in range(3000):
            n = rng.randint(1, 5)
            keys = [f"R{i}" for i in rng.sample(range(50), n)]
            weights = [(k, rng.randint(1, 10000)) for k in keys]
            total = rng.randint(0, 10_000_000)
            a = allocate_largest_remainder(total, weights)
            shuffled = weights[:]
            rng.shuffle(shuffled)
            self.assertEqual(a, allocate_largest_remainder(total, shuffled))
            self.assertEqual(sum(a.values()), total)
            wsum = sum(w for _, w in weights)
            for k, w in weights:   # every share within one cent of its exact quota
                self.assertLess(abs(Fraction(a[k]) - Fraction(total * w, wsum)), 1)


class CumulativeReversalTests(unittest.TestCase):
    def test_reviewer_case_ten_cents_three_parts(self):
        cums = [cumulative_reversal(10, r, 200) for r in (0, 67, 134, 200)]
        self.assertEqual([b - a for a, b in zip(cums, cums[1:])], [3, 4, 3])
        naive = sum(half_up_div(10 * r, 200) for r in (67, 67, 66))
        self.assertEqual(naive, 9, "naive per-refund rounding drifts; documented as the reason for the method")

    def test_full_refund_always_exact_random_partitions(self):
        rng = random.Random(7)
        for _ in range(2000):
            x = rng.randint(1, 5_000_000)
            e = rng.randint(0, x)
            cuts = sorted(rng.sample(range(1, x), min(rng.randint(0, 6), x - 1))) if x > 1 else []
            parts = [b - a for a, b in zip([0] + cuts, cuts + [x])]
            done, total_rev = 0, 0
            for p in parts:
                before = cumulative_reversal(e, done, x)
                done += p
                step = cumulative_reversal(e, done, x) - before
                self.assertGreaterEqual(step, 0)
                total_rev += step
            self.assertEqual(total_rev, e)


class NoFloatInMoneyPathTests(unittest.TestCase):
    def test_no_float_arithmetic_in_money_modules(self):
        root = Path(__file__).resolve().parent.parent / "rcw"
        for name in ("money.py", "engine.py", "importer.py", "services.py", "variance.py", "exports.py"):
            src = (root / name).read_text(encoding="utf-8")
            code = "\n".join(line.split("#")[0] for line in src.splitlines())
            with self.subTest(module=name):
                self.assertIsNone(re.search(r"\bfloat\s*\(", code), f"float() used in {name}")
                self.assertIsNone(re.search(r"(?<![\w.])round\s*\(", code), f"builtin round() used in {name}")


if __name__ == "__main__":
    unittest.main()
