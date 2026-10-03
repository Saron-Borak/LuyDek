"""Bot flows against a fake Telegram: digests, taps, adjustments, registration, order lists."""

import asyncio
from types import SimpleNamespace

from PIL import Image

from luydek import db
from luydek.bot import digest, handlers, keyboards
from luydek.services import stock, transactions
from tests.conftest import pack_of, pid

CHAT = 777


class FakeBot:
    username = "luydek_test_bot"

    def __init__(self):
        self.sent, self.edits, self._next = [], [], 100

    def _msg(self):
        self._next += 1
        return SimpleNamespace(message_id=self._next)

    async def send_message(self, chat_id, text, reply_markup=None, parse_mode=None):
        self.sent.append(("text", chat_id, text, reply_markup))
        return self._msg()

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None):
        self.sent.append(("photo", chat_id, caption, reply_markup))
        return self._msg()

    async def send_media_group(self, chat_id, media):
        self.sent.append(("album", chat_id, [m.caption for m in media], None))
        return [self._msg() for _ in media]

    async def edit_message_caption(self, chat_id, message_id, caption=None, reply_markup=None):
        self.edits.append((message_id, caption, reply_markup))

    async def edit_message_text(self, text, chat_id, message_id, parse_mode=None, reply_markup=None):
        self.edits.append((message_id, text, reply_markup))


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT))
        self.answers, self.edits = [], []

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text)

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edits.append((None, reply_markup))


def context(cfg, bot, args=()):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"cfg": cfg}), bot=bot, args=list(args))


def tap(cfg, bot, data):
    query = FakeQuery(data)
    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=CHAT))
    asyncio.run(handlers.on_callback(update, context(cfg, bot)))
    return query


def register(conn, lang="en"):
    conn.execute("INSERT INTO bot_users (chat_id, name, lang, created_at) VALUES (?, 'Dara', ?, '2026-09-27')",
                 (CHAT, lang))
    conn.commit()


def pending_sale(conn, cfg, sku="ichitan-420", alt="oishi-380"):
    (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 60), "green").save(cfg.media_dir / "crops" / "c.jpg")
    guess, other = pack_of(conn, sku), pack_of(conn, alt)
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [transactions.DetectionRecord(
        guess, 0.84, "medium", "image", (0, 0, 40, 60), "crops/c.jpg", [(guess, 0.84), (other, 0.83)])])
    transactions.close_transaction(conn, txn)
    conn.commit()
    return transactions.pending_items(conn)[0]["id"]


def test_callback_data_fits_telegram_limit(conn):
    big = 10 ** 12
    markup = keyboards.quantities(f"a:q:phone_order:{big}", keyboards.QTY_CHOICES, "km")
    datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    datas += [f"v:{big}:p:{big}", f"il:{big}:p:{big}", f"b:{big}:q:{big}:24", f"a:l:credit_sale:{big}"]
    assert all(len(d.encode()) <= 64 for d in datas)


def test_digest_sends_header_photo_and_confirm_all(conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    bot = FakeBot()
    assert asyncio.run(digest.send_digest(bot, cfg)) == 1
    kinds = [s[0] for s in bot.sent]
    assert kinds == ["text", "photo", "text"]
    header, photo, confirm = bot.sent
    assert "1 need a quick check" in header[2]
    assert "Is this Ichitan green tea 420ml?" in photo[2]
    buttons = [b.text for row in photo[3].inline_keyboard for b in row]
    assert "✅ Yes" in buttons and "Oishi green tea 380ml" in buttons  # the look-alike is one tap away
    assert confirm[3].inline_keyboard[0][0].callback_data.startswith("d:")
    # Already sent: the next scheduled digest does not repeat it.
    bot2 = FakeBot()
    asyncio.run(digest.send_digest(bot2, cfg))
    assert not [s for s in bot2.sent if s[0] == "photo"]
    assert transactions.item(conn, item_id)["digest_id"] is not None


def test_tapping_the_look_alike_corrects_the_sale_everywhere(conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    bot = FakeBot()
    asyncio.run(digest.send_digest(bot, cfg))
    tap(cfg, bot, f"v:{item_id}:p:{pack_of(conn, 'oishi-380')}")
    check = db.connect(cfg.db_path)
    assert transactions.item(check, item_id)["status"] == "corrected"
    assert stock.on_hand(check)[pid(check, "oishi-380")] == -1
    # The photo message was edited to show the result, with its buttons removed.
    assert any("Corrected: Oishi" in (e[1] or "") and e[2] is None for e in bot.edits)
    assert check.execute("SELECT COUNT(*) FROM taps").fetchone()[0] == 1


def test_confirm_all_settles_the_digest(conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    bot = FakeBot()
    asyncio.run(digest.send_digest(bot, cfg))
    digest_id = transactions.item(conn, item_id)["digest_id"]
    query = tap(cfg, bot, f"d:{digest_id}")
    assert "Confirmed 1 items" in query.edits[0][0]
    assert transactions.item(db.connect(cfg.db_path), item_id)["status"] == "confirmed"


def test_other_opens_a_numbered_grid(conn, cfg, stub_encoder):
    register(conn)
    item_id = pending_sale(conn, cfg)
    bot = FakeBot()
    tap(cfg, bot, f"v:{item_id}:o:0")
    question = bot.sent[-1]
    assert question[2] == f"Which one is #{item_id}?"
    labels = [b.text for row in question[3].inline_keyboard for b in row]
    assert labels[0].startswith("1. Ichitan") and labels[1].startswith("2. Oishi")
    assert "▶ More" in labels and "🆕 New product" in labels


def test_adjustment_is_three_taps(conn, cfg):
    register(conn)
    bot = FakeBot()
    coke = pid(conn, "coke-330")
    q = tap(cfg, bot, "a:r:credit_sale")
    assert "Which product?" in q.edits[0][0]
    tap(cfg, bot, f"a:p:credit_sale:{coke}")
    q = tap(cfg, bot, f"a:q:credit_sale:{coke}:3")
    assert "Recorded" in q.edits[0][0]
    row = db.connect(cfg.db_path).execute("SELECT type, qty FROM stock_movements").fetchone()
    assert tuple(row) == ("sale", -3)


def test_registration_needs_the_shop_pin(conn, cfg):
    replies = []
    msg = SimpleNamespace(reply_text=lambda text, reply_markup=None: _record(replies, text))
    update = SimpleNamespace(message=msg, effective_message=msg, effective_chat=SimpleNamespace(id=CHAT),
                             effective_user=SimpleNamespace(first_name="Srey"))
    asyncio.run(handlers.cmd_start(update, context(cfg, FakeBot(), ["0000"])))
    assert "PIN" in replies[-1]
    asyncio.run(handlers.cmd_start(update, context(cfg, FakeBot(), [cfg.shop.pin])))
    assert db.connect(cfg.db_path).execute("SELECT name, lang FROM bot_users").fetchone()[:] == ("Srey", "km")
    assert "សួស្តី" in replies[-1]


async def _record(replies, text):
    replies.append(text)


def test_strangers_cannot_tap(conn, cfg):
    item_id = pending_sale(conn, cfg)
    query = tap(cfg, FakeBot(), f"v:{item_id}:y")
    assert query.answers and transactions.item(conn, item_id)["status"] == "pending"


def test_order_lists_go_out_in_khmer_with_the_simulated_warning(conn, cfg):
    register(conn, lang="km")
    stock.record(conn, pid(conn, "vitc-1000"), 14, "opening", simulated=True)
    conn.commit()
    bot = FakeBot()
    assert asyncio.run(digest.send_orders(bot, cfg)) == 4
    texts = [s[2] for s in bot.sent]
    assert all("simulated" in x for x in texts)
    assert any("ថ្ងៃច័ន្ទ" in x for x in texts)   # Pharma agent comes Monday


def test_end_of_day_closes_open_questions(conn, cfg):
    register(conn)
    item_id = pending_sale(conn, cfg)
    bot = FakeBot()
    asyncio.run(digest.send_digest(bot, cfg))
    assert asyncio.run(digest.end_of_day(bot, cfg)) == 1
    assert any("best guess" in (e[1] or "") for e in bot.edits)
    assert transactions.item(db.connect(cfg.db_path), item_id)["status"] == "best_guess"


def test_items_wait_for_the_first_verifier(conn, cfg):
    item_id = pending_sale(conn, cfg)
    assert asyncio.run(digest.send_digest(FakeBot(), cfg)) == 0
    assert transactions.item(conn, item_id)["digest_id"] is None
    register(conn)
    bot = FakeBot()
    assert asyncio.run(digest.send_digest(bot, cfg)) == 1
