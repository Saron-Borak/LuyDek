"""Shelves from the owner's own spreadsheet: reading .xlsx and .csv, guessing the columns, matching the
products (never dropping one), putting them on their shelves, counting the stock, and the way back out."""

import io
import zipfile
from xml.sax.saxutils import escape

import pytest

from luydek import db
from luydek.services import batches, shelfsheet, stock, stockmap
from tests.conftest import pid
from tests.test_shelf import client_for


def xlsx(sheets: dict[str, list[list]]) -> bytes:
    """A small Excel workbook, written the way Excel writes one (shared strings, numbers, a skipped row)."""
    shared: list[str] = []
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        names = list(sheets)
        z.writestr("xl/workbook.xml", '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names))
                   + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml" '
                             'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>'
                             for i in range(len(names))) + "</Relationships>")
        for i, n in enumerate(names):
            rows = []
            for r, row in enumerate(sheets[n], start=1):
                if row is None:
                    continue                                   # Excel leaves empty rows out
                cells = []
                for c, v in enumerate(row):
                    ref = f"{chr(65 + c)}{r}"
                    if v is None or v == "":
                        continue
                    if isinstance(v, (int, float)):
                        cells.append(f'<c r="{ref}"><v>{v}</v></c>')
                    elif isinstance(v, str) and v.startswith("inline:"):
                        cells.append(f'<c r="{ref}" t="inlineStr"><is><t>{escape(v[7:])}</t></is></c>')
                    else:
                        if v not in shared:
                            shared.append(v)
                        cells.append(f'<c r="{ref}" t="s"><v>{shared.index(v)}</v></c>')
                rows.append(f'<row r="{r}">{"".join(cells)}</row>')
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", '<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
                       f'spreadsheetml/2006/main"><sheetData>{"".join(rows)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", '<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   + "".join(f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
    return buf.getvalue()


def test_an_excel_workbook_is_read_with_no_spreadsheet_package():
    data = xlsx({"Notes": [["hello"]],
                 "Stock": [["My shop's stock, October"], None, ["Item", "Qty", "Exp"],
                           ["Coca-Cola can 330ml", 24, 46327], ["inline:ស្ទីង", 6.5, "31/12/2026"]]})
    rows, sheets = shelfsheet.read(data, "stock.xlsx", "Stock")
    assert sheets == ["Notes", "Stock"]
    assert rows[1] == [] and rows[2] == ["Item", "Qty", "Exp"]               # the empty row is kept: row numbers match Excel
    assert rows[3] == ["Coca-Cola can 330ml", 24.0, 46327.0] and rows[4][0] == "ស្ទីង"
    assert shelfsheet.read(data, "stock.xlsx")[0] == [["hello"]]            # the first sheet unless told
    with pytest.raises(ValueError, match="xlsx"):
        shelfsheet.read(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100, "old.xls")
    with pytest.raises(ValueError):
        shelfsheet.read(b"PK\x03\x04 not really", "x.xlsx")
    rows, sheets = shelfsheet.read("Product;Shelf\r\nSting;A\r\n".encode("utf-8-sig"), "x.csv")
    assert rows == [["Product", "Shelf"], ["Sting", "A"]] and sheets == []


def test_the_columns_are_guessed_from_their_headings():
    heading, m = shelfsheet.guess([["Stock count 1 Oct"], [], ["No.", "Item name", "Stock location", "Row", "On hand",
                                                               "Best before", "Lot no", "Barcode", "Unit price"]])
    assert heading == 2
    assert (m["product"], m["shelf"], m["level"], m["quantity"], m["expiry"], m["lot"], m["barcode"]) == (1, 2, 3, 4, 5, 6, 7)
    assert m["position"] is None and m["facings"] is None
    _, km = shelfsheet.guess([["ឈ្មោះទំនិញ", "ធ្នើ", "ជាន់", "ចំនួន", "ថ្ងៃផុតកំណត់"]])
    assert (km["product"], km["shelf"], km["level"], km["quantity"], km["expiry"]) == (0, 1, 2, 3, 4)


def test_cells_are_read_as_shops_write_them():
    d = shelfsheet.parse_date
    assert [d("31/12/2026"), d("2026-12-31"), d("31-12-26"), d("12/31/2026"), d("12/2026"), d("Dec 2026"),
            d("31 Dec 2026"), d("EXP: 05.03.2027"), d(46387.0), d("46387"), d("soon"), d("")] == [
        "2026-12-31", "2026-12-31", "2026-12-31", "2026-12-31", "2026-12-31", "2026-12-31",
        "2026-12-31", "2027-03-05", "2026-12-31", "2026-12-31", None, None]
    assert [shelfsheet.parse_level(v) for v in (2.0, "Level 3", "L1", "Top", "bottom shelf", "កណ្តាល", "x", "")] == [
        2, 3, 1, "top", "bottom", "middle", None, None]
    assert [shelfsheet.parse_quantity(v) for v in (24.0, "1,200", "12 pcs", "-3", "", "n/a", 0, 0.0, "0")] == [
        24, 1200, 12, None, None, None, 0, 0, 0]                          # 0 is a count, not a blank
    assert [shelfsheet.text_of(v) for v in (0, 12.0, 12.5, None)] == ["0", "12", "12.5", ""]


def table():
    return [["Shelf", "Level", "Position", "Product", "Barcode", "Qty", "Expiry", "Lot"],
            ["Drinks fridge", 1, 2, "Sprite bottle 1.5L", "", 6, "15/11/2026", ""],
            ["", 1, 1, "Coca-Cola can 330ml", "", 24, "01/03/2027", "L77"],   # merged cell: the fridge
            ["", 1, 1, "Coca-Cola can 330ml", "", 12, "01/01/2027", ""],      # the same spot, another date
            ["", 2, "", "anything", "8851959132012", 10, "", ""],             # known by its barcode
            ["Shelf A", "bottom", "", "Paracetamol 500 tabs", "", 30, "06/2027", ""],   # a close name
            ["", "top", "", "Bayon Water 500ml", "", 0, "", ""],              # a new product, none on the shelf
            ["", "", "", "", "", 99, "", ""],                                 # a sum: no product
            ["", "", "", "Total", "", 99, "", ""],                            # nor this one
            ["Shelf A", 2, "", "Lay's classic 50g", "", "lots", "", ""]]


def test_a_sheet_puts_products_on_their_shelves_and_counts_them(conn):
    sting = pid(conn, "sting-330")
    conn.execute("UPDATE product_packs SET barcode = '8851959132012' WHERE product_id = ?", (sting,))
    heading, mapping = shelfsheet.guess(table())
    got = shelfsheet.interpret(conn, table(), heading, mapping)
    rows = got["rows"]
    assert got["skipped"] == 2 and len(rows) == 7
    assert [r["shelf"] for r in rows] == ["Drinks fridge"] * 4 + ["Shelf A"] * 3   # the places above, filled down
    assert (rows[0]["how"], rows[3]["how"], rows[3]["product"]) == ("name", "barcode", sting)
    para = rows[4]
    assert para["how"] == "close" and para["product"] == pid(conn, "para-500") and para["expiry"] == "2027-06-30"
    assert rows[5]["product"] is None                                     # unknown: asked about, still listed
    assert rows[6]["problems"] == ["quantity “lots” not understood"] and rows[6]["quantity"] is None
    for r in rows:
        if r["product"] is None:
            r["new_name"] = r["name"]
    done = shelfsheet.save(conn, rows, count_stock=True)
    assert done["created"] == ["Bayon Water 500ml"] and done["places"] == 2 and done["spots"] == 6
    shelves = {p["name"]: p for p in stockmap.shelves(conn)}
    assert shelves["Drinks fridge"]["kind"] == "fridge" and shelves["Drinks fridge"]["levels"] == 2  # as many as written
    spots = stockmap.facings(conn)
    fridge, shelf_a = shelves["Drinks fridge"]["id"], shelves["Shelf A"]["id"]
    coke, sprite, water = pid(conn, "coke-330"), pid(conn, "sprite-1500"), conn.execute(
        "SELECT id FROM products WHERE name_en = 'Bayon Water 500ml'").fetchone()[0]
    assert [(f["product"], f["level"]) for f in spots[fridge]] == [(coke, 1), (sprite, 1), (sting, 2)]
    assert [(f["product"], f["level"]) for f in spots[shelf_a]] == [(water, 1), (pid(conn, "lays-50"), 2),
                                                                     (pid(conn, "para-500"), 4)]  # top, 2, bottom
    on = stock.on_hand(conn)
    assert (on[coke], on[sprite], on[sting], on[pid(conn, "para-500")], on.get(water, 0)) == (36, 6, 10, 30, 0)
    assert pid(conn, "lays-50") not in on or on[pid(conn, "lays-50")] == 0                  # not understood: not counted
    cokes = sorted((b.expiry, b.left, b.location_id, b.lot_no) for b in batches.open_batches(conn, coke))
    assert cokes == [("2027-01-01", 12, fridge, None), ("2027-03-01", 24, fridge, "L77")]
    again = shelfsheet.interpret(conn, table(), heading, mapping)["rows"]
    assert (again[4]["product"], again[4]["how"]) == (pid(conn, "para-500"), "name")    # its name in the sheet, learned
    # The same shelf again, all on one level: what is drawn for it is replaced (left to right in the order of
    # the rows), the other shelf left alone.
    again = [r for r in rows if r["shelf"] == "Shelf A"]
    for r in again:
        r["level"] = 1
    shelfsheet.save(conn, again, count_stock=False)
    assert [f["product"] for f in stockmap.facings(conn)[shelf_a]] == [pid(conn, "para-500"), water, pid(conn, "lays-50")]
    assert len(stockmap.facings(conn)[fridge]) == 3


def test_names_are_matched_to_products_still_in_the_list(conn):
    coke = pid(conn, "coke-330")
    conn.execute("UPDATE products SET name_en = 'Coke can' WHERE id = ?", (coke,))      # renamed: no alias for it
    old = pid(conn, "ichitan-420")
    conn.execute("UPDATE products SET active = 0 WHERE id = ?", (old,))               # taken off the list
    t = [["Product"], ["coke CAN"], ["Ichitan green tea 420ml"], ["Ichitan green tea 420"]]
    rows = shelfsheet.interpret(conn, t, *shelfsheet.guess(t))["rows"]
    assert (rows[0]["product"], rows[0]["how"]) == (coke, "name")
    assert rows[1]["product"] is None and old not in rows[1]["offer"] and old not in rows[2]["offer"]


def test_a_sheet_of_one_shelf_counts_that_shelf_only(conn):
    coke = pid(conn, "coke-330")
    back = stockmap.add_place(conn, "Back room")
    stock.record(conn, coke, 48, "delivery", location_id=back, expiry_date="2027-05-01")
    stock.record(conn, coke, 10, "delivery", expiry_date="2026-12-01")      # no place yet: it goes to its first spot
    t = [["Shelf", "Product", "Qty", "Expiry"], ["Fridge", "Coca-Cola can 330ml", 7, "01/02/2027"]]
    heading, mapping = shelfsheet.guess(t)
    shelfsheet.save(conn, shelfsheet.interpret(conn, t, heading, mapping)["rows"], count_stock=True)
    fridge = next(p["id"] for p in stockmap.shelves(conn) if p["name"] == "Fridge")
    left = sorted((b.location_id or 0, b.left, b.expiry) for b in batches.open_batches(conn, coke))
    assert left == sorted([(back, 48, "2027-05-01"), (fridge, 7, "2027-02-01")])   # the back room kept, the fridge counted
    t = [["Shelf", "Product", "Qty"], ["Fridge", "Coca-Cola can 330ml", 0]]              # counted again: none left
    shelfsheet.save(conn, shelfsheet.interpret(conn, t, *shelfsheet.guess(t))["rows"], count_stock=True)
    assert stock.on_hand(conn)[coke] == 48


def test_with_no_levels_a_shelf_is_shared_out_over_its_levels(conn):
    names = ["Coca-Cola can 330ml", "Sprite bottle 1.5L", "Sting energy drink 330ml", "Lay's classic 50g",
             "Mama instant noodles pork", "Oishi green tea 380ml", "Ichitan green tea 420ml", "Dumex Dugro baby formula 400g"]
    t = [["Product"]] + [[n] for n in names]
    heading, mapping = shelfsheet.guess(t)
    rows = shelfsheet.interpret(conn, t, heading, mapping, shelf_for_all="Shelf Z")["rows"]
    shelfsheet.save(conn, rows, count_stock=False)
    z = next(p for p in stockmap.shelves(conn) if p["name"] == "Shelf Z")
    assert [f["level"] for f in stockmap.facings(conn)[z["id"]]] == [1, 1, 2, 2, 3, 3, 4, 4]


def test_the_shelves_go_out_as_a_sheet_and_come_back_the_same(conn):
    heading, mapping = shelfsheet.guess(table())
    rows = shelfsheet.interpret(conn, table(), heading, mapping)["rows"]
    for r in rows:
        r["new_name"] = r["name"] if r["product"] is None else None
    shelfsheet.save(conn, rows, count_stock=True)
    before = (stockmap.facings(conn), stock.on_hand(conn))
    text = shelfsheet.export(conn)
    assert text.startswith("﻿Shelf,Level (1 = top)")
    back, _ = shelfsheet.read(text.encode("utf-8"), "luydek-shelves.csv")
    heading, mapping = shelfsheet.guess(back)
    assert mapping["product"] == 3 and mapping["shelf"] == 0 and mapping["level"] == 1 and mapping["expiry"] == 8
    again = shelfsheet.interpret(conn, back, heading, mapping)["rows"]
    assert all(r["product"] is not None for r in again) and not any(r["problems"] for r in again)
    assert not any(r["shelf"] for r in again if r["product"] == pid(conn, "vitc-1000"))   # on no shelf: no place filled in
    shelfsheet.save(conn, again, count_stock=True)
    assert (stockmap.facings(conn), stock.on_hand(conn)) == before


def test_the_sheet_on_the_map_page(cfg, conn, stub_encoder, monkeypatch):
    csv_text = "Item,Where,Qty,Exp\r\nCoca-Cola can 330ml,Shelf A,12,31/12/2026\r\nNew Thing,Shelf A,3,\r\n"
    with client_for(cfg, monkeypatch) as c:
        got = c.post("/api/map/sheet/read", files={"file": ("stock.csv", csv_text.encode("utf-8-sig"), "text/csv")}).json()
        assert got["mapping"]["product"] == 0 and got["mapping"]["shelf"] == 1 and len(got["rows"]) == 2
        mapping = {**got["mapping"], "shelf": None}
        checked = c.post("/api/map/sheet/check", json={"table": got["table"], "heading": got["heading"], "mapping": mapping,
                                                       "shelf_for_all": "Counter"}).json()
        assert [r["shelf"] for r in checked["rows"]] == ["Counter", "Counter"]
        rows = [{**r, "new_name": r["name"] if r["product"] is None else None} for r in got["rows"]]
        saved = c.post("/api/map/sheet/save", json={"rows": rows, "count_stock": True}).json()
        assert saved["done"]["counted"] == 2 and saved["done"]["created"] == ["New Thing"]
        assert any(p["name"] == "Shelf A" and len(p["facings"]) == 2 for p in saved["places"])
        bad = c.post("/api/map/sheet/save", json={"rows": [{"row": 2, "shelf": "Shelf A"}], "count_stock": False})
        assert bad.status_code == 400 and "row 2" in bad.json()["detail"]
        out = c.get("/map/shelves.csv")
        assert out.status_code == 200 and "attachment" in out.headers["content-disposition"]
        assert "New Thing" in out.content.decode("utf-8-sig")
        assert c.post("/api/map/sheet/read", files={"file": ("x.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "x")}).status_code == 400
    assert stock.on_hand(db.connect(cfg.db_path))[pid(conn, "coke-330")] == 12
