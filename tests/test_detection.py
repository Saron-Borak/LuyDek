"""Detection that never loses a sale: "other" is asked about, set-aside things are logged and
recoverable, items bridged by gap-filling come apart, and anything recorded can be fixed."""

import pytest
from fastapi.testclient import TestClient

from luydek import db
from luydek.recognition import pipeline, segment
from luydek.services import stock, transactions
from luydek.web.server import create_app
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid
from tests.test_onboarding import unknown_sale
from tests.test_recognition import enroll_all

JUNK = (90, 60, 140)  # BGR: something reddish-brown the owner once said wasn't a sale


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


def junk_frame():
    frame = mat_frame()
    frame[150:300, 250:380] = JUNK
    return frame


def taught_junk(conn, cfg):
    rec = enroll_all(conn, cfg, skus=("coke-330",))
    rec.enroll(conn, junk_frame(), None, "other")   # as a "Not a sale" answer would
    return rec


# --- what is asked, what is set aside -----------------------------------------------------------

def test_other_is_asked_about_never_dropped(conn, cfg, stub_encoder):
    rec = taught_junk(conn, cfg)
    [d] = rec.recognize(conn, junk_frame(), "o1")
    assert d.ignored is None                                  # it could be a product: never lost
    assert (d.record.tier, d.record.hint) == ("low", "junk")
    assert d.record.pack_id == pack_of(conn, "coke-330")      # the closest product is offered


def test_a_repeat_patch_of_background_is_set_aside(conn, cfg, stub_encoder, monkeypatch):
    rec = taught_junk(conn, cfg)
    monkeypatch.setattr(rec, "_background_likeness", lambda *a: [0.96])  # it looks like the mat itself
    [d] = rec.recognize(conn, junk_frame(), "o2")
    assert d.ignored == "background"
    monkeypatch.setattr(rec, "_background_likeness", lambda *a: [0.5])   # something put down: ask
    [d] = rec.recognize(conn, junk_frame(), "o3")
    assert d.ignored is None


def test_the_mat_itself_is_never_recorded_automatically(conn, cfg, stub_encoder, monkeypatch):
    rec = enroll_all(conn, cfg, skus=("coke-330",))
    frame = mat_frame()
    paint(frame, "coke-330", 100, 100, 90 + 5, 130 - 4)       # the pose of one enrollment photo
    [d] = rec.recognize(conn, frame, "m1")
    assert d.record.tier == "high"
    monkeypatch.setattr(rec, "_background_likeness", lambda *a: [0.93])
    [d] = rec.recognize(conn, frame, "m2")
    assert d.record.tier == "medium" and d.record.hint == "background"


def test_items_a_finger_apart_are_not_one(conn, cfg, stub_encoder):
    rec = enroll_all(conn, cfg, skus=("coke-330", "dumex-400"))
    frame = mat_frame()
    paint(frame, "coke-330", 150, 150)
    paint(frame, "dumex-400", 252, 150)                        # a 12px gap: the gap-filling joins them
    [blob] = segment.segment(frame, mat_frame(), cfg.recognition.segment)
    assert len(blob.parts) == 2
    found = sorted(d.record.pack_id for d in rec.recognize(conn, frame, "s1"))
    assert found == sorted([pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")])


# --- nothing set aside is lost ------------------------------------------------------------------

def test_set_aside_things_are_logged_once_and_can_be_a_sale(client, conn, cfg):
    rec = enroll_all(conn, cfg, skus=("coke-330",))
    for i in range(3):
        phone = mat_frame()
        phone[100 + i * 10:260, 200:290] = (20, 20, 20)
        rec.enroll(conn, phone, None, "phone")
    conn.commit()
    frame = mat_frame()
    paint(frame, "coke-330", 60, 80)
    frame[200:360, 400:490] = (20, 20, 20)
    r = client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}).json()
    assert r["ms"] >= 0 and [d["ignored"] for d in r["detections"] if d["ignored"]] == ["phone"]
    client.post("/api/capture", files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}, data={"txn_id": r["txn_id"]})
    client.post(f"/api/transactions/{r['txn_id']}/close")
    check = db.connect(cfg.db_path)
    [aside] = check.execute("SELECT * FROM ignored_detections").fetchall()   # once, though seen twice
    assert aside["reason"] == "phone" and aside["crop_path"]
    assert "set aside 1 thing today" in client.get("/").text

    page = client.get("/log").text
    assert "Set aside: looks like a phone" in page and f'data-fix="ignored" data-id="{aside["id"]}"' in page
    # It was a sale after all (the owner says so): recorded when it happened, and learned.
    r = client.post(f"/ignored/{aside['id']}/sale", data={"pack_id": pack_of(conn, "coke-330")},
                    headers={"Accept": "application/json"})
    assert r.json()["status"] == "corrected"
    check = db.connect(cfg.db_path)
    assert stock.on_hand(check)[pid(check, "coke-330")] == -2            # the auto one and this one
    assert check.execute("SELECT fixed_item_id FROM ignored_detections").fetchone()[0] == r.json()["item"]
    assert "recorded as a sale" in client.get("/log").text


def test_set_aside_things_are_let_go_after_a_while(conn, cfg):
    txn = transactions.open_transaction(conn)
    transactions.add_ignored(conn, txn, [("hand", transactions.DetectionRecord(None, 0.9, "high", "image",
                                                                               (0, 0, 10, 10), None, []))])
    conn.execute("UPDATE ignored_detections SET created_at = '2026-09-01 10:00:00'")
    assert transactions.forget_ignored(conn, cfg.media_dir, 14) == 1
    assert conn.execute("SELECT COUNT(*) FROM ignored_detections").fetchone()[0] == 0


# --- fixing what was recorded -------------------------------------------------------------------

def recorded_sale(conn, pack_id, crop="crops/r.jpg"):
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(pack_id, 0.99, "high", "image",
                                                                          (0, 0, 10, 10), crop, [(pack_id, 0.99)])])
    transactions.close_transaction(conn, txn)
    return conn.execute("SELECT MAX(id) FROM transaction_items").fetchone()[0]


def test_a_recorded_sale_can_be_fixed(conn):
    coke, sprite = pack_of(conn, "coke-330"), pack_of(conn, "sprite-1500")
    item_id = recorded_sale(conn, coke)
    learned = []
    transactions.fix(conn, item_id, "pick", pack_id=sprite, learn=lambda *a: learned.append(a[1:]))
    levels = stock.on_hand(conn)
    assert levels.get(pid(conn, "coke-330"), 0) == 0 and levels[pid(conn, "sprite-1500")] == -1
    assert learned == [("crops/r.jpg", sprite, None, "correction")]
    assert transactions.item(conn, item_id)["status"] == "corrected"
    transactions.fix(conn, item_id, "qty", count=3)
    assert stock.on_hand(conn)[pid(conn, "sprite-1500")] == -3
    transactions.fix(conn, item_id, "remove")
    assert stock.on_hand(conn).get(pid(conn, "sprite-1500"), 0) == 0
    assert transactions.item(conn, item_id)["status"] == "removed"
    with pytest.raises(ValueError):
        transactions.fix(conn, unknown_sale_row(conn), "remove")      # still waiting: answer it instead


def unknown_sale_row(conn):
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(None, 0.4, "low", "image", (0, 0, 9, 9))])
    transactions.close_transaction(conn, txn)
    return conn.execute("SELECT MAX(id) FROM transaction_items").fetchone()[0]


def test_several_items_in_one_photo_are_separate_sales(conn):
    item_id = unknown_sale_row(conn)
    coke, dumex = pack_of(conn, "coke-330"), pack_of(conn, "dumex-400")
    made = transactions.several(conn, item_id, [(coke, 1), (dumex, 2)])
    assert len(made) == 2 and transactions.item(conn, item_id)["status"] == "split"
    levels = stock.on_hand(conn)
    assert (levels[pid(conn, "coke-330")], levels[pid(conn, "dumex-400")]) == (-1, -2)
    assert not transactions.pending_items(conn)


def test_the_check_card_says_why_it_asks_and_takes_several(client, conn, cfg):
    item_id = unknown_sale(conn, cfg, "w.jpg")
    conn.execute("UPDATE transaction_items SET hint = 'junk' WHERE id = ?", (item_id,))
    conn.commit()
    page = client.get("/").text
    assert "It looked like clutter to the camera" in page and f'data-several="{item_id}"' in page
    r = client.post(f"/items/{item_id}/several", headers={"Accept": "application/json"},
                    data={"picks": f"{pack_of(conn, 'coke-330')}:1,{pack_of(conn, 'sprite-1500')}:1"})
    assert r.json()["status"] == "split" and len(r.json()["items"]) == 2


def test_the_log_and_a_products_history_show_everything(client, conn, cfg):
    coke = pack_of(conn, "coke-330")
    stock.record(conn, pid(conn, "coke-330"), 20, "opening")
    item_id = recorded_sale(conn, coke)
    conn.commit()
    page = client.get("/log").text
    assert "Camera log" in page and "Recorded automatically" in page and f'data-fix="item" data-id="{item_id}"' in page
    r = client.post(f"/items/{item_id}/fix", data={"action": "qty", "count": "2"}, headers={"Accept": "application/json"})
    assert r.json()["count"] == 2
    page = client.get(f"/catalog/{pid(conn, 'coke-330')}/history").text
    assert "History · Coca-Cola can 330ml" in page and "Checked: the camera was right" in page
    assert "Counted" in page and ">18<" in page                          # 20 counted, 2 sold: 18 left
    assert 'href="/log"' in client.get("/").text


# --- the empty-mat photo keeps up with the light ------------------------------------------------

def test_the_empty_mat_photo_refreshes_only_when_the_mat_is_empty(client, conn, cfg):
    pipeline.save_reference(conn, cfg, jpeg(mat_frame()))
    conn.commit()
    busy = mat_frame()
    paint(busy, "coke-330", 200, 150)
    r = client.post("/api/reference/refresh", files={"image": ("e.jpg", jpeg(busy), "image/jpeg")}).json()
    assert r["refreshed"] is False
    r = client.post("/api/reference/refresh", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")}).json()
    assert r["refreshed"] is True
    page = client.get("/counter").text
    assert 'id="readout"' in page
