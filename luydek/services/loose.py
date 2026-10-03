"""Medicines sold by the tablet.

A pharmacist often cuts two tablets off a strip instead of selling the box. For
those products the base unit is one tablet and the box is a pack of, say, 100,
so a whole box sold is -100 and two loose tablets are -2 in the same ledger.

Loose tablets are counted on the dispensing tray (recognition/tray.py), and
the box put down beside the tray says which medicine they are. This module
holds the product side of that: which products are sold loose, how stock reads
in boxes and tablets, and how big one tablet looks to the counter camera,
learned from counts the owner (or the tray reader) checked.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from pathlib import Path

from PIL import Image

from .. import clock
from . import catalog


def is_loose(conn: sqlite3.Connection, product_id: int | None) -> bool:
    if product_id is None:
        return False
    row = conn.execute("SELECT sold_loose FROM products WHERE id = ?", (product_id,)).fetchone()
    return bool(row and row["sold_loose"])


def base_pack(conn: sqlite3.Connection, product_id: int) -> int:
    """The one-tablet pack: what a tray item's count is counted in."""
    return catalog.default_pack_id(conn, product_id)


def box_pack(conn: sqlite3.Connection, product_id: int) -> int:
    """The biggest pack: where a loose medicine's photos and box barcode belong."""
    row = conn.execute(
        "SELECT id FROM product_packs WHERE product_id = ? ORDER BY units DESC, id LIMIT 1", (product_id,)
    ).fetchone()
    return row["id"]


def loose_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Loose-sold medicines, the most recently sold first: the short list for "which medicine?"."""
    return conn.execute(
        """
        SELECT p.*, (SELECT MAX(created_at) FROM stock_movements m
                     WHERE m.product_id = p.id AND m.type = 'sale') AS last_sale
        FROM products p WHERE p.active = 1 AND p.sold_loose = 1
        ORDER BY last_sale IS NULL, last_sale DESC, p.name_en
        """
    ).fetchall()


def box_sizes(conn: sqlite3.Connection) -> dict[int, tuple[str, int]]:
    """For each loose-sold product, its biggest pack: ("box", 100)."""
    rows = conn.execute(
        """
        SELECT pk.product_id, pk.label, pk.units FROM product_packs pk
        JOIN products p ON p.id = pk.product_id
        WHERE p.sold_loose = 1 AND pk.units > 1
        ORDER BY pk.units
        """
    ).fetchall()
    return {r["product_id"]: (r["label"], r["units"]) for r in rows}  # the last (biggest) wins


def plural(word: str, n: int) -> str:
    if n == 1:
        return word
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


def breakdown(units: int, box_label: str, box_units: int, piece: str) -> str:
    """347 tablets in boxes of 100 -> "3 boxes + 47 tablets"."""
    boxes, rest = divmod(max(units, 0), box_units)
    parts = []
    if boxes:
        parts.append(f"{boxes} {plural(box_label, boxes)}")
    if rest or not boxes:
        parts.append(f"{rest} {plural(piece, rest)}")
    return " + ".join(parts)


def set_sold_loose(conn: sqlite3.Connection, product_id: int, per_box: int, piece: str = "tablet") -> None:
    """Switch a product to selling by the tablet, converting everything already recorded.

    Stock, sales, deliveries and forecasts were in boxes; they become tablets
    (x `per_box`). The old one-box pack keeps its photos and barcode and becomes
    a pack of `per_box`; a new one-tablet pack becomes the base unit.
    """
    p = catalog.product(conn, product_id)
    if p is None:
        raise KeyError(product_id)
    if p["sold_loose"]:
        raise ValueError("already sold loose")
    if per_box < 2:
        raise ValueError("a box must hold at least 2 tablets")
    n = int(per_box)
    conn.execute("UPDATE stock_movements SET qty = qty * ? WHERE product_id = ?", (n, product_id))
    conn.execute("UPDATE product_packs SET units = units * ? WHERE product_id = ?", (n, product_id))
    conn.execute("UPDATE invoice_lines SET units_total = units_total * ? WHERE product_id = ? "
                 "AND units_total IS NOT NULL", (n, product_id))
    conn.execute("UPDATE forecasts SET avg_daily_sales = avg_daily_sales * ?, current_stock = current_stock * ?, "
                 "recommended_units = recommended_units * ? WHERE product_id = ?", (n, n, n, product_id))
    conn.execute("INSERT INTO product_packs (product_id, label, units) VALUES (?, ?, 1)", (product_id, piece))
    conn.execute("UPDATE products SET sold_loose = 1, unit = ?, case_size = case_size * ?, unit_cost = unit_cost / ? "
                 "WHERE id = ?", (piece, n, n, product_id))


# --- what one tablet looks like on the tray ------------------------------------------

def unit_area(conn: sqlite3.Connection, product_id: int | None, min_samples: int = 2) -> float | None:
    """The learned size of one tablet (share of the frame), once enough counts were checked."""
    if product_id is None:
        return None
    row = conn.execute("SELECT samples_json FROM tray_units WHERE product_id = ?", (product_id,)).fetchone()
    samples = json.loads(row["samples_json"]) if row else []
    return statistics.median(samples) if len(samples) >= min_samples else None


def learn_unit(conn: sqlite3.Connection, product_id: int, areas: list[float], count: int, keep: int = 12) -> None:
    """A checked count teaches the tray how big one tablet of this medicine is.

    One piece per tablet gives a sample per piece. Fewer pieces than tablets
    (a cut blister of two, tablets touching) gives one sample: the total over the
    count. More pieces than tablets means something else lay on the tray, so
    nothing is learned.
    """
    if count <= 0 or not areas or len(areas) > count:
        return
    new = list(areas) if len(areas) == count else [sum(areas) / count]
    row = conn.execute("SELECT samples_json FROM tray_units WHERE product_id = ?", (product_id,)).fetchone()
    samples = (json.loads(row["samples_json"]) if row else []) + [round(a, 7) for a in new]
    conn.execute(
        "INSERT INTO tray_units (product_id, samples_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(product_id) DO UPDATE SET samples_json = excluded.samples_json, updated_at = excluded.updated_at",
        (product_id, json.dumps(samples[-keep:]), clock.stamp()),
    )


def count_tablets(areas: list[float], unit: float | None, tolerance: float) -> tuple[int, bool]:
    """(tablets, sure) for the pieces on the tray, given the size of one tablet.

    A piece counts as its nearest whole number of tablets (at least one). The
    count is sure only when every piece is within `tolerance` of a whole number;
    without a learned size each piece is guessed as one tablet, never sure.
    """
    if not areas:
        return 0, True
    if not unit:
        return len(areas), False
    total, sure = 0, True
    for area in areas:
        ratio = area / unit
        k = max(1, round(ratio))
        sure = sure and abs(ratio - k) <= tolerance
        total += k
    return total, sure


def forget_units(conn: sqlite3.Connection) -> None:
    """The tray moved: tablet sizes measured at the old spot no longer apply."""
    conn.execute("DELETE FROM tray_units")


# --- the photo the owner sees -------------------------------------------------------------

def side_by_side(media_dir: Path, left: str | None, right: str, out: str, height: int = 360) -> str:
    """The source box and the tray in one photo, so one glance answers both "which" and "how many"."""
    tiles = []
    for rel in (left, right):
        if rel and (media_dir / rel).exists():
            im = Image.open(media_dir / rel).convert("RGB")
            tiles.append(im.resize((max(1, round(im.width * height / im.height)), height)))
    if not tiles:
        return right
    gap = 12 if len(tiles) == 2 else 0
    sheet = Image.new("RGB", (sum(t.width for t in tiles) + gap, height), "white")
    x = 0
    for tile in tiles:
        sheet.paste(tile, (x, 0))
        x += tile.width + gap
    (media_dir / out).parent.mkdir(parents=True, exist_ok=True)
    sheet.save(media_dir / out, quality=90)
    return out
