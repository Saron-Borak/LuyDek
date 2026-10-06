"""Stock by batch: first expired, first out; the shelf check; put-away; places; counts; stock alerts."""

from datetime import timedelta

from luydek import clock
from luydek.services import alerts, batches, locations, stock
from tests.conftest import pid


def day(n):
    """n days ago, at noon."""
    return clock.now().replace(hour=12) - timedelta(days=n)


def test_sales_take_from_the_batch_that_expires_first(conn):
    p = pid(conn, "dutchlady-choc")
    late = stock.record(conn, p, 24, "delivery", expiry_date="2027-03-01", at=day(20))
    soon = stock.record(conn, p, 12, "delivery", expiry_date="2026-11-01", at=day(10))  # arrived later, expires sooner
    stock.record(conn, p, -15, "sale", at=day(5))
    left = {b.id: b.left for b in batches.open_batches(conn, p)}
    assert soon not in left and left == {late: 21}   # the 12 expiring sooner sold first, then 3 of the others
    assert batches.earliest_expiry(conn)[p] == "2027-03-01"


def test_a_removal_takes_from_its_own_batch_and_a_put_back_returns(conn):
    p = pid(conn, "dutchlady-choc")
    a = stock.record(conn, p, 10, "delivery", expiry_date="2026-10-01", at=day(20))
    b = stock.record(conn, p, 10, "delivery", expiry_date="2026-12-01", at=day(10))
    assert batches.remove(conn, b, 4, reason="damaged") == 4    # four of the later batch were crushed
    stock.adjust(conn, p, "put_back", 1)                        # and one sale went back on the shelf
    stock.record(conn, p, -3, "sale")
    left = {x.id: x.left for x in batches.open_batches(conn, p)}
    assert left == {a: 8, b: 6}
    assert sum(left.values()) == stock.on_hand(conn)[p]


def test_selling_more_than_came_in_is_owed_by_the_next_delivery(conn):
    p = pid(conn, "sting-330")
    stock.record(conn, p, 5, "opening", at=day(9))
    stock.record(conn, p, -8, "sale", at=day(5))                # 3 sold that nobody knew were there
    s = batches.replay(conn, [p])[p]
    assert s.deficit == 3 and not s.open
    new = stock.record(conn, p, 24, "delivery", expiry_date="2027-01-01", at=day(1))
    s = batches.replay(conn, [p])[p]
    assert s.deficit == 0 and [(b.id, b.left) for b in s.open] == [(new, 21)] == [(new, stock.on_hand(conn)[p])]


def test_undated_batches_go_in_the_order_they_came_and_a_shelf_life_gives_an_estimate(conn):
    p = pid(conn, "lays-50")
    first = stock.record(conn, p, 6, "delivery", at=day(30))
    second = stock.record(conn, p, 6, "delivery", at=day(3))
    stock.record(conn, p, -4, "sale")
    assert [(b.id, b.left) for b in batches.open_batches(conn, p)] == [(first, 2), (second, 6)]
    conn.execute("UPDATE products SET shelf_life_days = 90 WHERE id = ?", (p,))
    est = batches.open_batches(conn, p)[0]
    assert est.expiry is None and est.est_expiry == (day(30).date() + timedelta(days=90)).isoformat()


def test_the_shelf_check_walks_place_by_place(conn):
    fridge, shelf = locations.add(conn, "Fridge"), locations.add(conn, "Shelf B")
    milk, vit, ors = pid(conn, "dutchlady-choc"), pid(conn, "vitc-1000"), pid(conn, "ors-sachet")
    today = clock.today()
    expired = stock.record(conn, milk, 6, "delivery", expiry_date=(today - timedelta(days=2)).isoformat(),
                           location_id=fridge, at=day(30))
    slow = stock.record(conn, vit, 10, "delivery", expiry_date=(today + timedelta(days=12)).isoformat(),
                        location_id=shelf, at=day(60))
    stock.record(conn, vit, -1, "sale", at=day(3))           # 1 in four weeks: 9 won't go in 12 days
    stock.record(conn, ors, 20, "opening", at=day(29))
    for d in range(2, 30, 2):                                # one every other day, from the undated stock
        stock.record(conn, ors, -1, "sale", at=day(d))
    sooner = stock.record(conn, ors, 4, "delivery", expiry_date=(today + timedelta(days=20)).isoformat())
    tasks = batches.shelf_check(conn, within_days=30)
    assert [(t.place, t.product["id"], t.action, t.batch.id, t.batch.left) for t in tasks] == [
        ("Fridge", milk, "remove", expired, 6),              # walking order: the fridge first...
        ("Shelf B", vit, "clear", slow, 9),                  # ...then Shelf B...
        (None, ors, "front", sooner, 4),                     # ...then what has no place yet
    ]
    assert batches.remove(conn, expired) == 6
    assert [t.action for t in batches.shelf_check(conn)] == ["clear", "front"]


def test_put_away_says_in_front_or_behind(conn):
    fridge = locations.add(conn, "Fridge")
    p = pid(conn, "dutchlady-choc")
    locations.set_home(conn, p, fridge)
    stock.record(conn, p, 6, "delivery", expiry_date="2026-10-12", at=day(10))
    later = stock.record(conn, p, 24, "delivery", expiry_date="2027-01-20")
    sooner = stock.record(conn, p, 12, "delivery", expiry_date="2026-10-05")
    out = {x["batch_id"]: x for x in batches.put_away(conn, [later, sooner])}
    # Both arrive together: each is placed against the 6 already there, not against the other.
    assert out[later]["how"] == "behind" and out[later]["place"] == "Fridge" and out[later]["old_expiry"] == "2026-10-12"
    assert out[sooner]["how"] == "front" and out[sooner]["old_expiry"] == "2026-10-12" and out[sooner]["old_left"] == 6
    first = stock.record(conn, pid(conn, "sting-330"), 24, "delivery")
    assert batches.put_away(conn, [first])[0]["how"] == "on"


def test_places_and_home_place(conn):
    assert locations.add_suggested(conn) == len(locations.SUGGESTED)
    assert locations.add_suggested(conn) == 0
    shelf = locations.add(conn, "  shelf   a ")             # same place, any spacing or case
    assert shelf == next(r["id"] for r in locations.places(conn) if r["name"] == "Shelf A")
    p = pid(conn, "sting-330")
    b = stock.record(conn, p, 24, "delivery")
    locations.set_home(conn, p, shelf)                       # batches with no place yet follow
    assert batches.open_batches(conn, p)[0].location_id == shelf
    assert stock.record(conn, p, 6, "delivery") != b and batches.open_batches(conn, p)[-1].location_id == shelf
    locations.remove(conn, shelf)
    assert conn.execute("SELECT location_id FROM products WHERE id = ?", (p,)).fetchone()[0] is None


def test_a_count_corrects_the_stock_and_minds_sales_still_to_check(conn, cfg):
    from tests.test_bot import pending_sale

    p = pid(conn, "ichitan-420")
    stock.record(conn, p, 10, "opening")
    assert stock.count(conn, p, 10, "spot_check") == 0       # matched: logged, nothing changes
    pending_sale(conn, cfg)                                  # one Ichitan sold, not checked yet
    assert stock.count(conn, p, 9, "spot_check") == 0        # the shelf shows 9, and that's right
    stock.count(conn, p, 7, "spot_check")                    # two more gone than the camera saw
    assert stock.on_hand(conn)[p] == 8                       # 7 on the shelf + 1 to be taken when checked
    rows = conn.execute("SELECT counted, expected FROM stock_counts ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(10, 10), (9, 9), (7, 9)]


def test_stock_alerts_go_out_once_until_back_in_stock(conn):
    p = pid(conn, "sting-330")
    stock.record(conn, p, 10, "opening", at=day(8))
    for d in range(7):
        stock.record(conn, p, -1, "sale", at=day(d))
    assert [r["alert"] for r in alerts.new_stock_alerts(conn) if r["id"] == p] == []   # 3 left, 1 a day: fine
    stock.record(conn, p, -2, "sale")
    assert [r["alert"] for r in alerts.new_stock_alerts(conn) if r["id"] == p] == ["low"]
    assert [r for r in alerts.new_stock_alerts(conn) if r["id"] == p] == []            # told once
    stock.record(conn, p, -1, "sale")
    assert [r["alert"] for r in alerts.new_stock_alerts(conn) if r["id"] == p] == ["out"]
    stock.record(conn, p, 24, "delivery")
    assert [r for r in alerts.new_stock_alerts(conn) if r["id"] == p] == []            # back: forgotten
    stock.record(conn, p, -24, "sale")
    assert [r["alert"] for r in alerts.new_stock_alerts(conn) if r["id"] == p] == ["out"]


def test_spot_checks_pick_what_decides_orders_and_rest_a_week(conn):
    sting, coke, lays = pid(conn, "sting-330"), pid(conn, "coke-330"), pid(conn, "lays-50")
    for p, n in ((sting, 5), (coke, 40), (lays, 30)):
        stock.record(conn, p, n, "opening", at=day(20))
    for d in range(14):
        stock.record(conn, coke, -2, "sale", at=day(d))
        stock.record(conn, lays, -1, "sale", at=day(d))
        if d < 4:                                            # Sting: 1 left, at 4 a week
            stock.record(conn, sting, -1, "sale", at=day(d))
    picks = [r["id"] for r in alerts.spot_checks(conn, n=2)]
    assert picks == [sting, coke]                            # running low first, then the best seller
    stock.count(conn, sting, 1, "spot_check")
    assert [r["id"] for r in alerts.spot_checks(conn, n=2)] == [coke, lays]
