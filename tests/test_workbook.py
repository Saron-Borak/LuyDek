"""The stock in Excel: a workbook out (summary, products, every batch to count), and back in: counts per
batch and place, corrections, product details; listed before anything is saved, used once."""

from datetime import timedelta

from luydek import clock, db
from luydek.services import batches, locations, shelfsheet, stock, stockmap, workbook, xlsx
from tests.conftest import pid
from tests.test_shelf import client_for


def day(n):
    return clock.now().replace(hour=12) - timedelta(days=n)


def on(n):
    return (clock.today() + timedelta(days=n)).isoformat()


def shop(conn):
    fridge, shelf = locations.add(conn, "Fridge"), locations.add(conn, "Shelf A")
    milk, coke = pid(conn, "dutchlady-straw"), pid(conn, "coke-330")
    stockmap.save_shelf(conn, fridge, [{"product": coke, "level": 1}, {"product": milk, "level": 2}])
    stock.record(conn, milk, 10, "delivery", expiry_date=on(-3), location_id=fridge, lot_no="L1", at=day(30))
    stock.record(conn, milk, 30, "delivery", expiry_date=on(120), location_id=fridge, lot_no="L2", at=day(5))
    stock.record(conn, coke, 24, "delivery", expiry_date=on(200), location_id=fridge, at=day(4))
    stock.record(conn, coke, 48, "delivery", expiry_date=on(220), location_id=shelf, at=day(4))
    conn.commit()
    return fridge, shelf, milk, coke


def sheets(data: bytes) -> dict[str, list[list]]:
    names = shelfsheet.read(data, "s.xlsx")[1]
    return {n: shelfsheet.read(data, "s.xlsx", n)[0] for n in names}


def rewrite(book: dict[str, list[list]]) -> bytes:
    """The workbook as an owner saves it after editing: the same sheets, the new values."""
    out = xlsx.Workbook()
    for name, rows in book.items():
        s = out.sheet(name)
        for row in rows:
            s.row(list(row))
    return out.save()


def col(rows, name):
    return [shelfsheet.text_of(c).lower() for c in rows[0]].index(name.lower())


def test_the_workbook_has_the_stock_every_batch_and_room_to_count(conn, cfg):
    fridge, shelf, milk, coke = shop(conn)
    book = sheets(workbook.build(conn, cfg))
    assert list(book) == ["Summary", "Stock", "Count", "How to use"]
    summary = {shelfsheet.text_of(r[0]): r[1:] for r in book["Summary"] if r}
    assert summary["Units past their date"][0] == 10 and "10 Dutch Lady" in shelfsheet.text_of(summary["Units past their date"][1])
    stock_rows = book["Stock"]
    row = next(r for r in stock_rows if r[col(stock_rows, "Product")] == "Dutch Lady UHT milk strawberry 180ml")
    assert row[col(stock_rows, "On hand")] == 40 and row[col(stock_rows, "Status")] == "Has expired stock"
    assert "10 · " in row[col(stock_rows, "Dates on the shelf")] and "30 · " in row[col(stock_rows, "Dates on the shelf")]
    count = book["Count"]
    milk_rows = [r for r in count[1:] if r[col(count, "Product")] == "Dutch Lady UHT milk strawberry 180ml"]
    assert [(r[col(count, "On the books")], r[col(count, "Lot")]) for r in milk_rows] == [(10, "L1"), (30, "L2")]
    assert all(shelfsheet.text_of(r[col(count, "Counted")]) == "" for r in milk_rows)          # yours to fill
    assert {r[col(count, "Where")] for r in count[1:] if r and r[col(count, "Product")] == "Coca-Cola can 330ml"} == {
        "Fridge", "Shelf A"}


def test_counted_and_corrected_in_excel_then_applied_once(conn, cfg):
    fridge, shelf, milk, coke = shop(conn)
    book = sheets(workbook.build(conn, cfg))
    count, st = book["Count"], book["Stock"]
    c_prod, c_where, c_count, c_exp = (col(count, n) for n in ("Product", "Where", "Counted", "Expiry"))
    for r in count[1:]:
        if not r or len(r) <= c_count:
            continue
        if r[c_prod] == "Dutch Lady UHT milk strawberry 180ml" and r[col(count, "Lot")] == "L1":
            r[c_count] = 0                                        # thrown away
        if r[c_prod] == "Coca-Cola can 330ml" and r[c_where] == "Fridge":
            r[c_count] = 20                                       # four missing
        if r[c_prod] == "Coca-Cola can 330ml" and r[c_where] == "Shelf A":
            r[c_where] = "Fridge"                                 # moved, not counted
    found = [""] * len(count[0])
    found[c_prod], found[c_where], found[c_exp], found[c_count] = "Sprite bottle 1.5L", "Shelf A", "01/12/2026", 6
    count.append(found)
    coke_row = next(r for r in st if r[col(st, "Product")] == "Coca-Cola can 330ml")
    coke_row[col(st, "Cost per unit")] = 0.45
    coke_row[col(st, "Must-have")] = "no"                    # it was a must-have
    new = [""] * len(st[0])
    new[col(st, "Product")], new[col(st, "Case size")] = "Bayon water 500ml", 24
    st.append(new)
    data = rewrite(book)

    plan = workbook.read(conn, cfg, data, "s.xlsx")
    assert not plan["problems"]
    counts = {(c["name"], c["place"]): (c["before"], c["after"]) for c in plan["counts"]}
    assert counts[("Dutch Lady UHT milk strawberry 180ml", "Fridge")] == (40, 30)
    assert counts[("Sprite bottle 1.5L", "Shelf A")] == (0, 6)
    # the coke on the shelf moved to the fridge on this sheet (a correction), and the fridge was counted: 4 missing
    assert counts[("Coca-Cola can 330ml", "Fridge")] == (72, 68) and ("Coca-Cola can 330ml", "Shelf A") not in counts
    assert [b["what"] for b in plan["batch_edits"]] == ["moved from Shelf A to Fridge"]
    assert {c["field"] for c in plan["products"][0]["changes"]} == {"Cost per unit", "Must-have"}
    assert plan["new_products"] == [{"row": len(st), "name": "Bayon water 500ml"}]
    stock.record(conn, milk, 5, "delivery", expiry_date=on(90), location_id=fridge, at=day(0))   # after the download
    conn.commit()
    done = workbook.apply(conn, cfg, data, "s.xlsx", by="sheet")
    conn.commit()
    assert done["counted"] == 3 and done["created"] == ["Bayon water 500ml"] and done["products"] == 1
    left = sorted((b.left, b.expiry, b.location_id) for b in batches.open_batches(conn, milk))
    assert left == sorted([(30, on(120), fridge), (5, on(90), fridge)])   # the expired 10 gone; today's delivery kept
    assert sum(b.left for b in batches.open_batches(conn, coke) if b.location_id == fridge) == 68
    assert tuple(conn.execute("SELECT unit_cost, is_critical FROM products WHERE id = ?", (coke,)).fetchone()) == (0.45, 0)
    again = workbook.read(conn, cfg, data, "s.xlsx")
    assert again["problems"] and "already used" in again["problems"][0]["text"]
    try:
        workbook.apply(conn, cfg, data, "s.xlsx", by="sheet")
        raise AssertionError("a sheet is used once")
    except ValueError as exc:
        assert "can't be read" in str(exc)


def test_rows_it_cant_read_are_listed_and_nothing_is_saved(conn, cfg):
    shop(conn)
    book = sheets(workbook.build(conn, cfg))
    count = book["Count"]
    c_prod, c_where, c_count = (col(count, n) for n in ("Product", "Where", "Counted"))
    count[1][c_count] = "a dozen"
    count[2][c_where] = "Kitchen"
    bad = [""] * len(count[0])
    bad[c_prod], bad[c_count] = "Unicorn milk", 3
    count.append(bad)
    data = rewrite(book)
    plan = workbook.read(conn, cfg, data, "s.xlsx")
    texts = " | ".join(p["text"] for p in plan["problems"])
    assert "a dozen" in texts and "Kitchen" in texts and "Unicorn milk" in texts
    before = stock.on_hand(conn)
    try:
        workbook.apply(conn, cfg, data, "s.xlsx", by="sheet")
        raise AssertionError("refused")
    except ValueError:
        pass
    assert stock.on_hand(conn) == before


def test_sales_since_the_download_are_pointed_out(conn, cfg):
    fridge, shelf, milk, coke = shop(conn)
    book = sheets(workbook.build(conn, cfg))
    count = book["Count"]
    for r in count[1:]:
        if r and r[col(count, "Product")] == "Coca-Cola can 330ml" and r[col(count, "Where")] == "Fridge":
            r[col(count, "Counted")] = 24
    clock.freeze(clock.now() + timedelta(hours=2))
    stock.record(conn, coke, -3, "sale")
    conn.commit()
    plan = workbook.read(conn, cfg, rewrite(book), "s.xlsx")
    assert plan["since"] == [{"name": "Coca-Cola can 330ml", "sold": 3}]


def test_the_page_the_download_and_the_upload(cfg, conn, stub_encoder, monkeypatch):
    fridge, shelf, milk, coke = shop(conn)
    with client_for(cfg, monkeypatch) as c:
        page = c.get("/spreadsheet").text
        assert "Stock in Excel" in page and 'href="/stock.xlsx"' in page and ">Stock in Excel<" in page
        r = c.get("/stock.xlsx")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
        book = sheets(r.content)
        count = book["Count"]
        for row in count[1:]:
            if row and row[col(count, "Product")] == "Coca-Cola can 330ml" and row[col(count, "Where")] == "Shelf A":
                row[col(count, "Counted")] = 40
        data = rewrite(book)
        files = {"file": ("stock.xlsx", data, "application/octet-stream")}
        plan = c.post("/api/workbook/check", files=files).json()
        assert plan["counts"][0]["after"] == 40 and "_counts" not in plan
        assert c.post("/api/workbook/apply", files=files).json()["counted"] == 1
        assert c.post("/api/workbook/check", files={"file": ("x.csv", b"a,b\n1,2", "text/csv")}).status_code == 400
    assert sum(b.left for b in batches.open_batches(db.connect(cfg.db_path), coke) if b.location_id == shelf) == 40
