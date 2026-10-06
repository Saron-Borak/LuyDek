"""Make training scenes for the one-class "item on the mat" detector, with exact outlines.

Real products (cut out of the shop's own photos) are laid on real photos of the shop's empty mat:
alone, side by side, touching, overlapping, at any angle, with soft shadows, in changing light, sharp
or blurry, at the counter camera's resolution or higher. Every outline is known, so nothing is
labelled by hand. Detached shadows and light patches with no item under them are in the scenes too,
so the detector learns they are not items.

    python generate.py --kit . --out dataset --train 3000 --val 400

The kit folder holds cutouts/*.png (RGBA, one product each, named <product>__<n>.png) and
backgrounds/*.jpg (empty-mat photos). Products are sized as a share of the picture, from a sweet
(7% of its width) to a remote (50%): the cut-outs come from different cameras. Validation scenes use backgrounds and product photos the
training scenes never see. Writes YOLO segmentation labels and dataset.yaml (one class: item).
Needs only numpy and opencv.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing
import os
import random
from pathlib import Path

import cv2
import numpy as np

MIN_VISIBLE = 0.35      # an item covered more than this by others is not labelled (nobody could count it)
MIN_PIXELS = 120        # nor a sliver this small
SIZES = (640, 960, 1280, 1920)  # output widths: the counter camera today (upscaled) up to full HD


def load_kit(kit: Path) -> tuple[dict[str, list[np.ndarray]], list[np.ndarray]]:
    products: dict[str, list[np.ndarray]] = {}
    for p in sorted((kit / "cutouts").glob("*.png")):
        img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if img is None or img.ndim != 3 or img.shape[2] != 4:
            continue
        products.setdefault(p.stem.split("__")[0], []).append(img)
    backgrounds = [cv2.imread(str(p)) for p in sorted((kit / "backgrounds").glob("*.jpg"))]
    return products, [b for b in backgrounds if b is not None]


def split(products, backgrounds, rng: random.Random, val_share=0.15):
    """Hold out some backgrounds and some photos of each product for validation."""
    bgs = backgrounds[:]
    rng.shuffle(bgs)
    nb = max(1, round(len(bgs) * val_share))
    train_p, val_p = {}, {}
    for name, imgs in products.items():
        imgs = imgs[:]
        rng.shuffle(imgs)
        nv = round(len(imgs) * val_share) if len(imgs) >= 4 else 0
        val_p[name] = imgs[:nv] or imgs[:1]
        train_p[name] = imgs[nv:] or imgs
    return (train_p, bgs[nb:] or bgs), (val_p, bgs[:nb])


def rotate(rgba: np.ndarray, angle: float, scale: float) -> np.ndarray:
    h, w = rgba.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, scale)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos) + 2, int(h * cos + w * sin) + 2
    m[0, 2] += nw / 2 - w / 2
    m[1, 2] += nh / 2 - h / 2
    return cv2.warpAffine(rgba, m, (nw, nh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def soft_blob(shape, rng: random.Random) -> np.ndarray:
    """A soft, irregular patch (an arm's shadow, a light change) with nothing under it."""
    h, w = shape
    m = np.zeros((h, w), np.float32)
    cx, cy = rng.uniform(0.1, 0.9) * w, rng.uniform(0.1, 0.9) * h
    ax, ay = rng.uniform(0.08, 0.35) * w, rng.uniform(0.04, 0.15) * h
    cv2.ellipse(m, (int(cx), int(cy)), (int(ax), int(ay)), rng.uniform(0, 180), 0, 360, 1.0, -1)
    return cv2.GaussianBlur(m, (0, 0), max(3.0, 0.04 * w * rng.uniform(0.5, 1.5)))


def place(scene_w, scene_h, item_w, item_h, placed, mode, rng: random.Random):
    """Top-left corner for an item: anywhere free, right against an item already there, or partly on it."""
    if placed and mode != "free":
        x0, y0, x1, y1 = rng.choice(placed)
        ang = rng.uniform(0, 2 * math.pi)
        gap = rng.uniform(-0.05, 0.02) if mode == "touch" else rng.uniform(-0.45, -0.15)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        dx = math.cos(ang) * ((x1 - x0) / 2 + item_w / 2 + gap * item_w)
        dy = math.sin(ang) * ((y1 - y0) / 2 + item_h / 2 + gap * item_h)
        x, y = int(cx + dx - item_w / 2), int(cy + dy - item_h / 2)
    else:
        x = rng.randint(-item_w // 6, max(-item_w // 6 + 1, scene_w - item_w + item_w // 6))
        y = rng.randint(-item_h // 6, max(-item_h // 6 + 1, scene_h - item_h + item_h // 6))
    return x, y


def scene(products, backgrounds, rng: random.Random):
    """One scene: (BGR image, list of polygons in pixels)."""
    bg = rng.choice(backgrounds)
    bh, bw = bg.shape[:2]
    out_w = rng.choice(SIZES)
    k = out_w / bw
    out_h = round(bh * k)
    img = cv2.resize(bg, (out_w, out_h), interpolation=cv2.INTER_CUBIC if k > 1 else cv2.INTER_AREA)
    if rng.random() < 0.5:
        img = cv2.flip(img, 1)
    img = img.astype(np.float32)

    n = rng.choices([0, rng.randint(1, 3), rng.randint(4, 8), rng.randint(9, 14)], weights=[6, 38, 42, 14])[0]
    # The cut-outs come from different cameras, so their own sizes mean nothing: each product gets a
    # size for this scene (its long side, as a share of the picture's width), from a sweet to a remote.
    sizes = {name: math.exp(rng.uniform(math.log(0.07), math.log(0.5))) for name in products}
    light = rng.uniform(0, 2 * math.pi)              # where the light comes from: shadows fall the other way
    owners = np.full((out_h, out_w), -1, np.int32)   # which item each pixel shows (-1: the mat)
    full_areas, boxes = [], []
    for i in range(n):
        name = rng.choice(list(products))
        rgba = rng.choice(products[name])
        if rng.random() < 0.5:
            rgba = cv2.flip(rgba, 1)
        long_side = sizes[name] * out_w * rng.uniform(0.9, 1.1)
        rgba = rotate(rgba, rng.uniform(0, 360), long_side / max(rgba.shape[:2]))
        ih, iw = rgba.shape[:2]
        if iw < 8 or ih < 8 or iw >= out_w or ih >= out_h:
            continue
        mode = rng.choices(["free", "touch", "overlap"], weights=[45, 40, 15])[0]
        x, y = place(out_w, out_h, iw, ih, boxes, mode, rng)
        sx0, sy0, sx1, sy1 = max(0, x), max(0, y), min(out_w, x + iw), min(out_h, y + ih)
        if sx1 - sx0 < 8 or sy1 - sy0 < 8:
            continue
        crop = rgba[sy0 - y:sy1 - y, sx0 - x:sx1 - x].astype(np.float32)
        alpha = crop[..., 3] / 255.0
        # its shadow first, under it: offset away from the light, soft
        size = max(iw, ih)
        off = rng.uniform(0.02, 0.08) * size
        mx, my = int(-math.cos(light) * off), int(-math.sin(light) * off)
        sigma = max(1.5, rng.uniform(0.01, 0.05) * size)
        m = int(3 * sigma) + max(abs(mx), abs(my)) + 2           # only around the item: much faster
        rx0, ry0, rx1, ry1 = max(0, sx0 - m), max(0, sy0 - m), min(out_w, sx1 + m), min(out_h, sy1 + m)
        shadow = np.zeros((ry1 - ry0, rx1 - rx0), np.float32)
        tx0, ty0 = sx0 - rx0 + mx, sy0 - ry0 + my
        cx0, cy0 = max(0, tx0), max(0, ty0)
        cx1, cy1 = min(shadow.shape[1], tx0 + (sx1 - sx0)), min(shadow.shape[0], ty0 + (sy1 - sy0))
        if cx1 > cx0 and cy1 > cy0:
            shadow[cy0:cy1, cx0:cx1] = alpha[cy0 - ty0:cy1 - ty0, cx0 - tx0:cx1 - tx0]
        shadow = cv2.GaussianBlur(shadow, (0, 0), sigma)
        img[ry0:ry1, rx0:rx1] *= (1 - shadow * rng.uniform(0.15, 0.55))[..., None]
        # then the item, with a little light of its own
        rgb = crop[..., :3] * rng.uniform(0.85, 1.15)
        a3 = alpha[..., None]
        img[sy0:sy1, sx0:sx1] = img[sy0:sy1, sx0:sx1] * (1 - a3) + rgb * a3
        owners[sy0:sy1, sx0:sx1][alpha > 0.5] = i
        full_areas.append((i, float((alpha > 0.5).sum())))
        boxes.append((sx0, sy0, sx1, sy1))

    for _ in range(rng.choices([0, 1, 2], weights=[60, 30, 10])[0]):  # shadows and light with no item under them
        blob = soft_blob((out_h, out_w), rng)
        if rng.random() < 0.75:
            img *= (1 - blob * rng.uniform(0.2, 0.55))[..., None]
        else:
            img *= (1 + blob * rng.uniform(0.15, 0.5))[..., None]

    # the whole picture: brightness, colour of the light, a gradient across the mat, camera noise
    img *= rng.uniform(0.7, 1.25)
    img *= np.array([rng.uniform(0.9, 1.1), 1.0, rng.uniform(0.9, 1.1)], np.float32)
    gx = np.linspace(rng.uniform(0.8, 1.0), rng.uniform(1.0, 1.2), out_w, dtype=np.float32)
    img *= (gx[None, :, None] if rng.random() < 0.5 else gx[None, ::-1, None])
    img += np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(1, 6), img.shape).astype(np.float32)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rng.random() < 0.35:                          # a small camera, enlarged
        f = rng.uniform(0.3, 0.7)
        small = cv2.resize(img, (max(32, int(out_w * f)), max(24, int(out_h * f))), interpolation=cv2.INTER_AREA)
        img = cv2.resize(small, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    if rng.random() < 0.2:
        img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.6, 1.6))

    polygons = []
    for i, full in full_areas:
        vis = (owners == i).astype(np.uint8)
        area = int(vis.sum())
        if area < MIN_PIXELS or area < MIN_VISIBLE * full:
            continue
        contours, _ = cv2.findContours(vis, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) < 0.6 * area:
            continue                                  # cut in two by another item: too ambiguous to label
        c = cv2.approxPolyDP(c, 1.0, True).reshape(-1, 2)
        if len(c) >= 3:
            polygons.append(c)
    return img, polygons


def write(out: Path, part: str, idx: int, img, polygons, quality: int) -> None:
    h, w = img.shape[:2]
    (out / "images" / part).mkdir(parents=True, exist_ok=True)
    (out / "labels" / part).mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out / "images" / part / f"{idx:05d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    lines = ["0 " + " ".join(f"{x / w:.5f} {y / h:.5f}" for x, y in poly) for poly in polygons]
    (out / "labels" / part / f"{idx:05d}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))


_parts: dict = {}
_out: Path | None = None


def _init(kit: str, seed: int, out: str) -> None:
    """Each worker loads the kit itself (quicker than being sent 160 MB of photos) and splits it the
    same way, from the same seed."""
    global _parts, _out
    products, backgrounds = load_kit(Path(kit))
    (tr_p, tr_b), (va_p, va_b) = split(products, backgrounds, random.Random(seed))
    _parts, _out = {"train": (tr_p, tr_b), "val": (va_p, va_b)}, Path(out)


def _make(job: tuple[str, int, int]) -> int:
    """One scene, in a worker process: its own seed, so the dataset is the same however many workers."""
    part, idx, seed = job
    rng = random.Random(seed)
    products, backgrounds = _parts[part]
    img, polys = scene(products, backgrounds, rng)
    write(_out, part, idx, img, polys, rng.randint(60, 95))
    return len(polys)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kit", default=".", help="folder with cutouts/ and backgrounds/")
    ap.add_argument("--out", default="dataset")
    ap.add_argument("--train", type=int, default=3000)
    ap.add_argument("--val", type=int, default=400)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--workers", type=int, default=min(8, os.cpu_count() or 2),
                    help="processes making scenes (1: all in this one)")
    args = ap.parse_args()
    products, backgrounds = load_kit(Path(args.kit))
    if not products or not backgrounds:
        raise SystemExit("The kit needs cutouts/*.png and backgrounds/*.jpg")
    out = Path(args.out)
    counts = {}
    def jobs(part: str, n: int) -> list[tuple[str, int, int]]:
        return [(part, i, args.seed * 1_000_003 + i * 2 + (part == "val")) for i in range(n)]

    if args.workers <= 1:
        _init(args.kit, args.seed, str(out))
        for part, n in (("train", args.train), ("val", args.val)):
            counts[part] = (n, sum(map(_make, jobs(part, n))))
    else:
        with multiprocessing.Pool(args.workers, initializer=_init, initargs=(args.kit, args.seed, str(out))) as pool:
            for part, n in (("train", args.train), ("val", args.val)):
                counts[part] = (n, sum(pool.imap_unordered(_make, jobs(part, n), chunksize=8)))
    (out / "dataset.yaml").write_text(f"path: {out.resolve().as_posix()}\ntrain: images/train\nval: images/val\n"
                                      "names:\n  0: item\n")
    print(f"{len(products)} products, {sum(map(len, products.values()))} product photos, {len(backgrounds)} mat photos")
    for part, (n, total) in counts.items():
        print(f"{part}: {n} scenes, {total} items")


if __name__ == "__main__":
    main()
