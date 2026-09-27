"""Build a SYNTHETIC demo shop that needs no real products.

Paints six look-alike "products" (Ichitan vs Oishi green tea, Paracetamol 250 vs
500, Coca-Cola, Dumex) onto a plain mat, enrolls them with the real CLIP model,
calibrates the empty mat, adds simulated sales history, and writes sample
counter photos to data/synthetic/samples/.

    python scripts/synthetic_demo.py
    python -m luydek --config configs/synthetic.yaml --port 8010
    python scripts/sale_from_photo.py --server http://127.0.0.1:8010 --use-reference data/synthetic/samples/*.jpg

Painted boxes are much easier than real packaging; accuracy numbers from this
shop say nothing about a real counter. Use scripts/calibrate.py on real photos.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luydek import db  # noqa: E402
from luydek.config import load_config  # noqa: E402
from luydek.recognition import pipeline  # noqa: E402
from luydek.services import catalog  # noqa: E402
from scripts.simulate_history import simulate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MAT = (190, 200, 205)  # BGR, a pale grey counter mat
PRODUCTS = {  # sku: (body colour BGR, brand word, size text)
    "ichitan-420": ((40, 140, 60), "ICHITAN", "420ml"),
    "oishi-380": ((50, 150, 70), "OISHI", "380ml"),
    "coke-330": ((30, 30, 200), "Coca-Cola", "330ml"),
    "dumex-400": ((200, 120, 30), "DUMEX", "Dugro"),
    "para-500": ((230, 230, 235), "PARA", "500mg"),
    "para-250": ((225, 225, 240), "PARA", "250mg"),
}
SAMPLE_SALES = {
    "sale1_two_cokes_and_formula.jpg": [("coke-330", 60, 80, 0), ("coke-330", 260, 90, 6), ("dumex-400", 520, 300, -4)],
    "sale2_lookalikes.jpg": [("para-250", 120, 120, 10), ("oishi-380", 420, 260, -6)],
    "sale3_green_tea.jpg": [("ichitan-420", 300, 200, 3)],
}


def mat() -> np.ndarray:
    frame = np.zeros((600, 800, 3), np.uint8)
    frame[:] = MAT
    return frame


def paint(frame: np.ndarray, sku: str, x: int, y: int, angle: float = 0) -> None:
    body, word, size = PRODUCTS[sku]
    tile = np.zeros((170, 110, 3), np.uint8)
    tile[:] = body
    cv2.rectangle(tile, (0, 50), (110, 110), (245, 245, 245), -1)
    cv2.putText(tile, word[:8], (4, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (20, 20, 20), 2)
    cv2.putText(tile, size, (14, 102), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 180), 2)
    if angle:
        rot = cv2.getRotationMatrix2D((55, 85), angle, 1.0)
        tile = cv2.warpAffine(tile, rot, (110, 170), borderValue=MAT)
    frame[y:y + 170, x:x + 110] = tile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "synthetic.yaml")
    parser.add_argument("--shots", type=int, default=5, help="enrollment photos per product")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if "synthetic" not in str(cfg.media_dir):
        sys.exit(f"Refusing to wipe {cfg.media_dir}: this script only rebuilds the synthetic shop.")
    for sub in ("gallery", "crops", "frames", "reference"):
        shutil.rmtree(cfg.media_dir / sub, ignore_errors=True)  # the reset below orphans them otherwise
    with db.session(cfg) as conn:
        db.init_schema(conn)
        catalog.reset(conn)
        catalog.load_seed(conn, cfg.resolve(cfg.paths.seed_catalog))
        ok, buf = cv2.imencode(".jpg", mat())
        pipeline.save_reference(conn, cfg, buf.tobytes())

    print("Loading CLIP...")
    rec = pipeline.get_recognizer(cfg)
    angles = (0, 8, -8, 15, -15, 4, -4, 12, -12, 0)[: args.shots]
    with db.session(cfg) as conn:
        packs = {pk["sku"]: pk["pack_id"] for pk in catalog.packs(conn) if pk["units"] == 1}
        for sku in PRODUCTS:
            for i, angle in enumerate(angles):
                frame = mat()
                paint(frame, sku, 80 + 90 * (i % 6), 60 + 50 * (i % 6), angle)
                rec.enroll(conn, frame, packs[sku])
    print(f"Enrolled {len(PRODUCTS)} painted products x {len(angles)} photos")

    written = simulate(cfg, weeks=8)
    print(f"Wrote {written} SIMULATED movements")

    samples = cfg.resolve(cfg.paths.db).parent / "samples"
    samples.mkdir(parents=True, exist_ok=True)
    for name, items in SAMPLE_SALES.items():
        frame = mat()
        for sku, x, y, angle in items:
            paint(frame, sku, x, y, angle)
        cv2.imwrite(str(samples / name), frame)
    print(f"Sample counter photos in {samples}")
    print("Run:  python -m luydek --config configs/synthetic.yaml --port 8010")


if __name__ == "__main__":
    main()
