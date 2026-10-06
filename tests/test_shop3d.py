"""The shop in 3D: racks laid out on the floor by what they are (or where the owner put them), and the one
move it suggests from the sales in the ledger."""

from datetime import timedelta

from luydek import clock
from luydek.services import locations, shop3d, stock, stockmap
from tests.conftest import pid
from tests.test_shelf import client_for


def day(n):
    return clock.now() - timedelta(days=n)


def footprint(p):
    f = p["floor"]
    hw, hd = (f["d"] / 2, f["w"] / 2) if f["turn"] % 2 else (f["w"] / 2, f["d"] / 2)
    return f["x"] - hw, f["z"] - hd, f["x"] + hw, f["z"] + hd


def test_racks_stand_by_what_they_are_and_never_on_each_other(conn, cfg):
    for name in ("Counter", "Shelf A", "Shelf B", "Shelf C", "Shelf D", "Shelf E", "Fridge", "Back room"):
        locations.add(conn, name)
    m = stockmap.stock_map(conn, cfg)
    by = {p["name"]: p for p in m["places"]}
    z = {n: by[n]["floor"]["z"] for n in by}
    assert z["Fridge"] < z["Shelf A"] < z["Shelf E"] < z["Counter"]          # back wall, rows, the counter in front
    assert z["Back room"] == z["Fridge"] and z["Shelf A"] == z["Shelf D"] < z["Shelf E"]   # four to a row
    boxes = [footprint(p) for p in m["places"]]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1]
    fl = m["floor"]
    assert all(fl["x0"] <= b[0] and b[2] <= fl["x1"] and fl["z0"] <= b[1] and b[3] <= fl["z1"] for b in boxes)
    shelf = by["Shelf A"]
    assert len(shelf["floor"]["boards"]) == 4 and shelf["floor"]["boards"][0] > shelf["floor"]["boards"][-1]
    assert by["Counter"]["floor"]["boards"] == [shop3d.BASE["counter"]]      # products stand on the counter top


def test_where_the_owner_put_them_is_kept(conn, cfg):
    shelf = locations.add(conn, "Shelf A")
    milk = pid(conn, "dutchlady-choc")
    stockmap.save_shelf(conn, shelf, [{"product": milk, "level": 1, "width": 9}])
    assert shop3d.save(conn, [{"id": shelf, "x": 4.5, "z": 999, "turn": 5}]) == 1
    p = next(p for p in stockmap.stock_map(conn, cfg)["places"] if p["id"] == shelf)
    f = p["floor"]
    assert (f["x"], f["z"], f["turn"], f["auto"]) == (4.5, 60.0, 1, False)   # kept on a sane floor, a quarter turn
    assert f["w"] == round(9 * shop3d.FACING + 0.1, 3)                       # as wide as what stands on it
    lo, z0, hi, z1 = footprint(p)
    assert round(hi - lo, 3) == f["d"] and round(z1 - z0, 3) == f["w"]       # turned: its width runs along z
    shop3d.save(conn, [{"id": shelf, "auto": True}])
    assert next(p for p in stockmap.stock_map(conn, cfg)["places"] if p["id"] == shelf)["floor"]["auto"] is True


def sell(conn, sku, n):
    product = pid(conn, sku)
    stock.record(conn, product, n + 20, "delivery", at=day(30))
    for i in range(n):
        stock.record(conn, product, -1, "sale", at=day(1 + i % 20))
    return product


def test_a_best_seller_out_of_reach_swaps_with_a_slow_seller_at_hand(conn, cfg):
    counter, shelf_a, shelf_c = (locations.add(conn, n) for n in ("Counter", "Shelf A", "Shelf C"))
    sting, para, coke = sell(conn, "sting-330", 30), sell(conn, "para-500", 2), sell(conn, "coke-330", 12)
    stockmap.save_shelf(conn, shelf_c, [{"product": sting, "level": 4}, {"product": coke, "level": 2}])  # Sting at the bottom
    stockmap.save_shelf(conn, shelf_a, [{"product": para, "level": 2}])                                   # hand height
    stockmap.save_shelf(conn, counter, [])
    conn.commit()
    move = stockmap.stock_map(conn, cfg)["move"]
    assert (move["from"]["product"], move["to"]["product"]) == (sting, para)
    assert (move["from"]["place"], move["from"]["level"], move["to"]["place"]) == (shelf_c, 4, shelf_a)
    assert (move["sold_from"], move["sold_to"]) == (30, 2) and "bottom level of Shelf C" in move["why"]


def test_fridge_stock_swaps_only_with_fridge_stock(conn, cfg):
    shelf, fridge = locations.add(conn, "Shelf A"), locations.add(conn, "Fridge")
    sting, para, coke = sell(conn, "sting-330", 30), sell(conn, "para-500", 1), sell(conn, "coke-330", 2)
    stockmap.save_shelf(conn, fridge, [{"product": coke, "level": 1}, {"product": sting, "level": 4}], levels=4)
    stockmap.save_shelf(conn, shelf, [{"product": para, "level": 2}])
    move = stockmap.stock_map(conn, cfg)["move"]
    assert (move["from"]["product"], move["to"]["product"]) == (sting, coke)   # not the paracetamol on the shelf
    assert "a spot at hand height on Fridge" in move["why"]


def test_no_move_without_sales_clear_enough(conn, cfg):
    shelf = locations.add(conn, "Shelf A")
    sting, para = sell(conn, "sting-330", 5), sell(conn, "para-500", 2)   # 5 sold: not a best seller yet
    stockmap.save_shelf(conn, shelf, [{"product": sting, "level": 4}, {"product": para, "level": 2}])
    assert stockmap.stock_map(conn, cfg)["move"] is None


def test_the_3d_shop_on_the_map_page(cfg, conn, stub_encoder, monkeypatch):
    shelf = locations.add(conn, "Shelf A")
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get("/map?view=3d").text
        assert 'data-view="3d"' in page and 'id="shop3d"' in page and "shop3d.js" in page
        m = c.post("/api/map/floor", json={"places": [{"id": shelf, "x": 2, "z": 3, "turn": 2}]}).json()
        f = next(p for p in m["places"] if p["id"] == shelf)["floor"]
        assert (f["x"], f["z"], f["turn"]) == (2.0, 3.0, 2) and "move" in m and m["floor"]["facing"] == shop3d.FACING
        assert c.post("/api/map/floor", json={"places": [{"id": shelf}]}).status_code == 400
        assert c.get("/static/shop3d.js").status_code == 200
