"""Stock-in from supplier invoices, and new products from them.

A photo of the paper invoice goes to Claude with the store's catalog; Claude
returns structured lines (item as written, quantity, unit, cost, expiry, free
or not) and its best product match for each. The alias table then overrides
the model wherever the owner has already taught us a name, and the owner
confirms the whole invoice with one tap before any stock moves.

A line that isn't in the product list yet comes with a clean name, the case
size and the price, so it becomes a product in one tap. On install day, a few
past invoices build most of the product list without adding stock twice
("only add the products").
"""

from __future__ import annotations

import base64
import difflib
import io
import json
import sqlite3
from typing import Literal

import anthropic
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from .. import clock, db
from ..config import Config
from . import catalog, stock


class ExtractedLine(BaseModel):
    text_as_written: str = Field(description="The item name exactly as written on the invoice, any script")
    product_id: int | None = Field(description="Catalog id of this item, or null if no catalog entry clearly fits")
    quantity: float = Field(description="Quantity in the invoice's unit")
    unit: Literal["case", "pack", "piece"]
    units_per_case: int | None = Field(description="Units per case or pack, only if printed on the invoice")
    unit_cost: float | None = Field(description="Price per invoice unit (not the line total); null if not shown")
    expiry_date: str | None = Field(description="Expiry date as YYYY-MM-DD if written for this line")
    is_free: bool = Field(description="True for free, bonus or promotional lines")
    product_name: str | None = Field(None, description=(
        "For lines with product_id null: a clean shelf name in Latin letters, brand + product + variant + size, "
        "e.g. 'Coca-Cola can 330ml'. Expand abbreviations only when sure; null if the line is unreadable"))
    product_name_km: str | None = Field(None, description="The Khmer name if the line is written in Khmer, else null")
    unit_name: str | None = Field(None, description=(
        "What one single item is: can, bottle, sachet, box, strip, pack or piece; null if unclear"))


class ExtractedInvoice(BaseModel):
    supplier_name: str | None
    invoice_date: str | None = Field(description="YYYY-MM-DD if legible")
    currency: Literal["USD", "KHR"] | None
    lines: list[ExtractedLine]


SYSTEM_PROMPT = """You read supplier delivery invoices for small shops and pharmacies in Cambodia.
Invoices may be printed or handwritten, in Khmer, English, or both, and are often photographed
at an angle on a counter. Transcribe what is there; the shop owner checks every line afterwards."""

INSTRUCTIONS = """Extract every line item from this supplier invoice.

The shop's catalog (id: English / Khmer name — case size, supplier; other names it goes by):
{catalog}

Known suppliers: {suppliers}

Rules:
- One entry per line item, in the order they appear. Never invent lines that are not on the invoice.
- text_as_written: copy the item name as written, keeping Khmer script as it is.
- product_id: the catalog id only when the line clearly is that product, including size and
  variant (250mg is not 500mg; 380ml is not 420ml; chocolate is not strawberry). Otherwise null.
- unit: "case" for cases or cartons (កេស, ឡាំង, ctn, carton); "pack" for packs, bundles or
  shrink-wrapped multipacks; "piece" for single bottles, cans, tins, sachets or single boxes.
- unit_cost: the price per invoice unit. If only a line total is shown, divide it by the quantity.
- is_free: true for free goods, bonus or promotion lines (FOC, free, bonus, ថែម, ជូន).
- If a number is hard to read, give your best reading rather than skipping the line.
- For lines with no catalog match, also give product_name (and product_name_km, unit_name) so the
  shop can add the product in one tap. units_per_case matters most here: give it whenever the
  invoice prints it (24x330ml, 12 btl/ctn, ១២ដប)."""


class ExtractionError(Exception):
    """The invoice could not be read; the owner should use the manual form."""


class ExtractionOffline(ExtractionError):
    """Temporary: no connection or the API is overloaded. Retry later."""


def prepare_image(data: bytes, max_long_edge: int) -> bytes:
    """Upright, downscaled JPEG. Phone photos arrive sideways and far larger than needed."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_long_edge, max_long_edge))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


def extract(image: bytes, catalog_text: str, supplier_names: list[str], cfg: Config,
            client: anthropic.Anthropic | None = None) -> ExtractedInvoice:
    client = client or anthropic.Anthropic()
    jpeg = prepare_image(image, cfg.invoices.max_long_edge)
    try:
        response = client.beta.messages.parse(
            model=cfg.invoices.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode()}},
                    {"type": "text", "text": INSTRUCTIONS.format(catalog=catalog_text,
                                                                 suppliers=", ".join(supplier_names))},
                ],
            }],
            output_format=ExtractedInvoice,
            # If a safety classifier declines, re-run on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except (anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError) as exc:
        raise ExtractionOffline(str(exc)) from exc
    except anthropic.APIStatusError as exc:
        raise ExtractionError(f"{exc.status_code}: {exc.message}") from exc
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise ExtractionError(f"no structured result (stop_reason={response.stop_reason})")
    return response.parsed_output


# --- invoice lifecycle -----------------------------------------------------------

def create_from_photo(conn: sqlite3.Connection, photo_path: str, chat_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO invoices (photo_path, status, source, chat_id, received_at) VALUES (?, 'queued', 'photo', ?, ?)",
        (photo_path, chat_id, clock.stamp()),
    )
    return cur.lastrowid


def process(cfg: Config, invoice_id: int, client: anthropic.Anthropic | None = None) -> str:
    """Read a queued invoice photo. Returns the new status: extracted / queued / failed.

    The database is not held open while Claude reads the photo.
    """
    with db.session(cfg) as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        catalog_text = catalog.catalog_for_prompt(conn)
        supplier_names = [s["name"] for s in catalog.suppliers(conn)]
    if client is None and not cfg.anthropic_key_present:
        _set_status(cfg, invoice_id, "failed", "no_key")
        return "failed"
    try:
        image = (cfg.media_dir / inv["photo_path"]).read_bytes()
        extracted = extract(image, catalog_text, supplier_names, cfg, client)
    except ExtractionOffline as exc:
        _set_status(cfg, invoice_id, "queued", str(exc)[:500])
        return "queued"
    except (ExtractionError, OSError) as exc:
        _set_status(cfg, invoice_id, "failed", str(exc)[:500])
        return "failed"
    with db.session(cfg) as conn:
        ingest(conn, invoice_id, extracted, cfg.invoices.khr_per_usd)
    return "extracted"


def _set_status(cfg: Config, invoice_id: int, status: str, error: str | None) -> None:
    with db.session(cfg) as conn:
        conn.execute("UPDATE invoices SET status = ?, error = ? WHERE id = ?", (status, error, invoice_id))


def ingest(conn: sqlite3.Connection, invoice_id: int, x: ExtractedInvoice, khr_per_usd: float = 4100) -> None:
    """Store extracted lines, each matched to a product where we can."""
    valid_ids = {r["id"] for r in conn.execute("SELECT id FROM products WHERE active = 1")}
    to_usd = 1 / khr_per_usd if x.currency == "KHR" else 1.0
    matched_suppliers: list[int] = []
    conn.execute("DELETE FROM invoice_lines WHERE invoice_id = ?", (invoice_id,))
    for line in x.lines:
        product_id = catalog.resolve_alias(conn, line.text_as_written)
        matched_by = "alias" if product_id else None
        if product_id is None and line.product_id in valid_ids:
            product_id, matched_by = line.product_id, "model"
        units = units_total(conn, product_id, line.quantity, line.unit, line.units_per_case)
        conn.execute(
            """
            INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total,
                                       unit_cost, expiry_date, is_free, matched_by,
                                       name_suggested, name_suggested_km, units_per_case, unit_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (invoice_id, product_id, line.text_as_written, line.quantity, line.unit, units,
             round(line.unit_cost * to_usd, 4) if line.unit_cost is not None else None,
             line.expiry_date, int(line.is_free), matched_by,
             (line.product_name or "").strip() or None, (line.product_name_km or "").strip() or None,
             line.units_per_case if line.units_per_case and line.units_per_case > 0 else None,
             (line.unit_name or "").strip().lower() or None),
        )
        if product_id:
            sup = conn.execute("SELECT supplier_id FROM products WHERE id = ?", (product_id,)).fetchone()
            if sup and sup["supplier_id"]:
                matched_suppliers.append(sup["supplier_id"])
    supplier_id = match_supplier(conn, x.supplier_name, matched_suppliers)
    conn.execute(
        "UPDATE invoices SET status = 'extracted', supplier_id = ?, supplier_name_raw = ?, extracted_json = ?, "
        "error = NULL WHERE id = ?",
        (supplier_id, x.supplier_name, x.model_dump_json(), invoice_id),
    )


def match_supplier(conn: sqlite3.Connection, name: str | None, product_suppliers: list[int]) -> int | None:
    """The supplier most of the lines belong to, unless the printed name is a near-exact match.

    Names are a weak signal: "Snack agent" and "Coca-Cola agent" share half their
    letters. The products on the invoice rarely lie about who delivered them.
    """
    by_products = max(set(product_suppliers), key=product_suppliers.count) if product_suppliers else None
    by_name, score = None, 0.0
    if name:
        for s in catalog.suppliers(conn):
            ratio = difflib.SequenceMatcher(None, catalog.normalize(name), catalog.normalize(s["name"])).ratio()
            if ratio > score:
                by_name, score = s["id"], ratio
    if by_products is not None and not (by_name is not None and score >= 0.85):
        return by_products
    return by_name if score >= 0.6 else by_products


def units_total(conn: sqlite3.Connection, product_id: int | None, qty: float, unit: str,
                units_per: int | None) -> int | None:
    """Invoice quantity in base units: "2 cases" of 24-can Coke is 48."""
    if product_id is None:
        return None
    p = conn.execute("SELECT case_size FROM products WHERE id = ?", (product_id,)).fetchone()
    if unit == "case":
        per = units_per or p["case_size"]
    elif unit == "pack":
        biggest = conn.execute(
            "SELECT MAX(units) FROM product_packs WHERE product_id = ? AND units > 1", (product_id,)
        ).fetchone()[0]
        per = units_per or biggest or 1
    else:
        per = 1
    return int(round(qty * per))


def lines(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT l.*, p.name_en, p.name_km, p.case_size FROM invoice_lines l
        LEFT JOIN products p ON p.id = l.product_id WHERE l.invoice_id = ? ORDER BY l.id
        """,
        (invoice_id,),
    ).fetchall()


def unresolved(conn: sqlite3.Connection, invoice_id: int) -> list[sqlite3.Row]:
    return [l for l in lines(conn, invoice_id) if l["product_id"] is None and not l["skipped"]]


def set_line_product(conn: sqlite3.Connection, line_id: int, product_id: int) -> None:
    l = conn.execute("SELECT * FROM invoice_lines WHERE id = ?", (line_id,)).fetchone()
    # Recomputed from the newly chosen product's own case size.
    conn.execute(
        "UPDATE invoice_lines SET product_id = ?, units_total = ?, skipped = 0, matched_by = 'owner' WHERE id = ?",
        (product_id, units_total(conn, product_id, l["qty"], l["unit"], None), line_id),
    )


def needs_case_size(line: sqlite3.Row) -> bool:
    """A case or pack line whose invoice doesn't say how many are in one: ask before making the product."""
    return line["unit"] in ("case", "pack") and not line["units_per_case"]


def new_product_for_line(conn: sqlite3.Connection, line_id: int, name: str | None = None,
                         units_per_case: int | None = None) -> int:
    """Make the product an unmatched line describes, with its supplier, case size and price. Returns its id.

    A name that is already a product (or one of its other names) is matched to it instead.
    """
    line = conn.execute("SELECT * FROM invoice_lines WHERE id = ?", (line_id,)).fetchone()
    if line is None:
        raise KeyError(line_id)
    inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (line["invoice_id"],)).fetchone()
    name_en = " ".join((name or line["name_suggested"] or line["text_as_written"] or "").split())[:80]
    if not name_en:
        raise ValueError("Give the product a name.")
    per = units_per_case or line["units_per_case"]
    if line["unit"] in ("case", "pack") and not per:
        raise ValueError(f"How many are in one {line['unit']} of {name_en}?")
    existing = catalog.resolve_alias(conn, name_en)
    if existing is not None:
        set_line_product(conn, line_id, existing)
        return existing
    per_invoice_unit = per if line["unit"] in ("case", "pack") else 1
    unit_cost = (round(line["unit_cost"] / per_invoice_unit, 4)
                 if line["unit_cost"] is not None and not line["is_free"] else 0.0)
    product_id = catalog.add_product(
        conn, sku=catalog.unique_sku(conn, name_en), name_en=name_en,
        name_km=" ".join((line["name_suggested_km"] or "").split()) or name_en,
        unit=line["unit_name"] or "piece", case_size=per if line["unit"] == "case" else 1,
        supplier_id=inv["supplier_id"], unit_cost=unit_cost, origin="invoice")
    if line["unit"] == "pack":
        catalog.add_pack(conn, product_id, "pack", per)
    catalog.add_alias(conn, product_id, line["text_as_written"], "invoice")
    conn.execute(
        "UPDATE invoice_lines SET product_id = ?, units_total = ?, units_per_case = ?, skipped = 0, matched_by = 'new' "
        "WHERE id = ?",
        (product_id, units_total(conn, product_id, line["qty"], line["unit"], per), per, line_id),
    )
    return product_id


def new_products_for_all(conn: sqlite3.Connection, invoice_id: int) -> tuple[int, int]:
    """Every open line that says enough becomes a product. Returns (made, still open: they need a case size)."""
    made = left = 0
    for line in unresolved(conn, invoice_id):
        if needs_case_size(line):
            left += 1
            continue
        new_product_for_line(conn, line["id"])
        made += 1
    return made, left


def add_supplier_from_invoice(conn: sqlite3.Connection, invoice_id: int, name: str, visit_weekday: int) -> int:
    """The supplier printed on the invoice, added to the list; its new products join its order list."""
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("Give the supplier a name.")
    row = conn.execute("SELECT id FROM suppliers WHERE name = ?", (name,)).fetchone()
    supplier_id = row["id"] if row else catalog.add_supplier(conn, name, visit_weekday)
    set_supplier(conn, invoice_id, supplier_id)
    conn.execute(
        "UPDATE products SET supplier_id = ? WHERE supplier_id IS NULL AND id IN "
        "(SELECT product_id FROM invoice_lines WHERE invoice_id = ? AND matched_by = 'new')",
        (supplier_id, invoice_id),
    )
    return supplier_id


def skip_line(conn: sqlite3.Connection, line_id: int) -> None:
    conn.execute("UPDATE invoice_lines SET skipped = 1 WHERE id = ?", (line_id,))


def set_supplier(conn: sqlite3.Connection, invoice_id: int, supplier_id: int) -> None:
    conn.execute("UPDATE invoices SET supplier_id = ? WHERE id = ?", (supplier_id, invoice_id))
    # Products this invoice just added belong to whoever delivered it.
    conn.execute(
        "UPDATE products SET supplier_id = ? WHERE supplier_id IS NULL AND id IN "
        "(SELECT product_id FROM invoice_lines WHERE invoice_id = ? AND matched_by = 'new')",
        (supplier_id, invoice_id),
    )


def confirm(conn: sqlite3.Connection, invoice_id: int, by: str = "owner") -> tuple[int, int]:
    """Post the invoice to the ledger. Returns (base units added, lines posted)."""
    inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv["status"] == "confirmed":
        return 0, 0
    if unresolved(conn, invoice_id):
        raise ValueError("every line needs a product or a skip before confirming")
    added = posted = 0
    for l in lines(conn, invoice_id):
        if l["skipped"] or not l["units_total"]:
            continue
        _post_line(conn, l)
        # Teach the alias table: next time this spelling maps without asking.
        catalog.add_alias(conn, l["product_id"], l["text_as_written"], "invoice")
        added += l["units_total"]
        posted += 1
    conn.execute("UPDATE invoices SET status = 'confirmed', confirmed_at = ? WHERE id = ?", (clock.stamp(), invoice_id))
    return added, posted


def products_only(conn: sqlite3.Connection, invoice_id: int) -> int:
    """An old invoice, read to set the shop up: keep its products, names and prices, add no stock.

    Returns how many lines were used.
    """
    inv = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv["status"] in ("confirmed", "products"):
        return 0
    if unresolved(conn, invoice_id):
        raise ValueError("every line needs a product or a skip first")
    used = 0
    for l in lines(conn, invoice_id):
        if l["skipped"] or not l["product_id"]:
            continue
        catalog.add_alias(conn, l["product_id"], l["text_as_written"], "invoice")
        if l["unit_cost"] is not None and not l["is_free"] and l["qty"] and l["units_total"]:
            conn.execute("UPDATE products SET unit_cost = ? WHERE id = ?",
                         (round(l["unit_cost"] * l["qty"] / l["units_total"], 4), l["product_id"]))
        used += 1
    conn.execute("UPDATE invoices SET status = 'products', confirmed_at = ? WHERE id = ?", (clock.stamp(), invoice_id))
    return used


def _post_line(conn: sqlite3.Connection, l: sqlite3.Row) -> None:
    per_unit_cost = None
    if l["unit_cost"] is not None and not l["is_free"] and l["qty"]:
        per_unit_cost = round(l["unit_cost"] * l["qty"] / l["units_total"], 4)
        conn.execute("UPDATE products SET unit_cost = ? WHERE id = ?", (per_unit_cost, l["product_id"]))
    stock.record(conn, l["product_id"], l["units_total"], "freebie" if l["is_free"] else "delivery",
                 ref_type="invoice_line", ref_id=l["id"], unit_cost=per_unit_cost, expiry_date=l["expiry_date"])


def reject(conn: sqlite3.Connection, invoice_id: int) -> None:
    conn.execute("UPDATE invoices SET status = 'rejected' WHERE id = ? AND status != 'confirmed'", (invoice_id,))


def add_bonus(conn: sqlite3.Connection, invoice_id: int, product_id: int, units: int) -> None:
    """Supplier freebies ("buy 10 get 1") that are not printed on the invoice."""
    cur = conn.execute(
        """
        INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total, is_free, matched_by)
        VALUES (?, ?, 'bonus', ?, 'piece', ?, 1, 'owner')
        """,
        (invoice_id, product_id, units, units),
    )
    status = conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()["status"]
    if status == "confirmed":
        _post_line(conn, conn.execute("SELECT * FROM invoice_lines WHERE id = ?", (cur.lastrowid,)).fetchone())


def create_manual(conn: sqlite3.Connection, supplier_id: int | None, entries: list[dict]) -> int:
    """An invoice typed on the dashboard: [{product_id, qty, unit, unit_cost, expiry_date, is_free}]."""
    cur = conn.execute(
        "INSERT INTO invoices (supplier_id, status, source, received_at) VALUES (?, 'extracted', 'manual', ?)",
        (supplier_id, clock.stamp()),
    )
    invoice_id = cur.lastrowid
    for e in entries:
        p = catalog.product(conn, e["product_id"])
        conn.execute(
            """
            INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total,
                                       unit_cost, expiry_date, is_free, matched_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual')
            """,
            (invoice_id, p["id"], p["name_en"], e["qty"], e["unit"],
             units_total(conn, p["id"], e["qty"], e["unit"], None), e.get("unit_cost"),
             e.get("expiry_date") or None, int(bool(e.get("is_free")))),
        )
    return invoice_id


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT i.*, s.name AS supplier,
               (SELECT COUNT(*) FROM invoice_lines l WHERE l.invoice_id = i.id) AS line_count,
               (SELECT COALESCE(SUM(units_total), 0) FROM invoice_lines l
                 WHERE l.invoice_id = i.id AND l.skipped = 0) AS units
        FROM invoices i LEFT JOIN suppliers s ON s.id = i.supplier_id
        ORDER BY i.received_at DESC, i.id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


def queued(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM invoices WHERE status = 'queued' ORDER BY id").fetchall()


def extracted_json(row: sqlite3.Row) -> dict | None:
    return json.loads(row["extracted_json"]) if row["extracted_json"] else None
