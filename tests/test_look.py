"""Photos in lists, the sales chart, the phone QR code and one icon set."""

import io
import pathlib
import re
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from luydek import clock
from luydek.services import stock
from luydek.web import server
from luydek.web.server import create_app
from tests.conftest import NOW, pid
from tests.test_api import enroll


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_list_photos_are_small_and_made_once(client, conn, cfg):
    enroll(client, conn, "coke-330", shots=2)
    gid = conn.execute("SELECT MIN(id) FROM gallery_images").fetchone()[0]
    r = client.get(f"/thumb/{gid}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(r.content)).size) <= 160
    assert len(list((cfg.media_dir / "thumbs").iterdir())) == 1
    assert client.get("/thumb/999.jpg").status_code == 404


def test_products_show_their_photo_in_lists_and_pickers(client, conn):
    enroll(client, conn, "coke-330", shots=2)
    gid = conn.execute("SELECT MIN(id) FROM gallery_images").fetchone()[0]
    page = client.get("/stock").text
    assert f'src="/thumb/{gid}.jpg"' in page                           # the stock list
    assert 'class="bi bi-image"' in page                               # products without photos yet
    picker = client.get("/adjust").text
    assert f'"img": "/thumb/{gid}.jpg"' in picker                      # picker data
    assert f'src="/thumb/{gid}.jpg"' in client.get("/orders").text


def test_sales_days_mark_an_empty_shelf(conn):
    coke = pid(conn, "coke-330")
    start = NOW - timedelta(days=5)
    stock.record(conn, coke, 2, "opening", at=start)
    stock.record(conn, coke, -2, "sale", at=start + timedelta(days=1))
    stock.record(conn, coke, 24, "delivery", at=start + timedelta(days=4))
    stock.record(conn, coke, -3, "sale", at=start + timedelta(days=4, hours=1))
    days = stock.product_days(conn, coke, 7)
    # 21-27 Sep: counted on the 22nd, sold out on the 23rd, restocked and selling again on the 26th.
    assert [d["sold"] for d in days] == [0, 0, 2, 0, 0, 3, 0]
    # Empty only after it sold out: before the first count nothing was known, so nothing is shaded.
    assert [d["empty"] for d in days] == [False, False, False, True, True, False, False]


def test_product_page_charts_four_weeks_of_sales(client, conn):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 50, "opening", at=NOW - timedelta(days=30))
    for day in range(1, 15):
        stock.record(conn, coke, -day % 4 - 1, "sale", at=NOW - timedelta(days=day))
    conn.commit()
    page = client.get(f"/catalog/{coke}").text
    assert 'class="mini-chart"' in page and "sold per day, last 4 weeks" in page
    assert page.count('class="hit"') == 28 and "tips.js" in page
    assert "Latest changes" in page and "<details" in page


def test_today_compares_this_week_with_last(client, conn):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 100, "opening", at=NOW - timedelta(days=20))
    stock.record(conn, coke, -10, "sale", at=NOW - timedelta(days=10))
    stock.record(conn, coke, -12, "sale", at=NOW - timedelta(days=2))
    conn.commit()
    page = client.get("/sales").text
    assert "This week 12 sold" in page and "20%</span> vs last week" in page


def test_setup_shows_a_qr_code_for_phones(client, monkeypatch):
    monkeypatch.setattr(server, "_lan_ip", lambda: "192.168.1.20")
    monkeypatch.setenv("LUYDEK_HOST", "0.0.0.0")
    page = client.get("/setup").text
    assert "<svg" in page and "http://192.168.1.20:" in page and "Add to Home screen" in page
    monkeypatch.setenv("LUYDEK_HOST", "127.0.0.1")
    assert "only this computer can open LuyDek" in client.get("/setup").text
    monkeypatch.setattr(server, "_lan_ip", lambda: None)
    assert "isn't on a Wi-Fi network" in client.get("/setup").text


def test_web_pages_use_one_icon_set():
    emoji = re.compile("[\U0001F300-\U0001FAFF☀-➿⬀-⯿⌀-⏿]")
    web = pathlib.Path(__file__).resolve().parents[1] / "luydek" / "web"
    for path in [*web.glob("templates/*.html"), *web.glob("static/*.js")]:
        found = [c for c in emoji.findall(path.read_text(encoding="utf-8")) if c != "✓"]  # ✓ is text
        assert not found, f"{path.name}: {found}"


def test_clock_is_frozen_for_these_tests():
    assert clock.now() == NOW and isinstance(NOW, datetime)
