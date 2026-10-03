"""The owner-facing pages: action first, setup and demo tools out of the way, words that read right."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from luydek import render
from luydek.services import stock, transactions
from luydek.services.forecast import Line, SupplierPlan
from luydek.services.transactions import DetectionRecord
from luydek.web.server import create_app
from tests.conftest import jpeg, mat_frame, pack_of, pid


def make_client(cfg, monkeypatch, team=False):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    cfg.shop.team_tools = team
    return TestClient(create_app(cfg))


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    with make_client(cfg, monkeypatch) as c:
        yield c


def pending(conn, sku, tier="medium", guess=True):
    p = pack_of(conn, sku)
    txn = transactions.open_transaction(conn)
    transactions.add_frame(conn, txn, None, [DetectionRecord(p if guess else None, 0.8, tier, "image", (0, 0, 9, 9),
                                                             None, [(p, 0.8)])])
    transactions.close_transaction(conn, txn)
    conn.commit()


def test_all_correct_confirms_every_guess_and_leaves_the_rest(client, conn):
    pending(conn, "coke-330")
    pending(conn, "sprite-1500")
    pending(conn, "dumex-400", tier="low", guess=False)
    page = client.get("/").text
    assert "All correct (<span data-all-n>2</span>)" in page and "1 need you to pick below" in page
    r = client.post("/items/confirm-all", follow_redirects=False)
    assert r.status_code == 303 and "Confirmed%202%20items" in r.headers["location"]
    assert stock.on_hand(conn)[pid(conn, "coke-330")] == -1
    assert [it["pack_id"] for it in transactions.pending_items(conn)] == [None]


def test_nothing_waiting_shows_all_clear(client):
    page = client.get("/").text
    # Both are in the page so answering in place can switch between them; only one shows.
    assert 'class="all-clear mb-3 "' in page and 'class="card check-card mb-3 d-none"' in page


# --- answering in place ---------------------------------------------------------------------------

def test_answers_from_the_page_get_json_back(client, conn):
    pending(conn, "coke-330")
    item = transactions.pending_items(conn)[0]["id"]
    json_ = {"Accept": "application/json"}
    r = client.post(f"/items/{item}/resolve", data={"action": "qty", "delta": "1"}, headers=json_)
    assert r.json() == {"item": item, "status": "pending", "count": 2, "already": False}
    r = client.post(f"/items/{item}/resolve", data={"action": "yes"}, headers=json_)
    assert r.json()["status"] == "confirmed" and stock.on_hand(conn)[pid(conn, "coke-330")] == -2
    # Answered on Telegram meanwhile: the page's late answer changes nothing.
    assert client.post(f"/items/{item}/resolve", data={"action": "remove"}, headers=json_).json()["already"] is True
    assert client.post("/items/999/resolve", data={"action": "yes"}, headers=json_).status_code == 400


def test_all_correct_only_confirms_what_the_owner_saw(client, conn):
    pending(conn, "coke-330")
    seen = transactions.pending_items(conn)[0]["id"]
    pending(conn, "sprite-1500")                                   # arrived after the page was drawn
    r = client.post("/items/confirm-all", data={"ids": str(seen)}, headers={"Accept": "application/json"})
    assert r.json() == {"confirmed": [seen]}
    assert [it["pack_id"] for it in transactions.pending_items(conn)] == [pack_of(conn, "sprite-1500")]


def test_the_check_list_can_be_fetched_alone(client, conn):
    pending(conn, "coke-330")
    html = client.get("/fragments/checks").text
    assert html.lstrip().startswith("{#") is False and '<section id="check" data-count="1"' in html
    assert "Is this <b>Coca-Cola can 330ml</b>" in html and "<nav" not in html


def test_pickers_list_best_sellers_first(client, conn):
    import json
    import re
    stock.record(conn, pid(conn, "sprite-1500"), -9, "sale")
    stock.record(conn, pid(conn, "coke-330"), -3, "sale")
    conn.commit()
    data = json.loads(re.search(r'<script type="application/json" id="picker-data">(.*?)</script>',
                                client.get("/").text, re.S).group(1))
    assert [i["label"] for i in data[:2]] == ["Sprite bottle 1.5L", "Coca-Cola can 330ml"]
    coke = [i for i in data if i["product"] == pid(conn, "coke-330")]
    assert [i["base"] for i in coke] == [True, False]              # the can, then the 6-pack
    assert "កូកា" in coke[0]["find"]                              # Khmer names and aliases are searchable


def test_photos_delete_in_bulk_with_json(client, conn, cfg):
    from tests.test_api import enroll
    enroll(client, conn, "coke-330", shots=3)
    ids = [r[0] for r in conn.execute("SELECT id FROM gallery_images")]
    r = client.post("/catalog/gallery/delete", data={"gid": ids[:2]}, headers={"Accept": "application/json"})
    assert r.json() == {"deleted": 2}


def test_owner_mode_hides_the_team_tools(client):
    assert " Demo</button>" not in client.get("/").text
    assert "Pilot report" not in client.get("/").text
    assert 'id="upload"' not in client.get("/counter").text
    assert "What the owner gets in Telegram" not in client.get("/orders").text
    assert client.get("/report").status_code == 200          # still there for whoever has the link


def test_team_mode_shows_them(cfg, conn, stub_encoder, monkeypatch):
    with make_client(cfg, monkeypatch, team=True) as c:
        assert " Demo</button>" in c.get("/").text and "Pilot report" in c.get("/").text
        assert 'id="upload"' in c.get("/counter").text
        assert "What the owner gets in Telegram" in c.get("/orders").text


def test_setup_is_a_checklist_that_follows_the_shop(client, conn):
    page = client.get("/setup").text
    assert "Calibrate the counter camera" in page and "1 of 7 steps done" in page   # the seed already has suppliers
    assert "/counter#setup" in page and "/catalog#needs-photos" in page
    assert 'class="collapse show mt-3" id="setup"' in client.get("/counter").text   # setup open until done

    client.post("/api/calibrate", files={"image": ("e.jpg", jpeg(mat_frame()), "image/jpeg")})
    client.post("/api/roi", data={"x0": 0.05, "y0": 0.05, "x1": 0.95, "y1": 0.95})
    assert "3 of 7 steps done" in client.get("/setup").text
    assert 'class="collapse  mt-3" id="setup"' in client.get("/counter").text       # folded away after


def test_order_quantities_read_right(conn):
    def line(cases, unit="box", order_unit="case", case_size=10):
        return Line(product_id=1, sku="x", name_en="Paracetamol", name_km="ប៉ារ៉ា", unit=unit, case_size=case_size,
                    is_critical=False, unit_cost=0.5, stock=0, avg_daily=1, sold_last_week=7, sold_4w=28,
                    days_since_sale=0, stockout_days=0, action="order", units=cases * case_size, cases=cases,
                    order_unit=order_unit)
    plan = SupplierPlan(1, "Pharma", "💊", date(2026, 10, 5), 6, 7, 0, False,
                        [line(1), line(2), line(3, "tablet", "box", 100)])
    text = render.order_message(plan, "en", simulated=False)
    assert "Paracetamol — 1 case\n" in text and "Paracetamol — 2 cases\n" in text
    assert "Paracetamol — 3 boxes" in text and "case(s)" not in text


def test_pages_never_make_up_plurals(client, conn):
    stock.record(conn, pid(conn, "para-250"), 2, "opening")
    conn.commit()
    for path in ("/", "/orders", "/catalog", f"/catalog/{pid(conn, 'para-250')}",
                 f"/catalog/{pid(conn, 'cetirizine-10')}"):
        page = client.get(path).text
        assert "boxs" not in page and "case(s)" not in page, path
    assert "box of 100 tablets" in client.get("/catalog").text
