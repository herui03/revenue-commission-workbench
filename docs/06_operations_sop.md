# 06 · Operations SOP (monthly commission close — demo procedure)

A concise standard operating procedure for the workbench. Roles are **labels** (analyst-1 =
preparer, manager-1 = reviewer, ops-1 = payroll liaison); the tool checks that preparer and reviewer
labels differ but does not authenticate anyone.

## Monthly timetable (illustrative)
| When | Who | Step | Tool action | Done when |
|---|---|---|---|---|
| Business day 1–2 | Preparer | Load source files in order: reps → plans → assignments → contracts → splits → cash → payout register | Imports page (quarantine mode) or `python -m rcw import …` | Every batch reconciles (rows and amounts) |
| BD 2 | Preparer | Work the quarantine list: fix at source and re-import, or dismiss with a reason | Quarantine page | No open quarantined **cash** row for the month |
| BD 2–3 | Preparer | Calculate the month; clear every blocking hold (import missing splits/assignments; decide late events) | Period page → Calculate | 0 blocking holds; all control checks PASS |
| BD 3 | Preparer | Spot-check statements: split sales, threshold crossings, clawbacks, adjustments | Statement → evidence | Each checked line explained |
| BD 3 | Preparer | Submit for review | Submit | Status *In review* |
| BD 4 | Reviewer (different label) | Review statements, controls, exclusions, manual adjustments; close | Close | Snapshot SHA-256 shown, payables recorded |
| BD 4 | Reviewer | Export statements and control report; file them with the snapshot hash | Export buttons | Files stored |
| BD 5+ | Payroll liaison | Import the payout register; refresh variance; investigate each case; resolve with reason code + note | Variance / case pages | Every case RESOLVED or CLEARED; corrections posted as adjustments in the open month |

## Rules that are never bypassed
1. **Never edit a closed month.** Late cash/refunds become holds in the first open month; post them
   (prior-period adjustment) or exclude them — always with a reason.
2. **Never overwrite a source row.** A changed value arrives as a new row or file; conflicts are
   quarantined for a person to decide.
3. **Manual adjustments** need a reason and are reversed only by an opposite adjustment.
4. **Resolved ≠ paid.** Resolving a variance case records the explanation. Money moves only through
   a reasoned adjustment and, outside this tool, payroll.
5. **Stale review = re-review.** If anything is imported, decided or adjusted after submission,
   close is refused (`STALE_REVIEW`); recalculate and resubmit.

## Common errors and what to do
| Message code | Meaning | Action |
|---|---|---|
| `BLOCKING_HOLDS` | Holds exist for the month | Open the holds panel; resolve each one |
| `STALE_REVIEW` / `STALE_RUN` | Inputs changed after review / after the last calculation | Recalculate, re-check, resubmit |
| `SAME_ACTOR` | Preparer label tried to close | Switch *Acting as* to the reviewer label |
| `EARLIER_PERIOD_OPEN` | An earlier month with activity is still open | Close months in order |
| `PERIOD_NOT_ENDED` | Month end is not before the business date | Wait, or (demo only) load the next demo stage |
| `DUPLICATE_FILE` | These exact bytes were already imported | Nothing to do — no double count |
| `CONFLICT_EXISTING` | Same id, different values | Confirm which is right at source; corrections come as new events/adjustments |
| `LATE_EVENT_PENDING` | Cash dated in a closed month | Post as prior-period adjustment (reason) or exclude (reason) |

## Local operation
* Start: double-click `launch_demo.command` (Mac) or `python3 -m rcw --db instance/demo_workbench.db serve --seed-if-missing`.
  The server listens on 127.0.0.1 only. Your database is kept between runs.
* Reset (destructive, explicit): `reset_demo.command` (type RESET) or
  `python3 -m rcw --db instance/demo_workbench.db reset-demo --yes` — a timestamped backup is written to `instance/backups/` first.
* Integrity checks: `python3 -m rcw --db instance/demo_workbench.db status` (audit hash chain); closed
  snapshots are re-hashed on every export.
* Backups: copy `instance/*.db` while the server is stopped. There is no replication or multi-user support.

## Known limitations (tell stakeholders)
Labels are not logins; the audit log is append-only in the app but a database owner can rewrite the
file; no payment is executed; no FX; negative payouts are recorded, not carried forward; split changes
after cash need manual adjustments; single-user local SQLite.
