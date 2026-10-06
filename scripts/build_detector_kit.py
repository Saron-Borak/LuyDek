"""Pack the training kit for the one-class "item on the mat" detector (run the training on Colab).

    python scripts/build_detector_kit.py            # -> training/item-detector-kit.zip

The kit holds:
* cutouts/   every product photo in the gallery, cut out of its background (photos whose cut isn't
             trustworthy are left out, and the ones named with --skip-cutouts: look at
             training/cutouts_sheet.jpg for cuts that took a slab of background with them),
             named <product>__<n>.png;
* backgrounds/  the shop's empty-mat photos, except the ones named with --skip: look at
             training/backgrounds_sheet.jpg (numbered) and leave out any with a person, a hand or an
             object in it. An object left in would teach the detector that items are background;
* generate.py   makes the training scenes from those (scripts/detector_kit/generate.py);
* train.ipynb   the Colab notebook: generate, train, check, download the model.

Only photos of products and of the empty mat go in: no sales photos. The database is opened
read-only and nothing in data/ is changed. training/cutouts_sheet.jpg shows every cut-out, to check.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import zipfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from luydek.config import load_config  # noqa: E402
from luydek.recognition.cutout import product_mask  # noqa: E402

KIT_SOURCES = ROOT / "scripts" / "detector_kit"
CUTOUT_MAX = 480  # the generator sizes every product itself; bigger cut-outs only make the kit heavier


def sheet(images: list[np.ndarray], path: Path, tile=(160, 120), cols=12) -> None:
    """A numbered contact sheet, to look over what goes into the kit."""
    tiles = []
    for i, img in enumerate(images):
        if img.shape[2] == 4:  # a cut-out: on grey, so its outline shows
            a = img[..., 3:4] / 255.0
            img = (img[..., :3] * a + 128 * (1 - a)).astype(np.uint8)
        h, w = img.shape[:2]
        k = min(tile[0] / w, tile[1] / h)
        t = np.full((tile[1], tile[0], 3), 40, np.uint8)
        small = cv2.resize(img, (max(1, int(w * k)), max(1, int(h * k))))
        t[:small.shape[0], :small.shape[1]] = small
        cv2.putText(t, str(i), (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
        tiles.append(t)
    while len(tiles) % cols:
        tiles.append(np.zeros_like(tiles[0]))
    rows = [cv2.hconcat(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    cv2.imwrite(str(path), cv2.vconcat(rows), [cv2.IMWRITE_JPEG_QUALITY, 80])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "training" / "item-detector-kit"))
    ap.add_argument("--skip", nargs="*", default=[],
                    help="empty-mat photos to leave out: numbers on backgrounds_sheet.jpg or file names")
    ap.add_argument("--skip-cutouts", nargs="*", type=int, default=[],
                    help="cut-outs to leave out (the background came with them): numbers on cutouts_sheet.jpg")
    args = ap.parse_args()
    cfg = load_config()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    (out / "cutouts").mkdir(parents=True)
    (out / "backgrounds").mkdir()

    conn = sqlite3.connect(f"file:{cfg.db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT g.id, g.crop_path, p.sku FROM gallery_images g JOIN product_packs pk ON pk.id = g.pack_id "
        "JOIN products p ON p.id = pk.product_id WHERE p.active = 1 ORDER BY p.sku, g.id").fetchall()
    conn.close()

    kept, skipped, skipped_by_hand, per_product, cuts = 0, 0, 0, {}, []
    for r in rows:
        bgr = cv2.imread(str(cfg.media_dir / r["crop_path"]))
        if bgr is None:
            skipped += 1
            continue
        mask = product_mask(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        if mask is None:
            skipped += 1
            continue
        x, y, w, h = cv2.boundingRect(mask)
        alpha = cv2.GaussianBlur(mask.astype(np.float32), (3, 3), 0)[y:y + h, x:x + w]
        rgba = np.dstack([bgr[y:y + h, x:x + w], (alpha * 255).astype(np.uint8)])
        k = CUTOUT_MAX / max(w, h)
        if k < 1:
            rgba = cv2.resize(rgba, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA)
        number = len(cuts)
        cuts.append(rgba)
        if number in args.skip_cutouts:
            skipped_by_hand += 1
            continue
        n = per_product.get(r["sku"], 0)
        per_product[r["sku"]] = n + 1
        cv2.imwrite(str(out / "cutouts" / f"{r['sku']}__{n}.png"), rgba)
        kept += 1

    refs = sorted((cfg.media_dir / "reference").glob("*.jpg"), key=lambda p: p.stat().st_mtime)
    skip = set(args.skip)
    all_mats, mats, left_out = [], 0, 0
    for i, p in enumerate(refs):
        img = cv2.imread(str(p))
        if img is None:
            continue
        all_mats.append(img)
        if str(i) in skip or p.name in skip or p.stem in skip:
            left_out += 1
            continue
        cv2.imwrite(str(out / "backgrounds" / f"mat_{mats:03d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
        mats += 1
    if all_mats:
        sheet(all_mats, out.parent / "backgrounds_sheet.jpg")
    if cuts:
        sheet(cuts, out.parent / "cutouts_sheet.jpg", tile=(120, 120), cols=16)

    shutil.copy(KIT_SOURCES / "generate.py", out / "generate.py")
    shutil.copy(KIT_SOURCES / "train.ipynb", out / "train.ipynb")
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, Path("kit") / f.relative_to(out))
    print(f"{kept} product photos cut out ({skipped} left out: no trustworthy cut; {skipped_by_hand} with "
          f"--skip-cutouts), {len(per_product)} products")
    print(f"{mats} empty-mat photos ({left_out} left out with --skip)")
    print(f"to look over: {out.parent / 'backgrounds_sheet.jpg'}, {out.parent / 'cutouts_sheet.jpg'}")
    print(f"kit: {out}\nzip: {archive} ({archive.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
