"""Stock in on the mat: scan, name what's new, read the date, save as a delivery or as a count."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
from PIL import Image

from luydek import clock, db
from luydek.recognition import labels, pipeline
from luydek.services import batches, locations, stock, stockin, transactions
from luydek.bot import handlers
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid
from tests.test_bot import CHAT, FakeBot, context, register, tap
from tests.test_labels_report import FakeClient
from tests.test_recognition import enroll_all
from tests.test_shelf import client_for


def rows(*specs):
    return stockin.parse_rows([dict(s) for s in specs])


def test_a_delivery_with_two_dates_is_two_batches_and_says_where_they_go(conn):
    fridge = locations.add(conn, "Fridge")
    milk = pid(conn, "dutchlady-choc")
    stock.record(conn, milk, 4, "opening", expiry_date="2026-10-20", at=clock.now() - timedelta(days=5))
    out = stockin.save(conn, "delivery", rows({"pack_id": pack_of(conn, "dutchlady-choc"), "location_id": fridge,
                                               "groups": [{"count": 6, "expiry": "2027-01-10", "lot": " L 2601 "},
                                                          {"count": 2, "expiry": "2026-10-05"}]}), by="test")
    assert (out["products"], out["units"]) == (1, 8)
    got = sorted((b.expiry, b.left, b.lot_no) for b in batches.open_batches(conn, milk))
    assert got == [("2026-10-05", 2, None), ("2026-10-20", 4, None), ("2027-01-10", 6, "L 2601")]
    hows = {(p["expiry"], p["how"], p["place"]) for p in out["putaway"]}
    assert hows == {("2027-01-10", "behind", "Fridge"), ("2026-10-05", "front", "Fridge")}
    assert conn.execute("SELECT location_id FROM products WHERE id = ?", (milk,)).fetchone()[0] == fridge  # its home now


def test_a_pack_counts_its_units(conn):
    six = pack_of(conn, "coke-330", 6)
    stockin.save(conn, "delivery", rows({"pack_id": six, "groups": [{"count": 2}]}), by="test")
    assert stock.on_hand(conn)[pid(conn, "coke-330")] == 12


def test_a_dated_count_replaces_the_shelf_batch_by_batch(conn):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 10, "opening", at=clock.now() - timedelta(days=9))
    stockin.save(conn, "count", rows({"pack_id": pack_of(conn, "coke-330"),
                                      "groups": [{"count": 4, "expiry": "2027-03-01"}, {"count": 3, "expiry": "2026-12-01"}]}),
                 by="test")
    assert stock.on_hand(conn)[coke] == 7
    assert sorted((b.expiry, b.left) for b in batches.open_batches(conn, coke)) == [("2026-12-01", 3), ("2027-03-01", 4)]
    logged = conn.execute("SELECT counted, expected, source FROM stock_counts WHERE product_id = ?", (coke,)).fetchone()
    assert tuple(logged) == (7, 10, "stock_in")


def test_a_count_leaves_room_for_sales_not_checked_yet(conn):
    coke, pack = pid(conn, "coke-330"), pack_of(conn, "coke-330")
    stock.record(conn, coke, 10, "opening", at=clock.now() - timedelta(days=9))
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(pack, 0.8, "medium", "image", (0, 0, 9, 9))] * 2)
    transactions.close_transaction(conn, txn)                # 2 sold, waiting for the owner's check
    stockin.save(conn, "count", rows({"pack_id": pack, "groups": [{"count": 5, "expiry": "2027-01-01"}]}), by="test")
    assert stock.on_hand(conn)[coke] == 7                    # 5 on the shelf + the 2 the check will take off
    for item in transactions.pending_items(conn):
        transactions.resolve(conn, item["id"], "yes", by="test")
    assert stock.on_hand(conn)[coke] == 5


def test_an_undated_count_is_an_ordinary_count(conn):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 10, "opening", at=clock.now() - timedelta(days=9))
    stockin.save(conn, "count", rows({"pack_id": pack_of(conn, "coke-330"), "groups": [{"count": 8}]}), by="test")
    assert stock.on_hand(conn)[coke] == 8
    assert conn.execute("SELECT qty FROM stock_movements WHERE product_id = ? ORDER BY id DESC", (coke,)).fetchone()[0] == -2


def test_rows_from_the_page_are_checked():
    got = stockin.parse_rows([
        {"pack_id": 3, "groups": [{"count": 0}, {"count": 2, "expiry": ""}], "crops": ["crops/a.jpg", "../x", 5]},
        {"pack_id": None, "name": "", "groups": [{"count": 3}]},          # neither a product nor a name: dropped
    ])
    assert len(got) == 1 and got[0].groups == [stockin.Group(2)] and got[0].crops == ["crops/a.jpg"]
    try:
        stockin.parse_rows([{"pack_id": 3, "groups": [{"count": 1, "expiry": "31/12/2026"}]}])
    except ValueError:
        pass
    else:
        raise AssertionError("a date that isn't ISO must be refused")


def test_unknown_items_are_grouped_by_look():
    a, b = np.array([1, 0, 0], np.float32), np.array([0, 1, 0], np.float32)
    assert stockin.group_unknowns([a, a * 2, b, a + 0.01, b]) == [0, 0, 1, 0, 1]


def test_naming_a_new_product_teaches_the_camera(conn):
    taught = []
    out = stockin.name(conn, ["crops/a.jpg", "crops/b.jpg"], name_en="Fanta can 330ml", name_km="ហ្វានតា",
                       teach=lambda c, crop, pack: taught.append((crop, pack)) or True)
    assert out["created"] and out["label"] == "Fanta can 330ml" and out["taught"] == 2
    assert [p for _, p in taught] == [out["pack_id"]] * 2
    again = stockin.name(conn, [], name_en="fanta can 330ML")      # the same name later: the same product
    assert not again["created"] and again["pack_id"] == out["pack_id"]


def test_reading_an_expiry_date(cfg):
    client = FakeClient(labels.ExpiryReading(expiry="2027-04-30", lot="L2604A ", text_seen="MFG 01/05/26 EXP 04/2027",
                                             sure=True))
    got = labels.read_expiry(Image.new("RGB", (2000, 800), "white"), cfg, client)
    assert (got.expiry, got.lot, got.sure) == ("2027-04-30", "L2604A", True)
    sent = client.calls[0]["messages"][0]["content"][0]["source"]
    assert sent["media_type"] == "image/jpeg"
    bad = FakeClient(labels.ExpiryReading(expiry="2569-04-30", lot="", text_seen="EXP 30/04/2569", sure=True))
    got = labels.read_expiry(Image.new("RGB", (100, 100)), cfg, bad)   # a Buddhist year left unconverted
    assert (got.expiry, got.sure) == ("", False)


# --- the page ----------------------------------------------------------------------------------------

def test_scan_name_and_save_from_the_page(cfg, conn, stub_encoder, monkeypatch):
    enroll_all(conn, cfg, skus=("coke-330", "sprite-1500"))
    conn.commit()
    frame = mat_frame()
    paint(frame, "coke-330", 30, 40)
    paint(frame, "coke-330", 170, 40)
    paint(frame, "sprite-1500", 320, 40)
    paint(frame, "dumex-400", 470, 250)                                   # not taught yet
    with client_for(cfg, monkeypatch) as c:
        assert "Stock in" in c.get("/stock-in").text
        res = c.post("/api/stockin/scan", files={"image": ("m.jpg", jpeg(frame), "image/jpeg")},
                     data={"use_reference": "false"}).json()
        by_group = {}
        for it in res["items"]:
            by_group.setdefault(it["group"], []).append(it)
        coke, sprite = pack_of(conn, "coke-330"), pack_of(conn, "sprite-1500")
        assert len(by_group[f"p{coke}"]) == 2 and len(by_group[f"p{sprite}"]) == 1
        [unknown] = [g for k, g in by_group.items() if not k.startswith("p")]
        assert unknown[0]["pack_id"] is None and (cfg.media_dir / unknown[0]["crop"]).exists()

        named = c.post("/api/stockin/name", json={"crops": [unknown[0]["crop"]], "name": "Dumex Dugro 400g"}).json()
        assert named["created"] and named["taught"] == 1
        crops = [it["crop"] for it in res["items"]]
        saved = c.post("/api/stockin/save", json={"mode": "delivery", "crops": crops, "rows": [
            {"pack_id": coke, "groups": [{"count": 2, "expiry": "2027-02-01"}], "teach": False},
            {"pack_id": sprite, "groups": [{"count": 1}], "teach": True, "crops": [by_group[f"p{sprite}"][0]["crop"]]},
            {"pack_id": named["pack_id"], "groups": [{"count": 1, "expiry": "2027-06-30", "lot": "B12"}]},
        ]}).json()
        assert (saved["products"], saved["units"]) == (3, 4)
        assert not any((cfg.media_dir / crop).exists() for crop in crops)     # the scan's copies are cleaned up
        assert c.post("/api/stockin/save", json={"mode": "delivery", "rows": []}).status_code == 400

        again = c.post("/api/stockin/scan", files={"image": ("m.jpg", jpeg(frame), "image/jpeg")},
                       data={"use_reference": "false"}).json()
        assert {it["pack_id"] for it in again["items"]} == {coke, sprite, named["pack_id"]}  # it learned the new one
    c2 = db.connect(cfg.db_path)
    assert stock.on_hand(c2)[pid(c2, "coke-330")] == 2
    [b] = batches.open_batches(c2, named["product_id"])
    assert (b.expiry, b.lot_no) == ("2027-06-30", "B12")


def test_the_date_reader_on_the_page(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    batch = stock.record(conn, pid(conn, "coke-330"), 6, "delivery")
    conn.commit()
    photo = jpeg(mat_frame(200, 100))
    with client_for(cfg, monkeypatch) as c:
        assert c.post("/api/stockin/expiry", files={"image": ("d.jpg", photo, "image/jpeg")}).status_code == 503
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(labels, "read_expiry", lambda img, cfg_, client=None: labels.ExpiryReading(
            expiry="2027-01-31", lot="", text_seen="EXP 31.01.27", sure=True))
        assert c.post("/api/stockin/expiry", files={"image": ("d.jpg", photo, "image/jpeg")}).json()["expiry"] == "2027-01-31"
        c.post(f"/batches/{batch}/expiry-photo", files={"image": ("d.jpg", photo, "image/jpeg")})
    assert batches.get(db.connect(cfg.db_path), batch).expiry == "2027-01-31"


def test_teaching_skips_near_copies(cfg, conn, stub_encoder):
    rec = pipeline.get_recognizer(cfg)
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    frame = mat_frame(120, 160)
    paint(frame, "coke-330", 10, 10)
    Image.fromarray(frame[:, :, ::-1]).save(cfg.media_dir / "crops" / "a.jpg")
    pack = pack_of(conn, "coke-330")
    assert rec.teach(conn, "crops/a.jpg", pack, 5) is True
    assert rec.teach(conn, "crops/a.jpg", pack, 5) is False                # the same photo again
    assert rec.teach(conn, "crops/gone.jpg", pack, 5) is False
    assert conn.execute("SELECT source FROM gallery_images WHERE pack_id = ?", (pack,)).fetchall()[0][0] == "enrollment"


def test_the_nav_and_shelf_link_to_stock_in(cfg, conn, stub_encoder, monkeypatch):
    with client_for(cfg, monkeypatch) as c:
        assert 'href="/stock-in"' in c.get("/shelf").text
        page = c.get("/stock-in?mode=count").text
        assert "Save the count" in page and 'href="/stock-in"' in page


# --- Telegram: dates for a delivery that came without them -------------------------------------------

class FakePhotoMessage:
    def __init__(self, data: bytes, reply_to: int):
        self.reply_to_message = SimpleNamespace(message_id=reply_to)
        self.replies, self.document = [], None
        file = SimpleNamespace(download_to_memory=self._download)
        self.photo = [SimpleNamespace(get_file=self._get_file(file))]
        self.data = data

    @staticmethod
    def _get_file(file):
        async def get():
            return file
        return get

    async def _download(self, out):
        out.write(self.data)

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append(text)


def undated_invoice(conn):
    """A confirmed-to-be delivery of two products whose shelf life nobody has said yet."""
    conn.execute("UPDATE products SET shelf_life_days = NULL WHERE sku IN ('coke-330', 'lays-50')")
    inv = conn.execute("INSERT INTO invoices (status, received_at) VALUES ('extracted', '2026-09-27')").lastrowid
    for sku in ("coke-330", "lays-50", "sprite-1500"):                    # sprite keeps 270 days: no question
        conn.execute("INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total) "
                     "VALUES (?, ?, ?, 1, 'case', 24)", (inv, pid(conn, sku), sku))
    conn.commit()
    return inv


def test_a_delivery_with_no_dates_asks_for_them(conn, cfg):
    register(conn)
    inv = undated_invoice(conn)
    bot = FakeBot()
    tap(cfg, bot, f"i:{inv}:ok")
    asks = [s for s in bot.sent if "when does it expire" in (s[2] or "")]
    assert [a[2].split(" ×")[0] for a in asks] == ["📅 Coca-Cola can 330ml", "📅 Lay's classic 50g"]
    coke_batch = int(asks[0][3].inline_keyboard[0][0].callback_data.split(":")[1])
    assert [b.callback_data for row in asks[0][3].inline_keyboard for b in row] == [
        f"x:{coke_batch}:3", f"x:{coke_batch}:6", f"x:{coke_batch}:12", f"x:{coke_batch}:0"]
    query = tap(cfg, bot, f"x:{coke_batch}:6")
    assert "keeps about 6 months" in query.edits[-1][0]
    c = db.connect(cfg.db_path)
    assert c.execute("SELECT shelf_life_days FROM products WHERE sku = 'coke-330'").fetchone()[0] == 182
    assert batches.get(c, coke_batch).est_expiry == "2027-03-28"           # assumed from now on, never printed


def test_a_photo_in_reply_reads_the_date(conn, cfg, monkeypatch):
    register(conn)
    inv = undated_invoice(conn)
    bot = FakeBot()
    tap(cfg, bot, f"i:{inv}:ok")
    asked = next(i for i, s in enumerate(bot.sent) if "when does it expire" in (s[2] or ""))
    ask_id = 101 + asked                                                    # FakeBot numbers messages from 101
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(labels, "read_expiry", lambda img, cfg_, client=None: labels.ExpiryReading(
        expiry="2027-05-31", lot="L0526", text_seen="EXP 31/05/27 L0526", sure=True))
    msg = FakePhotoMessage(jpeg(mat_frame(200, 100)), ask_id)
    update = SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(handlers.on_photo(update, context(cfg, bot)))
    assert bot.edits[-1][0] == ask_id and "expires 31/05/27 · lot L0526" in bot.edits[-1][1]
    c = db.connect(cfg.db_path)
    coke = pid(c, "coke-330")
    [b] = batches.open_batches(c, coke)
    assert (b.expiry, b.lot_no) == ("2027-05-31", "L0526")
    assert c.execute("SELECT shelf_life_days FROM products WHERE id = ?", (coke,)).fetchone()[0] == 246
    assert c.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1  # not taken for a new invoice


def test_put_away_lists_all_that_came_even_after_selling_more_than_received(conn):
    sting = pid(conn, "sting-330")
    stock.record(conn, sting, -2, "sale", at=clock.now() - timedelta(days=1))   # sold before any stock was recorded
    out = stockin.save(conn, "delivery", rows({"pack_id": pack_of(conn, "sting-330"), "groups": [{"count": 3}]}),
                       by="test")
    assert [p["qty"] for p in out["putaway"]] == [3]                          # all 3 are in the owner's hands
    assert stock.on_hand(conn)[sting] == 1                                    # the books: 2 of them repaid the sales
