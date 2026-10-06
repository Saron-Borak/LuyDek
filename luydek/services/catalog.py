"""Master product list: products, the packs the camera sees, and name aliases.

Invoice names ("Coca 330"), catalog names ("Coca-Cola can 330ml") and owner
names ("Coke") must all land on one product id; `product_aliases` is where
that mapping accumulates.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from pathlib import Path

import yaml

from .. import clock

_NON_WORD = re.compile(r"[^0-9a-zក-៿]+")  # keep Latin, digits and the Khmer block


def normalize(text: str) -> str:
    """Lower-case, strip punctuation, collapse spaces. Khmer is kept intact."""
    return _NON_WORD.sub(" ", text.lower()).strip()


def display_name(row: sqlite3.Row | dict, lang: str) -> str:
    return row["name_km"] if lang == "km" else row["name_en"]


# --- reads -------------------------------------------------------------------

def products(conn: sqlite3.Connection, supplier_id: int | None = None) -> list[sqlite3.Row]:
    sql = """
        SELECT p.*, s.name AS supplier_name
        FROM products p LEFT JOIN suppliers s ON s.id = p.supplier_id
        WHERE p.active = 1
    """
    params: tuple = ()
    if supplier_id is not None:
        sql += " AND p.supplier_id = ?"
        params = (supplier_id,)
    return conn.execute(sql + " ORDER BY s.name, p.name_en", params).fetchall()


def products_by_recent_sales(conn: sqlite3.Connection, since: str, supplier_id: int | None = None) -> list[sqlite3.Row]:
    """Best-sellers first, so a picker usually needs no scrolling."""
    return conn.execute(
        """
        SELECT p.*, COALESCE(-SUM(CASE WHEN m.type = 'sale' THEN m.qty END), 0) AS sold
        FROM products p
        LEFT JOIN stock_movements m ON m.product_id = p.id AND m.created_at >= ?
        WHERE p.active = 1 AND (? IS NULL OR p.supplier_id = ?)
        GROUP BY p.id
        ORDER BY sold DESC, p.name_en
        """,
        (since, supplier_id, supplier_id),
    ).fetchall()


def product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def suppliers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM suppliers ORDER BY visit_weekday, name").fetchall()


def supplier(conn: sqlite3.Connection, supplier_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()


def pack(conn: sqlite3.Connection, pack_id: int) -> sqlite3.Row | None:
    """A pack joined with its product, the shape recognition and review both need."""
    return conn.execute(
        """
        SELECT pk.id AS pack_id, pk.label, pk.units, pk.barcode, pk.product_id,
               p.name_en, p.name_km, p.sku
        FROM product_packs pk JOIN products p ON p.id = pk.product_id
        WHERE pk.id = ?
        """,
        (pack_id,),
    ).fetchone()


def packs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT pk.id AS pack_id, pk.label, pk.units, pk.barcode, pk.product_id,
               p.name_en, p.name_km, p.sku,
               (SELECT COUNT(*) FROM gallery_images g WHERE g.pack_id = pk.id) AS photos
        FROM product_packs pk JOIN products p ON p.id = pk.product_id
        WHERE p.active = 1
        ORDER BY p.name_en, pk.units
        """
    ).fetchall()


def default_pack_id(conn: sqlite3.Connection, product_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM product_packs WHERE product_id = ? ORDER BY units, id LIMIT 1", (product_id,)
    ).fetchone()
    return row["id"]


def pack_by_barcode(conn: sqlite3.Connection, code: str) -> int | None:
    row = conn.execute("SELECT id FROM product_packs WHERE barcode = ?", (code,)).fetchone()
    return row["id"] if row else None


def pack_label(row: sqlite3.Row, lang: str) -> str:
    """"Coca-Cola can 330ml" or "Coca-Cola can 330ml (6-pack)"."""
    name = display_name(row, lang)
    return name if row["units"] == 1 else f"{name} ({row['label']})"


# --- aliases -----------------------------------------------------------------

def resolve_alias(conn: sqlite3.Connection, text: str) -> int | None:
    row = conn.execute(
        "SELECT product_id FROM product_aliases WHERE alias_norm = ?", (normalize(text),)
    ).fetchone()
    return row["product_id"] if row else None


def add_alias(conn: sqlite3.Connection, product_id: int, text: str, source: str) -> None:
    norm = normalize(text)
    if norm:
        conn.execute(
            "INSERT OR IGNORE INTO product_aliases (product_id, alias_norm, source) VALUES (?, ?, ?)",
            (product_id, norm, source),
        )


def search(conn: sqlite3.Connection, text: str, limit: int = 8) -> list[int]:
    """Products whose name (English, Khmer, any alias or the SKU) holds every word typed, best-sellers
    first; failing that, the closest names ("coca" finds Coca-Cola 330ml, "oishi" both Oishi teas)."""
    words = normalize(text).split()
    if not words:
        return []
    rows = conn.execute(
        """
        SELECT p.id, p.sku, p.name_en, p.name_km, GROUP_CONCAT(a.alias_norm, ' ') AS aliases,
               COALESCE((SELECT -SUM(qty) FROM stock_movements m WHERE m.product_id = p.id AND m.type = 'sale'), 0) AS sold
        FROM products p LEFT JOIN product_aliases a ON a.product_id = p.id
        WHERE p.active = 1 GROUP BY p.id ORDER BY sold DESC, p.name_en
        """
    ).fetchall()
    hits = [r["id"] for r in rows
            if all(w in normalize(f"{r['name_en']} {r['name_km']} {r['aliases'] or ''} {r['sku'] or ''}") for w in words)]
    return hits[:limit] if hits else likely_products(conn, text, limit=min(limit, 3))


def fuzzy_candidates(conn: sqlite3.Connection, text: str, limit: int = 3) -> list[int]:
    """Best-guess products for an unrecognised name, most similar first."""
    return [pid for pid, _ in fuzzy_scored(conn, text, limit)]


def likely_products(conn: sqlite3.Connection, text: str, limit: int = 3) -> list[int]:
    """Products worth offering for a name written on an invoice ("Oishi 380 x24").

    Letter similarity alone offers Paracetamol for "Pocari 500", so a product must also
    share the brand word (the first word written, or its start) unless it is a near-exact match.
    """
    words = [w for w in normalize(text).split() if len(w) >= 3 and not w[0].isdigit()]
    brand = words[0] if words else ""
    keep = []
    for pid, score in fuzzy_scored(conn, text, limit * 3):
        names = conn.execute(
            "SELECT name_en AS n FROM products WHERE id = ? UNION ALL SELECT alias_norm FROM product_aliases "
            "WHERE product_id = ?", (pid, pid)).fetchall()
        tokens = {t for r in names for t in normalize(r["n"]).split()}
        if score >= 0.75 or (brand and any(t.startswith(brand) or brand.startswith(t) for t in tokens if len(t) >= 3)):
            keep.append(pid)
    return keep[:limit]


def fuzzy_scored(conn: sqlite3.Connection, text: str, limit: int = 3) -> list[tuple[int, float]]:
    """(product id, similarity 0-1) for an unrecognised name, most similar first."""
    target = normalize(text)
    names = conn.execute(
        """
        SELECT product_id, alias_norm AS name FROM product_aliases
        UNION ALL SELECT id, name_en FROM products WHERE active = 1
        UNION ALL SELECT id, name_km FROM products WHERE active = 1
        """
    ).fetchall()
    best: dict[int, float] = {}
    for row in names:
        score = difflib.SequenceMatcher(None, target, normalize(row["name"])).ratio()
        best[row["product_id"]] = max(score, best.get(row["product_id"], 0.0))
    ranked = sorted(best, key=best.get, reverse=True)
    return [(pid, best[pid]) for pid in ranked[:limit]]


def catalog_for_prompt(conn: sqlite3.Connection) -> str:
    """The catalog as compact text lines, for the invoice reader."""
    rows = conn.execute(
        """
        SELECT p.id, p.name_en, p.name_km, p.case_size, p.unit, s.name AS supplier,
               GROUP_CONCAT(a.alias_norm, ' | ') AS aliases
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        LEFT JOIN product_aliases a ON a.product_id = p.id
        WHERE p.active = 1
        GROUP BY p.id ORDER BY p.id
        """
    ).fetchall()
    return "\n".join(
        f"{r['id']}: {r['name_en']} / {r['name_km']} — {r['case_size']} {r['unit']}s per case,"
        f" supplier {r['supplier']}; also written as: {r['aliases'] or '-'}"
        for r in rows
    )


# --- writes ------------------------------------------------------------------

def add_product(
    conn: sqlite3.Connection,
    *,
    sku: str,
    name_en: str,
    name_km: str,
    unit: str = "piece",
    case_size: int = 1,
    supplier_id: int | None = None,
    is_critical: bool = False,
    shelf_life_days: int | None = None,
    unit_cost: float = 0.0,
    barcode: str | None = None,
    origin: str = "catalog",
) -> int:
    """`origin` says where the product came from: "catalog" (typed in or seeded), "counter" (named by
    the owner at a sale the camera didn't know) or "invoice" (read off a supplier invoice)."""
    cur = conn.execute(
        """
        INSERT INTO products (sku, name_en, name_km, unit, case_size, supplier_id,
                              is_critical, shelf_life_days, unit_cost, origin, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sku, name_en, name_km, unit, case_size, supplier_id, int(is_critical), shelf_life_days, unit_cost,
         origin, clock.stamp()),
    )
    product_id = cur.lastrowid
    conn.execute(
        "INSERT INTO product_packs (product_id, label, units, barcode) VALUES (?, ?, 1, ?)",
        (product_id, unit, barcode or None),
    )
    for name in (name_en, name_km):
        add_alias(conn, product_id, name, "catalog")
    return product_id


def add_pack(conn: sqlite3.Connection, product_id: int, label: str, units: int, barcode: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO product_packs (product_id, label, units, barcode) VALUES (?, ?, ?, ?)",
        (product_id, label, units, barcode or None),
    )
    return cur.lastrowid


def unique_sku(conn: sqlite3.Connection, name: str) -> str:
    """A readable sku from the English name ("Fanta can 330ml" -> "fanta-can-330ml")."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "product"
    sku, n = base, 2
    while conn.execute("SELECT 1 FROM products WHERE sku = ?", (sku,)).fetchone():
        sku, n = f"{base}-{n}", n + 1
    return sku


def update_product(conn: sqlite3.Connection, product_id: int, **fields) -> None:
    allowed = {"name_en", "name_km", "unit", "case_size", "supplier_id", "is_critical", "shelf_life_days", "unit_cost"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        conn.execute(
            f"UPDATE products SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
            (*sets.values(), product_id),
        )
    for key in ("name_en", "name_km"):
        if fields.get(key):
            add_alias(conn, product_id, fields[key], "catalog")
    if "barcode" in fields:
        target = default_pack_id(conn, product_id)
        if conn.execute("SELECT sold_loose FROM products WHERE id = ?", (product_id,)).fetchone()["sold_loose"]:
            # Sold by the tablet: the barcode is printed on the box, never on a tablet.
            target = conn.execute("SELECT id FROM product_packs WHERE product_id = ? ORDER BY units DESC, id LIMIT 1",
                                  (product_id,)).fetchone()["id"]
        conn.execute("UPDATE product_packs SET barcode = ? WHERE id = ?", (fields["barcode"] or None, target))


def archive_product(conn: sqlite3.Connection, product_id: int) -> None:
    """Hide a product from the catalog, pickers and forecasts. Its history stays."""
    conn.execute("UPDATE products SET active = 0 WHERE id = ?", (product_id,))


def delete_pack(conn: sqlite3.Connection, pack_id: int) -> bool:
    """Remove a multipack the camera no longer needs to know. False if sales already used it."""
    pk = conn.execute("SELECT units FROM product_packs WHERE id = ?", (pack_id,)).fetchone()
    used = conn.execute("SELECT 1 FROM transaction_items WHERE pack_id = ? OR guess_pack_id = ? LIMIT 1",
                        (pack_id, pack_id)).fetchone()
    if pk is None or pk["units"] == 1 or used:
        return False
    conn.execute("DELETE FROM gallery_images WHERE pack_id = ?", (pack_id,))
    conn.execute("DELETE FROM product_packs WHERE id = ?", (pack_id,))
    return True


def delete_alias(conn: sqlite3.Connection, alias_id: int) -> None:
    conn.execute("DELETE FROM product_aliases WHERE id = ? AND source != 'catalog'", (alias_id,))


def aliases(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM product_aliases WHERE product_id = ? ORDER BY source = 'catalog', id DESC", (product_id,)
    ).fetchall()


def add_supplier(conn: sqlite3.Connection, name: str, visit_weekday: int, *, emoji: str = "🚚",
                 min_order: float = 0, contact: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO suppliers (name, emoji, visit_weekday, min_order, contact) VALUES (?, ?, ?, ?, ?)",
        (name.strip(), emoji or "🚚", visit_weekday, min_order, contact or None),
    )
    return cur.lastrowid


def update_supplier(conn: sqlite3.Connection, supplier_id: int, **fields) -> None:
    allowed = {"name", "emoji", "visit_weekday", "min_order", "contact"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if sets:
        conn.execute(f"UPDATE suppliers SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                     (*sets.values(), supplier_id))


# --- gallery -----------------------------------------------------------------

def catalog_rows(conn: sqlite3.Connection) -> list[dict]:
    """Every active product with its stock, photo count and a thumbnail, for the catalog list."""
    rows = conn.execute(
        """
        SELECT p.*, s.name AS supplier_name, s.emoji,
               COALESCE(m.on_hand, 0) AS on_hand,
               (m.product_id IS NOT NULL AND (p.origin = 'catalog' OR m.counted)) AS tracked,
               COALESCE(g.photos, 0) AS photos, g.thumb,
               (SELECT COUNT(*) FROM product_packs pk WHERE pk.product_id = p.id) AS packs
        FROM products p
        LEFT JOIN suppliers s ON s.id = p.supplier_id
        LEFT JOIN (SELECT product_id, SUM(qty) AS on_hand, MAX(type != 'sale') AS counted
                   FROM stock_movements GROUP BY product_id) m
               ON m.product_id = p.id
        LEFT JOIN (
            -- SQLite takes the bare crop_path from the MIN(g.id) row: the first photo.
            SELECT pk.product_id, COUNT(*) AS photos, g.crop_path AS thumb, MIN(g.id)
            FROM gallery_images g JOIN product_packs pk ON pk.id = g.pack_id
            GROUP BY pk.product_id
        ) g ON g.product_id = p.id
        WHERE p.active = 1
        ORDER BY p.name_en
        """
    ).fetchall()
    return [dict(r) for r in rows]


def gallery(conn: sqlite3.Connection, product_id: int) -> dict[int, list[sqlite3.Row]]:
    """Gallery photos of each of the product's packs, newest first."""
    out: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute(
        """
        SELECT g.id, g.pack_id, g.crop_path, g.source, g.created_at FROM gallery_images g
        JOIN product_packs pk ON pk.id = g.pack_id WHERE pk.product_id = ? ORDER BY g.id DESC
        """,
        (product_id,),
    ):
        out.setdefault(r["pack_id"], []).append(r)
    return out


def photo_ids(conn: sqlite3.Connection) -> tuple[dict[int, int], dict[int, int]]:
    """The photo that stands for each product and for each pack in lists: its first
    enrollment photo (a clean shot on the mat), else the first learned one.
    Returns ({product_id: gallery_id}, {pack_id: gallery_id})."""
    by_product: dict[int, int] = {}
    by_pack: dict[int, int] = {}
    for r in conn.execute(
        "SELECT g.id, g.pack_id, pk.product_id FROM gallery_images g JOIN product_packs pk ON pk.id = g.pack_id "
        "ORDER BY g.source != 'enrollment', pk.units DESC, g.id"
    ):
        by_pack.setdefault(r["pack_id"], r["id"])
        by_product.setdefault(r["product_id"], r["id"])
    return by_product, by_pack


def delete_gallery_image(conn: sqlite3.Connection, media_dir: Path, gallery_id: int) -> bool:
    row = conn.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (gallery_id,)).fetchone()
    if row is None:
        return False
    (media_dir / row["crop_path"]).unlink(missing_ok=True)
    conn.execute("DELETE FROM gallery_images WHERE id = ?", (gallery_id,))
    return True


def load_seed(conn: sqlite3.Connection, path: Path) -> dict[str, int]:
    """Load suppliers and products from the seed YAML. Returns sku -> product id."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    supplier_ids: dict[str, int] = {}
    for s in data.get("suppliers", []):
        cur = conn.execute(
            """
            INSERT INTO suppliers (name, emoji, visit_weekday, visit_every_days, lead_days, min_order, contact)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                s["name"], s.get("emoji", "🚚"), s["visit_weekday"], s.get("visit_every_days", 7),
                s.get("lead_days", 0), s.get("min_order", 0), s.get("contact"),
            ),
        )
        supplier_ids[s["name"]] = cur.lastrowid

    skus: dict[str, int] = {}
    for p in data.get("products", []):
        product_id = add_product(
            conn,
            sku=p["sku"],
            name_en=p["name_en"],
            name_km=p["name_km"],
            unit=p.get("unit", "piece"),
            case_size=p.get("case_size", 1),
            supplier_id=supplier_ids.get(p.get("supplier")),
            is_critical=p.get("critical", False),
            shelf_life_days=p.get("shelf_life_days"),
            unit_cost=p.get("unit_cost", 0.0),
            barcode=p.get("barcode"),
        )
        for extra in p.get("packs", []):
            add_pack(conn, product_id, extra["label"], extra["units"], extra.get("barcode"))
        if p.get("sold_loose"):
            conn.execute("UPDATE products SET sold_loose = 1 WHERE id = ?", (product_id,))
        for alias in p.get("aliases", []):
            add_alias(conn, product_id, str(alias), "catalog")
        skus[p["sku"]] = product_id
    return skus


def seed_sim_params(path: Path) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {p["sku"]: p.get("sim", {}) for p in data.get("products", [])}


def reset(conn: sqlite3.Connection) -> None:
    """Drop every row. Used by the seed script's --reset."""
    for table in (
        "taps", "bot_messages", "digests", "forecasts", "invoice_lines", "invoices",
        "stock_movements", "transaction_items", "frame_detections", "transactions",
        "gallery_images", "product_aliases", "product_packs", "facings", "products", "suppliers",
        "bot_users", "settings",
    ):
        conn.execute(f"DELETE FROM {table}")
