"""The shop in 3D: every place a rack on the shop floor, every product a box on its level.

A warehouse visualisation for a mini-mart. Each storage spot is a box coloured by any measure
(days to expiry, units, sleeping money, sales…), and the whole scene turns, tilts and zooms under the
mouse or a finger. The racks are the shop's own places, drawn from what stands where on each
shelf (stockmap.py). They are laid out on the floor by what they are (fridges along the back wall,
shelves in rows, the counter at the front) until the owner drags them where they really are. Sizes
are in metres, about a small shop's.

It also suggests one move, the shop's version of re-slotting the heaviest pallet next to the dock: a
best seller in a spot that is hard to reach (the bottom or the top of a shelf, far from the counter)
changes places with a slow seller holding a spot at hand height near the counter. Customers find
what is at hand height first, and what sells most is restocked most often. Fridge stock only swaps
with fridge stock.
"""

from __future__ import annotations

import math
import sqlite3

FACING = 0.25                    # metres a product takes on a level, side by side
MIN_FACINGS = 4                  # an empty or nearly empty rack is still drawn shelf-wide
LEVEL_H = {"shelf": 0.42, "fridge": 0.40, "counter": 0.40, "storage": 0.50, "other": 0.42}
BASE = {"shelf": 0.12, "fridge": 0.22, "counter": 0.90, "storage": 0.10, "other": 0.30}   # the lowest board
DEPTH = {"shelf": 0.45, "fridge": 0.70, "counter": 0.60, "storage": 0.60, "other": 0.50}
AISLE = 1.4                      # between rows of shelves
GAP = 0.5                        # between racks side by side
PER_ROW = 4
MARGIN = 1.0                     # floor around everything
HAND = (0.85, 1.6)               # board heights a customer reaches without bending or stretching
CUSTOMER_KINDS = ("shelf", "fridge", "counter", "other")   # the back room is not where customers pick
MIN_SOLD = 8                     # a best seller sold at least this many in 4 weeks...
TIMES = 3                        # ...and this many times what the slow seller sold


def board(kind: str, levels: int, level: int) -> float:
    """The height (m) of the board a level's products stand on; level 1 is the top."""
    return BASE.get(kind, 0.3) + (levels - level) * LEVEL_H.get(kind, 0.42)


def size(place: dict) -> tuple[float, float, float]:
    """(width, depth, height) of a place's rack, from its levels and what stands on them."""
    kind, levels = place["kind"], place["levels"]
    widest = max([sum(f["width"] for f in place["facings"] if f["level"] == lv) for lv in range(1, levels + 1)] + [0])
    w = max(MIN_FACINGS, widest) * FACING + 0.1
    h = board(kind, levels, 1) + LEVEL_H.get(kind, 0.42) + 0.04
    return round(w, 3), DEPTH.get(kind, 0.5), round(h, 3)


def arrange(places: list[dict]) -> dict:
    """Each place's spot on the floor (`floor`: centre x, z, quarter turns, size), where the owner put it or
    laid out by what it is; and the floor's size. The front of the shop is towards +z, a rack's front too
    unless turned."""
    back, rows, front = [], [], []
    for p in places:
        w, d, h = size(p)
        p["floor"] = {"w": w, "d": d, "h": h, "turn": int(p.get("floor_turn") or 0) % 4,
                      "boards": [round(board(p["kind"], p["levels"], lv), 3) for lv in range(1, p["levels"] + 1)],
                      "lh": LEVEL_H.get(p["kind"], 0.42)}
        if p.get("floor_x") is not None and p.get("floor_z") is not None:
            p["floor"].update(x=round(float(p["floor_x"]), 3), z=round(float(p["floor_z"]), 3), auto=False)
        elif p["kind"] in ("fridge", "storage"):
            back.append(p)
        elif p["kind"] == "counter":
            front.append(p)
        else:
            rows.append(p)
    z = MARGIN
    if back:                                      # along the back wall, facing the shop
        x = MARGIN
        depth = max(p["floor"]["d"] for p in back)
        for p in back:
            p["floor"].update(x=round(x + p["floor"]["w"] / 2, 3), z=round(z + depth / 2, 3), auto=True)
            x += p["floor"]["w"] + GAP
        z += depth + AISLE
    for i in range(0, len(rows), PER_ROW):        # shelves in rows, side by side, facing the front
        row = rows[i:i + PER_ROW]
        x = MARGIN
        depth = max(p["floor"]["d"] for p in row)
        for p in row:
            p["floor"].update(x=round(x + p["floor"]["w"] / 2, 3), z=round(z + depth / 2, 3), auto=True)
            x += p["floor"]["w"] + GAP
        z += depth + AISLE
    placed = [p for p in places if "x" in p["floor"] and p not in front]
    right = max([p["floor"]["x"] + _half(p)[0] for p in placed] + [MARGIN + 4.0])
    x = right
    for p in front:                               # the counter at the front, on the right
        p["floor"].update(x=round(x - p["floor"]["w"] / 2, 3), z=round(z + p["floor"]["d"] / 2, 3), auto=True)
        x -= p["floor"]["w"] + GAP
    x0 = min([p["floor"]["x"] - _half(p)[0] for p in places] + [MARGIN]) - MARGIN
    z0 = min([p["floor"]["z"] - _half(p)[1] for p in places] + [MARGIN]) - MARGIN
    x1 = max([p["floor"]["x"] + _half(p)[0] for p in places] + [MARGIN + 4.0]) + MARGIN
    z1 = max([p["floor"]["z"] + _half(p)[1] for p in places] + [MARGIN + 3.0]) + MARGIN
    return {"x0": round(x0, 3), "z0": round(z0, 3), "x1": round(x1, 3), "z1": round(z1, 3), "facing": FACING,
            "margin": MARGIN, "hand": list(HAND)}


def _half(p: dict) -> tuple[float, float]:
    """Half the rack's footprint along x and z, turned or not."""
    f = p["floor"]
    return (f["d"] / 2, f["w"] / 2) if f["turn"] % 2 else (f["w"] / 2, f["d"] / 2)


def save(conn: sqlite3.Connection, items: list[dict]) -> int:
    """Where the owner put each place on the floor (centre x, z in metres, quarter turns); `auto`: laid
    out on its own again. Returns how many places changed."""
    n = 0
    for it in items:
        if it.get("auto"):
            x = z = turn = None
        else:
            x = max(-60.0, min(60.0, float(it["x"])))
            z = max(-60.0, min(60.0, float(it["z"])))
            turn = int(it.get("turn") or 0) % 4
        n += conn.execute("UPDATE locations SET floor_x = ?, floor_z = ?, floor_turn = ? WHERE id = ? AND active = 1",
                          (None if x is None else round(x, 3), None if z is None else round(z, 3), turn,
                           int(it["id"]))).rowcount
    return n


def _where(spot: dict, good: bool) -> str:
    """Where a spot is, in the words of the suggestion: why it is hard to reach, or why it is easy."""
    p, levels, level = spot["place"], spot["place"]["levels"], spot["level"]
    if good:
        return (f"a spot at hand height on {p['name']}" + (", near the counter" if spot["reach"] >= 0.85 else "")
                if spot["hand"] else f"level {level} of {p['name']}, near the counter")
    if spot["hand"]:
        return f"{p['name']}, far from the counter"
    if levels > 1 and level == levels:
        return f"the bottom level of {p['name']}"
    if levels > 1 and level == 1:
        return f"the top level of {p['name']}"
    return f"level {level} of {p['name']}, below hand height" if spot["height"] < HAND[0] else f"level {level} of {p['name']}"


def suggest(places: list[dict], products: dict) -> dict | None:
    """One swap worth making, or None: a best seller in a hard-to-reach spot for a slow seller in an easy
    one (at hand height, near the counter). Only with sales enough to say so, from the ledger."""
    counter = next((p for p in places if p["kind"] == "counter" and "x" in p.get("floor", {})), None)
    far = max([math.dist((p["floor"]["x"], p["floor"]["z"]), (counter["floor"]["x"], counter["floor"]["z"]))
               for p in places if counter] + [1.0])
    spots = []
    for p in places:
        if p["kind"] not in CUSTOMER_KINDS:
            continue
        at: dict[int, int] = {}
        for f in p["facings"]:
            at[f["level"]] = at.get(f["level"], 0) + 1
            info = products.get(f["product"]) or products.get(str(f["product"]))
            if not info:
                continue
            height = board(p["kind"], p["levels"], f["level"])
            hand = HAND[0] <= height <= HAND[1]
            near = 1 - math.dist((p["floor"]["x"], p["floor"]["z"]),
                                 (counter["floor"]["x"], counter["floor"]["z"])) / far if counter else 0.5
            spots.append({"place": p, "level": f["level"], "index": at[f["level"]] - 1, "product": f["product"],
                          "sold": info.get("sold_28d") or 0, "hand": hand, "height": height, "cold": p["kind"] == "fridge",
                          "reach": round(0.7 * (1.0 if hand else 0.4) + 0.3 * near, 3)})
    best, score = None, 0.0
    for a in spots:                                   # a: sells well, badly placed
        if a["sold"] < MIN_SOLD:
            continue
        for b in spots:                               # b: sells little, well placed
            if (b["product"] == a["product"] or a["sold"] < TIMES * max(b["sold"], 1) or b["reach"] - a["reach"] < 0.25
                    or a["cold"] != b["cold"]):     # what is kept cold stays in a fridge, and the rest out of one
                continue
            s = (a["sold"] - b["sold"]) * (b["reach"] - a["reach"])
            if s > score:
                best, score = (a, b), s
    if best is None:
        return None
    a, b = best
    name = lambda pid: (products.get(pid) or products.get(str(pid)))["name"]  # noqa: E731
    return {
        "from": {"place": a["place"]["id"], "level": a["level"], "index": a["index"], "product": a["product"]},
        "to": {"place": b["place"]["id"], "level": b["level"], "index": b["index"], "product": b["product"]},
        "sold_from": a["sold"], "sold_to": b["sold"],
        "why": (f"{name(a['product'])} sold {a['sold']} in the last 4 weeks but stands on {_where(a, False)}; "
                f"{name(b['product'])} sold {b['sold']} and has {_where(b, True)}. Swap them: what sells most "
                "goes where customers reach it first."),
    }
