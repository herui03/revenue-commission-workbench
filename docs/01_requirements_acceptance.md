# 01 · Scope, requirements and acceptance criteria

> Written **before** the calculation engine (commit f59e777). Later clarifications are marked *(updated)*. The hand-computed expectations it
> references (`tests/expected/hand_calculations.json`) were committed in the
> same first commit, so the git history shows the answers existed before the code.

## 1. Problem statement

A Revenue / Sales Operations analyst must be able to answer, for any month:

1. **What cash was collected?** (not what was booked or invoiced)
2. **What part of it is commission-eligible, and for which rep?** (credit splits, plan assignment)
3. **What is owed per rep and why?** (rate version, tier bracket, split, rounding — per line)
4. **Why did the number change?** (refund clawbacks, late data, manual adjustments, plan changes)
5. **How does period close stop history being rewritten?** (frozen snapshots, adjustments instead of restatement)
6. **Why does payroll's recorded payout differ from the expected payout?** (variance cases with evidence)

This repository is a **synthetic, independent portfolio prototype**. It is not accounting or
revenue-recognition software, not connected to any payroll/bank/CRM, and every entity is invented.

## 2. Metrics — labelled precisely, never mixed

| Metric | Meaning here | Commissionable in this demo? |
|---|---|---|
| Booking (booked contract value) | Value of a signed contract, dated by `booking_date` | No |
| Invoice | Billing document. Not imported; `invoice_ref` on cash rows is informational only | No |
| Cash collected | `COLLECTION` event: money received, dated by `event_date` | **Yes, basis for commission** |
| Refund | `REFUND` event returning part/all of one specific earlier collection | Reverses (claws back) the original earning |
| Credited collections | Cash allocated to reps by the contract's credit split | Tier basis |
| Commission earned / clawback / adjustment | Calculation output lines | — |
| Expected payout | Net of all lines for rep × period × currency | — |
| Recorded payout | What a (synthetic) payout register says was recorded | Compared, never edited |

Currencies (SGD, USD) are always reported separately. No FX conversion exists anywhere.

## 3. The invented demo policy (one choice, not an industry or legal rule)

* Cash-based: commission is earned when a `COLLECTION` is received.
* Credit split per contract (basis points, must total exactly 100.00%).
* Marginal monthly accelerator, per **rep × calendar month × currency**:
  credited positive collections up to 10,000.00 earn the base rate (5%); the excess earns the
  accelerator rate (8% in plan v1, 9% in demo plan v2 effective 2026-05-01).
* Attainment counts **gross positive credited collections** only. Refunds never restore tier capacity.
* A refund reverses the **stored original earning** proportionally (never recomputed at today's rate).
* Plan versions are immutable, month-aligned and chosen by the cash event date.
* Full detail and alternatives considered: `docs/03_policy_decision_log.md`.

## 4. Functional requirements and acceptance criteria

IDs are referenced from tests and from the UAT matrix (`docs/05_uat_matrix.md`).

### Imports and data quality
| ID | Requirement | Acceptance criterion |
|---|---|---|
| IMP-01 | Import 7 documented CSV kinds (reps, contracts, splits, plans, assignments, cash events, recorded payouts) | Demo fixtures import with no network; data dictionary documents every column |
| IMP-02 | Provenance | Every accepted row stores batch id, file SHA-256 and source row number |
| IMP-03 | File idempotency by content hash | Same bytes under another filename → no-op `DUPLICATE_FILE`, no double credit |
| IMP-04 | Row idempotency / conflicts | Identical existing row → `DUPLICATE_ROW` skip; same id, different content → quarantined, existing row untouched |
| IMP-05 | Strict (atomic) mode | Any invalid row → nothing committed; full error list returned |
| IMP-06 | Quarantine mode + control totals | rows read = accepted + duplicate + quarantined, and amount totals reconcile per currency |
| IMP-07 | Validation | header (missing/duplicate/unknown), ISO dates within 2000-2099, amount format (no float, ≤2 dp, no separators, ASCII digits), IDs, currency, orphans, split totals, currency mismatch, refund before collection, over-refund, ambiguous assignments (same rep + same start date), future-dated rows, retroactive plan change into a closed period |
| IMP-08 | Collision-safe keys | IDs restricted to `[A-Z0-9_-]{1,40}`; composite keys hashed as canonical JSON arrays, never by string concatenation |

### Calculation
| ID | Requirement | Acceptance criterion |
|---|---|---|
| CAL-01 | Integer minor units, no float | Engine & importer contain no `float` arithmetic on money (test greps source) |
| CAL-02 | Split conservation | Credited cents per collection sum exactly to the source cents (largest remainder, ties by rep id) |
| CAL-03 | Marginal accelerator | Hand-computed HC-01, HC-04, HC-11, HC-14 match |
| CAL-04 | Refund reversal against original | HC-02: −80.00 even after plan v2 (9%) exists |
| CAL-05 | No rounding drift across partial refunds | HC-08: 10¢ commission, 3 partial refunds → exactly −10¢ total |
| CAL-06 | Refund does not restore capacity | HC-10 |
| CAL-07 | Order independence | Shuffled files / permuted import order → identical result digest |
| CAL-08 | Explainable lines | Every line exposes plan id+version, rates, bracket portions, split, exact value and rounding |
| CAL-09 | Eligibility holds | Missing split / missing or ambiguous assignment / missing plan version → blocking hold, excluded from totals, shown with amount |

### Period workflow
| ID | Requirement | Acceptance criterion |
|---|---|---|
| PER-01 | Draft → review → close | Invalid transitions rejected with a reason |
| PER-02 | Immutable closed snapshot | Snapshot JSON (inputs, plan versions, lines, controls) + SHA-256; DB triggers block edits |
| PER-03 | Reproducible export | Closed-period CSV/HTML export byte-identical after later imports, plan changes, adjustments and case resolution |
| PER-04 | Stale review detection | Close recomputes; if the digest over lines, holds, exclusions, totals, controls, KPIs and inputs ≠ the reviewed run → rejected (`STALE_REVIEW`); the snapshot is built only from the stored reviewed run *(updated after reviewer finding R-2)* |
| PER-05 | Blocking holds | Close refused while blocking holds exist (eligibility, pending late events, material quarantined cash rows) |
| PER-06 | Late data after close | Late event → blocking `LATE_EVENT_PENDING` hold in first open period; reviewer posts it as a linked prior-period adjustment (reason required) or excludes it; closed statement untouched |
| PER-07 | No duplicate close / payables | Second close refused; payables unique per period×rep×currency |
| PER-08 | Manual adjustments | Reason required; open periods only; shown separately from source earnings |
| PER-09 | Chronological close | Cannot close a month while an earlier month with activity is open; cannot close a month that has not ended |

### Variance & audit
| ID | Requirement | Acceptance criterion |
|---|---|---|
| VAR-01 | Expected vs recorded by rep × period × currency | Variance = recorded − expected, basis labelled CLOSED or DRAFT |
| VAR-02 | Cases keep identity | Case id, owner, notes survive refresh; changed variance after resolution reopens the case with a system note |
| VAR-03 | Resolved ≠ corrected/paid | UI and exports say so; resolving never changes payables |
| VAR-04 | Evidence drill-down | Statement → line → cash event → contract → split → plan version → computation |
| VAR-05 | Read-only history | All GET pages, as-of views and exports leave the DB byte-identical |
| AUD-01 | Append-only application audit | Every mutation logged in the same transaction; UPDATE/DELETE blocked by triggers; hash chain verifiable. Honest limit: a DB owner can still rewrite the file |
| SEC-01 | Output safety | CSV formula-injection protection (text cells), HTML autoescape, localhost bind, CSRF token on POST |

### Product / demo
| ID | Requirement | Acceptance criterion |
|---|---|---|
| DEM-01 | One command from a clean clone | `python3 -m rcw demo` (stdlib only, offline) plays the full story and writes exports |
| DEM-02 | Web UI 3-step flow | Choose period → calculate & review → inspect / close / export, with empty/error states |
| DEM-03 | Story coverage | split sale, threshold crossing, partial refund, payout mismatch, late event after close |
| DEM-04 | Offline replay | Self-contained HTML, labelled DEMO REPLAY, built only from captured run artifacts |
| BEN-01 | Benchmark | Seeded 10k cash events; report correctness controls, runtime, environment, denominators |

## 5. Explicitly out of scope
Real authentication/authorization, segregation of duties enforcement, payment execution,
revenue recognition (ASC 606 / IFRS 15), FX, taxes, draws/guarantees, quota-based plans,
CRM/payroll integrations, multi-tenant hosting.

## 6. Build plan (order of work)
1. This document + hand calculations (committed first).
2. Money/rounding primitives → importer + validation → pure engine → period workflow → variance → exports → CLI demo.
3. Tests: hand-computed scenarios, validation, workflow, exports, oracle cross-check, web.
4. Flask UI, browser E2E + screenshots, layout fixes.
5. Benchmark, docs (EN) + learning/interview guide (中文), offline replay, CI, ZIP.
