# Revenue & Commission Operations Workbench — one-page overview

Each month a sales-operations team must work out what each salesperson is owed in commission,
explain each amount, and keep a finished month from changing when late data arrives. This web
application calculates commission from spreadsheet (CSV) files, links every amount to its source row
and locks each reviewed month. Each difference from the payroll system becomes an investigation case
with a suggested cause. Resolving the case records an explanation and does not change or pay any
amount.

## What it does

- **Imports with checks**: rows with problems are held back with a reason, not silently dropped, and a
  file loaded twice is skipped.
- **Pays on money received**, not on contracts signed or invoices sent. Shared deals are split to the
  exact cent, and SGD and USD are never combined.
- **Locks a reviewed month**: the preparer cannot also close it, and later data never reopens it.

## Example from the demo

The invented plan pays 5% on the first 10,000 collected in a month and 8% above that.

1. A customer pays **USD 12,345.67** on a deal shared 60/40, so Cedar is credited **7,407.40** and Delta
   **4,938.27**. The odd cent goes to the larger remainder, so the credits add up exactly to the payment.
2. A later **5,000.00** payment takes Cedar past 10,000: 2,592.60 earns 5% and 2,407.40 earns 8%, which
   is 322.22. Cedar's April commission is **692.59**.
3. The payroll register shows **620.37**. The workbench opens a **−72.22** case and points out that
   payroll applied 5% to everything, missing the extra 3% on 2,407.40.
4. After April closes, a 1,500.00 payment dated 29 April arrives. April is not reopened: May stays
   blocked until a reviewer, giving a reason, records it in May as an April correction of **120.00**
   (April's 8%). April's exported statement is unchanged.

## See it

- A 3-minute demo script is in [`07_demo_script.md`](07_demo_script.md); setup is in the
  [README](../README.md#run-it).
- Offline replay of recorded screenshots, labelled as such:
  [`presentation/walkthrough.html`](presentation/walkthrough.html).
- Screenshots:
    - [statement with the shared deal and threshold](evidence/screenshots/04_statement_cedar_split_crossing.png)
    - [late payment blocking May](evidence/screenshots/06_may_late_hold_blocks_close.png)
    - [payroll case resolved](evidence/screenshots/10_case_resolved.png)
    - [all 13](evidence/screenshots/)

![April closed: totals per currency, charts and control checks](evidence/screenshots/03_april_closed.png)

## Stack

Python (standard library only for the calculations and command-line demo), SQLite in one local file,
Flask web pages served on the local computer, Playwright browser checks and GitHub Actions CI.

Amounts are stored as whole cents, not binary floating-point numbers, which avoids floating-point
rounding errors. Rounding and cent-allocation rules still apply and are tested.

## Checks

The code reproduces 16 hand-worked scenarios and passes 88 automated tests and 19 browser checks. On
10,000 generated payments it matches a separate reference calculation on all 480 totals. 11 defects
were fixed, each with a test ([worked examples](../tests/expected/HAND_CALCULATIONS.md),
[benchmark](08_benchmark.md), [defect log](04_defects_log.md)).

## Scope and limits

- **Data**: every salesperson, customer, plan and payroll record is invented. Nothing connects to a
  real payroll, bank or CRM system, and no payments are made.
- **Rules**: the commission rules are one invented example
  ([alternatives](03_policy_decision_log.md)).
- **Validation**: the checks show arithmetic consistency with those rules; nothing has been tested
  with real users or business data ([coverage matrix](05_uat_matrix.md)).
- **Access and audit**: user names are labels, not logins. The change log can't be edited in the
  application, but whoever has the database file could alter it.
- **Out of scope**:
    - currency conversion;
    - accounting revenue rules;
    - carrying negative balances forward;
    - multi-user or cloud hosting.
- **Screenshots**: recorded from an earlier version; four show older on-screen wording, with the same
  numbers ([provenance](evidence/README.md)).
