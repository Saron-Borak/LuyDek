"""Loose tablets: stock in tablets, the dispensing tray, and the box beside it."""

from types import SimpleNamespace

import cv2
import pytest
from fastapi.testclient import TestClient

from luydek import db
from luydek.bot import digest
from luydek.config import TrayCfg
from luydek.recognition import pipeline, tray
from luydek.services import loose, stock, transactions
from luydek.services.transactions import DetectionRecord
from luydek.web.server import create_app
from tests.conftest import jpeg, mat_frame, pack_of, pid

U = 0.0006          # one tablet's area as a share of the frame, for the tests that don't use images
TRAY_PX = (440, 300, 620, 460)
TRAY_ROI = (445 / 640, 305 / 480, 615 / 640, 455 / 480)


def counter(tablets=(), blister=None, box=False):
    """The test counter: a pale mat with a black tablet tray in the bottom-right corner."""
    frame = mat_frame()
    x0, y0, x1, y1 = TRAY_PX
    frame[y0:y1, x0:x1] = (25, 25, 25)
    for cx, cy in tablets:
        cv2.ellipse(frame, (cx, cy), (9, 6), 0, 0, 360, (245, 245, 245), -1)
    if blister:  # a cut piece of blister strip: foil round two tablets
        bx, by = blister
        cv2.rectangle(frame, (bx, by), (bx + 44, by + 22), (190, 190, 195), -1)
        for dx in (11, 33):
            cv2.ellipse(frame, (bx + dx, by + 11), (8, 6), 0, 0, 360, (250, 250, 250), -1)
    if box:
        paint_box(frame, 60, 80)
    return frame


def paint_box(frame, x, y):
    cv2.rectangle(frame, (x, y), (x + 100, y + 140), (230, 200, 120), -1)
    cv2.rectangle(frame, (x, y + 50), (x + 100, y + 80), (40, 40, 200), -1)


def txn_with(conn, detections=(), areas=None, oversized=False):
    txn = transactions.open_transaction(conn)
    frame_no = transactions.add_frame(conn, txn, None, list(detections))
    if areas is not None:
        transactions.add_tray(conn, txn, frame_no, list(areas), oversized, None)
    return txn


def box_seen(conn, sku="cetirizine-10", tier="high", units=100):
    p = pack_of(conn, sku, units)
    return DetectionRecord(p, 0.98 if tier == "high" else 0.85, tier, "image", (60, 80, 100, 140), None, [(p, 0.98)])


# --- counting ------------------------------------------------------------------------------

def test_pieces_are_counted_against_the_learned_tablet_size():
    assert loose.count_tablets([U, 2.08 * U, 0.93 * U], U, 0.35) == (4, True)   # a cut piece of two counts 2
    assert loose.count_tablets([U, 1.5 * U], U, 0.35) == (3, False)            # half a tablet more: not sure
    assert loose.count_tablets([U, U, U], None, 0.35) == (3, False)            # size not learned yet
    assert loose.count_tablets([], U, 0.35) == (0, True)


def test_the_tray_finds_each_piece_and_ignores_crumbs(cfg):
    frame = counter(tablets=[(470, 330), (520, 380), (580, 430)])
    frame[400, 460] = (255, 255, 255)                              # a crumb
    reading = tray.read(frame, counter(), TRAY_ROI, cfg.recognition.tray)
    assert len(reading.areas) == 3 and not reading.oversized
    assert max(reading.areas) / min(reading.areas) < 1.2
    assert reading.crop.size == (170, 150)
    big = counter()
    big[310:450, 450:600] = (200, 180, 90)                         # a box put down on the tray
    assert tray.read(big, counter(), TRAY_ROI, cfg.recognition.tray).oversized


def test_tablets_on_the_tray_are_never_items_on_the_mat(cfg):
    frame = counter(tablets=[(470, 330), (520, 380)], box=True)
    blobs = pipeline.segment.segment(frame, counter(), cfg.recognition.segment, exclude=TRAY_ROI)
    assert len(blobs) == 1 and blobs[0].bbox[0] < 200


# --- learning the tablet size ----------------------------------------------------------------

def test_checked_counts_teach_the_tablet_size(conn):
    cet = pid(conn, "cetirizine-10")
    assert loose.unit_area(conn, cet) is None
    loose.learn_unit(conn, cet, [U * 1.02, U * 0.98], 2)       # two loose tablets: two samples
    assert loose.unit_area(conn, cet) == pytest.approx(U)
    loose.learn_unit(conn, cet, [2 * U], 2)                    # one piece of two: one sample of half
    loose.learn_unit(conn, cet, [U, U, U], 2)                  # more pieces than tablets: learns nothing
    assert loose.unit_area(conn, cet) == pytest.approx(U)


# --- closing a sale ----------------------------------------------------------------------------

def test_the_box_beside_the_tray_sells_tablets_not_the_box(conn):
    cet, coke = pid(conn, "cetirizine-10"), pid(conn, "coke-330")
    loose.learn_unit(conn, cet, [U, U], 2)
    stock.record(conn, cet, 300, "opening")
    coke_seen = DetectionRecord(pack_of(conn, "coke-330"), 0.99, "high", "image", (0, 0, 9, 9), None, [])
    txn = txn_with(conn, [box_seen(conn), coke_seen], areas=[U, 2.05 * U])
    summary = transactions.close_transaction(conn, txn)
    assert summary["auto"] == 2 and summary["pending"] == 0
    item = conn.execute("SELECT * FROM transaction_items WHERE method = 'tray'").fetchone()
    assert (item["count"], item["pack_id"], item["status"]) == (3, pack_of(conn, "cetirizine-10"), "auto")
    assert stock.on_hand(conn)[cet] == 297                     # 3 tablets, not the 100-tablet box
    assert stock.on_hand(conn)[coke] == -1                     # the rest of the sale is as before
    assert conn.execute("SELECT COUNT(*) FROM tray_readings").fetchone()[0] == 0


def test_a_box_alone_is_a_whole_box_sale(conn):
    txn = txn_with(conn, [box_seen(conn)])
    transactions.close_transaction(conn, txn)
    assert stock.on_hand(conn)[pid(conn, "cetirizine-10")] == -100


def test_an_unlearned_size_asks_the_owner_and_the_answer_teaches_it(conn):
    cet = pid(conn, "cetirizine-10")
    txn = txn_with(conn, [box_seen(conn)], areas=[U, U])
    transactions.close_transaction(conn, txn)
    (item,) = transactions.pending_items(conn)
    assert (item["method"], item["tier"], item["count"]) == ("tray", "medium", 2)
    transactions.resolve(conn, item["id"], "yes")
    assert stock.on_hand(conn)[cet] == -2
    assert loose.unit_area(conn, cet) == pytest.approx(U)     # next time it counts on its own


def test_a_forgotten_box_asks_which_medicine(conn):
    cet = pid(conn, "cetirizine-10")
    txn = txn_with(conn, [], areas=[U, U, U])
    transactions.close_transaction(conn, txn)
    (item,) = transactions.pending_items(conn)
    assert (item["pack_id"], item["tier"], item["count"]) == (None, "low", 3)
    assert [p for p, _ in transactions.candidates(item)] == [pack_of(conn, "cetirizine-10")]
    # Picking the box (the photo the owner recognises) records tablets, not boxes.
    transactions.resolve(conn, item["id"], "pick", pack_id=pack_of(conn, "cetirizine-10", 100))
    assert stock.on_hand(conn)[cet] == -3


def test_two_medicines_beside_the_tray_go_to_the_owner(conn):
    loose.set_sold_loose(conn, pid(conn, "para-500"), 10)
    loose.learn_unit(conn, pid(conn, "cetirizine-10"), [U, U], 2)
    txn = txn_with(conn, [box_seen(conn), box_seen(conn, "para-500", units=10)], areas=[U])
    transactions.close_transaction(conn, txn)
    (item,) = transactions.pending_items(conn)
    assert item["tier"] == "medium" and len(transactions.candidates(item)) == 1


def test_a_look_alike_box_leaves_the_medicine_to_check(conn):
    loose.learn_unit(conn, pid(conn, "cetirizine-10"), [U, U], 2)
    txn = txn_with(conn, [box_seen(conn, tier="medium")], areas=[U])
    transactions.close_transaction(conn, txn)
    assert transactions.pending_items(conn)[0]["tier"] == "medium"


def test_a_hand_over_the_tray_is_not_a_count(conn):
    loose.learn_unit(conn, pid(conn, "cetirizine-10"), [U, U], 2)
    txn = txn_with(conn, [box_seen(conn)], areas=[U], oversized=True)
    transactions.close_transaction(conn, txn)
    assert transactions.pending_items(conn)[0]["tier"] == "medium"


# --- selling a medicine by the tablet ---------------------------------------------------------

def test_switching_to_tablets_converts_the_stock_and_keeps_the_box(conn):
    para = pid(conn, "para-500")
    old_pack = pack_of(conn, "para-500")
    stock.record(conn, para, 20, "opening")
    stock.record(conn, para, -2, "sale")
    loose.set_sold_loose(conn, para, 10)
    assert stock.on_hand(conn)[para] == 180
    assert pack_of(conn, "para-500", 10) == old_pack          # the box keeps its photos and barcode
    row = conn.execute("SELECT * FROM products WHERE id = ?", (para,)).fetchone()
    assert (row["unit"], row["case_size"], row["sold_loose"]) == ("tablet", 100, 1)
    assert row["unit_cost"] == pytest.approx(0.06)
    with pytest.raises(ValueError):
        loose.set_sold_loose(conn, para, 10)


def test_stock_reads_in_boxes_and_tablets(conn):
    stock.record(conn, pid(conn, "cetirizine-10"), 347, "opening")
    row = next(r for r in stock.stock_table(conn) if r["sku"] == "cetirizine-10")
    assert row["breakdown"] == "3 boxes + 47 tablets"
    assert loose.breakdown(100, "box", 100, "tablet") == "1 box"
    assert loose.breakdown(1, "box", 100, "tablet") == "1 tablet"


def test_the_telegram_question_says_tablets(conn):
    txn = txn_with(conn, [box_seen(conn)], areas=[U, U])
    transactions.close_transaction(conn, txn)
    item = transactions.pending_items(conn)[0]
    assert "💊 Sold loose: 2 × Cetirizine 10mg tablets?" in digest.item_caption(item, "en")


# --- Claude reads what the tray could not ---------------------------------------------------------

class FakeClient:
    def __init__(self, reading):
        self.calls, self.reading = [], reading
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=self.reading)


def pending_tray(conn, cfg, tier="high", areas=(U, U, U)):
    from PIL import Image
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (200, 100), "black").save(cfg.media_dir / "crops" / "t.jpg")
    txn = txn_with(conn, [box_seen(conn, tier=tier)], areas=list(areas))
    transactions.close_transaction(conn, txn)
    conn.execute("UPDATE transaction_items SET crop_path = 'crops/t.jpg' WHERE method = 'tray'")
    conn.commit()
    return transactions.pending_items(conn)[0]["id"]


def test_claude_counts_what_the_tray_could_not_and_the_tray_learns(conn, cfg):
    item_id = pending_tray(conn, cfg)
    client = FakeClient(tray.TrayCount(count=3, count_sure=True, text_seen="", choice=None, medicine_sure=False))
    assert tray.settle(cfg, client=client) == 1
    text = client.calls[0]["messages"][0]["content"][1]["text"]
    assert "Count the tablets" in text and "Which medicine" not in text   # the box already said which
    check = db.connect(cfg.db_path)
    row = transactions.item(check, item_id)
    assert (row["status"], row["count"], row["verified_by"]) == ("auto", 3, "tray reader")
    assert stock.on_hand(check)[pid(check, "cetirizine-10")] == -3
    assert loose.unit_area(check, pid(check, "cetirizine-10")) == pytest.approx(U)


def test_an_unsure_reading_goes_to_the_owner_with_a_better_guess(conn, cfg):
    item_id = pending_tray(conn, cfg, tier="medium")
    client = FakeClient(tray.TrayCount(count=4, count_sure=False, text_seen="CETIRI", choice=1, medicine_sure=False))
    assert tray.settle(cfg, client=client) == 0
    assert "Which medicine" in client.calls[0]["messages"][0]["content"][1]["text"]
    row = transactions.item(db.connect(cfg.db_path), item_id)
    assert (row["status"], row["count"], row["label_text"]) == ("pending", 4, "CETIRI")
    assert tray.settle(cfg, client=client) == 0 and len(client.calls) == 1   # asked once only


def test_the_label_reader_leaves_tray_items_alone(conn, cfg):
    from luydek.recognition import labels
    pending_tray(conn, cfg, tier="medium")
    client = FakeClient(labels.LabelReading(text_seen="", choice=None, sure=False))
    labels.settle(cfg, client=client)
    assert client.calls == []


# --- the counter, end to end ------------------------------------------------------------------------

@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


def post_frame(client, url, frame, **data):
    return client.post(url, files={"image": ("f.jpg", jpeg(frame), "image/jpeg")}, data=data)


def test_the_counter_counts_tablets_and_learns_from_the_first_check(client, conn):
    assert post_frame(client, "/api/calibrate", counter()).status_code == 200
    r = client.post("/api/roi", data={"area": "tray", "x0": TRAY_ROI[0], "y0": TRAY_ROI[1],
                                      "x1": TRAY_ROI[2], "y1": TRAY_ROI[3]})
    assert r.status_code == 200 and client.get("/api/state").json()["tray"] == pytest.approx(list(TRAY_ROI), abs=1e-3)
    box = pack_of(conn, "cetirizine-10", 100)
    for i in range(4):
        frame = counter()
        paint_box(frame, 80 + 40 * i, 60 + 30 * i)
        assert post_frame(client, "/api/enroll", frame, pack_id=box, use_reference="true").status_code == 200

    # First loose sale: the tray doesn't know a tablet's size yet, so it asks.
    res = post_frame(client, "/api/capture", counter(tablets=[(480, 340), (560, 420)], box=True)).json()
    assert [d["label"] for d in res["detections"]] == ["Cetirizine 10mg tablets (box)"]   # tablets aren't mat items
    assert (res["tray"]["pieces"], res["tray"]["count"], res["tray"]["sure"]) == (2, 2, False)
    closed = client.post(f"/api/transactions/{res['txn_id']}/close").json()
    assert closed["pending"] == 1 and closed["lines"][0]["method"] == "tray"
    item = transactions.pending_items(conn)[0]
    client.post(f"/items/{item['id']}/resolve", data={"action": "yes"})

    # Second sale: it knows, and records three tablets on its own.
    res = post_frame(client, "/api/capture", counter(tablets=[(470, 330), (520, 380), (580, 430)], box=True)).json()
    assert (res["tray"]["count"], res["tray"]["sure"]) == (3, True)
    closed = client.post(f"/api/transactions/{res['txn_id']}/close").json()
    assert closed["auto"] == 1 and closed["lines"][0]["count"] == 3
    assert stock.on_hand(conn)[pid(conn, "cetirizine-10")] == -5

    assert client.post("/api/roi", data={"area": "tray", "clear": "true"}).json()["roi"] is None
    assert loose.unit_area(conn, pid(conn, "cetirizine-10")) is None       # a moved tray learns again


def test_product_page_switches_a_medicine_to_tablets(client, conn):
    para = pid(conn, "para-500")
    page = client.get(f"/catalog/{para}").text
    assert "Pharmacies: sold by the tablet too?" in page
    client.post(f"/catalog/{para}/loose", data={"per_box": "10", "piece": "tablet"})
    page = client.get(f"/catalog/{para}").text
    assert "Sold by the tablet" in page and "one box holds 10" in page
    assert client.get(f"/catalog/{pid(conn, 'cetirizine-10')}").status_code == 200


def test_tray_settings_are_validated(client):
    r = client.post("/api/roi", data={"area": "tray", "x0": 0.1, "y0": 0.1, "x1": 0.12, "y1": 0.2})
    assert r.status_code == 400
    assert client.post("/api/roi", data={"area": "shelf", "clear": "true"}).status_code == 400


def test_config_defaults_match_the_shipped_config():
    from luydek.config import load_config
    assert load_config().recognition.tray == TrayCfg()
