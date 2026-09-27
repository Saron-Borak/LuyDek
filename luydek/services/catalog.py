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


def fuzzy_candidates(conn: sqlite3.Connection, text: str, limit: int = 3) -> list[int]:
    """Best-guess products for an unrecognised name, most similar first."""
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
    return ranked[:limit]


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
) -> int:
    cur = conn.execute(
        """
        INSERT INTO products (sku, name_en, name_km, unit, case_size, supplier_id,
                              is_critical, shelf_life_days, unit_cost)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sku, name_en, name_km, unit, case_size, supplier_id, int(is_critical), shelf_life_days, unit_cost),
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
        "gallery_images", "product_aliases", "product_packs", "products", "suppliers",
        "bot_users", "settings",
    ):
        conn.execute(f"DELETE FROM {table}")
