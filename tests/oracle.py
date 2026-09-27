"""Independent reference model ("oracle") for cross-checking the engine on random data.

Deliberately written from the policy text, not from rcw/engine.py: different data structures,
Fraction arithmetic and plain loops. Scope: no closes / late events / adjustments. It shares the
policy (and its author) with the engine, so agreement shows the code matches the *policy*,
not that the policy matches any real commission plan.
"""
from __future__ import annotations

import math
from fractions import Fraction


def _half_up(f: Fraction) -> int:
    return math.floor(f + Fraction(1, 2))


def _alloc(total: int, weights: dict[str, int]) -> dict[str, int]:
    wsum = sum(weights.values())
    exact = {k: Fraction(total * w, wsum) for k, w in weights.items()}
    got = {k: math.floor(v) for k, v in exact.items()}
    left = total - sum(got.values())
    order = sorted(weights, key=lambda k: (-(exact[k] - got[k]), k))
    for k in order[:left]:
        got[k] += 1
    return got


def _plan_for(plans, assignments, rep, currency, day):
    mine = [a for a in assignments if a["rep_id"] == rep and a["effective_from"] <= day
            and (not a["effective_to"] or day <= a["effective_to"])]
    if not mine:
        return None
    a = max(mine, key=lambda x: x["effective_from"])
    vers = [p for p in plans if p["plan_id"] == a["plan_id"] and p["currency"] == currency and p["effective_from"] <= day]
    return max(vers, key=lambda p: p["effective_from"]) if vers else None


def expected_totals(contracts, splits, plans, assignments, events) -> dict[tuple[str, str, str], int]:
    """(period, rep, currency) -> net expected payout in minor units."""
    totals: dict[tuple[str, str, str], int] = {}
    earning: dict[str, dict[str, int]] = {}
    credit: dict[str, dict[str, int]] = {}
    ladders: dict[tuple[str, str, str], int] = {}
    colls = sorted((e for e in events if e["event_type"] == "COLLECTION"), key=lambda e: (e["event_date"], e["event_id"]))
    for e in colls:
        period = e["event_date"][:7]
        weights = splits[e["contract_id"]]
        shares = _alloc(e["amount"], weights)
        credit[e["event_id"]] = shares
        earning[e["event_id"]] = {}
        for rep in sorted(shares):
            plan = _plan_for(plans, assignments, rep, e["currency"], e["event_date"])
            key = (period, rep, e["currency"])
            done = ladders.get(key, 0)
            base = max(0, min(shares[rep], plan["threshold"] - done))
            above = shares[rep] - base
            ladders[key] = done + shares[rep]
            amt = _half_up(Fraction(base * plan["base_bps"] + above * plan["accel_bps"], 10000))
            earning[e["event_id"]][rep] = amt
            totals[key] = totals.get(key, 0) + amt
    refunded: dict[str, int] = {}
    reversed_credit: dict[str, dict[str, int]] = {}
    refunds = sorted((e for e in events if e["event_type"] == "REFUND"), key=lambda e: (e["event_date"], e["event_id"]))
    by_id = {e["event_id"]: e for e in events}
    for r in refunds:
        orig = by_id[r["original_event_id"]]
        x = orig["amount"]
        before = refunded.get(orig["event_id"], 0)
        after = before + r["amount"]
        refunded[orig["event_id"]] = after
        rc = reversed_credit.setdefault(orig["event_id"], {k: 0 for k in credit[orig["event_id"]]})
        rem = {k: credit[orig["event_id"]][k] - rc[k] for k in rc}
        for k, v in _alloc(r["amount"], rem).items():
            rc[k] += v
        for rep, e_amt in earning[orig["event_id"]].items():
            rev = _half_up(Fraction(e_amt * after, x)) - _half_up(Fraction(e_amt * before, x))
            key = (r["event_date"][:7], rep, r["currency"])
            totals[key] = totals.get(key, 0) - rev
    return totals
