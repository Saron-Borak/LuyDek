"""The places in the shop: Fridge, Shelf A, Back room, Drawer 4.

Named once, in the shop's own words. Each product has a home place, where new stock goes unless
someone puts it elsewhere; each batch remembers where it was put. The shelf check walks the shop
in the order of these places.
"""

from __future__ import annotations

import sqlite3

SUGGESTED = ("Counter", "Shelf A", "Shelf B", "Shelf C", "Fridge", "Back room")


def places(conn: sqlite3.Connection, active_only: bool = True) -> list[sqlite3.Row]:
    sql = "SELECT * FROM locations" + (" WHERE active = 1" if active_only else "") + " ORDER BY sort, name COLLATE NOCASE"
    return conn.execute(sql).fetchall()


def add(conn: sqlite3.Connection, name: str) -> int:
    """A new place (or one removed before, brought back). Returns its id."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("a place needs a name")
    row = conn.execute("SELECT id FROM locations WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    if row is not None:
        conn.execute("UPDATE locations SET active = 1 WHERE id = ?", (row["id"],))
        return row["id"]
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM locations").fetchone()[0]
    return conn.execute("INSERT INTO locations (name, sort) VALUES (?, ?)", (name, sort)).lastrowid


def add_suggested(conn: sqlite3.Connection) -> int:
    """The usual places of a small shop, for a quick start. Returns how many were added."""
    have = {r["name"].lower() for r in places(conn, active_only=False)}
    for name in SUGGESTED:
        if name.lower() not in have:
            add(conn, name)
    return len([n for n in SUGGESTED if n.lower() not in have])


def rename(conn: sqlite3.Connection, location_id: int, name: str) -> None:
    name = " ".join(name.split())
    if not name:
        raise ValueError("a place needs a name")
    clash = conn.execute("SELECT id FROM locations WHERE name = ? COLLATE NOCASE AND id != ?",
                         (name, location_id)).fetchone()
    if clash is not None:
        raise ValueError(f"there is already a place called {name}")
    conn.execute("UPDATE locations SET name = ? WHERE id = ?", (name, location_id))


def remove(conn: sqlite3.Connection, location_id: int) -> None:
    """Hide a place. Batches keep it in their history; products that lived there have no home place."""
    conn.execute("UPDATE locations SET active = 0 WHERE id = ?", (location_id,))
    conn.execute("UPDATE products SET location_id = NULL WHERE location_id = ?", (location_id,))


def set_home(conn: sqlite3.Connection, product_id: int, location_id: int | None, move_stock: bool = True) -> None:
    """Where a product lives. With `move_stock`, its batches that had no place yet go there too."""
    conn.execute("UPDATE products SET location_id = ? WHERE id = ?", (location_id, product_id))
    if move_stock and location_id:
        conn.execute("UPDATE stock_movements SET location_id = ? WHERE product_id = ? AND qty > 0 AND batch_id IS NULL "
                     "AND location_id IS NULL", (location_id, product_id))


def homeless(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Active products without a home place yet."""
    return conn.execute("SELECT id, name_en, name_km FROM products WHERE active = 1 AND location_id IS NULL "
                        "ORDER BY name_en").fetchall()
