"""The stock map: each shelf face on, level by level; every batch in its spot, what has expired and
where money sleeps; and a shelf made from a photo of it."""

from datetime import timedelta

from luydek import clock, db
from luydek.recognition import detector, pipeline
from luydek.recognition.detector import Found
from luydek.services import batches, locations, shelfphoto, stock, stockmap
from tests.conftest import jpeg, mat_frame, pack_of, paint, pid
from tests.test_detector import FakeDetector
from tests.test_recognition import enroll_all
from tests.test_shelf import client_for


def day(n):
    return clock.now() - timedelta(days=n)


def test_shelves_have_levels_and_keep_what_stands_where(conn):
    for name in ("Counter", "Shelf A", "Fridge", "Back room"):
        locations.add(conn, name)
    got = {p["name"]: p for p in stockmap.shelves(conn)}
    assert [(got[n]["kind"], got[n]["levels"]) for n in ("Counter", "Shelf A", "Fridge", "Back room")] == [
        ("counter", 1), ("shelf", 4), ("fridge", 4), ("storage", 3)]
    shelf = got["Shelf A"]["id"]
    coke, sprite, milk = pid(conn, "coke-330"), pid(conn, "sprite-1500"), pid(conn, "dutchlady-choc")
    loose = stock.record(conn, milk, 6, "delivery")                       # no place yet
    stockmap.save_shelf(conn, shelf, [{"product": coke, "level": 1, "width": 3}, {"product": milk, "level": 4},
                                      {"product": sprite, "level": 1}, {"product": coke, "level": 9, "width": 40}])
    spots = stockmap.facings(conn)[shelf]
    assert [(f["product"], f["level"], f["width"]) for f in spots] == [
        (coke, 1, 3), (sprite, 1, 1), (milk, 4, 1), (coke, 4, stockmap.MAX_WIDTH)]   # kept on the shelf, left to right
    assert batches.get(conn, loose).location_id == shelf                  # its first spot is its home, and its stock's
    stockmap.save_shelf(conn, shelf, levels=2)                            # fewer levels: the bottom ones move up
    assert {f["level"] for f in stockmap.facings(conn)[shelf]} == {1, 2}
    assert next(p for p in stockmap.shelves(conn) if p["id"] == shelf)["levels"] == 2
    fridge = got["Fridge"]["id"]
    stockmap.reorder(conn, [fridge, shelf])
    assert [p["id"] for p in stockmap.shelves(conn)][:2] == [fridge, shelf]


def test_every_batch_is_in_its_spot_with_its_state(conn, cfg):
    fridge, shelf = locations.add(conn, "Fridge"), locations.add(conn, "Shelf A")
    milk, coke, para = pid(conn, "dutchlady-choc"), pid(conn, "coke-330"), pid(conn, "para-500")
    old = stock.record(conn, milk, 6, "delivery", location_id=fridge,
                       expiry_date=(clock.today() - timedelta(days=2)).isoformat(), at=day(30))
    soon = stock.record(conn, milk, 12, "delivery", location_id=fridge,
                        expiry_date=(clock.today() + timedelta(days=10)).isoformat(), at=day(5))
    stock.record(conn, milk, -1, "sale", at=day(1))
    stock.record(conn, para, 40, "delivery", location_id=shelf, at=day(80))   # never sold since: sleeping money
    stock.record(conn, coke, 2, "opening", at=day(20))
    stock.record(conn, coke, -2, "sale", at=day(2))                          # out of stock
    stockmap.save_shelf(conn, shelf, [{"product": para, "level": 2}, {"product": coke, "level": 2}])
    stockmap.save_shelf(conn, fridge, [{"product": coke, "level": 1}, {"product": milk, "level": 2}])
    loose = stock.record(conn, pid(conn, "sting-330"), 5, "delivery", at=day(3))  # no place yet
    conn.commit()
    m = stockmap.stock_map(conn, cfg)
    rows = {r["batch"]: r for r in m["batches"]}
    assert rows[old]["left"] == 5 and "expired" in rows[old]["status"]     # the sale came out of the oldest date
    assert "expiring" in rows[soon]["status"] and rows[soon]["place"] == fridge
    para_row = next(r for r in m["batches"] if r["product"] == para)
    assert "sleeping" in para_row["status"] and para_row["since_sale"] is None
    assert rows[loose]["place"] is None and m["unplaced"]["units"] == 5
    places = {p["id"]: p for p in m["places"]}
    assert (places[fridge]["expired"], places[fridge]["expiring"], places[fridge]["units"]) == (1, 1, 17)
    assert para_row["value"] > 0 and places[shelf]["sleeping_value"] == para_row["value"]   # 40 at cost
    assert [f["product"] for f in places[shelf]["facings"]] == [para, coke]
    gap = next(o for o in m["out"] if o["product"] == coke)
    assert gap["places"] == sorted([shelf, fridge]) and places[fridge]["out"] == [coke]      # a gap in each spot
    assert m["walk"][0]["place"] == fridge and m["walk"][0]["tasks"][0]["action"] == "remove"
    assert m["products"][coke]["name"] and m["default_levels"]["fridge"] == 4


def test_the_map_page_and_its_actions(cfg, conn, stub_encoder, monkeypatch):
    fridge = locations.add(conn, "Fridge")
    milk, coke = pid(conn, "dutchlady-choc"), pid(conn, "coke-330")
    old = stock.record(conn, milk, 6, "delivery", location_id=fridge,
                       expiry_date=(clock.today() - timedelta(days=2)).isoformat(), at=day(30))
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get(f"/map?q=dutch&show=expired&shelf={fridge}").text
        assert "Stock map" in page and 'id="map-data"' in page and 'value="dutch"' in page and "shelfmaker.js" in page
        added = c.post("/api/map/places", json={"name": "Shelf B", "kind": "shelf", "levels": 5}).json()
        shelf_b = added["added"]
        assert next(p for p in added["places"] if p["id"] == shelf_b)["levels"] == 5
        saved = c.post(f"/api/map/places/{shelf_b}/shelf", json={
            "name": "Shelf B (drinks)", "levels": 3,
            "items": [{"product": coke, "level": 1, "width": 2}, {"new_name": "Bayon Water 500ml", "level": 3}]}).json()
        b = next(p for p in saved["places"] if p["id"] == shelf_b)
        assert b["name"] == "Shelf B (drinks)" and b["levels"] == 3 and len(b["facings"]) == 2
        water = b["facings"][1]["product"]
        assert saved["products"][str(water)]["name"] == "Bayon Water 500ml"     # a new product, made on the shelf
        assert c.post(f"/api/map/places/{shelf_b}/shelf", json={"items": [{"level": 1}]}).status_code == 400
        assert c.post("/api/map/order", json={"ids": [shelf_b, fridge]}).json()["places"][0]["id"] == shelf_b
        moved = c.post(f"/api/map/batches/{old}/move", json={"location_id": shelf_b}).json()
        assert next(x for x in moved["batches"] if x["batch"] == old)["place"] == shelf_b
        gone = c.post(f"/api/map/batches/{old}/remove", json={"qty": 6, "reason": "expired"}).json()
        assert gone["removed"] == 6 and not any(x["batch"] == old for x in gone["batches"])
        assert c.post(f"/api/map/places/{fridge}/remove").status_code == 200
        assert c.post("/api/map/order", json={"ids": ["x"]}).status_code == 400
    c2 = db.connect(cfg.db_path)
    assert stock.on_hand(c2)[milk] == 0
    assert batches.get(c2, old).left == 0


def test_levels_are_what_stands_on_the_same_board():
    boxes = [(300, 40, 380, 200), (10, 60, 90, 205),          # top board: a short box beside a tall bottle
             (100, 90, 190, 198),
             (20, 300, 110, 470), (130, 380, 200, 465)]       # bottom board
    assert shelfphoto.levels(boxes) == [[1, 2, 0], [3, 4]]


def test_two_cokes_side_by_side_in_one_box_are_a_group_a_cap_on_a_label_is_not():
    left, right = Found((100, 100, 200, 400), .9), Found((210, 110, 300, 398), .9)
    assert detector.groups([Found((95, 95, 305, 405), .95), left, right]) == {0}
    cap, label = Found((130, 100, 170, 140), .6), Found((110, 300, 190, 400), .8)
    assert detector.groups([Found((100, 100, 200, 500), .92), cap, label]) == set()


def test_a_shelf_from_a_photo(cfg, conn, stub_encoder, monkeypatch):
    enroll_all(conn, cfg, skus=("coke-330", "sprite-1500", "dumex-400"), shots=5)
    conn.commit()
    frame = mat_frame(800, 560)
    spots = [("coke-330", 40, 60), ("coke-330", 140, 60), ("sprite-1500", 260, 60),     # top: two cokes, a sprite
             ("dumex-400", 60, 330)]                                                     # bottom: a tin
    for sku, x, y in spots:
        paint(frame, sku, x, y)
    found = [Found((x, y, x + 90, y + 130), .9) for _, x, y in spots] + [Found((520, 330, 610, 460), .8)]
    paint(frame, "ichitan-420", 520, 330)                     # one it doesn't know
    found.append(Found((30, 50, 360, 196), .95))              # the whole top level outlined as one: a group
    frame[200:222, 40:130] = 250                               # a price tag on the board's edge
    found.append(Found((40, 200, 130, 222), .7))
    rec = pipeline.get_recognizer(cfg)
    rec.finder = FakeDetector(found)
    try:
        with client_for(cfg, monkeypatch) as c:
            got = c.post("/api/map/photo", files={"image": ("s.jpg", jpeg(frame), "image/jpeg")}).json()
            top, bottom = got["levels"]
            assert [(s["product"], s["width"], s["sure"]) for s in top] == [
                (pid(conn, "coke-330"), 2, True), (pid(conn, "sprite-1500"), 1, True)]   # two cokes side by side: one spot
            assert bottom[0]["product"] == pid(conn, "dumex-400") and len(top[0]["crops"]) == 2
            assert not bottom[1]["sure"]                                     # asked about, never dropped
            assert len(got["aside"]) == 1 and got["aside"][0]["level"] == 0  # the price tag: set aside, in view
            items = [{"product": s["product"], "level": 1, "width": s["width"], "crops": s["crop_paths"]} for s in top]
            items.append({"new_name": "Ichitan Green Tea", "level": 2, "crops": bottom[1]["crop_paths"]})
            made = c.post("/api/map/places", json={"name": "Drinks shelf", "levels": 2, "items": items}).json()
            shelf = next(p for p in made["places"] if p["id"] == made["added"])
            assert [f["level"] for f in shelf["facings"]] == [1, 1, 2]
            tea = shelf["facings"][2]["product"]
            assert made["products"][str(tea)]["photo"] is not None           # its photo off the shelf is its first
    finally:
        rec.finder = None
    with db.session(cfg) as c2:
        assert c2.execute("SELECT COUNT(*) FROM gallery_images WHERE pack_id = ?",
                          (pack_of(c2, "coke-330"),)).fetchone()[0] == 5     # products with photos aren't taught more


def test_no_finder_no_shelf_photo(cfg, conn, stub_encoder, monkeypatch):
    with client_for(cfg, monkeypatch) as c:
        r = c.post("/api/map/photo", files={"image": ("s.jpg", jpeg(mat_frame()), "image/jpeg")})
        assert r.status_code == 400 and "spreadsheet" in r.json()["detail"]
