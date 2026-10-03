"""Bringing stock in without typing it: counting a shelf on the mat, or a delivery with no invoice.

Items go on the mat (or any plain surface, photographed with a phone) a few at a time. The camera
counts them and says what they are; every one it sees is also a photo that teaches it. A close-up
of the date stamp gives the expiry (and lot), and units with different dates become different
batches. Then one save:

* **count** (setting a shop up, or recounting): the shelf is set to exactly what was counted, as
  batches with their dates and place;
* **delivery**: the units are added as new batches, and the put-away instructions say where.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date
from typing import Callable

import numpy as np

from . import batches, catalog, learn, stock

MAX_TEACH = 10  # photos a single save (or naming) adds per product
SAME_THING = 0.9  # unknown items on one scan this alike are taken to be the same product

Teacher = Callable[[sqlite3.Connection, str, int], bool]  # (conn, crop path, pack) -> added to the gallery


@dataclass
class Group:
    """Units of one product sharing one expiry date."""
    count: int
    expiry: str | None = None
    lot: str | None = None


@dataclass
class Row:
    pack_id: int | None             # what the camera (or the owner) said it is
    name: str = ""                  # a product not in the list yet
    groups: list[Group] = field(default_factory=list)
    location_id: int | None = None
    crops: list[str] = field(default_factory=list)   # media paths of the photos taken on the mat
    teach: bool = False


def parse_rows(raw: list[dict]) -> list[Row]:
    """Rows from the page, checked: whole positive counts, real dates, crop paths inside crops/."""
    rows = []
    for r in raw:
        groups = []
        for g in r.get("groups") or []:
            n = int(g.get("count") or 0)
            if n <= 0:
                continue
            expiry = (g.get("expiry") or "").strip() or None
            if expiry:
                expiry = date.fromisoformat(expiry).isoformat()
            lot = " ".join(str(g.get("lot") or "").split())[:40] or None
            groups.append(Group(n, expiry, lot))
        crops = [c for c in r.get("crops") or [] if isinstance(c, str) and c.startswith("crops/") and ".." not in c]
        pack = r.get("pack_id")
        rows.append(Row(int(pack) if pack else None, learn.clean_name(r.get("name") or ""), groups,
                        int(r["location_id"]) if r.get("location_id") else None, crops, bool(r.get("teach"))))
    return [r for r in rows if r.groups and (r.pack_id or r.name)]


def group_unknowns(vectors: list[np.ndarray], threshold: float = SAME_THING) -> list[int]:
    """A group number per item the camera doesn't know, so ten cans of something new are one question
    rather than ten: each item joins the first group whose first item it looks like."""
    firsts: list[np.ndarray] = []
    out = []
    for vec in vectors:
        v = vec / (np.linalg.norm(vec) + 1e-9)
        for i, f in enumerate(firsts):
            if float(f @ v) >= threshold:
                out.append(i)
                break
        else:
            firsts.append(v)
            out.append(len(firsts) - 1)
    return out


def name(conn: sqlite3.Connection, crops: list[str], *, pack_id: int | None = None, name_en: str = "",
         name_km: str = "", teach: Teacher | None = None) -> dict:
    """The owner says what a group of unknown items is: a product in the list, or a new one by name.
    Its photos teach the camera straight away, so the next scan knows it."""
    name_en, name_km = learn.clean_name(name_en), learn.clean_name(name_km)
    if not pack_id and not (name_en or name_km):
        raise ValueError("Pick the product or type its name first.")
    pid, pack_id, created = product_for(conn, Row(pack_id, name_en or name_km))
    if created and name_km and name_km != name_en:
        conn.execute("UPDATE products SET name_km = ? WHERE id = ?", (name_km, pid))
        catalog.add_alias(conn, pid, name_km, "catalog")
    taught = sum(bool(teach(conn, c, pack_id)) for c in crops[:MAX_TEACH]) if teach else 0
    home = conn.execute("SELECT location_id FROM products WHERE id = ?", (pid,)).fetchone()[0]
    return {"pack_id": pack_id, "product_id": pid, "label": catalog.pack_label(catalog.pack(conn, pack_id), "en"),
            "created": created, "taught": taught, "location_id": home}


def product_for(conn: sqlite3.Connection, row: Row) -> tuple[int, int, bool]:
    """(product, pack, newly added) for a row: its pack, or the product its name already is, or a new one."""
    if row.pack_id:
        pk = catalog.pack(conn, row.pack_id)
        if pk is None:
            raise ValueError("that product is gone")
        return pk["product_id"], row.pack_id, False
    existing = catalog.resolve_alias(conn, row.name)
    if existing is not None and conn.execute("SELECT active FROM products WHERE id = ?", (existing,)).fetchone()[0]:
        return existing, catalog.default_pack_id(conn, existing), False
    pid = catalog.add_product(conn, sku=catalog.unique_sku(conn, row.name), name_en=row.name, name_km=row.name,
                              origin="stock_in")
    return pid, catalog.default_pack_id(conn, pid), True


def save(conn: sqlite3.Connection, mode: str, rows: list[Row], *, by: str, teach: Teacher | None = None) -> dict:
    """Post what was counted (mode "count") or delivered (mode "delivery"). Rows marked `teach` add
    their photos to the camera's gallery. Returns what happened."""
    if mode not in ("count", "delivery"):
        raise ValueError("count or delivery")
    by_product: dict[int, list[tuple[Row, Group]]] = {}
    created, taught = [], 0
    for row in rows:
        pid, pack_id, new = product_for(conn, row)
        if new:
            created.append(row.name)
        if row.location_id and not conn.execute("SELECT location_id FROM products WHERE id = ?", (pid,)).fetchone()[0]:
            conn.execute("UPDATE products SET location_id = ? WHERE id = ?", (row.location_id, pid))  # its home from now on
        units = catalog.pack(conn, pack_id)["units"]
        for g in row.groups:
            by_product.setdefault(pid, []).append((row, Group(g.count * units, g.expiry, g.lot)))
        if teach and row.teach:
            taught += sum(bool(teach(conn, crop, pack_id)) for crop in row.crops[:MAX_TEACH])
    new_batches, units_total = [], 0
    for pid, parts in by_product.items():
        units_total += sum(g.count for _, g in parts)
        if mode == "delivery":
            cost = conn.execute("SELECT unit_cost FROM products WHERE id = ?", (pid,)).fetchone()[0] or None
            for row, g in parts:
                new_batches.append(stock.record(conn, pid, g.count, "delivery", ref_type="stock_in", unit_cost=cost,
                                                reason=f"stock in by {by}", expiry_date=g.expiry,
                                                location_id=row.location_id, lot_no=g.lot))
        else:
            _count(conn, pid, parts, by)
    return {"products": len(by_product), "units": units_total, "created": created, "taught": taught,
            "putaway": batches.put_away(conn, new_batches) if mode == "delivery" else []}


def _count(conn: sqlite3.Connection, product_id: int, parts: list[tuple[Row, Group]], by: str) -> None:
    """Set a product's shelf to exactly what was counted, batch by batch.

    With no dates or places to record this is an ordinary count. Otherwise what was there is taken
    out and what was counted is put back as batches, so the dates are the ones just read.
    """
    total = sum(g.count for _, g in parts)
    dated = any(g.expiry or g.lot or row.location_id for row, g in parts)
    if not dated:
        stock.count(conn, product_id, total, "stock_in", by=by)
        return
    row = conn.execute("SELECT COALESCE(SUM(qty), 0) AS qty, COUNT(*) AS n FROM stock_movements WHERE product_id = ?",
                       (product_id,)).fetchone()
    current, had = row["qty"], row["n"] > 0
    waiting = stock.pending_units(conn, product_id)   # sold, not checked yet: the ledger keeps them until then
    conn.execute("INSERT INTO stock_counts (product_id, counted, expected, source, counted_by, created_at) "
                 "VALUES (?, ?, ?, 'stock_in', ?, datetime('now', 'localtime'))",
                 (product_id, total, current - waiting, by))
    if current:
        stock.record(conn, product_id, -current, "spot_check", reason=f"recounted by batch by {by}")
    ordered = sorted(parts, key=lambda p: (p[1].expiry or batches.NO_DATE))
    for i, (r, g) in enumerate(ordered):
        stock.record(conn, product_id, g.count + (waiting if i == 0 else 0), "spot_check" if had else "opening",
                     reason=f"counted by {by}", expiry_date=g.expiry, location_id=r.location_id, lot_no=g.lot)
