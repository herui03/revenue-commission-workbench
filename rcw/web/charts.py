"""Server-rendered inline SVG charts (no JS library, works offline).

Colors come from CSS tokens (validated palette: series-1 blue / series-2 orange for identity,
blue <-> red poles for the bridge's polarity), so light/dark mode swap in one place.
Every mark carries a data-tip for the hover tooltip; the rep table on the same page is the
table view. One currency per chart — amounts in different currencies are never combined.
"""
from __future__ import annotations

import html
from typing import Any

from ..money import format_minor


def _e(s: Any) -> str:
    return html.escape(str(s), quote=True)


def attainment_svg(totals: list[dict[str, Any]], names: dict[str, str], currency: str) -> str:
    rows = [t for t in totals if t["currency"] == currency and t["threshold_minor"] is not None]
    if not rows:
        return ""
    label_w, right_pad, row_h, bar_h, top = 150, 90, 34, 16, 28
    width = 720
    plot_w = width - label_w - right_pad
    vmax = max(max(t["credited_minor"] for t in rows), max(t["threshold_minor"] for t in rows)) or 1
    vmax = int(vmax * 1.08)
    height = top + row_h * len(rows) + 8

    def x(v: int) -> float:
        return label_w + plot_w * v / vmax

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Credited cash per rep versus the monthly threshold, {_e(currency)}">']
    parts.append(f'<line class="axis" x1="{label_w}" y1="{top - 6}" x2="{label_w}" y2="{height - 4}"/>')
    for i, t in enumerate(rows):
        y = top + i * row_h
        cy = y + (row_h - bar_h) / 2
        name = names.get(t["rep_id"], t["rep_id"])
        parts.append(f'<text class="lbl" x="{label_w - 10}" y="{cy + bar_h - 4}" text-anchor="end">{_e(name)}</text>')
        base, accel = t["base_portion_minor"], t["accel_portion_minor"]
        x0, x1, x2 = x(0), x(base), x(base + accel)
        gap = 2 if base and accel else 0
        if base:
            tip = (f"{name}: {format_minor(base, currency)} credited up to the threshold, earning the base rate")
            parts.append(_bar(x0, cy, max(x1 - x0 - gap, 1), bar_h, "s1", tip, rounded=not accel))
        if accel:
            tip = f"{name}: {format_minor(accel, currency)} credited above the threshold, earning the accelerator rate"
            parts.append(_bar(x1, cy, max(x2 - x1, 1), bar_h, "s2", tip, rounded=True))
        thr = x(t["threshold_minor"])
        parts.append(f'<line class="thr" x1="{thr:.1f}" y1="{y + 3}" x2="{thr:.1f}" y2="{y + row_h - 3}" '
                     f'data-tip="Threshold {_e(format_minor(t["threshold_minor"], currency))} per rep per month"/>')
        parts.append(f'<text class="val" x="{max(x2, thr) + 8:.1f}" y="{cy + bar_h - 4}">'
                     f'{_e(format_minor(t["credited_minor"]))}</text>')
    thr0 = x(rows[0]["threshold_minor"])
    parts.append(f'<text class="muted-t" x="{thr0:.1f}" y="{top - 12}" text-anchor="middle">threshold '
                 f'{_e(format_minor(rows[0]["threshold_minor"]))}</text>')
    parts.append("</svg>")
    legend = ('<div class="legend"><span><i class="sw s1"></i>Credited up to threshold (base rate)</span>'
              '<span><i class="sw s2"></i>Credited above threshold (accelerator rate)</span>'
              '<span><i class="sw thr-key"></i>Monthly threshold</span></div>')
    return legend + "".join(parts)


def _bar(x: float, y: float, w: float, h: float, cls: str, tip: str, *, rounded: bool) -> str:
    """Horizontal bar: square at the baseline, 4px rounded data end."""
    r = min(4.0, w / 2, h / 2) if rounded else 0
    if r:
        d = (f"M{x:.1f},{y:.1f} H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
             f"V{y + h - r:.1f} Q{x + w:.1f},{y + h:.1f} {x + w - r:.1f},{y + h:.1f} H{x:.1f} Z")
    else:
        d = f"M{x:.1f},{y:.1f} H{x + w:.1f} V{y + h:.1f} H{x:.1f} Z"
    return f'<path class="{cls}" d="{d}" data-tip="{_e(tip)}"/>'


def bridge_svg(kpi: dict[str, int], currency: str) -> str:
    """Commission bridge: earned -> clawbacks -> late adjustments -> manual adjustments -> expected payouts."""
    steps = [("Commission earned", kpi["earnings_minor"], "delta"),
             ("Refund clawbacks", kpi["clawbacks_minor"], "delta"),
             ("Late (prior-period)", kpi["late_minor"], "delta"),
             ("Manual adjustments", kpi["manual_minor"], "delta"),
             ("Expected payouts", kpi["net_payable_minor"], "total")]
    running, bars = 0, []
    for label, v, kind in steps:
        if kind == "total":
            bars.append((label, 0, v, v, "tot"))
        else:
            bars.append((label, running, running + v, v, "up" if v >= 0 else "down"))
            running += v
    lo = min(0, *(min(a, b) for _, a, b, _, _ in bars))
    hi = max(0, *(max(a, b) for _, a, b, _, _ in bars)) or 1
    width, height, left, top, bottom = 720, 240, 12, 26, 44
    plot_h = height - top - bottom
    col_w = (width - 2 * left) / len(bars)
    bw = min(56, col_w * 0.5)

    def y(v: int) -> float:
        return top + plot_h * (hi - v) / (hi - lo)

    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="Commission bridge from earned to expected payouts, {_e(currency)}">']
    parts.append(f'<line class="axis" x1="{left}" y1="{y(0):.1f}" x2="{width - left}" y2="{y(0):.1f}"/>')
    for i, (label, a, b, v, cls) in enumerate(bars):
        cx = left + col_w * i + col_w / 2
        y0, y1 = sorted((y(a), y(b)))
        h = max(y1 - y0, 1.5)
        tip = f"{label}: {format_minor(v, currency, signed=cls != 'tot')}"
        parts.append(f'<rect class="{cls}" x="{cx - bw / 2:.1f}" y="{y0:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                     f'rx="3" data-tip="{_e(tip)}"/>')
        ty = y0 - 7 if (v >= 0 or cls == "tot") else y0 + h + 15
        parts.append(f'<text class="val" x="{cx:.1f}" y="{ty:.1f}" text-anchor="middle">'
                     f'{_e(format_minor(v, signed=cls != "tot"))}</text>')
        parts.append(f'<text class="lbl" x="{cx:.1f}" y="{height - 18}" text-anchor="middle">{_e(label)}</text>')
        if i < len(bars) - 1 and cls != "tot":
            nxt = left + col_w * (i + 1) + col_w / 2
            parts.append(f'<line class="conn" x1="{cx + bw / 2:.1f}" y1="{y(b):.1f}" x2="{nxt - bw / 2:.1f}" '
                         f'y2="{y(b):.1f}"/>')
    parts.append("</svg>")
    legend = ('<div class="legend"><span><i class="sw up"></i>Increase</span><span><i class="sw down"></i>Decrease'
              '</span><span><i class="sw tot"></i>Total</span></div>')
    return legend + "".join(parts)
