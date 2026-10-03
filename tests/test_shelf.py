"""The shelf in use: quick counts, the shelf check and put-away in Telegram, the /shelf page, the Mini App."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from luydek import clock, db
from luydek.bot import digest, handlers
from luydek.services import batches, locations, stock
from luydek.web.server import create_app
from tests.conftest import pid
from tests.test_bot import CHAT, FakeBot, context, register, tap
from tests.test_miniapp import TOKEN, launch
from tests.test_summary import FakeMessage


def expiring(conn, sku="dutchlady-choc", days=-2, qty=6, place=None):
    """A batch that expires `days` from today (negative: already expired)."""
    loc = locations.add(conn, place) if place else None
    return stock.record(conn, pid(conn, sku), qty, "delivery", location_id=loc,
                        expiry_date=(clock.today() + timedelta(days=days)).isoformat(),
                        at=clock.now() - timedelta(days=30))


def test_a_quick_count_is_one_tap_and_every_phone_sees_the_answer(conn, cfg):
    register(conn)
    conn.execute("INSERT INTO bot_users (chat_id, name, lang, created_at) VALUES (778, 'Sokha', 'km', '2026-09-27')")
    p = pid(conn, "sting-330")
    stock.record(conn, p, 5, "opening", at=clock.now() - timedelta(days=9))
    for d in range(4):
        stock.record(conn, p, -1, "sale", at=clock.now() - timedelta(days=d))
    conn.commit()
    bot = FakeBot()
    assert asyncio.run(digest.send_spot_checks(bot, cfg, n=1)) == 1
    asks = [s for s in bot.sent if "Quick count" in (s[2] or "")]
    assert len(asks) == 1 and "LuyDek has 1" in asks[0][2] and len(bot.sent) == 2       # both phones asked
    datas = [b.callback_data for row in asks[0][3].inline_keyboard for b in row]
    assert datas == [f"k:{p}:0", f"k:{p}:1", f"k:{p}:2", f"k:{p}:o"]
    tap(cfg, bot, f"k:{p}:1")                                      # right as recorded
    assert len(bot.edits) == 2 and all("just as recorded" in e[1] or "ត្រឹមត្រូវ" in e[1] for e in bot.edits)
    assert stock.on_hand(db.connect(cfg.db_path))[p] == 1
    tap(cfg, bot, f"k:{p}:0")                                      # answered already: nothing changes
    assert stock.on_hand(db.connect(cfg.db_path))[p] == 1


def test_another_number_is_typed_in_reply_in_khmer_digits_too(conn, cfg):
    register(conn)
    p = pid(conn, "coke-330")
    stock.record(conn, p, 24, "opening", at=clock.now() - timedelta(days=9))
    conn.commit()
    bot = FakeBot()
    asyncio.run(digest.send_spot_checks(bot, cfg, n=1))
    tap(cfg, bot, f"k:{p}:o")
    prompt_id = bot._next                                          # the "How many are there?" message
    msg = FakeMessage("២០")                                        # 20, in Khmer digits
    msg.reply_to_message = SimpleNamespace(message_id=prompt_id)
    msg.chat = SimpleNamespace(id=CHAT)
    update = SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(handlers.on_text(update, context(cfg, bot)))
    assert stock.on_hand(db.connect(cfg.db_path))[p] == 20
    assert any("corrected by -4" in (e[1] or "") for e in bot.edits)


def test_the_shelf_check_in_telegram_and_its_removed_button(conn, cfg):
    register(conn)
    old = expiring(conn, place="Fridge")
    conn.commit()
    bot = FakeBot()
    assert asyncio.run(digest.send_shelf_check(bot, cfg, scheduled=True)) == 1
    _, _, text, markup = bot.sent[-1]
    assert "Fridge" in text and "Dutch Lady UHT milk chocolate 180ml ×6: expired" in text
    [btn] = [b for row in markup.inline_keyboard for b in row]
    assert btn.callback_data == f"w:{old}:r"
    tap(cfg, bot, f"w:{old}:r")
    c = db.connect(cfg.db_path)
    assert stock.on_hand(c)[pid(c, "dutchlady-choc")] == 0
    assert "nothing expires" in bot.edits[-1][1]                   # the list redrawn without it
    assert asyncio.run(digest.send_shelf_check(bot, cfg, scheduled=True)) == 0     # on schedule: quiet when done


def test_a_confirmed_invoice_says_where_to_put_it(conn, cfg):
    register(conn)
    fridge = locations.add(conn, "Fridge")
    milk = pid(conn, "dutchlady-choc")
    locations.set_home(conn, milk, fridge)
    stock.record(conn, milk, 6, "delivery", expiry_date="2026-10-12", at=clock.now() - timedelta(days=5))
    inv = conn.execute("INSERT INTO invoices (status, received_at) VALUES ('extracted', '2026-09-27')").lastrowid
    conn.execute("INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total, "
                 "expiry_date) VALUES (?, ?, 'DL choc', 1, 'case', 24, '2027-01-20')", (inv, milk))
    conn.commit()
    bot = FakeBot()
    tap(cfg, bot, f"i:{inv}:ok")
    text = bot.sent[-1][2]
    assert "Where to put it" in text and "×24 (exp 20/01/27) → Fridge, behind the 6 already there (exp 12/10/26)" in text


def test_stock_alerts_reach_every_phone_once(conn, cfg):
    register(conn)
    p = pid(conn, "sting-330")
    stock.record(conn, p, 2, "opening", at=clock.now() - timedelta(days=3))
    stock.record(conn, p, -2, "sale")
    conn.commit()
    bot = FakeBot()
    assert asyncio.run(digest.send_stock_alerts(bot, cfg)) == 1
    assert "Out of stock: Sting energy drink 330ml" in bot.sent[-1][2]
    assert asyncio.run(digest.send_stock_alerts(bot, cfg)) == 0


def client_for(cfg, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg.telegram.enabled = False
    return TestClient(create_app(cfg))


def test_the_shelf_page_and_its_actions(cfg, conn, stub_encoder, monkeypatch):
    old = expiring(conn, place="Fridge")
    soon = expiring(conn, "vitc-1000", days=10, qty=3)
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get("/shelf").text
        assert "Expired: take it off" in page and "Won&#39;t sell in time" in page and "Fridge" in page
        c.post("/shelf/places/suggested")
        shelf_b = next(r["id"] for r in locations.places(db.connect(cfg.db_path)) if r["name"] == "Shelf B")
        assert c.post(f"/batches/{soon}/edit", data={"location_id": shelf_b, "expiry": "2026-10-20"}).status_code == 200
        b = batches.get(db.connect(cfg.db_path), soon)
        assert (b.location_id, b.expiry) == (shelf_b, "2026-10-20")
        c.post(f"/batches/{old}/remove", data={"qty": "6"})
        vit = pid(conn, "vitc-1000")
        c.post("/shelf/homes", data={f"home_{vit}": shelf_b})
        product = c.get(f"/catalog/{vit}").text
        assert "On the shelf, by batch" in product and 'selected>Shelf B' in product
    assert stock.on_hand(db.connect(cfg.db_path))[pid(conn, "dutchlady-choc")] == 0


def test_the_mini_app_shelf_check(cfg, conn, stub_encoder, monkeypatch):
    register(conn)
    old = expiring(conn, place="Fridge")
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        c.headers["X-Telegram-Init-Data"] = launch()
        s = c.get("/api/tg/stock").json()
        [w] = s["walk"]
        assert (w["batch"], w["action"], w["place"], w["left"]) == (old, "remove", "Fridge", 6)
        milk = next(p for p in s["products"] if p["id"] == pid(conn, "dutchlady-choc"))
        assert milk["batches"][0]["place"] == "Fridge"
        assert c.post(f"/api/tg/batches/{old}/remove").json() == {"removed": 6}
        assert c.get("/api/tg/stock").json()["walk"] == []
