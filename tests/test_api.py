"""The web app end to end: pages render, a photographed sale lands in the review queue,
settling it on the dashboard moves stock."""

import pytest
from fastapi.testclient import TestClient

from luydek import db
from luydek.services import catalog, stock, transactions
from luydek.web.server import create_app
import numpy as np

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
    assert "Check 1 sale" in client.get("/").text                     # Home: one thing to do
    page = client.get("/sales").text
    assert "Check 1 item" in page and "All correct (<span data-all-n>1</span>)" in page
    assert page.index('id="check"') < page.index("Sales today")      # waiting items come first

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
    assert "Out of stock" in html and "Dumex Dugro baby formula 400g is out" in html
    assert "Coming up" in html


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


def calibrate(client):
    assert client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")}).status_code == 200


def test_marked_mat_limits_what_the_camera_counts(client, conn):
    calibrate(client)
    enroll(client, conn, "coke-330")
    enroll(client, conn, "dumex-400")
    assert client.post("/api/roi", data={"x0": 0.5, "y0": 0, "x1": 1, "y1": 1}).json()["roi"] == [0.5, 0, 1, 1]
    assert client.get("/api/state").json()["roi"] == [0.5, 0, 1, 1]
    frame = mat_frame()
    paint(frame, "coke-330", 40, 60)
    paint(frame, "dumex-400", 420, 200)
    dets = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}).json()["detections"]
    assert [d["label"] for d in dets] == [catalog.pack_label(catalog.pack(conn, pack_of(conn, "dumex-400")), "en")]

    assert client.post("/api/roi", data={"x0": 0.1, "y0": 0.1, "x1": 0.12, "y1": 0.9}).status_code == 400
    assert client.post("/api/roi", data={"x0": 0.1}).status_code == 400
    assert client.post("/api/roi", data={"clear": "true"}).json()["roi"] is None


def test_capture_after_the_camera_moved_asks_for_recalibration(client, conn):
    calibrate(client)
    dark = np.clip(mat_frame().astype(np.int16) - 80, 0, 255).astype(np.uint8)
    r = client.post("/api/capture", files={"image": ("f.jpg", jpeg(dark), "image/jpeg")})
    assert r.status_code == 409 and r.json()["scene_changed"] is True
    assert "Calibrate" in r.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0   # nothing recorded
    # Enrollment refuses it too, rather than learning a photo of the changed, empty scene.
    photos = conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0]
    r = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(dark), "image/jpeg")},
                    data={"pack_id": pack_of(conn, "coke-330"), "use_reference": "true"})
    assert r.status_code == 400 and "Nothing stood out" in r.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0] == photos
    # Recalibrated in the new light, sales are recorded again.
    assert client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(dark), "image/jpeg")}).status_code == 200
    frame = dark.copy()
    paint(frame, "coke-330", 200, 150)
    r = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")})
    assert r.status_code == 200 and len(r.json()["detections"]) == 1


def test_enrolling_an_empty_mat_is_refused(client, conn):
    calibrate(client)
    r = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(mat_frame()), "image/jpeg")},
                    data={"pack_id": pack_of(conn, "coke-330"), "use_reference": "true"})
    assert r.status_code == 400 and "Nothing found on the mat" in r.json()["detail"]
    assert conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0] == 0


def test_photos_from_another_phone_are_cut_out_from_their_own_background(client, conn):
    """Not the counter camera's view (another phone, a table, zoomed in): the item still counts."""
    calibrate(client)
    table = np.zeros_like(mat_frame())
    table[:] = (60, 90, 140)                       # a wooden table, nothing like the mat
    paint(table, "coke-330", 260, 170, w=120, h=160)
    r = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(table), "image/jpeg")},
                    data={"pack_id": pack_of(conn, "coke-330"), "use_reference": "true"})
    assert r.status_code == 200 and r.json()["own_background"] is True
    x, y, w, h = r.json()["bbox"]
    assert 200 < x + w / 2 < 440 and w < 320        # the can, not the whole photo
    r = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(table), "image/jpeg")},
                    data={"pack_id": pack_of(conn, "coke-330"), "use_reference": "true", "burst": "true"})
    assert r.json() == {"skipped": "same", "own_background": True}   # hold-to-record still skips repeats


def test_the_counter_shows_and_sounds_each_item_and_the_sale(client, conn, cfg):
    """What the counter page needs for its beeps and its Sold card: each item's photo and product
    as it is seen, and each sold line's photo when the sale closes."""
    page = client.get("/counter").text
    assert 'id="btn-sound"' in page and 'id="sold"' in page and 'id="seen"' in page
    client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")})
    enroll(client, conn, "coke-330")
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    seen = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}).json()
    [d] = seen["detections"]
    assert d["pack_id"] == pack_of(conn, "coke-330") and (cfg.media_dir / d["crop"].removeprefix("/media/")).exists()
    sold = client.post(f"/api/transactions/{seen['txn_id']}/close").json()
    [line] = sold["lines"]
    assert line["label"] == "Coca-Cola can 330ml" and (cfg.media_dir / line["photo"].removeprefix("/media/")).exists()
