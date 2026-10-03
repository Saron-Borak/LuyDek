"""Faster onboarding: hold-to-record photos, products learned while selling, products from invoices."""

import asyncio
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from luydek import db
from luydek.bot import digest, handlers
from luydek.recognition import labels, pipeline
from luydek.services import catalog, forecast, learn, stock, transactions
from luydek.web.server import create_app
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid
from tests.test_bot import CHAT, FakeBot, register, tap
from tests.test_bot import context as bot_context
from tests.test_labels_report import FakeClient
from tests.test_recognition import enroll_all

ARM = (60, 90, 150)  # BGR: a brown hand-and-arm reaching in from the edge of the mat


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


@pytest.fixture
def calibrated(conn, cfg):
    pipeline.save_reference(conn, cfg, jpeg(mat_frame()))
    conn.commit()


def arm(frame, reach=260):
    cv2.rectangle(frame, (0, 200), (reach, 260), ARM, -1)


# --- hold to record -------------------------------------------------------------------------------

def test_a_burst_keeps_new_views_and_skips_repeats(conn, cfg, stub_encoder, calibrated):
    rec = pipeline.get_recognizer(cfg)
    coke = pack_of(conn, "coke-330")
    frame = mat_frame()
    paint(frame, "coke-330", 250, 150)
    gid, _ = rec.enroll(conn, frame, coke, burst=True)
    assert gid
    with pytest.raises(pipeline.Skipped, match="same"):
        rec.enroll(conn, frame, coke, burst=True)               # the item hasn't been turned
    rec.enroll(conn, frame, coke)                               # a deliberate tap is always kept
    assert conn.execute("SELECT COUNT(*) FROM gallery_images WHERE pack_id = ?", (coke,)).fetchone()[0] == 2


def test_a_burst_never_learns_the_hand(conn, cfg, stub_encoder, calibrated):
    rec = pipeline.get_recognizer(cfg)
    sprite = pack_of(conn, "sprite-1500")
    only_hand = mat_frame()
    arm(only_hand, 330)
    with pytest.raises(pipeline.Skipped, match="hand"):
        rec.enroll(conn, only_hand, sprite, burst=True)
    with pytest.raises(pipeline.Skipped, match="empty"):
        rec.enroll(conn, mat_frame(), sprite, burst=True)       # lifted off the mat for a moment
    both = mat_frame()
    paint(both, "sprite-1500", 420, 120)
    arm(both, 200)                                              # the hand is bigger, but the product is kept
    gid, (x, y, w, h) = rec.enroll(conn, both, sprite, burst=True)
    assert x > 300
    # Without burst mode a tap with nothing on the mat still says so plainly.
    with pytest.raises(ValueError, match="Nothing found on the mat"):
        rec.enroll(conn, mat_frame(), sprite)


def test_the_photo_api_reports_skipped_frames(client, conn, calibrated):
    frame = mat_frame()
    paint(frame, "dumex-400", 250, 150)
    form = {"pack_id": pack_of(conn, "dumex-400"), "use_reference": "true", "burst": "true"}
    first = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(frame), "image/jpeg")}, data=form).json()
    assert first["photos"] == 1 and "skipped" not in first
    again = client.post("/api/enroll", files={"image": ("s.jpg", jpeg(frame), "image/jpeg")}, data=form)
    assert again.status_code == 200 and again.json() == {"skipped": "same", "own_background": False}


def test_the_photo_page_records_while_the_button_is_held(client, conn):
    page = client.get("/catalog/enroll", params={"pack_id": pack_of(conn, "coke-330")}).text
    assert "Hold the button down" in page and 'id="burst-n"' in page
    assert 'addEventListener("pointerdown"' in page and 'f.append("burst"' in page


# --- new products learned while selling ---------------------------------------------------------

def crop(cfg, name, body=(150, 60, 170), band=(250, 160, 40)):
    """A painted product photo the catalog has never seen (purple with an orange band, by default)."""
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    im = np.full((120, 80, 3), 235, np.uint8)
    im[10:110, 10:70] = body
    im[45:65, 10:70] = band
    Image.fromarray(im).save(cfg.media_dir / "crops" / name)
    return f"crops/{name}"


def unknown_sale(conn, cfg, name, **colours):
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(
        None, 0.5, "low", "image", (0, 0, 80, 120), crop(cfg, name, **colours), [])])
    transactions.close_transaction(conn, txn)
    conn.commit()
    return conn.execute("SELECT MAX(id) FROM transaction_items").fetchone()[0]


def test_naming_an_unknown_sale_makes_a_product_that_is_not_counted_yet(conn, cfg, stub_encoder):
    item_id = unknown_sale(conn, cfg, "a.jpg")
    result = learn.add_from_item(conn, item_id, "  Oishi   honey lemon 380ml ", by="Dara",
                                 learn=pipeline.learner(cfg), media_dir=cfg.media_dir)
    assert result["created"] and result["name"] == "Oishi honey lemon 380ml"
    new = result["product_id"]
    row = conn.execute("SELECT origin, name_km, supplier_id FROM products WHERE id = ?", (new,)).fetchone()
    assert (row["origin"], row["name_km"], row["supplier_id"]) == ("counter", "Oishi honey lemon 380ml", None)
    assert transactions.item(conn, item_id)["status"] == "corrected"
    assert stock.on_hand(conn)[new] == -1                                   # the sale is kept as demand...
    line = next(r for r in stock.stock_table(conn) if r["id"] == new)
    assert line["status"] == "new" and not line["negative"] and line["sold_uncounted"] == 1  # ...not as a count
    assert conn.execute("SELECT COUNT(*) FROM gallery_images g JOIN product_packs pk ON pk.id = g.pack_id "
                        "WHERE pk.product_id = ?", (new,)).fetchone()[0] == 1   # the camera learned the photo
    assert [p["name_en"] for p in learn.unfinished(conn)] == ["Oishi honey lemon 380ml"]

    # With a supplier it joins the order list, but nothing is ordered before a first count.
    catalog.update_product(conn, new, supplier_id=1)
    plan = next(p for p in forecast.build_plans(conn, cfg.forecast) if p.supplier_id == 1)
    assert next(ln for ln in plan.lines if ln.product_id == new).action in ("check", "skip")
    stock.opening_count(conn, {new: 10})
    assert next(r for r in stock.stock_table(conn) if r["id"] == new)["status"] != "new"


def test_a_name_that_is_already_a_product_records_that_product(conn, cfg, stub_encoder):
    item_id = unknown_sale(conn, cfg, "b.jpg")
    before = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    result = learn.add_from_item(conn, item_id, "coca-cola CAN 330ml", by="Dara")
    assert not result["created"] and result["product_id"] == pid(conn, "coke-330")
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == before
    with pytest.raises(ValueError):
        learn.add_from_item(conn, unknown_sale(conn, cfg, "c.jpg"), "   ", by="Dara")


def test_one_name_re_asks_about_look_alike_sales(conn, cfg, stub_encoder):
    enroll_all(conn, cfg, skus=("coke-330",), shots=2)
    first = unknown_sale(conn, cfg, "d1.jpg")
    later = unknown_sale(conn, cfg, "d2.jpg")                   # the same new product, sold again
    set_aside = unknown_sale(conn, cfg, "d3.jpg")
    transactions.resolve(conn, set_aside, "unknown", by="Dara")  # "Not now" yesterday
    other = unknown_sale(conn, cfg, "e.jpg", body=(40, 170, 60), band=(30, 30, 30))  # something else
    result = learn.add_from_item(conn, first, "Oishi honey lemon 380ml", by="Dara", learn=pipeline.learner(cfg))
    changed = pipeline.regrouper(cfg)(conn, result["pack_id"], skip=first)
    assert sorted(changed) == sorted([later, set_aside])
    for item_id in (later, set_aside):
        row = transactions.item(conn, item_id)
        assert (row["status"], row["pack_id"], row["tier"]) == ("pending", result["pack_id"], "medium")  # asked
    assert transactions.item(conn, other)["pack_id"] is None
    assert transactions.confirm_many(conn, changed, by="Dara") == 2        # one "All correct"
    assert stock.on_hand(conn)[result["product_id"]] == -3


def test_the_package_is_read_for_a_name(conn, cfg):
    item_id = unknown_sale(conn, cfg, "f.jpg")
    reading = labels.NameReading(name_en="Oishi honey lemon 380ml", name_km="អូអ៊ីស៊ី", sure=True)
    assert labels.name_new(cfg, client=FakeClient(reading)) == 1
    row = transactions.item(db.connect(cfg.db_path), item_id)
    assert (row["name_guess"], row["name_guess_km"]) == ("Oishi honey lemon 380ml", "អូអ៊ីស៊ី")
    assert labels.name_new(cfg, client=FakeClient(reading)) == 0            # each item is read once

    # A name that is already a product: that product becomes the first suggestion instead.
    known = unknown_sale(db.connect(cfg.db_path), cfg, "g.jpg")
    labels.name_new(cfg, client=FakeClient(labels.NameReading(name_en="Ichitan green tea 420ml", name_km="",
                                                              sure=True)))
    row = transactions.item(db.connect(cfg.db_path), known)
    assert row["name_guess"] is None and transactions.candidates(row)[0][0] == pack_of(conn, "ichitan-420")


def test_today_offers_new_product_and_a_name_later_list(client, conn, cfg):
    item_id = unknown_sale(conn, cfg, "h.jpg")
    conn.execute("UPDATE transaction_items SET name_guess = 'Oishi honey lemon 380ml' WHERE id = ?", (item_id,))
    conn.commit()
    page = client.get("/").text
    assert f'action="/items/{item_id}/new-product"' in page and "Add Oishi honey lemon 380ml" in page
    assert "New product" in page and "Not now, ask me later" in page

    client.post(f"/items/{item_id}/resolve", data={"action": "unknown"})       # not now
    page = client.get("/fragments/checks").text
    assert "New products to name" in page and f'data-item="{item_id}"' in page

    r = client.post(f"/items/{item_id}/new-product", data={"name": "Oishi honey lemon 380ml"},
                    headers={"Accept": "application/json"})
    assert r.status_code == 200 and r.json()["created"] and r.json()["status"] == "corrected"
    page = client.get("/").text
    assert "has no supplier yet" in page and 'href="/catalog#learned"' in page
    assert "not counted · 1 sold" in page
    assert "from a sale" in client.get("/catalog").text


def test_a_set_aside_sale_can_turn_out_to_be_a_listed_product(client, conn, cfg):
    item_id = unknown_sale(conn, cfg, "i.jpg")
    client.post(f"/items/{item_id}/resolve", data={"action": "unknown"})
    r = client.post(f"/items/{item_id}/new-product", data={"pack_id": pack_of(conn, "sprite-1500")},
                    headers={"Accept": "application/json"})
    assert r.json()["status"] == "corrected" and not r.json()["created"]
    assert stock.on_hand(db.connect(cfg.db_path))[pid(conn, "sprite-1500")] == -1


def test_telegram_adds_a_new_product_by_tap_or_by_reply(conn, cfg, stub_encoder):
    register(conn)
    bot = FakeBot()
    guessed = unknown_sale(conn, cfg, "j.jpg")
    conn.execute("UPDATE transaction_items SET name_guess = 'Oishi honey lemon 380ml' WHERE id = ?", (guessed,))
    conn.commit()
    asyncio.run(digest.send_digest(bot, cfg))
    photo = next(s for s in bot.sent if s[0] == "photo")
    buttons = [b.text for row in photo[3].inline_keyboard for b in row]
    assert "🆕 New product" in buttons and any(b.startswith("➕ Add Oishi") for b in buttons)
    assert "The package reads “Oishi honey lemon 380ml”" in photo[2]
    tap(cfg, bot, f"v:{guessed}:ng")
    assert transactions.item(db.connect(cfg.db_path), guessed)["status"] == "corrected"
    assert "Added Oishi honey lemon 380ml" in bot.sent[-1][2]

    typed = unknown_sale(conn, cfg, "k.jpg", body=(20, 90, 200), band=(240, 240, 240))
    tap(cfg, bot, f"v:{typed}:n")
    assert bot.sent[-1][2].startswith(f"#{typed} · What is it?")
    replies = []

    async def reply_text(text):
        replies.append(text)

    update = SimpleNamespace(message=SimpleNamespace(
        text="Pocari Sweat 500ml", chat=SimpleNamespace(id=CHAT), reply_text=reply_text,
        reply_to_message=SimpleNamespace(message_id=bot._next)))
    asyncio.run(handlers.on_text(update, bot_context(cfg, bot)))
    row = transactions.item(db.connect(cfg.db_path), typed)
    assert row["status"] == "corrected" and row["name_en"] == "Pocari Sweat 500ml"
    assert replies and "Added Pocari Sweat 500ml" in replies[0]
