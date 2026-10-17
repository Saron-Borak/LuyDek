"""The stock first: one product, several dates, each batch shown on its own; the Today page leads with the
stock and what to do with it, and the sidebar puts the stock pages first."""

from datetime import timedelta

from luydek import clock
from luydek.services import batches, locations, stock
from tests.conftest import pid
from tests.test_shelf import client_for


def day(n):
    return clock.now().replace(hour=12) - timedelta(days=n)


def on(n):
    return (clock.today() + timedelta(days=n)).isoformat()


def strawberry(conn):
    """Dutch Lady on the fridge: 10 past their date, 6 expiring in 9 days, 30 good till next year, 4 with no date."""
    fridge = locations.add(conn, "Fridge")
    p = pid(conn, "dutchlady-straw")
    stock.record(conn, p, 30, "delivery", expiry_date=on(200), location_id=fridge, at=day(3))
    stock.record(conn, p, 10, "delivery", expiry_date=on(-2), location_id=fridge, at=day(40))
    stock.record(conn, p, 6, "delivery", expiry_date=on(9), location_id=fridge, at=day(20))
    stock.record(conn, p, 4, "delivery", at=day(1))
    stock.record(conn, pid(conn, "coke-330"), -3, "sale", at=day(1))
    conn.commit()
    return p, fridge


def test_each_batch_keeps_its_own_date_and_state(conn):
    p, fridge = strawberry(conn)
    lots = batches.lots(conn, 30)[p]
    # soonest first; the batch with no date sorts by the date its shelf life guesses, as sales do
    assert [(lot["left"], lot["state"]) for lot in lots] == [(10, "expired"), (6, "soon"), (4, "undated"), (30, "ok")]
    assert [lot["place"] for lot in lots] == ["Fridge", "Fridge", None, "Fridge"] and lots[0]["days_left"] == -2
    stock.record(conn, p, -12, "sale", at=day(0))           # the oldest date goes first: all 10, then 2 of the next
    assert [(lot["left"], lot["state"]) for lot in batches.lots(conn, 30)[p]][:2] == [(4, "soon"), (4, "undated")]


def test_home_leads_with_the_stock_and_what_to_do(cfg, conn, stub_encoder, monkeypatch):
    p, fridge = strawberry(conn)
    with client_for(cfg, monkeypatch) as c:
        home = c.get("/").text
        stock_list = c.get("/stock").text
        product = c.get(f"/catalog/{p}").text
    # Home: what the stock is worth, then one row per thing to do, the most urgent first
    assert home.index("Stock on your shelves") < home.index("To do now")
    assert "Past their date" in home
    # the expired part of the strawberry milk, not all of it, with its place
    assert "Take 10 Dutch Lady UHT milk strawberry 180ml off the shelf" in home and "Fridge" in home
    assert home.index("off the shelf") < home.index("Sell 6 Dutch Lady UHT milk strawberry 180ml soon")
    # Stock: the product says what part is past its date
    assert "PAST ITS DATE" in stock_list and "10 of 50 past their date" in stock_list
    # the product page: every delivery on the shelf, each with its own date
    assert product.count('class="lot lot-expired') == 1 and 'class="lot lot-soon' in product
    assert 'class="lot lot-undated' in product and "Deliveries on the shelf" in product
    # four places and the +: Home, Stock, Orders, More
    assert home.index(">Home<") < home.index(">Stock<") < home.index(">Orders<") < home.index(">More<")


def test_a_sales_check_waiting_is_a_slim_bar_above_the_stock(cfg, conn, stub_encoder, monkeypatch):
    conn.execute("INSERT INTO transactions (id, started_at, source, status) VALUES (1, ?, 'camera', 'pending')",
                 (clock.stamp(day(0)),))
    conn.execute("INSERT INTO transaction_items (txn_id, pack_id, count, confidence, tier, method, status) "
                 "VALUES (1, (SELECT id FROM product_packs WHERE product_id = ? LIMIT 1), 1, .8, 'medium', 'image', "
                 "'pending')", (pid(conn, "coke-330"),))
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get("/").text
        sales = c.get("/sales").text
    assert "Check 1 sale" in page and 'href="/sales#check"' in page   # the first thing to do on Home
    assert page.index("To do now") < page.index("Check 1 sale")
    assert '<section id="check" data-count="1"' in sales


def test_dates_read_the_way_people_say_them():
    clock.freeze(clock.now().replace(year=2026, month=10, day=7, hour=12))
    try:
        assert clock.day_words("2026-10-05") == "5 Oct 2026" and clock.day_words("2026-10-05", short=True) == "5 Oct"
        assert clock.day_words("2027-03-23", short=True) == "23 Mar 2027" and clock.day_words(None) == ""
        assert clock.when_words("2026-10-07 09:06:00") == "today 09:06"
        assert clock.when_words("2026-10-06 18:40:12", cap=True) == "Yesterday 18:40"
        assert clock.when_words("2026-10-01 07:05:00") == "1 Oct, 07:05"
    finally:
        clock.freeze(None)


def test_history_shows_who_adjusted_and_why(cfg, conn, stub_encoder, monkeypatch):
    p, _ = strawberry(conn)
    stock.adjust(conn, p, "family_use", 2, by="Dara")
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        r = c.get(f"/catalog/{p}/history")
        assert r.status_code == 200 and "Family use by Dara" in r.text


def test_keyboard_reaches_the_stock_and_a_dead_link_has_a_way_back(cfg, conn, stub_encoder, monkeypatch):
    p, _ = strawberry(conn)
    with client_for(cfg, monkeypatch) as c:
        today = c.get("/").text
        assert 'class="skip-link" href="#main"' in today and 'id="main"' in today
        stock_list = c.get("/stock").text
        assert f'class="prow" href="/catalog/{p}"' in stock_list and "onclick=\"location=" not in stock_list
        page = c.get("/no-such-page", headers={"Accept": "text/html"})
        assert page.status_code == 404 and "There&#39;s no page here" in page.text and 'href="/stock"' in page.text
        gone = c.get("/catalog/99999", headers={"Accept": "text/html"})
        assert gone.status_code == 404 and "No such product" in gone.text
        api = c.get("/api/no-such-thing")
        assert api.status_code == 404 and api.json() == {"detail": "Not Found"}
