"""Build the Telegram application and schedule its jobs.

Runs inside the web server's event loop (see web/server.py), using long
polling, so the shop needs no public URL.
"""

from __future__ import annotations

import logging

from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .. import clock, db
from ..config import Config, parse_hhmm
from ..services import transactions
from . import digest, handlers

log = logging.getLogger("luydek.bot")


def build(cfg: Config) -> Application | None:
    if not cfg.telegram_token:
        log.warning("TELEGRAM_BOT_TOKEN not set: the bot is off, the dashboard review queue still works")
        return None
    app = Application.builder().token(cfg.telegram_token).build()
    app.bot_data["cfg"] = cfg

    app.add_handler(CommandHandler("start", handlers.cmd_start))
    app.add_handler(CommandHandler("lang", handlers.cmd_lang))
    app.add_handler(CommandHandler("digest", handlers.cmd_digest))
    app.add_handler(CommandHandler("orders", handlers.cmd_orders))
    app.add_handler(CommandHandler("week", handlers.cmd_week))
    app.add_handler(CommandHandler("adjust", handlers.cmd_adjust))
    app.add_handler(CommandHandler("invoice", handlers.cmd_invoice))
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
    jq.run_repeating(_job_housekeeping, interval=300, first=60, name="housekeeping")
    return app


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["cfg"]


async def _job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await digest.send_digest(context.bot, _cfg(context))


async def _job_end_of_day(context: ContextTypes.DEFAULT_TYPE) -> None:
    n = await digest.end_of_day(context.bot, _cfg(context))
    log.info("end of day: %d items accepted as best guess", n)


async def _job_orders(context: ContextTypes.DEFAULT_TYPE) -> None:
    # The evening before each agent's visit.
    await digest.send_orders(context.bot, _cfg(context), only_visiting_tomorrow=True)


async def _job_week(context: ContextTypes.DEFAULT_TYPE) -> None:
    if clock.today().weekday() == 6:  # Sunday
        await digest.send_week_summary(context.bot, _cfg(context))


async def _job_housekeeping(context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = _cfg(context)
    with db.session(cfg) as conn:
        transactions.close_stale(conn, cfg.verification.stale_transaction_minutes, cfg.media_dir,
                                 cfg.verification.delete_photos_after_verify)
    await digest.retry_queued_invoices(context.bot, cfg)
