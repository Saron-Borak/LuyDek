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
    batch_id: int | None = None,
    location_id: int | None = None,
    lot_no: str | None = None,
) -> int:
    """One signed change. Stock coming in starts a batch (see services/batches.py): it is put where
    the product lives unless `location_id` says otherwise. `batch_id` takes from one batch only."""
    if qty > 0 and batch_id is None and location_id is None:
        row = conn.execute("SELECT location_id FROM products WHERE id = ?", (product_id,)).fetchone()
        location_id = row["location_id"] if row else None
    cur = conn.execute(
        """
        INSERT INTO stock_movements (product_id, qty, type, ref_type, ref_id, reason,
                                     unit_cost, expiry_date, simulated, created_at, batch_id, location_id, lot_no)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product_id, int(qty), type_, ref_type, ref_id, reason, unit_cost, expiry_date,
         int(simulated), clock.stamp(at), batch_id, location_id, lot_no),
    )
    return cur.lastrowid


def adjust(conn: sqlite3.Connection, product_id: int, reason: str, units: int, by: str | None = None) -> int:
    type_, sign = ADJUST_REASONS[reason]
    return record(conn, product_id, sign * abs(units), type_, ref_type="adjust", reason=f"{reason} by {by}" if by else reason)


def opening_count(conn: sqlite3.Connection, counts: dict[int, int], source: str = "count_page") -> None:
    """Day-0 count by the install team: sets each product to exactly the counted level."""
    for product_id, counted in counts.items():
        count(conn, product_id, counted, source)


def count(conn: sqlite3.Connection, product_id: int, counted: int, source: str, by: str | None = None,
          expiry_date: str | None = None, location_id: int | None = None, lot_no: str | None = None) -> int:
    """Someone counted the shelf: set the level to exactly that, and log the count (a count that
    matched is worth knowing too). Returns the correction made (0 when the count matched).

    Finding more than expected brings a batch in (with `expiry_date` if the counter read one);
    finding fewer takes from the batches that expire first.
    """
    row = conn.execute("SELECT COALESCE(SUM(qty), 0) AS qty, COUNT(*) AS n FROM stock_movements WHERE product_id = ?",
                       (product_id,)).fetchone()
    # Sales still waiting to be checked have left the shelf but not the ledger yet: they are taken
    # off when checked, so the ledger is set to the count plus them, or they would count twice.
    waiting = pending_units(conn, product_id)
    expected = row["qty"] - waiting
    delta = int(counted) - expected
    conn.execute("INSERT INTO stock_counts (product_id, counted, expected, source, counted_by, created_at) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (product_id, int(counted), expected, source, by, clock.stamp()))
    if delta:
        record(conn, product_id, delta, "opening" if not row["n"] else "spot_check", reason=f"counted ({source})",
               expiry_date=expiry_date if delta > 0 else None, location_id=location_id if delta > 0 else None,
               lot_no=lot_no if delta > 0 else None)
    return delta


def pending_units(conn: sqlite3.Connection, product_id: int) -> int:
    """Units of a product in sales the owner hasn't checked yet."""
    return conn.execute(
        "SELECT COALESCE(SUM(ti.count * pk.units), 0) FROM transaction_items ti JOIN product_packs pk ON pk.id = ti.pack_id "
        "WHERE ti.status = 'pending' AND pk.product_id = ?", (product_id,)).fetchone()[0]


def last_counted(conn: sqlite3.Connection) -> dict[int, str]:
    """When each product's shelf was last counted."""
    return {r["product_id"]: r["at"] for r in conn.execute(
        """
        SELECT product_id, MAX(at) AS at FROM (
            SELECT product_id, created_at AS at FROM stock_counts
            UNION ALL SELECT product_id, created_at FROM stock_movements WHERE type IN ('opening', 'spot_check')
        ) GROUP BY product_id
        """)}


def on_hand(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute("SELECT product_id, SUM(qty) AS qty FROM stock_movements GROUP BY product_id")
    return {r["product_id"]: r["qty"] for r in rows}


def has_simulated(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM stock_movements WHERE simulated = 1 LIMIT 1").fetchone() is not None


def earliest_live_expiry(conn: sqlite3.Connection, levels: dict[int, int] | None = None) -> dict[int, str]:
    """The soonest printed expiry still on the shelf, per product (see services/batches.py: sales
    take from the batch that expires first). `levels` is accepted for older callers and unused."""
    from . import batches

    return batches.earliest_expiry(conn)


def stock_table(conn: sqlite3.Connection, expiry_warning_days: int = 30) -> list[dict]:
    """Every active product with its level, recent sales, unconfirmed sales and flags.

    One grouped query over the ledger; flags are derived in Python from that row.
    """
    now = clock.now()
    rows = conn.execute(
        """
        SELECT p.id, p.sku, p.name_en, p.name_km, p.unit, p.case_size, p.is_critical,
               p.unit_cost, p.supplier_id, p.origin, s.name AS supplier_name, s.emoji,
               COALESCE(m.on_hand, 0)  AS on_hand,
               COALESCE(m.sold_7d, 0)  AS sold_7d,
               COALESCE(m.sold_28d, 0) AS sold_28d,
               m.last_sale,
               m.product_id IS NOT NULL AS tracked,
               COALESCE(m.counted, 0) AS counted,
               COALESCE(pend.units, 0) AS pending_units
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        LEFT JOIN (
            SELECT product_id,
                   SUM(qty) AS on_hand,
                   -SUM(CASE WHEN type = 'sale' AND created_at >= :d7  THEN qty ELSE 0 END) AS sold_7d,
                   -SUM(CASE WHEN type = 'sale' AND created_at >= :d28 THEN qty ELSE 0 END) AS sold_28d,
                   MAX(CASE WHEN type = 'sale' AND qty < 0 THEN created_at END) AS last_sale,
                   MAX(type != 'sale') AS counted
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

    from . import loose  # loose -> catalog; imported here to keep stock importable on its own

    boxes = loose.box_sizes(conn)
    levels = {r["id"]: r["on_hand"] for r in rows}
    expiries = earliest_live_expiry(conn, levels)
    soon = (now.date() + timedelta(days=expiry_warning_days)).isoformat()

    table = []
    for r in rows:
        row = dict(r)
        if row["origin"] != "catalog" and not row["counted"]:
            # Learned at the counter: sales so far are real demand, but the shelf was never counted.
            row["tracked"], row["sold_uncounted"] = False, -row["on_hand"]
        daily = row["sold_7d"] / 7
        row["days_cover"] = round(row["on_hand"] / daily, 1) if daily > 0 and row["on_hand"] > 0 else None
        row["value"] = round(max(row["on_hand"], 0) * row["unit_cost"], 2)
        row["expiry"] = expiries.get(row["id"])
        row["negative"] = row["on_hand"] < 0 and bool(row["tracked"])
        row["out"] = row["on_hand"] <= 0
        row["low"] = not row["out"] and daily > 0 and row["on_hand"] < daily * 3
        row["expiring"] = bool(row["expiry"] and row["expiry"] <= soon)
        # "3 boxes + 47 tablets"; under a box, the number of tablets already says it all.
        box = boxes.get(row["id"])
        row["breakdown"] = loose.breakdown(row["on_hand"], *box, row["unit"]) if box and row["on_hand"] >= box[1] else None
        # One status per product, for the colour dot and the sort order.
        if not row["tracked"]:
            row["status"] = "new"          # never counted: nothing is known yet
        elif row["out"]:
            row["status"] = "out"
        elif row["low"]:
            row["status"] = "low"
        elif row["expiring"]:
            row["status"] = "expiring"
        else:
            row["status"] = "ok"
        row["urgency"] = (STATUS_ORDER.index(row["status"]), not row["is_critical"],
                          row["days_cover"] if row["days_cover"] is not None else 1e9)
        table.append(row)
    return table


STATUS_ORDER = ("out", "low", "expiring", "new", "ok")


def product_days(conn: sqlite3.Connection, product_id: int, days: int = 28) -> list[dict]:
    """Each of the last `days` days, today last: units sold, and whether the shelf was empty.

    "Empty" means the day started with nothing on the shelf and nothing was sold,
    so a gap in the chart reads as "nothing to sell", not "nobody wanted it".
    """
    end = clock.today() + timedelta(days=1)
    start = end - timedelta(days=days)
    opening, net, sold = daily_series(conn, start, end)
    level, seen = opening.get(product_id, 0), product_id in opening
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        s = sold.get(product_id, {}).get(d, 0)
        n = net.get(product_id, {}).get(d, 0)
        out.append({"date": d, "sold": s, "empty": seen and level <= 0 and s == 0})
        level += n
        seen = seen or n != 0
    return out


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
