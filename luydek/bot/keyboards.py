"""Inline keyboards. Callback data stays under Telegram's 64-byte limit:

    v:<item>:y | p:<pack> | q:<±1> | x | u | o:<page>    verify one item
    v:<item>:n | ng                                     a new product: type its name | add the name read
    d:<digest>                                          confirm all in a digest
    r                                                   resend everything pending
    a:r:<reason> | a:l:<reason>:<page> | a:p:<reason>:<product> | a:q:<reason>:<product>:<n> | a:c
    i:<invoice>:ok | no | s:<supplier> | b              invoice
    il:<line>:p:<product> | il:<line>:k | il:<line>:n   invoice line (n: add it as a new product)
    b:<invoice>:l:<page> | b:<invoice>:p:<product> | b:<invoice>:q:<product>:<n>
    s:m | s:l:<page> | s:p:<product> | s:a              stock left: new picker | a page | one product | every item
"""

from __future__ import annotations

import sqlite3

from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from ..i18n import t
from ..services import catalog
from ..services.stock import ADJUST_REASONS

PAGE = 8
QTY_CHOICES = (1, 2, 3, 4, 5, 6, 10, 12, 24)
BONUS_CHOICES = (1, 2, 3, 6, 12, 24)


def short(text: str, n: int = 30) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def main_menu(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(t(lang, "menu_today")), KeyboardButton(t(lang, "menu_stock"))],
         [KeyboardButton(t(lang, "menu_invoice")), KeyboardButton(t(lang, "menu_adjust"))],
         [KeyboardButton(t(lang, "menu_orders")), KeyboardButton(t(lang, "menu_week"))]],
        resize_keyboard=True,
        is_persistent=True,
    )


def item_keyboard(conn: sqlite3.Connection, item: sqlite3.Row, candidates: list[tuple[int, float]],
                  lang: str) -> InlineKeyboardMarkup:
    iid = item["id"]
    rows = []
    if item["pack_id"] is not None:
        rows.append([Btn(t(lang, "yes"), callback_data=f"v:{iid}:y")])
    alts = [p for p, _ in candidates if p != item["pack_id"]][:2]
    alt_row = []
    for pack_id in alts:
        pk = catalog.pack(conn, pack_id)
        if pk is not None:
            alt_row.append(Btn(short(catalog.pack_label(pk, lang)), callback_data=f"v:{iid}:p:{pack_id}"))
    if alt_row:
        rows.append(alt_row)
    rows.append([
        Btn("➖", callback_data=f"v:{iid}:q:-1"),
        Btn(f"×{item['count']}", callback_data="noop"),
        Btn("➕", callback_data=f"v:{iid}:q:1"),
    ])
    rows.append([Btn(t(lang, "other"), callback_data=f"v:{iid}:o:0"),
                 Btn(t(lang, "not_item"), callback_data=f"v:{iid}:x")])
    if item["method"] != "tray":
        new_row = [Btn(t(lang, "new_product"), callback_data=f"v:{iid}:n")]
        if item["name_guess"]:
            new_row.insert(0, Btn(t(lang, "add_named", name=short(item["name_guess"], 24)), callback_data=f"v:{iid}:ng"))
        rows.append(new_row)
    return InlineKeyboardMarkup(rows)


def grid_keyboard(item_id: int, packs: list[sqlite3.Row], first_number: int, has_more: bool, page: int,
                  lang: str) -> InlineKeyboardMarkup:
    buttons = [Btn(f"{first_number + i}. {short(catalog.pack_label(pk, lang), 22)}",
                   callback_data=f"v:{item_id}:p:{pk['pack_id']}") for i, pk in enumerate(packs)]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    nav = []
    if has_more:
        nav.append(Btn(t(lang, "more"), callback_data=f"v:{item_id}:o:{page + 1}"))
    nav.append(Btn(t(lang, "new_product"), callback_data=f"v:{item_id}:n"))
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def open_app(url: str, lang: str, n: int, digest_id: int | None = None) -> InlineKeyboardMarkup:
    """The Mini App's button: check the items by swiping. "Confirm all" stays one tap away."""
    rows = [[Btn(t(lang, "app_open", n=n) if n else t(lang, "app_open_none"),
                 web_app=WebAppInfo(url.rstrip("/") + "/tg"))]]
    if digest_id is not None and n:
        rows.append([Btn(t(lang, "confirm_all"), callback_data=f"d:{digest_id}")])
    return InlineKeyboardMarkup(rows)


def confirm_all(digest_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t(lang, "confirm_all"), callback_data=f"d:{digest_id}")]])


def show_again(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t(lang, "show_again"), callback_data="r")]])


# --- stock left -------------------------------------------------------------------

def app_url(base: str, tab: str | None = None) -> str:
    return base.rstrip("/") + "/tg" + (f"?tab={tab}" if tab else "")


def summary_keyboard(lang: str, miniapp_url: str | None) -> InlineKeyboardMarkup:
    """Under the day's summary: stock left per product, in the chat or in the Mini App."""
    rows = [[Btn(t(lang, "menu_stock"), callback_data="s:m")]]
    if miniapp_url:
        rows.append([Btn(t(lang, "sum_open_app"), web_app=WebAppInfo(app_url(miniapp_url, "stock")))])
    return InlineKeyboardMarkup(rows)


def stock_picker(products: list, labels: dict[int, str], page: int, lang: str,
                 paged: bool = True) -> InlineKeyboardMarkup:
    """"Every item" first, then one button per product showing what is left ("Coca-Cola 330ml · 36")."""
    rows = [[Btn(t(lang, "stock_every"), callback_data="s:a")]]
    chunk = products[page * PAGE:(page + 1) * PAGE] if paged else products[:PAGE]
    buttons = [Btn(f"{short(catalog.display_name(p, lang), 24)} · {labels.get(p['id'], '?')}",
                   callback_data=f"s:p:{p['id']}") for p in chunk]
    rows += [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    arrows = []
    if paged and page > 0:
        arrows.append(Btn(t(lang, "prev"), callback_data=f"s:l:{page - 1}"))
    if paged and (page + 1) * PAGE < len(products):
        arrows.append(Btn(t(lang, "next"), callback_data=f"s:l:{page + 1}"))
    if arrows:
        rows.append(arrows)
    return InlineKeyboardMarkup(rows)


def product_stock_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[Btn(t(lang, "stock_back"), callback_data="s:l:0"),
                                  Btn(t(lang, "stock_every"), callback_data="s:a")]])


# --- adjustments ------------------------------------------------------------------

def adjust_reasons(lang: str) -> InlineKeyboardMarkup:
    buttons = [Btn(t(lang, f"reason_{r}"), callback_data=f"a:r:{r}") for r in ADJUST_REASONS]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([Btn(t(lang, "cancel"), callback_data="a:c")])
    return InlineKeyboardMarkup(rows)


def product_page(products: list[sqlite3.Row], page: int, lang: str, pick: str, nav: str) -> InlineKeyboardMarkup:
    """Paginated product picker. `pick` and `nav` are callback prefixes for choosing and paging."""
    chunk = products[page * PAGE:(page + 1) * PAGE]
    buttons = [Btn(short(catalog.display_name(p, lang), 28), callback_data=f"{pick}:{p['id']}") for p in chunk]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    arrows = []
    if page > 0:
        arrows.append(Btn(t(lang, "prev"), callback_data=f"{nav}:{page - 1}"))
    if (page + 1) * PAGE < len(products):
        arrows.append(Btn(t(lang, "next"), callback_data=f"{nav}:{page + 1}"))
    if arrows:
        rows.append(arrows)
    rows.append([Btn(t(lang, "cancel"), callback_data="a:c")])
    return InlineKeyboardMarkup(rows)


def quantities(prefix: str, choices: tuple[int, ...], lang: str) -> InlineKeyboardMarkup:
    buttons = [Btn(str(n), callback_data=f"{prefix}:{n}") for n in choices]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    rows.append([Btn(t(lang, "cancel"), callback_data="a:c")])
    return InlineKeyboardMarkup(rows)


# --- invoices -----------------------------------------------------------------------

def invoice_keyboard(conn: sqlite3.Connection, invoice: sqlite3.Row, lang: str) -> InlineKeyboardMarkup:
    iid = invoice["id"]
    if invoice["status"] == "confirmed":
        return InlineKeyboardMarkup([[Btn(t(lang, "invoice_bonus"), callback_data=f"i:{iid}:b")]])
    rows = []
    if invoice["supplier_id"] is None:
        sups = [Btn(f"{s['emoji']} {short(s['name'], 20)}", callback_data=f"i:{iid}:s:{s['id']}")
                for s in catalog.suppliers(conn)]
        rows += [sups[i:i + 2] for i in range(0, len(sups), 2)]
    rows.append([Btn(t(lang, "invoice_confirm"), callback_data=f"i:{iid}:ok")])
    rows.append([Btn(t(lang, "invoice_bonus"), callback_data=f"i:{iid}:b"),
                 Btn(t(lang, "invoice_reject"), callback_data=f"i:{iid}:no")])
    return InlineKeyboardMarkup(rows)


def line_keyboard(conn: sqlite3.Connection, line: sqlite3.Row, lang: str) -> InlineKeyboardMarkup:
    options = catalog.likely_products(conn, line["text_as_written"], limit=4)
    buttons = []
    for pid in options:
        p = catalog.product(conn, pid)
        buttons.append(Btn(short(catalog.display_name(p, lang), 28), callback_data=f"il:{line['id']}:p:{pid}"))
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    # Not in the product list yet: add it with the name, case size and price the invoice gives.
    # A case with no size printed is left to the web page, which asks for it.
    if line["unit"] == "piece" or line["units_per_case"]:
        name = line["name_suggested"] or line["text_as_written"]
        rows.insert(0, [Btn(t(lang, "add_named", name=short(name, 26)), callback_data=f"il:{line['id']}:n")])
    rows.append([Btn(t(lang, "invoice_skip_line"), callback_data=f"il:{line['id']}:k")])
    return InlineKeyboardMarkup(rows)
