"""Telegram update handlers: commands, the reply-keyboard menu, button taps, invoice photos."""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import uuid
from datetime import date, timedelta

import anthropic
from PIL import Image
from telegram import ForceReply, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from .. import clock, db, render
from ..config import Config
from ..i18n import STRINGS, t
from ..recognition import labels, pipeline
from ..services import batches, catalog, invoices, learn, metrics, stock, transactions
from . import digest, keyboards

log = logging.getLogger("luydek.bot")


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


def _user(cfg: Config, chat_id: int):
    with db.session(cfg) as conn:
        return conn.execute("SELECT * FROM bot_users WHERE chat_id = ?", (chat_id,)).fetchone()


def registered(fn):
    """Only registered verifiers (owner, family, staff) can use the bot."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        cfg = _cfg(context)
        user = _user(cfg, update.effective_chat.id)
        if user is None:
            await update.effective_message.reply_text(t(cfg.shop.default_lang, "not_registered"))
            return
        return await fn(update, context, user)
    return wrapper


# --- commands ----------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    chat_id = update.effective_chat.id
    name = update.effective_user.first_name if update.effective_user else ""
    user = _user(cfg, chat_id)
    if user is None:
        if not context.args or context.args[0] != cfg.shop.pin:
            await update.message.reply_text(
                t("km", "need_pin") + "\n" + t("en", "need_pin"))
            return
        with db.session(cfg) as conn:
            conn.execute(
                "INSERT INTO bot_users (chat_id, name, lang, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, name, cfg.shop.default_lang, clock.stamp()),
            )
        user = _user(cfg, chat_id)
    lang = user["lang"]
    await update.message.reply_text(t(lang, "welcome", name=name), reply_markup=keyboards.main_menu(lang))


@registered
async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    lang = "en" if user["lang"] == "km" else "km"
    with db.session(_cfg(context)) as conn:
        conn.execute("UPDATE bot_users SET lang = ? WHERE chat_id = ?", (lang, user["chat_id"]))
    await update.message.reply_text(t(lang, "lang_set"), reply_markup=keyboards.main_menu(lang))


@registered
async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await digest.send_digest(context.bot, _cfg(context), manual=True)


@registered
async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await digest.send_orders(context.bot, _cfg(context), chat_id=user["chat_id"])


@registered
async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await digest.send_week_summary(context.bot, _cfg(context), chat_id=user["chat_id"])


@registered
async def cmd_adjust(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await update.effective_message.reply_text(t(user["lang"], "adjust_why"),
                                              reply_markup=keyboards.adjust_reasons(user["lang"]))


@registered
async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """/check: open the Mini App (or, without one, send the waiting items again)."""
    cfg = _cfg(context)
    if not cfg.telegram.miniapp_url:
        await digest.send_digest(context.bot, cfg, resend=True, manual=True)
        return
    with db.session(cfg) as conn:
        n = len(transactions.pending_items(conn))
    await update.message.reply_text(t(user["lang"], "app_intro"),
                                    reply_markup=keyboards.open_app(cfg.telegram.miniapp_url, user["lang"], n))


@registered
async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await update.effective_message.reply_text(t(user["lang"], "invoice_help"))


@registered
async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """/today: what sold today and what is left, now (it also comes by itself at the end of the day)."""
    await digest.send_day_summary(context.bot, _cfg(context), chat_id=user["chat_id"])


@registered
async def cmd_stock(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """/stock: pick a product to see what is left, or every item. /stock coca: straight to Coca-Cola."""
    query = " ".join(context.args or []).strip()
    if query:
        await _stock_search(update, context, user, query)
    else:
        await digest.send_stock_picker(context.bot, _cfg(context), user["chat_id"], user["lang"])


async def _stock_search(update: Update, context: ContextTypes.DEFAULT_TYPE, user, query: str) -> None:
    """A product's name typed in the chat: one match shows its stock, several are offered as buttons."""
    cfg, lang = _cfg(context), user["lang"]
    with db.session(cfg) as conn:
        found = catalog.search(conn, query)
    if len(found) == 1:
        await update.effective_message.reply_text(digest.product_stock_text(cfg, found[0], lang), parse_mode="HTML",
                                                  reply_markup=keyboards.product_stock_keyboard(lang))
    elif found:
        await update.effective_message.reply_text(t(lang, "stock_matches", q=query),
                                                  reply_markup=digest.stock_picker(cfg, lang, only=found))
    else:
        await update.effective_message.reply_text(t(lang, "stock_nomatch", q=query),
                                                  reply_markup=digest.stock_picker(cfg, lang))


@registered
async def cmd_shelf(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """/shelf: the walk round the shop now: what has expired, what goes in front."""
    await digest.send_shelf_check(context.bot, _cfg(context), chat_id=user["chat_id"])


@registered
async def cmd_count(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    """/count: a few quick counts now, instead of waiting for the afternoon."""
    cfg = _cfg(context)
    if not await digest.send_spot_checks(context.bot, cfg, chat_id=user["chat_id"], n=max(cfg.verification.spot_checks, 3)):
        await update.effective_message.reply_text("✅")


MENU = {
    key: cmd for key, cmd in (
        ("menu_invoice", cmd_invoice), ("menu_adjust", cmd_adjust),
        ("menu_orders", cmd_orders), ("menu_week", cmd_week),
        ("menu_today", cmd_today), ("menu_stock", cmd_stock), ("menu_shelf", cmd_shelf),
    )
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    if update.message.reply_to_message is not None and text and await _counted(update, context, text):
        return
    if update.message.reply_to_message is not None and text and await _named(update, context, text):
        return
    for key, handler in MENU.items():
        if text in STRINGS[key].values():
            await handler(update, context)
            return
    # Anything else typed is taken as a product's name: "how many Coca-Cola are left?"
    user = _user(_cfg(context), update.effective_chat.id)
    if user is not None and text:
        await _stock_search(update, context, user, text[:60])


# --- invoice photos ---------------------------------------------------------------------

@registered
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    cfg = _cfg(context)
    lang = user["lang"]
    msg = update.message
    if msg.reply_to_message is not None and await _expiry_photo(update, context, user):
        return
    source = msg.photo[-1] if msg.photo else msg.document
    rel = f"invoices/{uuid.uuid4().hex[:12]}.jpg"
    (cfg.media_dir / "invoices").mkdir(parents=True, exist_ok=True)
    tg_file = await source.get_file()
    await tg_file.download_to_drive(cfg.media_dir / rel)
    with db.session(cfg) as conn:
        invoice_id = invoices.create_from_photo(conn, rel, user["chat_id"])
    if not cfg.anthropic_key_present:
        with db.session(cfg) as conn:
            conn.execute("UPDATE invoices SET status = 'failed', error = 'no_key' WHERE id = ?", (invoice_id,))
        await msg.reply_text(t(lang, "invoice_no_key"))
        return
    working = await msg.reply_text(t(lang, "invoice_reading"))
    status = await asyncio.to_thread(invoices.process, cfg, invoice_id)
    if status == "extracted":
        await working.delete()
        await digest.send_invoice(context.bot, cfg, invoice_id, user["chat_id"], lang)
    elif status == "queued":
        await working.edit_text(t(lang, "invoice_queued"))
    else:
        await working.edit_text(t(lang, "invoice_failed"))


async def _expiry_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> bool:
    """A photo sent in reply to "when does it expire?": read the date off it. False if the reply was
    to something else (then the photo is an invoice, as usual)."""
    cfg, lang, msg = _cfg(context), user["lang"], update.message
    asked = msg.reply_to_message.message_id
    with db.session(cfg) as conn:
        ask = conn.execute("SELECT ref_id FROM bot_messages WHERE kind = 'expiry' AND chat_id = ? AND message_id = ?",
                           (user["chat_id"], asked)).fetchone()
    if ask is None:
        return False
    if not cfg.anthropic_key_present:
        await msg.reply_text(t(lang, "expiry_no_key"))
        return True
    buf = io.BytesIO()
    await (await (msg.photo[-1] if msg.photo else msg.document).get_file()).download_to_memory(buf)
    try:
        reading = await asyncio.to_thread(labels.read_expiry, Image.open(buf), cfg)
    except (anthropic.APIError, OSError) as exc:
        log.warning("date reader failed: %s", exc)
        reading = None
    if reading is None or not reading.sure or not reading.expiry:
        await msg.reply_text(t(lang, "expiry_unread"))
        return True
    with db.session(cfg) as conn:
        b = batches.get(conn, ask["ref_id"])
        if b is None:
            return True
        wrong_lot = batches.check_lot(conn, b.id, reading.lot)
        batches.update(conn, b.id, expiry=reading.expiry, **({"lot_no": reading.lot} if reading.lot else {}))
        p = catalog.product(conn, b.product_id)
        keeps = (date.fromisoformat(reading.expiry) - date.fromisoformat(b.received_at[:10])).days
        if p["shelf_life_days"] is None and keeps > 0:  # a guess for later deliveries that come with no date
            catalog.update_product(conn, p["id"], shelf_life_days=keeps)
    text = t(lang, "expiry_read", name=catalog.display_name(p, lang), n=b.left, date=render.short_date(reading.expiry),
             lot=t(lang, "expiry_lot", lot=reading.lot) if reading.lot else "")
    if wrong_lot:
        text += t(lang, "expiry_lot_mismatch", expected=wrong_lot["expected"],
                  supplier=wrong_lot["supplier"] or t(lang, "the_supplier"))
    try:
        await context.bot.edit_message_text(text, chat_id=user["chat_id"], message_id=asked)
    except BadRequest:
        await msg.reply_text(text)
    return True


# --- button taps -------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    cfg = _cfg(context)
    if query.data == "noop":
        await query.answer()
        return
    user = _user(cfg, query.message.chat.id)
    if user is None:
        await query.answer(t(cfg.shop.default_lang, "not_registered"), show_alert=True)
        return
    with db.session(cfg) as conn:
        metrics.log_tap(conn, user["chat_id"], query.data.split(":")[0])

    parts = query.data.split(":")
    handler = {"v": _verify, "d": _confirm_digest, "r": _resend, "a": _adjust,
               "i": _invoice, "il": _invoice_line, "b": _bonus, "s": _stock, "k": _count,
               "w": _shelf, "x": _expiry}.get(parts[0])
    if handler is None:
        await query.answer()
        return
    try:
        await handler(query, context, cfg, user, parts)
    except Exception:
        log.exception("callback %s failed", query.data)
        await query.answer("⚠️", show_alert=False)


def _resolve(cfg: Config, item_id: int, action: str, **kwargs) -> dict:
    with db.session(cfg) as conn:
        return transactions.resolve(
            conn, item_id, action, learn=pipeline.learner(cfg), media_dir=cfg.media_dir,
            delete_photo=cfg.verification.delete_photos_after_verify, **kwargs,
        )


async def _verify(query, context, cfg: Config, user, parts: list[str]) -> None:
    item_id, action = int(parts[1]), parts[2]
    lang = user["lang"]
    if action == "o":
        await query.answer()
        await digest.send_grid(context.bot, cfg, user["chat_id"], lang, item_id, int(parts[3]))
        return
    kwargs = {"by": user["name"] or str(user["chat_id"])}
    if action == "p":
        kwargs["pack_id"] = int(parts[3])
    elif action == "q":
        kwargs["delta"] = int(parts[3])
    if action == "n":  # a new product: ask for its name as a reply, so no menu is in the way
        await query.answer()
        msg = await context.bot.send_message(user["chat_id"], t(lang, "ask_name", n=item_id),
                                             reply_markup=ForceReply(selective=True))
        with db.session(cfg) as conn:
            digest.remember(conn, "name_prompt", item_id, user["chat_id"], msg.message_id)
        return
    if action == "ng":  # add it under the name read off the package
        with db.session(cfg) as conn:
            row = transactions.item(conn, item_id)
        guess = row["name_guess"] if row is not None else None
        if not guess:
            await query.answer()
            return
        await query.answer()
        await _add_product(context, cfg, user, item_id, guess, row["name_guess_km"] or "")
        return
    verb = {"y": "yes", "p": "pick", "q": "qty", "x": "remove", "u": "unknown"}[action]
    result = await asyncio.to_thread(_resolve, cfg, item_id, verb, **kwargs)
    await query.answer(t(lang, "already") if result["already"] else None)
    await digest.refresh_item(context.bot, cfg, item_id)


async def _named(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """A reply to "What is it?": the name of a new product. False if the reply was to something else."""
    cfg = _cfg(context)
    user = _user(cfg, update.message.chat.id)
    if user is None:
        return False
    with db.session(cfg) as conn:
        prompt = conn.execute(
            "SELECT ref_id FROM bot_messages WHERE kind = 'name_prompt' AND chat_id = ? AND message_id = ?",
            (user["chat_id"], update.message.reply_to_message.message_id),
        ).fetchone()
    if prompt is None:
        return False
    await _add_product(context, cfg, user, prompt["ref_id"], text, "", reply_to=update.message)
    return True


async def _add_product(context, cfg: Config, user, item_id: int, name: str, name_km: str, reply_to=None) -> None:
    """Make the product, record the sale and learn the photo; re-ask look-alike sales about it."""
    lang = user["lang"]

    def run() -> dict:
        with db.session(cfg) as conn:
            result = learn.add_from_item(
                conn, item_id, name, name_km, by=user["name"] or str(user["chat_id"]), learn=pipeline.learner(cfg),
                media_dir=cfg.media_dir, delete_photo=cfg.verification.delete_photos_after_verify)
            regroup = pipeline.regrouper(cfg)
            result["regrouped"] = regroup(conn, result["pack_id"], skip=item_id) if regroup and not result["already"] else []
            return result

    try:
        result = await asyncio.to_thread(run)
    except ValueError:
        return
    if result["already"]:
        text = t(lang, "already")
    else:
        text = t(lang, "added_product", name=result["name"])
        if result["regrouped"]:
            text += "\n" + t(lang, "added_more", n=len(result["regrouped"]))
    if reply_to is not None:
        await reply_to.reply_text(text)
    else:
        await context.bot.send_message(user["chat_id"], text)
    for changed in [item_id, *result["regrouped"]]:
        await digest.refresh_item(context.bot, cfg, changed)


async def _confirm_digest(query, context, cfg: Config, user, parts: list[str]) -> None:
    digest_id = int(parts[1])
    with db.session(cfg) as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM transaction_items WHERE digest_id = ? AND status = 'pending'", (digest_id,)
        )]

    def confirm() -> int:
        with db.session(cfg) as conn:
            return transactions.confirm_many(conn, ids, user["name"] or str(user["chat_id"]),
                                             media_dir=cfg.media_dir,
                                             delete_photo=cfg.verification.delete_photos_after_verify)

    n = await asyncio.to_thread(confirm)
    await query.answer()
    await query.edit_message_text(t(user["lang"], "confirmed_all", n=n))
    for item_id in ids:
        await digest.refresh_item(context.bot, cfg, item_id)


async def _resend(query, context, cfg: Config, user, parts: list[str]) -> None:
    await query.answer()
    with db.session(cfg) as conn:
        for item in transactions.pending_items(conn):
            await digest.send_item(context.bot, cfg, conn, user["chat_id"], user["lang"], item)


async def _adjust(query, context, cfg: Config, user, parts: list[str]) -> None:
    lang = user["lang"]
    await query.answer()
    step = parts[1]
    if step == "c":
        await query.edit_message_text(t(lang, "cancelled"))
        return
    reason = parts[2]
    since = clock.stamp(clock.now() - timedelta(days=28))
    if step in ("r", "l"):
        page = int(parts[3]) if step == "l" else 0
        with db.session(cfg) as conn:
            products = catalog.products_by_recent_sales(conn, since)
        markup = keyboards.product_page(products, page, lang, pick=f"a:p:{reason}", nav=f"a:l:{reason}")
        await query.edit_message_text(f"{t(lang, 'reason_' + reason)}\n{t(lang, 'adjust_which')}", reply_markup=markup)
    elif step == "p":
        with db.session(cfg) as conn:
            p = catalog.product(conn, int(parts[3]))
        await query.edit_message_text(
            f"{t(lang, 'reason_' + reason)}\n{t(lang, 'adjust_howmany', name=catalog.display_name(p, lang))}",
            reply_markup=keyboards.quantities(f"a:q:{reason}:{p['id']}", keyboards.QTY_CHOICES, lang),
        )
    elif step == "q":
        product_id, qty = int(parts[3]), int(parts[4])
        with db.session(cfg) as conn:
            stock.adjust(conn, product_id, reason, qty, by=user["name"])
            p = catalog.product(conn, product_id)
        await query.edit_message_text(t(lang, "adjust_done", reason=t(lang, "reason_" + reason),
                                        name=catalog.display_name(p, lang), qty=qty))


async def _stock(query, context, cfg: Config, user, parts: list[str]) -> None:
    """Stock left: s:m a new picker, s:l:<page> another page, s:p:<product> one product, s:a every item."""
    lang = user["lang"]
    await query.answer()
    step = parts[1]
    try:
        if step == "m":
            await digest.send_stock_picker(context.bot, cfg, user["chat_id"], lang)
        elif step == "l":
            await query.edit_message_text(t(lang, "stock_pick"), reply_markup=digest.stock_picker(cfg, lang, int(parts[2])))
        elif step == "p":
            text = digest.product_stock_text(cfg, int(parts[2]), lang)
            if text:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboards.product_stock_keyboard(lang))
        elif step == "a":
            await digest.send_stock_list(context.bot, cfg, user["chat_id"], lang)
    except BadRequest as exc:  # tapped twice: the message already shows it
        if "not modified" not in str(exc).lower():
            raise


async def _count(query, context, cfg: Config, user, parts: list[str]) -> None:
    """Quick count: k:<product>:<n> sets the shelf to n; k:<product>:o asks for another number."""
    lang, product_id = user["lang"], int(parts[1])
    await query.answer()
    if parts[2] == "o":
        with db.session(cfg) as conn:
            p = catalog.product(conn, product_id)
        msg = await context.bot.send_message(user["chat_id"], t(lang, "count_reply", name=catalog.display_name(p, lang)),
                                             reply_markup=ForceReply(selective=True))
        with db.session(cfg) as conn:
            digest.remember(conn, "count_prompt", product_id, user["chat_id"], msg.message_id)
        return
    await _record_count(context, cfg, user, product_id, int(parts[2]))


async def _record_count(context, cfg: Config, user, product_id: int, counted: int) -> None:
    with db.session(cfg) as conn:
        if conn.execute("SELECT 1 FROM bot_messages WHERE kind IN ('count', 'count_prompt') AND ref_id = ?",
                        (product_id,)).fetchone() is None:
            return                      # already answered on another phone
        delta = stock.count(conn, product_id, counted, "spot_check", by=user["name"] or str(user["chat_id"]))
    await digest.close_count(context.bot, cfg, product_id, counted, delta)


KHMER_DIGITS = str.maketrans("០១២៣៤៥៦៧៨៩", "0123456789")


async def _counted(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """A number typed in reply to "How many are there?". False if the reply was to something else."""
    cfg = _cfg(context)
    user = _user(cfg, update.message.chat.id)
    if user is None:
        return False
    with db.session(cfg) as conn:
        prompt = conn.execute(
            "SELECT ref_id FROM bot_messages WHERE kind = 'count_prompt' AND chat_id = ? AND message_id = ?",
            (user["chat_id"], update.message.reply_to_message.message_id),
        ).fetchone()
        p = catalog.product(conn, prompt["ref_id"]) if prompt else None
    if prompt is None:
        return False
    digits = "".join(ch for ch in text.translate(KHMER_DIGITS) if ch.isdigit())
    if not digits:                      # Khmer digits are read too; anything else: ask again
        await update.message.reply_text(t(user["lang"], "count_reply", name=catalog.display_name(p, user["lang"])))
        return True
    await _record_count(context, cfg, user, prompt["ref_id"], int(digits))
    return True


KEEPS = {3: 91, 6: 182, 12: 365, 0: 0}  # the expiry buttons, in days; 0: it doesn't expire


async def _expiry(query, context, cfg: Config, user, parts: list[str]) -> None:
    """x:<batch>:<months>: how long the product keeps, when there's no date to photograph."""
    lang, months = user["lang"], int(parts[2])
    with db.session(cfg) as conn:
        b = batches.get(conn, int(parts[1]))
        p = catalog.product(conn, b.product_id) if b else None
        if p is not None:
            catalog.update_product(conn, p["id"], shelf_life_days=KEEPS[months])
    await query.answer()
    if p is None:
        return
    name = catalog.display_name(p, lang)
    await query.edit_message_text(t(lang, "expiry_never_set", name=name) if months == 0 else
                                  t(lang, "expiry_life_set", name=name, life=t(lang, f"expiry_keeps_{months}")))


async def _shelf(query, context, cfg: Config, user, parts: list[str]) -> None:
    """Shelf check: w:<batch>:r, that batch is off the shelf; w:m, send the check again."""
    lang = user["lang"]
    if parts[1] == "m":
        await query.answer()
        await digest.send_shelf_check(context.bot, cfg, chat_id=user["chat_id"])
        return
    batch_id = int(parts[1])
    with db.session(cfg) as conn:
        b = batches.get(conn, batch_id)
        n = batches.remove(conn, batch_id, reason="expired", by=user["name"] or str(user["chat_id"])) if b else 0
        p = catalog.product(conn, b.product_id) if b else None
    await query.answer(t(lang, "shelf_removed", name=catalog.display_name(p, lang), n=n) if n else t(lang, "already"))
    await digest.refresh_shelf_checks(context.bot, cfg)


async def _invoice(query, context, cfg: Config, user, parts: list[str]) -> None:
    lang = user["lang"]
    invoice_id, action = int(parts[1]), parts[2]
    if action == "ok":
        try:
            with db.session(cfg) as conn:
                units, lines = invoices.confirm(conn, invoice_id, by=user["name"] or "owner")
        except ValueError:
            await query.answer(t(lang, "invoice_needs_lines"), show_alert=True)
            return
        await query.answer()
        await digest.refresh_invoice(context.bot, cfg, invoice_id, footer=t(lang, "invoice_added", units=units,
                                                                             lines=lines))
        with db.session(cfg) as conn:
            new = batches.for_invoice(conn, invoice_id)
        await digest.send_put_away(context.bot, cfg, user["chat_id"], lang, new)
        await digest.send_expiry_asks(context.bot, cfg, user["chat_id"], lang, new)
    elif action == "no":
        with db.session(cfg) as conn:
            invoices.reject(conn, invoice_id)
        await query.answer()
        await digest.refresh_invoice(context.bot, cfg, invoice_id, footer=t(lang, "invoice_rejected"))
    elif action == "s":
        with db.session(cfg) as conn:
            invoices.set_supplier(conn, invoice_id, int(parts[3]))
        await query.answer()
        await digest.refresh_invoice(context.bot, cfg, invoice_id)
    elif action == "b":
        await query.answer()
        with db.session(cfg) as conn:
            inv = conn.execute("SELECT supplier_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            products = catalog.products_by_recent_sales(conn, clock.stamp(clock.now() - timedelta(days=28)),
                                                        inv["supplier_id"])
        await context.bot.send_message(
            user["chat_id"], t(lang, "bonus_which"),
            reply_markup=keyboards.product_page(products, 0, lang, pick=f"b:{invoice_id}:p", nav=f"b:{invoice_id}:l"),
        )


async def _invoice_line(query, context, cfg: Config, user, parts: list[str]) -> None:
    lang = user["lang"]
    line_id, action = int(parts[1]), parts[2]
    with db.session(cfg) as conn:
        if action == "p":
            invoices.set_line_product(conn, line_id, int(parts[3]))
        elif action == "n":
            invoices.new_product_for_line(conn, line_id)
        else:
            invoices.skip_line(conn, line_id)
        line = conn.execute(
            "SELECT l.*, p.name_en, p.name_km FROM invoice_lines l LEFT JOIN products p ON p.id = l.product_id "
            "WHERE l.id = ?", (line_id,)
        ).fetchone()
    await query.answer()
    if line["skipped"]:
        text = t(lang, "invoice_line_skipped", text=line["text_as_written"])
    else:
        text = f"{'🆕' if line['matched_by'] == 'new' else '✅'} {line['text_as_written']} → {catalog.display_name(line, lang)}"
    await query.edit_message_text(text)
    await digest.refresh_invoice(context.bot, cfg, line["invoice_id"])


async def _bonus(query, context, cfg: Config, user, parts: list[str]) -> None:
    lang = user["lang"]
    invoice_id, step = int(parts[1]), parts[2]
    await query.answer()
    if step == "l":
        with db.session(cfg) as conn:
            inv = conn.execute("SELECT supplier_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            products = catalog.products_by_recent_sales(conn, clock.stamp(clock.now() - timedelta(days=28)),
                                                        inv["supplier_id"])
        await query.edit_message_reply_markup(
            keyboards.product_page(products, int(parts[3]), lang, pick=f"b:{invoice_id}:p", nav=f"b:{invoice_id}:l"))
    elif step == "p":
        with db.session(cfg) as conn:
            p = catalog.product(conn, int(parts[3]))
        await query.edit_message_text(
            f"{t(lang, 'bonus_which')}\n{catalog.display_name(p, lang)}",
            reply_markup=keyboards.quantities(f"b:{invoice_id}:q:{p['id']}", keyboards.BONUS_CHOICES, lang),
        )
    elif step == "q":
        product_id, qty = int(parts[3]), int(parts[4])
        with db.session(cfg) as conn:
            invoices.add_bonus(conn, invoice_id, product_id, qty)
            p = catalog.product(conn, product_id)
        await query.edit_message_text(t(lang, "bonus_done", name=catalog.display_name(p, lang), qty=qty))
        await digest.refresh_invoice(context.bot, cfg, invoice_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("telegram update failed", exc_info=context.error)

