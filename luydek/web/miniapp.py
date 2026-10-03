"""The Telegram Mini App: checking items by swiping, inside Telegram.

The owner opens it from the bot (the digest's "Check N items" button, or the menu button). It
shows the waiting items as a stack of cards: swipe right when the camera is right, left to
correct it (another product, a count, not a sale, several items, a new product). A second tab
lists what was recorded today, to fix anything recorded wrongly; a third shows today's sales
and the stock left of every product (`/tg?tab=stock` opens it directly).

Telegram loads the page on the phone, so it needs an https address (`telegram.miniapp_url`,
e.g. from `tailscale serve`). Every API call carries Telegram's signed launch data
(`initData`), checked here against the bot token: only phones registered with the shop PIN
get in. Photos are served through links signed for a few hours, since an <img> can't send it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from datetime import timedelta

from .. import clock, db
from ..config import Config
from ..i18n import t
from ..recognition import pipeline
from ..services import catalog, learn, metrics, summary, transactions

MAX_AGE = 24 * 3600          # launch data older than this is refused
IMAGE_TTL = 6 * 3600          # signed photo links last this long


# --- Telegram's signed launch data ---------------------------------------------------------------

def check_init_data(init_data: str, bot_token: str, now: float | None = None, max_age: int = MAX_AGE) -> dict | None:
    """The Telegram user in `initData`, if Telegram signed it for this bot and it is recent; else None.

    https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
    """
    if not init_data or not bot_token:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not received or not hmac.compare_digest(expected, received):
        return None
    try:
        if (now or time.time()) - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs.get("user", "null"))
    except (ValueError, TypeError):
        return None


def sign_init_data(fields: dict, bot_token: str) -> str:
    """Build launch data the way Telegram does (for tests and local trials)."""
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    return urlencode({**fields, "hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


# --- signed photo links ----------------------------------------------------------------------------

def _image_key(bot_token: str) -> bytes:
    return hashlib.sha256(("luydek-images:" + bot_token).encode()).digest()


def image_url(kind: str, ref: int, bot_token: str, now: float | None = None) -> str:
    exp = int((now or time.time()) + IMAGE_TTL)
    sig = hmac.new(_image_key(bot_token), f"{kind}:{ref}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"/api/tg/img/{kind}/{ref}?e={exp}&s={sig}"


def image_ok(kind: str, ref: int, exp: int, sig: str, bot_token: str, now: float | None = None) -> bool:
    if exp < (now or time.time()):
        return False
    good = hmac.new(_image_key(bot_token), f"{kind}:{ref}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(good, sig)


# --- the routes ------------------------------------------------------------------------------------

def register(app: FastAPI, cfg: Config, templates, picker_items, refresh_item) -> None:
    """Add the Mini App page and its API. `picker_items(conn)` and `refresh_item(item_id)` come from
    the server (the product list, and re-drawing an item's Telegram messages after an answer)."""

    def user_of(request: Request, form_value: str | None = None):
        init = request.headers.get("X-Telegram-Init-Data") or form_value or ""
        tg_user = check_init_data(init, cfg.bot_token or "")
        if tg_user is None:
            raise HTTPException(401, "Open this from the shop's Telegram bot.")
        with db.session(cfg) as conn:
            row = conn.execute("SELECT * FROM bot_users WHERE chat_id = ?", (tg_user["id"],)).fetchone()
        if row is None:
            raise HTTPException(403, "Send /start with the shop PIN to the bot first.")
        return row

    def card(conn, it, lang: str, token: str) -> dict:
        cands = []
        for pack_id, score in transactions.candidates(it)[:4]:
            pk = catalog.pack(conn, pack_id)
            if pk is not None and pack_id != it["pack_id"]:
                cands.append({"pack": pack_id, "label": catalog.pack_label(pk, lang), "score": round(score, 2)})
        guess = catalog.pack(conn, it["pack_id"]) if it["pack_id"] else None
        return {
            "id": it["id"], "status": it["status"], "time": it["started_at"][11:16], "date": it["started_at"][:10],
            "count": it["count"], "tier": it["tier"], "method": it["method"], "hint": it["hint"],
            "confidence": round(it["confidence"], 2),
            "photo": image_url("item", it["id"], token) if it["crop_path"] else None,
            "guess": {"pack": it["pack_id"], "label": catalog.pack_label(guess, lang)} if guess else None,
            "name_guess": it["name_guess"], "candidates": cands, "tray": it["method"] == "tray",
        }

    @app.get("/tg", response_class=HTMLResponse)
    def miniapp_page(request: Request):
        return templates.TemplateResponse(request, "tg.html", {"cfg": cfg})

    @app.get("/api/tg/state")
    def miniapp_state(request: Request):
        """Everything the app shows: the cards to check, today's recorded sales, the product list."""
        user = user_of(request)
        lang, token = user["lang"] or cfg.shop.default_lang, cfg.bot_token or ""
        day = clock.today()
        with db.session(cfg) as conn:
            pending = [card(conn, it, lang, token) for it in transactions.pending_items(conn)]
            today = transactions.day_log(conn, f"{day.isoformat()} 00:00:00",
                                         f"{(day + timedelta(days=1)).isoformat()} 00:00:00")
            recorded = [card(conn, it, lang, token) for s in today for it in s["items"]
                        if it["status"] in transactions.FIXABLE][:60]
            products = []
            by_product, by_pack = catalog.photo_ids(conn)
            for i in picker_items(conn):
                gid = by_pack.get(i["pack"]) or by_product.get(i["product"])
                products.append({"pack": i["pack"], "label": i["label"], "km": i.get("km"), "find": i["find"],
                                 "base": i["base"], "img": image_url("thumb", gid, token) if gid else None})
        img = {p["pack"]: p["img"] for p in products}
        for c in pending + recorded:
            for x in [c["guess"], *c["candidates"]]:
                if x:
                    x["img"] = img.get(x["pack"])
        return {"lang": lang, "name": user["name"], "pending": pending, "recorded": recorded, "products": products,
                "shop": cfg.shop.name}

    @app.get("/api/tg/stock")
    def miniapp_stock(request: Request):
        """The Stock tab: today's sales in numbers, and every product with what is left."""
        user = user_of(request)
        lang, token = user["lang"] or cfg.shop.default_lang, cfg.bot_token or ""
        with db.session(cfg) as conn:
            s = summary.day_summary(conn, None, cfg.forecast.expiry_warning_days)
            rows = summary.stock_left(conn, cfg.forecast.expiry_warning_days)
            by_product, _ = catalog.photo_ids(conn)
        products = [{
            "id": r["id"], "name": catalog.display_name(r, lang), "find": catalog.normalize(f"{r['name_en']} {r['name_km']}"),
            "left": r["on_hand"], "breakdown": r["breakdown"] if lang == "en" else None, "status": r["status"],
            "tracked": bool(r["tracked"]), "negative": r["negative"], "critical": bool(r["is_critical"]),
            "sold_today": r["sold_today"], "sold_7d": r["sold_7d"], "sold_28d": r["sold_28d"],
            "uncounted": r.get("sold_uncounted", 0), "days": r["days_cover"], "expiry": r["expiry"],
            "value": r["value"], "pending": r["pending_units"], "supplier": r["supplier_name"],
            "img": image_url("thumb", by_product[r["id"]], token) if r["id"] in by_product else None,
        } for r in rows]
        return {"lang": lang, "date": s["date"].isoformat(), "txns": s["txns"], "units": s["units"],
                "pending": s["pending"], "received": s["received_units"], "value": s["value"],
                "watch": len(s["watch"]), "simulated": s["simulated"], "products": products}

    @app.post("/api/tg/items/{item_id}")
    async def miniapp_answer(request: Request, item_id: int, action: str = Form(...), pack_id: int | None = Form(None),
                             count: int | None = Form(None), name: str = Form(""), picks: str = Form(""),
                             init_data: str | None = Form(None)):
        """One answer from a card: yes / pick / remove / unknown (not now) / new / several / fix-*."""
        user = user_of(request, init_data)
        by = user["name"] or str(user["chat_id"])

        def run() -> dict:
            with db.session(cfg) as conn:
                metrics.log_tap(conn, user["chat_id"], f"app:{action}")
                opts = {"learn": pipeline.learner(cfg), "media_dir": cfg.media_dir,
                        "delete_photo": cfg.verification.delete_photos_after_verify}
                row = transactions.item(conn, item_id)
                if row is None:
                    raise KeyError(item_id)
                if action.startswith("fix-"):
                    return transactions.fix(conn, item_id, action[4:], pack_id=pack_id, count=count, by=by,
                                            learn=opts["learn"])
                if count is not None and row["status"] == "pending" and count != row["count"]:
                    transactions.resolve(conn, item_id, "qty", delta=count - row["count"], by=by, **opts)
                if action == "new":
                    result = learn.add_from_item(conn, item_id, name, by=by, **opts)
                    regroup = pipeline.regrouper(cfg)
                    result["regrouped"] = regroup(conn, result["pack_id"], skip=item_id) if regroup else []
                    return result
                if action == "several":
                    pairs = [(int(p), int(n)) for p, n in (x.split(":") for x in picks.split(",") if ":" in x)]
                    return {"status": "split", "items": transactions.several(conn, item_id, pairs, by=by,
                                                                               media_dir=cfg.media_dir)}
                if row["status"] == "unknown" and action == "pick":
                    return transactions.name_item(conn, item_id, pack_id, by, opts["learn"], cfg.media_dir)
                return transactions.resolve(conn, item_id, action, pack_id=pack_id, by=by, **opts)

        try:
            result = await run_in_threadpool(run)
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc).strip("'")}, status_code=400)
        for changed in [item_id, *result.get("regrouped", [])]:
            await refresh_item(changed)
        return {"item": item_id, **{k: v for k, v in result.items() if k in ("status", "count", "already", "name",
                                                                              "regrouped", "items")}}

    @app.post("/api/tg/confirm-all")
    async def miniapp_confirm_all(request: Request, ids: str = Form(""), init_data: str | None = Form(None)):
        user = user_of(request, init_data)
        wanted = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        with db.session(cfg) as conn:
            metrics.log_tap(conn, user["chat_id"], "app:all")
            done = transactions.confirm_many(conn, wanted, by=user["name"] or str(user["chat_id"]),
                                             learn=pipeline.learner(cfg), media_dir=cfg.media_dir,
                                             delete_photo=cfg.verification.delete_photos_after_verify)
        for item_id in wanted:
            await refresh_item(item_id)
        return {"confirmed": done}

    @app.get("/api/tg/img/{kind}/{ref}")
    def miniapp_image(kind: str, ref: int, e: int = 0, s: str = ""):
        if kind not in ("item", "thumb") or not image_ok(kind, ref, e, s, cfg.bot_token or ""):
            raise HTTPException(403, "This photo link has expired: reopen the app.")
        with db.session(cfg) as conn:
            if kind == "item":
                row = conn.execute("SELECT crop_path FROM transaction_items WHERE id = ?", (ref,)).fetchone()
                path = row["crop_path"] if row else None
            else:
                row = conn.execute("SELECT crop_path FROM gallery_images WHERE id = ?", (ref,)).fetchone()
                path = row["crop_path"] if row else None
        if not path or not (cfg.media_dir / path).exists():
            raise HTTPException(404)
        return FileResponse(cfg.media_dir / path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    host = urlparse(cfg.telegram.miniapp_url or "").hostname
    if host and cfg.telegram.miniapp_host_only:
        @app.middleware("http")
        async def miniapp_only(request: Request, call_next):
            """On the Mini App's public address, only the Mini App is served: the dashboard has no
            login yet, so it stays on this computer (or the private Tailscale address)."""
            if request.url.hostname == host and not (
                    request.url.path in ("/tg", "/favicon.ico") or request.url.path.startswith(("/api/tg/", "/static/"))):
                return JSONResponse({"error": "not here"}, status_code=404)
            return await call_next(request)


def menu_label(lang: str) -> str:
    return t(lang, "app_open")
