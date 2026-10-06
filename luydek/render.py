"""Owner-facing message text (Telegram and the dashboard's message previews)."""

from __future__ import annotations

import sqlite3
from html import escape

from . import clock
from .i18n import t
from .services.forecast import Line, SupplierPlan
from .services.loose import plural
from .services.stock import STATUS_ORDER
from .services.summary import SOLD_LINES, WATCH_LINES


def line_reason(line: Line, plan: SupplierPlan, lang: str) -> str:
    text = t(
        lang,
        "r_" + line.reason,
        stock=line.stock,
        last_week=line.sold_last_week,
        cycle=plan.cycle_days,
        cover=line.cover_days if line.cover_days is not None else "∞",
        since=line.days_since_sale if line.days_since_sale is not None else "30+",
        sold_4w=line.sold_4w,
        value=f"{line.value:.2f}",
        units=line.units,
    )
    if line.capped_by_expiry and line.action == "order":
        text += t(lang, "r_capped")
    if line.stockout_days and line.action in ("order", "market", "check"):
        text += t(lang, "r_stockout", days=line.stockout_days)
    if line.expiry:
        text += t(lang, "r_expiring", date=line.expiry)
    if line.stock < 0:
        text += t(lang, "r_negative")
    return text


def order_message(plan: SupplierPlan, lang: str, simulated: bool) -> str:
    name = lambda l: escape(l.name_km if lang == "km" else l.name_en)  # noqa: E731
    day = t(lang, f"weekday_{plan.visit_date.weekday()}")
    out = [f"<b>{t(lang, 'orders_title', emoji=plan.emoji, supplier=escape(plan.name), day=day)}</b>"]
    if simulated:
        out.append(f"<i>{t(lang, 'orders_simulated')}</i>")
    if plan.observing:
        out.append(t(lang, "orders_observing"))

    sections = [("order", "orders_head_order"), ("market", "orders_head_market"), ("check", "orders_head_check"),
                ("skip", "orders_head_skip"), ("sleeping", "orders_head_sleeping")]
    for action, head in sections:
        lines = plan.of(action)
        if not lines:
            continue
        out.append(f"\n<b>{t(lang, head)}</b>")
        for l in lines:
            qty = ""
            if action == "order":
                if l.order_unit != "case":
                    qty = " — " + t(lang, "orders_boxes", n=l.cases, label=plural(l.order_unit, l.cases))
                else:
                    qty = " — " + t(lang, "orders_case" if l.cases == 1 else "orders_cases", n=l.cases)
            elif action == "market":
                qty = " — " + t(lang, "orders_units", n=l.units)
            out.append(f"• {name(l)}{qty}\n   <i>{line_reason(l, plan, lang)}</i>")
    if not plan.of("order") and not plan.of("market"):
        out.append("\n" + t(lang, "orders_none"))
    if plan.order_value:
        total = t(lang, "orders_total", value=f"{plan.order_value:.2f}")
        if plan.below_min_order:
            total += t(lang, "orders_min", min=f"{plan.min_order:.0f}")
        out.append("\n" + total)
    return "\n".join(out)


def invoice_message(conn: sqlite3.Connection, invoice_id: int, lang: str) -> str:
    inv = conn.execute(
        "SELECT i.*, s.name AS supplier FROM invoices i LEFT JOIN suppliers s ON s.id = i.supplier_id WHERE i.id = ?",
        (invoice_id,),
    ).fetchone()
    lines = conn.execute(
        """
        SELECT l.*, p.name_en, p.name_km FROM invoice_lines l LEFT JOIN products p ON p.id = l.product_id
        WHERE l.invoice_id = ? ORDER BY l.id
        """,
        (invoice_id,),
    ).fetchall()
    supplier = inv["supplier"] or inv["supplier_name_raw"] or "?"
    out = [f"<b>{t(lang, 'invoice_title', supplier=escape(supplier))}</b>"]
    for i, l in enumerate(lines, 1):
        name = escape(l["name_km"] if lang == "km" else l["name_en"]) if l["product_id"] else None
        written = escape(l["text_as_written"])
        if l["skipped"]:
            text = t(lang, "invoice_line_skipped", text=written)
        elif name is None:
            text = t(lang, "invoice_line_unmatched", text=written)
        elif l["is_free"]:
            text = t(lang, "invoice_line_free", text=written, name=name, units=l["units_total"])
        else:
            text = t(lang, "invoice_line", text=written, name=name, qty=_num(l["qty"]),
                     unit=t(lang, f"unit_{l['unit']}"), units=l["units_total"])
        if l["expiry_date"] and not l["skipped"]:
            text += t(lang, "invoice_expiry", date=escape(l["expiry_date"]))
        out.append(f"{i}. {text}")
    return "\n".join(out)


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


# --- the day's summary and stock left ------------------------------------------------------------

TELEGRAM_LIMIT = 3900  # a Telegram message holds 4096 characters; keep a margin for the HTML tags


def day_label(d, lang: str) -> str:
    return f"{t(lang, f'weekday_{d.weekday()}')} {d.day:02d}/{d.month:02d}"


def left_text(r: dict, lang: str, cover: bool = False) -> str:
    """"36 left", "3 boxes + 47 tablets left", "none left", "not counted yet"."""
    if not r["tracked"]:
        return t(lang, "left_new")
    if r["negative"]:
        return t(lang, "left_negative")
    if r["on_hand"] <= 0:
        return t(lang, "left_out")
    # The box breakdown is written in English words; Khmer gets the plain count.
    text = t(lang, "left", n=r["breakdown"] if r.get("breakdown") and lang == "en" else r["on_hand"])
    if cover and r.get("days_cover") is not None:
        text += t(lang, "left_days", days=max(1, round(r["days_cover"])))
    return text


def _name(r: dict, lang: str) -> str:
    return escape(r["name_km"] if lang == "km" else r["name_en"], quote=False)  # Telegram needs only <, >, &


def day_summary_message(s: dict, lang: str) -> str:
    out = [f"<b>{t(lang, 'sum_title', day=day_label(s['date'], lang))}</b>"]
    if s["simulated"]:
        out.append(f"<i>{t(lang, 'orders_simulated')}</i>")
    out.append(t(lang, "sum_sales", txns=s["txns"], units=s["units"]) if s["units"] or s["txns"] else t(lang, "sum_none"))
    if s["pending"]:
        out.append(t(lang, "sum_pending", n=s["pending"]))
    if s["received_units"]:
        out.append(t(lang, "sum_received", units=s["received_units"], products=s["received_products"]))
    if s["sold"]:
        out.append(f"\n<b>{t(lang, 'sum_head_sold')}</b>")
        for r in s["sold"][:SOLD_LINES]:
            out.append("• " + t(lang, "sum_sold_line", name=_name(r, lang), sold=r["sold"], left=left_text(r, lang)))
        if len(s["sold"]) > SOLD_LINES:
            out.append(t(lang, "sum_more", n=len(s["sold"]) - SOLD_LINES))
    if s["watch"]:
        out.append(f"\n<b>{t(lang, 'sum_head_watch')}</b>")
        for r in s["watch"][:WATCH_LINES]:
            must = t(lang, "must_have") if r["is_critical"] else ""
            out.append(f"• {_name(r, lang)} — {left_text(r, lang, cover=True)}{must}")
        if len(s["watch"]) > WATCH_LINES:
            out.append(t(lang, "sum_more", n=len(s["watch"]) - WATCH_LINES))
    if s["value"]:
        out.append("\n" + t(lang, "sum_value", value=f"{s['value']:.2f}"))
    return "\n".join(out)


def stock_list_messages(rows: list[dict], lang: str, day, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Every product and what is left, grouped out / low / expiring / not counted / in stock,
    split into as many messages as Telegram needs."""
    blocks = [f"<b>{t(lang, 'stock_title', day=day_label(day, lang))}</b>"]
    for status in STATUS_ORDER:
        group = [r for r in rows if r["status"] == status]
        if not group:
            continue
        blocks.append(f"\n<b>{t(lang, 'stock_head_' + status)}</b>")
        for r in group:
            line = f"• {_name(r, lang)} — {left_text(r, lang)}"
            if r.get("sold_today"):
                line += t(lang, "stock_sold_today", n=r["sold_today"])
            if r["is_critical"] and status in ("out", "low"):
                line += t(lang, "must_have")
            blocks.append(line)
    value = sum(r["value"] for r in rows)
    if value:
        blocks.append("\n" + t(lang, "sum_value", value=f"{value:.2f}"))
    messages, current = [], ""
    for block in blocks:
        if current and len(current) + 1 + len(block) > limit:
            messages.append(current)
            current = block.lstrip("\n")
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        messages.append(current)
    return messages


def product_stock_message(r: dict, lang: str) -> str:
    """One product: what is left, how fast it sells, when it runs out, expiry and the agent's next visit."""
    out = [f"<b>{_name(r, lang)}</b>"]
    if not r["tracked"]:
        out.append(t(lang, "p_uncounted", n=r.get("sold_uncounted", 0)))
    else:
        line = f"📦 {left_text(r, lang)}"
        if r["status"] in ("out", "low") and not r["negative"]:  # below zero already says what to do
            line += " · " + t(lang, "p_" + r["status"])
        if r["is_critical"]:
            line += t(lang, "must_have")
        out.append(line)
    out.append(t(lang, "p_sold", today=r.get("sold_today", 0), week=r["sold_7d"], month=r["sold_28d"]))
    if r["tracked"] and r.get("days_cover") is not None:
        out.append(t(lang, "p_cover", days=max(1, round(r["days_cover"]))))
    if r["pending_units"]:
        out.append(t(lang, "p_pending", n=r["pending_units"]))
    if r.get("place"):
        out.append(t(lang, "p_place", place=escape(r["place"], quote=False)))
    dated = [b for b in r.get("batches", []) if b["expiry"]]
    if len(r.get("batches", [])) > 1 and dated:
        out.append(t(lang, "p_batches", batches=" · ".join(
            t(lang, "batch_line", n=b["left"], date=short_date(b["expiry"])) if b["expiry"] else str(b["left"])
            for b in r["batches"][:4])))
    elif r.get("expiry"):
        out.append(t(lang, "p_expiry", date=short_date(r["expiry"])))
    if r.get("supplier_name") and r.get("next_visit"):
        visit = r["next_visit"]
        day = t(lang, "p_agent_today") if visit == clock.today() else t(lang, f"weekday_{visit.weekday()}")
        out.append(t(lang, "p_agent", supplier=escape(r["supplier_name"]), day=day))
    if r["tracked"] and r["value"]:
        out.append(t(lang, "p_value", value=f"{r['value']:.2f}"))
    if r.get("last_sale"):
        out.append(f"<i>{t(lang, 'p_last', when=r['last_sale'][:16])}</i>")
    return "\n".join(out)


# --- stock alerts, the shelf check, putting a delivery away ----------------------------------------

def short_date(iso: str | None) -> str:
    """2026-10-12 -> 12/10/26, the way dates are printed on packs here."""
    if not iso:
        return ""
    y, m, d = iso[:10].split("-")
    return f"{d}/{m}/{y[2:]}"


def stock_alert_message(rows: list[dict], lang: str) -> str:
    """One message for everything that just ran out or ran low."""
    out_names = [_name(r, lang) + (t(lang, "must_have") if r["is_critical"] else "") for r in rows if r["alert"] == "out"]
    low_names = [f"{_name(r, lang)} ({left_text(r, lang, cover=True)})" + (t(lang, "must_have") if r["is_critical"] else "")
                 for r in rows if r["alert"] == "low"]
    parts = []
    if out_names:
        parts.append(t(lang, "alert_out", names=", ".join(out_names)))
    if low_names:
        parts.append(t(lang, "alert_low", names=", ".join(low_names)))
    return "\n".join(parts)


def shelf_check_message(tasks: list, lang: str, within_days: int) -> str:
    """The walk round the shop, place by place."""
    if not tasks:
        return t(lang, "shelf_none", days=within_days)
    n = {a: sum(1 for x in tasks if x.action == a) for a in ("remove", "front", "clear")}
    out = [f"<b>{t(lang, 'shelf_title')}</b>", t(lang, "shelf_counts", **n)]
    place = object()
    for task in tasks:
        if task.place != place:
            place = task.place
            out.append(f"\n<b>📍 {escape(place, quote=False) if place else t(lang, 'shelf_noplace')}</b>")
        out.append(t(lang, f"shelf_{task.action}", name=_name(task.product, lang), n=task.batch.left,
                     date=short_date(task.batch.expiry)))
    return "\n".join(out)


def sources_message(unrecorded: list[dict], flags: list, lang: str) -> str:
    """Stock that didn't come in on any record, and packs with the wrong lot."""
    if not unrecorded and not flags:
        return ""
    out = [f"<b>{t(lang, 'src_title')}</b>"]
    for r in unrecorded[:8]:
        out.append(t(lang, "src_unrecorded", name=_name(r, lang), n=r["units"]))
    for f in flags[:8]:
        supplier = t(lang, "src_from", supplier=escape(f["supplier"], quote=False)) if f["supplier"] else ""
        out.append(t(lang, "src_lot", name=_name(f, lang), found=escape(f["found"], quote=False),
                     expected=escape(f["expected"], quote=False), supplier=supplier))
    out.append(f"<i>{t(lang, 'src_hint')}</i>")
    return "\n".join(out)


def put_away_message(items: list[dict], lang: str) -> str:
    """Where each part of a delivery goes, in front of or behind the stock already there."""
    if not items:
        return ""
    out = [f"<b>{t(lang, 'putaway_title')}</b>"]
    for x in items:
        exp = t(lang, "exp_short", date=short_date(x["expiry"])) if x["expiry"] else ""
        old_exp = t(lang, "exp_short", date=short_date(x["old_expiry"])) if x["old_expiry"] else ""
        place = escape(x["place"], quote=False) if x["place"] else t(lang, "putaway_its_shelf")
        out.append(t(lang, f"putaway_{x['how']}", name=_name(x, lang), n=x["qty"], exp=exp, place=place,
                     old=x["old_left"], old_exp=old_exp))
    return "\n".join(out)
