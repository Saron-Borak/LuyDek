"""The Telegram Mini App: signed launch data, signed photo links, swiping answers, fixing recorded sales."""

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from luydek import db
from luydek.bot import digest
from luydek.services import stock, transactions
from luydek.web import miniapp
from luydek.web.server import create_app
from tests.conftest import pack_of, pid
from tests.test_bot import CHAT, FakeBot, pending_sale, register
from tests.test_detection import recorded_sale, unknown_sale_row

TOKEN = "123456:TEST-token"


def launch(user_id=CHAT, token=TOKEN, age=0):
    return miniapp.sign_init_data({"auth_date": str(int(time.time()) - age), "query_id": "AAE",
                                   "user": json.dumps({"id": user_id, "first_name": "Dara"})}, token)


@pytest.fixture
def app_client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg.telegram.enabled = False            # the token checks the app's requests; no bot polls in tests
    with TestClient(create_app(cfg)) as c:
        c.headers["X-Telegram-Init-Data"] = launch()
        yield c


def test_launch_data_must_be_signed_by_this_bot_and_recent():
    good = launch()
    assert miniapp.check_init_data(good, TOKEN)["id"] == CHAT
    assert miniapp.check_init_data(good, "999:other-bot") is None
    assert miniapp.check_init_data(good.replace("Dara", "Mallory"), TOKEN) is None     # tampered
    assert miniapp.check_init_data(launch(age=2 * 86400), TOKEN) is None                # too old
    assert miniapp.check_init_data("", TOKEN) is None


def test_photo_links_are_signed_and_expire():
    url = miniapp.image_url("item", 7, TOKEN, now=1000)
    q = dict(p.split("=") for p in url.split("?")[1].split("&"))
    assert miniapp.image_ok("item", 7, int(q["e"]), q["s"], TOKEN, now=1000)
    assert not miniapp.image_ok("item", 8, int(q["e"]), q["s"], TOKEN, now=1000)       # another photo
    assert not miniapp.image_ok("item", 7, int(q["e"]), q["s"], TOKEN, now=int(q["e"]) + 1)


def test_only_the_shops_phones_get_in(app_client, conn):
    assert app_client.get("/tg").status_code == 200 and "telegram-web-app.js" in app_client.get("/tg").text
    assert app_client.get("/api/tg/state", headers={"X-Telegram-Init-Data": ""}).status_code == 401
    assert app_client.get("/api/tg/state").status_code == 403                          # not registered yet
    register(conn)
    assert app_client.get("/api/tg/state").status_code == 200


def test_the_cards_and_a_swipe_right(app_client, conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    state = app_client.get("/api/tg/state").json()
    [c] = state["pending"]
    assert c["id"] == item_id and c["guess"]["label"] == "Ichitan green tea 420ml"
    assert c["candidates"][0]["label"] == "Oishi green tea 380ml" and c["photo"].startswith("/api/tg/img/item/")
    assert app_client.get(c["photo"]).status_code == 200                               # the signed photo loads
    assert app_client.get(c["photo"].replace("&s=", "&s=0")).status_code == 403
    assert state["products"] and state["lang"] == "en"

    r = app_client.post(f"/api/tg/items/{item_id}", data={"action": "yes", "count": 2})
    assert r.json()["status"] == "confirmed"
    check = db.connect(cfg.db_path)
    assert stock.on_hand(check)[pid(check, "ichitan-420")] == -2                      # the count was fixed too
    assert check.execute("SELECT action FROM taps").fetchone()[0] == "app:yes"


def test_swiping_left_corrects_it(app_client, conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    r = app_client.post(f"/api/tg/items/{item_id}", data={"action": "pick", "pack_id": pack_of(conn, "oishi-380")})
    assert r.json()["status"] == "corrected"
    other = unknown_sale_row(conn)
    conn.commit()
    r = app_client.post(f"/api/tg/items/{other}", data={"action": "several",
                                                         "picks": f"{pack_of(conn, 'coke-330')}:1,{pack_of(conn, 'dumex-400')}:1"})
    assert r.json()["status"] == "split"
    new = unknown_sale_row(conn)
    conn.commit()
    r = app_client.post(f"/api/tg/items/{new}", data={"action": "new", "name": "Pocari Sweat 500ml"})
    assert r.json()["status"] == "corrected" and r.json()["name"] == "Pocari Sweat 500ml"
    later = unknown_sale_row(conn)
    conn.commit()
    assert app_client.post(f"/api/tg/items/{later}", data={"action": "unknown"}).json()["status"] == "unknown"


def test_the_rest_are_right_in_one_tap(app_client, conn, cfg):
    register(conn)
    pending_sale(conn, cfg)
    pending_sale(conn, cfg)
    ids = ",".join(str(i["id"]) for i in transactions.pending_items(conn))
    assert app_client.post("/api/tg/confirm-all", data={"ids": ids}).json()["confirmed"] == 2


def test_recorded_today_can_be_fixed(app_client, conn, cfg):
    register(conn)
    item_id = recorded_sale(conn, pack_of(conn, "coke-330"))
    conn.commit()
    state = app_client.get("/api/tg/state").json()
    assert [c["id"] for c in state["recorded"]] == [item_id]
    r = app_client.post(f"/api/tg/items/{item_id}", data={"action": "fix-pick", "pack_id": pack_of(conn, "sprite-1500")})
    assert r.json()["status"] == "corrected"
    check = db.connect(cfg.db_path)
    assert stock.on_hand(check)[pid(check, "sprite-1500")] == -1
    assert stock.on_hand(check).get(pid(check, "coke-330"), 0) == 0


def test_the_digest_is_one_button_with_the_app(conn, cfg):
    register(conn)
    pending_sale(conn, cfg)
    cfg.telegram.miniapp_url = "https://shop-laptop.tail1234.ts.net/"
    bot = FakeBot()
    asyncio.run(digest.send_digest(bot, cfg))
    assert [s[0] for s in bot.sent] == ["text"]                                       # no photo per item
    buttons = [b for row in bot.sent[0][3].inline_keyboard for b in row]
    assert buttons[0].web_app.url == "https://shop-laptop.tail1234.ts.net/tg" and "Check 1 items" in buttons[0].text
    assert buttons[1].callback_data.startswith("d:")                                  # all correct: still one tap


def test_a_public_address_serves_only_the_app(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg.telegram.enabled = False
    cfg.telegram.miniapp_url = "https://luydek.example-tunnel.com"
    cfg.telegram.miniapp_host_only = True
    with TestClient(create_app(cfg), base_url="https://luydek.example-tunnel.com") as c:
        assert c.get("/").status_code == 404                                           # the dashboard stays private
        assert c.get("/tg").status_code == 200
    with TestClient(create_app(cfg)) as local:
        assert local.get("/").status_code == 200                                       # on this computer: all of it
