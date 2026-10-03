"""The day in one message, and the stock left on the shelf.

The day summary is sent after the end-of-day job settles the unchecked guesses (so its
numbers are final) and whenever the owner asks (/today). Stock left, for one product or for
every product, is the stock table with today's sales added. Both read the same ledger as the
dashboard, so the phone and the laptop always agree.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from .. import clock
from . import stock
from .forecast import next_visit

SOLD_LINES = 25      # the day summary lists at most this many products sold ("+N more" after)
WATCH_LINES = 8      # ... and at most this many running out


def _window(day: date) -> tuple[str, str]:
    return f"{day.isoformat()} 00:00:00", f"{(day + timedelta(days=1)).isoformat()} 00:00:00"


def sold_on(conn: sqlite3.Connection, day: date) -> dict[int, int]:
    """Units sold per product on `day`: the camera's sales, credit sales and phone orders, less put-backs."""
    start, end = _window(day)
    return {
        r["product_id"]: r["sold"]
        for r in conn.execute(
            "SELECT product_id, -SUM(qty) AS sold FROM stock_movements "
            "WHERE type = 'sale' AND created_at >= ? AND created_at < ? GROUP BY product_id HAVING sold > 0",
            (start, end),
        )
    }


def stock_left(conn: sqlite3.Connection, expiry_warning_days: int = 30, day: date | None = None) -> list[dict]:
    """Every active product with what is left, most urgent first, and what sold `day` (today)."""
    today = sold_on(conn, day or clock.today())
    rows = stock.stock_table(conn, expiry_warning_days)
    for r in rows:
        r["sold_today"] = today.get(r["id"], 0)
    return sorted(rows, key=lambda r: (r["urgency"], r["name_en"]))


def product_stock(conn: sqlite3.Connection, product_id: int, expiry_warning_days: int = 30) -> dict | None:
    """One product's stock with its sales today, this week and these four weeks, and its agent's next visit."""
    row = next((r for r in stock_left(conn, expiry_warning_days) if r["id"] == product_id), None)
    if row is None:
        return None
    sup = conn.execute(
        "SELECT s.visit_weekday FROM products p JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = ?",
        (product_id,),
    ).fetchone()
    row["next_visit"] = next_visit(sup["visit_weekday"], clock.today()) if sup else None
    return row


def day_summary(conn: sqlite3.Connection, day: date | None = None, expiry_warning_days: int = 30) -> dict:
    """What sold on `day`, what came in, what is still to check, and what is running out now."""
    day = day or clock.today()
    start, end = _window(day)
    rows = {r["id"]: r for r in stock_left(conn, expiry_warning_days, day)}

    sold = [{**rows[pid], "sold": n} for pid, n in sold_on(conn, day).items() if pid in rows]
    sold.sort(key=lambda r: (-r["sold"], r["name_en"]))

    received = conn.execute(
        "SELECT COUNT(DISTINCT product_id) AS products, COALESCE(SUM(qty), 0) AS units FROM stock_movements "
        "WHERE type IN ('delivery', 'freebie') AND qty > 0 AND created_at >= ? AND created_at < ?",
        (start, end),
    ).fetchone()
    txns = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE started_at >= ? AND started_at < ? AND status != 'open'",
        (start, end),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM transaction_items ti JOIN transactions t ON t.id = ti.txn_id "
        "WHERE ti.status = 'pending' AND t.started_at >= ? AND t.started_at < ?",
        (start, end),
    ).fetchone()[0]
    simulated = conn.execute(
        "SELECT 1 FROM stock_movements WHERE simulated = 1 AND created_at >= ? AND created_at < ? LIMIT 1",
        (start, end),
    ).fetchone() is not None

    # Running out: products that were counted and are out or low now. Must-have items first.
    watch = [r for r in rows.values() if r["tracked"] and r["status"] in ("out", "low")]
    watch.sort(key=lambda r: (not r["is_critical"], r["urgency"], r["name_en"]))
    return {
        "date": day,
        "txns": txns,
        "units": sum(r["sold"] for r in sold),
        "sold": sold,
        "pending": pending,
        "received_units": received["units"],
        "received_products": received["products"],
        "watch": watch,
        "value": round(sum(r["value"] for r in rows.values()), 2),
        "simulated": simulated,
    }
