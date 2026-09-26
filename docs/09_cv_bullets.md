# 09 · Honest CV bullet templates

Rules: say *synthetic*, *prototype* and *AI-assisted*; claim only what the repository shows; no
invented users, savings, accuracy percentages or business impact. Pick 2–3 and adapt.

**Who did what (be exact):** Herui directed the brief and requirements; Claude Code (AI) implemented,
tested and documented; Codex (AI) reviewed independently on Herui's behalf. Use a bullet only after you
have personally reproduced and can explain it (checklist: `docs/zh/学习指南.md` §11). Until then, describe
it as "directed an AI-assisted build" rather than "built".

**Portfolio project line**
> Revenue & Commission Operations Workbench — independent portfolio prototype (synthetic data); AI-assisted
> build (Claude Code implementation, Codex independent review) directed by me. Python, SQLite, Flask.
> github.com/herui03/revenue-commission-workbench

**Bullets (choose what you can explain in an interview — each states your role honestly)**
- Directed an AI-assisted build (Claude Code implementation, Codex independent AI review) of a cash-based
  commission workflow — import → calculate → review → close → variance — that keeps bookings, invoices,
  cash collected and commission as separately labelled metrics.
- Set the requirements for an invented demo commission policy (credit splits, marginal monthly
  accelerator, refund clawbacks against the original earning); the AI hand-computed 16 expected scenarios
  before implementation and the engine reproduces all of them — *add "which I re-derived by hand" only
  after you have done §11 step 2*.
- Required cent-exact money handling: integer minor units, one deterministic rounding per line, split
  allocation whose credited cents always equal collected cents — *say "and verified it" only after re-running §11 steps 1–3*.
- Specified period-close controls — frozen SHA-256 snapshots, stale-review rejection, blocking holds for
  late data posted as reasoned prior-period adjustments — *add "and can demonstrate them end to end" after practising §11 step 7*.
- Used an independent AI review (Codex) to test the build; it found 5 defects (e.g. a same-day refund
  foreign-key crash, invalid calendar periods) that were fixed with regression tests.
- Project evidence (AI-generated; re-run it yourself before quoting): 88 automated tests, a headless-browser
  end-to-end run, and a seeded 10,000-event benchmark cross-checked against an independent reference model —
  synthetic data, so it demonstrates arithmetic consistency, not business impact.

**Do not write:** "saved X hours", "improved accuracy by Y%", "used by the finance team",
"production system", "SOX-compliant", "built/coded single-handedly", "I found the defects", or anything
implying real employer data.
