"""SIMULATED counter test: how LuyDek does on hundreds of sales with a known answer.

Real photos of this shop's products (the gallery) are cut out of their photos and
laid at random on an empty counter: 1-4 items a sale, some touching, some sales
with a stray non-product. Each sale goes through the real recognition steps
(find items against the empty-counter photo, split touching ones, CLIP match,
colour tie-break, confidence tiers) and is scored against what was placed.

Fair by construction:
  * a photo used in a test sale is never in the gallery that recognises it
    (the photos are split into folds; each fold is tested against the others);
  * the gallery is rebuilt the way a shop enrolls: each training photo placed
    alone on the same counter and cropped by the same segmentation.

Still a simulation: no shadows, no perspective, no moving hands, and each photo
keeps the lighting it was taken in. Use it to compare setups and settings; say
"simulated" wherever its numbers are shown. It only reads the database and photos.

    python scripts/counter_test.py                        # plain dark mat, 5 folds x 80 sales
    python scripts/counter_test.py --background counter   # the shop's own empty-counter photo
    python scripts/counter_test.py --sales 40 --out data/counter_test
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek.config import load_config  # noqa: E402
from luydek.recognition import segment  # noqa: E402
from luydek.recognition.cutout import cutout  # noqa: E402
from luydek.recognition.encoder import get_encoder  # noqa: E402
from luydek.recognition.matcher import Gallery, colour_signature, colour_tiebreak, tier  # noqa: E402

W, H = 1280, 960
IGNORE = 0  # class id of "not a product"


# --- photos -> cut-out products ---------------------------------------------------------------

def transform(rgba: np.ndarray, angle: float, scale: float) -> np.ndarray:
    h, w = rgba.shape[:2]
    rgba = cv2.resize(rgba, (max(4, int(w * scale)), max(4, int(h * scale))), interpolation=cv2.INTER_AREA)
    h, w = rgba.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(rgba, m, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0, 0))


def paste(scene: np.ndarray, obj: np.ndarray, x: int, y: int) -> np.ndarray:
    """Blend `obj` (RGBA) onto `scene` at x, y; returns the object's full-frame mask."""
    h, w = obj.shape[:2]
    a = obj[..., 3:4].astype(np.float32) / 255
    region = scene[y:y + h, x:x + w].astype(np.float32)
    scene[y:y + h, x:x + w] = (region * (1 - a) + obj[..., :3].astype(np.float32) * a).astype(np.uint8)
    mask = np.zeros((H, W), bool)
    mask[y:y + h, x:x + w] = obj[..., 3] > 127
    return mask


# --- counters and camera --------------------------------------------------------------------------

def plain_mat(rng: np.random.Generator) -> np.ndarray:
    """A matte dark-grey counter mat, lit a little unevenly, as the setup guide recommends."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    light = 1 + 0.06 * (xx / W - 0.5) - 0.05 * (yy / H - 0.5)
    base = np.array([58, 62, 66], np.float32) * light[..., None]
    grain = rng.normal(0, 1.5, (H // 4, W // 4, 3)).astype(np.float32)
    base += cv2.resize(grain, (W, H), interpolation=cv2.INTER_LINEAR)
    return np.clip(base, 0, 255).astype(np.uint8)


def capture(scene: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """What the camera hands over: sensor noise, a hint of exposure change, JPEG. Returns BGR."""
    img = scene.astype(np.float32) * rng.uniform(0.985, 1.015) + rng.normal(0, 2.2, scene.shape)
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(np.clip(img, 0, 255).astype(np.uint8), cv2.COLOR_RGB2BGR),
                           [cv2.IMWRITE_JPEG_QUALITY, 90])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


# --- the test ----------------------------------------------------------------------------------------

def load_photos(db_path: Path, media: Path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT g.id, g.crop_path, g.ignore_label, p.name_en FROM gallery_images g "
        "LEFT JOIN product_packs pk ON pk.id = g.pack_id LEFT JOIN products p ON p.id = pk.product_id ORDER BY g.id"
    ).fetchall()
    ref = conn.execute("SELECT value FROM settings WHERE key = 'reference_path'").fetchone()
    conn.close()
    names = sorted({r["name_en"] for r in rows if r["name_en"]})  # one class per product name (duplicates merge)
    cls = {n: i + 1 for i, n in enumerate(names)}
    photos, skipped = [], Counter()
    for r in rows:
        c = IGNORE if r["name_en"] is None else cls[r["name_en"]]
        try:
            obj = cutout(np.asarray(Image.open(media / r["crop_path"]).convert("RGB")))
        except OSError:
            obj = None
        if obj is None:
            skipped["not a product" if c == IGNORE else r["name_en"]] += 1
            continue
        photos.append({"id": r["id"], "cls": c, "obj": obj})
    return photos, {v: k for k, v in cls.items()} | {IGNORE: "not a product"}, skipped, ref["value"] if ref else None


def fit(obj: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    longest = max(obj.shape[:2])
    scale = min(1.0, 0.36 * W / longest) * rng.uniform(0.8, 1.0)
    return transform(obj, rng.uniform(-30, 30), scale)


def free_spot(obj, taken, rng, near=None):
    h, w = obj.shape[:2]
    for _ in range(80):
        if near is not None:  # touching: right next to the previous item, overlapping a few pixels
            nx0, ny0, nx1, ny1 = near
            x = int(nx1 - rng.integers(2, 10))
            lo, hi = max(10, ny0 - h // 2), min(H - h - 10, ny1 - h // 2)
            if lo >= hi:
                continue  # no room beside it at this height: try again
            y = int(rng.integers(lo, hi))
        else:
            x, y = int(rng.integers(10, max(11, W - w - 10))), int(rng.integers(10, max(11, H - h - 10)))
        if x < 5 or y < 5 or x + w > W - 5 or y + h > H - 5:
            continue
        box = (x, y, x + w, y + h)
        others = [t for t in taken if t is not near]
        if all(box[2] + 25 < t[0] or t[2] + 25 < box[0] or box[3] + 25 < t[1] or t[3] + 25 < box[1] for t in others):
            return x, y
    return None


def run(args) -> dict:
    cfg = load_config()
    rc = cfg.recognition
    photos, names, skipped, ref_rel = load_photos(cfg.db_path, cfg.media_dir)
    rng = np.random.default_rng(args.seed)
    enc = get_encoder(rc.model_name, rc.device)

    if args.background == "counter":
        if not ref_rel:
            sys.exit("No empty-counter photo yet: calibrate on the Counter camera page first.")
        counter = np.asarray(Image.open(cfg.media_dir / ref_rel).convert("RGB").resize((W, H)))
    by_cls = defaultdict(list)
    for p in photos:
        by_cls[p["cls"]].append(p)
    folds = {}
    for ps in by_cls.values():  # stratified: every product in every fold
        order = rng.permutation(len(ps))
        for n, i in enumerate(order):
            folds[ps[i]["id"]] = n % args.folds

    items_out, sales_out, examples = [], [], []
    t_rec = []
    for fold in range(args.folds):
        background = plain_mat(rng) if args.background == "mat" else counter
        reference = capture(background, rng)

        # Enroll the other folds' photos the way the shop does: alone on the counter, cropped by segmentation.
        vecs, labels, cols = [], [], []
        crops = []
        for p in photos:
            if folds[p["id"]] == fold:
                continue
            scene = background.copy()
            obj = fit(p["obj"], rng)
            spot = free_spot(obj, [], rng)
            if spot is None:
                continue
            paste(scene, obj, *spot)
            blob = segment.largest(capture(scene, rng), reference, rc.segment)
            if blob is None:
                continue
            crops.append(blob.crop)
            labels.append(("ignore", "other") if p["cls"] == IGNORE else ("pack", p["cls"]))
        if crops:
            vecs = enc.encode_images(crops)
            cols = np.stack([colour_signature(c) for c in crops])
        gallery = Gallery(np.asarray(vecs, np.float32), labels, cols)

        pool = [p for p in photos if folds[p["id"]] == fold and p["cls"] != IGNORE]
        junk = [p for p in photos if folds[p["id"]] == fold and p["cls"] == IGNORE]
        if not pool:
            continue
        for s in range(args.sales):
            n_items = int(rng.choice([1, 2, 3, 4], p=[0.3, 0.35, 0.25, 0.1]))
            chosen = [pool[int(rng.integers(len(pool)))] for _ in range(n_items)]
            if junk and rng.random() < 0.15:
                chosen.append(junk[int(rng.integers(len(junk)))])
            touching_pair = n_items >= 2 and rng.random() < 0.2
            scene, placed, boxes = background.copy(), [], []
            for k, p in enumerate(chosen):
                obj = fit(p["obj"], rng)
                spot = free_spot(obj, boxes, rng, near=boxes[-1] if (touching_pair and k == 1) else None)
                if spot is None:
                    continue
                mask = paste(scene, obj, *spot)
                boxes.append((spot[0], spot[1], spot[0] + obj.shape[1], spot[1] + obj.shape[0]))
                placed.append({"cls": p["cls"], "mask": mask, "area": int(mask.sum()),
                               "touching": touching_pair and k in (0, 1)})
            frame = capture(scene, rng)

            t0 = time.perf_counter()
            blobs = segment.segment(frame, reference, rc.segment)
            embs = enc.encode_images([b.crop for b in blobs]) if blobs else []
            dets = []
            for b, v in zip(blobs, embs, strict=True):
                m = gallery.match(v, rc.top_k_per_pack)
                row = {"bbox": b.bbox}
                if m.ignore_label:
                    row.update(pred=IGNORE, tier="ignored", pred_plain=IGNORE, tier_plain="ignored",
                               pred_guess=IGNORE, tier_guess="ignored")
                else:
                    plain_tier = tier(m.score, m.margin, rc)
                    row.update(pred_plain=m.pack_id, tier_plain=plain_tier)
                    mc = colour_tiebreak(gallery, m, colour_signature(b.crop), rc) or m
                    row.update(pred=mc.pack_id, tier=tier(mc.score, mc.margin, rc))
                    # Colour picks the look-alike but never skips the owner's check.
                    row.update(pred_guess=mc.pack_id, tier_guess=plain_tier)
                dets.append(row)
            t_rec.append(time.perf_counter() - t0)

            # Which placed item does each detection belong to? The one whose pixels its box covers most
            # (a box around a thin diagonal pen is mostly mat, so "is the centre on the item" misjudges it).
            def covered(it, box):
                x, y, bw, bh = box
                return it["mask"][y:y + bh, x:x + bw].sum() / max(1, it["area"])
            owner = []
            for d in dets:
                cover = [covered(it, d["bbox"]) for it in placed]
                best = int(np.argmax(cover)) if cover else None
                owner.append(best if best is not None and cover[best] >= 0.35 else None)
            sale = {"items": len(placed), "questions": 0, "all_auto_right": True, "fold": fold}
            for i, it in enumerate(placed):
                mine = [d for d, o in zip(dets, owner, strict=True) if o == i]
                rec = {"cls": it["cls"], "touching": it["touching"], "fold": fold}
                if not mine:
                    merged = any(covered(it, d["bbox"]) >= 0.5 for d in dets)  # inside another item's box
                    miss = "merged" if merged else "missed"
                    rec.update(outcome=miss, outcome_plain=miss, outcome_guess=miss, pred=None)
                else:
                    d = mine[0]
                    rec["extra"] = len(mine) - 1  # one item seen as two
                    for suffix in ("", "_plain", "_guess"):
                        pred, t = d["pred" + suffix], d["tier" + suffix]
                        if it["cls"] == IGNORE:
                            out = "ignored" if pred == IGNORE else ("auto" if t == "high" else "asked")
                        elif pred == IGNORE:
                            out = "dropped"
                        else:
                            out = ("auto_" if t == "high" else "ask_") + ("right" if pred == it["cls"] else "wrong")
                        rec["outcome" + suffix] = out
                    rec["pred"] = d["pred"]
                items_out.append(rec)
                if rec["outcome"] not in ("auto_right", "ignored"):
                    sale["all_auto_right"] = False
            extras = [d for d, o in zip(dets, owner, strict=True) if o is None]
            sale["spurious"] = len(extras)
            sale["spurious_auto"] = sum(d["tier"] == "high" for d in extras)
            sale["questions"] = sum(d["tier"] in ("medium", "low") for d in dets)
            if extras:
                sale["all_auto_right"] = False
            sales_out.append(sale)
            if len(examples) < 6 and (s % 13 == 0):
                examples.append((frame, dets, placed, names))
        print(f"fold {fold + 1}/{args.folds}: gallery {len(labels)} photos, {args.sales} sales", flush=True)
    return {"items": items_out, "sales": sales_out, "names": names, "skipped": dict(skipped),
            "photos": len(photos), "examples": examples, "rec_ms": 1000 * float(np.median(t_rec)) if t_rec else None}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5 / d
    return p, max(0.0, c - h), min(1.0, c + h)


def summarise(res: dict, suffix: str = "", without: frozenset = frozenset()) -> dict:
    skip = {c for c, n in res["names"].items() if n in without}
    items = [i for i in res["items"] if i["cls"] not in skip]
    prod = [i for i in items if i["cls"] != IGNORE]
    junk = [i for i in items if i["cls"] == IGNORE]
    oc = Counter(i["outcome" + suffix] for i in prod)
    n = len(prod)
    auto = oc["auto_right"] + oc["auto_wrong"]
    asked = oc["ask_right"] + oc["ask_wrong"]
    found = auto + asked + oc["dropped"]
    sales = res["sales"]
    touching = [i for i in prod if i["touching"]]
    out = {
        "products_placed": n,
        "found": wilson(found, n),
        "missed_or_merged": wilson(oc["missed"] + oc["merged"], n),
        "right_product": wilson(oc["auto_right"] + oc["ask_right"], n),
        "recorded_automatically": wilson(auto, n),
        "automatic_and_wrong": wilson(oc["auto_wrong"], max(auto, 1)),
        "asked_owner": wilson(asked, n),
        "asked_and_guess_right": wilson(oc["ask_right"], max(asked, 1)),
        "dropped_as_not_a_product": wilson(oc["dropped"], n),
        "stock_errors_if_owner_answers": wilson(oc["auto_wrong"] + oc["missed"] + oc["merged"] + oc["dropped"], n),
        "stock_errors_if_nobody_answers": wilson(oc["auto_wrong"] + oc["ask_wrong"] + oc["missed"] + oc["merged"]
                                                 + oc["dropped"], n),
        "touching_items_found_separately": wilson(sum(i["outcome" + suffix] not in ("missed", "merged")
                                                      for i in touching), len(touching)),
        "one_item_seen_as_two": wilson(sum(i.get("extra", 0) > 0 for i in prod), n),
        "sales": len(sales),
        "sales_fully_automatic_and_right": wilson(sum(s["all_auto_right"] for s in sales), len(sales)),
        "questions_per_sale": round(sum(s["questions"] for s in sales) / max(len(sales), 1), 2),
        "spurious_detections_per_sale": round(sum(s["spurious"] for s in sales) / max(len(sales), 1), 3),
        "junk_placed": len(junk),
        "junk_ignored": wilson(sum(i["outcome" + suffix] == "ignored" for i in junk), len(junk)),
        "junk_asked": wilson(sum(i["outcome" + suffix] == "asked" for i in junk), len(junk)),
        "junk_recorded_as_sale": wilson(sum(i["outcome" + suffix] == "auto" for i in junk), len(junk)),
        "outcomes": dict(oc),
    }
    per = defaultdict(Counter)
    for i in prod:
        per[res["names"][i["cls"]]][i["outcome" + suffix]] += 1
    out["per_product"] = {k: dict(v) for k, v in per.items()}
    conf = Counter((res["names"][i["cls"]], res["names"].get(i["pred"], "?")) for i in prod
                   if i["outcome" + suffix] in ("auto_wrong", "ask_wrong"))
    out["confusions"] = [(a, b, c) for (a, b), c in conf.most_common(6)]
    return out


def draw_examples(res: dict, out: Path) -> None:
    colours = {"high": (62, 207, 142), "medium": (245, 183, 0), "low": (239, 68, 68), "ignored": (156, 163, 175)}
    tiles = []
    for frame, dets, _placed, names in res["examples"]:
        img = frame.copy()
        for d in dets:
            x, y, bw, bh = d["bbox"]
            c = colours[d["tier"]][::-1]
            cv2.rectangle(img, (x, y), (x + bw, y + bh), c, 4)
            text = "not a product" if d["pred"] == IGNORE else f'{names[d["pred"]]} ({d["tier"]})'
            cv2.putText(img, text, (x + 4, max(24, y - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.8, c, 2)
        tiles.append(cv2.resize(img, (640, 480)))
    if tiles:
        while len(tiles) % 3:
            tiles.append(np.full_like(tiles[0], 255))
        rows = [np.hstack(tiles[i:i + 3]) for i in range(0, len(tiles), 3)]
        cv2.imwrite(str(out / "example_sales.jpg"), np.vstack(rows))


def pct(t) -> str:
    return f"{t[0]:.1%} ({t[1]:.0%}–{t[2]:.0%})"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--background", choices=["mat", "counter"], default="mat")
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--sales", type=int, default=80, help="sales per fold")
    parser.add_argument("--seed", type=int, default=11)
    parser.add_argument("--out", type=Path, default=Path("data/counter_test"))
    parser.add_argument("--without", default="", help='also report without these products, e.g. "Blue Pen,Black Pen"')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    res = run(args)
    s, plain, guess = summarise(res), summarise(res, "_plain"), summarise(res, "_guess")
    print(f"\nSIMULATED counter test · {args.background} · {s['sales']} sales · {s['products_placed']} products placed")
    print(f"photos usable as cut-outs: {res['photos']} (skipped: {res['skipped']})")
    for key in ("found", "right_product", "recorded_automatically", "automatic_and_wrong", "asked_owner",
                "asked_and_guess_right", "dropped_as_not_a_product", "missed_or_merged", "one_item_seen_as_two",
                "touching_items_found_separately", "stock_errors_if_owner_answers", "stock_errors_if_nobody_answers",
                "sales_fully_automatic_and_right", "junk_ignored", "junk_asked", "junk_recorded_as_sale"):
        print(f"  {key.replace('_', ' '):36} {pct(s[key])}")
    print(f"  {'questions per sale':36} {s['questions_per_sale']}")
    print(f"  {'spurious detections per sale':36} {s['spurious_detections_per_sale']}")
    print(f"  {'recognition time per sale':36} {res['rec_ms']:.0f} ms (median)")
    for label, v in (("colour tie-break off", plain), ("colour picks, owner still checks", guess)):
        print(f"  {label}: right product {pct(v['right_product'])}, recorded automatically "
              f"{pct(v['recorded_automatically'])}, automatic and wrong {pct(v['automatic_and_wrong'])}, "
              f"stock errors if owner answers {pct(v['stock_errors_if_owner_answers'])}")
    if args.without:
        without = frozenset(n.strip() for n in args.without.split(","))
        for label, suffix in (("", ""), ("colour off", "_plain"), ("colour picks, owner checks", "_guess")):
            w = summarise(res, suffix, without)
            print(f"  without {sorted(without)} {label}: {w['products_placed']} placed · found {pct(w['found'])} · "
                  f"right {pct(w['right_product'])} · automatic {pct(w['recorded_automatically'])} · "
                  f"automatic and wrong {pct(w['automatic_and_wrong'])} · stock errors if owner answers "
                  f"{pct(w['stock_errors_if_owner_answers'])} · questions/sale {w['questions_per_sale']}")
    print("  per product:", json.dumps(s["per_product"]))
    print("  confusions:", s["confusions"])
    (args.out / f"results_{args.background}.json").write_text(json.dumps(
        {"simulated": True, "background": args.background, "with_colour": s, "without_colour": plain,
         "colour_guess_only": guess,
         "rec_ms": res["rec_ms"], "skipped": res["skipped"]}, indent=1), encoding="utf-8")
    draw_examples(res, args.out)


if __name__ == "__main__":
    main()
