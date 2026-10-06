"""The training-scene generator for the item detector: scenes with exact outlines, ready for Ultralytics."""

import importlib.util
import random
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("generate", ROOT / "scripts" / "detector_kit" / "generate.py")
generate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generate)


def kit(tmp_path: Path) -> Path:
    (tmp_path / "cutouts").mkdir(parents=True)
    (tmp_path / "backgrounds").mkdir()
    for i, colour in enumerate([(40, 40, 200), (40, 180, 60), (200, 120, 30)]):
        rgba = np.zeros((90, 60, 4), np.uint8)
        rgba[5:85, 5:55, :3] = colour
        rgba[5:85, 5:55, 3] = 255
        cv2.imwrite(str(tmp_path / "cutouts" / f"product{i}__0.png"), rgba)
        cv2.imwrite(str(tmp_path / "cutouts" / f"product{i}__1.png"), rgba)
    for i in range(4):
        cv2.imwrite(str(tmp_path / "backgrounds" / f"mat_{i}.jpg"), np.full((384, 512, 3), 60 + 20 * i, np.uint8))
    return tmp_path


def test_scenes_have_an_outline_on_every_item(tmp_path):
    products, backgrounds = generate.load_kit(kit(tmp_path))
    assert len(products) == 3 and len(backgrounds) == 4
    rng = random.Random(4)
    seen = 0
    for _ in range(12):
        img, polygons = generate.scene(products, backgrounds, rng)
        h, w = img.shape[:2]
        assert w in generate.SIZES
        for poly in polygons:
            assert poly.shape[1] == 2 and len(poly) >= 3
            assert (poly[:, 0] >= 0).all() and (poly[:, 0] < w).all() and (poly[:, 1] >= 0).all() and (poly[:, 1] < h).all()
            # an outline sits on an item: the inside of it is one of the products' colours, not the mat
            m = np.zeros((h, w), np.uint8)
            cv2.fillPoly(m, [poly.astype(np.int32)], 1)
            inside = img[m > 0].astype(int)
            assert (inside.max(axis=1) - inside.min(axis=1)).mean() > 40   # coloured, unlike the grey mat
            seen += 1
    assert seen > 10


def test_the_dataset_is_written_in_the_ultralytics_layout(tmp_path):
    k = kit(tmp_path / "kit")
    out = tmp_path / "dataset"
    import sys
    argv = sys.argv
    sys.argv = ["generate.py", "--kit", str(k), "--out", str(out), "--train", "6", "--val", "2", "--seed", "1",
                "--workers", "1"]
    try:
        generate.main()
    finally:
        sys.argv = argv
    assert len(list((out / "images" / "train").glob("*.jpg"))) == 6
    assert len(list((out / "labels" / "val").glob("*.txt"))) == 2
    assert "names:\n  0: item" in (out / "dataset.yaml").read_text()
    for label in (out / "labels" / "train").glob("*.txt"):
        for line in label.read_text().splitlines():
            parts = line.split()
            assert parts[0] == "0" and len(parts) % 2 == 1
            assert all(0 <= float(v) <= 1 for v in parts[1:])
