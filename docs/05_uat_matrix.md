# 05 · UAT matrix — evidence-linked

Three different kinds of evidence, never mixed up:

* **Dev self-test**: automated tests and scripted runs written by the builder (Claude Code, an AI)
  and executed in this repository. Links point to the test or artifact.
* **Independent AI technical review**: Codex, an independent AI reviewer acting for Herui, ran static
  reviews and executed checks on checkpoints (e.g. commit `fb754e0`) and reported findings R-1 … R-5,
  all fixed with regression tests (see `04_defects_log.md`). This is an AI code review — not a human
  review, not business acceptance, and not evidence that Herui has personally reproduced the checks.
* **External business UAT**: acceptance by real Sales/RevOps/Finance users on real processes —
  **not performed**. This is a synthetic portfolio prototype; no such users or data were involved.

Evidence files: `docs/evidence/test_results.txt` (pytest -v), `unittest_stdlib_only.txt`,
`demo_story_output.txt`, `demo_exports/`, `e2e_results.json` + `screenshots/`, `benchmark.json`,
`ci_run.json`.

| Req | Acceptance criterion (from 01) | Dev self-test evidence | Result | External UAT |
|---|---|---|---|---|
| IMP-01 | 7 CSV kinds, demo fixtures offline | `data/demo/`, `test_variance.py::DemoStoryTests`, CI "Scripted demo story" | Pass | Not performed |
| IMP-02 | Provenance (batch, SHA-256, row) | `test_import.py::SameDayRefundTests` (source_row asserted), evidence page screenshot `05_line_evidence…` | Pass | Not performed |
| IMP-03 | Same bytes, other filename → no-op | `test_import.py::IdempotencyTests::test_same_bytes_other_filename_is_noop`, `test_web.py::…test_upload_duplicate…` | Pass | Not performed |
| IMP-04 | Duplicate row skipped / conflict quarantined, never overwritten | `test_reordered_file_rows_are_duplicates`, `test_conflicting_existing_row_is_quarantined_not_overwritten`, `test_conflict_inside_one_file_quarantines_all_copies` | Pass | Not performed |
| IMP-05 | Strict mode atomic | `test_strict_mode_is_atomic`, `SameDayRefundTests` (strict) | Pass | Not performed |
| IMP-06 | Quarantine + reconciling control totals | `test_control_totals_reconcile`, `FuzzTests` (150 random files), screenshot `12_import_batch_control_totals` | Pass | Not performed |
| IMP-07 | Validation rules | `ValidationCaseTests` (17 hand-listed cash cases + 6 refund cases), `ReferenceDataTests`, `CalendarRangeTests` | Pass | Not performed |
| IMP-08 | Collision-safe keys | `test_ids_are_case_sensitive_by_rejection`, V-16b (`X1:Y` rejected), `engine.line_key` guard, variance UNIQUE columns | Pass | Not performed |
| CAL-01 | No float in money path | `test_money.py::NoFloatInMoneyPathTests`, D-001 fix | Pass | Not performed |
| CAL-02 | Split conservation | HC-05/06/07/14/16, `AllocationTests` (3,000 random cases), benchmark credit_conserved | Pass | Not performed |
| CAL-03 | Marginal accelerator | HC-01, HC-04, HC-11, HC-12, HC-14 | Pass | Not performed |
| CAL-04 | Refund reverses original after plan change | HC-02 (−80.00, not −90.00), E2E "partial refund claws back −80.00" | Pass | Not performed |
| CAL-05 | No drift across partial refunds | HC-08 (10¢ in three parts → 10¢), HC-09, HC-15, `test_full_refund_always_exact_random_partitions` (2,000 cases) | Pass | Not performed |
| CAL-06 | Refund does not restore capacity | HC-10 | Pass | Not performed |
| CAL-07 | Order independence | `OrderIndependence` (shuffled, mixed and split files), benchmark shuffled re-import | Pass | Not performed |
| CAL-08 | Explainable lines | statement + evidence pages, screenshots `04`, `05`, `08` | Pass | Not performed |
| CAL-09 | Eligibility holds | `HoldTests` (NO_SPLIT, ORIGINAL_ON_HOLD, NO_ASSIGNMENT, NO_PLAN_VERSION) | Pass | Not performed |
| PER-01 | Draft → review → close; invalid transitions | `TransitionTests::test_invalid_transitions` | Pass | Not performed |
| PER-02 | Immutable snapshot | `ImmutabilityTests` (11 blocked SQL edits), snapshot integrity verify | Pass | Not performed |
| PER-03 | Reproducible closed export | `FrozenExportTests` (after plan, import, late post, adjustment, case resolution, new payout rows), HC-02/HC-13, demo + E2E byte-identical checks | Pass | Not performed |
| PER-04 | Stale review rejected | `StaleReviewTests` (cash, booking-only R-2, adjustment, plan version) | Pass | Not performed |
| PER-05 | Blocking holds stop close | `HoldTests`, HC-13, E2E "submit is disabled while a blocking hold exists" | Pass | Not performed |
| PER-06 | Late data after close | HC-13, `LateDataTests` (late refund, first-open posting, excluded original) | Pass | Not performed |
| PER-07 | No duplicate close / payables | `test_invalid_transitions` (ALREADY_CLOSED, payables unchanged), PRIMARY KEY on payables | Pass | Not performed |
| PER-08 | Manual adjustments | `AdjustmentTests` | Pass | Not performed |
| PER-09 | Chronological close; month ended | `test_period_not_ended_and_chronological_order` | Pass | Not performed |
| VAR-01 | Expected vs recorded with basis | `DemoStoryTests::test_variances_match_hand_values` | Pass | Not performed |
| VAR-02 | Cases keep identity, reopen on change | `CaseLifecycleTests::test_ids_notes_preserved_and_reopen_on_change`, R-3 regression | Pass | Not performed |
| VAR-03 | Resolved ≠ corrected/paid | case page text, `test_ids_notes…` (no payables created), screenshot `10_case_resolved` | Pass | Not performed |
| VAR-04 | Drill-down evidence | `test_full_story_over_http` (evidence page asserts), screenshots `05`, `08` | Pass | Not performed |
| VAR-05 | GET/as-of views read-only | `test_get_pages_are_read_only` (crawl, DB fingerprint), `test_historical_as_of_views_are_read_only` | Pass | Not performed |
| AUD-01 | Append-only audit, honest limits | `ImmutabilityTests` (chain detects edit after trigger drop), `test_every_mutation_is_audited` | Pass (with stated limits) | Not performed |
| SEC-01 | CSV/HTML safety, localhost, CSRF | `CsvSafetyTests`, `test_statement_csv_structure_and_escaping`, `test_csrf_and_trusted_host`, `test_every_post_form_carries_a_nonempty_csrf_token` | Pass | Not performed (no security audit) |
| DEM-01 | One command, clean clone | CI stdlib jobs (3.10/3.11/3.12) run `python -m rcw demo` from a fresh checkout | Pass | Not performed |
| DEM-02 | 3-step web flow, empty/error states | E2E 19 checks, `test_empty_state_and_security_headers` | Pass | Not performed |
| DEM-03 | Story coverage | E2E + demo story output | Pass | Not performed |
| DEM-04 | Offline replay from real artifacts | `docs/presentation/walkthrough.html` built by `scripts/build_presentation.py` from `docs/evidence/` | Pass | Not performed |
| BEN-01 | 10k benchmark with controls | `docs/08_benchmark.md` (generated), `benchmark.json` | Pass | Not performed |

**Not tested at all:** real users, real data volumes/shapes, concurrent multi-user access, browsers
other than headless Chromium, accessibility with assistive technology (only semantic HTML, labels
and colour-plus-icon status were applied), Windows launcher.
