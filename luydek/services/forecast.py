"""Weekly order list per supplier (handoff §5.6 baseline, explainable first).

    avg_daily = weighted average of the last 4 weeks, recent weeks weighted higher,
                counting only days the item was actually in stock
    target    = avg_daily × days until the delivery after this one
              + avg_daily × safety days (more for critical items)
    order     = max(0, target − stock), rounded UP to whole cases,
                capped at what can be sold before it expires

Every line carries the numbers behind it, so the owner can check it in their head.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .. import clock
from ..config import ForecastCfg
from . import stock

ACTIONS = ("order", "market", "check", "skip", "sleeping")


@dataclass
class Line:
    product_id: int
    sku: str
    name_en: str
    name_km: str
    unit: str
    case_size: int
    is_critical: bool
    unit_cost: float
    stock: int
    avg_daily: float
    sold_last_week: int
    sold_4w: int
    days_since_sale: int | None
    stockout_days: int
    action: str = "skip"
    reason: str = "enough"        # i18n key for the explanation
    units: int = 0                # recommended base units
    cases: int = 0
    capped_by_expiry: bool = False
    value: float = 0.0            # dollars at cost, for sleeping stock
    expiry: str | None = None     # soonest expiry still on the shelf, if near
    order_unit: str = "case"      # what `cases` counts: "box" for medicine sold by the tablet

    @property
    def cover_days(self) -> float | None:
        return round(self.stock / self.avg_daily, 1) if self.avg_daily > 0 else None


@dataclass
class SupplierPlan:
    supplier_id: int
    name: str
    emoji: str
    visit_date: date
    days_to_visit: int
    cycle_days: int
    min_order: float
    observing: bool
    lines: list[Line] = field(default_factory=list)

    def of(self, action: str) -> list[Line]:
        return [l for l in self.lines if l.action == action]

    @property
    def order_value(self) -> float:
        return round(sum(l.cases * l.case_size * l.unit_cost for l in self.of("order")), 2)

    @property
    def below_min_order(self) -> bool:
        return 0 < self.order_value < self.min_order

    @property
    def sleeping_value(self) -> float:
        return round(sum(l.value for l in self.of("sleeping")), 2)


def next_visit(visit_weekday: int, as_of: date) -> date:
    """The supplier's next visit on or after `as_of`."""
    return as_of + timedelta(days=(visit_weekday - as_of.weekday()) % 7)


def _history(conn: sqlite3.Connection, as_of: date, days: int, weights: list[float]) -> dict[int, dict]:
    """Per product: weighted in-stock average, last week's sales and stockout days."""
    start = as_of - timedelta(days=days)
    opening, net, sold = stock.daily_series(conn, start, as_of)
    product_ids = set(opening) | set(net)
    out: dict[int, dict] = {}
    for pid in product_ids:
        level = opening.get(pid, 0)
        seen = pid in opening
        week_sold = [0] * len(weights)
        week_days = [0] * len(weights)
        stockout_days = 0
        for i in range(days):
            d = (start + timedelta(days=i)).isoformat()
            day_net = net.get(pid, {}).get(d, 0)
            day_sold = sold.get(pid, {}).get(d, 0)
            week = (days - 1 - i) // 7
            if level > 0 or day_sold > 0:
                if week < len(weights):
                    week_sold[week] += day_sold
                    week_days[week] += 1
            elif seen:
                # A zero-stock day is not zero demand: leave it out of the
                # average so best-sellers are not under-ordered after a stockout.
                stockout_days += 1
            level += day_net
            seen = seen or day_net != 0
        den = sum(w * n for w, n in zip(weights, week_days, strict=True))
        num = sum(w * s for w, s in zip(weights, week_sold, strict=True))
        out[pid] = {
            "avg": num / den if den else None,
            "last_week": week_sold[0],
            "sold_4w": sum(week_sold),
            "stockout_days": stockout_days,
        }
    return out


def build_plans(
    conn: sqlite3.Connection, cfg: ForecastCfg, as_of: date | None = None, supplier_id: int | None = None
) -> list[SupplierPlan]:
    as_of = as_of or clock.today()
    history = _history(conn, as_of, cfg.history_days, cfg.week_weights)
    levels = stock.on_hand(conn)
    expiries = stock.earliest_live_expiry(conn, levels)
    expiry_soon = (as_of + timedelta(days=cfg.expiry_warning_days)).isoformat()
    observing = bool(cfg.observe_until and as_of.isoformat() < cfg.observe_until)
    from . import loose  # loose -> catalog; kept local like stock_table's import
    boxes = loose.box_sizes(conn)

    last_sale = {
        r["product_id"]: r["t"]
        for r in conn.execute(
            "SELECT product_id, MAX(created_at) AS t FROM stock_movements WHERE type = 'sale' AND qty < 0 GROUP BY product_id"
        )
    }
    first_seen = {
        r["product_id"]: r["t"]
        for r in conn.execute("SELECT product_id, MIN(created_at) AS t FROM stock_movements GROUP BY product_id")
    }
    counted = {r[0] for r in conn.execute("SELECT DISTINCT product_id FROM stock_movements WHERE type != 'sale'")}

    suppliers = conn.execute(
        "SELECT * FROM suppliers" + (" WHERE id = ?" if supplier_id else "") + " ORDER BY visit_weekday, name",
        (supplier_id,) if supplier_id else (),
    ).fetchall()
    plans = []
    for s in suppliers:
        visit = next_visit(s["visit_weekday"], as_of)
        plan = SupplierPlan(
            supplier_id=s["id"], name=s["name"], emoji=s["emoji"], visit_date=visit,
            days_to_visit=(visit - as_of).days, cycle_days=s["visit_every_days"] + s["lead_days"],
            min_order=s["min_order"], observing=observing,
        )
        for p in conn.execute(
            "SELECT * FROM products WHERE supplier_id = ? AND active = 1 ORDER BY name_en", (s["id"],)
        ):
            h = history.get(p["id"], {"avg": None, "last_week": 0, "sold_4w": 0, "stockout_days": 0})
            since = _days_since(last_sale.get(p["id"]), as_of)
            line = Line(
                product_id=p["id"], sku=p["sku"], name_en=p["name_en"], name_km=p["name_km"],
                unit=p["unit"], case_size=p["case_size"], is_critical=bool(p["is_critical"]),
                unit_cost=p["unit_cost"], stock=levels.get(p["id"], 0),
                avg_daily=round(h["avg"], 3) if h["avg"] is not None else 0.0,
                sold_last_week=h["last_week"], sold_4w=h["sold_4w"], days_since_sale=since,
                stockout_days=h["stockout_days"],
            )
            if expiries.get(p["id"]) and expiries[p["id"]] <= expiry_soon:
                line.expiry = expiries[p["id"]]
            if p["id"] in boxes and boxes[p["id"]][1] == p["case_size"]:
                line.order_unit = boxes[p["id"]][0]  # the agent sells this medicine by the box
            _decide(line, h["avg"], p["shelf_life_days"], _days_since(first_seen.get(p["id"]), as_of),
                    plan.days_to_visit + plan.cycle_days, cfg)
            if observing and line.action in ("order", "market"):
                line.action, line.reason, line.units, line.cases = "check", "observing", 0, 0
            elif p["origin"] != "catalog" and p["id"] not in counted and line.action in ("order", "market"):
                # Learned at the counter and never counted: its sales are known, its shelf isn't.
                line.action, line.reason, line.units, line.cases = "check", "count_first", 0, 0
            plan.lines.append(line)
        plan.lines.sort(key=lambda l: (ACTIONS.index(l.action), -l.cases, l.name_en))
        plans.append(plan)
    return plans


def _days_since(stamp: str | None, as_of: date) -> int | None:
    if not stamp:
        return None
    return (as_of - datetime.strptime(stamp[:10], "%Y-%m-%d").date()).days


def _decide(line: Line, avg: float | None, shelf_life: int | None, age_days: int | None, cover: int,
            cfg: ForecastCfg) -> None:
    on_shelf = max(line.stock, 0)
    old_enough = age_days is not None and age_days >= cfg.sleeping_days
    since = line.days_since_sale

    # 💤 Sleeping money: stock that has not moved, or would take months to sell.
    no_sale_for_long = (since is None and old_enough) or (since is not None and since >= cfg.sleeping_days)
    slow = avg is not None and avg > 0 and on_shelf / avg >= cfg.sleeping_cover_days
    if on_shelf > 0 and (no_sale_for_long or slow):
        line.action, line.reason = "sleeping", "sleeping"
        line.value = round(on_shelf * line.unit_cost, 2)
        return

    if avg is None:
        # Out of stock for the whole window: there is no demand signal at all.
        if line.is_critical:
            line.action, line.reason, line.cases = "order", "no_data_critical", 1
            line.units = line.case_size
        else:
            line.action, line.reason = "check", "no_data"
        return

    if on_shelf > 0 and since is not None and since >= cfg.skip_no_sales_days:
        line.action, line.reason = "skip", "no_sales"
        return

    safety = avg * (cfg.safety_days_critical if line.is_critical else cfg.safety_days)
    target = avg * cover + safety
    if on_shelf >= avg * (cover + cfg.overstock_extra_days):
        line.action, line.reason = "skip", "overstock"
        return
    need = math.ceil(target - on_shelf)
    if need <= 0:
        line.action, line.reason = "skip", "enough"
        return

    cases = math.ceil(need / line.case_size)
    if shelf_life:
        # Never buy more than can be sold before it expires. The shelf empties
        # first (FIFO), so what is already there eats into the new batch's life.
        sellable = math.floor(avg * shelf_life - on_shelf)
        cap = max(sellable, 0) // line.case_size
        if cases > cap:
            line.capped_by_expiry = True
            if cap == 0:
                line.action, line.reason, line.units = "market", "market", need
                return
            cases = cap
    line.action, line.reason = "order", "order"
    line.cases, line.units = cases, cases * line.case_size


def save(conn: sqlite3.Connection, plans: list[SupplierPlan]) -> None:
    """Record the recommendations so follow-rate can be checked after delivery."""
    now = clock.stamp()
    for plan in plans:
        for l in plan.lines:
            conn.execute(
                """
                INSERT INTO forecasts (visit_date, supplier_id, product_id, avg_daily_sales, current_stock,
                                       recommended_units, recommended_cases, action, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(visit_date, product_id) DO UPDATE SET
                    avg_daily_sales = excluded.avg_daily_sales, current_stock = excluded.current_stock,
                    recommended_units = excluded.recommended_units, recommended_cases = excluded.recommended_cases,
                    action = excluded.action, reason = excluded.reason, created_at = excluded.created_at
                """,
                (plan.visit_date.isoformat(), plan.supplier_id, l.product_id, l.avg_daily, l.stock,
                 l.units, l.cases, l.action, l.reason, now),
            )


def settle_follow_rate(conn: sqlite3.Connection, grace_days: int = 3) -> None:
    """Mark each past recommendation followed or not from what was actually delivered.

    No taps needed: an ORDER is followed if the delivery was within one case of
    it; a SKIP or SLEEPING line is followed if nothing arrived.
    """
    cutoff = (clock.today() - timedelta(days=grace_days)).isoformat()
    rows = conn.execute(
        """
        SELECT f.id, f.action, f.recommended_cases, p.case_size,
               COALESCE((SELECT SUM(m.qty) FROM stock_movements m
                         WHERE m.product_id = f.product_id AND m.type = 'delivery'
                           AND m.created_at >= f.visit_date
                           AND m.created_at < date(f.visit_date, '+' || :grace || ' day')), 0) AS delivered
        FROM forecasts f JOIN products p ON p.id = f.product_id
        WHERE f.followed IS NULL AND f.visit_date <= :cutoff AND f.action IN ('order', 'skip', 'sleeping')
        """,
        {"cutoff": cutoff, "grace": grace_days},
    ).fetchall()
    for r in rows:
        delivered_cases = r["delivered"] / max(r["case_size"], 1)
        if r["action"] == "order":
            followed = r["delivered"] > 0 and abs(delivered_cases - r["recommended_cases"]) <= 1
        else:
            followed = r["delivered"] == 0
        conn.execute("UPDATE forecasts SET followed = ? WHERE id = ?", (int(followed), r["id"]))
