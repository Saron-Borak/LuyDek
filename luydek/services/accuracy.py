"""How right the camera is in this shop: look-alike pairs, and the accuracy test.

Look-alike pairs are found from the shop's own photos (each matched against all the others, as if
it were a sale), so the owner learns which products the camera mixes up while setting up, not at
the counter. The accuracy test is a known product put on the mat in the shop's own light: what the
camera said, and whether it would have recorded the sale alone. "Wrong and sure" is the number that
matters: a wrong sale recorded with nobody asked.
"""

from __future__ import annotations

import sqlite3

from .. import clock
from ..config import RecognitionCfg
from ..recognition.matcher import Gallery
from . import catalog

LIGHTING = {"daylight": "Daylight", "lamp": "Evening lamp", "dim": "Dim or shadowed"}


def lookalike_pairs(conn: sqlite3.Connection, rc: RecognitionCfg) -> list[dict]:
    """Pairs of packs the camera mixes up, most mixed-up first."""
    gallery = Gallery.load(conn)
    out = []
    for pair, n in gallery.pairs(rc.top_k_per_pack, rc.lookalike_gap).items():
        a, b = (catalog.pack(conn, p) for p in sorted(pair))
        if a is not None and b is not None:
            out.append({"a": a, "b": b, "n": n, "label_a": catalog.pack_label(a, "en"),
                        "label_b": catalog.pack_label(b, "en")})
    return sorted(out, key=lambda r: (-r["n"], r["label_a"]))


def record(conn: sqlite3.Connection, lighting: str, expected_pack: int, detections: list) -> list[dict]:
    """One test photo: every item the camera found is a test of the expected product."""
    if lighting not in LIGHTING:
        raise ValueError("pick the light first")
    if catalog.pack(conn, expected_pack) is None:
        raise ValueError("that product is gone")
    out = []
    for d in detections:
        r = d.record
        got = None if d.ignored else r.pack_id
        tier = "ignored" if d.ignored else r.tier
        conn.execute(
            "INSERT INTO accuracy_tests (lighting, expected_pack, got_pack, tier, score, hint, method, crop_path, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lighting, expected_pack, got, tier, r.confidence, d.ignored or r.hint, r.method, r.crop_path,
             clock.stamp()))
        got_row = catalog.pack(conn, got) if got else None
        out.append({"got": catalog.pack_label(got_row, "en") if got_row else (d.ignored or "?"),
                    "right": got == expected_pack, "sure": tier == "high", "hint": d.ignored or r.hint,
                    "url": f"/media/{r.crop_path}" if r.crop_path else None})
    return out


def summary(conn: sqlite3.Connection) -> dict:
    """Per light: tests, right and recorded alone, asked, and wrong but recorded alone; and the mix-ups."""
    rows = conn.execute("SELECT * FROM accuracy_tests ORDER BY id").fetchall()
    by_light = []
    for key, name in LIGHTING.items():
        tests = [r for r in rows if r["lighting"] == key]
        if not tests:
            continue
        right = [r for r in tests if r["got_pack"] == r["expected_pack"]]
        by_light.append({
            "key": key, "name": name, "n": len(tests),
            "right": len(right),
            "auto_right": sum(1 for r in right if r["tier"] == "high"),
            "asked": sum(1 for r in tests if r["tier"] != "high"),
            "auto_wrong": sum(1 for r in tests if r["tier"] == "high" and r["got_pack"] != r["expected_pack"]),
        })
    mixups: dict[tuple, int] = {}
    for r in rows:
        if r["got_pack"] != r["expected_pack"]:
            key = (r["expected_pack"], r["got_pack"], r["hint"] if r["got_pack"] is None else None)
            mixups[key] = mixups.get(key, 0) + 1
    names = {}
    for exp, got, _ in mixups:
        for p in (exp, got):
            if p and p not in names:
                pk = catalog.pack(conn, p)
                names[p] = catalog.pack_label(pk, "en") if pk else "(removed)"
    return {
        "lights": by_light, "total": len(rows),
        "mixups": sorted(({"expected": names[e], "got": names.get(g) if g else (h or "nothing"), "n": n}
                          for (e, g, h), n in mixups.items()), key=lambda m: -m["n"]),
    }


def reset(conn: sqlite3.Connection) -> int:
    return conn.execute("DELETE FROM accuracy_tests").rowcount
