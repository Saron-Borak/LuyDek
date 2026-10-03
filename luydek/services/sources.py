"""Where the stock came from: the check for goods that didn't come from the shop's suppliers.

A copy of a real brand can look exactly like it, so the camera can't be the check. The paperwork
can: every unit sold should have come in on a delivery. Two things don't add up, and both are shown
on the shelf page and in the weekly shelf check:

* **sold more than came in**: units sold with no recorded stock to come from, which no delivery or
  invoice explained afterwards (a later count found them on the shelf, or nothing has covered them
  yet). A delivery that skipped the paperwork, a market run, or goods from somewhere else.
* **the wrong lot**: the lot printed on a pack (read from a photo of its date stamp) isn't the one
  on the invoice the batch came with.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import clock
from . import batches


def unrecorded(conn: sqlite3.Connection, days: int = 28) -> list[dict]:
    """Products that sold more in the last `days` than any delivery brought in, most first.

    Only products whose stock is known: one learned at the counter and never counted sells "from
    nowhere" until its first count, and that is expected.
    """
    since = clock.stamp(clock.now() - timedelta(days=days))
    known = {r["id"]: r for r in conn.execute(
        "SELECT p.id, p.name_en, p.name_km FROM products p WHERE p.active = 1 AND (p.origin = 'catalog' "
        "OR EXISTS (SELECT 1 FROM stock_movements m WHERE m.product_id = p.id AND m.type != 'sale'))")}
    out = []
    for pid, s in batches.replay(conn, list(known)).items():
        recent = [(at, n, how) for at, n, how in s.unrecorded if at >= since]
        if recent:
            out.append({"product_id": pid, "name_en": known[pid]["name_en"], "name_km": known[pid]["name_km"],
                        "units": sum(n for _, n, _ in recent),
                        "found": sum(n for _, n, how in recent if how == "count"),
                        "missing": sum(n for _, n, how in recent if how == "missing"),
                        "last_sold": max(at for at, _, _ in recent)})
    return sorted(out, key=lambda r: (-r["units"], r["name_en"]))


def lot_flags(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Packs whose lot isn't the one on their invoice, not yet looked into."""
    return conn.execute(
        """
        SELECT f.*, p.name_en, p.name_km, s.name AS supplier, i.id AS invoice_id
        FROM source_flags f
        JOIN products p ON p.id = f.product_id
        LEFT JOIN stock_movements m ON m.id = f.batch_id
        LEFT JOIN invoice_lines l ON l.id = m.ref_id AND m.ref_type = 'invoice_line'
        LEFT JOIN invoices i ON i.id = l.invoice_id
        LEFT JOIN suppliers s ON s.id = i.supplier_id
        WHERE f.kind = 'lot' AND f.cleared_at IS NULL
        ORDER BY f.created_at DESC
        """).fetchall()


def clear(conn: sqlite3.Connection, flag_id: int, by: str) -> None:
    """The owner looked into it (the supplier sent another lot, a typo): it's fine."""
    conn.execute("UPDATE source_flags SET cleared_at = ?, cleared_by = ? WHERE id = ? AND cleared_at IS NULL",
                 (clock.stamp(), by, flag_id))
