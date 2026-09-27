"""Telegram update handlers: commands, the reply-keyboard menu, button taps, invoice photos."""

from __future__ import annotations

import asyncio
import functools
import logging
import uuid
from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

from .. import clock, db
from ..config import Config
from ..i18n import STRINGS, t
from ..recognition import pipeline
from ..services import catalog, invoices, metrics, stock, transactions
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
async def cmd_invoice(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    await update.effective_message.reply_text(t(user["lang"], "invoice_help"))


MENU = {
    key: cmd for key, cmd in (
        ("menu_invoice", cmd_invoice), ("menu_adjust", cmd_adjust),
        ("menu_orders", cmd_orders), ("menu_week", cmd_week),
    )
}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    for key, handler in MENU.items():
        if text in STRINGS[key].values():
            await handler(update, context)
            return


# --- invoice photos ---------------------------------------------------------------------

@registered
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, user) -> None:
    cfg = _cfg(context)
    lang = user["lang"]
    msg = update.message
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
               "i": _invoice, "il": _invoice_line, "b": _bonus}.get(parts[0])
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
    verb = {"y": "yes", "p": "pick", "q": "qty", "x": "remove", "u": "unknown"}[action]
    result = await asyncio.to_thread(_resolve, cfg, item_id, verb, **kwargs)
    await query.answer(t(lang, "already") if result["already"] else None)
    await digest.refresh_item(context.bot, cfg, item_id)


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
        text = f"✅ {line['text_as_written']} → {catalog.display_name(line, lang)}"
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

