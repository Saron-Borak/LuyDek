"""Outgoing Telegram messages.

Used by the scheduled jobs, by bot commands and by the dashboard ("send now"),
so everything here takes a plain `telegram.Bot` rather than a handler context.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

from telegram import Bot, InputMediaPhoto
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError

from .. import clock, db, render
from ..config import Config
from ..i18n import t
from ..recognition import labels, tray
from ..services import alerts, batches, catalog, forecast, invoices, metrics, sources, stock, summary, transactions
from . import keyboards

log = logging.getLogger("luydek.bot")


def users(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM bot_users ORDER BY created_at").fetchall()


def remember(conn: sqlite3.Connection, kind: str, ref_id: int, chat_id: int, message_id: int) -> None:
    conn.execute(
        "INSERT INTO bot_messages (kind, ref_id, chat_id, message_id, created_at) VALUES (?, ?, ?, ?, ?)",
        (kind, ref_id, chat_id, message_id, clock.stamp()),
    )


def _window(now: datetime) -> str:
    if now.hour < 11:
        return "window_morning"
    if now.hour < 16:
        return "window_midday"
    return "window_evening"


# --- items ---------------------------------------------------------------------------

def item_caption(item: sqlite3.Row, lang: str) -> str:
    name = catalog.pack_label(item, lang) if item["pack_id"] is not None else "?"
    count = item["count"]
    status = item["status"]
    if status == "pending":
        if item["method"] == "tray":
            key = "ask_tray_low" if item["pack_id"] is None else "ask_tray"
        else:
            key = "ask_low" if item["tier"] == "low" else "ask_medium"
        text = t(lang, key, n=item["id"], time=item["started_at"][11:16], name=name, count=count)
        if item["hint"] in ("size", "lookalike"):
            text += t(lang, f"why_{item['hint']}")
        if item["name_guess"]:
            text += t(lang, "looks_new", name=item["name_guess"])
        return text
    head = f"#{item['id']} · {item['started_at'][11:16]}\n"
    if status in ("confirmed", "corrected", "best_guess"):
        return head + t(lang, status, name=name, count=count)
    return head + t(lang, status)


async def send_item(bot: Bot, cfg: Config, conn: sqlite3.Connection, chat_id: int, lang: str,
                    item: sqlite3.Row) -> None:
    markup = keyboards.item_keyboard(conn, item, transactions.candidates(item), lang)
    caption = item_caption(item, lang)
    crop = cfg.media_dir / item["crop_path"] if item["crop_path"] else None
    if crop is not None and crop.exists():
        with crop.open("rb") as fh:
            msg = await bot.send_photo(chat_id, fh, caption=caption, reply_markup=markup)
        remember(conn, "item", item["id"], chat_id, msg.message_id)
    else:
        msg = await bot.send_message(chat_id, caption, reply_markup=markup)
        remember(conn, "item_text", item["id"], chat_id, msg.message_id)


async def refresh_item(bot: Bot, cfg: Config, item_id: int) -> None:
    """Re-render every copy of an item's message (all verifiers' phones), plus its grids."""
    with db.session(cfg) as conn:
        item = transactions.item(conn, item_id)
        copies = conn.execute(
            "SELECT m.*, u.lang FROM bot_messages m LEFT JOIN bot_users u ON u.chat_id = m.chat_id "
            "WHERE m.kind IN ('item', 'item_text', 'grid') AND m.ref_id = ?",
            (item_id,),
        ).fetchall()
        markups = {}
        for c in copies:
            lang = c["lang"] or cfg.shop.default_lang
            if item["status"] == "pending" and lang not in markups:
                markups[lang] = keyboards.item_keyboard(conn, item, transactions.candidates(item), lang)
    for c in copies:
        lang = c["lang"] or cfg.shop.default_lang
        text = item_caption(item, lang)
        markup = markups.get(lang) if item["status"] == "pending" else None
        try:
            if c["kind"] == "item":
                await bot.edit_message_caption(c["chat_id"], c["message_id"], caption=text, reply_markup=markup)
            elif c["kind"] == "item_text":
                await bot.edit_message_text(text, c["chat_id"], c["message_id"], reply_markup=markup)
            elif item["status"] != "pending":  # a grid question is only closed, never redrawn
                await bot.edit_message_text(text, c["chat_id"], c["message_id"], reply_markup=None)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not update message %s: %s", c["message_id"], exc)
        except TelegramError as exc:
            log.warning("could not update message %s: %s", c["message_id"], exc)


async def send_grid(bot: Bot, cfg: Config, chat_id: int, lang: str, item_id: int, page: int) -> None:
    """"Other": numbered photos of likely products, then number buttons. No typing."""
    with db.session(cfg) as conn:
        item = transactions.item(conn, item_id)
        ranked = [p for p, _ in transactions.candidates(item)]
        all_packs = catalog.packs(conn)
        by_id = {pk["pack_id"]: pk for pk in all_packs}
        order = [p for p in ranked if p in by_id] + [pk["pack_id"] for pk in all_packs if pk["pack_id"] not in ranked]
        chunk = [by_id[p] for p in order[page * 6:(page + 1) * 6]]
        photos = {
            r["pack_id"]: r["crop_path"]
            for r in conn.execute(
                # SQLite takes bare columns from the MIN(id) row: each pack's first enrollment photo.
                "SELECT pack_id, crop_path, MIN(id) FROM gallery_images "
                "WHERE source = 'enrollment' AND pack_id IS NOT NULL GROUP BY pack_id"
            )
        }
    first = page * 6 + 1
    media = []
    handles = []
    for i, pk in enumerate(chunk):
        rel = photos.get(pk["pack_id"])
        if rel and (cfg.media_dir / rel).exists():
            fh = (cfg.media_dir / rel).open("rb")
            handles.append(fh)
            media.append(InputMediaPhoto(fh, caption=f"{first + i}. {catalog.pack_label(pk, lang)}"))
    try:
        if len(media) >= 2:
            await bot.send_media_group(chat_id, media)
        elif len(media) == 1:
            await bot.send_photo(chat_id, media[0].media, caption=media[0].caption)
    finally:
        for fh in handles:
            fh.close()
    markup = keyboards.grid_keyboard(item_id, chunk, first, (page + 1) * 6 < len(order), page, lang)
    msg = await bot.send_message(chat_id, t(lang, "which_one", n=item_id), reply_markup=markup)
    with db.session(cfg) as conn:
        remember(conn, "grid", item_id, chat_id, msg.message_id)


# --- digests ---------------------------------------------------------------------------

async def send_digest(bot: Bot, cfg: Config, resend: bool = False, manual: bool = False) -> int:
    """Batched check at a quiet moment: a header, one photo per unsure item, confirm-all."""
    import asyncio

    # Look-alikes whose label settles them, and tray counts Claude can read, never reach the owner.
    await asyncio.to_thread(labels.settle, cfg)
    await asyncio.to_thread(tray.settle, cfg)
    now = clock.now()
    with db.session(cfg) as conn:
        transactions.close_stale(conn, cfg.verification.stale_transaction_minutes, cfg.media_dir,
                                 cfg.verification.delete_photos_after_verify, cfg.recognition.tray)
        recipients = users(conn)
        if not recipients:
            return 0  # nobody registered yet: keep everything for the first verifier
        items = transactions.pending_items(conn, unsent_only=not resend)
        waiting = len(transactions.pending_items(conn)) - len(items)
        since = db.get_setting(conn, "last_digest_at") or clock.stamp(now.replace(hour=0, minute=0, second=0))
        stats = metrics.window_stats(conn, since)
        digest_id = None
        if items:
            digest_id = conn.execute(
                "INSERT INTO digests (sent_at, item_count) VALUES (?, ?)", (clock.stamp(now), len(items))
            ).lastrowid
            transactions.mark_sent(conn, [i["id"] for i in items], digest_id)
        if not resend:
            db.set_setting(conn, "last_digest_at", clock.stamp(now))
    if not items and not waiting and not manual and not stats["txns"]:
        return 0  # a quiet morning with no sales: say nothing

    window = "window_now" if manual else _window(now)
    app_url = cfg.telegram.miniapp_url
    for u in recipients:
        lang = u["lang"]
        if app_url and (items or waiting):
            # The Mini App: one message, one button; the items are checked by swiping.
            try:
                await bot.send_message(u["chat_id"], t(lang, "digest_header", window=t(lang, window), txns=stats["txns"],
                                                       auto=stats["auto"], pending=len(items) + waiting),
                                       reply_markup=keyboards.open_app(app_url, lang, len(items) + waiting, digest_id))
            except TelegramError as exc:
                log.warning("digest to %s failed: %s", u["chat_id"], exc)
            continue
        try:
            if items or resend:
                await bot.send_message(u["chat_id"], t(lang, "digest_header", window=t(lang, window), txns=stats["txns"],
                                                       auto=stats["auto"], pending=len(items)))
            elif waiting:
                await bot.send_message(u["chat_id"], t(lang, "digest_waiting", n=waiting),
                                       reply_markup=keyboards.show_again(lang))
            else:
                await bot.send_message(u["chat_id"], t(lang, "digest_nothing", txns=stats["txns"]))
            with db.session(cfg) as conn:
                for item in items:
                    await send_item(bot, cfg, conn, u["chat_id"], lang, transactions.item(conn, item["id"]))
            if items and digest_id:
                await bot.send_message(u["chat_id"], "👆", reply_markup=keyboards.confirm_all(digest_id, lang))
            if waiting and items:
                await bot.send_message(u["chat_id"], t(lang, "digest_waiting", n=waiting),
                                       reply_markup=keyboards.show_again(lang))
        except TelegramError as exc:
            log.warning("digest to %s failed: %s", u["chat_id"], exc)
    return len(items)


async def end_of_day(bot: Bot | None, cfg: Config) -> int:
    """Accept every unchecked guess as the best guess, and close the day's messages."""
    with db.session(cfg) as conn:
        transactions.close_stale(conn, 0, cfg.media_dir, cfg.verification.delete_photos_after_verify,
                                 cfg.recognition.tray)
        ids = transactions.best_guess_all(conn, cfg.media_dir, cfg.verification.delete_photos_after_verify)
    if bot is not None:
        for item_id in ids:
            await refresh_item(bot, cfg, item_id)
    return len(ids)


# --- order lists -------------------------------------------------------------------------

async def send_orders(bot: Bot, cfg: Config, *, only_visiting_tomorrow: bool = False,
                      chat_id: int | None = None) -> int:
    today = clock.today()
    with db.session(cfg) as conn:
        forecast.settle_follow_rate(conn)
        plans = forecast.build_plans(conn, cfg.forecast)
        if only_visiting_tomorrow:
            plans = [p for p in plans if p.visit_date == today + timedelta(days=1)]
        forecast.save(conn, plans)
        simulated = stock.has_simulated(conn)
        recipients = [u for u in users(conn) if chat_id is None or u["chat_id"] == chat_id]
        if chat_id is not None and not recipients:
            recipients = [{"chat_id": chat_id, "lang": cfg.shop.default_lang}]
    for u in recipients:
        for plan in plans:
            try:
                await bot.send_message(u["chat_id"], render.order_message(plan, u["lang"], simulated),
                                       parse_mode=ParseMode.HTML)
            except TelegramError as exc:
                log.warning("order list to %s failed: %s", u["chat_id"], exc)
    return len(plans)


async def send_week_summary(bot: Bot, cfg: Config, chat_id: int | None = None) -> None:
    today = clock.today()
    with db.session(cfg) as conn:
        week = metrics.accuracy(conn, today - timedelta(days=6), today + timedelta(days=1))
        taps = metrics.taps_per_day(conn)
        sleeping = sum(p.sleeping_value for p in forecast.build_plans(conn, cfg.forecast))
        recipients = [u for u in users(conn) if chat_id is None or u["chat_id"] == chat_id]
    for u in recipients:
        acc = week["accuracy"] if week["accuracy"] is not None else "–"
        await bot.send_message(u["chat_id"], t(u["lang"], "week_summary", acc=acc, taps=taps, txns=week["txns"],
                                               sleeping=f"{sleeping:.2f}"))


# --- the day's summary and stock left ------------------------------------------------------------

async def send_day_summary(bot: Bot, cfg: Config, chat_id: int | None = None, day=None) -> dict:
    """What sold today and what is left: to everyone at the end of the day, or to one phone that asked."""
    with db.session(cfg) as conn:
        s = summary.day_summary(conn, day, cfg.forecast.expiry_warning_days)
        recipients = [u for u in users(conn) if chat_id is None or u["chat_id"] == chat_id]
    for u in recipients:
        try:
            await bot.send_message(u["chat_id"], render.day_summary_message(s, u["lang"]), parse_mode=ParseMode.HTML,
                                   reply_markup=keyboards.summary_keyboard(u["lang"], cfg.telegram.miniapp_url))
        except TelegramError as exc:
            log.warning("day summary to %s failed: %s", u["chat_id"], exc)
    return s


def _stock_labels(conn: sqlite3.Connection, cfg: Config) -> dict[int, str]:
    """What each picker button shows after the name: the count left, "?" if never counted, "!" below zero."""
    labels = {}
    for r in summary.stock_left(conn, cfg.forecast.expiry_warning_days):
        labels[r["id"]] = "?" if not r["tracked"] else "!" if r["negative"] else str(max(r["on_hand"], 0))
    return labels


def stock_picker(cfg: Config, lang: str, page: int = 0, only: list[int] | None = None):
    """The "Stock left" menu: every product (best-sellers first) or just `only`, each with its count."""
    since = clock.stamp(clock.now() - timedelta(days=28))
    with db.session(cfg) as conn:
        products = catalog.products_by_recent_sales(conn, since)
        if only is not None:
            by_id = {p["id"]: p for p in products}
            products = [by_id[i] for i in only if i in by_id]
        labels = _stock_labels(conn, cfg)
    return keyboards.stock_picker(products, labels, page, lang, paged=only is None)


async def send_stock_picker(bot: Bot, cfg: Config, chat_id: int, lang: str) -> None:
    await bot.send_message(chat_id, t(lang, "stock_pick"), reply_markup=stock_picker(cfg, lang))


async def send_stock_list(bot: Bot, cfg: Config, chat_id: int, lang: str) -> int:
    """Every product and what is left, as many messages as it takes."""
    with db.session(cfg) as conn:
        rows = summary.stock_left(conn, cfg.forecast.expiry_warning_days)
    messages = render.stock_list_messages(rows, lang, clock.today())
    for text in messages:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    return len(messages)


def product_stock_text(cfg: Config, product_id: int, lang: str) -> str | None:
    with db.session(cfg) as conn:
        row = summary.product_stock(conn, product_id, cfg.forecast.expiry_warning_days)
    return render.product_stock_message(row, lang) if row else None


# --- out of stock, quick counts, the shelf check, putting deliveries away ---------------------------

async def send_stock_alerts(bot: Bot, cfg: Config) -> int:
    """Tell every phone what just ran out or ran low (each once, until it's back in stock)."""
    with db.session(cfg) as conn:
        if not users(conn):
            return 0                    # nobody to tell yet: keep the news for the first verifier
        rows = alerts.new_stock_alerts(conn, cfg.forecast.expiry_warning_days)
        recipients = users(conn)
    if not rows:
        return 0
    for u in recipients:
        try:
            await bot.send_message(u["chat_id"], render.stock_alert_message(rows, u["lang"]), parse_mode=ParseMode.HTML,
                                   reply_markup=keyboards.summary_keyboard(u["lang"], cfg.telegram.miniapp_url))
        except TelegramError as exc:
            log.warning("stock alert to %s failed: %s", u["chat_id"], exc)
    return len(rows)


async def send_spot_checks(bot: Bot, cfg: Config, chat_id: int | None = None, n: int | None = None) -> int:
    """A few quick counts: "LuyDek has 4 Sting. How many are on the shelf?", one tap when right."""
    with db.session(cfg) as conn:
        picks = alerts.spot_checks(conn, cfg.verification.spot_checks if n is None else n,
                                   expiry_warning_days=cfg.forecast.expiry_warning_days)
        recipients = [u for u in users(conn) if chat_id is None or u["chat_id"] == chat_id]
        where = batches.places(conn)
        homes = {r["id"]: r["location_id"] for r in conn.execute("SELECT id, location_id FROM products")}
    for r in picks:
        expected = max(r["on_hand"], 0)
        place = where.get(homes.get(r["id"]))
        for u in recipients:
            lang = u["lang"]
            text = t(lang, "count_ask", name=catalog.display_name(r, lang), n=expected,
                     place=f" · {place['name']}" if place else "")
            try:
                msg = await bot.send_message(u["chat_id"], text, reply_markup=keyboards.count_keyboard(r["id"], expected, lang))
            except TelegramError as exc:
                log.warning("spot check to %s failed: %s", u["chat_id"], exc)
                continue
            with db.session(cfg) as conn:
                remember(conn, "count", r["id"], u["chat_id"], msg.message_id)
    return len(picks)


async def close_count(bot: Bot, cfg: Config, product_id: int, counted: int, delta: int) -> None:
    """Every phone's copy of a quick count shows the answer, and loses its buttons."""
    with db.session(cfg) as conn:
        p = catalog.product(conn, product_id)
        copies = conn.execute(
            "SELECT m.*, u.lang FROM bot_messages m LEFT JOIN bot_users u ON u.chat_id = m.chat_id "
            "WHERE m.kind = 'count' AND m.ref_id = ?", (product_id,)).fetchall()
        conn.execute("DELETE FROM bot_messages WHERE kind IN ('count', 'count_prompt') AND ref_id = ?", (product_id,))
    for c in copies:
        lang = c["lang"] or cfg.shop.default_lang
        name = catalog.display_name(p, lang)
        text = t(lang, "count_ok", name=name, n=counted) if not delta else \
            t(lang, "count_fixed", name=name, n=counted, delta=f"{delta:+d}")
        try:
            await bot.edit_message_text(text, c["chat_id"], c["message_id"], reply_markup=None)
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not update count message: %s", exc)


def _shelf_text(tasks: list, unrecorded: list, flags: list, lang: str, days: int) -> str:
    text = render.shelf_check_message(tasks, lang, days)
    more = render.sources_message(unrecorded, flags, lang)
    return f"{text}\n\n{more}" if more else text


async def send_shelf_check(bot: Bot, cfg: Config, chat_id: int | None = None, scheduled: bool = False) -> int:
    """The walk round the shop, and stock that didn't come in on any record. On schedule, nothing is
    sent when nothing needs doing."""
    days = cfg.forecast.expiry_warning_days
    with db.session(cfg) as conn:
        tasks = batches.shelf_check(conn, days)
        unrecorded, flags = sources.unrecorded(conn), sources.lot_flags(conn)
        recipients = [u for u in users(conn) if chat_id is None or u["chat_id"] == chat_id]
    if scheduled and not (tasks or unrecorded or flags):
        return 0
    for u in recipients:
        try:
            msg = await bot.send_message(u["chat_id"], _shelf_text(tasks, unrecorded, flags, u["lang"], days),
                                         parse_mode=ParseMode.HTML,
                                         reply_markup=keyboards.shelf_keyboard(tasks, u["lang"], cfg.telegram.miniapp_url))
            with db.session(cfg) as conn:
                remember(conn, "shelf", 0, u["chat_id"], msg.message_id)
        except TelegramError as exc:
            log.warning("shelf check to %s failed: %s", u["chat_id"], exc)
    return len(tasks)


async def refresh_shelf_checks(bot: Bot, cfg: Config) -> None:
    """After a batch is taken off the shelf, every copy of the shelf check is redrawn without it."""
    days = cfg.forecast.expiry_warning_days
    with db.session(cfg) as conn:
        tasks = batches.shelf_check(conn, days)
        unrecorded, flags = sources.unrecorded(conn), sources.lot_flags(conn)
        copies = conn.execute(
            "SELECT m.*, u.lang FROM bot_messages m LEFT JOIN bot_users u ON u.chat_id = m.chat_id "
            "WHERE m.kind = 'shelf' ORDER BY m.id DESC LIMIT 20").fetchall()
    for c in copies:
        lang = c["lang"] or cfg.shop.default_lang
        try:
            await bot.edit_message_text(_shelf_text(tasks, unrecorded, flags, lang, days), c["chat_id"], c["message_id"],
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=keyboards.shelf_keyboard(tasks, lang, cfg.telegram.miniapp_url))
        except TelegramError as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not update shelf check: %s", exc)


async def send_put_away(bot: Bot, cfg: Config, chat_id: int, lang: str, batch_ids: list[int]) -> None:
    with db.session(cfg) as conn:
        items = batches.put_away(conn, batch_ids)
    if items:
        await bot.send_message(chat_id, render.put_away_message(items, lang), parse_mode=ParseMode.HTML)


MAX_EXPIRY_ASKS = 5  # questions after one delivery; the rest are on the invoice page


async def send_expiry_asks(bot: Bot, cfg: Config, chat_id: int, lang: str, batch_ids: list[int]) -> int:
    """After a delivery, a question for each product that came with no date and whose shelf life isn't
    known: a photo of the date answers it exactly, a tap says how long it usually keeps."""
    asks, seen = [], set()
    with db.session(cfg) as conn:
        for batch_id in batch_ids:
            b = batches.get(conn, batch_id)
            if b is None or b.expiry or b.left <= 0 or b.product_id in seen:
                continue
            p = catalog.product(conn, b.product_id)
            if p["shelf_life_days"] is None:
                seen.add(b.product_id)
                asks.append((b, p))
    for b, p in asks[:MAX_EXPIRY_ASKS]:
        msg = await bot.send_message(chat_id, t(lang, "expiry_ask", name=catalog.display_name(p, lang), n=b.left),
                                     reply_markup=keyboards.expiry_keyboard(b.id, lang))
        with db.session(cfg) as conn:
            remember(conn, "expiry", b.id, chat_id, msg.message_id)
    if len(asks) > MAX_EXPIRY_ASKS:
        await bot.send_message(chat_id, t(lang, "expiry_more", n=len(asks) - MAX_EXPIRY_ASKS))
    return min(len(asks), MAX_EXPIRY_ASKS)


# --- invoices ------------------------------------------------------------------------------

async def send_invoice(bot: Bot, cfg: Config, invoice_id: int, chat_id: int, lang: str) -> None:
    """The extracted invoice for one-tap confirmation, plus a question per unmatched line."""
    with db.session(cfg) as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        text = render.invoice_message(conn, invoice_id, lang)
        if inv["supplier_id"] is None:
            text += "\n\n" + t(lang, "invoice_unknown_supplier")
        markup = keyboards.invoice_keyboard(conn, inv, lang)
        open_lines = [(l, keyboards.line_keyboard(conn, l, lang)) for l in invoices.unresolved(conn, invoice_id)]
    msg = await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    with db.session(cfg) as conn:
        remember(conn, "invoice", invoice_id, chat_id, msg.message_id)
    for line, line_markup in open_lines:
        m = await bot.send_message(chat_id, t(lang, "invoice_line_unmatched", text=line["text_as_written"]),
                                   reply_markup=line_markup)
        with db.session(cfg) as conn:
            remember(conn, "invoice_line", line["id"], chat_id, m.message_id)


async def refresh_invoice(bot: Bot, cfg: Config, invoice_id: int, footer: str | None = None) -> None:
    with db.session(cfg) as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        copies = conn.execute(
            "SELECT m.*, u.lang FROM bot_messages m LEFT JOIN bot_users u ON u.chat_id = m.chat_id "
            "WHERE m.kind = 'invoice' AND m.ref_id = ?",
            (invoice_id,),
        ).fetchall()
        rendered = {}
        for c in copies:
            lang = c["lang"] or cfg.shop.default_lang
            if lang not in rendered:
                text = render.invoice_message(conn, invoice_id, lang)
                if inv["supplier_id"] is None and inv["status"] != "rejected":
                    text += "\n\n" + t(lang, "invoice_unknown_supplier")
                markup = None if inv["status"] == "rejected" else keyboards.invoice_keyboard(conn, inv, lang)
                rendered[lang] = (text, markup)
    for c in copies:
        lang = c["lang"] or cfg.shop.default_lang
        text, markup = rendered[lang]
        if footer:
            text += "\n\n" + footer
        try:
            await bot.edit_message_text(text, c["chat_id"], c["message_id"], parse_mode=ParseMode.HTML,
                                        reply_markup=markup)
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                log.warning("could not update invoice message: %s", exc)


async def retry_queued_invoices(bot: Bot, cfg: Config) -> None:
    """Invoices photographed while the internet was down are read once it is back."""
    import asyncio

    with db.session(cfg) as conn:
        queued = invoices.queued(conn)
        langs = {u["chat_id"]: u["lang"] for u in users(conn)}
    for inv in queued:
        status = await asyncio.to_thread(invoices.process, cfg, inv["id"])
        if status == "extracted" and inv["chat_id"]:
            await send_invoice(bot, cfg, inv["id"], inv["chat_id"], langs.get(inv["chat_id"], cfg.shop.default_lang))
