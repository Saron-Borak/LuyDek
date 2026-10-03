"""The day's summary (end of day and /today) and stock left (/stock, a typed name, the Mini App's Stock tab)."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from luydek import clock, render
from luydek.bot import app as bot_app
from luydek.bot import digest, handlers
from luydek.services import catalog, stock, summary
from luydek.web.server import create_app
from tests.conftest import pid
from tests.test_bot import CHAT, FakeBot, context, pending_sale, register, tap
from tests.test_miniapp import TOKEN, launch


@pytest.fixture
def day(conn, cfg):
    """A day at the shop: Coca-Cola sold at the counter, on credit and one put back; Ichitan nearly
    gone; Sprite delivered; one sale still to check. Yesterday's sales must not count."""
    coke, ichitan, sprite = pid(conn, "coke-330"), pid(conn, "ichitan-420"), pid(conn, "sprite-1500")
    yesterday = clock.now() - timedelta(days=1)
    stock.record(conn, coke, 48, "opening", at=yesterday - timedelta(hours=2))
    stock.record(conn, coke, -10, "sale", at=yesterday)
    stock.record(conn, coke, -12, "sale")
    stock.adjust(conn, coke, "credit_sale", 2)
    stock.adjust(conn, coke, "put_back", 1)
    stock.record(conn, ichitan, 5, "opening", at=yesterday)
    stock.record(conn, ichitan, -4, "sale")
    stock.record(conn, sprite, 24, "delivery")
    conn.commit()
    pending_sale(conn, cfg)
    return SimpleNamespace(coke=coke, ichitan=ichitan, sprite=sprite)


class FakeMessage:
    def __init__(self, text=""):
        self.text, self.reply_to_message, self.replies = text, None, []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))


def say(cfg, bot, text, args=None):
    """The owner types `text` (a menu button or a product name), or a /command with `args`."""
    msg = FakeMessage(text)
    update = SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(id=CHAT),
                             effective_user=SimpleNamespace(first_name="Dara"))
    ctx = context(cfg, bot, args or ())
    if args is not None:
        asyncio.run(handlers.cmd_stock(update, ctx))
    else:
        asyncio.run(handlers.on_text(update, ctx))
    return msg


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def test_the_day_counts_todays_sales_deliveries_and_what_is_running_out(conn, day):
    s = summary.day_summary(conn)
    sold = {r["id"]: r["sold"] for r in s["sold"]}
    assert sold == {day.coke: 13, day.ichitan: 4}           # 12 + 2 on credit - 1 put back; not yesterday's 10
    assert s["sold"][0]["id"] == day.coke and s["units"] == 17
    assert s["sold"][0]["on_hand"] == 25                     # 48 - 10 - 13
    assert (s["received_units"], s["received_products"]) == (24, 1)
    assert s["pending"] == 1 and s["txns"] == 1              # the camera sale still to check isn't in the ledger yet
    assert [r["id"] for r in s["watch"]] == [day.ichitan]     # 1 left at ~0.6 a day; never-counted products aren't listed


def test_the_summary_message_in_english_and_khmer(conn, day):
    s = summary.day_summary(conn)
    en = render.day_summary_message(s, "en")
    assert "Today · Sunday 27/09" in en and "1 sales · 17 items sold" in en
    assert "Coca-Cola can 330ml — 13 sold · 25 left" in en
    assert "RUNNING LOW" in en and "Ichitan green tea 420ml — 1 left" in en
    assert "1 still to check" in en and "Received +24" in en
    km = render.day_summary_message(s, "km")
    assert "លក់ 13 · នៅសល់ 25" in km and "ថ្ងៃអាទិត្យ" in km


def test_a_day_without_sales_says_so(conn):
    text = render.day_summary_message(summary.day_summary(conn), "en")
    assert "No sales recorded today" in text and "SOLD TODAY" not in text


def test_every_item_fits_telegram_messages(conn, day):
    rows = summary.stock_left(conn)
    messages = render.stock_list_messages(rows, "en", clock.today(), limit=400)
    assert len(messages) > 1 and all(len(m) <= 400 for m in messages)
    everything = "\n".join(messages)
    assert all(everything.count(f"• {r['name_en']} —") == 1 for r in rows)
    assert "Ichitan green tea 420ml — 1 left · 4 sold today" in everything
    assert everything.index("RUNNING LOW") < everything.index("NOT COUNTED YET") < everything.index("IN STOCK")


def test_one_products_stock(conn, day):
    row = summary.product_stock(conn, day.coke)
    text = render.product_stock_message(row, "en")
    assert "📦 25 left" in text and "must-have" in text
    assert "Sold 13 today · 23 in 7 days" in text           # yesterday's 10 fall inside the 7 days
    assert "Lasts about 8 more days" in text                  # 25 left at 23 a week
    assert "Coca-Cola agent comes" in text
    never = render.product_stock_message(summary.product_stock(conn, pid(conn, "lays-50")), "en")
    assert "Never counted" in never


def test_search_by_typed_name(conn):
    assert catalog.search(conn, "sprite") == [pid(conn, "sprite-1500")]
    assert set(catalog.search(conn, "coca")) == {pid(conn, "coke-330"), pid(conn, "coke-1500")}
    assert catalog.search(conn, "coca can") == [pid(conn, "coke-330")]
    assert catalog.search(conn, "") == []


def test_today_on_request_and_its_buttons(conn, cfg, day):
    register(conn)
    bot = FakeBot()
    say(cfg, bot, "📈 Sold today")
    [(_, chat, text, markup)] = bot.sent
    assert chat == CHAT and "13 sold · 25 left" in text
    assert [b.callback_data for b in buttons(markup)] == ["s:m"]      # no Mini App address in tests
    cfg.telegram.miniapp_url = "https://shop.example.ts.net"
    asyncio.run(digest.send_day_summary(bot, cfg, chat_id=CHAT))
    assert buttons(bot.sent[-1][3])[1].web_app.url == "https://shop.example.ts.net/tg?tab=stock"


def test_stock_left_one_product_or_every_item(conn, cfg, day):
    register(conn)
    bot = FakeBot()
    say(cfg, bot, "📋 Stock left")
    picker = bot.sent[-1][3]
    labels = {b.callback_data: b.text for b in buttons(picker)}
    assert labels["s:a"] == "📋 Every item"
    assert labels[f"s:p:{day.coke}"].endswith("· 25") and labels[f"s:p:{pid(conn, 'lays-50')}"].endswith("· ?")
    assert list(labels)[1] == f"s:p:{day.coke}"                       # best-sellers first
    assert all(len(d.encode()) <= 64 for d in labels)

    query = tap(cfg, bot, f"s:p:{day.ichitan}")
    assert "1 left · 🟠 running low" in query.edits[-1][0]

    before = len(bot.sent)
    tap(cfg, bot, "s:a")
    assert "Stock left · Sunday 27/09" in bot.sent[before][2]

    query = tap(cfg, bot, "s:l:1")                                     # the picker's second page
    assert query.edits[-1][0].startswith("📋 Stock left")


def test_a_typed_name_answers_with_its_stock(conn, cfg, day):
    register(conn)
    bot = FakeBot()
    [(text, _)] = say(cfg, bot, "sprite").replies
    assert text.startswith("<b>Sprite bottle 1.5L</b>") and "24 left" in text
    [(text, markup)] = say(cfg, bot, "coca").replies                  # two Coca-Colas: pick one
    assert "matching" in text and len(buttons(markup)) == 3
    [(text, _)] = say(cfg, bot, "xyzzy").replies
    assert "No product matches" in text
    [(text, _)] = say(cfg, bot, "", args=["sprite"]).replies        # /stock sprite
    assert "Sprite" in text


def test_strangers_get_no_stock(conn, cfg, day):
    bot = FakeBot()
    assert say(cfg, bot, "sprite").replies == [] and bot.sent == []


def test_end_of_day_sends_the_summary_after_settling_the_guesses(conn, cfg, day):
    register(conn)
    bot = FakeBot()
    asyncio.run(bot_app._job_end_of_day(context(cfg, bot)))
    text = bot.sent[-1][2]
    assert "Ichitan green tea 420ml — 5 sold" in text                # 4 + the unchecked sale, now a best guess
    assert "still to check" not in text

    cfg.verification.daily_summary = False
    bot = FakeBot()
    asyncio.run(bot_app._job_end_of_day(context(cfg, bot)))
    assert not any("Today ·" in (s[2] or "") for s in bot.sent)


def test_the_mini_apps_stock_tab(cfg, conn, day, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    cfg.telegram.enabled = False
    with TestClient(create_app(cfg)) as c:
        c.headers["X-Telegram-Init-Data"] = launch()
        assert c.get("/api/tg/stock").status_code == 403           # not registered yet
        register(conn)
        s = c.get("/api/tg/stock").json()
    assert (s["txns"], s["units"], s["pending"], s["received"], s["watch"]) == (1, 17, 1, 24, 1)
    by_id = {p["id"]: p for p in s["products"]}
    assert by_id[day.coke]["left"] == 25 and by_id[day.coke]["sold_today"] == 13
    assert by_id[day.ichitan]["status"] == "low" and not by_id[pid(conn, "lays-50")]["tracked"]
    assert s["products"][0]["id"] == day.ichitan                     # the most urgent first
