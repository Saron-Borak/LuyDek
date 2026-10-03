"""New products learned while selling.

A shop doesn't have to list and photograph everything before day one. When a
sale shows something the camera doesn't know, the owner says what it is (or
accepts the name read off the package) and it becomes a product on the spot:
the sale is recorded, the photo teaches the camera, and other unanswered sales
that look like it are re-guessed as it, so one "All correct" settles them.

Its stock stays "not counted yet" until the owner counts it (the sales are kept
and count as demand), and it joins the order lists once it has a supplier.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import catalog, transactions

MAX_NAME = 80


def clean_name(text: str) -> str:
    return " ".join((text or "").split())[:MAX_NAME]


def add_from_item(conn: sqlite3.Connection, item_id: int, name_en: str, name_km: str = "", *, by: str,
                  learn: transactions.Learner | None = None, media_dir: Path | None = None,
                  delete_photo: bool = False) -> dict:
    """Name the product in a pending or "not in the list" item. Returns what happened.

    A name that is already a product (or one of its names, "Coke") records the
    sale as that product instead of making a twin.
    """
    row = transactions.item(conn, item_id)
    if row is None:
        raise KeyError(item_id)
    if row["status"] not in ("pending", "unknown"):
        return {"status": row["status"], "already": True}
    name_en, name_km = clean_name(name_en), clean_name(name_km)
    if not name_en and not name_km:
        raise ValueError("Type the product's name first.")
    existing = catalog.resolve_alias(conn, name_en or name_km)
    if existing is not None and not conn.execute("SELECT active FROM products WHERE id = ?", (existing,)).fetchone()[0]:
        existing = None  # a removed product: start afresh
    if existing is None:
        product_id = catalog.add_product(conn, sku=catalog.unique_sku(conn, name_en or "new product"),
                                         name_en=name_en or name_km, name_km=name_km or name_en, origin="counter")
    else:
        product_id = existing
    pack_id = catalog.default_pack_id(conn, product_id)
    result = transactions.name_item(conn, item_id, pack_id, by, learn, media_dir, delete_photo)
    name = conn.execute("SELECT name_en FROM products WHERE id = ?", (product_id,)).fetchone()[0]
    return {**result, "product_id": product_id, "pack_id": pack_id, "created": existing is None, "name": name}


def unfinished(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Products learned at the counter or from an invoice that have no supplier yet, so no order list has them."""
    return conn.execute(
        "SELECT id, name_en, origin, added_at FROM products "
        "WHERE active = 1 AND origin != 'catalog' AND supplier_id IS NULL ORDER BY added_at DESC, id DESC"
    ).fetchall()
