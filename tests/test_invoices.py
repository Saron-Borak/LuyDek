import io
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from PIL import Image

from luydek import db
from luydek.services import catalog, invoices, stock
from luydek.services.invoices import ExtractedInvoice, ExtractedLine
from tests.conftest import pid


def line(text, qty, unit="case", product_id=None, cost=None, expiry=None, free=False, per=None):
    return ExtractedLine(text_as_written=text, product_id=product_id, quantity=qty, unit=unit, units_per_case=per,
                         unit_cost=cost, expiry_date=expiry, is_free=free)


class FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns a canned parse."""

    def __init__(self, result=None, stop_reason="end_turn", error=None):
        self.calls = []
        self.result, self.stop_reason, self.error = result, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=self.result)


def photo_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), "white").save(buf, "JPEG")
    return buf.getvalue()


def queued_invoice(conn, cfg):
    (cfg.media_dir / "invoices").mkdir(parents=True, exist_ok=True)
    (cfg.media_dir / "invoices" / "a.jpg").write_bytes(photo_bytes())
    invoice_id = invoices.create_from_photo(conn, "invoices/a.jpg", chat_id=42)
    conn.commit()
    return invoice_id


def test_process_sends_image_and_catalog_and_stores_lines(conn, cfg):
    coke = pid(conn, "coke-330")
    client = FakeClient(ExtractedInvoice(
        supplier_name="COCA-COLA AGENT", invoice_date="2026-09-27", currency="USD",
        lines=[line("Coca 330", 2, cost=9.12, expiry="2027-06-01"), line("Coca 330", 1, free=True)],
    ))
    invoice_id = queued_invoice(conn, cfg)
    assert invoices.process(cfg, invoice_id, client) == "extracted"

    call = client.calls[0]
    assert call["model"] == cfg.invoices.model
    assert call["output_format"] is ExtractedInvoice
    assert call["fallbacks"] == "default"
    image, text = call["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["media_type"] == "image/jpeg"
    assert "Coca-Cola can 330ml" in text["text"]              # the catalog travels with the photo
    # The 3000 px photo was downscaled before upload.
    import base64
    sent = Image.open(io.BytesIO(base64.b64decode(image["source"]["data"])))
    assert max(sent.size) <= cfg.invoices.max_long_edge

    conn2 = db.connect(cfg.db_path)
    inv = conn2.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    assert inv["status"] == "extracted" and inv["supplier_id"] is not None
    rows = invoices.lines(conn2, invoice_id)
    assert [(r["product_id"], r["units_total"], r["matched_by"]) for r in rows] == [(coke, 48, "alias"), (coke, 24, "alias")]


def test_alias_beats_the_model_and_unknown_names_wait_for_the_owner(conn):
    tea, oishi = pid(conn, "ichitan-420"), pid(conn, "oishi-380")
    invoice_id = invoices.create_from_photo(conn, "x.jpg")
    invoices.ingest(conn, invoice_id, ExtractedInvoice(supplier_name=None, invoice_date=None, currency=None, lines=[
        line("ichitan", 1, product_id=oishi),       # the owner's alias wins over a wrong model guess
        line("Oishi 380ml Honey", 1, product_id=oishi),
        line("unknown thing", 3, product_id=999),   # an id not in the catalog is ignored
    ]))
    rows = invoices.lines(conn, invoice_id)
    assert [(r["product_id"], r["matched_by"]) for r in rows] == [(tea, "alias"), (oishi, "model"), (None, None)]
    # Supplier inferred from the products when the name is missing.
    assert conn.execute("SELECT supplier_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()[0] is not None
    with pytest.raises(ValueError):
        invoices.confirm(conn, invoice_id)


def test_units_for_cases_packs_and_pieces(conn):
    coke = pid(conn, "coke-330")
    assert invoices.units_total(conn, coke, 2, "case", None) == 48
    assert invoices.units_total(conn, coke, 2, "case", 12) == 24
    assert invoices.units_total(conn, coke, 3, "pack", None) == 18   # the enrolled 6-pack
    assert invoices.units_total(conn, coke, 5, "piece", None) == 5


def test_riel_prices_are_converted_to_dollars(conn):
    invoice_id = invoices.create_from_photo(conn, "x.jpg")
    invoices.ingest(conn, invoice_id, ExtractedInvoice(supplier_name=None, invoice_date=None, currency="KHR",
                                                       lines=[line("coke", 1, cost=41000)]), khr_per_usd=4100)
    assert invoices.lines(conn, invoice_id)[0]["unit_cost"] == pytest.approx(10.0)


def test_confirm_posts_deliveries_freebies_costs_and_learns_aliases(conn):
    tea = pid(conn, "ichitan-420")
    invoice_id = invoices.create_from_photo(conn, "x.jpg")
    invoices.ingest(conn, invoice_id, ExtractedInvoice(supplier_name=None, invoice_date=None, currency="USD", lines=[
        line("ICHT GRN 420", 1, cost=12.0, expiry="2027-03-01"),
        line("ICHT GRN 420 FOC", 2, unit="piece", free=True),
    ]))
    for r in invoices.unresolved(conn, invoice_id):
        invoices.set_line_product(conn, r["id"], tea)
    units, posted = invoices.confirm(conn, invoice_id)
    assert (units, posted) == (26, 2)
    moves = conn.execute("SELECT type, qty, unit_cost, expiry_date FROM stock_movements ORDER BY id").fetchall()
    assert [tuple(m) for m in moves] == [("delivery", 24, 0.5, "2027-03-01"), ("freebie", 2, None, None)]
    assert catalog.product(conn, tea)["unit_cost"] == pytest.approx(0.5)
    # Next time this spelling maps straight away.
    assert catalog.resolve_alias(conn, "icht grn 420") == tea
    # Confirming twice never double-counts.
    assert invoices.confirm(conn, invoice_id) == (0, 0)


def test_bonus_after_confirmation_posts_immediately(conn):
    coke = pid(conn, "coke-330")
    invoice_id = invoices.create_manual(conn, None, [{"product_id": coke, "qty": 1, "unit": "case"}])
    invoices.confirm(conn, invoice_id)
    invoices.add_bonus(conn, invoice_id, coke, 2)
    assert stock.on_hand(conn)[coke] == 26


def test_connection_errors_queue_the_invoice_for_later(conn, cfg):
    invoice_id = queued_invoice(conn, cfg)
    err = anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
    assert invoices.process(cfg, invoice_id, FakeClient(error=err)) == "queued"
    assert invoices.process(cfg, invoice_id, FakeClient(stop_reason="refusal")) == "failed"


def test_no_api_key_fails_cleanly(conn, cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    invoice_id = queued_invoice(conn, cfg)
    assert invoices.process(cfg, invoice_id) == "failed"
