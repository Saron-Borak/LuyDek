"""Dashboard, counter capture page and APIs. The Telegram bot runs in the same process.

    python -m luydek            # http://localhost:8000 (uvicorn with create_app as a factory)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import numpy as np
import segno
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from starlette.concurrency import run_in_threadpool

from .. import clock, db, render
from ..bot import app as bot_app
from ..bot import digest
from ..config import Config, get_config
from ..i18n import t
from ..recognition import cutout, labels, pipeline, segment, tray
from ..services import catalog, forecast, invoices, learn, loose, metrics, report, stock, transactions
from . import charts as charts_mod
from . import miniapp

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


def _wants_json(request: Request) -> bool:
    """The page's own fetch() calls ask for JSON; plain form posts get a redirect back."""
    return "application/json" in request.headers.get("accept", "")


def _picker_items(conn) -> list[dict]:
    """Every pack for the product pickers, the last two weeks' best sellers first.

    `find` is everything a search should match: English and Khmer names, the
    pack's name and the other names invoices and the owner use. `base` marks the
    one-unit pack, for pickers that choose a product rather than a pack.
    """
    since = clock.stamp(clock.now() - timedelta(days=14))
    sold = {r[0]: r[1] for r in conn.execute(
        "SELECT product_id, -SUM(qty) FROM stock_movements WHERE type = 'sale' AND created_at >= ? GROUP BY product_id",
        (since,))}
    aliases: dict[int, list[str]] = {}
    for r in conn.execute("SELECT product_id, alias_norm FROM product_aliases"):
        aliases.setdefault(r[0], []).append(r[1])
    packs = catalog.packs(conn)
    base = {}
    for pk in packs:
        key = (pk["units"], pk["pack_id"])
        if pk["product_id"] not in base or key < base[pk["product_id"]]:
            base[pk["product_id"]] = key
    by_product, by_pack = catalog.photo_ids(conn)
    items = [{
        "pack": pk["pack_id"], "product": pk["product_id"], "label": catalog.pack_label(pk, "en"),
        "img": (f"/thumb/{by_pack.get(pk['pack_id']) or by_product.get(pk['product_id'])}.jpg"
                if by_pack.get(pk["pack_id"]) or by_product.get(pk["product_id"]) else None),
        "km": pk["name_km"], "units": pk["units"], "sold": sold.get(pk["product_id"], 0),
        "base": base[pk["product_id"]][1] == pk["pack_id"],
        "find": " ".join([pk["name_en"], pk["name_km"], pk["label"], *aliases.get(pk["product_id"], [])]).lower(),
    } for pk in packs]
    items.sort(key=lambda i: (-i["sold"], i["units"], i["label"]))
    return items


def _lan_ip() -> str | None:
    """This computer's address on the shop Wi-Fi, for phones to reach it.

    Connecting a UDP socket sends nothing; it only makes the system pick the
    network interface it would use, whose address we then read.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        ip = None
    finally:
        s.close()
    return None if not ip or ip.startswith("127.") else ip


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
        await bot_app.set_menu_button(tg.bot, cfg)
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

    @app.exception_handler(segment.SceneChanged)
    async def scene_changed(request: Request, exc: segment.SceneChanged):
        """Nothing is recorded; the counter page asks the owner to recalibrate."""
        return JSONResponse(status_code=409, content={
            "scene_changed": True, "changed": round(exc.changed, 2),
            "detail": f"The camera moved or the light changed ({exc}). "
                      "Clear the counter and press Calibrate.",
        })
    app.state.cfg = cfg
    app.state.bot = None
    app.state.tasks = set()

    def _read(txn_id: int) -> None:
        labels.settle(cfg, txn_id)
        labels.name_new(cfg, txn_id)  # something it can't place at all: read its name for a one-tap "add it"
        tray.settle(cfg, txn_id)

    def read_labels_later(txn_id: int) -> None:
        """Settle look-alikes and unsure tray counts in the background; the counter page never waits for it."""
        task = asyncio.create_task(asyncio.to_thread(_read, txn_id))
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
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
    templates.env.filters["plural"] = loose.plural  # {{ unit|plural(n) }}: box -> boxes, tablet -> tablets

    async def refresh_item(item_id: int) -> None:
        if app.state.bot is not None:
            await digest.refresh_item(app.state.bot.bot, cfg, item_id)

    miniapp.register(app, cfg, templates, _picker_items, refresh_item)

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        with db.session(cfg) as conn:
            simulated = stock.has_simulated(conn)
            pending = conn.execute("SELECT COUNT(*) FROM transaction_items WHERE status = 'pending'").fetchone()[0]
            verifiers = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
            photos = catalog.photo_ids(conn)
        return templates.TemplateResponse(request, name, {
            "cfg": cfg, "bot": app.state.bot, "simulated": simulated, "pending_count": pending,
            "photos": {"product": photos[0], "pack": photos[1]},
            "team": cfg.shop.team_tools, "verifiers": verifiers,
            "msg": request.query_params.get("msg"), "active": name.split(".")[0], **ctx,
        })

    def back(request: Request, msg: str | None = None, default: str = "/") -> RedirectResponse:
        target = request.headers.get("referer") or default
        target = target.split("?")[0]
        if msg:
            target += "?msg=" + quote(msg)
        return RedirectResponse(target, status_code=303)

    # --- dashboard -------------------------------------------------------------------

    def _checks(conn) -> dict:
        """What the check list needs: the waiting items, their alternatives and today's sales count."""
        pending = transactions.pending_items(conn)
        by_product, by_pack = catalog.photo_ids(conn)
        return {
            "pending": pending,
            "to_name": transactions.unknown_items(conn),
            "photos": {"product": by_product, "pack": by_pack},
            "pending_cands": {it["id"]: [(catalog.pack(conn, p), s) for p, s in transactions.candidates(it)[:4]]
                              for it in pending},
            "today": metrics.today_counts(conn),
        }

    @app.get("/fragments/checks", response_class=HTMLResponse)
    def checks_fragment(request: Request):
        """The check list alone: the Today page polls it to show new items without a reload."""
        with db.session(cfg) as conn:
            ctx = _checks(conn)
        return templates.TemplateResponse(request, "_checks.html", ctx)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(HERE / "static" / "brand" / "favicon.ico", media_type="image/x-icon",
                            headers={"Cache-Control": "public, max-age=604800"})

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        """Makes "Add to Home screen" show LuyDek's name, icon and colour."""
        return FileResponse(HERE / "static" / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/thumb/{gid}.jpg")
    def thumb(gid: int):
        """A small photo of the product for lists and pickers, made once and kept.

        The product is cut out and set on white, like a shop catalogue. If this photo won't
        cut cleanly (a wide shot with the table in it), the next few photos of the same pack
        are tried; if none will, the plain photo is used.
        """
        with db.session(cfg) as conn:
            row = conn.execute("SELECT crop_path, pack_id FROM gallery_images WHERE id = ?", (gid,)).fetchone()
            if row is None or not (cfg.media_dir / row["crop_path"]).exists():
                raise HTTPException(404, "No such photo")
            others = [r["crop_path"] for r in conn.execute(
                "SELECT crop_path FROM gallery_images WHERE pack_id = ? AND id != ? ORDER BY id LIMIT 5",
                (row["pack_id"], gid))] if row["pack_id"] is not None else []
        # Named after the photo file, not the id: an id can be reused after a delete, a file name can't.
        path = cfg.media_dir / "thumbs" / f"{Path(row['crop_path']).stem}_w.jpg"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            im = Image.open(cfg.media_dir / row["crop_path"]).convert("RGB")
            for crop in [row["crop_path"], *others]:
                if not (cfg.media_dir / crop).exists():
                    continue
                white = cutout.on_white(np.asarray(Image.open(cfg.media_dir / crop).convert("RGB")))
                if white is not None:
                    im = Image.fromarray(white)
                    break
            else:
                im.thumbnail((160, 160))
            im.save(path, quality=88)
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with db.session(cfg) as conn:
            table = stock.stock_table(conn, cfg.forecast.expiry_warning_days)
            checks = _checks(conn)
            pending = checks["pending"]
            recent = transactions.recent(conn, 10)
            today = checks["today"]
            trend = metrics.weekly_trend(conn)
            taps = metrics.taps_per_day(conn)
            follow = metrics.follow_rate(conn)
            plans = forecast.build_plans(conn, cfg.forecast)
            picker = _picker_items(conn)
            users = conn.execute("SELECT COUNT(*) FROM bot_users").fetchone()[0]
            unfinished = learn.unfinished(conn)
            set_aside_today = conn.execute(
                "SELECT COUNT(*) FROM ignored_detections WHERE created_at >= ? AND fixed_item_id IS NULL",
                (clock.stamp(datetime.combine(clock.today(), datetime.min.time())),)).fetchone()[0]
            busy = metrics.busy_hours(conn)
            spark_start = clock.today() - timedelta(days=13)
            _, _, sold_by_day = stock.daily_series(conn, spark_start, clock.today() + timedelta(days=1))
            now = clock.now()
            week_sold = conn.execute(
                "SELECT -SUM(CASE WHEN created_at >= :d7 THEN qty ELSE 0 END), "
                "       -SUM(CASE WHEN created_at < :d7 THEN qty ELSE 0 END) "
                "FROM stock_movements WHERE type = 'sale' AND created_at >= :d14",
                {"d7": clock.stamp(now - timedelta(days=7)), "d14": clock.stamp(now - timedelta(days=14))},
            ).fetchone()
        this_week, last_week = (week_sold[0] or 0), (week_sold[1] or 0)
        week = {"this": this_week, "last": last_week,
                "pct": round((this_week - last_week) / last_week * 100) if last_week else None}
        sleeping = round(sum(p.sleeping_value for p in plans), 2)
        table.sort(key=lambda r: r["urgency"])

        # Two weeks at a glance: the shop's line in the Sales tile, a small bar strip per product.
        spark_days = [spark_start + timedelta(days=i) for i in range(14)]
        keys = [d.isoformat() for d in spark_days]
        shop = [sum(by_day.get(k, 0) for by_day in sold_by_day.values()) for k in keys]
        sales_spark = charts_mod.sparkline(
            shop, charts_mod.AMBER, w=200, h=36, kind="line", label=f"Units sold per day, last 14 days: {sum(shop)}",
            tips=[f"{'Today' if i == 13 else d.strftime('%a %d %b')}: {n} sold" for i, (d, n) in enumerate(zip(spark_days, shop, strict=True))],
        ) if any(shop) else None
        for r in table:
            vals = [sold_by_day.get(r["id"], {}).get(k, 0) for k in keys]
            r["sold_14d"] = sum(vals)
            r["spark"] = charts_mod.sparkline(vals, label=f"Sold per day, last 14 days: {sum(vals)} in all")
        busy_chart = charts_mod.hours_chart(busy["counts"], busy["days"]) if busy["total"] >= 5 else None
        busy_peak = charts_mod.hour_name(busy["peak"]) if busy_chart else None

        # "Needs attention", in the owner's words rather than as table flags.
        next_visit = {p.supplier_id: p for p in plans}
        attention = {
            "out": [r for r in table if r["status"] == "out"],
            "low": [r for r in table if r["status"] == "low"],
            "expiring": sorted((r for r in table if r["expiring"]), key=lambda r: r["expiry"]),
            "sleeping": sorted((l for p in plans for l in p.of("sleeping")), key=lambda l: -l.value),
        }
        visits = sorted(plans, key=lambda p: (p.visit_date, p.name))

        # Waiting items are counted live in the check card right below, not here.
        summary = [f"{today['txns']} sale{'s' if today['txns'] != 1 else ''} recorded today"]
        if attention["out"]:
            summary.append(f"{len(attention['out'])} out of stock")
        if attention["low"]:
            summary.append(f"{len(attention['low'])} running low")
        return page(request, "dashboard.html", table=table, pending=pending, pending_cands=checks["pending_cands"],
                    to_name=checks["to_name"], unfinished=unfinished, set_aside_today=set_aside_today,
                    recent=recent, today=today, trend=trend, taps=taps, follow=follow, sleeping=sleeping,
                    picker=picker, users=users, reasons=list(stock.ADJUST_REASONS), attention=attention,
                    visits=visits, next_visit=next_visit, summary=summary, now=clock.now(), week=week,
                    sales_spark=sales_spark, busy_chart=busy_chart, busy_peak=busy_peak, busy=busy,
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
            result = await run_in_threadpool(run)
        except (KeyError, ValueError) as exc:
            if _wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, f"Could not update item {item_id}: {exc}")
        if app.state.bot is not None and not result.get("already"):
            await digest.refresh_item(app.state.bot.bot, cfg, item_id)
        if _wants_json(request):
            return {"item": item_id, "status": result["status"], "count": result.get("count"),
                    "already": result["already"]}
        return back(request)

    @app.post("/items/{item_id}/new-product")
    async def new_product(request: Request, item_id: int, name: str = Form(""), name_km: str = Form(""),
                          pack_id: int | None = Form(None)):
        """Something the camera didn't know: the owner names it (a new product, learned from this sale),
        or says which listed product it is. Other unanswered sales that look like it are re-guessed as it."""
        def run() -> dict:
            with db.session(cfg) as conn:
                opts = {"by": "dashboard", "learn": pipeline.learner(cfg), "media_dir": cfg.media_dir,
                        "delete_photo": cfg.verification.delete_photos_after_verify}
                if pack_id is not None:
                    if catalog.pack(conn, pack_id) is None:
                        raise ValueError(f"no product pack {pack_id}")
                    result = {**transactions.name_item(conn, item_id, pack_id, **opts), "pack_id": pack_id,
                              "created": False}
                else:
                    result = learn.add_from_item(conn, item_id, name, name_km, **opts)
                regroup = pipeline.regrouper(cfg)
                result["regrouped"] = (regroup(conn, result["pack_id"], skip=item_id)
                                       if regroup and not result["already"] else [])
                return result
        try:
            result = await run_in_threadpool(run)
        except (KeyError, ValueError) as exc:
            if _wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, f"Could not add it: {exc}")
        if app.state.bot is not None and not result["already"]:
            for changed in [item_id, *result["regrouped"]]:
                await digest.refresh_item(app.state.bot.bot, cfg, changed)
        if _wants_json(request):
            return {"item": item_id, "status": result["status"], "already": result["already"],
                    "created": result.get("created", False), "name": result.get("name"),
                    "regrouped": result["regrouped"]}
        n = len(result["regrouped"])
        more = f" {n} more sale{'s' if n != 1 else ''} look like it: check {'them' if n != 1 else 'it'} above." if n else ""
        return back(request, (f"Added {result['name']} to your products." if result.get("created") else "Saved.") + more)

    @app.post("/items/{item_id}/several")
    async def several_items(request: Request, item_id: int, picks: str = Form("")):
        """One photo, several items the camera saw as one: "pack:count,pack:count"."""
        pairs = []
        for part in picks.split(","):
            if ":" in part:
                pack, n = part.split(":", 1)
                if pack.strip().isdigit():
                    pairs.append((int(pack), int(n) if n.strip().isdigit() else 1))
        try:
            with db.session(cfg) as conn:
                made = transactions.several(conn, item_id, pairs, by="dashboard", media_dir=cfg.media_dir,
                                            delete_photo=cfg.verification.delete_photos_after_verify)
        except (KeyError, ValueError) as exc:
            if _wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, f"Could not record them: {exc}")
        if app.state.bot is not None:
            await digest.refresh_item(app.state.bot.bot, cfg, item_id)
        if _wants_json(request):
            return {"item": item_id, "status": "split", "items": made}
        return back(request, f"Recorded {len(made)} item{'s' if len(made) != 1 else ''} from one photo.")

    @app.post("/items/{item_id}/fix")
    async def fix_item(request: Request, item_id: int, action: str = Form(...), pack_id: int | None = Form(None),
                       count: int | None = Form(None)):
        """Correct a sale that was already recorded (from the camera log or a product's history)."""
        try:
            with db.session(cfg) as conn:
                result = transactions.fix(conn, item_id, action, pack_id=pack_id, count=count, by="dashboard",
                                          learn=pipeline.learner(cfg))
        except (KeyError, ValueError) as exc:
            if _wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, f"Could not fix it: {exc}")
        if _wants_json(request):
            return {"item": item_id, **result}
        return back(request, "Fixed. Stock and the camera were both corrected.")

    @app.post("/ignored/{ignored_id}/sale")
    async def ignored_was_a_sale(request: Request, ignored_id: int, pack_id: int | None = Form(None),
                                 name: str = Form("")):
        """Something the camera set aside was a sale after all: which product (or a new one)."""
        def run() -> dict:
            with db.session(cfg) as conn:
                item_id = transactions.item_from_ignored(conn, ignored_id)
                opts = {"by": "dashboard", "learn": pipeline.learner(cfg), "media_dir": cfg.media_dir}
                if pack_id is not None:
                    return {**transactions.name_item(conn, item_id, pack_id, **opts), "item": item_id}
                if name.strip():
                    return {**learn.add_from_item(conn, item_id, name, **opts), "item": item_id}
                return {"status": "unknown", "item": item_id}   # named later, from Today
        try:
            result = await run_in_threadpool(run)
        except (KeyError, ValueError) as exc:
            if _wants_json(request):
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, f"Could not record it: {exc}")
        if _wants_json(request):
            return result
        return back(request, "Recorded as a sale, and the camera learned from it."
                    if result["status"] == "corrected" else "Kept for you to name on Today.")

    @app.get("/log", response_class=HTMLResponse)
    def camera_log(request: Request, day: str | None = None):
        """Everything the camera did on one day: each sale with its items and how they were settled,
        and what it set aside, with the photo it saw. Anything can be fixed from here."""
        try:
            d = date.fromisoformat(day) if day else clock.today()
        except ValueError:
            d = clock.today()
        start, end = f"{d.isoformat()} 00:00:00", f"{(d + timedelta(days=1)).isoformat()} 00:00:00"
        with db.session(cfg) as conn:
            sales = transactions.day_log(conn, start, end)
            set_aside = transactions.ignored(conn, start, end)
            picker = _picker_items(conn)
        items = [it for s in sales for it in s["items"]]
        counts = {
            "sales": len(sales), "items": len(items),
            "auto": sum(1 for it in items if it["status"] == "auto"),
            "checked": sum(1 for it in items if it["status"] in ("confirmed", "corrected", "removed")),
            "waiting": sum(1 for it in items if it["status"] in ("pending", "unknown")),
            "set_aside": sum(1 for i in set_aside if not i["fixed_item_id"]),
        }
        return page(request, "log.html", day=d, prev=d - timedelta(days=1),
                    next=d + timedelta(days=1) if d < clock.today() else None, sales=sales, set_aside=set_aside,
                    counts=counts, picker=picker, fixable=transactions.FIXABLE)

    @app.get("/catalog/{product_id}/history", response_class=HTMLResponse)
    def product_history(request: Request, product_id: int):
        """All a product went through: every camera sighting and how it was settled, and every stock change."""
        with db.session(cfg) as conn:
            product = _product(conn, product_id)
            sightings = transactions.product_history(conn, product_id)
            moves = conn.execute(
                "SELECT * FROM stock_movements WHERE product_id = ? ORDER BY created_at, id", (product_id,)
            ).fetchall()
            picker = _picker_items(conn)
        ledger, level = [], 0
        for m in moves:
            level += m["qty"]
            ledger.append({**dict(m), "level": level})
        ledger.reverse()
        return page(request, "history.html", p=product, sightings=sightings, ledger=ledger, picker=picker,
                    fixable=transactions.FIXABLE,
                    active_tab=request.query_params.get("tab", "camera" if sightings or not ledger else "stock"))

    @app.post("/items/confirm-all")
    async def confirm_all(request: Request):
        """The web twin of Telegram's "confirm all as guessed": every waiting item that has a guess.

        `ids` limits it to the items the owner could see when they tapped, so an
        item from a sale that arrived since is never confirmed unseen.
        """
        shown = {int(v) for v in str((await request.form()).get("ids", "")).split(",") if v.strip().isdigit()}

        def run() -> list[int]:
            with db.session(cfg) as conn:
                ids = [r["id"] for r in transactions.pending_items(conn)
                       if r["pack_id"] is not None and (not shown or r["id"] in shown)]
                transactions.confirm_many(conn, ids, "dashboard", learn=pipeline.learner(cfg), media_dir=cfg.media_dir,
                                          delete_photo=cfg.verification.delete_photos_after_verify)
                return ids
        ids = await run_in_threadpool(run)
        if app.state.bot is not None:
            for item_id in ids:
                await digest.refresh_item(app.state.bot.bot, cfg, item_id)
        if _wants_json(request):
            return {"confirmed": ids}
        return back(request, f"Confirmed {len(ids)} item{'s' if len(ids) != 1 else ''}" if ids else "Nothing to confirm")

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
        if app.state.bot is not None and cfg.verification.daily_summary:
            await digest.send_day_summary(app.state.bot.bot, cfg)
            return back(request, f"End of day: {n} unchecked item(s) accepted as best guess; today's summary sent")
        return back(request, f"End of day: {n} unchecked item(s) accepted as best guess")

    @app.post("/actions/summary")
    async def action_summary(request: Request):
        if app.state.bot is None:
            return back(request, "Telegram bot is off (no TELEGRAM_BOT_TOKEN in .env)")
        await digest.send_day_summary(app.state.bot.bot, cfg)
        return back(request, "Today's summary sent")

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
        with db.session(cfg) as conn:
            calibrated = bool(db.get_setting(conn, pipeline.REFERENCE_KEY))
            mat_marked = pipeline.load_roi(conn) is not None
        # Camera setup stays folded away once the camera is calibrated and the mat is marked.
        return page(request, "counter.html", setup_open=not (calibrated and mat_marked))

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        """Everything install day needs, as a checklist with one button per step."""
        with db.session(cfg) as conn:
            ref = db.get_setting(conn, pipeline.REFERENCE_KEY)
            mat = pipeline.load_roi(conn)
            tray_roi = pipeline.load_roi(conn, pipeline.TRAY_KEY)
            rows = catalog.catalog_rows(conn)
            suppliers = catalog.suppliers(conn)
            loose_count = len(loose.loose_products(conn))
        calibrated_at = None
        if ref and (cfg.media_dir / ref).exists():
            calibrated_at = datetime.fromtimestamp((cfg.media_dir / ref).stat().st_mtime).strftime("%d %b %H:%M")
        photographed = sum(1 for r in rows if r["photos"] >= MIN_PHOTOS)
        uncounted = sum(1 for r in rows if not r["tracked"])
        no_supplier = sum(1 for r in rows if not r["supplier_id"])
        steps = [
            {"done": bool(ref), "title": "Calibrate the counter camera", "href": "/counter#setup", "button": "Open the camera",
             "detail": f"Last calibrated {calibrated_at}" if calibrated_at else
             "Point the camera straight down at the empty mat and press Calibrate."},
            {"done": mat is not None, "title": "Mark the mat", "href": "/counter#setup", "button": "Mark it",
             "detail": "Only items on the mat count; hands and things beside it are ignored." if mat else
             "Drag a box around the mat so things beside it never count as sales."},
            {"done": tray_roi is not None, "optional": True, "title": "Tablet tray (pharmacies)", "href": "/counter#setup",
             "button": "Mark the tray", "detail": (f"{loose_count} medicine{'s' if loose_count != 1 else ''} sold by the tablet. "
                                                   if loose_count else "") +
             "A small black card where loose tablets are counted. Skip it if you don't sell loose tablets."},
            {"done": rows and photographed == len(rows), "title": "Photograph your products",
             "href": "/catalog#needs-photos", "button": "Take photos",
             "detail": f"{photographed} of {len(rows)} products have enough photos. Start with the best sellers."},
            {"done": rows and uncounted == 0, "title": "Count the stock on the shelf", "href": "/catalog#count",
             "button": "Count stock", "detail": "Everything is counted." if not uncounted else
             f"{uncounted} product{'s have' if uncounted != 1 else ' has'} never been counted."},
            {"done": bool(suppliers) and no_supplier == 0, "title": "Suppliers and their visit days",
             "href": "/catalog#suppliers", "button": "Suppliers",
             "detail": f"{len(suppliers)} supplier{'s' if len(suppliers) != 1 else ''}"
                       + (f"; {no_supplier} product{'s' if no_supplier != 1 else ''} without one." if no_supplier else ".")},
            {"done": app.state.bot is not None, "title": "Telegram bot", "href": None, "button": None,
             "detail": (f"On. Each phone that should answer sends /start {cfg.shop.pin} to the bot."
                        if app.state.bot is not None else
                        "Off. Put TELEGRAM_BOT_TOKEN in .env and restart; until then, check items on the Today page.")},
            {"done": cfg.anthropic_key_present, "optional": True, "title": "Reading invoices and labels", "href": None,
             "button": None, "detail": "On: invoice photos and look-alike labels are read automatically."
             if cfg.anthropic_key_present else "Off. Put ANTHROPIC_API_KEY in .env; until then deliveries are typed in."},
        ]
        required = [s for s in steps if not s.get("optional")]

        # "Open on your phone": a QR code to scan once. With --public-url (an https:// address, e.g.
        # from `tailscale serve`) phones use that, and can be the counter camera too; otherwise the
        # plain address on the shop Wi-Fi, where browsers keep the camera to this computer.
        public = os.environ.get("LUYDEK_PUBLIC_URL")
        if public:
            phone = {"url": public, "secure": public.startswith("https://"), "reachable": True}
        else:
            host = os.environ.get("LUYDEK_HOST", "127.0.0.1")
            port = request.url.port or int(os.environ.get("LUYDEK_PORT", "8000"))
            ip = _lan_ip()
            phone = {"secure": False, "reachable": host in ("0.0.0.0", "::") or (ip is not None and host == ip)}
            if ip:
                phone["url"] = f"http://{ip}:{port}/"
        if phone.get("url"):
            phone["qr"] = segno.make(phone["url"], error="m").svg_inline(scale=5, dark="#26215C", border=2)
        return page(request, "setup.html", steps=steps, done=sum(1 for s in required if s["done"]),
                    total=len(required), phone=phone)

    @app.get("/api/state")
    def api_state():
        with db.session(cfg) as conn:
            ref = db.get_setting(conn, pipeline.REFERENCE_KEY)
            gallery = conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0]
            open_txn = conn.execute("SELECT id FROM transactions WHERE status = 'open' ORDER BY id DESC").fetchone()
            roi = pipeline.load_roi(conn)
            tray_roi = pipeline.load_roi(conn, pipeline.TRAY_KEY)
        return {"reference": f"/media/{ref}" if ref else None, "gallery": gallery, "recognizer_ready": pipeline.ready(),
                "finder": pipeline.finder_name(),
                "open_txn": open_txn["id"] if open_txn else None, "roi": roi, "tray": tray_roi}

    @app.post("/api/roi")
    def api_roi(x0: float | None = Form(None), y0: float | None = Form(None), x1: float | None = Form(None),
                y1: float | None = Form(None), clear: bool = Form(False), area: str = Form("mat")):
        """Mark the mat (or, with area=tray, the tablet tray) on the camera view as fractions of the frame,
        or clear it."""
        if area not in ("mat", "tray"):
            raise HTTPException(400, "area is mat or tray")
        if not clear and None in (x0, y0, x1, y1):
            raise HTTPException(400, "give x0, y0, x1 and y1, or clear")
        key = pipeline.TRAY_KEY if area == "tray" else pipeline.ROI_KEY
        try:
            with db.session(cfg) as conn:
                roi = pipeline.save_roi(conn, None if clear else (x0, y0, x1, y1), key)
                if area == "tray":
                    loose.forget_units(conn)  # a tray in a new spot looks different: learn tablet sizes again
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return {"roi": roi, "area": area}

    @app.post("/api/calibrate")
    async def api_calibrate(image: UploadFile = File(...)):
        data = await image.read()
        with db.session(cfg) as conn:
            rel = pipeline.save_reference(conn, cfg, data)
        return {"reference": f"/media/{rel}"}

    @app.post("/api/reference/refresh")
    async def api_reference_refresh(image: UploadFile = File(...)):
        """The counter page sends a fresh empty-mat photo when the mat has sat empty for a while, so
        slow light changes (the sun moving, a lamp) don't turn into false detections. Refused if
        anything item-sized differs from the current empty-mat photo: it may be something on the mat."""
        data = await image.read()

        def run() -> dict:
            frame = segment.decode(data)
            with db.session(cfg) as conn:
                reference = pipeline.load_reference(conn, cfg)
                roi = pipeline.load_roi(conn)
                if reference is not None:
                    try:
                        blobs = segment.segment(frame, reference, cfg.recognition.segment, roi, check_scene=True)
                    except segment.SceneChanged:
                        return {"refreshed": False, "why": "the whole view changed: calibrate by hand"}
                    if any(b.area_frac >= 0.02 for b in blobs):
                        return {"refreshed": False, "why": "something may be on the mat"}
                rel = pipeline.save_reference(conn, cfg, data)
            return {"refreshed": True, "reference": f"/media/{rel}"}
        return await run_in_threadpool(run)

    def _capture(data: bytes, txn_id: int | None, source: str, use_reference: bool) -> dict:
        started = time.perf_counter()
        frame = segment.decode(data)
        rec = pipeline.get_recognizer(cfg)
        with db.session(cfg) as conn:
            if txn_id is None:
                txn_id = transactions.open_transaction(conn, source)
            elif conn.execute("SELECT status FROM transactions WHERE id = ?", (txn_id,)).fetchone()["status"] != "open":
                txn_id = transactions.open_transaction(conn, source)
            name = f"t{txn_id}_{uuid.uuid4().hex[:6]}"
            detections = rec.recognize(conn, frame, name, use_reference=use_reference)
            reading = rec.read_tray(conn, frame) if use_reference else None
            photo = pipeline.save_frame(cfg, data, name)
            kept = [d.record for d in detections if not d.ignored]
            frame_no = transactions.add_frame(conn, txn_id, photo, kept, cfg.media_dir)
            # Nothing the camera sets aside is lost: it goes in the camera log, where it can still be a sale.
            transactions.add_ignored(conn, txn_id, [(d.ignored, d.record) for d in detections if d.ignored],
                                     cfg.media_dir)
            tray_out = _tray_frame(conn, txn_id, frame_no, name, reading, kept) if reading is not None else None
        h, w = frame.shape[:2]
        ms = round((time.perf_counter() - started) * 1000)
        log.info("capture t%s: %d item(s), %d set aside, %d ms", txn_id, len(kept),
                 sum(1 for d in detections if d.ignored), ms)
        return {
            "txn_id": txn_id, "frame_no": frame_no, "width": w, "height": h, "ms": ms,
            "detections": [{"bbox": list(d.record.bbox), "label": d.label, "tier": d.record.tier,
                            "score": d.record.confidence, "method": d.record.method, "ignored": d.ignored,
                            "pack_id": d.record.pack_id,
                            "crop": f"/media/{d.record.crop_path}" if d.record.crop_path else None}
                           for d in detections],
            "tray": tray_out,
        }

    def _tray_frame(conn, txn_id: int, frame_no: int, name: str, reading, kept) -> dict:
        """Store what lies on the tablet tray; count it for the overlay if the box beside it is known."""
        tc = cfg.recognition.tray
        if reading.areas or reading.oversized:
            rel = f"crops/{name}_tray.jpg"
            (cfg.media_dir / "crops").mkdir(parents=True, exist_ok=True)
            reading.crop.save(cfg.media_dir / rel, quality=92)
            transactions.add_tray(conn, txn_id, frame_no, reading.areas, reading.oversized, rel)
        sources = {pk["product_id"] for pk in (catalog.pack(conn, d.pack_id) for d in kept if d.pack_id)
                   if pk is not None and loose.is_loose(conn, pk["product_id"])}
        unit = loose.unit_area(conn, next(iter(sources)), tc.min_unit_samples) if len(sources) == 1 else None
        count, sure = loose.count_tablets(reading.areas, unit, tc.unit_tolerance)
        return {"bbox": list(reading.bbox), "pieces": len(reading.areas), "count": count,
                "sure": sure and len(sources) == 1 and not reading.oversized, "oversized": reading.oversized,
                "with_source": len(sources) == 1}

    def _close(txn_id: int) -> dict:
        with db.session(cfg) as conn:
            summary = transactions.close_transaction(conn, txn_id, cfg.media_dir,
                                                     cfg.verification.delete_photos_after_verify, cfg.recognition.tray)
            transactions.forget_ignored(conn, cfg.media_dir, cfg.recognition.ignored_keep_days)
            rows = conn.execute(
                "SELECT ti.*, p.name_en, p.name_km, pk.units, pk.label FROM transaction_items ti "
                "LEFT JOIN product_packs pk ON pk.id = ti.pack_id LEFT JOIN products p ON p.id = pk.product_id "
                "WHERE ti.txn_id = ?", (txn_id,)).fetchall()
        summary["lines"] = [{"id": r["id"], "label": catalog.pack_label(r, "en") if r["pack_id"] else "?",
                             "tier": r["tier"], "status": r["status"], "score": r["confidence"],
                             "count": r["count"], "method": r["method"],
                             "photo": f"/media/{r['crop_path']}" if r["crop_path"] else None} for r in rows]
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
        summary = await run_in_threadpool(_close, txn_id)
        if summary.get("pending"):
            read_labels_later(txn_id)
        return summary

    @app.post("/api/sale-photo")
    async def api_sale_photo(image: UploadFile = File(...), use_reference: bool = Form(False)):
        """Demo backup: one uploaded photo is one whole sale."""
        data = await image.read()
        try:
            captured = await run_in_threadpool(_capture, data, None, "upload", use_reference)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        summary = await run_in_threadpool(_close, captured["txn_id"])
        if summary.get("pending"):
            read_labels_later(captured["txn_id"])
        return {**captured, "summary": summary}

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
            boxes = loose.box_sizes(conn)
        return page(request, "catalog.html", products=products, suppliers=suppliers, ignore=ignore, boxes=boxes,
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
                         ignore_label: str | None = Form(None), use_reference: bool = Form(True),
                         burst: bool = Form(False)):
        if (pack_id is None) == (ignore_label is None):
            raise HTTPException(400, "give exactly one of pack_id or ignore_label")
        data = await image.read()

        def run() -> dict:
            frame = segment.decode(data)
            rec = pipeline.get_recognizer(cfg)
            own = False
            with db.session(cfg) as conn:
                try:
                    try:
                        gid, bbox = rec.enroll(conn, frame, pack_id, ignore_label, use_reference, burst=burst)
                    except segment.SceneChanged:
                        # Not the counter camera's view (another phone, zoomed in, held in the hand):
                        # the item is found against its own background instead of the empty mat. It
                        # must stand out from it: a changed but empty counter is never learned.
                        own = True
                        gid, bbox = rec.enroll(conn, frame, pack_id, ignore_label, False, burst=burst,
                                               whole_if_empty=False)
                except pipeline.Skipped as skip:
                    return {"skipped": str(skip), "own_background": own}
                row = conn.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (gid,)).fetchone()
                n = conn.execute("SELECT COUNT(*) FROM gallery_images WHERE " +
                                 ("pack_id = ?" if pack_id else "ignore_label = ?"),
                                 (pack_id or ignore_label,)).fetchone()[0]
            return {"id": gid, "bbox": list(bbox), "crop": f"/media/{row['crop_path']}", "photos": n,
                    "own_background": own}
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
            box = loose.box_sizes(conn).get(product_id)
            days = stock.product_days(conn, product_id, 28)
            learned = conn.execute("SELECT samples_json FROM tray_units WHERE product_id = ?", (product_id,)).fetchone()
            tray_marked = pipeline.load_roi(conn, pipeline.TRAY_KEY) is not None
        loose_info = None
        if product["sold_loose"]:
            loose_info = {
                "box": box,
                "breakdown": loose.breakdown(level, *box, product["unit"]) if box and level and level >= box[1] else None,
                "samples": len(json.loads(learned["samples_json"])) if learned else 0,
                "needed": cfg.recognition.tray.min_unit_samples, "tray_marked": tray_marked,
            }
        sold = [d["sold"] for d in days]
        sales = {
            "chart": charts_mod.daily_chart(days, f"{product['name_en']}: sold per day, last 4 weeks", product["unit"]),
            "total": sum(sold), "per_day": round(sum(sold) / len(sold), 1), "empty_days": sum(d["empty"] for d in days),
            "this_week": sum(sold[-7:]), "last_week": sum(sold[-14:-7]),
        }
        return page(request, "product.html", p=product, packs=packs, gallery=photos, aliases=aliases,
                    suppliers=suppliers, level=level, moves=moves, min_photos=MIN_PHOTOS, loose=loose_info,
                    sales=sales)

    @app.post("/catalog/{product_id}/loose")
    def make_loose(request: Request, product_id: int, per_box: str = Form(""), piece: str = Form("tablet")):
        """Start selling a medicine by the tablet: stock so far is converted from boxes to tablets."""
        n = _int(per_box)
        piece = piece.strip().lower() or "tablet"
        if not n or n < 2:
            return back(request, "Enter how many tablets one box holds")
        with db.session(cfg) as conn:
            _product(conn, product_id)
            try:
                loose.set_sold_loose(conn, product_id, n, piece)
            except ValueError as exc:
                return back(request, str(exc))
        return back(request, f"Now counted in {piece}s (one box = {n}). Loose ones are counted on the tray.")

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
        if _wants_json(request):
            return {"deleted": n}
        return back(request, f"Deleted {n} photo{'s' if n != 1 else ''}")

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
            rows = invoices.recent(conn, 30)
            products = catalog.products(conn)
            suppliers = catalog.suppliers(conn)
        waiting = [r for r in rows if r["status"] in ("extracted", "failed", "queued")]
        done = [r for r in rows if r["status"] not in ("extracted", "failed", "queued")]
        return page(request, "invoices.html", waiting=waiting, done=done, products=products, suppliers=suppliers,
                    key_present=cfg.anthropic_key_present)

    @app.post("/invoices/upload")
    async def upload_invoice(request: Request, image: UploadFile = File(...)):
        rel = f"invoices/{uuid.uuid4().hex[:12]}.jpg"
        (cfg.media_dir / rel).write_bytes(await image.read())
        with db.session(cfg) as conn:
            invoice_id = invoices.create_from_photo(conn, rel)
        status = await run_in_threadpool(invoices.process, cfg, invoice_id)
        msg = {"extracted": "Invoice read. Check each line, then add it to stock.",
               "queued": "No internet right now. It will be read when the connection is back.",
               "failed": "Could not read this photo. Type the lines in instead."}[status]
        return RedirectResponse(f"/invoices/{invoice_id}?msg=" + quote(msg), status_code=303)

    @app.post("/invoices/manual")
    async def manual_invoice(request: Request):
        form = await request.form()
        entries = []
        for i in range(20):
            pid, qty = _int(form.get(f"product_{i}")), _float(form.get(f"qty_{i}"))
            if pid and qty and qty > 0:
                entries.append({"product_id": pid, "qty": qty, "unit": form.get(f"unit_{i}") or "case",
                                "unit_cost": _float(form.get(f"cost_{i}")),
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
            suggestions = {l["id"]: catalog.likely_products(conn, l["text_as_written"])
                           for l in lines if l["product_id"] is None}
            products = catalog.products(conn)
            suppliers = catalog.suppliers(conn)
            preview = render.invoice_message(conn, invoice_id, "km")
        names = {pr["id"]: pr["name_en"] for pr in products}
        open_lines = [l for l in lines if l["product_id"] is None and not l["skipped"]]
        addable = [l for l in open_lines if not invoices.needs_case_size(l)]
        total = sum(l["units_total"] or 0 for l in lines if not l["skipped"] and l["product_id"])
        return page(request, "invoice.html", inv=inv, lines=lines, products=products, suppliers=suppliers,
                    suggestions=suggestions, preview=preview, extracted=invoices.extracted_json(inv),
                    names=names, open_lines=open_lines, total=total, addable=addable,
                    needs_size={l["id"] for l in open_lines if invoices.needs_case_size(l)}, weekdays=WEEKDAYS)

    @app.post("/invoices/{invoice_id}/lines/{line_id}")
    async def invoice_line(request: Request, invoice_id: int, line_id: int, product_id: str = Form("")):
        with db.session(cfg) as conn:
            if product_id == "skip":
                invoices.skip_line(conn, line_id)
            elif product_id:
                invoices.set_line_product(conn, line_id, int(product_id))
        await _refresh_invoice(invoice_id)
        return back(request)

    @app.post("/invoices/{invoice_id}/lines/{line_id}/new")
    async def invoice_line_new(request: Request, invoice_id: int, line_id: int, name: str = Form(""),
                               units_per_case: int | None = Form(None)):
        """An invoice line that isn't a product yet becomes one: name, supplier, case size and price from the invoice."""
        try:
            with db.session(cfg) as conn:
                invoices.new_product_for_line(conn, line_id, name or None, units_per_case)
        except (KeyError, ValueError) as exc:
            return back(request, str(exc).strip("'"))
        await _refresh_invoice(invoice_id)
        return back(request)

    @app.post("/invoices/{invoice_id}/new-products")
    async def invoice_new_products(request: Request, invoice_id: int):
        with db.session(cfg) as conn:
            made, left = invoices.new_products_for_all(conn, invoice_id)
        await _refresh_invoice(invoice_id)
        msg = f"Added {made} new product{'s' if made != 1 else ''}."
        if left:
            msg += f" {left} line{'s' if left != 1 else ''} still need{'s' if left == 1 else ''} the number in one case."
        return back(request, msg)

    @app.post("/invoices/{invoice_id}/new-supplier")
    async def invoice_new_supplier(request: Request, invoice_id: int, name: str = Form(""),
                                   visit_weekday: int = Form(...)):
        try:
            with db.session(cfg) as conn:
                invoices.add_supplier_from_invoice(conn, invoice_id, name, visit_weekday)
        except ValueError as exc:
            return back(request, str(exc))
        await _refresh_invoice(invoice_id)
        return back(request, f"Added {name.strip()} to your suppliers.")

    @app.post("/invoices/{invoice_id}/products-only")
    async def invoice_products_only(request: Request, invoice_id: int):
        """An old invoice, read to set the shop up: its products and prices, no stock."""
        try:
            with db.session(cfg) as conn:
                used = invoices.products_only(conn, invoice_id)
        except ValueError as exc:
            return back(request, str(exc))
        await _refresh_invoice(invoice_id)
        return back(request, f"Kept the products from {used} line{'s' if used != 1 else ''}. No stock was added.")

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

    # --- pilot report --------------------------------------------------------------------------------

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request):
        with db.session(cfg) as conn:
            rep = report.build(conn, cfg)
        charts = []
        if rep is not None:
            labels = [w.start.strftime("%d %b").lstrip("0") for w in rep.weeks]
            phases = [w.phase for w in rep.weeks]
            for key, title, better, kind in report.SERIES:
                values = [getattr(w, key) for w in rep.weeks]
                tips = [f"Week of {w.start.strftime('%d %b')}{' (so far)' if w.partial else ''}"
                        f"{' · watching only' if w.phase == 'baseline' else ''}: {charts_mod.fmt(v, kind)}"
                        for w, v in zip(rep.weeks, values, strict=True)]
                charts.append({"key": key, "title": title, "better": better, "kind": kind,
                               "svg": charts_mod.column_chart(values, labels, phases, kind, tips, title,
                                                              [w.partial for w in rep.weeks])})
        return page(request, "report.html", rep=rep, charts=charts, fmt=charts_mod.fmt)

    # --- orders -----------------------------------------------------------------------------------

    @app.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request, lang: str = "en"):
        with db.session(cfg) as conn:
            forecast.settle_follow_rate(conn)
            plans = forecast.build_plans(conn, cfg.forecast)
            plans.sort(key=lambda p: (p.visit_date, p.name))
            follow = metrics.follow_rate(conn)
            simulated = stock.has_simulated(conn)
        previews = {p.supplier_id: render.order_message(p, lang, simulated) for p in plans}
        reasons = {(p.supplier_id, l.product_id): render.line_reason(l, p, "en") for p in plans for l in p.lines}
        return page(request, "orders.html", plans=plans, previews=previews, reasons=reasons, follow=follow,
                    lang=lang, today=clock.today(), sleeping=sum(p.sleeping_value for p in plans),
                    timedelta=timedelta)

    return app
