"""The web app end to end: pages render, a photographed sale lands in the review queue,
settling it on the dashboard moves stock."""

import pytest
from fastapi.testclient import TestClient

from luydek import db
from luydek.services import catalog, stock, transactions
from luydek.web.server import create_app
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


def enroll(client, conn, sku, shots=4):
    for i in range(shots):
        frame = mat_frame()
        paint(frame, sku, 100 + 50 * i, 90 + 20 * i)
        r = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(frame), "image/jpeg")},
                        data={"pack_id": pack_of(conn, sku), "use_reference": "true"})
        assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize("path", ["/", "/counter", "/catalog", "/catalog/new", "/catalog/1", "/invoices", "/orders",
                                  "/orders?lang=km", "/api/state", "/catalog/enroll?ignore=phone"])
def test_pages_render(client, path):
    r = client.get(path)
    assert r.status_code == 200, r.text[:500]


def test_enrollment_grows_the_gallery(client, conn):
    assert enroll(client, conn, "coke-330")["photos"] == 4
    assert client.get("/catalog/enroll", params={"pack_id": pack_of(conn, "coke-330")}).status_code == 200
    assert client.get("/api/state").json()["gallery"] == 4


def test_photo_sale_to_review_queue_to_stock(client, conn, cfg):
    client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")})
    for sku in ("coke-330", "sprite-1500", "dumex-400"):
        enroll(client, conn, sku)

    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    paint(frame, "dumex-400", 380, 220)
    r = client.post("/api/sale-photo", files={"image": ("sale.jpg", jpeg(frame), "image/jpeg")},
                    data={"use_reference": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["detections"]) == 2
    assert body["summary"]["items"] == 2

    # Force one item to need the owner, then settle it from the dashboard queue.
    check = db.connect(cfg.db_path)
    item = check.execute("SELECT id FROM transaction_items WHERE txn_id = ? ORDER BY id LIMIT 1",
                         (body["txn_id"],)).fetchone()["id"]
    check.execute("UPDATE transaction_items SET status = 'pending', tier = 'medium' WHERE id = ?", (item,))
    check.execute("DELETE FROM stock_movements WHERE ref_type = 'transaction_item' AND ref_id = ?", (item,))
    check.commit()
    assert "Check these items" in client.get("/").text

    before = stock.on_hand(check)
    r = client.post(f"/items/{item}/resolve", data={"action": "yes"}, follow_redirects=False)
    assert r.status_code == 303
    after = stock.on_hand(check)
    moved = {p: after.get(p, 0) - before.get(p, 0) for p in after if after.get(p, 0) != before.get(p, 0)}
    assert list(moved.values()) == [-1]
    assert transactions.item(check, item)["status"] == "confirmed"


def test_live_capture_frames_then_close(client, conn):
    client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")})
    enroll(client, conn, "coke-330")
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    first = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}).json()
    paint(frame, "coke-330", 300, 200)
    second = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")},
                         data={"txn_id": first["txn_id"]}).json()
    assert second["txn_id"] == first["txn_id"] and second["frame_no"] == 2
    summary = client.post(f"/api/transactions/{first['txn_id']}/close").json()
    assert summary["items"] == 2


def test_manual_invoice_to_stock(client, conn, cfg):
    coke = pid(conn, "coke-330")
    r = client.post("/invoices/manual", data={"supplier_id": "1", "product_0": str(coke), "qty_0": "2",
                                               "unit_0": "case", "cost_0": "9.12", "expiry_0": "2027-05-01"},
                    follow_redirects=False)
    assert r.status_code == 303
    invoice_url = r.headers["location"]
    assert client.get(invoice_url).status_code == 200
    client.post(invoice_url + "/confirm")
    check = db.connect(cfg.db_path)
    assert stock.on_hand(check)[coke] == 48
    assert catalog.product(check, coke)["unit_cost"] == pytest.approx(0.38)


def test_opening_count_and_adjustment_forms(client, conn, cfg):
    coke = pid(conn, "coke-330")
    client.post("/catalog/opening-count", data={f"count_{coke}": "30"})
    client.post("/adjust", data={"product_id": coke, "reason": "family_use", "qty": "2"})
    assert stock.on_hand(db.connect(cfg.db_path))[coke] == 28


def test_upload_without_api_key_explains_itself(client, conn, cfg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = client.post("/invoices/upload", files={"image": ("i.jpg", jpeg(mat_frame()), "image/jpeg")})
    assert r.status_code == 200
    assert "needs an ANTHROPIC_API_KEY" in r.text


# --- catalog UX ------------------------------------------------------------------------------

def test_add_product_needs_only_a_name_and_goes_to_photos(client, cfg):
    r = client.post("/catalog/products", data={"name_en": "Fanta orange can 330ml", "name_km": "", "supplier_id": "1",
                                               "unit": "can", "case_size": "24", "unit_cost": "", "shelf_life_days": "",
                                               "barcode": "", "starting_stock": "18", "then": "photos"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/catalog/enroll?pack_id=")
    check = db.connect(cfg.db_path)
    p = check.execute("SELECT * FROM products WHERE name_en = 'Fanta orange can 330ml'").fetchone()
    assert p["sku"] == "fanta-orange-can-330ml" and p["name_km"] == p["name_en"] and p["shelf_life_days"] is None
    assert stock.on_hand(check)[p["id"]] == 18
    # A blank name is refused with a message, not a crash.
    assert "msg=" in client.post("/catalog/products", data={"name_en": " "}, follow_redirects=False).headers["location"]


def test_add_product_with_a_new_supplier(client, cfg):
    client.post("/catalog/products", data={"name_en": "Fresh bread", "supplier_id": "", "new_supplier": "Bakery van",
                                           "new_supplier_day": "5", "then": "another"})
    check = db.connect(cfg.db_path)
    row = check.execute("SELECT s.name, s.visit_weekday FROM products p JOIN suppliers s ON s.id = p.supplier_id "
                        "WHERE p.name_en = 'Fresh bread'").fetchone()
    assert tuple(row) == ("Bakery van", 5)


def test_edit_count_and_archive_a_product(client, conn, cfg):
    coke = pid(conn, "coke-330")
    client.post(f"/catalog/{coke}/edit", data={"name_en": "Coca-Cola can 330ml (red)", "name_km": "", "unit": "can",
                                               "case_size": "24", "supplier_id": "1", "unit_cost": "0.41",
                                               "shelf_life_days": "", "barcode": "8850999"})
    client.post(f"/catalog/{coke}/stock", data={"counted": "40"})
    check = db.connect(cfg.db_path)
    p = catalog.product(check, coke)
    assert (p["name_en"], p["unit_cost"], p["is_critical"]) == ("Coca-Cola can 330ml (red)", 0.41, 0)
    assert catalog.pack_by_barcode(check, "8850999") == pack_of(check, "coke-330")
    assert stock.on_hand(check)[coke] == 40

    client.post(f"/catalog/{coke}/archive")
    assert "Coca-Cola can 330ml (red)" not in client.get("/catalog").text
    assert client.get(f"/catalog/{coke}").status_code == 404


def test_photos_can_be_deleted_one_by_one_or_together(client, conn, cfg):
    enroll(client, conn, "coke-330", shots=4)
    check = db.connect(cfg.db_path)
    ids = [r["id"] for r in check.execute("SELECT id FROM gallery_images ORDER BY id")]
    crop = cfg.media_dir / check.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (ids[0],)).fetchone()[0]
    assert crop.exists()

    assert client.post(f"/api/gallery/{ids[0]}/delete").json() == {"deleted": ids[0]}
    assert not crop.exists()                                    # the file goes too
    assert client.post(f"/api/gallery/{ids[0]}/delete").status_code == 404
    client.post("/catalog/gallery/delete", data={"gid": [str(ids[1]), str(ids[2])]})
    assert [r["id"] for r in check.execute("SELECT id FROM gallery_images")] == [ids[3]]


def test_multipacks_can_be_added_and_removed_until_used(client, conn, cfg):
    coke = pid(conn, "coke-330")
    client.post(f"/catalog/{coke}/packs", data={"label": "12-pack", "units": "12"})
    check = db.connect(cfg.db_path)
    twelve = check.execute("SELECT id FROM product_packs WHERE label = '12-pack'").fetchone()["id"]
    client.post(f"/catalog/packs/{twelve}/delete")
    assert check.execute("SELECT COUNT(*) FROM product_packs WHERE id = ?", (twelve,)).fetchone()[0] == 0
    # The single-can pack can never be removed.
    client.post(f"/catalog/packs/{pack_of(check, 'coke-330')}/delete")
    assert catalog.pack(check, pack_of(check, "coke-330")) is not None


def test_names_can_be_added_and_removed(client, conn, cfg):
    coke = pid(conn, "coke-330")
    client.post(f"/catalog/{coke}/aliases", data={"alias": "Coke red can"})
    check = db.connect(cfg.db_path)
    assert catalog.resolve_alias(check, "coke red can") == coke
    alias_id = check.execute("SELECT id FROM product_aliases WHERE alias_norm = 'coke red can'").fetchone()["id"]
    client.post(f"/catalog/aliases/{alias_id}/delete")
    assert catalog.resolve_alias(check, "coke red can") is None
    catalog_alias = check.execute("SELECT id FROM product_aliases WHERE source = 'catalog' AND product_id = ?",
                                  (coke,)).fetchone()["id"]
    client.post(f"/catalog/aliases/{catalog_alias}/delete")    # the product's own names stay
    assert check.execute("SELECT 1 FROM product_aliases WHERE id = ?", (catalog_alias,)).fetchone()


def test_suppliers_can_be_added_and_edited(client, cfg):
    client.post("/catalog/suppliers", data={"name": "Ice delivery", "visit_weekday": "2"})
    check = db.connect(cfg.db_path)
    sid = check.execute("SELECT id FROM suppliers WHERE name = 'Ice delivery'").fetchone()["id"]
    client.post(f"/catalog/suppliers/{sid}", data={"name": "Ice truck", "visit_weekday": "3", "min_order": "10"})
    row = check.execute("SELECT name, visit_weekday, min_order FROM suppliers WHERE id = ?", (sid,)).fetchone()
    assert tuple(row) == ("Ice truck", 3, 10.0)


def test_dashboard_names_what_needs_attention(client, conn):
    stock.record(conn, pid(conn, "dumex-400"), 5, "opening")
    stock.record(conn, pid(conn, "dumex-400"), -5, "sale")
    conn.commit()
    html = client.get("/").text
    assert "Out of stock" in html and "Dumex Dugro baby formula 400g" in html
    assert "Next agent visits" in html


def test_order_lists_page_has_an_anchor_and_copyable_list_per_agent(client, conn):
    stock.record(conn, pid(conn, "para-500"), 2, "opening")
    conn.commit()
    html = client.get("/orders").text
    for sid in (1, 2, 3, 4):
        assert f'id="supplier-{sid}"' in html
    assert "How the numbers are worked out" in html


def test_deliveries_list_puts_invoices_needing_an_ok_first(client, conn):
    coke = pid(conn, "coke-330")
    client.post("/invoices/manual", data={"supplier_id": "1", "product_0": str(coke), "qty_0": "1", "unit_0": "case",
                                          "cost_0": "", "qty_1": "", "product_1": ""})
    html = client.get("/invoices").text
    waiting = html.split("Waiting for your OK")[1].split("Earlier deliveries")[0]
    assert "Coca-Cola agent" in waiting
