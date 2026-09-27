# 02 · Data dictionary

All files are UTF-8 CSV with a header row. Columns may appear in any order but names must match
exactly; a missing, unknown or duplicated column rejects the whole file (`HEADER_MISSING`,
`HEADER_UNKNOWN`, `HEADER_DUPLICATE`). Blank lines are ignored. Max 5 MB / 200,000 rows per file.

## Common formats

| Kind | Rule | Examples ✓ | Rejected ✗ |
|---|---|---|---|
| ID | 1–40 chars `A-Z 0-9 _ -`, starts with letter/digit, **uppercase only, never normalised** | `E-2026-0401`, `REP_07` | `rep-a` (lower case), `A:B`, `A|B`, `A B` |
| Date | ISO `YYYY-MM-DD`, real calendar date, years 2000–2099 | `2026-04-30` | `2026-02-30`, `06/04/2026`, `20260406`, `0001-01-01` |
| Period | `YYYY-MM`, month 01–12, years 2000–2099 | `2026-04` | `2026-13`, `0000-01`, `2026-4` |
| Amount | plain decimal, ≤ 12 integer digits, ≤ 2 decimals, ASCII digits only, parsed from text (never float) | `1234.5`, `0.01` | `1,250.00`, `12.345`, `1e3`, `NaN`, `-5` (sign only allowed in payout register/adjustments), `٣` |
| Percent | `0`–`100` with ≤ 2 decimals → basis points (60 → 6000) | `60`, `33.33` | `60.005`, `100.01` |
| Currency | `SGD` or `USD` (2 decimals). No FX anywhere | `SGD` | `EUR`, `sgd` |

**Key scope (collision safety).** One workbench = one synthetic company. Every ID is unique within
its own entity type (`event_id` among cash events, `contract_id` among contracts, …). Currency is an
attribute, never part of a key: a contract has exactly one currency and all its cash must match.
Composite keys (e.g. split = contract + rep; variance case = period + rep + currency) are stored as
separate columns with UNIQUE constraints and hashed as canonical JSON arrays, never concatenated.

## Files (import in this order)

### 1. `reps` — rep roster
| Column | Type | Rule |
|---|---|---|
| rep_id | ID | natural key |
| display_name | text ≤ 80 | synthetic names only |
| team | text ≤ 60 | |

### 2. `plans` — commission plan versions (immutable)
| Column | Type | Rule |
|---|---|---|
| plan_id | ID | |
| version | int 1–9999 | key = (plan_id, version, currency); version order must match date order (`PLAN_VERSION_ORDER`) |
| currency | currency | one row per currency |
| effective_from | date | 1st of a month (`EFFECTIVE_DATE_NOT_MONTH_START`); not in/before a closed month (`RETROACTIVE_PLAN`); unique per plan+currency (`PLAN_DATE_CONFLICT`) |
| threshold_amount | amount ≥ 0 | per rep per month |
| base_rate_pct | percent | rate up to the threshold |
| accelerator_rate_pct | percent | rate above the threshold |
| description | text ≤ 200 | optional |

### 3. `assignments` — which plan a rep is on
| Column | Type | Rule |
|---|---|---|
| assignment_id | ID | natural key |
| rep_id | ID | must exist (`UNKNOWN_REP`) |
| plan_id | ID | must have versions (`UNKNOWN_PLAN`) |
| effective_from | date | 1st of month; not in/before a closed month (`RETROACTIVE_ASSIGNMENT`); same rep + same start = `ASSIGNMENT_AMBIGUOUS` |
| effective_to | date or blank | blank = open-ended, else a month-end ≥ start (`EFFECTIVE_TO_INVALID`) |

Resolution: on a cash date the assignment with the latest start that covers the date wins.

### 4. `contracts` — bookings
| Column | Type | Rule |
|---|---|---|
| contract_id | ID | natural key |
| account_id | ID | one account id = one account name (`ACCOUNT_NAME_CONFLICT`) |
| account_name | text ≤ 120 | synthetic |
| currency | currency | all cash on the contract must match |
| booking_date | date | booking month drives the *Bookings signed* KPI only |
| booked_amount | amount > 0 | informational — not a commission basis |
| product | text ≤ 80 | |

### 5. `splits` — credit split per contract (immutable once loaded)
| Column | Type | Rule |
|---|---|---|
| contract_id | ID | must exist (`UNKNOWN_CONTRACT`) |
| rep_id | ID | must exist (`UNKNOWN_REP`) |
| split_pct | percent > 0 | all rows of a contract must total exactly 100.00 (`SPLIT_TOTAL_NOT_100`); if any row of the contract is invalid, the whole group is held (`SPLIT_GROUP_INCOMPLETE`); a contract that already has a split cannot get another (`CONFLICT_EXISTING`) |

### 6. `cash_events` — collections and refunds (the commission basis)
| Column | Type | Rule |
|---|---|---|
| event_id | ID | natural key |
| event_type | `COLLECTION` / `REFUND` | `EVENT_TYPE` |
| contract_id | ID | must exist (`UNKNOWN_CONTRACT`) |
| event_date | date | business (value) date; must not be after the workbench business date (`FUTURE_DATED`) |
| currency | currency | must equal the contract currency (`CURRENCY_MISMATCH`) |
| amount | amount > 0 | sign comes from the type (`AMOUNT_NOT_POSITIVE`, `AMOUNT_FORMAT`) |
| original_event_id | ID / blank | REFUND: required (`MISSING_ORIGINAL`), must be a COLLECTION (`UNKNOWN_ORIGINAL_EVENT`, `ORIGINAL_NOT_COLLECTION`), same contract (`REFUND_CONTRACT_MISMATCH`), not dated before it (`REFUND_BEFORE_COLLECTION`), cumulative refunds ≤ collected (`OVER_REFUND`). COLLECTION: must be blank (`UNEXPECTED_ORIGINAL`) |
| invoice_ref | text ≤ 60 | informational only |
| memo | text ≤ 200 | free text; CSV exports neutralise formula prefixes |

### 7. `recorded_payouts` — synthetic payout register (what "payroll" recorded)
| Column | Type | Rule |
|---|---|---|
| record_id | ID | natural key |
| rep_id | ID | must exist |
| period | period | not a future month (`FUTURE_DATED`) |
| currency | currency | |
| amount | signed amount ≠ 0 | may be negative (a recovery) |
| source_system | text ≤ 40 | e.g. `PAYROLL-SIM` |
| memo | text ≤ 200 | |

## Row outcomes and control totals

| Outcome | Meaning |
|---|---|
| `ACCEPTED` | loaded, with batch id + source row + row hash |
| `DUPLICATE_ROW` | identical to an existing row (or an earlier identical row in the same file) — skipped, never double counted |
| `QUARANTINED` | not loaded; stored with reason codes; `CONFLICT_EXISTING` = same key, different values (existing row untouched); `CONFLICT_IN_FILE` = the same key twice in one file with different values (all copies held) |

File outcomes: `COMMITTED`, `COMMITTED_WITH_QUARANTINE` (quarantine mode), `REJECTED` (header
problems, or any invalid row in **strict** mode — nothing committed), `DUPLICATE_FILE` (same SHA-256
as an already committed file, any filename — nothing imported).

Every batch records: rows read = accepted + duplicate + quarantined, and per currency (and event
type) read amount = accepted + duplicate + quarantined amount.

## Calculation outputs (stored per run; frozen at close)

| Field | Meaning |
|---|---|
| line_key | `TYPE:EVENT_ID:REP_ID` (or `ADJ:<id>`); stable across reruns |
| line_type | `EARNING`, `CLAWBACK`, `LATE_EARNING`, `LATE_CLAWBACK`, `MANUAL_ADJUSTMENT` |
| period / original_period | posting month / month whose ladder and plan version applied |
| credited_minor / credit_reversed_minor | cash credited to (or reversed from) the rep |
| base_portion_minor / accel_portion_minor | credited cash below / above the threshold |
| base_rate_bps / accel_rate_bps, plan_id, plan_version | frozen rule |
| exact_amount / amount_minor | exact value (minor units) and the single rounded result |
| detail | attainment before/after, split, provenance, refund cumulative figures |

Holds (excluded from totals, **blocking** close): `NO_SPLIT`, `NO_ASSIGNMENT`, `AMBIGUOUS_ASSIGNMENT`,
`NO_PLAN_VERSION`, `ORIGINAL_ON_HOLD`, `ORIGINAL_PENDING`, `ORIGINAL_EXCLUDED`,
`LATE_EVENT_PENDING`, `QUARANTINED_CASH_ROW`.

## Metrics (never mixed)
Bookings signed · Cash collected · Refunds · Net cash · Credited to reps · Commission earned ·
Clawbacks · Late adjustments · Manual adjustments · Expected payouts · Recorded payouts · Variance
(= recorded − expected). None of them is recognised revenue.
