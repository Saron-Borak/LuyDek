"""The stock in an Excel workbook: download it, count and edit in Excel, upload it to update LuyDek.

Four sheets:

* **Summary**: the stock at a glance (what it is worth, what is out, low, past its date, sleeping),
  by place and by supplier, with when it was downloaded.
* **Stock**: a row per product. The yellow columns (name, supplier, unit, case size, cost, barcode,
  must-have, how long it keeps) can be changed; a row added with no ID is a new product. The rest
  (on hand, worth, status, sales, dates, places) is for reading.
* **Count**: a row per batch on hand, each with its own date and place, and an empty yellow
  "Counted" column. Write what was found; leave it empty for what wasn't counted (it stays as it is);
  0 means none were there. "Where", "Expiry" and "Lot" can be corrected on any row. Stock found that
  LuyDek didn't know about goes in a new row at the bottom.
* **How to use**.

An upload is read first and every change listed (a count "37 -> 35", a product's new cost, a batch
moved) before anything is saved; nothing is saved while a row can't be read. Sales since the sheet
was downloaded are pointed out: a count made earlier would undo them.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .. import clock, db
from ..config import Config
from . import batches, catalog, forecast, shelfsheet, stock, stockin, stockmap
from .xlsx import Workbook

STATUS = {"out": ("Out of stock", "bad"), "low": ("Running low", "warn"), "expiring": ("Expiring soon", "warn"),
          "expired": ("Has expired stock", "bad"), "new": ("Not counted yet", "new"), "ok": ("OK", "ok")}
STOCK_COLUMNS = [  # heading, width, editable
    ("ID", 16, False), ("Product", 34, True), ("Khmer name", 30, True), ("Supplier", 24, True), ("Unit", 9, True),
    ("Case size", 9, True), ("Cost per unit", 11, True), ("Barcode", 16, True), ("Must-have", 10, True),
    ("Keeps for (days)", 10, True), ("On hand", 9, False), ("Worth at cost", 12, False), ("Status", 17, False),
    ("Sold, last 7 days", 10, False), ("Sold, last 4 weeks", 10, False), ("Days of stock left", 10, False),
    ("First date", 13, False), ("Dates on the shelf", 40, False), ("Where", 26, False)]
COUNT_COLUMNS = [
    ("Batch", 8, False), ("ID", 16, False), ("Product", 34, False), ("Where", 22, True), ("Level", 12, False),
    ("Lot", 12, True), ("Expiry", 13, True), ("On the books", 10, False), ("Counted", 10, True)]
NEW_ROWS = 15
YES = {"yes", "y", "true", "1", "x", "✓", "បាទ", "ចាស"}


def _day(d: str | None) -> date | None:
    return date.fromisoformat(d[:10]) if d else None


def nice(d: str | date | None) -> str:
    return clock.day_words(d)


# --- download ---------------------------------------------------------------------------------------

def build(conn: sqlite3.Connection, cfg: Config) -> bytes:
    days = cfg.forecast.expiry_warning_days
    now = clock.now()
    table = stock.stock_table(conn, days)
    lots = batches.lots(conn, days)
    shelves = stockmap.shelves(conn)
    level_of = {(loc, f["product"]): f["level"] for loc, fs in stockmap.facings(conn).items() for f in fs}
    extra = {r["id"]: r for r in conn.execute(
        "SELECT p.id, p.shelf_life_days, (SELECT barcode FROM product_packs k WHERE k.product_id = p.id AND k.barcode IS NOT NULL "
        "ORDER BY k.units LIMIT 1) AS barcode FROM products p")}
    plans = forecast.build_plans(conn, cfg.forecast)
    sleeping = {line.product_id: line.value for p in plans for line in p.of("sleeping")}
    suppliers = catalog.suppliers(conn)
    book = Workbook()

    # Summary
    s = book.sheet("Summary", widths=[30, 14, 14, 16, 16, 18])
    s.row([f"{cfg.shop.name} · stock"], style="title")
    s.row(["Downloaded", now], styles=["label", "datetime"])
    if stock.has_simulated(conn):
        s.row(["Includes simulated sales history"], style="warn")
    s.blank()
    shown = []
    for r in table:
        ls = lots.get(r["id"], [])
        state = "expired" if r["status"] not in ("out", "low") and any(lot["state"] == "expired" for lot in ls) else r["status"]
        shown.append((r, ls, state))
    out = [r["name_en"] for r, _, st in shown if st == "out"]
    low = [r["name_en"] for r, _, st in shown if st == "low"]
    expired = [(r["name_en"], lot["left"]) for r, ls, _ in shown for lot in ls if lot["state"] == "expired"]
    soon = sum(lot["left"] for _, ls, _ in shown for lot in ls if lot["state"] == "soon")
    value = round(sum(r["value"] for r in table), 2)
    units = sum(max(r["on_hand"], 0) for r in table if r["tracked"])
    s.row(["The stock", "", ""], styles=["head", "head", "head"])
    s.row(["On the shelves, at cost", value, f"{units} units of {sum(1 for r in table if r['on_hand'] > 0)} products"],
          styles=["label", "money_big", "muted"])
    s.row(["Out of stock", len(out), ", ".join(out)], styles=["label", "big", "bad" if out else "muted"])
    s.row(["Running low", len(low), ", ".join(low)], styles=["label", "big", "warn" if low else "muted"])
    s.row(["Units past their date", sum(n for _, n in expired), ", ".join(f"{n} {name}" for name, n in expired)],
          styles=["label", "big", "bad" if expired else "muted"])
    s.row(["Units expiring within " + str(days) + " days", soon, ""], styles=["label", "big", "muted"])
    s.row(["Sleeping money (no sale for weeks), at cost", round(sum(sleeping.values()), 2), f"{len(sleeping)} products"],
          styles=["label", "money_big", "muted"])
    s.blank()
    s.row(["By place", "Products", "Units", "Worth at cost", "Past their date", f"Expire within {days} days"], style="head")
    places = [(p["id"], p["name"]) for p in shelves] + [(None, "No place yet")]
    for pid_, name in places:
        here = [(r, lot) for r, ls, _ in shown for lot in ls if lot["place_id"] == pid_]
        if not here and pid_ is None:
            continue
        s.row([name, len({r["id"] for r, _ in here}), sum(lot["left"] for _, lot in here),
               round(sum(lot["left"] * (r["unit_cost"] or 0) for r, lot in here), 2),
               sum(lot["left"] for _, lot in here if lot["state"] == "expired"),
               sum(lot["left"] for _, lot in here if lot["state"] == "soon")],
              styles=[None, "int", "int", "money", "int", "int"])
    s.blank()
    s.row(["By supplier", "Products", "Units", "Worth at cost", "Next visit", "To order"], style="head")
    plan_of = {p.supplier_id: p for p in plans}
    for sup in suppliers:
        rows = [r for r in table if r["supplier_id"] == sup["id"]]
        plan = plan_of.get(sup["id"])
        order = plan.of("order") if plan else []
        s.row([f"{sup['name']}", len(rows), sum(max(r["on_hand"], 0) for r in rows), round(sum(r["value"] for r in rows), 2),
               plan.visit_date if plan else None,
               f"{len(order)} item{'s' if len(order) != 1 else ''}, ${plan.order_value:.2f}" if order else "nothing"],
              styles=[None, "int", "int", "money", "date", None])
    s.blank()
    s.row(["To count the stock or change a product, see the sheet “How to use”."], style="muted")

    # Stock: a row per product
    st = book.sheet("Stock", widths=[w for _, w, _ in STOCK_COLUMNS], freeze=1, filters=True)
    st.row([h for h, _, _ in STOCK_COLUMNS], styles=["head_edit" if e else "head" for _, _, e in STOCK_COLUMNS])
    sup_name = {x["id"]: x["name"] for x in suppliers}
    for r, ls, state in sorted(shown, key=lambda x: x[0]["name_en"].lower()):
        first = next((lot for lot in ls if lot["expiry"]), None)
        label, tone = STATUS.get(state, ("OK", "ok"))
        x = extra.get(r["id"], {})
        st.row([r["sku"], r["name_en"], r["name_km"], sup_name.get(r["supplier_id"], ""), r["unit"], r["case_size"],
                r["unit_cost"], x["barcode"] if x else None, "yes" if r["is_critical"] else "no",
                x["shelf_life_days"] if x else None, max(r["on_hand"], 0) if r["tracked"] else None, r["value"], label,
                r["sold_7d"], r["sold_28d"], r["days_cover"], _day(first["expiry"]) if first else None,
                "; ".join(f"{lot['left']} · {nice(lot['expiry']) or 'no date'}" for lot in ls),
                ", ".join(dict.fromkeys(lot["place"] or "no place yet" for lot in ls))],
               styles=["key", "edit", "edit", "edit", "edit", "edit_int", "edit_money", "edit", "edit", "edit_int", "int",
                       "money", tone, "int", "int", "num",
                       "bad_date" if first and first["state"] == "expired" else "warn_date" if first and first["state"] == "soon"
                       else "date", "wrap", None])
    for _ in range(NEW_ROWS):
        st.row([None] * len(STOCK_COLUMNS), styles=["key"] + ["edit" if e else None for _, _, e in STOCK_COLUMNS[1:]])
    last = len(st.rows)
    st.choices(3, 2, last, [x["name"] for x in suppliers])
    st.choices(8, 2, last, ["yes", "no"])

    # Count: a row per batch, place by place
    ct = book.sheet("Count", widths=[w for _, w, _ in COUNT_COLUMNS], freeze=1, filters=True)
    ct.row([h for h, _, _ in COUNT_COLUMNS], styles=["head_edit" if e else "head" for _, _, e in COUNT_COLUMNS])
    order = {p["id"]: i for i, p in enumerate(shelves)}
    levels = {p["id"]: p["levels"] for p in shelves}
    rows = []
    for r, ls, _ in shown:
        for lot in ls:
            rows.append((order.get(lot["place_id"], 10 ** 6), level_of.get((lot["place_id"], r["id"]), 99), r["name_en"].lower(),
                         lot["expiry"] or "9999", r, lot))
        if not ls and r["tracked"] and r["on_hand"] <= 0:      # out of stock: a row to count it, if some turns up
            home = conn.execute("SELECT location_id FROM products WHERE id = ?", (r["id"],)).fetchone()[0]
            rows.append((order.get(home, 10 ** 6), level_of.get((home, r["id"]), 99), r["name_en"].lower(), "9999", r,
                         {"batch": None, "left": 0, "expiry": None, "lot": None, "place_id": home, "state": "out",
                          "place": next((p["name"] for p in shelves if p["id"] == home), None)}))
    for *_, r, lot in sorted(rows, key=lambda x: x[:4]):
        lv = level_of.get((lot["place_id"], r["id"]))
        n_levels = levels.get(lot["place_id"], 1)
        level = "" if lv is None or n_levels == 1 else "top" if lv == 1 else "bottom" if lv == n_levels else f"level {lv}"
        ct.row([lot["batch"], r["sku"], r["name_en"], lot["place"] or "", level, lot["lot"], _day(lot["expiry"]),
                lot["left"], None],
               styles=["key", "key", None, "edit", "muted", "edit", "edit_date" if lot["state"] not in ("expired", "soon")
                       else ("bad_date" if lot["state"] == "expired" else "warn_date"), "int", "count"])
    for _ in range(NEW_ROWS):
        ct.row([None] * len(COUNT_COLUMNS), styles=["key", "key", "edit", "edit", None, "edit", "edit_date", None, "count"])
    ct.choices(3, 2, len(ct.rows), [p["name"] for p in shelves])

    # How to use
    h = book.sheet("How to use", widths=[110])
    h.row(["How to use this sheet"], style="title")
    h.row([f"Downloaded {now:%d %b %Y, %H:%M} from LuyDek. Count and upload on the same day."], style="subtitle")
    h.blank()
    for title, text in (
        ("Summary", "The stock at a glance: what it is worth, what is out, running low or past its date, by place and supplier."),
        ("Stock", "A row per product. Change the yellow columns (name, supplier, unit, case size, cost, barcode, must-have, "
                  "how many days it keeps) to change the product. To add a product, fill a row at the bottom and leave its ID "
                  "empty. The other columns are for reading: changing them does nothing."),
        ("Count", "A row per batch: the same product with two dates is two rows, count each one. Write what you found in "
                  "the yellow Counted column. Leave it empty for what you didn't count: it stays as it is. 0 means none were "
                  "there. Fix Where, Lot or Expiry on any row if they are wrong."),
        ("Stock LuyDek didn't know about", "Add a row at the bottom of Count: the product's name (or ID), Where, Expiry, "
                                           "and how many you counted."),
        ("Upload", "Save the file, then on LuyDek open Stock in Excel and upload it. Every change is listed first; nothing "
                   "is saved until you press Apply. Sales made after you counted would be undone by an old count, so "
                   "LuyDek tells you about sales since this sheet was downloaded."),
    ):
        h.row([title], style="label")
        h.row([text], style="wrap")
        h.blank()
    return book.save()


# --- upload -----------------------------------------------------------------------------------------

def _columns(head: list) -> dict[str, int]:
    return {shelfsheet.text_of(c).strip().lower(): i for i, c in enumerate(head) if shelfsheet.text_of(c).strip()}


def _get(row: list, cols: dict[str, int], name: str):
    i = cols.get(name.lower())
    return row[i] if i is not None and i < len(row) else ""


def _money(text) -> float | None:
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text)
    t = shelfsheet.text_of(text).replace("$", "").replace(",", "").strip()
    try:
        return float(t) if t else None
    except ValueError:
        raise ValueError(f"“{t}” isn't an amount")


def _whole(text, what: str) -> int | None:
    t = shelfsheet.text_of(text)
    if not t:
        return None
    n = shelfsheet.parse_quantity(text)
    if n is None:
        raise ValueError(f"{what} “{t}” isn't a whole number")
    return n


def read(conn: sqlite3.Connection, cfg: Config, data: bytes, filename: str) -> dict:
    """What an uploaded workbook would change, listed for the owner to check. Nothing is saved."""
    _, sheets = shelfsheet.read(data, filename)
    if "Stock" not in sheets and "Count" not in sheets:
        raise ValueError("That isn't a LuyDek stock sheet: download one on this page, count or edit it, and upload it.")
    plan = {"downloaded": None, "products": [], "new_products": [], "counts": [], "batch_edits": [], "problems": [],
            "since": [], "_edits": [], "_new": [], "_counts": {}}
    if "Summary" in sheets:
        for row in shelfsheet.read(data, filename, "Summary")[0]:
            if row and shelfsheet.text_of(row[0]).lower() == "downloaded" and len(row) > 1:
                v = row[1]
                if isinstance(v, (int, float)):
                    plan["downloaded"] = (datetime(1899, 12, 30) + timedelta(days=float(v))).replace(microsecond=0)
    products = {r["sku"].lower(): r for r in conn.execute(
        "SELECT p.*, (SELECT barcode FROM product_packs k WHERE k.product_id = p.id AND k.barcode IS NOT NULL ORDER BY k.units "
        "LIMIT 1) AS barcode FROM products p")}
    by_id = {r["id"]: r for r in products.values()}
    sup_by_name = {r["name"].lower(): r for r in catalog.suppliers(conn)}
    if "Stock" in sheets:
        _read_stock(conn, shelfsheet.read(data, filename, "Stock")[0], products, sup_by_name, plan)
    if "Count" in sheets:
        _read_count(conn, cfg, shelfsheet.read(data, filename, "Count")[0], products, by_id, plan)
    if plan["downloaded"] and plan["_counts"]:
        ids = list(plan["_counts"])
        for r in conn.execute(f"SELECT product_id, -SUM(qty) AS n FROM stock_movements WHERE type = 'sale' AND created_at > ? "
                              f"AND product_id IN ({','.join('?' * len(ids))}) GROUP BY product_id HAVING n > 0",
                              (clock.stamp(plan["downloaded"]), *ids)):
            plan["since"].append({"name": by_id[r["product_id"]]["name_en"], "sold": r["n"]})
    plan["nothing"] = not (plan["products"] or plan["new_products"] or plan["counts"] or plan["batch_edits"])
    if plan["downloaded"]:
        # A sheet is used once: its rows name batches as they were when it was downloaded, and a second upload
        # (or an edited copy) would count again what the first one already set.
        used = db.get_setting(conn, used_key(plan["downloaded"]))
        if used and not plan["nothing"]:
            plan["problems"].insert(0, {"sheet": "Summary", "row": 2, "text":
                f"this sheet was already used on {used[:16]}: download a fresh one to count or edit again"})
    plan["downloaded"] = plan["downloaded"].isoformat(sep=" ") if plan["downloaded"] else None
    return plan


def used_key(downloaded: datetime) -> str:
    return f"workbook_used:{downloaded.isoformat(sep=' ')}"


def _read_stock(conn, rows: list[list], products: dict, sup_by_name: dict, plan: dict) -> None:
    if not rows:
        return
    cols = _columns(rows[0])
    if "product" not in cols:
        plan["problems"].append({"sheet": "Stock", "row": 1, "text": "the Product column is missing"})
        return
    for n, row in enumerate(rows[1:], start=2):
        sku = shelfsheet.text_of(_get(row, cols, "ID"))
        name = shelfsheet.text_of(_get(row, cols, "Product"))
        if not sku and not name:
            continue
        try:
            want = {"name_en": name, "name_km": shelfsheet.text_of(_get(row, cols, "Khmer name")),
                    "unit": shelfsheet.text_of(_get(row, cols, "Unit")),
                    "case_size": _whole(_get(row, cols, "Case size"), "Case size"),
                    "unit_cost": _money(_get(row, cols, "Cost per unit")),
                    "barcode": shelfsheet.text_of(_get(row, cols, "Barcode")),
                    "is_critical": (int(shelfsheet.text_of(_get(row, cols, "Must-have")).lower() in YES)
                                    if shelfsheet.text_of(_get(row, cols, "Must-have")) else None),
                    "shelf_life_days": _whole(_get(row, cols, "Keeps for (days)"), "Keeps for (days)")}
            sup = shelfsheet.text_of(_get(row, cols, "Supplier"))
            if sup and sup.lower() not in sup_by_name:
                raise ValueError(f"no supplier called “{sup}” (add it on Order lists, or pick one from the list)")
            want["supplier_id"] = sup_by_name[sup.lower()]["id"] if sup else None
            if want["case_size"] is not None and want["case_size"] < 1:
                raise ValueError("Case size must be 1 or more")
            if want["unit_cost"] is not None and want["unit_cost"] < 0:
                raise ValueError("Cost per unit can't be below 0")
            if want["barcode"] and not want["barcode"].isdigit():
                raise ValueError(f"Barcode “{want['barcode']}” should be digits only")
        except ValueError as exc:
            plan["problems"].append({"sheet": "Stock", "row": n, "text": str(exc)})
            continue
        if not sku:
            if not name:
                continue
            plan["new_products"].append({"row": n, "name": name})
            plan["_new"].append(want)
            continue
        p = products.get(sku.lower())
        if p is None:
            plan["problems"].append({"sheet": "Stock", "row": n, "text": f"no product with the ID “{sku}”"})
            continue
        if not name:
            plan["problems"].append({"sheet": "Stock", "row": n, "text": f"{p['name_en']} needs a name"})
            continue
        if want["barcode"]:
            other = conn.execute("SELECT p.name_en FROM product_packs k JOIN products p ON p.id = k.product_id "
                                 "WHERE k.barcode = ? AND k.product_id != ?", (want["barcode"], p["id"])).fetchone()
            if other:
                plan["problems"].append({"sheet": "Stock", "row": n,
                                         "text": f"barcode {want['barcode']} is already {other['name_en']}'s"})
                continue
        labels = {"name_en": "Name", "name_km": "Khmer name", "unit": "Unit", "case_size": "Case size",
                  "unit_cost": "Cost per unit", "barcode": "Barcode", "is_critical": "Must-have",
                  "shelf_life_days": "Keeps for (days)", "supplier_id": "Supplier"}
        changes, fields = [], {}
        for key, new in want.items():
            old = p[key]
            if key in ("case_size", "is_critical") and new is None:
                same = True                                  # left empty: no change
            elif key == "unit_cost":
                same = new is None or abs((old or 0) - new) < 0.0005
            elif key in ("name_km", "unit"):
                same = not new or (old or "") == new
            elif key == "barcode":
                same = (old or "") == (new or "")
            else:
                same = (old if old is not None else None) == new
            if same:
                continue
            fields[key] = new
            show = {"unit_cost": lambda v: f"${v:.2f}" if v is not None else "–", "is_critical": lambda v: "yes" if v else "no",
                    "supplier_id": lambda v: next((s["name"] for s in sup_by_name.values() if s["id"] == v), "none")}
            fmt = show.get(key, lambda v: "–" if v in (None, "") else str(v))
            changes.append({"field": labels[key], "old": fmt(old), "new": fmt(new)})
        if changes:
            plan["products"].append({"id": p["id"], "name": p["name_en"], "changes": changes})
            plan["_edits"].append((p["id"], fields))


def _read_count(conn, cfg: Config, rows: list[list], products: dict, by_id: dict, plan: dict) -> None:
    if not rows:
        return
    cols = _columns(rows[0])
    if "counted" not in cols:
        plan["problems"].append({"sheet": "Count", "row": 1, "text": "the Counted column is missing"})
        return
    place_by_name = {p["name"].lower(): p for p in stockmap.shelves(conn)}
    name_of = {p["id"]: p["name"] for p in stockmap.shelves(conn)}
    lines: dict[int, list[dict]] = {}
    for n, row in enumerate(rows[1:], start=2):
        sku = shelfsheet.text_of(_get(row, cols, "ID"))
        name = shelfsheet.text_of(_get(row, cols, "Product"))
        counted_cell = _get(row, cols, "Counted")
        if not sku and not name:
            continue
        try:
            counted = _whole(counted_cell, "Counted")
            p = products.get(sku.lower()) if sku else None
            if p is None and name:
                found = shelfsheet.interpret(conn, [["Product"], [name]], 0, {**{f: None for f in shelfsheet.FIELDS},
                                                                           "product": 0})["rows"]
                pid = found[0]["product"] if found and found[0]["how"] in ("name", "barcode") else None
                p = by_id.get(pid) if pid else None
            if p is None:
                raise ValueError(f"no product “{sku or name}”: add it on the Stock sheet first")
            where = shelfsheet.text_of(_get(row, cols, "Where"))
            if where and where.lower() in ("no place", "no place yet"):
                where = ""
            if where and where.lower() not in place_by_name:
                raise ValueError(f"no place called “{where}” in the shop")
            place = place_by_name[where.lower()]["id"] if where else None
            exp_cell = _get(row, cols, "Expiry")
            expiry = shelfsheet.parse_date(exp_cell)
            if shelfsheet.text_of(exp_cell) and expiry is None:
                raise ValueError(f"Expiry “{shelfsheet.text_of(exp_cell)}” isn't a date")
            lot = shelfsheet.text_of(_get(row, cols, "Lot"))[:40] or None
            batch_id = _whole(_get(row, cols, "Batch"), "Batch")
            b = batches.get(conn, batch_id) if batch_id else None
            if batch_id and (b is None or b.product_id != p["id"]):
                raise ValueError(f"batch {batch_id} isn't {p['name_en']}'s: was the row copied?")
        except ValueError as exc:
            plan["problems"].append({"sheet": "Count", "row": n, "text": str(exc)})
            continue
        lines.setdefault(p["id"], []).append({"row": n, "batch": b, "place": place, "expiry": expiry, "lot": lot,
                                              "counted": counted})
    for pid, ls in lines.items():
        p = by_id[pid]
        open_ = batches.open_batches(conn, pid)
        left = {b.id: b.left for b in open_}
        # Corrections without a count: a batch moved, its date or its lot fixed.
        for line in ls:
            b = line["batch"]
            if b is None or b.id not in left:
                continue
            what = []
            if line["place"] != b.location_id:
                what.append(f"moved from {name_of.get(b.location_id, 'no place')} to {name_of.get(line['place'], 'no place')}")
            if line["expiry"] != b.expiry:
                what.append(f"date {nice(b.expiry) or 'none'} → {nice(line['expiry']) or 'none'}")
            if line["lot"] != (b.lot_no or None):
                what.append(f"lot {b.lot_no or 'none'} → {line['lot'] or 'none'}")
            if what and line["counted"] is not None:
                plan["batch_edits"].append({"name": p["name_en"], "left": b.left, "what": "; ".join(what) + " (and counted)"})
            elif what:                                   # a counted row's corrections are part of its count
                plan["batch_edits"].append({"name": p["name_en"], "left": b.left, "what": "; ".join(what)})
                plan["_edits"].append(("batch", b.id, line["place"], line["expiry"], line["lot"]))
        # Counts: every place a counted row is on (and the place its batch was on), set to the rows there.
        counted = [line for line in ls if line["counted"] is not None]
        if not counted:
            continue
        touched = {line["place"] for line in counted} | {line["batch"].location_id for line in counted if line["batch"]}
        places: dict[int | None, list] = {}
        for place in touched:
            groups, before = [], 0
            for line in ls:
                if line["place"] != place:
                    continue
                books = left.get(line["batch"].id, 0) if line["batch"] else 0
                before += books                       # what the books say is here, once the sheet's moves are made
                n = line["counted"] if line["counted"] is not None else books
                if n > 0:
                    groups.append(stockin.Group(n, line["expiry"], line["lot"]))
            # batches at this place the sheet doesn't list (came in after it was downloaded) stay as they are
            listed = {line["batch"].id for line in ls if line["batch"]}
            for b in open_:
                if b.location_id == place and b.id not in listed:
                    groups.append(stockin.Group(b.left, b.expiry, b.lot_no))
                    before += b.left
            after = sum(g.count for g in groups)
            places[place] = groups
            if after != before:
                plan["counts"].append({"product_id": pid, "name": p["name_en"], "place": name_of.get(place, "No place yet"),
                                       "before": before, "after": after, "diff": after - before,
                                       "dates": "; ".join(f"{g.count} · {nice(g.expiry) or 'no date'}" for g in groups)})
        plan["_counts"][pid] = places


def apply(conn: sqlite3.Connection, cfg: Config, data: bytes, filename: str, by: str) -> dict:
    """Save what an uploaded workbook changes. Refused while any row can't be read."""
    plan = read(conn, cfg, data, filename)
    if plan["problems"]:
        raise ValueError(f"{len(plan['problems'])} row(s) can't be read yet: fix them in the sheet and upload it again.")
    for edit in plan["_edits"]:
        if edit[0] == "batch":
            _, batch_id, place, expiry, lot = edit
            batches.update(conn, batch_id, location_id=place, expiry=expiry, lot_no=lot)
        else:
            pid, fields = edit
            catalog.update_product(conn, pid, **fields)
    created = []
    for want in plan["_new"]:
        pid = catalog.add_product(conn, sku=catalog.unique_sku(conn, want["name_en"]), name_en=want["name_en"],
                                  name_km=want["name_km"] or want["name_en"], unit=want["unit"] or "piece",
                                  case_size=want["case_size"] or 1, supplier_id=want["supplier_id"],
                                  is_critical=bool(want["is_critical"]), shelf_life_days=want["shelf_life_days"],
                                  unit_cost=want["unit_cost"] or 0.0, barcode=want["barcode"] or None, origin="sheet")
        created.append(want["name_en"])
    counted = 0
    for pid, places in plan["_counts"].items():
        if any(c["product_id"] == pid for c in plan["counts"]):
            shelfsheet.count_places(conn, pid, places, by)
            counted += 1
    if plan["downloaded"]:
        db.set_setting(conn, used_key(datetime.fromisoformat(plan["downloaded"])), clock.stamp())
    return {"products": len(plan["products"]), "created": created, "counted": counted,
            "batch_edits": len(plan["batch_edits"]), "units": sum(c["diff"] for c in plan["counts"])}


def public(plan: dict) -> dict:
    """The plan without the parts only `apply` needs."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}
