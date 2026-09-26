# 07 · Demo script (3 minutes, live) — with an offline fallback

**Before the call:** double-click `launch_demo.command` (or run
`python3 -m rcw --db instance/demo_workbench.db serve --seed-if-missing`) and open
http://127.0.0.1:5057. If the database already has a played story you want to repeat, use
`reset_demo.command` first (type RESET). **If nothing starts**, open
`docs/presentation/walkthrough.html` — it is a clearly labelled *demo replay* of a recorded run.

| Time | Click | Say |
|---|---|---|
| 0:00 | Overview | “A RevOps analyst must say what cash came in, what is eligible, what each rep is owed, why a number changed, and why payroll differs. Bookings, invoices, cash and commission are different numbers; this demo pays on cash collected under one invented policy.” |
| 0:15 | *Load demo data (stage 1)* → Imports → latest batch | “Files are identified by content hash. Three rows are quarantined on purpose, a duplicate row is skipped, and read = accepted + duplicate + quarantined, in rows and in money.” |
| 0:40 | Overview → *Close April 2026* → **Calculate draft** | “Five control checks pass: credited cents equal collected cents, refunds conserve, brackets add up.” |
| 0:55 | Rep table → Cedar → *Statement* → *evidence* on the 5,000.00 line | “60/40 split of 12,345.67: 7,407.40 and 4,938.27 — the odd cent goes to the larger remainder. Then Cedar crosses 10,000: 2,592.60 at 5%, 2,407.40 at 8%, one half-up rounding. Every line traces back to a file and row.” |
| 1:20 | Back → **Submit** → try **Close** as analyst-1 → switch *Acting as* to manager-1 → **Close** | “Same label can't prepare and close. Close re-computes and freezes exactly what was reviewed — if anything changed, it refuses as stale.” |
| 1:40 | Overview → *Load late-arriving data (stage 2)* | “June: plan v2 raises the accelerator to 9%, and a receipt dated 29 April arrives after April was closed.” |
| 1:50 | May → Calculate → holds panel | “April is not reopened. The late receipt blocks May until someone decides.” Type a reason → **Post as prior-period adjustment** → Recalculate |
| 2:10 | Aurora statement → clawback evidence | “A 1,000 refund of April's 2,000 sale reverses the stored 8% earning: −80.00, not −90.00 at today's rate. Refunds don't restore tier capacity, and partial refunds reverse cumulatively so there's no rounding drift.” |
| 2:25 | Submit (analyst-1) → Close (manager-1) → Variance → **Refresh** → VC-0002 | “Payroll recorded 590.00; expected 510.00. The tool matches +80.00 to the missed clawback — a suggestion. I resolve it with a reason code: resolved means explained, not paid.” |
| 2:45 | April → Export statement; mention evidence | “April's export is byte-identical after all of that. 88 automated tests, a browser run, and a 10,000-event benchmark against an independent reference model back the arithmetic — on synthetic data, so it proves consistency, not real-world validity.” |

**Likely follow-ups:** see `docs/zh/面试指南.md` (15 questions with answers).
