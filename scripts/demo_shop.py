"""Build a DEMO shop: real products, eight weeks of SIMULATED history, every feature in use.

"Sokha Mini-Mart" is a made-up Phnom Penh mini-mart as it would look after eight weeks on LuyDek:
45 real products (Cambodian beers and water, soft drinks, snacks, noodles, coffee, household goods and
a medicine cabinet) from six supplier agents, on eight places drawn face on and in 3D.

* Onboarding, eight weeks ago: the owner's spreadsheet put every product on its shelf, level by level,
  and counted the stock with its expiry dates (Map, New shelf, From a spreadsheet).
* Every supplier visit since: an invoice, confirmed line by line, with lots and expiry dates; a bonus
  case now and then; one new product added straight from an invoice and not put away yet.
* Sales every day; a credit sale, breakage, family use; quick counts; expired stock taken off.
* The stories the pitch tells: baby formula run out after a forgotten order (an alert, a gap on the
  shelf); a dead green tea and other sleeping money; strawberry milk past its date still on the
  shelf; Milo UHT to put in front; Pocari Sweat over-ordered that won't sell in time; the best
  seller, Sting, on the bottom of the fridge (the suggested move); a lot that doesn't match its
  invoice; paracetamol sold by the tablet.
* The last two days at the counter: real product photos laid on the real counter mat and put through
  the real camera pipeline (YOLOE + CLIP). What the camera was sure of, and what it asked about, is
  what it really did with those photos. Most questions were answered on Telegram, last night's
  leftovers became best guesses, and today's are still waiting. Mama noodles had no photos at the
  start: the camera asked, the owner answered, and it learned them while selling.

Every stock movement is marked simulated, so the app says "includes simulated history" everywhere.

    python scripts/demo_shop.py                      # data/demo/luydek.db, photos in data/demo/media
    python -m luydek --config configs/demo.yaml --port 8030

Assets (data/demo/assets): photos/<sku>.jpg (Open Food Facts and Wikimedia Commons, CC BY-SA; see
credits.json), cut/<key>.png (product cut-outs for the counter photos), mat.jpg (the counter mat).
Products without a photo show a box. Khmer names are a first draft for a native reader to check.
"""

from __future__ import annotations

import argparse
import io
import json
import random
import shutil
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek import clock, db  # noqa: E402
from luydek.config import load_config  # noqa: E402
from luydek.recognition import pipeline  # noqa: E402
from luydek.services import (alerts, batches, catalog, forecast, invoices, shelfsheet, shop3d, stock,  # noqa: E402
                             stockmap, transactions)

ROOT = Path(__file__).resolve().parents[1]
WEEKS = 8

# key: name, emoji, visit weekday (0 = Monday), every n days, minimum order ($), contact (made up)
SUPPLIERS = {
    "coke": ("Coca-Cola agent", "🥤", 1, 7, 20, "Agent Dara · 012 000 101"),
    "beer": ("Beer distributor", "🍺", 3, 7, 30, "Agent Visal · 012 000 102"),
    "drinks": ("Snack & drink distributor", "🍵", 2, 7, 15, "Agent Srey Neang · 012 000 103"),
    "nestle": ("Nestlé & dairy distributor", "🍼", 4, 7, 25, None),
    "home": ("Household distributor", "🧴", 5, 14, 20, None),
    "pharma": ("Pharma distributor", "💊", 0, 7, 30, None),
}

# sku, English name, Khmer name, unit, case size, supplier, cost per unit ($), shelf life (days),
# sales a day, opening count, invoice spellings
PRODUCTS = [
    ("coke-330", "Coca-Cola can 330ml", "កូកាកូឡា កំប៉ុង 330ml", "can", 24, "coke", 0.40, 270, 13, 70, ["coca 330", "coke can"]),
    ("coke-1500", "Coca-Cola bottle 1.5L", "កូកាកូឡា ដប 1.5L", "bottle", 12, "coke", 0.95, 270, 3, 24, ["coca 1.5"]),
    ("sprite-1500", "Sprite bottle 500ml", "ស្ព្រាយ ដប 500ml", "bottle", 24, "coke", 0.42, 270, 5, 36, ["sprite 500"]),
    ("fanta-330", "Fanta orange can 330ml", "ហ្វាន់តា ក្រូច កំប៉ុង 330ml", "can", 24, "coke", 0.40, 270, 4, 30, ["fanta can"]),
    ("sting-330", "Sting energy drink 330ml", "ស្ទីង 330ml", "can", 24, "drinks", 0.33, 365, 19, 90, ["sting red", "sting 330"]),
    ("pocari-500", "Pocari Sweat 500ml", "ប៉ូការី ស្វេត 500ml", "bottle", 24, "drinks", 0.75, 300, 0.5, 10, ["pocari"]),
    ("ichitan-420", "Ichitan green tea 420ml", "តែបៃតង អ៊ីជីតាន់ 420ml", "bottle", 24, "drinks", 0.55, 240, 3, 30, ["ichitan"]),
    ("oishi-380", "Oishi green tea 380ml", "តែបៃតង អូអ៊ីស៊ី 380ml", "bottle", 24, "drinks", 0.55, 240, 0, 36, ["oishi"]),
    ("vital-500", "Vital water 500ml", "ទឹកបរិសុទ្ធ វីតាល់ 500ml", "bottle", 24, "drinks", 0.15, 720, 24, 120, ["vital 500", "vital water"]),
    ("aquafina-500", "Aquafina water 500ml", "ទឹកបរិសុទ្ធ អាក្វាហ្វីណា 500ml", "bottle", 24, "drinks", 0.20, 720, 5, 36, ["aquafina"]),
    ("dutchlady-choc", "Dutch Lady chocolate milk 180ml", "ទឹកដោះគោ ដាច់ឡេឌី សូកូឡា 180ml", "box", 48, "nestle", 0.32, 180, 8, 60, ["dl choc", "dutch lady choco"]),
    ("dutchlady-straw", "Dutch Lady strawberry milk 180ml", "ទឹកដោះគោ ដាច់ឡេឌី ស្ត្របឺរី 180ml", "box", 48, "nestle", 0.32, 180, 1.4, 20, ["dl straw"]),
    ("milo-uht-180", "Milo UHT 180ml", "មីឡូ ប្រអប់ 180ml", "box", 48, "nestle", 0.38, 180, 2.2, 30, ["milo uht"]),
    ("angkor-330", "Angkor Beer can 330ml", "ស្រាបៀរ អង្គរ កំប៉ុង 330ml", "can", 24, "beer", 0.55, 365, 21, 120, ["angkor can", "angkor 330"]),
    ("cambodia-330", "Cambodia Beer can 330ml", "ស្រាបៀរ កម្ពុជា កំប៉ុង 330ml", "can", 24, "beer", 0.50, 365, 17, 96, ["cambodia can"]),
    ("tiger-330", "Tiger beer bottle 330ml", "ស្រាបៀរ តាយហ្គឺ ដប 330ml", "bottle", 24, "beer", 0.70, 365, 4, 30, ["tiger btl"]),
    ("heineken-330", "Heineken slim can 330ml", "ស្រាបៀរ ហៃណឺកែន កំប៉ុង 330ml", "can", 24, "beer", 0.80, 365, 3, 24, ["heineken"]),
    ("lays-50", "Lay's classic 50g", "ឡេយស៍ ដំឡូងបារាំង 50g", "bag", 30, "drinks", 0.45, 180, 6, 40, ["lays 50"]),
    ("pringles-107", "Pringles original 107g", "ព្រីងហ្គលស៍ 107g", "tube", 12, "drinks", 1.60, 365, 1.2, 12, ["pringles"]),
    ("oreo-133", "Oreo original 133g", "នំអូរីអូ 133g", "pack", 24, "drinks", 0.75, 270, 3, 24, ["oreo"]),
    ("kitkat-4f", "KitKat 4 finger", "ឃីតខេត 4 ដុំ", "bar", 48, "nestle", 0.50, 270, 4, 30, ["kitkat"]),
    ("snickers-50", "Snickers bar 50g", "ស្នីកឃ័រ 50g", "bar", 48, "drinks", 0.55, 270, 3, 24, ["snickers"]),
    ("chocopie-12", "Orion Choco Pie, box of 12", "នំឆូកូប៉ៃ ប្រអប់ 12", "box", 12, "drinks", 2.40, 240, 0.4, 10, ["choco pie"]),
    ("mentos-mint", "Mentos mint roll", "ស្ករគ្រាប់ មិនតូស", "roll", 40, "drinks", 0.30, 540, 5, 30, ["mentos"]),
    ("halls-mentho", "Halls Mentho-Lyptus", "ស្ករគ្រាប់ ហលស៍", "roll", 40, "drinks", 0.30, 540, 2, 20, ["halls"]),
    ("wrigley-spearmint", "Orbit spearmint gum, bottle", "ស្ករកៅស៊ូ អរប៊ីត", "bottle", 12, "drinks", 1.80, 540, 0.5, 8, ["orbit"]),
    ("mama-pork", "Mama instant noodles pork", "មីម៉ាម៉ា រសសាច់ជ្រូក", "packet", 30, "drinks", 0.20, 240, 11, 60, ["mama pork"]),
    ("nissin-cup", "Nissin Cup Noodles beef", "មីកែវ និស្ស៊ីន សាច់គោ", "cup", 24, "drinks", 0.65, 240, 2.5, 20, ["cup noodle"]),
    ("nescafe-3in1", "Nescafé 3in1 sachet", "កាហ្វេ នេស្កាហ្វេ 3in1", "sachet", 50, "nestle", 0.10, 540, 14, 80, ["nescafe 3in1"]),
    ("milo-400", "Milo powder 400g", "មីឡូ ម្សៅ 400g", "tin", 12, "nestle", 4.50, 540, 0.5, 6, ["milo 400"]),
    ("ovaltine-400", "Ovaltine original 400g", "អូវ៉ាល់ទីន 400g", "jar", 12, "nestle", 3.90, 540, 0.08, 6, ["ovaltine"]),
    ("nutella-200", "Nutella 200g", "នុយតេឡា 200g", "jar", 12, "nestle", 3.20, 365, 0.05, 6, ["nutella"]),
    ("cornflakes", "Kellogg's Corn Flakes 500g", "ខនហ្វ្លេក ខេឡុក 500g", "box", 12, "nestle", 2.80, 300, 0.4, 8, ["corn flakes"]),
    ("condensed-milk", "Carnation condensed milk 397g", "ទឹកដោះគោខាប់ កាណេសិន 397g", "can", 48, "nestle", 0.95, 540, 2, 24, ["carnation"]),
    ("maggi-sauce", "Maggi seasoning sauce", "ទឹកស៊ីអ៊ីវ ម៉ាហ្គី", "bottle", 12, "nestle", 1.70, 720, 1, 10, ["maggi"]),
    ("colgate-150", "Colgate toothpaste 150g", "ថ្នាំដុសធ្មេញ កុលហ្គេត 150g", "tube", 48, "home", 1.20, 900, 1.2, 14, ["colgate"]),
    ("lifebuoy-soap", "Lifebuoy soap 125g", "សាប៊ូដុំ លីហ្វបយ 125g", "bar", 72, "home", 0.55, 900, 1.5, 18, ["lifebuoy"]),
    ("dumex-400", "Dumex Dugro baby formula 400g", "ទឹកដោះគោម្សៅ ឌូម៉ិច ឌូក្រូ 400g", "tin", 12, "nestle", 9.50, 540, 0.7, 12, ["dumex"]),
    ("para-500", "Paracetamol 500mg", "ប៉ារ៉ាសេតាមុល 500mg (គ្រាប់)", "tablet", 100, "pharma", 0.012, 1095, 28, 400, ["paracetamol 500", "para 500"]),
    ("para-250", "Paracetamol 250mg children, strip", "ប៉ារ៉ាសេតាមុល កុមារ 250mg", "strip", 20, "pharma", 0.25, 730, 1.2, 15, ["paracetamol 250"]),
    ("cetirizine-10", "Cetirizine 10mg", "សេទីរីហ្ស៊ីន 10mg (គ្រាប់)", "tablet", 100, "pharma", 0.02, 730, 11, 300, ["cetirizine"]),
    ("ors-sachet", "ORS oral rehydration salts sachet", "អូអរអេស ម្សៅទឹកអំបិលស្ករ", "sachet", 50, "pharma", 0.12, 730, 3, 40, ["ors"]),
    ("berocca", "Berocca effervescent tube", "វីតាមីន Berocca (គ្រាប់រលាយ)", "tube", 12, "pharma", 3.60, 540, 0.4, 8, ["berocca"]),
    ("vitc-1000", "Vitamin C 1000mg effervescent tube", "វីតាមីន C 1000mg (គ្រាប់រលាយ)", "tube", 12, "pharma", 2.10, 365, 0.03, 8, ["vitamin c"]),
]
LOOSE = {"para-500": (10, 100), "cetirizine-10": (10, 100)}    # sold by the tablet: (strip, box)
CRITICAL = {"dumex-400", "para-500", "ors-sachet"}

# place: kind, levels, centre on the floor (x, z in metres) and quarter turns; then level: [(sku, facings)]
PLACES = {
    "Drinks fridge": ("fridge", 4, (1.6, 1.0, 0), {
        1: [("coke-330", 3), ("sprite-1500", 2), ("vital-500", 2)],
        2: [("pocari-500", 2), ("ichitan-420", 2), ("oishi-380", 2)],
        3: [("dutchlady-choc", 2), ("dutchlady-straw", 2), ("milo-uht-180", 2)],
        4: [("sting-330", 3), ("aquafina-500", 2), ("fanta-330", 2)]}),       # Sting, a best seller, at the bottom
    "Beer fridge": ("fridge", 3, (3.9, 1.0, 0), {
        1: [("angkor-330", 4)], 2: [("cambodia-330", 3), ("tiger-330", 2)], 3: [("heineken-330", 3)]}),
    "Snack shelf": ("shelf", 4, (1.9, 3.4, 0), {
        1: [("pringles-107", 2), ("chocopie-12", 2)], 2: [("lays-50", 3), ("oreo-133", 2)],
        3: [("kitkat-4f", 2), ("snickers-50", 2)], 4: [("cornflakes", 2)]}),
    "Noodle & coffee shelf": ("shelf", 4, (4.4, 3.4, 0), {
        1: [("nutella-200", 1), ("ovaltine-400", 2), ("milo-400", 2)], 2: [("nescafe-3in1", 2), ("condensed-milk", 2)],
        3: [("mama-pork", 3), ("nissin-cup", 2)], 4: [("maggi-sauce", 2)]}),
    "Household shelf": ("shelf", 4, (1.9, 5.2, 0), {
        1: [("colgate-150", 2)], 2: [("lifebuoy-soap", 2)], 4: [("coke-1500", 3)]}),
    "Medicine cabinet": ("shelf", 4, (7.3, 4.0, 3), {
        1: [("berocca", 2), ("vitc-1000", 1)], 2: [("para-500", 2), ("para-250", 1)],
        3: [("cetirizine-10", 1), ("ors-sachet", 2)], 4: [("dumex-400", 3)]}),
    "Counter": ("counter", 1, (5.6, 6.0, 0), {1: [("mentos-mint", 1), ("halls-mentho", 1), ("wrigley-spearmint", 1)]}),
    "Back room": ("storage", 3, (6.4, 0.9, 0), {
        1: [("oishi-380", 3)], 2: [("angkor-330", 3), ("vital-500", 2)], 3: [("coke-330", 2)]}),
}
BACK = {"oishi-380": 24, "angkor-330": 48, "vital-500": 48, "coke-330": 24}   # opening stock kept in the back room

# The counter photos: products with cut-outs, their size on the mat (long side, px on the 1920-wide mat).
CUT = {"coke-330": ("coke-330", 480), "sting-330": ("sting-330", 540), "ichitan-420": ("ichitan-420", 720),
       "oishi-380": ("oishi-380", 720), "sprite-1500": ("sprite-1500", 700), "mama-pork": ("mama-pork", 600),
       "lays-50": ("lays-50", 700), "dutchlady-choc": ("dutchlady-choc", 450), "berocca": ("berocca", 490),
       "ors-sachet": ("ors-sachet", 490)}
TAUGHT_AT_SALES = {"mama-pork"}   # no photos at the start: learned while selling


def lot(rng: random.Random, day: date) -> str:
    return f"L{day:%y%m}{rng.choice('ABCDEFGHJK')}{rng.randint(10, 99)}"


def jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


class Mat:
    """Product cut-outs laid on the real counter mat, turned, with a soft contact shadow."""

    def __init__(self, assets: Path) -> None:
        self.mat = Image.open(assets / "mat.jpg").convert("RGB")
        self.cuts = {k: Image.open(assets / "cut" / f"{key}.png").convert("RGBA") for k, (key, _) in CUT.items()}

    def scene(self, items: list[tuple[str, float, float, float]]) -> Image.Image:
        out = self.mat.copy()
        for sku, fx, fy, angle in items:
            im = self.cuts[sku]
            k = CUT[sku][1] / max(im.size)
            im = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))), Image.LANCZOS)
            im = im.rotate(angle, resample=Image.BICUBIC, expand=True)
            x, y = int(fx * out.width - im.width / 2), int(fy * out.height - im.height / 2)
            shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
            shadow.putalpha(im.split()[3].point(lambda v: int(v * 0.45)).filter(ImageFilter.GaussianBlur(9)))
            out.paste(shadow, (x + 7, y + 9), shadow)
            out.paste(im, (x, y), im)
        return out


def build(cfg, assets: Path, seed: int = 11) -> dict:
    rng = random.Random(seed)
    nrng = np.random.default_rng(seed)
    now = datetime.now().replace(microsecond=0)
    today = now.date()
    start = today - timedelta(weeks=WEEKS)
    at = lambda d, h, m=0: datetime.combine(d, time(h, m))  # noqa: E731

    for p in (cfg.db_path, Path(f"{cfg.db_path}-wal"), Path(f"{cfg.db_path}-shm")):
        p.unlink(missing_ok=True)
    shutil.rmtree(cfg.media_dir, ignore_errors=True)
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.db_path)
    db.init_schema(conn)
    clock.freeze(at(start, 7))

    # --- the shop: suppliers, products, places --------------------------------------------------
    sup = {}
    for key, (name, emoji, weekday, every, min_order, contact) in SUPPLIERS.items():
        sup[key] = catalog.add_supplier(conn, name, weekday, emoji=emoji, min_order=min_order, contact=contact)
        conn.execute("UPDATE suppliers SET visit_every_days = ? WHERE id = ?", (every, sup[key]))
    credits = json.loads((assets / "credits.json").read_text(encoding="utf-8"))
    pid, spec = {}, {}
    for sku, en, km, unit, case, s, cost, life, rate, opening, spellings in PRODUCTS:
        code = (credits.get(sku) or {}).get("barcode")
        pid[sku] = catalog.add_product(conn, sku=sku, name_en=en, name_km=km, unit=unit, case_size=case,
                                       supplier_id=sup[s], is_critical=sku in CRITICAL, shelf_life_days=life,
                                       unit_cost=cost, barcode=code if code and code.isdigit() and len(code) >= 8 else None)
        conn.execute("UPDATE products SET added_at = ? WHERE id = ?", (clock.stamp(), pid[sku]))
        for text in spellings:
            catalog.add_alias(conn, pid[sku], text, "invoice")
        if sku in LOOSE:
            strip, box = LOOSE[sku]
            conn.execute("UPDATE products SET sold_loose = 1 WHERE id = ?", (pid[sku],))
            catalog.add_pack(conn, pid[sku], "strip", strip)
            catalog.add_pack(conn, pid[sku], "box", box)
        spec[sku] = {"case": case, "supplier": s, "cost": cost, "life": life, "rate": rate, "opening": opening}
    place = {}
    for name, (kind, levels, _floor, _) in PLACES.items():
        place[name] = stockmap.add_place(conn, name, kind, levels)
    shop3d.save(conn, [{"id": place[n], "x": v[2][0], "z": v[2][1], "turn": v[2][2]} for n, v in PLACES.items()])

    # --- onboarding: the owner's spreadsheet, every shelf and an opening count with dates --------
    rows, n = [], 1
    order = [n for n in PLACES if n != "Back room"] + ["Back room"]   # home places first, the back room last
    seen = set()
    for name in order:
        for level, items in PLACES[name][3].items():
            for sku, width in items:
                first = sku not in seen
                seen.add(sku)
                if name == "Back room":
                    qty = BACK.get(sku, 0)
                else:
                    qty = spec[sku]["opening"] - BACK.get(sku, 0) if first else 0
                life = spec[sku]["life"]
                expiry = start + timedelta(days=int(life * rng.uniform(0.45, 0.85)))
                if sku == "chocopie-12":
                    expiry = start + timedelta(days=28)          # a short-dated box: expires within the month
                n += 1
                rows.append({"row": n, "name": sku, "product": pid[sku], "shelf": name, "level": level, "position": None,
                             "quantity": qty, "expiry": expiry.isoformat(), "lot": lot(rng, start), "facings": width,
                             "barcode": ""})
    done = shelfsheet.save(conn, rows, count_stock=True, by="Dara")
    conn.commit()
    print(f"onboarded from a spreadsheet: {done['spots']} spots on {done['places']} places, {done['units']} units counted")

    # --- the camera's photos: the mat, products laid on it, product photos ------------------------
    mat = Mat(assets)
    pipeline.save_reference(conn, cfg, jpeg(mat.scene([])))
    rec = pipeline.get_recognizer(cfg)
    taught = 0
    for sku in CUT:
        if sku in TAUGHT_AT_SALES:
            continue
        pack = catalog.default_pack_id(conn, pid[sku])
        for i in range(6):
            img = mat.scene([(sku, rng.uniform(0.35, 0.65), rng.uniform(0.35, 0.65), i * 60 + rng.uniform(-12, 12))])
            rec.enroll(conn, cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR), pack)
            taught += 1
    for sku, *_ in PRODUCTS:
        photo = assets / "photos" / f"{sku}.jpg"
        if sku in CUT or sku in TAUGHT_AT_SALES or not photo.exists():
            continue
        img = Image.open(photo).convert("RGB")
        img.thumbnail((640, 640))
        pack = catalog.default_pack_id(conn, pid[sku])
        w, h = img.size
        for variant in (img, img.rotate(7, expand=True, fillcolor=(235, 235, 235)),
                        img.crop((int(w * 0.07), int(h * 0.07), int(w * 0.93), int(h * 0.93)))):
            rec.add_example(conn, variant, pack, None, "enrollment")
            taught += 1
    conn.commit()
    print(f"{taught} product photos in the camera's gallery")

    # --- eight weeks: supplier visits and sales ---------------------------------------------------
    on = stock.on_hand(conn)
    stock_of = {sku: on.get(pid[sku], 0) for sku in pid}
    last_visit: dict[str, date] = {}
    # The counter's last two days: yesterday and today, or, built before the shop is busy, the two days before.
    last = today if now.hour >= 10 else today - timedelta(days=1)
    camera_from = last - timedelta(days=1)
    invoices_made = 0
    new_product = None

    def deliver(day: date, s: str, lines: list[tuple[str, int, int | None]], bonus: tuple[str, int] | None = None,
                hour: int = 9, new: bool = False) -> int:
        """An agent's invoice: [(sku, cases, days to expiry or None)], typed in and confirmed; with `new`, a line
        for a product the shop never had, added from the invoice in one tap."""
        nonlocal invoices_made, new_product
        clock.freeze(at(day, hour, rng.randint(0, 40)))
        entries = []
        for sku, cases, days in lines:
            sp = spec[sku]
            life = days if days is not None else int(sp["life"] * rng.uniform(0.55, 0.9))
            entries.append({"product_id": pid[sku], "qty": cases, "unit": "case",
                            "unit_cost": round(sp["cost"] * sp["case"] * rng.uniform(0.97, 1.03), 2),
                            "expiry_date": (day + timedelta(days=life)).isoformat()})
        inv = invoices.create_manual(conn, sup[s], entries)
        for line in invoices.lines(conn, inv):
            conn.execute("UPDATE invoice_lines SET lot_no = ? WHERE id = ?", (lot(rng, day), line["id"]))
        if new:
            new_product = add_new_from_invoice(conn, inv, day)
        clock.freeze(at(day, hour, 50))
        invoices.confirm(conn, inv, by="Dara")
        if bonus:
            invoices.add_bonus(conn, inv, pid[bonus[0]], bonus[1])
        for sku, cases, _ in lines:
            stock_of[sku] += cases * spec[sku]["case"]
            if sku in BACK and cases >= 3:      # big deliveries go to the back room until the shelf has room
                for b in batches.for_invoice(conn, inv):
                    if batches.get(conn, b).product_id == pid[sku]:
                        batches.update(conn, b, location_id=place["Back room"])
        if bonus:
            stock_of[bonus[0]] += bonus[1]
        invoices_made += 1
        return inv

    day = start
    while day <= today:
        weekday = day.weekday()
        # Stories with a date: what the owner ordered "by feeling", and what came short-dated.
        if day == today - timedelta(days=30):
            deliver(day, "drinks", [("pocari-500", 3, 48)], hour=10)           # a promotion: three cases, 48 days left
        if day == today - timedelta(days=14):
            deliver(day, "nestle", [("dutchlady-straw", 1, 12)], hour=10)      # a short-dated case: past it 2 days ago
        if day == today - timedelta(days=36):
            deliver(day, "nestle", [("milo-uht-180", 2, 44)], hour=10)         # 44 days left: put it in front soon
        # The agents' weekly (or fortnightly) visits, ordered by feeling.
        for s, (_, _, wd, every, _, _) in SUPPLIERS.items():
            if wd != weekday or day == start or (s in last_visit and (day - last_visit[s]).days < every):
                continue
            last_visit[s] = day
            lines = []
            for sku, sp in spec.items():
                if sp["supplier"] != s or sp["rate"] <= 0.05:
                    continue
                if sku == "dumex-400" and day > today - timedelta(days=16):
                    continue                                   # forgotten at the last two visits
                need = sp["rate"] * every * 1.4
                if stock_of[sku] < need:
                    cases = max(1, round((need * 1.2 - stock_of[sku]) / sp["case"] + rng.gauss(0, 0.4)))
                    if rng.random() < 0.18:
                        cases += 1                             # a little extra, to be safe
                    lines.append((sku, cases, None))
            new = s == "beer" and day > today - timedelta(days=7) and new_product is None
            if lines or new:
                deliver(day, s, lines, bonus=("angkor-330", 6) if s == "beer" and rng.random() < 0.5 else None, new=new)
        # Sales: customers, by the hour. The camera takes over the last two days for products it knows.
        dow = 1.25 if weekday >= 5 else 0.93
        for sku, sp in spec.items():
            if day >= camera_from and sku in CUT:
                continue
            rate = sp["rate"] * dow
            if sku in ("angkor-330", "cambodia-330", "tiger-330", "heineken-330") and weekday in (4, 5):
                rate *= 1.5
            if sku in ("para-500", "ors-sachet") and day > today - timedelta(days=21):
                rate *= 1.3                                    # fever season
            if day == today:
                rate *= max(0.0, min(1.0, (now.hour - 7) / 14))
            demand = int(nrng.poisson(rate)) if rate > 0 else 0
            if sku == "dumex-400" and day >= today - timedelta(days=3):
                demand = stock_of[sku]                         # the last tins go
            sold = min(demand, stock_of[sku])
            if sold <= 0:
                continue
            chunks = max(1, min(sold, rng.randint(1, 4)))
            for k in range(chunks):
                part = sold // chunks + (1 if k < sold % chunks else 0)
                hour = rng.randint(7, 20) if day < today else rng.randint(7, max(7, now.hour - 1))
                stock.record(conn, pid[sku], -part, "sale", ref_type="sim", simulated=True,
                             at=at(day, hour, rng.randint(0, 59)))
            stock_of[sku] -= sold
        # The rest of the shop's life, on its day.
        if day == today - timedelta(days=20):
            clock.freeze(at(day, 18, 10))
            stock.adjust(conn, pid["lays-50"], "damaged", 2, by="Sokha")
            stock_of["lays-50"] -= 2
        if day == today - timedelta(days=9):
            clock.freeze(at(day, 19, 5))
            stock.adjust(conn, pid["coke-330"], "family_use", 2, by="Dara")
            stock_of["coke-330"] -= 2
        if day == today - timedelta(days=4):
            clock.freeze(at(day, 20, 30))
            stock.adjust(conn, pid["angkor-330"], "credit_sale", 6, by="Dara")   # a neighbour pays at the end of the month
            stock_of["angkor-330"] -= 6
        if day == today - timedelta(days=12):                  # the weekly shelf check: expired Choco Pie off
            clock.freeze(at(day, 8, 35))
            for b in batches.open_batches(conn, pid["chocopie-12"]):
                if b.expiry and b.expiry < day.isoformat():
                    stock_of["chocopie-12"] -= batches.remove(conn, b.id, reason="expired", by="shelf check")
        if day == today - timedelta(days=7):                   # the afternoon quick counts
            clock.freeze(at(day, 15, 0))
            for sku, off in (("sting-330", 0), ("lays-50", -1), ("mentos-mint", 0)):
                counted = stock_of[sku] + off
                stock.count(conn, pid[sku], counted, "spot_check", by="Sokha")
                stock_of[sku] = counted
        if day == today - timedelta(days=5):                   # a pack's lot isn't the one on its invoice
            clock.freeze(at(day, 16, 20))
            row = conn.execute("SELECT id FROM stock_movements WHERE product_id = ? AND ref_type = 'invoice_line' AND qty > 0 "
                               "AND lot_no IS NOT NULL ORDER BY id DESC LIMIT 1", (pid["angkor-330"],)).fetchone()
            if row:
                batches.check_lot(conn, row["id"], f"L{day:%y%m}X77")
        conn.commit()
        day += timedelta(days=1)
    print(f"{invoices_made} invoices; sales simulated from {start} to {today}")

    # Last night the owner refilled the fridges and shelves from the back room: anything whose own place had
    # run empty got the back room's soonest-dated case brought out.
    homes = dict(conn.execute("SELECT id, location_id FROM products").fetchall())
    for sku in BACK:
        open_ = batches.open_batches(conn, pid[sku])
        if not any(b.location_id == homes[pid[sku]] for b in open_):
            back = [b for b in open_ if b.location_id == place["Back room"]]
            if back:
                batches.update(conn, back[0].id, location_id=homes[pid[sku]])
    conn.commit()

    # --- the counter camera: yesterday and this morning -----------------------------------------
    learner = pipeline.learner(cfg)
    users = [(7001, "Dara", "en", "owner"), (7002, "Sokha", "km", "verifier")]
    for chat, name, lang, role in users:
        conn.execute("INSERT INTO bot_users (chat_id, name, role, lang, created_at) VALUES (?, ?, ?, ?, ?)",
                     (chat, name, role, lang, clock.stamp(at(start, 7, 30))))
    weights = {"sting-330": 6, "coke-330": 5, "lays-50": 3, "mama-pork": 4, "dutchlady-choc": 3, "ichitan-420": 2,
               "sprite-1500": 2, "berocca": 1, "ors-sachet": 1}                 # Oishi hardly sells: only beside Ichitan
    times = [at(last - timedelta(days=1), h, m) for h, m in ((7, 40), (8, 25), (9, 10), (10, 5), (11, 30), (12, 15),
                                                                (13, 40), (15, 5), (16, 30), (17, 20), (18, 10), (19, 0),
                                                                (19, 45), (20, 20))]
    times += [t for t in (at(last, h, m) for h, m in ((7, 15), (7, 50), (8, 35), (9, 20), (10, 10), (11, 0),
                                                       (12, 5), (13, 30), (14, 45))) if t < now - timedelta(minutes=20)]
    def truth(item, placed):
        x, y, w, h = json.loads(item["bbox"])
        cx, cy = x + w / 2, y + h / 2
        return min(placed, key=lambda p: (p[1] - cx) ** 2 + (p[2] - cy) ** 2)[0]

    made, taps, best = [], [], []
    answered = 0
    for camera_day in (last - timedelta(days=1), last):
        for when in [t for t in times if t.date() == camera_day]:
            k = rng.choices([1, 2, 3], weights=[5, 3, 1])[0]
            picks = rng.choices(list(weights), weights=list(weights.values()), k=k)
            if rng.random() < 0.08:
                picks = ["ichitan-420", "oishi-380"]           # the look-alike pair, side by side
            spots = [(0.3, 0.38), (0.62, 0.42), (0.45, 0.68)]
            items = [(sku, spots[i][0] + rng.uniform(-0.05, 0.05), spots[i][1] + rng.uniform(-0.05, 0.05),
                      rng.uniform(0, 360)) for i, sku in enumerate(picks)]
            img = mat.scene(items)
            clock.freeze(when)
            txn = transactions.open_transaction(conn, "camera")
            name = f"t{txn}_demo"
            found = rec.recognize(conn, cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR), name, use_reference=True)
            photo = pipeline.save_frame(cfg, jpeg(img), name)
            transactions.add_frame(conn, txn, photo, [d.record for d in found if not d.ignored], cfg.media_dir)
            transactions.add_ignored(conn, txn, [(d.ignored, d.record) for d in found if d.ignored], cfg.media_dir)
            clock.freeze(when + timedelta(minutes=1))
            transactions.close_transaction(conn, txn, cfg.media_dir, cfg.verification.delete_photos_after_verify,
                                           cfg.recognition.tray)
            placed = [(sku, fx * img.width, fy * img.height) for sku, fx, fy, _ in items]
            made.append((txn, when, placed))
            # The questions, answered on Telegram: most of them; some of today's are still waiting.
            for item in conn.execute("SELECT * FROM transaction_items WHERE txn_id = ? AND status = 'pending'",
                                     (txn,)).fetchall():
                if rng.random() < (0.8 if camera_day == last else 0.15):
                    continue
                chat, who = rng.choice(users)[:2]
                answer = when + timedelta(minutes=rng.randint(20, 150))
                clock.freeze(answer)
                right = catalog.default_pack_id(conn, pid[truth(item, placed)])
                if item["pack_id"] == right:
                    transactions.resolve(conn, item["id"], "yes", by=f"telegram:{who}", learn=learner,
                                         media_dir=cfg.media_dir, delete_photo=True)
                    taps.append((chat, "yes", answer))
                else:
                    transactions.resolve(conn, item["id"], "pick", pack_id=right, by=f"telegram:{who}", learn=learner,
                                         media_dir=cfg.media_dir, delete_photo=True)
                    taps.append((chat, "pick", answer))
                answered += 1
            conn.commit()
        if camera_day < last:
            clock.freeze(at(camera_day, 22, 0))                # the end of the day: what is left becomes best guesses
            best += transactions.best_guess_all(conn, cfg.media_dir, delete_photo=True)
            conn.commit()
    for chat, action, when in taps:
        conn.execute("INSERT INTO taps (chat_id, action, created_at) VALUES (?, ?, ?)", (chat, action, clock.stamp(when)))
    for h, m in ((9, 30), (13, 0), (20, 30)):
        conn.execute("INSERT INTO digests (sent_at, item_count) VALUES (?, ?)",
                     (clock.stamp(at(last - timedelta(days=1), h, m)), rng.randint(1, 4)))

    # --- today: alerts, the order lists, and every movement labelled simulated -----------------
    clock.freeze(now)
    fresh = alerts.new_stock_alerts(conn, cfg.forecast.expiry_warning_days)
    forecast.save(conn, forecast.build_plans(conn, cfg.forecast))
    conn.execute("UPDATE stock_movements SET simulated = 1")
    conn.commit()
    clock.freeze(None)
    m = stockmap.stock_map(conn, cfg)
    status = dict(conn.execute("SELECT status, COUNT(*) FROM transaction_items GROUP BY status").fetchall())
    out = {
        "products": len(PRODUCTS) + (1 if new_product else 0), "places": len(PLACES), "invoices": invoices_made,
        "movements": conn.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0],
        "camera sales": len(made), "items": status, "answered on Telegram": answered, "best guesses": len(best),
        "alerts": [r["name_en"] for r in fresh],
        "expired batches": sum("expired" in b["status"] for b in m["batches"]),
        "expiring batches": sum("expiring" in b["status"] for b in m["batches"]),
        "won't sell in time": sum("clear" in b["status"] for b in m["batches"]),
        "sleeping money": round(sum(b["value"] for b in m["batches"] if "sleeping" in b["status"]), 2),
        "out of stock": [o["name"] for o in m["out"]], "no place yet": m["unplaced"],
        "suggested move": (m["move"] or {}).get("why"),
        "gallery": conn.execute("SELECT COUNT(*) FROM gallery_images").fetchone()[0],
    }
    conn.close()
    return out


def add_new_from_invoice(conn, inv: int, day: date) -> int:
    """A product the agent brought that the shop never had: added from its invoice line in one tap, and
    not put away yet (it has no place)."""
    cur = conn.execute(
        "INSERT INTO invoice_lines (invoice_id, product_id, text_as_written, qty, unit, units_total, unit_cost, "
        "expiry_date, matched_by, name_suggested, name_suggested_km, units_per_case, unit_name) "
        "VALUES (?, NULL, 'Ganzberg 330 can x24', 1, 'case', NULL, 12.0, ?, NULL, 'Ganzberg beer can 330ml', "
        "'ស្រាបៀរ ហ្គាន់ប៊ឺក កំប៉ុង 330ml', 24, 'can')", (inv, (day + timedelta(days=300)).isoformat()))
    return invoices.new_product_for_line(conn, cur.lastrowid)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", default=str(ROOT / "configs" / "demo.yaml"))
    ap.add_argument("--assets", default=str(ROOT / "data" / "demo" / "assets"))
    args = ap.parse_args()
    cfg = load_config(args.config)
    if "demo" not in str(cfg.db_path):
        sys.exit("This builds a demo database from scratch: point it at a demo config, never the shop's.")
    out = build(cfg, Path(args.assets))
    for k, v in out.items():
        print(f"{k:20} {v}")
    print(f"\nStart it:  python -m luydek --config {args.config} --port 8030")


if __name__ == "__main__":
    main()
