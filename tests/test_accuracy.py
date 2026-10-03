"""Accuracy: size on the mat, look-alike pairs, the accuracy test, and where the stock came from."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import numpy as np

from luydek import clock, db
from luydek.bot import digest, handlers
from luydek.recognition import labels, pipeline
from luydek.recognition.matcher import Gallery, fits_size
from luydek.services import accuracy, batches, invoices, sources, stock
from luydek.services.invoices import ExtractedInvoice, ExtractedLine
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid
from tests.test_bot import CHAT, FakeBot, context, register, tap
from tests.test_invoices import FakeClient, queued_invoice
from tests.test_shelf import client_for
from tests.test_stockin import FakePhotoMessage


def day(n):
    return clock.now() - timedelta(days=n)


# --- size on the mat ----------------------------------------------------------------------------------

def test_fits_size():
    assert fits_size(0.05, None, 1.4)                       # size not known: no say
    assert fits_size(0.05, (0.04, 0.06), 1.4) and fits_size(0.03, (0.04, 0.06), 1.4)
    assert not fits_size(0.2, (0.04, 0.06), 1.4) and not fits_size(0.01, (0.04, 0.06), 1.4)


def gallery_of(vectors_by_pack: dict[int, list], areas_by_pack: dict[int, list] | None = None) -> Gallery:
    vectors, labels_, areas = [], [], []
    for pack, vecs in vectors_by_pack.items():
        for i, v in enumerate(vecs):
            vectors.append(np.asarray(v, np.float32) / np.linalg.norm(v))
            labels_.append(("pack", pack))
            areas.append((areas_by_pack or {}).get(pack, [np.nan] * len(vecs))[i])
    return Gallery(np.stack(vectors), labels_, None, np.asarray(areas, np.float32))


def test_the_wrong_size_is_asked_and_the_right_size_guessed(cfg, stub_encoder):
    rec = pipeline.get_recognizer(cfg)
    g = gallery_of({1: [[1, 0, 0], [1, .1, 0], [1, 0, .1]], 2: [[0, 1, 0], [.1, 1, 0], [0, 1, .1]]},
                   {1: [.02, .022, .021], 2: [.10, .11, .12]})
    cands = [(1, .99), (2, .95)]
    assert rec._doubt(g, 1, .99, cands, "image", .021)[3] is None             # its usual size
    pack, score, got, hint = rec._doubt(g, 1, .99, cands, "image", .105)       # the big one's size
    assert (pack, score, hint) == (2, .95, "size") and got[0] == (2, .95)
    assert rec._doubt(g, 1, .99, [(1, .99), (2, .70)], "image", .105)[:2] == (1, .99)  # too far behind to swap
    assert rec._doubt(g, 1, .99, cands, "image", None)[3] is None             # another camera: size not used


def test_look_alike_pairs_are_found_and_asked(cfg, stub_encoder):
    rec = pipeline.get_recognizer(cfg)
    g = gallery_of({1: [[1, .02, 0], [1, 0, .03], [1, .03, 0]], 2: [[1, .05, .02], [1, .02, .05], [1, .06, 0]],
                    3: [[0, 0, 1], [0, .1, 1], [.1, 0, 1]]})
    assert set(g.pairs()) == {frozenset((1, 2))}
    assert rec._doubt(g, 1, .99, [(1, .99), (2, .96)], "image", None)[3] == "lookalike"
    assert rec._doubt(g, 1, .99, [(1, .99), (2, .85)], "image", None)[3] is None    # clearly ahead
    assert rec._doubt(g, 1, .99, [(1, .99), (2, .96)], "colour", None)[3] is None   # colour told them apart
    assert rec._doubt(g, 3, .99, [(3, .99), (1, .96)], "image", None)[3] is None    # not a known pair


def test_size_from_the_counter_camera_tells_a_can_from_a_big_bottle(conn, cfg, stub_encoder):
    """Same colours, same design, different size: what only the size on the mat can tell."""
    pipeline.save_reference(conn, cfg, jpeg(mat_frame()))
    rec = pipeline.get_recognizer(cfg)
    can, bottle = pack_of(conn, "coke-330"), pack_of(conn, "coke-1500")
    for i in range(4):
        small, big = mat_frame(), mat_frame()
        paint(small, "coke-330", 100 + 40 * i, 80 + 20 * i, 60 + 2 * i, 90 + 3 * i)
        paint(big, "coke-330", 80 + 30 * i, 60 + 15 * i, 170 + 4 * i, 255 + 6 * i)
        rec.enroll(conn, small, can)
        rec.enroll(conn, big, bottle)
    assert conn.execute("SELECT COUNT(*) FROM gallery_images WHERE area_frac IS NOT NULL").fetchone()[0] == 8
    for (w, h), want in (((62, 93), can), ((175, 262), bottle)):
        frame = mat_frame()
        paint(frame, "coke-330", 250, 120, w, h)
        (d,) = rec.recognize(conn, frame, f"s{want}")
        assert d.record.pack_id == want and d.record.tier != "high"   # the right size, and still asked


def test_look_alikes_on_the_catalog_and_enroll_pages(cfg, conn, stub_encoder, monkeypatch):
    rec = pipeline.get_recognizer(cfg)
    can, bottle = pack_of(conn, "coke-330"), pack_of(conn, "coke-1500")
    for i in range(3):
        for pack in (can, bottle):                              # the same paint: the camera can't tell them apart
            frame = mat_frame()
            paint(frame, "coke-330", 100 + 50 * i + (7 if pack == bottle else 0), 80 + 30 * i)
            rec.enroll(conn, frame, pack)
    conn.commit()
    [pair] = accuracy.lookalike_pairs(conn, cfg.recognition)
    assert {pair["a"]["pack_id"], pair["b"]["pack_id"]} == {can, bottle}
    with client_for(cfg, monkeypatch) as c:
        assert "Look-alikes the camera mixes up" in c.get("/catalog").text
        assert "The camera mixes this up with" in c.get(f"/catalog/enroll?pack_id={can}").text


# --- the accuracy test --------------------------------------------------------------------------------

def test_the_accuracy_test_page(cfg, conn, stub_encoder, monkeypatch):
    from tests.test_recognition import enroll_all
    enroll_all(conn, cfg, skus=("coke-330", "sprite-1500"))
    conn.commit()
    coke, sprite = pack_of(conn, "coke-330"), pack_of(conn, "sprite-1500")
    frame = mat_frame()
    paint(frame, "coke-330", 200, 150)
    photo = jpeg(frame)
    with client_for(cfg, monkeypatch) as c:
        assert "Accuracy test" in c.get("/accuracy").text
        res = c.post("/api/accuracy/test", files={"image": ("t.jpg", photo, "image/jpeg")},
                     data={"pack_id": coke, "lighting": "daylight", "use_reference": "false"}).json()
        assert [r["right"] for r in res["results"]] == [True]
        res = c.post("/api/accuracy/test", files={"image": ("t.jpg", photo, "image/jpeg")},  # said to be Sprite
                     data={"pack_id": sprite, "lighting": "lamp", "use_reference": "false"}).json()
        assert [r["right"] for r in res["results"]] == [False] and "Mix-ups" in res["summary_html"]
        assert c.post("/api/accuracy/test", files={"image": ("t.jpg", photo, "image/jpeg")},
                      data={"pack_id": coke, "lighting": "moonlight"}).status_code == 400
    s = accuracy.summary(db.connect(cfg.db_path))
    assert [(x["key"], x["n"], x["right"]) for x in s["lights"]] == [("daylight", 1, 1), ("lamp", 1, 0)]
    assert s["mixups"][0]["expected"] == "Sprite bottle 1.5L" and s["mixups"][0]["got"] == "Coca-Cola can 330ml"


# --- sold more than came in -----------------------------------------------------------------------------

def test_sold_more_than_came_in(conn):
    coke, sprite, sting = pid(conn, "coke-330"), pid(conn, "sprite-1500"), pid(conn, "sting-330")
    stock.record(conn, coke, 5, "opening", at=day(20))
    for d in range(7):
        stock.record(conn, coke, -1, "sale", at=day(12 - d))   # 2 more than the 5 that were there
    stock.count(conn, coke, 2, "spot_check")                     # and a count still finds 2 on the shelf
    stock.record(conn, sprite, 2, "opening", at=day(20))
    stock.record(conn, sprite, -4, "sale", at=day(9))
    stock.record(conn, sprite, 12, "delivery", at=day(8))        # the invoice came a day late: fine
    stock.record(conn, sting, 1, "opening", at=day(20))
    stock.record(conn, sting, -3, "sale", at=day(3))             # nothing has covered these 2 yet
    new = conn.execute("INSERT INTO products (sku, name_en, name_km, origin, added_at) "
                       "VALUES ('x', 'Learned at the counter', 'x', 'counter', '2026-09-20')").lastrowid
    stock.record(conn, new, -5, "sale", at=day(2))               # never counted: selling "from nowhere" is expected
    got = {r["name_en"]: (r["units"], r["found"], r["missing"]) for r in sources.unrecorded(conn)}
    assert got == {"Coca-Cola can 330ml": (2, 2, 0), "Sting energy drink 330ml": (2, 0, 2)}
    assert stock.on_hand(conn)[coke] == 2


def test_the_shelf_page_and_weekly_check_say_where_stock_did_not_add_up(cfg, conn, stub_encoder, monkeypatch):
    register(conn)
    sting = pid(conn, "sting-330")
    stock.record(conn, sting, 1, "opening", at=day(20))
    stock.record(conn, sting, -3, "sale", at=day(3))
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get("/shelf").text
    assert "Where did it come from?" in page and "Sold 2 in the last 4 weeks that no delivery brought in" in page
    bot = FakeBot()
    asyncio.run(digest.send_shelf_check(bot, cfg, scheduled=True))      # no expiry tasks, but this to say
    text = bot.sent[-1][2]
    assert "Where did it come from?" in text and "Sting energy drink 330ml: sold 2 in 4 weeks" in text


# --- lot numbers ---------------------------------------------------------------------------------------

def lot_invoice(conn, cfg, lot="L-0499"):
    coke = pid(conn, "coke-330")
    client = FakeClient(ExtractedInvoice(supplier_name="COCA-COLA AGENT", invoice_date="2026-09-27", currency="USD",
                                         lines=[ExtractedLine(text_as_written="Coca 330", product_id=coke, quantity=1,
                                                              unit="case", units_per_case=None, unit_cost=None,
                                                              expiry_date=None, lot_no=lot, is_free=False)]))
    invoice_id = queued_invoice(conn, cfg)
    assert invoices.process(cfg, invoice_id, client) == "extracted"
    return invoice_id


def test_the_lot_on_the_invoice_goes_with_the_stock_and_a_different_lot_is_flagged(conn, cfg):
    invoice_id = lot_invoice(conn, cfg)
    with db.session(cfg) as c:
        assert c.execute("SELECT lot_no FROM invoice_lines").fetchone()[0] == "L-0499"
        invoices.confirm(c, invoice_id, by="test")
        [batch] = batches.for_invoice(c, invoice_id)
        assert batches.get(c, batch).lot_no == "L-0499"
        assert batches.check_lot(c, batch, "LOT L0499") is None            # the same lot, written another way
        assert batches.check_lot(c, batch, "") is None
        wrong = batches.check_lot(c, batch, "L0526")
        assert wrong["expected"] == "L-0499"
        batches.check_lot(c, batch, "L0526")                                # read twice: noted once
        [flag] = sources.lot_flags(c)
        assert (flag["found"], flag["expected"], flag["name_en"]) == ("L0526", "L-0499", "Coca-Cola can 330ml")
        sources.clear(c, flag["id"], by="test")
        assert sources.lot_flags(c) == []


def test_a_date_photo_with_another_lot_warns_in_telegram(conn, cfg, monkeypatch):
    register(conn)
    conn.execute("UPDATE products SET shelf_life_days = NULL WHERE sku = 'coke-330'")
    conn.commit()
    invoice_id = lot_invoice(conn, cfg)
    bot = FakeBot()
    tap(cfg, bot, f"i:{invoice_id}:ok")
    asked = next(i for i, s in enumerate(bot.sent) if "when does it expire" in (s[2] or ""))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(labels, "read_expiry", lambda img, cfg_, client=None: labels.ExpiryReading(
        expiry="2027-05-31", lot="L0526", text_seen="EXP 31/05/27 L0526", sure=True))
    msg = FakePhotoMessage(jpeg(mat_frame(200, 100)), 101 + asked)
    update = SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(handlers.on_photo(update, context(cfg, bot)))
    assert "The invoice says lot L-0499" in bot.edits[-1][1]
    assert len(sources.lot_flags(db.connect(cfg.db_path))) == 1
