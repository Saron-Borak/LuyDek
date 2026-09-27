"""Dashboard, counter capture page and APIs. The Telegram bot runs in the same process.

    python -m luydek            # http://localhost:8000 (uvicorn with create_app as a factory)
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from .. import clock, db, render
from ..bot import app as bot_app
from ..bot import digest
from ..config import Config, get_config
from ..i18n import t
from ..recognition import pipeline, segment
from ..services import catalog, forecast, invoices, metrics, stock, transactions

log = logging.getLogger("luydek.web")
HERE = Path(__file__).parent
IGNORE_LABELS = ("phone", "cash", "calculator", "plastic_bag", "hand", "other")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.cfg
    for sub in ("crops", "frames", "gallery", "invoices", "reference"):
        (cfg.media_dir / sub).mkdir(parents=True, exist_ok=True)
    with db.session(cfg) as conn:
        db.init_schema(conn)

    tg = bot_app.build(cfg)
    app.state.bot = tg
    if tg is not None:
        await tg.initialize()
        await tg.start()
        await tg.updater.start_polling(drop_pending_updates=False)
        log.info("Telegram bot @%s polling", tg.bot.username)
    if not os.environ.get("LUYDEK_NO_PRELOAD"):
        # Load CLIP in the background so the first sale is not the slow one.
        app.state.preload = asyncio.create_task(asyncio.to_thread(pipeline.get_recognizer, cfg))
    try:
        yield
    finally:
        if tg is not None:
            await tg.updater.stop()
            await tg.stop()
            await tg.shutdown()


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or get_config()
    app = FastAPI(title="LuyDek", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.bot = None
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.mount("/media", StaticFiles(directory=cfg.media_dir), name="media")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(pack_label=catalog.pack_label, display_name=catalog.display_name, t=t)

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        with db.session(cfg) as conn:
            simulated = stock.has_simulated(conn)
            pending = conn.execute("SELECT COUNT(*) FROM transaction_items WHERE status = 'pending'").fetchone()[0]
        return templates.TemplateResponse(request, name, {
            "cfg": cfg, "bot": app.state.bot, "simulated": simulated, "pending_count": pending,
            "msg": request.query_params.get("msg"), "active": name.split(".")[0], **ctx,
        })

    def back(request: Request, msg: str | None = None, default: str = "/") -> RedirectResponse:
        target = request.headers.get("referer") or default
        target = target.split("?")[0]
        if msg:
            target += "?msg=" + quote(msg)
        return RedirectResponse(target, status_code=303)

    # --- dashboard -------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db.session(cfg) as conn:
            table = stock.stock_table(conn, cfg.forecast.expiry_warning_days)
            pending = transactions.pending_items(conn)
            pending_cands = {
                it["id"]: [(catalog.pack(conn, p), s) for p, s in transactions.candidates(it)[:4]] for it in pending
            }
            recent = transactions.recent(conn, 10)
            today = metrics.today_counts(conn)
            trend = metrics.weekly_trend(conn)
            taps = metrics.taps_per_day(conn)
            follow = metrics.follow_rate(conn)
            plans = forecast.build_plans(conn, cfg.forecast)
            packs = catalog.packs(conn)
            users = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
        sleeping = sum(p.sleeping_value for p in plans)
        return page(request, "dashboard.html", table=table, pending=pending, pending_cands=pending_cands,
                    recent=recent, today=today, trend=trend, taps=taps, follow=follow, sleeping=sleeping,
                    packs=packs, users=users, reasons=list(stock.ADJUST_REASONS))

    @app.post("/items/{item_id}/resolve")
    async def resolve_item(request: Request, item_id: int, action: str = Form(...),
                           pack_id: int | None = Form(None), delta: int = Form(0)):
        def run() -> dict:
            with db.session(cfg) as conn:
                return transactions.resolve(
                    conn, item_id, action, pack_id=pack_id, delta=delta, by="dashboard",
                    learn=pipeline.learner(cfg), media_dir=cfg.media_dir,
                    delete_photo=cfg.verification.delete_photos_after_verify,
                )
        try:
            await run_in_threadpool(run)
        except (KeyError, ValueError) as exc:
            return back(request, f"Could not update item {item_id}: {exc}")
        if app.state.bot is not None:
            await digest.refresh_item(app.state.bot.bot, cfg, item_id)
        return back(request)

    @app.post("/adjust")
    def adjust(request: Request, product_id: int = Form(...), reason: str = Form(...), qty: int = Form(...)):
        with db.session(cfg) as conn:
            stock.adjust(conn, product_id, reason, qty, by="dashboard")
        return back(request, "Stock adjusted")

    @app.post("/actions/digest")
    async def action_digest(request: Request):
        if app.state.bot is None:
            return back(request, "Telegram bot is off (no TELEGRAM_BOT_TOKEN in .env)")
        n = await digest.send_digest(app.state.bot.bot, cfg, manual=True)
        return back(request, f"Digest sent: {n} item(s) to check")

    @app.post("/actions/end-of-day")
    async def action_end_of_day(request: Request):
        n = await digest.end_of_day(app.state.bot.bot if app.state.bot else None, cfg)
        return back(request, f"End of day: {n} unchecked item(s) accepted as best guess")

    @app.post("/actions/orders")
    async def action_orders(request: Request):
        if app.state.bot is None:
            return back(request, "Telegram bot is off (no TELEGRAM_BOT_TOKEN in .env)")
        n = await digest.send_orders(app.state.bot.bot, cfg)
        return back(request, f"Sent {n} supplier order list(s) to Telegram")

    @app.post("/actions/week")
    async def action_week(request: Request):
        if app.state.bot is None:
            return back(request, "Telegram bot is off (no TELEGRAM_BOT_TOKEN in .env)")
        await digest.send_week_summary(app.state.bot.bot, cfg)
        return back(request, "Weekly summary sent")

    # --- counter ------------------------------------------------------------------------

    @app.get("/counter", response_class=HTMLResponse)
    def counter(request: Request):
        return page(request, "counter.html")

    @app.get("/api/state")
    def api_state():
        with db.session(cfg) as conn:
            ref = db.get_setting(conn, pipeline.REFERENCE_KEY)
            gallery = conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0]
            open_txn = conn.execute("SELECT id FROM transactions WHERE status = 'open' ORDER BY id DESC").fetchone()
        return {"reference": f"/media/{ref}" if ref else None, "gallery": gallery, "recognizer_ready": pipeline.ready(),
                "open_txn": open_txn["id"] if open_txn else None}

    @app.post("/api/calibrate")
    async def api_calibrate(image: UploadFile = File(...)):
        data = await image.read()
        with db.session(cfg) as conn:
            rel = pipeline.save_reference(conn, cfg, data)
        return {"reference": f"/media/{rel}"}

    def _capture(data: bytes, txn_id: int | None, source: str, use_reference: bool) -> dict:
        frame = segment.decode(data)
        rec = pipeline.get_recognizer(cfg)
        with db.session(cfg) as conn:
            if txn_id is None:
                txn_id = transactions.open_transaction(conn, source)
            elif conn.execute("SELECT status FROM transactions WHERE id = ?", (txn_id,)).fetchone()["status"] != "open":
                txn_id = transactions.open_transaction(conn, source)
            name = f"t{txn_id}_{uuid.uuid4().hex[:6]}"
            detections = rec.recognize(conn, frame, name, use_reference=use_reference)
            photo = pipeline.save_frame(cfg, data, name)
            kept = [d.record for d in detections if not d.ignored]
            frame_no = transactions.add_frame(conn, txn_id, photo, kept, cfg.media_dir)
        h, w = frame.shape[:2]
        return {
            "txn_id": txn_id, "frame_no": frame_no, "width": w, "height": h,
            "detections": [{"bbox": list(d.record.bbox), "label": d.label, "tier": d.record.tier,
                            "score": d.record.confidence, "method": d.record.method, "ignored": d.ignored}
                           for d in detections],
        }

    def _close(txn_id: int) -> dict:
        with db.session(cfg) as conn:
            summary = transactions.close_transaction(conn, txn_id, cfg.media_dir,
                                                     cfg.verification.delete_photos_after_verify)
            rows = conn.execute(
                "SELECT ti.*, p.name_en, p.name_km, pk.units, pk.label FROM transaction_items ti "
                "LEFT JOIN product_packs pk ON pk.id = ti.pack_id LEFT JOIN products p ON p.id = pk.product_id "
                "WHERE ti.txn_id = ?", (txn_id,)).fetchall()
        summary["lines"] = [{"id": r["id"], "label": catalog.pack_label(r, "en") if r["pack_id"] else "?",
                             "tier": r["tier"], "status": r["status"], "score": r["confidence"]} for r in rows]
        return summary

    @app.post("/api/capture")
    async def api_capture(image: UploadFile = File(...), txn_id: int | None = Form(None)):
        data = await image.read()
        try:
            return await run_in_threadpool(_capture, data, txn_id, "camera", True)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/transactions/{txn_id}/close")
    async def api_close(txn_id: int):
        return await run_in_threadpool(_close, txn_id)

    @app.post("/api/sale-photo")
    async def api_sale_photo(image: UploadFile = File(...), use_reference: bool = Form(False)):
        """Demo backup: one uploaded photo is one whole sale."""
        data = await image.read()
        try:
            captured = await run_in_threadpool(_capture, data, None, "upload", use_reference)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {**captured, "summary": await run_in_threadpool(_close, captured["txn_id"])}

    # --- catalog & enrollment ---------------------------------------------------------------

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        with db.session(cfg) as conn:
            table = stock.stock_table(conn)
            packs = catalog.packs(conn)
            suppliers = catalog.suppliers(conn)
            aliases = conn.execute(
                "SELECT a.*, p.name_en FROM product_aliases a JOIN products p ON p.id = a.product_id "
                "WHERE a.source != 'catalog' ORDER BY a.id DESC LIMIT 30").fetchall()
            ignore = conn.execute(
                "SELECT ignore_label, COUNT(*) AS n FROM gallery_images WHERE ignore_label IS NOT NULL "
                "GROUP BY ignore_label").fetchall()
        packs_by_product: dict[int, list] = {}
        for pk in packs:
            packs_by_product.setdefault(pk["product_id"], []).append(pk)
        return page(request, "catalog.html", table=table, packs_by_product=packs_by_product, suppliers=suppliers,
                    aliases=aliases, ignore={r["ignore_label"]: r["n"] for r in ignore}, ignore_labels=IGNORE_LABELS)

    @app.get("/catalog/enroll", response_class=HTMLResponse)
    def enroll_page(request: Request, pack_id: int | None = None, ignore: str | None = None):
        with db.session(cfg) as conn:
            pk = catalog.pack(conn, pack_id) if pack_id else None
            photos = conn.execute(
                "SELECT * FROM gallery_images WHERE " + ("pack_id = ?" if pk else "ignore_label = ?")
                + " ORDER BY id DESC", (pack_id if pk else ignore,)).fetchall()
        if pk is None and ignore not in IGNORE_LABELS:
            raise HTTPException(404)
        return page(request, "enroll.html", pk=pk, ignore=ignore, photos=photos)

    @app.post("/api/enroll")
    async def api_enroll(image: UploadFile = File(...), pack_id: int | None = Form(None),
                         ignore_label: str | None = Form(None), use_reference: bool = Form(True)):
        if (pack_id is None) == (ignore_label is None):
            raise HTTPException(400, "give exactly one of pack_id or ignore_label")
        data = await image.read()

        def run() -> dict:
            frame = segment.decode(data)
            rec = pipeline.get_recognizer(cfg)
            with db.session(cfg) as conn:
                gid, bbox = rec.enroll(conn, frame, pack_id, ignore_label, use_reference)
                row = conn.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (gid,)).fetchone()
                n = conn.execute("SELECT COUNT(*) FROM gallery_images WHERE " +
                                 ("pack_id = ?" if pack_id else "ignore_label = ?"),
                                 (pack_id or ignore_label,)).fetchone()[0]
            return {"id": gid, "bbox": list(bbox), "crop": f"/media/{row['crop_path']}", "photos": n}
        try:
            return await run_in_threadpool(run)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/catalog/gallery/{gid}/delete")
    def delete_gallery(request: Request, gid: int):
        with db.session(cfg) as conn:
            row = conn.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (gid,)).fetchone()
            if row:
                (cfg.media_dir / row["crop_path"]).unlink(missing_ok=True)
                conn.execute("DELETE FROM gallery_images WHERE id = ?", (gid,))
        return back(request)

    @app.post("/catalog/products")
    def add_product(request: Request, sku: str = Form(...), name_en: str = Form(...), name_km: str = Form(...),
                    unit: str = Form("piece"), case_size: int = Form(1), supplier_id: int | None = Form(None),
                    is_critical: bool = Form(False), shelf_life_days: int | None = Form(None),
                    unit_cost: float = Form(0.0), barcode: str | None = Form(None)):
        with db.session(cfg) as conn:
            catalog.add_product(conn, sku=sku.strip(), name_en=name_en.strip(), name_km=name_km.strip(), unit=unit,
                                case_size=case_size, supplier_id=supplier_id, is_critical=is_critical,
                                shelf_life_days=shelf_life_days, unit_cost=unit_cost, barcode=barcode or None)
        return back(request, f"Added {name_en}")

    @app.post("/catalog/packs")
    def add_pack(request: Request, product_id: int = Form(...), label: str = Form(...), units: int = Form(...),
                 barcode: str | None = Form(None)):
        with db.session(cfg) as conn:
            catalog.add_pack(conn, product_id, label, units, barcode or None)
        return back(request, f"Added pack {label}")

    @app.post("/catalog/aliases")
    def add_alias(request: Request, product_id: int = Form(...), alias: str = Form(...)):
        with db.session(cfg) as conn:
            catalog.add_alias(conn, product_id, alias, "owner")
        return back(request, f"Alias '{alias}' added")

    @app.post("/catalog/opening-count")
    async def opening_count(request: Request):
        form = await request.form()
        counts = {int(k[6:]): int(v) for k, v in form.items() if k.startswith("count_") and str(v).strip() != ""}
        with db.session(cfg) as conn:
            stock.opening_count(conn, counts)
        return back(request, f"Counted {len(counts)} product(s)")

    # --- invoices ------------------------------------------------------------------------------

    @app.get("/invoices", response_class=HTMLResponse)
    def invoices_page(request: Request):
        with db.session(cfg) as conn:
            rows = invoices.recent(conn)
            products = catalog.products(conn)
            suppliers = catalog.suppliers(conn)
        return page(request, "invoices.html", invoices=rows, products=products, suppliers=suppliers,
                    key_present=cfg.anthropic_key_present)

    @app.post("/invoices/upload")
    async def upload_invoice(request: Request, image: UploadFile = File(...)):
        rel = f"invoices/{uuid.uuid4().hex[:12]}.jpg"
        (cfg.media_dir / rel).write_bytes(await image.read())
        with db.session(cfg) as conn:
            invoice_id = invoices.create_from_photo(conn, rel)
        status = await run_in_threadpool(invoices.process, cfg, invoice_id)
        return RedirectResponse(f"/invoices/{invoice_id}?msg=" + quote(f"Invoice read: {status}"), status_code=303)

    @app.post("/invoices/manual")
    async def manual_invoice(request: Request):
        form = await request.form()
        entries = []
        for i in range(12):
            pid, qty = form.get(f"product_{i}"), form.get(f"qty_{i}")
            if pid and qty:
                entries.append({"product_id": int(pid), "qty": float(qty), "unit": form.get(f"unit_{i}", "case"),
                                "unit_cost": float(form[f"cost_{i}"]) if form.get(f"cost_{i}") else None,
                                "expiry_date": form.get(f"expiry_{i}") or None,
                                "is_free": bool(form.get(f"free_{i}"))})
        if not entries:
            return back(request, "No lines entered")
        supplier = form.get("supplier_id")
        with db.session(cfg) as conn:
            invoice_id = invoices.create_manual(conn, int(supplier) if supplier else None, entries)
        return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

    @app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
    def invoice_page(request: Request, invoice_id: int):
        with db.session(cfg) as conn:
            inv = conn.execute("SELECT i.*, s.name AS supplier FROM invoices i "
                               "LEFT JOIN suppliers s ON s.id = i.supplier_id WHERE i.id = ?", (invoice_id,)).fetchone()
            if inv is None:
                raise HTTPException(404)
            lines = invoices.lines(conn, invoice_id)
            suggestions = {l["id"]: catalog.fuzzy_candidates(conn, l["text_as_written"])
                           for l in lines if l["product_id"] is None}
            products = catalog.products(conn)
            suppliers = catalog.suppliers(conn)
            preview = render.invoice_message(conn, invoice_id, "km")
        return page(request, "invoice.html", inv=inv, lines=lines, products=products, suppliers=suppliers,
                    suggestions=suggestions, preview=preview, extracted=invoices.extracted_json(inv))

    @app.post("/invoices/{invoice_id}/lines/{line_id}")
    async def invoice_line(request: Request, invoice_id: int, line_id: int, product_id: str = Form("")):
        with db.session(cfg) as conn:
            if product_id == "skip":
                invoices.skip_line(conn, line_id)
            elif product_id:
                invoices.set_line_product(conn, line_id, int(product_id))
        await _refresh_invoice(invoice_id)
        return back(request)

    @app.post("/invoices/{invoice_id}/supplier")
    async def invoice_supplier(request: Request, invoice_id: int, supplier_id: int = Form(...)):
        with db.session(cfg) as conn:
            invoices.set_supplier(conn, invoice_id, supplier_id)
        await _refresh_invoice(invoice_id)
        return back(request)

    @app.post("/invoices/{invoice_id}/confirm")
    async def invoice_confirm(request: Request, invoice_id: int):
        try:
            with db.session(cfg) as conn:
                units, n = invoices.confirm(conn, invoice_id, by="dashboard")
        except ValueError as exc:
            return back(request, str(exc))
        await _refresh_invoice(invoice_id, t(cfg.shop.default_lang, "invoice_added", units=units, lines=n))
        return back(request, f"Added +{units} units from {n} line(s) to stock")

    @app.post("/invoices/{invoice_id}/reject")
    async def invoice_reject(request: Request, invoice_id: int):
        with db.session(cfg) as conn:
            invoices.reject(conn, invoice_id)
        await _refresh_invoice(invoice_id)
        return back(request, "Invoice discarded")

    @app.post("/invoices/{invoice_id}/bonus")
    async def invoice_bonus(request: Request, invoice_id: int, product_id: int = Form(...), units: int = Form(...)):
        with db.session(cfg) as conn:
            invoices.add_bonus(conn, invoice_id, product_id, units)
        await _refresh_invoice(invoice_id)
        return back(request, f"Free bonus +{units} recorded")

    async def _refresh_invoice(invoice_id: int, footer: str | None = None) -> None:
        if app.state.bot is not None:
            await digest.refresh_invoice(app.state.bot.bot, cfg, invoice_id, footer)

    # --- orders -----------------------------------------------------------------------------------

    @app.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request, lang: str = "en"):
        with db.session(cfg) as conn:
            forecast.settle_follow_rate(conn)
            plans = forecast.build_plans(conn, cfg.forecast)
            follow = metrics.follow_rate(conn)
            simulated = stock.has_simulated(conn)
        previews = {p.supplier_id: render.order_message(p, lang, simulated) for p in plans}
        reasons = {(p.supplier_id, l.product_id): render.line_reason(l, p, "en") for p in plans for l in p.lines}
        return page(request, "orders.html", plans=plans, previews=previews, reasons=reasons, follow=follow,
                    lang=lang, today=clock.today(), sleeping=sum(p.sleeping_value for p in plans),
                    timedelta=timedelta)

    return app
