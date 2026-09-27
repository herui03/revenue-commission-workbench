"""Plain-English explanation of a calculation line (shared by UI and exports)."""
from __future__ import annotations

from typing import Any

from .money import format_bps, format_minor

TYPE_LABELS = {
    "EARNING": "Commission earned",
    "CLAWBACK": "Refund clawback",
    "LATE_EARNING": "Late cash (prior-period adjustment)",
    "LATE_CLAWBACK": "Late refund (prior-period adjustment)",
    "MANUAL_ADJUSTMENT": "Manual adjustment",
}


def explain_line(ln: dict[str, Any]) -> str:
    ccy = ln["currency"]
    d = ln.get("detail") or {}

    def m(v: int) -> str:
        return format_minor(v, ccy, ascii_minus=True)

    t = ln["line_type"]
    if t in ("EARNING", "LATE_EARNING"):
        parts = []
        if ln["base_portion_minor"]:
            parts.append(f"{m(ln['base_portion_minor'])} up to threshold @ {format_bps(ln['base_rate_bps'])}")
        if ln["accel_portion_minor"]:
            parts.append(f"{m(ln['accel_portion_minor'])} above threshold @ {format_bps(ln['accel_rate_bps'])}")
        if not parts:
            parts.append("zero credit")
        prefix = ""
        if t == "LATE_EARNING":
            prefix = (f"Late cash dated {ln['event_date']} for closed {ln['original_period']}, posted in {ln['period']}; "
                      f"tiered after {ln['original_period']}'s frozen ladder. ")
        return (f"{prefix}Credited {m(ln['credited_minor'])} ({format_bps(ln['split_bps'])} split of "
                f"{m(d.get('event_amount_minor', 0))}); attainment {m(d.get('attainment_before_minor', 0))} -> "
                f"{m(d.get('attainment_after_minor', 0))} vs threshold {m(d.get('threshold_minor', 0))}: "
                f"{' + '.join(parts)} = exact {ln['exact_amount']} minor units -> {m(ln['amount_minor'])} "
                f"(half-up once; plan {ln['plan_id']} v{ln['plan_version']}).")
    if t in ("CLAWBACK", "LATE_CLAWBACK"):
        prefix = ""
        if t == "LATE_CLAWBACK":
            prefix = f"Late refund dated {ln['event_date']} for closed {ln['original_period']}, posted in {ln['period']}. "
        return (f"{prefix}Refund {m(d.get('refund_minor', 0))} of collection {ln['original_event_id']} "
                f"({m(d.get('original_collection_minor', 0))}); refunded {m(d.get('refunded_before_minor', 0))} -> "
                f"{m(d.get('refunded_after_minor', 0))}. Original earning {m(d.get('original_earning_minor', 0))} "
                f"(line {d.get('original_line_key')}, {d.get('original_period')}, plan {ln['plan_id']} "
                f"v{ln['plan_version']}) reversed cumulatively: {m(d.get('cumulative_reversal_after_minor', 0))} - "
                f"{m(d.get('cumulative_reversal_before_minor', 0))} = {m(-ln['amount_minor'])}. "
                f"Credit reversed {m(ln['credit_reversed_minor'])}. Tier capacity is not restored.")
    return (f"Manual {d.get('category', 'adjustment')} #{d.get('adj_id')} by {d.get('entered_by')}: "
            f"{d.get('reason')}" + (f" (case {d['case_id']})" if d.get("case_id") else "") +
            ". Shown separately from source earnings.")
