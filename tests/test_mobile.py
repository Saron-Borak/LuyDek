"""The phone-first app: four places and a +, Khmer or English per phone, and short flows that go on to the next."""

from pathlib import Path

from luydek import db
from luydek.services import stock
from luydek.web import words
from tests.conftest import pid
from tests.test_shelf import client_for

TEMPLATES = Path(__file__).parents[1] / "luydek" / "web" / "templates"


def test_every_wrapped_phrase_has_khmer():
    text = "".join(p.read_text(encoding="utf-8") for p in TEMPLATES.glob("*.html"))
    assert words.missing(text) == []


def test_words_pick_the_plural_and_fill_in():
    words.use("en")
    assert words.say("Check {n} sale|Check {n} sales", n=1) == "Check 1 sale"
    assert words.say("{n} left, about {d} day|{n} left, about {d} days", n=4, d=1, count=1) == "4 left, about 1 day"
    words.use("km")
    assert words.say("Check {n} sale|Check {n} sales", n=2) == "ពិនិត្យការលក់ 2" and words.say("Not a phrase yet") == "Not a phrase yet"
    assert words.unit("cans") == "កំប៉ុង" and words.date("5 Oct, today 09:06") == "5 តុលា, ថ្ងៃនេះ 09:06"
    words.use("xx")
    assert words.lang() == "en"


def test_a_phone_picks_its_language_and_keeps_it(cfg, conn, stub_encoder, monkeypatch):
    with client_for(cfg, monkeypatch) as c:
        assert "To do now" in c.get("/").text                      # this shop's default: English
        r = c.get("/lang/km?next=/stock", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/stock" and "ld_lang=km" in r.headers["set-cookie"]
        home = c.get("/").text
        assert 'lang="km"' in home and "ត្រូវធ្វើឥឡូវ" in home and "មានអ្វីកើតឡើង?" in home
        assert c.get("/lang/en?next=//evil.example", follow_redirects=False).headers["location"] == "/"
        assert "To do now" in c.get("/").text


def test_four_places_a_sheet_and_help_everywhere(cfg, conn, stub_encoder, monkeypatch):
    with client_for(cfg, monkeypatch) as c:
        for path in ("/", "/stock", "/orders", "/more", "/help", "/count", "/adjust", "/sales"):
            page = c.get(path).text
            assert '<nav class="tabbar' in page and 'data-open-sheet="what"' in page and 'href="/help' in page, path
        sheet = c.get("/").text.split('<dialog class="sheet"')[1].split("</dialog>")[0]
        for href in ('href="/invoices"', 'href="/adjust"', 'href="/count"', 'href="/catalog/new"', 'href="/stock-in?mode=count"'):
            assert href in sheet, href
        more = c.get("/more").text
        assert all(h in more for h in ('href="/shelf"', 'href="/map"', 'href="/spreadsheet"', 'href="/setup"', 'href="/lang/km'))
        assert 'id="practice"' in c.get("/help").text and 'class="appbar-back" href="/more"' in c.get("/shelf").text


def test_count_one_product_then_the_next(cfg, conn, stub_encoder, monkeypatch):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 10, "opening")
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get(f"/count?product={coke}").text
        assert 'name="counted" value="10"' in page and 'name="next" value="/count"' in page and 'data-step="1"' in page
        r = c.post(f"/catalog/{coke}/stock", data={"counted": "7", "next": "/count"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/count?msg=")
    assert stock.on_hand(db.connect(cfg.db_path))[coke] == 7


def test_sold_or_used_starts_from_the_product(cfg, conn, stub_encoder, monkeypatch):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 10, "opening")
    conn.commit()
    with client_for(cfg, monkeypatch) as c:
        page = c.get(f"/adjust?product={coke}").text
        assert f'name="product_id" id="adj-product" value="{coke}"' in page and "Coca-Cola can 330ml" in page
        product = c.get(f"/catalog/{coke}").text
        assert f'href="/adjust?product={coke}"' in product and f'href="/count?product={coke}"' in product
        r = c.post("/adjust", data={"product_id": coke, "reason": "family_use", "qty": "2", "next": "/adjust"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/adjust?msg=")
    assert stock.on_hand(db.connect(cfg.db_path))[coke] == 8
