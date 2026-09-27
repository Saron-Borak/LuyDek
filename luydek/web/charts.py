"""Small inline-SVG column charts for the pilot report (no JS chart library).

One series per chart, one bar per week. Observation weeks are drawn in a
de-emphasis gray and pilot weeks in the accent blue ("emphasis" form), so the
eye lands on the pilot. Bars are capped at 24px with a 4px rounded top, one
hairline grid, a value label only on the latest bar, and a hit area per week
that carries the tooltip text (the page script reads data-tip).
"""

from __future__ import annotations

import math
from html import escape

from markupsafe import Markup

ACCENT = "#2a78d6"      # pilot weeks (validated: 3:1+ on the surface, CVD-distinct from the gray)
MUTED = "#8e8c84"       # observation weeks
GRID = "#e7e5df"
TEXT = "#52514e"
INK = "#0b0b0b"

W, H = 360, 150
LEFT, RIGHT, TOP, BOTTOM = 36, 6, 18, 24


def nice_ceiling(value: float) -> float:
    """1 / 2 / 2.5 / 5 x 10^k at or above `value`."""
    if value <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(value))
    for m in (1, 2, 2.5, 5, 10):
        if value <= m * exp:
            return m * exp
    return 10 * exp


def fmt(value: float | None, kind: str) -> str:
    if value is None:
        return "–"
    if kind == "money":
        if value >= 100 or float(value).is_integer():
            return f"${value:,.0f}"
        return f"${value:,.2f}"
    if kind == "pct":
        return f"{value:.0f}%"
    if kind == "rate":
        return f"{value:.1f}"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def column_chart(values: list[float | None], labels: list[str], phases: list[str], kind: str,
                 tips: list[str], title: str, partial: list[bool] | None = None) -> Markup:
    """`partial` marks weeks still in progress: drawn at half strength so they don't read as final."""
    partial = partial or [False] * len(values)
    n = len(values)
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    base_y = TOP + plot_h
    top_value = nice_ceiling(max([v for v in values if v is not None] or [0]))
    if kind == "pct":
        top_value = 100
    band = plot_w / max(n, 1)
    bar_w = min(24.0, band * 0.62)

    out = [f'<svg viewBox="0 0 {W} {H}" class="mini-chart" role="img" aria-label="{escape(title)}">']
    for frac in (0, 0.5, 1):  # hairline grid and y ticks
        y = base_y - frac * plot_h
        out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{LEFT - 6}" y="{y + 3.5:.1f}" text-anchor="end" font-size="10" fill="{TEXT}">'
                   f'{escape(fmt(top_value * frac, kind))}</text>')
    last = max((i for i, v in enumerate(values) if v is not None), default=None)
    for i, (v, label, phase, tip) in enumerate(zip(values, labels, phases, tips, strict=True)):
        cx = LEFT + band * i + band / 2
        x = cx - bar_w / 2
        if v is not None and v > 0:
            h = max(1.5, v / top_value * plot_h)
            y = base_y - h
            r = min(4.0, bar_w / 2, h)
            color = ACCENT if phase == "pilot" else MUTED
            opacity = ' fill-opacity="0.45"' if partial[i] else ""
            out.append(f'<path d="M{x:.1f},{base_y} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                       f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base_y} Z" '
                       f'fill="{color}"{opacity}/>')
        if i == last and v is not None:  # label only the latest value
            vy = base_y - (v / top_value * plot_h if v > 0 else 0) - 5
            out.append(f'<text x="{cx:.1f}" y="{vy:.1f}" text-anchor="middle" font-size="11" font-weight="600" '
                       f'fill="{INK}">{escape(fmt(v, kind))}</text>')
        if n <= 8 or i % 2 == 0 or i == n - 1:
            out.append(f'<text x="{cx:.1f}" y="{H - 8}" text-anchor="middle" font-size="9.5" fill="{TEXT}">'
                       f'{escape(label)}</text>')
        # The hit area is the whole week's band, far bigger than the bar.
        out.append(f'<rect x="{LEFT + band * i:.1f}" y="{TOP}" width="{band:.1f}" height="{plot_h}" fill="transparent" '
                   f'class="hit" tabindex="0" data-tip="{escape(tip)}"/>')
    out.append("</svg>")
    return Markup("".join(out))
