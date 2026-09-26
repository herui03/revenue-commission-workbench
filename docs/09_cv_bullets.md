# 09 · Honest CV bullet templates

Rules: say *synthetic*, *prototype* and *AI-assisted*; claim only what the repository shows; no
invented users, savings, accuracy percentages or business impact. Pick 2–3 and adapt.

**Portfolio project line**
> Revenue & Commission Operations Workbench — independent portfolio prototype (synthetic data), built
> with AI assistance (Claude Code). Python, SQLite, Flask. github.com/herui03/revenue-commission-workbench

**Bullets (choose what you can explain in an interview)**
- Designed a cash-based commission workflow (import → calculate → review → close → variance) that
  keeps bookings, invoices, cash collected and commission as separately labelled metrics.
- Specified an invented demo policy — credit splits, marginal monthly accelerator, refund clawbacks
  against the original earning — and hand-computed 16 expected scenarios before implementation;
  the engine reproduces all of them.
- Implemented money in integer minor units with deterministic rounding and cent-exact split
  allocation; controls prove credited cash equals collected cash to the cent.
- Built period-close controls: immutable snapshots with SHA-256, stale-review detection, blocking
  holds for late data (posted as reasoned prior-period adjustments instead of restating a closed month).
- Built expected-vs-recorded payout variance cases with owner, reason code and notes that persist
  across reruns, plus drill-down from statement line to source file row.
- Verified with 88 automated tests (hand-calculated scenarios, validation, workflow, exports), a
  headless-browser end-to-end run, and a seeded 10,000-event benchmark cross-checked against an
  independent reference model (synthetic data; demonstrates arithmetic consistency, not business impact).

**Do not write:** "saved X hours", "improved accuracy by Y%", "used by the finance team",
"production system", "SOX-compliant", or anything implying real employer data.
