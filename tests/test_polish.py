"""The Night market look: brand files, offline assets, clean product photos, sparklines, busy hours,
spinners on slow forms and friendly empty states."""

import io
import json
import pathlib
import re
from datetime import timedelta

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from luydek import clock
from luydek.recognition import cutout
from luydek.services import metrics, stock
from luydek.web import charts
from luydek.web.server import create_app
from tests.conftest import MAT, NOW, mat_frame, paint, pid
from tests.test_api import enroll

WEB = pathlib.Path(__file__).resolve().parents[1] / "luydek" / "web"


@pytest.fixture
def client(cfg, conn, stub_encoder, monkeypatch):
    monkeypatch.setenv("LUYDEK_NO_PRELOAD", "1")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with TestClient(create_app(cfg)) as c:
        yield c


def test_brand_files_are_served(client):
    ico = client.get("/favicon.ico")
    assert ico.status_code == 200 and ico.headers["content-type"] == "image/x-icon"
    m = client.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    manifest = json.loads(m.content)
    assert manifest["name"] == "LuyDek" and manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    page = client.get("/").text
    assert 'rel="manifest"' in page and "brand/favicon.svg" in page and "apple-touch-icon" in page


def test_the_look_never_needs_the_internet(client):
    page = client.get("/").text
    assert not re.search(r'(href|src)="https?://', page), "a stylesheet, script or font from the internet"
    for url in re.findall(r'(?:href|src)="(/static/[^"?]+)', page):
        assert client.get(url).status_code == 200, url
    # The icon font the icon stylesheet points at is there too.
    css = (WEB / "static/vendor/bootstrap-icons/bootstrap-icons.min.css").read_text(encoding="utf-8")
    font = re.search(r'url\("(?:\./)?fonts/(bootstrap-icons\.woff2)', css).group(1)
    assert (WEB / "static/vendor/bootstrap-icons/fonts" / font).exists()
    app_css = (WEB / "static/app.css").read_text(encoding="utf-8")
    fonts = re.findall(r'url\("([^"]+\.woff2)"\)', app_css)       # relative to /static/app.css
    assert len(fonts) == 3                                          # Inter (2 ranges) and Kantumruy Pro for Khmer
    for woff in fonts:
        assert client.get(f"/static/{woff}").status_code == 200, woff


def test_one_navigation_for_laptops_and_phones(client):
    page = client.get("/orders").text
    assert page.count('href="/orders"') >= 3            # sidebar, phone menu, bottom bar
    assert 'class="side-link active" href="/orders"' in page
    assert 'aria-current="page"' in page


# --- clean product photos ------------------------------------------------------------------------

def test_a_product_is_cut_out_onto_white():
    frame = mat_frame(320, 240)
    paint(frame, "coke-330", 110, 50)
    rgb = frame[..., ::-1].copy()
    white = cutout.on_white(rgb)
    assert white is not None and white.shape == (160, 160, 3)
    assert (white[:6, :6] > 245).all() and (white[-6:, -6:] > 245).all()   # a white page
    r, g, b = white[80, 80].astype(int)                                     # the red can in the middle
    assert r > 150 and g < 90 and b < 90


def test_a_photo_that_wont_cut_cleanly_is_left_alone():
    flat = np.full((240, 320, 3), MAT[::-1], np.uint8)
    assert cutout.on_white(flat) is None
    # A thin pen on a table with a wall behind: GrabCut takes the table with it, the edge check says no.
    scene = np.full((240, 320, 3), 235, np.uint8)
    scene[90:] = (150, 150, 150)
    scene[150:156, 60:260] = (30, 40, 160)
    assert cutout.on_white(scene) is None


def test_list_photos_are_cut_out(client, conn, cfg):
    enroll(client, conn, "coke-330", shots=2)
    gid = conn.execute("SELECT MIN(id) FROM gallery_images").fetchone()[0]
    im = np.asarray(Image.open(io.BytesIO(client.get(f"/thumb/{gid}.jpg").content)).convert("RGB"))
    assert im.shape == (160, 160, 3) and (im[:4, :4] > 240).all()
    assert [p.name.endswith("_w.jpg") for p in (cfg.media_dir / "thumbs").iterdir()] == [True]


# --- sparklines and busy hours --------------------------------------------------------------------

def test_sparklines_are_word_sized():
    bars = charts.sparkline([0, 2, 0, 5], label="Sold per day")
    assert 'class="spark mini-chart"' in bars and "<title>Sold per day</title>" in bars
    assert bars.count("<path") == 2                     # one bar per day that sold, none for quiet days
    line = charts.sparkline([3, 4, 2, 6], charts.AMBER, kind="line", tips=["a", "b", "c", "d"])
    assert "<polyline" in line and "<circle" in line and line.count('class="hit"') == 4
    assert charts.sparkline([0, 0, 0]).count("<path") == 0   # nothing sold: just the hairline


def test_busy_hours_average_over_the_days_the_camera_ran(conn):
    for day, hour in [(1, 9), (1, 17), (1, 17), (2, 17), (3, 12), (40, 17)]:   # the last is too old
        at = (NOW - timedelta(days=day)).replace(hour=hour, minute=10)
        conn.execute("INSERT INTO transactions (started_at, source, status) VALUES (?, 'camera', 'auto')",
                     (clock.stamp(at),))
    conn.execute("INSERT INTO transactions (started_at, source, status) VALUES (?, 'camera', 'open')",
                 (clock.stamp(NOW),))                     # still open: not a sale yet
    busy = metrics.busy_hours(conn)
    assert busy == {"counts": {9: 1, 12: 1, 17: 3}, "days": 3, "total": 5, "peak": 17}
    svg = charts.hours_chart(busy["counts"], busy["days"])
    assert "5pm–6pm: 3 sales in 3 days, about 1 a day" in svg
    assert "7am–8am: 0 sales in 3 days" in svg and ">9am<" in svg and ">9pm<" in svg


def test_today_shows_two_weeks_at_a_glance(client, conn):
    coke = pid(conn, "coke-330")
    stock.record(conn, coke, 100, "opening", at=NOW - timedelta(days=20))
    for day in (1, 2, 5):
        stock.record(conn, coke, -3, "sale", at=NOW - timedelta(days=day))
    conn.commit()
    page = client.get("/").text
    assert 'class="stat hero"' in page and "<polyline" in page          # the shop's line in the Sales tile
    assert "Sold per day, last 14 days: 9 in all" in page               # coke's strip in the stock table
    assert "Sold · 14 days" in page and "tips.js" in page
    assert "Not enough sales yet" in page                               # no camera sales: no busy hours yet


def test_busy_hours_appear_after_a_few_sales(client, conn):
    for i in range(6):
        at = (NOW - timedelta(days=i % 3)).replace(hour=10 + i, minute=0)
        conn.execute("INSERT INTO transactions (started_at, source, status) VALUES (?, 'camera', 'auto')",
                     (clock.stamp(at),))
    conn.commit()
    page = client.get("/").text
    assert "Sales by hour of day" in page and "Busiest around" in page


# --- spinners and empty states --------------------------------------------------------------------

def test_slow_forms_show_what_is_happening(client):
    page = client.get("/invoices").text
    assert 'data-busy="Reading the invoice' in page and 'data-busy="Saving' in page
    assert "Reading… (up to a minute)\"; });" not in page               # the old one-off script is gone
    assert "ui.js" in page
    assert 'data-busy="Saving the counts' in client.get("/catalog").text
    assert 'data-busy="Adding' in client.get("/catalog/new").text


def test_empty_lists_invite_the_next_step(client):
    page = client.get("/invoices").text
    assert 'class="empty-state"' in page and "No deliveries yet" in page
    page = client.get("/").text
    assert "No camera sales yet" in page and 'href="/counter" class="btn' in page


def test_must_have_tags_use_the_palette():
    for path in WEB.glob("templates/*.html"):
        assert "pill bg-dark text-white" not in path.read_text(encoding="utf-8"), path.name


# --- a phone as the counter camera ------------------------------------------------------------------

def test_setup_points_phones_at_the_secure_address(client, monkeypatch):
    monkeypatch.setenv("LUYDEK_PUBLIC_URL", "https://shop-laptop.tail1234.ts.net/")
    page = client.get("/setup").text
    assert "https://shop-laptop.tail1234.ts.net/" in page and "<svg" in page
    assert "Tailscale" in page and "counter camera" in page
    monkeypatch.delenv("LUYDEK_PUBLIC_URL")
    assert "Phone as the counter camera" in client.get("/setup").text


def test_the_counter_page_suits_a_phone():
    js = (WEB / "static/counter.js").read_text(encoding="utf-8")
    cam = (WEB / "static/camera.js").read_text(encoding="utf-8")
    css = (WEB / "static/app.css").read_text(encoding="utf-8")
    assert 'v.facingMode = { ideal: "environment" }' in cam       # the back camera, not the selfie one
    assert "cam.start(" in js and "LuyCam.saved()" in js           # and the one picked last time
    assert "width: { ideal: 4096 }, height: { ideal: 4096 }" in cam   # its largest picture, in its own shape
    assert 'setProperty("--ar"' in cam and "aspect-ratio: var(--ar)" in css   # the viewfinder takes that shape
    assert "applyConstraints({ advanced: [{ zoom:" in cam and "scale(${z})" in cam   # its own zoom, else digital
    assert 'navigator.wakeLock.request("screen")' in js           # the screen stays on
    assert 'readyState === "ended"' in js                          # the camera reopens after the phone wakes
    assert "LuyCam.whyNot()" in js                                  # plain words when the address isn't https
    assert 'viewChanged("zoom")' in js                             # a new zoom asks for a fresh calibration


def test_every_stylesheet_closes_what_it_opens():
    """One missing brace and every rule after it silently applies only inside the block left open
    (it happened once, in a merge: the Sold card lost its styles on laptops)."""
    for path in (WEB / "static").glob("*.css"):
        depth = 0
        for ch in re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S):
            depth += (ch == "{") - (ch == "}")
            assert depth >= 0, f"{path.name}: a }} with nothing open"
        assert depth == 0, f"{path.name}: {depth} block(s) never closed"
