# Hand calculations (written before the engine existed)

All money is in **minor units (cents)**. Rates are in **basis points** (500 bps = 5.00%).
`half_up(a / b)` means: round the exact fraction to the nearest cent, ties away from zero
(for non-negative values: `(2a + b) // (2b)`). Reversals are computed as positive magnitudes
and then negated, so a clawback is the mirror image of an earning.

Demo plan: threshold 1,000,000¢ (10,000.00); v1 base 500 bps / accelerator 800 bps;
v2 (effective 2026-05-01) base 500 bps / accelerator 900 bps.

These numbers are typed by hand into `hand_calculations.json`. Tests read the JSON and compare
against the engine output. Production code never generates or reads these values.

---

### HC-01 Threshold crossing (brief example)
Rep A, 100% credit, April (plan v1).
* 04-06 collection 1,000,000 → all below threshold → 1,000,000 × 500 / 10,000 = **50,000** (500.00)
* 04-20 collection 200,000 → attainment already 1,000,000, all above → 200,000 × 800 / 10,000 = **16,000** (160.00)
* April total **66,000** (660.00)

### HC-02 Refund after a later plan change reverses the ORIGINAL earning
* v2 (accelerator 9%) imported, effective 2026-05-01.
* 05-12 refund 100,000 of the 04-20 collection (x = 200,000, original earning E = 16,000).
* cumulative reversal = half_up(16,000 × 100,000 / 200,000) = 8,000 → line **−8,000** (−80.00).
* Using today's 9% would give −9,000; the policy forbids that.

### HC-03 New cash under v2
* 05-08 collection 1,100,000 → May attainment starts at 0: 1,000,000 × 5% = 50,000; 100,000 × 9% = 9,000 → **59,000**.
* Rep A May net = 59,000 − 8,000 = **51,000** (510.00).

### HC-04 Crossing inside one collection
* 04-03 collection 900,000 → 45,000.
* 04-09 collection 300,000 → below portion 100,000 × 5% = 5,000; above 200,000 × 8% = 16,000 → **21,000**.
* April total **66,000**.

### HC-05 Split 60/40 of 12,345.67 (odd cent)
* 1,234,567 × 6,000 / 10,000 = 740,740.2 → floor 740,740 (remainder .2)
* 1,234,567 × 4,000 / 10,000 = 493,826.8 → floor 493,826 (remainder .8)
* floors sum 1,234,566 → 1 residual cent to the largest remainder (the 40% rep) → **740,740 / 493,827**, sum 1,234,567 ✓
* earnings at 5%: 740,740 × 500 / 10,000 = 37,037.0 → **37,037**; 493,827 × 500 / 10,000 = 24,691.35 → **24,691**

### HC-06 Split 60/40 of 100.01 (reviewer case d)
* 10,001 × 0.6 = 6,000.6 → 6,000 (.6); 10,001 × 0.4 = 4,000.4 → 4,000 (.4); residual 1 → larger remainder (60% rep)
* → **6,001 / 4,000**, sum 10,001 ✓

### HC-07 Split 50/50 of 0.01 (tie)
* 1 × 0.5 = 0.5 each → floors 0/0, residual 1, remainders tie → tie broken by rep id ascending
* `REP-A` gets **1**, `REP-B` gets **0** (a zero-credit line is still shown for transparency).

### HC-08 10¢ commission, full refund in three parts (reviewer case c)
* collection 200 (2.00) × 5% = **10** cents earned.
* refunds 67, 67, 66 (sum 200 = full refund)
  * after 67:  half_up(10 × 67 / 200) = half_up(3.35) = 3 → reversal **−3**
  * after 134: half_up(10 × 134 / 200) = half_up(6.70) = 7 → reversal 7 − 3 = **−4**
  * after 200: 10 → reversal 10 − 7 = **−3**
* total reversal **−10** exactly. (Naive per-refund rounding: 3 + 3 + 3 = 9 → 1¢ drift.)

### HC-09 Cumulative reversal on an awkward fraction
* credited 33,333 × 5% = 1,666.65 → half_up → **1,667**.
* three refunds of 11,111 each:
  * cum 1: 1,667 × 11,111 / 33,333 = 555.67 → 556 → **−556**
  * cum 2: 1,667 × 22,222 / 33,333 = 1,111.33 → 1,111 → **−555**
  * cum 3: 1,667 → **−556**
* total −1,667 ✓ (naive: 3 × 556 = 1,668 → drift).

### HC-10 Refund does not restore tier capacity
* 04-02 collection 1,000,000 → 50,000
* 04-10 refund 500,000 of it → −half_up(50,000 × 500,000 / 1,000,000) = **−25,000**
* 04-15 collection 200,000 → attainment (gross positive) already 1,000,000 → all above → **16,000**
* April net **41,000** (if capacity were restored it would be 10,000 for the last line → 35,000).

### HC-11 Plan boundary (month-aligned versions)
* 04-30 collection 1,100,000 (v1): 50,000 + 100,000 × 8% = 8,000 → **58,000**
* 05-01 collection 1,100,000 (v2; May attainment restarts): 50,000 + 100,000 × 9% = 9,000 → **59,000**

### HC-12 Currencies have separate ladders
* April: 600,000 SGD and 600,000 USD for the same rep → **30,000 SGD** and **30,000 USD**; no accelerator (no FX, no combined ladder).

### HC-13 Late collection after close
* April closed; the rep's frozen April USD attainment is 1,240,740.
* Late collection dated 04-29, 150,000 USD, imported after close → blocking hold in May.
* Reviewer posts it as a prior-period adjustment in May: it takes the next slice of April's ladder
  (all above threshold) at April's plan v1 8% → **12,000**, line type LATE_EARNING, original period 2026-04.
* April's closed export is byte-identical before and after.

### HC-14 Split with a threshold crossing for one rep
* Rep A already has 800,000 credited this month.
* Collection 2,000,000 split A 60% / B 40% → A 1,200,000, B 800,000.
* A: below 200,000 × 5% = 10,000; above 1,000,000 × 8% = 80,000 → **90,000**.
* B (no prior attainment): 800,000 × 5% → **40,000**.

### HC-15 Full refund of a split collection returns every rep to exactly zero
* HC-05 collection fully refunded later: reversals **−37,037** and **−24,691**; credit reversed **740,740** and **493,827**.

### HC-16 Credit reversal on an odd split with partial refunds
* collection 1,001 split 50/50: 500.5 each → tie → `REP-A` 501, `REP-B` 500. Earnings 25.05 → 25 and 25.00 → 25.
* refund 1: credit reversal allocated by *not-yet-reversed* credit (501, 500) → A 1, B 0.
  Earnings: half_up(25 × 1 / 1,001) = 0 for both.
* refund 1,000 (completes the refund): remaining credit (500, 500) → 500 / 500; earnings: 25 − 0 = 25 each.
* Totals: credit reversed 501 / 500, earnings reversed 25 / 25 — exact.

---

## Demo story (numbers the demo must reproduce)

| Period | Rep | Currency | Lines | Expected |
|---|---|---|---|---|
| 2026-04 | Aurora | SGD | 1,000,000 @5% = 50,000; 200,000 @8% = 16,000 | **66,000** (660.00) |
| 2026-04 | Birch | SGD | 400,000 @5% | **20,000** (200.00) |
| 2026-04 | Cedar | USD | split 60% of 1,234,567 = 740,740 → 37,037; then 500,000: 259,260 @5% = 12,963 + 240,740 @8% = 19,259.2 → exact 32,222.2 → 32,222 | **69,259** (692.59) |
| 2026-04 | Delta | USD | split 40% = 493,827 @5% = 24,691.35 → 24,691 | **24,691** (246.91) |
| 2026-05 | Aurora | SGD | 1,100,000 under v2 = 59,000; clawback −8,000 (original 8%) | **51,000** (510.00) |
| 2026-05 | Cedar | USD | split 60% of 300,000 = 180,000 @5% = 9,000; late April 150,000 @8% = 12,000 | **21,000** (210.00) |
| 2026-05 | Delta | USD | split 40% of 300,000 = 120,000 @5% = 6,000 | **6,000** (60.00) |

Recorded payouts (synthetic register) and the resulting variances:

| Period | Rep | Currency | Recorded | Expected | Variance (recorded − expected) | Planted cause |
|---|---|---|---|---|---|---|
| 2026-04 | Cedar | USD | 62,037 | 69,259 | **−7,222** | register paid base rate on the whole credit: accelerator uplift 240,740 × 3% = 7,222.2 |
| 2026-05 | Aurora | SGD | 59,000 | 51,000 | **+8,000** | register missed the −80.00 clawback |
| 2026-05 | Cedar | USD | 9,000 | 21,000 | **−12,000** | register omitted the late April adjustment |
