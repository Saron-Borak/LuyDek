"""The pilot report: the handoff's §8 metrics, week by week, before vs now.

"Before" is the observation weeks at the start of the pilot, when the camera and
the checks run but no order advice is given. "Now" is the last two weeks. The
before/after gap is the 60-day proof the distributor asked for.

Every number is computed from the ledger and the logs; nothing is modelled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .. import clock
from ..config import Config
from . import metrics, stock


@dataclass
class Week:
    start: date
    end: date                      # inclusive
    phase: str                     # baseline / pilot
    stockout_critical: int = 0     # must-have product-days with an empty shelf
    stockout_all: int = 0
    dead_value: float = 0.0        # $ at cost with no sale in 45+ days, at the week's end
    units_sold: int = 0
    accuracy: int | None = None    # % of settled camera items the camera got right
    items: int = 0
    taps_per_day: float | None = None
    follow_rate: int | None = None
    follow_n: int = 0
    active_days: int = 0           # days someone verified or adjusted in the bot
    partial: bool = False          # the week in progress: shown, but not compared


@dataclass
class Headline:
    key: str
    label: str
    better: str                    # lower / higher
    fmt: str                       # count / money / pct / rate
    before: float | None
    now: float | None

    @property
    def change(self) -> float | None:
        if self.before is None or self.now is None or self.before == 0:
            return None
        return (self.now - self.before) / self.before * 100

    @property
    def good(self) -> bool | None:
        if self.before is None or self.now is None or self.now == self.before:
            return None
        return (self.now < self.before) == (self.better == "lower")


@dataclass
class Report:
    pilot_start: date
    baseline_weeks: int
    weeks: list[Week] = field(default_factory=list)
    headlines: list[Headline] = field(default_factory=list)
    simulated: bool = False

    @property
    def comparable(self) -> bool:
        done = [w for w in self.weeks if not w.partial]
        return any(w.phase == "baseline" for w in done) and sum(w.phase == "pilot" for w in done) >= 2


SERIES = (  # key, label, better, format
    ("stockout_critical", "Empty-shelf days on must-have items", "lower", "count"),
    ("dead_value", "Dead stock (no sale in 45+ days)", "lower", "money"),
    ("follow_rate", "Order advice followed", "higher", "pct"),
    ("taps_per_day", "Owner taps per day", "lower", "rate"),
    ("accuracy", "Camera right without help", "higher", "pct"),
    ("stockout_all", "Empty-shelf days, all products", "lower", "count"),
    ("units_sold", "Units sold", "higher", "count"),
    ("active_days", "Days the owner checked in", "higher", "count"),
)


def pilot_start(conn: sqlite3.Connection, cfg: Config) -> date | None:
    if cfg.pilot.start:
        return date.fromisoformat(cfg.pilot.start)
    first = conn.execute("SELECT MIN(created_at) FROM stock_movements").fetchone()[0]
    return datetime.strptime(first[:10], "%Y-%m-%d").date() if first else None


def build(conn: sqlite3.Connection, cfg: Config, as_of: date | None = None) -> Report | None:
    start = pilot_start(conn, cfg)
    if start is None:
        return None
    as_of = as_of or clock.today()
    # Weeks run from the pilot's day 0, so week 1 is always the first observation week.
    # The observation weeks are always shown (they are the "before"), then as many of
    # the latest weeks as fit.
    n_weeks = max(1, (as_of - start).days // 7 + 1)
    base = min(cfg.pilot.baseline_weeks, n_weeks)
    shown = list(range(base)) + list(range(max(base, n_weeks - (cfg.pilot.report_weeks - base)), n_weeks))
    weeks = [Week(start + timedelta(days=7 * k), start + timedelta(days=7 * k + 6),
                  "baseline" if k < cfg.pilot.baseline_weeks else "pilot",
                  partial=start + timedelta(days=7 * k + 6) > as_of)
             for k in shown]
    report = Report(pilot_start=start, baseline_weeks=cfg.pilot.baseline_weeks, weeks=weeks)
    _shelf_metrics(conn, cfg, report, min(as_of, weeks[-1].end), shown_days={
        w.start + timedelta(days=i) for w in weeks for i in range(7)})
    _log_metrics(conn, report, as_of)
    report.simulated = conn.execute(
        "SELECT 1 FROM stock_movements WHERE simulated = 1 AND created_at >= ? LIMIT 1",
        (f"{weeks[0].start} 00:00:00",),
    ).fetchone() is not None
    report.headlines = _headlines(report)
    return report


def _shelf_metrics(conn: sqlite3.Connection, cfg: Config, report: Report, last_day: date,
                   shown_days: set[date]) -> None:
    """Empty-shelf days, units sold and dead stock, from the daily ledger of every product."""
    first_day = report.weeks[0].start
    opening, net, sold = stock.daily_series(conn, first_day, last_day + timedelta(days=1))
    products = {r["id"]: r for r in conn.execute("SELECT id, is_critical, unit_cost FROM products WHERE active = 1")}
    before = f"{first_day} 00:00:00"
    last_sale = {r[0]: _day(r[1]) for r in conn.execute(
        "SELECT product_id, MAX(created_at) FROM stock_movements WHERE type = 'sale' AND qty < 0 AND created_at < ? "
        "GROUP BY product_id", (before,))}
    first_seen = {r[0]: _day(r[1]) for r in conn.execute(
        "SELECT product_id, MIN(created_at) FROM stock_movements GROUP BY product_id")}
    dead_after = timedelta(days=cfg.forecast.sleeping_days)

    week_of = {}
    for w in report.weeks:
        for i in range(7):
            week_of[w.start + timedelta(days=i)] = w

    level = {pid: opening.get(pid, 0) for pid in products}
    day = first_day
    while day <= last_day:
        d = day.isoformat()
        w = week_of.get(day)  # None in the hidden middle weeks: levels still move
        for pid, p in products.items():
            day_sold = sold.get(pid, {}).get(d, 0)
            seen = first_seen.get(pid) is not None and first_seen[pid] <= day
            if w is not None and seen and level[pid] <= 0 and day_sold == 0:
                w.stockout_all += 1
                if p["is_critical"]:
                    w.stockout_critical += 1
            if w is not None:
                w.units_sold += day_sold
            if day_sold > 0:
                last_sale[pid] = day
            level[pid] += net.get(pid, {}).get(d, 0)
        if w is not None and (day == w.end or day == last_day):
            # Dead stock is measured on the week's last day, as the handoff defines it.
            w.dead_value = round(sum(
                level[pid] * p["unit_cost"] for pid, p in products.items()
                if level[pid] > 0 and first_seen.get(pid) and day - first_seen[pid] >= dead_after
                and (last_sale.get(pid) is None or day - last_sale[pid] >= dead_after)
            ), 2)
        day += timedelta(days=1)


def _log_metrics(conn: sqlite3.Connection, report: Report, as_of: date) -> None:
    """Camera accuracy, owner taps and follow-rate come from the logs, not the ledger."""
    for w in report.weeks:
        end = min(w.end, as_of) + timedelta(days=1)
        acc = metrics.accuracy(conn, w.start, end)
        w.accuracy, w.items = acc["accuracy"], acc["items"]
        taps = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT date(created_at)) FROM taps WHERE created_at >= ? AND created_at < ?",
            (f"{w.start} 00:00:00", f"{end} 00:00:00"),
        ).fetchone()
        days = (end - w.start).days
        w.taps_per_day = round(taps[0] / days, 1) if (taps[0] or w.items) and days else None
        w.active_days = taps[1]
        row = conn.execute(
            "SELECT COUNT(followed), SUM(followed) FROM forecasts WHERE followed IS NOT NULL "
            "AND visit_date >= ? AND visit_date <= ?",
            (w.start.isoformat(), w.end.isoformat()),
        ).fetchone()
        w.follow_n = row[0] or 0
        w.follow_rate = round(100 * (row[1] or 0) / w.follow_n) if w.follow_n else None


def _headlines(report: Report) -> list[Headline]:
    base = [w for w in report.weeks if w.phase == "baseline" and not w.partial]
    recent = [w for w in report.weeks if w.phase == "pilot" and not w.partial][-2:]
    out = []
    for key, label, better, fmt in SERIES[:5]:
        out.append(Headline(key, label, better, fmt, _mean(base, key), _mean(recent, key)))
    return out


def _mean(weeks: list[Week], key: str) -> float | None:
    values = [getattr(w, key) for w in weeks if getattr(w, key) is not None]
    return round(sum(values) / len(values), 1) if values else None


def _day(stamp: str | None) -> date | None:
    return datetime.strptime(stamp[:10], "%Y-%m-%d").date() if stamp else None
