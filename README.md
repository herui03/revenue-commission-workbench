# Revenue & Commission Operations Workbench

**Synthetic, independent portfolio prototype** · built with AI assistance (Claude Code) · Python + SQLite + Flask ·
not accounting, revenue-recognition, payroll or employer software · all data invented.

**What it answers.** Every month a Revenue/Sales Operations analyst must explain:

| Question | Where the workbench answers it |
|---|---|
| What **cash** was collected? (not booked, not invoiced) | Period page — bookings, cash, refunds shown separately, per currency |
| What is **commission-eligible**, for which rep? | Credit splits (cent-exact), plan assignments, blocking holds |
| What is **owed per rep** — and why? | Rep statement → every line → cash event → contract → split → plan rule → calculation |
| **Why did the number change?** | Refund clawbacks at the original rate, late data posted as reasoned adjustments, manual adjustments shown separately |
| How does **close stop history being rewritten**? | Close freezes the exact reviewed run (SHA-256 snapshot); stale reviews are refused; closed exports are unchanged by any later application operation (not protected from tampering by whoever owns the database file) |
| Why does **payroll's recorded payout differ**? | Variance cases with owner, reason code, notes and suggested causes — *resolved ≠ paid* |

## Try it in 60 seconds

```bash
# Until the pull request is merged, the code lives on this branch (private repo: you need access):
git clone --branch claude/vigilant-curie-tsmtx0 https://github.com/herui03/revenue-commission-workbench.git
cd revenue-commission-workbench
#   (after the PR is merged into main, a plain `git clone https://github.com/herui03/revenue-commission-workbench.git` works)
#   (or: unzip the source ZIP and `cd revenue-commission-workbench`)

# 1) No install needed (Python 3.10+ stdlib): play the whole story, write exports to out/demo/
python3 -m rcw demo

# 2) Web UI (needs Flask once):
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m rcw --db instance/demo_workbench.db serve --seed-if-missing
#    → open http://127.0.0.1:5057  (bound to localhost only)
```

**Mac:** after the one-time install above, double-click **`launch_demo.command`**. It keeps your
existing demo database and seeds one only if it is missing; it never installs packages. To start over,
double-click **`reset_demo.command`** and type `RESET` (a backup is kept in `instance/backups/`).
If macOS says the file "cannot be opened because it is from an unidentified developer" (normal for
downloaded scripts), right-click it → **Open** → **Open** once; or use the terminal command above.
**No server possible?** Open `docs/presentation/walkthrough.html` — an offline **demo replay** built from
recorded run artifacts (clearly labelled; not live results).

## The demo story (guided in the UI, ~3 minutes)

1. **April** — a 60/40 **split sale** (USD 12,345.67 → 7,407.40 / 4,938.27), a **threshold crossing**
   (2,592.60 at 5% + 2,407.40 at 8%), three rows quarantined on purpose. Preparer submits; a different
   reviewer label closes.
2. **June data arrives** — plan v2 (accelerator 9% from May), a **partial refund** of an April sale
   (−80.00, reversed at the original 8%), and a receipt dated 29 April that arrived **after April closed**:
   May is blocked until a reviewer posts it as a prior-period adjustment with a reason. April's export
   stays byte-identical.
3. **Variance** — the synthetic payout register differs three times (+80.00 missed clawback, −72.22
   accelerator not paid, −120.00 late adjustment missing); cases suggest the cause; resolving explains,
   it does not pay.

| April closed (frozen snapshot, controls, charts) | Late April receipt blocks May | Evidence chain for one line |
|---|---|---|
| ![April closed](docs/evidence/screenshots/03_april_closed.png) | ![Late hold](docs/evidence/screenshots/06_may_late_hold_blocks_close.png) | ![Evidence](docs/evidence/screenshots/05_line_evidence_threshold_crossing.png) |

## The invented demo policy (one choice, not a rule)
Cash-based · credit split by contract (largest-remainder cents) · marginal monthly accelerator per
rep × month × currency (≤ 10,000 at 5%, excess at 8%) · attainment = gross credited collections ·
refunds reverse the **stored** original earning cumulatively — `half_up(E × refunded ÷ collected)` — so
partial refunds never drift and never restore tier capacity · integer minor units, one half-up rounding
per line · SGD and USD never combined. Every decision and its alternatives: `docs/03_policy_decision_log.md`.

## Evidence (developer self-tests — see the UAT matrix for what was *not* tested)
* **Hand-computed first:** `tests/expected/` was committed before any engine code (see git history).
* **Automated tests:** 88 tests. With `pip install -r requirements-dev.txt`, `python -m pytest` runs all 88 (plus
  301 subtests) and they pass. Without any install, `python3 -m unittest discover -s tests -t .` collects the same
  88 but **skips the 7 Flask web tests**, so 81 run and pass. Coverage: scenarios HC-01…16, validation, stale close,
  late data, immutability, exports, variance, an independent oracle on seeded random data, order independence.
* **Browser E2E:** Playwright drives the full story, checks console errors and horizontal overflow at 1440 px and 390 px (`docs/evidence/e2e_results.json`, screenshots).
* **Benchmark:** 10,000 seeded cash events, oracle match on every rep-month-currency total, runtime and
  environment in `docs/08_benchmark.md` (arithmetic consistency on synthetic data — not real-world validity).
* **CI:** GitHub Actions, read-only token, Python 3.10/3.11/3.12 stdlib jobs + web/browser job.
* **Defects actually found and fixed** (by the builder's own tests and by an independent AI reviewer): `docs/04_defects_log.md`.

## Docs
| | |
|---|---|
| `docs/01_requirements_acceptance.md` | scope, requirements, acceptance criteria |
| `docs/02_data_dictionary.md` | every CSV column, rule and reason code |
| `docs/03_policy_decision_log.md` | policy choices, alternatives, trade-offs |
| `docs/04_defects_log.md` | real defects, root causes, regression tests |
| `docs/05_uat_matrix.md` | evidence per requirement; external UAT **not performed** |
| `docs/06_operations_sop.md` | monthly close procedure, error codes, reset/backup |
| `docs/07_demo_script.md` | 3-minute live script |
| `docs/08_benchmark.md` | generated benchmark report |
| `docs/09_cv_bullets.md` | honest CV bullet templates |
| `docs/zh/学习指南.md` · `docs/zh/面试指南.md` | 中文学习指南 · 15 道面试题与回答 |

## Layout
`rcw/` domain (stdlib only): `money.py` (integer money, rounding, allocation) · `importer.py` (validation,
idempotency, quarantine) · `engine.py` (pure calculation) · `services.py` (calculate → review → close,
decisions, adjustments) · `variance.py` · `exports.py` · `cli.py` — and `rcw/web/` (Flask UI, same services).
`data/demo/` synthetic fixtures · `tests/` · `scripts/` (E2E, benchmark, evidence, replay) · `docs/`.

## Honest limitations
Actor names are **labels**, not logins (segregation of duties is illustrated, not enforced). The audit log
is append-only and hash-chained inside the app but **not tamper-proof** against whoever owns the SQLite
file. No payment is executed. No FX, no revenue recognition, no draws/carry-forward, split changes after
cash need manual adjustments, single-user local SQLite, years 2000–2099 only. Business UAT with real users
has not been performed.

## AI assistance and who did what
* **Direction & requirements:** Herui (repository owner) — defined the brief, scope and acceptance expectations.
* **Implementation, tests, docs, evidence:** Claude Code (Anthropic AI).
* **Independent review:** Codex, an independent AI reviewer acting for Herui, ran static reviews and executed checks on
  checkpoints and reported defects R-1…R-5 (all fixed with regression tests — `docs/04_defects_log.md`). This was an
  AI code review, not a human or business review.
* **Candidate learning still required:** Herui is learning this codebase. Before presenting any part as personal work,
  reproduce it by hand — the checklist is in `docs/zh/学习指南.md` §11.
