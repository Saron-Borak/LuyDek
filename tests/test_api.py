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


@pytest.mark.parametrize("path", ["/", "/counter", "/catalog", "/invoices", "/orders", "/orders?lang=km", "/api/state"])
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
    assert "Needs a quick check" in client.get("/").text

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
    assert "No ANTHROPIC_API_KEY" in r.text
