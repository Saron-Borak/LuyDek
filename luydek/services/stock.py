"""The stock ledger.

    stock on hand = opening count + deliveries (and freebies) - sales ± adjustments

Every change is a signed row in `stock_movements`; nothing stores a running
total. Sales include credit sales and phone orders the camera never saw,
because they are demand the forecast has to see. Family use, breakage and
expiry are adjustments: stock left, but no customer bought it.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .. import clock

# reason -> (movement type, sign of the quantity)
ADJUST_REASONS: dict[str, tuple[str, int]] = {
    "credit_sale": ("sale", -1),
    "phone_order": ("sale", -1),
    "family_use": ("adjustment", -1),
    "damaged": ("adjustment", -1),
    "expired": ("adjustment", -1),
    "put_back": ("sale", +1),  # the camera counted it, but it went back on the shelf
}


def record(
    conn: sqlite3.Connection,
    product_id: int,
    qty: int,
    type_: str,
    *,
    ref_type: str | None = None,
    ref_id: int | None = None,
    reason: str | None = None,
    unit_cost: float | None = None,
    expiry_date: str | None = None,
    simulated: bool = False,
    at: datetime | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO stock_movements (product_id, qty, type, ref_type, ref_id, reason,
                                     unit_cost, expiry_date, simulated, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product_id, int(qty), type_, ref_type, ref_id, reason, unit_cost, expiry_date,
         int(simulated), clock.stamp(at)),
    )
    return cur.lastrowid


def adjust(conn: sqlite3.Connection, product_id: int, reason: str, units: int, by: str | None = None) -> int:
    type_, sign = ADJUST_REASONS[reason]
    return record(conn, product_id, sign * abs(units), type_, ref_type="adjust", reason=f"{reason} by {by}" if by else reason)


def opening_count(conn: sqlite3.Connection, counts: dict[int, int]) -> None:
    """Day-0 count by the install team: sets each product to exactly the counted level."""
    current = on_hand(conn)
    for product_id, counted in counts.items():
        delta = counted - current.get(product_id, 0)
        if delta:
            record(conn, product_id, delta, "opening" if product_id not in current else "spot_check",
                   reason="opening count")


def on_hand(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT product_id, SUM(qty) AS qty FROM stock_movements GROUP BY product_id")
    return {r["product_id"]: r["qty"] for r in rows}


def has_simulated(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM stock_movements WHERE simulated = 1 LIMIT 1").fetchone() is not None


def earliest_live_expiry(conn: sqlite3.Connection, levels: dict[int, int]) -> dict[int, str]:
    """First-in-first-out estimate of the soonest expiry still on the shelf.

    Walk each product's receipts newest first until they cover what is on hand;
    the oldest receipt needed is the one that expires first.
    """
    rows = conn.execute(
        """
        SELECT product_id, qty, expiry_date FROM stock_movements
        WHERE type IN ('delivery', 'freebie', 'opening') AND qty > 0
        ORDER BY product_id, created_at DESC, id DESC
        """
    ).fetchall()
    result: dict[int, str] = {}
    remaining: dict[int, int] = {}
    for r in rows:
        pid = r["product_id"]
        left = remaining.setdefault(pid, levels.get(pid, 0))
        if left <= 0:
            continue
        if r["expiry_date"] and (pid not in result or r["expiry_date"] < result[pid]):
            result[pid] = r["expiry_date"]
        remaining[pid] = left - r["qty"]
    return result


def stock_table(conn: sqlite3.Connection, expiry_warning_days: int = 30) -> list[dict]:
    """Every active product with its level, recent sales, unconfirmed sales and flags.

    One grouped query over the ledger; flags are derived in Python from that row.
    """
    now = clock.now()
    rows = conn.execute(
        """
        SELECT p.id, p.sku, p.name_en, p.name_km, p.unit, p.case_size, p.is_critical,
               p.unit_cost, p.supplier_id, s.name AS supplier_name, s.emoji,
               COALESCE(m.on_hand, 0)  AS on_hand,
               COALESCE(m.sold_7d, 0)  AS sold_7d,
               COALESCE(m.sold_28d, 0) AS sold_28d,
               m.last_sale,
               COALESCE(pend.units, 0) AS pending_units
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        LEFT JOIN (
            SELECT product_id,
                   SUM(qty) AS on_hand,
                   -SUM(CASE WHEN type = 'sale' AND created_at >= :d7  THEN qty ELSE 0 END) AS sold_7d,
                   -SUM(CASE WHEN type = 'sale' AND created_at >= :d28 THEN qty ELSE 0 END) AS sold_28d,
                   MAX(CASE WHEN type = 'sale' AND qty < 0 THEN created_at END) AS last_sale
            FROM stock_movements GROUP BY product_id
        ) m ON m.product_id = p.id
        LEFT JOIN (
            SELECT pk.product_id, SUM(ti.count * pk.units) AS units
            FROM transaction_items ti JOIN product_packs pk ON pk.id = ti.pack_id
            WHERE ti.status = 'pending'
            GROUP BY pk.product_id
        ) pend ON pend.product_id = p.id
        WHERE p.active = 1
        ORDER BY s.visit_weekday, s.name, p.name_en
        """,
        {"d7": clock.stamp(now - timedelta(days=7)), "d28": clock.stamp(now - timedelta(days=28))},
    ).fetchall()

    levels = {r["id"]: r["on_hand"] for r in rows}
    expiries = earliest_live_expiry(conn, levels)
    soon = (now.date() + timedelta(days=expiry_warning_days)).isoformat()

    table = []
    for r in rows:
        row = dict(r)
        daily = row["sold_7d"] / 7
        row["days_cover"] = round(row["on_hand"] / daily, 1) if daily > 0 and row["on_hand"] > 0 else None
        row["value"] = round(max(row["on_hand"], 0) * row["unit_cost"], 2)
        row["expiry"] = expiries.get(row["id"])
        row["negative"] = row["on_hand"] < 0
        row["out"] = row["on_hand"] <= 0
        row["low"] = not row["out"] and daily > 0 and row["on_hand"] < daily * 3
        row["expiring"] = bool(row["expiry"] and row["expiry"] <= soon)
        table.append(row)
    return table


def daily_series(
    conn: sqlite3.Connection, start: date, end: date
) -> tuple[dict[int, int], dict[int, dict[str, int]], dict[int, dict[str, int]]]:
    """Opening level at `start`, and per-day net change and units sold in [start, end).

    Three grouped queries for every product at once; the forecast rebuilds the
    daily closing stock from these without touching the database per product.
    """
    s, e = f"{start.isoformat()} 00:00:00", f"{end.isoformat()} 00:00:00"
    opening = {
        r["product_id"]: r["qty"]
        for r in conn.execute(
            "SELECT product_id, SUM(qty) AS qty FROM stock_movements WHERE created_at < ? GROUP BY product_id", (s,)
        )
    }
    net: dict[int, dict[str, int]] = {}
    sold: dict[int, dict[str, int]] = {}
    for r in conn.execute(
        """
        SELECT product_id, date(created_at) AS d,
               SUM(qty) AS net,
               -SUM(CASE WHEN type = 'sale' THEN qty ELSE 0 END) AS sold
        FROM stock_movements
        WHERE created_at >= ? AND created_at < ?
        GROUP BY product_id, d
        """,
        (s, e),
    ):
        net.setdefault(r["product_id"], {})[r["d"]] = r["net"]
        sold.setdefault(r["product_id"], {})[r["d"]] = r["sold"]
    return opening, net, sold
