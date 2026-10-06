"""Build the Telegram application and schedule its jobs.

Runs inside the web server's event loop (see web/server.py), using long
polling, so the shop needs no public URL.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from telegram import Bot, BotCommand, MenuButtonCommands, MenuButtonWebApp, WebAppInfo
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .. import clock, db
from ..config import Config, parse_hhmm
from ..recognition import labels, tray
from ..services import transactions
from . import digest, handlers

log = logging.getLogger("luydek.bot")


def build(cfg: Config) -> Application | None:
    if not cfg.telegram.enabled:
        log.info("Telegram is switched off in this config; the dashboard review queue still works")
        return None
    if not cfg.telegram_token:
        log.warning("TELEGRAM_BOT_TOKEN not set: the bot is off, the dashboard review queue still works")
        return None
    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("lang", handlers.cmd_lang))
    app.add_handler(CommandHandler("digest", handlers.cmd_digest))
    app.add_handler(CommandHandler("check", handlers.cmd_check))
    app.add_handler(CommandHandler("orders", handlers.cmd_orders))
    app.add_handler(CommandHandler("week", handlers.cmd_week))
    app.add_handler(CommandHandler("adjust", handlers.cmd_adjust))
    app.add_handler(CommandHandler("invoice", handlers.cmd_invoice))
    app.add_handler(CommandHandler("today", handlers.cmd_today))
    app.add_handler(CommandHandler("stock", handlers.cmd_stock))
    app.add_handler(CommandHandler("shelf", handlers.cmd_shelf))
    app.add_handler(CommandHandler("count", handlers.cmd_count))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handlers.on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    app.add_error_handler(handlers.on_error)

    tz = cfg.tz
    jq = app.job_queue
    for hhmm in cfg.verification.digest_times:
        jq.run_daily(_job_digest, parse_hhmm(hhmm, tz), name=f"digest {hhmm}")
    jq.run_daily(_job_end_of_day, parse_hhmm(cfg.verification.end_of_day, tz), name="end of day")
    jq.run_daily(_job_orders, parse_hhmm(cfg.forecast.order_list_time, tz), name="order lists")
    jq.run_daily(_job_week, parse_hhmm(cfg.verification.weekly_summary_time, tz), name="weekly summary")
    if cfg.verification.spot_checks:
        jq.run_daily(_job_spot_checks, parse_hhmm(cfg.verification.spot_check_time, tz), name="quick counts")
    # The job queue counts days from Sunday (0); the config, like Python, from Monday.
    jq.run_daily(_job_shelf_check, parse_hhmm(cfg.verification.shelf_check_time, tz),
                 days=((cfg.verification.shelf_check_weekday + 1) % 7,), name="shelf check")
    jq.run_repeating(_job_housekeeping, interval=300, first=60, name="housekeeping")
    return app


COMMANDS = {
    "en": [("today", "What sold today, and what is running out"), ("stock", "Stock left: one product or every item"),
           ("shelf", "Shelf check: what has expired, what goes in front"), ("count", "A few quick counts"),
           ("check", "Check the items the camera wasn't sure of"), ("orders", "Order lists for the agents"),
           ("adjust", "Credit sale, family use, damaged…"), ("lang", "ភាសាខ្មែរ / English")],
    "km": [("today", "លក់អ្វីខ្លះថ្ងៃនេះ និងអ្វីជិតអស់"), ("stock", "ស្តុកនៅសល់៖ មួយមុខ ឬទាំងអស់"),
           ("shelf", "ពិនិត្យធ្នើ៖ អ្វីផុតកំណត់ អ្វីដាក់ខាងមុខ"),
           ("count", "រាប់រហ័សពីរបីមុខ"),
           ("check", "ពិនិត្យទំនិញដែលកាមេរ៉ាមិនច្បាស់"), ("orders", "បញ្ជីបញ្ជាទិញសម្រាប់ភ្នាក់ងារ"),
           ("adjust", "លក់ជំពាក់ គ្រួសារប្រើ ខូច…"), ("lang", "ភាសាខ្មែរ / English")],
}


async def set_menu_button(bot: Bot, cfg: Config) -> None:
    """With a Mini App address, the chat's menu button opens it; without, the usual command list.
    Either way, typing "/" lists the commands, in the phone's language."""
    try:
        await bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["en"]])
        await bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS["km"]], language_code="km")
    except TelegramError as exc:
        log.warning("could not set the command list: %s", exc)
    try:
        if cfg.telegram.miniapp_url:
            url = cfg.telegram.miniapp_url.rstrip("/") + "/tg"
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp("LuyDek", WebAppInfo(url)))
        else:
            await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramError as exc:
        log.warning("could not set the menu button: %s", exc)


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


async def _job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await digest.send_digest(context.bot, _cfg(context))


async def _job_end_of_day(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    n = await digest.end_of_day(context.bot, cfg)
    log.info("end of day: %d items accepted as best guess", n)
    if cfg.verification.daily_summary:
        # After the guesses are settled, so the numbers are final. A shop day runs until 5 am,
        # so an end of day set after midnight still reports the day that just closed.
        await digest.send_day_summary(context.bot, cfg, day=(clock.now() - timedelta(hours=5)).date())


async def _job_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    # The evening before each agent's visit.
    await digest.send_orders(context.bot, _cfg(context), only_visiting_tomorrow=True)


async def _job_week(context: ContextTypes.DEFAULT_TYPE) -> None:
    if clock.today().weekday() == 6:  # Sunday
        await digest.send_week_summary(context.bot, _cfg(context))


async def _job_spot_checks(context: ContextTypes.DEFAULT_TYPE) -> None:
    await digest.send_spot_checks(context.bot, _cfg(context))


async def _job_shelf_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    await digest.send_shelf_check(context.bot, _cfg(context), scheduled=True)


async def _job_housekeeping(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    if cfg.verification.stock_alerts:
        await digest.send_stock_alerts(context.bot, cfg)
    with db.session(cfg) as conn:
        transactions.close_stale(conn, cfg.verification.stale_transaction_minutes, cfg.media_dir,
                                 cfg.verification.delete_photos_after_verify, cfg.recognition.tray)
    await digest.retry_queued_invoices(context.bot, cfg)
    await asyncio.to_thread(labels.settle, cfg)  # look-alikes missed while offline
    await asyncio.to_thread(tray.settle, cfg)   # tray counts too
