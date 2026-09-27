# Revenue & Commission Operations Workbench — one-page overview

Every month a sales-operations team has to work out what each salesperson is owed in commission,
explain each amount, and make sure a finished month does not change when late or corrected data
arrives. This web application does that from spreadsheet (CSV) files. It shows every number's source,
locks each reviewed month, and turns later changes and payroll differences into recorded, explained
corrections.

## The questions it answers

| Question | What the tool does |
|---|---|
| How much money did customers actually pay? This is different from contracts signed or invoices sent. | Shows contracts, payments and refunds separately, per currency. |
| How much of that earns commission, and for which salesperson? Deals are often shared. | Splits each payment between salespeople to the exact cent and applies the commission plan. |
| Why is a salesperson's commission different from last time? | Lists refunds, late payments and manual corrections as separate lines, each with its reason. |
| How do we stop a finished month from being changed afterwards? | Freezes the reviewed month. Later data goes into the next open month as a marked correction. |
| Why does the payroll system show a different amount? | Opens an investigation case for each difference and suggests a likely cause. |

## A normal month (April, from the demo)

1. The analyst loads seven files: salespeople, commission plan, which plan each person is on,
   contracts, deal shares, payments and refunds received, and the payroll register. Four rows with
   planted problems are held back with a reason instead of being silently dropped.
2. A customer pays **USD 12,345.67**. Two salespeople share that deal 60/40, so they are credited
   **7,407.40** and **4,938.27**. The odd cent goes to whoever is owed the larger fraction, so the
   credited amounts always add up exactly to what the customer paid.
3. The plan pays 5% on the first 10,000 collected in a month and 8% on anything above that. One
   salesperson passes 10,000 during April with a 5,000.00 payment: 2,592.60 of it earns 5% and
   2,407.40 earns 8%, which is 322.22 in total. The screen shows both parts.
4. The preparer submits April for review. The same person cannot also close it, so a second person
   reviews and **closes** April. Its numbers are then frozen.

## When something goes wrong

1. A file loaded in June contains a payment dated **29 April**, after April was closed.
2. April is **not** reopened. Instead, May shows a blocking warning and cannot be submitted until
   someone decides what to do.
3. A reviewer enters a reason and records the payment as a correction inside May, marked as belonging
   to April. April's exported statement is exactly the same as before.
4. A customer refunds 1,000 of a 2,000 April payment. The commission taken back is **80.00**, at April's
   8% rate, even though the plan has since raised that rate to 9%.
5. The payroll register shows one salesperson receiving **80.00 more** than calculated. The workbench
   opens an investigation case and points out that 80.00 is exactly the refund deduction payroll missed.
   Closing the case records the explanation. It does not correct or pay the money.

## The demo in 3 minutes

| Time | What you see |
|---|---|
| 0:00 | Load the demo data. Four rows from three planted problems are held back, each with a reason. |
| 0:40 | Calculate April. Open one salesperson's statement and click from a commission line through to the original payment row. |
| 1:20 | Submit, then try to close as the same person (refused). Close as a second person. |
| 1:40 | Load the June data. The late April payment blocks May until a reasoned decision is made. |
| 2:10 | The partial refund reduces commission at the original rate, not at the new plan's higher rate. |
| 2:25 | Compare with the payroll register and resolve one difference with a reason. |

The spoken script is in [`07_demo_script.md`](07_demo_script.md). An offline replay with recorded
screenshots, labelled as a replay, is at [`presentation/walkthrough.html`](presentation/walkthrough.html).
Setup commands are in the [README](../README.md#run-it).

## Screenshots

| Screen | Link |
|---|---|
| April closed and locked: totals per currency, charts, control checks | [03_april_closed.png](evidence/screenshots/03_april_closed.png) |
| One salesperson's statement: shared deal and the 10,000 threshold | [04_statement_cedar_split_crossing.png](evidence/screenshots/04_statement_cedar_split_crossing.png) |
| Tracing one commission line back to the source payment | [05_line_evidence_threshold_crossing.png](evidence/screenshots/05_line_evidence_threshold_crossing.png) |
| Late April payment blocking May | [06_may_late_hold_blocks_close.png](evidence/screenshots/06_may_late_hold_blocks_close.png) |
| Payroll difference investigation, resolved with a reason | [10_case_resolved.png](evidence/screenshots/10_case_resolved.png) |
| Import check: every row accepted, skipped as a duplicate, or held back | [12_import_batch_control_totals.png](evidence/screenshots/12_import_batch_control_totals.png) |
| Same page on a phone-sized screen | [13_mobile_period_may.png](evidence/screenshots/13_mobile_period_may.png) |

![April closed](evidence/screenshots/03_april_closed.png)

## Technology

- **Python** for the calculations and the command-line demo. They use only the standard library, so
  there is nothing to install for that part.
- **SQLite** as a single local database file.
- **Flask** for the web pages. It runs on the local computer only.
- **Playwright** for automated browser checks.
- **GitHub Actions** runs the checks on every change.

Money is stored as whole cents, never as floating-point decimals, so amounts cannot drift.

## Checks

- **Worked examples**: 16 scenarios were calculated by hand before the calculation code was written,
  and the code reproduces all 16 ([worked examples](../tests/expected/HAND_CALCULATIONS.md)).
- **Automated tests**: 88 tests.
    - With the web dependencies installed, all 88 pass, plus 301 sub-checks.
    - With no installation, 81 run and pass, and the 7 web tests are skipped.
    - See the [test log](evidence/test_results.txt).
- **Browser check**: a real browser clicks through the whole story and passes 19 checks, including no
  sideways scrolling on a phone-sized screen ([results](evidence/e2e_results.json)).
- **Larger data run**: 10,000 generated payments and refunds. A separate, simpler reference
  calculation agrees with the application on all 480 salesperson-month-currency totals
  ([benchmark report](08_benchmark.md)).
- **Defects**: 11 defects found and fixed, each guarded by a test ([defect log](04_defects_log.md)).

## Scope and limits

- **Data**: every salesperson, customer, plan and payroll record is invented. The tool is not connected
  to any real payroll, bank or CRM system, and no business results are claimed.
- **Rules**: the commission rules are one invented example, chosen to make the mechanics visible.
  Alternatives are documented in [`03_policy_decision_log.md`](03_policy_decision_log.md).
- **Validation**: the checks show the arithmetic is consistent with those rules. There has been no
  testing with real users or real business data ([coverage matrix](05_uat_matrix.md)).
- **Access control**: user names are labels, not logins, so the "different person must close the
  month" rule is demonstrated, not enforced by real security.
- **Change log**: the log can't be edited through the application, but anyone who has the database
  file could alter it.
- **Screenshots**: they were recorded from an earlier version of the application. Four of them show
  on-screen wording that was later changed; the numbers are the same
  ([provenance](evidence/README.md)).
- **Out of scope**:
    - paying salespeople;
    - currency conversion;
    - accounting revenue rules;
    - carrying negative balances forward;
    - multi-user or cloud hosting.

## Where to look next

- [README](../README.md): capabilities, setup and project layout.
- [Requirements and acceptance criteria](01_requirements_acceptance.md).
- [Data dictionary](02_data_dictionary.md).
