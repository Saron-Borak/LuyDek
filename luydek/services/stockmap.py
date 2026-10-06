"""The shop's shelves, face on: what stands where, what has expired, and where money sleeps.

Like a map app, for stock, drawn the way the owner sees the shop: each place (Shelf A, the fridge,
the counter, the back room) from the front, level by level, one at a time like slides. What stands
where (the facings) comes from the owner: arranged on the page, read off a photo of the shelf, or
taken from the spreadsheet they already keep (shelfsheet.py). On each shelf:

* every product in its spot, with how many are left, its batches, their dates and lots;
* what has expired, what expires soon, and what won't sell before it expires (the shelf check);
* sleeping money: stock that hasn't sold for weeks (the order lists' rule), valued at cost;
* gaps: products out of stock, in the spot they stand in.

The page finds anything by name or lot ("Shelf A, level 2, 3rd from the left") and filters by any
of these; the shelf check becomes a walk, shelf by shelf.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta

from .. import clock
from ..config import Config
from . import batches, catalog, locations, stock

KINDS = ("shelf", "fridge", "counter", "storage", "other")
LEVELS = {"shelf": 4, "fridge": 4, "counter": 1, "storage": 3, "other": 2}  # usual levels, top to bottom
MAX_LEVELS = 12
MAX_WIDTH = 12  # facings: how many of a product stand side by side


def kind_of(name: str) -> str:
    """What a place is, guessed from its name (the owner can change it)."""
    n = name.lower()
    if re.search(r"fridge|cooler|freezer|chiller|ទូទឹកកក|ទូត្រជាក់", n):
        return "fridge"
    if re.search(r"counter|till|cashier|checkout|តុលក់|តុគិតលុយ", n):
        return "counter"
    if re.search(r"back ?room|store ?room|storage|stock ?room|warehouse|ឃ្លាំង", n):
        return "storage"
    if re.search(r"shelf|shelves|rack|aisle|display|drawer|cabinet|ធ្នើ|ទូ", n):
        return "shelf"
    return "other"


def shelves(conn: sqlite3.Connection) -> list[dict]:
    """Every active place in walk order, with what it is and how many levels it has."""
    out = []
    for r in conn.execute("SELECT * FROM locations WHERE active = 1 ORDER BY sort, name COLLATE NOCASE"):
        kind = r["kind"] if r["kind"] in KINDS else kind_of(r["name"])
        out.append({"id": r["id"], "name": r["name"], "sort": r["sort"], "kind": kind,
                    "levels": r["levels"] or LEVELS[kind]})
    return out


def facings(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """What stands where: for each place, {product, level, width} level by level, left to right."""
    out: dict[int, list[dict]] = {}
    for r in conn.execute("SELECT f.* FROM facings f JOIN products p ON p.id = f.product_id AND p.active = 1 "
                          "ORDER BY f.location_id, f.level, f.position, f.id"):
        out.setdefault(r["location_id"], []).append({"product": r["product_id"], "level": r["level"],
                                                     "width": r["width"]})
    return out


def add_place(conn: sqlite3.Connection, name: str, kind: str | None = None, levels: int | None = None) -> int:
    """A new place (or one removed before, brought back), with what it is and its levels."""
    location_id = locations.add(conn, name)
    kind = kind if kind in KINDS else None
    levels = max(1, min(MAX_LEVELS, int(levels))) if levels else None
    conn.execute("UPDATE locations SET kind = COALESCE(?, kind), levels = COALESCE(?, levels) WHERE id = ?",
                 (kind, levels, location_id))
    return location_id


def save_shelf(conn: sqlite3.Connection, location_id: int, items: list[dict] | None = None, *,
               levels: int | None = None, kind: str | None = None) -> None:
    """A shelf as the owner arranged it: what it is, its levels, and `items` ({product, level, width})
    left to right on each level, replacing what stood there. A product put on a shelf for the first
    time that has no home place yet gets this one, with its stock that had no place."""
    row = conn.execute("SELECT * FROM locations WHERE id = ? AND active = 1", (location_id,)).fetchone()
    if row is None:
        raise ValueError("that place is gone")
    kind = kind if kind in KINDS else None
    if levels is not None:
        levels = max(1, min(MAX_LEVELS, int(levels)))
    conn.execute("UPDATE locations SET kind = COALESCE(?, kind), levels = COALESCE(?, levels) WHERE id = ?",
                 (kind, levels, location_id))
    have = levels or row["levels"] or LEVELS[kind or (row["kind"] if row["kind"] in KINDS else kind_of(row["name"]))]
    if items is None:
        if levels is not None:     # fewer levels: what stood on the ones taken away goes to the bottom one
            items = [{**f, "level": min(f["level"], have)} for f in facings(conn).get(location_id, [])]
        else:
            return
    active = {r[0] for r in conn.execute("SELECT id FROM products WHERE active = 1")}
    conn.execute("DELETE FROM facings WHERE location_id = ?", (location_id,))
    position: dict[int, int] = {}
    for it in items:
        product = int(it["product"])
        if product not in active:
            raise ValueError("one of those products is gone from the list")
        level = max(1, min(have, int(it.get("level") or 1)))
        width = max(1, min(MAX_WIDTH, int(it.get("width") or 1)))
        position[level] = position.get(level, 0) + 1
        conn.execute("INSERT INTO facings (location_id, level, position, product_id, width) VALUES (?, ?, ?, ?, ?)",
                     (location_id, level, position[level], product, width))
        if conn.execute("SELECT location_id FROM products WHERE id = ?", (product,)).fetchone()[0] is None:
            locations.set_home(conn, product, location_id)


def reorder(conn: sqlite3.Connection, ids: list[int]) -> None:
    """The order of the places: the slides, and the shelf check's walk. Places not named keep their
    order, after these."""
    ids = [int(i) for i in ids]
    rest = [r[0] for r in conn.execute("SELECT id FROM locations ORDER BY sort, name COLLATE NOCASE") if r[0] not in ids]
    for i, location_id in enumerate(ids + rest, start=1):
        conn.execute("UPDATE locations SET sort = ? WHERE id = ?", (i, location_id))


def sleeping(conn: sqlite3.Connection, cfg: Config) -> dict[int, dict]:
    """Products whose stock is sleeping, the order lists' rule: none sold for `sleeping_days` (and
    stocked at least that long), or enough on the shelf for `sleeping_cover_days` at today's pace."""
    fc = cfg.forecast
    today = clock.today()
    first = {r["product_id"]: r["at"] for r in conn.execute(
        "SELECT product_id, MIN(created_at) AS at FROM stock_movements GROUP BY product_id")}
    out = {}
    for r in stock.stock_table(conn, fc.expiry_warning_days):
        if not r["tracked"] or r["on_hand"] <= 0:
            continue
        since = (today - datetime.strptime(r["last_sale"][:10], "%Y-%m-%d").date()).days if r["last_sale"] else None
        age = (today - datetime.strptime(first[r["id"]][:10], "%Y-%m-%d").date()).days if r["id"] in first else None
        avg = r["sold_28d"] / 28
        idle = (since is None and age is not None and age >= fc.sleeping_days) or (
            since is not None and since >= fc.sleeping_days)
        slow = avg > 0 and r["on_hand"] / avg >= fc.sleeping_cover_days
        if idle or slow:
            out[r["id"]] = {"days_since_sale": since, "cover_days": round(r["on_hand"] / avg) if avg > 0 else None,
                            "why": "no sale" if idle else "slow"}
    return out


def stock_map(conn: sqlite3.Connection, cfg: Config) -> dict:
    """Everything the shelves show, in one go."""
    days = cfg.forecast.expiry_warning_days
    today = clock.today()
    soon = today + timedelta(days=days)
    places = shelves(conn)
    by_id = {p["id"]: p for p in places}
    spots = facings(conn)
    table = {r["id"]: r for r in stock.stock_table(conn, days)}
    asleep = sleeping(conn, cfg)
    actions = {t.batch.id: t.action for t in batches.shelf_check(conn, days)}
    photos = catalog.photo_ids(conn)[0]

    for p in places:
        p.update(facings=spots.get(p["id"], []), products=set(), units=0, value=0.0, expired=0, expiring=0, clear=0,
                 sleeping_value=0.0, out=[])
    loose = {"products": set(), "units": 0}
    rows = []
    for pid, s in batches.replay(conn).items():
        prod = table.get(pid)
        if prod is None:
            continue
        for b in s.open:
            exp = b.expiry or b.est_expiry
            days_left = (date.fromisoformat(exp) - today).days if exp else None
            status = []
            if b.expiry and days_left is not None and days_left < 0:
                status.append("expired")
            elif b.expiry and date.fromisoformat(b.expiry) <= soon:
                status.append("expiring")
            if actions.get(b.id) == "clear":
                status.append("clear")
            if pid in asleep:
                status.append("sleeping")
            value = round(b.left * (prod["unit_cost"] or 0), 2)
            place = by_id.get(b.location_id)
            rows.append({"batch": b.id, "product": pid, "name": prod["name_en"], "name_km": prod["name_km"],
                         "sku": prod["sku"], "photo": photos.get(pid), "place": place["id"] if place else None,
                         "left": b.left, "expiry": b.expiry, "estimated": b.expiry is None and b.est_expiry is not None,
                         "days_left": days_left, "lot": b.lot_no, "value": value, "status": status,
                         "since_sale": asleep.get(pid, {}).get("days_since_sale"),
                         "received": b.received_at[:10]})
            target = place if place else loose
            target["products"].add(pid)
            target["units"] += b.left
            if place:
                place["value"] += value
                place["expired"] += "expired" in status
                place["expiring"] += "expiring" in status
                place["clear"] += "clear" in status
                place["sleeping_value"] += value if "sleeping" in status else 0
    # Out of stock: a gap in each spot the product stands in, or at its home place if it has no spot.
    stands = {}
    for location_id, fs in spots.items():
        for f in fs:
            if location_id in by_id:
                stands.setdefault(f["product"], set()).add(location_id)
    homes = {r["id"]: r["location_id"] for r in conn.execute("SELECT id, location_id FROM products WHERE active = 1")}
    out_of_stock = []
    for pid, r in table.items():
        if r["tracked"] and r["on_hand"] <= 0:
            where = sorted(stands.get(pid) or ({homes[pid]} if homes.get(pid) in by_id else set()))
            out_of_stock.append({"product": pid, "name": r["name_en"], "name_km": r["name_km"], "places": where,
                                 "photo": photos.get(pid), "critical": bool(r["is_critical"]),
                                 "sold_7d": r["sold_7d"]})
            for location_id in where:
                by_id[location_id]["out"].append(pid)
    for p in places:
        p["products"] = len(p["products"])
        p["value"] = round(p["value"], 2)
        p["sleeping_value"] = round(p["sleeping_value"], 2)

    walk, stop = [], None
    for t in batches.shelf_check(conn, days):
        pl = t.batch.location_id if t.batch.location_id in by_id else None
        if stop is None or stop["place"] != pl:
            stop = {"place": pl, "tasks": []}
            walk.append(stop)
        stop["tasks"].append({"batch": t.batch.id, "product": t.product["id"], "name": t.product["name_en"],
                              "left": t.batch.left, "expiry": t.batch.expiry, "days_left": t.days_left,
                              "action": t.action})
    products = {r["id"]: {"name": r["name_en"], "name_km": r["name_km"], "sku": r["sku"], "photo": photos.get(r["id"]),
                          "home": homes.get(r["id"])}
                for r in conn.execute("SELECT id, name_en, name_km, sku FROM products WHERE active = 1 ORDER BY name_en")}
    return {"places": places, "batches": rows, "out": out_of_stock, "walk": walk, "products": products,
            "unplaced": {"products": len(loose["products"]), "units": loose["units"]},
            "kinds": list(KINDS), "default_levels": LEVELS,
            "warning_days": days, "sleeping_days": cfg.forecast.sleeping_days, "today": today.isoformat()}
