"""A shelf from a photo of it: the products the camera finds, level by level, left to right.

Items on one level stand on the same board, so their bottoms line up even when one is a tall bottle
and the next a small box: a level is the items whose bottoms are close, from the top of the photo
down. Neighbours that are the same product are one spot, as many facings wide as there are of them;
neighbours the camera doesn't know but that look alike are one question, as on Stock in.
The owner checks it before anything is saved: what the camera isn't sure of is asked about, with the
closest products to choose from, and becomes a new product if it is none of them. Price tags and
shelf labels are set aside, shown with the level they are on, for the owner to bring back any that
is a product after all.
"""

from __future__ import annotations

import sqlite3
from statistics import median

import numpy as np

from . import catalog
from .stockin import SAME_THING

BOARD = 0.4  # bottoms closer than this share of the items' height stand on the same board


def levels(boxes: list[tuple[int, int, int, int]]) -> list[list[int]]:
    """Indexes of the boxes level by level, top to bottom, each level left to right."""
    out: list[list[int]] = []
    for i in sorted(range(len(boxes)), key=lambda i: boxes[i][3]):
        x0, y0, x1, y1 = boxes[i]
        if out:
            level = out[-1]
            bottom = median(boxes[j][3] for j in level)
            tall = median(boxes[j][3] - boxes[j][1] for j in level)
            if y1 - bottom <= BOARD * min(tall, y1 - y0):
                level.append(i)
                continue
        out.append([i])
    return [sorted(level, key=lambda i: boxes[i][0] + boxes[i][2]) for level in out]


def _alike(a, b) -> bool:
    if a is None or b is None:
        return False
    a, b = np.asarray(a, np.float32), np.asarray(b, np.float32)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)) >= SAME_THING


def proposal(conn: sqlite3.Connection, items: list, media_url: str = "/media/") -> dict:
    """The shelf as the photo shows it, for the owner to check: levels of spots, each with the product
    it looks like (None: the camera can't tell), whether that is sure, the products to offer, and the
    photos of it."""
    product_of = {}
    for it in items:
        for pack, _ in [(it.pack_id, 0.0)] + list(it.candidates):
            if pack is not None and pack not in product_of:
                pk = catalog.pack(conn, pack)
                product_of[pack] = pk["product_id"] if pk is not None else None
    active = {r[0] for r in conn.execute("SELECT id FROM products WHERE active = 1")}
    standing = [i for i, it in enumerate(items) if not it.aside]
    out, aside = [], []
    rows = [[standing[j] for j in level] for level in levels([items[i].box for i in standing])]
    for it in items:
        if it.aside:
            bottoms = [median(items[j].box[3] for j in row) for row in rows]
            near = min(range(len(rows)), key=lambda k: abs(bottoms[k] - it.box[1])) if rows else 0
            guess = product_of.get(it.pack_id)
            aside.append({"level": near, "guess": guess if guess in active else None, "crops": [media_url + it.crop_path],
                          "crop_paths": [it.crop_path]})
    for level in rows:
        spots: list[dict] = []
        for i in level:
            it = items[i]
            product = product_of.get(it.pack_id)
            product = product if product in active else None
            sure = it.tier == "high" and product is not None
            offer = list(dict.fromkeys(p for p in (product_of.get(pk) for pk, _ in it.candidates[:4])
                                       if p in active and p != product))[:3]
            mine = product if it.tier != "low" else None   # an unsure one is never put into a known spot
            last = spots[-1] if spots else None
            if last is not None and last["sure"] == sure and (
                    (mine is not None and last["product"] == mine)
                    or (mine is None and last["product"] is None and _alike(last["_vec"], it.vec))):
                last["width"] += 1                    # the next one along, the same product: one spot, wider
                last["crops"].append(media_url + it.crop_path)
                last["crop_paths"].append(it.crop_path)
                continue
            spots.append({"product": mine, "guess": product, "sure": sure, "width": 1, "offer": offer, "score": it.score,
                          "crops": [media_url + it.crop_path], "crop_paths": [it.crop_path], "_vec": it.vec})
        for spot in spots:
            del spot["_vec"]
        out.append(spots)
    return {"levels": out, "aside": aside, "found": len(items)}
