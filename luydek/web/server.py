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
MIN_PHOTOS = 5  # enrollment photos per pack before recognition is dependable
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _int(value) -> int | None:
    """A form number that may be blank."""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


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
    def asset(path: str) -> str:
        """A static URL that changes whenever the file does, so browsers never keep stale CSS/JS."""
        try:
            return f"/static/{path}?v={int((HERE / 'static' / path).stat().st_mtime)}"
        except OSError:
            return f"/static/{path}"

    templates.env.globals.update(pack_label=catalog.pack_label, display_name=catalog.display_name, t=t, asset=asset)

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
        sleeping = round(sum(p.sleeping_value for p in plans), 2)
        table.sort(key=lambda r: r["urgency"])

        # "Needs attention", in the owner's words rather than as table flags.
        next_visit = {p.supplier_id: p for p in plans}
        attention = {
            "out": [r for r in table if r["status"] == "out"],
            "low": [r for r in table if r["status"] == "low"],
            "expiring": sorted((r for r in table if r["expiring"]), key=lambda r: r["expiry"]),
            "sleeping": sorted((l for p in plans for l in p.of("sleeping")), key=lambda l: -l.value),
        }
        visits = sorted(plans, key=lambda p: (p.visit_date, p.name))

        summary = [f"{today['txns']} sale{'s' if today['txns'] != 1 else ''} recorded today"]
        if pending:
            summary.append(f"{len(pending)} item{'s' if len(pending) != 1 else ''} waiting for your check")
        if attention["out"]:
            summary.append(f"{len(attention['out'])} out of stock")
        if attention["low"]:
            summary.append(f"{len(attention['low'])} running low")
        return page(request, "dashboard.html", table=table, pending=pending, pending_cands=pending_cands,
                    recent=recent, today=today, trend=trend, taps=taps, follow=follow, sleeping=sleeping,
                    packs=packs, users=users, reasons=list(stock.ADJUST_REASONS), attention=attention,
                    visits=visits, next_visit=next_visit, summary=summary, now=clock.now(),
                    suppliers=sorted({(r["supplier_name"] or "") for r in table}))

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

    def _product(conn, product_id: int):
        row = conn.execute(
            "SELECT p.*, s.name AS supplier_name, s.emoji FROM products p "
            "LEFT JOIN suppliers s ON s.id = p.supplier_id WHERE p.id = ? AND p.active = 1", (product_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "No such product")
        return row

    @app.get("/catalog", response_class=HTMLResponse)
    def catalog_page(request: Request):
        with db.session(cfg) as conn:
            products = catalog.catalog_rows(conn)
            suppliers = catalog.suppliers(conn)
            ignore = {r["ignore_label"]: r["n"] for r in conn.execute(
                "SELECT ignore_label, COUNT(*) AS n FROM gallery_images WHERE ignore_label IS NOT NULL "
                "GROUP BY ignore_label")}
        return page(request, "catalog.html", products=products, suppliers=suppliers, ignore=ignore,
                    ignore_labels=IGNORE_LABELS, min_photos=MIN_PHOTOS, weekdays=WEEKDAYS)

    @app.get("/catalog/new", response_class=HTMLResponse)
    def new_product_page(request: Request):
        with db.session(cfg) as conn:
            suppliers = catalog.suppliers(conn)
        return page(request, "product_new.html", suppliers=suppliers, weekdays=WEEKDAYS, min_photos=MIN_PHOTOS)

    @app.post("/catalog/products")
    async def add_product(request: Request):
        f = await request.form()
        name_en = str(f.get("name_en", "")).strip()
        if not name_en:
            return back(request, "Give the product a name")
        with db.session(cfg) as conn:
            supplier_id = _int(f.get("supplier_id"))
            if str(f.get("new_supplier", "")).strip():
                supplier_id = catalog.add_supplier(conn, str(f["new_supplier"]), _int(f.get("new_supplier_day")) or 0)
            product_id = catalog.add_product(
                conn, sku=catalog.unique_sku(conn, name_en), name_en=name_en,
                name_km=str(f.get("name_km", "")).strip() or name_en, unit=str(f.get("unit") or "piece").strip(),
                case_size=_int(f.get("case_size")) or 1, supplier_id=supplier_id,
                is_critical=bool(f.get("is_critical")), shelf_life_days=_int(f.get("shelf_life_days")),
                unit_cost=_float(f.get("unit_cost")) or 0.0, barcode=str(f.get("barcode", "")).strip() or None,
            )
            starting = _int(f.get("starting_stock"))
            if starting is not None and starting >= 0:
                stock.opening_count(conn, {product_id: starting})
            pack_id = catalog.default_pack_id(conn, product_id)
        if f.get("then") == "another":
            return RedirectResponse("/catalog/new?msg=" + quote(f"Added {name_en}. Add the next one."), status_code=303)
        return RedirectResponse(f"/catalog/enroll?pack_id={pack_id}&msg=" + quote(
            f"Added {name_en}. Now take {MIN_PHOTOS} photos of it on the counter mat."), status_code=303)

    @app.get("/catalog/enroll", response_class=HTMLResponse)
    def enroll_page(request: Request, pack_id: int | None = None, ignore: str | None = None):
        with db.session(cfg) as conn:
            pk = catalog.pack(conn, pack_id) if pack_id else None
            photos = conn.execute(
                "SELECT * FROM gallery_images WHERE " + ("pack_id = ?" if pk else "ignore_label = ?")
                + " ORDER BY id DESC", (pack_id if pk else ignore,)).fetchall()
            reference = db.get_setting(conn, pipeline.REFERENCE_KEY)
        if pk is None and ignore not in IGNORE_LABELS:
            raise HTTPException(404)
        return page(request, "enroll.html", pk=pk, ignore=ignore, photos=photos, min_photos=MIN_PHOTOS,
                    calibrated=bool(reference))

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

    @app.get("/catalog/{product_id}", response_class=HTMLResponse)
    def product_page(request: Request, product_id: int):
        with db.session(cfg) as conn:
            product = _product(conn, product_id)
            packs = conn.execute(
                "SELECT pk.*, (SELECT COUNT(*) FROM gallery_images g WHERE g.pack_id = pk.id) AS photos "
                "FROM product_packs pk WHERE pk.product_id = ? ORDER BY pk.units, pk.id", (product_id,)).fetchall()
            photos = catalog.gallery(conn, product_id)
            aliases = catalog.aliases(conn, product_id)
            suppliers = catalog.suppliers(conn)
            level = stock.on_hand(conn).get(product_id)
            moves = conn.execute(
                "SELECT * FROM stock_movements WHERE product_id = ? ORDER BY created_at DESC, id DESC LIMIT 8",
                (product_id,)).fetchall()
        return page(request, "product.html", p=product, packs=packs, photos=photos, aliases=aliases,
                    suppliers=suppliers, level=level, moves=moves, min_photos=MIN_PHOTOS)

    @app.post("/catalog/{product_id}/edit")
    async def edit_product(request: Request, product_id: int):
        f = await request.form()
        name_en = str(f.get("name_en", "")).strip()
        if not name_en:
            return back(request, "The product needs a name")
        with db.session(cfg) as conn:
            _product(conn, product_id)
            catalog.update_product(
                conn, product_id, name_en=name_en, name_km=str(f.get("name_km", "")).strip() or name_en,
                unit=str(f.get("unit") or "piece").strip(), case_size=_int(f.get("case_size")) or 1,
                supplier_id=_int(f.get("supplier_id")), is_critical=int(bool(f.get("is_critical"))),
                shelf_life_days=_int(f.get("shelf_life_days")), unit_cost=_float(f.get("unit_cost")) or 0.0,
                barcode=str(f.get("barcode", "")).strip() or None,
            )
        return back(request, "Saved")

    @app.post("/catalog/{product_id}/stock")
    def count_product(request: Request, product_id: int, counted: str = Form("")):
        n = _int(counted)
        if n is None or n < 0:
            return back(request, "Enter how many are on the shelf")
        with db.session(cfg) as conn:
            stock.opening_count(conn, {product_id: n})
        return back(request, f"Stock set to {n}")

    @app.post("/catalog/{product_id}/archive")
    def archive_product(request: Request, product_id: int):
        with db.session(cfg) as conn:
            name = _product(conn, product_id)["name_en"]
            catalog.archive_product(conn, product_id)
        return RedirectResponse("/catalog?msg=" + quote(f"Removed {name} from the catalog (its history is kept)"),
                                status_code=303)

    @app.post("/catalog/{product_id}/packs")
    def add_pack(request: Request, product_id: int, label: str = Form(""), units: str = Form(""),
                 barcode: str = Form("")):
        n = _int(units)
        if not label.strip() or not n or n < 2:
            return back(request, "A multipack needs a name and 2 or more units")
        with db.session(cfg) as conn:
            catalog.add_pack(conn, product_id, label.strip(), n, barcode.strip() or None)
        return back(request, f"Added {label}. Take photos of it so the camera knows it.")

    @app.post("/catalog/packs/{pack_id}/delete")
    def delete_pack(request: Request, pack_id: int):
        with db.session(cfg) as conn:
            ok = catalog.delete_pack(conn, pack_id)
        return back(request, "Pack removed" if ok else "This pack is already in recorded sales, so it stays")

    @app.post("/catalog/{product_id}/aliases")
    def add_alias(request: Request, product_id: int, alias: str = Form("")):
        if not alias.strip():
            return back(request)
        with db.session(cfg) as conn:
            catalog.add_alias(conn, product_id, alias, "owner")
        return back(request, f"'{alias}' now means this product")

    @app.post("/catalog/aliases/{alias_id}/delete")
    def delete_alias(request: Request, alias_id: int):
        with db.session(cfg) as conn:
            catalog.delete_alias(conn, alias_id)
        return back(request)

    @app.post("/api/gallery/{gid}/delete")
    def api_delete_gallery(gid: int):
        with db.session(cfg) as conn:
            if not catalog.delete_gallery_image(conn, cfg.media_dir, gid):
                raise HTTPException(404, "No such photo")
        return {"deleted": gid}

    @app.post("/catalog/gallery/delete")
    async def delete_gallery_many(request: Request):
        ids = [int(v) for v in (await request.form()).getlist("gid") if str(v).isdigit()]
        with db.session(cfg) as conn:
            n = sum(catalog.delete_gallery_image(conn, cfg.media_dir, gid) for gid in ids)
        return back(request, f"Deleted {n} photo(s)")

    @app.post("/catalog/suppliers")
    async def add_supplier(request: Request):
        f = await request.form()
        name = str(f.get("name", "")).strip()
        if not name:
            return back(request, "Give the supplier a name")
        with db.session(cfg) as conn:
            catalog.add_supplier(conn, name, _int(f.get("visit_weekday")) or 0,
                                 emoji=str(f.get("emoji") or "🚚"), min_order=_float(f.get("min_order")) or 0,
                                 contact=str(f.get("contact", "")).strip() or None)
        return back(request, f"Added supplier {name}")

    @app.post("/catalog/suppliers/{supplier_id}")
    async def edit_supplier(request: Request, supplier_id: int):
        f = await request.form()
        with db.session(cfg) as conn:
            catalog.update_supplier(conn, supplier_id, name=str(f.get("name", "")).strip() or "Supplier",
                                    visit_weekday=_int(f.get("visit_weekday")) or 0,
                                    min_order=_float(f.get("min_order")) or 0,
                                    contact=str(f.get("contact", "")).strip() or None)
        return back(request, "Supplier saved")

    @app.post("/catalog/opening-count")
    async def opening_count(request: Request):
        form = await request.form()
        counts = {}
        for key, value in form.items():
            n = _int(value)
            if key.startswith("count_") and n is not None and n >= 0:
                counts[int(key[6:])] = n
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
