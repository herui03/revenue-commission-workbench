# 08 · Benchmark (generated from docs/evidence/benchmark.json — do not edit by hand)

Command: `python3 scripts/benchmark.py` (stdlib only). Seed `20260926`. Synthetic data only.

## What was run
* **10,000 cash events**: 8,750 collections + 1,250 refunds
  (366 collections fully refunded, many partially, some in later months),
  40 reps, 1,500 contracts (626 with 2-3 rep splits), SGD + USD,
  6 months, plan v1 then v2 (accelerator 8% → 9% from April).
* Strict-mode import of every file (rows shuffled) → draft calculation of all 6 months →
  calculate + submit + close each month in order → compare with the **independent oracle**
  (`tests/oracle.py`, written separately with Fractions and plain loops) → re-import the same rows
  in a different order into a second database.

## Correctness controls (arithmetic evidence)
| Control | Result |
|---|---|
| Oracle vs closed snapshots, per period × rep × currency | **480 / 480 match** |
| Credited cents == collected cents (per currency) | True |
| Credit reversed == refunded cents (per currency) | True |
| Engine control checks in every month | True |
| Draft totals (all months open) == totals after sequential close | True |
| Shuffled re-import gives identical totals | True |
| Audit hash chain | intact (30 entries) |
| Calculation lines frozen in snapshots | 15,178 |

| Currency | Collected | Credited | Refunded | Credit reversed | Expected payouts |
|---|---:|---:|---:|---:|---:|
| SGD | 26,192,826.67 | 26,192,826.67 | 2,352,818.09 | 2,352,818.09 | 1,944,299.05 |
| USD | 22,307,210.42 | 22,307,210.42 | 2,016,038.03 | 2,016,038.03 | 1,645,004.79 |

## Runtime on this machine (one run, wall clock)
| Stage | Seconds |
|---|---:|
| Import all files (strict mode, validation + provenance) | 0.891 |
| Draft compute of 6 open months | 2.963 |
| Calculate + submit + close, 6 months in order | 12.068 |
| Oracle (reference) | 0.253 |
| **Total benchmark wall time** | **21.003** |

Per month (calculate + submit + close): 2026-01: 1.221 s · 2026-02: 1.51 s · 2026-03: 1.768 s · 2026-04: 2.205 s · 2026-05: 2.44 s · 2026-06: 2.923 s. Close deliberately recomputes the month several
times (calculate, submit, close stale-check, variance refresh), so it costs more than one draft.

Environment: Intel(R) Xeon(R) Processor @ 2.80GHz, 4 logical CPUs, Python 3.11.15, SQLite 3.45.1,
Linux-6.18.44-fc-v37-x86_64-with-glibc2.39; single process, single thread.

## What this does and does not show
* **Shows:** the importer, engine and close workflow keep every cent and every control at 10k events,
  results are independent of file order, and the timings above on this machine.
* **Does not show:** real-world validity of the invented policy, performance on other hardware,
  concurrency, or any time saved for a real team (nothing was measured against a manual process).
  The oracle shares the policy with the engine, so agreement proves consistency with the *policy*,
  not that the policy is right.
