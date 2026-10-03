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


def test_supplier_comes_from_the_products_unless_the_name_is_near_exact(conn):
    snack = conn.execute("SELECT id FROM suppliers WHERE name = 'Snack & drink distributor'").fetchone()[0]
    coca = conn.execute("SELECT id FROM suppliers WHERE name = 'Coca-Cola agent'").fetchone()[0]
    # "Snack agent" looks like "Coca-Cola agent" to a string matcher; the tea says otherwise.
    assert invoices.match_supplier(conn, "Snack agent", [snack, snack]) == snack
    assert invoices.match_supplier(conn, "COCA-COLA AGENT", [snack]) == coca
    assert invoices.match_supplier(conn, "Coca-Cola agnt", []) == coca
    assert invoices.match_supplier(conn, None, []) is None


# --- new products from invoices -----------------------------------------------------------------



def new_line(text, qty, unit="case", name=None, name_km=None, per=None, cost=None, unit_name=None, free=False):
    return ExtractedLine(text_as_written=text, product_id=None, quantity=qty, unit=unit, units_per_case=per,
                         unit_cost=cost, expiry_date=None, is_free=free, product_name=name,
                         product_name_km=name_km, unit_name=unit_name)


def read_invoice(conn, supplier="Snack & drink distributor", lines=()):
    invoice_id = invoices.create_from_photo(conn, "invoices/x.jpg")
    invoices.ingest(conn, invoice_id, ExtractedInvoice(supplier_name=supplier, invoice_date=None, currency="USD",
                                                       lines=list(lines)))
    conn.commit()
    return invoice_id


def test_an_invoice_line_becomes_a_product_with_its_supplier_case_and_price(conn):
    snacks = conn.execute("SELECT id FROM suppliers WHERE name = 'Snack & drink distributor'").fetchone()[0]
    invoice_id = read_invoice(conn, lines=[
        new_line("Pocari 500 x24", 2, name="Pocari Sweat 500ml", per=24, cost=12.0, unit_name="bottle"),
    ])
    assert invoices.lines(conn, invoice_id)[0]["name_suggested"] == "Pocari Sweat 500ml"
    line_id = invoices.lines(conn, invoice_id)[0]["id"]
    new = invoices.new_product_for_line(conn, line_id)
    p = conn.execute("SELECT * FROM products WHERE id = ?", (new,)).fetchone()
    assert (p["name_en"], p["origin"], p["supplier_id"], p["case_size"], p["unit"]) == \
        ("Pocari Sweat 500ml", "invoice", snacks, 24, "bottle")
    assert p["unit_cost"] == pytest.approx(0.5)                                     # $12 a case of 24
    line = invoices.lines(conn, invoice_id)[0]
    assert (line["units_total"], line["matched_by"]) == (48, "new")
    assert catalog.resolve_alias(conn, "Pocari 500 x24") == new                    # known next time
    invoices.confirm(conn, invoice_id)
    assert stock.on_hand(conn)[new] == 48
    assert next(r for r in stock.stock_table(conn) if r["id"] == new)["status"] != "new"  # the delivery counts


def test_a_case_with_no_size_printed_asks_for_it(conn):
    invoice_id = read_invoice(conn, lines=[
        new_line("Mama tom yum 60g", 1, name="Mama tom yum noodles 60g"),              # no size printed
        new_line("Tiger balm 19g", 3, unit="piece", name="Tiger Balm 19g", cost=1.5),
    ])
    assert invoices.new_products_for_all(conn, invoice_id) == (1, 1)
    open_line = invoices.unresolved(conn, invoice_id)[0]
    assert invoices.needs_case_size(open_line)
    with pytest.raises(ValueError, match="How many are in one case"):
        invoices.new_product_for_line(conn, open_line["id"])
    new = invoices.new_product_for_line(conn, open_line["id"], units_per_case=30)
    assert conn.execute("SELECT case_size FROM products WHERE id = ?", (new,)).fetchone()[0] == 30
    assert not invoices.unresolved(conn, invoice_id)


def test_old_invoices_add_products_but_no_stock(conn):
    invoice_id = read_invoice(conn, supplier="Chip Mong Trading", lines=[
        new_line("Pocari 500 x24", 2, name="Pocari Sweat 500ml", per=24, cost=12.0),
        new_line("CC can 330 x24", 1, name="Coca-Cola can 330ml", per=24, cost=9.12),  # a spelling not seen yet
    ])
    assert conn.execute("SELECT supplier_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()[0] is None
    invoices.new_products_for_all(conn, invoice_id)
    lines = invoices.lines(conn, invoice_id)
    assert lines[1]["product_id"] == pid(conn, "coke-330")          # its clean name is a product already: no twin
    supplier_id = invoices.add_supplier_from_invoice(conn, invoice_id, "Chip Mong Trading", 2)
    pocari = lines[0]["product_id"]
    assert conn.execute("SELECT supplier_id FROM products WHERE id = ?", (pocari,)).fetchone()[0] == supplier_id
    assert invoices.products_only(conn, invoice_id) == 2
    assert pocari not in stock.on_hand(conn)                        # no stock moved
    assert conn.execute("SELECT status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()[0] == "products"
    assert conn.execute("SELECT unit_cost FROM products WHERE id = ?", (pid(conn, "coke-330"),)).fetchone()[0] == \
        pytest.approx(0.38)


def test_the_reader_is_asked_for_names_and_case_sizes(conn, cfg):
    client = FakeClient(ExtractedInvoice(supplier_name="X", invoice_date=None, currency="USD", lines=[]))
    invoice_id = queued_invoice(conn, cfg)
    invoices.process(cfg, invoice_id, client)
    text = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "product_name" in text and "units_per_case matters most" in text
    schema = ExtractedLine.model_json_schema()["properties"]
    assert {"product_name", "product_name_km", "unit_name"} <= set(schema)


def test_the_delivery_page_adds_new_products(conn, cfg, stub_encoder, monkeypatch):
    from fastapi.testclient import TestClient

    from luydek.web.server import create_app
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    invoice_id = read_invoice(conn, supplier="Chip Mong Trading", lines=[
        new_line("Pocari 500 x24", 2, name="Pocari Sweat 500ml", per=24, cost=12.0),
        new_line("Oishi 380 x24", 1, name="Oishi green tea honey lemon 380ml", per=24, cost=11.0),
        new_line("Mama 60g", 1, name="Mama tom yum noodles 60g"),
    ])
    with TestClient(create_app(cfg)) as client:
        page = client.get(f"/invoices/{invoice_id}").text
        assert "Add all 2 as new products" in page and "How many in one case?" in page
        assert 'value="Chip Mong Trading"' in page and "Their agent comes on" in page
        assert "Only add the products" in page
        client.post(f"/invoices/{invoice_id}/new-products")
        mama = invoices.unresolved(db.connect(cfg.db_path), invoice_id)[0]["id"]
        client.post(f"/invoices/{invoice_id}/lines/{mama}/new", data={"name": "Mama tom yum 60g", "units_per_case": "30"})
        client.post(f"/invoices/{invoice_id}/new-supplier", data={"name": "Chip Mong Trading", "visit_weekday": "2"})
        r = client.post(f"/invoices/{invoice_id}/products-only", follow_redirects=False)
        assert "No stock was added" in r.headers["location"].replace("%20", " ")
        assert "Products added" in client.get("/invoices").text
    check = db.connect(cfg.db_path)
    names = {r["name_en"] for r in check.execute("SELECT name_en FROM products WHERE origin = 'invoice'")}
    assert names == {"Pocari Sweat 500ml", "Oishi green tea honey lemon 380ml", "Mama tom yum 60g"}


def test_telegram_adds_an_invoice_line_as_a_new_product(conn, cfg):
    from luydek.bot import keyboards
    from tests.test_bot import FakeBot, register, tap
    register(conn)
    invoice_id = read_invoice(conn, lines=[new_line("Pocari 500 x24", 2, name="Pocari Sweat 500ml", per=24)])
    line = invoices.lines(conn, invoice_id)[0]
    buttons = [b for row in keyboards.line_keyboard(conn, line, "en").inline_keyboard for b in row]
    assert buttons[0].text == "➕ Add Pocari Sweat 500ml" and buttons[0].callback_data == f"il:{line['id']}:n"
    tap(cfg, FakeBot(), f"il:{line['id']}:n")
    assert invoices.lines(db.connect(cfg.db_path), invoice_id)[0]["matched_by"] == "new"
