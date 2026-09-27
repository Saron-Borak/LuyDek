"""Stock-in from supplier invoices.

A photo of the paper invoice goes to Claude with the store's catalog; Claude
returns structured lines (item as written, quantity, unit, cost, expiry, free
or not) and its best product match for each. The alias table then overrides
the model wherever the owner has already taught us a name, and the owner
confirms the whole invoice with one tap before any stock moves.
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
- If a number is hard to read, give your best reading rather than skipping the line."""


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
                                       unit_cost, expiry_date, is_free, matched_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (invoice_id, product_id, line.text_as_written, line.quantity, line.unit, units,
             round(line.unit_cost * to_usd, 4) if line.unit_cost is not None else None,
             line.expiry_date, int(line.is_free), matched_by),
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


def skip_line(conn: sqlite3.Connection, line_id: int) -> None:
    conn.execute("UPDATE invoice_lines SET skipped = 1 WHERE id = ?", (line_id,))


def set_supplier(conn: sqlite3.Connection, invoice_id: int, supplier_id: int) -> None:
    conn.execute("UPDATE invoices SET supplier_id = ? WHERE id = ?", (supplier_id, invoice_id))


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
