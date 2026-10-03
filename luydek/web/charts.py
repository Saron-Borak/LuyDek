"""Small inline-SVG charts (no JS chart library), drawn in the Night market palette.

Column charts: one series per chart, one bar per week. Observation weeks are
drawn in a de-emphasis gray and pilot weeks in violet ("emphasis" form), so the
eye lands on the pilot. Bars are capped at 24px with a 4px rounded top, one
hairline grid, a value label only on the latest bar, and a hit area per week
that carries the tooltip text (tips.js reads data-tip).

Sparklines are word-sized: no axes and no labels, just the shape of the last
two weeks next to the number they belong to.
"""

from __future__ import annotations

import math
from datetime import date
from html import escape

from markupsafe import Markup

# Checked together with the dataviz palette validator: 3:1+ on white, CVD-distinct from the gray.
ACCENT = "#534AB7"      # violet 600: pilot weeks, sales
MUTED = "#888780"       # neutral 500: observation weeks
AMBER = "#EF9F27"       # amber 400: on the dark violet hero tile
GRID = "#E7E4DB"        # neutral 100, the page's own hairline
EMPTY = "#ECEBE6"       # a day that started with an empty shelf
TEXT = "#5F5E5A"
INK = "#2C2C2A"

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


def daily_chart(days: list[dict], title: str, unit: str) -> Markup:
    """Units sold per day, one thin bar a day, today on the right.

    Days that started with an empty shelf sit on a light grey band. Dates are
    labelled weekly (counting back from today), and only the latest bar carries
    its number; every day has a hover/tap tooltip.
    """
    w, h, left, right, top, bottom = 360, 140, 30, 8, 18, 22
    n = len(days)
    plot_w, plot_h = w - left - right, h - top - bottom
    base_y = top + plot_h
    values = [d["sold"] for d in days]
    top_value = nice_ceiling(max(values) if values and max(values) > 0 else 1)
    band = plot_w / max(n, 1)
    bar_w = max(2.0, min(9.0, band * 0.7))
    out = [f'<svg viewBox="0 0 {w} {h}" class="mini-chart" role="img" aria-label="{escape(title)}">']
    for i, d in enumerate(days):  # empty-shelf days first, behind everything
        if d["empty"]:
            out.append(f'<rect x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" fill="{EMPTY}"/>')
    for frac in (0, 0.5, 1):
        y = base_y - frac * plot_h
        out.append(f'<line x1="{left}" x2="{w - right}" y1="{y:.1f}" y2="{y:.1f}" stroke="{GRID}" stroke-width="1"/>')
        out.append(f'<text x="{left - 6}" y="{y + 3.5:.1f}" text-anchor="end" font-size="10" fill="{TEXT}">'
                   f'{escape(fmt(top_value * frac, "count"))}</text>')
    for i, (d, v) in enumerate(zip(days, values, strict=True)):
        cx = left + band * i + band / 2
        x = cx - bar_w / 2
        if v > 0:
            bh = max(1.5, v / top_value * plot_h)
            y = base_y - bh
            r = min(2.0, bar_w / 2, bh)
            out.append(f'<path d="M{x:.1f},{base_y} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                       f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base_y} Z" '
                       f'fill="{ACCENT}"/>')
        day = date.fromisoformat(d["date"])
        if (n - 1 - i) % 7 == 0:
            out.append(f'<text x="{cx:.1f}" y="{h - 7}" text-anchor="middle" font-size="9.5" fill="{TEXT}">'
                       f'{"today" if i == n - 1 else day.strftime("%d %b").lstrip("0")}</text>')
        if i == n - 1 and v > 0:
            vy = base_y - v / top_value * plot_h - 5
            out.append(f'<text x="{cx:.1f}" y="{vy:.1f}" text-anchor="middle" font-size="11" font-weight="600" '
                       f'fill="{INK}">{v}</text>')
        tip = f'{day.strftime("%a %d %b")}: ' + ("shelf empty, nothing to sell" if d["empty"] and not v
                                                 else f"{v} {unit if v == 1 else unit + 's'} sold")
        out.append(f'<rect x="{left + band * i:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" fill="transparent" '
                   f'class="hit" tabindex="0" data-tip="{escape(tip)}"/>')
    out.append("</svg>")
    return Markup("".join(out))


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


def sparkline(values: list[float], color: str = ACCENT, *, w: int = 84, h: int = 24, kind: str = "bars",
              label: str = "", tips: list[str] | None = None) -> Markup:
    """A word-sized chart of recent days, oldest first.

    "bars" suits sparse counts (a product's sales, many days zero): each day sits
    on a hairline so a quiet day still reads as a day. "line" suits a busier total
    (the whole shop): a 2px line over a faint area, with a dot on today.
    `tips`, one per value, adds a hover/tap hit area per day.
    """
    n = len(values)
    top = max(values) if values and max(values) > 0 else 1
    pad = 3 if kind == "line" else 1
    band = (w - 2 * pad) / max(n, 1)
    base = h - 1
    out = [f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" class="spark mini-chart" role="img" '
           f'aria-label="{escape(label)}"><title>{escape(label)}</title>']
    if kind == "line":
        xs = [pad + band * i + band / 2 for i in range(n)]
        ys = [pad + (h - 2 * pad) * (1 - v / top) for v in values]
        pts = [f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True)]
        if n:
            out.append(f'<path d="M{xs[0]:.1f},{base} L{" L".join(pts)} L{xs[-1]:.1f},{base} Z" '
                       f'fill="{color}" fill-opacity="0.14"/>')
            out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
            out.append(f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3" fill="{color}"/>')
    else:
        out.append(f'<line x1="{pad}" x2="{w - pad}" y1="{base + 0.5}" y2="{base + 0.5}" stroke="{color}" '
                   f'stroke-opacity="0.25" stroke-width="1"/>')
        bar_w = max(1.5, band * 0.66)
        for i, v in enumerate(values):
            if v <= 0:
                continue
            x = pad + band * i + (band - bar_w) / 2
            bh = max(2.0, v / top * (h - 3))
            y = base - bh
            r = min(1.5, bar_w / 2)
            out.append(f'<path d="M{x:.1f},{base} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                       f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base} Z" '
                       f'fill="{color}"/>')
    for i, tip in enumerate(tips or []):
        out.append(f'<rect x="{pad + band * i:.1f}" y="0" width="{band:.1f}" height="{h}" fill="transparent" '
                   f'class="hit" tabindex="0" data-tip="{escape(tip)}"/>')
    out.append("</svg>")
    return Markup("".join(out))


def hour_name(hour: int) -> str:
    """0-23 as the owner says it: 9am, 12pm, 5pm."""
    return f"{(hour % 12) or 12}{'am' if hour < 12 else 'pm'}"


def hours_chart(counts: dict[int, int], days: int, title: str = "Sales by hour of day") -> Markup:
    """Sales per hour of the day, averaged over the days the camera ran.

    At least 7am to 9pm is shown so a quiet morning reads as quiet, not missing.
    The busiest hour carries its number; every hour has a tooltip.
    """
    w, h, left, right, top_pad, bottom = 420, 150, 8, 8, 20, 24
    hours = [hr for hr, c in counts.items() if c]
    lo, hi = min([7, *hours]), max([21, *hours])
    span = list(range(lo, hi + 1))
    days = max(days, 1)
    avg = [counts.get(hr, 0) / days for hr in span]
    top = max(avg) if max(avg) > 0 else 1
    peak = max(range(len(span)), key=lambda i: avg[i])
    plot_w, plot_h = w - left - right, h - top_pad - bottom
    base_y = top_pad + plot_h
    band = plot_w / len(span)
    bar_w = min(18.0, band * 0.66)
    out = [f'<svg viewBox="0 0 {w} {h}" class="mini-chart" role="img" aria-label="{escape(title)}">',
           f'<line x1="{left}" x2="{w - right}" y1="{base_y}" y2="{base_y}" stroke="{GRID}" stroke-width="1"/>']
    for i, (hr, v) in enumerate(zip(span, avg, strict=True)):
        cx = left + band * i + band / 2
        x = cx - bar_w / 2
        if v > 0:
            bh = max(2.0, v / top * plot_h)
            y = base_y - bh
            r = min(4.0, bar_w / 2, bh)
            out.append(f'<path d="M{x:.1f},{base_y} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} '
                       f'H{x + bar_w - r:.1f} Q{x + bar_w:.1f},{y:.1f} {x + bar_w:.1f},{y + r:.1f} V{base_y} Z" '
                       f'fill="{ACCENT}"/>')
        if i == peak and v > 0:
            out.append(f'<text x="{cx:.1f}" y="{base_y - v / top * plot_h - 5:.1f}" text-anchor="middle" '
                       f'font-size="12" font-weight="600" fill="{INK}">{escape(fmt(round(v, 1), "count"))}</text>')
        if hr % 3 == 0:
            out.append(f'<text x="{cx:.1f}" y="{h - 6}" text-anchor="middle" font-size="11" fill="{TEXT}">'
                       f'{hour_name(hr)}</text>')
        n = counts.get(hr, 0)
        tip = (f"{hour_name(hr)}–{hour_name((hr + 1) % 24)}: {n} sale{'s' if n != 1 else ''} in {days} "
               f"day{'s' if days != 1 else ''}" + (f", about {fmt(round(v, 1), 'count')} a day" if n else ""))
        out.append(f'<rect x="{left + band * i:.1f}" y="{top_pad}" width="{band:.1f}" height="{plot_h}" '
                   f'fill="transparent" class="hit" tabindex="0" data-tip="{escape(tip)}"/>')
    out.append("</svg>")
    return Markup("".join(out))
