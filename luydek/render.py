"""Owner-facing message text (Telegram and the dashboard's message previews)."""

from __future__ import annotations

import sqlite3
from html import escape

from .i18n import t
from .services.forecast import Line, SupplierPlan


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
                qty = " — " + t(lang, "orders_cases", n=l.cases)
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
