# Revenue & Commission Operations Workbench — overview for recruiters and hiring managers

**In one sentence:** a small web application that shows how a Sales/Revenue Operations analyst can
work out what each salesperson is owed in commission, explain every number, and lock a finished month
so later data cannot quietly change it.

It is a **portfolio prototype built on invented data**. It is not used by any company, is not connected
to any real payroll, bank or CRM system, and no business results are claimed.

## Who did what

| Role | Who |
|---|---|
| Brief, scope and requirements | **Herui** (repository owner) |
| Code, tests, documentation, screenshots | **Claude Code** (an AI coding assistant) |
| Independent review — found and reported 5 defects, all fixed | **Codex** (a separate AI reviewer, acting for Herui) |

Herui's own understanding of the design and the numbers is **not claimed here**. It is to be shown by
Herui directly, for example by running the demo live, walking through a calculation by hand and
answering questions about the choices below.

## The problem it addresses

At every month-end a sales operations team has to answer:

- How much money did customers actually pay us? This is different from contracts signed or invoices sent.
- How much of that money earns commission, and for which salesperson? Deals are often shared.
- Why is a salesperson's commission different from last time? Refunds, late payments, corrections.
- How do we stop a finished month from being changed afterwards?
- Why does the payroll system show a different amount from what we calculated?

## A normal flow (April, from the demo)

1. The analyst loads seven spreadsheet files: salespeople, commission plan, which plan each person is on,
   contracts, deal shares, payments and refunds received, and the payroll register.
2. A customer pays **USD 12,345.67**. Two salespeople share that deal 60/40, so they are credited
   **7,407.40** and **4,938.27**. The odd cent goes to whoever is owed the larger fraction, so the
   credited amounts always add up exactly to what the customer paid.
3. The plan pays 5% on the first 10,000 collected in a month and 8% on anything above that. One
   salesperson passes 10,000 during April, so part of one payment earns 5% and the rest earns 8%.
   The screen shows both parts.
4. The preparer submits April for review. The system will not let the same person close the month,
   so a second person reviews and **closes** it. April's numbers are then frozen.

## A failure flow (what happens when things go wrong)

1. In June, a bank feed delivers a payment dated **29 April**, after April was closed.
2. The system does **not** reopen April. Instead, May shows a red warning and cannot be submitted
   until someone decides what to do.
3. A reviewer enters a reason and records the payment as a correction inside May, clearly marked
   "belongs to April". April's exported statement is exactly the same as before.
4. Separately, the payroll register paid one salesperson **80.00 more** than calculated. The
   workbench opens an investigation case and points out that 80.00 is exactly a refund
   deduction the payroll team missed. Closing the case records the explanation. It does not mean
   the money has been corrected or paid.

## What can be shown in 3 minutes

| Time | What you see |
|---|---|
| 0:00 | Load the demo data. Three planted problems (four rows) are held back with a reason instead of being silently dropped. |
| 0:40 | Calculate April. Open one salesperson's statement and click through from a commission line to the original payment row. |
| 1:20 | Submit, then try to close as the same person (refused). Close as a second person. |
| 1:40 | Load the June data. May is blocked by the late April payment until a reasoned decision is made. |
| 2:10 | A partial refund reduces commission at the original rate, not at the new plan's higher rate. |
| 2:25 | Compare with the payroll register and resolve one difference with a reason. |

A 3-minute script is in [`07_demo_script.md`](07_demo_script.md). If the application cannot be started,
[`presentation/walkthrough.html`](presentation/walkthrough.html) is an offline replay. It contains recorded
screenshots and is clearly labelled as a replay, not a live system.

## Screenshots

Screenshots are recorded outputs of a browser run at commit `8e8f7cc`. Four of them show wording that was
later corrected (numbers unchanged); see [evidence/README.md](evidence/README.md).

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

## Evidence (all on invented data)

- **Worked examples first:** 16 business scenarios were calculated by hand and saved *before* the
  calculation code was written. The code reproduces all 16.
  [Worked examples](../tests/expected/HAND_CALCULATIONS.md).
- **Automated tests:** 88 tests.
  - With the web dependencies installed, all 88 pass, plus 301 sub-checks.
  - Without any installation, 81 run and pass, and the 7 web tests are skipped.
  - [Test log](evidence/test_results.txt).
- **Browser check:** a real browser clicks through the whole story and passes 19 checks, including no
  sideways scrolling on a phone-sized screen. [Results](evidence/e2e_results.json).
- **Larger data run:** 10,000 invented payments and refunds. A separate, simpler reference calculation
  agreed with the application on all 480 salesperson-month-currency totals, and no cent was lost.
  [Benchmark report](08_benchmark.md).
- **Problems found and fixed:** 11 real defects, each with the test that now guards it: 6 found by the
  builder's own tests and 5 by the AI reviewer. [Defect log](04_defects_log.md).
- **What was *not* done:** no testing with real users or a real business. [Test coverage matrix](05_uat_matrix.md).

These checks show that the arithmetic is consistent with the invented rules. They do not show that
the rules match any real company's commission plan, or that the tool saves anyone time.

## Limitations

- **User names are labels, not logins.** The "different person must close the month" rule is
  demonstrated, not enforced by real security.
- **The change log cannot be edited through the application,** but anyone who has the database file
  could alter it.
- **Out of scope:**
  - payments to salespeople;
  - currency conversion;
  - accounting revenue rules;
  - carrying negative balances forward;
  - multi-user or cloud hosting.
- **The commission rules are one invented example.** They are chosen to make the mechanics visible and
  are documented with alternatives in [`03_policy_decision_log.md`](03_policy_decision_log.md).

## Where to look next

- [README](../README.md): how to run it.
- [Requirements and acceptance criteria](01_requirements_acceptance.md).
- [Data dictionary](02_data_dictionary.md).
