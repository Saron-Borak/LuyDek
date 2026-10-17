"""Shelves from the owner's own spreadsheet: Excel (.xlsx) or CSV, whatever its columns are called.

A shop that keeps a list keeps it in Excel: the product, how many, maybe where it is and when it
expires. The columns are guessed from their headings (English or Khmer), and the owner can change
the guess. Each product is matched to the list as invoice lines are (barcode, then a name it is
known by, then the closest name); any that don't match are asked about, and become new products
unless the owner picks one: never dropped. The sheet's name for each is remembered, so the same
sheet next time matches on its own. Then one save:

* each place named in the sheet gets its levels and its products, left to right, replacing what was
  drawn for it before (places not in the sheet are left alone);
* with "count the stock" on, each product's stock on each place in the sheet is set to the sheet's
  quantities, as batches with their dates; its stock on places the sheet doesn't mention is kept
  (a sheet of the fridge is a count of the fridge). A product with no stock anywhere else gets an
  exact count of everything, as on Stock in.

Excel's merged cells leave the cells under a place name empty, so an empty place cell means the
place above. Without a level column, a place's products are shared out over its levels in order.

A .xlsx is a zip of XML files, read here with the standard library: no spreadsheet package. The
old binary .xls is not; Excel saves a copy as .xlsx.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from datetime import date, timedelta
from xml.etree import ElementTree as ET

from .. import clock
from . import batches, catalog, learn, stock, stockin, stockmap

MAX_ROWS = 5000
FIELDS = ("product", "shelf", "level", "position", "quantity", "expiry", "lot", "barcode", "facings")
HEADINGS = {
    "product": ("product", "products", "item", "items", "name", "item name", "product name", "description",
                "goods", "article", "medicine", "drug", "ទំនិញ", "ឈ្មោះ", "ផលិតផល", "មុខទំនិញ", "ឈ្មោះទំនិញ", "ថ្នាំ"),
    "shelf": ("shelf", "shelf name", "location", "place", "rack", "aisle", "bay", "zone", "section", "where",
              "ធ្នើ", "ទីតាំង", "កន្លែង"),
    "level": ("level", "shelf level", "row", "tier", "layer", "height", "ជាន់", "ថ្នាក់", "ជួរ"),
    "position": ("position", "pos", "slot", "order", "column", "from left", "from the left", "លំដាប់"),
    "quantity": ("quantity", "qty", "count", "counted", "stock", "on hand", "units", "amount", "pcs", "balance",
                 "ចំនួន", "បរិមាណ", "ស្តុក"),
    "expiry": ("expiry", "expiry date", "exp", "exp date", "expiration", "expiration date", "expires", "best before",
               "bb", "bbe", "use by", "ផុតកំណត់", "ថ្ងៃផុតកំណត់", "ហួសកំណត់", "ថ្ងៃហួសកំណត់"),
    "lot": ("lot", "lot no", "lot number", "batch", "batch no", "batch number", "ឡូត៍", "លេខឡូត៍"),
    "barcode": ("barcode", "bar code", "ean", "upc", "gtin", "sku", "item code", "product code", "code", "បាកូដ",
                "លេខកូដ"),
    "facings": ("facings", "facing", "faces", "width", "wide"),
}
TOP = ("top", "upper", "highest", "លើ", "ខាងលើ", "លើគេ")
BOTTOM = ("bottom", "lower", "lowest", "floor", "ក្រោម", "ខាងក្រោម", "ក្រោមគេ")
MIDDLE = ("middle", "centre", "center", "mid", "កណ្តាល", "កណ្ដាល")
TOTALS = ("total", "totals", "grand total", "sub total", "subtotal", "sum", "សរុប", "សរុបរួម")  # a sheet's sum line
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
                                     start=1)}
EXCEL_EPOCH = date(1899, 12, 30)


# --- reading the file -----------------------------------------------------------------------------

def read(data: bytes, filename: str, sheet: str | None = None) -> tuple[list[list], list[str]]:
    """(rows of cells, names of the workbook's sheets) from a .xlsx or .csv file. Cells are text, or
    numbers as Excel stored them (a date in Excel is a number of days)."""
    name = (filename or "").lower()
    if data[:4] == b"PK\x03\x04" or name.endswith((".xlsx", ".xlsm")):
        return _xlsx(data, sheet)
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" or name.endswith(".xls"):
        raise ValueError("That is an old Excel file (.xls). In Excel: File, Save as, Excel Workbook (.xlsx), then try again.")
    return _csv(data), []


M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _column(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _xlsx(data: bytes, sheet: str | None) -> tuple[list[list], list[str]]:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        workbook = ET.fromstring(z.read("xl/workbook.xml"))
    except (zipfile.BadZipFile, KeyError, ET.ParseError):
        raise ValueError("That file doesn't open as an Excel workbook.")
    with z:
        sheets = [(s.get("name"), s.get(f"{R}id")) for s in workbook.iter(f"{M}sheet")]
        if not sheets:
            raise ValueError("That workbook has no sheets.")
        name, rid = next((s for s in sheets if s[0] == sheet), sheets[0])
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        target = next((r.get("Target") for r in rels if r.get("Id") == rid), None)
        if target is None:
            raise ValueError("That workbook's sheets can't be found.")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(f"{M}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{M}t")))
        rows: list[list] = []
        for row in ET.fromstring(z.read(path)).iter(f"{M}row"):
            number = int(row.get("r") or len(rows) + 1)
            while len(rows) < number - 1:
                rows.append([])                       # rows Excel left out: empty ones
            cells: dict[int, object] = {}
            for c in row.iter(f"{M}c"):
                col = _column(c.get("r")) if c.get("r") else len(cells)
                kind, v = c.get("t"), c.find(f"{M}v")
                text = v.text if v is not None and v.text is not None else ""
                if kind == "s":
                    value: object = shared[int(text)] if text else ""
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in c.iter(f"{M}t"))
                elif kind == "b":
                    value = "TRUE" if text == "1" else "FALSE"
                elif kind in ("str", "d", "e"):
                    value = text
                else:
                    try:
                        value = float(text) if text else ""
                    except ValueError:
                        value = text
                cells[col] = value
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
            if len(rows) > MAX_ROWS:
                raise ValueError(f"That sheet has more than {MAX_ROWS} rows: split it, a few shelves at a time.")
    return rows, [s[0] for s in sheets]


def _csv(data: bytes) -> list[list]:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = data.decode("utf-16")                 # Excel's "Unicode text"
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect)]
    if len(rows) > MAX_ROWS:
        raise ValueError(f"That sheet has more than {MAX_ROWS} rows: split it, a few shelves at a time.")
    return rows


# --- what the columns are -------------------------------------------------------------------------

def _norm(text) -> str:
    return " ".join(re.sub(r"[^\wក-៿]+", " ", str(text).lower().replace("_", " ")).split())


def _heading_score(text) -> tuple[str | None, int]:
    """The field a heading names, and how sure (the length of the name it holds: "stock location" is
    a place more than a quantity)."""
    h = _norm(text)
    if not h or _number(text):
        return None, 0
    best, score = None, 0
    words = f" {h} "
    for field, names in HEADINGS.items():
        for n in names:
            s = 100 + len(n) if h == n else len(n) if f" {n} " in words or (len(n) >= 4 and n in h) else 0
            if s > score:
                best, score = field, s
    return best, score


def guess(rows: list[list]) -> tuple[int, dict[str, int | None]]:
    """(the heading row, {field: column}) for a sheet: the row among the first few with the most
    headings it understands; each column for one field, the surest first."""
    best_row, best_hits = 0, -1
    for i, row in enumerate(rows[:15]):
        hits = sum(1 for c in row if _heading_score(c)[0])
        if hits > best_hits:
            best_row, best_hits = i, hits
    if best_hits <= 0:
        best_row = next((i for i, r in enumerate(rows) if any(str(c).strip() for c in r)), 0)
    heading = rows[best_row] if rows else []
    scored = sorted(((s, i, f) for i, c in enumerate(heading) for f, s in [_heading_score(c)] if f),
                    reverse=True)
    mapping: dict[str, int | None] = {f: None for f in FIELDS}
    used = set()
    for _, col, field in scored:
        if mapping[field] is None and col not in used:
            mapping[field] = col
            used.add(col)
    return best_row, mapping


# --- what each cell says --------------------------------------------------------------------------

def _number(value) -> bool:
    """A number as Excel stored it (a float), or as it comes back from the page (0.0 is 0 in JSON)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def text_of(value) -> str:
    if _number(value):
        value = float(value)
        return str(int(value)) if value.is_integer() else str(value)
    return " ".join(str(value or "").split())


def parse_quantity(value) -> int | None:
    if _number(value):
        return int(round(value)) if value >= 0 else None
    m = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value or ""))
    if not m:
        return None
    n = float(m.group().replace(",", ""))
    return int(round(n)) if n >= 0 else None


def parse_level(value) -> int | str | None:
    """A level number (1 is the top), or "top", "middle", "bottom" for the words."""
    if _number(value):
        return int(value) if value >= 1 else None
    t = _norm(value)
    if not t:
        return None
    m = re.search(r"\d+", t)
    if m:
        return int(m.group()) or None
    for word, names in (("top", TOP), ("bottom", BOTTOM), ("middle", MIDDLE)):
        if any(n in t for n in names):
            return word
    return None


def parse_date(value) -> str | None:
    """An expiry date as written in a shop's sheet: 31/12/2026 (day first, as in Cambodia), 2026-12-31,
    12/2026 or Dec 2026 (the end of that month), 31 Dec 26, or an Excel date number."""
    if _number(value) or re.fullmatch(r"\d{5}(\.0+)?", str(value or "").strip()):
        n = float(value)
        if 20000 <= n <= 80000:
            return (EXCEL_EPOCH + timedelta(days=int(n))).isoformat()
        return None
    t = str(value or "").strip().lower()
    if not t:
        return None
    t = re.sub(r"^(exp|expiry|bb|best before|use by)[:.\s]*", "", t)

    def full(y: int) -> int:
        return y + 2000 if y < 100 else y

    def end_of(y: int, m: int) -> date:
        return (date(y, m, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    try:
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ t].*)?", t):
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", t):
            d, mo, y = int(m[1]), int(m[2]), full(int(m[3]))
            if mo > 12 and d <= 12:
                d, mo = mo, d                           # 12/31/2026: month first after all
            return date(y, mo, d).isoformat()
        if m := re.fullmatch(r"(\d{1,2})[-/.](\d{4}|\d{2})", t):
            return end_of(full(int(m[2])), int(m[1])).isoformat()
        if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})", t):
            return end_of(int(m[1]), int(m[2])).isoformat()
        if m := re.fullmatch(r"(\d{1,2})[\s-]+([a-z]{3})[a-z]*\.?[\s-]+(\d{2,4})", t):
            return date(full(int(m[3])), MONTHS[m[2]], int(m[1])).isoformat()
        if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?[\s-]+(\d{1,2}),?[\s-]+(\d{4})", t):
            return date(int(m[3]), MONTHS[m[1]], int(m[2])).isoformat()
        if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?[\s-]+(\d{2,4})", t):
            return end_of(full(int(m[2])), MONTHS[m[1]]).isoformat()
    except (ValueError, KeyError):
        return None
    return None


# --- the rows, as the owner will check them -------------------------------------------------------

def _product_for(conn: sqlite3.Connection, name: str, code: str, active: dict[int, set[str]]
                 ) -> tuple[int | None, str | None, list[int]]:
    """(product, how it was found, products to offer) for a row's name and barcode. `active`: each
    product still in the list, with the names it goes by. Products taken off the list are never offered."""
    if code:
        pack = catalog.pack_by_barcode(conn, code)
        pid = catalog.pack(conn, pack)["product_id"] if pack is not None else None
        if pid in active:
            return pid, "barcode", []
        row = conn.execute("SELECT id FROM products WHERE active = 1 AND sku = ? COLLATE NOCASE", (code,)).fetchone()
        if row is not None:
            return row["id"], "barcode", []
    if not name:
        return None, None, []
    norm = catalog.normalize(name)
    known = catalog.resolve_alias(conn, name)
    if known in active:
        return known, "name", []
    exact = next((pid for pid, names in active.items() if norm in names), None)
    if exact is not None:
        return exact, "name", []
    offer = [p for p in catalog.likely_products(conn, name, limit=8) if p in active][:3]
    scored = {p: v for p, v in catalog.fuzzy_scored(conn, name, limit=12) if p in active}
    if offer and scored.get(offer[0], 0) >= 0.85:
        return offer[0], "close", offer            # nearly the same name: the guess, to check
    return None, None, offer


def interpret(conn: sqlite3.Connection, rows: list[list], heading: int, mapping: dict[str, int | None],
              shelf_for_all: str | None = None) -> dict:
    """Each row below the headings, read with the owner's column choices: what it is, where, how many,
    when it expires; with the problems found. Rows with no product name or barcode, and totals, are
    counted, not listed (notes, empty lines)."""
    def cell(row: list, field: str):
        col = mapping.get(field)
        return row[col] if col is not None and 0 <= col < len(row) else ""

    out, skipped, place, cache = [], 0, None, {}
    active: dict[int, set[str]] = {}
    for r in conn.execute("SELECT id, name_en, name_km FROM products WHERE active = 1"):
        active[r["id"]] = {catalog.normalize(r["name_en"] or ""), catalog.normalize(r["name_km"] or "")}
    for number, row in enumerate(rows[heading + 1:], start=heading + 2):
        name, code = text_of(cell(row, "product")), text_of(cell(row, "barcode"))
        if _norm(name) in TOTALS and not code:
            name = ""                                  # the total at the foot of the sheet: not a product
        if not name and not code:
            if any(text_of(c) for c in row):
                skipped += 1
            continue
        if mapping.get("shelf") is not None:
            here = text_of(cell(row, "shelf"))
            place = here or place                      # an empty cell under a merged one: the place above
        shelf = shelf_for_all or place
        problems = []
        level_cell = cell(row, "level")
        level = parse_level(level_cell)
        if text_of(level_cell) and level is None:
            problems.append(f"level “{text_of(level_cell)}” not understood")
        qty_cell = cell(row, "quantity")
        quantity = parse_quantity(qty_cell)
        if text_of(qty_cell) and quantity is None:
            problems.append(f"quantity “{text_of(qty_cell)}” not understood")
        exp_cell = cell(row, "expiry")
        expiry = parse_date(exp_cell)
        if text_of(exp_cell) and expiry is None:
            problems.append(f"date “{text_of(exp_cell)}” not understood")
        key = (name.lower(), code)
        if key not in cache:
            cache[key] = _product_for(conn, name, code, active)
        product, how, offer = cache[key]
        out.append({"row": number, "name": name, "barcode": code, "product": product, "how": how, "offer": offer,
                    "shelf": shelf, "level": level, "position": parse_quantity(cell(row, "position")),
                    "quantity": quantity, "expiry": expiry, "lot": text_of(cell(row, "lot"))[:40] or None,
                    "facings": parse_quantity(cell(row, "facings")), "problems": problems})
    return {"rows": out, "skipped": skipped}


# --- saving ---------------------------------------------------------------------------------------

def save(conn: sqlite3.Connection, rows: list[dict], *, count_stock: bool, by: str = "sheet") -> dict:
    """Put the sheet's rows on the shelves (and count the stock, if asked). Each row: its `product`
    (an id), or `new_name` for a product to add; `shelf` (a place's name, or none); `level`,
    `position`, `facings`, `quantity`, `expiry`, `lot`, `barcode` as read. Returns what happened."""
    created, product_of = [], {}
    for r in rows:
        if r.get("product"):
            pid = int(r["product"])
            if not conn.execute("SELECT 1 FROM products WHERE id = ? AND active = 1", (pid,)).fetchone():
                raise ValueError(f"row {r.get('row')}: that product is gone from the list")
        else:
            name = learn.clean_name(r.get("new_name") or r.get("name") or "")
            if not name:
                raise ValueError(f"row {r.get('row')}: a new product needs a name")
            key = name.lower()
            if key not in product_of:
                pid = catalog.resolve_alias(conn, name)
                if pid is None or not conn.execute("SELECT active FROM products WHERE id = ?", (pid,)).fetchone()[0]:
                    pid = catalog.add_product(conn, sku=catalog.unique_sku(conn, name), name_en=name, name_km=name,
                                              origin="sheet")
                    created.append(name)
                product_of[key] = pid
            pid = product_of[key]
        r["_pid"] = pid
        written = str(r.get("name") or "")
        if written and catalog.resolve_alias(conn, written) is None:
            catalog.add_alias(conn, pid, written, "sheet")   # the sheet's own name for it, known next time
        code = str(r.get("barcode") or "").strip()
        if code and code.isdigit() and len(code) >= 8 and catalog.pack_by_barcode(conn, code) is None:
            conn.execute("UPDATE product_packs SET barcode = ? WHERE id = ? AND barcode IS NULL",
                         (code, catalog.default_pack_id(conn, pid)))      # the scanner knows it from now on

    # The places, each with its products level by level.
    by_shelf: dict[str, list[dict]] = {}
    names: dict[str, str] = {}
    for r in rows:
        shelf = " ".join(str(r.get("shelf") or "").split())
        if shelf:
            by_shelf.setdefault(shelf.lower(), []).append(r)
            names.setdefault(shelf.lower(), shelf)
    place_of: dict[str, int] = {}
    standing = 0
    for key, items in by_shelf.items():
        existing = conn.execute("SELECT id, kind, levels, active FROM locations WHERE name = ? COLLATE NOCASE",
                                (names[key],)).fetchone()
        location_id = stockmap.add_place(conn, names[key], kind=None if existing else stockmap.kind_of(names[key]))
        place_of[key] = location_id
        kind = (existing["kind"] if existing and existing["kind"] in stockmap.KINDS else stockmap.kind_of(names[key]))
        # As many levels as the highest number written (or the place has); "top" and "bottom" don't say how
        # many there are, so with those, at least the usual for the kind of place.
        numbers = [r["level"] for r in items if isinstance(r.get("level"), int)]
        worded = any(isinstance(r.get("level"), str) for r in items)
        levels = max(numbers + [existing["levels"] if existing and existing["levels"] else 0]
                     + [max(2, stockmap.LEVELS[kind]) if worded else 0]) or stockmap.LEVELS[kind]
        levels = min(levels, stockmap.MAX_LEVELS)
        placed = [r for r in items if r.get("level") is not None]
        if not placed:                               # no levels in the sheet: share them out in order
            products = list(dict.fromkeys(r["_pid"] for r in items))
            per = -(-len(products) // levels)
            level_of = {pid: i // per + 1 for i, pid in enumerate(products)}
        for r in items:
            lv = r.get("level")
            r["_level"] = (level_of[r["_pid"]] if not placed else 1 if lv == "top" else levels if lv == "bottom"
                           else (levels + 1) // 2 if lv == "middle" else lv if isinstance(lv, int) else None)
        spots: list[dict] = []
        order = sorted((r for r in items if r["_level"]),
                       key=lambda r: (r["_level"], r.get("position") or 10 ** 6, r.get("row") or 0))
        for r in order:
            same = next((s for s in spots if s["product"] == r["_pid"] and s["level"] == r["_level"]), None)
            width = max(1, min(stockmap.MAX_WIDTH, int(r.get("facings") or 1)))
            if same:
                same["width"] = max(same["width"], width)
            else:
                spots.append({"product": r["_pid"], "level": r["_level"], "width": width})
        stockmap.save_shelf(conn, location_id, spots, levels=levels)
        standing += len(spots)

    counted, units = 0, 0
    if count_stock:
        parts: dict[int, dict[int | None, list[stockin.Group]]] = {}
        for r in rows:
            if r.get("quantity") is None:
                continue
            q = int(r["quantity"])
            place = place_of.get(" ".join(str(r.get("shelf") or "").split()).lower())
            groups = parts.setdefault(r["_pid"], {}).setdefault(place, [])
            if q > 0:
                groups.append(stockin.Group(q, r.get("expiry") or None, r.get("lot") or None))
        for pid, places in parts.items():
            count_places(conn, pid, places, by)
            counted += 1
            units += sum(g.count for gs in places.values() for g in gs)
    return {"places": len(by_shelf), "spots": standing, "created": created, "counted": counted, "units": units}


def count_places(conn: sqlite3.Connection, product_id: int, places: dict[int | None, list], by: str) -> None:
    """Set a product's stock on each place counted (None: stock with no place yet) to its batches in the
    sheet, leaving its stock elsewhere alone. With none elsewhere, it is an exact count of everything,
    the way Stock in counts (sales still to be checked and any shortfall taken care of)."""
    open_ = batches.open_batches(conn, product_id)
    here = [b for b in open_ if b.location_id in places]
    if not [b for b in open_ if b.location_id not in places]:
        rows = [stockin.Row(catalog.default_pack_id(conn, product_id), groups=gs, location_id=place)
                for place, gs in places.items() if gs]
        if rows:
            stockin.save(conn, "count", rows, by=by)
        else:
            stock.count(conn, product_id, 0, "sheet", by=by)      # counted none
        return
    total = sum(g.count for gs in places.values() for g in gs)
    conn.execute("INSERT INTO stock_counts (product_id, counted, expected, source, counted_by, created_at) "
                 "VALUES (?, ?, ?, 'sheet', ?, ?)", (product_id, total, sum(b.left for b in here), by, clock.stamp()))
    for b in here:
        stock.record(conn, product_id, -b.left, "spot_check", batch_id=b.id, reason=f"recounted from a sheet by {by}")
    for place, gs in places.items():
        for g in sorted(gs, key=lambda g: g.expiry or batches.NO_DATE):
            stock.record(conn, product_id, g.count, "spot_check", reason=f"counted from a sheet by {by}",
                         expiry_date=g.expiry, location_id=place, lot_no=g.lot)


# --- the shop's shelves as a sheet ------------------------------------------------------------------

COLUMNS = ("Shelf", "Level (1 = top)", "Position (from the left)", "Product", "Product (Khmer)", "Barcode",
           "Facings", "Quantity", "Expiry (DD/MM/YYYY)", "Lot")


def export(conn: sqlite3.Connection) -> str:
    """The shelves as a CSV that opens in Excel (Khmer too), to fill in and bring back: a row per
    batch on each shelf, the products on no shelf first (an empty place cell would mean "the place
    above"). Quantities are what is on the shelf now."""
    places = {p["id"]: p for p in stockmap.shelves(conn)}
    spots = stockmap.facings(conn)
    prods = {r["id"]: r for r in conn.execute(
        "SELECT p.id, p.name_en, p.name_km, (SELECT barcode FROM product_packs k WHERE k.product_id = p.id "
        "AND k.units = 1 AND k.barcode IS NOT NULL LIMIT 1) AS barcode FROM products p WHERE p.active = 1 "
        "ORDER BY p.name_en")}
    tracked = {r["id"] for r in stock.stock_table(conn) if r["tracked"]}
    open_by: dict[tuple, list] = {}
    for pid, s in batches.replay(conn).items():
        for b in s.open:
            open_by.setdefault((pid, b.location_id if b.location_id in places else None), []).append(b)

    def lines(pid: int, place: int | None, level="", position="", width="") -> list[list]:
        p = prods[pid]
        here = open_by.pop((pid, place), [])
        base = [places[place]["name"] if place else "", level, position, p["name_en"],
                p["name_km"] if p["name_km"] != p["name_en"] else "", p["barcode"] or "", width]
        if not here:
            return [base + ["0" if pid in tracked else "", "", ""]]
        return [base + [b.left, f"{b.expiry[8:10]}/{b.expiry[5:7]}/{b.expiry[:4]}" if b.expiry else "", b.lot_no or ""]
                for b in here]

    shelved: list[list] = []
    seen = set()
    for location_id in places:
        pos: dict[int, int] = {}
        for f in spots.get(location_id, []):
            if f["product"] not in prods:
                continue
            pos[f["level"]] = pos.get(f["level"], 0) + 1
            seen.add(f["product"])
            shelved += lines(f["product"], location_id, f["level"], pos[f["level"]], f["width"])
        for (pid, place) in [k for k in open_by if k[1] == location_id and k[0] in prods]:
            seen.add(pid)
            shelved += lines(pid, place)               # on this shelf, no spot on a level yet
    body = [line for pid in prods if (pid, None) in open_by or pid not in seen for line in lines(pid, None)]
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(COLUMNS)
    w.writerows(body + shelved)
    return "﻿" + out.getvalue()
